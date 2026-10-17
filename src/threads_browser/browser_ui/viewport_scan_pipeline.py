"""One bounded viewport scan pipeline, with state held by its coordinator."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from hashlib import sha256

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator

from ..errors import UiReadError
from ..feed.models import DisplayObservation, PostTarget
from .feed_rows import (
    _CandidateDiagnostic,
    _FeedScanCounts,
    _FeedScanTimings,
)
from .viewport_scan_models import (
    _HomeRowObservation,
    _TrackedRowIdentity,
    _ViewportScanHooks,
    _ViewportScanRequest,
    _ViewportScanState,
    _VisibleTargets,
)


class ViewportScanPipeline:
    """Run focused scan phases against coordinator-owned state."""

    async def _scan_active(
        self,
        request: _ViewportScanRequest,
        hooks: _ViewportScanHooks,
    ) -> _VisibleTargets:
        state = self._require_state()
        viewport = self._page.viewport_size
        if viewport is None:
            raise UiReadError("FEED_VIEWPORT_UNVERIFIED")
        self._reset_scan_counts(state.counts)
        region, roots, roots_truncated, structures = await self._discover_candidates(
            state,
            viewport,
        )
        emit_row = self._row_emitter(state, request.on_row)
        scroll_anchor = await self._scan_candidate_rows(
            state,
            viewport,
            roots,
            roots_truncated,
            structures,
            emit_row,
        )
        await self._record_carried_identity_gaps(state, region, emit_row)
        return await self._finalize_scan(state, scroll_anchor)

    def _reset_scan_counts(self, counts: _FeedScanCounts) -> None:
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

    async def _discover_candidates(
        self,
        state: _ViewportScanState,
        viewport: dict[str, int],
    ):
        counts = state.counts
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
        return region, roots, roots_truncated, structures

    def _row_emitter(
        self,
        state: _ViewportScanState,
        on_row: Callable[[_HomeRowObservation], Awaitable[None]] | None,
    ) -> Callable[..., Awaitable[None]]:
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
            self.last_candidate_diagnostics = tuple(state.diagnostics)
            self.refresh_viewport_body_coverage_counts(
                self.last_candidate_diagnostics,
                state.counts,
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

        return emit_row

    async def _scan_candidate_rows(
        self,
        state: _ViewportScanState,
        viewport: dict[str, int],
        roots: list[Locator],
        roots_truncated: bool,
        structures: dict[int, object],
        emit_row: Callable[..., Awaitable[None]],
    ) -> dict[str, float] | None:
        request = state.request
        max_posts = request.max_posts
        currently_visible = request.currently_visible
        prefer_new_fingerprints = request.prefer_new_fingerprints
        stage = request.stage
        on_row = request.on_row
        counts = state.counts
        candidate_rows = state.candidate_rows
        candidate_dom_handles = state.candidate_dom_handles
        diagnostics = state.diagnostics
        targets = state.targets
        scanned_fingerprints = state.scanned_fingerprints
        scroll_anchor = state.scroll_anchor
        self._active_scan_candidate_handles = candidate_dom_handles
        await self._candidate_processor.collect(state, roots, structures)
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
        state.candidate_by_index = {
            index: (root, structure, carried_identity, diagnostic, observation_id)
            for index, root, structure, carried_identity, diagnostic, observation_id in candidate_rows
        }
        scroll_anchor = await self._row_verifier.scan_rows(
            state,
            viewport,
            emit_row,
            scroll_anchor,
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
        return scroll_anchor

    async def _record_carried_identity_gaps(
        self,
        state: _ViewportScanState,
        region: Locator,
        emit_row: Callable[..., Awaitable[None]],
    ) -> None:
        diagnostics = state.diagnostics
        counts = state.counts
        scanned_fingerprints = state.scanned_fingerprints
        matched_previous_identity_handles = state.matched_previous_identity_handles
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

    async def _finalize_scan(
        self,
        state: _ViewportScanState,
        scroll_anchor: dict[str, float] | None,
    ) -> _VisibleTargets:
        self.last_candidate_diagnostics = tuple(state.diagnostics)
        self._mark_incomplete_scan_coverage(state)
        self.refresh_viewport_body_coverage_counts(
            self.last_candidate_diagnostics,
            self.last_scan_counts,
        )
        self._set_scan_operation("FINALIZE")
        signature = self._scan_signature(state)
        (
            candidate_roots,
            candidate_observation_ids,
            candidate_identities,
        ) = await self._retain_candidate_handles(state)
        self._active_scan_candidate_handles = None
        self._scan_failure_site = None
        state.scroll_anchor = scroll_anchor
        return _VisibleTargets(
            tuple(state.targets),
            scroll_anchor,
            tuple(state.display_observations),
            state.counts.observed_rows,
            state.counts.coverage_complete,
            signature,
            tuple(candidate_roots),
            tuple(state.pending_signals),
            tuple(candidate_observation_ids),
            tuple(candidate_identities),
        )

    @staticmethod
    def _mark_incomplete_scan_coverage(state: _ViewportScanState) -> None:
        counts = state.counts
        if (
            counts.body_unknown
            or counts.uncertain_rows
            or counts.deferred_offscreen
            or counts.partial_body_rows
            or counts.structural_reasons.get("POST_ROOT_UNRESOLVED", 0) > 0
            or any(
                diagnostic.state != "VERIFIED"
                for diagnostic in state.diagnostics
                if diagnostic.state != "NON_POST"
            )
        ):
            counts.coverage_complete = False

    @staticmethod
    def _scan_signature(state: _ViewportScanState) -> tuple[object, ...]:
        return (
            tuple(
                (
                    target.url,
                    target.author,
                    target.body,
                    target.body_available,
                    round(target.viewport_y, 1),
                )
                for target in state.targets
            ),
            tuple(
                (
                    observation.body,
                    observation.status,
                    round(observation.viewport_y, 1),
                )
                for observation in state.display_observations
            ),
            state.counts.observed_rows,
            state.counts.coverage_complete,
            state.counts.uncertain_rows,
        )

    async def _retain_candidate_handles(
        self,
        state: _ViewportScanState,
    ) -> tuple[list[Locator], list[str], list[_TrackedRowIdentity]]:
        candidate_roots: list[Locator] = []
        candidate_observation_ids: list[str] = []
        candidate_identities: list[_TrackedRowIdentity] = []
        candidate_handles = state.candidate_dom_handles
        for index, root, _, _, diagnostic, observation_id in state.candidate_rows[
            : state.request.max_posts
        ]:
            handle = candidate_handles.get(index)
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
                for handle in candidate_handles.values()
                if id(handle) not in retained_handles
            )
        )
        return candidate_roots, candidate_observation_ids, candidate_identities
