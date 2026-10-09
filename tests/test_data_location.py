"""업데이트 파일을 새 폴더에 풀어도 데이터가 남는다: 고정 위치 + 예전 폴더 데이터 자동으로 옮겨 오기."""

import os
import sqlite3
import time

from radio_helper import db


def make_db(path, experiences=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = db.connect(path)
    db.init_db(c)
    for i in range(experiences):
        c.execute("INSERT INTO experiences (story, created_at, updated_at) VALUES (?, ?, ?)", (f"경험 {i}", db.now(), db.now()))
    c.commit()
    c.close()
    return path


def test_home_data_dir_is_outside_program(monkeypatch, tmp_path):
    monkeypatch.setattr(db.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert db.home_data_dir() == tmp_path / "Local" / "RadioHelper"
    assert db.PROGRAM_ROOT not in db.home_data_dir().parents


def test_best_old_db_picks_latest_with_user_data(tmp_path):
    root = tmp_path / "Downloads"
    old = make_db(root / "radio_helper_update_0.11" / "data" / db.DB_NAME, experiences=3)
    newer = make_db(root / "radio_helper_update_0.13" / "data" / db.DB_NAME, experiences=140)
    empty = make_db(root / "radio_helper_update_0.14" / "data" / db.DB_NAME)          # 새 폴더에서 처음 켜 빈 데이터
    nested = make_db(root / "x" / "radio_helper_update_0.9" / "data" / db.DB_NAME, experiences=1)
    t = time.time()
    os.utime(old, (t - 300, t - 300))
    os.utime(nested, (t - 600, t - 600))
    os.utime(newer, (t - 100, t - 100))
    os.utime(empty, (t, t))
    assert set(db.old_data_candidates([root])) == {old, newer, empty, nested}
    assert db.best_old_db([root]) == newer


def test_adopt_copies_once(tmp_path):
    root = tmp_path / "Downloads"
    src = make_db(root / "radio_helper_update_0.13" / "data" / db.DB_NAME, experiences=5)
    (src.parent / "inspect").mkdir()
    (src.parent / "inspect" / "gorilla_last.png").write_bytes(b"png")
    home = tmp_path / "RadioHelper"
    home.mkdir()
    assert db.adopt_old_data(home, [root]) == src
    c = sqlite3.connect(home / db.DB_NAME)
    assert c.execute("SELECT COUNT(*) FROM experiences").fetchone()[0] == 5
    c.close()
    assert (home / "inspect" / "gorilla_last.png").exists() and "radio_helper_update_0.13" in (home / "옮겨온_데이터.txt").read_text(encoding="utf-8")
    # 이미 데이터가 있으면 다시 덮어쓰지 않는다
    make_db(root / "radio_helper_update_0.15" / "data" / db.DB_NAME, experiences=9)
    assert db.adopt_old_data(home, [root]) is None
    c = sqlite3.connect(home / db.DB_NAME)
    assert c.execute("SELECT COUNT(*) FROM experiences").fetchone()[0] == 5
    c.close()


def test_nothing_to_adopt(tmp_path):
    home = tmp_path / "RadioHelper"
    home.mkdir()
    assert db.adopt_old_data(home, [tmp_path / "없는 폴더", tmp_path]) is None and not (home / db.DB_NAME).exists()


def test_env_dir_skips_adopt(data_dir):
    assert db.data_dir() == data_dir and db.ADOPTED_FROM is None
