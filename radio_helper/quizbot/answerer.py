"""방송 녹취(+채팅창 화면)에서 퀴즈 문제·정답·기발한 오답, 사연 주제를 정리한다 (Claude API, 유료).

키워드가 들렸을 때만 호출한다. 호출 1회에 최근 몇 분 분량의 녹취(수천 토큰)와, 채팅창 영역을 지정했으면
채팅창 사진 몇 장(장당 수백 토큰)을 보낸다.
API 키는 환경 변수 ANTHROPIC_API_KEY 또는 윈도우 자격 증명 관리자(관리 화면에서 저장)에서 읽는다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

KEYRING_SERVICE = "radio_helper"
KEYRING_USER = "anthropic_api_key"

KINDS = ["new_question", "reannouncement", "answer_reveal", "not_quiz"]

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": KINDS},
        "duplicate_of": {"type": "integer", "description": "이미 감지한 문제 목록의 번호와 같은 문제면 그 번호, 아니면 0"},
        "question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}},
        "answer": {"type": "string", "description": "단답형 정답. 모르면 빈 문자열"},
        "confidence": {"type": "number", "description": "0~1. 정답이 맞을 확률"},
        "gorilla_accepted": {"type": "string", "enum": ["yes", "no", "unknown"]},
        "entry_instructions": {"type": "string"},
        "deadline_hint": {"type": "string"},
        "formatted_message": {"type": "string", "description": "진행자가 보내는 형식을 정했으면 그 형식대로 쓴 전송 문구, 아니면 빈 문자열"},
        "reasoning_note": {"type": "string", "description": "판단 근거 한두 문장 (녹취·채팅창에서 무엇을 보고 판단했는지)"},
        "witty_answer": {"type": "string", "description": "일부러 틀린 '기발한 오답'. 웃긴 포인트가 없으면 빈 문자열"},
        "witty_point": {"type": "string", "description": "그 오답이 왜 웃기거나 기발한지 한 문장. 없으면 빈 문자열"},
        "wit_score": {"type": "number", "description": "0~1. 진행자가 소개하고 싶을 만큼 웃기거나 기발한 정도. 애매하면 0.5 이하"},
        "fun_welcome": {"type": "boolean", "description": "진행자가 재미있는 오답·엉뚱한 답도 환영한다고 했으면 true"},
    },
    "required": ["kind", "duplicate_of", "question", "options", "answer", "confidence", "gorilla_accepted",
                 "entry_instructions", "deadline_hint", "formatted_message", "reasoning_note",
                 "witty_answer", "witty_point", "wit_score", "fun_welcome"],
    "additionalProperties": False,
}

SYSTEM = """당신은 라디오 방송(SBS·MBC·KBS 등)의 음성 인식 녹취를 읽고, 청취자 참여 퀴즈를 정리하는 도우미입니다.
녹취는 자동 음성 인식 결과라서 받아쓰기 오류가 있을 수 있습니다. 앞뒤 맥락으로 원래 말을 추정하세요.

판단 규칙:
- kind
  - new_question: 녹취 안에서 진행자가 청취자에게 맞힐 문제를 새로 냈다.
  - reannouncement: '이미 감지한 문제'와 같은 문제를 다시 안내했다. duplicate_of 에 그 번호를 적는다.
  - answer_reveal: 진행자가 정답이나 당첨자를 발표하고 있다 (이제 응모할 때가 아니다).
  - not_quiz: 퀴즈가 아니다 (노래 제목, 사연 소개, 광고, 다른 프로그램 예고 등).
- answer: 짧은 단답형. 설명·문장부호·따옴표 없이 정답 낱말만. 보기가 있으면 보기 중 하나를 그대로.
  확실하지 않으면 가장 그럴듯한 답을 적되 confidence 를 낮게 준다. 전혀 모르면 빈 문자열과 0.
- confidence: 문제를 제대로 알아들었는지와 정답이 맞는지를 함께 반영한 0~1 값.
  녹취가 잘려 문제가 불완전하면 0.5 이하.
