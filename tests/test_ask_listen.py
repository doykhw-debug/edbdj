"""프로그램을 켤 때 '뭐로 들으시겠어요?' — 지난번 채널(고릴라)로 저절로 시작하지 않는다 (MBC·KBS를 들을 수도 있음).
오류로 꺼졌다가 저절로 다시 켤 때(--restarted)는 묻지 않고 하던 대로 이어 간다."""

import pytest
from conftest import csrf

from radio_helper import __main__ as launcher
from radio_helper import app as app_module
from radio_helper import db
from radio_helper.quizbot import config


@pytest.fixture
def boot(data_dir, monkeypatch):
    started = []
    monkeypatch.setattr(launcher, "pick_port", lambda first: (5099, []))
    for name in ("self_check", "disable_console_quick_edit", "record_native_crashes", "watch_runner", "watch_updates",
                 "watch_tunnel_port", "wait_for_port"):
        monkeypatch.setattr(launcher, name, lambda *a, **k: None)
    monkeypatch.setattr(launcher.signal, "signal", lambda *a: None)
    monkeypatch.setattr(launcher, "run_simple", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "start_runner", lambda c: started.append(1) or "quizbot_x.log")

    def run(*args):
        monkeypatch.setattr("sys.argv", ["radio_helper", "--no-browser", *args])
        launcher.main()
        conn = db.connect()
        state = {k: config.get(conn, k) for k in ("live.active", "live.ask", "live.channel")}
        conn.close()
        return state, started
    conn = db.connect()
    db.init_db(conn)
    db.set_setting(conn, "live.active", "1")                 # 지난번에 파워FM(고릴라)을 듣다가 끔
    db.set_setting(conn, "live.channel", "파워FM")
    conn.close()
    return run


def test_normal_start_asks_instead_of_resuming(boot, capsys):
    state, started = boot()
    assert state == {"live.active": "0", "live.ask": "1", "live.channel": "파워FM"} and started == []
    assert "무엇으로 들을지 관리 화면 첫 화면에서 골라 주세요 (지난번: 파워FM)" in capsys.readouterr().out


def test_restart_after_crash_keeps_listening_without_asking(boot):
    state, started = boot("--restarted")
    assert state["live.active"] == "1" and state["live.ask"] == "0" and started == [1]   # 재시작 루틴 그대로


def test_restart_while_still_asking_keeps_waiting(boot):
    boot()                                                     # 켜서 묻는 중에
    state, started = boot("--restarted")                       # 오류로 다시 켜져도
    assert state["live.ask"] == "1" and state["live.active"] == "0" and started == []   # 계속 답을 기다림


def test_home_asks_and_picking_mbc_starts_listening(client, conn, monkeypatch):
    started, launched = [], []
    monkeypatch.setattr(app_module, "start_runner", lambda c: started.append(1) or "q.log")
    monkeypatch.setattr(app_module, "launch_quizbot", lambda args: launched.append(args) or "c.log")
    db.set_setting(conn, "live.ask", "1")
    db.set_setting(conn, "live.channel", "파워FM")
    db.set_setting(conn, "live.board_auto", "0")              # 지난번 선택 항목
    html = client.get("/").get_data(as_text=True)
    assert "뭐로 들으시겠어요?" in html and "지난번: <b>파워FM</b>" in html
    assert html.index("SBS 고릴라") < html.index("MBC mini") < html.index("KBS 콩")
    assert ">MBC FM4U</button>" in html and ">KBS 쿨FM</button>" in html

    token = csrf(client, "/")
    client.post("/listen/start", data={"csrf_token": token, "channel": "MBC FM4U", "keep_options": "1", "route": "app"})
    assert (config.get(conn, "live.active"), config.get(conn, "live.ask"), config.get(conn, "live.channel")) == \
        ("1", "0", "MBC FM4U")
    assert config.chat_app(conn, "MBC FM4U") == "mini" and started == [1]          # 보낼 곳·다시 띄울 앱은 MBC mini
    assert config.get(conn, "live.board_auto") == "0"                                # 선택 항목은 그대로
    assert "뭐로 들으시겠어요?" not in client.get("/").get_data(as_text=True)


def test_skip_listening(client, conn):
    db.set_setting(conn, "live.ask", "1")
    token = csrf(client, "/")
    client.post("/listen/skip", data={"csrf_token": token})
    assert (config.get(conn, "live.ask"), config.get(conn, "live.active")) == ("0", "0")
    assert "뭐로 들으시겠어요?" not in client.get("/").get_data(as_text=True)


def test_restart_after_update_continues_listening(boot, capsys):
    conn = db.connect()
    db.set_setting(conn, "update.started_at", db.now())        # 업데이트를 설치하고 다시 켬 (start_windows.bat 코드 3)
    conn.close()
    state, started = boot("--restarted")
    out = capsys.readouterr().out
    assert "[업데이트] 새 버전" in out and "예기치 않게" not in out
    assert state["live.active"] == "1" and state["live.ask"] == "0" and started == [1]   # 묻지 않고 이어서 들음
    conn = db.connect()
    assert db.get_setting(conn, "update.started_at") == ""
    conn.close()
