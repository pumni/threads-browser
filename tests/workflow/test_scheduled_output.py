from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Route

from tests.fixtures.browser import (
    _is_offline_profile_url,
    _offline_playwright_manager,
    _seed_pending_checkpoint,
)
from threads_browser import workflow
from threads_browser.browser_ui.scrolling import (
    NativeWheelScrollEngine,
)
from threads_browser.cli import main
from threads_browser.reader import (
    HOME_URL,
    ThreadsHomeReader,
)
from threads_browser.storage import (
    AccountStore,
    CheckpointStore,
)


def test_scheduled_offline_chromium_cli_suppresses_row_and_scroll_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    terms = ("hiring",)
    long_body = " ".join(["Synthetic weather remains unresolved."] * 120)
    cards = "".join(
        f"""<article class="card"><a href="/@candidate/post/verified-{index}">
<time>now</time></a><a href="/@candidate">candidate</a>
<p class="body">Synthetic weather update {index}.</p></article>"""
        for index in range(12)
    )
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; }}
nav {{ height: 36px; }}
main {{ display: block; }}
.card {{ position: relative; height: 288px; }}
.body {{ position: absolute; top: 108px; left: 12px; line-height: 24px; }}
.incomplete {{ height: 288px; }}
.incomplete .body {{ top: 80px; width: 220px; height: 24px; overflow: hidden; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article class="card incomplete">
<a href="/@candidate/post/incomplete"><time>now</time></a>
<a href="/@candidate">candidate</a><p class="body">{long_body}</p></article>
{cards}</main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    policy, stale_fingerprint, _ = _seed_pending_checkpoint(
        store,
        account,
        terms,
        "https://www.threads.com/@candidate/post/absent-pending",
    )
    checkpoint_before = store.load(account.account_id, policy)
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(data_root))

    async def respond(route: Route) -> None:
        if route.request.url == HOME_URL:
            body = feed_html
        elif _is_offline_profile_url(route.request.url):
            body = "<!doctype html><html><body><h1>Local profile</h1></body></html>"
        else:
            await route.abort()
            return
        await route.fulfill(status=200, content_type="text/html", body=body)

    monkeypatch.setattr(
        workflow,
        "async_playwright",
        lambda: _offline_playwright_manager(respond, headless=False),
    )
    exit_code = main(
        [
            "run",
            "offline",
            "--scheduled",
            "--max-posts",
            "20",
            "--max-scrolls",
            "1",
            "--max-total-posts",
            "40",
            "--max-runtime-seconds",
            "60",
            "--term",
            "hiring",
        ]
    )
    captured = capsys.readouterr()
    output = captured.out + captured.err
    summary = next((line for line in captured.out.splitlines() if line.startswith("NURTURE ")), "")
    status_json = (data_root / "operations" / "last_run.json").read_text(encoding="utf-8")
    status = json.loads(status_json)
    checkpoint_after = store.load(account.account_id, policy)

    assert exit_code == 1
    assert summary.startswith("NURTURE status=RELEVANT_POST_NOT_CURRENT ")
    assert "scroll_attempts=1 scrolls=1" in summary
    assert "gap_category=READ_IN_PROGRESS" in summary
    assert "STOP RELEVANT_POST_NOT_CURRENT" in captured.err
    assert status["status"] == "STOPPED"
    assert status["cycle_status"] == "RELEVANT_POST_NOT_CURRENT"
    assert status["stop_reason"] == "RELEVANT_POST_NOT_CURRENT"
    assert status["exit_code"] == 1
    assert status["counters"]["scrolls"] == 1
    assert checkpoint_after == checkpoint_before
    assert stale_fingerprint in checkpoint_after.pending_read_fingerprints
    forbidden_tokens = (
        "row_id=",
        "[ROW",
        "[SCROLL_ROW_PROGRESS]",
        "[SCROLL_GEOMETRY]",
        "[DETAIL_STATE]",
        "scrollTop_before=",
        "clip_bottom_px=",
        "text_rect_count=",
        "structural_reasons=",
        "[URL_STRUCTURE]",
        str(profile),
        actor,
        long_body,
        "https://www.threads.com/",
    )
    assert not [token for token in forbidden_tokens if token in output]
    assert output.splitlines()
    assert all(
        line.startswith(("NURTURE ", "STOP ", "ERROR ", "WARNING ")) for line in output.splitlines()
    )
    assert all(
        value not in status_json
        for value in (str(profile), actor, long_body, "https://www.threads.com/")
    )
    with store.profile_lock(account):
        pass
    assert store.load(account.account_id, policy) == checkpoint_before

    interactive_code = main(
        [
            "run",
            "offline",
            "--max-posts",
            "20",
            "--max-scrolls",
            "1",
            "--max-total-posts",
            "40",
            "--max-runtime-seconds",
            "60",
            "--term",
            "hiring",
        ]
    )
    interactive_capture = capsys.readouterr()
    interactive_output = interactive_capture.out
    safe_diagnostics = [
        line
        for line in (interactive_capture.out + interactive_capture.err).splitlines()
        if line.startswith(("NURTURE ", "STOP ", "ERROR ", "WARNING ", "[FAILURE_DIAGNOSTIC]"))
    ]
    assert interactive_code == 0, " ".join(safe_diagnostics)
    assert "[SCROLL_ROW_PROGRESS]" in interactive_output
    assert "row_id=" in interactive_output
    assert "[SCROLL_GEOMETRY]" in interactive_output


def test_scheduled_offline_chromium_cli_suppresses_failure_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    AccountStore(data_root).add("offline", actor, profile)
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(data_root))

    async def respond(route: Route) -> None:
        if route.request.url == HOME_URL:
            body = "<!doctype html><html><body><main>Offline fixture</main></body></html>"
        elif _is_offline_profile_url(route.request.url):
            body = "<!doctype html><html><body><h1>Local profile</h1></body></html>"
        else:
            await route.abort()
            return
        await route.fulfill(status=200, content_type="text/html", body=body)

    monkeypatch.setattr(
        workflow,
        "async_playwright",
        lambda: _offline_playwright_manager(respond),
    )

    async def fail_during_home_open(reader: ThreadsHomeReader) -> None:
        await reader._page.goto(HOME_URL, wait_until="domcontentloaded")
        raise PlaywrightError("synthetic controlled failure")

    monkeypatch.setattr(ThreadsHomeReader, "open_home", fail_during_home_open)
    exit_code = main(
        [
            "run",
            "offline",
            "--scheduled",
            "--term",
            "hiring",
            "--max-posts",
            "1",
            "--max-scrolls",
            "0",
            "--max-runtime-seconds",
            "30",
        ]
    )
    captured = capsys.readouterr()
    status = json.loads((data_root / "operations" / "last_run.json").read_text(encoding="utf-8"))

    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "STOP UI_READ_UNVERIFIED\n"
    assert status["status"] == "FAILED"
    assert status["stop_reason"] == "UI_READ_UNVERIFIED"
    assert status["exit_code"] == 1
    assert "FAILURE_DIAGNOSTIC" not in captured.out + captured.err


