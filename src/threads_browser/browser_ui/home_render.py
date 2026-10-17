"""Home and profile render-settlement observations."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar
from urllib.parse import urljoin

from playwright.async_api import Page

from ..console_output import ConsoleOutputPolicy, emit_console_diagnostic
from ..errors import UiReadError
from ..feed.urls import _profile_author, _same_home_origin
from .feed_rows import _FEED_ROOT_SELECTOR, _FeedScanCounts
from .loading import LoadingObserver
from .loading import RenderObservation as _RenderObservation

_ReadinessResult = TypeVar("_ReadinessResult")


@dataclass(slots=True)
class HomeRenderState:
    last_transition_at: float | None = None


class HomeRenderHost(Protocol):
    _page: Page
    _console_output_policy: ConsoleOutputPolicy
    _loading_observer: LoadingObserver
    _render_min_dwell_ms: int
    _render_settle_interval_ms: int
    _render_stable_observations: int
    last_scan_counts: _FeedScanCounts

    @property
    def _last_ui_transition_at(self) -> float | None: ...
    @_last_ui_transition_at.setter
    def _last_ui_transition_at(self, value: float | None) -> None: ...

    async def _recoverable_stage(
        self, stage: str, timeout_code: str, attempt, *, recoverable_codes=None, cancel_code=None
    ): ...
    def _remaining_readiness_ms(self, deadline: float, timeout_code: str) -> int: ...
    async def _require_feed_url(self, *, route_error_code: str = "HOME_UI_UNVERIFIED") -> None: ...
    async def _visible_profile_controls(self) -> list: ...
    async def _visible_for_you_controls(self) -> list: ...
    async def _for_you_is_active(self, control) -> bool: ...
    def _is_for_you_route(self, value: str) -> bool: ...
    async def _visible_feed_evidence(self) -> bool: ...
    def _report_render_settled(self, stage: str, observation, checks: int) -> None: ...


class HomeRenderObserver:
    """Own transient render signatures and stable-shell readiness checks."""

    def __init__(self, host: HomeRenderHost) -> None:
        self._host = host
        self.state = HomeRenderState()

    def mark_transition(self) -> None:
        self.state.last_transition_at = time.monotonic()

    async def _wait_for_home_render(
        self,
        stage: str,
        *,
        timeout_code: str,
        require_active_for_you: bool,
        require_feed: bool,
    ) -> None:
        async def observe_stage(deadline: float) -> None:
            await self._host._await_render_settled(
                stage,
                deadline,
                timeout_code=timeout_code,
                observe=lambda: self._host._observe_home_render(
                    deadline,
                    timeout_code=timeout_code,
                    require_active_for_you=require_active_for_you,
                    require_feed=require_feed,
                ),
            )

        await self._host._recoverable_stage(
            stage,
            timeout_code,
            observe_stage,
            recoverable_codes=(timeout_code,),
        )

    async def _observe_home_render(
        self,
        deadline: float,
        *,
        timeout_code: str,
        require_active_for_you: bool,
        require_feed: bool,
    ) -> _RenderObservation[None] | None:
        self._host._remaining_readiness_ms(deadline, timeout_code)
        try:
            await self._host._require_feed_url(route_error_code=timeout_code)
        except UiReadError as error:
            if error.code in {
                "PROFILE_CONTROL_NOT_READY",
                "FOR_YOU_CONTROL_NOT_READY",
                "FOR_YOU_STATE_CHANGED",
                timeout_code,
            }:
                return None
            raise

        profiles = await self._host._visible_profile_controls()
        for_you_controls = await self._host._visible_for_you_controls()
        if len(profiles) > 1:
            raise UiReadError("PROFILE_CONTROL_AMBIGUOUS")
        if len(for_you_controls) > 1:
            raise UiReadError("FOR_YOU_CONTROL_AMBIGUOUS")
        if not for_you_controls:
            return None

        profile_href = await profiles[0].get_attribute("href") if profiles else None
        if profile_href is not None:
            profile_destination = urljoin(self._host._page.url, profile_href)
            if not _same_home_origin(profile_destination):
                raise UiReadError("OFF_ORIGIN_REDIRECT")
            profile_actor = _profile_author(profile_destination)
            if profile_actor is not None and profile_actor != self._host._expected_actor:
                raise UiReadError("ACTOR_MISMATCH")
        for_you_href = await for_you_controls[0].get_attribute("href")
        if for_you_href is not None and not _same_home_origin(
            urljoin(self._host._page.url, for_you_href)
        ):
            raise UiReadError("OFF_ORIGIN_REDIRECT")
        active = await self._host._for_you_is_active(for_you_controls[0])
        on_for_you_route = self._host._is_for_you_route(self._host._page.url)
        if require_active_for_you and not (active or on_for_you_route):
            return None
        feed_ready = await self._host._visible_feed_evidence() if require_feed else False
        if require_feed and not feed_ready:
            return None

        dom_state = await self._host._page.evaluate(
            """rootSelector => {
                const body = document.body;
                const visibleCount = selector => Array.from(
                    document.querySelectorAll(selector)
                ).filter(element => {
                    const rect = element.getBoundingClientRect();
                    const style = getComputedStyle(element);
                    return rect.width > 0 && rect.height > 0 &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                }).length;
                return [
                    body ? body.childNodes.length : 0,
                    body ? body.childElementCount : 0,
                    visibleCount(rootSelector),
                    visibleCount('a[href*="/post/"]'),
                ];
            }""",
            _FEED_ROOT_SELECTOR,
        )
        if not dom_state[0]:
            return None
        signature = (
            self._host._page.url,
            profile_href,
            for_you_href,
            active,
            on_for_you_route,
            feed_ready,
            *dom_state,
        )
        return _RenderObservation(
            signature,
            None,
            ready_state="FEED_EVIDENCE" if require_feed else "UI_SHELL_SETTLED",
        )

    async def _observe_profile_render(
        self,
    ) -> _RenderObservation[None] | None:
        await self._host._verify_current_profile_route()
        dom_state = await self._host._page.evaluate(
            """() => {
                const body = document.body;
                const visibleCount = selector => Array.from(
                    document.querySelectorAll(selector)
                ).filter(element => {
                    const rect = element.getBoundingClientRect();
                    const style = getComputedStyle(element);
                    return rect.width > 0 && rect.height > 0 &&
                        style.visibility !== 'hidden' && style.display !== 'none';
                }).length;
                return [
                    body ? body.childNodes.length : 0,
                    body ? body.childElementCount : 0,
                    visibleCount('main, [role="main"], h1, [role="tab"]'),
                ];
            }"""
        )
        if not dom_state[0]:
            return None
        return _RenderObservation((self._host._page.url, *dom_state), None)

    async def _await_render_settled(
        self,
        stage: str,
        deadline: float,
        *,
        timeout_code: str,
        observe: Callable[[], Awaitable[_RenderObservation[_ReadinessResult] | None]],
        validate: Callable[[], Awaitable[None]] | None = None,
    ) -> _ReadinessResult:
        return await self._host._loading_observer.wait_for_render_settled(
            stage,
            deadline,
            timeout_code=timeout_code,
            observe=observe,
            remaining_ms=self._host._remaining_readiness_ms,
            transition_at=self._host._last_ui_transition_at,
            min_dwell_ms=self._host._render_min_dwell_ms,
            settle_interval_ms=self._host._render_settle_interval_ms,
            stable_observations=self._host._render_stable_observations,
            validate=validate,
            report=lambda observation, checks: self._host._report_render_settled(
                stage,
                observation,
                checks,
            ),
        )

    def _report_render_settled(
        self,
        stage: str,
        observation: _RenderObservation[object],
        checks: int,
    ) -> None:
        coverage_complete = str(getattr(observation.value, "coverage_complete", False)).lower()
        ready_state = observation.ready_state
        if ready_state == "FEED_VISIBLE_UNVERIFIED":
            emit_console_diagnostic(
                self._host._console_output_policy,
                f"OBSERVED stage={stage} state=FEED_VISIBLE_UNVERIFIED verified=0 "
                "identity=OBSERVED_UNVERIFIED "
                f"observed_rows={getattr(observation.value, 'observed_rows', 0)} "
                f"body_extracted={self._host.last_scan_counts.body_extracted} "
                f"viewport_coverage_complete={coverage_complete} render_checks={checks}",
            )
        elif ready_state == "OBSERVATION_UNCERTAIN":
            emit_console_diagnostic(
                self._host._console_output_policy,
                f"OBSERVED stage={stage} state=OBSERVATION_UNCERTAIN "
                f"verified={self._host.last_scan_counts.verified} "
                f"observed_rows={self._host.last_scan_counts.observed_rows} "
                f"uncertain_rows={self._host.last_scan_counts.uncertain_rows} "
                f"viewport_coverage_complete={coverage_complete} render_checks={checks}",
            )
        elif ready_state == "DISPLAY_OBSERVATION":
            emit_console_diagnostic(
                self._host._console_output_policy,
                f"OBSERVED stage={stage} state=DISPLAY_OBSERVATION "
                f"verified={self._host.last_scan_counts.verified} "
                f"observed_rows={self._host.last_scan_counts.observed_rows} "
                f"body_extracted={self._host.last_scan_counts.body_extracted} "
                f"viewport_coverage_complete={coverage_complete} render_checks={checks}",
            )
        elif ready_state == "BODY_EXTRACTED":
            emit_console_diagnostic(
                self._host._console_output_policy,
                f"READY stage={stage} state=BODY_EXTRACTED "
                f"verified={self._host.last_scan_counts.verified} "
                f"observed_rows={self._host.last_scan_counts.observed_rows} "
                f"body_extracted={self._host.last_scan_counts.body_extracted} "
                f"viewport_coverage_complete={coverage_complete} render_checks={checks}",
            )
        elif ready_state == "UI_SHELL_SETTLED":
            emit_console_diagnostic(
                self._host._console_output_policy,
                f"READY stage={stage} state=UI_SHELL_SETTLED render_checks={checks}",
            )
        elif ready_state == "FEED_EVIDENCE":
            emit_console_diagnostic(
                self._host._console_output_policy,
                f"OBSERVED stage={stage} state=FEED_EVIDENCE render_checks={checks}",
            )
        elif ready_state == "DETAIL_VERIFIED":
            emit_console_diagnostic(
                self._host._console_output_policy,
                f"READY stage={stage} state=DETAIL_VERIFIED render_checks={checks}",
            )
        else:
            emit_console_diagnostic(
                self._host._console_output_policy, f"READY stage={stage} render_checks={checks}"
            )
