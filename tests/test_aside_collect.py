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
    out = collect.collect(conn, reader=reader, say=lambda m: None, now=datetime(2026, 10, 22, 11, 0),
                          hub_reader=lambda *a: [])
    assert HOME in seen and out["added"] == 1
    row = conn.execute("SELECT * FROM corners WHERE board_url LIKE '%cbs_P000221_request'").fetchone()
    assert row["program"] == "허윤희의 꿈과 음악사이에" and row["station"] == "CBS" and row["board_exists"] == 1
    assert "코너 게시판 모으기" in row["dev_note"]
    assert collect.collect(conn, reader=reader, say=lambda m: None, hub_reader=lambda *a: [])["added"] == 0   # 이미 있는 주소는 그대로
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


# ── CBS 라디오 화면 (사용자가 보내 준 화면, 2026-10-22) ─────────────────
HUB_LINKS = [("https://www.cbs.co.kr/program/home/cbs_P000257", "유지수의 해피송"),
             ("https://www.cbs.co.kr/program/home/cbs_P000554", "하효진의 라이키팝 ON AIR"),
             ("https://www.cbs.co.kr/program/home/cbs_P000554", ""),                 # 그림 링크 (같은 프로그램)
             ("https://www.cbs.co.kr/schedule?type=standardFm", "편성표"),
             ("https://www.cbs.co.kr/radio/program/home/cbs_P000269", "박재홍의 한판승부")]
LIKEY_MENU = [("https://www.cbs.co.kr/program/home/cbs_P000554", "홈"),
              ("https://www.cbs.co.kr/board/list/cbs_P000554_notice", "공지사항"),
              ("https://www.cbs.co.kr/program/playlist/cbs_P000554", "선곡표"),
              ("https://www.cbs.co.kr/board/list/cbs_P000554_request", "사연과 신청곡"),
              ("https://www.cbs.co.kr/board/list/cbs_P000554_chart", "수 차트복권")]


def test_hub_programs_from_cbs_radio_page():
    got = collect.hub_programs("CBS", HUB_LINKS)
    assert [(x["code"], x["program"]) for x in got] == [("cbs_P000257", "유지수의 해피송"),
                                                       ("cbs_P000554", "하효진의 라이키팝"),
                                                       ("cbs_P000269", "박재홍의 한판승부")]
    assert collect.HUBS["CBS"]["url"] == "https://www.cbs.co.kr/radio"


def test_collect_starts_from_cbs_radio_page(conn):
    conn.execute("INSERT INTO programs (channel, code, title, start_time, end_time, days, main_url, on_air, updated_at) "
                 "VALUES ('CBS 표준FM', 'lk', '하효진의 라이키팝', '16:05', '17:00', '월~금', '', 1, ?)", (db.now(),))
    conn.commit()
    tabs_seen = []

    def hub_reader(url, tabs, expand=False):
        tabs_seen.append((url, tabs))
        return HUB_LINKS if "cbs" in url else []

    def reader(url):
        return LIKEY_MENU if url.endswith("cbs_P000554") else []
    out = collect.collect(conn, reader=reader, hub_reader=hub_reader, say=lambda m: None)
    assert ("https://www.cbs.co.kr/radio", ["표준FM", "음악FM", "JOY4U"]) in tabs_seen and out["hub"] == 3
    assert ("https://www.sbs.co.kr/radio?div=gnb_pc", ["파워FM", "러브FM", "고릴라M"]) in tabs_seen
    assert ("https://www.imbc.com/broad/radio/", []) in tabs_seen
    rows = {r["board_url"]: r for r in conn.execute("SELECT * FROM corners WHERE program = '하효진의 라이키팝'")}
    assert set(rows) == {"https://www.cbs.co.kr/board/list/cbs_P000554_request",
                         "https://www.cbs.co.kr/board/list/cbs_P000554_chart"}                  # 사연과 신청곡 + 요일별 코너
    assert conn.execute("SELECT main_url FROM programs WHERE code = 'lk'").fetchone()[0] \
        == "https://www.cbs.co.kr/program/home/cbs_P000554"                                    # 편성표에 홈페이지 채움


def test_rules_button_and_nickname_login():
    from radio_helper import boards

    assert boards.RULES_LINK.match("게시판 운영지침") and boards.RULES_LINK.match("! 게시판 운영지침")
    assert not boards.RULES_LINK.match("글쓰기")
    assert boards.HOMES["CBS"] == "https://www.cbs.co.kr/radio"


