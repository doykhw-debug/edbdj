"""방송 녹취(+채팅창 화면)에서 퀴즈 문제·정답·기발한 오답, 사연 주제를 정리한다 (Claude).

키워드가 들렸을 때만 호출한다. 호출 1회에 최근 몇 분 분량의 녹취(수천 토큰)와, 채팅창 영역을 지정했으면
채팅창 사진 몇 장(장당 수백 토큰)을 보낸다.
Claude 를 부르는 길은 둘 (설정 'quizbot.billing'):
- api: API 키(충전 요금). 키는 환경 변수 ANTHROPIC_API_KEY 또는 윈도우 자격 증명 관리자(관리 화면에서 저장)에서 읽는다.
- subscription: Claude 구독. 이 PC 에 로그인해 둔 공식 Claude Code 를 실행한다 (claude_cli.py, 로그인 정보는 다루지 않음).
"""

from __future__ import annotations

import json
import os
import time
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
        "chat_answers": {"type": "array", "description": "채팅창에서 다른 청취자들이 이 문제에 보낸 답을 같은 답끼리 묶은 것",
                         "items": {"type": "object", "properties": {
                             "keyword": {"type": "string", "description": "답의 핵심 낱말 (짧게, 닉네임·문장 없이)"},
                             "count": {"type": "integer", "description": "그 답을 쓴 서로 다른 글 수"}},
                             "required": ["keyword", "count"], "additionalProperties": False}},
        "consensus_reason": {"type": "string", "description": "chat_answers 중 가장 많은 답이 문제에 어울리는 이유 한 문장 (40자 안, 내 말로). 없으면 빈 문자열"},
    },
    "required": ["kind", "duplicate_of", "question", "options", "answer", "confidence", "gorilla_accepted",
                 "entry_instructions", "deadline_hint", "formatted_message", "reasoning_note",
                 "witty_answer", "witty_point", "wit_score", "fun_welcome", "chat_answers", "consensus_reason"],
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
  확실하지 않아도 문제·보기·힌트·채팅창에서 가장 그럴듯한 답을 반드시 하나 적고 confidence 를 낮게 준다
  (퀴즈 답은 틀려도 손해가 없어 비워 두지 않는다). 문제 내용도 힌트도 전혀 없을 때만 빈 문자열과 0.
  삼행시·이행시처럼 정답이 없는 창작 응모는 진행자가 준 글자로 지은 답을 answer 에 적는다.
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
- 다른 청취자들이 이 문제에 올린 답을 chat_answers 에 같은 답끼리 묶어 적는다: keyword 는 답의 핵심 낱말만
  (예: '정답은 사과요~', '사과!!' → keyword '사과', count 2), count 는 그 답을 쓴 서로 다른 글 수.
  문제와 상관없는 인사·잡담·신청곡은 넣지 않는다. 채팅창 화면이 없거나 답이 안 보이면 빈 배열.
- consensus_reason: 가장 많이 나온 답이 문제(질문)에 왜 어울리는지 청취자 말투로 한 문장 (40자 안).
  다른 사람 글을 베끼지 말고 내 말로 쓴다. 채팅 답이 없으면 빈 문자열.
- answer 는 여전히 녹취·힌트로 판단한 정답을 적는다 (채팅 답을 쓸지는 프로그램이 정한다).
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
    chat_answers: list[dict] = field(default_factory=list)
    consensus_reason: str = ""

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
        grouped: dict[str, dict] = {}
        for item in a.chat_answers or []:
            if not isinstance(item, dict):
                continue
            word = " ".join(str(item.get("keyword") or "").split()).strip("\"'“”‘’!?.~ ")[:40]
            try:
                count = max(0, int(item.get("count") or 0))
            except (TypeError, ValueError):
                count = 0
            if word and count:
                key = word.lower()
                grouped[key] = {"keyword": grouped.get(key, {}).get("keyword", word),
                                "count": grouped.get(key, {}).get("count", 0) + count}
        a.chat_answers = sorted(grouped.values(), key=lambda x: -x["count"])
        a.consensus_reason = " ".join((a.consensus_reason or "").split())[:80]
        return a

    def consensus(self, minimum: int = 3) -> dict | None:
        """채팅 답 가운데 minimum 개 이상 겹친 것 (가장 많은 것). 없으면 None."""
        top = self.chat_answers[0] if self.chat_answers else None
        if top and top["count"] >= minimum and (len(self.chat_answers) < 2 or self.chat_answers[1]["count"] < top["count"]):
            return top
        return None


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
        "song": {"type": "string", "description": "신청곡 '가수 - 제목'. 경험·정보의 곡이 주제에 맞으면 그것, 아니면 상황에 맞는 실제 곡. 받지 않으면 빈 문자열"},
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
- 신청곡: 진행자가 신청곡도 받거나 신청곡을 곁들이는 코너면 song 에 '가수 - 제목' 하나를 씁니다.
  경험이나 보내는 사람 정보에 적힌 신청곡이 주제에 맞으면 그것을, 아니면 오늘 주제·사연 분위기·프로그램·시간대·계절에
  어울리는 실제로 있는 노래를 직접 고릅니다 (가수와 제목이 정확한 곡만, 확실하지 않으면 다른 곡). 같은 날 같은 곡을 반복하지 않습니다.
  노래에 얽힌 추억·사건은 지어내지 않습니다 (곡만 신청). 신청곡을 받지 않는 자리면 빈 문자열.

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


