"""0.62: 게시판 작업은 Aside 브라우저로 · 코너 게시판 모으기 (프로그램 홈페이지 메뉴)."""

from datetime import datetime
from types import SimpleNamespace

from conftest import csrf

from radio_helper import aside, autofill, collect, db

ASIDE = r"C:\Users\me\AppData\Local\Aside\Application\aside.exe"


# ── Aside 찾기·열기 ───────────────────────────────────────────────────
def test_find_aside_in_order():
    env = {"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}
    exists = lambda p: p == ASIDE.replace("\\", "/") or p.replace("\\", "/") == ASIDE.replace("\\", "/")   # noqa: E731
    assert aside.find(r"D:\my\aside.exe", exists=lambda p: p == r"D:\my\aside.exe", reg=lambda: None, link="") \
        == r"D:\my\aside.exe"                                                    # 적어 둔 경로가 먼저
    assert aside.find("", exists=lambda p: p == r"E:\reg\aside.exe", env=env, reg=lambda: r"E:\reg\aside.exe",
                      link="") == r"E:\reg\aside.exe"                             # App Paths
    assert aside.find("", exists=exists, env=env, reg=lambda: None, link="") is not None   # 흔한 설치 폴더
    assert aside.find("", exists=lambda p: p == r"F:\x\Aside.exe", env={}, reg=lambda: None, link="Aside.lnk",
                      target=lambda link: r"F:\x\Aside.exe") == r"F:\x\Aside.exe"   # 시작 메뉴 바로 가기
    assert aside.find("", exists=lambda p: True, env={}, reg=lambda: None, link="Aside.lnk",
                      target=lambda link: r"F:\x\chrome.exe") is None             # 다른 프로그램을 가리키면 아님
    assert aside.find("", exists=lambda p: False, env=env, reg=lambda: None, link="") is None


def test_chosen_respects_edge_and_caches(conn):
    calls = []

    def finder(saved):
        calls.append(saved)
        return saved or ASIDE
    assert aside.chosen(conn, finder) == ASIDE and db.get_setting(conn, aside.FOUND_KEY) == ASIDE
    assert aside.chosen(conn, finder) == ASIDE and calls[-1] == ASIDE           # 다음부터는 찾아 둔 경로부터
    db.set_setting(conn, aside.BROWSER_KEY, "edge")
    assert aside.chosen(conn, finder) is None


class FakeChromium:
    def __init__(self, fail_aside=False):
        self.calls, self.fail_aside = [], fail_aside

    def launch_persistent_context(self, path, **kw):
        self.calls.append((path, kw))
        if "executable_path" in kw and self.fail_aside:
            raise RuntimeError("Browser closed unexpectedly\nlog...")
        return SimpleNamespace(pages=[SimpleNamespace(on=lambda *a: None)], new_page=None)


def test_board_browser_opens_aside_profile_then_falls_back_to_edge(data_dir, monkeypatch):
    conn = db.connect()
    db.init_db(conn)
    monkeypatch.setattr(autofill.sys, "platform", "win32")
    monkeypatch.delenv("RADIO_HELPER_CHROMIUM", raising=False)
    monkeypatch.delenv("RADIO_HELPER_BROWSER_CHANNEL", raising=False)
    monkeypatch.setattr(autofill, "no_window", lambda: False)
    monkeypatch.setattr(aside, "find", lambda saved="", **kw: ASIDE)
    db.set_setting(conn, "boards.logins", '{"SBS": {"ok": true}}')                   # Edge 프로필에서 로그인했던 기록
    p = SimpleNamespace(chromium=FakeChromium())
    autofill.launch_profile(p, headless=True)
    assert db.get_setting(conn, "boards.logins") == ""                           # 새 Aside 프로필은 다시 로그인
    from radio_helper import boards
    assert boards.LOGIN_KEY == aside.LOGINS_KEY
    path, kw = p.chromium.calls[0]
    assert kw == {"executable_path": ASIDE, "headless": True} and path.endswith("browser_profile_aside")

    p = SimpleNamespace(chromium=FakeChromium(fail_aside=True))
    autofill.launch_profile(p, headless=True)
    assert p.chromium.calls[1][1] == {"headless": True, "channel": "msedge"}    # 안 열리면 Edge
    assert "Aside 를 열지 못해 Edge 로 엶 — RuntimeError: Browser closed unexpectedly" in \
        db.get_setting(conn, aside.ERROR_KEY)

    db.set_setting(conn, aside.BROWSER_KEY, "edge")
    p = SimpleNamespace(chromium=FakeChromium())
    autofill.launch_profile(p, headless=False)
    assert p.chromium.calls == [(str(autofill.profile_dir()), {"headless": False, "channel": "msedge"})]
    conn.close()


def test_corners_page_browser_setting(client, conn, monkeypatch):
    monkeypatch.setattr(aside, "find", lambda saved="", **kw: saved or None)
    html = client.get("/corners").get_data(as_text=True)
    assert "Aside 못 찾음 → Edge" in html and "코너 게시판 지금 모으기" in html
    r = client.post("/corners/browser", data={"csrf_token": csrf(client, "/corners"), "browser": "auto",
                                              "aside_path": f'"{ASIDE}"'}, follow_redirects=True)
    assert f"Aside({ASIDE})로 엽니다" in r.get_data(as_text=True)
    assert db.get_setting(conn, aside.PATH_KEY) == ASIDE


# ── 코너 게시판 모으기 ────────────────────────────────────────────────
HOME = "https://www.cbs.co.kr/program/home/cbs_P000221"
LINKS = [("https://www.cbs.co.kr/board/list/cbs_P000221_request", "사연과 신청곡"),
         ("https://www.cbs.co.kr/board/list/cbs_P000221_notice", "공지사항"),
         ("https://www.cbs.co.kr/board/list/cbs_P000221_photo", "포토"),
         ("https://www.cbs.co.kr/board/list/cbs_P000221_oldMusic", "선곡표"),
         ("https://www.cbs.co.kr/program/vod/cbs_P000221", "다시듣기"),
         ("https://www.cbs.co.kr/board/list/cbs_P000999_request", "다른 프로그램"),
         ("https://example.com/board/x", "사연 남의 사이트")]


def test_board_links_keep_listener_boards_only():
    got = collect.board_links(HOME, LINKS)
    assert [x["url"] for x in got] == ["https://www.cbs.co.kr/board/list/cbs_P000221_request"]
    mbc = "https://www.imbc.com/broad/radio/fm4u/morningfm/index.html"                  # MBC: 게시판은 m.imbc.com
    got = collect.board_links(mbc, [("https://m.imbc.com/imbbs/imbbslist/1000587100000100000?bid=goodm99", "사연과 신청곡"),
                                    ("https://www.imbc.com/broad/radio/fm4u/morningfm/notice/index.html", "공지사항"),
                                    ("https://m.imbc.com/imbbs/imbbslist/1?bid=x", "")])
    assert [x["text"] for x in got] == ["사연과 신청곡"]
    assert collect.program_tokens(mbc) == ["morningfm"] and collect.program_tokens(HOME) == ["cbs_P000221"]


def test_collect_visits_program_homepages_and_adds_boards(conn):
    conn.execute("INSERT INTO programs (channel, code, title, start_time, end_time, days, main_url, on_air, updated_at) "
                 "VALUES ('CBS 음악FM', 'x1', '허윤희의 꿈과 음악사이에', '22:00', '00:00', '매일', ?, 1, ?)", (HOME, db.now()))
    conn.execute("INSERT INTO programs (channel, code, title, start_time, end_time, days, main_url, on_air, updated_at) "
                 "VALUES ('CBS 음악FM', 'x2', '홈페이지 없음', '20:00', '22:00', '매일', '', 1, ?)", (db.now(),))
    conn.commit()
    seen = []

    def reader(url):
        seen.append(url)
        if url == HOME:
            return LINKS
        raise TimeoutError("timeout")
    out = collect.collect(conn, reader=reader, say=lambda m: None, now=datetime(2026, 10, 22, 11, 0))
    assert HOME in seen and out["added"] == 1
    row = conn.execute("SELECT * FROM corners WHERE board_url LIKE '%cbs_P000221_request'").fetchone()
    assert row["program"] == "허윤희의 꿈과 음악사이에" and row["station"] == "CBS" and row["board_exists"] == 1
    assert "코너 게시판 모으기" in row["dev_note"]
    assert collect.collect(conn, reader=reader, say=lambda m: None)["added"] == 0   # 이미 있는 주소는 그대로
    msg = conn.execute("SELECT message FROM events WHERE message LIKE '코너 게시판 모으기%' ORDER BY id DESC").fetchone()[0]
    assert "새 게시판 0개" in msg


def test_collect_tick_weekly_when_boards_idle(conn, monkeypatch):
    monkeypatch.setattr(collect, "board_busy", lambda: False)
    conn.execute("INSERT INTO programs (channel, code, title, main_url, on_air, updated_at) "
                 "VALUES ('CBS 음악FM', 'x1', '꿈음', ?, 1, ?)", (HOME, db.now()))
    conn.commit()
    launched = []
    now = datetime(2026, 10, 22, 11, 0)
    assert collect.tick(conn, now, launch=launched.append) and launched == [["run"]]
    assert not collect.tick(conn, now, launch=launched.append)                    # 방금 띄움
    db.set_setting(conn, collect.LAST_KEY, "2026-10-22 11:30:00")
    assert not collect.tick(conn, datetime(2026, 10, 25, 11, 0), launch=launched.append)   # 일주일 안
    assert collect.tick(conn, datetime(2026, 10, 30, 12, 0), launch=launched.append)
    monkeypatch.setattr(collect, "board_busy", lambda: True)
    db.set_setting(conn, collect.STARTED_KEY, "")
    db.set_setting(conn, collect.LAST_KEY, "")
    assert not collect.tick(conn, now, launch=launched.append)                    # 게시판 작업 중에는 기다림
