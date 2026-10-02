"""원고 검사: 개인정보·금지어·재료에 없는 소재·인용·글자 수·중복.

자동 검사는 사람의 확인을 대신하지 않는다. 걸러낼 수 있는 것만 미리 알려 준다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable

BLOCK = "block"
WARN = "warn"


@dataclass(frozen=True)
class Finding:
    level: str  # block / warn
    code: str
    message: str


_PHONE = re.compile(r"(?<!\d)01[016789][-\s.]?\d{3,4}[-\s.]?\d{4}(?!\d)")
_LANDLINE = re.compile(r"(?<!\d)0\d{1,2}[-\s.)]\d{3,4}[-\s.]\d{4}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_RRN = re.compile(r"(?<!\d)\d{6}[-\s]?[1-4]\d{6}(?!\d)")
_APT_UNIT = re.compile(r"\d+\s?동\s?\d+\s?호")
_ROAD_ADDR = re.compile(r"[가-힣]{2,}(?:로|길)\s?\d+(?:-\d+)?(?:번길)?\s?\d*")
_LOT_ADDR = re.compile(r"\d+(?:-\d+)?\s?번지")

# 재료에 없는데 원고에 등장하면 '당첨을 위해 만들어 넣은 소재'일 수 있는 단어.
SENSITIVE_WORDS = [
    "투병", "수술", "입원", "응급실", "진단", "항암", "질병", "불치",
    "사고", "사망", "돌아가셨", "돌아가신", "장례", "부고", "임종",
    "빚", "대출", "파산", "실직", "해고", "폐업", "부도", "생활고",
    "이혼", "별거", "가정폭력", "유산",
]

_QUOTED = re.compile(r"[\"“”]([^\"“”]{2,})[\"“”]")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def split_terms(text: str | None) -> list[str]:
    """쉼표·줄바꿈으로 구분된 단어 목록. 20자를 넘는 문장은 자동 검사 대상에서 뺀다."""
    if not text:
        return []
    terms = [t.strip() for t in re.split(r"[,\n、]", text)]
    return [t for t in terms if t and len(t) <= 20]


def check_personal_info(text: str, banned_terms: Iterable[str] = ()) -> list[Finding]:
    found: list[Finding] = []
    if _PHONE.search(text) or _LANDLINE.search(text):
        found.append(Finding(BLOCK, "phone", "전화번호로 보이는 숫자가 있습니다. 연락처는 공개 본문에 넣지 않습니다."))
    if _EMAIL.search(text):
        found.append(Finding(BLOCK, "email", "이메일 주소가 있습니다."))
    if _RRN.search(text):
        found.append(Finding(BLOCK, "rrn", "주민등록번호 형식의 숫자가 있습니다."))
    if _APT_UNIT.search(text) or _LOT_ADDR.search(text):
        found.append(Finding(BLOCK, "address", "동·호수나 번지 같은 상세 주소가 있습니다."))
    elif _ROAD_ADDR.search(text):
        found.append(Finding(WARN, "road_address", "도로명 주소처럼 보이는 표현이 있습니다. 상세 주소인지 확인하세요."))
    for term in banned_terms:
        if term and term in text:
            found.append(Finding(BLOCK, "banned_term", f"공개하지 않기로 한 단어 '{term}'이(가) 들어 있습니다."))
    return found


def check_sensitive_additions(draft_text: str, source_text: str) -> list[Finding]:
    added = [w for w in SENSITIVE_WORDS if w in draft_text and w not in source_text]
    if not added:
        return []
    return [Finding(WARN, "sensitive_added",
                    "실제 경험 재료에 없는 민감 소재가 원고에 있습니다: " + ", ".join(added)
                    + ". 질병·사고·사망·경제적 곤란·가족관계는 만들어 넣지 않습니다.")]


def check_numbers(draft_text: str, source_text: str) -> list[Finding]:
    src = set(_NUMBER.findall(source_text))
    extra = sorted({n for n in _NUMBER.findall(draft_text) if n not in src})
    if not extra:
        return []
    return [Finding(WARN, "numbers_added",
                    "재료에 없는 숫자가 있습니다: " + ", ".join(extra[:10])
                    + ". 날짜·금액·횟수가 실제와 같은지 확인하세요.")]


def check_quotes(draft_text: str, quotes: str | None, quote_kind: str) -> list[Finding]:
    spans = _QUOTED.findall(draft_text)
    if not spans:
        return []
    if quote_kind != "exact":
        return [Finding(WARN, "quote_unverified",
                        "큰따옴표 직접 인용이 있지만 재료의 말은 '정확한 인용'으로 표시되지 않았습니다. "
                        "기억이 불확실한 대사는 간접 화법으로 바꾸세요.")]
    src = quotes or ""
    unknown = [s for s in spans if s.strip() not in src]
    if unknown:
        return [Finding(WARN, "quote_not_in_source",
                        "재료에 없는 직접 인용이 있습니다: " + " / ".join(u[:20] for u in unknown[:3]))]
    return []


def check_required(title: str, body: str) -> list[Finding]:
    found = []
    if not title.strip():
        found.append(Finding(BLOCK, "empty_title", "제목이 비어 있습니다."))
    if not body.strip():
        found.append(Finding(BLOCK, "empty_body", "본문이 비어 있습니다."))
    return found


def check_length(body: str, char_limit: int | None) -> list[Finding]:
    if char_limit and len(body) > char_limit:
        return [Finding(BLOCK, "too_long", f"본문이 {len(body)}자로, 코너 제한 {char_limit}자를 넘습니다.")]
    return []


def check_similarity(body: str, previous_bodies: Iterable[str], threshold: float = 0.6) -> list[Finding]:
    for prev in previous_bodies:
        if prev and SequenceMatcher(None, body, prev).ratio() >= threshold:
            return [Finding(WARN, "similar_text",
                            "이전에 제출한 글과 문장이 많이 비슷합니다. 같은 사건을 문장만 바꿔 여러 곳에 보내지 않습니다.")]
    return []


def experience_source_text(exp) -> str:
    fields = ["when_text", "people", "story", "quotes", "highlight", "ending", "fixed_facts", "song"]
    return "\n".join((exp[f] or "") for f in fields)


def run_all(*, title: str, body: str, song: str, exp, profile: dict, char_limit: int | None,
            previous_bodies: Iterable[str] = ()) -> list[Finding]:
    full = f"{title}\n{body}\n{song}"
    banned = split_terms(profile.get("banned_words")) + split_terms(exp["hide"])
    source = experience_source_text(exp)
    findings: list[Finding] = []
    findings += check_required(title, body)
    findings += check_length(body, char_limit)
    findings += check_personal_info(full, banned)
    findings += check_sensitive_additions(full, source)
    findings += check_numbers(full, source)
    findings += check_quotes(full, exp["quotes"], exp["quote_kind"])
    findings += check_similarity(body, previous_bodies)
    if (exp["prior_history"] or "").strip():
        findings.append(Finding(WARN, "prior_history",
                                "이 경험은 다른 방송·게시판 제출/채택 이력이 있습니다: " + exp["prior_history"].strip()[:60]))
    return findings
