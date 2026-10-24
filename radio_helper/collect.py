"""코너 게시판 모으기 (0.62): 프로그램 홈페이지 메뉴에서 청취자 사연·신청곡·코너 게시판 주소를 모아 '코너'에 넣는다.

    python -m radio_helper.collect run

- 사용자 PC 에서 도우미 브라우저(창 없이)로 실제 홈페이지를 열어, 메뉴에 있는 같은 프로그램의 게시판 주소만 넣는다
  (주소를 지어내지 않음). 공지·다시듣기·선곡표·포토·당첨자 같은 게시판은 뺀다.
- 시작점: 방송국 라디오 첫 화면(아래 HUBS)의 프로그램 목록 + 프로그램 목록(편성표)의 공식 홈페이지 주소 +
  이미 있는 코너 화면에서 찾아 둔 같은 프로그램 코너.
- 일주일마다 저절로 다시 모은다 (개편·새 코너 반영). 이미 있는 주소는 그대로 둔다.
- 넣은 코너는 '매일 게시판 확인'이 하나씩 공지를 읽고, 받지 않는 곳은 건너뛴다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta

from urllib.parse import urlparse

from . import db, stations
from .boards import BOARD_LOCK, board_busy

SKIP_TEXT = re.compile(r"공지|다시\s*듣기|선곡|포토|사진|갤러리|당첨|VOD|보이는\s*라디오|FAQ|자주\s*묻는|제작진|출연자|"
                       r"notice|replay|photo|gallery|winner", re.I)
MAX_PER_PAGE = 15
REFRESH_DAYS = 7
LAST_KEY = "collect.last_run"
STARTED_KEY = "collect.started_at"
# 방송국 라디오 첫 화면 → 프로그램 홈페이지 (사용자가 보내 준 CBS 화면, 2026-10-22):
#   https://www.cbs.co.kr/radio 위쪽 '표준FM · 음악FM · JOY4U' 탭마다 프로그램 그림 줄 → 프로그램 홈
#   (…/program/home/cbs_P000554 같은 주소). 프로그램 홈 메뉴: 홈 · 공지사항 · 선곡표 · 사연과 신청곡, 아래에 '요일별 코너'.
#   '사연과 신청곡'은 비공개 게시판(작성자 본인과 관리자만 봄)이고 '게시판 운영지침' 단추와 '글쓰기' 단추가 있다.
# SBS (2026-10-22 화면): https://www.sbs.co.kr/radio?div=gnb_pc '파워FM · 러브FM · 고릴라M' 탭마다 진행자 그림 줄 →
#   프로그램 홈(programs.sbs.co.kr/radio/<코드>/main). 메뉴: 프로그램 소개 · 라디오 선곡표 · (프로그램마다 다른 참여 게시판,
#   예: 황제파워 '황제의 전당 · 황제파워 AI 로고송 모집 · 참여해주세요') · 전화 연결 신청 · 다시듣기/팟캐스트.
# MBC (2026-10-22 화면): https://www.imbc.com/broad/radio/ '표준FM 95.9 · FM4U 91.9' 지금 방송 칸(아래 펼침에 다음 순서) →
#   프로그램 홈(www.imbc.com/broad/radio/fm4u/<이름>/). 메뉴: mini 메시지 · 선곡표 · 다시듣기 · 보이는 라디오 · 사연과 신청곡 ·
#   인스타그램 · 상품문의, 아래에 '매일코너'·'요일코너' 목록.
# KBS (2026-10-22 화면): https://radio.kbs.co.kr/ '1Radio · HappyFM · 3Radio · ClassicFM · CoolFM · 한민족방송' 탭마다 프로그램
#   그림 목록(시간순) → 프로그램 홈(program.kbs.co.kr/2fm/radio/<이름>/…). 메뉴: 다시 듣기 ▾ · 청취자 참여 ▾ · 선물방 ▾ ·
#   공지사항 ▾ (펼침 메뉴 안에 게시판 링크), 아래 '오늘의 코너 · 전체 코너 소개'.
HUBS = {
    "SBS": {"url": "https://www.sbs.co.kr/radio?div=gnb_pc", "tabs": ["파워FM", "러브FM", "고릴라M"],
            "program": re.compile(r"^https://(?:m\.)?programs\.sbs\.co\.kr/radio/([A-Za-z0-9_]+)(?:[/?#]|$)", re.I),
            "home": "https://programs.sbs.co.kr/radio/{}/main"},
    "MBC": {"url": "https://www.imbc.com/broad/radio/", "tabs": [], "expand": True,
            "program": re.compile(r"^https://(?:www\.)?imbc\.com/broad/radio/((?:fm4u|fm|chm)/[A-Za-z0-9_]+)(?:/|$)", re.I),
            "home": "https://www.imbc.com/broad/radio/{}/"},
    "KBS": {"url": "https://radio.kbs.co.kr/", "tabs": ["CoolFM", "HappyFM", "1Radio", "ClassicFM"],
            "program": re.compile(r"^https://program\.kbs\.co\.kr/((?:1radio|2radio|3radio|1fm|2fm|cool|happy)/radio/"
                                  r"[A-Za-z0-9_\-]+)(?:[/?#]|$)", re.I),
            "home": "https://program.kbs.co.kr/{}/pc/index.html"},
    "CBS": {"url": "https://www.cbs.co.kr/radio", "tabs": ["표준FM", "음악FM", "JOY4U"],
            "program": re.compile(r"^https://(?:www\.)?cbs\.co\.kr/(?:radio/)?program/home/(cbs_P\d+)", re.I),
            "home": "https://www.cbs.co.kr/program/home/{}"},
}
NOT_PROGRAM = {"main", "schedule", "notice", "index", "event", "events", "podcast", "onair", "mini"}


BOARD_URL = re.compile(r"board|bbs|corner|requestsong|storys?(?:/|$)", re.I)
# 게시판 안의 글 하나(글 보기)·쪽 넘김 주소 — 게시판이 아니다 (0.63 은 이것까지 넣어 '새 게시판 2332개'가 됨)
POST_VIEW = stations.POST_URL          # 0.69: 판별은 stations 한곳에서
STRONG_TEXT = re.compile(r"사연|신청곡|참여해|참여하기|모집|코너")       # 주소 모양이 달라도 게시판으로 보는 메뉴 글자
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
        if POST_VIEW.search(where):
            continue
        if not (BOARD_URL.search(where) or STRONG_TEXT.search(text)) or SKIP_TEXT.search(text) \
                or SKIP_TEXT.search(where.rsplit("/", 1)[-1]):
            continue
        if not (any(t in where for t in tokens) or BOARD_TEXT.search(text)):
            continue
        seen.add(href)
        out.append({"url": href, "text": text})
        if len(out) >= MAX_PER_PAGE:
            break
    return out


def hub_programs(station: str, links: list[tuple[str, str]]) -> list[dict]:
    """라디오 첫 화면 링크 → 프로그램 홈페이지 [{program, url, code}] (순수 계산, 같은 프로그램은 한 번)."""
    hub = HUBS[station]
    out, seen = [], set()
    for href, text in links:
        m = hub["program"].match((href or "").split("#")[0])
        if not m or m.group(1) in seen or m.group(1).rsplit("/", 1)[-1].lower() in NOT_PROGRAM:
            continue
        seen.add(m.group(1))
        name = " ".join((text or "").split())
        name = re.sub(r"\s*(ON\s*AIR|보이는\s*라디오)\s*", " ", name, flags=re.I).strip()
        out.append({"program": name[:60] or f"{station} 프로그램", "url": hub["home"].format(m.group(1)),
                    "code": m.group(1)})
    return out


def read_hub(url: str, tabs: list[str], expand: bool = False) -> list[tuple[str, str]]:
    """라디오 첫 화면: 채널 탭을 하나씩 눌러 가며 보이는 링크를 모두 모은다. expand: 접힌 목록(펼침 단추)도 연다 (MBC)."""
    from playwright.sync_api import sync_playwright

    from . import autofill

    links: list = []
    with sync_playwright() as p:
        ctx, page = autofill.launch_profile(p, headless=True)
        try:
            page.goto(url)
            for tab in [None, *tabs]:
                if tab:
                    try:
                        page.get_by_text(tab, exact=True).first.click(timeout=5000)
                    except Exception:
                        try:                                  # 글자가 그림인 탭 (KBS 'CoolFM' 등)
                            page.locator(f"img[alt*='{tab}'], [title*='{tab}'], [aria-label*='{tab}']").first.click(timeout=3000)
                        except Exception:
                            continue
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                links += page.eval_on_selector_all("a[href]", "els => els.map(a => [a.href, a.innerText || ''])")
            if expand:
                folded = page.locator("[aria-expanded=false]")
                for i in range(min(folded.count(), 8)):
                    try:
                        folded.nth(i).click(timeout=3000)
                        page.wait_for_timeout(800)
                    except Exception:
                        continue
                links += page.eval_on_selector_all("a[href]", "els => els.map(a => [a.href, a.innerText || ''])")
        finally:
            ctx.close()
    return links


def same_code(url: str) -> str:
    """같은 프로그램 홈인지 견주는 열쇠 (끝의 main·index.html·/ 는 뗌)."""
    m = re.search(r"cbs_P\d+", url or "")
    if m:
        return m.group(0)
    return re.sub(r"(/main|/index\.html?)?/?$", "", (url or "").split("?")[0]).lower()


def start_pages(conn, hub_found: list[dict] | None = None) -> list[dict]:
    """방문할 프로그램 홈페이지 [{channel, program, url}] (허용된 방송국 https 주소만, 같은 프로그램은 한 번).
    hub_found: 라디오 첫 화면에서 찾은 프로그램 [{station, program, url}]."""
    out, seen = [], set()
    for h in hub_found or []:
        if same_code(h["url"]) not in seen:
            seen.add(same_code(h["url"]))
            out.append({"channel": h["station"], "program": h["program"], "url": h["url"]})
    for p in conn.execute("SELECT channel, title, main_url FROM programs WHERE on_air = 1 ORDER BY channel, start_time"):
        url = (p["main_url"] or "").strip()
        if same_code(url) in seen or stations.station_of(url) is None:
            continue
        seen.add(same_code(url))
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
    if station is None or stations.is_post_url(url):     # 다른 사용자가 쓴 글 하나는 코너가 아님 (0.69)
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO corners (program, kind, title, board_url, write_url, dev_note, station, updated_at) "
        "VALUES (?, 'board', ?, ?, ?, ?, ?, ?)",
        (program or station, (title or "코너 게시판")[:60], url, db.corner_write_url(url) or url, note, station, db.now()))
    return cur.rowcount > 0


def cleanup_posts(conn) -> int:
    """예전 모으기가 게시판으로 넣은 '글 하나'(다른 사용자가 쓴 글) 주소를 지운다. 그 코너로 보낸 적이 있거나 '매일 자동'을
    켠 것은 기록을 위해 남기되 다시 열지 않게 한다(board_exists = 0). 지운 수."""
    gone = 0
    for c in conn.execute("SELECT id, board_url, COALESCE(auto_on, 0) AS auto_on FROM corners").fetchall():
        if not stations.is_post_url(c["board_url"]):
            continue
        if c["auto_on"] or conn.execute("SELECT 1 FROM submissions WHERE corner_id = ? LIMIT 1", (c["id"],)).fetchone():
            conn.execute("UPDATE corners SET board_exists = 0, auto_on = 0 WHERE id = ?", (c["id"],))
            continue
        conn.execute("DELETE FROM corners WHERE id = ?", (c["id"],))
        gone += 1
    conn.commit()
    return gone


def collect_page(conn, start: dict, reader=read_links, stamp: str = "", say=print) -> int | None:
    """프로그램 홈페이지 하나의 메뉴에서 게시판을 넣는다. 새로 넣은 수 (열지 못하면 None)."""
    stamp = stamp or datetime.now().strftime("%Y-%m-%d")
    try:
        links = reader(start["url"])
    except Exception as e:
        say(f"{start['program']}: 홈페이지를 열지 못함 — {type(e).__name__}: {str(e)[:80]}")
        return None
    n = 0
    for x in board_links(start["url"], links):
        if add(conn, x["url"], x["text"], start["program"],
               f"코너 게시판 모으기 ({start['channel']} {start['program']} 홈페이지 메뉴, {stamp})"):
            n += 1
    conn.commit()
    return n


def collect(conn, reader=read_links, say=print, now: datetime | None = None, hub_reader=read_hub) -> dict:
    """모으기 한 번. {pages, failed, added, programs, hub}"""
    now = now or datetime.now()
    stamp = now.strftime("%Y-%m-%d")
    result = {"pages": 0, "failed": 0, "added": 0, "programs": [], "hub": 0}
    found = []
    for station, hub in HUBS.items():
        try:
            progs = hub_programs(station, hub_reader(hub["url"], hub["tabs"], hub.get("expand", False)))
        except Exception as e:
            say(f"{station} 라디오 첫 화면을 열지 못함 — {type(e).__name__}: {str(e)[:80]}")
            continue
        for x in progs:
            found.append({**x, "station": station})
            # 편성표에 같은 이름의 프로그램이 있으면 홈페이지 주소를 채워 둔다 (다음 모으기·공지 읽기에 씀)
            conn.execute("UPDATE programs SET main_url = ? WHERE COALESCE(main_url, '') = '' AND title = ? "
                         "AND channel LIKE ?", (x["url"], x["program"], f"{station}%"))
        conn.commit()
        result["hub"] += len(progs)
        say(f"{station} 라디오 첫 화면: 프로그램 {len(progs)}개")
    for start in start_pages(conn, found):
        n = collect_page(conn, start, reader, stamp, say)
        if n is None:
            result["failed"] += 1
            continue
        result["pages"] += 1
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
    db.log(conn, "post", f"코너 게시판 모으기: 홈페이지 {result['pages']}곳 확인"
                         + (f"(라디오 첫 화면에서 프로그램 {result['hub']}개)" if result["hub"] else "")
                         + f" · 새 게시판 {result['added']}개"
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
    if db.is_stopped(conn) or board_busy() or not (HUBS or start_pages(conn)):
        return False
    from .boards import paused

    if paused(conn):                         # 게시판 작업 정지 중 (채팅만)
        return False
    from . import aside_records

    if aside_records.rules_on(conn):        # Aside 규칙: 전체 코너 수집은 사용자가 '지금 모으기'를 누를 때만
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
