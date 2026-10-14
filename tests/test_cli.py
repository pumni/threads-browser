from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from threads_browser import cli
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
