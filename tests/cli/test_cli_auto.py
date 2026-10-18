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
    assert "UAT " not in captured.out
    assert "FEED_END_REACHED" in captured.out
    assert captured.err == ""
    assert not (data_root / "preferences" / "terms").exists()
    assert not (data_root / "operations" / "last_run.json").exists()


def test_auto_cli_uat_diagnostics_require_a_cycle_limit(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(tmp_path / "private-root"))

    assert main(["auto", "local1", "--uat-diagnostics"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR UAT_DIAGNOSTICS_REQUIRES_CYCLE_LIMIT" in captured.err


def test_auto_cli_opt_in_diagnostics_print_safe_phase_summaries(
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
    received_diagnostics: list[object] = []

    async def fake_cycle(run_account, store, *, uat_diagnostics):
        received_diagnostics.append(uat_diagnostics)
        return SimpleNamespace(
            status="SESSION_COMPLETE",
            stop_reason="FEED_END_REACHED",
            home_sampled=3,
            home_new=1,
            home_relevant=1,
            home_unknown=0,
            detail_reads=1,
            scrolls=2,
            scroll_attempts=2,
            viewport_observations=2,
            observed_rows=3,
            provenance_verified=3,
            body_extracted=3,
            observation_unknown=0,
            skipped_rows=0,
            viewport_coverage_complete=True,
            unique_provenance_identities=3,
            unique_verified_posts=1,
            unique_deferred_posts=0,
            unresolved_gaps=0,
            read_in_progress=0,
            unresolved_observations=0,
            unrepresented_gaps=0,
            stranded_body_ranges=0,
            lookahead_rows=0,
            gap_category="NONE",
            checkpoint_saved=True,
            checkpoint_runs=1,
            new_fingerprints=("a" * 64,),
            body="private post body",
            url="https://threads.example/@private/post/secret",
        )

    monkeypatch.setattr(workflow, "run_browser_nurture_auto_cycle", fake_cycle)

    assert main(["auto", "local1", "--max-cycles", "1", "--uat-diagnostics"]) == 0

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert len(received_diagnostics) == 1
    assert "UAT attempt=1 phase=ATTEMPT status=STARTED" in output
    assert "phase=HOME status=OBSERVED" in output
    assert "phase=FEED status=OBSERVED" in output
    assert "phase=COVERAGE status=COMPLETE" in output
    assert "phase=CHECKPOINT status=SAVED" in output
    assert "private post body" not in output
    assert "threads.example" not in output
    assert "offline.operator" not in output
    assert str(profile) not in output
    assert captured.err == ""
    assert account.alias == "local1"
