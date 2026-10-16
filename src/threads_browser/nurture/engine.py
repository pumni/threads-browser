"""Bounded, read-only Home / For You nurture orchestration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from ..browser_ui.session import classify_playwright_failure, emit_failure_diagnostic
from ..feed.models import BodyReadEvidence, PostTarget
from ..feed.relevance import canonical_terms, classify_body
from ..reader import ThreadsHomeReader, UiReadError, _HomeRowObservation
from ..storage import BrowserAccount, BrowserDataError, Checkpoint, CheckpointStore
from .diagnostics import (
    _detail_stop,
    _format_structure_diagnostics,
    _print_detail_body,
    _print_observed_text,
    _print_post_text,
    _print_url_structure_diagnostics,
    _print_visible_segments,
)
from .observation import BodyRangeCoverageFacts, RowObservationFacts
from .state import (
    CoverageLedger,
    DedupeLedger,
    NurtureRunState,
    WorkflowPhase,
    WorkflowStats,
    policy_fingerprint,
)

_MAX_SEEN = 5_000
_DETAIL_READ_FAILURES = frozenset(
    {
        "DETAIL_RENDER_PENDING",
        "DETAIL_RENDER_UNAVAILABLE",
        "DETAIL_ROOT_UNVERIFIED",
        "DETAIL_ROOT_AMBIGUOUS",
        "DETAIL_BODY_UNVERIFIED",
        "DETAIL_PROVENANCE_UNVERIFIED",
    }
)


def _reader_playwright_error(
    page: Page,
    error: PlaywrightError,
    *,
    phase: str,
    operation: str,
) -> UiReadError:
    failure = classify_playwright_failure(page, error)
    emit_failure_diagnostic(
        phase=phase,
        operation=operation,
        error_class=failure.error_class,
        page_closed=failure.page_closed,
        context_closed=failure.context_closed,
    )
    code = (
        "BROWSER_SESSION_UNAVAILABLE"
        if failure.error_class == "TARGET_CLOSED"
        else "DOM_CHANGED_DURING_SCAN"
        if failure.error_class == "DOM_DETACHED" and operation == "SCAN_ROW"
        else "UI_READ_UNVERIFIED"
    )
    return UiReadError(code)


@dataclass(frozen=True, slots=True)
class NurtureResult:
    status: str
    home_sampled: int
    home_new: int
    home_relevant: int
    home_irrelevant: int
    home_unknown: int
    duplicates: int
    detail_reads: int
    scroll_attempts: int
    scrolls: int
    checkpoint_runs: int
    home_relevant_ratio: float | None
    skipped_unverified: int = 0
    ui_status: str = "UNVERIFIED_LIVE_UI"
    observed_rows: int = 0
    body_extracted: int = 0
    provenance_verified: int = 0
    classified: int = 0
    observation_unknown: int = 0
    unverified_relevant: int = 0
    skipped_rows: int = 0
    viewport_coverage_complete: bool = True
    viewport_observations: int = 0
    unique_provenance_identities: int = 0
    unique_verified_posts: int = 0
    unique_deferred_posts: int = 0
    unresolved_gaps: int = 0
    visible_text_segments: int = 0
    stop_reason: str = "UNKNOWN"


async def run_page_workflow(
    page: Page,
    account: BrowserAccount,
    checkpoint_store: CheckpointStore,
    *,
    max_posts: int,
    terms: tuple[str, ...],
    max_scrolls: int = 8,
    max_total_posts: int = 40,
    readiness_prompt: Callable[[str, int], Awaitable[bool]] | None = None,
    show_post_text: bool = False,
    show_observed_text: bool = False,
    detail: bool = False,
    reader_factory: Callable[..., ThreadsHomeReader] = ThreadsHomeReader,
) -> NurtureResult:
    state = NurtureRunState()
    if account.expected_actor is None:
        raise BrowserDataError("ACCOUNT_NOT_VERIFIED")
    if not 1 <= max_posts <= 20:
        raise UiReadError("POST_LIMIT_INVALID")
    if not 0 <= max_scrolls <= 20:
        raise UiReadError("SCROLL_LIMIT_INVALID")
    if not 1 <= max_total_posts <= 400:
        raise UiReadError("TOTAL_SCAN_LIMIT_INVALID")
    normalized_terms = canonical_terms(terms)
    fingerprint = policy_fingerprint(normalized_terms)
    checkpoint = checkpoint_store.load(account.account_id, fingerprint)
    dedupe = DedupeLedger.from_checkpoint(
        checkpoint.seen_fingerprints, checkpoint.pending_read_fingerprints
    )
    stats = WorkflowStats()
    reader = reader_factory(
        page,
        account.expected_actor,
        readiness_prompt=readiness_prompt,
        detail_mode=detail,
    )
    reader.set_failure_context("INITIALIZING", "SETTLE")
    try:
        await reader.open_home()
    except (UiReadError, PlaywrightError) as raw_error:
        error = (
            raw_error
            if isinstance(raw_error, UiReadError)
            else _reader_playwright_error(
                page,
                raw_error,
                phase=state.phase.value,
                operation="SETTLE",
            )
        )
        state.stop(error.code)
        if detail:
            print(f"[HOME] status=FAILED code={error.code}")
        raise error from None
    state.transition(WorkflowPhase.OBSERVING)
    if detail:
        print("[HOME] status=READY account=VERIFIED source=FOR_YOU")
    selected = None
    coverage = CoverageLedger()
    previous_unverified_rows: list[tuple[str, float]] = []
    viewport_number = 0
    exhaustion_reason = "MAX_SCROLLS"
    pending_scroll_baseline: dict[str, object] | None = None
    actionability_rejected_codes: set[str] = set()

    def reliability_metrics() -> dict[str, int]:
        return coverage.reliability_metrics(
            viewport_observations=stats.observed_rows,
            visible_text_segments=stats.visible_text_segments,
        )

    async def advance_feed(*, scan_after_scroll: bool = False) -> bool:
        nonlocal pending_scroll_baseline
        attempt = reader.scroll_attempts + 1
        baseline = {
            "visible_text_segments": stats.visible_text_segments,
            "unique_verified_posts": len(coverage.fully_read),
            "body_extracted": stats.body_extracted,
            "observed_bodies": len(coverage.observed_body_identities),
            "rows": {
                diagnostic.observation_id: diagnostic.read_evidence
                for diagnostic in reader.last_candidate_diagnostics
                if diagnostic.observation_id is not None and diagnostic.read_evidence is not None
            },
        }
        if detail:
            print(f"[SCROLL] attempt={attempt} status=REQUESTED")
        state.transition(WorkflowPhase.SCROLLING)
        reader.set_failure_context(state.phase.value, "SCROLL")
        try:
            advanced = await reader.advance_feed(scan_after_scroll=scan_after_scroll)
        except (UiReadError, PlaywrightError) as raw_error:
            error = (
                raw_error
                if isinstance(raw_error, UiReadError)
                else _reader_playwright_error(
                    page,
                    raw_error,
                    phase=state.phase.value,
                    operation="SCROLL",
                )
            )
            state.stop(error.code)
            if detail:
                print(f"[SCROLL] attempt={attempt} status=FAILED code={error.code}")
            raise error from None
        if detail:
            gesture = reader.last_scroll_gesture
            wheel_steps = 0 if gesture is None else len(gesture.wheel_deltas)
            gesture_ms = 0 if gesture is None else gesture.duration_ms
            if advanced:
                print(
                    f"[SCROLL] attempt={reader.scroll_attempts} status=ADVANCED "
                    f"observed=1 delta_px={reader.last_scroll_delta:g} "
                    f"wheel_steps={wheel_steps} gesture_ms={gesture_ms}"
                )
            else:
                print(
                    f"[SCROLL] attempt={attempt} status=STOPPED "
                    f"reason={reader.last_scroll_stop_reason or 'NOT_ADVANCED'}"
                )
        if advanced:
            state.transition(WorkflowPhase.WAITING_FOR_CONTENT)
            state.transition(WorkflowPhase.VERIFYING)
            pending_scroll_baseline = baseline
            state.transition(WorkflowPhase.OBSERVING)
        else:
            state.transition(WorkflowPhase.OBSERVING)
        return advanced

    async def first_actionable(targets: list[PostTarget]) -> PostTarget | None:
        for target in targets:
            try:
                current = await reader.actionable_now(target)
            except (UiReadError, PlaywrightError) as raw_error:
                error = (
                    raw_error
                    if isinstance(raw_error, UiReadError)
                    else _reader_playwright_error(
                        page,
                        raw_error,
                        phase=state.phase.value,
                        operation="SCAN_ROW",
                    )
                )
                if error.code not in {
                    "DETAIL_NAVIGATION_UNVERIFIED",
                    "DETAIL_PROVENANCE_UNVERIFIED",
                    "POST_AUTHOR_UNVERIFIED",
                    "POST_PROVENANCE_UNVERIFIED",
                }:
                    raise error from None
                actionability_rejected_codes.add(error.code)
                if detail:
                    print(f"[ACTIONABILITY] status=REJECTED code={error.code}")
                continue
            if detail:
                print("[ACTIONABILITY] status=CONFIRMED")
            return current
        return None

    async def select_verified_fallback(targets: list[PostTarget]) -> None:
        nonlocal selected
        if stats.found_relevant or stats.unverified_relevant or not targets:
            return
        pending_order = {fingerprint: index for index, fingerprint in enumerate(dedupe.pending)}
        pending_targets = [
            target for target in targets if target.fingerprint in dedupe.pending_set
        ]
        if dedupe.pending_set:
            if not pending_targets:
                return
            ordered = sorted(
                pending_targets,
                key=lambda target: pending_order[target.fingerprint],
            )
        else:
            ordered = [
                target
                for target in targets
                if target.fingerprint not in dedupe.previously_seen
            ]
            if not ordered:
                return
        selected = await first_actionable(ordered)
        if selected is None:
            return
        if selected.fingerprint not in dedupe.pending_set:
            dedupe.pending.append(selected.fingerprint)
            dedupe.pending_set.add(selected.fingerprint)

    while True:
        remaining = max_total_posts - len(dedupe.seen_this_run)
        if remaining <= 0:
            exhaustion_reason = "MAX_TOTAL_POSTS"
            break
        viewport_number += 1
        viewport_relevant = 0
        viewport_irrelevant = 0
        viewport_unknown = 0
        viewport_classified = 0
        viewport_unverified_relevant = 0
        viewport_observation_unknown = 0
        viewport_selected = None
        viewport_relevant_targets = []
        viewport_fallback_targets = []
        current_unverified_rows: list[tuple[str, float]] = []
        matched_overlap_rows: set[int] = set()
        row_target_progress_ids: set[str] = set()
        emitted_verified_fingerprints: set[str] = set()
        completed_viewport_fingerprints: set[str] = set()

        async def observe_row(
            row: _HomeRowObservation,
            *,
            viewport_number: int = viewport_number,
            viewport_relevant_targets: list[PostTarget] = viewport_relevant_targets,
            viewport_fallback_targets: list[PostTarget] = viewport_fallback_targets,
            current_unverified_rows: list[tuple[str, float]] = current_unverified_rows,
            previous_unverified_rows: list[tuple[str, float]] = previous_unverified_rows,
            matched_overlap_rows: set[int] = matched_overlap_rows,
            row_target_progress_ids: set[str] = row_target_progress_ids,
            emitted_verified_fingerprints: set[str] = emitted_verified_fingerprints,
            completed_viewport_fingerprints: set[str] = completed_viewport_fingerprints,
            pending_scroll_baseline: dict[str, object] | None = pending_scroll_baseline,
        ) -> None:
            nonlocal viewport_classified, viewport_irrelevant
            nonlocal viewport_observation_unknown, viewport_relevant
            nonlocal viewport_unverified_relevant, viewport_unknown

            diagnostic = row.diagnostic
            target = row.target
            if diagnostic.state == "VERIFIED" and target is not None:
                if (
                    reader.last_scan_retry_count
                    and target.fingerprint in emitted_verified_fingerprints
                ):
                    return
                emitted_verified_fingerprints.add(target.fingerprint)
            evidence = diagnostic.read_evidence
            previous_evidence = None
            if pending_scroll_baseline is not None and diagnostic.observation_id:
                previous_rows = pending_scroll_baseline.get("rows", {})
                if isinstance(previous_rows, dict):
                    candidate_evidence = previous_rows.get(diagnostic.observation_id)
                    if isinstance(candidate_evidence, BodyReadEvidence):
                        previous_evidence = candidate_evidence
            if evidence is not None and previous_evidence is not None:
                same_scroller = (
                    evidence.scroll_surface_key == previous_evidence.scroll_surface_key
                    and evidence.scroller_scroll_top is not None
                    and previous_evidence.scroller_scroll_top is not None
                )
                if same_scroller:
                    scroll_top_delta = (
                        evidence.scroller_scroll_top - previous_evidence.scroller_scroll_top
                    )
                    diagnostic.same_scroller_scroll_top_delta = scroll_top_delta
                    scroller_moved = abs(scroll_top_delta) > 1
                    wheel_surface_matches = (
                        reader.last_scroll_surface_key is not None
                        and evidence.scroll_surface_key == reader.last_scroll_surface_key
                    )
                    diagnostic.wheel_surface_matches = wheel_surface_matches
                    diagnostic.row_top_before_px = previous_evidence.row_top_px
                    diagnostic.row_bottom_before_px = previous_evidence.row_bottom_px
                    diagnostic.clip_top_before_px = previous_evidence.clip_top_px
                    diagnostic.clip_bottom_before_px = previous_evidence.clip_bottom_px
                    diagnostic.text_top_before_px = previous_evidence.text_rect_top_px
                    diagnostic.text_bottom_before_px = previous_evidence.text_rect_bottom_px
                    flags: list[str] = []
                    if not wheel_surface_matches:
                        flags.append("SCROLLER_MISMATCH")
                    row_delta: float | None = None
                    if (
                        previous_evidence.row_top_px is not None
                        and previous_evidence.clip_top_px is not None
                        and evidence.row_top_px is not None
                        and evidence.clip_top_px is not None
                    ):
                        row_delta = (evidence.row_top_px - evidence.clip_top_px) - (
                            previous_evidence.row_top_px - previous_evidence.clip_top_px
                        )
                        diagnostic.row_movement_px = row_delta
                    distance_decreased = bool(
                        previous_evidence.closest_text_side == "BELOW"
                        and evidence.closest_text_side == "BELOW"
                        and previous_evidence.closest_text_distance_px is not None
                        and evidence.closest_text_distance_px is not None
                        and evidence.closest_text_distance_px
                        < previous_evidence.closest_text_distance_px - 1
                    )
                    if (
                        previous_evidence.closest_text_distance_px is not None
                        and evidence.closest_text_distance_px is not None
                    ):
                        diagnostic.text_distance_delta_px = (
                            evidence.closest_text_distance_px
                            - previous_evidence.closest_text_distance_px
                        )
                    row_moved_toward_view = bool(
                        wheel_surface_matches
                        and scroller_moved
                        and row_delta is not None
                        and row_delta < -1
                        and distance_decreased
                    )
                    if row_moved_toward_view:
                        flags.append("ROW_MOVED_TOWARD_VIEW")
                    same_row_position = row_delta is not None and abs(row_delta) <= 1
                    same_text_position = (
                        previous_evidence.text_rect_top_px is not None
                        and evidence.text_rect_top_px is not None
                        and abs(previous_evidence.text_rect_top_px - evidence.text_rect_top_px) <= 1
                    )
                    if scroller_moved and same_row_position and same_text_position:
                        flags.append("SCROLLTOP_MOVED_ROW_STATIC")
                    scroll_geometry = reader.last_scroll_geometry or {}
                    height_delta = scroll_geometry.get("scroll_height_delta")
                    expected_row_delta = -scroll_top_delta
                    if (
                        row_delta is not None
                        and abs(row_delta - expected_row_delta)
                        > max(24, abs(scroll_top_delta) * 0.25)
                    ) or (isinstance(height_delta, (int, float)) and abs(height_delta) > 1):
                        flags.append("LAYOUT_SHIFT")
                    if flags:
                        diagnostic.scroll_reason_flags = tuple(flags)
                    if row_moved_toward_view:
                        row_target_progress_ids.add(diagnostic.observation_id)
                elif reader.last_scroll_surface_key is not None:
                    diagnostic.wheel_surface_matches = False
                    diagnostic.scroll_reason_flags = ("SCROLLER_MISMATCH",)

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
            coverage_outcome = coverage.observe_row(
                RowObservationFacts(
                    fingerprint=row_fingerprint,
                    observation_id=diagnostic.observation_id,
                    gap_id=f"row:{viewport_number}:{diagnostic.row_index}",
                    state=diagnostic.state,
                    has_body=has_body,
                    body_complete=bool(
                        diagnostic.state == "VERIFIED"
                        and target is not None
                        and target.body_complete
                    ),
                )
            )
            if row_fingerprint is not None and evidence is not None:
                coverage.observe_body_range(
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
            stats.provenance_verified += int(coverage_outcome.identity_added)
            reader.last_scan_counts.new_verified_rows += int(coverage_outcome.body_completed)
            if coverage_outcome.body_completed and row_fingerprint is not None:
                completed_viewport_fingerprints.add(row_fingerprint)
            if detail:
                observation_emitted = bool(
                    (row.target and (row.target.body or row.target.visible_segments))
                    or (row.display and (row.display.body or row.display.visible_segments))
                )
                metrics = evidence

                def metric_count(value: int | None) -> str:
                    return "UNKNOWN" if value is None else str(value)

                print(
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
            if target is not None:
                if diagnostic.state == "PARTIAL_BODY":
                    new_segments = tuple(
                        segment
                        for segment in target.visible_segments
                        if segment.segment_id not in dedupe.streamed_segment_ids
                    )
                    stats.visible_text_segments += len(new_segments)
                    dedupe.streamed_segment_ids.update(
                        segment.segment_id for segment in new_segments
                    )
                    if detail or show_post_text:
                        _print_visible_segments(
                            f"viewport={viewport_number} row={diagnostic.row_index}",
                            new_segments,
                        )
                        if new_segments:
                            dedupe.streamed_home_fingerprints.add(target.fingerprint)
                    return
                if diagnostic.state != "VERIFIED" or not target.body or not target.body_complete:
                    return
                new_segments = tuple(
                    segment
                    for segment in target.visible_segments
                    if segment.segment_id not in dedupe.streamed_segment_ids
                )
                stats.visible_text_segments += len(new_segments)
                dedupe.streamed_segment_ids.update(segment.segment_id for segment in new_segments)
                if (
                    new_segments
                    and target.fingerprint in dedupe.streamed_home_fingerprints
                    and (detail or show_post_text)
                ):
                    _print_visible_segments(
                        f"viewport={viewport_number} row={diagnostic.row_index}",
                        new_segments,
                    )
                if target.fingerprint in dedupe.seen_this_run:
                    if (
                        target.fingerprint in dedupe.previously_seen
                        and target.fingerprint in dedupe.checkpoint_overlap
                    ):
                        stats.duplicates += 1
                    elif target.fingerprint in dedupe.previously_seen:
                        dedupe.checkpoint_overlap.add(target.fingerprint)
                    if detail and target.fingerprint not in dedupe.streamed_home_fingerprints:
                        print(
                            f"[POST post={dedupe.post_numbers[target.fingerprint]}] "
                            "status=VERIFIED decision=OVERLAP_DUPLICATE HOME_BODY=OMITTED",
                            flush=True,
                        )
                    if (
                        dedupe.decisions_by_fingerprint.get(target.fingerprint) == "RELEVANT"
                        and target.fingerprint in dedupe.pending_set
                        and target.currently_visible
                    ):
                        viewport_relevant_targets.append(target)
                    elif (
                        target.body.strip()
                        and dedupe.decisions_by_fingerprint.get(target.fingerprint) != "RELEVANT"
                        and target.currently_visible
                    ):
                        viewport_fallback_targets.append(target)
                    return
                dedupe.seen_this_run.add(target.fingerprint)
                dedupe.next_post_number += 1
                dedupe.post_numbers[target.fingerprint] = dedupe.next_post_number
                stats.body_extracted += int(bool(target.body and target.body.strip()))
                viewport_classified += 1
                is_new = target.fingerprint not in dedupe.previously_seen
                if is_new:
                    dedupe.new_fingerprints.add(target.fingerprint)
                decision = classify_body(target.body, normalized_terms)
                dedupe.decisions_by_fingerprint[target.fingerprint] = decision
                stats.classified += 1
                if detail:
                    if target.fingerprint not in dedupe.streamed_home_fingerprints:
                        if _print_detail_body(
                            "HOME",
                            f"post={dedupe.post_numbers[target.fingerprint]}",
                            "VERIFIED",
                            decision,
                            "TRUNCATED" if target.body_truncated else "COMPLETE",
                            target.body,
                        ):
                            stats.previewed_bodies += 1
                        dedupe.streamed_home_fingerprints.add(target.fingerprint)
                    else:
                        print(
                            f"[POST post={dedupe.post_numbers[target.fingerprint]}] "
                            f"status=VERIFIED decision={decision} "
                            f"extent={'TRUNCATED' if target.body_truncated else 'COMPLETE'} "
                            f"chars={len(target.body)} text=STREAMED",
                            flush=True,
                        )
                elif (
                    show_post_text
                    and target.fingerprint not in dedupe.streamed_home_fingerprints
                    and _print_post_text(viewport_number, decision, target.body)
                ):
                    stats.previewed_bodies += 1
                if decision == "RELEVANT":
                    stats.relevant += 1
                    viewport_relevant += 1
                    stats.found_relevant = True
                    if is_new and target.fingerprint not in dedupe.pending_set:
                        dedupe.pending.append(target.fingerprint)
                        dedupe.pending_set.add(target.fingerprint)
                    if target.fingerprint in dedupe.pending_set and target.currently_visible:
                        viewport_relevant_targets.append(target)
                elif decision == "IRRELEVANT":
                    stats.irrelevant += 1
                    viewport_irrelevant += 1
                    if target.currently_visible:
                        viewport_fallback_targets.append(target)
                else:
                    stats.unknown += 1
                    viewport_unknown += 1
                    if target.currently_visible:
                        viewport_fallback_targets.append(target)
                return

            observation = row.display
            if observation is None:
                return
            if observation.body is None or not observation.body.strip():
                stats.observation_unknown += 1
                viewport_observation_unknown += 1
                if detail:
                    print(
                        f"[POST viewport={viewport_number} row={diagnostic.row_index}] "
                        "status=OBSERVATION_UNCERTAIN decision=UNKNOWN body=UNAVAILABLE",
                        flush=True,
                    )
                elif show_observed_text:
                    _print_observed_text(viewport_number, "UNKNOWN_UNVERIFIED", observation)
                return
            body_digest = sha256(dedupe.display_salt + observation.body.encode("utf-8")).hexdigest()
            overlap_duplicate = False
            for previous_index, (previous_digest, previous_y) in enumerate(
                previous_unverified_rows
            ):
                if matched_overlap_rows and previous_index in matched_overlap_rows:
                    continue
                if previous_digest != body_digest:
                    continue
                if abs((previous_y - reader.last_scroll_delta) - observation.viewport_y) <= 48:
                    matched_overlap_rows.add(previous_index)
                    overlap_duplicate = True
                    break
            if not overlap_duplicate:
                overlap_duplicate = any(
                    current_digest == body_digest and abs(current_y - observation.viewport_y) <= 2
                    for current_digest, current_y in current_unverified_rows
                )
            if overlap_duplicate:
                if detail:
                    print(
                        f"[POST viewport={viewport_number} row={diagnostic.row_index}] "
                        "status=OBSERVED_UNVERIFIED decision=OVERLAP_DUPLICATE "
                        "HOME_BODY=OMITTED",
                        flush=True,
                    )
                return
            current_unverified_rows.append((body_digest, observation.viewport_y))
            stats.body_extracted += 1
            decision = classify_body(observation.body, normalized_terms)
            stats.classified += 1
            viewport_classified += 1
            if decision == "RELEVANT":
                decision = "RELEVANT_UNVERIFIED"
                stats.unverified_relevant += 1
                if diagnostic.state != "OBSERVED_UNVERIFIED":
                    stats.unsettled_unverified_relevance += 1
                viewport_unverified_relevant += 1
            elif decision == "IRRELEVANT":
                decision = "IRRELEVANT_UNVERIFIED"
            else:
                decision = "UNKNOWN_UNVERIFIED"
                stats.observation_unknown += 1
                viewport_observation_unknown += 1
            if detail:
                if _print_detail_body(
                    "HOME",
                    f"viewport={viewport_number} row={diagnostic.row_index}",
                    "OBSERVED_UNVERIFIED",
                    decision,
                    "OBSERVED",
                    observation.body,
                ):
                    stats.previewed_bodies += 1
            elif show_observed_text:
                _print_observed_text(viewport_number, decision, observation)

        state.transition(WorkflowPhase.WAITING_FOR_CONTENT)
        state.transition(WorkflowPhase.VERIFYING)
        reader.set_scan_viewport_index(viewport_number)
        reader.set_failure_context(state.phase.value, "SCAN_ROW")
        try:
            await reader.scan_viewport(
                min(max_posts, remaining),
                scanned_fingerprints=dedupe.seen_this_run,
                on_row=observe_row,
            )
            reader.last_scan_counts.new_verified_rows = len(completed_viewport_fingerprints)
        except (UiReadError, PlaywrightError) as raw_error:
            error = (
                raw_error
                if isinstance(raw_error, UiReadError)
                else _reader_playwright_error(
                    page,
                    raw_error,
                    phase=state.phase.value,
                    operation="SCAN_ROW",
                )
            )
            state.stop(error.code)
            counts = reader.last_scan_counts
            counts.coverage_complete = False
            reader.refresh_viewport_body_coverage_counts()
            rejection_reasons = (
                ",".join(
                    f"{reason}:{count}"
                    for reason, count in sorted(counts.rejection_reasons.items())
                )
                or "none"
            )
            print(
                f"SCAN_STOP viewport={viewport_number} visible_links={counts.visible_links} "
                f"inspected={counts.inspected} url_parsed={counts.url_parsed} "
                f"author_unverified={counts.author_unverified} "
                f"wrapper_ambiguous={counts.wrapper_ambiguous} "
                f"card_not_ready={counts.card_not_ready} "
                f"verified={counts.verified} body_read={counts.body_read} "
                f"body_unknown={counts.body_unknown} classified={viewport_classified} "
                f"relevant={viewport_relevant} skipped={counts.skipped} "
                f"rejection_reasons={rejection_reasons} "
                f"unrepresented_root_count={counts.unrepresented_root_count} "
                f"unresolved_visited_rows={counts.unresolved_visited_rows} "
                f"lookahead_rows={counts.lookahead_rows} "
                f"{_format_structure_diagnostics(counts)}"
            )
            if detail:
                print(
                    f"[TIMING] viewport={viewport_number} "
                    f"discover_ms={reader.last_scan_timings.discover_ms} "
                    f"read_row_ms={reader.last_scan_timings.read_row_ms} "
                    f"stabilize_ms={reader.last_scan_timings.stabilize_ms} "
                    f"wait_reason={reader.last_scan_timings.wait_reason}",
                    flush=True,
                )
                _print_url_structure_diagnostics(counts)
                print(
                    f"[VIEWPORT {viewport_number}] status=PARTIAL code={error.code} "
                    f"coverage={str(counts.coverage_complete).lower()} "
                    f"unrepresented_root_count={counts.unrepresented_root_count} "
                    f"unresolved_visited_rows={counts.unresolved_visited_rows} "
                    f"lookahead_rows={counts.lookahead_rows}",
                    flush=True,
                )
            if error.code.endswith("NOT_READY") and (
                dedupe.seen_this_run or stats.body_extracted or stats.observed_rows
            ):
                counts.coverage_complete = False
                stats.observed_rows += counts.observed_rows
                stats.skipped_rows += counts.skipped_rows
                if detail:
                    _detail_stop("VIEWPORT_OBSERVATION_INCOMPLETE")
                return NurtureResult(
                    status="VIEWPORT_OBSERVATION_INCOMPLETE",
                    home_sampled=len(dedupe.seen_this_run),
                    home_new=len(dedupe.new_fingerprints),
                    home_relevant=stats.relevant,
                    home_irrelevant=stats.irrelevant,
                    home_unknown=stats.unknown,
                    duplicates=stats.duplicates,
                    detail_reads=0,
                    scroll_attempts=reader.scroll_attempts,
                    scrolls=reader.scrolls,
                    checkpoint_runs=checkpoint.completed_runs,
                    home_relevant_ratio=None
                    if not dedupe.seen_this_run
                    else stats.relevant / len(dedupe.seen_this_run),
                    skipped_unverified=reader.skipped_unverified,
                    observed_rows=stats.observed_rows,
                    body_extracted=stats.body_extracted,
                    provenance_verified=stats.provenance_verified,
                    classified=stats.classified,
                    observation_unknown=stats.observation_unknown,
                    unverified_relevant=stats.unverified_relevant,
                    skipped_rows=stats.skipped_rows,
                    viewport_coverage_complete=False,
                    **reliability_metrics(),
                )
            if detail:
                print(
                    f"[VIEWPORT] status=FAILED code={error.code} "
                    f"coverage={str(counts.coverage_complete).lower()} "
                    f"unrepresented_root_count={counts.unrepresented_root_count} "
                    f"unresolved_visited_rows={counts.unresolved_visited_rows} "
                    f"lookahead_rows={counts.lookahead_rows}"
                )
            raise error from None
        state.transition(WorkflowPhase.OBSERVING)
        scan_counts = reader.last_scan_counts
        if detail:
            print(
                f"[VIEWPORT {viewport_number}] observed={scan_counts.observed_rows} "
                f"verified={scan_counts.verified} body_read={scan_counts.body_read} "
                f"body_unknown={scan_counts.body_unknown} skipped={scan_counts.skipped} "
                "coverage="
                f"{str(scan_counts.coverage_complete).lower()} "
                f"unrepresented_root_count={scan_counts.unrepresented_root_count} "
                f"unresolved_visited_rows={scan_counts.unresolved_visited_rows} "
                f"lookahead_rows={scan_counts.lookahead_rows}",
                flush=True,
            )
            print(
                f"[TIMING] viewport={viewport_number} "
                f"discover_ms={reader.last_scan_timings.discover_ms} "
                f"read_row_ms={reader.last_scan_timings.read_row_ms} "
                f"stabilize_ms={reader.last_scan_timings.stabilize_ms} "
                f"wait_reason={reader.last_scan_timings.wait_reason}",
                flush=True,
            )
            _print_url_structure_diagnostics(scan_counts)
        # Only use this immediate-viewport geometry/body comparison to suppress
        # duplicate overlap work; unverified rows receive no stable identity.
        previous_unverified_rows = current_unverified_rows
        stats.observed_rows += scan_counts.observed_rows
        stats.skipped_rows += scan_counts.skipped_rows
        unresolved_rows = [
            row
            for row in reader.last_candidate_diagnostics
            if row.state not in {"NON_POST", "VERIFIED"}
        ]
        if scan_counts.roots_truncated or (
            not scan_counts.coverage_complete and not unresolved_rows
        ):
            coverage.unrepresented_gaps.add(f"viewport:{viewport_number}")
        stats.viewport_coverage_complete = not coverage.has_unresolved_gaps

        if pending_scroll_baseline is not None:
            baseline_segments = int(
                pending_scroll_baseline.get("visible_text_segments", stats.visible_text_segments)
            )
            baseline_verified = int(
                pending_scroll_baseline.get("unique_verified_posts", len(coverage.fully_read))
            )
            baseline_bodies = int(
                pending_scroll_baseline.get(
                    "observed_bodies",
                    len(coverage.observed_body_identities),
                )
            )
            new_segments = max(0, stats.visible_text_segments - baseline_segments)
            new_observed_bodies = max(
                0,
                len(coverage.observed_body_identities) - baseline_bodies,
            )
            new_verified = max(
                0,
                len(coverage.fully_read) - baseline_verified,
            )
            approach_deltas = [
                -diagnostic.text_distance_delta_px
                for diagnostic in reader.last_candidate_diagnostics
                if "ROW_MOVED_TOWARD_VIEW" in diagnostic.scroll_reason_flags
                and diagnostic.text_distance_delta_px is not None
            ]
            text_approach_delta = max(approach_deltas, default=None)
            per_row_approach = bool(row_target_progress_ids)
            made_progress = bool(
                new_segments or new_observed_bodies or new_verified or per_row_approach
            )
            stats.consecutive_moved_no_content = (
                0 if made_progress else stats.consecutive_moved_no_content + 1
            )
            for diagnostic in reader.last_candidate_diagnostics:
                metrics = diagnostic.read_evidence
                if metrics is None:
                    continue

                def px(value: float | None) -> str:
                    return "UNKNOWN" if value is None else f"{value:.1f}"

                def count(value: int | None) -> str:
                    return "UNKNOWN" if value is None else str(value)

                print(
                    f"[SCROLL_ROW_PROGRESS] attempt={reader.scrolls} "
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
            geometry = reader.last_scroll_geometry or {}
            if geometry:
                print(
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
            print(
                f"[SCROLL_PROGRESS] attempt={reader.scrolls} "
                f"state={'ADVANCED_WITH_CONTENT' if made_progress else 'MOVED_NO_NEW_CONTENT'} "
                f"new_segments={new_segments} "
                f"new_observed_bodies={new_observed_bodies} "
                f"new_verified_posts={new_verified} "
                f"tracked_rows_approaching={len(row_target_progress_ids)} "
                f"text_approach_delta_px={'UNKNOWN' if text_approach_delta is None else f'{text_approach_delta:.1f}'} "
                f"deferred_above_rects={scan_counts.deferred_above} "
                f"deferred_below_rects={scan_counts.deferred_below} "
                f"viewport_row_appearances={stats.observed_rows} "
                f"unique_provenance_identities={len(coverage.provenance_verified)} "
                f"unique_verified_posts={len(coverage.fully_read)} "
                f"unresolved_gaps={coverage.unresolved_gap_count} "
                f"band={reader.last_scroll_band} "
                f"end_proximity={'NEAR' if reader.last_scroll_near_end else 'FAR'} "
                f"end_distance_px={'UNKNOWN' if reader.last_scroll_end_distance is None else f'{reader.last_scroll_end_distance:.1f}'} "
                f"no_new_content_streak={stats.consecutive_moved_no_content}",
                flush=True,
            )
            pending_scroll_baseline = None
            if stats.consecutive_moved_no_content >= 3:
                if (
                    reader.last_scroll_end_distance is not None
                    and reader.last_scroll_end_distance <= 1
                    and stats.viewport_coverage_complete
                ):
                    exhaustion_reason = "FEED_END_REACHED"
                else:
                    exhaustion_reason = "READ_PROGRESS_STALLED"
                    coverage.unrepresented_gaps.add(f"stalled:{reader.scrolls}")
                    stats.viewport_coverage_complete = False
                print(
                    f"[SCROLL_PROGRESS] status=STOPPED reason={exhaustion_reason} "
                    "coverage=INCOMPLETE "
                    f"unresolved_gaps={coverage.unresolved_gap_count}",
                    flush=True,
                )
                break
        observation_status = (
            "RELEVANT_UNVERIFIED"
            if viewport_unverified_relevant
            else "OBSERVED_UNVERIFIED"
            if scan_counts.body_extracted > scan_counts.body_read
            else "OBSERVATION_UNCERTAIN"
            if scan_counts.uncertain_rows
            else "NONE"
        )
        print(
            f"SCANNING viewport={viewport_number} verified={scan_counts.verified} "
            f"relevant={viewport_relevant} skipped={scan_counts.skipped} "
            f"visible_links={scan_counts.visible_links} "
            f"inspected={scan_counts.inspected} "
            f"body_read={scan_counts.body_read} "
            f"body_unknown={scan_counts.body_unknown} "
            f"classified={viewport_classified} "
            f"observed_rows={scan_counts.observed_rows} "
            f"body_extracted={scan_counts.body_extracted} "
            f"provenance_verified_current={scan_counts.identity_verified} "
            f"body_complete_current={scan_counts.verified} "
            f"unique_provenance_identities={len(coverage.provenance_verified)} "
            f"unique_verified_posts={len(coverage.fully_read)} "
            f"viewport_row_appearances={stats.observed_rows} "
            f"unverified_relevant={viewport_unverified_relevant} "
            f"observation_status={observation_status} "
            f"skipped_rows={scan_counts.skipped_rows} "
            f"viewport_coverage_complete={str(scan_counts.coverage_complete).lower()} "
            f"irrelevant={viewport_irrelevant} unknown={viewport_unknown} "
            f"observation_unknown={viewport_observation_unknown} "
            f"url_parsed={scan_counts.url_parsed} "
            f"author_unverified={scan_counts.author_unverified} "
            f"wrapper_ambiguous={scan_counts.wrapper_ambiguous} "
            f"card_not_ready={scan_counts.card_not_ready} "
            "rejection_reasons="
            + (
                ",".join(
                    f"{reason}:{count}"
                    for reason, count in sorted(scan_counts.rejection_reasons.items())
                )
                or "none"
            )
            + " "
            + _format_structure_diagnostics(scan_counts)
        )
        if not scan_counts.coverage_complete and not reader.last_viewport_deferred_only:
            print(
                f"VIEWPORT_COVERAGE_INCOMPLETE viewport={viewport_number} "
                f"observed_rows={scan_counts.observed_rows} cap={min(max_posts, remaining)}"
            )
            if detail:
                print(
                    f"[VIEWPORT] verified_body_extracted={scan_counts.body_read} "
                    f"observed_body_extracted={scan_counts.body_extracted - scan_counts.body_read} "
                    f"scan={'PARTIAL' if reader.last_viewport_can_wheel else 'NOT_COMMITTED'} "
                    f"wheel={'ALLOWED' if reader.last_viewport_can_wheel else 'BLOCKED'}"
                )
        if viewport_relevant_targets:
            viewport_relevant_targets.sort(
                key=lambda target: target.fingerprint in dedupe.previously_seen
            )
            if max_scrolls > 0 and reader.scroll_attempts == 0:
                if await advance_feed(
                    scan_after_scroll=len(dedupe.seen_this_run) >= max_total_posts
                ):
                    if len(dedupe.seen_this_run) >= max_total_posts:
                        if (
                            reader.last_viewport_coverage_complete
                            and reader.last_viewport_observed_rows <= max_posts
                        ):
                            still_visible = [
                                target
                                for target in reader.last_scroll_targets
                                if target.fingerprint in dedupe.pending_set
                                and dedupe.decisions_by_fingerprint.get(target.fingerprint)
                                == "RELEVANT"
                                and classify_body(target.body, normalized_terms) == "RELEVANT"
                            ]
                            still_visible.sort(
                                key=lambda target: target.fingerprint in dedupe.previously_seen
                            )
                            selected = await first_actionable(still_visible)
                            if selected is not None:
                                break
                        exhaustion_reason = "MAX_TOTAL_POSTS"
                        break
                    continue
                if reader.last_scroll_stop_reason == "FEED_END_REACHED":
                    exhaustion_reason = "FEED_END_REACHED"
                    # The current viewport was fully verified and remains in
                    # view; use its eligible post when there is no next page.
            viewport_selected = await first_actionable(viewport_relevant_targets)
        if viewport_selected is not None:
            selected = viewport_selected
            break
        if len(dedupe.seen_this_run) >= max_total_posts:
            exhaustion_reason = "MAX_TOTAL_POSTS"
            if max_scrolls == 0 or reader.scroll_attempts > 0:
                await select_verified_fallback(viewport_fallback_targets)
            break
        if reader.scroll_attempts >= max_scrolls:
            exhaustion_reason = "MAX_SCROLLS"
            await select_verified_fallback(viewport_fallback_targets)
            break
        if not await advance_feed():
            exhaustion_reason = reader.last_scroll_stop_reason or "FEED_NOT_ADVANCED"
            if exhaustion_reason in {"FEED_END_REACHED", "FEED_NOT_SCROLLABLE"}:
                await select_verified_fallback(viewport_fallback_targets)
            break

    detail_reads = 0
    detail_retry_pending = False
    if selected is None and not stats.found_relevant:
        if exhaustion_reason in {"FEED_END_REACHED", "FEED_NOT_SCROLLABLE"}:
            print(f"SEARCH_STOP reason={exhaustion_reason} viewports={viewport_number}")
        else:
            print(f"SEARCH_BUDGET_EXHAUSTED reason={exhaustion_reason} viewports={viewport_number}")
    if selected is not None:
        state.transition(WorkflowPhase.SELECTING)
        selected_number = dedupe.post_numbers[selected.fingerprint]
        selected_decision = dedupe.decisions_by_fingerprint.get(
            selected.fingerprint,
            "UNKNOWN",
        )
        if detail:
            if selected_decision == "RELEVANT":
                print(
                    f"[SELECT] post={selected_number} decision=RELEVANT "
                    f"reason={'RELEVANT_PENDING' if selected.fingerprint in dedupe.previously_seen else 'RELEVANT_NEW'}"
                )
            else:
                print(
                    f"[SELECT] post={selected_number} "
                    "decision=VERIFIED_FALLBACK reason=NO_RELEVANT_MATCH"
                )
        state.transition(WorkflowPhase.DETAIL)
        reader.set_failure_context(state.phase.value, "DETAIL")
        try:
            detail_target = await reader.read_detail(selected)
        except (UiReadError, PlaywrightError) as raw_error:
            error = (
                raw_error
                if isinstance(raw_error, UiReadError)
                else _reader_playwright_error(
                    page,
                    raw_error,
                    phase=state.phase.value,
                    operation="DETAIL",
                )
            )
            if error.code not in _DETAIL_READ_FAILURES or not getattr(
                reader,
                "detail_route_verified",
                False,
            ):
                state.stop(error.code)
                if detail:
                    print(
                        f"[DETAIL] post={selected_number} "
                        f"status=FAILED code={error.code}"
                    )
                    _detail_stop(error.code)
                raise error from None
            detail_retry_pending = True
            if detail:
                print(
                    f"[DETAIL] post={selected_number} status=UNAVAILABLE "
                    f"code={error.code} retry=PENDING"
                )
        if not detail_retry_pending and detail:
            home_body = " ".join((selected.body or "").split())
            expanded_body = " ".join((detail_target.body or "").split())
            extent = (
                "EXPANDED"
                if len(expanded_body) > len(home_body)
                else "TRUNCATED"
                if detail_target.body_truncated
                else "FULL"
            )
            print(
                f"[DETAIL] post={selected_number} status=VERIFIED "
                f"provenance=VERIFIED extent={extent}"
            )
            _print_detail_body(
                "DETAIL",
                f"post={selected_number}",
                "VERIFIED",
                selected_decision,
                extent,
                detail_target.body,
            )
        if not detail_retry_pending:
            print("DETAIL_VERIFIED status=PROVENANCE_VERIFIED")
        if detail:
            print(f"[BACK] post={selected_number} status=REQUESTED")
        state.transition(WorkflowPhase.BACK)
        reader.set_failure_context(state.phase.value, "BACK")
        try:
            await reader.return_to_home()
        except (UiReadError, PlaywrightError) as raw_error:
            error = (
                raw_error
                if isinstance(raw_error, UiReadError)
                else _reader_playwright_error(
                    page,
                    raw_error,
                    phase=state.phase.value,
                    operation="BACK",
                )
            )
            state.stop(error.code)
            if detail:
                anchor_status = (
                    "CHANGED" if error.code == "RETURN_CONTEXT_CHANGED" else "UNVERIFIED"
                )
                print(
                    f"[BACK] post={selected_number} status=FAILED "
                    f"anchor={anchor_status} code={error.code}"
                )
            raise error from None
        if detail:
            print(
                f"[BACK] post={selected_number} status=RESTORED "
                "anchor=IDENTITY_VERIFIED"
            )
        if detail_retry_pending:
            status = "DETAIL_RETRY_PENDING"
        else:
            detail_reads = 1
            dedupe.pending.remove(selected.fingerprint)
            dedupe.pending_set.remove(selected.fingerprint)
            status = "COMPLETE"
    elif (
        selected is None
        and stats.unverified_relevant
        and not stats.found_relevant
        and not coverage.has_skips
    ):
        print("SEARCH_INCOMPLETE reason=UNVERIFIED_RELEVANCE")
        if detail:
            _detail_stop("UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION")
        return NurtureResult(
            status="UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION",
            stop_reason="UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION",
            home_sampled=len(dedupe.seen_this_run),
            home_new=len(dedupe.new_fingerprints),
            home_relevant=stats.relevant,
            home_irrelevant=stats.irrelevant,
            home_unknown=stats.unknown,
            duplicates=stats.duplicates,
            detail_reads=0,
            scroll_attempts=reader.scroll_attempts,
            scrolls=reader.scrolls,
            checkpoint_runs=checkpoint.completed_runs,
            home_relevant_ratio=(
                None if not dedupe.seen_this_run else stats.relevant / len(dedupe.seen_this_run)
            ),
            skipped_unverified=reader.skipped_unverified,
            observed_rows=stats.observed_rows,
            body_extracted=stats.body_extracted,
            provenance_verified=stats.provenance_verified,
            classified=stats.classified,
            observation_unknown=stats.observation_unknown,
            unverified_relevant=stats.unverified_relevant,
            skipped_rows=stats.skipped_rows,
            viewport_coverage_complete=stats.viewport_coverage_complete,
            **reliability_metrics(),
        )
    elif selected is None and actionability_rejected_codes:
        print("SEARCH_INCOMPLETE reason=NO_ACTIONABLE_TARGET")
        if detail:
            _detail_stop("NO_ACTIONABLE_TARGET")
        state.stop("NO_ACTIONABLE_TARGET")
        return NurtureResult(
            status="NO_ACTIONABLE_TARGET",
            stop_reason="NO_ACTIONABLE_TARGET",
            home_sampled=len(dedupe.seen_this_run),
            home_new=len(dedupe.new_fingerprints),
            home_relevant=stats.relevant,
            home_irrelevant=stats.irrelevant,
            home_unknown=stats.unknown,
            duplicates=stats.duplicates,
            detail_reads=0,
            scroll_attempts=reader.scroll_attempts,
            scrolls=reader.scrolls,
            checkpoint_runs=checkpoint.completed_runs,
            home_relevant_ratio=(
                None if not dedupe.seen_this_run else stats.relevant / len(dedupe.seen_this_run)
            ),
            skipped_unverified=reader.skipped_unverified,
            observed_rows=stats.observed_rows,
            body_extracted=stats.body_extracted,
            provenance_verified=stats.provenance_verified,
            classified=stats.classified,
            observation_unknown=stats.observation_unknown,
            unverified_relevant=stats.unverified_relevant,
            skipped_rows=stats.skipped_rows,
            viewport_coverage_complete=stats.viewport_coverage_complete,
            **reliability_metrics(),
        )
    elif len(dedupe.seen_this_run) == 0 and not coverage.has_skips:
        if show_post_text and stats.previewed_bodies == 0:
            print(
                "POST_TEXT status=NONE "
                f"verified_posts={len(dedupe.seen_this_run)} extracted={stats.previewed_bodies}"
            )
        if detail:
            _detail_stop(
                "UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION"
                if stats.unverified_relevant
                else "OBSERVED_UNVERIFIED_ONLY"
                if stats.body_extracted
                else "OBSERVATION_UNCERTAIN_ONLY"
                if stats.observed_rows
                else "NO_VERIFIED_POSTS_IN_BUDGET"
            )
        return NurtureResult(
            status=(
                "UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION"
                if stats.unverified_relevant
                else "OBSERVED_UNVERIFIED_ONLY"
                if stats.body_extracted
                else "OBSERVATION_UNCERTAIN_ONLY"
                if stats.observed_rows
                else "NO_VERIFIED_POSTS_IN_BUDGET"
            ),
            stop_reason=(
                "UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION"
                if stats.unverified_relevant
                else "OBSERVED_UNVERIFIED_ONLY"
                if stats.body_extracted
                else "OBSERVATION_UNCERTAIN_ONLY"
                if stats.observed_rows
                else "NO_VERIFIED_POSTS_IN_BUDGET"
            ),
            home_sampled=0,
            home_new=0,
            home_relevant=0,
            home_irrelevant=0,
            home_unknown=0,
            duplicates=stats.duplicates,
            detail_reads=0,
            scroll_attempts=reader.scroll_attempts,
            scrolls=reader.scrolls,
            checkpoint_runs=checkpoint.completed_runs,
            home_relevant_ratio=None,
            skipped_unverified=reader.skipped_unverified,
            observed_rows=stats.observed_rows,
            body_extracted=stats.body_extracted,
            provenance_verified=stats.provenance_verified,
            classified=stats.classified,
            observation_unknown=stats.observation_unknown,
            unverified_relevant=stats.unverified_relevant,
            skipped_rows=stats.skipped_rows,
            viewport_coverage_complete=stats.viewport_coverage_complete,
            **reliability_metrics(),
        )
    elif not detail_retry_pending and (
        dedupe.pending_set or (stats.found_relevant and exhaustion_reason == "FEED_END_REACHED")
    ):
        incomplete_status = (
            "FEED_END_REACHED"
            if exhaustion_reason == "FEED_END_REACHED"
            else "RELEVANT_POST_NOT_CURRENT"
        )
        print(f"SEARCH_INCOMPLETE reason={incomplete_status}")
        if detail:
            _detail_stop(incomplete_status)
        return NurtureResult(
            status=incomplete_status,
            stop_reason=incomplete_status,
            home_sampled=len(dedupe.seen_this_run),
            home_new=len(dedupe.new_fingerprints),
            home_relevant=stats.relevant,
            home_irrelevant=stats.irrelevant,
            home_unknown=stats.unknown,
            duplicates=stats.duplicates,
            detail_reads=0,
            scroll_attempts=reader.scroll_attempts,
            scrolls=reader.scrolls,
            checkpoint_runs=checkpoint.completed_runs,
            home_relevant_ratio=(
                None if not dedupe.seen_this_run else stats.relevant / len(dedupe.seen_this_run)
            ),
            skipped_unverified=reader.skipped_unverified,
            observed_rows=stats.observed_rows,
            body_extracted=stats.body_extracted,
            provenance_verified=stats.provenance_verified,
            classified=stats.classified,
            observation_unknown=stats.observation_unknown,
            unverified_relevant=stats.unverified_relevant,
            skipped_rows=stats.skipped_rows,
            viewport_coverage_complete=stats.viewport_coverage_complete,
            **reliability_metrics(),
        )
    elif exhaustion_reason == "FEED_END_REACHED":
        status = "FEED_END_REACHED"
    elif exhaustion_reason == "FEED_NOT_SCROLLABLE":
        status = "FEED_NOT_SCROLLABLE"
    elif stats.unverified_relevant and not dedupe.seen_this_run:
        status = "OBSERVED_UNVERIFIED_ONLY"
    elif stats.found_relevant or stats.relevant:
        status = "NO_NEW_RELEVANT_POST"
    elif stats.unknown:
        status = "VERIFIED_BUT_UNKNOWN_BODY"
    elif stats.irrelevant:
        status = "VERIFIED_IRRELEVANT"
    else:
        status = "NO_RELEVANT_POST_IN_BUDGET"

    if detail_retry_pending:
        status = "DETAIL_RETRY_PENDING"
    elif coverage.has_skips:
        status = "COMPLETE_WITH_SKIPS"

    if len(dedupe.pending) > _MAX_SEEN:
        raise BrowserDataError("STATE_INVALID")
    seen = list(dict.fromkeys((*checkpoint.seen_fingerprints, *dedupe.seen_this_run)))
    if len(seen) > _MAX_SEEN:
        retained_pending = [value for value in seen if value in dedupe.pending_set]
        recent = [value for value in seen if value not in dedupe.pending_set]
        available = _MAX_SEEN - len(retained_pending)
        seen = retained_pending + (recent[-available:] if available else [])
    updated = Checkpoint(
        account.account_id,
        fingerprint,
        completed_runs=checkpoint.completed_runs + 1,
        home_sampled=checkpoint.home_sampled + len(dedupe.seen_this_run),
        home_new=checkpoint.home_new + len(dedupe.new_fingerprints),
        home_relevant=checkpoint.home_relevant + stats.relevant,
        home_irrelevant=checkpoint.home_irrelevant + stats.irrelevant,
        home_unknown=checkpoint.home_unknown + stats.unknown,
        duplicates=checkpoint.duplicates + stats.duplicates,
        detail_reads=checkpoint.detail_reads + detail_reads,
        scroll_attempts=checkpoint.scroll_attempts + reader.scroll_attempts,
        scrolls=checkpoint.scrolls + reader.scrolls,
        pending_read_fingerprints=tuple(dedupe.pending),
        seen_fingerprints=tuple(seen),
    )
    if state.phase is not WorkflowPhase.CHECKPOINTING:
        state.transition(WorkflowPhase.CHECKPOINTING)
    try:
        checkpoint_store.save(updated)
    except BrowserDataError as error:
        state.stop(error.code)
        if detail:
            _detail_stop(error.code)
        raise
    if detail:
        print(f"[CHECKPOINT] status=SAVED completed_runs={updated.completed_runs}")
        print(f"[CYCLE] status={status}")
    if show_post_text and stats.previewed_bodies == 0:
        print(
            "POST_TEXT status=NONE "
            f"verified_posts={len(dedupe.seen_this_run)} extracted={stats.previewed_bodies}"
        )
    ratio = None if not dedupe.seen_this_run else stats.relevant / len(dedupe.seen_this_run)
    state.stop(status)
    return NurtureResult(
        status=status,
        stop_reason="DETAIL_RETRY_PENDING" if detail_retry_pending else exhaustion_reason,
        home_sampled=len(dedupe.seen_this_run),
        home_new=len(dedupe.new_fingerprints),
        home_relevant=stats.relevant,
        home_irrelevant=stats.irrelevant,
        home_unknown=stats.unknown,
        duplicates=stats.duplicates,
        detail_reads=detail_reads,
        scroll_attempts=reader.scroll_attempts,
        scrolls=reader.scrolls,
        checkpoint_runs=updated.completed_runs,
        home_relevant_ratio=ratio,
        skipped_unverified=reader.skipped_unverified,
        observed_rows=stats.observed_rows,
        body_extracted=stats.body_extracted,
        provenance_verified=stats.provenance_verified,
        classified=stats.classified,
        observation_unknown=stats.observation_unknown,
        unverified_relevant=stats.unverified_relevant,
        skipped_rows=stats.skipped_rows,
        viewport_coverage_complete=stats.viewport_coverage_complete,
        viewport_observations=stats.observed_rows,
        unique_verified_posts=len(coverage.fully_read),
        unique_deferred_posts=len(coverage.unique_deferred),
        unresolved_gaps=(coverage.unresolved_gap_count),
        unique_provenance_identities=len(coverage.provenance_verified),
        visible_text_segments=stats.visible_text_segments,
    )
