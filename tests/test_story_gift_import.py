import json

import pytest
from conftest import csrf, make_experience
from test_quizbot import MON_0700, Clock, FakeGorilla, FakeRecorder, ScriptTranscriber, add_schedule, fake_client

from radio_helper import db, importer
from radio_helper.quizbot import answerer, runner, schedule, story
from radio_helper.quizbot.answerer import StoryAnalysis
from radio_helper.quizbot.detector import is_gift_signal, is_story_signal


# ── 신호 ─────────────────────────────────────────────────────────────
def test_story_and_gift_signals():
    assert is_story_signal("오늘의 주제는 첫 출근입니다 사연 보내주세요")
    assert is_story_signal("여러분의 추억 이야기를 들려주세요")
    # '사연'·'신청곡'·'게시판'은 들리기만 해도 신호 (실제 모집인지는 분석에서 가림)
    assert is_story_signal("이 사연 보내주신 분 감사합니다") and is_story_signal("신청곡 받습니다")
    assert is_story_signal("오늘은 게시판으로만 받을게요")
    assert not is_story_signal("다음 곡 듣고 오겠습니다")
    assert is_gift_signal("정답 맞히신 분께 커피 기프티콘 선물 드려요")
    assert not is_gift_signal("다음 곡 듣고 오겠습니다")


# ── 가져오기 ─────────────────────────────────────────────────────────
DATA = {
    "profile": {"nickname": "편집하는 아빠", "tone": "유쾌함", "unknown_key": "x"},
    "experiences": [
        {"label": "편집 화면", "story": "첫째가 편집 화면을 보고 유튜버냐고 물었다", "quotes": "아빠 유튜버야?",
         "gorilla_line": "편집하는 아빠인데 첫째가 저를 유튜버로 압니다 ㅎㅎ"},
        {"label": "빈 경험", "story": ""},
        "잘못된 형식",
    ],
}


def test_import_fills_profile_and_adds_unconfirmed(conn):
    db.save_profile(conn, {"tone": "담백함"})
    report = importer.import_data(conn, importer.parse("```json\n" + json.dumps(DATA, ensure_ascii=False) + "\n```"))
    p = db.get_profile(conn)
    assert p["nickname"] == "편집하는 아빠" and p["tone"] == "담백함"  # 이미 있던 칸은 그대로
    assert report.added == 1 and report.skipped_invalid == 2 and "tone" in report.profile_kept
    assert any("unknown_key" in n for n in report.notes)
    e = conn.execute("SELECT * FROM experiences").fetchone()
    assert e["user_confirmed"] == 0 and e["quote_kind"] == "gist" and e["gorilla_line"].startswith("편집하는 아빠")

    again = importer.import_data(conn, DATA, overwrite_profile=True)
    assert again.added == 0 and again.skipped_duplicates == 1
    assert db.get_profile(conn)["tone"] == "유쾌함"


def test_import_rejects_bad_input(conn):
    with pytest.raises(importer.ImportError_):
        importer.parse("이건 JSON 아님")
    with pytest.raises(importer.ImportError_):
        importer.parse('{"name": 1}')
    with pytest.raises(importer.ImportError_):
        importer.import_data(conn, {"experiences": "목록 아님"})


def test_import_page(client, conn):
    token = csrf(client, "/import")
    assert "파일 형식" in client.get("/import").get_data(as_text=True)
    r = client.post("/import", data={"csrf_token": token, "text": json.dumps(DATA, ensure_ascii=False)},
                    follow_redirects=True)
    assert "경험 1건 추가" in r.get_data(as_text=True)
    assert conn.execute("SELECT COUNT(*) FROM experiences").fetchone()[0] == 1


# ── 사연: 후보·검사 ──────────────────────────────────────────────────
def test_candidate_experiences_mask_and_filter(conn):
    db.save_profile(conn, {"banned_words": "김약사"})
    ok = make_experience(conn, story="김약사님 약국에서 생긴 일", hide="○○약국")
    make_experience(conn, story="확인 안 된 경험", user_confirmed=0)
    cands = story.candidate_experiences(conn)
    assert [c["id"] for c in cands] == [ok] and "김약사" not in json.dumps(cands, ensure_ascii=False)
    conn.execute("INSERT INTO story_posts (program, broadcast_date, topic, experience_id, status, created_at, updated_at) "
                 "VALUES ('p', '2026-10-05', 't', ?, 'entered', ?, ?)", (ok, db.now(), db.now()))
    conn.commit()
    assert story.candidate_experiences(conn) == []  # 이미 보낸 경험은 다시 쓰지 않는다


