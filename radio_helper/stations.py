"""게시판 주소 → 방송국 (SBS·MBC·KBS·CBS). 자동 입력은 이 방송국들의 https 주소에서만 한다 (안전장치)."""

from __future__ import annotations

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
