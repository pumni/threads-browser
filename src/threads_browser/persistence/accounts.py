"""Dedicated profile registration, identity binding, and OS profile locks."""

from __future__ import annotations

import os
import sys
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO, Self
from uuid import UUID, uuid4

from .filesystem import _ensure_directory, _ensure_root, _read_json, _write_json
from .models import BrowserAccount, BrowserDataError
from .schema import _ACCOUNT_KEYS, _ACTOR, _ALIAS


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

    def list(self) -> tuple[BrowserAccount, ...]:
        root = _ensure_root(self._data_root)
        directory = _ensure_directory(root, "accounts")
        accounts: list[BrowserAccount] = []
        try:
            paths = tuple(directory.glob("*.json"))
        except OSError:
            raise BrowserDataError("STATE_INVALID") from None
        for path in sorted(paths, key=lambda item: item.name.casefold()):
            alias = path.stem
            if _ALIAS.fullmatch(alias) is None or path.name != f"{alias}.json":
                raise BrowserDataError("ACCOUNT_INVALID")
            document = _read_json(path)
            if document is None:
                raise BrowserDataError("ACCOUNT_INVALID")
            accounts.append(self._decode_account(alias, document, root))
        return tuple(accounts)

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


def profile_lock(data_root: Path, account: BrowserAccount) -> ProcessLock:
    root = _ensure_root(data_root)
    profile_key = sha256(
        os.path.normcase(str(account.profile_directory)).casefold().encode("utf-8")
    ).hexdigest()
    return ProcessLock(root / "locks" / f"profile-{profile_key}.lock")
