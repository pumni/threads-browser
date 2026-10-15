"""Home shell, actor route and For You session verification."""

from __future__ import annotations

import re
from typing import Protocol
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Locator, Page, expect
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ..errors import UiReadError
from ..feed.relevance import normalize_text
from ..feed.urls import _profile_author, _same_home_origin
from .loading import CardNotReady as _CardNotReady

_FOR_YOU_NAME = re.compile(r"^for you$", re.IGNORECASE)


class HomeSessionHost(Protocol):
    _page: Page
    _expected_actor: str
    _home_url: str | None
    _verified_actor_route: str | None

    async def _visible_navigation_controls(self, role: str) -> list[Locator]: ...
    async def _visible_navigation_profile_text(self) -> list[Locator]: ...
    def _remaining_readiness_ms(self, deadline: float, timeout_code: str) -> int: ...
    async def _wait_for_pending_card(self, pending: _CardNotReady, deadline: float) -> None: ...


class HomeSession:
    """Verify the home shell, account route and For You state through Playwright."""

    def __init__(self, host: HomeSessionHost, *, readiness_timeout_ms: int) -> None:
        self._host = host
        self._readiness_timeout_ms = readiness_timeout_ms

    async def _for_you_control(self, *, timeout_ms: int | None = None) -> Locator:
        links = self._host._page.get_by_role("link", name=_FOR_YOU_NAME)
        tabs = self._host._page.get_by_role("tab", name=_FOR_YOU_NAME)
        try:
            await expect(links.or_(tabs).first).to_be_visible(
                timeout=timeout_ms or self._readiness_timeout_ms
            )
        except (AssertionError, PlaywrightTimeoutError):
            raise UiReadError("HOME_UI_UNVERIFIED") from None
        matches = await self._visible_for_you_controls()
        if len(matches) > 1:
            raise UiReadError("FOR_YOU_CONTROL_AMBIGUOUS")
        if not matches:
            raise UiReadError("HOME_UI_UNVERIFIED")
        return matches[0]

    async def _visible_for_you_controls(self) -> list[Locator]:
        matches: list[Locator] = []
        links = self._host._page.get_by_role("link", name=_FOR_YOU_NAME)
        tabs = self._host._page.get_by_role("tab", name=_FOR_YOU_NAME)
        for controls in (links, tabs):
            for index in range(await controls.count()):
                control = controls.nth(index)
                if await control.is_visible():
                    matches.append(control)
        return matches

    @staticmethod
    async def _for_you_is_active(control: Locator) -> bool:
        return (
            await control.get_attribute("aria-current") == "page"
            or await control.get_attribute("aria-selected") == "true"
        )

    @staticmethod
    def _is_for_you_route(value: str) -> bool:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        return (
            parsed.scheme == "https"
            and parsed.netloc.casefold() == "www.threads.com"
            and parsed.path == "/for_you"
            and not parsed.query
            and not parsed.fragment
        )

    @staticmethod
    def _is_home_navigation_url(value: str) -> bool:
        try:
            parsed = urlsplit(str(value))
        except ValueError:
            return False
        return (
            parsed.scheme == "https"
            and parsed.netloc.casefold() == "www.threads.com"
            and parsed.path in {"/", "/home", "/for_you"}
            and not parsed.query
            and not parsed.fragment
        )

    async def _require_feed_url(
        self,
        *,
        route_error_code: str = "HOME_UI_UNVERIFIED",
    ) -> None:
        if not _same_home_origin(self._host._page.url):
            raise UiReadError("OFF_ORIGIN_REDIRECT")
        try:
            parsed = urlsplit(self._host._page.url)
        except ValueError:
            raise UiReadError("HOME_UI_UNVERIFIED") from None
        if parsed.path not in {"/", "/home", "/for_you"} or parsed.query or parsed.fragment:
            path = parsed.path.casefold()
            if "challenge" in path or "checkpoint" in path:
                raise UiReadError("CHALLENGE_REQUIRED")
            if path in {"/login", "/accounts/login"}:
                raise UiReadError("LOGIN_REQUIRED")
            actor = _profile_author(self._host._page.url)
            if actor is not None and actor != self._host._expected_actor:
                raise UiReadError("ACTOR_MISMATCH")
            raise UiReadError(route_error_code)
        await self._check_blocker()
        if self._host._home_url is None:
            return
        if self._host._page.url != self._host._home_url:
            raise UiReadError(route_error_code)
        if self._host._verified_actor_route is None:
            raise UiReadError("ACTOR_UNVERIFIED")
        verified_actor = _profile_author(self._host._verified_actor_route)
        if verified_actor != self._host._expected_actor:
            raise UiReadError("ACTOR_MISMATCH")
        profile_controls = await self._host._visible_navigation_controls("link")
        profile_controls.extend(await self._host._visible_navigation_controls("button"))
        if len(profile_controls) > 1:
            raise UiReadError("PROFILE_CONTROL_AMBIGUOUS")
        if not profile_controls:
            profile_controls = await self._host._visible_navigation_profile_text()
        if len(profile_controls) > 1:
            raise UiReadError("PROFILE_CONTROL_AMBIGUOUS")
        if not profile_controls:
            raise UiReadError("PROFILE_CONTROL_NOT_READY")
        profile_href = await profile_controls[0].get_attribute("href")
        if profile_href is not None:
            profile_destination = urljoin(self._host._page.url, profile_href)
            if not _same_home_origin(profile_destination):
                raise UiReadError("OFF_ORIGIN_REDIRECT")
            profile_actor = _profile_author(profile_destination)
            if profile_actor is not None and profile_actor != self._host._expected_actor:
                raise UiReadError("ACTOR_MISMATCH")

        for_you_controls = await self._visible_for_you_controls()
        if not for_you_controls:
            raise UiReadError("FOR_YOU_CONTROL_NOT_READY")
        if len(for_you_controls) > 1:
            raise UiReadError("FOR_YOU_CONTROL_AMBIGUOUS")
        active = await self._for_you_is_active(for_you_controls[0])
        if not active and not self._is_for_you_route(self._host._page.url):
            raise UiReadError("FOR_YOU_STATE_CHANGED")

    async def _wait_for_verified_feed(
        self,
        deadline: float,
        *,
        timeout_code: str,
        route_error_code: str = "HOME_UI_UNVERIFIED",
    ) -> None:
        while True:
            self._host._remaining_readiness_ms(deadline, timeout_code)
            try:
                await self._require_feed_url(route_error_code=route_error_code)
            except UiReadError as error:
                if error.code not in {
                    "PROFILE_CONTROL_NOT_READY",
                    "FOR_YOU_CONTROL_NOT_READY",
                }:
                    raise
                await self._host._wait_for_pending_card(
                    _CardNotReady(
                        self._host._page.locator("body"),
                        condition="retry",
                    ),
                    deadline,
                )
            else:
                return

    async def _check_blocker(self) -> None:
        title = normalize_text(await self._host._page.title())
        headings = self._host._page.get_by_role("heading")
        parts = [title]
        for heading in await headings.all():
            if await heading.is_visible():
                parts.append(normalize_text(await heading.inner_text()))
        visible_labels = " ".join(parts)
        if any(marker in visible_labels for marker in ("log in", "sign in", "login")):
            raise UiReadError("LOGIN_REQUIRED")
        if any(
            marker in visible_labels
            for marker in ("challenge", "checkpoint", "verify your identity", "security check")
        ):
            raise UiReadError("CHALLENGE_REQUIRED")
        if any(
            marker in visible_labels
            for marker in (
                "rate limit",
                "too many requests",
                "try again later",
                "temporarily blocked",
            )
        ):
            raise UiReadError("RATE_LIMITED")
