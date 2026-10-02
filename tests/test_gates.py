from conftest import make_draft, make_experience, open_corner, target_corner_id

from radio_helper import db, gates


def test_seeded_target_is_only_story_and_song(conn):
    rows = conn.execute("SELECT title FROM corners WHERE is_target = 1").fetchall()
    assert [r["title"] for r in rows] == ["사연과 신청곡 (부제:In your letter)"]
    assert conn.execute("SELECT COUNT(*) FROM corners WHERE recruiting != 'unknown'").fetchone()[0] == 0


def test_unconfirmed_conditions_block_real_fill(conn):
    did = make_draft(conn, make_experience(conn))
    r = gates.evaluate(conn, did, gates.MODE_FILL)
    joined = " ".join(r.blockers)
    assert not r.ok
    assert "모집 중" in joined and "공지" in joined and "승인" in joined and "실제와 같다" in joined


def test_fully_confirmed_draft_passes(conn):
    open_corner(conn)
    did = make_draft(conn, make_experience(conn), status="approved", fact_confirmed=1)
    r = gates.evaluate(conn, did, gates.MODE_FILL)
    assert r.ok, r.blockers


def test_global_stop_blocks_everything(conn):
    open_corner(conn)
    did = make_draft(conn, make_experience(conn), status="approved", fact_confirmed=1)
    db.set_setting(conn, "global_stop", "1")
    for mode in (gates.MODE_MOCK, gates.MODE_FILL):
        assert not gates.evaluate(conn, did, mode).ok
    assert not gates.evaluate_inspect(conn, target_corner_id(conn)).ok


def test_mock_mode_ignores_corner_conditions(conn):
    did = make_draft(conn, make_experience(conn, user_confirmed=0))
    assert gates.evaluate(conn, did, gates.MODE_MOCK).ok


def test_notice_change_requires_review(conn):
    cid = target_corner_id(conn)
    open_corner(conn)
    assert db.update_corner_notice(conn, cid, "사연 모집: 500자 이내") is False
    conn.execute("UPDATE corners SET notice_checked_at = ? WHERE id = ?", (db.now(), cid))
    conn.commit()
    assert db.update_corner_notice(conn, cid, "사연 모집: 500자 이내") is False  # 같은 공지
    assert db.update_corner_notice(conn, cid, "사연 모집: 300자 이내, AI 작성 금지") is True
    corner = conn.execute("SELECT * FROM corners WHERE id = ?", (cid,)).fetchone()
    assert corner["notice_changed"] == 1 and corner["notice_checked_at"] is None
    blockers, _ = gates.corner_blockers(corner)
    assert any("공지가 바뀌었습니다" in b for b in blockers)


def test_past_deadline_and_ai_restriction_block(conn):
    open_corner(conn, deadline="2000-01-01", ai_assist_policy="restricted")
    corner = conn.execute("SELECT * FROM corners WHERE id = ?", (target_corner_id(conn),)).fetchone()
    blockers, _ = gates.corner_blockers(corner)
    assert any("마감" in b for b in blockers) and any("AI" in b for b in blockers)


def test_same_experience_not_sent_twice(conn):
    open_corner(conn)
    eid = make_experience(conn)
    first = make_draft(conn, eid, status="approved", fact_confirmed=1)
    second = make_draft(conn, eid, status="approved", fact_confirmed=1, body="같은 사건을 다르게 쓴 글")
    conn.execute(
        "INSERT INTO submissions (draft_id, experience_id, corner_id, title, body, post_status, created_at, updated_at) "
        "VALUES (?, ?, ?, 't', 'b', 'unknown', ?, ?)", (first, eid, target_corner_id(conn), db.now(), db.now()))
    conn.commit()
    r = gates.evaluate(conn, second, gates.MODE_FILL)
    assert any("같은 경험" in b for b in r.blockers)
    # 실패로 정리된 이력은 다시 시도할 수 있다
    conn.execute("UPDATE submissions SET post_status = 'failed'")
    conn.commit()
    assert gates.evaluate(conn, second, gates.MODE_FILL).ok


def test_personal_info_blocks_fill(conn):
    open_corner(conn)
    did = make_draft(conn, make_experience(conn), status="approved", fact_confirmed=1,
                     body="연락 주세요 010-1234-5678")
    assert any("전화번호" in b for b in gates.evaluate(conn, did, gates.MODE_FILL).blockers)


def test_char_limit_blocks(conn):
    open_corner(conn, char_limit=10)
    did = make_draft(conn, make_experience(conn), status="approved", fact_confirmed=1, body="가" * 11)
    assert any("제한" in b for b in gates.evaluate(conn, did, gates.MODE_FILL).blockers)
