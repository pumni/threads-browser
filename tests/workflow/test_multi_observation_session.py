from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.async_api import Route

from tests.fixtures.browser import (
    SyntheticPost,
    _offline_playwright_manager,
    _post_fingerprint,
    _seed_pending_checkpoint,
    create_local_account,
    synthetic_home_html,
)
from threads_browser import reader as reader_module
from threads_browser import workflow
from threads_browser.nurture.session_state import SessionProgress
from threads_browser.nurture.state import DedupeLedger
from threads_browser.reader import HOME_URL, UiReadError
from threads_browser.storage import BrowserDataError, CheckpointStore
from threads_browser.workflow import policy_fingerprint


async def _install_resume_segment(
    reader: reader_module.ThreadsHomeReader,
    *,
    gap_slug: str,
    gap_body: str,
    target_slug: str,
    target_body: str,
    resolve_gap_after_scroll: bool,
) -> None:
    await reader._page.evaluate(
        """({gapSlug, gapBody, targetSlug, targetBody, resolveGapAfterScroll}) => {
          const main = document.querySelector('main');
          main.replaceChildren();
          const spacer = document.createElement('div');
          spacer.style.height = '3600px';
          main.append(spacer);
          const addCard = (slug, body, style, bodyStyle) => {
            const article = document.createElement('article');
            article.style.cssText = style;
            const permalink = document.createElement('a');
            permalink.href = `/@candidate/post/${slug}`;
            const time = document.createElement('time');
            time.textContent = 'now';
            permalink.append(time);
            const author = document.createElement('a');
            author.href = '/@candidate';
            author.textContent = 'candidate';
            const text = document.createElement('p');
            text.style.cssText = bodyStyle;
            text.textContent = body;
            article.append(permalink, author, text);
            main.append(article);
          };
          const scrollY = window.scrollY;
          if (resolveGapAfterScroll) {
            addCard(
              gapSlug,
              gapBody,
              `position:absolute;top:${scrollY + 580}px;left:20px;width:520px;height:260px`,
              'position:absolute;top:80px;left:12px;width:400px;margin:0;font:16px/24px monospace;overflow-wrap:anywhere',
            );
            addCard(
              targetSlug,
              targetBody,
              `position:absolute;top:${scrollY + 920}px;left:20px;width:520px;height:260px`,
              'position:absolute;top:80px;left:12px;width:400px;margin:0;font:16px/24px monospace',
            );
          } else {
            addCard(
              gapSlug,
              gapBody,
              'position:fixed;top:70px;left:20px;width:520px;height:300px',
              'position:absolute;top:80px;left:12px;width:220px;height:24px;margin:0;overflow:hidden;font:16px/24px monospace',
            );
            addCard(
              targetSlug,
              targetBody,
              'position:fixed;top:410px;left:20px;width:520px;height:220px',
              'position:absolute;top:80px;left:12px;width:400px;margin:0;font:16px/24px monospace',
            );
          }
        }""",
        {
            "gapSlug": gap_slug,
            "gapBody": gap_body,
            "targetSlug": target_slug,
            "targetBody": target_body,
            "resolveGapAfterScroll": resolve_gap_after_scroll,
        },
    )


def _track_scan_states(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str | None, str]]:
    observations: list[tuple[str | None, str]] = []
    original_scan = reader_module.ThreadsHomeReader.scan_viewport

    async def tracked_scan(self, *args, **kwargs):
        targets = await original_scan(self, *args, **kwargs)
        observations.extend(
            (diagnostic.identity_fingerprint, diagnostic.state)
            for diagnostic in self.last_candidate_diagnostics
        )
        return targets

    monkeypatch.setattr(reader_module.ThreadsHomeReader, "scan_viewport", tracked_scan)
    return observations


