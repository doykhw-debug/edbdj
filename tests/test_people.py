"""인물 관계도 · 사연 보관함 · '화자는 나' 규칙 (예시 데이터는 모두 지어낸 것)."""

import json

from conftest import csrf, make_experience

from radio_helper import db, generator, people
from radio_helper.quizbot import answerer, story

MAP = """# 예시 인물 관계도

## 출처 표기

| 태그 | 뜻 |

## 주인공

### 예시아빠 (40세)

- 사는 곳: 대전 〔주신〕
- 직업: 회사원 〔주신〕

## 가족 인물 카드

### 우리 집 · 대전

#### 와이프

- 한 줄 소개: 아내
- 나이: 38세 〔주신〕
- 최애 음식: 떡볶이 〔임의〕

### 외가 · 대구

#### 가나

- 한 줄 소개: 사촌 동생
- 나이: 30세 〔임의〕

## 가족 밖 인물 카드

### 직장 · 예시 회사

#### 홍길동 과장님

- 한 줄 소개: 회사 선배
- 성격: 농담 많음 〔대화〕
- 사건:
  - 2025.01 과장 승진
  - 2025.08 회식에서 노래 〔통화〕

### 대학 친구 모임

#### 김철수

- 관계: 대학 동기 〔대화〕

## 사건 연표

### 사건 · 우리 집

**예시아빠** (주인공)

- 2024.05 새 아파트 입주
- 2025.03 지어낸 일 (R001) 〔사연〕

**와이프** (아내)

- 2023.06 첫째 임신 소식
- 2025.10 숙소 오버부킹에 항의해 더 좋은 호텔로 옮김 〔통화〕
- 2026.01 확인 안 된 일 〔임의〕

## 라디오 사연 사건 (가상)

**와이프**

- 2020.01 가상 사건 (R002) 〔사연〕

## 확인이 필요한 것과 빈칸

- 없음
"""

LOG = "\n".join(json.dumps(r, ensure_ascii=False) for r in [
    {"id": "R001", "source_id": "001", "source_title": "원본 제목", "source_site": "네이트판", "character": "주인공",
     "event_date": "2025.03", "intro": "제 이야기인데요", "event_summary": "요약", "broadcast_timing": "3월",
     "title": "가상 사연 하나", "song": "가수 - 노래", "closing": "한마디", "body": "제 이야기인데요, 본문입니다."},
    {"id": "R002", "source_id": "002", "source_title": "원본", "source_site": "디시", "character": "홍길동",
     "event_date": "2020.01", "intro": "제 회사 선배 이야기인데요", "title": "가상 사연 둘", "body": "본문 둘"},
    {"id": "R003", "character": "와이프", "title": "가상 사연 셋", "body": "본문 셋", "intro": "제 와이프 이야기인데요",
     "source_title": "원본 셋"},
])


def load(conn):
    people.import_people(conn, people.parse_character_map(MAP))
    people.import_library(conn, people.parse_story_log(LOG))


def person(conn, name):
    return conn.execute("SELECT * FROM people WHERE name = ?", (name,)).fetchone()


# ── 읽기 ────────────────────────────────────────────────────────────
def test_parse_character_map():
    ps = {p.name: p for p in people.parse_character_map(MAP)}
    assert list(ps) == ["예시아빠", "와이프", "가나", "홍길동 과장님", "김철수"]
    assert (ps["예시아빠"].side, ps["예시아빠"].age) == ("self", "40세")
    assert (ps["와이프"].side, ps["와이프"].age, ps["와이프"].relation) == ("family", "38세", "아내")
    assert {"key": "최애 음식", "value": "떡볶이", "tag": "임의"} in ps["와이프"].details
    assert ps["홍길동 과장님"].side == "work" and ps["김철수"].side == "friend"
    # 실제 사건만: 〔사연〕 가상 사건과 〔임의〕 사건은 넣지 않는다
    assert [e["text"] for e in ps["예시아빠"].events] == ["새 아파트 입주"]
    assert [e["when"] for e in ps["와이프"].events] == ["2023.06", "2025.10"]
    assert [e["tag"] for e in ps["홍길동 과장님"].events] == ["", "통화"]


def test_default_aliases_and_intro():
    assert people.default_alias("예시아빠", "self", "나") == "나"
    assert people.default_alias("와이프", "family", "우리 집") == "와이프"
    assert people.default_alias("가나", "family", "외가 · 대구") == "사촌"
    assert people.default_alias("홍길동 과장님", "work", "직장 · 예시 회사") == "회사 동료"
    assert people.default_alias("김철수", "friend", "대학 친구 모임") == "대학 친구"
    assert people.intro_for("와이프") == "제 와이프 이야기인데요" and people.intro_for(None) == "제 이야기인데요"


def test_import_people_and_library(conn):
    load(conn)
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 5
    conn.execute("UPDATE people SET alias = '회사 선배', closeness = '가까움' WHERE name = '홍길동 과장님'")
    conn.commit()
    added, updated = people.import_people(conn, people.parse_character_map(MAP))
    assert (added, updated) == (0, 5)
    assert (person(conn, "홍길동 과장님")["alias"], person(conn, "홍길동 과장님")["closeness"]) == ("회사 선배", "가까움")

    rows = conn.execute("SELECT * FROM story_library ORDER BY code").fetchall()
    assert [r["kind"] for r in rows] == ["fiction"] * 3                     # 원본을 옮긴 글은 가상
    assert rows[0]["person_id"] == person(conn, "예시아빠")["id"]           # '주인공' → 나
    assert rows[1]["person_id"] == person(conn, "홍길동 과장님")["id"]      # '홍길동' → '홍길동 과장님'
    assert rows[0]["origin"] == "네이트판 001 원본 제목"
    assert people.import_library(conn, people.parse_story_log(LOG)) == (0, 3)


