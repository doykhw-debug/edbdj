"""'청취 시작' 한 번으로 바로 듣기: 예약 없이 지금 듣는 채널을 계속 듣는다.

- 지금 시각과 채널로 현재 프로그램을 찾는다 (편성에 없으면 '러브FM 방송'처럼 채널로 기록).
- 고릴라 공감로그로 퀴즈 정답·사연을 보내는 권한을 모든 채널에 연다 (설정에서 끌 수 있음).
- 관리 화면·자막 창이 보는 '지금 상태'(소리 크기, 최근 자막, 최근 감지)를 만든다.
"""

from __future__ import annotations

import json
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


# 화면에 표시하는 키워드 → 종류(색)
KEYWORD_KIND = {"퀴즈": "퀴즈", "정답": "퀴즈", "오답": "퀴즈", "힌트": "퀴즈",
                "사연": "사연", "신청곡": "사연", "게시판": "사연", "선물": "선물"}


def keywords(text: str) -> list[str]:
    """문장에 나온 키워드 (화면 강조용). 키워드 낱말이 없어도 신호 표현이면 종류 이름을 붙인다."""
    found = [k for k in KEYWORD_KIND if k in text]
    for kind, signal in (("퀴즈", is_quiz_signal), ("사연", is_story_signal), ("선물", is_gift_signal)):
        if signal(text) and not any(KEYWORD_KIND[k] == kind for k in found):
            found.append(kind)
    return found


def _age_seconds(stamp: str, now: datetime) -> float | None:
    try:
        return (now - datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")).total_seconds()
    except (TypeError, ValueError):
        return None


LAUNCH_GRACE_SECONDS = 90  # '청취 시작'을 누른 뒤 실행기가 첫 신호를 보내기까지 기다리는 시간

STATUS_TEXT = {"collecting": "수집 중", "starting": "준비 중", "launching": "시작하는 중", "stalled": "응답 없음",
               "off": "꺼짐"}


def _json_setting(conn: sqlite3.Connection, key: str):
    try:
        return json.loads(db.get_setting(conn, key) or "null")
    except ValueError:
        return None


def view_model(conn: sqlite3.Connection, now: datetime | None = None, lines: int = 20) -> dict:
    """관리 화면과 자막 창이 함께 쓰는 지금 상태."""
    now = now or datetime.now()
    alive = config.runner_alive(conn, now)
    active = is_active(conn)
    level_age = _age_seconds(db.get_setting(conn, "quizbot.level_at"), now)
    launched_age = _age_seconds(db.get_setting(conn, "quizbot.launched_at"), now)
    collecting = alive and active and level_age is not None and level_age < 20
    rows = conn.execute("SELECT * FROM transcripts ORDER BY id DESC LIMIT ?", (lines,)).fetchall()
    events = conn.execute("SELECT at, message FROM events WHERE kind IN ('quizbot', 'gorilla') "
                          "ORDER BY id DESC LIMIT 8").fetchall()
    pending_quiz = conn.execute("SELECT COUNT(*) FROM quizzes WHERE source = 'auto' AND entry_status = 'pending' "
                                "AND approved = 0").fetchone()[0]
    pending_story = conn.execute("SELECT COUNT(*) FROM story_posts WHERE status = 'pending' AND approved = 0"
                                 ).fetchone()[0]
    channel = config.get(conn, "live.channel")
    if not active:
        status = "off"
    elif collecting:
        status = "collecting"
    elif alive:
        status = "starting"
    elif launched_age is not None and launched_age < LAUNCH_GRACE_SECONDS:
        status = "launching"
    else:
        status = "stalled"  # 켜 두었는데 실행기 신호가 없다 → 꺼졌거나 멈춤
    stt_test = _json_setting(conn, "quizbot.stt_test")
    if stt_test and stt_test.get("running"):
        age = _age_seconds(stt_test.get("at"), now)
        stt_test["stuck"] = age is not None and age > 600  # 10분 넘게 진행 중이면 도중에 꺼진 것
    return {
        "status": status,
        "status_text": STATUS_TEXT[status],
        "active": active, "alive": alive, "channel": channel,
        "program": program_label(conn, channel, now),
        "state": db.get_setting(conn, "quizbot.state"),
        "level": int(db.get_setting(conn, "quizbot.level") or 0) if collecting else 0,
        "lines": [{"at": r["at"][11:19], "text": r["text"], "keywords": keywords(r["text"]),
                   "kinds": [KEYWORD_KIND[k] for k in keywords(r["text"])]} for r in reversed(rows)],
        "events": [{"at": e["at"][11:16], "message": e["message"]} for e in events],
        "pending": {"quizzes": pending_quiz, "stories": pending_story},
        "last_chunk": _json_setting(conn, "quizbot.last_chunk") if active else None,
        "stt_test": stt_test,
    }


def chunk_text(chunk: dict | None) -> str:
    """'마지막 10초' 한 줄."""
    if not chunk:
        return ""
    said = f"'{chunk['text']}'" if chunk.get("text") else "말소리 없음"
    return f"마지막 녹음 {chunk['at']} · 소리 {chunk['level']}/100 · 받아쓰기 {chunk['seconds']}초 → {said}"
