"""Compatibility API for the bounded Home / For You workflow."""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, async_playwright

from .browser_ui.session import classify_playwright_failure, emit_failure_diagnostic
from .nurture.diagnostics import _terminal_safe_post_text
from .nurture.engine import NurtureResult
from .nurture.engine import run_page_workflow as _execute_page_workflow
from .nurture.state import InvalidWorkflowTransition, policy_fingerprint
from .reader import ThreadsHomeReader, UiReadError
from .storage import BrowserAccount, BrowserDataError, CheckpointStore, ensure_safe_browser_profile

__all__ = [
    "NurtureResult",
    "_terminal_safe_post_text",
    "policy_fingerprint",
    "run_browser_nurture",
    "run_page_workflow",
]


def _detail_terminal_ready() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, OSError, ValueError):
        return False


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
) -> NurtureResult:
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
    readiness_prompt: Callable[[str, int], Awaitable[bool]] | None = None,
    show_post_text: bool = False,
    show_observed_text: bool = False,
    detail: bool = False,
) -> NurtureResult:
    if account.expected_actor is None:
        raise BrowserDataError("ACCOUNT_NOT_VERIFIED")
    if detail and not _detail_terminal_ready():
        raise UiReadError("DETAIL_TTY_REQUIRED")
    ensure_safe_browser_profile(account.profile_directory)
    try:
        async with async_playwright() as playwright:
            try:
                context = await playwright.chromium.launch_persistent_context(
                    user_data_dir=str(account.profile_directory),
                    headless=False,
                )
            except PlaywrightError as error:
                failure = classify_playwright_failure(None, error)
                emit_failure_diagnostic(
                    phase="INITIALIZING",
                    operation="LAUNCH_SESSION",
                    error_class=failure.error_class,
                    page_closed=failure.page_closed,
                    context_closed=failure.context_closed,
                )
                raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
            except OSError:
                emit_failure_diagnostic(
                    phase="INITIALIZING",
                    operation="LAUNCH_SESSION",
                    error_class="OTHER",
                    page_closed=False,
                    context_closed=None,
                )
                raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                return await run_page_workflow(
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
            finally:
                try:
                    await context.close()
                except PlaywrightError:
                    pass
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
