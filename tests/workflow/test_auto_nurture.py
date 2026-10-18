from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path

import pytest
from playwright.async_api import Route

from tests.fixtures.browser import (
    SyntheticPost,
    _offline_playwright_manager,
    _post_fingerprint,
    create_local_account,
    run_offline_pages,
    synthetic_detail_html,
    synthetic_home_html,
)
from threads_browser import workflow
from threads_browser.console_output import ConsoleOutputPolicy
from threads_browser.feed.job_intent import AUTO_POLICY_VERSION
from threads_browser.feed.urls import HOME_URL
from threads_browser.nurture.session import run_page_workflow_session
from threads_browser.nurture.state import auto_policy_fingerprint, policy_fingerprint
from threads_browser.nurture.uat_diagnostics import AutoUatDiagnostics
from threads_browser.reader import ThreadsHomeReader, UiReadError
from threads_browser.storage import Checkpoint, CheckpointStore


def _detail_html(post: SyntheticPost) -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8"></head><body><main><article>'
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
        SyntheticPost("hire-post", "Bên mình đang tuyển nhân viên kinh doanh tại Hà Nội."),
        SyntheticPost(
            "career-post",
            "Năm ngoái mình đang tìm việc làm ở Đà Nẵng. "
            "Hồi trước bên mình đang tuyển nhân viên kinh doanh.",
        ),
        SyntheticPost(
            "independent-course-clause",
            "Bên mình đang tuyển nhân viên kinh doanh, nhưng khóa học kỹ năng phỏng vấn cũng đang mở. "
            "Đã tuyển đủ người cho vị trí khác.",
        ),
        SyntheticPost(
            "closed-post",
            "Bên mình đang tuyển QA Engineer. Đã tuyển đủ người cho vị trí này.",
        ),
        SyntheticPost(
            "closed-applications",
            "Bên mình đang tuyển nhân viên kinh doanh. Nhưng không nhận hồ sơ nữa.",
        ),
        SyntheticPost("english-seeking", "Looking for a job in Vietnam."),
        SyntheticPost("seeking-post", "Em đang tìm công việc QA Tester tại TP.HCM."),
    )
    hire_url = "https://www.threads.com/@candidate/post/hire-post"
    legacy_auto_policy = sha256(
        b"threads-browser:auto-nurture:job-intent-v1"
    ).hexdigest()
    assert AUTO_POLICY_VERSION == "threads-browser:auto-nurture:job-intent-v2-vi"
    legacy_auto_checkpoint = Checkpoint(
        account.account_id,
        legacy_auto_policy,
        completed_runs=1,
        home_sampled=1,
        home_new=1,
        home_relevant=1,
        seen_fingerprints=(_post_fingerprint(hire_url),),
    )
    store.save(legacy_auto_checkpoint)
    legacy_auto_path = (
        data_root
        / "nurture"
        / "checkpoints"
        / f"{account.account_id}-{legacy_auto_policy}.json"
    )
    legacy_auto_bytes = legacy_auto_path.read_bytes()
    current_auto_policy = auto_policy_fingerprint()
    assert current_auto_policy != legacy_auto_policy
    current_auto_checkpoint = Checkpoint(
        account.account_id,
        current_auto_policy,
        completed_runs=1,
        home_sampled=1,
        home_new=1,
        home_relevant=1,
        seen_fingerprints=(_post_fingerprint(hire_url),),
        pending_read_fingerprints=(_post_fingerprint(hire_url),),
    )
    store.save(current_auto_checkpoint)
    assert store.load(account.account_id, current_auto_policy) == current_auto_checkpoint
    user_terms_policy = policy_fingerprint(("hiring",))
    user_terms_checkpoint = Checkpoint(
        account.account_id,
        user_terms_policy,
        completed_runs=1,
    )
    store.save(user_terms_checkpoint)
    user_terms_path = (
        data_root
        / "nurture"
        / "checkpoints"
        / f"{account.account_id}-{user_terms_policy}.json"
    )
    user_terms_bytes = user_terms_path.read_bytes()
    home_html = synthetic_home_html(actor, posts).replace(
        "<html><body>",
        '<html><head><meta charset="utf-8"></head><body>',
        1,
    ).replace(
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
            visited.append(url.rsplit("/", 1)[-1])
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

    uat_lines: list[str] = []
    uat_diagnostics = AutoUatDiagnostics(emit=uat_lines.append)
    uat_diagnostics.begin_attempt()
    result = asyncio.run(
        workflow.run_browser_nurture_auto_cycle(
            account,
            store,
            uat_diagnostics=uat_diagnostics,
        )
    )
    uat_diagnostics.report_result(result)
    assert result.status == "SESSION_COMPLETE"
    assert result.checkpoint_saved is True
    assert result.detail_reads == 3
    assert result.new_fingerprints

    assert set(visited) == {"seeking-post", "hire-post", "independent-course-clause"}
    assert len(visited) == 3
    assert wheel_counts_at_detail[0] == 0
    uat_output = "\n".join(uat_lines)
    assert "phase=HOME status=READY" in uat_output
    assert "phase=FEED status=OBSERVED viewport=1" in uat_output
    assert "phase=DETAIL status=VERIFIED detail_reads=1" in uat_output
    assert "phase=BACK status=RETURNED" in uat_output
    assert "phase=COVERAGE status=COMPLETE" in uat_output
    assert "phase=CHECKPOINT status=SAVED" in uat_output
    assert actor not in uat_output
    assert all(url not in uat_output for url in details)
    assert all(post.body not in uat_output for post in posts)
    checkpoint = store.load(account.account_id, auto_policy_fingerprint())
    assert checkpoint.detail_reads == 3
    assert checkpoint.completed_runs == 2
    assert checkpoint.pending_read_fingerprints == ()
    assert _post_fingerprint(hire_url) in checkpoint.seen_fingerprints
    assert store.load(account.account_id, legacy_auto_policy) == legacy_auto_checkpoint
    assert legacy_auto_path.read_bytes() == legacy_auto_bytes
    assert store.load(account.account_id, user_terms_policy) == user_terms_checkpoint
    assert user_terms_path.read_bytes() == user_terms_bytes

    resumed_result = asyncio.run(
        workflow.run_browser_nurture_auto_cycle(account, store)
    )
    assert resumed_result.checkpoint_saved is True
    assert resumed_result.detail_reads == 0
    assert resumed_result.home_new == 0
    assert set(visited) == {"seeking-post", "hire-post", "independent-course-clause"}
    assert len(visited) == 3
    resumed_checkpoint = store.load(account.account_id, auto_policy_fingerprint())
    assert resumed_checkpoint.completed_runs == 3
    assert resumed_checkpoint.detail_reads == 3
    assert store.load(account.account_id, legacy_auto_policy) == legacy_auto_checkpoint
    assert legacy_auto_path.read_bytes() == legacy_auto_bytes
    assert store.load(account.account_id, user_terms_policy) == user_terms_checkpoint
    assert user_terms_path.read_bytes() == user_terms_bytes
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
    post = SyntheticPost(
        "verified-job",
        "Chúng tôi đang tuyển QA Engineer cho nhóm sản phẩm.",
    )
    home_html = synthetic_home_html(actor, (post,)).replace(
        "<html><body>",
        '<html><head><meta charset="utf-8"></head><body>',
        1,
    ).replace(
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


def test_auto_revalidation_rejects_changed_body_before_detail_click(
    tmp_path: Path,
    monkeypatch,
) -> None:
    actor = "offline.operator"
    data_root = tmp_path / "data"
    account = create_local_account(data_root, actor=actor)
    store = CheckpointStore(data_root)
    post = SyntheticPost(
        "changed-before-detail",
        "Chúng tôi đang tuyển QA Engineer cho nhóm sản phẩm.",
    )
    home_html = synthetic_home_html(actor, (post,)).replace(
        "<html><body>",
        '<html><head><meta charset="utf-8"></head><body>',
        1,
    )
    target_url = f"https://www.threads.com/@candidate/post/{post.slug}"
    actionability_calls = 0
    detail_requests = 0
    original_revalidate_target = ThreadsHomeReader._revalidate_action_target

    async def change_body_on_final_revalidation(self, target):
        nonlocal actionability_calls
        actionability_calls += 1
        if actionability_calls == 2:
            await self._page.locator("main p").evaluate(
                "(node, value) => { node.textContent = value; }",
                "Bên mình không tuyển nhân viên nữa.",
            )
        return await original_revalidate_target(self, target)

    monkeypatch.setattr(
        ThreadsHomeReader,
        "_revalidate_action_target",
        change_body_on_final_revalidation,
    )

    async def exercise(page) -> None:
        nonlocal detail_requests

        async def count_detail(route: Route) -> None:
            nonlocal detail_requests
            detail_requests += 1
            await route.fulfill(
                status=200,
                content_type="text/html",
                body=_detail_html(post),
            )

        await page.route(target_url, count_detail)
        with store.profile_lock(account), pytest.raises(
            UiReadError,
            match="DETAIL_PROVENANCE_UNVERIFIED",
        ):
            await run_page_workflow_session(
                page,
                account,
                store,
                max_posts=5,
                terms=(),
                max_scrolls=0,
                max_total_posts=10,
                max_detail_reads=1,
                console_output_policy=ConsoleOutputPolicy.AUTO,
                auto_mode=True,
            )

    run_offline_pages({HOME_URL: home_html}, exercise)
    checkpoint = store.load(account.account_id, auto_policy_fingerprint())
    assert actionability_calls == 2
    assert detail_requests == 0
    assert checkpoint.completed_runs == 0
    assert checkpoint.detail_reads == 0
    assert checkpoint.seen_fingerprints == ()
    assert checkpoint.pending_read_fingerprints == ()


@pytest.mark.parametrize(
    ("body", "unverified_relevant"),
    (
        ("Mình đang tìm việc làm ở Đà Nẵng.", 1),
        ("We're hiring an engineer.", 0),
    ),
    ids=("vietnamese_provisional_positive", "english_provisional_negative"),
)
def test_auto_classifier_applies_to_unverified_observations(
    tmp_path: Path,
    body: str,
    unverified_relevant: int,
) -> None:
    actor = "offline.operator"
    data_root = tmp_path / "data"
    account = create_local_account(data_root, actor=actor)
    store = CheckpointStore(data_root)
    home_html = f"""<!doctype html><html><head><meta charset="utf-8"></head><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@{actor}">Profile</a>
</nav><main><div><a href="/@candidate">candidate</a><time>now</time>
  <span>{body}</span></div></main></body></html>"""

    async def exercise(page) -> None:
        with store.profile_lock(account):
            result = await run_page_workflow_session(
                page,
                account,
                store,
                max_posts=5,
                terms=(),
                max_scrolls=0,
                max_total_posts=10,
                max_detail_reads=1,
                console_output_policy=ConsoleOutputPolicy.AUTO,
                auto_mode=True,
            )

        assert result.unverified_relevant == unverified_relevant
        assert result.home_sampled == 0
        assert result.provenance_verified == 0
        assert result.detail_reads == 0
        assert result.checkpoint_saved is False
        checkpoint = store.load(account.account_id, auto_policy_fingerprint())
        assert checkpoint.completed_runs == 0
        assert checkpoint.detail_reads == 0
        assert checkpoint.seen_fingerprints == ()
        assert checkpoint.pending_read_fingerprints == ()

    run_offline_pages({HOME_URL: home_html}, exercise)