def test_cbs_header_nickname_means_logged_in(monkeypatch):
    from test_board_daily import FakePage

    from radio_helper import boards

    monkeypatch.setattr(autofill, "guess_selectors", lambda page: {"title": None, "body": None})
    monkeypatch.setattr(autofill, "page_text", lambda page: page.text)
    header = "CBS\nTV\n라디오\n선교\n디지털콘텐츠\n뉴스\n온에어\n연서해피아빠 님\n편성표\n더보기\n표준FM 음악FM"
    assert boards.login_state(FakePage(header)) == (True, "'연서해피아빠 님'이 보임")
    assert boards.login_state(FakePage("CBS 라디오 온에어 로그인 편성표 청취자님 사연"))[0] is False


def test_hub_programs_from_sbs_and_mbc_radio_pages():
    sbs = collect.hub_programs("SBS", [("https://programs.sbs.co.kr/radio/kingcastlepower/main?div=radio", "황제성의 황제파워"),
                                       ("https://programs.sbs.co.kr/radio/kingcastlepower/main", ""),
                                       ("https://programs.sbs.co.kr/radio/ten/main", "배성재의 텐"),
                                       ("https://www.sbs.co.kr/schedule/index.html", "편성표")])
    assert [(x["program"], x["url"]) for x in sbs] == [
        ("황제성의 황제파워", "https://programs.sbs.co.kr/radio/kingcastlepower/main"),
        ("배성재의 텐", "https://programs.sbs.co.kr/radio/ten/main")]
    mbc = collect.hub_programs("MBC", [("https://www.imbc.com/broad/radio/fm4u/perfect/index.html", "완벽한 하루 이상순입니다"),
                                       ("https://www.imbc.com/broad/radio/fm/look/", "조승원의 뉴스하이킥"),
                                       ("https://www.imbc.com/broad/radio/notice/3900708_2998.html", "알립니다"),
                                       ("https://www.imbc.com/broad/radio/fm4u/schedule/", "편성표")])
    assert [x["url"] for x in mbc] == ["https://www.imbc.com/broad/radio/fm4u/perfect/",
                                       "https://www.imbc.com/broad/radio/fm/look/"]
    assert collect.same_code("https://www.imbc.com/broad/radio/fm4u/perfect/index.html") == \
        collect.same_code("https://www.imbc.com/broad/radio/fm4u/perfect/")
    assert collect.same_code("https://programs.sbs.co.kr/radio/ten/main") == \
        collect.same_code("https://programs.sbs.co.kr/radio/ten")


def test_program_menus_sbs_and_mbc():
    sbs_home = "https://programs.sbs.co.kr/radio/kingcastlepower/main"
    got = collect.board_links(sbs_home, [
        ("https://programs.sbs.co.kr/radio/kingcastlepower/about", "프로그램 소개"),
        ("https://programs.sbs.co.kr/radio/kingcastlepower/musiclist", "라디오 선곡표"),
        ("https://programs.sbs.co.kr/radio/kingcastlepower/boards/74226", "황제의 전당"),
        ("https://programs.sbs.co.kr/radio/kingcastlepower/boards/88001", "황제파워 AI 로고송 모집"),
        ("https://programs.sbs.co.kr/radio/kingcastlepower/boards/74230", "참여해주세요"),
        ("https://programs.sbs.co.kr/radio/kingcastlepower/vods/1", "다시듣기/팟캐스트")])
    assert [x["text"] for x in got] == ["황제의 전당", "황제파워 AI 로고송 모집", "참여해주세요"]
    mbc_home = "https://www.imbc.com/broad/radio/fm4u/perfect/"
    got = collect.board_links(mbc_home, [
        ("https://mini.imbc.com/v2/message", "mini 메시지"),
        ("https://www.imbc.com/broad/radio/fm4u/perfect/musiclist/", "선곡표"),
        ("https://www.imbc.com/broad/radio/fm4u/perfect/requestsong/", "사연과 신청곡"),
        ("https://m.imbc.com/imbbs/imbbslist/1001?bid=perfect01&menu=mn-daily", "청라이브"),
        ("https://www.instagram.com/x", "인스타그램")])
    assert [x["text"] for x in got] == ["사연과 신청곡", "청라이브"]


