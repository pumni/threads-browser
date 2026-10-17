from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.async_api import Route

from tests.fixtures.browser import (
    SyntheticPost,
    _offline_playwright_manager,
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
