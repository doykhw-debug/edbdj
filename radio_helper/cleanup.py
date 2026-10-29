"""오래된 기록·증거 사진 저절로 지우기 (0.92 사용자 요청: 3일 지난 것).

C 드라이브가 부족해져, 데이터 폴더에 쌓이기만 하던 것을 정리한다:
- logs/ 의 실행 기록 파일 (도구·청취기·보조 청취기·게시판·멈춤 기록 hang_*) — 마지막으로 쓴 지 3일 지난 것
- 계속 덧붙여 쓰는 server.log·server_crash.log 는 지우지 않고 너무 커지면 뒤쪽만 남긴다
- evidence/ 의 보낸 증거 사진과 그 기록 줄 — 찍은 지 3일 지난 것 (빈 달 폴더도)
관리 화면 서버의 감시(30초마다)가 부르고, 실제로는 6시간에 한 번만 한다. 지우는 데 실패한 파일(쓰는 중 등)은 다음에.
사연·퀴즈·보낸 기록(데이터베이스)과 위치 지정 사진(inspect/)은 지우지 않는다.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta

from . import db, evidence

KEEP_DAYS = 3
EVERY_SECONDS = 6 * 3600
LAST_KEY = "cleanup.last"
APPEND_LOGS = ("server.log", "server_crash.log")
APPEND_MAX = 5 * 1024 * 1024      # 덧붙이는 기록이 이보다 크면 뒤쪽 1MB 만 남긴다
APPEND_KEEP = 1024 * 1024


def keep_days(conn: sqlite3.Connection) -> int:
    try:
        return max(1, int(db.get_setting(conn, "cleanup.keep_days") or KEEP_DAYS))
    except ValueError:
        return KEEP_DAYS


def _trim(path) -> int:
    """덧붙이는 기록이 너무 크면 뒤쪽만 남긴다. 줄인 바이트 수."""
    try:
        size = path.stat().st_size
        if size <= APPEND_MAX:
            return 0
        with open(path, "rb") as f:
            f.seek(size - APPEND_KEEP)
            tail = f.read()
        path.write_bytes(tail)
        return size - len(tail)
    except OSError:
        return 0


def run(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """정리 한 번. {logs, photos, bytes}"""
    now = now or datetime.now()
    days = keep_days(conn)
    cutoff = now - timedelta(days=days)
    out = {"logs": 0, "photos": 0, "bytes": 0}
    log_dir = db.data_dir() / "logs"
    if log_dir.exists():
        for f in log_dir.iterdir():
            if not f.is_file():
                continue
            if f.name in APPEND_LOGS:
                out["bytes"] += _trim(f)
                continue
            try:
                if datetime.fromtimestamp(f.stat().st_mtime) >= cutoff:
                    continue
                size = f.stat().st_size
                f.unlink()
            except OSError:
                continue
            out["logs"] += 1
            out["bytes"] += size
    root = evidence.root()
    stamp = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    for row in conn.execute("SELECT id, file FROM evidence WHERE taken_at < ?", (stamp,)).fetchall():
        path = root / row["file"]
        try:
            if path.exists():
                out["bytes"] += path.stat().st_size
                path.unlink()
        except OSError:
            continue                  # 못 지운 사진은 기록도 남겨 둔다 (다음에 다시)
        conn.execute("DELETE FROM evidence WHERE id = ?", (row["id"],))
        out["photos"] += 1
    conn.commit()
    if root.exists():                 # 기록 없이 남은 오래된 사진 · 빈 달 폴더
        for f in root.rglob("*"):
            try:
                if f.is_file() and datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                    out["bytes"] += f.stat().st_size
                    f.unlink()
                    out["photos"] += 1
            except OSError:
                continue
        for d in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
            try:
                d.rmdir()             # 비어 있을 때만 지워짐
            except OSError:
                pass
    if out["logs"] or out["photos"] or out["bytes"]:
        db.log(conn, "cleanup", f"{days}일 지난 기록 정리: 실행 기록 {out['logs']}개 · 증거 사진 {out['photos']}장 "
                                f"({out['bytes'] / 1024 / 1024:.1f}MB 비움)")
    return out


def tick(conn: sqlite3.Connection, now: float | None = None) -> dict | None:
    """감시가 30초마다 부른다: 6시간에 한 번 정리 (프로그램을 켜면 바로 한 번)."""
    now = time.time() if now is None else now
    try:
        last = float(db.get_setting(conn, LAST_KEY) or 0)
    except ValueError:
        last = 0.0
    if 0 <= now - last < EVERY_SECONDS:
        return None
    db.set_setting(conn, LAST_KEY, str(now))
    return run(conn, datetime.fromtimestamp(now))
