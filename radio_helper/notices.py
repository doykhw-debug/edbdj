"""메뉴의 빨간 알림 숫자 (확인 대기 퀴즈·사연).

'확인함'을 누르면 그때까지 쌓인 대기 글은 숫자에서 빠진다. 대기 글 자체는 그대로 남아 목록에서 보내거나 건너뛸 수 있고,
그 뒤에 새로 생긴 대기 글만 다시 숫자로 뜬다.
"""

from __future__ import annotations

import sqlite3

from . import db

KINDS = {
    "quizzes": ("notice.seen_quiz_id",
                "SELECT {what} FROM quizzes WHERE source = 'auto' AND entry_status = 'pending' AND approved = 0"),
    "stories": ("notice.seen_story_id",
                "SELECT {what} FROM story_posts WHERE status = 'pending' AND approved = 0"),
}


def _seen(conn: sqlite3.Connection, key: str) -> int:
    try:
        return int(db.get_setting(conn, key) or 0)
    except ValueError:
        return 0


def pending_counts(conn: sqlite3.Connection) -> dict:
    """확인 대기 전체 수 (첫 화면 '확인 대기' 칸)."""
    return {kind: conn.execute(sql.format(what="COUNT(*)")).fetchone()[0] for kind, (_key, sql) in KINDS.items()}


def unseen_counts(conn: sqlite3.Connection) -> dict:
    """'확인함'을 누른 뒤 새로 생긴 확인 대기 수 (메뉴 알림 숫자·창 제목)."""
    return {kind: conn.execute(sql.format(what="COUNT(*)") + " AND id > ?", (_seen(conn, key),)).fetchone()[0]
            for kind, (key, sql) in KINDS.items()}


def mark_seen(conn: sqlite3.Connection, kinds=None) -> None:
    """지금까지의 확인 대기 글을 알림 숫자에서 뺀다 (글은 그대로)."""
    for kind in kinds or KINDS:
        key, sql = KINDS[kind]
        top = conn.execute(sql.format(what="MAX(id)")).fetchone()[0]
        if top is not None and top > _seen(conn, key):
            db.set_setting(conn, key, str(top))
