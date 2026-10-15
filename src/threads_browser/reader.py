"""Small async Playwright reader for a bounded Home / For You observation."""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
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

from .browser_ui.geometry import with_scrollport_geometry as _with_scrollport_geometry
from .browser_ui.scrolling import NativeWheelScrollEngine, ScrollGestureResult
from .errors import UiReadError
from .feed.models import (
    BodyReadEvidence,
    DisplayObservation,
    HomeSample,
    PostTarget,
    VisibleBodySegment,
)
from .feed.relevance import canonical_terms, classify_body, normalize_text
from .feed.urls import (
    _USERNAME,
    HOME_URL,
    THREADS_ORIGIN,
    _CandidateDiagnosticReason,
    _post_url,
    _post_url_hint,
    _post_url_rejection_reason,
    _post_url_structure,
    _profile_author,
    _same_home_origin,
)

__all__ = [
    "HOME_URL",
    "THREADS_ORIGIN",
    "DisplayObservation",
    "HomeSample",
    "PostTarget",
    "ThreadsHomeReader",
    "UiReadError",
    "canonical_terms",
    "classify_body",
    "normalize_text",
]

_NAVIGATION_TIMEOUT_MS = 20_000
_DETAIL_TIMEOUT_MS = 15_000
_READINESS_TIMEOUT_MS = 8_000
_AUTOMATIC_READINESS_WINDOWS = 2
_READINESS_RECHECK_INTERVAL_MS = 200
_CARD_HYDRATION_WAIT_MS = 800
_BODY_STABILITY_INTERVAL_MS = 180
_RENDER_MIN_DWELL_MS = 600
_RENDER_SETTLE_INTERVAL_MS = 300
_RENDER_STABLE_OBSERVATIONS = 2
_SCROLL_ADVANCE_RATIO = 0.4
_SCROLL_ADVANCE_MIN_PX = 64
_SCROLL_ADVANCE_MAX_PX = 360
_MAX_BODY_CODEPOINTS = 10_000
_MAX_BODY_TEXT_NODES = 256
_MAX_BODY_RANGE_CHECKS = 40_000
_MAX_BODY_PROGRESS_POSTS = 200
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


