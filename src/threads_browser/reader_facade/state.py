"""State compatibility methods retained by the reader facade."""

from __future__ import annotations

from playwright.async_api import (
    JSHandle,
    Locator,
)

from ..browser_ui.body_text import (
    _MAX_BODY_CODEPOINTS,  # noqa: F401 - retained reader-module compatibility name
    _canonicalize_body,  # noqa: F401 - retained reader-module compatibility name
    _canonicalize_body_block,  # noqa: F401 - retained reader-module compatibility name
)
from ..browser_ui.feed_rows import (
    _MAX_CARD_LINKS,  # noqa: F401 - retained reader-module compatibility name
    _MAX_PERMALINK_CANDIDATES,
)
from ..browser_ui.feed_rows import (
    UnverifiedFeedCandidate as _UnverifiedFeedCandidate,
)
from ..browser_ui.root_discovery import (
    _MAX_FEED_ROOTS_PER_TYPE,
    _MAX_VISIBLE_ROW_CANDIDATES,
)
from ..browser_ui.scroll_session import (
    _SCROLL_ADVANCE_MAX_PX,
    _SCROLL_ADVANCE_MIN_PX,
    _SCROLL_ADVANCE_RATIO,
)
from ..browser_ui.scrolling import ScrollGestureResult
from ..browser_ui.viewport_scan import ViewportScanCoordinator
from ..browser_ui.viewport_scan_models import (
    _ScanFailureSite,
    _TrackedRowIdentity,
)
from ..console_output import (
    emit_console_diagnostic,
)
from ..feed.models import (
    PostTarget,
)
from ..feed.urls import (
    _rejected_post_link_blocks_candidate,  # noqa: F401 - retained reader-module compatibility name
)


