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
