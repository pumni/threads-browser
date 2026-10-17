"""Compatibility API for the bounded Home / For You workflow."""

from __future__ import annotations

import asyncio
import math
import sys
from collections.abc import Awaitable, Callable

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright

from .browser_ui.session import classify_playwright_failure, emit_failure_diagnostic
from .nurture.diagnostics import _terminal_safe_post_text
from .nurture.engine import NurtureResult
from .nurture.engine import _run_page_workflow_cycle as _execute_page_workflow
from .nurture.state import InvalidWorkflowTransition, policy_fingerprint
from .reader import ThreadsHomeReader, UiReadError
from .storage import BrowserAccount, BrowserDataError, CheckpointStore, ensure_safe_browser_profile

__all__ = [
    "NurtureResult",
    "_terminal_safe_post_text",
    "policy_fingerprint",
    "run_browser_nurture",
]

_CLEANUP_ALLOWANCE_SECONDS = 5


def _detail_terminal_ready() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, OSError, ValueError):
        return False


async def _run_page_workflow_locked(
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
) -> NurtureResult:
    """Run one page cycle; caller must hold the account profile lock."""
    try:
        return await _execute_page_workflow(
            page,
            account,
            checkpoint_store,
            max_posts=max_posts,
            terms=terms,
            max_scrolls=max_scrolls,
            max_total_posts=max_total_posts,
            readiness_prompt=readiness_prompt,
            show_post_text=show_post_text,
            show_observed_text=show_observed_text,
            detail=detail,
            reader_factory=ThreadsHomeReader,
        )
    except UiReadError:
        raise
    except PlaywrightError as error:
        failure = classify_playwright_failure(page, error)
        emit_failure_diagnostic(
            phase="UNKNOWN",
            operation="OTHER",
            error_class=failure.error_class,
            page_closed=failure.page_closed,
            context_closed=failure.context_closed,
        )
        code = (
            "BROWSER_SESSION_UNAVAILABLE"
            if failure.error_class == "TARGET_CLOSED"
            else "DOM_CHANGED_DURING_SCAN"
            if failure.error_class == "DOM_DETACHED"
            else "UI_READ_UNVERIFIED"
        )
        raise UiReadError(code) from None
    except InvalidWorkflowTransition as error:
        emit_failure_diagnostic(
            phase=error.phase.value,
            operation="STATE_TRANSITION",
            error_class="STATE_INVALID",
            page_closed=page.is_closed(),
            context_closed=None,
        )
        raise UiReadError("INTERNAL_STATE_ERROR") from None
    except (ValueError, RuntimeError):
        emit_failure_diagnostic(
            phase="UNKNOWN",
            operation="STATE_TRANSITION",
            error_class="STATE_INVALID",
            page_closed=page.is_closed(),
            context_closed=None,
        )
        raise UiReadError("INTERNAL_STATE_ERROR") from None
    except Exception:  # noqa: BLE001 - Sanitize unexpected failures at the API boundary.
        emit_failure_diagnostic(
            phase="UNKNOWN",
            operation="OTHER",
            error_class="OTHER",
            page_closed=page.is_closed(),
            context_closed=None,
        )
        raise UiReadError("INTERNAL_OPERATION_ERROR") from None