def test_session_reads_two_different_details_after_back_in_one_headed_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    data_root = tmp_path / "data"
    account = create_local_account(data_root, actor=actor)
    store = CheckpointStore(data_root)
    _, stale_pending, _ = _seed_pending_checkpoint(
        store,
        account,
        ("hiring",),
        "https://www.threads.com/@candidate/post/stale-session-entry",
    )
    posts = tuple(
        SyntheticPost(
            slug=f"session-{index}",
            body=(
                f"Synthetic career discussion {index}."
                if index < 3
                else f"Hiring synthetic role {index}."
            ),
            row_id=f"session-row-{index}",
        )
        for index in range(1, 5)
    )
    home_html = synthetic_home_html(actor, posts).replace(
        "<article",
        '<article style="min-height: 560px; box-sizing: border-box"',
    )
    home_html = home_html.replace(
        "</body>",
        "<script>window.wheelCount=0;addEventListener('wheel',"
        "()=>window.wheelCount++)</script></body>",
    )
    details = {
        f"https://www.threads.com/@candidate/post/{post.slug}": (
            "<!doctype html><html><body><main><article>"
            f'<a href="/@candidate/post/{post.slug}"><time>now</time></a>'
            '<a href="/@candidate">candidate</a>'
            f"<p>{post.body}</p></article></main></body></html>"
        )
        for post in posts
    }
    detail_visits: list[str] = []
    launch_count = 0
    home_open_count = 0
    restored_scroll_positions: list[int] = []

    original_open_home = reader_module.ThreadsHomeReader.open_home
    original_return_to_home = reader_module.ThreadsHomeReader.return_to_home

    async def tracked_open_home(self) -> None:
        nonlocal home_open_count
        home_open_count += 1
        await original_open_home(self)

    async def tracked_return_to_home(self) -> None:
        await original_return_to_home(self)
        restored_scroll_positions.append(await self._page.evaluate("window.scrollY"))

    monkeypatch.setattr(reader_module.ThreadsHomeReader, "open_home", tracked_open_home)
    monkeypatch.setattr(
        reader_module.ThreadsHomeReader,
        "return_to_home",
        tracked_return_to_home,
    )

    async def respond(route: Route) -> None:
        url = route.request.url
        if url == HOME_URL:
            body = home_html
        elif url in details:
            detail_visits.append(url)
            lock = store.profile_lock(account)
            with pytest.raises(BrowserDataError) as busy:
                lock.acquire()
            assert busy.value.code == "ACCOUNT_BUSY"
            body = details[url]
        elif url == f"https://www.threads.com/@{actor}":
            body = "<!doctype html><html><body><h1>Local profile</h1></body></html>"
        else:
            await route.abort()
            return
        await route.fulfill(status=200, content_type="text/html", body=body)

    def record_launch() -> None:
        nonlocal launch_count
        launch_count += 1

    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 1_500)
    monkeypatch.setattr(reader_module, "_DETAIL_TIMEOUT_MS", 1_500)
    monkeypatch.setattr(
        workflow,
        "async_playwright",
        lambda: _offline_playwright_manager(
            respond,
            headless=False,
            on_launch=record_launch,
        ),
    )

    result = asyncio.run(
        workflow.run_browser_nurture_session(
            account,
            store,
            max_posts=10,
            terms=("hiring",),
            max_scrolls=8,
            max_total_posts=20,
            max_detail_reads=2,
            max_runtime_seconds=60,
        )
    )

    checkpoint = store.load(account.account_id, policy_fingerprint(("hiring",)))
    assert launch_count == 1
    assert home_open_count == 1
    assert len(detail_visits) == 2
    assert len(set(detail_visits)) == 2
    assert len(restored_scroll_positions) == 2
    assert restored_scroll_positions[0] > 0
    assert restored_scroll_positions == sorted(restored_scroll_positions)
    assert result.detail_reads == 2
    assert result.scroll_attempts > 1
    assert result.status == "SESSION_BUDGET_EXHAUSTED"
    assert result.stop_reason == "MAX_DETAIL_READS"
    assert result.checkpoint_saved is True
    assert result.scroll_attempts <= 8
    assert result.checkpoint_runs == checkpoint.completed_runs == 2
    assert checkpoint.detail_reads == 2
    assert stale_pending in checkpoint.pending_read_fingerprints
    with store.profile_lock(account):
        pass


