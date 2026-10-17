from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.async_api import Route

from tests.fixtures.browser import (
    SyntheticPost,
    _offline_playwright_manager,
    create_local_account,
    synthetic_home_html,
)
from threads_browser import reader as reader_module
from threads_browser import workflow
from threads_browser.console_output import ConsoleOutputPolicy
from threads_browser.feed.models import PostTarget
from threads_browser.nurture.detail_cycle import DetailBackCoordinator
from threads_browser.nurture.state import DedupeLedger, NurtureRunState, WorkflowPhase
from threads_browser.reader import HOME_URL, UiReadError
from threads_browser.storage import BrowserDataError, CheckpointStore
from threads_browser.workflow import policy_fingerprint


def _run_offline_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    posts: tuple[SyntheticPost, ...],
    *,
    max_detail_reads: int,
) -> tuple[object, object, SimpleNamespace]:
    actor = "offline.operator"
    data_root = tmp_path / "data"
    account = create_local_account(data_root, actor=actor)
    store = CheckpointStore(data_root)
    home_html = synthetic_home_html(actor, posts)
    details = {
        f"https://www.threads.com/@{post.author}/post/{post.slug}": (
            "<!doctype html><html><body><main><article>"
            f'<a href="/@{post.author}/post/{post.slug}"><time>now</time></a>'
            f'<a href="/@{post.author}">{post.author}</a>'
            f"<p>{post.body}</p></article></main></body></html>"
        )
        for post in posts
    }
    telemetry = SimpleNamespace(
        detail_visits=[],
        context_ids=set(),
        profile_lock_held_for_details=[],
        launch_count=0,
        home_open_count=0,
        closed_pages=[],
        profile_lock_released=False,
    )
    original_open_home = reader_module.ThreadsHomeReader.open_home
    original_page_workflow = workflow._run_page_workflow_session_locked

    async def tracked_open_home(self) -> None:
        telemetry.home_open_count += 1
        await original_open_home(self)

    async def track_page_close(page, *args, **kwargs):
        page.on("close", lambda: telemetry.closed_pages.append(True))
        return await original_page_workflow(page, *args, **kwargs)

    async def respond(route: Route) -> None:
        url = route.request.url
        if url == HOME_URL:
            body = home_html
        elif url in details:
            telemetry.detail_visits.append(url)
            telemetry.context_ids.add(id(route.request.frame.page.context))
            lock = store.profile_lock(account)
            try:
                lock.acquire()
            except BrowserDataError as error:
                assert error.code == "ACCOUNT_BUSY"
                telemetry.profile_lock_held_for_details.append(True)
            else:
                lock.release()
                pytest.fail("the profile lock was not held during a Detail visit")
            body = details[url]
        elif url == f"https://www.threads.com/@{actor}":
            body = "<!doctype html><html><body><h1>Local profile</h1></body></html>"
        else:
            await route.abort()
            return
        await route.fulfill(status=200, content_type="text/html", body=body)

    def record_launch() -> None:
        telemetry.launch_count += 1

    monkeypatch.setattr(reader_module.ThreadsHomeReader, "open_home", tracked_open_home)
    monkeypatch.setattr(
        workflow,
        "_run_page_workflow_session_locked",
        track_page_close,
    )
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
            max_scrolls=0,
            max_total_posts=20,
            max_detail_reads=max_detail_reads,
            max_runtime_seconds=60,
        )
    )
    checkpoint = store.load(account.account_id, policy_fingerprint(("hiring",)))
    with store.profile_lock(account):
        telemetry.profile_lock_released = True
    return result, checkpoint, telemetry


def _detail_posts(count: int, *, relevant: bool = False) -> tuple[SyntheticPost, ...]:
    return tuple(
        SyntheticPost(
            slug=f"session-idempotence-{index}",
            body=(
                f"Hiring synthetic role {index}."
                if relevant
                else f"Synthetic weather update {index}."
            ),
        )
        for index in range(1, count + 1)
    )


def _assert_clean_session_shutdown(telemetry: SimpleNamespace, expected_visits: int) -> None:
    assert telemetry.launch_count == 1
    assert telemetry.home_open_count == 1
    assert len(telemetry.context_ids) == 1
    assert telemetry.profile_lock_held_for_details == [True] * expected_visits
    assert telemetry.closed_pages == [True]
    assert telemetry.profile_lock_released is True