# ── 코너 게시판 공지 읽기·사연 고르기 (boards.py — 방송국 상관없이) ────────────
DAYS = ["월", "화", "수", "목", "금", "토", "일"]
NOTICE_SCHEMA = {
    "type": "object",
    "properties": {
        "corner_title": {"type": "string", "description": "화면에 보이는 코너 이름 (예: 오프닝 한마디)"},
        "program": {"type": "string", "description": "프로그램 이름 (예: 정오의 희망곡)"},
        "notice": {"type": "string", "description": "코너 안내·공지 글 그대로 (메뉴·다른 청취자 글 목록은 빼고, 1500자 이내)"},
        "accepting": {"type": "string", "enum": ["yes", "no", "unknown"],
                      "description": "지금 글을 받는가 (마감·종료·쉼이면 no)"},
        "kind": {"type": "string", "enum": ["story", "short", "song", "photo", "other"],
                 "description": "story 사연·이야기 / short 짧은 한마디·한 줄 / song 신청곡 위주 / photo 사진·파일 필수 / other"},
        "theme": {"type": "string", "description": "무엇을 써 달라는지 한 문장 (주제·조건)"},
        "max_chars": {"type": "integer", "description": "글자 수 제한이 적혀 있으면 그 숫자, 없으면 0"},
        "ai_restricted": {"type": "boolean",
                          "description": "AI 작성·대필·창작물·남의 글을 금지하거나 '직접 쓴 글만' 받는다고 적혀 있으면 true"},
        "needs_real": {"type": "boolean", "description": "실제로 겪은 일·실화를 받는다고 하면 true"},
        "deadline": {"type": "string", "description": "마감 날짜가 적혀 있으면 YYYY-MM-DD, 없으면 빈 글"},
        "air_days": {"type": "array", "items": {"type": "string", "enum": DAYS},
                     "description": "이 코너가 방송되는 요일이 적혀 있으면 그 요일들 (매일이면 빈 배열)"},
        "needs_attachment": {"type": "boolean", "description": "사진·파일 첨부가 꼭 필요하면 true"},
        "reason": {"type": "string", "description": "판단 근거 한두 문장"},
    },
    "required": ["corner_title", "program", "notice", "accepting", "kind", "theme", "max_chars", "ai_restricted",
                 "needs_real", "deadline", "air_days", "needs_attachment", "reason"],
    "additionalProperties": False,
}
NOTICE_SYSTEM = """당신은 라디오 프로그램(SBS·MBC·KBS) 홈페이지의 코너 게시판 화면 글을 읽고,
청취자가 이 코너에 글을 올리기 전에 지켜야 할 안내(공지)를 정리하는 도우미입니다.

- 화면 글에는 메뉴, 다른 코너 이름, 다른 청취자들이 올린 글·소개된 글, 광고가 섞여 있습니다.
  코너 안내·공지 문장만 골라 notice 에 원문 그대로 옮깁니다. 소개된 글 예시는 notice 에 넣지 않지만
  어떤 글을 원하는지(kind, theme) 판단할 때는 참고합니다.
- 공지에 적혀 있지 않은 조건은 지어내지 않습니다. 모르면 accepting 은 unknown, 숫자는 0, 날짜는 빈 글.
- 'AI 로 쓴 글 금지', '대필·창작 금지', '직접 쓴 글만', '타인 글 도용 금지(본인 글만)' 같은 문구가 있으면 ai_restricted 를 true 로 합니다.
- 응답은 JSON 하나만."""

PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "experience_id": {"type": "integer", "description": "가장 잘 맞는 경험의 번호, 맞는 것이 없으면 0"},
        "title": {"type": "string", "description": "게시판 글 제목 (30자 이내, 그 경험에 있는 말로만)"},
        "line": {"type": "string",
                 "description": "kind 가 short 일 때만: 그 경험에 있는 사실·말로만 만든 짧은 한마디. 아니면 빈 글"},
        "reason": {"type": "string", "description": "고른 이유 또는 맞는 것이 없는 이유 한 문장"},
    },
    "required": ["experience_id", "title", "line", "reason"],
    "additionalProperties": False,
}
PICK_SYSTEM = """당신은 청취자 본인('나')이 실제로 있었던 일이라고 확인한 경험들 가운데,
라디오 코너 공지가 원하는 글에 가장 잘 맞는 것 하나를 고르는 도우미입니다.

- 공지의 주제(theme)와 글 종류(kind)에 맞지 않으면 억지로 고르지 말고 experience_id 를 0 으로 합니다.
- 제목과 한마디(line)에는 그 경험에 적힌 사실·인물·말만 씁니다. 새 사건·장면·숫자·인물·대사를 지어내지 않습니다.
  가볍게 줄이거나 말투를 다듬는 것은 됩니다. 실명·회사명·연락처는 쓰지 않고 ○○ 로 가린 부분은 그대로 둡니다.
- kind 가 short(짧은 한마디)이면 line 을 글자 수 제한(없으면 120자) 안에서 재치 있게, 그 경험 속 실제 말·상황으로 만듭니다.
  다른 kind 이면 line 은 빈 글입니다 (본문은 사용자가 확인한 글을 그대로 씁니다).
- 응답은 JSON 하나만."""


def build_notice_message(station: str, url: str, page_text: str) -> str:
    return f"방송국: {station}\n코너 주소: {url}\n\n화면 글:\n{page_text}"


def build_pick_message(notice: dict, candidates: list[dict]) -> str:
    lines = [f"코너: {notice.get('program', '')} · {notice.get('corner_title', '')}",
             f"글 종류(kind): {notice.get('kind', '')}", f"주제: {notice.get('theme', '')}",
             f"글자 수 제한: {notice.get('max_chars') or '없음'}", "", "확인한 경험들:"]
    for c in candidates:
        lines.append(f"[{c['id']}] {c.get('label') or ''} — {c.get('story') or ''}"
                     + (f" / 말: {c['quotes']}" if c.get("quotes") else "")
                     + (f" / 포인트: {c['highlight']}" if c.get("highlight") else ""))
    return "\n".join(lines)


def parse_notice(text: str) -> dict:
    data = json.loads(text)
    if not isinstance(data, dict) or "accepting" not in data:
        raise ValueError("공지 정리 결과 형식이 다릅니다")
    data["max_chars"] = max(0, int(data.get("max_chars") or 0))
    data["air_days"] = [d for d in data.get("air_days") or [] if d in DAYS]
    return data


def parse_pick(text: str) -> dict:
    data = json.loads(text)
    if not isinstance(data, dict) or "experience_id" not in data:
        raise ValueError("고른 결과 형식이 다릅니다")
    data["experience_id"] = int(data.get("experience_id") or 0)
    return data


# ── 전 채널 편성표 (schedules.py — MBC·KBS 는 웹 검색으로) ──────────────────
SCHEDULE_RESEARCH_SYSTEM = """당신은 한국 라디오 채널의 '현재' 정규 편성표를 웹에서 찾아 정리하는 도우미입니다.
- 방송국 공식 홈페이지(imbc.com, kbs.co.kr, sbs.co.kr)의 편성표·프로그램 소개를 먼저 찾고,
  부족하면 최근 개편 기사 등 가장 최근 자료로 확인합니다. 오늘 날짜는 {today} 입니다. 지난 개편 전 편성은 쓰지 않습니다.
- 확인하지 못한 시간대·진행자는 비워 둡니다. 지어내지 않습니다.
- 평일과 주말 편성이 다르면 따로 적습니다. 자정을 넘기는 프로그램은 끝 시각을 그대로 적습니다 (예: 23:00~01:00).
- 결과는 프로그램마다 한 줄: 프로그램 이름 | 진행자 | 시작 HH:MM | 끝 HH:MM | 요일(매일/평일/주말/월~금/토,일 …) | 공식 홈페이지 주소(모르면 빈칸)
  마지막 줄에 근거로 삼은 자료(주소)와 그 자료의 날짜를 적습니다."""

