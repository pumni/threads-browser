from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Route
from playwright.async_api import async_playwright as real_async_playwright

from threads_browser import cli, login, reader
from threads_browser.cli import main
from threads_browser.reader import HOME_URL, UiReadError
from threads_browser.storage import (
    AccountStore,
    BrowserDataError,
    profile_lock,
)


class _FakePage:
    def __init__(self, *, navigation_error: Exception | None = None) -> None:
        self.url = "about:blank"
        self.navigation_error = navigation_error
        self.visited: list[str] = []

    async def goto(self, url: str, **_: object) -> None:
        self.visited.append(url)
        if self.navigation_error is not None:
            raise self.navigation_error
        self.url = url


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self.pages = [page]
        self.closed = False
        self.close_count = 0
        self.authenticated = False
        self.close_event = asyncio.Event()
        self.waiting_for_authentication = asyncio.Event()
        self.browser_open = asyncio.Event()

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        if self.closed:
            return
        self.close_count += 1
        self.user_close()

    def user_close(self) -> None:
        self.closed = True
        self.close_event.set()

    async def wait_for_event(self, event: str) -> None:
        assert event == "close"
        if self.authenticated:
            self.browser_open.set()
        else:
            self.waiting_for_authentication.set()
        await self.close_event.wait()


class _FakePlaywrightManager:
    def __init__(
        self,
        context: _FakeContext,
        calls: dict[str, object],
        *,
        launch_error: Exception | None = None,
    ) -> None:
        self.context = context
        self.calls = calls
        self.launch_error = launch_error
        self.exited = False

    async def __aenter__(self) -> SimpleNamespace:
        async def launch_persistent_context(**kwargs: object) -> _FakeContext:
            self.calls.update(kwargs)
            if self.launch_error is not None:
                raise self.launch_error
            return self.context

        return SimpleNamespace(
            chromium=SimpleNamespace(launch_persistent_context=launch_persistent_context)
        )

    async def __aexit__(self, *_: object) -> None:
        self.exited = True


def _account(tmp_path: Path, *, alias: str = "local1", actor: str | None = None):
    root = tmp_path / "data"
    store = AccountStore(root)
    if actor is None:
        account = store.add(alias)
    else:
        profile = tmp_path / "synthetic-profile"
        profile.mkdir()
        account = store.add(alias, actor, profile)
    return root, account.profile_directory, store, account


def _install_fake_browser(
    monkeypatch: pytest.MonkeyPatch,
    *,
    page: _FakePage | None = None,
    launch_error: Exception | None = None,
):
    active_page = page or _FakePage()
    context = _FakeContext(active_page)
    calls: dict[str, object] = {}
    manager = _FakePlaywrightManager(context, calls, launch_error=launch_error)
    monkeypatch.setattr(login, "async_playwright", lambda: manager)
    return active_page, context, manager, calls


def _install_reader(
    monkeypatch: pytest.MonkeyPatch,
    context: _FakeContext,
    outcomes: list[str | UiReadError],
    *,
    wait: asyncio.Event | None = None,
):
    calls: list[str | None] = []
    waits: list[None] = []

    class FakeReader:
        def __init__(self, _page: object, expected_actor: str | None) -> None:
            calls.append(expected_actor)

        async def verify_login_actor(self) -> str:
            outcome = outcomes.pop(0)
            if isinstance(outcome, UiReadError):
                raise outcome
            context.authenticated = True
            return outcome

        async def wait_for_login_control(self) -> None:
            waits.append(None)
            if wait is not None:
                await wait.wait()

    monkeypatch.setattr(login, "ThreadsHomeReader", FakeReader)
    return calls, waits


async def _run_until_browser_close(
    account,
    store: AccountStore,
    context: _FakeContext,
) -> login.ManualLoginResult:
    task = asyncio.create_task(login.run_manual_login(account, store))
    await asyncio.wait_for(context.browser_open.wait(), timeout=1)
    assert not context.closed
    context.user_close()
    return await task


def test_first_login_binds_verified_actor_and_keeps_browser_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, profile, store, account = _account(tmp_path)
    _, context, manager, calls = _install_fake_browser(monkeypatch)
    verify_calls, waits = _install_reader(
        monkeypatch,
        context,
        [UiReadError("LOGIN_REQUIRED"), "offline.operator"],
    )

    result = asyncio.run(_run_until_browser_close(account, store, context))

    bound = store.get("local1")
    document = json.loads((root / "accounts" / "local1.json").read_text(encoding="utf-8"))
    assert result.status == "CLOSED"
    assert calls == {"user_data_dir": str(profile), "headless": False}
    assert verify_calls == [None, None, None]
    assert len(waits) == 1
    assert bound.account_id == account.account_id
    assert bound.profile_directory == account.profile_directory
    assert bound.expected_actor == "offline.operator"
    assert document["expected_actor"] == "offline.operator"
    assert context.closed and context.close_count == 0
    assert manager.exited
    assert not (root / "nurture").exists()
    output = capsys.readouterr().out
    assert output.index("LOGIN status=WAITING_FOR_AUTHENTICATION") < output.index(
        "LOGIN status=VERIFIED"
    )
    assert "ACCOUNT local1" in output
    assert "BROWSER status=OPEN" in output
    assert str(profile) not in output
    assert "offline.operator" not in output


