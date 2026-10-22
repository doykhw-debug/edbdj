"""게시판 사연 자동 승인·등록 (0.29): 방송에서 '게시판에 남겨 달라'고 한 사연은 검사 경고가 없으면
사람 승인 없이 입력하고 '등록'까지 누른다. 로그인·보안 문자는 여전히 사용자가 직접 (우회하지 않음).

실제 SBS 사이트는 시험 환경에서 열 수 없고 로그인이 필요해서, 같은 주소 모양의 가짜 글쓰기 화면을 쓴다.
"""

import threading
import time
from pathlib import Path

import pytest
from conftest import csrf, make_draft, make_experience, target_corner_id
from flask import Flask, redirect, request
from test_autofill import CHROMIUM
from test_board_flow import LIST_PATH, WRITE_PATH, serve
from test_live import live_runner, start_live
from test_quizbot import FakeGorilla
from test_story_gift_import import story_for
from test_witty_chat import FullAnswerer, runner_start

from radio_helper import autofill, db, gates
from radio_helper.quizbot import config, runner

TITLE = "첫 출근 날의 커피"
BODY = "첫 출근 날 실수로 사장님 커피를 마셨습니다.\n\n그날 이후 커피는 꼭 이름표를 붙입니다."


def add_story_post(conn, eid, did, status="unknown", channel="파워FM", program="김영철의 파워FM"):
    cur = conn.execute(
        "INSERT INTO story_posts (program, channel, broadcast_date, topic, experience_id, message, status, target, "
        "draft_id, created_at, updated_at) VALUES (?, ?, '2026-10-11', '첫 출근 날', ?, ?, ?, 'board', ?, ?, ?)",
        (program, channel, eid, BODY, status, did, db.now(), db.now()))
    conn.commit()
    return cur.lastrowid


# ── 제출 조건 (post 모드) ───────────────────────────────────────────
def test_post_mode_replaces_approval_with_clean_checks(conn):
    eid = make_experience(conn)
    did = make_draft(conn, eid)                                     # 승인 전·사실 확인 전·코너 공지 확인 전
    fill = gates.evaluate(conn, did, gates.MODE_FILL)
    assert not fill.ok and any("승인" in b for b in fill.blockers) and any("입력 요소" in b for b in fill.blockers)
    post = gates.evaluate(conn, did, gates.MODE_POST)
    assert post.ok, post.blockers
    assert any("모집 중으로 보고" in w for w in post.warnings) and any("자동으로 찾습니다" in w for w in post.warnings)

    # 사람이 보지 않으므로 검사 '경고'도 막는다 (재료에 없는 숫자 = 지어냈을 수 있음)
    conn.execute("UPDATE drafts SET body = '첫째가 3시간 동안 제 편집 화면을 봤어요.' WHERE id = ?", (did,))
    conn.commit()
    assert gates.evaluate(conn, did, gates.MODE_FILL).warnings
    r = gates.evaluate(conn, did, gates.MODE_POST)
    assert not r.ok and any(b.startswith("자동 등록은 검사 경고가 없는 원고만") for b in r.blockers)


@pytest.mark.parametrize("change, reason", [
    ("recruiting = 'closed'", "모집 마감"),
    ("deadline = '2020-01-01'", "마감"),
    ("ai_assist_policy = 'restricted'", "AI 보조"),
    ("write_url = NULL", "글쓰기 화면 주소"),
])
def test_post_mode_still_blocks(conn, change, reason):
    db.set_setting(conn, gates.AI_OK_KEY, "0")                  # 'AI 글 제한 공지도 올리기'를 끈 경우
    did = make_draft(conn, make_experience(conn))
    conn.execute(f"UPDATE corners SET {change} WHERE id = ?", (target_corner_id(conn),))
    conn.commit()
    r = gates.evaluate(conn, did, gates.MODE_POST)
    assert not r.ok and any(reason in b for b in r.blockers)


def test_ai_restricted_notice_posts_when_user_needs_ai_help(conn):
    # 0.65: 사용자가 AI 보조 도구가 꼭 필요하다고 정함 → 'AI 글 제한' 공지는 막지 않고 경고만 (기본 켬)
    from datetime import datetime

    from radio_helper import boards

    did = make_draft(conn, make_experience(conn))
    conn.execute("UPDATE corners SET ai_assist_policy = 'restricted' WHERE id = ?", (target_corner_id(conn),))
    conn.commit()
    r = gates.evaluate(conn, did, gates.MODE_POST)
    assert r.ok and any("AI 보조 도구가 꼭 필요함" in w for w in r.warnings)
    notice = {"accepting": "yes", "ai_restricted": True}
    corner = boards.corner_row(conn, target_corner_id(conn))
    assert boards.skip_reason(notice, corner, datetime(2026, 10, 22), ai_assist_ok=True) is None
    assert "AI" in boards.skip_reason(notice, corner, datetime(2026, 10, 22), ai_assist_ok=False)


