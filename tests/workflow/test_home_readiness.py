from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from playwright.async_api import Route

from tests.fixtures.browser import (
    _run_offline_page,
    _run_page_workflow_for_test,
)
from threads_browser import reader as reader_module
from threads_browser.reader import (
    ThreadsHomeReader,
    UiReadError,
    canonical_terms,
)
from threads_browser.storage import (
    AccountStore,
    CheckpointStore,
)
from threads_browser.workflow import policy_fingerprint


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
