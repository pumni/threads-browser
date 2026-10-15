from __future__ import annotations

import asyncio
import json
import re
import tempfile
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


def test_relevance_classifier_is_conservative() -> None:
    terms = canonical_terms(("tuyển dụng", "engineer"))
    assert classify_body("We are hiring an engineer", terms) == "RELEVANT"
    assert classify_body("A weather update", terms) == "IRRELEVANT"
    assert classify_body(None, terms) == "UNKNOWN"
    assert classify_body("   ", terms) == "UNKNOWN"


def test_offline_chromium_cli_reads_once_and_resumes_without_double_counting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    old_url = "https://www.threads.com/@candidate/post/old"
    target_url = "https://www.threads.com/@candidate/post/one"
    old_body = "A weather update"
    body = "Hiring a frontend engineer in Vietnam"
    feed_html = f"""<!doctype html>
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
  <p>{body}</p>
</article>
</body></html>"""
    detail_html = f"""<!doctype html><html><body>
<article>
  <a href="/@candidate/post/one"><time>1h</time></a>
  <a href="/@candidate">@candidate</a>
  <p>{body}</p>
</article>
</body></html>"""
    account_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(account_root).add("offline", actor, profile)
    store = CheckpointStore(account_root)
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(account_root))
    requests: list[str] = []

    async def respond(route: Route) -> None:
        url = route.request.url
        requests.append(url)
        if url == HOME_URL:
            html = feed_html
        elif url == target_url:
            html = detail_html
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
        "1",
        "--term",
        "hiring",
        "--term",
        "engineer",
    ]

    first_code = main(command)
    first = capsys.readouterr()
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
    assert "NURTURE status=NO_NEW_RELEVANT_POST source=HOME_FOR_YOU" in second.out
    assert "home_sampled=2 home_new=0" in second.out
    assert "home_relevant=1 home_irrelevant=1" in second.out
    assert "home_relevant_ratio=0.50" in second.out
    assert "duplicates=2 detail_reads=0" in second.out
    assert "scroll_attempts=1 scrolls=1" in second.out
    assert all(
        value not in first.out + first.err + second.out + second.err
        for value in (str(profile), actor, old_body, body, old_url, target_url)
    )

    assert requests
    assert all(urlsplit(url).netloc == "www.threads.com" for url in requests)
    assert {HOME_URL, target_url}.issubset(requests)
    assert requests.count(target_url) == 1
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 2
    assert checkpoint.home_sampled == 4
    assert checkpoint.home_new == 2
    assert checkpoint.home_relevant == 2
    assert checkpoint.home_irrelevant == 2
    assert checkpoint.duplicates == 2
    assert checkpoint.detail_reads == 1
    assert checkpoint.scroll_attempts == 2
    assert checkpoint.scrolls == 2
    assert len(checkpoint.seen_fingerprints) == 2
    assert checkpoint.pending_read_fingerprints == ()
    checkpoint_path = (
        account_root
        / "nurture"
        / "checkpoints"
        / f"{account.account_id}-{checkpoint.policy_fingerprint}.json"
    )
    persisted = checkpoint_path.read_text(encoding="utf-8")
    assert old_body not in persisted
    assert body not in persisted
    assert old_url not in persisted
    assert target_url not in persisted
    assert actor not in persisted


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
  setTimeout(() => window.scrollBy(0, 850), 100);
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
.post-card {{ box-sizing: border-box; height: 500px; padding-top: 80px; }}
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
      setTimeout(() => document.querySelector("nav").append(profile), 750);
      setTimeout(() => main.innerHTML = renderedFeed, 750);
    }} else {{
      main.innerHTML = renderedFeed;
    }}
  }}
}});
window.addEventListener("wheel", event => {{
  event.preventDefault();
  const wheels = Number(sessionStorage.getItem("recovery-wheel-count") || "0") + 1;
  sessionStorage.setItem("recovery-wheel-count", String(wheels));
  setTimeout(() => window.scrollBy(0, 400), 40);
}}, {{ once: true, passive: false }});
</script></body></html>"""
    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 600)
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
                max_posts=1,
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
        assert "WAITING stage=DETAIL retry=1" in output
        assert "RECOVERED stage=DETAIL retry=1" in output


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
        assert elapsed < 1
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
    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 150)
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


def test_delayed_wheel_and_detail_hydration_return_to_home() -> None:
    actor = "offline.operator"
    detail_url = "https://www.threads.com/@candidate/post/ready-detail"
    detail_body = "Hiring a frontend engineer in Vietnam"
    feed_html = f"""<!doctype html><html><head><style>
.post-card {{ box-sizing: border-box; height: 760px; padding-top: 80px; }}
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a id="profile" aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main>
  <div class="post-card"><a href="/@candidate/post/first"><time>2h</time></a>
    <a href="/@candidate">@candidate</a><p>A weather update</p></div>
  <div class="post-card"><a href="/@candidate/post/ready-detail"><time>1h</time></a>
    <a href="/@candidate">@candidate</a><p>{detail_body}</p></div>
