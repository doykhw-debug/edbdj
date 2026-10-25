"""0.74: 게시판 읽기·쓰기·로그인 확인을 Aside CLI(aside repl)로 — 내 Aside 로그인 그대로, Edge 보조 창 없음.

진짜 크로미움으로 끝까지 하는 시험은 가짜 aside CLI(tests/e2e/fake_aside.js, Node + Playwright)로 돈다
(RADIO_HELPER_E2E=1 일 때). 나머지는 명령·출력 해석만 본다.
"""

import http.server
import json
import os
import shutil
import subprocess
import threading
from functools import partial
from pathlib import Path

import pytest

from radio_helper import aside_cli, boards, db

FAKE = str(Path(__file__).parent / "e2e" / "fake_aside.js")
BOARD = "https://programs.sbs.co.kr/radio/aaa/boards/111"


class Out:
    def __init__(self, stdout="", rc=0):
        self.stdout, self.stderr, self.returncode = stdout.encode("utf-8"), b"", rc


def test_find_prefers_saved_then_path_but_never_the_browser_exe():
    env = {"USERPROFILE": "/home/me", "LOCALAPPDATA": "/home/me/AppData/Local"}
    shim = next(p for p in aside_cli.candidates(env) if p.endswith("aside.cmd"))
    exists = {r"C:\tools\aside.exe", shim}.__contains__
    assert aside_cli.find(r"C:\tools\aside.exe", which=lambda n: None, exists=exists, env=env) == r"C:\tools\aside.exe"
    browser = r"C:\Program Files\Aside\Application\Aside.exe"                       # 윈도우는 대소문자 무시 → 브라우저일 수 있음
    assert aside_cli.find("", which=lambda n: browser, exists=exists, env=env) == shim
    assert aside_cli.find("", which=lambda n: r"C:\Users\me\.local\bin\aside.exe", exists=exists, env=env).endswith("aside.exe")
    assert aside_cli.find("", which=lambda n: None, exists=lambda p: False, env=env) is None


def test_command_wraps_script_for_cmd_shims():
    assert aside_cli.command("aside.exe", "console.log(1)") == ["aside.exe", "repl", "console.log(1)"]
    cmd = aside_cli.command(r"C:\x\aside.cmd", 'console.log("a & b | c")')
    assert cmd[:4] == ["cmd", "/c", r"C:\x\aside.cmd", "repl"]
    assert not set('"&|<>^%') & set(cmd[4])                                        # cmd.exe 가 깨뜨리는 글자 없음


def test_run_reads_json_dir_and_errors(monkeypatch):
    got = aside_cli.run("x", exe="aside", runner=lambda cmd, t: Out('ASIDE_DIR=/tmp/s\nRH_JSON={"a": 1}\nRH_OK\n[ok'))
    assert got["data"] == {"a": 1} and got["dir"] == "/tmp/s"
    with pytest.raises(aside_cli.AsideCliError, match="openTab is not defined"):
        aside_cli.run("x", exe="aside", runner=lambda cmd, t: Out("[error ReferenceError: openTab is not defined"))
    monkeypatch.setattr(aside_cli, "find", lambda saved="": None)
    with pytest.raises(aside_cli.AsideCliError, match="찾지 못했습니다"):
        aside_cli.run("x")


def test_probe_and_ready_cache(conn):
    calls = []

    def runner(cmd, timeout):
        calls.append(cmd)
        return Out("aside 1.26.0") if cmd[-1] == "--version" else Out("RH_READY /tmp/s\nRH_OK\n[ok")
    st = aside_cli.probe(conn, runner=runner, finder=lambda saved: "aside")
    assert st["ok"] and st["version"] == "aside 1.26.0" and "연결됨" in st["detail"]
    n = len(calls)
    assert aside_cli.ready(conn, runner=runner, finder=lambda saved: "aside") and len(calls) == n   # 10분 안에는 다시 안 봄
    bad = aside_cli.probe(conn, runner=lambda c, t: Out(""), finder=lambda saved: "aside")
    assert not bad["ok"] and "Aside 앱이 답하지 않음" in bad["detail"]
    none = aside_cli.probe(conn, finder=lambda saved: None)
    assert not none["ok"] and "찾지 못함" in none["detail"]


def test_install_skills_runs_the_requested_command(conn, monkeypatch):
    monkeypatch.setattr(aside_cli, "find", lambda saved="": "aside")
    seen = []
    text = aside_cli.install_skills(conn, runner=lambda cmd, t: seen.append(cmd) or Out("installed"))
    assert seen == [["aside", "skills", "install", "--target", "claude-code"]] and "installed" in text