SCHEDULE_SCHEMA = {
    "type": "object",
    "properties": {
        "programs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "프로그램 이름"},
                    "host": {"type": "string", "description": "진행자 (모르면 빈 글)"},
                    "start": {"type": "string", "description": "시작 HH:MM (24시간)"},
                    "end": {"type": "string", "description": "끝 HH:MM (24시간, 자정 넘기면 그대로 예: 01:00)"},
                    "days": {"type": "string", "description": "매일 / 평일 / 주말 / 월~금 / 토,일 처럼"},
                    "homepage": {"type": "string", "description": "공식 홈페이지 주소 (모르면 빈 글)"},
                },
                "required": ["title", "host", "start", "end", "days", "homepage"],
                "additionalProperties": False,
            },
        },
        "sources": {"type": "array", "items": {"type": "string"}, "description": "근거 자료 주소"},
        "as_of": {"type": "string", "description": "근거 자료의 날짜·개편 시점 (모르면 빈 글)"},
    },
    "required": ["programs", "sources", "as_of"],
    "additionalProperties": False,
}
SCHEDULE_PARSE_SYSTEM = """라디오 편성표 조사 결과(글)를 정해진 JSON 으로 옮기는 도우미입니다.
글에 있는 것만 옮기고, 없는 프로그램·시간을 만들지 않습니다. 시각은 24시간 HH:MM 으로 바꿉니다
(예: 오후 2시 → 14:00, 새벽 1시 → 01:00). 응답은 JSON 하나만."""


DJ_NOTICE_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean", "description": "DJ 한마디·제작진 공지·고정 공지가 보이면 true"},
        "text": {"type": "string", "description": "그 글 그대로 (보이는 만큼, 200자 안)"},
        "wants_song": {"type": "boolean", "description": "신청곡·듣고 싶은 노래를 보내 달라고 하면 true"},
        "wants_story": {"type": "boolean", "description": "사연·이야기를 보내 달라고 하면 true"},
        "topic": {"type": "string", "description": "오늘의 주제·코너가 적혀 있으면 그것, 없으면 빈 문자열"},
    },
    "required": ["found", "text", "wants_song", "wants_story", "topic"],
    "additionalProperties": False,
}

DJ_NOTICE_SYSTEM = """당신은 라디오 앱(SBS 고릴라, KBS 콩, MBC mini) 채팅 창 사진에서 'DJ 한마디'·제작진 공지·고정 공지를 읽는 도우미입니다.
- 보통 채팅 목록 위쪽 띠('DJ 한마디', '제작진', '[공지]')에 있습니다. 다른 청취자 글은 공지가 아닙니다.
- text 에 보이는 글을 그대로 옮기고(잘린 곳은 보이는 데까지), 신청곡·듣고 싶은 노래를 청하는지(wants_song),
  사연·이야기를 청하는지(wants_story), 오늘의 주제(topic)를 적습니다.
- 보이지 않는 내용을 지어내지 않습니다. 응답은 JSON 하나만."""

TODAY_RESEARCH_SYSTEM = """당신은 라디오 신청곡을 고르기 위해 오늘({today})의 분위기를 웹에서 확인하는 도우미입니다.
- 서울의 오늘 날씨(맑음·비·눈·기온·미세먼지 등)를 날씨 사이트·기사로 확인합니다.
- 오늘·어제 한국에서 많이 이야기되는 가벼운 이슈(계절 행사, 스포츠 경기 결과, 기념일, 연휴 등)를 2~3개 찾습니다.
  사고·재난·범죄·정치 갈등은 신청곡과 어울리지 않으므로 고르지 않습니다.
- 찾지 못한 것은 '확인 못 함'이라고 씁니다. 지어내지 않습니다.
- 결과: 날씨 한 줄, 이슈 두세 줄, 근거로 삼은 자료(주소)."""

