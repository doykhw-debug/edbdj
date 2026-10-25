"""0.76: '문자로만 받는다' 판단은 무시하고 무조건 채팅 — 그 판단으로 못 보낸 정답·사연을 다시 보내기."""

from conftest import csrf, make_experience

from radio_helper import db
from radio_helper.quizbot import runner

OLD_QUIZ_HOLD = "보류: 진행자가 고릴라가 아닌 다른 방법(문자 등)으로 받는다고 함 — '문자로 보내기'를 누르면 보냄"


def add_quiz(conn, decision, at="2026-10-25 09:00:00", status="pending", key="q1"):
    cur = conn.execute(
        "INSERT INTO quizzes (dedupe_key, account, channel, program, broadcast_date, question_key, kind, question, answer, "
        "entry_status, decision, source, created_at, updated_at) "
        "VALUES (?, 'me', 'MBC FM4U', '정오의 희망곡', '2026-10-25', ?, 'new', '과일은?', '사과', ?, ?, 'auto', ?, ?)",
        (key, key, status, decision, at, at))
    conn.commit()
    return cur.lastrowid


def add_story(conn, eid, decision, target="chat", at="2026-10-25 09:00:00"):
    cur = conn.execute(
        "INSERT INTO story_posts (channel, program, broadcast_date, topic, experience_id, message, status, target, decision, "
        "created_at, updated_at) VALUES ('CBS 음악FM', '꿈음', '2026-10-25', '첫 출근', ?, '긴 게시판 글', 'pending', ?, ?, ?, ?)",
        (eid, target, decision, at, at))
    conn.commit()
    return cur.lastrowid


def test_resend_wrong_holds_marks_today_only(conn):
    eid = make_experience(conn, story="첫 출근 날 실수로 사장님 커피를 마셨다", highlight="", ending="", quotes="",
                          quote_kind="none")
    q_today = add_quiz(conn, OLD_QUIZ_HOLD)
    q_conf = add_quiz(conn, "보류: 확신도 0.50 < 기준 0.80", key="q2")                        # 다른 이유는 그대로
    q_old = add_quiz(conn, OLD_QUIZ_HOLD, at="2026-10-24 09:00:00", key="q3")                 # 어제 것은 그대로
    s_chat = add_story(conn, eid, "보류: 진행자가 고릴라가 아닌 다른 방법(문자 등)으로 받는다고 함")
    s_board = add_story(conn, eid, "보류: 게시판용 글 — 원고 검토함에서 게시판에 입력", target="board")
    got = runner.resend_wrong_holds(conn, "2026-10-25 00:00:00")
    assert got == {"quizzes": 1, "stories": 1, "board_to_chat": 1}
    approved = {r[0] for r in conn.execute("SELECT id FROM quizzes WHERE approved = 1")}
    assert approved == {q_today} and q_conf not in approved and q_old not in approved
    assert conn.execute("SELECT approved FROM story_posts WHERE id = ?", (s_chat,)).fetchone()[0] == 1
    chat = conn.execute("SELECT * FROM story_posts WHERE target = 'chat' AND id NOT IN (?)", (s_chat,)).fetchone()
    assert chat["approved"] == 1 and chat["source"] == "approved" and "커피" in chat["message"] and f"#{s_board}" in chat["note"]
    assert runner.resend_wrong_holds(conn, "2026-10-25 00:00:00")["board_to_chat"] == 0        # 두 번 만들지 않음


def test_resend_button_on_screens(client, conn):
    for path in ("/quizzes", "/stories"):
        assert "오늘 못 보낸 정답·사연 채팅으로 다시 보내기" in client.get(path).get_data(as_text=True)
    add_quiz(conn, OLD_QUIZ_HOLD, at=db.now())
    r = client.post("/quizbot/resend-held", data={"csrf_token": csrf(client, "/quizzes"), "next": "/quizzes"},
                    follow_redirects=True)
    assert "채팅으로 다시 보냅니다: 정답 1건" in r.get_data(as_text=True)
    assert conn.execute("SELECT approved FROM quizzes").fetchone()[0] == 1
