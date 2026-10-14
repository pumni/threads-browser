"""Small async Playwright reader for a bounded Home / For You observation."""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Literal, TypeVar
from urllib.parse import urljoin, urlsplit, urlunsplit

from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    Locator,
    Page,
    expect,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

THREADS_ORIGIN = "https://www.threads.com"
HOME_URL = f"{THREADS_ORIGIN}/"
_NAVIGATION_TIMEOUT_MS = 20_000
_DETAIL_TIMEOUT_MS = 15_000
_READINESS_TIMEOUT_MS = 8_000
_AUTOMATIC_READINESS_WINDOWS = 2
_READINESS_RECHECK_INTERVAL_MS = 200
_MAX_BODY_CODEPOINTS = 10_000
_MAX_CARD_ANCESTORS = 8
_MAX_CARD_LINKS = 40
_MAX_PERMALINK_CANDIDATES = 40
_USERNAME = re.compile(r"[A-Za-z0-9._]{1,30}")
_POST_PATH = re.compile(r"/@(?P<author>[A-Za-z0-9._]{1,30})/post/(?P<post>[A-Za-z0-9_-]{1,120})")
_PROFILE_PATH = re.compile(r"/@(?P<author>[A-Za-z0-9._]{1,30})/?")
_FOR_YOU_NAME = re.compile(r"^for you$", re.IGNORECASE)
_PROFILE_NAME = re.compile(r"^(?:profile|your profile|account)$", re.IGNORECASE)
_MORE_NAME = re.compile(r"^(?:more|see more|read more)$", re.IGNORECASE)
_ReadinessResult = TypeVar("_ReadinessResult")


class UiReadError(Exception):
    code: str

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _CardWaitSignal:
    locator: Locator
    condition: Literal["visible", "in_viewport", "nonempty_text", "mutation"]
    timeout_code: str | None = None


class _CardNotReady(Exception):
    def __init__(
        self,
        locator: Locator | None = None,
        *,
        in_viewport: bool = False,
        nonempty_text: bool = False,
        timeout_code: str | None = None,
        condition: Literal["visible", "mutation"] = "visible",
        signals: tuple[_CardWaitSignal, ...] | None = None,
    ) -> None:
        if signals is None:
            if locator is None:
                raise ValueError("card readiness requires a locator")
            if in_viewport:
                condition = "in_viewport"
            elif nonempty_text:
                condition = "nonempty_text"
            signals = (_CardWaitSignal(locator, condition, timeout_code),)
        self.signals = signals
        self.timeout_code = timeout_code or next(
            (signal.timeout_code for signal in signals if signal.timeout_code),
            None,
        )
        super().__init__()

    @classmethod
    def combine(cls, pending: list[_CardNotReady]) -> _CardNotReady:
        return cls(signals=tuple(signal for item in pending for signal in item.signals))


@dataclass(frozen=True, slots=True, repr=False)
class PostTarget:
    url: str = field(repr=False)
    author: str = field(repr=False)
    body: str | None = field(repr=False)
    fingerprint: str
    permalink: Locator = field(repr=False)
    visible_after_scroll: bool = field(default=False, repr=False)
    viewport_y: float = field(default=0.0, repr=False)

    def __repr__(self) -> str:
        return "PostTarget(<private>)"


@dataclass(frozen=True, slots=True)
class HomeSample:
    targets: tuple[PostTarget, ...]
    observed_targets: tuple[PostTarget, ...]


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def canonical_terms(terms: tuple[str, ...]) -> tuple[str, ...]:
    cleaned = tuple(dict.fromkeys(normalize_text(term) for term in terms if term.strip()))
    if not cleaned or any(len(term) > 100 for term in cleaned):
        raise UiReadError("TERMS_INVALID")
    return cleaned


def classify_body(body: str | None, terms: tuple[str, ...]) -> str:
    if body is None or not body.strip():
        return "UNKNOWN"
    normalized = normalize_text(body)
    return "RELEVANT" if any(term in normalized for term in terms) else "IRRELEVANT"


def _post_url(value: str) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(urljoin(HOME_URL, value))
    except ValueError:
        return None
    match = _POST_PATH.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != "www.threads.com"
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        return None
    canonical = urlunsplit(("https", "www.threads.com", parsed.path, "", ""))
    return canonical, match.group("author").casefold()


def _profile_author(value: str) -> str | None:
    try:
        parsed = urlsplit(urljoin(HOME_URL, value))
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != "www.threads.com"
        or parsed.query
        or parsed.fragment
    ):
        return None
    match = _PROFILE_PATH.fullmatch(parsed.path)
    return None if match is None else match.group("author").casefold()


def _same_home_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.netloc.casefold() == "www.threads.com"


