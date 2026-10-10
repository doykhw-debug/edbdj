"""실행 창: 번호(포트)가 겹치면 다음 번호, 서버가 뜬 뒤 브라우저 열기, 자체 점검."""

import socket
import threading

from werkzeug.serving import make_server

from radio_helper import __main__ as launcher


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def serve(app):
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.get_json()["app"] == "radio_helper"


def test_port_states(app):
    assert launcher.port_state(free_port()) == "free"
    srv = serve(app)
    try:
        assert launcher.port_state(srv.server_port) == "ours"
    finally:
        srv.shutdown()
    # /health 가 없던 예전 판 도우미도 알아본다
    from flask import Flask

    old = Flask("old")
    old.get("/")(lambda: "<title>라디오 참여 도우미</title>")
    other_web = Flask("other")
    other_web.get("/")(lambda: "다른 웹 프로그램")
    for web, expected in ((old, "ours"), (other_web, "busy")):
        srv = serve(web)
        try:
            assert launcher.port_state(srv.server_port) == expected
        finally:
            srv.shutdown()
    # 다른 프로그램(응답 없는 연결)이 쓰는 번호
    other = socket.socket()
    other.bind(("127.0.0.1", 0))
    other.listen(1)
    try:
        assert launcher.port_state(other.getsockname()[1]) == "busy"
    finally:
        other.close()


def test_pick_port_skips_busy_ports(app, monkeypatch):
    states = {5000: "busy", 5001: "ours", 5002: "free"}
    monkeypatch.setattr(launcher, "port_state", lambda p: states.get(p, "busy"))
    port, notes = launcher.pick_port(5000)
    assert port == 5002 and "다른 프로그램" in notes[0] and "예전 검은 창" in notes[1]
    assert launcher.pick_port(6000, tries=3) == (None, [f"{p}번: 다른 프로그램이 쓰는 중이거나 응답 없음"
                                                        for p in (6000, 6001, 6002)])


def test_self_check_opens_browser_after_server_is_up(app, monkeypatch):
    opened, said = [], []
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    srv = serve(app)
    try:
        url = f"http://127.0.0.1:{srv.server_port}/"
        assert launcher.self_check(url, True, say=said.append)
    finally:
        srv.shutdown()
    assert opened == [url] and "정상 응답" in said[0]


def test_self_check_reports_when_server_never_starts(monkeypatch):
    opened, said = [], []
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    assert not launcher.self_check(f"http://127.0.0.1:{free_port()}/", True, wait=0.5, say=said.append)
    assert opened == [] and "응답하지 않습니다" in said[0]


# ── 다시 켜도 열려 있던 화면이 그대로 동작 · 중간에 꺼지면 자동으로 다시 켬 ─────────────────
def test_page_opened_before_restart_still_works(data_dir):
    """관리 화면을 다시 켜도(업데이트·재시작) 세션 키가 같아서, 전에 열어 둔 화면의 버튼이 그대로 동작한다."""
    import re

    from radio_helper import db
    from radio_helper.app import create_app

    first = create_app().test_client()
    first.environ_base["HTTP_HOST"] = "127.0.0.1:5000"
    html = first.get("/settings").get_data(as_text=True)
    token = re.search(r'name="csrf_token" value="([0-9a-f]+)"', html).group(1)
    cookie = first.get_cookie("session")

    second = create_app().test_client()                     # 서버를 다시 켬 (새 프로세스와 같음)
    second.environ_base["HTTP_HOST"] = "127.0.0.1:5000"
    second.set_cookie("session", cookie.value, domain="localhost")   # 브라우저에 남아 있던 쿠키 (시험 클라이언트 주소는 localhost)
    r = second.post("/settings", data={"csrf_token": token, "global_stop": "1"})
    assert r.status_code == 302
    conn = db.connect()
    assert db.is_stopped(conn)                               # 예전엔 'Bad Request' 로 막혔음
    conn.close()
    assert (data_dir / "session.key").read_text().strip() == second.application.config["SECRET_KEY"]


def test_stale_page_returns_to_same_screen_with_notice(client, conn):
    from radio_helper import db

    r = client.post("/settings", data={"csrf_token": "오래된값", "global_stop": "1"},
                    headers={"Referer": "http://localhost/settings"})          # 시험 클라이언트 주소는 localhost
    assert r.status_code == 302 and r.headers["Location"].endswith("/settings")
    assert not db.is_stopped(conn)                           # 처리는 하지 않음
    page = client.get("/settings").get_data(as_text=True)
    assert "화면이 오래되어 방금 누른 것은 처리하지 않았습니다" in page
    r = client.post("/settings", data={"global_stop": "1"}, headers={"Referer": "https://evil.example.com/x"})
    assert r.headers["Location"].endswith("/") and not db.is_stopped(conn)   # 바깥 사이트로는 돌려보내지 않음


