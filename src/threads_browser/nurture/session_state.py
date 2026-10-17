"""Run-local ledgers and safe checkpoint deltas for one multi-observation session."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..reader import ThreadsHomeReader
from ..storage import Checkpoint, CheckpointStore
from .checkpoint import CheckpointCommitter
from .results import NurtureResult, _result_from_observation
from .state import CoverageLedger, DedupeLedger, NurtureRunState, WorkflowStats


@dataclass(slots=True)
class SessionProgress:
    """Carry verified feed state and checkpoint deltas across Detail/Back cycles."""

    checkpoint: Checkpoint
    checkpoint_store: CheckpointStore
    account_id: str
    policy_fingerprint: str
    dedupe: DedupeLedger
    coverage: CoverageLedger = field(default_factory=CoverageLedger)
    totals: WorkflowStats = field(default_factory=WorkflowStats)
    uncommitted_stats: WorkflowStats = field(default_factory=WorkflowStats)
    detail_attempts: int = 0
    detail_reads: int = 0
    uncommitted_detail_reads: int = 0
    viewport_number: int = 0
    previous_unverified_rows: list[tuple[str, float]] = field(default_factory=list)
    committed_fingerprints: set[str] = field(default_factory=set)
    committed_new_fingerprints: set[str] = field(default_factory=set)
    committed_scroll_attempts: int = 0
    committed_scrolls: int = 0
    checkpoint_saved: bool = False

    def record_cycle(
        self,
        stats: WorkflowStats,
        *,
        detail_attempted: bool,
        detail_reads: int,
        viewport_number: int,
        previous_unverified_rows: list[tuple[str, float]],
    ) -> None:
        self.totals.accumulate(stats)
        self.uncommitted_stats.accumulate(stats)
        self.detail_attempts += int(detail_attempted)
        self.detail_reads += detail_reads
        self.uncommitted_detail_reads += detail_reads
        self.viewport_number = viewport_number
        self.previous_unverified_rows = list(previous_unverified_rows)

    def has_safe_boundary(self, stats: WorkflowStats) -> bool:
        return (
            not self.coverage.has_skips
            and not self.coverage.has_unresolved_gaps
            and stats.viewport_coverage_complete
        )

    def commit(
        self,
        *,
        stats: WorkflowStats,
        reader: ThreadsHomeReader,
        state: NurtureRunState,
        detail_reads: int,
        completed_runs_delta: int | None = None,
    ) -> Checkpoint:
        if not self.has_safe_boundary(stats):
            raise ValueError("cannot commit an incomplete session observation")
        observed = self.dedupe.seen_this_run - self.committed_fingerprints
        newly_seen = self.dedupe.new_fingerprints - self.committed_new_fingerprints
        scroll_attempts = reader.scroll_attempts - self.committed_scroll_attempts
        scrolls = reader.scrolls - self.committed_scrolls
        if scroll_attempts < 0 or scrolls < 0:
            raise ValueError("session scroll counters moved backwards")
        if completed_runs_delta is None:
            completed_runs_delta = 0 if self.checkpoint_saved else 1
        updated = CheckpointCommitter(
            self.checkpoint_store,
            account_id=self.account_id,
            policy_fingerprint=self.policy_fingerprint,
        ).commit(
            self.checkpoint,
            dedupe=self.dedupe,
            stats=self.uncommitted_stats,
            reader=reader,
            state=state,
            detail_reads=self.uncommitted_detail_reads,
            observed_fingerprints=observed,
            new_fingerprints=newly_seen,
            scroll_attempts=scroll_attempts,
            scrolls=scrolls,
            completed_runs_delta=completed_runs_delta,
        )
        self.checkpoint = updated
        self.committed_fingerprints.update(self.dedupe.seen_this_run)
        self.committed_new_fingerprints.update(self.dedupe.new_fingerprints)
        self.committed_scroll_attempts = reader.scroll_attempts
        self.committed_scrolls = reader.scrolls
        self.uncommitted_stats = WorkflowStats()
        self.uncommitted_detail_reads = 0
        self.checkpoint_saved = True
        return updated

    def result(
        self,
        reader: ThreadsHomeReader,
        *,
        status: str,
        stop_reason: str,
        checkpoint_saved: bool | None = None,
    ) -> NurtureResult:
        coverage_complete = not self.coverage.has_skips and not self.coverage.has_unresolved_gaps
        return _result_from_observation(
            status=status,
            stop_reason=stop_reason,
            dedupe=self.dedupe,
            stats=self.totals,
            reader=reader,
            coverage=self.coverage,
            detail_reads=self.detail_reads,
            checkpoint_runs=self.checkpoint.completed_runs,
            viewport_coverage_complete=coverage_complete,
            checkpoint_saved=(
                self.checkpoint_saved if checkpoint_saved is None else checkpoint_saved
            ),
        )
