"""입력·제출 전 차단 조건.

확인하지 못한 조건은 '허용'이 아니라 '대기'로 취급한다 (명세 3-B, 4절).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from . import checks, db

MODE_MOCK = "mock"        # 로컬 모의 화면. 실제 사이트에 아무것도 남기지 않는다.
MODE_INSPECT = "inspect"  # 실제 글쓰기 화면 읽기 전용 점검.
MODE_FILL = "fill"        # 실제 글쓰기 화면에 입력만. 등록 버튼은 사용자가 직접 누른다.

ACTIVE_POST_STATUSES = ("filled", "posted", "unknown")


@dataclass
class GateResult:
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blockers


def _parse_deadline(text: str | None) -> datetime | None:
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = datetime.strptime(text.strip(), fmt)
            return d.replace(hour=23, minute=59) if fmt == "%Y-%m-%d" else d
        except ValueError:
            continue
    return None


def corner_blockers(corner: sqlite3.Row) -> tuple[list[str], list[str]]:
    blockers, warnings = [], []
    if not corner["is_target"]:
        blockers.append("이 코너는 제출 대상으로 지정되지 않았습니다. (첫 대상은 '사연과 신청곡' 한 곳)")
    if corner["recruiting"] != "open":
        blockers.append("현재 사연 모집 여부가 '모집 중'으로 확인되지 않았습니다.")
    if corner["notice_changed"]:
        blockers.append("코너 공지가 바뀌었습니다. 재검토 후 '공지 확인 완료'를 눌러야 합니다.")
    elif not corner["notice_checked_at"]:
        blockers.append("모집 공지·마감·필수 항목을 확인했다는 기록이 없습니다.")
    deadline = _parse_deadline(corner["deadline"])
    if corner["deadline"] and deadline is None:
        warnings.append(f"마감 '{corner['deadline']}'을(를) 날짜로 읽지 못했습니다. YYYY-MM-DD 형식을 권장합니다.")
    elif deadline and deadline < datetime.now():
        blockers.append(f"코너 마감({corner['deadline']})이 지났습니다.")
    elif not corner["deadline"]:
        warnings.append("마감이 입력되지 않았습니다.")
    if corner["ai_assist_policy"] == "restricted":
        blockers.append("이 코너 공지는 창작물·AI 보조 작성에 제한을 둡니다. 직접 쓴 글만 보낼 수 있습니다.")
    elif corner["ai_assist_policy"] == "unknown":
        warnings.append("AI 보조 작성 허용 여부를 확인하지 못했습니다.")
    if not corner["write_url"]:
        blockers.append("확인된 글쓰기 화면 주소가 없습니다.")
    return blockers, warnings


def previous_bodies(conn: sqlite3.Connection, exclude_draft_id: int | None = None) -> list[str]:
    rows = conn.execute(
        "SELECT body FROM submissions WHERE post_status IN ('filled','posted','unknown') "
        "AND (draft_id IS NULL OR draft_id != ?)",
        (exclude_draft_id or -1,),
    ).fetchall()
    return [r["body"] or "" for r in rows]


def draft_findings(conn: sqlite3.Connection, draft: sqlite3.Row) -> list[checks.Finding]:
    exp = conn.execute("SELECT * FROM experiences WHERE id = ?", (draft["experience_id"],)).fetchone()
    corner = conn.execute("SELECT * FROM corners WHERE id = ?", (draft["corner_id"],)).fetchone()
    return checks.run_all(
        title=draft["title"], body=draft["body"], song=draft["song"], exp=exp,
        profile=db.get_profile(conn), char_limit=corner["char_limit"],
        previous_bodies=previous_bodies(conn, draft["id"]),
    )


def evaluate_inspect(conn: sqlite3.Connection, corner_id: int) -> GateResult:
    """읽기 전용 점검: 글을 남기지 않으므로 일괄 중지와 글쓰기 주소만 본다."""
    result = GateResult()
    if db.is_stopped(conn):
        result.blockers.append("일괄 중지가 켜져 있습니다. 설정에서 해제하기 전에는 아무 입력도 하지 않습니다.")
    corner = conn.execute("SELECT * FROM corners WHERE id = ?", (corner_id,)).fetchone()
    if corner is None:
        result.blockers.append(f"코너 #{corner_id}을(를) 찾을 수 없습니다.")
    elif not corner["write_url"]:
        result.blockers.append("확인된 글쓰기 화면 주소가 없습니다.")
    return result


def evaluate(conn: sqlite3.Connection, draft_id: int, mode: str) -> GateResult:
    """mock(로컬 모의 화면) 또는 fill(실제 화면 입력만) 전 점검."""
    result = GateResult()
    if db.is_stopped(conn):
        result.blockers.append("일괄 중지가 켜져 있습니다. 설정에서 해제하기 전에는 아무 입력도 하지 않습니다.")

    draft = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    if draft is None:
        result.blockers.append(f"원고 #{draft_id}을(를) 찾을 수 없습니다.")
        return result
    corner = conn.execute("SELECT * FROM corners WHERE id = ?", (draft["corner_id"],)).fetchone()

    findings = draft_findings(conn, draft)
    if mode == MODE_MOCK:
        # 모의 화면은 실제 사이트에 아무것도 남기지 않으므로 빈 원고만 막는다.
        result.blockers += [f.message for f in findings if f.code in ("empty_title", "empty_body")]
        result.warnings += [f.message for f in findings if f.code not in ("empty_title", "empty_body")]
        return result

    exp = conn.execute("SELECT * FROM experiences WHERE id = ?", (draft["experience_id"],)).fetchone()
    b, w = corner_blockers(corner)
    result.blockers += b
    result.warnings += w
    if not exp["user_confirmed"]:
        result.blockers.append("이 경험이 실제 있었던 일이라고 사용자가 확인하지 않았습니다.")
    if draft["status"] != "approved":
        result.blockers.append("원고가 승인되지 않았습니다. 사용자가 확인한 원고만 제출 대상입니다.")
    if not draft["fact_confirmed"]:
        result.blockers.append("원고의 사건·인물·결과가 실제와 같다는 확인이 없습니다.")
    active = conn.execute(
        "SELECT s.id, c.title AS corner_title, s.post_status FROM submissions s "
        "JOIN corners c ON c.id = s.corner_id "
        "WHERE s.experience_id = ? AND s.post_status IN ('filled','posted','unknown')",
        (draft["experience_id"],),
    ).fetchall()
    for s in active:
        result.blockers.append(
            f"같은 경험이 이미 제출 이력 #{s['id']} ({s['corner_title']}, {STATUS_LABELS.get(s['post_status'], s['post_status'])})에 있습니다. "
            "한 경험은 한 곳에만 보내고, 결과 불명이면 다시 보내지 않습니다.")
    result.blockers += [f.message for f in findings if f.level == checks.BLOCK]
    result.warnings += [f.message for f in findings if f.level == checks.WARN]
    return result


STATUS_LABELS = {
    "filled": "입력만 완료·등록 미확인",
    "posted": "등록 확인",
    "unknown": "결과 불명",
    "failed": "실패",
}
