"""0.64: 내 Aside 새 탭으로 게시판 읽기·쓰기 (확장 다리) · 모으기가 '글 하나' 주소를 게시판으로 넣지 않게."""

import json

import pytest

from radio_helper import boards, collect, db, tabbridge

BOARD = "https://programs.sbs.co.kr/radio/testshow/boards/1"


def headers(conn):
    return {"X-Radio-Helper-Token": tabbridge.token(conn), "Content-Type": "application/json"}


# ── 확장 폴더 ─────────────────────────────────────────────────────────
def test_extension_files(conn, tmp_path, monkeypatch):
    monkeypatch.setenv("PORT", "5077")
    folder = tabbridge.write_extension(conn, tmp_path / "ext")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3 and "http://127.0.0.1:5077/*" in manifest["host_permissions"]
    assert all(h.startswith("https://") for h in manifest["host_permissions"][1:])          # 방송국 게시판만
    assert [c["js"] for c in manifest["content_scripts"]] == [["page.js"], ["content.js"]]
    bg = (folder / "background.js").read_text(encoding="utf-8")
    assert tabbridge.token(conn) in bg and "http://127.0.0.1:5077" in bg
    content = (folder / "content.js").read_text(encoding="utf-8")
    assert content.count("type=password") == 1 and "password]\").value" not in content   # 로그인 화면인지만 보고 값은 안 읽음


# ── 다리 주소 ─────────────────────────────────────────────────────────
def test_bridge_needs_token_and_local(client, conn):
    assert client.post("/bridge/ping", json={}).status_code == 403                         # 열쇠 없음
    assert client.post("/bridge/ping", json={}, headers={"X-Radio-Helper-Token": "x"}).status_code == 403
    r = client.post("/bridge/ping", json={"version": "1.0.0"}, headers=headers(conn))
    assert r.status_code == 200 and tabbridge.connected(conn)
    remote = client.post("/bridge/ping", json={}, headers={**headers(conn), "Cf-Connecting-Ip": "1.2.3.4"},
                         environ_base={"HTTP_HOST": "radio.haetsallab.com"})
    assert remote.status_code in (403, 404)                                                 # 밖에서는 안 됨


def test_task_report_wait_flow(client, conn):
    tid = tabbridge.add_task(conn, "write", BOARD, {"title": "가을", "body": "본문", "dry_run": True})
    assert client.post("/bridge/task", json={"url": "https://www.imbc.com/x"}, headers=headers(conn)).get_json() == {}
    got = client.post("/bridge/task", json={"url": BOARD}, headers=headers(conn)).get_json()
    assert got["id"] == tid and got["title"] == "가을" and got["created_ms"] > 0
    r = client.post("/bridge/report", json={"id": tid, "step": "to_write"}, headers=headers(conn)).get_json()
    assert r == {"ok": True, "close": False}
    client.post("/bridge/report", json={"id": tid, "status": "login"}, headers=headers(conn))
    assert tabbridge.task(conn, tid)["status"] == "login"
    r = client.post("/bridge/report", json={"id": tid, "status": "done", "step": "filled"}, headers=headers(conn)).get_json()
    assert r["close"] is True
    assert tabbridge.wait(conn, tid, 1, sleep=lambda s: None)["step"] == "filled"
    assert client.post("/bridge/report", json={"id": tid, "status": "done"}, headers=headers(conn)).get_json()["ok"] is False


def test_wait_times_out_and_waits_longer_on_login(conn):
    t = [0.0]
    clock, sleep = (lambda: t[0]), (lambda s: t.__setitem__(0, t[0] + s))
    tid = tabbridge.add_task(conn, "read", BOARD, now=0)
    assert tabbridge.wait(conn, tid, 5, sleep=sleep, clock=clock)["status"] == "expired"
    tid = tabbridge.add_task(conn, "write", BOARD, now=t[0])
    tabbridge.report(conn, tid, {"status": "login"})
    start = t[0]
    assert tabbridge.wait(conn, tid, 5, sleep=sleep, clock=clock)["status"] == "login_timeout"
    assert t[0] - start >= tabbridge.LOGIN_WAIT                                             # 로그인은 10분까지 기다림