SONG_SCHEMA = {
    "type": "object",
    "properties": {
        "artist": {"type": "string"},
        "title": {"type": "string"},
        "reason": {"type": "string", "description": "이 곡을 고른 이유 (날씨·이슈·프로그램 분위기 중 무엇에 맞췄는지)"},
        "message": {"type": "string", "description": "채팅에 보낼 신청 글 (공백 포함 120자 안, 끝에 '가수 - 제목 신청합니다')"},
    },
    "required": ["artist", "title", "reason", "message"],
    "additionalProperties": False,
}

SONG_SYSTEM = """당신은 라디오 청취자로서 채팅(공감로그·mini·콩)에 보낼 신청곡 하나를 고르는 도우미입니다.
- 실제로 있는 곡만 고릅니다 (가수와 제목이 정확해야 함). 확실하지 않은 곡은 고르지 않습니다.
- 오늘의 날씨·이슈(조사 결과)와 프로그램 분위기·시간대, DJ 한마디에 어울리는 곡을 고릅니다.
  조사 결과에 없는 날씨·사건을 지어내지 않습니다.
- 최근에 보낸 곡(목록)은 다시 고르지 않습니다.
- message: 공백 포함 120자 안의 짧은 신청 글. 날씨·이슈를 한마디로 짚고 끝에 '가수 - 제목 신청합니다'.
  보내는 사람의 가족·직장·개인 사연을 지어내지 않습니다. 이모티콘·해시태그는 쓰지 않습니다.
- 응답은 JSON 하나만."""

SONG_CHECK_SYSTEM = """당신은 라디오 신청곡이 실제로 있는 곡인지 웹에서 확인하는 도우미입니다.
- 음원 사이트(멜론·지니·벅스)·위키·기사에서 그 제목의 곡과 부른 가수를 찾습니다.
- 그 가수가 부른 그 제목의 곡이 있으면 '있음', 제목은 있지만 다른 가수의 곡이면 '다른 가수'와 실제로 부른(원곡 또는
  잘 알려진) 가수, 찾지 못하면 '확인 못 함'이라고 씁니다. 지어내지 않습니다."""

SONG_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "exists": {"type": "boolean", "description": "조사 결과로 그 가수의 그 곡이 있다고 확인되면 true"},
        "artist": {"type": "string", "description": "확인된 가수 (다른 가수의 곡이면 실제 가수, 모르면 빈 문자열)"},
        "title": {"type": "string", "description": "확인된 정확한 제목 (모르면 빈 문자열)"},
    },
    "required": ["exists", "artist", "title"],
    "additionalProperties": False,
}

BOX = {"type": "array", "items": {"type": "integer"}}
LOCATE_SCHEMA = {
    "type": "object",
    "properties": {
        "app_visible": {"type": "boolean"},
        "chat_open": {"type": "boolean"},
        "login_required": {"type": "boolean"},
        "input_box": BOX,
        "send_box": BOX,
        "chat_box": BOX,
        "toggle_box": BOX,
        "note": {"type": "string"},
    },
    "required": ["app_visible", "chat_open", "login_required", "input_box", "send_box", "chat_box", "toggle_box", "note"],
    "additionalProperties": False,
}

LOCATE_SYSTEM = """당신은 한국 라디오 앱(SBS 고릴라, KBS 콩, MBC mini) 창을 찍은 화면 사진에서 채팅 부분의 위치를 찾는 도우미입니다.
좌표는 받은 사진의 픽셀이며 왼쪽 위가 (0, 0) 입니다. 네모는 [왼쪽 x, 위 y, 오른쪽 x, 아래 y] 정수 4개로 적고, 보이지 않으면 빈 배열 [] 입니다.
- input_box: 청취자가 채팅 글을 적는 입력칸 (예: '공감로그를 남기시려면 로그인 해주세요', '로그인 후 채팅에 참여할 수 있습니다',
  'mini 메시지 작성 (200자 내외)'). 글자가 들어가는 칸 전체.
- send_box: 그 입력칸의 글을 보내는 버튼 ('전송'·'등록'·'보내기'). 로그인 전이라 같은 자리에 '로그인' 버튼이 있으면 그 버튼.
- chat_box: 다른 청취자 글이 올라오는 채팅 목록 (보통 입력칸 위). 공지 띠·탭·플레이어는 넣지 않습니다.
- chat_open: 채팅 목록과 입력칸이 보이면 true. 채팅이 닫혀 있으면 false 로 하고, 채팅을 여는 버튼(말풍선 아이콘 등)이 보이면 toggle_box 에.
- login_required: 로그인해야 글을 쓸 수 있다는 안내가 보이면 true.
- app_visible: 사진에 라디오 앱 창이 보이면 true.
확실하지 않은 것은 빈 배열로 둡니다. 지어내지 않습니다. 응답은 JSON 하나만."""


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


