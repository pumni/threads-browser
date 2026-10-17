from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from tests.fixtures.browser import create_local_account
from threads_browser.storage import BrowserAccount


def verified_local_account(data_root: Path, alias: str = "local1") -> BrowserAccount:
    return create_local_account(data_root, alias=alias)


def fake_nurture_result() -> SimpleNamespace:
    return SimpleNamespace(
        status="NO_VERIFIED_POSTS_IN_BUDGET",
        ui_status="UNVERIFIED_LIVE_UI",
        home_sampled=0,
        home_new=0,
        home_relevant=0,
        home_irrelevant=0,
        home_unknown=0,
        home_relevant_ratio=None,
        duplicates=0,
        detail_reads=0,
        scroll_attempts=0,
        scrolls=0,
        skipped_unverified=0,
        checkpoint_runs=0,
    )
