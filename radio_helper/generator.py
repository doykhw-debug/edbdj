"""사연 초안 만들기.

유료 AI API는 연결하지 않는다 (명세 4절). 대신 두 가지를 제공한다.
1) 템플릿 초안: 사용자가 적은 실제 경험 문장을 그대로 배치하고 인사·맺음말만 붙인다. 사건을 보태지 않는다.
2) 대화형 AI용 요청문: 사용자가 이미 쓰는 Claude 채팅 등에 붙여 넣어 다듬은 원고를 받아 오는 용도.
"""

from __future__ import annotations

import re

from . import checks

PROGRAM_HOST = "영철 씨"

GREETINGS = {
    "유쾌함": "{host}, 좋은 아침입니다!{intro}",
    "담백함": "안녕하세요, {host}.{intro}",
    "따뜻함": "{host}, 그리고 철파엠 가족 여러분, 안녕하세요.{intro}",
}
CLOSINGS = {
    "유쾌함": "{host}, 오늘도 재미있는 방송 부탁드려요!",
    "담백함": "늘 좋은 방송 감사합니다.",
    "따뜻함": "오늘 하루도 모두 편안하게 보내셨으면 좋겠습니다. 늘 고맙습니다.",
}
DEFAULT_TONE = "담백함"


def _ro(word: str) -> str:
    """'(으)로' 조사."""
    ch = word.strip()[-1] if word.strip() else ""
    if "가" <= ch <= "힣":
        final = (ord(ch) - 0xAC00) % 28
        return "로" if final in (0, 8) else "으로"  # 받침 없음 또는 ㄹ 받침
    return "(으)로"


def mask_terms(text: str, terms: list[str]) -> str:
    for t in sorted(terms, key=len, reverse=True):
        if t:
            text = text.replace(t, "○○")
    return text


def _intro(profile: dict) -> str:
    nickname = profile.get("nickname") or "청취자"
    region = profile.get("region")
    job = profile.get("job_label")
    parts = []
    if region:
        parts.append(f"{region}에 사는")
    if job:
        parts.append(f"{job}{_ro(job)} 일하고 있는")
    who = " ".join(parts)
    return f" {who} {nickname}입니다." if who else f" {nickname}입니다."


def _first_sentence(text: str, limit: int = 28) -> str:
    text = " ".join((text or "").split())
    m = re.split(r"(?<=[.!?。])\s|\n", text, maxsplit=1)
    s = m[0] if m else text
    s = s.rstrip(".。 ")
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _paragraph(text: str | None) -> str:
    return "\n".join(line.strip() for line in (text or "").strip().splitlines() if line.strip())


def template_draft(exp, profile: dict, corner=None, about: str | None = None,
                   private: list[str] | None = None) -> dict[str, str]:
    """사용자가 적은 경험 문장을 그대로 쓰는 초안. 반환: title, body, song.

    화자는 늘 나다. about(이야기 주인공의 호칭, 예: '와이프')이 있으면 "제 와이프 이야기인데요"로 시작한다.
    private: 사연에 나오면 안 되는 인물 이름 (○○로 가림).
    """
    tone = profile.get("tone") if profile.get("tone") in GREETINGS else DEFAULT_TONE
    banned = checks.split_terms(profile.get("banned_words")) + checks.split_terms(exp["hide"]) + list(private or [])

    paras = [GREETINGS[tone].format(host=PROGRAM_HOST, intro=_intro(profile))]

    story = _paragraph(exp["story"])
    if about and about != "나":
        story = f"제 {about} 이야기인데요.\n{story}"
    if exp["when_text"]:
        story = f"{exp['when_text'].strip()} 있었던 일이에요.\n{story}"
    paras.append(story)

    quotes = [q.strip(' "“”') for q in _paragraph(exp["quotes"]).splitlines()]
    quotes = [q for q in quotes if q]
    if quotes:
        if exp["quote_kind"] == "exact":
            paras.append("\n".join(f"“{q}”" for q in quotes))
        else:
            # 정확한 인용이 아니면 직접 인용으로 확정하지 않는다.
            paras.append("\n".join(f"{q} — 대략 이런 말이었어요." for q in quotes))

    if exp["highlight"]:
        paras.append(_paragraph(exp["highlight"]))
    if exp["ending"]:
        paras.append(_paragraph(exp["ending"]))
    paras.append(CLOSINGS[tone].format(host=PROGRAM_HOST))

    song = (exp["song"] or "").strip()
    if song:
        paras.append(f"신청곡: {song}")

    body = mask_terms("\n\n".join(p for p in paras if p), banned)
    title = mask_terms(_first_sentence(exp["highlight"] or exp["story"]), banned)
    return {"title": title, "body": body, "song": song}


