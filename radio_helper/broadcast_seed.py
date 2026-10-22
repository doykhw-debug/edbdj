"""처음부터 넣어 두는 방송사 편성표·코너 게시판 목록 (0.62, broadcast_seed.json).

- 편성표: 2026-10-22 에 각 방송사 공식 편성표(일부는 백과사전 편성표)에서 확인한 프로그램만 넣는다. 확인하지 못한
  시간대는 비워 둔다 (지어내지 않음). 그 채널에 편성이 하나도 없을 때만 채우고, 출처를 '웹 검색 편성표 … (기본값 …)'로
  적어 두어 일주일마다 하는 웹 검색 새로고침이 개편을 반영하면 그대로 바뀌거나 '편성 아님'으로 돌아간다.
- 코너 게시판: 공식 홈페이지 메뉴에서 실제로 본 청취자 사연·신청곡·코너 게시판 주소만 넣는다. '매일 게시판 확인'이
  하나씩 공지를 읽고 받지 않는 곳은 건너뛴다. 한 번만 넣으므로 지운 코너는 다시 생기지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path

DATA = Path(__file__).with_name("broadcast_seed.json")
VERSION = "062"
SCHEDULE_KEY = "meta.seed_schedules"
BOARD_KEY = "meta.seed_boards"


def load(path: Path = DATA) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def seed_schedules(conn, data: dict | None = None) -> list[str]:
    """편성이 비어 있는 채널만 기본 편성표로 채운다 (판마다 한 번). 채운 채널 목록."""
    from . import db, schedules

    if db.get_setting(conn, SCHEDULE_KEY) == VERSION:
        return []
    data = load() if data is None else data
    done = []
    for channel, entry in (data.get("schedules") or {}).items():
        if conn.execute("SELECT 1 FROM programs WHERE channel = ? AND on_air = 1", (channel,)).fetchone():
            continue                                    # 이미 편성이 있음 (웹 검색·SBS 공식 페이지·사용자 수정)
        programs = schedules.clean(entry.get("programs") or [])
        if programs:
            schedules.save(conn, channel, programs, f"기본값 {entry.get('as_of') or ''}".strip())
            done.append(channel)
    db.set_setting(conn, SCHEDULE_KEY, VERSION)
    return done


def seed_boards(conn, data: dict | None = None) -> int:
    """기본 코너 게시판을 넣는다 (판마다 한 번, 이미 있는 주소는 그대로). 새로 넣은 수."""
    from . import db, stations

    if db.get_setting(conn, BOARD_KEY) == VERSION:
        return 0
    data = load() if data is None else data
    added = 0
    for b in data.get("boards") or []:
        url = (b.get("url") or "").strip()
        station = stations.station_of(url)
        if station is None or not b.get("title"):
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO corners (program, kind, title, board_url, write_url, dev_note, station, updated_at) "
            "VALUES (?, 'board', ?, ?, ?, ?, ?, ?)",
            (b.get("program") or station, b["title"][:80], url, db.corner_write_url(url) or url,
             f"기본 코너 목록 ({b.get('channel') or station}, 2026-10-22 공식 홈페이지에서 수집)", station, db.now()))
        added += cur.rowcount
    db.set_setting(conn, BOARD_KEY, VERSION)
    conn.commit()
    return added
