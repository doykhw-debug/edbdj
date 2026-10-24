"""방송국(SBS·MBC·KBS·CBS) 상관없이 코너 게시판에 자동으로 글을 올린다.

    python -m radio_helper.boards run --corner 12 [--test | --real] [--scheduled]
    python -m radio_helper.boards login          (미리 로그인: 도우미 브라우저 창에서 방송국마다 직접 로그인)
    python -m radio_helper.boards login-check    (방송국마다 로그인이 살아 있는지 창 없이 확인)

매일 게시판 확인 (0.60, 코너 화면 맨 위): 모든 코너를 하루 한 번 하나씩 확인해, 아직 올린 적 없는 게시판이거나
그때와 공지가 달라졌으면(새 공지) 맞는 사연을 길게(채팅용 한 줄이 아닌 본문) 올린다. 같은 공지에는 다시 올리지 않는다.
하루 상한(기본 5건)과 시험(입력만·증거 사진) 모드가 있다. 정해진 시각의 자동 실행은 창을 띄우지 않으므로, 로그인이
풀렸으면 기다리지 않고 '로그인 필요'로 남긴다 (미리 로그인 버튼으로 다시 로그인).

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
import os
import re
import sys
import time
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from urllib.parse import urlparse

from . import aside_records, db, gates, stations
from .checks import CLOSING, GREETING

DAYS = "월화수목금토일"
DEFAULT_LIMIT = {"short": 120}
WRITE_LINK = re.compile(r"^\s*(글\s*쓰기|사연\s*(보내기|쓰기|올리기)|글\s*작성|작성하기|참여하기)\s*$")
MAX_LINKS = 40
BOARD_LOCK = "board_post.lock"   # 게시판 작업(공지 읽기·입력·로그인 확인)은 하나씩 — 도우미 브라우저 프로필 하나를 같이 씀
DAILY_KEY, DAILY_TIME_KEY, DAILY_TEST_KEY, DAILY_CAP_KEY = "boards.daily", "boards.daily_time", "boards.daily_test", \
    "boards.daily_cap"
DAILY_DEFAULTS = {DAILY_KEY: "0", DAILY_TIME_KEY: "10:30", DAILY_TEST_KEY: "1", DAILY_CAP_KEY: "5"}
DAILY_ENABLED = "meta.boards_daily_060"
LOGIN_KEY = "boards.logins"
LAUNCHED_KEY = "boards.launched_at"
LAUNCH_GAP_SECONDS = 60          # 하나 띄운 뒤 잠금을 잡을 때까지 다음 것을 띄우지 않는다
SAME_NOTICE = 0.8                # 공지 원문이 이만큼 비슷하면 같은 공지 (Claude 가 옮겨 적은 글이 조금씩 달라도)
SAME_THEME = 0.6                 # 원문이 없을 때 주제 한 문장으로 견줌
HOMES = {"SBS": "https://www.sbs.co.kr", "MBC": "https://www.imbc.com", "KBS": "https://www.kbs.co.kr",
         "CBS": "https://www.cbs.co.kr/radio"}
LOGIN_WAIT_MINUTES = 20


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
    """코너 주소로 추가 (SBS·MBC·KBS·CBS 의 https 주소만). 이미 있으면 그 코너 번호."""
    url = (url or "").strip()
    station = stations.station_of(url)
    if station is None:
        raise ValueError("SBS·MBC·KBS·CBS 홈페이지의 https 코너 주소만 추가할 수 있습니다.")
    if stations.is_post_url(url):
        raise ValueError("게시판 안의 글 하나(다른 사용자가 쓴 글) 주소입니다 — 게시판 목록 주소를 넣어 주세요.")
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
        if href in seen or stations.station_of(href) is None or u.hostname != base.hostname or stations.is_post_url(href):
            continue
        if parts[:len(head)] != head or not re.search(r"corner|board", u.path + "?" + u.query, re.I):
            continue
        seen.add(href)
        out.append({"url": href, "text": " ".join((text or "").split())[:40]})
        if len(out) >= MAX_LINKS:
            break
    return out


RULES_LINK = re.compile(r"^[^\w가-힣]*(게시판\s*)?(운영\s*지침|이용\s*(안내|규칙|수칙)|게시판\s*안내)\s*$")


def read_rules(page, autofill) -> str:
    """'게시판 운영지침' 같은 단추를 눌러 뜨는 안내(창 또는 다른 화면)의 글. CBS '사연과 신청곡' 게시판에 있음.
    다른 화면으로 넘어가면 읽고 돌아온다. 없으면 빈 글."""
    try:
        buttons = page.locator("a, button")
        for i in range(min(buttons.count(), 200)):
            b = buttons.nth(i)
            try:
                if not (RULES_LINK.match(b.inner_text() or "") and b.is_visible()):
                    continue
                before_url, before = page.url, autofill.page_text(page)
                b.click()
                page.wait_for_timeout(1200)
                after = autofill.page_text(page)
                if page.url != before_url:
                    page.go_back()
                    page.wait_for_load_state("load", timeout=15000)
                    return after[:3000]
                added = after.replace(before, "").strip() if after.startswith(before[:200]) else after
                try:
                    page.keyboard.press("Escape")       # 안내 창 닫기
                except Exception:
                    pass
                return added[:3000]
            except Exception:
                continue
    except Exception:
        pass
    return ""


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
            rules = read_rules(page, autofill)
            if rules:
                text = f"{text}\n\n[게시판 운영지침]\n{rules}"
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


HOLD_PREFIX = "[보류] "                 # 이 이유면 Aside 규칙에서 '조건 보류'로 적어 자동으로 다시 시도하지 않음


def skip_reason(notice: dict, corner, now: datetime, ai_assist_ok: bool = False, strict: bool = False) -> str | None:
    """공지·요일로 보아 오늘 올리면 안 되는 이유 (없으면 None). ai_assist_ok: 'AI 글 제한' 공지도 올림 (사용자 설정).
    strict: Aside 규칙의 자동 실행 — 공지에 '지금 모집한다'는 말이 있어야 올림 (게시판·글쓰기 버튼만으로는 모집 중이 아님)."""
    if notice.get("accepting") == "no":
        return "공지상 지금은 글을 받지 않습니다 (마감·종료·쉼)."
    entry = notice.get("entry") or ""
    if entry == "comment":
        return HOLD_PREFIX + "공지 글에 댓글로 참여하는 코너라 게시판 글쓰기로 올리지 않습니다 (댓글 글자 수 제한이 따로 있음)."
    if entry == "external":
        return HOLD_PREFIX + "메일·외부 신청서·문자·전화로 받는 코너라 게시판에 올리지 않습니다."
    if strict and notice.get("accepting") != "yes":
        return "공지에서 지금 모집한다는 말을 확인하지 못해 올리지 않습니다 (Aside 규칙: 모집 확인 후에만)."
    if notice.get("ai_restricted") and not ai_assist_ok:
        return "공지가 AI 작성·대필·창작 글을 제한합니다 — 직접 쓴 글만 받는 코너라 자동으로 올리지 않습니다."
    if notice.get("needs_attachment") or notice.get("kind") == "photo":
        return HOLD_PREFIX + "사진·파일 첨부가 꼭 필요한 코너라 자동으로 올리지 않습니다."
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
    if notice.get("max_chars") and notice.get("limit_unit") == "bytes":   # 바이트 제한: 한글 한 글자를 3바이트로 (넉넉히)
        limit = max(20, notice["max_chars"] // 3)
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
        body = letter(story.approved_text(conn, exp, limit, song=song, board=True), limit)
    title = (pick.get("title") or "").strip()
    if not title or len(title) > 40 or any(f["level"] in ("block", "warn") for f in
                                          story.message_checks(conn, title, exp, "ai", limit=40)):
        title = (exp["label"] or "").strip()[:40] or re.split(r"(?<=[.!?。])\s", body)[0][:40]
    return title, body


def letter(body: str, limit: int) -> str:
    """게시판 글 모양: 인사 — 본문 — 맺음 (글자 수 안에 들 때만, 사실은 덧붙이지 않음)."""
    full = f"{GREETING}\n\n{body}\n\n{CLOSING}"
    return full if len(full) <= limit else body


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
        reader=read_page, poster=None, allow_window: bool | None = None, scheduled: bool = False,
        trigger: str | None = None) -> str:
    """공지 읽기 → 판단 → 고르기 → 입력(·등록). 결과 글 (코너의 '마지막 결과'에도 적음).
    scheduled: 정해진 시각의 자동 실행 — 창을 띄우지 않고(로그인이 풀렸으면 기다리지 않음) 하루 상한을 지킨다.
    trigger: scheduled / onair(방송 중 게시판 멘트) / manual(사용자가 '지금 실행') — Aside 규칙 판단에 씀."""
    from . import autofill

    now = now or datetime.now()
    corner = corner_row(conn, cid)
    if test is None:
        test = bool(corner["auto_test"]) if corner["auto_on"] else daily(conn)["test"]
    if db.is_stopped(conn):
        return finish(conn, cid, "일괄 중지가 켜져 있어 하지 않음")
    if stations.corner_station(corner) is None:
        return finish(conn, cid, "SBS·MBC·KBS·CBS 의 https 코너 주소가 아니라 하지 않음")
    if scheduled:
        allow_window = False
    trigger = trigger or ("scheduled" if scheduled else "manual")
    try:
        if reader is read_page and poster is None and tabs_on(conn):   # 0.64: 내 Aside 새 탭 (로그인 그대로)
            from . import tabbridge

            reader, poster = tabbridge.read_page, tabbridge.post
        with autofill.input_lock(BOARD_LOCK, wait_s=autofill.POST_LOCK_WAIT_SECONDS):
            return _run(conn, cid, test, answerer, now, reader, poster, allow_window, scheduled, trigger)
    except autofill.AutofillError as e:     # 다른 게시판 작업이 오래 끝나지 않음
        return finish(conn, cid, f"입력 중단 — {e}")


def close_corner(conn, cid: int, why: str) -> None:
    """다시 열지 않게 '게시판 없음'으로 돌린다 (매일 확인·지금 방송·로그인 확인 모두 board_exists = 1 만 엶)."""
    conn.execute("UPDATE corners SET board_exists = 0, dev_note = TRIM(COALESCE(dev_note, '') || ' · ' || ?), "
                 "updated_at = ? WHERE id = ?", (f"{db.now()[:10]} {why} — 다시 열지 않음", db.now(), cid))
    conn.commit()
    db.log(conn, "post", f"코너 #{cid}: {why} — 다시 열지 않습니다")


def _run(conn, cid: int, test: bool, answerer, now: datetime, reader, poster, allow_window, scheduled: bool,
         trigger: str = "manual") -> str:
    from . import autofill

    corner = corner_row(conn, cid)
    rules = aside_records.rules_on(conn)
    # 모집한다는 말이 있어야 올림: 매일 자동, '게시판 멘트 때만' 방송 중 (방송 중이면 바로는 '안 받음'만 거름)
    strict = rules and (trigger == "scheduled" or (trigger == "onair" and aside_records.onair_mode(conn) == "mention"))
    one_a_day = rules and trigger in ("scheduled", "onair")
    try:
        blocked = aside_records.block_reason(conn, corner, trigger)
        if blocked:
            raise BoardSkip(blocked)
        if scheduled:
            cap = daily(conn)["cap"]
            if cap and done_today(conn, now) >= cap:                  # 0 = 제한 없음
                raise BoardSkip(f"오늘 자동 게시판 글 {cap}건을 채워 내일 다시 확인합니다")
        if answerer is None:
            from .quizbot.answerer import make, ready

            ok, why = ready(conn)
            if not ok:
                raise BoardSkip(f"공지를 읽지 못했습니다 — {why}")
            answerer = make(conn)
        if stations.is_post_url(corner["board_url"]):
            close_corner(conn, cid, "게시판이 아닌 글 하나(다른 사용자가 쓴 글) 주소")
            raise BoardSkip("게시판이 아닌 글 하나(다른 사용자가 쓴 글) 주소라 열지 않습니다 — 이 코너는 다시 열지 않습니다")
        page = reader(corner)
        if stations.denied_page(page.get("text") or "") and not page.get("write_url"):
            close_corner(conn, cid, "권한이 없는 화면")
            raise BoardSkip("권한이 없는 화면(다른 사람의 글·비밀글·삭제된 글)이라 올리지 않습니다 — 이 코너는 다시 열지 않습니다")
        notice = answerer.read_notice(stations.corner_station(corner) or "", corner["board_url"], page["text"][:12000])
        save_notice(conn, corner, notice, page)
        corner = corner_row(conn, cid)
        why = skip_reason(notice, corner, now, gates.ai_ok(conn), strict=strict) or \
            already_posted(conn, corner, notice, test, now=now if one_a_day else None,
                           daily_only=trigger == "onair")
        if why:
            if why.startswith(HOLD_PREFIX) and aside_records.rules_on(conn):   # 막힌 후보는 반복 시도하지 않음
                conn.execute("UPDATE corners SET hold_state = 'hold', hold_reason = ? WHERE id = ?",
                             (why[len(HOLD_PREFIX):][:300], cid))
                conn.commit()
            raise BoardSkip(why.removeprefix(HOLD_PREFIX))
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
        sub_id = post(conn, draft, corner, allow_window=allow_window, dry_run=test)   # (게시판 잠금은 run 이 잡고 있음)
        status = conn.execute("SELECT post_status FROM submissions WHERE id = ?", (sub_id,)).fetchone()
        if status and status[0] in ("test", "posted", "unknown"):
            record_posted(conn, cid, notice, test=status[0] == "test", at=db.now())
        label = {"test": "시험 입력 완료 (등록 안 누름)", "posted": "등록 확인", "unknown": "등록 누름·결과 불명"}.get(
            status[0] if status else "", status[0] if status else "?")
        return finish(conn, cid, f"{label} — 경험 #{eid} '{title}' (원고 #{did}, 제출 이력 #{sub_id})"
                                 + (f" · 고른 이유: {reason}" if reason else ""))
    except BoardSkip as e:
        return finish(conn, cid, f"올리지 않음 — {e}")
    except autofill.AutofillError as e:
        if "로그인" in str(e):
            mark_login(conn, stations.corner_station(corner) or "", False,
                       "자동 등록 중 로그인이 풀린 것을 발견 — '미리 로그인'으로 다시 로그인하세요")
        return finish(conn, cid, f"입력 중단 — {e}")
    except Exception as e:   # 브라우저·API 오류 등
        return finish(conn, cid, f"오류 — {type(e).__name__}: {str(e)[:200]}")


def due(corner, now: datetime, d: dict | None = None) -> bool:
    """오늘 실행할 때가 됐는지: 코너마다 정한 '매일 자동'은 그 시각·요일, 아니면 '매일 게시판 확인'의 시각."""
    if (corner["auto_last_on"] or "") == now.strftime("%Y-%m-%d"):
        return False
    if corner["auto_on"]:
        if corner["auto_days"] and DAYS[now.weekday()] not in corner["auto_days"]:
            return False
        return (now.hour, now.minute) >= _time(corner)
    if d and d["on"]:
        return (now.hour, now.minute) >= _hhmm(d["time"])
    return False


def _hhmm(text: str) -> tuple[int, int]:
    try:
        hh, mm = (int(x) for x in (text or "10:30").split(":"))
        return hh, mm
    except ValueError:
        return 10, 30


def board_busy() -> bool:
    """게시판 작업이 지금 돌고 있는지 (잠금 파일의 주인이 살아 있음)."""
    from . import autofill

    path = db.data_dir() / BOARD_LOCK
    try:
        return path.exists() and not autofill.lock_owner_gone(path) and \
            time.time() - path.stat().st_mtime < autofill.LOCK_STALE_SECONDS
    except OSError:
        return False


def tick(conn, now: datetime | None = None, launch=None) -> list[int]:
    """관리 화면 서버가 30초마다 부른다: 시각이 된 코너를 하나 띄운다 (하루 한 번). 게시판 작업은 같은 브라우저 프로필을
    쓰므로 하나가 끝나야 다음 것을 띄운다 (0.60 — 예전에는 같은 시각의 코너를 한꺼번에 띄워 프로필이 겹쳤음)."""
    now = now or datetime.now()
    if db.is_stopped(conn) or board_busy():
        return []
    try:
        last = float(db.get_setting(conn, LAUNCHED_KEY) or 0)
    except ValueError:
        last = 0.0
    if 0 <= now.timestamp() - last < LAUNCH_GAP_SECONDS:
        return []
    d = daily(conn)
    rows = conn.execute("SELECT * FROM corners WHERE auto_on = 1 OR (? = 1 AND board_exists = 1) "
                        "ORDER BY auto_on DESC, is_target DESC, (posted_notice IS NOT NULL), id", (int(d["on"]),)).fetchall()
    for corner in rows:
        if stations.corner_station(corner) is None or not due(corner, now, d) or \
                aside_records.block_reason(conn, corner, "scheduled"):
            continue
        conn.execute("UPDATE corners SET auto_last_on = ? WHERE id = ?", (now.strftime("%Y-%m-%d"), corner["id"]))
        conn.commit()
        if launch is None:
            from .app import launch_module

            launch = lambda args: launch_module("radio_helper.boards", args, "boards")   # noqa: E731
        log_name = launch(["run", "--corner", str(corner["id"]), "--scheduled"])
        db.set_setting(conn, LAUNCHED_KEY, str(now.timestamp()))
        test = corner["auto_test"] if corner["auto_on"] else d["test"]
        db.log(conn, "post", f"{corner['title']} 매일 {'자동 등록' if corner['auto_on'] else '게시판 확인'} 시작 "
                             f"({'시험' if test else '등록'}, 기록: {log_name})")
        return [corner["id"]]
    return []


# ── 매일 게시판 확인 설정 · 이미 올린 공지 ──────────────────────────────
# ── 지금 방송 중인 프로그램의 게시판 (0.63) ─────────────────────────────
ONAIR_KEY = "boards.onair"            # 1 = 지금 방송 프로그램의 게시판에 쓴다 (Aside 창을 띄워)
ONAIR_STATUS = "boards.onair_status"  # 지금 방송 게시판 쓰기가 지금 무엇을 하는지 (화면 표시)
ONAIR_ON_071 = "meta.onair_on_071"    # 0.71: 사용자 요청으로 한 번 켬
MENTION_MINUTES = 10                  # Aside 규칙: 최근 이만큼의 방송 녹취에서 게시판 참여 멘트를 찾는다
ONAIR_DONE = "boards.onair_done"      # 오늘 한 프로그램들 {"날짜": ["채널|프로그램", …]}


def _flat(text: str) -> str:
    return re.sub(r"[\s·,.:!?'\"()\[\]<>~-]+", "", text or "").lower()


def onair_corners(conn, channel: str, program: str) -> list:
    """그 프로그램의 게시판 (코너의 프로그램 이름이 같거나 한쪽이 다른 쪽을 품음 + 같은 방송국)."""
    from .quizbot import config

    want, station = _flat(program), config.broadcaster_of(channel)
    if len(want) < 3:
        return []
    out = []
    for c in conn.execute("SELECT * FROM corners WHERE board_exists = 1 ORDER BY is_target DESC, id").fetchall():
        if stations.corner_station(c) != station:
            continue
        have = _flat(c["program"])
        if have and (have == want or (len(have) >= 4 and (have in want or want in have))):
            out.append(c)
    return out


def onair_now(conn, now: datetime) -> list[dict]:
    """듣는 채널마다 지금 방송 중인 프로그램 [{channel, program}] (편성표에 있는 것만)."""
    from .quizbot import lanes, live

    out = []
    for x in lanes.all_lanes(conn):
        program = live.current_program(conn, x["channel"], now)
        if program and not any(o["channel"] == x["channel"] for o in out):
            out.append({"channel": x["channel"], "program": program})
    return out


def _done_today(conn, now: datetime) -> set[str]:
    try:
        data = json.loads(db.get_setting(conn, ONAIR_DONE) or "{}")
    except ValueError:
        data = {}
    return set(data.get(now.strftime("%Y-%m-%d"), []))


def recent_mention(conn, channel: str, now: datetime) -> str | None:
    """이 채널의 최근 방송 녹취에서 게시판 참여 멘트 (Aside 규칙: 멘트가 나와야 그 프로그램 게시판을 확인)."""
    since = (now - timedelta(minutes=MENTION_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    for r in conn.execute("SELECT text FROM transcripts WHERE channel = ? AND at >= ? ORDER BY id DESC LIMIT 60",
                          (channel, since)):
        found = aside_records.board_mention(r["text"])
        if found:
            return found
    return None


def onair_tick(conn, now: datetime | None = None, launch=None) -> list[str]:
    """관리 화면 서버가 30초마다 부른다: 편성표로 지금 방송 중인 프로그램마다 하루 한 번, 그 게시판 쓰기를 띄운다
    (0.71: 듣기를 켜지 않아도 듣는 채널 설정으로). '게시판 멘트 때만'을 고르면 듣는 중 방송에서 참여 멘트가 나왔을 때만.
    무엇을 하는지·왜 안 하는지는 ONAIR_STATUS 에 적어 화면에 보인다. 게시판 작업은 같은 브라우저 프로필을 쓰므로 하나씩."""
    from .quizbot import lanes, live

    now = now or datetime.now()

    def status(text: str) -> list:
        if db.get_setting(conn, ONAIR_STATUS) != text:
            db.set_setting(conn, ONAIR_STATUS, text)
        return []

    if db.get_setting(conn, ONAIR_KEY, "1") != "1":
        return status("꺼져 있음 — 위 '지금 방송 프로그램 게시판에 쓰기'를 켜세요")
    if db.is_stopped(conn):
        return status("일괄 중지가 켜져 있음")
    mode = aside_records.onair_mode(conn) if aside_records.rules_on(conn) else "live"
    if mode == "mention" and not live.is_active(conn):
        return status("'게시판 멘트 때만'이라 듣는 중일 때만 합니다 — 지금은 듣지 않음")
    if board_busy():
        return []
    try:
        last = float(db.get_setting(conn, LAUNCHED_KEY) or 0)
    except ValueError:
        last = 0.0
    if 0 <= now.timestamp() - last < LAUNCH_GAP_SECONDS:
        return []
    done = _done_today(conn, now)
    current = onair_now(conn, now)
    if not current:
        return status("편성표에 지금 방송 중인 프로그램이 없음 (채널: "
                      + ", ".join(x["channel"] for x in lanes.all_lanes(conn) if x["channel"]) + ")")
    waiting = []
    for x in current:
        key = f"{x['channel']}|{x['program']}"
        if key in done:
            continue
        mention = recent_mention(conn, x["channel"], now) if mode == "mention" else None
        if mode == "mention" and not mention:
            waiting.append(x["program"])
            continue
        db.set_setting(conn, ONAIR_DONE, json.dumps({now.strftime("%Y-%m-%d"): sorted(done | {key})}, ensure_ascii=False))
        if launch is None:
            from .app import launch_module

            launch = lambda args: launch_module("radio_helper.boards", args, "boards")   # noqa: E731
        log_name = launch(["onair", "--channel", x["channel"], "--program", x["program"]]
                          + (["--mention", mention] if mention else []))
        db.set_setting(conn, LAUNCHED_KEY, str(now.timestamp()))
        db.log(conn, "post", f"지금 방송 '{x['program']}'({x['channel']}) 게시판 쓰기 시작"
                             + (f" · 들은 멘트: “{mention[:80]}”" if mention else "") + f" (기록: {log_name})")
        status(f"{now:%H:%M} '{x['program']}' 게시판 쓰기 시작 (결과는 아래 '최근 게시판 작업')")
        return [key]
    if waiting:
        return status(f"'{', '.join(waiting)}' — 방송에서 게시판 참여 멘트를 기다리는 중")
    return status("지금 방송 프로그램은 오늘 이미 했음: " + ", ".join(x["program"] for x in current))


def run_onair(conn, channel: str, program: str, runner=None, collector=None, say=print) -> list[str]:
    """지금 방송 프로그램의 게시판에 하나씩 쓴다 (공지 읽기·판단·입력·등록은 run 과 같음, 시험 모드면 입력만).
    게시판을 모르면 그 프로그램 홈페이지 메뉴에서 먼저 모은다 (Aside 규칙이면 모으기는 사용자가 누를 때만).
    Aside 규칙: 상시·이벤트·중복 주소·이미 올린 곳·결과 불명·보류 게시판은 열지 않는다."""
    rules = aside_records.rules_on(conn)
    corners = onair_corners(conn, channel, program)
    if not corners and not rules:
        row = conn.execute("SELECT main_url FROM programs WHERE channel = ? AND title = ? AND on_air = 1",
                           (channel, program)).fetchone()
        if row and stations.station_of(row["main_url"] or ""):
            from . import collect

            (collector or collect.collect_page)(conn, {"channel": channel, "program": program, "url": row["main_url"]},
                                                say=say)
            corners = onair_corners(conn, channel, program)
    skipped = [c for c in corners if aside_records.block_reason(conn, c, "onair")]
    corners = [c for c in corners if c not in skipped]
    if not corners:
        msg = (f"지금 방송 '{program}'({channel}): " +
               (f"게시판 {len(skipped)}곳 모두 Aside 규칙으로 열지 않습니다 (상시·이벤트·중복·이미 올림·결과 불명·보류)"
                if skipped else "아는 게시판이 없습니다 — 코너 화면 '코너 게시판 지금 모으기' 뒤 다시 합니다"))
        db.log(conn, "post", msg)
        db.set_setting(conn, ONAIR_STATUS, f"{datetime.now():%H:%M} {msg}")
        say(msg)
        return []
    results = []
    for c in corners:
        text = (runner or run)(conn, c["id"], allow_window=True, trigger="onair")
        results.append(f"{c['title']}: {text}")
        say(results[-1])
    db.log(conn, "post", f"지금 방송 '{program}'({channel}) 게시판 {len(corners)}곳: " + " / ".join(results)[:700])
    db.set_setting(conn, ONAIR_STATUS, f"{datetime.now():%H:%M} '{program}' 게시판 {len(corners)}곳 끝 — " + " / ".join(results)[:300])
    return results


def daily(conn) -> dict:
    get = lambda k: db.get_setting(conn, k, DAILY_DEFAULTS[k])   # noqa: E731
    try:
        cap = max(0, int(get(DAILY_CAP_KEY)))
    except ValueError:
        cap = 5
    at = get(DAILY_TIME_KEY)
    return {"on": get(DAILY_KEY) == "1", "time": at if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at or "") else "10:30",
            "test": get(DAILY_TEST_KEY) != "0", "cap": cap}


def enable_daily_once(conn) -> bool:
    """0.60: 매일 게시판 확인을 한 번 켠다 (시험 모드로 시작 — 증거 사진을 확인한 뒤 '등록까지'로 바꿈)."""
    if db.get_setting(conn, DAILY_ENABLED) == "1":
        return False
    db.set_setting(conn, DAILY_ENABLED, "1")
    if db.get_setting(conn, DAILY_KEY, None) is not None:
        return False
    db.set_setting(conn, DAILY_KEY, "1")
    db.log(conn, "post", "매일 게시판 확인을 켰습니다 (시험 모드: 입력하고 증거 사진만, 등록은 누르지 않음) — 코너 화면 맨 위")
    return True


def done_today(conn, now: datetime) -> int:
    """오늘 자동으로 입력·등록한 게시판 글 수 (시험 입력 포함)."""
    return conn.execute("SELECT COUNT(*) FROM submissions WHERE created_at >= ? AND note LIKE '게시판 자동 등록%'",
                        (now.strftime("%Y-%m-%d") + " 00:00:00",)).fetchone()[0]


def _norm(text) -> str:
    return " ".join(str(text or "").split())


def same_notice(old: dict, new: dict) -> bool:
    """그때 공지와 지금 공지가 같은지. 원문이 둘 다 있으면 원문이 비슷한지, 없으면 주제가 비슷한지, 마감이 바뀌면 새 공지."""
    if old.get("deadline") and new.get("deadline") and old["deadline"] != new["deadline"]:
        return False
    a, b = _norm(old.get("notice")), _norm(new.get("notice"))
    if a and b:
        return SequenceMatcher(None, a, b).ratio() >= SAME_NOTICE
    ta, tb = _norm(old.get("theme")), _norm(new.get("theme"))
    if ta and tb:
        return SequenceMatcher(None, ta, tb).ratio() >= SAME_THEME
    return True


def record_posted(conn, cid: int, notice: dict, test: bool, at: str | None = None) -> None:
    keep = {k: notice.get(k) or "" for k in ("notice", "theme", "deadline")}
    conn.execute("UPDATE corners SET posted_notice = ? WHERE id = ?",
                 (json.dumps({**keep, "at": at or db.now(), "test": bool(test)}, ensure_ascii=False), cid))
    conn.commit()


def already_posted(conn, corner, notice: dict, test: bool, now: datetime | None = None,
                   daily_only: bool = False) -> str | None:
    """이 게시판에 이미 올렸고 공지가 그때와 같으면 그 이유 (올리지 않음). 올린 적 없거나 새 공지면 None.
    시험 입력만 한 공지는 '등록까지' 모드에서 다시 (실제로) 올린다.
    now: Aside 규칙의 자동 실행 — 공지가 바뀌어도 같은 날(같은 방송)에는 한 게시판에 한 번만.
    daily_only: 지금 방송 중 (0.71) — 공지가 그대로여도 다른 날 방송이면 (다른 경험으로) 다시 쓴다."""
    try:
        saved = json.loads(corner["posted_notice"] or "null") if "posted_notice" in corner.keys() else None
    except ValueError:
        saved = None
    if not isinstance(saved, dict):
        row = conn.execute("SELECT created_at FROM submissions WHERE corner_id = ? AND post_status IN ('posted', 'unknown') "
                           "ORDER BY id DESC LIMIT 1", (corner["id"],)).fetchone()
        if row is None:
            return None
        if daily_only and now is not None:
            return "오늘 이미 이 게시판에 올렸습니다 — 같은 방송에는 한 번만" \
                if row["created_at"][:10] == now.strftime("%Y-%m-%d") else None
        # 예전 버전에서 올린 게시판: 그때 공지를 몰라 지금 공지를 그때 것으로 보고, 공지가 바뀌면 올린다
        record_posted(conn, corner["id"], notice, test=False, at=row["created_at"])
        return f"이미 {row['created_at'][:10]}에 올린 게시판입니다 — 새 공지가 올라오면 다시 올립니다"
    if saved.get("test") and not test:
        return None
    if now is not None and not saved.get("test") and (saved.get("at") or "")[:10] == now.strftime("%Y-%m-%d"):
        return "오늘 이미 이 게시판에 올렸습니다 — 같은 방송에는 한 번만 (Aside 규칙)"
    if daily_only and now is not None:
        return None
    if same_notice(saved, notice):
        done = "시험 입력했습니다" if saved.get("test") else "올렸습니다"
        return f"{(saved.get('at') or '')[:10]}에 지금 공지로 이미 {done} — 새 공지가 올라오면 다시 올립니다"
    return None


# ── 미리 로그인 · 로그인 확인 ─────────────────────────────────────────
def login_targets(conn) -> list[dict]:
    """방송국마다 로그인을 확인할 곳: 그 방송국 코너의 글쓰기 화면(로그인해야 열림), 없으면 게시판 화면."""
    from . import autofill

    out: dict[str, dict] = {}
    for c in conn.execute("SELECT * FROM corners WHERE board_exists = 1 "
                          "ORDER BY (write_url IS NULL), auto_on DESC, is_target DESC, id").fetchall():
        st = stations.corner_station(c)
        url = c["write_url"] or c["board_url"]
        if st and st not in out and url and autofill._allowed_real_url(url):
            out[st] = {"station": st, "url": url, "write": bool(c["write_url"]), "home": HOMES.get(st, "")}
    return [out[s] for s in stations.STATIONS if s in out]


NICK = re.compile(r"(?:^|\s)[^\s]{2,20}\s?님(?=\s|$)", re.M)


def login_state(page) -> tuple[bool | None, str]:
    """(로그인됨?, 근거). 글쓰기 칸이 보이면 됨, 비밀번호 칸이 보이면 안 됨, 그 밖에는 '로그아웃'·'로그인' 글자로."""
    from . import autofill

    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    found = autofill.guess_selectors(page)
    if found["title"] and found["body"]:
        return True, "글쓰기 화면이 열림"
    try:
        if page.locator("input[type=password]").count():
            return False, "로그인 화면이 뜸"
    except Exception:
        pass
    text = autofill.page_text(page)
    if "로그아웃" in text:
        return True, "'로그아웃'이 보임"
    head = text[:300]
    nick = NICK.search(head)
    if nick and "로그인" not in head:            # CBS: 위쪽 메뉴에 '연서해피아빠 님' (로그아웃은 펼침 메뉴 안)
        return True, f"'{nick.group(0).strip()}'이 보임"
    if "로그인" in text:
        return False, "'로그인'만 보임"
    return None, "확인 못 함"


def mark_login(conn, station: str, ok: bool | None, detail: str) -> None:
    if not station:
        return
    try:
        saved = json.loads(db.get_setting(conn, LOGIN_KEY) or "{}")
    except ValueError:
        saved = {}
    saved[station] = {"ok": ok, "detail": detail, "at": db.now()}
    db.set_setting(conn, LOGIN_KEY, json.dumps(saved, ensure_ascii=False))


def logins(conn) -> dict:
    try:
        data = json.loads(db.get_setting(conn, LOGIN_KEY) or "{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _check(conn, ctx, targets, say) -> dict:
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    for t in targets:
        try:
            page.goto(t["url"])
            ok, detail = login_state(page)
        except Exception as e:
            ok, detail = None, f"열지 못함 ({type(e).__name__})"
        mark_login(conn, t["station"], ok, detail)
        say(f"{t['station']}: {'로그인됨' if ok else '로그인 필요' if ok is False else '확인 못 함'} — {detail}")
    return logins(conn)


def tabs_on(conn) -> bool:
    try:
        from . import tabbridge

        return tabbridge.use_tabs(conn)
    except Exception:
        return False


def check_logins_tabs(conn, targets: list[dict], say=print, reader=None) -> dict:
    """내 Aside 새 탭으로 방송국마다 글쓰기(없으면 게시판) 화면을 열어 로그인 여부를 본다."""
    from . import tabbridge

    for t in targets:
        try:
            got = (reader or _tab_read)(conn, t["url"])
            ok = tabbridge.login_from_text(got.get("text") or "", bool(got.get("password")))
            why = {True: "내 Aside 에서 로그인됨", False: "내 Aside 에서 로그인 화면", None: "판단 못 함"}[ok]
            if got.get("write_url"):
                ok, why = True, "내 Aside 에서 글쓰기 화면이 열림"
        except Exception as e:
            ok, why = None, f"확인 못 함 — {str(e)[:80]}"
        mark_login(conn, t["station"], ok, why)
        say(f"{t['station']}: {why}")
    return logins(conn)


def _tab_read(conn, url: str) -> dict:
    from . import tabbridge

    tid = tabbridge.add_task(conn, "read", url)
    tabbridge.open_tab(url)
    got = tabbridge.wait(conn, tid, tabbridge.READ_WAIT)
    if got.get("status") != "done":
        raise RuntimeError(tabbridge._why(got, "화면을 읽지 못함"))
    return got


def check_logins(conn, say=print) -> dict:
    """방송국마다 창 없이 열어 로그인이 살아 있는지 확인해 적어 둔다."""
    from playwright.sync_api import sync_playwright

    from . import autofill

    targets = login_targets(conn)
    if not targets:
        say("확인할 코너가 없습니다.")
        return logins(conn)
    if tabs_on(conn):
        with autofill.input_lock(BOARD_LOCK, wait_s=autofill.POST_LOCK_WAIT_SECONDS):
            return check_logins_tabs(conn, targets, say)
    with autofill.input_lock(BOARD_LOCK, wait_s=autofill.POST_LOCK_WAIT_SECONDS):
        with sync_playwright() as p:
            ctx, _page = autofill.launch_profile(p, headless=True)
            try:
                return _check(conn, ctx, targets, say)
            finally:
                ctx.close()


def open_login(conn, say=print, minutes: int = LOGIN_WAIT_MINUTES) -> dict:
    """미리 로그인: 도우미 브라우저 창에 방송국마다 탭을 열어 두고, 사용자가 직접 로그인한 뒤 창을 닫으면 확인한다.
    로그인 정보는 이 프로그램이 읽지 않고 도우미 브라우저 프로필에만 남는다 (다음부터 창 없이 등록)."""
    from playwright.sync_api import sync_playwright

    from . import autofill

    targets = login_targets(conn)
    if not targets:
        say("코너가 없어 열 곳이 없습니다. 먼저 코너를 추가하세요.")
        return logins(conn)
    if tabs_on(conn):                       # 내 Aside 에 방송국마다 새 탭 (이미 로그인돼 있으면 그대로)
        from . import tabbridge

        for t in targets:
            tabbridge.open_tab(t["home"] or t["url"])
        say(f"내 Aside 에 새 탭({', '.join(t['station'] for t in targets)})을 열었습니다. 로그인돼 있지 않은 곳만 로그인하세요.")
        return logins(conn)
    with autofill.input_lock(BOARD_LOCK, wait_s=autofill.POST_LOCK_WAIT_SECONDS):
        with sync_playwright() as p:
            ctx, page = autofill.launch_profile(p, headless=False)
            for i, t in enumerate(targets):
                tab = page if i == 0 else ctx.new_page()
                try:
                    tab.goto(t["url"])
                except Exception:
                    pass
            say(f"뜬 브라우저 창의 탭({', '.join(t['station'] for t in targets)})마다 직접 로그인하고, 다 되면 창을 닫으세요 "
                f"({minutes}분 기다림).")
            deadline = time.time() + minutes * 60
            while time.time() < deadline:
                try:
                    if not ctx.pages:
                        break
                except Exception:
                    break
                time.sleep(1)
            try:
                ctx.close()
            except Exception:
                pass
            ctx, _page = autofill.launch_profile(p, headless=True)
            try:
                return _check(conn, ctx, targets, say)
            finally:
                ctx.close()


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
    ap = argparse.ArgumentParser(description="코너 게시판 자동 등록 (SBS·MBC·KBS·CBS)")
    ap.add_argument("action", choices=["run", "login", "login-check", "onair"])
    ap.add_argument("--corner", type=int)
    ap.add_argument("--channel", help="onair: 듣는 채널")
    ap.add_argument("--program", help="onair: 지금 방송 프로그램")
    ap.add_argument("--mention", help="onair: 방송에서 들은 게시판 참여 멘트 (기록용)")
    ap.add_argument("--scheduled", action="store_true", help="정해진 시각의 자동 실행 (창 없이, 하루 상한)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--test", action="store_true", help="입력만 하고 등록은 누르지 않음")
    mode.add_argument("--real", action="store_true", help="등록까지")
    args = ap.parse_args(argv)
    conn = db.connect()
    db.init_db(conn)
    try:
        if args.action == "login":
            open_login(conn)
            return 0
        if args.action == "login-check":
            check_logins(conn)
            return 0
        if args.action == "onair":
            from . import autofill

            os.environ[autofill.SHOW_ENV] = "1"          # Aside 창을 띄워 보이게 (게시판마다 run 이 잠금을 잡음)
            if args.mention:
                print(f"들은 게시판 멘트: {args.mention}", flush=True)
            run_onair(conn, args.channel or "", args.program or "", say=lambda m: print(m, flush=True))
            return 0
        if args.corner is None:
            ap.error("run 에는 --corner 가 필요합니다.")
        text = run(conn, args.corner, test=True if args.test else False if args.real else None, scheduled=args.scheduled)
    finally:
        conn.close()
    print(text, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
