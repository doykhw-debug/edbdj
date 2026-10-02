"""녹취에서 '퀴즈·사연·선물이 나오는 중'인 순간을 찾는다.

키워드: 퀴즈·정답·오답·힌트(퀴즈) / 사연·신청곡·게시판(사연) / 선물(선물)

매 녹음 단위마다 유료 분석을 부르지 않도록, 퀴즈 신호 단어가 들리면 문제가 끝까지 나올 때까지
settle_seconds 기다린 뒤 한 번 분석한다. 분석 사이에는 cooldown_seconds 를 둔다.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# 이 표현이 들리면 퀴즈가 나오는 중일 가능성이 높다. ('고릴라로 보내 주세요'만으로는 사연 안내일 수 있어 제외)
SIGNALS = [r"퀴즈", r"정답", r"오답", r"힌트", r"초성", r"맞[혀춰]\s*(보|주)", r"문제\s*(나갑|드립|낼|냅|입니다|하나)",
           r"오늘의\s*문제"]
_SIGNALS = [re.compile(p) for p in SIGNALS]


def is_quiz_signal(text: str) -> bool:
    return any(p.search(text) for p in _SIGNALS)


# 진행자가 청취자에게 주제를 주고 사연·신청곡을 보내 달라고 할 때.
# '사연'·'신청곡'·'게시판'은 소개할 때도 자주 나오지만, 놓치지 않도록 들리면 바로 신호로 본다.
# (실제 모집인지는 분석 단계에서 가리고, 분석은 사이 간격·횟수 상한으로 아낀다)
STORY_SIGNALS = [
    r"사연", r"신청곡", r"게시판",
    r"사연\s*(을|를|도|들)?\s*((보내|남겨|올려)(?!\s*주신|\s*주셨)|기다|받습|받아요|모집|주세요|참여)",
    r"(오늘의|이번\s*주|오늘)\s*(주제|키워드|질문|테마)",
    r"주제(는|로|가)\s",
    r"(이야기|경험|에피소드|추억)\s*(을|를|도)?\s*(보내|남겨|들려|나눠|공유)",
    r"공감\s*로그\s*(로|에)?\s*(보내|남겨|참여|많이)",
    r"문자\s*(로|를)?\s*(보내|남겨|참여)",
]
_STORY_SIGNALS = [re.compile(p) for p in STORY_SIGNALS]


def is_story_signal(text: str) -> bool:
    return any(p.search(text) for p in _STORY_SIGNALS)


@dataclass
class TranscriptBuffer:
    max_seconds: int = 600
    lines: deque = field(default_factory=deque)

    def add(self, at: datetime, text: str) -> None:
        if text:
            self.lines.append((at, text))
        while self.lines and (at - self.lines[0][0]).total_seconds() > self.max_seconds:
            self.lines.popleft()

    def window(self, now: datetime, seconds: int) -> str:
        since = now - timedelta(seconds=seconds)
        return "\n".join(f"[{t:%H:%M:%S}] {text}" for t, text in self.lines if t >= since)


@dataclass
class Detector:
    settle_seconds: int = 40
    cooldown_seconds: int = 60
    due_at: datetime | None = None
    last_analysis: datetime | None = None

    signal: object = is_quiz_signal

    def feed(self, at: datetime, text: str) -> None:
        if self.due_at is None and self.signal(text):
            earliest = at + timedelta(seconds=self.settle_seconds)
            if self.last_analysis is not None:
                earliest = max(earliest, self.last_analysis + timedelta(seconds=self.cooldown_seconds))
            self.due_at = earliest

    @property
    def armed(self) -> bool:
        """키워드를 듣고 분석을 기다리는 중 (이동안 채팅창도 함께 모은다)."""
        return self.due_at is not None

    def is_due(self, now: datetime) -> bool:
        return self.due_at is not None and now >= self.due_at

    def mark_analyzed(self, now: datetime) -> None:
        self.last_analysis = now
        self.due_at = None


# 선물·경품 안내. 노래 가사의 '선물' 등은 분석 단계에서 걸러진다.
GIFT_SIGNALS = [r"선물", r"경품", r"기프티콘", r"쿠폰", r"상품권", r"추첨", r"증정", r"당첨"]
_GIFT_SIGNALS = [re.compile(p) for p in GIFT_SIGNALS]


def is_gift_signal(text: str) -> bool:
    return any(p.search(text) for p in _GIFT_SIGNALS)
