"""python -m radio_helper           → PC: 관리 화면 실행 (http://127.0.0.1:5000, 환경 변수 PORT 로 바꿈)
python -m radio_helper --server  → 서버 운영 모드: 127.0.0.1:PORT(기본 5002)에 waitress 로 연다.
                                   밖에서는 Cloudflare 터널로 https://radio.haetsallab.com (server.py 참고)"""

import argparse
import faulthandler
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime

from werkzeug.serving import make_server, run_simple

from . import __version__, boards, cleanup, collect, db, remote, repair, schedules, server
from .app import create_app, ensure_runner, launch_quizbot, start_runner
from .quizbot import config as qconfig
from .quizbot import lanes

APP_NAME = "radio_helper"
PORT_TRIES = 20
PORT_WAIT_SECONDS = 60      # 다시 켤 때 방금 꺼진 관리 화면이 번호(5000)를 놓을 때까지 기다리는 시간
TUNNEL_WATCH_SECONDS = 20   # 다른 번호로 켜졌을 때 5000번이 비었는지 보는 간격
# 끝날 때 돌려주는 값: start_windows.bat 이 보고 비정상 종료(그 밖의 값)일 때만 자동으로 다시 켠다
EXIT_OK, EXIT_CRASH, EXIT_NO_PORT = 0, 1, 2
_crash_file = None   # 파이썬 밖(라이브러리)에서 꺼질 때 위치를 남기는 파일 (닫히지 않게 잡아 둠)
CTRL_C_CONFIRM_SECONDS = 3   # 이 시간 안에 Ctrl+C 를 두 번 눌러야 끝난다 (한 번은 실수로 보고 계속)
WATCHDOG_SECONDS = 30        # 듣기 실행기가 꺼졌는지 보는 간격
# 이 PC 안의 주소만 부르므로 윈도우 프록시 설정을 거치지 않는다
_local = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def server_log(message: str) -> None:
    """관리 화면 서버가 켜지고 꺼진 기록 (데이터 폴더 logs/server.log). 중간에 꺼졌을 때 원인을 찾는 용도."""
    try:
        log_dir = db.data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "server.log", "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except OSError:
        pass


def ctrl_c_handler(clock=time.monotonic, say=print):
    """Ctrl+C 를 한 번 누르면 안내만 하고 계속, 3초 안에 한 번 더 누르면 끝낸다.
    (검은 창에서 글자를 복사하려고 Ctrl+C 를 누르거나 실수로 눌러 모두 꺼지던 것을 막음)"""
    last = [None]

    def handler(_signum, _frame):
        t = clock()
        if last[0] is not None and t - last[0] <= CTRL_C_CONFIRM_SECONDS:
            raise KeyboardInterrupt
        last[0] = t
        say(f"[알림] Ctrl+C 를 눌렀습니다. 정말 끝내려면 {CTRL_C_CONFIRM_SECONDS}초 안에 Ctrl+C 를 한 번 더 누르세요. "
            "(실수였다면 그냥 두세요 — 관리 화면과 청취는 계속됩니다)")
        server_log("Ctrl+C 한 번 — 끝내지 않고 계속")
    return handler


def ask_listen(conn, was_listening: bool) -> None:
    """프로그램을 켤 때: 듣기를 멈춰 두고 관리 화면 첫 화면에서 무엇으로 들을지 고르게 한다."""
    last = qconfig.get(conn, "live.channel")
    db.set_setting(conn, "live.ask", "1")
    if was_listening:
        db.set_setting(conn, "live.active", "0")   # 고를 때까지 듣지도 보내지도 않는다 (채팅 앱도 켜지 않음)
    db.log(conn, "quizbot", f"프로그램 시작 — 무엇으로 들을지 묻는 중 (지난번: {last})")
    print(f" 무엇으로 들을지 관리 화면 첫 화면에서 골라 주세요 (지난번: {last}). 고르면 바로 청취를 시작합니다.")


