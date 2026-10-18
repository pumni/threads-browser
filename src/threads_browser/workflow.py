"""Compatibility API for the bounded Home / For You workflow."""

from __future__ import annotations

import asyncio
import math
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright

from .browser_ui.session import classify_playwright_failure, emit_failure_diagnostic
from .console_output import ConsoleOutputPolicy
from .nurture.diagnostics import _terminal_safe_post_text
from .nurture.engine import NurtureResult, _validate_cycle
from .nurture.engine import _run_page_workflow_cycle as _execute_page_workflow
from .nurture.session import run_page_workflow_session as _execute_page_workflow_session
from .nurture.state import InvalidWorkflowTransition, policy_fingerprint
from .reader import ThreadsHomeReader, UiReadError
from .storage import (
    BrowserAccount,
    BrowserDataError,
    CheckpointStore,
    ProcessLock,
    ensure_safe_browser_profile,
)

__all__ = [
    "NurtureResult",
    "_terminal_safe_post_text",
    "policy_fingerprint",
    "run_browser_nurture",
    "run_browser_nurture_auto_cycle",
    "run_browser_nurture_session",
]

_CLEANUP_ALLOWANCE_SECONDS = 5
# Hosted Windows CI observed context close exceed the former five-second bound.
_SESSION_CONTEXT_CLEANUP_ALLOWANCE_SECONDS = 8
_SESSION_MANAGER_CLEANUP_ALLOWANCE_SECONDS = 5
_MIN_SESSION_OPERATION_SECONDS = 0.1


def _session_cleanup_allowances(max_runtime_seconds: float) -> tuple[float, float]:
    """Reserve bounded teardown time inside the requested session runtime."""
    context_allowance = _SESSION_CONTEXT_CLEANUP_ALLOWANCE_SECONDS
    manager_allowance = _SESSION_MANAGER_CLEANUP_ALLOWANCE_SECONDS
    total_allowance = context_allowance + manager_allowance
    available_for_cleanup = max_runtime_seconds - _MIN_SESSION_OPERATION_SECONDS
    scale = min(1.0, available_for_cleanup / total_allowance)
    return context_allowance * scale, manager_allowance * scale


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
    console_output_policy: ConsoleOutputPolicy = ConsoleOutputPolicy.INTERACTIVE,
) -> NurtureResult:
    """Run one page cycle; caller must hold the account profile lock."""
    return await _run_locked_page_operation(
        page,
        lambda: _execute_page_workflow(
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
            console_output_policy=console_output_policy,
            reader_factory=ThreadsHomeReader,
        ),
        console_output_policy=console_output_policy,
    )


async def _run_page_workflow_session_locked(
    page: Page,
    account: BrowserAccount,
    checkpoint_store: CheckpointStore,
    *,
    max_posts: int,
    terms: tuple[str, ...],
    max_scrolls: int,
    max_total_posts: int,
    max_detail_reads: int,
    readiness_prompt: Callable[[str, int], Awaitable[bool]] | None,
    console_output_policy: ConsoleOutputPolicy,
    auto_mode: bool = False,
) -> NurtureResult:
    """Run a multi-observation session under the caller-held profile lock."""
    return await _run_locked_page_operation(
        page,
        lambda: _execute_page_workflow_session(
            page,
            account,
            checkpoint_store,
            max_posts=max_posts,
            terms=terms,
            max_scrolls=max_scrolls,
            max_total_posts=max_total_posts,
            max_detail_reads=max_detail_reads,
            readiness_prompt=readiness_prompt,
            console_output_policy=console_output_policy,
            auto_mode=auto_mode,
            reader_factory=ThreadsHomeReader,
        ),
        console_output_policy=console_output_policy,
    )


