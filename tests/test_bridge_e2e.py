"""내 Aside 확장 다리 끝까지 (진짜 Chromium + 확장 + 도우미 서버). 오래 걸려 RADIO_HELPER_E2E=1 일 때만.

SBS 주소를 가짜 게시판 화면으로 돌려: 읽기 → 시험 입력 → 등록(확인 창 자동 확인) → 목록에서 제목 확인 → 탭 닫힘,
그리고 사용자가 먼저 열어 둔 탭은 건드리지 않는지 본다.
"""

import os
import threading
import time
from pathlib import Path

import pytest

CHROME = os.environ.get("RADIO_HELPER_E2E_CHROME", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
pytestmark = pytest.mark.skipif(os.environ.get("RADIO_HELPER_E2E") != "1" or not Path(CHROME).exists(),
                                reason="RADIO_HELPER_E2E=1 과 Chromium 이 있을 때만")

BOARD = "https://programs.sbs.co.kr/radio/testshow/boards/1"
WRITE = "https://programs.sbs.co.kr/radio/testshow/write"


def test_extension_bridge_end_to_end(data_dir, tmp_path, monkeypatch):
    from playwright.sync_api import sync_playwright
    from werkzeug.serving import make_server

    from radio_helper import db, tabbridge
    from radio_helper.app import create_app

    monkeypatch.setenv("PORT", "5079")
    app = create_app()
    srv = make_server("127.0.0.1", 5079, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    conn = db.connect()
    ext = tabbridge.write_extension(conn)
    posted = []
    pages = {
        BOARD: "<h1>사연과 신청곡</h1><p>공지: 사연을 보내 주세요</p><a href='/radio/testshow/write'>글쓰기</a><ul>{}</ul>",
        WRITE: "<form onsubmit='return false'><table><tr><th>제목</th><td><input type=text name=subject></td></tr>"
               "<tr><th>내용</th><td><textarea name=content></textarea></td></tr></table><button type=button onclick=\""
               "if(confirm('등록하시겠습니까?')){fetch('/radio/testshow/save',{method:'POST',"
               "body:document.querySelector('[name=subject]').value}).then(()=>location.href='/radio/testshow/boards/1')}\">"
               "등록</button></form>",
    }

    def handle(route, request):
        url = request.url.split("?")[0]
        if url.endswith("/save"):
            posted.append(request.post_data)
            return route.fulfill(status=200, body="ok")
        if url in pages:
            body = pages[url].replace("{}", "".join(f"<li>{t}</li>" for t in posted))
            return route.fulfill(status=200, content_type="text/html; charset=utf-8", body=f"<html><body>{body}</body></html>")
        return route.fulfill(status=404, body="")

    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(str(tmp_path / "profile"), executable_path=CHROME, headless=True,
                                                       args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}",
                                                             "--headless=new"])
            ctx.route("https://programs.sbs.co.kr/**", handle)
            pump = lambda s: ctx.pages[0].wait_for_timeout(s * 1000)   # noqa: E731  (가짜 화면 응답을 돌리며 기다림)
            for _ in range(40):
                if tabbridge.connected(conn):
                    break
                pump(0.5)
            assert tabbridge.connected(conn)

            tid = tabbridge.add_task(conn, "read", BOARD)
            ctx.new_page().goto(BOARD)
            got = tabbridge.wait(conn, tid, 30, sleep=pump)
            assert got["status"] == "done" and "공지: 사연을 보내 주세요" in got["text"]

            tid = tabbridge.add_task(conn, "write", BOARD, {"title": "가을 산책", "body": "본문", "dry_run": True,
                                                            "selectors": {}, "board_url": BOARD})
            test_tab = ctx.new_page()
            test_tab.goto(BOARD)
            got = tabbridge.wait(conn, tid, 30, sleep=pump)
            assert got["step"] == "filled" and test_tab.input_value("[name=subject]") == "가을 산책" and posted == []

            tid = tabbridge.add_task(conn, "write", BOARD, {"title": "겨울 이야기", "body": "본문", "dry_run": False,
                                                            "selectors": {}, "board_url": BOARD})
            real_tab = ctx.new_page()
            real_tab.goto(BOARD)
            got = tabbridge.wait(conn, tid, 40, sleep=pump)
            assert got["step"] == "verified" and posted == ["겨울 이야기"] and "겨울 이야기" in got["after_text"]
            pump(4)
            assert real_tab.is_closed()

            mine = ctx.new_page()                                         # 사용자가 먼저 열어 둔 탭
            tid = tabbridge.add_task(conn, "write", BOARD, {"title": "남의 탭", "body": "x", "dry_run": True, "selectors": {}},
                                     now=time.time() + 30)
            mine.goto(WRITE)
            mine.wait_for_timeout(5000)
            assert mine.input_value("[name=subject]") == "" and tabbridge.task(conn, tid)["status"] == "waiting"
            ctx.close()
    finally:
        srv.shutdown()
        conn.close()
