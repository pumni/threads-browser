"""Canonical Home card identity, body verification, and recovery."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from hashlib import sha256

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import JSHandle, Locator

from ..feed.models import BodyReadEvidence, PostTarget
from ..feed.urls import (
    _post_url,
    _post_url_hint,
    _post_url_rejection_reason,
    _profile_author,
    _rejected_post_link_blocks_candidate,
)
from .body_text import (
    _MAX_BODY_CODEPOINTS,
    BodyTextReader,
    _canonicalize_body,
    _canonicalize_body_block,
    _VisibleBodyRead,
)
from .feed_rows import (
    _BODY_EXCLUSION_SELECTOR,
    _BODY_TEXT_SELECTOR,
    _FEED_ROOT_SELECTOR,
    _MAX_CARD_ANCESTORS,
    _MAX_CARD_LINKS,
    _MAX_STRUCTURE_ELEMENTS,
    FeedRowScanner,
)
from .feed_rows import (
    UnverifiedFeedCandidate as _UnverifiedFeedCandidate,
)
from .loading import (
    CardNotReady as _CardNotReady,
)
from .loading import (
    CardWaitSignal as _CardWaitSignal,
)
from .loading import (
    LoadingObserver,
)

_MORE_NAME = re.compile(r"^(?:more|see more|read more)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _CardDeferredOffscreen(Exception):
    def __init__(self, fingerprint: str, reason: str) -> None:
        self.fingerprint = fingerprint
        self.reason = reason
        super().__init__()


class CardProvenanceReader:
    """Own canonical card verification and its cross-scan body cache."""

    def __init__(
        self,
        feed_rows: FeedRowScanner,
        body_text: BodyTextReader,
        loading_observer: LoadingObserver,
    ) -> None:
        self._feed_rows = feed_rows
        self._body_text = body_text
        self._loading_observer = loading_observer
        self.cached_home_bodies: dict[str, tuple[str, bool]] = {}

    async def _belongs_to_card(
        self,
        element: Locator,
        root: Locator,
        root_handle: JSHandle,
        *,
        isolated_scope: bool = False,
    ) -> bool:
        return await self._feed_rows._belongs_to_card(
            element, root, root_handle, isolated_scope=isolated_scope
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
            element, root, root_handle, isolated_scope=isolated_scope
        )

    async def _is_in_visible_scrollports(self, element: Locator) -> bool:
        return await self._feed_rows._is_in_visible_scrollports(element)

    @staticmethod
    async def _is_outside_content_landmark(link: Locator) -> bool:
        return await FeedRowScanner._is_outside_content_landmark(link)

    @staticmethod
    def _expected_profile_link_signal(root: Locator, author: str) -> Locator:
        return FeedRowScanner._expected_profile_link_signal(root, author)

    async def _same_dom_element(self, root: Locator, handle: JSHandle) -> bool:
        try:
            return bool(await root.evaluate("(node, expected) => node === expected", handle))
        except PlaywrightError:
            return False

    async def extract_card_body(
        self,
        root: Locator,
        root_handle: JSHandle,
        *,
        require_body: bool,
        isolated_scope: bool = False,
    ) -> tuple[str | None, bool, bool, tuple[_CardWaitSignal, ...]]:
        buttons = root.get_by_role("button", name=_MORE_NAME)
        body_truncated = False
        for index in range(min(await buttons.count(), _MAX_CARD_LINKS)):
            button = buttons.nth(index)
            if await self._belongs_to_card(
                button,
                root,
                root_handle,
                isolated_scope=isolated_scope,
            ) and await button.is_visible():
                body_truncated = True
                break

        candidates = root.locator(_BODY_TEXT_SELECTOR)
        bodies: list[str] = []
        empty_candidates: list[Locator] = []
        count = min(await candidates.count(), _MAX_STRUCTURE_ELEMENTS)
        for index in range(count):
            candidate = candidates.nth(index)
            if not await self._safe_body_candidate(
                candidate,
                root,
                root_handle,
                isolated_scope=isolated_scope,
            ):
                continue
            if not await candidate.is_visible():
                continue
            if await candidate.evaluate(
                "node => node.tagName !== 'P' && Boolean(node.querySelector('p'))"
            ):
                continue
            has_body_candidate_ancestor = await candidate.evaluate(
                """(node, args) => {
                    let current = node.parentElement;
                    while (current && current !== args.root) {
                        if (
                            current.matches(args.body)
                            && !current.querySelector(args.excluded)
                            && !current.querySelector(args.roots)
                        ) return true;
                        current = current.parentElement;
                    }
                    return current !== args.root;
                }""",
                {
                    "root": root_handle,
                    "body": _BODY_TEXT_SELECTOR,
                    "excluded": _BODY_EXCLUSION_SELECTOR,
                    "roots": _FEED_ROOT_SELECTOR,
                },
            )
            if has_body_candidate_ancestor:
                continue
            text = _canonicalize_body_block(await candidate.inner_text())
            if not text:
                empty_candidates.append(candidate)
                continue
            bodies.append(text)

        if not bodies:
            if require_body:
                empty_signals = tuple(
                    _CardWaitSignal(
                        candidate,
                        "nonempty_text",
                        "DETAIL_BODY_UNVERIFIED",
                    )
                    for candidate in empty_candidates
                )
                if empty_signals:
                    raise _CardNotReady(
                        signals=empty_signals,
                        timeout_code="DETAIL_BODY_UNVERIFIED",
                        reason_code="POST_BODY_PENDING",
                    )
                raise _CardNotReady(
                    candidates.first,
                    nonempty_text=True,
                    timeout_code="DETAIL_BODY_UNVERIFIED",
                    reason_code="POST_BODY_PENDING",
                )
            return (
                None,
                False,
                body_truncated,
                tuple(
                    _CardWaitSignal(candidate, "nonempty_text") for candidate in empty_candidates
                ),
            )
        body = _canonicalize_body("\n".join(bodies))
        if len(body) > _MAX_BODY_CODEPOINTS:
            if require_body:
                raise _CardNotReady(
                    candidates.first,
                    nonempty_text=True,
                    timeout_code="DETAIL_BODY_UNVERIFIED",
                )
            return None, True, body_truncated, ()
        if body.rstrip().endswith(("…", "...")):
            body_truncated = True
        return body, True, body_truncated, ()

    async def wait_for_stable_card_dom(
        self,
        root: Locator,
        *,
        body_stability_interval_ms: int,
        card_hydration_wait_ms: int,
    ) -> bool:
        """Wait for a bounded quiet interval after a hydration signal."""
        return await self._loading_observer.wait_for_card_quiet(
            root,
            dwell_ms=min(body_stability_interval_ms, card_hydration_wait_ms),
            timeout_ms=card_hydration_wait_ms,
        )

    async def recover_ambiguous_visible_card(
        self,
        root: Locator,
        root_handle: JSHandle,
        *,
        viewport_y: float,
        cached_body_fingerprints: set[str] | None,
        allow_hydration_retry: bool = True,
        read_stable_card: Callable[..., Awaitable[tuple[PostTarget, bool]]],
        wait_for_verified_feed: Callable[..., Awaitable[None]],
        readiness_deadline: Callable[[int], float],
        wait_for_pending_card: Callable[..., Awaitable[bool]],
        readiness_timeout_ms: int,
        card_hydration_wait_ms: int,
    ) -> tuple[PostTarget, bool]:
        """Recover only when one visible timestamp permalink maps to one inner row."""
        if (
            not await root.is_visible()
            or not await self._is_in_visible_scrollports(root)
            or not await self._same_dom_element(root, root_handle)
        ):
            raise _CardNotReady(root, reason_code="POST_ROOT_DOM_IDENTITY_CHANGED")

        links = root.locator("a")
        link_count = await links.count()
        if link_count > _MAX_CARD_LINKS:
            raise _UnverifiedFeedCandidate(
                "POST_PROVENANCE_UNVERIFIED",
                "CANDIDATE_AMBIGUOUS",
                (),
                diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
            )
        timestamp_candidates: list[tuple[str, str, Locator, JSHandle]] = []
        rejected_post_links: list[tuple[str, bool, str]] = []
        hydration_signals: list[_CardWaitSignal] = []
        try:
            for index in range(link_count):
                link = links.nth(index)
                if not await self._belongs_to_card(link, root, root_handle):
                    continue
                if await self._is_outside_content_landmark(link) or not await link.is_visible():
                    continue
                if not await self._is_in_visible_scrollports(link):
                    continue
                href = await link.get_attribute("href")
                if href is None or not _post_url_hint(href):
                    continue
                times = link.locator("time")
                has_timestamp = await times.count() > 0
                visible_timestamp = any(
                    [await times.nth(time_index).is_visible() for time_index in range(await times.count())]
                )
                parsed = _post_url(href)
                if parsed is None:
                    rejected_post_links.append(
                        (href, has_timestamp, _post_url_rejection_reason(href))
                    )
                    if not hydration_signals:
                        hydration_signals.append(_CardWaitSignal(link, "retry"))
                    continue
                if visible_timestamp:
                    handle = await link.element_handle()
                    if handle is not None:
                        timestamp_candidates.append((parsed[0], parsed[1], link, handle))
                else:
                    hydration_signals.append(_CardWaitSignal(link, "retry"))

            blocking_invalid = next(
                (
                    item
                    for item in rejected_post_links
                    if _rejected_post_link_blocks_candidate(
                        timestamp_link=item[1], reason=item[2]
                    )
                ),
                None,
            )
            if blocking_invalid is not None:
                _, _, reason = blocking_invalid
                raise _UnverifiedFeedCandidate(
                    "POST_PROVENANCE_UNVERIFIED",
                    "CANDIDATE_NONCANONICAL",
                    (),
                    diagnostic_reason=reason,
                )
            if len(timestamp_candidates) > 1:
                raise _UnverifiedFeedCandidate(
                    "POST_PROVENANCE_UNVERIFIED",
                    "CANDIDATE_AMBIGUOUS",
                    (),
                    diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
                )
            if not timestamp_candidates:
                if not hydration_signals:
                    raise _UnverifiedFeedCandidate(
                        "POST_PROVENANCE_UNVERIFIED",
                        "CANDIDATE_AMBIGUOUS",
                        (),
                        diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
                    )
                if allow_hydration_retry:
                    await wait_for_verified_feed(
                        readiness_deadline(readiness_timeout_ms),
                        timeout_code="HOME_UI_UNVERIFIED",
                    )
                    changed = await wait_for_pending_card(
                        _CardNotReady(signals=tuple(hydration_signals)),
                        readiness_deadline(card_hydration_wait_ms),
                        max_wait_ms=card_hydration_wait_ms,
                    )
                    if changed:
                        if not await self._same_dom_element(root, root_handle):
                            raise _CardNotReady(
                                root,
                                reason_code="POST_ROOT_DOM_IDENTITY_CHANGED",
                            )
                        return await self.recover_ambiguous_visible_card(
                            root,
                            root_handle,
                            viewport_y=viewport_y,
                            cached_body_fingerprints=cached_body_fingerprints,
                            allow_hydration_retry=False,
                            read_stable_card=read_stable_card,
                            wait_for_verified_feed=wait_for_verified_feed,
                            readiness_deadline=readiness_deadline,
                            wait_for_pending_card=wait_for_pending_card,
                            readiness_timeout_ms=readiness_timeout_ms,
                            card_hydration_wait_ms=card_hydration_wait_ms,
                        )
                raise _UnverifiedFeedCandidate(
                    "POST_PROVENANCE_UNVERIFIED",
                    "CANDIDATE_AMBIGUOUS",
                    (),
                    diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
                )

            expected_url, expected_author, permalink, permalink_handle = timestamp_candidates[0]
            pending_signals: list[_CardWaitSignal] = []
            ancestor = permalink.locator("xpath=..")
            for _ in range(_MAX_CARD_ANCESTORS):
                if not await self._same_dom_element(root, root_handle):
                    raise _CardNotReady(root, reason_code="POST_ROOT_DOM_IDENTITY_CHANGED")
                if await ancestor.count() == 0:
                    break
                candidate_handle = await ancestor.element_handle()
                if candidate_handle is None:
                    break
                try:
                    contained = bool(
                        await root_handle.evaluate(
                            "(outer, node) => outer === node || outer.contains(node)",
                            candidate_handle,
                        )
                    )
                    is_outer = bool(
                        await root_handle.evaluate(
                            "(outer, node) => outer === node",
                            candidate_handle,
                        )
                    )
                    if not contained or is_outer:
                        break
                    semantic = bool(
                        await candidate_handle.evaluate(
                            "node => node.matches('article, [role=\"article\"], "
                            "[data-pressable-container=\"true\"]')"
                        )
                    )
                    if await ancestor.is_visible() and await self._is_in_visible_scrollports(ancestor):
                        try:
                            target, body_stable = await read_stable_card(
                                ancestor,
                                viewport_y=viewport_y,
                                cached_body_fingerprints=cached_body_fingerprints,
                                expected_root_handle=candidate_handle,
                                isolated_scope=not semantic,
                            )
                        except _CardNotReady as pending:
                            if pending.reason_code == "POST_ROOT_DOM_IDENTITY_CHANGED":
                                raise _CardNotReady(
                                    root,
                                    reason_code="POST_ROOT_DOM_IDENTITY_CHANGED",
                                ) from None
                            pending_signals.extend(pending.signals)
                        except (_CardDeferredOffscreen, _UnverifiedFeedCandidate):
                            pass
                        else:
                            same_permalink = bool(
                                await candidate_handle.evaluate(
                                    "(candidate, expected) => candidate.contains(expected)",
                                    permalink_handle,
                                )
                            )
                            if (
                                same_permalink
                                and target.url == expected_url
                                and target.author == expected_author
                                and target.currently_visible
                                and await self._same_dom_element(root, root_handle)
                            ):
                                return target, body_stable
                finally:
                    await candidate_handle.dispose()
                ancestor = ancestor.locator("xpath=..")

            if allow_hydration_retry:
                pending_signals.append(_CardWaitSignal(permalink, "retry"))
                visible_signals_list: list[_CardWaitSignal] = []
                for signal in pending_signals:
                    if await self._is_in_visible_scrollports(signal.locator):
                        visible_signals_list.append(signal)
                visible_signals = tuple(visible_signals_list)
                if visible_signals:
                    await wait_for_verified_feed(
                        readiness_deadline(readiness_timeout_ms),
                        timeout_code="HOME_UI_UNVERIFIED",
                    )
                    changed = await wait_for_pending_card(
                        _CardNotReady(signals=visible_signals),
                        readiness_deadline(card_hydration_wait_ms),
                        max_wait_ms=card_hydration_wait_ms,
                    )
                    if changed:
                        if not await self._same_dom_element(root, root_handle):
                            raise _CardNotReady(
                                root,
                                reason_code="POST_ROOT_DOM_IDENTITY_CHANGED",
                            )
                        if not await permalink_handle.evaluate(
                            "node => node.isConnected"
                        ):
                            raise _CardNotReady(
                                root,
                                reason_code="POST_ROOT_DOM_IDENTITY_CHANGED",
                            )
                        return await self.recover_ambiguous_visible_card(
                            root,
                            root_handle,
                            viewport_y=viewport_y,
                            cached_body_fingerprints=cached_body_fingerprints,
                            allow_hydration_retry=False,
                            read_stable_card=read_stable_card,
                            wait_for_verified_feed=wait_for_verified_feed,
                            readiness_deadline=readiness_deadline,
                            wait_for_pending_card=wait_for_pending_card,
                            readiness_timeout_ms=readiness_timeout_ms,
                            card_hydration_wait_ms=card_hydration_wait_ms,
                        )
            raise _UnverifiedFeedCandidate(
                "POST_PROVENANCE_UNVERIFIED",
                "CANDIDATE_AMBIGUOUS",
                (expected_url,),
                diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
            )
        finally:
            for _, _, _, handle in timestamp_candidates:
                await handle.dispose()

    async def read_card(
        self,
        root: Locator,
        *,
        currently_visible: bool = False,
        viewport_y: float = 0.0,
        require_body: bool = False,
        cached_body_fingerprints: set[str] | None = None,
        visible_text_progress: bool = False,
        expected_root_handle: JSHandle | None = None,
        isolated_scope: bool = False,
        read_visible_body_progress: Callable[..., Awaitable[_VisibleBodyRead]],
        scan_operation: Callable[[str], None] | None = None,
    ) -> PostTarget:
        if scan_operation is not None:
            scan_operation("READ_CARD")
        if not await root.is_visible():
            raise _CardNotReady(root)
        root_handle = await root.element_handle()
        if root_handle is None:
            raise _CardNotReady(root)
        if expected_root_handle is not None and not await self._same_dom_element(
            root, expected_root_handle
        ):
            await root_handle.dispose()
            raise _CardNotReady(root, reason_code="POST_ROOT_DOM_IDENTITY_CHANGED")
        post_links: list[tuple[str, str, Locator, bool]] = []
        rejected_post_links: list[tuple[str, bool, str]] = []
        authors: set[str] = set()
        links = root.locator("a")
        link_count = await links.count()
        if link_count > _MAX_CARD_LINKS:
            raise _UnverifiedFeedCandidate(
                "POST_PROVENANCE_UNVERIFIED",
                "CANDIDATE_AMBIGUOUS",
                (),
                diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
            )
        for index in range(link_count):
            link = links.nth(index)
            if not await self._belongs_to_card(
                link,
                root,
                root_handle,
                isolated_scope=isolated_scope,
            ):
                continue
            if await self._is_outside_content_landmark(link) or not await link.is_visible():
                continue
            href = await link.get_attribute("href")
            if href is None:
                continue
            timestamp_link = await link.locator("time").count() > 0
            parsed_post = _post_url(href)
            if parsed_post is not None:
                post_links.append((parsed_post[0], parsed_post[1], link, timestamp_link))
                continue
            if _post_url_hint(href):
                rejected_post_links.append((href, timestamp_link, _post_url_rejection_reason(href)))
                continue
            author = _profile_author(href)
            if author is not None:
                authors.add(author)
        blocking_invalid = next(
            (
                item
                for item in rejected_post_links
                if _rejected_post_link_blocks_candidate(
                    timestamp_link=item[1], reason=item[2]
                )
            ),
            None,
        )
        if blocking_invalid is not None:
            href, _, reason = blocking_invalid
            raise _UnverifiedFeedCandidate(
                "POST_PROVENANCE_UNVERIFIED",
                "CANDIDATE_NONCANONICAL",
                (href,),
                diagnostic_reason=reason,
            )
        unique_urls = {item[0] for item in post_links}
        unique_permalink_links = {(item[0], item[1]) for item in post_links}
        if len(unique_urls) > 1 or len(unique_permalink_links) > 1 or len(authors) > 1:
            raise _UnverifiedFeedCandidate(
                "POST_PROVENANCE_UNVERIFIED",
                "CANDIDATE_AMBIGUOUS",
                tuple(unique_urls or authors),
                diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
            )
        if not post_links and rejected_post_links:
            href, _, reason = rejected_post_links[0]
            raise _UnverifiedFeedCandidate(
                "POST_PROVENANCE_UNVERIFIED",
                "CANDIDATE_NONCANONICAL",
                (href,),
                diagnostic_reason=reason,
            )
        if not post_links:
            raise _CardNotReady(
                root.locator('a[href*="/post/"]').first,
                reason_code="POST_PERMALINK_PENDING",
            )
        if not authors:
            raise _CardNotReady(
                self._expected_profile_link_signal(root, post_links[0][1]),
                reason_code="POST_AUTHOR_PENDING",
            )
        if len(unique_urls) != 1 or len(unique_permalink_links) != 1:
            raise _UnverifiedFeedCandidate(
                "POST_PROVENANCE_UNVERIFIED",
                "CANDIDATE_AMBIGUOUS",
                tuple(unique_urls),
                diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
            )
        timestamp_links = [item for item in post_links if item[3]]
        url, permalink_author, link, _ = timestamp_links[0] if timestamp_links else post_links[0]
        author = next(iter(authors))
        if author != permalink_author:
            raise _UnverifiedFeedCandidate(
                "POST_AUTHOR_UNVERIFIED",
                "CANDIDATE_AUTHOR_CONFLICT",
                (url,),
                diagnostic_reason="CANDIDATE_AUTHOR_MISMATCH",
            )

        fingerprint = sha256(f"{url}\n{author}".encode()).hexdigest()
        body_truncated = False
        cached_body = (
            self.cached_home_bodies.get(fingerprint)
            if not require_body
            and cached_body_fingerprints is not None
            and fingerprint in cached_body_fingerprints
            else None
        )
        if cached_body is not None and not currently_visible:
            return PostTarget(
                url,
                author,
                cached_body[0],
                fingerprint,
                link,
                body_available=bool(cached_body[0].strip()),
                body_truncated=cached_body[1],
                read_evidence=BodyReadEvidence(
                    state="NOT_SAMPLED_CACHE",
                    body_text_nodes_present=None,
                    render_rect_count=None,
                    visible_text_rect_count=None,
                    deferred_above_rect_count=None,
                    deferred_below_rect_count=None,
                    root_intersects=None,
                    root_position_band="NOT_SAMPLED",
                    scroll_surface_key="NOT_SAMPLED",
                ),
                viewport_y=viewport_y,
            )
        permalink_visible = True
        visible_body: _VisibleBodyRead | None = None
        if currently_visible:
            if visible_text_progress:
                buttons = root.get_by_role("button", name=_MORE_NAME)
                for index in range(min(await buttons.count(), _MAX_CARD_LINKS)):
                    button = buttons.nth(index)
                    if await self._belongs_to_card(
                        button,
                        root,
                        root_handle,
                        isolated_scope=isolated_scope,
                    ) and await button.is_visible():
                        body_truncated = True
                        break
                if scan_operation is not None:
                    scan_operation("READ_BODY")
                visible_body = await read_visible_body_progress(
                    root,
                    root_handle,
                    fingerprint=fingerprint,
                    isolated_scope=isolated_scope,
                )
            else:
                has_body_candidate, body_fully_visited = await self.visible_body_candidate_state(
                    root,
                    fingerprint=fingerprint,
                )
                if has_body_candidate and not body_fully_visited:
                    raise _CardDeferredOffscreen(
                        fingerprint,
                        "POST_BODY_PARTIALLY_VISIBLE",
                    )
            permalink_visible = await self._is_in_visible_scrollports(link)
            if not permalink_visible and not visible_text_progress and not has_body_candidate:
                raise _CardDeferredOffscreen(
                    fingerprint,
                    "POST_PERMALINK_OUTSIDE_VISIBLE_SCROLLPORT",
                )

        if visible_text_progress and visible_body is not None:
            body = visible_body.body
            body_available = bool(body and body.strip())
            body_complete = visible_body.complete and body_available
            body_consistent = visible_body.consistent
            body_segments = visible_body.segments
            body_truncated = body_truncated or visible_body.truncated
        else:
            if scan_operation is not None:
                scan_operation("READ_BODY")
            body, body_available, body_truncated, body_signals = await self.extract_card_body(
                root,
                root_handle,
                require_body=require_body,
                isolated_scope=isolated_scope,
            )
            if not body_available and body_signals:
                raise _CardNotReady(
                    signals=body_signals,
                    reason_code="POST_BODY_PENDING",
                )
            body_complete = body_available
            body_consistent = True
            body_segments = ()
        if body and body.rstrip().endswith(("…", "...")):
            body_truncated = True
        if expected_root_handle is not None and not await self._same_dom_element(
            root, expected_root_handle
        ):
            await root_handle.dispose()
            raise _CardNotReady(root, reason_code="POST_ROOT_DOM_IDENTITY_CHANGED")
        return PostTarget(
            url,
            author,
            body,
            fingerprint,
            link,
            body_available=body_available,
            body_truncated=body_truncated,
            body_complete=body_complete,
            body_consistent=body_consistent,
            visible_segments=body_segments,
            read_evidence=(None if visible_body is None else visible_body.read_evidence),
            currently_visible=(
                currently_visible
                and permalink_visible
                and (visible_body is None or visible_body.read_evidence.root_intersects)
            ),
            viewport_y=viewport_y,
        )

    async def read_stable_visible_card(
        self,
        root: Locator,
        *,
        viewport_y: float,
        cached_body_fingerprints: set[str] | None,
        expected_root_handle: JSHandle | None = None,
        allow_hydration_retry: bool = False,
        isolated_scope: bool = False,
        read_card: Callable[..., Awaitable[PostTarget]],
        wait_for_stable_dom: Callable[..., Awaitable[bool]],
        scan_operation: Callable[[str], None] | None = None,
        body_stability_interval_ms: int,
        card_hydration_wait_ms: int,
    ) -> tuple[PostTarget, bool]:
        """Require the same row identity and body across a bounded dwell."""
        if expected_root_handle is None:
            expected_root_handle = await root.element_handle()
        if expected_root_handle is None:
            raise _CardNotReady(root)
        target = await read_card(
            root,
            currently_visible=True,
            viewport_y=viewport_y,
            cached_body_fingerprints=cached_body_fingerprints,
            visible_text_progress=True,
            expected_root_handle=expected_root_handle,
            isolated_scope=isolated_scope,
        )
        for attempt in range(2):
            # Measure stability between completed samples. DOM extraction can
            # take longer than the interval on a busy feed, so compare actual
            # content and retry once after hydration settles if it changed.
            if scan_operation is not None:
                scan_operation("STABILITY_CHECK")
            await asyncio.sleep(max(1, body_stability_interval_ms) / 1000)
            settled = await read_card(
                root,
                currently_visible=True,
                viewport_y=viewport_y,
                cached_body_fingerprints=cached_body_fingerprints,
                visible_text_progress=True,
                expected_root_handle=expected_root_handle,
                isolated_scope=isolated_scope,
            )
            body_same = (
                _canonicalize_body(target.body or "") == _canonicalize_body(settled.body or "")
                and target.body_complete == settled.body_complete
                and target.body_truncated == settled.body_truncated
            )
            identity_same = target.url == settled.url and target.author == settled.author
            if body_same and identity_same and settled.body_consistent:
                segments = tuple(
                    {
                        segment.segment_id: segment
                        for segment in target.visible_segments + settled.visible_segments
                    }.values()
                )
                return replace(settled, visible_segments=segments), True
            if (
                allow_hydration_retry
                and attempt == 0
                and await wait_for_stable_dom(root)
            ):
                target = await read_card(
                    root,
                    currently_visible=True,
                    viewport_y=viewport_y,
                    cached_body_fingerprints=cached_body_fingerprints,
                    visible_text_progress=True,
                    expected_root_handle=expected_root_handle,
                    isolated_scope=isolated_scope,
                )
                continue
            return settled, False
        raise AssertionError("bounded card stability retry exhausted")

    async def visible_body_candidate_state(
        self,
        root: Locator,
        *,
        fingerprint: str | None = None,
    ) -> tuple[bool, bool]:
        return await self._body_text._visible_body_candidate_state(
            root,
            fingerprint=fingerprint,
        )
