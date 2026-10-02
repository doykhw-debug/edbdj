"""예약 시간 계산. 자정을 넘기는 예약(23:00~01:00)도 처리한다."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

DAY_NAMES = "월화수목금토일"


@dataclass(frozen=True)
class Window:
    schedule_id: int
    start: datetime
    end: datetime

    @property
    def broadcast_date(self) -> str:
        return self.start.strftime("%Y-%m-%d")


_HM = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def valid_hm(text: str | None) -> bool:
    return bool(text and _HM.match(text.strip()))


def _hm(text: str) -> tuple[int, int]:
    h, m = text.strip().split(":")
    return int(h), int(m)


def window_for(schedule, day: datetime) -> Window:
    """day 날짜에 시작하는 예약 구간. end 가 start 보다 이르면 다음 날로 넘긴다."""
    sh, sm = _hm(schedule["start_time"])
    eh, em = _hm(schedule["end_time"])
    start = day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=sh, minutes=sm)
    end = day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=eh, minutes=em)
    if end <= start:
        end += timedelta(days=1)
    return Window(schedule["id"], start, end)


def active_window(schedules, now: datetime) -> Window | None:
    """지금 진행 중인 예약 구간. 어제 시작해 자정을 넘긴 구간도 본다."""
    for s in schedules:
        if not s["enabled"]:
            continue
        for day in (now, now - timedelta(days=1)):
            if str(day.weekday()) not in (s["days"] or ""):
                continue
            w = window_for(s, day)
            if w.start <= now < w.end:
                return w
    return None


def next_window(schedules, now: datetime) -> Window | None:
    best = None
    for s in schedules:
        if not s["enabled"]:
            continue
        for offset in range(0, 8):
            day = now + timedelta(days=offset)
            if str(day.weekday()) not in (s["days"] or ""):
                continue
            w = window_for(s, day)
            if w.start > now and (best is None or w.start < best.start):
                best = w
                break
    return best


def days_label(days: str) -> str:
    days = days or ""
    if days == "0123456":
        return "매일"
    if days == "01234":
        return "평일"
    if days == "56":
        return "주말"
    return "".join(DAY_NAMES[int(d)] for d in sorted(set(days)) if d.isdigit() and int(d) < 7)
