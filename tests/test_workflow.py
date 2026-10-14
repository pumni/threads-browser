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
    assert "detail_reads=1 scroll_attempts=1 scrolls=1 checkpoint_runs=1" in first.out
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


def test_readiness_timeouts_do_not_complete_checkpoint_and_bad_provenance_stops(
    monkeypatch: pytest.MonkeyPatch,
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
</nav><main><div>
  <a href="/@candidate/post/one"><time>2h</time></a>
  <a href="/@quoted/post/two"><time>1h</time></a>
  <a href="/@candidate">@candidate</a><a href="/@quoted">@quoted</a>
  <p>Two identities in one card.</p>
</div></main></body></html>"""

    async def malformed_exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        with pytest.raises(UiReadError) as caught:
            await reader.sample_and_scroll(2)
        assert caught.value.code == "POST_PROVENANCE_UNVERIFIED"
        assert reader.scroll_attempts == 0

    _run_offline_page(malformed_html, malformed_exercise)

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


def test_non_article_mixed_permalink_container_fails_closed() -> None:
    html = """<!doctype html><html><head><style>
.mixed-card { height: 900px; }
</style></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@offline.operator">Profile</a>
  <a href="/@navigation/post/decoy">Navigation decoy</a>
</nav>
<main><div class="mixed-card">
  <a href="/@candidate/post/first"><time>2h</time></a>
  <a href="/@candidate">@candidate</a>
  <a href="/@quoted/post/second"><time>1h</time></a>
  <a href="/@quoted">@quoted</a>
  <p>Two post identities share one wrapper.</p>
</div></main></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, "offline.operator")
        await reader.open_home()
        with pytest.raises(UiReadError) as caught:
            await reader.sample_and_scroll(2)
        assert caught.value.code == "POST_PROVENANCE_UNVERIFIED"
        assert reader.scroll_attempts == 0
        assert reader.scrolls == 0

    _run_offline_page(html, exercise)


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
