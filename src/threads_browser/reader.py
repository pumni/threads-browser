"""Small async Playwright reader for a bounded Home / For You observation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from playwright.async_api import (
    Locator,
    Page,
)

from .browser_ui.body_text import (
    _MAX_BODY_CODEPOINTS,  # noqa: F401 - retained reader-module compatibility name
    BodyTextReader,
    _canonicalize_body,  # noqa: F401 - retained reader-module compatibility name
    _canonicalize_body_block,  # noqa: F401 - retained reader-module compatibility name
)
from .browser_ui.candidate_structure import CandidateStructureCollector
from .browser_ui.card_provenance import (
    CardProvenanceReader,
)
from .browser_ui.detail import DetailNavigator
from .browser_ui.feed_observation import (
    HomeFeedObserver,
)
from .browser_ui.feed_rows import (
    _MAX_CARD_LINKS,  # noqa: F401 - retained reader-module compatibility name
    FeedRowScanner,
    _CandidateDiagnostic,
    _FeedScanCounts,
    _FeedScanTimings,
)
from .browser_ui.feed_scan import FeedScanSession
from .browser_ui.feed_targets import VisiblePostTargetSelector
from .browser_ui.home_navigation import HomeNavigator
from .browser_ui.home_render import HomeRenderObserver
from .browser_ui.loading import (
    CardNotReady as _CardNotReady,  # noqa: F401 - retained reader-module compatibility name
)
from .browser_ui.loading import (
    LoadingObserver,
)
from .browser_ui.loading import (
    RenderObservation as _RenderObservation,  # noqa: F401 - retained reader-module compatibility name
)
from .browser_ui.login_navigation import LoginNavigator
from .browser_ui.recovery import ReaderRecovery
from .browser_ui.root_discovery import (
    _MAX_VISIBLE_ROW_CANDIDATES,  # noqa: F401 - retained reader-module compatibility name
    FeedRootDiscoverer,
)
from .browser_ui.scan_recovery import ScanFailureRevalidator
from .browser_ui.scroll_session import (
    FeedScrollSession,
)
from .browser_ui.session import (
    HomeSession,
)
from .browser_ui.viewport_scan import (
    ViewportScanCoordinator,
)
from .browser_ui.viewport_scan_models import (
    _HomeRowObservation,  # noqa: F401 - retained reader-module compatibility name
    _TrackedRowIdentity,
    _VisibleTargets,  # noqa: F401 - retained reader-module compatibility name
)
from .console_output import (
    ConsoleOutputPolicy,
)
from .errors import UiReadError
from .feed.models import (
    DisplayObservation,
    HomeSample,
    PostTarget,
)
from .feed.relevance import canonical_terms, classify_body, normalize_text
from .feed.urls import (
    _USERNAME,
    HOME_URL,
    THREADS_ORIGIN,
    _rejected_post_link_blocks_candidate,  # noqa: F401 - retained reader-module compatibility name
)
from .reader_facade.body import ReaderBodyCompatibility
from .reader_facade.constants import (
    _AUTOMATIC_READINESS_WINDOWS,
    _BODY_STABILITY_INTERVAL_MS,
    _CARD_HYDRATION_WAIT_MS,
    _DETAIL_TIMEOUT_MS,
    _NAVIGATION_TIMEOUT_MS,
    _READINESS_RECHECK_INTERVAL_MS,
    _READINESS_TIMEOUT_MS,
    _RENDER_MIN_DWELL_MS,
    _RENDER_SETTLE_INTERVAL_MS,
    _RENDER_STABLE_OBSERVATIONS,
)
from .reader_facade.detail import ReaderDetailCompatibility
from .reader_facade.feed import ReaderFeedCompatibility
from .reader_facade.navigation import ReaderNavigationCompatibility
from .reader_facade.state import ReaderStateCompatibility

__all__ = [
    "HOME_URL",
    "THREADS_ORIGIN",
    "DisplayObservation",
    "HomeSample",
    "PostTarget",
    "ThreadsHomeReader",
    "UiReadError",
    "canonical_terms",
    "classify_body",
    "normalize_text",
]

class ThreadsHomeReader(
    ReaderStateCompatibility,
    ReaderNavigationCompatibility,
    ReaderFeedCompatibility,
    ReaderDetailCompatibility,
    ReaderBodyCompatibility,
):
    """Reads verified visible Home / For You posts and at most one exact post."""

    @property
    def _readiness_timeout_ms(self) -> int:
        return _READINESS_TIMEOUT_MS

    @property
    def _navigation_timeout_ms(self) -> int:
        return _NAVIGATION_TIMEOUT_MS

    @property
    def _automatic_readiness_windows(self) -> int:
        return _AUTOMATIC_READINESS_WINDOWS

    @property
    def _readiness_recheck_interval_ms(self) -> int:
        return _READINESS_RECHECK_INTERVAL_MS

    @property
    def _card_hydration_wait_ms(self) -> int:
        return _CARD_HYDRATION_WAIT_MS

    @property
    def _body_stability_interval_ms(self) -> int:
        return _BODY_STABILITY_INTERVAL_MS

    @property
    def _render_min_dwell_ms(self) -> int:
        return _RENDER_MIN_DWELL_MS

    @property
    def _render_settle_interval_ms(self) -> int:
        return _RENDER_SETTLE_INTERVAL_MS

    @property
    def _render_stable_observations(self) -> int:
        return _RENDER_STABLE_OBSERVATIONS

    def __init__(
        self,
        page: Page,
        expected_actor: str | None,
        *,
        readiness_prompt: Callable[[str, int], Awaitable[bool]] | None = None,
        detail_mode: bool = False,
        console_output_policy: ConsoleOutputPolicy = ConsoleOutputPolicy.INTERACTIVE,
        auto_nurture: bool = False,
    ) -> None:
        if expected_actor is not None and _USERNAME.fullmatch(expected_actor) is None:
            raise UiReadError("ACTOR_INVALID")
        self._page = page
        self._scroll_session = FeedScrollSession(self)
        self._home_render = HomeRenderObserver(self)
        self._recovery = ReaderRecovery(self)
        self._home_navigation = HomeNavigator(
            self,
            expected_actor=(None if expected_actor is None else expected_actor.casefold()),
        )
        self._login_navigation = LoginNavigator(self)
        self._console_output_policy = console_output_policy
        self._loading_observer = LoadingObserver(
            page,
            console_output_policy=console_output_policy,
        )
        self._body_text = BodyTextReader(page)
        self._readiness_prompt = readiness_prompt
        self._detail_mode = detail_mode
        self._failure_phase = "INITIALIZING"
        self._failure_operation = "SETTLE"
        self._scan_viewport_index: int | None = None
        self.last_scan_retry_count = 0
        self._last_ui_transition_at: float | None = None
        self.feed_duplicates = 0
        self.last_scan_counts = _FeedScanCounts()
        self._feed_rows = FeedRowScanner(page)
        self._structure_collector = CandidateStructureCollector(self)
        self._scan_revalidator = ScanFailureRevalidator(self)
        self._card_provenance = CardProvenanceReader(
            self._feed_rows,
            self._body_text,
            self._loading_observer,
        )
        self._target_selector = VisiblePostTargetSelector(self)
        self._viewport_scan = ViewportScanCoordinator(
            self._page,
            self._feed_rows,
            self._card_provenance,
        )
        self._feed_scan = FeedScanSession(self, self._viewport_scan)
        self._root_discovery = FeedRootDiscoverer(self)
        self._feed_observer = HomeFeedObserver(self)
        self.last_candidate_diagnostics: tuple[_CandidateDiagnostic, ...] = ()
        self.last_display_observations: tuple[DisplayObservation, ...] = ()
        self.last_scan_timings = _FeedScanTimings()
        self.last_viewport_coverage_complete = True
        self.last_viewport_observed_rows = 0
        self.last_viewport_deferred_only = False
        self.last_viewport_can_wheel = False
        self.last_deferred_fingerprints: frozenset[str] = frozenset()
        self._last_viewport_limit = 20
        self._last_viewport_signature: tuple[object, ...] | None = None
        self._last_viewport_candidate_roots: tuple[Locator, ...] = ()
        self._last_viewport_candidate_identities: tuple[_TrackedRowIdentity, ...] = ()
        self._carried_viewport_candidate_identities: tuple[_TrackedRowIdentity, ...] = ()
        self._pending_viewport_candidate_identities: tuple[_TrackedRowIdentity, ...] = ()
        self._home_session = HomeSession(self, readiness_timeout_ms=_READINESS_TIMEOUT_MS)
        self._detail_navigator = DetailNavigator(
            self,
            timeout_ms=_DETAIL_TIMEOUT_MS,
            console_output_policy=console_output_policy,
            auto_nurture=auto_nurture,
        )
        self._reported_render_rescans: set[tuple[str, float | None]] = set()
        self._skipped_candidate_identities: set[str] = set()
        self._reported_candidate_skips: dict[tuple[str, str], set[str]] = {}
