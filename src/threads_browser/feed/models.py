"""Value objects for post and viewport observations; no navigation side effects."""

from __future__ import annotations

from dataclasses import dataclass, field

from playwright.async_api import Locator


@dataclass(frozen=True, slots=True, repr=False)
class VisibleBodySegment:
    """A newly visible piece of a post body; all text stays out of reprs."""

    segment_id: str = field(repr=False)
    text: str = field(repr=False)
    viewport_y: float = field(default=0.0, repr=False)
    content_y: float = field(default=0.0, repr=False)

    def __repr__(self) -> str:
        return "VisibleBodySegment(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class BodyReadEvidence:
    """Structural evidence about where rendered body text is located."""

    state: str = "UNKNOWN"
    body_text_nodes_present: int | None = 0
    render_rect_count: int | None = 0
    visible_text_rect_count: int | None = 0
    deferred_above_rect_count: int | None = 0
    deferred_below_rect_count: int | None = 0
    closest_text_distance_px: float | None = None
    closest_text_side: str = "NONE"
    row_top_px: float | None = field(default=None, repr=False)
    row_bottom_px: float | None = field(default=None, repr=False)
    clip_top_px: float | None = field(default=None, repr=False)
    clip_bottom_px: float | None = field(default=None, repr=False)
    text_rect_top_px: float | None = field(default=None, repr=False)
    text_rect_bottom_px: float | None = field(default=None, repr=False)
    frontier_content_px: float | None = field(default=None, repr=False)
    root_intersects: bool | None = False
    root_position_band: str = "UNKNOWN"
    scroller_scroll_top: float | None = field(default=None, repr=False)
    scroll_surface_key: str = field(default="", repr=False)

    def __repr__(self) -> str:
        return f"BodyReadEvidence(state={self.state!r}, structural_counts=<safe>)"


@dataclass(frozen=True, slots=True, repr=False)
class PostTarget:
    url: str = field(repr=False)
    author: str = field(repr=False)
    body: str | None = field(repr=False)
    fingerprint: str
    permalink: Locator = field(repr=False)
    body_available: bool = field(default=False, repr=False)
    currently_visible: bool = field(default=False, repr=False)
    viewport_y: float = field(default=0.0, repr=False)
    body_truncated: bool = field(default=False, repr=False)
    body_complete: bool = field(default=True, repr=False)
    body_consistent: bool = field(default=True, repr=False)
    visible_segments: tuple[VisibleBodySegment, ...] = field(default=(), repr=False)
    read_evidence: BodyReadEvidence | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return "PostTarget(<private>)"


@dataclass(frozen=True, slots=True)
class HomeSample:
    targets: tuple[PostTarget, ...]
    observed_targets: tuple[PostTarget, ...]
    skipped_unverified: int = 0


@dataclass(frozen=True, slots=True, repr=False)
class DisplayObservation:
    """Locally read card text without verified post identity."""

    body: str | None = field(default=None, repr=False)
    status: str = "OBSERVED_UNVERIFIED"
    viewport_y: float = field(default=0.0, repr=False)
    visible_segments: tuple[VisibleBodySegment, ...] = field(default=(), repr=False)
    read_evidence: BodyReadEvidence | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return f"DisplayObservation(status={self.status!r}, body=<private>)"
