from __future__ import annotations

from types import SimpleNamespace

import pytest

from threads_browser.nurture.engine import _result_from_observation
from threads_browser.nurture.state import CoverageLedger, DedupeLedger, WorkflowStats


@pytest.mark.parametrize(
    (
        "status",
        "stop_reason",
        "checkpoint_saved",
        "checkpoint_runs",
        "report_home_counts",
        "viewport_coverage_complete",
        "detail_reads",
    ),
    (
        (
            "VIEWPORT_OBSERVATION_INCOMPLETE",
            "VIEWPORT_OBSERVATION_INCOMPLETE",
            False,
            4,
            True,
            False,
            0,
        ),
        (
            "UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION",
            "UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION",
            False,
            4,
            True,
            True,
            0,
        ),
        ("NO_ACTIONABLE_TARGET", "NO_ACTIONABLE_TARGET", False, 4, True, True, 0),
        ("NO_VERIFIED_POSTS_IN_BUDGET", "NO_VERIFIED_POSTS_IN_BUDGET", False, 4, False, True, 0),
        ("FEED_END_REACHED", "FEED_END_REACHED", False, 4, True, True, 0),
        (
            "RELEVANT_POST_NOT_CURRENT",
            "RELEVANT_POST_NOT_CURRENT",
            False,
            4,
            True,
            True,
            0,
        ),
        ("COMPLETE_WITH_SKIPS", "FEED_END_REACHED", True, 5, True, True, 1),
    ),
    ids=(
        "partial-viewport",
        "unverified-relevance",
        "no-actionable-target",
        "no-verified-posts",
        "feed-end-pending",
        "relevant-post-not-current",
        "committed-cycle",
    ),
)
def test_result_snapshot_contract(
    status: str,
    stop_reason: str,
    checkpoint_saved: bool,
    checkpoint_runs: int,
    report_home_counts: bool,
    viewport_coverage_complete: bool,
    detail_reads: int,
) -> None:
    dedupe = DedupeLedger.from_checkpoint(
        seen_fingerprints=("previous-post",),
        pending_fingerprints=("pending-post",),
    )
    dedupe.seen_this_run.update(("current-one", "current-two"))
    dedupe.new_fingerprints.update(("current-one", "current-two"))
    stats = WorkflowStats(
        duplicates=3,
        relevant=1,
        irrelevant=2,
        unknown=3,
        observed_rows=7,
        body_extracted=5,
        provenance_verified=4,
        classified=6,
        observation_unknown=1,
        unverified_relevant=2,
        skipped_rows=1,
        visible_text_segments=9,
    )
    coverage = CoverageLedger(
        provenance_verified={"post-one", "post-two"},
        fully_read={"post-one"},
        unique_deferred={"post-two"},
        unresolved_observations={"gap-one"},
    )
    reader = SimpleNamespace(scroll_attempts=4, scrolls=3, skipped_unverified=5)

    result = _result_from_observation(
        status=status,
        stop_reason=stop_reason,
        dedupe=dedupe,
        stats=stats,
        reader=reader,
        coverage=coverage,
        checkpoint_runs=checkpoint_runs,
        detail_reads=detail_reads,
        report_home_counts=report_home_counts,
        viewport_coverage_complete=viewport_coverage_complete,
        checkpoint_saved=checkpoint_saved,
    )

    assert result.status == status
    assert result.stop_reason == stop_reason
    assert result.checkpoint_saved is checkpoint_saved
    assert result.checkpoint_runs == checkpoint_runs
    assert result.detail_reads == detail_reads
    assert result.home_sampled == (2 if report_home_counts else 0)
    assert result.home_new == (2 if report_home_counts else 0)
    assert result.home_relevant == (1 if report_home_counts else 0)
    assert result.home_irrelevant == (2 if report_home_counts else 0)
    assert result.home_unknown == (3 if report_home_counts else 0)
    assert result.home_relevant_ratio == (0.5 if report_home_counts else None)
    assert result.duplicates == 3
    assert result.scroll_attempts == 4
    assert result.scrolls == 3
    assert result.skipped_unverified == 5
    assert result.observed_rows == result.viewport_observations == 7
    assert result.body_extracted == 5
    assert result.provenance_verified == 4
    assert result.classified == 6
    assert result.observation_unknown == 1
    assert result.unverified_relevant == 2
    assert result.skipped_rows == 1
    assert result.viewport_coverage_complete is viewport_coverage_complete
    assert result.unique_provenance_identities == 2
    assert result.unique_verified_posts == 1
    assert result.unique_deferred_posts == 1
    assert result.unresolved_gaps == 1
    assert result.visible_text_segments == 9
