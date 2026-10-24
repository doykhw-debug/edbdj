"""매일 게시판 확인 (0.60): 하루 한 번 모든 코너를 하나씩, 안 올린 게시판·새 공지에만 길게 올리기 · 미리 로그인."""

import json
from datetime import datetime

from conftest import csrf
from test_boards import MBC_URL, FakeAnswerer, funny, notice, page_reader, record_poster

from radio_helper import boards, db
from radio_helper.quizbot import story

MONDAY = datetime(2026, 10, 19, 11, 0)


def add_exp(conn, label, text, line="", ending=""):
    return funny(conn, label=label, story=text, gorilla_line=line, ending=ending)


# ── 길게 ──────────────────────────────────────────────────────────────
def test_board_uses_full_story_not_chat_line(conn):
    eid = add_exp(conn, "편집", "주말에 영상을 편집하는데 첫째가 옆에서 한참 봤어요. 그날 이후 편집할 때마다 옆에 앉아요.",
                  line="첫째가 편집할 때마다 옆에 앉아요")
    exp = conn.execute("SELECT * FROM experiences WHERE id = ?", (eid,)).fetchone()
    assert story.approved_text(conn, exp, 2000) == "첫째가 편집할 때마다 옆에 앉아요"            # 채팅: 한 줄
    board = story.approved_text(conn, exp, 2000, board=True)
    assert board.startswith("주말에 영상을 편집하는데") and len(board) > 40                     # 게시판: 본문
    assert boards.letter("본문", 2000) == "안녕하세요.\n\n본문\n\n늘 감사합니다."
    assert boards.letter("가" * 95, 100) == "가" * 95                                           # 글자 수를 넘으면 본문만


# ── 같은 공지에는 다시 안 올림 ─────────────────────────────────────────
def test_same_notice_tolerates_rewording_but_sees_new_ones():
    old = {"notice": "매일 오후 2시 오프닝 한마디를 보내 주세요. 짧고 웃긴 이야기 환영합니다.", "theme": "웃긴 한마디",
           "deadline": ""}
    reworded = dict(old, notice="매일 오후 2시 오프닝 한마디를 보내주세요. 짧고 웃긴 이야기 환영!", theme="짧고 웃긴 한마디")
    assert boards.same_notice(old, reworded)                                     # Claude 가 조금 다르게 옮겨도 같은 공지
    assert not boards.same_notice(old, dict(old, notice="이번 주 주제는 가을 소풍 추억입니다. 사진은 받지 않습니다."))
    assert not boards.same_notice(dict(old, deadline="2026-10-20"), dict(old, deadline="2026-10-27"))
    assert boards.same_notice({"notice": "", "theme": "가을 소풍 추억"}, {"notice": "", "theme": "가을 소풍의 추억"})


def test_post_once_per_notice_then_again_on_new_notice(conn):
    cid = boards.add_corner(conn, MBC_URL)
    a = add_exp(conn, "편집", "주말에 영상을 편집하는데 첫째가 옆에서 한참 봤어요.")
    b = add_exp(conn, "소풍", "가을 소풍 때 도시락을 잊고 가서 친구 김밥을 나눠 먹었어요.")
    calls = []
    fake = FakeAnswerer(note=notice(kind="story"), pick=lambda c: {"experience_id": c[0]["id"], "title": "제목", "line": "",
                                                                     "reason": "맞음"})
    text = boards.run(conn, cid, test=False, answerer=fake, reader=page_reader(), poster=record_poster(calls, "posted"),
                      now=MONDAY)
    assert "등록 확인" in text and calls[0]["draft"]["experience_id"] == a
    assert json.loads(boards.corner_row(conn, cid)["posted_notice"])["test"] is False
    text = boards.run(conn, cid, test=False, answerer=fake, reader=page_reader(), poster=record_poster(calls, "posted"),
                      now=MONDAY)
    assert "지금 공지로 이미 올렸습니다" in text and len(calls) == 1               # 다음 날 같은 공지 → 안 올림
    fake.note = notice(kind="story", notice="이번 주 주제: 가을 소풍 추억을 보내 주세요", theme="가을 소풍")
    text = boards.run(conn, cid, test=False, answerer=fake, reader=page_reader(), poster=record_poster(calls, "posted"),
                      now=MONDAY)
    assert "등록 확인" in text and calls[1]["draft"]["experience_id"] == b      # 새 공지 → 다른 경험으로 올림


