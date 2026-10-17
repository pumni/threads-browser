"""Ephemeral data passed between one candidate row's scan steps."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from playwright.async_api import JSHandle, Locator

from ..feed.models import BodyReadEvidence, DisplayObservation, PostTarget
from .body_text import _VisibleBodyRead
from .feed_rows import (
    UnverifiedFeedCandidate as _UnverifiedFeedCandidate,
)
from .feed_rows import (
    _CandidateDiagnostic,
    _CandidateStructure,
    _FeedScanCounts,
    _FeedScanTimings,
)
from .viewport_scan_models import _TrackedRowIdentity, _ViewportScanState


class ViewportRowScanHost(Protocol):
    last_scan_counts: _FeedScanCounts
    last_scan_timings: _FeedScanTimings
    last_candidate_diagnostics: tuple[_CandidateDiagnostic, ...]
    last_display_observations: tuple[DisplayObservation, ...]
    last_viewport_can_wheel: bool
    _cached_home_bodies: dict[str, tuple[str, bool]]
    _scan_candidate_index: int | None
    _scan_failure_site: Any
    _active_scan_candidate_handles: dict[int, JSHandle] | None

    async def _is_carried_viewport_candidate(self, root: Locator) -> _TrackedRowIdentity | None: ...
    async def _capture_scan_failure_site(self, *args: Any, **kwargs: Any) -> None: ...
    def _set_scan_operation(self, operation: str) -> None: ...
    async def _same_dom_element(self, root: Locator, handle: JSHandle) -> bool: ...
    async def _inspect_candidate_root(
        self, root: Locator, viewport: dict[str, int]
    ) -> _CandidateStructure: ...
    async def _read_stable_visible_card(
        self, *args: Any, **kwargs: Any
    ) -> tuple[PostTarget, bool]: ...
    async def _read_card(self, *args: Any, **kwargs: Any) -> PostTarget: ...
    async def _observe_display_body(
        self, root: Locator
    ) -> tuple[str | None, tuple[Any, ...], BodyReadEvidence]: ...
    async def _is_in_visible_scrollports(self, element: Locator) -> bool: ...
    async def _wait_for_stable_card_dom(self, root: Locator) -> bool: ...
    async def _wait_for_pending_card(self, *args: Any, **kwargs: Any) -> bool: ...
    def _record_scan_skip(self, stage: str, error: _UnverifiedFeedCandidate) -> None: ...
    def _record_structure_reason(self, reason: str) -> None: ...
    async def _read_visible_body_progress(self, *args: Any, **kwargs: Any) -> _VisibleBodyRead: ...
    async def _dispose_handle_batch(self, handles: tuple[JSHandle, ...]) -> None: ...
    def _readiness_deadline(self, timeout_ms: int) -> float: ...
    def refresh_viewport_body_coverage_counts(self, *args: Any, **kwargs: Any) -> None: ...


@dataclass(slots=True, repr=False)
class CandidateWork:
    state: _ViewportScanState
    row_index: int
    root: Locator = field(repr=False)
    structure: _CandidateStructure = field(repr=False)
    carried_row: bool
    diagnostic: _CandidateDiagnostic = field(repr=False)
    root_handle: JSHandle = field(repr=False)
    box: dict[str, float] = field(repr=False)
    read_started: float
    emit_row: Callable[..., Awaitable[None]] = field(repr=False)

    def __repr__(self) -> str:
        return "CandidateWork(<private>)"


@dataclass(slots=True, repr=False)
class CandidateRoot:
    """Revalidated root and provenance cues before body reading begins."""

    state: _ViewportScanState
    row_index: int
    root: Locator = field(repr=False)
    structure: _CandidateStructure = field(repr=False)
    carried_row: bool
    diagnostic: _CandidateDiagnostic = field(repr=False)
    root_handle: JSHandle = field(repr=False)

    def __repr__(self) -> str:
        return "CandidateRoot(<private>)"