def test_message_checks(conn):
    eid = make_experience(conn)
    exp = conn.execute("SELECT * FROM experiences WHERE id = ?", (eid,)).fetchone()
    assert story.has_block(story.message_checks(conn, "연락 주세요 010-1234-5678", exp, "ai"))
    assert story.has_block(story.message_checks(conn, "가" * 201, exp, "ai"))
    warns = story.message_checks(conn, "아버지가 수술을 받으셨어요", exp, "ai")
    assert not story.has_block(warns) and any("민감" in w["message"] for w in warns)
    assert story.has_block(story.message_checks(conn, "직접 쓴 글", None, "ai"))
    assert not story.has_block(story.message_checks(conn, "직접 쓴 글", None, "manual"))


def test_claude_story_and_gift_requests():
    client, msgs = fake_client(json.dumps({"is_call": True, "topic": "첫 출근", "gorilla_accepted": "yes",
                                           "deadline_hint": "", "experience_id": 3, "use_user_line": False,
                                           "message": " 첫 출근 날 ... ", "added_facts": ["", "추가함"],
                                           "fit_reason": "주제 일치"}))
    res = answerer.ClaudeAnswerer(client=client).analyze_story(
        "p", "[07:00:00] 오늘의 주제", {"tone": "유쾌함"}, [{"id": 3, "story": "첫 출근", "gorilla_line": "한 줄"}])
    assert res.is_call and res.message == "첫 출근 날 ..." and res.added_facts == ["추가함"]
    call = msgs.calls[0]
    assert call["output_config"]["format"]["schema"] is answerer.STORY_SCHEMA
    assert "#3" in call["messages"][0]["content"] and "직접 쓴 한 줄: 한 줄" in call["messages"][0]["content"]

    client, msgs = fake_client(json.dumps({"gifts": [
        {"gift": "커피 기프티콘", "condition": "정답자 중 추첨", "entry_method": "고릴라", "related": "quiz",
         "deadline": "", "winners": "3명", "announce": ""},
        {"gift": "", "condition": "", "entry_method": "", "related": "x", "deadline": "", "winners": "", "announce": ""}]}))
    gifts = answerer.ClaudeAnswerer(client=client).analyze_gifts("p", "선물")
    assert gifts == [{"gift": "커피 기프티콘", "condition": "정답자 중 추첨", "entry_method": "고릴라",
                      "related": "quiz", "deadline": "", "winners": "3명", "announce": ""}]
    assert msgs.calls[0]["output_config"]["format"]["schema"] is answerer.GIFT_SCHEMA


# ── 실행기: 사연·선물 ────────────────────────────────────────────────
class FullAnswerer:
    def __init__(self, stories=(), gifts=()):
        self.stories, self.gifts = list(stories), list(gifts)
        self.story_calls, self.gift_calls = [], []

    def analyze(self, program, transcript, known):
        from radio_helper.quizbot.answerer import QuizAnalysis
        return QuizAnalysis()  # 퀴즈 아님

    def analyze_story(self, program, transcript, profile, experiences):
        self.story_calls.append(experiences)
        r = self.stories.pop(0)
        return r(experiences) if callable(r) else r

    def analyze_gifts(self, program, transcript):
        self.gift_calls.append(transcript)
        return self.gifts.pop(0)


def make(conn, script, ans, sender=None):
    clock = Clock(MON_0700)
    notes = []
    deps = runner.Deps(recorder_factory=lambda c: FakeRecorder(clock, c),
                       transcriber_factory=lambda m, p: ScriptTranscriber(script),
                       answerer=ans, sender=sender or FakeGorilla(), now=clock.now, sleep=clock.sleep,
                       notify=notes.append)
    r = runner.Runner(conn, deps)
    return r, clock, notes


def run_window(r, clock):
    w = schedule.active_window(r.schedules(), clock.now())
    r.run_window(w)


def story_for(eid, **over):
    kw = dict(is_call=True, topic="첫 출근 날", gorilla_accepted="yes", experience_id=eid,
              message="첫 출근 날 실수로 사장님 커피를 마셨어요", fit_reason="주제와 맞음")
    kw.update(over)
    return StoryAnalysis(**kw)


