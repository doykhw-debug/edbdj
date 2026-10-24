"""게시판 주소 → 방송국 (SBS·MBC·KBS·CBS). 자동 입력은 이 방송국들의 https 주소에서만 한다 (안전장치)."""

from __future__ import annotations

import re
from urllib.parse import urlparse

STATIONS = ("SBS", "MBC", "KBS", "CBS")
EXACT_HOSTS = {"programs.sbs.co.kr": "SBS", "m.programs.sbs.co.kr": "SBS"}
DOMAIN_SUFFIXES = {"imbc.com": "MBC", "kbs.co.kr": "KBS",   # 이 도메인과 그 아래 주소 (www.imbc.com, program.kbs.co.kr …)
                   "cbs.co.kr": "CBS"}                        # 0.55: CBS (www.cbs.co.kr/board/…)


def station_of(url: str | None) -> str | None:
    """허용된 방송국 게시판 주소면 방송국 이름, 아니면 None."""
    try:
        u = urlparse(url or "")
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if u.scheme != "https" or not host:
        return None
    if host in EXACT_HOSTS:
        return EXACT_HOSTS[host]
    for domain, station in DOMAIN_SUFFIXES.items():
        if host == domain or host.endswith("." + domain):
            return station
    return None


def corner_station(corner) -> str | None:
    """코너의 방송국: 적어 둔 값, 없으면 게시판 주소로 (예전 SBS 코너는 칼럼이 비어 있음)."""
    keys = corner.keys() if hasattr(corner, "keys") else corner
    saved = corner["station"] if "station" in keys else None
    return saved or station_of(corner["write_url"] if "write_url" in keys else None) or \
        station_of(corner["board_url"] if "board_url" in keys else None)


# 게시판 안의 '글 하나'(글 보기)·쪽 넘김 주소 — 게시판이 아니다. 다른 사용자가 쓴 글은 열지 않는다 (0.69, 사용자 요청:
# 권한이 없는 글을 계속 열어 봄). 관리자 공지는 게시판 화면(목록·공지 칸)에서 읽는다.
POST_URL = re.compile(
    r"/view(?:/|\b)|/read(?:/|\b)|/detail|/article|/posts?/\d|imbbsview|talkview|articleview|"
    r"(?:view|read|detail)\.(?:html?|php|jsp|asp)|"
    r"[?&](?:cmd=view|mode=(?:view|read)|act=(?:view|read)|(?:board_?)?no|seq|idx|article_?(?:id|no)|bbs_?(?:seq|no)|"
    r"num|uid|wr_id|list_id|post_?(?:id|no)|ntt_?id|bno|content_?id|seq_?no|doc_?id|page)=|,read,", re.I)
# 글을 열 권한이 없다는 화면 (다른 사람의 비밀글·삭제된 글·잘못된 접근)
DENIED = re.compile(r"(?:읽기|열람|접근|보기)\s*권한이\s*없|권한이\s*없(?:습니다|어)|잘못된\s*접근|비밀\s*글입니다|"
                    r"비공개\s*글입니다|삭제되었거나\s*존재하지\s*않|존재하지\s*않는\s*(?:게시물|글)|작성자\s*본인만\s*(?:열람|볼\s*수)")


def is_post_url(url: str | None) -> bool:
    u = urlparse(url or "")
    return bool(POST_URL.search((u.path + "?" + u.query).lower()))


def denied_page(text: str) -> bool:
    """글을 열 권한이 없다는 화면인가 (로그인하라는 화면은 아님 — 그때는 로그인 문제)."""
    head = (text or "")[:1500]
    m = DENIED.search(head)
    return bool(m) and "로그인" not in head[max(0, m.start() - 80): m.end() + 80]
