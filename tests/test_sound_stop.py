"""듣는 도중 라디오 소리가 끊기는 문제:

1) 고릴라 전송이 엉뚱한 곳을 누르지 않게 — 누를 자리를 다른 창이 가리면, 창 크기·자리가 위치 지정 때와
   다르면, 그 창이 맨 앞이 아니면 누르거나 키를 보내지 않는다.
2) 소리가 끊기면 '끊기기 직전 도우미가 한 일'과 기본 스피커 변경을 기록하고, 다시 들리면 끊긴 시간을 남긴다.
"""

import sys
from datetime import datetime, timedelta
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest
from test_quizbot import MON_0700, Clock, FakeGorilla, ScriptTranscriber, add_schedule, analysis, run_one_window

from radio_helper import db
from radio_helper.quizbot import config, gorilla, live, runner, silence
from radio_helper.quizbot.answerer import QuizAnalysis


# ── 1) 고릴라 오클릭·키 입력 방지 ────────────────────────────────────
def test_layout_checks_are_pure():
    assert gorilla.size_changed("640,950", (0, 0, 640, 950)) is None
    assert gorilla.size_changed("640,950", (10, 10, 670, 990)) is None            # 조금 바뀐 것은 괜찮음
    assert "640×950" in gorilla.size_changed("640,950", (0, 0, 1280, 700))
    assert gorilla.size_changed("", (0, 0, 1, 1)) is None                        # 모르면 막지 않음
    assert gorilla.window_moved("700,0,1340,950", (700, 0, 1340, 950)) is None
    assert gorilla.window_moved("700,0,1340,950", (100, 0, 740, 950))
    assert gorilla.window_moved("", (0, 0, 1, 1)) is None
    assert gorilla.covered_by({"hwnd": 7}, 7) is None and gorilla.covered_by({"hwnd": 9}, None) is None
    assert "라디오 자막" in gorilla.covered_by({"hwnd": 9, "title": "라디오 자막 · 라디오 참여 도우미",
                                               "process": "python.exe"}, 7)


def test_window_region_remembers_window_position():
    app = {"title": "공감로그", "process": "gorealra.exe", "rect": (700, 0, 1340, 950)}
    st, _ = gorilla.region_settings("window", (700, 0, 1340, 950), app, None)
    assert st["gorilla.region_window"] == "700,0,1340,950"
    assert config.app_settings("kong", st)["kong.region_window"] == "700,0,1340,950"


class Win:
    handle = 1001

    def __init__(self, rect=(1000, 0, 1640, 950)):
        self.rect = rect

    def rectangle(self):
        left, top, right, bottom = self.rect
        return SimpleNamespace(left=left, top=top, right=right, bottom=bottom)


