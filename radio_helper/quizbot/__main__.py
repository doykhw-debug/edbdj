"""python -m radio_helper.quizbot <명령>

  run              실행기: '청취 시작'이 켜져 있으면 바로 듣고, 아니면 예약에 따라 듣는다
  captions         항상 위에 뜨는 자막 창
  check            필요한 구성 요소·API 키·고릴라 창·스피커 점검
  auto-setup       열린 창 중 고릴라 채팅창을 찾아 설정 저장 (읽기만 함)
  inspect-gorilla  고릴라 창의 화면 요소 목록 저장 (읽기만 함)
  select window    화면에 네모를 그려 고릴라 창 전체 영역 저장 (창 인식이 안 될 때)
  select input     화면에 네모를 그려 채팅 입력칸 영역 저장
  select send      화면에 네모를 그려 전송 버튼 영역 저장
  calibrate input  7초 뒤 마우스 위치를 채팅 입력칸 위치로 저장
  calibrate send   7초 뒤 마우스 위치를 전송 버튼 위치로 저장
  send-test        채팅 앱에 시험 글('파워 FM 화이팅' 등)을 실제로 입력하고 전송까지 누름
  sms-check        휴대폰(USB) 문자 연결 점검
  sms-test         지금 채널 번호로 시험 문자를 실제로 보냄 (요금 발생)

  창·전송 도구는 --app gorilla|mini|kong 으로 앱을 고른다 (기본 gorilla).
  stt-test         PC 소리 10초를 녹음해 받아쓰기 시험 (단계별 결과를 관리 화면에 표시)
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import sys
import time

from .. import db
from . import config


def say(msg: str) -> None:
    print(msg, flush=True)


def cmd_run(conn) -> int:
    lock = config.instance_lock("quizbot_run")
    if lock is None:
        say("[중단] 듣기 실행기가 이미 실행 중입니다.")
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
    channel = config.get(conn, "live.channel")
    app = config.chat_app(conn, channel)
    if config.route(conn) == "sms" or app is None:
        number = config.sms_number(conn, channel)
        item(f"{channel} 문자 번호", bool(number), f"— {number}" if number else "— 청취 화면에서 문자 번호를 넣으세요")
    else:
        label = config.app_label(app)
        try:
            from .gorilla import Gorilla

            w = Gorilla(config.GorillaConfig.load(conn, app)).find_window()
            item(f"{label} 창 ({channel})", w is not None,
                 f"— '{w.window_text()}'" if w is not None else f"— {label} PC 앱을 실행하세요")
        except Exception as e:
            item(f"{label} 창 ({channel})", False, f"— {type(e).__name__}")
    try:
        from .audio import LoopbackRecorder

        with LoopbackRecorder(1) as rec:
            item("스피커 녹음 장치", True, f"— {rec.device_name}")
    except Exception as e:
        item("스피커 녹음 장치", False, f"— {type(e).__name__}: {str(e)[:80]}")
    db.set_setting(conn, "quizbot.check", json.dumps({"at": db.now(), "items": results}, ensure_ascii=False))
    db.log(conn, "quizbot", "환경 점검: " + ", ".join(f"{r['name']} {'OK' if r['ok'] else 'X'}" for r in results))
    return 0 if all(r["ok"] for r in results) else 1


def _save(conn, app: str, settings: dict, message: str) -> str:
    """화면 도구 결과를 그 앱의 설정으로 저장하고, 안내 문구의 앱 이름을 맞춘다."""
    for key, value in config.app_settings(app, settings).items():
        db.set_setting(conn, key, value)
    message = config.localize(app, message)
    db.log(conn, "gorilla", f"[{config.app_label(app)}] {message}")
    say(message)
    return message


def cmd_auto_setup(conn, app: str = "gorilla") -> int:
    from .gorilla import Gorilla, dump

    report = Gorilla(config.GorillaConfig.load(conn, app)).auto_setup()
    out_dir = db.data_dir() / "inspect"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{app}_auto_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(dump(report), encoding="utf-8")
    _save(conn, app, report["settings"], "자동 찾기: " + report["message"])
    return 0 if report["settings"] else 1


def cmd_inspect(conn, app: str = "gorilla") -> int:
    from .gorilla import Gorilla, dump

    report = Gorilla(config.GorillaConfig.load(conn, app)).inspect()
    out_dir = db.data_dir() / "inspect"
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"{app}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(dump(report), encoding="utf-8")
    _save(conn, app, {}, f"고릴라 창 점검: 입력칸(Edit) {report['edit_count']}개, 버튼 {len(report['button_names'])}종 → {out.name}")
    return 0


def cmd_calibrate(conn, target: str, app: str = "gorilla") -> int:
    from .gorilla import Gorilla

    label = "채팅 입력칸" if target == "input" else "전송 버튼"
    for n in range(7, 0, -1):
        say(f"{n}초 뒤 마우스가 있는 곳을 {config.app_label(app)} {label} 위치로 저장합니다…")
        time.sleep(1)
    settings, message = Gorilla.calibrate(target)
    _save(conn, app, settings, message)
    return 0


def cmd_select(conn, target: str, app: str = "gorilla") -> int:
    from .gorilla import Gorilla

    result = Gorilla(config.GorillaConfig.load(conn, app)).select(target)
    if result is None:
        say("취소했습니다. 아무것도 바꾸지 않았습니다.")
        return 1
    _save(conn, app, *result)
    return 0


def cmd_send_test(conn, app: str = "gorilla") -> int:
    from .. import quiz
    from .gorilla import Gorilla
    from .runner import shared_input_lock

    text = config.get(conn, f"{app}.test_message").strip() or config.CHAT_APPS[app]["test"]
    with shared_input_lock():
        result, msg = Gorilla(config.GorillaConfig.load(conn, app)).send_test(text)
    _save(conn, app, {}, f"전송 테스트 → {quiz.ENTRY_LABELS.get(result.status, result.status)}: {msg}")
    return 0 if result.status in ("entered", "posted") else 1


def cmd_sms_check(conn) -> int:
    from .sms import AdbSms

    phone = AdbSms.from_settings(conn)
    items = []

    def item(name, ok, detail=""):
        items.append({"name": name, "ok": bool(ok), "detail": detail})
        say(f"[{'OK' if ok else '확인 필요'}] {name} {detail}")

    item("adb (platform-tools)", phone.adb, phone.adb or "— 문자 설정 화면의 안내대로 platform-tools 를 풀어 두세요")
    if phone.adb:
        try:
            ok, detail = phone.status()
            item("휴대폰 연결 (USB 디버깅)", ok, detail)
            if ok:
                info = phone.info()
                item("휴대폰", True, info["model"])
                item("기본 문자 앱", bool(info["sms_app"]), info["sms_app"] or "확인 못 함")
        except Exception as e:
            item("휴대폰 연결 (USB 디버깅)", False, f"{type(e).__name__}: {str(e)[:150]}")
    numbers = {c: n for c, n in config.channels(conn).items() if n}
    item("채널별 문자 번호", bool(numbers), ", ".join(f"{c} {n}" for c, n in numbers.items()) or "없음")
    db.set_setting(conn, "sms.check", json.dumps({"at": db.now(), "items": items}, ensure_ascii=False))
    db.log(conn, "sms", "문자 연결 점검: " + ", ".join(f"{i['name']} {'OK' if i['ok'] else 'X'}" for i in items))
    return 0 if all(i["ok"] for i in items) else 1


def cmd_sms_test(conn) -> int:
    from .. import quiz
    from .sms import AdbSms, with_signature

    channel = config.get(conn, "live.channel")
    number = config.sms_number(conn, channel)
    if not number:
        say(f"[중단] '{channel}' 채널의 문자 번호가 없습니다. 청취 화면의 채널 목록에서 번호를 넣어 주세요.")
        db.log(conn, "sms", f"문자 전송 테스트 중단: {channel} 문자 번호 없음")
        return 1
    app = config.chat_app(conn, channel) or "gorilla"
    text = with_signature(config.get(conn, f"{app}.test_message").strip() or config.CHAT_APPS[app]["test"],
                          db.get_profile(conn).get("nickname"), config.get(conn, "sms.signature") != "0")
    result = AdbSms.from_settings(conn).send(number, text, shot="sms_test")
    msg = f"문자 전송 테스트 '{text}' → {number}: {quiz.ENTRY_LABELS.get(result.status, result.status)} ({result.detail})"
    db.log(conn, "sms", msg)
    say(msg)
    return 0 if result.status == "entered" else 1


def cmd_stt_test(conn) -> int:
    """받아쓰기 테스트: 녹음 장치 → 10초 녹음 → 모델 불러오기 → 받아쓰기. 단계마다 화면에 남긴다.

    프로그램이 도중에 꺼져도(예: 그래픽카드 라이브러리 문제) 어느 단계에서 멈췄는지 화면에 남도록
    단계를 시작할 때마다 저장한다.
    """
    from . import live

    result = {"at": db.now(), "running": True, "ok": False, "phase": "녹음 장치 여는 중", "steps": [],
              "text": "", "level": None}

    def save():
        db.set_setting(conn, "quizbot.stt_test", json.dumps(result, ensure_ascii=False))

    def phase(text):
        result["phase"] = text
        save()
        say(f"… {text}")

    def step(name, ok, detail=""):
        result["steps"].append({"name": name, "ok": bool(ok), "detail": detail})
        save()
        say(f"[{'OK' if ok else '확인 필요'}] {name} {detail}")

    save()
    try:
        from .audio import LoopbackRecorder, rms

        with LoopbackRecorder(10) as rec:
            step("녹음 장치", True, rec.device_name)
            phase("PC 소리 10초 녹음 중")
            audio = rec.read_chunk()
        level = live.level_percent(rms(audio))
        result["level"] = level
        step("10초 녹음", level > 0, f"소리 크기 {level}/100" + (
            "" if level > 0 else " — 소리가 들리지 않습니다. 고릴라 재생·음소거·기본 스피커(이어폰)를 확인하세요"))
        from .stt import WhisperTranscriber

        model, device = config.get(conn, "quizbot.whisper_model"), config.get(conn, "quizbot.whisper_device")
        phase(f"음성 인식 모델 불러오는 중 ({device} · {model}, 처음 한 번은 내려받기로 몇 분)")
        tr = WhisperTranscriber(model, "", device)
        step("음성 인식 모델", True, f"{tr.label} · {tr.load_seconds:.1f}초")
        phase("받아쓰는 중")
        started = time.monotonic()
        text = tr.transcribe(audio)
        took = time.monotonic() - started
        result["text"] = text
        step("받아쓰기", bool(text), f"{took:.1f}초 · " + (
            f"'{text[:150]}'" if text else "말소리를 찾지 못함 (음악만 나왔거나 소리가 너무 작음 — 진행자가 말할 때 다시 해 보세요)"))
        result["ok"] = bool(text)
    except Exception as e:
        step(result["phase"] or "실행", False, f"{type(e).__name__}: {str(e).strip()[:200]}")
    finally:
        result["running"], result["phase"] = False, ""
        save()
    db.log(conn, "quizbot", "받아쓰기 테스트: " + (f"성공 — '{result['text'][:60]}'" if result["ok"]
                                             else "실패 — " + (result["steps"][-1]["detail"] if result["steps"] else "")))
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="고릴라 퀴즈 자동 참여")
    ap.add_argument("command", choices=["run", "captions", "check", "stt-test", "auto-setup", "inspect-gorilla",
                                        "select", "calibrate", "send-test", "sms-check", "sms-test"])
    ap.add_argument("target", nargs="?", choices=["window", "input", "send"])
    ap.add_argument("--app", choices=list(config.CHAT_APPS), default="gorilla",
                    help="채팅 앱: gorilla(SBS 고릴라) / mini(MBC) / kong(KBS 콩)")
    args = ap.parse_args(argv)
    faulthandler.enable()  # 음성 인식 등 내부 라이브러리가 프로그램을 갑자기 끄면 그 위치를 기록 파일에 남긴다
    conn = db.connect()
    db.init_db(conn)
    try:
        if args.command == "run":
            return cmd_run(conn)
        if args.command == "captions":
            from .captions import run_window

            lock = config.instance_lock("captions")
            if lock is None:
                say("자막 창이 이미 열려 있습니다.")
                return 0
            run_window()
            return 0
        if args.command == "check":
            return cmd_check(conn)
        if args.command == "stt-test":
            return cmd_stt_test(conn)
        if args.command == "sms-check":
            return cmd_sms_check(conn)
        if db.is_stopped(conn):
            say("[중단] 일괄 중지가 켜져 있습니다.")
            return 2
        if args.command == "sms-test":
            return cmd_sms_test(conn)
        if args.command == "auto-setup":
            return cmd_auto_setup(conn, args.app)
        if args.command == "inspect-gorilla":
            return cmd_inspect(conn, args.app)
        if args.command == "select":
            if not args.target:
                ap.error("select 에는 window, input, send 중 하나가 필요합니다.")
            return cmd_select(conn, args.target, args.app)
        if args.command == "calibrate":
            if args.target not in ("input", "send"):
                ap.error("calibrate 에는 input 또는 send 가 필요합니다.")
            return cmd_calibrate(conn, args.target, args.app)
        return cmd_send_test(conn, args.app)
    except Exception as e:
        first = config.localize(args.app, (str(e).strip().splitlines() or [type(e).__name__])[0][:200])
        say(f"[오류] {type(e).__name__}: {first}")
        db.log(conn, "quizbot", f"{args.command} 오류: {first}")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
