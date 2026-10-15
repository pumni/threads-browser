from __future__ import annotations

import asyncio
import re
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from playwright.async_api import Route
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright as real_async_playwright

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
    BrowserAccount,
    BrowserDataError,
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


def _run_offline_page(html: str, exercise) -> None:
    async def run() -> None:
        async with real_async_playwright() as playwright:
            browser = await playwright.chromium.launch()
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


@pytest.fixture(autouse=True)
def _short_offline_render_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader_module, "_RENDER_MIN_DWELL_MS", 25)
    monkeypatch.setattr(reader_module, "_RENDER_SETTLE_INTERVAL_MS", 20)


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
<html><head><style>article {{ min-height: 900px; }}</style></head><body>
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
    old_detail_html = f"""<!doctype html><html><body>
<article>
  <a href="/@candidate/post/one"><time>1h</time></a>
  <a href="/@candidate">@candidate</a>
  <p>{old_relevant_body}</p>
</article>
</body></html>"""
    new_detail_html = f"""<!doctype html><html><body>
<article>
  <a href="/@candidate/post/two"><time>now</time></a>
  <a href="/@candidate">@candidate</a>
  <p>{new_relevant_body}</p>
</article>
</body></html>"""
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
    feed_version["value"] = 2
    second_code = main(command)
    second = capsys.readouterr()

    assert first_code == 0, first.out + first.err
    assert "NURTURE status=COMPLETE source=HOME_FOR_YOU" in first.out
    assert "ui=UNVERIFIED_LIVE_UI" in first.out
    assert "home_sampled=2 home_new=2" in first.out
    assert "home_relevant=1 home_irrelevant=1" in first.out
    assert "home_relevant_ratio=0.50" in first.out
    assert "detail_reads=1 scroll_attempts=1 scrolls=1 skipped_unverified=0" in first.out
    assert second_code == 0, second.out + second.err
    assert "NURTURE status=COMPLETE source=HOME_FOR_YOU" in second.out
    assert "home_sampled=3 home_new=1" in second.out
    assert "home_relevant=2 home_irrelevant=1" in second.out
    assert "home_relevant_ratio=0.67" in second.out
    assert "duplicates=4 detail_reads=1" in second.out
    assert "scroll_attempts=4 scrolls=4" in second.out
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
<body><main><article>
<a href="/@candidate/post/native-cycle"><time>now</time></a>
<a href="/@candidate">@candidate</a><p>{expanded_body}</p>
</article></main></body></html>"""
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
        result = await workflow.run_page_workflow(
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
    assert "[BACK] post=1 status=RESTORED anchor=MATCHED" in output
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
                    body=f"""<!doctype html><html><body><main><div id="detail">
<a href="/@candidate/post/recovery-detail"><time>1h</time></a>
<a href="/@candidate">@candidate</a><script>
setTimeout(() => document.querySelector("#detail").insertAdjacentHTML(
  "beforeend", '<p>{detail_body}</p>'
), 750);
</script></div></main></body></html>""",
                )

            await page.route(detail_url, serve_detail)
            monkeypatch.setattr(page, "goto", goto_then_timeout)
            result = await workflow.run_page_workflow(
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
                await workflow.run_page_workflow(
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
    detail_html = f"""<!doctype html><html><body><article>
