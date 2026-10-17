"""Revalidate the exact row and viewport before permitting a scan retry."""

from __future__ import annotations

from typing import Protocol

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from ..errors import UiReadError
from ..feed.urls import _same_home_origin
from .feed_rows import FeedRowScanner
from .session import classify_playwright_failure
from .viewport_scan_models import _ScanFailureSite, _TrackedRowIdentity


class ScanFailureHost(Protocol):
    _page: Page
    _readiness_timeout_ms: int
    _feed_rows: FeedRowScanner
    _last_viewport_candidate_identities: tuple[_TrackedRowIdentity, ...]
    _scan_failure_site: _ScanFailureSite | None

    def _readiness_deadline(self, timeout_ms: int) -> float: ...
    async def _wait_for_verified_feed(self, deadline: float, *, timeout_code: str) -> None: ...
    async def _visible_feed_evidence(self) -> bool: ...
    async def _content_region(self) -> Locator: ...
    async def _top_level_candidate_roots(
        self, region: Locator, viewport: dict[str, int]
    ) -> tuple[list[Locator], bool]: ...
    async def _is_in_visible_scrollports(self, element: Locator) -> bool: ...


class ScanFailureRevalidator:
    """Check whether one failed DOM operation may be retried safely."""

    def __init__(self, host: ScanFailureHost) -> None:
        self._host = host

    async def revalidate(self) -> str:
        site = self._host._scan_failure_site
        if self._host._page.is_closed():
            return "CLOSED"
        if site is None:
            return "INSUFFICIENT"
        if not _same_home_origin(self._host._page.url):
            return "CHANGED"
        if site.identity_conflict:
            return "CHANGED"
        try:
            deadline = self._host._readiness_deadline(self._host._readiness_timeout_ms)
            await self._host._wait_for_verified_feed(
                deadline,
                timeout_code="SCAN_ROW_UNVERIFIED",
            )
            if not await self._host._visible_feed_evidence():
                return "CHANGED"
            viewport = self._host._page.viewport_size
            if viewport is None or (viewport["width"], viewport["height"]) != site.viewport_size:
                return "CHANGED"
            position = await self._host._scan_position()
            if any(
                abs(current - previous) > 1
                for current, previous in zip(position, site.scroll_position, strict=True)
            ):
                return "CHANGED"
            region = await self._host._content_region()
            roots, _ = await self._host._top_level_candidate_roots(region, viewport)
            matching: list[Locator] = []
            for root in roots:
                if await root.evaluate(
                    "(node, expected) => node === expected",
                    site.root_handle,
                ):
                    matching.append(root)
            if len(matching) != 1:
                return "CHANGED"
            root = matching[0]
            if not await root.is_visible() or not await self._host._is_in_visible_scrollports(root):
                return "CHANGED"
            box = await root.bounding_box()
            if box is None or any(
                abs(current - previous) > 1
                for current, previous in zip(
                    (float(box["x"]), float(box["y"]), float(box["width"]), float(box["height"])),
                    site.box,
                    strict=True,
                )
            ):
                return "CHANGED"
            evidence = await self._host._feed_rows._candidate_identity_evidence(
                root,
                site.root_handle,
            )
            if not evidence.root_matches:
                return "CHANGED"
            if site.identity_source == "CURRENT_CANONICAL":
                if site.canonical_identity is None:
                    return "INSUFFICIENT"
                if evidence.conflicts_with(
                    site.canonical_identity,
                    site.author_fingerprint,
                ):
                    return "CHANGED"
                if evidence.canonical_fingerprint != site.canonical_identity:
                    return "CHANGED"
                carried = site.carried_identity
                if (
                    carried is not None
                    and carried.fingerprint is not None
                    and (
                        carried.fingerprint != site.canonical_identity
                        or evidence.conflicts_with(
                            carried.fingerprint,
                            carried.author_fingerprint,
                        )
                    )
                ):
                    return "CHANGED"
                return "REUSED"
            if site.identity_source == "CARRIED_VERIFIED":
                carried = site.carried_identity
                if (
                    carried is None
                    or not any(
                        identity is carried
                        for identity in self._host._last_viewport_candidate_identities
                    )
                    or not carried.body_verified
                    or carried.fingerprint is None
                    or carried.fingerprint != site.canonical_identity
                ):
                    return "INSUFFICIENT"
                if evidence.conflicts_with(
                    site.canonical_identity,
                    site.author_fingerprint,
                ):
                    return "CHANGED"
                return "REUSED"
            return "INSUFFICIENT"
        except PlaywrightError as error:
            failure = classify_playwright_failure(self._host._page, error)
            return "CLOSED" if failure.error_class == "TARGET_CLOSED" else "INSUFFICIENT"
        except UiReadError:
            return "INSUFFICIENT"
