from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path

import pytest
from playwright.async_api import Route

from tests.fixtures.browser import (
    _assert_bounded_wheel_gestures,
    _run_offline_page,
    _run_page_workflow_for_test,
)
from threads_browser import reader as reader_module
from threads_browser import workflow
from threads_browser.browser_ui.detail import DetailNavigator
from threads_browser.console_output import ConsoleOutputPolicy
from threads_browser.feed.models import PostTarget
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


def test_auto_actionability_rejects_partial_revalidated_body() -> None:
    target = PostTarget(
        url="https://www.threads.com/@candidate/post/partial",
        author="candidate",
        body="Chúng tôi đang tuyển QA Engineer cho nhóm sản phẩm.",
        fingerprint="a" * 64,
        permalink=None,  # type: ignore[arg-type]
        currently_visible=True,
        body_complete=True,
        body_consistent=True,
    )

    class Host:
        async def _revalidate_action_target(self, _target):
            return replace(_target, body_complete=False)

    navigator = DetailNavigator(
        Host(),
        timeout_ms=1_000,
        console_output_policy=ConsoleOutputPolicy.AUTO,
    )
    with pytest.raises(UiReadError, match="DETAIL_PROVENANCE_UNVERIFIED"):
        asyncio.run(navigator.actionable_now(target))


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
        result = await _run_page_workflow_for_test(
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
        await _assert_bounded_wheel_gestures(page, result.scrolls)

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
article:first-of-type {{ height: 808px; padding-top: 350px; }}
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
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate/post/intermediate"><time>2h</time></a>
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
        result = await _run_page_workflow_for_test(
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
    assert workflow._terminal_safe_post_text(f"before{character}after") == (f"before{escaped}after")


def test_unverified_candidate_does_not_block_later_verified_selection(
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
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=3,
            max_total_posts=10,
            terms=("hiring", "engineer"),
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.home_sampled == result.home_new == 1
        assert result.home_relevant == 1
        assert result.skipped_unverified == 1
        assert result.scroll_attempts == result.scrolls
        assert result.scroll_attempts > 0
        assert result.detail_reads == result.checkpoint_runs == 1
        await _assert_bounded_wheel_gestures(page, result.scrolls)
        assert float(await page.evaluate("sessionStorage.getItem('wheel-delta')")) < 360

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert detail_requests == 1
    assert (
        "SKIPPED stage=FEED_INITIAL reason=CANDIDATE_NONCANONICAL count=1 "
        "cause=POST_PATH_SHAPE_INVALID"
    ) in output
    assert ("SCANNING viewport=1 verified=0 relevant=0 skipped=1") in output
    assert "body_read=0 body_unknown=0 classified=1" in output
    assert "unverified_relevant=1" in output
    assert "SCROLL attempt=1 observed=1" in output
    assert all(value not in output for value in (*invalid_urls, "Hiring content must be skipped"))
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring", "engineer"))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 1
    assert checkpoint.detail_reads == 1
    assert checkpoint.pending_read_fingerprints == ()


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
</nav><main><div class="post-card">
  <a href="/@candidate/post/trailing-safe/"><time>1h</time></a>
  <a href="{invalid_secondary}">Secondary</a>
  <a href="/@candidate">@candidate</a><span>{body}</span>
</div></main></body></html>"""
    detail_html = f"""<!doctype html><html><body><main><div class="post-card">
  <a href="/@candidate/post/trailing-safe/"><time>1h</time></a>
  <a href="/@candidate">@candidate</a><p>{body}</p>
</div></main></body></html>"""
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
    assert "[ACTIONABILITY] status=CONFIRMED" in output
    assert invalid_secondary not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 1


def test_noncanonical_link_reasons_are_aggregate_private_and_skipped(
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
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=20,
            max_scrolls=0,
            max_total_posts=10,
            terms=("hiring",),
            show_post_text=True,
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.home_sampled == 0
        assert result.skipped_unverified == 8, result
        assert result.unverified_relevant > 0
        assert result.detail_reads == 0
        assert result.checkpoint_runs == 1
        assert not result.viewport_coverage_complete

    _run_offline_page(feed_html, exercise)
    output = capsys.readouterr().out
    assert "cause=POST_LINK_HAS_QUERY_OR_FRAGMENT" in output
    assert "cause=POST_ORIGIN_INVALID" in output
    assert "cause=POST_PATH_SHAPE_INVALID" in output
    assert "cause=POST_AUTHOR_OR_ID_INVALID" in output
    assert "cause=CANDIDATE_AUTHOR_MISMATCH" in output
    assert "cause=CANDIDATE_WRAPPER_AMBIGUOUS" in output
    assert "url_parsed=3 author_unverified=1 wrapper_ambiguous=1 card_not_ready=0" in output
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
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 0
    assert checkpoint.seen_fingerprints == ()


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
