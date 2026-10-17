from __future__ import annotations

from pathlib import Path

import pytest
from playwright.async_api import Route

from tests.fixtures.browser import (
    _post_fingerprint,
    _run_offline_page,
    _run_page_workflow_for_test,
    _seed_pending_checkpoint,
)
from threads_browser import reader as reader_module
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


def test_empty_detail_is_reported_unavailable_and_home_progress_is_saved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/stable-empty"
    body = "Hiring a synthetic target body."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><a href="/@candidate/post/stable-empty"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p></article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    detail_requests = 0
    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 1_500)
    monkeypatch.setattr(reader_module, "_DETAIL_TIMEOUT_MS", 1_000)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=(
                    '<!doctype html><html><body><main style="min-height: 1px"></main></body></html>'
                ),
            )

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
        assert detail_requests == 1

    _run_offline_page(feed_html, exercise)
    policy = policy_fingerprint(canonical_terms(("hiring",)))
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 0
    assert len(checkpoint.pending_read_fingerprints) == 1
    output = capsys.readouterr().out
    assert "terminal_reason=RENDER_UNAVAILABLE" in output
    assert "status=UNAVAILABLE code=DETAIL_RENDER_UNAVAILABLE retry=PENDING" in output
    assert "WAITING stage=DETAIL retry=1" in output
    diagnostics = "\n".join(
        line for line in output.splitlines() if line.startswith("[DETAIL_STATE]")
    )
    assert all(value not in diagnostics for value in (target_url, actor, body, str(profile)))