async def _run_locked_page_operation(
    page: Page,
    operation: Callable[[], Awaitable[NurtureResult]],
    *,
    console_output_policy: ConsoleOutputPolicy,
) -> NurtureResult:
    try:
        return await operation()
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
            console_output_policy=console_output_policy,
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
            console_output_policy=console_output_policy,
        )
        raise UiReadError("INTERNAL_STATE_ERROR") from None
    except (ValueError, RuntimeError):
        emit_failure_diagnostic(
            phase="UNKNOWN",
            operation="STATE_TRANSITION",
            error_class="STATE_INVALID",
            page_closed=page.is_closed(),
            context_closed=None,
            console_output_policy=console_output_policy,
        )
        raise UiReadError("INTERNAL_STATE_ERROR") from None
    except Exception:  # noqa: BLE001 - Sanitize unexpected failures at the API boundary.
        emit_failure_diagnostic(
            phase="UNKNOWN",
            operation="OTHER",
            error_class="OTHER",
            page_closed=page.is_closed(),
            context_closed=None,
            console_output_policy=console_output_policy,
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
    console_output_policy: ConsoleOutputPolicy = ConsoleOutputPolicy.INTERACTIVE,
) -> NurtureResult:
    if console_output_policy is ConsoleOutputPolicy.SCHEDULED and (
        detail or show_post_text or show_observed_text
    ):
        raise UiReadError("SCHEDULED_TEXT_OUTPUT_INVALID")
    if account.expected_actor is None:
        raise BrowserDataError("ACCOUNT_NOT_VERIFIED")
    if detail and not _detail_terminal_ready():
        raise UiReadError("DETAIL_TTY_REQUIRED")
    if max_runtime_seconds is not None and (
        not math.isfinite(max_runtime_seconds) or max_runtime_seconds <= 0
    ):
        raise UiReadError("RUNTIME_LIMIT_INVALID")
    return await _run_managed_browser_operation(
        account,
        checkpoint_store,
        operation=lambda page: _run_page_workflow_locked(
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
            console_output_policy=console_output_policy,
        ),
        max_runtime_seconds=max_runtime_seconds,
        timeout_code="CYCLE_TIMEOUT",
        reserve_cleanup=False,
        console_output_policy=console_output_policy,
    )


async def run_browser_nurture_session(
    account: BrowserAccount,
    checkpoint_store: CheckpointStore,
    *,
    max_posts: int,
    terms: tuple[str, ...],
    max_scrolls: int,
    max_total_posts: int,
    max_detail_reads: int,
    max_runtime_seconds: float,
    readiness_prompt: Callable[[str, int], Awaitable[bool]] | None = None,
    console_output_policy: ConsoleOutputPolicy = ConsoleOutputPolicy.INTERACTIVE,
    auto_mode: bool = False,
) -> NurtureResult:
    """Run a bounded multi-observation session under one headed Chromium context."""
    if console_output_policy is ConsoleOutputPolicy.SCHEDULED:
        raise UiReadError("SCHEDULED_SESSION_UNSUPPORTED")
    _validate_cycle(
        account,
        max_posts,
        max_scrolls,
        max_total_posts,
        terms,
        allow_empty_terms=auto_mode,
    )
    if not 1 <= max_detail_reads <= 40:
        raise UiReadError("DETAIL_READ_LIMIT_INVALID")
    if (
        not math.isfinite(max_runtime_seconds)
        or not 10 < max_runtime_seconds <= 3600
    ):
        raise UiReadError("RUNTIME_LIMIT_INVALID")
    if account.expected_actor is None:
        raise BrowserDataError("ACCOUNT_NOT_VERIFIED")

    session_options = {"auto_mode": True} if auto_mode else {}
    return await _run_managed_browser_operation(
        account,
        checkpoint_store,
        operation=lambda page: _run_page_workflow_session_locked(
            page,
            account,
            checkpoint_store,
            max_posts=max_posts,
            terms=terms,
            max_scrolls=max_scrolls,
            max_total_posts=max_total_posts,
            max_detail_reads=max_detail_reads,
            readiness_prompt=readiness_prompt,
            console_output_policy=console_output_policy,
            **session_options,
        ),
        max_runtime_seconds=max_runtime_seconds,
        timeout_code="SESSION_TIMEOUT",
        reserve_cleanup=True,
        console_output_policy=console_output_policy,
    )


