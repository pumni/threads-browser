from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from threads_browser import cli, workflow
from threads_browser.cli import main
from threads_browser.reader import UiReadError
from threads_browser.storage import BrowserDataError, TermsStore


@pytest.mark.parametrize(
    "code",
    ("ACCOUNT_BUSY", "LOGIN_REQUIRED", "CHALLENGE_REQUIRED", "RATE_LIMITED", "CYCLE_TIMEOUT"),
)
def test_scheduled_failures_write_sanitized_status(
    tmp_path: Path,
    monkeypatch,
    capsys,
    code: str,
) -> None:
    root = tmp_path / "private-root"
    profile = tmp_path / "existing-profile"
    profile.mkdir()
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(root))
    assert (
        main(
            [
                "account",
                "add",
                "pilot",
                "--actor",
                "offline.operator",
                "--profile-dir",
                str(profile),
            ]
        )
        == 0
    )
    TermsStore(root).set("pilot", ("hiring",))
    capsys.readouterr()
    monkeypatch.setattr(cli, "_has_interactive_terminal", lambda: True)
    calls: dict[str, object] = {}

    async def fail_run(_account, _store, **kwargs):
        calls.update(kwargs)
        if code == "ACCOUNT_BUSY":
            raise BrowserDataError(code)
        raise UiReadError(code)

    monkeypatch.setattr(workflow, "run_browser_nurture", fail_run)
    assert main(["run", "pilot", "--scheduled", "--max-runtime-seconds", "720"]) == 1
    captured = capsys.readouterr()
    status = json.loads((root / "operations" / "last_run.json").read_text(encoding="utf-8"))

    assert status["status"] == "FAILED"
    assert status["stop_reason"] == code
    assert status["exit_code"] == 1
    assert status["checkpoint_runs"] == 0
    assert status["counters"] == {}
    assert status["started_at"].endswith("Z")
    assert status["finished_at"].endswith("Z")
    assert status["duration_seconds"] >= 0
    assert calls["readiness_prompt"] is None
    complete_output = json.dumps(status) + captured.out + captured.err
    assert "offline.operator" not in complete_output
    assert "pilot" not in complete_output
    assert str(profile) not in complete_output


def test_scheduled_success_records_only_safe_counts(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    root = tmp_path / "private-root"
    profile = tmp_path / "existing-profile"
    profile.mkdir()
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(root))
    assert (
        main(
            [
                "account",
                "add",
                "pilot",
                "--actor",
                "offline.operator",
                "--profile-dir",
                str(profile),
            ]
        )
        == 0
    )
    TermsStore(root).set("pilot", ("hiring",))
    capsys.readouterr()

    async def successful_run(_account, _store, **_kwargs):
        return SimpleNamespace(
            status="COMPLETE_WITH_SKIPS",
            checkpoint_saved=True,
            ui_status="UNVERIFIED_LIVE_UI",
            stop_reason="FEED_END_REACHED",
            checkpoint_runs=7,
            home_sampled=12,
            home_new=3,
            home_relevant=2,
            home_irrelevant=1,
            home_unknown=0,
            home_relevant_ratio=0.67,
            duplicates=4,
            detail_reads=1,
            scroll_attempts=2,
            scrolls=2,
            skipped_unverified=1,
            unique_verified_posts=1,
            actor="offline.operator",
            url="https://www.threads.com/@candidate/post/synthetic",
            body="synthetic body",
        )

    monkeypatch.setattr(workflow, "run_browser_nurture", successful_run)
    assert main(["run", "pilot", "--scheduled", "--max-runtime-seconds", "720"]) == 0
    output = capsys.readouterr().out
    status_json = (root / "operations" / "last_run.json").read_text(encoding="utf-8")
    status = json.loads(status_json)

    assert output.startswith("NURTURE status=COMPLETE_WITH_SKIPS source=HOME_FOR_YOU")
    assert "home_sampled=12 home_new=3" in output
    assert "scroll_attempts=2 scrolls=2" in output
    assert not any(
        token in output
        for token in (
            "row_id=",
            "[ROW",
            "[SCROLL_ROW_PROGRESS]",
            "[SCROLL_GEOMETRY]",
            "scrollTop_before=",
            "clip_bottom_px=",
            "structural_reasons=",
        )
    )
    assert status["status"] == "COMPLETE"
    assert status["cycle_status"] == "COMPLETE_WITH_SKIPS"
    assert status["stop_reason"] == "FEED_END_REACHED"
    assert status["exit_code"] == 0
    assert status["checkpoint_runs"] == 7
    assert status["counters"] == {
        "detail_reads": 1,
        "home_new": 3,
        "home_sampled": 12,
        "scroll_attempts": 2,
        "scrolls": 2,
        "skipped_unverified": 1,
        "unique_verified_posts": 1,
    }
    assert all(
        value not in status_json + output
        for value in (
            "offline.operator",
            str(profile),
            "threads.com",
            "synthetic body",
        )
    )


