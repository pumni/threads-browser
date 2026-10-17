"""Privacy-safe result snapshots from one bounded observation cycle."""

from __future__ import annotations

from dataclasses import dataclass

from ..reader import ThreadsHomeReader
from .state import CoverageLedger, DedupeLedger, WorkflowStats


@dataclass(frozen=True, slots=True)
class NurtureResult:
    status: str
    home_sampled: int
    home_new: int
    home_relevant: int
    home_irrelevant: int
    home_unknown: int
    duplicates: int
    detail_reads: int
    scroll_attempts: int
    scrolls: int
    checkpoint_runs: int
    home_relevant_ratio: float | None
    skipped_unverified: int = 0
    ui_status: str = "UNVERIFIED_LIVE_UI"
    observed_rows: int = 0
    body_extracted: int = 0
    provenance_verified: int = 0
    classified: int = 0
    observation_unknown: int = 0
    unverified_relevant: int = 0
    skipped_rows: int = 0
    viewport_coverage_complete: bool = True
    viewport_observations: int = 0
    unique_provenance_identities: int = 0
    unique_verified_posts: int = 0
    unique_deferred_posts: int = 0
    unresolved_gaps: int = 0
    visible_text_segments: int = 0
    stop_reason: str = "UNKNOWN"
    checkpoint_saved: bool = False
    read_in_progress: int = 0
    unresolved_observations: int = 0
    unrepresented_gaps: int = 0
    gap_sources_total: int = 0
    stranded_body_ranges: int = 0
    lookahead_rows: int = 0
    gap_category: str = "NONE"


def _result_from_observation(
    *,
    status: str,
    stop_reason: str,
    dedupe: DedupeLedger,
    stats: WorkflowStats,
    reader: ThreadsHomeReader,
    coverage: CoverageLedger,
    checkpoint_runs: int,
    detail_reads: int = 0,
    report_home_counts: bool = True,
    viewport_coverage_complete: bool | None = None,
    checkpoint_saved: bool = False,
) -> NurtureResult:
    """Snapshot read-only observations separately from cycle/checkpoint disposition.

    Callers may set ``checkpoint_saved`` only after the checkpoint store commits.
    An existing ``checkpoint_runs`` value alone never marks this attempt saved.
    """
    home_sampled = (
        len(dedupe.home_observed_this_session) if report_home_counts else 0
    )
    home_new = len(dedupe.new_fingerprints) if report_home_counts else 0
    return NurtureResult(
        status=status,
        stop_reason=stop_reason,
        home_sampled=home_sampled,
        home_new=home_new,
        home_relevant=stats.relevant if report_home_counts else 0,
        home_irrelevant=stats.irrelevant if report_home_counts else 0,
        home_unknown=stats.unknown if report_home_counts else 0,
        duplicates=stats.duplicates,
        detail_reads=detail_reads,
        scroll_attempts=reader.scroll_attempts,
        scrolls=reader.scrolls,
        checkpoint_runs=checkpoint_runs,
        home_relevant_ratio=(
            None if not home_sampled else stats.relevant / home_sampled
        ),
        skipped_unverified=reader.skipped_unverified,
        observed_rows=stats.observed_rows,
        body_extracted=stats.body_extracted,
        provenance_verified=stats.provenance_verified,
        classified=stats.classified,
        observation_unknown=stats.observation_unknown,
        unverified_relevant=stats.unverified_relevant,
        skipped_rows=stats.skipped_rows,
        viewport_coverage_complete=(
            stats.viewport_coverage_complete
            if viewport_coverage_complete is None
            else viewport_coverage_complete
        ),
        **coverage.reliability_metrics(
            viewport_observations=stats.observed_rows,
            visible_text_segments=stats.visible_text_segments,
            lookahead_rows=stats.lookahead_rows,
        ),
        checkpoint_saved=checkpoint_saved,
    )
