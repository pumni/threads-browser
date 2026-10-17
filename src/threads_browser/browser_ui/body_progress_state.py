"""Mutable text-range state and geometry evidence for visible body reads."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from ..feed.models import VisibleBodySegment

_MAX_BODY_PROGRESS_POSTS = 200

_RANGE_TOPOLOGY_CODES = frozenset(
    {
        "missing_vertical_above",
        "missing_vertical_below",
        "missing_horizontal_left",
        "missing_horizontal_right",
        "missing_inside_clip",
        "missing_mixed",
        "indivisible_codepoint",
        "limit",
    }
)

_UNRECOVERABLE_RANGE_CODES = frozenset(
    {
        "missing_vertical_above",
        "missing_horizontal_left",
        "missing_horizontal_right",
        "missing_inside_clip",
        "limit",
    }
)


def _canonicalize_body_block(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


@dataclass(slots=True, repr=False)
class _BodyTextProgress:
    length: int
    block: int
    order: int
    expected_ranges: list[tuple[int, int]] = field(default_factory=list, repr=False)
    visited_ranges: list[tuple[int, int]] = field(default_factory=list, repr=False)
    range_evidence: list[tuple[int, int, tuple[str, ...]]] = field(
        default_factory=list,
        repr=False,
    )
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


@dataclass(slots=True)
class _ProgressMetrics:
    new_segments: list[VisibleBodySegment] = field(default_factory=list)
    current_identities: set[str] = field(default_factory=set)
    has_rendered_text: bool = False
    content_changed: bool = False
    deferred_above: int = 0
    deferred_below: int = 0
    nearest_deferred_below: float | None = None
    frontier_content: float | None = None


@dataclass(frozen=True, slots=True)
class _CoverageSummary:
    expected_ranges: int
    visited_ranges: int
    missing_ranges: int
    dom_replaced: bool
    missing_topology: tuple[str, ...]
    unrecoverable_tokens: tuple[str, ...]


class BodyProgressState:
    """Own bounded, run-local body fragments and their coverage geometry."""

    def __init__(self) -> None:
        self._body_text_progress: dict[str, dict[str, _BodyTextProgress]] = {}
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

    def _record_node(
        self,
        node: dict[str, Any],
        *,
        progress_state: dict[str, _BodyTextProgress],
        fingerprint: str | None,
        metrics: _ProgressMetrics,
    ) -> None:
        order = int(node["index"])
        identity = str(node["key"])
        metrics.current_identities.add(identity)
        length = int(node["length"])
        block = int(node["block"])
        range_evidence = [
            (
                int(item["start"]),
                int(item["end"]),
                tuple(str(code) for code in item["topology"] if str(code) in _RANGE_TOPOLOGY_CODES),
            )
            for item in node["rangeEvidence"]
        ]
        expected = self._merge_ranges([(int(start), int(end)) for start, end in node["expected"]])
        metrics.has_rendered_text |= bool(expected)
        metrics.deferred_above += int(node["deferredAbove"])
        metrics.deferred_below += int(node["deferredBelow"])
        if node["nearestBelow"] is not None:
            distance = float(node["nearestBelow"])
            metrics.nearest_deferred_below = min(
                distance
                if metrics.nearest_deferred_below is None
                else metrics.nearest_deferred_below,
                distance,
            )
        if node["frontier"] is not None:
            frontier = float(node["frontier"])
            metrics.frontier_content = max(
                frontier if metrics.frontier_content is None else metrics.frontier_content,
                frontier,
            )
        progress = progress_state.get(identity)
        if progress is None:
            progress = _BodyTextProgress(length, block, order)
            progress_state[identity] = progress
        elif length < progress.length:
            self._truncate_progress(progress, length)
        else:
            progress.length = length
        progress.block = block
        progress.order = order
        progress.expected_ranges = self._merge_ranges([*progress.expected_ranges, *expected])
        progress.range_evidence = [
            item
            for item in progress.range_evidence
            if not any(
                item[0] < expected_end and item[1] > expected_start
                for expected_start, expected_end in expected
            )
        ]
        progress.range_evidence.extend(range_evidence)
        new_segments, content_changed = self._record_visible_fragments(
            progress,
            node["visible"],
            identity=identity,
            fingerprint=fingerprint,
        )
        metrics.new_segments.extend(new_segments)
        metrics.content_changed |= content_changed

    def _truncate_progress(self, progress: _BodyTextProgress, length: int) -> None:
        progress.expected_ranges = [
            (start, min(end, length)) for start, end in progress.expected_ranges if start < length
        ]
        progress.visited_ranges = [
            (start, min(end, length)) for start, end in progress.visited_ranges if start < length
        ]
        progress.range_evidence = [
            (start, min(end, length), topology)
            for start, end, topology in progress.range_evidence
            if start < length
        ]
        progress.fragments = [
            _TextFragment(
                fragment.start,
                min(fragment.end, length),
                self._utf16_slice(fragment.text, 0, min(fragment.end, length) - fragment.start),
                fragment.viewport_y,
                fragment.content_y,
            )
            for fragment in progress.fragments
            if fragment.start < length
        ]
        progress.length = length

    def _record_visible_fragments(
        self,
        progress: _BodyTextProgress,
        visible: list[dict[str, Any]],
        *,
        identity: str,
        fingerprint: str | None,
    ) -> tuple[list[VisibleBodySegment], bool]:
        new_segments: list[VisibleBodySegment] = []
        content_changed = False
        pieces = sorted(visible, key=lambda piece: (int(piece["start"]), int(piece["end"])))
        for piece in pieces:
            start = int(piece["start"])
            end = int(piece["end"])
            text = str(piece["text"])
            if self._visible_content_changed(progress, start, end, text):
                content_changed = True
                progress.visited_ranges.clear()
                progress.fragments.clear()
            for new_start, new_end in self._subtract_ranges(start, end, progress.visited_ranges):
                fragment_text = self._utf16_slice(text, new_start - start, new_end - start)
                progress.visited_ranges = self._merge_ranges(
                    [*progress.visited_ranges, (new_start, new_end)]
                )
                viewport_y = float(piece["top"])
                content_y = float(piece["contentY"])
                progress.fragments.append(
                    _TextFragment(
                        new_start,
                        new_end,
                        fragment_text,
                        viewport_y,
                        content_y,
                    )
                )
                if fragment_text.strip():
                    new_segments.append(
                        VisibleBodySegment(
                            self._segment_key(
                                fingerprint, identity, new_start, new_end, fragment_text
                            ),
                            fragment_text,
                            viewport_y,
                            content_y,
                        )
                    )
        return new_segments, content_changed

    def _visible_content_changed(
        self,
        progress: _BodyTextProgress,
        start: int,
        end: int,
        text: str,
    ) -> bool:
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
                return True
        return False

    def _segment_key(
        self,
        fingerprint: str | None,
        identity: str,
        start: int,
        end: int,
        text: str,
    ) -> str:
        return sha256(
            self._body_progress_salt
            + (f"{fingerprint or 'display'}:{identity}:{start}:{end}:").encode()
            + text.encode("utf-8", errors="replace")
        ).hexdigest()

    def _coverage_summary(
        self,
        progress_state: dict[str, _BodyTextProgress],
        current_identities: set[str],
    ) -> _CoverageSummary:
        expected_count = 0
        visited_count = 0
        missing_count = 0
        dom_replaced = False
        topology_summary: set[str] = set()
        unrecoverable_tokens: list[str] = []
        for identity, progress in progress_state.items():
            expected_count += len(progress.expected_ranges)
            visited_count += len(self._merge_ranges(progress.visited_ranges))
            for start, end in progress.expected_ranges:
                missing_ranges = self._subtract_ranges(start, end, progress.visited_ranges)
                missing_count += len(missing_ranges)
                for missing_start, missing_end in missing_ranges:
                    topology = self._missing_range_topology(progress, missing_start, missing_end)
                    topology_summary.update(topology)
                    if topology & _UNRECOVERABLE_RANGE_CODES:
                        unrecoverable_tokens.append(
                            f"{identity}:{missing_start}:{missing_end}:{','.join(sorted(topology))}"
                        )
                if identity not in current_identities and missing_ranges:
                    dom_replaced = True
        return _CoverageSummary(
            expected_count,
            visited_count,
            missing_count,
            dom_replaced,
            tuple(sorted(topology_summary)),
            tuple(unrecoverable_tokens),
        )

    @staticmethod
    def _missing_range_topology(
        progress: _BodyTextProgress,
        missing_start: int,
        missing_end: int,
    ) -> set[str]:
        topology = {
            code
            for evidence_start, evidence_end, codes in progress.range_evidence
            if evidence_start < missing_end and evidence_end > missing_start
            for code in codes
        }
        if not topology:
            topology.add("missing_inside_clip")
        if (
            len(
                topology
                & {
                    "missing_vertical_above",
                    "missing_vertical_below",
                    "missing_horizontal_left",
                    "missing_horizontal_right",
                }
            )
            > 1
        ):
            topology.add("missing_mixed")
        return topology

    @staticmethod
    def _body_is_complete(
        fingerprint: str | None,
        progress_state: dict[str, _BodyTextProgress],
        metrics: _ProgressMetrics,
        observation: dict[str, Any],
    ) -> bool:
        return bool(
            fingerprint is not None
            and progress_state
            and metrics.has_rendered_text
            and not observation["truncated"]
            and not metrics.content_changed
            and all(
                not BodyProgressState._subtract_ranges(start, end, progress.visited_ranges)
                for progress in progress_state.values()
                for start, end in progress.expected_ranges
            )
        )

    @staticmethod
    def _body_text(progress_state: dict[str, _BodyTextProgress]) -> str:
        body_blocks: list[tuple[int, list[str]]] = []
        for _identity, progress in sorted(progress_state.items(), key=lambda item: item[1].order):
            if not body_blocks or body_blocks[-1][0] != progress.block:
                body_blocks.append((progress.block, []))
            block_parts = body_blocks[-1][1]
            for fragment in sorted(progress.fragments, key=lambda item: item.start):
                block_parts.append(fragment.text)
        return "\n".join(
            text
            for text in (_canonicalize_body_block("".join(parts)) for _, parts in body_blocks)
            if text
        )
