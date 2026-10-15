from __future__ import annotations

import pytest

from threads_browser.nurture.state import (
    CoverageLedger,
    DedupeLedger,
    NurtureRunState,
    WorkflowPhase,
)


def test_coverage_ledger_preserves_unique_and_unresolved_counts() -> None:
    ledger = CoverageLedger()
    ledger.mark_deferred("post-one", "row-one", "gap-one")
    ledger.mark_deferred(None, "row-two", "gap-two")
    ledger.mark_deferred(None, None, "gap-three")

    assert ledger.unresolved_gap_count == 3
    assert ledger.unique_deferred == {"post-one"}

    ledger.mark_verified("post-one", "row-one")

    assert ledger.unresolved_gap_count == 2
    assert ledger.provenance_verified == {"post-one"}
    assert ledger.reliability_metrics(
        viewport_observations=4,
        visible_text_segments=2,
    ) == {
        "viewport_observations": 4,
        "unique_provenance_identities": 1,
        "unique_verified_posts": 0,
        "unique_deferred_posts": 1,
        "unresolved_gaps": 2,
        "visible_text_segments": 2,
    }


def test_nurture_state_accepts_observe_scroll_detail_back_cycle() -> None:
    state = NurtureRunState()
    state.transition(WorkflowPhase.OBSERVING)
    state.transition(WorkflowPhase.WAITING_FOR_CONTENT)
    state.transition(WorkflowPhase.VERIFYING)
    state.transition(WorkflowPhase.OBSERVING)
    state.transition(WorkflowPhase.SCROLLING)
    state.transition(WorkflowPhase.WAITING_FOR_CONTENT)
    state.transition(WorkflowPhase.VERIFYING)
    state.transition(WorkflowPhase.SELECTING)
    state.transition(WorkflowPhase.DETAIL)
    state.transition(WorkflowPhase.BACK)
    state.transition(WorkflowPhase.CHECKPOINTING)
    state.stop("COMPLETE")

    assert state.phase is WorkflowPhase.STOPPED
    assert state.stop_reason == "COMPLETE"


def test_nurture_state_rejects_invalid_transition() -> None:
    state = NurtureRunState()

    with pytest.raises(ValueError, match="invalid nurture transition"):
        state.transition(WorkflowPhase.DETAIL)


def test_dedupe_ledger_keeps_checkpoint_and_current_run_sets_separate() -> None:
    ledger = DedupeLedger.from_checkpoint(
        seen_fingerprints=("seen-post",),
        pending_fingerprints=("pending-post",),
    )

    ledger.seen_this_run.add("new-post")
    ledger.new_fingerprints.add("new-post")

    assert ledger.previously_seen == {"seen-post"}
    assert ledger.pending == ["pending-post"]
    assert ledger.pending_set == {"pending-post"}
    assert ledger.seen_this_run == {"new-post"}
    assert ledger.new_fingerprints == {"new-post"}
