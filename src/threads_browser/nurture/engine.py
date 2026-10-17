"""Bounded, read-only Home / For You nurture orchestration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from ..console_output import ConsoleOutputPolicy, emit_console_diagnostic
from ..feed.relevance import canonical_terms
from ..reader import ThreadsHomeReader, UiReadError
from ..storage import BrowserAccount, BrowserDataError, CheckpointStore
from .cycle_context import CycleContext
from .feed_scroll import FeedScrollController
from .finish_cycle import finish_cycle
from .outcomes import _reader_playwright_error
from .results import NurtureResult, _result_from_observation  # noqa: F401 - compatibility exports
from .scan_cycle import scan_home_feed
from .selection import VerifiedTargetSelector
from .state import (
    CoverageLedger,
    DedupeLedger,
    NurtureRunState,
    WorkflowPhase,
    WorkflowStats,
    policy_fingerprint,
)


async def _run_page_workflow_cycle(
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
    console_output_policy: ConsoleOutputPolicy = ConsoleOutputPolicy.INTERACTIVE,
    reader_factory: Callable[..., ThreadsHomeReader] = ThreadsHomeReader,
) -> NurtureResult:
    """Run one internal page cycle under workflow's held profile lock."""
    state = NurtureRunState()
    normalized_terms = _validate_cycle(account, max_posts, max_scrolls, max_total_posts, terms)
    fingerprint = policy_fingerprint(normalized_terms)
    checkpoint = checkpoint_store.load(account.account_id, fingerprint)
    dedupe = DedupeLedger.from_checkpoint(
        checkpoint.seen_fingerprints,
        checkpoint.pending_read_fingerprints,
    )
    stats = WorkflowStats()
    reader = _make_reader(
        page,
        account,
        readiness_prompt=readiness_prompt,
        detail=detail,
        console_output_policy=console_output_policy,
        reader_factory=reader_factory,
    )
    reader.set_failure_context("INITIALIZING", "SETTLE")
    await _open_home(reader, page, state, console_output_policy, detail)
    state.transition(WorkflowPhase.OBSERVING)
    if detail:
        emit_console_diagnostic(
            console_output_policy,
            "[HOME] status=READY account=VERIFIED source=FOR_YOU",
        )

    coverage = CoverageLedger()
    selector, scroll_controller = _make_selection_components(
        page,
        reader,
        state,
        stats,
        dedupe,
        coverage,
        console_output_policy,
        detail,
    )
    context = CycleContext(
        page=page,
        account=account,
        checkpoint_store=checkpoint_store,
        checkpoint=checkpoint,
        policy_fingerprint=fingerprint,
        normalized_terms=normalized_terms,
        state=state,
        dedupe=dedupe,
        stats=stats,
        reader=reader,
        coverage=coverage,
        selector=selector,
        scroll_controller=scroll_controller,
        max_posts=max_posts,
        max_scrolls=max_scrolls,
        max_total_posts=max_total_posts,
        console_output_policy=console_output_policy,
        detail=detail,
        show_post_text=show_post_text,
        show_observed_text=show_observed_text,
    )
    partial_result = await scan_home_feed(context)
    if partial_result is not None:
        return partial_result
    return await finish_cycle(context)


def _validate_cycle(
    account: BrowserAccount,
    max_posts: int,
    max_scrolls: int,
    max_total_posts: int,
    terms: tuple[str, ...],
) -> tuple[str, ...]:
    if account.expected_actor is None:
        raise BrowserDataError("ACCOUNT_NOT_VERIFIED")
    if not 1 <= max_posts <= 20:
        raise UiReadError("POST_LIMIT_INVALID")
    if not 0 <= max_scrolls <= 20:
        raise UiReadError("SCROLL_LIMIT_INVALID")
    if not 1 <= max_total_posts <= 400:
        raise UiReadError("TOTAL_SCAN_LIMIT_INVALID")
    return canonical_terms(terms)


def _make_reader(
    page: Page,
    account: BrowserAccount,
    *,
    readiness_prompt: Callable[[str, int], Awaitable[bool]] | None,
    detail: bool,
    console_output_policy: ConsoleOutputPolicy,
    reader_factory: Callable[..., ThreadsHomeReader],
) -> ThreadsHomeReader:
    reader_options = {}
    if console_output_policy is not ConsoleOutputPolicy.INTERACTIVE:
        reader_options["console_output_policy"] = console_output_policy
    return reader_factory(
        page,
        account.expected_actor,
        readiness_prompt=readiness_prompt,
        detail_mode=detail,
        **reader_options,
    )


async def _open_home(
    reader: ThreadsHomeReader,
    page: Page,
    state: NurtureRunState,
    console_output_policy: ConsoleOutputPolicy,
    detail: bool,
) -> None:
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
                console_output_policy=console_output_policy,
            )
        )
        state.stop(error.code)
        if detail:
            emit_console_diagnostic(
                console_output_policy,
                f"[HOME] status=FAILED code={error.code}",
            )
        raise error from None


def _make_selection_components(
    page: Page,
    reader: ThreadsHomeReader,
    state: NurtureRunState,
    stats: WorkflowStats,
    dedupe: DedupeLedger,
    coverage: CoverageLedger,
    console_output_policy: ConsoleOutputPolicy,
    detail: bool,
) -> tuple[VerifiedTargetSelector, FeedScrollController]:
    selector = VerifiedTargetSelector(
        page=page,
        reader=reader,
        state=state,
        stats=stats,
        dedupe=dedupe,
        coverage=coverage,
        console_output_policy=console_output_policy,
        detail=detail,
        map_playwright_error=_reader_playwright_error,
    )
    scroll_controller = FeedScrollController(
        page=page,
        reader=reader,
        stats=stats,
        coverage=coverage,
        state=state,
        console_output_policy=console_output_policy,
        detail=detail,
        map_playwright_error=_reader_playwright_error,
    )
    return selector, scroll_controller