# ── KBS 라디오 화면 · 지금 방송 프로그램 게시판 (0.63) ─────────────────
def test_hub_programs_from_kbs_radio_page():
    got = collect.hub_programs("KBS", [
        ("https://program.kbs.co.kr/2fm/radio/mrradio/pc/index.html", "윤정수 남창희의 미스터 라디오 월-일 16:00~18:00"),
        ("https://program.kbs.co.kr/2fm/radio/gabeesuperradio/", "가비의 슈퍼라디오"),
        ("https://program.kbs.co.kr/2fm/radio/mrradio/pc/board.html?bbs_loc=R2026-0001-03-123456,list,none,1,0", ""),
        ("https://radio.kbs.co.kr/schedule", "편성표")])
    assert [x["url"] for x in got] == ["https://program.kbs.co.kr/2fm/radio/mrradio/pc/index.html",
                                       "https://program.kbs.co.kr/2fm/radio/gabeesuperradio/pc/index.html"]
    menu = collect.board_links(got[0]["url"], [
        ("https://program.kbs.co.kr/2fm/radio/mrradio/pc/board.html?bbs_loc=R1,list", "사연과 신청곡"),
        ("https://program.kbs.co.kr/2fm/radio/mrradio/pc/board.html?bbs_loc=R2,list", "분노가 콸콸콸"),
        ("https://program.kbs.co.kr/2fm/radio/mrradio/pc/board.html?bbs_loc=R3,list", "공지사항"),
        ("https://program.kbs.co.kr/2fm/radio/mrradio/pc/vod.html", "다시 듣기")])
    assert [x["text"] for x in menu] == ["사연과 신청곡", "분노가 콸콸콸"]          # 청취자 참여 펼침 메뉴 안


def _onair_setup(conn):
    from radio_helper import boards

    db.set_setting(conn, "live.channel", "KBS 쿨FM")
    db.set_setting(conn, "live.active", "1")
    conn.execute("INSERT INTO programs (channel, code, title, start_time, end_time, days, main_url, on_air, updated_at) "
                 "VALUES ('KBS 쿨FM', 'mr', '윤정수 남창희의 미스터 라디오', '16:00', '18:00', '매일', "
                 "'https://program.kbs.co.kr/2fm/radio/mrradio/pc/index.html', 1, ?)", (db.now(),))
    conn.commit()
    return boards


def test_onair_tick_launches_current_program_once_a_day(conn, monkeypatch):
    boards = _onair_setup(conn)
    monkeypatch.setattr(boards, "board_busy", lambda: False)
    launched = []
    now = datetime(2026, 10, 22, 16, 30)
    assert boards.onair_tick(conn, now, launch=launched.append) == ["KBS 쿨FM|윤정수 남창희의 미스터 라디오"]
    assert launched == [["onair", "--channel", "KBS 쿨FM", "--program", "윤정수 남창희의 미스터 라디오"]]
    assert boards.onair_tick(conn, datetime(2026, 10, 22, 16, 40), launch=launched.append) == []   # 오늘 했음
    assert boards.onair_tick(conn, datetime(2026, 10, 22, 19, 0), launch=launched.append) == []    # 편성 없음
    db.set_setting(conn, "live.active", "0")
    assert boards.onair_tick(conn, datetime(2026, 10, 23, 16, 30), launch=launched.append) == []   # 듣지 않을 때는 안 함
    db.set_setting(conn, "live.active", "1")
    db.set_setting(conn, boards.ONAIR_KEY, "0")
    assert boards.onair_tick(conn, datetime(2026, 10, 23, 16, 30), launch=launched.append) == []   # 끔


def test_run_onair_collects_then_writes_every_board(conn):
    boards = _onair_setup(conn)
    collected, ran = [], []

    def collector(c, start, say=print):
        collected.append(start["url"])
        for url, title in (("https://program.kbs.co.kr/2fm/radio/mrradio/pc/board.html?bbs_loc=R1,list", "사연과 신청곡"),
                           ("https://program.kbs.co.kr/2fm/radio/mrradio/pc/board.html?bbs_loc=R2,list", "분노가 콸콸콸")):
            collect.add(c, url, title, start["program"], "시험")
        return 2

    def runner(c, cid, allow_window=None):
        ran.append((cid, allow_window))
        return "시험 입력함"
    out = boards.run_onair(conn, "KBS 쿨FM", "윤정수 남창희의 미스터 라디오", runner=runner, collector=collector,
                           say=lambda m: None)
    assert collected == ["https://program.kbs.co.kr/2fm/radio/mrradio/pc/index.html"]
    assert len(out) == 2 and all(w for _, w in ran)                               # 창을 띄워도 됨
    collect.add(conn, "https://programs.sbs.co.kr/radio/mrradio/boards/1", "다른 방송국", "윤정수 남창희의 미스터 라디오", "x")
    assert len(boards.onair_corners(conn, "KBS 쿨FM", "윤정수 남창희의 미스터 라디오")) == 2   # 같은 방송국만
    assert boards.run_onair(conn, "KBS 쿨FM", "없는 프로그램", runner=runner, collector=collector, say=lambda m: None) == []


def test_show_env_opens_visible_window(data_dir, monkeypatch):
    monkeypatch.setenv(autofill.SHOW_ENV, "1")
    monkeypatch.setenv("RADIO_HELPER_BROWSER_CHANNEL", "chromium")
    monkeypatch.setattr(autofill, "no_window", lambda: False)
    p = SimpleNamespace(chromium=FakeChromium())
    autofill.launch_profile(p, headless=True)
    assert p.chromium.calls[0][1]["headless"] is False
