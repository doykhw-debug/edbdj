"""방송국(SBS·MBC·KBS) 상관없이 코너 게시판에 자동으로 글을 올린다.

    python -m radio_helper.boards run --corner 12 [--test | --real]

매번 (코너마다 정한 시각에 하루 한 번, 또는 화면의 '지금 실행'):
1. 코너 화면을 연다 (창 없이, 도우미 전용 브라우저 — 한 번 로그인하면 기억). 글쓰기 칸이 없으면 '글쓰기'를 눌러 찾는다.
2. Claude 가 홈페이지 공지를 매번 다시 읽고 정리한다: 지금 받는지, 무엇을(사연·짧은 한마디·신청곡), 주제, 글자 수,
   AI 글·대필 제한, 마감, 방송 요일, 첨부 필요. 정리한 것은 코너에 적는다 (사람이 '공지 확인'을 누르지 않아도 됨).
3. 받지 않음·AI 글 제한·마감 지남·첨부 필요·방송 요일이 아님 → 올리지 않고 이유를 남긴다.
4. 사용자가 '실제로 있었던 일'로 확인한 경험 중 이 방송국에 아직 안 보낸 것에서 공지에 맞는 것 하나를 고른다.
   사연은 확인한 글 그대로, 짧은 한마디는 그 경험의 말·사실로만 만들고 검사를 통과해야 쓴다. 맞는 것이 없으면 올리지 않는다.
5. 원고를 만들어(검사 경고 0건이 승인을 대신) 글쓰기 화면에 입력 → 시험이면 등록은 누르지 않고 증거 사진만,
   아니면 등록까지 누르고 게시판에서 올라간 글을 확인한다. 로그인·보안 문자는 창을 띄워 사용자에게 맡긴다 (우회하지 않음).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from urllib.parse import urlparse

from . import db, gates, stations

DAYS = "월화수목금토일"
DEFAULT_LIMIT = {"short": 120}
WRITE_LINK = re.compile(r"^\s*(글\s*쓰기|사연\s*(보내기|쓰기|올리기)|글\s*작성|작성하기|참여하기)\s*$")
MAX_LINKS = 40


class BoardSkip(RuntimeError):
    """올리지 않음 (이유는 메시지)."""


def corner_row(conn, cid: int):
    row = conn.execute("SELECT * FROM corners WHERE id = ?", (cid,)).fetchone()
    if row is None:
        raise BoardSkip(f"코너 #{cid} 이(가) 없습니다.")
    return row


def notice_of(corner) -> dict:
    try:
        return json.loads(corner["notice_ai"] or "{}")
    except (ValueError, TypeError):
        return {}


def add_corner(conn, url: str, title: str = "") -> int:
    """코너 주소로 추가 (SBS·MBC·KBS 의 https 주소만). 이미 있으면 그 코너 번호."""
    url = (url or "").strip()
    station = stations.station_of(url)
    if station is None:
        raise ValueError("SBS·MBC·KBS 홈페이지의 https 코너 주소만 추가할 수 있습니다.")
    row = conn.execute("SELECT id FROM corners WHERE board_url = ?", (url,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO corners (program, kind, title, board_url, write_url, station, updated_at) VALUES (?, 'board', ?, ?, ?, ?, ?)",
        (station, title.strip() or f"새 코너 ({station})", url, url, station, db.now()))
    conn.commit()
    return cur.lastrowid


# ── 1. 코너 화면 읽기 ─────────────────────────────────────────────────
STRUCTURE = re.compile(r"^(v\d+|setting|corner|cornerboards?|cornerboardwrite|boards?|bbs|pc|mobile|m)$|\.", re.I)


def program_root(path: str) -> list[str]:
    """주소에서 프로그램 부분: 구조를 뜻하는 칸(v2·setting·corner·board·pc …) 앞까지.
    MBC /broad/radio/fm4u/musicparty/v2/… → broad/radio/fm4u/musicparty, SBS /radio/0chulpowerfm/cornerboards/… →
    radio/0chulpowerfm, KBS /2fm/radio/프로그램/pc/board.html → 2fm/radio/프로그램."""
    parts = [p for p in path.split("/") if p]
    for i, part in enumerate(parts):
        if STRUCTURE.search(part):
            return parts[:i] or parts[:2]
    return parts[:-1] or parts


def same_program_links(base_url: str, links: list[tuple[str, str]]) -> list[dict]:
    """같은 프로그램의 다른 코너로 보이는 주소 (같은 방송국·같은 주소 앞부분·'corner'/'board' 가 든 주소)."""
    base = urlparse(base_url)
    head = program_root(base.path)
    out, seen = [], {base_url.split("#")[0]}
    for href, text in links:
        href = (href or "").split("#")[0]
        u = urlparse(href)
        parts = [p for p in u.path.split("/") if p]
        if href in seen or stations.station_of(href) is None or u.hostname != base.hostname:
            continue
        if parts[:len(head)] != head or not re.search(r"corner|board", u.path + "?" + u.query, re.I):
            continue
        seen.add(href)
        out.append({"url": href, "text": " ".join((text or "").split())[:40]})
        if len(out) >= MAX_LINKS:
            break
    return out


def read_page(corner) -> dict:
    """코너 화면의 글(공지 포함)·같은 프로그램 코너 주소·글쓰기 화면 주소."""
    from playwright.sync_api import sync_playwright

    from . import autofill

    url = corner["board_url"]
    with sync_playwright() as p:
        ctx, page = autofill.launch_profile(p, headless=True)
        try:
            page.goto(url)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            text = autofill.page_text(page)
            try:
                links = page.eval_on_selector_all("a[href]", "els => els.map(a => [a.href, a.innerText || ''])")
            except Exception:
                links = []
            write_url = None
            found = autofill.guess_selectors(page)
            if found["title"] and found["body"]:
                write_url = page.url
            else:   # 글쓰기 칸이 따로 있는 게시판: '글쓰기'를 눌러 본다
                buttons = page.locator("a, button")
                for i in range(min(buttons.count(), 300)):
                    b = buttons.nth(i)
                    try:
                        if WRITE_LINK.match(b.inner_text() or "") and b.is_visible():
                            b.click()
                            page.wait_for_load_state("load", timeout=15000)
                            break
                    except Exception:
                        continue
                if page.url != url and autofill._allowed_real_url(page.url):
                    write_url = page.url
        finally:
            ctx.close()
    return {"text": text, "links": same_program_links(url, links), "write_url": write_url}


# ── 2. 공지 정리 → 코너에 적기 ─────────────────────────────────────────
def save_notice(conn, corner, notice: dict, page: dict) -> None:
    text = (notice.get("notice") or "").strip()
    ai = {**notice, "read_at": db.now()}
    title = corner["title"]
    if (title.startswith("새 코너") or not title.strip()) and notice.get("corner_title"):
        title = notice["corner_title"].strip()[:60]
    program = corner["program"]
    if program in stations.STATIONS and notice.get("program"):
        program = f"{program} {notice['program'].strip()[:40]}"
    conn.execute(
        """UPDATE corners SET notice_text = ?, notice_hash = ?, notice_checked_at = ?, notice_changed = 0,
               notice_ai = ?, recruiting = ?, ai_assist_policy = ?, char_limit = ?, deadline = ?, title = ?,
               program = ?, station = ?, write_url = COALESCE(?, write_url), found_links = ?, updated_at = ?
           WHERE id = ?""",
        (text or None, db.notice_hash(text), db.now(), json.dumps(ai, ensure_ascii=False),
         {"yes": "open", "no": "closed"}.get(notice.get("accepting"), "unknown"),
         "restricted" if notice.get("ai_restricted") else "unknown",
         notice.get("max_chars") or None, notice.get("deadline") or None, title, program,
         stations.corner_station(corner), page.get("write_url"),
         json.dumps(page.get("links") or [], ensure_ascii=False), db.now(), corner["id"]))
    conn.commit()


def skip_reason(notice: dict, corner, now: datetime) -> str | None:
    """공지·요일로 보아 오늘 올리면 안 되는 이유 (없으면 None)."""
    if notice.get("accepting") == "no":
        return "공지상 지금은 글을 받지 않습니다 (마감·종료·쉼)."
    if notice.get("ai_restricted"):
        return "공지가 AI 작성·대필·창작 글을 제한합니다 — 직접 쓴 글만 받는 코너라 자동으로 올리지 않습니다."
    if notice.get("needs_attachment") or notice.get("kind") == "photo":
        return "사진·파일 첨부가 꼭 필요한 코너라 자동으로 올리지 않습니다."
    deadline = notice.get("deadline") or ""
    try:
        if deadline and datetime.strptime(deadline, "%Y-%m-%d").date() < now.date():
            return f"공지의 마감({deadline})이 지났습니다."
    except ValueError:
        pass
    days = corner["auto_days"] if "auto_days" in corner.keys() else ""
    air = notice.get("air_days") or []
    if not days and air:   # 요일 코너: 방송 전날 올린다
        tomorrow = DAYS[(now.weekday() + 1) % 7]
        if tomorrow not in air:
            return f"{'·'.join(air)}요일 코너라 방송 전날에만 올립니다."
    return None


# ── 3. 사연 고르기 → 원고 ───────────────────────────────────────────────
def pick_text(conn, notice: dict, exp, pick: dict) -> tuple[str, str] | None:
    """(제목, 본문). 사연은 확인한 글 그대로, 짧은 한마디는 검사를 통과한 것만. 쓸 수 없으면 None."""
    from .quizbot import story

    kind = notice.get("kind") or "story"
    limit = notice.get("max_chars") or DEFAULT_LIMIT.get(kind, 2000)
    if kind == "short":
        body = (pick.get("line") or "").strip()
        if not body or any(f["level"] in ("block", "warn") for f in story.message_checks(conn, body, exp, "ai", limit=limit)):
            # Claude 가 만든 한마디가 검사에 걸리면 그 경험에 적힌 말 그대로
            body = next((v.strip() for v in ((exp["quotes"] or "").splitlines() + [exp["highlight"] or ""])
                         if v.strip() and len(v.strip()) <= limit), "")
            if not body:
                return None
    else:
        song = (exp["song"] or "").strip() if kind in ("song", "story") else ""
        body = story.approved_text(conn, exp, limit, song=song)
    title = (pick.get("title") or "").strip()
    if not title or len(title) > 40 or any(f["level"] in ("block", "warn") for f in
                                          story.message_checks(conn, title, exp, "ai", limit=40)):
        title = (exp["label"] or "").strip()[:40] or re.split(r"(?<=[.!?。])\s", body)[0][:40]
    return title, body


def choose(conn, corner, notice: dict, answerer) -> tuple[int, str, str, str]:
    """(경험 번호, 제목, 본문, 고른 이유)."""
    from .quizbot import story

    station = stations.corner_station(corner) or "SBS"
    candidates = story.candidate_experiences(conn, station)
    if not candidates:
        raise BoardSkip(f"{station}에 아직 안 보낸 '확인한 경험'이 없습니다.")
    pick = answerer.pick_board_story(notice, candidates[:60])
    eid = pick.get("experience_id") or 0
    if eid not in {c["id"] for c in candidates}:
        raise BoardSkip("공지 주제에 맞는 확인한 경험이 없어 올리지 않습니다"
                        + (f" — {pick['reason']}" if pick.get("reason") else "") + ".")
    exp = conn.execute("SELECT * FROM experiences WHERE id = ?", (eid,)).fetchone()
    made = pick_text(conn, notice, exp, pick)
    if made is None:
        raise BoardSkip(f"경험 #{eid} 로 공지에 맞는 짧은 글을 만들지 못했습니다 (지어낸 내용 없이).")
    return eid, made[0], made[1], pick.get("reason") or ""


def make_draft(conn, corner_id: int, eid: int, title: str, body: str, confirmed_text: bool = True) -> int:
    """자동 승인 원고 (검사 경고 0건이 승인을 대신). 확인한 글 그대로면 사실 확인도 된 것으로 둔다."""
    now = db.now()
    cur = conn.execute(
        "INSERT INTO drafts (experience_id, corner_id, title, body, song, source, fact_confirmed, status, approved_at, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, '', ?, ?, 'approved', ?, ?, ?)",
        (eid, corner_id, title, body, "manual" if confirmed_text else "pasted", int(confirmed_text), now, now, now))
    conn.commit()
    return cur.lastrowid


# ── 실행 ──────────────────────────────────────────────────────────────
def finish(conn, corner_id: int, text: str) -> str:
    conn.execute("UPDATE corners SET auto_result = ?, updated_at = ? WHERE id = ?",
                 (f"{db.now()[5:16]} {text}"[:500], db.now(), corner_id))
    conn.commit()
    db.log(conn, "post", f"코너 #{corner_id} 자동 등록: {text}"[:600])
    return text


def run(conn, cid: int, test: bool | None = None, answerer=None, now: datetime | None = None,
        reader=read_page, poster=None, allow_window: bool | None = None) -> str:
    """공지 읽기 → 판단 → 고르기 → 입력(·등록). 결과 글 (코너의 '마지막 결과'에도 적음)."""
    from . import autofill

    now = now or datetime.now()
    corner = corner_row(conn, cid)
    test = bool(corner["auto_test"]) if test is None else test
    if db.is_stopped(conn):
        return finish(conn, cid, "일괄 중지가 켜져 있어 하지 않음")
    if stations.corner_station(corner) is None:
        return finish(conn, cid, "SBS·MBC·KBS 의 https 코너 주소가 아니라 하지 않음")
    try:
        if answerer is None:
            from .quizbot.answerer import ClaudeAnswerer, get_api_key

            if not get_api_key():
                raise BoardSkip("Claude API 키가 없어 공지를 읽지 못했습니다 (퀴즈 자동 화면에서 저장).")
            answerer = ClaudeAnswerer()
        page = reader(corner)
        notice = answerer.read_notice(stations.corner_station(corner) or "", corner["board_url"], page["text"][:12000])
        save_notice(conn, corner, notice, page)
        corner = corner_row(conn, cid)
        why = skip_reason(notice, corner, now)
        if why:
            raise BoardSkip(why)
        eid, title, body, reason = choose(conn, corner, notice, answerer)
        did = make_draft(conn, cid, eid, title, body, confirmed_text=notice.get("kind") != "short")
        result = gates.evaluate(conn, did, gates.MODE_POST)
        if not result.ok:
            conn.execute("UPDATE drafts SET status = 'archived', updated_at = ? WHERE id = ?", (db.now(), did))
            conn.commit()
            raise BoardSkip("검사에 걸려 올리지 않음: " + " / ".join(result.blockers)[:300])
        draft = conn.execute("SELECT * FROM drafts WHERE id = ?", (did,)).fetchone()
        if allow_window is None:
            allow_window = not autofill.no_window()
        post = poster or autofill.run_post
        with autofill.input_lock("board_post.lock", wait_s=autofill.POST_LOCK_WAIT_SECONDS):
            sub_id = post(conn, draft, corner, allow_window=allow_window, dry_run=test)
        status = conn.execute("SELECT post_status FROM submissions WHERE id = ?", (sub_id,)).fetchone()
        label = {"test": "시험 입력 완료 (등록 안 누름)", "posted": "등록 확인", "unknown": "등록 누름·결과 불명"}.get(
            status[0] if status else "", status[0] if status else "?")
        return finish(conn, cid, f"{label} — 경험 #{eid} '{title}' (원고 #{did}, 제출 이력 #{sub_id})"
                                 + (f" · 고른 이유: {reason}" if reason else ""))
    except BoardSkip as e:
        return finish(conn, cid, f"올리지 않음 — {e}")
    except autofill.AutofillError as e:
        return finish(conn, cid, f"입력 중단 — {e}")
    except Exception as e:   # 브라우저·API 오류 등
        return finish(conn, cid, f"오류 — {type(e).__name__}: {str(e)[:200]}")


def due(corner, now: datetime) -> bool:
    if not corner["auto_on"] or (corner["auto_last_on"] or "") == now.strftime("%Y-%m-%d"):
        return False
    if corner["auto_days"] and DAYS[now.weekday()] not in corner["auto_days"]:
        return False
    return (now.hour, now.minute) >= _time(corner)


def tick(conn, now: datetime | None = None, launch=None) -> list[int]:
    """관리 화면 서버가 30초마다 부른다: 시각이 된 코너를 하나씩 따로 실행한다 (하루 한 번)."""
    now = now or datetime.now()
    if db.is_stopped(conn):
        return []
    started = []
    for corner in conn.execute("SELECT * FROM corners WHERE auto_on = 1 ORDER BY id").fetchall():
        if not due(corner, now):
            continue
        conn.execute("UPDATE corners SET auto_last_on = ? WHERE id = ?", (now.strftime("%Y-%m-%d"), corner["id"]))
        conn.commit()
        if launch is None:
            from .app import launch_module

            launch = lambda args: launch_module("radio_helper.boards", args, "boards")   # noqa: E731
        log_name = launch(["run", "--corner", str(corner["id"])])
        db.log(conn, "post", f"{corner['title']} 매일 자동 등록 시작 ({'시험' if corner['auto_test'] else '등록'}, 기록: {log_name})")
        started.append(corner["id"])
    return started


def _time(corner) -> tuple[int, int]:
    try:
        hh, mm = (int(x) for x in (corner["auto_time"] or "10:30").split(":"))
        return hh, mm
    except ValueError:
        return 10, 30


def next_run(corner, now: datetime) -> str:
    """화면 표시용: 다음 자동 실행 (꺼져 있으면 빈 글)."""
    if not corner["auto_on"]:
        return ""
    hh, mm = _time(corner)
    for add in range(8):
        day = now + timedelta(days=add)
        if corner["auto_days"] and DAYS[day.weekday()] not in corner["auto_days"]:
            continue
        if add == 0 and (corner["auto_last_on"] or "") == now.strftime("%Y-%m-%d"):
            continue
        if add == 0 and (now.hour, now.minute) >= (hh, mm):
            return "곧 (1분 안)"
        return f"{day:%m/%d}({DAYS[day.weekday()]}) {hh:02d}:{mm:02d}"
    return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="코너 게시판 자동 등록 (SBS·MBC·KBS)")
    ap.add_argument("action", choices=["run"])
    ap.add_argument("--corner", type=int, required=True)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--test", action="store_true", help="입력만 하고 등록은 누르지 않음")
    mode.add_argument("--real", action="store_true", help="등록까지")
    args = ap.parse_args(argv)
    conn = db.connect()
    db.init_db(conn)
    try:
        text = run(conn, args.corner, test=True if args.test else False if args.real else None)
    finally:
        conn.close()
    print(text, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
