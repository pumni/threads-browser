"""Open a verified feed Detail target using the configured click policy."""

from __future__ import annotations

from typing import Protocol

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from ..console_output import ConsoleOutputPolicy
from ..errors import UiReadError
from ..feed.job_intent import classify_auto_body
from ..feed.models import PostTarget


class _DetailClickHost(Protocol):
    _page: Page
    _detail_return_anchor: str | None
    _detail_return_target: PostTarget | None

    def _remaining_readiness_ms(self, deadline: float, timeout_code: str) -> int: ...
    def _mark_ui_transition(self) -> None: ...


class _DetailClickNavigator(Protocol):
    _host: _DetailClickHost
    _console_output_policy: ConsoleOutputPolicy

    async def _detail_click_point(self, permalink: Locator) -> tuple[float, float]: ...


async def click_detail_target(
    navigator: _DetailClickNavigator,
    target: PostTarget,
    deadline: float,
) -> None:
    """Recheck Auto eligibility, then open the visible target without scrolling."""
    host = navigator._host
    auto_nurture = navigator._console_output_policy is ConsoleOutputPolicy.AUTO
    if auto_nurture and classify_auto_body(target.body) != "RELEVANT":
        raise UiReadError("AUTO_TARGET_NOT_ELIGIBLE")

    click_point = await navigator._detail_click_point(target.permalink)
    host._detail_return_anchor = target.fingerprint
    host._detail_return_target = target
    host._remaining_readiness_ms(deadline, "DETAIL_NAVIGATION_UNVERIFIED")
    try:
        if auto_nurture:
            await target.permalink.click(
                scroll="none",
                timeout=host._remaining_readiness_ms(
                    deadline,
                    "DETAIL_NAVIGATION_UNVERIFIED",
                ),
            )
        else:
            await host._page.mouse.click(*click_point)
    except PlaywrightError:
        raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED") from None
    host._mark_ui_transition()
