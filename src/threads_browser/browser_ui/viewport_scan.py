"""Per-invocation Home viewport scan coordination."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import JSHandle, Locator, Page

from ..feed.models import BodyReadEvidence, DisplayObservation, PostTarget
from .card_provenance import CardProvenanceReader
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
from .loading import CardWaitSignal as _CardWaitSignal
from .viewport_scan_models import (
    _ScanFailureSite,
    _TrackedRowIdentity,
    _ViewportScanHooks,
    _ViewportScanRequest,
    _ViewportScanState,
    _VisibleTargets,
)
from .viewport_scan_pipeline import ViewportScanPipeline


class ViewportScanCoordinator(ViewportScanPipeline):
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
        from .viewport_candidate import ViewportCandidateProcessor
        from .viewport_row_verifier import ViewportRowVerifier
        self._row_verifier = ViewportRowVerifier(self)
        self._candidate_processor = ViewportCandidateProcessor(self)
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
