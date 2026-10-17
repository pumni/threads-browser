"""Feed-root discovery and scan-local candidate identity retention."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from playwright.async_api import JSHandle, Locator, Page

from .feed_rows import (
    _BODY_EXCLUSION_SELECTOR,
    _BODY_TEXT_SELECTOR,
    _FEED_ROOT_SELECTOR,
    _MAX_CARD_ANCESTORS,
    _MAX_STRUCTURE_ELEMENTS,
    _QUOTE_SELECTOR,
)
from .geometry import with_scrollport_geometry as _with_scrollport_geometry
from .viewport_scan_models import _TrackedRowIdentity

_MAX_FEED_ROOTS_PER_TYPE = 40
_MAX_VISIBLE_ROW_CANDIDATES = 40


@dataclass(slots=True)
class FeedRootState:
    """DOM handles retained only across adjacent viewport observations."""

    last_roots: tuple[Locator, ...] = ()
    last_identities: tuple[_TrackedRowIdentity, ...] = ()
    carried_identities: tuple[_TrackedRowIdentity, ...] = ()
    pending_identities: tuple[_TrackedRowIdentity, ...] = ()


class FeedRootDiscoveryHost(Protocol):
    _page: Page
    _last_viewport_candidate_roots: tuple[Locator, ...]
    _last_viewport_candidate_identities: tuple[_TrackedRowIdentity, ...]
    _carried_viewport_candidate_identities: tuple[_TrackedRowIdentity, ...]
    _pending_viewport_candidate_identities: tuple[_TrackedRowIdentity, ...]
    _max_feed_roots_per_type: int
    _max_visible_row_candidates: int

    async def _is_outside_content_landmark(self, element: Locator) -> bool: ...
    async def _is_inside_quote(self, element: Locator) -> bool: ...
    async def _is_in_visible_scrollports(self, element: Locator) -> bool: ...
    async def _is_nested_feed_root(self, element: Locator) -> bool: ...
    def _intersects_viewport(self, box: dict[str, float], viewport: dict[str, int]) -> bool: ...
    async def _same_dom_element(self, root: Locator, handle: JSHandle) -> bool: ...
    @property
    def _viewport_scan(self): ...


class FeedRootDiscoverer:
    """Find visible top-level rows and carry verified DOM identities safely."""

    def __init__(self, host: FeedRootDiscoveryHost) -> None:
        self._host = host
        self.state = FeedRootState()

    async def discard_pending(self) -> None:
        retained = {id(identity.dom_handle) for identity in self.state.last_identities}
        pending = self.state.pending_identities
        self.state.pending_identities = ()
        await self._host._viewport_scan.dispose_handles(
            tuple(
                identity.dom_handle
                for identity in pending
                if id(identity.dom_handle) not in retained
            )
        )

    async def remember(
        self,
        roots: tuple[Locator, ...],
        identities: tuple[_TrackedRowIdentity, ...],
    ) -> None:
        retained = {id(identity.dom_handle) for identity in identities}
        previous = (*self.state.last_identities, *self.state.pending_identities)
        self.state.last_roots = roots
        self.state.last_identities = identities
        self.state.pending_identities = ()
        await self._host._viewport_scan.dispose_handles(
            tuple(
                identity.dom_handle
                for identity in previous
                if id(identity.dom_handle) not in retained
            )
        )

    async def _discard_pending_viewport_candidates(self) -> None:
        retained = {
            id(identity.dom_handle) for identity in self._host._last_viewport_candidate_identities
        }
        pending = self._host._pending_viewport_candidate_identities
        self._host._pending_viewport_candidate_identities = ()
        await self._host._viewport_scan.dispose_handles(
            tuple(
                identity.dom_handle
                for identity in pending
                if id(identity.dom_handle) not in retained
            )
        )

    async def invalidate_document_candidates(self) -> None:
        """Drop DOM handles after a verified navigation while keeping ledger evidence."""
        identities = (
            *self.state.last_identities,
            *self.state.pending_identities,
            *self.state.carried_identities,
        )
        unique_handles = {
            id(identity.dom_handle): identity.dom_handle for identity in identities
        }
        self.state.last_roots = ()
        self.state.last_identities = ()
        self.state.pending_identities = ()
        self.state.carried_identities = ()
        await self._host._viewport_scan.dispose_handles(tuple(unique_handles.values()))

    async def _remember_viewport_candidates(
        self,
        roots: tuple[Locator, ...],
        identities: tuple[_TrackedRowIdentity, ...],
    ) -> None:
        retained = {id(identity.dom_handle) for identity in identities}
        previous = (
            *self._host._last_viewport_candidate_identities,
            *self._host._pending_viewport_candidate_identities,
        )
        self._host._last_viewport_candidate_roots = roots
        self._host._last_viewport_candidate_identities = identities
        self._host._pending_viewport_candidate_identities = ()
        await self._host._viewport_scan.dispose_handles(
            tuple(
                identity.dom_handle
                for identity in previous
                if id(identity.dom_handle) not in retained
            )
        )

    async def discover(
        self,
        region: Locator,
        viewport: dict[str, int],
    ) -> tuple[list[Locator], bool]:
        candidates: list[tuple[float, Locator]] = []
        candidate_keys: set[str] = set()

        async def add_candidate(
            root: Locator,
            box: dict[str, float],
            key: str | None = None,
        ) -> None:
            if key is None:
                key = await root.evaluate(
                    """node => {
                        const path = [];
                        for (let current = node; current;
                             current = current.parentElement) {
                            const parent = current.parentElement;
                            path.push(parent ? Array.prototype.indexOf.call(
                                parent.children, current
                            ) : 0);
                        }
                        return path.reverse().join("/");
                    }"""
                )
            if key in candidate_keys:
                return
            candidate_keys.add(key)
            candidates.append((box["y"], root))

        self._host._carried_viewport_candidate_identities = ()
        truncated = await self._collect_semantic_candidates(
            region,
            viewport,
            add_candidate,
        )
        carried = await self._collect_carried_candidates(region, viewport, add_candidate)
        self._host._carried_viewport_candidate_identities = tuple(carried)
        truncated |= await self._collect_fallback_candidates(
            region,
            viewport,
            add_candidate,
        )
        candidates.sort(key=lambda item: item[0])
        roots = [root for _, root in candidates]
        if len(roots) > self._host._max_visible_row_candidates:
            truncated = True
            roots = roots[: self._host._max_visible_row_candidates]
        return roots, truncated

    async def _collect_semantic_candidates(
        self,
        region: Locator,
        viewport: dict[str, int],
        add_candidate: Callable[..., Awaitable[None]],
    ) -> bool:
        truncated = False
        selectors = (
            'article, [role="article"]:not(article)',
            '[data-pressable-container="true"]:not(article):not([role="article"])',
        )

        async def inspect_semantic_root(root: Locator) -> dict[str, float] | None:
            if not await root.is_visible():
                return None
            if await self._host._is_outside_content_landmark(
                root
            ) or await self._host._is_inside_quote(root):
                return None
            box = await root.bounding_box()
            if (
                box is None
                or not self._host._intersects_viewport(box, viewport)
                or not await self._host._is_in_visible_scrollports(root)
            ):
                return None
            if await self._host._is_nested_feed_root(root):
                return None
            return box

        for selector in selectors:
            roots = region.locator(selector)
            available = await roots.count()
            count = min(available, self._host._max_feed_roots_per_type)
            truncated = truncated or available > count
            selected_roots = [roots.nth(index) for index in range(count)]
            boxes = await asyncio.gather(*(inspect_semantic_root(root) for root in selected_roots))
            eligible_roots = [
                (root, box)
                for root, box in zip(selected_roots, boxes, strict=True)
                if box is not None
            ]
            identity_keys = await asyncio.gather(
                *(
                    root.evaluate(
                        """node => {
                            const path = [];
                            for (let current = node; current;
                                 current = current.parentElement) {
                                const parent = current.parentElement;
                                path.push(parent ? Array.prototype.indexOf.call(
                                    parent.children, current
                                ) : 0);
                            }
                            return path.reverse().join("/");
                        }"""
                    )
                    for root, _ in eligible_roots
                )
            )
            for root_box, identity in zip(
                eligible_roots,
                identity_keys,
                strict=True,
            ):
                root, box = root_box
                await add_candidate(root, box, str(identity))
        return truncated

    async def _collect_carried_candidates(
        self,
        region: Locator,
        viewport: dict[str, int],
        add_candidate: Callable[..., Awaitable[None]],
    ) -> list[_TrackedRowIdentity]:
        carried_identities: list[_TrackedRowIdentity] = []
        region_handle = await region.element_handle()
        if region_handle is not None:
            try:

                async def inspect_carried_identity(
                    identity: _TrackedRowIdentity,
                ) -> dict[str, object] | None:
                    return await identity.dom_handle.evaluate(
                        _with_scrollport_geometry("""(node, args) => {
                            /*SCROLLPORT_GEOMETRY*/
                            const style = getComputedStyle(node);
                            const rect = node.getBoundingClientRect();
                            if (!node.isConnected || !args.region.contains(node) ||
                                style.display === "none" ||
                                style.visibility === "hidden" ||
                                rect.width <= 0 || rect.height <= 0 ||
                                node.closest(args.outside) ||
                                node.closest(args.quote) ||
                                !geometryFor(node).intersects) return null;
                            const path = [];
                            for (let current = node; current;
                                 current = current.parentElement) {
                                const parent = current.parentElement;
                                path.push(parent ? Array.prototype.indexOf.call(
                                    parent.children, current
                                ) : 0);
                            }
                            return {
                                y: rect.top,
                                key: path.reverse().join("/"),
                            };
                        }"""),
                        {
                            "viewport": viewport,
                            "region": region_handle,
                            "outside": (
                                "nav, aside, footer, form, [contenteditable='true'], "
                                "[role='navigation'], [role='menu'], "
                                "[role='complementary'], [role='dialog'], "
                                "[role='search'], [role='banner'], "
                                "[role='contentinfo'], [role='textbox']"
                            ),
                            "quote": _QUOTE_SELECTOR,
                        },
                    )

                previous_identities = self._host._last_viewport_candidate_identities
                identity_boxes = await asyncio.gather(
                    *(inspect_carried_identity(identity) for identity in previous_identities)
                )
                carried_identities.extend(
                    identity
                    for identity, box in zip(previous_identities, identity_boxes, strict=True)
                    if box is not None
                )
                for index, root in enumerate(self._host._last_viewport_candidate_roots):
                    if index >= len(previous_identities):
                        continue
                    identity = previous_identities[index]
                    if not await self._host._same_dom_element(root, identity.dom_handle):
                        continue
                    box = identity_boxes[index]
                    if box is None:
                        continue
                    await add_candidate(
                        root,
                        {"y": float(box["y"])},
                        str(box["key"]),
                    )
            finally:
                await region_handle.dispose()
        return carried_identities

    async def _collect_fallback_candidates(
        self,
        region: Locator,
        viewport: dict[str, int],
        add_candidate: Callable[..., Awaitable[None]],
    ) -> bool:
        truncated = False
        # Some pages expose independent post rows only as ordinary containers.
        # Infer a root from the nearest visible ancestor that owns exactly one
        # timestamp and one profile-author link; no post permalink is required.
        times = region.locator("time")
        time_count = await times.count()
        if time_count > _MAX_STRUCTURE_ELEMENTS:
            truncated = True
        fallbacks = await times.evaluate_all(
            _with_scrollport_geometry("""(timeNodes, args) => {
                /*SCROLLPORT_GEOMETRY*/
                const visible = element => {
                    const style = getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    if (style.display === "none" || style.visibility === "hidden" ||
                        rect.width <= 0 || rect.height <= 0) return false;
                    return geometryFor(element).intersects;
                };
                const outside = element => element.closest(args.outside) !== null;
                const insideQuote = element => element.closest(args.quote) !== null;
                const profileLink = element => {
                    const href = element.getAttribute("href");
                    if (!href) return false;
                    try {
                        const url = new URL(href, location.href);
                        return url.protocol === "https:" &&
                            url.hostname.toLowerCase() === "www.threads.com" &&
                            !url.search && !url.hash &&
                            /^[/]@[A-Za-z0-9._]{1,30}[/]?$/.test(url.pathname);
                    } catch { return false; }
                };
                const postPathHint = element => {
                    const href = element.getAttribute("href");
                    if (!href) return false;
                    try {
                        return new URL(href, location.href).pathname
                            .split("/").includes("post");
                    } catch { return false; }
                };
                const identityPath = node => {
                    const path = [];
                    for (let current = node; current; current = current.parentElement) {
                        const parent = current.parentElement;
                        path.push(parent ? Array.prototype.indexOf.call(
                            parent.children, current
                        ) : 0);
                    }
                    return path.reverse().join("/");
                };
                const seen = new Set();
                const results = [];
                const limit = Math.min(timeNodes.length, args.maxTimes);
                for (let index = 0; index < limit; index++) {
                    const time = timeNodes[index];
                    if (outside(time) || insideQuote(time) || !visible(time) ||
                        time.parentElement?.closest(args.roots)) continue;
                    let ancestor = time;
                    let fallback = null;
                    for (let depth = 1; depth <= args.maxAncestors; depth++) {
                        ancestor = ancestor.parentElement;
                        if (!ancestor) break;
                        const tag = ancestor.tagName.toLowerCase();
                        const role = ancestor.getAttribute("role");
                        if (["body", "html", "main"].includes(tag) ||
                            role === "main" || role === "feed") break;
                        if (outside(ancestor) || insideQuote(ancestor)) break;
                        if (ancestor.matches(args.roots)) break;
                        if (!visible(ancestor)) continue;
                        const belongs = element => {
                            const nearest = element.parentElement?.closest(args.roots);
                            return ancestor.contains(element) && !nearest;
                        };
                        const times = Array.from(ancestor.querySelectorAll("time"))
                            .filter(element => belongs(element) && !outside(element) &&
                                !insideQuote(element) && visible(element));
                        const links = Array.from(ancestor.querySelectorAll("a"))
                            .filter(element => belongs(element) && !outside(element) &&
                                visible(element));
                        const profileCount = links.filter(profileLink).length;
                        const postCount = links.filter(postPathHint).length;
                        const bodies = Array.from(
                            ancestor.querySelectorAll(args.body)
                        ).filter(element => {
                            if (!belongs(element) || !visible(element)) return false;
                            let current = element;
                            while (current && current !== ancestor) {
                                if (current.matches(args.bodyExcluded)) return false;
                                current = current.parentElement;
                            }
                            return current === ancestor &&
                                !element.querySelector(
                                    `${args.bodyExcluded}, ${args.roots}`
                                );
                        });
                        const box = ancestor.getBoundingClientRect();
                        const evidence = profileCount || postCount || bodies.length;
                        if (times.length === 1 && evidence) {
                            fallback = {depth, y: box.top, identity: identityPath(ancestor)};
                            if (bodies.length) break;
                        }
                        if (times.length > 1 || profileCount > 1) {
                            if (bodies.length || postCount) {
                                fallback = {
                                    depth, y: box.top, identity: identityPath(ancestor),
                                };
                            }
                            break;
                        }
                    }
                    if (fallback === null || seen.has(fallback.identity)) continue;
                    seen.add(fallback.identity);
                    results.push({index, ...fallback});
                }
                return results;
            }"""),
            {
                "viewport": viewport,
                "roots": _FEED_ROOT_SELECTOR,
                "quote": _QUOTE_SELECTOR,
                "body": _BODY_TEXT_SELECTOR,
                "bodyExcluded": _BODY_EXCLUSION_SELECTOR,
                "outside": (
                    "nav, aside, footer, form, [contenteditable='true'], "
                    "[role='navigation'], [role='menu'], [role='complementary'], "
                    "[role='dialog'], [role='search'], [role='banner'], "
                    "[role='contentinfo'], [role='textbox']"
                ),
                "maxAncestors": _MAX_CARD_ANCESTORS,
                "maxTimes": _MAX_STRUCTURE_ELEMENTS,
            },
        )
        for fallback in fallbacks:
            timestamp = times.nth(fallback["index"])
            ancestor = timestamp.locator(f"xpath=ancestor::*[{fallback['depth']}]")
            await add_candidate(
                ancestor,
                {"y": fallback["y"]},
                str(fallback["identity"]),
            )
        return truncated
