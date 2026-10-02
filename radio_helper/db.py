"""로컬 SQLite 저장소.

비밀번호·쿠키·세션 토큰은 어떤 테이블에도 저장하지 않는다.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from . import seed

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corners (
    id                INTEGER PRIMARY KEY,
    program           TEXT NOT NULL,
    kind              TEXT NOT NULL,
    title             TEXT NOT NULL,
    board_url         TEXT NOT NULL UNIQUE,
    write_url         TEXT,
    dev_note          TEXT,
    board_exists      INTEGER NOT NULL DEFAULT 1,
    recruiting        TEXT NOT NULL DEFAULT 'unknown',   -- unknown / open / closed
    notice_text       TEXT,
    notice_hash       TEXT,
    notice_checked_at TEXT,
    notice_changed    INTEGER NOT NULL DEFAULT 0,
    char_limit        INTEGER,
    deadline          TEXT,
    required_fields   TEXT,
    ai_assist_policy  TEXT NOT NULL DEFAULT 'unknown',   -- unknown / allowed / restricted
    is_target         INTEGER NOT NULL DEFAULT 0,
    title_selector    TEXT,
    body_selector     TEXT,
    song_selector     TEXT,
    updated_at        TEXT
);

CREATE TABLE IF NOT EXISTS experiences (
    id             INTEGER PRIMARY KEY,
    label          TEXT,
    when_text      TEXT,
    people         TEXT,
    story          TEXT NOT NULL,
    quotes         TEXT,
    quote_kind     TEXT NOT NULL DEFAULT 'none',   -- none / exact / gist
    highlight      TEXT,
    ending         TEXT,
    fixed_facts    TEXT,
    hide           TEXT,
    song           TEXT,
    prior_history  TEXT,
    user_confirmed INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id             INTEGER PRIMARY KEY,
    experience_id  INTEGER NOT NULL REFERENCES experiences(id),
    corner_id      INTEGER NOT NULL REFERENCES corners(id),
    title          TEXT NOT NULL DEFAULT '',
    body           TEXT NOT NULL DEFAULT '',
    song           TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT 'template',  -- template / pasted / manual
    fact_confirmed INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'draft',     -- draft / approved / archived
    approved_at    TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- 글 등록 성공 ≠ 사연 채택 ≠ 당첨 ≠ 상품 수령: 각 상태를 별도 칼럼으로 둔다.
CREATE TABLE IF NOT EXISTS submissions (
    id             INTEGER PRIMARY KEY,
    draft_id       INTEGER REFERENCES drafts(id),
    experience_id  INTEGER NOT NULL REFERENCES experiences(id),
    corner_id      INTEGER NOT NULL REFERENCES corners(id),
    title          TEXT,
    body           TEXT,
    post_status    TEXT NOT NULL DEFAULT 'filled',    -- filled / posted / unknown / failed
    post_url       TEXT,
    adopted        TEXT NOT NULL DEFAULT 'unknown',   -- unknown / yes / no
    won            TEXT NOT NULL DEFAULT 'unknown',
    prize_received TEXT NOT NULL DEFAULT 'unknown',
    note           TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quizzes (
    id               INTEGER PRIMARY KEY,
    dedupe_key       TEXT NOT NULL UNIQUE,
    account          TEXT NOT NULL,
    channel          TEXT NOT NULL,
    program          TEXT NOT NULL,
    broadcast_date   TEXT NOT NULL,
    question_key     TEXT NOT NULL,
    kind             TEXT NOT NULL DEFAULT 'new',     -- new / rerun / answer_reveal
    question         TEXT,
    options          TEXT,
    deadline         TEXT,
    entry_channel    TEXT,
    gorilla_accepted TEXT NOT NULL DEFAULT 'unknown', -- unknown / yes / no
    answer           TEXT,
    answer_verified  INTEGER NOT NULL DEFAULT 0,
    entry_status     TEXT NOT NULL DEFAULT 'pending', -- pending / entered / posted / unknown / failed
    answer_accepted  TEXT NOT NULL DEFAULT 'unknown',
    won              TEXT NOT NULL DEFAULT 'unknown',
    prize_received   TEXT NOT NULL DEFAULT 'unknown',
    repeat_count     INTEGER NOT NULL DEFAULT 1,
    note             TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- 채널 편성 프로그램. on_air=0 은 공식 페이지에서 발견했지만 채널을 확인하지 못한 후보.
CREATE TABLE IF NOT EXISTS programs (
    id         INTEGER PRIMARY KEY,
    channel    TEXT NOT NULL,
    code       TEXT NOT NULL UNIQUE,
    title      TEXT NOT NULL,
    host       TEXT,
    start_time TEXT,
    end_time   TEXT,
    days       TEXT,
    main_url   TEXT NOT NULL,
    on_air     INTEGER NOT NULL DEFAULT 1,
    source     TEXT,
    checked_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    message TEXT NOT NULL
);

-- 로컬 모의 글쓰기 화면에 "등록"된 글. 실제 사이트와 무관하다.
CREATE TABLE IF NOT EXISTS mock_posts (
    id         INTEGER PRIMARY KEY,
    cornerid   TEXT,
    title      TEXT,
    content    TEXT,
    song       TEXT,
    created_at TEXT NOT NULL
);
"""

