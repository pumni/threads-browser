"""Sanitized lifecycle status transitions for scheduled CLI runs."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from .storage import BrowserAccount, BrowserDataError, CheckpointStore


def _new_scheduled_status() -> tuple[dict[str, object], float]:
    started_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    return (
        {
            "schema_version": 1,
            "started_at": started_at,
            "finished_at": None,
            "duration_seconds": 0.0,
            "status": "RUNNING",
            "cycle_status": None,
            "stop_reason": "RUNNING",
            "exit_code": None,
            "checkpoint_runs": None,
            "counters": {},
        },
        monotonic(),
    )


def _safe_status_code(value: object, *, default: str) -> str:
    if isinstance(value, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", value):
        return value
    return default


def _mark_scheduled_failure(summary: dict[str, object] | None, reason: str) -> None:
    if summary is None:
        return
    summary.update(
        status="FAILED",
        cycle_status=None,
        stop_reason=_safe_status_code(reason, default="INTERNAL_OPERATION_ERROR"),
        exit_code=1,
        checkpoint_runs=None,
        counters={},
    )


def _mark_scheduled_result(summary: dict[str, object] | None, result: object) -> None:
    if summary is None:
        return
    checkpoint_saved = getattr(result, "checkpoint_saved", False) is True
    counters = {
        name: value
        for name in (
            "home_sampled",
            "home_new",
            "detail_reads",
            "scroll_attempts",
            "scrolls",
            "skipped_unverified",
            "unique_verified_posts",
        )
        if type(value := getattr(result, name, None)) is int and value >= 0
    }
    checkpoint_runs = getattr(result, "checkpoint_runs", None)
    summary.update(
        status="COMPLETE" if checkpoint_saved else "STOPPED",
        cycle_status=_safe_status_code(getattr(result, "status", None), default="UNKNOWN"),
        stop_reason=_safe_status_code(
            getattr(result, "stop_reason", None), default="UNKNOWN"
        ),
        exit_code=0 if checkpoint_saved else 1,
        checkpoint_runs=(
            checkpoint_runs
            if type(checkpoint_runs) is int and checkpoint_runs >= 0
            else None
        ),
        counters=counters,
    )


def _finish_scheduled_status(
    summary: dict[str, object], started_monotonic: float
) -> None:
    if summary["status"] == "RUNNING":
        _mark_scheduled_failure(summary, "INTERNAL_OPERATION_ERROR")
    summary["finished_at"] = datetime.now(UTC).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    summary["duration_seconds"] = round(max(0, monotonic() - started_monotonic), 3)


def _record_known_checkpoint_runs(
    summary: dict[str, object] | None,
    data_root: Path,
    account: BrowserAccount,
    terms: tuple[str, ...],
) -> None:
    if summary is None:
        return
    from .feed.relevance import canonical_terms
    from .reader import UiReadError
    from .workflow import policy_fingerprint

    try:
        checkpoint = CheckpointStore(data_root).load(
            account.account_id,
            policy_fingerprint(canonical_terms(terms)),
        )
    except (BrowserDataError, UiReadError):
        return
    summary["checkpoint_runs"] = checkpoint.completed_runs
