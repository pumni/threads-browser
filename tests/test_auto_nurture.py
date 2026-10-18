from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from threads_browser.errors import UiReadError
from threads_browser.nurture.auto import run_auto_nurture
from threads_browser.nurture.state import auto_policy_fingerprint, policy_fingerprint
from threads_browser.storage import BrowserDataError


def _result(
    *fingerprints: str,
    home_new: int | None = None,
    status: str = "SESSION_COMPLETE",
    checkpoint_saved: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        stop_reason="FEED_END_REACHED",
        home_sampled=len(fingerprints),
        home_new=len(fingerprints) if home_new is None else home_new,
        home_relevant=1,
        detail_reads=1,
        scrolls=2,
        checkpoint_saved=checkpoint_saved,
        new_fingerprints=fingerprints,
    )


def test_auto_supervisor_counts_distinct_fingerprints_and_backs_off() -> None:
    first = "a" * 64
    second = "b" * 64
    results = iter(
        (
            _result(first, first),
            _result(first),
            _result(),
            _result(second),
        )
    )
    sleeps: list[float] = []
    output: list[str] = []

    async def run_cycle() -> SimpleNamespace:
        return next(results)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    assert (
        asyncio.run(
            run_auto_nurture(run_cycle, max_cycles=4, sleep=fake_sleep, emit=output.append)
        )
        == 4
    )
    assert sleeps == [15, 15, 30]
    assert "observed=2 new=1" in output[0]
    assert "observed=1 new=0" in output[1]
    assert "new=0" in output[2]
    assert "new=1" in output[3]
    assert "cooldown_s=30" in output[2]


@pytest.mark.parametrize("code", ["LOGIN_REQUIRED", "CHALLENGE_REQUIRED", "RATE_LIMITED"])
def test_auto_supervisor_stops_without_retrying_security_states(code: str) -> None:
    sleeps: list[float] = []

    async def run_cycle() -> SimpleNamespace:
        raise UiReadError(code)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with pytest.raises(UiReadError) as caught:
        asyncio.run(run_auto_nurture(run_cycle, sleep=fake_sleep))

    assert caught.value.code == code
    assert sleeps == []


@pytest.mark.parametrize(
    "code",
    ("HOME_NAVIGATION_TIMEOUT", "FEED_INITIAL_NOT_READY", "FEED_SCROLL_NOT_READY"),
)
def test_auto_supervisor_caps_transient_retries(code: str) -> None:
    attempts = 0
    sleeps: list[float] = []

    async def run_cycle() -> SimpleNamespace:
        nonlocal attempts
        attempts += 1
        raise UiReadError(code)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with pytest.raises(UiReadError) as caught:
        asyncio.run(run_auto_nurture(run_cycle, sleep=fake_sleep))

    assert caught.value.code == code
    assert attempts == 3
    assert sleeps == [10, 30]


@pytest.mark.parametrize(
    "code",
    ("ACCOUNT_NOT_VERIFIED", "ACCOUNT_BUSY", "BROWSER_CLEANUP_FAILED"),
)
def test_auto_supervisor_does_not_retry_account_or_cleanup_failures(code: str) -> None:
    sleeps: list[float] = []

    async def run_cycle() -> SimpleNamespace:
        raise BrowserDataError(code)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with pytest.raises(BrowserDataError) as caught:
        asyncio.run(run_auto_nurture(run_cycle, sleep=fake_sleep))

    assert caught.value.code == code
    assert sleeps == []


@pytest.mark.parametrize("code", ["FEED_INITIAL_NOT_READY", "FEED_SCROLL_NOT_READY"])
def test_auto_supervisor_retries_feed_readiness_codes(code: str) -> None:
    attempts = 0
    sleeps: list[float] = []
    output: list[str] = []

    async def run_cycle() -> SimpleNamespace:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise UiReadError(code)
        return _result("c" * 64)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    assert asyncio.run(
        run_auto_nurture(
            run_cycle,
            max_cycles=1,
            sleep=fake_sleep,
            emit=output.append,
        )
    ) == 1
    assert attempts == 2
    assert sleeps == [10]
    assert f"stop_code={code} retry=1 cooldown_s=10" in output[0]


@pytest.mark.parametrize(
    ("status", "checkpoint_saved"),
    (
        ("SESSION_COVERAGE_INCOMPLETE", False),
        ("VIEWPORT_OBSERVATION_INCOMPLETE", False),
        ("DETAIL_RETRY_PENDING", False),
        ("DETAIL_RETRY_PENDING", True),
        ("NO_ACTIONABLE_TARGET", False),
        ("UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION", False),
        ("OBSERVED_UNVERIFIED_ONLY", False),
        ("OBSERVATION_UNCERTAIN_ONLY", False),
        ("NO_VERIFIED_POSTS_IN_BUDGET", False),
        ("RELEVANT_POST_NOT_CURRENT", False),
    ),
)
def test_auto_supervisor_recovers_from_safe_incomplete_sessions(
    status: str,
    checkpoint_saved: bool,
) -> None:
    attempts = 0
    sleeps: list[float] = []
    output: list[str] = []
    fingerprint = "d" * 64

    async def run_cycle() -> SimpleNamespace:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _result(
                fingerprint,
                status=status,
                checkpoint_saved=checkpoint_saved,
            )
        return _result(fingerprint)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    assert asyncio.run(
        run_auto_nurture(
            run_cycle,
            max_cycles=1,
            sleep=fake_sleep,
            emit=output.append,
        )
    ) == 1
    assert attempts == 2
    assert sleeps == [10]
    assert f"status={status} checkpoint_saved={str(checkpoint_saved).lower()}" in output[0]
    assert "incomplete_retry=1 cooldown_s=10" in output[0]
    assert "observed=1 new=1" in output[1]


def test_auto_supervisor_stops_after_incomplete_retry_cap() -> None:
    attempts = 0
    sleeps: list[float] = []
    output: list[str] = []

    async def run_cycle() -> SimpleNamespace:
        nonlocal attempts
        attempts += 1
        return _result(
            "e" * 64,
            status="SESSION_COVERAGE_INCOMPLETE",
            checkpoint_saved=False,
        )

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with pytest.raises(UiReadError) as caught:
        asyncio.run(run_auto_nurture(run_cycle, sleep=fake_sleep, emit=output.append))

    assert caught.value.code == "INCOMPLETE_RETRY_LIMIT"
    assert attempts == 3
    assert sleeps == [10, 30]
    assert all("checkpoint_saved=false" in line for line in output)
    assert "INCOMPLETE_RETRY_LIMIT" in output[-1]


def test_auto_policy_checkpoint_is_versioned_separately_from_terms() -> None:
    auto = auto_policy_fingerprint()
    assert len(auto) == 64
    assert auto != policy_fingerprint(("auto-nurture-v1",))
