"""0.73: 게시판 읽기·쓰기 정지 (채팅만) · 설정 화면 GPT 모델 목록."""

from datetime import datetime

from conftest import csrf

from radio_helper import boards, collect, db
from radio_helper.quizbot import answerer

URL = "https://programs.sbs.co.kr/radio/aaa/boards/111"


def test_pause_stops_every_board_job(conn, monkeypatch, data_dir):
    monkeypatch.setattr(boards, "board_busy", lambda: False)
    monkeypatch.setattr(collect, "board_busy", lambda: False)
    cid = boards.add_corner(conn, URL)
    conn.execute("UPDATE corners SET auto_on = 1, auto_time = '09:00' WHERE id = ?", (cid,))
    conn.commit()
    killed = []
    (data_dir / boards.BOARD_LOCK).write_text("424242", encoding="ascii")          # 돌고 있는 게시판 작업
    monkeypatch.setattr("radio_helper.autofill.pid_alive", lambda pid: pid == 424242)
    text = boards.set_paused(conn, True, kill=killed.append)
    assert boards.paused(conn) and killed == [424242] and "끝냈습니다" in text
    assert not (data_dir / boards.BOARD_LOCK).exists()
    now = datetime(2026, 10, 22, 11, 0)
    launched = []
    assert boards.tick(conn, now, launch=launched.append) == [] and launched == []
    assert boards.onair_tick(conn, now, launch=launched.append) == [] and "정지" in db.get_setting(conn, boards.ONAIR_STATUS)
    assert not collect.tick(conn, now, launch=launched.append)
    assert "정지" in boards.run(conn, cid, test=True)
    said = []
    boards.check_logins(conn, say=said.append)
    assert "정지" in said[0]
    ran = []
    boards.run_onair(conn, "SBS 파워FM", "가", runner=lambda *a, **k: ran.append(1) or "ok", say=lambda m: None)
    assert ran == []
    assert "다시 켰" in boards.set_paused(conn, False) and not boards.paused(conn)
    assert boards.tick(conn, now, launch=launched.append) == [cid]


def test_pause_button_banner_and_blocked_routes(client, conn):
    cid = boards.add_corner(conn, URL)
    page = client.get("/").get_data(as_text=True)
    assert "게시판 작업 정지 (채팅만)" in page
    client.post("/boards/pause", data={"csrf_token": csrf(client), "on": "1", "next": "/corners"})
    assert boards.paused(conn)
    page = client.get("/corners").get_data(as_text=True)
    assert "게시판 읽기·쓰기 정지 중" in page and "게시판 작업 다시 켜기" in page
    r = client.post(f"/corners/{cid}/auto-run", data={"csrf_token": csrf(client), "mode": "test"})
    assert r.status_code == 302
    assert "게시판을 열지 않습니다" in client.get("/corners").get_data(as_text=True)
    client.post("/boards/pause", data={"csrf_token": csrf(client), "on": "0", "next": "//evil.example"})
    assert not boards.paused(conn)


def test_model_lists_and_custom_name(client, conn, monkeypatch):
    page = client.get("/quizbot").get_data(as_text=True)
    assert 'value="gpt-4.1-mini" selected' in page and 'value="gpt-6-luna" selected' in page
    assert "gpt-6-astra — 최고 성능" in page and "직접 입력" in page
    client.post("/quizbot/settings", data={"csrf_token": csrf(client), "billing": "chatgpt", "gpt_fallback": "0",
                                           "gpt_model": "__custom__", "gpt_model_custom": "gpt-4.1-mini-2025-04-14",
                                           "chatgpt_model": "gpt-5.6-terra", "chatgpt_model_custom": ""})
    assert db.get_setting(conn, "quizbot.gpt_model") == "gpt-4.1-mini-2025-04-14"
    assert db.get_setting(conn, "quizbot.chatgpt_model") == "gpt-5.6-terra"
    page = client.get("/quizbot").get_data(as_text=True)
    assert 'value="gpt-4.1-mini-2025-04-14"' in page                              # 목록에 없는 값은 직접 입력 칸에
    assert answerer.REASONING_MODEL.match("gpt-6-luna") and not answerer.REASONING_MODEL.match("gpt-4.1-mini")
