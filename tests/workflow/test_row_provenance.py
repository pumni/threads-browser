from __future__ import annotations

import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Route

from tests.fixtures.browser import (
    _run_offline_page,
    _run_page_workflow_for_test,
)
from threads_browser import reader as reader_module
from threads_browser import workflow
from threads_browser.browser_ui.body_text import BodyTextReader
from threads_browser.browser_ui.card_provenance import CardProvenanceReader
from threads_browser.browser_ui.feed_rows import FeedRowScanner, UnverifiedFeedCandidate
from threads_browser.browser_ui.loading import LoadingObserver
from threads_browser.reader import (
    HOME_URL,
    ThreadsHomeReader,
    UiReadError,
    canonical_terms,
)
from threads_browser.storage import (
    AccountStore,
    BrowserDataError,
    Checkpoint,
    CheckpointStore,
)
from threads_browser.workflow import policy_fingerprint


def test_mixed_candidate_rows_do_not_block_a_verified_selected_post(
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
            result = await _run_page_workflow_for_test(
                page,
                account,
                store,
                max_posts=3,
                terms=("hiring", "engineer"),
            )
            assert result.status == "COMPLETE_WITH_SKIPS"
            assert result.home_sampled == 1
            assert result.detail_reads == 1
            assert result.scroll_attempts == result.scrolls > 0
            assert not result.viewport_coverage_complete
            assert await page.evaluate("sessionStorage.getItem('wheel-target')") is not None

        _run_offline_page(html, exercise)
        policy = policy_fingerprint(canonical_terms(("hiring", "engineer")))
        checkpoint = store.load(account.account_id, policy)
        assert checkpoint.completed_runs == 1
        assert checkpoint.home_sampled == 1
        assert len(checkpoint.seen_fingerprints) == 1
        assert checkpoint.detail_reads == 1
        assert clean_detail_requests == 1
        assert rejected_detail_requests == 0
        output = capsys.readouterr().out
        assert "SKIPPED stage=FEED_INITIAL reason=CANDIDATE_NONCANONICAL count=1" in output
        assert "VIEWPORT_COVERAGE_INCOMPLETE" in output
        assert "quoted candidate" not in output
        assert "malformed candidate" not in output


def test_detail_author_provenance_mismatch_returns_home_with_retry_pending(
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
            result = await _run_page_workflow_for_test(
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
            assert result.status == "DETAIL_RETRY_PENDING", state
            assert result.detail_reads == 0
            assert result.checkpoint_runs == 1
            assert page.url == HOME_URL
            assert await page.evaluate("sessionStorage.getItem('wheelCount')") == "0"

        _run_offline_page(html, exercise)
        policy = policy_fingerprint(canonical_terms(("hiring",)))
        checkpoint = store.load(account.account_id, policy)
        assert checkpoint.completed_runs == 1
        assert checkpoint.detail_reads == 0
        assert len(checkpoint.pending_read_fingerprints) == 1
        assert detail_requests == 1
        assert readiness_prompts == []
        output = capsys.readouterr().out
        assert "terminal_reason=AUTHOR_CONFLICT" in output
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


def test_replaced_home_row_gets_a_new_observation_id() -> None:
    actor = "offline.operator"
    original_url = "https://www.threads.com/@candidate/post/identity-old"
    replacement_url = "https://www.threads.com/@candidate/post/identity-new"
    original_body = "Synthetic fixture original row body."
    replacement_body = "Synthetic fixture replacement row body."
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article id="row">
<a href="/@candidate/post/identity-old"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{original_body}</p>
</article></main></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        original_targets = await reader.scan_viewport(1)
        assert len(original_targets) == 1
        original = original_targets[0]
        old_diagnostic = next(
            row
            for row in reader.last_candidate_diagnostics
            if row.identity_fingerprint == original.fingerprint
        )
        original_observation_id = old_diagnostic.observation_id

        await page.locator("#row").evaluate(
            """node => {
                const inserted = document.createElement('article');
                inserted.innerHTML = `
                    <a href="/@candidate/post/identity-inserted"><time>new</time></a>
                    <a href="/@candidate">candidate</a>
                    <p>Synthetic fixture inserted row body.</p>`;
                node.before(inserted);
            }"""
        )
        middle_targets = await reader.scan_viewport(2)
        assert len(middle_targets) == 2
        middle_original = next(target for target in middle_targets if target.url == original_url)
        middle_diagnostic = next(
            row
            for row in reader.last_candidate_diagnostics
            if row.identity_fingerprint == middle_original.fingerprint
        )
        assert middle_diagnostic.observation_id == original_observation_id

        await page.locator("#row").evaluate(
            """node => {
                const replacement = node.cloneNode(true);
                replacement.querySelector("a[href*='/post/']").href =
                    '/@candidate/post/identity-new';
                replacement.querySelector('p').textContent =
                    'Synthetic fixture replacement row body.';
                node.replaceWith(replacement);
            }"""
        )
        identity_before_replacement = next(
            identity
            for identity in reader._last_viewport_candidate_identities
            if identity.fingerprint == middle_original.fingerprint
        )
        assert not await identity_before_replacement.dom_handle.evaluate("node => node.isConnected")

        replacement_targets = await reader.scan_viewport(2)
        replacement = next(
            target for target in replacement_targets if target.url == replacement_url
        )
        assert replacement.url == replacement_url
        assert replacement.body == replacement_body
        new_diagnostic = next(
            row
            for row in reader.last_candidate_diagnostics
            if row.identity_fingerprint == replacement.fingerprint
        )
        assert new_diagnostic.observation_id != original_observation_id
        assert original.url == original_url

    _run_offline_page(html, exercise)


def test_workflow_skips_detail_when_no_home_candidate_is_actionable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/not-actionable"
    body = "Hiring a frontend engineer in Vietnam"
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article>
<a href="/@candidate/post/not-actionable"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")
    policy = policy_fingerprint(canonical_terms(("hiring",)))
    store.save(Checkpoint(account.account_id, policy, completed_runs=4))
    detail_reads = 0
    detail_requests = 0

    class NonActionableReader(ThreadsHomeReader):
        async def actionable_now(self, target):
            raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")

        async def read_detail(self, target):
            nonlocal detail_reads
            detail_reads += 1
            raise AssertionError("Detail must not be entered")

    monkeypatch.setattr(workflow, "ThreadsHomeReader", NonActionableReader)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            await route.abort()

        await page.route(target_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=0,
            terms=("hiring",),
            detail=False,
        )
        assert result.status == "NO_ACTIONABLE_TARGET"
        assert result.stop_reason == "NO_ACTIONABLE_TARGET"
        assert result.detail_reads == 0
        assert result.checkpoint_runs == 4
        assert result.checkpoint_saved is False

    _run_offline_page(html, exercise)
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.completed_runs == 4
    assert detail_reads == detail_requests == 0


def test_checkpoint_save_failure_never_returns_a_saved_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    detail_url = "https://www.threads.com/@candidate/post/weather"
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article>
<a href="/@candidate/post/weather"><time>now</time></a>
<a href="/@candidate">candidate</a><p>A synthetic weather update</p>
</article></main></body></html>"""
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")

    def fail_save(_checkpoint) -> None:
        raise BrowserDataError("STATE_INVALID")

    monkeypatch.setattr(store, "save", fail_save)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(
                status=200,
                content_type="text/html",
                body="""<!doctype html><html><body><main>
<div data-pressable-container="true">
<header><a href="/@candidate">@candidate</a></header>
<div class="post-body"><p>A synthetic weather update</p></div>
</div></main></body></html>""",
            )

        await page.route(detail_url, serve_detail)
        with pytest.raises(UiReadError) as caught:
            await _run_page_workflow_for_test(
                page,
                account,
                store,
                max_posts=1,
                max_scrolls=0,
                terms=("hiring",),
            )
        assert caught.value.code == "INTERNAL_OPERATION_ERROR"

    _run_offline_page(html, exercise)
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 0


def test_unresolved_post_root_does_not_block_a_verified_selected_post(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    clean_body = "Hiring a frontend engineer in Vietnam"
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main>
<article id="unresolved-root">
  <a href="/@candidate/post/unresolved"><span>Permalink</span></a>
</article>
<article id="clean-root">
  <a href="/@candidate/post/clean-unresolved-peer"><time>now</time></a>
  <a href="/@candidate">candidate</a><p>{clean_body}</p>
</article>
</main></body></html>"""
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
                    f"<p>{clean_body}</p>"
                    "</article></main></body></html>"
                ),
            )

        await page.route("**/@candidate/post/clean-unresolved-peer", serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=0,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert not result.viewport_coverage_complete
        assert result.detail_reads == 1
        assert result.checkpoint_runs == 1

    _run_offline_page(html, exercise)
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 1
    output = capsys.readouterr().out
    assert "POST_ROOT_UNRESOLVED:1" in output


def test_ambiguous_pressable_row_recovers_only_its_timestamp_author_and_body() -> None:
    actor = "offline.operator"
    primary_body = "Hiring a backend engineer in Vietnam"
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article id="ambiguous-wrapper">
  <div id="primary-post">
    <a href="/@candidate/post/primary"><time>now</time></a>
    <a href="/@candidate">candidate</a>
    <p>{primary_body}</p>
    <a href="/@candidate/post/secondary?source=synthetic">secondary link</a>
  </div>
  <div id="related-post">
    <a href="/@candidate/post/related">related link</a>
    <p>Weather text from a related card</p>
  </div>
  <blockquote>
    <a href="/@candidate/post/quoted"><time>2h</time></a>
    <p>Quoted text from another post</p>
  </blockquote>
  <div data-pressable-container="true" id="media-card">
    <a href="/@candidate/post/media"><time>3h</time></a>
    <a href="/@candidate">candidate</a><p>Media card text</p>
  </div>
</article></main></body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        targets = await reader.scan_viewport(2)

        assert len(targets) == 1
        assert targets[0].url.endswith("/post/primary")
        assert targets[0].author == "candidate"
        assert targets[0].body == primary_body
        assert targets[0].currently_visible
        assert reader.last_scan_counts.coverage_complete
        assert reader.last_scan_counts.wrapper_ambiguous == 0
        assert not reader.last_scan_counts.structural_reasons.get("ROW_PROVENANCE_UNRESOLVED")
        assert reader.last_candidate_diagnostics[0].reason == "PROVENANCE_VERIFIED"

    _run_offline_page(html, exercise)


def test_card_provenance_component_verifies_canonical_url_author_and_body() -> None:
    html = """<!doctype html><html><body><main><article>
<a id="permalink" href="/@candidate/post/card-proof"><time>now</time></a>
<a id="author" href="/@candidate">candidate</a>
<p>Hiring a backend engineer</p>
</article></main></body></html>"""

    async def exercise(page) -> None:
        body_text = BodyTextReader(page)
        card_reader = CardProvenanceReader(
            FeedRowScanner(page),
            body_text,
            LoadingObserver(page),
        )

        async def unused_visible_body_progress(*args, **kwargs):
            raise AssertionError("body progress is not used for an offscreen read")

        root = page.locator("article")
        target = await card_reader.read_card(
            root,
            read_visible_body_progress=unused_visible_body_progress,
        )
        assert target.url.endswith("/@candidate/post/card-proof")
        assert target.author == "candidate"
        assert target.body == "Hiring a backend engineer"

        await page.locator("#author").evaluate(
            "node => node.setAttribute('href', '/@different_author')"
        )
        with pytest.raises(UnverifiedFeedCandidate) as author_error:
            await card_reader.read_card(
                root,
                read_visible_body_progress=unused_visible_body_progress,
            )
        assert author_error.value.reason == "CANDIDATE_AUTHOR_CONFLICT"

        await page.locator("#author").evaluate("node => node.setAttribute('href', '/@candidate')")
        await page.locator("#permalink").evaluate(
            "node => node.setAttribute('href', '/@candidate/post/card-proof?source=bad')"
        )
        with pytest.raises(UnverifiedFeedCandidate) as url_error:
            await card_reader.read_card(
                root,
                read_visible_body_progress=unused_visible_body_progress,
            )
        assert url_error.value.reason == "CANDIDATE_NONCANONICAL"

    _run_offline_page(html, exercise)


def test_unresolved_ambiguous_row_does_not_block_a_verified_peer_checkpoint(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main>
<article id="unresolved-wrapper">
  <a href="/@candidate/post/ambiguous"><time>now</time></a>
  <a href="/@candidate/post/related">related link</a>
  <a href="/@candidate">candidate</a>
  <p>Hiring body whose post boundary is not isolated</p>
</article>
<article id="clean-row">
  <a href="/@candidate/post/clean-row"><time>1h</time></a>
  <a href="/@candidate">candidate</a>
  <p>A synthetic weather update</p>
</article>
</main></body></html>"""
    profile = tmp_path / "profile"
    profile.mkdir()
    account = AccountStore(tmp_path / "data").add("offline", actor, profile)
    store = CheckpointStore(tmp_path / "data")

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=4,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE_WITH_SKIPS"
        assert result.unresolved_gaps == 1
        assert result.home_sampled == 1
        assert result.detail_reads == 0
        assert result.checkpoint_runs == 1

    _run_offline_page(html, exercise)
    output = capsys.readouterr().out
    assert "ROW_PROVENANCE_UNRESOLVED:1" in output
    assert "POST_ROOT_UNRESOLVED:1" not in output
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == 1


def test_removed_ambiguous_row_gets_a_new_observation_identity() -> None:
    actor = "offline.operator"
    html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article id="row-to-remove">
  <a href="/@candidate/post/primary"><time>now</time></a>
  <a href="/@candidate/post/related">related link</a>
  <a href="/@candidate">candidate</a>
  <p>A synthetic row removed during recovery</p>
</article></main></body></html>"""

    class RemoveRowReader(ThreadsHomeReader):
        original_observation_id: str | None = None

        async def _recover_ambiguous_visible_card(self, root, root_handle, **kwargs):
            self.original_observation_id = self.last_candidate_diagnostics[0].observation_id
            await root.evaluate("node => node.remove()")
            raise reader_module._CardNotReady(
                root,
                reason_code="POST_ROOT_DOM_IDENTITY_CHANGED",
            )

    async def exercise(page) -> None:
        reader = RemoveRowReader(page, actor)
        await reader.open_home()
        targets = await reader.scan_viewport(1)

        assert targets == ()
        assert reader.original_observation_id is not None
        assert len(reader.last_candidate_diagnostics) == 1
        diagnostic = reader.last_candidate_diagnostics[0]
        assert diagnostic.reason == "POST_ROOT_IDENTITY_CHANGED"
        assert diagnostic.observation_id != reader.original_observation_id
        assert not reader.last_viewport_can_wheel

    _run_offline_page(html, exercise)
