"""사연 모음(각색) 가져오기: 인물 줄로 친구 추가·직업 적기 → 보관함에 인물별로 → 확인 전 경험 → 고치거나 지우기.
(예시 이름·사연은 모두 지어낸 것)"""

import json

from conftest import csrf
from test_people import MAP, load, person

from radio_helper import db, people
from radio_helper.quizbot import story


def collection(*extra):
    rows = [
        {"type": "person", "name": "철수", "side": "friend", "grp": "친구", "alias": "은행 다니는 친구",
         "relation": "친구 · 은행원", "job": "은행원", "job_tag": "주신"},
        {"type": "person", "name": "다라", "side": "friend", "grp": "친구", "alias": "간호사 친구",
         "relation": "친구 · 간호사", "job": "간호사", "job_tag": "임의", "note": "직업은 사연 묶음용 임의 값"},
    ]
    stories = [
        {"id": "Y001", "character": "철수", "title": "창구 이야기", "intro": "", "body": "은행 창구에서 있었던 일",
         "source_site": "YouTube 예시 Shorts", "source_id": "https://www.youtube.com/shorts/x1", "category": "화자"},
        {"id": "Y002", "character": "다라", "title": "병원 이야기", "intro": "", "body": "병동에서 있었던 일",
         "source_site": "YouTube 예시 Shorts", "source_id": "https://www.youtube.com/shorts/x2", "category": "주제"},
        {"id": "Y003", "character": "와이프", "title": "시댁 이야기", "intro": "", "body": "시댁에서 있었던 일",
         "source_site": "YouTube 예시 Shorts", "source_title": "원본 제목", "category": "화자"},
        {"id": "Y004", "character": "나", "title": "지하철 이야기", "intro": "", "body": "지하철에서 본 일",
         "source_site": "YouTube 예시 Shorts", "category": "기본"},
    ]
    # 사연 줄이 먼저, 인물 줄이 뒤에 있어도 인물을 먼저 넣는다
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in [*stories, *rows, *extra])


def test_people_lines_and_stories_by_person(client, conn):
    people.import_people(conn, people.parse_character_map(MAP))
    text = collection()
    assert people.detect(text) == "library"
    token = csrf(client, "/import")
    r = client.post("/import", data={"csrf_token": token, "text": text})
    assert r.headers["Location"].endswith("/library")

    # 기존 '김철수'는 이름 끝(철수)으로 찾아 직업만 적고, 새 친구 '다라'는 만든다 (실제 사건은 넣지 않음)
    gd = person(conn, "김철수")
    assert {"key": "직업", "value": "은행원", "tag": "주신"} in json.loads(gd["details"])
    assert person(conn, "철수") is None
    dr = person(conn, "다라")
    assert (dr["side"], dr["alias"], dr["events"]) == ("friend", "간호사 친구", "[]")
    assert json.loads(dr["details"]) == [{"key": "직업", "value": "간호사", "tag": "임의"}]

    by_code = {r["code"]: r for r in conn.execute("SELECT * FROM story_library")}
    assert by_code["Y001"]["person_id"] == gd["id"] and by_code["Y002"]["person_id"] == dr["id"]
    assert by_code["Y003"]["person_id"] == person(conn, "와이프")["id"]
    assert by_code["Y004"]["person_id"] == conn.execute("SELECT id FROM people WHERE side = 'self'").fetchone()[0]
    assert all(r["kind"] == "fiction" for r in by_code.values())                # 원본이 있는 각색 사연
    assert "shorts/x1" in by_code["Y001"]["origin"]
    assert story.candidate_experiences(conn) == []                              # 아무것도 보내지 않음
    page = client.get("/library").get_data(as_text=True)
    assert "창구 이야기" in page and "가상" in page
    assert "간호사 친구" in client.get(f"/people/{dr['id']}").get_data(as_text=True)


def test_ambiguous_name_makes_new_person(conn):
    load(conn)
    conn.execute("INSERT INTO people (name, side, grp, created_at, updated_at) VALUES ('최철수', 'friend', '친구', ?, ?)",
                 (db.now(), db.now()))
    conn.commit()
    assert people.find_person(conn, "철수") is None                              # 김철수·최철수 중 누군지 모름
    assert people.find_person(conn, "와이프") == person(conn, "와이프")["id"]
    assert people.find_person(conn, "홍길동") == person(conn, "홍길동 과장님")["id"]


