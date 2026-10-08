"""SBS 라디오 전 채널(파워FM·러브FM·고릴라M) 프로그램 목록 끌어오기.

사용자 PC에서 공식 페이지를 읽기만 한다 (로그인·입력 없음).
- 등록된 프로그램마다 공식 메인 페이지를 열어 방송 시간·요일과 게시판 링크를 읽는다.
- SBS 라디오 첫 화면에서 프로그램 링크를 모으고, 목록에 없는 프로그램은 그 메인 페이지를 열어
  채널과 방송 시간을 읽는다. 둘 다 읽히면 편성에 넣고, 아니면 '후보'로 남겨 사용자가 확인한다.

- 코너 게시판(…/cornerboards/번호)은 그 페이지를 한 번 더 열어 코너 탭(?cornerid=)을 모두 모은다.

실행: python -m radio_helper.programs refresh
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Callable, Iterable
from urllib.parse import parse_qs, urljoin, urlparse

from . import db, seed

RADIO_HOME_URL = "https://www.sbs.co.kr/radio"
PROGRAM_HOSTS = {"programs.sbs.co.kr", "m.programs.sbs.co.kr"}
MAX_BOARD_VISITS = 4  # 프로그램마다 더 열어 볼 게시판 페이지 수 (코너 탭·이름 읽기)

# fetch(url) -> (본문 텍스트, [(링크 주소, 링크 글자), ...][, 최종 주소])
Fetcher = Callable[[str], tuple]

_RANGE_HHMM = re.compile(r"(\d{1,2}):(\d{2})\s*[~∼〜\-–]\s*(\d{1,2}):(\d{2})")
_AMPM = r"(오전|오후|낮|밤|저녁|새벽|아침)"
_RANGE_KO = re.compile(
    _AMPM + r"?\s*(\d{1,2})시\s*(?:(\d{1,2})분|(반))?\s*[~∼〜\-–]\s*"
    + _AMPM + r"?\s*(\d{1,2})시\s*(?:(\d{1,2})분|(반))?")
_DAYS = re.compile(r"(매일|평일|주말|월\s*[~∼\-–]\s*[금토일]|[월화수목금토일](?:\s*[,·]\s*[월화수목금토일])+|"
                   r"(?:토|일)요일|월요일\s*[~∼\-–]\s*[금토일]요일)")
_PROGRAM_PATH = re.compile(r"^/radio/([A-Za-z0-9_]+)/")
_BOARD_PATH = re.compile(r"^/radio/([A-Za-z0-9_]+)/(cornerboards|boards)/(\d+)")
# 링크가 아닌 곳(스크립트·데이터)에 적힌 게시판 주소
_BOARD_ANY = re.compile(r"(?:https?://(?:m\.)?programs\.sbs\.co\.kr)?/radio/[A-Za-z0-9_]+/(?:cornerboards|boards)/\d+"
                        r"(?:/?\?cornerid=\d+)?")
# 글 하나를 가리키는 주소의 글자는 게시판 이름이 아니다
_POST_PARAMS = {"board_no", "boardno", "cmd", "no", "article_no", "articleno", "page"}
_LABEL_KEYS = ("title", "name", "menu_name", "menuName", "menu_title", "menuTitle", "label", "text",
               "corner_name", "cornerName", "board_name", "boardName")

_PM_WORDS = ("오후", "밤", "저녁")
_CHANNEL_WORDS = (
    (seed.CHANNEL_POWERFM, re.compile(r"파워\s*FM|POWER\s*FM", re.I)),
    (seed.CHANNEL_LOVEFM, re.compile(r"러브\s*FM|LOVE\s*FM", re.I)),
    ("고릴라M", re.compile(r"고릴라\s*M(?![a-z])|GORILLA\s*M(?![a-z])", re.I)),
)


def detect_channel(text: str) -> str | None:
    """페이지 글에서 가장 많이 나온 채널 이름. 메뉴처럼 모든 채널이 같은 횟수로 나오면 모른다(None)."""
    counts = sorted(((len(rx.findall(text or "")), name) for name, rx in _CHANNEL_WORDS), reverse=True)
    if counts[0][0] == 0 or counts[0][0] == counts[1][0]:
        return None
    return counts[0][1]


@dataclass
class ProgramPageInfo:
    start: str | None = None
    end: str | None = None
    days: str | None = None
    channel: str | None = None
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


def board_link(href: str, label: str, base_url: str, codes: Iterable[str]) -> tuple[str, str, str] | None:
    """게시판·코너 링크면 (종류, 이름, 정리한 주소). 다른 프로그램·다른 사이트 링크는 None.

    글 하나를 여는 주소(…?board_no=…)도 그 게시판이 있다는 뜻이라 게시판 주소로 줄여 모은다.
    다만 그 글자는 글 제목이므로 이름으로 쓰지 않는다(빈 이름)."""
    u = urlparse(urljoin(base_url, (href or "").replace("&amp;", "&")))
    if u.hostname not in PROGRAM_HOSTS:
        return None
    m = _BOARD_PATH.match(u.path)
    if not m or m.group(1).lower() not in {c.lower() for c in codes if c}:
        return None
    query = parse_qs(u.query)
    kind = "코너" if m.group(2) == "cornerboards" else "게시판"
    clean = f"https://programs.sbs.co.kr/radio/{m.group(1)}/{m.group(2)}/{m.group(3)}"
    corner = (query.get("cornerid") or [""])[0]
    if kind == "코너" and corner.isdigit():
        clean += f"?cornerid={corner}"
    label = " ".join((label or "").split())
    if len(label) > 60 or _POST_PARAMS & set(query):
        label = ""
    return kind, label, clean


class BoardSet:
    """찾은 게시판·코너를 주소별로 한 번만 모은다. 이름 없는 주소는 뒤에 이름이 나오면 채운다."""

    def __init__(self):
        self._items: dict[str, list[str]] = {}

    def add(self, kind: str, label: str, url: str) -> bool:
        if url in self._items:
            if label and not self._items[url][1]:
                self._items[url][1] = label
            return False
        self._items[url] = [kind, label]
        return True

    def add_links(self, links: Iterable[tuple[str, str]], base_url: str, codes: Iterable[str]) -> list[str]:
        codes = list(codes)
        added = []
        for href, label in links:
            found = board_link(href, label, base_url, codes)
            if found and self.add(*found):
                added.append(found[2])
        return added

    def label(self, url: str) -> str:
        return self._items[url][1]

    def urls(self) -> list[str]:
        return list(self._items)

    def items(self) -> list[tuple[str, str, str]]:
        """(종류, 이름, 주소). 코너 탭을 찾은 코너 게시판은 탭 없는 주소를 뺀다."""
        tabbed = {u.split("?")[0] for u in self._items if "?cornerid=" in u}
        return [(k, label, u) for u, (k, label) in self._items.items() if u not in tabbed]


def program_codes(code: str, final_url: str | None) -> set[str]:
    """프로그램 코드와, 페이지가 다른 주소로 넘어갔다면 그 주소의 코드."""
    codes = {code}
    if final_url:
        m = _PROGRAM_PATH.match(urlparse(final_url).path)
        if m and urlparse(final_url).hostname in PROGRAM_HOSTS:
            codes.add(m.group(1))
    return codes


def parse_program_page(code: str, text: str, links: Iterable[tuple[str, str]], base_url: str) -> ProgramPageInfo:
    start, end, days = parse_time_range(text)
    info = ProgramPageInfo(start=start, end=end, days=days, channel=detect_channel(text))
    found = BoardSet()
    found.add_links(links, base_url, program_codes(code, base_url))
    info.boards = found.items()
    return info


def links_in_text(text: str) -> list[tuple[str, str]]:
    """HTML·스크립트 글에 적힌 게시판 주소 (이름 없음)."""
    return [(m.group(0), "") for m in _BOARD_ANY.finditer((text or "").replace("\\/", "/"))]


def links_in_json(text: str) -> list[tuple[str, str]]:
    """페이지가 받아 온 데이터(JSON)에서 게시판 주소와, 같은 묶음에 있는 이름을 찾는다."""
    try:
        data = json.loads(text)
    except ValueError:
        return links_in_text(text)
    found: list[tuple[str, str]] = []

    def walk(obj):
        if isinstance(obj, dict):
            label = next((obj[k] for k in _LABEL_KEYS if isinstance(obj.get(k), str) and obj[k].strip()), "")
            for v in obj.values():
                if isinstance(v, str):
                    found.extend((href, label) for href, _ in links_in_text(v))
                else:
                    walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)
    return found


def _unpack(res, url: str) -> tuple[str, list[tuple[str, str]], str]:
    """fetch 결과 (본문, 링크) 또는 (본문, 링크, 최종 주소)."""
    return res[0], list(res[1]), (res[2] if len(res) > 2 and res[2] else url)


def collect_boards(fetch: Fetcher, code: str, links, page_url: str,
                   limit: int = MAX_BOARD_VISITS) -> tuple[list[tuple[str, str, str]], int]:
    """메인 페이지 링크에서 게시판을 모으고, 코너 게시판과 이름 없는 게시판은 그 페이지를 열어 더 읽는다.

    코너 게시판 페이지 안에만 코너 탭(?cornerid=) 목록이 있는 경우가 많다. (게시판들, 더 열어 본 페이지 수)"""
    codes = program_codes(code, page_url)
    found = BoardSet()
    queue = found.add_links(links, page_url, codes)
    opened_corner_boards: set[str] = set()
    visits = 0

    def wanted(url: str) -> bool:
        if "/cornerboards/" in url:
            return url.split("?")[0] not in opened_corner_boards
        return not found.label(url)

    while queue and visits < limit:
        url = queue.pop(0)
        if not wanted(url):
            continue
        visits += 1
        try:
            _text, page_links, final = _unpack(fetch(url), url)
        except Exception:
            continue  # 열리지 않으면 같은 코너 게시판의 다른 탭 주소로 다시 해 본다
        if "/cornerboards/" in url:
            opened_corner_boards.add(url.split("?")[0])
        queue.extend(found.add_links(page_links, final, codes | program_codes(code, final)))
    return found.items(), visits


def board_title(kind: str, label: str, url: str) -> str:
    """이름을 못 읽은 게시판의 임시 이름."""
    if label:
        return label
    m = re.search(r"/(\d+)(?:\?cornerid=(\d+))?$", url)
    if m and m.group(2):
        return f"(이름 확인 필요) 코너 {m.group(2)}"
    return f"(이름 확인 필요) {kind} {m.group(1) if m else ''}".strip()


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
    added: list[str] = field(default_factory=list)       # 채널·시간을 읽어 편성에 넣은 새 프로그램
    candidates: list[str] = field(default_factory=list)  # 채널이나 시간을 못 읽어 확인이 필요한 프로그램
    errors: list[str] = field(default_factory=list)
    board_counts: dict[str, int] = field(default_factory=dict)  # 이번에 찾은 게시판·코너 수 (프로그램별)
    no_boards: list[str] = field(default_factory=list)           # 게시판 링크를 하나도 못 찾은 프로그램

    def summary(self) -> str:
        parts = [f"프로그램 {self.checked}개 확인"]
        if self.time_changes:
            parts.append("시간 변경 " + ", ".join(self.time_changes))
        if self.unreadable:
            parts.append("시간을 읽지 못함 " + ", ".join(self.unreadable))
        parts.append(f"게시판·코너 {sum(self.board_counts.values())}개 찾음(새로 추가 {len(self.new_boards)}개)")
        if self.no_boards:
            parts.append("게시판을 못 찾음 " + ", ".join(self.no_boards))
        if self.added:
            parts.append("새 프로그램 추가 " + ", ".join(self.added))
        if self.candidates:
            parts.append("새 프로그램 후보 " + ", ".join(self.candidates))
        if self.errors:
            parts.append("오류 " + "; ".join(self.errors))
        return " / ".join(parts)


def _save_boards(conn: sqlite3.Connection, report: RefreshReport, title: str, fetch: Fetcher, code: str,
                 links, page_url: str, say: Callable[[str], None]) -> None:
    boards, visits = collect_boards(fetch, code, links, page_url)
    added = 0
    for kind, label, url in boards:
        note = "공식 페이지에서 자동 수집. 모집 여부·형식 미확인" + ("" if label else " · 이름을 못 읽음(열어서 확인)")
        cur = conn.execute(
            """INSERT OR IGNORE INTO corners (program, kind, title, board_url, dev_note, is_target, updated_at)
               VALUES (?, ?, ?, ?, ?, 0, ?)""",
            (title, kind, board_title(kind, label, url), url, note, db.now()))
        if cur.rowcount:
            added += 1
            report.new_boards.append(f"{title} · {board_title(kind, label, url)}")
    report.board_counts[title] = len(boards)
    if not boards:
        report.no_boards.append(title)
    say(f"- {title}: 게시판·코너 {len(boards)}개 (새로 추가 {added}개, 링크 {len(links)}개 읽음, 게시판 {visits}곳 더 열어 봄)")


def refresh(conn: sqlite3.Connection, fetch: Fetcher, discover: bool = True,
            say: Callable[[str], None] = lambda _msg: None) -> RefreshReport:
    report = RefreshReport()
    programs = conn.execute("SELECT * FROM programs WHERE on_air = 1 ORDER BY channel, start_time").fetchall()
    for p in programs:
        try:
            text, links, final = _unpack(fetch(p["main_url"]), p["main_url"])
        except Exception as e:  # 한 페이지 실패가 전체를 멈추지 않게 한다
            report.errors.append(f"{p['title']}: {type(e).__name__}")
            say(f"- {p['title']}: 페이지를 열지 못함 ({type(e).__name__})")
            continue
        report.checked += 1
        info = parse_program_page(p["code"], text, links, final)
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
        if info.channel and p["channel"] in (None, "", "미확인"):
            conn.execute("UPDATE programs SET channel = ? WHERE id = ?", (info.channel, p["id"]))
        _save_boards(conn, report, p["title"], fetch, p["code"], links, final, say)
        conn.commit()

    if discover:
        try:
            _text, links, _final = _unpack(fetch(RADIO_HOME_URL), RADIO_HOME_URL)
            known = {r["code"].lower() for r in conn.execute("SELECT code FROM programs")}
            for code, title in discover_programs(links).items():
                if code.lower() in known:
                    continue
                url = seed.program_main_url(code)
                page_links, final = [], url
                try:
                    page_text, page_links, final = _unpack(fetch(url), url)
                    info = parse_program_page(code, page_text, page_links, final)
                except Exception:
                    info = ProgramPageInfo()
                on_air = 1 if info.channel and info.start and info.end else 0
                conn.execute(
                    """INSERT OR IGNORE INTO programs (channel, code, title, start_time, end_time, days, main_url, on_air,
                           source, checked_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (info.channel or "미확인", code, title, info.start, info.end, info.days, url, on_air,
                     "공식 페이지" if on_air else "SBS 라디오 첫 화면에서 발견", db.now(), db.now()))
                (report.added if on_air else report.candidates).append(
                    f"{title}({info.channel} {info.start}~{info.end})" if on_air else title)
                if on_air:  # 새로 편성에 넣은 프로그램의 게시판도 같이 모은다
                    _save_boards(conn, report, title, fetch, code, page_links, final, say)
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
    responses = []

    def keep(resp):
        try:
            host = urlparse(resp.url).hostname or ""
            if host.endswith("sbs.co.kr") and resp.request.resource_type in ("xhr", "fetch"):
                responses.append(resp)
        except Exception:
            pass

    page.on("response", keep)

    def fetch(url: str):
        responses.clear()
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # 메뉴·게시판 목록은 스크립트가 나중에 그린다: 게시판 링크가 생길 때까지, 그다음 통신이 잦아들 때까지
        # 잠깐 기다린다 (광고 통신이 끝나지 않는 페이지도 있어 짧게 끊는다)
        for wait in (lambda: page.wait_for_selector('a[href*="boards/"]', state="attached", timeout=8_000),
                     lambda: page.wait_for_load_state("networkidle", timeout=3_000)):
            try:
                wait()
            except Exception:
                pass
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(700)
        except Exception:
            pass
        text = page.inner_text("body")
        links = [tuple(x) for x in page.eval_on_selector_all(
            "a[href]", "els => els.map(e => [e.getAttribute('href'), (e.innerText || e.textContent || "
                       "e.getAttribute('title') || e.getAttribute('aria-label') || '').trim()])")]
        # 링크(a)가 아니라 클릭 처리나 받아 온 데이터에만 적힌 게시판 주소도 모은다 (이름 있는 링크가 먼저)
        links += links_in_text(page.content())
        for resp in list(responses):
            try:
                if "json" in (resp.headers.get("content-type") or "") or resp.url.endswith(".json"):
                    links += links_in_json(resp.text())
            except Exception:
                pass
        return text, links, page.url

    def close():
        browser.close()
        pw.stop()

    return fetch, close


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SBS 라디오 프로그램 목록·방송 시간·코너 게시판을 공식 페이지에서 새로고침")
    ap.add_argument("command", choices=["refresh"])
    ap.add_argument("--no-discover", action="store_true", help="SBS 라디오 첫 화면에서 새 프로그램 찾기를 건너뜀")
    args = ap.parse_args(argv)
    conn = db.connect()
    db.init_db(conn)
    try:
        fetch, close = playwright_fetcher()
        try:
            report = refresh(conn, fetch, discover=not args.no_discover, say=lambda msg: print(msg, flush=True))
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
