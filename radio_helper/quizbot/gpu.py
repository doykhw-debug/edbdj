"""음성 인식 장치 고르기: 그래픽카드를 별도 프로세스로 한 번 점검하고, 통과했을 때만 그래픽카드를 쓴다.

그래픽카드 라이브러리(CUDA·cuDNN)가 맞지 않으면 오류 메시지 없이 프로세스가 통째로 꺼지기 때문에
실행기 안에서 바로 열지 않고 'python -m radio_helper.quizbot gpu-check' 로 따로 시험한다.
점검 결과는 저장해 두었다가, 라이브러리 판이 바뀌었거나 실패한 지 하루가 지나면 다시 점검한다.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from typing import Callable

from .. import db

CHECK_KEY = "stt.gpu_check"
RECHECK_AFTER_FAIL = timedelta(days=1)
PROBE_TIMEOUT = 600          # 처음엔 점검용 작은 모델(약 75MB)을 내려받는다
LIB_PACKAGES = ("ctranslate2", "nvidia-cublas-cu12", "nvidia-cudnn-cu12")


def libs_signature() -> str:
    """그래픽카드 점검 결과에 영향을 주는 라이브러리 판."""
    from importlib import metadata

    parts = []
    for name in LIB_PACKAGES:
        try:
            parts.append(f"{name}={metadata.version(name)}")
        except metadata.PackageNotFoundError:
            parts.append(f"{name}=-")
    return ";".join(parts)


def saved_check(conn: sqlite3.Connection) -> dict | None:
    try:
        return json.loads(db.get_setting(conn, CHECK_KEY) or "null")
    except ValueError:
        return None


def needs_check(saved: dict | None, signature: str, now: datetime) -> bool:
    if not saved or saved.get("sig") != signature:
        return True
    if saved.get("ok"):
        return False
    try:
        return now - datetime.strptime(saved.get("at", ""), "%Y-%m-%d %H:%M:%S") >= RECHECK_AFTER_FAIL
    except ValueError:
        return True


def probe(timeout: int = PROBE_TIMEOUT) -> tuple[bool, str]:
    """별도 프로세스로 그래픽카드 받아쓰기를 시험한다. (통과, 설명)"""
    try:
        done = subprocess.run([sys.executable, "-m", "radio_helper.quizbot", "gpu-check"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"{timeout}초 안에 끝나지 않음"
    except OSError as e:
        return False, f"점검 프로그램을 실행하지 못함: {e}"
    out = (done.stdout or "") + (done.stderr or "")
    if done.returncode == 0 and "GPU_OK" in out:
        return True, "그래픽카드 받아쓰기 시험 통과"
    if "NO_CUDA_DEVICE" in out:
        return False, "NVIDIA 그래픽카드를 찾지 못함"
    last = next((line.strip() for line in reversed(out.splitlines()) if line.strip()), "")
    return False, f"종료 코드 {done.returncode}" + (f" · {last[:160]}" if last else "")


def choose_device(conn: sqlite3.Connection, wanted: str, now: datetime | None = None,
                  run_probe: Callable[[], tuple[bool, str]] | None = None) -> tuple[str, str]:
    """설정값(auto/cpu/cuda) → 실제로 쓸 장치와 이유. auto 는 점검을 통과해야 그래픽카드."""
    if wanted == "cpu":
        return "cpu", "설정: CPU"
    now = now or datetime.now()
    signature = libs_signature()
    saved = saved_check(conn)
    if needs_check(saved, signature, now):
        ok, detail = (run_probe or probe)()
        saved = {"ok": ok, "at": now.strftime("%Y-%m-%d %H:%M:%S"), "sig": signature, "detail": detail}
        db.set_setting(conn, CHECK_KEY, json.dumps(saved, ensure_ascii=False))
        db.log(conn, "quizbot", f"그래픽카드 점검: {'통과' if ok else '실패 → CPU'} ({detail})")
    if saved.get("ok"):
        return "cuda", "그래픽카드 점검 통과"
    return "cpu", f"그래픽카드 점검 실패 → CPU ({saved.get('detail', '')})"