def test_session_stops_before_detail_when_resumed_feed_has_read_in_progress_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    data_root = tmp_path / "data"
    account = create_local_account(data_root, actor=actor)
    store = CheckpointStore(data_root)
    posts = tuple(
        SyntheticPost(
            slug=f"coverage-stop-{index}",
            body=(
                f"Synthetic career discussion {index}."
                if index < 3
                else f"Hiring synthetic role {index}."
            ),
        )
        for index in range(1, 5)
    )
    home_html = synthetic_home_html(actor, posts).replace(
        "<article",
        '<article style="min-height: 560px; box-sizing: border-box"',
    )
    details = {
        f"https://www.threads.com/@candidate/post/{post.slug}": (
            "<!doctype html><html><body><main><article>"
            f'<a href="/@candidate/post/{post.slug}"><time>now</time></a>'
            '<a href="/@candidate">candidate</a>'
            f"<p>{post.body}</p></article></main></body></html>"
        )
        for post in posts
    }
    gap_slug = "resumed-read-in-progress"
    target_slug = "resumed-actionable-target"
    gap_url = f"https://www.threads.com/@candidate/post/{gap_slug}"
    target_url = f"https://www.threads.com/@candidate/post/{target_slug}"
    gap_body = "Synthetic weather text remains unread. " * 40
    target_body = "Hiring synthetic role in the resumed feed."
    detail_visits: list[str] = []
    back_count = 0
    launch_count = 0
    closed_pages: list[bool] = []

    original_return_to_home = reader_module.ThreadsHomeReader.return_to_home
    original_page_workflow = workflow._run_page_workflow_session_locked

    async def install_gap_after_first_back(self) -> None:
        nonlocal back_count
        await original_return_to_home(self)
        back_count += 1
        if back_count == 1:
            await _install_resume_segment(
                self,
                gap_slug=gap_slug,
                gap_body=gap_body,
                target_slug=target_slug,
                target_body=target_body,
                resolve_gap_after_scroll=False,
            )

    async def track_page_close(page, *args, **kwargs):
        page.on("close", lambda: closed_pages.append(True))
        return await original_page_workflow(page, *args, **kwargs)

    async def respond(route: Route) -> None:
        url = route.request.url
        if url == HOME_URL:
            body = home_html
        elif url in details:
            detail_visits.append(url)
            body = details[url]
        elif url == f"https://www.threads.com/@{actor}":
            body = "<!doctype html><html><body><h1>Local profile</h1></body></html>"
        else:
            await route.abort()
            return
        await route.fulfill(status=200, content_type="text/html", body=body)

    def record_launch() -> None:
        nonlocal launch_count
        launch_count += 1

    monkeypatch.setattr(
        reader_module.ThreadsHomeReader,
        "return_to_home",
        install_gap_after_first_back,
    )
    monkeypatch.setattr(workflow, "_run_page_workflow_session_locked", track_page_close)
    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 1_500)
    monkeypatch.setattr(reader_module, "_DETAIL_TIMEOUT_MS", 1_500)
    monkeypatch.setattr(
        workflow,
        "async_playwright",
        lambda: _offline_playwright_manager(
            respond,
            headless=False,
            on_launch=record_launch,
        ),
    )

    result = asyncio.run(
        workflow.run_browser_nurture_session(
            account,
            store,
            max_posts=10,
            terms=("hiring",),
            max_scrolls=8,
            max_total_posts=20,
            max_detail_reads=2,
            max_runtime_seconds=60,
        )
    )

    checkpoint = store.load(account.account_id, policy_fingerprint(("hiring",)))
    gap_fingerprint = _post_fingerprint(gap_url)
    target_fingerprint = _post_fingerprint(target_url)
    assert launch_count == 1
    assert back_count == 1
    assert len(detail_visits) == 1
    assert detail_visits[0] in details
    assert result.status == "SESSION_COVERAGE_INCOMPLETE"
    assert result.stop_reason == "COVERAGE_INCOMPLETE"
    assert result.detail_reads == 1
    assert result.read_in_progress == result.unresolved_gaps == 1
    assert not result.viewport_coverage_complete
    assert result.checkpoint_saved is True
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 1
    assert gap_fingerprint not in checkpoint.seen_fingerprints
    assert target_fingerprint not in checkpoint.seen_fingerprints
    assert target_fingerprint not in checkpoint.pending_read_fingerprints
    assert closed_pages == [True]
    with store.profile_lock(account):
        pass


