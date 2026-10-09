"""기발한 오답 · 채팅창 교차 분석 · 힌트 · 게시판에만 받는 사연 · 신청곡."""

import json

from conftest import csrf, make_experience
from test_live import live_runner, start_live
from test_quizbot import FakeGorilla, analysis, fake_client
from test_story_gift_import import FullAnswerer, story_for

import radio_helper.app as app_module
from radio_helper import db
from radio_helper.quizbot import answerer, gorilla, runner
from radio_helper.quizbot.answerer import QuizAnalysis
from radio_helper.quizbot.detector import is_quiz_signal

WITTY = dict(witty_answer="제 월급이요", witty_point="'통장에서 금방 사라지는 것'을 월급으로 비틈", wit_score=0.9)


def qa(**over):
    return QuizAnalysis.from_json(json.dumps(analysis(**{**WITTY, "fun_welcome": False, **over})))


# ── 정답 / 기발한 오답 고르기 ───────────────────────────────────────
def test_choose_answer():
    pick = runner.choose_answer
    assert pick(qa(confidence=0.95), 0.8, 0.3, 0.7, roll=0.9) == "correct"      # 확실하면 대체로 정답
    assert pick(qa(confidence=0.95), 0.8, 0.3, 0.7, roll=0.1) == "witty"        # 30%는 오답
    assert pick(qa(confidence=0.4), 0.8, 0.3, 0.7, roll=0.9) == "witty"         # 불확실하면 오답
    assert pick(qa(fun_welcome=True), 0.8, 0.3, 0.7, roll=0.9) == "witty"       # 진행자가 재밌는 오답 환영
    assert pick(qa(confidence=0.4, wit_score=0.5), 0.8, 0.3, 0.7, 0.1) == "correct"   # 덜 웃기면 쓰지 않음
    assert pick(qa(confidence=0.4), 0.8, 0.0, 0.7, roll=0.0) == "correct"       # 섞기 끔
    # 웃긴 포인트가 없는 오답은 읽을 때부터 버린다
    a = qa(witty_point="")
    assert (a.witty_answer, a.wit_score) == ("", 0.0)
    assert pick(a, 0.8, 1.0, 0.7, 0.0) == "correct"


def test_keywords_trigger_quiz():
    assert is_quiz_signal("힌트 하나 더 드릴게요") and is_quiz_signal("재밌는 오답도 환영")


class ChatAnswerer:
    """퀴즈 분석 흉내: 받은 채팅창 사진을 기록한다."""
    def __init__(self, results):
        self.results, self.calls = list(results), []

    def analyze(self, program, transcript, known, images=()):
        self.calls.append({"known": known, "images": list(images)})
        r = self.results.pop(0)
        return QuizAnalysis.from_json(json.dumps(r))


class ChatGorilla(FakeGorilla):
    def __init__(self):
        super().__init__()
        self.shots = 0

    def chat_image(self, save_as=None):
        self.shots += 1
        return f"jpeg{self.shots}".encode()


def run_quiz(conn, results, script, n=10, sender=None, **flags):
    start_live(conn, **flags)
    ans = ChatAnswerer(results)
    r, _ = live_runner(conn, script, ans, sender or FakeGorilla(), n=n)
    r.run_live()
    return ans, r


QUIZ = {3: "오늘의 퀴즈 통장에서 금방 사라지는 것은? 정답은 채팅으로"}


def test_low_confidence_quiz_sends_witty_answer(conn):
    sender = FakeGorilla()
    # '퀴즈는 무조건 보내기'를 끈 경우: 확신이 낮으면 기발한 오답으로 (켜 두면 가장 그럴듯한 답 — test_simple_quiz)
    run_quiz(conn, [analysis(confidence=0.5, **WITTY)], QUIZ, sender=sender, quiz_always="0")
    q = conn.execute("SELECT * FROM quizzes WHERE source = 'auto'").fetchone()
    assert sender.sent == ["제 월급이요"] and q["answer_kind"] == "witty" and q["answer"] == "사과"
    assert any("기발한 오답 '제 월급이요'" in e["message"] for e in conn.execute("SELECT message FROM events"))


