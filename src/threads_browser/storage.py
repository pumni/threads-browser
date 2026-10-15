"""Private account and checkpoint storage for the independent Browser CLI."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO, Self
from uuid import UUID, uuid4

_DATA_ROOT_ENV = "THREADS_BROWSER_DATA_ROOT"
_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_ACTOR = re.compile(r"[A-Za-z0-9._]{1,30}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_ACCOUNT_KEYS = {
    "schema_version",
    "account_id",
    "alias",
    "expected_actor",
    "profile_directory",
}
_CHECKPOINT_KEYS = {
    "schema_version",
    "account_id",
    "policy_fingerprint",
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
    "pending_read_fingerprints",
    "seen_fingerprints",
}
_MAX_DOCUMENT_BYTES = 256 * 1024
_MAX_COUNTER = 1_000_000_000_000
_MAX_SEEN = 5_000
_ERROR_CODES = {
    "ACCOUNT_BUSY",
    "ACCOUNT_INVALID",
    "ACCOUNT_NOT_FOUND",
    "ACCOUNT_EXISTS",
    "ACCOUNT_NOT_VERIFIED",
    "ACTOR_MISMATCH",
    "DATA_ROOT_INVALID",
    "DATA_ROOT_UNAVAILABLE",
    "PROFILE_UNAVAILABLE",
    "PROFILE_EXISTS",
    "LOCK_UNAVAILABLE",
    "STATE_INVALID",
    "STATE_PATH_INVALID",
}


class BrowserDataError(Exception):
    code: str

    def __init__(self, code: str) -> None:
        if code not in _ERROR_CODES:
            raise ValueError("unsupported browser data error")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class BrowserAccount:
    account_id: UUID
    alias: str
    expected_actor: str | None = field(repr=False)
    profile_directory: Path = field(repr=False)

    def __repr__(self) -> str:
        return "BrowserAccount(<private>)"


def normalize_actor(value: object) -> str:
    if not isinstance(value, str):
        raise BrowserDataError("ACCOUNT_INVALID")
    actor = value.strip()
    actor = actor.removeprefix("@")
    if _ACTOR.fullmatch(actor) is None:
        raise BrowserDataError("ACCOUNT_INVALID")
    return actor.casefold()


def ensure_safe_browser_profile(profile_directory: Path) -> None:
    """Reject the default desktop browser data roots without reading their contents."""
    try:
        candidate = profile_directory.expanduser().resolve(strict=False)
        if os.name == "nt":
            local_app_data = os.environ.get("LOCALAPPDATA")
            local = (
                Path(local_app_data)
                if local_app_data is not None and local_app_data.strip()
                else Path.home() / "AppData" / "Local"
            )
            roots = tuple(
                local / "Google" / name / "User Data"
                for name in ("Chrome", "Chrome Beta", "Chrome Dev", "Chrome SxS")
            ) + tuple(
                local / "Microsoft" / name / "User Data"
                for name in ("Edge", "Edge Beta", "Edge Dev", "Edge SxS")
            )
        elif sys.platform == "darwin":
            support = Path.home() / "Library" / "Application Support"
            roots = tuple(
                support / "Google" / name
                for name in ("Chrome", "Chrome Beta", "Chrome Dev", "Chrome Canary")
            ) + tuple(
                support / name
                for name in ("Microsoft Edge", "Microsoft Edge Beta", "Microsoft Edge Dev", "Microsoft Edge Canary")
            )
        else:
            config = Path.home() / ".config"
            roots = tuple(
                config / name
                for name in (
                    "google-chrome",
                    "google-chrome-beta",
                    "google-chrome-unstable",
                    "chromium",
                    "microsoft-edge",
                    "microsoft-edge-beta",
                    "microsoft-edge-dev",
                    "microsoft-edge-canary",
                )
            )
        candidate_path = os.path.normcase(os.path.abspath(candidate)).casefold()
        for root in roots:
            root_path = (
                os.path.normcase(os.path.abspath(root.expanduser().resolve(strict=False)))
                .casefold()
            )
            try:
                if os.path.commonpath((candidate_path, root_path)) == root_path:
                    raise BrowserDataError("PROFILE_UNAVAILABLE")
            except ValueError:
                continue
    except BrowserDataError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise BrowserDataError("PROFILE_UNAVAILABLE") from None


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _create_dedicated_profile(profile_directory: Path) -> Path:
    """Create a previously absent profile without following existing path links."""
    try:
        candidate = Path(os.path.abspath(profile_directory))
        parts = candidate.parts
        if len(parts) < 2:
            raise BrowserDataError("PROFILE_UNAVAILABLE")

        current = Path(candidate.anchor)
        if _is_link_or_junction(current):
            raise BrowserDataError("PROFILE_UNAVAILABLE")
        for part in parts[1:-1]:
            current = current / part
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            if _is_link_or_junction(current) or not current.is_dir():
                raise BrowserDataError("PROFILE_UNAVAILABLE")

        target = current / parts[-1]
        try:
            target.mkdir(mode=0o700)
        except FileExistsError:
            raise BrowserDataError("PROFILE_EXISTS") from None
        if _is_link_or_junction(target) or not target.is_dir():
            raise BrowserDataError("PROFILE_UNAVAILABLE")
        return target.resolve(strict=True)
    except BrowserDataError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise BrowserDataError("PROFILE_UNAVAILABLE") from None


def resolve_data_root(environment: Mapping[str, str] | None = None) -> Path:
    values = os.environ if environment is None else environment
    override = values.get(_DATA_ROOT_ENV)
    try:
        if override is not None:
            if not override.strip():
                raise BrowserDataError("DATA_ROOT_INVALID")
            return Path(override).expanduser().resolve(strict=False)
        if os.name == "nt":
            local_app_data = values.get("LOCALAPPDATA")
            if local_app_data is None or not local_app_data.strip():
                raise BrowserDataError("DATA_ROOT_UNAVAILABLE")
            return (Path(local_app_data) / "ThreadsBrowser").resolve(strict=False)
        return (Path.home() / ".local" / "share" / "threads-browser").resolve(strict=False)
    except BrowserDataError:
        raise
    except (OSError, RuntimeError):
        raise BrowserDataError("DATA_ROOT_INVALID") from None


def _ensure_root(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        root = path.resolve(strict=True)
        if not root.is_dir():
            raise OSError
        if os.name != "nt":
            os.chmod(root, 0o700)
        return root
    except OSError:
        raise BrowserDataError("DATA_ROOT_UNAVAILABLE") from None


def _ensure_directory(root: Path, name: str) -> Path:
    directory = root
    try:
        for part in Path(name).parts:
            candidate = directory / part
            if candidate.is_symlink():
                raise BrowserDataError("STATE_PATH_INVALID")
            candidate.mkdir(exist_ok=True, mode=0o700)
            resolved = candidate.resolve(strict=True)
            if not resolved.is_dir() or not resolved.is_relative_to(root):
                raise BrowserDataError("STATE_PATH_INVALID")
            if os.name != "nt":
                os.chmod(resolved, 0o700)
            directory = resolved
        return directory
    except BrowserDataError:
        raise
    except OSError:
        raise BrowserDataError("DATA_ROOT_UNAVAILABLE") from None


def _account_path(root: Path, alias: str) -> Path:
    if _ALIAS.fullmatch(alias) is None:
        raise BrowserDataError("ACCOUNT_INVALID")
    directory = _ensure_directory(root, "accounts")
    target = directory / f"{alias}.json"
    if not target.resolve(strict=False).is_relative_to(root) or target.is_symlink():
        raise BrowserDataError("STATE_PATH_INVALID")
    return target


class ProcessLock:
    """A non-blocking OS file lock; the OS releases it when the process exits."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._stream: BinaryIO | None = None

    def acquire(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self._path.parent.is_symlink() or self._path.is_symlink():
                raise OSError
            stream = self._path.open("a+b")
            if os.name != "nt":
                os.chmod(self._path, 0o600)
        except OSError:
            raise BrowserDataError("LOCK_UNAVAILABLE") from None
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise BrowserDataError("ACCOUNT_BUSY") from None
        self._stream = stream

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            raise BrowserDataError("LOCK_UNAVAILABLE") from None
        finally:
            stream.close()
            self._stream = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class AccountStore:
    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root

    def add(
        self,
        alias: str,
        actor: str | None = None,
        profile_directory: Path | None = None,
        *,
        create_profile: bool = False,
    ) -> BrowserAccount:
        if _ALIAS.fullmatch(alias) is None:
            raise BrowserDataError("ACCOUNT_INVALID")
        if (actor is None) != (profile_directory is None):
            raise BrowserDataError("ACCOUNT_INVALID")

        local_account = actor is None
        expected_actor = None if local_account else normalize_actor(actor)
        try:
            root_candidate = self._data_root.expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            raise BrowserDataError("DATA_ROOT_INVALID") from None
        if local_account:
            profile_candidate = root_candidate / "profiles" / alias
            ensure_safe_browser_profile(profile_candidate)
            root = _ensure_root(root_candidate)
            profile_candidate = root / "profiles" / alias
            ensure_safe_browser_profile(profile_candidate)
            should_create_profile = True
            schema_version = 2
        else:
            try:
                expanded_profile = profile_directory.expanduser()
                if create_profile and ".." in expanded_profile.parts:
                    raise BrowserDataError("PROFILE_UNAVAILABLE")
                profile_candidate = (
                    Path(os.path.abspath(expanded_profile))
                    if create_profile
                    else expanded_profile.resolve(strict=False)
                )
            except (OSError, RuntimeError, ValueError):
                raise BrowserDataError("PROFILE_UNAVAILABLE") from None
            if create_profile:
                ensure_safe_browser_profile(profile_candidate)
                should_create_profile = True
            else:
                try:
                    profile = profile_directory.expanduser().resolve(strict=True)
                except (OSError, RuntimeError, ValueError):
                    raise BrowserDataError("PROFILE_UNAVAILABLE") from None
                if not profile.is_dir():
                    raise BrowserDataError("PROFILE_UNAVAILABLE")
                ensure_safe_browser_profile(profile)
                should_create_profile = False
            schema_version = 1

        root = _ensure_root(root_candidate)
        lock_path = root / "locks" / "registry.lock"
        with ProcessLock(lock_path):
            target = _account_path(root, alias)
            if target.exists():
                raise BrowserDataError("ACCOUNT_EXISTS")
            created_profile = False
            if should_create_profile:
                profile = _create_dedicated_profile(profile_candidate)
                created_profile = True
            account = BrowserAccount(uuid4(), alias, expected_actor, profile)
            document = {
                "schema_version": schema_version,
                "account_id": str(account.account_id),
                "alias": account.alias,
                "expected_actor": account.expected_actor,
                "profile_directory": str(account.profile_directory),
            }
            try:
                _write_json(root, target, document)
            except Exception:
                if created_profile:
                    try:
                        profile.rmdir()
                    except OSError:
                        pass
                raise
            return account

    def get(self, alias: str) -> BrowserAccount:
        if _ALIAS.fullmatch(alias) is None:
            raise BrowserDataError("ACCOUNT_INVALID")
        root = _ensure_root(self._data_root)
        target = _account_path(root, alias)
        document = _read_json(target)
        if document is None:
            raise BrowserDataError("ACCOUNT_NOT_FOUND")
        return self._decode_account(alias, document, root)

    def bind_actor(self, account: BrowserAccount, actor: str) -> BrowserAccount:
        """Atomically bind an unverified local account to its first verified actor."""
        expected_actor = normalize_actor(actor)
        root = _ensure_root(self._data_root)
        with ProcessLock(root / "locks" / "registry.lock"):
            target = _account_path(root, account.alias)
            document = _read_json(target)
            if document is None:
                raise BrowserDataError("ACCOUNT_NOT_FOUND")
            current = self._decode_account(account.alias, document, root)
            if (
                current.account_id != account.account_id
                or current.profile_directory != account.profile_directory
            ):
                raise BrowserDataError("ACCOUNT_INVALID")
            if current.expected_actor is not None:
                if current.expected_actor != expected_actor:
                    raise BrowserDataError("ACTOR_MISMATCH")
                return current
            if document.get("schema_version") != 2:
                raise BrowserDataError("ACCOUNT_INVALID")
            document["expected_actor"] = expected_actor
            _write_json(root, target, document)
            return BrowserAccount(
                current.account_id,
                current.alias,
                expected_actor,
                current.profile_directory,
            )

    @staticmethod
    def _decode_account(
        alias: str,
        document: dict[str, object],
        root: Path,
    ) -> BrowserAccount:
        schema_version = document.get("schema_version")
        if (
            set(document) != _ACCOUNT_KEYS
            or type(schema_version) is not int
            or schema_version not in {1, 2}
        ):
            raise BrowserDataError("ACCOUNT_INVALID")
        raw_id = document.get("account_id")
        raw_alias = document.get("alias")
        raw_actor = document.get("expected_actor")
        raw_profile = document.get("profile_directory")
        if (
            not isinstance(raw_id, str)
            or raw_alias != alias
            or (schema_version == 1 and not isinstance(raw_actor, str))
            or (schema_version == 2 and raw_actor is not None and not isinstance(raw_actor, str))
            or not isinstance(raw_profile, str)
        ):
            raise BrowserDataError("ACCOUNT_INVALID")
        try:
            account_id = UUID(raw_id)
            expected_actor = None if raw_actor is None else normalize_actor(raw_actor)
        except (ValueError, BrowserDataError):
            raise BrowserDataError("ACCOUNT_INVALID") from None
        if (
            str(account_id) != raw_id
            or (expected_actor is not None and expected_actor != raw_actor)
        ):
            raise BrowserDataError("ACCOUNT_INVALID")
        profile = Path(raw_profile)
        if not profile.is_absolute():
            raise BrowserDataError("ACCOUNT_INVALID")
        try:
            resolved_profile = profile.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise BrowserDataError("PROFILE_UNAVAILABLE") from None
        if resolved_profile != profile or not profile.is_dir():
            raise BrowserDataError("PROFILE_UNAVAILABLE")
        ensure_safe_browser_profile(profile)
        if schema_version == 2:
            expected_profile = root / "profiles" / alias
            try:
                expected_profile = expected_profile.resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                raise BrowserDataError("PROFILE_UNAVAILABLE") from None
            if resolved_profile != expected_profile:
                raise BrowserDataError("ACCOUNT_INVALID")
        return BrowserAccount(account_id, alias, expected_actor, profile)

@dataclass(frozen=True, slots=True, repr=False)
class Checkpoint:
    account_id: UUID
    policy_fingerprint: str
    completed_runs: int = 0
    home_sampled: int = 0
    home_new: int = 0
    home_relevant: int = 0
    home_irrelevant: int = 0
    home_unknown: int = 0
    duplicates: int = 0
    detail_reads: int = 0
    scroll_attempts: int = 0
    scrolls: int = 0
    pending_read_fingerprints: tuple[str, ...] = field(default=(), repr=False)
    seen_fingerprints: tuple[str, ...] = field(default=(), repr=False)
    schema_version: int = 3

    def __repr__(self) -> str:
        return (
            "Checkpoint("
            f"completed_runs={self.completed_runs}, home_sampled={self.home_sampled}, "
            f"detail_reads={self.detail_reads}, seen_count={len(self.seen_fingerprints)})"
        )

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "account_id": str(self.account_id),
            "policy_fingerprint": self.policy_fingerprint,
            "completed_runs": self.completed_runs,
            "home_sampled": self.home_sampled,
            "home_new": self.home_new,
            "home_relevant": self.home_relevant,
            "home_irrelevant": self.home_irrelevant,
            "home_unknown": self.home_unknown,
            "duplicates": self.duplicates,
            "detail_reads": self.detail_reads,
            "scroll_attempts": self.scroll_attempts,
            "scrolls": self.scrolls,
            "pending_read_fingerprints": list(self.pending_read_fingerprints),
            "seen_fingerprints": list(self.seen_fingerprints),
        }