def watch_updates(interval: float = 600, clock=datetime.now) -> None:
    """새 버전 확인(6시간마다)과 자동 업데이트(정한 시각, 기본 새벽 4시). 새 버전이 2분 넘게 잘 돌면 '확인됨'으로 표시."""
    from . import updater
    from .app import start_update_thread

    time.sleep(updater.CONFIRM_AFTER_SECONDS)
    updater.confirm_running(__version__)
    while True:
        try:
            conn = db.connect()
            try:
                now = clock()
                if updater.due_check(conn, now):
                    try:
                        updater.fetch_latest(conn)
                    except updater.UpdateError:
                        pass
                if updater.should_auto_install(conn, now) and not db.get_setting(conn, "update.started_at"):
                    db.set_setting(conn, "update.started_at", db.now())
                    server_log(f"자동 업데이트 시작 ({__version__} → {db.get_setting(conn, 'update.latest')})")
                    start_update_thread()
            finally:
                conn.close()
        except Exception as e:   # 확인이 멈추지 않게
            server_log(f"업데이트 확인 오류: {type(e).__name__}: {e}")
        time.sleep(interval)


def watch_runner(interval: float = WATCHDOG_SECONDS) -> None:
    """'청취 시작'이 켜져 있는데 듣기 실행기가 꺼지면 다시 띄운다 (뒤에서 계속 돎)."""
    restarts: list = []
    while True:
        time.sleep(interval)
        try:
            conn = db.connect()
            try:
                done = ensure_runner(conn, restarts, time.monotonic())
                lanes.ensure(conn, launch_quizbot)   # 동시 청취: 꺼진 보조 채널 청취기를 다시 띄움
                fixed = repair.auto(conn)   # 음성 인식 부품이 없으면 스스로 고침 (설치 / Python 3.12 로 다시 켬)
                boards.tick(conn)           # 코너 게시판 매일 자동 등록 (정한 시각이 된 코너)
                collect.tick(conn)          # 코너 게시판 모으기: 일주일마다 프로그램 홈페이지 메뉴에서 (0.62)
                boards.onair_tick(conn)     # 듣는 중: 지금 방송 프로그램의 게시판에 모두 쓰기 (Aside 창, 0.63)
                schedules.tick(conn)        # 전 채널 편성표: 일주일 지나면 MBC·KBS·CBS(웹 검색)·SBS(공식 페이지) 다시
                cleanup.tick(conn)          # 3일 지난 실행 기록·증거 사진 지우기 (6시간에 한 번, 0.92)
            finally:
                conn.close()
            if done and done != "limit":
                server_log(f"듣기 실행기가 꺼져 있어 다시 켬 ({done})")
                print(f"[알림] 듣기 실행기가 꺼져 있어 자동으로 다시 켰습니다. (기록: {done})")
            if fixed == "pip":
                print("[알림] 음성 인식 부품이 없어 뒤에서 설치합니다. 설치되면 듣기가 이어집니다.")
            elif fixed == "rebuild":
                print("[알림] 음성 인식 부품이 이 Python 에 없어 Python 3.12 로 다시 설치하고 다시 켭니다 (5~10분).")
        except Exception as e:   # 감시가 멈추지 않게
            server_log(f"듣기 실행기 감시 오류: {type(e).__name__}: {e}")


def record_native_crashes() -> None:
    global _crash_file
    try:
        log_dir = db.data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        _crash_file = open(log_dir / "server_crash.log", "a", encoding="utf-8")
        faulthandler.enable(file=_crash_file)
    except OSError:
        faulthandler.enable()


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


def wait_for_port(port: int, seconds: float = PORT_WAIT_SECONDS, sleep=time.sleep, say=print) -> str:
    """다시 켤 때(업데이트·오류 뒤) 방금 꺼진 관리 화면이 번호를 아직 놓지 않았으면 잠깐 기다린다.
    바로 다음 번호(5001)로 켜면 밖에서 보기(Cloudflare 터널 → 5000번)가 끊기기 때문. 마지막 상태를 돌려준다."""
    state = port_state(port)
    if state != "busy":
        return state
    say(f"[알림] {port}번이 아직 비지 않았습니다 (방금 꺼진 관리 화면을 정리하는 중). "
        f"밖에서 보기 주소가 이 번호를 써서 최대 {seconds:.0f}초 기다립니다.")
    waited = 0.0
    while waited < seconds:
        sleep(2)
        waited += 2
        state = port_state(port)
        if state != "busy":
            break
    return state


