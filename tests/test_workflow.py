from __future__ import annotations

import asyncio
import json
import re
import tempfile
import time
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Route
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright as real_async_playwright

from threads_browser import reader as reader_module
from threads_browser import workflow
from threads_browser.browser_ui import body_text as body_text_module
from threads_browser.browser_ui.body_text import BodyTextReader
from threads_browser.browser_ui.card_provenance import CardProvenanceReader
from threads_browser.browser_ui.feed_rows import FeedRowScanner, UnverifiedFeedCandidate
from threads_browser.browser_ui.geometry import with_scrollport_geometry
from threads_browser.browser_ui.loading import LoadingObserver
from threads_browser.browser_ui.scrolling import (
    MAX_WHEEL_STEP_PX,
    NativeWheelScrollEngine,
)
from threads_browser.cli import main
from threads_browser.nurture import state as nurture_state
from threads_browser.nurture.observation import RowObservationFacts
from threads_browser.nurture.state import CoverageLedger, WorkflowPhase
from threads_browser.reader import (
    HOME_URL,
    ThreadsHomeReader,
    UiReadError,
    canonical_terms,
    classify_body,
)
from threads_browser.storage import (
    AccountStore,
    BrowserAccount,
    BrowserDataError,
    Checkpoint,
    CheckpointStore,
)
from threads_browser.workflow import policy_fingerprint


def _is_offline_profile_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.netloc == "www.threads.com"
        and re.fullmatch(r"/@[A-Za-z0-9._]{1,30}/?", parsed.path) is not None
        and not parsed.query
        and not parsed.fragment
    )


def _run_offline_page(html: str, exercise, *, headless: bool = True) -> None:
    async def run() -> None:
        async with real_async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=headless)
            context = await browser.new_context(viewport={"width": 1280, "height": 720})

            async def respond(route: Route) -> None:
                if route.request.url == HOME_URL:
                    await route.fulfill(status=200, content_type="text/html", body=html)
                elif _is_offline_profile_url(route.request.url):
                    await route.fulfill(
                        status=200,
                        content_type="text/html",
                        body="<!doctype html><html><body><h1>Local profile</h1></body></html>",
                    )
                else:
                    await route.abort()

            await context.route("**/*", respond)
            page = await context.new_page()
            try:
                await page.goto(HOME_URL, wait_until="domcontentloaded")
                await exercise(page)
            finally:
                await context.close()
                await browser.close()

    asyncio.run(run())


async def _assert_bounded_wheel_gestures(page, scrolls: int) -> None:
    wheel_count = int(await page.evaluate("Number(sessionStorage.getItem('wheel-count') || '0')"))
    assert wheel_count >= scrolls * 2


def _offline_playwright_manager(respond):
    class OfflinePlaywrightManager:
        """Real persistent Chromium with every request fulfilled locally."""

        async def __aenter__(self) -> SimpleNamespace:
            self.manager = real_async_playwright()
            self.playwright = await self.manager.start()
            chromium = self.playwright.chromium

            class Chromium:
                async def launch_persistent_context(self, **kwargs):
                    options = dict(kwargs)
                    options["headless"] = True
                    context = await chromium.launch_persistent_context(**options)
                    await context.route("**/*", respond)
                    return context

            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *_: object) -> None:
            await self.playwright.stop()

    return OfflinePlaywrightManager()


def _mock_persistent_browser(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    state = {"launches": 0}

    class Context:
        def __init__(self) -> None:
            self.pages = [object()]

        async def close(self) -> None:
            pass

    class Chromium:
        async def launch_persistent_context(self, **_kwargs):
            state["launches"] += 1
            return Context()

    class Manager:
        async def __aenter__(self):
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *_args):
            pass

    monkeypatch.setattr(workflow, "async_playwright", Manager)
    return state


async def _run_page_workflow_for_test(page, account, checkpoint_store, **kwargs):
    with checkpoint_store.profile_lock(account):
        return await workflow._run_page_workflow_locked(
            page, account, checkpoint_store, **kwargs
        )


@pytest.fixture(autouse=True)
def _short_offline_render_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader_module, "_RENDER_MIN_DWELL_MS", 25)
    monkeypatch.setattr(reader_module, "_RENDER_SETTLE_INTERVAL_MS", 20)


def _post_fingerprint(url: str, author: str = "candidate") -> str:
    return sha256(f"{url}\n{author}".encode()).hexdigest()


def _seed_pending_checkpoint(
    store: CheckpointStore,
    account: BrowserAccount,
    terms: tuple[str, ...],
    pending_url: str,
    *,
    completed_urls: tuple[str, ...] = (),
) -> tuple[str, str, tuple[str, ...]]:
    policy = policy_fingerprint(canonical_terms(terms))
    pending_fingerprint = _post_fingerprint(pending_url)
    completed_fingerprints = tuple(_post_fingerprint(url) for url in completed_urls)
    seen_fingerprints = (pending_fingerprint, *completed_fingerprints)
    store.save(
        Checkpoint(
            account.account_id,
            policy,
            completed_runs=len(seen_fingerprints),
            home_sampled=len(seen_fingerprints),
            home_new=len(seen_fingerprints),
            home_irrelevant=len(seen_fingerprints),
            detail_reads=len(completed_fingerprints),
            pending_read_fingerprints=(pending_fingerprint,),
            seen_fingerprints=seen_fingerprints,
        )
    )
    return policy, pending_fingerprint, completed_fingerprints


def test_relevance_classifier_is_conservative() -> None:
    terms = canonical_terms(("tuyển dụng", "engineer"))
    assert classify_body("We are hiring an engineer", terms) == "RELEVANT"
    assert classify_body("A weather update", terms) == "IRRELEVANT"
    assert classify_body(None, terms) == "UNKNOWN"
    assert classify_body("   ", terms) == "UNKNOWN"


def test_job_seeking_and_hiring_terms_are_literal_and_independent() -> None:
    job_seeking = canonical_terms(("đang tìm việc",))
    hiring = canonical_terms(("đang tuyển",))
    assert classify_body("Tôi đang tìm việc kỹ sư", job_seeking) == "RELEVANT"
    assert classify_body("Công ty đang tuyển kỹ sư", job_seeking) == "IRRELEVANT"
    assert classify_body("Công ty đang tuyển kỹ sư", hiring) == "RELEVANT"
    assert classify_body("Tôi đang tìm việc kỹ sư", hiring) == "IRRELEVANT"


def test_offline_chromium_cli_reads_once_and_resumes_without_double_counting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    old_url = "https://www.threads.com/@candidate/post/old"
    old_relevant_url = "https://www.threads.com/@candidate/post/one"
    new_relevant_url = "https://www.threads.com/@candidate/post/two"
    old_body = "A weather update"
    old_relevant_body = "Hiring a backend engineer in Vietnam"
    new_relevant_body = "Hiring a frontend engineer in Vietnam"
    first_feed_html = f"""<!doctype html>
<html><head><style>article {{ min-height: 840px; }}</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav>
<article>
  <a href="/@candidate/post/old"><time>2h</time></a>
  <a href="/@candidate">@candidate</a>
  <p>{old_body}</p>
</article>
<article>
  <a href="/@candidate/post/one"><time>1h</time></a>
  <a href="/@candidate">@candidate</a>
  <p>{old_relevant_body}</p>
</article>
</body></html>"""
    second_feed_html = first_feed_html.replace(
        "</body>",
        f"""<article>
  <a href="/@candidate/post/two"><time>now</time></a>
  <a href="/@candidate">@candidate</a>
  <p>{new_relevant_body}</p>
</article></body>""",
    )
    old_detail_html = f"""<!doctype html><html><body><main>
<div data-pressable-container="true">
  <a href="/@candidate">@candidate</a>
  <p>{old_relevant_body}</p>
  </div></main></body></html>"""
    new_detail_html = f"""<!doctype html><html><body><main>
<div data-pressable-container="true">
  <a href="/@candidate">@candidate</a>
  <p>{new_relevant_body}</p>
  </div></main></body></html>"""
    account_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(account_root).add("offline", actor, profile)
    store = CheckpointStore(account_root)
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(account_root))
    requests: list[str] = []
    feed_version = {"value": 1}

    async def respond(route: Route) -> None:
        url = route.request.url
        requests.append(url)
        if url == HOME_URL:
            html = (first_feed_html, second_feed_html)[feed_version["value"] - 1]
        elif url == old_relevant_url:
            html = old_detail_html
        elif url == new_relevant_url:
            html = new_detail_html
        elif _is_offline_profile_url(url):
            html = "<!doctype html><html><body><h1>Local profile</h1></body></html>"
        else:
            await route.abort()
            return
        await route.fulfill(status=200, content_type="text/html", body=html)

    monkeypatch.setattr(
        workflow,
        "async_playwright",
        lambda: _offline_playwright_manager(respond),
    )
    command = [
        "run",
        "offline",
        "--max-posts",
        "2",
        "-t",
        "hiring",
        "-t",
        "engineer",
    ]

    first_code = main(command)
    first = capsys.readouterr()
    first_summary = next(
        (line for line in first.out.splitlines() if line.startswith("NURTURE ")), ""
    )
    feed_version["value"] = 2
    second_code = main(command)
    second = capsys.readouterr()
    second_summary = next(
        (line for line in second.out.splitlines() if line.startswith("NURTURE ")), ""
    )
    second_scroll_diagnostics = "\n".join(
        line
        for line in second.out.splitlines()
        if line.startswith(("[SCROLL_PROGRESS]", "[SCROLL_GEOMETRY]"))
    )

    assert first_code == 0, first.out + first.err
    assert first_summary.startswith("NURTURE status=COMPLETE source=HOME_FOR_YOU")
    assert "ui=UNVERIFIED_LIVE_UI" in first_summary
    assert "home_sampled=2 home_new=2" in first_summary
    assert "home_relevant=1 home_irrelevant=1" in first_summary
    assert "home_relevant_ratio=0.50" in first_summary
    assert "detail_reads=1 scroll_attempts=1 scrolls=1 skipped_unverified=0" in first_summary
    assert second_code == 0, second.out + second.err
    assert second_summary.startswith(
        "NURTURE status=COMPLETE source=HOME_FOR_YOU"
    ), f"{second_summary}\n{second_scroll_diagnostics}"
    assert "home_sampled=3 home_new=1" in second_summary
    assert "home_relevant=2 home_irrelevant=1" in second_summary
    assert "home_relevant_ratio=0.67" in second_summary
    assert "duplicates=4 detail_reads=1" in second_summary
    assert "scroll_attempts=4 scrolls=4" in second_summary
    assert all(
        value not in first.out + first.err + second.out + second.err
        for value in (
            str(profile),
            actor,
            old_body,
            old_relevant_body,
            new_relevant_body,
            old_url,
            old_relevant_url,
            new_relevant_url,
        )
    )
    assert "POST_TEXT" not in first.out + first.err + second.out + second.err

    with store.profile_lock(account):
        pass

    assert requests
    assert all(urlsplit(url).netloc == "www.threads.com" for url in requests)
    assert {HOME_URL, old_relevant_url, new_relevant_url}.issubset(requests)
    assert requests.count(old_relevant_url) == 1
    assert requests.count(new_relevant_url) == 1
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 2
    assert checkpoint.home_sampled == 5
    assert checkpoint.home_new == 3
    assert checkpoint.home_relevant == 3
    assert checkpoint.home_irrelevant == 2
    assert checkpoint.duplicates == 4
    assert checkpoint.detail_reads == 2
    assert checkpoint.scroll_attempts == 5
    assert checkpoint.scrolls == 5
    assert len(checkpoint.seen_fingerprints) == 3
    assert checkpoint.pending_read_fingerprints == ()
    checkpoint_path = (
        account_root
        / "nurture"
        / "checkpoints"
        / f"{account.account_id}-{checkpoint.policy_fingerprint}.json"
    )
    persisted = checkpoint_path.read_text(encoding="utf-8")
    assert old_body not in persisted
    assert old_relevant_body not in persisted
    assert new_relevant_body not in persisted
    assert old_url not in persisted
    assert old_relevant_url not in persisted
    assert new_relevant_url not in persisted
    assert actor not in persisted


def test_scheduled_offline_chromium_cli_suppresses_row_and_scroll_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    terms = ("hiring",)
    long_body = " ".join(["Synthetic weather remains unresolved."] * 120)
    cards = "".join(
        f'''<article class="card"><a href="/@candidate/post/verified-{index}">
<time>now</time></a><a href="/@candidate">candidate</a>
<p class="body">Synthetic weather update {index}.</p></article>'''
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
        lambda: _offline_playwright_manager(respond),
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
    summary = next(
        (line for line in captured.out.splitlines() if line.startswith("NURTURE ")), ""
    )
    status_json = (data_root / "operations" / "last_run.json").read_text(
        encoding="utf-8"
    )
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
        line.startswith(("NURTURE ", "STOP ", "ERROR ", "WARNING "))
        for line in output.splitlines()
    )
    assert all(
        value not in status_json
        for value in (str(profile), actor, long_body, "https://www.threads.com/")
    )

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
    interactive_output = capsys.readouterr().out
    assert interactive_code == 0
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
    status = json.loads(
        (data_root / "operations" / "last_run.json").read_text(encoding="utf-8")
    )

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
        f'''<article class="card"><a href="/@candidate/post/scroll-failure-{index}">
<time>now</time></a><a href="/@candidate">candidate</a>
<p class="body">Synthetic weather update {index}.</p></article>'''
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
    status = json.loads(
        (data_root / "operations" / "last_run.json").read_text(encoding="utf-8")
    )

    assert wheel_calls == 1
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "STOP UI_READ_UNVERIFIED\n"
    assert "FAILURE_DIAGNOSTIC" not in captured.out + captured.err
    assert status["status"] == "FAILED"
    assert status["stop_reason"] == "UI_READ_UNVERIFIED"
    assert status["exit_code"] == 1


def test_first_viewport_relevance_scrolls_then_reads_and_restores_home(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    post_url = "https://www.threads.com/@candidate/post/native-cycle"
    truncated_body = "Hiring a backend engineer…"
    expanded_body = "Hiring a backend engineer in Ho Chi Minh City"
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
.post-card {{ box-sizing: border-box; height: 1200px; padding-top: 300px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
  <article class="post-card">
    <a href="/@candidate/post/native-cycle"><time>now</time></a>
    <a href="/@candidate">@candidate</a>
    <p>{truncated_body}</p><button type="button">See more</button>
  </article>
  <article class="post-card">
    <a href="/@candidate/post/weather"><time>1h</time></a>
    <a href="/@candidate">@candidate</a><p>A synthetic weather update</p>
  </article>
</main><script>
window.addEventListener("wheel", () => {{
  sessionStorage.setItem("native-cycle-wheel", "1");
}}, {{ once: true }});
document.querySelector('a[href*="native-cycle"]').addEventListener("click", () => {{
  sessionStorage.setItem(
    "native-cycle-click-after-scroll",
    String(document.scrollingElement.scrollTop > 0)
  );
}});
</script></body></html>"""
    detail_html = f"""<!doctype html><html><head><meta charset="utf-8"></head>
<body><main><div data-pressable-container="true">
<header><a href="/@candidate">@candidate</a></header>
<div class="post-body"><p>{expanded_body}</p></div>
<blockquote><div data-pressable-container="true">
<a href="/@quoted">@quoted</a><p>Quoted synthetic job text.</p>
</div></blockquote>
</div>
<article><a href="/@replier">@replier</a><p>A synthetic reply.</p></article>
</main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    profile_visits = 0
    detail_visits = 0

    async def exercise(page) -> None:
        async def serve_profile(route: Route) -> None:
            nonlocal profile_visits
            profile_visits += 1
            await route.fulfill(
                status=200,
                content_type="text/html",
                body="<!doctype html><html><body><h1>Local profile</h1></body></html>",
            )

        async def serve_detail(route: Route) -> None:
            nonlocal detail_visits
            detail_visits += 1
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=detail_html,
            )

        await page.route("https://www.threads.com/@offline.operator", serve_profile)
        await page.route(post_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=3,
            max_scrolls=1,
            max_total_posts=5,
            terms=("hiring",),
            detail=True,
        )
        assert result.status == "COMPLETE"
        assert result.scroll_attempts == result.scrolls == 1
        assert result.detail_reads == 1
        assert await page.evaluate(
            "sessionStorage.getItem('native-cycle-click-after-scroll')"
        ) == "true"
        assert await page.evaluate(
            "sessionStorage.getItem('native-cycle-wheel')"
        ) == "1"
        assert detail_visits == 1

    _run_offline_page(feed_html, exercise)
    assert profile_visits == 1
    policy = policy_fingerprint(canonical_terms(("hiring",)))
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 1
    assert checkpoint.detail_reads == 1
    output = capsys.readouterr().out
    assert "[HOME] status=READY account=VERIFIED source=FOR_YOU" in output
    assert "[VIEWPORT 1]" in output
    assert "[VIEWPORT 2]" in output
    assert "source=HOME_BODY extent=TRUNCATED" in output
    assert "[SCROLL] attempt=1 status=ADVANCED observed=1" in output
    assert "[SELECT] post=1 decision=RELEVANT reason=RELEVANT_NEW" in output
    assert "[DETAIL] post=1 status=VERIFIED provenance=VERIFIED extent=EXPANDED" in output
    assert "<<< DETAIL_BODY BEGIN status=VERIFIED decision=RELEVANT extent=EXPANDED >>>" in output
    assert "[BACK] post=1 status=RESTORED anchor=IDENTITY_VERIFIED" in output
    assert "[CHECKPOINT] status=SAVED completed_runs=1" in output
    assert "[CYCLE] status=COMPLETE" in output
    assert output.count(f"| {truncated_body}") == 1
    assert output.count(f"| {expanded_body}") == 1
    assert output.index("[HOME]") < output.index("[VIEWPORT 1]")
    assert output.index("[VIEWPORT 1]") < output.index("[SCROLL] attempt=1")
    assert output.index("[SCROLL] attempt=1 status=ADVANCED") < output.index("[SELECT]")
    assert output.index("[SELECT]") < output.index("[DETAIL]")
    assert output.index("[DETAIL]") < output.index("[BACK]")
    assert output.index("[BACK]") < output.index("[CHECKPOINT]")
    persisted = (
        data_root
        / "nurture"
        / "checkpoints"
        / f"{account.account_id}-{policy}.json"
    ).read_text(encoding="utf-8")
    assert all(value not in persisted for value in (truncated_body, expanded_body, post_url, actor))


def test_nested_feed_scroll_is_observed_and_no_movement_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    second_url = "https://www.threads.com/@candidate/post/second"
    body = "Hiring a frontend engineer in Vietnam"

    def nested_feed_html(*, prevent_wheel: bool = False) -> str:
        prevent = (
            "window.addEventListener('wheel', event => event.preventDefault(), "
            "{passive: false});"
            if prevent_wheel
            else ""
        )
        return f"""<!doctype html><html><head><style>
html, body {{ height: 100%; margin: 0; overflow: hidden; }}
nav {{ height: 40px; }}
#scroll-port {{ height: calc(100vh - 40px); overflow-y: auto; }}
.post-card {{ box-sizing: border-box; height: 760px; padding-top: 80px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><div id="scroll-port">
  <div class="post-card"><a href="/@candidate/post/first"><time>2h</time></a>
    <a href="/@candidate">@candidate</a><p>A weather update</p></div>
  <div class="post-card"><a href="/@candidate/post/second"><time>1h</time></a>
    <a href="/@candidate">@candidate</a><p>{body}</p></div>
</div><script>{prevent}</script></body></html>"""

    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 3_000)
    monkeypatch.setattr(reader_module, "_READINESS_RECHECK_INTERVAL_MS", 20)

    async def scrolling_exercise(page) -> None:
        assert await page.get_by_role("main").count() == 0
        assert await page.get_by_role("feed").count() == 0
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        before = await reader.scan_viewport(2)
        assert len(before) == 1
        assert await reader.advance_feed()
        positions = await page.evaluate(
            """() => ({
                inner: document.querySelector("#scroll-port").scrollTop,
                document: document.scrollingElement.scrollTop,
            })"""
        )
        assert positions["inner"] > 1
        assert positions["document"] == 0
        after = await reader.scan_viewport(
            2,
            scanned_fingerprints={before[0].fingerprint},
        )
        assert len(after) == 1
        assert after[0].url == second_url
        assert reader.scroll_attempts == 1
        assert reader.scrolls == 1

    async def stalled_exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        assert len(await reader.scan_viewport(2)) == 1
        with pytest.raises(UiReadError) as caught:
            await reader.advance_feed()
        assert caught.value.code == "HOME_SCROLL_UNVERIFIED"
        positions = await page.evaluate(
            """() => ({
                inner: document.querySelector("#scroll-port").scrollTop,
                document: document.scrollingElement.scrollTop,
            })"""
        )
        assert positions == {"inner": 0, "document": 0}
        assert reader.scroll_attempts == 1
        assert reader.scrolls == 0

    _run_offline_page(nested_feed_html(), scrolling_exercise)
    _run_offline_page(nested_feed_html(prevent_wheel=True), stalled_exercise)


def test_non_article_permalink_cards_sample_and_verify_detail() -> None:
    actor = "offline.operator"
    first_url = "https://www.threads.com/@candidate/post/first-div"
    detail_url = "https://www.threads.com/@candidate/post/second-div"
    detail_body = "Hiring a frontend engineer in Vietnam"
    feed_html = f"""<!doctype html><html><head><style>
.post-card {{ box-sizing: border-box; height: 760px; padding-top: 80px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
  <a href="/@navigation/post/decoy">Navigation decoy</a>
</nav>
<main>
  <div class="post-card">
    <header><a href="/@candidate/post/first-div"><time>2h</time></a>
      <a href="/@candidate/post/first-div"><span>Permalink</span></a></header>
    <a href="/@candidate">@candidate</a><p>A weather update</p>
  </div>
  <div class="post-card">
    <header><a href="/@candidate/post/second-div"><time>1h</time></a></header>
    <a href="/@candidate">@candidate</a><p>{detail_body}</p>
  </div>
</main></body></html>"""
    detail_html = f"""<!doctype html><html><body><main>
<div><header><a href="/@candidate/post/second-div"><time>1h</time></a></header>
<a href="/@candidate">@candidate</a><p>{detail_body}</p></div>
</main></body></html>"""

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=detail_html,
            )

        await page.route(detail_url, serve_detail)
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        sample = await reader.sample_and_scroll(2)
        assert reader.scroll_attempts == 1
        assert reader.scrolls == 1
        assert {target.url for target in sample.observed_targets} == {
            first_url,
            detail_url,
        }
        target = next(target for target in sample.targets if target.url == detail_url)
        detail = await reader.read_detail(target)
        assert detail.url == detail_url
        assert detail.author == "candidate"
        assert detail.body == detail_body
        await reader.return_to_home()
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)


def test_detail_target_with_nested_and_adjacent_post_links_is_verified() -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/detail-with-related-links"
    body = "Hiring a synthetic engineer for a test fixture."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
  <a href="/@candidate/post/detail-with-related-links"><time>now</time></a>
  <a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    detail_html = f"""<!doctype html><html><body><main>
<article id="detail-root">
  <a href="/@candidate/post/detail-with-related-links"><time>now</time></a>
  <a href="/@candidate/post/detail-with-related-links">canonical permalink</a>
  <a href="/@candidate">candidate</a><p>{body}</p>
  <div class="related-post-links">
    <a href="/@reply/post/reply-context">synthetic reply</a>
    <a href="/@quote/post/quote-context">synthetic quote</a>
  </div>
</article>
<aside><a href="/@other/post/adjacent-context">synthetic adjacent post</a></aside>
</main></body></html>"""

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(status=200, content_type="text/html", body=detail_html)

        await page.route(target_url, serve_detail)
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        targets = await reader.scan_viewport(1)
        assert len(targets) == 1

        detail = await reader.read_detail(targets[0])
        assert reader.detail_route_verified
        assert detail.url == target_url
        assert detail.author == "candidate"
        assert detail.body == body
        reported = reader._detail_navigator._last_reported_detail_state
        assert reported is not None
        assert reported[1].canonical_self_permalink_candidates == 2
        assert reported[1].matching_roots == 1
        assert reported[1].terminal_reason == "DETAIL_VERIFIED"
        await reader.return_to_home()
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)


def test_detail_without_landmark_or_identity_links_uses_unique_matching_body(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/body-only-detail"
    body = "Hiring a synthetic backend engineer."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
<a href="/@candidate/post/body-only-detail"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    detail_html = (
        "<!doctype html><html><body><div><p>Hiring a synthetic</p>"
        "<p>backend engineer.</p></div></body></html>"
    )
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(status=200, content_type="text/html", body=detail_html)

        await page.route(target_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=0,
            terms=("hiring",),
            detail=True,
        )
        assert result.status == "COMPLETE"
        assert result.detail_reads == 1
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 1
    assert checkpoint.pending_read_fingerprints == ()
    output = capsys.readouterr().out
    assert "main_regions_count=0" in output
    assert "matching_roots=1" in output
    assert "terminal_reason=DETAIL_VERIFIED" in output


def test_back_accepts_selected_post_after_vertical_layout_shift(
    tmp_path: Path,
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/back-layout-shift"
    body = "Hiring a synthetic designer."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
<a id="selected" href="/@candidate/post/back-layout-shift"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main><script>
const selected = document.querySelector("#selected");
selected.addEventListener("click", () => {{
  sessionStorage.setItem("return-anchor-y", String(
    document.querySelector("article").getBoundingClientRect().y
  ));
}});
window.addEventListener("pageshow", () => {{
  if (sessionStorage.getItem("return-anchor-y") === null) return;
  const spacer = document.createElement("div");
  spacer.id = "return-layout-shift";
  spacer.style.height = "120px";
  document.querySelector("main").prepend(spacer);
  const before = Number(sessionStorage.getItem("return-anchor-y"));
  const after = document.querySelector("article").getBoundingClientRect().y;
  sessionStorage.setItem("return-anchor-drift", String(after - before));
  sessionStorage.removeItem("return-anchor-y");
}});
</script></body></html>"""
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(status=200, content_type="text/html", body=detail_html)

        await page.route(target_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=0,
            terms=("hiring",),
            detail=True,
        )
        drift = await page.evaluate("Number(sessionStorage.getItem('return-anchor-drift'))")
        assert drift > 48
        assert result.status == "COMPLETE"
        assert result.detail_reads == 1
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 1


@pytest.mark.parametrize(
    ("detail_fragment", "expected_code"),
    [
        (
            (
                '<div data-pressable-container="true"><a href="/@candidate">candidate</a>'
                '<a href="/@other">other</a><p>{body}</p></div>'
            ),
            "DETAIL_PROVENANCE_UNVERIFIED",
        ),
        (
            (
                '<div data-pressable-container="true"><a href="/@candidate">candidate</a>'
                '<a href="/@candidate/post/detail-evidence?view=1">rejected</a>'
                '<p>{body}</p></div>'
            ),
            "DETAIL_PROVENANCE_UNVERIFIED",
        ),
        (
            (
                '<div data-pressable-container="true">'
                '<a href="/@candidate/post/detail-evidence">canonical target</a>'
                '<a href="/@candidate">candidate</a>'
                '<a href="/@candidate/post/detail-evidence?view=1">noncanonical link</a>'
                '<p>{body}</p></div>'
            ),
            "DETAIL_PROVENANCE_UNVERIFIED",
        ),
        (
            (
                '<div data-pressable-container="true"><a href="/@candidate">candidate</a>'
                '<a href="/@candidate/post/another-post">mismatched post</a>'
                '<p>{body}</p></div>'
            ),
            "DETAIL_PROVENANCE_UNVERIFIED",
        ),
        (
            (
                '<div data-pressable-container="true">'
                '<a href="/@candidate/post/detail-evidence">canonical target</a>'
                '<a href="/@candidate/post/another-post">mismatched post</a>'
                '<p>{body}</p></div>'
            ),
            "DETAIL_PROVENANCE_UNVERIFIED",
        ),
        (
            (
                '<div data-pressable-container="true"><a href="/@candidate">candidate</a>'
                '<p>A different synthetic body.</p></div>'
            ),
            "DETAIL_BODY_UNVERIFIED",
        ),
        (
            (
                '<div data-pressable-container="true" id="selected-post">'
                '<a href="/@candidate/post/detail-evidence">selected post</a>'
                '<a href="/@candidate">candidate</a><p>{body}</p></div>'
                '<div data-pressable-container="true" id="related-reply-or-quote">'
                '<a href="/@candidate/post/related-context">related post</a>'
                '<a href="/@candidate">candidate</a><p>{body}</p></div>'
            ),
            "DETAIL_ROOT_AMBIGUOUS",
        ),
    ],
    ids=(
        "multiple-authors",
        "noncanonical-post-link",
        "target-plus-noncanonical-post-link",
        "mismatched-post-link",
        "mismatched-post-link-without-author",
        "changed-body",
        "ambiguous-reply-or-quote-root",
    ),
)
def test_detail_verification_failures_return_home_and_keep_retry_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    detail_fragment: str,
    expected_code: str,
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/detail-evidence"
    body = "Hiring a synthetic target body."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><a href="/@candidate/post/detail-evidence"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p></article></main></body></html>"""
    detail_html = (
        '<!doctype html><html><body><main>'
        + detail_fragment.replace("{body}", body)
        + "</main></body></html>"
    )
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 1_500)
    monkeypatch.setattr(reader_module, "_DETAIL_TIMEOUT_MS", 1_000)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(status=200, content_type="text/html", body=detail_html)

        await page.route(target_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=0,
            terms=("hiring",),
            detail=True,
        )
        assert result.status == "DETAIL_RETRY_PENDING"
        assert result.detail_reads == 0
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)
    policy = policy_fingerprint(canonical_terms(("hiring",)))
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 0
    assert len(checkpoint.pending_read_fingerprints) == 1
    output = capsys.readouterr().out
    assert f"status=UNAVAILABLE code={expected_code} retry=PENDING" in output
    assert "[BACK] post=1 status=RESTORED anchor=IDENTITY_VERIFIED" in output
    assert "DETAIL_VERIFIED status=PROVENANCE_VERIFIED" not in output
    diagnostics = "\n".join(
        line for line in output.splitlines() if line.startswith("[DETAIL_STATE]")
    )
    assert "route_verified=true" in diagnostics
    assert all(value not in diagnostics for value in (target_url, actor, body, str(profile)))


