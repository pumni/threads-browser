"""Selection of visible, provenance-verified Home post targets."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from typing import Protocol

from playwright.async_api import Locator, Page

from ..errors import UiReadError
from ..feed.models import PostTarget
from ..feed.urls import _post_url, _post_url_hint, _post_url_rejection_reason
from .feed_rows import (
    _FEED_ROOT_SELECTOR,
    _CandidateDiagnostic,
    _FeedScanCounts,
)
from .feed_rows import (
    UnverifiedFeedCandidate as _UnverifiedFeedCandidate,
)
from .loading import CardNotReady as _CardNotReady
from .viewport_scan_models import _HomeRowObservation


class VisibleTargetHost(Protocol):
    _page: Page
    last_scan_counts: _FeedScanCounts
    feed_duplicates: int
    _cached_home_bodies: dict[str, tuple[str, bool]]
    _max_permalink_candidates: int

    async def _content_region(self) -> Locator: ...
    async def _is_outside_content_landmark(self, element: Locator) -> bool: ...
    async def _is_nested_feed_content(self, element: Locator) -> bool: ...
    async def _is_inside_quote(self, element: Locator) -> bool: ...
    async def _is_in_visible_scrollports(self, element: Locator) -> bool: ...
    def _intersects_viewport(
        self, box: dict[str, float], viewport: dict[str, int]
    ) -> bool: ...
    async def _resolve_permalink_card(
        self, link: Locator, post_url: str, author: str
    ) -> tuple[Locator, dict[str, float]]: ...
    async def _read_card(
        self,
        root: Locator,
        *,
        currently_visible: bool = False,
        viewport_y: float = 0.0,
        cached_body_fingerprints: set[str] | None = None,
        visible_text_progress: bool = False,
    ) -> PostTarget: ...
    def _record_scan_skip(self, stage: str, error: _UnverifiedFeedCandidate) -> None: ...
    def _record_structure_reason(self, reason: str) -> None: ...


class VisiblePostTargetSelector:
    """Resolve visible permalink candidates into verified feed targets."""

    def __init__(self, host: VisibleTargetHost) -> None:
        self._host = host

    async def select(
        self,
        max_posts: int,
        *,
        currently_visible: bool,
        prefer_new_fingerprints: set[str] | None,
        stage: str,
        on_row: Callable[[_HomeRowObservation], Awaitable[None]] | None = None,
    ) -> tuple[tuple[PostTarget, ...], dict[str, float] | None]:
        viewport = self._host._page.viewport_size
        if viewport is None:
            raise UiReadError("FEED_VIEWPORT_UNVERIFIED")
        region = await self._host._content_region()
        permalinks = region.locator('a[href*="/post/"]')
        scan_limit = min(
            await permalinks.count(),
            min(max_posts + 20, self._host._max_permalink_candidates),
        )
        targets_by_url: dict[str, PostTarget] = {}
        scroll_anchor: dict[str, float] | None = None
        pending_cards: list[_CardNotReady] = []
        scanned_fingerprints: set[str] = set()
        for index in range(scan_limit):
            permalink = permalinks.nth(index)
            if not await permalink.is_visible():
                continue
            if await self._host._is_outside_content_landmark(permalink):
                continue
            if await self._host._is_nested_feed_content(permalink):
                continue
            if await self._host._is_inside_quote(permalink):
                continue
            if await permalink.evaluate(
                "(node, selector) => Boolean(node.parentElement?.closest(selector))",
                _FEED_ROOT_SELECTOR,
            ):
                continue
            permalink_box = await permalink.bounding_box()
            if permalink_box is None or not await self._host._is_in_visible_scrollports(permalink):
                continue
            self._host.last_scan_counts.inspected += 1
            href = await permalink.get_attribute("href")
            parsed_post = None if href is None else _post_url(href)
            if parsed_post is None:
                if href is not None and _post_url_hint(href):
                    self._host._record_scan_skip(
                        stage,
                        _UnverifiedFeedCandidate(
                            "POST_PROVENANCE_UNVERIFIED",
                            "CANDIDATE_NONCANONICAL",
                            (href,),
                            diagnostic_reason=_post_url_rejection_reason(href),
                        ),
                    )
                continue
            if parsed_post[0] in targets_by_url:
                self._host.feed_duplicates += 1
                continue

            try:
                root, box = await self._host._resolve_permalink_card(
                    permalink,
                    parsed_post[0],
                    parsed_post[1],
                )
                if not self._host._intersects_viewport(
                    box, viewport
                ) or not await self._host._is_in_visible_scrollports(root):
                    continue
                target = await self._host._read_card(
                    root,
                    currently_visible=currently_visible,
                    viewport_y=box["y"],
                    cached_body_fingerprints=prefer_new_fingerprints,
                    visible_text_progress=currently_visible,
                )
            except _CardNotReady as pending:
                self._host.last_scan_counts.card_not_ready += 1
                pending_cards.append(pending)
                continue
            except _UnverifiedFeedCandidate as error:
                self._host._record_scan_skip(stage, error)
                continue
            if target.url != parsed_post[0] or target.author != parsed_post[1]:
                self._host._record_scan_skip(
                    stage,
                    _UnverifiedFeedCandidate(
                        "POST_PROVENANCE_UNVERIFIED",
                        "CANDIDATE_AMBIGUOUS",
                        (parsed_post[0], target.url),
                        diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
                    ),
                )
                continue
            observation_id = secrets.token_hex(8)
            self._host.last_scan_counts.identity_verified += 1
            self._host.last_scan_counts.verified += int(target.body_complete)
            self._host.last_scan_counts.partial_body_rows += int(not target.body_complete)
            if target.body and target.body.strip() and target.body_complete:
                body_was_cached = bool(
                    prefer_new_fingerprints is not None
                    and target.fingerprint in prefer_new_fingerprints
                    and target.fingerprint in self._host._cached_home_bodies
                )
                self._host.last_scan_counts.body_read += int(not body_was_cached)
                self._host.last_scan_counts.body_extracted += 1
                self._host._cached_home_bodies.setdefault(
                    target.fingerprint,
                    (target.body, target.body_truncated),
                )
            elif target.body and target.body.strip():
                self._host.last_scan_counts.body_extracted += 1
            else:
                self._host.last_scan_counts.body_unknown += 1
                if (
                    target.read_evidence is not None
                    and target.read_evidence.state == "UNSUPPORTED_STRUCTURE"
                ):
                    self._host._record_structure_reason("POST_BODY_STRUCTURE_UNSUPPORTED")
            if not target.body_complete:
                self._host.last_scan_counts.skipped_rows += 1
                self._host.last_scan_counts.coverage_complete = False
                evidence_state = (
                    "NO_BODY_NODES" if target.read_evidence is None else target.read_evidence.state
                )
                state_reason = {
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
                }.get(
                    evidence_state,
                    ("PENDING_HYDRATION", "POST_BODY_PENDING"),
                )
                diagnostic = _CandidateDiagnostic(
                    row_index=len(targets_by_url) + 1,
                    root_kind="CONTAINER",
                    has_visible_time=True,
                    has_post_permalink=True,
                    has_profile_author=True,
                    has_readable_body=bool(target.body and target.body.strip()),
                    state=(
                        "PARTIAL_BODY" if target.body and target.body.strip() else state_reason[0]
                    ),
                    reason=(
                        "VISIBLE_TEXT_PROGRESS"
                        if target.body and target.body.strip()
                        else state_reason[1]
                    ),
                    identity_fingerprint=target.fingerprint,
                    observation_id=observation_id,
                    read_evidence=target.read_evidence,
                )
                if on_row is not None:
                    await on_row(
                        _HomeRowObservation(
                            diagnostic,
                            target=target,
                            fingerprint=target.fingerprint,
                        )
                    )
                continue
            if target.fingerprint in scanned_fingerprints or (
                prefer_new_fingerprints is not None
                and target.fingerprint in prefer_new_fingerprints
            ):
                self._host.feed_duplicates += 1
                targets_by_url[target.url] = target
                continue
            scanned_fingerprints.add(target.fingerprint)
            if scroll_anchor is None:
                scroll_anchor = box
            targets_by_url[target.url] = target
            if on_row is not None:
                diagnostic = _CandidateDiagnostic(
                    row_index=len(targets_by_url),
                    root_kind="CONTAINER",
                    has_visible_time=True,
                    has_post_permalink=True,
                    has_profile_author=True,
                    has_readable_body=bool(target.body and target.body.strip()),
                    state="VERIFIED",
                    reason="PROVENANCE_VERIFIED",
                    identity_fingerprint=target.fingerprint,
                )
                await on_row(
                    _HomeRowObservation(
                        diagnostic,
                        target=target,
                        fingerprint=target.fingerprint,
                    )
                )
            if len(scanned_fingerprints) >= max_posts:
                break

        if (
            not targets_by_url
            and pending_cards
            and self._host.last_scan_counts.skipped == 0
            and self._host.last_scan_counts.unsupported_roots == 0
        ):
            raise _CardNotReady.combine(pending_cards)
        targets = [
            target
            for target in targets_by_url.values()
            if target.fingerprint in scanned_fingerprints
        ]
        return tuple(targets), scroll_anchor