def test_session_continues_after_a_permitted_scroll_completes_partial_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    data_root = tmp_path / "data"
    account = create_local_account(data_root, actor=actor)
    store = CheckpointStore(data_root)
    gap_slug = "scroll-resolved-coverage-gap"
    target_slug = "target-after-coverage-scroll"
    gap_body = "Synthetic weather text fragment. " * 8
    target_body = "Hiring synthetic role after permitted scroll."
    posts = (
        SyntheticPost(slug="scroll-irrelevant-1", body="Synthetic career discussion."),
        SyntheticPost(slug=gap_slug, body=gap_body, row_id="partial-gap-row"),
        SyntheticPost(slug=target_slug, body=target_body),
    )
    home_html = synthetic_home_html(actor, posts).replace(
        "<body>",
        "<body><style>"
        "nav{height:36px}main{display:block}article{position:relative;height:200px}"
        "article>p{position:absolute;top:108px;left:12px;width:400px;margin:0;line-height:24px}"
        "#partial-gap-row>p{top:80px}"
        "</style>",
    )
    details = {
        f"https://www.threads.com/@candidate/post/{post.slug}": (
            "<!doctype html><html><body><main><article>"
            f'<a href="/@candidate/post/{post.slug}"><time>now</time></a>'
            '<a href="/@candidate">candidate</a>'
            f"<p>{post.body}</p></article></main></body></html>"
        )
        for post in posts
    }
    gap_url = f"https://www.threads.com/@candidate/post/{gap_slug}"
    target_url = f"https://www.threads.com/@candidate/post/{target_slug}"
    detail_visits: list[str] = []
    scan_states = _track_scan_states(monkeypatch)
    original_page_workflow = workflow._run_page_workflow_session_locked

    async def use_short_headed_viewport(page, *args, **kwargs):
        await page.set_viewport_size({"width": 1280, "height": 360})
        return await original_page_workflow(page, *args, **kwargs)

    async def respond(route: Route) -> None:
        url = route.request.url
        if url == HOME_URL:
            body = home_html
        elif url in details:
            detail_visits.append(url)
            body = details[url]
        elif url == f"https://www.threads.com/@{actor}":
            body = "<!doctype html><html><body><h1>Local profile</h1></body></html>"
        else:
            await route.abort()
            return
        await route.fulfill(status=200, content_type="text/html", body=body)

    monkeypatch.setattr(
        reader_module, "_READINESS_TIMEOUT_MS", 1_500
    )
    monkeypatch.setattr(reader_module, "_DETAIL_TIMEOUT_MS", 1_500)
    monkeypatch.setattr(workflow, "_run_page_workflow_session_locked", use_short_headed_viewport)
    monkeypatch.setattr(
        workflow,
        "async_playwright",
        lambda: _offline_playwright_manager(respond, headless=False),
    )

    result = asyncio.run(
        workflow.run_browser_nurture_session(
            account,
            store,
            max_posts=10,
            terms=("hiring",),
            max_scrolls=8,
            max_total_posts=20,
            max_detail_reads=1,
            max_runtime_seconds=60,
        )
    )

    checkpoint = store.load(account.account_id, policy_fingerprint(("hiring",)))
    gap_fingerprint = _post_fingerprint(gap_url)
    gap_states = [state for fingerprint, state in scan_states if fingerprint == gap_fingerprint]
    assert len(detail_visits) == 1
    assert detail_visits[0] == target_url
    assert result.detail_reads == 1
    assert result.status == "SESSION_BUDGET_EXHAUSTED"
    assert result.stop_reason == "MAX_DETAIL_READS"
    assert result.viewport_coverage_complete
    assert result.unresolved_gaps == result.read_in_progress == 0
    assert result.scrolls >= 2
    assert "PARTIAL_BODY" in gap_states
    assert "VERIFIED" in gap_states
    assert result.checkpoint_saved is True
    assert checkpoint.detail_reads == 1
    assert _post_fingerprint(target_url) in checkpoint.seen_fingerprints
    with store.profile_lock(account):
        pass


