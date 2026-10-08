"""인물 관계도 · 사연 보관함.

- 화자는 언제나 '나'(보내는 사람)다. 다른 사람이 주인공인 실제 경험은 "제 와이프 이야기인데요"처럼
  관계 호칭으로 시작해 쓴다. 사연과 AI 요청에는 실명 대신 '호칭'만 쓴다.
- 인물 관계도 파일(character-map.md)에서 인물 카드·실제 사건(연표)을 가져온다.
  〔사연〕 태그의 가상 사건은 인물 카드에 넣지 않는다.
- 사연 로그(.jsonl)는 '사연 보관함'에 넣는다. 원본 사연을 각색한 가상 사연(fiction)은 읽기용이며,
  실제로 있었던 일이 아니므로 이 프로그램은 보내지 않는다.
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


def import_library(conn: sqlite3.Connection, rows: list[dict]) -> tuple[int, int]:
    by_name = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM people")}
    by_first = {}
    for name, pid in by_name.items():
        by_first.setdefault(name.split()[0], pid)   # '홍길동 과장님' 은 '홍길동' 으로도 찾는다
    self_ids = [r["id"] for r in conn.execute("SELECT id, name FROM people WHERE side = 'self'")]
    self_names = SELF_NAMES | {r["name"] for r in conn.execute("SELECT name FROM people WHERE side = 'self'")}
    added = updated = 0
    for row in rows:
        label = row["person_label"]
        bare = re.sub(r"\s*\(.*\)$", "", label)
        person_id = (self_ids[0] if self_ids and (label in self_names or bare in self_names) else
                     by_name.get(label) or by_name.get(bare) or by_first.get(bare))
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


def detect(text: str) -> str | None:
    """가져오기 파일 종류: people(인물 관계도 .md) / library(사연 로그 .jsonl) / None(기존 JSON)."""
    head = text.lstrip()[:2000]
    if head.startswith("{") and "\n" in text.strip() and '"body"' in head and '"intro"' in head:
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
