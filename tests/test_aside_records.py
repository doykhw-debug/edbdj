"""0.70: Aside 로 따로 한 게시판 점검·등록 기록 가져오기와 Aside 규칙 (실시간은 방송 멘트 때만, 상시·이벤트 정기 자동 제외,
올린 곳·결과 불명은 다시 보내지 않음, 같은 경험은 모든 방송국·채널에서 한 번)."""

import io
import json
import zipfile
from datetime import datetime

from conftest import csrf

from radio_helper import aside_records, boards, db, gates
from radio_helper.quizbot import story

SBS_A = "https://programs.sbs.co.kr/radio/aaa/boards/111"
SBS_B = "https://programs.sbs.co.kr/radio/bbb/boards/222"
MBC_C = "https://www.imbc.com/broad/radio/fm4u/ccc/v2/setting/corner/index.html"
KBS_D = "https://program.kbs.co.kr/2fm/radio/ddd/pc/board.html?smenu=1&bbs_loc=R1,list,none,1,0"
NEW_E = "https://programs.sbs.co.kr/radio/eee/boards/555"


def exp(conn, label="가을 소풍"):
    cur = conn.execute("INSERT INTO experiences (label, story, user_confirmed, created_at, updated_at) "
                       "VALUES (?, '실제로 있었던 일', 1, ?, ?)", (label, db.now(), db.now()))
    conn.commit()
    return cur.lastrowid


def artifacts(conn):
    """앱 코너 3곳(A·B·C)과 경험 2개, 그리고 Aside 결과 파일 (가짜 내용)."""
    a, b, c = (boards.add_corner(conn, u, t) for u, t in ((SBS_A, "사연"), (SBS_B, "신청곡"), (MBC_C, "코너")))
    e1, e2 = exp(conn, "하나"), exp(conn, "둘")
    main = {"records": [
        {"representative_id": a, "canonical_url": SBS_A, "operation_class": "excluded_from_recurring",
         "state": "registered_no_repeat", "reason": "등록 확인", "program": "가", "corner": "사연"},
        {"representative_id": b, "canonical_url": SBS_B, "operation_class": "realtime_board_mention_gated",
         "state": "not_selected_wait_next_user_request", "program": "나", "corner": "신청곡"},
        {"representative_id": c, "canonical_url": MBC_C, "operation_class": "excluded_from_recurring",
         "state": "hold_no_repeat_without_condition_change", "reason": "음성 파일 첨부 필수", "program": "다",
         "corner": "코너", "publication_type": "official_notice_comment"},
        {"representative_id": 9999, "canonical_url": KBS_D, "operation_class": "excluded_from_recurring",
         "state": "registered_no_repeat", "program": "라", "corner": "사연과 신청곡"},
        {"representative_id": 8888, "canonical_url": "https://program.kbs.co.kr/x/pc/board.html?bbs_loc=R9,list",
         "operation_class": "realtime_board_mention_gated", "state": "not_selected_wait_next_user_request"},
    ]}
    posted = [
        {"execution_id": "x1", "board_canonical": SBS_A, "experience_id": e1, "title": "제목1", "body": "본문1",
         "post_url": SBS_A.replace("boards", "board") + "?cmd=view&board_no=1", "site_registered_at": "2026.10.22 21:04"},
        {"execution_id": "x2", "board_canonical": KBS_D, "experience_id": 77777, "title": "제목2", "body": "본문2",
         "program": "라", "corner": "사연과 신청곡"},
    ]
    unknown = {"records": [{"board_key": "SBS:eee:555:", "board_canonical": NEW_E, "experience_id": e2,
                            "attempted_title": "제목3", "attempted_body": "본문3", "reason": "검색해도 없음"}]}
    types = "id,original_url,operation_class\n" \
            f"{a},{SBS_A},excluded_from_recurring\n"
    files = {aside_records.MAIN_FILE: json.dumps(main).encode(), aside_records.POSTED_FILE: json.dumps(posted).encode(),
             aside_records.UNKNOWN_FILE: json.dumps(unknown).encode(), aside_records.TYPES_FILE: types.encode("utf-8-sig")}
    return (a, b, c), (e1, e2), files


def row(conn, cid):
    return conn.execute("SELECT * FROM corners WHERE id = ?", (cid,)).fetchone()


