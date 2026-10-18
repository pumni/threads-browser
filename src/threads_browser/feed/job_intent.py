"""Conservative, auditable Vietnamese job-intent checks for Auto Nurture."""

from __future__ import annotations

import re
import unicodedata
from datetime import date

AUTO_POLICY_VERSION = "threads-browser:auto-nurture:job-intent-v3-vi"

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
    r"(?:nhan\s+vien(?:\s+(?:kinh\s+doanh|phuc\s+vu|thu\s+ngan|pha\s+che|"
    r"ban\s+hang|kho(?:\s+van)?|van\s+phong|giao\s+hang|le\s+tan|"
    r"tu\s+van|bao\s+ve|tai\s+xe|lai\s+xe|cong\s+nhan|phu\s+bep|"
    r"dau\s+bep|tap\s+vu|tho\s+dien|tho\s+han|ky\s+thuat\s+vien|"
    r"hanh\s+chinh(?:\s+nhan\s+su)?|cham\s+soc\s+khach\s+hang))?|"
    r"nhan\s+su|lap\s+trinh\s+vien|phuc\s+vu|thu\s+ngan|pha\s+che|"
    r"bao\s+ve|kho(?:\s+van)?|lai\s+xe|tai\s+xe|giao\s+hang|ban\s+hang|"
    r"tap\s+vu|le\s+tan|cong\s+nhan|phu\s+bep|dau\s+bep|tho\s+dien|"
    r"tho\s+han|ky\s+thuat\s+vien|hanh\s+chinh\s+nhan\s+su|"
    r"kiem\s+thu\s+vien|ke\s+toan|"
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
    rf"(?:hien(?:\s+(?:tai|nay))\s+)?(?:dang|can|muon)\s+"
    r"tim(?:\s+kiem)?\s+(?:viec(?:\s+lam)?|cong\s+viec)\b"
)
_SEEKING_NEEDS_WORK_POSITIVE = re.compile(
    r"\bem\s+(?:hien(?:\s+(?:tai|nay))\s+)?(?:dang\s+)?"
    r"can\s+(?:tim\s+)?viec(?:\s+lam)?\b"
)
_SEEKING_POSITIVE_WITH_INHERITED_SUBJECT = re.compile(
    r"\b(?:hien(?:\s+(?:tai|nay))?\s+)?(?:dang|can)\s+"
    r"tim(?:\s+kiem)?\s+(?:viec(?:\s+lam)?|cong\s+viec)\b"
)
_HIRING_NEGATION = re.compile(
    r"\b(?:khong|chua|chang)(?:\s+con)?(?:\s+dang)?\s+tuyen(?:\s+dung)?\b|"
    r"\b(?:ngung|dung)\s+tuyen(?:\s+dung)?\b|"
    r"\b(?:ngung|dung)\s+nhan\s+(?:cv|ho\s+so|don\s+ung\s+tuyen)"
    r"(?:\s+(?:nua|them))?\b|"
    r"\bhet\s+han\s+(?:nhan\s+(?:cv|ho\s+so)|nop\s+ho\s+so|"
    r"ung\s+tuyen|tuyen\s+dung)\b|"
    r"\bhan\s+(?:nop\s+ho\s+so|ung\s+tuyen|tuyen\s+dung)\s+da\s+(?:qua|het)\b|"
    r"\bda\s+(?:qua|het)\s+han(?:\s+(?:nop\s+ho\s+so|ung\s+tuyen|tuyen\s+dung))?\b|"
    r"\b(?:khong|chua)\s+nhan\s+cv(?:\s+(?:nua|them))?\b|"
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
    r"\b(?:khoa\s+hoc|lop\s+hoc|tuyen\s+sinh|"
    r"dang\s+ky\s+(?:khoa\s+hoc|lop\s+hoc|workshop|dao\s+tao)|"
    r"workshop|dao\s+tao|course|courses|class\s+enrollment|training|"
    r"career\s+advice|career\s+tips|interview\s+tips|cv\s+tips|"
    r"chia\s+se|kinh\s+nghiem|meo|loi\s+khuyen|huong\s+dan|"
    r"phan\s+tich|thi\s+truong\s+lao\s+dong|"
    r"tong\s+hop|danh\s+sach|tin\s+tuyen\s+dung)\b"
)
_THIRD_PARTY_CONTEXT = re.compile(
    r"\b(?:nguoi\s+khac|minh\s+anh|mot\s+cong\s+ty|nhieu\s+cong\s+ty|"
    r"cac\s+cong\s+ty|"
    r"cong\s+ty\s+(?:khac|cua\s+ban|nay|do|kia)|"
    r"doanh\s+nghiep\s+(?:nay|do|kia))\b"
)
_SELF_RECRUITER_INTAKE = re.compile(
    r"\b(?:ben\s+(?:minh|toi)|chung\s+(?:toi|minh)|toi|em|anh|chi)\s+"
    r"(?:(?:hien(?:\s+(?:tai|nay))?\s+)?(?:dang\s+)?)"
    r"nhan\s+(?:cv|ho\s+so|don\s+ung\s+tuyen)\b"
)
_DIRECT_HIRING_INTENT = re.compile(
    r"\b(?:(?:hien(?:\s+(?:tai|nay))?\s+)?(?:dang\s+)?(?:can\s+)?)"
    r"tuyen(?:\s+(?:gap|dung|lai|ngay))?\b|"
    r"\b(?:dang\s+)?nhan\s+(?:ho\s+so|cv|don\s+ung\s+tuyen)\b"
)
_APPLICANT_INTAKE = re.compile(
    r"\b(?:gui\s+(?:cv|ho\s+so)|nop\s+(?:cv|ho\s+so)|"
    r"ung\s+tuyen|nhan\s+(?:cv|ho\s+so|don\s+ung\s+tuyen))\b"
)
_EXPLICIT_DEADLINE = re.compile(
    r"\b(?:han\s+(?:nop\s+ho\s+so|ung\s+tuyen|tuyen\s+dung)|deadline)\s*"
    r"(?:la\s+|den\s+|toi\s+|[:\-]\s*)?"
    r"(?:(?P<iso_year>\d{4})-(?P<iso_month>\d{1,2})-(?P<iso_day>\d{1,2})|"
    r"(?P<day>\d{1,2})[/-](?P<month>\d{1,2})[/-](?P<year>\d{4}))\b"
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
    r"\b(?:hien\s+tai|hien\s+nay|hien\s+(?:dang|can)|bay\s+gio|"
    r"hom\s+nay|luc\s+nay|tuyen\s+lai)\b"
)
_SEEKING_NAME_CONTEXT = re.compile(
    r"\b(?:nguyen|tran|le|pham|hoang|vu|vo|dang|bui|do|ngo|duong|ly|phan|"
    r"truong|dinh|luu)(?:\s+[a-z]{1,16}){0,2}\s+(?:minh|anh|chi)\b"
)
_THIRD_PARTY_SEEKING_CONTEXT = re.compile(
    r"\b(?:ban|anh|chi|ong|ba|co|chu)\s+[a-z]{1,16}\s+"
    r"(?:hien(?:\s+(?:tai|nay))?\s+)?(?:dang|can|muon)\s+"
    r"tim(?:\s+kiem)?\s+(?:viec(?:\s+lam)?|cong\s+viec)\b"
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
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[^\S\n]+", " ", text)
    return re.sub(r" *\n *", "\n", text).strip()


def _direct_hiring_events(text: str) -> list[tuple[int, str, str | None]]:
    """Find direct ads with a role and explicit applicant-intake evidence."""
    events: list[tuple[int, str, str | None]] = []
    block_start = 0
    for separator in (*re.finditer(r"\n[ \t]*\n", text), None):
        block_end = len(text) if separator is None else separator.start()
        block = text[block_start:block_end]
        flattened = block.replace("\n", " ")
        if block:
            for intent in _DIRECT_HIRING_INTENT.finditer(flattened):
                preceding_words = flattened[max(0, intent.start() - 32) : intent.start()]
                if re.search(r"\b(?:ung|han)\s+$", preceding_words):
                    continue
                if re.search(
                    r"\b(?:da|khong|chua|chang)(?:\s+con)?(?:\s+dang)?\s+$|"
                    r"\b(?:ngung|dung)\s+$",
                    preceding_words,
                ):
                    continue
                sentence_start = max(
                    flattened.rfind(mark, 0, intent.start()) for mark in ".!?"
                ) + 1
                sentence_end_candidates = [
                    (position, mark)
                    for mark in ".!?"
                    if (position := flattened.find(mark, intent.end())) >= 0
                ]
                sentence_end, sentence_ending = min(
                    sentence_end_candidates,
                    default=(len(flattened), ""),
                )
                sentence = flattened[sentence_start:sentence_end]
                if sentence_ending == "?":
                    continue
                if (
                    _THIRD_PARTY_CONTEXT.search(sentence)
                    and _SELF_RECRUITER_INTAKE.search(sentence) is None
                ):
                    continue
                window_start = max(0, intent.start() - 100)
                window_end = min(len(flattened), intent.end() + 360)
                window = flattened[window_start:window_end]
                if _NON_JOB_CONTEXT.search(window):
                    continue
                role_matches = list(_HIRING_ROLE_PATTERN.finditer(window))
                role_match = min(
                    role_matches,
                    key=lambda match: abs(
                        window_start + match.start() - intent.start()
                    ),
                    default=None,
                )
                if (
                    role_match is None
                    or role_match.group("role") == "nhan vien"
                    or _APPLICANT_INTAKE.search(window) is None
                ):
                    continue

                prefix = flattened[sentence_start:intent.start()]
                if _UNCERTAIN_CONTEXT.search(sentence):
                    continue
                past_markers = list(_PAST_CONTEXT.finditer(prefix))
                current_markers = list(_CURRENT_CONTEXT.finditer(prefix))
                if past_markers and (
                    not current_markers or past_markers[-1].start() > current_markers[-1].start()
                ):
                    continue

                events.append(
                    (
                        block_start + intent.start(),
                        "HIRING",
                        role_match.group("role"),
                    )
                )
        if separator is None:
            break
        block_start = separator.end()
    return events


def _expired_deadline_events(text: str) -> list[tuple[int, str, str | None]]:
    events: list[tuple[int, str, str | None]] = []
    for match in _EXPLICIT_DEADLINE.finditer(text):
        if match.group("iso_year") is not None:
            year, month, day = (
                int(match.group("iso_year")),
                int(match.group("iso_month")),
                int(match.group("iso_day")),
            )
        else:
            year, month, day = (
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            )
        try:
            deadline = date(year, month, day)
        except ValueError:
            continue
        if deadline >= date.today():
            continue
        nearby_start = max(0, match.start() - 240)
        nearby = text[nearby_start : match.end() + 240]
        role_matches = list(_HIRING_ROLE_PATTERN.finditer(nearby))
        role_match = min(
            role_matches,
            key=lambda item: abs(item.start() - (match.start() - nearby_start)),
            default=None,
        )
        if role_match is not None:
            events.append((match.end(), "NOT_HIRING", role_match.group("role")))
    return events


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
            or _THIRD_PARTY_SEEKING_CONTEXT.search(clause)
        ):
            offset += len(clause) + 1
            continue

        hiring_closures = list(_HIRING_NEGATION.finditer(clause))
        seeking_closures = list(_SEEKING_NEGATION.finditer(clause))
        has_hiring_subject = re.search(rf"\b{_HIRING_SUBJECT}\b", clause) is not None
        has_seeking_subject = re.search(rf"\b{_SEEKING_SUBJECT}\b", clause) is not None
        mixed_english_context = _ENGLISH_CONTEXT.search(clause) is not None
        third_party_context = _THIRD_PARTY_CONTEXT.search(clause) is not None
        if (
            third_party_context
            and _SELF_RECRUITER_INTAKE.search(clause) is None
        ):
            offset += len(clause) + 1
            continue
        if not mixed_english_context:
            hiring_matches = list(_HIRING_POSITIVE.finditer(clause))
            if hiring_subject_seen and not has_hiring_subject:
                hiring_matches.extend(_HIRING_POSITIVE_WITH_INHERITED_SUBJECT.finditer(clause))
            for match in hiring_matches:
                events.append((offset + match.start(), "HIRING", match.group("role")))
            seeking_matches = list(_SEEKING_POSITIVE.finditer(clause))
            seeking_matches.extend(_SEEKING_NEEDS_WORK_POSITIVE.finditer(clause))
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
    events: list[tuple[int, str, str | None]] = []
    for sentence_match in _SENTENCES.finditer(text):
        raw_sentence = sentence_match.group(0)
        sentence = raw_sentence.strip()
        leading_space = len(raw_sentence) - len(raw_sentence.lstrip())
        events.extend(
            (
                sentence_match.start() + leading_space + offset,
                event,
                role,
            )
            for offset, event, role in _sentence_events(sentence)
        )
    direct_events = _direct_hiring_events(text)
    for direct_event in direct_events:
        position, _, role = direct_event
        if any(
            event == "HIRING"
            and existing_role is not None
            and role is not None
            and _same_hiring_role(existing_role, role)
            and abs(existing_position - position) <= 160
            and not any(
                close_position > min(existing_position, position)
                and close_position < max(existing_position, position)
                and close_event in {"NOT_HIRING", "NOT_HIRING_OTHER"}
                and (
                    close_role is None
                    or _same_hiring_role(close_role, role)
                )
                for close_position, close_event, close_role in events
            )
            for existing_position, event, existing_role in events
        ):
            continue
        events.append(direct_event)
    events.extend(_expired_deadline_events(text))
    events.sort(key=lambda event: event[0])

    active_hiring_roles: list[str] = []
    seeking_job_is_current = False
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
