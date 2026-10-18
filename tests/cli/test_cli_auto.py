from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from threads_browser import cli, workflow
from threads_browser.cli import main
from threads_browser.storage import AccountStore


def test_auto_cli_runs_without_terms_or_tty_and_prints_aggregate_only(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    data_root = tmp_path / "private-root"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(data_root))
    account = AccountStore(data_root).add("local1", "offline.operator", profile)
    monkeypatch.setattr(cli, "_has_interactive_terminal", lambda: False)
    calls: list[tuple[object, object]] = []

    async def fake_cycle(run_account, store):
        calls.append((run_account, store))
        return SimpleNamespace(
            status="SESSION_COMPLETE",
            stop_reason="FEED_END_REACHED",
            home_sampled=3,
            home_new=1,
            home_relevant=1,
            detail_reads=1,
            scrolls=2,
            checkpoint_saved=True,
            new_fingerprints=("a" * 64,),
        )

    monkeypatch.setattr(workflow, "run_browser_nurture_auto_cycle", fake_cycle)

    assert main(["auto", "local1", "--max-cycles", "1"]) == 0

    captured = capsys.readouterr()
    assert len(calls) == 1
    assert calls[0][0] == account
    assert "AUTO cycle=1 observed=3 new=1 eligible=1 detail=1 scrolls=2" in captured.out
    assert "offline.operator" not in captured.out + captured.err
    assert str(profile) not in captured.out + captured.err
    assert "FEED_END_REACHED" in captured.out
    assert captured.err == ""
    assert not (data_root / "preferences" / "terms").exists()
    assert not (data_root / "operations" / "last_run.json").exists()
