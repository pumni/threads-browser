"""Privacy-safe aggregate result formatting for terminal output."""

from __future__ import annotations

import re


def _count(result: object, name: str, default: int = 0) -> int:
    value = getattr(result, name, None)
    return value if type(value) is int and value >= 0 else default


def _status_code(result: object, name: str, default: str) -> str:
    value = getattr(result, name, None)
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", value) else default


def _format_result(result: object) -> str:
    ratio_value = getattr(result, "home_relevant_ratio", None)
    ratio = (
        f"{ratio_value:.2f}"
        if type(ratio_value) in {int, float} and 0 <= ratio_value <= 1
        else "n/a"
    )
    observed_rows = _count(result, "observed_rows")
    provenance_verified = _count(result, "provenance_verified")
    coverage_complete = getattr(result, "viewport_coverage_complete", True)
    if type(coverage_complete) is not bool:
        coverage_complete = False
    values = {
        "status": _status_code(result, "status", "UNKNOWN"),
        "stop_reason": _status_code(result, "stop_reason", "UNKNOWN"),
        "ui": _status_code(result, "ui_status", "UNKNOWN"),
        "home_sampled": _count(result, "home_sampled"),
        "home_new": _count(result, "home_new"),
        "home_relevant": _count(result, "home_relevant"),
        "home_irrelevant": _count(result, "home_irrelevant"),
        "home_unknown": _count(result, "home_unknown"),
        "duplicates": _count(result, "duplicates"),
        "detail_reads": _count(result, "detail_reads"),
        "scroll_attempts": _count(result, "scroll_attempts"),
        "scrolls": _count(result, "scrolls"),
        "skipped_unverified": _count(result, "skipped_unverified"),
        "checkpoint_runs": _count(result, "checkpoint_runs"),
        "observed_rows": observed_rows,
        "viewport_row_appearances": _count(
            result, "viewport_observations", observed_rows
        ),
        "body_extracted": _count(result, "body_extracted"),
        "provenance_verified": provenance_verified,
        "unique_provenance_identities": _count(
            result, "unique_provenance_identities", provenance_verified
        ),
        "unique_verified_posts": _count(result, "unique_verified_posts"),
        "unique_deferred_posts": _count(result, "unique_deferred_posts"),
        "unresolved_gaps": _count(result, "unresolved_gaps"),
        "read_in_progress": _count(result, "read_in_progress"),
        "unresolved_observations": _count(result, "unresolved_observations"),
        "unrepresented_gaps": _count(result, "unrepresented_gaps"),
        "gap_sources_total": _count(result, "gap_sources_total"),
        "stranded_body_ranges": _count(result, "stranded_body_ranges"),
        "lookahead_rows": _count(result, "lookahead_rows"),
        "gap_category": _status_code(result, "gap_category", "NONE"),
        "visible_text_segments": _count(result, "visible_text_segments"),
        "classified": _count(result, "classified"),
        "observation_unknown": _count(result, "observation_unknown"),
        "unverified_relevant": _count(result, "unverified_relevant"),
        "skipped_rows": _count(result, "skipped_rows"),
    }
    status = values.pop("status")
    stop_reason = values.pop("stop_reason")
    ui_status = values.pop("ui")
    home_counts = " ".join(
        f"{name}={values.pop(name)}"
        for name in (
            "home_sampled",
            "home_new",
            "home_relevant",
            "home_irrelevant",
            "home_unknown",
        )
    )
    remaining = " ".join(f"{name}={value}" for name, value in values.items())
    return (
        f"NURTURE status={status} source=HOME_FOR_YOU stop_reason={stop_reason} "
        f"ui={ui_status} {home_counts} home_relevant_ratio={ratio} {remaining} "
        f"viewport_coverage_complete={str(coverage_complete).lower()}"
    )
