import pytest

from radio_helper import quiz


def base(**over):
    f = dict(program="김영철의 파워FM", broadcast_date="2026-10-02", question_key="1부 퀴즈",
             question="오늘의 표현에 나온 단어는?", deadline="9시", gorilla_accepted="yes")
    f.update(over)
    return f


def test_reannouncement_is_one_item(conn):
    qid, created = quiz.add_or_bump(conn, base())
    qid2, created2 = quiz.add_or_bump(conn, base(question_key="1부  퀴즈!"))
    assert created and not created2 and qid == qid2
    row = conn.execute("SELECT * FROM quizzes WHERE id = ?", (qid,)).fetchone()
    assert row["repeat_count"] == 2


def test_different_date_or_account_is_new(conn):
    a, _ = quiz.add_or_bump(conn, base())
    b, created_b = quiz.add_or_bump(conn, base(broadcast_date="2026-10-03"))
    c, created_c = quiz.add_or_bump(conn, base(account="다른 계정"))
    assert created_b and created_c and len({a, b, c}) == 3


def test_requires_question_identity(conn):
    with pytest.raises(ValueError):
        quiz.add_or_bump(conn, base(question_key="", question=""))


def test_hold_reasons(conn):
    qid, _ = quiz.add_or_bump(conn, base(gorilla_accepted="unknown", deadline=""))
    q = conn.execute("SELECT * FROM quizzes WHERE id = ?", (qid,)).fetchone()
    reasons = " ".join(quiz.hold_reasons(q))
    assert "고릴라 응모" in reasons and "마감" in reasons and "정답 후보" in reasons

    conn.execute("UPDATE quizzes SET gorilla_accepted='yes', deadline='9시', answer='사과', answer_verified=1")
    conn.commit()
    q = conn.execute("SELECT * FROM quizzes WHERE id = ?", (qid,)).fetchone()
    assert quiz.hold_reasons(q) == []
    assert quiz.hold_reasons(q, stopped=True)

    conn.execute("UPDATE quizzes SET kind='rerun'")
    conn.commit()
    q = conn.execute("SELECT * FROM quizzes WHERE id = ?", (qid,)).fetchone()
    assert any("재방송" in r for r in quiz.hold_reasons(q))

    conn.execute("UPDATE quizzes SET kind='new', entry_status='unknown'")
    conn.commit()
    q = conn.execute("SELECT * FROM quizzes WHERE id = ?", (qid,)).fetchone()
    assert any("다시 보내지 않습니다" in r for r in quiz.hold_reasons(q))