def test_scheduled_uncheckpointed_cycle_is_stopped_with_nonzero_exit(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    root = tmp_path / "private-root"
    profile = tmp_path / "existing-profile"
    profile.mkdir()
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(root))
    assert (
        main(
            [
                "account",
                "add",
                "pilot",
                "--actor",
                "offline.operator",
                "--profile-dir",
                str(profile),
            ]
        )
        == 0
    )
    TermsStore(root).set("pilot", ("hiring",))
    capsys.readouterr()

    async def incomplete_run(_account, _store, **_kwargs):
        return SimpleNamespace(
            status="VIEWPORT_OBSERVATION_INCOMPLETE",
            stop_reason="VIEWPORT_OBSERVATION_INCOMPLETE",
            checkpoint_saved=False,
            ui_status="UNVERIFIED_LIVE_UI",
            home_sampled=1,
            home_new=1,
            home_relevant=0,
            home_irrelevant=0,
            home_unknown=0,
            home_relevant_ratio=None,
            duplicates=0,
            detail_reads=0,
            scroll_attempts=0,
            scrolls=0,
            skipped_unverified=0,
            checkpoint_runs=4,
        )

    monkeypatch.setattr(workflow, "run_browser_nurture", incomplete_run)
    exit_code = main(["run", "pilot", "--scheduled", "--max-runtime-seconds", "720"])
    captured = capsys.readouterr()
    status_json = (root / "operations" / "last_run.json").read_text(encoding="utf-8")
    status = json.loads(status_json)

    assert exit_code == 1
    assert status["status"] == "STOPPED"
    assert status["cycle_status"] == "VIEWPORT_OBSERVATION_INCOMPLETE"
    assert status["stop_reason"] == "VIEWPORT_OBSERVATION_INCOMPLETE"
    assert status["exit_code"] == 1
    assert status["checkpoint_runs"] == 4
    assert "NURTURE status=VIEWPORT_OBSERVATION_INCOMPLETE" in captured.out
    assert "STOP VIEWPORT_OBSERVATION_INCOMPLETE" in captured.err
    assert "pilot" not in status_json + captured.out + captured.err


def test_scheduled_missing_terms_hint_does_not_include_account_alias(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    root = tmp_path / "private-root"
    profile = tmp_path / "existing-profile"
    profile.mkdir()
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(root))
    assert (
        main(
            [
                "account",
                "add",
                "pilot",
                "--actor",
                "offline.operator",
                "--profile-dir",
                str(profile),
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["run", "pilot", "--scheduled", "--max-runtime-seconds", "720"]) == 1
    captured = capsys.readouterr()
    status_json = (root / "operations" / "last_run.json").read_text(encoding="utf-8")
    status = json.loads(status_json)

    assert "ERROR TERMS_NOT_CONFIGURED" in captured.err
    assert "Configure terms with:" not in captured.out + captured.err
    assert "pilot" not in status_json + captured.out + captured.err
    assert status["status"] == "FAILED"
    assert status["stop_reason"] == "TERMS_NOT_CONFIGURED"
    assert status["exit_code"] == 1


def test_scheduled_runs_require_runtime_and_reject_text_opt_ins(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    root = tmp_path / "private-root"
    profile = tmp_path / "existing-profile"
    profile.mkdir()
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(root))
    assert (
        main(
            [
                "account",
                "add",
                "pilot",
                "--actor",
                "offline.operator",
                "--profile-dir",
                str(profile),
            ]
        )
        == 0
    )
    TermsStore(root).set("pilot", ("hiring",))
    capsys.readouterr()

    assert main(["run", "pilot", "--scheduled"]) == 1
    assert capsys.readouterr().err == "ERROR SCHEDULED_RUNTIME_REQUIRED\n"
    status = json.loads((root / "operations" / "last_run.json").read_text(encoding="utf-8"))
    assert status["status"] == "FAILED"
    assert status["stop_reason"] == "SCHEDULED_RUNTIME_REQUIRED"

    assert main(["run", "pilot", "--scheduled", "--detail"]) == 1
    assert capsys.readouterr().err == "ERROR SCHEDULED_TEXT_OUTPUT_INVALID\n"