def test_server_crash_is_logged_and_exits_for_restart(data_dir, monkeypatch):
    import pytest

    monkeypatch.setattr(launcher, "pick_port", lambda first: (5099, []))
    monkeypatch.setattr(launcher, "self_check", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "disable_console_quick_edit", lambda: None)
    monkeypatch.setattr(launcher, "record_native_crashes", lambda: None)
    monkeypatch.setattr(launcher, "watch_runner", lambda: None)                 # 시험 중엔 감시 스레드를 띄우지 않음
    monkeypatch.setattr(launcher.signal, "signal", lambda *a: None)            # 시험 프로세스의 Ctrl+C 처리는 그대로

    def boom(*a, **k):
        raise OSError("소켓 오류")

    monkeypatch.setattr(launcher, "run_simple", boom)
    monkeypatch.setattr("sys.argv", ["radio_helper", "--no-browser"])
    with pytest.raises(SystemExit) as e:
        launcher.main()
    assert e.value.code == launcher.EXIT_CRASH                 # start_windows.bat 이 다시 켬
    log = (data_dir / "logs" / "server.log").read_text(encoding="utf-8")
    assert "시작 — http://127.0.0.1:5099/" in log and "오류로 멈춤" in log and "소켓 오류" in log

    monkeypatch.setattr(launcher, "run_simple", lambda *a, **k: None)   # Ctrl+C: 안에서 받아 정상 종료 → 다시 켜지 않음
    monkeypatch.setattr("sys.argv", ["radio_helper", "--no-browser", "--restarted"])
    launcher.main()
    log = (data_dir / "logs" / "server.log").read_text(encoding="utf-8")
    assert "(비정상 종료 뒤 자동으로 다시 켬)" in log and "종료 (Ctrl+C" in log
    from radio_helper import db

    conn = db.connect()
    assert any("자동으로 다시 켰습니다" in m for (m,) in conn.execute("SELECT message FROM events"))
    conn.close()


def test_windows_launcher_restarts_only_on_crash():
    from pathlib import Path

    bat = (Path(__file__).resolve().parent.parent / "start_windows.bat").read_bytes()
    assert b"\r\n" in bat and b"\n" not in bat.replace(b"\r\n", b"")    # 윈도우 줄바꿈 유지
    text = bat.decode("ascii")
    assert ':run' in text and 'goto run' in text and '--no-browser --restarted' in text
    assert f'if "%RH_CODE%"=="{launcher.EXIT_OK}" goto end' in text
    assert f'if "%RH_CODE%"=="{launcher.EXIT_NO_PORT}" goto end' in text and "GEQ 20" in text


# ── Ctrl+C 로 듣기가 꺼지던 문제 ───────────────────────────────────────
def test_server_needs_ctrl_c_twice(data_dir):
    import pytest

    t = [100.0]
    said = []
    handler = launcher.ctrl_c_handler(clock=lambda: t[0], say=said.append)
    handler(2, None)                                          # 한 번: 안내만 하고 계속
    assert said and "한 번 더" in said[0]
    t[0] += 5
    handler(2, None)                                          # 3초가 지나면 다시 처음부터
    t[0] += 1
    with pytest.raises(KeyboardInterrupt):                    # 3초 안에 두 번째 → 끝냄
        handler(2, None)
    assert "Ctrl+C 한 번" in (data_dir / "logs" / "server.log").read_text(encoding="utf-8")


def test_helpers_ignore_ctrl_c(capsys):
    import signal

    from radio_helper.quizbot import __main__ as qmain

    old = signal.getsignal(signal.SIGINT)
    try:
        qmain.ignore_ctrl_c()
        signal.raise_signal(signal.SIGINT)                    # 예전엔 여기서 KeyboardInterrupt → 듣기 실행기 꺼짐
        assert "Ctrl+C 신호를 받았지만 계속합니다" in capsys.readouterr().out
    finally:
        signal.signal(signal.SIGINT, old)


def test_children_start_in_own_process_group(data_dir, monkeypatch):
    import subprocess

    from radio_helper import app as app_module

    calls = []
    monkeypatch.setattr(app_module.subprocess, "Popen", lambda *a, **k: calls.append(k))
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(app_module.sys, "platform", "win32")
    app_module.launch_quizbot(["run"])
    assert calls[-1]["creationflags"] == 0x200                # 검은 창의 Ctrl+C 가 듣기 실행기에 가지 않음


def test_watchdog_restarts_dead_runner_only_when_listening(conn, monkeypatch):
    from datetime import datetime, timedelta

    from radio_helper import app as app_module
    from radio_helper import db
    from radio_helper.quizbot import config

    started = []
    monkeypatch.setattr(app_module, "start_runner", lambda c: started.append(1) or f"quizbot_{len(started)}.log")
    old = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    db.set_setting(conn, "quizbot.launched_at", old)
    restarts = []

    assert app_module.ensure_runner(conn, restarts, 0) is None and not started   # 청취 중이 아니면 손대지 않음
    db.set_setting(conn, "live.active", "1")
    assert app_module.ensure_runner(conn, restarts, 10) == "quizbot_1.log"         # 꺼진 실행기를 다시 켬
    msgs = [m for (m,) in conn.execute("SELECT message FROM events")]
    assert any("자동으로 다시 켰습니다" in m for m in msgs)

    lock = config.instance_lock("quizbot_run")                                     # 실행기가 살아 있음
    assert app_module.ensure_runner(conn, restarts, 20) is None and len(started) == 1
    lock.close()

    db.set_setting(conn, "quizbot.launched_at", db.now())                          # 막 띄운 중
    assert app_module.ensure_runner(conn, restarts, 30) is None
    db.set_setting(conn, "quizbot.launched_at", old)
    db.set_setting(conn, "quizbot.stop", "1")                                      # '멈춤' 중
    assert app_module.ensure_runner(conn, restarts, 40) is None
    db.set_setting(conn, "quizbot.stop", "0")

    for i in range(4):
        assert app_module.ensure_runner(conn, restarts, 50 + i).startswith("quizbot_")
    assert app_module.ensure_runner(conn, restarts, 60) == "limit" and len(started) == 5   # 30분에 5번까지
    assert app_module.ensure_runner(conn, restarts, 1900) == "quizbot_6.log"               # 30분 지나면 다시
