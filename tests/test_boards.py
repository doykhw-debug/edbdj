"""방송국 상관없는 코너 게시판 자동 등록 (0.43): MBC·KBS·SBS 코너 주소 → 매번 홈페이지 공지를 읽고 →
공지에 맞는 '확인한 경험' 하나 → 입력(시험) 또는 등록. 실제 사이트 대신 같은 모양의 가짜 MBC 코너 화면을 쓴다
(공지·읽기 전용 작성자 칸·제목 칸·iframe 안의 iframe 스마트 에디터·같은 주소에서 글이 올라가는 '등록')."""

import json
from datetime import datetime
from pathlib import Path

import pytest
from conftest import csrf, make_draft, make_experience
from flask import Flask, request
from markupsafe import escape
from test_autofill import CHROMIUM
from test_board_flow import serve

from radio_helper import autofill, boards, db, gates, stations
from radio_helper.quizbot import story

MBC_URL = "https://www.imbc.com/broad/radio/fm4u/musicparty/v2/setting/corner/daily/2726825_76233.html"
PAGE = "/broad/radio/fm4u/musicparty/v2/setting/corner/daily/2726825_76233.html"
NOTICE = ("정오의 희망곡 오프닝을 여러분의 이야기로 채워주세요. 겪은, 들은 짧은 웃긴 한마디를 남겨주시면, "
          "오프닝에 소개해드립니다.")
MONDAY = datetime(2026, 10, 19, 11, 0)   # 월요일


def notice(**over):
    base = {"corner_title": "오프닝 한마디", "program": "정오의 희망곡", "notice": NOTICE, "accepting": "yes",
            "kind": "short", "theme": "겪은·들은 짧은 웃긴 한마디", "max_chars": 0, "ai_restricted": False,
            "needs_real": True, "deadline": "", "air_days": [], "needs_attachment": False, "reason": "공지에 적힘"}
    return {**base, **over}


class FakeAnswerer:
    def __init__(self, note=None, pick=None):
        self.note, self.pick, self.seen = note or notice(), pick or {}, {}

    def read_notice(self, station, url, page_text):
        self.seen.update(station=station, url=url, text=page_text)
        return dict(self.note)

    def pick_board_story(self, note, candidates):
        self.seen["candidates"] = [c["id"] for c in candidates]
        if callable(self.pick):
            return self.pick(candidates)
        return {"experience_id": candidates[0]["id"], "title": "아빠 유튜버야?", "line": "", "reason": "주제에 맞음",
                **self.pick}


def page_reader(text=NOTICE, links=(), write_url=None):
    return lambda corner: {"text": text, "links": list(links), "write_url": write_url}


def record_poster(calls, status="test"):
    def post(conn, draft, corner, allow_window=True, dry_run=False):
        calls.append({"draft": dict(draft), "dry_run": dry_run, "allow_window": allow_window})
        cur = conn.execute("INSERT INTO submissions (draft_id, experience_id, corner_id, title, body, post_status, "
                           "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (draft["id"], draft["experience_id"], corner["id"], draft["title"], draft["body"],
                            "test" if dry_run else status, db.now(), db.now()))
        conn.commit()
        return cur.lastrowid
    return post


def funny(conn, **over):
    fields = dict(label="아빠 유튜버", story="주말에 집에서 영상 편집을 하는데 첫째가 옆에 와서 한참 화면을 봤습니다.",
                  quotes="아빠 유튜버야?", quote_kind="exact", highlight="첫째가 아빠를 유튜버로 생각한 게 웃겼어요.")
    return make_experience(conn, **{**fields, **over})


# ── 주소 → 방송국 ─────────────────────────────────────────────────────
def test_station_of():
    assert stations.station_of(MBC_URL) == "MBC"
    assert stations.station_of("https://imbc.com/x") == "MBC"
    assert stations.station_of("https://program.kbs.co.kr/2fm/radio/x/pc/board.html?bbs_loc=1") == "KBS"
    assert stations.station_of("https://programs.sbs.co.kr/radio/x/cornerboardwrite/1") == "SBS"
    for bad in ("http://www.imbc.com/x", "https://www.imbc.com.evil.com/x", "https://evilimbc.com/x",
                "https://www.sbs.co.kr/x", "", None, "javascript:alert(1)"):
        assert stations.station_of(bad) is None, bad
    assert autofill._allowed_real_url(MBC_URL) and autofill._is_write_page(MBC_URL, MBC_URL)
    assert not autofill._is_write_page(MBC_URL.replace("76233", "76234"), MBC_URL)


