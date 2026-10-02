"""python -m radio_helper.quizbot <명령>

  run              예약에 따라 퀴즈 자동 참여 (관리 화면의 '시작' 버튼과 같음)
  check            필요한 구성 요소·API 키·고릴라 창·스피커 점검
  auto-setup       열린 창 중 고릴라 채팅창을 찾아 설정 저장 (읽기만 함)
  inspect-gorilla  고릴라 창의 화면 요소 목록 저장 (읽기만 함)
  select window    화면에 네모를 그려 고릴라 창 전체 영역 저장 (창 인식이 안 될 때)
  select input     화면에 네모를 그려 채팅 입력칸 영역 저장
  select send      화면에 네모를 그려 전송 버튼 영역 저장
  calibrate input  7초 뒤 마우스 위치를 채팅 입력칸 위치로 저장
  calibrate send   7초 뒤 마우스 위치를 전송 버튼 위치로 저장
  type-test        고릴라 입력칸에 '입력 테스트'를 넣기만 함 (보내지 않음)
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from .. import db
from . import config


def say(msg: str) -> None:
    print(msg, flush=True)


def cmd_run(conn) -> int:
    if config.runner_alive(conn):
        say("[중단] 퀴즈 자동 참여가 이미 실행 중입니다.")
        return 2
    from .runner import Runner, build_real_deps

    db.set_setting(conn, "quizbot.stop", "0")
    try:
        deps = build_real_deps(conn)
    except ImportError as e:
        say(f"[중단] 필요한 구성 요소가 설치되지 않았습니다: {e.name}. start_windows.bat 을 다시 실행하세요.")
        return 1
    db.log(conn, "quizbot", "퀴즈 자동 참여 시작")
    say("퀴즈 자동 참여를 시작합니다. 멈추려면 관리 화면에서 '멈춤'을 누르세요.")
    Runner(conn, deps).run_forever()
    db.log(conn, "quizbot", "퀴즈 자동 참여 멈춤")
    return 0


def cmd_check(conn) -> int:
    results = []

    def item(name, ok, detail=""):
        results.append({"name": name, "ok": bool(ok), "detail": detail})
        say(f"[{'OK' if ok else '확인 필요'}] {name} {detail}")

    for mod, label in [("pyaudiowpatch", "PC 소리 녹음 (PyAudioWPatch)"), ("pywinauto", "고릴라 화면 조작 (pywinauto)"),
                       ("faster_whisper", "음성 인식 (faster-whisper)"), ("anthropic", "Claude API (anthropic)")]:
        try:
            __import__(mod)
            item(label, True)
        except Exception as e:
            item(label, False, f"— 설치 안 됨 ({type(e).__name__})")
    from .answerer import get_api_key

    item("Claude API 키", bool(get_api_key()), "" if get_api_key() else "— 관리 화면 '퀴즈 자동'에서 저장하세요")
    try:
        from .gorilla import Gorilla

        w = Gorilla(config.GorillaConfig.load(conn)).find_window()
        item("고릴라 창", w is not None, f"— '{w.window_text()}'" if w is not None else "— 고릴라 PC 앱을 실행하세요")
    except Exception as e:
        item("고릴라 창", False, f"— {type(e).__name__}")
    try:
        from .audio import LoopbackRecorder

        with LoopbackRecorder(1) as rec:
            item("스피커 녹음 장치", True, f"— {rec.device_name}")
    except Exception as e:
        item("스피커 녹음 장치", False, f"— {type(e).__name__}: {str(e)[:80]}")
    db.set_setting(conn, "quizbot.check", json.dumps({"at": db.now(), "items": results}, ensure_ascii=False))
    db.log(conn, "quizbot", "환경 점검: " + ", ".join(f"{r['name']} {'OK' if r['ok'] else 'X'}" for r in results))
    return 0 if all(r["ok"] for r in results) else 1


def cmd_auto_setup(conn) -> int:
    from .gorilla import Gorilla, dump

    report = Gorilla(config.GorillaConfig.load(conn)).auto_setup()
    for key, value in report["settings"].items():
        db.set_setting(conn, key, value)
    out_dir = db.data_dir() / "inspect"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"gorilla_auto_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(dump(report), encoding="utf-8")
    db.log(conn, "gorilla", "자동 찾기: " + report["message"])
    say(report["message"])
    return 0 if report["settings"] else 1


def cmd_inspect(conn) -> int:
    from .gorilla import Gorilla, dump

    report = Gorilla(config.GorillaConfig.load(conn)).inspect()
    out_dir = db.data_dir() / "inspect"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"gorilla_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(dump(report), encoding="utf-8")
    db.log(conn, "gorilla", f"고릴라 창 점검: 입력칸(Edit) {report['edit_count']}개, 버튼 {len(report['button_names'])}종 → {out.name}")
    say(f"점검 보고서: {out}")
    return 0


def cmd_calibrate(conn, target: str) -> int:
    from .gorilla import Gorilla

    label = "채팅 입력칸" if target == "input" else "전송 버튼"
    for n in range(7, 0, -1):
        say(f"{n}초 뒤 마우스가 있는 곳을 고릴라 {label} 위치로 저장합니다…")
        time.sleep(1)
    settings, message = Gorilla.calibrate(target)
    for key, value in settings.items():
        db.set_setting(conn, key, value)
    db.log(conn, "gorilla", message)
    say(message)
    return 0


def cmd_select(conn, target: str) -> int:
    from .gorilla import Gorilla

    result = Gorilla(config.GorillaConfig.load(conn)).select(target)
    if result is None:
        say("취소했습니다. 아무것도 바꾸지 않았습니다.")
        return 1
    settings, message = result
    for key, value in settings.items():
        db.set_setting(conn, key, value)
    db.log(conn, "gorilla", message)
    say(message)
    return 0


def cmd_type_test(conn) -> int:
    from .gorilla import Gorilla

    msg = Gorilla(config.GorillaConfig.load(conn)).type_only("입력 테스트")
    db.log(conn, "gorilla", "입력 테스트: " + msg)
    say(msg)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="고릴라 퀴즈 자동 참여")
    ap.add_argument("command", choices=["run", "check", "auto-setup", "inspect-gorilla", "select", "calibrate", "type-test"])
    ap.add_argument("target", nargs="?", choices=["window", "input", "send"])
    args = ap.parse_args(argv)
    conn = db.connect()
    db.init_db(conn)
    try:
        if args.command == "run":
            return cmd_run(conn)
        if args.command == "check":
            return cmd_check(conn)
        if db.is_stopped(conn):
            say("[중단] 일괄 중지가 켜져 있습니다.")
            return 2
        if args.command == "auto-setup":
            return cmd_auto_setup(conn)
        if args.command == "inspect-gorilla":
            return cmd_inspect(conn)
        if args.command == "select":
            if not args.target:
                ap.error("select 에는 window, input, send 중 하나가 필요합니다.")
            return cmd_select(conn, args.target)
        if args.command == "calibrate":
            if args.target not in ("input", "send"):
                ap.error("calibrate 에는 input 또는 send 가 필요합니다.")
            return cmd_calibrate(conn, args.target)
        return cmd_type_test(conn)
    except Exception as e:
        first = (str(e).strip().splitlines() or [type(e).__name__])[0][:200]
        say(f"[오류] {type(e).__name__}: {first}")
        db.log(conn, "quizbot", f"{args.command} 오류: {first}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
