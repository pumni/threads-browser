from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import Route

from tests.fixtures.browser import (
    SyntheticPost,
    _offline_playwright_manager,
    create_local_account,
    run_offline_pages,
    synthetic_detail_html,
    synthetic_home_html,
)
from threads_browser import workflow
from threads_browser.console_output import ConsoleOutputPolicy
from threads_browser.feed.urls import HOME_URL
from threads_browser.nurture.session import run_page_workflow_session
from threads_browser.nurture.state import auto_policy_fingerprint
from threads_browser.storage import CheckpointStore


def _detail_html(post: SyntheticPost) -> str:
    return (
        "<!doctype html><html><body><main><article>"
        f'<a href="/@candidate/post/{post.slug}"><time>now</time></a>'
        '<a href="/@candidate">candidate</a>'
        f"<p>{post.body}</p></article></main></body></html>"
    )


def test_auto_session_opens_only_current_job_posts_and_prioritizes_visible_match(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    actor = "offline.operator"
    data_root = tmp_path / "data"
    account = create_local_account(data_root, actor=actor)
    store = CheckpointStore(data_root)
    posts = (
        SyntheticPost("hire-post", "We're hiring an engineer for a local team."),
        SyntheticPost("career-post", "Career advice about writing a resume."),
        SyntheticPost("course-post", "A hiring course is open for enrollment."),
        SyntheticPost("closed-post", "We're hiring engineers, but the position is filled."),
        SyntheticPost("seeking-post", "I'm looking for a job in Da Nang."),
    )
    home_html = synthetic_home_html(actor, posts).replace(
        "</body>",
        "<script>window.wheelCount=0;addEventListener('wheel',"
        "()=>window.wheelCount++);document.addEventListener('click',event=>{"
        "const a=event.target.closest('a');if(!a||!a.href.includes('/post/')||"
        "sessionStorage.getItem('wheel-at-first-detail')!==null)return;"
        "sessionStorage.setItem('wheel-at-first-detail',String(window.wheelCount));"
        "})</script></body>",
    )
    details = {
        f"https://www.threads.com/@candidate/post/{post.slug}": _detail_html(post)
        for post in posts
    }
    visited: list[str] = []
    wheel_counts_at_detail: list[int] = []
    original_locked_cycle = workflow._run_page_workflow_session_locked

    async def respond(route: Route) -> None:
        url = route.request.url
        if url == HOME_URL:
            body = home_html
        elif url in details:
            visited.append(url)
            body = details[url]
        elif url == f"https://www.threads.com/@{actor}":
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

    async def inspect_completed_cycle(page, *args, **kwargs):
        result = await original_locked_cycle(page, *args, **kwargs)
        wheel_counts_at_detail.append(
            await page.evaluate(
                "Number(sessionStorage.getItem('wheel-at-first-detail'))"
            )
        )
        return result

    monkeypatch.setattr(
        workflow,
        "_run_page_workflow_session_locked",
        inspect_completed_cycle,
    )

    result = asyncio.run(workflow.run_browser_nurture_auto_cycle(account, store))
    assert result.status == "SESSION_COMPLETE"
    assert result.checkpoint_saved is True
    assert result.detail_reads == 2
    assert result.new_fingerprints

    assert visited == [
        "https://www.threads.com/@candidate/post/hire-post",
        "https://www.threads.com/@candidate/post/seeking-post",
    ]
    assert wheel_counts_at_detail[0] == 0
    checkpoint = store.load(account.account_id, auto_policy_fingerprint())
    assert checkpoint.detail_reads == 2
    raw_checkpoint = (
        data_root
        / "nurture"
        / "checkpoints"
        / f"{account.account_id}-{auto_policy_fingerprint()}.json"
    ).read_text(encoding="utf-8")
    assert all(url not in raw_checkpoint for url in details)
    assert all(post.body not in raw_checkpoint for post in posts)
    assert capsys.readouterr().out == ""


def test_auto_does_not_checkpoint_a_mismatched_detail_body(tmp_path: Path, monkeypatch) -> None:
    actor = "offline.operator"
    data_root = tmp_path / "data"
    account = create_local_account(data_root, actor=actor)
    store = CheckpointStore(data_root)
    post = SyntheticPost("verified-job", "We're hiring an engineer for a local team.")
    home_html = synthetic_home_html(actor, (post,)).replace(
        "</body>",
        "<script>document.addEventListener('click',event=>{const a=event.target.closest('a');"
        "if(!a||!a.href.includes('/post/'))return;"
        "const visits=JSON.parse(sessionStorage.getItem('detail-visits')||'[]');"
        "visits.push(a.href);sessionStorage.setItem('detail-visits',JSON.stringify(visits));"
        "})</script></body>",
    )
    target_url = f"https://www.threads.com/@candidate/post/{post.slug}"

    async def exercise(page) -> None:
        with store.profile_lock(account):
            result = await run_page_workflow_session(
                page,
                account,
                store,
                max_posts=20,
                terms=(),
                max_scrolls=0,
                max_total_posts=20,
                max_detail_reads=1,
                console_output_policy=ConsoleOutputPolicy.AUTO,
                auto_mode=True,
            )
        assert result.status == "DETAIL_RETRY_PENDING"
        assert result.detail_reads == 0
        assert result.checkpoint_saved is False
        assert await page.evaluate(
            "JSON.parse(sessionStorage.getItem('detail-visits') || '[]')"
        ) == [target_url]

    run_offline_pages(
        {
            HOME_URL: home_html,
            target_url: synthetic_detail_html(("A different post body.",)),
        },
        exercise,
    )

    checkpoint = store.load(account.account_id, auto_policy_fingerprint())
    assert checkpoint.completed_runs == 0
    assert checkpoint.detail_reads == 0
