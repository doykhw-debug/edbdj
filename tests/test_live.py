"""'청취 시작' 한 번으로 듣기 · 자막 · 관리 화면."""

import json
from datetime import datetime, timedelta

from conftest import csrf, make_experience
from test_quizbot import MON_0700, Clock, FakeGorilla, FakeRecorder, ScriptAnswerer, ScriptTranscriber, analysis
from test_story_gift_import import FullAnswerer, story_for

import radio_helper.app as app_module
from radio_helper import db
from radio_helper.quizbot import captions, live, runner

MON_1400 = MON_0700 + timedelta(hours=7)


# ── 편성·상태 ────────────────────────────────────────────────────────
def test_days_match():
    mon, sat = datetime(2026, 10, 5), datetime(2026, 10, 10)
    assert live.days_match("매일", sat) and live.days_match("", sat) and live.days_match(None, mon)
    assert live.days_match("평일", mon) and not live.days_match("평일", sat)
    assert live.days_match("주말", sat) and not live.days_match("주말", mon)
    assert live.days_match("월~금", mon) and not live.days_match("월~금", sat)
    assert live.days_match("토,일", sat) and not live.days_match("토,일", mon)


def test_current_program_and_label(conn):
    assert live.current_program(conn, "파워FM", MON_0700 + timedelta(minutes=30)) == "김영철의 파워FM"
    # 자정을 넘기는 프로그램 (배성재의 텐 등)도 찾는다
    late = conn.execute("SELECT title, start_time, end_time FROM programs WHERE channel = '파워FM' "
                        "AND end_time < start_time").fetchone()
    if late:
        h, m = map(int, late["end_time"].split(":"))
        after_midnight = datetime(2026, 10, 6, 0, 0) + timedelta(minutes=max(h * 60 + m - 1, 0))
        assert live.current_program(conn, "파워FM", after_midnight) == late["title"]
    assert live.current_program(conn, "러브FM", MON_0700 + timedelta(hours=2, minutes=30)) == "이숙영의 러브FM"
    assert live.current_program(conn, "러브FM", datetime(2026, 10, 10, 9, 30)) is None  # 토요일
    assert live.program_label(conn, "러브FM", MON_1400) == "러브FM 방송"


def test_level_percent_and_keywords():
    assert live.level_percent(0) == 0 and live.level_percent(0.0005) == 0
    assert live.level_percent(1.0) == 100 and 40 < live.level_percent(0.03) < 60
    assert live.keywords("오늘의 퀴즈 정답은 고릴라로 보내주세요") == ["퀴즈", "정답"]
    assert live.keywords("신청곡과 사연은 게시판에, 힌트 하나 드릴게요") == ["힌트", "사연", "신청곡", "게시판"]
    assert live.keywords("선물로 오답도 받아요") == ["오답", "선물"]
    assert live.keywords("다음 곡 듣고 오겠습니다") == []


def test_view_model_status(conn):
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    assert live.view_model(conn, now)["status"] == "off"
    db.set_setting(conn, "live.active", "1")
    assert live.view_model(conn, now)["status"] == "stalled"      # 켜 두었는데 실행기를 띄운 적 없음
    db.set_setting(conn, "quizbot.launched_at", stamp)
    assert live.view_model(conn, now)["status"] == "launching"
    # 띄운 지 한참 지났는데 신호가 없으면 응답 없음
    assert live.view_model(conn, now + timedelta(seconds=120))["status_text"] == "응답 없음"
    db.set_setting(conn, "quizbot.state", "음성 인식 준비 중")
    db.set_setting(conn, "quizbot.heartbeat", stamp)
    assert live.view_model(conn, now)["status"] == "starting"
    db.set_setting(conn, "quizbot.level", "63")
    db.set_setting(conn, "quizbot.level_at", stamp)
    conn.execute("INSERT INTO transcripts (broadcast_date, at, text) VALUES ('2026-10-02', ?, ?)",
                 (stamp, "오늘의 퀴즈 나갑니다"))
    conn.commit()
    vm = live.view_model(conn, now)
    assert (vm["status"], vm["status_text"], vm["level"]) == ("collecting", "수집 중", 63)
    assert vm["lines"][-1]["keywords"] == ["퀴즈"] and vm["program"]
    # 소리 신호가 끊긴 지 오래면 수집 중이 아니다
    assert live.view_model(conn, now + timedelta(seconds=30))["status"] != "collecting"


