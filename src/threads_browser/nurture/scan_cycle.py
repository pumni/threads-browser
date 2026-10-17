"""Bounded viewport observation loop for one Home page cycle."""

from __future__ import annotations

from playwright.async_api import Error as PlaywrightError

from ..browser_ui.feed_rows import _FeedScanCounts
from ..console_output import emit_console_diagnostic
from ..errors import UiReadError
from ..feed.models import PostTarget
from ..feed.relevance import classify_body
from .cycle_context import CycleContext
from .diagnostics import (
    _detail_stop,
    _format_structure_diagnostics,
    _print_url_structure_diagnostics,
)
from .home_observation import HomeRowObserver
from .outcomes import _reader_playwright_error
from .results import NurtureResult, _result_from_observation
from .state import WorkflowPhase


async def scan_home_feed(context: CycleContext) -> NurtureResult | None:
    """Observe bounded viewports and select only a currently actionable target."""
    while True:
        remaining = context.max_total_posts - len(context.dedupe.home_observed_this_session)
        if remaining <= 0:
            context.exhaustion_reason = "MAX_TOTAL_POSTS"
            break
        context.viewport_number += 1
        observer = _new_row_observer(context)
        scan_limit = min(context.max_posts, remaining)
        partial_result = await _scan_viewport(
            context,
            observer,
            scan_limit=scan_limit,
        )
        if partial_result is not None:
            return partial_result
        if _record_viewport_observation(context, observer, scan_limit=scan_limit):
            break
        if await _select_or_advance(context, observer):
            break
    return None


def _new_row_observer(context: CycleContext) -> HomeRowObserver:
    return HomeRowObserver(
        reader=context.reader,
        coverage=context.coverage,
        dedupe=context.dedupe,
        stats=context.stats,
        viewport_number=context.viewport_number,
        normalized_terms=context.normalized_terms,
        console_output_policy=context.console_output_policy,
        previous_unverified_rows=context.previous_unverified_rows,
        pending_scroll_baseline=context.scroll_controller.pending_scroll_baseline,
        detail=context.detail,
        show_post_text=context.show_post_text,
        show_observed_text=context.show_observed_text,
    )


async def _scan_viewport(
    context: CycleContext,
    observer: HomeRowObserver,
    *,
    scan_limit: int,
) -> NurtureResult | None:
    context.state.transition(WorkflowPhase.WAITING_FOR_CONTENT)
    context.state.transition(WorkflowPhase.VERIFYING)
    context.reader.set_scan_viewport_index(context.viewport_number)
    context.reader.set_failure_context(context.state.phase.value, "SCAN_ROW")
    try:
        await context.reader.scan_viewport(
            scan_limit,
            scanned_fingerprints=context.dedupe.home_observed_this_session,
            on_row=observer.observe,
        )
        context.reader.last_scan_counts.new_verified_rows = len(
            observer.completed_viewport_fingerprints
        )
    except (UiReadError, PlaywrightError) as raw_error:
        return _handle_scan_failure(context, observer, raw_error)
    context.state.transition(WorkflowPhase.OBSERVING)
    return None


def _handle_scan_failure(
    context: CycleContext,
    observer: HomeRowObserver,
    raw_error: UiReadError | PlaywrightError,
) -> NurtureResult | None:
    error = (
        raw_error
        if isinstance(raw_error, UiReadError)
        else _reader_playwright_error(
            context.page,
            raw_error,
            phase=context.state.phase.value,
            operation="SCAN_ROW",
            console_output_policy=context.console_output_policy,
        )
    )
    context.state.stop(error.code)
    counts = context.reader.last_scan_counts
    counts.coverage_complete = False
    context.reader.refresh_viewport_body_coverage_counts()
    rejection_reasons = _rejection_reasons(counts)
    emit_console_diagnostic(
        context.console_output_policy,
        f"SCAN_STOP viewport={context.viewport_number} visible_links={counts.visible_links} "
        f"inspected={counts.inspected} url_parsed={counts.url_parsed} "
        f"author_unverified={counts.author_unverified} "
        f"wrapper_ambiguous={counts.wrapper_ambiguous} "
        f"card_not_ready={counts.card_not_ready} verified={counts.verified} "
        f"body_read={counts.body_read} body_unknown={counts.body_unknown} "
        f"classified={observer.viewport_classified} relevant={observer.viewport_relevant} "
        f"skipped={counts.skipped} rejection_reasons={rejection_reasons} "
        f"unrepresented_root_count={counts.unrepresented_root_count} "
        f"unresolved_visited_rows={counts.unresolved_visited_rows} "
        f"lookahead_rows={counts.lookahead_rows} "
        f"{_format_structure_diagnostics(counts)}",
    )
    if context.detail:
        emit_console_diagnostic(
            context.console_output_policy,
            f"[TIMING] viewport={context.viewport_number} "
            f"discover_ms={context.reader.last_scan_timings.discover_ms} "
            f"read_row_ms={context.reader.last_scan_timings.read_row_ms} "
            f"stabilize_ms={context.reader.last_scan_timings.stabilize_ms} "
            f"wait_reason={context.reader.last_scan_timings.wait_reason}",
            flush=True,
        )
        _print_url_structure_diagnostics(counts)
        emit_console_diagnostic(
            context.console_output_policy,
            f"[VIEWPORT {context.viewport_number}] status=PARTIAL code={error.code} "
            f"coverage={str(counts.coverage_complete).lower()} "
            f"unrepresented_root_count={counts.unrepresented_root_count} "
            f"unresolved_visited_rows={counts.unresolved_visited_rows} "
            f"lookahead_rows={counts.lookahead_rows}",
            flush=True,
        )
    if error.code.endswith("NOT_READY") and (
        context.dedupe.home_observed_this_session
        or context.stats.body_extracted
        or context.stats.observed_rows
    ):
        context.stats.observed_rows += counts.observed_rows
        context.stats.skipped_rows += counts.skipped_rows
        context.stats.lookahead_rows += counts.lookahead_rows
        if context.detail:
            _detail_stop("VIEWPORT_OBSERVATION_INCOMPLETE")
        return _result_from_observation(
            status="VIEWPORT_OBSERVATION_INCOMPLETE",
            stop_reason="VIEWPORT_OBSERVATION_INCOMPLETE",
            dedupe=context.dedupe,
            stats=context.stats,
            reader=context.reader,
            coverage=context.coverage,
            checkpoint_runs=context.checkpoint.completed_runs,
            viewport_coverage_complete=False,
        )
    if context.detail:
        emit_console_diagnostic(
            context.console_output_policy,
            f"[VIEWPORT] status=FAILED code={error.code} "
            f"coverage={str(counts.coverage_complete).lower()} "
            f"unrepresented_root_count={counts.unrepresented_root_count} "
            f"unresolved_visited_rows={counts.unresolved_visited_rows} "
            f"lookahead_rows={counts.lookahead_rows}",
        )
    raise error from None


