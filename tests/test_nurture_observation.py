from __future__ import annotations

import pytest

from threads_browser.nurture.observation import (
    BodyRangeCoverageFacts,
    RowCoverageState,
    RowObservationFacts,
    observe_row,
)
from threads_browser.nurture.state import CoverageLedger


def _facts(
    state: str,
    *,
    fingerprint: str | None = "post-a",
    observation_id: str | None = "row-a",
    has_body: bool = False,
    body_complete: bool = False,
) -> RowObservationFacts:
    return RowObservationFacts(
        fingerprint=fingerprint,
        observation_id=observation_id,
        gap_id="viewport-row",
        state=state,
        has_body=has_body,
        body_complete=body_complete,
    )


def test_observation_reducer_is_pure_and_partial_then_complete_is_monotonic() -> None:
    previous = RowCoverageState()
    partial = _facts("PARTIAL_BODY", has_body=True)

    first = observe_row(partial, previous)
    second = observe_row(partial, first.coverage)
    complete = observe_row(
        _facts("VERIFIED", observation_id="replacement-row", has_body=True, body_complete=True),
        second.coverage,
    )
    repeated_complete = observe_row(
        _facts("VERIFIED", observation_id="replacement-row", has_body=True, body_complete=True),
        complete.coverage,
    )

    assert previous == RowCoverageState()
    assert first.coverage == RowCoverageState(True, True, False, False)
    assert first.identity_added and first.read_started
    assert second.coverage == first.coverage
    assert not second.identity_added and not second.read_started
    assert complete.coverage == RowCoverageState(True, False, True, False)
    assert complete.body_completed
    assert repeated_complete.coverage == complete.coverage
    assert not repeated_complete.body_completed


@pytest.mark.parametrize("state", ["DEFERRED_OFFSCREEN", "DEFERRED_TEXT_BELOW"])
def test_first_below_viewport_candidate_is_identity_only(state: str) -> None:
    outcome = observe_row(_facts(state), RowCoverageState())

    assert outcome.coverage == RowCoverageState(identity_recognized=True)
    assert outcome.identity_added
    assert not outcome.read_started


def test_same_post_observed_twice_keeps_coverage_counters_stable() -> None:
    ledger = CoverageLedger()

    ledger.observe_row(_facts("PARTIAL_BODY", has_body=True))
    first_counts = ledger.reliability_metrics(viewport_observations=1, visible_text_segments=1)
    ledger.observe_row(
        _facts(
            "PARTIAL_BODY",
            observation_id="replacement-row",
            has_body=True,
        )
    )
    second_counts = ledger.reliability_metrics(viewport_observations=2, visible_text_segments=1)

    assert first_counts | {"viewport_observations": 2} == second_counts
    assert ledger.provenance_verified == {"post-a"}
    assert ledger.read_in_progress == {"post-a"}
    assert ledger.fully_read == set()
    assert ledger.unresolved_gap_count == 1

    ledger.observe_row(
        _facts(
            "VERIFIED",
            observation_id="replacement-row",
            has_body=True,
            body_complete=True,
        )
    )
    assert ledger.read_in_progress == set()
    assert ledger.fully_read == {"post-a"}
    assert ledger.unresolved_gap_count == 0


def test_complete_observation_resolves_only_its_own_post_gap() -> None:
    ledger = CoverageLedger()
    ledger.observe_row(_facts("PARTIAL_BODY", has_body=True))
    ledger.observe_row(
        _facts(
            "PARTIAL_BODY",
            fingerprint="post-b",
            observation_id="row-b",
            has_body=True,
        )
    )

    ledger.observe_row(
        _facts("VERIFIED", has_body=True, body_complete=True)
    )

    assert ledger.read_in_progress == {"post-b"}
    assert ledger.fully_read == {"post-a"}
    assert ledger.unresolved_gap_count == 1


def test_stranded_body_range_stays_with_its_post_until_that_post_completes() -> None:
    ledger = CoverageLedger()
    ledger.observe_row(_facts("PARTIAL_BODY", has_body=True))
    assert ledger.observe_body_range(
        "post-a",
        BodyRangeCoverageFacts(
            body_complete=False,
            missing_range_count=1,
            unrecoverable_range_count=1,
            unrecoverable_range_signature="synthetic-private-token",
        ),
    )

    ledger.observe_row(
        _facts(
            "VERIFIED",
            fingerprint="post-b",
            observation_id="row-b",
            has_body=True,
            body_complete=True,
        )
    )
    assert not ledger.observe_body_range(
        "post-b",
        BodyRangeCoverageFacts(
            body_complete=True,
            missing_range_count=0,
            unrecoverable_range_count=0,
        ),
    )

    assert ledger.read_in_progress == {"post-a"}
    assert ledger.stranded_body_ranges == {"post-a"}
    assert ledger.body_range_signatures == {
        "post-a": "synthetic-private-token"
    }
    assert "synthetic-private-token" not in repr(ledger)
    assert ledger.unresolved_gap_count == 1

    ledger.observe_row(_facts("VERIFIED", has_body=True, body_complete=True))
    assert not ledger.observe_body_range(
        "post-a",
        BodyRangeCoverageFacts(
            body_complete=True,
            missing_range_count=0,
            unrecoverable_range_count=0,
        ),
    )
    assert ledger.read_in_progress == set()
    assert ledger.stranded_body_ranges == set()
    assert ledger.body_range_signatures == {}
    assert ledger.unresolved_gap_count == 0


def test_below_viewport_reobservation_preserves_only_an_existing_gap() -> None:
    ledger = CoverageLedger()
    ledger.observe_row(_facts("PARTIAL_BODY", has_body=True))

    ledger.observe_row(_facts("DEFERRED_TEXT_BELOW", has_body=False))

    assert ledger.provenance_verified == {"post-a"}
    assert ledger.read_in_progress == {"post-a"}
    assert ledger.unresolved_gap_count == 1


def test_new_run_starts_with_no_inherited_body_progress() -> None:
    current_run = CoverageLedger()
    current_run.observe_row(_facts("PARTIAL_BODY", has_body=True))
    resumed_run = CoverageLedger()

    assert resumed_run.provenance_verified == set()
    assert resumed_run.read_in_progress == set()
    assert resumed_run.fully_read == set()