def test_post_mode_unconfirmed_experience_and_stop_switch(conn):
    did = make_draft(conn, make_experience(conn, user_confirmed=0))
    assert any("실제 있었던 일" in b for b in gates.evaluate(conn, did, gates.MODE_POST).blockers)
    did = make_draft(conn, make_experience(conn))
    assert gates.evaluate(conn, did, gates.MODE_POST).ok
    db.set_setting(conn, "global_stop", "1")
    assert any("일괄 중지" in b for b in gates.evaluate(conn, did, gates.MODE_POST).blockers)


def test_post_mode_has_no_daily_cap(conn):
    did = make_draft(conn, make_experience(conn))
    for i in range(12):                                              # 하루 상한 없음 (무제한)
        conn.execute("INSERT INTO submissions (draft_id, experience_id, corner_id, title, body, post_status, created_at, "
                     "updated_at) VALUES (NULL, ?, ?, ?, ?, 'posted', ?, ?)",
                     (make_experience(conn), target_corner_id(conn), f"글 {i}", f"다른 글 {i}번째", db.now(), db.now()))
    conn.commit()
    assert gates.evaluate(conn, did, gates.MODE_POST).ok


def test_post_mode_ignores_its_own_story_but_not_others(conn):
    eid = make_experience(conn)
    did = make_draft(conn, eid)
    add_story_post(conn, eid, did, status="unknown")                 # 이 원고로 등록 중인 사연 자신
    assert gates.evaluate(conn, did, gates.MODE_POST).ok
    add_story_post(conn, eid, None, status="entered")                # 같은 경험을 SBS 채팅으로 이미 보냄
    r = gates.evaluate(conn, did, gates.MODE_POST)
    assert not r.ok and any("이미 보냈습니다" in b for b in r.blockers)


# ── 게시판 고르기 ───────────────────────────────────────────────────
def test_board_corner_follows_what_the_host_said(conn):
    rows = [("사연과 신청곡", 1, "https://programs.sbs.co.kr/radio/love/cornerboardwrite/1/?cornerid=1"),
            ("러브게임의 법칙", 0, "https://programs.sbs.co.kr/radio/love/cornerboardwrite/1/?cornerid=2"),
            ("러브게임", 0, None)]
    for i, (title, target, url) in enumerate(rows):
        conn.execute("INSERT INTO corners (program, kind, title, board_url, is_target, write_url, updated_at) "
                     "VALUES ('시험 러브게임', '코너', ?, ?, ?, ?, ?)",
                     (title, f"https://programs.sbs.co.kr/radio/love/cornerboards/1?cornerid={i + 1}", target, url, db.now()))
    conn.commit()
    pick = lambda hint="": runner.board_corner(conn, "시험 러브게임", hint)["title"]  # noqa: E731
    assert pick() == "사연과 신청곡"
    assert pick("오늘은 [러브게임의 법칙] 게시판에 사연 남겨 주세요") == "러브게임의 법칙"   # 긴 이름 우선
    assert pick("러브 게임의 법칙 게시판으로") == "러브게임의 법칙"                        # 띄어쓰기 무시
    assert runner.board_corner(conn, "없는 프로그램") is None


# ── 듣기 프로그램: 자동 승인 → 등록 도구 ─────────────────────────────
def run_board(conn, res, launched, **flags):
    start_live(conn, channel="파워FM", auto_story="1", **flags)
    sender = FakeGorilla()
    r, _ = live_runner(conn, {3: "오늘의 주제는 첫 출근 날 사연은 게시판에만 남겨 주세요"}, FullAnswerer(stories=[res]),
                       sender, n=8, start=runner_start())
    r.deps.launch_board = lambda draft_id: launched.append(draft_id) or "autofill_test.log"
    r.run_live()
    return sender


