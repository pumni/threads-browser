from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from threads_browser import storage as storage_module
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
        scroll_attempts=3,
        scrolls=3,
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

    with pytest.raises(BrowserDataError) as invalid_actor:
        store.add("pilot", "not an actor", tmp_path)
    assert invalid_actor.value.code == "ACCOUNT_INVALID"

    with pytest.raises(BrowserDataError) as missing_profile:
        store.add("pilot", "actor", tmp_path / "missing")
    assert missing_profile.value.code == "PROFILE_UNAVAILABLE"


def test_account_registration_can_create_a_new_profile_and_preserves_schema(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private-root"
    profile = tmp_path / "profiles" / "pilot"
    store = AccountStore(root)

    account = store.add("pilot", "@Offline.Operator", profile, create_profile=True)

    assert profile.is_dir()
    assert account.profile_directory == profile.resolve()
    document = json.loads((root / "accounts" / "pilot.json").read_text(encoding="utf-8"))
    assert set(document) == {
        "schema_version",
        "account_id",
        "alias",
        "expected_actor",
        "profile_directory",
    }
    assert store.get("pilot") == account


def test_local_account_registration_creates_an_unbound_dedicated_profile(
    tmp_path: Path,
) -> None:
    root = tmp_path / "private-root"
    store = AccountStore(root)

    account = store.add("local1")
    document = json.loads((root / "accounts" / "local1.json").read_text(encoding="utf-8"))

    assert account.expected_actor is None
    assert account.profile_directory == (root / "profiles" / "local1").resolve()
    assert account.profile_directory.is_dir()
    assert document == {
        "schema_version": 2,
        "account_id": str(account.account_id),
        "alias": "local1",
        "expected_actor": None,
        "profile_directory": str(account.profile_directory),
    }
    assert store.get("local1") == account


def test_local_account_binding_is_atomic_and_preserves_identity(tmp_path: Path) -> None:
    root = tmp_path / "private-root"
    store = AccountStore(root)
    account = store.add("local1")

    bound = store.bind_actor(account, "@Offline.Operator")
    document = json.loads((root / "accounts" / "local1.json").read_text(encoding="utf-8"))

    assert bound.account_id == account.account_id
    assert bound.profile_directory == account.profile_directory
    assert bound.expected_actor == "offline.operator"
    assert document["schema_version"] == 2
    assert document["expected_actor"] == "offline.operator"
    assert set(document) == {
        "schema_version",
        "account_id",
        "alias",
        "expected_actor",
        "profile_directory",
    }
    assert store.get("local1") == bound


def test_local_account_binding_never_replaces_a_saved_actor(tmp_path: Path) -> None:
    store = AccountStore(tmp_path / "private-root")
    account = store.add("local1")
    bound = store.bind_actor(account, "first.actor")

    with pytest.raises(BrowserDataError) as caught:
        store.bind_actor(bound, "second.actor")

    assert caught.value.code == "ACTOR_MISMATCH"
    assert store.get("local1") == bound


def test_local_accounts_have_independent_profiles_and_ids(tmp_path: Path) -> None:
    root = tmp_path / "private-root"
    store = AccountStore(root)

    first = store.add("local1")
    second = store.add("local2")

    assert first.account_id != second.account_id
    assert first.profile_directory != second.profile_directory
    assert store.get("local1") == first
    assert store.get("local2") == second


def test_local_account_duplicate_does_not_overwrite_profile_or_metadata(
    tmp_path: Path,
) -> None:
    store = AccountStore(tmp_path / "private-root")
    first = store.add("local1")
    metadata = (tmp_path / "private-root" / "accounts" / "local1.json").read_bytes()

    with pytest.raises(BrowserDataError) as caught:
        store.add("local1")

    assert caught.value.code == "ACCOUNT_EXISTS"
    assert (tmp_path / "private-root" / "accounts" / "local1.json").read_bytes() == metadata
    assert store.get("local1") == first


def test_local_account_rejects_an_orphaned_existing_profile(tmp_path: Path) -> None:
    root = tmp_path / "private-root"
    orphan = root / "profiles" / "local1"
    orphan.mkdir(parents=True)

    with pytest.raises(BrowserDataError) as caught:
        AccountStore(root).add("local1")

    assert caught.value.code == "PROFILE_EXISTS"
    assert orphan.is_dir()
    assert not (root / "accounts" / "local1.json").exists()


def test_legacy_account_json_remains_readable(tmp_path: Path) -> None:
    root = tmp_path / "private-root"
    profile = tmp_path / "legacy-profile"
    profile.mkdir()
    store = AccountStore(root)
    original = store.add("legacy1", "@Offline.Operator", profile)

    loaded = store.get("legacy1")

    assert loaded == original
    assert loaded.expected_actor == "offline.operator"
    assert json.loads((root / "accounts" / "legacy1.json").read_text(encoding="utf-8"))[
        "schema_version"
    ] == 1


def test_account_creation_does_not_overwrite_an_existing_profile(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "existing-profile"
    profile.mkdir()
    marker = profile / "keep.txt"
    marker.write_text("leave this directory alone", encoding="utf-8")

    with pytest.raises(BrowserDataError) as caught:
        AccountStore(tmp_path / "root").add(
            "pilot",
            "actor",
            profile,
            create_profile=True,
        )

    assert caught.value.code == "PROFILE_EXISTS"
    assert marker.read_text(encoding="utf-8") == "leave this directory alone"


@pytest.mark.parametrize(
    ("vendor", "browser"),
    [("Google", "Chrome"), ("Microsoft", "Edge")],
)
def test_account_creation_rejects_default_desktop_browser_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    vendor: str,
    browser: str,
) -> None:
    import os

    if os.name != "nt":
        pytest.skip("Windows desktop browser roots are platform-specific")
    local_app_data = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    profile = local_app_data / vendor / browser / "User Data" / "automation"

    with pytest.raises(BrowserDataError) as caught:
        AccountStore(tmp_path / "root").add(
            "pilot",
            "actor",
            profile,
            create_profile=True,
        )

    assert caught.value.code == "PROFILE_UNAVAILABLE"
    assert not profile.exists()


def test_account_creation_rejects_parent_traversal_segments(tmp_path: Path) -> None:
    profile = tmp_path / "profiles" / ".." / "traversed"

    with pytest.raises(BrowserDataError) as caught:
        AccountStore(tmp_path / "root").add(
            "pilot",
            "actor",
            profile,
            create_profile=True,
        )

    assert caught.value.code == "PROFILE_UNAVAILABLE"
    assert not (tmp_path / "traversed").exists()


def test_account_creation_rejects_symlinked_parent_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked-parent"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        link.mkdir()
        monkeypatch.setattr(
            storage_module,
            "_is_link_or_junction",
            lambda path: path == link,
        )

    with pytest.raises(BrowserDataError) as caught:
        AccountStore(tmp_path / "root").add(
            "pilot",
            "actor",
            link / "pilot",
            create_profile=True,
        )

    assert caught.value.code == "PROFILE_UNAVAILABLE"
    assert not (target / "pilot").exists()
    assert not (link / "pilot").exists()


def test_duplicate_alias_does_not_create_another_profile(tmp_path: Path) -> None:
    store = AccountStore(tmp_path / "root")
    first_profile = tmp_path / "first"
    first_profile.mkdir()
    store.add("pilot", "actor", first_profile)
    second_profile = tmp_path / "profiles" / "second"

    with pytest.raises(BrowserDataError) as caught:
        store.add("pilot", "other", second_profile, create_profile=True)

    assert caught.value.code == "ACCOUNT_EXISTS"
    assert not second_profile.exists()
