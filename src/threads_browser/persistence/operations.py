"""Sanitized, atomic latest-run status persistence."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from pathlib import Path

from .filesystem import _ensure_directory, _ensure_root, _write_json
from .models import BrowserDataError
from .schema import _OPERATION_COUNTERS, _OPERATION_STATUS_KEYS, _UTC_TIMESTAMP


def write_operation_status(data_root: Path, document: Mapping[str, object]) -> None:
    """Atomically replace the sanitized latest scheduled-run summary."""
    if (
        set(document) != _OPERATION_STATUS_KEYS
        or type(document.get("schema_version")) is not int
        or document.get("schema_version") != 1
        or not isinstance(document.get("started_at"), str)
        or _UTC_TIMESTAMP.fullmatch(document["started_at"]) is None
        or (
            document.get("finished_at") is not None
            and (
                not isinstance(document.get("finished_at"), str)
                or _UTC_TIMESTAMP.fullmatch(document["finished_at"]) is None
            )
        )
        or not isinstance(document.get("status"), str)
        or document.get("status") not in {"RUNNING", "COMPLETE", "FAILED", "STOPPED"}
        or not isinstance(document.get("stop_reason"), str)
        or re.fullmatch(r"[A-Z0-9_]{1,80}", document["stop_reason"]) is None
        or (
            document.get("cycle_status") is not None
            and (
                not isinstance(document.get("cycle_status"), str)
                or re.fullmatch(r"[A-Z0-9_]{1,80}", document["cycle_status"]) is None
            )
        )
    ):
        raise BrowserDataError("STATE_INVALID")

    duration = document.get("duration_seconds")
    exit_code = document.get("exit_code")
    checkpoint_runs = document.get("checkpoint_runs")
    counters = document.get("counters")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration < 0
        or (exit_code is not None and type(exit_code) is not int)
        or (
            checkpoint_runs is not None
            and (type(checkpoint_runs) is not int or checkpoint_runs < 0)
        )
        or not isinstance(counters, Mapping)
        or not set(counters).issubset(_OPERATION_COUNTERS)
        or any(type(value) is not int or value < 0 for value in counters.values())
    ):
        raise BrowserDataError("STATE_INVALID")

    if not _truthful_lifecycle(document, exit_code, checkpoint_runs, counters):
        raise BrowserDataError("STATE_INVALID")

    root = _ensure_root(data_root)
    directory = _ensure_directory(root, "operations")
    _write_json(root, directory / "last_run.json", dict(document))


def _truthful_lifecycle(
    document: Mapping[str, object],
    exit_code: object,
    checkpoint_runs: object,
    counters: object,
) -> bool:
    status = document["status"]
    finished_at = document["finished_at"]
    stop_reason = document["stop_reason"]
    if status == "RUNNING":
        return (
            finished_at is None
            and document["cycle_status"] is None
            and exit_code is None
            and checkpoint_runs is None
            and counters == {}
            and stop_reason == "RUNNING"
        )
    if finished_at is None or type(exit_code) is not int or stop_reason == "RUNNING":
        return False
    if status == "COMPLETE":
        return exit_code == 0 and type(checkpoint_runs) is int and checkpoint_runs > 0
    return status in {"FAILED", "STOPPED"} and exit_code == 1
