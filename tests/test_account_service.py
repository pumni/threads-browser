from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path

import pytest

from threads_browser import account_service
from threads_browser.login import ManualLoginResult
from threads_browser.storage import AccountStore, BrowserDataError, profile_lock


def test_public_api_creates_a_local_account(tmp_path: Path) -> None:
    account = account_service.add_local_account("service1", data_root=tmp_path / "data")

    assert account.expected_actor is None
    assert account.profile_directory == (tmp_path / "data" / "profiles" / "service1")
    assert account.profile_directory.is_dir()
    assert AccountStore(tmp_path / "data").get("service1") == account


def test_open_login_is_tty_free_and_holds_lock_through_browser_lifetime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "data"
    account = account_service.add_local_account("service1", data_root=root)
    statuses: list[str] = []

    async def fake_run_manual_login(registered, accounts, *, status_callback):
        assert registered == account
        assert accounts.get("service1") == account
        assert status_callback is not None
        with pytest.raises(BrowserDataError) as caught:
            profile_lock(root, registered).acquire()
        assert caught.value.code == "ACCOUNT_BUSY"
        status_callback("VERIFIED")
        return ManualLoginResult("CLOSED")

    monkeypatch.setattr(account_service, "run_manual_login", fake_run_manual_login)

    result = asyncio.run(
        account_service.open_account_login(
            "service1",
            data_root=root,
            status_callback=statuses.append,
        )
    )

    assert result.status == "CLOSED"
    assert statuses == ["VERIFIED"]
    with profile_lock(root, account):
        pass


def test_cancelling_open_login_releases_the_profile_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "data"
    account = account_service.add_local_account("service1", data_root=root)

    async def cancel_login(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(account_service, "run_manual_login", cancel_login)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(account_service.open_account_login("service1", data_root=root))

    with profile_lock(root, account):
        pass


def test_console_script_is_not_registered_but_module_runner_remains_available() -> None:
    project_root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert "threads-browser" not in pyproject["project"].get("scripts", {})
    assert (project_root / "src" / "threads_browser" / "cli.py").is_file()