def test_subsequent_login_reuses_bound_session_without_terminal_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, profile, store, account = _account(tmp_path, actor="offline.operator")
    _, context, _, calls = _install_fake_browser(monkeypatch)
    verify_calls, waits = _install_reader(monkeypatch, context, ["offline.operator"])

    result = asyncio.run(_run_until_browser_close(account, store, context))

    assert result.status == "CLOSED"
    assert calls == {"user_data_dir": str(profile), "headless": False}
    assert verify_calls == ["offline.operator"]
    assert waits == []
    assert store.get("local1").account_id == account.account_id
    output = capsys.readouterr().out
    assert "LOGIN status=SESSION_REUSED" in output
    assert "LOGIN status=VERIFIED" in output
    assert "BROWSER status=OPEN" in output


def test_expired_session_waits_for_manual_reauthentication_without_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _, store, account = _account(tmp_path, actor="offline.operator")
    metadata_path = root / "accounts" / "local1.json"
    before = metadata_path.read_bytes()
    _, context, _, _ = _install_fake_browser(monkeypatch)
    verify_calls, waits = _install_reader(
        monkeypatch,
        context,
        [UiReadError("LOGIN_REQUIRED"), "offline.operator"],
    )

    result = asyncio.run(_run_until_browser_close(account, store, context))

    assert result.status == "CLOSED"
    assert verify_calls == ["offline.operator"] * 3
    assert len(waits) == 1
    assert metadata_path.read_bytes() == before
    output = capsys.readouterr().out
    assert "LOGIN status=WAITING_FOR_AUTHENTICATION" in output
    assert "LOGIN status=SESSION_REUSED" not in output


def test_challenge_waits_for_human_completion_but_rate_limit_fails_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, store, account = _account(tmp_path)
    _, challenge_context, _, _ = _install_fake_browser(monkeypatch)
    _, waits = _install_reader(
        monkeypatch,
        challenge_context,
        [UiReadError("CHALLENGE_REQUIRED"), "offline.operator"],
    )
    result = asyncio.run(_run_until_browser_close(account, store, challenge_context))
    assert result.status == "CLOSED"
    assert len(waits) == 1

    _, limited_context, _, _ = _install_fake_browser(monkeypatch)
    _install_reader(monkeypatch, limited_context, [UiReadError("RATE_LIMITED")])
    with pytest.raises(UiReadError) as caught:
        asyncio.run(login.run_manual_login(store.get("local1"), store))
    assert caught.value.code == "RATE_LIMITED"
    assert limited_context.closed
    output = capsys.readouterr().out
    assert "WAITING_FOR_AUTHENTICATION" in output


def test_actor_mismatch_never_rebinds_an_existing_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _, store, account = _account(tmp_path, actor="offline.operator")
    metadata_path = root / "accounts" / "local1.json"
    before = metadata_path.read_bytes()
    _, context, _, _ = _install_fake_browser(monkeypatch)
    _install_reader(monkeypatch, context, [UiReadError("ACTOR_MISMATCH")])

    with pytest.raises(UiReadError) as caught:
        asyncio.run(login.run_manual_login(account, store))

    assert caught.value.code == "ACTOR_MISMATCH"
    assert context.closed and context.close_count == 1
    assert metadata_path.read_bytes() == before
    assert store.get("local1").expected_actor == "offline.operator"


def test_closing_browser_during_first_login_cancels_and_releases_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _, store, account = _account(tmp_path)
    _, context, manager, _ = _install_fake_browser(monkeypatch)
    never_ready = asyncio.Event()
    _install_reader(
        monkeypatch,
        context,
        [UiReadError("LOGIN_REQUIRED")],
        wait=never_ready,
    )

    async def exercise() -> login.ManualLoginResult:
        task = asyncio.create_task(login.run_manual_login(account, store))
        await asyncio.wait_for(context.waiting_for_authentication.wait(), timeout=1)
        assert not context.closed
        context.user_close()
        return await task

    result = asyncio.run(exercise())

    assert result.status == "CANCELLED"
    assert context.closed
    assert manager.exited
    assert store.get("local1").expected_actor is None
    assert not (root / "nurture").exists()


def test_browser_failure_closes_context_and_sanitizes_profile_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, profile, store, account = _account(tmp_path)
    _, context, manager, calls = _install_fake_browser(
        monkeypatch,
        page=_FakePage(navigation_error=PlaywrightError(str(profile))),
    )

    with pytest.raises(UiReadError) as caught:
        asyncio.run(login.run_manual_login(account, store))

    assert caught.value.code == "BROWSER_SESSION_UNAVAILABLE"
    assert context.closed and context.close_count == 1
    assert manager.exited
    assert calls["headless"] is False
    assert str(profile) not in str(caught.value) + capsys.readouterr().out


