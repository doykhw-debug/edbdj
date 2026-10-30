"""0.93: 메모리 부족(0xc000012d)·보조 5~10 칸이 켜지지 않던 문제 — 공용 받아쓰기, 메모리 확인, 한 칸씩 띄우기."""

import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from radio_helper import db
from radio_helper.quizbot import __main__ as qmain
from radio_helper.quizbot import lanes, runner, stt_server
from radio_helper.quizbot.stt import WhisperTranscriber


class FakeWhisper:
    """faster-whisper 흉내: 받은 소리 길이와 힌트를 글자로 돌려준다."""

    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **kw):
        self.calls.append(kw)
        secs = len(audio) / 16000
        return [SimpleNamespace(start=0.0, end=secs, text=f" {secs:.0f}초 {kw.get('hotwords', '')}")], None


@pytest.fixture
def server(tmp_path):
    model = FakeWhisper()
    t = threading.Thread(target=stt_server.serve, args=(tmp_path, model, {"model": "large-v3-turbo", "device": "cuda",
                                                                           "workers": 4}),
                         kwargs={"say": lambda m: None, "idle": 600}, daemon=True)
    t.start()
    for _ in range(100):
        st = stt_server.running(tmp_path)
        if st:
            break
        time.sleep(0.05)
    return SimpleNamespace(folder=tmp_path, model=model, state=st)


def test_pack_roundtrip():
    audio = np.linspace(-1, 1, 1600, dtype=np.float32)
    back, params = stt_server.unpack(stt_server.pack(audio, {"hotwords": "파워FM 정답"}))
    assert np.allclose(back, audio) and params == {"hotwords": "파워FM 정답"}


def test_lanes_share_one_model(server):
    assert server.state and server.state["model"] == "large-v3-turbo"
    remote = stt_server.RemoteModel(server.state["url"])
    tr = WhisperTranscriber("large-v3-turbo", "두시탈출", "cuda", hints="컬투 정답", overlap_seconds=0, engine=remote)
    text = tr.feed(np.zeros(16000 * 5, dtype=np.float32))
    assert text == "5초 컬투 정답" and "공용 받아쓰기" in tr.label
    assert server.model.calls[0]["language"] == "ko" and server.model.calls[0]["initial_prompt"].startswith("라디오 두시탈출")
    seen = []
    workers = [threading.Thread(target=lambda: seen.append(remote.transcribe(np.zeros(16000, dtype=np.float32))))
               for _ in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(5)
    assert len(seen) == 4                                                         # 여러 칸이 동시에 보내도 됨


def test_ensure_launches_once_and_waits(tmp_path):
    launched = []
    states = iter([None, None, None, {"url": "http://127.0.0.1:1", "model": "m"}])
    st = stt_server.ensure(tmp_path, launched.append, ["--model", "m"], sleep=lambda s: None,
                           check=lambda folder: next(states))
    assert st["model"] == "m" and launched == [["stt-server", "--model", "m"]]
    assert not (tmp_path / stt_server.LOCK_FILE).exists()
    with pytest.raises(RuntimeError):
        stt_server.ensure(tmp_path, launched.append, [], wait=0.05, sleep=lambda s: time.sleep(0.01),
                          check=lambda folder: None)


def test_shared_model_falls_back_when_server_fails(conn, monkeypatch):
    monkeypatch.setattr(stt_server, "ensure", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("안 뜸")))
    assert runner.shared_model(conn, "large-v3-turbo", "cuda", launch=lambda a: None) is None
    assert "이 청취기에서 모델을 올립니다" in conn.execute("SELECT message FROM events ORDER BY id DESC").fetchone()[0]


def test_shared_when_three_or_more_lanes(conn):
    assert not lanes.shared_stt(conn)                                              # 한 채널만: 예전처럼
    db.set_setting(conn, "live.multi", "1")
    db.set_setting(conn, "live.lanes", json.dumps([{"channel": "MBC FM4U"}, {"channel": "CBS 음악FM"}]))
    assert lanes.shared_stt(conn) and lanes.stt_workers(conn) == 3
    db.set_setting(conn, "quizbot.stt_shared", "0")
    assert not lanes.shared_stt(conn)


# ── 보조 5~10 칸 · 메모리 · 한 칸씩 ─────────────────────────────────────────
def test_lane_command_accepts_lanes_up_to_ten(monkeypatch):
    got = []
    monkeypatch.setattr(qmain, "cmd_lane", lambda conn, lane: got.append(lane) or 0)
    monkeypatch.setattr(qmain, "lower_priority", lambda: None)
    assert qmain.main(["lane", "--lane", "8"]) == 0 and got == ["8"]               # 0.92 까지는 2~4 만 받아 바로 꺼졌음


def test_lanes_start_one_at_a_time_and_need_memory(conn, monkeypatch):
    monkeypatch.setattr(lanes.config, "instance_lock", lambda name: SimpleNamespace(close=lambda: None))
    db.set_setting(conn, "live.active", "1")
    db.set_setting(conn, "live.multi", "1")
    db.set_setting(conn, "live.lanes", json.dumps([{"channel": "MBC FM4U"}, {"channel": "EBS FM"}, {"channel": "YTN 라디오"}]))
    launched = []
    assert lanes.ensure(conn, lambda a: launched.append(a) or "x.log", now=1000.0, free_mb=lambda: 8000) == ["2"]
    assert lanes.ensure(conn, lambda a: launched.append(a) or "x.log", now=1030.0, free_mb=lambda: 8000) == ["3"]
    assert lanes.ensure(conn, lambda a: launched.append(a) or "x.log", now=1060.0, free_mb=lambda: 900) == []
    assert "메모리 부족" in db.get_setting(conn, "lane4.state") and len(launched) == 2


def test_ldplayer_send_skips_screen_queue(conn):
    """LDPlayer 앱은 ADB 로 보내 화면·마우스를 쓰지 않으므로 PC 앱 차례 줄에 서지 않는다 (느린 명령이 모두를 막았음)."""
    from contextlib import contextmanager

    from radio_helper import autofill
    from test_live import live_runner, start_live
    from test_quizbot import FakeGorilla, ScriptAnswerer, analysis

    @contextmanager
    def busy(wait_seconds=0):
        raise autofill.AutofillError("다른 입력 작업이 진행 중입니다.")
        yield

    start_live(conn)
    ld = FakeGorilla()
    ld.screen_png = lambda: None                                               # LDPlayer 도구 흉내
    assert runner.uses_adb(ld) and not runner.uses_adb(FakeGorilla())
    r, _ = live_runner(conn, {3: "자 오늘의 퀴즈 나갑니다 철수가 좋아하는 과일은 정답은 고릴라로 보내주세요"},
                       ScriptAnswerer([analysis()]), ld, n=12)
    r.deps.lock = busy                                                         # PC 앱 차례는 꽉 차 있어도
    r.run_live()
    assert ld.sent == ["사과"]                                                  # LDPlayer 는 바로 보냄
