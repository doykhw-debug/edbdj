"""밖에서 관리 화면 보기 — radio.haetsallab.com (Cloudflare Tunnel → 이 PC의 관리 화면).

- 터널 주소로 들어온 요청(또는 Cloudflare 가 붙이는 Cf-Connecting-Ip 머리글이 있는 요청)은 '원격'으로 보고
  비밀번호로 로그인해야 연다. PC 앞(127.0.0.1)에서는 예전처럼 로그인 없이 연다.
- 비밀번호는 PC 앞에서만 정하고 바꾼다. 원본은 어디에도 저장하지 않고 PBKDF2 해시만 데이터 폴더 DB에 둔다.
- 5번 틀리면 15분 동안 잠근다. 비밀번호를 바꾸면 원격으로 로그인해 둔 화면은 모두 로그아웃된다.
- 터널 토큰(Cloudflare 가 주는 긴 문자열)은 이 프로그램이 받지도 저장하지도 않는다 (cloudflared 서비스가 가짐).
- 서버 모드(server.py)에서는 모든 요청에 로그인을 받고, 비밀번호는 서버 터미널에서 정한다:
  .venv/bin/python -m radio_helper.remote set-password   (끄기: off, 상태: status)
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import json
import secrets
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from . import db, server

DEFAULT_DOMAIN = "radio.haetsallab.com"
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
TUNNEL_PORT = 5000            # PC: Cloudflare 터널이 가리키는 관리 화면 번호 (서버는 PORT, 기본 5002)
MIN_PASSWORD = 8
MAX_FAILS = 5
LOCK_MINUTES = 15
ITERATIONS = 200_000
REMOTE_ENDPOINTS = {"static", "health", "remote_login"}   # 로그인 전에도 여는 곳


def domain(conn: sqlite3.Connection) -> str:
    return (db.get_setting(conn, "remote.domain", DEFAULT_DOMAIN) or DEFAULT_DOMAIN).strip().lower()


def allowed_host(conn: sqlite3.Connection, host: str) -> bool:
    return host in LOCAL_HOSTS or host == domain(conn)


def is_remote(host: str, headers) -> bool:
    """터널 주소로 왔거나, Cloudflare·프록시를 거쳐 온 요청 (터널이 Host 를 localhost 로 바꿔도 원격으로 본다)."""
    return host not in LOCAL_HOSTS or any(headers.get(h) for h in ("Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For"))


def enabled(conn: sqlite3.Connection) -> bool:
    return bool(db.get_setting(conn, "remote.password_hash"))


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS).hex()
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest}"


def check_password(password: str, stored: str) -> bool:
    try:
        _algo, iterations, salt, digest = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, digest)


def session_mark(conn: sqlite3.Connection) -> str:
    """로그인한 세션에 남기는 표시. 비밀번호를 바꾸면 달라져서 예전 원격 로그인이 풀린다."""
    stored = db.get_setting(conn, "remote.password_hash") or ""
    return hashlib.sha256(stored.encode()).hexdigest()[:24] if stored else ""


def set_password(conn: sqlite3.Connection, password: str, again: str) -> str | None:
    """비밀번호 정하기. 문제가 있으면 안내 글, 잘 됐으면 None."""
    if len(password) < MIN_PASSWORD:
        return f"비밀번호는 {MIN_PASSWORD}자 이상으로 정해 주세요."
    if password != again:
        return "두 번 적은 비밀번호가 다릅니다."
    db.set_setting(conn, "remote.password_hash", hash_password(password))
    db.set_setting(conn, "remote.fails", "0")
    db.set_setting(conn, "remote.locked_until", "")
    db.log(conn, "remote", "원격 접속 비밀번호를 정함 (PC에서) — 예전 원격 로그인은 모두 풀림")
    return None


def turn_off(conn: sqlite3.Connection) -> None:
    db.set_setting(conn, "remote.password_hash", "")
    db.log(conn, "remote", "원격 접속을 끔 (PC에서)")


def locked_until(conn: sqlite3.Connection, now: datetime) -> datetime | None:
    raw = db.get_setting(conn, "remote.locked_until") or ""
    try:
        until = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return until if until > now else None


def login(conn: sqlite3.Connection, password: str, ip: str, now: datetime) -> str | None:
    """원격 로그인. 성공하면 None, 아니면 안내 글. 틀린 횟수와 접속한 곳(IP)만 기록한다 (비밀번호는 기록하지 않음)."""
    until = locked_until(conn, now)
    if until:
        return f"비밀번호를 여러 번 틀려 {until:%H:%M}까지 잠겨 있습니다."
    if check_password(password, db.get_setting(conn, "remote.password_hash") or ""):
        db.set_setting(conn, "remote.fails", "0")
        db.log(conn, "remote", f"원격 로그인 ({ip or '주소 모름'})")
        return None
    fails = int(db.get_setting(conn, "remote.fails") or 0) + 1
    db.set_setting(conn, "remote.fails", str(fails))
    if fails >= MAX_FAILS:
        db.set_setting(conn, "remote.fails", "0")
        db.set_setting(conn, "remote.locked_until", (now + timedelta(minutes=LOCK_MINUTES)).strftime("%Y-%m-%d %H:%M:%S"))
        db.log(conn, "remote", f"원격 로그인 {MAX_FAILS}번 실패 → {LOCK_MINUTES}분 잠금 ({ip or '주소 모름'})")
        return f"비밀번호를 {MAX_FAILS}번 틀려 {LOCK_MINUTES}분 동안 잠갔습니다."
    db.log(conn, "remote", f"원격 로그인 실패 {fails}번째 ({ip or '주소 모름'})")
    return f"비밀번호가 맞지 않습니다. ({MAX_FAILS - fails}번 더 틀리면 {LOCK_MINUTES}분 잠금)"


# ── 연결 상태 ─────────────────────────────────────────────────────────
def cloudflared_running() -> bool | None:
    """이 PC에서 cloudflared(터널 프로그램)가 돌고 있는지. 윈도우가 아니면 None."""
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq cloudflared.exe", "/NH"], capture_output=True,
                             text=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return "cloudflared.exe" in out.lower()


def no_origin_message(port: int, cloudflared: bool | None) -> str:
    """502: 터널은 연결돼 있는데 터널이 넘겨주는 곳(서비스 주소)에서 관리 화면이 답하지 않음."""
    head = "Cloudflare 와 터널은 연결돼 있지만, 터널이 넘겨주는 곳에서 관리 화면이 답하지 않습니다 (HTTP 502). "
    if cloudflared is False:
        return head + ("이 PC 에는 터널 프로그램(cloudflared)이 돌고 있지 않으므로, 이 주소는 다른 컴퓨터(예: Oracle 서버)의 터널로 "
                       "가고 있습니다. 서버에 아직 배포하지 않았다면 그쪽에는 관리 화면이 없습니다 — Cloudflare Zero Trust → "
                       f"Networks → Tunnels 에서 이 주소(Public Hostname)를 이 PC 의 터널로 옮기고 서비스를 HTTP · localhost:{port} 로 "
                       "하거나, 서버 배포를 마치세요.")
    if cloudflared:
        return head + (f"이 PC 의 터널은 돌고 있으니 Cloudflare 의 Public Hostname 서비스 주소가 HTTP · localhost:{port} 인지 "
                       "확인하세요 (서버용 127.0.0.1:5002 로 바꿔 두었으면 이 PC 와 어긋납니다). 이 주소가 다른 컴퓨터(서버)의 "
                       "터널에 들어 있어도 이렇게 됩니다.")
    return head + (f"서비스 주소가 관리 화면 번호(localhost:{port})와 같은지, 그 컴퓨터에서 관리 화면이 켜져 있는지 확인하세요 "
                   "(서버로 옮겨 두었는데 서버 배포 전이면 이렇게 됩니다).")


def probe(host: str, opener=None, timeout: float = 10, port: int = TUNNEL_PORT,
          cloudflared: bool | None = None) -> tuple[str, str]:
    """밖에서 그 주소로 이 도우미까지 오는지: (ok/access/no_origin/tunnel_down/dns/error, 설명)."""
    opener = opener or urllib.request.build_opener()
    url = f"https://{host}/health"
    try:
        with opener.open(url, timeout=timeout) as r:
            final, body = r.geturl(), r.read(4000)
    except urllib.error.HTTPError as e:
        if e.code in (502, 504):
            return "no_origin", no_origin_message(port, cloudflared)
        if e.code in (503, 521, 523, 530):
            return "tunnel_down", (f"Cloudflare 까지는 가지만 터널이 연결되지 않았습니다 (HTTP {e.code}). 터널 프로그램(cloudflared)이 "
                                   "꺼졌거나 그 컴퓨터가 꺼져 있습니다.")
        return "error", f"HTTP {e.code} 응답"
    except urllib.error.URLError as e:
        reason = str(getattr(e, "reason", e))
        if "Name or service" in reason or "getaddrinfo" in reason or "nodename" in reason or "11001" in reason:
            return "dns", "주소를 찾지 못했습니다 — Cloudflare 터널에 이 주소(Public Hostname)를 아직 넣지 않았습니다."
        return "error", f"연결 실패: {reason[:120]}"
    except OSError as e:
        return "error", f"연결 실패: {e}"
    if "cloudflareaccess.com" in final:
        return "access", "연결됨 — Cloudflare Access 로그인 화면이 먼저 나옵니다 (권장 설정)."
    try:
        if json.loads(body.decode("utf-8")).get("app") == "radio_helper":
            return "ok", "연결됨 — 밖에서 이 주소로 관리 화면이 열립니다."
    except (ValueError, UnicodeDecodeError, AttributeError):
        pass
    return "error", f"응답은 왔지만 이 도우미가 아닙니다. 터널이 http://localhost:{port} 을 가리키는지 확인하세요."


# ── 서버 터미널에서 비밀번호 정하기 ─────────────────────────────────────
def cli(argv=None, ask=getpass.getpass, say=print) -> int:
    """python -m radio_helper.remote set-password | off | status — 서버(또는 PC) 터미널에서. 비밀번호는 화면에 안 보임."""
    ap = argparse.ArgumentParser(prog="python -m radio_helper.remote", description="원격 접속 비밀번호")
    ap.add_argument("action", choices=["set-password", "off", "status"])
    args = ap.parse_args(argv)
    server.load_env()   # 서비스와 같은 데이터 폴더(RADIO_HELPER_DATA_DIR)를 쓰도록
    conn = db.connect()
    try:
        db.init_db(conn)
        if args.action == "status":
            say(f"데이터 폴더: {db.data_dir()}")
            say("원격 로그인: " + ("켜짐 (비밀번호 있음)" if enabled(conn) else "꺼짐 (비밀번호 없음)"))
            say(f"REMOTE_LOGIN={'on' if enabled(conn) else 'off'}")   # 배포 스크립트가 읽는 표시
            return 0
        if args.action == "off":
            turn_off(conn)
            say("원격 로그인을 껐습니다. 밖에서 열면 '원격 접속 꺼짐'만 보입니다.")
            return 0
        error = set_password(conn, ask(f"새 비밀번호 ({MIN_PASSWORD}자 이상): "), ask("한 번 더: "))
        if error:
            say(error)
            return 1
        say(f"비밀번호를 정했습니다. https://{domain(conn)} 에서 이 비밀번호로 로그인하세요.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(cli())
