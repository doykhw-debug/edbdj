"""python -m radio_helper  → 로컬 관리 화면 실행 (http://127.0.0.1:5000)"""

import argparse
import json
import logging
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

from werkzeug.serving import run_simple

from . import db
from .app import create_app, start_runner
from .quizbot import config as qconfig

APP_NAME = "radio_helper"
PORT_TRIES = 20
# 이 PC 안의 주소만 부르므로 윈도우 프록시 설정을 거치지 않는다
_local = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def disable_console_quick_edit() -> None:
    """윈도우 검은 창 안을 클릭하면 '선택' 모드가 되어 프로그램이 멈춘다. 이를 끈다."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)  # 표준 입력
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            enable_extended_flags, enable_quick_edit = 0x0080, 0x0040
            kernel32.SetConsoleMode(handle, (mode.value | enable_extended_flags) & ~enable_quick_edit)
    except Exception:
        pass


def _can_bind(port: int) -> bool:
    """그 번호를 아무도 안 쓰는지. 윈도우는 다른 프로그램이 쓰는 번호에도 겹쳐 열 수 있어 '혼자 쓰기'로 확인한다."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def port_state(port: int) -> str:
    """'free'(비어 있음) / 'ours'(이미 켜진 도우미가 응답) / 'busy'(다른 프로그램이나 멈춘 도우미)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            pass
    except OSError:
        return "free" if _can_bind(port) else "busy"
    try:
        with _local.open(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            if json.loads(r.read().decode("utf-8")).get("app") == APP_NAME:
                return "ours"
    except urllib.error.HTTPError as e:
        if e.code == 404 and _old_version_page(port):  # /health 가 없던 예전 판
            return "ours"
    except Exception:
        pass
    return "busy"


def _old_version_page(port: int) -> bool:
    try:
        with _local.open(f"http://127.0.0.1:{port}/", timeout=3) as r:
            return "라디오 참여 도우미" in r.read().decode("utf-8", "replace")
    except Exception:
        return False


def pick_port(first: int, tries: int = PORT_TRIES) -> tuple[int | None, list[str]]:
    """first 부터 비어 있는 번호를 찾는다. (번호, 건너뛴 이유들)"""
    notes = []
    for port in range(first, first + tries):
        state = port_state(port)
        if state == "free":
            return port, notes
        notes.append(f"{port}번: " + ("이미 켜진 도우미(예전 검은 창)가 쓰는 중" if state == "ours"
                                      else "다른 프로그램이 쓰는 중이거나 응답 없음"))
    return None, notes


def self_check(url: str, open_browser: bool, wait: float = 15.0, say=print) -> bool:
    """서버가 뜰 때까지 기다렸다가 브라우저를 열고, 첫 화면이 실제로 열리는지 확인해 결과를 알려 준다."""
    deadline = time.time() + wait
    while True:
        try:
            with _local.open(url + "health", timeout=2):
                break
        except Exception as e:
            if time.time() > deadline:
                say(f"[점검] 관리 화면 서버가 {wait:.0f}초 안에 응답하지 않습니다 ({type(e).__name__}). "
                    "이 창을 캡처해 보내 주세요.")
                return False
            time.sleep(0.2)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    started = time.time()
    try:
        with _local.open(url, timeout=30) as r:
            r.read()
        say(f"[점검] 관리 화면 정상 응답 ({time.time() - started:.1f}초). 브라우저에서 {url} 을 보세요.")
        return True
    except urllib.error.HTTPError as e:
        say(f"[점검] 관리 화면이 오류를 냈습니다 (HTTP {e.code}). 위에 나온 오류 내용과 이 창을 캡처해 보내 주세요.")
    except Exception as e:
        say(f"[점검] 관리 화면이 30초 안에 열리지 않습니다 ({type(e).__name__}). 이 창을 캡처해 보내 주세요.")
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="SBS 라디오 참여 도우미 — 로컬 관리 화면")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--no-browser", action="store_true", help="실행 후 브라우저를 자동으로 열지 않음")
    args = ap.parse_args()
    disable_console_quick_edit()

    port, notes = pick_port(args.port)
    for note in notes:
        print(f"[알림] {note} → 다음 번호를 씁니다.")
    if any("예전 검은 창" in n for n in notes):
        print("[알림] 예전에 켜 둔 검은 창은 예전 파일로 돌고 있을 수 있습니다. 그 창은 닫아도 됩니다.")
    if port is None:
        print(f"[오류] {args.port}~{args.port + PORT_TRIES - 1}번을 모두 다른 프로그램이 쓰고 있습니다. "
              "PC를 다시 켠 뒤 실행하거나 이 창을 캡처해 보내 주세요.")
        sys.exit(1)

    app = create_app()
    url = f"http://127.0.0.1:{port}/"
    print("=" * 60)
    print(" 라디오 참여 도우미가 실행 중입니다.")
    print(f" 관리 화면: {url}")
    print("   브라우저가 저절로 안 열리면 위 주소를 Ctrl+클릭하거나 주소창에 입력하세요.")
    print(" 이 검은 창을 닫으면 관리 화면과 청취·퀴즈 참여가 멈춥니다.")
    print(" 켜 둔 채로 최소화하세요. 끝낼 때는 이 창에서 Ctrl+C.")
    print(f" 데이터 위치: {db.data_dir()}  (업데이트 파일을 어디에 풀어도 그대로 남음)")
    if db.ADOPTED_FROM:
        print(f" 예전 폴더의 경험·사연 데이터를 옮겨 왔습니다: {db.ADOPTED_FROM.parent}")
    print("=" * 60)
    conn = db.connect()
    try:
        listening = qconfig.get(conn, "live.active") == "1"
        if ((qconfig.get(conn, "quizbot.autostart") == "1" or listening) and not db.is_stopped(conn)
                and not qconfig.runner_alive(conn)):
            db.set_setting(conn, "quizbot.stop", "0")
            print(("청취를 이어서 시작합니다." if listening else "퀴즈 자동 참여도 함께 시작합니다.")
                  + f" (기록: {start_runner(conn)})")
    finally:
        conn.close()
    # 서버가 실제로 뜬 뒤에 브라우저를 연다 (먼저 열면 '연결할 수 없음' 화면이 뜬다)
    threading.Thread(target=self_check, args=(url, not args.no_browser), daemon=True).start()
    # 개발 서버 시작 안내(빨간 WARNING)와 접속 기록은 오류처럼 보여 감춘다. 오류·경고는 그대로 보인다.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    # 이 PC에서만 접속 가능하도록 127.0.0.1 에만 연다.
    run_simple("127.0.0.1", port, app, threaded=True)


if __name__ == "__main__":
    main()
