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
        "nurture",
        "run",
        "offline",
        "--headed",
        "--max-posts",
        "2",
        "--term",
        "hiring",
        "--term",
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
        class BoundaryReader(ThreadsHomeReader):
            injected = False

            async def _feed_readiness_signals(self):
                pending = await super()._feed_readiness_signals()
                if not self.injected and pending is None:
                    self.injected = True
                    await self._page.evaluate(
                        """() => {
                            document.querySelector('main').innerHTML = `
                              <div><a href="/@candidate/post/boundary-ready">Permalink</a>
                                <a href="/@candidate">@candidate</a>
                                <p>A weather update</p></div>`;
                        }"""
                    )
                return pending

        reader = BoundaryReader(page, actor)
        await reader.open_home()
        started = asyncio.get_running_loop().time()
        targets, _ = await reader._wait_for_home_targets(
            1,
            timeout_code="FEED_INITIAL_NOT_READY",
        )
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 3.5
        assert reader.injected
        assert len(targets) == 1
        assert targets[0].url == url

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


def test_delayed_wheel_and_detail_hydration_return_to_home(
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
        await reader.return_to_home()
        assert page.url == HOME_URL
        home_targets = await reader._visible_targets(1)
        assert home_targets.targets
        assert detail_requests == 1

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "READY stage=FEED_SCROLL state=BODY_EXTRACTED" in output
    assert "READY stage=DETAIL state=DETAIL_VERIFIED" in output
    assert "READY stage=HOME_RETURN state=UI_SHELL_SETTLED" in output


def test_empty_scan_rescans_new_feed_with_zero_scroll_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
            result = await workflow.run_page_workflow(
                page,
                account,
                store,
                max_posts=1,
                max_scrolls=0,
                terms=("hiring",),
            )
            assert result.status == "COMPLETE"
            assert result.scroll_attempts == 0
            assert result.scrolls == 0
            assert result.detail_reads == 1

        _run_offline_page(feed_html, exercise)
        checkpoint = store.load(account.account_id, policy)
        assert checkpoint.completed_runs == 1
        assert checkpoint.scroll_attempts == 0
        assert checkpoint.detail_reads == 1
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
        ) -> None:
            super().__init__(
                page,
                expected_actor,
                readiness_prompt=readiness_prompt,
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
document.querySelector("#profile").addEventListener("click", () => {{
  const visits = Number(sessionStorage.getItem("profile-visits") || "0") + 1;
  sessionStorage.setItem("profile-visits", String(visits));
  if (visits >= 2) deactivateForYou();
}});
window.addEventListener("pageshow", () => {{
  if (Number(sessionStorage.getItem("profile-visits") || "0") >= 2) {{
    deactivateForYou();
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
            assert result.home_sampled == 0
            assert result.detail_reads == 0
            assert result.scroll_attempts == result.scrolls == 0
            assert not result.viewport_coverage_complete
            assert await page.evaluate("sessionStorage.getItem('wheel-target')") is None

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


def test_noncanonical_first_viewport_can_advance_to_verified_relevant_post(
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
            max_scrolls=3,
            max_total_posts=10,
            terms=("hiring", "engineer"),
        )
        assert result.status == "COMPLETE"
        assert result.home_sampled == result.home_new == 1
        assert result.home_relevant == 1
        assert result.skipped_unverified == 1
        assert result.scroll_attempts == result.scrolls == 1
        assert result.detail_reads == 1
        assert await page.evaluate("sessionStorage.getItem('wheel-count')") == "1"
        assert float(await page.evaluate("sessionStorage.getItem('wheel-delta')")) < 360

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert (
        "SKIPPED stage=FEED_INITIAL reason=CANDIDATE_NONCANONICAL count=1 "
        "cause=POST_PATH_SHAPE_INVALID"
    ) in output
    assert (
        "SCANNING viewport=1 verified=0 relevant=0 skipped=1"
    ) in output
    assert "body_read=0 body_unknown=0 classified=1" in output
    assert "unverified_relevant=1" in output
    assert "STOP stage=FEED_SCROLL" not in output
    assert all(value not in output for value in (*invalid_urls, "Hiring content must be skipped"))
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.home_sampled == 1
    assert checkpoint.home_relevant == 1


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


def test_verified_multiline_body_stays_unknown_without_private_text_logging(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    first_paragraph = "Hiring for a confidential engineering role"
    quoted_paragraph = "Quoted context mentions a separate hiring plan"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
  <a href="/@candidate/post/multiline"><time>1h</time></a>
  <a href="/@candidate">@candidate</a>
  <p>{first_paragraph}</p><p>{quoted_paragraph}</p>
</article></main></body></html>"""
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
            max_posts=1,
            max_scrolls=0,
            max_total_posts=10,
            terms=("hiring",),
            show_post_text=True,
        )
        assert result.status == "VIEWPORT_COVERAGE_INCOMPLETE"
        assert result.home_sampled == 1
        assert result.home_relevant == result.home_irrelevant == 0
        assert result.home_unknown == 1
        assert result.detail_reads == 0
        assert result.checkpoint_runs == 0
        assert not result.viewport_coverage_complete

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert (
        "SCANNING viewport=1 verified=1 relevant=0 skipped=0 visible_links=1 "
        "inspected=1 body_read=0 body_unknown=1 classified=1 observed_rows=1 "
        "body_extracted=0 provenance_verified=1"
    ) in output
    assert "viewport_coverage_complete=false" in output
    assert first_paragraph not in output
    assert quoted_paragraph not in output
    assert "POST_TEXT viewport=1 status=UNKNOWN" in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 0
    assert checkpoint.home_unknown == 0


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
        result = await workflow.run_page_workflow(
            page,
            account,
            store,
            max_posts=3,
            max_scrolls=2,
            max_total_posts=20,
            terms=("hiring",),
        )
        assert result.status == "VERIFIED_IRRELEVANT"
        assert result.home_sampled == 3
        assert result.home_irrelevant == 3
        assert result.home_unknown == 0
        assert result.detail_reads == 0
        assert result.scroll_attempts == result.scrolls == 2
        assert result.checkpoint_runs == 1
        assert await page.evaluate("sessionStorage.getItem('wheel-count')") == "2"

    _run_offline_page(feed_html, exercise)
    policy = policy_fingerprint(canonical_terms(("hiring",)))
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 3
    assert checkpoint.home_irrelevant == 3
    assert checkpoint.scroll_attempts == checkpoint.scrolls == 2
    assert "SEARCH_BUDGET_EXHAUSTED reason=MAX_SCROLLS viewports=3" in capsys.readouterr().out


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
    assert all(
        body not in output
        for body in (unverified_body, verified_body, selected_body, quoted_body)
    )
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
