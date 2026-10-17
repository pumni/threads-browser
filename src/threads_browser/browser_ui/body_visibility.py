"""Track rendered body-block visibility across adjacent feed viewports."""

from __future__ import annotations

from playwright.async_api import Locator

from .feed_rows import (
    _BODY_EXCLUSION_SELECTOR,
    _BODY_TEXT_SELECTOR,
    _FEED_ROOT_SELECTOR,
    _MAX_STRUCTURE_ELEMENTS,
)
from .geometry import with_scrollport_geometry as _with_scrollport_geometry


class BodyVisibilityCoverage:
    """Own viewport intervals used to decide whether body blocks were visited."""

    def __init__(self) -> None:
        self._body_visibility_ranges: dict[
            tuple[str, int], tuple[float, float, float, list[tuple[float, float]]]
        ] = {}

    async def inspect(
        self,
        root: Locator,
        *,
        fingerprint: str | None = None,
    ) -> tuple[bool, bool]:
        candidates = await root.evaluate(
            _with_scrollport_geometry("""(root, args) => {
                /*SCROLLPORT_GEOMETRY*/
                const bodySafe = element => {
                    let current = element;
                    while (current && current !== root) {
                        if (current.matches(args.excluded)) return false;
                        current = current.parentElement;
                    }
                    if (current !== root) return false;
                    const nearest = element.parentElement?.closest(args.roots);
                    if (root.matches(args.roots)) {
                        if (nearest !== root) return false;
                    } else if (!root.contains(element) || nearest) {
                        return false;
                    }
                    return !element.querySelector(`${args.excluded}, ${args.roots}`);
                };
                const cssVisible = element => {
                    const style = getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== "none" && style.visibility !== "hidden" &&
                        rect.width > 0 && rect.height > 0;
                };
                const geometry = element => {
                    const rect = element.getBoundingClientRect();
                    const geometry = geometryFor(element);
                    const metrics = scrollMetrics(element);
                    const contentTop = rect.top + metrics.offset;
                    const contentBottom = rect.bottom + metrics.offset;
                    const visibleTop = geometry.visible.top + metrics.offset;
                    const visibleBottom = geometry.visible.bottom + metrics.offset;
                    return {
                        top: contentTop,
                        bottom: contentBottom,
                        width: rect.width,
                        visibleTop: Math.max(contentTop, visibleTop),
                        visibleBottom: Math.min(contentBottom, visibleBottom),
                        intersects: geometry.intersects,
                    };
                };
                const all = Array.from(root.querySelectorAll(args.body))
                    .filter(element => bodySafe(element) && cssVisible(element))
                    .slice(0, args.maxStructure);
                const bodySet = new Set(all);
                const outermost = all.filter(element => {
                    for (let parent = element.parentElement;
                         parent && parent !== root; parent = parent.parentElement) {
                        if (bodySet.has(parent)) return false;
                    }
                    return true;
                });
                return outermost.map(geometry);
            }"""),
            {
                "excluded": _BODY_EXCLUSION_SELECTOR,
                "roots": _FEED_ROOT_SELECTOR,
                "body": _BODY_TEXT_SELECTOR,
                "maxStructure": _MAX_STRUCTURE_ELEMENTS,
            },
        )
        if not candidates:
            return False, False
        fully_visited = True
        any_visible = False
        for index, candidate in enumerate(candidates):
            top = float(candidate["top"])
            bottom = float(candidate["bottom"])
            width = float(candidate["width"])
            visible_top = float(candidate["visibleTop"])
            visible_bottom = float(candidate["visibleBottom"])
            intersects = bool(candidate["intersects"])
            any_visible = any_visible or intersects
            if fingerprint is None:
                fully_visited = fully_visited and intersects
                continue
            key = (fingerprint, index)
            previous = self._body_visibility_ranges.get(key)
            if (
                previous is None
                or abs(previous[0] - top) > 2
                or abs(previous[1] - bottom) > 2
                or abs(previous[2] - width) > 2
            ):
                intervals: list[tuple[float, float]] = []
            else:
                intervals = list(previous[3])
            if visible_bottom > visible_top:
                intervals.append((visible_top, visible_bottom))
            merged: list[list[float]] = []
            for start, end in sorted(intervals):
                if merged and start <= merged[-1][1] + 1:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])
            self._body_visibility_ranges[key] = (
                top,
                bottom,
                width,
                [(start, end) for start, end in merged],
            )
            fully_visited = fully_visited and bool(
                merged and merged[0][0] <= top + 2 and merged[0][1] >= bottom - 2
            )
        return True, fully_visited if fingerprint is not None else any_visible
