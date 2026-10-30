"""공용 받아쓰기 (0.93): 받아쓰기 모델을 한 번만 올리고 모든 청취기(주 채널·보조 칸)가 함께 쓴다.

예전에는 청취기마다 모델과 그래픽카드 부품(CUDA)을 따로 올려 한 칸에 메모리 2~3GB 를 썼다. 사용자 PC 에서 8칸 +
LDPlayer 6창이면 메모리가 모자라 'python.exe 응용 프로그램 오류 0xc000012d'(가상 메모리 부족)로 청취기가 켜지지 못했다.
이제 동시 청취(3칸 이상)에서는 이 프로그램 하나가 모델을 들고(여러 칸을 동시에 받아쓰도록 num_workers),
청취기는 소리 조각만 보내고 글자를 받는다. 이 PC 안(127.0.0.1)에서만 연다.

    python -m radio_helper.quizbot stt-server --model large-v3-turbo --device cuda --compute float16 --workers 4

- 누가 띄우나: 처음 받아쓰기를 시작하는 청취기가 띄우고(잠금 파일로 하나만), 나머지는 뜰 때까지 기다린다.
- 상태 파일 data/stt_server.json 에 번호(port)·프로그램 번호(pid)를 적는다. 15분 동안 요청이 없으면 스스로 꺼진다.
- 못 뜨면 청취기는 예전처럼 자기 모델을 올린다 (듣기는 멈추지 않음).
"""

from __future__ import annotations

import json
import os
import struct
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import numpy as np

STATE_FILE = "stt_server.json"
LOCK_FILE = "stt_server.lock"
IDLE_EXIT = 900           # 이만큼 요청이 없으면 꺼짐
WAIT_READY = 240          # 모델을 올리는 동안 기다리는 시간 (처음 내려받기 포함)
REQUEST_TIMEOUT = 120


def pack(audio: np.ndarray, params: dict) -> bytes:
    head = json.dumps(params, ensure_ascii=False).encode("utf-8")
    return struct.pack(">I", len(head)) + head + np.asarray(audio, dtype=np.float32).tobytes()


def unpack(body: bytes) -> tuple[np.ndarray, dict]:
    n = struct.unpack(">I", body[:4])[0]
    params = json.loads(body[4:4 + n].decode("utf-8"))
    return np.frombuffer(body[4 + n:], dtype=np.float32), params


def _state_path(folder: Path) -> Path:
    return Path(folder) / STATE_FILE


def _pid_alive(pid: int) -> bool:
    from ..autofill import pid_alive

    return pid_alive(pid)


