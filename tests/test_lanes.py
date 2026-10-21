"""동시 청취 (0.53): 한 PC 에서 방송국마다 한 채널씩 함께 듣고 받아쓰고 보낸다.

윈도우 볼륨 믹서에서 앱마다 출력 장치를 나누고(고릴라 → 스피커, 콩 → 가상 장치 A, mini → 가상 장치 B),
채널마다 그 장치를 따로 녹음한다 (앱 소리 직접 받기는 test_app_audio). 보조 채널은 보조 청취기(별도 프로세스)가 듣고, 보내기는 공용 잠금으로 차례로.
"""

import json

import numpy as np
from conftest import csrf
from test_live import live_runner, start_live
from test_quizbot import FakeGorilla, ScriptAnswerer, analysis

from radio_helper import db
from radio_helper.quizbot import audio, config, lanes, live, runner


def set_lanes(conn, multi="1", device="", extra=()):
    db.set_setting(conn, "live.multi", multi)
    db.set_setting(conn, "live.device", device)
    db.set_setting(conn, "live.lanes", json.dumps(list(extra), ensure_ascii=False))


# ── 설정 ──────────────────────────────────────────────────────────
def test_lanes_and_problems(conn):
    db.set_setting(conn, "live.channel", "파워FM")
    set_lanes(conn, extra=[{"channel": "KBS 쿨FM", "device": "CABLE Input"}, {"channel": "", "device": ""}])
    assert [x["channel"] for x in lanes.all_lanes(conn)] == ["파워FM", "KBS 쿨FM"]
    assert lanes.lane_of(conn, "2")["device"] == "CABLE Input" and lanes.lane_of(conn, "3") is None
    assert lanes.problems(conn, lanes.all_lanes(conn)) == []
    same_app = [{"lane": "", "channel": "파워FM", "device": ""}, {"lane": "2", "channel": "러브FM", "device": "CABLE"}]
    assert "둘 다 고릴라 앱" in lanes.problems(conn, same_app)[0]
    same_device = [{"lane": "", "channel": "파워FM", "device": ""}, {"lane": "2", "channel": "KBS 쿨FM", "device": ""}]
    assert "같은 녹음 장치" in lanes.problems(conn, same_device)[0]
    set_lanes(conn, multi="0", extra=[{"channel": "KBS 쿨FM", "device": "CABLE Input"}])
    assert [x["channel"] for x in lanes.all_lanes(conn)] == ["파워FM"]            # 끄면 주 채널만


def test_should_run_and_ensure_launch(conn):
    set_lanes(conn, extra=[{"channel": "KBS 쿨FM", "device": "CABLE Input"}, {"channel": "MBC FM4U", "device": "CABLE-A"}])
    launched = []

    def launch(args):
        launched.append(args)
        return "quizbot_x.log"

    assert lanes.ensure(conn, launch, now=1000.0) == [] and not lanes.should_run(conn, "2")   # 청취 중이 아님
    db.set_setting(conn, "live.active", "1")
    assert lanes.should_run(conn, "2")
    assert lanes.ensure(conn, launch, now=1000.0) == ["2", "3"]
    assert launched == [["lane", "--lane", "2"], ["lane", "--lane", "3"]]
    assert lanes.ensure(conn, launch, now=1050.0) == []                           # 막 띄운 것은 2분 동안 다시 안 띄움
    assert lanes.ensure(conn, launch, now=1200.0) == ["2", "3"]                   # 꺼졌으면(잠금이 풀림) 다시
    db.set_setting(conn, "quizbot.stop", "1")
    assert not lanes.should_run(conn, "2")


def test_pick_loopback_device_by_name():
    devs = [{"name": "스피커 (Realtek(R) Audio) [Loopback]", "index": 1},
            {"name": "CABLE Input (VB-Audio Virtual Cable) [Loopback]", "index": 2},
            {"name": "CABLE-A Input (VB-Audio Cable A) [Loopback]", "index": 3}]
    assert audio.pick_loopback(devs, "CABLE Input (VB-Audio Virtual Cable)")["index"] == 2
    assert audio.pick_loopback(devs, "cable-a input")["index"] == 3
    assert audio.pick_loopback(devs, "없는 장치") is None and audio.pick_loopback(devs, "") is None


