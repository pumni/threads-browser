"""Conservative, auditable Vietnamese job-intent checks for Auto Nurture."""

from __future__ import annotations

import re
import unicodedata

AUTO_POLICY_VERSION = "threads-browser:auto-nurture:job-intent-v2-vi"

# Normalize Vietnamese accents before matching. Eligibility comes from the
# Vietnamese sentence structure below, never from the presence of diacritics.
_HIRING_SUBJECT = (
    r"(?:ben\s+(?:minh|toi)|chung\s+toi|chung\s+minh|"
    r"cong\s+ty(?:\s+(?:minh|toi|chung\s+toi))?|"
    r"doanh\s+nghiep(?:\s+(?:minh|toi))?|"
    r"team\s+(?:minh|toi)|doi\s+ngu\s+(?:minh|toi)|"
    r"phong\s+nhan\s+su(?:\s+(?:minh|toi))?)"
)
_SEEKING_SUBJECT = r"(?:minh|em|toi|anh|chi|chung\s+toi|chung\s+minh)"
_HIRING_STATUS = (
    r"(?:dang(?:\s+can)?|hien(?:\s+(?:tai|nay))?(?:\s+dang)?(?:\s+can)?|can)"
)
_HIRING_ROLE = (
    r"(?:nhan\s+vien(?:\s+kinh\s+doanh)?|nhan\s+su|lap\s+trinh\s+vien|"
    r"ky\s+su|kiem\s+thu\s+vien|ke\s+toan|tai\s+xe|"
    r"qa(?:\s+(?:engineer|tester))?|testers?|engineers?|developers?|"
    r"designers?|managers?|analysts?|recruiters?|accountants?|interns?|"
    r"sales(?:people)?|marketers?|backend(?:\s+developers?)?|"
    r"frontend(?:\s+developers?)?|product\s+managers?|data\s+scientists?)"
)
_HIRING_POSITIVE = re.compile(
    rf"\b{_HIRING_SUBJECT}\b(?:\s+[\w-]+){{0,3}}\s+"
    rf"{_HIRING_STATUS}\s+tuyen(?:\s+dung)?\b"
    rf"[\w\s/-]{{0,64}}?\b(?:[0-9]+\s+)?(?P<role>{_HIRING_ROLE})\b"
)
_HIRING_POSITIVE_WITH_INHERITED_SUBJECT = re.compile(
    rf"\b{_HIRING_STATUS}\s+tuyen(?:\s+dung)?\b"
    rf"[\w\s/-]{{0,64}}?\b(?:[0-9]+\s+)?(?P<role>{_HIRING_ROLE})\b"
)
_HIRING_ROLE_PATTERN = re.compile(rf"\b(?P<role>{_HIRING_ROLE})\b")
_SEEKING_POSITIVE = re.compile(
    rf"\b(?:hien(?:\s+(?:tai|nay))?\s+)?{_SEEKING_SUBJECT}\s+"
    rf"(?:hien(?:\s+(?:tai|nay))\s+)?(?:dang|can)\s+"
    r"tim(?:\s+kiem)?\s+(?:viec(?:\s+lam)?|cong\s+viec)\b"
)
_SEEKING_POSITIVE_WITH_INHERITED_SUBJECT = re.compile(
    r"\b(?:hien(?:\s+(?:tai|nay))?\s+)?(?:dang|can)\s+"
    r"tim(?:\s+kiem)?\s+(?:viec(?:\s+lam)?|cong\s+viec)\b"
)
_HIRING_NEGATION = re.compile(
    r"\b(?:khong|chua|chang)(?:\s+con)?(?:\s+dang)?\s+tuyen(?:\s+dung)?\b|"
    r"\b(?:ngung|dung)\s+tuyen(?:\s+dung)?\b|"
    r"\bda\s+(?:tuyen\s+(?:du|xong|duoc\s+du)|dong\s+tuyen\s+dung)\b|"
    r"\bkhong\s+(?:tiep\s+)?nhan\s+(?:ho\s+so|don\s+ung\s+tuyen|ung\s+vien)"
    r"(?:\s+(?:nua|them))?\b|"
    r"\b(?:position\s+(?:is\s+)?filled|applications?\s+(?:are\s+)?closed)\b"
)
_DIFFERENT_POSITION = re.compile(r"\bvi tri khac\b")
_SEEKING_NEGATION = re.compile(
    r"\b(?:khong|chua|chang)(?:\s+con)?(?:\s+dang)?\s+"
    r"tim(?:\s+kiem)?\s+(?:viec(?:\s+lam)?|cong\s+viec)\b|"
    r"\bda\s+(?:tim\s+duoc\s+(?:mot\s+)?viec|co\s+viec)\b|"
    r"\btim\s+duoc\s+viec\s+(?:roi|r\s+oi)\b"
)
_NON_JOB_CONTEXT = re.compile(
    r"\b(?:khoa\s+hoc|lop\s+hoc|tuyen\s+sinh|dang\s+ky(?:\s+khoa\s+hoc)?|"
    r"workshop|dao\s+tao|course|courses|class\s+enrollment|training|"
    r"career\s+advice|career\s+tips|interview\s+tips|cv\s+tips|"
    r"chia\s+se|kinh\s+nghiem|meo|loi\s+khuyen|huong\s+dan|"
    r"phan\s+tich|thi\s+truong\s+lao\s+dong|nguoi\s+khac|minh\s+anh|"
    r"mot\s+cong\s+ty|nhieu\s+cong\s+ty|cac\s+cong\s+ty|"
    r"cong\s+ty\s+(?:nay|do|kia)|doanh\s+nghiep\s+(?:nay|do|kia))\b"
)
_UNCERTAIN_CONTEXT = re.compile(
    r"\b(?:neu|gia\s+su|co\s+the|co\s+le|du\s+kien|suy\s+doan|"
    r"maybe|might|would|perhaps|possibly|speculation)\b"
)
_PAST_CONTEXT = re.compile(
    r"\b(?:nam\s+ngoai|hoi\s+truoc|truoc\s+day|truoc\s+kia|ngay\s+truoc|"
    r"luc\s+truoc|hoi\s+do|nam\s+truoc|da\s+tung|tung)\b"
)
_CURRENT_CONTEXT = re.compile(
    r"\b(?:hien\s+tai|hien\s+nay|bay\s+gio|hom\s+nay|luc\s+nay)\b"
)
_SEEKING_NAME_CONTEXT = re.compile(
    r"\b(?:nguyen|tran|le|pham|hoang|vu|vo|dang|bui|do|ngo|duong|ly|phan|"
    r"truong|dinh|luu)(?:\s+[a-z]{1,16}){0,2}\s+(?:minh|anh|chi)\b"
)
_ENGLISH_CONTEXT = re.compile(
    r"\b(?:we(?:'re|\s+are)?|our\s+team|now\s+hiring|"
    r"open\s+to\s+work|looking\s+for|actively\s+looking|seeking\s+(?:a\s+)?job|"
    r"please\s+apply|apply\s+now|join\s+us|for\s+(?:the|our)\s+(?:team|role|position))\b"
)
_QUOTED_TEXT = re.compile(r"“[^”]*”|\"[^\"]*\"|‘[^’]*’|(?<!\w)'[^']*'")
_SENTENCES = re.compile(r"[^.!?\r\n]+(?:[.!?]+|$)")
_CLAUSE_BREAK = re.compile(
    r"\s*[,;]\s*|\s+\b(?:nhung|tuy\s+nhien|but|however|va)\b\s+"
)