def test_story_waits_for_confirmation_then_sends(conn):
    add_schedule(conn)
    eid = make_experience(conn, story="첫 출근 날 실수로 사장님 커피를 마셨다", highlight="", ending="")
    sender = FakeGorilla()
    ans = FullAnswerer(stories=[story_for(eid)])
    r, clock, notes = make(conn, {3: "오늘의 주제는 첫 출근 날입니다 사연 보내주세요"}, ans, sender)
    run_window(r, clock)
    post = conn.execute("SELECT * FROM story_posts").fetchone()
    assert post["status"] == "pending" and post["source"] == "ai" and sender.sent == []
    assert "확인 후 전송" in post["decision"] and notes  # 알림
    conn.execute("UPDATE story_posts SET approved = 1")
    conn.commit()
    r.process_approved(None)
    assert sender.sent == ["첫 출근 날 실수로 사장님 커피를 마셨어요"]
    assert conn.execute("SELECT status FROM story_posts").fetchone()[0] == "entered"
    assert story.candidate_experiences(conn) == []  # 보낸 경험은 다시 쓰지 않음


def test_user_line_auto_send_when_allowed(conn):
    add_schedule(conn, story_auto_user_line=1, gorilla_confirmed=1)
    eid = make_experience(conn, gorilla_line="첫 출근 날 사장님 커피를 제 것인 줄 알고 마셨습니다 ㅎㅎ")
    sender = FakeGorilla()
    r, clock, _ = make(conn, {3: "오늘의 주제 첫 출근"},
                       FullAnswerer(stories=[story_for(eid, use_user_line=True, gorilla_accepted="unknown")]), sender)
    run_window(r, clock)
    assert sender.sent == ["첫 출근 날 사장님 커피를 제 것인 줄 알고 마셨습니다 ㅎㅎ"]
    post = conn.execute("SELECT * FROM story_posts").fetchone()
    assert post["source"] == "user_line" and post["decision"] == "직접 쓴 한 줄 자동 전송"


def test_story_skipped_without_confirmed_experiences_and_merges_topics(conn):
    add_schedule(conn)
    ans = FullAnswerer()
    r, clock, _ = make(conn, {3: "오늘의 주제 첫 출근"}, ans)
    run_window(r, clock)
    assert ans.story_calls == []  # 확인된 경험이 없으면 유료 분석을 하지 않는다

    eid = make_experience(conn)
    make_experience(conn, story="다른 실제 경험")  # 첫 경험이 확인 대기 글에 묶여도 분석할 경험이 남도록
    conn.execute("DELETE FROM transcripts")
    conn.commit()
    ans = FullAnswerer(stories=[story_for(eid), story_for(eid, topic="첫 출근 날 이야기")])
    db.set_setting(conn, "quizbot.story_cooldown_seconds", "60")
    r, clock, _ = make(conn, {3: "오늘의 주제 첫 출근", 20: "사연 기다립니다 첫 출근"}, ans)
    run_window(r, clock)
    rows = conn.execute("SELECT * FROM story_posts").fetchall()
    assert len(ans.story_calls) == 2 and len(rows) == 1 and rows[0]["repeat_count"] == 2


def test_blocked_story_never_sends_even_if_approved(conn):
    add_schedule(conn)
    eid = make_experience(conn)
    sender = FakeGorilla()
    r, clock, _ = make(conn, {3: "오늘의 주제"},
                       FullAnswerer(stories=[story_for(eid, message="제 번호 010-1234-5678 로 연락 주세요")]), sender)
    run_window(r, clock)
    conn.execute("UPDATE story_posts SET approved = 1")
    conn.commit()
    r.process_approved(None)
    post = conn.execute("SELECT * FROM story_posts").fetchone()
    assert sender.sent == [] and "막힌 항목" in post["decision"]


def test_no_fitting_experience_records_topic_for_manual(conn):
    add_schedule(conn)
    make_experience(conn)
    r, clock, _ = make(conn, {3: "오늘의 주제 첫 출근"}, FullAnswerer(stories=[story_for(0, message="지어낸 글")]))
    run_window(r, clock)
    post = conn.execute("SELECT * FROM story_posts").fetchone()
    assert post["experience_id"] is None and post["message"] == "" and "맞는 실제 경험이 없음" in post["note"]


def test_gifts_recorded_by_channel_and_merged(conn):
    add_schedule(conn, story_enabled=0)
    g1 = {"gift": "커피 기프티콘", "condition": "정답자 중 추첨", "entry_method": "", "related": "quiz",
          "deadline": "", "winners": "", "announce": ""}
    g2 = dict(g1, gift="커피 기프티콘 3명", entry_method="고릴라 공감로그", winners="3명")
    db.set_setting(conn, "quizbot.gift_cooldown_seconds", "60")
    ans = FullAnswerer(gifts=[[g1], [g2]])
    r, clock, _ = make(conn, {3: "정답 맞히신 분께 기프티콘 선물", 20: "선물은 커피 기프티콘 세 분"}, ans)
    run_window(r, clock)
    rows = conn.execute("SELECT * FROM gift_events").fetchall()
    assert len(ans.gift_calls) == 2 and len(rows) == 1
    g = rows[0]
    assert (g["channel"], g["program"], g["repeat_count"]) == ("파워FM", "김영철의 파워FM", 2)
    assert g["entry_method"] == "고릴라 공감로그" and g["winners"] == "3명" and g["condition"] == "정답자 중 추첨"