def test_new_task_folds_old_ones(conn):
    a = tabbridge.add_task(conn, "read", BOARD)
    b = tabbridge.add_task(conn, "read", BOARD)
    assert tabbridge.task(conn, a)["status"] == "expired" and tabbridge.task_for(conn, BOARD)["id"] == b


# ── boards.run 에 끼우기 ───────────────────────────────────────────────
def extension(conn, result):
    """가짜 확장: 탭이 열리면 가장 최근 작업에 result 를 알려 준다."""
    def opener(url):
        t = tabbridge.task_for(conn, url)
        tabbridge.report(conn, t["id"], result(t) if callable(result) else result)
    return opener


def test_read_page_through_tab(data_dir):
    conn = db.connect()
    db.init_db(conn)
    corner = {"board_url": BOARD}
    got = tabbridge.read_page(corner, opener=extension(conn, {"status": "done", "step": "read", "text": "공지: 사연 받습니다",
                                                              "rules": "욕설 금지",
                                                              "links": [["https://programs.sbs.co.kr/radio/testshow/boards/2",
                                                                         "코너"]],
                                                              "write_url": None}),
                              waiter=lambda c, tid, s: tabbridge.wait(c, tid, 1, sleep=lambda x: None))
    assert "공지: 사연 받습니다" in got["text"] and "[게시판 운영지침]\n욕설 금지" in got["text"]
    assert [x["url"] for x in got["links"]] == ["https://programs.sbs.co.kr/radio/testshow/boards/2"]
    conn.close()


def _draft_corner(conn):
    cid = boards.add_corner(conn, BOARD)
    eid = conn.execute("INSERT INTO experiences (story, created_at, updated_at) VALUES ('x', ?, ?)",
                       (db.now(), db.now())).lastrowid
    did = conn.execute("INSERT INTO drafts (experience_id, corner_id, title, body, status, created_at, updated_at) "
                       "VALUES (?, ?, '가을 이야기', '본문', 'approved', ?, ?)", (eid, cid, db.now(), db.now())).lastrowid
    conn.commit()
    return conn.execute("SELECT * FROM drafts WHERE id = ?", (did,)).fetchone(), boards.corner_row(conn, cid)


def test_post_through_tab_records_submission(conn):
    draft, corner = _draft_corner(conn)
    wait = lambda c, tid, s: tabbridge.wait(c, tid, 1, sleep=lambda x: None)   # noqa: E731
    sid = tabbridge.post(conn, draft, corner, dry_run=True, opener=extension(conn, {"status": "done", "step": "filled"}),
                         waiter=wait)
    assert conn.execute("SELECT post_status FROM submissions WHERE id = ?", (sid,)).fetchone()[0] == "test"
    sid = tabbridge.post(conn, draft, corner, opener=extension(conn, {"status": "done", "step": "verified",
                                                                     "after_text": "목록 가을 이야기 ...",
                                                                     "after_url": BOARD}), waiter=wait)
    row = conn.execute("SELECT * FROM submissions WHERE id = ?", (sid,)).fetchone()
    assert row["post_status"] == "posted" and row["post_url"] == BOARD and "내 Aside 새 탭" in row["note"]
    sid = tabbridge.post(conn, draft, corner, opener=extension(conn, {"status": "done", "step": "verified",
                                                                     "after_text": "다른 글"}), waiter=wait)
    assert conn.execute("SELECT post_status FROM submissions WHERE id = ?", (sid,)).fetchone()[0] == "unknown"
    from radio_helper import autofill
    import pytest
    with pytest.raises(autofill.AutofillError, match="응답하지 않았습니다"):
        tabbridge.post(conn, draft, corner, opener=lambda url: None, waiter=wait)