# ── 보조 청취기 ────────────────────────────────────────────────────
def test_lane_runner_listens_its_channel_and_sends_with_its_app(conn):
    start_live(conn, channel="파워FM")
    set_lanes(conn, extra=[{"channel": "KBS 쿨FM", "device": "CABLE Input"}])
    kong = FakeGorilla()
    r, _ = live_runner(conn, {3: "오늘의 퀴즈 철수가 좋아하는 과일은 정답은 콩 채팅으로"}, ScriptAnswerer([analysis()]),
                       FakeGorilla(), n=12)
    r.deps.chat_senders = {"kong": kong}
    r.lane = "2"
    r.run_lane()
    assert kong.sent == ["사과"]                                                  # KBS 쿨FM → 콩 앱으로
    rows = conn.execute("SELECT DISTINCT channel FROM transcripts").fetchall()
    assert [x[0] for x in rows] == ["KBS 쿨FM"]
    q = conn.execute("SELECT * FROM quizzes").fetchone()
    assert q["channel"] == "KBS 쿨FM"
    assert db.get_setting(conn, "lane2.state") == "멈춤" and db.get_setting(conn, "lane2.last_chunk")
    assert not db.get_setting(conn, "quizbot.last_chunk")                         # 주 채널 상태는 건드리지 않음
    msgs = " ".join(m for (m,) in conn.execute("SELECT message FROM events"))
    assert "동시 청취 시작 — 보조 2 KBS 쿨FM (CABLE Input)" in msgs


def test_lane_stops_when_multi_turned_off(conn):
    start_live(conn, channel="파워FM")
    set_lanes(conn, extra=[{"channel": "KBS 쿨FM", "device": "CABLE Input"}])

    def turn_off(i):
        if i == 3:
            db.set_setting(conn, "live.multi", "0")

    r, recorders = live_runner(conn, {}, ScriptAnswerer([]), FakeGorilla(), n=50, on_chunk=turn_off)
    r.lane = "2"
    r.run_lane()
    assert recorders[0].i == 3 and config.get(conn, "live.active") == "1"         # 보조만 멈추고 주 청취는 그대로


def test_hourly_limit_counts_each_channel(conn):
    now = db.now()
    for ch in ("KBS 쿨FM",) * 4:
        conn.execute("INSERT INTO quizzes (dedupe_key, account, channel, program, broadcast_date, question_key, kind, "
                     "source, sent_at, created_at, updated_at) VALUES (?, '기본', ?, 'p', '2026-10-20', ?, 'new', 'auto', ?, "
                     "?, ?)", (f"k{now}{ch}{conn.total_changes}", ch, f"q{conn.total_changes}", now, now, now))
    conn.commit()
    start_live(conn, channel="파워FM")
    sender = FakeGorilla()
    r, _ = live_runner(conn, {3: "오늘의 퀴즈 철수가 좋아하는 과일은 정답은 고릴라로"}, ScriptAnswerer([analysis()]), sender, n=12)
    r.run_live()
    assert sender.sent == ["사과"]                                                 # KBS 쿨FM 4건이 파워FM 상한을 쓰지 않음


# ── 장치 점검 ──────────────────────────────────────────────────────
class ToneRecorder:
    def __init__(self, samples, name):
        self.samples, self.device_name = samples, name

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read_chunk(self):
        return self.samples


def test_device_test_finds_silent_and_shared_devices(conn):
    db.set_setting(conn, "live.channel", "파워FM")
    set_lanes(conn, extra=[{"channel": "KBS 쿨FM", "device": "CABLE Input"}, {"channel": "MBC FM4U", "device": "CABLE-A"}])
    t = np.arange(16_000 * 10) / 16_000
    voice = (0.3 * np.sin(2 * np.pi * 220 * t) * (np.sin(2 * np.pi * 0.7 * t) > 0)).astype(np.float32)
    sounds = {"": voice, "CABLE Input": voice.copy(), "CABLE-A": np.zeros(16_000 * 10, dtype=np.float32)}
    result = lanes.device_test(conn, recorder_factory=lambda x: ToneRecorder(sounds[x["device"]], x["device"] or "스피커"),
                               transcriber=lambda s: "안녕하세요 파워FM", gpu_ok=False)
    items = {it["channel"]: it for it in result["items"]}
    assert items["파워FM"]["level"] > 8 and items["파워FM"]["text"] == "안녕하세요 파워FM"
    assert items["MBC FM4U"]["level"] == 0 and items["MBC FM4U"]["text"] == ""
    text = " ".join(result["warnings"])
    assert "'MBC FM4U': 소리가 없습니다" in text                                   # 출력 장치를 안 나눔·재생 안 함
    assert "같은 소리" in text                                                    # 파워FM·KBS 쿨FM 장치에 같은 소리
    assert "그래픽카드" in text                                                    # CPU 면 여러 채널이 느릴 수 있음
    assert lanes.saved_test(conn)["items"][0]["channel"] == "파워FM"


