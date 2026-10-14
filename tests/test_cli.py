from __future__ import annotations

from pathlib import Path

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
