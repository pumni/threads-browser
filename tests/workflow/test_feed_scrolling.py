from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import Route

from tests.fixtures.browser import (
    _run_offline_page,
    _run_page_workflow_for_test,
)
from threads_browser import reader as reader_module
from threads_browser.browser_ui.scrolling import (
    MAX_WHEEL_STEP_PX,
)
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
  <div class="card-body">{"".join(f"<p>{part}</p>" for part in fragments)}</div>
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
<span dir="auto">{pending_body}</span></div>{"".join(post_rows[1:])}</body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        waits: list[tuple[str, int, int | None]] = []
        original_wait = reader._wait_for_pending_card
        hydration_scheduled = False

        async def record_wait(pending, deadline, *, max_wait_ms=None):
            nonlocal hydration_scheduled
            for signal in pending.signals:
                waits.append((signal.condition, await signal.locator.count(), max_wait_ms))
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
            condition == "visible" and wait_ms == reader_module._CARD_HYDRATION_WAIT_MS
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
  <span dir="auto">{f"Hiring a synthetic fixture engineer {index}." if index == 1 else f"Weather fixture {index}."}</span>
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
