"""동시 청취 중 '모든 라디오 소리가 함께 끊김'과 레인보우 소리 0 (0.61).

실제 사례: 고릴라·mini·콩이 모두 모니터 스피커 'LG ULTRAFINE (NVIDIA High Definition Audio)'로 나가는 PC 에서
20분쯤 듣다가 모든 라디오 소리가 끊김 → 화면이 꺼지면 모니터 스피커도 꺼지므로 듣는 동안 화면 꺼짐·절전을 막고,
끊길 때 원인 자료(앱별 소리·기본 스피커·마지막 입력·그래픽카드 드라이버 재시작)를 남긴다.
레인보우는 볼륨 믹서 측정도 0 인데 '기본값으로 돌리세요'만 안내했다 → 재생 여부·들리는 장치·기본 스피커를 알린다.
"""

import ctypes
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import numpy as np
from test_app_audio import FakeSessions
from test_quizbot import MON_0700, Clock, FakeGorilla, ScriptTranscriber, add_schedule, analysis, run_one_window

from radio_helper import db
from radio_helper.quizbot import clues, lanes, power, runner, silence
from radio_helper.quizbot.answerer import QuizAnalysis

LG = "LG ULTRAFINE(NVIDIA High Definition Audio)"
REALTEK = "스피커(Realtek(R) Audio)"
CABLE = "CABLE Input(VB-Audio Virtual Cable)"
PROCS = {1: (0, "explorer.exe"), 40: (1, "gorealra.exe"), 50: (1, "mbc mini.exe"), 60: (1, "kbskong.exe"),
         70: (1, "flutter_rainbow.exe"), 80: (1, "python.exe"), 81: (1, "python.exe")}


def session(pid, peak, device, default=False):
    return {"pid": pid, "peak": peak, "active": True, "device": device, "default": default, "default_name": CABLE}


# 사용자 화면 그대로: 세 앱은 LG 모니터, 레인보우는 Realtek 에서 0, 기본 스피커는 둘 다 아님
SCREEN = [session(50, 0.17, LG), session(80, 0.17, LG), session(60, 0.12, LG), session(40, 0.11, LG),
          session(81, 0.0, CABLE, default=True), session(70, 0.0, REALTEK)]


# ── 화면 꺼짐·절전 막기 ──────────────────────────────────────────────
class FakeKernel32:
    def __init__(self):
        self.calls = []

    def SetThreadExecutionState(self, flags):
        self.calls.append(flags)
        return 0x80000000


def test_keep_awake_holds_display_and_system_only_while_listening():
    k = FakeKernel32()
    with power.keep_awake(kernel32=k) as ok:
        assert ok and k.calls == [0x80000003]          # ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
    assert k.calls[-1] == 0x80000000                    # 나가면 원래대로
    with power.keep_awake(display=False, kernel32=k):
        assert k.calls[-1] == 0x80000001
    with power.keep_awake() as ok:                      # 윈도우가 아니면 아무것도 안 함
        assert ok is False


def test_idle_seconds_and_gpu_resets():
    class User32:
        def GetLastInputInfo(self, ref):
            ref._obj.dwTime = 1_000
            return 1
    assert power.idle_seconds(User32(), tick=lambda: 1_201_000) == 1200.0
    assert power.idle_seconds(User32(), tick=lambda: 500) == ((500 - 1_000) & 0xFFFFFFFF) / 1000   # 49일 넘김

    seen = []

    def run(args, **kw):
        seen.append(args)
        return SimpleNamespace(returncode=0, stdout="<Events><Event xmlns='x'>a</Event><Event xmlns='x'>b</Event></Events>")
    assert power.gpu_resets(run=run) == 2
    assert "EventID=4101" in seen[0][3] and "timediff(@SystemTime) <= 600000" in seen[0][3]
    assert power.gpu_resets(run=lambda a, **k: SimpleNamespace(returncode=5, stdout="")) is None

    def boom(*a, **k):
        raise OSError("no wevtutil")
    assert power.gpu_resets(run=boom) is None


# ── 끊겼을 때 원인 자료 ──────────────────────────────────────────────
def view(exe, peak, device=""):
    return {"exe": exe, "peak": peak, "device": device}


def test_describe_tells_common_cause_from_one_app():
    others = [view("gorealra.exe", 0, LG), view("mbc mini.exe", 0, LG), view("kbskong.exe", 0, LG), view("python.exe", 17, LG)]
    text = clues.describe("고릴라", others[0], others, CABLE, idle=1300, resets=1)
    assert "앱 자체가 소리를 내지 않습니다" in text
    assert "소리 내는 라디오 앱이 하나도 없습니다" in text and "공통 원인" in text      # 도우미 녹음(python)은 빼고 판단
    assert "가상 장치라 출력을 '기본값'으로 둔 앱 소리는 귀에 들리지 않습니다" in text
    assert "22분 전" in text and "모니터 스피커 소리도 끊깁니다" in text
    assert "그래픽카드 드라이버가 1번 다시 시작됐습니다" in text

    loud = [view("gorealra.exe", 14, LG), view("kbskong.exe", 12, LG)]
    text = clues.describe("고릴라", loud[0], loud, "스피커", idle=30, resets=0)
    assert "녹음 연결이 끊긴 것이라 다시 연결했습니다" in text and "공통 원인" not in text
    assert "마지막 키보드·마우스 입력 30초 전" in text and "그래픽카드" not in text and "가상 장치" not in text

    text = clues.describe("레인보우", None, loud)
    assert "레인보우 소리 길이 없습니다" in text and "재생(▶)" in text
    assert clues.describe("고릴라", None, [], scanned=False) == ""                    # 못 읽었으면 지어내지 않음


