"""인물 관계도의 실제 사건(한 줄)을 사연처럼 풀어 쓴다 (Claude API).

- 대상: 관계도 사건에서 옮긴 경험 중 '실제로 있었던 일'로 확인됐고 아직 풀어 쓰지 않은 것.
- 재료: 사건 한 줄 + 그 인물에 대해 확인된 정보(〔임의〕·〔사연〕 정보는 빼고, 실명은 ○○로 가림) + 문체.
- 사실은 바꾸지 않고 나의 마음·배경·마무리로만 살을 붙인다. 원래 사건 한 줄은 '바꾸면 안 되는 사실'로 남긴다.
- AI가 재료에 없는 사실을 넣었다고 표시하거나 실명·연락처가 들어가면 원래 한 줄을 그대로 둔다.

실행: python -m radio_helper.quizbot enrich
"""

from __future__ import annotations

import json
import sqlite3
from typing import Callable

from datetime import datetime

from .. import checks, db, generator
from . import config, story

BATCH = 8
STATUS_KEY = "stories.enrich"
LOG_KEY = "stories.enrich_log"
START_GRACE = 30   # 시작 직후 이 초 동안은 실행기가 아직 안 떴어도 '멈춤'으로 보지 않는다
SKIP_DETAIL_TAGS = {"임의", "사연"}
SKIP_DETAIL_KEYS = ("연락처", "전화", "주소", "이메일", "실명", "계좌")
MIN_STORY = 80


def pending(conn: sqlite3.Connection) -> list:
    return conn.execute("SELECT * FROM experiences WHERE from_event IS NOT NULL AND user_confirmed = 1 "
                        "AND enriched_at IS NULL ORDER BY id").fetchall()


def item_for(conn: sqlite3.Connection, exp) -> dict:
    """Claude 에 보낼 사건 하나 (실명·가릴 단어는 ○○)."""
    terms = story.banned_terms(conn, exp)
    pid = int((exp["from_event"] or "0|").split("|", 1)[0] or 0)
    person = conn.execute("SELECT * FROM people WHERE id = ?", (pid,)).fetchone()
    facts = []
    if person is not None:
        for d in json.loads(person["details"] or "[]"):
            if d.get("tag") in SKIP_DETAIL_TAGS or any(k in (d.get("key") or "") for k in SKIP_DETAIL_KEYS):
                continue
            fact = generator.mask_terms(f"{d.get('key')}: {d.get('value')}", terms)[:80]
            if fact.strip(": "):
                facts.append(fact)
    about = story.about_of(conn, exp) or "나"
    return {"id": exp["id"], "about": about, "when": exp["when_text"] or "",
            "relation": generator.mask_terms((person["relation"] or "") if person is not None and about != "나" else "",
                                             terms),
            "event": generator.mask_terms(exp["story"] or "", terms), "facts": facts[:8]}


def accept(conn: sqlite3.Connection, exp, out: dict) -> str:
    """풀어 쓴 결과를 쓸 수 있으면 '', 아니면 원래 한 줄을 남기는 이유."""
    if out.get("added_facts"):
        return "재료에 없는 내용을 넣었다고 표시함: " + " / ".join(out["added_facts"])[:120]
    text = " ".join(x for x in (out.get("story"), out.get("highlight"), out.get("ending")) if x)
    if len(out.get("story") or "") < MIN_STORY:
        return "풀어 쓴 글이 너무 짧음"
    blocks = [f.message for f in checks.check_personal_info(text, story.banned_terms(conn, exp)) if f.level == checks.BLOCK]
    if blocks:
        return "개인정보 검사: " + blocks[0]
    return ""


def save_status(conn: sqlite3.Connection, **status) -> None:
    db.set_setting(conn, STATUS_KEY, json.dumps({"at": db.now(), **status}, ensure_ascii=False))