def test_post_records_submission_from_cli_result(conn, monkeypatch):
    from radio_helper import evidence

    monkeypatch.setattr(aside_cli, "find", lambda saved="": "aside")
    cid = boards.add_corner(conn, BOARD)
    eid = conn.execute("INSERT INTO experiences (story, user_confirmed, created_at, updated_at) VALUES ('x', 1, ?, ?)",
                       (db.now(), db.now())).lastrowid
    did = boards.make_draft(conn, cid, eid, "가을 소풍", "본문입니다")
    draft = conn.execute("SELECT * FROM drafts WHERE id = ?", (did,)).fetchone()
    corner = conn.execute("SELECT * FROM corners WHERE id = ?", (cid,)).fetchone()
    shots = []
    monkeypatch.setattr(evidence, "save", lambda *a, **k: shots.append(a[-1]))
    result = {"status": "done", "step": "verified", "after_text": "목록 가을 소풍 2026.10.25", "after_url": BOARD,
              "dialogs": ["등록하시겠습니까?"], "found": {"title": "#subject", "body": "#content"}}
    sid = aside_cli.post(conn, draft, corner, runner=lambda c, t: Out("RH_JSON=" + json.dumps(result) + "\nRH_OK"))
    sub = conn.execute("SELECT * FROM submissions WHERE id = ?", (sid,)).fetchone()
    assert sub["post_status"] == "posted" and "Aside CLI" in sub["note"] and "등록하시겠습니까" in sub["note"]
    assert conn.execute("SELECT title_selector FROM corners WHERE id = ?", (cid,)).fetchone()[0] == "#subject"
    from radio_helper import autofill
    with pytest.raises(autofill.AutofillError, match="로그인이 필요"):
        aside_cli.post(conn, draft, corner, runner=lambda c, t: Out('RH_JSON={"status": "login"}\nRH_OK'))


def test_login_check_and_board_mode(conn, monkeypatch):
    monkeypatch.setattr(aside_cli, "find", lambda saved="": "aside")
    ok, why = aside_cli.check_login(conn, BOARD, runner=lambda c, t: Out(
        'RH_JSON=' + json.dumps({"text": "홍길동 님 로그아웃", "password": False}) + "\nRH_OK"))
    assert ok is True and "로그인됨" in why
    ok, why = aside_cli.check_login(conn, BOARD, runner=lambda c, t: Out(
        'RH_JSON=' + json.dumps({"text": "로그인", "password": True}) + "\nRH_OK"))
    assert ok is False
    monkeypatch.setattr(aside_cli, "ready", lambda c: False)
    monkeypatch.setattr(boards, "tabs_on", lambda c: False)
    boards.add_corner(conn, BOARD)
    said = []
    out = boards.check_logins(conn, say=said.append)
    assert "Aside 연결 안 됨" in said[0] and out["SBS"]["ok"] is None              # Edge 로 확인하지 않음


# ── 끝까지: 가짜 aside CLI + 진짜 크로미움 + 가짜 게시판 ──────────────────────────
BOARD_HTML = """<html><head><meta charset="utf-8"></head><body><h1>사연 게시판</h1><p>공지: 이번 주 주제는 가을 소풍입니다. 지금 모집합니다.</p>
<a href="write.html">글쓰기</a><a href="list.html">목록</a></body></html>"""
WRITE_HTML = """<html><head><meta charset="utf-8"></head><body><form action="list.html" method="get" onsubmit="return confirm('등록하시겠습니까?')">
<label>제목 <input type="text" id="subject" name="subject"></label>
<textarea id="content" name="content"></textarea>
<button type="submit">등록</button></form></body></html>"""
LIST_HTML = """<html><head><meta charset="utf-8"></head><body><div id="l"></div><script>
document.getElementById('l').innerText = '목록 ' + (new URLSearchParams(location.search).get('subject') || '');
</script></body></html>"""


SITE_DIR: dict[str, str] = {}


@pytest.fixture
def site(tmp_path):
    for name, html in (("board.html", BOARD_HTML), ("write.html", WRITE_HTML), ("list.html", LIST_HTML)):
        (tmp_path / name).write_text(html, encoding="utf-8")
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    SITE_DIR[str(httpd.server_address[1])] = str(tmp_path)
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def fake_runner(cmd, timeout):
    if cmd[0] == "cmd":                     # .cmd 감싸기도 같은 가짜로
        cmd = cmd[2:]
    done = subprocess.run(["node", FAKE, *cmd[1:]], capture_output=True, timeout=timeout,
                          env={**os.environ, "NODE_PATH": os.environ.get("NODE_PATH", "")})
    return done


e2e = pytest.mark.skipif(os.environ.get("RADIO_HELPER_E2E") != "1" or not shutil.which("node"),
                         reason="RADIO_HELPER_E2E=1 과 node+playwright 가 있을 때만")


