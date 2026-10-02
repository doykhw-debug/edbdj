import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest
from conftest import csrf

from radio_helper import app as app_module
from radio_helper import db
from radio_helper.quizbot import answerer, config, gorilla, runner, schedule
from radio_helper.quizbot.answerer import AnswererError, QuizAnalysis
from radio_helper.quizbot.audio import rms, to_mono_16k
from radio_helper.quizbot.detector import Detector, TranscriptBuffer, is_quiz_signal

MON_0700 = datetime(2026, 10, 5, 7, 0, 0)  # 월요일


# ── 예약 ─────────────────────────────────────────────────────────────
def sched(**over):
    s = {"id": 1, "enabled": 1, "days": "01234", "start_time": "07:00", "end_time": "09:00"}
    s.update(over)
    return s


def test_active_window_and_days():
    assert schedule.active_window([sched()], MON_0700 + timedelta(minutes=30)).end == MON_0700 + timedelta(hours=2)
    assert schedule.active_window([sched()], MON_0700 - timedelta(minutes=1)) is None
    saturday = datetime(2026, 10, 10, 8, 0)
    assert schedule.active_window([sched()], saturday) is None
    assert schedule.active_window([sched(enabled=0)], MON_0700) is None


def test_window_crossing_midnight():
    s = sched(start_time="23:00", end_time="01:00", days="4")  # 금요일 밤
    fri = datetime(2026, 10, 9, 23, 30)
    w = schedule.active_window([s], fri)
    assert w and w.broadcast_date == "2026-10-09"
    sat_early = datetime(2026, 10, 10, 0, 30)  # 토요일 새벽이지만 금요일 예약의 연장
    assert schedule.active_window([s], sat_early).broadcast_date == "2026-10-09"
    assert schedule.active_window([s], datetime(2026, 10, 10, 1, 0)) is None


def test_next_window_and_labels():
    n = schedule.next_window([sched()], datetime(2026, 10, 9, 10, 0))  # 금요일 10시 → 다음 월요일 7시
    assert n.start == datetime(2026, 10, 12, 7, 0)
    assert schedule.days_label("0123456") == "매일" and schedule.days_label("01234") == "평일"
    assert schedule.days_label("13") == "화목"


# ── 감지·녹음 ────────────────────────────────────────────────────────
def test_quiz_signal():
    assert is_quiz_signal("자 오늘의 퀴즈 나갑니다")
    assert is_quiz_signal("정답은 고릴라로 보내 주세요")
    assert is_quiz_signal("초성 힌트 드릴게요")
    assert not is_quiz_signal("사연은 고릴라로 보내 주세요")
    assert not is_quiz_signal("다음 곡 듣고 오겠습니다")


def test_detector_settle_and_cooldown():
    d = Detector(settle_seconds=40, cooldown_seconds=60)
    t0 = MON_0700
    d.feed(t0, "오늘의 퀴즈")
    assert not d.is_due(t0 + timedelta(seconds=39)) and d.is_due(t0 + timedelta(seconds=40))
    d.mark_analyzed(t0 + timedelta(seconds=40))
    d.feed(t0 + timedelta(seconds=45), "정답 다시 알려드려요")
    assert d.due_at == t0 + timedelta(seconds=100)  # 쿨다운이 더 늦다


def test_transcript_buffer_window():
    b = TranscriptBuffer(max_seconds=300)
    b.add(MON_0700, "첫 줄")
    b.add(MON_0700 + timedelta(seconds=200), "둘째 줄")
    text = b.window(MON_0700 + timedelta(seconds=210), 60)
    assert "둘째 줄" in text and "첫 줄" not in text


def test_audio_conversion():
    stereo_48k = (np.ones(48_000 * 2, dtype=np.int16) * 16384).tobytes()  # 1초, 2채널
    out = to_mono_16k(stereo_48k, channels=2, rate=48_000)
    assert len(out) == 16_000 and abs(out.mean() - 0.5) < 1e-3
    assert rms(np.zeros(100, dtype=np.float32)) == 0.0 and rms(out) > 0.4
    assert len(to_mono_16k(b"", 2, 48_000)) == 0