def test_chunk_text_and_stt_test_stuck(conn):
    assert live.chunk_text(None) == ""
    assert live.chunk_text({"at": "00:22:10", "level": 41, "seconds": 2.3, "text": ""}) == \
        "마지막 녹음 00:22:10 · 소리 41/100 · 받아쓰기 2.3초 → 말소리 없음"
    assert live.chunk_text({"at": "00:22:20", "level": 41, "seconds": 2.0, "text": "안녕하세요"}).endswith("'안녕하세요'")
    old = (datetime.now() - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    db.set_setting(conn, "quizbot.stt_test", json.dumps({"at": old, "running": True, "phase": "받아쓰는 중", "steps": []}))
    assert live.view_model(conn)["stt_test"]["stuck"] is True


def test_caption_lines():
    vm = {"lines": [{"at": "07:00:01", "text": "음악", "keywords": []},
                    {"at": "07:00:11", "text": "오늘의 퀴즈", "keywords": ["퀴즈"]}]}
    lines = captions.caption_lines(vm)
    assert lines[0] == ("07:00  음악", "#e5e7eb")
    assert lines[1][0] == "07:00  [퀴즈] 오늘의 퀴즈" and lines[1][1] == captions.KEYWORD_COLOR["퀴즈"]


# ── 실행기: 청취 ─────────────────────────────────────────────────────
class StopAfter(FakeRecorder):
    """n 번째 녹음 뒤 '청취 중지'를 누른 것처럼 한다. on_chunk(i) 로 중간에 상황을 바꾼다."""
    def __init__(self, conn, clock, chunk, n, on_chunk=None):
        super().__init__(clock, chunk)
        self.conn, self.n, self.i, self.on_chunk = conn, n, 0, on_chunk
        self.on_level = None

    def read_chunk(self):
        self.i += 1
        if self.on_chunk:
            self.on_chunk(self.i)
        if self.i >= self.n:
            db.set_setting(self.conn, "live.active", "0")
        return super().read_chunk()


def live_runner(conn, script, ans, sender, n, start=MON_1400, on_chunk=None):
    clock = Clock(start)
    recorders = []

    def factory(chunk):
        recorders.append(StopAfter(conn, clock, chunk, n, on_chunk))
        return recorders[-1]

    deps = runner.Deps(recorder_factory=factory, transcriber_factory=lambda m, p: ScriptTranscriber(script),
                       answerer=ans, sender=sender, now=clock.now, sleep=clock.sleep)
    return runner.Runner(conn, deps), recorders


def start_live(conn, channel="러브FM", **flags):
    db.set_setting(conn, "live.active", "1")
    db.set_setting(conn, "live.channel", channel)
    for k, v in flags.items():
        db.set_setting(conn, f"live.{k}", v)


def test_live_listens_any_channel_and_sends_quiz(conn):
    start_live(conn)
    sender = FakeGorilla()
    r, recorders = live_runner(conn, {3: "자 오늘의 퀴즈 나갑니다 철수가 좋아하는 과일은 정답은 고릴라로 보내주세요"},
                               ScriptAnswerer([analysis()]), sender, n=12)
    r.run_live()
    assert sender.sent == ["사과"]
    q = conn.execute("SELECT * FROM quizzes WHERE source = 'auto'").fetchone()
    assert (q["channel"], q["program"], q["schedule_id"], q["decision"]) == ("러브FM", "러브FM 방송", None, "자동 전송")
    rows = conn.execute("SELECT DISTINCT channel, program FROM transcripts").fetchall()
    assert [tuple(r) for r in rows] == [("러브FM", "러브FM 방송")]
    assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 12
    assert recorders[0].on_level == r.on_second  # 녹음 중 소리 크기를 화면에 알리고 끊김을 살핀다
    assert db.get_setting(conn, "quizbot.level_at")
    chunk = json.loads(db.get_setting(conn, "quizbot.last_chunk"))   # '마지막 녹음' 줄 (말소리가 없어도 갱신)
    assert chunk["text"] == "음악이 흐릅니다" and chunk["level"] > 0
    r.report_level(50)
    assert db.get_setting(conn, "quizbot.heartbeat") == db.get_setting(conn, "quizbot.level_at")
    messages = [e["message"] for e in conn.execute("SELECT message FROM events")]
    assert "청취 시작 — 러브FM" in messages and "청취 멈춤" in messages
    assert any(m.startswith("첫 받아쓰기") for m in messages)


def test_reannouncement_across_the_hour_is_not_sent_again(conn):
    start_live(conn)
    sender = FakeGorilla()
    script = {3: "자 오늘의 퀴즈 철수가 좋아하는 과일은 정답은 고릴라로 보내주세요",
              80: "퀴즈 다시 한번 알려드립니다 철수가 좋아하는 과일은"}
    r, _ = live_runner(conn, script, ScriptAnswerer([analysis(), analysis()]), sender, n=100,
                       start=MON_1400 + timedelta(minutes=55))  # 14:55 → 15:11
    r.run_live()
    qs = conn.execute("SELECT * FROM quizzes WHERE source = 'auto'").fetchall()
    assert sender.sent == ["사과"] and len(qs) == 1 and qs[0]["repeat_count"] == 2


def test_live_quiz_waits_for_confirmation_when_auto_off(conn):
    start_live(conn, channel="파워FM", auto_quiz="0")
    sender = FakeGorilla()
    r, _ = live_runner(conn, {3: "오늘의 퀴즈 정답은 고릴라로"}, ScriptAnswerer([analysis()]), sender, n=10,
                       start=MON_0700)
    r.run_live()
    q = conn.execute("SELECT * FROM quizzes WHERE source = 'auto'").fetchone()
    assert sender.sent == [] and q["entry_status"] == "pending" and q["program"] == "김영철의 파워FM"
    assert "자동 전송이 꺼져" in q["decision"]


def test_live_keeps_listening_without_gorilla_and_sends_when_found(conn):
    start_live(conn)
    sender = FakeGorilla(running=False)

    def gorilla_appears(i):
        if i == 9:
            sender.running = True

    r, _ = live_runner(conn, {3: "오늘의 퀴즈 철수가 좋아하는 과일 정답은 고릴라로"}, ScriptAnswerer([analysis()]),
                       sender, n=20, on_chunk=gorilla_appears)
    r.run_live()
    # 고릴라 창이 없어도 자막은 계속 만든다
    assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 20
    q = conn.execute("SELECT * FROM quizzes WHERE source = 'auto'").fetchone()
    assert sender.sent == ["사과"] and q["entry_status"] == "entered"
    messages = " ".join(e["message"] for e in conn.execute("SELECT message FROM events"))
    assert "고릴라 창을 찾지 못했습니다" in messages and "고릴라 창을 찾았습니다" in messages


def test_quiz_retried_every_minute_when_gorilla_really_missing(conn):
    """고릴라 창이 정말 없으면: 보류하지 않고 보내 보고, 못 보냈으니(누르기 전에 막힘) 1분마다 다시 시도한다."""
    start_live(conn)
    sender = FakeGorilla(running=False)
    r, _ = live_runner(conn, {3: "오늘의 퀴즈 정답은 고릴라로"}, ScriptAnswerer([analysis()]), sender, n=20)
    r.run_live()
    q = conn.execute("SELECT * FROM quizzes WHERE source = 'auto'").fetchone()
    assert sender.sent == [] and q["entry_status"] == "pending" and q["decision"].startswith(runner.RETRY_PREFIX)
    assert len(sender.tried) == 3 and q["sent_at"] is None      # 첫 시도 뒤 2분(10초씩 12번) 동안 1분 간격으로만
    msgs = [m for (m,) in conn.execute("SELECT message FROM events") if "못 보냄" in m]
    assert len(msgs) == 1                                        # 실패 알림은 한 번만


def test_quiz_sent_even_when_window_check_says_missing(conn):
    """실제 사례: 창 찾기 판단은 '못 찾음'이었지만 표시한 자리로는 보낼 수 있었다 → 보류하지 않고 바로 보냄."""
    start_live(conn)
    sender = FakeGorilla(running=False, reachable=True)
    r, _ = live_runner(conn, {3: "오늘의 퀴즈 정답은 고릴라로"}, ScriptAnswerer([analysis()]), sender, n=10)
    r.run_live()
    q = conn.execute("SELECT * FROM quizzes WHERE source = 'auto'").fetchone()
    assert sender.sent == ["사과"] and q["entry_status"] == "entered"


def test_retry_gives_up_after_twenty_minutes(conn):
    from datetime import timedelta

    start_live(conn)
    sender = FakeGorilla(running=False)
    r, recorders = live_runner(conn, {3: "오늘의 퀴즈 정답은 고릴라로"}, ScriptAnswerer([analysis()]), sender, n=10)
    r.run_live()
    qid = conn.execute("SELECT id FROM quizzes WHERE source = 'auto'").fetchone()[0]
    r.deps.now = lambda: recorders[0].clock.now() + timedelta(minutes=25)   # 처음 못 보낸 뒤 25분
    db.set_setting(conn, "live.active", "1")
    r.try_send(qid, live.session(conn, r.deps.now()))
    q = conn.execute("SELECT * FROM quizzes WHERE id = ?", (qid,)).fetchone()
    assert q["entry_status"] == "pending" and "20분 동안 다시 시도했지만" in q["decision"] and not q["approved"]


def test_live_story_auto_ai_only_when_clean(conn):
    eid = make_experience(conn, story="첫 출근 날 실수로 사장님 커피를 마셨다", highlight="", ending="", quotes="",
                          quote_kind="none")
    start_live(conn, auto_story="1")
    sender = FakeGorilla()
    r, _ = live_runner(conn, {3: "오늘의 주제는 첫 출근 날입니다 사연 보내주세요"},
                       FullAnswerer(stories=[story_for(eid, gorilla_accepted="unknown")]), sender, n=8)
    r.run_live()
    post = conn.execute("SELECT * FROM story_posts").fetchone()
    assert sender.sent == ["첫 출근 날 실수로 사장님 커피를 마셨어요"], post["decision"]
    assert post["status"] == "entered" and post["program"] == "러브FM 방송"


def test_live_story_with_warnings_waits(conn):
    eid = make_experience(conn, story="첫 출근 날 실수로 사장님 커피를 마셨다", highlight="", ending="", quotes="",
                          quote_kind="none")
    start_live(conn, auto_story="1")
    sender = FakeGorilla()
    r, _ = live_runner(conn, {3: "오늘의 주제는 첫 출근 날입니다 사연 보내주세요"},
                       FullAnswerer(stories=[story_for(eid, added_facts=["사장님이 웃으셨다"])]), sender, n=8)
    r.run_live()
    post = conn.execute("SELECT * FROM story_posts").fetchone()
    assert sender.sent == [] and post["status"] == "pending" and "확인 후 전송" in post["decision"]


def test_run_forever_switches_to_live(conn):
    start_live(conn)
    sender = FakeGorilla()
    clock = Clock(MON_1400)
    calls = []

    def factory(chunk):
        return StopAfter(conn, clock, chunk, 3)

    def sleep(s):
        calls.append(s)
        clock.sleep(s)
        db.set_setting(conn, "quizbot.stop", "1")  # 청취가 끝나고 대기로 돌아오면 끝낸다

    deps = runner.Deps(recorder_factory=factory, transcriber_factory=lambda m, p: ScriptTranscriber({}),
                       answerer=ScriptAnswerer([]), sender=sender, now=clock.now, sleep=sleep)
    runner.Runner(conn, deps).run_forever()
    assert conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0] == 3
    assert calls == [5] and db.get_setting(conn, "quizbot.state") == "멈춤"