def load_status(conn: sqlite3.Connection) -> dict:
    try:
        return json.loads(db.get_setting(conn, STATUS_KEY) or "{}")
    except ValueError:
        return {}


def process_alive() -> bool:
    """풀어 쓰기 실행기가 지금 돌고 있는지 (실행기가 잡고 있는 잠금으로 확인)."""
    lock = config.instance_lock("enrich")
    if lock is None:
        return True
    lock.close()
    return False


def view(conn: sqlite3.Connection, now: datetime | None = None, alive: Callable[[], bool] = process_alive) -> dict:
    """화면용 상태: state = idle(할 것 없음·대기) / running(진행 중) / stalled(실행기가 꺼짐) / done(끝)."""
    status, left = load_status(conn), len(pending(conn))
    state = "idle"
    if status.get("running"):
        try:
            age = ((now or datetime.now()) - datetime.strptime(status.get("at", ""), "%Y-%m-%d %H:%M:%S")).total_seconds()
        except ValueError:
            age = 1e9
        state = "running" if alive() or age < START_GRACE else "stalled"
    elif status.get("total") and not left:
        state = "done"
    return {**status, "state": state, "left": left,
            "progress": (status.get("done") or 0) + (status.get("kept") or 0)}


def run(conn: sqlite3.Connection, enrich: Callable[[dict, list[dict]], list[dict]],
        say: Callable[[str], None] = lambda _m: None) -> tuple[int, int]:
    """남은 사건을 BATCH 개씩 풀어 쓴다. (풀어 쓴 수, 원래 한 줄로 둔 수)"""
    todo = pending(conn)
    total, done, kept, error = len(todo), 0, 0, ""
    profile = story.masked_profile(conn)
    started = load_status(conn).get("started") or db.now()
    save_status(conn, running=True, done=0, kept=0, total=total, started=started)
    for start in range(0, total, BATCH):
        batch = todo[start:start + BATCH]
        try:
            results = {r["id"]: r for r in enrich(profile, [item_for(conn, e) for e in batch])}
        except Exception as e:  # 한 묶음이 실패해도 다음 묶음은 계속한다 (다음 실행 때 다시 시도)
            error = str(e)[:150]
            say(f"[오류] {type(e).__name__}: {error}")
            save_status(conn, running=True, done=done, kept=kept, total=total, started=started, error=error)
            if "API 키" in error or "권한" in error:
                break   # 키·권한 문제는 다음 묶음도 똑같이 실패한다
            continue
        for exp in batch:
            out = results.get(exp["id"]) or {}
            reason = accept(conn, exp, out) if out else "결과가 오지 않음"
            if reason:
                conn.execute("UPDATE experiences SET enriched_at = ?, enrich_note = ?, updated_at = ? WHERE id = ?",
                             (db.now(), "원래 한 줄 유지 — " + reason, db.now(), exp["id"]))
                kept += 1
            else:
                conn.execute(
                    """UPDATE experiences SET story = ?, highlight = ?, ending = ?,
                           fixed_facts = CASE WHEN COALESCE(fixed_facts, '') = '' THEN ? ELSE fixed_facts END,
                           enriched_at = ?, enrich_note = NULL, updated_at = ? WHERE id = ?""",
                    (out["story"], out["highlight"], out["ending"],
                     f"{exp['when_text'] or ''} {exp['story'] or ''}".strip(), db.now(), db.now(), exp["id"]))
                done += 1
        conn.commit()
        save_status(conn, running=True, done=done, kept=kept, total=total, started=started)
        say(f"… {min(start + BATCH, total)}/{total} (풀어 씀 {done} · 원래 한 줄 {kept})")
    save_status(conn, running=False, done=done, kept=kept, total=total, started=started,
                **({"error": error + " — 못 한 사건은 다음에 다시 시도합니다"} if error else {}))
    db.log(conn, "record", f"관계도 사건 사연처럼 풀어 쓰기: {done}건 완료, {kept}건은 원래 한 줄 유지")
    return done, kept
