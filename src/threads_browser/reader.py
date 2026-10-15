"""Small async Playwright reader for a bounded Home / For You observation."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Literal, TypeVar
from urllib.parse import urljoin, urlsplit

from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    JSHandle,
    Locator,
    Page,
    expect,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from .errors import UiReadError
from .feed.models import DisplayObservation, HomeSample, PostTarget
from .feed.relevance import canonical_terms, classify_body, normalize_text
from .feed.urls import (
    HOME_URL,
    THREADS_ORIGIN,
    _CandidateDiagnosticReason,
    _USERNAME,
    _post_url,
    _post_url_rejection_reason,
    _profile_author,
    _same_home_origin,
)

_NAVIGATION_TIMEOUT_MS = 20_000
_DETAIL_TIMEOUT_MS = 15_000
_READINESS_TIMEOUT_MS = 8_000
_AUTOMATIC_READINESS_WINDOWS = 2
_READINESS_RECHECK_INTERVAL_MS = 200
_RENDER_MIN_DWELL_MS = 600
_RENDER_SETTLE_INTERVAL_MS = 300
_RENDER_STABLE_OBSERVATIONS = 2
_SCROLL_ADVANCE_RATIO = 0.4
_SCROLL_ADVANCE_MIN_PX = 64
_SCROLL_ADVANCE_MAX_PX = 360
_MAX_BODY_CODEPOINTS = 10_000
_MAX_CARD_ANCESTORS = 8
_MAX_CARD_LINKS = 40
_MAX_PERMALINK_CANDIDATES = 40
_MAX_FEED_ROOTS_PER_TYPE = 40
_MAX_STRUCTURE_ELEMENTS = 200
_MAX_VISIBLE_ROW_CANDIDATES = 40
_UNSUPPORTED_CONFIRMATION_SCANS = 3
_FEED_ROOT_SELECTOR = 'article, [role="article"], [data-pressable-container="true"]'
_BODY_TEXT_SELECTOR = 'p, span, div'
_QUOTE_SELECTOR = 'blockquote, [role="quote"]'
_BODY_EXCLUSION_SELECTOR = (
    'a, button, time, [role="button"], [role="link"], [aria-hidden="true"], '
    'nav, aside, footer, form, [role="navigation"], [role="menu"], '
    '[role="complementary"], [role="dialog"], [role="search"], '
    '[role="textbox"], [contenteditable="true"], blockquote, [role="quote"]'
)
_FOR_YOU_NAME = re.compile(r"^for you$", re.IGNORECASE)
_PROFILE_NAME = re.compile(r"^(?:profile|your profile|account)$", re.IGNORECASE)
_MORE_NAME = re.compile(r"^(?:more|see more|read more)$", re.IGNORECASE)
_ReadinessResult = TypeVar("_ReadinessResult")


class _UnverifiedFeedCandidate(UiReadError):
    def __init__(
        self,
        code: str,
        reason: Literal[
            "CANDIDATE_AMBIGUOUS",
            "CANDIDATE_NONCANONICAL",
            "CANDIDATE_AUTHOR_CONFLICT",
        ],
        identities: tuple[str, ...],
        *,
        diagnostic_reason: _CandidateDiagnosticReason | None = None,
    ) -> None:
        self.reason = reason
        self.diagnostic_reason = diagnostic_reason
        self.identities = frozenset(
            sha256(identity.encode("utf-8")).hexdigest()
            for identity in identities
            if identity
        )
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


@dataclass(slots=True)
class _FeedScanCounts:
    visible_links: int = 0
    inspected: int = 0
    article_roots: int = 0
    pressable_roots: int = 0
    visible_times: int = 0
    profile_anchor_candidates: int = 0
    visible_anchors: int = 0
    visible_post_links: int = 0
    body_candidates: int = 0
    paragraph_candidates: int = 0
    url_parsed: int = 0
    author_unverified: int = 0
    wrapper_ambiguous: int = 0
    card_not_ready: int = 0
    verified: int = 0
    body_read: int = 0
    body_unknown: int = 0
    observed_rows: int = 0
    body_extracted: int = 0
    skipped_rows: int = 0
    uncertain_rows: int = 0
    coverage_complete: bool = True
    skipped: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    structural_reasons: dict[str, int] = field(default_factory=dict)
    unsupported_roots: int = 0
    region_status: str = "UNSET"
    scroll_surface_status: str = "NOT_CHECKED"


@dataclass(frozen=True, slots=True)
class _CandidateStructure:
    visible_anchors: int = 0
    visible_post_links: int = 0
    canonical_post_links: int = 0
    profile_anchor_candidates: int = 0
    visible_times: int = 0
    body_candidates: int = 0
    paragraph_candidates: int = 0


@dataclass(frozen=True, slots=True)
class _RenderObservation[T]:
    signature: tuple[object, ...] = field(repr=False)
    value: T
    terminal_code: str | None = None
    ready_state: str | None = "READY"


@dataclass(frozen=True, slots=True)
class _FeedTargetsObservation:
    targets: tuple[PostTarget, ...]
    scroll_anchor: dict[str, float] | None
    duplicate_delta: int = 0
    display_observations: tuple[DisplayObservation, ...] = ()
    observed_rows: int = 0
    coverage_complete: bool = True
    render_signature: tuple[object, ...] = field(default=(), repr=False)
    candidate_roots: tuple[Locator, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class _VisibleTargets:
    targets: tuple[PostTarget, ...]
    scroll_anchor: dict[str, float] | None
    display_observations: tuple[DisplayObservation, ...]
    observed_rows: int
    coverage_complete: bool
    signature: tuple[object, ...] = field(repr=False)
    candidate_roots: tuple[Locator, ...] = field(default=(), repr=False)
    pending_roots: tuple[Locator, ...] = field(default=(), repr=False)


class ThreadsHomeReader:
    """Reads verified visible Home / For You posts and at most one exact post."""

    def __init__(
        self,
        page: Page,
        expected_actor: str | None,
        *,
        readiness_prompt: Callable[[str, int], Awaitable[bool]] | None = None,
    ) -> None:
        if expected_actor is not None and _USERNAME.fullmatch(expected_actor) is None:
            raise UiReadError("ACTOR_INVALID")
        self._page = page
        self._expected_actor = None if expected_actor is None else expected_actor.casefold()
        self._readiness_prompt = readiness_prompt
        self._home_url: str | None = None
        self._verified_actor_route: str | None = None
        self._last_ui_transition_at: float | None = None
        self.scroll_attempts = 0
        self.scrolls = 0
        self.feed_duplicates = 0
        self.last_scan_counts = _FeedScanCounts()
        self.last_display_observations: tuple[DisplayObservation, ...] = ()
        self.last_viewport_coverage_complete = True
        self.last_viewport_observed_rows = 0
        self._last_viewport_limit = 20
        self.last_scroll_delta = 0.0
        self._last_viewport_signature: tuple[object, ...] | None = None
        self._last_viewport_candidate_roots: tuple[Locator, ...] = ()
        self._carried_viewport_candidate_roots: tuple[Locator, ...] = ()
        self._reported_render_rescans: set[tuple[str, float | None]] = set()
        self._skipped_candidate_identities: set[str] = set()
        self._reported_candidate_skips: dict[tuple[str, str], set[str]] = {}

    def _mark_ui_transition(self) -> None:
        self._last_ui_transition_at = time.monotonic()

    @property
    def skipped_unverified(self) -> int:
        return len(self._skipped_candidate_identities)

    def _record_skipped_candidate(
        self,
        stage: str,
        error: _UnverifiedFeedCandidate,
    ) -> None:
        self._skipped_candidate_identities.update(error.identities)
        key = (stage, error.diagnostic_reason or error.reason)
        identities = self._reported_candidate_skips.setdefault(key, set())
        previous_count = len(identities)
        identities.update(error.identities)
        if len(identities) != previous_count:
            cause = (
                ""
                if error.diagnostic_reason is None
                else f" cause={error.diagnostic_reason}"
            )
            print(
                f"SKIPPED stage={stage} reason={error.reason} "
                f"count={len(identities)}{cause}"
            )

    def _record_scan_skip(
        self,
        stage: str,
        error: _UnverifiedFeedCandidate,
    ) -> None:
        counts = self.last_scan_counts
        counts.skipped += 1
        diagnostic_reason = error.diagnostic_reason
        if diagnostic_reason is not None:
            counts.rejection_reasons[diagnostic_reason] = (
                counts.rejection_reasons.get(diagnostic_reason, 0) + 1
            )
        if diagnostic_reason == "CANDIDATE_WRAPPER_AMBIGUOUS":
            counts.wrapper_ambiguous += 1
        elif diagnostic_reason == "CANDIDATE_AUTHOR_MISMATCH":
            counts.author_unverified += 1
        elif diagnostic_reason is None and error.reason == "CANDIDATE_AMBIGUOUS":
            counts.wrapper_ambiguous += 1
        elif diagnostic_reason is None and error.reason == "CANDIDATE_AUTHOR_CONFLICT":
            counts.author_unverified += 1
        self._record_skipped_candidate(stage, error)

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

        self._mark_ui_transition()
        await self._wait_for_home_render(
            "HOME_NAVIGATION",
            timeout_code="HOME_NAVIGATION_TIMEOUT",
            require_active_for_you=False,
            require_feed=False,
        )

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
            self._mark_ui_transition()

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
        await self._wait_for_home_render(
            "FOR_YOU_ACTIVATION",
            timeout_code="FOR_YOU_ACTIVATION_NOT_READY",
            require_active_for_you=True,
            require_feed=False,
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

    async def verify_login_actor(self) -> str:
        """Verify the signed-in actor through Threads navigation and return Home."""
        if _same_home_origin(self._page.url):
            try:
                path = urlsplit(self._page.url).path.casefold()
            except ValueError:
                raise UiReadError("PROFILE_ROUTE_UNVERIFIED") from None
            if "challenge" in path or "checkpoint" in path:
                raise UiReadError("CHALLENGE_REQUIRED")
            if path in {"/login", "/accounts/login"}:
                raise UiReadError("LOGIN_REQUIRED")
            if any(marker in path for marker in ("rate_limit", "too_many_requests")):
                raise UiReadError("RATE_LIMITED")
        elif self._page.url != "about:blank":
            raise UiReadError("LOGIN_LOADING")

        if not self._is_home_navigation_url(self._page.url):
            try:
                await self._page.goto(
                    HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=_NAVIGATION_TIMEOUT_MS,
                )
            except PlaywrightTimeoutError:
                raise UiReadError("HOME_NAVIGATION_TIMEOUT") from None

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
        if any(marker in path for marker in ("rate_limit", "too_many_requests")):
            raise UiReadError("RATE_LIMITED")
        await self._check_blocker()
        if not self._is_home_navigation_url(self._page.url):
            raise UiReadError("PROFILE_ROUTE_UNVERIFIED")

        controls = await self._visible_profile_controls()
        if not controls:
            body = self._page.locator("body")
            if (
                await body.get_attribute("aria-busy") == "true"
                or not normalize_text(await body.inner_text())
            ):
                raise UiReadError("LOGIN_LOADING")
            raise UiReadError("LOGIN_UI_UNSUPPORTED")
        actor = await self._follow_profile_control(controls[0], return_home=False)
        await self._return_to_threads_home()
        return actor

    async def wait_for_login_control(self) -> None:
        """Wait indefinitely for Threads navigation, surviving document replacement."""
        predicate = """() => {
                    if (location.origin !== "https://www.threads.com") return false;
                    const labels = new Set(["profile", "your profile", "account"]);
                    const navs = document.querySelectorAll('nav, [role="navigation"]');
                    for (const nav of navs) {
                        const navStyle = getComputedStyle(nav);
                        const navBox = nav.getBoundingClientRect();
                        if (
                            navStyle.display === "none"
                            || navStyle.visibility === "hidden"
                            || navBox.width <= 0
                            || navBox.height <= 0
                        ) continue;
                        for (const control of nav.querySelectorAll(
                            'a, button, [role="link"], [role="button"]'
                        )) {
                            const label = (
                                control.getAttribute("aria-label")
                                || control.innerText
                                || control.textContent
                                || ""
                            ).trim().toLocaleLowerCase();
                            const style = getComputedStyle(control);
                            const box = control.getBoundingClientRect();
                            if (
                                labels.has(label)
                                && style.display !== "none"
                                && style.visibility !== "hidden"
                                && box.width > 0
                                && box.height > 0
                            ) return true;
                        }
                    }
                    return false;
                }"""
        while True:
            if self._page.is_closed():
                raise UiReadError("BROWSER_SESSION_UNAVAILABLE")
            try:
                await self._page.wait_for_function(
                    predicate,
                    polling=1000,
                    timeout=0,
                )
                return
            except PlaywrightTimeoutError:
                if self._page.is_closed():
                    raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
                try:
                    await self._page.wait_for_load_state(
                        "domcontentloaded",
                        timeout=_NAVIGATION_TIMEOUT_MS,
                    )
                except PlaywrightTimeoutError:
                    continue
                await asyncio.sleep(0.25)
            except PlaywrightError as error:
                if self._page.is_closed():
                    raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
                message = str(error).casefold()
                if not any(
                    marker in message
                    for marker in (
                        "execution context was destroyed",
                        "cannot find context with specified id",
                        "frame was detached",
                        "navigating frame was detached",
                    )
                ):
                    raise UiReadError("LOGIN_UI_UNSUPPORTED") from None
                try:
                    await self._page.wait_for_load_state(
                        "domcontentloaded",
                        timeout=_NAVIGATION_TIMEOUT_MS,
                    )
                except PlaywrightTimeoutError:
                    continue
                await asyncio.sleep(0.25)

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

    async def _follow_profile_control(
        self,
        control: Locator,
        *,
        return_home: bool = True,
    ) -> str:
        href = await control.get_attribute("href")
        destination = None if href is None else urljoin(self._page.url, href)
        if destination is not None and not _same_home_origin(destination):
            raise UiReadError("OFF_ORIGIN_REDIRECT")
        current_url = self._page.url
        try:
            await control.click(timeout=_READINESS_TIMEOUT_MS)
        except PlaywrightTimeoutError:
            pass
        self._mark_ui_transition()

        async def verify_profile_navigation(deadline: float) -> str:
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
            actor = await self._verify_current_profile_route()
            await self._await_render_settled(
                "PROFILE_VERIFY",
                deadline,
                timeout_code="PROFILE_CLICK_NO_NAVIGATION",
                observe=self._observe_profile_render,
            )
            return actor

        actor = await self._recoverable_stage(
            "PROFILE_VERIFY",
            "PROFILE_CLICK_NO_NAVIGATION",
            verify_profile_navigation,
            recoverable_codes=("PROFILE_CLICK_NO_NAVIGATION",),
        )
        if return_home:
            await self._go_back_to_home()
        return actor

    async def _verify_current_profile_route(self) -> str:
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
        if any(marker in path for marker in ("rate_limit", "too_many_requests")):
            raise UiReadError("RATE_LIMITED")
        actor = _profile_author(self._page.url)
        if actor is None:
            raise UiReadError("PROFILE_ROUTE_UNVERIFIED")
        if self._expected_actor is not None and actor != self._expected_actor:
            raise UiReadError("ACTOR_MISMATCH")
        self._expected_actor = actor
        self._verified_actor_route = self._page.url
        return actor

    async def _go_back_to_home(self) -> None:
        previous_url = self._page.url
        try:
            await self._page.go_back(
                wait_until="domcontentloaded",
                timeout=_NAVIGATION_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            pass
        self._mark_ui_transition()

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
            await self._await_render_settled(
                "HOME_RETURN",
                deadline,
                timeout_code="RETURN_UNVERIFIED",
                observe=lambda: self._observe_home_render(
                    deadline,
                    timeout_code="RETURN_UNVERIFIED",
                    require_active_for_you=True,
                    require_feed=False,
                ),
            )

        await self._recoverable_stage(
            "HOME_RETURN",
            "RETURN_UNVERIFIED",
            verify_home_return,
            recoverable_codes=("RETURN_UNVERIFIED",),
        )

    async def _return_to_threads_home(self) -> None:
        for attempt in range(2):
            try:
                await self._page.goto(
                    HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=_NAVIGATION_TIMEOUT_MS,
                )
                break
            except PlaywrightTimeoutError:
                if self._is_home_navigation_url(self._page.url):
                    break
                if attempt == 1:
                    raise UiReadError("HOME_NAVIGATION_TIMEOUT") from None
        if not _same_home_origin(self._page.url):
            raise UiReadError("OFF_ORIGIN_REDIRECT")
        if not self._is_home_navigation_url(self._page.url):
            raise UiReadError("HOME_NAVIGATION_TIMEOUT")
        await self._check_blocker()

    async def scan_viewport(
        self,
        max_posts: int,
        *,
        scanned_fingerprints: set[str] | None = None,
    ) -> tuple[PostTarget, ...]:
        if not 1 <= max_posts <= 20:
            raise UiReadError("POST_LIMIT_INVALID")
        self._last_viewport_limit = max_posts
        initial_viewport = self.scrolls == 0
        targets, _ = await self._wait_for_home_targets(
            max_posts,
            timeout_code=(
                "FEED_INITIAL_NOT_READY"
                if initial_viewport
                else "FEED_SCROLL_NOT_READY"
            ),
            currently_visible=True,
            prefer_new_fingerprints=scanned_fingerprints,
            stage="FEED_INITIAL" if initial_viewport else "FEED_SCROLL",
        )
        return targets

    async def advance_feed(self) -> bool:
        """Issue one read-only wheel event and require observed scroll displacement."""
        if not self.last_viewport_coverage_complete:
            raise UiReadError("VIEWPORT_COVERAGE_INCOMPLETE")
        await self._wait_for_verified_feed(
            self._readiness_deadline(_READINESS_TIMEOUT_MS),
            timeout_code="HOME_SCROLL_SURFACE_UNVERIFIED",
        )
        surface = await self._scroll_surface()
        witness: JSHandle | None = None
        try:
            try:
                point, wheel_delta, viewport_height = (
                    await self._safe_feed_scroll_point(surface)
                )
                initial_position = await surface.evaluate("element => element.scrollTop")
                witness = await self._watch_scroll_completion(
                    surface,
                    initial_position,
                )
                await self._page.mouse.move(*point)
                self.scroll_attempts += 1
                await self._page.mouse.wheel(0, wheel_delta)
                self._mark_ui_transition()
            except PlaywrightError:
                print(f"SCROLL attempt={self.scroll_attempts} observed=0")
                raise UiReadError("HOME_SCROLL_FAILED") from None

            async def observe_scroll(window_deadline: float) -> float:
                if witness is None:
                    raise UiReadError("HOME_SCROLL_UNVERIFIED")
                observed_delta = await self._wait_for_scroll_movement(
                    witness,
                    window_deadline,
                )
                if observed_delta is None:
                    raise UiReadError("HOME_SCROLL_UNVERIFIED")
                await self._wait_for_verified_feed(
                    window_deadline,
                    timeout_code="HOME_SCROLL_UNVERIFIED",
                )
                if observed_delta > viewport_height * 0.5 + 1:
                    raise UiReadError("HOME_SCROLL_ADVANCE_OVERSIZED")

                previous_signature = self._last_viewport_signature

                async def observe_new_viewport():
                    observation = await self._observe_feed_targets(
                        20,
                        window_deadline,
                        timeout_code="HOME_SCROLL_UNVERIFIED",
                        currently_visible=True,
                        prefer_new_fingerprints=None,
                        stage="FEED_SCROLL",
                        report_rescan=True,
                    )
                    if (
                        observation is None
                        or not self._has_new_viewport_content(
                            previous_signature,
                            observation.signature,
                            observed_delta,
                        )
                    ):
                        return None
                    return observation

                rendered = await self._await_render_settled(
                    "FEED_SCROLL",
                    window_deadline,
                    timeout_code="HOME_SCROLL_UNVERIFIED",
                    observe=observe_new_viewport,
                    validate=lambda: self._wait_for_verified_feed(
                        window_deadline,
                        timeout_code="HOME_SCROLL_UNVERIFIED",
                    ),
                )
                self._last_viewport_signature = rendered.render_signature
                self.last_display_observations = rendered.display_observations
                self.last_viewport_coverage_complete = rendered.coverage_complete
                self.last_viewport_observed_rows = rendered.observed_rows
                self._last_viewport_candidate_roots = rendered.candidate_roots
                return observed_delta

            try:
                observed_delta = await self._recoverable_stage(
                    "HOME_SCROLL",
                    "HOME_SCROLL_UNVERIFIED",
                    observe_scroll,
                )
            except UiReadError:
                print(f"SCROLL attempt={self.scroll_attempts} observed=0")
                raise
            self.scrolls += 1
            self.last_scroll_delta = observed_delta
            delta_ratio = observed_delta / viewport_height
            delta_band = (
                "SMALL"
                if delta_ratio <= 0.5
                else "MEDIUM"
                if delta_ratio <= 0.75
                else "LARGE"
            )
            print(
                f"SCROLL attempt={self.scroll_attempts} observed=1 "
                f"delta_band={delta_band}"
            )
            return True
        finally:
            if witness is not None:
                try:
                    await witness.evaluate("state => state.cancel()")
                except PlaywrightError:
                    pass
                await witness.dispose()
            await surface.dispose()

    async def sample_and_scroll(self, max_posts: int) -> HomeSample:
        """Compatibility helper for callers that need a before/after sample."""
        self.scroll_attempts = 0
        self.scrolls = 0
        self.feed_duplicates = 0
        before = await self.scan_viewport(max_posts)
        before_by_fingerprint = {target.fingerprint: target for target in before}
        self.feed_duplicates += len(before) - len(before_by_fingerprint)
        await self.advance_feed()
        after = await self.scan_viewport(
            max_posts,
            scanned_fingerprints=set(before_by_fingerprint),
        )
        after_by_fingerprint = {target.fingerprint: target for target in after}
        self.feed_duplicates += len(after) - len(after_by_fingerprint)
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
            skipped_unverified=self.skipped_unverified,
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

    async def _wait_for_home_render(
        self,
        stage: str,
        *,
        timeout_code: str,
        require_active_for_you: bool,
        require_feed: bool,
    ) -> None:
        async def observe_stage(deadline: float) -> None:
            await self._await_render_settled(
                stage,
                deadline,
                timeout_code=timeout_code,
                observe=lambda: self._observe_home_render(
                    deadline,
                    timeout_code=timeout_code,
                    require_active_for_you=require_active_for_you,
                    require_feed=require_feed,
                ),
            )

        await self._recoverable_stage(
            stage,
            timeout_code,
            observe_stage,
            recoverable_codes=(timeout_code,),
        )

    async def _observe_home_render(
        self,
        deadline: float,
        *,
        timeout_code: str,
        require_active_for_you: bool,
        require_feed: bool,
    ) -> _RenderObservation[None] | None:
        self._remaining_readiness_ms(deadline, timeout_code)
        try:
            await self._require_feed_url(route_error_code=timeout_code)
        except UiReadError as error:
            if error.code in {
                "PROFILE_CONTROL_NOT_READY",
                "FOR_YOU_CONTROL_NOT_READY",
                "FOR_YOU_STATE_CHANGED",
                timeout_code,
            }:
                return None
            raise

        profiles = await self._visible_profile_controls()
        for_you_controls = await self._visible_for_you_controls()
        if len(profiles) > 1:
            raise UiReadError("PROFILE_CONTROL_AMBIGUOUS")
        if len(for_you_controls) > 1:
            raise UiReadError("FOR_YOU_CONTROL_AMBIGUOUS")
        if not for_you_controls:
            return None

        profile_href = (
            await profiles[0].get_attribute("href") if profiles else None
        )
        if profile_href is not None:
            profile_destination = urljoin(self._page.url, profile_href)
            if not _same_home_origin(profile_destination):
                raise UiReadError("OFF_ORIGIN_REDIRECT")
            profile_actor = _profile_author(profile_destination)
            if profile_actor is not None and profile_actor != self._expected_actor:
                raise UiReadError("ACTOR_MISMATCH")
        for_you_href = await for_you_controls[0].get_attribute("href")
        if for_you_href is not None and not _same_home_origin(
            urljoin(self._page.url, for_you_href)
        ):
            raise UiReadError("OFF_ORIGIN_REDIRECT")
        active = await self._for_you_is_active(for_you_controls[0])
        on_for_you_route = self._is_for_you_route(self._page.url)
        if require_active_for_you and not (active or on_for_you_route):
            return None
        feed_ready = await self._visible_feed_evidence() if require_feed else False
        if require_feed and not feed_ready:
            return None

        dom_state = await self._page.evaluate(
            """rootSelector => {
                const body = document.body;
                const visibleCount = selector => Array.from(
                    document.querySelectorAll(selector)
                ).filter(element => {
                    const rect = element.getBoundingClientRect();
                    const style = getComputedStyle(element);
                    return rect.width > 0 && rect.height > 0 &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                }).length;
                return [
                    body ? (body.innerText || '').trim().length : 0,
                    body ? body.childElementCount : 0,
                    visibleCount(rootSelector),
                    visibleCount('a[href*="/post/"]'),
                ];
            }""",
            _FEED_ROOT_SELECTOR,
        )
        if not dom_state[0]:
            return None
        signature = (
            self._page.url,
            profile_href,
            for_you_href,
            active,
            on_for_you_route,
            feed_ready,
            *dom_state,
        )
        return _RenderObservation(
            signature,
            None,
            ready_state="FEED_EVIDENCE" if require_feed else "UI_SHELL_SETTLED",
        )

    async def _observe_profile_render(
        self,
    ) -> _RenderObservation[None] | None:
        await self._verify_current_profile_route()
        dom_state = await self._page.evaluate(
            """() => {
                const body = document.body;
                const visibleCount = selector => Array.from(
                    document.querySelectorAll(selector)
                ).filter(element => {
                    const rect = element.getBoundingClientRect();
                    const style = getComputedStyle(element);
                    return rect.width > 0 && rect.height > 0 &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                }).length;
                return [
                    body ? (body.innerText || '').trim().length : 0,
                    body ? body.childElementCount : 0,
                    visibleCount('main, [role="main"], h1, [role="tab"]'),
                ];
            }"""
        )
        if not dom_state[0]:
            return None
        return _RenderObservation((self._page.url, *dom_state), None)

    async def _await_animation_frames(
        self,
        deadline: float,
        timeout_code: str,
    ) -> None:
        timeout_seconds = self._remaining_readiness_ms(deadline, timeout_code) / 1000
        try:
            await asyncio.wait_for(
                self._page.evaluate(
                    """() => new Promise(resolve =>
                        requestAnimationFrame(() => requestAnimationFrame(resolve))
                    )"""
                ),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            raise UiReadError(timeout_code) from None

    async def _await_render_settled(
        self,
        stage: str,
        deadline: float,
        *,
        timeout_code: str,
        observe: Callable[
            [], Awaitable[_RenderObservation[_ReadinessResult] | None]
        ],
        validate: Callable[[], Awaitable[None]] | None = None,
    ) -> _ReadinessResult:
        transition_at = self._last_ui_transition_at or time.monotonic()
        dwell_until = transition_at + _RENDER_MIN_DWELL_MS / 1000
        dwell_remaining = min(
            max(0.0, dwell_until - time.monotonic()),
            self._remaining_readiness_ms(deadline, timeout_code) / 1000,
        )
        if dwell_remaining:
            await asyncio.sleep(dwell_remaining)

        checks = 0
        stable_observations = 0
        previous_signature: tuple[object, ...] | None = None
        waiting_reported = False
        while True:
            self._remaining_readiness_ms(deadline, timeout_code)
            await self._await_animation_frames(deadline, timeout_code)
            observation = await observe()
            checks += 1
            if observation is None:
                if previous_signature is not None:
                    print(f"RESCAN stage={stage} reason=DOM_CHANGED_AFTER_SCAN")
                previous_signature = None
                stable_observations = 0
                if not waiting_reported:
                    print(f"WAITING stage={stage} reason=RENDER_PENDING")
                    waiting_reported = True
            else:
                signature = observation.signature
                if signature == previous_signature:
                    stable_observations += 1
                else:
                    if previous_signature is not None:
                        print(f"RESCAN stage={stage} reason=DOM_CHANGED_AFTER_SCAN")
                    previous_signature = signature
                    stable_observations = 1
                if stable_observations >= _RENDER_STABLE_OBSERVATIONS:
                    if observation.terminal_code is not None:
                        raise UiReadError(observation.terminal_code)
                    if observation.ready_state is None:
                        if not waiting_reported:
                            print(
                                f"WAITING stage={stage} "
                                "reason=READABLE_FEED_NOT_CONFIRMED"
                            )
                            waiting_reported = True
                    else:
                        if validate is not None:
                            await validate()
                        else:
                            self._remaining_readiness_ms(deadline, timeout_code)
                    if observation.ready_state == "FEED_VISIBLE_UNVERIFIED":
                        print(
                            f"OBSERVED stage={stage} "
                            "state=FEED_VISIBLE_UNVERIFIED verified=0 "
                            "identity=OBSERVED_UNVERIFIED "
                            f"observed_rows={getattr(observation.value, 'observed_rows', 0)} "
                            f"body_extracted={getattr(self.last_scan_counts, 'body_extracted', 0)} "
                            "viewport_coverage_complete="
                            f"{str(getattr(observation.value, 'coverage_complete', False)).lower()} "
                            f"render_checks={checks}"
                        )
                    elif observation.ready_state == "OBSERVATION_UNCERTAIN":
                        print(
                            f"OBSERVED stage={stage} state=OBSERVATION_UNCERTAIN "
                            f"verified={self.last_scan_counts.verified} "
                            f"observed_rows={self.last_scan_counts.observed_rows} "
                            f"uncertain_rows={self.last_scan_counts.uncertain_rows} "
                            "viewport_coverage_complete="
                            f"{str(getattr(observation.value, 'coverage_complete', False)).lower()} "
                            f"render_checks={checks}"
                        )
                    elif observation.ready_state == "DISPLAY_OBSERVATION":
                        print(
                            f"OBSERVED stage={stage} state=DISPLAY_OBSERVATION "
                            f"verified={self.last_scan_counts.verified} "
                            f"observed_rows={self.last_scan_counts.observed_rows} "
                            f"body_extracted={self.last_scan_counts.body_extracted} "
                            "viewport_coverage_complete="
                            f"{str(getattr(observation.value, 'coverage_complete', False)).lower()} "
                            f"render_checks={checks}"
                        )
                    elif observation.ready_state == "BODY_EXTRACTED":
                        print(
                            f"READY stage={stage} state=BODY_EXTRACTED "
                            f"verified={self.last_scan_counts.verified} "
                            f"observed_rows={self.last_scan_counts.observed_rows} "
                            f"body_extracted={self.last_scan_counts.body_extracted} "
                            "viewport_coverage_complete="
                            f"{str(getattr(observation.value, 'coverage_complete', False)).lower()} "
                            f"render_checks={checks}"
                        )
                    elif observation.ready_state == "UI_SHELL_SETTLED":
                        print(
                            f"READY stage={stage} state=UI_SHELL_SETTLED "
                            f"render_checks={checks}"
                        )
                    elif observation.ready_state == "FEED_EVIDENCE":
                        print(
                            f"OBSERVED stage={stage} state=FEED_EVIDENCE "
                            f"render_checks={checks}"
                        )
                    elif observation.ready_state == "DETAIL_VERIFIED":
                        print(
                            f"READY stage={stage} state=DETAIL_VERIFIED "
                            f"render_checks={checks}"
                        )
                    else:
                        print(f"READY stage={stage} render_checks={checks}")
                    if observation.ready_state is not None:
                        return observation.value

            remaining = self._remaining_readiness_ms(deadline, timeout_code)
            interval_ms = min(max(1, _RENDER_SETTLE_INTERVAL_MS), remaining)
            await asyncio.sleep(interval_ms / 1000)

    async def _observe_feed_targets(
        self,
        max_posts: int,
        deadline: float,
        *,
        timeout_code: str,
        currently_visible: bool,
        prefer_new_fingerprints: set[str] | None,
        stage: str,
        report_rescan: bool,
    ) -> _RenderObservation[_FeedTargetsObservation] | None:
        await self._wait_for_verified_feed(
            deadline,
            timeout_code=timeout_code,
        )
        duplicate_count_before = self.feed_duplicates
        try:
            scan = await self._visible_targets(
                max_posts,
                currently_visible=currently_visible,
                prefer_new_fingerprints=prefer_new_fingerprints,
                stage=stage,
            )
        except _CardNotReady as pending:
            self.feed_duplicates = duplicate_count_before
            await self._wait_for_pending_card(pending, deadline)
            return None
        duplicate_delta = self.feed_duplicates - duplicate_count_before
        self.feed_duplicates = duplicate_count_before
        if scan.pending_roots:
            pending = _CardNotReady(
                signals=tuple(
                    _CardWaitSignal(root, "mutation") for root in scan.pending_roots
                )
            )
            if await self._wait_for_pending_card(pending, deadline):
                return None
        if not scan.observed_rows:
            pending = await self._feed_readiness_signals()
            if pending is not None:
                await self._wait_for_pending_card(pending, deadline)
                return None
            if not await self._visible_feed_evidence():
                return None
        self._remaining_readiness_ms(deadline, timeout_code)
        counts = self.last_scan_counts
        ready_state = (
            "OBSERVATION_UNCERTAIN"
            if not scan.coverage_complete and scan.observed_rows
            else "BODY_EXTRACTED"
            if counts.body_read and scan.coverage_complete
            else "FEED_VISIBLE_UNVERIFIED"
            if counts.body_extracted and not counts.verified and scan.coverage_complete
            else "DISPLAY_OBSERVATION"
            if counts.body_extracted and scan.coverage_complete
            else "OBSERVATION_UNCERTAIN"
            if scan.observed_rows
            else None
        )
        observation_value = _FeedTargetsObservation(
            scan.targets,
            scan.scroll_anchor,
            duplicate_delta,
            scan.display_observations,
            scan.observed_rows,
            scan.coverage_complete,
            scan.signature,
            scan.candidate_roots,
        )
        return _RenderObservation(
            scan.signature,
            observation_value,
            ready_state=ready_state,
        )

    async def _wait_for_home_targets(
        self,
        max_posts: int,
        *,
        timeout_code: str,
        currently_visible: bool = False,
        prefer_new_fingerprints: set[str] | None = None,
        stage: str | None = None,
    ) -> tuple[tuple[PostTarget, ...], dict[str, float]]:
        stage_name = stage or {
            "FEED_INITIAL_NOT_READY": "FEED_INITIAL",
            "FEED_RETURN_NOT_READY": "FEED_RETURN",
            "HOME_SCROLL_UNVERIFIED": "FEED_SCROLL",
        }.get(timeout_code, "FEED")

        async def attempt(
            window_deadline: float,
        ) -> tuple[tuple[PostTarget, ...], dict[str, float]]:
            self.last_scan_counts = _FeedScanCounts()
            unsupported_scans = 0

            async def observe() -> _RenderObservation[
                _FeedTargetsObservation
            ] | None:
                nonlocal unsupported_scans
                observation = await self._observe_feed_targets(
                    max_posts,
                    window_deadline,
                    timeout_code=timeout_code,
                    currently_visible=currently_visible,
                    prefer_new_fingerprints=prefer_new_fingerprints,
                    stage=stage_name,
                    report_rescan=True,
                )
                if observation is None:
                    unsupported_scans = 0
                    return None
                if (
                    self.last_scan_counts.body_extracted
                    or self.last_scan_counts.body_read
                ):
                    unsupported_scans = 0
                    return observation
                if (
                    self.last_scan_counts.unsupported_roots
                    or observation.value.observed_rows
                    or self.last_scan_counts.skipped
                ):
                    unsupported_scans += 1
                    if unsupported_scans < _UNSUPPORTED_CONFIRMATION_SCANS:
                        return None
                    return _RenderObservation(
                        observation.signature,
                        observation.value,
                        ready_state="OBSERVATION_UNCERTAIN",
                    )
                unsupported_scans = 0
                return None

            result = await self._await_render_settled(
                stage_name,
                window_deadline,
                timeout_code=timeout_code,
                observe=observe,
                validate=lambda: self._wait_for_verified_feed(
                    window_deadline,
                    timeout_code=timeout_code,
                ),
            )
            self.feed_duplicates += result.duplicate_delta
            self.last_display_observations = result.display_observations
            self.last_viewport_coverage_complete = result.coverage_complete
            self.last_viewport_observed_rows = result.observed_rows
            self.last_scan_counts.coverage_complete = result.coverage_complete
            self._last_viewport_signature = result.render_signature
            self._last_viewport_candidate_roots = result.candidate_roots
            return result.targets, result.scroll_anchor or {}

        try:
            return await self._recoverable_stage(
                stage_name,
                timeout_code,
                attempt,
            )
        except UiReadError as error:
            if error.code in {timeout_code, "FEED_VISIBLE_UNSUPPORTED"}:
                print(f"STOP stage={stage_name} code={error.code}")
            raise

    async def _feed_readiness_signals(self) -> _CardNotReady | None:
        signals: list[_CardWaitSignal] = []
        viewport = self._page.viewport_size
        if viewport is None:
            return None
        region = await self._content_region()
        roots, _ = await self._top_level_candidate_roots(region, viewport)
        for root in roots:
            structure = await self._inspect_candidate_root(root, viewport)
            if not structure.body_candidates:
                signals.append(_CardWaitSignal(root, "mutation"))

        permalinks = region.locator('a[href*="/post/"]')
        for index in range(min(await permalinks.count(), _MAX_PERMALINK_CANDIDATES)):
            permalink = permalinks.nth(index)
            if await self._is_outside_content_landmark(permalink):
                continue
            if await self._is_nested_feed_content(permalink):
                continue
            box = await permalink.bounding_box()
            if viewport is None or box is None or not self._intersects_viewport(box, viewport):
                signals.append(_CardWaitSignal(permalink, "in_viewport"))

        if not signals:
            return None
        signals.append(_CardWaitSignal(self._page.locator("body"), "mutation"))
        return _CardNotReady(signals=tuple(signals))

    async def _wait_for_pending_card(
        self,
        pending: _CardNotReady,
        deadline: float,
    ) -> bool:
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            return False
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
        completed, unfinished = await asyncio.wait(
            tasks,
            timeout=wait_ms / 1000,
            return_when=asyncio.FIRST_COMPLETED,
        )
        changed = any(task.result() for task in completed)
        for task in unfinished:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return changed

    async def _visible_feed_evidence(self) -> bool:
        viewport = self._page.viewport_size
        if viewport is None:
            return False
        region = await self._content_region()
        roots, _ = await self._top_level_candidate_roots(region, viewport)
        for root in roots:
            structure = await self._inspect_candidate_root(root, viewport)
            if structure.visible_times and (
                structure.profile_anchor_candidates
                or structure.visible_post_links
            ):
                return True
        permalinks = region.locator('a[href*="/post/"]')
        for index in range(min(await permalinks.count(), _MAX_PERMALINK_CANDIDATES)):
            permalink = permalinks.nth(index)
            if await self._is_outside_content_landmark(permalink):
                continue
            if await self._is_nested_feed_content(permalink):
                continue
            if not await self._is_in_viewport(permalink, viewport):
                continue
            href = await permalink.get_attribute("href")
            if href is None:
                continue
            if _post_url(href) is not None:
                return True
            root = permalink.locator(
                "xpath=ancestor::*[self::article or @role='article' "
                "or @data-pressable-container='true'][1]"
            )
            if await root.count() == 0 or await self._is_nested_feed_root(root):
                continue
            structure = await self._inspect_candidate_root(root, viewport)
            if (
                structure.visible_times
                and structure.profile_anchor_candidates
                and structure.body_candidates
            ):
                return True
        return False

    async def _safe_feed_scroll_point(
        self,
        surface: JSHandle,
    ) -> tuple[tuple[float, float], int, float]:
        viewport = self._page.viewport_size
        if viewport is None:
            raise UiReadError("FEED_VIEWPORT_UNVERIFIED")
        box = await surface.evaluate(
            """element => {
                const rect = element.getBoundingClientRect();
                return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
            }"""
        )
        if not box or box["width"] <= 0 or box["height"] <= 0:
            raise UiReadError("HOME_SCROLL_SURFACE_UNVERIFIED")
        left = max(0.0, box["x"])
        right = min(float(viewport["width"]), box["x"] + box["width"])
        top = max(0.0, box["y"])
        bottom = min(float(viewport["height"]), box["y"] + box["height"])
        if left >= right or top >= bottom:
            raise UiReadError("HOME_SCROLL_SURFACE_UNVERIFIED")
        visible_height = bottom - top
        if visible_height < 2:
            raise UiReadError("HOME_SCROLL_SURFACE_UNVERIFIED")
        advance = min(
            _SCROLL_ADVANCE_MAX_PX,
            max(_SCROLL_ADVANCE_MIN_PX, round(visible_height * _SCROLL_ADVANCE_RATIO)),
            max(1, int(visible_height * 0.5)),
        )
        xs = (left + (right - left) * 0.55, left + (right - left) * 0.75)
        ys = (
            top + (bottom - top) * 0.4,
            top + (bottom - top) * 0.6,
            top + (bottom - top) * 0.8,
        )
        for y in ys:
            for x in xs:
                safe = await surface.evaluate(
                    """(surface, point) => {
                        const target = document.elementFromPoint(point.x, point.y);
                        if (!target || !surface.contains(target)) return false;
                        const excluded = target.closest(
                            "nav, aside, footer, [role='navigation'], [role='menu'], " +
                            "[role='complementary'], [role='dialog'], [role='search'], " +
                            "[role='banner'], [role='contentinfo']"
                        );
                        const control = target.closest(
                            "a, button, input, select, textarea, [role='button'], " +
                            "[role='link'], [role='tab'], [role='menuitem']"
                        );
                        return !excluded && !control;
                    }""",
                    {"x": x, "y": y},
                )
                if safe:
                    return (x, y), advance, visible_height
        raise UiReadError("HOME_SCROLL_SURFACE_UNVERIFIED")

    async def _scroll_surface(self) -> JSHandle:
        if not await self._visible_feed_evidence():
            self.last_scan_counts.scroll_surface_status = "UNVERIFIED"
            raise UiReadError("HOME_SCROLL_SURFACE_UNVERIFIED")
        region = await self._content_region()
        viewport = self._page.viewport_size
        if viewport is None:
            self.last_scan_counts.scroll_surface_status = "UNVERIFIED"
            raise UiReadError("FEED_VIEWPORT_UNVERIFIED")
        region_handle = await region.element_handle()
        if region_handle is None:
            self.last_scan_counts.scroll_surface_status = "UNVERIFIED"
            raise UiReadError("HOME_SCROLL_SURFACE_UNVERIFIED")
        row_handles: list[JSHandle] = []
        try:
            for root in self._last_viewport_candidate_roots:
                handle = await root.element_handle()
                if handle is None:
                    self.last_scan_counts.scroll_surface_status = "UNVERIFIED"
                    raise UiReadError("HOME_SCROLL_SURFACE_UNVERIFIED")
                row_handles.append(handle)
            surface = await self._page.evaluate_handle(
                """([region, rows, maxCandidates]) => {
                const root = document.scrollingElement;
                const visibleItem = item => {
                    if (!item.isConnected || (item !== region && !region.contains(item)) ||
                        item.closest(
                        "nav, aside, footer, [role='navigation'], [role='menu'], " +
                        "[role='complementary'], [role='dialog'], [role='search'], " +
                        "[role='banner'], [role='contentinfo'], form, " +
                        "[contenteditable='true'], [role='textbox']"
                    )) return false;
                    const style = window.getComputedStyle(item);
                    const rect = item.getBoundingClientRect();
                    return style.display !== "none" && style.visibility !== "hidden" &&
                        rect.width > 0 && rect.height > 0 && rect.bottom > 0 &&
                        rect.right > 0 && rect.top < window.innerHeight &&
                        rect.left < window.innerWidth;
                };
                const isScrollable = element => {
                    if (element.scrollHeight <= element.clientHeight + 1) return false;
                    if (element === root) return true;
                    const overflow = window.getComputedStyle(element).overflowY;
                    return overflow === "auto" || overflow === "scroll" ||
                        overflow === "overlay";
                };
                let items = rows;
                if (items.length && !items.every(visibleItem)) return null;
                if (!items.length) {
                    items = Array.from(
                        region.querySelectorAll('a[href*="/post/"]')
                    ).slice(0, maxCandidates).filter(visibleItem);
                }
                if (!items.length) return null;

                const candidates = new Set();
                for (const item of items) {
                    for (let element = item; element;
                         element = element.parentElement) {
                        if (isScrollable(element)) candidates.add(element);
                    }
                }
                const innermost = Array.from(candidates).filter(candidate =>
                    items.every(item => candidate === item || candidate.contains(item)) &&
                    !Array.from(candidates).some(other => candidate !== other &&
                        candidate.contains(other) &&
                        items.every(item => other === item || other.contains(item)))
                );
                if (innermost.length !== 1) return null;
                const surface = innermost[0];
                if (!items.every(item => surface === item || surface.contains(item))) return null;
                return surface;
                }""",
                arg=[region_handle, row_handles, _MAX_PERMALINK_CANDIDATES],
            )
        finally:
            await region_handle.dispose()
            for handle in row_handles:
                await handle.dispose()
        if await surface.evaluate("element => element === null"):
            await surface.dispose()
            self.last_scan_counts.scroll_surface_status = "UNVERIFIED"
            raise UiReadError("HOME_SCROLL_SURFACE_UNVERIFIED")
        self.last_scan_counts.scroll_surface_status = "VALIDATED"
        return surface

    async def _watch_scroll_completion(
        self,
        surface: JSHandle,
        initial_position: float,
    ) -> JSHandle:
        return await self._page.evaluate_handle(
            """([element, initialPosition]) => {
                const eventTarget = element === document.scrollingElement
                    ? document : element;
                const supportsScrollEnd = "onscrollend" in eventTarget;
                const state = {done: false, delta: 0, cancel: () => {}};
                let active = true;
                let frameOne = 0;
                let frameTwo = 0;
                const cleanup = () => {
                    element.removeEventListener("scroll", onScroll);
                    eventTarget.removeEventListener("scrollend", finish);
                    if (frameOne) cancelAnimationFrame(frameOne);
                    if (frameTwo) cancelAnimationFrame(frameTwo);
                };
                const finish = () => {
                    if (!active) return;
                    state.delta = Math.abs(element.scrollTop - initialPosition);
                    state.done = true;
                    active = false;
                    cleanup();
                };
                const onScroll = () => {
                    if (supportsScrollEnd) return;
                    if (frameOne) cancelAnimationFrame(frameOne);
                    if (frameTwo) cancelAnimationFrame(frameTwo);
                    frameOne = requestAnimationFrame(() => {
                        frameTwo = requestAnimationFrame(finish);
                    });
                };
                state.cancel = () => {
                    active = false;
                    cleanup();
                };
                element.addEventListener("scroll", onScroll, {passive: true});
                eventTarget.addEventListener("scrollend", finish, {once: true});
                return state;
            }""",
            arg=[surface, initial_position],
        )

    async def _wait_for_scroll_movement(
        self,
        witness: JSHandle,
        deadline: float,
    ) -> float | None:
        result = None
        try:
            result = await self._page.wait_for_function(
                "state => state.done && state.delta > 1 ? state.delta : false",
                arg=witness,
                timeout=self._remaining_readiness_ms(
                    deadline,
                    "HOME_SCROLL_UNVERIFIED",
                ),
                polling="raf",
            )
            delta = await result.json_value()
            return None if delta is False or delta is None else float(delta)
        except PlaywrightTimeoutError:
            return None
        except PlaywrightError:
            raise UiReadError("HOME_SCROLL_UNVERIFIED") from None
        finally:
            if result is not None:
                try:
                    await result.dispose()
                except PlaywrightError:
                    pass

    async def _is_carried_viewport_candidate(self, root: Locator) -> bool:
        for previous in self._carried_viewport_candidate_roots:
            handle = await previous.element_handle()
            if handle is None:
                continue
            try:
                if await root.evaluate("(node, other) => node === other", handle):
                    return True
            finally:
                await handle.dispose()
        return False

    async def _visible_targets(
        self,
        max_posts: int,
        *,
        currently_visible: bool = False,
        prefer_new_fingerprints: set[str] | None = None,
        stage: str = "FEED",
    ) -> _VisibleTargets:
        viewport = self._page.viewport_size
        if viewport is None:
            raise UiReadError("FEED_VIEWPORT_UNVERIFIED")
        counts = self.last_scan_counts
        counts.visible_links = 0
        counts.inspected = 0
        counts.article_roots = 0
        counts.pressable_roots = 0
        counts.visible_times = 0
        counts.profile_anchor_candidates = 0
        counts.visible_anchors = 0
        counts.visible_post_links = 0
        counts.body_candidates = 0
        counts.paragraph_candidates = 0
        counts.url_parsed = 0
        counts.card_not_ready = 0
        counts.verified = 0
        counts.body_read = 0
        counts.body_unknown = 0
        counts.observed_rows = 0
        counts.body_extracted = 0
        counts.skipped_rows = 0
        counts.uncertain_rows = 0
        counts.coverage_complete = True
        counts.skipped = 0
        counts.rejection_reasons.clear()
        counts.unsupported_roots = 0
        counts.region_status = "UNSET"
        counts.scroll_surface_status = "NOT_CHECKED"
        counts.structural_reasons.clear()
        region = await self._content_region()
        roots, roots_truncated = await self._top_level_candidate_roots(region, viewport)
        structures = await self._collect_structure_diagnostics(
            region,
            roots,
            viewport,
        )
        candidate_rows: list[tuple[int, Locator, _CandidateStructure]] = []
        for index, root in enumerate(roots):
            structure = structures[index]
            has_row_cues = structure.visible_times and (
                structure.profile_anchor_candidates
                or structure.visible_post_links
                or structure.body_candidates
            )
            semantic_row = (
                structure.body_candidates
                and (
                    structure.visible_times
                    or structure.profile_anchor_candidates
                    or structure.visible_post_links
                )
                and await root.evaluate(
                "node => node.matches('article, [role=\"article\"], "
                "[data-pressable-container=\"true\"]')"
                )
            )
            carried_row = await self._is_carried_viewport_candidate(root)
            if has_row_cues or semantic_row or carried_row:
                candidate_rows.append((index, root, structure))
        counts.observed_rows = len(candidate_rows)
        counts.coverage_complete = (
            not roots_truncated
            and len(candidate_rows) <= max_posts
            and not any(structure.visible_times > 1 for _, _, structure in candidate_rows)
        )
        targets: list[PostTarget] = []
        scroll_anchor: dict[str, float] | None = None
        display_observations: list[DisplayObservation] = []
        pending_roots: list[Locator] = []
        scanned_fingerprints: set[str] = set()
        for row_index, root, structure in candidate_rows[:max_posts]:
            box = await root.bounding_box()
            if box is None:
                counts.skipped_rows += 1
                counts.uncertain_rows += 1
                display_observations.append(
                    DisplayObservation(None, "OBSERVATION_UNCERTAIN")
                )
                continue
            counts.inspected += 1
            if scroll_anchor is None:
                scroll_anchor = box
            if not (
                structure.profile_anchor_candidates
                or structure.visible_post_links
            ) and not carried_row:
                counts.skipped_rows += 1
                counts.uncertain_rows += 1
                self._record_structure_reason("DISPLAY_ROW_CUES_INSUFFICIENT")
                display_observations.append(
                    DisplayObservation(None, "OBSERVATION_UNCERTAIN", box["y"])
                )
                continue
            if (
                structure.visible_times > 1
            ):
                counts.skipped_rows += 1
                counts.uncertain_rows += 1
                self._record_structure_reason("CANDIDATE_ROW_AMBIGUOUS")
                display_observations.append(
                    DisplayObservation(None, "OBSERVATION_UNCERTAIN", box["y"])
                )
                continue
            try:
                target = await self._read_card(
                    root,
                    currently_visible=currently_visible,
                    viewport_y=box["y"],
                )
            except _CardNotReady:
                counts.card_not_ready += 1
                body = await self._observe_display_body(root)
                if body:
                    counts.body_extracted += 1
                    display_observations.append(
                        DisplayObservation(body, viewport_y=box["y"])
                    )
                else:
                    counts.body_unknown += 1
                    counts.skipped_rows += 1
                    counts.uncertain_rows += 1
                    pending_roots.append(root)
                    display_observations.append(
                        DisplayObservation(None, "OBSERVATION_UNCERTAIN", box["y"])
                    )
                continue
            except _UnverifiedFeedCandidate as error:
                self._record_scan_skip(stage, error)
                if error.diagnostic_reason == "CANDIDATE_WRAPPER_AMBIGUOUS":
                    counts.skipped_rows += 1
                    counts.uncertain_rows += 1
                    display_observations.append(
                        DisplayObservation(None, "OBSERVATION_UNCERTAIN", box["y"])
                    )
                    continue
                body = await self._observe_display_body(root)
                if body:
                    counts.body_extracted += 1
                    display_observations.append(
                        DisplayObservation(body, viewport_y=box["y"])
                    )
                else:
                    counts.body_unknown += 1
                    counts.skipped_rows += 1
                    counts.uncertain_rows += 1
                    pending_roots.append(root)
                    display_observations.append(
                        DisplayObservation(None, "OBSERVATION_UNCERTAIN", box["y"])
                    )
                continue
            if currently_visible:
                permalink_box = await target.permalink.bounding_box()
                if (
                    permalink_box is None
                    or not self._intersects_viewport(permalink_box, viewport)
                ):
                    body = await self._observe_display_body(root)
                    if body:
                        counts.body_extracted += 1
                        display_observations.append(
                            DisplayObservation(body, viewport_y=box["y"])
                        )
                    else:
                        counts.body_unknown += 1
                        counts.skipped_rows += 1
                        counts.uncertain_rows += 1
                        pending_roots.append(root)
                        display_observations.append(
                            DisplayObservation(
                                None,
                                "OBSERVATION_UNCERTAIN",
                                box["y"],
                            )
                        )
                    continue
            counts.verified += 1
            if target.body and target.body.strip():
                counts.body_read += 1
                counts.body_extracted += 1
            else:
                counts.body_unknown += 1
                self._record_structure_reason("POST_BODY_STRUCTURE_UNSUPPORTED")
                pending_roots.append(root)
            if (
                target.fingerprint in scanned_fingerprints
                or (
                    prefer_new_fingerprints is not None
                    and target.fingerprint in prefer_new_fingerprints
                )
            ):
                self.feed_duplicates += 1
                continue
            scanned_fingerprints.add(target.fingerprint)
            targets.append(target)
        if not candidate_rows:
            permalink_targets, permalink_anchor = await self._visible_permalink_targets(
                max_posts,
                currently_visible=currently_visible,
                prefer_new_fingerprints=prefer_new_fingerprints,
                stage=stage,
            )
            targets.extend(permalink_targets)
            if scroll_anchor is None:
                scroll_anchor = permalink_anchor
            counts.observed_rows = max(
                len(permalink_targets),
                counts.visible_post_links,
            )
            counts.coverage_complete = (
                not roots_truncated and counts.observed_rows <= max_posts
            )
        if counts.body_unknown or counts.uncertain_rows:
            counts.coverage_complete = False
        signature = (
            tuple(
                (
                    target.url,
                    target.author,
                    target.body,
                    target.body_available,
                    round(target.viewport_y, 1),
                )
                for target in targets
            ),
            tuple(
                (
                    observation.body,
                    observation.status,
                    round(observation.viewport_y, 1),
                )
                for observation in display_observations
            ),
            counts.observed_rows,
            counts.coverage_complete,
            counts.uncertain_rows,
        )
        return _VisibleTargets(
            tuple(targets),
            scroll_anchor,
            tuple(display_observations),
            counts.observed_rows,
            counts.coverage_complete,
            signature,
            tuple(root for _, root, _ in candidate_rows[:max_posts]),
            tuple(pending_roots),
        )

    @staticmethod
    def _has_new_viewport_content(
        previous: tuple[object, ...] | None,
        current: tuple[object, ...],
        scroll_delta: float,
    ) -> bool:
        if previous is None:
            return True
        previous_verified = previous[0]
        current_verified = current[0]
        previous_urls = {entry[0] for entry in previous_verified}
        if any(entry[0] not in previous_urls for entry in current_verified):
            return True
        previous_positions = {entry[0]: entry[4] for entry in previous_verified}
        if any(
            entry[0] in previous_positions
            and abs((previous_positions[entry[0]] - scroll_delta) - entry[4]) <= 48
            for entry in current_verified
        ):
            return True
        if current[2] > previous[2]:
            return True

        previous_observations = previous[1]
        matched: set[int] = set()
        for body, _status, viewport_y in current[1]:
            if body is None:
                continue
            for index, (previous_body, _previous_status, previous_y) in enumerate(
                previous_observations
            ):
                if index in matched or previous_body != body:
                    continue
                if abs((previous_y - scroll_delta) - viewport_y) <= 48:
                    matched.add(index)
                    break
            else:
                return True
        return False

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

    async def _is_in_viewport(
        self,
        element: Locator,
        viewport: dict[str, int],
    ) -> bool:
        if not await element.is_visible():
            return False
        box = await element.bounding_box()
        return box is not None and self._intersects_viewport(box, viewport)

    async def _is_nested_feed_root(self, root: Locator) -> bool:
        return bool(
            await root.evaluate(
                "(node, selector) => Boolean(node.parentElement?.closest(selector))",
                _FEED_ROOT_SELECTOR,
            )
        )

    async def _is_nested_feed_content(self, element: Locator) -> bool:
        return bool(
            await element.evaluate(
                """(node, selector) => {
                    const nearest = node.parentElement?.closest(selector);
                    return Boolean(nearest?.parentElement?.closest(selector));
                }""",
                _FEED_ROOT_SELECTOR,
            )
        )

    async def _belongs_to_card(
        self,
        element: Locator,
        root: Locator,
        root_handle: JSHandle,
    ) -> bool:
        return bool(
            await element.evaluate(
                """(node, args) => {
                    if (node.closest(args.quote)) return false;
                    const nearest = node.parentElement?.closest(args.selector);
                    if (args.root.matches(args.selector)) {
                        return nearest === args.root;
                    }
                    return args.root.contains(node) && nearest === null;
                }""",
                {
                    "selector": _FEED_ROOT_SELECTOR,
                    "quote": _QUOTE_SELECTOR,
                    "root": root_handle,
                },
            )
        )

    async def _safe_body_candidate(
        self,
        element: Locator,
        root: Locator,
        root_handle: JSHandle,
    ) -> bool:
        return bool(
            await element.evaluate(
                """(node, args) => {
                    const excluded = args.excluded;
                    let current = node;
                    while (current && current !== args.root) {
                        if (current.matches(excluded)) return false;
                        current = current.parentElement;
                    }
                    if (current !== args.root) return false;
                    const nearestRoot = node.parentElement?.closest(args.roots);
                    if (args.root.matches(args.roots)) {
                        if (nearestRoot !== args.root) return false;
                    } else if (!args.root.contains(node) || nearestRoot) {
                        return false;
                    }
                    return !node.querySelector(
                        `${excluded}, ${args.roots}`
                    );
                }""",
                {
                    "root": root_handle,
                    "roots": _FEED_ROOT_SELECTOR,
                    "excluded": _BODY_EXCLUSION_SELECTOR,
                },
            )
        )

    async def _safe_unscoped_text_candidate(self, element: Locator) -> bool:
        if await self._is_outside_content_landmark(element):
            return False
        if await self._is_nested_feed_content(element):
            return False
        return bool(
            await element.evaluate(
                """(node, excluded) => {
                    let current = node;
                    while (current) {
                        if (current.matches(excluded)) return false;
                        current = current.parentElement;
                    }
                    return !node.querySelector(excluded);
                }""",
                _BODY_EXCLUSION_SELECTOR,
            )
        )

    @staticmethod
    def _profile_anchor_candidate(value: str) -> bool:
        return _profile_author(value) is not None

    async def _top_level_candidate_roots(
        self,
        region: Locator,
        viewport: dict[str, int],
    ) -> tuple[list[Locator], bool]:
        candidates: list[tuple[float, Locator]] = []
        candidate_handles: list[JSHandle] = []
        self._carried_viewport_candidate_roots = ()
        carried_roots: list[Locator] = []
        truncated = False

        async def add_candidate(root: Locator, box: dict[str, float]) -> None:
            handle = await root.element_handle()
            if handle is None:
                return
            for existing in candidate_handles:
                if await handle.evaluate("(node, other) => node === other", existing):
                    await handle.dispose()
                    return
            candidate_handles.append(handle)
            candidates.append((box["y"], root))

        selectors = (
            'article, [role="article"]:not(article)',
            '[data-pressable-container="true"]:not(article):not([role="article"])',
        )
        for selector in selectors:
            roots = region.locator(selector)
            available = await roots.count()
            count = min(available, _MAX_FEED_ROOTS_PER_TYPE)
            truncated = truncated or available > count
            for index in range(count):
                root = roots.nth(index)
                if not await root.is_visible():
                    continue
                if (
                    await self._is_outside_content_landmark(root)
                    or await self._is_inside_quote(root)
                ):
                    continue
                box = await root.bounding_box()
                if box is None or not self._intersects_viewport(box, viewport):
                    continue
                if await self._is_nested_feed_root(root):
                    continue
                await add_candidate(root, box)

        region_handle = await region.element_handle()
        if region_handle is not None:
            try:
                for root in self._last_viewport_candidate_roots:
                    if not await root.is_visible():
                        continue
                    if (
                        await self._is_outside_content_landmark(root)
                        or await self._is_inside_quote(root)
                        or not await root.evaluate(
                            "(node, parent) => parent.contains(node)",
                            region_handle,
                        )
                    ):
                        continue
                    box = await root.bounding_box()
                    if box is None or not self._intersects_viewport(box, viewport):
                        continue
                    carried_roots.append(root)
                    await add_candidate(root, box)
            finally:
                await region_handle.dispose()
        self._carried_viewport_candidate_roots = tuple(carried_roots)

        # Some pages expose independent post rows only as ordinary containers.
        # Infer a root from the nearest visible ancestor that owns exactly one
        # timestamp and one profile-author link; no post permalink is required.
        times = region.locator("time")
        time_count = await times.count()
        if time_count > _MAX_STRUCTURE_ELEMENTS:
            truncated = True
        for index in range(min(time_count, _MAX_STRUCTURE_ELEMENTS)):
            timestamp = times.nth(index)
            if (
                await self._is_outside_content_landmark(timestamp)
                or await self._is_inside_quote(timestamp)
                or await timestamp.evaluate(
                    "(node, selector) => Boolean(node.parentElement?.closest(selector))",
                    _FEED_ROOT_SELECTOR,
                )
                or not await self._is_in_viewport(timestamp, viewport)
            ):
                continue
            ancestor = timestamp
            fallback_candidate: tuple[Locator, dict[str, float]] | None = None
            for _ in range(_MAX_CARD_ANCESTORS):
                ancestor = ancestor.locator("xpath=..").first
                if await ancestor.count() == 0:
                    break
                tag_and_role = await ancestor.evaluate(
                    "node => [node.tagName.toLowerCase(), node.getAttribute('role')]"
                )
                if tag_and_role[0] in {"body", "html", "main"} or tag_and_role[1] in {
                    "main",
                    "feed",
                }:
                    break
                if (
                    await self._is_outside_content_landmark(ancestor)
                    or await self._is_inside_quote(ancestor)
                ):
                    break
                if await ancestor.evaluate(
                    "(node, selector) => node.matches(selector)",
                    _FEED_ROOT_SELECTOR,
                ):
                    # The semantic root was already collected above. A
                    # permalink-only ancestor inside it is the same row.
                    fallback_candidate = None
                    break
                box = await ancestor.bounding_box()
                if box is None or not self._intersects_viewport(box, viewport):
                    continue
                structure = await self._inspect_candidate_root(ancestor, viewport)
                if structure.visible_times == 1 and (
                    structure.profile_anchor_candidates
                    or structure.visible_post_links
                    or structure.body_candidates
                ):
                    fallback_candidate = (ancestor, box)
                    if structure.body_candidates:
                        await add_candidate(ancestor, box)
                        fallback_candidate = None
                        break
                if (
                    structure.visible_times > 1
                    or structure.profile_anchor_candidates > 1
                ):
                    if structure.body_candidates or structure.visible_post_links:
                        fallback_candidate = (ancestor, box)
                    break
            if fallback_candidate is not None:
                await add_candidate(fallback_candidate[0], fallback_candidate[1])

        candidates.sort(key=lambda item: item[0])
        roots = [root for _, root in candidates]
        if len(roots) > _MAX_VISIBLE_ROW_CANDIDATES:
            truncated = True
            roots = roots[:_MAX_VISIBLE_ROW_CANDIDATES]
        for handle in candidate_handles:
            await handle.dispose()
        return roots, truncated

    @staticmethod
    async def _is_inside_quote(element: Locator) -> bool:
        return (
            await element.locator(
                "xpath=ancestor-or-self::*[self::blockquote or @role='quote']"
            ).count()
            > 0
        )

    async def _inspect_candidate_root(
        self,
        root: Locator,
        viewport: dict[str, int],
    ) -> _CandidateStructure:
        structure = await root.evaluate(
            """(root, args) => {
                const visible = element => {
                    const style = getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== "none" && style.visibility !== "hidden" &&
                        rect.width > 0 && rect.height > 0 &&
                        rect.right > 0 && rect.bottom > 0 &&
                        rect.left < args.viewport.width &&
                        rect.top < args.viewport.height;
                };
                const belongs = element => {
                    if (element.closest(args.quote)) return false;
                    const nearest = element.parentElement?.closest(args.roots);
                    if (root.matches(args.roots)) return nearest === root;
                    return root.contains(element) && nearest === null;
                };
                const outside = element => element.closest(args.outside) !== null;
                const anchors = Array.from(root.querySelectorAll("a"))
                    .filter(element => belongs(element) && !outside(element) && visible(element))
                    .slice(0, args.maxLinks);
                const times = Array.from(root.querySelectorAll("time"))
                    .filter(element => belongs(element) && !outside(element) && visible(element))
                    .slice(0, args.maxLinks).length;
                const bodySafe = element => {
                    let current = element;
                    while (current && current !== root) {
                        if (current.matches(args.bodyExcluded)) return false;
                        current = current.parentElement;
                    }
                    if (current !== root || !belongs(element)) return false;
                    return !element.querySelector(`${args.bodyExcluded}, ${args.roots}`);
                };
                const bodies = Array.from(root.querySelectorAll(args.body))
                    .filter(element => bodySafe(element) && visible(element))
                    .slice(0, args.maxStructure);
                return {
                    hrefs: anchors.map(element => element.getAttribute("href")),
                    visible_anchors: anchors.length,
                    visible_times: times,
                    body_candidates: bodies.length,
                    paragraph_candidates: bodies.filter(element =>
                        element.tagName === "P").length,
                };
            }""",
            {
                "viewport": viewport,
                "roots": _FEED_ROOT_SELECTOR,
                "quote": _QUOTE_SELECTOR,
                "outside": (
                    "nav, aside, footer, form, [contenteditable='true'], "
                    "[role='navigation'], [role='menu'], [role='complementary'], "
                    "[role='dialog'], [role='search'], [role='banner'], "
                    "[role='contentinfo'], [role='textbox']"
                ),
                "body": _BODY_TEXT_SELECTOR,
                "bodyExcluded": _BODY_EXCLUSION_SELECTOR,
                "maxLinks": _MAX_CARD_LINKS,
                "maxStructure": _MAX_STRUCTURE_ELEMENTS,
            },
        )
        hrefs = (href for href in structure["hrefs"] if href is not None)
        href_list = tuple(hrefs)
        return _CandidateStructure(
            visible_anchors=structure["visible_anchors"],
            visible_post_links=sum("/post/" in href for href in href_list),
            canonical_post_links=sum(_post_url(href) is not None for href in href_list),
            profile_anchor_candidates=sum(
                self._profile_anchor_candidate(href) for href in href_list
            ),
            visible_times=structure["visible_times"],
            body_candidates=structure["body_candidates"],
            paragraph_candidates=structure["paragraph_candidates"],
        )

    def _record_structure_reason(self, reason: str) -> None:
        counts = self.last_scan_counts
        counts.structural_reasons[reason] = counts.structural_reasons.get(reason, 0) + 1

    async def _collect_structure_diagnostics(
        self,
        region: Locator,
        roots: list[Locator],
        viewport: dict[str, int],
    ) -> dict[int, _CandidateStructure]:
        counts = self.last_scan_counts
        structures: dict[int, _CandidateStructure] = {}
        for index, root in enumerate(roots):
            is_semantic = bool(
                await root.evaluate(
                    "node => node.matches('article, [role=\"article\"]')"
                )
            )
            is_pressable = await root.get_attribute("data-pressable-container") == "true"
            counts.article_roots += int(is_semantic)
            counts.pressable_roots += int(is_pressable)
            structure = await self._inspect_candidate_root(root, viewport)
            structures[index] = structure
            counts.visible_anchors += structure.visible_anchors
            counts.visible_post_links += structure.visible_post_links
            counts.url_parsed += structure.canonical_post_links
            counts.profile_anchor_candidates += structure.profile_anchor_candidates
            counts.visible_times += structure.visible_times
            counts.body_candidates += structure.body_candidates
            counts.paragraph_candidates += structure.paragraph_candidates
            row_evidence = structure.visible_times and (
                structure.profile_anchor_candidates or structure.visible_post_links
            )
            if row_evidence and not structure.body_candidates:
                counts.unsupported_roots += 1
                self._record_structure_reason("DISPLAY_BODY_PENDING")
            elif row_evidence and not (
                structure.canonical_post_links and structure.profile_anchor_candidates
            ):
                self._record_structure_reason("DISPLAY_OBSERVATION_ONLY")
            elif (
                structure.visible_post_links and not structure.canonical_post_links
            ) or (
                structure.canonical_post_links
                and not structure.profile_anchor_candidates
            ):
                self._record_structure_reason("POST_ROOT_UNRESOLVED")

        anchors = region.locator("a")
        for index in range(min(await anchors.count(), _MAX_STRUCTURE_ELEMENTS)):
            anchor = anchors.nth(index)
            if await self._is_outside_content_landmark(anchor):
                continue
            if await self._is_nested_feed_content(anchor):
                continue
            if await anchor.evaluate(
                "(node, selector) => Boolean(node.parentElement?.closest(selector))",
                _FEED_ROOT_SELECTOR,
            ):
                continue
            if not await self._is_in_viewport(anchor, viewport):
                continue
            counts.visible_anchors += 1
            href = await anchor.get_attribute("href")
            if href is None:
                continue
            if "/post/" in href:
                counts.visible_post_links += 1
                if _post_url(href) is not None:
                    counts.url_parsed += 1
            if self._profile_anchor_candidate(href):
                counts.profile_anchor_candidates += 1

        times = region.locator("time")
        for index in range(min(await times.count(), _MAX_STRUCTURE_ELEMENTS)):
            node = times.nth(index)
            if await self._is_nested_feed_content(node):
                continue
            if await node.evaluate(
                "(element, selector) => Boolean(element.parentElement?.closest(selector))",
                _FEED_ROOT_SELECTOR,
            ):
                continue
            if not await self._is_outside_content_landmark(node) and await self._is_in_viewport(
                node,
                viewport,
            ):
                counts.visible_times += 1

        body_nodes = region.locator(_BODY_TEXT_SELECTOR)
        paragraphs = region.locator("p")
        for index in range(min(await paragraphs.count(), _MAX_STRUCTURE_ELEMENTS)):
            node = paragraphs.nth(index)
            if await self._is_nested_feed_content(node):
                continue
            if await node.evaluate(
                "(element, selector) => Boolean(element.parentElement?.closest(selector))",
                _FEED_ROOT_SELECTOR,
            ):
                continue
            if (
                await self._safe_unscoped_text_candidate(node)
                and await self._is_in_viewport(node, viewport)
            ):
                counts.paragraph_candidates += 1
        for index in range(min(await body_nodes.count(), _MAX_STRUCTURE_ELEMENTS)):
            node = body_nodes.nth(index)
            if await self._is_nested_feed_content(node):
                continue
            if await node.evaluate(
                "(element, selector) => Boolean(element.parentElement?.closest(selector))",
                _FEED_ROOT_SELECTOR,
            ):
                continue
            if (
                await self._safe_unscoped_text_candidate(node)
                and await self._is_in_viewport(node, viewport)
            ):
                counts.body_candidates += 1
        counts.visible_links = counts.visible_post_links
        return structures

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
                self.last_scan_counts.region_status = "AMBIGUOUS"
                raise UiReadError(error_code)
            if visible:
                self.last_scan_counts.region_status = "LANDMARK"
                return visible[0]
        self.last_scan_counts.region_status = "BODY_FALLBACK"
        return self._page.locator("body")

    @staticmethod
    async def _is_outside_content_landmark(link: Locator) -> bool:
        excluded = link.locator(
            "xpath=ancestor-or-self::*[self::nav or self::aside or self::footer "
            "or self::form or @contenteditable='true' "
            "or @role='navigation' or @role='menu' or @role='complementary' "
            "or @role='dialog' or @role='search' or @role='banner' "
            "or @role='contentinfo' or @role='textbox']"
        )
        return await excluded.count() > 0

    async def _resolve_permalink_card(
        self,
        permalink: Locator,
        expected_url: str,
        expected_author: str,
    ) -> tuple[Locator, dict[str, float]]:
        if await self._is_nested_feed_content(permalink):
            raise _UnverifiedFeedCandidate(
                "POST_PROVENANCE_UNVERIFIED",
                "CANDIDATE_AMBIGUOUS",
                (expected_url,),
                diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
            )
        if await self._is_inside_quote(permalink):
            raise _UnverifiedFeedCandidate(
                "POST_PROVENANCE_UNVERIFIED",
                "CANDIDATE_AMBIGUOUS",
                (expected_url,),
                diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
            )
        candidate_root = permalink.locator(
            "xpath=ancestor::*[self::article or @role='article' "
            "or @data-pressable-container='true'][1]"
        )
        if await candidate_root.count() and not await self._is_nested_feed_root(
            candidate_root
        ):
            box = await candidate_root.bounding_box()
            if box is not None:
                return candidate_root, box
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

            links = ancestor.locator("a")
            link_count = await links.count()
            if link_count > _MAX_CARD_LINKS:
                raise _UnverifiedFeedCandidate(
                    "POST_PROVENANCE_UNVERIFIED",
                    "CANDIDATE_AMBIGUOUS",
                    (expected_url,),
                    diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
                )
            post_authors: dict[str, str] = {}
            authors: set[str] = set()
            for index in range(link_count):
                link = links.nth(index)
                if (
                    await self._is_outside_content_landmark(link)
                    or await self._is_nested_feed_content(link)
                    or not await link.is_visible()
                ):
                    continue
                href = await link.get_attribute("href")
                if href is None:
                    continue
                parsed_post = _post_url(href)
                if parsed_post is not None:
                    post_authors[parsed_post[0]] = parsed_post[1]
                    continue
                if "/post/" in href:
                    raise _UnverifiedFeedCandidate(
                        "POST_PROVENANCE_UNVERIFIED",
                        "CANDIDATE_NONCANONICAL",
                        (expected_url, href),
                        diagnostic_reason=_post_url_rejection_reason(href),
                    )
                author = _profile_author(href)
                if author is not None:
                    authors.add(author)
            if len(post_authors) > 1:
                raise _UnverifiedFeedCandidate(
                    "POST_PROVENANCE_UNVERIFIED",
                    "CANDIDATE_AMBIGUOUS",
                    tuple(post_authors),
                    diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
                )
            if expected_url not in post_authors:
                continue
            if post_authors[expected_url] != expected_author:
                raise _UnverifiedFeedCandidate(
                    "POST_AUTHOR_UNVERIFIED",
                    "CANDIDATE_AUTHOR_CONFLICT",
                    (expected_url,),
                    diagnostic_reason="CANDIDATE_AUTHOR_MISMATCH",
                )
            if len(authors) > 1:
                raise _UnverifiedFeedCandidate(
                    "POST_PROVENANCE_UNVERIFIED",
                    "CANDIDATE_AMBIGUOUS",
                    (expected_url,),
                    diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
                )
            if authors and expected_author not in authors:
                raise _UnverifiedFeedCandidate(
                    "POST_AUTHOR_UNVERIFIED",
                    "CANDIDATE_AUTHOR_CONFLICT",
                    (expected_url,),
                    diagnostic_reason="CANDIDATE_AUTHOR_MISMATCH",
                )

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
        raise _UnverifiedFeedCandidate(
            "POST_PROVENANCE_UNVERIFIED",
            "CANDIDATE_AMBIGUOUS",
            (expected_url,),
            diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
        )

    @staticmethod
    def _expected_profile_link_signal(root: Locator, author: str) -> Locator:
        return root.locator(
            f'a[href$="/@{author}" i], a[href$="/@{author}/" i]'
        ).first

    async def _visible_permalink_targets(
        self,
        max_posts: int,
        *,
        currently_visible: bool,
        prefer_new_fingerprints: set[str] | None,
        stage: str,
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
        targets_by_url: dict[str, PostTarget] = {}
        scroll_anchor: dict[str, float] | None = None
        pending_cards: list[_CardNotReady] = []
        scanned_fingerprints: set[str] = set()
        for index in range(scan_limit):
            permalink = permalinks.nth(index)
            if not await permalink.is_visible():
                continue
            if await self._is_outside_content_landmark(permalink):
                continue
            if await self._is_nested_feed_content(permalink):
                continue
            if await self._is_inside_quote(permalink):
                continue
            if await permalink.evaluate(
                "(node, selector) => Boolean(node.parentElement?.closest(selector))",
                _FEED_ROOT_SELECTOR,
            ):
                continue
            permalink_box = await permalink.bounding_box()
            if permalink_box is None or not self._intersects_viewport(permalink_box, viewport):
                continue
            self.last_scan_counts.inspected += 1
            href = await permalink.get_attribute("href")
            parsed_post = None if href is None else _post_url(href)
            if parsed_post is None:
                if href is not None and "/post/" in href:
                    self._record_scan_skip(
                        stage,
                        _UnverifiedFeedCandidate(
                            "POST_PROVENANCE_UNVERIFIED",
                            "CANDIDATE_NONCANONICAL",
                            (href,),
                            diagnostic_reason=_post_url_rejection_reason(href),
                        ),
                    )
                continue
            if parsed_post[0] in targets_by_url:
                self.feed_duplicates += 1
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
                    currently_visible=currently_visible,
                    viewport_y=box["y"],
                )
            except _CardNotReady as pending:
                self.last_scan_counts.card_not_ready += 1
                pending_cards.append(pending)
                continue
            except _UnverifiedFeedCandidate as error:
                self._record_scan_skip(stage, error)
                continue
            if target.url != parsed_post[0] or target.author != parsed_post[1]:
                self._record_scan_skip(
                    stage,
                    _UnverifiedFeedCandidate(
                        "POST_PROVENANCE_UNVERIFIED",
                        "CANDIDATE_AMBIGUOUS",
                        (parsed_post[0], target.url),
                        diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
                    ),
                )
                continue
            self.last_scan_counts.verified += 1
            if target.body and target.body.strip():
                self.last_scan_counts.body_read += 1
                self.last_scan_counts.body_extracted += 1
            else:
                self.last_scan_counts.body_unknown += 1
                self._record_structure_reason("POST_BODY_STRUCTURE_UNSUPPORTED")
            if (
                target.fingerprint in scanned_fingerprints
                or (
                    prefer_new_fingerprints is not None
                    and target.fingerprint in prefer_new_fingerprints
                )
            ):
                self.feed_duplicates += 1
                targets_by_url[target.url] = target
                continue
            scanned_fingerprints.add(target.fingerprint)
            if scroll_anchor is None:
                scroll_anchor = box
            targets_by_url[target.url] = target
            if len(scanned_fingerprints) >= max_posts:
                break

        if (
            not targets_by_url
            and pending_cards
            and self.last_scan_counts.skipped == 0
            and self.last_scan_counts.unsupported_roots == 0
        ):
            raise _CardNotReady.combine(pending_cards)
        targets = [
            target
            for target in targets_by_url.values()
            if target.fingerprint in scanned_fingerprints
        ]
        return tuple(targets), scroll_anchor

    async def read_detail(self, target: PostTarget) -> PostTarget:
        try:
            return await self._read_detail(target)
        except UiReadError as error:
            print(f"STOP stage=DETAIL code={error.code}")
            raise

    async def _read_detail(self, target: PostTarget) -> PostTarget:
        if not target.currently_visible:
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")
        deadline = self._readiness_deadline(_DETAIL_TIMEOUT_MS)
        try:
            await self._wait_for_verified_feed(
                deadline,
                timeout_code="DETAIL_NAVIGATION_UNVERIFIED",
            )
            if not await target.permalink.is_visible():
                raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")
            box = await target.permalink.bounding_box()
            viewport = self._page.viewport_size
            if (
                box is None
                or viewport is None
                or not self._intersects_viewport(box, viewport)
            ):
                raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")
            href = await target.permalink.get_attribute("href")
            parsed = None if href is None else _post_url(href)
            if parsed != (target.url, target.author):
                raise UiReadError("DETAIL_PROVENANCE_UNVERIFIED")
            remaining = self._remaining_readiness_ms(
                deadline,
                "DETAIL_NAVIGATION_UNVERIFIED",
            )
            await target.permalink.click(timeout=remaining)
            self._mark_ui_transition()
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

            async def observe() -> _RenderObservation[PostTarget] | None:
                nonlocal last_timeout_code
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
                        last_timeout_code,
                    )
                    if not detail.body_available:
                        last_timeout_code = "DETAIL_BODY_UNVERIFIED"
                        return None
                    signature = (
                        detail.url,
                        detail.author,
                        detail.body,
                        detail.body_available,
                        len(matching),
                    )
                    return _RenderObservation(
                        signature,
                        detail,
                        ready_state="DETAIL_VERIFIED",
                    )
                except _CardNotReady as pending:
                    last_timeout_code = (
                        pending.timeout_code or "DETAIL_ROOT_UNVERIFIED"
                    )
                    await self._wait_for_pending_card(pending, window_deadline)
                    return None

            try:
                return await self._await_render_settled(
                    "DETAIL",
                    window_deadline,
                    timeout_code="DETAIL_ROOT_UNVERIFIED",
                    observe=observe,
                )
            except UiReadError as error:
                if (
                    error.code == "DETAIL_ROOT_UNVERIFIED"
                    and last_timeout_code != "DETAIL_ROOT_UNVERIFIED"
                ):
                    raise UiReadError(last_timeout_code) from None
                raise

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
            self._last_viewport_limit,
            timeout_code="FEED_RETURN_NOT_READY",
        )
        if not self.last_viewport_coverage_complete:
            raise UiReadError("VIEWPORT_COVERAGE_INCOMPLETE")
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
            raise UiReadError("PROFILE_CONTROL_NOT_READY")
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
                if error.code not in {
                    "PROFILE_CONTROL_NOT_READY",
                    "FOR_YOU_CONTROL_NOT_READY",
                }:
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
    async def _extract_card_body(
        self,
        root: Locator,
        root_handle: JSHandle,
        *,
        require_body: bool,
    ) -> tuple[str | None, bool]:
        buttons = root.get_by_role("button", name=_MORE_NAME)
        for index in range(min(await buttons.count(), _MAX_CARD_LINKS)):
            button = buttons.nth(index)
            if await self._belongs_to_card(button, root, root_handle) and await button.is_visible():
                return None, True

        candidates = root.locator(_BODY_TEXT_SELECTOR)
        bodies: list[str] = []
        paragraph_bodies = 0
        count = min(await candidates.count(), _MAX_STRUCTURE_ELEMENTS)
        for index in range(count):
            candidate = candidates.nth(index)
            if not await self._safe_body_candidate(candidate, root, root_handle):
                continue
            if not await candidate.is_visible():
                continue
            has_body_candidate_ancestor = await candidate.evaluate(
                """(node, args) => {
                    let current = node.parentElement;
                    while (current && current !== args.root) {
                        if (
                            current.matches(args.body)
                            && !current.querySelector(args.excluded)
                            && !current.querySelector(args.roots)
                        ) return true;
                        current = current.parentElement;
                    }
                    return current !== args.root;
                }""",
                {
                    "root": root_handle,
                    "body": _BODY_TEXT_SELECTOR,
                    "excluded": _BODY_EXCLUSION_SELECTOR,
                    "roots": _FEED_ROOT_SELECTOR,
                },
            )
            if has_body_candidate_ancestor:
                continue
            text = (await candidate.inner_text()).strip()
            if not text:
                continue
            bodies.append(text)
            paragraph_bodies += int(
                await candidate.evaluate("element => element.tagName === 'P'")
            )

        if not bodies:
            if require_body:
                raise _CardNotReady(
                    candidates.first,
                    nonempty_text=True,
                    timeout_code="DETAIL_BODY_UNVERIFIED",
                )
            return None, False
        if paragraph_bodies > 1:
            if require_body:
                raise _CardNotReady(
                    candidates.first,
                    nonempty_text=True,
                    timeout_code="DETAIL_BODY_UNVERIFIED",
                )
            return None, False
        body = "\n".join(bodies)
        if len(body) > _MAX_BODY_CODEPOINTS:
            if require_body:
                raise _CardNotReady(
                    candidates.first,
                    nonempty_text=True,
                    timeout_code="DETAIL_BODY_UNVERIFIED",
                )
            return None, True
        return body, True

    async def _observe_display_body(self, root: Locator) -> str | None:
        root_handle = await root.element_handle()
        if root_handle is None:
            return None
        body, available = await self._extract_card_body(
            root,
            root_handle,
            require_body=False,
        )
        return body if available and body and body.strip() else None

    async def _read_card(
        self,
        root: Locator,
        *,
        currently_visible: bool = False,
        viewport_y: float = 0.0,
        require_body: bool = False,
    ) -> PostTarget:
        if not await root.is_visible():
            raise _CardNotReady(root)
        root_handle = await root.element_handle()
        if root_handle is None:
            raise _CardNotReady(root)
        post_links: list[tuple[str, str, Locator]] = []
        authors: set[str] = set()
        links = root.locator("a")
        link_count = await links.count()
        if link_count > _MAX_CARD_LINKS:
            raise _UnverifiedFeedCandidate(
                "POST_PROVENANCE_UNVERIFIED",
                "CANDIDATE_AMBIGUOUS",
                (),
                diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
            )
        for index in range(link_count):
            link = links.nth(index)
            if not await self._belongs_to_card(link, root, root_handle):
                continue
            if await self._is_outside_content_landmark(link) or not await link.is_visible():
                continue
            href = await link.get_attribute("href")
            if href is None:
                continue
            parsed_post = _post_url(href)
            if parsed_post is not None:
                post_links.append((parsed_post[0], parsed_post[1], link))
                continue
            if "/post/" in href:
                raise _UnverifiedFeedCandidate(
                    "POST_PROVENANCE_UNVERIFIED",
                    "CANDIDATE_NONCANONICAL",
                    (href,),
                    diagnostic_reason=_post_url_rejection_reason(href),
                )
            author = _profile_author(href)
            if author is not None:
                authors.add(author)
        unique_urls = {item[0] for item in post_links}
        unique_permalink_links = {
            (item[0], item[1]) for item in post_links
        }
        if len(unique_urls) > 1 or len(unique_permalink_links) > 1 or len(authors) > 1:
            raise _UnverifiedFeedCandidate(
                "POST_PROVENANCE_UNVERIFIED",
                "CANDIDATE_AMBIGUOUS",
                tuple(unique_urls or authors),
                diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
            )
        if not post_links:
            raise _CardNotReady(root, condition="mutation")
        if not authors:
            raise _CardNotReady(
                self._expected_profile_link_signal(root, post_links[0][1])
            )
        if len(unique_urls) != 1 or len(unique_permalink_links) != 1:
            raise _UnverifiedFeedCandidate(
                "POST_PROVENANCE_UNVERIFIED",
                "CANDIDATE_AMBIGUOUS",
                tuple(unique_urls),
                diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
            )
        url, permalink_author, link = post_links[0]
        author = next(iter(authors))
        if author != permalink_author:
            raise _UnverifiedFeedCandidate(
                "POST_AUTHOR_UNVERIFIED",
                "CANDIDATE_AUTHOR_CONFLICT",
                (url,),
                diagnostic_reason="CANDIDATE_AUTHOR_MISMATCH",
            )

        body, body_available = await self._extract_card_body(
            root,
            root_handle,
            require_body=require_body,
        )
        fingerprint = sha256(f"{url}\n{author}".encode()).hexdigest()
        return PostTarget(
            url,
            author,
            body,
            fingerprint,
            link,
            body_available=body_available,
            currently_visible=currently_visible,
            viewport_y=viewport_y,
        )
