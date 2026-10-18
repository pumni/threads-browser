from __future__ import annotations

from types import SimpleNamespace

from threads_browser.nurture.uat_diagnostics import AutoUatDiagnostics


def test_uat_diagnostic_reporter_redacts_untrusted_values_and_bounds_counts() -> None:
    lines: list[str] = []
    diagnostics = AutoUatDiagnostics(emit=lines.append)
    diagnostics.begin_attempt()
    diagnostics.report(
        "https://threads.example/@private",
        "post text and cookies",
        viewport=10**20,
        code="https://threads.example/@private/post/secret",
        counts={"observed_rows": 10**20, "private_handle": "private.operator"},
        gap_category="private post text",
    )

    output = "\n".join(lines)
    assert "attempt=1 phase=ATTEMPT status=STARTED" in output
    assert "phase=STOP status=FAILED viewport=1000000 code=UNKNOWN" in output
    assert "observed_rows=1000000" in output
    assert "private_handle" not in output
    assert "private.operator" not in output
    assert "threads.example" not in output
    assert "cookies" not in output
    assert "private post text" not in output


def test_uat_diagnostic_result_reports_only_phase_counts() -> None:
    lines: list[str] = []
    diagnostics = AutoUatDiagnostics(emit=lines.append)
    diagnostics.begin_attempt()
    diagnostics.report_result(
        SimpleNamespace(
            status="SESSION_COVERAGE_INCOMPLETE",
            home_sampled=4,
            home_relevant=1,
            home_unknown=2,
            viewport_observations=3,
            scroll_attempts=2,
            scrolls=1,
            observed_rows=4,
            provenance_verified=3,
            body_extracted=2,
            observation_unknown=1,
            skipped_rows=1,
            detail_reads=1,
            viewport_coverage_complete=False,
            unique_provenance_identities=3,
            unique_verified_posts=1,
            unique_deferred_posts=1,
            unresolved_gaps=2,
            read_in_progress=1,
            unresolved_observations=1,
            unrepresented_gaps=0,
            stranded_body_ranges=1,
            lookahead_rows=1,
            gap_category="MULTIPLE",
            checkpoint_saved=True,
            checkpoint_runs=2,
            body="private post body",
            url="https://threads.example/@private/post/secret",
            actor="private.operator",
        )
    )

    output = "\n".join(lines)
    assert "phase=HOME status=OBSERVED relevant=1 sampled=4 unknown=2" in output
    assert "phase=FEED status=OBSERVED body_read=2" in output
    assert "phase=COVERAGE status=INCOMPLETE" in output
    assert "gap_category=MULTIPLE" in output
    assert "phase=CHECKPOINT status=SAVED checkpoint_runs=2" in output
    assert "phase=ATTEMPT status=RESULT code=SESSION_COVERAGE_INCOMPLETE" in output
    assert "private post body" not in output
    assert "threads.example" not in output
    assert "private.operator" not in output
