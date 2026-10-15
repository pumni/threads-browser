"""Pure decisions for one Home row observation."""

from __future__ import annotations

from dataclasses import dataclass

_LOOKAHEAD_STATES = frozenset({"DEFERRED_OFFSCREEN", "DEFERRED_TEXT_BELOW"})


@dataclass(frozen=True, slots=True)
class RowCoverageState:
    identity_recognized: bool = False
    read_in_progress: bool = False
    body_complete: bool = False
    unresolved_observation: bool = False


@dataclass(frozen=True, slots=True)
class RowObservationFacts:
    fingerprint: str | None
    observation_id: str | None
    gap_id: str
    state: str
    has_body: bool
    body_complete: bool


@dataclass(frozen=True, slots=True)
class RowObservationOutcome:
    coverage: RowCoverageState
    identity_added: bool
    read_started: bool
    body_completed: bool
    hydration_pending: bool | None
    body_observed: bool


def observe_row(
    facts: RowObservationFacts,
    previous: RowCoverageState,
) -> RowObservationOutcome:
    """Reduce a row observation to monotonic identity and body-read state.

    The function has no browser, logging, or statistics dependencies. A row
    below the viewport is lookahead until it has been encountered in an earlier
    observation or visible body content is available.
    """

    identity_recognized = previous.identity_recognized or facts.fingerprint is not None
    entered = facts.state != "NON_POST" and (
        facts.state not in _LOOKAHEAD_STATES or facts.has_body
    )
    encountered = entered or previous.read_in_progress or previous.unresolved_observation
    body_complete = previous.body_complete or (
        facts.fingerprint is not None
        and facts.state == "VERIFIED"
        and facts.body_complete
        and facts.has_body
    )
    read_in_progress = (
        False
        if body_complete
        else previous.read_in_progress
        or (identity_recognized and encountered)
    )
    unresolved_observation = (
        previous.unresolved_observation
        if facts.fingerprint is None and not entered
        else bool(facts.fingerprint is None and entered)
    )
    if body_complete or (identity_recognized and encountered):
        unresolved_observation = False

    if facts.state == "PENDING_HYDRATION":
        hydration_pending: bool | None = True
    elif facts.state in {"VERIFIED", "NON_POST"}:
        hydration_pending = False
    else:
        hydration_pending = None

    coverage = RowCoverageState(
        identity_recognized=identity_recognized,
        read_in_progress=read_in_progress,
        body_complete=body_complete,
        unresolved_observation=unresolved_observation,
    )
    return RowObservationOutcome(
        coverage=coverage,
        identity_added=identity_recognized and not previous.identity_recognized,
        read_started=read_in_progress and not previous.read_in_progress,
        body_completed=body_complete and not previous.body_complete,
        hydration_pending=hydration_pending,
        body_observed=facts.has_body,
    )
