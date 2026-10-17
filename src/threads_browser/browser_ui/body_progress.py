"""Mutable range coverage state for visible feed body text."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from hashlib import sha256
from typing import Any

from ..feed.models import BodyReadEvidence, VisibleBodySegment
from .body_progress_state import (
    _MAX_BODY_PROGRESS_POSTS,
    BodyProgressState,
    _BodyTextProgress,
    _canonicalize_body_block,  # noqa: F401 - retained module compatibility name
    _CoverageSummary,
    _ProgressMetrics,
    _TextFragment,  # noqa: F401 - retained module compatibility name
)
from .feed_rows import _FeedScanCounts


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


class BodyProgressTracker(BodyProgressState):
    """Own run-local text fragments and the evidence that they cover the body."""

    @staticmethod
    def _read_evidence(observation: dict[str, Any]) -> BodyReadEvidence:
        return BodyReadEvidence(
            state=str(observation["textEvidence"]),
            body_text_nodes_present=int(observation["bodyTextNodesPresent"]),
            render_rect_count=int(observation["renderRectCount"]),
            visible_text_rect_count=int(observation["visibleTextRectCount"]),
            fully_contained_text_rect_count=int(observation["fullyContainedTextRectCount"]),
            intersect_only_text_rect_count=int(observation["intersectOnlyTextRectCount"]),
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

    def _progress_state(
        self,
        fingerprint: str | None,
        read_evidence: BodyReadEvidence,
    ) -> tuple[dict[str, _BodyTextProgress], bool]:
        if fingerprint is None:
            return {}, False
        state = self._body_text_progress.get(fingerprint)
        if state is not None:
            return state, False
        if len(self._body_text_progress) >= _MAX_BODY_PROGRESS_POSTS:
            return {}, True
        state = {}
        self._body_text_progress[fingerprint] = state
        return state, False

    @staticmethod
    def _limited_read(read_evidence: BodyReadEvidence) -> _VisibleBodyRead:
        return _VisibleBodyRead(
            None,
            False,
            False,
            truncated=True,
            read_evidence=replace(
                read_evidence,
                body_incomplete_reason="LIMIT",
                expected_range_count=0,
                visited_range_count=0,
                missing_range_count=0,
            ),
        )

    def _incomplete_reason(
        self,
        *,
        body_complete: bool,
        metrics: _ProgressMetrics,
        summary: _CoverageSummary,
        observation: dict[str, Any],
    ) -> str:
        if body_complete:
            return "NONE"
        if metrics.content_changed:
            return "CONTENT_CHANGED"
        if observation["limitReached"]:
            return "LIMIT"
        if observation["truncated"]:
            return "TRUNCATED"
        if summary.dom_replaced:
            return "DOM_REPLACED"
        if summary.missing_ranges:
            return "UNVISITED_RANGE"
        return "STABILITY_PENDING"

    def _finalize_evidence(
        self,
        read_evidence: BodyReadEvidence,
        summary: _CoverageSummary,
        metrics: _ProgressMetrics,
        fingerprint: str | None,
        body_complete: bool,
        observation: dict[str, Any],
    ) -> BodyReadEvidence:
        unrecoverable_signature = (
            sha256(
                self._body_progress_salt
                + (
                    f"{fingerprint or 'display'}:" + ";".join(sorted(summary.unrecoverable_tokens))
                ).encode()
            ).hexdigest()
            if summary.unrecoverable_tokens
            else ""
        )
        return replace(
            read_evidence,
            body_incomplete_reason=self._incomplete_reason(
                body_complete=body_complete,
                metrics=metrics,
                summary=summary,
                observation=observation,
            ),
            expected_range_count=summary.expected_ranges,
            visited_range_count=summary.visited_ranges,
            missing_range_count=summary.missing_ranges,
            missing_range_topology=summary.missing_topology,
            unrecoverable_range_count=len(summary.unrecoverable_tokens),
            unrecoverable_range_signature=unrecoverable_signature,
        )

    @staticmethod
    def _update_scan_counts(
        scan_counts: _FeedScanCounts,
        metrics: _ProgressMetrics,
    ) -> None:
        scan_counts.new_visible_text_segments += len(metrics.new_segments)
        scan_counts.deferred_above += metrics.deferred_above
        scan_counts.deferred_below += metrics.deferred_below
        if metrics.nearest_deferred_below is not None:
            current = scan_counts.nearest_deferred_below_px
            scan_counts.nearest_deferred_below_px = min(
                metrics.nearest_deferred_below if current is None else current,
                metrics.nearest_deferred_below,
            )
        if metrics.frontier_content is not None:
            current_frontier = scan_counts.frontier_content_px
            scan_counts.frontier_content_px = max(
                metrics.frontier_content if current_frontier is None else current_frontier,
                metrics.frontier_content,
            )

    def consume(
        self,
        observation: dict[str, Any],
        *,
        fingerprint: str | None,
        scan_counts: _FeedScanCounts,
    ) -> _VisibleBodyRead:
        read_evidence = self._read_evidence(observation)
        progress_state, limited = self._progress_state(fingerprint, read_evidence)
        if limited:
            return self._limited_read(read_evidence)

        metrics = _ProgressMetrics()
        for node in observation["nodes"]:
            self._record_node(
                node,
                progress_state=progress_state,
                fingerprint=fingerprint,
                metrics=metrics,
            )

        summary = self._coverage_summary(
            progress_state,
            metrics.current_identities,
        )
        body_complete = self._body_is_complete(
            fingerprint,
            progress_state,
            metrics,
            observation,
        )
        read_evidence = self._finalize_evidence(
            read_evidence,
            summary,
            metrics,
            fingerprint,
            body_complete,
            observation,
        )
        body = self._body_text(progress_state)
        self._update_scan_counts(scan_counts, metrics)
        return _VisibleBodyRead(
            body if body.strip() else None,
            body_complete,
            metrics.has_rendered_text or bool(progress_state),
            not metrics.content_changed,
            tuple(metrics.new_segments),
            metrics.deferred_above,
            metrics.deferred_below,
            metrics.nearest_deferred_below,
            metrics.frontier_content,
            bool(observation["truncated"]),
            replace(read_evidence, frontier_content_px=metrics.frontier_content),
        )