async def run_browser_nurture_auto_cycle(
    account: BrowserAccount,
    checkpoint_store: CheckpointStore,
) -> NurtureResult:
    """Run one read-only Auto Nurture cycle through the managed browser lifecycle."""
    return await run_browser_nurture_session(
        account,
        checkpoint_store,
        max_posts=20,
        terms=(),
        max_scrolls=20,
        max_total_posts=100,
        max_detail_reads=5,
        max_runtime_seconds=600,
        readiness_prompt=None,
        console_output_policy=ConsoleOutputPolicy.AUTO,
        auto_mode=True,
    )


async def _run_managed_browser_operation(
    account: BrowserAccount,
    checkpoint_store: CheckpointStore,
    *,
    operation: Callable[[Page], Awaitable[NurtureResult]],
    max_runtime_seconds: float | None,
    timeout_code: str,
    reserve_cleanup: bool,
    console_output_policy: ConsoleOutputPolicy,
) -> NurtureResult:
    """Own the profile lock and one persistent Chromium context for an operation."""
    lock = checkpoint_store.profile_lock(account)
    lock.acquire()
    playwright_manager = None
    manager_start_attempted = False
    context = None
    context_launch_attempted = False
    session_armed = False
    timeout_scope = None
    operation_timeout = max_runtime_seconds
    context_cleanup_timeout = _CLEANUP_ALLOWANCE_SECONDS
    manager_cleanup_timeout = _CLEANUP_ALLOWANCE_SECONDS
    if reserve_cleanup and max_runtime_seconds is not None:
        context_cleanup_timeout, manager_cleanup_timeout = _session_cleanup_allowances(
            max_runtime_seconds
        )
        operation_timeout = (
            max_runtime_seconds - context_cleanup_timeout - manager_cleanup_timeout
        )
    try:
        ensure_safe_browser_profile(account.profile_directory)
        try:
            lock.arm_uncertain_session()
        except BrowserDataError:
            _emit_cleanup_failure(
                stage="QUARANTINE_LOCK",
                timed_out=False,
                console_output_policy=console_output_policy,
            )
            raise BrowserDataError("BROWSER_CLEANUP_FAILED") from None
        session_armed = True
        timeout_scope = asyncio.timeout(operation_timeout)
        try:
            async with timeout_scope:
                playwright_manager = async_playwright()
                manager_start_attempted = True
                playwright = await playwright_manager.__aenter__()
                context_launch_attempted = True
                context = await playwright.chromium.launch_persistent_context(
                    user_data_dir=str(account.profile_directory),
                    headless=False,
                )
                page = context.pages[0] if context.pages else await context.new_page()
                return await operation(page)
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
                console_output_policy=console_output_policy,
            )
            raise UiReadError(timeout_code) from None
    except (UiReadError, BrowserDataError):
        raise
    except PlaywrightError as error:
        failure = classify_playwright_failure(None, error)
        emit_failure_diagnostic(
            phase="INITIALIZING",
            operation="LAUNCH_SESSION",
            error_class=failure.error_class,
            page_closed=failure.page_closed,
            context_closed=None,
            console_output_policy=console_output_policy,
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
            console_output_policy=console_output_policy,
        )
        raise UiReadError("INTERNAL_OPERATION_ERROR") from None
    finally:
        operation_failed = sys.exc_info()[0] is not None
        cleanup_failed, cleanup_cancelled = await _cleanup_managed_browser_operation(
            lock,
            context=context,
            context_launch_attempted=context_launch_attempted,
            playwright_manager=playwright_manager,
            manager_start_attempted=manager_start_attempted,
            session_armed=session_armed,
            context_cleanup_timeout=context_cleanup_timeout,
            manager_cleanup_timeout=manager_cleanup_timeout,
            console_output_policy=console_output_policy,
        )
        if cleanup_failed:
            raise UiReadError("BROWSER_CLEANUP_FAILED") from None
        if cleanup_cancelled and not operation_failed:
            raise asyncio.CancelledError


