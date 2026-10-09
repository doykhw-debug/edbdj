"""게시판 사연 전체 흐름을 실제 브라우저(Chromium)로 끝까지:

진행자 '게시판에만' → 원고 검토함에 원고 → 사용자 승인 → 제출 조건(코너 모집·공지 확인) →
모의 화면 입력·등록 → 실제 화면 입력(가짜 SBS 글쓰기 화면, 도구는 입력만) → 사용자가 등록(가짜 화면이 대신)
→ 제출 이력 '결과 불명' + 글 주소 → 같은 방송국(SBS)에는 그 경험을 다시 보내지 않음.

실제 SBS 사이트는 이 저장소의 시험 환경에서 열 수 없고 로그인이 필요해서, 같은 주소 모양의 가짜 화면을 쓴다.
"""

import threading
from pathlib import Path

import pytest
from conftest import csrf, make_experience
from flask import Flask, redirect, request
from test_autofill import CHROMIUM
from test_story_gift_import import story_for
from test_witty_chat import run_story
from werkzeug.serving import make_server

from radio_helper import autofill, db, gates
from radio_helper.quizbot import story

TITLE = "첫 출근 날의 커피"
BODY = "첫 출근 날 실수로 사장님 커피를 마셨습니다.\n\n그날 이후 커피는 꼭 이름표를 붙입니다."
WRITE_PATH = "/radio/0chulpowerfm/cornerboardwrite/57577/"
LIST_PATH = "/radio/0chulpowerfm/cornerboards/57577"


def serve(app):
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def fake_sbs_board(posted: list):
    """SBS 글쓰기 화면과 같은 주소 모양의 가짜 게시판. 두 칸이 채워지면 '사용자가 등록을 누른 것처럼' 스스로 등록한다."""
    site = Flask("fake_sbs")

    @site.route(WRITE_PATH, methods=["GET", "POST"])
    def write():
        if request.method == "POST":
            posted.append({"title": request.form["title"], "content": request.form["content"]})
            return redirect(f"{LIST_PATH}?cornerid=3002&no={len(posted)}")
        return """<!doctype html><meta charset="utf-8"><title>글쓰기</title>
<form id="f" method="post"><input id="title" name="title"><textarea id="content" name="content"></textarea>
<button type="submit">등록</button></form>
<script>setInterval(() => { const t = document.getElementById('title').value, c = document.getElementById('content').value;
  if (t && c && !window.sent) { window.sent = true; setTimeout(() => document.getElementById('f').submit(), 1500); } }, 300);</script>"""

    @site.get(LIST_PATH)
    def board():
        p = posted[-1]
        return f"<!doctype html><meta charset='utf-8'><h1>{p['title']}</h1><pre>{p['content']}</pre>"

    return site


@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="Chromium 없음")
def test_board_story_whole_flow(client, conn, app, monkeypatch):
    pytest.importorskip("playwright")
    monkeypatch.setenv("RADIO_HELPER_CHROMIUM", CHROMIUM)

    # 1) 진행자가 '게시판에만' → 원고 검토함에 원고 (채팅으로는 보내지 않음)
    eid = make_experience(conn, story="첫 출근 날 실수로 사장님 커피를 마셨다", highlight="", ending="", quotes="",
                          quote_kind="none")
    sender = run_story(conn, story_for(eid, board_only=True, board_title=TITLE, board_body=BODY))
    post = conn.execute("SELECT * FROM story_posts").fetchone()
    assert sender.sent == [] and post["target"] == "board"
    did = post["draft_id"]
    draft = conn.execute("SELECT * FROM drafts WHERE id = ?", (did,)).fetchone()
    corner_id = draft["corner_id"]

    # 2) 승인 전·코너 확인 전에는 실제 화면 입력이 막힘
    blocked = gates.evaluate(conn, did, gates.MODE_FILL)
    assert not blocked.ok and any("승인" in b for b in blocked.blockers) and any("모집" in b for b in blocked.blockers)

    # 3) 사용자가 원고 승인 ('실제와 같음' 체크) + 코너 공지 확인 (화면에서 하는 그대로)
    token = csrf(client, f"/drafts/{did}")
    client.post(f"/drafts/{did}/approve", data={"csrf_token": token})                      # 체크 안 하면 승인 안 됨
    assert conn.execute("SELECT status FROM drafts WHERE id = ?", (did,)).fetchone()[0] == "draft"
    client.post(f"/drafts/{did}/approve", data={"csrf_token": token, "fact_confirmed": "1"})
    assert conn.execute("SELECT status FROM drafts WHERE id = ?", (did,)).fetchone()[0] == "approved"

    # 4) 모의 화면 입력·등록 (이 도우미 안의 가짜 글쓰기 화면, 실제 사이트에는 아무것도 안 남김)
    helper = serve(app)
    try:
        assert autofill.main(["--draft", str(did), "--mode", "mock", "--headless",
                              "--base-url", f"http://127.0.0.1:{helper.server_port}/"]) == 0
    finally:
        helper.shutdown()
    mock = conn.execute("SELECT * FROM mock_posts ORDER BY id DESC").fetchone()
    assert mock["title"] == TITLE and mock["content"].replace("\r\n", "\n") == BODY
    assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0

    # 5) 실제 화면 입력: 가짜 SBS 글쓰기 화면 (주소 허용 목록만 이 시험용으로 넓힘)
    posted: list = []
    site = serve(fake_sbs_board(posted))
    base = f"http://127.0.0.1:{site.server_port}"
    monkeypatch.setattr(autofill, "_allowed_real_url", lambda url: url.startswith(base))
    monkeypatch.setattr(autofill, "LOGIN_WAIT_SECONDS", 20)
    monkeypatch.setattr(autofill, "AFTER_FILL_WAIT_SECONDS", 8)
    client.post(f"/corners/{corner_id}", data={
        "csrf_token": token, "recruiting": "open", "deadline": "2099-12-31", "ai_assist_policy": "allowed",
        "is_target": "1", "write_url": f"{base}{WRITE_PATH}?cornerid=3002", "title_selector": "#title",
        "body_selector": "#content", "notice_text": "사연과 신청곡을 받습니다."})
    client.post(f"/corners/{corner_id}/confirm-notice", data={"csrf_token": token})
    assert gates.evaluate(conn, did, gates.MODE_FILL).ok
    try:
        assert autofill.main(["--draft", str(did), "--mode", "fill", "--headless"]) == 0
    finally:
        site.shutdown()

    # 도구는 입력만, 등록은 '사용자'(가짜 화면 스크립트)가 → 게시판에 원고 그대로 올라감
    assert posted == [{"title": TITLE, "content": BODY.replace("\n", "\r\n")}] or \
        [{"title": p["title"], "content": p["content"].replace("\r\n", "\n")} for p in posted] == \
        [{"title": TITLE, "content": BODY}]
    sub = conn.execute("SELECT * FROM submissions").fetchone()
    assert sub["draft_id"] == did and sub["post_status"] == "unknown"                   # 글쓰기 화면을 벗어남
    assert sub["post_url"] == f"{base}{LIST_PATH}?cornerid=3002&no=1"
    events = " ".join(r[0] for r in conn.execute("SELECT message FROM events"))
    assert "입력 완료" in events and "결과 불명" in events

    # 6) 사용자가 글 주소를 확인해 '등록 확인'으로 바꿈 → 보낸 사연 목록에 SBS 게시판으로
    client.post(f"/submissions/{sub['id']}", data={"csrf_token": token, "post_status": "posted",
                                                   "post_url": sub["post_url"], "adopted": "unknown",
                                                   "won": "unknown", "prize_received": "unknown"})
    assert conn.execute("SELECT post_status FROM submissions").fetchone()[0] == "posted"
    sent = client.get("/stories/sent").get_data(as_text=True)
    assert TITLE in sent and "게시판" in sent and "SBS 1" in sent

    # 7) 같은 경험은 SBS(게시판·채팅 모두)에 다시 쓰지 않고, 다른 방송국에는 쓸 수 있음
    assert eid not in {e["id"] for e in story.candidate_experiences(conn, "SBS")}
    assert eid in {e["id"] for e in story.candidate_experiences(conn, "MBC")}
    assert not gates.evaluate(conn, did, gates.MODE_FILL).ok
    assert not (db.data_dir() / "input.lock").exists()


