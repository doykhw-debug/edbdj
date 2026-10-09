"""python -m radio_helper  → 로컬 관리 화면 실행 (http://127.0.0.1:5000)"""

import argparse
import logging
import sys
import webbrowser

from werkzeug.serving import run_simple

from . import db
from .app import create_app, start_runner
from .quizbot import config as qconfig


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


def main() -> None:
    ap = argparse.ArgumentParser(description="SBS 라디오 참여 도우미 — 로컬 관리 화면")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--no-browser", action="store_true", help="실행 후 브라우저를 자동으로 열지 않음")
    args = ap.parse_args()
    disable_console_quick_edit()
    app = create_app()
    url = f"http://127.0.0.1:{args.port}/"
    print("=" * 60)
    print(" 라디오 참여 도우미가 실행 중입니다. (정상)")
    print(f" 관리 화면: {url}")
    print("   브라우저가 저절로 안 열리면 위 주소를 주소창에 입력하세요.")
    print(" 이 검은 창을 닫으면 관리 화면과 청취·퀴즈 참여가 멈춥니다.")
    print(" 켜 둔 채로 최소화하세요. 끝낼 때는 이 창에서 Ctrl+C.")
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
    if not args.no_browser:
        webbrowser.open(url)
    # 개발 서버 시작 안내(빨간 WARNING)와 접속 기록은 오류처럼 보여 감춘다. 오류·경고는 그대로 보인다.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    # 이 PC에서만 접속 가능하도록 127.0.0.1 에만 연다.
    run_simple("127.0.0.1", args.port, app, threaded=True)


if __name__ == "__main__":
    main()
