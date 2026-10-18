from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from threads_browser.errors import UiReadError
from threads_browser.nurture.auto import run_auto_nurture
from threads_browser.nurture.state import auto_policy_fingerprint, policy_fingerprint


def _result(*fingerprints: str, home_new: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        status="SESSION_COMPLETE",
        stop_reason="FEED_END_REACHED",
        home_sampled=len(fingerprints),
        home_new=len(fingerprints) if home_new is None else home_new,
        home_relevant=1,
        detail_reads=1,
        scrolls=2,
        checkpoint_saved=True,
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


def test_auto_supervisor_caps_transient_retries() -> None:
    attempts = 0
    sleeps: list[float] = []

    async def run_cycle() -> SimpleNamespace:
        nonlocal attempts
        attempts += 1
        raise UiReadError("HOME_NAVIGATION_TIMEOUT")

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    with pytest.raises(UiReadError) as caught:
        asyncio.run(run_auto_nurture(run_cycle, sleep=fake_sleep))

    assert caught.value.code == "HOME_NAVIGATION_TIMEOUT"
    assert attempts == 3
    assert sleeps == [10, 30]


def test_auto_policy_checkpoint_is_versioned_separately_from_terms() -> None:
    auto = auto_policy_fingerprint()
    assert len(auto) == 64
    assert auto != policy_fingerprint(("auto-nurture-v1",))