def _rejection_reasons(counts: _FeedScanCounts) -> str:
    return (
        ",".join(f"{reason}:{count}" for reason, count in sorted(counts.rejection_reasons.items()))
        or "none"
    )


def _record_viewport_observation(
    context: CycleContext,
    observer: HomeRowObserver,
    *,
    scan_limit: int,
) -> bool:
    counts = context.reader.last_scan_counts
    if context.detail:
        emit_console_diagnostic(
            context.console_output_policy,
            f"[VIEWPORT {context.viewport_number}] observed={counts.observed_rows} "
            f"verified={counts.verified} body_read={counts.body_read} "
            f"body_unknown={counts.body_unknown} skipped={counts.skipped} "
            f"coverage={str(counts.coverage_complete).lower()} "
            f"unrepresented_root_count={counts.unrepresented_root_count} "
            f"unresolved_visited_rows={counts.unresolved_visited_rows} "
            f"lookahead_rows={counts.lookahead_rows}",
            flush=True,
        )
        emit_console_diagnostic(
            context.console_output_policy,
            f"[TIMING] viewport={context.viewport_number} "
            f"discover_ms={context.reader.last_scan_timings.discover_ms} "
            f"read_row_ms={context.reader.last_scan_timings.read_row_ms} "
            f"stabilize_ms={context.reader.last_scan_timings.stabilize_ms} "
            f"wait_reason={context.reader.last_scan_timings.wait_reason}",
            flush=True,
        )
        _print_url_structure_diagnostics(counts)

    context.previous_unverified_rows = observer.current_unverified_rows
    context.stats.observed_rows += counts.observed_rows
    context.stats.skipped_rows += counts.skipped_rows
    context.stats.lookahead_rows += counts.lookahead_rows
    unresolved_rows = [
        row
        for row in context.reader.last_candidate_diagnostics
        if row.state not in {"NON_POST", "VERIFIED"}
    ]
    if counts.roots_truncated or (not counts.coverage_complete and not unresolved_rows):
        context.coverage.unrepresented_gaps.add(f"viewport:{context.viewport_number}")
    context.stats.viewport_coverage_complete = not context.coverage.has_unresolved_gaps
    progress_stop_reason = context.scroll_controller.record_scan_progress(
        observer.row_target_progress_ids,
        counts,
    )
    if progress_stop_reason is not None:
        context.exhaustion_reason = progress_stop_reason
        return True

    _emit_scan_progress(context, observer, counts)
    if not counts.coverage_complete and not context.reader.last_viewport_deferred_only:
        _emit_incomplete_coverage(context, counts, scan_limit=scan_limit)
    return False


