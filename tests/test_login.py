from __future__ import annotations

import asyncio
import threading
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

    async def close(self) -> None:
        self.closed = True
        self.close_count += 1


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


def _account(tmp_path: Path):
    root = tmp_path / "data"
    profile = tmp_path / "synthetic-profile"
    profile.mkdir()
    account = AccountStore(root).add("offline", "@offline.operator", profile)
    return root, profile, account


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


def _install_verifier(monkeypatch: pytest.MonkeyPatch, outcomes: list[str | None]):
    calls: list[str] = []

    class FakeReader:
        def __init__(self, _page: object, expected_actor: str) -> None:
            calls.append(expected_actor)

        async def verify_login_actor(self) -> None:
            outcome = outcomes.pop(0)
            if outcome is not None:
                raise UiReadError(outcome)

    monkeypatch.setattr(login, "ThreadsHomeReader", FakeReader)
    return calls


def test_manual_login_uses_registered_profile_and_keeps_browser_open_until_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, profile, account = _account(tmp_path)
    page, context, manager, calls = _install_fake_browser(monkeypatch)
    verifier_calls = _install_verifier(monkeypatch, [None])

    def confirm(_: str) -> str:
        assert not context.closed
        return ""

    monkeypatch.setattr("builtins.input", confirm)
    result = asyncio.run(login.run_manual_login(account))

    assert result.status == "VERIFIED"
    assert calls == {"user_data_dir": str(profile.resolve()), "headless": False}
    assert page.visited == [HOME_URL]
    assert verifier_calls == ["offline.operator"]
    assert context.closed and context.close_count == 1
    assert manager.exited
    captured = capsys.readouterr()
    assert "LOGIN status=WAITING_FOR_USER" in captured.out
    assert str(profile) not in captured.out + captured.err
    assert "offline.operator" not in captured.out + captured.err


def test_manual_login_requires_an_explicit_retry_after_incomplete_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, account = _account(tmp_path)
    _, context, _, _ = _install_fake_browser(monkeypatch)
    verifier_calls = _install_verifier(monkeypatch, ["LOGIN_REQUIRED", None])
    inputs = iter(("", ""))

    def confirm(_: str) -> str:
        assert not context.closed
        return next(inputs)

    monkeypatch.setattr("builtins.input", confirm)
    result = asyncio.run(login.run_manual_login(account))

    assert result.status == "VERIFIED"
    assert len(verifier_calls) == 2
    assert "LOGIN status=NOT_VERIFIED reason=LOGIN_REQUIRED" in capsys.readouterr().err


@pytest.mark.parametrize("error_code", ["ACTOR_MISMATCH", "LOGIN_REQUIRED"])
def test_manual_login_never_reports_unverified_or_mismatched_actor_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_code: str,
) -> None:
    _, _, account = _account(tmp_path)
    _, context, _, _ = _install_fake_browser(monkeypatch)
    _install_verifier(monkeypatch, [error_code, error_code])
    inputs = iter(("", ""))

    def confirm(_: str) -> str:
        assert not context.closed
        return next(inputs)

    monkeypatch.setattr("builtins.input", confirm)
    result = asyncio.run(login.run_manual_login(account))

    assert result.status == "FAILED"
    assert result.error_code == error_code
    assert context.closed


def test_manual_login_cancellation_closes_browser_without_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, _, account = _account(tmp_path)
    _, context, _, _ = _install_fake_browser(monkeypatch)
    verifier_calls = _install_verifier(monkeypatch, [])
    monkeypatch.setattr("builtins.input", lambda _: "q")

    result = asyncio.run(login.run_manual_login(account))

    assert result.status == "CANCELLED"
    assert verifier_calls == []
    assert context.closed
    assert not (root / "nurture").exists()


def test_manual_login_input_does_not_block_the_asyncio_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, account = _account(tmp_path)
    _, context, _, _ = _install_fake_browser(monkeypatch)
    _install_verifier(monkeypatch, [None])
    input_started = threading.Event()
    release_input = threading.Event()

    def blocking_input(_: str) -> str:
        assert not context.closed
        input_started.set()
        release_input.wait(timeout=2)
        return ""

    monkeypatch.setattr("builtins.input", blocking_input)

    async def exercise() -> None:
        task = asyncio.create_task(login.run_manual_login(account))
        try:
            assert await asyncio.to_thread(input_started.wait, 1)
            heartbeat = asyncio.Event()
            asyncio.get_running_loop().call_soon(heartbeat.set)
            await asyncio.wait_for(heartbeat.wait(), timeout=0.5)
        finally:
            release_input.set()
        assert (await task).status == "VERIFIED"

    asyncio.run(exercise())


def test_manual_login_task_cancellation_closes_browser_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, account = _account(tmp_path)
    _, context, _, _ = _install_fake_browser(monkeypatch)
    input_started = threading.Event()
    release_input = threading.Event()

    def blocking_input(_: str) -> str:
        input_started.set()
        release_input.wait(timeout=2)
        return ""

    monkeypatch.setattr("builtins.input", blocking_input)

    async def exercise() -> None:
        task = asyncio.create_task(login.run_manual_login(account))
        assert await asyncio.to_thread(input_started.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert context.closed
        release_input.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())


