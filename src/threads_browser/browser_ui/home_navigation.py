"""Home navigation and actor verification for one headed browser page."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Locator, Page, expect
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ..errors import UiReadError
from ..feed.urls import HOME_URL, _profile_author, _same_home_origin
from .loading import CardNotReady as _CardNotReady

_PROFILE_NAME = re.compile(r"^(?:profile|your profile|account)$", re.IGNORECASE)


@dataclass(slots=True)
class HomeNavigationState:
    """The verified route and actor bound to one reader session."""

    expected_actor: str | None
    home_url: str | None = None
    verified_actor_route: str | None = None


class HomeNavigationHost(Protocol):
    _page: Page
    _readiness_prompt: Callable[[str, int], Awaitable[bool]] | None
    _readiness_timeout_ms: int
    _navigation_timeout_ms: int

    @property
    def _expected_actor(self) -> str | None: ...
    @_expected_actor.setter
    def _expected_actor(self, value: str | None) -> None: ...
    @property
    def _home_url(self) -> str | None: ...
    @_home_url.setter
    def _home_url(self, value: str | None) -> None: ...
    @property
    def _verified_actor_route(self) -> str | None: ...
    @_verified_actor_route.setter
    def _verified_actor_route(self, value: str | None) -> None: ...

    async def _for_you_control(self, *, timeout_ms: int | None = None) -> Locator: ...
    async def _for_you_is_active(self, control: Locator) -> bool: ...
    async def _visible_for_you_controls(self) -> list[Locator]: ...
    def _is_home_navigation_url(self, value: str) -> bool: ...
    async def _require_feed_url(self, *, route_error_code: str = "HOME_UI_UNVERIFIED") -> None: ...
    async def _check_blocker(self) -> None: ...
    async def _recoverable_stage(
        self,
        stage: str,
        timeout_code: str,
        attempt: Callable[..., Awaitable[object]],
        *,
        recoverable_codes: tuple[str, ...] = (),
        cancel_code: str | None = None,
    ) -> object: ...
    def _remaining_readiness_ms(self, deadline: float, timeout_code: str) -> int: ...
    def _mark_ui_transition(self) -> None: ...
    async def _wait_for_home_render(
        self, stage: str, *, timeout_code: str, require_active_for_you: bool, require_feed: bool
    ) -> None: ...
    async def _wait_for_pending_card(self, pending: _CardNotReady, deadline: float) -> None: ...
    async def _await_render_settled(
        self,
        stage: str,
        deadline: float,
        *,
        timeout_code: str,
        observe: Callable[[], Awaitable[object]],
    ) -> object: ...
    async def _observe_profile_render(self, deadline: float, *, timeout_code: str) -> object: ...
    async def _wait_for_verified_feed(
        self, deadline: float, *, timeout_code: str, route_error_code: str = "HOME_UI_UNVERIFIED"
    ) -> None: ...


class HomeNavigator:
    """Own bounded Home, For You, login verification, and profile navigation."""

    def __init__(self, host: HomeNavigationHost, *, expected_actor: str | None) -> None:
        self._host = host
        self.state = HomeNavigationState(expected_actor=expected_actor)

    async def _visible_profile_controls(self) -> list[Locator]:
        controls = await self._visible_navigation_controls("link")
        controls.extend(await self._visible_navigation_controls("button"))
        if len(controls) > 1:
            raise UiReadError("PROFILE_CONTROL_AMBIGUOUS")
        if not controls:
            controls = await self._visible_navigation_profile_text()
        if len(controls) > 1:
            raise UiReadError("PROFILE_CONTROL_AMBIGUOUS")
        return controls

    async def _visible_navigation_controls(self, role: str) -> list[Locator]:
        navigations = self._host._page.get_by_role("navigation")
        matches: list[Locator] = []
        for index in range(await navigations.count()):
            navigation = navigations.nth(index)
            if not await navigation.is_visible():
                continue
            controls = navigation.get_by_role(role, name=_PROFILE_NAME)
            for control_index in range(await controls.count()):
                control = controls.nth(control_index)
                if await control.is_visible():
                    matches.append(control)
        return matches

    async def _visible_navigation_profile_text(self) -> list[Locator]:
        navigations = self._host._page.get_by_role("navigation")
        matches: list[Locator] = []
        for index in range(await navigations.count()):
            navigation = navigations.nth(index)
            if not await navigation.is_visible():
                continue
            labels = navigation.get_by_text("Profile", exact=True)
            for label_index in range(await labels.count()):
                label = labels.nth(label_index)
                if await label.is_visible():
                    matches.append(label)
        return matches

    async def open_home(self) -> None:
        await self._navigate_home()
        self._host._mark_ui_transition()
        await self._host._wait_for_home_render(
            "HOME_NAVIGATION",
            timeout_code="HOME_NAVIGATION_TIMEOUT",
            require_active_for_you=False,
            require_feed=False,
        )
        control = await self._find_for_you_control()
        destination = await self._for_you_destination(control)
        if not await self._host._for_you_is_active(control):
            await self._activate_for_you(control, destination)
        await self._host._wait_for_home_render(
            "FOR_YOU_ACTIVATION",
            timeout_code="FOR_YOU_ACTIVATION_NOT_READY",
            require_active_for_you=True,
            require_feed=False,
        )
        await self._host._require_feed_url()
        self._host._home_url = self._host._page.url
        await self._host.verify_actor()

    async def _navigate_home(self) -> None:
        try:
            await self._host._page.goto(
                HOME_URL,
                wait_until="domcontentloaded",
                timeout=self._host._navigation_timeout_ms,
            )
        except PlaywrightTimeoutError:
            await self._host._recoverable_stage(
                "HOME_NAVIGATION",
                "HOME_NAVIGATION_TIMEOUT",
                self._recover_home_navigation,
                recoverable_codes=("HOME_NAVIGATION_TIMEOUT",),
            )
        else:
            await self._host._require_feed_url()

    async def _recover_home_navigation(self, deadline: float) -> None:
        while True:
            remaining = self._host._remaining_readiness_ms(
                deadline,
                "HOME_NAVIGATION_TIMEOUT",
            )
            current_url = self._host._page.url
            if current_url != "about:blank":
                if not _same_home_origin(current_url):
                    raise UiReadError("OFF_ORIGIN_REDIRECT")
                await self._host._check_blocker()
                path = urlsplit(current_url).path.casefold()
                if "challenge" in path or "checkpoint" in path:
                    raise UiReadError("CHALLENGE_REQUIRED")
                if path in {"/login", "/accounts/login"}:
                    raise UiReadError("LOGIN_REQUIRED")
                actor = _profile_author(current_url)
                if actor is not None and actor != self._host._expected_actor:
                    raise UiReadError("ACTOR_MISMATCH")
                if self._host._is_home_navigation_url(current_url):
                    await self._host._require_feed_url()
                    return
            try:
                await self._host._page.wait_for_url(
                    self._host._is_home_navigation_url,
                    timeout=remaining,
                )
            except PlaywrightTimeoutError:
                raise UiReadError("HOME_NAVIGATION_TIMEOUT") from None
            await self._host._require_feed_url()

    async def _find_for_you_control(self) -> Locator:
        async def find(deadline: float) -> Locator:
            remaining = self._host._remaining_readiness_ms(
                deadline,
                "FOR_YOU_CONTROL_NOT_READY",
            )
            try:
                return await self._host._for_you_control(timeout_ms=remaining)
            except UiReadError as error:
                if error.code != "HOME_UI_UNVERIFIED":
                    raise
                raise UiReadError("FOR_YOU_CONTROL_NOT_READY") from None

        control = await self._host._recoverable_stage(
            "FOR_YOU_CONTROL",
            "FOR_YOU_CONTROL_NOT_READY",
            find,
            recoverable_codes=("FOR_YOU_CONTROL_NOT_READY",),
        )
        return control

    async def _for_you_destination(self, control: Locator) -> str | None:
        href = await control.get_attribute("href")
        destination = None if href is None else urljoin(self._host._page.url, href)
        if destination is not None and not _same_home_origin(destination):
            raise UiReadError("OFF_ORIGIN_REDIRECT")
        return destination

    async def _activate_for_you(
        self,
        control: Locator,
        destination: str | None,
    ) -> None:
        current_url = self._host._page.url
        try:
            await control.click(timeout=self._host._navigation_timeout_ms)
        except PlaywrightTimeoutError:
            pass
        self._host._mark_ui_transition()
        await self._host._recoverable_stage(
            "FOR_YOU_ACTIVATION",
            "FOR_YOU_ACTIVATION_NOT_READY",
            lambda deadline: self._verify_for_you_activation(
                deadline,
                destination=destination,
                current_url=current_url,
            ),
            recoverable_codes=("FOR_YOU_ACTIVATION_NOT_READY",),
        )

    async def _verify_for_you_activation(
        self,
        deadline: float,
        *,
        destination: str | None,
        current_url: str,
    ) -> Locator:
        while True:
            remaining = self._host._remaining_readiness_ms(
                deadline,
                "FOR_YOU_ACTIVATION_NOT_READY",
            )
            await self._host._require_feed_url(route_error_code="FOR_YOU_ROUTE_CHANGED")
            controls = await self._host._visible_for_you_controls()
            if len(controls) > 1:
                raise UiReadError("FOR_YOU_CONTROL_AMBIGUOUS")
            if not controls:
                try:
                    await self._host._for_you_control(timeout_ms=remaining)
                except UiReadError as error:
                    if error.code != "HOME_UI_UNVERIFIED":
                        raise
                    raise UiReadError("FOR_YOU_ACTIVATION_NOT_READY") from None
                continue
            active_control = controls[0]
            if await self._host._for_you_is_active(active_control) or self._host._is_for_you_route(
                self._host._page.url
            ):
                return active_control
            if destination is not None and destination != current_url:
                try:
                    await self._host._page.wait_for_url(destination, timeout=remaining)
                except PlaywrightTimeoutError:
                    raise UiReadError("FOR_YOU_ACTIVATION_NOT_READY") from None
                continue
            try:
                await expect(active_control).to_have_attribute(
                    "aria-current",
                    "page",
                    timeout=remaining,
                )
            except (AssertionError, PlaywrightTimeoutError):
                try:
                    await expect(active_control).to_have_attribute(
                        "aria-selected",
                        "true",
                        timeout=self._host._remaining_readiness_ms(
                            deadline,
                            "FOR_YOU_ACTIVATION_NOT_READY",
                        ),
                    )
                except (AssertionError, PlaywrightTimeoutError):
                    if not self._host._is_for_you_route(self._host._page.url):
                        await self._host._require_feed_url(route_error_code="FOR_YOU_ROUTE_CHANGED")
                        raise UiReadError("FOR_YOU_ACTIVATION_NOT_READY") from None

    async def verify_actor(self) -> None:
        async def find_profile_control(deadline: float) -> Locator:
            while True:
                self._host._remaining_readiness_ms(deadline, "PROFILE_CONTROL_NOT_FOUND")
                if not _same_home_origin(self._host._page.url):
                    raise UiReadError("OFF_ORIGIN_REDIRECT")
                await self._host._check_blocker()
                if (
                    self._host._home_url is not None
                    and self._host._page.url != self._host._home_url
                ):
                    raise UiReadError("PROFILE_ROUTE_CHANGED")
                controls = await self._visible_profile_controls()
                if controls:
                    return controls[0]
                await self._host._wait_for_pending_card(
                    _CardNotReady(
                        self._host._page.locator("body"),
                        condition="retry",
                    ),
                    deadline,
                )

        control = await self._host._recoverable_stage(
            "PROFILE_CONTROL",
            "PROFILE_CONTROL_NOT_FOUND",
            find_profile_control,
            recoverable_codes=("PROFILE_CONTROL_NOT_FOUND",),
        )
        await self._host._follow_profile_control(control)

    async def _follow_profile_control(
        self,
        control: Locator,
        *,
        return_home: bool = True,
    ) -> str:
        href = await control.get_attribute("href")
        destination = None if href is None else urljoin(self._host._page.url, href)
        if destination is not None and not _same_home_origin(destination):
            raise UiReadError("OFF_ORIGIN_REDIRECT")
        current_url = self._host._page.url
        try:
            await control.click(timeout=self._host._readiness_timeout_ms)
        except PlaywrightTimeoutError:
            pass
        self._host._mark_ui_transition()

        async def verify_profile_navigation(deadline: float) -> str:
            remaining = self._host._remaining_readiness_ms(
                deadline,
                "PROFILE_CLICK_NO_NAVIGATION",
            )
            await self._host._check_blocker()
            if not _same_home_origin(self._host._page.url):
                raise UiReadError("OFF_ORIGIN_REDIRECT")
            if self._host._page.url == current_url:
                try:
                    await self._host._page.wait_for_url(
                        lambda url: url != current_url,
                        timeout=remaining,
                    )
                except PlaywrightTimeoutError:
                    raise UiReadError("PROFILE_CLICK_NO_NAVIGATION") from None
            actor = await self._host._verify_current_profile_route()
            await self._host._await_render_settled(
                "PROFILE_VERIFY",
                deadline,
                timeout_code="PROFILE_CLICK_NO_NAVIGATION",
                observe=self._host._observe_profile_render,
            )
            return actor

        actor = await self._host._recoverable_stage(
            "PROFILE_VERIFY",
            "PROFILE_CLICK_NO_NAVIGATION",
            verify_profile_navigation,
            recoverable_codes=("PROFILE_CLICK_NO_NAVIGATION",),
        )
        if return_home:
            await self._host._go_back_to_home()
        return actor

    async def _verify_current_profile_route(self) -> str:
        await self._host._check_blocker()
        if not _same_home_origin(self._host._page.url):
            raise UiReadError("OFF_ORIGIN_REDIRECT")
        try:
            path = urlsplit(self._host._page.url).path.casefold()
        except ValueError:
            raise UiReadError("PROFILE_ROUTE_UNVERIFIED") from None
        if "challenge" in path or "checkpoint" in path:
            raise UiReadError("CHALLENGE_REQUIRED")
        if path in {"/login", "/accounts/login"}:
            raise UiReadError("LOGIN_REQUIRED")
        if any(marker in path for marker in ("rate_limit", "too_many_requests")):
            raise UiReadError("RATE_LIMITED")
        actor = _profile_author(self._host._page.url)
        if actor is None:
            raise UiReadError("PROFILE_ROUTE_UNVERIFIED")
        if self._host._expected_actor is not None and actor != self._host._expected_actor:
            raise UiReadError("ACTOR_MISMATCH")
        self._host._expected_actor = actor
        self._host._verified_actor_route = self._host._page.url
        return actor

    async def _go_back_to_home(self) -> None:
        previous_url = self._host._page.url
        try:
            await self._host._page.go_back(
                wait_until="domcontentloaded",
                timeout=self._host._navigation_timeout_ms,
            )
        except PlaywrightTimeoutError:
            pass
        self._host._mark_ui_transition()

        async def verify_home_return(deadline: float) -> None:
            self._host._remaining_readiness_ms(deadline, "RETURN_UNVERIFIED")
            if not _same_home_origin(self._host._page.url):
                raise UiReadError("OFF_ORIGIN_REDIRECT")
            await self._host._check_blocker()
            path = urlsplit(self._host._page.url).path.casefold()
            if "challenge" in path or "checkpoint" in path:
                raise UiReadError("CHALLENGE_REQUIRED")
            if path in {"/login", "/accounts/login"}:
                raise UiReadError("LOGIN_REQUIRED")
            route_actor = _profile_author(self._host._page.url)
            if route_actor is not None and route_actor != self._host._expected_actor:
                raise UiReadError("ACTOR_MISMATCH")
            if self._host._page.url == previous_url:
                try:
                    await self._host._page.wait_for_url(
                        lambda url: url != previous_url,
                        timeout=self._host._remaining_readiness_ms(
                            deadline,
                            "RETURN_UNVERIFIED",
                        ),
                    )
                except PlaywrightTimeoutError:
                    raise UiReadError("RETURN_UNVERIFIED") from None
            await self._host._wait_for_verified_feed(
                deadline,
                timeout_code="RETURN_UNVERIFIED",
                route_error_code="RETURN_ROUTE_CHANGED",
            )
            if self._host._page.url != self._host._home_url:
                raise UiReadError("RETURN_ROUTE_CHANGED")
            await self._host._await_render_settled(
                "HOME_RETURN",
                deadline,
                timeout_code="RETURN_UNVERIFIED",
                observe=lambda: self._host._observe_home_render(
                    deadline,
                    timeout_code="RETURN_UNVERIFIED",
                    require_active_for_you=True,
                    require_feed=False,
                ),
            )

        await self._host._recoverable_stage(
            "HOME_RETURN",
            "RETURN_UNVERIFIED",
            verify_home_return,
            recoverable_codes=("RETURN_UNVERIFIED",),
        )

    async def _return_to_threads_home(self) -> None:
        for attempt in range(2):
            try:
                await self._host._page.goto(
                    HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=self._host._navigation_timeout_ms,
                )
                break
            except PlaywrightTimeoutError:
                if self._host._is_home_navigation_url(self._host._page.url):
                    break
                if attempt == 1:
                    raise UiReadError("HOME_NAVIGATION_TIMEOUT") from None
        if not _same_home_origin(self._host._page.url):
            raise UiReadError("OFF_ORIGIN_REDIRECT")
        if not self._host._is_home_navigation_url(self._host._page.url):
            raise UiReadError("HOME_NAVIGATION_TIMEOUT")
        await self._host._check_blocker()