def _emit_scan_progress(
    context: CycleContext,
    observer: HomeRowObserver,
    counts: _FeedScanCounts,
) -> None:
    observation_status = (
        "RELEVANT_UNVERIFIED"
        if observer.viewport_unverified_relevant
        else "OBSERVED_UNVERIFIED"
        if counts.body_extracted > counts.body_read
        else "OBSERVATION_UNCERTAIN"
        if counts.uncertain_rows
        else "NONE"
    )
    emit_console_diagnostic(
        context.console_output_policy,
        f"SCANNING viewport={context.viewport_number} verified={counts.verified} "
        f"relevant={observer.viewport_relevant} skipped={counts.skipped} "
        f"visible_links={counts.visible_links} inspected={counts.inspected} "
        f"body_read={counts.body_read} body_unknown={counts.body_unknown} "
        f"classified={observer.viewport_classified} observed_rows={counts.observed_rows} "
        f"body_extracted={counts.body_extracted} "
        f"provenance_verified_current={counts.identity_verified} "
        f"body_complete_current={counts.verified} "
        f"unique_provenance_identities={len(context.coverage.provenance_verified)} "
        f"unique_verified_posts={len(context.coverage.fully_read)} "
        f"viewport_row_appearances={context.stats.observed_rows} "
        f"unverified_relevant={observer.viewport_unverified_relevant} "
        f"observation_status={observation_status} skipped_rows={counts.skipped_rows} "
        f"viewport_coverage_complete={str(counts.coverage_complete).lower()} "
        f"irrelevant={observer.viewport_irrelevant} unknown={observer.viewport_unknown} "
        f"observation_unknown={observer.viewport_observation_unknown} "
        f"url_parsed={counts.url_parsed} author_unverified={counts.author_unverified} "
        f"wrapper_ambiguous={counts.wrapper_ambiguous} "
        f"card_not_ready={counts.card_not_ready} "
        f"rejection_reasons={_rejection_reasons(counts)} "
        f"{_format_structure_diagnostics(counts)}",
    )


def _emit_incomplete_coverage(
    context: CycleContext,
    counts: _FeedScanCounts,
    *,
    scan_limit: int,
) -> None:
    emit_console_diagnostic(
        context.console_output_policy,
        f"VIEWPORT_COVERAGE_INCOMPLETE viewport={context.viewport_number} "
        f"observed_rows={counts.observed_rows} cap={scan_limit}",
    )
    if context.detail:
        emit_console_diagnostic(
            context.console_output_policy,
            f"[VIEWPORT] verified_body_extracted={counts.body_read} "
            f"observed_body_extracted={counts.body_extracted - counts.body_read} "
            f"scan={'PARTIAL' if context.reader.last_viewport_can_wheel else 'NOT_COMMITTED'} "
            f"wheel={'ALLOWED' if context.reader.last_viewport_can_wheel else 'BLOCKED'}",
        )


async def _select_or_advance(
    context: CycleContext,
    observer: HomeRowObserver,
) -> bool:
    if observer.viewport_relevant_targets:
        observer.viewport_relevant_targets.sort(
            key=lambda target: target.fingerprint in context.dedupe.checkpoint_seen
        )
        if context.max_scrolls > 0 and context.reader.scroll_attempts == 0:
            if await context.scroll_controller.advance(
                scan_after_scroll=(
                    len(context.dedupe.home_observed_this_session)
                    >= context.max_total_posts
                )
            ):
                if (
                    len(context.dedupe.home_observed_this_session)
                    >= context.max_total_posts
                ):
                    context.selected = await _select_still_visible(context)
                    if context.selected is not None:
                        return True
                    context.exhaustion_reason = "MAX_TOTAL_POSTS"
                    return True
                return False
            if context.reader.last_scroll_stop_reason == "FEED_END_REACHED":
                context.exhaustion_reason = "FEED_END_REACHED"

        context.selected = await context.selector.first_actionable(
            observer.viewport_relevant_targets
        )
        if context.selected is not None:
            return True

    seen_count = len(context.dedupe.home_observed_this_session)
    if seen_count >= context.max_total_posts:
        context.exhaustion_reason = "MAX_TOTAL_POSTS"
        if context.max_scrolls == 0 or context.reader.scroll_attempts > 0:
            context.selected = await context.selector.select_fallback(
                observer.viewport_fallback_targets
            )
        return True
    if context.reader.scroll_attempts >= context.max_scrolls:
        context.exhaustion_reason = "MAX_SCROLLS"
        context.selected = await context.selector.select_fallback(
            observer.viewport_fallback_targets
        )
        return True
    if not await context.scroll_controller.advance():
        context.exhaustion_reason = context.reader.last_scroll_stop_reason or "FEED_NOT_ADVANCED"
        if context.exhaustion_reason in {"FEED_END_REACHED", "FEED_NOT_SCROLLABLE"}:
            context.selected = await context.selector.select_fallback(
                observer.viewport_fallback_targets
            )
        return True
    return False


async def _select_still_visible(context: CycleContext) -> PostTarget | None:
    if not (
        context.reader.last_viewport_coverage_complete
        and context.reader.last_viewport_observed_rows <= context.max_posts
    ):
        return None
    still_visible = [
        target
        for target in context.reader.last_scroll_targets
        if target.fingerprint in context.dedupe.pending_set
        and context.dedupe.decisions_by_fingerprint.get(target.fingerprint) == "RELEVANT"
        and classify_body(target.body, context.normalized_terms) == "RELEVANT"
    ]
    still_visible.sort(key=lambda target: target.fingerprint in context.dedupe.checkpoint_seen)
    return await context.selector.first_actionable(still_visible)