def test_board_run_uses_my_aside_tabs_when_connected(conn, monkeypatch):
    cid = boards.add_corner(conn, BOARD)
    used = []
    monkeypatch.setattr(boards, "_run", lambda conn_, cid_, test, answerer, now, reader, poster, aw, sched:
                        used.append((reader, poster)) or "ok")
    boards.run(conn, cid)
    assert used[-1] == (boards.read_page, None)                                            # 연결 전: 예전처럼
    db.set_setting(conn, tabbridge.PING_KEY, str(__import__("time").time()))
    boards.run(conn, cid)
    assert used[-1] == (tabbridge.read_page, tabbridge.post)                               # 연결되면 내 Aside 새 탭
    db.set_setting(conn, "boards.browser", "edge")
    boards.run(conn, cid)
    assert used[-1] == (boards.read_page, None)                                            # Edge 를 고르면 예전처럼


def test_login_check_through_tabs(conn):
    out = boards.check_logins_tabs(conn, [{"station": "CBS", "url": "https://www.cbs.co.kr/board/list/cbs_P1_request"},
                                          {"station": "SBS", "url": BOARD}],
                                   say=lambda m: None,
                                   reader=lambda c, url: {"text": "라디오 연서해피아빠 님 편성표"} if "cbs" in url
                                   else {"text": "로그인 회원가입", "password": True})
    assert out["CBS"]["ok"] is True and out["SBS"]["ok"] is False


# ── 모으기: 글 하나 주소 빼기 ──────────────────────────────────────────
def test_post_view_links_are_not_boards():
    home = "https://www.cbs.co.kr/program/home/cbs_P000221"
    got = collect.board_links(home, [
        ("https://www.cbs.co.kr/board/list/cbs_P000221_request", "사연과 신청곡"),
        ("https://www.cbs.co.kr/board/view/cbs_P000221_request?no=12345", "오늘 들은 노래 사연"),
        ("https://www.cbs.co.kr/board/list/cbs_P000221_request?page=2", "2"),
        ("https://programs.sbs.co.kr/radio/x/boards/1?cmd=view&board_no=99", "사연"),
        ("https://m.imbc.com/imbbs/imbbsview?bid=x&list_id=1", "사연 글")])
    assert [x["url"] for x in got] == ["https://www.cbs.co.kr/board/list/cbs_P000221_request"]


def test_cleanup_removes_collected_posts(conn):
    keep = collect.add(conn, "https://www.cbs.co.kr/board/list/cbs_P000221_request", "사연과 신청곡", "꿈음", "모으기")
    assert not collect.add(conn, "https://www.cbs.co.kr/board/view/cbs_P000221_request?no=9", "남의 글", "꿈음", "모으기")
    for no, title in ((1, "어떤 글"), (2, "보낸 글")):          # 0.63 이 넣어 둔 것 (지금은 add 가 거부)
        conn.execute("INSERT INTO corners (program, kind, title, board_url, write_url, station, updated_at) "
                     "VALUES ('꿈음', 'board', ?, ?, ?, 'CBS', ?)",
                     (title, f"https://www.cbs.co.kr/board/view/cbs_P000221_request?no={no}",
                      f"https://www.cbs.co.kr/board/view/cbs_P000221_request?no={no}", db.now()))
    sent = conn.execute("SELECT id FROM corners WHERE board_url LIKE '%no=2'").fetchone()[0]
    eid = conn.execute("INSERT INTO experiences (story, created_at, updated_at) VALUES ('x', ?, ?)",
                       (db.now(), db.now())).lastrowid
    conn.execute("INSERT INTO submissions (experience_id, corner_id, post_status, created_at, updated_at) "
                 "VALUES (?, ?, 'posted', ?, ?)", (eid, sent, db.now(), db.now()))
    conn.commit()
    assert keep and collect.cleanup_posts(conn) == 1                                        # 보낸 적 있는 것은 둠
    urls = [r[0] for r in conn.execute("SELECT board_url FROM corners WHERE board_url LIKE '%cbs_P000221%'")]
    assert "https://www.cbs.co.kr/board/view/cbs_P000221_request?no=1" not in urls and len(urls) == 2
    assert conn.execute("SELECT board_exists FROM corners WHERE id = ?", (sent,)).fetchone()[0] == 0   # 남겨도 다시 안 엶