# ── 관리 화면 ────────────────────────────────────────────────────────
def test_home_and_listen_start_stop(client, conn, monkeypatch):
    launched = []
    monkeypatch.setattr(app_module, "launch_quizbot", lambda args: launched.append(args) or "q.log")
    page = client.get("/").get_data(as_text=True)
    assert "청취 시작" in page and "러브FM" in page and "준비 상태" in page
    token = csrf(client, "/")

    client.post("/listen/start", data={"csrf_token": token, "channel": "러브FM", "auto_quiz": "1", "gift": "1",
                                       "captions": "1"})
    assert db.get_setting(conn, "live.active") == "1" and db.get_setting(conn, "live.channel") == "러브FM"
    assert (db.get_setting(conn, "live.auto_quiz"), db.get_setting(conn, "live.auto_story")) == ("1", "0")
    assert launched == [["run"], ["captions"]]
    page = client.get("/").get_data(as_text=True)
    assert "청취 중지" in page and "nav-dot" in page

    # 두 번 눌러도 실행기를 또 띄우지 않는다
    client.post("/listen/start", data={"csrf_token": token, "channel": "러브FM", "captions": "1"})
    assert launched.count(["run"]) == 1

    vm = client.get("/live.json").get_json()
    assert vm["active"] is True and vm["channel"] == "러브FM" and vm["status"] in ("launching", "starting")

    client.post("/listen/stop", data={"csrf_token": token})
    assert db.get_setting(conn, "live.active") == "0"
    assert db.get_setting(conn, "quizbot.stop") == "1"  # 예약이 없으면 실행기도 끝낸다

    assert client.post("/listen/start", data={"csrf_token": token, "channel": "없는 채널"}).status_code == 400
    db.set_setting(conn, "global_stop", "1")
    client.post("/listen/start", data={"csrf_token": token, "channel": "파워FM"})
    assert db.get_setting(conn, "live.active") == "0"