def test_collect_reads_this_lanes_app(conn):
    text = clues.collect(conn, "CBS 음악FM", scan=lambda: (PROCS, SCREEN), idle=lambda: 40.0, resets=lambda: None)
    assert "지금 flutter_rainbow.exe 소리 0 ('스피커(Realtek(R) Audio)') — 앱 자체가 소리를 내지 않습니다" in text
    assert f"윈도우 기본 스피커: '{CABLE}'" in text and "mbc mini.exe 17" in text
    text = clues.collect(conn, "CBS 음악FM", scan=lambda: 1 / 0, idle=lambda: 1 / 0, resets=lambda: 1 / 0)
    assert text == ""


def test_cut_event_carries_clues(conn):
    got = []
    deps = runner.Deps(recorder_factory=None, transcriber_factory=None, answerer=None, sender=None,
                       clues=lambda c, channel: got.append(channel) or " [자료]")
    r = runner.Runner(conn, deps)
    r.silence, r._silence_events = silence.SilenceWatch(), [("cut", 25.0, datetime(2026, 10, 21, 22, 5))]
    r.handle_silence(SimpleNamespace(), datetime(2026, 10, 21, 22, 6), "KBS 쿨FM")
    msg = conn.execute("SELECT message FROM events ORDER BY id DESC").fetchone()[0]
    assert got == ["KBS 쿨FM"] and msg.startswith("소리 끊김 —") and msg.endswith(" [자료]")

    def broken(c, channel):
        raise RuntimeError("x")
    r.deps.clues = broken
    r._silence_events = [("cut", 25.0, datetime(2026, 10, 21, 22, 7))]
    r.handle_silence(SimpleNamespace(), datetime(2026, 10, 21, 22, 8), "KBS 쿨FM")
    assert "원인 자료를 읽지 못함" in conn.execute("SELECT message FROM events ORDER BY id DESC").fetchone()[0]


def test_listening_keeps_screen_on_unless_turned_off(conn, monkeypatch):
    held = []

    @contextmanager
    def fake_awake(display=True):
        held.append("on")
        yield True
        held.append("off")
    monkeypatch.setattr(power, "keep_awake", fake_awake)

    def listen_once():
        add_schedule(conn)
        clock = Clock(MON_0700)

        class Rec:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read_chunk(self):
                clock.sleep(10)
                return np.zeros(16_000, np.float32)
        ans = SimpleNamespace(analyze=lambda *a: QuizAnalysis.from_json(__import__("json").dumps(analysis())))
        deps = runner.Deps(recorder_factory=lambda chunk: Rec(), transcriber_factory=lambda m, p: ScriptTranscriber({}),
                           answerer=ans, sender=FakeGorilla(), now=clock.now, sleep=clock.sleep)
        run_one_window(conn, runner.Runner(conn, deps), clock)

    listen_once()
    assert held == ["on", "off"]                         # 기본: 듣는 동안만
    db.set_setting(conn, "live.keep_awake", "0")
    conn.execute("DELETE FROM quiz_schedules")
    listen_once()
    assert held == ["on", "off"]


def test_home_offers_keep_awake_checked(client):
    html = client.get("/").get_data(as_text=True)
    assert 'name="keep_awake" value="1" checked' in html and "화면 꺼짐·절전 막기" in html


# ── 레인보우 소리 0: 장치 점검 안내 ───────────────────────────────────
def test_scanner_reports_windows_default_device():
    fake = FakeSessions([[(41, 1, [0.5])], [(12, 1, [0.0])]], names=[LG, CABLE], default=1)
    out = {x["pid"]: x for x in fake.scanner().scan()}
    assert out[41]["default_name"] == CABLE and not out[41]["default"] and out[12]["default"]


def test_device_test_points_rainbow_to_the_device_you_hear(conn, monkeypatch):
    monkeypatch.setattr(lanes, "app_audio_supported", lambda: True)
    db.set_setting(conn, "live.channel", "파워FM")
    db.set_setting(conn, "live.multi", "1")
    db.set_setting(conn, "live.lanes", '[{"channel": "CBS 음악FM", "device": ""}]')
    db.set_setting(conn, "gorilla.process_name", "gorealra.exe")

    class Rec:
        def __init__(self, samples):
            self.samples, self.device_name = samples, "앱"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read_chunk(self):
            return self.samples

    loud = (0.3 * np.sin(np.arange(160_000) / 5)).astype(np.float32)
    result = lanes.device_test(conn, seconds=1, transcriber=None, gpu_ok=True, scan=lambda: (PROCS, SCREEN),
                               recorder_factory=lambda x: Rec(loud if not x["lane"] else np.zeros(160_000, np.float32)))
    text = " ".join(result["warnings"])
    assert result["default_device"] == CABLE
    assert f"윈도우 기본 스피커가 가상 장치 '{CABLE}' 입니다" in text
    assert "볼륨 믹서에서도 flutter_rainbow.exe 소리가 0 입니다 (출력 '스피커(Realtek(R) Audio)')" in text
    assert "재생(▶)" in text and f"'{LG}'(소리가 나는 다른 채널 앱과 같은 장치)" in text
    assert "'기본값'으로 돌리세요" not in text


def test_loud_device_skips_our_own_recording():
    assert lanes.loud_device(PROCS, SCREEN) == LG
    only_ours = [session(80, 0.3, CABLE), session(70, 0.0, REALTEK)]
    assert lanes.loud_device(PROCS, only_ours) == ""


def test_lastinputinfo_size_matches_windows():
    assert ctypes.sizeof(power.LASTINPUTINFO) == 8