def _plain_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).casefold()
    text = "".join(character for character in text if not unicodedata.combining(character))
    text = text.replace("đ", "d").replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).strip()


def _sentence_events(sentence: str) -> list[tuple[int, str, str | None]]:
    """Return ordered, sentence-local hiring/seeking status evidence."""
    events: list[tuple[int, str, str | None]] = []
    offset = 0
    hiring_subject_seen = False
    seeking_subject_seen = False
    historical_context = False
    for clause in _CLAUSE_BREAK.split(sentence):
        if not clause:
            continue
        if _PAST_CONTEXT.search(clause):
            historical_context = True
        elif _CURRENT_CONTEXT.search(clause):
            historical_context = False

        if (
            historical_context
            or clause.rstrip().endswith("?")
            or _NON_JOB_CONTEXT.search(clause)
            or _UNCERTAIN_CONTEXT.search(clause)
            or _SEEKING_NAME_CONTEXT.search(clause)
        ):
            offset += len(clause) + 1
            continue

        hiring_closures = list(_HIRING_NEGATION.finditer(clause))
        seeking_closures = list(_SEEKING_NEGATION.finditer(clause))
        has_hiring_subject = re.search(rf"\b{_HIRING_SUBJECT}\b", clause) is not None
        has_seeking_subject = re.search(rf"\b{_SEEKING_SUBJECT}\b", clause) is not None
        mixed_english_context = _ENGLISH_CONTEXT.search(clause) is not None
        if not mixed_english_context:
            hiring_matches = list(_HIRING_POSITIVE.finditer(clause))
            if hiring_subject_seen and not has_hiring_subject:
                hiring_matches.extend(_HIRING_POSITIVE_WITH_INHERITED_SUBJECT.finditer(clause))
            for match in hiring_matches:
                events.append((offset + match.start(), "HIRING", match.group("role")))
            seeking_matches = list(_SEEKING_POSITIVE.finditer(clause))
            if seeking_subject_seen and not has_seeking_subject:
                seeking_matches.extend(_SEEKING_POSITIVE_WITH_INHERITED_SUBJECT.finditer(clause))
            for match in seeking_matches:
                events.append((offset + match.start(), "SEEKING_JOB", None))
        for match in hiring_closures:
            closed_role = _HIRING_ROLE_PATTERN.search(clause)
            event = "NOT_HIRING_OTHER" if _DIFFERENT_POSITION.search(clause) else "NOT_HIRING"
            events.append(
                (
                    offset + match.start(),
                    event,
                    closed_role.group("role") if closed_role is not None else None,
                )
            )
        for match in seeking_closures:
            events.append((offset + match.start(), "NOT_SEEKING_JOB", None))
        if not mixed_english_context:
            hiring_subject_seen = hiring_subject_seen or has_hiring_subject
            seeking_subject_seen = seeking_subject_seen or has_seeking_subject
        offset += len(clause) + 1
    return sorted(events, key=lambda event: event[0])


