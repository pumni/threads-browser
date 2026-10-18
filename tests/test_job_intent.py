from __future__ import annotations

import pytest

from threads_browser.feed.job_intent import classify_auto_body, classify_job_intent


@pytest.mark.parametrize(
    ("body", "expected"),
    (
        ("Bên mình đang tuyển nhân viên kinh doanh tại Hà Nội.", "HIRING"),
        ("Công ty đang cần tuyển 2 lập trình viên Backend.", "HIRING"),
        ("Chúng tôi đang tuyển QA Engineer cho nhóm sản phẩm.", "HIRING"),
        ("Mình đang tìm việc làm ở Đà Nẵng.", "SEEKING_JOB"),
        ("Em đang tìm công việc QA Tester tại TP.HCM.", "SEEKING_JOB"),
        ("Tôi đang tìm việc lập trình Backend.", "SEEKING_JOB"),
        ("Ben minh dang tuyen nhan vien kinh doanh", "HIRING"),
        ("Minh dang tim viec lam", "SEEKING_JOB"),
        (
            "Không tuyển nhân viên nữa. Công ty đang cần tuyển 2 lập trình viên Backend.",
            "HIRING",
        ),
        (
            "Bên mình đang tuyển QA Engineer. Đã tuyển đủ người cho vị trí khác.",
            "HIRING",
        ),
        (
            (
                "Bên mình đang tuyển QA Engineer. Đã tuyển đủ người cho vị trí này. "
                "Hiện tại bên mình đang tuyển Backend Developer."
            ),
            "HIRING",
        ),
        (
            (
                "Bên mình đang tuyển QA Engineer. Bên mình đang tuyển Backend Developer. "
                "Đã tuyển đủ Backend Developer cho vị trí này."
            ),
            "HIRING",
        ),
        (
            "Bên mình đã tuyển đủ người cho vị trí này, nhưng bên mình đang tuyển QA Engineer.",
            "HIRING",
        ),
        ("Không tuyển nhân viên nữa, nhưng bên mình đang tuyển QA Engineer.", "HIRING"),
        ("Bên mình không tuyển nhân viên nữa, nhưng đang tuyển QA Engineer.", "HIRING"),
        ("Công ty không còn tuyển nhân viên, còn tôi đang tìm việc làm.", "SEEKING_JOB"),
        (
            "Bên mình đã tuyển đủ người; nhưng hiện tại bên mình đang tuyển QA Engineer.",
            "HIRING",
        ),
        (
            "Bên mình không nhận hồ sơ nữa, nhưng hiện tại đang tuyển QA Engineer.",
            "HIRING",
        ),
        (
            "Bên mình đang tuyển nhân viên kinh doanh, nhưng khóa học kỹ năng phỏng vấn cũng đang mở.",
            "HIRING",
        ),
        (
            "Công ty khác đang tuyển QA Engineer, nhưng bên mình đang tuyển Backend Developer.",
            "HIRING",
        ),
        (
            "Năm ngoái mình từng tìm việc, nhưng hiện tại mình đang tìm việc làm.",
            "SEEKING_JOB",
        ),
    ),
    ids=(
        "vi_hiring_employee",
        "vi_hiring_backend",
        "vi_hiring_english_title",
        "vi_seeking_da_nang",
        "vi_seeking_qa_title",
        "vi_seeking_backend",
        "unaccented_hiring_structure",
        "unaccented_seeking_structure",
        "positive_sentence_after_closing_sentence",
        "positive_sentence_before_other_closure",
        "independent_current_opening_after_closure",
        "specific_closure_keeps_other_open_role",
        "positive_clause_after_closing_clause",
        "positive_clause_after_negation",
        "inherited_subject_after_negation",
        "negation_in_other_intent_group",
        "current_hiring_after_closed_clause",
        "current_hiring_after_application_closure",
        "job_intent_with_independent_course_clause",
        "independent_author_clause_after_third_party_company",
        "current_seeking_after_past_clause",
    ),
)
def test_vietnamese_current_job_intent_is_eligible(body: str, expected: str) -> None:
    assert classify_job_intent(body) == expected
    assert classify_auto_body(body) == "RELEVANT"