BILLINGS = {"subscription": "Claude 구독 (Claude Code 로그인) — PC 기본", "api": "API 키 (충전 요금)"}


def billing(conn) -> str:
    from . import config

    from .. import server

    value = config.get(conn, "quizbot.billing")
    if value in BILLINGS:
        return value
    return "api" if server.is_server() else "subscription"   # 기본: PC 는 Claude 구독, 서버는 API 키


def ready(conn) -> tuple[bool, str]:
    """Claude 를 부를 수 있는지 (가볍게: 키가 있는지 / claude 프로그램이 있는지). (가능, 안 될 때 안내)"""
    from . import claude_cli, config

    has_key = bool(get_api_key())
    if billing(conn) == "api":
        return has_key, "" if has_key else "API 키 방식인데 API 키가 없습니다 (설정 → Claude 연결에서 'Claude 구독'을 고르거나 키 저장)."
    if claude_cli.find_claude():
        return True, ""
    if has_key and config.get(conn, "quizbot.billing_fallback") == "1":
        return True, "Claude Code 가 없어 API 키로 대신합니다."
    return False, "Claude 구독으로 쓰려면 이 컴퓨터에 Claude Code 를 설치하고 한 번 로그인하세요 (설정 화면 안내)."


def _note(message: str) -> None:
    """구독 → API 키로 대신한 일을 작업 기록에 (같은 내용은 30분에 한 번만)."""
    now = time.time()
    if now - _NOTED.get(message, 0) < 1800:
        return
    _NOTED[message] = now
    try:
        from .. import db

        conn = db.connect()
        try:
            db.log(conn, "quizbot", message)
        finally:
            conn.close()
    except Exception:
        pass


_NOTED: dict[str, float] = {}


def make(conn, **kwargs) -> "ClaudeAnswerer":
    """설정(모델·노력·결제 방식)대로 만든다."""
    from . import config

    return ClaudeAnswerer(config.get(conn, "quizbot.model"), config.get(conn, "quizbot.effort"),
                          billing=billing(conn), fallback=config.get(conn, "quizbot.billing_fallback") == "1",
                          note=_note, **kwargs)