class CheckpointStore:
    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root

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


def profile_lock(data_root: Path, account: BrowserAccount) -> ProcessLock:
    root = _ensure_root(data_root)
    profile_key = sha256(
        os.path.normcase(str(account.profile_directory)).casefold().encode("utf-8")
    ).hexdigest()
    return ProcessLock(root / "locks" / f"profile-{profile_key}.lock")


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


def _read_json(path: Path) -> dict[str, object] | None:
    if path.is_symlink():
        raise BrowserDataError("STATE_PATH_INVALID")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise BrowserDataError("STATE_INVALID") from None
    if len(raw) > _MAX_DOCUMENT_BYTES:
        raise BrowserDataError("STATE_INVALID")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise BrowserDataError("STATE_INVALID") from None
    if not isinstance(value, dict):
        raise BrowserDataError("STATE_INVALID")
    return value


def _write_json(root: Path, target: Path, document: dict[str, object]) -> None:
    if target.is_symlink() or not target.resolve(strict=False).is_relative_to(root):
        raise BrowserDataError("STATE_PATH_INVALID")
    try:
        payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if len(payload) > _MAX_DOCUMENT_BYTES:
            raise BrowserDataError("STATE_INVALID")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".threads-browser-",
            suffix=".tmp",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
            if os.name != "nt":
                os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    except BrowserDataError:
        raise
    except OSError:
        raise BrowserDataError("STATE_INVALID") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("invalid JSON constant")
