"""0.92: 3일 지난 실행 기록·증거 사진 저절로 지우기 (C 드라이브 부족)."""

import os
import time
from datetime import datetime, timedelta

from radio_helper import cleanup, db, evidence


def age(path, days):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def test_old_logs_and_photos_are_removed(conn, data_dir):
    logs = data_dir / "logs"
    logs.mkdir(exist_ok=True)
    old, new = logs / "quizbot_20261020_0101_ab12.log", logs / "quizbot_20261030_0101_cd34.log"
    old.write_text("오래된 기록", encoding="utf-8")
    new.write_text("새 기록", encoding="utf-8")
    age(old, 5)
    (logs / "server.log").write_bytes(b"x" * (cleanup.APPEND_MAX + 10))
    age(logs / "server.log", 9)                                                     # 덧붙이는 기록은 지우지 않고 줄임
    now = datetime.now()
    evidence.save(conn, "quizzes", 1, "mini", "옛 답", "entered", [("보낸 뒤", b"\x89PNG old")], now=now - timedelta(days=4))
    evidence.save(conn, "quizzes", 2, "mini", "새 답", "entered", [("보낸 뒤", b"\x89PNG new")], now=now - timedelta(hours=5))
    old_files = [evidence.root() / r["file"] for r in conn.execute("SELECT file FROM evidence WHERE item_id = 1")]
    for f in old_files:
        age(f, 4)
    out = cleanup.run(conn, now)
    assert not old.exists() and new.exists() and out["logs"] == 1
    assert (logs / "server.log").stat().st_size == cleanup.APPEND_KEEP
    assert [r["item_id"] for r in conn.execute("SELECT item_id FROM evidence")] == [2] and out["photos"] == 1
    assert not any(f.exists() for f in old_files)
    assert "3일 지난 기록 정리" in conn.execute("SELECT message FROM events ORDER BY id DESC").fetchone()[0]


def test_runs_every_six_hours_and_days_setting(conn, data_dir):
    assert cleanup.tick(conn, now=1_000_000.0) is not None
    assert cleanup.tick(conn, now=1_000_000.0 + 3600) is None
    assert cleanup.tick(conn, now=1_000_000.0 + cleanup.EVERY_SECONDS + 1) is not None
    assert cleanup.keep_days(conn) == 3
    db.set_setting(conn, "cleanup.keep_days", "7")
    assert cleanup.keep_days(conn) == 7
