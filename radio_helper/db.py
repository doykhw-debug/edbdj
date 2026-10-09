"""로컬 SQLite 저장소.

비밀번호·쿠키·세션 토큰은 어떤 테이블에도 저장하지 않는다.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
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

-- 퀴즈 자동 참여 예약. days 는 월=0 … 일=6 숫자 문자열 (예: "01234" = 평일).
CREATE TABLE IF NOT EXISTS quiz_schedules (
    id                INTEGER PRIMARY KEY,
    program_id        INTEGER NOT NULL REFERENCES programs(id),
    days              TEXT NOT NULL DEFAULT '0123456',
    start_time        TEXT NOT NULL,
    end_time          TEXT NOT NULL,
    auto_submit       INTEGER NOT NULL DEFAULT 0,
    min_confidence    REAL NOT NULL DEFAULT 0.8,
    gorilla_confirmed INTEGER NOT NULL DEFAULT 0,  -- 이 프로그램 퀴즈는 고릴라로 받는다고 사용자가 확인함
    enabled           INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- 진행자가 주제를 주고 사연·메시지를 받을 때, 내 실제 경험으로 만든 공감로그 글 (확인 후 전송)
CREATE TABLE IF NOT EXISTS story_posts (
    id               INTEGER PRIMARY KEY,
    schedule_id      INTEGER,
    program          TEXT NOT NULL,
    broadcast_date   TEXT NOT NULL,
    topic            TEXT NOT NULL,
    experience_id    INTEGER REFERENCES experiences(id),
    message          TEXT NOT NULL DEFAULT '',
    source           TEXT NOT NULL DEFAULT 'ai',        -- ai: AI 초안 / user_line: 직접 쓴 한 줄
    status           TEXT NOT NULL DEFAULT 'pending',   -- pending / entered / posted / unknown / failed / skipped
    approved         INTEGER NOT NULL DEFAULT 0,
    gorilla_accepted TEXT NOT NULL DEFAULT 'unknown',
    deadline         TEXT,
    decision         TEXT,
    warnings         TEXT,                              -- 검사 결과 JSON
    excerpt          TEXT,
    repeat_count     INTEGER NOT NULL DEFAULT 1,
    sent_at          TEXT,
    note             TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- 방송에서 들은 선물(경품) 정보와 받는 조건. 채널·프로그램별로 모아 본다.
CREATE TABLE IF NOT EXISTS gift_events (
    id             INTEGER PRIMARY KEY,
    channel        TEXT NOT NULL,
    program        TEXT NOT NULL,
    broadcast_date TEXT NOT NULL,
    heard_at       TEXT NOT NULL,
    gift           TEXT NOT NULL,
    condition      TEXT,      -- 받는 조건 (예: 퀴즈 정답자 중 추첨, 사연 채택)
    entry_method   TEXT,      -- 참여 방법 (고릴라 공감로그 / 문자 #1077 / 홈페이지 등)
    related        TEXT,      -- quiz / story / event / other
    deadline       TEXT,
    winners        TEXT,      -- 당첨 인원
    announce       TEXT,      -- 발표·연락 방법
    excerpt        TEXT,
    repeat_count   INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- 방송 음성 인식 결과 (오디오 자체는 저장하지 않는다)
CREATE TABLE IF NOT EXISTS transcripts (
    id             INTEGER PRIMARY KEY,
    schedule_id    INTEGER,
    broadcast_date TEXT NOT NULL,
    at             TEXT NOT NULL,
    text           TEXT NOT NULL
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

-- 인물 관계도: 나(화자)와 주변 인물. 이 PC 에만 저장된다 (실명은 사연·AI 요청에 쓰지 않고 '호칭'만 쓴다).
CREATE TABLE IF NOT EXISTS people (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,              -- 관계도에 보이는 이름
    alias      TEXT,                              -- 사연에 쓰는 호칭 (예: 와이프, 대학 친구, 회사 선배)
    grp        TEXT,                              -- 묶음 (예: 우리 집 · 대전, 직장)
    side       TEXT NOT NULL DEFAULT 'family',    -- self / family / work / friend / life
    relation   TEXT,                              -- 한 줄 소개
    age        TEXT,
    details    TEXT NOT NULL DEFAULT '[]',        -- [{"key", "value", "tag"}]
    events     TEXT NOT NULL DEFAULT '[]',        -- 실제 사건 [{"when", "text", "tag"}] (가상 사건은 넣지 않음)
    closeness  TEXT NOT NULL DEFAULT '',          -- 가까움 / 보통 / 서먹
    note       TEXT,
    sort       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 사연 보관함: 가져온 사연 글. kind = fiction(각색·가상) 은 읽기용이며 보내지 않는다.
CREATE TABLE IF NOT EXISTS story_library (
    id           INTEGER PRIMARY KEY,
    code         TEXT UNIQUE,
    person_id    INTEGER REFERENCES people(id),
    person_label TEXT,
    title        TEXT NOT NULL DEFAULT '',
    intro        TEXT,
    event_date   TEXT,
    summary      TEXT,
    timing       TEXT,
    song         TEXT,
    closing      TEXT,
    body         TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT 'fiction',  -- fiction / real
    origin       TEXT,
    category     TEXT,
    theme        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
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


DB_NAME = "radio_helper.sqlite3"
PROGRAM_ROOT = Path(__file__).resolve().parent.parent
_adopt_checked = False
ADOPTED_FROM: Path | None = None   # 이번 실행에서 예전 폴더 데이터를 옮겨 왔으면 그 위치


def home_data_dir() -> Path:
    """데이터를 두는 고정 위치. 프로그램 폴더 밖이라 업데이트 파일을 어디에 풀어도 그대로 남는다.

    윈도우: C:\\Users\\<이름>\\AppData\\Local\\RadioHelper"""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "RadioHelper"


def data_dir() -> Path:
    env = os.environ.get("RADIO_HELPER_DATA_DIR")
    d = Path(env) if env else home_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    if not env:
        _adopt_old_data(d)
    return d


def _user_rows(path: Path) -> int:
    """사용자가 넣은 데이터 수 (경험·인물·사연·퀴즈 기록). 처음 만든 빈 데이터는 0."""
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
        try:
            n = 0
            for table in ("experiences", "people", "story_library", "quizzes", "story_posts", "submissions"):
                try:
                    n += c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except sqlite3.Error:
                    pass
            return n
        finally:
            c.close()
    except sqlite3.Error:
        return 0


def old_data_candidates(roots: list[Path]) -> list[Path]:
    """예전 위치(프로그램 폴더의 data)와, 같은 곳에 풀어 둔 다른 업데이트 폴더들의 data."""
    found: list[Path] = []
    for root in roots:
        for pattern in (f"data/{DB_NAME}", f"*/data/{DB_NAME}", f"*/*/data/{DB_NAME}"):
            try:
                found += [p for p in root.glob(pattern) if p.is_file()]
            except OSError:
                pass
    unique = []
    for p in found:
        if p.resolve() not in {u.resolve() for u in unique}:
            unique.append(p)
    return unique


def best_old_db(roots: list[Path]) -> Path | None:
    """데이터가 들어 있는 것 중 가장 최근에 쓴 것."""
    scored = [(p.stat().st_mtime, p) for p in old_data_candidates(roots) if _user_rows(p) > 0]
    return max(scored)[1] if scored else None


def adopt_old_data(target_dir: Path, roots: list[Path]) -> Path | None:
    """고정 위치에 데이터가 아직 없으면 예전 폴더의 데이터를 복사해 온다. 복사한 원본 위치를 돌려준다."""
    target = target_dir / DB_NAME
    if target.exists():
        return None
    src = best_old_db(roots)
    if src is None:
        return None
    s, t = sqlite3.connect(str(src), timeout=10), sqlite3.connect(str(target))
    try:
        s.backup(t)   # 쓰는 중이어도 깨지지 않게 SQLite 백업으로 복사
    finally:
        s.close()
        t.close()
    for sub in ("inspect",):
        old = src.parent / sub
        if old.is_dir():
            shutil.copytree(old, target_dir / sub, dirs_exist_ok=True)
    (target_dir / "옮겨온_데이터.txt").write_text(f"{now()} 에 이 위치로 옮겨 온 데이터의 원래 위치:\n{src}\n",
                                                  encoding="utf-8")
    return src


def _adopt_old_data(d: Path) -> None:
    global _adopt_checked, ADOPTED_FROM
    if _adopt_checked:
        return
    _adopt_checked = True
    home = Path.home()
    # 이 프로그램 폴더 → 같은 곳에 풀어 둔 다른 업데이트 폴더 → 내려받기·바탕 화면·문서
    roots = [PROGRAM_ROOT, PROGRAM_ROOT.parent] + [
        home / name for name in ("Downloads", "Desktop", "Documents", "다운로드", "바탕 화면")
        if (home / name).is_dir() and (home / name) != PROGRAM_ROOT.parent]
    try:
        ADOPTED_FROM = adopt_old_data(d, roots)
    except (OSError, sqlite3.Error):
        ADOPTED_FROM = None


def db_path() -> Path:
    return data_dir() / DB_NAME


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or db_path()), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# 기존 데이터베이스에 나중에 추가된 칼럼
_ADDED_COLUMNS = {
    "experiences": [
        ("gorilla_line", "TEXT"),                       # 공감로그용 한 줄 (사용자가 직접 씀, 200자 이내)
        ("about_person_id", "INTEGER"),                 # 누구 이야기인가 (화자는 늘 나, 비면 내 이야기)
        ("from_library_id", "INTEGER"),                 # 사연 보관함에서 옮긴 경험 (사용자가 고치고 확인해야 씀)
        ("from_event", "TEXT"),                         # 인물 관계도의 실제 사건에서 옮긴 경험 "인물id|시기|사건"
        ("enriched_at", "TEXT"),                        # 관계도 사건을 사연처럼 풀어 쓴 때 (enrich.py)
        ("enrich_note", "TEXT"),                        # 풀어 쓰지 않고 원래 한 줄을 둔 이유
    ],
    "quiz_schedules": [
        ("story_enabled", "INTEGER NOT NULL DEFAULT 1"),         # 사연·주제 모집도 듣기
        ("story_auto_user_line", "INTEGER NOT NULL DEFAULT 0"),  # 직접 쓴 한 줄은 확인 없이 전송
        ("gift_enabled", "INTEGER NOT NULL DEFAULT 1"),          # 선물 정보도 기록
        ("story_auto_ai", "INTEGER NOT NULL DEFAULT 0"),         # AI 초안도 검사 통과 시 자동 전송
    ],
    "transcripts": [
        ("channel", "TEXT"),
        ("program", "TEXT"),
    ],
    "quizzes": [
        ("source", "TEXT NOT NULL DEFAULT 'manual'"),   # manual / auto
        ("schedule_id", "INTEGER"),
        ("confidence", "REAL"),
        ("excerpt", "TEXT"),                            # 판단에 쓴 방송 녹취 일부
        ("send_text", "TEXT"),
        ("sent_at", "TEXT"),
        ("approved", "INTEGER NOT NULL DEFAULT 0"),     # 사용자가 화면에서 보내기를 승인함
        ("decision", "TEXT"),                           # 자동 전송/보류 판단 이유
        ("route", "TEXT"),                              # 사용자가 고른 보내는 방법 (비면 기본 설정)
        ("sent_via", "TEXT"),                           # 실제로 보낸 방법: gorilla / mini / kong / sms
        ("answer_kind", "TEXT NOT NULL DEFAULT 'correct'"),  # 보낼 답: correct 정답 / witty 기발한 오답
        ("witty_answer", "TEXT"),
        ("witty_point", "TEXT"),                        # 왜 웃기거나 기발한지
        ("wit_score", "REAL"),
        ("fun_welcome", "INTEGER NOT NULL DEFAULT 0"),  # 진행자가 재밌는 오답도 환영한다고 함
        ("chat_shots", "INTEGER NOT NULL DEFAULT 0"),   # 분석에 함께 쓴 채팅창 사진 수
    ],
    "story_posts": [
        ("channel", "TEXT"),
        ("route", "TEXT"),
        ("sent_via", "TEXT"),
        ("target", "TEXT NOT NULL DEFAULT 'chat'"),    # chat 채팅창 / board 게시판 (진행자가 '게시판에만'이라고 할 때)
        ("board_title", "TEXT"),
        ("draft_id", "INTEGER"),                        # 게시판용이면 원고 검토함의 원고
        ("song", "TEXT"),
        ("chat_shots", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, cols in _ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    for kind, title, board_url, write_url, dev_note, is_target in seed.CORNERS:
        conn.execute(
            """INSERT OR IGNORE INTO corners
               (program, kind, title, board_url, write_url, dev_note, is_target, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (seed.PROGRAM, kind, title, board_url, write_url, dev_note, is_target, now()),
        )
    for channel, programs in ((seed.CHANNEL_POWERFM, seed.POWERFM_PROGRAMS),
                              (seed.CHANNEL_LOVEFM, seed.LOVEFM_PROGRAMS)):
        for code, title, host, start, end, days in programs:
            conn.execute(
                """INSERT OR IGNORE INTO programs
                   (channel, code, title, host, start_time, end_time, days, main_url, on_air, source, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (channel, code, title, host or None, start, end, days, seed.program_main_url(code),
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
