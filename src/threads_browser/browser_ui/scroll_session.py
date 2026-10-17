"""Measured feed-scroll operations and their per-reader progress state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import JSHandle, Page

from ..console_output import ConsoleOutputPolicy, emit_console_diagnostic
from ..errors import UiReadError
from ..feed.models import HomeSample, PostTarget
from .feed_rows import _FeedScanCounts
from .geometry import with_scrollport_geometry as _with_scrollport_geometry
from .scrolling import NativeWheelScrollEngine, ScrollGestureResult
from .session import classify_playwright_failure, emit_failure_diagnostic
from .viewport_scan_models import _TrackedRowIdentity

_SCROLL_ADVANCE_RATIO = 0.4
_SCROLL_ADVANCE_MIN_PX = 64
_SCROLL_ADVANCE_MAX_PX = 360


@dataclass(slots=True)
class FeedScrollState:
    """Measured wheel progress retained for one reader session."""

    scroll_attempts: int = 0
    scrolls: int = 0
    last_scroll_delta: float = 0.0
    last_scroll_gesture: ScrollGestureResult | None = None
    last_scroll_band: str = "UNKNOWN"
    last_scroll_end_distance: float | None = None
    last_scroll_near_end: bool = False
    last_scroll_stop_reason: str | None = None
    last_scroll_surface_key: str | None = None
    last_scroll_geometry: dict[str, object] | None = None
    last_scroll_targets: tuple[PostTarget, ...] = ()
    last_viewport_signature: tuple[object, ...] | None = None


class FeedScrollHost(Protocol):
    _page: Page
    _detail_mode: bool
    _failure_phase: str
    _console_output_policy: ConsoleOutputPolicy
    _readiness_timeout_ms: int
    _scroll_advance_ratio: float
    _scroll_advance_min_px: int
    _scroll_advance_max_px: int
    _max_permalink_candidates: int
    last_scan_counts: _FeedScanCounts
    last_display_observations: tuple
    last_viewport_coverage_complete: bool
    last_viewport_observed_rows: int
    _last_viewport_candidate_identities: tuple[_TrackedRowIdentity, ...]

    async def _wait_for_verified_feed(self, deadline: float, *, timeout_code: str) -> None: ...
    def _readiness_deadline(self, timeout_ms: int) -> float: ...
    async def _scroll_surface(self) -> JSHandle | None: ...
    async def _safe_feed_scroll_point(
        self, surface: JSHandle
    ) -> tuple[tuple[float, float], int, float, str]: ...
    async def _scroll_surface_snapshot(self, surface: JSHandle) -> dict[str, object]: ...
    def _mark_ui_transition(self) -> None: ...
    def _observe_feed_targets(self, *args, **kwargs): ...
    async def _await_render_settled(self, *args, **kwargs): ...
    async def _recoverable_stage(self, *args, **kwargs): ...
    async def _remember_viewport_candidates(self, roots, identities) -> None: ...
    async def _visible_feed_evidence(self) -> bool: ...
    async def _content_region(self): ...
    async def advance_feed(self, *, scan_after_scroll: bool = True) -> bool: ...
    async def scan_viewport(
        self, max_posts: int, *, scanned_fingerprints: set[str] | None = None
    ) -> tuple[PostTarget, ...]: ...
    @property
    def feed_duplicates(self) -> int: ...
    @feed_duplicates.setter
    def feed_duplicates(self, value: int) -> None: ...
    @property
    def scroll_attempts(self) -> int: ...
    @scroll_attempts.setter
    def scroll_attempts(self, value: int) -> None: ...
    @property
    def scrolls(self) -> int: ...
    @scrolls.setter
    def scrolls(self, value: int) -> None: ...
    @property
    def skipped_unverified(self) -> int: ...


class FeedScrollSession:
    """Own bounded wheel interactions, geometry checks, and scroll counters."""

    def __init__(self, host: FeedScrollHost) -> None:
        self._host = host
        self._engine = NativeWheelScrollEngine(host._page)
        self.state = FeedScrollState()

    @staticmethod
    def _has_new_viewport_content(
        previous: tuple[object, ...] | None,
        current: tuple[object, ...],
        scroll_delta: float,
    ) -> bool:
        if previous is None:
            return True
        previous_verified = previous[0]
        current_verified = current[0]
        previous_urls = {entry[0] for entry in previous_verified}
        if any(entry[0] not in previous_urls for entry in current_verified):
            return True
        previous_positions = {entry[0]: entry[4] for entry in previous_verified}
        if any(
            entry[0] in previous_positions
            and abs((previous_positions[entry[0]] - scroll_delta) - entry[4]) <= 48
            for entry in current_verified
        ):
            return True
        if current[2] > previous[2]:
            return True

        previous_observations = previous[1]
        matched: set[int] = set()
        for body, _status, viewport_y in current[1]:
            if body is None:
                continue
            for index, (previous_body, _previous_status, previous_y) in enumerate(
                previous_observations
            ):
                if index in matched or previous_body != body:
                    continue
                if abs((previous_y - scroll_delta) - viewport_y) <= 48:
                    matched.add(index)
                    break
            else:
                return True
        return False

    async def advance_feed(self, *, scan_after_scroll: bool = True) -> bool:
        """Issue a bounded native wheel gesture and measure its displacement."""
        self._host.last_scroll_stop_reason = None
        self._host.last_scroll_targets = ()
        await self._host._wait_for_verified_feed(
            self._host._readiness_deadline(self._host._readiness_timeout_ms),
            timeout_code="HOME_SCROLL_SURFACE_UNVERIFIED",
        )
        surface = await self._host._scroll_surface()
        if surface is None:
            self._host.last_scroll_stop_reason = "FEED_NOT_SCROLLABLE"
            return False
        try:
            try:
                (
                    point,
                    wheel_delta,
                    viewport_height,
                    hit_test_tag,
                ) = await self._host._safe_feed_scroll_point(surface)
                scroll_state = await self._host._scroll_surface_snapshot(surface)
                initial_position = float(scroll_state["top"])
                if float(scroll_state["max"]) - initial_position <= 1:
                    self._host.last_scroll_stop_reason = "FEED_END_REACHED"
                    return False
                self._host.scroll_attempts += 1
                gesture = await self._engine.advance(
                    surface,
                    point,
                    wheel_delta,
                    viewport_height,
                )
                self._host.last_scroll_gesture = gesture
                self._host._mark_ui_transition()
            except PlaywrightError as error:
                failure = classify_playwright_failure(self._host._page, error)
                emit_failure_diagnostic(
                    phase=self._host._failure_phase,
                    operation="SCROLL",
                    error_class=failure.error_class,
                    page_closed=failure.page_closed,
                    context_closed=failure.context_closed,
                    console_output_policy=self._host._console_output_policy,
                )
                if not self._host._detail_mode:
                    emit_console_diagnostic(
                        self._host._console_output_policy,
                        f"SCROLL attempt={self._host.scroll_attempts} observed=0",
                    )
                code = (
                    "BROWSER_SESSION_UNAVAILABLE"
                    if failure.error_class == "TARGET_CLOSED"
                    else "UI_READ_UNVERIFIED"
                )
                raise UiReadError(code) from None
            observed_delta = gesture.observed_delta
            if observed_delta <= 1:
                if not self._host._detail_mode:
                    emit_console_diagnostic(
                        self._host._console_output_policy,
                        f"SCROLL attempt={self._host.scroll_attempts} observed=0",
                    )
                raise UiReadError("HOME_SCROLL_UNVERIFIED")
            if observed_delta > viewport_height * 0.5 + 1:
                raise UiReadError("HOME_SCROLL_ADVANCE_OVERSIZED")

            async def observe_scroll(window_deadline: float) -> float:
                await self._host._wait_for_verified_feed(
                    window_deadline,
                    timeout_code="HOME_SCROLL_UNVERIFIED",
                )

                if not scan_after_scroll:
                    return observed_delta

                previous_signature = self._host._last_viewport_signature

                async def observe_new_viewport():
                    observation = await self._host._observe_feed_targets(
                        20,
                        window_deadline,
                        timeout_code="HOME_SCROLL_UNVERIFIED",
                        currently_visible=True,
                        prefer_new_fingerprints=None,
                        stage="FEED_SCROLL",
                        report_rescan=True,
                    )
                    if observation is None or not self._has_new_viewport_content(
                        previous_signature,
                        observation.signature,
                        observed_delta,
                    ):
                        return None
                    return observation

                rendered = await self._host._await_render_settled(
                    "FEED_SCROLL",
                    window_deadline,
                    timeout_code="HOME_SCROLL_UNVERIFIED",
                    observe=observe_new_viewport,
                    validate=lambda: self._host._wait_for_verified_feed(
                        window_deadline,
                        timeout_code="HOME_SCROLL_UNVERIFIED",
                    ),
                )
                self._host._last_viewport_signature = rendered.render_signature
                self._host.last_scroll_targets = rendered.targets
                self._host.last_display_observations = rendered.display_observations
                self._host.last_viewport_coverage_complete = rendered.coverage_complete
                self._host.last_viewport_observed_rows = rendered.observed_rows
                await self._host._remember_viewport_candidates(
                    rendered.candidate_roots,
                    rendered.candidate_identities,
                )
                return observed_delta

            try:
                observed_delta = await self._host._recoverable_stage(
                    "HOME_SCROLL",
                    "HOME_SCROLL_UNVERIFIED",
                    observe_scroll,
                    operation="SETTLE",
                )
            except UiReadError:
                if not self._host._detail_mode:
                    emit_console_diagnostic(
                        self._host._console_output_policy,
                        f"SCROLL attempt={self._host.scroll_attempts} observed=0",
                    )
                raise
            self._host.scrolls += 1
            self._host.last_scroll_delta = observed_delta
            final_surface = await self._host._scroll_surface_snapshot(surface)
            self._host.last_scroll_surface_key = str(final_surface["key"])
            self._host.last_scroll_geometry = {
                "surface_key": self._host.last_scroll_surface_key,
                "surface_tag": str(final_surface["tag"]),
                "overflow_y": str(final_surface["overflowY"]),
                "hit_test_tag": hit_test_tag,
                "scroll_top_before": float(scroll_state["top"]),
                "scroll_top_after": float(final_surface["top"]),
                "scroll_height_before": float(scroll_state["scrollHeight"]),
                "scroll_height_after": float(final_surface["scrollHeight"]),
                "clip_top_before": float(scroll_state["clipTop"]),
                "clip_top_after": float(final_surface["clipTop"]),
                "clip_bottom_before": float(scroll_state["clipBottom"]),
                "clip_bottom_after": float(final_surface["clipBottom"]),
                "scroll_top_delta": (float(final_surface["top"]) - float(scroll_state["top"])),
                "scroll_height_delta": (
                    float(final_surface["scrollHeight"]) - float(scroll_state["scrollHeight"])
                ),
            }
            final_scroll_state = await surface.evaluate(
                "element => Math.max(0, element.scrollHeight - element.clientHeight - element.scrollTop)"
            )
            self._host.last_scroll_end_distance = float(final_scroll_state)
            self._host.last_scroll_near_end = self._host.last_scroll_end_distance <= max(
                wheel_delta * 1.5,
                viewport_height * 0.75,
            )
            delta_ratio = observed_delta / viewport_height
            delta_band = (
                "SMALL" if delta_ratio <= 0.5 else "MEDIUM" if delta_ratio <= 0.75 else "LARGE"
            )
            self._host.last_scroll_band = delta_band
            if not self._host._detail_mode:
                emit_console_diagnostic(
                    self._host._console_output_policy,
                    f"SCROLL attempt={self._host.scroll_attempts} observed=1 delta_band={delta_band}",
                )
            return True
        finally:
            await surface.dispose()

    async def _scroll_surface_snapshot(
        self,
        surface: JSHandle,
    ) -> dict[str, object]:
        return await surface.evaluate(
            _with_scrollport_geometry("""element => {
                /*SCROLLPORT_GEOMETRY*/
                const bounds = scrollportBoundsFor(element);
                return {
                    key: keyFor(element),
                    tag: bounds.documentSurface ? "DOCUMENT" : element.tagName,
                    overflowY: bounds.documentSurface
                        ? "document" : getComputedStyle(element).overflowY,
                    top: bounds.documentSurface ? window.scrollY : element.scrollTop,
                    max: bounds.documentSurface
                        ? Math.max(0, element.scrollHeight - window.innerHeight)
                        : Math.max(0, element.scrollHeight - element.clientHeight),
                    scrollHeight: element.scrollHeight,
                    clipTop: bounds.top,
                    clipBottom: bounds.bottom,
                };
            }""")
        )

    async def sample_and_scroll(self, max_posts: int) -> HomeSample:
        """Compatibility helper for callers that need a before/after sample."""
        self._host.scroll_attempts = 0
        self._host.scrolls = 0
        self._host.feed_duplicates = 0
        before = await self._host.scan_viewport(max_posts)
        before_by_fingerprint = {target.fingerprint: target for target in before}
        self._host.feed_duplicates += len(before) - len(before_by_fingerprint)
        advanced = await self._host.advance_feed()
        if not advanced and self._host.last_scroll_stop_reason == "FEED_NOT_SCROLLABLE":
            raise UiReadError("HOME_SCROLL_SURFACE_UNVERIFIED")
        after = await self._host.scan_viewport(
            max_posts,
            scanned_fingerprints=set(before_by_fingerprint),
        )
        after_by_fingerprint = {target.fingerprint: target for target in after}
        self._host.feed_duplicates += len(after) - len(after_by_fingerprint)
        for fingerprint in after_by_fingerprint:
            if fingerprint in before_by_fingerprint:
                self._host.feed_duplicates += 1
        after_first = list(after_by_fingerprint.values())
        after_first.extend(
            target
            for fingerprint, target in before_by_fingerprint.items()
            if fingerprint not in after_by_fingerprint
        )
        return HomeSample(
            targets=tuple(after_first[:max_posts]),
            observed_targets=tuple(after_first),
            skipped_unverified=self._host.skipped_unverified,
        )

    async def _safe_feed_scroll_point(
        self,
        surface: JSHandle,
    ) -> tuple[tuple[float, float], int, float, str]:
        viewport = self._host._page.viewport_size
        if viewport is None:
            raise UiReadError("FEED_VIEWPORT_UNVERIFIED")
        box = await surface.evaluate(
            _with_scrollport_geometry("""element => {
                /*SCROLLPORT_GEOMETRY*/
                const bounds = scrollportBoundsFor(element);
                return {
                    x: bounds.left, y: bounds.top,
                    width: bounds.right - bounds.left,
                    height: bounds.bottom - bounds.top,
                };
            }""")
        )
        if not box or box["width"] <= 0 or box["height"] <= 0:
            raise UiReadError("HOME_SCROLL_SURFACE_UNVERIFIED")
        left = max(0.0, box["x"])
        right = min(float(viewport["width"]), box["x"] + box["width"])
        top = max(0.0, box["y"])
        bottom = min(float(viewport["height"]), box["y"] + box["height"])
        if left >= right or top >= bottom:
            raise UiReadError("HOME_SCROLL_SURFACE_UNVERIFIED")
        visible_height = bottom - top
        if visible_height < 2:
            raise UiReadError("HOME_SCROLL_SURFACE_UNVERIFIED")
        advance = min(
            self._host._scroll_advance_max_px,
            max(
                self._host._scroll_advance_min_px,
                round(visible_height * self._host._scroll_advance_ratio),
            ),
            max(1, int(visible_height * 0.5)),
        )
        xs = (left + (right - left) * 0.55, left + (right - left) * 0.75)
        ys = (
            top + (bottom - top) * 0.4,
            top + (bottom - top) * 0.6,
            top + (bottom - top) * 0.8,
        )
        for y in ys:
            for x in xs:
                safe = await surface.evaluate(
                    """(surface, point) => {
                        const target = document.elementFromPoint(point.x, point.y);
                        if (!target || !surface.contains(target)) return false;
                        const excluded = target.closest(
                            "nav, aside, footer, [role='navigation'], [role='menu'], " +
                            "[role='complementary'], [role='dialog'], [role='search'], " +
                            "[role='banner'], [role='contentinfo']"
                        );
                        const control = target.closest(
                            "a, button, input, select, textarea, [role='button'], " +
                            "[role='link'], [role='tab'], [role='menuitem']"
                        );
                        const nestedScroller = (() => {
                            for (let current = target; current && current !== surface;
                                 current = current.parentElement) {
                                const style = getComputedStyle(current);
                                if (current.scrollHeight > current.clientHeight + 1 &&
                                    ["auto", "scroll", "overlay"].includes(
                                        style.overflowY
                                    )) return true;
                            }
                            return false;
                        })();
                        return !excluded && !control && !nestedScroller;
                    }""",
                    {"x": x, "y": y},
                )
                if safe:
                    hit_type = await surface.evaluate(
                        """(surface, point) => {
                            const target = document.elementFromPoint(point.x, point.y);
                            return target?.tagName ?? "UNKNOWN";
                        }""",
                        {"x": x, "y": y},
                    )
                    return (x, y), advance, visible_height, str(hit_type)
        raise UiReadError("HOME_SCROLL_SURFACE_UNVERIFIED")

    async def _scroll_surface(self) -> JSHandle | None:
        if not await self._host._visible_feed_evidence():
            self._host.last_scan_counts.scroll_surface_status = "UNVERIFIED"
            raise UiReadError("HOME_SCROLL_SURFACE_UNVERIFIED")
        region = await self._host._content_region()
        viewport = self._host._page.viewport_size
        if viewport is None:
            self._host.last_scan_counts.scroll_surface_status = "UNVERIFIED"
            raise UiReadError("FEED_VIEWPORT_UNVERIFIED")
        region_handle = await region.element_handle()
        if region_handle is None:
            self._host.last_scan_counts.scroll_surface_status = "UNVERIFIED"
            raise UiReadError("HOME_SCROLL_SURFACE_UNVERIFIED")
        row_handles = [
            identity.dom_handle for identity in self._host._last_viewport_candidate_identities
        ]
        try:
            surface_state = await self._host._page.evaluate_handle(
                _with_scrollport_geometry("""([region, rows, maxCandidates]) => {
                /*SCROLLPORT_GEOMETRY*/
                const root = document.scrollingElement;
                const visibleItem = item => {
                    if (!item.isConnected || (item !== region && !region.contains(item)) ||
                        item.closest(
                        "nav, aside, footer, [role='navigation'], [role='menu'], " +
                        "[role='complementary'], [role='dialog'], [role='search'], " +
                        "[role='banner'], [role='contentinfo'], form, " +
                        "[contenteditable='true'], [role='textbox']"
                    )) return false;
                    const style = window.getComputedStyle(item);
                    const rect = item.getBoundingClientRect();
                    return style.display !== "none" && style.visibility !== "hidden" &&
                        rect.width > 0 && rect.height > 0 &&
                        geometryFor(item).intersects;
                };
                const postPathHint = item => {
                    const href = item.getAttribute("href");
                    if (!href) return false;
                    try {
                        return new URL(href, location.href).pathname
                            .split("/").includes("post");
                    } catch { return false; }
                };
                let items = rows;
                if (items.length && !items.every(visibleItem)) return null;
                if (!items.length) {
                    items = Array.from(region.querySelectorAll("a"))
                        .filter(postPathHint).slice(0, maxCandidates)
                        .filter(visibleItem);
                }
                if (!items.length) return {state: "NOT_SCROLLABLE", surface: null};

                const candidates = new Set();
                for (const item of items) {
                    for (let element = item; element;
                         element = element.parentElement) {
                        if ((element === root &&
                             root.scrollHeight > root.clientHeight + 1) ||
                            isScrollable(element)) {
                            candidates.add(element);
                        }
                    }
                }
                const innermost = Array.from(candidates).filter(candidate =>
                    items.every(item => candidate === item || candidate.contains(item)) &&
                    !Array.from(candidates).some(other => candidate !== other &&
                        candidate.contains(other) &&
                        items.every(item => other === item || other.contains(item)))
                );
                if (!candidates.size) {
                    return {state: "NOT_SCROLLABLE", surface: null};
                }
                if (innermost.length !== 1) {
                    return {state: "AMBIGUOUS", surface: null};
                }
                const surface = innermost[0];
                if (!items.every(item => surface === item || surface.contains(item))) {
                    return {state: "AMBIGUOUS", surface: null};
                }
                return {
                    state: "READY", surface, surfaceKey: keyFor(surface),
                    surfaceTag: surface === root ? "DOCUMENT" : surface.tagName,
                    overflowY: surface === root
                        ? "document" : getComputedStyle(surface).overflowY,
                };
                }"""),
                arg=[region_handle, row_handles, self._host._max_permalink_candidates],
            )
        finally:
            await region_handle.dispose()
        state_handle = await surface_state.get_property("state")
        state = await state_handle.json_value()
        await state_handle.dispose()
        if state == "NOT_SCROLLABLE":
            await surface_state.dispose()
            self._host.last_scan_counts.scroll_surface_status = "NOT_SCROLLABLE"
            return None
        if state != "READY":
            await surface_state.dispose()
            self._host.last_scan_counts.scroll_surface_status = "UNVERIFIED"
            raise UiReadError("HOME_SCROLL_SURFACE_UNVERIFIED")
        key_handle = await surface_state.get_property("surfaceKey")
        key_value = await key_handle.json_value()
        await key_handle.dispose()
        self._host.last_scroll_surface_key = str(key_value)
        surface = await surface_state.get_property("surface")
        await surface_state.dispose()
        self._host.last_scan_counts.scroll_surface_status = "VALIDATED"
        return surface
