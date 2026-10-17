"""Bounded feed advancement and measured post-scroll progress decisions."""

from __future__ import annotations

from collections.abc import Callable

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from ..browser_ui.feed_rows import _FeedScanCounts
from ..console_output import ConsoleOutputPolicy, emit_console_diagnostic
from ..errors import UiReadError
from ..reader import ThreadsHomeReader
from .state import CoverageLedger, NurtureRunState, WorkflowPhase, WorkflowStats


class FeedScrollController:
    """Own one cycle's scroll baseline, UI transition and progress stop checks."""

    def __init__(
        self,
        *,
        page: Page,
        reader: ThreadsHomeReader,
        stats: WorkflowStats,
        coverage: CoverageLedger,
        state: NurtureRunState,
        console_output_policy: ConsoleOutputPolicy,
        detail: bool,
        map_playwright_error: Callable[..., UiReadError],
    ) -> None:
        self._page = page
        self._reader = reader
        self._stats = stats
        self._coverage = coverage
        self._state = state
        self._console_output_policy = console_output_policy
        self._detail = detail
        self._map_playwright_error = map_playwright_error
        self.pending_scroll_baseline: dict[str, object] | None = None

    async def advance(self, *, scan_after_scroll: bool = False) -> bool:
        attempt = self._reader.scroll_attempts + 1
        baseline = {
            "visible_text_segments": self._stats.visible_text_segments,
            "unique_verified_posts": len(self._coverage.fully_read),
            "body_extracted": self._stats.body_extracted,
            "observed_bodies": len(self._coverage.observed_body_identities),
            "rows": {
                diagnostic.observation_id: diagnostic.read_evidence
                for diagnostic in self._reader.last_candidate_diagnostics
                if diagnostic.observation_id is not None and diagnostic.read_evidence is not None
            },
        }
        if self._detail:
            emit_console_diagnostic(self._console_output_policy, f"[SCROLL] attempt={attempt} status=REQUESTED")
        self._state.transition(WorkflowPhase.SCROLLING)
        self._reader.set_failure_context(self._state.phase.value, "SCROLL")
        try:
            advanced = await self._reader.advance_feed(scan_after_scroll=scan_after_scroll)
        except (UiReadError, PlaywrightError) as raw_error:
            error = (
                raw_error
                if isinstance(raw_error, UiReadError)
                else self._map_playwright_error(
                    self._page,
                    raw_error,
                    phase=self._state.phase.value,
                    operation="SCROLL",
                    console_output_policy=self._console_output_policy,
                )
            )
            self._state.stop(error.code)
            if self._detail:
                emit_console_diagnostic(self._console_output_policy, f"[SCROLL] attempt={attempt} status=FAILED code={error.code}")
            raise error from None
        if self._detail:
            gesture = self._reader.last_scroll_gesture
            wheel_steps = 0 if gesture is None else len(gesture.wheel_deltas)
            gesture_ms = 0 if gesture is None else gesture.duration_ms
            if advanced:
                emit_console_diagnostic(self._console_output_policy,
                    f"[SCROLL] attempt={self._reader.scroll_attempts} status=ADVANCED "
                    f"observed=1 delta_px={self._reader.last_scroll_delta:g} "
                    f"wheel_steps={wheel_steps} gesture_ms={gesture_ms}"
                )
            else:
                emit_console_diagnostic(self._console_output_policy,
                    f"[SCROLL] attempt={attempt} status=STOPPED "
                    f"reason={self._reader.last_scroll_stop_reason or 'NOT_ADVANCED'}"
                )
        if advanced:
            self._state.transition(WorkflowPhase.WAITING_FOR_CONTENT)
            self._state.transition(WorkflowPhase.VERIFYING)
            self.pending_scroll_baseline = baseline
            self._state.transition(WorkflowPhase.OBSERVING)
        else:
            self._state.transition(WorkflowPhase.OBSERVING)
        return advanced


    def record_scan_progress(
        self,
        row_target_progress_ids: set[str],
        scan_counts: _FeedScanCounts,
    ) -> str | None:
        exhaustion_reason: str | None = None
        if self.pending_scroll_baseline is not None:
            baseline_segments = int(
                self.pending_scroll_baseline.get("visible_text_segments", self._stats.visible_text_segments)
            )
            baseline_verified = int(
                self.pending_scroll_baseline.get("unique_verified_posts", len(self._coverage.fully_read))
            )
            baseline_bodies = int(
                self.pending_scroll_baseline.get(
                    "observed_bodies",
                    len(self._coverage.observed_body_identities),
                )
            )
            new_segments = max(0, self._stats.visible_text_segments - baseline_segments)
            new_observed_bodies = max(
                0,
                len(self._coverage.observed_body_identities) - baseline_bodies,
            )
            new_verified = max(
                0,
                len(self._coverage.fully_read) - baseline_verified,
            )
            approach_deltas = [
                -diagnostic.text_distance_delta_px
                for diagnostic in self._reader.last_candidate_diagnostics
                if "ROW_MOVED_TOWARD_VIEW" in diagnostic.scroll_reason_flags
                and diagnostic.text_distance_delta_px is not None
            ]
            text_approach_delta = max(approach_deltas, default=None)
            per_row_approach = bool(row_target_progress_ids)
            made_progress = bool(
                new_segments or new_observed_bodies or new_verified or per_row_approach
            )
            self._stats.consecutive_moved_no_content = (
                0 if made_progress else self._stats.consecutive_moved_no_content + 1
            )
            for diagnostic in self._reader.last_candidate_diagnostics:
                metrics = diagnostic.read_evidence
                if metrics is None:
                    continue

                def px(value: float | None) -> str:
                    return "UNKNOWN" if value is None else f"{value:.1f}"

                def count(value: int | None) -> str:
                    return "UNKNOWN" if value is None else str(value)

                emit_console_diagnostic(self._console_output_policy,
                    f"[SCROLL_ROW_PROGRESS] attempt={self._reader.scrolls} "
                    f"row_id={diagnostic.observation_id or 'UNTRACKED'} "
                    f"root_intersects={'UNKNOWN' if metrics.root_intersects is None else str(metrics.root_intersects).lower()} "
                    f"body_text_nodes_present={count(metrics.body_text_nodes_present)} "
                    f"render_rect_count={count(metrics.render_rect_count)} "
                    f"visible_text_rect_count={count(metrics.visible_text_rect_count)} "
                    f"deferred_above_rect_count={count(metrics.deferred_above_rect_count)} "
                    f"deferred_below_rect_count={count(metrics.deferred_below_rect_count)} "
                    f"closest_text_distance_px={px(metrics.closest_text_distance_px)} "
                    f"root_position_band={metrics.root_position_band} "
                    f"same_scroller_scrollTop_delta={'UNKNOWN' if diagnostic.same_scroller_scroll_top_delta is None else f'{diagnostic.same_scroller_scroll_top_delta:.1f}'} "
                    f"target_approached={str(diagnostic.observation_id in row_target_progress_ids).lower()} "
                    f"scroll_surface_key={metrics.scroll_surface_key or 'UNKNOWN'} "
                    f"wheel_surface_ancestor={'UNKNOWN' if diagnostic.wheel_surface_matches is None else str(diagnostic.wheel_surface_matches).lower()} "
                    f"row_top_before_px={px(diagnostic.row_top_before_px)} "
                    f"row_bottom_before_px={px(diagnostic.row_bottom_before_px)} "
                    f"clip_top_before_px={px(diagnostic.clip_top_before_px)} "
                    f"clip_bottom_before_px={px(diagnostic.clip_bottom_before_px)} "
                    f"text_top_before_px={px(diagnostic.text_top_before_px)} "
                    f"text_bottom_before_px={px(diagnostic.text_bottom_before_px)} "
                    f"row_top_px={px(metrics.row_top_px)} "
                    f"row_bottom_px={px(metrics.row_bottom_px)} "
                    f"clip_top_px={px(metrics.clip_top_px)} "
                    f"clip_bottom_px={px(metrics.clip_bottom_px)} "
                    f"text_top_px={px(metrics.text_rect_top_px)} "
                    f"text_bottom_px={px(metrics.text_rect_bottom_px)} "
                    f"row_movement_px={px(diagnostic.row_movement_px)} "
                    f"text_distance_delta_px={px(diagnostic.text_distance_delta_px)} "
                    f"scroll_flags={','.join(diagnostic.scroll_reason_flags) or 'NONE'} "
                    f"body_evidence={metrics.state}",
                    flush=True,
                )
            geometry = self._reader.last_scroll_geometry or {}
            if geometry:
                emit_console_diagnostic(self._console_output_policy,
                    f"[SCROLL_GEOMETRY] surface_key={geometry.get('surface_key', 'UNKNOWN')} "
                    f"surface_tag={geometry.get('surface_tag', 'UNKNOWN')} "
                    f"overflow_y={geometry.get('overflow_y', 'UNKNOWN')} "
                    "hit_test=VALIDATED "
                    f"hit_test_tag={geometry.get('hit_test_tag', 'UNKNOWN')} "
                    f"scrollTop_before={geometry.get('scroll_top_before', 'UNKNOWN')} "
                    f"scrollTop_after={geometry.get('scroll_top_after', 'UNKNOWN')} "
                    f"scrollTop_delta={geometry.get('scroll_top_delta', 'UNKNOWN')} "
                    f"clip_top_before={geometry.get('clip_top_before', 'UNKNOWN')} "
                    f"clip_top_after={geometry.get('clip_top_after', 'UNKNOWN')} "
                    f"clip_bottom_before={geometry.get('clip_bottom_before', 'UNKNOWN')} "
                    f"clip_bottom_after={geometry.get('clip_bottom_after', 'UNKNOWN')} "
                    f"scroll_height_before={geometry.get('scroll_height_before', 'UNKNOWN')} "
                    f"scroll_height_after={geometry.get('scroll_height_after', 'UNKNOWN')} "
                    f"scroll_height_delta={geometry.get('scroll_height_delta', 'UNKNOWN')}",
                    flush=True,
                )
            emit_console_diagnostic(self._console_output_policy,
                f"[SCROLL_PROGRESS] attempt={self._reader.scrolls} "
                f"state={'ADVANCED_WITH_CONTENT' if made_progress else 'MOVED_NO_NEW_CONTENT'} "
                f"new_segments={new_segments} "
                f"new_observed_bodies={new_observed_bodies} "
                f"new_verified_posts={new_verified} "
                f"tracked_rows_approaching={len(row_target_progress_ids)} "
                f"text_approach_delta_px={'UNKNOWN' if text_approach_delta is None else f'{text_approach_delta:.1f}'} "
                f"deferred_above_rects={scan_counts.deferred_above} "
                f"deferred_below_rects={scan_counts.deferred_below} "
                f"viewport_row_appearances={self._stats.observed_rows} "
                f"unique_provenance_identities={len(self._coverage.provenance_verified)} "
                f"unique_verified_posts={len(self._coverage.fully_read)} "
                f"unresolved_gaps={self._coverage.unresolved_gap_count} "
                f"band={self._reader.last_scroll_band} "
                f"end_proximity={'NEAR' if self._reader.last_scroll_near_end else 'FAR'} "
                f"end_distance_px={'UNKNOWN' if self._reader.last_scroll_end_distance is None else f'{self._reader.last_scroll_end_distance:.1f}'} "
                f"no_new_content_streak={self._stats.consecutive_moved_no_content}",
                flush=True,
            )
            self.pending_scroll_baseline = None
            if self._stats.consecutive_moved_no_content >= 3:
                if (
                    self._reader.last_scroll_end_distance is not None
                    and self._reader.last_scroll_end_distance <= 1
                    and self._stats.viewport_coverage_complete
                ):
                    exhaustion_reason = "FEED_END_REACHED"
                else:
                    exhaustion_reason = "READ_PROGRESS_STALLED"
                    self._coverage.unrepresented_gaps.add(f"stalled:{self._reader.scrolls}")
                    self._stats.viewport_coverage_complete = False
                emit_console_diagnostic(self._console_output_policy,
                    f"[SCROLL_PROGRESS] status=STOPPED reason={exhaustion_reason} "
                    "coverage=INCOMPLETE "
                    f"unresolved_gaps={self._coverage.unresolved_gap_count}",
                    flush=True,
                )
                return exhaustion_reason
        return None