def test_guessed_job_does_not_overwrite_given_one(conn):
    load(conn)
    people.import_person_records(conn, people.parse_person_records(collection()))
    again = json.dumps({"type": "person", "name": "철수", "job": "택시 기사", "job_tag": "임의"}, ensure_ascii=False)
    assert people.import_person_records(conn, people.parse_person_records(again)) == (0, 0)
    gd = person(conn, "김철수")
    assert [d["value"] for d in json.loads(gd["details"]) if d["key"] == "직업"] == ["은행원"]


def test_review_delete_and_confirm(client, conn):
    load(conn)
    people.import_library(conn, people.parse_story_log(collection()))
    token = csrf(client, "/library")
    client.post("/library/to-experiences", data={"csrf_token": token})
    rows = conn.execute("SELECT e.*, l.code FROM experiences e JOIN story_library l ON l.id = e.from_library_id "
                        "WHERE l.code LIKE 'Y%' ORDER BY l.code").fetchall()
    assert len(rows) == 4 and all(e["user_confirmed"] == 0 for e in rows)
    page = client.get("/experiences/review").get_data(as_text=True)
    assert "아님 · 지우기" in page

    y1, y2 = rows[0], rows[1]
    form = {"csrf_token": token, "src": "library", "ids": [str(e["id"]) for e in rows]}
    for e in rows:
        form.update({f"label_{e['id']}": e["label"], f"when_text_{e['id']}": e["when_text"],
                     f"story_{e['id']}": e["story"]})
    form[f"del_{y1['id']}"] = "1"                                               # 아닌 것은 지움
    form[f"story_{y2['id']}"] = "병동에서 실제로 있었던 일 — 내가 고쳐 씀"            # 살을 붙여 고친 것은 확인
    form[f"ok_{y2['id']}"] = "1"
    r = client.post("/experiences/review", data=form, follow_redirects=True)
    assert "1건 확인" in r.get_data(as_text=True) and "1건 지움" in r.get_data(as_text=True)
    assert conn.execute("SELECT 1 FROM experiences WHERE id = ?", (y1["id"],)).fetchone() is None
    assert conn.execute("SELECT 1 FROM story_library WHERE code = 'Y001'").fetchone() is None   # 다시 가져오지 않음
    kept = conn.execute("SELECT * FROM experiences WHERE id = ?", (y2["id"],)).fetchone()
    assert kept["user_confirmed"] == 1 and "내가 고쳐 씀" in kept["story"]
    client.post("/library/to-experiences", data={"csrf_token": token})
    assert conn.execute("SELECT COUNT(*) FROM experiences e JOIN story_library l ON l.id = e.from_library_id "
                        "WHERE l.code LIKE 'Y%'").fetchone()[0] == 3


def test_delete_buttons(client, conn):
    load(conn)
    people.import_library(conn, people.parse_story_log(collection()))
    token = csrf(client, "/library")
    client.post("/library/to-experiences", data={"csrf_token": token})
    exp = lambda code: conn.execute(  # noqa: E731
        "SELECT e.* FROM experiences e JOIN story_library l ON l.id = e.from_library_id WHERE l.code = ?",
        (code,)).fetchone()
    lib = lambda code: conn.execute("SELECT id FROM story_library WHERE code = ?", (code,)).fetchone()  # noqa: E731

    # 경험 화면: 확인 전이면 지울 수 있고 보관함 원본도 함께
    e3 = exp("Y003")
    assert "이 경험 지우기" in client.get(f"/experiences/{e3['id']}").get_data(as_text=True)
    client.post(f"/experiences/{e3['id']}/delete", data={"csrf_token": token})
    assert conn.execute("SELECT 1 FROM experiences WHERE id = ?", (e3["id"],)).fetchone() is None and lib("Y003") is None

    # 확인한 경험은 지우지 않음
    e4 = exp("Y004")
    conn.execute("UPDATE experiences SET user_confirmed = 1 WHERE id = ?", (e4["id"],))
    conn.commit()
    assert "이 경험 지우기" not in client.get(f"/experiences/{e4['id']}").get_data(as_text=True)
    r = client.post(f"/experiences/{e4['id']}/delete", data={"csrf_token": token}, follow_redirects=True)
    assert "지우지 않습니다" in r.get_data(as_text=True)
    assert conn.execute("SELECT 1 FROM experiences WHERE id = ?", (e4["id"],)).fetchone()

    # 보관함에서 지우기: 확인 전 경험은 함께 지우고, 확인한 경험은 남기고 연결만 끊음
    e2 = exp("Y002")
    client.post(f"/library/{lib('Y002')['id']}/delete", data={"csrf_token": token})
    assert lib("Y002") is None and conn.execute("SELECT 1 FROM experiences WHERE id = ?", (e2["id"],)).fetchone() is None
    client.post(f"/library/{lib('Y004')['id']}/delete", data={"csrf_token": token})
    left = conn.execute("SELECT * FROM experiences WHERE id = ?", (e4["id"],)).fetchone()
    assert lib("Y004") is None and left["user_confirmed"] == 1 and left["from_library_id"] is None

    # 원고를 만든 경험은 지우지 않음
    e1 = exp("Y001")
    conn.execute("INSERT INTO drafts (experience_id, corner_id, title, body, created_at, updated_at) "
                 "VALUES (?, (SELECT id FROM corners LIMIT 1), '제목', '본문', ?, ?)", (e1["id"], db.now(), db.now()))
    conn.commit()
    assert not people.delete_unconfirmed_experience(conn, e1["id"])