def test_test_fill_once_then_real_mode_posts(conn):
    cid = boards.add_corner(conn, MBC_URL)
    add_exp(conn, "편집", "주말에 영상을 편집하는데 첫째가 옆에서 한참 봤어요.")
    calls = []
    fake = FakeAnswerer(note=notice(kind="story"))
    boards.run(conn, cid, test=True, answerer=fake, reader=page_reader(), poster=record_poster(calls, "test"), now=MONDAY)
    text = boards.run(conn, cid, test=True, answerer=fake, reader=page_reader(), poster=record_poster(calls, "test"),
                      now=MONDAY)
    assert "이미 시험 입력했습니다" in text and len(calls) == 1                     # 시험도 같은 공지에 한 번
    text = boards.run(conn, cid, test=False, answerer=fake, reader=page_reader(), poster=record_poster(calls, "posted"),
                      now=MONDAY)
    assert "등록 확인" in text and len(calls) == 2                                  # '등록까지'로 바꾸면 실제로 올림


def test_board_posted_by_older_version_counts_as_posted(conn):
    cid = boards.add_corner(conn, MBC_URL)
    eid = add_exp(conn, "편집", "주말에 영상을 편집하는데 첫째가 옆에서 한참 봤어요.")
    conn.execute("INSERT INTO submissions (draft_id, experience_id, corner_id, post_status, created_at, updated_at) "
                 "VALUES (NULL, ?, ?, 'posted', '2026-10-10 10:00:00', ?)", (eid, cid, db.now()))
    conn.commit()
    calls = []
    text = boards.run(conn, cid, test=False, answerer=FakeAnswerer(note=notice(kind="story")), reader=page_reader(),
                      poster=record_poster(calls), now=MONDAY)
    assert "이미 2026-10-10에 올린 게시판" in text and calls == []


# ── 하루 한 번, 하나씩 ────────────────────────────────────────────────
def test_daily_check_launches_corners_one_at_a_time(conn):
    db.set_setting(conn, "boards.aside_rules", "0")     # 예전 규칙으로 (0.70 Aside 규칙 끔)
    a = boards.add_corner(conn, MBC_URL)
    b = boards.add_corner(conn, MBC_URL.replace("76233", "76299"))
    others = [r["id"] for r in conn.execute("SELECT id FROM corners WHERE id NOT IN (?, ?)", (a, b))]
    if others:
        conn.execute(f"UPDATE corners SET board_exists = 0 WHERE id IN ({','.join('?' * len(others))})", others)
        conn.commit()
    launched = []
    launch = lambda args: launched.append(args) or "boards_x.log"   # noqa: E731
    assert boards.tick(conn, MONDAY, launch) == []                               # 매일 확인이 꺼져 있음
    db.set_setting(conn, boards.DAILY_KEY, "1")
    assert boards.tick(conn, MONDAY.replace(hour=10, minute=0), launch) == []    # 10:30 전
    assert boards.tick(conn, MONDAY, launch) == [a]
    assert boards.tick(conn, MONDAY.replace(second=30), launch) == []           # 막 띄움 → 1분은 기다림
    (db.data_dir() / boards.BOARD_LOCK).write_text(str(__import__("os").getpid()), encoding="ascii")
    assert boards.tick(conn, MONDAY.replace(minute=5), launch) == []            # 게시판 작업 중 → 다음 차례
    (db.data_dir() / boards.BOARD_LOCK).unlink()
    assert boards.tick(conn, MONDAY.replace(minute=5), launch) == [b]
    assert boards.tick(conn, MONDAY.replace(minute=10), launch) == []           # 오늘은 둘 다 함
    assert launched == [["run", "--corner", str(a), "--scheduled"], ["run", "--corner", str(b), "--scheduled"]]
    assert boards.tick(conn, MONDAY.replace(day=20), launch) == [a]             # 다음 날 다시


