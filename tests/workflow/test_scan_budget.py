from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Route

from tests.fixtures.browser import (
    _assert_bounded_wheel_gestures,
    _run_offline_page,
    _run_page_workflow_for_test,
)
from threads_browser.reader import (
    HOME_URL,
    ThreadsHomeReader,
    UiReadError,
    canonical_terms,
)
from threads_browser.storage import (
    AccountStore,
    CheckpointStore,
)
from threads_browser.workflow import policy_fingerprint


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
        f"""<article><a href="/@candidate/post/earlier-{index}"><time>{index}h</time></a>
<a href="/@candidate">candidate</a><p>A synthetic weather update.</p></article>"""
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
        assert reader.last_candidate_diagnostics[0].reason == ("ROW_CONTENT_CHANGED_DURING_READ")
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
