"""Visible login-session observation and actor verification."""

from __future__ import annotations

import asyncio
from typing import Protocol
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ..errors import UiReadError
from ..feed.relevance import normalize_text
from ..feed.urls import HOME_URL, _same_home_origin


class LoginNavigationHost(Protocol):
    _page: Page
    _navigation_timeout_ms: int

    def _is_home_navigation_url(self, value: str) -> bool: ...
    async def _check_blocker(self) -> None: ...
    async def _visible_profile_controls(self) -> list: ...
    async def _follow_profile_control(self, control, *, return_home: bool = True) -> str: ...
    async def _return_to_threads_home(self) -> None: ...


class LoginNavigator:
    """Observe the operator-owned login session without reading credentials."""

    def __init__(self, host: LoginNavigationHost) -> None:
        self._host = host

    async def verify_login_actor(self) -> str:
        """Verify the signed-in actor through Threads navigation and return Home."""
        if _same_home_origin(self._host._page.url):
            try:
                path = urlsplit(self._host._page.url).path.casefold()
            except ValueError:
                raise UiReadError("PROFILE_ROUTE_UNVERIFIED") from None
            if "challenge" in path or "checkpoint" in path:
                raise UiReadError("CHALLENGE_REQUIRED")
            if path in {"/login", "/accounts/login"}:
                raise UiReadError("LOGIN_REQUIRED")
            if any(marker in path for marker in ("rate_limit", "too_many_requests")):
                raise UiReadError("RATE_LIMITED")
        elif self._host._page.url != "about:blank":
            raise UiReadError("LOGIN_LOADING")

        if not self._host._is_home_navigation_url(self._host._page.url):
            try:
                await self._host._page.goto(
                    HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=self._host._navigation_timeout_ms,
                )
            except PlaywrightTimeoutError:
                raise UiReadError("HOME_NAVIGATION_TIMEOUT") from None

        if not _same_home_origin(self._host._page.url):
            raise UiReadError("OFF_ORIGIN_REDIRECT")
        try:
            path = urlsplit(self._host._page.url).path.casefold()
        except ValueError:
            raise UiReadError("PROFILE_ROUTE_UNVERIFIED") from None
        if "challenge" in path or "checkpoint" in path:
            raise UiReadError("CHALLENGE_REQUIRED")
        if path in {"/login", "/accounts/login"}:
            raise UiReadError("LOGIN_REQUIRED")
        if any(marker in path for marker in ("rate_limit", "too_many_requests")):
            raise UiReadError("RATE_LIMITED")
        await self._host._check_blocker()
        if not self._host._is_home_navigation_url(self._host._page.url):
            raise UiReadError("PROFILE_ROUTE_UNVERIFIED")

        controls = await self._host._visible_profile_controls()
        if not controls:
            body = self._host._page.locator("body")
            if await body.get_attribute("aria-busy") == "true" or not normalize_text(
                await body.inner_text()
            ):
                raise UiReadError("LOGIN_LOADING")
            raise UiReadError("LOGIN_UI_UNSUPPORTED")
        actor = await self._host._follow_profile_control(controls[0], return_home=False)
        await self._host._return_to_threads_home()
        return actor

    async def wait_for_login_control(self) -> None:
        """Wait indefinitely for Threads navigation, surviving document replacement."""
        predicate = """() => {
                    if (location.origin !== "https://www.threads.com") return false;
                    const labels = new Set(["profile", "your profile", "account"]);
                    const navs = document.querySelectorAll('nav, [role="navigation"]');
                    for (const nav of navs) {
                        const navStyle = getComputedStyle(nav);
                        const navBox = nav.getBoundingClientRect();
                        if (
                            navStyle.display === "none"
                            || navStyle.visibility === "hidden"
                            || navBox.width <= 0
                            || navBox.height <= 0
                        ) continue;
                        for (const control of nav.querySelectorAll(
                            'a, button, [role="link"], [role="button"]'
                        )) {
                            const label = (
                                control.getAttribute("aria-label")
                                || control.innerText
                                || control.textContent
                                || ""
                            ).trim().toLocaleLowerCase();
                            const style = getComputedStyle(control);
                            const box = control.getBoundingClientRect();
                            if (
                                labels.has(label)
                                && style.display !== "none"
                                && style.visibility !== "hidden"
                                && box.width > 0
                                && box.height > 0
                            ) return true;
                        }
                    }
                    return false;
                }"""
        while True:
            if self._host._page.is_closed():
                raise UiReadError("BROWSER_SESSION_UNAVAILABLE")
            try:
                await self._host._page.wait_for_function(
                    predicate,
                    polling=1000,
                    timeout=0,
                )
                return
            except PlaywrightTimeoutError:
                if self._host._page.is_closed():
                    raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
                try:
                    await self._host._page.wait_for_load_state(
                        "domcontentloaded",
                        timeout=self._host._navigation_timeout_ms,
                    )
                except PlaywrightTimeoutError:
                    continue
                await asyncio.sleep(0.25)
            except PlaywrightError as error:
                if self._host._page.is_closed():
                    raise UiReadError("BROWSER_SESSION_UNAVAILABLE") from None
                message = str(error).casefold()
                if not any(
                    marker in message
                    for marker in (
                        "execution context was destroyed",
                        "cannot find context with specified id",
                        "frame was detached",
                        "navigating frame was detached",
                    )
                ):
                    raise UiReadError("LOGIN_UI_UNSUPPORTED") from None
                try:
                    await self._host._page.wait_for_load_state(
                        "domcontentloaded",
                        timeout=self._host._navigation_timeout_ms,
                    )
                except PlaywrightTimeoutError:
                    continue
                await asyncio.sleep(0.25)