def attach_tunnel_port(app, port: int, state=None, make=None) -> bool:
    """관리 화면이 5000번이 아닌 번호로 켜져 있을 때 5000번이 비면 그 번호로도 연다 → 밖에서 보기가 다시 이어진다."""
    target = remote.TUNNEL_PORT
    if port == target or (state or port_state)(target) != "free":
        return False
    srv = (make or make_server)(server.BIND, target, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True, name="tunnel-port").start()
    app.config["SERVER_PORT"] = target   # 원격 접속 화면: 이제 터널 번호와 같음
    server_log(f"{target}번이 비어 그 번호로도 열었습니다 (밖에서 보기 다시 연결, 처음 켠 번호 {port})")
    try:
        conn = db.connect()
        try:
            db.log(conn, "remote", f"관리 화면을 {target}번으로도 열어 밖에서 보기(radio.haetsallab.com)를 다시 이었습니다")
        finally:
            conn.close()
    except Exception:
        pass
    return True


def watch_tunnel_port(app, port: int, interval: float = TUNNEL_WATCH_SECONDS) -> None:
    while True:
        time.sleep(interval)
        try:
            if attach_tunnel_port(app, port):
                print(f"[알림] {remote.TUNNEL_PORT}번이 비어 그 번호로도 열었습니다. 밖에서 보기 주소가 다시 열립니다.", flush=True)
                return
        except Exception as e:   # 열지 못하면 다음에 다시
            server_log(f"{remote.TUNNEL_PORT}번 이어 붙이기 실패: {type(e).__name__}: {e}")


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
        hang = dump_hang()       # 0.91: 어디서 멈췄는지 (모든 스레드의 지금 위치) 파일로 남긴다
        say(f"[점검] 관리 화면이 30초 안에 열리지 않습니다 ({type(e).__name__}). 이 창을 캡처해 보내 주세요."
            + (f" 멈춘 자리 기록: 데이터 폴더 logs\\{hang} (이 파일도 보내 주세요)" if hang else ""))
        try:                     # 늦게라도 열리는지 한 번 더 (얼마나 걸렸는지 알려 줌)
            with _local.open(url, timeout=120) as r:
                r.read()
            say(f"[점검] 관리 화면이 늦게 열렸습니다 ({time.time() - started:.0f}초). 브라우저에서 새로 고침해 보세요.")
            return True
        except Exception:
            say("[점검] 2분이 지나도 관리 화면이 열리지 않습니다.")
    return False


def dump_hang() -> str | None:
    """지금 이 프로그램의 모든 스레드가 어디서 무엇을 하는지 logs/hang_*.txt 로 남긴다. 파일 이름 (못 남기면 None)."""
    try:
        out_dir = db.data_dir() / "logs"
        out_dir.mkdir(parents=True, exist_ok=True)
        name = f"hang_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        with open(out_dir / name, "w", encoding="utf-8") as f:
            f.write("관리 화면 첫 화면이 30초 안에 열리지 않았을 때 모든 스레드의 위치\n\n")
            f.flush()
            faulthandler.dump_traceback(file=f, all_threads=True)
        return name
    except Exception:
        return None


