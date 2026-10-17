"""Private-root validation and bounded atomic JSON file primitives."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .models import BrowserDataError
from .schema import _DATA_ROOT_ENV, _MAX_DOCUMENT_BYTES


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