# ── 분석 ─────────────────────────────────────────────────────────────
def analysis(**over):
    data = {"kind": "new_question", "duplicate_of": 0, "question": "철수가 좋아하는 과일은?", "options": [],
            "answer": "사과", "confidence": 0.9, "gorilla_accepted": "yes", "entry_instructions": "고릴라로",
            "deadline_hint": "9시 전", "formatted_message": "", "reasoning_note": "진행자가 문제를 냄"}
    data.update(over)
    return data


def test_analysis_from_json_normalizes():
    a = QuizAnalysis.from_json(json.dumps(analysis(answer=' "사과" ', confidence=3, kind="weird",
                                                   gorilla_accepted="maybe")))
    assert (a.answer, a.confidence, a.kind, a.gorilla_accepted) == ("사과", 1.0, "not_quiz", "unknown")


class FakeMessages:
    def __init__(self, response):
        self.response, self.calls = response, []

    def create(self, **kw):
        self.calls.append(kw)
        return self.response


def fake_client(text, stop_reason="end_turn"):
    resp = SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="thinking", thinking=""),
                                                              SimpleNamespace(type="text", text=text)])
    msgs = FakeMessages(resp)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def test_claude_answerer_request_and_parse():
    client, msgs = fake_client(json.dumps(analysis()))
    a = answerer.ClaudeAnswerer(client=client)
    res = a.analyze("김영철의 파워FM", "[07:10:00] 오늘의 퀴즈", [(3, "이전 문제")])
    assert res.answer == "사과" and res.kind == "new_question"
    call = msgs.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["effort"] == "medium"
    assert "3. 이전 문제" in call["messages"][0]["content"]
    assert set(answerer.SCHEMA["required"]) == set(answerer.SCHEMA["properties"])


def test_claude_answerer_refusal_and_bad_json():
    client, _ = fake_client("", stop_reason="refusal")
    with pytest.raises(AnswererError):
        answerer.ClaudeAnswerer(client=client).analyze("p", "t", [])
    client, _ = fake_client("not json")
    with pytest.raises(AnswererError):
        answerer.ClaudeAnswerer(client=client).analyze("p", "t", [])


# ── 고릴라 (순수 계산 부분) ──────────────────────────────────────────
def test_gorilla_geometry_and_judgement():
    rect = (100, 200, 500, 1000)
    fx, fy = gorilla.point_to_fraction(rect, 300, 900)
    assert (fx, fy) == (0.5, 0.875) and gorilla.fraction_to_point(rect, fx, fy) == (300, 900)
    with pytest.raises(gorilla.GorillaError):
        gorilla.point_to_fraction(rect, 50, 50)
    assert gorilla.judge_result("uia", "", "사과", True).status == "posted"
    assert gorilla.judge_result("uia", "", "사과", False).status == "entered"
    assert gorilla.judge_result("uia", "사과", "사과", False).status == "unknown"
    assert gorilla.judge_result("coords", None, "사과", False).status == "entered"
    assert config.format_message("[퀴즈] {answer}", " 사과 ") == "[퀴즈] 사과"
    assert config.format_message("정답:", "사과") == "정답: 사과"


# ── 실행기 (가짜 녹음·인식·분석·고릴라) ─────────────────────────────
class Clock:
    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


class FakeRecorder:
    def __init__(self, clock, chunk):
        self.clock, self.chunk = clock, chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read_chunk(self):
        self.clock.sleep(self.chunk)
        return np.full(16_000, 0.1, dtype=np.float32)


class ScriptTranscriber:
    """녹음 단위 번호 → 녹취 문장"""
    def __init__(self, script):
        self.script, self.i = script, 0

    def transcribe(self, audio):
        self.i += 1
        return self.script.get(self.i, "음악이 흐릅니다")


class ScriptAnswerer:
    def __init__(self, results):
        self.results, self.calls = list(results), []

    def analyze(self, program, transcript, known):
        self.calls.append((program, transcript, known))
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return QuizAnalysis.from_json(json.dumps(r)) if isinstance(r, dict) else r(known)