def run_server(port: int, serve=None) -> None:
    """서버 운영 모드: 127.0.0.1:port 에 waitress(운영용 WSGI 서버)로 연다. 듣기 실행기·자동 업데이트·브라우저 열기는
    하지 않는다. 꺼지면 systemd(radio.service, Restart=always)가 다시 켠다."""
    record_native_crashes()
    if not _can_bind(port):
        print(f"[오류] {port}번을 다른 프로그램이 쓰고 있습니다. .env 의 PORT 를 바꾸거나 그 프로그램을 끄세요.")
        server_log(f"서버 모드 시작 못 함: {port}번을 다른 프로그램이 씀")
        sys.exit(EXIT_NO_PORT)
    app = create_app()
    app.config["SERVER_PORT"] = port
    conn = db.connect()
    try:
        # 서버에서는 듣지 않는다: PC에서 옮겨 온 데이터의 '청취 중'·'뭐로 들을지 묻는 중' 표시를 끈다 (PC 쪽 데이터는 그대로)
        for key in ("live.active", "live.ask"):
            db.set_setting(conn, key, "0")
        db.set_setting(conn, "update.started_at", "")
        has_password = remote.enabled(conn)
        db.log(conn, "server", f"서버 모드 시작 — {server.public_url()} (127.0.0.1:{port}, 버전 {__version__})")
    finally:
        conn.close()
    print("=" * 60)
    print(f" 라디오 참여 도우미 {__version__} — 서버 운영 모드")
    print(f" 밖에서: {server.public_url()}  (Cloudflare 터널 → http://127.0.0.1:{port})")
    print(f" 데이터 위치: {db.data_dir()}")
    if not has_password:
        print(" [주의] 로그인 비밀번호가 없어 아직 아무도 열 수 없습니다. 서버에서 정하세요:")
        print("        .venv/bin/python -m radio_helper.remote set-password")
    print("=" * 60, flush=True)
    server_log(f"서버 모드 시작 — 127.0.0.1:{port}")
    if serve is None:
        try:
            from waitress import serve
        except ImportError:
            print("[경고] waitress 가 없어 개발용 서버로 엽니다. pip install -r requirements-server.txt 를 하세요.", flush=True)
            run_simple(server.BIND, port, app, threaded=True)
            return
    serve(app, host=server.BIND, port=port, threads=8, ident="radio-helper", channel_timeout=120)