def running(folder: Path, opener=urllib.request.urlopen) -> dict | None:
    """떠 있는 공용 받아쓰기 {port, pid, model, …} (응답할 때만)."""
    try:
        st = json.loads(_state_path(folder).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not st.get("ready") or not st.get("port") or not _pid_alive(int(st.get("pid") or 0)):
        return None
    try:
        with opener(f"http://127.0.0.1:{st['port']}/health", timeout=3) as r:
            r.read()
    except Exception:
        return None
    st["url"] = f"http://127.0.0.1:{st['port']}"
    return st


def ensure(folder: Path, launch, args: list[str], wait: float = WAIT_READY, sleep=time.sleep,
           check=running) -> dict:
    """공용 받아쓰기가 떠 있게 한다 (없으면 하나만 띄우고 뜰 때까지 기다림). 못 뜨면 RuntimeError."""
    st = check(folder)
    if st:
        return st
    lock = Path(folder) / LOCK_FILE
    try:
        if lock.exists() and (time.time() - lock.stat().st_mtime > wait
                              or not _pid_alive(int(lock.read_text(encoding="ascii") or 0))):
            lock.unlink(missing_ok=True)          # 띄우다 멈춘 잠금
    except (OSError, ValueError):
        lock.unlink(missing_ok=True)
    mine = False
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        mine = True
    except FileExistsError:
        pass                                      # 다른 청취기가 띄우는 중 → 기다림
    try:
        if mine:
            launch(["stt-server", *args])
        deadline = time.time() + wait
        while time.time() < deadline:
            st = check(folder)
            if st:
                return st
            sleep(2)
    finally:
        if mine:
            lock.unlink(missing_ok=True)
    raise RuntimeError(f"공용 받아쓰기가 {wait:.0f}초 안에 뜨지 않음")


class RemoteModel:
    """faster-whisper WhisperModel 처럼 transcribe(소리, …) → (구간들, None). 실제로는 공용 받아쓰기에 보낸다."""

    def __init__(self, url: str, opener=urllib.request.urlopen, timeout: float = REQUEST_TIMEOUT):
        self.url, self.opener, self.timeout = url, opener, timeout

    def transcribe(self, audio, language=None, beam_size=5, vad_filter=True, condition_on_previous_text=False,
                   initial_prompt=None, hotwords=None):
        params = {"language": language, "beam_size": beam_size, "vad_filter": vad_filter,
                  "condition_on_previous_text": condition_on_previous_text, "initial_prompt": initial_prompt,
                  "hotwords": hotwords}
        req = urllib.request.Request(self.url + "/transcribe", data=pack(audio, {k: v for k, v in params.items()
                                                                                 if v is not None}),
                                     headers={"Content-Type": "application/octet-stream"}, method="POST")
        with self.opener(req, timeout=self.timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return [SimpleNamespace(**seg) for seg in data["segments"]], None


def make_handler(model, state: dict):
    lock_free = hasattr(model, "transcribe")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):      # 요청마다 검은 창에 찍지 않음
            pass

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"ok": True, "model": state.get("model"), "served": state["served"]})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/transcribe" or not lock_free:
                return self._send(404, {"error": "not found"})
            state["last"] = time.time()
            try:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                audio, params = unpack(body)
                segments, _info = model.transcribe(audio, **params)
                out = [{"start": float(s.start), "end": float(s.end), "text": s.text} for s in segments]
            except Exception as e:
                return self._send(500, {"error": f"{type(e).__name__}: {str(e)[:200]}"})
            state["served"] += 1
            state["last"] = time.time()
            return self._send(200, {"segments": out})
    return Handler


def serve(folder: Path, model, info: dict, port: int = 0, idle: float = IDLE_EXIT, say=print) -> int:
    """모델로 받아쓰기 서버를 연다 (요청이 idle 초 동안 없으면 꺼짐). 연 번호를 상태 파일에 적는다."""
    state = {"served": 0, "last": time.time(), **info}
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(model, state))
    srv.daemon_threads = True
    st = {"port": srv.server_address[1], "pid": os.getpid(), "ready": True, **info, "started": time.time()}
    _state_path(folder).write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    say(f"공용 받아쓰기 준비됨 — 127.0.0.1:{st['port']} · {info.get('model')} · {info.get('device')} "
        f"· 동시 {info.get('workers')}칸")

    def watch():
        while True:
            time.sleep(min(30, idle))
            if time.time() - state["last"] > idle:
                say(f"{idle / 60:.0f}분 동안 받아쓰기 요청이 없어 끕니다 (받아쓴 조각 {state['served']}개)")
                srv.shutdown()
                return
    threading.Thread(target=watch, daemon=True).start()
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        srv.server_close()
        try:
            if json.loads(_state_path(folder).read_text(encoding="utf-8")).get("pid") == os.getpid():
                _state_path(folder).unlink()
        except (OSError, ValueError):
            pass
    return state["served"]


def load_model(model_size: str, device: str, compute: str, workers: int):
    from .stt import add_nvidia_dll_dirs, resolve_model

    model_size = resolve_model(model_size, device)
    if device == "cuda":
        add_nvidia_dll_dirs()
    from faster_whisper import WhisperModel

    return WhisperModel(model_size, device=device, compute_type=compute, num_workers=max(1, workers)), model_size