def test_scheduled_offline_chromium_cli_suppresses_reader_scroll_failure_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    cards = "".join(
        f"""<article class="card"><a href="/@candidate/post/scroll-failure-{index}">
<time>now</time></a><a href="/@candidate">candidate</a>
<p class="body">Synthetic weather update {index}.</p></article>"""
        for index in range(14)
    )
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; }}
nav {{ height: 36px; }}
main {{ display: block; }}
.card {{ position: relative; height: 288px; }}
.body {{ position: absolute; top: 108px; left: 12px; line-height: 24px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main>{cards}</main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    AccountStore(data_root).add("offline", actor, profile)
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(data_root))
    wheel_calls = 0

    async def respond(route: Route) -> None:
        if route.request.url == HOME_URL:
            body = feed_html
        elif _is_offline_profile_url(route.request.url):
            body = "<!doctype html><html><body><h1>Local profile</h1></body></html>"
        else:
            await route.abort()
            return
        await route.fulfill(status=200, content_type="text/html", body=body)

    async def fail_scroll(self, *_args) -> None:
        nonlocal wheel_calls
        wheel_calls += 1
        raise PlaywrightError("synthetic controlled scroll failure")

    monkeypatch.setattr(
        workflow,
        "async_playwright",
        lambda: _offline_playwright_manager(respond),
    )
    monkeypatch.setattr(NativeWheelScrollEngine, "advance", fail_scroll)
    exit_code = main(
        [
            "run",
            "offline",
            "--scheduled",
            "--term",
            "hiring",
            "--max-posts",
            "20",
            "--max-scrolls",
            "1",
            "--max-total-posts",
            "40",
            "--max-runtime-seconds",
            "60",
        ]
    )
    captured = capsys.readouterr()
    status = json.loads((data_root / "operations" / "last_run.json").read_text(encoding="utf-8"))

    assert wheel_calls == 1
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "STOP UI_READ_UNVERIFIED\n"
    assert "FAILURE_DIAGNOSTIC" not in captured.out + captured.err
    assert status["status"] == "FAILED"
    assert status["stop_reason"] == "UI_READ_UNVERIFIED"
    assert status["exit_code"] == 1