def test_stale_pending_allows_one_verified_fallback_and_saves_remaining_pending(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    stale_url = "https://www.threads.com/@candidate/post/absent-pending"
    fallback_url = "https://www.threads.com/@candidate/post/fresh-fallback"
    body = "A synthetic weather update."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><a href="/@candidate/post/fresh-fallback"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p></article></main></body></html>"""
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    policy, stale_fingerprint, _ = _seed_pending_checkpoint(
        store,
        account,
        ("hiring",),
        stale_url,
    )
    detail_attempts = 0

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_attempts
            detail_attempts += 1
            await route.fulfill(status=200, content_type="text/html", body=detail_html)

        await page.route(fallback_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=0,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE"
        assert result.detail_reads == 1
        assert result.checkpoint_saved
        assert result.checkpoint_runs == 2
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)
    checkpoint = store.load(account.account_id, policy)
    fallback_fingerprint = _post_fingerprint(fallback_url)
    assert checkpoint.schema_version == 3
    assert checkpoint.completed_runs == 2
    assert checkpoint.detail_reads == 1
    assert checkpoint.pending_read_fingerprints == (stale_fingerprint,)
    assert fallback_fingerprint in checkpoint.seen_fingerprints
    assert fallback_fingerprint not in checkpoint.pending_read_fingerprints
    assert detail_attempts == 1
    output = capsys.readouterr().out
    assert all(
        value not in output for value in (stale_url, fallback_url, actor, body, str(profile))
    )


def test_stale_pending_detail_failure_keeps_both_entries_for_retry(
    tmp_path: Path,
) -> None:
    actor = "offline.operator"
    stale_url = "https://www.threads.com/@candidate/post/absent-pending-failure"
    fallback_url = "https://www.threads.com/@candidate/post/failing-fallback"
    body = "A synthetic weather update."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><a href="/@candidate/post/failing-fallback"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p></article></main></body></html>"""
    bad_detail_html = """<!doctype html><html><body><main><article>
<a href="/@candidate">candidate</a><p>A different synthetic update.</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    policy, stale_fingerprint, _ = _seed_pending_checkpoint(
        store,
        account,
        ("hiring",),
        stale_url,
    )
    detail_attempts = 0

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_attempts
            detail_attempts += 1
            await route.fulfill(status=200, content_type="text/html", body=bad_detail_html)

        await page.route(fallback_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=1,
            max_scrolls=0,
            terms=("hiring",),
        )
        assert result.status == "DETAIL_RETRY_PENDING"
        assert result.detail_reads == 0
        assert result.checkpoint_saved
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)
    checkpoint = store.load(account.account_id, policy)
    fallback_fingerprint = _post_fingerprint(fallback_url)
    assert checkpoint.schema_version == 3
    assert checkpoint.completed_runs == 2
    assert checkpoint.detail_reads == 0
    assert checkpoint.pending_read_fingerprints == (stale_fingerprint, fallback_fingerprint)
    assert detail_attempts == 1


def test_current_pending_beats_a_fresh_fallback(
    tmp_path: Path,
) -> None:
    actor = "offline.operator"
    pending_url = "https://www.threads.com/@candidate/post/current-pending"
    fresh_url = "https://www.threads.com/@candidate/post/other-fallback"
    pending_body = "A pending synthetic weather update."
    fresh_body = "A fresh synthetic weather update."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav><main>
<article><a href="/@candidate/post/current-pending"><time>2h</time></a>
<a href="/@candidate">candidate</a><p>{pending_body}</p></article>
<article><a href="/@candidate/post/other-fallback"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{fresh_body}</p></article>
</main></body></html>"""
    pending_detail = f"""<!doctype html><html><body><main><article>
<a href="/@candidate">candidate</a><p>{pending_body}</p>
</article></main></body></html>"""
    fresh_detail = f"""<!doctype html><html><body><main><article>
<a href="/@candidate">candidate</a><p>{fresh_body}</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    policy, pending_fingerprint, _ = _seed_pending_checkpoint(
        store,
        account,
        ("hiring",),
        pending_url,
    )
    detail_attempts = {pending_url: 0, fresh_url: 0}

    async def exercise(page) -> None:
        async def serve(url: str, html: str, route: Route) -> None:
            detail_attempts[url] += 1
            await route.fulfill(status=200, content_type="text/html", body=html)

        await page.route(
            pending_url,
            lambda route: serve(pending_url, pending_detail, route),
        )
        await page.route(
            fresh_url,
            lambda route: serve(fresh_url, fresh_detail, route),
        )
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=0,
            terms=("hiring",),
        )
        assert result.status == "COMPLETE"
        assert result.detail_reads == 1
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)
    checkpoint = store.load(account.account_id, policy)
    assert detail_attempts == {pending_url: 1, fresh_url: 0}
    assert checkpoint.pending_read_fingerprints == ()
    assert pending_fingerprint not in checkpoint.pending_read_fingerprints


def test_stale_pending_does_not_reopen_completed_posts_or_navigate_without_actionability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    stale_url = "https://www.threads.com/@candidate/post/absent-pending-actionability"
    completed_url = "https://www.threads.com/@candidate/post/already-completed"
    fresh_url = "https://www.threads.com/@candidate/post/not-actionable"
    completed_body = "A completed synthetic weather update."
    fresh_body = "A fresh synthetic weather update."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav><main>
<article><a href="/@candidate/post/already-completed"><time>2h</time></a>
<a href="/@candidate">candidate</a><p>{completed_body}</p></article>
<article><a href="/@candidate/post/not-actionable"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{fresh_body}</p></article>
</main></body></html>"""
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate">candidate</a><p>{fresh_body}</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    policy, stale_fingerprint, completed_fingerprints = _seed_pending_checkpoint(
        store,
        account,
        ("hiring",),
        stale_url,
        completed_urls=(completed_url,),
    )
    actionability_checks: list[str] = []
    detail_attempts = 0

    async def reject_fresh_target(self, target):
        actionability_checks.append(target.fingerprint)
        raise UiReadError("DETAIL_NAVIGATION_UNVERIFIED")

    monkeypatch.setattr(ThreadsHomeReader, "actionable_now", reject_fresh_target)

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_attempts
            detail_attempts += 1
            await route.fulfill(status=200, content_type="text/html", body=detail_html)

        await page.route(fresh_url, serve_detail)
        for _ in range(2):
            result = await _run_page_workflow_for_test(
                page,
                account,
                store,
                max_posts=2,
                max_scrolls=0,
                terms=("hiring",),
            )
            assert result.status == "NO_ACTIONABLE_TARGET"
            assert result.detail_reads == 0
            assert not result.checkpoint_saved
            assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)
    fresh_fingerprint = _post_fingerprint(fresh_url)
    assert actionability_checks == [fresh_fingerprint, fresh_fingerprint]
    assert detail_attempts == 0
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.completed_runs == 2
    assert checkpoint.detail_reads == 1
    assert checkpoint.pending_read_fingerprints == (stale_fingerprint,)
    assert completed_fingerprints[0] in checkpoint.seen_fingerprints