def test_detect():
    assert people.detect(MAP) == "people" and people.detect(LOG) == "library"
    assert people.detect('{"profile": {}, "experiences": []}') is None


# ── 화자는 나 · 호칭만 · 실명 막기 ───────────────────────────────────
def test_story_material_uses_alias_and_masks_names(conn):
    load(conn)
    wife = person(conn, "와이프")["id"]
    eid = make_experience(conn, story="와이프가 숙소 오버부킹에 항의해 더 좋은 호텔로 옮겼다. 홍길동 과장님도 놀랐다", people="와이프")
    conn.execute("UPDATE experiences SET about_person_id = ? WHERE id = ?", (wife, eid))
    conn.commit()
    item = next(e for e in story.candidate_experiences(conn) if e["id"] == eid)
    assert (item["about"], item["intro"]) == ("와이프", "제 와이프 이야기인데요")
    assert "홍길동" not in item["story"] and "○○" in item["story"]               # 실명은 가림
    msg = answerer.build_story_message("p", "녹취", {}, [item])
    assert "이야기 주인공: 와이프" in msg and "제 와이프 이야기인데요" in msg
    assert "화자는 언제나 보내는 사람 본인('나')" in answerer.STORY_SYSTEM
    warnings = story.message_checks(conn, "홍길동 과장님 이야기", conn.execute(
        "SELECT * FROM experiences WHERE id = ?", (eid,)).fetchone(), "ai")
    assert story.has_block(warnings)                                               # 실명이 들어가면 막음


def test_template_draft_starts_with_relation(conn):
    load(conn)
    eid = make_experience(conn, story="숙소를 바꿨다", highlight="", ending="", quotes="", quote_kind="none")
    exp = conn.execute("SELECT * FROM experiences WHERE id = ?", (eid,)).fetchone()
    d = generator.template_draft(exp, {"nickname": "예시아빠"}, about="와이프", private=["홍길동"])
    assert "제 와이프 이야기인데요.\n숙소를 바꿨다" in d["body"]
    assert "제 이야기인데요" not in generator.template_draft(exp, {"nickname": "예시아빠"})["body"]


# ── 화면 ────────────────────────────────────────────────────────────
def test_import_pages_and_people_screens(client, conn):
    token = csrf(client, "/import")
    r = client.post("/import", data={"csrf_token": token, "text": MAP})
    assert r.headers["Location"].endswith("/people")
    r = client.post("/import", data={"csrf_token": token, "text": LOG})
    assert r.headers["Location"].endswith("/library")

    page = client.get("/people").get_data(as_text=True)
    assert "<svg" in page and "홍길동 과장님 (회사 동료)" in page and "외가 · 1" in page and "화자는 언제나" in page
    pid = person(conn, "홍길동 과장님")["id"]
    page = client.get(f"/people/{pid}").get_data(as_text=True)
    assert "제 회사 동료 이야기인데요" in page and "과장 승진" in page and "가상 · 보내지 않음" in page
    client.post(f"/people/{pid}", data={"csrf_token": token, "alias": "회사 선배", "closeness": "가까움", "note": ""})
    assert person(conn, "홍길동 과장님")["alias"] == "회사 선배"

    # 실제 사건 → 그 사람 이야기로 경험 만들기 (확인 전 상태)
    wife = person(conn, "와이프")["id"]
    event = "숙소 오버부킹에 항의해 더 좋은 호텔로 옮김"
    form = client.get(f"/experiences/new?about={wife}&when=2025.10&text={event}").get_data(as_text=True)
    assert f'<option value="{wife}" selected' in form and f'value="{event}"' in form
    # 본문은 고쳐 저장해도 된다 (짧은 이름으로 알아봄)
    client.post("/experiences/new", data={"csrf_token": token, "story": "숙소를 바꿨다", "when_text": "2025.10",
                                          "label": event, "about_person_id": str(wife), "quote_kind": "none"})
    e = conn.execute("SELECT * FROM experiences WHERE story = '숙소를 바꿨다'").fetchone()
    assert (e["about_person_id"], e["user_confirmed"]) == (wife, 0)
    assert "경험 있음" in client.get(f"/people/{wife}").get_data(as_text=True)

    lib = client.get("/library").get_data(as_text=True)
    assert "가상 3편" in lib and "가상 사연 하나" in lib and "보내지 않고" in lib
    lid = conn.execute("SELECT id FROM story_library WHERE code = 'R001'").fetchone()[0]
    item = client.get(f"/library/{lid}").get_data(as_text=True)
    assert "가상(각색) 사연" in item and "네이트판 001" in item
    assert "<form" not in item.split("</nav>")[1]          # 보내기·전송 버튼이 없다
    assert "가상 사연 둘" in client.get(f"/library?person={pid}").get_data(as_text=True)
    assert "가상 사연 하나" not in client.get(f"/library?person={pid}").get_data(as_text=True)


def test_fiction_never_used_as_story_material(conn):
    load(conn)
    # 보관함 사연은 실제 경험이 아니므로 사연 초안(AI)의 재료 목록에 없다
    assert story.candidate_experiences(conn) == []
    db.log(conn, "test", "ok")
