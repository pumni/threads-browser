from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.fixtures.browser import (
    _mock_persistent_browser,
    _run_offline_page,
)
from threads_browser import reader as reader_module
from threads_browser import workflow
from threads_browser.reader import (
    HOME_URL,
    ThreadsHomeReader,
    UiReadError,
)
from threads_browser.storage import (
    AccountStore,
    BrowserAccount,
    BrowserDataError,
    Checkpoint,
    CheckpointStore,
)
from threads_browser.workflow import policy_fingerprint


def test_for_you_click_must_leave_an_active_marker_or_route() -> None:
    html = """<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@offline.operator">Profile</a>
</nav>
<article><p>Offline feed</p></article>
</body></html>"""

    async def exercise(page) -> None:
        with pytest.raises(UiReadError) as caught:
            await ThreadsHomeReader(page, "offline.operator").open_home()
        assert caught.value.code == "FOR_YOU_ACTIVATION_NOT_READY"

    _run_offline_page(html, exercise)


def test_actor_verification_scopes_sidebar_profile_text_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for_you = '<a aria-current="page" aria-label="For you" href="/">For you</a>'
    outside_navigation = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">{for_you}</nav>
<main><div>Profile</div></main>
</body></html>"""

    async def profile_text_outside_sidebar_stops(page) -> None:
        with pytest.raises(UiReadError) as caught:
            await ThreadsHomeReader(page, "offline.operator").open_home()
        assert caught.value.code == "PROFILE_CONTROL_NOT_FOUND"
        assert page.url == HOME_URL

    _run_offline_page(outside_navigation, profile_text_outside_sidebar_stops)

    no_navigation = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">{for_you}
  <div><span>Profile</span></div>
</nav></body></html>"""

    async def profile_click_without_navigation_stops(page) -> None:
        with pytest.raises(UiReadError) as caught:
            await ThreadsHomeReader(page, "offline.operator").open_home()
        assert caught.value.code == "PROFILE_CLICK_NO_NAVIGATION"

    with monkeypatch.context() as short_timeout:
        short_timeout.setattr(reader_module, "_READINESS_TIMEOUT_MS", 1_500)
        _run_offline_page(no_navigation, profile_click_without_navigation_stops)

    sidebar_text = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">{for_you}
  <div onclick="window.location.href='/@offline.operator'"><span>Profile</span></div>
</nav></body></html>"""

    async def sidebar_text_opens_and_verifies_profile(page) -> None:
        await ThreadsHomeReader(page, "offline.operator").open_home()
        assert page.url == HOME_URL

    _run_offline_page(sidebar_text, sidebar_text_opens_and_verifies_profile)

    unsupported_route = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">{for_you}
  <div onclick="history.pushState({{}}, '', '/settings')"><span>Profile</span></div>
</nav></body></html>"""

    async def unsupported_profile_route_stops(page) -> None:
        with pytest.raises(UiReadError) as caught:
            await ThreadsHomeReader(page, "offline.operator").open_home()
        assert caught.value.code == "PROFILE_ROUTE_UNVERIFIED"
        assert page.url.endswith("/settings")

    _run_offline_page(unsupported_route, unsupported_profile_route_stops)

    ambiguous = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">{for_you}
  <div><span>Profile</span></div><div><span>Profile</span></div>
</nav>
</body></html>"""

    async def ambiguous_sidebar_text_stops(page) -> None:
        with pytest.raises(UiReadError) as caught:
            await ThreadsHomeReader(page, "offline.operator").open_home()
        assert caught.value.code == "PROFILE_CONTROL_AMBIGUOUS"
        assert page.url == HOME_URL

    _run_offline_page(ambiguous, ambiguous_sidebar_text_stops)

    mismatched = f"""<!doctype html><html><body>
<nav aria-label="Main" role="navigation">{for_you}
  <div onclick="window.location.href='/@other.operator'"><span>Profile</span></div>