class FakeGorilla:
    def __init__(self, running=True, result="entered", explode=False):
        self.running, self.result, self.explode, self.sent = running, result, explode, []

    def is_running(self):
        return self.running

    def send(self, text):
        self.sent.append(text)
        if self.explode:
            raise RuntimeError("창이 사라짐")
        return gorilla.SendResult(self.result, "테스트")


def add_schedule(conn, **over):
    pid = conn.execute("SELECT id FROM programs WHERE code = '0chulpowerfm'").fetchone()[0]
    v = dict(days="01234", start_time="07:00", end_time="07:10", auto_submit=1, min_confidence=0.8,
             gorilla_confirmed=0, enabled=1)
    v.update(over)
    cur = conn.execute(
        f"INSERT INTO quiz_schedules (program_id, {', '.join(v)}, created_at, updated_at) "
        f"VALUES (?, {', '.join('?' * len(v))}, ?, ?)", (pid, *v.values(), db.now(), db.now()))
    conn.commit()
    return cur.lastrowid


def make_runner(conn, script, results, sender=None, start=MON_0700):
    clock = Clock(start)
    ans = ScriptAnswerer(results)
    sender = sender or FakeGorilla()
    deps = runner.Deps(recorder_factory=lambda chunk: FakeRecorder(clock, chunk),
                       transcriber_factory=lambda model, program: ScriptTranscriber(script),
                       answerer=ans, sender=sender, now=clock.now, sleep=clock.sleep)
    return runner.Runner(conn, deps), clock, ans, sender


def run_one_window(conn, r, clock):
    sched_rows = r.schedules()
    w = schedule.active_window(sched_rows, clock.now())
    assert w is not None
    r.run_window(w)
    return w


def auto_quizzes(conn):
    return conn.execute("SELECT * FROM quizzes WHERE source = 'auto' ORDER BY id").fetchall()


def test_full_window_sends_once_and_handles_reannounce_and_reveal(conn):
    add_schedule(conn)
    script = {3: "자 오늘의 퀴즈 나갑니다 철수가 좋아하는 과일은 정답은 고릴라로 보내주세요",
              12: "퀴즈 다시 한번 알려드립니다 철수가 좋아하는 과일",
              24: "정답 발표하겠습니다 정답은 사과였습니다"}

    def reannounce(known):
        return QuizAnalysis.from_json(json.dumps(analysis(kind="reannouncement", duplicate_of=known[0][0])))

    r, clock, ans, sender = make_runner(conn, script, [
        analysis(), reannounce, analysis(kind="answer_reveal", answer="사과", question="철수가 좋아하는 과일")])
    run_one_window(conn, r, clock)

    assert sender.sent == ["사과"]                     # 재안내·정답 발표 때 다시 보내지 않음
    assert len(ans.calls) == 3
    qs = auto_quizzes(conn)
    assert len(qs) == 1
    q = qs[0]
    assert q["entry_status"] == "entered" and q["repeat_count"] == 2 and q["decision"] == "자동 전송"
    assert "방송 정답 발표: 사과" in q["note"]
    assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 40  # 10분 / 15초
    assert clock.now() >= MON_0700 + timedelta(minutes=10)


def test_held_when_unsure_then_sent_after_approval(conn):
    add_schedule(conn)
    script = {2: "오늘의 퀴즈 철수가 좋아하는 과일은"}
    r, clock, _ans, sender = make_runner(conn, script, [analysis(confidence=0.5, gorilla_accepted="unknown")])
    run_one_window(conn, r, clock)
    q = auto_quizzes(conn)[0]
    assert sender.sent == [] and q["entry_status"] == "pending"
    assert "확신도" in q["decision"] and "고릴라 응모 인정 여부 미확인" in q["decision"]

    # 사용자가 화면에서 답을 고쳐 승인 → 실행기가 보낸다
    conn.execute("UPDATE quizzes SET approved = 1, answer = '바나나' WHERE id = ?", (q["id"],))
    conn.commit()
    r.process_approved(None)
    assert sender.sent == ["바나나"]
    assert auto_quizzes(conn)[0]["decision"] == "사용자 승인 후 전송"


