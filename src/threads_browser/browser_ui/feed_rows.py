"""Playwright DOM discovery and structural evidence for feed rows."""

from __future__ import annotations

from dataclasses import dataclass, field

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import JSHandle, Locator, Page

from ..errors import UiReadError
from ..feed.models import BodyReadEvidence
from ..feed.urls import _post_url, _post_url_hint, _profile_author
from .geometry import with_scrollport_geometry as _with_scrollport_geometry

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
_MAX_STRUCTURE_ELEMENTS = 200


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

    async def _belongs_to_card(
        self,
        element: Locator,
        root: Locator,
        root_handle: JSHandle,
    ) -> bool:

        return bool(
            await element.evaluate(
                """(node, args) => {

                    if (node.closest(args.quote)) return false;

                    const nearest = node.parentElement?.closest(args.selector);

                    if (args.root.matches(args.selector)) {

                        return nearest === args.root;

                    }

                    return args.root.contains(node) && nearest === null;

                }""",
                {
                    "selector": _FEED_ROOT_SELECTOR,
                    "quote": _QUOTE_SELECTOR,
                    "root": root_handle,
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