def test_device_test_ok_when_separate(conn):
    db.set_setting(conn, "live.channel", "파워FM")
    set_lanes(conn, extra=[{"channel": "KBS 쿨FM", "device": "CABLE Input"}])
    rng = np.random.default_rng(1)

    def speech(seed):
        r = np.random.default_rng(seed)
        env = np.repeat(r.random(100) > 0.5, 1600).astype(np.float32)
        return (0.3 * rng.standard_normal(16_000 * 10) * env).astype(np.float32)

    sounds = {"": speech(2), "CABLE Input": speech(3)}
    result = lanes.device_test(conn, recorder_factory=lambda x: ToneRecorder(sounds[x["device"]], x["device"]), transcriber=lambda s: "말",
                               gpu_ok=True)
    assert result["warnings"] == []


def test_device_open_error_is_reported(conn):
    db.set_setting(conn, "live.channel", "파워FM")
    set_lanes(conn, extra=[{"channel": "KBS 쿨FM", "device": "없는 장치"}])

    def factory(x):
        if x["device"] == "없는 장치":
            raise RuntimeError("녹음 장치 '없는 장치'를 찾지 못했습니다")
        return ToneRecorder(np.full(160_000, 0.2, dtype=np.float32), "스피커")

    result = lanes.device_test(conn, recorder_factory=factory, transcriber=None, gpu_ok=True)
    assert "녹음 장치를 열지 못함" in " ".join(result["warnings"])


# ── 화면 ──────────────────────────────────────────────────────────
def test_home_panel_save_and_conflict(client, conn, monkeypatch):
    from radio_helper import app as app_module

    launched = []
    monkeypatch.setattr(app_module, "launch_quizbot", lambda args: launched.append(args) or "quizbot_x.log")
    html = client.get("/").get_data(as_text=True)
    assert "동시 청취" in html and "VB-Audio Virtual Cable" in html and "볼륨 믹서" in html
    token = csrf(client)
    r = client.post("/listen/lanes", data={"csrf_token": token, "multi": "1", "device": "",
                                           "lane2_channel": "러브FM", "lane2_device": "CABLE Input",
                                           "lane3_channel": "", "lane3_device": ""}, follow_redirects=True)
    assert "동시 청취를 켜지 않았습니다" in r.get_data(as_text=True) and config.get(conn, "live.multi") == "0"
    db.set_setting(conn, "live.active", "1")
    r = client.post("/listen/lanes", data={"csrf_token": token, "multi": "1", "device": "",
                                           "lane2_channel": "KBS 쿨FM", "lane2_device": "CABLE Input",
                                           "lane3_channel": "MBC FM4U", "lane3_device": "CABLE-A Input"},
                    follow_redirects=True)
    assert config.get(conn, "live.multi") == "1" and launched == [["lane", "--lane", "2"], ["lane", "--lane", "3"]]
    page = r.get_data(as_text=True)
    assert "보조 2" in page and "KBS 쿨FM" in page and "CABLE-A Input" in page
    client.post("/listen/lanes/test", data={"csrf_token": token})
    client.post("/stt/gpu-recheck", data={"csrf_token": token})
    assert launched[-2:] == [["device-test"], ["gpu-recheck"]]


def test_captions_show_channel_when_multi(conn):
    for ch, text in (("파워FM", "오늘의 퀴즈"), ("KBS 쿨FM", "사연 보내 주세요")):
        conn.execute("INSERT INTO transcripts (broadcast_date, at, text, channel, program) VALUES "
                     "('2026-10-20', '2026-10-20 12:00:00', ?, ?, 'p')", (text, ch))
    conn.commit()
    assert [x["text"] for x in live.view_model(conn)["lines"]] == ["오늘의 퀴즈", "사연 보내 주세요"]
    db.set_setting(conn, "live.multi", "1")
    assert [x["text"] for x in live.view_model(conn)["lines"]] == ["[파워FM] 오늘의 퀴즈", "[KBS 쿨FM] 사연 보내 주세요"]


def test_lane_state_keys():
    assert lanes.key("", "state") == "quizbot.state" and lanes.key("3", "heartbeat") == "lane3.heartbeat"
    assert runner.Runner(None, None, lane="2").key("last_chunk") == "lane2.last_chunk"