def _canonicalize_body(value: str) -> str:
    lines = [
        re.sub(r"[^\S\n]+", " ", line).strip()
        for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    return "\n".join(line for line in lines if line)


def _canonicalize_body_block(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _body_progress_state(
    evidence: BodyReadEvidence | None,
    *,
    has_readable_body: bool,
) -> tuple[str, str]:
    if has_readable_body:
        return "PARTIAL_BODY", "VISIBLE_TEXT_PROGRESS"
    state = "NO_BODY_NODES" if evidence is None else evidence.state
    return {
        "TEXT_BELOW": ("DEFERRED_TEXT_BELOW", "BODY_TEXT_BELOW_VIEWPORT"),
        "TEXT_ABOVE": ("DEFERRED_TEXT_ABOVE", "BODY_TEXT_ABOVE_VIEWPORT"),
        "MEDIA_OR_WHITESPACE_GAP": (
            "MEDIA_OR_WHITESPACE_GAP",
            "NO_RENDERED_BODY_TEXT_IN_VIEW",
        ),
        "UNSUPPORTED_STRUCTURE": (
            "UNSUPPORTED_STRUCTURE",
            "POST_BODY_STRUCTURE_UNSUPPORTED",
        ),
    }.get(state, ("PENDING_HYDRATION", "POST_BODY_PENDING"))


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
    condition: Literal["visible", "in_viewport", "nonempty_text", "retry"]
    timeout_code: str | None = None


class _CardNotReady(Exception):
    def __init__(
        self,
        locator: Locator | None = None,
        *,
        in_viewport: bool = False,
        nonempty_text: bool = False,
        timeout_code: str | None = None,
        condition: Literal["visible", "retry"] = "visible",
        signals: tuple[_CardWaitSignal, ...] | None = None,
        reason_code: str = "READINESS_SIGNAL_PENDING",
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
        self.reason_code = reason_code
        super().__init__()

    @classmethod
    def combine(cls, pending: list[_CardNotReady]) -> _CardNotReady:
        return cls(signals=tuple(signal for item in pending for signal in item.signals))


class _CardDeferredOffscreen(Exception):
    def __init__(self, fingerprint: str, reason: str) -> None:
        self.fingerprint = fingerprint
        self.reason = reason
        super().__init__()


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
    url_timestamp_links: int = 0
    url_secondary_links: int = 0
    url_canonical_links: int = 0
    url_rejected_links: int = 0
    author_unverified: int = 0
    wrapper_ambiguous: int = 0
    card_not_ready: int = 0
    deferred_offscreen: int = 0
    identity_verified: int = 0
    new_verified_rows: int = 0
    partial_body_rows: int = 0
    deferred_identity_rows: int = 0
    new_visible_text_segments: int = 0
    deferred_above: int = 0
    deferred_below: int = 0
    nearest_deferred_below_px: float | None = None
    frontier_content_px: float | None = None
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
    url_structure_counts: dict[str, int] = field(default_factory=dict)
    structural_reasons: dict[str, int] = field(default_factory=dict)
    unsupported_roots: int = 0
    roots_truncated: bool = False
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
    readable_body_candidates: int = 0
    paragraph_candidates: int = 0
    offscreen_post_links: int = 0
    offscreen_profile_anchors: int = 0
    offscreen_times: int = 0
    offscreen_body_candidates: int = 0
    post_url_links: tuple[tuple[str, bool], ...] = ()
    root_kind: str = "CONTAINER"


@dataclass(slots=True, repr=False)
class _TextFragment:
    start: int
    end: int
    text: str = field(repr=False)
    viewport_y: float = field(default=0.0, repr=False)
    content_y: float = field(default=0.0, repr=False)

    def __repr__(self) -> str:
        return "_TextFragment(<private>)"


@dataclass(slots=True, repr=False)
class _BodyTextProgress:
    length: int
    block: int
    order: int
    expected_ranges: list[tuple[int, int]] = field(default_factory=list, repr=False)
    visited_ranges: list[tuple[int, int]] = field(default_factory=list, repr=False)
    fragments: list[_TextFragment] = field(default_factory=list, repr=False)

    def __repr__(self) -> str:
        return "_BodyTextProgress(<private>)"


@dataclass(frozen=True, slots=True)
class _VisibleBodyRead:
    body: str | None = field(repr=False)
    complete: bool
    has_rendered_text: bool
    consistent: bool = True
    segments: tuple[VisibleBodySegment, ...] = field(default=(), repr=False)
    deferred_above: int = 0
    deferred_below: int = 0
    nearest_deferred_below_px: float | None = None
    frontier_content_px: float | None = None
    truncated: bool = False
    read_evidence: BodyReadEvidence = field(default_factory=BodyReadEvidence)


@dataclass(frozen=True, slots=True)
class _HomeRowObservation:
    diagnostic: _CandidateDiagnostic
    target: PostTarget | None = None
    display: DisplayObservation | None = None
    fingerprint: str | None = None
    candidate: bool = True


@dataclass(slots=True)
class _FeedScanTimings:
    discover_ms: int = 0
    read_row_ms: int = 0
    stabilize_ms: int = 0
    wait_reason: str = "NONE"


@dataclass(slots=True)
class _CandidateDiagnostic:
    row_index: int
    root_kind: str
    has_visible_time: bool
    has_post_permalink: bool
    has_profile_author: bool
    has_readable_body: bool
    state: str = "NON_POST"
    reason: str = "NO_POST_ROW_EVIDENCE"
    identity_fingerprint: str | None = field(default=None, repr=False)
    observation_id: str | None = field(default=None, repr=False)
    read_evidence: BodyReadEvidence | None = field(default=None, repr=False)
    same_scroller_scroll_top_delta: float | None = field(default=None, repr=False)
    row_movement_px: float | None = field(default=None, repr=False)
    text_distance_delta_px: float | None = field(default=None, repr=False)
    row_top_before_px: float | None = field(default=None, repr=False)
    row_bottom_before_px: float | None = field(default=None, repr=False)
    clip_top_before_px: float | None = field(default=None, repr=False)
    clip_bottom_before_px: float | None = field(default=None, repr=False)
    text_top_before_px: float | None = field(default=None, repr=False)
    text_bottom_before_px: float | None = field(default=None, repr=False)
    wheel_surface_matches: bool | None = field(default=None, repr=False)
    scroll_reason_flags: tuple[str, ...] = field(default=(), repr=False)


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
    candidate_observation_ids: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class _VisibleTargets:
    targets: tuple[PostTarget, ...]
    scroll_anchor: dict[str, float] | None
    display_observations: tuple[DisplayObservation, ...]
    observed_rows: int
    coverage_complete: bool
    signature: tuple[object, ...] = field(repr=False)
    candidate_roots: tuple[Locator, ...] = field(default=(), repr=False)
    pending_signals: tuple[_CardWaitSignal, ...] = field(default=(), repr=False)
    candidate_observation_ids: tuple[str, ...] = field(default=(), repr=False)


class ThreadsHomeReader:
    """Reads verified visible Home / For You posts and at most one exact post."""

    def __init__(
        self,
        page: Page,
        expected_actor: str | None,
        *,
        readiness_prompt: Callable[[str, int], Awaitable[bool]] | None = None,
        detail_mode: bool = False,
    ) -> None:
        if expected_actor is not None and _USERNAME.fullmatch(expected_actor) is None:
            raise UiReadError("ACTOR_INVALID")
        self._page = page
        self._scroll_engine = NativeWheelScrollEngine(page)
        self._expected_actor = None if expected_actor is None else expected_actor.casefold()
        self._readiness_prompt = readiness_prompt
        self._detail_mode = detail_mode
        self._home_url: str | None = None
        self._verified_actor_route: str | None = None
        self._last_ui_transition_at: float | None = None
        self.scroll_attempts = 0
        self.scrolls = 0
        self.feed_duplicates = 0
        self.last_scan_counts = _FeedScanCounts()
        self.last_candidate_diagnostics: tuple[_CandidateDiagnostic, ...] = ()
        self.last_display_observations: tuple[DisplayObservation, ...] = ()
        self.last_scan_timings = _FeedScanTimings()
        self.last_scroll_targets: tuple[PostTarget, ...] = ()
        self.last_viewport_coverage_complete = True
        self.last_viewport_observed_rows = 0
        self.last_viewport_deferred_only = False
        self.last_viewport_can_wheel = False
        self.last_deferred_fingerprints: frozenset[str] = frozenset()
        self._last_viewport_limit = 20
        self.last_scroll_delta = 0.0
        self.last_scroll_gesture: ScrollGestureResult | None = None
        self.last_scroll_band = "UNKNOWN"
        self.last_scroll_end_distance: float | None = None
        self.last_scroll_near_end = False
        self.last_scroll_stop_reason: str | None = None
        self.last_scroll_surface_key: str | None = None
        self.last_scroll_geometry: dict[str, object] | None = None
        self._last_viewport_signature: tuple[object, ...] | None = None
        self._last_viewport_candidate_roots: tuple[Locator, ...] = ()
        self._last_viewport_candidate_observation_ids: tuple[str, ...] = ()
        self._carried_viewport_candidate_roots: tuple[Locator, ...] = ()
        self._carried_viewport_candidate_observation_ids: tuple[str, ...] = ()
        self._carried_viewport_candidate_handles: tuple[JSHandle, ...] = ()
        self._body_text_progress: dict[str, dict[str, _BodyTextProgress]] = {}
        self._body_visibility_ranges: dict[str, set[tuple[int, int, int]]] = {}
        self._body_progress_salt = secrets.token_bytes(16)
        self._cached_home_bodies: dict[str, tuple[str, bool]] = {}
        self._detail_return_anchor: tuple[str, float] | None = None
        self._detail_return_target: PostTarget | None = None
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
                                timeout=self._remaining_readiness_ms(
                                    deadline,
                                    "FOR_YOU_ACTIVATION_NOT_READY",
                                ),
                            )
                        except (AssertionError, PlaywrightTimeoutError):
                            if not self._is_for_you_route(self._page.url):
                                await self._require_feed_url(
                                    route_error_code="FOR_YOU_ROUTE_CHANGED"
                                )
                                raise UiReadError(
                                    "FOR_YOU_ACTIVATION_NOT_READY"
                                ) from None

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
                        condition="retry",
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
        on_row: Callable[[_HomeRowObservation], Awaitable[None]] | None = None,
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
            on_row=on_row,
        )
        return targets

    async def advance_feed(self, *, scan_after_scroll: bool = True) -> bool:
        """Issue a bounded native wheel gesture and measure its displacement."""
        self.last_scroll_stop_reason = None
        self.last_scroll_targets = ()
        await self._wait_for_verified_feed(
            self._readiness_deadline(_READINESS_TIMEOUT_MS),
            timeout_code="HOME_SCROLL_SURFACE_UNVERIFIED",
        )
        surface = await self._scroll_surface()
        if surface is None:
            self.last_scroll_stop_reason = "FEED_NOT_SCROLLABLE"
            return False
        try:
            try:
                point, wheel_delta, viewport_height, hit_test_tag = (
                    await self._safe_feed_scroll_point(surface)
                )
                scroll_state = await self._scroll_surface_snapshot(surface)
                initial_position = float(scroll_state["top"])
                if float(scroll_state["max"]) - initial_position <= 1:
                    self.last_scroll_stop_reason = "FEED_END_REACHED"
                    return False
                self.scroll_attempts += 1
                gesture = await self._scroll_engine.advance(
                    surface,
                    point,
                    wheel_delta,
                    viewport_height,
                )
                self.last_scroll_gesture = gesture
                self._mark_ui_transition()
            except PlaywrightError:
                if not self._detail_mode:
                    print(f"SCROLL attempt={self.scroll_attempts} observed=0")
                raise UiReadError("HOME_SCROLL_FAILED") from None
            observed_delta = gesture.observed_delta
            if observed_delta <= 1:
                if not self._detail_mode:
                    print(f"SCROLL attempt={self.scroll_attempts} observed=0")
                raise UiReadError("HOME_SCROLL_UNVERIFIED")
            if observed_delta > viewport_height * 0.5 + 1:
                raise UiReadError("HOME_SCROLL_ADVANCE_OVERSIZED")

            async def observe_scroll(window_deadline: float) -> float:
                await self._wait_for_verified_feed(
                    window_deadline,
                    timeout_code="HOME_SCROLL_UNVERIFIED",
                )

                if not scan_after_scroll:
                    return observed_delta

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
                self.last_scroll_targets = rendered.targets
                self.last_display_observations = rendered.display_observations
                self.last_viewport_coverage_complete = rendered.coverage_complete
                self.last_viewport_observed_rows = rendered.observed_rows
                self._last_viewport_candidate_roots = rendered.candidate_roots
                self._last_viewport_candidate_observation_ids = (
                    rendered.candidate_observation_ids
                )
                return observed_delta

            try:
                observed_delta = await self._recoverable_stage(
                    "HOME_SCROLL",
                    "HOME_SCROLL_UNVERIFIED",
                    observe_scroll,
                )
            except UiReadError:
                if not self._detail_mode:
                    print(f"SCROLL attempt={self.scroll_attempts} observed=0")
                raise
            self.scrolls += 1
            self.last_scroll_delta = observed_delta
            final_surface = await self._scroll_surface_snapshot(surface)
            self.last_scroll_surface_key = str(final_surface["key"])
            self.last_scroll_geometry = {
                "surface_key": self.last_scroll_surface_key,
                "surface_tag": str(final_surface["tag"]),
                "overflow_y": str(final_surface["overflowY"]),
                "hit_test_tag": hit_test_tag,
                "scroll_top_before": float(scroll_state["top"]),
                "scroll_top_after": float(final_surface["top"]),
                "scroll_height_before": float(scroll_state["scrollHeight"]),
                "scroll_height_after": float(final_surface["scrollHeight"]),
                "clip_top_before": float(scroll_state["clipTop"]),
                "clip_top_after": float(final_surface["clipTop"]),
                "clip_bottom_before": float(scroll_state["clipBottom"]),
                "clip_bottom_after": float(final_surface["clipBottom"]),
                "scroll_top_delta": (
                    float(final_surface["top"]) - float(scroll_state["top"])
                ),
                "scroll_height_delta": (
                    float(final_surface["scrollHeight"])
                    - float(scroll_state["scrollHeight"])
                ),
            }
            final_scroll_state = await surface.evaluate(
                "element => Math.max(0, element.scrollHeight - element.clientHeight - element.scrollTop)"
            )
            self.last_scroll_end_distance = float(final_scroll_state)
            self.last_scroll_near_end = self.last_scroll_end_distance <= max(
                wheel_delta * 1.5,
                viewport_height * 0.75,
            )
            delta_ratio = observed_delta / viewport_height
            delta_band = (
                "SMALL"
                if delta_ratio <= 0.5
                else "MEDIUM"
                if delta_ratio <= 0.75
                else "LARGE"
            )
            self.last_scroll_band = delta_band
            if not self._detail_mode:
                print(
                    f"SCROLL attempt={self.scroll_attempts} observed=1 "
                    f"delta_band={delta_band}"
                )
            return True
        finally:
            await surface.dispose()

    async def _scroll_surface_snapshot(
        self,
        surface: JSHandle,
    ) -> dict[str, object]:
        return await surface.evaluate(
            _with_scrollport_geometry("""element => {
                /*SCROLLPORT_GEOMETRY*/
                const bounds = scrollportBoundsFor(element);
                return {
                    key: keyFor(element),
                    tag: bounds.documentSurface ? "DOCUMENT" : element.tagName,
                    overflowY: bounds.documentSurface
                        ? "document" : getComputedStyle(element).overflowY,
                    top: bounds.documentSurface ? window.scrollY : element.scrollTop,
                    max: bounds.documentSurface
                        ? Math.max(0, element.scrollHeight - window.innerHeight)
                        : Math.max(0, element.scrollHeight - element.clientHeight),
                    scrollHeight: element.scrollHeight,
                    clipTop: bounds.top,
                    clipBottom: bounds.bottom,
                };
            }""")
        )

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
                    body ? body.childNodes.length : 0,
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
                    body ? body.childNodes.length : 0,
                    body ? body.childElementCount : 0,
                    visibleCount('main, [role="main"], h1, [role="tab"]'),
                ];
            }"""
        )
        if not dom_state[0]:
            return None
        return _RenderObservation((self._page.url, *dom_state), None)

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
        if scan.pending_signals:
            pending = _CardNotReady(signals=scan.pending_signals)
            if await self._wait_for_pending_card(
                pending,
                deadline,
                max_wait_ms=_CARD_HYDRATION_WAIT_MS,
            ):
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
            scan.candidate_observation_ids,
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
        on_row: Callable[[_HomeRowObservation], Awaitable[None]] | None = None,
    ) -> tuple[tuple[PostTarget, ...], dict[str, float]]:
        stage_name = stage or {
            "FEED_INITIAL_NOT_READY": "FEED_INITIAL",
            "FEED_RETURN_NOT_READY": "FEED_RETURN",
            "HOME_SCROLL_UNVERIFIED": "FEED_SCROLL",
        }.get(timeout_code, "FEED")

        async def attempt(deadline: float) -> _VisibleTargets:
            self.last_scan_timings = _FeedScanTimings(
                wait_reason="FOR_YOU_SHELL_NOT_READY"
            )
            await self._wait_for_verified_feed(
                deadline,
                timeout_code=timeout_code,
            )
            self.last_scan_counts = _FeedScanCounts()
            scan = await self._visible_targets(
                max_posts,
                currently_visible=currently_visible,
                prefer_new_fingerprints=prefer_new_fingerprints,
                stage=stage_name,
                on_row=on_row,
            )
            useful_observation = bool(
                scan.targets
                or self.last_scan_counts.body_extracted
                or self.last_scan_counts.deferred_offscreen
                or (
                    self.last_scan_counts.uncertain_rows
                    and not self.last_scan_counts.card_not_ready
                )
            )
            if on_row is not None and useful_observation:
                # Row callbacks are progressive observations. An expensive DOM
                # discovery must not invalidate rows already emitted against a
                # verified Home shell by consuming the readiness deadline.
                await self._wait_for_verified_feed(
                    self._readiness_deadline(_READINESS_TIMEOUT_MS),
                    timeout_code=timeout_code,
                )
                return scan
            await self._wait_for_verified_feed(
                deadline,
                timeout_code=timeout_code,
            )
            if not (
                scan.targets
                or self.last_scan_counts.body_extracted
                or self.last_scan_counts.deferred_offscreen
                or (
                    self.last_scan_counts.uncertain_rows
                    and not self.last_scan_counts.card_not_ready
                )
            ):
                pending_readiness = await self._feed_readiness_signals()
                readiness_timing = self.last_scan_timings
                should_rescan = False
                if pending_readiness is not None:
                    wait_started = time.perf_counter()
                    changed = await self._wait_for_pending_card(
                        pending_readiness,
                        deadline,
                    )
                    readiness_timing.stabilize_ms += round(
                        (time.perf_counter() - wait_started) * 1000
                    )
                    readiness_timing.wait_reason = (
                        "FEED_READINESS_SIGNAL"
                        if changed
                        else "FEED_READINESS_TIMEOUT"
                    )
                    should_rescan = changed
                if not should_rescan:
                    should_rescan = await self._visible_feed_evidence()
                if should_rescan:
                    previous_discover_ms = readiness_timing.discover_ms
                    previous_read_row_ms = readiness_timing.read_row_ms
                    previous_stabilize_ms = readiness_timing.stabilize_ms
                    previous_wait_reason = readiness_timing.wait_reason
                    self.last_scan_counts = _FeedScanCounts()
                    scan = await self._visible_targets(
                        max_posts,
                        currently_visible=currently_visible,
                        prefer_new_fingerprints=prefer_new_fingerprints,
                        stage=stage_name,
                        on_row=on_row,
                    )
                    self.last_scan_timings.discover_ms += previous_discover_ms
                    self.last_scan_timings.read_row_ms += previous_read_row_ms
                    self.last_scan_timings.stabilize_ms += previous_stabilize_ms
                    if self.last_scan_timings.wait_reason == "NONE":
                        self.last_scan_timings.wait_reason = previous_wait_reason
                if not (
                    scan.targets
                    or self.last_scan_counts.body_extracted
                    or self.last_scan_counts.deferred_offscreen
                    or (
                        self.last_scan_counts.uncertain_rows
                        and not self.last_scan_counts.card_not_ready
                    )
                ):
                    if self.last_scan_timings.wait_reason == "NONE":
                        self.last_scan_timings.wait_reason = (
                            "NO_VERIFIABLE_ROW_EVIDENCE"
                        )
                    raise UiReadError(timeout_code)
            return scan

        try:
            scan = await self._recoverable_stage(
                stage_name,
                timeout_code,
                attempt,
                recoverable_codes=(timeout_code,),
            )
        except UiReadError as error:
            if error.code in {timeout_code, "FEED_VISIBLE_UNSUPPORTED"}:
                print(f"STOP stage={stage_name} code={error.code}")
            raise
        self.last_display_observations = scan.display_observations
        self.last_viewport_coverage_complete = scan.coverage_complete
        self.last_viewport_observed_rows = scan.observed_rows
        self.last_scan_counts.coverage_complete = scan.coverage_complete
        self._last_viewport_signature = scan.signature
        self._last_viewport_candidate_roots = scan.candidate_roots
        self._last_viewport_candidate_observation_ids = (
            scan.candidate_observation_ids
        )
        self.last_viewport_deferred_only = (
            not scan.coverage_complete
            and any(
                diagnostic.state
                in {
                    "DEFERRED_OFFSCREEN",
                    "DEFERRED_TEXT_ABOVE",
                    "DEFERRED_TEXT_BELOW",
                    "PARTIAL_BODY",
                    "MEDIA_OR_WHITESPACE_GAP",
                }
                for diagnostic in self.last_candidate_diagnostics
            )
            and all(
                diagnostic.identity_fingerprint is not None
                for diagnostic in self.last_candidate_diagnostics
                if diagnostic.state
                in {
                    "DEFERRED_OFFSCREEN",
                    "DEFERRED_TEXT_ABOVE",
                    "DEFERRED_TEXT_BELOW",
                    "PARTIAL_BODY",
                    "MEDIA_OR_WHITESPACE_GAP",
                }
            )
        )
        self.last_viewport_can_wheel = bool(
            not scan.coverage_complete
            and scan.observed_rows <= self._last_viewport_limit
        )
        self.last_deferred_fingerprints = frozenset(
            diagnostic.identity_fingerprint
            for diagnostic in self.last_candidate_diagnostics
            if diagnostic.state == "DEFERRED_OFFSCREEN"
            and diagnostic.identity_fingerprint is not None
        )
        counts = self.last_scan_counts
        if counts.verified and counts.body_read and scan.coverage_complete:
            print(
                f"READY stage={stage_name} state=BODY_EXTRACTED "
                f"verified={counts.verified} observed_rows={counts.observed_rows} "
                f"body_extracted={counts.body_extracted} "
                f"viewport_coverage_complete={str(counts.coverage_complete).lower()}"
            )
        elif (
            counts.body_extracted
            and not counts.verified
            and scan.coverage_complete
        ):
            print(
                f"OBSERVED stage={stage_name} "
                "state=FEED_VISIBLE_UNVERIFIED verified=0 "
                f"identity=OBSERVED_UNVERIFIED observed_rows={counts.observed_rows} "
                f"body_extracted={counts.body_extracted} "
                f"viewport_coverage_complete={str(counts.coverage_complete).lower()}"
            )
        elif counts.observed_rows:
            print(
                f"OBSERVED stage={stage_name} state=OBSERVATION_UNCERTAIN "
                f"verified={counts.verified} observed_rows={counts.observed_rows} "
                f"uncertain_rows={counts.uncertain_rows} "
                f"viewport_coverage_complete={str(counts.coverage_complete).lower()}"
            )
        return scan.targets, scan.scroll_anchor or {}

    async def _feed_readiness_signals(self) -> _CardNotReady | None:
        signals: list[_CardWaitSignal] = []
        viewport = self._page.viewport_size
        if viewport is None:
            return None
        region = await self._content_region()

        permalinks = region.locator('a[href*="/post/"]')
        for index in range(min(await permalinks.count(), _MAX_PERMALINK_CANDIDATES)):
            permalink = permalinks.nth(index)
            if await self._is_outside_content_landmark(permalink):
                continue
            if await self._is_nested_feed_content(permalink):
                continue
            if not await permalink.is_visible():
                continue
            # A visible DOM node can still be outside the page or clipped by
            # an inner feed scroller. Never wait for an offscreen permalink.
            if await self._is_in_visible_scrollports(permalink):
                signals.append(_CardWaitSignal(permalink, "retry"))

        if not signals:
            return None
        return _CardNotReady(signals=tuple(signals))

    async def _wait_for_pending_card(
        self,
        pending: _CardNotReady,
        deadline: float,
        *,
        max_wait_ms: int | None = None,
    ) -> bool:
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            return False
        wait_ms = min(
            remaining,
            max_wait_ms or _READINESS_RECHECK_INTERVAL_MS,
        )

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
                elif signal.condition == "retry":
                    await expect(signal.locator).to_be_visible(timeout=wait_ms)
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
            if (structure.visible_times or structure.offscreen_times) and (
                structure.profile_anchor_candidates
                or structure.visible_post_links
                or structure.offscreen_profile_anchors
                or structure.offscreen_post_links
            ):
                return True
        permalinks = region.locator('a[href*="/post/"]')
        for index in range(min(await permalinks.count(), _MAX_PERMALINK_CANDIDATES)):
            permalink = permalinks.nth(index)
            if await self._is_outside_content_landmark(permalink):
                continue
            if await self._is_nested_feed_content(permalink):
                continue
            if not await self._is_in_visible_scrollports(permalink):
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
    ) -> tuple[tuple[float, float], int, float, str]:
        viewport = self._page.viewport_size
        if viewport is None:
            raise UiReadError("FEED_VIEWPORT_UNVERIFIED")
        box = await surface.evaluate(
            _with_scrollport_geometry("""element => {
                /*SCROLLPORT_GEOMETRY*/
                const bounds = scrollportBoundsFor(element);
                return {
                    x: bounds.left, y: bounds.top,
                    width: bounds.right - bounds.left,
                    height: bounds.bottom - bounds.top,
                };
            }""")
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
                        const nestedScroller = (() => {
                            for (let current = target; current && current !== surface;
                                 current = current.parentElement) {
                                const style = getComputedStyle(current);
                                if (current.scrollHeight > current.clientHeight + 1 &&
                                    ["auto", "scroll", "overlay"].includes(
                                        style.overflowY
                                    )) return true;
                            }
                            return false;
                        })();
                        return !excluded && !control && !nestedScroller;
                    }""",
                    {"x": x, "y": y},
                )
                if safe:
                    hit_type = await surface.evaluate(
                        """(surface, point) => {
                            const target = document.elementFromPoint(point.x, point.y);
                            return target?.tagName ?? "UNKNOWN";
                        }""",
                        {"x": x, "y": y},
                    )
                    return (x, y), advance, visible_height, str(hit_type)
        raise UiReadError("HOME_SCROLL_SURFACE_UNVERIFIED")

    async def _scroll_surface(self) -> JSHandle | None:
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
            surface_state = await self._page.evaluate_handle(
                _with_scrollport_geometry("""([region, rows, maxCandidates]) => {
                /*SCROLLPORT_GEOMETRY*/
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
                        rect.width > 0 && rect.height > 0 &&
                        geometryFor(item).intersects;
                };
                const postPathHint = item => {
                    const href = item.getAttribute("href");
                    if (!href) return false;
                    try {
                        return new URL(href, location.href).pathname
                            .split("/").includes("post");
                    } catch { return false; }
                };
                let items = rows;
                if (items.length && !items.every(visibleItem)) return null;
                if (!items.length) {
                    items = Array.from(region.querySelectorAll("a"))
                        .filter(postPathHint).slice(0, maxCandidates)
                        .filter(visibleItem);
                }
                if (!items.length) return {state: "NOT_SCROLLABLE", surface: null};

                const candidates = new Set();
                for (const item of items) {
                    for (let element = item; element;
                         element = element.parentElement) {
                        if ((element === root &&
                             root.scrollHeight > root.clientHeight + 1) ||
                            isScrollable(element)) {
                            candidates.add(element);
                        }
                    }
                }
                const innermost = Array.from(candidates).filter(candidate =>
                    items.every(item => candidate === item || candidate.contains(item)) &&
                    !Array.from(candidates).some(other => candidate !== other &&
                        candidate.contains(other) &&
                        items.every(item => other === item || other.contains(item)))
                );
                if (!candidates.size) {
                    return {state: "NOT_SCROLLABLE", surface: null};
                }
                if (innermost.length !== 1) {
                    return {state: "AMBIGUOUS", surface: null};
                }
                const surface = innermost[0];
                if (!items.every(item => surface === item || surface.contains(item))) {
                    return {state: "AMBIGUOUS", surface: null};
                }
                return {
                    state: "READY", surface, surfaceKey: keyFor(surface),
                    surfaceTag: surface === root ? "DOCUMENT" : surface.tagName,
                    overflowY: surface === root
                        ? "document" : getComputedStyle(surface).overflowY,
                };
                }"""),
                arg=[region_handle, row_handles, _MAX_PERMALINK_CANDIDATES],
            )
        finally:
            await region_handle.dispose()
            for handle in row_handles:
                await handle.dispose()
        state_handle = await surface_state.get_property("state")
        state = await state_handle.json_value()
        await state_handle.dispose()
        if state == "NOT_SCROLLABLE":
            await surface_state.dispose()
            self.last_scan_counts.scroll_surface_status = "NOT_SCROLLABLE"
            return None
        if state != "READY":
            await surface_state.dispose()
            self.last_scan_counts.scroll_surface_status = "UNVERIFIED"
            raise UiReadError("HOME_SCROLL_SURFACE_UNVERIFIED")
        key_handle = await surface_state.get_property("surfaceKey")
        key_value = await key_handle.json_value()
        await key_handle.dispose()
        self.last_scroll_surface_key = str(key_value)
        surface = await surface_state.get_property("surface")
        await surface_state.dispose()
        self.last_scan_counts.scroll_surface_status = "VALIDATED"
        return surface

    async def _is_carried_viewport_candidate(self, root: Locator) -> str | None:
        handles = self._carried_viewport_candidate_handles
        if not handles:
            return None
        matched_index = await root.evaluate(
            "(node, previous) => previous.findIndex(item => node === item)",
            list(handles),
        )
        if matched_index < 0:
            return None
        if matched_index >= len(self._carried_viewport_candidate_observation_ids):
            return None
        return self._carried_viewport_candidate_observation_ids[matched_index]

    async def _visible_targets(
        self,
        max_posts: int,
        *,
        currently_visible: bool = False,
        prefer_new_fingerprints: set[str] | None = None,
        stage: str = "FEED",
        on_row: Callable[[_HomeRowObservation], Awaitable[None]] | None = None,
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
        counts.url_timestamp_links = 0
        counts.url_secondary_links = 0
        counts.url_canonical_links = 0
        counts.url_rejected_links = 0
        counts.card_not_ready = 0
        counts.deferred_offscreen = 0
        counts.identity_verified = 0
        counts.new_verified_rows = 0
        counts.partial_body_rows = 0
        counts.deferred_identity_rows = 0
        counts.new_visible_text_segments = 0
        counts.deferred_above = 0
        counts.deferred_below = 0
        counts.nearest_deferred_below_px = None
        counts.frontier_content_px = None
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
        counts.url_structure_counts.clear()
        counts.unsupported_roots = 0
        counts.roots_truncated = False
        counts.region_status = "UNSET"
        counts.scroll_surface_status = "NOT_CHECKED"
        counts.structural_reasons.clear()
        self.last_candidate_diagnostics = ()
        self.last_display_observations = ()
        self.last_scan_timings = _FeedScanTimings()
        self.last_viewport_can_wheel = False
        discover_started = time.perf_counter()
        region = await self._content_region()
        roots, roots_truncated = await self._top_level_candidate_roots(region, viewport)
        counts.roots_truncated = roots_truncated
        structures = await self._collect_structure_diagnostics(
            region,
            roots,
            viewport,
        )
        self.last_scan_timings.discover_ms = round(
            (time.perf_counter() - discover_started) * 1000
        )

        async def emit_row(
            diagnostic: _CandidateDiagnostic,
            *,
            target: PostTarget | None = None,
            display: DisplayObservation | None = None,
            fingerprint: str | None = None,
            candidate: bool = True,
        ) -> None:
            if on_row is not None:
                await on_row(
                    _HomeRowObservation(
                        diagnostic,
                        target,
                        display,
                        fingerprint or diagnostic.identity_fingerprint,
                        candidate,
                    )
                )

        candidate_rows: list[
            tuple[int, Locator, _CandidateStructure, bool, _CandidateDiagnostic, str]
        ] = []
        diagnostics: list[_CandidateDiagnostic] = []
        for index, root in enumerate(roots):
            structure = structures[index]
            root_kind = structure.root_kind
            diagnostic = _CandidateDiagnostic(
                row_index=index + 1,
                root_kind=root_kind,
                has_visible_time=structure.visible_times > 0,
                has_post_permalink=structure.visible_post_links > 0,
                has_profile_author=structure.profile_anchor_candidates > 0,
                has_readable_body=structure.readable_body_candidates > 0,
            )
            diagnostics.append(diagnostic)
            has_row_cues = (structure.visible_times or structure.offscreen_times) and (
                structure.profile_anchor_candidates
                or structure.visible_post_links
                or structure.body_candidates
                or structure.offscreen_post_links
                or structure.offscreen_profile_anchors
                or structure.offscreen_body_candidates
            )
            semantic_evidence = (
                (structure.body_candidates or structure.offscreen_body_candidates)
                and (
                    structure.visible_times
                    or structure.offscreen_times
                    or structure.profile_anchor_candidates
                    or structure.visible_post_links
                    or structure.offscreen_profile_anchors
                    or structure.offscreen_post_links
                )
            )
            semantic_row = semantic_evidence and (
                root_kind != "PRESSABLE"
                or structure.visible_times
                or structure.visible_post_links
            ) and root_kind in {"ARTICLE", "ROLE_ARTICLE", "PRESSABLE"}
            carried_observation_id = await self._is_carried_viewport_candidate(root)
            carried_row = carried_observation_id is not None
            if has_row_cues or semantic_row or carried_row:
                diagnostic.state = "PENDING"
                diagnostic.reason = "POST_ROW_REQUIRES_VERIFICATION"
                diagnostic.observation_id = (
                    carried_observation_id or secrets.token_hex(8)
                )
                candidate_rows.append(
                    (
                        index,
                        root,
                        structure,
                        carried_row,
                        diagnostic,
                        diagnostic.observation_id,
                    )
                )
        for handle in self._carried_viewport_candidate_handles:
            await handle.dispose()
        self._carried_viewport_candidate_handles = ()
        self.last_candidate_diagnostics = tuple(diagnostics)
        counts.observed_rows = len(candidate_rows)
        counts.coverage_complete = (
            not roots_truncated
            and len(candidate_rows) <= max_posts
            and not any(
                structure.visible_times > 1
                for _, _, structure, _, _, _ in candidate_rows
            )
        )
        targets: list[PostTarget] = []
        scroll_anchor: dict[str, float] | None = None
        display_observations: list[DisplayObservation] = []
        pending_signals: list[_CardWaitSignal] = []
        scanned_fingerprints: set[str] = set()
        candidate_by_index = {
            index: (root, structure, carried_row, diagnostic, observation_id)
            for index, root, structure, carried_row, diagnostic, observation_id
            in candidate_rows
        }
        processed_candidates = 0
        for row_index, diagnostic in enumerate(diagnostics):
            candidate = candidate_by_index.get(row_index)
            if candidate is None:
                await emit_row(diagnostic, candidate=False)
                continue
            if processed_candidates >= max_posts:
                diagnostic.state = "REJECTED"
                diagnostic.reason = "POST_SCAN_CAP_REACHED"
                counts.skipped_rows += 1
                await emit_row(diagnostic)
                continue
            processed_candidates += 1
            root, structure, carried_row, diagnostic, _ = candidate
            box = await root.bounding_box()
            if box is None:
                counts.skipped_rows += 1
                counts.uncertain_rows += 1
                diagnostic.state = "REJECTED"
                diagnostic.reason = "POST_ROOT_NOT_VISIBLE"
                display_observations.append(
                    DisplayObservation(None, "OBSERVATION_UNCERTAIN")
                )
                await emit_row(
                    diagnostic,
                    display=display_observations[-1],
                )
                continue
            counts.inspected += 1
            if scroll_anchor is None:
                scroll_anchor = box
            if not (
                structure.profile_anchor_candidates
                or structure.visible_post_links
                or structure.offscreen_post_links
                or structure.offscreen_profile_anchors
            ) and not carried_row:
                counts.skipped_rows += 1
                counts.uncertain_rows += 1
                diagnostic.state = "REJECTED"
                diagnostic.reason = "DISPLAY_ROW_CUES_INSUFFICIENT"
                self._record_structure_reason("DISPLAY_ROW_CUES_INSUFFICIENT")
                display_observations.append(
                    DisplayObservation(None, "OBSERVATION_UNCERTAIN", box["y"])
                )
                await emit_row(diagnostic, display=display_observations[-1])
                continue
            if (
                structure.visible_times > 1
            ):
                counts.skipped_rows += 1
                counts.uncertain_rows += 1
                diagnostic.state = "REJECTED"
                diagnostic.reason = "CANDIDATE_ROW_AMBIGUOUS"
                self._record_structure_reason("CANDIDATE_ROW_AMBIGUOUS")
                display_observations.append(
                    DisplayObservation(None, "OBSERVATION_UNCERTAIN", box["y"])
                )
                await emit_row(diagnostic, display=display_observations[-1])
                continue
            read_started = time.perf_counter()
            try:
                if currently_visible:
                    stabilize_started = time.perf_counter()
                    target, body_stable = await self._read_stable_visible_card(
                        root,
                        viewport_y=box["y"],
                        cached_body_fingerprints=prefer_new_fingerprints,
                    )
                    self.last_scan_timings.stabilize_ms += round(
                        (time.perf_counter() - stabilize_started) * 1000
                    )
                    if not body_stable:
                        diagnostic.state = "OBSERVED_UNVERIFIED"
                        diagnostic.reason = "ROW_CONTENT_CHANGED_DURING_READ"
                        diagnostic.identity_fingerprint = target.fingerprint
                        diagnostic.read_evidence = target.read_evidence
                        diagnostic.has_readable_body = bool(
                            target.body and target.body.strip()
                        )
                        counts.uncertain_rows += 1
                        counts.skipped_rows += 1
                        if diagnostic.has_readable_body:
                            counts.body_extracted += 1
                            display_observations.append(
                                DisplayObservation(
                                    target.body,
                                    viewport_y=box["y"],
                                    read_evidence=target.read_evidence,
                                )
                            )
                        else:
                            counts.body_unknown += 1
                            display_observations.append(
                                DisplayObservation(
                                    None,
                                    "OBSERVATION_UNCERTAIN",
                                    box["y"],
                                    read_evidence=target.read_evidence,
                                )
                            )
                        await emit_row(
                            diagnostic,
                            display=display_observations[-1],
                            fingerprint=target.fingerprint,
                        )
                        continue
                else:
                    target = await self._read_card(
                        root,
                        currently_visible=False,
                        viewport_y=box["y"],
                        cached_body_fingerprints=prefer_new_fingerprints,
                    )
            except _CardDeferredOffscreen as deferred:
                self.last_scan_timings.read_row_ms += round(
                    (time.perf_counter() - read_started) * 1000
                )
                counts.deferred_offscreen += 1
                counts.skipped_rows += 1
                self.last_scan_timings.wait_reason = "DEFERRED_OFFSCREEN"
                diagnostic.state = "DEFERRED_OFFSCREEN"
                diagnostic.reason = deferred.reason
                diagnostic.identity_fingerprint = deferred.fingerprint
                await emit_row(diagnostic, fingerprint=deferred.fingerprint)
                continue
            except _CardNotReady as pending:
                self.last_scan_timings.read_row_ms += round(
                    (time.perf_counter() - read_started) * 1000
                )
                counts.card_not_ready += 1
                body, body_signals, read_evidence = await self._observe_display_body(
                    root
                )
                diagnostic.read_evidence = read_evidence
                diagnostic.has_readable_body = bool(body)
                should_wait_for_permalink = (
                    pending.reason_code == "POST_PERMALINK_PENDING"
                    and diagnostic.root_kind
                    in {"ARTICLE", "ROLE_ARTICLE", "PRESSABLE"}
                )
                should_wait_for_missing_author = (
                    pending.reason_code == "POST_AUTHOR_PENDING"
                )
                signals = (
                    pending.signals
                    if should_wait_for_permalink or should_wait_for_missing_author
                    else body_signals
                )
                if signals:
                    pending_signals.extend(signals)
                signal_is_visible = False
                for signal in signals:
                    if await self._is_in_visible_scrollports(signal.locator):
                        signal_is_visible = True
                        break
                    if (
                        await signal.locator.count() == 0
                        and await self._is_in_visible_scrollports(root)
                    ):
                        signal_is_visible = True
                        break
                if signals and not signal_is_visible:
                    self.last_scan_timings.wait_reason = "DEFERRED_IDENTITY"
                    if should_wait_for_permalink or should_wait_for_missing_author:
                        diagnostic.state = "DEFERRED_IDENTITY"
                        diagnostic.reason = "READINESS_SIGNAL_OUTSIDE_VISIBLE_SCROLLPORT"
                    else:
                        diagnostic.state, diagnostic.reason = _body_progress_state(
                            read_evidence,
                            has_readable_body=bool(body),
                        )
                    counts.deferred_offscreen += 1
                    counts.skipped_rows += 1
                    display = DisplayObservation(
                        body,
                        "OBSERVATION_UNCERTAIN" if body is None else "OBSERVED_UNVERIFIED",
                        box["y"],
                        read_evidence=read_evidence,
                    )
                    if body:
                        counts.body_extracted += 1
                    else:
                        counts.body_unknown += 1
                    display_observations.append(display)
                    await emit_row(diagnostic, display=display)
                    continue
                if should_wait_for_permalink or should_wait_for_missing_author:
                    diagnostic.state = "PENDING_HYDRATION"
                    diagnostic.reason = pending.reason_code
                    counts.skipped_rows += 1
                    counts.uncertain_rows += 1
                    counts.body_unknown += int(not body)
                    if body:
                        counts.body_extracted += 1
                elif body:
                    diagnostic.state = "OBSERVED_UNVERIFIED"
                    diagnostic.reason = pending.reason_code
                    counts.body_extracted += 1
                elif body_signals:
                    diagnostic.state = "PENDING_HYDRATION"
                    diagnostic.reason = "POST_BODY_PENDING"
                    counts.body_unknown += 1
                    counts.skipped_rows += 1
                    counts.uncertain_rows += 1
                else:
                    diagnostic.state, diagnostic.reason = _body_progress_state(
                        read_evidence,
                        has_readable_body=False,
                    )
                    counts.body_unknown += 1
                    counts.skipped_rows += 1
                    counts.uncertain_rows += 1
                if body:
                    display_observations.append(
                        DisplayObservation(
                            body,
                            viewport_y=box["y"],
                            read_evidence=read_evidence,
                        )
                    )
                else:
                    display_observations.append(
                        DisplayObservation(
                            None,
                            "OBSERVATION_UNCERTAIN",
                            box["y"],
                            read_evidence=read_evidence,
                        )
                    )
                wait_started = time.perf_counter()
                if signal_is_visible:
                    self.last_scan_timings.wait_reason = "CARD_HYDRATION"
                    changed = await self._wait_for_pending_card(
                        _CardNotReady(signals=tuple(signals)),
                        self._readiness_deadline(_CARD_HYDRATION_WAIT_MS),
                        max_wait_ms=_CARD_HYDRATION_WAIT_MS,
                    )
                    self.last_scan_timings.stabilize_ms += round(
                        (time.perf_counter() - wait_started) * 1000
                    )
                    if not changed:
                        self.last_scan_timings.wait_reason = "CARD_HYDRATION_TIMEOUT"
                    if changed:
                        self.last_scan_timings.wait_reason = "CARD_HYDRATION"
                        if currently_visible:
                            quiet_started = time.perf_counter()
                            body_quiet = await self._wait_for_stable_card_dom(root)
                            self.last_scan_timings.stabilize_ms += round(
                                (time.perf_counter() - quiet_started) * 1000
                            )
                            if not body_quiet:
                                self.last_scan_timings.wait_reason = (
                                    "CARD_BODY_NOT_STABLE"
                                )
                                await emit_row(
                                    diagnostic,
                                    display=display_observations[-1]
                                    if display_observations
                                    else None,
                                )
                                continue
                        try:
                            if currently_visible:
                                target, body_stable = (
                                    await self._read_stable_visible_card(
                                        root,
                                        viewport_y=box["y"],
                                        cached_body_fingerprints=(
                                            prefer_new_fingerprints
                                        ),
                                    )
                                )
                                if not body_stable:
                                    self.last_scan_timings.wait_reason = (
                                        "CARD_BODY_NOT_STABLE"
                                    )
                                    await emit_row(
                                        diagnostic,
                                        display=display_observations[-1]
                                        if display_observations
                                        else None,
                                    )
                                    continue
                            else:
                                target = await self._read_card(
                                    root,
                                    currently_visible=False,
                                    viewport_y=box["y"],
                                    cached_body_fingerprints=prefer_new_fingerprints,
                                )
                        except (_CardNotReady, _UnverifiedFeedCandidate, _CardDeferredOffscreen):
                            pass
                        else:
                            counts.skipped_rows = max(0, counts.skipped_rows - 1)
                            counts.uncertain_rows = max(0, counts.uncertain_rows - 1)
                            if body:
                                counts.body_extracted = max(
                                    0,
                                    counts.body_extracted - 1,
                                )
                            else:
                                counts.body_unknown = max(0, counts.body_unknown - 1)
                            if display_observations:
                                display_observations.pop()
                            diagnostic.identity_fingerprint = target.fingerprint
                            diagnostic.read_evidence = target.read_evidence
                            diagnostic.has_readable_body = bool(
                                target.body and target.body.strip()
                            )
                            if target.body_complete:
                                diagnostic.state = "VERIFIED"
                                diagnostic.reason = "PROVENANCE_VERIFIED"
                            else:
                                diagnostic.state, diagnostic.reason = (
                                    _body_progress_state(
                                        target.read_evidence,
                                        has_readable_body=diagnostic.has_readable_body,
                                    )
                                )
                            counts.identity_verified += 1
                            counts.verified += int(target.body_complete)
                            counts.partial_body_rows += int(not target.body_complete)
                            counts.body_read += int(
                                diagnostic.has_readable_body and target.body_complete
                            )
                            counts.body_extracted += int(diagnostic.has_readable_body)
                            if not diagnostic.has_readable_body:
                                counts.body_unknown += 1
                                counts.uncertain_rows += 1
                                counts.skipped_rows += 1
                                if diagnostic.state == "UNSUPPORTED_STRUCTURE":
                                    self._record_structure_reason(
                                        "POST_BODY_STRUCTURE_UNSUPPORTED"
                                    )
                                elif diagnostic.state == "DEFERRED_TEXT_ABOVE":
                                    counts.deferred_above += 1
                                elif diagnostic.state == "DEFERRED_TEXT_BELOW":
                                    counts.deferred_below += 1
                            elif target.body_complete:
                                targets.append(target)
                            await emit_row(diagnostic, target=target)
                            continue
                await emit_row(
                    diagnostic,
                    display=display_observations[-1],
                )
                continue
            except _UnverifiedFeedCandidate as error:
                self.last_scan_timings.read_row_ms += round(
                    (time.perf_counter() - read_started) * 1000
                )
                self._record_scan_skip(stage, error)
                if error.diagnostic_reason == "CANDIDATE_WRAPPER_AMBIGUOUS":
                    body, body_signals, read_evidence = await self._observe_display_body(
                        root
                    )
                    diagnostic.read_evidence = read_evidence
                    diagnostic.has_readable_body = bool(body)
                    if body:
                        diagnostic.state = "OBSERVED_UNVERIFIED"
                        diagnostic.reason = error.diagnostic_reason
                        counts.body_extracted += 1
                    else:
                        diagnostic.state, diagnostic.reason = _body_progress_state(
                            read_evidence,
                            has_readable_body=False,
                        )
                        if body_signals:
                            pending_signals.extend(body_signals)
                        counts.body_unknown += 1
                        counts.skipped_rows += 1
                        counts.uncertain_rows += 1
                    display = DisplayObservation(
                        body,
                        "OBSERVATION_UNCERTAIN" if body is None else "OBSERVED_UNVERIFIED",
                        box["y"],
                        read_evidence=read_evidence,
                    )
                    display_observations.append(display)
                    await emit_row(diagnostic, display=display)
                    continue
                body, body_signals, read_evidence = await self._observe_display_body(
                    root
                )
                diagnostic.read_evidence = read_evidence
                diagnostic.has_readable_body = bool(body)
                if body:
                    counts.body_extracted += 1
                    diagnostic.state = "OBSERVED_UNVERIFIED"
                    diagnostic.reason = error.diagnostic_reason or error.reason
                    display_observations.append(
                        DisplayObservation(
                            body,
                            viewport_y=box["y"],
                            read_evidence=read_evidence,
                        )
                    )
                else:
                    counts.body_unknown += 1
                    counts.skipped_rows += 1
                    counts.uncertain_rows += 1
                    if body_signals:
                        diagnostic.state = "PENDING_HYDRATION"
                        diagnostic.reason = "POST_BODY_PENDING"
                    else:
                        diagnostic.state, diagnostic.reason = _body_progress_state(
                            read_evidence,
                            has_readable_body=False,
                        )
                        if diagnostic.state == "PENDING_HYDRATION":
                            diagnostic.state = "REJECTED"
                            diagnostic.reason = (
                                error.diagnostic_reason or error.reason
                            )
                    pending_signals.extend(body_signals)
                    display_observations.append(
                        DisplayObservation(
                            None,
                            "OBSERVATION_UNCERTAIN",
                            box["y"],
                            read_evidence=read_evidence,
                        )
                    )
                await emit_row(
                    diagnostic,
                    display=display_observations[-1],
                )
                continue
            self.last_scan_timings.read_row_ms += round(
                (time.perf_counter() - read_started) * 1000
            )
            diagnostic.identity_fingerprint = target.fingerprint
            diagnostic.read_evidence = target.read_evidence
            diagnostic.state = "VERIFIED" if target.body_complete else "PARTIAL_BODY"
            diagnostic.reason = (
                "PROVENANCE_VERIFIED"
                if target.body_complete
                else "VISIBLE_TEXT_PROGRESS"
            )
            counts.identity_verified += 1
            counts.verified += int(target.body_complete)
            counts.partial_body_rows += int(not target.body_complete)
            diagnostic.has_readable_body = bool(target.body and target.body.strip())
            if target.body and target.body.strip():
                body_was_cached = bool(
                    prefer_new_fingerprints is not None
                    and target.fingerprint in prefer_new_fingerprints
                    and target.fingerprint in self._cached_home_bodies
                )
                counts.body_read += int(not body_was_cached and target.body_complete)
                counts.body_extracted += 1
                if target.body_complete:
                    self._cached_home_bodies.setdefault(
                        target.fingerprint,
                        (target.body, target.body_truncated),
                    )
            else:
                counts.body_unknown += 1
                diagnostic.state, diagnostic.reason = _body_progress_state(
                    target.read_evidence,
                    has_readable_body=False,
                )
                if diagnostic.state == "DEFERRED_TEXT_BELOW":
                    counts.deferred_below += 1
                elif diagnostic.state == "DEFERRED_TEXT_ABOVE":
                    counts.deferred_above += 1
                elif diagnostic.state == "UNSUPPORTED_STRUCTURE":
                    self._record_structure_reason(
                        "POST_BODY_STRUCTURE_UNSUPPORTED"
                    )
                elif diagnostic.state == "PENDING_HYDRATION":
                    _, body_signals, _ = await self._observe_display_body(root)
                    pending_signals.extend(body_signals)
                counts.skipped_rows += 1
                counts.uncertain_rows += 1
                diagnostic.identity_fingerprint = target.fingerprint
                await emit_row(
                    diagnostic,
                    target=target,
                    fingerprint=target.fingerprint,
                )
                continue
            if not target.body_complete:
                counts.skipped_rows += 1
                counts.coverage_complete = False
                await emit_row(
                    diagnostic,
                    target=target,
                    fingerprint=target.fingerprint,
                )
                continue
            if (
                target.fingerprint in scanned_fingerprints
                or (
                    prefer_new_fingerprints is not None
                    and target.fingerprint in prefer_new_fingerprints
                )
            ):
                self.feed_duplicates += 1
                await emit_row(
                    diagnostic,
                    target=target,
                    fingerprint=target.fingerprint,
                )
                continue
            scanned_fingerprints.add(target.fingerprint)
            targets.append(target)
            await emit_row(
                diagnostic,
                target=target,
                fingerprint=target.fingerprint,
            )
        if not candidate_rows:
            permalink_targets, permalink_anchor = await self._visible_permalink_targets(
                max_posts,
                currently_visible=currently_visible,
                prefer_new_fingerprints=prefer_new_fingerprints,
                stage=stage,
                on_row=on_row,
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
        if (
            counts.body_unknown
            or counts.uncertain_rows
            or counts.deferred_offscreen
            or counts.partial_body_rows
            or any(
                diagnostic.state != "VERIFIED"
                for diagnostic in diagnostics
                if diagnostic.state != "NON_POST"
            )
        ):
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
            tuple(root for _, root, _, _, _, _ in candidate_rows[:max_posts]),
            tuple(pending_signals),
            tuple(
                observation_id
                for _, _, _, _, _, observation_id in candidate_rows[:max_posts]
            ),
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

    async def _is_in_visible_scrollports(self, element: Locator) -> bool:
        try:
            if await element.count() == 0:
                return False
            return bool(
                await element.evaluate(
                    _with_scrollport_geometry("""node => {
                        /*SCROLLPORT_GEOMETRY*/
                        if (!node.isConnected) return false;
                        for (let current = node; current;
                             current = current.parentElement) {
                            const style = getComputedStyle(current);
                            if (style.display === "none" ||
                                style.visibility === "hidden" ||
                                Number(style.opacity) === 0) return false;
                        }
                        return geometryFor(node).intersects;
                    }""")
                )
            )
        except PlaywrightError:
            return False

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
        candidate_keys: set[str] = set()
        self._carried_viewport_candidate_roots = ()
        self._carried_viewport_candidate_observation_ids = ()
        self._carried_viewport_candidate_handles = ()
        carried_roots: list[Locator] = []
        carried_observation_ids: list[str] = []
        truncated = False

        async def add_candidate(
            root: Locator,
            box: dict[str, float],
            key: str | None = None,
        ) -> None:
            if key is None:
                key = await root.evaluate(
                    """node => {
                        const path = [];
                        for (let current = node; current;
                             current = current.parentElement) {
                            const parent = current.parentElement;
                            path.push(parent ? Array.prototype.indexOf.call(
                                parent.children, current
                            ) : 0);
                        }
                        return path.reverse().join("/");
                    }"""
                )
            if key in candidate_keys:
                return
            candidate_keys.add(key)
            candidates.append((box["y"], root))

        selectors = (
            'article, [role="article"]:not(article)',
            '[data-pressable-container="true"]:not(article):not([role="article"])',
        )
        async def inspect_semantic_root(root: Locator) -> dict[str, float] | None:
            if not await root.is_visible():
                return None
            if (
                await self._is_outside_content_landmark(root)
                or await self._is_inside_quote(root)
            ):
                return None
            box = await root.bounding_box()
            if (
                box is None
                or not self._intersects_viewport(box, viewport)
                or not await self._is_in_visible_scrollports(root)
            ):
                return None
            if await self._is_nested_feed_root(root):
                return None
            return box

        for selector in selectors:
            roots = region.locator(selector)
            available = await roots.count()
            count = min(available, _MAX_FEED_ROOTS_PER_TYPE)
            truncated = truncated or available > count
            selected_roots = [roots.nth(index) for index in range(count)]
            boxes = await asyncio.gather(
                *(inspect_semantic_root(root) for root in selected_roots)
            )
            eligible_roots = [
                (root, box)
                for root, box in zip(selected_roots, boxes, strict=True)
                if box is not None
            ]
            identity_keys = await asyncio.gather(
                *(
                    root.evaluate(
                        """node => {
                            const path = [];
                            for (let current = node; current;
                                 current = current.parentElement) {
                                const parent = current.parentElement;
                                path.push(parent ? Array.prototype.indexOf.call(
                                    parent.children, current
                                ) : 0);
                            }
                            return path.reverse().join("/");
                        }"""
                    )
                    for root, _ in eligible_roots
                )
            )
            for (root_box, identity) in zip(
                eligible_roots,
                identity_keys,
                strict=True,
            ):
                root, box = root_box
                await add_candidate(root, box, str(identity))

        region_handle = await region.element_handle()
        if region_handle is not None:
            try:
                async def inspect_carried_root(
                    root: Locator,
                ) -> dict[str, object] | None:
                    if not await self._is_in_visible_scrollports(root):
                        return None
                    return await root.evaluate(
                        """(node, args) => {
                            const style = getComputedStyle(node);
                            const rect = node.getBoundingClientRect();
                            if (style.display === "none" ||
                                style.visibility === "hidden" ||
                                rect.width <= 0 || rect.height <= 0 ||
                                node.closest(args.outside) ||
                                node.closest(args.quote) ||
                                !args.region.contains(node)) return null;
                            const path = [];
                            for (let current = node; current;
                                 current = current.parentElement) {
                                const parent = current.parentElement;
                                path.push(parent ? Array.prototype.indexOf.call(
                                    parent.children, current
                                ) : 0);
                            }
                            return {
                                y: rect.top,
                                key: path.reverse().join("/"),
                            };
                        }""",
                        {
                            "viewport": viewport,
                            "region": region_handle,
                            "outside": (
                                "nav, aside, footer, form, [contenteditable='true'], "
                                "[role='navigation'], [role='menu'], "
                                "[role='complementary'], [role='dialog'], "
                                "[role='search'], [role='banner'], "
                                "[role='contentinfo'], [role='textbox']"
                            ),
                            "quote": _QUOTE_SELECTOR,
                        },
                    )

                carried_boxes = await asyncio.gather(
                    *(
                        inspect_carried_root(root)
                        for root in self._last_viewport_candidate_roots
                    )
                )
                for index, (root, box) in enumerate(
                    zip(self._last_viewport_candidate_roots, carried_boxes, strict=True)
                ):
                    if box is None:
                        continue
                    carried_roots.append(root)
                    carried_observation_ids.append(
                        self._last_viewport_candidate_observation_ids[index]
                        if index < len(self._last_viewport_candidate_observation_ids)
                        else secrets.token_hex(8)
                    )
                    await add_candidate(
                        root,
                        {"y": float(box["y"])},
                        str(box["key"]),
                    )
            finally:
                await region_handle.dispose()
        self._carried_viewport_candidate_roots = tuple(carried_roots)
        self._carried_viewport_candidate_observation_ids = tuple(
            carried_observation_ids
        )
        carried_handles = await asyncio.gather(
            *(root.element_handle() for root in carried_roots)
        )
        retained_carried = [
            (handle, observation_id)
            for handle, observation_id in zip(
                carried_handles,
                carried_observation_ids,
                strict=True,
            )
            if handle is not None
        ]
        self._carried_viewport_candidate_handles = tuple(
            handle for handle, _ in retained_carried
        )
        self._carried_viewport_candidate_observation_ids = tuple(
            observation_id for _, observation_id in retained_carried
        )

        # Some pages expose independent post rows only as ordinary containers.
        # Infer a root from the nearest visible ancestor that owns exactly one
        # timestamp and one profile-author link; no post permalink is required.
        times = region.locator("time")
        time_count = await times.count()
        if time_count > _MAX_STRUCTURE_ELEMENTS:
            truncated = True
        fallbacks = await times.evaluate_all(
            _with_scrollport_geometry("""(timeNodes, args) => {
                /*SCROLLPORT_GEOMETRY*/
                const visible = element => {
                    const style = getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    if (style.display === "none" || style.visibility === "hidden" ||
                        rect.width <= 0 || rect.height <= 0) return false;
                    return geometryFor(element).intersects;
                };
                const outside = element => element.closest(args.outside) !== null;
                const insideQuote = element => element.closest(args.quote) !== null;
                const profileLink = element => {
                    const href = element.getAttribute("href");
                    if (!href) return false;
                    try {
                        const url = new URL(href, location.href);
                        return url.protocol === "https:" &&
                            url.hostname.toLowerCase() === "www.threads.com" &&
                            !url.search && !url.hash &&
                            /^[/]@[A-Za-z0-9._]{1,30}[/]?$/.test(url.pathname);
                    } catch { return false; }
                };
                const postPathHint = element => {
                    const href = element.getAttribute("href");
                    if (!href) return false;
                    try {
                        return new URL(href, location.href).pathname
                            .split("/").includes("post");
                    } catch { return false; }
                };
                const identityPath = node => {
                    const path = [];
                    for (let current = node; current; current = current.parentElement) {
                        const parent = current.parentElement;
                        path.push(parent ? Array.prototype.indexOf.call(
                            parent.children, current
                        ) : 0);
                    }
                    return path.reverse().join("/");
                };
                const seen = new Set();
                const results = [];
                const limit = Math.min(timeNodes.length, args.maxTimes);
                for (let index = 0; index < limit; index++) {
                    const time = timeNodes[index];
                    if (outside(time) || insideQuote(time) || !visible(time) ||
                        time.parentElement?.closest(args.roots)) continue;
                    let ancestor = time;
                    let fallback = null;
                    for (let depth = 1; depth <= args.maxAncestors; depth++) {
                        ancestor = ancestor.parentElement;
                        if (!ancestor) break;
                        const tag = ancestor.tagName.toLowerCase();
                        const role = ancestor.getAttribute("role");
                        if (["body", "html", "main"].includes(tag) ||
                            role === "main" || role === "feed") break;
                        if (outside(ancestor) || insideQuote(ancestor)) break;
                        if (ancestor.matches(args.roots)) break;
                        if (!visible(ancestor)) continue;
                        const belongs = element => {
                            const nearest = element.parentElement?.closest(args.roots);
                            return ancestor.contains(element) && !nearest;
                        };
                        const times = Array.from(ancestor.querySelectorAll("time"))
                            .filter(element => belongs(element) && !outside(element) &&
                                !insideQuote(element) && visible(element));
                        const links = Array.from(ancestor.querySelectorAll("a"))
                            .filter(element => belongs(element) && !outside(element) &&
                                visible(element));
                        const profileCount = links.filter(profileLink).length;
                        const postCount = links.filter(postPathHint).length;
                        const bodies = Array.from(
                            ancestor.querySelectorAll(args.body)
                        ).filter(element => {
                            if (!belongs(element) || !visible(element)) return false;
                            let current = element;
                            while (current && current !== ancestor) {
                                if (current.matches(args.bodyExcluded)) return false;
                                current = current.parentElement;
                            }
                            return current === ancestor &&
                                !element.querySelector(
                                    `${args.bodyExcluded}, ${args.roots}`
                                );
                        });
                        const box = ancestor.getBoundingClientRect();
                        const evidence = profileCount || postCount || bodies.length;
                        if (times.length === 1 && evidence) {
                            fallback = {depth, y: box.top, identity: identityPath(ancestor)};
                            if (bodies.length) break;
                        }
                        if (times.length > 1 || profileCount > 1) {
                            if (bodies.length || postCount) {
                                fallback = {
                                    depth, y: box.top, identity: identityPath(ancestor),
                                };
                            }
                            break;
                        }
                    }
                    if (fallback === null || seen.has(fallback.identity)) continue;
                    seen.add(fallback.identity);
                    results.push({index, ...fallback});
                }
                return results;
            }"""),
            {
                "viewport": viewport,
                "roots": _FEED_ROOT_SELECTOR,
                "quote": _QUOTE_SELECTOR,
                "body": _BODY_TEXT_SELECTOR,
                "bodyExcluded": _BODY_EXCLUSION_SELECTOR,
                "outside": (
                    "nav, aside, footer, form, [contenteditable='true'], "
                    "[role='navigation'], [role='menu'], [role='complementary'], "
                    "[role='dialog'], [role='search'], [role='banner'], "
                    "[role='contentinfo'], [role='textbox']"
                ),
                "maxAncestors": _MAX_CARD_ANCESTORS,
                "maxTimes": _MAX_STRUCTURE_ELEMENTS,
            },
        )
        for fallback in fallbacks:
            timestamp = times.nth(fallback["index"])
            ancestor = timestamp.locator(
                f"xpath=ancestor::*[{fallback['depth']}]"
            )
            await add_candidate(
                ancestor,
                {"y": fallback["y"]},
                str(fallback["identity"]),
            )

        candidates.sort(key=lambda item: item[0])
        roots = [root for _, root in candidates]
        if len(roots) > _MAX_VISIBLE_ROW_CANDIDATES:
            truncated = True
            roots = roots[:_MAX_VISIBLE_ROW_CANDIDATES]
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
                const postPathHint = value => {
                    if (!value) return false;
                    try {
                        return new URL(value, location.href).pathname
                            .split("/").includes("post");
                    } catch { return false; }
                };
                const cssVisible = element => {
                    const style = getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== "none" && style.visibility !== "hidden" &&
                        rect.width > 0 && rect.height > 0;
                };
                const intersectsVisibleArea = element => {
                    const rect = element.getBoundingClientRect();
                    let left = Math.max(0, rect.left);
                    let top = Math.max(0, rect.top);
                    let right = Math.min(args.viewport.width, rect.right);
                    let bottom = Math.min(args.viewport.height, rect.bottom);
                    for (let parent = element.parentElement; parent;
                         parent = parent.parentElement) {
                        const style = getComputedStyle(parent);
                        const clipX = style.overflowX !== "visible";
                        const clipY = style.overflowY !== "visible";
                        if (!clipX && !clipY) continue;
                        const bounds = parent.getBoundingClientRect();
                        const clipLeft = bounds.left + parent.clientLeft;
                        const clipTop = bounds.top + parent.clientTop;
                        const clipRight = clipLeft + parent.clientWidth;
                        const clipBottom = clipTop + parent.clientHeight;
                        if (clipX) {
                            left = Math.max(left, clipLeft);
                            right = Math.min(right, clipRight);
                        }
                        if (clipY) {
                            top = Math.max(top, clipTop);
                            bottom = Math.min(bottom, clipBottom);
                        }
                    }
                    return right > left && bottom > top;
                };
                const visible = element => cssVisible(element) &&
                    intersectsVisibleArea(element);
                const offscreen = element => cssVisible(element) &&
                    !intersectsVisibleArea(element);
                const belongs = element => {
                    if (element.closest(args.quote)) return false;
                    const nearest = element.parentElement?.closest(args.roots);
                    if (root.matches(args.roots)) return nearest === root;
                    return root.contains(element) && nearest === null;
                };
                const outside = element => element.closest(args.outside) !== null;
                const allAnchors = Array.from(root.querySelectorAll("a"))
                    .filter(element => belongs(element) && !outside(element));
                const anchors = allAnchors
                    .filter(visible)
                    .slice(0, args.maxLinks);
                const offscreenAnchors = allAnchors.filter(offscreen)
                    .slice(0, args.maxLinks);
                const allTimes = Array.from(root.querySelectorAll("time"))
                    .filter(element => belongs(element) && !outside(element));
                const times = allTimes.filter(visible).slice(0, args.maxLinks).length;
                const offscreenTimes = allTimes.filter(offscreen)
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
                const allBodies = Array.from(root.querySelectorAll(args.body))
                    .filter(bodySafe);
                const bodies = allBodies.filter(visible).slice(0, args.maxStructure);
                const offscreenBodies = allBodies.filter(offscreen)
                    .slice(0, args.maxStructure);
                return {
                    root_kind: root.tagName === "ARTICLE" ? "ARTICLE" :
                        root.getAttribute("role") === "article" ? "ROLE_ARTICLE" :
                        root.getAttribute("data-pressable-container") === "true" ? "PRESSABLE" :
                        "CONTAINER",
                    hrefs: anchors.map(element => element.getAttribute("href")),
                    post_links: anchors
                        .map(element => ({
                            href: element.getAttribute("href"),
                            timestamp: Boolean(
                                element.querySelector("time") &&
                                visible(element.querySelector("time"))
                            ),
                        }))
                        .filter(item => postPathHint(item.href)),
                    offscreen_hrefs: offscreenAnchors.map(element =>
                        element.getAttribute("href")),
                    visible_anchors: anchors.length,
                    visible_times: times,
                    offscreen_times: offscreenTimes,
                    body_candidates: bodies.length,
                    offscreen_body_candidates: offscreenBodies.length,
                    readable_body_candidates: bodies.filter(element => {
                        const walker = document.createTreeWalker(
                            element, NodeFilter.SHOW_TEXT
                        );
                        return Boolean(walker.nextNode());
                    }).length,
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
        href_list = tuple(href for href in structure["hrefs"] if href is not None)
        offscreen_hrefs = tuple(
            href for href in structure["offscreen_hrefs"] if href is not None
        )
        post_url_links = tuple(
            (item["href"], bool(item["timestamp"]))
            for item in structure["post_links"]
        )
        return _CandidateStructure(
            visible_anchors=structure["visible_anchors"],
            visible_post_links=sum(_post_url_hint(href) for href in href_list),
            canonical_post_links=sum(_post_url(href) is not None for href in href_list),
            profile_anchor_candidates=sum(
                self._profile_anchor_candidate(href) for href in href_list
            ),
            visible_times=structure["visible_times"],
            body_candidates=structure["body_candidates"],
            readable_body_candidates=structure["readable_body_candidates"],
            paragraph_candidates=structure["paragraph_candidates"],
            offscreen_post_links=sum(_post_url_hint(href) for href in offscreen_hrefs),
            offscreen_profile_anchors=sum(
                self._profile_anchor_candidate(href) for href in offscreen_hrefs
            ),
            offscreen_times=structure["offscreen_times"],
            offscreen_body_candidates=structure["offscreen_body_candidates"],
            post_url_links=post_url_links,
            root_kind=structure["root_kind"],
        )

    @staticmethod
    async def _candidate_root_kind(root: Locator) -> str:
        tag = await root.evaluate("node => node.tagName.toLowerCase()")
        if tag == "article":
            return "ARTICLE"
        if await root.get_attribute("role") == "article":
            return "ROLE_ARTICLE"
        if await root.get_attribute("data-pressable-container") == "true":
            return "PRESSABLE"
        return "CONTAINER"

    def _record_structure_reason(self, reason: str) -> None:
        counts = self.last_scan_counts
        counts.structural_reasons[reason] = counts.structural_reasons.get(reason, 0) + 1

    def _record_url_structure(self, href: str, timestamp_link: bool) -> None:
        if "/post/" not in href:
            return
        counts = self.last_scan_counts
        segments, trailing_slash, has_query, has_fragment, is_relative = (
            _post_url_structure(href)
        )
        canonical = _post_url(href) is not None
        counts.url_timestamp_links += int(timestamp_link)
        counts.url_secondary_links += int(not timestamp_link)
        counts.url_canonical_links += int(canonical)
        counts.url_rejected_links += int(not canonical)
        shape = (
            f"segments={segments},trailing_slash={int(trailing_slash)},"
            f"query={int(has_query)},fragment={int(has_fragment)},"
            f"relative={int(is_relative)},role="
            f"{'TIMESTAMP' if timestamp_link else 'SECONDARY'},"
            f"canonical={int(canonical)}"
        )
        counts.url_structure_counts[shape] = (
            counts.url_structure_counts.get(shape, 0) + 1
        )

    async def _collect_structure_diagnostics(
        self,
        region: Locator,
        roots: list[Locator],
        viewport: dict[str, int],
    ) -> dict[int, _CandidateStructure]:
        counts = self.last_scan_counts
        structures: dict[int, _CandidateStructure] = {}
        inspected = await asyncio.gather(
            *(self._inspect_candidate_root(root, viewport) for root in roots)
        )
        for index, (root, structure) in enumerate(zip(roots, inspected, strict=True)):
            is_semantic = structure.root_kind in {"ARTICLE", "ROLE_ARTICLE"}
            is_pressable = structure.root_kind == "PRESSABLE"
            counts.article_roots += int(is_semantic)
            counts.pressable_roots += int(is_pressable)
            structures[index] = structure
            for href, timestamp_link in structure.post_url_links:
                self._record_url_structure(href, timestamp_link)
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

        unscoped = await region.evaluate(
            """(region, args) => {
                const postPathHint = value => {
                    if (!value) return false;
                    try {
                        return new URL(value, location.href).pathname
                            .split("/").includes("post");
                    } catch { return false; }
                };
                const outsideSelector = args.outside;
                const visibleInPage = element => {
                    const style = getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== "none" && style.visibility !== "hidden" &&
                        rect.width > 0 && rect.height > 0 && rect.right > 0 &&
                        rect.bottom > 0 && rect.left < args.viewport.width &&
                        rect.top < args.viewport.height;
                };
                const outside = element => element.closest(outsideSelector) !== null;
                const nested = element => {
                    const nearest = element.parentElement?.closest(args.roots);
                    return Boolean(nearest?.parentElement?.closest(args.roots));
                };
                const insideFeedRoot = element =>
                    element.parentElement?.closest(args.roots) !== null;
                const safeText = element => {
                    let current = element;
                    while (current) {
                        if (current.matches(args.bodyExcluded)) return false;
                        current = current.parentElement;
                    }
                    return !element.querySelector(args.bodyExcluded);
                };
                const anchors = Array.from(region.querySelectorAll("a"))
                    .slice(0, args.maxElements)
                    .filter(element => !outside(element) && !nested(element) &&
                        !insideFeedRoot(element) && visibleInPage(element));
                const times = Array.from(region.querySelectorAll("time"))
                    .slice(0, args.maxElements)
                    .filter(element => !outside(element) && !nested(element) &&
                        !insideFeedRoot(element) && visibleInPage(element));
                const paragraphs = Array.from(region.querySelectorAll("p"))
                    .slice(0, args.maxElements)
                    .filter(element => !outside(element) && !nested(element) &&
                        !insideFeedRoot(element) && visibleInPage(element) &&
                        safeText(element));
                const bodies = Array.from(region.querySelectorAll(args.body))
                    .slice(0, args.maxElements)
                    .filter(element => !outside(element) && !nested(element) &&
                        !insideFeedRoot(element) && visibleInPage(element) &&
                        safeText(element));
                return {
                    profileHrefs: anchors
                        .map(element => element.getAttribute("href"))
                        .filter(Boolean),
                    postLinks: anchors
                        .map(element => ({
                            href: element.getAttribute("href"),
                            timestamp: Boolean(
                                element.querySelector("time") &&
                                visibleInPage(element.querySelector("time"))
                            ),
                        }))
                        .filter(item => postPathHint(item.href)),
                    visibleAnchors: anchors.length,
                    visibleTimes: times.length,
                    paragraphCandidates: paragraphs.length,
                    bodyCandidates: bodies.length,
                };
            }""",
            {
                "viewport": viewport,
                "roots": _FEED_ROOT_SELECTOR,
                "outside": (
                    "nav, aside, footer, form, [contenteditable='true'], "
                    "[role='navigation'], [role='menu'], [role='complementary'], "
                    "[role='dialog'], [role='search'], [role='banner'], "
                    "[role='contentinfo'], [role='textbox']"
                ),
                "body": _BODY_TEXT_SELECTOR,
                "bodyExcluded": _BODY_EXCLUSION_SELECTOR,
                "maxElements": _MAX_STRUCTURE_ELEMENTS,
            },
        )
        counts.visible_anchors += unscoped["visibleAnchors"]
        counts.visible_times += unscoped["visibleTimes"]
        counts.paragraph_candidates += unscoped["paragraphCandidates"]
        counts.body_candidates += unscoped["bodyCandidates"]
        for item in unscoped["postLinks"]:
            href = item["href"]
            counts.visible_post_links += 1
            counts.url_parsed += int(_post_url(href) is not None)
            self._record_url_structure(href, bool(item["timestamp"]))
        for href in unscoped["profileHrefs"]:
            counts.profile_anchor_candidates += int(
                self._profile_anchor_candidate(href)
            )
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
                if _post_url_hint(href):
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
        on_row: Callable[[_HomeRowObservation], Awaitable[None]] | None = None,
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
            if permalink_box is None or not await self._is_in_visible_scrollports(permalink):
                continue
            self.last_scan_counts.inspected += 1
            href = await permalink.get_attribute("href")
            parsed_post = None if href is None else _post_url(href)
            if parsed_post is None:
                if href is not None and _post_url_hint(href):
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
                if (
                    not self._intersects_viewport(box, viewport)
                    or not await self._is_in_visible_scrollports(root)
                ):
                    continue
                target = await self._read_card(
                    root,
                    currently_visible=currently_visible,
                    viewport_y=box["y"],
                    cached_body_fingerprints=prefer_new_fingerprints,
                    visible_text_progress=currently_visible,
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
            observation_id = secrets.token_hex(8)
            self.last_scan_counts.identity_verified += 1
            self.last_scan_counts.verified += int(target.body_complete)
            self.last_scan_counts.partial_body_rows += int(not target.body_complete)
            if target.body and target.body.strip() and target.body_complete:
                body_was_cached = bool(
                    prefer_new_fingerprints is not None
                    and target.fingerprint in prefer_new_fingerprints
                    and target.fingerprint in self._cached_home_bodies
                )
                self.last_scan_counts.body_read += int(not body_was_cached)
                self.last_scan_counts.body_extracted += 1
                self._cached_home_bodies.setdefault(
                    target.fingerprint,
                    (target.body, target.body_truncated),
                )
            elif target.body and target.body.strip():
                self.last_scan_counts.body_extracted += 1
            else:
                self.last_scan_counts.body_unknown += 1
                if (
                    target.read_evidence is not None
                    and target.read_evidence.state == "UNSUPPORTED_STRUCTURE"
                ):
                    self._record_structure_reason(
                        "POST_BODY_STRUCTURE_UNSUPPORTED"
                    )
            if not target.body_complete:
                self.last_scan_counts.skipped_rows += 1
                self.last_scan_counts.coverage_complete = False
                evidence_state = (
                    "NO_BODY_NODES"
                    if target.read_evidence is None
                    else target.read_evidence.state
                )
                state_reason = {
                    "TEXT_BELOW": ("DEFERRED_TEXT_BELOW", "BODY_TEXT_BELOW_VIEWPORT"),
                    "TEXT_ABOVE": ("DEFERRED_TEXT_ABOVE", "BODY_TEXT_ABOVE_VIEWPORT"),
                    "MEDIA_OR_WHITESPACE_GAP": (
                        "MEDIA_OR_WHITESPACE_GAP",
                        "NO_RENDERED_BODY_TEXT_IN_VIEW",
                    ),
                    "UNSUPPORTED_STRUCTURE": (
                        "UNSUPPORTED_STRUCTURE",
                        "POST_BODY_STRUCTURE_UNSUPPORTED",
                    ),
                }.get(
                    evidence_state,
                    ("PENDING_HYDRATION", "POST_BODY_PENDING"),
                )
                diagnostic = _CandidateDiagnostic(
                    row_index=len(targets_by_url) + 1,
                    root_kind="CONTAINER",
                    has_visible_time=True,
                    has_post_permalink=True,
                    has_profile_author=True,
                    has_readable_body=bool(target.body and target.body.strip()),
                    state=(
                        "PARTIAL_BODY"
                        if target.body and target.body.strip()
                        else state_reason[0]
                    ),
                    reason=(
                        "VISIBLE_TEXT_PROGRESS"
                        if target.body and target.body.strip()
                        else state_reason[1]
                    ),
                    identity_fingerprint=target.fingerprint,
                    observation_id=observation_id,
                    read_evidence=target.read_evidence,
                )
                if on_row is not None:
                    await on_row(
                        _HomeRowObservation(
                            diagnostic,
                            target=target,
                            fingerprint=target.fingerprint,
                        )
                    )
                continue
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
            if on_row is not None:
                diagnostic = _CandidateDiagnostic(
                    row_index=len(targets_by_url),
                    root_kind="CONTAINER",
                    has_visible_time=True,
                    has_post_permalink=True,
                    has_profile_author=True,
                    has_readable_body=bool(target.body and target.body.strip()),
                    state="VERIFIED",
                    reason="PROVENANCE_VERIFIED",
                    identity_fingerprint=target.fingerprint,
                )
                await on_row(
                    _HomeRowObservation(
                        diagnostic,
                        target=target,
                        fingerprint=target.fingerprint,
                    )
                )
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

    async def _revalidate_action_target(self, target: PostTarget) -> PostTarget:
        region = await self._content_region()
        links = region.locator('a[href*="/post/"]')
        matches: list[PostTarget] = []
        for index in range(min(await links.count(), _MAX_PERMALINK_CANDIDATES)):
            link = links.nth(index)
            if (
                not await link.is_visible()
                or await self._is_outside_content_landmark(link)
                or await self._is_nested_feed_content(link)
                or await self._is_inside_quote(link)
                or not await self._is_in_visible_scrollports(link)
            ):
                continue
            href = await link.get_attribute("href")
            parsed = None if href is None else _post_url(href)
            if parsed != (target.url, target.author):
                continue
            root, box = await self._resolve_permalink_card(
                link,
                target.url,
                target.author,
            )
            current, body_stable = await self._read_stable_visible_card(
                root,
                viewport_y=box["y"],
                cached_body_fingerprints={target.fingerprint},
            )
            if (
                not body_stable
                or not current.currently_visible
                or current.fingerprint != target.fingerprint
                or normalize_text(current.body or "")
                != normalize_text(target.body or "")
            ):
                raise UiReadError("DETAIL_PROVENANCE_UNVERIFIED")
            matches.append(current)
        if len(matches) != 1:
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")
        return matches[0]

    async def _detail_click_point(self, permalink: Locator) -> tuple[float, float]:
        viewport = self._page.viewport_size
        box = await permalink.bounding_box()
        if (
            viewport is None
            or box is None
            or box["width"] <= 0
            or box["height"] <= 0
            or not await self._is_in_visible_scrollports(permalink)
        ):
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")
        point = (
            box["x"] + box["width"] / 2,
            box["y"] + box["height"] / 2,
        )
        if not (0 <= point[0] < viewport["width"] and 0 <= point[1] < viewport["height"]):
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")
        try:
            hit_is_permalink = await permalink.evaluate(
                """(node, point) => {
                    const hit = document.elementFromPoint(point.x, point.y);
                    return Boolean(hit && (hit === node || node.contains(hit)));
                }""",
                {"x": point[0], "y": point[1]},
            )
        except PlaywrightError:
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED") from None
        if not hit_is_permalink:
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")
        return point

    async def _read_detail(self, target: PostTarget) -> PostTarget:
        if not target.currently_visible:
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")
        deadline = self._readiness_deadline(_DETAIL_TIMEOUT_MS)
        try:
            def is_target_url(value: str) -> bool:
                parsed = _post_url(value)
                return parsed is not None and parsed == (target.url, target.author)

            await self._wait_for_verified_feed(
                deadline,
                timeout_code="DETAIL_NAVIGATION_UNVERIFIED",
            )
            current = await self._revalidate_action_target(target)
            click_point = await self._detail_click_point(current.permalink)
            self._remaining_readiness_ms(
                deadline,
                "DETAIL_NAVIGATION_UNVERIFIED",
            )
            # Page.mouse.click sends input at the already-visible anchor point
            # without Playwright's locator auto-scroll behavior.
            try:
                await self._page.mouse.click(*click_point)
            except PlaywrightError:
                raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED") from None
            self._mark_ui_transition()
            await self._page.wait_for_url(
                is_target_url,
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
                if not is_target_url(self._page.url):
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
                            if href is not None and _post_url_hint(href) and parsed is None:
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
        if detail.body is None or target.body is None:
            raise UiReadError("DETAIL_BODY_UNVERIFIED")
        home_body = " ".join(normalize_text(target.body).split())
        detail_body = " ".join(normalize_text(detail.body).split())
        body_matches = detail_body == home_body
        prefix = home_body
        if target.body_truncated and not body_matches:
            prefix = home_body.rstrip(" .…")
            body_matches = bool(prefix) and detail_body.startswith(prefix)
        if not body_matches:
            raise UiReadError("DETAIL_BODY_UNVERIFIED")
        self._detail_return_anchor = (target.fingerprint, target.viewport_y)
        self._detail_return_target = target
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
                if href is not None and _post_url_hint(href):
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
        anchor = self._detail_return_anchor
        target = self._detail_return_target
        if anchor is None or target is None:
            raise UiReadError("RETURN_CONTEXT_CHANGED")
        try:
            current = await self._revalidate_action_target(target)
        except UiReadError:
            raise UiReadError("RETURN_CONTEXT_CHANGED") from None
        if current.fingerprint != anchor[0] or abs(current.viewport_y - anchor[1]) > 48:
            raise UiReadError("RETURN_CONTEXT_CHANGED")
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
                        condition="retry",
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
    ) -> tuple[str | None, bool, bool, tuple[_CardWaitSignal, ...]]:
        buttons = root.get_by_role("button", name=_MORE_NAME)
        body_truncated = False
        for index in range(min(await buttons.count(), _MAX_CARD_LINKS)):
            button = buttons.nth(index)
            if await self._belongs_to_card(button, root, root_handle) and await button.is_visible():
                body_truncated = True
                break

        candidates = root.locator(_BODY_TEXT_SELECTOR)
        bodies: list[str] = []
        empty_candidates: list[Locator] = []
        count = min(await candidates.count(), _MAX_STRUCTURE_ELEMENTS)
        for index in range(count):
            candidate = candidates.nth(index)
            if not await self._safe_body_candidate(candidate, root, root_handle):
                continue
            if not await candidate.is_visible():
                continue
            if await candidate.evaluate(
                "node => node.tagName !== 'P' && Boolean(node.querySelector('p'))"
            ):
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
            text = _canonicalize_body_block(await candidate.inner_text())
            if not text:
                empty_candidates.append(candidate)
                continue
            bodies.append(text)

        if not bodies:
            if require_body:
                empty_signals = tuple(
                    _CardWaitSignal(
                        candidate,
                        "nonempty_text",
                        "DETAIL_BODY_UNVERIFIED",
                    )
                    for candidate in empty_candidates
                )
                if empty_signals:
                    raise _CardNotReady(
                        signals=empty_signals,
                        timeout_code="DETAIL_BODY_UNVERIFIED",
                        reason_code="POST_BODY_PENDING",
                    )
                raise _CardNotReady(
                    candidates.first,
                    nonempty_text=True,
                    timeout_code="DETAIL_BODY_UNVERIFIED",
                    reason_code="POST_BODY_PENDING",
                )
            return (
                None,
                False,
                body_truncated,
                tuple(
                    _CardWaitSignal(candidate, "nonempty_text")
                    for candidate in empty_candidates
                ),
            )
        body = _canonicalize_body("\n".join(bodies))
        if len(body) > _MAX_BODY_CODEPOINTS:
            if require_body:
                raise _CardNotReady(
                    candidates.first,
                    nonempty_text=True,
                    timeout_code="DETAIL_BODY_UNVERIFIED",
                )
            return None, True, body_truncated, ()
        if body.rstrip().endswith(("…", "...")):
            body_truncated = True
        return body, True, body_truncated, ()

    async def _observe_display_body(
        self,
        root: Locator,
    ) -> tuple[str | None, tuple[_CardWaitSignal, ...], BodyReadEvidence]:
        root_handle = await root.element_handle()
        if root_handle is None:
            return None, (), BodyReadEvidence(state="NO_BODY_NODES")
        observed = await self._read_visible_body_progress(root, root_handle)
        if observed.has_rendered_text:
            return observed.body, (), observed.read_evidence
        candidates = root.locator(_BODY_TEXT_SELECTOR)
        signals = tuple(
            _CardWaitSignal(candidates.nth(index), "nonempty_text")
            for index in range(min(await candidates.count(), _MAX_STRUCTURE_ELEMENTS))
            if await self._safe_body_candidate(candidates.nth(index), root, root_handle)
        )
        return None, signals, observed.read_evidence

    @staticmethod
    def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
        merged: list[list[int]] = []
        for start, end in sorted(ranges):
            if end <= start:
                continue
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return [(start, end) for start, end in merged]

    @staticmethod
    def _subtract_ranges(
        start: int,
        end: int,
        covered: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        remaining: list[tuple[int, int]] = []
        cursor = start
        for old_start, old_end in covered:
            if old_end <= cursor:
                continue
            if old_start >= end:
                break
            if old_start > cursor:
                remaining.append((cursor, min(old_start, end)))
            cursor = max(cursor, old_end)
            if cursor >= end:
                break
        if cursor < end:
            remaining.append((cursor, end))
        return remaining

    @staticmethod
    def _utf16_slice(value: str, start: int, end: int) -> str:
        encoded = value.encode("utf-16-le", errors="surrogatepass")
        return encoded[start * 2 : end * 2].decode("utf-16-le", errors="surrogatepass")

    @classmethod
    def _fragment_text(
        cls,
        fragments: list[_TextFragment],
        start: int,
        end: int,
    ) -> str | None:
        for fragment in fragments:
            if fragment.start <= start and fragment.end >= end:
                return cls._utf16_slice(
                    fragment.text,
                    start - fragment.start,
                    end - fragment.start,
                )
        return None

    async def _read_visible_body_progress(
        self,
        root: Locator,
        root_handle: JSHandle,
        *,
        fingerprint: str | None = None,
    ) -> _VisibleBodyRead:
        observation = await root.evaluate(
            _with_scrollport_geometry("""(root, args) => {
                /*SCROLLPORT_GEOMETRY*/
                const rootRect = root.getBoundingClientRect();
                const rootGeometry = geometryFor(root);
                const rootClip = rootGeometry.clip;
                const rootIntersects = rootGeometry.intersects;
                const rootPositionBand = rootRect.bottom <= rootClip.top ? "ABOVE" :
                    rootRect.top >= rootClip.bottom ? "BELOW" : "INTERSECTS";
                const cssVisible = element => {
                    for (let current = element; current; current = current.parentElement) {
                        const style = getComputedStyle(current);
                        if (style.display === "none" || style.visibility === "hidden" ||
                            Number(style.opacity) === 0) return false;
                        if (current === root) break;
                    }
                    return true;
                };
                const belongs = element => {
                    const nearest = element.parentElement?.closest(args.roots);
                    if (root.matches(args.roots)) return nearest === root;
                    return root.contains(element) && !nearest;
                };
                const excluded = element =>
                    element.matches(args.excluded) ||
                    Boolean(element.parentElement?.closest(args.excluded));
                const hasBodyContainer = element =>
                    root.matches(args.body) || element.matches(args.body) ||
                    Boolean(element.parentElement?.closest(args.body));
                const nearestBlock = element => {
                    for (let current = element.parentElement; current;
                         current = current.parentElement) {
                        const display = getComputedStyle(current).display;
                        if (["block", "flow-root", "flex", "grid", "list-item",
                             "table", "table-cell", "table-caption"].includes(display)) {
                            return current;
                        }
                        if (current === root) return root;
                    }
                    return root;
                };
                const stableNodeKey = textNode => {
                    const parts = [];
                    const owner = textNode.parentElement;
                    for (let current = owner; current && current !== root;
                         current = current.parentElement) {
                        let sameTagIndex = 0;
                        for (const sibling of current.parentElement?.children || []) {
                            if (sibling.tagName !== current.tagName) continue;
                            if (sibling === current) break;
                            sameTagIndex++;
                        }
                        parts.push(`${current.tagName.toLowerCase()}:${sameTagIndex}`);
                    }
                    let textIndex = 0;
                    for (const sibling of owner.childNodes) {
                        if (sibling === textNode) break;
                        if (sibling.nodeType === Node.TEXT_NODE) textIndex++;
                    }
                    return `${parts.reverse().join("/")}/text:${textIndex}`;
                };
                const bodyContainers = Array.from(root.querySelectorAll(args.body))
                    .filter(element => belongs(element) && !excluded(element) &&
                        cssVisible(element)).length;
                const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                const nodes = [];
                const blockIds = new WeakMap();
                const renderRectKeys = new Set();
                const visibleRectKeys = new Set();
                const aboveRectKeys = new Set();
                const belowRectKeys = new Set();
                let nextBlockId = 0;
                let totalLength = 0;
                let rangeChecks = 0;
                let bodyTextNodesPresent = 0;
                let unsupportedTextNodesPresent = 0;
                let closestTextBelow = null;
                let closestTextAbove = null;
                let closestTextTop = null;
                let closestTextBottom = null;
                let truncated = false;
                let node;
                while ((node = walker.nextNode())) {
                    const parent = node.parentElement;
                    if (!parent || !belongs(parent) || excluded(parent) ||
                        !cssVisible(parent)) continue;
                    if (!node.length) continue;
                    if (!hasBodyContainer(parent)) {
                        if (node.data.trim()) unsupportedTextNodesPresent++;
                        continue;
                    }
                    bodyTextNodesPresent++;
                    const length = Math.min(node.length, args.maxChars - totalLength);
                    if (length <= 0) { truncated = true; break; }
                    if (length < node.length) truncated = true;
                    totalLength += length;
                    const block = nearestBlock(node);
                    if (!blockIds.has(block)) blockIds.set(block, nextBlockId++);
                    const clip = clipFor(parent);
                    const metrics = scrollMetrics(root);
                    const expected = [];
                    const visible = [];
                    let deferredAbove = 0;
                    let deferredBelow = 0;
                    let deferredOther = 0;
                    let nearestBelow = null;
                    let frontier = null;
                    const range = document.createRange();
                    const inspect = (start, end) => {
                        if (rangeChecks++ >= args.maxRangeChecks) {
                            truncated = true;
                            return;
                        }
                        range.setStart(node, start);
                        range.setEnd(node, end);
                        const rects = Array.from(range.getClientRects()).filter(rect =>
                            rect.width > 0 && rect.height > 0
                        );
                        if (!rects.length) return;
                        expected.push([start, end]);
                        const rectIdentity = stableNodeKey(node);
                        const states = rects.map(rect => {
                            const key = `${rectIdentity}:${Math.round(rect.left * 2) / 2}:` +
                                `${Math.round(rect.top * 2) / 2}:${Math.round(rect.right * 2) / 2}:` +
                                `${Math.round(rect.bottom * 2) / 2}`;
                            const visible = rect.right > clip.left &&
                                rect.left < clip.right && rect.bottom > clip.top &&
                                rect.top < clip.bottom;
                            renderRectKeys.add(key);
                            if (visible) visibleRectKeys.add(key);
                            else if (rect.bottom <= clip.top) aboveRectKeys.add(key);
                            else if (rect.top >= clip.bottom) belowRectKeys.add(key);
                            return {
                                visible,
                                fully: rect.left >= clip.left &&
                                    rect.right <= clip.right && rect.top >= clip.top &&
                                    rect.bottom <= clip.bottom,
                            };
                        });
                        for (const rect of rects) {
                            if (rect.top >= clip.bottom) {
                                const distance = Math.max(0, rect.top - clip.bottom);
                                if (distance < (closestTextBelow ?? Infinity)) {
                                    closestTextBelow = distance;
                                    closestTextTop = rect.top;
                                    closestTextBottom = rect.bottom;
                                }
                            } else if (rect.bottom <= clip.top) {
                                const distance = Math.max(0, clip.top - rect.bottom);
                                if (distance < (closestTextAbove ?? Infinity)) {
                                    closestTextAbove = distance;
                                    closestTextTop = rect.top;
                                    closestTextBottom = rect.bottom;
                                }
                            } else if (closestTextTop === null) {
                                closestTextTop = rect.top;
                                closestTextBottom = rect.bottom;
                            }
                        }
                        if (states.every(state => state.fully)) {
                            const text = node.data.slice(start, end);
                            const top = Math.min(...rects.map(rect => rect.top));
                            const bottom = Math.max(...rects.map(rect => rect.bottom));
                            visible.push({
                                start, end, text, top,
                                contentY: bottom + metrics.offset,
                            });
                            frontier = Math.max(frontier ?? -Infinity,
                                bottom + metrics.offset);
                            return;
                        }
                        if (states.every(state => !state.visible)) {
                            const above = rects.every(rect => rect.bottom <= clip.top);
                            const below = rects.every(rect => rect.top >= clip.bottom);
                            if (above) deferredAbove++;
                            else if (below) {
                                deferredBelow++;
                                const distance = Math.max(0,
                                    Math.min(...rects.map(rect => rect.top)) - clip.bottom);
                                nearestBelow = Math.min(nearestBelow ?? Infinity, distance);
                            } else deferredOther++;
                            return;
                        }
                        if (end - start <= 1) {
                            const top = Math.min(...rects.map(rect => rect.top));
                            const bottom = Math.max(...rects.map(rect => rect.bottom));
                            visible.push({
                                start, end, text: node.data.slice(start, end), top,
                                contentY: bottom + metrics.offset,
                            });
                            frontier = Math.max(frontier ?? -Infinity,
                                bottom + metrics.offset);
                            return;
                        }
                        let middle = start + Math.floor((end - start) / 2);
                        if (middle > start && middle < end) {
                            const before = node.data.charCodeAt(middle - 1);
                            const after = node.data.charCodeAt(middle);
                            if (before >= 0xD800 && before <= 0xDBFF &&
                                after >= 0xDC00 && after <= 0xDFFF) {
                                middle++;
                            }
                        }
                        inspect(start, middle);
                        inspect(middle, end);
                    };
                    inspect(0, length);
                    range.detach();
                    const mergedVisible = [];
                    for (const piece of visible.sort((a, b) => a.start - b.start)) {
                        const previous = mergedVisible[mergedVisible.length - 1];
                        if (previous && previous.end === piece.start &&
                            Math.abs(previous.top - piece.top) < 0.5 &&
                            Math.abs(previous.contentY - piece.contentY) < 0.5) {
                            previous.end = piece.end;
                            previous.text += piece.text;
                        } else {
                            mergedVisible.push({...piece});
                        }
                    }
                    nodes.push({
                        index: nodes.length,
                        key: stableNodeKey(node),
                        length,
                        block: blockIds.get(block),
                        expected,
                        visible: mergedVisible,
                        deferredAbove,
                        deferredBelow,
                        deferredOther,
                        nearestBelow,
                        frontier,
                    });
                    if (truncated) break;
                    if (nodes.length >= args.maxNodes) { truncated = true; break; }
                }
                const closestTextSide = closestTextBelow !== null &&
                    (closestTextAbove === null || closestTextBelow <= closestTextAbove)
                    ? "BELOW" : closestTextAbove !== null ? "ABOVE" : "NONE";
                const closestTextDistance = closestTextSide === "BELOW"
                    ? closestTextBelow : closestTextSide === "ABOVE"
                    ? closestTextAbove : null;
                let textEvidence = "NO_BODY_NODES";
                if (visibleRectKeys.size) textEvidence = "TEXT_IN_VIEW";
                else if (renderRectKeys.size && belowRectKeys.size &&
                    (!aboveRectKeys.size || closestTextBelow <= closestTextAbove)) {
                    textEvidence = "TEXT_BELOW";
                } else if (renderRectKeys.size && aboveRectKeys.size) {
                    textEvidence = "TEXT_ABOVE";
                } else if (renderRectKeys.size) {
                    textEvidence = "MEDIA_OR_WHITESPACE_GAP";
                } else if (bodyTextNodesPresent || bodyContainers) {
                    textEvidence = bodyTextNodesPresent
                        ? "MEDIA_OR_WHITESPACE_GAP" : "NO_BODY_NODES";
                } else if (unsupportedTextNodesPresent) {
                    textEvidence = "UNSUPPORTED_STRUCTURE";
                }
                return {
                    nodes, truncated, bodyContainers, bodyTextNodesPresent,
                    unsupportedTextNodesPresent,
                    renderRectCount: renderRectKeys.size,
                    visibleTextRectCount: visibleRectKeys.size,
                    deferredAboveRectCount: aboveRectKeys.size,
                    deferredBelowRectCount: belowRectKeys.size,
                    closestTextDistance,
                    closestTextSide,
                    rootIntersects,
                    rootPositionBand,
                    scrollTop: scrollMetrics(root).top,
                    scrollHeight: scrollMetrics(root).scrollHeight,
                    scrollSurfaceKey: scrollMetrics(root).key,
                    rowTop: rootRect.top,
                    rowBottom: rootRect.bottom,
                    clipTop: rootClip.top,
                    clipBottom: rootClip.bottom,
                    closestTextTop,
                    closestTextBottom,
                    textEvidence,
                };
            }"""),
            {
                "roots": _FEED_ROOT_SELECTOR,
                "body": _BODY_TEXT_SELECTOR,
                "excluded": _BODY_EXCLUSION_SELECTOR,
                "maxChars": _MAX_BODY_CODEPOINTS,
                "maxNodes": _MAX_BODY_TEXT_NODES,
                "maxRangeChecks": _MAX_BODY_RANGE_CHECKS,
            },
        )

        read_evidence = BodyReadEvidence(
            state=str(observation["textEvidence"]),
            body_text_nodes_present=int(observation["bodyTextNodesPresent"]),
            render_rect_count=int(observation["renderRectCount"]),
            visible_text_rect_count=int(observation["visibleTextRectCount"]),
            deferred_above_rect_count=int(observation["deferredAboveRectCount"]),
            deferred_below_rect_count=int(observation["deferredBelowRectCount"]),
            closest_text_distance_px=(
                None
                if observation["closestTextDistance"] is None
                else float(observation["closestTextDistance"])
            ),
            closest_text_side=str(observation["closestTextSide"]),
            row_top_px=float(observation["rowTop"]),
            row_bottom_px=float(observation["rowBottom"]),
            clip_top_px=float(observation["clipTop"]),
            clip_bottom_px=float(observation["clipBottom"]),
            text_rect_top_px=(
                None
                if observation["closestTextTop"] is None
                else float(observation["closestTextTop"])
            ),
            text_rect_bottom_px=(
                None
                if observation["closestTextBottom"] is None
                else float(observation["closestTextBottom"])
            ),
            root_intersects=bool(observation["rootIntersects"]),
            root_position_band=str(observation["rootPositionBand"]),
            scroller_scroll_top=float(observation["scrollTop"]),
            scroll_surface_key=str(observation["scrollSurfaceKey"]),
        )
        state: dict[str, _BodyTextProgress] | None = None
        if fingerprint is not None:
            state = self._body_text_progress.get(fingerprint)
            if state is None:
                if len(self._body_text_progress) >= _MAX_BODY_PROGRESS_POSTS:
                    return _VisibleBodyRead(
                        None,
                        False,
                        False,
                        truncated=True,
                        read_evidence=read_evidence,
                    )
                state = {}
                self._body_text_progress[fingerprint] = state
        progress_state = {} if state is None else state

        new_segments: list[VisibleBodySegment] = []
        has_rendered_text = False
        content_changed = False
        deferred_above = 0
        deferred_below = 0
        nearest_deferred_below: float | None = None
        frontier_content: float | None = None
        for node in observation["nodes"]:
            order = int(node["index"])
            identity = str(node["key"])
            length = int(node["length"])
            block = int(node["block"])
            expected = self._merge_ranges(
                [(int(start), int(end)) for start, end in node["expected"]]
            )
            has_rendered_text = has_rendered_text or bool(expected)
            deferred_above += int(node["deferredAbove"])
            deferred_below += int(node["deferredBelow"])
            if node["nearestBelow"] is not None:
                distance = float(node["nearestBelow"])
                nearest_deferred_below = min(
                    nearest_deferred_below if nearest_deferred_below is not None else distance,
                    distance,
                )
            if node["frontier"] is not None:
                frontier_content = max(
                    frontier_content
                    if frontier_content is not None
                    else float(node["frontier"]),
                    float(node["frontier"]),
                )

            progress = progress_state.get(identity)
            if progress is None:
                progress = _BodyTextProgress(length, block, order)
                progress_state[identity] = progress
            elif length < progress.length:
                progress.expected_ranges = [
                    (start, min(end, length))
                    for start, end in progress.expected_ranges
                    if start < length
                ]
                progress.visited_ranges = [
                    (start, min(end, length))
                    for start, end in progress.visited_ranges
                    if start < length
                ]
                progress.fragments = [
                    _TextFragment(
                        fragment.start,
                        min(fragment.end, length),
                        self._utf16_slice(fragment.text, 0, min(fragment.end, length) - fragment.start),
                        fragment.viewport_y,
                        fragment.content_y,
                    )
                    for fragment in progress.fragments
                    if fragment.start < length
                ]
                progress.length = length
            else:
                progress.length = length
            progress.block = block
            progress.order = order
            progress.expected_ranges = expected

            current_visible = sorted(
                node["visible"],
                key=lambda piece: (int(piece["start"]), int(piece["end"])),
            )
            for piece in current_visible:
                start = int(piece["start"])
                end = int(piece["end"])
                text = str(piece["text"])
                for old in progress.fragments:
                    overlap_start = max(start, old.start)
                    overlap_end = min(end, old.end)
                    if overlap_start >= overlap_end:
                        continue
                    old_text = self._utf16_slice(
                        old.text,
                        overlap_start - old.start,
                        overlap_end - old.start,
                    )
                    new_text = self._utf16_slice(
                        text,
                        overlap_start - start,
                        overlap_end - start,
                    )
                    if old_text != new_text:
                        content_changed = True
                        progress.visited_ranges.clear()
                        progress.fragments.clear()
                        break
                uncovered = self._subtract_ranges(start, end, progress.visited_ranges)
                for new_start, new_end in uncovered:
                    fragment_text = self._utf16_slice(
                        text,
                        new_start - start,
                        new_end - start,
                    )
                    if not fragment_text.strip():
                        progress.visited_ranges = self._merge_ranges(
                            [*progress.visited_ranges, (new_start, new_end)]
                        )
                        progress.fragments.append(
                            _TextFragment(
                                new_start,
                                new_end,
                                fragment_text,
                                float(piece["top"]),
                                float(piece["contentY"]),
                            )
                        )
                        continue
                    progress.visited_ranges = self._merge_ranges(
                        [*progress.visited_ranges, (new_start, new_end)]
                    )
                    progress.fragments.append(
                        _TextFragment(
                            new_start,
                            new_end,
                            fragment_text,
                            float(piece["top"]),
                            float(piece["contentY"]),
                        )
                    )
                    segment_key = sha256(
                        self._body_progress_salt
                        + (
                            f"{fingerprint or 'display'}:{identity}:"
                            f"{new_start}:{new_end}:"
                        ).encode()
                        + fragment_text.encode("utf-8", errors="replace")
                    ).hexdigest()
                    new_segments.append(
                        VisibleBodySegment(
                            segment_key,
                            fragment_text,
                            float(piece["top"]),
                            float(piece["contentY"]),
                        )
                    )

        has_text_state = has_rendered_text or bool(progress_state)
        body_complete = bool(
            fingerprint is not None
            and progress_state
            and has_rendered_text
            and not observation["truncated"]
            and not content_changed
            and all(
                not self._subtract_ranges(start, end, progress.visited_ranges)
                for progress in progress_state.values()
                for start, end in progress.expected_ranges
            )
        )
        body_blocks: list[tuple[int, list[str]]] = []
        for _identity, progress in sorted(
            progress_state.items(), key=lambda item: item[1].order
        ):
            if not body_blocks or body_blocks[-1][0] != progress.block:
                body_blocks.append((progress.block, []))
            block_parts = body_blocks[-1][1]
            for fragment in sorted(progress.fragments, key=lambda item: item.start):
                block_parts.append(fragment.text)
        body = "\n".join(
            text
            for text in (
                _canonicalize_body_block("".join(parts))
                for _, parts in body_blocks
            )
            if text
        )
        self.last_scan_counts.new_visible_text_segments += len(new_segments)
        self.last_scan_counts.deferred_above += deferred_above
        self.last_scan_counts.deferred_below += deferred_below
        if nearest_deferred_below is not None:
            current = self.last_scan_counts.nearest_deferred_below_px
            self.last_scan_counts.nearest_deferred_below_px = min(
                nearest_deferred_below if current is None else current,
                nearest_deferred_below,
            )
        if frontier_content is not None:
            current_frontier = self.last_scan_counts.frontier_content_px
            self.last_scan_counts.frontier_content_px = max(
                frontier_content if current_frontier is None else current_frontier,
                frontier_content,
            )
        return _VisibleBodyRead(
            body if body.strip() else None,
            body_complete,
            has_text_state,
            not content_changed,
            tuple(new_segments),
            deferred_above,
            deferred_below,
            nearest_deferred_below,
            frontier_content,
            bool(observation["truncated"]),
            replace(read_evidence, frontier_content_px=frontier_content),
        )

    async def _read_card(
        self,
        root: Locator,
        *,
        currently_visible: bool = False,
        viewport_y: float = 0.0,
        require_body: bool = False,
        cached_body_fingerprints: set[str] | None = None,
        visible_text_progress: bool = False,
    ) -> PostTarget:
        if not await root.is_visible():
            raise _CardNotReady(root)
        root_handle = await root.element_handle()
        if root_handle is None:
            raise _CardNotReady(root)
        post_links: list[tuple[str, str, Locator, bool]] = []
        rejected_post_links: list[tuple[str, bool, str]] = []
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
            timestamp_link = await link.locator("time").count() > 0
            parsed_post = _post_url(href)
            if parsed_post is not None:
                post_links.append(
                    (parsed_post[0], parsed_post[1], link, timestamp_link)
                )
                continue
            if _post_url_hint(href):
                rejected_post_links.append(
                    (href, timestamp_link, _post_url_rejection_reason(href))
                )
                continue
            author = _profile_author(href)
            if author is not None:
                authors.add(author)
        blocking_invalid = next(
            (item for item in rejected_post_links if item[1]),
            next(
                (
                    item
                    for item in rejected_post_links
                    if item[2] == "POST_ORIGIN_INVALID"
                ),
                None,
            ),
        )
        if blocking_invalid is not None:
            href, _, reason = blocking_invalid
            raise _UnverifiedFeedCandidate(
                "POST_PROVENANCE_UNVERIFIED",
                "CANDIDATE_NONCANONICAL",
                (href,),
                diagnostic_reason=reason,
            )
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
        if not post_links and rejected_post_links:
            href, _, reason = rejected_post_links[0]
            raise _UnverifiedFeedCandidate(
                "POST_PROVENANCE_UNVERIFIED",
                "CANDIDATE_NONCANONICAL",
                (href,),
                diagnostic_reason=reason,
            )
        if not post_links:
            raise _CardNotReady(
                root.locator('a[href*="/post/"]').first,
                reason_code="POST_PERMALINK_PENDING",
            )
        if not authors:
            raise _CardNotReady(
                self._expected_profile_link_signal(root, post_links[0][1]),
                reason_code="POST_AUTHOR_PENDING",
            )
        if len(unique_urls) != 1 or len(unique_permalink_links) != 1:
            raise _UnverifiedFeedCandidate(
                "POST_PROVENANCE_UNVERIFIED",
                "CANDIDATE_AMBIGUOUS",
                tuple(unique_urls),
                diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
            )
        timestamp_links = [item for item in post_links if item[3]]
        url, permalink_author, link, _ = (
            timestamp_links[0] if timestamp_links else post_links[0]
        )
        author = next(iter(authors))
        if author != permalink_author:
            raise _UnverifiedFeedCandidate(
                "POST_AUTHOR_UNVERIFIED",
                "CANDIDATE_AUTHOR_CONFLICT",
                (url,),
                diagnostic_reason="CANDIDATE_AUTHOR_MISMATCH",
            )

        fingerprint = sha256(f"{url}\n{author}".encode()).hexdigest()
        body_truncated = False
        cached_body = (
            self._cached_home_bodies.get(fingerprint)
            if not require_body
            and cached_body_fingerprints is not None
            and fingerprint in cached_body_fingerprints
            else None
        )
        if cached_body is not None and not currently_visible:
            return PostTarget(
                url,
                author,
                cached_body[0],
                fingerprint,
                link,
                body_available=bool(cached_body[0].strip()),
                body_truncated=cached_body[1],
                read_evidence=BodyReadEvidence(
                    state="NOT_SAMPLED_CACHE",
                    body_text_nodes_present=None,
                    render_rect_count=None,
                    visible_text_rect_count=None,
                    deferred_above_rect_count=None,
                    deferred_below_rect_count=None,
                    root_intersects=None,
                    root_position_band="NOT_SAMPLED",
                    scroll_surface_key="NOT_SAMPLED",
                ),
                viewport_y=viewport_y,
            )
        permalink_visible = True
        visible_body: _VisibleBodyRead | None = None
        if currently_visible:
            if visible_text_progress:
                visible_body = await self._read_visible_body_progress(
                    root,
                    root_handle,
                    fingerprint=fingerprint,
                )
            else:
                has_body_candidate, body_fully_visited = (
                    await self._visible_body_candidate_state(
                        root,
                        fingerprint=fingerprint,
                    )
                )
                if has_body_candidate and not body_fully_visited:
                    raise _CardDeferredOffscreen(
                        fingerprint,
                        "POST_BODY_PARTIALLY_VISIBLE",
                    )
            permalink_visible = await self._is_in_visible_scrollports(link)
            if (
                not permalink_visible
                and not visible_text_progress
                and not has_body_candidate
            ):
                raise _CardDeferredOffscreen(
                    fingerprint,
                    "POST_PERMALINK_OUTSIDE_VISIBLE_SCROLLPORT",
                )

        if visible_text_progress and visible_body is not None:
            body = visible_body.body
            body_available = bool(body and body.strip())
            body_complete = visible_body.complete and body_available
            body_consistent = visible_body.consistent
            body_segments = visible_body.segments
            body_truncated = body_truncated or visible_body.truncated
        else:
            body, body_available, body_truncated, body_signals = (
                await self._extract_card_body(
                    root,
                    root_handle,
                    require_body=require_body,
                )
            )
            if not body_available and body_signals:
                raise _CardNotReady(
                    signals=body_signals,
                    reason_code="POST_BODY_PENDING",
                )
            body_complete = body_available
            body_consistent = True
            body_segments = ()
        if body and body.rstrip().endswith(("…", "...")):
            body_truncated = True
        return PostTarget(
            url,
            author,
            body,
            fingerprint,
            link,
            body_available=body_available,
            body_truncated=body_truncated,
            body_complete=body_complete,
            body_consistent=body_consistent,
            visible_segments=body_segments,
            read_evidence=(
                None if visible_body is None else visible_body.read_evidence
            ),
            currently_visible=(
                currently_visible
                and permalink_visible
                and (
                    visible_body is None
                    or visible_body.read_evidence.root_intersects
                )
            ),
            viewport_y=viewport_y,
        )

    async def _wait_for_stable_card_dom(self, root: Locator) -> bool:
        """Wait for a bounded quiet interval after a hydration signal."""
        return bool(
            await root.evaluate(
                """async (node, args) => {
                    const snapshot = () => JSON.stringify([
                        node.innerText,
                        Array.from(node.querySelectorAll("a"), link =>
                            link.getAttribute("href")),
                    ]);
                    let value = snapshot();
                    let changedAt = performance.now();
                    const observer = new MutationObserver(() => {
                        const current = snapshot();
                        if (current !== value) {
                            value = current;
                            changedAt = performance.now();
                        }
                    });
                    observer.observe(node, {
                        subtree: true,
                        childList: true,
                        characterData: true,
                        attributes: true,
                        attributeFilter: ["href"],
                    });
                    const deadline = performance.now() + args.timeoutMs;
                    while (
                        performance.now() - changedAt < args.dwellMs &&
                        performance.now() < deadline
                    ) {
                        await new Promise(resolve => setTimeout(resolve, 16));
                    }
                    observer.disconnect();
                    return performance.now() - changedAt >= args.dwellMs;
                }""",
                {
                    "dwellMs": min(
                        max(1, _BODY_STABILITY_INTERVAL_MS),
                        max(1, _CARD_HYDRATION_WAIT_MS),
                    ),
                    "timeoutMs": max(1, _CARD_HYDRATION_WAIT_MS),
                },
            )
        )

    async def _read_stable_visible_card(
        self,
        root: Locator,
        *,
        viewport_y: float,
        cached_body_fingerprints: set[str] | None,
    ) -> tuple[PostTarget, bool]:
        """Require the same row identity and body across a bounded dwell."""
        target = await self._read_card(
            root,
            currently_visible=True,
            viewport_y=viewport_y,
            cached_body_fingerprints=cached_body_fingerprints,
            visible_text_progress=True,
        )
        # Measure stability between completed samples. DOM extraction can take
        # longer than the existing hydration wait on a busy feed; that work
        # already provides elapsed time and must not make a stable row fail.
        await asyncio.sleep(max(1, _BODY_STABILITY_INTERVAL_MS) / 1000)
        settled = await self._read_card(
            root,
            currently_visible=True,
            viewport_y=viewport_y,
            cached_body_fingerprints=cached_body_fingerprints,
            visible_text_progress=True,
        )
        body_same = (
            _canonicalize_body(target.body or "")
            == _canonicalize_body(settled.body or "")
            and target.body_complete == settled.body_complete
            and target.body_truncated == settled.body_truncated
        )
        identity_same = target.url == settled.url and target.author == settled.author
        if not body_same or not identity_same or not settled.body_consistent:
            return settled, False
        segments = tuple(
            {
                segment.segment_id: segment
                for segment in target.visible_segments + settled.visible_segments
            }.values()
        )
        return replace(settled, visible_segments=segments), True

    async def _visible_body_candidate_state(
        self,
        root: Locator,
        *,
        fingerprint: str | None = None,
    ) -> tuple[bool, bool]:
        candidates = await root.evaluate(
            _with_scrollport_geometry("""(root, args) => {
                /*SCROLLPORT_GEOMETRY*/
                const bodySafe = element => {
                    let current = element;
                    while (current && current !== root) {
                        if (current.matches(args.excluded)) return false;
                        current = current.parentElement;
                    }
                    if (current !== root) return false;
                    const nearest = element.parentElement?.closest(args.roots);
                    if (root.matches(args.roots)) {
                        if (nearest !== root) return false;
                    } else if (!root.contains(element) || nearest) {
                        return false;
                    }
                    return !element.querySelector(`${args.excluded}, ${args.roots}`);
                };
                const cssVisible = element => {
                    const style = getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== "none" && style.visibility !== "hidden" &&
                        rect.width > 0 && rect.height > 0;
                };
                const geometry = element => {
                    const rect = element.getBoundingClientRect();
                    const geometry = geometryFor(element);
                    const metrics = scrollMetrics(element);
                    const contentTop = rect.top + metrics.offset;
                    const contentBottom = rect.bottom + metrics.offset;
                    const visibleTop = geometry.visible.top + metrics.offset;
                    const visibleBottom = geometry.visible.bottom + metrics.offset;
                    return {
                        top: contentTop,
                        bottom: contentBottom,
                        width: rect.width,
                        visibleTop: Math.max(contentTop, visibleTop),
                        visibleBottom: Math.min(contentBottom, visibleBottom),
                        intersects: geometry.intersects,
                    };
                };
                const all = Array.from(root.querySelectorAll(args.body))
                    .filter(element => bodySafe(element) && cssVisible(element))
                    .slice(0, args.maxStructure);
                const bodySet = new Set(all);
                const outermost = all.filter(element => {
                    for (let parent = element.parentElement;
                         parent && parent !== root; parent = parent.parentElement) {
                        if (bodySet.has(parent)) return false;
                    }
                    return true;
                });
                return outermost.map(geometry);
            }"""),
            {
                "excluded": _BODY_EXCLUSION_SELECTOR,
                "roots": _FEED_ROOT_SELECTOR,
                "body": _BODY_TEXT_SELECTOR,
                "maxStructure": _MAX_STRUCTURE_ELEMENTS,
            },
        )
        if not candidates:
            return False, False
        fully_visited = True
        any_visible = False
        for index, candidate in enumerate(candidates):
            top = float(candidate["top"])
            bottom = float(candidate["bottom"])
            width = float(candidate["width"])
            visible_top = float(candidate["visibleTop"])
            visible_bottom = float(candidate["visibleBottom"])
            intersects = bool(candidate["intersects"])
            any_visible = any_visible or intersects
            if fingerprint is None:
                fully_visited = fully_visited and intersects
                continue
            key = (fingerprint, index)
            previous = self._body_visibility_ranges.get(key)
            if (
                previous is None
                or abs(previous[0] - top) > 2
                or abs(previous[1] - bottom) > 2
                or abs(previous[2] - width) > 2
            ):
                intervals: list[tuple[float, float]] = []
            else:
                intervals = list(previous[3])
            if visible_bottom > visible_top:
                intervals.append((visible_top, visible_bottom))
            merged: list[list[float]] = []
            for start, end in sorted(intervals):
                if merged and start <= merged[-1][1] + 1:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])
            self._body_visibility_ranges[key] = (
                top,
                bottom,
                width,
                [(start, end) for start, end in merged],
            )
            fully_visited = fully_visited and bool(
                merged and merged[0][0] <= top + 2 and merged[0][1] >= bottom - 2
            )
        return True, fully_visited if fingerprint is not None else any_visible
