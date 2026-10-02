"""python -m radio_helper  → 로컬 관리 화면 실행 (http://127.0.0.1:5000)"""

import argparse
import sys
import webbrowser

from . import db
from .app import create_app, launch_quizbot
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
    print(f"관리 화면: {url}  (끝내려면 이 창에서 Ctrl+C)")
    print("이 검은 창을 닫으면 관리 화면과 퀴즈 자동 참여가 멈춥니다. 켜 둔 채로 최소화하세요.")
    print("아래 빨간 WARNING 문구는 Flask 기본 안내라 무시해도 됩니다.")
    conn = db.connect()
    try:
        if (qconfig.get(conn, "quizbot.autostart") == "1" and not db.is_stopped(conn)
                and not qconfig.runner_alive(conn)):
            db.set_setting(conn, "quizbot.stop", "0")
            print(f"퀴즈 자동 참여도 함께 시작합니다. (기록: {launch_quizbot(['run'])})")
    finally:
        conn.close()
    if not args.no_browser:
        webbrowser.open(url)
    # 이 PC에서만 접속 가능하도록 127.0.0.1 에만 연다.
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
