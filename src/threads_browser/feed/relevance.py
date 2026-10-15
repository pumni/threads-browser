"""Pure text normalization and literal relevance classification."""

from __future__ import annotations

import unicodedata

from ..errors import UiReadError


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def canonical_terms(terms: tuple[str, ...]) -> tuple[str, ...]:
    cleaned = tuple(dict.fromkeys(normalize_text(term) for term in terms if term.strip()))
    if not cleaned or any(len(term) > 100 for term in cleaned):
        raise UiReadError("TERMS_INVALID")
    return cleaned


def classify_body(body: str | None, terms: tuple[str, ...]) -> str:
    if body is None or not body.strip():
        return "UNKNOWN"
    normalized = normalize_text(body)
    return "RELEVANT" if any(term in normalized for term in terms) else "IRRELEVANT"
