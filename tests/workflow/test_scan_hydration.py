from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Route
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from tests.fixtures.browser import (
    _run_offline_page,
    _run_page_workflow_for_test,
)
from threads_browser import reader as reader_module
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
            assert await page.evaluate("sessionStorage.getItem('recovery-wheel-count')") == "1"

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