def test_collected_corners_get_write_url():
    f = db.corner_write_url
    assert f("https://programs.sbs.co.kr/radio/0chulpowerfm/cornerboards/57577?cornerid=3002") == \
        "https://programs.sbs.co.kr/radio/0chulpowerfm/cornerboardwrite/57577/?cornerid=3002"
    assert f("https://programs.sbs.co.kr/radio/ten/cornerboards/9") == \
        "https://programs.sbs.co.kr/radio/ten/cornerboardwrite/9/"
    assert f("https://programs.sbs.co.kr/radio/ten/boards/57950") is None        # 일반 게시판은 모양을 몰라 비움
    assert f("https://evil.example.com/radio/ten/cornerboards/9") is None


def test_init_fills_missing_write_urls_and_selectors_are_shared(conn):
    from radio_helper import seed

    # 0.10 에서 자동 수집해 글쓰기 주소가 빈 코너
    conn.execute("INSERT INTO corners (program, kind, title, board_url, updated_at) VALUES "
                 "('배성재의 텐', '코너', '텐 퀴즈', 'https://programs.sbs.co.kr/radio/ten/cornerboards/9?cornerid=4', ?)",
                 (db.now(),))
    conn.commit()
    db.init_db(conn)
    ten = conn.execute("SELECT * FROM corners WHERE title = '텐 퀴즈'").fetchone()
    assert ten["write_url"] == "https://programs.sbs.co.kr/radio/ten/cornerboardwrite/9/?cornerid=4"
    target = conn.execute("SELECT * FROM corners WHERE is_target = 1").fetchone()
    assert target["write_url"] == seed.CORNERS[0][3]                                    # 처음 넣은 공식 주소는 그대로

    # 입력 요소: 아무 데도 없으면 막고, 한 코너에서 확인하면 다른 SBS 코너도 그것을 씀
    eid = make_experience(conn)
    did = conn.execute("INSERT INTO drafts (experience_id, corner_id, title, body, status, fact_confirmed, created_at, "
                       "updated_at) VALUES (?, ?, '제목', '본문입니다', 'approved', 1, ?, ?)",
                       (eid, ten["id"], db.now(), db.now())).lastrowid
    conn.execute("UPDATE corners SET is_target = 1, recruiting = 'open', notice_checked_at = ?, deadline = '2099-12-31', "
                 "ai_assist_policy = 'allowed' WHERE id = ?", (db.now(), ten["id"]))
    conn.commit()
    r = gates.evaluate(conn, did, gates.MODE_FILL)
    assert not r.ok and any("입력 요소를 아직 모릅니다" in b for b in r.blockers)
    conn.execute("UPDATE corners SET title_selector = '#title', body_selector = '#content' WHERE id = ?", (target["id"],))
    conn.commit()
    r = gates.evaluate(conn, did, gates.MODE_FILL)
    assert r.ok and any("사연과 신청곡" in w for w in r.warnings)
    sel, note = gates.corner_selectors(conn, conn.execute("SELECT * FROM corners WHERE id = ?", (ten["id"],)).fetchone())
    assert (sel["title"], sel["body"]) == ("#title", "#content") and "사연과 신청곡" in note