def test_witty_toggle_off_keeps_correct_answer(conn):
    sender = FakeGorilla()
    run_quiz(conn, [analysis(confidence=0.5, **WITTY)], QUIZ, sender=sender, witty="0", quiz_always="0")
    q = conn.execute("SELECT * FROM quizzes WHERE source = 'auto'").fetchone()
    assert sender.sent == [] and q["answer_kind"] == "correct" and "확신도" in q["decision"]


def test_chat_screens_collected_from_keyword_and_sent_with_transcript(conn):
    db.set_setting(conn, "gorilla.chat_rect", "0.05,0.1,0.95,0.8")
    chat = ChatGorilla()
    ans, _ = run_quiz(conn, [analysis()], QUIZ, sender=chat, channel="파워FM")
    images = ans.calls[0]["images"]
    # 키워드가 들린 뒤부터 10초마다 찍고, 분석 땐 첫 장 + 최근 2장만 보낸다
    assert chat.shots >= 4 and len(images) == 3 and images[0][1] == b"jpeg1"
    assert images[-1][1] == f"jpeg{chat.shots}".encode()
    q = conn.execute("SELECT * FROM quizzes WHERE source = 'auto'").fetchone()
    assert q["chat_shots"] == 3
    assert any("채팅창 3장 함께 분석" in e["message"] for e in conn.execute("SELECT message FROM events"))


def test_no_chat_area_means_transcript_only(conn):
    ans, _ = run_quiz(conn, [analysis()], QUIZ, sender=ChatGorilla(), channel="파워FM")
    assert ans.calls[0]["images"] == []


def test_hint_raises_confidence_and_sends(conn):
    script = {3: "오늘의 퀴즈 나갑니다 빨갛고 둥근 과일", 20: "힌트 드릴게요 백설공주가 먹은 과일"}
    sender = FakeGorilla()

    def reannounce():
        return analysis(kind="reannouncement", duplicate_of=1, confidence=0.95)

    db.set_setting(conn, "quizbot.witty_ratio", "0")
    run_quiz(conn, [analysis(confidence=0.5), reannounce()], script, n=30, sender=sender, quiz_always="0")
    q = conn.execute("SELECT * FROM quizzes WHERE source = 'auto'").fetchone()
    assert sender.sent == ["사과"] and q["confidence"] == 0.95 and q["repeat_count"] == 2


def test_claude_request_includes_chat_images():
    client, msgs = fake_client(json.dumps(analysis(**WITTY, fun_welcome=False)))
    res = answerer.ClaudeAnswerer(client=client).analyze("p", "녹취", [], images=[("07:10:00", b"\xff\xd8jpeg")])
    content = msgs.calls[0]["messages"][0]["content"]
    assert [b["type"] for b in content] == ["text", "image", "text"]
    assert content[0]["text"] == "[채팅창 화면 07:10:00]" and content[1]["source"]["media_type"] == "image/jpeg"
    assert res.witty_answer == "제 월급이요"
    assert "기발한 오답" in answerer.SYSTEM and "채팅창 화면" in answerer.STORY_SYSTEM


def test_chat_area_selection_is_read_only():
    info = {"title": "GOREALRA", "process": "gorealra.exe", "rect": (0, 0, 400, 800)}
    settings, message = gorilla.region_settings("chat", (20, 80, 380, 600), info, None)
    assert settings["gorilla.chat_rect"] == "0.05,0.1,0.95,0.75"
    assert "gorilla.input_mode" not in settings and "gorilla.send_mode" not in settings and "채팅창" in message


# ── 사연: 게시판에만 / 신청곡 ───────────────────────────────────────
def run_story(conn, res, n=8):
    start_live(conn, channel="파워FM", auto_story="1")
    sender = FakeGorilla()
    r, _ = live_runner(conn, {3: "오늘의 주제는 첫 출근 날 사연은 게시판에만 남겨 주세요"}, FullAnswerer(stories=[res]),
                       sender, n=n, start=runner_start())
    r.run_live()
    return sender


def runner_start():
    from test_quizbot import MON_0700
    from datetime import timedelta
    return MON_0700 + timedelta(minutes=30)   # 김영철의 파워FM 시간 (게시판 '사연과 신청곡' 코너가 있음)


