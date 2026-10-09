"""PC에서 재생 중인 소리(고릴라 방송)를 녹음한다.

윈도우 WASAPI 루프백으로 '스피커로 나가는 소리'를 그대로 받는다. 마이크는 쓰지 않는다.
다른 프로그램 소리(알림음, 영상)도 함께 들어가므로 예약 시간에는 다른 소리를 꺼 두는 것이 좋다.
오디오는 저장하지 않고 음성 인식 후 버린다.
"""

from __future__ import annotations

import queue
import time

import numpy as np

TARGET_RATE = 16_000  # 음성 인식 입력


def to_mono_16k(pcm16: bytes, channels: int, rate: int) -> np.ndarray:
    """16비트 PCM 바이트 → float32 모노 16kHz."""
    samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        usable = len(samples) - len(samples) % channels
        samples = samples[:usable].reshape(-1, channels).mean(axis=1)
    if rate != TARGET_RATE and len(samples):
        n_out = int(round(len(samples) * TARGET_RATE / rate))
        x_old = np.linspace(0.0, 1.0, num=len(samples), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
        samples = np.interp(x_new, x_old, samples).astype(np.float32)
    return samples


QUIET_LEVEL = 25         # 소리 크기(0~100) 이 아래면 '작음'으로 알린다 (약 -45dB)
BOOST_TARGET = 0.5       # 받아쓰기 전에 키울 목표 크기 (최대치의 절반, -6dB)
BOOST_MAX_GAIN = 100.0   # 최대 100배(+40dB)까지만 키운다
BOOST_FLOOR = 0.001      # 이보다 작으면(-60dB) 잡음뿐이라 키우지 않는다


def boost_quiet(samples: np.ndarray) -> np.ndarray:
    """PC 볼륨이 작아 작게 녹음된 소리를 받아쓰기 전에 키운다. 이미 충분히 크면 그대로 둔다."""
    if samples is None or len(samples) == 0:
        return samples
    peak = float(np.percentile(np.abs(samples), 99.9))  # 툭 튀는 잡음 한두 개는 무시
    if peak < BOOST_FLOOR or peak >= BOOST_TARGET:
        return samples
    gain = min(BOOST_MAX_GAIN, BOOST_TARGET / peak)
    return np.clip(samples * gain, -1.0, 1.0).astype(np.float32)


def rms(samples: np.ndarray) -> float:
    if samples is None or len(samples) == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))


def level_of(pcm16: bytes) -> int:
    """16비트 PCM 조각의 소리 크기 0~100 (-60dB 이하는 0)."""
    from .live import level_percent

    if not pcm16:
        return 0
    samples = np.frombuffer(pcm16[: len(pcm16) - len(pcm16) % 2], dtype=np.int16).astype(np.float32) / 32768.0
    return level_percent(rms(samples))


class LoopbackRecorder:
    """기본 출력 장치의 루프백 녹음 (PyAudioWPatch, 윈도우 전용)."""

    def __init__(self, chunk_seconds: int = 15):
        self.chunk_seconds = chunk_seconds
        self._q: queue.Queue[bytes] = queue.Queue()
        self._pa = None
        self._stream = None
        self.device_name = ""
        self.channels = 2
        self.rate = 48_000
        self.on_level = None  # 녹음 중 약 1초마다 소리 크기(0~100)를 알려 받을 함수

    def __enter__(self):
        import pyaudiowpatch as pyaudio

        self._pa = pyaudio.PyAudio()
        wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        speakers = self._pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if not speakers.get("isLoopbackDevice"):
            for lb in self._pa.get_loopback_device_info_generator():
                if speakers["name"] in lb["name"]:
                    speakers = lb
                    break
            else:
                raise RuntimeError("기본 스피커의 루프백 장치를 찾지 못했습니다.")
        self.device_name = speakers["name"]
        self.channels = int(speakers["maxInputChannels"]) or 2
        self.rate = int(speakers["defaultSampleRate"])

        def callback(in_data, frame_count, time_info, status):
            self._q.put(in_data)
            return (None, pyaudio.paContinue)

        self._stream = self._pa.open(
            format=pyaudio.paInt16, channels=self.channels, rate=self.rate, input=True,
            input_device_index=speakers["index"], frames_per_buffer=1024, stream_callback=callback)
        self._stream.start_stream()
        return self

    def __exit__(self, *exc):
        try:
            if self._stream is not None:
                self._stream.stop_stream()
                self._stream.close()
        finally:
            if self._pa is not None:
                self._pa.terminate()

    def read_chunk(self) -> np.ndarray:
        """chunk_seconds 만큼 모아 돌려준다. 아무 소리도 재생되지 않으면 루프백은 데이터를 주지 않으므로
        시간이 지나면 모인 만큼(무음이면 빈 배열)을 돌려준다."""
        need = self.chunk_seconds * self.rate * self.channels * 2
        parts, got = [], 0
        deadline = time.monotonic() + self.chunk_seconds + 2
        last_report, recent = time.monotonic(), []
        while got < need:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                data = self._q.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                data = b""
            if data:
                parts.append(data)
                recent.append(data)
                got += len(data)
            if self.on_level is not None and time.monotonic() - last_report >= 1.0:
                last_report = time.monotonic()
                self.on_level(level_of(b"".join(recent)))
                recent = []
        return to_mono_16k(b"".join(parts), self.channels, self.rate)
