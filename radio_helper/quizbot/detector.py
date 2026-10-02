"""녹취에서 '퀴즈가 나오는 중'인 순간을 찾는다.

매 녹음 단위마다 유료 분석을 부르지 않도록, 퀴즈 신호 단어가 들리면 문제가 끝까지 나올 때까지
settle_seconds 기다린 뒤 한 번 분석한다. 분석 사이에는 cooldown_seconds 를 둔다.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# 이 표현이 들리면 퀴즈가 나오는 중일 가능성이 높다. ('고릴라로 보내 주세요'만으로는 사연 안내일 수 있어 제외)
SIGNALS = [r"퀴즈", r"정답", r"초성", r"맞[혀춰]\s*(보|주)", r"문제\s*(나갑|드립|낼|냅|입니다|하나)", r"오늘의\s*문제"]
_SIGNALS = [re.compile(p) for p in SIGNALS]


def is_quiz_signal(text: str) -> bool:
    return any(p.search(text) for p in _SIGNALS)


# 진행자가 청취자에게 주제를 주고 사연·메시지를 보내 달라고 할 때.
# 라디오는 '사연'이라는 말을 자주 하므로(사연 소개 등) '보내 달라·주제' 같은 요청 표현과 함께일 때만 본다.
STORY_SIGNALS = [
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

    def is_due(self, now: datetime) -> bool:
        return self.due_at is not None and now >= self.due_at

    def mark_analyzed(self, now: datetime) -> None:
        self.last_analysis = now
        self.due_at = None
