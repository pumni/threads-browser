"""Finite workflow phases and per-run observation coverage state."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256


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
            raise ValueError(f"invalid nurture transition: {self.phase} -> {phase}")
        self.phase = phase

    def stop(self, reason: str) -> None:
        if self.phase is not WorkflowPhase.STOPPED:
            self.transition(WorkflowPhase.STOPPED)
        self.stop_reason = reason


@dataclass(slots=True)
class CoverageLedger:
    provenance_verified: set[str] = field(default_factory=set)
    fully_read: set[str] = field(default_factory=set)
    deferred: set[str] = field(default_factory=set)
    unique_deferred: set[str] = field(default_factory=set)
    unresolved_observations: set[str] = field(default_factory=set)
    unrepresented_gaps: set[str] = field(default_factory=set)
    hydration_pending: set[str] = field(default_factory=set)
    observed_body_identities: set[str] = field(default_factory=set)

    def mark_verified(self, fingerprint: str, observation_id: str | None) -> None:
        self.provenance_verified.add(fingerprint)
        self.deferred.discard(fingerprint)
        if observation_id is not None:
            self.unresolved_observations.discard(observation_id)

    def mark_deferred(
        self,
        fingerprint: str | None,
        observation_id: str | None,
        gap_id: str,
    ) -> None:
        if fingerprint is not None:
            if observation_id is not None:
                self.unresolved_observations.discard(observation_id)
            self.deferred.add(fingerprint)
            self.unique_deferred.add(fingerprint)
        elif observation_id is not None:
            self.unresolved_observations.add(observation_id)
        else:
            self.unrepresented_gaps.add(gap_id)

    @property
    def unresolved_gap_count(self) -> int:
        return len(self.deferred) + len(self.unresolved_observations) + len(self.unrepresented_gaps)

    @property
    def has_unresolved_gaps(self) -> bool:
        return bool(self.unresolved_gap_count)

    def reliability_metrics(
        self,
        *,
        viewport_observations: int,
        visible_text_segments: int,
    ) -> dict[str, int]:
        return {
            "viewport_observations": viewport_observations,
            "unique_provenance_identities": len(self.provenance_verified),
            "unique_verified_posts": len(self.fully_read),
            "unique_deferred_posts": len(self.unique_deferred),
            "unresolved_gaps": self.unresolved_gap_count,
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
    consecutive_moved_no_content: int = 0
