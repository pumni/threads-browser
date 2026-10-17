"""Read and verify discovered candidate rows with fail-closed recovery."""

from __future__ import annotations

import secrets
import time
from collections.abc import Awaitable, Callable

from ..feed.models import DisplayObservation
from .body_text import _body_progress_state
from .card_provenance import _CardDeferredOffscreen
from .feed_rows import (
    UnverifiedFeedCandidate as _UnverifiedFeedCandidate,
)
from .feed_rows import (
    _CandidateDiagnostic,
)
from .loading import CardNotReady as _CardNotReady
from .viewport_row_models import (
    CandidateRoot,
    CandidateWork,
    ViewportRowScanHost,
    _ViewportScanState,
)
from .viewport_row_recovery import ViewportRowRecovery


class ViewportRowVerifier:
    """Own bounded per-row verification while sharing only scan-local state."""

    def __init__(self, host: ViewportRowScanHost) -> None:
        self._host = host
        self._recovery = ViewportRowRecovery(host)

    async def scan_rows(
        self,
        state: _ViewportScanState,
        viewport: dict[str, int],
        emit_row: Callable[..., Awaitable[None]],
        scroll_anchor: dict[str, float] | None,
    ) -> dict[str, float] | None:
        for row_index, diagnostic in enumerate(state.diagnostics):
            scroll_anchor = await self._process_candidate(
                state,
                row_index,
                diagnostic,
                viewport,
                emit_row,
                scroll_anchor,
            )
        return scroll_anchor

    async def _process_candidate(
        self,
        state: _ViewportScanState,
        row_index: int,
        diagnostic: _CandidateDiagnostic,
        viewport: dict[str, int],
        emit_row: Callable[..., Awaitable[None]],
        scroll_anchor: dict[str, float] | None,
    ) -> dict[str, float] | None:
        self._host._scan_candidate_index = row_index + 1
        self._host._set_scan_operation("STRUCTURE")
        candidate = state.candidate_by_index.get(row_index)
        if candidate is None:
            await emit_row(diagnostic, candidate=False)
            return scroll_anchor
        if state.processed_candidates >= state.request.max_posts:
            diagnostic.state = "REJECTED"
            diagnostic.reason = "POST_SCAN_CAP_REACHED"
            state.counts.skipped_rows += 1
            await emit_row(diagnostic)
            return scroll_anchor
        state.processed_candidates += 1
        candidate_root = await self._revalidate_candidate_root(
            state, row_index, candidate, viewport, emit_row
        )
        if candidate_root is None:
            return scroll_anchor
        work, scroll_anchor = await self._prepare_candidate(candidate_root, scroll_anchor, emit_row)
        if work is None:
            return scroll_anchor
        target = await self._read_candidate(work)
        if target is not None:
            await self._record_verified_target(work, target)
        return scroll_anchor

    async def _revalidate_candidate_root(
        self,
        state: _ViewportScanState,
        row_index: int,
        candidate: tuple,
        viewport: dict[str, int],
        emit_row: Callable[..., Awaitable[None]],
    ) -> CandidateRoot | None:
        root, structure, carried_identity, diagnostic, _ = candidate
        carried_row = carried_identity is not None
        root_handle = state.candidate_dom_handles.get(row_index)
        counts = state.counts
        if root_handle is None:
            counts.skipped_rows += 1
            counts.uncertain_rows += 1
            counts.coverage_complete = False
            diagnostic.state = "REJECTED"
            diagnostic.reason = "POST_ROOT_IDENTITY_UNAVAILABLE"
            await emit_row(diagnostic)
            return None
        await self._host._capture_scan_failure_site(
            root,
            root_handle,
            viewport,
            row_index + 1,
            carried_identity=carried_identity,
        )
        self._host._set_scan_operation("STRUCTURE")
        if not await self._host._same_dom_element(root, root_handle):
            root_handle = await self._replace_candidate_root(
                state, row_index, root, diagnostic, root_handle, emit_row
            )
            if root_handle is None:
                return None
            carried_row = False
            structure = await self._host._inspect_candidate_root(root, viewport)
            self._copy_structure_evidence(diagnostic, structure)
            if not await self._host._same_dom_element(root, root_handle):
                self._reject_changed_root(counts, diagnostic)
                await emit_row(diagnostic)
                return None
        return CandidateRoot(
            state,
            row_index,
            root,
            structure,
            carried_row,
            diagnostic,
            root_handle,
        )

    async def _replace_candidate_root(
        self,
        state: _ViewportScanState,
        row_index: int,
        root,
        diagnostic: _CandidateDiagnostic,
        old_handle,
        emit_row: Callable[..., Awaitable[None]],
    ):
        replacement_handle = await root.element_handle()
        await old_handle.dispose()
        if replacement_handle is None:
            state.candidate_dom_handles.pop(row_index, None)
            self._reject_changed_root(state.counts, diagnostic)
            await emit_row(diagnostic)
            return None
        state.candidate_dom_handles[row_index] = replacement_handle
        state.candidate_prior_fingerprints[row_index] = None
        diagnostic.observation_id = secrets.token_hex(8)
        diagnostic.identity_fingerprint = None
        diagnostic.author_fingerprint = None
        return replacement_handle

    @staticmethod
    def _copy_structure_evidence(diagnostic: _CandidateDiagnostic, structure) -> None:
        diagnostic.root_kind = structure.root_kind
        diagnostic.has_visible_time = structure.visible_times > 0
        diagnostic.has_post_permalink = structure.visible_post_links > 0
        diagnostic.has_profile_author = structure.profile_anchor_candidates > 0
        diagnostic.has_readable_body = structure.readable_body_candidates > 0

    @staticmethod
    def _reject_changed_root(counts, diagnostic: _CandidateDiagnostic) -> None:
        counts.skipped_rows += 1
        counts.uncertain_rows += 1
        counts.coverage_complete = False
        diagnostic.state = "REJECTED"
        diagnostic.reason = "POST_ROOT_IDENTITY_CHANGED"

    async def _prepare_candidate(
        self,
        candidate: CandidateRoot,
        scroll_anchor: dict[str, float] | None,
        emit_row: Callable[..., Awaitable[None]],
    ) -> tuple[CandidateWork | None, dict[str, float] | None]:
        state = candidate.state
        counts = state.counts
        self._host._set_scan_operation("BOUNDING_BOX")
        box = await candidate.root.bounding_box()
        if box is None:
            counts.skipped_rows += 1
            counts.uncertain_rows += 1
            candidate.diagnostic.state = "REJECTED"
            candidate.diagnostic.reason = "POST_ROOT_NOT_VISIBLE"
            state.display_observations.append(DisplayObservation(None, "OBSERVATION_UNCERTAIN"))
            await emit_row(candidate.diagnostic, display=state.display_observations[-1])
            return None, scroll_anchor
        counts.inspected += 1
        if scroll_anchor is None:
            scroll_anchor = box
        has_structure_cues = any(
            (
                candidate.structure.profile_anchor_candidates,
                candidate.structure.visible_post_links,
                candidate.structure.offscreen_post_links,
                candidate.structure.offscreen_profile_anchors,
            )
        )
        if not has_structure_cues and not candidate.carried_row:
            counts.skipped_rows += 1
            counts.uncertain_rows += 1
            candidate.diagnostic.state = "REJECTED"
            candidate.diagnostic.reason = "DISPLAY_ROW_CUES_INSUFFICIENT"
            self._host._record_structure_reason("DISPLAY_ROW_CUES_INSUFFICIENT")
            state.display_observations.append(
                DisplayObservation(None, "OBSERVATION_UNCERTAIN", box["y"])
            )
            await emit_row(candidate.diagnostic, display=state.display_observations[-1])
            return None, scroll_anchor
        if candidate.structure.visible_times > 1:
            counts.skipped_rows += 1
            counts.uncertain_rows += 1
            candidate.diagnostic.state = "REJECTED"
            candidate.diagnostic.reason = "CANDIDATE_ROW_AMBIGUOUS"
            self._host._record_structure_reason("CANDIDATE_ROW_AMBIGUOUS")
            state.display_observations.append(
                DisplayObservation(None, "OBSERVATION_UNCERTAIN", box["y"])
            )
            await emit_row(candidate.diagnostic, display=state.display_observations[-1])
            return None, scroll_anchor
        return (
            CandidateWork(
                state,
                candidate.row_index,
                candidate.root,
                candidate.structure,
                candidate.carried_row,
                candidate.diagnostic,
                candidate.root_handle,
                box,
                time.perf_counter(),
                emit_row,
            ),
            scroll_anchor,
        )

    async def _read_candidate(self, work: CandidateWork):
        request = work.state.request
        try:
            if request.currently_visible:
                target = await self._read_visible_candidate(work)
                if target is None:
                    return None
            else:
                target = await self._host._read_card(
                    work.root,
                    currently_visible=False,
                    viewport_y=work.box["y"],
                    cached_body_fingerprints=request.prefer_new_fingerprints,
                )
        except _CardDeferredOffscreen as deferred:
            await self._record_deferred_candidate(work, deferred)
            return None
        except _CardNotReady as pending:
            await self._recovery.handle_pending(work, pending)
            return None
        except _UnverifiedFeedCandidate as error:
            recovered = await self._recovery.handle_unverified(work, error)
            if recovered is None:
                return None
            target, _body_stable = recovered
        self._host.last_scan_timings.read_row_ms += round(
            (time.perf_counter() - work.read_started) * 1000
        )
        return target

    async def _read_visible_candidate(self, work: CandidateWork):
        stabilize_started = time.perf_counter()
        target, body_stable = await self._host._read_stable_visible_card(
            work.root,
            viewport_y=work.box["y"],
            cached_body_fingerprints=work.state.request.prefer_new_fingerprints,
            expected_root_handle=work.root_handle,
            allow_hydration_retry=work.structure.readable_body_candidates == 0,
        )
        self._host.last_scan_timings.stabilize_ms += round(
            (time.perf_counter() - stabilize_started) * 1000
        )
        if body_stable:
            return target
        await self._record_unstable_visible_read(work, target)
        return None

    async def _record_unstable_visible_read(self, work: CandidateWork, target) -> None:
        counts = work.state.counts
        diagnostic = work.diagnostic
        diagnostic.state = "OBSERVED_UNVERIFIED"
        diagnostic.reason = "ROW_CONTENT_CHANGED_DURING_READ"
        diagnostic.identity_fingerprint = target.fingerprint
        prior = work.state.candidate_prior_fingerprints.get(work.row_index)
        if prior not in {None, target.fingerprint}:
            diagnostic.observation_id = secrets.token_hex(8)
        work.state.candidate_prior_fingerprints[work.row_index] = target.fingerprint
        diagnostic.read_evidence = target.read_evidence
        diagnostic.has_readable_body = bool(target.body and target.body.strip())
        counts.uncertain_rows += 1
        counts.skipped_rows += 1
        if diagnostic.has_readable_body:
            counts.body_extracted += 1
            work.state.display_observations.append(
                DisplayObservation(
                    target.body,
                    viewport_y=work.box["y"],
                    read_evidence=target.read_evidence,
                )
            )
        else:
            counts.body_unknown += 1
            work.state.display_observations.append(
                DisplayObservation(
                    None,
                    "OBSERVATION_UNCERTAIN",
                    work.box["y"],
                    read_evidence=target.read_evidence,
                )
            )
        await work.emit_row(
            diagnostic,
            display=work.state.display_observations[-1],
            fingerprint=target.fingerprint,
        )

    async def _record_deferred_candidate(
        self,
        work: CandidateWork,
        deferred: _CardDeferredOffscreen,
    ) -> None:
        self._host.last_scan_timings.read_row_ms += round(
            (time.perf_counter() - work.read_started) * 1000
        )
        counts = work.state.counts
        counts.deferred_offscreen += 1
        counts.skipped_rows += 1
        self._host.last_scan_timings.wait_reason = "DEFERRED_OFFSCREEN"
        work.diagnostic.state = "DEFERRED_OFFSCREEN"
        work.diagnostic.reason = deferred.reason
        work.diagnostic.identity_fingerprint = deferred.fingerprint
        await work.emit_row(work.diagnostic, fingerprint=deferred.fingerprint)

    async def _record_verified_target(self, work: CandidateWork, target) -> None:
        state = work.state
        counts = state.counts
        diagnostic = work.diagnostic
        diagnostic.identity_fingerprint = target.fingerprint
        if state.candidate_prior_fingerprints.get(work.row_index) not in {
            None,
            target.fingerprint,
        }:
            diagnostic.observation_id = secrets.token_hex(8)
        state.candidate_prior_fingerprints[work.row_index] = target.fingerprint
        diagnostic.read_evidence = target.read_evidence
        diagnostic.state = "VERIFIED" if target.body_complete else "PARTIAL_BODY"
        diagnostic.reason = (
            "PROVENANCE_VERIFIED" if target.body_complete else "VISIBLE_TEXT_PROGRESS"
        )
        counts.identity_verified += 1
        counts.verified += int(target.body_complete)
        counts.partial_body_rows += int(not target.body_complete)
        diagnostic.has_readable_body = bool(target.body and target.body.strip())
        if not diagnostic.has_readable_body:
            await self._record_missing_body(work, target)
            return
        self._record_body_cache_and_counts(work, target)
        if not target.body_complete:
            counts.skipped_rows += 1
            counts.coverage_complete = False
            await work.emit_row(diagnostic, target=target, fingerprint=target.fingerprint)
            return
        if target.fingerprint in state.scanned_fingerprints or (
            state.request.prefer_new_fingerprints is not None
            and target.fingerprint in state.request.prefer_new_fingerprints
        ):
            state.hooks.increment_feed_duplicates()
        else:
            state.scanned_fingerprints.add(target.fingerprint)
            state.targets.append(target)
        await work.emit_row(diagnostic, target=target, fingerprint=target.fingerprint)

    def _record_body_cache_and_counts(self, work: CandidateWork, target) -> None:
        state = work.state
        counts = state.counts
        cached_fingerprints = state.request.prefer_new_fingerprints
        body_was_cached = bool(
            cached_fingerprints is not None
            and target.fingerprint in cached_fingerprints
            and target.fingerprint in self._host._cached_home_bodies
        )
        counts.body_read += int(not body_was_cached and target.body_complete)
        counts.body_extracted += 1
        if target.body_complete:
            self._host._cached_home_bodies.setdefault(
                target.fingerprint,
                (target.body, target.body_truncated),
            )

    async def _record_missing_body(self, work: CandidateWork, target) -> None:
        counts = work.state.counts
        diagnostic = work.diagnostic
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
            self._host._record_structure_reason("POST_BODY_STRUCTURE_UNSUPPORTED")
        elif diagnostic.state == "PENDING_HYDRATION":
            _, body_signals, _ = await self._host._observe_display_body(work.root)
            work.state.pending_signals.extend(body_signals)
        counts.skipped_rows += 1
        counts.uncertain_rows += 1
        diagnostic.identity_fingerprint = target.fingerprint
        await work.emit_row(diagnostic, target=target, fingerprint=target.fingerprint)
