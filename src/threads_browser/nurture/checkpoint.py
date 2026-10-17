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
    ) -> Checkpoint:
        if len(dedupe.pending) > _MAX_SEEN:
            raise BrowserDataError("STATE_INVALID")
        seen = list(dict.fromkeys((*previous.seen_fingerprints, *dedupe.seen_this_run)))
        if len(seen) > _MAX_SEEN:
            retained_pending = [value for value in seen if value in dedupe.pending_set]
            recent = [value for value in seen if value not in dedupe.pending_set]
            available = _MAX_SEEN - len(retained_pending)
            seen = retained_pending + (recent[-available:] if available else [])
        updated = Checkpoint(
            self._account_id,
            self._policy_fingerprint,
            completed_runs=previous.completed_runs + 1,
            home_sampled=previous.home_sampled + len(dedupe.seen_this_run),
            home_new=previous.home_new + len(dedupe.new_fingerprints),
            home_relevant=previous.home_relevant + stats.relevant,
            home_irrelevant=previous.home_irrelevant + stats.irrelevant,
            home_unknown=previous.home_unknown + stats.unknown,
            duplicates=previous.duplicates + stats.duplicates,
            detail_reads=previous.detail_reads + detail_reads,
            scroll_attempts=previous.scroll_attempts + reader.scroll_attempts,
            scrolls=previous.scrolls + reader.scrolls,
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