@e2e
def test_e2e_read_board_follow_write_and_post(site):
    got = aside_cli.run(aside_cli.read_script(site + "/board.html"), exe="aside", runner=fake_runner)["data"]
    assert "가을 소풍" in got["text"] and got["write_url"] is None and any("write.html" in h for h, _ in got["links"])
    payload = {"title": "가을 소풍 이야기", "body": "작년 가을에 아이들과 소풍을 갔어요.", "song": "", "dry_run": True,
               "selectors": {"title": None, "body": None, "song": None, "submit": None}}
    res = aside_cli.run(aside_cli.write_script(site + "/board.html", payload), exe="aside", runner=fake_runner)
    assert res["data"]["status"] == "done" and res["data"]["step"] == "filled"            # 게시판 → '글쓰기' → 입력만
    assert res["data"]["found"] == {"title": "#subject", "body": "#content"}
    assert Path(res["dir"], "filled.jpg").stat().st_size > 1000
    payload["dry_run"] = False
    res = aside_cli.run(aside_cli.write_script(site + "/write.html", payload), exe=r"C:\x\aside.cmd", runner=fake_runner)
    d = res["data"]
    assert d["status"] == "done" and d["step"] == "verified" and "가을 소풍 이야기" in d["after_text"]
    assert d["dialogs"] == ["등록하시겠습니까?"] and not d["still_form"]                  # 확인 창은 도우미가 '확인'


# ── 0.75: KBS 로그인 '판단 못 함' · 사용자 PC 의 CLI 자리 · 확장 고정 → 자동 ───────────────────
def test_cli_path_seen_on_users_pc_is_a_candidate():
    env = {"LOCALAPPDATA": "/AppData/Local", "USERPROFILE": "/home/me"}
    assert aside_cli.candidates(env)[0].replace("\\\\", "/").endswith("Aside/CLI/current/aside.exe")


def test_kbs_login_falls_back_to_station_home(conn, monkeypatch):
    monkeypatch.setattr(aside_cli, "find", lambda saved="": "aside")
    pages = iter([{"text": "사연과 신청곡 게시판 목록"}, {"text": "KBS 홍길동 님 MY 로그아웃"}])
    seen = []

    def runner(cmd, t):
        seen.append(cmd[-1])
        return Out("RH_JSON=" + json.dumps(next(pages)) + "\nRH_OK")
    ok, why = aside_cli.check_login(conn, "https://program.kbs.co.kr/x/pc/board.html", runner=runner,
                                    home="https://www.kbs.co.kr")
    assert ok is True and "방송국 첫 화면" in why and "www.kbs.co.kr" in seen[1]
    ok, why = aside_cli.check_login(conn, "https://program.kbs.co.kr/x/pc/board.html", home="https://www.kbs.co.kr",
                                    runner=lambda c, t: Out('RH_JSON={"text": "목록"}\nRH_OK'))
    assert ok is None and "판단 못 함" in why


def test_tab_choice_moves_to_auto_once(data_dir):
    from radio_helper import aside
    from radio_helper.app import create_app

    c = db.connect()
    db.init_db(c)
    db.set_setting(c, aside.BROWSER_KEY, "tab")
    c.close()
    create_app()
    c = db.connect()
    assert db.get_setting(c, aside.BROWSER_KEY) == "auto"
    db.set_setting(c, aside.BROWSER_KEY, "tab")                                     # 그 뒤 사용자가 고른 값은 그대로
    c.close()
    create_app()
    c = db.connect()
    assert db.get_setting(c, aside.BROWSER_KEY) == "tab"
    c.close()


LATE_HTML = """<html><head><meta charset="utf-8"></head><body><div id="gnb"></div>
<iframe src="inner.html"></iframe>
<script>setTimeout(() => { document.getElementById('gnb').innerText = 'KBS 홍길동 님 로그아웃'; }, 3000);</script></body></html>"""
INNER_HTML = """<html><head><meta charset="utf-8"></head><body>공지: 사연과 신청곡을 받습니다 (게시판 안쪽 틀)</body></html>"""


@e2e
def test_e2e_late_render_and_iframe_text(site):
    folder = Path(SITE_DIR[site.rsplit(":", 1)[1]])                                 # 가짜 게시판 폴더에 두 화면 추가
    for name, html in (("late.html", LATE_HTML), ("inner.html", INNER_HTML)):
        (folder / name).write_text(html, encoding="utf-8")
    got = aside_cli.run(aside_cli.read_script(site + "/late.html"), exe="aside", runner=fake_runner)["data"]
    assert "로그아웃" in got["text"] and "게시판 안쪽 틀" in got["text"]


def test_kbs_login_is_checked_on_radio_kbs_home(conn):
    """0.78 사용자 지정: KBS 로그인은 게시판 화면(예: 1라디오 뉴스 게시판)이 아닌 radio.kbs.co.kr 에서 확인."""
    boards.add_corner(conn, "https://program.kbs.co.kr/1radio/radio/knews/pc/board.html?smenu=9c15ec&"
                            "bbs_loc=R2023-0196-03-387642,list,none,1,0")
    kbs = next(t for t in boards.login_targets(conn) if t["station"] == "KBS")
    assert kbs["url"] == kbs["home"] == "https://radio.kbs.co.kr/"
    assert boards.HOMES["KBS"] == "https://radio.kbs.co.kr/"                          # 미리 로그인 탭도 이 화면