</main><script>
window.addEventListener("wheel", event => {{
  event.preventDefault();
  setTimeout(() => window.scrollBy(0, 850), 100);
}}, {{ once: true, passive: false }});
const main = document.querySelector("main");
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
        async def serve_detail(route: Route) -> None:
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
        home_targets, _ = await reader._visible_targets(1)
        assert home_targets

    _run_offline_page(feed_html, exercise)


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
  setTimeout(() => window.scrollBy(0, 850), 40);
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
                max_posts=1,
                terms=("hiring", "engineer"),
            )
        assert caught.value.code == "DETAIL_BODY_UNVERIFIED"

    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 1_000)
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
        with pytest.raises(UiReadError) as caught:
            await workflow.run_page_workflow(
                page,
                account,
                store,
                max_posts=2,
                terms=("hiring",),
                readiness_prompt=provenance_prompt,
            )
        assert caught.value.code == "HOME_SCROLL_UNVERIFIED"
        assert await page.evaluate("window.wheelCount") == 1
        assert provenance_prompts == [("HOME_SCROLL", 2)]

    with monkeypatch.context() as short_recovery:
        short_recovery.setattr(reader_module, "_READINESS_TIMEOUT_MS", 500)
        short_recovery.setattr(reader_module, "_READINESS_RECHECK_INTERVAL_MS", 20)
        _run_offline_page(malformed_html, malformed_exercise)
    policy = policy_fingerprint(canonical_terms(("hiring",)))
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.completed_runs == 0
    assert checkpoint.home_sampled == 0
    assert checkpoint.scroll_attempts == 0
    assert checkpoint.scrolls == 0
    output = capsys.readouterr().out
    assert "SKIPPED stage=FEED_INITIAL reason=CANDIDATE_AMBIGUOUS count=2" in output
    assert "SCROLL attempt=1 observed=0" in output
    assert "Two identities in one card" not in output

    no_movement_html = f"""<!doctype html><html><head><style>
.post-card {{ box-sizing: border-box; height: 760px; padding-top: 80px; }}
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
        monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 1_000)
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
    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 1_000)
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
                    max_posts=1,
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
  setTimeout(() => window.scrollBy(0, 850), 40);
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
  setTimeout(() => window.scrollBy(0, 850), 40);
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


def test_mixed_feed_candidate_is_skipped_and_clean_post_is_read_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    clean_url = "https://www.threads.com/@candidate/post/clean"
    clean_body = "Hiring a frontend engineer in Vietnam"
    html = """<!doctype html><html><head><style>
