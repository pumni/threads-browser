from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Route

from tests.fixtures.browser import (
    _is_offline_profile_url,
    _offline_playwright_manager,
    _run_offline_page,
    _run_page_workflow_for_test,
)
from threads_browser import reader as reader_module
from threads_browser import workflow
from threads_browser.cli import main
from threads_browser.reader import (
    HOME_URL,
    ThreadsHomeReader,
    UiReadError,
    canonical_terms,
    classify_body,
)
from threads_browser.storage import (
    AccountStore,
    CheckpointStore,
)
from threads_browser.workflow import policy_fingerprint


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
    assert second_summary.startswith("NURTURE status=COMPLETE source=HOME_FOR_YOU"), (
        f"{second_summary}\n{second_scroll_diagnostics}"
    )
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
        assert (
            await page.evaluate("sessionStorage.getItem('native-cycle-click-after-scroll')")
            == "true"
        )
        assert await page.evaluate("sessionStorage.getItem('native-cycle-wheel')") == "1"
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
        data_root / "nurture" / "checkpoints" / f"{account.account_id}-{policy}.json"
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
            "window.addEventListener('wheel', event => event.preventDefault(), {passive: false});"
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
