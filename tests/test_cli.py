from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import tomllib
from pathlib import Path
from types import SimpleNamespace

from threads_browser import cli, workflow
from threads_browser.cli import main
from threads_browser.reader import UiReadError
from threads_browser.storage import AccountStore, TermsStore, profile_lock


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


def _verified_local_account(data_root: Path, alias: str = "local1"):
    accounts = AccountStore(data_root)
    account = accounts.add(alias)
    accounts.bind_actor(account, "offline.operator")
    return accounts.get(alias)


def _fake_nurture_result():
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


def test_project_registers_only_the_short_tb_script() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["scripts"] == {"tb": "threads_browser.cli:main"}


def test_module_runner_keeps_legacy_command_tree_available() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "threads_browser", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    assert "account" in completed.stdout
    assert "nurture" in completed.stdout


def test_short_add_login_and_ls_commands(tmp_path: Path, monkeypatch, capsys) -> None:
    root = tmp_path / "private-root"
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(root))
    assert main(["add", "local1"]) == 0
    assert capsys.readouterr().out == "ACCOUNT status=REGISTERED\n"

    login_calls = {}

    async def fake_login(alias, *, data_root, status_callback):
        login_calls.update(alias=alias, data_root=data_root, callback=status_callback)
        status_callback("BROWSER_OPEN")
        return SimpleNamespace(status="CLOSED")

    monkeypatch.setattr(cli, "open_account_login", fake_login)
    assert main(["login", "local1"]) == 0
    assert login_calls["alias"] == "local1"
    assert login_calls["data_root"] == root.resolve()
    assert "BROWSER status=OPEN" in capsys.readouterr().out

    AccountStore(root).bind_actor(AccountStore(root).get("local1"), "offline.operator")
    assert main(["ls"]) == 0
    output = capsys.readouterr().out
    assert output == "local1 VERIFIED\n"
    assert str(root / "profiles") not in output
    assert "offline.operator" not in output


def test_short_terms_command_persists_normalized_terms_separately(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    root = tmp_path / "private-root"
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(root))
    _verified_local_account(root)
    account_path = root / "accounts" / "local1.json"
    account_before = account_path.read_bytes()

    assert main(["terms", "local1", "Hiring", "đang tuyển", " hiring "]) == 0
    assert capsys.readouterr().out == "TERMS status=SAVED count=2\n"
    assert TermsStore(root).get("local1") == ("hiring", "đang tuyển")
    assert (root / "preferences" / "terms" / "local1.json").is_file()
    assert account_path.read_bytes() == account_before

    assert main(["terms", "local1"]) == 0
    assert capsys.readouterr().out == "hiring\nđang tuyển\n"
    assert main(["terms", "local1", "--clear"]) == 0
    assert capsys.readouterr().out == "TERMS status=CLEARED\n"
    assert TermsStore(root).get("local1") == ()


def test_short_run_uses_saved_terms_and_documented_default_budgets(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    root = tmp_path / "private-root"
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(root))
    _verified_local_account(root)
    TermsStore(root).set("local1", ("Hiring", "đang tuyển"))
    calls = {}

    async def fake_run(_account, _store, **kwargs):
        calls.update(kwargs)
        return _fake_nurture_result()

    monkeypatch.setattr(workflow, "run_browser_nurture", fake_run)
    assert main(["run", "local1"]) == 0
    output = capsys.readouterr().out
    assert "NURTURE status=NO_VERIFIED_POSTS_IN_BUDGET" in output
    assert calls["terms"] == ("hiring", "đang tuyển")
    assert calls["max_posts"] == 20
    assert calls["max_scrolls"] == 8
    assert calls["max_total_posts"] == 40


def test_one_time_run_terms_override_without_changing_saved_terms(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    root = tmp_path / "private-root"
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(root))
    _verified_local_account(root)
    calls = {}

    async def fake_run(_account, _store, **kwargs):
        calls.update(kwargs)
        return _fake_nurture_result()

    monkeypatch.setattr(workflow, "run_browser_nurture", fake_run)
    assert main(["run", "local1", "-t", "Hiring"]) == 0
    capsys.readouterr()
    assert calls["terms"] == ("Hiring",)
    assert TermsStore(root).get("local1") == ()

    TermsStore(root).set("local1", ("saved term",))
    assert main(
        ["run", "local1", "--term", "remote", "-t", "engineer", "--max-scrolls", "12"]
    ) == 0
    capsys.readouterr()
    assert calls["terms"] == ("remote", "engineer")
    assert calls["max_posts"] == 20
    assert calls["max_scrolls"] == 12
    assert calls["max_total_posts"] == 40
    assert TermsStore(root).get("local1") == ("saved term",)

    assert main(
        ["run", "local1", "--max-posts", "2", "--max-total-posts", "15", "-t", "fresh"]
    ) == 0
    capsys.readouterr()
    assert calls["terms"] == ("fresh",)
    assert calls["max_posts"] == 2
    assert calls["max_total_posts"] == 15
    assert TermsStore(root).get("local1") == ("saved term",)


def test_short_run_reports_missing_account_actor_and_terms(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    root = tmp_path / "private-root"
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(root))
    assert main(["run", "missing"]) == 1
    assert capsys.readouterr().err == "ERROR ACCOUNT_NOT_FOUND\n"

    assert main(["add", "local1"]) == 0
    capsys.readouterr()
    assert main(["run", "local1"]) == 1
    assert capsys.readouterr().err == "ERROR ACCOUNT_NOT_VERIFIED\n"

    AccountStore(root).bind_actor(AccountStore(root).get("local1"), "offline.operator")
    assert main(["run", "local1"]) == 1
    error = capsys.readouterr().err
    assert "ERROR TERMS_NOT_CONFIGURED" in error
    assert 'tb terms local1 "hiring"' in error


def test_short_run_login_required_and_profile_lock_errors_are_safe(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    root = tmp_path / "private-root"
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(root))
    account = _verified_local_account(root)
    TermsStore(root).set("local1", ("hiring",))

    async def login_required(*_args, **_kwargs):
        raise UiReadError("LOGIN_REQUIRED")

    monkeypatch.setattr(workflow, "run_browser_nurture", login_required)
    assert main(["run", "local1"]) == 1
    error = capsys.readouterr().err
    assert "STOP LOGIN_REQUIRED" in error
    assert "tb login local1" in error

    lock = profile_lock(root, account)
    lock.acquire()
    try:
        assert main(["run", "local1"]) == 1
        assert capsys.readouterr().err == "ERROR ACCOUNT_BUSY\n"
    finally:
        lock.release()
