"""Bounded multi-observation feed sessions over one persistent Home reader."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from playwright.async_api import Page

from ..console_output import ConsoleOutputPolicy, emit_console_diagnostic
from ..reader import ThreadsHomeReader, UiReadError
from ..storage import BrowserAccount, CheckpointStore
from .cycle_context import CycleContext
from .engine import _create_cycle_context, _make_reader, _validate_cycle
from .finish_cycle import finish_cycle
from .results import NurtureResult
from .scan_cycle import scan_home_feed
from .session_state import SessionProgress
from .state import DedupeLedger, auto_policy_fingerprint, policy_fingerprint
from .uat_diagnostics import AutoUatDiagnostics


async def run_page_workflow_session(
    page: Page,
    account: BrowserAccount,
    checkpoint_store: CheckpointStore,
    *,
    max_posts: int,
    terms: tuple[str, ...],
    max_scrolls: int,
    max_total_posts: int,
    max_detail_reads: int,
    readiness_prompt: Callable[[str, int], Awaitable[bool]] | None = None,
    console_output_policy: ConsoleOutputPolicy = ConsoleOutputPolicy.INTERACTIVE,
    auto_mode: bool = False, uat_diagnostics: AutoUatDiagnostics | None = None,
    reader_factory: Callable[..., ThreadsHomeReader] = ThreadsHomeReader,
) -> NurtureResult:
    """Continue verified Detail/Back observations without resetting session evidence."""
    normalized_terms, fingerprint = _prepare_session_policy(
        account, max_posts, terms, max_scrolls, max_total_posts, max_detail_reads, auto_mode
    )
    checkpoint = checkpoint_store.load(account.account_id, fingerprint)
    progress = SessionProgress(
        checkpoint=checkpoint,
        checkpoint_store=checkpoint_store,
        account_id=account.account_id,
        policy_fingerprint=fingerprint,
        dedupe=DedupeLedger.from_checkpoint(
            checkpoint.seen_fingerprints,
            checkpoint.pending_read_fingerprints,
        ),
    )
    reader = _make_reader(
        page,
        account,
        readiness_prompt=readiness_prompt,
        detail=False,
        console_output_policy=console_output_policy,
        reader_factory=reader_factory,
    )
    last_context: CycleContext | None = None
    while True:
        if progress.detail_attempts >= max_detail_reads:
            return _finalize_session(
                progress,
                reader,
                last_context,
                status="SESSION_BUDGET_EXHAUSTED",
                stop_reason="MAX_DETAIL_READS",
            )
        if len(progress.dedupe.home_observed_this_session) >= max_total_posts:
            return _finalize_session(
                progress,
                reader,
                last_context,
                status="SESSION_BUDGET_EXHAUSTED",
                stop_reason="MAX_TOTAL_POSTS",
            )

        context = await _create_cycle_context(
            page,
            account,
            checkpoint_store,
            checkpoint=progress.checkpoint,
            policy_fingerprint=fingerprint,
            normalized_terms=normalized_terms,
            reader=reader,
            dedupe=progress.dedupe,
            coverage=progress.coverage,
            max_posts=max_posts,
            max_scrolls=max_scrolls,
            max_total_posts=max_total_posts,
            console_output_policy=console_output_policy,
            detail=False,
            show_post_text=False,
            show_observed_text=False,
            session_progress=progress,
            auto_mode=auto_mode,
            home_already_verified=progress.detail_reads > 0,
            viewport_number=progress.viewport_number,
            previous_unverified_rows=progress.previous_unverified_rows,
            uat_diagnostics=uat_diagnostics,
        )
        last_context = context
        partial_result = await scan_home_feed(context)
        if partial_result is not None:
            progress.record_cycle(
                context.stats,
                detail_attempted=context.detail_attempted,
                detail_reads=context.detail_reads,
                viewport_number=context.viewport_number,
                previous_unverified_rows=context.previous_unverified_rows,
            )
            return progress.result(
                reader,
                status=partial_result.status,
                stop_reason=partial_result.stop_reason,
            )

        result = await finish_cycle(context)
        if context.detail_reads:
            if len(progress.dedupe.home_observed_this_session) >= max_total_posts:
                return _finalize_session(
                    progress,
                    reader,
                    context,
                    status="SESSION_BUDGET_EXHAUSTED",
                    stop_reason="MAX_TOTAL_POSTS",
                )
            if progress.detail_attempts >= max_detail_reads:
                return _finalize_session(
                    progress,
                    reader,
                    context,
                    status="SESSION_BUDGET_EXHAUSTED",
                    stop_reason="MAX_DETAIL_READS",
                )
            await reader.prepare_home_resume()
            continue

        if context.detail_retry_pending:
            return _finalize_session(
                progress,
                reader,
                context,
                status="DETAIL_RETRY_PENDING",
                stop_reason="DETAIL_RETRY_PENDING",
            )
        if result.status == "SESSION_COVERAGE_INCOMPLETE":
            return progress.result(
                reader,
                status=result.status,
                stop_reason=result.stop_reason,
            )
        if context.session_checkpoint_committed:
            status = (
                "SESSION_COMPLETE"
                if context.exhaustion_reason in {"FEED_END_REACHED", "FEED_NOT_SCROLLABLE"}
                else "SESSION_BUDGET_EXHAUSTED"
            )
            return _finalize_session(
                progress,
                reader,
                context,
                status=status,
                stop_reason=context.exhaustion_reason,
            )
        return progress.result(
            reader,
            status=result.status,
            stop_reason=result.stop_reason,
        )


def _finalize_session(
    progress: SessionProgress,
    reader: ThreadsHomeReader,
    context: CycleContext | None,
    *,
    status: str,
    stop_reason: str,
) -> NurtureResult:
    if progress.coverage.has_skips or progress.coverage.has_unresolved_gaps:
        return progress.result(
            reader,
            status="SESSION_COVERAGE_INCOMPLETE",
            stop_reason="COVERAGE_INCOMPLETE",
        )
    if context is not None and context.session_checkpoint_committed:
        if not progress.has_safe_boundary(context.stats):
            return progress.result(
                reader,
                status="SESSION_COVERAGE_INCOMPLETE",
                stop_reason="COVERAGE_INCOMPLETE",
            )
        emit_console_diagnostic(
            context.console_output_policy,
            f"[SESSION] status={status} stop_reason={stop_reason}",
        )
    elif not progress.checkpoint_saved:
        return progress.result(
            reader,
            status="SESSION_COVERAGE_INCOMPLETE"
            if progress.coverage.has_skips or progress.coverage.has_unresolved_gaps
            else status,
            stop_reason=(
                "COVERAGE_INCOMPLETE"
                if progress.coverage.has_skips or progress.coverage.has_unresolved_gaps
                else stop_reason
            ),
        )
    return progress.result(reader, status=status, stop_reason=stop_reason)


def _prepare_session_policy(
    account: BrowserAccount,
    max_posts: int,
    terms: tuple[str, ...],
    max_scrolls: int,
    max_total_posts: int,
    max_detail_reads: int,
    auto_mode: bool,
) -> tuple[tuple[str, ...], str]:
    normalized_terms = _validate_cycle(
        account,
        max_posts,
        max_scrolls,
        max_total_posts,
        terms,
        allow_empty_terms=auto_mode,
    )
    if not 1 <= max_detail_reads <= 40:
        raise UiReadError("DETAIL_READ_LIMIT_INVALID")
    fingerprint = (
        auto_policy_fingerprint() if auto_mode else policy_fingerprint(normalized_terms)
    )
    return normalized_terms, fingerprint
