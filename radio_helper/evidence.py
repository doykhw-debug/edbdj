"""보낸 증거 사진.

퀴즈 정답·사연을 채팅 앱(고릴라·mini·콩)이나 휴대폰 문자로 보낼 때, 게시판에 입력할 때 화면을 찍어
데이터 폴더의 evidence/년-월/ 에 JPEG 로 저장하고 evidence 표에 무엇을 언제 보냈는지와 함께 남긴다.
사진은 이 PC 에만 남고 어디로도 보내지 않는다 (Claude 분석에도 쓰지 않음).
"""

from __future__ import annotations

import io
import sqlite3
from datetime import datetime
from pathlib import Path

from . import db

DIR_NAME = "evidence"
MAX_WIDTH, MAX_HEIGHT = 1600, 6000   # 게시판 전체 화면처럼 긴 사진은 가로 기준으로 줄인다
ITEM_LABELS = {"quizzes": "퀴즈 정답", "story_posts": "사연(채팅·문자)", "submissions": "게시판 사연",
               "tests": "전송 테스트"}   # 전송 테스트는 항목 번호 없이 0


def root() -> Path:
    return db.data_dir() / DIR_NAME


def as_jpeg(data: bytes) -> tuple[bytes, str]:
    """PNG 등 → 줄인 JPEG. Pillow 가 없거나 읽지 못하면 원본 그대로 (확장자를 맞춘다)."""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data)).convert("RGB")
        scale = min(1.0, MAX_WIDTH / img.width, MAX_HEIGHT / img.height)
        if scale < 1.0:
            img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue(), ".jpg"
    except Exception:
        return data, ".png" if data[:4] == b"\x89PNG" else ".jpg"


def save(conn: sqlite3.Connection, item_table: str, item_id: int, via: str | None, text: str, status: str,
         shots, now: datetime | None = None) -> list[int]:
    """shots: [(설명, 이미지 바이트)] → evidence 행 번호들. 사진 저장에 실패해도 보내기는 막지 않는다."""
    ids = []
    now = now or datetime.now()
    for n, (label, data) in enumerate(shots or ()):
        if not data:
            continue
        try:
            body, ext = as_jpeg(data)
            rel = f"{now:%Y-%m}/{now:%Y%m%d_%H%M%S}_{item_table}{item_id}_{n + 1}{ext}"
            path = root() / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        except OSError:
            continue
        cur = conn.execute(
            "INSERT INTO evidence (item_table, item_id, via, label, text, status, file, taken_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (item_table, item_id, via, label, (text or "")[:2000], status, rel, now.strftime("%Y-%m-%d %H:%M:%S")))
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


def file_of(conn: sqlite3.Connection, eid: int) -> Path | None:
    """증거 사진 파일 (evidence 폴더 밖을 가리키면 None)."""
    row = conn.execute("SELECT file FROM evidence WHERE id = ?", (eid,)).fetchone()
    if row is None:
        return None
    base = root().resolve()
    path = (base / row["file"]).resolve()
    if base not in path.parents or not path.is_file():
        return None
    return path


def counts(conn: sqlite3.Connection, item_table: str) -> dict[int, int]:
    """항목 번호 → 사진 수 (목록 화면의 📷 표시)."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT item_id, COUNT(*) FROM evidence WHERE item_table = ? GROUP BY item_id", (item_table,))}


def listing(conn: sqlite3.Connection, item_table: str | None = None, item_id: int | None = None,
            day: str | None = None, limit: int = 60) -> list[sqlite3.Row]:
    where, params = [], []
    if item_table:
        where.append("item_table = ?")
        params.append(item_table)
    if item_id is not None:
        where.append("item_id = ?")
        params.append(item_id)
    if day:
        where.append("substr(taken_at, 1, 10) = ?")
        params.append(day)
    sql = "SELECT * FROM evidence" + (" WHERE " + " AND ".join(where) if where else "") + \
          " ORDER BY taken_at DESC, id LIMIT ?"   # 같은 때 찍은 사진(작성 → 보낸 뒤)은 찍은 순서대로
    return conn.execute(sql, (*params, limit)).fetchall()


def usage(conn: sqlite3.Connection) -> tuple[int, int]:
    """(사진 수, 차지하는 바이트)"""
    n = conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    size = sum(p.stat().st_size for p in root().rglob("*") if p.is_file()) if root().exists() else 0
    return n, size
