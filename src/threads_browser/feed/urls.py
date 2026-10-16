"""Canonical on-origin Threads post and profile URL validation."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

THREADS_ORIGIN = "https://www.threads.com"
HOME_URL = f"{THREADS_ORIGIN}/"
_USERNAME = re.compile(r"[A-Za-z0-9._]{1,30}")
_POST_PATH = re.compile(
    r"/@(?P<author>[A-Za-z0-9._]{1,30})/post/(?P<post>[A-Za-z0-9_-]{1,120})/?"
)
_PROFILE_PATH = re.compile(r"/@(?P<author>[A-Za-z0-9._]{1,30})/?")
_CandidateDiagnosticReason = Literal[
    "POST_LINK_HAS_QUERY_OR_FRAGMENT",
    "POST_PATH_SHAPE_INVALID",
    "POST_ORIGIN_INVALID",
    "POST_AUTHOR_OR_ID_INVALID",
    "CANDIDATE_WRAPPER_AMBIGUOUS",
    "CANDIDATE_AUTHOR_MISMATCH",
]


def _post_url(value: str) -> tuple[str, str] | None:
    try:
        parsed = urlsplit(urljoin(HOME_URL, value))
    except ValueError:
        return None
    match = _POST_PATH.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != "www.threads.com"
        or parsed.query
        or parsed.fragment
        or match is None
    ):
        return None
    canonical = urlunsplit(("https", "www.threads.com", parsed.path, "", ""))
    canonical = canonical.removesuffix("/")
    return canonical, match.group("author").casefold()


def _post_url_hint(value: str) -> bool:
    """Identify post-like paths without mistaking query or fragment text for a path."""
    try:
        return "post" in urlsplit(value).path.split("/")
    except ValueError:
        return False


def _post_url_structure(value: str) -> tuple[int, bool, bool, bool, bool]:
    """Return privacy-safe URL shape facts without retaining or exposing its value."""
    try:
        source = urlsplit(value)
        resolved = urlsplit(urljoin(HOME_URL, value))
    except ValueError:
        return 0, False, False, False, not value.startswith("http")
    segments = sum(bool(segment) for segment in resolved.path.split("/"))
    return (
        segments,
        resolved.path.endswith("/"),
        bool(source.query),
        bool(source.fragment),
        not bool(source.scheme or source.netloc),
    )


def _post_url_rejection_reason(value: str) -> _CandidateDiagnosticReason:
    try:
        parsed = urlsplit(urljoin(HOME_URL, value))
    except ValueError:
        return "POST_ORIGIN_INVALID"
    if parsed.query or parsed.fragment:
        return "POST_LINK_HAS_QUERY_OR_FRAGMENT"
    if parsed.scheme != "https" or parsed.netloc.casefold() != "www.threads.com":
        return "POST_ORIGIN_INVALID"
    segments = parsed.path.split("/")
    if len(segments) != 4 or segments[0] != "" or segments[2] != "post":
        return "POST_PATH_SHAPE_INVALID"
    author = segments[1][1:] if segments[1].startswith("@") else ""
    post_id = segments[3]
    if _USERNAME.fullmatch(author) is None or re.fullmatch(
        r"[A-Za-z0-9_-]{1,120}", post_id
    ) is None:
        return "POST_AUTHOR_OR_ID_INVALID"
    return "POST_PATH_SHAPE_INVALID"


def _rejected_post_link_blocks_candidate(
    *, timestamp_link: bool, reason: _CandidateDiagnosticReason
) -> bool:
    """Apply the same ambiguity rule to primary and secondary post-like links."""
    return timestamp_link or reason == "POST_ORIGIN_INVALID"


def _profile_author(value: str) -> str | None:
    try:
        parsed = urlsplit(urljoin(HOME_URL, value))
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.netloc.casefold() != "www.threads.com"
        or parsed.query
        or parsed.fragment
    ):
        return None
    match = _PROFILE_PATH.fullmatch(parsed.path)
    return None if match is None else match.group("author").casefold()


def _same_home_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.netloc.casefold() == "www.threads.com"
