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

    def feed(self, at: datetime, text: str) -> None:
        if self.due_at is None and is_quiz_signal(text):
            earliest = at + timedelta(seconds=self.settle_seconds)
            if self.last_analysis is not None:
                earliest = max(earliest, self.last_analysis + timedelta(seconds=self.cooldown_seconds))
            self.due_at = earliest

    def is_due(self, now: datetime) -> bool:
        return self.due_at is not None and now >= self.due_at

    def mark_analyzed(self, now: datetime) -> None:
        self.last_analysis = now
        self.due_at = None
