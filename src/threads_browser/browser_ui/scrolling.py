"""Bounded native wheel gestures with measured scroll settlement."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from playwright.async_api import JSHandle, Page

MAX_WHEEL_STEP_PX = 48
_MAX_GESTURE_DISTANCE_PX = 360
_MAX_GESTURE_DURATION_MS = 1_800
_MAX_STEP_WAIT_MS = 350
_STABLE_FRAMES = 3


@dataclass(frozen=True, slots=True)
class ScrollGestureResult:
    requested_delta: int
    observed_delta: float
    wheel_deltas: tuple[int, ...]
    duration_ms: int


class NativeWheelScrollEngine:
    """Issue small native wheel inputs and wait for each scroll to settle."""

    def __init__(
        self,
        page: Page,
        *,
        max_duration_ms: int = _MAX_GESTURE_DURATION_MS,
    ) -> None:
        self._page = page
        self._max_duration_ms = max_duration_ms

    async def advance(
        self,
        surface: JSHandle,
        point: tuple[float, float],
        requested_delta: int,
        viewport_height: float,
    ) -> ScrollGestureResult:
        if requested_delta == 0 or viewport_height <= 0:
            return ScrollGestureResult(requested_delta, 0.0, (), 0)

        distance_limit = min(
            abs(requested_delta),
            _MAX_GESTURE_DISTANCE_PX,
            max(1, math.floor(viewport_height * 0.5)),
        )
        bounded_delta = distance_limit if requested_delta > 0 else -distance_limit
        wheel_deltas = tuple(self._split_delta(bounded_delta))
        started = time.monotonic()
        deadline = started + self._max_duration_ms / 1000
        initial_position = await self._position(surface)
        current_position = initial_position
        await self._page.mouse.move(*point)

        issued: list[int] = []
        for wheel_delta in wheel_deltas:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            issued.append(wheel_delta)
            await self._page.mouse.wheel(0, wheel_delta)
            wait_result = await surface.evaluate(
                """async (element, args) => {
                    const position = () => element === document.scrollingElement
                        ? window.scrollY : element.scrollTop;
                    return await new Promise(resolve => {
                        const started = performance.now();
                        let last = position();
                        let moved = Math.abs(last - args.initialPosition) > 1;
                        let stableFrames = 0;
                        const sample = () => {
                            const current = position();
                            if (Math.abs(current - args.initialPosition) > 1) {
                                moved = true;
                            }
                            if (Math.abs(current - last) > 0.5) {
                                last = current;
                                stableFrames = 0;
                            } else if (moved) {
                                stableFrames += 1;
                            }
                            const elapsed = performance.now() - started;
                            if (moved && stableFrames >= args.stableFrames) {
                                resolve({position: current, moved, stable: true});
                            } else if (elapsed >= args.timeoutMs) {
                                resolve({position: current, moved, stable: false});
                            } else {
                                requestAnimationFrame(sample);
                            }
                        };
                        requestAnimationFrame(sample);
                    });
                }""",
                {
                    "initialPosition": current_position,
                    "stableFrames": _STABLE_FRAMES,
                    "timeoutMs": min(_MAX_STEP_WAIT_MS, remaining_ms),
                },
            )
            current_position = float(wait_result["position"])
            if not wait_result["moved"] or not wait_result["stable"]:
                break
            observed = abs(current_position - initial_position)
            if observed >= distance_limit - 1:
                break

        observed_delta = abs(current_position - initial_position)
        return ScrollGestureResult(
            requested_delta=bounded_delta,
            observed_delta=observed_delta,
            wheel_deltas=tuple(issued),
            duration_ms=round((time.monotonic() - started) * 1000),
        )

    async def _position(self, surface: JSHandle) -> float:
        return float(
            await surface.evaluate(
                "element => element === document.scrollingElement"
                " ? window.scrollY : element.scrollTop"
            )
        )

    @staticmethod
    def _split_delta(delta: int) -> tuple[int, ...]:
        remaining = delta
        steps: list[int] = []
        while remaining:
            step = max(-MAX_WHEEL_STEP_PX, min(MAX_WHEEL_STEP_PX, remaining))
            steps.append(step)
            remaining -= step
        return tuple(steps)
