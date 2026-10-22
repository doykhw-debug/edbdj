"""코너 게시판 모으기 (0.62): 프로그램 홈페이지 메뉴에서 청취자 사연·신청곡·코너 게시판 주소를 모아 '코너'에 넣는다.

    python -m radio_helper.collect run

- 사용자 PC 에서 도우미 브라우저(창 없이)로 실제 홈페이지를 열어, 메뉴에 있는 같은 프로그램의 게시판 주소만 넣는다
  (주소를 지어내지 않음). 공지·다시듣기·선곡표·포토·당첨자 같은 게시판은 뺀다.
- 시작점: 프로그램 목록(편성표)의 공식 홈페이지 주소 + 이미 있는 코너 화면에서 찾아 둔 같은 프로그램 코너.
- 일주일마다 저절로 다시 모은다 (개편·새 코너 반영). 이미 있는 주소는 그대로 둔다.
- 넣은 코너는 '매일 게시판 확인'이 하나씩 공지를 읽고, 받지 않는 곳은 건너뛴다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta

from . import db, stations
from urllib.parse import urlparse

from .boards import BOARD_LOCK, board_busy

SKIP_TEXT = re.compile(r"공지|다시\s*듣기|선곡|포토|사진|갤러리|당첨|VOD|보이는\s*라디오|FAQ|자주\s*묻는|제작진|출연자|"
                       r"notice|replay|photo|gallery|winner", re.I)
MAX_PER_PAGE = 15
REFRESH_DAYS = 7
LAST_KEY = "collect.last_run"
STARTED_KEY = "collect.started_at"


BOARD_URL = re.compile(r"board|bbs|corner", re.I)
BOARD_TEXT = re.compile(r"사연|신청|코너|게시판|참여|청취자|한마디|퀴즈|고민|이야기")
GENERIC = {"program", "programs", "home", "main", "radio", "broad", "index", "index.html", "main.html", "pc", "m", "mobile",
           "fm", "fm4u", "2fm", "1radio", "2radio", "sfm", "lovefm", "powerfm", "v2", "wiz"}


def program_tokens(home: str) -> list[str]:
    """홈페이지 주소에서 프로그램을 가리키는 칸 (예: cbs_P000221 · morningfm · 0chulpowerfm)."""
    parts = [p for p in urlparse(home).path.split("/") if p]
    return [p for p in parts if p.lower() not in GENERIC and "." not in p and len(p) >= 4]


def board_links(base_url: str, links: list[tuple[str, str]]) -> list[dict]:
    """홈페이지 링크 중 그 프로그램의 청취자 게시판 (순수 계산). 같은 방송국 주소 + 게시판 모양 주소 +
    (주소에 프로그램 칸이 있거나, 링크 글자가 사연·신청곡·코너 같은 말). 공지·다시듣기·선곡표·포토 등은 뺀다.
    MBC 처럼 게시판이 다른 주소(m.imbc.com)에 있어도 같은 방송국이면 받는다."""
    station = stations.station_of(base_url)
    tokens = [t.lower() for t in program_tokens(base_url)]
    out, seen = [], {base_url.split("#")[0]}
    for href, text in links:
        href = (href or "").split("#")[0].strip()
        text = " ".join((text or "").split())[:40]
        if not href or href in seen or stations.station_of(href) != station or station is None:
            continue
        u = urlparse(href)
        where = (u.path + "?" + u.query).lower()
        if not BOARD_URL.search(where) or SKIP_TEXT.search(text) or SKIP_TEXT.search(where.rsplit("/", 1)[-1]):
            continue
        if not (any(t in where for t in tokens) or BOARD_TEXT.search(text)):
            continue
        seen.add(href)
        out.append({"url": href, "text": text})
        if len(out) >= MAX_PER_PAGE:
            break
    return out


def start_pages(conn) -> list[dict]:
    """방문할 프로그램 홈페이지 [{channel, program, url}] (허용된 방송국 https 주소만, 같은 주소는 한 번)."""
    out, seen = [], set()
    for p in conn.execute("SELECT channel, title, main_url FROM programs WHERE on_air = 1 ORDER BY channel, start_time"):
        url = (p["main_url"] or "").strip()
        if url in seen or stations.station_of(url) is None:
            continue
        seen.add(url)
        out.append({"channel": p["channel"], "program": p["title"], "url": url})
    return out


def read_links(url: str) -> list[tuple[str, str]]:
    """도우미 브라우저(창 없이)로 열어 본 화면의 링크 [(주소, 글자)]."""
    from playwright.sync_api import sync_playwright

    from . import autofill

    with sync_playwright() as p:
        ctx, page = autofill.launch_profile(p, headless=True)
        try:
            page.goto(url)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            return page.eval_on_selector_all("a[href]", "els => els.map(a => [a.href, a.innerText || ''])")
        finally:
            ctx.close()


def add(conn, url: str, title: str, program: str, note: str) -> bool:
    station = stations.station_of(url)
    if station is None:
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO corners (program, kind, title, board_url, write_url, dev_note, station, updated_at) "
        "VALUES (?, 'board', ?, ?, ?, ?, ?, ?)",
        (program or station, (title or "코너 게시판")[:60], url, db.corner_write_url(url) or url, note, station, db.now()))
    return cur.rowcount > 0


def collect(conn, reader=read_links, say=print, now: datetime | None = None) -> dict:
    """모으기 한 번. {pages, failed, added, programs}"""
    now = now or datetime.now()
    stamp = now.strftime("%Y-%m-%d")
    result = {"pages": 0, "failed": 0, "added": 0, "programs": []}
    for start in start_pages(conn):
        try:
            links = reader(start["url"])
        except Exception as e:
            result["failed"] += 1
            say(f"{start['program']}: 홈페이지를 열지 못함 — {type(e).__name__}: {str(e)[:80]}")
            continue
        result["pages"] += 1
        n = 0
        for x in board_links(start["url"], links):
            if add(conn, x["url"], x["text"], start["program"],
                   f"코너 게시판 모으기 ({start['channel']} {start['program']} 홈페이지 메뉴, {stamp})"):
                n += 1
        conn.commit()
        if n:
            result["added"] += n
            result["programs"].append(f"{start['program']} {n}")
        say(f"{start['program']}: 게시판 {n}개 새로")
    # 이미 있는 코너 화면에서 찾아 둔 같은 프로그램 코너 (공지를 읽을 때 모아 둔 것)
    for c in conn.execute("SELECT program, found_links FROM corners WHERE COALESCE(found_links, '') != ''").fetchall():
        try:
            found = json.loads(c["found_links"])
        except ValueError:
            continue
        for x in found:
            text = x.get("text") or ""
            if SKIP_TEXT.search(text):
                continue
            if add(conn, x.get("url") or "", text, c["program"], f"코너 게시판 모으기 (같은 프로그램 코너 화면, {stamp})"):
                result["added"] += 1
    conn.commit()
    db.set_setting(conn, LAST_KEY, now.strftime("%Y-%m-%d %H:%M:%S"))
    db.log(conn, "post", f"코너 게시판 모으기: 홈페이지 {result['pages']}곳 확인 · 새 게시판 {result['added']}개"
                         + (f" ({', '.join(result['programs'][:8])})" if result["programs"] else "")
                         + (f" · 열지 못함 {result['failed']}곳" if result["failed"] else ""))
    return result


def _older(raw: str, now: datetime, delta: timedelta) -> bool:
    try:
        return now - datetime.strptime(raw, "%Y-%m-%d %H:%M:%S") >= delta
    except ValueError:
        return True


def tick(conn, now: datetime | None = None, launch=None) -> bool:
    """관리 화면 서버가 30초마다 부른다: 일주일이 지났으면 뒤에서 모으기를 띄운다 (게시판 작업이 없을 때)."""
    now = now or datetime.now()
    if db.is_stopped(conn) or board_busy() or not start_pages(conn):
        return False
    if not _older(db.get_setting(conn, LAST_KEY), now, timedelta(days=REFRESH_DAYS)):
        return False
    if not _older(db.get_setting(conn, STARTED_KEY), now, timedelta(hours=6)):
        return False                       # 방금 띄운 것이 아직 도는 중일 수 있음
    db.set_setting(conn, STARTED_KEY, now.strftime("%Y-%m-%d %H:%M:%S"))
    if launch is None:
        from .app import launch_module

        launch = lambda args: launch_module("radio_helper.collect", args, "collect")   # noqa: E731
    launch(["run"])
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="코너 게시판 모으기 (프로그램 홈페이지 메뉴)")
    ap.add_argument("action", choices=["run"])
    ap.parse_args(argv)
    from . import autofill

    conn = db.connect()
    db.init_db(conn)
    try:
        with autofill.input_lock(BOARD_LOCK, wait_s=autofill.POST_LOCK_WAIT_SECONDS):   # 게시판 작업과 같은 브라우저 프로필
            collect(conn, say=lambda m: print(m, flush=True))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
