from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import Route

from tests.fixtures.browser import (
    _run_offline_page,
    _run_page_workflow_for_test,
    create_local_account,
    run_offline_pages,
    synthetic_detail_html,
)
from threads_browser import reader as reader_module
from threads_browser.reader import (
    HOME_URL,
    ThreadsHomeReader,
    canonical_terms,
)
from threads_browser.storage import (
    AccountStore,
    CheckpointStore,
)
from threads_browser.workflow import policy_fingerprint


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


def test_detail_target_with_nested_and_adjacent_post_links_is_verified() -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/detail-with-related-links"
    body = "Hiring a synthetic engineer for a test fixture."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
  <a href="/@candidate/post/detail-with-related-links"><time>now</time></a>
  <a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    detail_html = f"""<!doctype html><html><body><main>
<article id="detail-root">
  <a href="/@candidate/post/detail-with-related-links"><time>now</time></a>
  <a href="/@candidate/post/detail-with-related-links">canonical permalink</a>
  <a href="/@candidate">candidate</a><p>{body}</p>
  <div class="related-post-links">
    <a href="/@reply/post/reply-context">synthetic reply</a>
    <a href="/@quote/post/quote-context">synthetic quote</a>
  </div>
</article>
<aside><a href="/@other/post/adjacent-context">synthetic adjacent post</a></aside>
</main></body></html>"""

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(status=200, content_type="text/html", body=detail_html)

        await page.route(target_url, serve_detail)
        reader = ThreadsHomeReader(page, actor)
        await reader.open_home()
        targets = await reader.scan_viewport(1)
        assert len(targets) == 1

        detail = await reader.read_detail(targets[0])
        assert reader.detail_route_verified
        assert detail.url == target_url
        assert detail.author == "candidate"
        assert detail.body == body
        reported = reader._detail_navigator._last_reported_detail_state
        assert reported is not None
        assert reported[1].canonical_self_permalink_candidates == 2
        assert reported[1].matching_roots == 1
        assert reported[1].terminal_reason == "DETAIL_VERIFIED"
        await reader.return_to_home()
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)


def test_detail_without_landmark_or_identity_links_uses_unique_matching_body(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/body-only-detail"
    body = "Hiring a synthetic backend engineer."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
<a href="/@candidate/post/body-only-detail"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    detail_html = synthetic_detail_html(("Hiring a synthetic", "backend engineer."))
    data_root = tmp_path / "data"
    account = create_local_account(data_root)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=0,
            terms=("hiring",),
            detail=True,
        )
        assert result.status == "COMPLETE"
        assert result.detail_reads == 1
        assert page.url == HOME_URL

    run_offline_pages({HOME_URL: feed_html, target_url: detail_html}, exercise)
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 1
    assert checkpoint.pending_read_fingerprints == ()
    output = capsys.readouterr().out
    assert "main_regions_count=0" in output
    assert "matching_roots=1" in output
    assert "terminal_reason=DETAIL_VERIFIED" in output