def test_manual_login_browser_navigation_failure_closes_context_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, profile, account = _account(tmp_path)
    _, context, manager, calls = _install_fake_browser(
        monkeypatch,
        page=_FakePage(navigation_error=PlaywrightError(str(profile))),
    )

    with pytest.raises(UiReadError) as caught:
        asyncio.run(login.run_manual_login(account))

    assert caught.value.code == "BROWSER_SESSION_UNAVAILABLE"
    assert context.closed
    assert manager.exited
    assert calls["headless"] is False
    assert str(profile) not in str(caught.value) + capsys.readouterr().out


def test_account_login_cli_requires_headed_interactive_terminal_before_data_access(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "resolve_data_root",
        lambda: (_ for _ in ()).throw(AssertionError("data root accessed")),
    )
    monkeypatch.setattr(cli, "_has_interactive_terminal", lambda: False)

    assert main(["account", "login", "offline"]) == 1
    assert capsys.readouterr().err == "ERROR HEADED_REQUIRED\n"
    assert main(["account", "login", "offline", "--headed"]) == 1
    assert capsys.readouterr().err == "ERROR LOGIN_TTY_REQUIRED\n"


def test_account_login_cli_uses_shared_profile_lock_and_preserves_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, profile, account = _account(tmp_path)
    metadata_path = root / "accounts" / "offline.json"
    before = metadata_path.read_bytes()
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(root))
    monkeypatch.setattr(cli, "_has_interactive_terminal", lambda: True)
    calls: list[Path] = []

    async def fake_login(registered_account):
        calls.append(registered_account.profile_directory)
        with pytest.raises(BrowserDataError) as caught:
            profile_lock(root, registered_account).acquire()
        assert caught.value.code == "ACCOUNT_BUSY"
        return login.ManualLoginResult("VERIFIED")

    monkeypatch.setattr(login, "run_manual_login", fake_login)
    assert main(["account", "login", "offline", "--headed"]) == 0

    assert calls == [profile.resolve()]
    assert metadata_path.read_bytes() == before
    assert AccountStore(root).get("offline").account_id == account.account_id
    assert not (root / "nurture").exists()
    with profile_lock(root, account):
        pass
    captured = capsys.readouterr()
    assert captured.out == "LOGIN status=VERIFIED\n"
    assert str(profile) not in captured.out + captured.err
    assert "offline.operator" not in captured.out + captured.err


def test_account_login_cancellation_releases_profile_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _, account = _account(tmp_path)
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(root))
    monkeypatch.setattr(cli, "_has_interactive_terminal", lambda: True)

    async def cancel_login(_registered_account):
        return login.ManualLoginResult("CANCELLED")

    monkeypatch.setattr(login, "run_manual_login", cancel_login)

    assert main(["account", "login", "offline", "--headed"]) == 1
    assert capsys.readouterr().out == "LOGIN status=CANCELLED\n"
    with profile_lock(root, account):
        pass
    assert not (root / "nurture").exists()


def test_account_login_missing_account_returns_fixed_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("THREADS_BROWSER_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(cli, "_has_interactive_terminal", lambda: True)

    assert main(["account", "login", "missing", "--headed"]) == 1
    assert capsys.readouterr().err == "ERROR ACCOUNT_NOT_FOUND\n"


def _run_local_actor_verification(
    home_html: str,
    profile_actor: str | None,
    expected_actor: str,
) -> tuple[str, str | None]:
    async def exercise() -> tuple[str, str | None]:
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
                        body=f"<!doctype html><html><body><main><h1>Profile {profile_actor}</h1></main></body></html>",
                    )
                    return
                await route.abort()

            await context.route("**/*", respond)
            page = await context.new_page()
            try:
                await page.goto(HOME_URL, wait_until="domcontentloaded")
                verifier = reader.ThreadsHomeReader(page, expected_actor)
                try:
                    await verifier.verify_login_actor()
                except UiReadError as error:
                    return error.code, page.url
                return "VERIFIED", page.url
            finally:
                await context.close()
                await browser.close()

    return asyncio.run(exercise())


def test_reader_login_verification_clicks_profile_and_checks_expected_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reader, "_RENDER_MIN_DWELL_MS", 20)
    monkeypatch.setattr(reader, "_RENDER_SETTLE_INTERVAL_MS", 10)
    home = """<!doctype html><html><body>
<nav aria-label="Main" role="navigation">
  <a aria-label="Profile" href="/@offline.operator">Profile</a>
</nav>
<main>Home</main></body></html>"""

    matching, matching_url = _run_local_actor_verification(
        home,
        "offline.operator",
        "offline.operator",
    )
    mismatch, mismatch_url = _run_local_actor_verification(
        home.replace("offline.operator", "other.operator"),
        "other.operator",
        "offline.operator",
    )

    assert matching == "VERIFIED"
    assert matching_url == "https://www.threads.com/@offline.operator"
    assert mismatch == "ACTOR_MISMATCH"
    assert mismatch_url == "https://www.threads.com/@other.operator"


def test_reader_login_verification_fails_when_login_is_incomplete() -> None:
    logged_out = """<!doctype html><html><head><title>Log in to Threads</title></head>
<body><form><input name="username"></form></body></html>"""

    code, _ = _run_local_actor_verification(
        logged_out,
        None,
        "offline.operator",
    )

    assert code == "LOGIN_REQUIRED"