def test_pages_render(client, conn):
    for path in ("/", "/board", "/stories", "/quizzes", "/quizbot", "/gorilla", "/programs", "/profile",
                 "/gifts", "/settings"):
        assert client.get(path).status_code == 200, path
    assert "러브FM" in client.get("/programs").get_data(as_text=True)


def test_inspect_image_route(client, data_dir):
    (data_dir / "inspect").mkdir(exist_ok=True)
    (data_dir / "inspect" / "gorilla_test.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (data_dir / "secret.png").write_bytes(b"x")
    assert client.get("/inspect-image/gorilla_test.png").data.startswith(b"\x89PNG")
    assert client.get("/inspect-image/..%2Fsecret.png").status_code == 404
    assert client.get("/inspect-image/other.png").status_code == 404
    assert "gorilla_test.png" in client.get("/gorilla").get_data(as_text=True)


def test_quiz_result_only_touches_result_fields(client, conn):
    conn.execute("""INSERT INTO quizzes (dedupe_key, account, channel, program, broadcast_date, question_key, question,
                    answer, entry_status, source, created_at, updated_at)
                    VALUES ('k', 'a', '러브FM', 'p', '2026-10-05', 'q', '문제', '사과', 'entered', 'auto', ?, ?)""",
                 (db.now(), db.now()))
    conn.commit()
    token = csrf(client, "/quizzes")
    assert "문제" in client.get("/quizzes").get_data(as_text=True)
    client.post("/quizzes/1/result", data={"csrf_token": token, "answer_accepted": "yes", "won": "no",
                                           "prize_received": "unknown"})
    q = conn.execute("SELECT * FROM quizzes WHERE id = 1").fetchone()
    assert (q["answer_accepted"], q["won"], q["question"], q["answer"]) == ("yes", "no", "문제", "사과")
    assert client.post("/quizzes/1/result", data={"csrf_token": token, "won": "maybe"}).status_code == 400


# ── 받아쓰기 테스트 · 전송 테스트 · 응답 없음 ────────────────────────
class FakeLoopback:
    def __init__(self, seconds, fail=False):
        self.device_name = "스피커 (테스트)"
        self.fail = fail

    def __enter__(self):
        if self.fail:
            raise RuntimeError("기본 스피커의 루프백 장치를 찾지 못했습니다.")
        return self

    def __exit__(self, *a):
        return False

    def read_chunk(self):
        import numpy as np
        return np.full(160_000, 0.05, dtype=np.float32)


class FakeWhisper:
    def __init__(self, model, program, device, **kw):
        from radio_helper.quizbot.stt import resolve_model

        self.label, self.load_seconds = f"{device.upper()} · {resolve_model(model, device, 8)}", 0.1

    def transcribe(self, audio):
        return "네 오늘의 퀴즈 나갑니다"


def test_stt_test_command_records_each_step(conn, monkeypatch):
    from radio_helper.quizbot import __main__ as cli
    from radio_helper.quizbot import audio, stt

    monkeypatch.setattr(audio, "LoopbackRecorder", lambda s: FakeLoopback(s))
    monkeypatch.setattr(stt, "WhisperTranscriber", FakeWhisper)
    assert cli.cmd_stt_test(conn) == 0
    t = json.loads(db.get_setting(conn, "quizbot.stt_test"))
    assert t["ok"] and not t["running"] and t["text"] == "네 오늘의 퀴즈 나갑니다"
    assert [s["name"] for s in t["steps"]] == ["녹음 장치", "10초 녹음", "음성 인식 모델", "받아쓰기"]
    # 장치 auto: 그래픽카드 점검(테스트에서는 실패로 고정)에 떨어지면 CPU, 코어가 적으면 small
    assert "CPU · small" in t["steps"][2]["detail"] and "그래픽카드 점검 실패" in t["steps"][2]["detail"]

    monkeypatch.setattr(audio, "LoopbackRecorder", lambda s: FakeLoopback(s, fail=True))
    assert cli.cmd_stt_test(conn) == 1
    t = json.loads(db.get_setting(conn, "quizbot.stt_test"))
    assert not t["ok"] and t["steps"][-1]["name"] == "녹음 장치 여는 중" and "루프백" in t["steps"][-1]["detail"]


def test_send_test_command_uses_test_message(conn, monkeypatch):
    from radio_helper.quizbot import __main__ as cli
    from radio_helper.quizbot import gorilla

    sent = []

    class FakeG:
        def __init__(self, cfg):
            pass

        def send_test(self, text):
            sent.append(text)
            return gorilla.SendResult("entered", "입력칸 비워짐"), "보냄"

    monkeypatch.setattr(gorilla, "Gorilla", FakeG)
    assert cli.cmd_send_test(conn) == 0 and sent == ["파워 FM 화이팅"]
    db.set_setting(conn, "gorilla.test_message", "러브FM 화이팅")
    cli.cmd_send_test(conn)
    assert sent[-1] == "러브FM 화이팅"
    assert any("전송 테스트" in e["message"] for e in conn.execute("SELECT message FROM events"))


def test_stt_and_send_test_routes(client, conn, monkeypatch):
    launched = []
    monkeypatch.setattr(app_module, "launch_quizbot", lambda args: launched.append(args) or "q.log")
    token = csrf(client, "/")
    client.post("/listen/stt-test", data={"csrf_token": token})
    assert launched == [["stt-test"]] and json.loads(db.get_setting(conn, "quizbot.stt_test"))["running"]
    client.post("/listen/stt-test", data={"csrf_token": token})   # 진행 중이면 또 띄우지 않음
    assert launched == [["stt-test"]]
    assert "받아쓰기 테스트" in client.get("/").get_data(as_text=True)

    client.post("/quizbot/tool", data={"csrf_token": token, "tool": "send-test"})
    assert launched[-1] == ["send-test", "--app", "gorilla"]
    assert client.post("/quizbot/tool", data={"csrf_token": token, "tool": "type-test"}).status_code == 400
    assert "파워 FM 화이팅" in client.get("/gorilla").get_data(as_text=True)


def test_stalled_listener_shows_log_and_restarts(client, conn, data_dir, monkeypatch):
    launched = []
    monkeypatch.setattr(app_module, "launch_quizbot", lambda args: launched.append(args) or "quizbot_new.log")
    (data_dir / "logs").mkdir(exist_ok=True)
    (data_dir / "logs" / "quizbot_20261003_002000_ab12.log").write_text(
        "퀴즈 자동 참여를 시작합니다.\nCould not load library cudnn_ops_infer64_8.dll\n", encoding="utf-8")
    old = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    for k, v in {"live.active": "1", "quizbot.state": "딘딘의 뮤직하이 듣는 중", "quizbot.heartbeat": old,
                 "quizbot.launched_at": old, "quizbot.run_log": "quizbot_20261003_002000_ab12.log"}.items():
        db.set_setting(conn, k, v)
    page = client.get("/").get_data(as_text=True)
    assert "응답 없음" in page and "cudnn_ops_infer64_8.dll" in page and "다시 시작" in page
    assert client.get("/live.json").get_json()["status"] == "stalled"
    db.set_setting(conn, "quizbot.run_log", "../../secret.log")   # 기록 파일 이름은 정해진 형식만
    assert client.get("/live.json").get_json()["log_tail"] == ""

    token = csrf(client, "/")
    client.post("/listen/start", data={"csrf_token": token, "channel": "파워FM"})
    assert launched[0] == ["run"] and db.get_setting(conn, "quizbot.run_log") == "quizbot_new.log"


# ── PC 볼륨이 작을 때: 받아쓰기 전에 키우기 · 소리 작음 안내 ─────────────────
def test_boost_quiet_audio():
    import numpy as np

    from radio_helper.quizbot import audio

    t = np.linspace(0, 1, 16_000, endpoint=False)
    quiet = (0.01 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    assert abs(float(np.abs(audio.boost_quiet(quiet)).max()) - 0.5) < 0.01          # 작은 소리 → 키움
    loud = (0.8 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    assert audio.boost_quiet(loud) is loud                                             # 큰 소리는 그대로
    hiss = (0.0005 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    assert audio.boost_quiet(hiss) is hiss                                             # 잡음 수준은 키우지 않음
    tiny = (0.002 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    assert abs(float(np.abs(audio.boost_quiet(tiny)).max()) - 0.2) < 0.01             # 최대 100배까지만


def test_transcriber_boosts_before_recognition():
    import numpy as np

    from radio_helper.quizbot import stt

    heard = []

    class FakeModel:
        def transcribe(self, audio, **kw):
            heard.append(float(np.abs(audio).max()))
            return [], None

    tr = object.__new__(stt.WhisperTranscriber)
    tr.model, tr.beam_size, tr._has_hotwords = FakeModel(), 1, False
    tr.set_context("", "")
    tr.transcribe(np.full(32_000, 0.01, dtype=np.float32))
    assert heard and heard[0] > 0.4


def test_stt_test_warns_when_quiet(conn, monkeypatch):
    import numpy as np

    from radio_helper.quizbot import __main__ as cli
    from radio_helper.quizbot import audio, stt

    class QuietLoopback(FakeLoopback):
        def read_chunk(self):
            return np.full(160_000, 0.003, dtype=np.float32)

    monkeypatch.setattr(audio, "LoopbackRecorder", lambda s: QuietLoopback(s))
    monkeypatch.setattr(stt, "WhisperTranscriber", FakeWhisper)
    assert cli.cmd_stt_test(conn) == 0
    rec = json.loads(db.get_setting(conn, "quizbot.stt_test"))["steps"][1]
    assert rec["ok"] and "소리가 작습니다" in rec["detail"] and "음소거" in rec["detail"]
    assert cli.level_advice(60, audio.QUIET_LEVEL) == "" and "들리지 않습니다" in cli.level_advice(0, 25)


# ── 받아쓰기 테스트 '진행 중'이 풀리지 않던 문제 ─────────────────────────
def test_stt_test_stuck_when_process_gone(conn):
    from radio_helper.quizbot import config as qconfig

    now = datetime.now()
    started = (now - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    db.set_setting(conn, "quizbot.stt_test", json.dumps({"at": started, "running": True, "phase": "음성 인식 준비 중",
                                                        "steps": []}))
    assert live.view_model(conn, now)["stt_test"]["stuck"] is True                 # 실행기가 없다 → 멈춤
    lock = qconfig.instance_lock(live.STT_TEST_LOCK)                               # 실행기가 돌고 있으면
    try:
        assert live.view_model(conn, now)["stt_test"]["stuck"] is False
    finally:
        lock.close()
    just = now.strftime("%Y-%m-%d %H:%M:%S")
    db.set_setting(conn, "quizbot.stt_test", json.dumps({"at": just, "running": True, "steps": []}))
    assert live.view_model(conn, now)["stt_test"]["stuck"] is False                # 막 눌렀음 (실행기 뜨는 중)


def test_stt_test_refuses_second_run(conn, capsys):
    from radio_helper.quizbot import __main__ as cli
    from radio_helper.quizbot import config as qconfig

    lock = qconfig.instance_lock(live.STT_TEST_LOCK)
    try:
        assert cli.cmd_stt_test(conn) == 0 and "이미 진행 중" in capsys.readouterr().out
    finally:
        lock.close()


def test_setup_item_ok_when_listening_already_transcribes(client, conn):
    def item():
        page = client.get("/").get_data(as_text=True)
        return page.rsplit("받아쓰기 테스트</a>", 1)[1][:200]     # 준비 상태 목록의 항목 (요약 줄 말고)

    old = (datetime.now() - timedelta(minutes=40)).strftime("%Y-%m-%d %H:%M:%S")
    db.set_setting(conn, "quizbot.stt_test", json.dumps({"at": old, "running": True, "steps": []}))
    assert "도중에 멈춤" in item()
    db.set_setting(conn, "quizbot.stt_test", json.dumps({"at": db.now(), "running": True, "phase": "음성 인식 준비 중 (모델 내려받기)",
                                                        "steps": []}))
    from radio_helper.quizbot import config as qconfig
    lock = qconfig.instance_lock(live.STT_TEST_LOCK)
    try:
        assert "진행 중 — 음성 인식 준비 중 (모델 내려받기)" in item()
    finally:
        lock.close()
    conn.execute("INSERT INTO transcripts (broadcast_date, at, text) VALUES ('2026-10-09', ?, '오늘의 퀴즈 나갑니다')",
                 (db.now(),))
    conn.commit()
    assert "청취 중 받아쓰기 확인됨" in item()
    assert "badge ok\">OK</span>" in client.get("/").get_data(as_text=True).rsplit("받아쓰기 테스트</a>", 1)[0][-160:]


# ── 최근 퀴즈와 보낸 답을 눈에 띄게 ─────────────────────────────────
def add_quiz(conn, key, question, answer, status, at, **extra):
    cols = dict(dedupe_key=key, account="a", channel="파워FM", program="황제성의 황제파워", broadcast_date=at[:10],
                question_key=key, question=question, answer=answer, entry_status=status, created_at=at, updated_at=at,
                source="auto", **extra)
    conn.execute(f"INSERT INTO quizzes ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(cols.values()))
    conn.commit()


def test_last_quiz_text(client, conn):
    now = datetime.now()
    stamp = lambda m: (now - timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
    assert live.last_quiz_text(conn, now) == ""
    add_quiz(conn, "q1", "옛날 문제", "답1", "posted", stamp(45))                                  # 30분 지남
    assert live.last_quiz_text(conn, now) == ""
    add_quiz(conn, "q2", "황제성이 개그콘서트에서 꼭 한번 출연해 보고 싶었던 코너는 무엇인가?", "봉숭아학당", "posted",
             stamp(2), sent_at=stamp(1), send_text="봉숭아학당")
    text = live.last_quiz_text(conn, now)
    assert text.startswith(f"퀴즈 {stamp(1)[11:16]} · 황제성이 개그콘서트에서")
    assert text.endswith("→ 보낸 답 '봉숭아학당' · 채팅에 올라감 확인")
    add_quiz(conn, "q3", "다음 문제", "정답", "pending", stamp(0))
    assert live.last_quiz_text(conn, now).endswith("→ 답 '정답' 확인 대기 (퀴즈 기록에서 보내기)")
    add_quiz(conn, "q4", "웃긴 문제", "정답", "entered", stamp(0), answer_kind="witty", witty_answer="엉뚱한 답")
    assert "기발한 오답 '엉뚱한 답' · 채팅 입력함·올라감 미확인" in live.last_quiz_text(conn, now)
    add_quiz(conn, "q5", "정답 발표", "x", "posted", stamp(0), kind="answer_reveal")               # 정답 발표는 제외
    assert "웃긴 문제" in live.last_quiz_text(conn, now)
    assert "웃긴 문제" in client.get("/live.json").get_json()["quiz_text"]
    assert 'id="last-quiz"' in client.get("/").get_data(as_text=True) and "엉뚱한 답" in client.get("/").get_data(as_text=True)


def test_entry_label_says_chat_or_sms():
    from radio_helper import quiz

    assert quiz.entry_label("posted", "gorilla") == "채팅에 올라감 확인"
    assert quiz.entry_label("entered", None) == "채팅 입력함·올라감 미확인"
    assert quiz.entry_label("entered", "sms") == "문자 보냄" and quiz.entry_label("failed", "sms") == "실패"
