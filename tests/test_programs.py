from conftest import csrf

from radio_helper import app as app_module
from radio_helper import programs, seed


def test_all_powerfm_programs_seeded(conn):
    rows = conn.execute("SELECT * FROM programs WHERE on_air = 1 AND channel = '파워FM' ORDER BY start_time").fetchall()
    assert len(rows) == len(seed.POWERFM_PROGRAMS) == 13
    love = conn.execute("SELECT * FROM programs WHERE channel = '러브FM'").fetchall()
    assert [r["title"] for r in love] == [t for _c, t, *_ in seed.LOVEFM_PROGRAMS]
    chul = conn.execute("SELECT * FROM programs WHERE code = '0chulpowerfm'").fetchone()
    assert (chul["title"], chul["start_time"], chul["end_time"]) == ("김영철의 파워FM", "07:00", "09:00")
    assert chul["main_url"] == "https://programs.sbs.co.kr/radio/0chulpowerfm/main"
    # 하루 24시간이 빈틈없이 이어진다 (01:00 ~ 다음 날 01:00)
    slots = sorted((r["start_time"], r["end_time"]) for r in rows)
    for (s1, e1), (s2, _e2) in zip(slots, slots[1:]):
        assert e1 == s2


def test_seed_is_idempotent(conn):
    from radio_helper import db

    db.init_db(conn)
    assert conn.execute("SELECT COUNT(*) FROM programs").fetchone()[0] == \
        len(seed.POWERFM_PROGRAMS) + len(seed.LOVEFM_PROGRAMS)


def test_parse_time_range():
    f = programs.parse_time_range
    assert f("매일 07:00 ~ 09:00 방송") == ("07:00", "09:00", "매일")
    assert f("클립 03:21 재생 / 월~금 16:00~18:00") == ("16:00", "18:00", "월~금")
    assert f("매일 밤 11시 ~ 새벽 1시") == ("23:00", "01:00", "매일")
    assert f("오후 4시~6시") == ("16:00", "18:00", None)
    assert f("오전 11시~12시")[:2] == ("11:00", "12:00")
    assert f("밤 11시~1시")[:2] == ("23:00", "01:00")
    assert f("평일 오전 9시 반 ~ 11시") == ("09:30", "11:00", "평일")
    assert f("방송 시간 안내 없음") == (None, None, None)


def test_parse_program_page_boards_only_for_that_program():
    links = [
        ("/radio/cultwoshow/boards/58047", "사연과 신청곡"),
        ("https://programs.sbs.co.kr/radio/cultwoshow/cornerboards/58000?cornerid=1", " 오늘의 퀴즈 "),
        ("/radio/cultwoshow/boards/58047", "사연과 신청곡"),        # 중복
        ("/radio/lovegame/boards/57680", "다른 프로그램 게시판"),    # 다른 프로그램
        ("https://evil.example.com/radio/cultwoshow/boards/1", "외부"),
        ("/radio/cultwoshow/clips/69953", "클립"),                 # 게시판 아님
    ]
    info = programs.parse_program_page("cultwoshow", "매일 14:00~16:00", links,
                                       "https://programs.sbs.co.kr/radio/cultwoshow/main")
    assert (info.start, info.end, info.days) == ("14:00", "16:00", "매일")
    assert info.boards == [
        ("게시판", "사연과 신청곡", "https://programs.sbs.co.kr/radio/cultwoshow/boards/58047"),
        ("코너", "오늘의 퀴즈", "https://programs.sbs.co.kr/radio/cultwoshow/cornerboards/58000?cornerid=1"),
    ]


def test_discover_programs():
    links = [("https://programs.sbs.co.kr/radio/newshow/main", "새 프로그램"),
             ("https://programs.sbs.co.kr/radio/lovegame/main?div=x", "박소현의 러브게임"),
             ("https://www.sbs.co.kr/news", "뉴스")]
    assert programs.discover_programs(links) == {"newshow": "새 프로그램", "lovegame": "박소현의 러브게임"}


def fake_site(pages):
    def fetch(url):
        if url not in pages:
            raise TimeoutError(url)
        return pages[url]
    return fetch