@pytest.fixture
def desk(monkeypatch):
    """가짜 윈도우 화면: 누른 자리·보낸 키를 기록한다."""
    state = SimpleNamespace(clicks=[], keys=[], at_point={"hwnd": 1001, "title": "공감로그", "process": "gorealra.exe"},
                            front=1001)
    pw = ModuleType("pywinauto")
    pw.mouse = SimpleNamespace(click=lambda coords: state.clicks.append(coords))
    keyboard = ModuleType("pywinauto.keyboard")
    keyboard.send_keys = lambda keys, pause=None: state.keys.append(keys)
    monkeypatch.setitem(sys.modules, "pywinauto", pw)
    monkeypatch.setitem(sys.modules, "pywinauto.keyboard", keyboard)
    monkeypatch.setattr(gorilla, "window_at_point", lambda x, y: dict(state.at_point, point=(x, y)))
    monkeypatch.setattr(gorilla, "foreground_root", lambda: state.front)
    monkeypatch.setattr(gorilla.Gorilla, "_clipboard", staticmethod(lambda text=None: text))
    monkeypatch.setattr(gorilla, "FRONT_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(gorilla.time, "sleep", lambda s: None)
    return state


def coords_gorilla(**over):
    v = dict(process_name="gorealra.exe", window_size="640,950", input_mode="coords", send_mode="coords",
             input_x=0.5, input_y=0.95, send_x=0.9, send_y=0.95)
    v.update(over)
    return gorilla.Gorilla(config.GorillaConfig(**v))


def test_coords_send_clicks_only_when_safe(desk):
    g, w = coords_gorilla(), Win()
    assert g._put_text(w, "사과") is None
    g._press_send(w)
    assert desk.clicks == [(1320, 902), (1576, 902)] and desk.keys == ["^a{BACKSPACE}^v"]


def test_resized_window_is_not_clicked(desk):
    with pytest.raises(gorilla.GorillaError, match="창 크기"):
        coords_gorilla()._put_text(Win((0, 0, 1280, 700)), "사과")        # 최대화가 풀리는 등 크기가 바뀜
    assert desk.clicks == [] and desk.keys == []


def test_covered_point_is_not_clicked(desk):
    desk.at_point = {"hwnd": 2002, "title": "라디오 자막 · 라디오 참여 도우미", "process": "python.exe"}
    with pytest.raises(gorilla.GorillaError, match="가리고"):
        coords_gorilla()._put_text(Win(), "사과")
    desk.at_point = {"hwnd": 3003, "title": "고릴라", "process": "gorealra.exe"}   # 같은 앱의 플레이어 창도 안 됨
    with pytest.raises(gorilla.GorillaError, match="가리고"):
        coords_gorilla()._press_send(Win())
    assert desk.clicks == [] and desk.keys == []


def test_keys_only_go_to_the_front_window(desk):
    desk.front = 4004    # 클릭해도 고릴라가 맨 앞으로 오지 않음 (다른 프로그램이 앞에 있음)
    with pytest.raises(gorilla.GorillaError, match="맨 앞"):
        coords_gorilla()._put_text(Win(), "사과")
    assert desk.keys == []                                   # 다른 프로그램에 지우기 키가 들어가지 않음
    with pytest.raises(gorilla.GorillaError, match="맨 앞"):
        coords_gorilla(send_mode="enter")._press_send(Win())
    assert desk.keys == []


def test_moved_window_region_is_not_clicked(desk):
    g = coords_gorilla(screen_region="1000,0,1640,950", region_window="1000,0,1640,950", window_size="")
    g._window = lambda: Win()
    g._put_text(Win(), "사과")
    assert desk.clicks == [(1320, 902)]
    with pytest.raises(gorilla.GorillaError, match="다른 자리"):
        g._put_text(Win((200, 0, 840, 950)), "사과")           # 영역을 지정한 뒤 고릴라 창을 옮김
    assert len(desk.clicks) == 1


# ── 2) 소리 끊김 감시 ──────────────────────────────────────────────
def test_silence_watch_cut_recheck_and_back():
    t0 = datetime(2026, 10, 9, 18, 0, 0)
    sw = silence.SilenceWatch()
    for s in range(30):                                       # 처음부터 조용한 것은 끊김이 아님
        assert sw.feed(t0 + timedelta(seconds=s), 0) is None
    assert sw.feed(t0 + timedelta(seconds=30), 60) is None
    got = [sw.feed(t0 + timedelta(seconds=31 + s), 0) for s in range(25)]
    assert [g for g in got if g] == [("cut", 20.0)]           # 한 번만 알림
    assert not sw.due_recheck(t0 + timedelta(seconds=70))
    assert sw.due_recheck(t0 + timedelta(seconds=112))
    assert sw.feed(t0 + timedelta(seconds=120), 55) == ("back", 89.0)
    sw.touch(t0 + timedelta(seconds=10), "고릴라 채팅 전송 (위치 클릭)")
    assert sw.touches_before(t0 + timedelta(seconds=31)) and not sw.touches_before(t0 + timedelta(seconds=200))
    assert "건드리지 않았습니다" in silence.cut_message(t0, 20, [])


class LevelRecorder:
    """1초마다 소리 크기를 알리는 가짜 녹음기. 고릴라 전송 뒤 40초 동안 소리가 끊겼다가 돌아온다."""

    def __init__(self, clock, chunk, sender, conn):
        self.clock, self.chunk, self.sender, self.conn = clock, chunk, sender, conn
        self.on_level, self.device_name, self.reopened = None, "스피커(이어폰)", 0
        self.cut_at, self.seen = None, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def reopen(self):
        self.reopened += 1
        self.device_name = "모니터 스피커(HDMI)"     # 이어폰 연결이 끊겨 기본 스피커가 바뀜
        return self.device_name

    def read_chunk(self):
        self.seen.append(db.get_setting(self.conn, "quizbot.silent_since"))
        if self.sender.sent and self.cut_at is None:
            self.cut_at = self.clock.now()
        quiet = self.cut_at is not None and self.clock.now() - self.cut_at < timedelta(seconds=40)
        for _ in range(self.chunk):
            self.clock.sleep(1)
            self.on_level(0 if quiet else 60)
        return np.zeros(16_000, dtype=np.float32) if quiet else np.full(16_000, 0.1, dtype=np.float32)


def test_cut_after_send_is_logged_with_last_action_and_speaker_change(conn):
    add_schedule(conn)
    clock = Clock(MON_0700)
    sender = FakeGorilla()
    sender.cfg = SimpleNamespace(input_mode="coords", send_mode="coords")
    recorders = []

    def make_recorder(chunk):
        recorders.append(LevelRecorder(clock, chunk, sender, conn))
        return recorders[-1]

    ans = SimpleNamespace(analyze=lambda program, transcript, known: QuizAnalysis.from_json(
        __import__("json").dumps(analysis())))
    deps = runner.Deps(recorder_factory=make_recorder,
                       transcriber_factory=lambda model, program: ScriptTranscriber({3: "오늘의 퀴즈 철수가 좋아하는 과일은"}),
                       answerer=ans, sender=sender, now=clock.now, sleep=clock.sleep)
    r = runner.Runner(conn, deps)
    run_one_window(conn, r, clock)

    assert sender.sent == ["사과"]
    msgs = [m for (m,) in conn.execute("SELECT message FROM events ORDER BY id")]
    cut = next(m for m in msgs if m.startswith("소리 끊김 —"))
    assert "직전 도우미가 한 일" in cut and "고릴라 채팅 전송 (위치 클릭)" in cut
    assert "'스피커(이어폰)' → '모니터 스피커(HDMI)'" in cut and recorders[0].reopened == 1
    back = next(m for m in msgs if m.startswith("소리 다시 들림"))
    assert "초 끊겼습니다" in back
    assert msgs.index(cut) < msgs.index(back)
    assert not any("소리가 들리지 않습니다" in m for m in msgs)   # 듣다가 끊긴 것은 위 기록으로만
    assert any(recorders[0].seen) and db.get_setting(conn, "quizbot.silent_since") == ""


def test_banner_shows_only_while_collecting(client, conn):
    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    for k, v in {"live.active": "1", "quizbot.state": "듣는 중", "quizbot.heartbeat": stamp,
                 "quizbot.level_at": stamp, "quizbot.silent_since": "2026-10-09 18:00:05"}.items():
        db.set_setting(conn, k, v)
    vm = live.view_model(conn, now)
    assert vm["status"] == "collecting" and vm["silence_text"].startswith("소리 끊김: 18:00:05부터")
    assert "18:00:05부터 소리가 없습니다" in client.get("/").get_data(as_text=True)
    db.set_setting(conn, "live.active", "0")
    assert live.view_model(conn, now)["silence_text"] == ""


def test_listener_runs_at_lower_priority():
    from radio_helper.quizbot import __main__ as qmain

    assert qmain.lower_priority() is (sys.platform == "win32")