.post-card { box-sizing: border-box; height: 260px; }
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
  const card = event.target.closest(".post-card");
  sessionStorage.setItem("wheel-target", card?.id || "mixed-card");
  setTimeout(() => window.scrollBy(0, 850), 40);
}, { once: true });
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
                max_posts=2,
                terms=("hiring", "engineer"),
            )
            assert result.status == "COMPLETE"
            assert result.home_sampled == 1
            assert result.home_relevant == 1
            assert result.detail_reads == 1
            assert result.scroll_attempts == 1
            assert result.scrolls == 1
            assert result.skipped_unverified == 3
            assert await page.evaluate(
                "sessionStorage.getItem('wheel-target')"
            ) == "clean-card"

        _run_offline_page(html, exercise)
        policy = policy_fingerprint(canonical_terms(("hiring", "engineer")))
        checkpoint = store.load(account.account_id, policy)
        assert checkpoint.completed_runs == 1
        assert checkpoint.home_sampled == 1
        assert checkpoint.seen_fingerprints
        assert checkpoint.detail_reads == 1
        assert clean_detail_requests == 1
        assert rejected_detail_requests == 0
        output = capsys.readouterr().out
        assert "SKIPPED stage=FEED_INITIAL reason=CANDIDATE_NONCANONICAL count=1" in output
        assert "SKIPPED stage=FEED_INITIAL reason=CANDIDATE_AMBIGUOUS count=2" in output
        assert "SKIPPED stage=FEED_RETURN reason=CANDIDATE_NONCANONICAL count=1" in output
        assert "SKIPPED stage=FEED_RETURN reason=CANDIDATE_AMBIGUOUS count=2" in output
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
  setTimeout(() => window.scrollBy(0, 850), 40);
}}, {{ once: true, passive: false }});
</script></body></html>"""
    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 1_000)
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
            assert await page.evaluate("sessionStorage.getItem('wheelCount')") == "1"

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


def test_unread_relevant_post_resumes_after_scroll_without_second_read(
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
        "2",
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
    assert "home_sampled=4 home_new=4" in first.out
    assert "home_relevant=2 home_irrelevant=2" in first.out
    assert "detail_reads=1" in first.out
    assert len(checkpoint.pending_read_fingerprints) == 1
    assert len(checkpoint.seen_fingerprints) == 4

    second_code = main(command)
    second = capsys.readouterr()
    checkpoint = store.load(account.account_id, policy)
    assert second_code == 0, second.out + second.err
    assert "NURTURE status=COMPLETE source=HOME_FOR_YOU" in second.out
    assert "home_sampled=4 home_new=0" in second.out
    assert "detail_reads=1" in second.out
    assert checkpoint.pending_read_fingerprints == ()

    third_code = main(command)
    third = capsys.readouterr()
    assert third_code == 0, third.out + third.err
    assert "NURTURE status=NO_NEW_RELEVANT_POST source=HOME_FOR_YOU" in third.out
    assert "detail_reads=0" in third.out
    assert requests.count(pending_url) == 1
    assert requests.count(first_url) == 1
    checkpoint_path = (
        account_root
        / "nurture"
        / "checkpoints"
        / f"{account.account_id}-{policy}.json"
    )
    prior_schema = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    prior_schema["schema_version"] = 2
    checkpoint_path.write_text(json.dumps(prior_schema), encoding="utf-8")
    with pytest.raises(BrowserDataError) as unsupported:
        store.load(account.account_id, policy)
    assert unsupported.value.code == "STATE_INVALID"


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
        assert caught.value.code == "HOME_UI_UNVERIFIED"

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
        short_timeout.setattr(reader_module, "_READINESS_TIMEOUT_MS", 100)
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
        assert caught.value.code == "HOME_SCROLL_UNVERIFIED"
        assert reader.scroll_attempts == 1
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
article {{ box-sizing: border-box; height: 600px; padding-top: 60px; }}
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
  window.scrollBy(0, 560);
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
            max_posts=1,
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
<div class="post-card"><a href="{invalid_urls[0]}">Permalink A</a>
  <a href="{invalid_urls[1]}">Permalink B</a><p>Hiring content must be skipped</p></div>
<div class="post-card"><a href="/@candidate/post/later-valid"><time>1h</time></a>
  <a href="/@candidate">@candidate</a><p>{detail_body}</p></div>
</main><script>
window.addEventListener("wheel", event => {{
  event.preventDefault();
  const count = Number(sessionStorage.getItem("wheel-count") || "0") + 1;
  sessionStorage.setItem("wheel-count", String(count));
  window.scrollBy(0, 560);
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
            max_posts=1,
            max_scrolls=3,
            max_total_posts=10,
            terms=("hiring", "engineer"),
        )
        assert result.status == "COMPLETE"
        assert result.home_sampled == result.home_new == 1
        assert result.home_relevant == 1
        assert result.skipped_unverified == 2
        assert result.scroll_attempts == result.scrolls == 1
        assert result.detail_reads == 1
        assert await page.evaluate("sessionStorage.getItem('wheel-count')") == "1"

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "SKIPPED stage=FEED_INITIAL reason=CANDIDATE_NONCANONICAL count=2" in output
    assert "SCANNING viewport=1 verified=0 relevant=0 skipped=2" in output
    assert all(value not in output for value in (*invalid_urls, "Hiring content must be skipped"))
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.home_sampled == 1
    assert checkpoint.home_relevant == 1


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
  window.scrollBy(0, 560);
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
            max_posts=1,
            max_scrolls=2,
            max_total_posts=20,
            terms=("hiring",),
        )
        assert result.status == "NO_RELEVANT_POST_IN_BUDGET"
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
  window.scrollBy(0, 560);
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
  window.scrollBy(0, 560);
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
            max_posts=1,
            max_scrolls=8,
            max_total_posts=2,
            terms=("hiring",),
        )
        assert result.status == "NO_RELEVANT_POST_IN_BUDGET"
        assert result.home_sampled == 2
        assert result.scroll_attempts == result.scrolls == 1
        assert result.checkpoint_runs == 1
        assert await page.evaluate("sessionStorage.getItem('wheel-count')") == "1"

    _run_offline_page(valid_html, valid_exercise)
    valid_checkpoint = valid_store.load(
        valid_account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert valid_checkpoint.completed_runs == 1
    assert valid_checkpoint.home_sampled == 2
    assert valid_checkpoint.scroll_attempts == 1

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
        result = await workflow.run_page_workflow(
            page,
            zero_account,
            zero_store,
            max_posts=1,
            max_scrolls=2,
            max_total_posts=10,
            terms=("hiring",),
        )
        assert result.status == "NO_VERIFIED_POSTS_IN_BUDGET"
        assert result.home_sampled == result.home_new == 0
        assert result.home_relevant == result.home_irrelevant == 0
        assert result.scroll_attempts == result.scrolls == 2
        assert result.checkpoint_runs == 0
        assert await page.evaluate("sessionStorage.getItem('wheel-count')") == "2"

    _run_offline_page(zero_verified_html, zero_exercise)
    zero_policy = policy_fingerprint(canonical_terms(("hiring",)))
    zero_checkpoint = zero_store.load(zero_account.account_id, zero_policy)
    assert zero_checkpoint.completed_runs == 0
    assert zero_checkpoint.home_sampled == 0
    output = capsys.readouterr().out
    assert "SEARCH_BUDGET_EXHAUSTED reason=MAX_TOTAL_POSTS viewports=2" in output
    assert "SEARCH_BUDGET_EXHAUSTED reason=MAX_SCROLLS viewports=3" in output