def _same_hiring_role(active_role: str, closed_role: str) -> bool:
    active_words = set(active_role.split())
    closed_words = set(closed_role.split())
    return active_words <= closed_words or closed_words <= active_words


def classify_job_intent(body: str | None) -> str:
    """Return HIRING, SEEKING_JOB, OTHER, or UNKNOWN from Vietnamese evidence."""
    if body is None or not body.strip():
        return "UNKNOWN"

    text = _plain_text(_QUOTED_TEXT.sub(" ", body))
    active_hiring_roles: list[str] = []
    seeking_job_is_current = False
    for match in _SENTENCES.finditer(text):
        events = _sentence_events(match.group(0).strip())
        for _, event, role in events:
            if event == "HIRING" and role is not None:
                active_hiring_roles.append(role)
            elif event in {"NOT_HIRING", "NOT_HIRING_OTHER"}:
                if role is not None:
                    for index in range(len(active_hiring_roles) - 1, -1, -1):
                        if _same_hiring_role(active_hiring_roles[index], role):
                            del active_hiring_roles[index]
                            break
                elif event == "NOT_HIRING" and active_hiring_roles:
                    active_hiring_roles.pop()
            elif event == "SEEKING_JOB":
                seeking_job_is_current = True
            elif event == "NOT_SEEKING_JOB":
                seeking_job_is_current = False

    positive_intents: set[str] = set()
    if active_hiring_roles:
        positive_intents.add("HIRING")
    if seeking_job_is_current:
        positive_intents.add("SEEKING_JOB")

    if len(positive_intents) != 1:
        return "OTHER"
    return next(iter(positive_intents))


def classify_auto_body(body: str | None) -> str:
    """Map job intent to the existing relevant/irrelevant/unknown ledger values."""
    intent = classify_job_intent(body)
    if intent in {"HIRING", "SEEKING_JOB"}:
        return "RELEVANT"
    return intent if intent == "UNKNOWN" else "IRRELEVANT"