def test_import_records_posts_unknowns_and_classes_once(conn, tmp_path):
    (a, b, c), (e1, e2), files = artifacts(conn)
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    s = aside_records.import_path(conn, str(tmp_path))
    assert (s["posted"], s["unknown"], s["hold"], s["mention"], s["sub_added"], s["no_experience"]) == (2, 1, 1, 1, 2, 1)
    assert s["missing"] == 1 and s["corners_added"] == 2               # 이력을 붙일 KBS·결과 불명 SBS 게시판만 새로
    assert (row(conn, a)["run_class"], row(conn, a)["hold_state"]) == ("standing", "posted")
    assert (row(conn, b)["run_class"], row(conn, b)["hold_state"]) == ("mention", "")
    assert row(conn, c)["hold_state"] == "hold" and "댓글" in row(conn, c)["dev_note"]
    subs = conn.execute("SELECT * FROM submissions ORDER BY id").fetchall()
    assert [(x["experience_id"], x["post_status"]) for x in subs] == [(e1, "posted"), (e2, "unknown")]
    assert subs[0]["created_at"] == "2026-10-22 21:04:00" and "[aside:x1]" in subs[0]["note"]
    new = conn.execute("SELECT * FROM corners WHERE board_url = ?", (NEW_E,)).fetchone()
    assert new["hold_state"] == "unknown"
    again = aside_records.import_path(conn, str(tmp_path))              # 다시 가져와도 겹치지 않음
    assert again["sub_added"] == 0 and again["sub_exists"] == 2 and again["corners_added"] == 0
    assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 2
    assert "등록함 2" in aside_records.describe(again)


def test_zip_with_folder_and_cp949_names(conn):
    (a, _, _), _, files = artifacts(conn)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i, (name, data) in enumerate(files.items()):
            if i == 0:   # 윈도우 탐색기 압축: 이름이 cp949 (UTF-8 표시 없음) — 같은 길이의 ASCII 이름으로 쓴 뒤 바이트를 바꿈
                zf.writestr("artifacts/" + "X" * len(name.encode("cp949")), data)
            else:
                zf.writestr("artifacts/" + name, data)
        zf.writestr("artifacts/참고_최초목록/" + aside_records.MAIN_FILE, b"{}")   # 예전 목록은 읽지 않음
    first = next(iter(files))
    raw = buf.getvalue().replace(b"X" * len(first.encode("cp949")), first.encode("cp949"))
    got = aside_records.read_source(data=raw)
    assert set(got) == set(files) and json.loads(got[aside_records.MAIN_FILE])["records"]
    assert aside_records.apply(conn, got)["posted"] == 2


def test_missing_files_are_explained(tmp_path):
    try:
        aside_records.read_source(path=str(tmp_path))
    except aside_records.AsideImportError as e:
        assert "기록 파일" in str(e)
    else:
        raise AssertionError("빈 폴더인데 가져옴")


def test_auto_import_only_when_main_file_changes(conn, tmp_path):
    _, _, files = artifacts(conn)
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    assert aside_records.auto_import(conn, str(tmp_path))["posted"] == 2
    assert aside_records.auto_import(conn, str(tmp_path)) is None
    assert aside_records.auto_import(conn, str(tmp_path / "없음")) is None


def test_block_reason_by_trigger(conn):
    cid = boards.add_corner(conn, SBS_A)

    def why(cls, hold, trigger, auto=0):
        conn.execute("UPDATE corners SET run_class = ?, hold_state = ?, auto_on = ? WHERE id = ?", (cls, hold, auto, cid))
        return aside_records.block_reason(conn, row(conn, cid), trigger)

    assert why("mention", "", "onair") is None and "멘트" in why("mention", "", "scheduled")
    assert why("mention", "", "manual") is None
    assert "상시" in why("standing", "", "onair") and "상시" in why("standing", "", "scheduled", auto=1)
    assert why("standing", "", "manual") is None                       # 사용자가 누른 한 번은 됨
    assert "다시 올리지 않음" in why("", "posted", "manual") and "결과 불명" in why("mention", "unknown", "onair")
    assert "보류" in why("", "hold", "onair") and why("", "hold", "manual") is None
    assert "중복" in why("alias", "", "manual")
    assert "매일" in why("", "", "scheduled") and why("", "", "scheduled", auto=1) is None
    db.set_setting(conn, aside_records.RULES_KEY, "0")
    assert why("standing", "posted", "scheduled") is None              # 규칙을 끄면 예전처럼


def test_board_mention_detection():
    yes = ["오늘 주제는 첫눈입니다. 홈페이지 게시판에 사연 남겨 주세요.", "신청곡은 홈페이지 게시판으로 보내 주시고요",
           "여러분 이야기 게시판에 올려 주세요!", "사연은 미니 게시판에 적어 주세요"]
    no = ["선곡표는 홈페이지 게시판에서 확인하세요.", "당첨자는 홈페이지 게시판에 발표합니다.", "오늘 날씨가 참 좋네요.",
          "문자 #8000 으로 보내 주세요."]
    assert all(aside_records.board_mention(t) for t in yes)
    assert not any(aside_records.board_mention(t) for t in no)


