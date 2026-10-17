"""Navigation compatibility methods retained by the reader facade."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Literal, TypeVar

from playwright.async_api import (
    Locator,
)

from ..browser_ui.body_text import (
    _MAX_BODY_CODEPOINTS,  # noqa: F401 - retained reader-module compatibility name
    _canonicalize_body,  # noqa: F401 - retained reader-module compatibility name
    _canonicalize_body_block,  # noqa: F401 - retained reader-module compatibility name
    )
from ..browser_ui.feed_rows import (
    _MAX_CARD_LINKS,  # noqa: F401 - retained reader-module compatibility name
    )
from ..browser_ui.loading import (
    RenderObservation as _RenderObservation,
)
from ..browser_ui.session import (
    HomeSession,
)
from ..errors import UiReadError
from ..feed.urls import (
    _rejected_post_link_blocks_candidate,  # noqa: F401 - retained reader-module compatibility name
)

_ReadinessResult = TypeVar("_ReadinessResult")


class ReaderNavigationCompatibility:
    """Compatibility delegates grouped by navigation responsibility."""

    async def open_home(self) -> None:
        await self._home_navigation.open_home()

    async def verify_actor(self) -> None:
        await self._home_navigation.verify_actor()

    async def verify_login_actor(self) -> str:
        return await self._login_navigation.verify_login_actor()

    async def wait_for_login_control(self) -> None:
        await self._login_navigation.wait_for_login_control()

    async def _visible_profile_controls(self) -> list[Locator]:
        return await self._home_navigation._visible_profile_controls()

    async def _visible_navigation_controls(
        self,
        role: Literal["link", "button"],
    ) -> list[Locator]:
        return await self._home_navigation._visible_navigation_controls(role)

    async def _visible_navigation_profile_text(self) -> list[Locator]:
        return await self._home_navigation._visible_navigation_profile_text()

    async def _follow_profile_control(
        self,
        control: Locator,
        *,
        return_home: bool = True,
    ) -> str:
        return await self._home_navigation._follow_profile_control(
            control,
            return_home=return_home,
        )

    async def _verify_current_profile_route(self) -> str:
        return await self._home_navigation._verify_current_profile_route()

    async def _go_back_to_home(self) -> None:
        await self._home_navigation._go_back_to_home()

    async def _return_to_threads_home(self) -> None:
        await self._home_navigation._return_to_threads_home()

    @staticmethod
    def _readiness_deadline(timeout_ms: int) -> float:
        return time.monotonic() + timeout_ms / 1000

    @staticmethod
    def _remaining_readiness_ms(deadline: float, timeout_code: str) -> int:
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise UiReadError(timeout_code)
        return remaining

    async def _recoverable_stage(
        self,
        stage: str,
        timeout_code: str,
        attempt: Callable[[float], Awaitable[_ReadinessResult]],
        *,
        recoverable_codes: tuple[str, ...] | None = None,
        operation: str | None = None,
        cancel_code: str | None = None,
    ) -> _ReadinessResult:
        return await self._recovery.run_stage(
            stage,
            timeout_code,
            attempt,
            recoverable_codes=recoverable_codes,
            operation=operation,
            cancel_code=cancel_code,
        )

    async def _wait_for_home_render(
        self,
        stage: str,
        *,
        timeout_code: str,
        require_active_for_you: bool,
        require_feed: bool,
    ) -> None:
        await self._home_render._wait_for_home_render(
            stage,
            timeout_code=timeout_code,
            require_active_for_you=require_active_for_you,
            require_feed=require_feed,
        )

    async def _observe_home_render(
        self,
        deadline: float,
        *,
        timeout_code: str,
        require_active_for_you: bool,
        require_feed: bool,
    ) -> _RenderObservation[None] | None:
        return await self._home_render._observe_home_render(
            deadline,
            timeout_code=timeout_code,
            require_active_for_you=require_active_for_you,
            require_feed=require_feed,
        )

    async def _observe_profile_render(self) -> _RenderObservation[None] | None:
        return await self._home_render._observe_profile_render()

    async def _await_render_settled(
        self,
        stage: str,
        deadline: float,
        *,
        timeout_code: str,
        observe: Callable[[], Awaitable[_RenderObservation[_ReadinessResult] | None]],
        validate: Callable[[], Awaitable[None]] | None = None,
    ) -> _ReadinessResult:
        return await self._home_render._await_render_settled(
            stage,
            deadline,
            timeout_code=timeout_code,
            observe=observe,
            validate=validate,
        )

    def _report_render_settled(
        self,
        stage: str,
        observation: _RenderObservation[object],
        checks: int,
    ) -> None:
        self._home_render._report_render_settled(stage, observation, checks)

    async def _for_you_control(self, *, timeout_ms: int | None = None) -> Locator:
        return await self._home_session._for_you_control(timeout_ms=timeout_ms)

    async def _visible_for_you_controls(self) -> list[Locator]:
        return await self._home_session._visible_for_you_controls()

    async def _for_you_is_active(self, control: Locator) -> bool:
        return await self._home_session._for_you_is_active(control)

    @staticmethod
    def _is_for_you_route(value: str) -> bool:
        return HomeSession._is_for_you_route(value)

    @staticmethod
    def _is_home_navigation_url(value: str) -> bool:
        return HomeSession._is_home_navigation_url(value)

    async def _require_feed_url(
        self,
        *,
        route_error_code: str = "HOME_UI_UNVERIFIED",
    ) -> None:
        await self._home_session._require_feed_url(route_error_code=route_error_code)

    async def _wait_for_verified_feed(
        self,
        deadline: float,
        *,
        timeout_code: str,
        route_error_code: str = "HOME_UI_UNVERIFIED",
    ) -> None:
        await self._home_session._wait_for_verified_feed(
            deadline,
            timeout_code=timeout_code,
            route_error_code=route_error_code,
        )

    async def _check_blocker(self) -> None:
        await self._home_session._check_blocker()