def test_add_corner_by_url(client, conn):
    token = csrf(client, "/corners")
    r = client.post("/corners/add-url", data={"csrf_token": token, "url": MBC_URL}, follow_redirects=True)
    page = r.get_data(as_text=True)
    assert "매일 자동 등록" in page and "MBC" in page and "지금 공지 읽고 시험" in page
    row = conn.execute("SELECT * FROM corners WHERE board_url = ?", (MBC_URL,)).fetchone()
    assert (row["station"], row["write_url"], row["auto_on"], row["auto_test"]) == ("MBC", MBC_URL, 0, 1)
    assert boards.add_corner(conn, MBC_URL) == row["id"]                                   # 같은 주소는 하나
    r = client.post("/corners/add-url", data={"csrf_token": token, "url": "https://example.com/board"},
                    follow_redirects=True)
    assert "SBS·MBC·KBS" in r.get_data(as_text=True)


# ── 공지 읽기 → 판단 ─────────────────────────────────────────────────
def test_notice_saved_and_short_line_posted(conn):
    cid = boards.add_corner(conn, MBC_URL)
    eid = funny(conn)
    calls = []
    fake = FakeAnswerer(pick={"line": "첫째가 아빠를 유튜버로 생각한 게 웃겼어요."})
    text = boards.run(conn, cid, test=True, answerer=fake, reader=page_reader(), poster=record_poster(calls),
                      now=MONDAY, allow_window=False)
    assert "시험 입력 완료" in text and f"경험 #{eid}" in text
    assert fake.seen["station"] == "MBC" and NOTICE in fake.seen["text"]
    corner = conn.execute("SELECT * FROM corners WHERE id = ?", (cid,)).fetchone()
    assert corner["title"] == "오프닝 한마디" and corner["recruiting"] == "open" and corner["notice_text"] == NOTICE
    assert corner["notice_checked_at"] and json.loads(corner["notice_ai"])["kind"] == "short"
    assert corner["auto_result"].endswith(text[:20]) or text[:20] in corner["auto_result"]
    assert calls[0]["dry_run"] is True and calls[0]["draft"]["body"] == "첫째가 아빠를 유튜버로 생각한 게 웃겼어요."
    assert calls[0]["draft"]["status"] == "approved"


def test_short_line_with_made_up_facts_falls_back_to_real_words(conn):
    cid = boards.add_corner(conn, MBC_URL)
    funny(conn)
    calls = []
    fake = FakeAnswerer(pick={"line": "첫째가 3번이나 \"아빠 최고 유튜버\"라고 했어요"})   # 숫자·말을 지어냄
    boards.run(conn, cid, test=True, answerer=fake, reader=page_reader(), poster=record_poster(calls), now=MONDAY)
    assert calls[0]["draft"]["body"] == "아빠 유튜버야?"                                 # 경험에 적힌 말 그대로


def test_story_corner_uses_approved_text_as_is(conn):
    cid = boards.add_corner(conn, MBC_URL.replace("76233", "76234"))
    eid = funny(conn, song="아이유 - 밤편지")
    calls = []
    fake = FakeAnswerer(note=notice(kind="story", corner_title="사연과 신청곡", theme="사연과 신청곡"))
    boards.run(conn, cid, test=True, answerer=fake, reader=page_reader(), poster=record_poster(calls), now=MONDAY)
    body = calls[0]["draft"]["body"]
    assert body.startswith("안녕하세요.\n\n주말에 집에서 영상 편집을 하는데")                 # 0.60: 게시판 글 모양 (인사·맺음)
    assert body.endswith("(신청곡: 아이유 - 밤편지)\n\n늘 감사합니다.")
    d = conn.execute("SELECT * FROM drafts WHERE id = ?", (calls[0]["draft"]["id"],)).fetchone()
    assert (d["experience_id"], d["fact_confirmed"]) == (eid, 1)


