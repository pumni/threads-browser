"""Human-driven login for a registered persistent Chromium profile."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Literal

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from .reader import HOME_URL, ThreadsHomeReader, UiReadError
from .storage import BrowserAccount, BrowserDataError, ensure_safe_browser_profile

_MAX_VERIFICATION_ATTEMPTS = 2
_NAVIGATION_TIMEOUT_MS = 20_000


@dataclass(frozen=True, slots=True)
class ManualLoginResult:
    status: Literal["VERIFIED", "CANCELLED", "FAILED"]
    error_code: str | None = None


async def run_manual_login(account: BrowserAccount) -> ManualLoginResult:
    """Open the registered profile and verify identity after human confirmation."""
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
                await page.goto(
                    HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=_NAVIGATION_TIMEOUT_MS,
                )

                print("LOGIN status=WAITING_FOR_USER")
                print("Open Threads in the Chromium window and log in manually.")
                print("Complete any required verification in the browser.")
                print("Press Enter after login is complete, or q to cancel.")

                last_error = "LOGIN_REQUIRED"
                for attempt in range(_MAX_VERIFICATION_ATTEMPTS):
                    try:
                        answer = await asyncio.to_thread(
                            input,
                            "Login verification > ",
                        )
                    except EOFError:
                        return ManualLoginResult("CANCELLED")
                    if answer.strip().casefold() in {"q", "quit"}:
                        return ManualLoginResult("CANCELLED")

                    try:
                        await ThreadsHomeReader(page, account.expected_actor).verify_login_actor()
                    except UiReadError as error:
                        last_error = error.code
                        if attempt + 1 < _MAX_VERIFICATION_ATTEMPTS:
                            print(f"STOP {error.code}", file=sys.stderr)
                            print(
                                f"LOGIN status=NOT_VERIFIED reason={error.code}",
                                file=sys.stderr,
                            )
                            print(
                                "Finish login or select the registered account in Chromium, "
                                "then press Enter to retry; enter q to cancel.",
                                file=sys.stderr,
                            )
                            continue
                        return ManualLoginResult("FAILED", error.code)
                    return ManualLoginResult("VERIFIED")
                return ManualLoginResult("FAILED", last_error)
            finally:
                if context is not None:
                    await context.close()
    except (UiReadError, BrowserDataError):
        raise
    except PlaywrightTimeoutError:
        raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
    except PlaywrightError:
        raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
    except (OSError, RuntimeError, ValueError):
        raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
