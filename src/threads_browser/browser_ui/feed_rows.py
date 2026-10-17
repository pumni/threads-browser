"""Playwright DOM discovery and structural evidence for feed rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Literal

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import JSHandle, Locator, Page

from ..errors import UiReadError
from ..feed.models import BodyReadEvidence
from ..feed.urls import (
    _CandidateDiagnosticReason,
    _post_url,
    _post_url_hint,
    _post_url_rejection_reason,
    _profile_author,
    _rejected_post_link_blocks_candidate,
)
from .geometry import with_scrollport_geometry as _with_scrollport_geometry
from .loading import CardNotReady

_FEED_ROOT_SELECTOR = 'article, [role="article"], [data-pressable-container="true"]'
_BODY_TEXT_SELECTOR = "p, span, div"
_QUOTE_SELECTOR = 'blockquote, [role="quote"]'
_BODY_EXCLUSION_SELECTOR = (
    'a, button, time, [role="button"], [role="link"], [aria-hidden="true"], '
    'nav, aside, footer, form, [role="navigation"], [role="menu"], '
    '[role="complementary"], [role="dialog"], [role="search"], '
    '[role="textbox"], [contenteditable="true"], blockquote, [role="quote"]'
)
_MAX_CARD_LINKS = 40
_MAX_CARD_ANCESTORS = 8
_MAX_PERMALINK_CANDIDATES = 40
_MAX_STRUCTURE_ELEMENTS = 200


class UnverifiedFeedCandidate(UiReadError):
    def __init__(
        self,
        code: str,
        reason: Literal[
            "CANDIDATE_AMBIGUOUS",
            "CANDIDATE_NONCANONICAL",
            "CANDIDATE_AUTHOR_CONFLICT",
        ],
        identities: tuple[str, ...],
        *,
        diagnostic_reason: _CandidateDiagnosticReason | None = None,
    ) -> None:
        self.reason = reason
        self.diagnostic_reason = diagnostic_reason
        self.identities = frozenset(
            sha256(identity.encode("utf-8")).hexdigest() for identity in identities if identity
        )
        super().__init__(code)


@dataclass(slots=True)
class _CandidateDiagnostic:
    row_index: int
    root_kind: str
    has_visible_time: bool
    has_post_permalink: bool
    has_profile_author: bool
    has_readable_body: bool
    state: str = "NON_POST"
    reason: str = "NO_POST_ROW_EVIDENCE"
    identity_fingerprint: str | None = field(default=None, repr=False)
    author_fingerprint: str | None = field(default=None, repr=False)
    observation_id: str | None = field(default=None, repr=False)
    read_evidence: BodyReadEvidence | None = field(default=None, repr=False)
    same_scroller_scroll_top_delta: float | None = field(default=None, repr=False)
    row_movement_px: float | None = field(default=None, repr=False)
    text_distance_delta_px: float | None = field(default=None, repr=False)
    row_top_before_px: float | None = field(default=None, repr=False)
    row_bottom_before_px: float | None = field(default=None, repr=False)
    clip_top_before_px: float | None = field(default=None, repr=False)
    clip_bottom_before_px: float | None = field(default=None, repr=False)
    text_top_before_px: float | None = field(default=None, repr=False)
    text_bottom_before_px: float | None = field(default=None, repr=False)
    wheel_surface_matches: bool | None = field(default=None, repr=False)
    scroll_reason_flags: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class _CandidateStructure:
    visible_anchors: int = 0
    visible_post_links: int = 0
    canonical_post_links: int = 0
    profile_anchor_candidates: int = 0
    visible_times: int = 0
    body_candidates: int = 0
    readable_body_candidates: int = 0
    paragraph_candidates: int = 0
    offscreen_post_links: int = 0
    offscreen_profile_anchors: int = 0
    offscreen_times: int = 0
    offscreen_body_candidates: int = 0
    post_url_links: tuple[tuple[str, bool], ...] = ()
    root_kind: str = "CONTAINER"


@dataclass(frozen=True, slots=True)
class _CandidateIdentityEvidence:
    root_matches: bool
    canonical_fingerprint: str | None
    post_fingerprints: frozenset[str] = frozenset()
    author_fingerprints: frozenset[str] = frozenset()

    def conflicts_with(
        self,
        fingerprint: str,
        author_fingerprint: str | None,
    ) -> bool:
        return any(value != fingerprint for value in self.post_fingerprints) or any(
            author_fingerprint is None or value != author_fingerprint
            for value in self.author_fingerprints
        )


@dataclass(slots=True)
class _FeedScanCounts:
    visible_links: int = 0
    inspected: int = 0
    article_roots: int = 0
    pressable_roots: int = 0
    visible_times: int = 0
    profile_anchor_candidates: int = 0
    visible_anchors: int = 0
    visible_post_links: int = 0
    body_candidates: int = 0
    paragraph_candidates: int = 0
    url_parsed: int = 0
    url_timestamp_links: int = 0
    url_secondary_links: int = 0
    url_canonical_links: int = 0
    url_rejected_links: int = 0
    author_unverified: int = 0
    wrapper_ambiguous: int = 0
    card_not_ready: int = 0
    deferred_offscreen: int = 0
    identity_verified: int = 0
    new_verified_rows: int = 0
    partial_body_rows: int = 0
    deferred_identity_rows: int = 0
    new_visible_text_segments: int = 0
    deferred_above: int = 0
    deferred_below: int = 0
    nearest_deferred_below_px: float | None = None
    frontier_content_px: float | None = None
    verified: int = 0
    body_read: int = 0
    body_unknown: int = 0
    observed_rows: int = 0
    body_extracted: int = 0
    skipped_rows: int = 0
    uncertain_rows: int = 0
    coverage_complete: bool = True
    skipped: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    url_structure_counts: dict[str, int] = field(default_factory=dict)
    structural_reasons: dict[str, int] = field(default_factory=dict)
    unsupported_roots: int = 0
    roots_truncated: bool = False
    unrepresented_root_count: int = 0
    unresolved_visited_rows: int = 0
    lookahead_rows: int = 0
    region_status: str = "UNSET"
    scroll_surface_status: str = "NOT_CHECKED"


@dataclass(slots=True)
class _FeedScanTimings:
    discover_ms: int = 0
    read_row_ms: int = 0
    stabilize_ms: int = 0
    wait_reason: str = "NONE"


class FeedRowScanner:
    """Find visible feed roots and collect safe row-structure evidence."""

    def __init__(self, page: Page) -> None:
        self._page = page
        self.last_scan_counts = _FeedScanCounts()

    async def _candidate_identity_evidence(
        self,
        root: Locator,
        root_handle: JSHandle,
    ) -> _CandidateIdentityEvidence:
        links = await root.evaluate(
            _with_scrollport_geometry(
                """(node, args) => {
                    /*SCROLLPORT_GEOMETRY*/
                    if (node !== args.expected) return null;
                    const visible = element => {
                        for (let current = element; current;
                             current = current.parentElement) {
                            const style = getComputedStyle(current);
                            if (style.display === "none" ||
                                style.visibility === "hidden" ||
                                Number(style.opacity) === 0) return false;
                        }
                        return geometryFor(element).intersects;
                    };
                    const links = [];
                    for (const link of node.querySelectorAll("a[href]")) {
                        if (link.closest(args.quote) || link.closest(args.excluded)) continue;
                        const nearest = link.parentElement?.closest(args.roots);
                        const belongs = node.matches(args.roots)
                            ? nearest === node
                            : node.contains(link) && !nearest;
                        if (!belongs || !visible(link)) continue;
                        const time = link.querySelector("time");
                        links.push({
                            href: link.getAttribute("href"),
                            timestamp: Boolean(time && visible(time)),
                        });
                        if (links.length > args.maxLinks) break;
                    }
                    return links;
                }"""
            ),
            {
                "expected": root_handle,
                "roots": _FEED_ROOT_SELECTOR,
                "quote": _QUOTE_SELECTOR,
                "excluded": (
                    "nav, aside, footer, form, [contenteditable='true'], "
                    "[role='navigation'], [role='menu'], "
                    "[role='complementary'], [role='dialog'], "
                    "[role='search'], [role='banner'], "
                    "[role='contentinfo'], [role='textbox']"
                ),
                "maxLinks": _MAX_CARD_LINKS,
            },
        )
        if links is None or len(links) > _MAX_CARD_LINKS:
            return _CandidateIdentityEvidence(False, None)

        post_fingerprints: set[str] = set()
        timestamp_fingerprints: set[str] = set()
        route_author_fingerprints: set[str] = set()
        author_fingerprints: set[str] = set()
        for item in links:
            href = item["href"]
            parsed = _post_url(href)
            if parsed is not None:
                url, route_author = parsed
                fingerprint = sha256(f"{url}\n{route_author}".encode()).hexdigest()
                route_author_fingerprint = sha256(route_author.encode()).hexdigest()
                post_fingerprints.add(fingerprint)
                route_author_fingerprints.add(route_author_fingerprint)
                if item["timestamp"]:
                    timestamp_fingerprints.add(fingerprint)
                continue
            if _post_url_hint(href):
                continue
            author = _profile_author(href)
            if author is not None:
                author_fingerprints.add(sha256(author.encode()).hexdigest())

        canonical_fingerprint: str | None = None
        if (
            len(post_fingerprints) == 1
            and len(timestamp_fingerprints) == 1
            and len(route_author_fingerprints) == 1
            and author_fingerprints == route_author_fingerprints
        ):
            canonical_fingerprint = next(iter(timestamp_fingerprints))
        return _CandidateIdentityEvidence(
            True,
            canonical_fingerprint,
            frozenset(post_fingerprints),
            frozenset(author_fingerprints),
        )

    async def _belongs_to_card(
        self,
        element: Locator,
        root: Locator,
        root_handle: JSHandle,
        *,
        isolated_scope: bool = False,
    ) -> bool:

        return bool(
            await element.evaluate(
                """(node, args) => {

                    if (node.closest(args.quote)) return false;

                    const nearest = node.parentElement?.closest(args.selector);

                    if (args.root.matches(args.selector)) {

                        return nearest === args.root;

                    }

                    if (args.isolatedScope) {

                        const owner = args.root.parentElement?.closest(args.selector);

                        return args.root.contains(node) &&
                            (!nearest || nearest === owner);

                    }

                    return args.root.contains(node) && nearest === null;

                }""",
                {
                    "selector": _FEED_ROOT_SELECTOR,
                    "quote": _QUOTE_SELECTOR,
                    "root": root_handle,
                    "isolatedScope": isolated_scope,
                },
            )
        )

    @staticmethod
    async def _candidate_root_kind(root: Locator) -> str:

        tag = await root.evaluate("node => node.tagName.toLowerCase()")

        if tag == "article":
            return "ARTICLE"

        if await root.get_attribute("role") == "article":
            return "ROLE_ARTICLE"

        if await root.get_attribute("data-pressable-container") == "true":
            return "PRESSABLE"

        return "CONTAINER"

    async def _content_region(
        self,
        *,
        prefer_main: bool = False,
        error_code: str = "FEED_REGION_UNVERIFIED",
    ) -> Locator:

        roles = ("main", "feed") if prefer_main else ("feed", "main")

        for role in roles:
            landmarks = self._page.get_by_role(role)

            visible: list[Locator] = []

            for index in range(await landmarks.count()):
                landmark = landmarks.nth(index)

                if await landmark.is_visible():
                    visible.append(landmark)

            if len(visible) > 1:
                self.last_scan_counts.region_status = "AMBIGUOUS"

                raise UiReadError(error_code)

            if visible:
                self.last_scan_counts.region_status = "LANDMARK"

                return visible[0]

        self.last_scan_counts.region_status = "BODY_FALLBACK"

        return self._page.locator("body")

    async def _inspect_candidate_root(
        self,
        root: Locator,
        viewport: dict[str, int],
    ) -> _CandidateStructure:

        structure = await root.evaluate(
            """(root, args) => {

                const postPathHint = value => {

                    if (!value) return false;

                    try {

                        return new URL(value, location.href).pathname

                            .split("/").includes("post");

                    } catch { return false; }

                };

                const cssVisible = element => {

                    const style = getComputedStyle(element);

                    const rect = element.getBoundingClientRect();

                    return style.display !== "none" && style.visibility !== "hidden" &&

                        rect.width > 0 && rect.height > 0;

                };

                const intersectsVisibleArea = element => {

                    const rect = element.getBoundingClientRect();

                    let left = Math.max(0, rect.left);

                    let top = Math.max(0, rect.top);

                    let right = Math.min(args.viewport.width, rect.right);

                    let bottom = Math.min(args.viewport.height, rect.bottom);

                    for (let parent = element.parentElement; parent;

                         parent = parent.parentElement) {

                        const style = getComputedStyle(parent);

                        const clipX = style.overflowX !== "visible";

                        const clipY = style.overflowY !== "visible";

                        if (!clipX && !clipY) continue;

                        const bounds = parent.getBoundingClientRect();

                        const clipLeft = bounds.left + parent.clientLeft;

                        const clipTop = bounds.top + parent.clientTop;

                        const clipRight = clipLeft + parent.clientWidth;

                        const clipBottom = clipTop + parent.clientHeight;

                        if (clipX) {

                            left = Math.max(left, clipLeft);

                            right = Math.min(right, clipRight);

                        }

                        if (clipY) {

                            top = Math.max(top, clipTop);

                            bottom = Math.min(bottom, clipBottom);

                        }

                    }

                    return right > left && bottom > top;

                };

                const visible = element => cssVisible(element) &&

                    intersectsVisibleArea(element);

                const offscreen = element => cssVisible(element) &&

                    !intersectsVisibleArea(element);

                const belongs = element => {

                    if (element.closest(args.quote)) return false;

                    const nearest = element.parentElement?.closest(args.roots);

                    if (root.matches(args.roots)) return nearest === root;

                    return root.contains(element) && nearest === null;

                };

                const outside = element => element.closest(args.outside) !== null;

                const allAnchors = Array.from(root.querySelectorAll("a"))

                    .filter(element => belongs(element) && !outside(element));

                const anchors = allAnchors

                    .filter(visible)

                    .slice(0, args.maxLinks);

                const offscreenAnchors = allAnchors.filter(offscreen)

                    .slice(0, args.maxLinks);

                const allTimes = Array.from(root.querySelectorAll("time"))

                    .filter(element => belongs(element) && !outside(element));

                const times = allTimes.filter(visible).slice(0, args.maxLinks).length;

                const offscreenTimes = allTimes.filter(offscreen)

                    .slice(0, args.maxLinks).length;

                const bodySafe = element => {

                    let current = element;

                    while (current && current !== root) {

                        if (current.matches(args.bodyExcluded)) return false;

                        current = current.parentElement;

                    }

                    if (current !== root || !belongs(element)) return false;

                    return !element.querySelector(`${args.bodyExcluded}, ${args.roots}`);

                };

                const allBodies = Array.from(root.querySelectorAll(args.body))

                    .filter(bodySafe);

                const bodies = allBodies.filter(visible).slice(0, args.maxStructure);

                const offscreenBodies = allBodies.filter(offscreen)

                    .slice(0, args.maxStructure);

                return {

                    root_kind: root.tagName === "ARTICLE" ? "ARTICLE" :

                        root.getAttribute("role") === "article" ? "ROLE_ARTICLE" :

                        root.getAttribute("data-pressable-container") === "true" ? "PRESSABLE" :

                        "CONTAINER",

                    hrefs: anchors.map(element => element.getAttribute("href")),

                    post_links: anchors

                        .map(element => ({

                            href: element.getAttribute("href"),

                            timestamp: Boolean(

                                element.querySelector("time") &&

                                visible(element.querySelector("time"))

                            ),

                        }))

                        .filter(item => postPathHint(item.href)),

                    offscreen_hrefs: offscreenAnchors.map(element =>

                        element.getAttribute("href")),

                    visible_anchors: anchors.length,

                    visible_times: times,

                    offscreen_times: offscreenTimes,

                    body_candidates: bodies.length,

                    offscreen_body_candidates: offscreenBodies.length,

                    readable_body_candidates: bodies.filter(element => {

                        const walker = document.createTreeWalker(

                            element, NodeFilter.SHOW_TEXT

                        );

                        return Boolean(walker.nextNode());

                    }).length,

                    paragraph_candidates: bodies.filter(element =>

                        element.tagName === "P").length,

                };

            }""",
            {
                "viewport": viewport,
                "roots": _FEED_ROOT_SELECTOR,
                "quote": _QUOTE_SELECTOR,
                "outside": (
                    "nav, aside, footer, form, [contenteditable='true'], "
                    "[role='navigation'], [role='menu'], [role='complementary'], "
                    "[role='dialog'], [role='search'], [role='banner'], "
                    "[role='contentinfo'], [role='textbox']"
                ),
                "body": _BODY_TEXT_SELECTOR,
                "bodyExcluded": _BODY_EXCLUSION_SELECTOR,
                "maxLinks": _MAX_CARD_LINKS,
                "maxStructure": _MAX_STRUCTURE_ELEMENTS,
            },
        )

        href_list = tuple(href for href in structure["hrefs"] if href is not None)

        offscreen_hrefs = tuple(href for href in structure["offscreen_hrefs"] if href is not None)

        post_url_links = tuple(
            (item["href"], bool(item["timestamp"])) for item in structure["post_links"]
        )

        return _CandidateStructure(
            visible_anchors=structure["visible_anchors"],
            visible_post_links=sum(_post_url_hint(href) for href in href_list),
            canonical_post_links=sum(_post_url(href) is not None for href in href_list),
            profile_anchor_candidates=sum(
                self._profile_anchor_candidate(href) for href in href_list
            ),
            visible_times=structure["visible_times"],
            body_candidates=structure["body_candidates"],
            readable_body_candidates=structure["readable_body_candidates"],
            paragraph_candidates=structure["paragraph_candidates"],
            offscreen_post_links=sum(_post_url_hint(href) for href in offscreen_hrefs),
            offscreen_profile_anchors=sum(
                self._profile_anchor_candidate(href) for href in offscreen_hrefs
            ),
            offscreen_times=structure["offscreen_times"],
            offscreen_body_candidates=structure["offscreen_body_candidates"],
            post_url_links=post_url_links,
            root_kind=structure["root_kind"],
        )

    async def _is_in_visible_scrollports(self, element: Locator) -> bool:

        try:
            if await element.count() == 0:
                return False

            return bool(
                await element.evaluate(
                    _with_scrollport_geometry("""node => {

                        /*SCROLLPORT_GEOMETRY*/

                        if (!node.isConnected) return false;

                        for (let current = node; current;

                             current = current.parentElement) {

                            const style = getComputedStyle(current);

                            if (style.display === "none" ||

                                style.visibility === "hidden" ||

                                Number(style.opacity) === 0) return false;

                        }

                        return geometryFor(node).intersects;

                    }""")
                )
            )

        except PlaywrightError:
            return False

    @staticmethod
    async def _is_inside_quote(element: Locator) -> bool:

        return (
            await element.locator(
                "xpath=ancestor-or-self::*[self::blockquote or @role='quote']"
            ).count()
            > 0
        )

    async def _is_nested_feed_content(self, element: Locator) -> bool:

        return bool(
            await element.evaluate(
                """(node, selector) => {

                    const nearest = node.parentElement?.closest(selector);

                    return Boolean(nearest?.parentElement?.closest(selector));

                }""",
                _FEED_ROOT_SELECTOR,
            )
        )

    async def _is_nested_feed_root(self, root: Locator) -> bool:

        return bool(
            await root.evaluate(
                "(node, selector) => Boolean(node.parentElement?.closest(selector))",
                _FEED_ROOT_SELECTOR,
            )
        )

    @staticmethod
    async def _is_outside_content_landmark(link: Locator) -> bool:

        excluded = link.locator(
            "xpath=ancestor-or-self::*[self::nav or self::aside or self::footer "
            "or self::form or @contenteditable='true' "
            "or @role='navigation' or @role='menu' or @role='complementary' "
            "or @role='dialog' or @role='search' or @role='banner' "
            "or @role='contentinfo' or @role='textbox']"
        )

        return await excluded.count() > 0

    @staticmethod
    def _profile_anchor_candidate(value: str) -> bool:

        return _profile_author(value) is not None

    async def _safe_body_candidate(
        self,
        element: Locator,
        root: Locator,
        root_handle: JSHandle,
        *,
        isolated_scope: bool = False,
    ) -> bool:

        return bool(
            await element.evaluate(
                """(node, args) => {

                    const excluded = args.excluded;

                    let current = node;

                    while (current && current !== args.root) {

                        if (current.matches(excluded)) return false;

                        current = current.parentElement;

                    }

                    if (current !== args.root) return false;

                    const nearestRoot = node.parentElement?.closest(args.roots);

                    if (args.root.matches(args.roots)) {

                        if (nearestRoot !== args.root) return false;

                    } else if (args.isolatedScope) {

                        const owner = args.root.parentElement?.closest(args.roots);

                        if (!args.root.contains(node) ||
                            (nearestRoot && nearestRoot !== owner)) return false;

                    } else if (!args.root.contains(node) || nearestRoot) {

                        return false;

                    }

                    return !node.querySelector(

                        `${excluded}, ${args.roots}`

                    );

                }""",
                {
                    "root": root_handle,
                    "roots": _FEED_ROOT_SELECTOR,
                    "excluded": _BODY_EXCLUSION_SELECTOR,
                    "isolatedScope": isolated_scope,
                },
            )
        )

    async def _safe_unscoped_text_candidate(self, element: Locator) -> bool:

        if await self._is_outside_content_landmark(element):
            return False

        if await self._is_nested_feed_content(element):
            return False

        return bool(
            await element.evaluate(
                """(node, excluded) => {

                    let current = node;

                    while (current) {

                        if (current.matches(excluded)) return false;

                        current = current.parentElement;

                    }

                    return !node.querySelector(excluded);

                }""",
                _BODY_EXCLUSION_SELECTOR,
            )
        )

    async def _resolve_permalink_card(
        self,
        permalink: Locator,
        expected_url: str,
        expected_author: str,
    ) -> tuple[Locator, dict[str, float]]:
        if await self._is_nested_feed_content(permalink):
            raise UnverifiedFeedCandidate(
                "POST_PROVENANCE_UNVERIFIED",
                "CANDIDATE_AMBIGUOUS",
                (expected_url,),
                diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
            )
        if await self._is_inside_quote(permalink):
            raise UnverifiedFeedCandidate(
                "POST_PROVENANCE_UNVERIFIED",
                "CANDIDATE_AMBIGUOUS",
                (expected_url,),
                diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
            )
        candidate_root = permalink.locator(
            "xpath=ancestor::*[self::article or @role='article' "
            "or @data-pressable-container='true'][1]"
        )
        if await candidate_root.count() and not await self._is_nested_feed_root(candidate_root):
            box = await candidate_root.bounding_box()
            if box is not None:
                return candidate_root, box
        ancestor = permalink
        smallest_without_body: tuple[Locator, dict[str, float]] | None = None
        pending_author: Locator | None = None
        for _ in range(_MAX_CARD_ANCESTORS):
            ancestor = ancestor.locator("xpath=..")
            if await ancestor.count() == 0:
                break
            if (
                await ancestor.get_attribute("role") in {"feed", "main"}
                or await ancestor.locator("xpath=self::main").count() > 0
            ):
                break
            if not await ancestor.is_visible():
                continue

            links = ancestor.locator("a")
            link_count = await links.count()
            if link_count > _MAX_CARD_LINKS:
                raise UnverifiedFeedCandidate(
                    "POST_PROVENANCE_UNVERIFIED",
                    "CANDIDATE_AMBIGUOUS",
                    (expected_url,),
                    diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
                )
            post_authors: dict[str, str] = {}
            authors: set[str] = set()
            rejected_post_links: list[tuple[str, bool, _CandidateDiagnosticReason]] = []
            for index in range(link_count):
                link = links.nth(index)
                if (
                    await self._is_outside_content_landmark(link)
                    or await self._is_nested_feed_content(link)
                    or not await link.is_visible()
                ):
                    continue
                href = await link.get_attribute("href")
                if href is None:
                    continue
                parsed_post = _post_url(href)
                if parsed_post is not None:
                    post_authors[parsed_post[0]] = parsed_post[1]
                    continue
                if _post_url_hint(href):
                    rejected_post_links.append(
                        (
                            href,
                            await link.locator("time").count() > 0,
                            _post_url_rejection_reason(href),
                        )
                    )
                author = _profile_author(href)
                if author is not None:
                    authors.add(author)
            if len(post_authors) > 1:
                raise UnverifiedFeedCandidate(
                    "POST_PROVENANCE_UNVERIFIED",
                    "CANDIDATE_AMBIGUOUS",
                    tuple(post_authors),
                    diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
                )
            if expected_url not in post_authors:
                continue
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
                raise UnverifiedFeedCandidate(
                    "POST_PROVENANCE_UNVERIFIED",
                    "CANDIDATE_NONCANONICAL",
                    (expected_url, href),
                    diagnostic_reason=reason,
                )
            if post_authors[expected_url] != expected_author:
                raise UnverifiedFeedCandidate(
                    "POST_AUTHOR_UNVERIFIED",
                    "CANDIDATE_AUTHOR_CONFLICT",
                    (expected_url,),
                    diagnostic_reason="CANDIDATE_AUTHOR_MISMATCH",
                )
            if len(authors) > 1:
                raise UnverifiedFeedCandidate(
                    "POST_PROVENANCE_UNVERIFIED",
                    "CANDIDATE_AMBIGUOUS",
                    (expected_url,),
                    diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
                )
            if authors and expected_author not in authors:
                raise UnverifiedFeedCandidate(
                    "POST_AUTHOR_UNVERIFIED",
                    "CANDIDATE_AUTHOR_CONFLICT",
                    (expected_url,),
                    diagnostic_reason="CANDIDATE_AUTHOR_MISMATCH",
                )

            box = await ancestor.bounding_box()
            if box is None:
                continue
            if not authors:
                if pending_author is None:
                    pending_author = self._expected_profile_link_signal(
                        ancestor,
                        expected_author,
                    )
                continue
            candidate = (ancestor, box)
            if smallest_without_body is None:
                smallest_without_body = candidate
            paragraphs = ancestor.get_by_role("paragraph")
            paragraph_count = await paragraphs.count()
            if paragraph_count > 1:
                return candidate
            if paragraph_count == 1 and await paragraphs.first.is_visible():
                return candidate

        if pending_author is not None:
            raise CardNotReady(pending_author)
        if smallest_without_body is not None:
            return smallest_without_body
        raise UnverifiedFeedCandidate(
            "POST_PROVENANCE_UNVERIFIED",
            "CANDIDATE_AMBIGUOUS",
            (expected_url,),
            diagnostic_reason="CANDIDATE_WRAPPER_AMBIGUOUS",
        )

    @staticmethod
    def _expected_profile_link_signal(root: Locator, author: str) -> Locator:
        return root.locator(f'a[href$="/@{author}" i], a[href$="/@{author}/" i]').first
