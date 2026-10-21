"""전 채널 편성표 — SBS 는 공식 페이지(programs.py), MBC·KBS·CBS·EBS 는 Claude 웹 검색으로 찾아 '프로그램' 목록에 넣는다.

    python -m radio_helper.schedules refresh [--channel "MBC FM4U"]

- 듣는 채널 목록(청취 화면의 채널) 중 SBS 가 아닌 채널마다: Claude 가 웹 검색으로 현재 정규 편성표를 찾고(공식 홈페이지 우선),
  프로그램 이름·진행자·시간·요일을 정리한다. 확인하지 못한 시간대는 비워 두고 지어내지 않는다.
- 이 편성표로 '지금 무슨 프로그램인지'를 알아 퀴즈·사연 기록에 프로그램 이름이 남고, 프로그램마다 분석 횟수 상한이 새로 시작한다.
- 사용자가 고친 프로그램(출처 '사용자 수정')은 덮어쓰지 않는다. 새 편성에서 빠진 것은 '편성 아님'으로 돌린다.
- 일주일마다 저절로 다시 확인한다 (관리 화면이 켜져 있을 때, 개편 반영). SBS 공식 페이지 새로고침도 함께.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta

from . import db

SOURCE = "웹 검색 편성표"
REFRESH_DAYS = 7
MIN_PROGRAMS = 4          # 이보다 적게 찾으면 실패로 보고 기존 편성을 그대로 둔다
SBS_CHANNELS = {"파워FM", "러브FM", "고릴라M"}
# 채널 이름 → 검색할 때 쓰는 설명 (주파수는 수도권 기준)
DESCRIBE = {
    "MBC FM4U": "MBC FM4U (MBC 라디오 FM4U, 수도권 91.9MHz)",
    "MBC 표준FM": "MBC 표준FM (MBC 라디오 표준FM, 수도권 95.9MHz)",
    "KBS 쿨FM": "KBS 쿨FM (KBS 2FM, 수도권 89.1MHz)",
    "KBS 해피FM": "KBS 해피FM (KBS 2라디오, 수도권 106.1MHz)",
    "KBS 1라디오": "KBS 1라디오 (수도권 97.3MHz)",
    "KBS 클래식FM": "KBS 클래식FM (KBS 1FM, 수도권 93.1MHz)",
    "CBS 음악FM": "CBS 음악FM (CBS 기독교방송 음악FM, 수도권 93.9MHz)",
    "CBS 표준FM": "CBS 표준FM (CBS 기독교방송 표준FM, 수도권 98.1MHz)",
    "EBS FM": "EBS FM (EBS 라디오, 수도권 104.5MHz)",
}
HHMM = re.compile(r"^([01]?\d|2[0-4]):([0-5]\d)$")
LOG_PREFIX = "방송사 편성표"           # 웹 검색 편성표 기록 (0.54 까지는 'MBC·KBS 편성표')
LOG_LIKE = f"(message LIKE '{LOG_PREFIX}%' OR message LIKE 'MBC·KBS 편성표%')"   # 예전 기록도 함께


def channels_to_fetch(conn) -> list[str]:
    """듣는 채널 중 SBS 가 아닌 것 (MBC·KBS·CBS·EBS·기타)."""
    from .quizbot import config

    return [c for c in config.channels(conn) if c not in SBS_CHANNELS]


def describe(channel: str) -> str:
    return DESCRIBE.get(channel, f"{channel} (한국 라디오 채널)")


def _hhmm(value: str) -> str | None:
    m = HHMM.match((value or "").strip())
    if not m:
        return None
    h = int(m.group(1)) % 24
    return f"{h:02d}:{m.group(2)}"


def _code(channel: str, title: str, days: str) -> str:
    slug = re.sub(r"\s+", "", f"{channel}:{title}:{days}")
    return slug[:120]


def clean(programs: list[dict]) -> list[dict]:
    """시간이 읽히는 것만, 같은 이름·요일은 하나로."""
    out, seen = [], set()
    for p in programs:
        title = " ".join((p.get("title") or "").split())[:80]
        start, end = _hhmm(p.get("start")), _hhmm(p.get("end"))
        days = " ".join((p.get("days") or "매일").split())[:20] or "매일"
        if not title or not start or not end or start == end:
            continue
        key = (title, days)
        if key in seen:
            continue
        seen.add(key)
        homepage = (p.get("homepage") or "").strip()
        out.append({"title": title, "host": " ".join((p.get("host") or "").split())[:40], "start": start, "end": end,
                    "days": days, "homepage": homepage if homepage.startswith("https://") else ""})
    return out


def save(conn, channel: str, programs: list[dict], as_of: str = "") -> dict:
    """채널 편성 저장. {added, updated, retired}"""
    now = db.now()
    source = f"{SOURCE} {now[:10]}" + (f" ({as_of[:40]})" if as_of else "")
    keep, added, updated = set(), 0, 0
    for p in programs:
        code = _code(channel, p["title"], p["days"])
        keep.add(code)
        row = conn.execute("SELECT * FROM programs WHERE code = ?", (code,)).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO programs (channel, code, title, host, start_time, end_time, days, main_url, on_air, source,
                       checked_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (channel, code, p["title"], p["host"] or None, p["start"], p["end"], p["days"], p["homepage"], source,
                 now, now))
            added += 1
        elif row["source"] != "사용자 수정":
            conn.execute(
                "UPDATE programs SET title = ?, host = ?, start_time = ?, end_time = ?, days = ?, "
                "main_url = CASE WHEN ? != '' THEN ? ELSE main_url END, on_air = 1, source = ?, checked_at = ?, "
                "updated_at = ? WHERE id = ?",
                (p["title"], p["host"] or None, p["start"], p["end"], p["days"], p["homepage"], p["homepage"], source,
                 now, now, row["id"]))
            updated += 1
    retired = 0
    for row in conn.execute("SELECT id, code FROM programs WHERE channel = ? AND on_air = 1 AND source LIKE ?",
                            (channel, SOURCE + "%")).fetchall():
        if row["code"] not in keep:
            conn.execute("UPDATE programs SET on_air = 0, updated_at = ? WHERE id = ?", (now, row["id"]))
            retired += 1
    db.set_setting(conn, f"schedules.checked.{channel}", now)
    conn.commit()
    return {"added": added, "updated": updated, "retired": retired}


def refresh_channel(conn, channel: str, answerer, today: str | None = None) -> str:
    today = today or datetime.now().strftime("%Y-%m-%d")
    research = answerer.research_schedule(describe(channel), today)
    data = answerer.parse_schedule(research)
    programs = clean(data.get("programs") or [])
    if len(programs) < MIN_PROGRAMS:
        db.set_setting(conn, f"schedules.checked.{channel}", db.now())   # 일주일 뒤 다시 (계속 묻지 않게)
        raise ValueError(f"프로그램을 {len(programs)}개밖에 찾지 못해 기존 편성을 그대로 둠")
    done = save(conn, channel, programs, data.get("as_of") or "")
    sources = ", ".join((data.get("sources") or [])[:3])
    return (f"{channel}: 프로그램 {len(programs)}개 (새로 {done['added']} · 바뀜 {done['updated']} · 편성 아님 "
            f"{done['retired']})" + (f" — 근거: {sources}" if sources else ""))


def refresh(conn, answerer=None, channels: list[str] | None = None, say=print) -> list[str]:
    if answerer is None:
        from .quizbot.answerer import make, ready

        ok, why = ready(conn)
        if not ok:
            msg = f"{LOG_PREFIX}를 찾지 못했습니다 — {why}"
            db.log(conn, "programs", msg)
            return [msg]
        answerer = make(conn)
    results = []
    for channel in channels or channels_to_fetch(conn):
        try:
            text = refresh_channel(conn, channel, answerer)
        except Exception as e:   # 한 채널 실패가 전체를 멈추지 않게
            text = f"{channel}: 실패 — {type(e).__name__}: {str(e)[:150]}"
        say(text)
        results.append(text)
    db.log(conn, "programs", f"{LOG_PREFIX} (웹 검색): " + " / ".join(results)[:900])
    return results


def stale_channels(conn, now: datetime) -> list[str]:
    out = []
    for channel in channels_to_fetch(conn):
        raw = db.get_setting(conn, f"schedules.checked.{channel}") or ""
        try:
            if now - datetime.strptime(raw, "%Y-%m-%d %H:%M:%S") < timedelta(days=REFRESH_DAYS):
                continue
        except ValueError:
            pass
        out.append(channel)
    return out


def tick(conn, now: datetime | None = None, launch=None, launch_sbs=None) -> list[str]:
    """관리 화면 서버가 30초마다 부른다: 일주일이 지난 편성표를 뒤에서 다시 가져온다. 한 일 목록."""
    now = now or datetime.now()
    done = []
    if db.is_stopped(conn):
        return done
    started = db.get_setting(conn, "schedules.started_at") or ""
    try:
        if now - datetime.strptime(started, "%Y-%m-%d %H:%M:%S") < timedelta(hours=6):
            return done   # 방금 시작한 것이 아직 도는 중일 수 있음
    except ValueError:
        pass
    if stale_channels(conn, now):
        db.set_setting(conn, "schedules.started_at", now.strftime("%Y-%m-%d %H:%M:%S"))
        if launch is None:
            from .app import launch_module

            launch = lambda args: launch_module("radio_helper.schedules", args, "schedules")   # noqa: E731
        launch(["refresh"])
        done.append("mbc_kbs")
    last = conn.execute("SELECT at FROM events WHERE kind = 'programs' AND message LIKE '공식 페이지 새로고침%' "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    try:
        sbs_old = last is None or now - datetime.strptime(last["at"], "%Y-%m-%d %H:%M:%S") >= timedelta(days=REFRESH_DAYS)
    except ValueError:
        sbs_old = True
    if sbs_old and not db.get_setting(conn, "schedules.sbs_started_on") == now.strftime("%Y-%m-%d"):
        db.set_setting(conn, "schedules.sbs_started_on", now.strftime("%Y-%m-%d"))
        if launch_sbs is None:
            from .app import launch_programs_refresh

            launch_sbs = launch_programs_refresh
        launch_sbs()
        done.append("sbs")
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MBC·KBS·CBS·EBS 편성표 가져오기 (웹 검색)")
    ap.add_argument("action", choices=["refresh"])
    ap.add_argument("--channel", action="append", help="이 채널만 (여러 번 가능)")
    args = ap.parse_args(argv)
    conn = db.connect()
    db.init_db(conn)
    try:
        refresh(conn, channels=args.channel)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
