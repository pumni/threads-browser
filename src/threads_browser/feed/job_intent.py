"""Conservative, deterministic intent checks for Auto Nurture V1."""

from __future__ import annotations

import re
import unicodedata

AUTO_POLICY_VERSION = "threads-browser:auto-nurture:job-intent-v1"

_HIRING_PHRASES = (
    "cần tuyển",
    "tuyển nhân viên",
    "tuyển dụng nhân viên",
    "tuyển vị trí",
    "tuyển dụng vị trí",
    "tuyển dụng cho vị trí",
    "tìm ứng viên",
    "chúng tôi đang tuyển",
    "bên mình đang tuyển",
    "we're hiring",
    "we are hiring",
    "our team is hiring",
    "now hiring",
    "looking to hire",
    "hiring for",
    "job opening for",
    "open position for",
    "recruiting for",
    "seeking candidates for",
)
_SEEKING_PHRASES = (
    "tôi đang tìm việc",
    "mình đang tìm việc",
    "em đang tìm việc",
    "anh đang tìm việc",
    "tôi đang tìm công việc",
    "mình đang tìm công việc",
    "em đang tìm công việc",
    "đang tìm việc làm",
    "đang tìm công việc",
    "đang tìm kiếm việc làm",
    "đang tìm kiếm công việc",
    "mong muốn tìm việc",
    "cần tìm việc",
    "open to work",
    "actively looking for work",
    "actively seeking employment",
    "looking for a job",
    "looking for work",
    "looking for my next role",
    "seeking a job",
    "seeking employment",
    "seeking my next role",
    "i am looking for a job",
    "i'm looking for work",
)
_HIRING_NEGATIONS = (
    "không còn tuyển",
    "không tuyển nữa",
    "đã tuyển đủ",
    "đã tuyển xong",
    "đã đóng tuyển dụng",
    "ngừng tuyển",
    "not hiring",
    "no longer hiring",
    "position filled",
    "position is filled",
    "role filled",
    "applications are closed",
    "no longer accepting applications",
)
_SEEKING_NEGATIONS = (
    "không tìm việc",
    "không còn tìm việc",
    "đã tìm được việc",
    "đã có việc",
    "not looking for a job",
    "no longer looking for work",
    "found a job",
    "got a job",
)
_NON_JOB_CONTEXT = (
    "khóa học",
    "lớp học",
    "tuyển sinh",
    "đăng ký khóa học",
    "course",
    "courses",
    "class enrollment",
    "workshop enrollment",
)
_HIRING_ROLE = re.compile(
    r"(?<!\w)tuyen\s+(?:[0-9]+\s+)?(?:"
    r"nhan su|ky su|lap trinh vien|developer|engineer|designer|manager|analyst|"
    r"recruiter|sales|marketing|accountant|intern|nhan vien)(?!\w)"
)
_ENGLISH_HIRING_ROLE = re.compile(
    r"(?<!\w)hiring\s+(?:(?:a|an)\s+)?(?:[0-9]+\s+)?(?:"
    r"engineers?|developers?|designers?|managers?|analysts?|recruiters?|"
    r"accountants?|interns?|sales(?:people)?|marketers?)(?!\w)"
)


def _plain_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).casefold()
    text = "".join(character for character in text if not unicodedata.combining(character))
    text = text.replace("đ", "d").replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).strip()


def _has_phrase(text: str, phrase: str) -> bool:
    normalized = _plain_text(phrase)
    return re.search(rf"(?<!\w){re.escape(normalized)}(?!\w)", text) is not None


def classify_job_intent(body: str | None) -> str:
    """Return HIRING, SEEKING_JOB, OTHER, or UNKNOWN from strong phrases only."""
    if body is None or not body.strip():
        return "UNKNOWN"
    text = _plain_text(body)
    if any(_has_phrase(text, phrase) for phrase in _NON_JOB_CONTEXT):
        return "OTHER"
    hiring = any(_has_phrase(text, phrase) for phrase in _HIRING_PHRASES)
    hiring = hiring or bool(_HIRING_ROLE.search(text)) or bool(_ENGLISH_HIRING_ROLE.search(text))
    seeking = any(_has_phrase(text, phrase) for phrase in _SEEKING_PHRASES)
    hiring = hiring and not any(_has_phrase(text, phrase) for phrase in _HIRING_NEGATIONS)
    seeking = seeking and not any(_has_phrase(text, phrase) for phrase in _SEEKING_NEGATIONS)
    if hiring == seeking:
        return "OTHER"
    return "HIRING" if hiring else "SEEKING_JOB"


def classify_auto_body(body: str | None) -> str:
    """Map job intent to the existing relevant/irrelevant/unknown ledger values."""
    intent = classify_job_intent(body)
    if intent in {"HIRING", "SEEKING_JOB"}:
        return "RELEVANT"
    return intent if intent == "UNKNOWN" else "IRRELEVANT"
