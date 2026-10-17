"""Collect privacy-safe structural evidence for visible feed rows."""

from __future__ import annotations

import asyncio
from typing import Protocol

from playwright.async_api import Locator

from ..feed.urls import _post_url, _post_url_structure
from .feed_rows import (
    _BODY_EXCLUSION_SELECTOR,
    _BODY_TEXT_SELECTOR,
    _FEED_ROOT_SELECTOR,
    _MAX_STRUCTURE_ELEMENTS,
    _CandidateStructure,
    _FeedScanCounts,
)


class CandidateStructureHost(Protocol):
    last_scan_counts: _FeedScanCounts

    async def _inspect_candidate_root(
        self, root: Locator, viewport: dict[str, int]
    ) -> _CandidateStructure: ...
    @staticmethod
    def _profile_anchor_candidate(value: str) -> bool: ...


class CandidateStructureCollector:
    """Aggregate row structure and URL shape without retaining page content."""

    def __init__(self, host: CandidateStructureHost) -> None:
        self._host = host

    def _record_structure_reason(self, reason: str) -> None:
        counts = self._host.last_scan_counts
        counts.structural_reasons[reason] = counts.structural_reasons.get(reason, 0) + 1

    def _record_url_structure(self, href: str, timestamp_link: bool) -> None:
        if "/post/" not in href:
            return
        counts = self._host.last_scan_counts
        segments, trailing_slash, has_query, has_fragment, is_relative = _post_url_structure(href)
        canonical = _post_url(href) is not None
        counts.url_timestamp_links += int(timestamp_link)
        counts.url_secondary_links += int(not timestamp_link)
        counts.url_canonical_links += int(canonical)
        counts.url_rejected_links += int(not canonical)
        shape = (
            f"segments={segments},trailing_slash={int(trailing_slash)},"
            f"query={int(has_query)},fragment={int(has_fragment)},"
            f"relative={int(is_relative)},role="
            f"{'TIMESTAMP' if timestamp_link else 'SECONDARY'},"
            f"canonical={int(canonical)}"
        )
        counts.url_structure_counts[shape] = counts.url_structure_counts.get(shape, 0) + 1

    async def _collect_structure_diagnostics(
        self,
        region: Locator,
        roots: list[Locator],
        viewport: dict[str, int],
    ) -> dict[int, _CandidateStructure]:
        counts = self._host.last_scan_counts
        structures: dict[int, _CandidateStructure] = {}
        inspected = await asyncio.gather(
            *(self._host._inspect_candidate_root(root, viewport) for root in roots)
        )
        for index, (root, structure) in enumerate(zip(roots, inspected, strict=True)):
            is_semantic = structure.root_kind in {"ARTICLE", "ROLE_ARTICLE"}
            is_pressable = structure.root_kind == "PRESSABLE"
            counts.article_roots += int(is_semantic)
            counts.pressable_roots += int(is_pressable)
            structures[index] = structure
            for href, timestamp_link in structure.post_url_links:
                self._host._record_url_structure(href, timestamp_link)
            counts.visible_anchors += structure.visible_anchors
            counts.visible_post_links += structure.visible_post_links
            counts.url_parsed += structure.canonical_post_links
            counts.profile_anchor_candidates += structure.profile_anchor_candidates
            counts.visible_times += structure.visible_times
            counts.body_candidates += structure.body_candidates
            counts.paragraph_candidates += structure.paragraph_candidates
            row_evidence = structure.visible_times and (
                structure.profile_anchor_candidates or structure.visible_post_links
            )
            if row_evidence and not structure.body_candidates:
                counts.unsupported_roots += 1
                self._host._record_structure_reason("DISPLAY_BODY_PENDING")
            elif row_evidence and not (
                structure.canonical_post_links and structure.profile_anchor_candidates
            ):
                self._host._record_structure_reason("DISPLAY_OBSERVATION_ONLY")

        unscoped = await region.evaluate(
            """(region, args) => {
                const postPathHint = value => {
                    if (!value) return false;
                    try {
                        return new URL(value, location.href).pathname
                            .split("/").includes("post");
                    } catch { return false; }
                };
                const outsideSelector = args.outside;
                const visibleInPage = element => {
                    const style = getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.display !== "none" && style.visibility !== "hidden" &&
                        rect.width > 0 && rect.height > 0 && rect.right > 0 &&
                        rect.bottom > 0 && rect.left < args.viewport.width &&
                        rect.top < args.viewport.height;
                };
                const outside = element => element.closest(outsideSelector) !== null;
                const nested = element => {
                    const nearest = element.parentElement?.closest(args.roots);
                    return Boolean(nearest?.parentElement?.closest(args.roots));
                };
                const insideFeedRoot = element =>
                    element.parentElement?.closest(args.roots) !== null;
                const safeText = element => {
                    let current = element;
                    while (current) {
                        if (current.matches(args.bodyExcluded)) return false;
                        current = current.parentElement;
                    }
                    return !element.querySelector(args.bodyExcluded);
                };
                const anchors = Array.from(region.querySelectorAll("a"))
                    .slice(0, args.maxElements)
                    .filter(element => !outside(element) && !nested(element) &&
                        !insideFeedRoot(element) && visibleInPage(element));
                const times = Array.from(region.querySelectorAll("time"))
                    .slice(0, args.maxElements)
                    .filter(element => !outside(element) && !nested(element) &&
                        !insideFeedRoot(element) && visibleInPage(element));
                const paragraphs = Array.from(region.querySelectorAll("p"))
                    .slice(0, args.maxElements)
                    .filter(element => !outside(element) && !nested(element) &&
                        !insideFeedRoot(element) && visibleInPage(element) &&
                        safeText(element));
                const bodies = Array.from(region.querySelectorAll(args.body))
                    .slice(0, args.maxElements)
                    .filter(element => !outside(element) && !nested(element) &&
                        !insideFeedRoot(element) && visibleInPage(element) &&
                        safeText(element));
                return {
                    profileHrefs: anchors
                        .map(element => element.getAttribute("href"))
                        .filter(Boolean),
                    postLinks: anchors
                        .map(element => ({
                            href: element.getAttribute("href"),
                            timestamp: Boolean(
                                element.querySelector("time") &&
                                visibleInPage(element.querySelector("time"))
                            ),
                        }))
                        .filter(item => postPathHint(item.href)),
                    visibleAnchors: anchors.length,
                    visibleTimes: times.length,
                    paragraphCandidates: paragraphs.length,
                    bodyCandidates: bodies.length,
                };
            }""",
            {
                "viewport": viewport,
                "roots": _FEED_ROOT_SELECTOR,
                "outside": (
                    "nav, aside, footer, form, [contenteditable='true'], "
                    "[role='navigation'], [role='menu'], [role='complementary'], "
                    "[role='dialog'], [role='search'], [role='banner'], "
                    "[role='contentinfo'], [role='textbox']"
                ),
                "body": _BODY_TEXT_SELECTOR,
                "bodyExcluded": _BODY_EXCLUSION_SELECTOR,
                "maxElements": _MAX_STRUCTURE_ELEMENTS,
            },
        )
        counts.visible_anchors += unscoped["visibleAnchors"]
        counts.visible_times += unscoped["visibleTimes"]
        counts.paragraph_candidates += unscoped["paragraphCandidates"]
        counts.body_candidates += unscoped["bodyCandidates"]
        for item in unscoped["postLinks"]:
            href = item["href"]
            counts.visible_post_links += 1
            counts.url_parsed += int(_post_url(href) is not None)
            self._host._record_url_structure(href, bool(item["timestamp"]))
        for href in unscoped["profileHrefs"]:
            counts.profile_anchor_candidates += int(self._host._profile_anchor_candidate(href))
        counts.visible_links = counts.visible_post_links
        return structures