</nav>
</body></html>"""

    async def mismatched_profile_route(page) -> None:
        with pytest.raises(UiReadError) as caught:
            await ThreadsHomeReader(page, "offline.operator").open_home()
        assert caught.value.code == "ACTOR_MISMATCH"
        assert page.url == "https://www.threads.com/@other.operator"

    _run_offline_page(mismatched, mismatched_profile_route)


def test_scroll_attempt_is_not_counted_without_observed_feed_movement() -> None:
    html = """<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-current="page" aria-label="For you" href="/">For you</a>
  <a aria-label="Profile" href="/@offline.operator">Profile</a>
</nav>
<article>
  <a href="/@candidate/post/one"><time>1h</time></a>
  <a href="/@candidate">@candidate</a>
  <p>Offline feed</p>
</article>
</body></html>"""

    async def exercise(page) -> None:
        reader = ThreadsHomeReader(page, "offline.operator")
        await reader.open_home()
        with pytest.raises(UiReadError) as caught:
            await reader.sample_and_scroll(1)
        assert caught.value.code == "HOME_SCROLL_SURFACE_UNVERIFIED"
        assert reader.scroll_attempts == 0
        assert reader.scrolls == 0

    _run_offline_page(html, exercise)


def test_default_desktop_profile_is_rejected_before_browser_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os
    import sys

    if os.name == "nt":
        local = tmp_path / "LocalAppData"
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        profile = local / "Google" / "Chrome" / "User Data" / "Profile 1"
    elif sys.platform == "darwin":
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        profile = tmp_path / "Library" / "Application Support" / "Google" / "Chrome" / "Profile 1"
    else:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        profile = tmp_path / ".config" / "google-chrome" / "Profile 1"
    profile.mkdir(parents=True)
    with pytest.raises(BrowserDataError) as registration_error:
        AccountStore(tmp_path / "data").add("offline", "actor", profile)
    assert registration_error.value.code == "PROFILE_UNAVAILABLE"

    account = BrowserAccount(uuid4(), "offline", "actor", profile.resolve())

    def browser_must_not_start():
        pytest.fail("Playwright started for a default desktop profile")

    monkeypatch.setattr(workflow, "async_playwright", browser_must_not_start)
    with pytest.raises(BrowserDataError) as launch_error:
        asyncio.run(
            workflow.run_browser_nurture(
                account,
                CheckpointStore(tmp_path / "run-data"),
                max_posts=1,
                terms=("hiring",),
            )
        )
    assert launch_error.value.code == "PROFILE_UNAVAILABLE"


def test_direct_browser_workflow_rejects_busy_profile_and_releases_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "dedicated-profile"
    profile.mkdir()
    account = BrowserAccount(uuid4(), "offline", "offline.operator", profile)
    store = CheckpointStore(tmp_path / "data")

    def browser_must_not_start():
        pytest.fail("busy profile must be rejected before Playwright starts")

    monkeypatch.setattr(workflow, "async_playwright", browser_must_not_start)
    with store.profile_lock(account), pytest.raises(BrowserDataError) as busy:
        asyncio.run(workflow.run_browser_nurture(account, store, max_posts=1, terms=("hiring",)))
    assert busy.value.code == "ACCOUNT_BUSY"

    def fail_browser_start():
        raise OSError("synthetic browser startup failure")

    monkeypatch.setattr(workflow, "async_playwright", fail_browser_start)
    with pytest.raises(UiReadError) as failed:
        asyncio.run(workflow.run_browser_nurture(account, store, max_posts=1, terms=("hiring",)))
    assert failed.value.code == "INTERNAL_OPERATION_ERROR"
    with store.profile_lock(account):
        pass


def test_only_managed_browser_workflow_is_a_public_profile_entry_point() -> None:
    assert "run_browser_nurture" in workflow.__all__
    assert hasattr(workflow, "run_browser_nurture")
    assert "run_page_workflow" not in workflow.__all__
    assert not hasattr(workflow, "run_page_workflow")


def test_concurrent_browser_workflows_reject_same_profile_before_checkpoint_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "dedicated-profile"
    profile.mkdir()
    account = BrowserAccount(uuid4(), "offline", "offline.operator", profile)
    store = CheckpointStore(tmp_path / "data")
    policy = policy_fingerprint(("hiring",))
    initial = Checkpoint(account.account_id, policy, completed_runs=7)
    store.save(initial)
    browser = _mock_persistent_browser(monkeypatch)
    engine_entered = asyncio.Event()
    allow_engine_to_finish = asyncio.Event()
    checkpoint_access: list[str] = []
    original_load = store.load

    async def read_then_wait(_page, _account, checkpoint_store, **_kwargs):
        probe = checkpoint_store.profile_lock(account)
        try:
            probe.acquire()
        except BrowserDataError as error:
            assert error.code == "ACCOUNT_BUSY"
        else:
            probe.release()
            pytest.fail("managed page cycle ran without the profile lock")
        checkpoint_access.append("load")
        checkpoint = checkpoint_store.load(account.account_id, policy)
        engine_entered.set()
        await allow_engine_to_finish.wait()
        checkpoint_access.append("save")
        checkpoint_store.save(replace(checkpoint, completed_runs=8))
        return SimpleNamespace(status="COMPLETE")

    monkeypatch.setattr(workflow, "_execute_page_workflow", read_then_wait)

    async def exercise() -> None:
        first = asyncio.create_task(
            workflow.run_browser_nurture(account, store, max_posts=1, terms=("hiring",))
        )
        try:
            await asyncio.wait_for(engine_entered.wait(), timeout=2)
            with pytest.raises(BrowserDataError) as busy:
                await workflow.run_browser_nurture(account, store, max_posts=1, terms=("hiring",))
            assert busy.value.code == "ACCOUNT_BUSY"
            assert browser["launches"] == 1
            assert checkpoint_access == ["load"]
            assert original_load(account.account_id, policy) == initial
        finally:
            allow_engine_to_finish.set()
        await first

    asyncio.run(exercise())

    assert checkpoint_access == ["load", "save"]
    assert original_load(account.account_id, policy).completed_runs == 8
    with store.profile_lock(account):
        pass


def test_browser_workflows_for_different_profiles_can_run_concurrently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "data"
    accounts = tuple(
        BrowserAccount(
            uuid4(),
            f"offline-{index}",
            "offline.operator",
            tmp_path / f"profile-{index}",
        )
        for index in (1, 2)
    )
    for account in accounts:
        account.profile_directory.mkdir()
    store = CheckpointStore(root)
    browser = _mock_persistent_browser(monkeypatch)
    entered_profiles: list[Path] = []
    both_entered = asyncio.Event()
    allow_completion = asyncio.Event()

    async def wait_for_peer(_page, account, _store, **_kwargs):
        entered_profiles.append(account.profile_directory)
        if len(entered_profiles) == 2:
            both_entered.set()
        await allow_completion.wait()
        return SimpleNamespace(status="COMPLETE")

    monkeypatch.setattr(workflow, "_execute_page_workflow", wait_for_peer)

    async def exercise() -> None:
        attempts = [
            asyncio.create_task(
                workflow.run_browser_nurture(account, store, max_posts=1, terms=("hiring",))
            )
            for account in accounts
        ]
        try:
            await asyncio.wait_for(both_entered.wait(), timeout=2)
            assert browser["launches"] == 2
        finally:
            allow_completion.set()
        await asyncio.gather(*attempts)

    asyncio.run(exercise())

    assert set(entered_profiles) == {account.profile_directory for account in accounts}
    for account in accounts:
        with store.profile_lock(account):
            pass


def test_browser_runtime_timeout_during_launch_releases_profile_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "dedicated-profile"
    profile.mkdir()
    account = BrowserAccount(uuid4(), "offline", "offline.operator", profile)
    store = CheckpointStore(tmp_path / "data")
    manager_state = {"entered": False, "exited": False}

    class Chromium:
        async def launch_persistent_context(self, **_kwargs):
            await asyncio.Event().wait()

    class Manager:
        async def __aenter__(self):
            manager_state["entered"] = True
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *_args):
            manager_state["exited"] = True

    monkeypatch.setattr(workflow, "async_playwright", Manager)

    with pytest.raises(UiReadError) as caught:
        asyncio.run(
            workflow.run_browser_nurture(
                account,
                store,
                max_posts=1,
                terms=("hiring",),
                max_runtime_seconds=0.01,
            )
        )

    assert caught.value.code == "CYCLE_TIMEOUT"
    assert manager_state == {"entered": True, "exited": True}
    checkpoint = store.load(account.account_id, policy_fingerprint(("hiring",)))
    assert checkpoint.seen_fingerprints == ()
    assert checkpoint.pending_read_fingerprints == ()
    with store.profile_lock(account):
        pass


def test_browser_runtime_timeout_during_playwright_startup_attempts_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "dedicated-profile"
    profile.mkdir()
    account = BrowserAccount(uuid4(), "offline", "offline.operator", profile)
    store = CheckpointStore(tmp_path / "data")
    manager_state = {"startup_started": False, "shutdown_attempted": False}

    class Manager:
        async def __aenter__(self):
            manager_state["startup_started"] = True
            await asyncio.Event().wait()

        async def __aexit__(self, *_args):
            manager_state["shutdown_attempted"] = True

    monkeypatch.setattr(workflow, "async_playwright", Manager)

    with pytest.raises(UiReadError) as caught:
        asyncio.run(
            workflow.run_browser_nurture(
                account,
                store,
                max_posts=1,
                terms=("hiring",),
                max_runtime_seconds=0.01,
            )
        )

    assert caught.value.code == "CYCLE_TIMEOUT"
    assert manager_state == {"startup_started": True, "shutdown_attempted": True}
    checkpoint = store.load(account.account_id, policy_fingerprint(("hiring",)))
    assert checkpoint.seen_fingerprints == ()
    assert checkpoint.pending_read_fingerprints == ()
    with store.profile_lock(account):
        pass


def test_browser_runtime_timeout_during_active_detail_closes_context_and_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "dedicated-profile"
    profile.mkdir()
    account = BrowserAccount(uuid4(), "offline", "offline.operator", profile)
    store = CheckpointStore(tmp_path / "data")
    lifecycle = {"closed": False, "manager_exited": False, "detail": False}

    class Context:
        def __init__(self) -> None:
            self.pages = [object()]

        async def close(self):
            lifecycle["closed"] = True

    context = Context()

    class Chromium:
        async def launch_persistent_context(self, **_kwargs):
            return context

    class Manager:
        async def __aenter__(self):
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *_args):
            lifecycle["manager_exited"] = True

    async def wait_in_detail(*_args, **kwargs):
        lifecycle["detail"] = kwargs["detail"]
        await asyncio.Event().wait()

    monkeypatch.setattr(workflow, "async_playwright", Manager)
    monkeypatch.setattr(workflow, "_detail_terminal_ready", lambda: True)
    monkeypatch.setattr(workflow, "_run_page_workflow_locked", wait_in_detail)

    with pytest.raises(UiReadError) as caught:
        asyncio.run(
            workflow.run_browser_nurture(
                account,
                store,
                max_posts=1,
                terms=("hiring",),
                max_runtime_seconds=0.01,
                detail=True,
            )
        )

    assert caught.value.code == "CYCLE_TIMEOUT"
    assert lifecycle == {"closed": True, "manager_exited": True, "detail": True}
    checkpoint = store.load(account.account_id, policy_fingerprint(("hiring",)))
    assert checkpoint.seen_fingerprints == ()
    assert checkpoint.pending_read_fingerprints == ()
    with store.profile_lock(account):
        pass


def test_external_browser_cancellation_propagates_after_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "dedicated-profile"
    profile.mkdir()
    account = BrowserAccount(uuid4(), "offline", "offline.operator", profile)
    store = CheckpointStore(tmp_path / "data")
    lifecycle = {"closed": False, "manager_exited": False}

    class Context:
        def __init__(self) -> None:
            self.pages = [object()]

        async def close(self):
            lifecycle["closed"] = True

    class Chromium:
        async def launch_persistent_context(self, **_kwargs):
            return Context()

    class Manager:
        async def __aenter__(self):
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *_args):
            lifecycle["manager_exited"] = True

    async def wait_for_cancellation(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(workflow, "async_playwright", Manager)
    monkeypatch.setattr(workflow, "_run_page_workflow_locked", wait_for_cancellation)

    async def exercise() -> None:
        task = asyncio.create_task(
            workflow.run_browser_nurture(
                account,
                store,
                max_posts=1,
                terms=("hiring",),
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert lifecycle == {"closed": True, "manager_exited": True}
    checkpoint = store.load(account.account_id, policy_fingerprint(("hiring",)))
    assert checkpoint.seen_fingerprints == ()
    assert checkpoint.pending_read_fingerprints == ()
    with store.profile_lock(account):
        pass


def test_cancellation_during_browser_cleanup_holds_lock_until_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = tmp_path / "dedicated-profile"
    profile.mkdir()
    account = BrowserAccount(uuid4(), "offline", "offline.operator", profile)
    store = CheckpointStore(tmp_path / "data")
    close_started = asyncio.Event()
    finish_close = asyncio.Event()
    lifecycle = {"manager_exited": False}

    class Context:
        def __init__(self) -> None:
            self.pages = [object()]

        async def close(self):
            close_started.set()
            await finish_close.wait()

    class Chromium:
        async def launch_persistent_context(self, **_kwargs):
            return Context()

    class Manager:
        async def __aenter__(self):
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *_args):
            lifecycle["manager_exited"] = True

    async def successful_cycle(*_args, **_kwargs):
        return SimpleNamespace(status="COMPLETE")

    monkeypatch.setattr(workflow, "async_playwright", Manager)
    monkeypatch.setattr(workflow, "_run_page_workflow_locked", successful_cycle)

    async def exercise() -> None:
        task = asyncio.create_task(
            workflow.run_browser_nurture(account, store, max_posts=1, terms=("hiring",))
        )
        await asyncio.wait_for(close_started.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)

        probe = store.profile_lock(account)
        try:
            probe.acquire()
        except BrowserDataError as error:
            assert error.code == "ACCOUNT_BUSY"
        else:
            probe.release()
            pytest.fail("profile lock was released before browser shutdown")

        finish_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert lifecycle["manager_exited"]
    with store.profile_lock(account):
        pass


@pytest.mark.parametrize("failure", ("timeout", "exception"))
def test_browser_cleanup_failure_is_reported_after_successful_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    profile = tmp_path / "dedicated-profile"
    profile.mkdir()
    account = BrowserAccount(uuid4(), "offline", "offline.operator", profile)
    store = CheckpointStore(tmp_path / "data")
    lifecycle = {"manager_exited": False}

    class Context:
        def __init__(self) -> None:
            self.pages = [object()]

        async def close(self):
            if failure == "timeout":
                await asyncio.Event().wait()
            raise RuntimeError("synthetic cleanup failure")

    class Chromium:
        async def launch_persistent_context(self, **_kwargs):
            return Context()

    class Manager:
        async def __aenter__(self):
            return SimpleNamespace(chromium=Chromium())

        async def __aexit__(self, *_args):
            lifecycle["manager_exited"] = True

    async def successful_cycle(*_args, **_kwargs):
        return SimpleNamespace(status="COMPLETE")

    monkeypatch.setattr(workflow, "async_playwright", Manager)
    monkeypatch.setattr(workflow, "_run_page_workflow_locked", successful_cycle)
    if failure == "timeout":
        monkeypatch.setattr(workflow, "_CLEANUP_ALLOWANCE_SECONDS", 0.01)

    with pytest.raises(UiReadError) as caught:
        asyncio.run(
            workflow.run_browser_nurture(
                account,
                store,
                max_posts=1,
                terms=("hiring",),
            )
        )

    assert caught.value.code == "BROWSER_CLEANUP_FAILED"
    assert lifecycle["manager_exited"]
    with store.profile_lock(account):
        pass


def test_browser_workflow_detail_requires_tty_before_profile_or_browser_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = BrowserAccount(
        uuid4(),
        "offline",
        "offline.operator",
        tmp_path / "synthetic-profile",
    )
    monkeypatch.setattr(workflow, "_detail_terminal_ready", lambda: False)
    monkeypatch.setattr(
        workflow,
        "async_playwright",
        lambda: pytest.fail("detail mode accessed browser without a TTY"),
    )

    with pytest.raises(UiReadError) as caught:
        asyncio.run(
            workflow.run_browser_nurture(
                account,
                CheckpointStore(tmp_path / "data"),
                max_posts=1,
                terms=("hiring",),
                detail=True,
            )
        )

    assert caught.value.code == "DETAIL_TTY_REQUIRED"
