"""Resolve the one-cycle status and fail-closed early return policy."""

from __future__ import annotations

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from ..browser_ui.session import classify_playwright_failure, emit_failure_diagnostic
from ..console_output import ConsoleOutputPolicy, emit_console_diagnostic
from ..errors import UiReadError
from ..feed.models import PostTarget
from ..reader import ThreadsHomeReader
from .diagnostics import _detail_stop
from .results import NurtureResult, _result_from_observation
from .state import (
    CoverageLedger,
    DedupeLedger,
    NurtureRunState,
    WorkflowStats,
)


def _reader_playwright_error(
    page: Page,
    error: PlaywrightError,
    *,
    phase: str,
    operation: str,
    console_output_policy: ConsoleOutputPolicy = ConsoleOutputPolicy.INTERACTIVE,
) -> UiReadError:
    failure = classify_playwright_failure(page, error)
    emit_failure_diagnostic(
        phase=phase,
        operation=operation,
        error_class=failure.error_class,
        page_closed=failure.page_closed,
        context_closed=failure.context_closed,
        console_output_policy=console_output_policy,
    )
    code = (
        "BROWSER_SESSION_UNAVAILABLE"
        if failure.error_class == "TARGET_CLOSED"
        else "DOM_CHANGED_DURING_SCAN"
        if failure.error_class == "DOM_DETACHED" and operation == "SCAN_ROW"
        else "UI_READ_UNVERIFIED"
    )
    return UiReadError(code)


class CycleOutcomeResolver:
    """Keep early-stop and completion status policy outside scan orchestration."""

    def __init__(
        self,
        *,
        reader: ThreadsHomeReader,
        coverage: CoverageLedger,
        dedupe: DedupeLedger,
        stats: WorkflowStats,
        state: NurtureRunState,
        console_output_policy: ConsoleOutputPolicy,
        detail: bool,
        show_post_text: bool,
    ) -> None:
        self._reader = reader
        self._coverage = coverage
        self._dedupe = dedupe
        self._stats = stats
        self._state = state
        self._console_output_policy = console_output_policy
        self._detail = detail
        self._show_post_text = show_post_text

    def resolve(
        self,
        *,
        selected: PostTarget | None,
        selected_status: str | None,
        detail_retry_pending: bool,
        exhaustion_reason: str,
        actionability_rejected: bool,
        checkpoint_runs: int,
    ) -> NurtureResult | str:
        if selected is not None:
            status = selected_status or "COMPLETE"
        elif self._stats.unverified_relevant and not self._stats.found_relevant and not self._coverage.has_skips:
            emit_console_diagnostic(
                self._console_output_policy,
                "SEARCH_INCOMPLETE reason=UNVERIFIED_RELEVANCE",
            )
            if self._detail:
                _detail_stop("UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION")
            return self._result(
                "UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION",
                checkpoint_runs=checkpoint_runs,
            )
        elif actionability_rejected:
            emit_console_diagnostic(
                self._console_output_policy,
                "SEARCH_INCOMPLETE reason=NO_ACTIONABLE_TARGET",
            )
            if self._detail:
                _detail_stop("NO_ACTIONABLE_TARGET")
            self._state.stop("NO_ACTIONABLE_TARGET")
            return self._result("NO_ACTIONABLE_TARGET", checkpoint_runs=checkpoint_runs)
        elif not self._dedupe.seen_this_run and not self._coverage.has_skips:
            if self._show_post_text and self._stats.previewed_bodies == 0:
                emit_console_diagnostic(
                    self._console_output_policy,
                    "POST_TEXT status=NONE "
                    f"verified_posts={len(self._dedupe.seen_this_run)} "
                    f"extracted={self._stats.previewed_bodies}",
                )
            status = (
                "UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION"
                if self._stats.unverified_relevant
                else "OBSERVED_UNVERIFIED_ONLY"
                if self._stats.body_extracted
                else "OBSERVATION_UNCERTAIN_ONLY"
                if self._stats.observed_rows
                else "NO_VERIFIED_POSTS_IN_BUDGET"
            )
            if self._detail:
                _detail_stop(status)
            return self._result(
                status,
                checkpoint_runs=checkpoint_runs,
                report_home_counts=False,
            )
        elif not detail_retry_pending and (
            self._dedupe.pending_set
            or (self._stats.found_relevant and exhaustion_reason == "FEED_END_REACHED")
        ):
            status = (
                "FEED_END_REACHED"
                if exhaustion_reason == "FEED_END_REACHED"
                else "RELEVANT_POST_NOT_CURRENT"
            )
            emit_console_diagnostic(
                self._console_output_policy,
                f"SEARCH_INCOMPLETE reason={status}",
            )
            if self._detail:
                _detail_stop(status)
            return self._result(status, checkpoint_runs=checkpoint_runs)
        elif exhaustion_reason == "FEED_END_REACHED":
            status = "FEED_END_REACHED"
        elif exhaustion_reason == "FEED_NOT_SCROLLABLE":
            status = "FEED_NOT_SCROLLABLE"
        elif self._stats.unverified_relevant and not self._dedupe.seen_this_run:
            status = "OBSERVED_UNVERIFIED_ONLY"
        elif self._stats.found_relevant or self._stats.relevant:
            status = "NO_NEW_RELEVANT_POST"
        elif self._stats.unknown:
            status = "VERIFIED_BUT_UNKNOWN_BODY"
        elif self._stats.irrelevant:
            status = "VERIFIED_IRRELEVANT"
        else:
            status = "NO_RELEVANT_POST_IN_BUDGET"

        if detail_retry_pending:
            return "DETAIL_RETRY_PENDING"
        if self._coverage.has_skips:
            return "COMPLETE_WITH_SKIPS"
        return status

    def _result(
        self,
        status: str,
        *,
        checkpoint_runs: int,
        report_home_counts: bool = True,
    ) -> NurtureResult:
        return _result_from_observation(
            status=status,
            stop_reason=status,
            dedupe=self._dedupe,
            stats=self._stats,
            reader=self._reader,
            coverage=self._coverage,
            checkpoint_runs=checkpoint_runs,
            report_home_counts=report_home_counts,
        )