@pytest.mark.parametrize("over, why", [
    ({"accepting": "no"}, "받지 않습니다"),
    ({"ai_restricted": True}, "AI 작성"),
    ({"needs_attachment": True}, "첨부"),
    ({"kind": "photo"}, "첨부"),
    ({"deadline": "2026-10-01"}, "마감"),
    ({"air_days": ["목"]}, "방송 전날"),
])
def test_notice_says_no(conn, over, why):
    cid = boards.add_corner(conn, MBC_URL)
    funny(conn)
    calls = []
    text = boards.run(conn, cid, test=False, answerer=FakeAnswerer(note=notice(**over)), reader=page_reader(),
                      poster=record_poster(calls), now=MONDAY)
    assert text.startswith("올리지 않음") and why in text and not calls


def test_weekday_corner_posts_the_day_before(conn):
    cid = boards.add_corner(conn, MBC_URL)
    funny(conn)
    calls = []
    text = boards.run(conn, cid, test=True, answerer=FakeAnswerer(note=notice(air_days=["화"])), reader=page_reader(),
                      poster=record_poster(calls), now=MONDAY)                         # 월요일 → 화요일 코너
    assert "시험 입력 완료" in text and calls


def test_no_matching_story_is_not_forced(conn):
    cid = boards.add_corner(conn, MBC_URL)
    funny(conn)
    calls = []
    fake = FakeAnswerer(pick=lambda c: {"experience_id": 0, "title": "", "line": "", "reason": "선생님 이야기가 없음"})
    text = boards.run(conn, cid, test=False, answerer=fake, reader=page_reader(), poster=record_poster(calls), now=MONDAY)
    assert "맞는 확인한 경험이 없어" in text and "선생님 이야기가 없음" in text and not calls
    assert conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0


def test_same_station_never_twice_other_station_ok(conn):
    """SBS 게시판에 보낸 경험도 MBC 에는 보낼 수 있고, MBC 에 한 번 보낸 경험은 MBC 에 다시 보내지 않는다."""
    eid = funny(conn)
    sbs = conn.execute("SELECT id FROM corners WHERE board_url LIKE 'https://programs.sbs.co.kr/%' LIMIT 1").fetchone()
    sbs_cid = sbs["id"] if sbs else boards.add_corner(conn, "https://programs.sbs.co.kr/radio/x/cornerboards/1")
    did = make_draft(conn, eid, sbs_cid)
    conn.execute("INSERT INTO submissions (draft_id, experience_id, corner_id, post_status, created_at, updated_at) "
                 "VALUES (?, ?, ?, 'posted', ?, ?)", (did, eid, sbs_cid, db.now(), db.now()))
    conn.commit()
    cid = boards.add_corner(conn, MBC_URL)
    calls = []
    fake = FakeAnswerer(pick={"line": "첫째가 아빠를 유튜버로 생각한 게 웃겼어요."})
    text = boards.run(conn, cid, test=False, answerer=fake, reader=page_reader(), poster=record_poster(calls, "posted"),
                      now=MONDAY)
    assert "등록 확인" in text and fake.seen["candidates"] == [eid]
    text = boards.run(conn, cid, test=False, answerer=fake, reader=page_reader(), poster=record_poster(calls), now=MONDAY)
    assert "지금 공지로 이미 올렸습니다" in text and len(calls) == 1                   # 0.60: 같은 공지에는 다시 안 올림
    fake.note = notice(notice="이번 주 주제: 가을 운동회 이야기를 보내 주세요", theme="가을 운동회")   # 새 공지
    text = boards.run(conn, cid, test=False, answerer=fake, reader=page_reader(), poster=record_poster(calls), now=MONDAY)
    assert "MBC에 아직 안 보낸" in text and len(calls) == 1
    # gates 도 방송국별로 본다: 같은 경험의 MBC 원고는 막힘
    did2 = make_draft(conn, eid, cid)
    assert any("같은 방송국(MBC)" in b for b in gates.evaluate(conn, did2, gates.MODE_POST).blockers)


def test_no_api_key_or_stopped(conn, monkeypatch):
    cid = boards.add_corner(conn, MBC_URL)
    from radio_helper.quizbot import answerer

    monkeypatch.setattr(answerer, "get_api_key", lambda: None)
    db.set_setting(conn, "quizbot.billing", "api")              # API 키 방식인데 키가 없음
    assert "API 키" in boards.run(conn, cid, reader=page_reader(), poster=record_poster([]))
    db.set_setting(conn, "global_stop", "1")
    assert "일괄 중지" in boards.run(conn, cid, reader=page_reader(), poster=record_poster([]))
    conn.execute("UPDATE corners SET auto_on = 1, auto_time = '00:00' WHERE id = ?", (cid,))
    conn.commit()
    assert boards.tick(conn, MONDAY, lambda args: "x") == []                           # 일괄 중지면 매일 자동도 안 함


