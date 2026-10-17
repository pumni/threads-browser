from __future__ import annotations

import pytest

from threads_browser.nurture.observation import BodyRangeCoverageFacts, RowObservationFacts
from threads_browser.nurture.state import (
    CoverageLedger,
    DedupeLedger,
    NurtureRunState,
    WorkflowPhase,
)


def test_coverage_ledger_preserves_unique_and_unresolved_counts() -> None:
    ledger = CoverageLedger()
    ledger.observe_row(
        RowObservationFacts("post-one", "row-one", "gap-one", "PARTIAL_BODY", True, False)
    )
    ledger.observe_row(
        RowObservationFacts(None, "row-two", "gap-two", "POST_ROOT_UNRESOLVED", False, False)
    )
    ledger.observe_row(
        RowObservationFacts(None, None, "gap-three", "POST_ROOT_UNRESOLVED", False, False)
    )

    assert ledger.unresolved_gap_count == 3
    assert ledger.unique_deferred == {"post-one"}

    ledger.observe_row(
        RowObservationFacts("post-one", "row-one", "gap-one", "VERIFIED", True, True)
    )

    assert ledger.unresolved_gap_count == 2
    assert ledger.provenance_verified == {"post-one"}
    assert ledger.fully_read == {"post-one"}
    assert ledger.reliability_metrics(
        viewport_observations=4,
        visible_text_segments=2,
    ) == {
        "viewport_observations": 4,
        "unique_provenance_identities": 1,
        "unique_verified_posts": 1,
        "unique_deferred_posts": 1,
        "unresolved_gaps": 2,
        "read_in_progress": 0,
        "unresolved_observations": 1,
        "unrepresented_gaps": 1,
        "gap_sources_total": 2,
        "stranded_body_ranges": 0,
        "lookahead_rows": 0,
        "gap_category": "MULTIPLE",
        "visible_text_segments": 2,
    }


def test_coverage_diagnostics_report_sanitized_gap_sources_and_totals() -> None:
    ledger = CoverageLedger()
    ledger.observe_row(
        RowObservationFacts(
            "private-fingerprint",
            "private-observation",
            "private-gap",
            "PARTIAL_BODY",
            True,
            False,
        )
    )
    ledger.observe_row(
        RowObservationFacts(
            None,
            "private-observation-two",
            "private-gap-two",
            "POST_ROOT_UNRESOLVED",
            False,
            False,
        )
    )
    ledger.observe_row(
        RowObservationFacts(
            None,
            None,
            "private-gap-three",
            "POST_ROOT_UNRESOLVED",
            False,
            False,
        )
    )
    ledger.observe_body_range(
        "private-fingerprint",
        BodyRangeCoverageFacts(
            body_complete=False,
            missing_range_count=1,
            unrecoverable_range_count=1,
            unrecoverable_range_signature="private-signature",
        ),
    )

    metrics = ledger.reliability_metrics(
        viewport_observations=6,
        visible_text_segments=4,
        lookahead_rows=5,
    )

    assert metrics["unresolved_gaps"] == 3
    assert metrics["gap_sources_total"] == metrics["unresolved_gaps"]
    assert metrics["read_in_progress"] == 1
    assert metrics["unresolved_observations"] == 1
    assert metrics["unrepresented_gaps"] == 1
    assert metrics["stranded_body_ranges"] == 1
    assert metrics["lookahead_rows"] == 5
    assert metrics["gap_category"] == "MULTIPLE"
    assert not any(
        private_value in repr(metrics)
        for private_value in (
            "private-fingerprint",
            "private-observation",
            "private-gap",
            "private-signature",
        )
    )


@pytest.mark.parametrize(
    ("facts", "expected_category"),
    (
        (
            RowObservationFacts("private-fingerprint", "obs", "gap", "PARTIAL_BODY", True, False),
            "READ_IN_PROGRESS",
        ),
        (
            RowObservationFacts(None, "private-observation", "gap", "POST_ROOT_UNRESOLVED", False, False),
            "UNVERIFIED_OBSERVATION",
        ),
        (
            RowObservationFacts(None, None, "private-gap", "POST_ROOT_UNRESOLVED", False, False),
            "UNREPRESENTED_GAP",
        ),
    ),
)
def test_coverage_gap_category_identifies_single_source(
    facts: RowObservationFacts,
    expected_category: str,
) -> None:
    ledger = CoverageLedger()
    ledger.observe_row(facts)

    metrics = ledger.reliability_metrics(
        viewport_observations=1,
        visible_text_segments=0,
    )

    assert metrics["gap_category"] == expected_category
    assert metrics["gap_sources_total"] == metrics["unresolved_gaps"] == 1


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

    ledger.home_observed_this_session.add("new-post")
    ledger.new_fingerprints.add("new-post")
    ledger.detail_attempted_this_session.update(("read-post", "retry-post"))
    ledger.detail_read_this_session.add("read-post")

    assert ledger.checkpoint_seen == {"seen-post"}
    assert ledger.pending == ["pending-post"]
    assert ledger.pending_set == {"pending-post"}
    assert ledger.home_observed_this_session == {"new-post"}
    assert ledger.new_fingerprints == {"new-post"}
    assert ledger.detail_attempted_this_session == {"read-post", "retry-post"}
    assert ledger.detail_read_this_session == {"read-post"}