def test_detail_dom_replacement_does_not_mix_root_and_body_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/dom-replacement"
    body = "Hiring a synthetic target through a DOM hydration fixture."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
  <a href="/@candidate/post/dom-replacement"><time>now</time></a>
  <a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    detail_html = f"""<!doctype html><html><body><main>
<article id="detail-root">
  <a href="/@candidate/post/dom-replacement"><time>now</time></a>
  <a href="/@candidate">candidate</a><p>{body}</p>
</article>
</main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    detail_requests = 0
    root_replacements = 0
    original_read_detail_body = ThreadsHomeReader._read_detail_body
    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 1_500)
    monkeypatch.setattr(reader_module, "_DETAIL_TIMEOUT_MS", 1_000)

    async def replace_root_before_body(self, root, fingerprint):
        nonlocal root_replacements
        if root_replacements == 0:
            root_replacements += 1
            await self._page.evaluate(
                """() => {
                    const oldRoot = document.querySelector("#detail-root");
                    const replacement = document.createElement("article");
                    replacement.id = "detail-root";
                    replacement.innerHTML =
                      '<a href="/@different"><span>different author</span></a>' +
                      '<p>Hiring a synthetic target through a DOM hydration fixture.</p>';
                    oldRoot.replaceWith(replacement);
                }"""
            )
        return await original_read_detail_body(self, root, fingerprint)

    monkeypatch.setattr(ThreadsHomeReader, "_read_detail_body", replace_root_before_body)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            await route.fulfill(status=200, content_type="text/html", body=detail_html)

        await page.route(target_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=0,
            terms=("hiring",),
            detail=True,
        )
        assert result.status == "DETAIL_RETRY_PENDING"
        assert result.detail_reads == 0
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)
    policy = policy_fingerprint(canonical_terms(("hiring",)))
    checkpoint = store.load(account.account_id, policy)
    assert root_replacements == 1
    assert detail_requests == 1
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 0
    assert len(checkpoint.pending_read_fingerprints) == 1
    output = capsys.readouterr().out
    assert "[BACK] post=1 status=RESTORED anchor=IDENTITY_VERIFIED" in output
    assert "DETAIL_VERIFIED status=PROVENANCE_VERIFIED" not in output
    detail_states = [line for line in output.splitlines() if line.startswith("[DETAIL_STATE]")]
    assert any("terminal_reason=AUTHOR_CONFLICT" in line for line in detail_states)


def test_empty_detail_is_reported_unavailable_and_home_progress_is_saved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/stable-empty"
    body = "Hiring a synthetic target body."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><a href="/@candidate/post/stable-empty"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p></article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    detail_requests = 0
    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 1_500)
    monkeypatch.setattr(reader_module, "_DETAIL_TIMEOUT_MS", 1_000)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=(
                    "<!doctype html><html><body>"
                    '<main style="min-height: 1px"></main></body></html>'
                ),
            )

        await page.route(target_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=0,
            terms=("hiring",),
            detail=True,
        )
        assert result.status == "DETAIL_RETRY_PENDING"
        assert result.detail_reads == 0
        assert page.url == HOME_URL
        assert detail_requests == 1

    _run_offline_page(feed_html, exercise)
    policy = policy_fingerprint(canonical_terms(("hiring",)))
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 0
    assert len(checkpoint.pending_read_fingerprints) == 1
    output = capsys.readouterr().out
    assert "terminal_reason=RENDER_UNAVAILABLE" in output
    assert "status=UNAVAILABLE code=DETAIL_RENDER_UNAVAILABLE retry=PENDING" in output
    assert "WAITING stage=DETAIL retry=1" in output
    diagnostics = "\n".join(
        line for line in output.splitlines() if line.startswith("[DETAIL_STATE]")
    )
    assert all(value not in diagnostics for value in (target_url, actor, body, str(profile)))


def test_stale_pending_allows_one_verified_fallback_and_saves_remaining_pending(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    stale_url = "https://www.threads.com/@candidate/post/absent-pending"
    fallback_url = "https://www.threads.com/@candidate/post/fresh-fallback"
    body = "A synthetic weather update."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><a href="/@candidate/post/fresh-fallback"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p></article></main></body></html>"""
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    policy, stale_fingerprint, _ = _seed_pending_checkpoint(
        store,
        account,
        ("hiring",),
        stale_url,
    )
    detail_attempts = 0

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_attempts
            detail_attempts += 1
            await route.fulfill(status=200, content_type="text/html", body=detail_html)

        await page.route(fallback_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=0,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE"
        assert result.detail_reads == 1
        assert result.checkpoint_saved
        assert result.checkpoint_runs == 2
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)
    checkpoint = store.load(account.account_id, policy)
    fallback_fingerprint = _post_fingerprint(fallback_url)
    assert checkpoint.schema_version == 3
    assert checkpoint.completed_runs == 2
    assert checkpoint.detail_reads == 1
    assert checkpoint.pending_read_fingerprints == (stale_fingerprint,)
    assert fallback_fingerprint in checkpoint.seen_fingerprints
    assert fallback_fingerprint not in checkpoint.pending_read_fingerprints
    assert detail_attempts == 1
    output = capsys.readouterr().out
    assert all(value not in output for value in (stale_url, fallback_url, actor, body, str(profile)))


def test_stale_pending_detail_failure_keeps_both_entries_for_retry(
    tmp_path: Path,
) -> None:
    actor = "offline.operator"
    stale_url = "https://www.threads.com/@candidate/post/absent-pending-failure"
    fallback_url = "https://www.threads.com/@candidate/post/failing-fallback"
    body = "A synthetic weather update."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><a href="/@candidate/post/failing-fallback"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p></article></main></body></html>"""
    bad_detail_html = """<!doctype html><html><body><main><article>
<a href="/@candidate">candidate</a><p>A different synthetic update.</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    policy, stale_fingerprint, _ = _seed_pending_checkpoint(
        store,
        account,
        ("hiring",),
        stale_url,
    )
    detail_attempts = 0

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_attempts
            detail_attempts += 1
            await route.fulfill(status=200, content_type="text/html", body=bad_detail_html)

        await page.route(fallback_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=0,
            terms=("hiring",),
        )
        assert result.status == "DETAIL_RETRY_PENDING"
        assert result.detail_reads == 0
        assert result.checkpoint_saved
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)
    checkpoint = store.load(account.account_id, policy)
    fallback_fingerprint = _post_fingerprint(fallback_url)
    assert checkpoint.schema_version == 3
    assert checkpoint.completed_runs == 2
    assert checkpoint.detail_reads == 0
    assert checkpoint.pending_read_fingerprints == (stale_fingerprint, fallback_fingerprint)
    assert detail_attempts == 1


def test_current_pending_beats_a_fresh_fallback(
    tmp_path: Path,
) -> None:
    actor = "offline.operator"
    pending_url = "https://www.threads.com/@candidate/post/current-pending"
    fresh_url = "https://www.threads.com/@candidate/post/other-fallback"
    pending_body = "A pending synthetic weather update."
    fresh_body = "A fresh synthetic weather update."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav><main>
<article><a href="/@candidate/post/current-pending"><time>2h</time></a>
<a href="/@candidate">candidate</a><p>{pending_body}</p></article>
<article><a href="/@candidate/post/other-fallback"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{fresh_body}</p></article>
</main></body></html>"""
    pending_detail = f"""<!doctype html><html><body><main><article>
<a href="/@candidate">candidate</a><p>{pending_body}</p>
</article></main></body></html>"""
    fresh_detail = f"""<!doctype html><html><body><main><article>
<a href="/@candidate">candidate</a><p>{fresh_body}</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    policy, pending_fingerprint, _ = _seed_pending_checkpoint(
        store,
        account,
        ("hiring",),
        pending_url,
    )
    detail_attempts = {pending_url: 0, fresh_url: 0}

    async def exercise(page) -> None:
        async def serve(url: str, html: str, route: Route) -> None:
            detail_attempts[url] += 1
            await route.fulfill(status=200, content_type="text/html", body=html)

        await page.route(
            pending_url,
            lambda route: serve(pending_url, pending_detail, route),
        )
        await page.route(
            fresh_url,
            lambda route: serve(fresh_url, fresh_detail, route),
        )
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=0,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE"
        assert result.detail_reads == 1
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)
    checkpoint = store.load(account.account_id, policy)
    assert detail_attempts == {pending_url: 1, fresh_url: 0}
    assert checkpoint.pending_read_fingerprints == ()
    assert pending_fingerprint not in checkpoint.pending_read_fingerprints


def test_stale_pending_does_not_reopen_completed_posts_or_navigate_without_actionability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    stale_url = "https://www.threads.com/@candidate/post/absent-pending-actionability"
    completed_url = "https://www.threads.com/@candidate/post/already-completed"
    fresh_url = "https://www.threads.com/@candidate/post/not-actionable"
    completed_body = "A completed synthetic weather update."
    fresh_body = "A fresh synthetic weather update."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav><main>
<article><a href="/@candidate/post/already-completed"><time>2h</time></a>
<a href="/@candidate">candidate</a><p>{completed_body}</p></article>
<article><a href="/@candidate/post/not-actionable"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{fresh_body}</p></article>
</main></body></html>"""
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate">candidate</a><p>{fresh_body}</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    policy, stale_fingerprint, completed_fingerprints = _seed_pending_checkpoint(
        store,
        account,
        ("hiring",),
        stale_url,
        completed_urls=(completed_url,),
    )
    actionability_checks: list[str] = []
    detail_attempts = 0

    async def reject_fresh_target(self, target):
        actionability_checks.append(target.fingerprint)
        raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")

    monkeypatch.setattr(ThreadsHomeReader, "actionable_now", reject_fresh_target)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_attempts
            detail_attempts += 1
            await route.fulfill(status=200, content_type="text/html", body=detail_html)

        await page.route(fresh_url, serve_detail)
        for _ in range(2):
            result = await _run_page_workflow_for_test(
                page,
                account,
                store,
                max_posts=2,
                max_scrolls=0,
                terms=("hiring",),
            )
            assert result.status == "NO_ACTIONABLE_TARGET"
            assert result.detail_reads == 0
            assert not result.checkpoint_saved
            assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)
    fresh_fingerprint = _post_fingerprint(fresh_url)
    assert actionability_checks == [fresh_fingerprint, fresh_fingerprint]
    assert detail_attempts == 0
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.completed_runs == 2
    assert checkpoint.detail_reads == 1
    assert checkpoint.pending_read_fingerprints == (stale_fingerprint,)
    assert completed_fingerprints[0] in checkpoint.seen_fingerprints


def test_stale_pending_does_not_mask_incomplete_home_observations(
    tmp_path: Path,
) -> None:
    actor = "offline.operator"
    stale_url = "https://www.threads.com/@candidate/post/absent-pending-incomplete"
    fallback_url = "https://www.threads.com/@candidate/post/clean-fallback"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav><main>
<article id="unresolved-wrapper">
<a href="/@candidate/post/ambiguous"><time>now</time></a>
<a href="/@candidate/post/related">related link</a>
<a href="/@candidate">candidate</a>
<p>A synthetic weather update with an unresolved post boundary.</p>
</article>
<article><a href="/@candidate/post/clean-fallback"><time>1h</time></a>
<a href="/@candidate">candidate</a><p>A clean synthetic weather update.</p></article>
</main></body></html>"""
    detail_html = """<!doctype html><html><body><main><article>
<a href="/@candidate">candidate</a><p>A clean synthetic weather update.</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    policy, stale_fingerprint, _ = _seed_pending_checkpoint(
        store,
        account,
        ("hiring",),
        stale_url,
    )
    detail_attempts = 0

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_attempts
            detail_attempts += 1
            await route.fulfill(status=200, content_type="text/html", body=detail_html)

        await page.route(fallback_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=0,
            terms=("hiring",),
        )
        assert result.status == "RELEVANT_POST_NOT_CURRENT"
        assert result.detail_reads == 0
        assert not result.checkpoint_saved
        assert not result.viewport_coverage_complete
        assert result.unresolved_gaps > 0
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 0
    assert checkpoint.pending_read_fingerprints == (stale_fingerprint,)
    assert detail_attempts == 0


def test_failed_fallback_retries_and_completed_post_stays_deduped_across_ten_cycles(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/fallback-retry"
    body = "A synthetic weather update."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
<a href="/@candidate/post/fallback-retry"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    valid_detail = f"""<!doctype html><html><body><main><article>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    empty_detail = "<!doctype html><html><body><main style='min-height: 1px'></main></body></html>"
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    detail_attempts = 0

    async def serve_detail(route: Route) -> None:
        nonlocal detail_attempts
        detail_attempts += 1
        await route.fulfill(
            status=200,
            content_type="text/html",
            body=empty_detail if detail_attempts == 1 else valid_detail,
        )

    async def repeated_cycles(page) -> None:
        await page.route(target_url, serve_detail)
        checkpoints = []
        for cycle in range(10):
            result = await _run_page_workflow_for_test(
                page,
                account,
                store,
                max_posts=1,
                max_scrolls=0,
                terms=("hiring",),
                detail=True,
            )
            if cycle == 0:
                assert result.status == "DETAIL_RETRY_PENDING"
                assert result.detail_reads == 0
            elif cycle == 1:
                assert result.status == "COMPLETE"
                assert result.detail_reads == 1
            else:
                assert result.detail_reads == 0
            assert page.url == HOME_URL
            checkpoint = store.load(
                account.account_id,
                policy_fingerprint(canonical_terms(("hiring",))),
            )
            if checkpoints:
                assert checkpoint.completed_runs >= checkpoints[-1].completed_runs
                assert checkpoint.detail_reads >= checkpoints[-1].detail_reads
                assert set(checkpoint.seen_fingerprints).issuperset(
                    checkpoints[-1].seen_fingerprints
                )
            checkpoints.append(checkpoint)
        assert checkpoints[0].completed_runs == 1
        assert checkpoints[0].detail_reads == 0
        assert len(checkpoints[0].pending_read_fingerprints) == 1
        assert checkpoints[1].completed_runs == 2
        assert checkpoints[1].detail_reads == 1
        assert checkpoints[1].pending_read_fingerprints == ()
        assert checkpoints[-1].completed_runs >= checkpoints[1].completed_runs

    _run_offline_page(feed_html, repeated_cycles)
    final_checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert final_checkpoint.completed_runs == 10
    assert final_checkpoint.detail_reads == 1
    assert final_checkpoint.pending_read_fingerprints == ()
    assert detail_attempts == 2
    assert "decision=VERIFIED_FALLBACK" in capsys.readouterr().out


def test_completed_fallback_is_not_opened_again_on_unchanged_feed(
    tmp_path: Path,
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/completed-fallback"
    body = "A synthetic weather update."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
  <a href="/@candidate/post/completed-fallback"><time>now</time></a>
  <a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    detail_attempts = 0

    async def serve_detail(route: Route) -> None:
        nonlocal detail_attempts
        detail_attempts += 1
        await route.fulfill(
            status=200,
            content_type="text/html",
            body=detail_html,
        )

    async def first_cycle(page) -> None:
        await page.route(target_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=0,
            terms=("hiring",),
            detail=True,
        )
        assert result.detail_reads == 1
        assert page.url == HOME_URL

    async def second_cycle(page) -> None:
        await page.route(target_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=0,
            terms=("hiring",),
            detail=True,
        )
        assert result.detail_reads == 0
        assert page.url == HOME_URL

    _run_offline_page(feed_html, first_cycle)
    _run_offline_page(feed_html, second_cycle)

    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert detail_attempts == 1
    assert checkpoint.completed_runs == 2
    assert checkpoint.detail_reads == 1
    assert checkpoint.pending_read_fingerprints == ()


def test_q_stops_pending_detail_without_back_or_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/pending-identity"
    body = "Hiring a synthetic target body."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><a href="/@candidate/post/pending-identity"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p></article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    readiness_prompts: list[tuple[str, int]] = []
    detail_requests = 0
    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 400)
    monkeypatch.setattr(reader_module, "_DETAIL_TIMEOUT_MS", 1_000)
    monkeypatch.setattr(reader_module, "_RENDER_SETTLE_INTERVAL_MS", 20)

    async def readiness_prompt(stage: str, retry: int) -> bool:
        readiness_prompts.append((stage, retry))
        return False

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=(
                    '<!doctype html><html><body><main aria-busy="true" '
                    'style="min-height: 1px">Loading</main></body></html>'
                ),
            )

        await page.route(target_url, serve_detail)
        with pytest.raises(UiReadError) as caught:
            await _run_page_workflow_for_test(
                page,
                account,
                store,
                max_posts=1,
                max_scrolls=0,
                terms=("hiring",),
                detail=True,
                readiness_prompt=readiness_prompt,
            )
        assert caught.value.code == "OPERATOR_CANCELLED"
        assert page.url == target_url
        assert detail_requests == 1

    _run_offline_page(feed_html, exercise)
    policy = policy_fingerprint(canonical_terms(("hiring",)))
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.completed_runs == checkpoint.detail_reads == 0
    assert readiness_prompts == [("DETAIL", 2)]
    output = capsys.readouterr().out
    assert "hydration_pending=true" in output
    assert "[BACK] post=1 status=REQUESTED" not in output
    assert "[CHECKPOINT] status=SAVED" not in output
    diagnostics = "\n".join(
        line for line in output.splitlines() if line.startswith("[DETAIL_STATE]")
    )
    assert all(value not in diagnostics for value in (target_url, actor, body, str(profile)))


def test_delayed_home_feed_after_profile_return_is_sampled() -> None:
    actor = "offline.operator"
    feed_html = f"""<!doctype html><html><head><style>
.post-card {{ box-sizing: border-box; height: 760px; padding-top: 80px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a id="profile" aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main></main><script>
const main = document.querySelector("main");
document.querySelector("#profile").addEventListener("click", () => main.replaceChildren());
window.addEventListener("pageshow", () => {{
  const shows = Number(sessionStorage.getItem("home-shows") || "0") + 1;
  sessionStorage.setItem("home-shows", String(shows));
  if (shows >= 3) setTimeout(() => {{
    main.innerHTML = `
      <div class="post-card"><a href="/@candidate/post/delayed-first"><time>2h</time></a>
        <a href="/@candidate">@candidate</a><p>A weather update</p></div>
      <div class="post-card"><a href="/@candidate/post/delayed-second"><time>1h</time></a>
        <a href="/@candidate">@candidate</a><p>Another update</p></div>`;
  }}, 100);
}});
window.addEventListener("wheel", event => {{
  event.preventDefault();
  setTimeout(() => window.scrollBy(0, event.deltaY), 100);
}}, {{ once: true, passive: false }});
</script></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        sample = await reader.sample_and_scroll(2)
        assert reader.scroll_attempts == 1
        assert reader.scrolls == 1
        assert len(sample.observed_targets) >= 1
        assert any("delayed-" in target.url for target in sample.observed_targets)

    _run_offline_page(feed_html, exercise)


def test_transient_home_and_detail_hydration_recover_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    detail_url = "https://www.threads.com/@candidate/post/recovery-detail"
    detail_body = "Hiring a frontend engineer in Vietnam"
    feed_html = f"""<!doctype html><html><head><style>
.post-card {{ box-sizing: border-box; height: 760px; padding-top: 80px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a id="profile" aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main></main><script>
const main = document.querySelector("main");
const renderedFeed = `
  <div class="post-card"><a href="/@candidate/post/older"><time>2h</time></a>
    <a href="/@candidate">@candidate</a><p>A weather update</p></div>
  <div class="post-card"><a href="/@candidate/post/recovery-detail"><time>1h</time></a>
    <a href="/@candidate">@candidate</a><p>{detail_body}</p></div>
`;
window.addEventListener("pageshow", () => {{
  const shows = Number(sessionStorage.getItem("recovery-home-shows") || "0") + 1;
  sessionStorage.setItem("recovery-home-shows", String(shows));
  if (shows >= 3) {{
    if (!sessionStorage.getItem("recovery-feed-scheduled")) {{
      sessionStorage.setItem("recovery-feed-scheduled", "1");
      const profile = document.querySelector("#profile");
      profile.remove();
      setTimeout(() => document.querySelector("nav").append(profile), 15500);
      setTimeout(() => main.innerHTML = renderedFeed, 15500);
    }} else {{
      main.innerHTML = renderedFeed;
    }}
  }}
}});
window.addEventListener("wheel", event => {{
  event.preventDefault();
  const wheels = Number(sessionStorage.getItem("recovery-wheel-count") || "0") + 1;
  sessionStorage.setItem("recovery-wheel-count", String(wheels));
  setTimeout(() => window.scrollBy(0, event.deltaY), 40);
}}, {{ once: true, passive: false }});
</script></body></html>"""
    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 15_000)
    monkeypatch.setattr(reader_module, "_READINESS_RECHECK_INTERVAL_MS", 75)
    monkeypatch.setattr(reader_module, "_DETAIL_TIMEOUT_MS", 2_000)

    with tempfile.TemporaryDirectory(prefix="threads-browser-recovery-once-") as root:
        synthetic_root = Path(root)
        profile = synthetic_root / "profile"
        profile.mkdir()
        account = AccountStore(synthetic_root / "data").add("offline", actor, profile)
        store = CheckpointStore(synthetic_root / "data")
        detail_requests = 0

        async def exercise(page) -> None:
            original_goto = page.goto
            goto_calls = 0

            async def goto_then_timeout(*args, **kwargs) -> None:
                nonlocal goto_calls
                goto_calls += 1
                await original_goto(*args, **kwargs)
                raise PlaywrightTimeoutError("synthetic committed navigation timeout")

            async def serve_detail(route: Route) -> None:
                nonlocal detail_requests
                detail_requests += 1
                await route.fulfill(
                    status=200,
                    content_type="text/html",
                    body=f"""<!doctype html><html><body><main aria-busy="true">
<div data-pressable-container="true" id="detail"></div></main><script>
setTimeout(() => {{
  const detail = document.querySelector("#detail");
  detail.innerHTML = '<header><a href="/@candidate">@candidate</a></header>' +
    '<div><p>{detail_body}</p></div>';
  document.querySelector("main").removeAttribute("aria-busy");
}}, 750);
</script></body></html>""",
                )

            await page.route(detail_url, serve_detail)
            monkeypatch.setattr(page, "goto", goto_then_timeout)
            result = await _run_page_workflow_for_test(
                page,
                account,
                store,
                max_posts=2,
                terms=("hiring", "engineer"),
            )
            assert result.status == "COMPLETE"
            assert result.scroll_attempts == 1
            assert result.scrolls == 1
            assert result.detail_reads == 1
            assert goto_calls == 1
            assert await page.evaluate(
                "sessionStorage.getItem('recovery-wheel-count')"
            ) == "1"

        _run_offline_page(feed_html, exercise)
        policy = policy_fingerprint(canonical_terms(("hiring", "engineer")))
        checkpoint = store.load(account.account_id, policy)
        assert checkpoint.completed_runs == 1
        assert checkpoint.scroll_attempts == 1
        assert checkpoint.scrolls == 1
        assert checkpoint.detail_reads == 1
        assert detail_requests == 1
        output = capsys.readouterr().out
        assert "WAITING stage=HOME_RETURN retry=1" in output
        assert "RECOVERED stage=HOME_RETURN retry=1" in output
        assert "WAITING stage=DETAIL reason=RENDER_PENDING" in output
        assert "READY stage=DETAIL state=DETAIL_VERIFIED" in output


def test_observation_rechecks_readiness_missed_before_wait_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    url = "https://www.threads.com/@candidate/post/boundary-ready"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main></main></body></html>"""
    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 1_500)
    monkeypatch.setattr(reader_module, "_READINESS_RECHECK_INTERVAL_MS", 100)

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()

        async def inject_delayed_post() -> None:
            await asyncio.sleep(0.05)
            await page.locator("main").evaluate(
                """main => {
                    main.innerHTML = `<div>
                      <a href="/@candidate/post/boundary-ready">Permalink</a>
                      <a href="/@candidate">@candidate</a>
                      <p>A weather update</p>
                    </div>`;
                }"""
            )

        injection = asyncio.create_task(inject_delayed_post())
        started = asyncio.get_running_loop().time()
        targets, _ = await reader._wait_for_home_targets(
            1,
            timeout_code="FEED_INITIAL_NOT_READY",
        )
        elapsed = asyncio.get_running_loop().time() - started
        await injection
        assert elapsed < 3.5
        assert len(targets) == 1
        assert targets[0].url == url

    _run_offline_page(feed_html, exercise)


def test_streamed_row_survives_discovery_deadline_overrun_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    body = "Synthetic weather update remains emitted after slow discovery."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
  <a href="/@candidate/post/slow-discovery"><time>1h</time></a>
  <a href="/@candidate">@candidate</a><p>{body}</p>
</article></main></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 250)
        original_discovery = reader._top_level_candidate_roots

        async def slow_discovery(region, viewport):
            await asyncio.sleep(0.4)
            return await original_discovery(region, viewport)

        monkeypatch.setattr(reader, "_top_level_candidate_roots", slow_discovery)
        emitted: list[str] = []

        async def capture(row) -> None:
            if row.target is not None and row.target.body is not None:
                emitted.append(row.target.body)

        targets, _ = await reader._wait_for_home_targets(
            1,
            timeout_code="FEED_INITIAL_NOT_READY",
            currently_visible=True,
            on_row=capture,
        )
        assert [target.body for target in targets] == [body]
        assert emitted == [body]
        assert reader.last_scan_timings.discover_ms >= 350

    _run_offline_page(feed_html, exercise)


def test_viewport_scan_cancellation_disposes_candidate_handles() -> None:
    actor = "offline.operator"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
  <a href="/@candidate/post/cancel-scan"><time>1h</time></a>
  <a href="/@candidate">@candidate</a><p>A synthetic weather update.</p>
</article></main></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        handles = []

        async def cancel_after_first_candidate(row) -> None:
            if row.candidate:
                active = reader._active_scan_candidate_handles
                assert active is not None
                handles.extend(active.values())
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await reader.scan_viewport(1, on_row=cancel_after_first_candidate)

        assert handles
        assert reader._active_scan_candidate_handles is None
        with pytest.raises(PlaywrightError):
            await handles[0].evaluate("node => node.isConnected")

    _run_offline_page(feed_html, exercise)


def test_attended_wait_can_extend_then_stop_without_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main></main></body></html>"""
    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 500)
    monkeypatch.setattr(reader_module, "_READINESS_RECHECK_INTERVAL_MS", 30)

    with tempfile.TemporaryDirectory(prefix="threads-browser-recovery-stop-") as root:
        synthetic_root = Path(root)
        profile = synthetic_root / "profile"
        profile.mkdir()
        account = AccountStore(synthetic_root / "data").add("offline", actor, profile)
        store = CheckpointStore(synthetic_root / "data")
        prompt_calls: list[tuple[str, int]] = []

        async def readiness_prompt(stage: str, retry: int) -> bool:
            prompt_calls.append((stage, retry))
            return len(prompt_calls) == 1

        async def exercise(page) -> None:
            with pytest.raises(UiReadError) as caught:
                await _run_page_workflow_for_test(
                    page,
                    account,
                    store,
                    max_posts=1,
                    terms=("hiring",),
                    readiness_prompt=readiness_prompt,
                )
            assert caught.value.code == "FEED_INITIAL_NOT_READY"
            assert prompt_calls == [("FEED_INITIAL", 2), ("FEED_INITIAL", 3)]

        _run_offline_page(feed_html, exercise)
        policy = policy_fingerprint(canonical_terms(("hiring",)))
        checkpoint = store.load(account.account_id, policy)
        assert checkpoint.completed_runs == 0
        assert checkpoint.scroll_attempts == 0
        assert checkpoint.scrolls == 0
        assert checkpoint.detail_reads == 0


def test_delayed_wheel_and_detail_hydration_reports_changed_return_context(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    detail_url = "https://www.threads.com/@candidate/post/ready-detail"
    detail_body = "Hiring a frontend engineer in Vietnam"
    monkeypatch.setattr(reader_module, "_RENDER_MIN_DWELL_MS", 250)
    monkeypatch.setattr(reader_module, "_RENDER_SETTLE_INTERVAL_MS", 100)
    feed_html = f"""<!doctype html><html><head><style>
