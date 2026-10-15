from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

from threads_browser import cli, workflow
from threads_browser.cli import main


def test_account_cli_keeps_profile_reference_and_actor_out_of_output(
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
                "@offline.operator",
                "--profile-dir",
                str(profile),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert output == "ACCOUNT status=REGISTERED\n"
    assert str(profile) not in output
    assert "offline.operator" not in output


def test_account_cli_create_profile_registers_a_new_directory(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    root = tmp_path / "private-root"
    profile = tmp_path / "profiles" / "pilot"
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(root))

    result = main(
        [
            "account",
            "add",
            "pilot",
            "--actor",
            "@offline.operator",
            "--profile-dir",
            str(profile),
            "--create-profile",
        ]
    )

    assert result == 0
    assert profile.is_dir()
    assert capsys.readouterr().out == "ACCOUNT status=REGISTERED\n"


def test_account_cli_creates_a_local_unverified_account(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    root = tmp_path / "private-root"
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(root))

    assert main(["account", "add", "local1"]) == 0

    assert capsys.readouterr().out == "ACCOUNT status=REGISTERED\n"
    assert (root / "profiles" / "local1").is_dir()
    account = json.loads((root / "accounts" / "local1.json").read_text(encoding="utf-8"))
    assert account["expected_actor"] is None
    assert account["schema_version"] == 2


def test_nurture_rejects_an_unverified_local_account_before_browser_startup(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    root = tmp_path / "private-root"
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(root))
    assert main(["account", "add", "local1"]) == 0
    capsys.readouterr()

    result = main(
        [
            "nurture",
            "run",
            "local1",
            "--headed",
            "--max-posts",
            "2",
            "--max-scrolls",
            "1",
            "--max-total-posts",
            "5",
            "--term",
            "hiring",
        ]
    )

    assert result == 1
    assert capsys.readouterr().err == "ERROR ACCOUNT_NOT_VERIFIED\n"
    assert not (root / "nurture").exists()


def test_nurture_cli_requires_explicit_headed_mode(tmp_path: Path, monkeypatch, capsys) -> None:
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

    code = main(
        [
            "nurture",
            "run",
            "pilot",
            "--max-posts",
            "5",
            "--term",
            "hiring",
        ]
    )
    assert code == 1
    assert capsys.readouterr().err == "ERROR HEADED_REQUIRED\n"


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


def test_attended_readiness_prompt_keeps_async_loop_responsive(monkeypatch) -> None:
    input_started = threading.Event()
    release_input = threading.Event()
    calls = 0

    def blocking_input(_: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            input_started.set()
            release_input.wait(timeout=1)
            return ""
        if calls == 2:
            return "q"
        raise EOFError

    monkeypatch.setattr("builtins.input", blocking_input)

    async def exercise() -> None:
        prompt = asyncio.create_task(cli._prompt_readiness_extension("DETAIL", 2))
        try:
            await asyncio.sleep(0)
            assert await asyncio.to_thread(input_started.wait, 1)
            assert not prompt.done()

            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_soon(heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), timeout=0.5)
        finally:
            release_input.set()

        assert await prompt is True
        assert await cli._prompt_readiness_extension("DETAIL", 3) is False
        assert await cli._prompt_readiness_extension("DETAIL", 4) is False

    asyncio.run(exercise())