def test_wrong_detail_identity_in_later_segment_preserves_prior_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    data_root = tmp_path / "data"
    account = create_local_account(data_root, actor=actor)
    store = CheckpointStore(data_root)
    posts = tuple(
        SyntheticPost(
            slug=f"identity-later-{index}",
            body=(
                f"Synthetic career discussion {index}."
                if index < 3
                else f"Hiring synthetic role {index}."
            ),
        )
        for index in range(1, 5)
    )
    home_html = synthetic_home_html(actor, posts).replace(
        "<article",
        '<article style="min-height: 560px; box-sizing: border-box"',
    )
    detail_visits: list[str] = []
    prior_checkpoints = []
    fingerprint = policy_fingerprint(("hiring",))

    async def respond(route: Route) -> None:
        url = route.request.url
        if url == HOME_URL:
            body = home_html
        elif url.startswith("https://www.threads.com/@candidate/post/"):
            detail_visits.append(url)
            if len(detail_visits) == 2:
                prior_checkpoints.append(store.load(account.account_id, fingerprint))
            selected_slug = url.rsplit("/", maxsplit=1)[-1]
            selected = next(post for post in posts if post.slug == selected_slug)
            detail_slug = selected_slug if len(detail_visits) == 1 else "wrong-later-identity"
            body = (
                "<!doctype html><html><body><main><article>"
                f'<a href="/@candidate/post/{detail_slug}"><time>now</time></a>'
                '<a href="/@candidate">candidate</a>'
                f"<p>{selected.body}</p></article></main></body></html>"
            )
        elif url == f"https://www.threads.com/@{actor}":
            body = "<!doctype html><html><body><h1>Local profile</h1></body></html>"
        else:
            await route.abort()
            return
        await route.fulfill(status=200, content_type="text/html", body=body)

    monkeypatch.setattr(reader_module, "_READINESS_TIMEOUT_MS", 1_500)
    monkeypatch.setattr(reader_module, "_DETAIL_TIMEOUT_MS", 1_500)
    monkeypatch.setattr(
        workflow,
        "async_playwright",
        lambda: _offline_playwright_manager(respond, headless=False),
    )

    result = asyncio.run(
        workflow.run_browser_nurture_session(
            account,
            store,
            max_posts=10,
            terms=("hiring",),
            max_scrolls=8,
            max_total_posts=20,
            max_detail_reads=4,
            max_runtime_seconds=60,
        )
    )

    checkpoint = store.load(account.account_id, policy_fingerprint(("hiring",)))
    assert len(detail_visits) == 2
    assert len(set(detail_visits)) == 2
    assert len(prior_checkpoints) == 1
    assert result.status == "DETAIL_RETRY_PENDING"
    assert result.detail_reads == 1
    first_fingerprint = _post_fingerprint(detail_visits[0])
    assert first_fingerprint in checkpoint.seen_fingerprints
    assert checkpoint == prior_checkpoints[0]
    assert checkpoint.completed_runs == 1
    assert checkpoint.detail_reads == 1
    assert result.checkpoint_saved is True
    assert result.checkpoint_runs == checkpoint.completed_runs
    with store.profile_lock(account):
        pass


def test_session_respects_zero_scroll_budget_in_a_single_browser_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    data_root = tmp_path / "data"
    account = create_local_account(data_root, actor=actor)
    store = CheckpointStore(data_root)
    post = SyntheticPost(slug="no-scroll", body="Hiring synthetic role.")
    detail_url = f"https://www.threads.com/@candidate/post/{post.slug}"
    home_html = synthetic_home_html(actor, (post,))
    detail_html = (
        "<!doctype html><html><body><main><article>"
        f'<a href="/@candidate/post/{post.slug}"><time>now</time></a>'
        '<a href="/@candidate">candidate</a>'
        f"<p>{post.body}</p></article></main></body></html>"
    )

    async def respond(route: Route) -> None:
        url = route.request.url
        if url == HOME_URL:
            body = home_html
        elif url == detail_url:
            body = detail_html
        elif url == f"https://www.threads.com/@{actor}":
            body = "<!doctype html><html><body><h1>Local profile</h1></body></html>"
        else:
            await route.abort()
            return
        await route.fulfill(status=200, content_type="text/html", body=body)

    monkeypatch.setattr(
        reader_module,
        "_READINESS_TIMEOUT_MS",
        1_500,
    )
    monkeypatch.setattr(
        reader_module,
        "_DETAIL_TIMEOUT_MS",
        1_500,
    )
    monkeypatch.setattr(
        workflow,
        "async_playwright",
        lambda: _offline_playwright_manager(respond, headless=False),
    )

    result = asyncio.run(
        workflow.run_browser_nurture_session(
            account,
            store,
            max_posts=5,
            terms=("hiring",),
            max_scrolls=0,
            max_total_posts=2,
            max_detail_reads=1,
            max_runtime_seconds=60,
        )
    )

    assert result.detail_reads == 1
    assert result.scroll_attempts == result.scrolls == 0
    assert result.checkpoint_saved is True
    assert result.stop_reason == "MAX_DETAIL_READS"


