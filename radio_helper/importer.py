"""특징(기본 정보)·실제 경험 가져오기.

JSON 형식:
{
  "profile": {"nickname": "...", "region": "...", ...},          # db.PROFILE_KEYS 중 필요한 것만
  "experiences": [{"label": "...", "story": "...", ...}, ...]     # story 는 꼭 있어야 함
}

- 가져온 경험은 '실제로 있었던 일' 확인이 꺼진 상태로 들어간다. 사용자가 읽고 체크해야 쓰인다.
- 같은 내용(있었던 일)의 경험은 다시 넣지 않는다.
- 기본 정보는 기본적으로 비어 있는 칸만 채운다 (덮어쓰기를 고르면 바꾼다).
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from . import db

EXPERIENCE_KEYS = ["label", "when_text", "people", "story", "quotes", "quote_kind", "highlight", "ending",
                   "fixed_facts", "hide", "song", "prior_history", "gorilla_line"]
MAX_LEN = 4000


class ImportError_(ValueError):
    pass


@dataclass
class ImportReport:
    profile_set: list[str] = field(default_factory=list)
    profile_kept: list[str] = field(default_factory=list)
    added: int = 0
    skipped_duplicates: int = 0
    skipped_invalid: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"기본 정보 {len(self.profile_set)}칸 채움"]
        if self.profile_kept:
            parts.append(f"이미 적힌 {len(self.profile_kept)}칸은 그대로 둠")
        parts.append(f"경험 {self.added}건 추가")
        if self.skipped_duplicates:
            parts.append(f"같은 경험 {self.skipped_duplicates}건 건너뜀")
        if self.skipped_invalid:
            parts.append(f"'있었던 일'이 비어 {self.skipped_invalid}건 건너뜀")
        return ", ".join(parts)


def parse(text: str) -> dict:
    text = (text or "").lstrip("﻿").strip()
    # 채팅에서 복사하면 ```json … ``` 으로 감싸져 올 수 있다
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if m:
        text = m.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ImportError_(f"JSON 형식이 아닙니다 ({e.lineno}번째 줄 근처). 받은 파일 내용을 그대로 붙여 넣으세요.")
    if not isinstance(data, dict) or not ({"profile", "experiences"} & set(data)):
        raise ImportError_("'profile' 또는 'experiences' 항목이 없습니다.")
    return data


def _clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = "\n".join(str(v) for v in value)
    return str(value).strip()[:MAX_LEN]


def _norm(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", text or "")


def import_data(conn: sqlite3.Connection, data: dict, overwrite_profile: bool = False) -> ImportReport:
    report = ImportReport()
    profile = data.get("profile") or {}
    if not isinstance(profile, dict):
        raise ImportError_("'profile' 은 항목: 값 형태여야 합니다.")
    current = db.get_profile(conn)
    for key in db.PROFILE_KEYS:
        if key not in profile:
            continue
        value = _clean(profile[key])
        if not value:
            continue
        if current.get(key) and not overwrite_profile:
            report.profile_kept.append(key)
            continue
        db.set_setting(conn, f"profile.{key}", value)
        report.profile_set.append(key)
    unknown = [k for k in profile if k not in db.PROFILE_KEYS]
    if unknown:
        report.notes.append("모르는 기본 정보 항목은 건너뜀: " + ", ".join(unknown[:10]))

    experiences = data.get("experiences") or []
    if not isinstance(experiences, list):
        raise ImportError_("'experiences' 는 목록이어야 합니다.")
    existing = {_norm(r["story"]) for r in conn.execute("SELECT story FROM experiences")}
    for item in experiences:
        if not isinstance(item, dict):
            report.skipped_invalid += 1
            continue
        values = {k: _clean(item.get(k)) for k in EXPERIENCE_KEYS}
        if not values["story"]:
            report.skipped_invalid += 1
            continue
        if _norm(values["story"]) in existing:
            report.skipped_duplicates += 1
            continue
        values["quote_kind"] = values["quote_kind"] if values["quote_kind"] in ("exact", "gist") else (
            "gist" if values["quotes"] else "none")
        if len(values["gorilla_line"]) > 200:
            report.notes.append(f"'{values['label'] or values['story'][:15]}'의 공감로그 한 줄이 200자를 넘어 잘랐습니다.")
            values["gorilla_line"] = values["gorilla_line"][:200]
        conn.execute(
            f"INSERT INTO experiences ({', '.join(EXPERIENCE_KEYS)}, user_confirmed, created_at, updated_at) "
            f"VALUES ({', '.join('?' * len(EXPERIENCE_KEYS))}, 0, ?, ?)",
            (*(values[k] or None if k not in ("story", "quote_kind") else values[k] for k in EXPERIENCE_KEYS),
             db.now(), db.now()))
        existing.add(_norm(values["story"]))
        report.added += 1
    conn.commit()
    db.log(conn, "import", "가져오기: " + report.summary())
    return report


TEMPLATE = {
    "profile": {
        "nickname": "방송에서 불릴 이름 또는 별명",
        "region": "공개 가능한 지역 (예: 경기 용인)",
        "job_label": "공개 가능한 직업 표현 (예: 제약 영업)",
        "family_aliases": "가족 호칭 (예: 아내 → 옆지기, 아이 → 첫째)",
        "avoid_topics": "피하고 싶은 소재",
        "tone": "유쾌함 / 담백함 / 따뜻함 중 하나",
        "song_pref": "좋아하는 노래·가수 (선택)",
    },
    "experiences": [
        {
            "label": "목록에서 알아볼 짧은 이름",
            "when_text": "언제 (대략)",
            "people": "등장인물 (실명 없이)",
            "story": "실제로 있었던 일을 편하게",
            "quotes": "기억나는 말 (없으면 비워 두기)",
            "quote_kind": "exact(정확히 이 말) / gist(취지만 기억)",
            "highlight": "웃기거나 속상하거나 놀랐던 점",
            "ending": "실제 결말",
            "gorilla_line": "공감로그에 그대로 보내도 되는 한 줄 (직접 쓴 문장, 200자 이내, 선택)",
        }
    ],
}
