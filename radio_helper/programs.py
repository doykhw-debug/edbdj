"""파워FM 프로그램 목록 끌어오기.

사용자 PC에서 공식 페이지를 읽기만 한다 (로그인·입력 없음).
- 등록된 프로그램마다 공식 메인 페이지를 열어 방송 시간·요일과 게시판 링크를 읽는다.
- SBS 라디오 첫 화면에서 프로그램 링크를 모아, 목록에 없는 프로그램은 '후보'로 남긴다.
  채널(파워FM/러브FM)은 링크만으로 알 수 없으므로 사용자가 확인해 추가한다.

실행: python -m radio_helper.programs refresh
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Callable, Iterable
from urllib.parse import urljoin, urlparse

from . import db, seed

RADIO_HOME_URL = "https://www.sbs.co.kr/radio"
PROGRAM_HOSTS = {"programs.sbs.co.kr", "m.programs.sbs.co.kr"}

# fetch(url) -> (본문 텍스트, [(링크 주소, 링크 글자), ...])
Fetcher = Callable[[str], tuple[str, list[tuple[str, str]]]]

_RANGE_HHMM = re.compile(r"(\d{1,2}):(\d{2})\s*[~∼〜\-–]\s*(\d{1,2}):(\d{2})")
_AMPM = r"(오전|오후|낮|밤|저녁|새벽|아침)"
_RANGE_KO = re.compile(
    _AMPM + r"?\s*(\d{1,2})시\s*(?:(\d{1,2})분|(반))?\s*[~∼〜\-–]\s*"
    + _AMPM + r"?\s*(\d{1,2})시\s*(?:(\d{1,2})분|(반))?")
_DAYS = re.compile(r"(매일|평일|주말|월\s*[~∼\-–]\s*[금토일]|[월화수목금토일](?:\s*[,·]\s*[월화수목금토일])+|"
                   r"(?:토|일)요일|월요일\s*[~∼\-–]\s*[금토일]요일)")
_PROGRAM_PATH = re.compile(r"^/radio/([A-Za-z0-9_]+)/")
_BOARD_PATH = re.compile(r"^/radio/([A-Za-z0-9_]+)/(cornerboards|boards)/(\d+)")

_PM_WORDS = ("오후", "밤", "저녁")


@dataclass
class ProgramPageInfo:
    start: str | None = None
    end: str | None = None
    days: str | None = None
    boards: list[tuple[str, str, str]] = field(default_factory=list)  # (kind, title, url)


def _hhmm(h: int, m: int) -> str | None:
    if 0 <= h <= 24 and 0 <= m < 60:
        return f"{h % 24:02d}:{m:02d}"
    return None


def _ko_hour(ampm: str | None, hour: int) -> int:
    if ampm in _PM_WORDS and hour < 12:
        return hour + 12
    if ampm == "낮" and hour < 7:
        return hour + 12
    if ampm in ("새벽", "오전", "아침") and hour == 12:
        return 0
    return hour


def _ko_end_hour(a1: str | None, a2: str | None, start_h: int, h2: int) -> int:
    if a2:
        return _ko_hour(a2, h2)
    if a1 in _PM_WORDS or a1 == "낮":
        if h2 == 12:
            return 0  # 오후 10시~12시 → 자정
        return h2 + 12 if h2 + 12 > start_h else h2  # 밤 11시~1시 → 01시
    return h2 + 12 if h2 <= start_h and h2 + 12 <= 24 else h2  # 오전 11시~1시 → 13시


def parse_time_range(text: str) -> tuple[str | None, str | None, str | None]:
    """본문에서 방송 시간 범위와 요일을 찾는다. 바로 앞에 요일 표현이 붙은 범위를 우선한다."""
    candidates = []
    for m in _RANGE_HHMM.finditer(text):
        start, end = _hhmm(int(m.group(1)), int(m.group(2))), _hhmm(int(m.group(3)), int(m.group(4)))
        if start and end and start != end:
            candidates.append((m.start(), start, end))
    for m in _RANGE_KO.finditer(text):
        a1, h1, m1, half1, a2, h2, m2, half2 = m.groups()
        start_h = _ko_hour(a1, int(h1))
        end_h = _ko_end_hour(a1, a2, start_h, int(h2))
        start = _hhmm(start_h, 30 if half1 else int(m1 or 0))
        end = _hhmm(end_h, 30 if half2 else int(m2 or 0))
        if start and end and start != end:
            candidates.append((m.start(), start, end))
    candidates.sort()
    for pos, start, end in candidates:
        days = _DAYS.findall(text[max(0, pos - 20):pos])
        if days:
            return start, end, re.sub(r"\s+", "", days[-1])
    if candidates:
        return candidates[0][1], candidates[0][2], None
    return None, None, None


def parse_program_page(code: str, text: str, links: Iterable[tuple[str, str]], base_url: str) -> ProgramPageInfo:
    start, end, days = parse_time_range(text)
    info = ProgramPageInfo(start=start, end=end, days=days)
    seen = set()
    for href, label in links:
        url = urljoin(base_url, href)
        u = urlparse(url)
        if u.hostname not in PROGRAM_HOSTS:
            continue
        m = _BOARD_PATH.match(u.path)
        if not m or m.group(1) != code:
            continue
        label = " ".join((label or "").split())
        if not label or len(label) > 60:
            continue
        kind = "코너" if m.group(2) == "cornerboards" else "게시판"
        clean = f"https://programs.sbs.co.kr{u.path}" + (f"?{u.query}" if u.query else "")
        if clean in seen:
            continue
        seen.add(clean)
        info.boards.append((kind, label, clean))
    return info


def discover_programs(links: Iterable[tuple[str, str]], base_url: str = RADIO_HOME_URL) -> dict[str, str]:
    """SBS 라디오 첫 화면 링크에서 프로그램 코드와 이름을 모은다. {code: title}"""
    found: dict[str, str] = {}
    for href, label in links:
        u = urlparse(urljoin(base_url, href))
        if u.hostname not in PROGRAM_HOSTS:
            continue
        m = _PROGRAM_PATH.match(u.path)
        label = " ".join((label or "").split())
        if not m or not label or len(label) > 40:
            continue
        found.setdefault(m.group(1), label)
    return found


@dataclass
class RefreshReport:
    checked: int = 0
    time_changes: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    new_boards: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"프로그램 {self.checked}개 확인"]
        if self.time_changes:
            parts.append("시간 변경 " + ", ".join(self.time_changes))
        if self.unreadable:
            parts.append("시간을 읽지 못함 " + ", ".join(self.unreadable))
        parts.append(f"새 게시판 {len(self.new_boards)}개")
        if self.candidates:
            parts.append("새 프로그램 후보 " + ", ".join(self.candidates))
        if self.errors:
            parts.append("오류 " + "; ".join(self.errors))
        return " / ".join(parts)


def refresh(conn: sqlite3.Connection, fetch: Fetcher, discover: bool = True) -> RefreshReport:
    report = RefreshReport()
    programs = conn.execute("SELECT * FROM programs WHERE on_air = 1 ORDER BY start_time").fetchall()
    for p in programs:
        try:
            text, links = fetch(p["main_url"])
        except Exception as e:  # 한 페이지 실패가 전체를 멈추지 않게 한다
            report.errors.append(f"{p['title']}: {type(e).__name__}")
            continue
        report.checked += 1
        info = parse_program_page(p["code"], text, links, p["main_url"])
        if info.start and info.end:
            if (info.start, info.end) != (p["start_time"], p["end_time"]):
                report.time_changes.append(f"{p['title']} {p['start_time']}~{p['end_time']} → {info.start}~{info.end}")
            conn.execute(
                "UPDATE programs SET start_time = ?, end_time = ?, days = COALESCE(?, days), source = ?, "
                "checked_at = ?, updated_at = ? WHERE id = ?",
                (info.start, info.end, info.days, "공식 페이지", db.now(), db.now(), p["id"]))
        else:
            report.unreadable.append(p["title"])
            conn.execute("UPDATE programs SET checked_at = ?, updated_at = ? WHERE id = ?", (db.now(), db.now(), p["id"]))
        for kind, label, url in info.boards:
            cur = conn.execute(
                """INSERT OR IGNORE INTO corners (program, kind, title, board_url, dev_note, is_target, updated_at)
                   VALUES (?, ?, ?, ?, ?, 0, ?)""",
                (p["title"], kind, label, url, "공식 페이지에서 자동 수집. 모집 여부·형식 미확인", db.now()))
            if cur.rowcount:
                report.new_boards.append(f"{p['title']} · {label}")
        conn.commit()

    if discover:
        try:
            _text, links = fetch(RADIO_HOME_URL)
            known = {r["code"] for r in conn.execute("SELECT code FROM programs")}
            for code, title in discover_programs(links).items():
                if code in known:
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO programs (channel, code, title, main_url, on_air, source, checked_at, updated_at)
                       VALUES ('미확인', ?, ?, ?, 0, 'SBS 라디오 첫 화면에서 발견', ?, ?)""",
                    (code, title, seed.program_main_url(code), db.now(), db.now()))
                report.candidates.append(title)
            conn.commit()
        except Exception as e:
            report.errors.append(f"라디오 첫 화면: {type(e).__name__}")
    db.log(conn, "programs", "공식 페이지 새로고침: " + report.summary())
    return report


