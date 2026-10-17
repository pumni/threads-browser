"""Versioned checkpoint persistence and validation."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from .accounts import ProcessLock, profile_lock
from .filesystem import _ensure_directory, _ensure_root, _read_json, _write_json
from .models import BrowserAccount, BrowserDataError, Checkpoint
from .schema import _CHECKPOINT_KEYS, _MAX_COUNTER, _MAX_SEEN, _SHA256


class CheckpointStore:
    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root

    def profile_lock(self, account: BrowserAccount) -> ProcessLock:
        """Use the same profile lock for CLI and direct Python workflow callers."""
        return profile_lock(self._data_root, account)

    def load(self, account_id: UUID, policy_fingerprint: str) -> Checkpoint:
        if _SHA256.fullmatch(policy_fingerprint) is None:
            raise BrowserDataError("STATE_INVALID")
        root = _ensure_root(self._data_root)
        directory = _ensure_directory(root, "nurture/checkpoints")
        target = directory / f"{account_id}-{policy_fingerprint}.json"
        document = _read_json(target)
        if document is None:
            return Checkpoint(account_id, policy_fingerprint)
        return _decode_checkpoint(document, account_id, policy_fingerprint)

    def save(self, checkpoint: Checkpoint) -> None:
        document = checkpoint.to_document()
        validated = _decode_checkpoint(
            document, checkpoint.account_id, checkpoint.policy_fingerprint
        )
        root = _ensure_root(self._data_root)
        directory = _ensure_directory(root, "nurture/checkpoints")
        target = directory / (
            f"{validated.account_id}-{validated.policy_fingerprint}.json"
        )
        _write_json(root, target, validated.to_document())


def _decode_checkpoint(
    document: dict[str, object],
    expected_account: UUID,
    expected_policy: str,
) -> Checkpoint:
    if (
        set(document) != _CHECKPOINT_KEYS
        or type(document.get("schema_version")) is not int
        or document.get("schema_version") != 3
    ):
        raise BrowserDataError("STATE_INVALID")
    if document.get("account_id") != str(expected_account):
        raise BrowserDataError("STATE_INVALID")
    if document.get("policy_fingerprint") != expected_policy or _SHA256.fullmatch(
        expected_policy
    ) is None:
        raise BrowserDataError("STATE_INVALID")
    counts: dict[str, int] = {}
    for key in (
        "completed_runs",
        "home_sampled",
        "home_new",
        "home_relevant",
        "home_irrelevant",
        "home_unknown",
        "duplicates",
        "detail_reads",
        "scroll_attempts",
        "scrolls",
    ):
        value = document.get(key)
        if type(value) is not int or value < 0 or value > _MAX_COUNTER:
            raise BrowserDataError("STATE_INVALID")
        counts[key] = value
    seen = document.get("seen_fingerprints")
    pending = document.get("pending_read_fingerprints")
    if (
        not isinstance(seen, list)
        or len(seen) > _MAX_SEEN
        or any(not isinstance(value, str) or _SHA256.fullmatch(value) is None for value in seen)
        or len(set(seen)) != len(seen)
    ):
        raise BrowserDataError("STATE_INVALID")
    if (
        not isinstance(pending, list)
        or len(pending) > _MAX_SEEN
        or any(
            not isinstance(value, str) or _SHA256.fullmatch(value) is None
            for value in pending
        )
        or len(set(pending)) != len(pending)
        or not set(pending).issubset(seen)
    ):
        raise BrowserDataError("STATE_INVALID")
    if (
        counts["home_relevant"] + counts["home_irrelevant"] + counts["home_unknown"]
        != counts["home_sampled"]
        or counts["home_new"] > counts["home_sampled"]
        or counts["detail_reads"] > counts["home_new"]
        or counts["scrolls"] > counts["scroll_attempts"]
        or counts["scroll_attempts"] > counts["completed_runs"] * 20
    ):
        raise BrowserDataError("STATE_INVALID")
    return Checkpoint(
        expected_account,
        expected_policy,
        **counts,
        pending_read_fingerprints=tuple(pending),
        seen_fingerprints=tuple(seen),
    )