.post-card {{ box-sizing: border-box; height: 760px; padding-top: 20px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a id="profile" aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
  <div class="post-card"><a href="/@candidate/post/first"><time>2h</time></a>
    <a href="/@candidate">@candidate</a><p>A weather update</p></div>
  <div id="scroll-room" style="height: 400px"></div>
</main><script>
const main = document.querySelector("main");
const scrollRoom = document.querySelector("#scroll-room");
const delayedCard = `<div class="post-card">
  <a href="/@candidate/post/ready-detail"><time>1h</time></a>
  <a href="/@candidate">@candidate</a><p>{detail_body}</p></div>`;
window.addEventListener("wheel", event => {{
  event.preventDefault();
  setTimeout(() => {{
    window.scrollBy(0, 360);
    setTimeout(() => scrollRoom.insertAdjacentHTML("beforebegin", delayedCard), 100);
  }}, 100);
}}, {{ once: true, passive: false }});
const restoreFeed = () => main.innerHTML = `
  <div class="post-card"><a href="/@candidate/post/first"><time>2h</time></a>
    <a href="/@candidate">@candidate</a><p>A weather update</p></div>
  <div class="post-card"><a href="/@candidate/post/ready-detail"><time>1h</time></a>
    <a href="/@candidate">@candidate</a><p>{detail_body}</p></div>`;
if (sessionStorage.getItem("return-feed") === "cleared") main.replaceChildren();
window.addEventListener("pageshow", () => {{
  if (sessionStorage.getItem("return-feed") === "cleared") {{
    sessionStorage.setItem("return-feed", "rendering");
    setTimeout(restoreFeed, 300);
  }}
}});
document.querySelector("#profile").addEventListener("click", () => {{
  if (sessionStorage.getItem("return-feed") === "armed") {{
    main.replaceChildren();
    sessionStorage.setItem("return-feed", "cleared");
  }} else {{
    sessionStorage.setItem("return-feed", "armed");
  }}
}});
</script></body></html>"""

    async def exercise(page) -> None:
        detail_requests = 0

        async def serve_detail(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=f"""<!doctype html><html><body><main><div id="detail">
<a href="/@candidate/post/ready-detail"><time>1h</time></a>
<script>
setTimeout(() => document.querySelector("#detail").insertAdjacentHTML(
  "beforeend", '<a href="/@candidate">@candidate</a><p>{detail_body}</p>'
), 100);
</script></div></main></body></html>""",
            )

        await page.route(detail_url, serve_detail)
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        sample = await reader.sample_and_scroll(2)
        target = next(target for target in sample.targets if target.url == detail_url)
        assert target.body == detail_body
        detail = await reader.read_detail(target)
        assert detail.url == detail_url
        assert detail.author == "candidate"
        assert detail.body == detail_body
        with pytest.raises(UiReadError) as caught:
            await reader.return_to_home()
        assert caught.value.code == "RETURN_CONTEXT_CHANGED"
        assert page.url == HOME_URL
        assert detail_requests == 1

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "READY stage=FEED_SCROLL state=BODY_EXTRACTED" in output
    assert "READY stage=DETAIL state=DETAIL_VERIFIED" in output
    assert "READY stage=HOME_RETURN state=UI_SHELL_SETTLED" in output


def test_empty_scan_rescans_new_feed_but_changed_back_anchor_stops_without_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/late-feed"
    body = "Hiring a backend engineer"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a id="profile" aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article id="seed">
  <a href="/@candidate/post/seed"><time>2h</time></a>
  <a href="/@candidate">@candidate</a><p>A weather update</p>
</article></main></body></html>"""
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate/post/late-feed"><time>now</time></a>
<a href="/@candidate">@candidate</a><p>{body}</p>
</article></main></body></html>"""
    state = {"scan": False, "empty_once": False, "injected": False}

    with tempfile.TemporaryDirectory(prefix="threads-browser-render-race-") as root:
        synthetic_root = Path(root)
        profile = synthetic_root / "profile"
        profile.mkdir()
        account = AccountStore(synthetic_root / "data").add(
            "offline",
            actor,
            profile,
        )
        store = CheckpointStore(synthetic_root / "data")
        policy = policy_fingerprint(canonical_terms(("hiring",)))

        original_open_home = ThreadsHomeReader.open_home
        original_visible_targets = ThreadsHomeReader._visible_targets
        original_feed_evidence = ThreadsHomeReader._visible_feed_evidence

        async def open_then_clear_seed(reader: ThreadsHomeReader) -> None:
            await original_open_home(reader)

            async def serve_detail(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="text/html",
                    body=detail_html,
                )

            await reader._page.route(target_url, serve_detail)
            await reader._page.locator("#seed").evaluate("element => element.remove()")
            state["scan"] = True

        async def empty_once(
            reader: ThreadsHomeReader,
            *args,
            **kwargs,
        ):
            if state["scan"] and not state["empty_once"]:
                state["empty_once"] = True
                return reader_module._VisibleTargets((), None, (), 0, True, ())
            return await original_visible_targets(reader, *args, **kwargs)

        async def inject_after_empty_scan(reader: ThreadsHomeReader) -> bool:
            if state["scan"] and state["empty_once"] and not state["injected"]:
                state["injected"] = True
                await reader._page.locator("main").evaluate(
                    """main => main.innerHTML = `
                      <article>
                        <a href="/@candidate/post/late-feed"><time>now</time></a>
                        <a href="/@candidate">@candidate</a>
                        <p>Hiring a backend engineer</p>
                      </article>`"""
                )
            return await original_feed_evidence(reader)

        monkeypatch.setattr(ThreadsHomeReader, "open_home", open_then_clear_seed)
        monkeypatch.setattr(ThreadsHomeReader, "_visible_targets", empty_once)
        monkeypatch.setattr(
            ThreadsHomeReader,
            "_visible_feed_evidence",
            inject_after_empty_scan,
        )

        async def exercise(page) -> None:
            with pytest.raises(UiReadError) as caught:
                await _run_page_workflow_for_test(
                    page,
                    account,
                    store,
                    max_posts=1,
                    max_scrolls=0,
                    terms=("hiring",),
                    detail=True,
                )
            assert caught.value.code == "RETURN_CONTEXT_CHANGED"
            assert page.url == HOME_URL
            assert await page.locator('a[href$="/late-feed"]').count() == 0
            assert await page.locator('a[href$="/seed"]').count() == 1

        _run_offline_page(feed_html, exercise)
        output = capsys.readouterr().out
        assert "[BACK] post=1 status=FAILED anchor=CHANGED code=RETURN_CONTEXT_CHANGED" in output
        assert "[CHECKPOINT] status=SAVED" not in output
        checkpoint = store.load(account.account_id, policy)
        assert checkpoint.completed_runs == 0
        assert checkpoint.scroll_attempts == 0
        assert checkpoint.detail_reads == 0
        assert state == {"scan": True, "empty_once": True, "injected": True}


def test_feed_barrier_waits_through_body_hydration_and_dom_change(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/body-hydration"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
  <a href="/@candidate/post/seed"><time>2h</time></a>
  <a href="/@candidate">@candidate</a><p>A weather update</p>
</article></main></body></html>"""
    monkeypatch.setattr(reader_module, "_RENDER_MIN_DWELL_MS", 80)
    monkeypatch.setattr(reader_module, "_RENDER_SETTLE_INTERVAL_MS", 50)

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        await page.locator("main").evaluate(
            """main => {
                main.innerHTML = `
                  <article id="hydrating">
                    <a href="/@candidate/post/body-hydration"><time>now</time></a>
                    <a id="author" href="/@candidate">@candidate</a>
                  </article>`;
                setTimeout(() => document.querySelector('#hydrating')
                    .insertAdjacentHTML('beforeend', '<p>A weather update</p>'), 90);
                setTimeout(() => document.querySelector('#hydrating p').textContent =
                    'Hiring a backend engineer', 175);
            }"""
        )
        reader._mark_ui_transition()
        started = time.monotonic()
        targets = await reader.scan_viewport(1)
        elapsed = time.monotonic() - started
        assert len(targets) == 1
        assert targets[0].url == target_url
        assert targets[0].body == "Hiring a backend engineer"
        assert targets[0].body_available
        assert classify_body(targets[0].body, canonical_terms(("hiring",))) == "RELEVANT"
        assert elapsed >= 0.18

    _run_offline_page(feed_html, exercise)
    assert "READY stage=FEED_INITIAL state=BODY_EXTRACTED" in capsys.readouterr().out


def test_failed_selected_detail_saves_home_and_keeps_retry_pending(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/pending-detail"
    relevant_body = "Hiring a frontend engineer"
    feed_html = f"""<!doctype html><html><head><style>
.post-card {{ box-sizing: border-box; height: 760px; padding-top: 80px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
  <div class="post-card"><a href="/@candidate/post/first"><time>2h</time></a>
    <a href="/@candidate">@candidate</a><p>A weather update</p></div>
  <div class="post-card"><a href="/@candidate/post/pending-detail"><time>1h</time></a>
    <a href="/@candidate">@candidate</a><p>{relevant_body}</p></div>
</main><script>
window.addEventListener("wheel", event => {{
  event.preventDefault();
  setTimeout(() => window.scrollBy(0, event.deltaY), 40);
}}, {{ once: true, passive: false }});
</script></body></html>"""
    async def timeout_exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=(
                    '<!doctype html><html><body><main><div>'
                    '<a href="/@candidate/post/pending-detail"><time>1h</time></a>'
                    '<a href="/@candidate">@candidate</a>'
                    "</div></main></body></html>"
                ),
            )

        await page.route(target_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            terms=("hiring", "engineer"),
        )
        assert result.status == "DETAIL_RETRY_PENDING"
        assert result.detail_reads == 0
        assert page.url == HOME_URL

    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 5_000)
    monkeypatch.setattr(reader_module, "_DETAIL_TIMEOUT_MS", 1_000)
    with tempfile.TemporaryDirectory(prefix="threads-browser-readiness-") as synthetic_root:
        synthetic_path = Path(synthetic_root)
        data_root = synthetic_path / "data"
        profile = synthetic_path / "profile"
        profile.mkdir()
        account = AccountStore(data_root).add("offline", actor, profile)
        store = CheckpointStore(data_root)
        _run_offline_page(feed_html, timeout_exercise)
        policy = policy_fingerprint(canonical_terms(("hiring", "engineer")))
        checkpoint = store.load(account.account_id, policy)
        assert checkpoint.completed_runs == 1
        assert store.load(account.account_id, policy).detail_reads == 0
        assert len(checkpoint.pending_read_fingerprints) == 1

    malformed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
  <a href="/@candidate/post/one"><time>2h</time></a>
  <a href="/@quoted/post/two"><time>1h</time></a>
  <a href="/@candidate">@candidate</a><a href="/@quoted">@quoted</a>
<p>Two identities in one card.</p>
</article></main><script>window.wheelCount = 0;
window.addEventListener("wheel", () => window.wheelCount++);</script></body></html>"""

    provenance_prompts: list[tuple[str, int]] = []

    async def provenance_prompt(stage: str, retry: int) -> bool:
        provenance_prompts.append((stage, retry))
        return False

    async def malformed_exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            terms=("hiring",),
            readiness_prompt=provenance_prompt,
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.observed_rows == 1
        assert result.detail_reads == result.scroll_attempts == 0
        assert result.checkpoint_runs == 1
        assert result.checkpoint_saved is True
        assert await page.evaluate("window.wheelCount") == 0
        assert provenance_prompts == []

    with monkeypatch.context() as short_recovery:
        short_recovery.setattr(reader_module, "_READINESS_TIMEOUT_MS", 15_000)
        short_recovery.setattr(reader_module, "_READINESS_RECHECK_INTERVAL_MS", 20)
        short_recovery.setattr(reader_module, "_RENDER_MIN_DWELL_MS", 100)
        short_recovery.setattr(reader_module, "_RENDER_SETTLE_INTERVAL_MS", 40)
        _run_offline_page(malformed_html, malformed_exercise)
    policy = policy_fingerprint(canonical_terms(("hiring",)))
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 0
    assert checkpoint.scroll_attempts == 0
    assert checkpoint.scrolls == 0
    output = capsys.readouterr().out
    assert "OBSERVED stage=FEED_INITIAL state=OBSERVATION_UNCERTAIN" in output
    assert "VIEWPORT_COVERAGE_INCOMPLETE viewport=1 observed_rows=1 cap=2" in output
    assert "Two identities in one card" not in output

    no_movement_html = f"""<!doctype html><html><head><style>
.post-card {{ box-sizing: border-box; height: 1000px; padding-top: 80px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
  <div class="post-card"><a href="/@candidate/post/first"><time>2h</time></a>
    <a href="/@candidate">@candidate</a><p>A weather update</p></div>
  <div class="post-card"><a href="/@candidate/post/second"><time>1h</time></a>
    <a href="/@candidate">@candidate</a><p>Another update</p></div>
</main></body></html>"""

    async def no_movement_exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()

        async def wheel_without_movement(_delta_x: float, _delta_y: float) -> None:
            return None

        monkeypatch.setattr(page.mouse, "wheel", wheel_without_movement)
        monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 5_000)
        monkeypatch.setattr(reader_module, "_RENDER_MIN_DWELL_MS", 100)
        with pytest.raises(UiReadError) as caught:
            await reader.sample_and_scroll(2)
        assert caught.value.code == "HOME_SCROLL_UNVERIFIED"
        assert reader.scroll_attempts == 1
        assert reader.scrolls == 0

    _run_offline_page(no_movement_html, no_movement_exercise)


def test_detached_scroll_anchor_without_feed_displacement_stops_without_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    feed_html = f"""<!doctype html><html><head><style>
.post-card {{ box-sizing: border-box; height: 760px; padding-top: 80px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><div class="post-card" id="old-card">
  <a href="/@candidate/post/stable"><time>2h</time></a>
  <a href="/@candidate">@candidate</a><p>A weather update</p>
</div></main><script>
window.addEventListener("wheel", event => {{
  event.preventDefault();
  const oldCard = document.querySelector("#old-card");
  oldCard.replaceWith(oldCard.cloneNode(true));
}}, {{ once: true, passive: false }});
</script></body></html>"""
    readers: list[ThreadsHomeReader] = []

    class TrackingReader(ThreadsHomeReader):
        def __init__(
            self,
            page,
            expected_actor: str,
            *,
            readiness_prompt=None,
            detail_mode: bool = False,
        ) -> None:
            super().__init__(
                page,
                expected_actor,
                readiness_prompt=readiness_prompt,
                detail_mode=detail_mode,
            )
            readers.append(self)

    monkeypatch.setattr(workflow, "ThreadsHomeReader", TrackingReader)
    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 15_000)
    with tempfile.TemporaryDirectory(prefix="threads-browser-detached-scroll-") as root:
        synthetic_root = Path(root)
        profile = synthetic_root / "profile"
        profile.mkdir()
        account = AccountStore(synthetic_root / "data").add("offline", actor, profile)
        store = CheckpointStore(synthetic_root / "data")

        async def exercise(page) -> None:
            with pytest.raises(UiReadError) as caught:
                await _run_page_workflow_for_test(
                    page,
                    account,
                    store,
                    max_posts=2,
                    terms=("hiring",),
                )
            assert caught.value.code == "HOME_SCROLL_UNVERIFIED"

        _run_offline_page(feed_html, exercise)
        policy = policy_fingerprint(canonical_terms(("hiring",)))
        checkpoint = store.load(account.account_id, policy)
        assert checkpoint.completed_runs == 0
        assert checkpoint.scrolls == 0
        assert readers[0].scroll_attempts == 1
        assert readers[0].scrolls == 0


def test_detail_return_with_inactive_for_you_stops_without_checkpoint() -> None:
    actor = "offline.operator"
    detail_url = "https://www.threads.com/@candidate/post/return-check"
    detail_body = "Hiring a frontend engineer in Vietnam"
    feed_html = f"""<!doctype html><html><head><style>
.post-card {{ box-sizing: border-box; height: 760px; padding-top: 80px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a id="for-you" aria-current="page" aria-label="For you" href="/">For you</a>
  <a id="profile" aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
  <div class="post-card"><a href="/@candidate/post/first"><time>2h</time></a>
    <a href="/@candidate">@candidate</a><p>A weather update</p></div>
  <div class="post-card"><a href="/@candidate/post/return-check"><time>1h</time></a>
    <a href="/@candidate">@candidate</a><p>{detail_body}</p></div>
</main><script>
window.addEventListener("wheel", event => {{
  event.preventDefault();
  setTimeout(() => window.scrollBy(0, event.deltaY), 40);
}}, {{ once: true, passive: false }});
const deactivateForYou = () => {{
  const control = document.querySelector("#for-you");
  control.removeAttribute("aria-current");
  control.setAttribute("aria-selected", "false");
}};
document.querySelectorAll('a[href*="/post/"]').forEach(link => {{
  link.addEventListener("click", () => {{
    sessionStorage.setItem("deactivate-for-you-on-back", "1");
  }});
}});
window.addEventListener("pageshow", () => {{
  if (location.pathname === "/" &&
      sessionStorage.getItem("deactivate-for-you-on-back") === "1") {{
    deactivateForYou();
    sessionStorage.removeItem("deactivate-for-you-on-back");
  }}
}});
</script></body></html>"""

    with tempfile.TemporaryDirectory(prefix="threads-browser-return-tab-") as root:
        synthetic_root = Path(root)
        profile = synthetic_root / "profile"
        profile.mkdir()
        account = AccountStore(synthetic_root / "data").add("offline", actor, profile)
        store = CheckpointStore(synthetic_root / "data")

        async def exercise(page) -> None:
            async def serve_detail(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="text/html",
                    body=f"""<!doctype html><html><body><main><div>
<a href="/@candidate/post/return-check"><time>1h</time></a>
<a href="/@candidate">@candidate</a><p>{detail_body}</p>
</div></main></body></html>""",
                )

            await page.route(detail_url, serve_detail)
            with pytest.raises(UiReadError) as caught:
                await _run_page_workflow_for_test(
                    page,
                    account,
                    store,
                    max_posts=2,
                    terms=("hiring", "engineer"),
                )
            assert caught.value.code == "FOR_YOU_STATE_CHANGED"
            assert await page.get_by_text(detail_body, exact=True).is_visible()

        _run_offline_page(feed_html, exercise)
        policy = policy_fingerprint(canonical_terms(("hiring", "engineer")))
        checkpoint = store.load(account.account_id, policy)
        assert checkpoint.completed_runs == 0
        assert checkpoint.detail_reads == 0
        assert checkpoint.scrolls == 0


def test_home_waits_for_delayed_strict_card_hydration() -> None:
    actor = "offline.operator"
    first_url = "https://www.threads.com/@candidate/post/hydrated-first"
    second_url = "https://www.threads.com/@candidate/post/hydrated-second"
    body = "Hiring a frontend engineer"
    feed_html = f"""<!doctype html><html><head><style>
.post-card {{ box-sizing: border-box; height: 760px; padding-top: 80px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a id="profile" aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
  <div class="post-card" id="hydrating">
    <a href="/@candidate/post/hydrated-first"><time>2h</time></a>
    <a href="/@">Loading</a>
  </div>
  <div class="post-card">
    <a href="/@candidate/post/hydrated-second"><time>1h</time></a>
    <a href="/@candidate">@candidate</a><p>A weather update</p>
  </div>
</main><script>
const hydrate = () => {{
  const card = document.querySelector("#hydrating");
  if (card.querySelector('a[href="/@candidate"]')) return;
  card.querySelector('a[href="/@"]').remove();
  card.insertAdjacentHTML("beforeend", '<a href="/@candidate">@candidate</a>'
    + '<p>{body}</p>');
}};
window.addEventListener("pageshow", () => {{
  const shows = Number(sessionStorage.getItem("home-shows") || "0") + 1;
  sessionStorage.setItem("home-shows", String(shows));
  if (shows >= 2) setTimeout(hydrate, 300);
}});
window.addEventListener("wheel", event => {{
  event.preventDefault();
  setTimeout(() => window.scrollBy(0, event.deltaY), 40);
}}, {{ once: true, passive: false }});
</script></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        sample = await reader.sample_and_scroll(2)
        first = next(target for target in sample.observed_targets if target.url == first_url)
        assert first.body == body
        assert any(target.url == second_url for target in sample.observed_targets)
        assert reader.scrolls == 1

    _run_offline_page(feed_html, exercise)


def test_home_readiness_skips_offscreen_permalink_for_visible_valid_card() -> None:
    actor = "offline.operator"
    visible_url = "https://www.threads.com/@candidate/post/visible"
    feed_html = f"""<!doctype html><html><head><style>
main {{ position: relative; height: 1400px; }}
.post-card {{ box-sizing: border-box; position: absolute; width: 100%; height: 320px; }}
#offscreen {{ top: 1000px; }}
#visible {{ top: 80px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
  <div class="post-card" id="offscreen">
    <a href="/@candidate/post/offscreen"><time>3h</time></a>
  </div>
  <div class="post-card" id="visible">
    <a href="/@candidate/post/visible"><time>1h</time></a>
    <a href="/@candidate">@candidate</a><p>A weather update</p>
  </div>
</main></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        targets, _ = await reader._wait_for_home_targets(
            1,
            timeout_code="FEED_INITIAL_NOT_READY",
        )
        assert len(targets) == 1
        assert targets[0].url == visible_url
        assert targets[0].author == "candidate"

    _run_offline_page(feed_html, exercise)


def test_initial_feed_readiness_deadline_stops_without_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    feed_html = f"""<!doctype html><html><head><style>
.post-card {{ box-sizing: border-box; height: 760px; padding-top: 80px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><div class="post-card">
  <a href="/@candidate/post/never-ready"><time>1h</time></a>
  <a href="/@">Loading</a>
</div></main></body></html>"""
    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 300)

    with tempfile.TemporaryDirectory(prefix="threads-browser-feed-deadline-") as root:
        synthetic_root = Path(root)
        profile = synthetic_root / "profile"
        profile.mkdir()
        account = AccountStore(synthetic_root / "data").add("offline", actor, profile)
        store = CheckpointStore(synthetic_root / "data")

        async def exercise(page) -> None:
            with pytest.raises(UiReadError) as caught:
                await _run_page_workflow_for_test(
                    page,
                    account,
                    store,
                    max_posts=1,
                    terms=("hiring",),
                )
            assert caught.value.code == "FEED_INITIAL_NOT_READY"

        _run_offline_page(feed_html, exercise)
        policy = policy_fingerprint(canonical_terms(("hiring",)))
        checkpoint = store.load(account.account_id, policy)
        assert checkpoint.completed_runs == 0
        assert checkpoint.home_sampled == 0
        assert checkpoint.scroll_attempts == 0
        assert checkpoint.scrolls == 0


def test_mixed_candidate_rows_do_not_block_a_verified_selected_post(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    clean_url = "https://www.threads.com/@candidate/post/clean"
    clean_body = "Hiring a frontend engineer in Vietnam"
    html = """<!doctype html><html><head><style>
.post-card { box-sizing: border-box; height: 360px; }
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@offline.operator">Profile</a>
</nav>
<main><div class="post-card" id="noncanonical-card">
  <a href="/@candidate/post/noncanonical?share=1"><time>3h</time></a>
  <a href="/@candidate">@candidate</a><p>A malformed candidate that must stay unread.</p>
</div><div class="post-card mixed-card">
  <a href="/@candidate/post/first"><time>2h</time></a>
  <a href="/@candidate">@candidate</a>
  <a href="/@quoted/post/second"><time>1h</time></a>
  <a href="/@quoted">@quoted</a>
  <p>A quoted candidate that must stay unread.</p>
</div><div class="post-card" id="clean-card">
  <a href="/@candidate/post/clean"><time>1h</time></a>
  <a href="/@candidate">@candidate</a><p>Hiring a frontend engineer in Vietnam</p>
</div></main><script>
window.addEventListener("wheel", event => {
  event.preventDefault();
  const card = event.target.closest(".post-card");
  sessionStorage.setItem("wheel-target", card?.id || "mixed-card");
  setTimeout(() => window.scrollBy(0, event.deltaY), 40);
}, { once: true, passive: false });
</script></body></html>"""

    with tempfile.TemporaryDirectory(prefix="threads-browser-mixed-skip-") as root:
        synthetic_root = Path(root)
        profile = synthetic_root / "profile"
        profile.mkdir()
        account = AccountStore(synthetic_root / "data").add("offline", actor, profile)
        store = CheckpointStore(synthetic_root / "data")
        clean_detail_requests = 0
        rejected_detail_requests = 0

        async def exercise(page) -> None:
            async def serve_clean_detail(route: Route) -> None:
                nonlocal clean_detail_requests
                clean_detail_requests += 1
                await route.fulfill(
                    status=200,
                    content_type="text/html",
                    body=f"""<!doctype html><html><body><main><div>
<a href="/@candidate/post/clean"><time>1h</time></a>
<a href="/@candidate">@candidate</a><p>{clean_body}</p>
</div></main></body></html>""",
                )

            async def reject_mixed_detail(route: Route) -> None:
                nonlocal rejected_detail_requests
                rejected_detail_requests += 1
                await route.abort()

            await page.route(clean_url, serve_clean_detail)
            await page.route("**/@candidate/post/first", reject_mixed_detail)
            await page.route("**/@quoted/post/second", reject_mixed_detail)
            result = await _run_page_workflow_for_test(
                page,
                account,
                store,
                max_posts=3,
                terms=("hiring", "engineer"),
            )
            assert result.status == "COMPLETE_WITH_SKIPS"
            assert result.home_sampled == 1
            assert result.detail_reads == 1
            assert result.scroll_attempts == result.scrolls > 0
            assert not result.viewport_coverage_complete
            assert await page.evaluate("sessionStorage.getItem('wheel-target')") is not None

        _run_offline_page(html, exercise)
        policy = policy_fingerprint(canonical_terms(("hiring", "engineer")))
        checkpoint = store.load(account.account_id, policy)
        assert checkpoint.completed_runs == 1
        assert checkpoint.home_sampled == 1
        assert len(checkpoint.seen_fingerprints) == 1
        assert checkpoint.detail_reads == 1
        assert clean_detail_requests == 1
        assert rejected_detail_requests == 0
        output = capsys.readouterr().out
        assert "SKIPPED stage=FEED_INITIAL reason=CANDIDATE_NONCANONICAL count=1" in output
        assert "VIEWPORT_COVERAGE_INCOMPLETE" in output
        assert "quoted candidate" not in output
        assert "malformed candidate" not in output


def test_detail_author_provenance_mismatch_returns_home_with_retry_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/selected"
    body = "Hiring a frontend engineer"
    html = f"""<!doctype html><html><head><style>
.post-card {{ box-sizing: border-box; height: 900px; padding-top: 280px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><div class="post-card">
<a href="/@candidate/post/selected"><time>1h</time></a>
<a href="/@candidate">@candidate</a><p>{body}</p>
</div></main><script>
sessionStorage.setItem("wheelCount", "0");
window.addEventListener("wheel", event => {{
  sessionStorage.setItem(
    "wheelCount",
    String(Number(sessionStorage.getItem("wheelCount") || "0") + 1)
  );
  event.preventDefault();
  setTimeout(() => window.scrollBy(0, event.deltaY), 40);
}}, {{ once: true, passive: false }});
</script></body></html>"""
    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 15_000)
    monkeypatch.setattr(reader_module, "_READINESS_RECHECK_INTERVAL_MS", 50)
    monkeypatch.setattr(reader_module, "_DETAIL_TIMEOUT_MS", 1_000)

    with tempfile.TemporaryDirectory(prefix="threads-browser-detail-provenance-") as root:
        synthetic_root = Path(root)
        profile = synthetic_root / "profile"
        profile.mkdir()
        account = AccountStore(synthetic_root / "data").add("offline", actor, profile)
        store = CheckpointStore(synthetic_root / "data")
        detail_requests = 0
        readiness_prompts: list[tuple[str, int]] = []

        async def readiness_prompt(stage: str, retry: int) -> bool:
            readiness_prompts.append((stage, retry))
            return False

        async def exercise(page) -> None:
            async def serve_mismatched_detail(route: Route) -> None:
                nonlocal detail_requests
                detail_requests += 1
                await route.fulfill(
                    status=200,
                    content_type="text/html",
                    body=f"""<!doctype html><html><body><main><div>
<a href="/@candidate/post/selected"><time>1h</time></a>
<a href="/@other">@other</a><p>{body}</p>
</div></main></body></html>""",
                )

            await page.route(target_url, serve_mismatched_detail)
            result = await _run_page_workflow_for_test(
                page,
                account,
                store,
                max_posts=1,
                max_scrolls=0,
                terms=("hiring",),
                readiness_prompt=readiness_prompt,
            )
            state = await page.evaluate(
                "() => ({wheel: sessionStorage.getItem('wheelCount'), scrollY: window.scrollY, "
                "scrollHeight: document.documentElement.scrollHeight})"
            )
            assert result.status == "DETAIL_RETRY_PENDING", state
            assert result.detail_reads == 0
            assert result.checkpoint_runs == 1
            assert page.url == HOME_URL
            assert await page.evaluate("sessionStorage.getItem('wheelCount')") == "0"

        _run_offline_page(html, exercise)
        policy = policy_fingerprint(canonical_terms(("hiring",)))
        checkpoint = store.load(account.account_id, policy)
        assert checkpoint.completed_runs == 1
        assert checkpoint.detail_reads == 0
        assert len(checkpoint.pending_read_fingerprints) == 1
        assert detail_requests == 1
        assert readiness_prompts == []
        output = capsys.readouterr().out
        assert "terminal_reason=AUTHOR_CONFLICT" in output
        assert "@other" not in output


def test_virtualized_detail_target_is_rediscovered_before_locator_click() -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/virtualized"
    body = "Synthetic fixture post survives virtualized DOM replacement."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article id="virtualized">
<a href="/@candidate/post/virtualized"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    detail_requests = 0

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        target = (await reader.scan_viewport(1))[0]
        original_link = await target.permalink.element_handle()
        assert original_link is not None
        await page.locator("#virtualized").evaluate(
            "node => node.replaceWith(node.cloneNode(true))"
        )
        assert not await original_link.evaluate("node => node.isConnected")
        await original_link.dispose()

        async def serve_detail(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=feed_html,
            )

        await page.route(target_url, serve_detail)
        detail = await reader.read_detail(target)
        assert detail.url == target_url
        assert detail.body == body
        assert detail_requests == 1

    _run_offline_page(feed_html, exercise)


def test_virtualized_replacement_with_new_identity_is_never_clicked() -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/virtualized-old"
    replacement_url = "https://www.threads.com/@candidate/post/virtualized-new"
    body = "Synthetic fixture original post body."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article id="virtualized">
<a href="/@candidate/post/virtualized-old"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    detail_requests = 0

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        target = (await reader.scan_viewport(1))[0]
        await page.locator("#virtualized").evaluate(
            """node => {
                const replacement = node.cloneNode(true);
                replacement.innerHTML = replacement.innerHTML
                    .replaceAll("virtualized-old", "virtualized-new")
                    .replace("Synthetic fixture original post body.",
                        "Synthetic fixture replacement post body.");
                node.replaceWith(replacement);
            }"""
        )

        async def serve_stale_target(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=feed_html,
            )

        await page.route(target_url, serve_stale_target)
        with pytest.raises(UiReadError) as caught:
            await reader.read_detail(target)
        assert caught.value.code == "DETAIL_NAVIGATION_UNVERIFIED"
        assert page.url.endswith("/")
        assert replacement_url not in page.url
        assert await page.locator("#virtualized").inner_text() != ""
        assert detail_requests == 0

    _run_offline_page(feed_html, exercise)


def test_replaced_home_row_gets_a_new_observation_id() -> None:
    actor = "offline.operator"
    original_url = "https://www.threads.com/@candidate/post/identity-old"
    replacement_url = "https://www.threads.com/@candidate/post/identity-new"
    original_body = "Synthetic fixture original row body."
    replacement_body = "Synthetic fixture replacement row body."
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article id="row">
<a href="/@candidate/post/identity-old"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{original_body}</p>
</article></main></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        original_targets = await reader.scan_viewport(1)
        assert len(original_targets) == 1
        original = original_targets[0]
        old_diagnostic = next(
            row
            for row in reader.last_candidate_diagnostics
            if row.identity_fingerprint == original.fingerprint
        )
        original_observation_id = old_diagnostic.observation_id

        await page.locator("#row").evaluate(
            """node => {
                const inserted = document.createElement('article');
                inserted.innerHTML = `
                    <a href="/@candidate/post/identity-inserted"><time>new</time></a>
                    <a href="/@candidate">candidate</a>
                    <p>Synthetic fixture inserted row body.</p>`;
                node.before(inserted);
            }"""
        )
        middle_targets = await reader.scan_viewport(2)
        assert len(middle_targets) == 2
        middle_original = next(target for target in middle_targets if target.url == original_url)
        middle_diagnostic = next(
            row
            for row in reader.last_candidate_diagnostics
            if row.identity_fingerprint == middle_original.fingerprint
        )
        assert middle_diagnostic.observation_id == original_observation_id

        await page.locator("#row").evaluate(
            """node => {
                const replacement = node.cloneNode(true);
                replacement.querySelector("a[href*='/post/']").href =
                    '/@candidate/post/identity-new';
                replacement.querySelector('p').textContent =
                    'Synthetic fixture replacement row body.';
                node.replaceWith(replacement);
            }"""
        )
        identity_before_replacement = next(
            identity
            for identity in reader._last_viewport_candidate_identities
            if identity.fingerprint == middle_original.fingerprint
        )
        assert not await identity_before_replacement.dom_handle.evaluate(
            "node => node.isConnected"
        )

        replacement_targets = await reader.scan_viewport(2)
        replacement = next(target for target in replacement_targets if target.url == replacement_url)
        assert replacement.url == replacement_url
        assert replacement.body == replacement_body
        new_diagnostic = next(
            row
            for row in reader.last_candidate_diagnostics
            if row.identity_fingerprint == replacement.fingerprint
        )
        assert new_diagnostic.observation_id != original_observation_id
        assert original.url == original_url

    _run_offline_page(html, exercise)


def test_workflow_skips_detail_when_no_home_candidate_is_actionable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/not-actionable"
    body = "Hiring a frontend engineer in Vietnam"
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article>
<a href="/@candidate/post/not-actionable"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")
    policy = policy_fingerprint(canonical_terms(("hiring",)))
    store.save(Checkpoint(account.account_id, policy, completed_runs=4))
    detail_reads = 0
    detail_requests = 0

    class NonActionableReader(ThreadsHomeReader):
        async def actionable_now(self, target):
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")

        async def read_detail(self, target):
            nonlocal detail_reads
            detail_reads += 1
            raise AssertionError("Detail must not be entered")

    monkeypatch.setattr(workflow, "ThreadsHomeReader", NonActionableReader)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            await route.abort()

        await page.route(target_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=0,
            terms=("hiring",),
            detail=False,
        )
        assert result.status == "NO_ACTIONABLE_TARGET"
        assert result.stop_reason == "NO_ACTIONABLE_TARGET"
        assert result.detail_reads == 0
        assert result.checkpoint_runs == 4
        assert result.checkpoint_saved is False

    _run_offline_page(html, exercise)
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.completed_runs == 4
    assert detail_reads == detail_requests == 0


def test_checkpoint_save_failure_never_returns_a_saved_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    detail_url = "https://www.threads.com/@candidate/post/weather"
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article>
<a href="/@candidate/post/weather"><time>now</time></a>
<a href="/@candidate">candidate</a><p>A synthetic weather update</p>
</article></main></body></html>"""
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")

    def fail_save(_checkpoint) -> None:
        raise BrowserDataError("STATE_INVALID")

    monkeypatch.setattr(store, "save", fail_save)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(
                status=200,
                content_type="text/html",
                body="""<!doctype html><html><body><main>
<div data-pressable-container="true">
<header><a href="/@candidate">@candidate</a></header>
<div class="post-body"><p>A synthetic weather update</p></div>
</div></main></body></html>""",
            )

        await page.route(detail_url, serve_detail)
        with pytest.raises(UiReadError) as caught:
            await _run_page_workflow_for_test(
                page,
                account,
                store,
                max_posts=1,
                max_scrolls=0,
                terms=("hiring",),
            )
        assert caught.value.code == "INTERNAL_OPERATION_ERROR"

    _run_offline_page(html, exercise)
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 0


def test_unresolved_post_root_does_not_block_a_verified_selected_post(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    clean_body = "Hiring a frontend engineer in Vietnam"
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main>
<article id="unresolved-root">
  <a href="/@candidate/post/unresolved"><span>Permalink</span></a>
</article>
<article id="clean-root">
  <a href="/@candidate/post/clean-unresolved-peer"><time>now</time></a>
  <a href="/@candidate">candidate</a><p>{clean_body}</p>
</article>
</main></body></html>"""
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            post_path = urlsplit(route.request.url).path
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=(
                    "<!doctype html><html><body><main><article>"
                    f'<a href="{post_path}"><time>now</time></a>'
                    '<a href="/@candidate">candidate</a>'
                    f"<p>{clean_body}</p>"
                    "</article></main></body></html>"
                ),
            )

        await page.route("**/@candidate/post/clean-unresolved-peer", serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=0,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert not result.viewport_coverage_complete
        assert result.detail_reads == 1
        assert result.checkpoint_runs == 1

    _run_offline_page(html, exercise)
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 1
    output = capsys.readouterr().out
    assert "POST_ROOT_UNRESOLVED:1" in output


def test_ambiguous_pressable_row_recovers_only_its_timestamp_author_and_body() -> None:
    actor = "offline.operator"
    primary_body = "Hiring a backend engineer in Vietnam"
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article id="ambiguous-wrapper">
  <div id="primary-post">
    <a href="/@candidate/post/primary"><time>now</time></a>
    <a href="/@candidate">candidate</a>
    <p>{primary_body}</p>
    <a href="/@candidate/post/secondary?source=synthetic">secondary link</a>
  </div>
  <div id="related-post">
    <a href="/@candidate/post/related">related link</a>
    <p>Weather text from a related card</p>
  </div>
  <blockquote>
    <a href="/@candidate/post/quoted"><time>2h</time></a>
    <p>Quoted text from another post</p>
  </blockquote>
  <div data-pressable-container="true" id="media-card">
    <a href="/@candidate/post/media"><time>3h</time></a>
    <a href="/@candidate">candidate</a><p>Media card text</p>
  </div>
</article></main></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        targets = await reader.scan_viewport(2)

        assert len(targets) == 1
        assert targets[0].url.endswith("/post/primary")
        assert targets[0].author == "candidate"
        assert targets[0].body == primary_body
        assert targets[0].currently_visible
        assert reader.last_scan_counts.coverage_complete
        assert reader.last_scan_counts.wrapper_ambiguous == 0
        assert not reader.last_scan_counts.structural_reasons.get(
            "ROW_PROVENANCE_UNRESOLVED"
        )
        assert reader.last_candidate_diagnostics[0].reason == "PROVENANCE_VERIFIED"

    _run_offline_page(html, exercise)


def test_card_provenance_component_verifies_canonical_url_author_and_body() -> None:
    html = """<!doctype html><html><body><main><article>
<a id="permalink" href="/@candidate/post/card-proof"><time>now</time></a>
<a id="author" href="/@candidate">candidate</a>
<p>Hiring a backend engineer</p>
</article></main></body></html>"""

    async def exercise(page) -> None:
        body_text = BodyTextReader(page)
        card_reader = CardProvenanceReader(
            FeedRowScanner(page),
            body_text,
            LoadingObserver(page),
        )

        async def unused_visible_body_progress(*args, **kwargs):
            raise AssertionError("body progress is not used for an offscreen read")

        root = page.locator("article")
        target = await card_reader.read_card(
            root,
            read_visible_body_progress=unused_visible_body_progress,
        )
        assert target.url.endswith("/@candidate/post/card-proof")
        assert target.author == "candidate"
        assert target.body == "Hiring a backend engineer"

        await page.locator("#author").evaluate(
            "node => node.setAttribute('href', '/@different_author')"
        )
        with pytest.raises(UnverifiedFeedCandidate) as author_error:
            await card_reader.read_card(
                root,
                read_visible_body_progress=unused_visible_body_progress,
            )
        assert author_error.value.reason == "CANDIDATE_AUTHOR_CONFLICT"

        await page.locator("#author").evaluate(
            "node => node.setAttribute('href', '/@candidate')"
        )
        await page.locator("#permalink").evaluate(
            "node => node.setAttribute('href', '/@candidate/post/card-proof?source=bad')"
        )
        with pytest.raises(UnverifiedFeedCandidate) as url_error:
            await card_reader.read_card(
                root,
                read_visible_body_progress=unused_visible_body_progress,
            )
        assert url_error.value.reason == "CANDIDATE_NONCANONICAL"

    _run_offline_page(html, exercise)


def test_unresolved_ambiguous_row_does_not_block_a_verified_peer_checkpoint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main>
<article id="unresolved-wrapper">
  <a href="/@candidate/post/ambiguous"><time>now</time></a>
  <a href="/@candidate/post/related">related link</a>
  <a href="/@candidate">candidate</a>
  <p>Hiring body whose post boundary is not isolated</p>
</article>
<article id="clean-row">
  <a href="/@candidate/post/clean-row"><time>1h</time></a>
  <a href="/@candidate">candidate</a>
  <p>A synthetic weather update</p>
</article>
</main></body></html>"""
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=4,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.unresolved_gaps == 1
        assert result.home_sampled == 1
        assert result.detail_reads == 0
        assert result.checkpoint_runs == 1

    _run_offline_page(html, exercise)
    output = capsys.readouterr().out
    assert "ROW_PROVENANCE_UNRESOLVED:1" in output
    assert "POST_ROOT_UNRESOLVED:1" not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 1


def test_removed_ambiguous_row_gets_a_new_observation_identity() -> None:
    actor = "offline.operator"
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article id="row-to-remove">
  <a href="/@candidate/post/primary"><time>now</time></a>
  <a href="/@candidate/post/related">related link</a>
  <a href="/@candidate">candidate</a>
  <p>A synthetic row removed during recovery</p>
</article></main></body></html>"""

    class RemoveRowReader(ThreadsHomeReader):
        original_observation_id: str | None = None

        async def _recover_ambiguous_visible_card(self, root, root_handle, **kwargs):
            self.original_observation_id = self.last_candidate_diagnostics[0].observation_id
            await root.evaluate("node => node.remove()")
            raise reader_module._CardNotReady(
                root,
                reason_code="POST_ROOT_DOM_IDENTITY_CHANGED",
            )

    async def exercise(page) -> None:
        reader = RemoveRowReader(page, actor)
        await reader.open_home()
        targets = await reader.scan_viewport(1)

        assert targets == ()
        assert reader.original_observation_id is not None
        assert len(reader.last_candidate_diagnostics) == 1
        diagnostic = reader.last_candidate_diagnostics[0]
        assert diagnostic.reason == "POST_ROOT_IDENTITY_CHANGED"
        assert diagnostic.observation_id != reader.original_observation_id
        assert not reader.last_viewport_can_wheel

    _run_offline_page(html, exercise)


@pytest.mark.parametrize("close_context", [False, True], ids=["page", "context"])
def test_closed_session_during_scan_is_classified_without_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    close_context: bool,
) -> None:
    actor = "offline.operator"
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><a href="/@candidate/post/closed-scan"><time>now</time></a>
<a href="/@candidate">candidate</a><p>A synthetic weather update</p></article></main>
</body></html>"""
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")

    class ClosedDuringScanReader(ThreadsHomeReader):
        async def scan_viewport(self, *args, **kwargs):
            if close_context:
                await self._page.context.close()
            else:
                await self._page.close()
            await self._page.title()

    monkeypatch.setattr(workflow, "ThreadsHomeReader", ClosedDuringScanReader)

    async def exercise(page) -> None:
        with pytest.raises(UiReadError) as caught:
            await _run_page_workflow_for_test(
                page,
                account,
                store,
                max_posts=1,
                max_scrolls=0,
                terms=("hiring",),
            )
        assert caught.value.code == "BROWSER_SESSION_UNAVAILABLE"

    _run_offline_page(html, exercise)
    output = capsys.readouterr().out
    assert (
        "[FAILURE_DIAGNOSTIC] phase=VERIFYING operation=SCAN_ROW error_class=TARGET_CLOSED "
        "error_kind=OTHER page_closed=true context_closed=unknown "
        "row_identity_reused=unknown row_identity_changed=unknown"
    ) in output
    assert output.count("[FAILURE_DIAGNOSTIC]") == 1
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 0


def test_transient_execution_context_scan_error_is_retried_after_reverification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article id="candidate-row"><a href="/@candidate/post/detached-row"><time>now</time></a>
<a href="/@candidate">candidate</a><p>A synthetic weather update</p></article></main>
</body></html>"""
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")

    class DetachOnceReader(ThreadsHomeReader):
        detached = False

        async def _read_visible_body_progress(self, root, *args, **kwargs):
            if not self.detached and await root.get_attribute("id") == "candidate-row":
                self.detached = True
                raise PlaywrightError("Execution context was destroyed PRIVATE_MARKER")
            return await super()._read_visible_body_progress(root, *args, **kwargs)

    monkeypatch.setattr(workflow, "ThreadsHomeReader", DetachOnceReader)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(
                status=200,
                content_type="text/html",
                body="""<!doctype html><html><body><main><article>
<a href="/@candidate/post/detached-row"><time>now</time></a>
<a href="/@candidate">candidate</a><p>A synthetic weather update</p>
</article></main></body></html>""",
            )

        await page.route("**/post/detached-row", serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=0,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE"
        assert result.home_sampled == 1
        assert result.detail_reads == 1

    _run_offline_page(html, exercise)
    output = capsys.readouterr().out
    assert (
        "[FAILURE_DIAGNOSTIC] phase=VERIFYING stage=FEED_INITIAL viewport=1 candidate=1 "
        "operation=READ_BODY error_class=DOM_DETACHED error_kind=EXECUTION_CONTEXT "
        "page_closed=false context_closed=unknown row_identity_reused=true "
        "row_identity_changed=false"
    ) in output
    assert "RECOVERED stage=FEED_INITIAL retry=1" in output
    assert "PRIVATE_MARKER" not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1


def _synthetic_three_card_feed(actor: str) -> str:
    cards = "\n".join(
        f"""<article id="scan-row-{index}">
<a href="/@candidate/post/scan-row-{index}"><time>now</time></a>
<a href="/@candidate">candidate</a>
<p>SYNTHETIC_PRIVATE_ROW_{index} weather update</p></article>"""
        for index in range(1, 4)
    )
    return f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main>{cards}</main></body></html>"""


@pytest.mark.parametrize(
    ("error_message", "expected_class", "expected_kind"),
    [
        ("synthetic protocol failure PRIVATE_MARKER", "OTHER", "OTHER"),
        (
            "Protocol error: evaluation failed PRIVATE_MARKER",
            "OTHER",
            "EVALUATION_PROTOCOL",
        ),
        (
            "RangeError: Maximum call stack size exceeded PRIVATE_MARKER",
            "OTHER",
            "JS_STACK_OVERFLOW",
        ),
        ("RangeError: invalid range PRIVATE_MARKER", "OTHER", "JS_RANGE_ERROR"),
        ("Evaluation failed: PRIVATE_MARKER", "OTHER", "EVALUATION"),
        ("strict mode violation: PRIVATE_MARKER", "OTHER", "STRICT_MODE"),
        (
            "Execution context was destroyed PRIVATE_MARKER",
            "DOM_DETACHED",
            "EXECUTION_CONTEXT",
        ),
    ],
    ids=(
        "other",
        "evaluation-protocol",
        "stack-overflow",
        "range-error",
        "evaluation",
        "strict-mode",
        "execution-context",
    ),
)
def test_transient_scan_row_failure_retries_without_replaying_verified_posts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_message: str,
    expected_class: str,
    expected_kind: str,
) -> None:
    actor = "offline.operator"
    html = _synthetic_three_card_feed(actor)
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")
    detail_requests = 0

    class FailThirdBodyOnceReader(ThreadsHomeReader):
        failures = 0

        async def _read_visible_body_progress(self, root, *args, **kwargs):
            if await root.get_attribute("id") == "scan-row-3" and not self.failures:
                self.failures += 1
                self._body_text.last_failure_substep = "BODY_RANGE_EVALUATE"
                raise PlaywrightError(error_message)
            return await super()._read_visible_body_progress(root, *args, **kwargs)

    monkeypatch.setattr(workflow, "ThreadsHomeReader", FailThirdBodyOnceReader)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            post_id = route.request.url.rsplit("/", maxsplit=1)[-1]
            row_number = post_id.rsplit("-", maxsplit=1)[-1]
            body = f"SYNTHETIC_PRIVATE_ROW_{row_number} weather update"
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=(
                    "<!doctype html><html><body><main><article>"
                    f'<a href="/@candidate/post/{post_id}"><time>now</time></a>'
                    f'<a href="/@candidate">candidate</a><p>{body}</p>'
                    "</article></main></body></html>"
                ),
            )

        await page.route("**/post/scan-row-*", serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=3,
            max_scrolls=0,
            terms=("weather",),
        )
        assert result.status == "COMPLETE"
        assert result.home_sampled == 3
        assert result.home_new == 3
        assert result.classified == 3
        assert result.observed_rows == 3
        assert result.provenance_verified == 3
        assert result.unique_verified_posts == 3
        assert result.visible_text_segments == 3
        assert result.duplicates == 0
        assert result.detail_reads == 1
        assert detail_requests == 1

    _run_offline_page(html, exercise)
    output = capsys.readouterr().out
    assert (
        "[FAILURE_DIAGNOSTIC] phase=VERIFYING stage=FEED_INITIAL viewport=1 candidate=3 "
        f"operation=READ_BODY error_class={expected_class} error_kind={expected_kind} "
        "page_closed=false context_closed=unknown row_identity_reused=true "
        "row_identity_changed=false"
    ) in output
    assert "RECOVERED stage=FEED_INITIAL retry=1" in output
    assert "body_substep=BODY_RANGE_EVALUATE" in output
    assert "identity_source=CURRENT_CANONICAL retry_eligibility=REUSED" in output
    assert "PRIVATE_MARKER" not in output
    assert all(f"SYNTHETIC_PRIVATE_ROW_{index}" not in output for index in range(1, 4))
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("weather",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 3
    assert len(checkpoint.seen_fingerprints) == 3
    assert checkpoint.detail_reads == 1


def test_read_body_retry_uses_carried_verified_row_after_native_scroll(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    detail_body = "Hiring a backend engineer in Vietnam"
    feed_html = f"""<!doctype html><html><head><style>
html, body {{ margin: 0; }}
nav {{ height: 36px; }}
article {{ position: relative; box-sizing: border-box; height: 420px; margin: 0; }}
.identity {{ position: absolute; top: 6px; left: 12px; }}
.body {{ position: absolute; top: 320px; left: 12px; right: 12px; }}
.secondary {{ position: absolute; top: 360px; left: 12px; }}
article:nth-of-type(3) .body {{ top: 70px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main>
<article id="scroll-row-1">
<div class="identity"><a href="/@candidate/post/scroll-row-1"><time>3h</time></a>
<a href="/@candidate">candidate</a></div>
<div class="body"><p>A synthetic weather update</p></div>
<a class="secondary" href="/@candidate/post/related?source=synthetic">related</a>
</article>
<article id="scroll-row-2">
<div class="identity"><a href="/@candidate/post/scroll-row-2"><time>2h</time></a>
<a href="/@candidate">candidate</a></div>
<div class="body"><p>A synthetic local event</p></div>
</article>
<article id="scroll-row-3">
<div class="identity"><a href="/@candidate/post/scroll-row-3"><time>1h</time></a>
<a href="/@candidate">candidate</a></div>
<div class="body"><p>{detail_body}</p></div>
</article>
</main></body></html>"""
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")

    class FailCarriedBodyOnceReader(ThreadsHomeReader):
        last_instance = None
        active_reader = None

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.failed = False
            self.inject_body_failure = False
            type(self).last_instance = self

        async def _read_visible_body_progress(self, root, *args, **kwargs):
            if (
                self._scan_stage == "FEED_SCROLL"
                and await root.get_attribute("id") == "scroll-row-1"
                and not self.failed
            ):
                self.failed = True
                self.inject_body_failure = True
                type(self).active_reader = self
                try:
                    return await super()._read_visible_body_progress(root, *args, **kwargs)
                finally:
                    self.inject_body_failure = False
            return await super()._read_visible_body_progress(root, *args, **kwargs)

    original_evaluate = Locator.evaluate

    async def fail_body_range_evaluate(locator, expression, arg=None):
        active_reader = FailCarriedBodyOnceReader.active_reader
        if (
            active_reader is not None
            and active_reader.inject_body_failure
            and "bodyTextNodesPresent" in expression
            and await locator.get_attribute("id") == "scroll-row-1"
        ):
            raise PlaywrightError("synthetic body range failure PRIVATE_MARKER")
        return await original_evaluate(locator, expression, arg)

    monkeypatch.setattr(Locator, "evaluate", fail_body_range_evaluate)
    monkeypatch.setattr(workflow, "ThreadsHomeReader", FailCarriedBodyOnceReader)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=(
                    "<!doctype html><html><body><main><article>"
                    '<a href="/@candidate/post/scroll-row-3"><time>1h</time></a>'
                    '<a href="/@candidate">candidate</a>'
                    f"<p>{detail_body}</p></article></main></body></html>"
                ),
            )

        await page.route("**/post/scroll-row-3", serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=3,
            max_scrolls=1,
            terms=("hiring", "engineer"),
        )
        assert result.status == "COMPLETE"
        assert result.home_sampled == 3
        assert result.classified == 3
        assert result.provenance_verified == 3
        assert result.unique_verified_posts == 3
        assert result.detail_reads == 1
        assert result.scroll_attempts == result.scrolls == 1

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "operation=READ_BODY" in output
    assert "body_substep=BODY_RANGE_EVALUATE" in output
    assert "identity_source=CARRIED_VERIFIED retry_eligibility=REUSED" in output
    assert "PRIVATE_MARKER" not in output
    assert "POST_ROOT_UNRESOLVED:1" not in output
    reader = FailCarriedBodyOnceReader.last_instance
    assert reader is not None
    assert reader.last_scan_counts.coverage_complete
    assert reader.last_scan_counts.unrepresented_root_count == 0
    assert reader.last_scan_counts.unresolved_visited_rows == 0
    assert reader.last_scan_counts.structural_reasons.get("POST_ROOT_UNRESOLVED", 0) == 0
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 3
    assert checkpoint.detail_reads == 1


@pytest.mark.parametrize(
    "failure_mode",
    ["persistent", "canonical-change", "canonical-author-change", "root-replaced"],
)
def test_unrevalidated_scan_row_failure_stops_without_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_mode: str,
) -> None:
    actor = "offline.operator"
    html = _synthetic_three_card_feed(actor)
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")
    detail_route_requests = 0

    class FailThirdBodyReader(ThreadsHomeReader):
        last_instance = None

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.failures = 0
            self.disposed_handle_batches = []
            type(self).last_instance = self

        async def _read_visible_body_progress(self, root, *args, **kwargs):
            if await root.get_attribute("id") != "scan-row-3":
                return await super()._read_visible_body_progress(root, *args, **kwargs)
            self.failures += 1
            if failure_mode == "canonical-change" and self.failures == 1:
                await root.locator('a[href*="/post/"]').first.evaluate(
                    "node => node.setAttribute('href', '/@candidate/post/rebound-row')"
                )
            elif failure_mode == "canonical-author-change" and self.failures == 1:
                await root.locator('a[href="/@candidate"]').evaluate(
                    "node => node.setAttribute('href', '/@other-candidate')"
                )
            elif failure_mode == "root-replaced" and self.failures == 1:
                await root.evaluate("node => node.replaceWith(node.cloneNode(true))")
            self._body_text.last_failure_substep = "BODY_RANGE_EVALUATE"
            raise PlaywrightError("synthetic protocol failure PRIVATE_MARKER")

        async def _dispose_active_scan_candidate_handles(self):
            handles = self._active_scan_candidate_handles
            if handles is not None:
                self.disposed_handle_batches.append(len(handles))
            await super()._dispose_active_scan_candidate_handles()

    monkeypatch.setattr(workflow, "ThreadsHomeReader", FailThirdBodyReader)

    async def exercise(page) -> None:
        async def track_detail(route: Route) -> None:
            nonlocal detail_route_requests
            detail_route_requests += 1
            await route.fulfill(status=500, content_type="text/plain", body="unavailable")

        await page.route("**/post/**", track_detail)
        with pytest.raises(UiReadError) as caught:
            await _run_page_workflow_for_test(
                page,
                account,
                store,
                max_posts=3,
                max_scrolls=0,
                terms=("hiring",),
            )
        assert caught.value.code == "SCAN_ROW_UNVERIFIED"
        assert detail_route_requests == 0

    _run_offline_page(html, exercise)
    output = capsys.readouterr().out
    assert "PRIVATE_MARKER" not in output
    assert "body_substep=BODY_RANGE_EVALUATE" in output
    assert (
        "retry_eligibility=REUSED" in output
        if failure_mode == "persistent"
        else "retry_eligibility=CHANGED" in output
    ), output
    assert all(f"SYNTHETIC_PRIVATE_ROW_{index}" not in output for index in range(1, 4))
    assert "offline.operator" not in output
    assert "scan-row-3" not in output
    assert "/@candidate/post/" not in output
    assert str(profile) not in output
    if failure_mode == "persistent":
        assert output.count("error_kind=OTHER") == 2
        assert output.count("row_identity_reused=true row_identity_changed=false") == 2
    else:
        assert output.count("error_kind=OTHER") == 1
        assert "row_identity_reused=false row_identity_changed=true" in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 0
    assert checkpoint.home_sampled == 0
    assert checkpoint.detail_reads == 0
    assert checkpoint.seen_fingerprints == ()
    assert FailThirdBodyReader.last_instance is not None
    assert FailThirdBodyReader.last_instance._active_scan_candidate_handles is None
    assert FailThirdBodyReader.last_instance.disposed_handle_batches
    assert FailThirdBodyReader.last_instance.disposed_handle_batches[0] == 3


def test_invalid_workflow_transition_is_not_mapped_to_browser_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
</body></html>"""
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")
    monkeypatch.setitem(
        nurture_state._ALLOWED_TRANSITIONS,
        WorkflowPhase.INITIALIZING,
        frozenset(),
    )

    async def exercise(page) -> None:
        with pytest.raises(UiReadError) as caught:
            await _run_page_workflow_for_test(
                page,
                account,
                store,
                max_posts=1,
                max_scrolls=0,
                terms=("hiring",),
            )
        assert caught.value.code == "INTERNAL_STATE_ERROR"

    _run_offline_page(html, exercise)
    output = capsys.readouterr().out
    assert (
        "[FAILURE_DIAGNOSTIC] phase=INITIALIZING operation=STATE_TRANSITION "
        "error_class=STATE_INVALID error_kind=OTHER page_closed=false "
        "context_closed=unknown row_identity_reused=unknown row_identity_changed=unknown"
    ) in output
    assert "BROWSER_SESSION_UNAVAILABLE" not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 0


def test_other_playwright_scan_error_is_not_mapped_to_browser_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
</body></html>"""
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")

    class OtherErrorReader(ThreadsHomeReader):
        async def scan_viewport(self, *args, **kwargs):
            raise PlaywrightError("synthetic protocol failure PRIVATE_MARKER")

    monkeypatch.setattr(workflow, "ThreadsHomeReader", OtherErrorReader)

    async def exercise(page) -> None:
        with pytest.raises(UiReadError) as caught:
            await _run_page_workflow_for_test(
                page,
                account,
                store,
                max_posts=1,
                max_scrolls=0,
                terms=("hiring",),
            )
        assert caught.value.code == "UI_READ_UNVERIFIED"

    _run_offline_page(html, exercise)
    output = capsys.readouterr().out
    assert (
        "[FAILURE_DIAGNOSTIC] phase=VERIFYING operation=SCAN_ROW error_class=OTHER "
        "error_kind=OTHER page_closed=false context_closed=unknown "
        "row_identity_reused=unknown row_identity_changed=unknown"
    ) in output
    assert "PRIVATE_MARKER" not in output
    assert output.count("[FAILURE_DIAGNOSTIC]") == 1
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 0


def test_detail_does_not_auto_scroll_an_offscreen_target() -> None:
    actor = "offline.operator"
    body = "Synthetic fixture post moves outside the viewport."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article id="offscreen-detail">
<a href="/@candidate/post/offscreen-detail"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        target = (await reader.scan_viewport(1))[0]
        original_scroll_y = await page.evaluate("() => window.scrollY")
        await page.locator("#offscreen-detail").evaluate(
            "node => node.style.transform = 'translateY(1200px)'"
        )

        with pytest.raises(UiReadError) as caught:
            await reader.read_detail(target)

        assert caught.value.code == "DETAIL_NAVIGATION_UNVERIFIED"
        assert page.url == HOME_URL
        assert await page.evaluate("() => window.scrollY") == original_scroll_y

    _run_offline_page(feed_html, exercise)


def test_detail_body_mismatch_returns_home_without_claiming_read(tmp_path: Path) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/body-mismatch"
    home_body = "Hiring a frontend engineer"
    wrong_detail_body = "A different post body"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
<a href="/@candidate/post/body-mismatch"><time>now</time></a>
<a href="/@candidate">@candidate</a><p>{home_body}</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        async def serve_wrong_detail(route: Route) -> None:
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=f"""<!doctype html><html><body><main><article>
<a href="/@candidate/post/body-mismatch"><time>now</time></a>
<a href="/@candidate">@candidate</a><p>{wrong_detail_body}</p>
 </article></main></body></html>""",
            )

        await page.route(target_url, serve_wrong_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=0,
            terms=("hiring",),
        )
        assert result.status == "DETAIL_RETRY_PENDING"
        assert result.detail_reads == 0
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 0
    assert len(checkpoint.pending_read_fingerprints) == 1


def test_overlapping_scan_completes_coverage_before_detail_and_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    pending_url = "https://www.threads.com/@candidate/post/pending"
    first_url = "https://www.threads.com/@candidate/post/first"
    relevant_body = "Hiring a frontend engineer in Vietnam"
    irrelevant_body = "A weather update"
    first_feed = (
        ("pending", relevant_body),
        ("old", irrelevant_body),
        ("middle", irrelevant_body),
        ("first", relevant_body),
    )
    resumed_feed = (
        ("old", irrelevant_body),
        ("middle", irrelevant_body),
        ("pending", relevant_body),
        ("first", relevant_body),
    )

    def render_feed(
        posts: tuple[tuple[str, str], ...],
        *,
        detail: bool = False,
    ) -> str:
        cards = "".join(
            f'''<article><a href="/@candidate/post/{slug}"><time>1h</time></a>
<a href="/@candidate">@candidate</a><p>{body}</p></article>'''
            for slug, body in posts
        )
        content = f"<main>{cards}</main>" if detail else cards
        return f"""<!doctype html><html><head>
<style>article {{ height: 360px; }}</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav>{content}</body></html>"""

    detail_pages = {
        pending_url: render_feed((("pending", relevant_body),), detail=True),
        first_url: render_feed((("first", relevant_body),), detail=True),
    }
    account_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(account_root).add("offline", actor, profile)
    store = CheckpointStore(account_root)
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(account_root))
    requests: list[str] = []
    home_requests = 0

    async def respond(route: Route) -> None:
        nonlocal home_requests
        url = route.request.url
        requests.append(url)
        if url == HOME_URL:
            home_requests += 1
            posts = first_feed if home_requests == 1 else resumed_feed
            html = render_feed(posts)
        elif url.rstrip("/") in detail_pages:
            html = detail_pages[url.rstrip("/")]
        elif _is_offline_profile_url(url):
            html = "<!doctype html><html><body><h1>Local profile</h1></body></html>"
        else:
            await route.abort()
            return
        await route.fulfill(status=200, content_type="text/html", body=html)

    monkeypatch.setattr(
        workflow,
        "async_playwright",
        lambda: _offline_playwright_manager(respond),
    )
    command = [
        "nurture",
        "run",
        "offline",
        "--headed",
        "--max-posts",
        "4",
        "--max-scrolls",
        "12",
        "--term",
        "hiring",
        "--term",
        "engineer",
    ]

    first_code = main(command)
    first = capsys.readouterr()
    policy = policy_fingerprint(canonical_terms(("hiring", "engineer")))
    checkpoint = store.load(account.account_id, policy)
    assert first_code == 0, first.out + first.err
    assert "NURTURE status=COMPLETE source=HOME_FOR_YOU" in first.out
    assert "home_sampled=3 home_new=3" in first.out
    assert "home_relevant=1 home_irrelevant=2" in first.out
    assert "detail_reads=1" in first.out
    assert "viewport_coverage_complete=true" in first.out
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 3
    assert checkpoint.pending_read_fingerprints == ()
    assert len(checkpoint.seen_fingerprints) == 3
    assert requests.count(pending_url) + requests.count(first_url) == 1


def test_for_you_click_must_leave_an_active_marker_or_route() -> None:
    html = """<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@offline.operator">Profile</a>
</nav>
<article><p>Offline feed</p></article>
</body></html>"""

    async def exercise(page) -> None:
        with pytest.raises(UiReadError) as caught:
            await ThreadsHomeReader(page, "offline.operator").open_home()
        assert caught.value.code == "FOR_YOU_ACTIVATION_NOT_READY"

    _run_offline_page(html, exercise)


def test_actor_verification_scopes_sidebar_profile_text_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for_you = '<a aria-current="page" aria-label="For you" href="/">For you</a>'
    outside_navigation = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">{for_you}</nav>
<main><div>Profile</div></main>
</body></html>"""

    async def profile_text_outside_sidebar_stops(page) -> None:
        with pytest.raises(UiReadError) as caught:
            await ThreadsHomeReader(page, "offline.operator").open_home()
        assert caught.value.code == "PROFILE_CONTROL_NOT_FOUND"
        assert page.url == HOME_URL

    _run_offline_page(outside_navigation, profile_text_outside_sidebar_stops)

    no_navigation = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">{for_you}
  <div><span>Profile</span></div>
</nav></body></html>"""

    async def profile_click_without_navigation_stops(page) -> None:
        with pytest.raises(UiReadError) as caught:
            await ThreadsHomeReader(page, "offline.operator").open_home()
        assert caught.value.code == "PROFILE_CLICK_NO_NAVIGATION"

    with monkeypatch.context() as short_timeout:
        short_timeout.setattr(reader_module, "_READINESS_TIMEOUT_MS", 1_500)
        _run_offline_page(no_navigation, profile_click_without_navigation_stops)

    sidebar_text = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">{for_you}
  <div onclick="window.location.href='/@offline.operator'"><span>Profile</span></div>
</nav></body></html>"""

    async def sidebar_text_opens_and_verifies_profile(page) -> None:
        await ThreadsHomeReader(page, "offline.operator").open_home()
        assert page.url == HOME_URL

    _run_offline_page(sidebar_text, sidebar_text_opens_and_verifies_profile)

    unsupported_route = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">{for_you}
  <div onclick="history.pushState({{}}, '', '/settings')"><span>Profile</span></div>
</nav></body></html>"""

    async def unsupported_profile_route_stops(page) -> None:
        with pytest.raises(UiReadError) as caught:
            await ThreadsHomeReader(page, "offline.operator").open_home()
        assert caught.value.code == "PROFILE_ROUTE_UNVERIFIED"
        assert page.url.endswith("/settings")

    _run_offline_page(unsupported_route, unsupported_profile_route_stops)

    ambiguous = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">{for_you}
  <div><span>Profile</span></div><div><span>Profile</span></div>
</nav>
</body></html>"""

    async def ambiguous_sidebar_text_stops(page) -> None:
        with pytest.raises(UiReadError) as caught:
            await ThreadsHomeReader(page, "offline.operator").open_home()
        assert caught.value.code == "PROFILE_CONTROL_AMBIGUOUS"
        assert page.url == HOME_URL

    _run_offline_page(ambiguous, ambiguous_sidebar_text_stops)

    mismatched = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">{for_you}
  <div onclick="window.location.href='/@other.operator'"><span>Profile</span></div>
</nav>
</body></html>"""

    async def mismatched_profile_route(page) -> None:
        with pytest.raises(UiReadError) as caught:
            await ThreadsHomeReader(page, "offline.operator").open_home()
        assert caught.value.code == "ACTOR_MISMATCH"
        assert page.url == "https://www.threads.com/@other.operator"

    _run_offline_page(mismatched, mismatched_profile_route)


def test_scroll_attempt_is_not_counted_without_observed_feed_movement() -> None:
    html = """<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@offline.operator">Profile</a>
</nav>
<article>
  <a href="/@candidate/post/one"><time>1h</time></a>
  <a href="/@candidate">@candidate</a>
  <p>Offline feed</p>
</article>
</body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, "offline.operator")
        await reader.open_home()
        with pytest.raises(UiReadError) as caught:
            await reader.sample_and_scroll(1)
        assert caught.value.code == "HOME_SCROLL_SURFACE_UNVERIFIED"
        assert reader.scroll_attempts == 0
        assert reader.scrolls == 0

    _run_offline_page(html, exercise)


def test_default_desktop_profile_is_rejected_before_browser_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import sys

    if os.name == "nt":
        local = tmp_path / "LocalAppData"
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        profile = local / "Google" / "Chrome" / "User Data" / "Profile 1"
    elif sys.platform == "darwin":
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        profile = (
            tmp_path
            / "Library"
            / "Application Support"
            / "Google"
            / "Chrome"
            / "Profile 1"
        )
    else:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        profile = tmp_path / ".config" / "google-chrome" / "Profile 1"
    profile.mkdir(parents=True)
    with pytest.raises(BrowserDataError) as registration_error:
        AccountStore(tmp_path / "data").add("offline", "actor", profile)
    assert registration_error.value.code == "PROFILE_UNAVAILABLE"

    account = BrowserAccount(uuid4(), "offline", "actor", profile.resolve())

    def browser_must_not_start():
        pytest.fail("Playwright started for a default desktop profile")

    monkeypatch.setattr(workflow, "async_playwright", browser_must_not_start)
    with pytest.raises(BrowserDataError) as launch_error:
        asyncio.run(
            workflow.run_browser_nurture(
                account,
                CheckpointStore(tmp_path / "run-data"),
                max_posts=1,
                terms=("hiring",),
            )
        )
    assert launch_error.value.code == "PROFILE_UNAVAILABLE"


def test_direct_browser_workflow_rejects_busy_profile_and_releases_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "dedicated-profile"
    profile.mkdir()
    account = BrowserAccount(uuid4(), "offline", "offline.operator", profile)
    store = CheckpointStore(tmp_path / "data")

    def browser_must_not_start():
        pytest.fail("busy profile must be rejected before Playwright starts")

    monkeypatch.setattr(workflow, "async_playwright", browser_must_not_start)
    with store.profile_lock(account), pytest.raises(BrowserDataError) as busy:
        asyncio.run(
            workflow.run_browser_nurture(
                account, store, max_posts=1, terms=("hiring",)
            )
        )
    assert busy.value.code == "ACCOUNT_BUSY"

    def fail_browser_start():
        raise OSError("synthetic browser startup failure")

    monkeypatch.setattr(workflow, "async_playwright", fail_browser_start)
    with pytest.raises(UiReadError) as failed:
        asyncio.run(
            workflow.run_browser_nurture(
                account, store, max_posts=1, terms=("hiring",)
            )
        )
    assert failed.value.code == "INTERNAL_OPERATION_ERROR"
    with store.profile_lock(account):
        pass


def test_only_managed_browser_workflow_is_a_public_profile_entry_point() -> None:
    assert "run_browser_nurture" in workflow.__all__
    assert hasattr(workflow, "run_browser_nurture")
    assert "run_page_workflow" not in workflow.__all__
    assert not hasattr(workflow, "run_page_workflow")


def test_concurrent_browser_workflows_reject_same_profile_before_checkpoint_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "dedicated-profile"
    profile.mkdir()
    account = BrowserAccount(uuid4(), "offline", "offline.operator", profile)
    store = CheckpointStore(tmp_path / "data")
    policy = policy_fingerprint(("hiring",))
    initial = Checkpoint(account.account_id, policy, completed_runs=7)
    store.save(initial)
    browser = _mock_persistent_browser(monkeypatch)
    engine_entered = asyncio.Event()
    allow_engine_to_finish = asyncio.Event()
    checkpoint_access: list[str] = []
    original_load = store.load

    async def read_then_wait(_page, _account, checkpoint_store, **_kwargs):
        probe = checkpoint_store.profile_lock(account)
        try:
            probe.acquire()
        except BrowserDataError as error:
            assert error.code == "ACCOUNT_BUSY"
        else:
            probe.release()
            pytest.fail("managed page cycle ran without the profile lock")
        checkpoint_access.append("load")
        checkpoint = checkpoint_store.load(account.account_id, policy)
        engine_entered.set()
        await allow_engine_to_finish.wait()
        checkpoint_access.append("save")
        checkpoint_store.save(replace(checkpoint, completed_runs=8))
        return SimpleNamespace(status="COMPLETE")

    monkeypatch.setattr(workflow, "_execute_page_workflow", read_then_wait)

    async def exercise() -> None:
        first = asyncio.create_task(
            workflow.run_browser_nurture(
                account, store, max_posts=1, terms=("hiring",)
            )
        )
        try:
            await asyncio.wait_for(engine_entered.wait(), timeout=2)
            with pytest.raises(BrowserDataError) as busy:
                await workflow.run_browser_nurture(
                    account, store, max_posts=1, terms=("hiring",)
                )
            assert busy.value.code == "ACCOUNT_BUSY"
            assert browser["launches"] == 1
            assert checkpoint_access == ["load"]
            assert original_load(account.account_id, policy) == initial
        finally:
            allow_engine_to_finish.set()
        await first

    asyncio.run(exercise())

    assert checkpoint_access == ["load", "save"]
    assert original_load(account.account_id, policy).completed_runs == 8
    with store.profile_lock(account):
        pass


def test_browser_workflows_for_different_profiles_can_run_concurrently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "data"
    accounts = tuple(
        BrowserAccount(
            uuid4(),
            f"offline-{index}",
            "offline.operator",
            tmp_path / f"profile-{index}",
        )
        for index in (1, 2)
    )
    for account in accounts:
        account.profile_directory.mkdir()
    store = CheckpointStore(root)
    browser = _mock_persistent_browser(monkeypatch)
    entered_profiles: list[Path] = []
    both_entered = asyncio.Event()
    allow_completion = asyncio.Event()

    async def wait_for_peer(_page, account, _store, **_kwargs):
        entered_profiles.append(account.profile_directory)
        if len(entered_profiles) == 2:
            both_entered.set()
        await allow_completion.wait()
        return SimpleNamespace(status="COMPLETE")

    monkeypatch.setattr(workflow, "_execute_page_workflow", wait_for_peer)

    async def exercise() -> None:
        attempts = [
            asyncio.create_task(
                workflow.run_browser_nurture(
                    account, store, max_posts=1, terms=("hiring",)
                )
            )
            for account in accounts
        ]
        try:
            await asyncio.wait_for(both_entered.wait(), timeout=2)
            assert browser["launches"] == 2
        finally:
            allow_completion.set()
        await asyncio.gather(*attempts)

    asyncio.run(exercise())

    assert set(entered_profiles) == {
        account.profile_directory for account in accounts
    }
    for account in accounts:
        with store.profile_lock(account):
            pass


def test_browser_runtime_timeout_during_launch_releases_profile_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "dedicated-profile"
    profile.mkdir()
    account = BrowserAccount(uuid4(), "offline", "offline.operator", profile)
    store = CheckpointStore(tmp_path / "data")
    manager_state = {"entered": False, "exited": False}

    class Chromium:
        async def launch_persistent_context(self, **_kwargs):
            await asyncio.Event().wait()

    class Manager:
        async def __aenter__(self):
            manager_state["entered"] = True
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *_args):
            manager_state["exited"] = True

    monkeypatch.setattr(workflow, "async_playwright", Manager)

    with pytest.raises(UiReadError) as caught:
        asyncio.run(
            workflow.run_browser_nurture(
                account,
                store,
                max_posts=1,
                terms=("hiring",),
                max_runtime_seconds=0.01,
            )
        )

    assert caught.value.code == "CYCLE_TIMEOUT"
    assert manager_state == {"entered": True, "exited": True}
    checkpoint = store.load(account.account_id, policy_fingerprint(("hiring",)))
    assert checkpoint.seen_fingerprints == ()
    assert checkpoint.pending_read_fingerprints == ()
    with store.profile_lock(account):
        pass


def test_browser_runtime_timeout_during_playwright_startup_attempts_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "dedicated-profile"
    profile.mkdir()
    account = BrowserAccount(uuid4(), "offline", "offline.operator", profile)
    store = CheckpointStore(tmp_path / "data")
    manager_state = {"startup_started": False, "shutdown_attempted": False}

    class Manager:
        async def __aenter__(self):
            manager_state["startup_started"] = True
            await asyncio.Event().wait()

        async def __aexit__(self, *_args):
            manager_state["shutdown_attempted"] = True

    monkeypatch.setattr(workflow, "async_playwright", Manager)

    with pytest.raises(UiReadError) as caught:
        asyncio.run(
            workflow.run_browser_nurture(
                account,
                store,
                max_posts=1,
                terms=("hiring",),
                max_runtime_seconds=0.01,
            )
        )

    assert caught.value.code == "CYCLE_TIMEOUT"
    assert manager_state == {"startup_started": True, "shutdown_attempted": True}
    checkpoint = store.load(account.account_id, policy_fingerprint(("hiring",)))
    assert checkpoint.seen_fingerprints == ()
    assert checkpoint.pending_read_fingerprints == ()
    with store.profile_lock(account):
        pass


def test_browser_runtime_timeout_during_active_detail_closes_context_and_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "dedicated-profile"
    profile.mkdir()
    account = BrowserAccount(uuid4(), "offline", "offline.operator", profile)
    store = CheckpointStore(tmp_path / "data")
    lifecycle = {"closed": False, "manager_exited": False, "detail": False}

    class Context:
        def __init__(self) -> None:
            self.pages = [object()]

        async def close(self):
            lifecycle["closed"] = True

    context = Context()

    class Chromium:
        async def launch_persistent_context(self, **_kwargs):
            return context

    class Manager:
        async def __aenter__(self):
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *_args):
            lifecycle["manager_exited"] = True

    async def wait_in_detail(*_args, **kwargs):
        lifecycle["detail"] = kwargs["detail"]
        await asyncio.Event().wait()

    monkeypatch.setattr(workflow, "async_playwright", Manager)
    monkeypatch.setattr(workflow, "_detail_terminal_ready", lambda: True)
    monkeypatch.setattr(workflow, "_run_page_workflow_locked", wait_in_detail)

    with pytest.raises(UiReadError) as caught:
        asyncio.run(
            workflow.run_browser_nurture(
                account,
                store,
                max_posts=1,
                terms=("hiring",),
                max_runtime_seconds=0.01,
                detail=True,
            )
        )

    assert caught.value.code == "CYCLE_TIMEOUT"
    assert lifecycle == {"closed": True, "manager_exited": True, "detail": True}
    checkpoint = store.load(account.account_id, policy_fingerprint(("hiring",)))
    assert checkpoint.seen_fingerprints == ()
    assert checkpoint.pending_read_fingerprints == ()
    with store.profile_lock(account):
        pass


def test_external_browser_cancellation_propagates_after_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "dedicated-profile"
    profile.mkdir()
    account = BrowserAccount(uuid4(), "offline", "offline.operator", profile)
    store = CheckpointStore(tmp_path / "data")
    lifecycle = {"closed": False, "manager_exited": False}

    class Context:
        def __init__(self) -> None:
            self.pages = [object()]

        async def close(self):
            lifecycle["closed"] = True

    class Chromium:
        async def launch_persistent_context(self, **_kwargs):
            return Context()

    class Manager:
        async def __aenter__(self):
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *_args):
            lifecycle["manager_exited"] = True

    async def wait_for_cancellation(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(workflow, "async_playwright", Manager)
    monkeypatch.setattr(workflow, "_run_page_workflow_locked", wait_for_cancellation)

    async def exercise() -> None:
        task = asyncio.create_task(
            workflow.run_browser_nurture(
                account,
                store,
                max_posts=1,
                terms=("hiring",),
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert lifecycle == {"closed": True, "manager_exited": True}
    checkpoint = store.load(account.account_id, policy_fingerprint(("hiring",)))
    assert checkpoint.seen_fingerprints == ()
    assert checkpoint.pending_read_fingerprints == ()
    with store.profile_lock(account):
        pass


def test_cancellation_during_browser_cleanup_holds_lock_until_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "dedicated-profile"
    profile.mkdir()
    account = BrowserAccount(uuid4(), "offline", "offline.operator", profile)
    store = CheckpointStore(tmp_path / "data")
    close_started = asyncio.Event()
    finish_close = asyncio.Event()
    lifecycle = {"manager_exited": False}

    class Context:
        def __init__(self) -> None:
            self.pages = [object()]

        async def close(self):
            close_started.set()
            await finish_close.wait()

    class Chromium:
        async def launch_persistent_context(self, **_kwargs):
            return Context()

    class Manager:
        async def __aenter__(self):
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *_args):
            lifecycle["manager_exited"] = True

    async def successful_cycle(*_args, **_kwargs):
        return SimpleNamespace(status="COMPLETE")

    monkeypatch.setattr(workflow, "async_playwright", Manager)
    monkeypatch.setattr(workflow, "_run_page_workflow_locked", successful_cycle)

    async def exercise() -> None:
        task = asyncio.create_task(
            workflow.run_browser_nurture(
                account, store, max_posts=1, terms=("hiring",)
            )
        )
        await asyncio.wait_for(close_started.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)

        probe = store.profile_lock(account)
        try:
            probe.acquire()
        except BrowserDataError as error:
            assert error.code == "ACCOUNT_BUSY"
        else:
            probe.release()
            pytest.fail("profile lock was released before browser shutdown")

        finish_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert lifecycle["manager_exited"]
    with store.profile_lock(account):
        pass


@pytest.mark.parametrize("failure", ("timeout", "exception"))
def test_browser_cleanup_failure_is_reported_after_successful_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    profile = tmp_path / "dedicated-profile"
    profile.mkdir()
    account = BrowserAccount(uuid4(), "offline", "offline.operator", profile)
    store = CheckpointStore(tmp_path / "data")
    lifecycle = {"manager_exited": False}

    class Context:
        def __init__(self) -> None:
            self.pages = [object()]

        async def close(self):
            if failure == "timeout":
                await asyncio.Event().wait()
            raise RuntimeError("synthetic cleanup failure")

    class Chromium:
        async def launch_persistent_context(self, **_kwargs):
            return Context()

    class Manager:
        async def __aenter__(self):
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *_args):
            lifecycle["manager_exited"] = True

    async def successful_cycle(*_args, **_kwargs):
        return SimpleNamespace(status="COMPLETE")

    monkeypatch.setattr(workflow, "async_playwright", Manager)
    monkeypatch.setattr(workflow, "_run_page_workflow_locked", successful_cycle)
    if failure == "timeout":
        monkeypatch.setattr(workflow, "_CLEANUP_ALLOWANCE_SECONDS", 0.01)

    with pytest.raises(UiReadError) as caught:
        asyncio.run(
            workflow.run_browser_nurture(
                account,
                store,
                max_posts=1,
                terms=("hiring",),
            )
        )

    assert caught.value.code == "BROWSER_CLEANUP_FAILED"
    assert lifecycle["manager_exited"]
    with store.profile_lock(account):
        pass


def test_browser_workflow_detail_requires_tty_before_profile_or_browser_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = BrowserAccount(
        uuid4(),
        "offline",
        "offline.operator",
        tmp_path / "synthetic-profile",
    )
    monkeypatch.setattr(workflow, "_detail_terminal_ready", lambda: False)
    monkeypatch.setattr(
        workflow,
        "async_playwright",
        lambda: pytest.fail("detail mode accessed browser without a TTY"),
    )

    with pytest.raises(UiReadError) as caught:
        asyncio.run(
            workflow.run_browser_nurture(
                account,
                CheckpointStore(tmp_path / "data"),
                max_posts=1,
                terms=("hiring",),
                detail=True,
            )
        )

    assert caught.value.code == "DETAIL_TTY_REQUIRED"


def test_relevance_search_finds_visible_post_after_two_observed_advances(
    tmp_path: Path,
) -> None:
    actor = "offline.operator"
    detail_url = "https://www.threads.com/@candidate/post/relevant-third"
    detail_body = "Hiring a senior engineer"
    feed_html = f"""<!doctype html><html><head><style>
article {{ box-sizing: border-box; height: 550px; padding-top: 60px; }}
article:first-of-type {{ padding-top: 400px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
<article><a href="/@candidate/post/first"><time>3h</time></a>
  <a href="/@candidate">@candidate</a><p>A weather update</p></article>
<article><a href="/@candidate/post/second"><time>2h</time></a>
  <a href="/@candidate">@candidate</a><p>A local event</p></article>
<article><a href="/@candidate/post/relevant-third"><time>1h</time></a>
  <a href="/@candidate">@candidate</a><p>{detail_body}</p></article>
</main><script>
window.addEventListener("wheel", event => {{
  event.preventDefault();
  const count = Number(sessionStorage.getItem("wheel-count") || "0") + 1;
  sessionStorage.setItem("wheel-count", String(count));
  sessionStorage.setItem("wheel-delta", String(event.deltaY));
  window.scrollBy(0, event.deltaY);
}}, {{ passive: false }});
</script></body></html>"""
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate/post/relevant-third"><time>1h</time></a>
<a href="/@candidate">@candidate</a><p>{detail_body}</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=detail_html,
            )

        await page.route(detail_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=3,
            max_scrolls=5,
            max_total_posts=20,
            terms=("hiring", "engineer"),
        )
        assert result.status == "COMPLETE"
        assert result.home_sampled == 3
        assert result.home_new == 3
        assert result.home_relevant == 1
        assert result.home_irrelevant == 2
        assert result.detail_reads == 1
        assert result.scroll_attempts == result.scrolls == 2
        assert result.checkpoint_runs == 1
        await _assert_bounded_wheel_gestures(page, result.scrolls)

    _run_offline_page(feed_html, exercise)
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 3
    assert checkpoint.detail_reads == 1
    assert checkpoint.scroll_attempts == checkpoint.scrolls == 2


def test_overlapping_wheel_finds_intermediate_relevant_post(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    detail_url = "https://www.threads.com/@candidate/post/intermediate"
    detail_body = "Hiring an engineer for the infrastructure team"
    feed_html = f"""<!doctype html><html><head><style>
article {{ box-sizing: border-box; padding-top: 60px; }}
article:first-of-type {{ height: 808px; padding-top: 350px; }}
article:last-of-type {{ height: 800px; }}
article:nth-of-type(2) {{ height: 180px; padding-top: 0; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
<article><a href="/@candidate/post/first"><time>3h</time></a>
  <a href="/@candidate">@candidate</a><p>A weather update</p></article>
<article><a href="/@candidate/post/intermediate"><time>2h</time></a>
  <a href="/@candidate">@candidate</a><p>{detail_body}</p></article>
<article><a href="/@candidate/post/last"><time>1h</time></a>
  <a href="/@candidate">@candidate</a><p>A local event</p></article>
</main><script>
window.addEventListener("wheel", event => {{
  event.preventDefault();
  sessionStorage.setItem("wheel-delta", String(event.deltaY));
  window.scrollBy(0, event.deltaY);
}}, {{ passive: false }});
</script></body></html>"""
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate/post/intermediate"><time>2h</time></a>
<a href="/@candidate">@candidate</a><p>{detail_body}</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=detail_html,
            )

        await page.route(detail_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=1,
            max_total_posts=10,
            terms=("hiring", "engineer"),
            show_post_text=True,
        )
        assert result.status == "COMPLETE"
        assert result.home_sampled == 2
        assert result.home_irrelevant == result.home_relevant == 1
        assert result.detail_reads == 1
        assert result.scroll_attempts == result.scrolls == 1
        assert float(await page.evaluate("sessionStorage.getItem('wheel-delta')")) < 360

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "delta_band=SMALL" in output
    assert "classified=1" in output
    assert "irrelevant=0 unknown=0" in output
    assert output.count("A weather update") == 1
    assert output.count(detail_body) == 1
    assert output.count("<<< POST TEXT BEGIN >>>") == 2
    assert "POST_TEXT viewport=1 status=EXTRACTED decision=IRRELEVANT" in output
    assert "POST_TEXT viewport=2 status=EXTRACTED decision=RELEVANT" in output
    assert "STOP" not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 2
    assert checkpoint.detail_reads == 1
    assert checkpoint.scroll_attempts == checkpoint.scrolls == 1
    checkpoint_path = (
        data_root
        / "nurture"
        / "checkpoints"
        / f"{account.account_id}-{checkpoint.policy_fingerprint}.json"
    )
    persisted = checkpoint_path.read_text(encoding="utf-8")
    assert "A weather update" not in persisted
    assert detail_body not in persisted


def test_post_text_preview_escapes_terminal_and_bidi_controls() -> None:
    unsafe = "Hiring\x1b[31m engineer\u202e remote\nnext\rline\tend"
    assert workflow._terminal_safe_post_text(unsafe) == (
        "Hiring\\u001B[31m engineer\\u202E remote\nnext\\u000Dline\\tend"
    )


@pytest.mark.parametrize(
    ("character", "escaped"),
    [
        ("\u200b", "\\u200B"),
        ("\u206a", "\\u206A"),
        ("\u206f", "\\u206F"),
    ],
)
def test_post_text_preview_escapes_unicode_format_characters(
    character: str,
    escaped: str,
) -> None:
    assert workflow._terminal_safe_post_text(f"before{character}after") == (
        f"before{escaped}after"
    )


def test_unverified_candidate_does_not_block_later_verified_selection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    detail_url = "https://www.threads.com/@candidate/post/later-valid"
    invalid_urls = (
        "/@candidate/post/bad/one",
        "/@candidate/post/bad/two",
    )
    detail_body = "We are hiring a platform engineer"
    feed_html = f"""<!doctype html><html><head><style>
.post-card {{ box-sizing: border-box; height: 760px; padding-top: 60px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
<div class="post-card" data-pressable-container="true">
  <a href="{invalid_urls[0]}"><time>2h</time></a>
  <a href="{invalid_urls[1]}">Permalink B</a>
  <a href="/@candidate">@candidate</a><p>Hiring content must be skipped</p>
</div>
<div class="post-card" data-pressable-container="true">
  <a href="/@candidate/post/later-valid"><time>1h</time></a>
  <a href="/@candidate">@candidate</a><p>{detail_body}</p></div>
</main><script>
window.addEventListener("wheel", event => {{
  event.preventDefault();
  const count = Number(sessionStorage.getItem("wheel-count") || "0") + 1;
  sessionStorage.setItem("wheel-count", String(count));
  sessionStorage.setItem("wheel-delta", String(event.deltaY));
  window.scrollBy(0, event.deltaY);
}}, {{ passive: false }});
</script></body></html>"""
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate/post/later-valid"><time>1h</time></a>
<a href="/@candidate">@candidate</a><p>{detail_body}</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    detail_requests = 0

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=detail_html,
            )

        await page.route(detail_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=3,
            max_total_posts=10,
            terms=("hiring", "engineer"),
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.home_sampled == result.home_new == 1
        assert result.home_relevant == 1
        assert result.skipped_unverified == 1
        assert result.scroll_attempts == result.scrolls
        assert result.scroll_attempts > 0
        assert result.detail_reads == result.checkpoint_runs == 1
        await _assert_bounded_wheel_gestures(page, result.scrolls)
        assert float(await page.evaluate("sessionStorage.getItem('wheel-delta')")) < 360

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert detail_requests == 1
    assert (
        "SKIPPED stage=FEED_INITIAL reason=CANDIDATE_NONCANONICAL count=1 "
        "cause=POST_PATH_SHAPE_INVALID"
    ) in output
    assert (
        "SCANNING viewport=1 verified=0 relevant=0 skipped=1"
    ) in output
    assert "body_read=0 body_unknown=0 classified=1" in output
    assert "unverified_relevant=1" in output
    assert "SCROLL attempt=1 observed=1" in output
    assert all(value not in output for value in (*invalid_urls, "Hiring content must be skipped"))
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 1
    assert checkpoint.detail_reads == 1
    assert checkpoint.pending_read_fingerprints == ()


def test_timestamp_permalink_accepts_safe_trailing_slash_and_ignores_bad_secondary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    detail_url = "https://www.threads.com/@candidate/post/trailing-safe/"
    invalid_secondary = "/@candidate/post/secondary/extra?marker=private"
    body = "Synthetic hiring note with one safely verified timestamp permalink."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><div class="post-card">
  <a href="/@candidate/post/trailing-safe/"><time>1h</time></a>
  <a href="{invalid_secondary}">Secondary</a>
  <a href="/@candidate">@candidate</a><span>{body}</span>
</div></main></body></html>"""
    detail_html = f"""<!doctype html><html><body><main><div class="post-card">
  <a href="/@candidate/post/trailing-safe/"><time>1h</time></a>
  <a href="/@candidate">@candidate</a><p>{body}</p>
</div></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    detail_requests = 0

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=detail_html,
            )

        await page.route(detail_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=3,
            max_scrolls=0,
            terms=("hiring",),
            detail=True,
        )
        assert result.status == "COMPLETE"
        assert result.home_sampled == result.provenance_verified == 1
        assert result.detail_reads == 1
        assert result.viewport_coverage_complete

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert detail_requests == 1
    assert "[ACTIONABILITY] status=CONFIRMED" in output
    assert invalid_secondary not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 1


def test_noncanonical_link_reasons_are_aggregate_private_and_skipped(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    private_values = (
        "query-private-marker",
        "fragment-private-marker",
        "private-origin.invalid",
        "off-origin-private-marker",
        "broken-path-private-marker",
        "bad-author-private-marker",
        "private-author-body-marker",
        "private-wrapper-body-marker",
    )
    rejected_bodies = (
        "Hiring query body",
        "Hiring fragment body",
        "Hiring origin body",
        "Hiring path body",
        private_values[6],
        private_values[7],
    )
    feed_html = f"""<!doctype html><html><head><style>
.post-card {{ box-sizing: border-box; height: 55px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
<div class="post-card" data-pressable-container="true"><a href="/@candidate/post/query-id?token={private_values[0]}">Query</a>
  <p>Hiring query body</p></div>
<div class="post-card" data-pressable-container="true"><a href="/@candidate/post/fragment-id#{private_values[1]}">Fragment</a>
  <p>Hiring fragment body</p></div>
<div class="post-card" data-pressable-container="true"><a href="https://{private_values[2]}/@candidate/post/{private_values[3]}">Origin</a>
  <p>Hiring origin body</p></div>
<div class="post-card" data-pressable-container="true"><a href="/@candidate/post/{private_values[4]}/extra">Path</a>
  <p>Hiring path body</p></div>
<div class="post-card" data-pressable-container="true"><a href="/@bad!author/post/{private_values[5]}">Author</a>
  <p>Hiring author body</p></div>
<div class="post-card" data-pressable-container="true"><a href="/@candidate/post/author-mismatch">Post</a>
  <a href="/@different.author">Author</a><p>{private_values[6]}</p></div>
<div class="post-card" data-pressable-container="true"><a href="/@candidate/post/wrapper-one">Post one</a>
  <a href="/@candidate/post/wrapper-two">Post two</a>
  <a href="/@candidate">Author</a><p>{private_values[7]}</p></div>
</main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=20,
            max_scrolls=0,
            max_total_posts=10,
            terms=("hiring",),
            show_post_text=True,
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.home_sampled == 0
        assert result.skipped_unverified == 8, result
        assert result.unverified_relevant > 0
        assert result.detail_reads == 0
        assert result.checkpoint_runs == 1
        assert not result.viewport_coverage_complete

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "cause=POST_LINK_HAS_QUERY_OR_FRAGMENT" in output
    assert "cause=POST_ORIGIN_INVALID" in output
    assert "cause=POST_PATH_SHAPE_INVALID" in output
    assert "cause=POST_AUTHOR_OR_ID_INVALID" in output
    assert "cause=CANDIDATE_AUTHOR_MISMATCH" in output
    assert "cause=CANDIDATE_WRAPPER_AMBIGUOUS" in output
    assert "url_parsed=3 author_unverified=1 wrapper_ambiguous=1 card_not_ready=0" in output
    assert (
        "rejection_reasons=CANDIDATE_AUTHOR_MISMATCH:1,"
        "CANDIDATE_WRAPPER_AMBIGUOUS:1,POST_AUTHOR_OR_ID_INVALID:1,"
        "POST_LINK_HAS_QUERY_OR_FRAGMENT:2,POST_ORIGIN_INVALID:1,"
        "POST_PATH_SHAPE_INVALID:1"
    ) in output
    assert all(value not in output for value in private_values)
    assert all(body not in output for body in rejected_bodies)
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 0
    assert checkpoint.seen_fingerprints == ()


def test_query_text_does_not_make_a_profile_link_look_like_a_post_path() -> None:
    actor = "offline.operator"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article>
<a href="/@candidate/?next=/post/ignored"><time>now</time></a>
<a href="/@candidate/post/synthetic-valid">Post</a>
<a href="/@candidate">candidate</a><p>A synthetic weather update</p>
</article></main></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        targets = await reader.scan_viewport(1)
        assert len(targets) == 1
        assert targets[0].url.endswith("/post/synthetic-valid")
        assert targets[0].author == "candidate"
        assert reader.last_scan_counts.url_canonical_links == 1
        assert reader.last_scan_counts.url_rejected_links == 0

    _run_offline_page(feed_html, exercise)


def test_bounded_fallback_discovery_handles_dense_synthetic_timestamp_fixture() -> None:
    actor = "offline.operator"
    rows = "".join(
        f"""<div class="row"><a href="/@candidate"><time>{index}h</time></a>
<a href="/@candidate">candidate</a><p>Weather fixture {index}</p></div>"""
        for index in range(160)
    )
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
nav {{ height: 24px; }}
.row {{ height: 4px; overflow: visible; }}
.row p {{ margin: 0; line-height: 1px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main>{rows}</main></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        viewport = page.viewport_size
        assert viewport is not None
        region = await reader._content_region()
        started = time.perf_counter()
        roots, truncated = await reader._top_level_candidate_roots(region, viewport)
        await reader._collect_structure_diagnostics(region, roots, viewport)
        elapsed_ms = (time.perf_counter() - started) * 1000
        assert len(roots) == reader_module._MAX_VISIBLE_ROW_CANDIDATES
        assert truncated
        assert elapsed_ms < 10_000

    _run_offline_page(feed_html, exercise)


def test_home_and_detail_canonicalize_multiline_inline_body_equally_without_logging(
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    detail_url = "https://www.threads.com/@candidate/post/multiline"
    expected_body = (
        "Hiring engineers 👩🏽‍💻 in Việt Nam\n"
        "Second paragraph with inline emojis 🚀 and Unicode café."
    )
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
  <a href="/@candidate/post/multiline"><time>1h</time></a>
  <a href="/@candidate">@candidate</a>
  <p>
    Hiring   <span>engineers 👩🏽‍💻</span>   in Việt Nam
  </p>
  <p>Second paragraph with <em>inline emojis 🚀</em> and Unicode café.</p>
</article></main></body></html>"""
    detail_html = """<!doctype html><html><head><meta charset="utf-8"></head><body><main><article>
<a href="/@candidate/post/multiline"><time>1h</time></a>
<a href="/@candidate">candidate</a>
<p>  Hiring <span> engineers 👩🏽‍💻 </span> in   Việt Nam </p>
<p>
  Second paragraph with <em>inline emojis 🚀</em> and Unicode café.
</p>
</article></main></body></html>"""

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=detail_html,
            )

        await page.route(detail_url, serve_detail)
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        targets = await reader.scan_viewport(1)
        assert len(targets) == 1
        target = targets[0]
        assert target.body == expected_body
        assert target.body_complete
        detail = await reader.read_detail(target)
        assert detail.body == target.body == expected_body
        assert detail.url == detail_url
        await reader.return_to_home()
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert expected_body not in output
    assert "READY stage=DETAIL state=DETAIL_VERIFIED" in output


def test_visible_body_segments_keep_utf16_surrogate_pairs_intact() -> None:
    actor = "offline.operator"
    emoji = "👩🏽‍💻"
    body = "A" * 4095 + emoji + "B" * 4090
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
nav {{ height: 32px; }}
article {{ position: relative; height: 1400px; margin: 0; }}
article p {{ position: absolute; top: 0; left: 0; width: 1280px; margin: 0;
font: 16px/18px monospace; overflow-wrap: anywhere; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><a href="/@candidate/post/utf16"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p></article></main></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        rows = []

        async def record(row) -> None:
            rows.append(row)

        await reader.scan_viewport(1, on_row=record)
        target = next(row.target for row in rows if row.target is not None)
        assert target.body is not None
        assert not target.body_complete
        assert emoji in target.body
        assert target.visible_segments
        assert all(
            not 0xD800 <= ord(character) <= 0xDFFF
            for segment in target.visible_segments
            for character in segment.text
        )

    _run_offline_page(feed_html, exercise)


def test_partially_clipped_range_stays_missing_after_forward_wheels() -> None:
    letters = "ABCDEFGHIJKLM"
    spans = "".join(f"<span>{letter}</span>" for letter in letters)
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; }}
article {{ position: relative; height: 320px; margin: 0; }}
article p {{ position: absolute; top: 40px; left: 0; width: 200px;
margin: 0; font: 16px/18px monospace; }}
article p span {{ display: block; height: 18px; }}
article p span:first-child {{ position: relative; left: -2px; }}
#spacer {{ height: 2000px; }}
</style></head><body><main><article><p>{spans}</p></article>
<div id="spacer"></div></main></body></html>"""

    async def exercise(page) -> None:
        await page.set_viewport_size({"width": 1280, "height": 720})
        reader = ThreadsHomeReader(page, "offline.operator")
        root = page.locator("article")
        root_handle = await root.element_handle()
        assert root_handle is not None
        try:
            first = await reader._read_visible_body_progress(
                root,
                root_handle,
                fingerprint="synthetic-clipped-range",
            )
            evidence = first.read_evidence
            assert not first.complete
            assert evidence.render_rect_count == 13
            assert evidence.visible_text_rect_count == 13
            assert evidence.fully_contained_text_rect_count == 12
            assert evidence.intersect_only_text_rect_count == 1
            assert evidence.missing_range_count == 1
            assert "missing_horizontal_left" in evidence.missing_range_topology
            assert evidence.unrecoverable_range_count == 1
            assert evidence.unrecoverable_range_signature

            for _ in range(3):
                previous_scroll = await page.evaluate("window.scrollY")
                await page.mouse.wheel(0, 288)
                await page.wait_for_function(
                    "previous => window.scrollY > previous",
                    arg=previous_scroll,
                )
                current = await reader._read_visible_body_progress(
                    root,
                    root_handle,
                    fingerprint="synthetic-clipped-range",
                )
                assert not current.complete
                assert current.read_evidence.missing_range_count == 1

            assert "missing_vertical_above" in current.read_evidence.missing_range_topology
        finally:
            await root_handle.dispose()

    _run_offline_page(feed_html, exercise)


@pytest.mark.parametrize(
    ("body", "body_top", "body_left", "font", "sequence", "scroll_to_complete"),
    [
        ("😀", 682, 0, "32px/48px monospace", "😀", True),
        ("😀", -20, 0, "32px/48px monospace", "😀", False),
        ("😀", 200, -10, "32px/48px monospace", "😀", False),
        (
            "line\n" * 12 + "👩🏽‍💻" + "\nend",
            320,
            0,
            "24px/32px monospace",
            "👩🏽‍💻",
            True,
        ),
    ],
    ids=("bottom-astral", "top-astral", "left-astral", "bottom-zwj"),
)
def test_partially_clipped_unicode_ranges_wait_until_fully_visible(
    body: str,
    body_top: int,
    body_left: int,
    font: str,
    sequence: str,
    scroll_to_complete: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; }}
article {{ position: relative; height: 1600px; margin: 0; }}
article p {{ position: absolute; top: {body_top}px; left: {body_left}px; width: 1200px;
margin: 0; white-space: pre; font: {font}; }}
</style></head><body><main><article><p>{body}</p></article></main></body></html>"""

    async def exercise(page) -> None:
        await page.set_viewport_size({"width": 1280, "height": 720})
        reader = ThreadsHomeReader(page, "offline.operator")
        root = page.locator("article")
        root_handle = await root.element_handle()
        assert root_handle is not None
        try:
            first = await reader._read_visible_body_progress(
                root,
                root_handle,
                fingerprint="synthetic-partial-unicode",
            )
            assert not first.complete
            assert first.read_evidence.missing_range_count
            assert first.read_evidence.visible_text_rect_count
            assert sequence not in (first.body or "")

            if not scroll_to_complete:
                return
            await page.mouse.wheel(0, 288)
            await page.wait_for_function("window.scrollY > 0")
            completed = await reader._read_visible_body_progress(
                root,
                root_handle,
                fingerprint="synthetic-partial-unicode",
            )
            assert completed.complete
            assert completed.body is not None
            assert sequence in completed.body
            assert completed.read_evidence.missing_range_count == 0
            assert completed.read_evidence.visited_range_count == (
                completed.read_evidence.expected_range_count
            )
            assert all(
                not 0xD800 <= ord(character) <= 0xDFFF
                for segment in completed.segments
                for character in segment.text
            )
        finally:
            await root_handle.dispose()

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "RangeError" not in output
    assert "error_kind=OTHER" not in output


def test_body_range_depth_limit_marks_read_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    body = "Synthetic body fragment. " * 100
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; }}
nav {{ height: 32px; }}
article {{ margin: 0; }}
article p {{ width: 300px; margin: 0; font: 20px/24px monospace; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><a href="/@candidate/post/range-depth"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p></article></main></body></html>"""
    monkeypatch.setattr(body_text_module, "_MAX_BODY_RANGE_DEPTH", 1)

    async def exercise(page) -> None:
        await page.set_viewport_size({"width": 1280, "height": 720})
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        rows = []

        async def record(row) -> None:
            if row.target is not None:
                rows.append(row.target)

        await reader.scan_viewport(1, on_row=record)
        assert rows
        target = rows[-1]
        assert not target.body_complete
        assert target.read_evidence is not None
        assert target.read_evidence.body_incomplete_reason == "LIMIT"
        assert not reader.last_scan_counts.coverage_complete

    _run_offline_page(feed_html, exercise)


def test_lookahead_rows_are_separate_from_visited_body_gaps() -> None:
    actor = "offline.operator"
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; }}
nav {{ height: 32px; }}
article {{ position: relative; box-sizing: border-box; height: 400px; margin: 0; }}
.identity {{ position: absolute; top: 8px; left: 8px; }}
.body {{ position: absolute; top: 700px; left: 8px; font: 20px/28px monospace; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main>
<article><div class="identity"><a href="/@candidate/post/lookahead"><time>now</time></a><a href="/@candidate">candidate</a></div>
<div class="body"><p>A synthetic lookahead body.</p></div></article>
<article><div class="identity"><a href="/@candidate/post/lookahead"><time>now</time></a><a href="/@candidate">candidate</a></div>
<div class="body"><p>A synthetic lookahead body.</p></div></article>
</main></body></html>"""

    async def exercise(page) -> None:
        await page.set_viewport_size({"width": 1280, "height": 720})
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        await reader.scan_viewport(4)

        assert not reader.last_scan_counts.coverage_complete
        assert reader.last_scan_counts.unresolved_visited_rows == 0, [
            (
                diagnostic.state,
                None
                if diagnostic.read_evidence is None
                else (
                    diagnostic.read_evidence.visible_text_rect_count,
                    diagnostic.read_evidence.closest_text_side,
                    diagnostic.read_evidence.closest_text_distance_px,
                ),
            )
            for diagnostic in reader.last_candidate_diagnostics
        ]
        assert reader.last_scan_counts.lookahead_rows == 1
        unresolved = [
            diagnostic
            for diagnostic in reader.last_candidate_diagnostics
            if diagnostic.state not in {"NON_POST", "VERIFIED"}
        ]
        assert len(unresolved) == 2
        assert all(diagnostic.read_evidence is not None for diagnostic in unresolved)
        assert all(
            diagnostic.read_evidence.visible_text_rect_count == 0
            and diagnostic.read_evidence.visited_range_count == 0
            and diagnostic.read_evidence.closest_text_side == "BELOW"
            for diagnostic in unresolved
        )

    _run_offline_page(feed_html, exercise)


def test_body_progress_completes_once_when_remaining_ranges_become_visible() -> None:
    actor = "offline.operator"
    body = "Synthetic hiring fixture fragment. " * 30
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
nav {{ height: 32px; }}
article {{ margin: 0; }}
article p {{ width: 400px; margin: 0; font: 16px/24px monospace; overflow-wrap: anywhere; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><a href="/@candidate/post/viewport-upgrade"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""

    async def exercise(page) -> None:
        await page.set_viewport_size({"width": 1280, "height": 220})
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        ledger = CoverageLedger()
        outcomes = []
        first_evidence = []

        async def observe(row) -> None:
            target = row.target
            if target is None:
                return
            facts = RowObservationFacts(
                fingerprint=target.fingerprint,
                observation_id=row.diagnostic.observation_id,
                gap_id="synthetic-row",
                state=row.diagnostic.state,
                has_body=bool(target.body and target.body.strip()),
                body_complete=target.body_complete,
            )
            outcomes.append(ledger.observe_row(facts))
            first_evidence.append(target.read_evidence)

        await reader.scan_viewport(1, on_row=observe)
        assert ledger.read_in_progress
        assert ledger.unresolved_gap_count == 1
        assert first_evidence[-1] is not None
        assert first_evidence[-1].body_incomplete_reason == "UNVISITED_RANGE"
        assert first_evidence[-1].missing_range_count

        await page.set_viewport_size({"width": 1280, "height": 900})
        completed_targets = await reader.scan_viewport(1, on_row=observe)
        assert len(completed_targets) == 1
        assert completed_targets[0].body_complete
        assert completed_targets[0].read_evidence is not None
        assert completed_targets[0].read_evidence.body_incomplete_reason == "NONE"
        assert (
            completed_targets[0].read_evidence.render_rect_count
            == completed_targets[0].read_evidence.visible_text_rect_count
        )
        assert sum(outcome.body_completed for outcome in outcomes) == 1
        assert ledger.read_in_progress == set()
        assert ledger.fully_read == {completed_targets[0].fingerprint}
        assert ledger.unresolved_gap_count == 0

        await reader.scan_viewport(1, on_row=observe)
        assert sum(outcome.body_completed for outcome in outcomes) == 1
        assert ledger.unresolved_gap_count == 0

    _run_offline_page(feed_html, exercise)


def test_max_scrolls_12_recovers_body_after_next_scroll_and_checkpoints(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    detail_url = "https://www.threads.com/@candidate/post/recover-after-scroll"
    body = "Synthetic hiring fixture fragment. " * 10
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; }}
nav {{ height: 36px; }}
article {{ position: relative; height: 1100px; margin: 0; }}
article .identity {{ position: absolute; top: 300px; left: 0; }}
article p {{ position: absolute; top: 600px; left: 0; width: 400px;
  margin: 0; font: 16px/24px monospace; overflow-wrap: anywhere; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><div class="identity">
<a href="/@candidate/post/recover-after-scroll"><time>now</time></a>
<a href="/@candidate">candidate</a></div><p>{body}</p></article></main></body></html>"""
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate/post/recover-after-scroll"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p></article></main></body></html>"""
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")

    async def exercise(page) -> None:
        await page.route(
            detail_url,
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=detail_html,
            ),
        )
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=12,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE"
        assert result.home_sampled == 1
        assert result.home_relevant == 1
        assert result.detail_reads == 1
        assert result.scroll_attempts == result.scrolls == 1
        assert result.viewport_coverage_complete

    _run_offline_page(feed_html, exercise)
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 1
    assert checkpoint.detail_reads == 1
    assert checkpoint.pending_read_fingerprints == ()
    output = capsys.readouterr().out
    assert "COVERAGE_STOP reason=SCROLL_BUDGET_EXHAUSTED_WITH_GAP" not in output
    assert body not in output


def test_partial_body_does_not_block_a_verified_selected_post(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    spans = "".join(f"<span>{letter}</span>" for letter in "ABCDEFGHIJKLM")
    detail_url = "https://www.threads.com/@candidate/post/verified-after-partial"
    eligible_body = "Synthetic hiring role fixture."
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate/post/verified-after-partial"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{eligible_body}</p></article></main></body></html>"""
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; }}
nav {{ height: 36px; }}
article {{ position: relative; height: 320px; margin: 0; }}
article p {{ position: absolute; top: 40px; left: 0; width: 200px;
margin: 0; font: 16px/18px monospace; }}
article p span {{ display: block; height: 18px; }}
article p span:first-child {{ position: relative; left: -2px; }}
#spacer {{ height: 2000px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><a href="/@candidate/post/clipped-range"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{spans}</p></article>
<article><a href="/@candidate/post/verified-after-partial"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{eligible_body}</p></article>
<div id="spacer"></div></main></body></html>"""
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")

    async def exercise(page) -> None:
        await page.route(
            detail_url,
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=detail_html,
            ),
        )
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=0,
            terms=("hiring",),
            detail=True,
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.scroll_attempts == result.scrolls == 0
        assert result.detail_reads == 1
        assert result.checkpoint_runs == 1
        assert result.unresolved_gaps == 1
        assert not result.viewport_coverage_complete

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "[DETAIL] post=1 status=VERIFIED" in output
    assert "[BACK] post=1 status=RESTORED anchor=IDENTITY_VERIFIED" in output
    assert "[CHECKPOINT] status=SAVED completed_runs=1" in output
    assert "[CYCLE] status=COMPLETE_WITH_SKIPS" in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 1
    assert checkpoint.detail_reads == 1
    assert len(checkpoint.seen_fingerprints) == 1
    assert checkpoint.pending_read_fingerprints == ()


def test_max_scrolls_12_checkpoints_with_a_new_incomplete_post(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    body = "Hiring synthetic backend engineer fixture. " * 900
    earlier_posts = "".join(
        f'''<article><a href="/@candidate/post/earlier-{index}"><time>{index}h</time></a>
<a href="/@candidate">candidate</a><p>A synthetic weather update.</p></article>'''
        for index in range(10)
    )
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; }}
nav {{ height: 36px; }}
article {{ box-sizing: border-box; height: 400px; margin: 0; }}
article.long {{ height: auto; width: 320px; }}
article.long p {{ margin: 0; font: 16px/24px monospace; overflow-wrap: anywhere; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main>{earlier_posts}<article class="long">
<a href="/@candidate/post/new-long"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p></article></main></body></html>"""
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            post_path = urlsplit(route.request.url).path
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=(
                    "<!doctype html><html><body><main><article>"
                    f'<a href="{post_path}"><time>now</time></a>'
                    '<a href="/@candidate">candidate</a>'
                    "<p>A synthetic weather update.</p>"
                    "</article></main></body></html>"
                ),
            )

        await page.route("**/@candidate/post/earlier-*", serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=3,
            max_scrolls=12,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.stop_reason == "MAX_SCROLLS"
        assert result.scroll_attempts == result.scrolls == 12
        assert result.home_sampled <= 10
        assert result.detail_reads == 1
        assert result.checkpoint_runs == 1
        assert not result.viewport_coverage_complete

    _run_offline_page(feed_html, exercise)
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled > 0
    assert checkpoint.detail_reads == 1
    assert checkpoint.pending_read_fingerprints == ()
    output = capsys.readouterr().out
    assert body not in output


def test_body_text_changing_during_the_home_read_stays_unverified() -> None:
    actor = "offline.operator"
    initial_body = "Synthetic hiring fixture alpha text."
    changed_body = "Synthetic hiring fixture bravo text."
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"></head><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><a href="/@candidate/post/changing"><time>now</time></a>
<a href="/@candidate">candidate</a><p id="body">{initial_body}</p>
</article></main></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        original_read = reader._read_card
        read_count = 0

        async def change_after_first_read(root, **kwargs):
            nonlocal read_count
            target = await original_read(root, **kwargs)
            read_count += 1
            if read_count == 1:
                await page.locator("#body").evaluate(
                    "(node, value) => node.textContent = value",
                    changed_body,
                )
            return target

        reader._read_card = change_after_first_read
        targets = await reader.scan_viewport(1)
        assert not targets
        assert read_count >= 2
        assert reader.last_candidate_diagnostics[0].reason == (
            "ROW_CONTENT_CHANGED_DURING_READ"
        )
        assert reader.last_candidate_diagnostics[0].state == "OBSERVED_UNVERIFIED"
        assert reader.last_viewport_can_wheel
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)


def test_irrelevant_posts_exhaust_scroll_budget_as_completed_read_only_search(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    feed_html = f"""<!doctype html><html><head><style>
article {{ box-sizing: border-box; height: 600px; padding-top: 60px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
<article><a href="/@candidate/post/one"><time>3h</time></a>
  <a href="/@candidate">@candidate</a><p>A weather update</p></article>
<article><a href="/@candidate/post/two"><time>2h</time></a>
  <a href="/@candidate">@candidate</a><p>A local event</p></article>
<article><a href="/@candidate/post/three"><time>1h</time></a>
  <a href="/@candidate">@candidate</a><p>A music note</p></article>
</main><script>
window.addEventListener("wheel", event => {{
  event.preventDefault();
  const count = Number(sessionStorage.getItem("wheel-count") || "0") + 1;
  sessionStorage.setItem("wheel-count", String(count));
  window.scrollBy(0, event.deltaY);
}}, {{ passive: false }});
</script></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        async def serve_verified_detail(route: Route) -> None:
            post_id = urlsplit(route.request.url).path.rsplit("/", 1)[-1]
            detail_body = {
                "one": "A weather update",
                "two": "A local event",
                "three": "A music note",
            }[post_id]
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=f"""<!doctype html><html><body><main><article>
<a href="/@candidate/post/{post_id}"><time>now</time></a>
<a href="/@candidate">@candidate</a><p>{detail_body}</p>
</article></main></body></html>""",
            )

        await page.route("**/@candidate/post/**", serve_verified_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=3,
            max_scrolls=2,
            max_total_posts=20,
            terms=("hiring",),
            detail=True,
        )
        assert result.status == "COMPLETE"
        assert result.home_sampled == 2
        assert result.home_irrelevant == 2
        assert result.home_unknown == 0
        assert result.detail_reads == 1
        assert result.scroll_attempts == result.scrolls == 2
        assert result.checkpoint_runs == 1
        await _assert_bounded_wheel_gestures(page, result.scrolls)

    _run_offline_page(feed_html, exercise)
    policy = policy_fingerprint(canonical_terms(("hiring",)))
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 2
    assert checkpoint.home_irrelevant == 2
    assert checkpoint.detail_reads == 1
    assert checkpoint.scroll_attempts == checkpoint.scrolls == 2
    output = capsys.readouterr().out
    assert "decision=VERIFIED_FALLBACK reason=NO_RELEVANT_MATCH" in output
    assert "[POST post=" in output
    assert "decision=IRRELEVANT source=DETAIL_BODY" in output


def test_total_scan_budget_checkpoints_verified_rows_and_zero_verified_still_fails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    valid_html = f"""<!doctype html><html><head><style>
article {{ box-sizing: border-box; height: 600px; padding-top: 60px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
<article><a href="/@candidate/post/one"><time>3h</time></a>
  <a href="/@candidate">@candidate</a><p>A weather update</p></article>
<article><a href="/@candidate/post/two"><time>2h</time></a>
  <a href="/@candidate">@candidate</a><p>A local event</p></article>
<article><a href="/@candidate/post/three"><time>1h</time></a>
  <a href="/@candidate">@candidate</a><p>A music note</p></article>
</main><script>
window.addEventListener("wheel", event => {{
  event.preventDefault();
  const count = Number(sessionStorage.getItem("wheel-count") || "0") + 1;
  sessionStorage.setItem("wheel-count", String(count));
  window.scrollBy(0, event.deltaY);
}}, {{ passive: false }});
</script></body></html>"""
    zero_verified_html = f"""<!doctype html><html><head><style>
.post-card {{ box-sizing: border-box; height: 600px; padding-top: 60px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
<div class="post-card"><a href="/@candidate/post/bad/one">Permalink</a></div>
<div class="post-card"><a href="/@candidate/post/bad/two">Permalink</a></div>
<div class="post-card"><a href="/@candidate/post/bad/three">Permalink</a></div>
</main><script>
window.addEventListener("wheel", event => {{
  event.preventDefault();
  const count = Number(sessionStorage.getItem("wheel-count") || "0") + 1;
  sessionStorage.setItem("wheel-count", String(count));
  window.scrollBy(0, event.deltaY);
}}, {{ passive: false }});
</script></body></html>"""
    valid_root = tmp_path / "valid"
    valid_profile = valid_root / "profile"
    valid_profile.mkdir(parents=True)
    valid_account = AccountStore(valid_root / "data").add(
        "offline",
        actor,
        valid_profile,
    )
    valid_store = CheckpointStore(valid_root / "data")

    async def valid_exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            valid_account,
            valid_store,
            max_posts=2,
            max_scrolls=8,
            max_total_posts=2,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.home_sampled <= 2
        assert result.detail_reads == 0
        assert result.scroll_attempts == result.scrolls <= 8
        assert result.checkpoint_runs == 1
        assert not result.viewport_coverage_complete
        await _assert_bounded_wheel_gestures(page, result.scrolls)

    _run_offline_page(valid_html, valid_exercise)
    valid_checkpoint = valid_store.load(
        valid_account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert valid_checkpoint.completed_runs == 1
    assert valid_checkpoint.home_sampled == 1
    assert len(valid_checkpoint.seen_fingerprints) == 1
    assert valid_checkpoint.detail_reads == 0
    assert valid_checkpoint.scroll_attempts > 0
    valid_output = capsys.readouterr().out
    assert "OBSERVED stage=FEED_INITIAL state=OBSERVATION_UNCERTAIN" in valid_output
    assert "VIEWPORT_COVERAGE_INCOMPLETE viewport=" in valid_output

    zero_root = tmp_path / "zero-verified"
    zero_profile = zero_root / "profile"
    zero_profile.mkdir(parents=True)
    zero_account = AccountStore(zero_root / "data").add(
        "offline",
        actor,
        zero_profile,
    )
    zero_store = CheckpointStore(zero_root / "data")

    async def zero_exercise(page) -> None:
        with pytest.raises(UiReadError) as caught:
            await _run_page_workflow_for_test(
                page,
                zero_account,
                zero_store,
                max_posts=3,
                max_scrolls=2,
                max_total_posts=10,
                terms=("hiring",),
            )
        assert caught.value.code == "FEED_INITIAL_NOT_READY"
        assert await page.evaluate("sessionStorage.getItem('wheel-count')") is None

    _run_offline_page(zero_verified_html, zero_exercise)
    zero_policy = policy_fingerprint(canonical_terms(("hiring",)))
    zero_checkpoint = zero_store.load(zero_account.account_id, zero_policy)
    assert zero_checkpoint.completed_runs == 0
    assert zero_checkpoint.home_sampled == 0
    output = capsys.readouterr().out
    assert "READY stage=FEED_INITIAL" not in output


def test_modern_landmark_free_div_feed_verifies_cards_and_detail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    detail_url = "https://www.threads.com/@candidate/post/job-seeking"
    first_body = "A short weather update."
    verified_body_first = "Tôi đang tìm việc"
    verified_body_second = "trong lĩnh vực kỹ thuật"
    excluded_text = (
        "đang tìm việc in a quoted post",
        "đang tìm việc in the sidebar",
        "đang tìm việc in the composer",
        "đang tìm việc in an unrelated article",
    )
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
body {{ font-family: sans-serif; }}
[data-pressable-container="true"] {{ display: block; margin: 16px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav>
<aside><article><a href="/@sidebar/post/decoy"><time>1h</time></a>
  <a href="/@sidebar">@sidebar</a><span dir="auto">{excluded_text[1]}</span>
</article></aside>
<form><div contenteditable="true" role="textbox">{excluded_text[2]}</div>
  <a href="/@composer/post/decoy">Compose</a></form>
<aside><article><p>{excluded_text[3]}</p></article></aside>
<div id="first-card" data-pressable-container="true">
  <a href="/@candidate/post/weather"><time>3h</time></a>
  <a href="/@candidate">@candidate</a>
  <div><span dir="auto">{first_body}</span></div>
  <div data-pressable-container="true"><blockquote>
    <a href="/@quoted/post/quote"><time>4h</time></a>
    <a href="/@quoted">@quoted</a>
    <span dir="auto">{excluded_text[0]}</span>
  </blockquote></div>
  <button type="button">Like</button>
</div>
<div id="second-card" data-pressable-container="true">
  <a href="/@candidate/post/job-seeking"><time>1h</time></a>
  <a href="/@candidate">@candidate</a>
  <span dir="auto">{verified_body_first}</span>
  <div dir="auto">{verified_body_second}</div>
  <button type="button">Reply</button>
</div>
</body></html>"""
    detail_html = f"""<!doctype html><html><head><meta charset="utf-8"></head><body><main>
<div data-pressable-container="true">
  <a href="/@candidate/post/job-seeking"><time>1h</time></a>
  <a href="/@candidate">@candidate</a>
  <span dir="auto">{verified_body_first}</span>
  <div dir="auto">{verified_body_second}</div>
</div>
</main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=detail_html,
            )

        await page.route(detail_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=0,
            max_total_posts=10,
            terms=("đang tìm việc",),
            show_post_text=True,
        )
        assert result.status == "COMPLETE"
        assert result.home_sampled == 2
        assert result.home_irrelevant == result.home_relevant == 1
        assert result.detail_reads == 1

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "article_roots=0 pressable_roots=2" in output
    assert f"| {first_body}" in output
    assert f"| {verified_body_first}" in output
    assert f"| {verified_body_second}" in output
    assert all(text not in output for text in excluded_text)
    preview_blocks = re.findall(
        r"<<< POST TEXT BEGIN >>>\n(.*?)\n<<< POST TEXT END >>>",
        output,
        re.DOTALL,
    )
    assert preview_blocks == [
        f"| {first_body}",
        f"| {verified_body_first}\n| {verified_body_second}",
    ]
    assert all(label not in "\n".join(preview_blocks) for label in ("Like", "Reply"))
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("đang tìm việc",))),
    )
    assert checkpoint.home_sampled == 2
    assert checkpoint.detail_reads == 1
    persisted = (
        data_root
        / "nurture"
        / "checkpoints"
        / f"{account.account_id}-{checkpoint.policy_fingerprint}.json"
    ).read_text(encoding="utf-8")
    assert all(body not in persisted for body in (first_body, *excluded_text))
    assert verified_body_first not in persisted
    assert verified_body_second not in persisted


def test_visible_card_without_permalink_is_skipped_without_fingerprint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    private_text = "Hiring text must stay unread without a post permalink"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav>
<aside><div data-pressable-container="true">
  <a href="/@sidebar/post/decoy"><time>2h</time></a>
  <a href="/@sidebar">@sidebar</a><span dir="auto">Hiring sidebar decoy</span>
</div></aside>
<form><div contenteditable="true" role="textbox">Hiring composer decoy</div>
  <a href="/@composer/post/decoy">Compose</a></form>
<article><p>Hiring unrelated article without feed evidence</p></article>
<div data-pressable-container="true">
  <time>now</time><a href="/@candidate">@candidate</a>
  <span dir="auto">{private_text}</span>
</div>
<script>window.addEventListener("wheel", () => {{
  sessionStorage.setItem("wheel-count", "1");
}});</script>
</body></html>"""
    monkeypatch.setattr(reader_module, "_READINESS_RECHECK_INTERVAL_MS", 20)
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=0,
            max_total_posts=10,
            terms=("hiring",),
            show_post_text=True,
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.observed_rows == 1
        assert result.body_extracted == 1
        assert result.provenance_verified == 0
        assert result.unverified_relevant == 1
        assert result.detail_reads == result.scroll_attempts == 0
        assert result.checkpoint_runs == 1
        assert await page.evaluate("sessionStorage.getItem('wheel-count')") is None

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert (
        "OBSERVED stage=FEED_INITIAL state=OBSERVATION_UNCERTAIN verified=0"
        in output
    )
    assert "pressable_roots=1" in output
    assert "observation_status=RELEVANT_UNVERIFIED" in output
    assert private_text not in output
    assert "Hiring sidebar decoy" not in output
    assert "Hiring composer decoy" not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 0
    assert checkpoint.seen_fingerprints == ()


def test_delayed_permalink_hydration_recovers_before_unsupported_stop() -> None:
    actor = "offline.operator"
    body = "A safely identified hydrated post"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav>
<div data-pressable-container="true">
  <time>now</time><a href="/@candidate">@candidate</a>
  <span dir="auto">{body}</span>
</div></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        await page.evaluate(
            """() => setTimeout(() => {
                const card = document.querySelector(
                    '[data-pressable-container="true"]'
                );
                const time = card.querySelector('time');
                const permalink = document.createElement('a');
                permalink.href = '/@candidate/post/hydrated';
                time.replaceWith(permalink);
                permalink.append(time);
            }, 80)"""
        )
        targets = await reader.scan_viewport(1)
        assert len(targets) == 1
        assert targets[0].url == "https://www.threads.com/@candidate/post/hydrated"
        assert targets[0].author == "candidate"
        assert targets[0].body == body

    _run_offline_page(feed_html, exercise)


def test_body_fallback_span_rows_ignore_pressable_non_post_and_nurture_verified_post(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/relevant"
    bodies = (
        "Synthetic fixture weather report.",
        "Synthetic fixture hiring a backend engineer.",
        "Synthetic fixture music note.",
    )
    rows = "".join(
        (
            '<div class="card" data-pressable-container="true">\n'
            f'<a href="/@candidate/post/{name}"><time>{index}h</time></a>\n'
            '<a href="/@candidate">@candidate</a>'
            f'<span dir="auto">{body}</span>\n'
            + (
                '<button aria-label="More">More</button>\n'
                if name == "relevant"
                else ""
            )
            + "</div>"
        )
        for index, (name, body) in enumerate(
            (("weather", bodies[0]), ("relevant", bodies[1]), ("music", bodies[2])),
            start=1,
        )
    )
    feed_html = f"""<!doctype html><html><head><style>
nav {{ height: 40px; }}
.card {{ box-sizing: border-box; height: 220px; padding: 12px; }}
#non-post {{ position: fixed; top: 2px; right: 2px; width: 90px; height: 40px; }}
.spacer {{ height: 500px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav>{rows}
<div id="non-post" data-pressable-container="true"><button>Open filters</button></div>
<div class="spacer"></div></body></html>"""
    detail_body = f"{bodies[1]} with its expanded synthetic fixture detail."
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate/post/relevant"><time>2h</time></a>
<a href="/@candidate">@candidate</a><span dir="auto">{detail_body}</span>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    detail_requests = 0

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=detail_html,
            )

        await page.route(target_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=1,
            max_total_posts=10,
            terms=("hiring", "engineer"),
            detail=True,
        )
        assert result.status == "COMPLETE"
        assert result.home_sampled == 3
        assert result.provenance_verified >= result.home_sampled
        assert result.detail_reads == 1
        assert result.scroll_attempts == result.scrolls == 1

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert detail_requests == 1
    assert "region=BODY_FALLBACK" in output
    assert "root=PRESSABLE has_visible_time=false" in output
    assert "state=NON_POST reason=NO_POST_ROW_EVIDENCE" in output
    assert "[SCROLL] attempt=1 status=ADVANCED" in output
    assert "[SELECT] post=" in output
    assert f"| {bodies[1]}" in output
    assert f"| {detail_body}" in output
    assert "Open filters" not in output
    assert "@candidate" not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 3
    assert checkpoint.detail_reads == 1


def test_progressive_body_fallback_emits_row_before_reading_next_with_y_jitter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    first_url = "https://www.threads.com/@candidate/post/progressive-first"
    first_body = "Synthetic hiring a backend engineer."
    second_body = "Synthetic weather update."
    feed_html = f"""<!doctype html><html><head><style>
@keyframes jitter {{ from {{ transform: translateY(0); }} to {{ transform: translateY(2px); }} }}
.card {{ height: 190px; animation: jitter 20ms infinite alternate; }}
.settled .card {{ animation-play-state: paused; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><div class="card" id="first">
<a href="/@candidate/post/progressive-first"><time>now</time></a>
<a href="/@candidate">@candidate</a><div><span dir="auto">{first_body}</span></div>
</div><div class="card" id="second">
<a href="/@candidate/post/progressive-second"><time>1h</time></a>
<a href="/@candidate">@candidate</a><div><span dir="auto">{second_body}</span></div>
</div></main></body></html>"""
    feed_html = feed_html.replace(
        "</body>",
        "<script>setTimeout(() => document.documentElement.classList.add('settled'), 2200);</script></body>",
    )
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate/post/progressive-first"><time>now</time></a>
<a href="/@candidate">@candidate</a><p>{first_body}</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    first_read_seen_before_second: list[str] = []
    original_read_card = ThreadsHomeReader._read_card

    async def record_before_second_row(reader, root, **kwargs):
        if await root.get_attribute("id") == "second" and not first_read_seen_before_second:
            first_read_seen_before_second.append(capsys.readouterr().out)
        return await original_read_card(reader, root, **kwargs)

    monkeypatch.setattr(ThreadsHomeReader, "_read_card", record_before_second_row)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=detail_html,
            )

        await page.route(first_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=4,
            max_scrolls=0,
            terms=("hiring",),
            detail=True,
        )
        assert result.status == "COMPLETE"
        assert result.home_sampled == 2
        assert result.detail_reads == 1

    _run_offline_page(feed_html, exercise)
    output = "".join(first_read_seen_before_second) + capsys.readouterr().out
    assert first_read_seen_before_second
    assert f"| {first_body}" in first_read_seen_before_second[0]
    assert second_body not in first_read_seen_before_second[0]
    assert output.count(f"| {first_body}") == 2  # Home observation and Detail.
    assert output.count(f"| {second_body}") == 1
    assert "stabilize_ms=" in output
    assert "/post/" not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 2


def test_three_verified_rows_stream_around_rejected_candidate_and_checkpoint_selection(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    detail_url = "https://www.threads.com/@candidate/post/second"
    verified = (
        ("first", "Synthetic weather note one."),
        ("second", "Synthetic hiring a backend engineer."),
        ("third", "Synthetic weather note three."),
    )
    cards = [
        f'''<div class="card"><a href="/@candidate/post/{slug}"><time>1h</time></a>
<a href="/@candidate">candidate</a><span dir="auto">{body}</span></div>'''
        for slug, body in verified[:1]
    ]
    cards.append(
        '''<div class="card"><a href="/@candidate/post/rejected?share=1"><time>now</time></a>
<a href="/@candidate">candidate</a><span dir="auto">Synthetic rejected container body.</span></div>'''
    )
    cards.extend(
        f'''<div class="card"><a href="/@candidate/post/{slug}"><time>1h</time></a>
<a href="/@candidate">candidate</a><span dir="auto">{body}</span></div>'''
        for slug, body in verified[1:]
    )
    feed_html = f"""<!doctype html><html><head><style>
nav {{ height: 36px; }} .card {{ height: 155px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main>{''.join(cards)}</main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        await page.route(
            detail_url,
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=(
                    "<!doctype html><html><body><main><article>"
                    '<a href="/@candidate/post/second"><time>1h</time></a>'
                    '<a href="/@candidate">candidate</a>'
                    f"<p>{verified[1][1]}</p>"
                    "</article></main></body></html>"
                ),
            ),
        )
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=0,
            terms=("hiring",),
            detail=True,
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.home_sampled == 3
        assert result.provenance_verified == 3
        assert result.detail_reads == 1
        assert result.checkpoint_runs == 1
        assert not result.viewport_coverage_complete

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    for _slug, body in verified:
        assert f"| {body}" in output
    assert "| Synthetic rejected container body." in output
    assert "state=OBSERVED_UNVERIFIED" in output
    assert "checkpoint=PENDING" in output
    assert "/post/" not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 3
    assert len(checkpoint.seen_fingerprints) == 3
    assert checkpoint.detail_reads == 1
    assert checkpoint.pending_read_fingerprints == ()


def test_offscreen_nested_row_is_deferred_then_read_and_changed_back_anchor_stops(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/nested-long"
    body = "Synthetic hiring a backend engineer after a long visible card."
    feed_html = f"""<!doctype html><html><head><style>
nav {{ height: 40px; }}
#scroll-port {{ height: 300px; overflow-y: auto; margin-top: 8px; }}
article {{ box-sizing: border-box; height: 680px; padding-top: 430px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main id="scroll-port"><article>
<a href="/@candidate/post/nested-long"><time>now</time></a>
<a href="/@candidate">candidate</a><span dir="auto">{body}</span>
</article></main></body></html>"""
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate/post/nested-long"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    detail_requests = 0

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=detail_html,
            )

        await page.route(target_url, serve_detail)
        with pytest.raises(UiReadError) as caught:
            await _run_page_workflow_for_test(
                page,
                account,
                store,
                max_posts=2,
                max_scrolls=5,
                terms=("hiring",),
                detail=True,
            )
        assert caught.value.code == "RETURN_CONTEXT_CHANGED"

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "state=DEFERRED_TEXT_BELOW" in output
    assert "reason=BODY_TEXT_BELOW_VIEWPORT" in output
    assert f"| {body}" in output
    assert output.count(f"| {body}") == 2
    assert "[BACK] post=1 status=FAILED anchor=CHANGED" in output
    assert detail_requests == 1
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 0
    assert checkpoint.detail_reads == 0


def test_tall_body_requires_overlapping_wheels_before_one_read(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    body_parts = (
        "Synthetic tall body first segment",
        "Synthetic tall body second segment",
        "Synthetic tall body third segment",
        "Synthetic tall body fourth segment",
    )
    paragraphs = "".join(f"<p>{part}</p>" for part in body_parts)
    feed_html = f"""<!doctype html><html><head><style>
nav {{ height: 36px; }}
#scroll-port {{ height: 260px; overflow-y: auto; margin-top: 8px; }}
article {{ position: relative; height: 980px; }}
.body {{ position: absolute; top: 35px; left: 12px; right: 12px; }}
.body p {{ height: 54px; margin: 0 0 150px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main id="scroll-port"><article>
<a href="/@candidate/post/tall"><time>now</time></a>
<a href="/@candidate">@candidate</a><div class="body">{paragraphs}</div>
</article></main><script>
window.wheelCount = 0;
window.wheelDeltas = [];
window.addEventListener("wheel", event => {{
  window.wheelCount++;
  window.wheelDeltas.push(event.deltaY);
}});
</script></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=8,
            max_total_posts=20,
            terms=("hiring",),
            detail=True,
        )
        assert result.home_sampled == 1
        assert result.provenance_verified == result.body_extracted == 1
        assert result.detail_reads == 0
        assert result.scroll_attempts >= 2
        assert result.viewport_coverage_complete
        assert await page.evaluate("window.wheelCount") > result.scroll_attempts
        assert await page.evaluate("Math.max(...window.wheelDeltas)") <= MAX_WHEEL_STEP_PX

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "reason=VISIBLE_TEXT_PROGRESS" in output
    assert "[SCROLL_PROGRESS]" in output
    for part in body_parts:
        assert output.count(part) == 1
    assert "[DETAIL]" not in output
    assert "[BACK]" not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.home_sampled == 1
    assert checkpoint.detail_reads == 0


def test_pressable_fragment_progress_keeps_identity_through_native_wheels_and_detail_back(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    detail_url = "https://www.threads.com/@candidate/post/eligible"
    fragments = (
        "Synthetic pressable fragment alpha",
        "Synthetic pressable fragment bravo",
        "Synthetic pressable fragment charlie",
        "Synthetic pressable fragment delta",
    )
    eligible_body = "Hiring a backend engineer…"
    detail_body = eligible_body
    feed_html = f"""<!doctype html><html><head><style>
html, body {{ margin: 0; }}
nav {{ height: 36px; }}
.card {{ position: relative; box-sizing: border-box; height: 240px; }}
.tall {{ height: 840px; }}
.identity {{ position: absolute; top: 4px; z-index: 2; }}
.decorative {{ position: absolute; inset: 22px 0 auto; height: 800px; }}
.card-body {{ position: absolute; top: 50px; left: 12px; right: 12px; z-index: 1; }}
.tall .card-body {{ top: 70px; }}
.card-body p {{ position: absolute; left: 0; height: 34px; margin: 0; }}
.card-body p:nth-child(1) {{ top: 0; }}
.card-body p:nth-child(2) {{ top: 220px; }}
.card-body p:nth-child(3) {{ top: 440px; }}
.card-body p:nth-child(4) {{ top: 650px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main>
<div id="tall" class="card tall" data-pressable-container="true">
  <div class="identity"><a href="/@candidate/post/tall"><time>now</time></a>
  <a href="/@candidate">candidate</a></div>
  <div class="decorative" aria-hidden="true"></div>
  <div class="card-body">{''.join(f'<p>{part}</p>' for part in fragments)}</div>
</div>
<div class="card" data-pressable-container="true">
  <div class="identity"><a href="/@candidate/post/weather"><time>1h</time></a>
  <a href="/@candidate">candidate</a></div>
  <div class="card-body"><p>A synthetic weather update</p></div>
</div>
<div class="card" data-pressable-container="true">
  <div class="identity"><a href="/@candidate/post/eligible"><time>now</time></a>
  <a href="/@candidate">candidate</a></div>
  <div class="card-body"><p>{eligible_body}</p></div>
</div></main><script>
window.nativeWheelCount = Number(sessionStorage.getItem('synthetic-wheel-count') || 0);
window.headerOffscreenAfterFirstWheel =
  sessionStorage.getItem('synthetic-header-offscreen') === 'true';
window.headerAttachedAfterFirstWheel =
  sessionStorage.getItem('synthetic-header-attached') === 'true';
window.nativeWheelDeltas = [];
window.addEventListener('wheel', event => {{
  window.nativeWheelCount += 1;
  window.nativeWheelDeltas.push(event.deltaY);
  sessionStorage.setItem('synthetic-wheel-count', String(window.nativeWheelCount));
}});
window.addEventListener('scroll', () => {{
  if (window.nativeWheelCount !== 1) return;
  const header = document.querySelector('#tall time').getBoundingClientRect();
  window.headerOffscreenAfterFirstWheel = header.bottom <= 0;
  window.headerAttachedAfterFirstWheel = document.querySelector('#tall time').isConnected;
  sessionStorage.setItem(
    'synthetic-header-offscreen', String(window.headerOffscreenAfterFirstWheel)
  );
  sessionStorage.setItem(
    'synthetic-header-attached', String(window.headerAttachedAfterFirstWheel)
  );
}}, {{ once: true }});
</script></body></html>"""
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate/post/eligible"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{detail_body}</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    detail_visits = 0

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_visits
            detail_visits += 1
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=detail_html,
            )

        await page.route(detail_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=3,
            max_scrolls=8,
            max_total_posts=10,
            terms=("hiring", "engineer"),
            detail=True,
        )
        assert result.status == "COMPLETE"
        assert result.home_sampled == result.unique_verified_posts == 3
        assert result.viewport_observations > result.unique_verified_posts
        assert result.detail_reads == detail_visits == 1
        assert result.scroll_attempts == result.scrolls
        assert result.visible_text_segments >= len(fragments)
        assert await page.evaluate("window.nativeWheelCount") > result.scroll_attempts
        assert not await page.evaluate("window.headerOffscreenAfterFirstWheel")
        assert await page.evaluate("window.headerAttachedAfterFirstWheel")

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    for fragment in fragments:
        assert output.count(fragment) == 1
    assert output.count("A synthetic weather update") == 1
    assert output.count("Hiring a backend engineer") == 2
    assert "[SCROLL_PROGRESS]" in output
    assert "[SELECT]" in output
    assert "[DETAIL] post=3 status=VERIFIED" in output
    assert "[BACK] post=3 status=RESTORED anchor=IDENTITY_VERIFIED" in output
    assert "[CHECKPOINT] status=SAVED completed_runs=1" in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 1
    persisted = (
        data_root
        / "nurture"
        / "checkpoints"
        / f"{account.account_id}-{policy_fingerprint(canonical_terms(('hiring', 'engineer')))}.json"
    ).read_text(encoding="utf-8")
    assert all(
        value not in persisted
        for value in (*fragments, eligible_body, detail_body, detail_url, actor)
    )


def test_moved_wheels_without_new_content_stop_with_a_bounded_gap(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    feed_html = f"""<!doctype html><html><head><style>
html, body {{ height: 100%; margin: 0; overflow: hidden; }}
nav {{ height: 36px; }}
#scroll-port {{ height: 300px; overflow-y: auto; margin-top: 8px; }}
.card {{ position: relative; height: 700px; }}
.decorative {{ height: 650px; }}
.body {{ position: absolute; top: 60px; left: 12px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main id="scroll-port"><div class="card" data-pressable-container="true">
<a href="/@candidate/post/no-new-content"><time>now</time></a>
<a href="/@candidate">candidate</a>
<div class="decorative" aria-hidden="true"></div>
<p class="body">Synthetic already-read weather update</p>
</div></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=8,
            max_total_posts=10,
            terms=("hiring",),
            detail=True,
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.scroll_attempts == result.scrolls == 3
        assert not result.viewport_coverage_complete
        assert result.unresolved_gaps == 1
        assert result.checkpoint_runs == 1

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "state=MOVED_NO_NEW_CONTENT" in output
    assert "status=STOPPED reason=READ_PROGRESS_STALLED" in output
    assert "[DETAIL]" not in output
    assert "[CHECKPOINT] status=SAVED completed_runs=1" in output
    assert "[CYCLE] status=COMPLETE_WITH_SKIPS" in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 1


def test_pending_pressable_waits_for_missing_permalink_and_rescans() -> None:
    actor = "offline.operator"
    post_rows = [
        f"""<div class="card" data-pressable-container="true">
  <a href="/@candidate/post/verified-{index}"><time>{index}h</time></a>
  <a href="/@candidate">@candidate</a><span dir="auto">Verified fixture body {index}</span>
</div>"""
        for index in range(1, 4)
    ]
    pending_body = "Hydrating synthetic fixture body."
    feed_html = f"""<!doctype html><html><head><style>
nav {{ height: 32px; }} .card {{ height: 150px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
    {post_rows[0]}<div class="card" data-pressable-container="true" id="hydrating">
<time>now</time><a href="/@candidate">@candidate</a>
<span dir="auto">{pending_body}</span></div>{''.join(post_rows[1:])}</body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        waits: list[tuple[str, int, int | None]] = []
        original_wait = reader._wait_for_pending_card
        hydration_scheduled = False

        async def record_wait(pending, deadline, *, max_wait_ms=None):
            nonlocal hydration_scheduled
            for signal in pending.signals:
                waits.append(
                    (signal.condition, await signal.locator.count(), max_wait_ms)
                )
            if not hydration_scheduled:
                hydration_scheduled = True
                await page.evaluate(
                    """() => setTimeout(() => {
                        const card = document.querySelector('#hydrating');
                        const time = card.querySelector('time');
                        const permalink = document.createElement('a');
                        permalink.href = '/@candidate/post/hydrated-fourth';
                        time.replaceWith(permalink);
                        permalink.append(time);
                    }, 80)"""
                )
            return await original_wait(
                pending,
                deadline,
                max_wait_ms=max_wait_ms,
            )

        reader._wait_for_pending_card = record_wait
        targets = await reader.scan_viewport(5)
        assert len(targets) == 4, (
            f"targets={len(targets)} verified={reader.last_scan_counts.verified} "
            f"coverage={reader.last_scan_counts.coverage_complete} waits={waits!r}"
        )
        assert reader.last_scan_counts.verified == 4
        assert reader.last_scan_counts.coverage_complete
        assert any(
            condition == "visible"
            and wait_ms == reader_module._CARD_HYDRATION_WAIT_MS
            for condition, count, wait_ms in waits
        )
        assert any(target.url.endswith("/post/hydrated-fourth") for target in targets)

    _run_offline_page(feed_html, exercise)


def test_unresolved_hydrating_pressable_is_skipped_after_verified_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    verified_rows = "".join(
        f"""<div class="card" data-pressable-container="true">
  <a href="/@candidate/post/verified-{index}"><time>{index}h</time></a>
  <a href="/@candidate">@candidate</a>
  <span dir="auto">{f'Hiring a synthetic fixture engineer {index}.' if index == 1 else f'Weather fixture {index}.'}</span>
</div>"""
        for index in range(1, 4)
    )
    unverified_body = "Synthetic pending body must stay unprinted."
    feed_html = f"""<!doctype html><html><head><style>
nav {{ height: 32px; }} .card {{ height: 150px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
{verified_rows}<div class="card" data-pressable-container="true" id="hydrating">
<time>now</time><a href="/@candidate">@candidate</a>
<span dir="auto">{unverified_body}</span></div></body></html>"""
    monkeypatch.setattr(reader_module, "_CARD_HYDRATION_WAIT_MS", 30)
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        await page.route(
            "**/@candidate/post/verified-1",
            lambda route: route.fulfill(
                status=200,
                content_type="text/html",
                body=(
                    "<!doctype html><html><body><main><article>"
                    '<a href="/@candidate/post/verified-1"><time>1h</time></a>'
                    '<a href="/@candidate">candidate</a>'
                    "<p>Hiring a synthetic fixture engineer 1.</p>"
                    "</article></main></body></html>"
                ),
            ),
        )
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=1,
            max_total_posts=10,
            terms=("hiring", "engineer"),
            detail=True,
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.home_sampled == result.provenance_verified == 3
        assert result.detail_reads == result.checkpoint_runs == 1
        assert result.scrolls == 0
        assert not result.viewport_coverage_complete

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "state=PENDING_HYDRATION reason=POST_PERMALINK_PENDING" in output
    assert "verified_body_extracted=3" in output
    assert "scan=PARTIAL wheel=ALLOWED" in output
    assert "[CHECKPOINT] status=SAVED completed_runs=1" in output
    assert "[CYCLE] status=COMPLETE_WITH_SKIPS" in output
    assert f"| {unverified_body}" in output
    assert "status=OBSERVED_UNVERIFIED" in output
    assert "@candidate" not in output
    assert "/post/" not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 3
    assert checkpoint.detail_reads == 1


def test_observed_unverified_row_does_not_block_a_measured_native_wheel(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    unverified_body = "Hiring a synthetic engineer in Vietnam."
    feed_html = f"""<!doctype html><html><head><style>
html, body {{ height: 100%; margin: 0; overflow: hidden; }}
nav {{ height: 36px; }}
#scroll-port {{ height: 300px; overflow-y: auto; margin-top: 8px; }}
.card {{ position: relative; height: 1000px; }}
.body {{ position: absolute; top: 70px; left: 12px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main id="scroll-port"><div class="card" data-pressable-container="true">
<a href="https://invalid.synthetic/@candidate/post/unverified"><time>now</time></a>
<a href="/@candidate">candidate</a><p class="body">{unverified_body}</p>
</div></main><script>
window.nativeWheelCount = 0;
window.addEventListener("wheel", () => window.nativeWheelCount++);
</script></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=1,
            max_total_posts=5,
            terms=("hiring", "engineer"),
            detail=True,
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.scroll_attempts == result.scrolls == 1
        assert result.unverified_relevant == 1
        assert result.detail_reads == 0
        assert result.checkpoint_runs == 1
        assert not result.viewport_coverage_complete
        assert await page.evaluate("window.nativeWheelCount") >= 2

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "state=OBSERVED_UNVERIFIED" in output
    assert "status=ADVANCED observed=1 delta_px=" in output
    assert "[DETAIL]" not in output
    assert "[BACK]" not in output
    assert "SEARCH_INCOMPLETE reason=UNVERIFIED_RELEVANCE" not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 0


def test_ephemeral_gap_upgrade_keeps_below_fold_row_as_lookahead(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    eligible_body = "Hiring a synthetic engineer in Vietnam."
    unrelated_body = "Synthetic weather row remains unresolved."
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ height: 100%; margin: 0; overflow: hidden; }}
nav {{ height: 36px; }}
#scroll-port {{ height: 260px; overflow-y: auto; margin-top: 8px; }}
.card {{ position: relative; height: 260px; }}
.body {{ position: absolute; top: 180px; left: 12px; line-height: 24px; }}
#filler {{ height: 720px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main id="scroll-port">
<div id="upgrade" class="card" data-pressable-container="true">
<time>now</time><a href="/@candidate">candidate</a>
<p class="body">{eligible_body}</p></div>
<div id="unrelated" class="card" data-pressable-container="true">
<a href="/@candidate/post/unrelated/extra"><time>1h</time></a>
<a href="/@candidate">candidate</a><p class="body">{unrelated_body}</p></div>
<div id="filler"></div></main><script>
window.nativeWheelCount = 0;
window.addEventListener("wheel", () => {{
  window.nativeWheelCount += 1;
  if (window.nativeWheelCount !== 1) return;
  const timestamp = document.querySelector("#upgrade time");
  const permalink = document.createElement("a");
  permalink.href = "/@candidate/post/upgraded-row";
  timestamp.replaceWith(permalink);
  permalink.append(timestamp);
}});
</script></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=1,
            max_total_posts=5,
            terms=("hiring", "engineer"),
            detail=True,
        )
        assert result.status == "RELEVANT_POST_NOT_CURRENT"
        assert result.scroll_attempts == result.scrolls == 1
        assert result.unresolved_gaps == 0
        assert result.detail_reads == 0
        assert result.checkpoint_runs == 0
        assert result.checkpoint_saved is False
        # The second row is below-fold lookahead, not an encountered read gap.
        assert result.viewport_coverage_complete
        assert await page.evaluate("window.nativeWheelCount") >= 2

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    records = re.findall(
        r"\[ROW row_id=([0-9a-f]+).*?state=([A-Z_]+)",
        output,
    )
    upgraded_id = next(
        row_id for row_id, state in records if state == "PENDING_HYDRATION"
    )
    assert (upgraded_id, "VERIFIED") in records
    assert any(
        row_id != upgraded_id and state == "DEFERRED_TEXT_BELOW"
        for row_id, state in records
    )
    assert "SEARCH_INCOMPLETE reason=RELEVANT_POST_NOT_CURRENT" in output
    assert "COVERAGE_STOP reason=SCROLL_BUDGET_EXHAUSTED_WITH_GAP" not in output
    assert "[DETAIL]" not in output
    assert "[BACK]" not in output
    assert "[CHECKPOINT] status=NOT_SAVED reason=RELEVANT_POST_NOT_CURRENT" in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 0
    assert checkpoint.detail_reads == 0


@pytest.mark.parametrize("max_scrolls", (3, 8))
def test_continuous_below_viewport_lookahead_does_not_block_fallback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    max_scrolls: int,
) -> None:
    actor = "offline.operator"
    stale_url = "https://www.threads.com/@candidate/post/absent-boundary-pending"
    terms = ("hiring",)
    # At a 720px viewport, rows are 288px tall and the new last row's body
    # starts at the lower viewport boundary. Each 288px wheel brings the old
    # lookahead body fully into view while another row becomes lookahead.
    cards = "".join(
        f'''<article class="card"><a href="/@candidate/post/lookahead-{index}">
<time>now</time></a><a href="/@candidate">candidate</a>
<p class="body">Synthetic weather update {index}.</p></article>'''
        for index in range(20)
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
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    policy, stale_fingerprint, _ = _seed_pending_checkpoint(
        store,
        account,
        terms,
        stale_url,
    )
    actionability_checks: list[str] = []

    async def reject_target(self, target):
        actionability_checks.append(target.fingerprint)
        raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")

    monkeypatch.setattr(ThreadsHomeReader, "actionable_now", reject_target)
    run_results = []

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=20,
            max_scrolls=max_scrolls,
            max_total_posts=40,
            terms=terms,
            detail=True,
        )
        assert result.status == "NO_ACTIONABLE_TARGET"
        assert result.scroll_attempts == result.scrolls == max_scrolls
        assert result.unresolved_gaps == 0
        assert result.gap_sources_total == 0
        assert result.gap_category == "NONE"
        assert result.viewport_coverage_complete
        assert result.detail_reads == 0
        assert page.url == HOME_URL
        run_results.append(result)

    _run_offline_page(feed_html, exercise, headless=False)
    output = capsys.readouterr().out
    scroll_deltas = [
        float(value)
        for value in re.findall(
            r"\[SCROLL\] attempt=\d+ status=ADVANCED observed=1 delta_px=([0-9.]+)",
            output,
        )
    ]
    assert len(scroll_deltas) == max_scrolls
    assert all(value == pytest.approx(288, abs=1) for value in scroll_deltas)
    assert actionability_checks
    viewport_lookahead = [
        int(value)
        for value in re.findall(r"\[VIEWPORT \d+\].*?lookahead_rows=(\d+)", output)
    ]
    assert len(viewport_lookahead) == max_scrolls + 1
    assert run_results[0].lookahead_rows == sum(viewport_lookahead)
    assert "state=DEFERRED_TEXT_BELOW" in output
    assert "unresolved_gaps=0" in output
    assert "[DETAIL]" not in output
    assert "[BACK]" not in output
    assert "[CHECKPOINT] status=NOT_SAVED reason=NO_ACTIONABLE_TARGET" in output
    assert all(value not in output for value in (stale_url, actor, str(profile)))
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.schema_version == 3
    assert checkpoint.completed_runs == 1
    assert checkpoint.pending_read_fingerprints == (stale_fingerprint,)


@pytest.mark.parametrize("max_scrolls", (3, 8))
def test_encountered_incomplete_body_keeps_fallback_blocked_after_scrolls(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    max_scrolls: int,
) -> None:
    actor = "offline.operator"
    stale_url = "https://www.threads.com/@candidate/post/absent-incomplete-pending"
    terms = ("hiring",)
    long_body = " ".join(["Synthetic weather remains unresolved."] * 120)
    cards = "".join(
        f'''<article class="card"><a href="/@candidate/post/verified-{index}">
<time>now</time></a><a href="/@candidate">candidate</a>
<p class="body">Synthetic weather update {index}.</p></article>'''
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
        stale_url,
    )
    actionability_checks: list[str] = []

    async def reject_target(self, target):
        actionability_checks.append(target.fingerprint)
        raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")

    monkeypatch.setattr(ThreadsHomeReader, "actionable_now", reject_target)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=20,
            max_scrolls=max_scrolls,
            max_total_posts=40,
            terms=terms,
            detail=True,
        )
        assert result.status == "RELEVANT_POST_NOT_CURRENT"
        assert result.scroll_attempts == result.scrolls == max_scrolls
        assert result.unresolved_gaps == 1
        assert result.read_in_progress == 1
        assert result.unresolved_observations == 0
        assert result.unrepresented_gaps == 0
        assert result.gap_sources_total == result.unresolved_gaps
        assert result.gap_category == "READ_IN_PROGRESS"
        assert not result.viewport_coverage_complete
        assert result.detail_reads == 0
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise, headless=False)
    output = capsys.readouterr().out
    scroll_deltas = [
        float(value)
        for value in re.findall(
            r"\[SCROLL\] attempt=\d+ status=ADVANCED observed=1 delta_px=([0-9.]+)",
            output,
        )
    ]
    assert len(scroll_deltas) == max_scrolls
    assert all(value == pytest.approx(288, abs=1) for value in scroll_deltas)
    assert not actionability_checks
    assert "state=PARTIAL_BODY" in output
    assert "reason=VISIBLE_TEXT_PROGRESS" in output
    assert "SEARCH_INCOMPLETE reason=RELEVANT_POST_NOT_CURRENT" in output
    assert "[DETAIL]" not in output
    assert "[BACK]" not in output
    assert "[CHECKPOINT] status=NOT_SAVED reason=RELEVANT_POST_NOT_CURRENT" in output
    assert all(value not in output for value in (stale_url, actor, long_body, str(profile)))
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.schema_version == 3
    assert checkpoint.completed_runs == 1
    assert checkpoint.pending_read_fingerprints == (stale_fingerprint,)


@pytest.mark.parametrize("max_scrolls", (3, 8))
@pytest.mark.parametrize("body_visible_at_budget_edge", (True, False))
def test_last_scroll_boundary_distinguishes_partial_body_from_below_viewport_lookahead(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    max_scrolls: int,
    body_visible_at_budget_edge: bool,
) -> None:
    actor = "offline.operator"
    terms = ("hiring",)
    boundary_index = max_scrolls + 2
    boundary_body_top = 90 if body_visible_at_budget_edge else 110
    boundary_body = "Synthetic weather details remain beyond the viewport. " * 3
    card_markup = []
    for index in range(boundary_index + 4):
        boundary_id = ' id="boundary-body"' if index == boundary_index else ""
        body = (
            boundary_body
            if index == boundary_index
            else f"Synthetic weather update {index}."
        )
        card_markup.append(
            f'''<article class="card"><a href="/@candidate/post/boundary-{index}">
<time>now</time></a><a href="/@candidate">candidate</a>
<p class="body"{boundary_id}>{body}</p></article>'''
        )
    cards = "".join(card_markup)
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; }}
nav {{ height: 36px; }}
main {{ display: block; }}
.card {{ position: relative; height: 288px; }}
.body {{ position: absolute; top: 108px; left: 12px; width: 320px;
  margin: 0; line-height: 24px; }}
#boundary-body {{ top: {boundary_body_top}px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main>{cards}</main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    stale_url = "https://www.threads.com/@candidate/post/absent-budget-edge"
    _, _, _ = _seed_pending_checkpoint(store, account, terms, stale_url)
    actionability_checks: list[str] = []

    async def reject_target(self, target):
        actionability_checks.append(target.fingerprint)
        raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")

    monkeypatch.setattr(ThreadsHomeReader, "actionable_now", reject_target)
    observed_results = []

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=20,
            max_scrolls=max_scrolls,
            max_total_posts=40,
            terms=terms,
            detail=True,
        )
        geometry = await page.locator("#boundary-body").evaluate(
            """element => {
              const rect = element.getBoundingClientRect();
              return {
                top: rect.top,
                bottom: rect.bottom,
                viewport_height: window.innerHeight,
              };
            }"""
        )
        assert result.scroll_attempts == result.scrolls == max_scrolls
        assert result.detail_reads == 0
        assert result.checkpoint_saved is False
        assert page.url == HOME_URL
        observed_results.append((result, geometry))

    _run_offline_page(feed_html, exercise, headless=False)
    output = capsys.readouterr().out
    result, geometry = observed_results[0]
    scroll_deltas = [
        float(value)
        for value in re.findall(
            r"\[SCROLL\] attempt=\d+ status=ADVANCED observed=1 delta_px=([0-9.]+)",
            output,
        )
    ]
    assert len(scroll_deltas) == max_scrolls
    assert all(value == pytest.approx(288, abs=1) for value in scroll_deltas)

    if body_visible_at_budget_edge:
        assert geometry["viewport_height"] - 24 <= geometry["top"] < geometry["viewport_height"]
        assert geometry["bottom"] > geometry["viewport_height"]
        assert result.status == "RELEVANT_POST_NOT_CURRENT"
        assert result.unresolved_gaps == result.gap_sources_total == 1
        assert result.read_in_progress == 1
        assert result.unresolved_observations == 0
        assert result.unrepresented_gaps == 0
        assert result.gap_category == "READ_IN_PROGRESS"
        assert not result.viewport_coverage_complete
        assert not actionability_checks
    else:
        assert geometry["top"] >= geometry["viewport_height"]
        assert result.status == "NO_ACTIONABLE_TARGET"
        assert result.unresolved_gaps == result.gap_sources_total == 0
        assert result.read_in_progress == 0
        assert result.gap_category == "NONE"
        assert result.viewport_coverage_complete
        assert actionability_checks

    assert "[DETAIL]" not in output
    assert "[BACK]" not in output
    assert all(value not in output for value in (stale_url, actor, str(profile), boundary_body))


def test_carried_candidate_status_is_kept_per_row() -> None:
    actor = "offline.operator"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<div id="carried"><time>2h</time><span dir="auto">Observed synthetic body.</span></div>
<div id="verified"><a href="/@candidate/post/verified"><time>1h</time></a>
<a href="/@candidate">@candidate</a><span dir="auto">Synthetic weather.</span></div>
</body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()

        carried_root = page.locator("#carried")
        carried_handle = await carried_root.element_handle()
        assert carried_handle is not None
        carried_identity = reader_module._TrackedRowIdentity(
            carried_handle,
            "synthetic-carried-observation",
        )
        await reader._remember_viewport_candidates(
            (carried_root,),
            (carried_identity,),
        )
        targets = await reader.scan_viewport(5)
        assert len(targets) == 1
        assert not reader.last_viewport_coverage_complete
        assert reader.last_candidate_diagnostics[0].state == "OBSERVED_UNVERIFIED"
        assert reader.last_candidate_diagnostics[1].state == "VERIFIED"
        assert any(
            observation.body == "Observed synthetic body."
            for observation in reader.last_display_observations
        )

    _run_offline_page(feed_html, exercise)


def test_relevant_visible_post_is_selected_when_initial_scroll_is_at_feed_end(
    tmp_path: Path,
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/at-end"
    body = "Hiring a synthetic fixture engineer at the feed end."
    feed_html = f"""<!doctype html><html><head><style>
main {{ height: 320px; overflow-y: auto; }}
.row {{ box-sizing: border-box; height: 500px; padding: 420px 10px 10px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main id="feed"><div class="row">
<a href="/@candidate/post/at-end"><time>now</time></a>
<a href="/@candidate">@candidate</a><span dir="auto">{body}</span>
<button aria-label="More">More</button>
</div></main><script>
const feed = document.querySelector('#feed');
feed.scrollTop = feed.scrollHeight;
</script></body></html>"""
    detail_body = f"{body} with a verified synthetic detail expansion."
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate/post/at-end"><time>now</time></a>
<a href="/@candidate">@candidate</a><span dir="auto">{detail_body}</span>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=detail_html,
            )

        await page.route(target_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=1,
            terms=("hiring", "engineer"),
        )
        assert result.status == "COMPLETE"
        assert result.home_relevant == 1
        assert result.detail_reads == 1
        assert result.scroll_attempts == 0

    _run_offline_page(feed_html, exercise)
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 1


def test_unverified_display_row_does_not_block_verified_selected_detail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    detail_url = "https://www.threads.com/@verified/post/selected"
    unverified_body = "Tôi đang tìm việc kỹ sư"
    quoted_body = "Hiring quoted context must not affect the parent row"
    verified_body = "A weather update"
    selected_body = "Hiring an engineer"
    feed_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
.row {{ box-sizing: border-box; min-height: 190px; padding: 12px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
<div class="row" id="display-only">
  <div><a href="/@seeker">@seeker</a><time>3h</time></div>
  <span>{unverified_body}</span>
  <blockquote><a href="/@quoted/post/quote"><time>4h</time></a>
    <a href="/@quoted">@quoted</a><span>{quoted_body}</span>
  </blockquote>
</div>
<div class="row" id="verified-quoted">
  <div><a href="/@verified/post/weather"><time>2h</time></a>
    <a href="/@verified">@verified</a></div>
  <span>{verified_body}</span>
  <blockquote><a href="/@quoted/post/quote-two"><time>5h</time></a>
    <a href="/@quoted">@quoted</a><span>{quoted_body}</span>
  </blockquote>
</div>
<div class="row" id="selected">
  <div><a href="/@verified/post/selected"><time>1h</time></a>
    <a href="/@verified">@verified</a></div>
  <span>{selected_body}</span>
</div>
</main></body></html>"""
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@verified/post/selected"><time>1h</time></a>
<a href="/@verified">@verified</a><span>{selected_body}</span>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    detail_requests = 0

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=detail_html,
            )

        await page.route(detail_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=20,
            max_scrolls=0,
            max_total_posts=20,
            terms=("đang tìm việc", "hiring"),
            detail=True,
            show_post_text=True,
            show_observed_text=True,
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.observed_rows == 3
        assert result.body_extracted == 3
        assert result.provenance_verified == 2
        assert result.unverified_relevant == 1
        assert result.detail_reads == 1
        assert result.checkpoint_runs == 1
        assert not result.viewport_coverage_complete

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert detail_requests == 1
    assert "OBSERVED stage=FEED_INITIAL state=OBSERVATION_UNCERTAIN verified=2" in output
    assert "VIEWPORT_COVERAGE_INCOMPLETE" in output
    assert "[DETAIL] post=2 status=VERIFIED" in output
    assert "[BACK] post=2 status=RESTORED anchor=IDENTITY_VERIFIED" in output
    assert output.count(f"| {unverified_body}") == 1
    assert output.count(f"| {verified_body}") == 1
    assert output.count(f"| {selected_body}") == 2
    assert quoted_body not in output
    assert "status=OBSERVED_UNVERIFIED decision=RELEVANT_UNVERIFIED" in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("đang tìm việc", "hiring"))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 2
    assert checkpoint.detail_reads == 1
    checkpoint_path = (
        data_root
        / "nurture"
        / "checkpoints"
        / f"{account.account_id}-{checkpoint.policy_fingerprint}.json"
    )
    assert checkpoint_path.exists()


def test_two_row_cap_completes_with_skips_without_wheel(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    rows = "".join(
        f"""<div class="row"><div><a href="/@author{index}">@author{index}</a>
          <time>{index}h</time></div><span>Visible row {index}</span></div>"""
        for index in range(4)
    )
    feed_html = f"""<!doctype html><html><head><style>
.row {{ box-sizing: border-box; min-height: 150px; padding: 10px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>{rows}</main>
<script>window.wheelCount = 0;
window.addEventListener("wheel", () => window.wheelCount++);</script>
</body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=2,
            max_total_posts=40,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.observed_rows == 4
        assert not result.viewport_coverage_complete
        assert result.detail_reads == result.scroll_attempts == 0
        assert result.checkpoint_runs == 1
        assert await page.evaluate("window.wheelCount") == 0

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "OBSERVED stage=FEED_INITIAL state=OBSERVATION_UNCERTAIN" in output
    assert "READY stage=FEED_INITIAL" not in output
    assert "VIEWPORT_COVERAGE_INCOMPLETE viewport=1 observed_rows=4 cap=2" in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 0


def test_unmarked_row_body_hydration_is_rescanned_without_early_ready(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    body = "A hydrated display observation"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><div class="row"><a href="/@candidate">@candidate</a>
  <time>now</time><span id="body"></span></div></main>
<script>setTimeout(() => document.querySelector("#body").textContent = "{body}", 120);</script>
</body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=0,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE_WITH_SKIPS", (
            f"status={result.status} unresolved_gaps={result.unresolved_gaps} "
            f"scroll_attempts={result.scroll_attempts} scrolls={result.scrolls}"
        )
        assert result.body_extracted == 1
        assert result.provenance_verified == 0
        assert result.detail_reads == 0
        assert result.checkpoint_runs == 1

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "OBSERVED stage=FEED_INITIAL state=OBSERVATION_UNCERTAIN verified=0" in output
    assert "READY stage=FEED_INITIAL" not in output
    assert body not in output
    policy = policy_fingerprint(canonical_terms(("hiring",)))
    assert store.load(account.account_id, policy).completed_runs == 1


def test_final_feed_gate_failure_cannot_print_ready(
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main></main></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        reader.last_scan_counts.verified = 1
        reader.last_scan_counts.body_read = 1
        reader.last_scan_counts.body_extracted = 1
        reader.last_scan_counts.observed_rows = 1

        async def observe():
            return reader_module._RenderObservation(
                ("stable-verified-feed",),
                SimpleNamespace(observed_rows=1),
                ready_state="BODY_EXTRACTED",
            )

        async def fail_final_validation() -> None:
            raise UiReadError("FEED_INITIAL_NOT_READY")

        with pytest.raises(UiReadError) as caught:
            await reader._await_render_settled(
                "FEED_INITIAL",
                reader._readiness_deadline(1_000),
                timeout_code="FEED_INITIAL_NOT_READY",
                observe=observe,
                validate=fail_final_validation,
            )
        assert caught.value.code == "FEED_INITIAL_NOT_READY"

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "READY stage=FEED_INITIAL" not in output


def test_unverified_keyword_mismatch_never_becomes_a_verified_match(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    body = "I am looking for work as an engineer"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><div><a href="/@seeker">@seeker</a><time>now</time>
  <span>{body}</span></div></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=0,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.unverified_relevant == 0
        assert result.home_sampled == 0
        assert result.detail_reads == 0
        assert result.checkpoint_runs == 1

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert body not in output
    assert "unverified_relevant=0" in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1


def test_unmarked_rows_use_one_overlapping_wheel_and_checkpoint_skips(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    bodies = (
        "Private synthetic weather observation one",
        "Private synthetic music observation two",
        "Private synthetic local observation three",
    )
    quoted = "Private quoted context must stay outside the parent row"
    rows = "".join(
        f"""<div class="row"><a href="/@candidate{index}">@candidate{index}</a>
<time>{index}h</time><span>{body}</span>
{f'<blockquote><span>{quoted}</span></blockquote>' if index == 1 else ""}
</div>"""
        for index, body in enumerate(bodies, start=1)
    )
    feed_html = f"""<!doctype html><html><head><style>
.row {{ box-sizing: border-box; height: 420px; padding: 24px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a id="profile" aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>{rows}</main>
<script>
window.wheelCount = 0;
window.rowClickCount = 0;
window.addEventListener("wheel", event => window.wheelCount++);
window.addEventListener("click", event => {{
  if (event.target.closest(".row")) window.rowClickCount++;
}});
</script></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=3,
            max_scrolls=1,
            max_total_posts=20,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.observed_rows == 5
        assert result.body_extracted == result.classified == 3
        assert result.provenance_verified == result.detail_reads == 0
        assert result.scroll_attempts == result.scrolls == 1
        assert result.checkpoint_runs == 1
        assert await page.evaluate("window.wheelCount") > result.scroll_attempts
        assert await page.evaluate("window.rowClickCount") == 0

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "SCROLL attempt=1 observed=1 delta_band=SMALL" in output
    assert "SEARCH_BUDGET_EXHAUSTED reason=MAX_SCROLLS viewports=2" in output
    assert all(value not in output for value in (*bodies, quoted))
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 0


def test_explicit_observed_text_preview_contains_only_scoped_unverified_body(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    body = "Synthetic display text about hiring"
    excluded = (
        "Synthetic quote must be excluded",
        "Synthetic composer must be excluded",
        "Synthetic action must be excluded",
    )
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav>
<form><div contenteditable="true" role="textbox">{excluded[1]}</div></form>
<main><div><a href="/@candidate">@candidate</a><time>now</time>
  <span>{body}</span><blockquote><span>{excluded[0]}</span></blockquote>
  <button>{excluded[2]}</button></div></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=0,
            terms=("hiring",),
            show_observed_text=True,
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.unverified_relevant == 1
        assert result.provenance_verified == result.detail_reads == 0
        assert result.checkpoint_runs == 1

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "status=OBSERVED_UNVERIFIED decision=RELEVANT_UNVERIFIED" in output
    assert "<<< UNVERIFIED OBSERVED TEXT BEGIN >>>" in output
    assert f"| {body}" in output
    assert all(value not in output for value in excluded)
    assert "POST TEXT BEGIN" not in output


def test_unverified_relevance_is_skipped_while_verified_rows_checkpoint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    verified_body = "A synthetic weather update"
    observed_body = "I am looking for work as an engineer"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
<div><a href="/@verified/post/weather"><time>2h</time></a>
  <a href="/@verified">@verified</a><span>{verified_body}</span></div>
<div><a href="/@seeker">@seeker</a><time>1h</time>
  <span>{observed_body}</span></div>
</main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=0,
            terms=("looking for work",),
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.home_sampled == result.home_irrelevant == 1
        assert result.unverified_relevant == 1
        assert result.detail_reads == 0
        assert result.checkpoint_runs == 1

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "SEARCH_INCOMPLETE reason=UNVERIFIED_RELEVANCE" not in output
    assert verified_body not in output
    assert observed_body not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("looking for work",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 1
    assert checkpoint.home_irrelevant == 1
    assert len(checkpoint.seen_fingerprints) == 1


def test_nested_scrollport_excludes_future_rows_and_uses_its_own_coordinates() -> None:
    actor = "offline.operator"
    feed_html = f"""<!doctype html><html><head><style>
html, body {{ height: 100%; margin: 0; overflow: hidden; }}
nav {{ height: 36px; }}
#outer {{ height: 420px; overflow-y: auto; }}
#feed {{ height: 120px; overflow-y: auto; }}
.card {{ box-sizing: border-box; height: 120px; padding: 8px; }}
#outer-filler {{ height: 600px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<section id="outer"><main id="feed">
<article id="first" class="card">
  <a href="/@candidate/post/nested-first"><time>now</time></a>
  <a href="/@candidate">candidate</a><p>A synthetic first row.</p>
</article>
<article id="below" class="card">
  <a href="/@candidate/post/nested-below"><time>1h</time></a>
  <a href="/@candidate">candidate</a><p>A synthetic second row.</p>
</article>
<article id="far-below" class="card">
  <a href="/@candidate/post/nested-far"><time>2h</time></a>
  <a href="/@candidate">candidate</a><p>A synthetic third row.</p>
</article>
</main><div id="outer-filler" aria-hidden="true"></div></section></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        initial = await reader.scan_viewport(5)
        assert len(initial) == 1
        assert initial[0].url.endswith("/post/nested-first")
        assert reader.last_scan_counts.observed_rows == 1
        assert reader.last_scan_counts.coverage_complete
        evidence = initial[0].read_evidence
        assert evidence is not None
        assert evidence.clip_top_px is not None
        assert evidence.clip_bottom_px is not None
        below_box = await page.locator("#below").bounding_box()
        assert below_box is not None
        assert below_box["y"] >= evidence.clip_bottom_px
        assert below_box["y"] < page.viewport_size["height"]

        assert await reader.advance_feed(scan_after_scroll=False)
        assert await reader.advance_feed(scan_after_scroll=False)
        first_box = await page.locator("#first").bounding_box()
        assert first_box is not None
        assert first_box["y"] + first_box["height"] <= evidence.clip_top_px
        cached = await reader._read_card(
            page.locator("#first"),
            currently_visible=False,
            cached_body_fingerprints={initial[0].fingerprint},
        )
        assert cached.read_evidence is not None
        assert cached.read_evidence.state == "NOT_SAMPLED_CACHE"
        assert cached.read_evidence.root_intersects is None
        assert cached.read_evidence.body_text_nodes_present is None
        assert not cached.currently_visible
        geometry = reader.last_scroll_geometry
        assert geometry is not None
        assert geometry["surface_tag"] == "MAIN"
        assert geometry["overflow_y"] == "auto"
        assert float(geometry["scroll_top_after"]) > float(
            geometry["scroll_top_before"]
        )
        after = await reader.scan_viewport(5)
        loaded = next(
            target for target in after if target.url.endswith("/post/nested-below")
        )
        assert loaded.body == "A synthetic second row."
        assert loaded.currently_visible
        assert reader.last_scan_counts.coverage_complete
        assert all(
            not diagnostic.observation_id
            or diagnostic.state in {"VERIFIED", "NON_POST"}
            for diagnostic in reader.last_candidate_diagnostics
        )

    _run_offline_page(feed_html, exercise)


def test_document_scroll_keeps_viewport_clip_and_reads_row_below_viewport() -> None:
    actor = "offline.operator"
    feed_html = f"""<!doctype html><html><head><style>
html {{ overflow-x: hidden; }}
body {{ margin: 0; }}
nav {{ height: 36px; }}
article {{ box-sizing: border-box; height: 720px; padding: 8px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<article id="first">
  <a href="/@candidate/post/document-first"><time>now</time></a>
  <a href="/@candidate">candidate</a><p>A synthetic first row.</p>
</article>
<article id="below">
  <a href="/@candidate/post/document-below"><time>1h</time></a>
  <a href="/@candidate">candidate</a><p>A synthetic row below the viewport.</p>
</article>
</body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        before = await reader.scan_viewport(4)
        assert len(before) == 1

        clip_expression = with_scrollport_geometry("""node => {
            /*SCROLLPORT_GEOMETRY*/
            const rect = node.getBoundingClientRect();
            const clip = clipFor(node);
            return {
                scrollY: window.scrollY,
                viewportHeight: window.innerHeight,
                rectTop: rect.top,
                rectBottom: rect.bottom,
                clipTop: clip.top,
                clipBottom: clip.bottom,
            };
        }""")
        before_clip = await page.locator("#below").evaluate(clip_expression)
        assert before_clip["scrollY"] == 0
        assert before_clip["rectTop"] >= before_clip["clipBottom"]

        assert await reader.advance_feed(scan_after_scroll=False)
        after_clip = await page.locator("#below").evaluate(clip_expression)
        assert after_clip["scrollY"] > 0
        assert after_clip["clipTop"] == before_clip["clipTop"]
        assert after_clip["clipBottom"] == before_clip["clipBottom"]
        assert after_clip["rectTop"] < after_clip["clipBottom"]
        assert after_clip["rectBottom"] > after_clip["clipTop"]

        after = await reader.scan_viewport(4)
        loaded = next(
            (target for target in after if target.url.endswith("/post/document-below")),
            None,
        )
        assert loaded is not None
        assert loaded.body is not None
        assert loaded.currently_visible

    _run_offline_page(feed_html, exercise)


def test_scroll_engine_sends_small_native_wheels_and_measures_the_gesture() -> None:
    feed_html = """<!doctype html><html><body>
<div style="height: 2400px"></div>
<script>
window.wheelDeltas = [];
window.addEventListener("wheel", event => window.wheelDeltas.push(event.deltaY));
</script></body></html>"""

    async def exercise(page) -> None:
        surface = await page.evaluate_handle("() => document.scrollingElement")
        try:
            result = await NativeWheelScrollEngine(page).advance(
                surface,
                (640, 360),
                288,
                720,
            )
        finally:
            await surface.dispose()

        assert result.requested_delta == 288
        assert len(result.wheel_deltas) == 6
        assert sum(result.wheel_deltas) == 288
        assert max(abs(delta) for delta in result.wheel_deltas) <= MAX_WHEEL_STEP_PX
        assert result.observed_delta > 1
        assert result.observed_delta <= 360
        assert result.duration_ms <= 1_800
        assert await page.evaluate("window.wheelDeltas") == list(result.wheel_deltas)
        assert await page.evaluate("window.scrollY") == pytest.approx(
            result.observed_delta
        )

    _run_offline_page(feed_html, exercise)


def test_sticky_row_with_scrolltop_change_is_not_reading_progress(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    body = "A synthetic weather note stays fixed while the feed moves."
    feed_html = f"""<!doctype html><html><head><style>
html, body {{ height: 100%; margin: 0; overflow: hidden; }}
nav {{ height: 36px; }}
#feed {{ height: 220px; overflow-y: auto; }}
#sticky {{ position: sticky; top: 0; height: 150px; padding: 8px; }}
#space {{ height: 1200px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main id="feed"><article id="sticky">
<a href="/@candidate/post/sticky"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p></article>
<div id="space" aria-hidden="true"></div></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=3,
            max_total_posts=10,
            terms=("hiring",),
            detail=True,
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.stop_reason == "READ_PROGRESS_STALLED"
        assert result.scroll_attempts == result.scrolls == 3
        assert not result.viewport_coverage_complete
        assert result.unresolved_gaps > 0

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "SCROLLTOP_MOVED_ROW_STATIC" in output
    assert "ROW_MOVED_TOWARD_VIEW" not in output
    assert "reason=READ_PROGRESS_STALLED coverage=INCOMPLETE" in output
    assert "scroll_height_delta=0.0" in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 1
    assert checkpoint.detail_reads == 0


def test_native_wheel_loads_new_visible_post_then_detail_back_and_checkpoint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    detail_url = "https://www.threads.com/@candidate/post/loaded"
    seed_body = "A synthetic weather seed post."
    loaded_body = "Hiring a synthetic backend engineer after the wheel."
    feed_html = f"""<!doctype html><html><head><style>
html, body {{ height: 100%; margin: 0; overflow: hidden; }}
nav {{ height: 36px; }}
#feed {{ height: 220px; overflow-y: auto; }}
.card {{ box-sizing: border-box; height: 132px; padding: 12px; }}
#space {{ height: 600px; }}
</style></head><body><nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main id="feed"><article class="card" id="seed">
<a href="/@candidate/post/seed"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{seed_body}</p></article>
<div id="space" aria-hidden="true"></div></main><script>
const feed = document.querySelector('#feed');
window.nativeWheelCount = Number(sessionStorage.getItem('synthetic-wheels') || 0);
window.loadedMore = sessionStorage.getItem('synthetic-loaded') === 'true';
window.feedHeightBefore = feed.scrollHeight;
const loadMore = () => {{
  if (window.loadedMore) return;
  const card = document.createElement('article');
  card.className = 'card';
  card.id = 'loaded';
  card.innerHTML = `<a href="/@candidate/post/loaded"><time>now</time></a>
    <a href="/@candidate">candidate</a><p>{loaded_body}</p>`;
  document.querySelector('#space').before(card);
  window.loadedMore = true;
  sessionStorage.setItem('synthetic-loaded', 'true');
  window.feedHeightAfter = feed.scrollHeight;
}};
if (window.loadedMore) {{
  window.loadedMore = false;
  loadMore();
  feed.scrollTop = Number(sessionStorage.getItem('synthetic-scrolltop') || 0);
}}
feed.addEventListener('wheel', () => {{
  window.nativeWheelCount += 1;
  sessionStorage.setItem('synthetic-wheels', String(window.nativeWheelCount));
  loadMore();
}});
feed.addEventListener('scroll', () => sessionStorage.setItem(
  'synthetic-scrolltop', String(feed.scrollTop)
), {{ passive: true }});
</script></body></html>"""
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate/post/loaded"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{loaded_body}</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    detail_requests = 0

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=detail_html,
            )

        await page.route(detail_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=3,
            max_scrolls=1,
            max_total_posts=10,
            terms=("hiring", "engineer"),
            detail=True,
        )
        assert result.status == "COMPLETE"
        assert result.home_relevant == 1
        assert result.home_sampled == result.unique_verified_posts == 2
        assert result.scroll_attempts == result.scrolls == 1
        assert result.detail_reads == 1
        assert await page.evaluate("window.nativeWheelCount") >= 2
        assert await page.evaluate("window.loadedMore")
        assert await page.evaluate("window.feedHeightAfter - window.feedHeightBefore") >= 120

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert detail_requests == 1
    assert output.count(seed_body) == 1
    assert output.count(loaded_body) == 2
    assert "scroll_height_delta=132.0" in output
    assert "[DETAIL] post=2 status=VERIFIED" in output
    assert "[BACK] post=2 status=RESTORED anchor=IDENTITY_VERIFIED" in output
    assert "[CHECKPOINT] status=SAVED completed_runs=1" in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 2
    assert checkpoint.detail_reads == 1
    persisted = (
        data_root
        / "nurture"
        / "checkpoints"
        / f"{account.account_id}-{checkpoint.policy_fingerprint}.json"
    ).read_text(encoding="utf-8")
    assert all(value not in persisted for value in (seed_body, loaded_body, detail_url, actor))
