"""사연·주제 모집 → 내 실제 경험으로 공감로그 글 만들기 (확인 후 전송).

- 사용자가 '실제로 있었던 일'로 확인한 경험만 쓴다. 이미 다른 곳에 보낸 경험은 쓰지 않는다.
- Claude 에 보내기 전 '공개하지 않을 단어'와 경험별 '가릴 내용'을 ○○로 가린다.
- AI 초안은 항상 사용자가 확인해야 보낸다. 사용자가 직접 쓴 한 줄은 예약에서 허용했을 때만 자동으로 보낸다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from .. import checks, db, generator, quiz
from .answerer import STORY_LIMIT

ACTIVE = ("pending", "entered", "posted", "unknown")
EXP_FIELDS = ["label", "when_text", "people", "story", "quotes", "quote_kind", "highlight", "ending",
              "fixed_facts", "gorilla_line"]
SIMILAR_TOPIC = 0.7


def banned_terms(conn: sqlite3.Connection, exp=None) -> list[str]:
    terms = checks.split_terms(db.get_profile(conn).get("banned_words"))
    if exp is not None:
        terms += checks.split_terms(exp["hide"])
    return terms


def used_experience_ids(conn: sqlite3.Connection) -> set[int]:
    used = {r[0] for r in conn.execute(
        f"SELECT experience_id FROM story_posts WHERE experience_id IS NOT NULL AND status IN ({','.join('?' * len(ACTIVE))})",
        ACTIVE)}
    used |= {r[0] for r in conn.execute(
        "SELECT experience_id FROM submissions WHERE post_status IN ('filled', 'posted', 'unknown')")}
    return used


def candidate_experiences(conn: sqlite3.Connection) -> list[dict]:
    """Claude 에 보낼 경험 목록 (확인됨·미사용, 가릴 단어는 ○○)."""
    used = used_experience_ids(conn)
    out = []
    for e in conn.execute("SELECT * FROM experiences WHERE user_confirmed = 1 ORDER BY id"):
        if e["id"] in used:
            continue
        terms = banned_terms(conn, e)
        item = {"id": e["id"]}
        for f in EXP_FIELDS:
            v = e[f] or ""
            item[f] = v if f == "quote_kind" else generator.mask_terms(v, terms)
        out.append(item)
    return out


def masked_profile(conn: sqlite3.Connection) -> dict:
    p = db.get_profile(conn)
    terms = banned_terms(conn)
    return {k: generator.mask_terms(p.get(k) or "", terms) for k in ("tone", "family_aliases", "avoid_topics")}


def message_checks(conn: sqlite3.Connection, message: str, exp, source: str, added_facts=()) -> list[dict]:
    """공감로그 글 검사. level 'block' 이 있으면 보낼 수 없다."""
    found: list[dict] = []
    if not message.strip():
        found.append({"level": "block", "message": "보낼 글이 비어 있습니다."})
    if len(message) > STORY_LIMIT:
        found.append({"level": "block", "message": f"{len(message)}자로 공감로그 제한({STORY_LIMIT}자)을 넘습니다."})
    for f in checks.check_personal_info(message, banned_terms(conn, exp)):
        found.append({"level": f.level, "message": f.message})
    if exp is not None and source != "user_line":
        src = checks.experience_source_text(exp) + "\n" + (exp["gorilla_line"] or "")
        for f in (checks.check_sensitive_additions(message, src) + checks.check_numbers(message, src)
                  + checks.check_quotes(message, exp["quotes"], exp["quote_kind"])):
            found.append({"level": f.level, "message": f.message})
    if added_facts:
        found.append({"level": "warn", "message": "AI가 경험에 없는 내용을 넣었다고 표시함: " + " / ".join(added_facts)[:200]})
    if exp is None:
        found.append({"level": "block", "message": "연결된 실제 경험이 없습니다."})
    elif not exp["user_confirmed"]:
        found.append({"level": "block", "message": "'실제로 있었던 일'로 확인되지 않은 경험입니다."})
    return found


def has_block(warnings: list[dict]) -> bool:
    return any(w["level"] == "block" for w in warnings)


def find_duplicate(existing, topic: str) -> int | None:
    t = quiz.normalize_question(topic)
    if not t:
        return None
    for r in existing:
        o = quiz.normalize_question(r["topic"])
        if o and (t in o or o in t or SequenceMatcher(None, t, o).ratio() >= SIMILAR_TOPIC):
            return r["id"]
    return None


def decide(conn: sqlite3.Connection, post, schedule, now: datetime, limit_per_hour: int) -> list[str]:
    """보내기를 막는 이유. 비어 있으면 보낸다."""
    reasons = []
    if db.is_stopped(conn):
        reasons.append("일괄 중지가 켜져 있음")
    if post["status"] != "pending":
        reasons.append(f"이미 처리됨({quiz.ENTRY_LABELS.get(post['status'], post['status'])})")
    warnings = json.loads(post["warnings"] or "[]")
    if has_block(warnings):
        reasons.append("검사에서 막힌 항목이 있음")
    if post["gorilla_accepted"] == "no":
        reasons.append("진행자가 고릴라가 아닌 다른 방법으로 받는다고 함")
    if not post["approved"]:
        auto_ok = (post["source"] == "user_line" and schedule is not None and schedule["story_auto_user_line"]
                   and (post["gorilla_accepted"] == "yes" or schedule["gorilla_confirmed"]))
        if not auto_ok:
            reasons.append("사연은 확인 후 전송 (화면에서 '이 글로 보내기')")
    since = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    sent = conn.execute("SELECT COUNT(*) FROM story_posts WHERE sent_at >= ?", (since,)).fetchone()[0]
    if sent >= limit_per_hour:
        reasons.append(f"최근 1시간 사연 전송 {sent}건으로 상한({limit_per_hour}) 도달")
    return reasons
