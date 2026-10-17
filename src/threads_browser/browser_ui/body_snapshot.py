"""Capture bounded visible text ranges and geometry from a feed row."""

from __future__ import annotations

from typing import Any

from playwright.async_api import Locator

from .feed_rows import _BODY_EXCLUSION_SELECTOR, _BODY_TEXT_SELECTOR, _FEED_ROOT_SELECTOR
from .geometry import with_scrollport_geometry as _with_scrollport_geometry


async def capture_body_snapshot(
    root: Locator,
    *,
    isolated_scope: bool,
    max_chars: int,
    max_nodes: int,
    max_range_checks: int,
    max_range_depth: int,
) -> dict[str, Any]:
    """Read DOM-only range evidence; text merging and coverage live elsewhere."""
    return await root.evaluate(
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
            const textClipFor = element => {
                const clip = clipFor(element);
                const style = getComputedStyle(element);
                if (style.display !== "inline" && style.display !== "contents" &&
                    (style.overflowX !== "visible" ||
                        style.overflowY !== "visible")) {
                    const bounds = element.getBoundingClientRect();
                    const left = bounds.left + element.clientLeft;
                    const top = bounds.top + element.clientTop;
                    if (style.overflowX !== "visible") {
                        clip.left = Math.max(clip.left, left);
                        clip.right = Math.min(clip.right, left + element.clientWidth);
                    }
                    if (style.overflowY !== "visible") {
                        clip.top = Math.max(clip.top, top);
                        clip.bottom = Math.min(clip.bottom, top + element.clientHeight);
                    }
                }
                return clip;
            };
            const rangeTopology = (rects, clip) => {
                const directions = new Set();
                for (const rect of rects) {
                    if (rect.left < clip.left) directions.add("missing_horizontal_left");
                    if (rect.right > clip.right) directions.add("missing_horizontal_right");
                    if (rect.top < clip.top) directions.add("missing_vertical_above");
                    if (rect.bottom > clip.bottom) directions.add("missing_vertical_below");
                }
                if (!directions.size) directions.add("missing_inside_clip");
                if (directions.size > 1) directions.add("missing_mixed");
                return [...directions];
            };
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
            const fullyContainedRectKeys = new Set();
            const intersectOnlyRectKeys = new Set();
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
            let limitReached = false;
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
                const clip = textClipFor(parent);
                const metrics = scrollMetrics(root);
                const expected = [];
                const visible = [];
                let deferredAbove = 0;
                let deferredBelow = 0;
                let deferredOther = 0;
                let nearestBelow = null;
                let frontier = null;
                const rangeEvidence = [];
                const recordRangeEvidence = (start, end, topology) => {
                    rangeEvidence.push({start, end, topology});
                };
                const range = document.createRange();
                const inspect = (start, end, depth = 0) => {
                    if (depth >= args.maxRangeDepth) {
                        truncated = true;
                        limitReached = true;
                        recordRangeEvidence(start, end, ["limit"]);
                        return;
                    }
                    if (rangeChecks++ >= args.maxRangeChecks) {
                        truncated = true;
                        limitReached = true;
                        recordRangeEvidence(start, end, ["limit"]);
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
                        const fully = rect.left >= clip.left &&
                            rect.right <= clip.right && rect.top >= clip.top &&
                            rect.bottom <= clip.bottom;
                        renderRectKeys.add(key);
                        if (visible) {
                            visibleRectKeys.add(key);
                            if (fully) fullyContainedRectKeys.add(key);
                            else intersectOnlyRectKeys.add(key);
                        }
                        else if (rect.bottom <= clip.top) aboveRectKeys.add(key);
                        else if (rect.top >= clip.bottom) belowRectKeys.add(key);
                        return {
                            visible,
                            fully,
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
                        if (above) {
                            deferredAbove++;
                            recordRangeEvidence(start, end, ["missing_vertical_above"]);
                        }
                        else if (below) {
                            deferredBelow++;
                            recordRangeEvidence(start, end, ["missing_vertical_below"]);
                            const distance = Math.max(0,
                                Math.min(...rects.map(rect => rect.top)) - clip.bottom);
                            nearestBelow = Math.min(nearestBelow ?? Infinity, distance);
                        } else {
                            deferredOther++;
                            recordRangeEvidence(start, end, rangeTopology(rects, clip));
                        }
                        return;
                    }
                    if (end - start <= 1) {
                        deferredOther++;
                        const topology = rangeTopology(rects, clip);
                        recordRangeEvidence(start, end, [...new Set(topology)]);
                        return;
                    }
                    let middle = start + Math.floor((end - start) / 2);
                    let indivisibleCodepoint = false;
                    if (middle > start && middle < end) {
                        const before = node.data.charCodeAt(middle - 1);
                        const after = node.data.charCodeAt(middle);
                        if (before >= 0xD800 && before <= 0xDBFF &&
                            after >= 0xDC00 && after <= 0xDFFF) {
                            middle++;
                            indivisibleCodepoint = middle >= end;
                        }
                    }
                    if (middle <= start || middle >= end) {
                        deferredOther++;
                        const topology = rangeTopology(rects, clip);
                        if (indivisibleCodepoint) topology.push("indivisible_codepoint");
                        recordRangeEvidence(start, end, [...new Set(topology)]);
                        return;
                    }
                    inspect(start, middle, depth + 1);
                    inspect(middle, end, depth + 1);
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
                    rangeEvidence,
                });
                if (truncated) break;
                if (nodes.length >= args.maxNodes) {
                    truncated = true;
                    limitReached = true;
                    break;
                }
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
                nodes, truncated, limitReached, bodyContainers, bodyTextNodesPresent,
                unsupportedTextNodesPresent,
                renderRectCount: renderRectKeys.size,
                visibleTextRectCount: visibleRectKeys.size,
                fullyContainedTextRectCount: fullyContainedRectKeys.size,
                intersectOnlyTextRectCount: intersectOnlyRectKeys.size,
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
            "maxChars": max_chars,
            "maxNodes": max_nodes,
            "maxRangeChecks": max_range_checks,
            "maxRangeDepth": max_range_depth,
            "isolatedScope": isolated_scope,
        },
    )