def test_refresh_updates_times_boards_and_candidates(conn):
    pages = {p: ("방송 시간 정보 없음", []) for (p,) in conn.execute("SELECT main_url FROM programs")}
    pages["https://programs.sbs.co.kr/radio/cultwoshow/main"] = (
        "두시탈출 컬투쇼 매일 오후 2시 ~ 4시 반", [("/radio/cultwoshow/boards/58047", "사연과 신청곡")])
    pages["https://programs.sbs.co.kr/radio/ten/main"] = ("월~금 22:00~24:00", [])
    pages[programs.RADIO_HOME_URL] = ("", [("https://programs.sbs.co.kr/radio/newshow/main", "새 프로그램"),
                                            ("https://programs.sbs.co.kr/radio/ten/main", "배성재의 텐")])
    del pages["https://programs.sbs.co.kr/radio/afterclub/main"]  # 한 페이지 실패

    report = programs.refresh(conn, fake_site(pages))

    total = conn.execute("SELECT COUNT(*) FROM programs WHERE code != 'newshow'").fetchone()[0]
    assert report.checked == total - 1 and any("애프터클럽" in e for e in report.errors)
    ten = conn.execute("SELECT * FROM programs WHERE code = 'ten'").fetchone()
    assert (ten["start_time"], ten["end_time"], ten["days"], ten["source"]) == ("22:00", "00:00", "월~금", "공식 페이지")
    cultwo = conn.execute("SELECT * FROM programs WHERE code = 'cultwoshow'").fetchone()
    assert (cultwo["start_time"], cultwo["end_time"]) == ("14:00", "16:30")
    # 시간을 못 읽은 프로그램은 기존 값을 지운다거나 바꾸지 않는다
    chul = conn.execute("SELECT * FROM programs WHERE code = '0chulpowerfm'").fetchone()
    assert (chul["start_time"], chul["end_time"]) == ("07:00", "09:00") and "김영철의 파워FM" in report.unreadable
    board = conn.execute("SELECT * FROM corners WHERE program = '두시탈출 컬투쇼'").fetchone()
    assert board["title"] == "사연과 신청곡" and board["recruiting"] == "unknown" and board["is_target"] == 0
    cand = conn.execute("SELECT * FROM programs WHERE code = 'newshow'").fetchone()
    assert cand["on_air"] == 0 and cand["channel"] == "미확인"

    # 다시 돌려도 게시판·후보가 중복으로 생기지 않는다
    again = programs.refresh(conn, fake_site(pages))
    assert again.new_boards == [] and again.candidates == []


def test_detect_channel():
    assert programs.detect_channel("SBS 러브FM 103.5MHz 매일 오후 2시") == "러브FM"
    assert programs.detect_channel("POWER FM 107.7 · 파워FM 편성표") == "파워FM"
    assert programs.detect_channel("파워FM 러브FM 고릴라M") is None      # 메뉴처럼 모두 한 번씩
    assert programs.detect_channel("시간 정보 없음") is None


def test_refresh_adds_programs_of_other_channels(conn):
    pages = {p: ("", []) for (p,) in conn.execute("SELECT main_url FROM programs")}
    pages[programs.RADIO_HOME_URL] = ("", [("https://programs.sbs.co.kr/radio/lovenew/main", "러브 새 프로그램"),
                                            ("https://programs.sbs.co.kr/radio/mystery/main", "모르는 프로그램")])
    pages["https://programs.sbs.co.kr/radio/lovenew/main"] = ("SBS 러브FM 103.5 · 러브FM 매일 오후 2시 ~ 4시", [])
    pages["https://programs.sbs.co.kr/radio/mystery/main"] = ("편성 정보 없음", [])

    report = programs.refresh(conn, fake_site(pages))

    new = conn.execute("SELECT * FROM programs WHERE code = 'lovenew'").fetchone()
    assert (new["channel"], new["start_time"], new["end_time"], new["on_air"]) == ("러브FM", "14:00", "16:00", 1)
    mystery = conn.execute("SELECT * FROM programs WHERE code = 'mystery'").fetchone()
    assert (mystery["channel"], mystery["on_air"]) == ("미확인", 0)
    assert any("러브 새 프로그램" in a for a in report.added) and report.candidates == ["모르는 프로그램"]


def test_program_pages_and_refresh_launch(client, conn, monkeypatch):
    assert "김영철의 파워FM" in client.get("/programs").get_data(as_text=True)
    launched = []
    monkeypatch.setattr(app_module, "launch_programs_refresh", lambda: launched.append(1) or "p.log")
    token = csrf(client, "/programs")
    client.post("/programs/refresh", data={"csrf_token": token})
    assert launched == [1]

    pid = conn.execute("SELECT id FROM programs WHERE code = 'ten'").fetchone()[0]
    assert client.get(f"/programs/{pid}").status_code == 200
    client.post(f"/programs/{pid}", data={"csrf_token": token, "title": "배성재의 텐", "channel": "파워FM",
                                          "start_time": "22:00", "end_time": "24:00", "days": "매일", "on_air": "1"})
    assert conn.execute("SELECT end_time FROM programs WHERE id = ?", (pid,)).fetchone()[0] == "24:00"
    assert client.get("/corners?program=김영철의 파워FM").status_code == 200


# ── 코너·게시판 모두 수집 ─────────────────────────────────────────────
MAIN = "https://programs.sbs.co.kr/radio/0chulpowerfm/main"
CB = "https://programs.sbs.co.kr/radio/0chulpowerfm/cornerboards/57577"