def chat_prompt(exp, profile: dict, corner) -> str:
    """대화형 AI에 붙여 넣을 요청문. 결과는 다시 이 도구에 붙여 넣고 검사·승인을 거친다."""
    tone = profile.get("tone") or DEFAULT_TONE
    if tone == "기타" and profile.get("tone_other"):
        tone = profile["tone_other"]
    limit = corner["char_limit"]
    quote_kind = {"exact": "정확한 인용", "gist": "취지만 기억함 (정확한 인용 아님)"}.get(exp["quote_kind"], "없음")

    lines = [
        f"아래는 SBS 파워FM '김영철의 파워FM' 「{corner['title']}」 게시판에 보낼 라디오 사연의 재료입니다.",
        "실제로 있었던 일입니다. 이 재료로 제목 후보 3개와 본문 1개를 써 주세요.",
        "",
        "[작성 원칙 — 반드시 지켜 주세요]",
        "1. 사건·등장인물·인물관계·결과는 재료 그대로 둡니다. 새로운 사건, 인물, 대사, 결말을 추가하지 않습니다.",
        "2. 바꿔도 되는 것은 문장, 구성, 리듬, 비유, 전달 방식뿐입니다.",
        "3. '정확한 인용'으로 표시되지 않은 말은 큰따옴표 직접 인용으로 쓰지 말고 간접 화법으로 씁니다.",
        "4. 질병·사고·사망·경제적 곤란·가족 갈등 등 재료에 없는 소재를 덧붙이지 않습니다.",
        "5. 실명, 회사·거래처명, 아이 이름, 연락처, 상세 주소는 쓰지 않고 아래 호칭을 씁니다.",
        "6. 재료에 없는 숫자(날짜·금액·횟수)를 만들지 않습니다.",
        "7. 재료에 없는 내용이 꼭 필요하면 본문에 넣지 말고 맨 끝 '확인 필요' 목록에 따로 적어 주세요.",
    ]
    if limit:
        lines.append(f"8. 본문은 공백 포함 {limit}자 이내로 씁니다.")
    lines += [
        "",
        "[보내는 사람]",
        f"- 방송용 이름: {profile.get('nickname') or '(미입력)'}",
        f"- 지역: {profile.get('region') or '(밝히지 않음)'}",
        f"- 직업 표현: {profile.get('job_label') or '(밝히지 않음)'}",
        f"- 가족 호칭: {profile.get('family_aliases') or '(미입력)'}",
        f"- 문체: {tone}",
        f"- 피할 소재: {profile.get('avoid_topics') or '(없음)'}",
        "",
        "[코너]",
        f"- 이름: {corner['title']}",
    ]
    if corner["notice_text"]:
        lines.append("- 공지 원문:\n" + corner["notice_text"].strip())
    if corner["required_fields"]:
        lines.append(f"- 필수 항목: {corner['required_fields']}")
    lines += [
        "",
        "[실제 경험]",
        f"- 언제: {exp['when_text'] or '(대략적으로도 미입력)'}",
        f"- 등장인물: {exp['people'] or '(미입력)'}",
        f"- 있었던 일:\n{(exp['story'] or '').strip()}",
        f"- 기억나는 말 ({quote_kind}):\n{(exp['quotes'] or '(없음)').strip()}",
        f"- 포인트(웃기거나 속상하거나 놀랐던 점): {exp['highlight'] or '(미입력)'}",
        f"- 실제 결말: {exp['ending'] or '(미입력)'}",
        f"- 바꾸면 안 되는 사실: {exp['fixed_facts'] or '(미입력)'}",
        f"- 신청곡: {exp['song'] or '(없음)'}",
        "",
        "[출력 형식]",
        "제목 후보:",
        "1.",
        "2.",
        "3.",
        "본문:",
        "(본문)",
        "신청곡:",
        "확인 필요:",
    ]
    banned = checks.split_terms(profile.get("banned_words")) + checks.split_terms(exp["hide"])
    return mask_terms("\n".join(lines), banned)


def parse_chat_result(text: str) -> dict[str, object]:
    """대화형 AI가 '출력 형식'대로 돌려준 결과를 나눈다. 반환: titles, body, song, needs_check."""
    sections: dict[str, list[str]] = {"titles": [], "body": [], "song": [], "needs_check": []}
    heads = {"제목 후보": "titles", "본문": "body", "신청곡": "song", "확인 필요": "needs_check"}
    current = None
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip().strip("*#[] ")
        head = next((h for h in heads if stripped.startswith(h) and stripped[len(h):].lstrip().startswith(":")
                     or stripped == h), None)
        if head:
            current = heads[head]
            rest = stripped[len(head):].lstrip().lstrip(":").strip().strip("*").strip()
            if rest:
                sections[current].append(rest)
            continue
        if current:
            sections[current].append(line)
    titles = [re.sub(r"^\s*(\d+[.)]|[-•])\s*", "", t).strip() for t in sections["titles"]]
    needs = [re.sub(r"^\s*[-•\d.)]+\s*", "", n).strip() for n in sections["needs_check"]]
    return {
        "titles": [t for t in titles if t],
        "body": "\n".join(sections["body"]).strip(),
        "song": " ".join(s.strip() for s in sections["song"] if s.strip()),
        "needs_check": [n for n in needs if n and n not in ("없음", "(없음)", "-")],
    }
