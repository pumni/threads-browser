"""Finite workflow phases and per-run observation coverage state."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256

from .observation import (
    BodyRangeCoverageFacts,
    BodyRangeCoverageState,
    RowCoverageState,
    RowObservationFacts,
    RowObservationOutcome,
)
from .observation import (
    observe_body_range as reduce_body_range_observation,
)
from .observation import (
    observe_row as reduce_row_observation,
)


class WorkflowPhase(StrEnum):
    INITIALIZING = "INITIALIZING"
    OBSERVING = "OBSERVING"
    SCROLLING = "SCROLLING"
    WAITING_FOR_CONTENT = "WAITING_FOR_CONTENT"
    VERIFYING = "VERIFYING"
    SELECTING = "SELECTING"
    DETAIL = "DETAIL"
    BACK = "BACK"
    CHECKPOINTING = "CHECKPOINTING"
    STOPPED = "STOPPED"


class InvalidWorkflowTransition(ValueError):
    def __init__(self, phase: WorkflowPhase, requested_phase: WorkflowPhase) -> None:
        self.phase = phase
        self.requested_phase = requested_phase
        super().__init__(f"invalid nurture transition: {phase} -> {requested_phase}")


_ALLOWED_TRANSITIONS: dict[WorkflowPhase, frozenset[WorkflowPhase]] = {
    WorkflowPhase.INITIALIZING: frozenset({WorkflowPhase.OBSERVING, WorkflowPhase.STOPPED}),
    WorkflowPhase.OBSERVING: frozenset(
        {
            WorkflowPhase.SCROLLING,
            WorkflowPhase.WAITING_FOR_CONTENT,
            WorkflowPhase.VERIFYING,
            WorkflowPhase.SELECTING,
            WorkflowPhase.CHECKPOINTING,
            WorkflowPhase.STOPPED,
        }
    ),
    WorkflowPhase.SCROLLING: frozenset(
        {
            WorkflowPhase.WAITING_FOR_CONTENT,
            WorkflowPhase.OBSERVING,
            WorkflowPhase.STOPPED,
        }
    ),
    WorkflowPhase.WAITING_FOR_CONTENT: frozenset(
        {
            WorkflowPhase.VERIFYING,
            WorkflowPhase.OBSERVING,
            WorkflowPhase.STOPPED,
        }
    ),
    WorkflowPhase.VERIFYING: frozenset(
        {
            WorkflowPhase.OBSERVING,
            WorkflowPhase.SCROLLING,
            WorkflowPhase.SELECTING,
            WorkflowPhase.CHECKPOINTING,
            WorkflowPhase.STOPPED,
        }
    ),
    WorkflowPhase.SELECTING: frozenset(
        {
            WorkflowPhase.DETAIL,
            WorkflowPhase.CHECKPOINTING,
            WorkflowPhase.STOPPED,
        }
    ),
    WorkflowPhase.DETAIL: frozenset({WorkflowPhase.BACK, WorkflowPhase.STOPPED}),
    WorkflowPhase.BACK: frozenset({WorkflowPhase.CHECKPOINTING, WorkflowPhase.STOPPED}),
    WorkflowPhase.CHECKPOINTING: frozenset({WorkflowPhase.STOPPED}),
    WorkflowPhase.STOPPED: frozenset(),
}


def policy_fingerprint(terms: tuple[str, ...]) -> str:
    payload = json.dumps(tuple(sorted(terms)), ensure_ascii=False, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class NurtureRunState:
    phase: WorkflowPhase = WorkflowPhase.INITIALIZING
    stop_reason: str | None = None

    def transition(self, phase: WorkflowPhase) -> None:
        if phase not in _ALLOWED_TRANSITIONS[self.phase]:
            raise InvalidWorkflowTransition(self.phase, phase)
        self.phase = phase

    def stop(self, reason: str) -> None:
        if self.phase is not WorkflowPhase.STOPPED:
            self.transition(WorkflowPhase.STOPPED)
        self.stop_reason = reason


@dataclass(slots=True)
class CoverageLedger:
    provenance_verified: set[str] = field(default_factory=set)
    fully_read: set[str] = field(default_factory=set)
    read_in_progress: set[str] = field(default_factory=set)
    unique_deferred: set[str] = field(default_factory=set)
    unresolved_observations: set[str] = field(default_factory=set)
    skipped_partial: set[str] = field(default_factory=set)
    unrepresented_gaps: set[str] = field(default_factory=set)
    hydration_pending: set[str] = field(default_factory=set)
    observed_body_identities: set[str] = field(default_factory=set)
    stranded_body_ranges: set[str] = field(default_factory=set)
    body_range_signatures: dict[str, str] = field(default_factory=dict, repr=False)

    def observe_row(self, facts: RowObservationFacts) -> RowObservationOutcome:
        fingerprint = facts.fingerprint
        observation_id = facts.observation_id
        previous = RowCoverageState(
            identity_recognized=(
                fingerprint is not None and fingerprint in self.provenance_verified
            ),
            read_in_progress=(fingerprint is not None and fingerprint in self.read_in_progress),
            body_complete=(fingerprint is not None and fingerprint in self.fully_read),
            unresolved_observation=(
                observation_id is not None and observation_id in self.unresolved_observations
            ),
        )
        outcome = reduce_row_observation(facts, previous)
        next_state = outcome.coverage

        if fingerprint is not None:
            if next_state.identity_recognized:
                self.provenance_verified.add(fingerprint)
            if next_state.read_in_progress:
                self.read_in_progress.add(fingerprint)
                self.unique_deferred.add(fingerprint)
            else:
                self.read_in_progress.discard(fingerprint)
            if next_state.body_complete:
                self.fully_read.add(fingerprint)
                self.skipped_partial.discard(fingerprint)
                self.stranded_body_ranges.discard(fingerprint)
                self.body_range_signatures.pop(fingerprint, None)
            elif next_state.read_in_progress:
                self.skipped_partial.add(fingerprint)

        if observation_id is not None:
            if next_state.unresolved_observation:
                self.unresolved_observations.add(observation_id)
                self.skipped_partial.add(observation_id)
            else:
                self.unresolved_observations.discard(observation_id)
                self.skipped_partial.discard(observation_id)
        elif next_state.unresolved_observation:
            self.unrepresented_gaps.add(facts.gap_id)
            self.skipped_partial.add(facts.gap_id)

        hydration_id = observation_id or facts.gap_id
        if outcome.hydration_pending is True:
            self.hydration_pending.add(hydration_id)
        elif outcome.hydration_pending is False:
            self.hydration_pending.discard(hydration_id)

        if outcome.body_observed:
            self.observed_body_identities.add(fingerprint or observation_id or facts.gap_id)
        return outcome

    def observe_body_range(
        self,
        fingerprint: str,
        facts: BodyRangeCoverageFacts,
    ) -> bool:
        previous = BodyRangeCoverageState(
            stranded=fingerprint in self.stranded_body_ranges,
            signature=self.body_range_signatures.get(fingerprint, ""),
        )
        outcome = reduce_body_range_observation(facts, previous)
        if outcome.coverage.stranded:
            self.stranded_body_ranges.add(fingerprint)
            if outcome.coverage.signature:
                self.body_range_signatures[fingerprint] = outcome.coverage.signature
        else:
            self.stranded_body_ranges.discard(fingerprint)
            self.body_range_signatures.pop(fingerprint, None)
        return outcome.coverage.stranded

    @property
    def unresolved_gap_count(self) -> int:
        return (
            len(self.read_in_progress)
            + len(self.unresolved_observations)
            + len(self.unrepresented_gaps)
        )

    @property
    def has_unresolved_gaps(self) -> bool:
        return bool(self.unresolved_gap_count)

    @property
    def has_skips(self) -> bool:
        return bool(self.skipped_partial or self.unrepresented_gaps)

    def reliability_metrics(
        self,
        *,
        viewport_observations: int,
        visible_text_segments: int,
        lookahead_rows: int = 0,
    ) -> dict[str, int | str]:
        read_in_progress = len(self.read_in_progress)
        unresolved_observations = len(self.unresolved_observations)
        unrepresented_gaps = len(self.unrepresented_gaps)
        gap_sources = tuple(
            category
            for category, count in (
                ("READ_IN_PROGRESS", read_in_progress),
                ("UNVERIFIED_OBSERVATION", unresolved_observations),
                ("UNREPRESENTED_GAP", unrepresented_gaps),
            )
            if count
        )
        gap_category = (
            "NONE"
            if not gap_sources
            else gap_sources[0]
            if len(gap_sources) == 1
            else "MULTIPLE"
        )
        return {
            "viewport_observations": viewport_observations,
            "unique_provenance_identities": len(self.provenance_verified),
            "unique_verified_posts": len(self.fully_read),
            "unique_deferred_posts": len(self.unique_deferred),
            "unresolved_gaps": self.unresolved_gap_count,
            "read_in_progress": read_in_progress,
            "unresolved_observations": unresolved_observations,
            "unrepresented_gaps": unrepresented_gaps,
            "gap_sources_total": self.unresolved_gap_count,
            "stranded_body_ranges": len(self.stranded_body_ranges),
            "lookahead_rows": lookahead_rows,
            "gap_category": gap_category,
            "visible_text_segments": visible_text_segments,
        }


@dataclass(slots=True)
class DedupeLedger:
    previously_seen: set[str]
    pending: list[str]
    pending_set: set[str]
    seen_this_run: set[str] = field(default_factory=set)
    new_fingerprints: set[str] = field(default_factory=set)
    decisions_by_fingerprint: dict[str, str] = field(default_factory=dict)
    post_numbers: dict[str, int] = field(default_factory=dict)
    next_post_number: int = 0
    streamed_home_fingerprints: set[str] = field(default_factory=set)
    streamed_segment_ids: set[str] = field(default_factory=set)
    checkpoint_overlap: set[str] = field(default_factory=set)
    display_salt: bytes = field(default_factory=lambda: secrets.token_bytes(16), repr=False)

    @classmethod
    def from_checkpoint(
        cls,
        seen_fingerprints: tuple[str, ...],
        pending_fingerprints: tuple[str, ...],
    ) -> DedupeLedger:
        pending = list(pending_fingerprints)
        return cls(
            previously_seen=set(seen_fingerprints),
            pending=pending,
            pending_set=set(pending),
        )


@dataclass(slots=True)
class WorkflowStats:
    duplicates: int = 0
    relevant: int = 0
    irrelevant: int = 0
    unknown: int = 0
    found_relevant: bool = False
    previewed_bodies: int = 0
    observed_rows: int = 0
    body_extracted: int = 0
    provenance_verified: int = 0
    classified: int = 0
    observation_unknown: int = 0
    unverified_relevant: int = 0
    unsettled_unverified_relevance: int = 0
    skipped_rows: int = 0
    viewport_coverage_complete: bool = True
    visible_text_segments: int = 0
    lookahead_rows: int = 0
    consecutive_moved_no_content: int = 0

    def accumulate(self, other: WorkflowStats) -> None:
        """Add one bounded cycle's metrics to this session snapshot."""
        for name in self.__dataclass_fields__:
            current = getattr(self, name)
            value = getattr(other, name)
            if name == "found_relevant":
                setattr(self, name, current or value)
            elif name == "viewport_coverage_complete":
                setattr(self, name, current and value)
            else:
                setattr(self, name, current + value)