async def _cleanup_managed_browser_operation(
    lock: ProcessLock,
    *,
    context: Any,
    context_launch_attempted: bool,
    playwright_manager: Any,
    manager_start_attempted: bool,
    session_armed: bool,
    context_cleanup_timeout: float,
    manager_cleanup_timeout: float,
    console_output_policy: ConsoleOutputPolicy,
) -> tuple[bool, bool]:
    """Close browser resources within fixed allowances, even when cancelled."""
    async def cleanup_resources() -> bool:
        cleanup_failed = False
        context_shutdown_verified = not context_launch_attempted
        manager_shutdown_verified = not manager_start_attempted
        try:
            if context is not None:
                try:
                    async with asyncio.timeout(context_cleanup_timeout):
                        await context.close()
                    context_shutdown_verified = True
                except TimeoutError:
                    cleanup_failed = True
                    _emit_cleanup_failure(
                        stage="CLOSE_CONTEXT",
                        timed_out=True,
                        console_output_policy=console_output_policy,
                    )
                except asyncio.CancelledError:
                    cleanup_failed = True
                    _emit_cleanup_failure(
                        stage="CLOSE_CONTEXT",
                        timed_out=False,
                        console_output_policy=console_output_policy,
                    )
                except Exception:  # noqa: BLE001 - sanitize browser cleanup failures.
                    cleanup_failed = True
                    _emit_cleanup_failure(
                        stage="CLOSE_CONTEXT",
                        timed_out=False,
                        console_output_policy=console_output_policy,
                    )
            elif context_launch_attempted:
                cleanup_failed = True
                _emit_cleanup_failure(
                    stage="CLOSE_CONTEXT",
                    timed_out=False,
                    console_output_policy=console_output_policy,
                )
            if manager_start_attempted:
                assert playwright_manager is not None
                try:
                    async with asyncio.timeout(manager_cleanup_timeout):
                        await playwright_manager.__aexit__(None, None, None)
                    manager_shutdown_verified = True
                except TimeoutError:
                    cleanup_failed = True
                    _emit_cleanup_failure(
                        stage="STOP_PLAYWRIGHT",
                        timed_out=True,
                        console_output_policy=console_output_policy,
                    )
                except asyncio.CancelledError:
                    cleanup_failed = True
                    _emit_cleanup_failure(
                        stage="STOP_PLAYWRIGHT",
                        timed_out=False,
                        console_output_policy=console_output_policy,
                    )
                except Exception:  # noqa: BLE001 - sanitize browser cleanup failures.
                    cleanup_failed = True
                    _emit_cleanup_failure(
                        stage="STOP_PLAYWRIGHT",
                        timed_out=False,
                        console_output_policy=console_output_policy,
                    )
            if (
                session_armed
                and not cleanup_failed
                and context_shutdown_verified
                and manager_shutdown_verified
            ):
                try:
                    lock.disarm_uncertain_session()
                except BrowserDataError:
                    cleanup_failed = True
                    _emit_cleanup_failure(
                        stage="QUARANTINE_LOCK",
                        timed_out=False,
                        console_output_policy=console_output_policy,
                    )
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
            return cleanup_failed, cleanup_cancelled
        except asyncio.CancelledError:
            current_task = asyncio.current_task()
            if current_task is None or current_task.cancelling() == 0:
                raise
            current_task.uncancel()
            cleanup_cancelled = True


def _emit_cleanup_failure(
    *,
    stage: str,
    timed_out: bool,
    console_output_policy: ConsoleOutputPolicy = ConsoleOutputPolicy.INTERACTIVE,
) -> None:
    emit_failure_diagnostic(
        phase="STOPPED",
        operation="OTHER",
        error_class="OTHER",
        error_kind="TIMEOUT" if timed_out else "OTHER",
        page_closed=None,
        context_closed=None,
        stage=stage,
        console_output_policy=console_output_policy,
    )