def test_board_story_auto_approved_and_posted(client, conn):
    eid = make_experience(conn, story="첫 출근 날 실수로 사장님 커피를 마셨다. 그날 이후 커피는 꼭 이름표를 붙인다",
                          highlight="", ending="", quotes="", quote_kind="none")
    launched: list = []
    sender = run_board(conn, story_for(eid, board_only=True, board_title=TITLE, board_body=BODY), launched)
    post = conn.execute("SELECT * FROM story_posts").fetchone()
    assert sender.sent == [] and launched == [post["draft_id"]]
    assert (post["status"], post["sent_via"]) == ("unknown", "board") and "자동 등록 중" in post["decision"]
    draft = conn.execute("SELECT * FROM drafts WHERE id = ?", (post["draft_id"],)).fetchone()
    assert draft["status"] == "approved" and draft["fact_confirmed"] == 0    # 사용자가 한 '사실 확인'처럼 꾸미지 않음
    events = " ".join(r[0] for r in conn.execute("SELECT message FROM events"))
    assert "게시판 자동 등록 시작" in events
    sent = client.get("/stories/sent").get_data(as_text=True)
    assert "게시판 (자동 등록 중)" in sent and "게시판 등록 중·결과 불명" in sent


def test_board_story_with_warning_is_held_when_approved_auto_off(conn):
    eid = make_experience(conn, story="첫 출근 날 실수로 사장님 커피를 마셨다", highlight="", ending="", quotes="",
                          quote_kind="none")
    launched: list = []
    run_board(conn, story_for(eid, board_only=True, board_title=TITLE,
                              board_body="첫 출근 날 커피를 3잔이나 마셨습니다."), launched,   # 재료에 없는 숫자
              story_approved="0")
    post = conn.execute("SELECT * FROM story_posts").fetchone()
    assert launched == [] and post["status"] == "pending"
    assert post["decision"].startswith("게시판 자동 등록 보류") and "원고 검토함" in post["decision"]
    assert conn.execute("SELECT status FROM drafts").fetchone()[0] == "draft"


def test_board_story_with_warning_posts_approved_text(conn):
    """승인한(확인·고친) 경험이면 다시 확인받지 않는다: AI 원고에 경고가 나면 승인한 글 그대로 원고를 바꿔 등록한다."""
    eid = make_experience(conn, label="첫 출근 날 커피", story="첫 출근 날 실수로 사장님 커피를 마셨다.",
                          highlight="", ending="그 뒤로 컵에 이름을 붙인다.", quotes="", quote_kind="none")
    launched: list = []
    run_board(conn, story_for(eid, board_only=True, board_title="커피 3잔의 비밀",
                              board_body="첫 출근 날 커피를 3잔이나 마셨습니다."), launched)
    post = conn.execute("SELECT * FROM story_posts").fetchone()
    draft = conn.execute("SELECT * FROM drafts").fetchone()
    assert launched == [draft["id"]] and post["status"] == "unknown" and post["source"] == "approved"
    assert (draft["title"], draft["body"]) == ("첫 출근 날 커피", "첫 출근 날 실수로 사장님 커피를 마셨다.\n\n그 뒤로 컵에 이름을 붙인다.")
    assert "3잔" not in draft["body"]


def test_board_auto_off_keeps_manual_review(conn):
    eid = make_experience(conn, story="첫 출근 날 실수로 사장님 커피를 마셨다", highlight="", ending="", quotes="",
                          quote_kind="none")
    launched: list = []
    run_board(conn, story_for(eid, board_only=True, board_title=TITLE, board_body="첫 출근 날 실수로 사장님 커피를 마셨습니다."),
              launched, board_auto="0")
    post = conn.execute("SELECT * FROM story_posts").fetchone()
    assert launched == [] and "원고 검토함" in post["decision"] and post["status"] == "pending"


def test_board_auto_defaults_and_toggle(client, conn):
    assert config.get(conn, "live.board_auto") == "1"
    assert "quizbot.max_board_posts_per_day" not in config.DEFAULTS
    html = client.get("/").get_data(as_text=True)
    assert 'name="board_auto"' in html and "게시판 사연도 자동 등록" in html


def test_corner_submit_selector_saved(client, conn):
    cid = target_corner_id(conn)
    token = csrf(client, f"/corners/{cid}")
    client.post(f"/corners/{cid}", data={"csrf_token": token, "recruiting": "open", "ai_assist_policy": "allowed",
                                         "is_target": "1", "write_url": "", "submit_selector": "#btnSave"})
    assert conn.execute("SELECT submit_selector FROM corners WHERE id = ?", (cid,)).fetchone()[0] == "#btnSave"


def test_post_lock_waits_for_previous_post(data_dir):
    path = db.data_dir() / "board_post.lock"
    path.write_text("1")
    threading.Timer(1.0, lambda: path.unlink()).start()
    started = time.time()
    with autofill.input_lock("board_post.lock", wait_s=10):
        assert path.exists()
    assert 0.8 < time.time() - started < 10 and not path.exists()
    path.write_text("1")
    with pytest.raises(autofill.AutofillError):
        with autofill.input_lock("board_post.lock", wait_s=0):
            pass


