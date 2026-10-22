"""처음부터 넣어 두는 편성표·코너 게시판 (0.62)."""

import pytest

from radio_helper import broadcast_seed, db, schedules
from radio_helper.quizbot import live

DATA = {
    "schedules": {
        "MBC FM4U": {"as_of": "공식 편성표 2026-10-22", "programs": [
            {"title": "아침 프로", "host": "가", "start": "07:00", "end": "09:00", "days": "평일"},
            {"title": "밤 프로", "host": "", "start": "23:00", "end": "01:00", "days": "매일"},
            {"title": "시간 없음", "start": "", "end": "", "days": "매일"}]},
        "KBS 쿨FM": {"programs": [{"title": "새 이름", "start": "07:00", "end": "09:00", "days": "매일"}]},
    },
    "boards": [
        {"channel": "MBC FM4U", "program": "아침 프로", "title": "사연과 신청곡",
         "url": "https://www.imbc.com/broad/radio/fm4u/morning/board.html"},
        {"channel": "파워FM", "program": "김영철의 파워FM", "title": "코너",
         "url": "https://programs.sbs.co.kr/radio/0chulpowerfm/cornerboards/57577?cornerid=9999"},
        {"channel": "?", "program": "?", "title": "남의 사이트", "url": "https://example.com/board"},
    ],
}


def test_fills_only_empty_channels_once(conn):
    schedules.save(conn, "KBS 쿨FM", [{"title": "웹 검색으로 찾은 것", "host": "", "start": "07:00", "end": "09:00",
                                       "days": "매일", "homepage": ""}])
    assert broadcast_seed.seed_schedules(conn, DATA) == ["MBC FM4U"]
    titles = [r["title"] for r in conn.execute("SELECT title FROM programs WHERE channel = 'KBS 쿨FM' AND on_air = 1")]
    assert titles == ["웹 검색으로 찾은 것"]                                     # 있는 편성은 그대로
    from datetime import datetime
    assert live.current_program(conn, "MBC FM4U", datetime(2026, 10, 22, 8, 0)) == "아침 프로"   # 목요일
    assert live.current_program(conn, "MBC FM4U", datetime(2026, 10, 24, 8, 0)) is None         # 토요일
    assert live.current_program(conn, "MBC FM4U", datetime(2026, 10, 23, 0, 30)) == "밤 프로"
    assert conn.execute("SELECT COUNT(*) FROM programs WHERE channel = 'MBC FM4U'").fetchone()[0] == 2   # 시간 없는 것 뺌
    src = conn.execute("SELECT source FROM programs WHERE channel = 'MBC FM4U' LIMIT 1").fetchone()[0]
    assert src.startswith(schedules.SOURCE) and "기본값 공식 편성표 2026-10-22" in src   # 웹 새로고침이 바꿀 수 있게
    conn.execute("UPDATE programs SET on_air = 0 WHERE channel = 'MBC FM4U'")
    assert broadcast_seed.seed_schedules(conn, DATA) == []                          # 판마다 한 번


def test_web_refresh_retires_seeded_programs(conn):
    broadcast_seed.seed_schedules(conn, DATA)
    schedules.save(conn, "MBC FM4U", [{"title": "개편 뒤 프로", "host": "", "start": "07:00", "end": "09:00",
                                       "days": "평일", "homepage": ""}])
    on = [r["title"] for r in conn.execute("SELECT title FROM programs WHERE channel = 'MBC FM4U' AND on_air = 1")]
    assert on == ["개편 뒤 프로"]


def test_boards_added_once_and_only_allowed_hosts(conn):
    assert broadcast_seed.seed_boards(conn, DATA) == 2
    rows = {r["board_url"]: r for r in conn.execute("SELECT * FROM corners")}
    mbc = rows["https://www.imbc.com/broad/radio/fm4u/morning/board.html"]
    assert mbc["station"] == "MBC" and mbc["program"] == "아침 프로" and mbc["board_exists"] == 1
    assert "기본 코너 목록" in mbc["dev_note"]
    sbs = rows["https://programs.sbs.co.kr/radio/0chulpowerfm/cornerboards/57577?cornerid=9999"]
    assert sbs["write_url"].endswith("/cornerboardwrite/57577/?cornerid=9999")
    assert "https://example.com/board" not in rows
    conn.execute("DELETE FROM corners WHERE station = 'MBC'")
    assert broadcast_seed.seed_boards(conn, DATA) == 0                              # 지운 코너는 다시 안 생김


@pytest.mark.seed_data
def test_shipped_data_is_well_formed(conn):
    data = broadcast_seed.load()
    assert data["schedules"] and data["boards"]
    for channel, entry in data["schedules"].items():
        cleaned = schedules.clean(entry["programs"])
        assert len(cleaned) >= schedules.MIN_PROGRAMS, channel
        assert entry.get("sources"), channel
    from radio_helper import stations
    assert all(stations.station_of(b["url"]) for b in data["boards"])
    assert len({b["url"] for b in data["boards"]}) == len(data["boards"])           # 같은 주소 두 번 없음
    filled = broadcast_seed.seed_schedules(conn)
    assert set(filled) == set(data["schedules"]) - {"파워FM", "러브FM"}            # 파워FM 은 처음 넣어 둔 편성이 있음
    assert broadcast_seed.seed_boards(conn) > 0


@pytest.mark.seed_data
def test_app_start_seeds(client, conn):
    assert db.get_setting(conn, broadcast_seed.BOARD_KEY) == broadcast_seed.VERSION
    assert conn.execute("SELECT COUNT(*) FROM corners WHERE dev_note LIKE '기본 코너 목록%'").fetchone()[0] > 0