def test_person_only_file(client, conn):
    load(conn)
    only = "\n".join(line for line in collection().splitlines() if '"person"' in line)
    assert people.detect(only) == "library"
    token = csrf(client, "/import")
    r = client.post("/import", data={"csrf_token": token, "text": only})
    assert r.headers["Location"].endswith("/people") and person(conn, "다라") is not None


# ── 승인한 글 그대로 · 사연 목록 내보내기 ─────────────────────────────
def test_approved_text_uses_my_words_within_limit(conn):
    from radio_helper.quizbot import story as story_mod

    load(conn)
    wife = person(conn, "와이프")["id"]
    long_story = "첫 문장입니다. 두 번째 문장도 있어요. " + "아주 긴 세 번째 문장 " * 20 + "끝."
    eid = conn.execute("INSERT INTO experiences (label, story, ending, about_person_id, user_confirmed, created_at, updated_at) "
                       "VALUES ('짧은 이름', ?, '마무리입니다.', ?, 1, ?, ?)",
                       (long_story, wife, db.now(), db.now())).lastrowid
    exp = conn.execute("SELECT * FROM experiences WHERE id = ?", (eid,)).fetchone()
    chat = story_mod.approved_text(conn, exp, 80, song="가수 - 노래")
    assert chat == "제 와이프 이야기인데요, 첫 문장입니다. 두 번째 문장도 있어요. (신청곡: 가수 - 노래)" and len(chat) <= 80
    board = story_mod.approved_text(conn, exp, 2000)
    assert board.startswith("제 와이프 이야기인데요, 첫 문장입니다.") and board.endswith("마무리입니다.")   # 게시판은 마무리까지
    conn.execute("UPDATE experiences SET gorilla_line = '내가 직접 쓴 한 줄' WHERE id = ?", (eid,))
    exp = conn.execute("SELECT * FROM experiences WHERE id = ?", (eid,)).fetchone()
    assert story_mod.approved_text(conn, exp, 200) == "내가 직접 쓴 한 줄"
    conn.execute("UPDATE experiences SET gorilla_line = '', story = '홍길동 과장님과 회식했다.' WHERE id = ?", (eid,))
    exp = conn.execute("SELECT * FROM experiences WHERE id = ?", (eid,)).fetchone()
    assert "홍길동" not in story_mod.approved_text(conn, exp, 200)                 # 실명은 ○○로 가림


def test_export_story_list(client, conn):
    load(conn)
    people.import_person_records(conn, people.parse_person_records(collection()))
    people.import_library(conn, people.parse_story_log(collection()))
    token = csrf(client, "/library")
    client.post("/library/to-experiences", data={"csrf_token": token})
    rows = conn.execute("SELECT e.id, l.code FROM experiences e JOIN story_library l ON l.id = e.from_library_id "
                        "WHERE l.code LIKE 'Y%' ORDER BY l.code").fetchall()
    for r in rows[1:]:                                                            # Y001 만 확인 전으로 남김
        conn.execute("UPDATE experiences SET user_confirmed = 1, story = story || ' (내가 고침)' WHERE id = ?", (r["id"],))
    conn.commit()
    assert "읽기용 (.txt)" in client.get("/experiences").get_data(as_text=True)

    r = client.get("/experiences/export")
    assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
    text = r.get_data().decode("utf-8-sig")
    assert "확인한 사연 3편" in text and "■ 와이프 (1편)" in text and "■ 내 이야기 (1편)" in text
    assert "■ 간호사 친구 (1편)" in text                                        # 실명(다라) 대신 호칭
    assert "[Y003] 시댁 이야기" in text and "(내가 고침)" in text and "창구 이야기" not in text   # 확인 전은 빼고
    assert "김철수" not in text and "다라" not in text
    assert "창구 이야기" in client.get("/experiences/export?all=1").get_data().decode("utf-8-sig")

    data = client.get("/experiences/export?fmt=jsonl").get_data(as_text=True)
    lines = [json.loads(x) for x in data.splitlines()]
    assert [x["id"] for x in lines] == ["Y002", "Y003", "Y004"] and all(x["kind"] == "real" for x in lines)
    # 다시 가져오면 보관함에 '실제' 사연으로 (다른 PC·백업 복원용)
    back = people.parse_story_log(data)
    assert {x["kind"] for x in back} == {"real"} and back[1]["person_label"] == "와이프"


