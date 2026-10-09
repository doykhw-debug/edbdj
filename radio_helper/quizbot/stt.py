"""음성 인식 (faster-whisper, 이 PC에서 무료로 실행).

처음 실행할 때 모델 파일을 내려받는다 (small 약 0.5GB, large-v3-turbo 약 1.6GB, large-v3 약 3GB).
이후에는 인터넷 없이 동작한다.

- 장치 auto: 그래픽카드 점검(gpu.py, 별도 프로세스)에 통과했을 때만 그래픽카드(cuda)로 돌린다.
  CUDA·cuDNN 라이브러리가 맞지 않으면 실행기가 말없이 꺼질 수 있어서, 점검 없이 cuda 로 바로 열지 않는다.
- 모델 auto: 그래픽카드면 large-v3, CPU면 large-v3-turbo (논리 코어 16개 미만이면 small).
- 겹쳐 듣기: 조각 끝 몇 초 안에서 시작한 말은 확정하지 않고 다음 조각과 이어서 다시 받아쓴다.
- 단어 힌트: 방송 낱말·초성 자음 이름·진행자·코너 이름을 모델에 미리 알려 준다.
"""

from __future__ import annotations

import importlib
import inspect
import os
import sys
import time
from pathlib import Path

import numpy as np

RATE = 16_000
MODELS = ("auto", "large-v3", "large-v3-turbo", "medium", "small", "base", "tiny")
DEVICES = ("auto", "cpu", "cuda")
TURBO_MIN_CORES = 16    # CPU로 large-v3-turbo 를 돌릴 최소 논리 코어 수 (적으면 small)

BROADCAST_WORDS = ("퀴즈, 정답, 오답, 힌트, 문제, 초성, 고릴라, 공감로그, 문자, 응모, 선물, 기프티콘, 경품, 당첨, "
                   "사연, 신청곡, 게시판, 파워FM, 러브FM, FM4U, 쿨FM")
CHOSEONG_NAMES = "기역, 니은, 디귿, 리을, 미음, 비읍, 시옷, 이응, 지읒, 치읓, 키읔, 티읕, 피읖, 히읗, 쌍기역, 쌍시옷"
MAX_HINT_CHARS = 300    # Whisper 힌트는 앞쪽 일부만 쓰이므로 길게 넣지 않는다


def resolve_model(model: str, device: str, cpu_count: int | None = None) -> str:
    """모델 auto → 장치와 CPU 코어 수에 맞는 모델."""
    if model and model != "auto":
        return model
    if device == "cuda":
        return "large-v3"
    cores = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    return "large-v3-turbo" if cores >= TURBO_MIN_CORES else "small"


def build_hints(names: list[str] | tuple[str, ...] = (), user_hints: str = "") -> str:
    """사용자 힌트 → 진행자·코너 이름 → 방송 낱말 → 초성 자음 이름 순서로, 겹치지 않게 이어 붙인다."""
    words: list[str] = []
    for chunk in (user_hints, ", ".join(n for n in names if n), BROADCAST_WORDS, CHOSEONG_NAMES):
        for w in (chunk or "").replace("\n", ",").split(","):
            w = " ".join(w.split())
            if w and w not in words:
                words.append(w)
    out = ""
    for w in words:
        if len(out) + len(w) + 2 > MAX_HINT_CHARS:
            break
        out = f"{out}, {w}" if out else w
    return out


def add_nvidia_dll_dirs() -> None:
    """pip 로 설치한 CUDA·cuDNN 라이브러리(nvidia-cublas-cu12, nvidia-cudnn-cu12)를 윈도우가 찾게 한다."""
    if sys.platform != "win32":
        return
    try:
        nvidia = importlib.import_module("nvidia")  # nvidia-cublas-cu12 등이 함께 쓰는 이름 공간
    except ImportError:
        return
    for base in getattr(nvidia, "__path__", []):
        for bin_dir in Path(base).glob("*/bin"):
            try:
                os.add_dll_directory(str(bin_dir))
            except (OSError, AttributeError):
                pass
            os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


class WhisperTranscriber:
    def __init__(self, model_size: str = "auto", program_title: str = "", device: str = "cpu",
                 beam_size: int = 5, overlap_seconds: float = 3.0, hints: str = ""):
        device = device if device in ("cpu", "cuda") else "cpu"   # auto 는 gpu.choose_device 가 미리 정한다
        model_size = resolve_model(model_size, device)
        if device == "cuda":
            add_nvidia_dll_dirs()
        from faster_whisper import WhisperModel

        self.device, self.model_size = device, model_size
        started = time.monotonic()
        self.model = WhisperModel(model_size, device=device, compute_type="float16" if device == "cuda" else "int8")
        self.load_seconds = time.monotonic() - started
        self.beam_size = max(1, int(beam_size or 1))
        self.overlap = max(0.0, float(overlap_seconds or 0))
        self.carry: np.ndarray | None = None
        self._has_hotwords = "hotwords" in inspect.signature(self.model.transcribe).parameters
        self.label = f"{'그래픽카드' if device == 'cuda' else 'CPU'} · {model_size} · 후보 {self.beam_size}"
        self.set_context(program_title, hints)

    def set_context(self, program_title: str = "", hints: str = "") -> None:
        """듣는 프로그램이 바뀌면 프로그램 이름과 단어 힌트를 바꾼다."""
        self.prompt = f"라디오 {program_title} 방송.".replace("  ", " ").strip()
        self.hints = hints or build_hints()

    def _segments(self, audio: np.ndarray) -> list:
        from .audio import boost_quiet

        kwargs = {"language": "ko", "beam_size": self.beam_size, "vad_filter": True,
                  "condition_on_previous_text": False}
        if self._has_hotwords:
            kwargs.update(initial_prompt=self.prompt, hotwords=self.hints)
        else:
            kwargs.update(initial_prompt=f"{self.prompt} {self.hints}")
        segments, _info = self.model.transcribe(boost_quiet(audio), **kwargs)
        return list(segments)

    def transcribe(self, audio: np.ndarray) -> str:
        """한 번에 받아쓰기 (받아쓰기 테스트용, 겹쳐 듣기 없음)."""
        if audio is None or len(audio) < RATE:  # 1초 미만
            return ""
        return _join(self._segments(audio))

    def feed(self, audio: np.ndarray) -> str:
        """청취용: 앞 조각에서 넘긴 끝부분을 붙여 받아쓰고, 이번 조각 끝 overlap 초 안에서 시작한 말은 다음으로 넘긴다."""
        if self.carry is not None and len(self.carry) and audio is not None and len(audio):
            audio = np.concatenate([self.carry, audio])
        self.carry = None
        if audio is None or len(audio) < RATE:
            return ""
        segments = self._segments(audio)
        keep, tail = split_at_overlap(segments, len(audio) / RATE, self.overlap)
        if tail:
            self.carry = audio[int(tail[0].start * RATE):]
        return _join(keep)


def split_at_overlap(segments: list, total_seconds: float, overlap: float) -> tuple[list, list]:
    """(확정할 말, 다음 조각과 이어서 다시 받아쓸 말). 끝 overlap 초 안에서 시작한 말부터 넘긴다."""
    if overlap <= 0 or total_seconds <= overlap:
        return list(segments), []
    cutoff = total_seconds - overlap
    for i, seg in enumerate(segments):
        if seg.start >= cutoff:
            return list(segments[:i]), list(segments[i:])
    return list(segments), []


def _join(segments) -> str:
    return " ".join(seg.text.strip() for seg in segments if seg.text.strip()).strip()
