from __future__ import annotations

import pytest

from threads_browser.feed.job_intent import classify_auto_body, classify_job_intent


@pytest.mark.parametrize(
    ("body", "expected"),
    (
        ("Chúng tôi đang tuyển nhân viên kinh doanh.", "HIRING"),
        ("Tuyển 2 lập trình viên cho nhóm sản phẩm.", "HIRING"),
        ("We're hiring an engineer for our team.", "HIRING"),
        ("Hiring engineers for a synthetic team.", "HIRING"),
        ("Mình đang tìm việc làm ở Đà Nẵng.", "SEEKING_JOB"),
        ("Open to work after five years in product.", "SEEKING_JOB"),
        ("I am looking for a job in design.", "SEEKING_JOB"),
        ("Chia sẻ kinh nghiệm tìm việc và sửa CV.", "OTHER"),
        ("Career advice about the word engineer.", "OTHER"),
        ("Khóa học tuyển dụng cho HR, đăng ký ngay.", "OTHER"),
        ("A course about hiring a manager is open for enrollment.", "OTHER"),
        ("Chúng tôi đang tuyển engineers, but the position is filled.", "OTHER"),
        ("Công ty không còn tuyển backend engineers.", "OTHER"),
        ("Tôi đang tìm việc nhưng đã tìm được việc.", "OTHER"),
        ("We're hiring and I am looking for a job.", "OTHER"),
        ("", "UNKNOWN"),
    ),
)
def test_auto_job_intent_requires_clear_current_intent(body: str, expected: str) -> None:
    assert classify_job_intent(body) == expected
    relevance = {
        "HIRING": "RELEVANT",
        "SEEKING_JOB": "RELEVANT",
        "OTHER": "IRRELEVANT",
        "UNKNOWN": "UNKNOWN",
    }
    assert classify_auto_body(body) == (
        relevance[expected]
    )


def test_raw_keyword_does_not_make_a_post_eligible() -> None:
    for body in ("Việc làm hôm nay.", "Engineer stories from my career.", "Hiring tips."):
        assert classify_job_intent(body) == "OTHER"
