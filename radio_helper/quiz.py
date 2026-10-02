"""고릴라 단답형 퀴즈: 중복 키와 보류 사유.

이번 단계에서는 방송 음성 수집과 고릴라 화면 조작을 하지 않는다.
사용자가 들은 문제를 기록하면 같은 문제의 재안내를 한 건으로 묶고,
조건이 불분명한 문제는 보류로 표시한다. 정답 입력은 사용자가 고릴라에서 직접 한다.
"""

from __future__ import annotations

import re
import sqlite3

from . import db

KIND_LABELS = {"new": "새 문제", "rerun": "재방송", "answer_reveal": "정답 발표"}
ENTRY_LABELS = {
    "pending": "대기",
    "entered": "입력함·게시 미확인",
    "posted": "게시 확인",
    "unknown": "결과 불명",
    "failed": "실패",
}


def normalize_question(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text or "").lower()


def dedupe_key(account: str, program: str, broadcast_date: str, question_key: str) -> str:
    parts = [account.strip(), program.strip(), broadcast_date.strip(), normalize_question(question_key)]
    return "|".join(parts)


def hold_reasons(q, stopped: bool = False) -> list[str]:
    reasons = []
    if stopped:
        reasons.append("일괄 중지가 켜져 있습니다.")
    if q["kind"] != "new":
        reasons.append(f"{KIND_LABELS.get(q['kind'], q['kind'])}입니다. 새 문제로 처리하지 않습니다.")
    if not (q["question"] or "").strip():
        reasons.append("문제 내용이 없습니다.")
    if not (q["deadline"] or "").strip():
        reasons.append("응모 마감을 확인하지 못했습니다.")
    if q["gorilla_accepted"] != "yes":
        reasons.append("이 퀴즈가 고릴라 응모로 인정되는지 확인하지 못했습니다.")
    if not (q["answer"] or "").strip():
        reasons.append("정답 후보가 없습니다.")
    elif not q["answer_verified"]:
        reasons.append("정답 후보를 검증하지 않았습니다.")
    if q["entry_status"] in ("entered", "posted", "unknown"):
        reasons.append("이미 입력했거나 결과 불명입니다. 다시 보내지 않습니다.")
    return reasons


def add_or_bump(conn: sqlite3.Connection, fields: dict) -> tuple[int, bool]:
    """새 문제면 추가하고 (id, True), 같은 문제의 재안내면 횟수만 올리고 (id, False) 를 돌려준다."""
    account = fields.get("account") or db.get_setting(conn, "profile.account_label") or "기본"
    qkey = fields.get("question_key") or fields.get("question") or ""
    if not normalize_question(qkey):
        raise ValueError("문제 번호나 문제 내용 중 하나는 있어야 합니다.")
    key = dedupe_key(account, fields["program"], fields["broadcast_date"], qkey)
    row = conn.execute("SELECT id FROM quizzes WHERE dedupe_key = ?", (key,)).fetchone()
    if row:
        conn.execute("UPDATE quizzes SET repeat_count = repeat_count + 1, updated_at = ? WHERE id = ?",
                     (db.now(), row["id"]))
        conn.commit()
        return row["id"], False
    cur = conn.execute(
        """INSERT INTO quizzes (dedupe_key, account, channel, program, broadcast_date, question_key, kind,
               question, options, deadline, entry_channel, gorilla_accepted, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (key, account, fields.get("channel") or "파워FM", fields["program"], fields["broadcast_date"],
         qkey.strip(), fields.get("kind") or "new", fields.get("question"), fields.get("options"),
         fields.get("deadline"), fields.get("entry_channel") or "고릴라",
         fields.get("gorilla_accepted") or "unknown", db.now(), db.now()),
    )
    conn.commit()
    return cur.lastrowid, True
