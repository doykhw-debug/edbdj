"""'청취 시작' 한 번으로 바로 듣기: 예약 없이 지금 듣는 채널을 계속 듣는다.

- 지금 시각과 채널로 현재 프로그램을 찾는다 (편성에 없으면 '러브FM 방송'처럼 채널로 기록).
- 고릴라 공감로그로 퀴즈 정답·사연을 보내는 권한을 모든 채널에 연다 (설정에서 끌 수 있음).
- 관리 화면·자막 창이 보는 '지금 상태'(소리 크기, 최근 자막, 최근 감지)를 만든다.
"""

from __future__ import annotations

import math
import re
import sqlite3
from datetime import datetime, timedelta

from .. import db
from . import config
from .detector import is_gift_signal, is_quiz_signal, is_story_signal

WEEKDAY = "월화수목금토일"


def days_match(days: str | None, d: datetime) -> bool:
    """'매일', '평일', '주말', '월~금', '토,일', '월수금' 같은 표현이 그 날짜에 해당하는지."""
    text = re.sub(r"\s+", "", days or "")
    wd = d.weekday()
    if not text or "매일" in text:
        return True
    if "평일" in text:
        return wd < 5
    if "주말" in text:
        return wd >= 5
    m = re.search(r"([월화수목금토일])요?일?[~∼\-–]([월화수목금토일])", text)
    if m:
        a, b = WEEKDAY.index(m.group(1)), WEEKDAY.index(m.group(2))
        return a <= wd <= b if a <= b else (wd >= a or wd <= b)
    named = [WEEKDAY.index(ch) for ch in text if ch in WEEKDAY]
    return wd in named if named else True


def _minutes(hm: str) -> int | None:
    try:
        h, m = hm.split(":")
        return int(h) * 60 + int(m)
    except (AttributeError, ValueError):
        return None


def current_program(conn: sqlite3.Connection, channel: str, now: datetime) -> str | None:
    t = now.hour * 60 + now.minute
    for p in conn.execute("SELECT * FROM programs WHERE channel = ? AND on_air = 1", (channel,)):
        start, end = _minutes(p["start_time"]), _minutes(p["end_time"])
        if start is None or end is None or start == end:
            continue
        if start < end:
            if start <= t < end and days_match(p["days"], now):
                return p["title"]
        elif t >= start and days_match(p["days"], now):           # 23:00 ~ 01:00 의 앞부분
            return p["title"]
        elif t < end and days_match(p["days"], now - timedelta(days=1)):  # 자정 넘긴 뒷부분
            return p["title"]
    return None


def program_label(conn: sqlite3.Connection, channel: str, now: datetime) -> str:
    # 편성에 없는 시간은 채널 이름으로만 묶는다. 시각을 넣으면 정시가 지날 때 이름이 바뀌어
    # 같은 퀴즈의 재안내를 새 문제로 보고 또 보낼 수 있다.
    return current_program(conn, channel, now) or f"{channel} 방송"


def flag(conn: sqlite3.Connection, key: str) -> int:
    return 1 if config.get(conn, key) == "1" else 0


def is_active(conn: sqlite3.Connection) -> bool:
    return config.get(conn, "live.active") == "1"


def session(conn: sqlite3.Connection, now: datetime) -> dict:
    """예약 행과 같은 모양의 '지금 듣기' 설정. 고릴라 응모는 열어 둔 것으로 본다."""
    channel = config.get(conn, "live.channel")
    try:
        min_conf = float(config.get(conn, "live.min_confidence"))
    except ValueError:
        min_conf = 0.8
    return {
        "id": None, "channel": channel, "program": program_label(conn, channel, now),
        "auto_submit": flag(conn, "live.auto_quiz"), "min_confidence": min_conf,
        "gorilla_confirmed": 1, "story_enabled": 1, "story_auto_user_line": 1,
        "story_auto_ai": flag(conn, "live.auto_story"), "gift_enabled": flag(conn, "live.gift"),
    }


def level_percent(rms: float) -> int:
    """소리 크기(RMS) → 0~100. -60dB 이하는 0."""
    if rms <= 0:
        return 0
    return max(0, min(100, int(round((20 * math.log10(rms) + 60) / 60 * 100))))


def keywords(text: str) -> list[str]:
    kinds = []
    if is_quiz_signal(text):
        kinds.append("퀴즈")
    if is_story_signal(text):
        kinds.append("사연")
    if is_gift_signal(text):
        kinds.append("선물")
    return kinds


def _age_seconds(stamp: str, now: datetime) -> float | None:
    try:
        return (now - datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")).total_seconds()
    except (TypeError, ValueError):
        return None


def view_model(conn: sqlite3.Connection, now: datetime | None = None, lines: int = 20) -> dict:
    """관리 화면과 자막 창이 함께 쓰는 지금 상태."""
    now = now or datetime.now()
    alive = config.runner_alive(conn, now)
    active = is_active(conn)
    level_age = _age_seconds(db.get_setting(conn, "quizbot.level_at"), now)
    collecting = alive and active and level_age is not None and level_age < 20
    rows = conn.execute("SELECT * FROM transcripts ORDER BY id DESC LIMIT ?", (lines,)).fetchall()
    events = conn.execute("SELECT at, message FROM events WHERE kind IN ('quizbot', 'gorilla') "
                          "ORDER BY id DESC LIMIT 8").fetchall()
    pending_quiz = conn.execute("SELECT COUNT(*) FROM quizzes WHERE source = 'auto' AND entry_status = 'pending' "
                                "AND approved = 0").fetchone()[0]
    pending_story = conn.execute("SELECT COUNT(*) FROM story_posts WHERE status = 'pending' AND approved = 0"
                                 ).fetchone()[0]
    channel = config.get(conn, "live.channel")
    if collecting:
        status = "collecting"
    elif active and alive:
        status = "starting"
    elif active:
        status = "launching"
    else:
        status = "off"
    return {
        "status": status,
        "status_text": {"collecting": "수집 중", "starting": "준비 중", "launching": "시작하는 중",
                        "off": "꺼짐"}[status],
        "active": active, "alive": alive, "channel": channel,
        "program": program_label(conn, channel, now),
        "state": db.get_setting(conn, "quizbot.state"),
        "level": int(db.get_setting(conn, "quizbot.level") or 0) if collecting else 0,
        "lines": [{"at": r["at"][11:19], "text": r["text"], "keywords": keywords(r["text"])} for r in reversed(rows)],
        "events": [{"at": e["at"][11:16], "message": e["message"]} for e in events],
        "pending": {"quizzes": pending_quiz, "stories": pending_story},
    }
