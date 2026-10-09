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