def test_scheduled_run_keeps_daily_cap_and_never_waits_for_login(conn):
    db.set_setting(conn, "boards.aside_rules", "0")     # 예전 규칙으로 (0.70 Aside 규칙 끔)
    cid = boards.add_corner(conn, MBC_URL)
    eid = add_exp(conn, "편집", "주말에 영상을 편집하는데 첫째가 옆에서 한참 봤어요.")
    db.set_setting(conn, boards.DAILY_CAP_KEY, "1")
    conn.execute("INSERT INTO submissions (draft_id, experience_id, corner_id, post_status, note, created_at, updated_at) "
                 "VALUES (NULL, ?, ?, 'test', '게시판 자동 등록 시험: …', ?, ?)",
                 (eid, cid, MONDAY.strftime("%Y-%m-%d 09:00:00"), db.now()))
    conn.commit()
    text = boards.run(conn, cid, answerer=FakeAnswerer(note=notice(kind="story")), reader=page_reader(),
                      poster=record_poster([]), now=MONDAY, scheduled=True)
    assert "오늘 자동 게시판 글 1건을 채워" in text
    db.set_setting(conn, boards.DAILY_CAP_KEY, "5")
    seen = {}

    def needs_login(conn_, draft, corner, allow_window, dry_run):
        from radio_helper import autofill

        seen["allow_window"] = allow_window
        raise autofill.AutofillError("MBC 로그인(또는 보안 문자)이 필요해 창을 띄워야 합니다.")
    text = boards.run(conn, cid, answerer=FakeAnswerer(note=notice(kind="story")), reader=page_reader(),
                      poster=needs_login, now=MONDAY, scheduled=True)
    assert seen["allow_window"] is False and "로그인" in text                    # 창을 띄워 기다리지 않음
    assert boards.logins(conn)["MBC"]["ok"] is False                             # 코너 화면에 '로그인 필요'
    db.set_setting(conn, boards.DAILY_CAP_KEY, "0")                               # 0 = 제한 없음
    posts = []
    boards.run(conn, cid, answerer=FakeAnswerer(note=notice(kind="story")), reader=page_reader(),
               poster=record_poster(posts), now=MONDAY, scheduled=True)
    assert posts


# ── 로그인 확인 ───────────────────────────────────────────────────────
class FakePage:
    def __init__(self, text="", password=False, form=False):
        self.text, self.password, self.form = text, password, form

    def wait_for_load_state(self, *a, **k):
        pass

    def locator(self, sel):
        return type("L", (), {"count": lambda _s: 1 if (self.password and "password" in sel) else 0})()


def test_login_state_reads_the_page(monkeypatch):
    from radio_helper import autofill

    monkeypatch.setattr(autofill, "guess_selectors", lambda page: {"title": "#t" if page.form else None,
                                                                   "body": "#b" if page.form else None})
    monkeypatch.setattr(autofill, "page_text", lambda page: page.text)
    assert boards.login_state(FakePage(form=True))[0] is True
    assert boards.login_state(FakePage(password=True)) == (False, "로그인 화면이 뜸")
    assert boards.login_state(FakePage("홈 로그아웃 마이페이지"))[0] is True
    assert boards.login_state(FakePage("홈 로그인 회원가입"))[0] is False
    assert boards.login_state(FakePage("홈"))[0] is None


def test_login_targets_one_per_station(conn):
    boards.add_corner(conn, MBC_URL)
    boards.add_corner(conn, MBC_URL.replace("76233", "76299"))
    targets = {t["station"]: t for t in boards.login_targets(conn)}
    assert "MBC" in targets and targets["MBC"]["url"].startswith("https://") and targets["MBC"]["home"] == boards.HOMES["MBC"]


# ── 화면 ──────────────────────────────────────────────────────────────
def test_corners_page_daily_panel_and_buttons(client, conn, monkeypatch):
    from radio_helper import app as app_module

    launched = []
    monkeypatch.setattr(app_module, "launch_module", lambda module, args, prefix: launched.append(args) or "boards_x.log")
    assert boards.daily(conn)["on"]                                              # 관리 화면을 켜면 한 번 켜짐 (시험 모드)
    assert boards.daily(conn)["test"] and boards.daily(conn)["cap"] == 5
    boards.add_corner(conn, MBC_URL)
    html = client.get("/corners").get_data(as_text=True)
    assert "매일 게시판 확인" in html and "미리 로그인" in html and "시험 (입력만)" in html and "MBC" in html
    token = csrf(client, "/corners")
    client.post("/corners/daily", data={"csrf_token": token, "on": "1", "time": "09:15", "cap": "3"})
    d = boards.daily(conn)
    assert (d["on"], d["time"], d["test"], d["cap"]) == (True, "09:15", False, 3)   # 시험 체크를 끄면 등록까지
    client.post("/corners/login", data={"csrf_token": token})
    client.post("/corners/login-check", data={"csrf_token": token})
    assert launched == [["login"], ["login-check"]]
    boards.mark_login(conn, "MBC", True, "글쓰기 화면이 열림")
    assert "로그인됨" in client.get("/corners").get_data(as_text=True)


def test_shared_greeting_does_not_make_different_stories_look_alike():
    from radio_helper import checks

    a = boards.letter("주말에 영상을 편집하는데 첫째가 옆에서 한참 봤어요.", 2000)
    b = boards.letter("가을 소풍 때 도시락을 잊고 가서 친구 김밥을 나눠 먹었어요.", 2000)
    assert checks.check_similarity(b, [a]) == []
    assert checks.check_similarity(boards.letter("주말에 영상을 편집하는데 첫째가 옆에서 한참 봤어요!", 2000), [a])   # 같은 사연은 그대로 잡음