class ReaderStateCompatibility:
    """Compatibility delegates grouped by state responsibility."""

    @property
    def _expected_actor(self) -> str | None:
        return self._home_navigation.state.expected_actor

    @_expected_actor.setter
    def _expected_actor(self, value: str | None) -> None:
        self._home_navigation.state.expected_actor = value

    @property
    def _home_url(self) -> str | None:
        return self._home_navigation.state.home_url

    @_home_url.setter
    def _home_url(self, value: str | None) -> None:
        self._home_navigation.state.home_url = value

    @property
    def _verified_actor_route(self) -> str | None:
        return self._home_navigation.state.verified_actor_route

    @_verified_actor_route.setter
    def _verified_actor_route(self, value: str | None) -> None:
        self._home_navigation.state.verified_actor_route = value

    @property
    def _failure_phase(self) -> str:
        return self._recovery.state.phase

    @_failure_phase.setter
    def _failure_phase(self, value: str) -> None:
        self._recovery.state.phase = value

    @property
    def _failure_operation(self) -> str:
        return self._recovery.state.operation

    @_failure_operation.setter
    def _failure_operation(self, value: str) -> None:
        self._recovery.state.operation = value

    @property
    def _max_permalink_candidates(self) -> int:
        return _MAX_PERMALINK_CANDIDATES

    @property
    def _max_feed_roots_per_type(self) -> int:
        return _MAX_FEED_ROOTS_PER_TYPE

    @property
    def _max_visible_row_candidates(self) -> int:
        return _MAX_VISIBLE_ROW_CANDIDATES

    @property
    def _scroll_engine(self) -> object:
        return self._scroll_session._engine

    @property
    def _scroll_advance_ratio(self) -> float:
        return _SCROLL_ADVANCE_RATIO

    @property
    def _scroll_advance_min_px(self) -> int:
        return _SCROLL_ADVANCE_MIN_PX

    @property
    def _scroll_advance_max_px(self) -> int:
        return _SCROLL_ADVANCE_MAX_PX

    @property
    def _last_ui_transition_at(self) -> float | None:
        return self._home_render.state.last_transition_at

    @_last_ui_transition_at.setter
    def _last_ui_transition_at(self, value: float | None) -> None:
        self._home_render.state.last_transition_at = value

    @property
    def last_viewport_coverage_complete(self) -> bool:
        return self._feed_observer.state.coverage_complete

    @last_viewport_coverage_complete.setter
    def last_viewport_coverage_complete(self, value: bool) -> None:
        self._feed_observer.state.coverage_complete = value

    @property
    def last_viewport_observed_rows(self) -> int:
        return self._feed_observer.state.observed_rows

    @last_viewport_observed_rows.setter
    def last_viewport_observed_rows(self, value: int) -> None:
        self._feed_observer.state.observed_rows = value

    @property
    def last_viewport_deferred_only(self) -> bool:
        return self._feed_observer.state.deferred_only

    @last_viewport_deferred_only.setter
    def last_viewport_deferred_only(self, value: bool) -> None:
        self._feed_observer.state.deferred_only = value

    @property
    def last_viewport_can_wheel(self) -> bool:
        return self._feed_observer.state.can_wheel

    @last_viewport_can_wheel.setter
    def last_viewport_can_wheel(self, value: bool) -> None:
        self._feed_observer.state.can_wheel = value

    @property
    def last_deferred_fingerprints(self) -> frozenset[str]:
        return self._feed_observer.state.deferred_fingerprints

    @last_deferred_fingerprints.setter
    def last_deferred_fingerprints(self, value: frozenset[str]) -> None:
        self._feed_observer.state.deferred_fingerprints = value

    @property
    def _last_viewport_limit(self) -> int:
        return self._feed_observer.state.last_viewport_limit

    @_last_viewport_limit.setter
    def _last_viewport_limit(self, value: int) -> None:
        self._feed_observer.state.last_viewport_limit = value

    @property
    def scroll_attempts(self) -> int:
        return self._scroll_session.state.scroll_attempts

    @scroll_attempts.setter
    def scroll_attempts(self, value: int) -> None:
        self._scroll_session.state.scroll_attempts = value

    @property
    def scrolls(self) -> int:
        return self._scroll_session.state.scrolls

    @scrolls.setter
    def scrolls(self, value: int) -> None:
        self._scroll_session.state.scrolls = value

    @property
    def last_scroll_delta(self) -> float:
        return self._scroll_session.state.last_scroll_delta

    @last_scroll_delta.setter
    def last_scroll_delta(self, value: float) -> None:
        self._scroll_session.state.last_scroll_delta = value

    @property
    def last_scroll_gesture(self) -> ScrollGestureResult | None:
        return self._scroll_session.state.last_scroll_gesture

    @last_scroll_gesture.setter
    def last_scroll_gesture(self, value: ScrollGestureResult | None) -> None:
        self._scroll_session.state.last_scroll_gesture = value

    @property
    def last_scroll_band(self) -> str:
        return self._scroll_session.state.last_scroll_band

    @last_scroll_band.setter
    def last_scroll_band(self, value: str) -> None:
        self._scroll_session.state.last_scroll_band = value

    @property
    def last_scroll_end_distance(self) -> float | None:
        return self._scroll_session.state.last_scroll_end_distance

    @last_scroll_end_distance.setter
    def last_scroll_end_distance(self, value: float | None) -> None:
        self._scroll_session.state.last_scroll_end_distance = value

    @property
    def last_scroll_near_end(self) -> bool:
        return self._scroll_session.state.last_scroll_near_end

    @last_scroll_near_end.setter
    def last_scroll_near_end(self, value: bool) -> None:
        self._scroll_session.state.last_scroll_near_end = value

    @property
    def last_scroll_stop_reason(self) -> str | None:
        return self._scroll_session.state.last_scroll_stop_reason

    @last_scroll_stop_reason.setter
    def last_scroll_stop_reason(self, value: str | None) -> None:
        self._scroll_session.state.last_scroll_stop_reason = value

    @property
    def last_scroll_surface_key(self) -> str | None:
        return self._scroll_session.state.last_scroll_surface_key

    @last_scroll_surface_key.setter
    def last_scroll_surface_key(self, value: str | None) -> None:
        self._scroll_session.state.last_scroll_surface_key = value

    @property
    def last_scroll_geometry(self) -> dict[str, object] | None:
        return self._scroll_session.state.last_scroll_geometry

    @last_scroll_geometry.setter
    def last_scroll_geometry(self, value: dict[str, object] | None) -> None:
        self._scroll_session.state.last_scroll_geometry = value

    @property
    def last_scroll_targets(self) -> tuple[PostTarget, ...]:
        return self._scroll_session.state.last_scroll_targets

    @last_scroll_targets.setter
    def last_scroll_targets(self, value: tuple[PostTarget, ...]) -> None:
        self._scroll_session.state.last_scroll_targets = value

    @property
    def _last_viewport_signature(self) -> tuple[object, ...] | None:
        return self._scroll_session.state.last_viewport_signature

    @_last_viewport_signature.setter
    def _last_viewport_signature(self, value: tuple[object, ...] | None) -> None:
        self._scroll_session.state.last_viewport_signature = value

    @property
    def _last_viewport_candidate_roots(self) -> tuple[Locator, ...]:
        return self._root_discovery.state.last_roots

    @_last_viewport_candidate_roots.setter
    def _last_viewport_candidate_roots(self, value: tuple[Locator, ...]) -> None:
        self._root_discovery.state.last_roots = value

    @property
    def _last_viewport_candidate_identities(self) -> tuple[_TrackedRowIdentity, ...]:
        return self._root_discovery.state.last_identities

    @_last_viewport_candidate_identities.setter
    def _last_viewport_candidate_identities(
        self,
        value: tuple[_TrackedRowIdentity, ...],
    ) -> None:
        self._root_discovery.state.last_identities = value

    @property
    def _carried_viewport_candidate_identities(self) -> tuple[_TrackedRowIdentity, ...]:
        return self._root_discovery.state.carried_identities

    @_carried_viewport_candidate_identities.setter
    def _carried_viewport_candidate_identities(
        self,
        value: tuple[_TrackedRowIdentity, ...],
    ) -> None:
        self._root_discovery.state.carried_identities = value

    @property
    def _pending_viewport_candidate_identities(self) -> tuple[_TrackedRowIdentity, ...]:
        return self._root_discovery.state.pending_identities

    @_pending_viewport_candidate_identities.setter
    def _pending_viewport_candidate_identities(
        self,
        value: tuple[_TrackedRowIdentity, ...],
    ) -> None:
        self._root_discovery.state.pending_identities = value

    @property
    def _detail_return_anchor(self) -> str | None:
        return self._detail_navigator.return_state.anchor

    @_detail_return_anchor.setter
    def _detail_return_anchor(self, value: str | None) -> None:
        self._detail_navigator.return_state.anchor = value

    @property
    def _detail_return_target(self) -> PostTarget | None:
        return self._detail_navigator.return_state.target

    @_detail_return_target.setter
    def _detail_return_target(self, value: PostTarget | None) -> None:
        self._detail_navigator.return_state.target = value

    @property
    def _cached_home_bodies(self) -> dict[str, tuple[str, bool]]:
        return self._card_provenance.cached_home_bodies

    @_cached_home_bodies.setter
    def _cached_home_bodies(self, value: dict[str, tuple[str, bool]]) -> None:
        self._card_provenance.cached_home_bodies = value

    @property
    def _scan_stage(self) -> str | None:
        return self._viewport_scan.stage

    @_scan_stage.setter
    def _scan_stage(self, value: str | None) -> None:
        self._viewport_scan.stage = value

    @property
    def _scan_candidate_index(self) -> int | None:
        return self._viewport_scan.candidate_index

    @_scan_candidate_index.setter
    def _scan_candidate_index(self, value: int | None) -> None:
        self._viewport_scan.candidate_index = value

    @property
    def _scan_operation(self) -> str:
        return self._viewport_scan.operation

    @property
    def _scan_failure_site(self) -> _ScanFailureSite | None:
        return self._viewport_scan.failure_site

    @_scan_failure_site.setter
    def _scan_failure_site(self, value: _ScanFailureSite | None) -> None:
        self._viewport_scan.failure_site = value

    @property
    def _active_scan_candidate_handles(self) -> dict[int, JSHandle] | None:
        return self._viewport_scan.active_candidate_handles

    @_active_scan_candidate_handles.setter
    def _active_scan_candidate_handles(self, value: dict[int, JSHandle] | None) -> None:
        self._viewport_scan.active_candidate_handles = value

    def _mark_ui_transition(self) -> None:
        self._home_render.mark_transition()

    def set_failure_context(self, phase: str, operation: str) -> None:
        self._recovery.set_context(phase, operation)

    def set_scan_viewport_index(self, viewport_index: int | None) -> None:
        self._scan_viewport_index = viewport_index

    def _set_scan_operation(self, operation: str) -> None:
        self._viewport_scan.set_operation(operation)

    def refresh_viewport_body_coverage_counts(self) -> None:
        ViewportScanCoordinator.refresh_viewport_body_coverage_counts(
            self.last_candidate_diagnostics,
            self.last_scan_counts,
        )

    async def _scan_position(self) -> tuple[float, float, float]:
        values = await self._page.evaluate(
            """() => [window.scrollX, window.scrollY,
                document.scrollingElement?.scrollTop ?? 0]"""
        )
        return (float(values[0]), float(values[1]), float(values[2]))

    async def _capture_scan_failure_site(
        self,
        root: Locator,
        root_handle: JSHandle,
        viewport: dict[str, int],
        candidate_index: int,
        carried_identity: _TrackedRowIdentity | None = None,
    ) -> None:
        await self._viewport_scan._capture_scan_failure_site(
            root,
            root_handle,
            viewport,
            candidate_index,
            carried_identity,
        )

    async def _revalidate_scan_failure(self) -> str:
        return await self._scan_revalidator.revalidate()

    async def _dispose_active_scan_candidate_handles(self) -> None:
        await self._viewport_scan._dispose_active_scan_candidate_handles()

    @property
    def skipped_unverified(self) -> int:
        return len(self._skipped_candidate_identities)

    def _record_skipped_candidate(
        self,
        stage: str,
        error: _UnverifiedFeedCandidate,
    ) -> None:
        self._skipped_candidate_identities.update(error.identities)
        key = (stage, error.diagnostic_reason or error.reason)
        identities = self._reported_candidate_skips.setdefault(key, set())
        previous_count = len(identities)
        identities.update(error.identities)
        if len(identities) != previous_count:
            cause = "" if error.diagnostic_reason is None else f" cause={error.diagnostic_reason}"
            emit_console_diagnostic(
                self._console_output_policy,
                f"SKIPPED stage={stage} reason={error.reason} count={len(identities)}{cause}",
            )

    def _record_scan_skip(
        self,
        stage: str,
        error: _UnverifiedFeedCandidate,
    ) -> None:
        counts = self.last_scan_counts
        counts.skipped += 1
        diagnostic_reason = error.diagnostic_reason
        if diagnostic_reason is not None:
            counts.rejection_reasons[diagnostic_reason] = (
                counts.rejection_reasons.get(diagnostic_reason, 0) + 1
            )
        if diagnostic_reason == "CANDIDATE_WRAPPER_AMBIGUOUS":
            counts.wrapper_ambiguous += 1
        elif diagnostic_reason == "CANDIDATE_AUTHOR_MISMATCH":
            counts.author_unverified += 1
        elif diagnostic_reason is None and error.reason == "CANDIDATE_AMBIGUOUS":
            counts.wrapper_ambiguous += 1
        elif diagnostic_reason is None and error.reason == "CANDIDATE_AUTHOR_CONFLICT":
            counts.author_unverified += 1
        self._record_skipped_candidate(stage, error)