@pytest.mark.parametrize(
    "body",
    (
        "Không tuyển nhân viên nữa.",
        "Không tuyển nhân viên.",
        "Bên mình chưa tuyển dụng.",
        "Đã tuyển đủ người cho vị trí này.",
        "Mình không tìm việc.",
        "Mình đã tìm được việc rồi.",
        "Chia sẻ kinh nghiệm tìm việc và sửa CV.",
        "Bạn đang tìm việc? Đây là 5 mẹo phỏng vấn.",
        "Quảng cáo khóa học tuyển dụng, đăng ký ngay.",
        "Tuyển sinh workshop đào tạo kỹ năng phỏng vấn.",
        "Phân tích thị trường lao động và cơ hội nghề nghiệp.",
        "Chia sẻ cơ hội của người khác đang tuyển nhân viên.",
        "We're hiring an engineer.",
        "Now hiring Backend Developers.",
        "Open to work.",
        "Looking for a job in Vietnam.",
        "Công ty đang tuyển QA Engineer, but the position is filled.",
        "Bên mình đang tuyển QA Engineer for our team, nhưng đang tuyển Backend Developer.",
        "Bên mình đang tuyển QA Engineer, nhưng đã tuyển đủ người cho vị trí này.",
        "Bên mình đang tuyển QA Engineer; nhưng đã tuyển đủ người.",
        "Bên mình đang tuyển nhân viên, không nhận hồ sơ nữa.",
        "Bên mình đang tuyển QA Engineer. Đã tuyển đủ người cho vị trí này.",
        "Bên mình đang tuyển nhân viên kinh doanh. Nhưng không nhận hồ sơ nữa.",
        "Bên mình đang tuyển QA Engineer. Đã tuyển đủ QA Engineer cho vị trí này.",
        "Năm ngoái mình đang tìm việc làm ở Đà Nẵng.",
        "Hồi trước bên mình đang tuyển nhân viên kinh doanh.",
        "Năm ngoái công ty đang cần tuyển QA Engineer.",
        "Nam ngoai, ben minh dang tuyen nhan vien kinh doanh.",
        "Năm ngoái mình đang tìm việc làm, nhưng chưa xác định hướng nghề nghiệp.",
        "Nếu mình đang tìm việc thì nên sửa CV trước.",
        "'Bên mình đang tuyển nhân viên kinh doanh.'",
        "Mình tìm việc làm.",
        "Mot cong ty dang tuyen QA Engineer.",
        "Công ty khác đang tuyển QA Engineer.",
        "Một công ty khác đang tuyển QA Engineer.",
        "Công ty của bạn đang tuyển QA Engineer.",
        "Bạn Minh đang tìm việc làm.",
        "Bạn Lan đang tìm việc làm.",
        "Ban Minh dang tim viec lam.",
        "Minh khong tim viec.",
        "Minh da tim duoc viec roi.",
        "Minh Anh dang tim viec.",
        "Nguyen Van Anh dang tim viec lam.",
        "Vietnam team hire QA engineer.",
        "Career advice about the word engineer.",
        "Khóa học về hiring an engineer.",
    ),
    ids=(
        "hiring_closed",
        "hiring_negative_without_nua",
        "hiring_not_started",
        "hiring_completed",
        "seeking_negative",
        "seeking_completed",
        "job_search_advice",
        "question_and_advice",
        "course_advertisement",
        "workshop_training",
        "labor_market_commentary",
        "other_persons_opportunity",
        "english_hiring",
        "english_now_hiring",
        "english_open_to_work",
        "english_seeking",
        "mixed_language_conflict",
        "mixed_english_subject_does_not_carry",
        "same_sentence_closure_after_positive",
        "semicolon_closure_after_positive",
        "application_closure_after_positive",
        "same_vacancy_closed_in_next_sentence",
        "applications_closed_in_next_sentence",
        "specific_vacancy_closed_in_next_sentence",
        "past_seeking_job",
        "past_hiring_from_company",
        "past_hiring_qa",
        "unaccented_past_hiring",
        "past_context_across_clause_boundary",
        "conditional_advice",
        "quoted_intent",
        "missing_current_marker",
        "unaccented_third_party_company",
        "third_party_company",
        "another_third_party_company",
        "reader_company",
        "third_party_named_seeker_minh",
        "third_party_named_seeker_lan",
        "unaccented_third_party_named_seeker",
        "unaccented_seeking_negative",
        "unaccented_seeking_completed",
        "unaccented_person_name",
        "unaccented_full_name",
        "english_company_context",
        "english_career_advice",
        "course_with_english_role",
    ),
)
def test_uncertain_or_non_current_context_is_ineligible(body: str) -> None:
    assert classify_job_intent(body) == "OTHER"
    assert classify_auto_body(body) == "IRRELEVANT"


def test_conflicting_current_intents_fail_closed() -> None:
    body = "Bên mình đang tuyển QA Engineer. Mình đang tìm việc làm ở Đà Nẵng."
    assert classify_job_intent(body) == "OTHER"
    assert classify_auto_body(body) == "IRRELEVANT"


def test_missing_body_remains_unknown() -> None:
    assert classify_job_intent(None) == "UNKNOWN"
    assert classify_job_intent("   ") == "UNKNOWN"
    assert classify_auto_body(None) == "UNKNOWN"
