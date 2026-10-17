"""Private data records for one bounded viewport scan."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from playwright.async_api import JSHandle, Locator

from ..feed.models import DisplayObservation, PostTarget
from .body_text import _VisibleBodyRead
from .feed_rows import (
    UnverifiedFeedCandidate as _UnverifiedFeedCandidate,
)
from .feed_rows import (
    _CandidateDiagnostic,
    _CandidateStructure,
    _FeedScanCounts,
    _FeedScanTimings,
)
from .loading import CardWaitSignal as _CardWaitSignal


@dataclass(frozen=True, slots=True)
class _HomeRowObservation:
    diagnostic: _CandidateDiagnostic
    target: PostTarget | None = None
    display: DisplayObservation | None = None
    fingerprint: str | None = None
    candidate: bool = True


@dataclass(frozen=True, slots=True, repr=False)
class _TrackedRowIdentity:
    """Keep transient DOM identity, observation ID, and post fingerprint distinct."""

    dom_handle: JSHandle = field(repr=False)
    observation_id: str = field(repr=False)
    fingerprint: str | None = field(default=None, repr=False)
    author_fingerprint: str | None = field(default=None, repr=False)
    body_verified: bool = False

    def __repr__(self) -> str:
        return "TrackedRowIdentity(<private>)"


@dataclass(frozen=True, slots=True, repr=False)
class _ScanFailureSite:
    stage: str
    viewport_index: int | None
    candidate_index: int | None
    operation: str
    root_handle: JSHandle = field(repr=False)
    canonical_identity: str | None = field(repr=False)
    identity_source: str
    author_fingerprint: str | None = field(repr=False)
    carried_identity: _TrackedRowIdentity | None = field(repr=False)
    identity_conflict: bool
    box: tuple[float, float, float, float] = field(repr=False)
    viewport_size: tuple[int, int]
    scroll_position: tuple[float, float, float] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _VisibleTargets:
    targets: tuple[PostTarget, ...]
    scroll_anchor: dict[str, float] | None
    display_observations: tuple[DisplayObservation, ...]
    observed_rows: int
    coverage_complete: bool
    signature: tuple[object, ...] = field(repr=False)
    candidate_roots: tuple[Locator, ...] = field(default=(), repr=False)
    pending_signals: tuple[_CardWaitSignal, ...] = field(default=(), repr=False)
    candidate_observation_ids: tuple[str, ...] = field(default=(), repr=False)
    candidate_identities: tuple[_TrackedRowIdentity, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class _ViewportScanRequest:
    max_posts: int
    currently_visible: bool
    prefer_new_fingerprints: set[str] | None
    stage: str
    viewport_index: int | None
    on_row: Callable[[_HomeRowObservation], Awaitable[None]] | None = None


@dataclass(frozen=True, slots=True)
class _ViewportScanHooks:
    """Reader compatibility operations needed during a scan, without reader state."""

    discover_roots: Callable[
        [Locator, dict[str, int]],
        Awaitable[tuple[list[Locator], bool, tuple[_TrackedRowIdentity, ...]]],
    ]
    collect_structures: Callable[
        [Locator, list[Locator], dict[str, int]],
        Awaitable[dict[int, _CandidateStructure]],
    ]
    scan_permalink_fallback: Callable[..., Awaitable[tuple[tuple[PostTarget, ...], dict[str, float] | None]]]
    read_card: Callable[..., Awaitable[PostTarget]]
    recover_ambiguous_visible_card: Callable[..., Awaitable[tuple[PostTarget, bool]]]
    read_visible_body_progress: Callable[..., Awaitable[_VisibleBodyRead]]
    wait_for_pending_card: Callable[..., Awaitable[bool]]
    record_scan_skip: Callable[[str, _UnverifiedFeedCandidate], None]
    publish_diagnostics: Callable[[tuple[_CandidateDiagnostic, ...]], None]
    publish_display_observations: Callable[[tuple[DisplayObservation, ...]], None]
    publish_can_wheel: Callable[[bool], None]
    increment_feed_duplicates: Callable[[], None]
    body_stability_interval_ms: int
    card_hydration_wait_ms: int


_CandidateRow = tuple[
    int,
    Locator,
    _CandidateStructure,
    _TrackedRowIdentity | None,
    _CandidateDiagnostic,
    str,
]


@dataclass(slots=True)
class _ViewportScanState:
    """Mutable data whose lifetime is one viewport scan and its retry handoff."""

    request: _ViewportScanRequest
    hooks: _ViewportScanHooks
    counts: _FeedScanCounts
    stage: str | None = None
    carried_identities: tuple[_TrackedRowIdentity, ...] = ()
    timings: _FeedScanTimings = field(default_factory=_FeedScanTimings)
    operation: str = "DISCOVER_ROOTS"
    candidate_index: int | None = None
    failure_site: _ScanFailureSite | None = None
    candidate_rows: list[_CandidateRow] = field(default_factory=list)
    candidate_dom_handles: dict[int, JSHandle] = field(default_factory=dict)
    candidate_prior_fingerprints: dict[int, str | None] = field(default_factory=dict)
    matched_previous_identity_handles: set[int] = field(default_factory=set)
    diagnostics: list[_CandidateDiagnostic] = field(default_factory=list)
    published_diagnostics: tuple[_CandidateDiagnostic, ...] = ()
    targets: list[PostTarget] = field(default_factory=list)
    scroll_anchor: dict[str, float] | None = None
    display_observations: list[DisplayObservation] = field(default_factory=list)
    pending_signals: list[_CardWaitSignal] = field(default_factory=list)
    scanned_fingerprints: set[str] = field(default_factory=set)
    candidate_by_index: dict[
        int,
        tuple[
            Locator,
            _CandidateStructure,
            _TrackedRowIdentity | None,
            _CandidateDiagnostic,
            str,
        ],
    ] = field(default_factory=dict)
    processed_candidates: int = 0
