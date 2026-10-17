"""Bind reader compatibility hooks to one bounded viewport scan."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from playwright.async_api import Locator, Page

from ..feed.models import DisplayObservation
from .feed_rows import _CandidateDiagnostic, _FeedScanCounts, _FeedScanTimings
from .viewport_scan import (
    ViewportScanCoordinator,
)
from .viewport_scan_models import (
    _HomeRowObservation,
    _TrackedRowIdentity,
    _ViewportScanHooks,
    _ViewportScanRequest,
    _VisibleTargets,
)


class FeedScanHost(Protocol):
    _page: Page
    _scan_viewport_index: int | None
    _body_stability_interval_ms: int
    _card_hydration_wait_ms: int
    last_scan_counts: _FeedScanCounts
    last_scan_timings: _FeedScanTimings
    last_candidate_diagnostics: tuple[_CandidateDiagnostic, ...]
    last_display_observations: tuple[DisplayObservation, ...]
    last_viewport_can_wheel: bool
    feed_duplicates: int

    async def _dispose_active_scan_candidate_handles(self) -> None: ...
    async def _discard_pending_viewport_candidates(self) -> None: ...
    async def _top_level_candidate_roots(
        self, region: Locator, viewport: dict[str, int]
    ) -> tuple[list[Locator], bool]: ...
    async def _collect_structure_diagnostics(
        self, region: Locator, roots: list[Locator], viewport: dict[str, int]
    ) -> dict[int, object]: ...
    async def _visible_permalink_targets(self, *args, **kwargs): ...
    async def _read_card(self, *args, **kwargs): ...
    async def _recover_ambiguous_visible_card(self, *args, **kwargs): ...
    async def _read_visible_body_progress(self, *args, **kwargs): ...
    async def _wait_for_pending_card(self, *args, **kwargs): ...
    def _record_scan_skip(self, *args, **kwargs) -> None: ...
    @property
    def _viewport_scan(self) -> ViewportScanCoordinator: ...
    @property
    def _pending_viewport_candidate_identities(self) -> tuple[_TrackedRowIdentity, ...]: ...
    @_pending_viewport_candidate_identities.setter
    def _pending_viewport_candidate_identities(
        self, value: tuple[_TrackedRowIdentity, ...]
    ) -> None: ...


class FeedScanSession:
    """Run one bounded viewport scan using verified reader-owned UI operations."""

    def __init__(self, host: FeedScanHost, coordinator: ViewportScanCoordinator) -> None:
        self._host = host
        self._coordinator = coordinator

    async def scan(
        self,
        max_posts: int,
        *,
        currently_visible: bool = False,
        prefer_new_fingerprints: set[str] | None = None,
        stage: str = "FEED",
        on_row: Callable[[_HomeRowObservation], Awaitable[None]] | None = None,
    ) -> _VisibleTargets:
        """Compatibility facade for the viewport scan coordinator."""
        await self._host._dispose_active_scan_candidate_handles()
        await self._host._discard_pending_viewport_candidates()

        async def discover_roots(
            region: Locator,
            viewport: dict[str, int],
        ) -> tuple[list[Locator], bool, tuple[_TrackedRowIdentity, ...]]:
            roots, roots_truncated = await self._host._top_level_candidate_roots(region, viewport)
            return roots, roots_truncated, self._host._carried_viewport_candidate_identities

        hooks = _ViewportScanHooks(
            discover_roots=discover_roots,
            collect_structures=self._host._collect_structure_diagnostics,
            scan_permalink_fallback=self._host._visible_permalink_targets,
            read_card=self._host._read_card,
            recover_ambiguous_visible_card=self._host._recover_ambiguous_visible_card,
            read_visible_body_progress=self._host._read_visible_body_progress,
            wait_for_pending_card=self._host._wait_for_pending_card,
            record_scan_skip=self._host._record_scan_skip,
            publish_diagnostics=lambda value: setattr(
                self._host, "last_candidate_diagnostics", value
            ),
            publish_display_observations=lambda value: setattr(
                self._host, "last_display_observations", value
            ),
            publish_can_wheel=lambda value: setattr(
                self._host, "last_viewport_can_wheel", value
            ),
            increment_feed_duplicates=lambda: setattr(
                self, "feed_duplicates", self._host.feed_duplicates + 1
            ),
            body_stability_interval_ms=self._host._body_stability_interval_ms,
            card_hydration_wait_ms=self._host._card_hydration_wait_ms,
        )
        request = _ViewportScanRequest(
            max_posts=max_posts,
            currently_visible=currently_visible,
            prefer_new_fingerprints=prefer_new_fingerprints,
            stage=stage,
            viewport_index=self._host._scan_viewport_index,
            on_row=on_row,
        )
        result = await self._host._viewport_scan.scan(
            request,
            counts=self._host.last_scan_counts,
            timings=self._host.last_scan_timings,
            hooks=hooks,
        )
        self._host._pending_viewport_candidate_identities = result.candidate_identities
        return result