<a href="/@candidate/post/late-feed"><time>now</time></a>
<a href="/@candidate">@candidate</a><p>{body}</p>
</article></body></html>"""
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
                await workflow.run_page_workflow(
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


def test_readiness_timeouts_and_unverified_feed_candidates_do_not_checkpoint(
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
        with pytest.raises(UiReadError) as caught:
            await workflow.run_page_workflow(
                page,
                account,
                store,
                max_posts=2,
                terms=("hiring", "engineer"),
            )
        assert caught.value.code == "DETAIL_BODY_UNVERIFIED"

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
        assert store.load(account.account_id, policy).completed_runs == 0
        assert store.load(account.account_id, policy).detail_reads == 0

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
        result = await workflow.run_page_workflow(
            page,
            account,
            store,
            max_posts=2,
            terms=("hiring",),
            readiness_prompt=provenance_prompt,
        )
        assert result.status == "VIEWPORT_COVERAGE_INCOMPLETE"
        assert result.observed_rows == 1
        assert result.detail_reads == result.scroll_attempts == 0
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
    assert checkpoint.completed_runs == 0
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
                await workflow.run_page_workflow(
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
                await workflow.run_page_workflow(
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
                await workflow.run_page_workflow(
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


def test_mixed_candidate_coverage_stops_before_detail_or_checkpoint(
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
            result = await workflow.run_page_workflow(
                page,
                account,
                store,
                max_posts=3,
                terms=("hiring", "engineer"),
            )
            assert result.status == "VIEWPORT_COVERAGE_INCOMPLETE"
            assert result.home_sampled == 1
            assert result.detail_reads == 0
            assert result.scroll_attempts == result.scrolls > 0
            assert not result.viewport_coverage_complete
            assert await page.evaluate("sessionStorage.getItem('wheel-target')") is not None

        _run_offline_page(html, exercise)
        policy = policy_fingerprint(canonical_terms(("hiring", "engineer")))
        checkpoint = store.load(account.account_id, policy)
        assert checkpoint.completed_runs == 0
        assert checkpoint.home_sampled == 0
        assert checkpoint.seen_fingerprints == ()
        assert checkpoint.detail_reads == 0
        assert clean_detail_requests == 0
        assert rejected_detail_requests == 0
        output = capsys.readouterr().out
        assert "SKIPPED stage=FEED_INITIAL reason=CANDIDATE_NONCANONICAL count=1" in output
        assert "VIEWPORT_COVERAGE_INCOMPLETE" in output
        assert "quoted candidate" not in output
        assert "malformed candidate" not in output


def test_detail_author_provenance_mismatch_stops_without_retry(
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
            with pytest.raises(UiReadError) as caught:
                await workflow.run_page_workflow(
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
            assert caught.value.code == "POST_AUTHOR_UNVERIFIED", state
            assert await page.evaluate("sessionStorage.getItem('wheelCount')") == "0"

        _run_offline_page(html, exercise)
        policy = policy_fingerprint(canonical_terms(("hiring",)))
        checkpoint = store.load(account.account_id, policy)
        assert checkpoint.completed_runs == 0
        assert checkpoint.detail_reads == 0
        assert detail_requests == 1
        assert readiness_prompts == []
        output = capsys.readouterr().out
        assert "STOP stage=DETAIL code=POST_AUTHOR_UNVERIFIED" in output
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


def test_detail_body_mismatch_fails_closed_without_checkpoint(tmp_path: Path) -> None:
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
                body=f"""<!doctype html><html><body><article>
<a href="/@candidate/post/body-mismatch"><time>now</time></a>
<a href="/@candidate">@candidate</a><p>{wrong_detail_body}</p>
</article></body></html>""",
            )

        await page.route(target_url, serve_wrong_detail)
        with pytest.raises(UiReadError) as caught:
            await workflow.run_page_workflow(
                page,
                account,
                store,
                max_posts=2,
                max_scrolls=0,
                terms=("hiring",),
            )
        assert caught.value.code == "DETAIL_BODY_UNVERIFIED"

    _run_offline_page(feed_html, exercise)
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 0
    assert checkpoint.detail_reads == 0


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

    def render_feed(posts: tuple[tuple[str, str], ...]) -> str:
        cards = "".join(
            f'''<article><a href="/@candidate/post/{slug}"><time>1h</time></a>
<a href="/@candidate">@candidate</a><p>{body}</p></article>'''
            for slug, body in posts
        )
        return f"""<!doctype html><html><head>
<style>article {{ height: 360px; }}</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav>{cards}</body></html>"""

    detail_pages = {
        pending_url: render_feed((("pending", relevant_body),)),
        first_url: render_feed((("first", relevant_body),)),
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
        elif url in detail_pages:
            html = detail_pages[url]
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
        result = await workflow.run_page_workflow(
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
        assert await page.evaluate("sessionStorage.getItem('wheel-count')") == "2"

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
article:first-of-type {{ height: 800px; padding-top: 350px; }}
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
    detail_html = f"""<!doctype html><html><body><article>
