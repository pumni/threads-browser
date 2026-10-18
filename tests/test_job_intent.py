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
            (
                "TUYỂN GẤP 3 NHÂN VIÊN PHỤC VỤ. Lương 8–10 triệu. "
                "Làm ở Q7. Inbox ứng tuyển."
            ),
            "HIRING",
        ),
        (
            (
                "TUYỂN DỤNG\nVỊ TRÍ: Thu ngân\nCA: Tối\nLƯƠNG: 8–10 triệu\n"
                "ĐỊA ĐIỂM: Quận 7\nGỬI CV để ứng tuyển"
            ),
            "HIRING",
        ),
        (
            (
                "TUYỂN GẤP 3 NHÂN VIÊN PHỤC VỤ. Lương: 8–10 triệu/tháng. "
                "Địa điểm: Quận 7. Liên hệ: 0900000000."
            ),
            "HIRING",
        ),
        (
            (
                "TUYỂN DỤNG\nVỊ TRÍ: Thu ngân\nLƯƠNG: 8–10 triệu/tháng\n"
                "ĐỊA ĐIỂM: Quận 7\nCA: Tối\nLIÊN HỆ: 0900000000"
            ),
            "HIRING",
        ),
        (
            (
                "Mình đăng lại bài này: TUYỂN GẤP 3 NHÂN VIÊN PHỤC VỤ. "
                "Lương: 8 triệu/tháng. Địa điểm: Quận 7. "
                "Riêng bên mình đang tuyển Backend Developer."
            ),
            "HIRING",
        ),
        (
            (
                "Mình đăng lại bài này. Riêng bên mình đang nhận CV cho đối tác "
                "ở vị trí QA Engineer."
            ),
            "HIRING",
        ),
        ("Cần tuyển 2 bảo vệ, ca đêm, liên hệ để ứng tuyển.", "HIRING"),
        (
            (
                "Mình đang nhận CV cho đối tác ở vị trí QA Engineer. "
                "Gửi CV qua inbox để ứng tuyển."
            ),
            "HIRING",
        ),
        ("Em cần việc làm ca tối tại Đà Nẵng.", "SEEKING_JOB"),
        ("Mình muốn tìm việc làm kế toán tại Hà Nội.", "SEEKING_JOB"),
        (
            (
                "Tuyển gấp nhân viên phục vụ, inbox ứng tuyển. "
                "Đã tuyển đủ cho vị trí khác."
            ),
            "HIRING",
        ),
        (
            (
                "Năm ngoái tuyển nhân viên phục vụ. "
                "Hiện đang tuyển lại nhân viên phục vụ, inbox nhận CV."
            ),
            "HIRING",
        ),
        (
            (
                "Tuyển gấp nhân viên phục vụ, inbox ứng tuyển. "
                "Đã tuyển đủ nhân viên phục vụ cho vị trí này. "
                "Hiện đang tuyển lại nhân viên phục vụ ca tối, inbox nhận CV."
            ),
            "HIRING",
        ),
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
        (
            (
                "Tuyển gấp nhân viên phục vụ, inbox ứng tuyển. Hết hạn nhận hồ sơ. "
                "Hiện đang tuyển lại nhân viên phục vụ ca tối, inbox nhận CV."
            ),
            "HIRING",
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
        "direct_structured_ad_without_company_subject",
        "multiline_labeled_direct_ad",
        "structured_direct_ad_without_application_verb",
        "multiline_labeled_ad_without_application_verb",
        "repost_with_independent_current_author_opening",
        "repost_with_current_author_partner_intake",
        "direct_ad_with_headcount_shift_and_contact",
        "recruiter_accepting_applications_for_partner",
        "first_person_em_needs_work",
        "first_person_minh_wants_work",
        "direct_ad_kept_open_by_different_vacancy_closure",
        "direct_vacancy_reopened_after_same_role_closure",
        "current_direct_reopening_after_historical_ad",
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
        "direct_vacancy_reopened_after_expired_applications",
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
        "Bạn Minh muốn tìm việc làm.",
        "Anh Minh muốn tìm việc làm.",
        "Bạn Lan đang tìm việc làm.",
        "Ban Minh dang tim viec lam.",
        "Minh khong tim viec.",
        "Minh da tim duoc viec roi.",
        "Minh Anh dang tim viec.",
        "Nguyen Van Anh dang tim viec lam.",
        "Vietnam team hire QA engineer.",
        "Career advice about the word engineer.",
        "Khóa học về hiring an engineer.",
        "Tuyển sinh lớp nghề – học phí 8 triệu – địa chỉ – liên hệ.",
        "Tổng hợp 20 tin tuyển dụng của công ty khác.",
        (
            "Tuyển gấp 3 nhân viên phục vụ. Lương 8–10 triệu. Inbox ứng tuyển. "
            "Đã tuyển đủ nhân viên phục vụ, ngừng nhận CV."
        ),
        (
            "Cần tuyển QA Engineer, inbox ứng tuyển. "
            "Hạn nộp hồ sơ: 01/01/2000."
        ),
        "Cần tuyển QA Engineer, inbox ứng tuyển. Hạn nộp hồ sơ đã qua hôm qua.",
        "Lương 8–10 triệu, gọi số 0900000000, địa chỉ Quận 7.",
        "Năm ngoái tuyển gấp nhân viên phục vụ, lương 8 triệu, inbox ứng tuyển.",
        "'Tuyển gấp nhân viên phục vụ, inbox ứng tuyển.'",
        "Hiring 3 servers. Salary 8 million. Inbox to apply.",
        "Hạn ứng tuyển: 31/12/2099. Vị trí: QA Engineer.",
        "Hạn ứng tuyển: 31/12. Vị trí: QA Engineer.",
        "Cần tuyển nhân viên, lương 8 triệu, inbox ứng tuyển.",
        "Có ai đang tuyển QA Engineer không? Inbox mình biết với.",
        "Công ty khác đang tuyển QA Engineer. Inbox mình để xin link.",
        "Có ai đang tuyển QA Engineer không? Liên hệ mình.",
        (
            "Công ty khác đang tuyển QA Engineer. Lương 8 triệu/tháng. "
            "Địa điểm: Quận 7. Inbox mình để xin link."
        ),
        (
            "Tuyển sinh khóa học QA Engineer. Học phí: 8 triệu. "
            "Địa điểm: Quận 7. Liên hệ đăng ký."
        ),
        (
            "Hướng dẫn tuyển dụng nhân viên phục vụ, lương 8 triệu, "
            "địa điểm Quận 7, liên hệ nhận tư vấn."
        ),
        (
            "TUYỂN GẤP 3 NHÂN VIÊN PHỤC VỤ. Lương 8 triệu. "
            "Địa điểm Quận 7. Liên hệ 0900000000. HẾT HẠN NHẬN HỒ SƠ."
        ),
        (
            "Mình đăng lại bài này: TUYỂN GẤP 3 NHÂN VIÊN PHỤC VỤ. "
            "Lương: 8 triệu/tháng. Địa điểm: Quận 7."
        ),
        (
            "Đăng hộ bạn mình: CẦN TUYỂN 2 THU NGÂN. "
            "Lương 9 triệu/tháng. Địa điểm: Hà Nội."
        ),
        (
            "Repost: TUYỂN GẤP 3 NHÂN VIÊN PHỤC VỤ. Lương 8 triệu/tháng. "
            "Địa điểm: Quận 7. Inbox mình để xin link."
        ),
        (
            "Minh dang lai bai nay: TUYEN GAP 3 NHAN VIEN PHUC VU. "
            "Luong: 8 trieu/thang. Dia diem: Quan 7."
        ),
        (
            "TUYỂN GẤP 3 NHÂN VIÊN PHỤC VỤ. Lương 8 triệu. "
            "Inbox ứng tuyển. HẾT HẠN NHẬN HỒ SƠ."
        ),
        "Co ai dang tuyen QA Engineer khong? Inbox minh biet voi.",
        "Cong ty khac dang tuyen QA Engineer. Inbox minh de xin link.",
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
        "third_party_named_seeker_minh_wants_work",
        "third_party_named_seeker_anh_wants_work",
        "third_party_named_seeker_lan",
        "unaccented_third_party_named_seeker",
        "unaccented_seeking_negative",
        "unaccented_seeking_completed",
        "unaccented_person_name",
        "unaccented_full_name",
        "english_company_context",
        "english_career_advice",
        "course_with_english_role",
        "course_enrollment_with_salary_address_and_contact",
        "third_party_job_listing_summary",
        "direct_vacancy_closed_and_cv_intake_stopped",
        "direct_vacancy_with_explicitly_expired_deadline",
        "direct_vacancy_with_explicitly_passed_deadline",
        "salary_phone_and_address_without_job_intent",
        "historical_direct_recruitment_ad",
        "quoted_direct_recruitment_ad",
        "english_only_direct_recruitment_ad",
        "deadline_and_role_without_hiring_intent",
        "deadline_without_year_and_hiring_intent",
        "generic_employee_title_without_identifiable_role",
        "question_about_someone_hiring_with_information_request",
        "third_party_listing_with_link_request",
        "question_with_contact_request",
        "third_party_structured_listing_with_link_request",
        "course_with_salary_location_and_contact",
        "career_advice_with_recruitment_details",
        "structured_direct_ad_with_closed_applications",
        "passive_repost_of_structured_direct_ad",
        "posted_on_behalf_of_friend_with_job_details",
        "repost_with_incidental_inbox_request",
        "unaccented_passive_repost_of_structured_ad",
        "direct_ad_with_explicitly_closed_applications",
        "unaccented_question_about_someone_hiring",
        "unaccented_third_party_listing_with_link_request",
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
