"""Per-invocation Home viewport scan coordination."""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from hashlib import sha256

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import JSHandle, Locator, Page

from ..errors import UiReadError
from ..feed.models import BodyReadEvidence, DisplayObservation, PostTarget
from .body_text import _body_progress_state, _VisibleBodyRead
from .card_provenance import CardProvenanceReader, _CardDeferredOffscreen
from .feed_rows import (
    _BODY_TEXT_SELECTOR,
    _MAX_STRUCTURE_ELEMENTS,
    FeedRowScanner,
    _CandidateDiagnostic,
    _CandidateStructure,
    _FeedScanCounts,
    _FeedScanTimings,
)
from .feed_rows import (
    UnverifiedFeedCandidate as _UnverifiedFeedCandidate,
)
from .loading import CardNotReady as _CardNotReady
from .loading import CardWaitSignal as _CardWaitSignal


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
    viewport_size: tuple[int, int]
    scroll_position: tuple[float, float, float] = field(repr=False)


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


@dataclass(frozen=True, slots=True)
class _ViewportScanRequest:
    max_posts: int
    currently_visible: bool
    prefer_new_fingerprints: set[str] | None
    stage: str
    viewport_index: int | None
    on_row: Callable[[_HomeRowObservation], Awaitable[None]] | None = None


@dataclass(frozen=True, slots=True)
class _ViewportScanHooks:
    """Reader compatibility operations needed during a scan, without reader state."""

    discover_roots: Callable[
        [Locator, dict[str, int]],
        Awaitable[tuple[list[Locator], bool, tuple[_TrackedRowIdentity, ...]]],
    ]
    collect_structures: Callable[
        [Locator, list[Locator], dict[str, int]],
        Awaitable[dict[int, _CandidateStructure]],
    ]
    scan_permalink_fallback: Callable[..., Awaitable[tuple[tuple[PostTarget, ...], dict[str, float] | None]]]
    read_card: Callable[..., Awaitable[PostTarget]]
    recover_ambiguous_visible_card: Callable[..., Awaitable[tuple[PostTarget, bool]]]
    read_visible_body_progress: Callable[..., Awaitable[_VisibleBodyRead]]
    wait_for_pending_card: Callable[..., Awaitable[bool]]
    record_scan_skip: Callable[[str, _UnverifiedFeedCandidate], None]
    publish_diagnostics: Callable[[tuple[_CandidateDiagnostic, ...]], None]
    publish_display_observations: Callable[[tuple[DisplayObservation, ...]], None]
    publish_can_wheel: Callable[[bool], None]
    increment_feed_duplicates: Callable[[], None]
    body_stability_interval_ms: int
    card_hydration_wait_ms: int


_CandidateRow = tuple[
    int,
    Locator,
    _CandidateStructure,
    _TrackedRowIdentity | None,
    _CandidateDiagnostic,
    str,
]


@dataclass(slots=True)
class _ViewportScanState:
    """Mutable data whose lifetime is one viewport scan and its retry handoff."""

    request: _ViewportScanRequest
    hooks: _ViewportScanHooks
    counts: _FeedScanCounts
    stage: str | None = None
    carried_identities: tuple[_TrackedRowIdentity, ...] = ()
    timings: _FeedScanTimings = field(default_factory=_FeedScanTimings)
    operation: str = "DISCOVER_ROOTS"
    candidate_index: int | None = None
    failure_site: _ScanFailureSite | None = None
    candidate_rows: list[_CandidateRow] = field(default_factory=list)
    candidate_dom_handles: dict[int, JSHandle] = field(default_factory=dict)
    candidate_prior_fingerprints: dict[int, str | None] = field(default_factory=dict)
    matched_previous_identity_handles: set[int] = field(default_factory=set)
    diagnostics: list[_CandidateDiagnostic] = field(default_factory=list)
    published_diagnostics: tuple[_CandidateDiagnostic, ...] = ()
    targets: list[PostTarget] = field(default_factory=list)
    scroll_anchor: dict[str, float] | None = None
    display_observations: list[DisplayObservation] = field(default_factory=list)
    pending_signals: list[_CardWaitSignal] = field(default_factory=list)
    scanned_fingerprints: set[str] = field(default_factory=set)
    candidate_by_index: dict[
        int,
        tuple[
            Locator,
            _CandidateStructure,
            _TrackedRowIdentity | None,
            _CandidateDiagnostic,
            str,
        ],
    ] = field(default_factory=dict)
    processed_candidates: int = 0


