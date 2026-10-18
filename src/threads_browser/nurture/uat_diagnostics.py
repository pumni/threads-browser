"""Opt-in, privacy-safe phase summaries for bounded Auto UAT runs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

_MAX_COUNT = 1_000_000
_PHASES = frozenset(
    {"ATTEMPT", "HOME", "FEED", "DETAIL", "BACK", "COVERAGE", "CHECKPOINT", "STOP"}
)
_STATUSES = frozenset(
    {
        "STARTED",
        "READY",
        "RESUMED",
        "OBSERVED",
        "PARTIAL",
        "FAILED",
        "VERIFIED",
        "RETRY_PENDING",
        "REQUESTED",
        "RETURNED",
        "COMPLETE",
        "INCOMPLETE",
        "SAVED",
        "NOT_SAVED",
        "RETRYING",
        "COMPLETED",
        "RESULT",
        "STOPPED",
    }
)
_COUNTERS = frozenset(
    {
        "retry",
        "cooldown_s",
        "sampled",
        "relevant",
        "unknown",
        "scroll_attempts",
        "scrolls",
        "observed_rows",
        "verified_rows",
        "body_read",
        "body_unknown",
        "skipped_rows",
        "detail_reads",
        "provenance_identities",
        "verified_posts",
        "deferred_posts",
        "unresolved_gaps",
        "read_in_progress",
        "unresolved_observations",
        "unrepresented_gaps",
        "stranded_body_ranges",
        "lookahead_rows",
        "checkpoint_runs",
    }
)
_GAP_CATEGORIES = frozenset(
    {
        "NONE",
        "READ_IN_PROGRESS",
        "UNVERIFIED_OBSERVATION",
        "UNREPRESENTED_GAP",
        "MULTIPLE",
    }
)
_SAFE_CODES = frozenset(
    {
        "ACCOUNT_BUSY",
        "ACCOUNT_INVALID",
        "ACCOUNT_NOT_VERIFIED",
        "ACTOR_MISMATCH",
        "AUTO_TARGET_NOT_ELIGIBLE",
        "BROWSER_CLEANUP_FAILED",
        "BROWSER_SESSION_UNAVAILABLE",
        "CHALLENGE_REQUIRED",
        "CYCLE_NOT_CHECKPOINTED",
        "DATA_ROOT_INVALID",
        "DATA_ROOT_UNAVAILABLE",
        "DETAIL_BODY_UNVERIFIED",
        "DETAIL_IDENTITY_ALREADY_ATTEMPTED",
        "DETAIL_NAVIGATION_UNVERIFIED",
        "DETAIL_PROVENANCE_UNVERIFIED",
        "DETAIL_RENDER_PENDING",
        "DETAIL_RENDER_UNAVAILABLE",
        "DETAIL_ROOT_AMBIGUOUS",
        "DETAIL_ROOT_UNVERIFIED",
        "DOM_CHANGED_DURING_SCAN",
        "FEED_END_REACHED",
        "FEED_INITIAL_NOT_READY",
        "FEED_NOT_SCROLLABLE",
        "FEED_REGION_UNVERIFIED",
        "FEED_SCROLL_NOT_READY",
        "FEED_VIEWPORT_UNVERIFIED",
        "FOR_YOU_ACTIVATION_NOT_READY",
        "FOR_YOU_CONTROL_AMBIGUOUS",
        "FOR_YOU_CONTROL_NOT_READY",
        "HOME_FEED_NOT_READY",
        "HOME_NAVIGATION_TIMEOUT",
        "HOME_RENDER_PENDING",
        "HOME_SCROLL_ADVANCE_OVERSIZED",
        "HOME_SCROLL_SURFACE_UNVERIFIED",
        "HOME_SCROLL_UNVERIFIED",
        "INCOMPLETE_RETRY_LIMIT",
        "INTERNAL_OPERATION_ERROR",
        "INTERNAL_STATE_ERROR",
        "LOCK_UNAVAILABLE",
        "LOGIN_REQUIRED",
        "OFF_ORIGIN_REDIRECT",
        "PROFILE_CONTROL_AMBIGUOUS",
        "PROFILE_CLICK_NO_NAVIGATION",
        "PROFILE_ROUTE_CHANGED",
        "PROFILE_ROUTE_UNVERIFIED",
        "PROFILE_UNAVAILABLE",
        "RATE_LIMITED",
        "RETURN_CONTEXT_CHANGED",
        "RETURN_ROUTE_CHANGED",
        "RETURN_UNVERIFIED",
        "SCAN_ROW_UNVERIFIED",
        "SESSION_BUDGET_EXHAUSTED",
        "SESSION_COMPLETE",
        "SESSION_COVERAGE_INCOMPLETE",
        "SESSION_TIMEOUT",
        "STATE_INVALID",
        "STATE_PATH_INVALID",
        "UI_READ_UNVERIFIED",
        "UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION",
        "VIEWPORT_OBSERVATION_INCOMPLETE",
        "DETAIL_RETRY_PENDING",
        "NO_ACTIONABLE_TARGET",
        "OBSERVED_UNVERIFIED_ONLY",
        "OBSERVATION_UNCERTAIN_ONLY",
        "NO_VERIFIED_POSTS_IN_BUDGET",
        "RELEVANT_POST_NOT_CURRENT",
        "COVERAGE_INCOMPLETE",
        "MAX_DETAIL_READS",
        "MAX_TOTAL_POSTS",
    }
)


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        return 0
    return min(value, _MAX_COUNT)


@dataclass(slots=True)
class AutoUatDiagnostics:
    """Emit only allowlisted codes, phase labels, and bounded integer counts."""

    emit: Callable[[str], None] = print
    attempt: int = 0

    def begin_attempt(self) -> None:
        self.attempt = min(self.attempt + 1, _MAX_COUNT)
        self.report("ATTEMPT", "STARTED")

    def report(
        self,
        phase: str,
        status: str,
        *,
        viewport: int | None = None,
        code: str | None = None,
        counts: Mapping[str, object] | None = None,
        gap_category: str | None = None,
    ) -> None:
        safe_phase = phase if isinstance(phase, str) and phase in _PHASES else "STOP"
        safe_status = status if isinstance(status, str) and status in _STATUSES else "FAILED"
        fields = [
            f"attempt={_count(self.attempt)}",
            f"phase={safe_phase}",
            f"status={safe_status}",
        ]
        if viewport is not None:
            fields.append(f"viewport={_count(viewport)}")
        if code is not None:
            safe_code = code if isinstance(code, str) and code in _SAFE_CODES else "UNKNOWN"
            fields.append(f"code={safe_code}")
        if counts is not None:
            safe_counts = sorted(
                (name, _count(value))
                for name, value in counts.items()
                if isinstance(name, str) and name in _COUNTERS
            )
            fields.extend(f"{name}={value}" for name, value in safe_counts)
        if gap_category is not None:
            category = (
                gap_category
                if isinstance(gap_category, str) and gap_category in _GAP_CATEGORIES
                else "UNKNOWN"
            )
            fields.append(f"gap_category={category}")
        self.emit("UAT " + " ".join(fields))

    def report_result(self, result: object) -> None:
        self.report(
            "HOME",
            "OBSERVED",
            counts={
                "sampled": getattr(result, "home_sampled", 0),
                "relevant": getattr(result, "home_relevant", 0),
                "unknown": getattr(result, "home_unknown", 0),
            },
        )
        self.report(
            "FEED",
            "OBSERVED",
            counts={
                "scroll_attempts": getattr(result, "scroll_attempts", 0),
                "scrolls": getattr(result, "scrolls", 0),
                "observed_rows": getattr(result, "observed_rows", 0),
                "verified_rows": getattr(result, "provenance_verified", 0),
                "body_read": getattr(result, "body_extracted", 0),
                "body_unknown": getattr(result, "observation_unknown", 0),
                "skipped_rows": getattr(result, "skipped_rows", 0),
            },
        )
        unresolved_gaps = _count(getattr(result, "unresolved_gaps", 0))
        complete = (
            getattr(result, "viewport_coverage_complete", False) is True
            and unresolved_gaps == 0
        )
        self.report(
            "COVERAGE",
            "COMPLETE" if complete else "INCOMPLETE",
            counts={
                "provenance_identities": getattr(result, "unique_provenance_identities", 0),
                "verified_posts": getattr(result, "unique_verified_posts", 0),
                "deferred_posts": getattr(result, "unique_deferred_posts", 0),
                "unresolved_gaps": unresolved_gaps,
                "read_in_progress": getattr(result, "read_in_progress", 0),
                "unresolved_observations": getattr(result, "unresolved_observations", 0),
                "unrepresented_gaps": getattr(result, "unrepresented_gaps", 0),
                "stranded_body_ranges": getattr(result, "stranded_body_ranges", 0),
                "lookahead_rows": getattr(result, "lookahead_rows", 0),
            },
            gap_category=getattr(result, "gap_category", "UNKNOWN"),
        )
        self.report(
            "CHECKPOINT",
            "SAVED" if getattr(result, "checkpoint_saved", False) is True else "NOT_SAVED",
            counts={"checkpoint_runs": getattr(result, "checkpoint_runs", 0)},
        )
        self.report(
            "ATTEMPT",
            "RESULT",
            code=getattr(result, "status", None),
            counts={"detail_reads": getattr(result, "detail_reads", 0)},
        )

    def report_terminal(self, code: object) -> None:
        safe_code = code if isinstance(code, str) else None
        self.report("STOP", "FAILED", code=safe_code)