# ── 매일 정한 시각 ───────────────────────────────────────────────────
def test_tick_once_a_day_at_time_and_days(conn):
    cid = boards.add_corner(conn, MBC_URL)
    conn.execute("UPDATE corners SET auto_on = 1, auto_time = '10:30', auto_days = '월수금' WHERE id = ?", (cid,))
    conn.commit()
    launched = []
    launch = lambda args: launched.append(args) or "boards_x.log"   # noqa: E731
    assert boards.tick(conn, MONDAY.replace(hour=10, minute=29), launch) == []
    assert boards.tick(conn, MONDAY.replace(hour=10, minute=31), launch) == [cid]
    assert launched == [["run", "--corner", str(cid), "--scheduled"]]          # 정해진 시각 실행 = 창 없이·하루 상한
    assert boards.tick(conn, MONDAY.replace(hour=15), launch) == []                    # 오늘은 했음
    assert boards.tick(conn, MONDAY.replace(day=20, hour=11), launch) == []            # 화요일은 아님
    assert boards.tick(conn, MONDAY.replace(day=21, hour=11), launch) == [cid]         # 수요일
    corner = conn.execute("SELECT * FROM corners WHERE id = ?", (cid,)).fetchone()
    assert boards.next_run(corner, MONDAY.replace(day=21, hour=12)) == "10/23(금) 10:30"


def test_auto_settings_form(client, conn):
    cid = boards.add_corner(conn, MBC_URL)
    token = csrf(client, f"/corners/{cid}")
    client.post(f"/corners/{cid}/auto", data={"csrf_token": token, "auto_on": "1", "auto_time": "11:20",
                                              "day_월": "1", "day_화": "1", "auto_test": "1"})
    row = conn.execute("SELECT * FROM corners WHERE id = ?", (cid,)).fetchone()
    assert (row["auto_on"], row["auto_time"], row["auto_days"], row["auto_test"]) == (1, "11:20", "월화", 1)
    client.post(f"/corners/{cid}/auto", data={"csrf_token": token, "auto_on": "1", "auto_time": "25:99",
                                              **{f"day_{d}": "1" for d in boards.DAYS}})
    row = conn.execute("SELECT * FROM corners WHERE id = ?", (cid,)).fetchone()
    assert (row["auto_time"], row["auto_days"], row["auto_test"]) == ("10:30", "", 0)   # 매일·실제 등록


def test_found_corners_can_be_added(client, conn):
    links = boards.same_program_links(MBC_URL, [
        ("https://www.imbc.com/broad/radio/fm4u/musicparty/v2/setting/corner/daily/2726825_76234.html", "사연과 신청곡"),
        ("https://www.imbc.com/broad/radio/fm4u/musicparty/v2/setting/corner/weekday/111.html", " Live On Air "),
        ("https://www.imbc.com/broad/radio/fm4u/other/v2/setting/corner/daily/1.html", "다른 프로그램"),
        ("https://www.example.com/corner/1", "광고"), (MBC_URL, "오프닝 한마디")])
    assert [l["text"] for l in links] == ["사연과 신청곡", "Live On Air"]
    cid = boards.add_corner(conn, MBC_URL)
    conn.execute("UPDATE corners SET found_links = ? WHERE id = ?", (json.dumps(links, ensure_ascii=False), cid))
    conn.commit()
    token = csrf(client, f"/corners/{cid}")
    client.post(f"/corners/{cid}/add-found", data={"csrf_token": token, "url": [links[0]["url"]]})
    titles = {r["title"] for r in conn.execute("SELECT title FROM corners WHERE station = 'MBC'")}
    assert titles == {"새 코너 (MBC)", "사연과 신청곡"}


