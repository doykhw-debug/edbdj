"""퀴즈 자동 참여 설정 (settings 테이블의 quizbot.* / gorilla.* 키)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from datetime import datetime, timedelta

from .. import db

DEFAULTS = {
    # 녹음·인식
    "quizbot.chunk_seconds": "15",
    "quizbot.whisper_model": "small",
    "quizbot.context_seconds": "180",     # 분석에 넘기는 최근 녹취 길이
    "quizbot.settle_seconds": "40",       # 퀴즈 신호 뒤 문제를 끝까지 듣고 분석하기까지 기다리는 시간
    "quizbot.cooldown_seconds": "60",
    "quizbot.max_analyses_per_window": "15",
    "quizbot.max_sends_per_hour": "4",
    # 분석 (Claude API)
    "quizbot.model": "claude-opus-5-5",
    "quizbot.effort": "medium",
    "quizbot.autostart": "0",
    # 고릴라 화면
    "gorilla.process_name": "",           # '자동 찾기'로 저장되는 고릴라 프로그램(실행 파일) 이름
    "gorilla.window_title": "고릴라|gorealra",
    "gorilla.input_mode": "uia",          # uia: 화면 요소로 입력칸 찾기 / coords: 지정한 위치 클릭
    "gorilla.input_auto_id": "",
    "gorilla.input_name": "공감로그|글쓰기",
    "gorilla.send_mode": "auto",          # auto(전송 버튼, 없으면 Enter) / button / enter / coords
    "gorilla.send_button_name": "전송|보내기|등록",
    "gorilla.input_x": "",
    "gorilla.input_y": "",
    "gorilla.send_x": "",
    "gorilla.send_y": "",
    "gorilla.message_template": "{answer}",
}

LABELS = {
    "quizbot.chunk_seconds": "녹음 단위(초)",
    "quizbot.whisper_model": "음성 인식 모델 (tiny/base/small/medium)",
    "quizbot.context_seconds": "분석에 쓰는 최근 녹취(초)",
    "quizbot.settle_seconds": "퀴즈 신호 후 대기(초)",
    "quizbot.cooldown_seconds": "분석 간 최소 간격(초)",
    "quizbot.max_analyses_per_window": "예약 1회당 최대 분석 횟수(비용 상한)",
    "quizbot.max_sends_per_hour": "시간당 최대 전송 수",
    "quizbot.model": "Claude 모델",
    "quizbot.effort": "분석 노력 수준 (low/medium/high)",
    "gorilla.process_name": "고릴라 프로그램 이름 (자동 찾기로 채워짐)",
    "gorilla.window_title": "고릴라 창 제목(정규식, 프로그램 이름이 없을 때)",
    "gorilla.input_mode": "입력칸 찾기 (uia/coords)",
    "gorilla.input_auto_id": "입력칸 AutomationId (uia)",
    "gorilla.input_name": "입력칸 이름 (uia)",
    "gorilla.send_mode": "전송 방법 (auto/button/enter/coords)",
    "gorilla.send_button_name": "전송 버튼 이름(정규식)",
    "gorilla.message_template": "보낼 문구 ({answer} 자리에 정답)",
}


def get(conn: sqlite3.Connection, key: str) -> str:
    return db.get_setting(conn, key, DEFAULTS.get(key, ""))


def get_int(conn: sqlite3.Connection, key: str) -> int:
    try:
        return int(float(get(conn, key)))
    except ValueError:
        return int(DEFAULTS[key])


def get_float(conn: sqlite3.Connection, key: str) -> float | None:
    v = get(conn, key)
    try:
        return float(v) if v != "" else None
    except ValueError:
        return None


@dataclass
class GorillaConfig:
    process_name: str = ""
    window_title: str = DEFAULTS["gorilla.window_title"]
    input_mode: str = "uia"
    input_auto_id: str = ""
    input_name: str = DEFAULTS["gorilla.input_name"]
    send_mode: str = "auto"
    send_button_name: str = DEFAULTS["gorilla.send_button_name"]
    input_x: float | None = None
    input_y: float | None = None
    send_x: float | None = None
    send_y: float | None = None
    message_template: str = "{answer}"

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> "GorillaConfig":
        values = {}
        for f in fields(cls):
            key = f"gorilla.{f.name}"
            values[f.name] = get_float(conn, key) if f.name.endswith(("_x", "_y")) else get(conn, key)
        return cls(**values)


def format_message(template: str, answer: str) -> str:
    template = template or "{answer}"
    if "{answer}" not in template:
        template = template + " {answer}"
    return template.replace("{answer}", answer.strip()).strip()


HEARTBEAT_FRESH_SECONDS = 180  # 음성 인식·분석 한 번에 수십 초가 걸릴 수 있다


def runner_alive(conn: sqlite3.Connection, now: datetime | None = None) -> bool:
    hb = db.get_setting(conn, "quizbot.heartbeat")
    state = db.get_setting(conn, "quizbot.state")
    if not hb or state in ("멈춤", ""):
        return False
    try:
        age = (now or datetime.now()) - datetime.strptime(hb, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return age < timedelta(seconds=HEARTBEAT_FRESH_SECONDS)