async def run_browser_nurture(
    account: BrowserAccount,
    checkpoint_store: CheckpointStore,
    *,
    max_posts: int,
    terms: tuple[str, ...],
    max_scrolls: int = 8,
    max_total_posts: int = 40,
    max_runtime_seconds: float | None = None,
    readiness_prompt: Callable[[str, int], Awaitable[bool]] | None = None,
    show_post_text: bool = False,
    show_observed_text: bool = False,
    detail: bool = False,
) -> NurtureResult:
    if account.expected_actor is None:
        raise BrowserDataError("ACCOUNT_NOT_VERIFIED")
    if detail and not _detail_terminal_ready():
        raise UiReadError("DETAIL_TTY_REQUIRED")
    if max_runtime_seconds is not None and (
        not math.isfinite(max_runtime_seconds) or max_runtime_seconds <= 0
    ):
        raise UiReadError("RUNTIME_LIMIT_INVALID")
    lock = checkpoint_store.profile_lock(account)
    lock.acquire()
    playwright_manager = None
    manager_start_attempted = False
    context = None
    timeout_scope = None
    try:
        ensure_safe_browser_profile(account.profile_directory)
        timeout_scope = asyncio.timeout(max_runtime_seconds)
        try:
            async with timeout_scope:
                playwright_manager = async_playwright()
                manager_start_attempted = True
                playwright = await playwright_manager.__aenter__()
                context = await playwright.chromium.launch_persistent_context(
                    user_data_dir=str(account.profile_directory),
                    headless=False,
                )
                page = context.pages[0] if context.pages else await context.new_page()
                return await _run_page_workflow_locked(
                    page,
                    account,
                    checkpoint_store,
                    max_posts=max_posts,
                    terms=terms,
                    max_scrolls=max_scrolls,
                    max_total_posts=max_total_posts,
                    readiness_prompt=readiness_prompt,
                    show_post_text=show_post_text,
                    show_observed_text=show_observed_text,
                    detail=detail,
                )
        except TimeoutError:
            if timeout_scope is None or not timeout_scope.expired():
                raise
            emit_failure_diagnostic(
                phase="STOPPED",
                operation="OTHER",
                error_class="OTHER",
                error_kind="TIMEOUT",
                page_closed=None,
                context_closed=None,
            )
            raise UiReadError("CYCLE_TIMEOUT") from None
    except (UiReadError, BrowserDataError):
        raise
    except PlaywrightError as error:
        failure = classify_playwright_failure(None, error)
        emit_failure_diagnostic(
            phase="INITIALIZING",
            operation="LAUNCH_SESSION",
            error_class=failure.error_class,
            page_closed=failure.page_closed,
            context_closed=failure.context_closed,
        )
        code = (
            "BROWSER_SESSION_UNAVAILABLE"
            if failure.error_class == "TARGET_CLOSED"
            else "UI_READ_UNVERIFIED"
        )
        raise UiReadError(code) from None
    except OSError:
        emit_failure_diagnostic(
            phase="INITIALIZING",
            operation="LAUNCH_SESSION",
            error_class="OTHER",
            page_closed=False,
            context_closed=None,
        )
        raise UiReadError("INTERNAL_OPERATION_ERROR") from None
    finally:
        operation_failed = sys.exc_info()[0] is not None
        async def cleanup_resources() -> bool:
            cleanup_failed = False
            try:
                if context is not None:
                    try:
                        async with asyncio.timeout(_CLEANUP_ALLOWANCE_SECONDS):
                            await context.close()
                    except TimeoutError:
                        cleanup_failed = True
                        _emit_cleanup_failure(timed_out=True)
                    except Exception:  # noqa: BLE001 - sanitize browser cleanup failures.
                        cleanup_failed = True
                        _emit_cleanup_failure(timed_out=False)
                if manager_start_attempted:
                    assert playwright_manager is not None
                    try:
                        async with asyncio.timeout(_CLEANUP_ALLOWANCE_SECONDS):
                            await playwright_manager.__aexit__(None, None, None)
                    except TimeoutError:
                        cleanup_failed = True
                        _emit_cleanup_failure(timed_out=True)
                    except Exception:  # noqa: BLE001 - sanitize browser cleanup failures.
                        cleanup_failed = True
                        _emit_cleanup_failure(timed_out=False)
            finally:
                lock.release()
            return cleanup_failed

        try:
            cleanup_task = asyncio.create_task(cleanup_resources())
        except BaseException:
            lock.release()
            raise
        cleanup_cancelled = False
        while True:
            try:
                cleanup_failed = await asyncio.shield(cleanup_task)
                break
            except asyncio.CancelledError:
                current_task = asyncio.current_task()
                if current_task is None or current_task.cancelling() == 0:
                    raise
                current_task.uncancel()
                cleanup_cancelled = True
        if cleanup_cancelled and not operation_failed:
            raise asyncio.CancelledError
        if cleanup_failed and not operation_failed:
            raise UiReadError("BROWSER_CLEANUP_FAILED") from None


def _emit_cleanup_failure(*, timed_out: bool) -> None:
    emit_failure_diagnostic(
        phase="STOPPED",
        operation="OTHER",
        error_class="OTHER",
        error_kind="TIMEOUT" if timed_out else "OTHER",
        page_closed=None,
        context_closed=None,
    )