# ── 등록 도구 (가짜 SBS 글쓰기 화면, 실제 브라우저) ──────────────────
def fake_sbs_write(posted: list, need_login=False, captcha=False):
    """사람이 누르는 것처럼 '등록'을 눌러야만 올라가는 가짜 글쓰기 화면 (등록 전에 확인 창이 뜸)."""
    site = Flask("fake_sbs_post")

    @site.route(WRITE_PATH, methods=["GET", "POST"])
    def write():
        if need_login:
            return redirect("/login")
        if request.method == "POST":
            posted.append({"title": request.form["title"], "content": request.form["content"]})
            return redirect(f"{LIST_PATH}?cornerid=3002&no={len(posted)}")
        return f"""<!doctype html><meta charset="utf-8"><title>글쓰기</title>
<header><input type="text" name="q" placeholder="검색"><a href="/login">로그인</a></header>
<form method="post" onsubmit="return confirm('등록하시겠습니까?')">
<input type="text" name="title" placeholder="제목을 입력하세요"><textarea name="content"></textarea>
{'<div class="g-recaptcha">자동입력 방지</div>' if captcha else ''}
<button type="button" onclick="history.back()">취소</button><button type="submit">등록</button></form>"""

    @site.get("/login")
    def login():
        return "<!doctype html><meta charset='utf-8'><input name='id'><input type='password' name='pw'>"

    @site.get(LIST_PATH)
    def board():
        p = posted[-1]
        return f"<!doctype html><meta charset='utf-8'><h1>{p['title']}</h1><pre>{p['content']}</pre>"

    return site


def post_setup(conn, monkeypatch, site):
    pytest.importorskip("playwright")
    monkeypatch.setenv("RADIO_HELPER_CHROMIUM", CHROMIUM)
    monkeypatch.setenv("RADIO_HELPER_HEADLESS", "1")                 # 시험: 로그인 창을 띄우지 않음
    monkeypatch.setattr(autofill, "READY_WAIT_SECONDS", 4)
    monkeypatch.setattr(autofill, "POSTED_WAIT_SECONDS", 10)
    srv = serve(site)
    base = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setattr(autofill, "_allowed_real_url", lambda url: url.startswith(base))
    cid = target_corner_id(conn)
    conn.execute("UPDATE corners SET write_url = ?, title_selector = NULL, body_selector = NULL WHERE id = ?",
                 (f"{base}{WRITE_PATH}?cornerid=3002", cid))
    eid = make_experience(conn, story="첫 출근 날 실수로 사장님 커피를 마셨다. 그날 이후 커피는 꼭 이름표를 붙인다",
                          highlight="", ending="", quotes="", quote_kind="none")
    did = make_draft(conn, eid, cid, title=TITLE, body=BODY, status="approved")
    pid = add_story_post(conn, eid, did)
    return srv, base, cid, did, pid


@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="Chromium 없음")
def test_post_fills_and_presses_register(client, conn, monkeypatch):
    posted: list = []
    srv, base, cid, did, pid = post_setup(conn, monkeypatch, fake_sbs_write(posted))
    try:
        assert autofill.main(["--draft", str(did), "--mode", "post"]) == 0
    finally:
        srv.shutdown()

    # 확인 창까지 눌러 원고 그대로 올라감
    assert [{"title": p["title"], "content": p["content"].replace("\r\n", "\n")} for p in posted] == \
        [{"title": TITLE, "content": BODY}]
    sub = conn.execute("SELECT * FROM submissions").fetchone()
    assert sub["draft_id"] == did and sub["post_status"] == "posted"
    assert sub["post_url"] == f"{base}{LIST_PATH}?cornerid=3002&no=1"
    sp = conn.execute("SELECT * FROM story_posts WHERE id = ?", (pid,)).fetchone()
    assert (sp["status"], sp["sent_via"]) == ("posted", "board") and "등록 확인" in sp["decision"]
    # 화면에서 찾은 입력 요소를 코너에 적어 둠 (검색칸이 아니라 제목칸)
    corner = conn.execute("SELECT * FROM corners WHERE id = ?", (cid,)).fetchone()
    assert (corner["title_selector"], corner["body_selector"]) == ('input[name="title"]', 'textarea[name="content"]')
    shots = conn.execute("SELECT * FROM evidence WHERE item_table = 'submissions' AND item_id = ? ORDER BY id",
                         (sub["id"],)).fetchall()
    assert [s["status"] for s in shots] == ["filled", "posted"] and shots[1]["label"].startswith("등록 뒤 화면")
    assert not (db.data_dir() / "board_post.lock").exists()
    assert (db.data_dir() / "browser_profile").is_dir()
    # 보낸 사연 목록에는 게시판 한 줄로만 (앱 채팅으로 잘못 나오거나 두 번 나오지 않음)
    sent = client.get("/stories/sent").get_data(as_text=True)
    assert sent.count(TITLE) == 1 and "게시판 · " in sent and "자동 등록 중" not in sent

    # 같은 원고·같은 경험은 SBS에 다시 올리지 않음
    assert autofill.main(["--draft", str(did), "--mode", "post"]) == 2
    assert len(posted) == 1