class ClaudeAnswerer:
    def __init__(self, model: str = "claude-sonnet-5-5", effort: str = "low", client=None, billing: str = "api",
                 fallback: bool = False, cli=None, note=None):
        self.model = model
        self.effort = effort
        self.billing = billing if billing in BILLINGS else "api"
        self.fallback = fallback          # 구독으로 안 될 때 API 키(충전 요금)로 이어서
        self.note = note or (lambda _message: None)
        self._client = client
        if cli is None:
            from . import claude_cli as cli
        self.cli = cli
        if self.billing == "api":
            self.client   # API 키 방식은 바로 준비 (anthropic 이 없으면 여기서 알림)

    @property
    def client(self):
        if self._client is None:
            import anthropic

            key = get_api_key()
            self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        return self._client

    @property
    def _anthropic(self):
        import anthropic

        return anthropic

    def paused(self) -> str:
        """구독 한도로 쉬는 중이면 그 안내 (API 키로 이어 갈 수 있게 켜 두었으면 빈 글 — 그때는 API 키로 분석)."""
        if self.billing != "subscription" or not getattr(self.cli, "limited", lambda: False)():
            return ""
        if self.fallback and get_api_key():
            return ""
        note = getattr(self.cli, "pause_note", lambda: "")()
        return f"Claude 구독 한도에 걸려 {note + ' ' if note else ''}분석을 쉽니다 (듣기·자막·기록은 계속)"

    def _subscription(self, run, what: str):
        """구독(Claude Code)으로 해 보고, 안 되면 설정에 따라 API 키로 대신하거나 오류."""
        try:
            return run()
        except self.cli.ClaudeCliError as e:
            if self.fallback and get_api_key():
                self.note(f"Claude 구독으로 {what}하지 못해 API 키(충전 요금)로 대신함: {str(e)[:150]}")
                return None
            raise AnswererError(str(e))

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
        if self.billing == "subscription":
            done = self._subscription(lambda: self.cli.run(system, self._content(user, images), schema=schema,
                                                           model=self.model, effort=self.effort), "분석")
            if done is not None:
                return json.dumps(done["data"], ensure_ascii=False)
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

    def research_schedule(self, channel: str, today: str, max_searches: int = 8) -> str:
        """웹 검색(서버 도구)으로 채널의 현재 편성표를 찾아 글로 받는다. (구조화 출력은 웹 검색 인용과 함께 못 써서 두 단계)"""
        ask = f"채널: {channel}\n이 채널의 현재 정규 편성표(모든 시간대)를 찾아 정리해 주세요."
        return self._web_research(SCHEDULE_RESEARCH_SYSTEM.format(today=today), ask, max_searches, "편성표를 찾")

    def research_today(self, today: str, max_searches: int = 4) -> str:
        """신청곡용: 오늘 서울 날씨와 가벼운 이슈를 웹에서 확인한 글."""
        return self._web_research(TODAY_RESEARCH_SYSTEM.format(today=today),
                                  f"오늘은 {today} 입니다. 서울 날씨와 가벼운 이슈를 확인해 주세요.", max_searches,
                                  "오늘 날씨·이슈를 찾")

    def read_dj_notice(self, program: str, jpeg: bytes) -> dict:
        """채팅 창 사진 → DJ 한마디·공지 (DJ_NOTICE_SCHEMA)."""
        text = self._call(DJ_NOTICE_SYSTEM, f"프로그램: {program}\n채팅 창 사진에서 DJ 한마디·공지를 읽어 주세요.",
                          DJ_NOTICE_SCHEMA, images=[("채팅 창", jpeg)])
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("JSON 객체가 아님")
            return data
        except (ValueError, TypeError) as e:
            raise AnswererError(f"DJ 한마디를 읽지 못했습니다: {e}")

    def pick_song(self, program: str, channel: str, now: str, notice: str, today: str, avoid: list[str]) -> dict:
        """신청곡 하나 (SONG_SCHEMA). today: research_today 결과."""
        user = (f"프로그램: {program} ({channel})\n지금: {now}\nDJ 한마디·공지: {notice or '(없음)'}\n\n"
                f"오늘 날씨·이슈 (웹에서 확인):\n{today or '(확인 못 함)'}\n\n"
                f"최근에 보낸 곡 (다시 고르지 않음): {', '.join(avoid) or '(없음)'}")
        text = self._call(SONG_SYSTEM, user, SONG_SCHEMA)
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("JSON 객체가 아님")
            return data
        except (ValueError, TypeError) as e:
            raise AnswererError(f"신청곡 결과를 읽지 못했습니다: {e}")

    def verify_song(self, artist: str, title: str) -> dict:
        """신청곡이 실제로 있는지 웹에서 확인 → {exists, artist, title} (다른 가수의 곡이면 실제 가수)."""
        research = self._web_research(SONG_CHECK_SYSTEM, f"가수: {artist}\n제목: {title}\n이 곡이 실제로 있는지 확인해 주세요.",
                                      3, "신청곡을 확인하")
        text = self._call("곡 확인 조사 글을 정해진 JSON 으로 옮깁니다. 글에 없는 내용은 만들지 않습니다. 응답은 JSON 하나만.",
                          f"확인한 곡: {artist} - {title}\n\n조사 결과:\n{research}", SONG_CHECK_SCHEMA)
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("JSON 객체가 아님")
            return data
        except (ValueError, TypeError) as e:
            raise AnswererError(f"곡 확인 결과를 읽지 못했습니다: {e}")

    def _web_research(self, system: str, ask: str, max_searches: int, what: str) -> str:
        """웹 검색으로 조사한 글 (구독: Claude Code 의 WebSearch / API 키: 서버 웹 검색 도구)."""
        if self.billing == "subscription":
            done = self._subscription(lambda: self.cli.run(
                system + f"\n- 웹 검색은 {max_searches}번 안에서 끝냅니다.",
                ask, tools=("WebSearch",), model=self.model, effort=self.effort), what)
            if done is not None:
                text = done["text"] if done["text"].strip() else "\n".join(done["texts"])
                if not text.strip():
                    raise AnswererError(f"{what}지 못했습니다 (빈 응답).")
                return text
        a = self._anthropic
        user = {"role": "user", "content": ask}
        assistant: list = []
        texts: list[str] = []
        for _ in range(4):   # 서버 쪽 검색이 길어지면 pause_turn 으로 끊기므로 이어서 요청
            messages = [user] + ([{"role": "assistant", "content": assistant}] if assistant else [])
            try:
                response = self.client.messages.create(
                    model=self.model, max_tokens=16000,
                    system=system,
                    tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}],
                    output_config={"effort": self.effort}, messages=messages)
            except a.AuthenticationError:
                raise AnswererError("Claude API 키가 없거나 올바르지 않습니다.")
            except a.BadRequestError as e:
                raise AnswererError(f"웹 검색 요청이 거절됨 (Claude Console 에서 웹 검색이 꺼져 있을 수 있음): {str(e)[:120]}")
            except a.RateLimitError:
                raise AnswererError("API 사용량 한도에 걸렸습니다. 잠시 뒤 다시 시도합니다.")
            except a.APIStatusError as e:
                raise AnswererError(f"API 오류 {e.status_code}")
            except a.APIConnectionError:
                raise AnswererError(f"인터넷 연결 오류로 {what}지 못했습니다.")
            if response.stop_reason == "refusal":
                raise AnswererError("모델이 이 요청을 거절했습니다.")
            texts += [b.text for b in response.content if getattr(b, "type", "") == "text"]
            if response.stop_reason != "pause_turn":
                break
            assistant = assistant + list(response.content)
        text = "\n".join(t for t in texts if t.strip())
        if not text.strip():
            raise AnswererError(f"{what}지 못했습니다 (빈 응답).")
        return text

    def parse_schedule(self, research: str) -> dict:
        """조사 글 → {programs: [...], sources, as_of}."""
        text = self._call(SCHEDULE_PARSE_SYSTEM, research, SCHEDULE_SCHEMA)
        try:
            data = json.loads(text)
            if not isinstance(data.get("programs"), list):
                raise ValueError("programs 가 없음")
            return data
        except (ValueError, TypeError, AttributeError) as e:
            raise AnswererError(f"편성표 정리 결과를 읽지 못했습니다: {e}")

    def read_notice(self, station: str, url: str, page_text: str) -> dict:
        """코너 게시판 화면 글 → 공지 정리 (NOTICE_SCHEMA)."""
        text = self._call(NOTICE_SYSTEM, build_notice_message(station, url, page_text), NOTICE_SCHEMA)
        try:
            return parse_notice(text)
        except (ValueError, TypeError, AttributeError) as e:
            raise AnswererError(f"공지 정리 결과를 읽지 못했습니다: {e}")

    def pick_board_story(self, notice: dict, candidates: list[dict]) -> dict:
        """공지에 맞는 확인한 경험 하나 (PICK_SCHEMA)."""
        text = self._call(PICK_SYSTEM, build_pick_message(notice, candidates), PICK_SCHEMA)
        try:
            return parse_pick(text)
        except (ValueError, TypeError, AttributeError) as e:
            raise AnswererError(f"고른 결과를 읽지 못했습니다: {e}")

    def locate_chat_ui(self, app_label: str, jpeg: bytes, width: int, height: int) -> dict:
        """앱 창 사진 → 입력칸·전송 버튼·채팅 목록·채팅 여는 버튼 네모 (사진 픽셀 좌표, LOCATE_SCHEMA)."""
        user = (f"{app_label} 앱 창을 찍은 사진입니다 (가로 {width} × 세로 {height} 픽셀). "
                "입력칸·전송 버튼·채팅 목록의 위치를 찾아 주세요.")
        text = self._call(LOCATE_SYSTEM, user, LOCATE_SCHEMA, images=[("앱 창", jpeg)])
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("JSON 객체가 아님")
            return data
        except (ValueError, TypeError) as e:
            raise AnswererError(f"위치 찾기 결과를 읽지 못했습니다: {e}")

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