<a href="/@candidate/post/intermediate"><time>2h</time></a>
<a href="/@candidate">@candidate</a><p>{detail_body}</p>
</article></body></html>"""
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
        result = await workflow.run_page_workflow(
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


def test_unverified_candidate_allows_wheel_but_blocks_detail_and_checkpoint(
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
        result = await workflow.run_page_workflow(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=3,
            max_total_posts=10,
            terms=("hiring", "engineer"),
        )
        assert result.status == "VIEWPORT_COVERAGE_INCOMPLETE"
        assert result.home_sampled == result.home_new == 1
        assert result.home_relevant == 1
        assert result.skipped_unverified == 1
        assert result.scroll_attempts == result.scrolls
        assert result.scroll_attempts > 0
        assert result.detail_reads == result.checkpoint_runs == 0
        assert int(await page.evaluate("sessionStorage.getItem('wheel-count')")) == result.scrolls
        assert float(await page.evaluate("sessionStorage.getItem('wheel-delta')")) < 360

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert detail_requests == 0
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
    assert checkpoint.completed_runs == 0
    assert checkpoint.home_sampled == 0


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
</nav><main><article>
  <a href="/@candidate/post/trailing-safe/"><time>1h</time></a>
  <a href="{invalid_secondary}">Secondary</a>
  <a href="/@candidate">@candidate</a><span>{body}</span>
</article></main></body></html>"""
    detail_html = f"""<!doctype html><html><body><article>
  <a href="/@candidate/post/trailing-safe/"><time>1h</time></a>
  <a href="/@candidate">@candidate</a><p>{body}</p>
</article></body></html>"""
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
        result = await workflow.run_page_workflow(
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
    assert "[URL_STRUCTURE] timestamp_primary=1 secondary_post_links=1" in output
    assert "valid_canonical_count=1 rejected_count=1" in output
    assert invalid_secondary not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 1


def test_noncanonical_link_reasons_are_aggregate_and_privacy_safe(
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
        result = await workflow.run_page_workflow(
            page,
            account,
            store,
            max_posts=20,
            max_scrolls=0,
            max_total_posts=10,
            terms=("hiring",),
            show_post_text=True,
        )
        assert result.status == "VIEWPORT_COVERAGE_INCOMPLETE"
        assert result.home_sampled == 0
        assert result.skipped_unverified == 8, result
        assert result.unverified_relevant > 0
        assert result.detail_reads == result.checkpoint_runs == 0
        assert not result.viewport_coverage_complete

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "cause=POST_LINK_HAS_QUERY_OR_FRAGMENT" in output
    assert "cause=POST_ORIGIN_INVALID" in output
    assert "cause=POST_PATH_SHAPE_INVALID" in output
    assert "cause=POST_AUTHOR_OR_ID_INVALID" in output
    assert "cause=CANDIDATE_AUTHOR_MISMATCH" in output
    assert "cause=CANDIDATE_WRAPPER_AMBIGUOUS" in output
    assert "url_parsed=3 author_unverified=2 wrapper_ambiguous=2 card_not_ready=0" in output
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
    assert checkpoint.completed_runs == 0


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
                body=f"""<!doctype html><html><body><article>
<a href="/@candidate/post/{post_id}"><time>now</time></a>
<a href="/@candidate">@candidate</a><p>{detail_body}</p>
</article></body></html>""",
            )

        await page.route("**/@candidate/post/**", serve_verified_detail)
        result = await workflow.run_page_workflow(
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
        assert result.home_sampled == 3
        assert result.home_irrelevant == 3
        assert result.home_unknown == 0
        assert result.detail_reads == 1
        assert result.scroll_attempts == result.scrolls == 2
        assert result.checkpoint_runs == 1
        assert await page.evaluate("sessionStorage.getItem('wheel-count')") == "2"

    _run_offline_page(feed_html, exercise)
    policy = policy_fingerprint(canonical_terms(("hiring",)))
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 3
    assert checkpoint.home_irrelevant == 3
    assert checkpoint.detail_reads == 1
    assert checkpoint.scroll_attempts == checkpoint.scrolls == 2
    output = capsys.readouterr().out
    assert "decision=VERIFIED_FALLBACK reason=NO_RELEVANT_MATCH" in output
    assert "[POST post=" in output
    assert "decision=IRRELEVANT source=DETAIL_BODY" in output


def test_total_scan_budget_and_zero_verified_exhaustion_are_truthful(
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
        result = await workflow.run_page_workflow(
            page,
            valid_account,
            valid_store,
            max_posts=2,
            max_scrolls=8,
            max_total_posts=2,
            terms=("hiring",),
        )
        assert result.status == "VERIFIED_IRRELEVANT"
        assert result.home_sampled == 2
        assert result.detail_reads == 0
        assert result.scroll_attempts == result.scrolls == 0
        assert result.checkpoint_runs == 1
        assert await page.evaluate("sessionStorage.getItem('wheel-count')") is None

    _run_offline_page(valid_html, valid_exercise)
    valid_checkpoint = valid_store.load(
        valid_account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert valid_checkpoint.completed_runs == 1
    assert valid_checkpoint.home_sampled == 2
    assert valid_checkpoint.detail_reads == 0
    assert valid_checkpoint.scroll_attempts == 0
    valid_output = capsys.readouterr().out
    assert "READY stage=FEED_INITIAL state=BODY_EXTRACTED" in valid_output

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
            await workflow.run_page_workflow(
                page,
                zero_account,
                zero_store,
                max_posts=3,
                max_scrolls=2,
                max_total_posts=10,
                terms=("hiring",),
            )
        assert caught.value.code == "HOME_SCROLL_SURFACE_UNVERIFIED"
        assert await page.evaluate("sessionStorage.getItem('wheel-count')") is None

    _run_offline_page(zero_verified_html, zero_exercise)
    zero_policy = policy_fingerprint(canonical_terms(("hiring",)))
    zero_checkpoint = zero_store.load(zero_account.account_id, zero_policy)
    assert zero_checkpoint.completed_runs == 0
    assert zero_checkpoint.home_sampled == 0
    output = capsys.readouterr().out
    assert "OBSERVED stage=FEED_INITIAL state=OBSERVATION_UNCERTAIN" in output
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
    detail_html = f"""<!doctype html><html><head><meta charset="utf-8"></head><body>
<div data-pressable-container="true">
  <a href="/@candidate/post/job-seeking"><time>1h</time></a>
  <a href="/@candidate">@candidate</a>
  <span dir="auto">{verified_body_first}</span>
  <div dir="auto">{verified_body_second}</div>
</div>
</body></html>"""
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
        result = await workflow.run_page_workflow(
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


def test_visible_card_without_permalink_is_observed_without_checkpoint(
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
        result = await workflow.run_page_workflow(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=0,
            max_total_posts=10,
            terms=("hiring",),
            show_post_text=True,
        )
        assert result.status == "UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION"
        assert result.observed_rows == 1
        assert result.body_extracted == 1
        assert result.provenance_verified == 0
        assert result.unverified_relevant == 1
        assert result.detail_reads == result.scroll_attempts == 0
        assert await page.evaluate("sessionStorage.getItem('wheel-count')") is None

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert (
        "OBSERVED stage=FEED_INITIAL state=FEED_VISIBLE_UNVERIFIED verified=0"
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
    assert checkpoint.completed_runs == 0
    assert checkpoint.home_sampled == 0


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
    detail_html = f"""<!doctype html><html><body><article>
<a href="/@candidate/post/relevant"><time>2h</time></a>
<a href="/@candidate">@candidate</a><span dir="auto">{detail_body}</span>
</article></body></html>"""
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
        result = await workflow.run_page_workflow(
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
    detail_html = f"""<!doctype html><html><body><article>
<a href="/@candidate/post/progressive-first"><time>now</time></a>
<a href="/@candidate">@candidate</a><p>{first_body}</p>
</article></body></html>"""
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
        result = await workflow.run_page_workflow(
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


def test_three_verified_rows_stream_around_rejected_candidate_without_checkpoint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
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
        result = await workflow.run_page_workflow(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=0,
            terms=("hiring",),
            detail=True,
        )
        assert result.status == "VIEWPORT_COVERAGE_INCOMPLETE"
        assert result.home_sampled == 3
        assert result.provenance_verified == 3
        assert result.detail_reads == 0
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
    assert checkpoint.completed_runs == 0
    assert checkpoint.home_sampled == 0
    assert checkpoint.detail_reads == 0


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
    detail_html = f"""<!doctype html><html><body><article>
<a href="/@candidate/post/nested-long"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></body></html>"""
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
            await workflow.run_page_workflow(
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
window.addEventListener("wheel", () => window.wheelCount++);
</script></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await workflow.run_page_workflow(
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
        assert await page.evaluate("window.wheelCount") == result.scroll_attempts

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
window.addEventListener('wheel', () => {{
  window.nativeWheelCount += 1;
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
        result = await workflow.run_page_workflow(
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
        assert await page.evaluate("window.nativeWheelCount") == result.scroll_attempts
        assert await page.evaluate("window.headerOffscreenAfterFirstWheel")
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
    assert "[BACK] post=3 status=RESTORED anchor=MATCHED" in output
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
        result = await workflow.run_page_workflow(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=8,
            max_total_posts=10,
            terms=("hiring",),
            detail=True,
        )
        assert result.status == "VIEWPORT_COVERAGE_INCOMPLETE"
        assert result.scroll_attempts == result.scrolls == 3
        assert not result.viewport_coverage_complete
        assert result.unresolved_gaps == 1
        assert result.checkpoint_runs == 0

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "state=MOVED_NO_NEW_CONTENT" in output
    assert "status=STOPPED reason=READ_PROGRESS_STALLED" in output
    assert "[DETAIL]" not in output
    assert "[CHECKPOINT] status=NOT_SAVED reason=VIEWPORT_COVERAGE_INCOMPLETE" in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 0


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


def test_unresolved_hydrating_pressable_reports_partial_without_detail_or_checkpoint(
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
        result = await workflow.run_page_workflow(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=1,
            max_total_posts=10,
            terms=("hiring", "engineer"),
            detail=True,
        )
        assert result.status == "VIEWPORT_COVERAGE_INCOMPLETE"
        assert result.home_sampled == result.provenance_verified == 3
        assert result.detail_reads == result.scroll_attempts == 0
        assert not result.viewport_coverage_complete

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "state=PENDING_HYDRATION reason=POST_PERMALINK_PENDING" in output
    assert "verified_body_extracted=3" in output
    assert "scan=PARTIAL wheel=ALLOWED" in output
    assert "[CHECKPOINT] status=NOT_SAVED reason=VIEWPORT_COVERAGE_INCOMPLETE" in output
    assert f"| {unverified_body}" in output
    assert "status=OBSERVED_UNVERIFIED" in output
    assert "@candidate" not in output
    assert "/post/" not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 0
    assert checkpoint.home_sampled == checkpoint.detail_reads == 0


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
        result = await workflow.run_page_workflow(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=1,
            max_total_posts=5,
            terms=("hiring", "engineer"),
            detail=True,
        )
        assert result.status == "VIEWPORT_COVERAGE_INCOMPLETE"
        assert result.scroll_attempts == result.scrolls == 1
        assert result.unverified_relevant == 1
        assert result.detail_reads == 0
        assert not result.viewport_coverage_complete
        assert await page.evaluate("window.nativeWheelCount") == 1

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "state=OBSERVED_UNVERIFIED" in output
    assert "status=ADVANCED observed=1 delta_px=" in output
    assert "[DETAIL]" not in output
    assert "[BACK]" not in output
    assert "[CHECKPOINT] status=NOT_SAVED reason=VIEWPORT_COVERAGE_INCOMPLETE" in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 0
    assert checkpoint.detail_reads == 0


def test_ephemeral_gap_upgrades_only_its_attached_row_and_keeps_other_gap_open(
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
        result = await workflow.run_page_workflow(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=1,
            max_total_posts=5,
            terms=("hiring", "engineer"),
            detail=True,
        )
        assert result.status == "VIEWPORT_COVERAGE_INCOMPLETE"
        assert result.scroll_attempts == result.scrolls == 1
        assert result.unresolved_gaps == 1
        assert result.detail_reads == 0
        assert result.checkpoint_runs == 0
        assert not result.viewport_coverage_complete
        assert await page.evaluate("window.nativeWheelCount") == 1

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
    assert "reason=SCROLL_BUDGET_EXHAUSTED_WITH_GAP" in output
    assert "[DETAIL]" not in output
    assert "[BACK]" not in output
    assert "[CHECKPOINT] status=NOT_SAVED reason=VIEWPORT_COVERAGE_INCOMPLETE" in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 0
    assert checkpoint.detail_reads == 0


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

        async def carried_candidate(root) -> bool:
            return await root.get_attribute("id") == "carried"

        reader._is_carried_viewport_candidate = carried_candidate
        targets = await reader.scan_viewport(5)
        assert len(targets) == 1
        assert reader.last_viewport_coverage_complete
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
    detail_html = f"""<!doctype html><html><body><article>
<a href="/@candidate/post/at-end"><time>now</time></a>
<a href="/@candidate">@candidate</a><span dir="auto">{detail_body}</span>
</article></body></html>"""
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
        result = await workflow.run_page_workflow(
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


def test_unmarked_rows_keep_display_and_provenance_evidence_separate(
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
        result = await workflow.run_page_workflow(
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
        assert result.status == "COMPLETE"
        assert result.observed_rows == 3
        assert result.body_extracted == 3
        assert result.provenance_verified == 2
        assert result.unverified_relevant == 1
        assert result.detail_reads == 1
        assert result.viewport_coverage_complete

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert detail_requests == 1
    assert "OBSERVED stage=FEED_INITIAL" not in output
    assert "READY stage=FEED_INITIAL state=BODY_EXTRACTED verified=2" in output
    assert "DETAIL_VERIFIED status=PROVENANCE_VERIFIED" in output
    assert output.count(f"| {unverified_body}") == 1
    assert output.count(f"| {verified_body}") == 1
    assert output.count(f"| {selected_body}") == 2
    assert quoted_body not in output
    assert "status=OBSERVED_UNVERIFIED decision=RELEVANT_UNVERIFIED" in output
    assert "[DETAIL] post=2 status=VERIFIED provenance=VERIFIED extent=FULL" in output
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
    persisted = checkpoint_path.read_text(encoding="utf-8")
    assert all(
        value not in persisted
        for value in (
            unverified_body,
            verified_body,
            selected_body,
            quoted_body,
            "/@verified/post/selected",
        )
    )


def test_two_row_cap_reports_incomplete_viewport_without_wheel_or_checkpoint(
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
        result = await workflow.run_page_workflow(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=2,
            max_total_posts=40,
            terms=("hiring",),
        )
        assert result.status == "VIEWPORT_COVERAGE_INCOMPLETE"
        assert result.observed_rows == 4
        assert not result.viewport_coverage_complete
        assert result.detail_reads == result.scroll_attempts == 0
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
    assert checkpoint.completed_runs == checkpoint.home_sampled == 0


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
        result = await workflow.run_page_workflow(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=0,
            terms=("hiring",),
        )
        assert result.status == "OBSERVED_UNVERIFIED_ONLY"
        assert result.body_extracted == 1
        assert result.provenance_verified == 0
        assert result.detail_reads == 0
        assert result.checkpoint_runs == 0

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "OBSERVED stage=FEED_INITIAL state=FEED_VISIBLE_UNVERIFIED verified=0" in output
    assert "READY stage=FEED_INITIAL" not in output
    assert body not in output
    policy = policy_fingerprint(canonical_terms(("hiring",)))
    assert store.load(account.account_id, policy).completed_runs == 0


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
        result = await workflow.run_page_workflow(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=0,
            terms=("hiring",),
        )
        assert result.status == "OBSERVED_UNVERIFIED_ONLY"
        assert result.unverified_relevant == 0
        assert result.home_sampled == 0
        assert result.detail_reads == 0
        assert result.checkpoint_runs == 0

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert body not in output
    assert "unverified_relevant=0" in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 0


def test_unmarked_rows_use_one_overlapping_wheel_and_dedupe_without_checkpoint(
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
        result = await workflow.run_page_workflow(
            page,
            account,
            store,
            max_posts=3,
            max_scrolls=1,
            max_total_posts=20,
            terms=("hiring",),
        )
        assert result.status == "OBSERVED_UNVERIFIED_ONLY"
        assert result.observed_rows == 5
        assert result.body_extracted == result.classified == 3
        assert result.provenance_verified == result.detail_reads == 0
        assert result.scroll_attempts == result.scrolls == 1
        assert result.checkpoint_runs == 0
        assert await page.evaluate("window.wheelCount") == 1
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
    assert checkpoint.completed_runs == checkpoint.home_sampled == 0


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
        result = await workflow.run_page_workflow(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=0,
            terms=("hiring",),
            show_observed_text=True,
        )
        assert result.status == "UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION"
        assert result.unverified_relevant == 1
        assert result.provenance_verified == result.detail_reads == 0
        assert result.checkpoint_runs == 0

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "status=OBSERVED_UNVERIFIED decision=RELEVANT_UNVERIFIED" in output
    assert "<<< UNVERIFIED OBSERVED TEXT BEGIN >>>" in output
    assert f"| {body}" in output
    assert all(value not in output for value in excluded)
    assert "POST TEXT BEGIN" not in output


def test_unverified_relevance_prevents_completed_checkpoint_with_verified_rows(
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
        result = await workflow.run_page_workflow(
            page,
            account,
            store,
            max_posts=5,
            max_scrolls=0,
            terms=("looking for work",),
        )
        assert result.status == "UNVERIFIED_RELEVANCE_REQUIRES_VERIFICATION"
        assert result.home_sampled == result.home_irrelevant == 1
        assert result.unverified_relevant == 1
        assert result.detail_reads == result.checkpoint_runs == 0

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "SEARCH_INCOMPLETE reason=UNVERIFIED_RELEVANCE" in output
    assert verified_body not in output
    assert observed_body not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("looking for work",))),
    )
    assert checkpoint.completed_runs == 0
