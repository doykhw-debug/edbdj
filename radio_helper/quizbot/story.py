"""사연·주제 모집 → 내 실제 경험으로 공감로그 글 만들기 (확인 후 전송).

- 사용자가 '실제로 있었던 일'로 확인한 경험만 쓴다. 한 번 보낸 경험은 같은 방송국(SBS·MBC·KBS·CBS)에 다시 보내지 않는다.
  (다른 방송국에는 보낼 수 있다)
- Claude 에 보내기 전 '공개하지 않을 단어'와 경험별 '가릴 내용'을 ○○로 가린다.
- 사용자가 직접 쓴 한 줄은 허용했을 때 자동으로 보낸다. AI 초안은 '사연 자동 전송'을 켜고 검사 경고가
  하나도 없을 때만 바로 보내고, 경고가 있으면 확인 대기로 남긴다.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from difflib import SequenceMatcher

from .. import checks, db, generator, quiz
from .answerer import STORY_LIMIT

ACTIVE = ("pending", "entered", "posted", "unknown")
EXP_FIELDS = ["label", "when_text", "people", "story", "quotes", "quote_kind", "highlight", "ending",
              "fixed_facts", "gorilla_line", "song"]
SIMILAR_TOPIC = 0.7


def banned_terms(conn: sqlite3.Connection, exp=None) -> list[str]:
    from .. import people

    terms = checks.split_terms(db.get_profile(conn).get("banned_words")) + people.private_terms(conn)
    if exp is not None:
        terms += checks.split_terms(exp["hide"])
    return terms


def about_of(conn: sqlite3.Connection, exp) -> str | None:
    """경험의 주인공 호칭 (내 이야기면 None). 화자는 늘 나."""
    pid = exp["about_person_id"] if "about_person_id" in exp.keys() else None
    if not pid:
        return None
    row = conn.execute("SELECT alias, side FROM people WHERE id = ?", (pid,)).fetchone()
    if row is None or row["side"] == "self":
        return None
    return row["alias"] or None


def post_broadcaster(conn: sqlite3.Connection, post) -> str:
    """공감로그·문자 사연이 간 방송국. 채널을 적지 않던 예전 기록은 프로그램의 채널로 본다."""
    from . import config

    channel = post["channel"] if "channel" in post.keys() else None
    if not channel:
        row = conn.execute("SELECT channel FROM programs WHERE title = ?", (post["program"],)).fetchone()
        channel = row["channel"] if row else None
    return config.broadcaster_of(channel)


def sent_broadcasters(conn: sqlite3.Connection) -> dict[int, set[str]]:
    """경험 → 이미 보냈거나 보내려고 줄 세운(보냈는지 모르는 것 포함) 방송국들. 게시판은 코너 주소의 방송국."""
    out: dict[int, set[str]] = {}
    for r in conn.execute(
            f"SELECT * FROM story_posts WHERE experience_id IS NOT NULL AND status IN ({','.join('?' * len(ACTIVE))})",
            ACTIVE):
        out.setdefault(r["experience_id"], set()).add(post_broadcaster(conn, r))
    from .. import stations

    for r in conn.execute("SELECT s.experience_id, c.station, c.write_url, c.board_url FROM submissions s "
                          "JOIN corners c ON c.id = s.corner_id WHERE s.post_status IN ('filled', 'posted', 'unknown')"):
        out.setdefault(r["experience_id"], set()).add(stations.corner_station(r) or "SBS")
    return out


def used_experience_ids(conn: sqlite3.Connection, broadcaster: str | None = None) -> set[int]:
    """broadcaster 에 이미 보낸 경험. broadcaster 를 안 주면 어디로든 한 번이라도 보낸 경험.
    Aside 규칙(0.70, 기본): 한 번 보낸(결과 불명 포함) 경험은 모든 방송국·채널에서 다시 쓰지 않는다."""
    from .. import aside_records

    if aside_records.rules_on(conn):
        broadcaster = None
    return {eid for eid, where in sent_broadcasters(conn).items() if broadcaster is None or broadcaster in where}


def candidate_experiences(conn: sqlite3.Connection, broadcaster: str | None = None) -> list[dict]:
    """Claude 에 보낼 경험 목록 (확인됨·이 방송국에 아직 안 보냄, 가릴 단어는 ○○)."""
    used = used_experience_ids(conn, broadcaster)
    out = []
    for e in conn.execute("SELECT * FROM experiences WHERE user_confirmed = 1 ORDER BY id"):
        if e["id"] in used:
            continue
        terms = banned_terms(conn, e)
        item = {"id": e["id"]}
        for f in EXP_FIELDS:
            v = e[f] or ""
            item[f] = v if f == "quote_kind" else generator.mask_terms(v, terms)
        about = about_of(conn, e)
        item["about"] = about or "나"
        item["intro"] = people_intro(about)
        out.append(item)
    return out


def approved_text(conn: sqlite3.Connection, exp, limit: int, song: str = "", board: bool = False) -> str:
    """사용자가 '실제로 있었던 일'로 확인하고 고친 경험 글 그대로 만든 사연 (AI가 덧붙인 내용 없음).
    공감로그용 한 줄이 있으면 그것, 없으면 본문(게시판처럼 길게 쓸 수 있으면 마무리까지)을 문장 단위로 앞에서부터
    limit 안에 담는다. 다른 사람 이야기면 '제 ○○ 이야기인데요,'로 시작한다. 가릴 단어(실명 등)는 ○○로 바꾼다.
    board: 게시판 글 — 채팅용 한 줄보다 본문이 길면 본문을 쓴다 (0.60 — 예전에는 한 줄이 있으면 게시판에도 한 줄만 올렸음)."""
    terms = banned_terms(conn, exp)
    tail = f" (신청곡: {song})" if song else ""
    line = (exp["gorilla_line"] or "").strip()
    if board and len((exp["story"] or "").strip()) > len(line):
        line = ""
    if line and len(line) + len(tail) <= limit:
        return generator.mask_terms(line, terms) + tail
    about = about_of(conn, exp)
    lead = f"{people_intro(about)}, " if about else ""
    body = (exp["story"] or "").strip()
    if limit > 400 and (exp["ending"] or "").strip():
        body += "\n\n" + exp["ending"].strip()
    body = generator.mask_terms(body, terms)
    room = limit - len(lead) - len(tail)
    if len(body) <= room:
        return lead + body + tail
    out = ""
    for sentence in re.split(r"(?<=[.!?…])\s+", body.replace("\n", " ")):
        if len(out) + len(sentence) + 1 > room:
            break
        out = f"{out} {sentence}".strip()
    if not out:   # 첫 문장부터 길면 낱말 단위로 자른다
        out = body[:max(room - 1, 0)].rsplit(" ", 1)[0] + "…"
    return lead + out + tail


def people_intro(about: str | None) -> str:
    from ..people import intro_for

    return intro_for(about)


def masked_profile(conn: sqlite3.Connection) -> dict:
    p = db.get_profile(conn)
    terms = banned_terms(conn)
    return {k: generator.mask_terms(p.get(k) or "", terms) for k in ("tone", "family_aliases", "avoid_topics")}


def message_checks(conn: sqlite3.Connection, message: str, exp, source: str, added_facts=(),
                   limit: int | None = None) -> list[dict]:
    """공감로그·게시판 글 검사. level 'block' 이 있으면 보낼 수 없다. limit: 글자 수 제한 (기본 채팅 200자)."""
    found: list[dict] = []
    limit = limit or STORY_LIMIT
    if not message.strip():
        found.append({"level": "block", "message": "보낼 글이 비어 있습니다."})
    if len(message) > limit:
        found.append({"level": "block", "message": f"{len(message)}자로 제한({limit}자)을 넘습니다."})
    for f in checks.check_personal_info(message, banned_terms(conn, exp)):
        found.append({"level": f.level, "message": f.message})
    if exp is not None and source not in ("user_line", "manual", "approved"):   # 사용자가 쓰거나 확인한 글 그대로
        src = checks.experience_source_text(exp) + "\n" + (exp["gorilla_line"] or "")
        core = SONG_TAIL.sub("", message)   # 신청곡은 사건이 아니므로 '재료에 없는 내용' 검사에서 뺀다
        for f in (checks.check_sensitive_additions(core, src) + checks.check_numbers(core, src)
                  + checks.check_quotes(core, exp["quotes"], exp["quote_kind"])):
            found.append({"level": f.level, "message": f.message})
    if added_facts:
        found.append({"level": "warn", "message": "AI가 경험에 없는 내용을 넣었다고 표시함: " + " / ".join(added_facts)[:200]})
    if exp is None:
        if source != "manual":
            found.append({"level": "block", "message": "연결된 실제 경험이 없습니다. 직접 써서 보낼 수 있습니다."})
    elif not exp["user_confirmed"]:
        found.append({"level": "block", "message": "'실제로 있었던 일'로 확인되지 않은 경험입니다."})
    return found


SONG_TAIL = re.compile(r"\s*\(신청곡:.*\)\s*$")


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


def decide(conn: sqlite3.Connection, post, schedule, now: datetime, limit_per_hour: int,
           route: str = "app") -> list[str]:
    """보내기를 막는 이유. 비어 있으면 보낸다. 문자로 보낼 때는 고릴라 응모 조건을 보지 않는다."""
    reasons = []
    if db.is_stopped(conn):
        reasons.append("일괄 중지가 켜져 있음")
    if "target" in post.keys() and post["target"] == "board":
        reasons.append("게시판용 글 — 원고 검토함에서 게시판에 입력")
    if post["status"] != "pending":
        reasons.append(f"이미 처리됨({quiz.ENTRY_LABELS.get(post['status'], post['status'])})")
    warnings = json.loads(post["warnings"] or "[]")
    if not (post["message"] or "").strip():      # 0.80: 맞는 경험이 없으면 글이 비어 있음 — 빈 글을 붙여넣어 '메시지를 작성해주세요'가 떴음
        reasons.append("보낼 글이 비어 있음 (주제에 맞는 경험이 없음 — 직접 써서 보내거나 건너뛰기)")
    if has_block(warnings):
        reasons.append("검사에서 막힌 항목이 있음")
    # 0.76: 응모 방법 판단('문자로만' 등)으로 막지 않는다 — 문자로만 받는 방송은 없음, 무조건 채팅
    if not post["approved"]:
        def allowed(key):
            return schedule is not None and key in schedule.keys() and bool(schedule[key])

        auto_ok = (
            (post["source"] == "user_line" and allowed("story_auto_user_line"))
            # 승인한(확인·고친) 경험 글 그대로: 덧붙인 내용이 없으니 '사연 자동 전송'이 켜져 있으면 바로 보낸다
            or (post["source"] == "approved" and allowed("story_auto_ai") and not has_block(warnings))
            # AI 초안은 검사 경고가 하나도 없을 때만 (재료에 없는 내용·민감 소재·숫자가 있으면 확인 대기)
            or (post["source"] == "ai" and allowed("story_auto_ai") and not warnings))
        if not auto_ok:
            reasons.append("사연은 확인 후 전송 (화면에서 '이 글로 보내기')")
    since = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    channel = post["channel"] if "channel" in post.keys() else None   # 채널마다 센다 (동시 청취)
    sent = conn.execute("SELECT COUNT(*) FROM story_posts WHERE sent_at >= ? AND channel IS ?",
                        (since, channel)).fetchone()[0]
    if limit_per_hour and sent >= limit_per_hour:          # 0 = 제한 없음
        reasons.append(f"최근 1시간 사연 전송 {sent}건으로 상한({limit_per_hour}) 도달")
    return reasons
