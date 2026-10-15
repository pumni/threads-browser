"""Exact post Detail navigation and safe return to the verified feed."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    Locator,
    Page,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from ..errors import UiReadError
from ..feed.models import PostTarget
from ..feed.relevance import normalize_text
from ..feed.urls import _post_url, _post_url_hint, _same_home_origin
from .loading import CardNotReady as _CardNotReady
from .loading import RenderObservation as _RenderObservation


class DetailReaderHost(Protocol):
    _page: Page
    _detail_return_anchor: tuple[str, float] | None
    _detail_return_target: PostTarget | None

    async def _is_in_visible_scrollports(self, element: Locator) -> bool: ...
    def _readiness_deadline(self, timeout_ms: int) -> float: ...
    async def _wait_for_verified_feed(self, deadline: float, *, timeout_code: str) -> None: ...
    def _remaining_readiness_ms(self, deadline: float, timeout_code: str) -> int: ...
    def _mark_ui_transition(self) -> None: ...
    async def _check_blocker(self) -> None: ...
    async def _read_card(self, root: Locator, *, require_body: bool = False) -> PostTarget: ...
    async def _wait_for_pending_card(self, pending: _CardNotReady, deadline: float) -> None: ...
    async def _await_render_settled(
        self,
        stage: str,
        deadline: float,
        *,
        timeout_code: str,
        observe: Callable[[], Awaitable[Any]],
    ) -> Any: ...
    async def _recoverable_stage(
        self,
        stage: str,
        timeout_code: str,
        attempt: Callable[[float], Awaitable[Any]],
        *,
        recoverable_codes: tuple[str, ...] = (),
    ) -> Any: ...
    async def _content_region(
        self,
        *,
        prefer_main: bool = False,
        error_code: str = "FEED_REGION_AMBIGUOUS",
    ) -> Locator: ...
    async def _is_outside_content_landmark(self, link: Locator) -> bool: ...
    async def _resolve_permalink_card(
        self, permalink: Locator, expected_url: str, expected_author: str
    ) -> tuple[Locator, Any]: ...
    async def _go_back_to_home(self) -> None: ...
    async def _revalidate_action_target(self, target: PostTarget) -> PostTarget: ...


class DetailNavigator:
    """Perform bounded Detail and Back actions through a reader-owned UI port."""

    def __init__(
        self,
        host: DetailReaderHost,
        *,
        timeout_ms: int,
        max_permalink_candidates: int,
    ) -> None:
        self._host = host
        self._timeout_ms = timeout_ms
        self._max_permalink_candidates = max_permalink_candidates

    async def _detail_click_point(self, permalink: Locator) -> tuple[float, float]:
        viewport = self._host._page.viewport_size
        box = await permalink.bounding_box()
        if (
            viewport is None
            or box is None
            or box["width"] <= 0
            or box["height"] <= 0
            or not await self._host._is_in_visible_scrollports(permalink)
        ):
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")
        point = (
            box["x"] + box["width"] / 2,
            box["y"] + box["height"] / 2,
        )
        if not (0 <= point[0] < viewport["width"] and 0 <= point[1] < viewport["height"]):
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")
        try:
            hit_is_permalink = await permalink.evaluate(
                """(node, point) => {
                    const hit = document.elementFromPoint(point.x, point.y);
                    return Boolean(hit && (hit === node || node.contains(hit)));
                }""",
                {"x": point[0], "y": point[1]},
            )
        except PlaywrightError:
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED") from None
        if not hit_is_permalink:
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")
        return point

    async def actionable_now(self, target: PostTarget) -> PostTarget:
        """Return a freshly verified Home target whose permalink can be clicked now."""
        if not target.currently_visible:
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")
        current = await self._host._revalidate_action_target(target)
        await self._detail_click_point(current.permalink)
        return current

    async def _read_detail(self, target: PostTarget) -> PostTarget:
        deadline = self._host._readiness_deadline(self._timeout_ms)
        try:

            def is_target_url(value: str) -> bool:
                parsed = _post_url(value)
                return parsed is not None and parsed == (target.url, target.author)

            await self._host._wait_for_verified_feed(
                deadline,
                timeout_code="DETAIL_NAVIGATION_UNVERIFIED",
            )
            current = await self.actionable_now(target)
            click_point = await self._detail_click_point(current.permalink)
            self._host._remaining_readiness_ms(
                deadline,
                "DETAIL_NAVIGATION_UNVERIFIED",
            )
            # Page.mouse.click sends input at the already-visible anchor point
            # without Playwright's locator auto-scroll behavior.
            try:
                await self._host._page.mouse.click(*click_point)
            except PlaywrightError:
                raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED") from None
            self._host._mark_ui_transition()
            await self._host._page.wait_for_url(
                is_target_url,
                timeout=self._host._remaining_readiness_ms(
                    deadline,
                    "DETAIL_NAVIGATION_UNVERIFIED",
                ),
            )
        except PlaywrightTimeoutError:
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED") from None
        if not _same_home_origin(self._host._page.url):
            raise UiReadError("OFF_ORIGIN_REDIRECT")

        async def read_detail_window(window_deadline: float) -> PostTarget:
            last_timeout_code = "DETAIL_ROOT_UNVERIFIED"

            async def observe() -> _RenderObservation[PostTarget] | None:
                nonlocal last_timeout_code
                self._host._remaining_readiness_ms(
                    window_deadline,
                    last_timeout_code,
                )
                if not _same_home_origin(self._host._page.url):
                    raise UiReadError("OFF_ORIGIN_REDIRECT")
                if not is_target_url(self._host._page.url):
                    await self._host._check_blocker()
                    raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")
                await self._host._check_blocker()
                try:
                    articles = self._host._page.get_by_role("article")
                    matching: list[Locator] = []
                    for index in range(await articles.count()):
                        article = articles.nth(index)
                        for link in await article.get_by_role("link").all():
                            href = await link.get_attribute("href")
                            parsed = None if href is None else _post_url(href)
                            if parsed is not None and parsed[0] == target.url:
                                matching.append(article)
                                break
                            if href is not None and _post_url_hint(href) and parsed is None:
                                raise UiReadError("DETAIL_PROVENANCE_UNVERIFIED")
                    if len(matching) > 1:
                        raise UiReadError("DETAIL_ROOT_AMBIGUOUS")
                    root = matching[0] if matching else await self._detail_permalink_card(target)
                    detail = await self._host._read_card(
                        root,
                        require_body=target.body is not None,
                    )
                    self._host._remaining_readiness_ms(
                        window_deadline,
                        last_timeout_code,
                    )
                    if not detail.body_available:
                        last_timeout_code = "DETAIL_BODY_UNVERIFIED"
                        return None
                    signature = (
                        detail.url,
                        detail.author,
                        detail.body,
                        detail.body_available,
                        len(matching),
                    )
                    return _RenderObservation(
                        signature,
                        detail,
                        ready_state="DETAIL_VERIFIED",
                    )
                except _CardNotReady as pending:
                    last_timeout_code = pending.timeout_code or "DETAIL_ROOT_UNVERIFIED"
                    await self._host._wait_for_pending_card(pending, window_deadline)
                    return None

            try:
                return await self._host._await_render_settled(
                    "DETAIL",
                    window_deadline,
                    timeout_code="DETAIL_ROOT_UNVERIFIED",
                    observe=observe,
                )
            except UiReadError as error:
                if (
                    error.code == "DETAIL_ROOT_UNVERIFIED"
                    and last_timeout_code != "DETAIL_ROOT_UNVERIFIED"
                ):
                    raise UiReadError(last_timeout_code) from None
                raise

        detail = await self._host._recoverable_stage(
            "DETAIL",
            "DETAIL_ROOT_UNVERIFIED",
            read_detail_window,
            recoverable_codes=(
                "DETAIL_ROOT_UNVERIFIED",
                "DETAIL_BODY_UNVERIFIED",
            ),
        )
        if detail.url != target.url or detail.author != target.author:
            raise UiReadError("DETAIL_PROVENANCE_UNVERIFIED")
        if detail.body is None or target.body is None:
            raise UiReadError("DETAIL_BODY_UNVERIFIED")
        home_body = " ".join(normalize_text(target.body).split())
        detail_body = " ".join(normalize_text(detail.body).split())
        body_matches = detail_body == home_body
        prefix = home_body
        if target.body_truncated and not body_matches:
            prefix = home_body.rstrip(" .…")
            body_matches = bool(prefix) and detail_body.startswith(prefix)
        if not body_matches:
            raise UiReadError("DETAIL_BODY_UNVERIFIED")
        self._host._detail_return_anchor = (target.fingerprint, target.viewport_y)
        self._host._detail_return_target = target
        return detail

    async def _detail_permalink_card(self, target: PostTarget) -> Locator:
        region = await self._host._content_region(
            prefer_main=True,
            error_code="DETAIL_REGION_AMBIGUOUS",
        )
        permalinks = region.locator('a[href*="/post/"]')
        scan_limit = min(await permalinks.count(), self._max_permalink_candidates)
        for index in range(scan_limit):
            permalink = permalinks.nth(index)
            if not await permalink.is_visible():
                continue
            if await self._host._is_outside_content_landmark(permalink):
                continue
            href = await permalink.get_attribute("href")
            parsed_post = None if href is None else _post_url(href)
            if parsed_post is None:
                if href is not None and _post_url_hint(href):
                    raise UiReadError("DETAIL_PROVENANCE_UNVERIFIED")
                continue
            if parsed_post[0] != target.url:
                continue
            root, _ = await self._host._resolve_permalink_card(
                permalink,
                target.url,
                target.author,
            )
            return root
        path = urlsplit(target.url).path
        matching_permalink = region.locator(f'a[href$="{path}"]')
        raise _CardNotReady(matching_permalink.first, in_viewport=True)

    async def return_to_home(self) -> None:
        await self._host._go_back_to_home()
        anchor = self._host._detail_return_anchor
        target = self._host._detail_return_target
        if anchor is None or target is None:
            raise UiReadError("RETURN_CONTEXT_CHANGED")
        try:
            current = await self._host._revalidate_action_target(target)
        except UiReadError:
            raise UiReadError("RETURN_CONTEXT_CHANGED") from None
        if current.fingerprint != anchor[0] or abs(current.viewport_y - anchor[1]) > 48:
            raise UiReadError("RETURN_CONTEXT_CHANGED")
