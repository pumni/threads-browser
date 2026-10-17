"""Detail/Back, outcome resolution and checkpoint commit for a page cycle."""

from __future__ import annotations

from ..console_output import emit_console_diagnostic
from ..storage import BrowserDataError
from .checkpoint import CheckpointCommitter
from .cycle_context import CycleContext
from .detail_cycle import DetailBackCoordinator
from .diagnostics import _detail_stop
from .outcomes import CycleOutcomeResolver, _reader_playwright_error
from .results import NurtureResult, _result_from_observation


async def finish_cycle(context: CycleContext) -> NurtureResult:
    """Read one selected Detail safely, resolve status and commit a valid boundary."""
    if context.selected is None and not context.stats.found_relevant:
        _report_search_stop(context)
    if context.selected is not None:
        await _read_selected_detail(context)

    outcome = CycleOutcomeResolver(
        reader=context.reader,
        coverage=context.coverage,
        dedupe=context.dedupe,
        stats=context.stats,
        state=context.state,
        console_output_policy=context.console_output_policy,
        detail=context.detail,
        show_post_text=context.show_post_text,
    ).resolve(
        selected=context.selected,
        selected_status=context.selected_status if context.selected is not None else None,
        detail_retry_pending=context.detail_retry_pending,
        exhaustion_reason=context.exhaustion_reason,
        actionability_rejected=bool(context.selector.rejected_codes),
        checkpoint_runs=context.checkpoint.completed_runs,
    )
    if isinstance(outcome, NurtureResult):
        return outcome
    status = outcome

    try:
        updated = CheckpointCommitter(
            context.checkpoint_store,
            account_id=context.account.account_id,
            policy_fingerprint=context.policy_fingerprint,
        ).commit(
            context.checkpoint,
            dedupe=context.dedupe,
            stats=context.stats,
            reader=context.reader,
            state=context.state,
            detail_reads=context.detail_reads,
        )
    except BrowserDataError as error:
        if context.detail:
            _detail_stop(error.code)
        raise
    if context.detail:
        emit_console_diagnostic(
            context.console_output_policy,
            f"[CHECKPOINT] status=SAVED completed_runs={updated.completed_runs}",
        )
        emit_console_diagnostic(context.console_output_policy, f"[CYCLE] status={status}")
    if context.show_post_text and context.stats.previewed_bodies == 0:
        emit_console_diagnostic(
            context.console_output_policy,
            "POST_TEXT status=NONE "
            f"verified_posts={len(context.dedupe.seen_this_run)} "
            f"extracted={context.stats.previewed_bodies}",
        )
    context.state.stop(status)
    return _result_from_observation(
        status=status,
        stop_reason=(
            "DETAIL_RETRY_PENDING" if context.detail_retry_pending else context.exhaustion_reason
        ),
        dedupe=context.dedupe,
        stats=context.stats,
        reader=context.reader,
        coverage=context.coverage,
        detail_reads=context.detail_reads,
        checkpoint_runs=updated.completed_runs,
        checkpoint_saved=True,
    )


def _report_search_stop(context: CycleContext) -> None:
    if context.exhaustion_reason in {"FEED_END_REACHED", "FEED_NOT_SCROLLABLE"}:
        emit_console_diagnostic(
            context.console_output_policy,
            f"SEARCH_STOP reason={context.exhaustion_reason} viewports={context.viewport_number}",
        )
    else:
        emit_console_diagnostic(
            context.console_output_policy,
            f"SEARCH_BUDGET_EXHAUSTED reason={context.exhaustion_reason} "
            f"viewports={context.viewport_number}",
        )


async def _read_selected_detail(context: CycleContext) -> None:
    target = context.selected
    if target is None:
        return
    outcome = await DetailBackCoordinator(
        page=context.page,
        reader=context.reader,
        state=context.state,
        dedupe=context.dedupe,
        console_output_policy=context.console_output_policy,
        detail=context.detail,
        map_playwright_error=_reader_playwright_error,
    ).read_and_return(
        target,
        selected_number=context.dedupe.post_numbers[target.fingerprint],
        selected_decision=context.dedupe.decisions_by_fingerprint.get(
            target.fingerprint,
            "UNKNOWN",
        ),
    )
    context.detail_reads = outcome.detail_reads
    context.detail_retry_pending = outcome.retry_pending
    context.selected_status = outcome.status
