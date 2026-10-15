"""Pure feed boundaries and import-compatibility smoke tests."""

import pytest

from threads_browser.errors import UiReadError
from threads_browser.feed import DisplayObservation, HomeSample, PostTarget
from threads_browser.feed.relevance import canonical_terms, classify_body, normalize_text
from threads_browser.feed.urls import _post_url, _profile_author, _same_home_origin
from threads_browser.reader import (
    DisplayObservation as ReaderDisplayObservation,
    HomeSample as ReaderHomeSample,
    PostTarget as ReaderPostTarget,
    UiReadError as ReaderUiReadError,
)


def test_reader_exports_keep_identity() -> None:
    assert DisplayObservation is ReaderDisplayObservation
    assert HomeSample is ReaderHomeSample
    assert PostTarget is ReaderPostTarget
    assert UiReadError is ReaderUiReadError


def test_feed_pure_normalization_and_classification() -> None:
    terms = canonical_terms(("  Hiring  ", "HIRING", "đang tuyển"))
    assert terms == ("hiring", "đang tuyển")
    assert normalize_text("  ＨＩＲＩＮＧ ") == "hiring"
    assert classify_body("We are HIRING", terms) == "RELEVANT"
    assert classify_body("Unrelated", terms) == "IRRELEVANT"
    assert classify_body(None, terms) == "UNKNOWN"
    with pytest.raises(UiReadError) as error:
        canonical_terms((" ",))
    assert error.value.code == "TERMS_INVALID"


def test_url_provenance_is_strict_and_on_origin() -> None:
    assert _post_url("/@Alice/post/AbC_123") == (
        "https://www.threads.com/@Alice/post/AbC_123",
        "alice",
    )
    assert _post_url("/@alice/post/AbC_123?tracking=1") is None
    assert _post_url("https://example.com/@alice/post/AbC_123") is None
    assert _profile_author("/@Alice") == "alice"
    assert _same_home_origin("https://www.threads.com/")
    assert not _same_home_origin("https://example.com/")