# ── 사연 전체 초기화 ──────────────────────────────────────────────────
def test_reset_stories_keeps_people_and_settings(client, conn):
    from radio_helper.quizbot import config

    load(conn)
    people.import_person_records(conn, people.parse_person_records(collection()))
    people.import_library(conn, people.parse_story_log(collection()))
    token = csrf(client, "/library")
    client.post("/library/to-experiences", data={"csrf_token": token})
    conn.execute("UPDATE experiences SET user_confirmed = 1")
    eids = [r[0] for r in conn.execute("SELECT id FROM experiences ORDER BY id")]
    corner = conn.execute("SELECT id FROM corners LIMIT 1").fetchone()[0]
    # 보낸 사연 기록 1건, 게시판 제출 1건(원고 포함), 그냥 원고 1건
    conn.execute("INSERT INTO story_posts (program, broadcast_date, topic, experience_id, message, status, created_at, "
                 "updated_at) VALUES ('p', '2026-10-16', '주제', ?, '보낸 글', 'entered', ?, ?)", (eids[0], db.now(), db.now()))
    did = conn.execute("INSERT INTO drafts (experience_id, corner_id, title, body, created_at, updated_at) "
                       "VALUES (?, ?, '제목', '본문', ?, ?)", (eids[1], corner, db.now(), db.now())).lastrowid
    conn.execute("INSERT INTO submissions (draft_id, experience_id, corner_id, title, body, post_status, created_at, "
                 "updated_at) VALUES (?, ?, ?, '제목', '본문', 'posted', ?, ?)", (did, eids[1], corner, db.now(), db.now()))
    conn.execute("INSERT INTO drafts (experience_id, corner_id, title, body, created_at, updated_at) "
                 "VALUES (?, ?, '안 보낸 원고', '본문', ?, ?)", (eids[2], corner, db.now(), db.now()))
    db.set_setting(conn, "gorilla.input_point", "650,617")
    conn.commit()
    people_before = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    assert "사연 전체 초기화" in client.get("/experiences").get_data(as_text=True)

    r = client.post("/experiences/reset", data={"csrf_token": token, "confirm": "아니"}, follow_redirects=True)
    assert "지우지 않았습니다" in r.get_data(as_text=True)
    n_lib = conn.execute("SELECT COUNT(*) FROM story_library").fetchone()[0]
    assert n_lib == 7 and len(eids) == 7                                                  # 예시 3편 + 모음 4편

    r = client.post("/experiences/reset", data={"csrf_token": token, "confirm": "초기화"}, follow_redirects=True)
    page = r.get_data(as_text=True)
    assert f"사연을 초기화했습니다: 보관함 {n_lib}편 · 실제 경험 {len(eids) - 1}건 · 원고 1건" in page and "backups" in page
    assert "게시판 제출 기록과 이어진 경험 1건은" in page
    assert conn.execute("SELECT COUNT(*) FROM story_library").fetchone()[0] == 0
    assert [r[0] for r in conn.execute("SELECT id FROM experiences")] == [eids[1]]      # 게시판 제출과 이어진 것만 남김
    assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 1           # 기록은 그대로
    assert conn.execute("SELECT experience_id, message FROM story_posts").fetchone()[:] == (None, "보낸 글")
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == people_before    # 인물·설정은 그대로
    assert config.get(conn, "gorilla.input_point") == "650,617"
    backups = list((db.data_dir() / "backups").glob("before_story_reset_*.sqlite3"))
    assert len(backups) == 1
    import sqlite3
    old = sqlite3.connect(str(backups[0]))
    assert old.execute("SELECT COUNT(*) FROM story_library").fetchone()[0] == n_lib         # 백업에는 지우기 전 그대로
    old.close()

    # 기록까지 지우기
    client.post("/experiences/reset", data={"csrf_token": token, "confirm": "초기화", "history": "1"})
    for table in ("experiences", "drafts", "submissions", "story_posts", "story_library"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == people_before


def test_reset_with_no_board_submissions_deletes_everything(client, conn):
    """게시판 제출 기록이 하나도 없을 때도 다 지운다 (NOT IN (NULL) 은 아무것도 지우지 않던 실수 방지)."""
    load(conn)
    people.library_to_experiences(conn)
    assert conn.execute("SELECT COUNT(*) FROM experiences").fetchone()[0] == 3
    r = people.reset_stories(conn)
    assert (r["library"], r["experiences"], r["kept"]) == (3, 3, 0)
    assert conn.execute("SELECT COUNT(*) FROM experiences").fetchone()[0] == 0


def test_full_jsonl_export_survives_reset_and_comes_back(client, conn):
    """사연 jsonl로 받기(전체) → 초기화 → 가져오기: 보관함·확인한 사연(고친 글·한 줄·인물)이 그대로 돌아온다."""
    load(conn)
    people.import_person_records(conn, people.parse_person_records(collection()))
    people.import_library(conn, people.parse_story_log(collection()))
    lib = {r["code"]: r["id"] for r in conn.execute("SELECT id, code FROM story_library")}
    e1, _ = people.library_to_experience(conn, conn.execute("SELECT * FROM story_library WHERE code = 'Y002'").fetchone())
    e2, _ = people.library_to_experience(conn, conn.execute("SELECT * FROM story_library WHERE code = 'R002'").fetchone())
    conn.execute("UPDATE experiences SET user_confirmed = 1, story = '병동에서 실제로 있었던 일 (내가 고침)', "
                 "gorilla_line = '직접 쓴 한 줄', ending = '마무리' WHERE id = ?", (e1,))
    manual = conn.execute("INSERT INTO experiences (label, story, user_confirmed, created_at, updated_at) "
                          "VALUES ('직접 쓴 경험', '내가 직접 쓴 이야기', 1, ?, ?)", (db.now(), db.now())).lastrowid
    conn.commit()
    assert e2 and manual and len(lib) == 7
    html = client.get("/library").get_data(as_text=True)
    assert "사연 jsonl로 받기 (보관함 + 실제 경험 전체)" in html
    assert "사연 jsonl로 받기 (전체)" in client.get("/experiences").get_data(as_text=True)

    data = client.get("/experiences/export?fmt=jsonl&all=1&library=1").get_data(as_text=True)
    rows = [json.loads(x) for x in data.splitlines()]
    by_id = {r["id"]: r for r in rows}
    assert len(rows) == 8                                              # 경험 3건(Y002·R002·직접) + 아직 안 옮긴 보관함 5편
    assert by_id["Y002"]["confirmed"] and by_id["Y002"]["person"] == "다라" and by_id["Y002"]["character"] == "간호사 친구"
    assert not by_id["R002"]["confirmed"] and by_id["R002"]["person"] == "홍길동 과장님"
    assert by_id["Y001"]["kind"] == "fiction" and "shorts/x1" in by_id["Y001"]["origin"]
    assert by_id[f"E{manual:04d}"]["person"] == "나"

    people.reset_stories(conn, include_history=True)
    assert conn.execute("SELECT COUNT(*) FROM experiences").fetchone()[0] == 0
    token = csrf(client, "/import")
    r = client.post("/import", data={"csrf_token": token, "text": data}, follow_redirects=True)
    assert "확인해 둔 사연 2편은 확인된 실제 경험으로 되살렸습니다" in r.get_data(as_text=True)
    assert conn.execute("SELECT COUNT(*) FROM story_library").fetchone()[0] == 8
    exps = {e["label"]: e for e in conn.execute("SELECT * FROM experiences")}
    assert set(exps) == {"병원 이야기", "직접 쓴 경험"} and all(e["user_confirmed"] for e in exps.values())
    nurse = exps["병원 이야기"]
    assert (nurse["story"], nurse["gorilla_line"], nurse["ending"]) == ("병동에서 실제로 있었던 일 (내가 고침)", "직접 쓴 한 줄", "마무리")
    assert nurse["about_person_id"] == person(conn, "다라")["id"] and exps["직접 쓴 경험"]["about_person_id"] is None
    y001 = conn.execute("SELECT * FROM story_library WHERE code = 'Y001'").fetchone()
    assert y001["kind"] == "fiction" and "shorts/x1" in y001["origin"] and y001["person_id"] == person(conn, "김철수")["id"]
    r002 = conn.execute("SELECT * FROM story_library WHERE code = 'R002'").fetchone()
    assert r002["person_id"] == person(conn, "홍길동 과장님")["id"] and r002["kind"] == "fiction"   # 확인 전이던 것은 보관함으로
