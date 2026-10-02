"""방송 녹취에서 퀴즈 문제·정답을 정리한다 (Claude API, 유료).

퀴즈 신호가 들렸을 때만 호출한다. 호출 1회에 최근 몇 분 분량의 녹취(수천 토큰)를 보낸다.
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
        "reasoning_note": {"type": "string", "description": "판단 근거 한두 문장"},
    },
    "required": ["kind", "duplicate_of", "question", "options", "answer", "confidence", "gorilla_accepted",
                 "entry_instructions", "deadline_hint", "formatted_message", "reasoning_note"],
    "additionalProperties": False,
}

SYSTEM = """당신은 SBS 파워FM 라디오 방송의 음성 인식 녹취를 읽고, 청취자 참여 퀴즈를 정리하는 도우미입니다.
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
- gorilla_accepted: 진행자가 고릴라(앱)·공감로그·채팅으로 정답을 받는다고 말했으면 yes,
  문자(#번호)나 전화 등 다른 방법만 말했으면 no, 언급이 없으면 unknown.
- formatted_message: 진행자가 '정답 앞에 #퀴즈를 붙여서' 같은 전송 형식을 정한 경우에만 그 형식으로 쓴 문구.
- 녹취에 없는 내용을 지어내지 않는다. 사람 이름·연락처를 만들어 넣지 않는다."""


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
        return a


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

    def analyze(self, program: str, transcript: str, known: list[tuple[int, str]]) -> QuizAnalysis:
        a = self._anthropic
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",  # 안전 분류기가 거절하면 서버가 권장 모델로 다시 실행
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": SCHEMA}},
                system=SYSTEM,
                messages=[{"role": "user", "content": build_user_message(program, transcript, known)}],
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
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return QuizAnalysis.from_json(text)
        except (ValueError, TypeError) as e:
            raise AnswererError(f"분석 결과를 읽지 못했습니다: {e}")
