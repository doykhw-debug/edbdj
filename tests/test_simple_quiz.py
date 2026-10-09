"""'퀴즈는 무조건 보내기'(기본 켜짐): 정답 후보가 있으면 객관식·주관식 모두 확신도가 기준보다 낮아도 보낸다
(틀려도 손해가 없으므로).

실제 사례: '영스맵스 두 번째 퀴즈 … (보기: 책 / 외모 추천)' 정답 후보 책, 확신도 0.75 < 기준 0.80 → 보류됐었다.
"""

from test_live import live_runner, start_live
from test_quizbot import FakeGorilla, ScriptAnswerer, analysis

from radio_helper import db
from radio_helper.quizbot import runner
from radio_helper.quizbot.answerer import QuizAnalysis

SCRIPT = {3: "영스맵스 두 번째 퀴즈 나갑니다 1번 책 2번 외모 추천 정답은 고릴라로 보내주세요"}
CHOICE = dict(question="입원해서 심심해할 동기를 위해 사연자님이 병문안 선물로 사 가자고 한 이것은 무엇일까요?",
              options=["책", "외모 추천"], answer="책", confidence=0.75, gorilla_accepted="unknown",
              witty_answer="영스트리트 라디오! 들으면 하나도 안 심심해요", witty_point="셀프 홍보식 반전", wit_score=0.45)
OPEN = dict(CHOICE, options=[], question="오늘 사연자가 병문안 선물로 고른 물건은?", confidence=0.4)


def listen(conn, result, **flags):
    start_live(conn, channel="파워FM", **flags)
    sender = FakeGorilla()
    r, _ = live_runner(conn, SCRIPT, ScriptAnswerer([result]), sender, n=10)
    r.run_live()
    return sender, conn.execute("SELECT * FROM quizzes WHERE source = 'auto'").fetchone()


def test_choice_quiz_sent_even_below_confidence(conn):
    sender, q = listen(conn, analysis(**CHOICE))
    assert sender.sent == ["책"] and q["entry_status"] == "entered"
    assert "무조건 보내기" in q["decision"] and "0.75" in q["decision"]


def test_open_question_sent_even_below_confidence(conn):
    sender, q = listen(conn, analysis(**OPEN))
    assert sender.sent == ["책"] and "확신도 0.40여도 보냄" in q["decision"]


def test_turned_off_waits_below_confidence(conn):
    sender, q = listen(conn, analysis(**OPEN), quiz_always="0")
    assert sender.sent == [] and q["entry_status"] == "pending" and "확신도 0.40 < 기준 0.80" in q["decision"]


def test_no_answer_candidate_still_waits(conn):
    sender, q = listen(conn, analysis(**dict(OPEN, answer="", witty_answer="")))
    assert sender.sent == [] and "정답 후보 없음" in q["decision"]


def test_still_respects_hourly_limit(conn):
    db.set_setting(conn, "quizbot.max_sends_per_hour", "0")
    sender, q = listen(conn, analysis(**CHOICE))
    assert sender.sent == [] and "상한" in q["decision"]


def test_unsure_quiz_sends_best_answer_not_forced_witty(conn):
    res = QuizAnalysis.from_json(__import__("json").dumps(analysis(**{**CHOICE, "wit_score": 0.9})))
    # 확신이 낮으면 예전에는 기발한 오답으로 바꿨지만, '무조건 보내기'면 가장 그럴듯한 답을 보낸다
    assert runner.choose_answer(res, 0.8, 0.3, 0.7, roll=0.9) == "witty"
    assert runner.choose_answer(res, 0.8, 0.3, 0.7, roll=0.9, simple=True) == "correct"
    assert runner.choose_answer(res, 0.8, 0.3, 0.7, roll=0.1, simple=True) == "witty"   # 섞기 비율은 그대로
    assert runner.always_send(conn, "책") and not runner.always_send(conn, " ")
    db.set_setting(conn, "live.quiz_always", "0")
    assert not runner.always_send(conn, "책")


def test_home_has_the_option_checked_by_default(client):
    page = client.get("/").get_data(as_text=True)
    assert 'name="quiz_always" value="1" checked' in page and "퀴즈는 무조건 보내기" in page
