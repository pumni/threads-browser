from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import Route

from tests.fixtures.browser import (
    _run_offline_page,
    _run_page_workflow_for_test,
)
from threads_browser.browser_ui.geometry import with_scrollport_geometry
from threads_browser.browser_ui.scrolling import (
    MAX_WHEEL_STEP_PX,
    NativeWheelScrollEngine,
)
from threads_browser.reader import (
    ThreadsHomeReader,
    canonical_terms,
)
from threads_browser.storage import (
    AccountStore,
    CheckpointStore,
)
from threads_browser.workflow import policy_fingerprint


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
        assert float(geometry["scroll_top_after"]) > float(geometry["scroll_top_before"])
        after = await reader.scan_viewport(5)
        loaded = next(target for target in after if target.url.endswith("/post/nested-below"))
        assert loaded.body == "A synthetic second row."
        assert loaded.currently_visible
        assert reader.last_scan_counts.coverage_complete
        assert all(
            not diagnostic.observation_id or diagnostic.state in {"VERIFIED", "NON_POST"}
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
        assert await page.evaluate("window.scrollY") == pytest.approx(result.observed_delta)

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
