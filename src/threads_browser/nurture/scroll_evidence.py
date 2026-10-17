"""Attribute visible-body geometry changes to a measured feed scroll."""

from __future__ import annotations

from ..feed.models import BodyReadEvidence
from ..reader import ThreadsHomeReader, _HomeRowObservation


def annotate_scroll_evidence(
    reader: ThreadsHomeReader,
    row: _HomeRowObservation,
    pending_scroll_baseline: dict[str, object] | None,
) -> bool:
    """Annotate row movement and report whether it advanced a deferred body."""
    diagnostic = row.diagnostic
    evidence = diagnostic.read_evidence
    previous_evidence = _previous_body_evidence(diagnostic.observation_id, pending_scroll_baseline)
    if evidence is None or previous_evidence is None:
        return False

    same_scroller = (
        evidence.scroll_surface_key == previous_evidence.scroll_surface_key
        and evidence.scroller_scroll_top is not None
        and previous_evidence.scroller_scroll_top is not None
    )
    if not same_scroller:
        if reader.last_scroll_surface_key is not None:
            diagnostic.wheel_surface_matches = False
            diagnostic.scroll_reason_flags = ("SCROLLER_MISMATCH",)
        return False

    scroll_top_delta = evidence.scroller_scroll_top - previous_evidence.scroller_scroll_top
    diagnostic.same_scroller_scroll_top_delta = scroll_top_delta
    scroller_moved = abs(scroll_top_delta) > 1
    wheel_surface_matches = (
        reader.last_scroll_surface_key is not None
        and evidence.scroll_surface_key == reader.last_scroll_surface_key
    )
    diagnostic.wheel_surface_matches = wheel_surface_matches
    diagnostic.row_top_before_px = previous_evidence.row_top_px
    diagnostic.row_bottom_before_px = previous_evidence.row_bottom_px
    diagnostic.clip_top_before_px = previous_evidence.clip_top_px
    diagnostic.clip_bottom_before_px = previous_evidence.clip_bottom_px
    diagnostic.text_top_before_px = previous_evidence.text_rect_top_px
    diagnostic.text_bottom_before_px = previous_evidence.text_rect_bottom_px

    flags: list[str] = []
    if not wheel_surface_matches:
        flags.append("SCROLLER_MISMATCH")
    row_delta = _row_delta(evidence, previous_evidence)
    if row_delta is not None:
        diagnostic.row_movement_px = row_delta
    distance_decreased = _text_distance_decreased(evidence, previous_evidence)
    if (
        previous_evidence.closest_text_distance_px is not None
        and evidence.closest_text_distance_px is not None
    ):
        diagnostic.text_distance_delta_px = (
            evidence.closest_text_distance_px - previous_evidence.closest_text_distance_px
        )
    row_moved_toward_view = bool(
        wheel_surface_matches
        and scroller_moved
        and row_delta is not None
        and row_delta < -1
        and distance_decreased
    )
    if row_moved_toward_view:
        flags.append("ROW_MOVED_TOWARD_VIEW")
    same_row_position = row_delta is not None and abs(row_delta) <= 1
    same_text_position = (
        previous_evidence.text_rect_top_px is not None
        and evidence.text_rect_top_px is not None
        and abs(previous_evidence.text_rect_top_px - evidence.text_rect_top_px) <= 1
    )
    if scroller_moved and same_row_position and same_text_position:
        flags.append("SCROLLTOP_MOVED_ROW_STATIC")
    if _layout_shift(reader, scroll_top_delta, row_delta):
        flags.append("LAYOUT_SHIFT")
    if flags:
        diagnostic.scroll_reason_flags = tuple(flags)
    return row_moved_toward_view


def _previous_body_evidence(
    observation_id: str | None,
    pending_scroll_baseline: dict[str, object] | None,
) -> BodyReadEvidence | None:
    if pending_scroll_baseline is None or not observation_id:
        return None
    previous_rows = pending_scroll_baseline.get("rows", {})
    if not isinstance(previous_rows, dict):
        return None
    candidate = previous_rows.get(observation_id)
    return candidate if isinstance(candidate, BodyReadEvidence) else None


def _row_delta(
    evidence: BodyReadEvidence,
    previous: BodyReadEvidence,
) -> float | None:
    if (
        evidence.row_top_px is None
        or evidence.clip_top_px is None
        or previous.row_top_px is None
        or previous.clip_top_px is None
    ):
        return None
    return (evidence.row_top_px - evidence.clip_top_px) - (
        previous.row_top_px - previous.clip_top_px
    )


def _text_distance_decreased(
    evidence: BodyReadEvidence,
    previous: BodyReadEvidence,
) -> bool:
    return bool(
        previous.closest_text_side == "BELOW"
        and evidence.closest_text_side == "BELOW"
        and previous.closest_text_distance_px is not None
        and evidence.closest_text_distance_px is not None
        and evidence.closest_text_distance_px < previous.closest_text_distance_px - 1
    )


def _layout_shift(
    reader: ThreadsHomeReader,
    scroll_top_delta: float,
    row_delta: float | None,
) -> bool:
    scroll_geometry = reader.last_scroll_geometry or {}
    height_delta = scroll_geometry.get("scroll_height_delta")
    expected_row_delta = -scroll_top_delta
    return bool(
        row_delta is not None
        and abs(row_delta - expected_row_delta) > max(24, abs(scroll_top_delta) * 0.25)
    ) or (isinstance(height_delta, (int, float)) and abs(height_delta) > 1)