def test_board_link_normalizes_urls():
    f = programs.board_link
    codes = {"0chulpowerfm"}
    assert f("/radio/0chulpowerfm/cornerboards/57577?cornerid=3002&div=gnb", " 사연과\n신청곡 ", MAIN, codes) == \
        ("코너", "사연과 신청곡", CB + "?cornerid=3002")
    # 글 하나를 여는 주소 → 게시판 주소로 줄이고, 글 제목은 이름으로 쓰지 않는다
    assert f("/radio/0chulpowerfm/boards/57580?cmd=view&amp;board_no=999", "오늘 받은 선물 후기", MAIN, codes) == \
        ("게시판", "", "https://programs.sbs.co.kr/radio/0chulpowerfm/boards/57580")
    assert f("https://m.programs.sbs.co.kr/radio/0ChulPowerFM/boards/57580/write", "글쓰기", MAIN, codes)[2] == \
        "https://programs.sbs.co.kr/radio/0ChulPowerFM/boards/57580"
    assert f("/radio/0chulpowerfm/boards/57580", "가" * 61, MAIN, codes)[1] == ""     # 너무 긴 글자는 이름 아님
    assert f("/radio/lovegame/boards/1", "다른 프로그램", MAIN, codes) is None
    assert f("/radio/0chulpowerfm/clips/1", "클립", MAIN, codes) is None


def test_links_in_text_and_json():
    html = '<div ng-click="go(\'/radio/ten/boards/57950\')"></div><script>u="https:\\/\\/programs.sbs.co.kr\\/radio\\/ten\\/cornerboards\\/1?cornerid=7"</script>'
    assert programs.links_in_text(html) == [("/radio/ten/boards/57950", ""),
                                            ("https://programs.sbs.co.kr/radio/ten/cornerboards/1?cornerid=7", "")]
    data = '{"menus": [{"menu_name": "사연과 신청곡", "link": "/radio/ten/boards/57950"}, {"title": "", "sub": [{"name": "퀴즈", "url": "https://programs.sbs.co.kr/radio/ten/cornerboards/1?cornerid=7"}]}]}'
    assert programs.links_in_json(data) == [("/radio/ten/boards/57950", "사연과 신청곡"),
                                            ("https://programs.sbs.co.kr/radio/ten/cornerboards/1?cornerid=7", "퀴즈")]
    assert programs.links_in_json("not json /radio/ten/boards/2") == [("/radio/ten/boards/2", "")]


def test_collect_boards_opens_corner_board_for_tabs():
    pages = {
        CB + "?cornerid=3002": ("", [("?cornerid=3002", "사연과 신청곡"), ("?cornerid=3003", "영철이가 쏜다"),
                                     ("/radio/0chulpowerfm/cornerboards/57577?cornerid=52015", "[월] 리얼드라마"),
                                     ("/radio/0chulpowerfm/boards/57580", "문화선물 게시판")]),
    }
    main_links = [("/radio/0chulpowerfm/cornerboards/57577", ""),                      # 탭 없는 코너 게시판
                  ("/radio/0chulpowerfm/cornerboards/57577?cornerid=3002", "코너"),
                  ("/radio/0chulpowerfm/boards/57580?board_no=5", "어떤 글 제목")]       # 이름 없는 게시판
    opened = []

    def fetch(url):
        opened.append(url)
        if url == CB:
            raise TimeoutError(url)
        return pages[url] if url in pages else ("", [])

    boards, visits = programs.collect_boards(fetch, "0chulpowerfm", main_links, MAIN)
    assert boards == [
        ("코너", "코너", CB + "?cornerid=3002"),
        ("게시판", "문화선물 게시판", "https://programs.sbs.co.kr/radio/0chulpowerfm/boards/57580"),
        ("코너", "영철이가 쏜다", CB + "?cornerid=3003"),
        ("코너", "[월] 리얼드라마", CB + "?cornerid=52015"),
    ]
    # 탭 없는 주소가 안 열리면 탭 주소로 다시 열고, 같은 코너 게시판은 그 뒤로 열지 않는다.
    # 이름 없던 게시판은 코너 페이지에서 이름이 나와 따로 열지 않는다 (탭 없는 주소는 목록에서 뺀다)
    assert opened == [CB, CB + "?cornerid=3002"] and visits == 2
    assert programs.collect_boards(fetch, "0chulpowerfm", main_links, MAIN, limit=0)[1] == 0


def test_collect_boards_visit_limit():
    links = [(f"/radio/ten/cornerboards/{i}", "") for i in range(10)]
    opened = []
    boards, visits = programs.collect_boards(lambda u: opened.append(u) or ("", []), "ten", links,
                                             "https://programs.sbs.co.kr/radio/ten/main", limit=3)
    assert visits == 3 and len(opened) == 3 and len(boards) == 10


