from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from tests.fixtures.cli import fake_nurture_result
from threads_browser import cli, workflow
from threads_browser.cli import main
from threads_browser.reader import UiReadError


def test_show_post_text_requires_tty_before_data_or_browser_startup(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(cli, "_has_interactive_terminal", lambda: False)
    monkeypatch.setattr(
        cli,
        "resolve_data_root",
        lambda: (_ for _ in ()).throw(AssertionError("data root accessed")),
    )

    result = main(
        [
            "nurture",
            "run",
            "offline",
            "--headed",
            "--show-post-text",
            "--max-posts",
            "1",
            "--term",
            "hiring",
        ]
    )

    assert result == 1
    assert capsys.readouterr().err == "ERROR POST_TEXT_TTY_REQUIRED\n"


def test_detail_requires_tty_before_data_or_browser_startup(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_has_interactive_terminal", lambda: False)
    monkeypatch.setattr(
        cli,
        "resolve_data_root",
        lambda: (_ for _ in ()).throw(AssertionError("data root accessed")),
    )

    result = main(["run", "offline", "--detail"])

    assert result == 1
    assert capsys.readouterr().err == "ERROR DETAIL_TTY_REQUIRED\n"


def test_detail_cli_supports_short_and_legacy_run_without_duplicate_opt_in_text(
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
    monkeypatch.setattr(cli, "_has_interactive_terminal", lambda: True)
    calls: dict[str, object] = {}

    async def fake_run(_account, _store, **kwargs):
        calls.update(kwargs)
        return SimpleNamespace(
            status="NO_VERIFIED_POSTS_IN_BUDGET",
            ui_status="UNVERIFIED_LIVE_UI",
            home_sampled=0,
            home_new=0,
            home_relevant=0,
            home_irrelevant=0,
            home_unknown=0,
            home_relevant_ratio=None,
            duplicates=0,
            detail_reads=0,
            scroll_attempts=0,
            scrolls=0,
            skipped_unverified=0,
            checkpoint_runs=0,
        )

    monkeypatch.setattr(workflow, "run_browser_nurture", fake_run)
    commands = (
        ["run", "pilot"],
        ["nurture", "run", "pilot", "--headed"],
    )
    for command in commands:
        calls.clear()
        result = main(
            [
                *command,
                "--detail",
                "--show-post-text",
                "--show-observed-text",
                "--max-posts",
                "1",
                "--term",
                "hiring",
            ]
        )
        captured = capsys.readouterr()

        assert result == 0
        assert calls["detail"] is True
        assert calls["show_post_text"] is True
        assert calls["show_observed_text"] is True
        assert captured.err.count("DETAIL_LOCAL_ONLY") == 1
        assert "POST_TEXT_LOCAL_ONLY" not in captured.err
        assert "OBSERVED_TEXT_LOCAL_ONLY" not in captured.err
        assert "do not capture or share this output" in captured.err

    async def failed_run(_account, _store, **_kwargs):
        raise UiReadError("RETURN_CONTEXT_CHANGED")

    monkeypatch.setattr(workflow, "run_browser_nurture", failed_run)
    result = main(["run", "pilot", "--detail", "--term", "hiring"])
    failed = capsys.readouterr()

    assert result == 1
    assert failed.err.count("[CHECKPOINT] status=NOT_SAVED reason=RETURN_CONTEXT_CHANGED") == 1
    assert failed.err.count("[STOP] code=RETURN_CONTEXT_CHANGED") == 1
    assert "STOP RETURN_CONTEXT_CHANGED" not in failed.err


def test_show_post_text_cli_passes_opt_in_only_after_tty_check(
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
    monkeypatch.setattr(cli, "_has_interactive_terminal", lambda: True)
    calls: dict[str, object] = {}

    async def fake_run(_account, _store, **kwargs):
        calls.update(kwargs)
        return SimpleNamespace(
            status="NO_VERIFIED_POSTS_IN_BUDGET",
            ui_status="UNVERIFIED_LIVE_UI",
            home_sampled=0,
            home_new=0,
            home_relevant=0,
            home_irrelevant=0,
            home_unknown=0,
            home_relevant_ratio=None,
            duplicates=0,
            detail_reads=0,
            scroll_attempts=0,
            scrolls=0,
            skipped_unverified=0,
            checkpoint_runs=0,
        )

    monkeypatch.setattr(workflow, "run_browser_nurture", fake_run)
    result = main(
        [
            "nurture",
            "run",
            "pilot",
            "--headed",
            "--show-post-text",
            "--max-posts",
            "1",
            "--term",
            "hiring",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert calls["show_post_text"] is True
    assert calls["detail"] is False
    assert "POST_TEXT_LOCAL_ONLY" in captured.err
    assert "do not paste preview text into PRs or chats" in captured.err


def test_show_observed_text_requires_tty_before_data_or_browser_startup(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(cli, "_has_interactive_terminal", lambda: False)
    monkeypatch.setattr(
        cli,
        "resolve_data_root",
        lambda: (_ for _ in ()).throw(AssertionError("data root accessed")),
    )

    result = main(
        [
            "nurture",
            "run",
            "offline",
            "--headed",
            "--show-observed-text",
            "--max-posts",
            "1",
            "--term",
            "hiring",
        ]
    )

    assert result == 1
    assert capsys.readouterr().err == "ERROR OBSERVED_TEXT_TTY_REQUIRED\n"


def test_show_observed_text_is_a_separate_tty_opt_in(
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
    monkeypatch.setattr(cli, "_has_interactive_terminal", lambda: True)
    calls: dict[str, object] = {}

    async def fake_run(_account, _store, **kwargs):
        calls.update(kwargs)
        return SimpleNamespace(
            status="OBSERVED_UNVERIFIED_ONLY",
            ui_status="UNVERIFIED_LIVE_UI",
            home_sampled=0,
            home_new=0,
            home_relevant=0,
            home_irrelevant=0,
            home_unknown=0,
            home_relevant_ratio=None,
            duplicates=0,
            detail_reads=0,
            scroll_attempts=0,
            scrolls=0,
            skipped_unverified=0,
            checkpoint_runs=0,
            observed_rows=1,
            body_extracted=1,
            provenance_verified=0,
            classified=1,
            observation_unknown=0,
            unverified_relevant=1,
            skipped_rows=0,
            viewport_coverage_complete=True,
        )

    monkeypatch.setattr(workflow, "run_browser_nurture", fake_run)
    result = main(
        [
            "nurture",
            "run",
            "pilot",
            "--headed",
            "--show-observed-text",
            "--max-posts",
            "1",
            "--term",
            "hiring",
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert calls["show_observed_text"] is True
    assert "OBSERVED_TEXT_LOCAL_ONLY" in captured.err
    assert "identity is unverified" in captured.err
    assert "observed_rows=1" in captured.out


def test_nurture_summary_includes_only_safe_aggregate_coverage_diagnostics() -> None:
    result = SimpleNamespace(
        **vars(fake_nurture_result()),
        unresolved_gaps=1,
        read_in_progress=1,
        unresolved_observations=0,
        unrepresented_gaps=0,
        stranded_body_ranges=1,
        lookahead_rows=3,
        gap_sources_total=1,
        gap_category="READ_IN_PROGRESS",
    )

    output = cli._format_result(result)

    assert "unresolved_gaps=1" in output
    assert "read_in_progress=1" in output
    assert "unresolved_observations=0" in output
    assert "unrepresented_gaps=0" in output
    assert "stranded_body_ranges=1" in output
    assert "lookahead_rows=3" in output
    assert "gap_sources_total=1" in output
    assert "gap_category=READ_IN_PROGRESS" in output
    assert "private-" not in output
