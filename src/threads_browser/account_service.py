"""Local account management services for Python applications."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .login import LoginStatus, ManualLoginResult, run_manual_login
from .storage import AccountStore, BrowserAccount, profile_lock, resolve_data_root


def add_local_account(
    alias: str,
    *,
    data_root: Path | None = None,
) -> BrowserAccount:
    """Create an unverified local account and its dedicated Chromium profile."""
    root = resolve_data_root() if data_root is None else data_root
    return AccountStore(root).add(alias)


async def open_account_login(
    alias: str,
    *,
    data_root: Path | None = None,
    status_callback: Callable[[LoginStatus], None] | None = None,
) -> ManualLoginResult:
    """Open an account's persistent browser until the operator closes it.

    The coroutine is suitable for a desktop application or another async Python
    caller. Cancelling it closes Chromium and releases the account profile lock.
    """
    root = resolve_data_root() if data_root is None else data_root
    accounts = AccountStore(root)
    account = accounts.get(alias)
    with profile_lock(root, account):
        return await run_manual_login(
            account,
            accounts,
            status_callback=status_callback,
        )
