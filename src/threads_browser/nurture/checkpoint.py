"""Build and persist one cycle's bounded checkpoint update."""

from __future__ import annotations

from ..reader import ThreadsHomeReader
from ..storage import BrowserDataError, Checkpoint, CheckpointStore
from .state import DedupeLedger, NurtureRunState, WorkflowPhase, WorkflowStats

_MAX_SEEN = 5_000


class CheckpointCommitter:
    """Own checkpoint retention, counters and the verified commit boundary."""

    def __init__(
        self,
        store: CheckpointStore,
        *,
        account_id: str,
        policy_fingerprint: str,
    ) -> None:
        self._store = store
        self._account_id = account_id
        self._policy_fingerprint = policy_fingerprint

    def commit(
        self,
        previous: Checkpoint,
        *,
        dedupe: DedupeLedger,
        stats: WorkflowStats,
        reader: ThreadsHomeReader,
        state: NurtureRunState,
        detail_reads: int,
        observed_fingerprints: set[str] | None = None,
        new_fingerprints: set[str] | None = None,
        scroll_attempts: int | None = None,
        scrolls: int | None = None,
        completed_runs_delta: int = 1,
    ) -> Checkpoint:
        if len(dedupe.pending) > _MAX_SEEN:
            raise BrowserDataError("STATE_INVALID")
        seen = list(dict.fromkeys((*previous.seen_fingerprints, *dedupe.seen_this_run)))
        if len(seen) > _MAX_SEEN:
            retained_pending = [value for value in seen if value in dedupe.pending_set]
            recent = [value for value in seen if value not in dedupe.pending_set]
            available = _MAX_SEEN - len(retained_pending)
            seen = retained_pending + (recent[-available:] if available else [])
        observed = (
            dedupe.seen_this_run if observed_fingerprints is None else observed_fingerprints
        )
        newly_seen = (
            dedupe.new_fingerprints if new_fingerprints is None else new_fingerprints
        )
        updated = Checkpoint(
            self._account_id,
            self._policy_fingerprint,
            completed_runs=previous.completed_runs + completed_runs_delta,
            home_sampled=previous.home_sampled + len(observed),
            home_new=previous.home_new + len(newly_seen),
            home_relevant=previous.home_relevant + stats.relevant,
            home_irrelevant=previous.home_irrelevant + stats.irrelevant,
            home_unknown=previous.home_unknown + stats.unknown,
            duplicates=previous.duplicates + stats.duplicates,
            detail_reads=previous.detail_reads + detail_reads,
            scroll_attempts=previous.scroll_attempts
            + (reader.scroll_attempts if scroll_attempts is None else scroll_attempts),
            scrolls=previous.scrolls + (reader.scrolls if scrolls is None else scrolls),
            pending_read_fingerprints=tuple(dedupe.pending),
            seen_fingerprints=tuple(seen),
        )
        if state.phase is not WorkflowPhase.CHECKPOINTING:
            state.transition(WorkflowPhase.CHECKPOINTING)
        try:
            self._store.save(updated)
        except BrowserDataError as error:
            state.stop(error.code)
            raise
        return updated
