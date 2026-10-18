"""Continuous supervisor for bounded, read-only Auto Nurture cycles."""

from __future__ import annotations

import asyncio
import re
from collections import deque
from collections.abc import Awaitable, Callable

from ..errors import UiReadError
from .results import NurtureResult
from .uat_diagnostics import AutoUatDiagnostics

_RETRYABLE_CODES = frozenset(
    {
        "HOME_NAVIGATION_TIMEOUT",
        "HOME_RENDER_PENDING",
        "HOME_FEED_NOT_READY",
        "FEED_INITIAL_NOT_READY",
        "FEED_SCROLL_NOT_READY",
        "FOR_YOU_ACTIVATION_NOT_READY",
        "DOM_CHANGED_DURING_SCAN",
        "UI_READ_UNVERIFIED",
        "DETAIL_NAVIGATION_UNVERIFIED",
        "DETAIL_RENDER_PENDING",
        "DETAIL_RENDER_UNAVAILABLE",
        "DETAIL_ROOT_UNVERIFIED",
        "DETAIL_BODY_UNVERIFIED",
        "DETAIL_PROVENANCE_UNVERIFIED",
    }
)
_RECOVERABLE_SESSION_STATUSES = frozenset(
    {
        "SESSION_COVERAGE_INCOMPLETE",
        "VIEWPORT_OBSERVATION_INCOMPLETE",
        "DETAIL_RETRY_PENDING",
        "NO_ACTIONABLE_TARGET",
        "UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION",
        "OBSERVED_UNVERIFIED_ONLY",
        "OBSERVATION_UNCERTAIN_ONLY",
        "NO_VERIFIED_POSTS_IN_BUDGET",
        "RELEVANT_POST_NOT_CURRENT",
    }
)
_MAX_TRANSIENT_RETRIES = 2
_MAX_INCOMPLETE_RETRIES = 2
_MAX_DISTINCT_FINGERPRINTS = 5_000
_BASE_IDLE_SECONDS = 15
_MAX_IDLE_SECONDS = 300
_RETRY_COOLDOWNS = (10, 30)
_SAFE_CODE = re.compile(r"[A-Z0-9_]{1,80}")


