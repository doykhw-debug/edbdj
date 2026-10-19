"""음성 인식 부품(faster-whisper)이 없을 때 스스로 고친다 (윈도우 PC).

- Python 이 맞는데(3.10~3.14) 부품만 없으면: 뒤에서 pip 로 설치한다 (관리 화면·청취는 그대로, 6시간에 한 번).
- Python 이 너무 새 버전이면(예: 3.15, 부품이 아직 없음): start_windows.bat 으로 켰을 때만, 하루 한 번
  rebuild_venv.flag 를 두고 업데이트와 같은 방법(종료 코드 3)으로 다시 켠다 → start_windows.bat 이
  find_python.py 로 Python 3.12 를 찾거나 설치해 .venv 를 다시 만들고, 듣던 방송은 이어서 듣는다 (5~10분).
- 듣기를 켜 두었을 때(청취 중·예약 자동 참여)만 저절로 고친다. 첫 화면의 '지금 고치기'로 바로 할 수도 있다.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import db, server

MODULE = "faster_whisper"
SUPPORTED = ((3, 10), (3, 14))
REBUILD_FLAG = "rebuild_venv.flag"
PIP_EVERY_HOURS = 6
ROOT = Path(__file__).resolve().parent.parent


def stt_installed(find=importlib.util.find_spec) -> bool:
    importlib.invalidate_caches()   # 방금 설치한 것도 보이게
    try:
        return find(MODULE) is not None
    except (ImportError, ValueError):
        return False


def python_ok(version=None) -> bool:
    v = tuple((version or sys.version_info)[:2])
    return SUPPORTED[0] <= v <= SUPPORTED[1]


def python_version() -> str:
    return "%d.%d.%d" % sys.version_info[:3]


def can_restart() -> bool:
    """start_windows.bat 으로 켰는가 (그래야 다시 켜면서 .venv 를 다시 만들 수 있다)."""
    return os.environ.get("RADIO_HELPER_LAUNCHER") == "bat"


def status(conn: sqlite3.Connection, find=importlib.util.find_spec) -> dict:
    installed = stt_installed(find)
    return {"missing": not installed, "python": python_version(), "python_ok": python_ok(),
            "can_restart": can_restart(), "rebuild_on": db.get_setting(conn, "repair.rebuild_on") or "",
            "pip_at": db.get_setting(conn, "repair.pip_at") or "", "server": server.is_server()}


def message(st: dict) -> str:
    """첫 화면에 보일 안내 (없으면 빈 글)."""
    if not st["missing"] or st["server"]:
        return ""
    if st["python_ok"]:
        return (f"음성 인식 부품(faster-whisper)이 설치되지 않았습니다 (Python {st['python']}). "
                "뒤에서 설치하는 중이거나 인터넷 문제로 실패했습니다 — '지금 고치기'로 다시 설치합니다.")
    return (f"음성 인식 부품(faster-whisper)은 Python {st['python']} 에서 아직 설치되지 않습니다 (너무 새 버전). "
            "'지금 고치기'를 누르면 Python 3.12 를 설치하고 프로그램을 다시 켭니다 (5~10분, 듣던 방송은 이어서).")


def start_pip(conn: sqlite3.Connection, root: Path = ROOT, popen=subprocess.Popen) -> str:
    """퀴즈 부품을 뒤에서 설치한다. 기록 파일 이름."""
    log_dir = db.data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    name = f"repair_{time.strftime('%Y%m%d_%H%M%S')}.log"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with open(log_dir / name, "w", encoding="utf-8") as out:
        popen([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r",
               str(root / "requirements-quiz.txt")], stdout=out, stderr=subprocess.STDOUT, cwd=str(root),
              creationflags=flags)
    db.set_setting(conn, "repair.pip_at", db.now())
    db.log(conn, "quizbot", f"음성 인식 부품이 없어 뒤에서 설치합니다 (기록: {name})")
    return name


def request_rebuild(conn: sqlite3.Connection, root: Path = ROOT, restart=None, now: datetime | None = None) -> None:
    """Python 3.12 로 .venv 를 다시 만들도록 표시하고 다시 켠다 (start_windows.bat 이 이어서 함)."""
    from . import updater

    (root / REBUILD_FLAG).write_text(python_version(), encoding="utf-8")
    (root / updater.RESTART_FLAG).write_text("repair", encoding="utf-8")
    db.set_setting(conn, "repair.rebuild_on", (now or datetime.now()).strftime("%Y-%m-%d"))
    db.set_setting(conn, "update.started_at", db.now())   # 다시 켜면 '이어서 듣기'로 안내
    db.log(conn, "quizbot", f"음성 인식 부품이 Python {python_version()} 에 없어 Python 3.12 로 다시 설치하고 다시 켭니다")
    updater.stop_helpers(conn)
    threading.Timer(2.0, restart or updater.restart_for_update).start()


def auto(conn: sqlite3.Connection, now: datetime | None = None, root: Path = ROOT, find=importlib.util.find_spec,
         restart=None, popen=subprocess.Popen) -> str | None:
    """관리 화면 서버가 30초마다 부른다. 한 일: 'pip' / 'rebuild' / None."""
    now = now or datetime.now()
    if server.is_server() or db.is_stopped(conn):
        return None
    listening = db.get_setting(conn, "live.active") == "1" or db.get_setting(conn, "quizbot.autostart") == "1"
    if not listening or stt_installed(find):
        return None
    if python_ok():
        last = db.get_setting(conn, "repair.pip_at") or ""
        try:
            recent = now - datetime.strptime(last, "%Y-%m-%d %H:%M:%S") < timedelta(hours=PIP_EVERY_HOURS)
        except ValueError:
            recent = False
        if recent:
            return None
        start_pip(conn, root, popen)
        return "pip"
    if can_restart() and (db.get_setting(conn, "repair.rebuild_on") or "") != now.strftime("%Y-%m-%d"):
        request_rebuild(conn, root, restart, now)
        return "rebuild"
    return None


def repair_now(conn: sqlite3.Connection, root: Path = ROOT, restart=None, popen=subprocess.Popen) -> str:
    """첫 화면 '지금 고치기'. 안내 글."""
    if python_ok():
        name = start_pip(conn, root, popen)
        return f"음성 인식 부품을 뒤에서 설치합니다 (몇 분, 기록: {name}). 설치되면 듣기가 저절로 이어집니다."
    if not can_restart():
        return ("이 창은 start_windows.bat 으로 켠 것이 아니라 다시 켤 수 없습니다. 검은 창을 닫고 start_windows.bat 을 "
                "더블클릭하면 Python 3.12 를 설치하고 고칩니다.")
    request_rebuild(conn, root, restart)
    return "Python 3.12 를 설치하고 프로그램을 다시 켭니다 (5~10분). 검은 창에 진행 상황이 나옵니다. 잠시 뒤 새로 고침하세요."