def test_wrong_actor_stops_session_before_checkpointing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = "offline.operator"
    data_root = tmp_path / "data"
    account = create_local_account(data_root, actor=actor)
    store = CheckpointStore(data_root)
    home_html = synthetic_home_html(actor, ()).replace(
        f'href="/@{actor}"', 'href="/@wrong.operator"'
    )

    async def respond(route: Route) -> None:
        if route.request.url == HOME_URL:
            body = home_html
        elif route.request.url == "https://www.threads.com/@wrong.operator":
            body = "<!doctype html><html><body><h1>Local profile</h1></body></html>"
        else:
            await route.abort()
            return
        await route.fulfill(status=200, content_type="text/html", body=body)

    monkeypatch.setattr(
        workflow,
        "async_playwright",
        lambda: _offline_playwright_manager(respond, headless=False),
    )

    with pytest.raises(UiReadError, match="ACTOR_MISMATCH"):
        asyncio.run(
            workflow.run_browser_nurture_session(
                account,
                store,
                max_posts=5,
                terms=("hiring",),
                max_scrolls=1,
                max_total_posts=2,
                max_detail_reads=1,
                max_runtime_seconds=60,
            )
        )

    checkpoint = store.load(account.account_id, policy_fingerprint(("hiring",)))
    assert checkpoint.completed_runs == 0
    assert checkpoint.seen_fingerprints == ()
    with store.profile_lock(account):
        pass


def test_session_timeout_closes_browser_and_releases_profile_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = create_local_account(tmp_path / "data")
    store = CheckpointStore(tmp_path / "data")
    lifecycle = {"closed": False, "manager_exited": False}

    class Context:
        def __init__(self) -> None:
            self.pages = [object()]

        async def close(self) -> None:
            lifecycle["closed"] = True

    context = Context()

    class Chromium:
        async def launch_persistent_context(self, **_kwargs):
            return context

    class Manager:
        async def __aenter__(self):
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *_args) -> None:
            lifecycle["manager_exited"] = True

    async def wait_forever(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(workflow, "async_playwright", Manager)
    monkeypatch.setattr(workflow, "_run_page_workflow_session_locked", wait_forever)

    with pytest.raises(UiReadError, match="SESSION_TIMEOUT"):
        asyncio.run(
            workflow.run_browser_nurture_session(
                account,
                store,
                max_posts=1,
                terms=("hiring",),
                max_scrolls=1,
                max_total_posts=1,
                max_detail_reads=1,
                max_runtime_seconds=10.1,
            )
        )

    assert lifecycle == {"closed": True, "manager_exited": True}
    checkpoint = store.load(account.account_id, policy_fingerprint(("hiring",)))
    assert checkpoint.completed_runs == 0
    assert checkpoint.seen_fingerprints == ()
    with store.profile_lock(account):
        pass


def test_session_progress_refuses_to_commit_read_in_progress_coverage(
    tmp_path: Path,
) -> None:
    account = create_local_account(tmp_path / "data")
    store = CheckpointStore(tmp_path / "data")
    fingerprint = policy_fingerprint(("hiring",))
    progress = SessionProgress(
        checkpoint=store.load(account.account_id, fingerprint),
        checkpoint_store=store,
        account_id=account.account_id,
        policy_fingerprint=fingerprint,
        dedupe=DedupeLedger.from_checkpoint((), ()),
    )
    progress.coverage.read_in_progress.add("synthetic-fingerprint")
    context = SimpleNamespace(
        stats=SimpleNamespace(viewport_coverage_complete=False),
    )

    with pytest.raises(ValueError, match="incomplete session observation"):
        progress.commit(
            stats=context.stats,
            reader=object(),
            state=object(),
            detail_reads=0,
        )

    assert not progress.checkpoint_saved
    assert store.load(account.account_id, fingerprint).completed_runs == 0