def test_cli_login_is_visible_without_requiring_headed_or_a_tty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, profile, store, account = _account(tmp_path)
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(root))
    monkeypatch.setattr(cli, "_has_interactive_terminal", lambda: False)
    calls: list[Path] = []

    async def fake_login(registered_account, registered_store):
        assert registered_store.get("local1") == account
        calls.append(registered_account.profile_directory)
        with pytest.raises(BrowserDataError) as caught:
            profile_lock(root, registered_account).acquire()
        assert caught.value.code == "ACCOUNT_BUSY"
        return login.ManualLoginResult("CLOSED")

    monkeypatch.setattr(login, "run_manual_login", fake_login)

    assert main(["account", "login", "local1"]) == 0
    assert main(["account", "login", "local1", "--headed"]) == 0
    assert calls == [profile, profile]
    assert capsys.readouterr().out == ""
    assert store.get("local1").account_id == account.account_id
    with profile_lock(root, account):
        pass


def test_account_login_missing_account_returns_fixed_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(cli, "_has_interactive_terminal", lambda: False)

    assert main(["account", "login", "missing"]) == 1
    assert capsys.readouterr().err == "ERROR ACCOUNT_NOT_FOUND\n"


def _run_local_actor_verification(
    home_html: str,
    profile_actor: str | None,
    expected_actor: str | None,
) -> tuple[str, str | None, str | None]:
    async def exercise() -> tuple[str, str | None, str | None]:
        async with real_async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context()

            async def respond(route: Route) -> None:
                if route.request.url == HOME_URL:
                    await route.fulfill(status=200, content_type="text/html", body=home_html)
                    return
                path = urlsplit(route.request.url).path
                if profile_actor is not None and path == f"/@{profile_actor}":
                    await route.fulfill(
                        status=200,
                        content_type="text/html",
                        body=(
                            "<!doctype html><html><body><main><h1>Profile "
                            f"{profile_actor}</h1></main></body></html>"
                        ),
                    )
                    return
                await route.abort()

            await context.route("**/*", respond)
            page = await context.new_page()
            try:
                await page.goto(HOME_URL, wait_until="domcontentloaded")
                verifier = reader.ThreadsHomeReader(page, expected_actor)
                try:
                    actor = await verifier.verify_login_actor()
                except UiReadError as error:
                    return error.code, None, page.url
                return "VERIFIED", actor, page.url
            finally:
                await context.close()
                await browser.close()

    return asyncio.run(exercise())


def test_reader_discovers_unbound_actor_through_profile_control_and_returns_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reader, "_RENDER_MIN_DWELL_MS", 20)
    monkeypatch.setattr(reader, "_RENDER_SETTLE_INTERVAL_MS", 10)
    home = """<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-label="Profile" href="/@offline.operator">Profile</a>
</nav>
<main>Home</main></body></html>"""

    status, actor, final_url = _run_local_actor_verification(
        home,
        "offline.operator",
        None,
    )

    assert status == "VERIFIED"
    assert actor == "offline.operator"
    assert final_url == HOME_URL


def test_reader_rejects_mismatched_bound_actor_and_login_required() -> None:
    home = """<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-label="Profile" href="/@other.operator">Profile</a>
</nav>
<main>Home</main></body></html>"""
    mismatch, _, mismatch_url = _run_local_actor_verification(
        home,
        "other.operator",
        "offline.operator",
    )
    logged_out = """<!doctype html><html><head><title>Log in to Threads</title></head>
<body><form><input name="username"></form></body></html>"""
    login_required, _, _ = _run_local_actor_verification(logged_out, None, None)

    assert mismatch == "ACTOR_MISMATCH"
    assert mismatch_url == "https://www.threads.com/@other.operator"
    assert login_required == "LOGIN_REQUIRED"


def test_reader_detects_authentication_automatically_after_dom_update() -> None:
    home = """<!doctype html><html><body>
<nav aria-label="Main" role="navigation"></nav>
<main>Waiting for login</main>
<script>
setTimeout(() => {
  document.querySelector('nav').innerHTML =
    '<a aria-label="Profile" href="/@offline.operator">Profile</a>';
}, 50);
</script></body></html>"""

    async def exercise() -> tuple[str, str]:
        async with real_async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context()

            async def respond(route: Route) -> None:
                if route.request.url == HOME_URL:
                    await route.fulfill(status=200, content_type="text/html", body=home)
                    return
                if urlsplit(route.request.url).path == "/@offline.operator":
                    await route.fulfill(
                        status=200,
                        content_type="text/html",
                        body="<html><body><main><h1>Profile</h1></main></body></html>",
                    )
                    return
                await route.abort()

            await context.route("**/*", respond)
            page = await context.new_page()
            try:
                await page.goto(HOME_URL, wait_until="domcontentloaded")
                verifier = reader.ThreadsHomeReader(page, None)
                await verifier.wait_for_login_control()
                actor = await verifier.verify_login_actor()
                return actor, page.url
            finally:
                await context.close()
                await browser.close()

    actor, final_url = asyncio.run(exercise())
    assert actor == "offline.operator"
    assert final_url == HOME_URL