def test_manual_mode_never_auto_sends(conn):
    add_schedule(conn, auto_submit=0)
    r, clock, _a, sender = make_runner(conn, {2: "오늘의 퀴즈"}, [analysis()])
    run_one_window(conn, r, clock)
    assert sender.sent == [] and "자동 전송이 꺼져" in auto_quizzes(conn)[0]["decision"]


def test_gorilla_confirmed_allows_unknown_channel_but_not_no(conn):
    add_schedule(conn, gorilla_confirmed=1)
    r, clock, _a, sender = make_runner(conn, {2: "오늘의 퀴즈", 20: "두 번째 퀴즈 문제 나갑니다"}, [
        analysis(gorilla_accepted="unknown"),
        analysis(question="영희가 좋아하는 색은?", answer="파랑", gorilla_accepted="no")])
    run_one_window(conn, r, clock)
    assert sender.sent == ["사과"]
    second = auto_quizzes(conn)[1]
    assert second["entry_status"] == "pending" and "다른 방법" in second["decision"]


def test_send_error_marks_unknown_and_never_resends(conn):
    add_schedule(conn)
    sender = FakeGorilla(explode=True)
    r, clock, _a, _s = make_runner(conn, {2: "오늘의 퀴즈"}, [analysis()], sender=sender)
    run_one_window(conn, r, clock)
    q = auto_quizzes(conn)[0]
    assert q["entry_status"] == "unknown" and "전송 중 오류" in q["note"]
    conn.execute("UPDATE quizzes SET approved = 1")
    conn.commit()
    r.process_approved(None)
    assert len(sender.sent) == 1  # 결과 불명은 다시 보내지 않는다


def test_waits_for_gorilla_and_skips_when_never_running(conn):
    add_schedule(conn)
    sender = FakeGorilla(running=False)
    r, clock, ans, _ = make_runner(conn, {2: "오늘의 퀴즈"}, [analysis()], sender=sender)
    run_one_window(conn, r, clock)
    assert ans.calls == [] and sender.sent == []
    assert clock.now() >= MON_0700 + timedelta(minutes=10)
    assert "고릴라가 실행 중이 아님" in db.get_setting(conn, "quizbot.state")


def test_global_stop_and_rate_limit_block_sending(conn):
    add_schedule(conn)
    db.set_setting(conn, "global_stop", "1")
    r, clock, _a, sender = make_runner(conn, {2: "오늘의 퀴즈"}, [analysis()])
    run_one_window(conn, r, clock)
    assert sender.sent == [] and "일괄 중지" in auto_quizzes(conn)[0]["decision"]

    db.set_setting(conn, "global_stop", "0")
    db.set_setting(conn, "quizbot.max_sends_per_hour", "0")
    q = auto_quizzes(conn)[0]
    assert any("상한" in x for x in runner.decide(conn, q, r.schedules()[0], MON_0700))


def test_analysis_cap_and_failures(conn):
    add_schedule(conn)
    db.set_setting(conn, "quizbot.max_analyses_per_window", "1")
    script = {2: "오늘의 퀴즈", 10: "또 퀴즈", 20: "또또 퀴즈"}
    r, clock, ans, sender = make_runner(conn, script, [AnswererError("API 오류 500")])
    run_one_window(conn, r, clock)
    assert len(ans.calls) == 1 and sender.sent == []
    msgs = [e["message"] for e in conn.execute("SELECT message FROM events")]
    assert any("분석 실패" in m for m in msgs) and any("상한(1)" in m for m in msgs)


def test_similar_question_counts_as_duplicate(conn):
    add_schedule(conn)
    script = {2: "오늘의 퀴즈", 12: "퀴즈 한번 더"}
    r, clock, _a, sender = make_runner(conn, script, [
        analysis(), analysis(question="철수가 좋아하는 과일은 무엇일까요")])
    run_one_window(conn, r, clock)
    assert len(auto_quizzes(conn)) == 1 and sender.sent == ["사과"]


