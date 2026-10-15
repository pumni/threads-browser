"""Visible body text progress and hydration evidence for feed rows."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field, replace
from hashlib import sha256

from playwright.async_api import JSHandle, Locator, Page

from ..feed.models import BodyReadEvidence, VisibleBodySegment
from .feed_rows import (
    _BODY_EXCLUSION_SELECTOR,
    _BODY_TEXT_SELECTOR,
    _FEED_ROOT_SELECTOR,
    _MAX_STRUCTURE_ELEMENTS,
    _FeedScanCounts,
)
from .geometry import with_scrollport_geometry as _with_scrollport_geometry

_MAX_BODY_CODEPOINTS = 10_000
_MAX_BODY_TEXT_NODES = 256
_MAX_BODY_RANGE_CHECKS = 40_000
_MAX_BODY_PROGRESS_POSTS = 200


def _body_progress_state(
    evidence: BodyReadEvidence | None,
    *,
    has_readable_body: bool,
) -> tuple[str, str]:
    if has_readable_body:
        return "PARTIAL_BODY", "VISIBLE_TEXT_PROGRESS"
    state = "NO_BODY_NODES" if evidence is None else evidence.state
    return {
        "TEXT_BELOW": ("DEFERRED_TEXT_BELOW", "BODY_TEXT_BELOW_VIEWPORT"),
        "TEXT_ABOVE": ("DEFERRED_TEXT_ABOVE", "BODY_TEXT_ABOVE_VIEWPORT"),
        "MEDIA_OR_WHITESPACE_GAP": (
            "MEDIA_OR_WHITESPACE_GAP",
            "NO_RENDERED_BODY_TEXT_IN_VIEW",
        ),
        "UNSUPPORTED_STRUCTURE": (
            "UNSUPPORTED_STRUCTURE",
            "POST_BODY_STRUCTURE_UNSUPPORTED",
        ),
    }.get(state, ("PENDING_HYDRATION", "POST_BODY_PENDING"))


def _canonicalize_body(value: str) -> str:
    lines = [
        re.sub(r"[^\S\n]+", " ", line).strip()
        for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    return "\n".join(line for line in lines if line)


def _canonicalize_body_block(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


@dataclass(slots=True, repr=False)
class _BodyTextProgress:
    length: int
    block: int
    order: int
    expected_ranges: list[tuple[int, int]] = field(default_factory=list, repr=False)
    visited_ranges: list[tuple[int, int]] = field(default_factory=list, repr=False)
    fragments: list[_TextFragment] = field(default_factory=list, repr=False)

    def __repr__(self) -> str:
        return "_BodyTextProgress(<private>)"


@dataclass(slots=True, repr=False)
class _TextFragment:
    start: int
    end: int
    text: str = field(repr=False)
    viewport_y: float = field(default=0.0, repr=False)
    content_y: float = field(default=0.0, repr=False)

    def __repr__(self) -> str:
        return "_TextFragment(<private>)"


@dataclass(frozen=True, slots=True)
class _VisibleBodyRead:
    body: str | None = field(repr=False)
    complete: bool
    has_rendered_text: bool
    consistent: bool = True
    segments: tuple[VisibleBodySegment, ...] = field(default=(), repr=False)
    deferred_above: int = 0
    deferred_below: int = 0
    nearest_deferred_below_px: float | None = None
    frontier_content_px: float | None = None
    truncated: bool = False
    read_evidence: BodyReadEvidence = field(default_factory=BodyReadEvidence)


class BodyTextReader:
    """Track visible text fragments without retaining data outside the run."""

    def __init__(self, page: Page) -> None:
        self._page = page
        self._body_text_progress: dict[str, dict[str, _BodyTextProgress]] = {}
        self._body_visibility_ranges: dict[str, set[tuple[int, int, int]]] = {}
        self._body_progress_salt = secrets.token_bytes(16)

    @classmethod
    def _fragment_text(
        cls,
        fragments: list[_TextFragment],
        start: int,
        end: int,
    ) -> str | None:
        for fragment in fragments:
            if fragment.start <= start and fragment.end >= end:
                return cls._utf16_slice(
                    fragment.text,
                    start - fragment.start,
                    end - fragment.start,
                )
        return None

    @staticmethod
    def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
        merged: list[list[int]] = []
        for start, end in sorted(ranges):
            if end <= start:
                continue
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return [(start, end) for start, end in merged]

    async def _read_visible_body_progress(
        self,
        root: Locator,
        root_handle: JSHandle,
        *,
        fingerprint: str | None = None,
        scan_counts: _FeedScanCounts,
        isolated_scope: bool = False,
    ) -> _VisibleBodyRead:
        observation = await root.evaluate(
            _with_scrollport_geometry("""(root, args) => {
                /*SCROLLPORT_GEOMETRY*/
                const rootRect = root.getBoundingClientRect();
                const rootGeometry = geometryFor(root);
                const rootClip = rootGeometry.clip;
                const rootIntersects = rootGeometry.intersects;
                const rootPositionBand = rootRect.bottom <= rootClip.top ? "ABOVE" :
                    rootRect.top >= rootClip.bottom ? "BELOW" : "INTERSECTS";
                const cssVisible = element => {
                    for (let current = element; current; current = current.parentElement) {
                        const style = getComputedStyle(current);
                        if (style.display === "none" || style.visibility === "hidden" ||
                            Number(style.opacity) === 0) return false;
                        if (current === root) break;
                    }
                    return true;
                };
                const belongs = element => {
                    const nearest = element.parentElement?.closest(args.roots);
                    if (root.matches(args.roots)) return nearest === root;
                    if (args.isolatedScope) {
                        const owner = root.parentElement?.closest(args.roots);
                        return root.contains(element) && (!nearest || nearest === owner);
                    }
                    return root.contains(element) && !nearest;
                };
                const excluded = element =>
                    element.matches(args.excluded) ||
                    Boolean(element.parentElement?.closest(args.excluded));
                const hasBodyContainer = element =>
                    root.matches(args.body) || element.matches(args.body) ||
                    Boolean(element.parentElement?.closest(args.body));
                const nearestBlock = element => {
                    for (let current = element.parentElement; current;
                         current = current.parentElement) {
                        const display = getComputedStyle(current).display;
                        if (["block", "flow-root", "flex", "grid", "list-item",
                             "table", "table-cell", "table-caption"].includes(display)) {
                            return current;
                        }
                        if (current === root) return root;
                    }
                    return root;
                };
                const stableNodeKey = textNode => {
                    const parts = [];
                    const owner = textNode.parentElement;
                    for (let current = owner; current && current !== root;
                         current = current.parentElement) {
                        let sameTagIndex = 0;
                        for (const sibling of current.parentElement?.children || []) {
                            if (sibling.tagName !== current.tagName) continue;
                            if (sibling === current) break;
                            sameTagIndex++;
                        }
                        parts.push(`${current.tagName.toLowerCase()}:${sameTagIndex}`);
                    }
                    let textIndex = 0;
                    for (const sibling of owner.childNodes) {
                        if (sibling === textNode) break;
                        if (sibling.nodeType === Node.TEXT_NODE) textIndex++;
                    }
                    return `${parts.reverse().join("/")}/text:${textIndex}`;
                };
                const bodyContainers = Array.from(root.querySelectorAll(args.body))
                    .filter(element => belongs(element) && !excluded(element) &&
                        cssVisible(element)).length;
                const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                const nodes = [];
                const blockIds = new WeakMap();
                const renderRectKeys = new Set();
                const visibleRectKeys = new Set();
                const aboveRectKeys = new Set();
                const belowRectKeys = new Set();
                let nextBlockId = 0;
                let totalLength = 0;
                let rangeChecks = 0;
                let bodyTextNodesPresent = 0;
                let unsupportedTextNodesPresent = 0;
                let closestTextBelow = null;
                let closestTextAbove = null;
                let closestTextTop = null;
                let closestTextBottom = null;
                let truncated = false;
                let node;
                while ((node = walker.nextNode())) {
                    const parent = node.parentElement;
                    if (!parent || !belongs(parent) || excluded(parent) ||
                        !cssVisible(parent)) continue;
                    if (!node.length) continue;
                    if (!hasBodyContainer(parent)) {
                        if (node.data.trim()) unsupportedTextNodesPresent++;
                        continue;
                    }
                    bodyTextNodesPresent++;
                    const length = Math.min(node.length, args.maxChars - totalLength);
                    if (length <= 0) { truncated = true; break; }
                    if (length < node.length) truncated = true;
                    totalLength += length;
                    const block = nearestBlock(node);
                    if (!blockIds.has(block)) blockIds.set(block, nextBlockId++);
                    const clip = clipFor(parent);
                    const metrics = scrollMetrics(root);
                    const expected = [];
                    const visible = [];
                    let deferredAbove = 0;
                    let deferredBelow = 0;
                    let deferredOther = 0;
                    let nearestBelow = null;
                    let frontier = null;
                    const range = document.createRange();
                    const inspect = (start, end) => {
                        if (rangeChecks++ >= args.maxRangeChecks) {
                            truncated = true;
                            return;
                        }
                        range.setStart(node, start);
                        range.setEnd(node, end);
                        const rects = Array.from(range.getClientRects()).filter(rect =>
                            rect.width > 0 && rect.height > 0
                        );
                        if (!rects.length) return;
                        expected.push([start, end]);
                        const rectIdentity = stableNodeKey(node);
                        const states = rects.map(rect => {
                            const key = `${rectIdentity}:${Math.round(rect.left * 2) / 2}:` +
                                `${Math.round(rect.top * 2) / 2}:${Math.round(rect.right * 2) / 2}:` +
                                `${Math.round(rect.bottom * 2) / 2}`;
                            const visible = rect.right > clip.left &&
                                rect.left < clip.right && rect.bottom > clip.top &&
                                rect.top < clip.bottom;
                            renderRectKeys.add(key);
                            if (visible) visibleRectKeys.add(key);
                            else if (rect.bottom <= clip.top) aboveRectKeys.add(key);
                            else if (rect.top >= clip.bottom) belowRectKeys.add(key);
                            return {
                                visible,
                                fully: rect.left >= clip.left &&
                                    rect.right <= clip.right && rect.top >= clip.top &&
                                    rect.bottom <= clip.bottom,
                            };
                        });
                        for (const rect of rects) {
                            if (rect.top >= clip.bottom) {
                                const distance = Math.max(0, rect.top - clip.bottom);
                                if (distance < (closestTextBelow ?? Infinity)) {
                                    closestTextBelow = distance;
                                    closestTextTop = rect.top;
                                    closestTextBottom = rect.bottom;
                                }
                            } else if (rect.bottom <= clip.top) {
                                const distance = Math.max(0, clip.top - rect.bottom);
                                if (distance < (closestTextAbove ?? Infinity)) {
                                    closestTextAbove = distance;
                                    closestTextTop = rect.top;
                                    closestTextBottom = rect.bottom;
                                }
                            } else if (closestTextTop === null) {
                                closestTextTop = rect.top;
                                closestTextBottom = rect.bottom;
                            }
                        }
                        if (states.every(state => state.fully)) {
                            const text = node.data.slice(start, end);
                            const top = Math.min(...rects.map(rect => rect.top));
                            const bottom = Math.max(...rects.map(rect => rect.bottom));
                            visible.push({
                                start, end, text, top,
                                contentY: bottom + metrics.offset,
                            });
                            frontier = Math.max(frontier ?? -Infinity,
                                bottom + metrics.offset);
                            return;
                        }
                        if (states.every(state => !state.visible)) {
                            const above = rects.every(rect => rect.bottom <= clip.top);
                            const below = rects.every(rect => rect.top >= clip.bottom);
                            if (above) deferredAbove++;
                            else if (below) {
                                deferredBelow++;
                                const distance = Math.max(0,
                                    Math.min(...rects.map(rect => rect.top)) - clip.bottom);
                                nearestBelow = Math.min(nearestBelow ?? Infinity, distance);
                            } else deferredOther++;
                            return;
                        }
                        if (end - start <= 1) {
                            const top = Math.min(...rects.map(rect => rect.top));
                            const bottom = Math.max(...rects.map(rect => rect.bottom));
                            visible.push({
                                start, end, text: node.data.slice(start, end), top,
                                contentY: bottom + metrics.offset,
                            });
                            frontier = Math.max(frontier ?? -Infinity,
                                bottom + metrics.offset);
                            return;
                        }
                        let middle = start + Math.floor((end - start) / 2);
                        if (middle > start && middle < end) {
                            const before = node.data.charCodeAt(middle - 1);
                            const after = node.data.charCodeAt(middle);
                            if (before >= 0xD800 && before <= 0xDBFF &&
                                after >= 0xDC00 && after <= 0xDFFF) {
                                middle++;
                            }
                        }
                        inspect(start, middle);
                        inspect(middle, end);
                    };
                    inspect(0, length);
                    range.detach();
                    const mergedVisible = [];
                    for (const piece of visible.sort((a, b) => a.start - b.start)) {
                        const previous = mergedVisible[mergedVisible.length - 1];
                        if (previous && previous.end === piece.start &&
                            Math.abs(previous.top - piece.top) < 0.5 &&
                            Math.abs(previous.contentY - piece.contentY) < 0.5) {
                            previous.end = piece.end;
                            previous.text += piece.text;
                        } else {
                            mergedVisible.push({...piece});
                        }
                    }
                    nodes.push({
                        index: nodes.length,
                        key: stableNodeKey(node),
                        length,
                        block: blockIds.get(block),
                        expected,
                        visible: mergedVisible,
                        deferredAbove,
                        deferredBelow,
                        deferredOther,
                        nearestBelow,
                        frontier,
                    });
                    if (truncated) break;
                    if (nodes.length >= args.maxNodes) { truncated = true; break; }
                }
                const closestTextSide = closestTextBelow !== null &&
                    (closestTextAbove === null || closestTextBelow <= closestTextAbove)
                    ? "BELOW" : closestTextAbove !== null ? "ABOVE" : "NONE";
                const closestTextDistance = closestTextSide === "BELOW"
                    ? closestTextBelow : closestTextSide === "ABOVE"
                    ? closestTextAbove : null;
                let textEvidence = "NO_BODY_NODES";
                if (visibleRectKeys.size) textEvidence = "TEXT_IN_VIEW";
                else if (renderRectKeys.size && belowRectKeys.size &&
                    (!aboveRectKeys.size || closestTextBelow <= closestTextAbove)) {
                    textEvidence = "TEXT_BELOW";
                } else if (renderRectKeys.size && aboveRectKeys.size) {
                    textEvidence = "TEXT_ABOVE";
                } else if (renderRectKeys.size) {
                    textEvidence = "MEDIA_OR_WHITESPACE_GAP";
                } else if (bodyTextNodesPresent || bodyContainers) {
                    textEvidence = bodyTextNodesPresent
                        ? "MEDIA_OR_WHITESPACE_GAP" : "NO_BODY_NODES";
                } else if (unsupportedTextNodesPresent) {
                    textEvidence = "UNSUPPORTED_STRUCTURE";
                }
                return {
                    nodes, truncated, bodyContainers, bodyTextNodesPresent,
                    unsupportedTextNodesPresent,
                    renderRectCount: renderRectKeys.size,
                    visibleTextRectCount: visibleRectKeys.size,
                    deferredAboveRectCount: aboveRectKeys.size,
                    deferredBelowRectCount: belowRectKeys.size,
                    closestTextDistance,
                    closestTextSide,
                    rootIntersects,
                    rootPositionBand,
                    scrollTop: scrollMetrics(root).top,
                    scrollHeight: scrollMetrics(root).scrollHeight,
                    scrollSurfaceKey: scrollMetrics(root).key,
                    rowTop: rootRect.top,
                    rowBottom: rootRect.bottom,
                    clipTop: rootClip.top,
                    clipBottom: rootClip.bottom,
                    closestTextTop,
                    closestTextBottom,
                    textEvidence,
                };
            }"""),
            {
                "roots": _FEED_ROOT_SELECTOR,
                "body": _BODY_TEXT_SELECTOR,
                "excluded": _BODY_EXCLUSION_SELECTOR,
                "maxChars": _MAX_BODY_CODEPOINTS,
                "maxNodes": _MAX_BODY_TEXT_NODES,
                "maxRangeChecks": _MAX_BODY_RANGE_CHECKS,
                "isolatedScope": isolated_scope,
            },
        )

        read_evidence = BodyReadEvidence(
            state=str(observation["textEvidence"]),
            body_text_nodes_present=int(observation["bodyTextNodesPresent"]),
            render_rect_count=int(observation["renderRectCount"]),
            visible_text_rect_count=int(observation["visibleTextRectCount"]),
            deferred_above_rect_count=int(observation["deferredAboveRectCount"]),
            deferred_below_rect_count=int(observation["deferredBelowRectCount"]),
            closest_text_distance_px=(
                None
                if observation["closestTextDistance"] is None
                else float(observation["closestTextDistance"])
            ),
            closest_text_side=str(observation["closestTextSide"]),
            row_top_px=float(observation["rowTop"]),
            row_bottom_px=float(observation["rowBottom"]),
            clip_top_px=float(observation["clipTop"]),
            clip_bottom_px=float(observation["clipBottom"]),
            text_rect_top_px=(
                None
                if observation["closestTextTop"] is None
                else float(observation["closestTextTop"])
            ),
            text_rect_bottom_px=(
                None
                if observation["closestTextBottom"] is None
                else float(observation["closestTextBottom"])
            ),
            root_intersects=bool(observation["rootIntersects"]),
            root_position_band=str(observation["rootPositionBand"]),
            scroller_scroll_top=float(observation["scrollTop"]),
            scroll_surface_key=str(observation["scrollSurfaceKey"]),
        )
        state: dict[str, _BodyTextProgress] | None = None
        if fingerprint is not None:
            state = self._body_text_progress.get(fingerprint)
            if state is None:
                if len(self._body_text_progress) >= _MAX_BODY_PROGRESS_POSTS:
                    return _VisibleBodyRead(
                        None,
                        False,
                        False,
                        truncated=True,
                        read_evidence=read_evidence,
                    )
                state = {}
                self._body_text_progress[fingerprint] = state
        progress_state = {} if state is None else state

        new_segments: list[VisibleBodySegment] = []
        has_rendered_text = False
        content_changed = False
        deferred_above = 0
        deferred_below = 0
        nearest_deferred_below: float | None = None
        frontier_content: float | None = None
        for node in observation["nodes"]:
            order = int(node["index"])
            identity = str(node["key"])
            length = int(node["length"])
            block = int(node["block"])
            expected = self._merge_ranges(
                [(int(start), int(end)) for start, end in node["expected"]]
            )
            has_rendered_text = has_rendered_text or bool(expected)
            deferred_above += int(node["deferredAbove"])
            deferred_below += int(node["deferredBelow"])
            if node["nearestBelow"] is not None:
                distance = float(node["nearestBelow"])
                nearest_deferred_below = min(
                    nearest_deferred_below if nearest_deferred_below is not None else distance,
                    distance,
                )
            if node["frontier"] is not None:
                frontier_content = max(
                    frontier_content if frontier_content is not None else float(node["frontier"]),
                    float(node["frontier"]),
                )

            progress = progress_state.get(identity)
            if progress is None:
                progress = _BodyTextProgress(length, block, order)
                progress_state[identity] = progress
            elif length < progress.length:
                progress.expected_ranges = [
                    (start, min(end, length))
                    for start, end in progress.expected_ranges
                    if start < length
                ]
                progress.visited_ranges = [
                    (start, min(end, length))
                    for start, end in progress.visited_ranges
                    if start < length
                ]
                progress.fragments = [
                    _TextFragment(
                        fragment.start,
                        min(fragment.end, length),
                        self._utf16_slice(
                            fragment.text, 0, min(fragment.end, length) - fragment.start
                        ),
                        fragment.viewport_y,
                        fragment.content_y,
                    )
                    for fragment in progress.fragments
                    if fragment.start < length
                ]
                progress.length = length
            else:
                progress.length = length
            progress.block = block
            progress.order = order
            progress.expected_ranges = expected

            current_visible = sorted(
                node["visible"],
                key=lambda piece: (int(piece["start"]), int(piece["end"])),
            )
            for piece in current_visible:
                start = int(piece["start"])
                end = int(piece["end"])
                text = str(piece["text"])
                for old in progress.fragments:
                    overlap_start = max(start, old.start)
                    overlap_end = min(end, old.end)
                    if overlap_start >= overlap_end:
                        continue
                    old_text = self._utf16_slice(
                        old.text,
                        overlap_start - old.start,
                        overlap_end - old.start,
                    )
                    new_text = self._utf16_slice(
                        text,
                        overlap_start - start,
                        overlap_end - start,
                    )
                    if old_text != new_text:
                        content_changed = True
                        progress.visited_ranges.clear()
                        progress.fragments.clear()
                        break
                uncovered = self._subtract_ranges(start, end, progress.visited_ranges)
                for new_start, new_end in uncovered:
                    fragment_text = self._utf16_slice(
                        text,
                        new_start - start,
                        new_end - start,
                    )
                    if not fragment_text.strip():
                        progress.visited_ranges = self._merge_ranges(
                            [*progress.visited_ranges, (new_start, new_end)]
                        )
                        progress.fragments.append(
                            _TextFragment(
                                new_start,
                                new_end,
                                fragment_text,
                                float(piece["top"]),
                                float(piece["contentY"]),
                            )
                        )
                        continue
                    progress.visited_ranges = self._merge_ranges(
                        [*progress.visited_ranges, (new_start, new_end)]
                    )
                    progress.fragments.append(
                        _TextFragment(
                            new_start,
                            new_end,
                            fragment_text,
                            float(piece["top"]),
                            float(piece["contentY"]),
                        )
                    )
                    segment_key = sha256(
                        self._body_progress_salt
                        + (f"{fingerprint or 'display'}:{identity}:{new_start}:{new_end}:").encode()
                        + fragment_text.encode("utf-8", errors="replace")
                    ).hexdigest()
                    new_segments.append(
                        VisibleBodySegment(
                            segment_key,
                            fragment_text,
                            float(piece["top"]),
                            float(piece["contentY"]),
                        )
                    )

        has_text_state = has_rendered_text or bool(progress_state)
        body_complete = bool(
            fingerprint is not None
            and progress_state
            and has_rendered_text
            and not observation["truncated"]
            and not content_changed
            and all(
                not self._subtract_ranges(start, end, progress.visited_ranges)
                for progress in progress_state.values()
                for start, end in progress.expected_ranges
            )
        )
        body_blocks: list[tuple[int, list[str]]] = []
        for _identity, progress in sorted(progress_state.items(), key=lambda item: item[1].order):
            if not body_blocks or body_blocks[-1][0] != progress.block:
                body_blocks.append((progress.block, []))
            block_parts = body_blocks[-1][1]
            for fragment in sorted(progress.fragments, key=lambda item: item.start):
                block_parts.append(fragment.text)
        body = "\n".join(
            text
            for text in (_canonicalize_body_block("".join(parts)) for _, parts in body_blocks)
            if text
        )
        scan_counts.new_visible_text_segments += len(new_segments)
        scan_counts.deferred_above += deferred_above
        scan_counts.deferred_below += deferred_below
        if nearest_deferred_below is not None:
            current = scan_counts.nearest_deferred_below_px
            scan_counts.nearest_deferred_below_px = min(
                nearest_deferred_below if current is None else current,
                nearest_deferred_below,
            )
        if frontier_content is not None:
            current_frontier = scan_counts.frontier_content_px
            scan_counts.frontier_content_px = max(
                frontier_content if current_frontier is None else current_frontier,
                frontier_content,
            )
        return _VisibleBodyRead(
            body if body.strip() else None,
            body_complete,
            has_text_state,
            not content_changed,
            tuple(new_segments),
            deferred_above,
            deferred_below,
            nearest_deferred_below,
            frontier_content,
            bool(observation["truncated"]),
            replace(read_evidence, frontier_content_px=frontier_content),
        )

    @staticmethod
    def _subtract_ranges(
        start: int,
        end: int,
        covered: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        remaining: list[tuple[int, int]] = []
        cursor = start
        for old_start, old_end in covered:
            if old_end <= cursor:
                continue
            if old_start >= end:
                break
            if old_start > cursor:
                remaining.append((cursor, min(old_start, end)))
            cursor = max(cursor, old_end)
            if cursor >= end:
                break
        if cursor < end:
            remaining.append((cursor, end))
        return remaining

    @staticmethod
    def _utf16_slice(value: str, start: int, end: int) -> str:
        encoded = value.encode("utf-16-le", errors="surrogatepass")
        return encoded[start * 2 : end * 2].decode("utf-16-le", errors="surrogatepass")

    async def _visible_body_candidate_state(
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