# ── 0.69: 다른 사용자가 쓴 글·권한 없는 화면은 열지 않음 ─────────────────
@pytest.mark.parametrize("url, post", [
    ("https://www.cbs.co.kr/board/list/cbs_P000221_request", False),
    ("https://programs.sbs.co.kr/radio/0chulpowerfm/cornerboards/57577?cornerid=3002", False),
    ("https://program.kbs.co.kr/2fm/radio/mrradio/pc/board.html?bbs_loc=R1,list,none,1,0", False),
    ("https://m.imbc.com/TalkBBS/TalkList/1006462100000100000?bid=3446&menu=mn-daily", False),
    ("https://www.cbs.co.kr/board/view/cbs_P000221_request?no=123", True),
    ("https://programs.sbs.co.kr/radio/cultwoshow/boards/58047?cmd=view&board_no=99", True),
    ("https://m.imbc.com/imbbs/imbbsview?bid=x&list_id=1", True),
    ("https://program.kbs.co.kr/2fm/radio/mrradio/pc/board.html?bbs_loc=R1,read,none,123", True),
    ("https://www.imbc.com/broad/radio/fm4u/x/article/12345", True),
    ("https://www.cbs.co.kr/board/list/x?bno=77", True),
])
def test_post_urls(url, post):
    from radio_helper import stations

    assert stations.is_post_url(url) is post


def test_denied_page_but_not_login_or_private_board():
    from radio_helper import stations

    assert stations.denied_page("해당 게시물에 대한 열람 권한이 없습니다.")
    assert stations.denied_page("비밀글입니다. 작성자와 관리자만 볼 수 있습니다.")
    assert not stations.denied_page("권한이 없습니다. 로그인 후 이용해 주세요.")             # 로그인 문제 → 닫지 않음
    assert not stations.denied_page("사연과 신청곡 비공개 게시판 작성자 본인과 관리자만 볼 수 있는 공간입니다. 글쓰기")


def test_run_never_opens_others_posts_and_closes_denied_boards(conn):
    from test_boards import FakeAnswerer, notice

    read = []
    conn.execute("INSERT INTO corners (program, kind, title, board_url, write_url, station, updated_at) "
                 "VALUES ('꿈음', 'board', '남의 글', 'https://www.cbs.co.kr/board/view/cbs_P1_request?no=5', NULL, 'CBS', ?)",
                 (db.now(),))
    post_id = conn.execute("SELECT id FROM corners WHERE board_url LIKE '%no=5'").fetchone()[0]
    text = boards.run(conn, post_id, test=True, answerer=FakeAnswerer(note=notice()),
                      reader=lambda c: read.append(c["board_url"]) or {"text": "", "links": [], "write_url": None})
    assert read == [] and "다시 열지 않습니다" in text
    assert conn.execute("SELECT board_exists FROM corners WHERE id = ?", (post_id,)).fetchone()[0] == 0

    cid = boards.add_corner(conn, "https://www.cbs.co.kr/board/list/cbs_P2_request")
    text = boards.run(conn, cid, test=True, answerer=FakeAnswerer(note=notice()),
                      reader=lambda c: {"text": "열람 권한이 없습니다.", "links": [], "write_url": None})
    assert "권한이 없는 화면" in text
    assert conn.execute("SELECT board_exists FROM corners WHERE id = ?", (cid,)).fetchone()[0] == 0
    with pytest.raises(ValueError, match="글 하나"):
        boards.add_corner(conn, "https://www.cbs.co.kr/board/view/cbs_P2_request?no=1")


def test_same_program_links_skip_posts():
    got = boards.same_program_links("https://www.cbs.co.kr/board/list/cbs_P3_request", [
        ("https://www.cbs.co.kr/board/list/cbs_P3_corner", "코너"),
        ("https://www.cbs.co.kr/board/view/cbs_P3_request?no=1", "남의 사연")])
    assert [x["url"] for x in got] == ["https://www.cbs.co.kr/board/list/cbs_P3_corner"]
