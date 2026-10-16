"""Exact post Detail navigation and safe return to the verified feed."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256
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
from ..feed.urls import _post_url, _post_url_hint, _profile_author, _same_home_origin
from .body_text import _VisibleBodyRead
from .loading import CardNotReady as _CardNotReady
from .loading import RenderObservation as _RenderObservation

_DETAIL_ROOT_SELECTOR = 'article, [role="article"], [data-pressable-container="true"]'
_DETAIL_QUOTE_SELECTOR = 'blockquote, [role="quote"]'
_DETAIL_PENDING_SELECTOR = (
    '[aria-busy="true"], [role="progressbar"], [data-loading="true"], '
    '[data-testid*="skeleton" i]'
)
_MAX_DETAIL_ROOTS = 40
_MAX_DETAIL_LINKS = 40


@dataclass(frozen=True, slots=True)
class _DetailState:
    route_verified: bool = False
    same_origin: bool = False
    main_regions_count: int = 0
    article_roots: int = 0
    pressable_roots: int = 0
    canonical_self_permalink_candidates: int = 0
    author_candidates: int = 0
    body_nodes_visible: int = 0
    matching_roots: int = 0
    hydration_pending: bool = False
    terminal_reason: str = "ROOT_ABSENT"


@dataclass(frozen=True, slots=True)
class _DetailRootRead:
    authors: frozenset[str]
    has_rejected_post_link: bool
    self_permalink_candidates: int
    body: _VisibleBodyRead
    body_matches: bool


def _detail_body_matches(target: PostTarget, body: str | None) -> bool:
    if body is None or target.body is None:
        return False
    home_body = " ".join(normalize_text(target.body).split())
    detail_body = " ".join(normalize_text(body).split())
    if detail_body == home_body:
        return True
    if not target.body_truncated:
        return False
    prefix = home_body.rstrip(" .…")
    return bool(prefix) and detail_body.startswith(prefix)


def _detail_error_code(reason: str) -> str:
    return {
        "ROOT_AMBIGUOUS": "DETAIL_ROOT_AMBIGUOUS",
        "SELF_LINK_REJECTED": "DETAIL_PROVENANCE_UNVERIFIED",
        "AUTHOR_UNVERIFIED": "DETAIL_PROVENANCE_UNVERIFIED",
        "AUTHOR_CONFLICT": "DETAIL_PROVENANCE_UNVERIFIED",
        "BODY_PENDING": "DETAIL_BODY_UNVERIFIED",
        "BODY_CHANGED": "DETAIL_BODY_UNVERIFIED",
        "BODY_UNVERIFIED": "DETAIL_BODY_UNVERIFIED",
        "STABILITY_PENDING": "DETAIL_BODY_UNVERIFIED",
    }.get(reason, "DETAIL_ROOT_UNVERIFIED")


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
    async def _read_detail_body(
        self, root: Locator, fingerprint: str
    ) -> _VisibleBodyRead: ...
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
        self._last_reported_detail_state: tuple[str, _DetailState] | None = None

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

    def _report_detail_state(self, phase: str, state: _DetailState) -> None:
        if self._last_reported_detail_state == (phase, state):
            return
        self._last_reported_detail_state = (phase, state)
        print(
            f"[DETAIL_STATE] phase={phase} "
            f"route_verified={str(state.route_verified).lower()} "
            f"same_origin={str(state.same_origin).lower()} "
            f"main_regions_count={state.main_regions_count} "
            f"article_roots={state.article_roots} "
            f"pressable_roots={state.pressable_roots} "
            "canonical_self_permalink_candidates="
            f"{state.canonical_self_permalink_candidates} "
            f"author_candidates={state.author_candidates} "
            f"body_nodes_visible={state.body_nodes_visible} "
            f"matching_roots={state.matching_roots} "
            f"hydration_pending={str(state.hydration_pending).lower()} "
            f"terminal_reason={state.terminal_reason}"
        )

    async def _detail_region(self) -> tuple[Locator | None, int]:
        for selector in ('main, [role="main"]', '[role="feed"]'):
            landmarks = self._host._page.locator(selector)
            visible: list[Locator] = []
            for index in range(await landmarks.count()):
                landmark = landmarks.nth(index)
                if await landmark.is_visible():
                    visible.append(landmark)
            if visible:
                return (visible[0] if len(visible) == 1 else None), len(visible)
        return None, 0

    async def _is_top_level_detail_root(self, root: Locator, region: Locator) -> bool:
        return bool(
            await root.evaluate(
                """(node, args) => {
                    if (!args.region.contains(node) || node.closest(args.quote)) return false;
                    for (let parent = node.parentElement;
                         parent && parent !== args.region; parent = parent.parentElement) {
                        if (parent.matches(args.roots)) return false;
                    }
                    return true;
                }""",
                {
                    "region": await region.element_handle(),
                    "quote": _DETAIL_QUOTE_SELECTOR,
                    "roots": _DETAIL_ROOT_SELECTOR,
                },
            )
        )

    async def _belongs_to_detail_root(
        self,
        link: Locator,
        root: Locator,
        root_handle: Any,
    ) -> bool:
        return bool(
            await link.evaluate(
                """(node, args) => {
                    if (node.closest(args.quote)) return false;
                    const nearest = node.parentElement?.closest(args.roots);
                    return args.root.matches(args.roots)
                        ? nearest === args.root
                        : args.root.contains(node) && !nearest;
                }""",
                {"root": root_handle, "quote": _DETAIL_QUOTE_SELECTOR,
                 "roots": _DETAIL_ROOT_SELECTOR},
            )
        )

    async def _has_pending_ui(self, region: Locator | None) -> bool:
        if await self._host._page.evaluate("document.readyState !== 'complete'"):
            return True
        if region is None:
            region = self._host._page.locator("body")
        if await region.get_attribute("aria-busy") == "true":
            return True
        signals = region.locator(_DETAIL_PENDING_SELECTOR)
        for index in range(min(await signals.count(), _MAX_DETAIL_LINKS)):
            signal = signals.nth(index)
            if await signal.get_attribute("aria-busy") == "true":
                return True
            if await signal.is_visible() and await self._host._is_in_visible_scrollports(signal):
                return True
        return False

    async def _count_self_permalink_candidates(
        self,
        region: Locator,
        target: PostTarget,
    ) -> int:
        links = region.locator('a[href*="/post/"]')
        matches = 0
        for index in range(min(await links.count(), _MAX_DETAIL_LINKS)):
            link = links.nth(index)
            if not await link.is_visible() or await self._host._is_outside_content_landmark(link):
                continue
            href = await link.get_attribute("href")
            parsed = None if href is None else _post_url(href)
            matches += int(parsed is not None and parsed[0] == target.url)
        return matches

    async def _read_detail_root(self, root: Locator, target: PostTarget) -> _DetailRootRead:
        root_handle = await root.element_handle()
        if root_handle is None:
            raise _CardNotReady(root, reason_code="POST_ROOT_DOM_IDENTITY_CHANGED")
        root_path = await root.evaluate(
            """node => {
                const path = [];
                for (let current = node; current?.parentElement;
                     current = current.parentElement) {
                    let siblingIndex = 0;
                    for (let sibling = current.previousElementSibling;
                         sibling; sibling = sibling.previousElementSibling) siblingIndex++;
                    path.push(`${current.tagName.toLowerCase()}:${siblingIndex}`);
                }
                return path.reverse().join("/");
            }"""
        )
        links = root.get_by_role("link")
        link_count = await links.count()
        if link_count > _MAX_DETAIL_LINKS:
            raise UiReadError("DETAIL_ROOT_AMBIGUOUS")
        authors: set[str] = set()
        self_permalink_candidates = 0
        has_rejected_post_link = False
        for index in range(link_count):
            link = links.nth(index)
            if (
                not await self._belongs_to_detail_root(link, root, root_handle)
                or not await link.is_visible()
                or await self._host._is_outside_content_landmark(link)
                or not await self._host._is_in_visible_scrollports(link)
            ):
                continue
            href = await link.get_attribute("href")
            if href is None:
                continue
            parsed_post = _post_url(href)
            if _post_url_hint(href):
                if parsed_post is None or parsed_post[0] != target.url:
                    has_rejected_post_link = True
                else:
                    self_permalink_candidates += 1
                continue
            author = _profile_author(href)
            if author is not None:
                authors.add(author)
        body = await self._host._read_detail_body(
            root,
            f"{target.fingerprint}:{root_path}",
        )
        return _DetailRootRead(
            frozenset(authors),
            has_rejected_post_link,
            self_permalink_candidates,
            body,
            _detail_body_matches(target, body.body),
        )

    async def _observe_detail(
        self,
        target: PostTarget,
    ) -> tuple[_RenderObservation[PostTarget] | None, _DetailState]:
        page = self._host._page
        same_origin = _same_home_origin(page.url)
        route_verified = _post_url(page.url) == (target.url, target.author)
        if not same_origin:
            state = _DetailState(
                route_verified=route_verified,
                same_origin=False,
                terminal_reason="OFF_ORIGIN",
            )
            self._report_detail_state("POST_ROUTE", state)
            raise UiReadError("OFF_ORIGIN_REDIRECT")
        if not route_verified:
            state = _DetailState(
                same_origin=True,
                terminal_reason="ROUTE_NOT_VERIFIED",
            )
            self._report_detail_state("POST_ROUTE", state)
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")

        region, region_count = await self._detail_region()
        pending = await self._has_pending_ui(region)
        if region is None:
            reason = "ROOT_AMBIGUOUS" if region_count > 1 else "ROOT_ABSENT"
            state = _DetailState(
                route_verified=True,
                same_origin=True,
                main_regions_count=region_count,
                hydration_pending=pending,
                terminal_reason=reason,
            )
            self._report_detail_state("POST_ROUTE", state)
            if pending:
                return None, state
            return (
                _RenderObservation(
                    ("no-main-region", region_count, pending),
                    target,
                    terminal_code=_detail_error_code(reason),
                ),
                state,
            )

        all_roots = region.locator(_DETAIL_ROOT_SELECTOR)
        root_count = await all_roots.count()
        article_roots = 0
        pressable_roots = 0
        candidate_roots: list[Locator] = []
        for index in range(min(root_count, _MAX_DETAIL_ROOTS)):
            root = all_roots.nth(index)
            if not await root.is_visible():
                continue
            tag = await root.evaluate("node => node.tagName.toLowerCase()")
            role = await root.get_attribute("role")
            is_pressable = await root.get_attribute("data-pressable-container") == "true"
            if tag == "article" or role == "article":
                article_roots += 1
            if is_pressable:
                pressable_roots += 1
            if (
                await self._is_top_level_detail_root(root, region)
                and await self._host._is_in_visible_scrollports(root)
            ):
                candidate_roots.append(root)

        self_permalink_candidates = await self._count_self_permalink_candidates(region, target)
        if root_count > _MAX_DETAIL_ROOTS:
            state = _DetailState(
                True, True, region_count, article_roots, pressable_roots,
                self_permalink_candidates, 0, 0, 0, pending, "ROOT_AMBIGUOUS",
            )
            self._report_detail_state("POST_ROUTE", state)
            return (
                _RenderObservation(
                    ("too-many-roots", root_count),
                    target,
                    terminal_code="DETAIL_ROOT_AMBIGUOUS",
                ),
                state,
            )

        # Older layouts expose a generic card wrapper but no semantic row root.
        # Accept that path only when it contains the target's canonical self link.
        if not candidate_roots and self_permalink_candidates:
            try:
                root = await self._detail_permalink_card(target)
                detail = await self._host._read_card(
                    root,
                    require_body=target.body is not None,
                )
            except _CardNotReady as readiness:
                pending = pending or readiness.reason_code in {
                    "POST_ROOT_DOM_IDENTITY_CHANGED",
                    "POST_BODY_PENDING",
                    "POST_AUTHOR_PENDING",
                    "POST_PERMALINK_PENDING",
                }
                state = _DetailState(
                    True, True, region_count, article_roots, pressable_roots,
                    self_permalink_candidates, 0, 0, 0, pending, "BODY_PENDING",
                )
                self._report_detail_state("POST_ROUTE", state)
                if pending:
                    return None, state
                return (
                    _RenderObservation(
                        ("generic-card-pending", self_permalink_candidates),
                        target,
                        terminal_code="DETAIL_BODY_UNVERIFIED",
                    ),
                    state,
                )
            body_matches = _detail_body_matches(target, detail.body)
            if detail.author != target.author:
                reason = "AUTHOR_CONFLICT"
            elif not body_matches:
                reason = "BODY_CHANGED"
            elif not detail.body_complete or not detail.body_consistent:
                reason = "BODY_PENDING" if not detail.body_consistent else "BODY_CHANGED"
            else:
                state = _DetailState(
                    True, True, region_count, article_roots, pressable_roots,
                    self_permalink_candidates, 1, 1, 1, False, "DETAIL_VERIFIED",
                )
                self._report_detail_state("POST_ROUTE", state)
                return (
                    _RenderObservation(
                        (detail.url, detail.author, sha256(detail.body.encode()).hexdigest()),
                        detail,
                        ready_state="DETAIL_VERIFIED",
                    ),
                    state,
                )
            state = _DetailState(
                True, True, region_count, article_roots, pressable_roots,
                self_permalink_candidates, 1, 1, 0, pending, reason,
            )
            self._report_detail_state("POST_ROUTE", state)
            if pending:
                return None, state
            return (
                _RenderObservation(
                    ("generic-card-unverified", reason),
                    target,
                    terminal_code=_detail_error_code(reason),
                ),
                state,
            )

        reads: list[_DetailRootRead] = []
        for root in candidate_roots:
            try:
                reads.append(await self._read_detail_root(root, target))
            except _CardNotReady:
                pending = True

        author_candidates = sum(len(item.authors) for item in reads)
        body_nodes_visible = sum(
            item.body.read_evidence.visible_text_rect_count or 0 for item in reads
        )
        body_matches = [item for item in reads if item.body_matches]
        expected_matches = [
            item
            for item in body_matches
            if item.authors == {target.author} and not item.has_rejected_post_link
        ]
        matching_roots = len(expected_matches)

        if len(body_matches) > 1:
            reason = "ROOT_AMBIGUOUS"
        elif body_matches and body_matches[0].has_rejected_post_link:
            reason = "SELF_LINK_REJECTED"
        elif body_matches and body_matches[0].authors and body_matches[0].authors != {target.author}:
            reason = "AUTHOR_CONFLICT"
        elif body_matches and not body_matches[0].authors:
            reason = "AUTHOR_UNVERIFIED"
        elif expected_matches:
            read = expected_matches[0]
            if not read.body.consistent:
                reason = "BODY_CHANGED"
            elif not read.body.complete:
                reason = (
                    "STABILITY_PENDING"
                    if read.body.read_evidence.body_incomplete_reason == "STABILITY_PENDING"
                    else "BODY_PENDING"
                )
            else:
                detail = PostTarget(
                    target.url,
                    target.author,
                    read.body.body,
                    target.fingerprint,
                    target.permalink,
                    body_available=bool(read.body.body and read.body.body.strip()),
                    currently_visible=True,
                    body_truncated=read.body.truncated,
                    body_complete=read.body.complete,
                    body_consistent=read.body.consistent,
                    visible_segments=read.body.segments,
                    read_evidence=read.body.read_evidence,
                )
                state = _DetailState(
                    True, True, region_count, article_roots, pressable_roots,
                    self_permalink_candidates, author_candidates, body_nodes_visible,
                    matching_roots, False, "DETAIL_VERIFIED",
                )
                self._report_detail_state("POST_ROUTE", state)
                return (
                    _RenderObservation(
                        (
                            detail.url,
                            detail.author,
                            sha256((detail.body or "").encode()).hexdigest(),
                            detail.body_complete,
                            detail.body_consistent,
                        ),
                        detail,
                        ready_state="DETAIL_VERIFIED",
                    ),
                    state,
                )
        elif any(item.authors == {target.author} for item in reads):
            reason = "BODY_CHANGED"
        elif any(item.authors and target.author not in item.authors for item in body_matches):
            reason = "AUTHOR_CONFLICT"
        elif candidate_roots and not any(item.authors for item in reads):
            reason = "AUTHOR_UNVERIFIED"
        else:
            reason = "ROOT_ABSENT"

        state = _DetailState(
            True, True, region_count, article_roots, pressable_roots,
            self_permalink_candidates, author_candidates, body_nodes_visible,
            matching_roots, pending, reason,
        )
        self._report_detail_state("POST_ROUTE", state)
        if pending:
            return None, state
        return (
            _RenderObservation(
                (
                    "detail-unverified",
                    reason,
                    article_roots,
                    pressable_roots,
                    author_candidates,
                    body_nodes_visible,
                    matching_roots,
                ),
                target,
                terminal_code=_detail_error_code(reason),
            ),
            state,
        )

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
        except PlaywrightTimeoutError:
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED") from None
        try:
            await self._host._page.wait_for_url(
                is_target_url,
                timeout=self._host._remaining_readiness_ms(
                    deadline,
                    "DETAIL_NAVIGATION_UNVERIFIED",
                ),
            )
        except PlaywrightTimeoutError:
            state = _DetailState(
                same_origin=_same_home_origin(self._host._page.url),
                terminal_reason="ROUTE_NOT_VERIFIED",
            )
            self._report_detail_state("ROUTE", state)
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED") from None
        if not _same_home_origin(self._host._page.url):
            self._report_detail_state(
                "ROUTE",
                _DetailState(terminal_reason="OFF_ORIGIN"),
            )
            raise UiReadError("OFF_ORIGIN_REDIRECT")

        if not is_target_url(self._host._page.url):
            self._report_detail_state(
                "ROUTE",
                _DetailState(
                    same_origin=True,
                    terminal_reason="ROUTE_NOT_VERIFIED",
                ),
            )
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")
        self._report_detail_state(
            "ROUTE",
            _DetailState(
                route_verified=True,
                same_origin=True,
                terminal_reason="ROUTE_VERIFIED",
            ),
        )

        latest_state = _DetailState(
            route_verified=True,
            same_origin=True,
            terminal_reason="ROOT_ABSENT",
        )

        async def read_detail_window(window_deadline: float) -> PostTarget:
            self._last_reported_detail_state = None

            async def observe() -> _RenderObservation[PostTarget] | None:
                nonlocal latest_state
                try:
                    await self._host._page.wait_for_function(
                        """() => Array.from(document.querySelectorAll(
                            'main, [role="main"], [role="feed"]'
                        )).some(node => {
                            if (node.getAttribute("aria-busy") === "true") return true;
                            const style = getComputedStyle(node);
                            const rect = node.getBoundingClientRect();
                            return style.display !== "none" &&
                                style.visibility !== "hidden" &&
                                rect.width > 0 && rect.height > 0;
                        })""",
                        timeout=self._host._remaining_readiness_ms(
                            window_deadline,
                            "DETAIL_RENDER_PENDING",
                        ),
                    )
                except PlaywrightTimeoutError:
                    observation, latest_state = await self._observe_detail(target)
                    return observation
                self._host._remaining_readiness_ms(
                    window_deadline,
                    "DETAIL_RENDER_PENDING",
                )
                await self._host._check_blocker()
                observation, latest_state = await self._observe_detail(target)
                self._host._remaining_readiness_ms(
                    window_deadline,
                    "DETAIL_RENDER_PENDING",
                )
                return observation

            return await self._host._await_render_settled(
                "DETAIL",
                window_deadline,
                timeout_code="DETAIL_RENDER_PENDING",
                observe=observe,
            )

        try:
            detail = await self._host._recoverable_stage(
                "DETAIL",
                "DETAIL_RENDER_PENDING",
                read_detail_window,
                recoverable_codes=("DETAIL_RENDER_PENDING",),
            )
        except UiReadError as error:
            if error.code == "DETAIL_RENDER_PENDING":
                raise UiReadError(_detail_error_code(latest_state.terminal_reason)) from None
            raise
        if detail.url != target.url or detail.author != target.author:
            raise UiReadError("DETAIL_PROVENANCE_UNVERIFIED")
        if detail.body is None or target.body is None:
            raise UiReadError("DETAIL_BODY_UNVERIFIED")
        if not _detail_body_matches(target, detail.body):
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
        if await matching_permalink.count() and await self._has_pending_ui(region):
            raise _CardNotReady(
                matching_permalink.first,
                in_viewport=True,
                timeout_code="DETAIL_RENDER_PENDING",
            )
        raise UiReadError("DETAIL_ROOT_UNVERIFIED")

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
