"""인물 관계도 · 사연 보관함.

- 화자는 언제나 '나'(보내는 사람)다. 다른 사람이 주인공인 실제 경험은 "제 와이프 이야기인데요"처럼
  관계 호칭으로 시작해 쓴다. 사연과 AI 요청에는 실명 대신 '호칭'만 쓴다.
- 인물 관계도 파일(character-map.md)에서 인물 카드·실제 사건(연표)을 가져온다.
  〔사연〕 태그의 가상 사건은 인물 카드에 넣지 않는다.
- 사연 로그(.jsonl)는 '사연 보관함'에 넣는다. 원본 사연을 각색한 가상 사연(fiction)은 그대로는 보내지 않는다.
  같은 파일에 {"type": "person", ...} 줄이 있으면 그 인물(친구 등)을 먼저 추가하거나 직업을 적는다.
  사연은 이름·호칭(와이프 등)·이름 끝(홍길동 ← 길동)로 인물에 붙인다.
- 인물의 실제 사건(대화·통화로 확인한 연표)은 '실제로 있었던 일'로 확인된 경험으로 한 번에 옮긴다.
  옮긴 사건은 quizbot/enrich.py 가 사실은 그대로 두고 사연처럼 풀어 쓴다.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field

from . import db

SIDES = {"self": "나", "family": "가족", "work": "일", "friend": "친구", "life": "생활"}
FAMILY_TERMS = {"와이프", "첫째 딸", "둘째 딸", "어머니", "아버지", "여동생", "매부", "조카", "장모님", "장인어른",
                "형님", "형님 여자친구", "외할머니", "이모", "이모부", "외삼촌", "할머니", "고모", "삼촌"}
SELF_NAMES = {"나", "주인공"}
TAG = re.compile(r"\s*〔([^〕]*)〕\s*$")
CODE = re.compile(r"\((R\d{3,})\)")


class PeopleImportError(ValueError):
    pass


# ── 호칭 ─────────────────────────────────────────────────────────────
def default_alias(name: str, side: str, grp: str) -> str:
    """사연에 쓸 호칭 (실명 대신). 사용자가 인물 화면에서 바꿀 수 있다."""
    if side == "self":
        return "나"
    if name in FAMILY_TERMS:
        return name
    if side == "family":
        return "사촌" if ("외가" in grp or "친가" in grp) else "친척"
    if "직장" in grp:
        return "회사 동료"
    if "영상" in grp:
        return "일로 만난 분"
    if "대학" in grp:
        return "대학 친구"
    if "친구" in grp:
        return "고향 친구"
    return "아는 분"


def intro_for(alias: str | None) -> str:
    """사연 첫머리: 화자는 늘 나."""
    if not alias or alias == "나":
        return "제 이야기인데요"
    return f"제 {alias} 이야기인데요"


def private_terms(conn: sqlite3.Connection) -> list[str]:
    """사연·AI 요청에 나오면 안 되는 인물 이름 (호칭과 다른 이름의 앞 낱말, 예: '홍길동 과장님' → '홍길동')."""
    terms = []
    for r in conn.execute("SELECT name, alias, side FROM people"):
        if r["side"] == "self" or r["name"] in FAMILY_TERMS or r["name"] == (r["alias"] or ""):
            continue
        first = r["name"].split()[0].strip("()·")
        if re.fullmatch(r"[가-힣]{2,4}", first) and first not in FAMILY_TERMS:
            terms.append(first)
    return terms


# ── 인물 관계도 파일 읽기 ─────────────────────────────────────────────
@dataclass
class Person:
    name: str
    grp: str = ""
    side: str = "family"
    age: str = ""
    relation: str = ""
    details: list = field(default_factory=list)
    events: list = field(default_factory=list)


def _split_tag(text: str) -> tuple[str, str]:
    m = TAG.search(text)
    return (text[:m.start()].strip(), m.group(1)) if m else (text.strip(), "")


def _side_for(section: str, grp: str) -> str:
    if section == "self":
        return "self"
    if section == "family":
        return "family"
    if "직장" in grp or "영상 일" in grp or "클라이언트" in grp:
        return "work"
    if "친구" in grp or "모임" in grp:
        return "friend"
    return "life"


def parse_character_map(text: str) -> list[Person]:
    """character-map.md → 인물 목록. 실제 사건만 events 에 넣는다 (〔사연〕·〔임의〕 사건 제외)."""
    people: dict[str, Person] = {}
    order: list[str] = []
    section, grp, current, in_events, timeline_person = "", "", None, False, None

    def get(name: str) -> Person | None:
        name = re.sub(r"\s*\(.*?\)\s*$", "", name).strip()
        if name in people:
            return people[name]
        for p in people.values():
            if p.side == "self" and (name in SELF_NAMES or name == p.name):
                return p
        return None

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            title = line[3:].strip()
            section = ("self" if title.startswith("주인공") else
                       "family" if title.startswith("가족 인물") else
                       "others" if title.startswith("가족 밖") else
                       "timeline" if title.startswith("사건 연표") else "skip")
            grp, current, in_events, timeline_person = "", None, False, None
            continue
        if section == "skip" or not line.strip():
            continue
        if line.startswith("### "):
            heading = line[4:].strip()
            if section == "self":
                m = re.match(r"(.+?)\s*\((\d+)세\)", heading)
                name, age = (m.group(1), m.group(2) + "세") if m else (heading, "")
                current = people.setdefault(name, Person(name=name, grp="나", side="self", age=age))
                if name not in order:
                    order.append(name)
            elif section in ("family", "others"):
                grp, current = heading, None
            continue
        if line.startswith("#### ") and section in ("family", "others"):
            name = line[5:].strip()
            current = people.setdefault(name, Person(name=name, grp=grp, side=_side_for(section, grp)))
            if name not in order:
                order.append(name)
            in_events = False
            continue
        if section == "timeline":
            m = re.match(r"\*\*(.+?)\*\*", line)
            if m:
                timeline_person = get(m.group(1))
                continue
            m = re.match(r"-\s+(\d{4}\.\d{2}(?:~[\d.]+)?)\s+(.+)", line)
            if m and timeline_person is not None:
                body, tag = _split_tag(m.group(2))
                if tag not in ("사연", "임의") and not CODE.search(body):
                    event = {"when": m.group(1), "text": body, "tag": tag}
                    if event not in timeline_person.events:
                        timeline_person.events.append(event)
            continue
        if current is None:
            continue
        m = re.match(r"(\s*)-\s+(.*)", line)
        if not m:
            continue
        indent, item = len(m.group(1)), m.group(2)
        if indent and in_events:
            em = re.match(r"(\d{4}\.\d{2}(?:~[\d.]+)?)\s+(.+)", item)
            if em:
                body, tag = _split_tag(em.group(2))
                if tag not in ("사연", "임의"):
                    current.events.append({"when": em.group(1), "text": body, "tag": tag})
            continue
        key, sep, value = item.partition(":")
        if not sep:
            continue
        key = key.strip()
        if key == "사건":
            in_events = True
            continue
        in_events = False
        value, tag = _split_tag(value)
        if key == "나이":
            current.age = value
        if key == "한 줄 소개":
            current.relation = value
        current.details.append({"key": key, "value": value, "tag": tag})
    return [people[n] for n in order]


def import_people(conn: sqlite3.Connection, people: list[Person]) -> tuple[int, int]:
    """(새로 넣은 수, 고친 수). 사용자가 바꾼 호칭·가까움·메모는 그대로 둔다."""
    added = updated = 0
    for i, p in enumerate(people):
        row = conn.execute("SELECT id FROM people WHERE name = ?", (p.name,)).fetchone()
        values = (p.grp, p.side, p.relation, p.age, json.dumps(p.details, ensure_ascii=False),
                  json.dumps(p.events, ensure_ascii=False), i, db.now())
        if row:
            conn.execute("UPDATE people SET grp = ?, side = ?, relation = ?, age = ?, details = ?, events = ?, sort = ?, "
                         "updated_at = ? WHERE id = ?", (*values, row["id"]))
            updated += 1
        else:
            conn.execute("INSERT INTO people (grp, side, relation, age, details, events, sort, updated_at, name, alias, "
                         "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (*values, p.name, default_alias(p.name, p.side, p.grp), db.now()))
            added += 1
    conn.commit()
    return added, updated


# ── 실제 사건 → 경험 ──────────────────────────────────────────────────
SKIP_EVENT_TAGS = {"사연", "임의"}   # 가상 사건·근거 없는 사건은 옮기지 않는다 (가져올 때도 이미 뺌)


def event_key(person_id: int, ev: dict) -> str:
    return f"{person_id}|{ev.get('when') or ''}|{(ev.get('text') or '').strip()}"


def events_to_experiences(conn: sqlite3.Connection, person_id: int | None = None,
                          dry_run: bool = False) -> tuple[int, int]:
    """인물 관계도의 실제 사건(대화·통화 등으로 확인한 연표)을 '실제로 있었던 일'로 확인된 경험으로 한 번에 옮긴다.

    관계도 연표는 사용자가 대화·통화로 확인한 실제 사건이라 사건마다 따로 검수하지 않는다(사용자 요청).
    가상(〔사연〕)·임의 사건은 옮기지 않고, 이미 옮겼거나 같은 시기·내용으로 만든 경험이 있으면 건너뛴다.
    (새로 만든 수, 건너뛴 수). dry_run 이면 세기만 한다."""
    people = conn.execute("SELECT * FROM people" + (" WHERE id = ?" if person_id else "") + " ORDER BY sort, id",
                          (person_id,) if person_id else ()).fetchall()
    moved = {r[0] for r in conn.execute("SELECT from_event FROM experiences WHERE from_event IS NOT NULL")}
    added = skipped = 0
    for p in people:
        about = None if p["side"] == "self" else p["id"]
        made = {(r["when_text"] or "", (r["label"] or "")[:30]) for r in conn.execute(
            "SELECT when_text, label FROM experiences WHERE " +
            ("about_person_id IS NULL" if about is None else "about_person_id = ?"), () if about is None else (about,))}
        for ev in json.loads(p["events"] or "[]"):
            text = (ev.get("text") or "").strip()
            if not text or ev.get("tag") in SKIP_EVENT_TAGS:
                continue
            key = event_key(p["id"], ev)
            if key in moved or ((ev.get("when") or ""), text[:30]) in made:
                skipped += 1
                continue
            if not dry_run:
                conn.execute(
                    """INSERT INTO experiences (label, when_text, people, story, quote_kind, about_person_id, from_event,
                           user_confirmed, created_at, updated_at) VALUES (?, ?, ?, ?, 'none', ?, ?, 1, ?, ?)""",
                    (text[:60], ev.get("when") or "", "나" if about is None else (p["alias"] or ""), text, about, key,
                     db.now(), db.now()))
            moved.add(key)
            added += 1
    if not dry_run:
        conn.commit()
    return added, skipped


def library_to_experience(conn: sqlite3.Connection, item) -> tuple[int, bool]:
    """보관함 사연 한 편을 '확인 전' 경험으로 옮긴다. (경험 번호, 새로 만들었나). 사용자가 고치고 확인해야 보낸다."""
    moved = conn.execute("SELECT id FROM experiences WHERE from_library_id = ?", (item["id"],)).fetchone()
    if moved:
        return moved["id"], False
    person = conn.execute("SELECT id, alias, side FROM people WHERE id = ?", (item["person_id"],)).fetchone() \
        if item["person_id"] else None
    about = person["id"] if person and person["side"] != "self" else None
    body = (item["body"] or "").strip()
    intro = (item["intro"] or "").strip().rstrip(".,")
    if intro and body.startswith(intro):   # '제 와이프 이야기인데요'는 원고를 만들 때 다시 붙인다
        body = body[len(intro):].lstrip(" ,.\n")
    cur = conn.execute(
        """INSERT INTO experiences (label, when_text, people, story, highlight, quote_kind, song, about_person_id,
               from_library_id, user_confirmed, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, 'none', ?, ?, ?, 0, ?, ?)""",
        ((item["title"] or "")[:60], item["event_date"] or "", (person["alias"] if about else "나") or "",
         body, item["summary"] or "", item["song"] or "", about, item["id"], db.now(), db.now()))
    return cur.lastrowid, True


def library_to_experiences(conn: sqlite3.Connection) -> tuple[int, int]:
    """보관함 사연을 모두 '확인 전' 경험으로 가져온다 (이미 옮긴 것은 건너뜀). (가져온 수, 건너뛴 수)"""
    added = skipped = 0
    for item in conn.execute("SELECT * FROM story_library ORDER BY code, id").fetchall():
        _eid, created = library_to_experience(conn, item)
        added += created
        skipped += not created
    conn.commit()
    return added, skipped


def confirm_moved_events(conn: sqlite3.Connection) -> int:
    """예전에 '확인 전'으로 옮긴 관계도 사건도 확인된 경험으로 바꾼다."""
    n = conn.execute("UPDATE experiences SET user_confirmed = 1, updated_at = ? "
                     "WHERE from_event IS NOT NULL AND user_confirmed = 0", (db.now(),)).rowcount
    conn.commit()
    return n


def events_left(conn: sqlite3.Connection, person_id: int | None = None) -> int:
    """아직 경험으로 옮기지 않은 실제 사건 수."""
    return events_to_experiences(conn, person_id, dry_run=True)[0]


# ── 사연 로그 읽기 ────────────────────────────────────────────────────
LIBRARY_FIELDS = {"code": "id", "person_label": "character", "title": "title", "intro": "intro",
                  "event_date": "event_date", "summary": "event_summary", "timing": "broadcast_timing",
                  "song": "song", "closing": "closing", "body": "body", "category": "category", "theme": "theme"}


def parse_story_log(text: str) -> list[dict]:
    rows = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except ValueError:
            raise PeopleImportError(f"{n}번째 줄을 읽지 못했습니다 (한 줄에 사연 하나씩인 .jsonl 파일이어야 합니다).")
        if not isinstance(data, dict) or not data.get("body"):
            continue
        row = {k: str(data.get(src) or "").strip() for k, src in LIBRARY_FIELDS.items()}
        # 원본 사연을 옮겨 쓴 글은 가상(각색)이다
        adapted = bool(data.get("source_id") or data.get("source_title") or data.get("source_site"))
        row["kind"] = "fiction" if adapted or data.get("kind") != "real" else "real"
        row["origin"] = " ".join(str(data.get(k) or "") for k in ("source_site", "source_id", "source_title")).strip()
        rows.append(row)
    if not rows:
        raise PeopleImportError("사연을 찾지 못했습니다.")
    return rows


def find_person(conn: sqlite3.Connection, label: str) -> int | None:
    """이름 → 인물 번호. 이름 그대로, '홍길동 과장님'의 앞 단어, 호칭(와이프 등), 이름 끝(홍길동 ← 길동, 한 명일 때만)."""
    label = re.sub(r"\s*\(.*\)$", "", (label or "").strip())
    if not label:
        return None
    rows = conn.execute("SELECT id, name, alias, side FROM people ORDER BY sort, id").fetchall()
    if label in SELF_NAMES or any(r["side"] == "self" and r["name"] == label for r in rows):
        mine = [r["id"] for r in rows if r["side"] == "self"]
        return mine[0] if mine else None
    for match in (lambda r: r["name"] == label, lambda r: r["name"].split()[0] == label,
                  lambda r: (r["alias"] or "") == label):
        found = [r["id"] for r in rows if match(r)]
        if found:
            return found[0]
    if len(label) >= 2:
        found = [r["id"] for r in rows if r["side"] != "self" and len(r["name"].split()[0]) > len(label)
                 and r["name"].split()[0].endswith(label)]
        if len(found) == 1:
            return found[0]
    return None


def parse_person_records(text: str) -> list[dict]:
    """사연 로그 안의 인물 줄: {"type": "person", "name", "side", "grp", "alias", "relation", "job", "job_tag", "note"}"""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{") or '"person"' not in line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("type") == "person" and str(data.get("name") or "").strip():
            out.append({k: str(data.get(k) or "").strip() for k in
                        ("name", "side", "grp", "alias", "relation", "job", "job_tag", "note")})
    return out


def import_person_records(conn: sqlite3.Connection, records: list[dict]) -> tuple[int, int]:
    """(새로 넣은 인물 수, 직업 등을 적은 기존 인물 수). 사용자가 바꾼 호칭·가까움은 그대로 둔다.
    직업은 '주신'(사용자가 알려 준 실제 정보)이면 바꾸고, '임의'면 비어 있을 때만 채운다. 실제 사건은 넣지 않는다."""
    added = updated = 0
    for rec in records:
        side = rec["side"] if rec["side"] in SIDES and rec["side"] != "self" else "friend"
        tag = rec["job_tag"] or "임의"
        pid = find_person(conn, rec["name"])
        if pid is None:
            details = [{"key": "직업", "value": rec["job"], "tag": tag}] if rec["job"] else []
            conn.execute("INSERT INTO people (name, alias, grp, side, relation, details, events, note, sort, created_at, "
                         "updated_at) VALUES (?, ?, ?, ?, ?, ?, '[]', ?, 900, ?, ?)",
                         (rec["name"], (rec["alias"] or default_alias(rec["name"], side, rec["grp"] or "친구"))[:20],
                          rec["grp"] or "친구", side, rec["relation"] or None,
                          json.dumps(details, ensure_ascii=False), rec["note"] or None, db.now(), db.now()))
            added += 1
            continue
        p = conn.execute("SELECT * FROM people WHERE id = ?", (pid,)).fetchone()
        details = json.loads(p["details"] or "[]")
        job = next((d for d in details if d.get("key") == "직업"), None)
        changed = False
        if rec["job"] and job is None:
            details.append({"key": "직업", "value": rec["job"], "tag": tag})
            changed = True
        elif rec["job"] and tag == "주신" and job.get("value") != rec["job"]:
            job.update(value=rec["job"], tag=tag)
            changed = True
        relation = p["relation"] or rec["relation"] or None
        if changed or relation != p["relation"]:
            conn.execute("UPDATE people SET details = ?, relation = ?, updated_at = ? WHERE id = ?",
                         (json.dumps(details, ensure_ascii=False), relation, db.now(), pid))
            updated += 1
    conn.commit()
    return added, updated


def delete_library_item(conn: sqlite3.Connection, lid: int) -> int:
    """보관함 사연을 지운다. 거기서 옮긴 '확인 전' 경험(원고·전송 기록이 없는 것)도 함께 지우고,
    이미 '실제로 있었던 일'로 확인한 경험은 남긴다(보관함 연결만 끊음). 지운 경험 수."""
    gone = 0
    for e in conn.execute("SELECT id, user_confirmed FROM experiences WHERE from_library_id = ?", (lid,)).fetchall():
        if not e["user_confirmed"] and not experience_in_use(conn, e["id"]):
            conn.execute("DELETE FROM experiences WHERE id = ?", (e["id"],))
            gone += 1
        else:
            conn.execute("UPDATE experiences SET from_library_id = NULL WHERE id = ?", (e["id"],))
    conn.execute("DELETE FROM story_library WHERE id = ?", (lid,))
    conn.commit()
    return gone


def experience_in_use(conn: sqlite3.Connection, eid: int) -> bool:
    """원고·사연 전송·제출 기록이 있는 경험 (지우면 기록이 깨진다)."""
    return any(conn.execute(f"SELECT 1 FROM {t} WHERE experience_id = ? LIMIT 1", (eid,)).fetchone()
               for t in ("drafts", "story_posts", "submissions"))


def delete_unconfirmed_experience(conn: sqlite3.Connection, eid: int) -> bool:
    """'확인 전' 경험을 지운다 (보관함에서 왔으면 보관함 원본도 — 다시 가져오지 않도록). 지웠으면 True."""
    e = conn.execute("SELECT * FROM experiences WHERE id = ?", (eid,)).fetchone()
    if e is None or e["user_confirmed"] or experience_in_use(conn, eid):
        return False
    conn.execute("DELETE FROM experiences WHERE id = ?", (eid,))
    if e["from_library_id"]:
        conn.execute("DELETE FROM story_library WHERE id = ?", (e["from_library_id"],))
    conn.commit()
    return True


def import_library(conn: sqlite3.Connection, rows: list[dict]) -> tuple[int, int]:
    added = updated = 0
    for row in rows:
        person_id = find_person(conn, row["person_label"])
        cols = [k for k in row]
        exists = conn.execute("SELECT id FROM story_library WHERE code = ?", (row["code"],)).fetchone() \
            if row["code"] else None
        if exists:
            conn.execute(f"UPDATE story_library SET {', '.join(f'{k} = ?' for k in cols)}, person_id = ?, updated_at = ? "
                         "WHERE id = ?", (*row.values(), person_id, db.now(), exists["id"]))
            updated += 1
        else:
            conn.execute(f"INSERT INTO story_library ({', '.join(cols)}, person_id, created_at, updated_at) "
                         f"VALUES ({', '.join('?' * len(cols))}, ?, ?, ?)", (*row.values(), person_id, db.now(), db.now()))
            added += 1
    conn.commit()
    return added, updated


# ── 사연 목록 내보내기 ────────────────────────────────────────────────
def export_rows(conn: sqlite3.Connection, include_unconfirmed: bool = False) -> list[dict]:
    """정리된 사연 목록 (실제 경험). 인물은 호칭으로만 적는다 (실명 대신). 보관함에서 온 것은 그 번호를 쓴다."""
    from .quizbot import story as story_mod

    sent = story_mod.sent_broadcasters(conn)
    rows = conn.execute(
        """SELECT e.*, p.alias AS about_alias, p.side AS about_side, l.code AS lib_code, l.category AS lib_category,
                  l.theme AS lib_theme FROM experiences e LEFT JOIN people p ON p.id = e.about_person_id
           LEFT JOIN story_library l ON l.id = e.from_library_id"""
        + ("" if include_unconfirmed else " WHERE e.user_confirmed = 1") + " ORDER BY COALESCE(l.code, ''), e.id").fetchall()
    out = []
    for e in rows:
        about = e["about_alias"] if e["about_alias"] and e["about_side"] != "self" else None
        out.append({
            "id": e["lib_code"] or f"E{e['id']:04d}", "character": about or "나", "title": e["label"] or "",
            "intro": intro_for(about), "event_date": e["when_text"] or "", "event_summary": e["highlight"] or "",
            "broadcast_timing": "", "song": e["song"] or "", "closing": e["ending"] or "", "body": e["story"] or "",
            "category": e["lib_category"] or "", "theme": e["lib_theme"] or "", "gorilla_line": e["gorilla_line"] or "",
            "kind": "real" if e["user_confirmed"] else "", "confirmed": bool(e["user_confirmed"]),
            "sent_to": sorted(sent.get(e["id"], set())),
        })
    return out


def export_jsonl(rows: list[dict]) -> str:
    """다시 가져오기용 (사연 로그 .jsonl 형식, 확인한 것은 kind=real)."""
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def export_text(rows: list[dict], today: str) -> str:
    """읽기용: 인물별로 묶은 사연 목록."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["character"], []).append(r)
    order = sorted(groups, key=lambda k: (k != "나", -len(groups[k]), k))
    confirmed = sum(1 for r in rows if r["confirmed"])
    lines = [f"라디오 사연 목록 — {today} 기준",
             f"실제로 있었던 일로 확인한 사연 {confirmed}편" + (f" · 확인 전 {len(rows) - confirmed}편" if len(rows) > confirmed else ""),
             "인물은 사연에 쓰는 호칭으로 적었습니다.", ""]
    for who in order:
        lines += ["", f"■ {'내 이야기' if who == '나' else who} ({len(groups[who])}편)", ""]
        for r in groups[who]:
            head = f"[{r['id']}] {r['title'] or '(제목 없음)'}" + (f" · {r['event_date']}" if r["event_date"] else "")
            if not r["confirmed"]:
                head += " · 확인 전"
            lines.append(head)
            if r["body"]:
                lines.append(r["body"].strip())
            if r["closing"]:
                lines.append(r["closing"].strip())
            extra = [f"신청곡: {r['song']}" if r["song"] else "", f"공감로그 한 줄: {r['gorilla_line']}" if r["gorilla_line"] else "",
                     f"보낸 곳: {', '.join(r['sent_to'])}" if r["sent_to"] else ""]
            lines += [x for x in extra if x]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def detect(text: str) -> str | None:
    """가져오기 파일 종류: people(인물 관계도 .md) / library(사연 로그 .jsonl) / None(기존 JSON)."""
    head = text.lstrip()[:2000]
    if head.startswith("{") and "\n" in text.strip() and (('"body"' in head and '"intro"' in head)
                                                           or '"type": "person"' in head):
        return "library"
    if head.startswith("#") and ("#### " in text) and ("## 가족 인물" in text or "## 주인공" in text):
        return "people"
    return None