async def run_auto_nurture(
    run_cycle: Callable[[], Awaitable[NurtureResult]],
    *,
    max_cycles: int | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    emit: Callable[[str], None] = print,
    uat_diagnostics: AutoUatDiagnostics | None = None,
) -> int:
    """Run fresh bounded browser sessions until interrupted or a safe stop."""
    if max_cycles is not None and (type(max_cycles) is not int or max_cycles < 1):
        raise UiReadError("CYCLE_LIMIT_INVALID")

    cycles = 0
    no_novel_cycles = 0
    transient_retries = 0
    incomplete_retries = 0
    recent_fingerprints: set[str] = set()
    fingerprint_order: deque[str] = deque()

    while max_cycles is None or cycles < max_cycles:
        if uat_diagnostics is not None:
            uat_diagnostics.begin_attempt()
        try:
            result = await run_cycle()
        except UiReadError as error:
            if error.code not in _RETRYABLE_CODES:
                if uat_diagnostics is not None:
                    uat_diagnostics.report_terminal(error.code)
                raise
            transient_retries += 1
            if transient_retries > _MAX_TRANSIENT_RETRIES:
                if uat_diagnostics is not None:
                    uat_diagnostics.report_terminal(error.code)
                raise
            cooldown = _RETRY_COOLDOWNS[transient_retries - 1]
            if uat_diagnostics is not None:
                uat_diagnostics.report(
                    "ATTEMPT",
                    "RETRYING",
                    code=error.code,
                    counts={"retry": transient_retries, "cooldown_s": cooldown},
                )
            emit(
                f"AUTO cycle={cycles + 1} stop_code={_safe_code(error.code)} "
                f"retry={transient_retries} cooldown_s={cooldown}"
            )
            await sleep(cooldown)
            continue

        transient_retries = 0
        if uat_diagnostics is not None:
            uat_diagnostics.report_result(result)
        status = getattr(result, "status", None)
        if status in _RECOVERABLE_SESSION_STATUSES:
            incomplete_retries += 1
            if incomplete_retries > _MAX_INCOMPLETE_RETRIES:
                if uat_diagnostics is not None:
                    uat_diagnostics.report_terminal("INCOMPLETE_RETRY_LIMIT")
                emit(
                    f"AUTO cycle={cycles + 1} stop_code=INCOMPLETE_RETRY_LIMIT "
                    f"status={_safe_code(status)} "
                    f"checkpoint_saved={str(getattr(result, 'checkpoint_saved', False) is True).lower()}"
                )
                raise UiReadError("INCOMPLETE_RETRY_LIMIT")
            cooldown = _RETRY_COOLDOWNS[incomplete_retries - 1]
            if uat_diagnostics is not None:
                uat_diagnostics.report(
                    "ATTEMPT",
                    "RETRYING",
                    code=_safe_code(status),
                    counts={"retry": incomplete_retries, "cooldown_s": cooldown},
                )
            emit(
                f"AUTO cycle={cycles + 1} status={_safe_code(status)} "
                f"checkpoint_saved={str(getattr(result, 'checkpoint_saved', False) is True).lower()} "
                f"incomplete_retry={incomplete_retries} cooldown_s={cooldown}"
            )
            await sleep(cooldown)
            continue

        if status not in {"SESSION_COMPLETE", "SESSION_BUDGET_EXHAUSTED"}:
            stop_code = _safe_code(getattr(result, "stop_reason", None))
            if uat_diagnostics is not None:
                uat_diagnostics.report_terminal(stop_code)
            raise UiReadError(stop_code)
        if getattr(result, "checkpoint_saved", False) is not True:
            if uat_diagnostics is not None:
                uat_diagnostics.report_terminal("CYCLE_NOT_CHECKPOINTED")
            raise UiReadError("CYCLE_NOT_CHECKPOINTED")

        cycles += 1
        incomplete_retries = 0
        fresh_count = _record_new_fingerprints(
            result,
            recent_fingerprints,
            fingerprint_order,
        )
        observed = _count(result, "home_sampled")
        eligible = _count(result, "home_relevant")
        details = _count(result, "detail_reads")
        scrolls = _count(result, "scrolls")
        refresh_cause = _safe_code(getattr(result, "stop_reason", None))
        no_novel_cycles = no_novel_cycles + 1 if fresh_count == 0 else 0
        backoff_step = min(max(0, no_novel_cycles - 1), 5)
        cooldown = min(_BASE_IDLE_SECONDS * 2**backoff_step, _MAX_IDLE_SECONDS)
        if uat_diagnostics is not None:
            uat_diagnostics.report("ATTEMPT", "COMPLETED", code=_safe_code(status))
        emit(
            f"AUTO cycle={cycles} observed={observed} new={fresh_count} "
            f"eligible={eligible} detail={details} scrolls={scrolls} "
            f"refresh={refresh_cause} cooldown_s={cooldown}"
        )

        if max_cycles is not None and cycles >= max_cycles:
            return cycles
        await sleep(cooldown)
    return cycles


def _record_new_fingerprints(
    result: NurtureResult,
    recent: set[str],
    order: deque[str],
) -> int:
    values = getattr(result, "new_fingerprints", ())
    fingerprints = (
        tuple(value for value in values if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value))
        if isinstance(values, (tuple, list))
        else ()
    )
    if not fingerprints:
        return _count(result, "home_new")

    novel = 0
    for fingerprint in fingerprints:
        if fingerprint in recent:
            continue
        recent.add(fingerprint)
        order.append(fingerprint)
        novel += 1
        if len(order) > _MAX_DISTINCT_FINGERPRINTS:
            recent.discard(order.popleft())
    return novel


def _count(result: object, name: str) -> int:
    value = getattr(result, name, None)
    return value if type(value) is int and value >= 0 else 0


def _safe_code(value: object) -> str:
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else "UNKNOWN"
