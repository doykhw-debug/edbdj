"""듣는 동안 윈도우가 화면을 끄거나 잠들지 않게 하고, 마지막 입력 시각·그래픽카드 드라이버 재시작 기록을 읽는다 (0.61).

라디오 앱들이 모니터 스피커(그래픽카드 HDMI·DP 소리, 예: 'LG ULTRAFINE (NVIDIA High Definition Audio)')로 나가면
윈도우가 화면을 끌 때 모니터가 대기 상태로 들어가며 모든 라디오 소리가 함께 끊긴다. 동영상 재생기처럼 듣는 동안만
'화면 필요'를 윈도우에 알린다. 윈도우가 아니면 아무것도 하지 않는다.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from contextlib import contextmanager

ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000001, 0x00000002
TDR_EVENT = 4101          # 윈도우 시스템 기록 'Display' 4101: 그래픽카드 드라이버가 응답하지 않아 다시 시작됨
CREATE_NO_WINDOW = 0x08000000


def _kernel32():
    if sys.platform != "win32":
        return None
    k = ctypes.WinDLL("kernel32")
    k.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
    k.SetThreadExecutionState.restype = ctypes.c_uint32
    return k


@contextmanager
def keep_awake(display: bool = True, kernel32=None):
    """with 안에서는 절전(과 화면 꺼짐)을 막는다. 이 스레드가 부른 동안만이고, 나가면(프로그램이 꺼져도) 원래대로.
    → 걸었는지"""
    k = kernel32 if kernel32 is not None else _kernel32()
    if k is None:
        yield False
        return
    ok = bool(k.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | (ES_DISPLAY_REQUIRED if display else 0)))
    try:
        yield ok
    finally:
        k.SetThreadExecutionState(ES_CONTINUOUS)


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint32)]


def idle_seconds(user32=None, tick=None) -> float | None:
    """마지막 키보드·마우스 입력(도우미의 클릭 포함)에서 몇 초 지났는지. 못 읽으면 None."""
    if user32 is None:
        if sys.platform != "win32":
            return None
        user32, k = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
        k.GetTickCount.restype = ctypes.c_uint32
        tick = k.GetTickCount
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    return ((int(tick()) - info.dwTime) & 0xFFFFFFFF) / 1000.0


def gpu_resets(minutes: int = 10, run=subprocess.run) -> int | None:
    """최근 minutes 분 안에 그래픽카드 드라이버가 다시 시작된 횟수 (윈도우 시스템 기록). 못 읽으면 None."""
    if run is subprocess.run and sys.platform != "win32":
        return None
    query = (f"*[System[Provider[@Name='Display'] and (EventID={TDR_EVENT}) and "
             f"TimeCreated[timediff(@SystemTime) <= {minutes * 60000}]]]")
    try:
        out = run(["wevtutil", "qe", "System", f"/q:{query}", "/c:20", "/rd:true", "/f:xml"], capture_output=True,
                  text=True, encoding="utf-8", errors="replace", timeout=8,
                  creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.count("<Event ")