- gorilla_accepted: 진행자가 방송사 앱 채팅(SBS 고릴라·공감로그, MBC mini, KBS 콩 등)으로 정답을 받는다고 말했으면 yes,
  문자(#번호)나 전화 등 다른 방법만 말했으면 no, 언급이 없으면 unknown.
- formatted_message: 진행자가 '정답 앞에 #퀴즈를 붙여서' 같은 전송 형식을 정한 경우에만 그 형식으로 쓴 문구.
- 힌트가 나오면 그 힌트를 반영해 답과 confidence 를 다시 매긴다.
- 녹취에 없는 내용을 지어내지 않는다. 사람 이름·연락처를 만들어 넣지 않는다.

기발한 오답 (witty_answer):
- 라디오 퀴즈는 정답만 뽑히지 않고, 진행자가 웃긴 오답을 골라 소개하기도 한다. 그래서 정답과 따로,
  일부러 틀렸지만 기발하거나 웃긴 답을 하나 제안한다.
- 반드시 '웃긴 포인트'가 있어야 한다: 문제 속 낱말을 비튼 말장난, 상황을 뒤집는 재치, 누구나 공감할 일상 반전 등.
  웃긴 포인트를 witty_point 에 한 문장으로 적는다. 억지스럽거나 그냥 틀린 답이면 쓰지 말고 빈 문자열, wit_score 0.
- 보내는 사람의 가족·직장·사건 같은 개인 사연을 지어내지 않는다. 문제와 방송 상황에서만 웃음을 찾는다.
- 특정인 비하, 외모·성별·지역 비하, 정치·종교, 선정적인 내용, 사고·재난 농담은 쓰지 않는다.
- 공백 포함 40자 이내, 채팅에 바로 보낼 수 있는 한 줄.
- fun_welcome: 진행자가 '재밌는 오답도 환영', '엉뚱한 답도 좋아요'처럼 말했으면 true.

채팅창 화면 (있을 때만):
- 같은 시간대에 방송사 앱 채팅창을 찍은 화면이다. 다른 청취자 글과 제작진 공지가 보인다.
- 녹취의 받아쓰기 오류를 바로잡고 문제·힌트를 정확히 파악하는 데 녹취와 함께 쓴다 (교차 확인).
- 다른 청취자들이 올린 답은 참고만 한다. 그대로 믿지 말고, 다른 사람의 글을 베끼지 않는다.
- 화면에 보이는 닉네임·개인정보를 결과에 쓰지 않는다."""


def build_user_message(program: str, transcript: str, known: list[tuple[int, str]]) -> str:
    known_text = "\n".join(f"{i}. {q}" for i, q in known) or "(없음)"
    return (f"프로그램: {program}\n\n"
            f"이미 감지한 문제 (오늘 이 프로그램):\n{known_text}\n\n"
            f"최근 방송 녹취:\n{transcript}")


@dataclass
class QuizAnalysis:
    kind: str = "not_quiz"
    duplicate_of: int = 0
    question: str = ""
    options: list[str] = field(default_factory=list)
    answer: str = ""
    confidence: float = 0.0
    gorilla_accepted: str = "unknown"
    entry_instructions: str = ""
    deadline_hint: str = ""
    formatted_message: str = ""
    reasoning_note: str = ""
    witty_answer: str = ""
    witty_point: str = ""
    wit_score: float = 0.0
    fun_welcome: bool = False

    @classmethod
    def from_json(cls, text: str) -> "QuizAnalysis":
        data = json.loads(text)
        a = cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})
        if a.kind not in KINDS:
            a.kind = "not_quiz"
        if a.gorilla_accepted not in ("yes", "no", "unknown"):
            a.gorilla_accepted = "unknown"
        a.confidence = max(0.0, min(1.0, float(a.confidence or 0)))
        a.answer = (a.answer or "").strip().strip("\"'“”‘’").strip()
        a.witty_answer = (a.witty_answer or "").strip().strip("\"'“”‘’").strip()[:60]
        a.witty_point = (a.witty_point or "").strip()
        a.wit_score = max(0.0, min(1.0, float(a.wit_score or 0)))
        if not a.witty_answer or not a.witty_point:  # 웃긴 포인트가 없는 오답은 쓰지 않는다
            a.witty_answer, a.witty_point, a.wit_score = "", "", 0.0
        a.fun_welcome = bool(a.fun_welcome)
        return a


# ── 사연·주제 모집 ───────────────────────────────────────────────────
STORY_LIMIT = 200  # 고릴라 공감로그 '200자 내외'

STORY_SCHEMA = {
    "type": "object",
    "properties": {
        "is_call": {"type": "boolean", "description": "진행자가 청취자에게 특정 주제로 사연·메시지를 보내 달라고 했는가"},
        "topic": {"type": "string"},
        "gorilla_accepted": {"type": "string", "enum": ["yes", "no", "unknown"]},
        "deadline_hint": {"type": "string"},
        "experience_id": {"type": "integer", "description": "주제에 맞는 경험 번호, 맞는 경험이 없으면 0"},
        "use_user_line": {"type": "boolean", "description": "그 경험의 '직접 쓴 한 줄'을 그대로 써도 주제에 맞는가"},
        "message": {"type": "string", "description": "공감로그에 보낼 글. 맞는 경험이 없으면 빈 문자열"},
        "added_facts": {"type": "array", "items": {"type": "string"},
                        "description": "경험 재료에 없는데 글에 넣은 내용. 원칙상 비어 있어야 한다"},
        "fit_reason": {"type": "string"},
        "board_only": {"type": "boolean", "description": "진행자가 '게시판에만'·'홈페이지 게시판으로만' 받는다고 했으면 true"},
        "board_title": {"type": "string", "description": "board_only 일 때 게시판 글 제목, 아니면 빈 문자열"},
        "board_body": {"type": "string", "description": "board_only 일 때 게시판 사연 본문, 아니면 빈 문자열"},
        "song": {"type": "string", "description": "진행자가 신청곡도 받으면 경험·보내는 사람 정보의 신청곡, 없으면 빈 문자열"},
    },
    "required": ["is_call", "topic", "gorilla_accepted", "deadline_hint", "experience_id", "use_user_line",
                 "message", "added_facts", "fit_reason", "board_only", "board_title", "board_body", "song"],
    "additionalProperties": False,
}

STORY_SYSTEM = f"""당신은 라디오(SBS·MBC·KBS 등) 녹취를 듣고, 청취자 본인의 '실제 경험' 목록에서 방송 주제에 맞는 것을 골라
앱 채팅(SBS 고릴라 공감로그 등)이나 문자로 보낼 짧은 글 초안을 쓰는 도우미입니다. 녹취는 음성 인식 결과라 오류가 있을 수 있습니다.

판단:
- is_call: 진행자가 청취자에게 특정 주제·질문으로 사연이나 메시지를 보내 달라고 요청했으면 true.
  다른 청취자의 사연을 읽어 주는 중, 광고, 노래 소개만 있으면 false.
- gorilla_accepted: 방송사 앱 채팅(고릴라·공감로그, mini, 콩 등)으로 받는다고 했으면 yes, 문자(#번호)·홈페이지 등 다른 방법만 말했으면 no, 언급 없으면 unknown.
- experience_id: 주제에 자연스럽게 맞는 경험이 있을 때만 그 번호. 억지로 끼워 맞추지 말고, 없으면 0.

글쓰기 원칙 (반드시 지킬 것):
- 화자는 언제나 보내는 사람 본인('나')입니다. 경험에 '이야기 주인공'이 적혀 있으면 그 사람 이야기를 내가 전하는 형식으로,
  적힌 첫머리(예: "제 와이프 이야기인데요")로 시작하고 그 사람은 호칭으로만 부릅니다. 주인공이 없으면 내 이야기입니다.
- 이야기 주인공을 바꾸지 않습니다. 내가 겪은 일을 다른 사람 일로, 다른 사람 일을 내 일로 옮기지 않습니다.
- 고른 경험의 재료에 있는 사건·인물·결과·숫자만 씁니다. 새 사건, 대사, 인물, 결말, 숫자를 만들지 않습니다.
- '정확한 인용'이 아닌 말은 큰따옴표 직접 인용으로 쓰지 않습니다.
- 질병·사고·사망·경제적 곤란·가족 갈등 등 재료에 없는 소재를 덧붙이지 않습니다.
- 가족은 정해 준 호칭으로 부르고, 실명·회사명·연락처·주소를 쓰지 않습니다. ○○로 가려진 부분은 그대로 둡니다.
- 공백 포함 {STORY_LIMIT}자 이내, 채팅에 어울리는 짧고 자연스러운 말투. 정해 준 문체를 따릅니다.
- 경험에 '직접 쓴 한 줄'이 있고 그대로 주제에 맞으면 use_user_line 을 true 로 하고 message 에 그 문장을 그대로 씁니다.
- 재료에 없는 내용을 넣었다면 added_facts 에 빠짐없이 적습니다.

보내는 곳:
- 기본은 앱 채팅(공감로그 등)입니다. message 는 채팅용 짧은 글입니다.
- 진행자가 '게시판에만', '홈페이지 게시판으로만' 받는다고 하면 board_only 를 true 로 하고,
  board_title(20자 이내)과 board_body(공백 포함 300~800자, 같은 원칙: 재료에 있는 사실만)를 씁니다.
- 신청곡: 진행자가 신청곡도 받으면 경험이나 보내는 사람 정보에 적힌 신청곡만 song 에 씁니다. 없으면 빈 문자열 (지어내지 않음).

채팅창 화면 (있을 때만):
- 같은 시간대에 방송사 앱 채팅창을 찍은 화면입니다. 녹취의 받아쓰기 오류를 바로잡고 오늘의 주제·모집 방법을 파악하는 데 함께 씁니다.
- 다른 청취자의 사연·글을 베끼지 않고, 화면의 닉네임·개인정보를 쓰지 않습니다."""


ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "story": {"type": "string", "description": "사연처럼 풀어 쓴 '있었던 일' (첫머리 호칭 문구 없이)"},
                    "highlight": {"type": "string", "description": "이 이야기의 포인트 한 줄"},
                    "ending": {"type": "string", "description": "마무리 한두 문장 (나의 마음·응원)"},
                    "added_facts": {"type": "array", "items": {"type": "string"},
                                    "description": "재료에 없는 사실을 넣었다면 적는다. 원칙상 비어 있어야 한다"},
                },
                "required": ["id", "story", "highlight", "ending", "added_facts"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

ENRICH_SYSTEM = """당신은 청취자 본인('나')의 실제 사건 기록(한 줄)을 라디오 사연 재료로 풀어 쓰는 도우미입니다.
사건은 모두 실제로 있었던 일이며, 사연처럼 읽히도록 살을 붙이되 사실은 바꾸거나 지어내지 않습니다.

살을 붙일 때 쓸 수 있는 것:
- 주어진 사건 한 줄(바꾸면 안 되는 사실)과 시기
- 그 인물에 대해 확인된 정보(관계·나이·직업·성격·취미 등 재료에 적힌 것)
- 그 일을 겪거나 전해 들은 나의 마음(반가움, 뿌듯함, 웃음, 짠함, 응원하는 마음 등)과 그 사람에게 건네고 싶은 말
- 재료 안에서 자연스럽게 이어지는 배경 설명(예: 재료에 '회사원'이면 바쁜 회사 생활 속에서)

쓰지 않는 것:
- 재료에 없는 새 사건·장면·장소·날짜·숫자·인물·결말, 큰따옴표 직접 인용(대사)
- 질병·사고·사망·돈 문제·가족 갈등처럼 재료에 없는 민감 소재
- 실명·회사명·학교명·연락처·주소. 사람은 정해 준 호칭으로만 부르고, ○○로 가려진 부분은 그대로 둡니다.

형식:
- 화자는 언제나 나입니다. 주인공이 다른 사람이면 내가 그 사람 이야기를 전하는 형식이지만,
  "제 와이프 이야기인데요" 같은 첫머리 문구는 쓰지 않습니다(보낼 때 따로 붙입니다).
- story: 공백 포함 250~450자, 존댓말 사연체(~했어요/~더라고요), 시간 순서로 자연스럽게.
- highlight: 이 이야기에서 듣는 사람이 웃거나 공감할 포인트 한 줄.
- ending: 나의 마음이나 응원으로 마무리하는 한두 문장 (새 사실 없이).
- 재료에 없는 사실을 넣었다면 added_facts 에 빠짐없이 적습니다. 정해 준 문체를 따릅니다.
- 받은 사건마다 같은 id 로 하나씩 돌려줍니다."""


def build_enrich_message(profile: dict, items: list[dict]) -> str:
    lines = ["[보내는 사람]", f"- 문체: {profile.get('tone') or '담백하고 따뜻함'}", "", "[실제 사건]"]
    for it in items:
        lines.append(f"#{it['id']} 주인공: {it.get('about') or '나'}" + (f" (나와의 관계: {it['relation']})"
                                                                     if it.get("relation") else ""))
        lines.append(f"  - 시기: {it.get('when') or '모름'}")
        lines.append(f"  - 사건(바꾸면 안 되는 사실): {it['event']}")
        for fact in it.get("facts") or []:
            lines.append(f"  - 확인된 정보: {fact}")
    return "\n".join(lines)


def parse_enriched(text: str) -> list[dict]:
    data = json.loads(text)
    out = []
    for it in data.get("items") or []:
        out.append({"id": int(it.get("id") or 0), "story": (it.get("story") or "").strip(),
                    "highlight": (it.get("highlight") or "").strip(), "ending": (it.get("ending") or "").strip(),
                    "added_facts": [f for f in (it.get("added_facts") or []) if f and f.strip()]})
    return out


def build_story_message(program: str, transcript: str, profile: dict, experiences: list[dict]) -> str:
    lines = [f"프로그램: {program}", "",
             "[보내는 사람]",
             f"- 문체: {profile.get('tone') or '담백함'}",
             f"- 가족 호칭: {profile.get('family_aliases') or '(없음)'}",
             f"- 피할 소재: {profile.get('avoid_topics') or '(없음)'}",
             "", "[실제 경험 목록]"]
    for e in experiences:
        quote_kind = {"exact": "정확한 인용", "gist": "취지만"}.get(e.get("quote_kind"), "없음")
        lines.append(f"#{e['id']} {e.get('label') or ''}".rstrip())
        if e.get("about") and e["about"] != "나":
            lines.append(f"  - 이야기 주인공: {e['about']} (화자는 나 · 첫머리 '{e.get('intro') or ''}')")
        for key, label in [("when_text", "언제"), ("people", "등장"), ("story", "있었던 일"), ("highlight", "포인트"),
                           ("ending", "결말"), ("fixed_facts", "바꾸면 안 되는 사실")]:
            if e.get(key):
                lines.append(f"  - {label}: {e[key]}")
        if e.get("quotes"):
            lines.append(f"  - 기억나는 말({quote_kind}): {e['quotes']}")
        if e.get("gorilla_line"):
            lines.append(f"  - 직접 쓴 한 줄: {e['gorilla_line']}")
        if e.get("song"):
            lines.append(f"  - 신청곡: {e['song']}")
    lines += ["", "[최근 방송 녹취]", transcript]
    return "\n".join(lines)


@dataclass
class StoryAnalysis:
    is_call: bool = False
    topic: str = ""
    gorilla_accepted: str = "unknown"
    deadline_hint: str = ""
    experience_id: int = 0
    use_user_line: bool = False
    message: str = ""
    added_facts: list[str] = field(default_factory=list)
    fit_reason: str = ""
    board_only: bool = False
    board_title: str = ""
    board_body: str = ""
    song: str = ""

    @classmethod
    def from_json(cls, text: str) -> "StoryAnalysis":
        data = json.loads(text)
        a = cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})
        if a.gorilla_accepted not in ("yes", "no", "unknown"):
            a.gorilla_accepted = "unknown"
        a.message = (a.message or "").strip()
        a.board_only = bool(a.board_only)
        a.board_title, a.board_body, a.song = (a.board_title or "").strip(), (a.board_body or "").strip(), (a.song or "").strip()
        a.added_facts = [f for f in (a.added_facts or []) if f and f.strip()]
        return a


# ── 선물(경품) 안내 ───────────────────────────────────────────────────
GIFT_FIELDS = ["gift", "condition", "entry_method", "related", "deadline", "winners", "announce"]
GIFT_SCHEMA = {
    "type": "object",
    "properties": {
        "gifts": {
            "type": "array",
            "description": "녹취에서 진행자가 안내한 선물·경품. 없으면 빈 배열",
            "items": {
                "type": "object",
                "properties": {
                    "gift": {"type": "string", "description": "선물 이름 (예: 커피 기프티콘, 영화 예매권 2매)"},
                    "condition": {"type": "string", "description": "받는 조건 (예: 퀴즈 정답자 중 추첨 3명)"},
                    "entry_method": {"type": "string", "description": "참여 방법 (고릴라 공감로그, mini, 콩, 문자 #1077, 홈페이지 등)"},
                    "related": {"type": "string", "enum": ["quiz", "story", "event", "other"]},
                    "deadline": {"type": "string"},
                    "winners": {"type": "string"},
                    "announce": {"type": "string", "description": "발표·연락 방법"},
                },
                "required": GIFT_FIELDS,
                "additionalProperties": False,
            },
        },
    },
    "required": ["gifts"],
    "additionalProperties": False,
}

GIFT_SYSTEM = """당신은 라디오 녹취(음성 인식 결과, 오류 있을 수 있음)에서 진행자가 청취자에게 안내한 선물·경품 정보를 정리합니다.
- 실제로 청취자에게 주는 선물·경품만 적습니다. 노래 가사, 광고 속 상품 소개, 사연 속 '선물' 이야기는 빼세요.
- 선물마다 받는 조건, 참여 방법, 관련 참여 종류(quiz 퀴즈 / story 사연·주제 / event 기타 이벤트 / other), 마감, 당첨 인원, 발표 방법을 적습니다.
- 녹취에 없는 내용은 빈 문자열로 둡니다. 지어내지 않습니다."""


def parse_gifts(text: str) -> list[dict]:
    data = json.loads(text)
    out = []
    for g in data.get("gifts") or []:
        item = {k: str(g.get(k) or "").strip() for k in GIFT_FIELDS}
        if item["related"] not in ("quiz", "story", "event", "other"):
            item["related"] = "other"
        if item["gift"]:
            out.append(item)
    return out


class AnswererError(RuntimeError):
    pass


def get_api_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    except Exception:
        return None


def save_api_key(key: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)


class ClaudeAnswerer:
    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium", client=None):
        import anthropic

        self._anthropic = anthropic
        self.model = model
        self.effort = effort
        key = get_api_key()
        self.client = client or (anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic())

    @staticmethod
    def _content(user: str, images=()) -> str | list:
        """채팅창 사진이 있으면 사진(시각 표시와 함께) 뒤에 본문 글을 붙인다."""
        if not images:
            return user
        import base64

        blocks = []
        for at, data in images:
            blocks.append({"type": "text", "text": f"[채팅창 화면 {at}]"})
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                       "data": base64.standard_b64encode(data).decode("ascii")}})
        blocks.append({"type": "text", "text": user})
        return blocks

    def _call(self, system: str, user: str, schema: dict, images=()) -> str:
        a = self._anthropic
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",  # 안전 분류기가 거절하면 서버가 권장 모델로 다시 실행
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                system=system,
                messages=[{"role": "user", "content": self._content(user, images)}],
            )
        except a.AuthenticationError:
            raise AnswererError("Claude API 키가 없거나 올바르지 않습니다.")
        except a.PermissionDeniedError:
            raise AnswererError("API 키에 이 모델을 쓸 권한이 없습니다.")
        except a.NotFoundError:
            raise AnswererError(f"모델 '{self.model}'을(를) 찾을 수 없습니다.")
        except a.RateLimitError:
            raise AnswererError("API 사용량 한도에 걸렸습니다. 잠시 뒤 다시 시도합니다.")
        except a.APIStatusError as e:
            raise AnswererError(f"API 오류 {e.status_code}")
        except a.APIConnectionError:
            raise AnswererError("인터넷 연결 오류로 분석하지 못했습니다.")

        if response.stop_reason == "refusal":
            raise AnswererError("모델이 이 요청을 거절했습니다.")
        if response.stop_reason == "max_tokens":
            raise AnswererError("응답이 잘렸습니다.")
        return next((b.text for b in response.content if b.type == "text"), "")

    def analyze(self, program: str, transcript: str, known: list[tuple[int, str]], images=()) -> QuizAnalysis:
        """images: [(시각, JPEG 바이트)] 키워드가 들린 뒤 찍은 채팅창 화면 (녹취와 교차 확인용)."""
        text = self._call(SYSTEM, build_user_message(program, transcript, known), SCHEMA, images)
        try:
            return QuizAnalysis.from_json(text)
        except (ValueError, TypeError, AttributeError) as e:
            raise AnswererError(f"분석 결과를 읽지 못했습니다: {e}")

    def analyze_gifts(self, program: str, transcript: str) -> list[dict]:
        text = self._call(GIFT_SYSTEM, f"프로그램: {program}\n\n최근 방송 녹취:\n{transcript}", GIFT_SCHEMA)
        try:
            return parse_gifts(text)
        except (ValueError, TypeError, AttributeError) as e:
            raise AnswererError(f"분석 결과를 읽지 못했습니다: {e}")

    def enrich_events(self, profile: dict, items: list[dict]) -> list[dict]:
        """실제 사건 한 줄들을 사연 재료로 풀어 쓴다. [{id, story, highlight, ending, added_facts}]"""
        text = self._call(ENRICH_SYSTEM, build_enrich_message(profile, items), ENRICH_SCHEMA)
        try:
            return parse_enriched(text)
        except (ValueError, TypeError, AttributeError) as e:
            raise AnswererError(f"풀어 쓴 결과를 읽지 못했습니다: {e}")

    def analyze_story(self, program: str, transcript: str, profile: dict,
                      experiences: list[dict], images=()) -> "StoryAnalysis":
        text = self._call(STORY_SYSTEM, build_story_message(program, transcript, profile, experiences), STORY_SCHEMA,
                          images)
        try:
            return StoryAnalysis.from_json(text)
        except (ValueError, TypeError, AttributeError) as e:
            raise AnswererError(f"분석 결과를 읽지 못했습니다: {e}")
