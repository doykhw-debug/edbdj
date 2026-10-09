"""음성 인식 정확도: 장치·모델 자동 선택, 그래픽카드 점검, 겹쳐 듣기, 후보 수, 단어 힌트."""

import json
import subprocess
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
from test_live import live_runner, start_live
from test_quizbot import FakeGorilla, ScriptAnswerer

from radio_helper import db
from radio_helper.quizbot import config, gpu, runner, stt

NOW = datetime(2026, 10, 9, 9, 0, 0)
REAL_PROBE = gpu.probe   # 테스트 중에는 conftest 가 점검을 막으므로, 읽는 방식만 따로 시험한다


# ── 모델·장치 고르기 ─────────────────────────────────────────────────
def test_resolve_model():
    assert stt.resolve_model("auto", "cuda") == "large-v3"
    assert stt.resolve_model("auto", "cpu", cpu_count=16) == "large-v3-turbo"
    assert stt.resolve_model("auto", "cpu", cpu_count=8) == "small"
    assert stt.resolve_model("medium", "cuda") == "medium"          # 직접 고른 값은 그대로


def test_migrate_old_defaults_once(conn):
    db.set_setting(conn, "quizbot.whisper_model", "small")
    db.set_setting(conn, "quizbot.whisper_device", "cpu")
    assert config.migrate_stt_defaults(conn)
    assert (config.get(conn, "quizbot.whisper_model"), config.get(conn, "quizbot.whisper_device")) == ("auto", "auto")
    # 한 번 바꾼 뒤 사용자가 다시 small·cpu 로 고르면 그대로 둔다
    db.set_setting(conn, "quizbot.whisper_model", "small")
    db.set_setting(conn, "quizbot.whisper_device", "cpu")
    assert not config.migrate_stt_defaults(conn)
    assert config.get(conn, "quizbot.whisper_model") == "small"


def test_migrate_keeps_other_choices(conn):
    db.set_setting(conn, "quizbot.whisper_model", "medium")
    db.set_setting(conn, "quizbot.whisper_device", "cpu")
    config.migrate_stt_defaults(conn)
    assert (config.get(conn, "quizbot.whisper_model"), config.get(conn, "quizbot.whisper_device")) == ("medium", "auto")
    assert config.get(conn, "quizbot.whisper_beam") == "5" and config.get(conn, "quizbot.stt_overlap") == "3"