def test_board_only_story_becomes_board_draft(conn):
    eid = make_experience(conn, story="첫 출근 날 실수로 사장님 커피를 마셨다", highlight="", ending="", quotes="",
                          quote_kind="none")
    sender = run_story(conn, story_for(eid, board_only=True, board_title="첫 출근 날의 커피",
                                       board_body="첫 출근 날 실수로 사장님 커피를 마셨습니다."))
    post = conn.execute("SELECT * FROM story_posts").fetchone()
    assert sender.sent == [] and post["target"] == "board" and post["draft_id"]
    draft = conn.execute("SELECT * FROM drafts WHERE id = ?", (post["draft_id"],)).fetchone()
    assert (draft["title"], draft["status"], draft["source"]) == ("첫 출근 날의 커피", "draft", "pasted")
    assert "원고 검토함" in post["decision"]


def test_board_only_without_board_known(conn):
    conn.execute("DELETE FROM corners")
    conn.commit()
    eid = make_experience(conn)
    run_story(conn, story_for(eid, board_only=True, board_body="본문"))
    post = conn.execute("SELECT * FROM story_posts").fetchone()
    assert post["draft_id"] is None and "코너" in post["decision"]


def test_song_only_from_experience(conn):
    eid = make_experience(conn, story="첫 출근 날 실수로 사장님 커피를 마셨다", highlight="", ending="", quotes="",
                          quote_kind="none", song="아이유 - 좋은 날")
    sender = run_story(conn, story_for(eid, gorilla_accepted="yes", song="아이유 - 좋은 날"))
    assert sender.sent == ["첫 출근 날 실수로 사장님 커피를 마셨어요 (신청곡: 아이유 - 좋은 날)"]


def test_invented_song_is_dropped(conn):
    eid = make_experience(conn, story="첫 출근 날 실수로 사장님 커피를 마셨다", highlight="", ending="", quotes="",
                          quote_kind="none")
    sender = run_story(conn, story_for(eid, gorilla_accepted="yes", song="지어낸 노래"))
    assert sender.sent == ["첫 출근 날 실수로 사장님 커피를 마셨어요"]


# ── 화면 ────────────────────────────────────────────────────────────
def test_pages_show_witty_board_and_chat_tools(client, conn, monkeypatch):
    launched = []
    monkeypatch.setattr(app_module, "launch_quizbot", lambda args: launched.append(args) or "q.log")
    conn.execute("""INSERT INTO quizzes (dedupe_key, account, channel, program, broadcast_date, question_key, question,
                    answer, confidence, source, answer_kind, witty_answer, witty_point, wit_score, created_at, updated_at)
                    VALUES ('k', 'a', '파워FM', 'p', '2026-10-05', 'q', '문제', '사과', 0.5, 'auto', 'witty',
                    '제 월급이요', '월급으로 비틈', 0.9, ?, ?)""", (db.now(), db.now()))
    conn.commit()
    page = client.get("/quizzes").get_data(as_text=True)
    assert "기발한 오답" in page and "제 월급이요" in page and 'value="제 월급이요"' in page
    token = csrf(client, "/quizzes")
    client.post("/quizbot/quizzes/1/approve", data={"csrf_token": token, "answer": "사과", "send_text": "제 월급이요"})
    assert conn.execute("SELECT answer_kind FROM quizzes").fetchone()[0] == "witty"

    assert "기발한 오답 섞기" in client.get("/").get_data(as_text=True)
    client.post("/listen/start", data={"csrf_token": token, "channel": "파워FM", "route": "app"})
    assert db.get_setting(conn, "live.witty") == "0"   # 체크하지 않으면 끔

    page = client.get("/gorilla?app=mini").get_data(as_text=True)
    assert "④ 채팅창 영역 지정" in page
    client.post("/quizbot/tool", data={"csrf_token": token, "tool": "select-chat", "app": "mini"})
    client.post("/quizbot/tool", data={"csrf_token": token, "tool": "chat-test", "app": "mini"})
    assert launched[-2:] == [["select", "chat", "--app", "mini"], ["chat-test", "--app", "mini"]]