def main() -> None:
    server.load_env()   # 프로그램 폴더의 .env (API 키·PORT·데이터 폴더). 이미 있는 환경 변수는 그대로
    ap = argparse.ArgumentParser(description="SBS 라디오 참여 도우미 — 관리 화면")
    ap.add_argument("--port", type=int, default=None,
                    help="관리 화면 번호 (기본: 환경 변수 PORT, 없으면 PC 5000 · 서버 5002)")
    ap.add_argument("--server", action="store_true", help="서버 운영 모드 (RADIO_HELPER_SERVER=1 과 같음)")
    ap.add_argument("--no-browser", action="store_true", help="실행 후 브라우저를 자동으로 열지 않음")
    ap.add_argument("--restarted", action="store_true", help="비정상 종료 뒤 start_windows.bat 이 다시 켠 경우")
    args = ap.parse_args()
    if args.server:
        os.environ[server.SERVER_ENV] = "1"
    if server.is_server():
        run_server(args.port or server.port())
        return
    disable_console_quick_edit()
    record_native_crashes()

    args.port = args.port or server.port()
    if args.restarted:
        wait_for_port(args.port)
    port, notes = pick_port(args.port)
    for note in notes:
        print(f"[알림] {note} → 다음 번호를 씁니다.")
    if any("예전 검은 창" in n for n in notes):
        print("[알림] 예전에 켜 둔 검은 창은 예전 파일로 돌고 있을 수 있습니다. 그 창은 닫아도 됩니다.")
    if port is None:
        print(f"[오류] {args.port}~{args.port + PORT_TRIES - 1}번을 모두 다른 프로그램이 쓰고 있습니다. "
              "PC를 다시 켠 뒤 실행하거나 이 창을 캡처해 보내 주세요.")
        server_log("시작 못 함: 쓸 수 있는 번호(포트)가 없음")
        sys.exit(EXIT_NO_PORT)

    app = create_app()
    app.config["SERVER_PORT"] = port   # 원격 접속 화면: 터널이 가리키는 번호(5000)와 같은지 보여 줌
    url = f"http://127.0.0.1:{port}/"
    print("=" * 60)
    print(" 라디오 참여 도우미가 실행 중입니다.")
    print(f" 관리 화면: {url}")
    print("   브라우저가 저절로 안 열리면 위 주소를 Ctrl+클릭하거나 주소창에 입력하세요.")
    print(" 이 검은 창을 닫으면 관리 화면과 청취·퀴즈 참여가 멈춥니다.")
    print(" 켜 둔 채로 최소화하세요. 끝낼 때는 이 창에서 Ctrl+C 를 두 번 (3초 안에).")
    print(f" 데이터 위치: {db.data_dir()}  (업데이트 파일을 어디에 풀어도 그대로 남음)")
    if port != remote.TUNNEL_PORT:
        print(f" [주의] 밖에서 보기(radio.haetsallab.com)는 {remote.TUNNEL_PORT}번으로 들어옵니다. "
              f"지금은 {port}번이라 밖에서는 안 열립니다 — {remote.TUNNEL_PORT}번이 비면 저절로 이어 붙입니다.")
    if db.ADOPTED_FROM:
        print(f" 예전 폴더의 경험·사연 데이터를 옮겨 왔습니다: {db.ADOPTED_FROM.parent}")
    print("=" * 60)
    server_log(f"시작 — {url}" + (" (비정상 종료 뒤 자동으로 다시 켬)" if args.restarted else ""))
    conn = db.connect()
    try:
        updating = bool(db.get_setting(conn, "update.started_at"))   # 업데이트를 설치하고 다시 켠 것
        db.set_setting(conn, "update.started_at", "")
        if args.restarted and updating:
            print(f"[업데이트] 새 버전 {__version__}(으)로 다시 켰습니다. 청취는 하던 대로 이어서 합니다.")
            db.log(conn, "update", f"새 버전 {__version__}(으)로 다시 켬")
        elif args.restarted:
            print("[재시작] 관리 화면이 예기치 않게 꺼져서 자동으로 다시 켰습니다. 열려 있던 화면은 그대로 쓰면 됩니다.")
            db.log(conn, "quizbot", "관리 화면이 예기치 않게 꺼져 자동으로 다시 켰습니다 (원인 기록: 데이터 폴더 logs/server.log)")
        listening = qconfig.get(conn, "live.active") == "1"
        if not args.restarted:
            # 처음 켤 때는 지난번 채널(예: 고릴라)로 저절로 시작하지 않고 '뭐로 들으시겠어요?'를 묻는다
            # (MBC·KBS를 들을 수도 있음). 오류로 꺼졌다가 저절로 다시 켤 때(--restarted)는 묻지 않고 하던 대로 이어 간다.
            ask_listen(conn, listening)
            listening = False
        elif qconfig.get(conn, "live.ask") == "1":
            listening = False   # 묻는 중에 다시 켜졌으면 계속 답을 기다린다
        if ((listening or (qconfig.get(conn, "quizbot.autostart") == "1" and qconfig.get(conn, "live.ask") != "1"))
                and not db.is_stopped(conn) and not qconfig.runner_alive(conn)):
            db.set_setting(conn, "quizbot.stop", "0")
            print(("청취를 이어서 시작합니다." if listening else "퀴즈 자동 참여도 함께 시작합니다.")
                  + f" (기록: {start_runner(conn)})")
    finally:
        conn.close()
    # 서버가 실제로 뜬 뒤에 브라우저를 연다 (먼저 열면 '연결할 수 없음' 화면이 뜬다)
    threading.Thread(target=self_check, args=(url, not args.no_browser), daemon=True).start()
    threading.Thread(target=watch_runner, daemon=True).start()
    threading.Thread(target=watch_updates, daemon=True).start()
    if port != remote.TUNNEL_PORT:
        threading.Thread(target=watch_tunnel_port, args=(app, port), daemon=True).start()
    signal.signal(signal.SIGINT, ctrl_c_handler())
    # 개발 서버 시작 안내(빨간 WARNING)와 접속 기록은 오류처럼 보여 감춘다. 오류·경고는 그대로 보인다.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    # 이 PC에서만 접속 가능하도록 127.0.0.1 에만 연다 (밖에서는 Cloudflare 터널 + 로그인).
    try:
        run_simple(server.BIND, port, app, threaded=True)   # Ctrl+C 는 안에서 받아 정상으로 끝남
    except Exception:
        server_log("오류로 멈춤:\n" + traceback.format_exc())
        print("[오류] 관리 화면이 오류로 멈췄습니다. 잠시 뒤 자동으로 다시 켭니다. (기록: 데이터 폴더 logs/server.log)")
        sys.exit(EXIT_CRASH)
    server_log("종료 (Ctrl+C 등 사용자가 끝냄)")


if __name__ == "__main__":
    main()
