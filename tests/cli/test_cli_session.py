from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from threads_browser import cli, workflow
from threads_browser.cli import main
from threads_browser.storage import TermsStore


def _configure_local_account(root: Path) -> Path:
    profile = root / "synthetic-profile"
    profile.mkdir(parents=True)
    return profile


def _result(*, status: str, stop_reason: str, checkpoint_saved: bool) -> SimpleNamespace:
    return SimpleNamespace(
        status=status,
        stop_reason=stop_reason,
        ui_status="UNVERIFIED_LIVE_UI",
        home_sampled=2,
        home_new=2,
        home_relevant=1,
        home_irrelevant=1,
        home_unknown=0,
        home_relevant_ratio=0.5,
        duplicates=0,
        detail_reads=1,
        scroll_attempts=1,
        scrolls=1,
        skipped_unverified=0,
        checkpoint_runs=1,
        checkpoint_saved=checkpoint_saved,
    )


def _install_account(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    profile = _configure_local_account(root)
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(root))
    assert (
        main(
            [
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
    return profile


def test_session_cli_forwards_bounded_caps_and_has_no_scheduled_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "private-root"
    profile = _install_account(root, monkeypatch)
    monkeypatch.setattr(cli, "_has_interactive_terminal", lambda: True)
    calls: dict[str, object] = {}

    async def fake_session(_account, _store, **kwargs):
        calls.update(kwargs)
        return _result(
            status="SESSION_BUDGET_EXHAUSTED",
            stop_reason="MAX_DETAIL_READS",
            checkpoint_saved=True,
        )

    monkeypatch.setattr(workflow, "run_browser_nurture_session", fake_session)
    result = main(
        [
            "session",
            "pilot",
            "--max-scrolls",
            "6",
            "--max-total-posts",
            "80",
            "--max-detail-reads",
            "5",
            "--max-runtime-seconds",
            "600",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert calls["max_posts"] == 20
    assert calls["max_scrolls"] == 6
    assert calls["max_total_posts"] == 80
    assert calls["max_detail_reads"] == 5
    assert calls["max_runtime_seconds"] == 600
    assert calls["terms"] == ("hiring",)
    assert calls["readiness_prompt"] is not None
    assert "SESSION_BUDGET_EXHAUSTED" in captured.out
    assert "checkpoint_saved=true" in captured.out
    assert "offline.operator" not in captured.out + captured.err
    assert str(profile) not in captured.out + captured.err
    assert not (root / "operations" / "last_run.json").exists()

    with pytest.raises(SystemExit):
        cli._parser().parse_args(
            [
                "session",
                "pilot",
                "--max-scrolls",
                "1",
                "--max-total-posts",
                "1",
                "--max-detail-reads",
                "1",
                "--max-runtime-seconds",
                "60",
                "--scheduled",
            ]
        )


def test_session_cli_fails_closed_when_only_an_earlier_boundary_was_saved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "private-root"
    _install_account(root, monkeypatch)
    monkeypatch.setattr(cli, "_has_interactive_terminal", lambda: True)

    async def partial_session(_account, _store, **_kwargs):
        return _result(
            status="SESSION_COVERAGE_INCOMPLETE",
            stop_reason="COVERAGE_INCOMPLETE",
            checkpoint_saved=True,
        )

    monkeypatch.setattr(workflow, "run_browser_nurture_session", partial_session)
    result = main(
        [
            "session",
            "pilot",
            "--max-scrolls",
            "1",
            "--max-total-posts",
            "10",
            "--max-detail-reads",
            "2",
            "--max-runtime-seconds",
            "60",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "checkpoint_saved=true" in captured.out
    assert "STOP COVERAGE_INCOMPLETE" in captured.err


@pytest.mark.parametrize("runtime", ("10", "3601", "nan", "inf"))
def test_session_cli_requires_a_finite_runtime_inside_the_product_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    runtime: str,
) -> None:
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(tmp_path / "private-root"))
    monkeypatch.setattr(cli, "_has_interactive_terminal", lambda: True)
    assert (
        main(
            [
                "session",
                "missing",
                "--max-scrolls",
                "1",
                "--max-total-posts",
                "1",
                "--max-detail-reads",
                "1",
                "--max-runtime-seconds",
                runtime,
            ]
        )
        == 1
    )
    assert capsys.readouterr().err == "ERROR RUNTIME_LIMIT_INVALID\n"
