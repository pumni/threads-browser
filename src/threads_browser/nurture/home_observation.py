"""Record one bounded Home viewport's row observations and coverage."""

from __future__ import annotations

from hashlib import sha256

from ..console_output import ConsoleOutputPolicy, emit_console_diagnostic
from ..feed.models import BodyReadEvidence, PostTarget, VisibleBodySegment
from ..feed.relevance import classify_body
from ..reader import ThreadsHomeReader, _HomeRowObservation
from .diagnostics import (
    _print_detail_body,
    _print_observed_text,
    _print_post_text,
    _print_visible_segments,
)
from .observation import BodyRangeCoverageFacts, RowObservationFacts
from .scroll_evidence import annotate_scroll_evidence
from .state import CoverageLedger, DedupeLedger, WorkflowStats


class HomeRowObserver:
    """Own dedupe, classification, evidence and overlap accounting for a viewport."""

    def __init__(
        self,
        *,
        reader: ThreadsHomeReader,
        coverage: CoverageLedger,
        dedupe: DedupeLedger,
        stats: WorkflowStats,
        viewport_number: int,
        normalized_terms: tuple[str, ...],
        console_output_policy: ConsoleOutputPolicy,
        previous_unverified_rows: list[tuple[str, float]],
        pending_scroll_baseline: dict[str, object] | None,
        detail: bool,
        show_post_text: bool,
        show_observed_text: bool,
    ) -> None:
        self.reader = reader
        self.coverage = coverage
        self.dedupe = dedupe
        self.stats = stats
        self.viewport_number = viewport_number
        self.normalized_terms = normalized_terms
        self.console_output_policy = console_output_policy
        self.previous_unverified_rows = previous_unverified_rows
        self.pending_scroll_baseline = pending_scroll_baseline
        self.detail = detail
        self.show_post_text = show_post_text
        self.show_observed_text = show_observed_text
        self.viewport_relevant_targets: list[PostTarget] = []
        self.viewport_fallback_targets: list[PostTarget] = []
        self.current_unverified_rows: list[tuple[str, float]] = []
        self.matched_overlap_rows: set[int] = set()
        self.row_target_progress_ids: set[str] = set()
        self.emitted_verified_fingerprints: set[str] = set()
        self.completed_viewport_fingerprints: set[str] = set()
        self.viewport_classified = 0
        self.viewport_irrelevant = 0
        self.viewport_observation_unknown = 0
        self.viewport_relevant = 0
        self.viewport_unverified_relevant = 0
        self.viewport_unknown = 0

    async def observe(self, row: _HomeRowObservation) -> None:
        diagnostic = row.diagnostic
        target = row.target
        if diagnostic.state == "VERIFIED" and target is not None:
            if (
                self.reader.last_scan_retry_count
                and target.fingerprint in self.emitted_verified_fingerprints
            ):
                return
            self.emitted_verified_fingerprints.add(target.fingerprint)
        evidence = self._annotate_scroll_progress(row)
        self._record_coverage(row, evidence)
        self._emit_detail_row_diagnostics(row, evidence)
        if target is not None:
            self._record_verified_target(row)
        else:
            self._record_unverified_observation(row)

    def _annotate_scroll_progress(
        self,
        row: _HomeRowObservation,
    ) -> BodyReadEvidence | None:
        moved_toward_view = annotate_scroll_evidence(
            self.reader,
            row,
            self.pending_scroll_baseline,
        )
        observation_id = row.diagnostic.observation_id
        if moved_toward_view and observation_id is not None:
            self.row_target_progress_ids.add(observation_id)
        return row.diagnostic.read_evidence

    def _record_coverage(
        self,
        row: _HomeRowObservation,
        evidence: BodyReadEvidence | None,
    ) -> None:
        target = row.target
        diagnostic = row.diagnostic
        row_fingerprint = (
            target.fingerprint
            if target is not None
            else row.fingerprint or diagnostic.identity_fingerprint
        )
        has_body = bool(
            (target is not None and target.body and target.body.strip())
            or (
                target is not None
                and any(segment.text.strip() for segment in target.visible_segments)
            )
            or (row.display is not None and row.display.body and row.display.body.strip())
        )
        coverage_outcome = self.coverage.observe_row(
            RowObservationFacts(
                fingerprint=row_fingerprint,
                observation_id=diagnostic.observation_id,
                gap_id=f"row:{self.viewport_number}:{diagnostic.row_index}",
                state=diagnostic.state,
                has_body=has_body,
                body_complete=bool(
                    diagnostic.state == "VERIFIED" and target is not None and target.body_complete
                ),
            )
        )
        if row_fingerprint is not None and evidence is not None:
            self.coverage.observe_body_range(
                row_fingerprint,
                BodyRangeCoverageFacts(
                    body_complete=bool(
                        diagnostic.state == "VERIFIED"
                        and target is not None
                        and target.body_complete
                    ),
                    missing_range_count=evidence.missing_range_count,
                    unrecoverable_range_count=evidence.unrecoverable_range_count,
                    unrecoverable_range_signature=evidence.unrecoverable_range_signature,
                ),
            )
        self.stats.provenance_verified += int(coverage_outcome.identity_added)
        self.reader.last_scan_counts.new_verified_rows += int(coverage_outcome.body_completed)
        if coverage_outcome.body_completed and row_fingerprint is not None:
            self.completed_viewport_fingerprints.add(row_fingerprint)

    def _emit_detail_row_diagnostics(
        self,
        row: _HomeRowObservation,
        evidence: BodyReadEvidence | None,
    ) -> None:
        diagnostic = row.diagnostic
        if self.detail:
            observation_emitted = bool(
                (row.target and (row.target.body or row.target.visible_segments))
                or (row.display and (row.display.body or row.display.visible_segments))
            )
            metrics = evidence

            def metric_count(value: int | None) -> str:
                return "UNKNOWN" if value is None else str(value)

            emit_console_diagnostic(
                self.console_output_policy,
                f"[ROW row_id={diagnostic.observation_id or 'UNTRACKED'} "
                f"viewport_row={diagnostic.row_index}] root={diagnostic.root_kind} "
                f"has_visible_time={str(diagnostic.has_visible_time).lower()} "
                f"has_post_permalink={str(diagnostic.has_post_permalink).lower()} "
                f"has_profile_author={str(diagnostic.has_profile_author).lower()} "
                f"has_readable_body={str(diagnostic.has_readable_body).lower()} "
                f"root_intersects={'UNKNOWN' if metrics is None or metrics.root_intersects is None else str(metrics.root_intersects).lower()} "
                f"body_text_nodes_present={metric_count(None if metrics is None else metrics.body_text_nodes_present)} "
                f"render_rect_count={metric_count(None if metrics is None else metrics.render_rect_count)} "
                f"visible_text_rect_count={metric_count(None if metrics is None else metrics.visible_text_rect_count)} "
                f"fully_contained_text_rect_count={metric_count(None if metrics is None else metrics.fully_contained_text_rect_count)} "
                f"intersect_only_text_rect_count={metric_count(None if metrics is None else metrics.intersect_only_text_rect_count)} "
                f"deferred_above_rect_count={metric_count(None if metrics is None else metrics.deferred_above_rect_count)} "
                f"deferred_below_rect_count={metric_count(None if metrics is None else metrics.deferred_below_rect_count)} "
                f"closest_text_distance_px={'UNKNOWN' if metrics is None or metrics.closest_text_distance_px is None else f'{metrics.closest_text_distance_px:.1f}'} "
                f"root_position_band={'UNKNOWN' if metrics is None else metrics.root_position_band} "
                f"same_scroller_scrollTop_delta={'UNKNOWN' if diagnostic.same_scroller_scroll_top_delta is None else f'{diagnostic.same_scroller_scroll_top_delta:.1f}'} "
                f"scroll_surface={'UNKNOWN' if metrics is None else metrics.scroll_surface_key} "
                f"body_evidence={'UNKNOWN' if metrics is None else metrics.state} "
                f"body_incomplete_reason={'UNKNOWN' if metrics is None else metrics.body_incomplete_reason} "
                f"expected_range_count={metric_count(None if metrics is None else metrics.expected_range_count)} "
                f"visited_range_count={metric_count(None if metrics is None else metrics.visited_range_count)} "
                f"missing_subrange_count={metric_count(None if metrics is None else metrics.missing_subrange_count)} "
                f"missing_range_topology={'UNKNOWN' if metrics is None else ','.join(metrics.missing_range_topology) or 'NONE'} "
                f"unrecoverable_range_count={metric_count(None if metrics is None else metrics.unrecoverable_range_count)} "
                f"scroll_flags={','.join(diagnostic.scroll_reason_flags) or 'NONE'} "
                f"state={diagnostic.state} reason={diagnostic.reason} "
                f"observation={'EMITTED' if observation_emitted else 'RECORDED'} "
                "checkpoint=PENDING",
                flush=True,
            )

    def _record_verified_target(self, row: _HomeRowObservation) -> None:
        target = row.target
        if target is None:
            return
        if row.diagnostic.state == "PARTIAL_BODY":
            self._record_partial_target(target, row.diagnostic.row_index)
            return
        if row.diagnostic.state != "VERIFIED" or not target.body or not target.body_complete:
            return

        self._record_verified_segments(target, row.diagnostic.row_index)
        if target.fingerprint in self.dedupe.home_observed_this_session:
            self._record_seen_target(target)
            return
        self._record_new_verified_target(target)

    def _record_partial_target(self, target: PostTarget, row_index: int) -> None:
        new_segments = self._new_segments(target)
        if self.detail or self.show_post_text:
            _print_visible_segments(
                f"viewport={self.viewport_number} row={row_index}",
                new_segments,
            )
            if new_segments:
                self.dedupe.streamed_home_fingerprints.add(target.fingerprint)

    def _record_verified_segments(self, target: PostTarget, row_index: int) -> None:
        new_segments = self._new_segments(target)
        if (
            new_segments
            and target.fingerprint in self.dedupe.streamed_home_fingerprints
            and (self.detail or self.show_post_text)
        ):
            _print_visible_segments(
                f"viewport={self.viewport_number} row={row_index}",
                new_segments,
            )

    def _new_segments(self, target: PostTarget) -> tuple[VisibleBodySegment, ...]:
        new_segments = tuple(
            segment
            for segment in target.visible_segments
            if segment.segment_id not in self.dedupe.streamed_segment_ids
        )
        self.stats.visible_text_segments += len(new_segments)
        self.dedupe.streamed_segment_ids.update(segment.segment_id for segment in new_segments)
        return new_segments

    def _record_seen_target(self, target: PostTarget) -> None:
        fingerprint = target.fingerprint
        if fingerprint in self.dedupe.checkpoint_seen:
            if fingerprint in self.dedupe.checkpoint_overlap:
                self.stats.duplicates += 1
            else:
                self.dedupe.checkpoint_overlap.add(fingerprint)
        if self.detail and fingerprint not in self.dedupe.streamed_home_fingerprints:
            emit_console_diagnostic(
                self.console_output_policy,
                f"[POST post={self.dedupe.post_numbers[fingerprint]}] "
                "status=VERIFIED decision=OVERLAP_DUPLICATE HOME_BODY=OMITTED",
                flush=True,
            )
        decision = self.dedupe.decisions_by_fingerprint.get(fingerprint)
        if decision == "RELEVANT" and fingerprint in self.dedupe.pending_set:
            if target.currently_visible:
                self.viewport_relevant_targets.append(target)
        elif (
            target.body is not None
            and target.body.strip()
            and decision != "RELEVANT"
            and target.currently_visible
        ):
            self.viewport_fallback_targets.append(target)

    def _record_new_verified_target(self, target: PostTarget) -> None:
        fingerprint = target.fingerprint
        self.dedupe.home_observed_this_session.add(fingerprint)
        self.dedupe.next_post_number += 1
        self.dedupe.post_numbers[fingerprint] = self.dedupe.next_post_number
        self.stats.body_extracted += int(bool(target.body and target.body.strip()))
        self.viewport_classified += 1
        is_new = fingerprint not in self.dedupe.checkpoint_seen
        if is_new:
            self.dedupe.new_fingerprints.add(fingerprint)
        decision = classify_body(target.body, self.normalized_terms)
        self.dedupe.decisions_by_fingerprint[fingerprint] = decision
        self.stats.classified += 1
        self._emit_verified_body(target, decision)
        self._record_decision(target, decision, is_new)

    def _emit_verified_body(self, target: PostTarget, decision: str) -> None:
        fingerprint = target.fingerprint
        if self.detail:
            if fingerprint not in self.dedupe.streamed_home_fingerprints:
                if _print_detail_body(
                    "HOME",
                    f"post={self.dedupe.post_numbers[fingerprint]}",
                    "VERIFIED",
                    decision,
                    "TRUNCATED" if target.body_truncated else "COMPLETE",
                    target.body,
                ):
                    self.stats.previewed_bodies += 1
                self.dedupe.streamed_home_fingerprints.add(fingerprint)
            else:
                emit_console_diagnostic(
                    self.console_output_policy,
                    f"[POST post={self.dedupe.post_numbers[fingerprint]}] "
                    f"status=VERIFIED decision={decision} "
                    f"extent={'TRUNCATED' if target.body_truncated else 'COMPLETE'} "
                    f"chars={len(target.body)} text=STREAMED",
                    flush=True,
                )
        elif (
            self.show_post_text
            and fingerprint not in self.dedupe.streamed_home_fingerprints
            and _print_post_text(self.viewport_number, decision, target.body)
        ):
            self.stats.previewed_bodies += 1

    def _record_decision(self, target: PostTarget, decision: str, is_new: bool) -> None:
        if decision == "RELEVANT":
            self.stats.relevant += 1
            self.viewport_relevant += 1
            self.stats.found_relevant = True
            if is_new and target.fingerprint not in self.dedupe.pending_set:
                self.dedupe.pending.append(target.fingerprint)
                self.dedupe.pending_set.add(target.fingerprint)
            if target.fingerprint in self.dedupe.pending_set and target.currently_visible:
                self.viewport_relevant_targets.append(target)
        elif decision == "IRRELEVANT":
            self.stats.irrelevant += 1
            self.viewport_irrelevant += 1
            if target.currently_visible:
                self.viewport_fallback_targets.append(target)
        else:
            self.stats.unknown += 1
            self.viewport_unknown += 1
            if target.currently_visible:
                self.viewport_fallback_targets.append(target)

    def _record_unverified_observation(self, row: _HomeRowObservation) -> None:
        diagnostic = row.diagnostic
        observation = row.display
        if observation is None:
            return
        if observation.body is None or not observation.body.strip():
            self.stats.observation_unknown += 1
            self.viewport_observation_unknown += 1
            if self.detail:
                emit_console_diagnostic(
                    self.console_output_policy,
                    f"[POST viewport={self.viewport_number} row={diagnostic.row_index}] "
                    "status=OBSERVATION_UNCERTAIN decision=UNKNOWN body=UNAVAILABLE",
                    flush=True,
                )
            elif self.show_observed_text:
                _print_observed_text(self.viewport_number, "UNKNOWN_UNVERIFIED", observation)
            return
        body_digest = sha256(
            self.dedupe.display_salt + observation.body.encode("utf-8")
        ).hexdigest()
        overlap_duplicate = False
        for previous_index, (previous_digest, previous_y) in enumerate(
            self.previous_unverified_rows
        ):
            if self.matched_overlap_rows and previous_index in self.matched_overlap_rows:
                continue
            if previous_digest != body_digest:
                continue
            if abs((previous_y - self.reader.last_scroll_delta) - observation.viewport_y) <= 48:
                self.matched_overlap_rows.add(previous_index)
                overlap_duplicate = True
                break
        if not overlap_duplicate:
            overlap_duplicate = any(
                current_digest == body_digest and abs(current_y - observation.viewport_y) <= 2
                for current_digest, current_y in self.current_unverified_rows
            )
        if overlap_duplicate:
            if self.detail:
                emit_console_diagnostic(
                    self.console_output_policy,
                    f"[POST viewport={self.viewport_number} row={diagnostic.row_index}] "
                    "status=OBSERVED_UNVERIFIED decision=OVERLAP_DUPLICATE "
                    "HOME_BODY=OMITTED",
                    flush=True,
                )
            return
        self.current_unverified_rows.append((body_digest, observation.viewport_y))
        self.stats.body_extracted += 1
        decision = classify_body(observation.body, self.normalized_terms)
        self.stats.classified += 1
        self.viewport_classified += 1
        if decision == "RELEVANT":
            decision = "RELEVANT_UNVERIFIED"
            self.stats.unverified_relevant += 1
            if diagnostic.state != "OBSERVED_UNVERIFIED":
                self.stats.unsettled_unverified_relevance += 1
            self.viewport_unverified_relevant += 1
        elif decision == "IRRELEVANT":
            decision = "IRRELEVANT_UNVERIFIED"
        else:
            decision = "UNKNOWN_UNVERIFIED"
            self.stats.observation_unknown += 1
            self.viewport_observation_unknown += 1
        if self.detail:
            if _print_detail_body(
                "HOME",
                f"viewport={self.viewport_number} row={diagnostic.row_index}",
                "OBSERVED_UNVERIFIED",
                decision,
                "OBSERVED",
                observation.body,
            ):
                self.stats.previewed_bodies += 1
        elif self.show_observed_text:
            _print_observed_text(self.viewport_number, decision, observation)
