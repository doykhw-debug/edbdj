"""자동 업데이트: GitHub(공개 저장소)에 올라온 새 버전을 받아 프로그램 폴더에 덮어쓴다. 데이터 폴더는 건드리지 않는다.

- VERSION 파일의 번호가 올라간 것만 새 버전으로 본다.
- 받은 zip 에서 프로그램 파일만 덮어쓴다: .venv·data·.git 은 건드리지 않는다. 지금 실행 중인 start_windows.bat 은
  바로 바꾸면 위험해서 start_windows.new.bat 으로 두고, 다시 켤 때 start_windows.bat 이 스스로 바꾼다.
- 덮어쓰기 전에 바뀌는 파일을 데이터 폴더 updates/backup-<예전 버전> 에 모아 두고 updates/last.json 에 적는다.
  새 버전이 켜진 뒤 2분 넘게 잘 돌면 '확인됨'으로 표시한다. 새 버전이 켜지자마자 계속 꺼지면 start_windows.bat 이
  rollback_update.py 로 예전 파일을 되돌리고, 그 버전은 다시 받지 않는다.
- 설치가 끝나면 관리 화면을 종료 코드 3 으로 끝내고, start_windows.bat 이 패키지를 확인한 뒤 다시 켠다 (청취는 이어서).
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from . import __version__, db

REPO = "doykhw-debug/edbdj"
BRANCH = "claude/funny-knuth-yzaoi1"
VERSION_URL = "https://raw.githubusercontent.com/{repo}/{branch}/VERSION"
ZIP_URL = "https://codeload.github.com/{repo}/zip/refs/heads/{branch}"
MAX_ZIP_BYTES = 60 * 1024 * 1024
SKIP_TOP = {".venv", "venv", "data", ".git", "__pycache__", ".pytest_cache"}
BAT = "start_windows.bat"
NEW_BAT = "start_windows.new.bat"
EXIT_UPDATE = 3
RESTART_FLAG = "update_restart.flag"   # start_windows.bat 이 이 파일이 있을 때만 코드 3 을 '업데이트 후 다시 켜기'로 본다
CHECK_EVERY_HOURS = 6
CONFIRM_AFTER_SECONDS = 120


class UpdateError(RuntimeError):
    pass


def parse_version(text: str) -> tuple[int, ...]:
    parts = []
    for p in (text or "").strip().lstrip("vV").split("."):
        if not p.isdigit():
            break
        parts.append(int(p))
    return tuple(parts) or (0,)


def newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def source(conn: sqlite3.Connection) -> tuple[str, str]:
    return (db.get_setting(conn, "update.repo", REPO) or REPO, db.get_setting(conn, "update.branch", BRANCH) or BRANCH)


def updates_dir() -> Path:
    path = db.data_dir() / "updates"
    path.mkdir(parents=True, exist_ok=True)
    return path


def last_record() -> dict:
    try:
        return json.loads((updates_dir() / "last.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def bad_versions() -> set[str]:
    """되돌린 적이 있는 버전 (다시 받지 않음)."""
    return set(last_record().get("bad", []))


def fetch_latest(conn: sqlite3.Connection, opener=None) -> str:
    """GitHub 의 VERSION 을 읽어 저장한다. 최신 버전 번호."""
    repo, branch = source(conn)
    opener = opener or urllib.request.build_opener()
    db.set_setting(conn, "update.checked_at", db.now())
    try:
        with opener.open(VERSION_URL.format(repo=repo, branch=branch), timeout=20) as r:
            latest = r.read(100).decode("utf-8").strip()
    except Exception as e:   # 인터넷이 잠깐 끊겨도 다음에 다시 본다
        db.set_setting(conn, "update.error", f"{type(e).__name__}: {str(e)[:120]}")
        raise UpdateError(f"새 버전 확인 실패: {type(e).__name__}") from e
    if not re.fullmatch(r"\d+(\.\d+)*", latest):
        db.set_setting(conn, "update.error", "VERSION 내용을 읽지 못함")
        raise UpdateError("새 버전 번호를 읽지 못했습니다.")
    db.set_setting(conn, "update.latest", latest)
    db.set_setting(conn, "update.error", "")
    return latest


def download(conn: sqlite3.Connection, opener=None) -> bytes:
    repo, branch = source(conn)
    opener = opener or urllib.request.build_opener()
    with opener.open(ZIP_URL.format(repo=repo, branch=branch), timeout=120) as r:
        data = r.read(MAX_ZIP_BYTES + 1)
    if len(data) > MAX_ZIP_BYTES:
        raise UpdateError("받은 파일이 너무 큽니다.")
    return data


def _members(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    """zip 안의 파일 → 프로그램 폴더 기준 경로. GitHub zip 은 맨 위에 '저장소-가지' 폴더가 하나 있다."""
    names = [i for i in zf.infolist() if not i.is_dir()]
    tops = {PurePosixPath(i.filename).parts[0] for i in names if PurePosixPath(i.filename).parts}
    strip = len(tops) == 1 and all(len(PurePosixPath(i.filename).parts) > 1 for i in names)
    out = {}
    for info in names:
        parts = PurePosixPath(info.filename).parts[1 if strip else 0:]
        if not parts or info.filename.startswith("/") or ".." in parts or ":" in parts[0]:
            raise UpdateError(f"잘못된 경로가 들어 있습니다: {info.filename[:80]}")
        if parts[0] in SKIP_TOP or "__pycache__" in parts:
            continue
        out["/".join(parts)] = info
    return out


def install(data: bytes, root: Path, expect: str | None = None) -> dict:
    """zip 을 프로그램 폴더에 덮어쓴다. 바뀌는 파일은 먼저 백업. {version, written, new_bat, backup}"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise UpdateError("받은 파일이 zip 이 아닙니다.") from e
    files = _members(zf)
    for need in ("VERSION", "radio_helper/__init__.py", "radio_helper/app.py", "radio_helper/__main__.py"):
        if need not in files:
            raise UpdateError(f"업데이트 파일에 {need} 가 없습니다.")
    version = zf.read(files["VERSION"]).decode("utf-8").strip()
    if expect and version != expect:
        raise UpdateError(f"받은 버전({version})이 확인한 버전({expect})과 다릅니다. 잠시 뒤 다시 하세요.")
    if version in bad_versions():
        raise UpdateError(f"{version} 은(는) 켜자마자 꺼져서 되돌린 버전이라 설치하지 않습니다.")
    current = (root / "VERSION").read_text(encoding="utf-8").strip() if (root / "VERSION").exists() else __version__
    backup = updates_dir() / f"backup-{current}-{datetime.now():%Y%m%d%H%M%S}"
    written, created, new_bat = [], [], False
    for rel, info in sorted(files.items()):
        content = zf.read(info)
        target = root / rel
        if rel == BAT:   # 실행 중인 배치 파일은 다시 켤 때 스스로 바꾼다
            if not target.exists() or target.read_bytes() != content:
                (root / NEW_BAT).write_bytes(content)
                new_bat = True
            continue
        if target.exists():
            if target.read_bytes() == content:
                continue
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup / rel)
        else:
            created.append(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".updating")
        tmp.write_bytes(content)
        os.replace(tmp, target)
        written.append(rel)
    record = {"from": current, "to": version, "at": db.now(), "backup": str(backup), "changed": written,
              "created": created, "confirmed": False, "bad": sorted(bad_versions())}
    (updates_dir() / "last.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"version": version, "written": len(written), "new_bat": new_bat, "backup": str(backup)}


def confirm_running(version: str) -> None:
    """새 버전으로 켜져 잘 돌고 있음 → 되돌리지 않는다."""
    rec = last_record()
    if rec and rec.get("to") == version and not rec.get("confirmed"):
        rec["confirmed"] = True
        (updates_dir() / "last.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")


def due_check(conn: sqlite3.Connection, now: datetime) -> bool:
    raw = db.get_setting(conn, "update.checked_at") or ""
    try:
        return (now - datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")).total_seconds() >= CHECK_EVERY_HOURS * 3600
    except ValueError:
        return True


def should_auto_install(conn: sqlite3.Connection, now: datetime, current: str = __version__) -> bool:
    """자동 업데이트: 켜져 있고, 새 버전이 있고, 정한 시각(기본 새벽 4시)이고, 오늘 아직 안 했을 때."""
    if (db.get_setting(conn, "update.auto", "1") or "1") != "1":
        return False
    latest = db.get_setting(conn, "update.latest") or ""
    if not latest or not newer(latest, current) or latest in bad_versions():
        return False
    try:
        hour = int(db.get_setting(conn, "update.hour", "4") or 4)
    except ValueError:
        hour = 4
    return now.hour == hour and (db.get_setting(conn, "update.tried_on") or "") != now.strftime("%Y-%m-%d")


def stop_helpers(conn: sqlite3.Connection, wait_s: float = 20) -> None:
    """다시 켜기 전에 듣기 실행기를 멈춘다 (다시 켜지면 새 버전으로 이어서 듣는다)."""
    from .quizbot import config as qconfig

    db.set_setting(conn, "quizbot.stop", "1")
    deadline = time.time() + wait_s
    while time.time() < deadline and qconfig.runner_alive(conn):
        time.sleep(1)


def restart_for_update() -> None:
    """관리 화면을 업데이트 종료 코드로 끝낸다 → start_windows.bat 이 다시 켠다."""
    os._exit(EXIT_UPDATE)


def update_now(conn: sqlite3.Connection, root: Path, opener=None, restart=restart_for_update) -> dict:
    """확인 → 받기 → 설치 → (실행기 멈춤) → 다시 켜기. 실패하면 UpdateError."""
    latest = fetch_latest(conn, opener)
    if not newer(latest, __version__):
        raise UpdateError(f"이미 최신 버전입니다 ({__version__}).")
    db.set_setting(conn, "update.tried_on", datetime.now().strftime("%Y-%m-%d"))
    report = install(download(conn, opener), root, expect=latest)
    db.log(conn, "update", f"업데이트 설치: {__version__} → {report['version']} (파일 {report['written']}개"
                           + (", 시작 파일은 다시 켤 때 바꿈" if report["new_bat"] else "") + ") — 다시 켭니다")
    # 종료 코드 3 은 윈도우의 '경로를 찾을 수 없음'과 같아서, 업데이트로 끝낸다는 표시 파일을 함께 둔다
    (root / RESTART_FLAG).write_text(report["version"], encoding="utf-8")
    stop_helpers(conn)
    threading.Timer(2.0, restart).start()   # 응답 화면을 보낸 뒤 끝낸다
    return report
