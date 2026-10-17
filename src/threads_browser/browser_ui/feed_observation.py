"""Readiness-driven observation of bounded Home feed viewports."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from playwright.async_api import Locator, Page

from ..console_output import ConsoleOutputPolicy, emit_console_diagnostic
from ..errors import UiReadError
from ..feed.models import DisplayObservation, PostTarget
from ..feed.urls import _post_url
from .feed_rows import (
    _CandidateDiagnostic,
    _FeedScanCounts,
    _FeedScanTimings,
)
from .loading import CardNotReady as _CardNotReady
from .loading import CardWaitSignal as _CardWaitSignal
from .loading import RenderObservation as _RenderObservation
from .viewport_scan_models import _HomeRowObservation, _TrackedRowIdentity, _VisibleTargets


@dataclass(frozen=True, slots=True)
class FeedTargetsObservation:
    targets: tuple[PostTarget, ...]
    scroll_anchor: dict[str, float] | None
    duplicate_delta: int = 0
    display_observations: tuple[DisplayObservation, ...] = ()
    observed_rows: int = 0
    coverage_complete: bool = True
    render_signature: tuple[object, ...] = field(default=(), repr=False)
    candidate_roots: tuple[Locator, ...] = field(default=(), repr=False)
    candidate_observation_ids: tuple[str, ...] = field(default=(), repr=False)
    candidate_identities: tuple[_TrackedRowIdentity, ...] = field(default=(), repr=False)


@dataclass(slots=True)
class FeedObservationState:
    """Latest viewport outcome exposed by the reader compatibility facade."""

    last_viewport_limit: int = 20
    coverage_complete: bool = True
    observed_rows: int = 0
    deferred_only: bool = False
    can_wheel: bool = False
    deferred_fingerprints: frozenset[str] = frozenset()


class HomeFeedObserverHost(Protocol):
    _page: Page
    _console_output_policy: ConsoleOutputPolicy
    _readiness_timeout_ms: int
    _readiness_recheck_interval_ms: int
    _card_hydration_wait_ms: int
    _max_permalink_candidates: int
    last_scan_counts: _FeedScanCounts
    last_scan_timings: _FeedScanTimings
    last_candidate_diagnostics: tuple[_CandidateDiagnostic, ...]
    last_display_observations: tuple[DisplayObservation, ...]
    feed_duplicates: int

    async def _wait_for_verified_feed(self, deadline: float, *, timeout_code: str) -> None: ...
    async def _visible_targets(
        self,
        max_posts: int,
        *,
        currently_visible: bool = False,
        prefer_new_fingerprints: set[str] | None = None,
        stage: str = "FEED",
        on_row: Callable[[_HomeRowObservation], Awaitable[None]] | None = None,
    ) -> _VisibleTargets: ...
    def _remaining_readiness_ms(self, deadline: float, timeout_code: str) -> int: ...
    def _readiness_deadline(self, timeout_ms: int) -> float: ...
    async def _recoverable_stage(
        self,
        stage: str,
        timeout_code: str,
        attempt: Callable[[float], Awaitable[_VisibleTargets]],
        *,
        recoverable_codes: tuple[str, ...] | None = None,
        operation: str | None = None,
    ) -> _VisibleTargets: ...
    async def _remember_viewport_candidates(
        self, roots: tuple[Locator, ...], identities: tuple[_TrackedRowIdentity, ...]
    ) -> None: ...
    async def _content_region(self) -> Locator: ...
    async def _is_outside_content_landmark(self, element: Locator) -> bool: ...
    async def _is_nested_feed_content(self, element: Locator) -> bool: ...
    async def _is_in_visible_scrollports(self, element: Locator) -> bool: ...
    async def _top_level_candidate_roots(
        self, region: Locator, viewport: dict[str, int]
    ) -> tuple[list[Locator], bool]: ...
    async def _inspect_candidate_root(self, root: Locator, viewport: dict[str, int]) -> object: ...
    async def _is_nested_feed_root(self, root: Locator) -> bool: ...


class HomeFeedObserver:
    """Own feed readiness retries and the final viewport observation summary."""

    def __init__(self, host: HomeFeedObserverHost) -> None:
        self._host = host
        self.state = FeedObservationState()

    async def _observe_feed_targets(
        self,
        max_posts: int,
        deadline: float,
        *,
        timeout_code: str,
        currently_visible: bool,
        prefer_new_fingerprints: set[str] | None,
        stage: str,
        report_rescan: bool,
    ) -> _RenderObservation[FeedTargetsObservation] | None:
        await self._host._wait_for_verified_feed(
            deadline,
            timeout_code=timeout_code,
        )
        duplicate_count_before = self._host.feed_duplicates
        try:
            scan = await self._host._visible_targets(
                max_posts,
                currently_visible=currently_visible,
                prefer_new_fingerprints=prefer_new_fingerprints,
                stage=stage,
            )
        except _CardNotReady as pending:
            self._host.feed_duplicates = duplicate_count_before
            await self._host._wait_for_pending_card(pending, deadline)
            return None
        duplicate_delta = self._host.feed_duplicates - duplicate_count_before
        self._host.feed_duplicates = duplicate_count_before
        if scan.pending_signals:
            pending = _CardNotReady(signals=scan.pending_signals)
            if await self._host._wait_for_pending_card(
                pending,
                deadline,
                max_wait_ms=self._host._card_hydration_wait_ms,
            ):
                return None
        if not scan.observed_rows:
            pending = await self._host._feed_readiness_signals()
            if pending is not None:
                await self._host._wait_for_pending_card(pending, deadline)
                return None
            if not await self._host._visible_feed_evidence():
                return None
        self._host._remaining_readiness_ms(deadline, timeout_code)
        counts = self._host.last_scan_counts
        ready_state = (
            "OBSERVATION_UNCERTAIN"
            if not scan.coverage_complete and scan.observed_rows
            else "BODY_EXTRACTED"
            if counts.body_read and scan.coverage_complete
            else "FEED_VISIBLE_UNVERIFIED"
            if counts.body_extracted and not counts.verified and scan.coverage_complete
            else "DISPLAY_OBSERVATION"
            if counts.body_extracted and scan.coverage_complete
            else "OBSERVATION_UNCERTAIN"
            if scan.observed_rows
            else None
        )
        observation_value = FeedTargetsObservation(
            scan.targets,
            scan.scroll_anchor,
            duplicate_delta,
            scan.display_observations,
            scan.observed_rows,
            scan.coverage_complete,
            scan.signature,
            scan.candidate_roots,
            scan.candidate_observation_ids,
            scan.candidate_identities,
        )
        return _RenderObservation(
            scan.signature,
            observation_value,
            ready_state=ready_state,
        )

    async def _wait_for_home_targets(
        self,
        max_posts: int,
        *,
        timeout_code: str,
        currently_visible: bool = False,
        prefer_new_fingerprints: set[str] | None = None,
        stage: str | None = None,
        on_row: Callable[[_HomeRowObservation], Awaitable[None]] | None = None,
    ) -> tuple[tuple[PostTarget, ...], dict[str, float]]:
        stage_name = stage or {
            "FEED_INITIAL_NOT_READY": "FEED_INITIAL",
            "FEED_RETURN_NOT_READY": "FEED_RETURN",
            "HOME_SCROLL_UNVERIFIED": "FEED_SCROLL",
        }.get(timeout_code, "FEED")

        async def attempt(deadline: float) -> _VisibleTargets:
            self._host.last_scan_timings = _FeedScanTimings(wait_reason="FOR_YOU_SHELL_NOT_READY")
            await self._host._wait_for_verified_feed(
                deadline,
                timeout_code=timeout_code,
            )
            self._host.last_scan_counts = _FeedScanCounts()
            scan = await self._host._visible_targets(
                max_posts,
                currently_visible=currently_visible,
                prefer_new_fingerprints=prefer_new_fingerprints,
                stage=stage_name,
                on_row=on_row,
            )
            useful_observation = bool(
                scan.targets
                or self._host.last_scan_counts.body_extracted
                or self._host.last_scan_counts.deferred_offscreen
                or (
                    self._host.last_scan_counts.uncertain_rows
                    and not self._host.last_scan_counts.card_not_ready
                )
            )
            if on_row is not None and useful_observation:
                # Row callbacks are progressive observations. An expensive DOM
                # discovery must not invalidate rows already emitted against a
                # verified Home shell by consuming the readiness deadline.
                await self._host._wait_for_verified_feed(
                    self._host._readiness_deadline(self._host._readiness_timeout_ms),
                    timeout_code=timeout_code,
                )
                return scan
            await self._host._wait_for_verified_feed(
                deadline,
                timeout_code=timeout_code,
            )
            if not (
                scan.targets
                or self._host.last_scan_counts.body_extracted
                or self._host.last_scan_counts.deferred_offscreen
                or (
                    self._host.last_scan_counts.uncertain_rows
                    and not self._host.last_scan_counts.card_not_ready
                )
            ):
                pending_readiness = await self._host._feed_readiness_signals()
                readiness_timing = self._host.last_scan_timings
                should_rescan = False
                if pending_readiness is not None:
                    wait_started = time.perf_counter()
                    changed = await self._host._wait_for_pending_card(
                        pending_readiness,
                        deadline,
                    )
                    readiness_timing.stabilize_ms += round(
                        (time.perf_counter() - wait_started) * 1000
                    )
                    readiness_timing.wait_reason = (
                        "FEED_READINESS_SIGNAL" if changed else "FEED_READINESS_TIMEOUT"
                    )
                    should_rescan = changed
                if not should_rescan:
                    should_rescan = await self._host._visible_feed_evidence()
                if should_rescan:
                    previous_discover_ms = readiness_timing.discover_ms
                    previous_read_row_ms = readiness_timing.read_row_ms
                    previous_stabilize_ms = readiness_timing.stabilize_ms
                    previous_wait_reason = readiness_timing.wait_reason
                    self._host.last_scan_counts = _FeedScanCounts()
                    scan = await self._host._visible_targets(
                        max_posts,
                        currently_visible=currently_visible,
                        prefer_new_fingerprints=prefer_new_fingerprints,
                        stage=stage_name,
                        on_row=on_row,
                    )
                    self._host.last_scan_timings.discover_ms += previous_discover_ms
                    self._host.last_scan_timings.read_row_ms += previous_read_row_ms
                    self._host.last_scan_timings.stabilize_ms += previous_stabilize_ms
                    if self._host.last_scan_timings.wait_reason == "NONE":
                        self._host.last_scan_timings.wait_reason = previous_wait_reason
                if not (
                    scan.targets
                    or self._host.last_scan_counts.body_extracted
                    or self._host.last_scan_counts.deferred_offscreen
                    or (
                        self._host.last_scan_counts.uncertain_rows
                        and not self._host.last_scan_counts.card_not_ready
                    )
                ):
                    if self._host.last_scan_timings.wait_reason == "NONE":
                        self._host.last_scan_timings.wait_reason = "NO_VERIFIABLE_ROW_EVIDENCE"
                    raise UiReadError(timeout_code)
            return scan

        scan = await self._host._recoverable_stage(
            stage_name,
            timeout_code,
            attempt,
            recoverable_codes=(timeout_code,),
            operation="SCAN_ROW",
        )
        self._host.last_display_observations = scan.display_observations
        self._host.last_viewport_coverage_complete = scan.coverage_complete
        self._host.last_viewport_observed_rows = scan.observed_rows
        self._host.last_scan_counts.coverage_complete = scan.coverage_complete
        self._host._last_viewport_signature = scan.signature
        await self._host._remember_viewport_candidates(
            scan.candidate_roots,
            scan.candidate_identities,
        )
        self._host.last_viewport_deferred_only = (
            not scan.coverage_complete
            and any(
                diagnostic.state
                in {
                    "DEFERRED_OFFSCREEN",
                    "DEFERRED_TEXT_ABOVE",
                    "DEFERRED_TEXT_BELOW",
                    "PARTIAL_BODY",
                    "MEDIA_OR_WHITESPACE_GAP",
                }
                for diagnostic in self._host.last_candidate_diagnostics
            )
            and all(
                diagnostic.identity_fingerprint is not None
                for diagnostic in self._host.last_candidate_diagnostics
                if diagnostic.state
                in {
                    "DEFERRED_OFFSCREEN",
                    "DEFERRED_TEXT_ABOVE",
                    "DEFERRED_TEXT_BELOW",
                    "PARTIAL_BODY",
                    "MEDIA_OR_WHITESPACE_GAP",
                }
            )
        )
        self._host.last_viewport_can_wheel = bool(
            not scan.coverage_complete
            and scan.observed_rows <= self._host._last_viewport_limit
            and not any(
                diagnostic.reason
                in {
                    "POST_ROOT_UNRESOLVED",
                    "POST_ROOT_IDENTITY_CHANGED",
                    "ROW_PROVENANCE_UNRESOLVED",
                }
                for diagnostic in self._host.last_candidate_diagnostics
            )
        )
        self._host.last_deferred_fingerprints = frozenset(
            diagnostic.identity_fingerprint
            for diagnostic in self._host.last_candidate_diagnostics
            if diagnostic.state == "DEFERRED_OFFSCREEN"
            and diagnostic.identity_fingerprint is not None
        )
        counts = self._host.last_scan_counts
        if counts.verified and counts.body_read and scan.coverage_complete:
            emit_console_diagnostic(
                self._host._console_output_policy,
                f"READY stage={stage_name} state=BODY_EXTRACTED "
                f"verified={counts.verified} observed_rows={counts.observed_rows} "
                f"body_extracted={counts.body_extracted} "
                f"viewport_coverage_complete={str(counts.coverage_complete).lower()}",
            )
        elif counts.body_extracted and not counts.verified and scan.coverage_complete:
            emit_console_diagnostic(
                self._host._console_output_policy,
                f"OBSERVED stage={stage_name} "
                "state=FEED_VISIBLE_UNVERIFIED verified=0 "
                f"identity=OBSERVED_UNVERIFIED observed_rows={counts.observed_rows} "
                f"body_extracted={counts.body_extracted} "
                f"viewport_coverage_complete={str(counts.coverage_complete).lower()}",
            )
        elif counts.observed_rows:
            emit_console_diagnostic(
                self._host._console_output_policy,
                f"OBSERVED stage={stage_name} state=OBSERVATION_UNCERTAIN "
                f"verified={counts.verified} observed_rows={counts.observed_rows} "
                f"uncertain_rows={counts.uncertain_rows} "
                f"viewport_coverage_complete={str(counts.coverage_complete).lower()}",
            )
        return scan.targets, scan.scroll_anchor or {}

    async def _feed_readiness_signals(self) -> _CardNotReady | None:
        signals: list[_CardWaitSignal] = []
        viewport = self._host._page.viewport_size
        if viewport is None:
            return None
        region = await self._host._content_region()

        permalinks = region.locator('a[href*="/post/"]')
        for index in range(min(await permalinks.count(), self._host._max_permalink_candidates)):
            permalink = permalinks.nth(index)
            if await self._host._is_outside_content_landmark(permalink):
                continue
            if await self._host._is_nested_feed_content(permalink):
                continue
            if not await permalink.is_visible():
                continue
            # A visible DOM node can still be outside the page or clipped by
            # an inner feed scroller. Never wait for an offscreen permalink.
            if await self._host._is_in_visible_scrollports(permalink):
                signals.append(_CardWaitSignal(permalink, "retry"))

        if not signals:
            return None
        return _CardNotReady(signals=tuple(signals))

    async def _wait_for_pending_card(
        self,
        pending: _CardNotReady,
        deadline: float,
        *,
        max_wait_ms: int | None = None,
    ) -> bool:
        return await self._host._loading_observer.wait_for_card_signal(
            pending,
            deadline,
            recheck_interval_ms=self._host._readiness_recheck_interval_ms,
            max_wait_ms=max_wait_ms,
        )

    async def _visible_feed_evidence(self) -> bool:
        viewport = self._host._page.viewport_size
        if viewport is None:
            return False
        region = await self._host._content_region()
        roots, _ = await self._host._top_level_candidate_roots(region, viewport)
        for root in roots:
            structure = await self._host._inspect_candidate_root(root, viewport)
            if (structure.visible_times or structure.offscreen_times) and (
                structure.profile_anchor_candidates
                or structure.visible_post_links
                or structure.offscreen_profile_anchors
                or structure.offscreen_post_links
            ):
                return True
        permalinks = region.locator('a[href*="/post/"]')
        for index in range(min(await permalinks.count(), self._host._max_permalink_candidates)):
            permalink = permalinks.nth(index)
            if await self._host._is_outside_content_landmark(permalink):
                continue
            if await self._host._is_nested_feed_content(permalink):
                continue
            if not await self._host._is_in_visible_scrollports(permalink):
                continue
            href = await permalink.get_attribute("href")
            if href is None:
                continue
            if _post_url(href) is not None:
                return True
            root = permalink.locator(
                "xpath=ancestor::*[self::article or @role='article' "
                "or @data-pressable-container='true'][1]"
            )
            if await root.count() == 0 or await self._host._is_nested_feed_root(root):
                continue
            structure = await self._host._inspect_candidate_root(root, viewport)
            if (
                structure.visible_times
                and structure.profile_anchor_candidates
                and structure.body_candidates
            ):
                return True
        return False