# ── 화면 ─────────────────────────────────────────────────────────────
def test_story_and_gift_pages(client, conn):
    sid = add_schedule(conn)
    eid = make_experience(conn, gorilla_line="직접 쓴 한 줄")
    conn.execute("""INSERT INTO story_posts (schedule_id, program, broadcast_date, topic, experience_id, message, warnings,
                    created_at, updated_at) VALUES (?, '김영철의 파워FM', '2026-10-05', '첫 출근', ?, 'AI 초안', '[]', ?, ?)""",
                 (sid, eid, db.now(), db.now()))
    conn.execute("""INSERT INTO story_posts (schedule_id, program, broadcast_date, topic, message, warnings, note,
                    created_at, updated_at) VALUES (?, '김영철의 파워FM', '2026-10-05', '여름휴가', '', '[]', '맞는 경험 없음', ?, ?)""",
                 (sid, db.now(), db.now()))
    conn.execute("""INSERT INTO gift_events (channel, program, broadcast_date, heard_at, gift, condition, related,
                    created_at, updated_at) VALUES ('파워FM', '김영철의 파워FM', '2026-10-05', '2026-10-05 07:20:00',
                    '영화 예매권', '사연 채택', 'story', ?, ?)""", (db.now(), db.now()))
    conn.commit()
    page = client.get("/stories").get_data(as_text=True)
    assert "첫 출근" in page and "AI 초안" in page
    assert client.get("/quizbot/pending.json").get_json()["stories"] == 2
    gifts = client.get("/gifts?channel=파워FM").get_data(as_text=True)
    assert "영화 예매권" in gifts and "사연 채택" in gifts

    assert '<span class="nav-count">2</span>' in page  # 메뉴의 '사연' 옆 확인 대기 수
    token = csrf(client, "/stories")
    resp = client.post("/quizbot/stories/1/approve", data={"csrf_token": token, "message": "직접 쓴 한 줄"})
    assert resp.headers["Location"].endswith("/stories")
    p1 = conn.execute("SELECT * FROM story_posts WHERE id = 1").fetchone()
    assert (p1["approved"], p1["source"]) == (1, "user_line")
    client.post("/quizbot/stories/2/approve", data={"csrf_token": token, "message": "010-1111-2222 연락"})
    assert conn.execute("SELECT approved FROM story_posts WHERE id = 2").fetchone()[0] == 0  # 막힘
    client.post("/quizbot/stories/2/approve", data={"csrf_token": token, "message": "저는 바다로 갔어요"})
    p2 = conn.execute("SELECT * FROM story_posts WHERE id = 2").fetchone()
    assert (p2["approved"], p2["source"]) == (1, "manual")
    client.post("/quizbot/stories/2/skip", data={"csrf_token": token})
    assert conn.execute("SELECT status FROM story_posts WHERE id = 2").fetchone()[0] == "skipped"

    # 경험 화면의 공감로그 한 줄 저장
    client.post(f"/experiences/{eid}", data={"csrf_token": token, "story": "있었던 일", "quote_kind": "none",
                                              "gorilla_line": "새 한 줄", "user_confirmed": "1"})
    assert conn.execute("SELECT gorilla_line FROM experiences WHERE id = ?", (eid,)).fetchone()[0] == "새 한 줄"


def test_schedule_story_options_saved(client, conn):
    token = csrf(client, "/quizbot")
    pid = conn.execute("SELECT id FROM programs WHERE code = 'cultwoshow'").fetchone()[0]
    client.post("/quizbot/schedules", data={"csrf_token": token, "program_id": pid, "days": ["0"],
                                            "story_enabled": "1", "gift_enabled": "1", "story_auto_ai": "1"})
    s = conn.execute("SELECT * FROM quiz_schedules").fetchone()
    assert (s["story_enabled"], s["story_auto_user_line"], s["story_auto_ai"], s["gift_enabled"]) == (1, 0, 1, 1)
    client.post(f"/quizbot/schedules/{s['id']}", data={"csrf_token": token, "enabled": "1", "story_auto_user_line": "1"})
    s = conn.execute("SELECT * FROM quiz_schedules").fetchone()
    assert (s["story_enabled"], s["story_auto_user_line"], s["story_auto_ai"], s["gift_enabled"]) == (0, 1, 0, 0)