class ViewportScanCoordinator:
    """Coordinate one bounded visible-candidate scan over narrow UI components."""

    def __init__(
        self,
        page: Page,
        feed_rows: FeedRowScanner,
        card_provenance: CardProvenanceReader,
    ) -> None:
        self._page = page
        self._feed_rows = feed_rows
        self._card_provenance = card_provenance
        self._state: _ViewportScanState | None = None

    @property
    def state(self) -> _ViewportScanState | None:
        return self._state

    @property
    def failure_site(self) -> _ScanFailureSite | None:
        return None if self._state is None else self._state.failure_site

    @failure_site.setter
    def failure_site(self, value: _ScanFailureSite | None) -> None:
        if self._state is not None:
            self._state.failure_site = value

    @property
    def stage(self) -> str | None:
        return None if self._state is None else self._state.stage

    @stage.setter
    def stage(self, value: str | None) -> None:
        if self._state is not None:
            self._state.stage = value

    @property
    def candidate_index(self) -> int | None:
        return None if self._state is None else self._state.candidate_index

    @candidate_index.setter
    def candidate_index(self, value: int | None) -> None:
        if self._state is not None:
            self._state.candidate_index = value

    @property
    def operation(self) -> str:
        return "DISCOVER_ROOTS" if self._state is None else self._state.operation

    @property
    def active_candidate_handles(self) -> dict[int, JSHandle] | None:
        if self._state is None or not self._state.candidate_dom_handles:
            return None
        return self._state.candidate_dom_handles

    @active_candidate_handles.setter
    def active_candidate_handles(self, value: dict[int, JSHandle] | None) -> None:
        if self._state is not None:
            self._state.candidate_dom_handles = {} if value is None else value

    def set_operation(self, operation: str) -> None:
        if self._state is None:
            return
        self._state.operation = operation
        if self._state.failure_site is not None:
            self._state.failure_site = replace(
                self._state.failure_site,
                operation=operation,
            )

    async def scan(
        self,
        request: _ViewportScanRequest,
        *,
        counts: _FeedScanCounts,
        timings: _FeedScanTimings,
        hooks: _ViewportScanHooks,
    ) -> _VisibleTargets:
        await self._dispose_active_scan_candidate_handles()
        state = _ViewportScanState(
            request=request,
            hooks=hooks,
            counts=counts,
            stage=request.stage,
            timings=timings,
        )
        self._state = state
        try:
            result = await self._scan_active(request, hooks)
        except asyncio.CancelledError:
            try:
                await self._finish_cleanup(state)
            finally:
                self._state = None
            raise
        except PlaywrightError:
            # The reader must revalidate this row before releasing the DOM
            # handles. Its bounded retry path owns that handoff.
            raise
        except BaseException:
            await self._finish_cleanup(state)
            self._state = None
            raise
        else:
            self._state = None
            return result

    async def _finish_cleanup(
        self,
        state: _ViewportScanState,
    ) -> None:
        cleanup = asyncio.create_task(self._dispose_scan_handles(state))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

    async def _dispose_scan_handles(
        self,
        state: _ViewportScanState,
    ) -> None:
        handles = state.candidate_dom_handles
        state.candidate_dom_handles = {}
        await self._dispose_handle_batch(tuple(handles.values()))

    async def dispose_handles(self, handles: tuple[JSHandle, ...]) -> None:
        cleanup = asyncio.create_task(self._dispose_handle_batch(handles))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

    async def _dispose_handle_batch(self, handles: tuple[JSHandle, ...]) -> None:
        await asyncio.gather(
            *(self._dispose_handle(handle) for handle in handles),
            return_exceptions=True,
        )

    @staticmethod
    async def _dispose_handle(handle: JSHandle) -> None:
        cleanup = asyncio.create_task(asyncio.wait_for(handle.dispose(), timeout=1.0))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            try:
                await cleanup
            except (PlaywrightError, TimeoutError):
                pass
            raise
        except (PlaywrightError, TimeoutError):
            pass

    async def _dispose_active_scan_candidate_handles(self) -> None:
        state = self._state
        if state is not None:
            try:
                await self._finish_cleanup(state)
            finally:
                self._state = None

    def clear_scan_state(self) -> None:
        state = self._state
        if state is not None and state.candidate_dom_handles:
            raise RuntimeError("cannot clear viewport scan state with live handles")
        self._state = None

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
        state = self._require_state()
        state.candidate_index = candidate_index
        self.set_operation("STRUCTURE")
        evidence = await self._feed_rows._candidate_identity_evidence(root, root_handle)
        box = await root.bounding_box()
        position = await self._scan_position()
        if not evidence.root_matches or box is None:
            state.failure_site = None
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
        state.failure_site = _ScanFailureSite(
            stage=state.stage or "FEED_INITIAL",
            viewport_index=state.request.viewport_index,
            candidate_index=candidate_index,
            operation=state.operation,
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

    async def _is_carried_viewport_candidate(
        self, root: Locator
    ) -> _TrackedRowIdentity | None:
        state = self._require_state()
        identities = state.carried_identities
        if not identities:
            return None
        matched_index = await root.evaluate(
            "(node, previous) => previous.findIndex(item => node === item)",
            [identity.dom_handle for identity in identities],
        )
        if matched_index < 0 or matched_index >= len(identities):
            return None
        return identities[matched_index]

    @property
    def _cached_home_bodies(self) -> dict[str, tuple[str, bool]]:
        return self._card_provenance.cached_home_bodies

    @property
    def last_scan_counts(self) -> _FeedScanCounts:
        return self._require_state().counts

    @property
    def last_scan_timings(self) -> _FeedScanTimings:
        return self._require_state().timings

    @last_scan_timings.setter
    def last_scan_timings(self, value: _FeedScanTimings) -> None:
        state = self._require_state()
        state.timings.discover_ms = value.discover_ms
        state.timings.read_row_ms = value.read_row_ms
        state.timings.stabilize_ms = value.stabilize_ms
        state.timings.wait_reason = value.wait_reason

    @property
    def last_candidate_diagnostics(self) -> tuple[_CandidateDiagnostic, ...]:
        return self._require_state().published_diagnostics

    @last_candidate_diagnostics.setter
    def last_candidate_diagnostics(
        self,
        value: tuple[_CandidateDiagnostic, ...],
    ) -> None:
        state = self._require_state()
        state.published_diagnostics = value
        state.hooks.publish_diagnostics(value)

    @property
    def last_display_observations(self) -> tuple[DisplayObservation, ...]:
        return tuple(self._require_state().display_observations)

    @last_display_observations.setter
    def last_display_observations(self, value: tuple[DisplayObservation, ...]) -> None:
        state = self._require_state()
        state.display_observations = list(value)
        state.hooks.publish_display_observations(value)

    @property
    def last_viewport_can_wheel(self) -> bool:
        return False

    @last_viewport_can_wheel.setter
    def last_viewport_can_wheel(self, value: bool) -> None:
        self._require_state().hooks.publish_can_wheel(value)

    @property
    def _active_scan_candidate_handles(self) -> dict[int, JSHandle] | None:
        return self.active_candidate_handles

    @_active_scan_candidate_handles.setter
    def _active_scan_candidate_handles(self, value: dict[int, JSHandle] | None) -> None:
        self.active_candidate_handles = value

    @property
    def _scan_stage(self) -> str | None:
        return self.stage

    @_scan_stage.setter
    def _scan_stage(self, value: str | None) -> None:
        self.stage = value

    @property
    def _scan_candidate_index(self) -> int | None:
        return self.candidate_index

    @_scan_candidate_index.setter
    def _scan_candidate_index(self, value: int | None) -> None:
        self.candidate_index = value

    @property
    def _scan_failure_site(self) -> _ScanFailureSite | None:
        return self.failure_site

    @_scan_failure_site.setter
    def _scan_failure_site(self, value: _ScanFailureSite | None) -> None:
        self.failure_site = value

    @property
    def _scan_operation(self) -> str:
        return self.operation

    async def _content_region(self) -> Locator:
        self._feed_rows.last_scan_counts = self.last_scan_counts
        return await self._feed_rows._content_region()

    async def _top_level_candidate_roots(
        self, region: Locator, viewport: dict[str, int]
    ) -> tuple[list[Locator], bool, tuple[_TrackedRowIdentity, ...]]:
        return await self._require_state().hooks.discover_roots(region, viewport)

    async def _collect_structure_diagnostics(
        self,
        region: Locator,
        roots: list[Locator],
        viewport: dict[str, int],
    ) -> dict[int, _CandidateStructure]:
        return await self._require_state().hooks.collect_structures(region, roots, viewport)

    async def _visible_permalink_targets(self, *args, **kwargs):
        return await self._require_state().hooks.scan_permalink_fallback(*args, **kwargs)

    async def _read_card(self, *args, **kwargs) -> PostTarget:
        return await self._require_state().hooks.read_card(*args, **kwargs)

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
        state = self._require_state()

        async def wait_for_stable_dom(candidate_root: Locator) -> bool:
            return await self._card_provenance.wait_for_stable_card_dom(
                candidate_root,
                body_stability_interval_ms=state.hooks.body_stability_interval_ms,
                card_hydration_wait_ms=state.hooks.card_hydration_wait_ms,
            )

        return await self._card_provenance.read_stable_visible_card(
            root,
            viewport_y=viewport_y,
            cached_body_fingerprints=cached_body_fingerprints,
            expected_root_handle=expected_root_handle,
            allow_hydration_retry=allow_hydration_retry,
            isolated_scope=isolated_scope,
            read_card=self._read_card,
            wait_for_stable_dom=wait_for_stable_dom,
            scan_operation=self._set_scan_operation,
            body_stability_interval_ms=state.hooks.body_stability_interval_ms,
            card_hydration_wait_ms=state.hooks.card_hydration_wait_ms,
        )

    async def _wait_for_stable_card_dom(self, root: Locator) -> bool:
        state = self._require_state()
        return await self._card_provenance.wait_for_stable_card_dom(
            root,
            body_stability_interval_ms=state.hooks.body_stability_interval_ms,
            card_hydration_wait_ms=state.hooks.card_hydration_wait_ms,
        )

    async def _recover_ambiguous_visible_card(self, *args, **kwargs) -> tuple[PostTarget, bool]:
        return await self._require_state().hooks.recover_ambiguous_visible_card(*args, **kwargs)

    async def _observe_display_body(
        self, root: Locator
    ) -> tuple[str | None, tuple[_CardWaitSignal, ...], BodyReadEvidence]:
        root_handle = await root.element_handle()
        if root_handle is None:
            return None, (), BodyReadEvidence(state="NO_BODY_NODES")
        try:
            observed = await self._read_visible_body_progress(root, root_handle)
            if observed.has_rendered_text:
                return observed.body, (), observed.read_evidence
            candidates = root.locator(_BODY_TEXT_SELECTOR)
            signals = tuple(
                _CardWaitSignal(candidates.nth(index), "nonempty_text")
                for index in range(min(await candidates.count(), _MAX_STRUCTURE_ELEMENTS))
                if await self._feed_rows._safe_body_candidate(
                    candidates.nth(index), root, root_handle
                )
            )
            return None, signals, observed.read_evidence
        finally:
            await self._dispose_handle(root_handle)

    async def _read_visible_body_progress(self, *args, **kwargs):
        return await self._require_state().hooks.read_visible_body_progress(*args, **kwargs)

    async def _wait_for_pending_card(self, *args, **kwargs) -> bool:
        return await self._require_state().hooks.wait_for_pending_card(*args, **kwargs)

    def _record_scan_skip(self, stage: str, error: _UnverifiedFeedCandidate) -> None:
        self._require_state().hooks.record_scan_skip(stage, error)

    def _record_structure_reason(self, reason: str) -> None:
        counts = self._require_state().counts
        counts.structural_reasons[reason] = counts.structural_reasons.get(reason, 0) + 1

    def _set_scan_operation(self, operation: str) -> None:
        self.set_operation(operation)

    async def _same_dom_element(self, root: Locator, handle: JSHandle) -> bool:
        return await self._card_provenance._same_dom_element(root, handle)

    async def _inspect_candidate_root(
        self,
        root: Locator,
        viewport: dict[str, int],
    ) -> _CandidateStructure:
        return await self._feed_rows._inspect_candidate_root(root, viewport)

    async def _is_in_visible_scrollports(self, element: Locator) -> bool:
        return await self._feed_rows._is_in_visible_scrollports(element)

    @staticmethod
    def refresh_viewport_body_coverage_counts(
        diagnostics: tuple[_CandidateDiagnostic, ...],
        counts: _FeedScanCounts,
    ) -> None:
        visited: set[str] = set()
        lookahead: set[str] = set()
        for diagnostic in diagnostics:
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
        counts.unresolved_visited_rows = len(visited)
        counts.lookahead_rows = len(lookahead)

    def _require_state(self) -> _ViewportScanState:
        if self._state is None:
            raise RuntimeError("viewport scan state is not active")
        return self._state

    @property
    def _carried_viewport_candidate_identities(self) -> tuple[_TrackedRowIdentity, ...]:
        return self._require_state().carried_identities

    @staticmethod
    def _readiness_deadline(timeout_ms: int) -> float:
        return time.monotonic() + timeout_ms / 1000

    async def _scan_active(
        self,
        request: _ViewportScanRequest,
        hooks: _ViewportScanHooks,
    ) -> _VisibleTargets:
        state = self._require_state()
        max_posts = request.max_posts
        currently_visible = request.currently_visible
        prefer_new_fingerprints = request.prefer_new_fingerprints
        stage = request.stage
        on_row = request.on_row
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
        roots, roots_truncated, state.carried_identities = await self._top_level_candidate_roots(
            region,
            viewport,
        )
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
            self.refresh_viewport_body_coverage_counts(
                self.last_candidate_diagnostics,
                self.last_scan_counts,
            )
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

        candidate_rows = state.candidate_rows
        candidate_dom_handles = state.candidate_dom_handles
        self._active_scan_candidate_handles = candidate_dom_handles
        candidate_prior_fingerprints = state.candidate_prior_fingerprints
        matched_previous_identity_handles = state.matched_previous_identity_handles
        diagnostics = state.diagnostics
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
        self.refresh_viewport_body_coverage_counts(
            self.last_candidate_diagnostics,
            self.last_scan_counts,
        )
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
        targets = state.targets
        scroll_anchor = state.scroll_anchor
        display_observations = state.display_observations
        pending_signals = state.pending_signals
        scanned_fingerprints = state.scanned_fingerprints
        state.candidate_by_index = {
            index: (root, structure, carried_identity, diagnostic, observation_id)
            for index, root, structure, carried_identity, diagnostic, observation_id in candidate_rows
        }
        for row_index, diagnostic in enumerate(diagnostics):
            self._scan_candidate_index = row_index + 1
            self._set_scan_operation("STRUCTURE")
            candidate = state.candidate_by_index.get(row_index)
            if candidate is None:
                await emit_row(diagnostic, candidate=False)
                continue
            if state.processed_candidates >= max_posts:
                diagnostic.state = "REJECTED"
                diagnostic.reason = "POST_SCAN_CAP_REACHED"
                counts.skipped_rows += 1
                await emit_row(diagnostic)
                continue
            state.processed_candidates += 1
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
                        self._readiness_deadline(hooks.card_hydration_wait_ms),
                        max_wait_ms=hooks.card_hydration_wait_ms,
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
                state.hooks.increment_feed_duplicates()
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
        self.refresh_viewport_body_coverage_counts(
            self.last_candidate_diagnostics,
            self.last_scan_counts,
        )
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
        await self._dispose_handle_batch(
            tuple(
                handle
                for handle in candidate_dom_handles.values()
                if id(handle) not in retained_handles
            )
        )
        self._active_scan_candidate_handles = None
        self._scan_failure_site = None
        state.scroll_anchor = scroll_anchor
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
