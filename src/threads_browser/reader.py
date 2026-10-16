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

from .browser_ui.body_text import (
    _MAX_BODY_CODEPOINTS,  # noqa: F401 - retained reader-module compatibility name
    BodyTextReader,
    _body_progress_state,
    _canonicalize_body,  # noqa: F401 - retained reader-module compatibility name
    _canonicalize_body_block,  # noqa: F401 - retained reader-module compatibility name
    _VisibleBodyRead,
)
from .browser_ui.card_provenance import (
    CardProvenanceReader,
    _CardDeferredOffscreen,
)
from .browser_ui.detail import DetailNavigator
from .browser_ui.feed_rows import (
    _BODY_EXCLUSION_SELECTOR,
    _BODY_TEXT_SELECTOR,
    _FEED_ROOT_SELECTOR,
    _MAX_CARD_ANCESTORS,
    _MAX_CARD_LINKS,  # noqa: F401 - retained reader-module compatibility name
    _MAX_STRUCTURE_ELEMENTS,
    _QUOTE_SELECTOR,
    FeedRowScanner,
    _CandidateDiagnostic,
    _CandidateStructure,
    _FeedScanCounts,
    _FeedScanTimings,
)
from .browser_ui.feed_rows import (
    UnverifiedFeedCandidate as _UnverifiedFeedCandidate,
)
from .browser_ui.geometry import with_scrollport_geometry as _with_scrollport_geometry
from .browser_ui.loading import (
    CardNotReady as _CardNotReady,
)
from .browser_ui.loading import (
    CardWaitSignal as _CardWaitSignal,
)
from .browser_ui.loading import (
    LoadingObserver,
)
from .browser_ui.loading import (
    RenderObservation as _RenderObservation,
)
from .browser_ui.scrolling import NativeWheelScrollEngine, ScrollGestureResult
from .browser_ui.session import (
    HomeSession,
    classify_playwright_error_kind,
    classify_playwright_failure,
    emit_failure_diagnostic,
)
from .errors import UiReadError
from .feed.models import (
    BodyReadEvidence,
    DisplayObservation,
    HomeSample,
    PostTarget,
)
from .feed.relevance import canonical_terms, classify_body, normalize_text
from .feed.urls import (
    _USERNAME,
    HOME_URL,
    THREADS_ORIGIN,
    _post_url,
    _post_url_hint,
    _post_url_rejection_reason,
    _post_url_structure,
    _profile_author,
    _rejected_post_link_blocks_candidate,  # noqa: F401 - retained reader-module compatibility name
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
_MAX_PERMALINK_CANDIDATES = 40
_MAX_FEED_ROOTS_PER_TYPE = 40
_MAX_VISIBLE_ROW_CANDIDATES = 40
_UNSUPPORTED_CONFIRMATION_SCANS = 3
_PROFILE_NAME = re.compile(r"^(?:profile|your profile|account)$", re.IGNORECASE)
_MORE_NAME = re.compile(r"^(?:more|see more|read more)$", re.IGNORECASE)
_ReadinessResult = TypeVar("_ReadinessResult")



@dataclass(frozen=True, slots=True)
class _HomeRowObservation:
    diagnostic: _CandidateDiagnostic
    target: PostTarget | None = None
    display: DisplayObservation | None = None
    fingerprint: str | None = None
    candidate: bool = True


@dataclass(frozen=True, slots=True, repr=False)
class _TrackedRowIdentity:
    """Keep transient DOM identity, observation ID, and post fingerprint distinct."""

    dom_handle: JSHandle = field(repr=False)
    observation_id: str = field(repr=False)
    fingerprint: str | None = field(default=None, repr=False)
    author_fingerprint: str | None = field(default=None, repr=False)
    body_verified: bool = False

    def __repr__(self) -> str:
        return "TrackedRowIdentity(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class _ScanFailureSite:
    stage: str
    viewport_index: int | None
    candidate_index: int | None
    operation: str
    root_handle: JSHandle = field(repr=False)
    canonical_identity: str | None = field(repr=False)
    identity_source: str
    author_fingerprint: str | None = field(repr=False)
    carried_identity: _TrackedRowIdentity | None = field(repr=False)
    identity_conflict: bool
    box: tuple[float, float, float, float] = field(repr=False)
    viewport_size: tuple[int, int] = field(repr=False)
    scroll_position: tuple[float, float, float] = field(repr=False)


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
    candidate_identities: tuple[_TrackedRowIdentity, ...] = field(default=(), repr=False)


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
    candidate_identities: tuple[_TrackedRowIdentity, ...] = field(default=(), repr=False)


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
        self._loading_observer = LoadingObserver(page)
        self._body_text = BodyTextReader(page)
        self._scroll_engine = NativeWheelScrollEngine(page)
        self._expected_actor = None if expected_actor is None else expected_actor.casefold()
        self._readiness_prompt = readiness_prompt
        self._detail_mode = detail_mode
        self._failure_phase = "INITIALIZING"
        self._failure_operation = "SETTLE"
        self._scan_stage: str | None = None
        self._scan_viewport_index: int | None = None
        self._scan_candidate_index: int | None = None
        self._scan_operation = "DISCOVER_ROOTS"
        self._scan_failure_site: _ScanFailureSite | None = None
        self._active_scan_candidate_handles: dict[int, JSHandle] | None = None
        self.last_scan_retry_count = 0
        self._home_url: str | None = None
        self._verified_actor_route: str | None = None
        self._last_ui_transition_at: float | None = None
        self.scroll_attempts = 0
        self.scrolls = 0
        self.feed_duplicates = 0
        self.last_scan_counts = _FeedScanCounts()
        self._feed_rows = FeedRowScanner(page)
        self._card_provenance = CardProvenanceReader(
            self._feed_rows,
            self._body_text,
            self._loading_observer,
        )
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
        self._last_viewport_candidate_identities: tuple[_TrackedRowIdentity, ...] = ()
        self._carried_viewport_candidate_identities: tuple[_TrackedRowIdentity, ...] = ()
        self._pending_viewport_candidate_identities: tuple[_TrackedRowIdentity, ...] = ()
        self._detail_return_anchor: str | None = None
        self._detail_return_target: PostTarget | None = None
        self._home_session = HomeSession(
            self, readiness_timeout_ms=_READINESS_TIMEOUT_MS
        )
        self._detail_navigator = DetailNavigator(
            self,
            timeout_ms=_DETAIL_TIMEOUT_MS,
        )
        self._reported_render_rescans: set[tuple[str, float | None]] = set()
        self._skipped_candidate_identities: set[str] = set()
        self._reported_candidate_skips: dict[tuple[str, str], set[str]] = {}

    @property
    def _cached_home_bodies(self) -> dict[str, tuple[str, bool]]:
        return self._card_provenance.cached_home_bodies

    @_cached_home_bodies.setter
    def _cached_home_bodies(self, value: dict[str, tuple[str, bool]]) -> None:
        self._card_provenance.cached_home_bodies = value

    def _mark_ui_transition(self) -> None:
        self._last_ui_transition_at = time.monotonic()

    def set_failure_context(self, phase: str, operation: str) -> None:
        self._failure_phase = phase
        self._failure_operation = operation

    def set_scan_viewport_index(self, viewport_index: int | None) -> None:
        self._scan_viewport_index = viewport_index

    def _set_scan_operation(self, operation: str) -> None:
        self._scan_operation = operation
        if self._scan_failure_site is not None:
            self._scan_failure_site = replace(
                self._scan_failure_site,
                operation=operation,
            )

    def refresh_viewport_body_coverage_counts(self) -> None:
        visited: set[str] = set()
        lookahead: set[str] = set()
        for diagnostic in self.last_candidate_diagnostics:
            if diagnostic.state in {"NON_POST", "VERIFIED"}:
                continue
            identity = (
                diagnostic.identity_fingerprint
                or diagnostic.observation_id
                or f"row:{diagnostic.row_index}"
            )
            evidence = diagnostic.read_evidence
            if evidence is None:
                continue
            body_has_entered_clip = (
                (evidence.visible_text_rect_count or 0) > 0
                or (evidence.visited_range_count or 0) > 0
            )
            if body_has_entered_clip:
                visited.add(identity)
            elif (
                evidence.closest_text_side == "BELOW"
                and evidence.closest_text_distance_px is not None
            ):
                lookahead.add(identity)
        lookahead.difference_update(visited)
        self.last_scan_counts.unresolved_visited_rows = len(visited)
        self.last_scan_counts.lookahead_rows = len(lookahead)

    async def _scan_position(self) -> tuple[float, float, float]:
        values = await self._page.evaluate(
            """() => [window.scrollX, window.scrollY,
                document.scrollingElement?.scrollTop ?? 0]"""
        )
        return (float(values[0]), float(values[1]), float(values[2]))

    async def _capture_scan_failure_site(
        self,
        root: Locator,
        root_handle: JSHandle,
        viewport: dict[str, int],
        candidate_index: int,
        carried_identity: _TrackedRowIdentity | None = None,
    ) -> None:
        self._scan_candidate_index = candidate_index
        self._set_scan_operation("STRUCTURE")
        evidence = await self._feed_rows._candidate_identity_evidence(root, root_handle)
        box = await root.bounding_box()
        position = await self._scan_position()
        if not evidence.root_matches or box is None:
            self._scan_failure_site = None
            return
        identity_source = "NONE"
        identity = evidence.canonical_fingerprint
        author_fingerprint = (
            next(iter(evidence.author_fingerprints))
            if len(evidence.author_fingerprints) == 1
            else None
        )
        identity_conflict = False
        if identity is not None:
            identity_source = "CURRENT_CANONICAL"
            if carried_identity is not None and carried_identity.fingerprint is not None:
                identity_conflict = (
                    identity != carried_identity.fingerprint
                    or evidence.conflicts_with(
                        carried_identity.fingerprint,
                        carried_identity.author_fingerprint,
                    )
                )
        elif (
            carried_identity is not None
            and carried_identity.body_verified
            and carried_identity.fingerprint is not None
        ):
            if not evidence.conflicts_with(
                carried_identity.fingerprint,
                carried_identity.author_fingerprint,
            ):
                identity = carried_identity.fingerprint
                author_fingerprint = carried_identity.author_fingerprint
                identity_source = "CARRIED_VERIFIED"
            else:
                identity_conflict = True
        self._scan_failure_site = _ScanFailureSite(
            stage=self._scan_stage or "FEED_INITIAL",
            viewport_index=self._scan_viewport_index,
            candidate_index=candidate_index,
            operation=self._scan_operation,
            root_handle=root_handle,
            canonical_identity=identity,
            identity_source=identity_source,
            author_fingerprint=author_fingerprint,
            carried_identity=carried_identity,
            identity_conflict=identity_conflict,
            box=(float(box["x"]), float(box["y"]), float(box["width"]), float(box["height"])),
            viewport_size=(viewport["width"], viewport["height"]),
            scroll_position=position,
        )

    async def _revalidate_scan_failure(self) -> str:
        site = self._scan_failure_site
        if self._page.is_closed():
            return "CLOSED"
        if site is None:
            return "INSUFFICIENT"
        if not _same_home_origin(self._page.url):
            return "CHANGED"
        if site.identity_conflict:
            return "CHANGED"
        try:
            deadline = self._readiness_deadline(_READINESS_TIMEOUT_MS)
            await self._wait_for_verified_feed(
                deadline,
                timeout_code="SCAN_ROW_UNVERIFIED",
            )
            if not await self._visible_feed_evidence():
                return "CHANGED"
            viewport = self._page.viewport_size
            if viewport is None or (
                viewport["width"], viewport["height"]
            ) != site.viewport_size:
                return "CHANGED"
            position = await self._scan_position()
            if any(abs(current - previous) > 1 for current, previous in zip(
                position, site.scroll_position, strict=True
            )):
                return "CHANGED"
            region = await self._content_region()
            roots, _ = await self._top_level_candidate_roots(region, viewport)
            matching: list[Locator] = []
            for root in roots:
                if await root.evaluate(
                    "(node, expected) => node === expected",
                    site.root_handle,
                ):
                    matching.append(root)
            if len(matching) != 1:
                return "CHANGED"
            root = matching[0]
            if not await root.is_visible() or not await self._is_in_visible_scrollports(root):
                return "CHANGED"
            box = await root.bounding_box()
            if box is None or any(
                abs(current - previous) > 1
                for current, previous in zip(
                    (float(box["x"]), float(box["y"]),
                     float(box["width"]), float(box["height"])),
                    site.box,
                    strict=True,
                )
            ):
                return "CHANGED"
            evidence = await self._feed_rows._candidate_identity_evidence(
                root,
                site.root_handle,
            )
            if not evidence.root_matches:
                return "CHANGED"
            if site.identity_source == "CURRENT_CANONICAL":
                if site.canonical_identity is None:
                    return "INSUFFICIENT"
                if evidence.conflicts_with(
                    site.canonical_identity,
                    site.author_fingerprint,
                ):
                    return "CHANGED"
                if evidence.canonical_fingerprint != site.canonical_identity:
                    return "CHANGED"
                carried = site.carried_identity
                if carried is not None and carried.fingerprint is not None and (
                    carried.fingerprint != site.canonical_identity
                    or evidence.conflicts_with(
                        carried.fingerprint,
                        carried.author_fingerprint,
                    )
                ):
                    return "CHANGED"
                return "REUSED"
            if site.identity_source == "CARRIED_VERIFIED":
                carried = site.carried_identity
                if (
                    carried is None
                    or not any(
                        identity is carried
                        for identity in self._last_viewport_candidate_identities
                    )
                    or not carried.body_verified
                    or carried.fingerprint is None
                    or carried.fingerprint != site.canonical_identity
                ):
                    return "INSUFFICIENT"
                if evidence.conflicts_with(
                    site.canonical_identity,
                    site.author_fingerprint,
                ):
                    return "CHANGED"
                return "REUSED"
            return "INSUFFICIENT"
        except PlaywrightError as error:
            failure = classify_playwright_failure(self._page, error)
            return "CLOSED" if failure.error_class == "TARGET_CLOSED" else "INSUFFICIENT"
        except UiReadError:
            return "INSUFFICIENT"

    async def _dispose_active_scan_candidate_handles(self) -> None:
        handles = self._active_scan_candidate_handles
        self._active_scan_candidate_handles = None
        if handles is None:
            return
        for handle in handles.values():
            try:
                await handle.dispose()
            except PlaywrightError:
                pass

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
            cause = "" if error.diagnostic_reason is None else f" cause={error.diagnostic_reason}"
            print(f"SKIPPED stage={stage} reason={error.reason} count={len(identities)}{cause}")

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
                    await self._require_feed_url(route_error_code="FOR_YOU_ROUTE_CHANGED")
                    controls = await self._visible_for_you_controls()
                    if len(controls) > 1:
                        raise UiReadError("FOR_YOU_CONTROL_AMBIGUOUS")
                    if not controls:
                        try:
                            await self._for_you_control(timeout_ms=remaining)
                        except UiReadError as error:
                            if error.code != "HOME_UI_UNVERIFIED":
                                raise
                            raise UiReadError("FOR_YOU_ACTIVATION_NOT_READY") from None
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
                                raise UiReadError("FOR_YOU_ACTIVATION_NOT_READY") from None

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
            if await body.get_attribute("aria-busy") == "true" or not normalize_text(
                await body.inner_text()
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
        self.last_scan_retry_count = 0
        initial_viewport = self.scrolls == 0
        targets, _ = await self._wait_for_home_targets(
            max_posts,
            timeout_code=(
                "FEED_INITIAL_NOT_READY" if initial_viewport else "FEED_SCROLL_NOT_READY"
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
                (
                    point,
                    wheel_delta,
                    viewport_height,
                    hit_test_tag,
                ) = await self._safe_feed_scroll_point(surface)
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
            except PlaywrightError as error:
                failure = classify_playwright_failure(self._page, error)
                emit_failure_diagnostic(
                    phase=self._failure_phase,
                    operation="SCROLL",
                    error_class=failure.error_class,
                    page_closed=failure.page_closed,
                    context_closed=failure.context_closed,
                )
                if not self._detail_mode:
                    print(f"SCROLL attempt={self.scroll_attempts} observed=0")
                code = (
                    "BROWSER_SESSION_UNAVAILABLE"
                    if failure.error_class == "TARGET_CLOSED"
                    else "UI_READ_UNVERIFIED"
                )
                raise UiReadError(code) from None
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
                    if observation is None or not self._has_new_viewport_content(
                        previous_signature,
                        observation.signature,
                        observed_delta,
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
                await self._remember_viewport_candidates(
                    rendered.candidate_roots,
                    rendered.candidate_identities,
                )
                return observed_delta

            try:
                observed_delta = await self._recoverable_stage(
                    "HOME_SCROLL",
                    "HOME_SCROLL_UNVERIFIED",
                    observe_scroll,
                    operation="SETTLE",
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
                "scroll_top_delta": (float(final_surface["top"]) - float(scroll_state["top"])),
                "scroll_height_delta": (
                    float(final_surface["scrollHeight"]) - float(scroll_state["scrollHeight"])
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
                "SMALL" if delta_ratio <= 0.5 else "MEDIUM" if delta_ratio <= 0.75 else "LARGE"
            )
            self.last_scroll_band = delta_band
            if not self._detail_mode:
                print(f"SCROLL attempt={self.scroll_attempts} observed=1 delta_band={delta_band}")
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
        advanced = await self.advance_feed()
        if not advanced and self.last_scroll_stop_reason == "FEED_NOT_SCROLLABLE":
            raise UiReadError("HOME_SCROLL_SURFACE_UNVERIFIED")
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
        operation: str | None = None,
        cancel_code: str | None = None,
    ) -> _ReadinessResult:
        allowed_codes = recoverable_codes or (timeout_code,)
        window = 0
        final_code = timeout_code
        dom_change_retried = False
        detail_ui_retried = False
        failure_operation = operation or self._failure_operation
        while True:
            deadline = self._readiness_deadline(_READINESS_TIMEOUT_MS)
            try:
                result = await attempt(deadline)
            except PlaywrightError as error:
                failure = classify_playwright_failure(self._page, error)
                if failure_operation == "SCAN_ROW":
                    row_identity = await self._revalidate_scan_failure()
                    failure_site = self._scan_failure_site
                    operation_name = self._scan_operation
                    if operation_name not in {
                        "DISCOVER_ROOTS",
                        "STRUCTURE",
                        "BOUNDING_BOX",
                        "READ_CARD",
                        "READ_BODY",
                        "STABILITY_CHECK",
                        "EMIT_ROW",
                        "FINALIZE",
                    }:
                        operation_name = "SCAN_ROW"
                    emit_failure_diagnostic(
                        phase=self._failure_phase,
                        operation=operation_name,
                        error_class=failure.error_class,
                        page_closed=failure.page_closed,
                        context_closed=failure.context_closed,
                        stage=self._scan_stage,
                        viewport_index=self._scan_viewport_index,
                        candidate_index=self._scan_candidate_index,
                        error_kind=classify_playwright_error_kind(error),
                        row_identity=(
                            row_identity if row_identity in {"REUSED", "CHANGED"} else "UNKNOWN"
                        ),
                        body_substep=(
                            self._body_text.last_failure_substep
                            if operation_name == "READ_BODY"
                            else None
                        ),
                        identity_source=(
                            "NONE" if failure_site is None else failure_site.identity_source
                        ),
                        retry_eligibility=(
                            row_identity
                            if row_identity in {"REUSED", "CHANGED"}
                            else "INSUFFICIENT"
                        ),
                    )
                    if (
                        failure.error_class == "TARGET_CLOSED"
                        or row_identity == "CLOSED"
                    ):
                        await self._dispose_active_scan_candidate_handles()
                        self._scan_failure_site = None
                        self._scan_stage = None
                        raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
                    if (
                        row_identity == "REUSED"
                        and self.last_scan_retry_count < 1
                    ):
                        await self._dispose_active_scan_candidate_handles()
                        self._scan_failure_site = None
                        self.last_scan_retry_count += 1
                        window += 1
                        print(f"WAITING stage={stage} retry={window}")
                        continue
                    await self._dispose_active_scan_candidate_handles()
                    self._scan_failure_site = None
                    self._scan_stage = None
                    raise UiReadError("SCAN_ROW_UNVERIFIED") from None
                emit_failure_diagnostic(
                    phase=self._failure_phase,
                    operation=failure_operation,
                    error_class=failure.error_class,
                    page_closed=failure.page_closed,
                    context_closed=failure.context_closed,
                    error_kind=classify_playwright_error_kind(error),
                )
                if failure.error_class == "TARGET_CLOSED":
                    raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
                if (
                    failure.error_class == "DOM_DETACHED"
                    and failure_operation in {"SCAN_ROW", "SETTLE", "DETAIL"}
                ):
                    if failure_operation == "DETAIL" and detail_ui_retried:
                        raise UiReadError("DETAIL_ROOT_UNVERIFIED") from None
                    if failure_operation != "DETAIL" and dom_change_retried:
                        raise UiReadError("DOM_CHANGED_DURING_SCAN") from None
                    if failure_operation == "DETAIL":
                        detail_ui_retried = True
                    else:
                        dom_change_retried = True
                    window += 1
                    print(f"WAITING stage={stage} retry={window}")
                    continue
                if (
                    failure_operation == "DETAIL"
                    and not detail_ui_retried
                    and _same_home_origin(self._page.url)
                ):
                    detail_ui_retried = True
                    window += 1
                    print(f"WAITING stage={stage} retry={window}")
                    continue
                raise UiReadError("UI_READ_UNVERIFIED") from None
            except UiReadError as error:
                if failure_operation == "SCAN_ROW":
                    await self._dispose_active_scan_candidate_handles()
                    self._scan_failure_site = None
                    self._scan_stage = None
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
                raise UiReadError(cancel_code or final_code) from None

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

        profile_href = await profiles[0].get_attribute("href") if profiles else None
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
        observe: Callable[[], Awaitable[_RenderObservation[_ReadinessResult] | None]],
        validate: Callable[[], Awaitable[None]] | None = None,
    ) -> _ReadinessResult:
        return await self._loading_observer.wait_for_render_settled(
            stage,
            deadline,
            timeout_code=timeout_code,
            observe=observe,
            remaining_ms=self._remaining_readiness_ms,
            transition_at=self._last_ui_transition_at,
            min_dwell_ms=_RENDER_MIN_DWELL_MS,
            settle_interval_ms=_RENDER_SETTLE_INTERVAL_MS,
            stable_observations=_RENDER_STABLE_OBSERVATIONS,
            validate=validate,
            report=lambda observation, checks: self._report_render_settled(
                stage,
                observation,
                checks,
            ),
        )

    def _report_render_settled(
        self,
        stage: str,
        observation: _RenderObservation[object],
        checks: int,
    ) -> None:
        coverage_complete = str(getattr(observation.value, "coverage_complete", False)).lower()
        ready_state = observation.ready_state
        if ready_state == "FEED_VISIBLE_UNVERIFIED":
            print(
                f"OBSERVED stage={stage} state=FEED_VISIBLE_UNVERIFIED verified=0 "
                "identity=OBSERVED_UNVERIFIED "
                f"observed_rows={getattr(observation.value, 'observed_rows', 0)} "
                f"body_extracted={self.last_scan_counts.body_extracted} "
                f"viewport_coverage_complete={coverage_complete} render_checks={checks}"
            )
        elif ready_state == "OBSERVATION_UNCERTAIN":
            print(
                f"OBSERVED stage={stage} state=OBSERVATION_UNCERTAIN "
                f"verified={self.last_scan_counts.verified} "
                f"observed_rows={self.last_scan_counts.observed_rows} "
                f"uncertain_rows={self.last_scan_counts.uncertain_rows} "
                f"viewport_coverage_complete={coverage_complete} render_checks={checks}"
            )
        elif ready_state == "DISPLAY_OBSERVATION":
            print(
                f"OBSERVED stage={stage} state=DISPLAY_OBSERVATION "
                f"verified={self.last_scan_counts.verified} "
                f"observed_rows={self.last_scan_counts.observed_rows} "
                f"body_extracted={self.last_scan_counts.body_extracted} "
                f"viewport_coverage_complete={coverage_complete} render_checks={checks}"
            )
        elif ready_state == "BODY_EXTRACTED":
            print(
                f"READY stage={stage} state=BODY_EXTRACTED "
                f"verified={self.last_scan_counts.verified} "
                f"observed_rows={self.last_scan_counts.observed_rows} "
                f"body_extracted={self.last_scan_counts.body_extracted} "
                f"viewport_coverage_complete={coverage_complete} render_checks={checks}"
            )
        elif ready_state == "UI_SHELL_SETTLED":
            print(f"READY stage={stage} state=UI_SHELL_SETTLED render_checks={checks}")
        elif ready_state == "FEED_EVIDENCE":
            print(f"OBSERVED stage={stage} state=FEED_EVIDENCE render_checks={checks}")
        elif ready_state == "DETAIL_VERIFIED":
            print(f"READY stage={stage} state=DETAIL_VERIFIED render_checks={checks}")
        else:
            print(f"READY stage={stage} render_checks={checks}")

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
            scan.candidate_identities,
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
            self.last_scan_timings = _FeedScanTimings(wait_reason="FOR_YOU_SHELL_NOT_READY")
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
                        "FEED_READINESS_SIGNAL" if changed else "FEED_READINESS_TIMEOUT"
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
                        self.last_scan_timings.wait_reason = "NO_VERIFIABLE_ROW_EVIDENCE"
                    raise UiReadError(timeout_code)
            return scan

        scan = await self._recoverable_stage(
            stage_name,
            timeout_code,
            attempt,
            recoverable_codes=(timeout_code,),
            operation="SCAN_ROW",
        )
        self.last_display_observations = scan.display_observations
        self.last_viewport_coverage_complete = scan.coverage_complete
        self.last_viewport_observed_rows = scan.observed_rows
        self.last_scan_counts.coverage_complete = scan.coverage_complete
        self._last_viewport_signature = scan.signature
        await self._remember_viewport_candidates(
            scan.candidate_roots,
            scan.candidate_identities,
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
            and not any(
                diagnostic.reason
                in {
                    "POST_ROOT_UNRESOLVED",
                    "POST_ROOT_IDENTITY_CHANGED",
                    "ROW_PROVENANCE_UNRESOLVED",
                }
                for diagnostic in self.last_candidate_diagnostics
            )
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
        elif counts.body_extracted and not counts.verified and scan.coverage_complete:
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
        return await self._loading_observer.wait_for_card_signal(
            pending,
            deadline,
            recheck_interval_ms=_READINESS_RECHECK_INTERVAL_MS,
            max_wait_ms=max_wait_ms,
        )

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
        row_handles = [
            identity.dom_handle for identity in self._last_viewport_candidate_identities
        ]
        try:
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

    async def _is_carried_viewport_candidate(
        self, root: Locator
    ) -> _TrackedRowIdentity | None:
        identities = self._carried_viewport_candidate_identities
        if not identities:
            return None
        matched_index = await root.evaluate(
            "(node, previous) => previous.findIndex(item => node === item)",
            [identity.dom_handle for identity in identities],
        )
        if matched_index < 0:
            return None
        if matched_index >= len(identities):
            return None
        return identities[matched_index]

    async def _same_dom_element(self, root: Locator, handle: JSHandle) -> bool:
        return await self._card_provenance._same_dom_element(root, handle)

    async def _discard_pending_viewport_candidates(self) -> None:
        retained = {id(identity.dom_handle) for identity in self._last_viewport_candidate_identities}
        for identity in self._pending_viewport_candidate_identities:
            if id(identity.dom_handle) not in retained:
                await identity.dom_handle.dispose()
        self._pending_viewport_candidate_identities = ()

    async def _remember_viewport_candidates(
        self,
        roots: tuple[Locator, ...],
        identities: tuple[_TrackedRowIdentity, ...],
    ) -> None:
        retained = {id(identity.dom_handle) for identity in identities}
        previous = (
            *self._last_viewport_candidate_identities,
            *self._pending_viewport_candidate_identities,
        )
        self._last_viewport_candidate_roots = roots
        self._last_viewport_candidate_identities = identities
        self._pending_viewport_candidate_identities = ()
        for identity in previous:
            if id(identity.dom_handle) not in retained:
                await identity.dom_handle.dispose()

    async def _visible_targets(
        self,
        max_posts: int,
        *,
        currently_visible: bool = False,
        prefer_new_fingerprints: set[str] | None = None,
        stage: str = "FEED",
        on_row: Callable[[_HomeRowObservation], Awaitable[None]] | None = None,
    ) -> _VisibleTargets:
        self._scan_stage = stage
        self._scan_candidate_index = None
        self._scan_failure_site = None
        self._set_scan_operation("DISCOVER_ROOTS")
        await self._dispose_active_scan_candidate_handles()
        await self._discard_pending_viewport_candidates()
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
        counts.unrepresented_root_count = 0
        counts.unresolved_visited_rows = 0
        counts.lookahead_rows = 0
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
        counts.unrepresented_root_count = int(roots_truncated)
        self._set_scan_operation("STRUCTURE")
        structures = await self._collect_structure_diagnostics(
            region,
            roots,
            viewport,
        )
        self.last_scan_timings.discover_ms = round((time.perf_counter() - discover_started) * 1000)

        async def emit_row(
            diagnostic: _CandidateDiagnostic,
            *,
            target: PostTarget | None = None,
            display: DisplayObservation | None = None,
            fingerprint: str | None = None,
            candidate: bool = True,
        ) -> None:
            if target is not None:
                diagnostic.identity_fingerprint = target.fingerprint
                diagnostic.author_fingerprint = sha256(target.author.encode()).hexdigest()
            self.last_candidate_diagnostics = tuple(diagnostics)
            self.refresh_viewport_body_coverage_counts()
            if on_row is not None:
                self._set_scan_operation("EMIT_ROW")
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
            tuple[
                int,
                Locator,
                _CandidateStructure,
                _TrackedRowIdentity | None,
                _CandidateDiagnostic,
                str,
            ]
        ] = []
        candidate_dom_handles: dict[int, JSHandle] = {}
        self._active_scan_candidate_handles = candidate_dom_handles
        candidate_prior_fingerprints: dict[int, str | None] = {}
        matched_previous_identity_handles: set[int] = set()
        diagnostics: list[_CandidateDiagnostic] = []
        for index, root in enumerate(roots):
            self._scan_candidate_index = index + 1
            self._set_scan_operation("STRUCTURE")
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
                structure.body_candidates or structure.offscreen_body_candidates
            ) and (
                structure.visible_times
                or structure.offscreen_times
                or structure.profile_anchor_candidates
                or structure.visible_post_links
                or structure.offscreen_profile_anchors
                or structure.offscreen_post_links
            )
            semantic_row = (
                semantic_evidence
                and (
                    root_kind != "PRESSABLE"
                    or structure.visible_times
                    or structure.visible_post_links
                )
                and root_kind in {"ARTICLE", "ROLE_ARTICLE", "PRESSABLE"}
            )
            unresolved_post_root = bool(
                (structure.visible_post_links and not structure.canonical_post_links)
                or (structure.canonical_post_links and not structure.profile_anchor_candidates)
            )
            carried_identity = await self._is_carried_viewport_candidate(root)
            carried_row = carried_identity is not None
            if carried_identity is not None:
                matched_previous_identity_handles.add(id(carried_identity.dom_handle))
                diagnostic.identity_fingerprint = carried_identity.fingerprint
                diagnostic.author_fingerprint = carried_identity.author_fingerprint
            if has_row_cues or semantic_row or carried_row:
                root_handle = await root.element_handle()
                if root_handle is not None:
                    candidate_dom_handles[index] = root_handle
                candidate_prior_fingerprints[index] = (
                    None if carried_identity is None else carried_identity.fingerprint
                )
                diagnostic.state = "PENDING"
                diagnostic.reason = "POST_ROW_REQUIRES_VERIFICATION"
                diagnostic.observation_id = (
                    secrets.token_hex(8)
                    if carried_identity is None
                    else carried_identity.observation_id
                )
                candidate_rows.append(
                    (
                        index,
                        root,
                        structure,
                        carried_identity,
                        diagnostic,
                        diagnostic.observation_id,
                    )
                )
            elif unresolved_post_root:
                diagnostic.state = "DEFERRED_IDENTITY"
                diagnostic.reason = "POST_ROOT_UNRESOLVED"
                diagnostic.observation_id = secrets.token_hex(8)
                self._record_structure_reason("POST_ROOT_UNRESOLVED")
        self.last_candidate_diagnostics = tuple(diagnostics)
        self.refresh_viewport_body_coverage_counts()
        unresolved_root_count = sum(
            diagnostic.reason == "POST_ROOT_UNRESOLVED" for diagnostic in diagnostics
        )
        counts.observed_rows = len(candidate_rows) + unresolved_root_count
        counts.coverage_complete = (
            not roots_truncated
            and counts.observed_rows <= max_posts
            and unresolved_root_count == 0
            and not any(structure.visible_times > 1 for _, _, structure, _, _, _ in candidate_rows)
        )
        targets: list[PostTarget] = []
        scroll_anchor: dict[str, float] | None = None
        display_observations: list[DisplayObservation] = []
        pending_signals: list[_CardWaitSignal] = []
        scanned_fingerprints: set[str] = set()
        candidate_by_index = {
            index: (root, structure, carried_identity, diagnostic, observation_id)
            for index, root, structure, carried_identity, diagnostic, observation_id in candidate_rows
        }
        processed_candidates = 0
        for row_index, diagnostic in enumerate(diagnostics):
            self._scan_candidate_index = row_index + 1
            self._set_scan_operation("STRUCTURE")
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
            root, structure, carried_identity, diagnostic, _ = candidate
            carried_row = carried_identity is not None
            root_handle = candidate_dom_handles.get(row_index)
            if root_handle is None:
                counts.skipped_rows += 1
                counts.uncertain_rows += 1
                counts.coverage_complete = False
                diagnostic.state = "REJECTED"
                diagnostic.reason = "POST_ROOT_IDENTITY_UNAVAILABLE"
                await emit_row(diagnostic)
                continue
            await self._capture_scan_failure_site(
                root,
                root_handle,
                viewport,
                row_index + 1,
                carried_identity=carried_identity,
            )
            self._set_scan_operation("STRUCTURE")
            if not await self._same_dom_element(root, root_handle):
                replacement_handle = await root.element_handle()
                await root_handle.dispose()
                if replacement_handle is None:
                    candidate_dom_handles.pop(row_index, None)
                    counts.skipped_rows += 1
                    counts.uncertain_rows += 1
                    counts.coverage_complete = False
                    diagnostic.state = "REJECTED"
                    diagnostic.reason = "POST_ROOT_IDENTITY_CHANGED"
                    await emit_row(diagnostic)
                    continue
                root_handle = replacement_handle
                candidate_dom_handles[row_index] = replacement_handle
                candidate_prior_fingerprints[row_index] = None
                carried_row = False
                diagnostic.observation_id = secrets.token_hex(8)
                diagnostic.identity_fingerprint = None
                diagnostic.author_fingerprint = None
                structure = await self._inspect_candidate_root(root, viewport)
                diagnostic.root_kind = structure.root_kind
                diagnostic.has_visible_time = structure.visible_times > 0
                diagnostic.has_post_permalink = structure.visible_post_links > 0
                diagnostic.has_profile_author = structure.profile_anchor_candidates > 0
                diagnostic.has_readable_body = structure.readable_body_candidates > 0
                if not await self._same_dom_element(root, root_handle):
                    counts.skipped_rows += 1
                    counts.uncertain_rows += 1
                    counts.coverage_complete = False
                    diagnostic.state = "REJECTED"
                    diagnostic.reason = "POST_ROOT_IDENTITY_CHANGED"
                    await emit_row(diagnostic)
                    continue
            self._set_scan_operation("BOUNDING_BOX")
            box = await root.bounding_box()
            if box is None:
                counts.skipped_rows += 1
                counts.uncertain_rows += 1
                diagnostic.state = "REJECTED"
                diagnostic.reason = "POST_ROOT_NOT_VISIBLE"
                display_observations.append(DisplayObservation(None, "OBSERVATION_UNCERTAIN"))
                await emit_row(
                    diagnostic,
                    display=display_observations[-1],
                )
                continue
            counts.inspected += 1
            if scroll_anchor is None:
                scroll_anchor = box
            if (
                not (
                    structure.profile_anchor_candidates
                    or structure.visible_post_links
                    or structure.offscreen_post_links
                    or structure.offscreen_profile_anchors
                )
                and not carried_row
            ):
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
            if structure.visible_times > 1:
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
                        expected_root_handle=root_handle,
                        allow_hydration_retry=structure.readable_body_candidates == 0,
                    )
                    self.last_scan_timings.stabilize_ms += round(
                        (time.perf_counter() - stabilize_started) * 1000
                    )
                    if not body_stable:
                        diagnostic.state = "OBSERVED_UNVERIFIED"
                        diagnostic.reason = "ROW_CONTENT_CHANGED_DURING_READ"
                        diagnostic.identity_fingerprint = target.fingerprint
                        if candidate_prior_fingerprints.get(row_index) not in {
                            None,
                            target.fingerprint,
                        }:
                            diagnostic.observation_id = secrets.token_hex(8)
                            carried_row = False
                        candidate_prior_fingerprints[row_index] = target.fingerprint
                        diagnostic.read_evidence = target.read_evidence
                        diagnostic.has_readable_body = bool(target.body and target.body.strip())
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
                if pending.reason_code == "POST_ROOT_DOM_IDENTITY_CHANGED":
                    replacement_handle = await root.element_handle()
                    previous_handle = candidate_dom_handles.get(row_index)
                    if previous_handle is not None:
                        await previous_handle.dispose()
                    if replacement_handle is None:
                        candidate_dom_handles.pop(row_index, None)
                    else:
                        candidate_dom_handles[row_index] = replacement_handle
                    candidate_prior_fingerprints[row_index] = None
                    diagnostic.observation_id = secrets.token_hex(8)
                    diagnostic.identity_fingerprint = None
                    diagnostic.author_fingerprint = None
                    carried_row = False
                self.last_scan_timings.read_row_ms += round(
                    (time.perf_counter() - read_started) * 1000
                )
                counts.card_not_ready += 1
                body, body_signals, read_evidence = await self._observe_display_body(root)
                diagnostic.read_evidence = read_evidence
                diagnostic.has_readable_body = bool(body)
                should_wait_for_permalink = (
                    pending.reason_code == "POST_PERMALINK_PENDING"
                    and diagnostic.root_kind in {"ARTICLE", "ROLE_ARTICLE", "PRESSABLE"}
                )
                should_wait_for_missing_author = pending.reason_code == "POST_AUTHOR_PENDING"
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
                    if await signal.locator.count() == 0 and await self._is_in_visible_scrollports(
                        root
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
                                self.last_scan_timings.wait_reason = "CARD_BODY_NOT_STABLE"
                                await emit_row(
                                    diagnostic,
                                    display=display_observations[-1]
                                    if display_observations
                                    else None,
                                )
                                continue
                        try:
                            if currently_visible:
                                target, body_stable = await self._read_stable_visible_card(
                                    root,
                                    viewport_y=box["y"],
                                    cached_body_fingerprints=(prefer_new_fingerprints),
                                    expected_root_handle=candidate_dom_handles.get(row_index),
                                    allow_hydration_retry=(
                                        structure.readable_body_candidates == 0
                                    ),
                                )
                                if not body_stable:
                                    self.last_scan_timings.wait_reason = "CARD_BODY_NOT_STABLE"
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
                            if candidate_prior_fingerprints.get(row_index) not in {
                                None,
                                target.fingerprint,
                            }:
                                diagnostic.observation_id = secrets.token_hex(8)
                                carried_row = False
                            candidate_prior_fingerprints[row_index] = target.fingerprint
                            diagnostic.read_evidence = target.read_evidence
                            diagnostic.has_readable_body = bool(target.body and target.body.strip())
                            if target.body_complete:
                                diagnostic.state = "VERIFIED"
                                diagnostic.reason = "PROVENANCE_VERIFIED"
                            else:
                                diagnostic.state, diagnostic.reason = _body_progress_state(
                                    target.read_evidence,
                                    has_readable_body=diagnostic.has_readable_body,
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
                                    self._record_structure_reason("POST_BODY_STRUCTURE_UNSUPPORTED")
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
                if (
                    error.diagnostic_reason == "CANDIDATE_WRAPPER_AMBIGUOUS"
                    and currently_visible
                ):
                    try:
                        target, body_stable = await self._recover_ambiguous_visible_card(
                            root,
                            root_handle,
                            viewport_y=box["y"],
                            cached_body_fingerprints=prefer_new_fingerprints,
                        )
                    except _CardNotReady as pending:
                        self._record_scan_skip(stage, error)
                        identity_changed = (
                            pending.reason_code == "POST_ROOT_DOM_IDENTITY_CHANGED"
                            or not await self._same_dom_element(root, root_handle)
                        )
                        if identity_changed:
                            diagnostic.observation_id = secrets.token_hex(8)
                            diagnostic.identity_fingerprint = None
                            diagnostic.author_fingerprint = None
                            candidate_prior_fingerprints[row_index] = None
                            carried_row = False
                            diagnostic.state = "REJECTED"
                            diagnostic.reason = "POST_ROOT_IDENTITY_CHANGED"
                            read_evidence = BodyReadEvidence(state="NO_BODY_NODES")
                            body = None
                        else:
                            observed = await self._read_visible_body_progress(root, root_handle)
                            read_evidence = observed.read_evidence
                            body = observed.body if observed.has_rendered_text else None
                            diagnostic.state = (
                                "OBSERVED_UNVERIFIED" if body else "REJECTED"
                            )
                            diagnostic.reason = "ROW_PROVENANCE_UNRESOLVED"
                            self._record_structure_reason("ROW_PROVENANCE_UNRESOLVED")
                        diagnostic.read_evidence = read_evidence
                        diagnostic.has_readable_body = bool(body)
                        counts.coverage_complete = False
                        counts.uncertain_rows += 1
                        counts.skipped_rows += 1
                        if body:
                            counts.body_extracted += 1
                        else:
                            counts.body_unknown += 1
                        display = DisplayObservation(
                            body,
                            "OBSERVATION_UNCERTAIN"
                            if body is None
                            else "OBSERVED_UNVERIFIED",
                            box["y"],
                            read_evidence=read_evidence,
                        )
                        display_observations.append(display)
                        self.last_scan_timings.read_row_ms += round(
                            (time.perf_counter() - read_started) * 1000
                        )
                        await emit_row(diagnostic, display=display)
                        continue
                    except (_CardDeferredOffscreen, _UnverifiedFeedCandidate):
                        self._record_scan_skip(stage, error)
                        identity_changed = not await self._same_dom_element(root, root_handle)
                        if identity_changed:
                            diagnostic.observation_id = secrets.token_hex(8)
                            diagnostic.identity_fingerprint = None
                            diagnostic.author_fingerprint = None
                            candidate_prior_fingerprints[row_index] = None
                            carried_row = False
                            diagnostic.state = "REJECTED"
                            diagnostic.reason = "POST_ROOT_IDENTITY_CHANGED"
                            read_evidence = BodyReadEvidence(state="NO_BODY_NODES")
                            body = None
                        else:
                            observed = await self._read_visible_body_progress(root, root_handle)
                            read_evidence = observed.read_evidence
                            body = observed.body if observed.has_rendered_text else None
                            diagnostic.state = (
                                "OBSERVED_UNVERIFIED" if body else "REJECTED"
                            )
                            diagnostic.reason = "ROW_PROVENANCE_UNRESOLVED"
                            self._record_structure_reason("ROW_PROVENANCE_UNRESOLVED")
                        diagnostic.read_evidence = read_evidence
                        diagnostic.has_readable_body = bool(body)
                        counts.coverage_complete = False
                        counts.uncertain_rows += 1
                        counts.skipped_rows += 1
                        if body:
                            counts.body_extracted += 1
                        else:
                            counts.body_unknown += 1
                        display = DisplayObservation(
                            body,
                            "OBSERVATION_UNCERTAIN"
                            if body is None
                            else "OBSERVED_UNVERIFIED",
                            box["y"],
                            read_evidence=read_evidence,
                        )
                        display_observations.append(display)
                        self.last_scan_timings.read_row_ms += round(
                            (time.perf_counter() - read_started) * 1000
                        )
                        await emit_row(diagnostic, display=display)
                        continue
                    if not body_stable:
                        diagnostic.identity_fingerprint = target.fingerprint
                        candidate_prior_fingerprints[row_index] = target.fingerprint
                        diagnostic.read_evidence = target.read_evidence
                        diagnostic.has_readable_body = bool(target.body and target.body.strip())
                        diagnostic.state = "OBSERVED_UNVERIFIED"
                        diagnostic.reason = "ROW_CONTENT_CHANGED_DURING_READ"
                        counts.identity_verified += 1
                        counts.uncertain_rows += 1
                        counts.skipped_rows += 1
                        counts.coverage_complete = False
                        if target.body:
                            counts.body_extracted += 1
                        else:
                            counts.body_unknown += 1
                        display = DisplayObservation(
                            target.body,
                            "OBSERVATION_UNCERTAIN"
                            if not target.body
                            else "OBSERVED_UNVERIFIED",
                            box["y"],
                            read_evidence=target.read_evidence,
                        )
                        display_observations.append(display)
                        await emit_row(
                            diagnostic,
                            target=target,
                            display=display,
                            fingerprint=target.fingerprint,
                        )
                        continue
                else:
                    self._record_scan_skip(stage, error)
                    body, body_signals, read_evidence = await self._observe_display_body(root)
                    diagnostic.read_evidence = read_evidence
                    diagnostic.has_readable_body = bool(body)
                    if body:
                        counts.body_extracted += 1
                        diagnostic.state = "OBSERVED_UNVERIFIED"
                        diagnostic.reason = (
                            "ROW_PROVENANCE_UNRESOLVED"
                            if error.diagnostic_reason == "CANDIDATE_WRAPPER_AMBIGUOUS"
                            else error.diagnostic_reason or error.reason
                        )
                        if error.diagnostic_reason == "CANDIDATE_WRAPPER_AMBIGUOUS":
                            counts.coverage_complete = False
                            counts.uncertain_rows += 1
                            counts.skipped_rows += 1
                            self._record_structure_reason("ROW_PROVENANCE_UNRESOLVED")
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
                        if error.diagnostic_reason == "CANDIDATE_WRAPPER_AMBIGUOUS":
                            counts.coverage_complete = False
                            self._record_structure_reason("ROW_PROVENANCE_UNRESOLVED")
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
                                diagnostic.reason = error.diagnostic_reason or error.reason
                        pending_signals.extend(body_signals)
                        display_observations.append(
                            DisplayObservation(
                                None,
                                "OBSERVATION_UNCERTAIN",
                                box["y"],
                                read_evidence=read_evidence,
                            )
                        )
                    self.last_scan_timings.read_row_ms += round(
                        (time.perf_counter() - read_started) * 1000
                    )
                    await emit_row(diagnostic, display=display_observations[-1])
                    continue
                if (
                    error.diagnostic_reason != "CANDIDATE_WRAPPER_AMBIGUOUS"
                    or not currently_visible
                ):
                    self.last_scan_timings.read_row_ms += round(
                        (time.perf_counter() - read_started) * 1000
                    )
                    await emit_row(diagnostic, display=display_observations[-1])
                    continue
            self.last_scan_timings.read_row_ms += round((time.perf_counter() - read_started) * 1000)
            diagnostic.identity_fingerprint = target.fingerprint
            if candidate_prior_fingerprints.get(row_index) not in {None, target.fingerprint}:
                diagnostic.observation_id = secrets.token_hex(8)
                carried_row = False
            candidate_prior_fingerprints[row_index] = target.fingerprint
            diagnostic.read_evidence = target.read_evidence
            diagnostic.state = "VERIFIED" if target.body_complete else "PARTIAL_BODY"
            diagnostic.reason = (
                "PROVENANCE_VERIFIED" if target.body_complete else "VISIBLE_TEXT_PROGRESS"
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
                    self._record_structure_reason("POST_BODY_STRUCTURE_UNSUPPORTED")
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
            if target.fingerprint in scanned_fingerprints or (
                prefer_new_fingerprints is not None
                and target.fingerprint in prefer_new_fingerprints
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
            counts.coverage_complete = not roots_truncated and counts.observed_rows <= max_posts
        scanned_fingerprints.update(target.fingerprint for target in targets)
        for identity in self._carried_viewport_candidate_identities:
            if (
                id(identity.dom_handle) in matched_previous_identity_handles
                or identity.fingerprint in scanned_fingerprints
            ):
                continue
            try:
                still_connected = bool(
                    await region.evaluate(
                        "(region, node) => node.isConnected && region.contains(node)",
                        identity.dom_handle,
                    )
                )
            except PlaywrightError:
                still_connected = False
            if not still_connected:
                continue
            diagnostic = _CandidateDiagnostic(
                row_index=len(diagnostics) + 1,
                root_kind="CARRIED",
                has_visible_time=False,
                has_post_permalink=False,
                has_profile_author=False,
                has_readable_body=False,
                state="DEFERRED_IDENTITY",
                reason="POST_ROW_CUES_MISSING",
                identity_fingerprint=identity.fingerprint,
                observation_id=identity.observation_id,
            )
            diagnostics.append(diagnostic)
            counts.observed_rows += 1
            counts.skipped_rows += 1
            counts.uncertain_rows += 1
            counts.coverage_complete = False
            await emit_row(diagnostic, fingerprint=identity.fingerprint)
        self.last_candidate_diagnostics = tuple(diagnostics)
        if (
            counts.body_unknown
            or counts.uncertain_rows
            or counts.deferred_offscreen
            or counts.partial_body_rows
            or counts.structural_reasons.get("POST_ROOT_UNRESOLVED", 0) > 0
            or any(
                diagnostic.state != "VERIFIED"
                for diagnostic in diagnostics
                if diagnostic.state != "NON_POST"
            )
        ):
            counts.coverage_complete = False
        self.last_candidate_diagnostics = tuple(diagnostics)
        self.refresh_viewport_body_coverage_counts()
        self._set_scan_operation("FINALIZE")
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
        candidate_roots: list[Locator] = []
        candidate_observation_ids: list[str] = []
        candidate_identities: list[_TrackedRowIdentity] = []
        for index, root, _, _, diagnostic, observation_id in candidate_rows[:max_posts]:
            handle = candidate_dom_handles.get(index)
            if handle is None:
                continue
            stable_id = diagnostic.observation_id or observation_id
            candidate_roots.append(root)
            candidate_observation_ids.append(stable_id)
            candidate_identities.append(
                _TrackedRowIdentity(
                    handle,
                    stable_id,
                    diagnostic.identity_fingerprint,
                    diagnostic.author_fingerprint,
                    diagnostic.state == "VERIFIED",
                )
            )
        retained_handles = {id(identity.dom_handle) for identity in candidate_identities}
        for handle in candidate_dom_handles.values():
            if id(handle) not in retained_handles:
                await handle.dispose()
        self._pending_viewport_candidate_identities = tuple(candidate_identities)
        self._active_scan_candidate_handles = None
        self._scan_failure_site = None
        self._scan_stage = None
        return _VisibleTargets(
            tuple(targets),
            scroll_anchor,
            tuple(display_observations),
            counts.observed_rows,
            counts.coverage_complete,
            signature,
            tuple(candidate_roots),
            tuple(pending_signals),
            tuple(candidate_observation_ids),
            tuple(candidate_identities),
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
        return await self._feed_rows._is_in_visible_scrollports(element)

    async def _is_nested_feed_root(self, root: Locator) -> bool:
        return await self._feed_rows._is_nested_feed_root(root)

    async def _is_nested_feed_content(self, element: Locator) -> bool:
        return await self._feed_rows._is_nested_feed_content(element)

    async def _belongs_to_card(
        self,
        element: Locator,
        root: Locator,
        root_handle: JSHandle,
        *,
        isolated_scope: bool = False,
    ) -> bool:
        return await self._feed_rows._belongs_to_card(
            element,
            root,
            root_handle,
            isolated_scope=isolated_scope,
        )

    async def _safe_body_candidate(
        self,
        element: Locator,
        root: Locator,
        root_handle: JSHandle,
        *,
        isolated_scope: bool = False,
    ) -> bool:
        return await self._feed_rows._safe_body_candidate(
            element,
            root,
            root_handle,
            isolated_scope=isolated_scope,
        )

    async def _safe_unscoped_text_candidate(self, element: Locator) -> bool:
        return await self._feed_rows._safe_unscoped_text_candidate(element)

    @staticmethod
    def _profile_anchor_candidate(value: str) -> bool:
        return FeedRowScanner._profile_anchor_candidate(value)

    async def _top_level_candidate_roots(
        self,
        region: Locator,
        viewport: dict[str, int],
    ) -> tuple[list[Locator], bool]:
        candidates: list[tuple[float, Locator]] = []
        candidate_keys: set[str] = set()
        self._carried_viewport_candidate_identities = ()
        carried_identities: list[_TrackedRowIdentity] = []
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
            if await self._is_outside_content_landmark(root) or await self._is_inside_quote(root):
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
            boxes = await asyncio.gather(*(inspect_semantic_root(root) for root in selected_roots))
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
            for root_box, identity in zip(
                eligible_roots,
                identity_keys,
                strict=True,
            ):
                root, box = root_box
                await add_candidate(root, box, str(identity))

        region_handle = await region.element_handle()
        if region_handle is not None:
            try:

                async def inspect_carried_identity(
                    identity: _TrackedRowIdentity,
                ) -> dict[str, object] | None:
                    return await identity.dom_handle.evaluate(
                        _with_scrollport_geometry("""(node, args) => {
                            /*SCROLLPORT_GEOMETRY*/
                            const style = getComputedStyle(node);
                            const rect = node.getBoundingClientRect();
                            if (!node.isConnected || !args.region.contains(node) ||
                                style.display === "none" ||
                                style.visibility === "hidden" ||
                                rect.width <= 0 || rect.height <= 0 ||
                                node.closest(args.outside) ||
                                node.closest(args.quote) ||
                                !geometryFor(node).intersects) return null;
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
                        }"""),
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

                previous_identities = self._last_viewport_candidate_identities
                identity_boxes = await asyncio.gather(
                    *(inspect_carried_identity(identity) for identity in previous_identities)
                )
                carried_identities.extend(
                    identity
                    for identity, box in zip(previous_identities, identity_boxes, strict=True)
                    if box is not None
                )
                for index, root in enumerate(self._last_viewport_candidate_roots):
                    if index >= len(previous_identities):
                        continue
                    identity = previous_identities[index]
                    if not await self._same_dom_element(root, identity.dom_handle):
                        continue
                    box = identity_boxes[index]
                    if box is None:
                        continue
                    await add_candidate(
                        root,
                        {"y": float(box["y"])},
                        str(box["key"]),
                    )
            finally:
                await region_handle.dispose()
        self._carried_viewport_candidate_identities = tuple(carried_identities)

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
            ancestor = timestamp.locator(f"xpath=ancestor::*[{fallback['depth']}]")
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
        return await FeedRowScanner._is_inside_quote(element)

    async def _inspect_candidate_root(
        self,
        root: Locator,
        viewport: dict[str, int],
    ) -> _CandidateStructure:
        return await self._feed_rows._inspect_candidate_root(root, viewport)

    @staticmethod
    async def _candidate_root_kind(root: Locator) -> str:
        return await FeedRowScanner._candidate_root_kind(root)

    def _record_structure_reason(self, reason: str) -> None:
        counts = self.last_scan_counts
        counts.structural_reasons[reason] = counts.structural_reasons.get(reason, 0) + 1

    def _record_url_structure(self, href: str, timestamp_link: bool) -> None:
        if "/post/" not in href:
            return
        counts = self.last_scan_counts
        segments, trailing_slash, has_query, has_fragment, is_relative = _post_url_structure(href)
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
        counts.url_structure_counts[shape] = counts.url_structure_counts.get(shape, 0) + 1

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
            counts.profile_anchor_candidates += int(self._profile_anchor_candidate(href))
        counts.visible_links = counts.visible_post_links
        return structures

    async def _content_region(
        self,
        *,
        prefer_main: bool = False,
        error_code: str = "FEED_REGION_UNVERIFIED",
    ) -> Locator:
        self._feed_rows.last_scan_counts = self.last_scan_counts
        return await self._feed_rows._content_region(
            prefer_main=prefer_main,
            error_code=error_code,
        )

    @staticmethod
    async def _is_outside_content_landmark(link: Locator) -> bool:
        return await FeedRowScanner._is_outside_content_landmark(link)

    async def _resolve_permalink_card(
        self,
        permalink: Locator,
        expected_url: str,
        expected_author: str,
    ) -> tuple[Locator, dict[str, float]]:
        return await self._feed_rows._resolve_permalink_card(
            permalink,
            expected_url,
            expected_author,
        )

    @staticmethod
    def _expected_profile_link_signal(root: Locator, author: str) -> Locator:
        return FeedRowScanner._expected_profile_link_signal(root, author)

    async def _recover_ambiguous_visible_card(
        self,
        root: Locator,
        root_handle: JSHandle,
        *,
        viewport_y: float,
        cached_body_fingerprints: set[str] | None,
        allow_hydration_retry: bool = True,
    ) -> tuple[PostTarget, bool]:
        """Compatibility delegate to bounded card provenance recovery."""
        return await self._card_provenance.recover_ambiguous_visible_card(
            root,
            root_handle,
            viewport_y=viewport_y,
            cached_body_fingerprints=cached_body_fingerprints,
            allow_hydration_retry=allow_hydration_retry,
            read_stable_card=self._read_stable_visible_card,
            wait_for_verified_feed=self._wait_for_verified_feed,
            readiness_deadline=self._readiness_deadline,
            wait_for_pending_card=self._wait_for_pending_card,
            readiness_timeout_ms=_READINESS_TIMEOUT_MS,
            card_hydration_wait_ms=_CARD_HYDRATION_WAIT_MS,
        )

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
                if not self._intersects_viewport(
                    box, viewport
                ) or not await self._is_in_visible_scrollports(root):
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
                    self._record_structure_reason("POST_BODY_STRUCTURE_UNSUPPORTED")
            if not target.body_complete:
                self.last_scan_counts.skipped_rows += 1
                self.last_scan_counts.coverage_complete = False
                evidence_state = (
                    "NO_BODY_NODES" if target.read_evidence is None else target.read_evidence.state
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
                        "PARTIAL_BODY" if target.body and target.body.strip() else state_reason[0]
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
            if target.fingerprint in scanned_fingerprints or (
                prefer_new_fingerprints is not None
                and target.fingerprint in prefer_new_fingerprints
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
        return await self._read_detail(target)

    @property
    def detail_route_verified(self) -> bool:
        return self._detail_navigator.route_verified

    async def actionable_now(self, target: PostTarget) -> PostTarget:
        return await self._detail_navigator.actionable_now(target)

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
            ):
                raise UiReadError("DETAIL_PROVENANCE_UNVERIFIED")
            matches.append(current)
        if len(matches) != 1:
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")
        return matches[0]

    async def _detail_click_point(self, permalink: Locator) -> tuple[float, float]:
        return await self._detail_navigator._detail_click_point(permalink)

    async def _read_detail(self, target: PostTarget) -> PostTarget:
        return await self._detail_navigator._read_detail(target)

    async def _detail_permalink_card(self, target: PostTarget) -> Locator:
        return await self._detail_navigator._detail_permalink_card(target)

    async def return_to_home(self) -> None:
        await self._detail_navigator.return_to_home()

    async def _for_you_control(self, *, timeout_ms: int | None = None) -> Locator:
        return await self._home_session._for_you_control(timeout_ms=timeout_ms)

    async def _visible_for_you_controls(self) -> list[Locator]:
        return await self._home_session._visible_for_you_controls()

    async def _for_you_is_active(self, control: Locator) -> bool:
        return await self._home_session._for_you_is_active(control)

    @staticmethod
    def _is_for_you_route(value: str) -> bool:
        return HomeSession._is_for_you_route(value)

    @staticmethod
    def _is_home_navigation_url(value: str) -> bool:
        return HomeSession._is_home_navigation_url(value)

    async def _require_feed_url(
        self,
        *,
        route_error_code: str = "HOME_UI_UNVERIFIED",
    ) -> None:
        await self._home_session._require_feed_url(route_error_code=route_error_code)

    async def _wait_for_verified_feed(
        self,
        deadline: float,
        *,
        timeout_code: str,
        route_error_code: str = "HOME_UI_UNVERIFIED",
    ) -> None:
        await self._home_session._wait_for_verified_feed(
            deadline,
            timeout_code=timeout_code,
            route_error_code=route_error_code,
        )

    async def _check_blocker(self) -> None:
        await self._home_session._check_blocker()

    async def _extract_card_body(
        self,
        root: Locator,
        root_handle: JSHandle,
        *,
        require_body: bool,
        isolated_scope: bool = False,
    ) -> tuple[str | None, bool, bool, tuple[_CardWaitSignal, ...]]:
        return await self._card_provenance.extract_card_body(
            root,
            root_handle,
            require_body=require_body,
            isolated_scope=isolated_scope,
        )

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

    async def _read_visible_body_progress(
        self,
        root: Locator,
        root_handle: JSHandle,
        *,
        fingerprint: str | None = None,
        isolated_scope: bool = False,
    ) -> _VisibleBodyRead:
        return await self._body_text._read_visible_body_progress(
            root,
            root_handle,
            fingerprint=fingerprint,
            scan_counts=self.last_scan_counts,
            isolated_scope=isolated_scope,
        )

    async def _read_detail_body(
        self,
        root: Locator,
        fingerprint: str,
    ) -> _VisibleBodyRead:
        root_handle = await root.element_handle()
        if root_handle is None:
            raise _CardNotReady(root, reason_code="POST_ROOT_DOM_IDENTITY_CHANGED")
        detail_fingerprint = sha256(f"detail:{fingerprint}".encode()).hexdigest()
        return await self._read_visible_body_progress(
            root,
            root_handle,
            fingerprint=detail_fingerprint,
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
        expected_root_handle: JSHandle | None = None,
        isolated_scope: bool = False,
    ) -> PostTarget:
        return await self._card_provenance.read_card(
            root,
            currently_visible=currently_visible,
            viewport_y=viewport_y,
            require_body=require_body,
            cached_body_fingerprints=cached_body_fingerprints,
            visible_text_progress=visible_text_progress,
            expected_root_handle=expected_root_handle,
            isolated_scope=isolated_scope,
            read_visible_body_progress=self._read_visible_body_progress,
            scan_operation=(
                self._set_scan_operation if self._scan_stage is not None else None
            ),
        )

    async def _wait_for_stable_card_dom(self, root: Locator) -> bool:
        return await self._card_provenance.wait_for_stable_card_dom(
            root,
            body_stability_interval_ms=_BODY_STABILITY_INTERVAL_MS,
            card_hydration_wait_ms=_CARD_HYDRATION_WAIT_MS,
        )

    async def _read_stable_visible_card(
        self,
        root: Locator,
        *,
        viewport_y: float,
        cached_body_fingerprints: set[str] | None,
        expected_root_handle: JSHandle | None = None,
        allow_hydration_retry: bool = False,
        isolated_scope: bool = False,
    ) -> tuple[PostTarget, bool]:
        return await self._card_provenance.read_stable_visible_card(
            root,
            viewport_y=viewport_y,
            cached_body_fingerprints=cached_body_fingerprints,
            expected_root_handle=expected_root_handle,
            allow_hydration_retry=allow_hydration_retry,
            isolated_scope=isolated_scope,
            read_card=self._read_card,
            wait_for_stable_dom=self._wait_for_stable_card_dom,
            scan_operation=(
                self._set_scan_operation if self._scan_stage is not None else None
            ),
            body_stability_interval_ms=_BODY_STABILITY_INTERVAL_MS,
            card_hydration_wait_ms=_CARD_HYDRATION_WAIT_MS,
        )

    async def _visible_body_candidate_state(
        self,
        root: Locator,
        *,
        fingerprint: str | None = None,
    ) -> tuple[bool, bool]:
        return await self._card_provenance.visible_body_candidate_state(
            root, fingerprint=fingerprint
        )
