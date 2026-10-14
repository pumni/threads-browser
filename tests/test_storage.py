from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from threads_browser.storage import (
    AccountStore,
    BrowserDataError,
    Checkpoint,
    CheckpointStore,
    profile_lock,
)


def test_account_reference_is_redacted_and_profile_lock_is_scoped(tmp_path: Path) -> None:
    root = tmp_path / "private-root"
    profile = tmp_path / "existing-profile"
    profile.mkdir()
    account = AccountStore(root).add("pilot", "@Offline.Operator", profile)

    assert account.expected_actor == "offline.operator"
    assert account.profile_directory == profile.resolve()
    assert str(profile) not in repr(account)
    assert "offline.operator" not in repr(account)
    assert AccountStore(root).get("pilot") == account

    first = profile_lock(root, account)
    second = profile_lock(root, account)
    first.acquire()
    try:
        with pytest.raises(BrowserDataError) as caught:
            second.acquire()
        assert caught.value.code == "ACCOUNT_BUSY"
    finally:
        first.release()
    with second:
        pass


def test_checkpoint_resume_is_strict_atomic_and_contains_only_fingerprints(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private-root"
    account_id = uuid4()
    policy = "a" * 64
    store = CheckpointStore(root)
    saved = Checkpoint(
        account_id,
        policy,
        completed_runs=1,
        home_sampled=1,
        home_new=1,
        home_relevant=1,
        detail_reads=1,
        scroll_attempts=1,
        scrolls=1,
        seen_fingerprints=("b" * 64,),
    )
    store.save(saved)

    checkpoint_path = root / "nurture" / "checkpoints" / f"{account_id}-{policy}.json"
    raw = checkpoint_path.read_text(encoding="utf-8")
    assert store.load(account_id, policy) == saved
    assert "post-body" not in raw
    assert "/@offline.operator/post/123" not in raw

    legacy = json.loads(raw)
    legacy["schema_version"] = 1
    checkpoint_path.write_text(json.dumps(legacy), encoding="utf-8")
    legacy_before = checkpoint_path.read_bytes()
    with pytest.raises(BrowserDataError) as old_schema:
        store.load(account_id, policy)
    assert old_schema.value.code == "STATE_INVALID"
    assert checkpoint_path.read_bytes() == legacy_before

    checkpoint_path.write_text(raw, encoding="utf-8")
    corrupted = json.loads(raw)
    corrupted["unexpected"] = True
    checkpoint_path.write_text(json.dumps(corrupted), encoding="utf-8")
    before = checkpoint_path.read_bytes()
    with pytest.raises(BrowserDataError) as caught:
        store.load(account_id, policy)
    assert caught.value.code == "STATE_INVALID"
    assert checkpoint_path.read_bytes() == before


def test_account_registration_rejects_invalid_alias_and_missing_profile(
    tmp_path: Path,
) -> None:
    store = AccountStore(tmp_path / "root")
    with pytest.raises(BrowserDataError) as invalid_alias:
        store.add("../private", "actor", tmp_path)
    assert invalid_alias.value.code == "ACCOUNT_INVALID"

    with pytest.raises(BrowserDataError) as missing_profile:
        store.add("pilot", "actor", tmp_path / "missing")
    assert missing_profile.value.code == "PROFILE_UNAVAILABLE"