def _onair(conn):
    db.set_setting(conn, "live.channel", "KBS 쿨FM")
    db.set_setting(conn, "live.active", "1")
    conn.execute("INSERT INTO programs (channel, code, title, start_time, end_time, days, main_url, on_air, updated_at) "
                 "VALUES ('KBS 쿨FM', 'mr', '미스터 라디오', '16:00', '18:00', '매일', "
                 "'https://program.kbs.co.kr/2fm/radio/mrradio/pc/index.html', 1, ?)", (db.now(),))
    conn.commit()


def say_on_air(conn, at, text):
    conn.execute("INSERT INTO transcripts (broadcast_date, at, text, channel, program) VALUES (?, ?, ?, 'KBS 쿨FM', ?)",
                 (at[:10], at, text, "미스터 라디오"))
    conn.commit()


def test_onair_waits_for_board_mention(conn, monkeypatch):
    _onair(conn)
    monkeypatch.setattr(boards, "board_busy", lambda: False)
    launched = []
    assert boards.onair_tick(conn, datetime(2026, 10, 22, 16, 30), launch=launched.append) == []   # 멘트 없음
    say_on_air(conn, "2026-10-22 16:20:00", "홈페이지 게시판에 사연 남겨 주세요")                    # 10분 넘게 지난 멘트
    say_on_air(conn, "2026-10-22 16:29:00", "선곡표는 게시판에서 보세요")
    assert boards.onair_tick(conn, datetime(2026, 10, 22, 16, 31), launch=launched.append) == []
    say_on_air(conn, "2026-10-22 16:35:00", "오늘 주제 '가을 소풍', 사연은 홈페이지 게시판에 올려 주세요.")
    assert boards.onair_tick(conn, datetime(2026, 10, 22, 16, 36), launch=launched.append) == ["KBS 쿨FM|미스터 라디오"]
    assert launched[0][:5] == ["onair", "--channel", "KBS 쿨FM", "--program", "미스터 라디오"]
    assert launched[0][5] == "--mention" and "게시판" in launched[0][6]
    assert boards.onair_tick(conn, datetime(2026, 10, 22, 16, 50), launch=launched.append) == []   # 하루 한 번


def test_run_onair_skips_standing_posted_and_does_not_collect(conn):
    _onair(conn)
    urls = [f"https://program.kbs.co.kr/2fm/radio/mrradio/pc/board.html?bbs_loc=R{i},list" for i in range(3)]
    ids = [boards.add_corner(conn, u, f"코너{i}") for i, u in enumerate(urls)]
    conn.execute("UPDATE corners SET program = '미스터 라디오'")
    conn.execute("UPDATE corners SET run_class = 'standing' WHERE id = ?", (ids[0],))
    conn.execute("UPDATE corners SET run_class = 'mention', hold_state = 'posted' WHERE id = ?", (ids[1],))
    conn.execute("UPDATE corners SET run_class = 'mention' WHERE id = ?", (ids[2],))
    conn.commit()
    ran = []
    out = boards.run_onair(conn, "KBS 쿨FM", "미스터 라디오",
                           runner=lambda c, cid, allow_window=None, trigger=None: ran.append((cid, trigger)) or "ok",
                           collector=lambda *a, **k: (_ for _ in ()).throw(AssertionError("모으지 않음")), say=lambda m: None)
    assert ran == [(ids[2], "onair")] and len(out) == 1
    assert boards.run_onair(conn, "KBS 쿨FM", "없는 프로그램", runner=None,
                            collector=lambda *a, **k: (_ for _ in ()).throw(AssertionError("모으지 않음")),
                            say=lambda m: None) == []


def test_daily_tick_only_launches_allowed_corners(conn, monkeypatch):
    monkeypatch.setattr(boards, "board_busy", lambda: False)
    db.set_setting(conn, boards.DAILY_KEY, "1")
    conn.execute("DELETE FROM corners")
    a, b, c = (boards.add_corner(conn, u) for u in (SBS_A, SBS_B, MBC_C))
    conn.execute("UPDATE corners SET run_class = 'standing', auto_on = 1, auto_time = '09:00' WHERE id = ?", (a,))
    conn.execute("UPDATE corners SET run_class = 'mention' WHERE id = ?", (b,))
    conn.execute("UPDATE corners SET auto_on = 1, auto_time = '09:00' WHERE id = ?", (c,))
    conn.commit()
    launched = []
    assert boards.tick(conn, datetime(2026, 10, 22, 11, 0), launch=launched.append) == [c]


class Reader:
    def __init__(self, text="공지"):
        self.text = text

    def __call__(self, corner):
        return {"text": self.text, "links": [], "write_url": corner["board_url"]}