def test_back_accepts_selected_post_after_vertical_layout_shift(
    tmp_path: Path,
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/back-layout-shift"
    body = "Hiring a synthetic designer."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
<a id="selected" href="/@candidate/post/back-layout-shift"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main><script>
const selected = document.querySelector("#selected");
selected.addEventListener("click", () => {{
  sessionStorage.setItem("return-anchor-y", String(
    document.querySelector("article").getBoundingClientRect().y
  ));
}});
window.addEventListener("pageshow", () => {{
  if (sessionStorage.getItem("return-anchor-y") === null) return;
  const spacer = document.createElement("div");
  spacer.id = "return-layout-shift";
  spacer.style.height = "120px";
  document.querySelector("main").prepend(spacer);
  const before = Number(sessionStorage.getItem("return-anchor-y"));
  const after = document.querySelector("article").getBoundingClientRect().y;
  sessionStorage.setItem("return-anchor-drift", String(after - before));
  sessionStorage.removeItem("return-anchor-y");
}});
</script></body></html>"""
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(status=200, content_type="text/html", body=detail_html)

        await page.route(target_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=0,
            terms=("hiring",),
            detail=True,
        )
        drift = await page.evaluate("Number(sessionStorage.getItem('return-anchor-drift'))")
        assert drift > 48
        assert result.status == "COMPLETE"
        assert result.detail_reads == 1
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)
    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 1


@pytest.mark.parametrize(
    ("detail_fragment", "expected_code"),
    [
        (
            (
                '<div data-pressable-container="true"><a href="/@candidate">candidate</a>'
                '<a href="/@other">other</a><p>{body}</p></div>'
            ),
            "DETAIL_PROVENANCE_UNVERIFIED",
        ),
        (
            (
                '<div data-pressable-container="true"><a href="/@candidate">candidate</a>'
                '<a href="/@candidate/post/detail-evidence?view=1">rejected</a>'
                "<p>{body}</p></div>"
            ),
            "DETAIL_PROVENANCE_UNVERIFIED",
        ),
        (
            (
                '<div data-pressable-container="true">'
                '<a href="/@candidate/post/detail-evidence">canonical target</a>'
                '<a href="/@candidate">candidate</a>'
                '<a href="/@candidate/post/detail-evidence?view=1">noncanonical link</a>'
                "<p>{body}</p></div>"
            ),
            "DETAIL_PROVENANCE_UNVERIFIED",
        ),
        (
            (
                '<div data-pressable-container="true"><a href="/@candidate">candidate</a>'
                '<a href="/@candidate/post/another-post">mismatched post</a>'
                "<p>{body}</p></div>"
            ),
            "DETAIL_PROVENANCE_UNVERIFIED",
        ),
        (
            (
                '<div data-pressable-container="true">'
                '<a href="/@candidate/post/detail-evidence">canonical target</a>'
                '<a href="/@candidate/post/another-post">mismatched post</a>'
                "<p>{body}</p></div>"
            ),
            "DETAIL_PROVENANCE_UNVERIFIED",
        ),
        (
            (
                '<div data-pressable-container="true"><a href="/@candidate">candidate</a>'
                "<p>A different synthetic body.</p></div>"
            ),
            "DETAIL_BODY_UNVERIFIED",
        ),
        (
            (
                '<div data-pressable-container="true" id="selected-post">'
                '<a href="/@candidate/post/detail-evidence">selected post</a>'
                '<a href="/@candidate">candidate</a><p>{body}</p></div>'
                '<div data-pressable-container="true" id="related-reply-or-quote">'
                '<a href="/@candidate/post/related-context">related post</a>'
                '<a href="/@candidate">candidate</a><p>{body}</p></div>'
            ),
            "DETAIL_ROOT_AMBIGUOUS",
        ),
    ],
    ids=(
        "multiple-authors",
        "noncanonical-post-link",
        "target-plus-noncanonical-post-link",
        "mismatched-post-link",
        "mismatched-post-link-without-author",
        "changed-body",
        "ambiguous-reply-or-quote-root",
    ),
)
def test_detail_verification_failures_return_home_and_keep_retry_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    detail_fragment: str,
    expected_code: str,
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/detail-evidence"
    body = "Hiring a synthetic target body."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><a href="/@candidate/post/detail-evidence"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p></article></main></body></html>"""
    detail_html = (
        "<!doctype html><html><body><main>"
        + detail_fragment.replace("{body}", body)
        + "</main></body></html>"
    )
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 1_500)
    # Keep the fixture deadline bounded while allowing hosted Windows route scheduling.
    monkeypatch.setattr(reader_module, "_DETAIL_TIMEOUT_MS", 3_000)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            await route.fulfill(status=200, content_type="text/html", body=detail_html)

        await page.route(target_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=0,
            terms=("hiring",),
            detail=True,
        )
        assert result.status == "DETAIL_RETRY_PENDING"
        assert result.detail_reads == 0
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)
    policy = policy_fingerprint(canonical_terms(("hiring",)))
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 0
    assert len(checkpoint.pending_read_fingerprints) == 1
    output = capsys.readouterr().out
    assert f"status=UNAVAILABLE code={expected_code} retry=PENDING" in output
    assert "[BACK] post=1 status=RESTORED anchor=IDENTITY_VERIFIED" in output
    assert "DETAIL_VERIFIED status=PROVENANCE_VERIFIED" not in output
    diagnostics = "\n".join(
        line for line in output.splitlines() if line.startswith("[DETAIL_STATE]")
    )
    assert "route_verified=true" in diagnostics
    assert all(value not in diagnostics for value in (target_url, actor, body, str(profile)))


def test_detail_dom_replacement_does_not_mix_root_and_body_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/dom-replacement"
    body = "Hiring a synthetic target through a DOM hydration fixture."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
  <a href="/@candidate/post/dom-replacement"><time>now</time></a>
  <a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    detail_html = f"""<!doctype html><html><body><main>
<article id="detail-root">
  <a href="/@candidate/post/dom-replacement"><time>now</time></a>
  <a href="/@candidate">candidate</a><p>{body}</p>
</article>
</main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    detail_requests = 0
    root_replacements = 0
    original_read_detail_body = ThreadsHomeReader._read_detail_body
    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 1_500)
    monkeypatch.setattr(reader_module, "_DETAIL_TIMEOUT_MS", 1_000)

    async def replace_root_before_body(self, root, fingerprint):
        nonlocal root_replacements
        if root_replacements == 0:
            root_replacements += 1
            await self._page.evaluate(
                """() => {
                    const oldRoot = document.querySelector("#detail-root");
                    const replacement = document.createElement("article");
                    replacement.id = "detail-root";
                    replacement.innerHTML =
                      '<a href="/@different"><span>different author</span></a>' +
                      '<p>Hiring a synthetic target through a DOM hydration fixture.</p>';
                    oldRoot.replaceWith(replacement);
                }"""
            )
        return await original_read_detail_body(self, root, fingerprint)

    monkeypatch.setattr(ThreadsHomeReader, "_read_detail_body", replace_root_before_body)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            await route.fulfill(status=200, content_type="text/html", body=detail_html)

        await page.route(target_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=0,
            terms=("hiring",),
            detail=True,
        )
        assert result.status == "DETAIL_RETRY_PENDING"
        assert result.detail_reads == 0
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)
    policy = policy_fingerprint(canonical_terms(("hiring",)))
    checkpoint = store.load(account.account_id, policy)
    assert root_replacements == 1
    assert detail_requests == 1
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 0
    assert len(checkpoint.pending_read_fingerprints) == 1
    output = capsys.readouterr().out
    assert "[BACK] post=1 status=RESTORED anchor=IDENTITY_VERIFIED" in output
    assert "DETAIL_VERIFIED status=PROVENANCE_VERIFIED" not in output
    detail_states = [line for line in output.splitlines() if line.startswith("[DETAIL_STATE]")]
    assert any("terminal_reason=AUTHOR_CONFLICT" in line for line in detail_states)
