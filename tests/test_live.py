"""'청취 시작' 한 번으로 듣기 · 자막 · 관리 화면."""

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
    assert live.keywords("오늘의 퀴즈 정답은 고릴라로 보내주세요") == ["퀴즈"]
    assert live.keywords("다음 곡 듣고 오겠습니다") == []


def test_view_model_status(conn):
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    assert live.view_model(conn, now)["status"] == "off"
    db.set_setting(conn, "live.active", "1")
    assert live.view_model(conn, now)["status"] == "launching"
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
    assert recorders[0].on_level == r.report_level  # 녹음 중 소리 크기를 화면에 알린다
    assert db.get_setting(conn, "quizbot.level_at")
    messages = [e["message"] for e in conn.execute("SELECT message FROM events")]
    assert "청취 시작 — 러브FM" in messages and "청취 멈춤" in messages


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


def test_held_quiz_is_not_sent_when_gorilla_never_found(conn):
    start_live(conn)
    sender = FakeGorilla(running=False)
    r, _ = live_runner(conn, {3: "오늘의 퀴즈 정답은 고릴라로"}, ScriptAnswerer([analysis()]), sender, n=10)
    r.run_live()
    q = conn.execute("SELECT * FROM quizzes WHERE source = 'auto'").fetchone()
    assert sender.sent == [] and q["entry_status"] == "pending" and q["decision"] == runner.GORILLA_HOLD


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