PROFILE_KEYS = [
    "nickname",          # 방송에 사용할 이름 또는 별명
    "region",            # 공개 가능한 거주 지역 범위
    "job_label",         # 공개 가능한 직업 표현
    "family_aliases",    # 가족을 부를 익명 호칭
    "avoid_topics",      # 피하고 싶은 소재와 공개하지 않을 정보
    "banned_words",      # 본문에 나오면 안 되는 단어 (실명·회사명 등, 쉼표 구분)
    "tone",              # 유쾌함 / 담백함 / 따뜻함 / 기타
    "tone_other",
    "song_pref",
    "phone_ok",          # 전화 연결 참여 가능 여부
    "login_method",      # SBS 아이디 / 소셜 로그인 등 (비밀번호는 받지 않음)
    "test_pc_login_ok",
    "account_label",     # 퀴즈 중복 키에 쓰는 계정 구분값 (아이디 아님, 별칭이면 충분)
]


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def data_dir() -> Path:
    d = Path(os.environ.get("RADIO_HELPER_DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "radio_helper.sqlite3"


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or db_path()), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for kind, title, board_url, write_url, dev_note, is_target in seed.CORNERS:
        conn.execute(
            """INSERT OR IGNORE INTO corners
               (program, kind, title, board_url, write_url, dev_note, is_target, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (seed.PROGRAM, kind, title, board_url, write_url, dev_note, is_target, now()),
        )
    for code, title, host, start, end, days in seed.POWERFM_PROGRAMS:
        conn.execute(
            """INSERT OR IGNORE INTO programs
               (channel, code, title, host, start_time, end_time, days, main_url, on_air, source, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (seed.CHANNEL_POWERFM, code, title, host or None, start, end, days, seed.program_main_url(code),
             seed.POWERFM_SEED_SOURCE, now()),
        )
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('global_stop', '0')")
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def is_stopped(conn: sqlite3.Connection) -> bool:
    return get_setting(conn, "global_stop", "0") == "1"


def get_profile(conn: sqlite3.Connection) -> dict[str, str]:
    return {k: get_setting(conn, f"profile.{k}") for k in PROFILE_KEYS}


def save_profile(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    for k in PROFILE_KEYS:
        if k in values:
            set_setting(conn, f"profile.{k}", values[k].strip())


def log(conn: sqlite3.Connection, kind: str, message: str) -> None:
    conn.execute("INSERT INTO events (at, kind, message) VALUES (?, ?, ?)", (now(), kind, message))
    conn.commit()


def notice_hash(text: str | None) -> str | None:
    if not text or not text.strip():
        return None
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def update_corner_notice(conn: sqlite3.Connection, corner_id: int, text: str) -> bool:
    """공지 원문을 저장한다. 이전에 확인한 공지와 내용이 다르면 재검토 필요로 표시하고 True 를 돌려준다."""
    row = conn.execute("SELECT notice_hash FROM corners WHERE id = ?", (corner_id,)).fetchone()
    new_hash = notice_hash(text)
    changed = bool(row and row["notice_hash"] and new_hash and row["notice_hash"] != new_hash)
    if new_hash == (row["notice_hash"] if row else None):
        return False
    conn.execute(
        """UPDATE corners SET notice_text = ?, notice_hash = ?, updated_at = ?,
           notice_changed = CASE WHEN ? THEN 1 ELSE notice_changed END,
           notice_checked_at = CASE WHEN ? THEN NULL ELSE notice_checked_at END
           WHERE id = ?""",
        (text.strip() or None, new_hash, now(), int(changed), int(changed), corner_id),
    )
    conn.commit()
    return changed