def test_stale_pending_does_not_mask_incomplete_home_observations(
    tmp_path: Path,
) -> None:
    actor = "offline.operator"
    stale_url = "https://www.threads.com/@candidate/post/absent-pending-incomplete"
    fallback_url = "https://www.threads.com/@candidate/post/clean-fallback"
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav><main>
<article id="unresolved-wrapper">
<a href="/@candidate/post/ambiguous"><time>now</time></a>
<a href="/@candidate/post/related">related link</a>
<a href="/@candidate">candidate</a>
<p>A synthetic weather update with an unresolved post boundary.</p>
</article>
<article><a href="/@candidate/post/clean-fallback"><time>1h</time></a>
<a href="/@candidate">candidate</a><p>A clean synthetic weather update.</p></article>
</main></body></html>"""
    detail_html = """<!doctype html><html><body><main><article>
<a href="/@candidate">candidate</a><p>A clean synthetic weather update.</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    policy, stale_fingerprint, _ = _seed_pending_checkpoint(
        store,
        account,
        ("hiring",),
        stale_url,
    )
    detail_attempts = 0

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_attempts
            detail_attempts += 1
            await route.fulfill(status=200, content_type="text/html", body=detail_html)

        await page.route(fallback_url, serve_detail)
        result = await _run_page_workflow_for_test(
            page,
            account,
            store,
            max_posts=2,
            max_scrolls=0,
            terms=("hiring",),
        )
        assert result.status == "RELEVANT_POST_NOT_CURRENT"
        assert result.detail_reads == 0
        assert not result.checkpoint_saved
        assert not result.viewport_coverage_complete
        assert result.unresolved_gaps > 0
        assert page.url == HOME_URL

    _run_offline_page(feed_html, exercise)
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 0
    assert checkpoint.pending_read_fingerprints == (stale_fingerprint,)
    assert detail_attempts == 0