# ── 관계도 그리기 ─────────────────────────────────────────────────────
SIDE_COLOR = {"self": "#2554c7", "family": "#2554c7", "work": "#18794e", "friend": "#c2410c", "life": "#667085"}


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def diagram(rows: list, size: int = 1100) -> dict:
    """방사형 관계도 좌표. 가운데 '나', 안쪽 고리에 묶음(‘외가 · …’는 ‘외가’ 하나로), 바깥 고리에 인물."""
    c = size / 2
    me = next((r for r in rows if r["side"] == "self"), None)
    others = [r for r in rows if r["side"] != "self"]
    groups: dict[str, list] = {}
    for r in others:
        groups.setdefault((r["grp"] or "기타").split(" · ")[0], []).append(r)
    n = max(1, len(others))
    r_group, r_person = size * 0.19, size * 0.30
    nodes, hubs, i = [], [], 0
    for grp, members in groups.items():
        angles = []
        for r in members:
            a = -math.pi / 2 + 2 * math.pi * (i + 0.5) / n
            angles.append(a)
            deg = math.degrees(a)
            left = 90 < (deg % 360) < 270
            alias = r["alias"] if r["alias"] and r["alias"] != r["name"] else ""
            label = _short(r["name"], 12) + (f" ({alias})" if alias else "")
            nodes.append({"id": r["id"], "name": r["name"], "label": label, "alias": r["alias"], "side": r["side"],
                          "color": SIDE_COLOR.get(r["side"], "#667085"),
                          "x": round(c + r_person * math.cos(a), 1), "y": round(c + r_person * math.sin(a), 1),
                          "lx": round(c + (r_person + 14) * math.cos(a), 1), "ly": round(c + (r_person + 14) * math.sin(a), 1),
                          "rot": round(deg + 180 if left else deg, 1), "anchor": "end" if left else "start",
                          "hub": len(hubs)})
            i += 1
        mid = sum(angles) / len(angles)
        deg = math.degrees(mid)
        left = 90 < (deg % 360) < 270
        hubs.append({"name": grp, "label": f"{_short(grp, 12)} · {len(members)}",
                     "x": round(c + r_group * math.cos(mid), 1), "y": round(c + r_group * math.sin(mid), 1),
                     # 묶음 이름은 바퀴살을 따라 가운데 쪽으로 쓴다 (옆 묶음과 겹치지 않게)
                     "lx": round(c + (r_group - 10) * math.cos(mid), 1), "ly": round(c + (r_group - 10) * math.sin(mid), 1),
                     "rot": round(deg + 180 if left else deg, 1), "anchor": "start" if left else "end",
                     "count": len(members), "color": SIDE_COLOR.get(members[0]["side"], "#667085")})
    return {"size": size, "c": c, "me": me, "nodes": nodes, "hubs": hubs}