@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="Chromium 없음")
@pytest.mark.parametrize("kw, reason", [({"need_login": True}, "로그인"), ({"captcha": True}, "보안 문자")])
def test_post_never_bypasses_login_or_captcha(conn, monkeypatch, kw, reason):
    posted: list = []
    srv, base, cid, did, pid = post_setup(conn, monkeypatch, fake_sbs_write(posted, **kw))
    try:
        assert autofill.main(["--draft", str(did), "--mode", "post"]) == 1
    finally:
        srv.shutdown()
    assert posted == []
    sp = conn.execute("SELECT * FROM story_posts WHERE id = ?", (pid,)).fetchone()
    assert sp["status"] == "pending" and sp["sent_at"] is None
    assert sp["decision"].startswith("게시판 자동 등록 못 함") and reason in sp["decision"]
    assert conn.execute("SELECT COUNT(*) FROM submissions WHERE post_status != 'failed'").fetchone()[0] == 0
    assert not (db.data_dir() / "board_post.lock").exists()


# ── 메뉴 알림 숫자 '확인함' ─────────────────────────────────────────
def add_pending_quiz(conn, question="오늘의 퀴즈"):
    cur = conn.execute("INSERT INTO quizzes (account, channel, program, broadcast_date, question, question_key, dedupe_key, "
                       "source, entry_status, created_at, updated_at) VALUES ('고릴라', '파워FM', '두시탈출 컬투쇼', "
                       "'2026-10-11', ?, ?, ?, 'auto', 'pending', ?, ?)",
                       (question, question, f"k|{question}", db.now(), db.now()))
    conn.commit()
    return cur.lastrowid


def test_seen_button_clears_menu_count_but_keeps_items(client, conn):
    from radio_helper import notices

    for q in ("퀴즈 하나", "퀴즈 둘", "퀴즈 셋"):
        add_pending_quiz(conn, q)
    eid = make_experience(conn)
    add_story_post(conn, eid, None, status="pending")
    add_story_post(conn, eid, None, status="pending")
    html = client.get("/").get_data(as_text=True)
    assert '<span class="nav-count">3</span>' in html and '<span class="nav-count">2</span>' in html
    assert "✓ 확인함" in html

    token = csrf(client, "/")
    r = client.post("/notices/seen", data={"csrf_token": token, "next": "/quizzes"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/quizzes")
    html = client.get("/").get_data(as_text=True)
    assert "nav-count" not in html and "✓ 확인함" not in html
    assert notices.pending_counts(conn) == {"quizzes": 3, "stories": 2}            # 글은 그대로 (첫 화면 '확인 대기')
    assert client.get("/live.json").get_json()["unseen"] == {"quizzes": 0, "stories": 0}
    assert "퀴즈 셋" in client.get("/quizzes").get_data(as_text=True)

    # 새로 생긴 대기 글만 다시 숫자로
    add_pending_quiz(conn, "새 퀴즈")
    assert notices.unseen_counts(conn) == {"quizzes": 1, "stories": 0}
    html = client.get("/quizzes").get_data(as_text=True)
    assert '<span class="nav-count">1</span>' in html and "✓ 알림 확인함" in html

    # 화면별 '알림 확인함'은 그 종류만
    add_story_post(conn, eid, None, status="pending")
    client.post("/notices/seen", data={"csrf_token": token, "kind": "stories", "next": "/stories#pending"})
    assert notices.unseen_counts(conn) == {"quizzes": 1, "stories": 0}
    # 바깥 주소로는 돌아가지 않음
    r = client.post("/notices/seen", data={"csrf_token": token, "next": "//evil.example.com/"})
    assert r.headers["Location"] in ("/", "http://127.0.0.1:5000/")