def test_item_taken_by_another_listener_is_not_sent_twice(conn):
    """동시 청취: 다른 청취기가 먼저 가져가 보내는 중(결과 불명)인 글은 다시 보내지 않는다."""
    start_live(conn, channel="파워FM")
    sender = FakeGorilla()
    r, _ = live_runner(conn, {}, ScriptAnswerer([]), sender, n=1)
    now = db.now()
    cur = conn.execute("INSERT INTO quizzes (dedupe_key, account, channel, program, broadcast_date, question_key, kind, "
                       "source, entry_status, answer, created_at, updated_at) VALUES ('k1', '기본', '파워FM', 'p', "
                       "'2026-10-20', 'q1', 'new', 'auto', 'unknown', '사과', ?, ?)", (now, now))
    conn.commit()
    assert r.deliver("quizzes", "entry_status", cur.lastrowid, "app", "파워FM", "사과", "자동 전송", "퀴즈") is None
    assert sender.sent == []


# ── 0.58: 동시 청취에서 보내기 차례 ──────────────────────────────────
def test_stale_lock_from_closed_program_is_cleared(data_dir):
    from radio_helper import autofill

    path = db.data_dir() / "input.lock"
    path.write_text("999999999", encoding="ascii")          # 보내는 도중 꺼진 프로그램이 남긴 잠금
    with autofill.input_lock():                              # 80분을 기다리지 않고 바로 잡음
        assert path.read_text(encoding="ascii") == str(__import__("os").getpid())
    path.write_text(str(__import__("os").getpid()), encoding="ascii")   # 살아 있는 프로그램의 잠금은 그대로
    import pytest

    with pytest.raises(autofill.AutofillError):
        with autofill.input_lock():
            pass
    path.unlink()


def test_busy_turn_is_retried_not_marked_unknown(conn):
    """다른 칸이 보내는 중이라 차례를 못 잡으면 아무것도 누르지 않은 것 → '결과 불명'이 아니라 다시 시도."""
    from contextlib import contextmanager

    from radio_helper import autofill
    from test_quizbot import ScriptAnswerer

    @contextmanager
    def busy(wait_seconds=0):
        raise autofill.AutofillError("다른 입력 작업이 진행 중입니다.")
        yield

    start_live(conn)
    sender = FakeGorilla()
    r, _ = live_runner(conn, {3: "자 오늘의 퀴즈 나갑니다 철수가 좋아하는 과일은 정답은 고릴라로 보내주세요"},
                       ScriptAnswerer([analysis()]), sender, n=12)
    r.deps.lock = busy
    r.run_live()
    row = conn.execute("SELECT entry_status, decision FROM quizzes WHERE source = 'auto'").fetchone()
    assert row["entry_status"] == "pending" and row["decision"].startswith(runner.RETRY_PREFIX)
    assert "다른 입력 작업이 끝나지 않아" in row["decision"] and sender.tried == []

    @contextmanager
    def free(wait_seconds=0):
        yield

    r.deps.lock = free                                     # 차례가 비면 1분 뒤 다시 시도해서 보냄
    r.deps.sleep(61)
    r.retry_held(live.session(conn, r.deps.now()))
    assert sender.sent == ["사과"]


def test_refind_waits_its_turn(conn):
    from contextlib import contextmanager

    from radio_helper import autofill
    from test_quizbot import ScriptAnswerer

    calls = []

    @contextmanager
    def busy(wait_seconds=0):
        calls.append(wait_seconds)
        raise autofill.AutofillError("busy")
        yield

    start_live(conn)
    r, _ = live_runner(conn, {}, ScriptAnswerer([]), FakeGorilla(), n=1)
    found = []
    r.deps.refind = lambda c, app: found.append(app) or {"settings": {"x": "1"}}
    r.deps.lock = busy
    assert r.refind_chat("파워FM") is False and found == [] and calls == [runner.REFIND_LOCK_WAIT]


def test_home_lists_recent_unsent_with_reason(client, conn):
    now = db.now()
    conn.execute("""INSERT INTO quizzes (dedupe_key, account, channel, program, broadcast_date, question_key, question, answer,
                    source, kind, entry_status, decision, created_at, updated_at)
                    VALUES ('k1', 'a', 'KBS 쿨FM', 'p', '2026-10-21', 'q', '문제', '사과', 'auto', 'new', 'pending',
                            '대기: 전송 못 함 — 1분마다 다시 시도 (다른 입력 작업이 끝나지 않아 아직 못 보냄)', ?, ?)""", (now, now))
    conn.commit()
    rows = live.recent_unsent(conn)
    assert rows[0]["kind"] == "퀴즈" and rows[0]["text"] == "사과" and "다른 입력 작업" in rows[0]["decision"]
    html = client.get("/").get_data(as_text=True)
    assert "최근 못 보낸 퀴즈·사연" in html and "KBS 쿨FM" in html and "다른 입력 작업이 끝나지 않아" in html