class Ans:
    def __init__(self, **notice):
        self.notice = {"accepting": "yes", "kind": "story", "theme": "", "max_chars": 0, "entry": "write", **notice}

    def read_notice(self, station, url, text):
        return dict(self.notice)

    def pick_board_story(self, notice, candidates):
        return {"experience_id": 0, "reason": "맞는 것 없음"}


def test_strict_run_needs_explicit_recruitment_and_holds_blocked_boards(conn):
    cid = boards.add_corner(conn, SBS_B)
    conn.execute("UPDATE corners SET run_class = 'mention' WHERE id = ?", (cid,))
    conn.commit()
    go = lambda ans, trig: boards.run(conn, cid, test=True, answerer=ans, reader=Reader(), poster=lambda *a, **k: 0,  # noqa: E731
                                      trigger=trig)
    assert "모집한다는 말" in go(Ans(accepting="unknown"), "onair")
    assert "모집한다는 말" not in go(Ans(accepting="unknown"), "manual")       # 사용자가 누른 것은 예전처럼
    assert "댓글" in go(Ans(entry="comment"), "onair")
    assert row(conn, cid)["hold_state"] == "hold"                             # 막힌 후보는 다시 시도하지 않음
    assert "보류" in go(Ans(), "onair")


def test_same_experience_never_again_on_any_station(conn):
    sbs, mbc = boards.add_corner(conn, SBS_A), boards.add_corner(conn, MBC_C)
    eid, other = exp(conn), exp(conn, "다른 일")
    conn.execute("INSERT INTO submissions (experience_id, corner_id, post_status, created_at, updated_at) "
                 "VALUES (?, ?, 'unknown', ?, ?)", (eid, sbs, db.now(), db.now()))
    conn.commit()
    assert eid not in {c["id"] for c in story.candidate_experiences(conn, "MBC")} and \
        other in {c["id"] for c in story.candidate_experiences(conn, "MBC")}
    did = boards.make_draft(conn, mbc, eid, "제목", "본문")
    assert any("모든 방송국" in b for b in gates.evaluate(conn, did, gates.MODE_POST).blockers)
    db.set_setting(conn, aside_records.RULES_KEY, "0")
    assert eid in {c["id"] for c in story.candidate_experiences(conn, "MBC")}     # 예전 규칙: 다른 방송국은 됨


def test_bytes_limit_and_same_day_once(conn):
    e = exp(conn)
    conn.execute("UPDATE experiences SET story = ? WHERE id = ?", ("가나다라마바사. " * 40, e))
    conn.commit()
    ex = conn.execute("SELECT * FROM experiences WHERE id = ?", (e,)).fetchone()
    title, body = boards.pick_text(conn, {"kind": "story", "max_chars": 300, "limit_unit": "bytes"}, ex, {"title": "t"})
    assert len(body.encode("utf-8")) <= 300
    cid = boards.add_corner(conn, SBS_B)
    boards.record_posted(conn, cid, {"notice": "어제 공지", "theme": "가"}, test=False, at="2026-10-22 09:00:00")
    corner = row(conn, cid)
    new = {"notice": "완전히 다른 오늘 공지 내용입니다", "theme": "나"}
    assert "같은 방송에는 한 번만" in boards.already_posted(conn, corner, new, False, now=datetime(2026, 10, 22, 18, 0))
    assert boards.already_posted(conn, corner, new, False, now=datetime(2026, 10, 23, 18, 0)) is None


def test_corner_screens_show_rules_and_import(client, conn, tmp_path):
    (a, _, _), _, files = artifacts(conn)
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    page = client.get("/corners").get_data(as_text=True)
    assert "Aside 기록·규칙" in page and "규칙 켜짐" in page
    r = client.post("/corners/aside-import", data={"csrf_token": csrf(client), "folder": str(tmp_path)})
    assert r.status_code == 302
    page = client.get("/corners").get_data(as_text=True)
    assert "Aside 등록함" in page and "상시·이벤트" in page and "등록함 2" in page
    edit = client.get(f"/corners/{a}").get_data(as_text=True)
    assert "보류 풀기" in edit
    client.post(f"/corners/{a}/aside", data={"csrf_token": csrf(client), "run_class": "mention", "release": "1"})
    assert (row(conn, a)["run_class"], row(conn, a)["hold_state"]) == ("mention", "")
    client.post("/corners/aside-rules", data={"csrf_token": csrf(client)})
    assert not aside_records.rules_on(conn)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(aside_records.POSTED_FILE, files[aside_records.POSTED_FILE])
    r = client.post("/corners/aside-import", data={"csrf_token": csrf(client), "file": (io.BytesIO(buf.getvalue()), "a.zip")},
                    content_type="multipart/form-data")
    assert r.status_code == 302
    assert (aside_records.last(conn)["source"], aside_records.last(conn)["sub_exists"]) == ("a.zip", 1)
