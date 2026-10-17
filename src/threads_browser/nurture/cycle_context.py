"""Mutable collaborators and bounded counters for one nurture page cycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from playwright.async_api import Page

from ..console_output import ConsoleOutputPolicy
from ..feed.models import PostTarget
from ..storage import BrowserAccount, Checkpoint, CheckpointStore
from .state import CoverageLedger, DedupeLedger, NurtureRunState, WorkflowStats

if TYPE_CHECKING:
    from ..reader import ThreadsHomeReader
    from .feed_scroll import FeedScrollController
    from .selection import VerifiedTargetSelector
    from .session_state import SessionProgress


@dataclass(slots=True)
class CycleContext:
    """Own the run-local references and state shared by cycle phases."""

    page: Page
    account: BrowserAccount
    checkpoint_store: CheckpointStore
    checkpoint: Checkpoint
    policy_fingerprint: str
    normalized_terms: tuple[str, ...]
    state: NurtureRunState
    dedupe: DedupeLedger
    stats: WorkflowStats
    reader: ThreadsHomeReader
    coverage: CoverageLedger
    selector: VerifiedTargetSelector
    scroll_controller: FeedScrollController
    max_posts: int
    max_scrolls: int
    max_total_posts: int
    console_output_policy: ConsoleOutputPolicy
    detail: bool
    show_post_text: bool
    show_observed_text: bool
    previous_unverified_rows: list[tuple[str, float]] = field(default_factory=list)
    viewport_number: int = 0
    exhaustion_reason: str = "MAX_SCROLLS"
    selected: PostTarget | None = None
    selected_status: str | None = None
    detail_reads: int = 0
    detail_retry_pending: bool = False
    session_progress: SessionProgress | None = None
    session_checkpoint_committed: bool = False
    detail_attempted: bool = False
