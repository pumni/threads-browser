"""Private per-account relevance term persistence."""

from __future__ import annotations

import unicodedata
from pathlib import Path

from .filesystem import _ensure_directory, _ensure_root, _read_json, _write_json
from .models import BrowserDataError
from .schema import _ALIAS


class TermsStore:
    """Private per-account relevance terms, separate from account and browser data."""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root

    def get(self, alias: str) -> tuple[str, ...]:
        path, _ = self._path(alias)
        document = _read_json(path)
        if document is None:
            return ()
        if (
            set(document) != {"schema_version", "alias", "terms"}
            or document.get("schema_version") != 1
            or document.get("alias") != alias
            or type(document.get("schema_version")) is not int
        ):
            raise BrowserDataError("STATE_INVALID")
        raw_terms = document.get("terms")
        if not isinstance(raw_terms, list) or any(
            not isinstance(term, str) for term in raw_terms
        ):
            raise BrowserDataError("STATE_INVALID")
        try:
            normalized = _normalize_terms(tuple(raw_terms))
        except BrowserDataError:
            raise BrowserDataError("STATE_INVALID") from None
        if normalized != tuple(raw_terms):
            raise BrowserDataError("STATE_INVALID")
        return normalized

    def set(self, alias: str, terms: tuple[str, ...]) -> tuple[str, ...]:
        normalized = _normalize_terms(terms, allow_empty=False)
        path, root = self._path(alias)
        _write_json(
            root,
            path,
            {"schema_version": 1, "alias": alias, "terms": list(normalized)},
        )
        return normalized

    def clear(self, alias: str) -> None:
        path, root = self._path(alias)
        _write_json(
            root,
            path,
            {"schema_version": 1, "alias": alias, "terms": []},
        )

    def _path(self, alias: str) -> tuple[Path, Path]:
        if _ALIAS.fullmatch(alias) is None:
            raise BrowserDataError("ACCOUNT_INVALID")
        root = _ensure_root(self._data_root)
        directory = _ensure_directory(root, "preferences/terms")
        target = directory / f"{alias}.json"
        if target.is_symlink() or not target.resolve(strict=False).is_relative_to(root):
            raise BrowserDataError("STATE_PATH_INVALID")
        return target, root


def _normalize_terms(terms: tuple[str, ...], *, allow_empty: bool = True) -> tuple[str, ...]:
    if any(not isinstance(term, str) for term in terms):
        raise BrowserDataError("STATE_INVALID")
    normalized = tuple(
        dict.fromkeys(unicodedata.normalize("NFKC", term).casefold().strip() for term in terms)
    )
    if (
        (not allow_empty and not normalized)
        or any(not term or len(term) > 100 for term in normalized)
    ):
        raise BrowserDataError("TERMS_INVALID")
    return normalized
