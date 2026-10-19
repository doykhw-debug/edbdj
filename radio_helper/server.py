"""서버 운영 모드 — Oracle 서버(우분투, ARM)에서 radio.haetsallab.com 으로 관리 화면을 연다.

- RADIO_HELPER_SERVER=1 이면 서버 모드: 관리 화면만 연다. 고릴라 청취·퀴즈 자동 참여·PC 소리 녹음·문자(휴대폰)는
  윈도우 PC 전용이라 서버에서는 켜지 않는다. 자동 업데이트도 끈다 (서버는 deploy.sh 가 git pull 로 바꾼다).
- 포트는 환경 변수 PORT (서버 기본 5002, PC 기본 5000). 주소는 언제나 127.0.0.1 — 밖에서는 Cloudflare 터널로만 들어온다.
- 서버에서는 같은 서버의 다른 프로그램도 127.0.0.1 로 들어올 수 있으므로 모든 요청에 로그인을 받는다.
  비밀번호는 서버에서 `python -m radio_helper.remote set-password` 로 정한다.
- 비밀값(API 키 등)은 프로그램 폴더의 .env 에 둔다 (.env.example 참고). 이미 있는 환경 변수는 덮어쓰지 않는다.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

SERVER_ENV = "RADIO_HELPER_SERVER"
BIND = "127.0.0.1"
SERVER_PORT = 5002
DESKTOP_PORT = 5000
PUBLIC_URL = "https://radio.haetsallab.com"
# 서버에서도 되는 듣기 실행기 도구 (Claude API 만 씀). 나머지(청취·고릴라·소리·문자)는 PC 전용.
SERVER_TOOLS = {"enrich"}
PC_ONLY_MESSAGE = ("이 기능은 윈도우 PC 프로그램에서만 됩니다 (청취·퀴즈 자동 참여·고릴라·문자·받아쓰기, "
                   "직접 보며 쓰는 브라우저 창). 이 서버(radio.haetsallab.com)에서는 사연·인물·원고·퀴즈 기록을 "
                   "관리합니다.")
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class PcOnly(RuntimeError):
    """서버에서 PC 전용 기능을 부름."""


def is_server() -> bool:
    return os.environ.get(SERVER_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def port(default: int | None = None) -> int:
    raw = (os.environ.get("PORT") or "").strip()
    if raw.isdigit() and 0 < int(raw) < 65536:
        return int(raw)
    if default is not None:
        return default
    return SERVER_PORT if is_server() else DESKTOP_PORT


def public_url() -> str:
    return (os.environ.get("PUBLIC_URL") or PUBLIC_URL).strip().rstrip("/")


def parse_env(text: str) -> dict[str, str]:
    """.env 내용: KEY=VALUE 줄만 (앞의 export, 따옴표, 줄 끝 주석은 뗌). 명령은 실행하지 않는다."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not _KEY.fullmatch(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value)
        out[key] = value
    return out


# 윈도우 PC 에서는 읽지 않는 값: 서버용 .env 를 PC 폴더에 잘못 두어도 PC 가 서버 모드로 바뀌지 않게
SERVER_ONLY_KEYS = {SERVER_ENV, "RADIO_HELPER_DATA_DIR"}
# 서버용 .env(RADIO_HELPER_SERVER=1 이 적힌 파일)를 PC 에서 읽을 때 더 빼는 값: PORT=5002 를 따르면 PC 관리 화면이
# 5002번으로 켜져 밖에서 보기(터널 → 5000번)가 끊기고, TZ=Asia/Seoul 은 윈도우에서 시각을 틀리게 만들 수 있다.
SERVER_FILE_KEYS = SERVER_ONLY_KEYS | {"PORT", "PUBLIC_URL", "RADIO_HELPER_HEADLESS", "TZ"}


def load_env(path: Path | None = None, windows: bool | None = None) -> list[str]:
    """프로그램 폴더의 .env 를 환경 변수로 읽는다 (이미 있는 값은 그대로). 읽은 이름 목록 (값은 돌려주지 않음)."""
    path = path or Path(__file__).resolve().parent.parent / ".env"
    windows = sys.platform == "win32" if windows is None else windows
    try:
        values = parse_env(path.read_text(encoding="utf-8-sig"))
    except OSError:
        return []
    skip = set()
    if windows:
        server_file = values.get(SERVER_ENV, "").strip().lower() in ("1", "true", "yes", "on")
        skip = SERVER_FILE_KEYS if server_file else SERVER_ONLY_KEYS
    loaded = []
    for key, value in values.items():
        if key in skip:
            continue
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def check_tool(args: list[str]) -> None:
    """서버에서 PC 전용 도구를 띄우려 하면 PcOnly."""
    if is_server() and (not args or args[0] not in SERVER_TOOLS):
        raise PcOnly(PC_ONLY_MESSAGE)