def playwright_fetcher(headless: bool = True):
    """Playwright 로 페이지를 렌더링해 본문과 링크를 읽는다. (fetch, close) 를 돌려준다."""
    from playwright.sync_api import sync_playwright

    from .autofill import launch_browser

    pw = sync_playwright().start()
    browser = launch_browser(pw, headless)
    page = browser.new_page()

    def fetch(url: str):
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1500)
        text = page.inner_text("body")
        links = page.eval_on_selector_all(
            "a[href]", "els => els.map(e => [e.getAttribute('href'), (e.innerText || e.title || '').trim()])")
        return text, [tuple(x) for x in links]

    def close():
        browser.close()
        pw.stop()

    return fetch, close


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="파워FM 프로그램 목록을 공식 페이지에서 새로고침")
    ap.add_argument("command", choices=["refresh"])
    ap.add_argument("--no-discover", action="store_true", help="SBS 라디오 첫 화면에서 새 프로그램 찾기를 건너뜀")
    args = ap.parse_args(argv)
    conn = db.connect()
    db.init_db(conn)
    try:
        fetch, close = playwright_fetcher()
        try:
            report = refresh(conn, fetch, discover=not args.no_discover)
        finally:
            close()
        print(report.summary(), flush=True)
        return 0
    except Exception as e:
        first = (str(e).strip().splitlines() or [type(e).__name__])[0][:200]
        print(f"[오류] {type(e).__name__}: {first}", flush=True)
        db.log(conn, "programs", f"공식 페이지 새로고침 오류: {first}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
