"""한 번 보낸 사연(경험)은 같은 방송국에 다시 보내지 않는다 + 보낸 사연 목록."""

from conftest import csrf, make_draft, make_experience, open_corner

from radio_helper import db, gates
from radio_helper.quizbot import config, story


def post(conn, eid, channel, program="방송", status="entered", via="gorilla", message="보낸 글"):
    cur = conn.execute(
        "INSERT INTO story_posts (program, broadcast_date, topic, experience_id, message, status, channel, sent_via, "
        "sent_at, created_at, updated_at) VALUES (?, '2026-10-09', '오늘의 주제', ?, ?, ?, ?, ?, ?, ?, ?)",
        (program, eid, message, status, channel, via, db.now(), db.now(), db.now()))
    conn.commit()
    return cur.lastrowid


def ids(items):
    return [e["id"] for e in items]


def test_broadcaster_of():
    f = config.broadcaster_of
    assert [f(c) for c in ("파워FM", "러브FM", "고릴라M", "SBS 파워FM", None, "")] == ["SBS"] * 6
    assert f("MBC FM4U") == "MBC" and f("KBS 쿨FM") == "KBS" and f("KBS 1라디오") == "KBS"
    assert f("CBS 음악FM") == "CBS 음악FM"


def test_same_broadcaster_blocked_other_allowed(conn):
    a, b = make_experience(conn, label="가"), make_experience(conn, label="나")
    post(conn, a, "파워FM")
    assert ids(story.candidate_experiences(conn, "SBS")) == [b]          # 러브FM 이어도 같은 SBS 라 안 씀
    assert ids(story.candidate_experiences(conn, "MBC")) == [a, b]       # 다른 방송국은 보낼 수 있음
    post(conn, a, "MBC FM4U", via="mini")
    assert ids(story.candidate_experiences(conn, "MBC")) == [b] and ids(story.candidate_experiences(conn, "KBS")) == [a, b]
    assert story.sent_broadcasters(conn)[a] == {"SBS", "MBC"}


def test_old_posts_and_boards_count_as_sbs(conn):
    a, b, c = (make_experience(conn, label=x) for x in "가나다")
    post(conn, a, None, program="두시탈출 컬투쇼")                        # 채널 없는 예전 기록 → 프로그램 채널(SBS)
    post(conn, b, "KBS 쿨FM", status="skipped")                          # 보내지 않은 글은 안 셈
    open_corner(conn)
    did = make_draft(conn, c, status="approved", fact_confirmed=1)
    conn.execute("INSERT INTO submissions (draft_id, experience_id, corner_id, post_status, created_at, updated_at) "
                 "SELECT id, experience_id, corner_id, 'unknown', ?, ? FROM drafts WHERE id = ?", (db.now(), db.now(), did))
    conn.commit()
    assert ids(story.candidate_experiences(conn, "SBS")) == [b]
    assert ids(story.candidate_experiences(conn, "KBS")) == [a, b, c]


def test_board_gate_only_blocks_sbs(conn):
    open_corner(conn)
    eid = make_experience(conn)
    did = make_draft(conn, eid, status="approved", fact_confirmed=1)
    post(conn, eid, "MBC FM4U", via="mini")
    assert gates.evaluate(conn, did, gates.MODE_FILL).ok                 # MBC 에만 보냈으면 SBS 게시판 가능
    post(conn, eid, "러브FM")
    r = gates.evaluate(conn, did, gates.MODE_FILL)
    assert not r.ok and any("같은 방송국" in b for b in r.blockers)


def test_approve_blocks_same_broadcaster(client, conn):
    eid = make_experience(conn)
    post(conn, eid, "파워FM")
    again = post(conn, eid, "러브FM", status="pending")
    other = post(conn, eid, "KBS 쿨FM", status="pending", via="kong")
    token = csrf(client, "/stories")
    client.post(f"/quizbot/stories/{again}/approve", data={"csrf_token": token, "message": "보낸 글"})
    row = conn.execute("SELECT approved, warnings FROM story_posts WHERE id = ?", (again,)).fetchone()
    assert row["approved"] == 0 and "SBS에 이미 보냈습니다" in row["warnings"]
    client.post(f"/quizbot/stories/{other}/approve", data={"csrf_token": token, "message": "보낸 글"})
    assert conn.execute("SELECT approved FROM story_posts WHERE id = ?", (other,)).fetchone()[0] == 1


def test_sent_list_page(client, conn):
    a = make_experience(conn, label="편집 화면")
    post(conn, a, "파워FM", program="김영철의 파워FM", message="첫째가 유튜버냐고 물었어요")
    post(conn, a, "MBC FM4U", program="정오의 희망곡", via="sms", status="unknown", message="문자로 보낸 글")
    post(conn, a, "KBS 쿨FM", status="skipped", message="안 보낸 글")
    page = client.get("/stories/sent").get_data(as_text=True)
    assert "첫째가 유튜버냐고 물었어요" in page and "문자로 보낸 글" in page and "안 보낸 글" not in page
    assert "SBS 1" in page and "MBC 1" in page and "전체 2" in page and "결과 불명" in page
    matrix = page.split("경험별로 보낸 방송국")[1]
    assert matrix.count(">✓<") == 2 and "편집 화면" in matrix
    only_mbc = client.get("/stories/sent?b=MBC").get_data(as_text=True)
    assert "문자로 보낸 글" in only_mbc and "첫째가 유튜버냐고" not in only_mbc
    assert "보낸 사연 목록" in client.get("/stories").get_data(as_text=True)
