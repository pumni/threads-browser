"""Feed compatibility methods retained by the reader facade."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from playwright.async_api import (
    JSHandle,
    Locator,
)

from ..browser_ui.body_text import (
    _MAX_BODY_CODEPOINTS,  # noqa: F401 - retained reader-module compatibility name
    _canonicalize_body,  # noqa: F401 - retained reader-module compatibility name
    _canonicalize_body_block,  # noqa: F401 - retained reader-module compatibility name
)
from ..browser_ui.feed_observation import (
    FeedTargetsObservation as _FeedTargetsObservation,
)
from ..browser_ui.feed_rows import (
    _MAX_CARD_LINKS,  # noqa: F401 - retained reader-module compatibility name
    FeedRowScanner,
    _CandidateStructure,
)
from ..browser_ui.loading import (
    CardNotReady as _CardNotReady,
)
from ..browser_ui.loading import (
    RenderObservation as _RenderObservation,
)
from ..browser_ui.scroll_session import FeedScrollSession
from ..browser_ui.viewport_scan_models import (
    _HomeRowObservation,
    _TrackedRowIdentity,
    _VisibleTargets,
)
from ..errors import UiReadError
from ..feed.models import (
    HomeSample,
    PostTarget,
)
from ..feed.urls import (
    _rejected_post_link_blocks_candidate,  # noqa: F401 - retained reader-module compatibility name
)


class ReaderFeedCompatibility:
    """Compatibility delegates grouped by feed responsibility."""

    async def scan_viewport(
        self,
        max_posts: int,
        *,
        scanned_fingerprints: set[str] | None = None,
        on_row: Callable[[_HomeRowObservation], Awaitable[None]] | None = None,
    ) -> tuple[PostTarget, ...]:
        if not 1 <= max_posts <= 20:
            raise UiReadError("POST_LIMIT_INVALID")
        self._last_viewport_limit = max_posts
        self.last_scan_retry_count = 0
        initial_viewport = self.scrolls == 0
        targets, _ = await self._wait_for_home_targets(
            max_posts,
            timeout_code=(
                "FEED_INITIAL_NOT_READY" if initial_viewport else "FEED_SCROLL_NOT_READY"
            ),
            currently_visible=True,
            prefer_new_fingerprints=scanned_fingerprints,
            stage="FEED_INITIAL" if initial_viewport else "FEED_SCROLL",
            on_row=on_row,
        )
        return targets

    async def advance_feed(self, *, scan_after_scroll: bool = True) -> bool:
        return await self._scroll_session.advance_feed(scan_after_scroll=scan_after_scroll)

    async def _scroll_surface_snapshot(self, surface: JSHandle) -> dict[str, object]:
        return await self._scroll_session._scroll_surface_snapshot(surface)

    async def sample_and_scroll(self, max_posts: int) -> HomeSample:
        return await self._scroll_session.sample_and_scroll(max_posts)

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
    ) -> _RenderObservation[_FeedTargetsObservation] | None:
        return await self._feed_observer._observe_feed_targets(
            max_posts,
            deadline,
            timeout_code=timeout_code,
            currently_visible=currently_visible,
            prefer_new_fingerprints=prefer_new_fingerprints,
            stage=stage,
            report_rescan=report_rescan,
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
        return await self._feed_observer._wait_for_home_targets(
            max_posts,
            timeout_code=timeout_code,
            currently_visible=currently_visible,
            prefer_new_fingerprints=prefer_new_fingerprints,
            stage=stage,
            on_row=on_row,
        )

    async def _feed_readiness_signals(self) -> _CardNotReady | None:
        return await self._feed_observer._feed_readiness_signals()

    async def _wait_for_pending_card(
        self,
        pending: _CardNotReady,
        deadline: float,
        *,
        max_wait_ms: int | None = None,
    ) -> bool:
        return await self._feed_observer._wait_for_pending_card(
            pending,
            deadline,
            max_wait_ms=max_wait_ms,
        )

    async def _visible_feed_evidence(self) -> bool:
        return await self._feed_observer._visible_feed_evidence()

    async def _safe_feed_scroll_point(
        self,
        surface: JSHandle,
    ) -> tuple[tuple[float, float], int, float, str]:
        return await self._scroll_session._safe_feed_scroll_point(surface)

    async def _scroll_surface(self) -> JSHandle | None:
        return await self._scroll_session._scroll_surface()

    async def _is_carried_viewport_candidate(self, root: Locator) -> _TrackedRowIdentity | None:
        identities = self._carried_viewport_candidate_identities
        if not identities:
            return None
        matched_index = await root.evaluate(
            "(node, previous) => previous.findIndex(item => node === item)",
            [identity.dom_handle for identity in identities],
        )
        if matched_index < 0:
            return None
        if matched_index >= len(identities):
            return None
        return identities[matched_index]

    async def _same_dom_element(self, root: Locator, handle: JSHandle) -> bool:
        return await self._card_provenance._same_dom_element(root, handle)

    async def _discard_pending_viewport_candidates(self) -> None:
        await self._root_discovery.discard_pending()

    async def _remember_viewport_candidates(
        self,
        roots: tuple[Locator, ...],
        identities: tuple[_TrackedRowIdentity, ...],
    ) -> None:
        await self._root_discovery.remember(roots, identities)

    async def _visible_targets(
        self,
        max_posts: int,
        *,
        currently_visible: bool = False,
        prefer_new_fingerprints: set[str] | None = None,
        stage: str = "FEED",
        on_row: Callable[[_HomeRowObservation], Awaitable[None]] | None = None,
    ) -> _VisibleTargets:
        return await self._feed_scan.scan(
            max_posts,
            currently_visible=currently_visible,
            prefer_new_fingerprints=prefer_new_fingerprints,
            stage=stage,
            on_row=on_row,
        )

    @staticmethod
    def _has_new_viewport_content(
        previous: tuple[object, ...] | None,
        current: tuple[object, ...],
        scroll_delta: float,
    ) -> bool:
        return FeedScrollSession._has_new_viewport_content(
            previous,
            current,
            scroll_delta,
        )

    @staticmethod
    def _intersects_viewport(
        box: dict[str, float],
        viewport: dict[str, int],
    ) -> bool:
        return (
            box["x"] < viewport["width"]
            and box["y"] < viewport["height"]
            and box["x"] + box["width"] > 0
            and box["y"] + box["height"] > 0
        )

    async def _is_in_viewport(
        self,
        element: Locator,
        viewport: dict[str, int],
    ) -> bool:
        if not await element.is_visible():
            return False
        box = await element.bounding_box()
        return box is not None and self._intersects_viewport(box, viewport)

    async def _is_in_visible_scrollports(self, element: Locator) -> bool:
        return await self._feed_rows._is_in_visible_scrollports(element)

    async def _is_nested_feed_root(self, root: Locator) -> bool:
        return await self._feed_rows._is_nested_feed_root(root)

    async def _is_nested_feed_content(self, element: Locator) -> bool:
        return await self._feed_rows._is_nested_feed_content(element)

    async def _belongs_to_card(
        self,
        element: Locator,
        root: Locator,
        root_handle: JSHandle,
        *,
        isolated_scope: bool = False,
    ) -> bool:
        return await self._feed_rows._belongs_to_card(
            element,
            root,
            root_handle,
            isolated_scope=isolated_scope,
        )

    async def _safe_body_candidate(
        self,
        element: Locator,
        root: Locator,
        root_handle: JSHandle,
        *,
        isolated_scope: bool = False,
    ) -> bool:
        return await self._feed_rows._safe_body_candidate(
            element,
            root,
            root_handle,
            isolated_scope=isolated_scope,
        )

    async def _safe_unscoped_text_candidate(self, element: Locator) -> bool:
        return await self._feed_rows._safe_unscoped_text_candidate(element)

    @staticmethod
    def _profile_anchor_candidate(value: str) -> bool:
        return FeedRowScanner._profile_anchor_candidate(value)

    async def _top_level_candidate_roots(
        self,
        region: Locator,
        viewport: dict[str, int],
    ) -> tuple[list[Locator], bool]:
        return await self._root_discovery.discover(region, viewport)

    @staticmethod
    async def _is_inside_quote(element: Locator) -> bool:
        return await FeedRowScanner._is_inside_quote(element)

    async def _inspect_candidate_root(
        self,
        root: Locator,
        viewport: dict[str, int],
    ) -> _CandidateStructure:
        return await self._feed_rows._inspect_candidate_root(root, viewport)

    @staticmethod
    async def _candidate_root_kind(root: Locator) -> str:
        return await FeedRowScanner._candidate_root_kind(root)

    def _record_structure_reason(self, reason: str) -> None:
        self._structure_collector._record_structure_reason(reason)

    def _record_url_structure(self, href: str, timestamp_link: bool) -> None:
        self._structure_collector._record_url_structure(href, timestamp_link)

    async def _collect_structure_diagnostics(
        self,
        region: Locator,
        roots: list[Locator],
        viewport: dict[str, int],
    ) -> dict[int, _CandidateStructure]:
        return await self._structure_collector._collect_structure_diagnostics(
            region,
            roots,
            viewport,
        )

    async def _content_region(
        self,
        *,
        prefer_main: bool = False,
        error_code: str = "FEED_REGION_UNVERIFIED",
    ) -> Locator:
        self._feed_rows.last_scan_counts = self.last_scan_counts
        return await self._feed_rows._content_region(
            prefer_main=prefer_main,
            error_code=error_code,
        )

    @staticmethod
    async def _is_outside_content_landmark(link: Locator) -> bool:
        return await FeedRowScanner._is_outside_content_landmark(link)

    async def _resolve_permalink_card(
        self,
        permalink: Locator,
        expected_url: str,
        expected_author: str,
    ) -> tuple[Locator, dict[str, float]]:
        return await self._feed_rows._resolve_permalink_card(
            permalink,
            expected_url,
            expected_author,
        )

    @staticmethod
    def _expected_profile_link_signal(root: Locator, author: str) -> Locator:
        return FeedRowScanner._expected_profile_link_signal(root, author)

    async def _recover_ambiguous_visible_card(
        self,
        root: Locator,
        root_handle: JSHandle,
        *,
        viewport_y: float,
        cached_body_fingerprints: set[str] | None,
        allow_hydration_retry: bool = True,
    ) -> tuple[PostTarget, bool]:
        """Compatibility delegate to bounded card provenance recovery."""
        return await self._card_provenance.recover_ambiguous_visible_card(
            root,
            root_handle,
            viewport_y=viewport_y,
            cached_body_fingerprints=cached_body_fingerprints,
            allow_hydration_retry=allow_hydration_retry,
            read_stable_card=self._read_stable_visible_card,
            wait_for_verified_feed=self._wait_for_verified_feed,
            readiness_deadline=self._readiness_deadline,
            wait_for_pending_card=self._wait_for_pending_card,
            readiness_timeout_ms=self._readiness_timeout_ms,
            card_hydration_wait_ms=self._card_hydration_wait_ms,
        )

    async def _visible_permalink_targets(
        self,
        max_posts: int,
        *,
        currently_visible: bool,
        prefer_new_fingerprints: set[str] | None,
        stage: str,
        on_row: Callable[[_HomeRowObservation], Awaitable[None]] | None = None,
    ) -> tuple[tuple[PostTarget, ...], dict[str, float] | None]:
        return await self._target_selector.select(
            max_posts,
            currently_visible=currently_visible,
            prefer_new_fingerprints=prefer_new_fingerprints,
            stage=stage,
            on_row=on_row,
        )