def test_detail_coordinator_stops_before_reopening_an_attempted_identity() -> None:
    fingerprint = "synthetic-fingerprint"
    dedupe = DedupeLedger.from_checkpoint((), ())
    dedupe.detail_attempted_this_session.add(fingerprint)
    state = NurtureRunState(phase=WorkflowPhase.OBSERVING)
    reader_calls = 0

    class Reader:
        async def read_detail(self, _target: PostTarget) -> PostTarget:
            nonlocal reader_calls
            reader_calls += 1
            raise AssertionError("an already attempted post reached Detail")

    coordinator = DetailBackCoordinator(
        page=None,
        reader=Reader(),
        state=state,
        dedupe=dedupe,
        console_output_policy=ConsoleOutputPolicy.SCHEDULED,
        detail=False,
        map_playwright_error=lambda *_args, **_kwargs: UiReadError("UI_READ_UNVERIFIED"),
    )
    target = PostTarget(
        url="https://www.threads.com/@candidate/post/synthetic",
        author="candidate",
        body="Synthetic body.",
        fingerprint=fingerprint,
        permalink=None,
    )

    with pytest.raises(UiReadError, match="DETAIL_IDENTITY_ALREADY_ATTEMPTED"):
        asyncio.run(
            coordinator.read_and_return(
                target,
                selected_number=1,
                selected_decision="IRRELEVANT",
            )
        )

    assert reader_calls == 0
    assert state.phase is WorkflowPhase.STOPPED
    assert state.stop_reason == "DETAIL_IDENTITY_ALREADY_ATTEMPTED"


def test_session_reads_distinct_visible_fallback_posts_after_back_at_zero_scroll_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    posts = _detail_posts(2)
    result, checkpoint, telemetry = _run_offline_session(
        tmp_path,
        monkeypatch,
        posts,
        max_detail_reads=5,
    )

    assert telemetry.detail_visits == [
        f"https://www.threads.com/@candidate/post/{post.slug}" for post in posts
    ]
    assert len(set(telemetry.detail_visits)) == 2
    assert result.detail_reads == checkpoint.detail_reads == 2
    assert result.status == "SESSION_BUDGET_EXHAUSTED"
    assert result.stop_reason == "MAX_SCROLLS"
    assert result.scroll_attempts == 0
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == checkpoint.home_new == 2
    assert len(checkpoint.seen_fingerprints) == 2
    assert checkpoint.pending_read_fingerprints == ()
    _assert_clean_session_shutdown(telemetry, expected_visits=2)
    output = capsys.readouterr().out
    assert all(url not in output for url in telemetry.detail_visits)
    assert "DETAIL_IDENTITY_ALREADY_ATTEMPTED" not in output


def test_session_safely_stops_after_the_only_eligible_fallback_was_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    post = _detail_posts(1)[0]
    result, checkpoint, telemetry = _run_offline_session(
        tmp_path,
        monkeypatch,
        (post,),
        max_detail_reads=7,
    )

    assert telemetry.detail_visits == [
        f"https://www.threads.com/@candidate/post/{post.slug}"
    ]
    assert result.detail_reads == checkpoint.detail_reads == 1
    assert result.status == "SESSION_BUDGET_EXHAUSTED"
    assert result.stop_reason == "MAX_SCROLLS"
    assert checkpoint.completed_runs == 1
    assert checkpoint.home_sampled == checkpoint.home_new == 1
    assert len(checkpoint.seen_fingerprints) == 1
    assert checkpoint.pending_read_fingerprints == ()
    _assert_clean_session_shutdown(telemetry, expected_visits=1)
    output = capsys.readouterr().out
    assert all(url not in output for url in telemetry.detail_visits)
    assert "DETAIL_IDENTITY_ALREADY_ATTEMPTED" not in output


def test_session_keeps_a_different_relevant_pending_detail_eligible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posts = _detail_posts(2, relevant=True)
    result, checkpoint, telemetry = _run_offline_session(
        tmp_path,
        monkeypatch,
        posts,
        max_detail_reads=4,
    )

    assert telemetry.detail_visits == [
        f"https://www.threads.com/@candidate/post/{post.slug}" for post in posts
    ]
    assert len(set(telemetry.detail_visits)) == 2
    assert result.detail_reads == checkpoint.detail_reads == 2
    assert result.stop_reason == "MAX_SCROLLS"
    assert checkpoint.pending_read_fingerprints == ()
    _assert_clean_session_shutdown(telemetry, expected_visits=2)