def test_runner_alive_heartbeat(conn):
    assert not config.runner_alive(conn)
    db.set_setting(conn, "quizbot.state", "대기 중")
    db.set_setting(conn, "quizbot.heartbeat", (datetime.now() - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S"))
    assert config.runner_alive(conn)
    db.set_setting(conn, "quizbot.heartbeat", (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S"))
    assert not config.runner_alive(conn)


# ── 관리 화면 ────────────────────────────────────────────────────────
def test_quizbot_pages_and_actions(client, conn, monkeypatch):
    launched = []
    monkeypatch.setattr(app_module, "launch_quizbot", lambda args: launched.append(args) or "q.log")
    assert client.get("/quizbot").status_code == 200
    assert client.get("/gorilla").status_code == 200
    token = csrf(client, "/quizbot")
    pid = conn.execute("SELECT id FROM programs WHERE code = '0chulpowerfm'").fetchone()[0]

    # 예약 추가: 시간을 비우면 방송 시간, 잘못된 시간은 거부
    client.post("/quizbot/schedules", data={"csrf_token": token, "program_id": pid, "days": ["0", "1"],
                                            "auto_submit": "1"})
    s = conn.execute("SELECT * FROM quiz_schedules").fetchone()
    assert (s["start_time"], s["end_time"], s["days"], s["auto_submit"]) == ("07:00", "09:00", "01", 1)
    client.post("/quizbot/schedules", data={"csrf_token": token, "program_id": pid, "days": ["0"],
                                            "start_time": "7시", "end_time": "9시"})
    assert conn.execute("SELECT COUNT(*) FROM quiz_schedules").fetchone()[0] == 1

    client.post("/quizbot/start", data={"csrf_token": token})
    assert launched == [["run"]]
    client.post("/quizbot/stop", data={"csrf_token": token})
    assert db.get_setting(conn, "quizbot.stop") == "1"

    client.post("/quizbot/tool", data={"csrf_token": token, "tool": "calibrate-input"})
    assert launched[-1] == ["calibrate", "input"]
    assert client.post("/quizbot/tool", data={"csrf_token": token, "tool": "rm -rf"}).status_code == 400

    # 확인 대기 문제 승인 / 건너뛰기
    conn.execute("""INSERT INTO quizzes (dedupe_key, account, channel, program, broadcast_date, question_key, question,
                    source, schedule_id, created_at, updated_at)
                    VALUES ('k1', 'a', '파워FM', '김영철의 파워FM', '2026-10-05', 'q', '문제', 'auto', ?, ?, ?)""",
                 (s["id"], db.now(), db.now()))
    conn.commit()
    qid = conn.execute("SELECT id FROM quizzes").fetchone()[0]
    assert "문제" in client.get("/quizbot").get_data(as_text=True)
    client.post(f"/quizbot/quizzes/{qid}/approve", data={"csrf_token": token, "answer": "사과"})
    q = conn.execute("SELECT * FROM quizzes WHERE id = ?", (qid,)).fetchone()
    assert (q["approved"], q["answer"], q["entry_status"]) == (1, "사과", "pending")
    client.post(f"/quizbot/quizzes/{qid}/skip", data={"csrf_token": token})
    assert conn.execute("SELECT entry_status FROM quizzes WHERE id = ?", (qid,)).fetchone()[0] == "skipped"
    # 이미 처리된 문제는 다시 승인할 수 없다
    client.post(f"/quizbot/quizzes/{qid}/approve", data={"csrf_token": token, "answer": "사과"})
    assert conn.execute("SELECT entry_status FROM quizzes WHERE id = ?", (qid,)).fetchone()[0] == "skipped"

    # 설정 저장 · API 키는 값이 남지 않는다
    saved = []
    monkeypatch.setattr(answerer, "save_api_key", lambda k: saved.append(k))
    client.post("/quizbot/settings", data={"csrf_token": token, "api_key": "sk-ant-test", "autostart": "1"})
    assert saved == ["sk-ant-test"] and db.get_setting(conn, "quizbot.autostart") == "1"
    assert "sk-ant-test" not in json.dumps([dict(r) for r in conn.execute("SELECT * FROM settings")])
    assert "sk-ant-test" not in json.dumps([dict(r) for r in conn.execute("SELECT * FROM events")])

    client.post("/gorilla", data={"csrf_token": token, "gorilla.message_template": "[퀴즈] {answer}"})
    assert db.get_setting(conn, "gorilla.message_template") == "[퀴즈] {answer}"

    # 일괄 중지는 퀴즈 자동 참여도 멈춘다
    db.set_setting(conn, "quizbot.stop", "0")
    client.post("/settings", data={"csrf_token": token, "global_stop": "1"})
    assert db.get_setting(conn, "quizbot.stop") == "1"
    client.post("/quizbot/start", data={"csrf_token": token})
    assert launched.count(["run"]) == 1


def test_old_database_gets_new_quiz_columns(data_dir):
    import sqlite3

    path = data_dir / "radio_helper.sqlite3"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE quizzes (id INTEGER PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, account TEXT NOT NULL, "
                "channel TEXT NOT NULL, program TEXT NOT NULL, broadcast_date TEXT NOT NULL, question_key TEXT NOT NULL, "
                "kind TEXT NOT NULL DEFAULT 'new', question TEXT, options TEXT, deadline TEXT, entry_channel TEXT, "
                "gorilla_accepted TEXT NOT NULL DEFAULT 'unknown', answer TEXT, answer_verified INTEGER NOT NULL DEFAULT 0, "
                "entry_status TEXT NOT NULL DEFAULT 'pending', answer_accepted TEXT NOT NULL DEFAULT 'unknown', "
                "won TEXT NOT NULL DEFAULT 'unknown', prize_received TEXT NOT NULL DEFAULT 'unknown', "
                "repeat_count INTEGER NOT NULL DEFAULT 1, note TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    old.commit()
    old.close()
    c = db.connect()
    db.init_db(c)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(quizzes)")}
    assert {"source", "confidence", "approved", "decision", "sent_at"} <= cols
    c.close()


def test_send_uses_shared_lock(conn):
    add_schedule(conn)
    used = []

    @contextmanager
    def lock():
        used.append(1)
        yield

    r, clock, _a, sender = make_runner(conn, {2: "오늘의 퀴즈"}, [analysis()])
    r.deps.lock = lock
    run_one_window(conn, r, clock)
    assert used == [1] and sender.sent == ["사과"]


def test_run_forever_survives_errors_and_stops(conn):
    add_schedule(conn)
    clock = Clock(MON_0700)
    sleeps = []

    def sleep(sec):
        sleeps.append(sec)
        clock.sleep(sec)
        if len(sleeps) >= 2:
            db.set_setting(conn, "quizbot.stop", "1")

    def broken_recorder(chunk):
        raise RuntimeError("루프백 장치 없음")

    deps = runner.Deps(recorder_factory=broken_recorder,
                       transcriber_factory=lambda m, p: ScriptTranscriber({}),
                       answerer=ScriptAnswerer([]), sender=FakeGorilla(), now=clock.now, sleep=sleep)
    runner.Runner(conn, deps).run_forever()
    assert db.get_setting(conn, "quizbot.state") == "멈춤" and db.get_setting(conn, "quizbot.stop") == "0"
    assert any("루프백 장치 없음" in r["message"] for r in conn.execute("SELECT message FROM events"))


def test_unexpected_answerer_error_is_logged(conn):
    add_schedule(conn)
    r, clock, ans, sender = make_runner(conn, {2: "오늘의 퀴즈"}, [KeyError("x")])
    run_one_window(conn, r, clock)
    assert sender.sent == [] and any("분석 중 오류" in e["message"] for e in conn.execute("SELECT message FROM events"))


def test_idle_outside_window(conn):
    add_schedule(conn)
    clock = Clock(datetime(2026, 10, 5, 10, 0))
    calls = []

    def sleep(sec):
        calls.append(sec)
        db.set_setting(conn, "quizbot.stop", "1")

    deps = runner.Deps(recorder_factory=lambda c: FakeRecorder(clock, c),
                       transcriber_factory=lambda m, p: ScriptTranscriber({}),
                       answerer=ScriptAnswerer([]), sender=FakeGorilla(), now=clock.now, sleep=sleep)
    rr = runner.Runner(conn, deps)
    rr.state = lambda text, _orig=rr.state: (calls.append(text), _orig(text))[1]
    rr.run_forever()
    assert any(isinstance(c, str) and "다음 예약 10/06 07:00" in c for c in calls)


# ── 고릴라 창 자동 찾기 ──────────────────────────────────────────────
class FakeWin:
    def __init__(self, title, rect=(0, 0, 400, 800)):
        self.title, self.rect = title, rect

    def window_text(self):
        return self.title

    def rectangle(self):
        left, top, right, bottom = self.rect
        return SimpleNamespace(left=left, top=top, right=right, bottom=bottom)


def fake_desktop(monkeypatch, windows):
    """windows: [(title, process, edit_names, button_names)]"""
    wins = [(FakeWin(t), t, p) for t, p, _e, _b in windows]
    controls = {t: {"Edit": e, "Button": b} for t, _p, e, b in windows}
    monkeypatch.setattr(gorilla, "_top_windows", lambda: wins)
    monkeypatch.setattr(gorilla, "_control_names", lambda w, ct, limit=300: controls[w.title][ct])


BROWSER = ("고릴라·인식 설정 · 라디오 참여 도우미 - Aside", "aside.exe", ["주소창"], ["뒤로", "새로고침"])
BROWSER2 = ("SBS 고릴라 - Chrome", "chrome.exe", [], [])
PLAYER = ("고릴라", "gorealra.exe", [], ["재생", "최소화"])
CHAT = ("공감로그", "gorealra.exe", ["공감로그 글쓰기(200자 내외)"], ["전송", "닫기"])


def test_score_excludes_browsers_and_own_pages():
    assert gorilla.score_window(*BROWSER) < 0
    assert gorilla.score_window(*BROWSER2) < 0
    assert gorilla.score_window(*CHAT) > gorilla.score_window(*PLAYER) > 0


def test_auto_setup_picks_chat_window(monkeypatch):
    fake_desktop(monkeypatch, [BROWSER, BROWSER2, PLAYER, CHAT])
    report = gorilla.Gorilla(config.GorillaConfig()).auto_setup()
    assert report["chosen"]["title"] == "공감로그"
    st = report["settings"]
    assert st["gorilla.process_name"] == "gorealra.exe" and st["gorilla.input_mode"] == "uia"
    assert st["gorilla.send_mode"] == "auto" and st["gorilla.input_x"] == ""


def test_auto_setup_falls_back_to_coords_and_reports_failure(monkeypatch):
    fake_desktop(monkeypatch, [BROWSER, ("고릴라", "gorealra.exe", [], [])])
    report = gorilla.Gorilla(config.GorillaConfig()).auto_setup()
    assert report["settings"]["gorilla.input_mode"] == "coords"  # 프로그램·제목은 맞지만 입력칸이 안 보임

    fake_desktop(monkeypatch, [BROWSER, BROWSER2, ("메모장", "notepad.exe", ["본문"], [])])
    report = gorilla.Gorilla(config.GorillaConfig()).auto_setup()
    assert report["settings"] == {} and "찾지 못했습니다" in report["message"]


def test_find_window_ignores_browser_and_prefers_chat(monkeypatch):
    fake_desktop(monkeypatch, [BROWSER, PLAYER, CHAT])
    g = gorilla.Gorilla(config.GorillaConfig())  # 기본 제목 패턴 '고릴라|gorealra'
    # 제목은 플레이어 창만 맞지만, 같은 프로그램의 공감로그 창을 함께 보고 입력칸이 있는 쪽을 고른다
    assert g.find_window().title == "공감로그"
    g = gorilla.Gorilla(config.GorillaConfig(process_name="gorealra.exe"))
    assert g.find_window().title == "공감로그" and g.is_running()
    fake_desktop(monkeypatch, [BROWSER])
    assert not g.is_running()


def test_auto_setup_title_only_window_goes_to_coords(monkeypatch):
    fake_desktop(monkeypatch, [BROWSER, ("고릴라", "electron.exe", [], [])])
    report = gorilla.Gorilla(config.GorillaConfig()).auto_setup()
    st = report["settings"]
    assert st["gorilla.input_mode"] == "coords" and st["gorilla.send_mode"] == "coords"
    assert st["gorilla.process_name"] == "electron.exe" and "위치 지정" in report["message"]


def test_calibration_uses_window_under_mouse():
    info = {"title": "공감로그", "process": "gorealra.exe", "rect": (100, 100, 500, 900), "point": (300, 820)}
    st = gorilla.calibration_settings("input", info)
    assert (st["gorilla.input_x"], st["gorilla.input_y"]) == ("0.5", "0.9")
    assert st["gorilla.input_mode"] == "coords" and st["gorilla.process_name"] == "gorealra.exe"
    assert st["gorilla.window_size"] == "400,800" and st["gorilla.window_title"] == "공감로그"
    st = gorilla.calibration_settings("send", info)
    assert st["gorilla.send_mode"] == "coords" and "gorilla.input_mode" not in st
    with pytest.raises(gorilla.GorillaError):
        gorilla.calibration_settings("input", {"title": "고릴라·인식 설정 · 라디오 참여 도우미 - Aside",
                                               "process": "aside.exe", "rect": (0, 0, 10, 10), "point": (5, 5)})


def test_find_window_prefers_calibrated_size(monkeypatch):
    player = (FakeWin("", (0, 0, 700, 950)), "", "gorealra.exe")
    chat = (FakeWin("", (700, 0, 1340, 950)), "", "gorealra.exe")
    monkeypatch.setattr(gorilla, "_top_windows", lambda: [player, chat])
    monkeypatch.setattr(gorilla, "_control_names", lambda w, ct, limit=300: [])
    g = gorilla.Gorilla(config.GorillaConfig(process_name="gorealra.exe", window_title="", window_size="640,950"))
    assert g.find_window() is chat[0]


def test_region_settings_relative_to_window():
    chat = {"title": "공감로그", "process": "gorealra.exe", "rect": (700, 0, 1340, 950), "point": (0, 0)}
    st, msg = gorilla.region_settings("input", (800, 880, 1200, 940), chat, None)
    assert st["gorilla.input_rect"] == "0.1562,0.9263,0.7812,0.9895"
    assert (float(st["gorilla.input_x"]), float(st["gorilla.input_y"])) == (0.4688, 0.9579)
    assert st["gorilla.input_mode"] == "coords" and st["gorilla.process_name"] == "gorealra.exe"
    assert st["gorilla.window_size"] == "640,950" and "입력칸" in msg
    st, _ = gorilla.region_settings("send", (1230, 870, 1330, 945), chat, None)
    assert st["gorilla.send_mode"] == "coords" and "gorilla.input_mode" not in st


def test_region_settings_window_region_and_errors():
    browser = {"title": "고릴라·인식 설정 · 라디오 참여 도우미 - Aside", "process": "aside.exe", "rect": (0, 0, 1, 1)}
    with pytest.raises(gorilla.GorillaError):
        gorilla.region_settings("input", (10, 10, 100, 40), browser, None)  # 브라우저 위에 그림
    with pytest.raises(gorilla.GorillaError):
        gorilla.region_settings("input", (10, 10, 12, 12), browser, None)  # 너무 작음

    # 창 인식이 안 되는 경우: 화면 영역을 기준으로
    app = {"title": "", "process": "", "rect": (0, 0, 1920, 1080)}
    st, _ = gorilla.region_settings("window", (700, 0, 1340, 950), app, None)
    assert st["gorilla.screen_region"] == "700,0,1340,950" and st["gorilla.input_x"] == ""
    region = tuple(int(v) for v in gorilla.parse_rect(st["gorilla.screen_region"]))
    st2, msg = gorilla.region_settings("input", (800, 880, 1200, 940), browser, region)  # 영역 기준이면 창 정보와 무관
    assert st2["gorilla.input_rect"] == "0.1562,0.9263,0.7812,0.9895" and "영역" in msg
    with pytest.raises(gorilla.GorillaError):
        gorilla.region_settings("send", (1500, 100, 1600, 150), app, region)  # 영역 밖


def test_parse_rect():
    assert gorilla.parse_rect("1,2,3,4") == (1.0, 2.0, 3.0, 4.0)
    assert gorilla.parse_rect("") is None and gorilla.parse_rect("1,2") is None and gorilla.parse_rect("a,b,c,d") is None
