"""Human-driven login for a registered persistent Chromium profile."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from playwright.async_api import BrowserContext, Page, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .errors import UiReadError
from .feed.urls import HOME_URL
from .reader import ThreadsHomeReader
from .storage import (
    AccountStore,
    BrowserAccount,
    BrowserDataError,
    ensure_safe_browser_profile,
)

_WAITABLE_LOGIN_CODES = {
    "LOGIN_REQUIRED",
    "LOGIN_LOADING",
    "CHALLENGE_REQUIRED",
    "HOME_NAVIGATION_TIMEOUT",
}
LoginStatus = Literal[
    "WAITING_FOR_AUTHENTICATION",
    "SESSION_REUSED",
    "VERIFIED",
    "BROWSER_OPEN",
]


@dataclass(frozen=True, slots=True)
class ManualLoginResult:
    status: Literal["CLOSED", "CANCELLED"]


async def _wait_for_authentication(
    page: Page,
    context: BrowserContext,
    expected_actor: str | None,
) -> bool:
    """Wait for the visible profile control or for the operator to close Chromium."""
    reader = ThreadsHomeReader(page, expected_actor)
    readiness = asyncio.create_task(reader.wait_for_login_control())
    closed = asyncio.create_task(context.wait_for_event("close"))
    try:
        done, _ = await asyncio.wait(
            (readiness, closed),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if closed in done or context.is_closed():
            return False
        try:
            await readiness
        except PlaywrightError:
            if context.is_closed():
                return False
            raise
        return not context.is_closed()
    finally:
        for task in (readiness, closed):
            if not task.done():
                task.cancel()
        await asyncio.gather(readiness, closed, return_exceptions=True)


async def _verify_actor(page: Page, expected_actor: str | None, context: BrowserContext) -> str:
    try:
        return await ThreadsHomeReader(page, expected_actor).verify_login_actor()
    except PlaywrightTimeoutError:
        if context.is_closed():
            raise
        raise UiReadError("LOGIN_LOADING") from None
    except PlaywrightError as error:
        if context.is_closed():
            raise
        message = str(error).casefold()
        if any(
            marker in message
            for marker in (
                "execution context was destroyed",
                "cannot find context with specified id",
                "frame was detached",
                "navigating frame was detached",
            )
        ):
            raise UiReadError("LOGIN_LOADING") from None
        raise


async def run_manual_login(
    account: BrowserAccount,
    accounts: AccountStore,
    *,
    status_callback: Callable[[LoginStatus], None] | None = None,
) -> ManualLoginResult:
    """Open Chromium, verify or establish the actor binding, and wait for window close."""
    ensure_safe_browser_profile(account.profile_directory)
    context = None
    try:
        async with async_playwright() as playwright:
            try:
                context = await playwright.chromium.launch_persistent_context(
                    user_data_dir=str(account.profile_directory),
                    headless=False,
                )
                page = context.pages[0] if context.pages else await context.new_page()
                session_reused = False
                try:
                    await page.goto(
                        HOME_URL,
                        wait_until="domcontentloaded",
                        timeout=20_000,
                    )
                except PlaywrightTimeoutError:
                    pass

                try:
                    actor = await _verify_actor(
                        page,
                        account.expected_actor,
                        context,
                    )
                    session_reused = account.expected_actor is not None
                except UiReadError as error:
                    if error.code not in _WAITABLE_LOGIN_CODES:
                        raise
                    if status_callback is not None:
                        status_callback("WAITING_FOR_AUTHENTICATION")
                    while True:
                        if not await _wait_for_authentication(
                            page,
                            context,
                            account.expected_actor,
                        ):
                            return ManualLoginResult("CANCELLED")
                        try:
                            actor = await _verify_actor(
                                page,
                                account.expected_actor,
                                context,
                            )
                        except UiReadError as retry_error:
                            if retry_error.code in _WAITABLE_LOGIN_CODES:
                                continue
                            raise
                        break

                try:
                    accounts.bind_actor(account, actor)
                except BrowserDataError as error:
                    if error.code == "ACTOR_MISMATCH":
                        raise UiReadError("ACTOR_MISMATCH") from None
                    raise

                if session_reused and status_callback is not None:
                    status_callback("SESSION_REUSED")
                if status_callback is not None:
                    status_callback("VERIFIED")
                    status_callback("BROWSER_OPEN")
                await context.wait_for_event("close")
                return ManualLoginResult("CLOSED")
            except PlaywrightTimeoutError:
                if context is not None and context.is_closed():
                    return ManualLoginResult("CANCELLED")
                raise UiReadError("HOME_NAVIGATION_TIMEOUT") from None
            except PlaywrightError:
                if context is not None and context.is_closed():
                    return ManualLoginResult("CANCELLED")
                raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
            finally:
                if context is not None and not context.is_closed():
                    await context.close()
    except (UiReadError, BrowserDataError):
        raise
    except (OSError, RuntimeError, ValueError):
        raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