def test_choose_device_probes_once_and_rechecks(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(gpu, "libs_signature", lambda: "ctranslate2=4.8")

    def probe(result):
        def run():
            calls.append(1)
            return result
        return run

    assert gpu.choose_device(conn, "cpu", NOW, probe((True, "ok"))) == ("cpu", "설정: CPU") and calls == []
    assert gpu.choose_device(conn, "auto", NOW, probe((True, "ok")))[0] == "cuda" and len(calls) == 1
    assert gpu.choose_device(conn, "auto", NOW + timedelta(days=5), probe((False, "x")))[0] == "cuda"
    assert len(calls) == 1                                              # 통과 결과는 저장해서 다시 점검하지 않음

    monkeypatch.setattr(gpu, "libs_signature", lambda: "ctranslate2=4.9")   # 라이브러리가 바뀌면 다시 점검
    device, note = gpu.choose_device(conn, "auto", NOW, probe((False, "종료 코드 3221225477")))
    assert device == "cpu" and "3221225477" in note and len(calls) == 2
    gpu.choose_device(conn, "auto", NOW + timedelta(hours=23), probe((True, "ok")))
    assert len(calls) == 2                                              # 실패 후 하루 전에는 다시 안 함
    assert gpu.choose_device(conn, "auto", NOW + timedelta(days=1), probe((True, "ok")))[0] == "cuda"
    assert len(calls) == 3
    assert any("그래픽카드 점검" in r["message"] for r in conn.execute("SELECT message FROM events"))
    assert json.loads(db.get_setting(conn, gpu.CHECK_KEY))["ok"] is True


def test_probe_reads_check_process(monkeypatch):
    def fake(out, code):
        return lambda *a, **k: subprocess.CompletedProcess(a, code, stdout=out, stderr="")

    monkeypatch.setattr(gpu.subprocess, "run", fake("…\nGPU_OK\n", 0))
    assert REAL_PROBE() == (True, "그래픽카드 받아쓰기 시험 통과")
    monkeypatch.setattr(gpu.subprocess, "run", fake("NO_CUDA_DEVICE\n", 2))
    assert REAL_PROBE() == (False, "NVIDIA 그래픽카드를 찾지 못함")
    monkeypatch.setattr(gpu.subprocess, "run", fake("Could not load library cudnn_ops64_9.dll\n", 3221226505))
    ok, detail = REAL_PROBE()
    assert not ok and "cudnn" in detail and "3221226505" in detail


# ── 단어 힌트 ───────────────────────────────────────────────────────
def test_build_hints():
    h = stt.build_hints(["배성재", "퀴즈"], "딘딘, 배성재")
    words = h.split(", ")
    assert words[:3] == ["딘딘", "배성재", "퀴즈"]                      # 사용자 힌트 먼저, 겹치면 한 번만
    assert "기역" in words and "기프티콘" in words
    assert len(stt.build_hints(["가" * 50] * 20)) <= stt.MAX_HINT_CHARS


def test_stt_hints_use_program_host_and_corners(conn):
    conn.execute("UPDATE programs SET host = '배성재' WHERE code = 'ten'")
    conn.execute("INSERT INTO corners (program, kind, title, board_url) VALUES "
                 "('배성재의 텐', '코너', '[월] 텐 퀴즈쇼', 'u1'), ('배성재의 텐', '코너', '(이름 확인 필요) 코너 4', 'u2')")
    db.set_setting(conn, "quizbot.stt_hints", "성재형")
    words = runner.stt_hints(conn, "배성재의 텐").split(", ")
    assert words[:3] == ["성재형", "배성재", "텐 퀴즈쇼"] and not any("확인 필요" in w for w in words)


# ── 겹쳐 듣기 ───────────────────────────────────────────────────────
def seg(start, end, text):
    return SimpleNamespace(start=start, end=end, text=text)


def test_split_at_overlap():
    segs = [seg(0.5, 3.0, "앞"), seg(4.0, 6.9, "가운데"), seg(7.2, 10.0, "끝")]
    keep, tail = stt.split_at_overlap(segs, 10.0, 3.0)
    assert [s.text for s in keep] == ["앞", "가운데"] and [s.text for s in tail] == ["끝"]
    assert stt.split_at_overlap(segs, 10.0, 0) == (segs, [])          # 0 이면 끔


class TimedModel:
    """받은 소리에서 0.1 이상인 구간마다 말 한 덩이가 있다고 본다 (구간 시작 초 → 글자)."""
    def __init__(self):
        self.lengths, self.kwargs = [], []

    def transcribe(self, audio, **kw):
        self.lengths.append(len(audio))
        self.kwargs.append(kw)
        loud = np.abs(audio) >= 0.1
        out, i = [], 0
        while i < len(loud):
            if loud[i]:
                j = i
                while j < len(loud) and loud[j]:
                    j += 1
                out.append(seg(i / 16_000, j / 16_000, f"말{len(out) + 1}@{i / 16_000:.0f}"))
                i = j
            else:
                i += 1
        return out, None


def make_tr(overlap=3.0, beam=5):
    tr = object.__new__(stt.WhisperTranscriber)
    tr.model, tr.beam_size, tr.overlap, tr.carry, tr._has_hotwords = TimedModel(), beam, overlap, None, True
    tr.set_context("배성재의 텐", "기역, 니은")
    return tr


def chunk(*spans, seconds=10):
    a = np.zeros(seconds * 16_000, dtype=np.float32)
    for s, e in spans:
        a[int(s * 16_000):int(e * 16_000)] = 0.3
    return a


def test_feed_carries_words_started_near_the_end():
    tr = make_tr()
    # 첫 조각: 1~4초 말 + 8.5초에 시작해 끝까지 이어진 말 → 8.5초부터는 다음 조각과 이어서 받아씀
    assert tr.feed(chunk((1, 4), (8.5, 10))) == "말1@1"
    assert abs(len(tr.carry) / 16_000 - 1.5) < 0.01
    # 둘째 조각: 앞 1.5초(넘긴 말) + 0~2초 이어지는 말 → 한 덩이로 확정
    text = tr.feed(chunk((0, 2)))
    assert text == "말1@0" and tr.model.lengths[-1] == int(11.5 * 16_000) and tr.carry is None
    kw = tr.model.kwargs[-1]
    assert kw["beam_size"] == 5 and kw["hotwords"] == "기역, 니은" and kw["initial_prompt"] == "라디오 배성재의 텐 방송."


def test_feed_without_overlap_and_one_shot_transcribe():
    tr = make_tr(overlap=0, beam=1)
    assert tr.feed(chunk((1, 4), (8.5, 10))) == "말1@1 말2@8" and tr.carry is None
    tr = make_tr()
    assert tr.transcribe(chunk((8.5, 10))) == "말1@8" and tr.carry is None   # 받아쓰기 테스트는 바로 확정


def test_transcriber_without_hotwords_puts_hints_in_prompt():
    tr = make_tr()
    tr._has_hotwords = False
    tr.transcribe(chunk((1, 3)))
    kw = tr.model.kwargs[-1]
    assert "hotwords" not in kw and kw["initial_prompt"] == "라디오 배성재의 텐 방송. 기역, 니은"


# ── 실행기 연결 ─────────────────────────────────────────────────────
def test_make_transcriber_uses_settings(conn, monkeypatch):
    made = {}

    class FakeWT:
        def __init__(self, model, program, device, **kw):
            made.update(model=model, program=program, device=device, **kw)

    monkeypatch.setattr(stt, "WhisperTranscriber", FakeWT)
    monkeypatch.setattr(gpu, "choose_device", lambda c, wanted: ("cpu", "그래픽카드 점검 실패 → CPU (x)"))
    db.set_setting(conn, "quizbot.whisper_beam", "1")
    tr = runner.make_transcriber(conn, "auto", "배성재의 텐")
    assert (made["device"], made["beam_size"], made["overlap_seconds"]) == ("cpu", 1, 3)
    assert "기역" in made["hints"] and tr.device_note.startswith("그래픽카드 점검 실패")


def test_runner_feeds_and_updates_context(conn):
    class FeedTranscriber:
        label, load_seconds, device_note = "CPU · small · 후보 5", 0.1, "설정: CPU"

        def __init__(self):
            self.contexts, self.fed = [], 0

        def set_context(self, program, hints):
            self.contexts.append((program, hints))

        def feed(self, audio):
            self.fed += 1
            return "음악이 흐릅니다"

        def transcribe(self, audio):
            raise AssertionError("청취 중에는 겹쳐 듣기(feed)를 쓴다")

    start_live(conn)
    db.set_setting(conn, "quizbot.whisper_model", "small")      # 예전 기본값 → 처음 한 번 auto 로
    tr = FeedTranscriber()
    r, _ = live_runner(conn, {}, ScriptAnswerer([]), FakeGorilla(), n=3)
    r.deps.transcriber_factory = lambda m, p: tr
    r.run_live()
    assert tr.fed == 3 and len(tr.contexts) == 1 and "기역" in tr.contexts[0][1]
    assert config.get(conn, "quizbot.whisper_model") == "auto"
    msgs = [e["message"] for e in conn.execute("SELECT message FROM events")]
    assert any("음성 인식 준비 완료 (CPU · small · 후보 5" in m and "설정: CPU" in m for m in msgs)