# ── 실제 브라우저: 가짜 MBC 코너 화면 ─────────────────────────────────
def fake_mbc(posted: list):
    site = Flask("fake_mbc")

    @site.get(PAGE)
    def corner():
        items = "".join(f"<li>{escape(p['title'])}</li>" for p in posted)
        return f"""<!doctype html><meta charset="utf-8"><title>오프닝 한마디</title>
<nav><a href="/broad/radio/fm4u/musicparty/v2/setting/corner/daily/2726825_76234.html">사연과 신청곡</a></nav>
<h2>매일코너 &gt; 오프닝 한마디</h2><div class="notice">{NOTICE}</div>
<table><tr><th>작성자</th><td><input type="text" id="writer" value="청취자" readonly></td></tr>
<tr><th>제목</th><td><input type="text" id="subj"></td></tr></table>
<iframe id="editor_frame" src="/editor/skin.html" style="width:600px;height:220px"></iframe>
<button type="button" onclick="save()">등록</button><ul id="list">{items}</ul>
<script>
function save() {{
  const body = document.getElementById('editor_frame').contentDocument.getElementById('se2_iframe').contentDocument.body;
  fetch('/save', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{title: document.getElementById('subj').value, content: body.innerText}})}})
  .then(() => {{ alert('등록되었습니다.'); location.reload(); }});
}}
</script>"""

    @site.get("/editor/skin.html")
    def skin():
        return ('<!doctype html><meta charset="utf-8"><div>글꼴 · 크기</div>'
                '<iframe id="se2_iframe" name="se2_iframe" src="/editor/body.html" style="width:560px;height:160px"></iframe>')

    @site.get("/editor/body.html")
    def editor_body():
        return '<!doctype html><meta charset="utf-8"><body></body><script>document.designMode = "on";</script>'

    @site.post("/save")
    def save():
        posted.append(request.get_json())
        return {"ok": True}

    return site


@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="Chromium 없음")
@pytest.mark.parametrize("test_mode", [True, False])
def test_real_browser_mbc_like_corner(conn, monkeypatch, test_mode):
    pytest.importorskip("playwright")
    monkeypatch.setenv("RADIO_HELPER_CHROMIUM", CHROMIUM)
    monkeypatch.setenv("RADIO_HELPER_HEADLESS", "1")
    monkeypatch.setattr(autofill, "READY_WAIT_SECONDS", 6)
    monkeypatch.setattr(autofill, "POSTED_WAIT_SECONDS", 10)
    posted: list = []
    srv = serve(fake_mbc(posted))
    base = f"http://127.0.0.1:{srv.server_port}"
    real = stations.station_of
    monkeypatch.setattr(stations, "station_of", lambda url: "MBC" if (url or "").startswith(base) else real(url))
    try:
        cid = boards.add_corner(conn, base + PAGE)
        eid = funny(conn)
        fake = FakeAnswerer(pick={"title": "아빠 유튜버야?", "line": "첫째가 아빠를 유튜버로 생각한 게 웃겼어요."})
        text = boards.run(conn, cid, test=test_mode, answerer=fake, now=MONDAY, allow_window=False)
    finally:
        srv.shutdown()
    assert NOTICE in fake.seen["text"]                                                  # 홈페이지 공지를 실제로 읽음
    corner = conn.execute("SELECT * FROM corners WHERE id = ?", (cid,)).fetchone()
    found = json.loads(corner["found_links"])
    assert [f["text"] for f in found] == ["사연과 신청곡"]                                # 같은 프로그램의 다른 코너
    # 작성자(읽기 전용)가 아니라 제목 칸, 본문은 iframe 안의 iframe(스마트 에디터)
    assert corner["title_selector"] == "#subj" and corner["body_selector"] == "#editor_frame || #se2_iframe || body"
    sub = conn.execute("SELECT * FROM submissions").fetchone()
    shots = conn.execute("SELECT * FROM evidence WHERE item_table = 'submissions' AND item_id = ?", (sub["id"],)).fetchall()
    if test_mode:
        assert "시험 입력 완료" in text and posted == [] and sub["post_status"] == "test"
        assert [s["status"] for s in shots] == ["test"]
    else:
        assert posted == [{"title": "아빠 유튜버야?", "content": "첫째가 아빠를 유튜버로 생각한 게 웃겼어요."}]
        assert "등록 확인" in text and sub["post_status"] == "posted" and "등록되었습니다" in sub["note"]
        assert [s["status"] for s in shots] == ["filled", "posted"]
        assert eid not in {e["id"] for e in story.candidate_experiences(conn, "MBC")}       # MBC 에 다시 안 보냄
        assert eid in {e["id"] for e in story.candidate_experiences(conn, "SBS")}