def test_failed_fallback_retries_and_completed_post_stays_deduped_across_ten_cycles(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/fallback-retry"
    body = "A synthetic weather update."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
<a href="/@candidate/post/fallback-retry"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    valid_detail = f"""<!doctype html><html><body><main><article>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    empty_detail = "<!doctype html><html><body><main style='min-height: 1px'></main></body></html>"
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    detail_attempts = 0

    async def serve_detail(route: Route) -> None:
        nonlocal detail_attempts
        detail_attempts += 1
        await route.fulfill(
            status=200,
            content_type="text/html",
            body=empty_detail if detail_attempts == 1 else valid_detail,
        )

    async def repeated_cycles(page) -> None:
        await page.route(target_url, serve_detail)
        checkpoints = []
        for cycle in range(10):
            result = await _run_page_workflow_for_test(
                page,
                account,
                store,
                max_posts=1,
                max_scrolls=0,
                terms=("hiring",),
                detail=True,
            )
            if cycle == 0:
                assert result.status == "DETAIL_RETRY_PENDING"
                assert result.detail_reads == 0
            elif cycle == 1:
                assert result.status == "COMPLETE"
                assert result.detail_reads == 1
            else:
                assert result.detail_reads == 0
            assert page.url == HOME_URL
            checkpoint = store.load(
                account.account_id,
                policy_fingerprint(canonical_terms(("hiring",))),
            )
            if checkpoints:
                assert checkpoint.completed_runs >= checkpoints[-1].completed_runs
                assert checkpoint.detail_reads >= checkpoints[-1].detail_reads
                assert set(checkpoint.seen_fingerprints).issuperset(
                    checkpoints[-1].seen_fingerprints
                )
            checkpoints.append(checkpoint)
        assert checkpoints[0].completed_runs == 1
        assert checkpoints[0].detail_reads == 0
        assert len(checkpoints[0].pending_read_fingerprints) == 1
        assert checkpoints[1].completed_runs == 2
        assert checkpoints[1].detail_reads == 1
        assert checkpoints[1].pending_read_fingerprints == ()
        assert checkpoints[-1].completed_runs >= checkpoints[1].completed_runs

    _run_offline_page(feed_html, repeated_cycles)
    final_checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert final_checkpoint.completed_runs == 10
    assert final_checkpoint.detail_reads == 1
    assert final_checkpoint.pending_read_fingerprints == ()
    assert detail_attempts == 2
    assert "decision=VERIFIED_FALLBACK" in capsys.readouterr().out


def test_completed_fallback_is_not_opened_again_on_unchanged_feed(
    tmp_path: Path,
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/completed-fallback"
    body = "A synthetic weather update."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><article>
  <a href="/@candidate/post/completed-fallback"><time>now</time></a>
  <a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    detail_html = f"""<!doctype html><html><body><main><article>
<a href="/@candidate">candidate</a><p>{body}</p>
</article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    detail_attempts = 0

    async def serve_detail(route: Route) -> None:
        nonlocal detail_attempts
        detail_attempts += 1
        await route.fulfill(
            status=200,
            content_type="text/html",
            body=detail_html,
        )

    async def first_cycle(page) -> None:
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
        assert result.detail_reads == 1
        assert page.url == HOME_URL

    async def second_cycle(page) -> None:
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
        assert result.detail_reads == 0
        assert page.url == HOME_URL

    _run_offline_page(feed_html, first_cycle)
    _run_offline_page(feed_html, second_cycle)

    checkpoint = store.load(
        account.account_id,
        policy_fingerprint(canonical_terms(("hiring",))),
    )
    assert detail_attempts == 1
    assert checkpoint.completed_runs == 2
    assert checkpoint.detail_reads == 1
    assert checkpoint.pending_read_fingerprints == ()


def test_q_stops_pending_detail_without_back_or_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    actor = "offline.operator"
    target_url = "https://www.threads.com/@candidate/post/pending-identity"
    body = "Hiring a synthetic target body."
    feed_html = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
<a aria-current="page" aria-label="For you" href="/">For you</a>
<a aria-label="Profile" href="/@{actor}">Profile</a></nav>
<main><article><a href="/@candidate/post/pending-identity"><time>now</time></a>
<a href="/@candidate">candidate</a><p>{body}</p></article></main></body></html>"""
    data_root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(data_root).add("offline", actor, profile)
    store = CheckpointStore(data_root)
    readiness_prompts: list[tuple[str, int]] = []
    detail_requests = 0
    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 400)
    monkeypatch.setattr(reader_module, "_DETAIL_TIMEOUT_MS", 1_000)
    monkeypatch.setattr(reader_module, "_RENDER_SETTLE_INTERVAL_MS", 20)

    async def readiness_prompt(stage: str, retry: int) -> bool:
        readiness_prompts.append((stage, retry))
        return False

    async def exercise(page) -> None:
        async def serve_detail(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=(
                    '<!doctype html><html><body><main aria-busy="true" '
                    'style="min-height: 1px">Loading</main></body></html>'
                ),
            )

        await page.route(target_url, serve_detail)
        with pytest.raises(UiReadError) as caught:
            await _run_page_workflow_for_test(
                page,
                account,
                store,
                max_posts=1,
                max_scrolls=0,
                terms=("hiring",),
                detail=True,
                readiness_prompt=readiness_prompt,
            )
        assert caught.value.code == "OPERATOR_CANCELLED"
        assert page.url == target_url
        assert detail_requests == 1

    _run_offline_page(feed_html, exercise)
    policy = policy_fingerprint(canonical_terms(("hiring",)))
    checkpoint = store.load(account.account_id, policy)
    assert checkpoint.completed_runs == checkpoint.detail_reads == 0
    assert readiness_prompts == [("DETAIL", 2)]
    output = capsys.readouterr().out
    assert "hydration_pending=true" in output
    assert "[BACK] post=1 status=REQUESTED" not in output
    assert "[CHECKPOINT] status=SAVED" not in output
    diagnostics = "\n".join(
        line for line in output.splitlines() if line.startswith("[DETAIL_STATE]")
    )
    assert all(value not in diagnostics for value in (target_url, actor, body, str(profile)))