class ThreadsHomeReader:
    """Reads one Home / For You sample and at most one exact visible post."""

    def __init__(
        self,
        page: Page,
        expected_actor: str,
        *,
        readiness_prompt: Callable[[str, int], Awaitable[bool]] | None = None,
    ) -> None:
        if _USERNAME.fullmatch(expected_actor) is None:
            raise UiReadError("ACTOR_INVALID")
        self._page = page
        self._expected_actor = expected_actor.casefold()
        self._readiness_prompt = readiness_prompt
        self._home_url: str | None = None
        self._verified_actor_route: str | None = None
        self.scroll_attempts = 0
        self.scrolls = 0
        self.feed_duplicates = 0

    async def open_home(self) -> None:
        try:
            await self._page.goto(
                HOME_URL,
                wait_until="domcontentloaded",
                timeout=_NAVIGATION_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            async def recover_home_navigation(deadline: float) -> None:
                while True:
                    remaining = self._remaining_readiness_ms(
                        deadline,
                        "HOME_NAVIGATION_TIMEOUT",
                    )
                    current_url = self._page.url
                    if current_url != "about:blank":
                        if not _same_home_origin(current_url):
                            raise UiReadError("OFF_ORIGIN_REDIRECT")
                        await self._check_blocker()
                        path = urlsplit(current_url).path.casefold()
                        if "challenge" in path or "checkpoint" in path:
                            raise UiReadError("CHALLENGE_REQUIRED")
                        if path in {"/login", "/accounts/login"}:
                            raise UiReadError("LOGIN_REQUIRED")
                        actor = _profile_author(current_url)
                        if actor is not None and actor != self._expected_actor:
                            raise UiReadError("ACTOR_MISMATCH")
                        if self._is_home_navigation_url(current_url):
                            await self._require_feed_url()
                            return
                    try:
                        await self._page.wait_for_url(
                            self._is_home_navigation_url,
                            timeout=remaining,
                        )
                    except PlaywrightTimeoutError:
                        raise UiReadError("HOME_NAVIGATION_TIMEOUT") from None
                    await self._require_feed_url()

            await self._recoverable_stage(
                "HOME_NAVIGATION",
                "HOME_NAVIGATION_TIMEOUT",
                recover_home_navigation,
                recoverable_codes=("HOME_NAVIGATION_TIMEOUT",),
            )
        else:
            await self._require_feed_url()

        async def find_for_you_control(deadline: float) -> Locator:
            remaining = self._remaining_readiness_ms(
                deadline,
                "FOR_YOU_CONTROL_NOT_READY",
            )
            try:
                return await self._for_you_control(timeout_ms=remaining)
            except UiReadError as error:
                if error.code != "HOME_UI_UNVERIFIED":
                    raise
                raise UiReadError("FOR_YOU_CONTROL_NOT_READY") from None

        control = await self._recoverable_stage(
            "FOR_YOU_CONTROL",
            "FOR_YOU_CONTROL_NOT_READY",
            find_for_you_control,
            recoverable_codes=("FOR_YOU_CONTROL_NOT_READY",),
        )
        href = await control.get_attribute("href")
        destination = None if href is None else urljoin(self._page.url, href)
        if destination is not None and not _same_home_origin(destination):
            raise UiReadError("OFF_ORIGIN_REDIRECT")
        if not await self._for_you_is_active(control):
            current_url = self._page.url
            try:
                await control.click(timeout=_NAVIGATION_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                pass

            async def verify_for_you_activation(deadline: float) -> Locator:
                while True:
                    remaining = self._remaining_readiness_ms(
                        deadline,
                        "FOR_YOU_ACTIVATION_NOT_READY",
                    )
                    await self._require_feed_url(
                        route_error_code="FOR_YOU_ROUTE_CHANGED"
                    )
                    controls = await self._visible_for_you_controls()
                    if len(controls) > 1:
                        raise UiReadError("FOR_YOU_CONTROL_AMBIGUOUS")
                    if not controls:
                        try:
                            await self._for_you_control(timeout_ms=remaining)
                        except UiReadError as error:
                            if error.code != "HOME_UI_UNVERIFIED":
                                raise
                            raise UiReadError(
                                "FOR_YOU_ACTIVATION_NOT_READY"
                            ) from None
                        continue
                    active_control = controls[0]
                    if await self._for_you_is_active(active_control) or self._is_for_you_route(
                        self._page.url
                    ):
                        return active_control
                    if destination is not None and destination != current_url:
                        try:
                            await self._page.wait_for_url(
                                destination,
                                timeout=remaining,
                            )
                        except PlaywrightTimeoutError:
                            raise UiReadError(
                                "FOR_YOU_ACTIVATION_NOT_READY"
                            ) from None
                        continue
                    handle = await active_control.element_handle()
                    if handle is None:
                        await self._wait_for_pending_card(
                            _CardNotReady(
                                self._page.locator("body"),
                                condition="mutation",
                            ),
                            deadline,
                        )
                        continue
                    result = None
                    try:
                        result = await self._page.wait_for_function(
                            "element => element.isConnected && "
                            "(element.getAttribute('aria-current') === 'page' || "
                            "element.getAttribute('aria-selected') === 'true' || "
                            "(location.origin === 'https://www.threads.com' && "
                            "location.pathname === '/for_you' && !location.search && "
                            "!location.hash))",
                            arg=handle,
                            timeout=remaining,
                            polling="raf",
                        )
                    except PlaywrightTimeoutError:
                        await self._require_feed_url(
                            route_error_code="FOR_YOU_ROUTE_CHANGED"
                        )
                        raise UiReadError("FOR_YOU_ACTIVATION_NOT_READY") from None
                    finally:
                        if result is not None:
                            await result.dispose()
                        await handle.dispose()

            control = await self._recoverable_stage(
                "FOR_YOU_ACTIVATION",
                "FOR_YOU_ACTIVATION_NOT_READY",
                verify_for_you_activation,
                recoverable_codes=("FOR_YOU_ACTIVATION_NOT_READY",),
            )
        await self._require_feed_url()
        self._home_url = self._page.url
        await self.verify_actor()
    async def verify_actor(self) -> None:
        async def find_profile_control(deadline: float) -> Locator:
            while True:
                self._remaining_readiness_ms(deadline, "PROFILE_CONTROL_NOT_FOUND")
                if not _same_home_origin(self._page.url):
                    raise UiReadError("OFF_ORIGIN_REDIRECT")
                await self._check_blocker()
                if self._home_url is not None and self._page.url != self._home_url:
                    raise UiReadError("PROFILE_ROUTE_CHANGED")
                controls = await self._visible_profile_controls()
                if controls:
                    return controls[0]
                await self._wait_for_pending_card(
                    _CardNotReady(
                        self._page.locator("body"),
                        condition="mutation",
                    ),
                    deadline,
                )

        control = await self._recoverable_stage(
            "PROFILE_CONTROL",
            "PROFILE_CONTROL_NOT_FOUND",
            find_profile_control,
            recoverable_codes=("PROFILE_CONTROL_NOT_FOUND",),
        )
        await self._follow_profile_control(control)

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

    async def _visible_navigation_controls(
        self,
        role: Literal["link", "button"],
    ) -> list[Locator]:
        navigations = self._page.get_by_role("navigation")
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
        navigations = self._page.get_by_role("navigation")
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

    async def _follow_profile_control(self, control: Locator) -> None:
        href = await control.get_attribute("href")
        destination = None if href is None else urljoin(self._page.url, href)
        if destination is not None and not _same_home_origin(destination):
            raise UiReadError("OFF_ORIGIN_REDIRECT")
        current_url = self._page.url
        try:
            await control.click(timeout=_READINESS_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            pass

        async def verify_profile_navigation(deadline: float) -> None:
            remaining = self._remaining_readiness_ms(
                deadline,
                "PROFILE_CLICK_NO_NAVIGATION",
            )
            await self._check_blocker()
            if not _same_home_origin(self._page.url):
                raise UiReadError("OFF_ORIGIN_REDIRECT")
            if self._page.url == current_url:
                try:
                    await self._page.wait_for_url(
                        lambda url: url != current_url,
                        timeout=remaining,
                    )
                except PlaywrightTimeoutError:
                    raise UiReadError("PROFILE_CLICK_NO_NAVIGATION") from None
            await self._verify_current_profile_route()

        await self._recoverable_stage(
            "PROFILE_VERIFY",
            "PROFILE_CLICK_NO_NAVIGATION",
            verify_profile_navigation,
            recoverable_codes=("PROFILE_CLICK_NO_NAVIGATION",),
        )
        await self._go_back_to_home()

    async def _verify_current_profile_route(self) -> None:
        await self._check_blocker()
        if not _same_home_origin(self._page.url):
            raise UiReadError("OFF_ORIGIN_REDIRECT")
        try:
            path = urlsplit(self._page.url).path.casefold()
        except ValueError:
            raise UiReadError("PROFILE_ROUTE_UNVERIFIED") from None
        if "challenge" in path or "checkpoint" in path:
            raise UiReadError("CHALLENGE_REQUIRED")
        if path in {"/login", "/accounts/login"}:
            raise UiReadError("LOGIN_REQUIRED")
        actor = _profile_author(self._page.url)
        if actor is None:
            raise UiReadError("PROFILE_ROUTE_UNVERIFIED")
        if actor != self._expected_actor:
            raise UiReadError("ACTOR_MISMATCH")
        self._verified_actor_route = self._page.url

    async def _go_back_to_home(self) -> None:
        previous_url = self._page.url
        try:
            await self._page.go_back(
                wait_until="domcontentloaded",
                timeout=_NAVIGATION_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            pass

        async def verify_home_return(deadline: float) -> None:
            self._remaining_readiness_ms(deadline, "RETURN_UNVERIFIED")
            if not _same_home_origin(self._page.url):
                raise UiReadError("OFF_ORIGIN_REDIRECT")
            await self._check_blocker()
            path = urlsplit(self._page.url).path.casefold()
            if "challenge" in path or "checkpoint" in path:
                raise UiReadError("CHALLENGE_REQUIRED")
            if path in {"/login", "/accounts/login"}:
                raise UiReadError("LOGIN_REQUIRED")
            route_actor = _profile_author(self._page.url)
            if route_actor is not None and route_actor != self._expected_actor:
                raise UiReadError("ACTOR_MISMATCH")
            if self._page.url == previous_url:
                try:
                    await self._page.wait_for_url(
                        lambda url: url != previous_url,
                        timeout=self._remaining_readiness_ms(
                            deadline,
                            "RETURN_UNVERIFIED",
                        ),
                    )
                except PlaywrightTimeoutError:
                    raise UiReadError("RETURN_UNVERIFIED") from None
            await self._wait_for_verified_feed(
                deadline,
                timeout_code="RETURN_UNVERIFIED",
                route_error_code="RETURN_ROUTE_CHANGED",
            )
            if self._page.url != self._home_url:
                raise UiReadError("RETURN_ROUTE_CHANGED")

        await self._recoverable_stage(
            "HOME_RETURN",
            "RETURN_UNVERIFIED",
            verify_home_return,
            recoverable_codes=("RETURN_UNVERIFIED",),
        )

    async def sample_and_scroll(self, max_posts: int) -> HomeSample:
        if not 1 <= max_posts <= 20:
            raise UiReadError("POST_LIMIT_INVALID")
        before, scroll_anchor = await self._wait_for_home_targets(
            max_posts,
            timeout_code="FEED_INITIAL_NOT_READY",
        )
        before_by_fingerprint = {target.fingerprint: target for target in before}
        before_by_url = {target.url: target for target in before}
        self.feed_duplicates = len(before) - len(before_by_fingerprint)
        self.scroll_attempts = 0
        self.scrolls = 0
        anchor_box = await before[0].permalink.bounding_box()
        if anchor_box is None:
            raise UiReadError("HOME_SCROLL_UNVERIFIED")
        try:
            await self._page.mouse.move(
                scroll_anchor["x"] + scroll_anchor["width"] / 2,
                scroll_anchor["y"] + scroll_anchor["height"] / 2,
            )
            self.scroll_attempts = 1
            await self._page.mouse.wheel(0, 850)
        except PlaywrightError:
            raise UiReadError("HOME_SCROLL_FAILED") from None

        async def observe_scroll(window_deadline: float) -> bool:
            while True:
                self._remaining_readiness_ms(
                    window_deadline,
                    "HOME_SCROLL_UNVERIFIED",
                )
                await self._wait_for_verified_feed(
                    window_deadline,
                    timeout_code="HOME_SCROLL_UNVERIFIED",
                )
                if await self._wait_for_scroll_movement(
                    before[0].permalink,
                    anchor_box["y"],
                    window_deadline,
                ):
                    return True
                try:
                    current, _ = await self._visible_targets(
                        max_posts,
                        visible_after_scroll=True,
                        prefer_new_fingerprints=set(before_by_fingerprint),
                    )
                except _CardNotReady as pending:
                    await self._wait_for_pending_card(
                        pending,
                        window_deadline,
                    )
                    continue
                if any(
                    target.url in before_by_url
                    and abs(target.viewport_y - before_by_url[target.url].viewport_y) > 1
                    for target in current
                ):
                    return True
                pending = await self._feed_readiness_signals()
                if not pending:
                    pending = _CardNotReady(
                        self._page.locator("body"),
                        condition="mutation",
                    )
                await self._wait_for_pending_card(pending, window_deadline)

        scroll_observed = await self._recoverable_stage(
            "HOME_SCROLL",
            "HOME_SCROLL_UNVERIFIED",
            observe_scroll,
        )
        after, _ = await self._wait_for_home_targets(
            max_posts,
            timeout_code="HOME_SCROLL_UNVERIFIED",
            visible_after_scroll=True,
            prefer_new_fingerprints=set(before_by_fingerprint),
            stage="FEED_SCROLL",
        )
        after_by_fingerprint = {target.fingerprint: target for target in after}
        self.feed_duplicates += len(after) - len(after_by_fingerprint)
        if not scroll_observed:
            raise UiReadError("HOME_SCROLL_UNVERIFIED")
        self.scrolls = 1
        for fingerprint in after_by_fingerprint:
            if fingerprint in before_by_fingerprint:
                self.feed_duplicates += 1
        after_first = list(after_by_fingerprint.values())
        after_first.extend(
            target
            for fingerprint, target in before_by_fingerprint.items()
            if fingerprint not in after_by_fingerprint
        )
        return HomeSample(
            targets=tuple(after_first[:max_posts]),
            observed_targets=tuple(after_first),
        )

    @staticmethod
    def _readiness_deadline(timeout_ms: int) -> float:
        return time.monotonic() + timeout_ms / 1000

    @staticmethod
    def _remaining_readiness_ms(deadline: float, timeout_code: str) -> int:
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise UiReadError(timeout_code)
        return remaining

    async def _recoverable_stage(
        self,
        stage: str,
        timeout_code: str,
        attempt: Callable[[float], Awaitable[_ReadinessResult]],
        *,
        recoverable_codes: tuple[str, ...] | None = None,
    ) -> _ReadinessResult:
        allowed_codes = recoverable_codes or (timeout_code,)
        window = 0
        final_code = timeout_code
        while True:
            deadline = self._readiness_deadline(_READINESS_TIMEOUT_MS)
            try:
                result = await attempt(deadline)
            except UiReadError as error:
                if error.code not in allowed_codes:
                    raise
                final_code = error.code
            else:
                if window:
                    print(f"RECOVERED stage={stage} retry={window}")
                return result

            window += 1
            print(f"WAITING stage={stage} retry={window}")
            if window < _AUTOMATIC_READINESS_WINDOWS:
                continue
            if self._readiness_prompt is None:
                raise UiReadError(final_code) from None
            if not await self._readiness_prompt(stage, window):
                raise UiReadError(final_code) from None

    async def _wait_for_home_targets(
        self,
        max_posts: int,
        *,
        timeout_code: str,
        visible_after_scroll: bool = False,
        prefer_new_fingerprints: set[str] | None = None,
        stage: str | None = None,
    ) -> tuple[tuple[PostTarget, ...], dict[str, float]]:
        async def attempt(window_deadline: float) -> tuple[tuple[PostTarget, ...], dict[str, float]]:
            while True:
                self._remaining_readiness_ms(window_deadline, timeout_code)
                await self._wait_for_verified_feed(
                    window_deadline,
                    timeout_code=timeout_code,
                )
                try:
                    targets, scroll_anchor = await self._visible_targets(
                        max_posts,
                        visible_after_scroll=visible_after_scroll,
                        prefer_new_fingerprints=prefer_new_fingerprints,
                    )
                except _CardNotReady as pending:
                    await self._wait_for_pending_card(
                        pending,
                        window_deadline,
                    )
                    continue
                if targets and scroll_anchor is not None:
                    self._remaining_readiness_ms(window_deadline, timeout_code)
                    await self._wait_for_verified_feed(
                        window_deadline,
                        timeout_code=timeout_code,
                    )
                    return targets, scroll_anchor
                pending = await self._feed_readiness_signals()
                if not pending:
                    pending = _CardNotReady(
                        self._page.locator("body"),
                        condition="mutation",
                    )
                await self._wait_for_pending_card(
                    pending,
                    window_deadline,
                )

        stage_name = stage or {
            "FEED_INITIAL_NOT_READY": "FEED_INITIAL",
            "FEED_RETURN_NOT_READY": "FEED_RETURN",
            "HOME_SCROLL_UNVERIFIED": "FEED_SCROLL",
        }.get(timeout_code, "FEED")
        return await self._recoverable_stage(
            stage_name,
            timeout_code,
            attempt,
        )

    async def _feed_readiness_signals(self) -> _CardNotReady | None:
        signals: list[_CardWaitSignal] = []
        viewport = self._page.viewport_size
        articles = self._page.get_by_role("article")
        article_count = await articles.count()
        for index in range(min(article_count, _MAX_PERMALINK_CANDIDATES)):
            article = articles.nth(index)
            if await self._is_outside_content_landmark(article):
                continue
            box = await article.bounding_box()
            if viewport is None or box is None or not self._intersects_viewport(box, viewport):
                signals.append(_CardWaitSignal(article, "in_viewport"))

        if article_count == 0:
            region = await self._content_region()
            permalinks = region.locator('a[href*="/post/"]')
            count = await permalinks.count()
            for index in range(min(count, _MAX_PERMALINK_CANDIDATES)):
                permalink = permalinks.nth(index)
                if await self._is_outside_content_landmark(permalink):
                    continue
                href = await permalink.get_attribute("href")
                if href is None or _post_url(href) is None:
                    raise UiReadError("POST_PROVENANCE_UNVERIFIED")
                box = await permalink.bounding_box()
                if (
                    viewport is None
                    or box is None
                    or not self._intersects_viewport(box, viewport)
                ):
                    signals.append(_CardWaitSignal(permalink, "in_viewport"))

        if not signals:
            return None
        signals.append(_CardWaitSignal(self._page.locator("body"), "mutation"))
        return _CardNotReady(signals=tuple(signals))

    async def _wait_for_pending_card(
        self,
        pending: _CardNotReady,
        deadline: float,
    ) -> None:
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            return
        wait_ms = min(remaining, _READINESS_RECHECK_INTERVAL_MS)

        async def wait_for_signal(signal: _CardWaitSignal) -> bool:
            try:
                if signal.condition == "in_viewport":
                    await expect(signal.locator).to_be_in_viewport(timeout=wait_ms)
                elif signal.condition == "nonempty_text":
                    await expect(signal.locator).to_be_visible(timeout=wait_ms)
                    await expect(signal.locator).to_have_text(
                        re.compile(r"\S"),
                        timeout=wait_ms,
                    )
                elif signal.condition == "mutation":
                    return bool(
                        await signal.locator.evaluate(
                            """(element, timeoutMs) => new Promise(resolve => {
                                const observer = new MutationObserver(() => {
                                    observer.disconnect();
                                    clearTimeout(timer);
                                    resolve(true);
                                });
                                observer.observe(element, {
                                    attributes: true,
                                    childList: true,
                                    characterData: true,
                                    subtree: true,
                                });
                                const timer = setTimeout(() => {
                                    observer.disconnect();
                                    resolve(false);
                                }, timeoutMs);
                            })""",
                            wait_ms,
                        )
                    )
                else:
                    await expect(signal.locator).to_be_visible(timeout=wait_ms)
                return True
            except (AssertionError, PlaywrightError):
                return False

        tasks = {
            asyncio.create_task(wait_for_signal(signal)) for signal in pending.signals
        }
        _, unfinished = await asyncio.wait(
            tasks,
            timeout=wait_ms / 1000,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in unfinished:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _wait_for_scroll_movement(
        self,
        permalink: Locator,
        initial_y: float,
        deadline: float,
    ) -> bool:
        handle = await permalink.element_handle()
        if handle is None:
            return False
        result = None
        outcome: str | None = None
        try:
            result = await self._page.wait_for_function(
                "([element, initialY]) => !element.isConnected ? 'detached' "
                ": Math.abs(element.getBoundingClientRect().y - initialY) > 1 "
                "? 'moved' : false",
                arg=[handle, initial_y],
                timeout=min(
                    self._remaining_readiness_ms(deadline, "HOME_SCROLL_UNVERIFIED"),
                    _READINESS_RECHECK_INTERVAL_MS,
                ),
                polling="raf",
            )
            outcome = await result.json_value()
        except PlaywrightTimeoutError:
            return False
        except PlaywrightError:
            raise UiReadError("HOME_SCROLL_UNVERIFIED") from None
        finally:
            if result is not None:
                try:
                    await result.dispose()
                except PlaywrightError:
                    pass
            try:
                await handle.dispose()
            except PlaywrightError:
                pass
        return outcome == "moved"

    async def _visible_targets(
        self,
        max_posts: int,
        *,
        visible_after_scroll: bool = False,
        prefer_new_fingerprints: set[str] | None = None,
    ) -> tuple[tuple[PostTarget, ...], dict[str, float] | None]:
        articles = self._page.get_by_role("article")
        viewport = self._page.viewport_size
        if viewport is None:
            raise UiReadError("FEED_VIEWPORT_UNVERIFIED")
        targets: list[PostTarget] = []
        scroll_anchor: dict[str, float] | None = None
        article_count = await articles.count()
        if article_count == 0:
            return await self._visible_permalink_targets(
                max_posts,
                visible_after_scroll=visible_after_scroll,
                prefer_new_fingerprints=prefer_new_fingerprints,
            )
        scan_limit = min(article_count, max_posts + 20)
        visible_limit = max_posts if prefer_new_fingerprints is None else max_posts + 20
        pending_cards: list[_CardNotReady] = []
        for index in range(scan_limit):
            article = articles.nth(index)
            if not await article.is_visible():
                continue
            box = await article.bounding_box()
            if (
                box is None
                or box["x"] >= viewport["width"]
                or box["y"] >= viewport["height"]
                or box["x"] + box["width"] <= 0
                or box["y"] + box["height"] <= 0
            ):
                continue
            if scroll_anchor is None:
                scroll_anchor = box
            try:
                target = await self._read_card(
                    article,
                    visible_after_scroll=visible_after_scroll,
                    viewport_y=box["y"],
                )
            except _CardNotReady as pending:
                pending_cards.append(pending)
                continue
            targets.append(target)
            if len(targets) >= visible_limit:
                break
        if not targets and pending_cards:
            raise _CardNotReady.combine(pending_cards)
        if prefer_new_fingerprints is not None:
            targets.sort(key=lambda target: target.fingerprint in prefer_new_fingerprints)
            targets = targets[:max_posts]
        return tuple(targets), scroll_anchor

    @staticmethod
    def _intersects_viewport(
        box: dict[str, float],
        viewport: dict[str, int],
    ) -> bool:
        return (
            box["x"] < viewport["width"]
            and box["y"] < viewport["height"]
            and box["x"] + box["width"] > 0
            and box["y"] + box["height"] > 0
        )

    async def _content_region(
        self,
        *,
        prefer_main: bool = False,
        error_code: str = "FEED_REGION_UNVERIFIED",
    ) -> Locator:
        roles = ("main", "feed") if prefer_main else ("feed", "main")
        for role in roles:
            landmarks = self._page.get_by_role(role)
            visible: list[Locator] = []
            for index in range(await landmarks.count()):
                landmark = landmarks.nth(index)
                if await landmark.is_visible():
                    visible.append(landmark)
            if len(visible) > 1:
                raise UiReadError(error_code)
            if visible:
                return visible[0]
        return self._page.locator("body")

    @staticmethod
    async def _is_outside_content_landmark(link: Locator) -> bool:
        excluded = link.locator(
            "xpath=ancestor-or-self::*[self::nav or self::aside or self::footer "
            "or @role='navigation' or @role='menu' or @role='complementary' "
            "or @role='dialog' or @role='search' or @role='banner' "
            "or @role='contentinfo']"
        )
        return await excluded.count() > 0

    async def _resolve_permalink_card(
        self,
        permalink: Locator,
        expected_url: str,
        expected_author: str,
    ) -> tuple[Locator, dict[str, float]]:
        ancestor = permalink
        smallest_without_body: tuple[Locator, dict[str, float]] | None = None
        pending_author: Locator | None = None
        for _ in range(_MAX_CARD_ANCESTORS):
            ancestor = ancestor.locator("xpath=..")
            if await ancestor.count() == 0:
                break
            if (
                await ancestor.get_attribute("role") in {"feed", "main"}
                or await ancestor.locator("xpath=self::main").count() > 0
            ):
                break
            if not await ancestor.is_visible():
                continue

            links = ancestor.get_by_role("link")
            link_count = await links.count()
            if link_count > _MAX_CARD_LINKS:
                raise UiReadError("POST_PROVENANCE_UNVERIFIED")
            post_authors: dict[str, str] = {}
            authors: set[str] = set()
            for index in range(link_count):
                link = links.nth(index)
                if await self._is_outside_content_landmark(link):
                    continue
                href = await link.get_attribute("href")
                if href is None:
                    continue
                parsed_post = _post_url(href)
                if parsed_post is not None:
                    post_authors[parsed_post[0]] = parsed_post[1]
                    continue
                if "/post/" in href:
                    raise UiReadError("POST_PROVENANCE_UNVERIFIED")
                author = _profile_author(href)
                if author is not None:
                    authors.add(author)
            if len(post_authors) > 1:
                raise UiReadError("POST_PROVENANCE_UNVERIFIED")
            if expected_url not in post_authors:
                continue
            if post_authors[expected_url] != expected_author:
                raise UiReadError("POST_AUTHOR_UNVERIFIED")
            if len(authors) > 1:
                raise UiReadError("POST_PROVENANCE_UNVERIFIED")
            if authors and expected_author not in authors:
                raise UiReadError("POST_AUTHOR_UNVERIFIED")

            box = await ancestor.bounding_box()
            if box is None:
                continue
            if not authors:
                if pending_author is None:
                    pending_author = self._expected_profile_link_signal(
                        ancestor,
                        expected_author,
                    )
                continue
            candidate = (ancestor, box)
            if smallest_without_body is None:
                smallest_without_body = candidate
            paragraphs = ancestor.get_by_role("paragraph")
            paragraph_count = await paragraphs.count()
            if paragraph_count > 1:
                return candidate
            if paragraph_count == 1 and await paragraphs.first.is_visible():
                return candidate

        if pending_author is not None:
            raise _CardNotReady(pending_author)
        if smallest_without_body is not None:
            return smallest_without_body
        raise UiReadError("POST_PROVENANCE_UNVERIFIED")

    @staticmethod
    def _expected_profile_link_signal(root: Locator, author: str) -> Locator:
        return root.locator(
            f'a[href$="/@{author}" i], a[href$="/@{author}/" i]'
        ).first

    async def _visible_permalink_targets(
        self,
        max_posts: int,
        *,
        visible_after_scroll: bool,
        prefer_new_fingerprints: set[str] | None,
    ) -> tuple[tuple[PostTarget, ...], dict[str, float] | None]:
        viewport = self._page.viewport_size
        if viewport is None:
            raise UiReadError("FEED_VIEWPORT_UNVERIFIED")
        region = await self._content_region()
        permalinks = region.locator('a[href*="/post/"]')
        scan_limit = min(
            await permalinks.count(),
            min(max_posts + 20, _MAX_PERMALINK_CANDIDATES),
        )
        visible_limit = max_posts if prefer_new_fingerprints is None else max_posts + 20
        targets_by_url: dict[str, PostTarget] = {}
        scroll_anchor: dict[str, float] | None = None
        pending_cards: list[_CardNotReady] = []
        for index in range(scan_limit):
            permalink = permalinks.nth(index)
            if not await permalink.is_visible():
                continue
            if await self._is_outside_content_landmark(permalink):
                continue
            href = await permalink.get_attribute("href")
            parsed_post = None if href is None else _post_url(href)
            if parsed_post is None:
                if href is not None and "/post/" in href:
                    raise UiReadError("POST_PROVENANCE_UNVERIFIED")
                continue
            if parsed_post[0] in targets_by_url:
                continue
            permalink_box = await permalink.bounding_box()
            if permalink_box is None or not self._intersects_viewport(permalink_box, viewport):
                continue

            try:
                root, box = await self._resolve_permalink_card(
                    permalink,
                    parsed_post[0],
                    parsed_post[1],
                )
                if not self._intersects_viewport(box, viewport):
                    continue
                target = await self._read_card(
                    root,
                    visible_after_scroll=visible_after_scroll,
                    viewport_y=box["y"],
                )
            except _CardNotReady as pending:
                pending_cards.append(pending)
                continue
            if target.url != parsed_post[0] or target.author != parsed_post[1]:
                raise UiReadError("POST_PROVENANCE_UNVERIFIED")
            if scroll_anchor is None:
                scroll_anchor = box
            targets_by_url[target.url] = target
            if len(targets_by_url) >= visible_limit:
                break

        if not targets_by_url and pending_cards:
            raise _CardNotReady.combine(pending_cards)
        targets = list(targets_by_url.values())
        if prefer_new_fingerprints is not None:
            targets.sort(key=lambda target: target.fingerprint in prefer_new_fingerprints)
            targets = targets[:max_posts]
        return tuple(targets), scroll_anchor

    async def read_detail(self, target: PostTarget) -> PostTarget:
        if not target.visible_after_scroll:
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")
        deadline = self._readiness_deadline(_DETAIL_TIMEOUT_MS)
        try:
            remaining = self._remaining_readiness_ms(
                deadline,
                "DETAIL_NAVIGATION_UNVERIFIED",
            )
            await target.permalink.click(timeout=remaining)
            await self._page.wait_for_url(
                target.url,
                timeout=self._remaining_readiness_ms(
                    deadline,
                    "DETAIL_NAVIGATION_UNVERIFIED",
                ),
            )
        except PlaywrightTimeoutError:
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED") from None
        if not _same_home_origin(self._page.url):
            raise UiReadError("OFF_ORIGIN_REDIRECT")

        async def read_detail_window(window_deadline: float) -> PostTarget:
            last_timeout_code = "DETAIL_ROOT_UNVERIFIED"
            while True:
                self._remaining_readiness_ms(
                    window_deadline,
                    last_timeout_code,
                )
                if not _same_home_origin(self._page.url):
                    raise UiReadError("OFF_ORIGIN_REDIRECT")
                if self._page.url != target.url:
                    await self._check_blocker()
                    raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")
                await self._check_blocker()
                try:
                    articles = self._page.get_by_role("article")
                    matching: list[Locator] = []
                    for index in range(await articles.count()):
                        article = articles.nth(index)
                        for link in await article.get_by_role("link").all():
                            href = await link.get_attribute("href")
                            parsed = None if href is None else _post_url(href)
                            if parsed is not None and parsed[0] == target.url:
                                matching.append(article)
                                break
                            if href is not None and "/post/" in href and parsed is None:
                                raise UiReadError("DETAIL_PROVENANCE_UNVERIFIED")
                    if len(matching) > 1:
                        raise UiReadError("DETAIL_ROOT_AMBIGUOUS")
                    root = matching[0] if matching else await self._detail_permalink_card(target)
                    detail = await self._read_card(
                        root,
                        require_body=target.body is not None,
                    )
                    self._remaining_readiness_ms(
                        window_deadline,
                        "DETAIL_ROOT_UNVERIFIED",
                    )
                    return detail
                except _CardNotReady as pending:
                    last_timeout_code = (
                        pending.timeout_code or "DETAIL_ROOT_UNVERIFIED"
                    )
                    await self._wait_for_pending_card(pending, window_deadline)

        detail = await self._recoverable_stage(
            "DETAIL",
            "DETAIL_ROOT_UNVERIFIED",
            read_detail_window,
            recoverable_codes=(
                "DETAIL_ROOT_UNVERIFIED",
                "DETAIL_BODY_UNVERIFIED",
            ),
        )
        if detail.url != target.url or detail.author != target.author:
            raise UiReadError("DETAIL_PROVENANCE_UNVERIFIED")
        if detail.body is None or target.body is None or detail.body != target.body:
            raise UiReadError("DETAIL_BODY_UNVERIFIED")
        return detail

    async def _detail_permalink_card(self, target: PostTarget) -> Locator:
        region = await self._content_region(
            prefer_main=True,
            error_code="DETAIL_REGION_AMBIGUOUS",
        )
        permalinks = region.locator('a[href*="/post/"]')
        scan_limit = min(await permalinks.count(), _MAX_PERMALINK_CANDIDATES)
        for index in range(scan_limit):
            permalink = permalinks.nth(index)
            if not await permalink.is_visible():
                continue
            if await self._is_outside_content_landmark(permalink):
                continue
            href = await permalink.get_attribute("href")
            parsed_post = None if href is None else _post_url(href)
            if parsed_post is None:
                if href is not None and "/post/" in href:
                    raise UiReadError("DETAIL_PROVENANCE_UNVERIFIED")
                continue
            if parsed_post[0] != target.url:
                continue
            root, _ = await self._resolve_permalink_card(
                permalink,
                target.url,
                target.author,
            )
            return root
        path = urlsplit(target.url).path
        matching_permalink = region.locator(f'a[href$="{path}"]')
        raise _CardNotReady(matching_permalink.first, in_viewport=True)

    async def return_to_home(self) -> None:
        await self._go_back_to_home()
        await self.verify_actor()
        await self._wait_for_home_targets(
            1,
            timeout_code="FEED_RETURN_NOT_READY",
        )
    async def _for_you_control(self, *, timeout_ms: int | None = None) -> Locator:
        links = self._page.get_by_role("link", name=_FOR_YOU_NAME)
        tabs = self._page.get_by_role("tab", name=_FOR_YOU_NAME)
        try:
            await expect(links.or_(tabs).first).to_be_visible(
                timeout=timeout_ms or _READINESS_TIMEOUT_MS
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
        links = self._page.get_by_role("link", name=_FOR_YOU_NAME)
        tabs = self._page.get_by_role("tab", name=_FOR_YOU_NAME)
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
        if not _same_home_origin(self._page.url):
            raise UiReadError("OFF_ORIGIN_REDIRECT")
        try:
            parsed = urlsplit(self._page.url)
        except ValueError:
            raise UiReadError("HOME_UI_UNVERIFIED") from None
        if parsed.path not in {"/", "/home", "/for_you"} or parsed.query or parsed.fragment:
            path = parsed.path.casefold()
            if "challenge" in path or "checkpoint" in path:
                raise UiReadError("CHALLENGE_REQUIRED")
            if path in {"/login", "/accounts/login"}:
                raise UiReadError("LOGIN_REQUIRED")
            actor = _profile_author(self._page.url)
            if actor is not None and actor != self._expected_actor:
                raise UiReadError("ACTOR_MISMATCH")
            raise UiReadError(route_error_code)
        await self._check_blocker()
        if self._home_url is None:
            return
        if self._page.url != self._home_url:
            raise UiReadError(route_error_code)
        if self._verified_actor_route is None:
            raise UiReadError("ACTOR_UNVERIFIED")
        verified_actor = _profile_author(self._verified_actor_route)
        if verified_actor != self._expected_actor:
            raise UiReadError("ACTOR_MISMATCH")
        profile_controls = await self._visible_navigation_controls("link")
        profile_controls.extend(await self._visible_navigation_controls("button"))
        if len(profile_controls) > 1:
            raise UiReadError("PROFILE_CONTROL_AMBIGUOUS")
        if not profile_controls:
            profile_controls = await self._visible_navigation_profile_text()
        if len(profile_controls) > 1:
            raise UiReadError("PROFILE_CONTROL_AMBIGUOUS")
        if not profile_controls:
            raise UiReadError("PROFILE_CONTROL_NOT_FOUND")
        profile_href = await profile_controls[0].get_attribute("href")
        if profile_href is not None:
            profile_destination = urljoin(self._page.url, profile_href)
            if not _same_home_origin(profile_destination):
                raise UiReadError("OFF_ORIGIN_REDIRECT")
            profile_actor = _profile_author(profile_destination)
            if profile_actor is not None and profile_actor != self._expected_actor:
                raise UiReadError("ACTOR_MISMATCH")

        for_you_controls = await self._visible_for_you_controls()
        if not for_you_controls:
            raise UiReadError("FOR_YOU_CONTROL_NOT_READY")
        if len(for_you_controls) > 1:
            raise UiReadError("FOR_YOU_CONTROL_AMBIGUOUS")
        active = await self._for_you_is_active(for_you_controls[0])
        if not active and not self._is_for_you_route(self._page.url):
            raise UiReadError("FOR_YOU_STATE_CHANGED")

    async def _wait_for_verified_feed(
        self,
        deadline: float,
        *,
        timeout_code: str,
        route_error_code: str = "HOME_UI_UNVERIFIED",
    ) -> None:
        while True:
            self._remaining_readiness_ms(deadline, timeout_code)
            try:
                await self._require_feed_url(route_error_code=route_error_code)
            except UiReadError as error:
                if error.code != "FOR_YOU_CONTROL_NOT_READY":
                    raise
                await self._wait_for_pending_card(
                    _CardNotReady(
                        self._page.locator("body"),
                        condition="mutation",
                    ),
                    deadline,
                )
            else:
                return

    async def _check_blocker(self) -> None:
        title = normalize_text(await self._page.title())
        headings = self._page.get_by_role("heading")
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
            for marker in ("rate limit", "too many requests", "try again later", "temporarily blocked")
        ):
            raise UiReadError("RATE_LIMITED")
    async def _read_card(
        self,
        article: Locator,
        *,
        visible_after_scroll: bool = False,
        viewport_y: float = 0.0,
        require_body: bool = False,
    ) -> PostTarget:
        if not await article.is_visible():
            raise _CardNotReady(article)
        post_links: list[tuple[str, str, Locator]] = []
        authors: set[str] = set()
        for link in await article.get_by_role("link").all():
            href = await link.get_attribute("href")
            if href is None:
                continue
            parsed_post = _post_url(href)
            if parsed_post is not None:
                post_links.append((parsed_post[0], parsed_post[1], link))
                continue
            if "/post/" in href:
                raise UiReadError("POST_PROVENANCE_UNVERIFIED")
            author = _profile_author(href)
            if author is not None:
                authors.add(author)
        unique_urls = {item[0] for item in post_links}
        unique_permalink_links = {
            (item[0], item[1]) for item in post_links
        }
        if len(unique_urls) > 1 or len(unique_permalink_links) > 1 or len(authors) > 1:
            raise UiReadError("POST_PROVENANCE_UNVERIFIED")
        if not post_links:
            raise _CardNotReady(article, condition="mutation")
        if not authors:
            raise _CardNotReady(
                self._expected_profile_link_signal(article, post_links[0][1])
            )
        if len(unique_urls) != 1 or len(unique_permalink_links) != 1:
            raise UiReadError("POST_PROVENANCE_UNVERIFIED")
        url, permalink_author, link = post_links[0]
        author = next(iter(authors))
        if author != permalink_author:
            raise UiReadError("POST_AUTHOR_UNVERIFIED")

        body: str | None = None
        paragraphs = article.get_by_role("paragraph")
        if await paragraphs.count() == 1:
            paragraph = paragraphs.nth(0)
            if await paragraph.is_visible():
                candidate = (await paragraph.inner_text()).strip()
                more_buttons = article.get_by_role("button", name=_MORE_NAME)
                if (
                    candidate
                    and len(candidate) <= _MAX_BODY_CODEPOINTS
                    and await more_buttons.count() == 0
                ):
                    body = candidate
                elif require_body and not candidate:
                    raise _CardNotReady(
                        paragraph,
                        nonempty_text=True,
                        timeout_code="DETAIL_BODY_UNVERIFIED",
                    )
            elif require_body:
                raise _CardNotReady(
                    paragraph,
                    nonempty_text=True,
                    timeout_code="DETAIL_BODY_UNVERIFIED",
                )
        elif require_body and await paragraphs.count() == 0:
            raise _CardNotReady(
                paragraphs.first,
                nonempty_text=True,
                timeout_code="DETAIL_BODY_UNVERIFIED",
            )
        fingerprint = sha256(f"{url}\n{author}".encode()).hexdigest()
        return PostTarget(
            url,
            author,
            body,
            fingerprint,
            link,
            visible_after_scroll,
            viewport_y,
        )
