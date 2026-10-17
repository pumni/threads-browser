"""Observe bounded browser rendering and feed hydration progress."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page, expect

from ..console_output import (
    ConsoleOutputPolicy,
    emit_console_diagnostic,
)
from ..errors import UiReadError


@dataclass(frozen=True, slots=True)
class CardWaitSignal:
    locator: Locator
    condition: Literal["visible", "in_viewport", "nonempty_text", "retry"]
    timeout_code: str | None = None


class CardNotReady(Exception):
    def __init__(
        self,
        locator: Locator | None = None,
        *,
        in_viewport: bool = False,
        nonempty_text: bool = False,
        timeout_code: str | None = None,
        condition: Literal["visible", "retry"] = "visible",
        signals: tuple[CardWaitSignal, ...] | None = None,
        reason_code: str = "READINESS_SIGNAL_PENDING",
    ) -> None:
        if signals is None:
            if locator is None:
                raise ValueError("card readiness requires a locator")
            if in_viewport:
                condition = "in_viewport"
            elif nonempty_text:
                condition = "nonempty_text"
            signals = (CardWaitSignal(locator, condition, timeout_code),)
        self.signals = signals
        self.timeout_code = timeout_code or next(
            (signal.timeout_code for signal in signals if signal.timeout_code),
            None,
        )
        self.reason_code = reason_code
        super().__init__()

    @classmethod
    def combine(cls, pending: list[CardNotReady]) -> CardNotReady:
        return cls(signals=tuple(signal for item in pending for signal in item.signals))


@dataclass(frozen=True, slots=True)
class RenderObservation[T]:
    signature: tuple[object, ...] = field(repr=False)
    value: T
    terminal_code: str | None = None
    ready_state: str | None = "READY"


class LoadingObserver:
    """Wait for visible DOM evidence to settle within explicit time bounds."""

    def __init__(
        self,
        page: Page,
        *,
        console_output_policy: ConsoleOutputPolicy = ConsoleOutputPolicy.INTERACTIVE,
    ) -> None:
        self._page = page
        self._console_output_policy = console_output_policy

    async def wait_for_render_settled[T](
        self,
        stage: str,
        deadline: float,
        *,
        timeout_code: str,
        observe: Callable[[], Awaitable[RenderObservation[T] | None]],
        remaining_ms: Callable[[float, str], int],
        transition_at: float | None,
        min_dwell_ms: int,
        settle_interval_ms: int,
        stable_observations: int,
        validate: Callable[[], Awaitable[None]] | None = None,
        report: Callable[[RenderObservation[T], int], None] | None = None,
    ) -> T:
        transition = transition_at or time.monotonic()
        dwell_until = transition + min_dwell_ms / 1000
        dwell_remaining = min(
            max(0.0, dwell_until - time.monotonic()),
            remaining_ms(deadline, timeout_code) / 1000,
        )
        if dwell_remaining:
            await self._page.wait_for_timeout(round(dwell_remaining * 1000))

        checks = 0
        stable_count = 0
        previous_signature: tuple[object, ...] | None = None
        waiting_reported = False
        while True:
            remaining_ms(deadline, timeout_code)
            observation = await observe()
            checks += 1
            if observation is None:
                if previous_signature is not None:
                    emit_console_diagnostic(self._console_output_policy, f"RESCAN stage={stage} reason=DOM_CHANGED_AFTER_SCAN")
                previous_signature = None
                stable_count = 0
                if not waiting_reported:
                    emit_console_diagnostic(self._console_output_policy, f"WAITING stage={stage} reason=RENDER_PENDING")
                    waiting_reported = True
            else:
                if observation.signature == previous_signature:
                    stable_count += 1
                else:
                    if previous_signature is not None:
                        emit_console_diagnostic(self._console_output_policy, f"RESCAN stage={stage} reason=DOM_CHANGED_AFTER_SCAN")
                    previous_signature = observation.signature
                    stable_count = 1
                if stable_count >= stable_observations:
                    if observation.terminal_code is not None:
                        raise UiReadError(observation.terminal_code)
                    if observation.ready_state is None:
                        if not waiting_reported:
                            emit_console_diagnostic(self._console_output_policy, f"WAITING stage={stage} reason=READABLE_FEED_NOT_CONFIRMED")
                            waiting_reported = True
                    else:
                        if validate is not None:
                            await validate()
                        else:
                            remaining_ms(deadline, timeout_code)
                    if report is not None:
                        report(observation, checks)
                    if observation.ready_state is not None:
                        return observation.value

            interval_ms = min(
                max(1, settle_interval_ms),
                remaining_ms(deadline, timeout_code),
            )
            await self._page.wait_for_timeout(interval_ms)

    async def wait_for_card_signal(
        self,
        pending: CardNotReady,
        deadline: float,
        *,
        recheck_interval_ms: int,
        max_wait_ms: int | None = None,
    ) -> bool:
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            return False
        wait_ms = min(remaining, max_wait_ms or recheck_interval_ms)

        async def wait_for_signal(signal: CardWaitSignal) -> bool:
            try:
                if signal.condition == "in_viewport":
                    await expect(signal.locator).to_be_in_viewport(timeout=wait_ms)
                elif signal.condition == "nonempty_text":
                    await expect(signal.locator).to_be_visible(timeout=wait_ms)
                    await expect(signal.locator).to_have_text(
                        re.compile(r"\S"),
                        timeout=wait_ms,
                    )
                elif signal.condition == "retry":
                    await expect(signal.locator).to_be_visible(timeout=wait_ms)
                else:
                    await expect(signal.locator).to_be_visible(timeout=wait_ms)
                return True
            except (AssertionError, PlaywrightError):
                return False

        tasks = {asyncio.create_task(wait_for_signal(signal)) for signal in pending.signals}
        completed, unfinished = await asyncio.wait(
            tasks,
            timeout=wait_ms / 1000,
            return_when=asyncio.FIRST_COMPLETED,
        )
        changed = any(task.result() for task in completed)
        for task in unfinished:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return changed

    async def wait_for_card_quiet(
        self,
        root: Locator,
        *,
        dwell_ms: int,
        timeout_ms: int,
    ) -> bool:
        return bool(
            await root.evaluate(
                """async (node, args) => {
                    const snapshot = () => JSON.stringify([
                        node.innerText,
                        Array.from(node.querySelectorAll("a"), link =>
                            link.getAttribute("href")),
                    ]);
                    let value = snapshot();
                    let changedAt = performance.now();
                    const observer = new MutationObserver(() => {
                        const current = snapshot();
                        if (current !== value) {
                            value = current;
                            changedAt = performance.now();
                        }
                    });
                    observer.observe(node, {
                        subtree: true,
                        childList: true,
                        characterData: true,
                        attributes: true,
                        attributeFilter: ["href"],
                    });
                    const deadline = performance.now() + args.timeoutMs;
                    while (
                        performance.now() - changedAt < args.dwellMs &&
                        performance.now() < deadline
                    ) {
                        await new Promise(resolve => setTimeout(resolve, 16));
                    }
                    observer.disconnect();
                    return performance.now() - changedAt >= args.dwellMs;
                }""",
                {"dwellMs": max(1, dwell_ms), "timeoutMs": max(1, timeout_ms)},
            )
        )
