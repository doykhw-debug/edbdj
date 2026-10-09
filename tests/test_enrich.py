"""관계도 실제 사건을 사연처럼 풀어 쓰기 (가짜 Claude 로 시험, 예시 데이터는 지어낸 것)."""

import json

from test_people import load

from radio_helper import db, people
from radio_helper.quizbot import __main__ as cli
from radio_helper.quizbot import answerer, enrich, story

LONG = "회사 일로 늘 바쁜 와이프가 여행지 숙소 문제로 속을 끓였는데, 끝까지 차분하게 이야기해서 결국 더 좋은 곳으로 옮겼어요. " * 2


def moved(conn):
    load(conn)
    people.events_to_experiences(conn)
    return {e["story"]: e for e in conn.execute("SELECT * FROM experiences ORDER BY id")}


def test_item_masks_names_and_skips_made_up_details(conn):
    exps = moved(conn)
    wife = enrich.item_for(conn, exps["숙소 오버부킹에 항의해 더 좋은 호텔로 옮김"])
    assert (wife["about"], wife["relation"], wife["when"]) == ("와이프", "아내", "2025.10")
    assert "나이: 38세" in wife["facts"] and not any("떡볶이" in f for f in wife["facts"])   # 〔임의〕 정보는 뺌
    boss = enrich.item_for(conn, exps["과장 승진"])
    assert boss["about"] == "회사 동료" and "성격: 농담 많음" in boss["facts"]
    assert "홍길동" not in json.dumps(boss, ensure_ascii=False)
    me = enrich.item_for(conn, exps["새 아파트 입주"])
    assert me["about"] == "나" and me["relation"] == ""
    msg = answerer.build_enrich_message({"tone": "담백"}, [wife])
    assert "사건(바꾸면 안 되는 사실): 숙소 오버부킹" in msg and "#" in msg


def test_run_saves_story_and_keeps_fact(conn):
    exps = moved(conn)
    calls = []

    def fake(profile, items):
        calls.append([i["id"] for i in items])
        out = []
        for i in items:
            if i["event"] == "과장 승진":     # 재료에 없는 내용을 넣었다고 스스로 표시 → 원래 한 줄 유지
                out.append({"id": i["id"], "story": LONG, "highlight": "", "ending": "", "added_facts": ["회식 장소"]})
            elif i["event"] == "회식에서 노래":  # 연락처가 들어감 → 원래 한 줄 유지
                out.append({"id": i["id"], "story": LONG + " 010-1234-5678", "highlight": "", "ending": "",
                            "added_facts": []})
            else:
                out.append({"id": i["id"], "story": LONG, "highlight": "끝까지 차분했던 와이프",
                            "ending": "멋있었어요, 여보.", "added_facts": []})
        return out

    done, kept = enrich.run(conn, fake)
    assert (done, kept) == (3, 2) and len(calls) == 1 and len(calls[0]) == 5
    wife = conn.execute("SELECT * FROM experiences WHERE id = ?", (exps["숙소 오버부킹에 항의해 더 좋은 호텔로 옮김"]["id"],)).fetchone()
    assert wife["story"] == LONG and wife["highlight"] == "끝까지 차분했던 와이프" and wife["ending"] == "멋있었어요, 여보."
    assert wife["fixed_facts"] == "2025.10 숙소 오버부킹에 항의해 더 좋은 호텔로 옮김" and wife["enriched_at"]
    boss = conn.execute("SELECT * FROM experiences WHERE id = ?", (exps["과장 승진"]["id"],)).fetchone()
    assert boss["story"] == "과장 승진" and "재료에 없는 내용" in boss["enrich_note"]
    assert "개인정보" in conn.execute("SELECT enrich_note FROM experiences WHERE story = '회식에서 노래'").fetchone()[0]
    assert enrich.pending(conn) == [] and enrich.load_status(conn) == {**enrich.load_status(conn), "running": False,
                                                                       "done": 3, "kept": 2, "total": 5}
    # 풀어 쓴 사연이 그대로 사연 재료가 된다
    item = next(e for e in story.candidate_experiences(conn, "SBS") if e["id"] == wife["id"])
    assert item["story"] == LONG and item["intro"] == "제 와이프 이야기인데요"


def test_failed_batch_stays_pending(conn):
    moved(conn)

    def boom(profile, items):
        raise answerer.AnswererError("인터넷 연결 오류로 분석하지 못했습니다.")

    said = []
    assert enrich.run(conn, boom, said.append) == (0, 0)
    assert len(enrich.pending(conn)) == 5 and "인터넷" in enrich.load_status(conn)["error"] and said


def test_batches_of_eight(conn):
    load(conn)
    for i in range(20):
        conn.execute("INSERT INTO experiences (story, from_event, user_confirmed, created_at, updated_at) "
                     "VALUES (?, ?, 1, ?, ?)", (f"사건 {i}", f"0|2025.01|사건 {i}", db.now(), db.now()))
    conn.commit()
    sizes = []
    enrich.run(conn, lambda p, items: sizes.append(len(items)) or [])
    assert sizes == [8, 8, 4]


def test_cli_without_api_key(conn, monkeypatch):
    moved(conn)
    monkeypatch.setattr(answerer, "get_api_key", lambda: None)
    assert cli.cmd_enrich(conn) == 2
    status = enrich.load_status(conn)
    assert status["running"] is False and "API 키" in status["error"] and status["total"] == 5


def test_parse_enriched():
    out = answerer.parse_enriched('{"items": [{"id": 3, "story": " 글 ", "highlight": "", "ending": "끝",'
                                  ' "added_facts": ["", " "]}]}')
    assert out == [{"id": 3, "story": "글", "highlight": "", "ending": "끝", "added_facts": []}]