def test_refresh_collects_all_boards_with_redirect_and_discovery(conn):
    pages = {p: ("", []) for (p,) in conn.execute("SELECT main_url FROM programs")}
    # 주소가 바뀐 프로그램: 최종 주소의 코드로 된 게시판도 그 프로그램 것으로 본다
    pages["https://programs.sbs.co.kr/radio/ten/main"] = (
        "월~금 22:00~24:00", [("/radio/ten2026/boards/57950", "사연과 신청곡")],
        "https://programs.sbs.co.kr/radio/ten2026/main")
    pages[MAIN] = ("매일 07:00 ~ 09:00", [("/radio/0chulpowerfm/cornerboards/57577?cornerid=3002", "사연")])
    pages[CB + "?cornerid=3002"] = ("", [("?cornerid=3099", "새 코너")])
    pages[programs.RADIO_HOME_URL] = ("", [("https://programs.sbs.co.kr/radio/lovenew/main", "러브 새 프로그램")])
    pages["https://programs.sbs.co.kr/radio/lovenew/main"] = (
        "SBS 러브FM 103.5 · 러브FM 매일 오후 2시 ~ 4시", [("/radio/lovenew/boards/1", "사연 게시판")])
    said = []

    report = programs.refresh(conn, lambda u: pages[u] if u in pages else ("", []), say=said.append)

    ten = conn.execute("SELECT * FROM corners WHERE program = '배성재의 텐'").fetchone()
    assert ten["board_url"] == "https://programs.sbs.co.kr/radio/ten2026/boards/57950"
    chul = {r["board_url"]: r["title"] for r in conn.execute("SELECT * FROM corners WHERE program = '김영철의 파워FM'")}
    assert chul[CB + "?cornerid=3002"].startswith("사연과 신청곡")          # 처음 넣은 이름은 그대로
    assert chul[CB + "?cornerid=3099"] == "새 코너" and len(chul) == len(programs.seed.CORNERS) + 1
    assert conn.execute("SELECT title FROM corners WHERE program = '러브 새 프로그램'").fetchone()[0] == "사연 게시판"
    assert report.board_counts["배성재의 텐"] == 1 and report.board_counts["김영철의 파워FM"] == 2
    assert "애프터클럽" in report.no_boards and "게시판을 못 찾음" in report.summary()
    assert any(s.startswith("- 김영철의 파워FM: 게시판·코너 2개 (새로 추가 1개") for s in said)

    again = programs.refresh(conn, lambda u: pages[u] if u in pages else ("", []))
    assert again.new_boards == [] and again.board_counts["배성재의 텐"] == 1


def test_unnamed_board_gets_placeholder(conn):
    pages = {p: ("", []) for (p,) in conn.execute("SELECT main_url FROM programs")}
    pages["https://programs.sbs.co.kr/radio/ten/main"] = ("", [("/radio/ten/boards/57950?board_no=3", "글 제목"),
                                                                ("/radio/ten/cornerboards/9?cornerid=4", "")])
    programs.refresh(conn, lambda u: pages[u] if u in pages else ("", []), discover=False)
    rows = {r["board_url"]: r for r in conn.execute("SELECT * FROM corners WHERE program = '배성재의 텐'")}
    board = rows["https://programs.sbs.co.kr/radio/ten/boards/57950"]
    assert board["title"] == "(이름 확인 필요) 게시판 57950" and "이름을 못 읽음" in board["dev_note"]
    assert rows["https://programs.sbs.co.kr/radio/ten/cornerboards/9?cornerid=4"]["title"] == "(이름 확인 필요) 코너 4"


def test_corners_page_collect_button_and_counts(client, conn, monkeypatch):
    launched = []
    monkeypatch.setattr(app_module, "launch_programs_refresh", lambda: launched.append(1) or "p.log")
    page = client.get("/corners").get_data(as_text=True)
    assert "코너·게시판 모두 수집" in page and 'name="back" value="corners"' in page
    assert "김영철의 파워FM 12" in page and "배성재의 텐 0" in page
    token = csrf(client, "/corners")
    r = client.post("/programs/refresh", data={"csrf_token": token, "back": "corners"})
    assert launched == [1] and r.headers["Location"].endswith("/corners")

    cid = conn.execute("SELECT id FROM corners ORDER BY id LIMIT 1").fetchone()[0]
    client.post(f"/corners/{cid}", data={"csrf_token": token, "corner_title": "고친 이름", "recruiting": "unknown",
                                          "ai_assist_policy": "unknown"})
    assert conn.execute("SELECT title FROM corners WHERE id = ?", (cid,)).fetchone()[0] == "고친 이름"
