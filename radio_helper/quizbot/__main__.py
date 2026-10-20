"""python -m radio_helper.quizbot <명령>

  run              실행기: '청취 시작'이 켜져 있으면 바로 듣고, 아니면 예약에 따라 듣는다
  captions         항상 위에 뜨는 자막 창
  check            필요한 구성 요소·Claude 연결(API 키/구독)·고릴라 창·스피커 점검
  auto-setup       열린 창 중 고릴라 채팅창을 찾아 설정 저장 (읽기만 함)
  inspect-gorilla  고릴라 창의 화면 요소 목록 저장 (읽기만 함)
  select window    화면에 네모를 그려 고릴라 창 전체 영역 저장 (창 인식이 안 될 때)
  select input     화면에 네모를 그려 채팅 입력칸 영역 저장
  select send      화면에 네모를 그려 전송 버튼 영역 저장
  select chat      화면에 네모를 그려 채팅 목록 영역 저장 (키워드가 들리면 읽어서 녹취와 함께 분석)
  chat-test        채팅 목록 영역을 지금 찍어 보기 (읽기만 함)
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


def ignore_ctrl_c() -> None:
    """검은 창에서 Ctrl+C 를 눌러도 듣기·자막·도구가 꺼지지 않게 한다 (기록에만 남김).
    멈출 때는 관리 화면의 '청취 중지'·'멈춤'을, 모두 끝낼 때는 검은 창을 닫는다."""
    import signal

    def handler(_signum, _frame):
        say(f"[{time.strftime('%H:%M:%S')}] Ctrl+C 신호를 받았지만 계속합니다 (멈추려면 관리 화면의 '청취 중지').")

    signal.signal(signal.SIGINT, handler)


def lower_priority() -> bool:
    """받아쓰기가 CPU 를 많이 써도 라디오 재생(고릴라 앱)이 먼저 돌도록 이 프로세스를 '낮음' 우선순위로 둔다."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        return bool(kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x4000))  # BELOW_NORMAL_PRIORITY_CLASS
    except Exception:
        return False


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


def cmd_lane(conn, lane: str) -> int:
    """동시 청취의 보조 채널 청취기 (관리 화면이 청취 중에 띄움)."""
    from . import lanes
    from .runner import Runner, build_real_deps

    lock = config.instance_lock(f"quizbot_lane{lane}")
    if lock is None:
        say(f"[중단] 보조 {lane} 청취기가 이미 실행 중입니다.")
        return 2
    target = lanes.lane_of(conn, lane)
    if target is None or not lanes.should_run(conn, lane):
        say(f"보조 {lane}: 동시 청취가 꺼져 있거나 채널이 없어 끝냅니다.")
        return 0
    try:
        deps = build_real_deps(conn, device=target["device"])
    except ImportError as e:
        say(f"[중단] 필요한 구성 요소가 설치되지 않았습니다: {e.name}. start_windows.bat 을 다시 실행하세요.")
        return 1
    say(f"보조 {lane}: {target['channel']} 을(를) '{target['device'] or '기본 스피커'}' 장치로 듣습니다.")
    Runner(conn, deps, lane=lane).run_lane()
    return 0


def cmd_device_test(conn) -> int:
    """동시 청취 장치 점검: 채널마다 그 장치를 동시에 10초 녹음 → 소리 크기·받아쓴 말·겹침."""
    from . import gpu, lanes
    from .stt import WhisperTranscriber

    db.set_setting(conn, lanes.TEST_KEY, json.dumps({"at": db.now(), "running": True}, ensure_ascii=False))
    say("채널마다 10초씩 함께 녹음합니다…")
    device, note = gpu.choose_device(conn, config.get(conn, "quizbot.whisper_device"))
    holder = {}

    def transcribe(samples):
        if "tr" not in holder:
            holder["tr"] = WhisperTranscriber(config.get(conn, "quizbot.whisper_model"), "", device,
                                              beam_size=config.whisper_beam(conn, device))
        return holder["tr"].transcribe(samples)

    result = lanes.device_test(conn, transcriber=transcribe, gpu_ok=device == "cuda")
    for it in result["items"]:
        say(f"{it['label']} {it['channel']} [{it.get('device_name') or it['device'] or '기본 스피커'}] 소리 {it['level']}"
            f" → '{it.get('text') or it.get('error') or ''}'")
    for w in result["warnings"]:
        say(f"[확인] {w}")
    say(f"받아쓰기 장치: {'그래픽카드' if device == 'cuda' else 'CPU'} ({note})")
    return 0 if not result["warnings"] else 1


def cmd_gpu_recheck(conn) -> int:
    """그래픽카드 받아쓰기를 지금 다시 점검한다 (저장해 둔 결과를 지우고)."""
    from . import gpu

    db.set_setting(conn, gpu.CHECK_KEY, "")
    say(f"라이브러리: {gpu.libs_signature()}")
    device, note = gpu.choose_device(conn, "auto")
    say(f"결과: {'그래픽카드' if device == 'cuda' else 'CPU'} — {note}")
    return 0 if device == "cuda" else 1


def cmd_check(conn) -> int:
    results = []

    def item(name, ok, detail=""):
        results.append({"name": name, "ok": bool(ok), "detail": detail})
        say(f"[{'OK' if ok else '확인 필요'}] {name} {detail}")

    from . import answerer, claude_cli

    subscription = answerer.billing(conn) == "subscription"
    parts = [("pyaudiowpatch", "PC 소리 녹음 (PyAudioWPatch)"), ("pywinauto", "고릴라 화면 조작 (pywinauto)"),
             ("faster_whisper", "음성 인식 (faster-whisper)")]
    if not subscription:   # 구독(Claude Code)으로 쓰면 API 부품은 필요 없다
        parts.append(("anthropic", "Claude API 부품 (anthropic, API 키 방식)"))
    for mod, label in parts:
        try:
            __import__(mod)
            item(label, True)
        except Exception as e:
            item(label, False, f"— 설치 안 됨 ({type(e).__name__})")
    if subscription:
        cli = claude_cli.status()
        claude_cli.save_check(conn, cli)
        item("Claude 구독 (Claude Code 로그인)", bool(cli["subscription"]), "— " + cli["detail"])
    else:
        has_key = bool(answerer.get_api_key())
        item("Claude 연결: API 키 (충전 요금)", has_key,
             "" if has_key else "— 설정 → Claude 연결에서 'Claude 구독'을 고르거나 API 키를 저장하세요")
    channel = config.get(conn, "live.channel")
    app = config.chat_app(conn, channel)
    if config.route(conn) == "sms" or app is None:
        number = config.sms_number(conn, channel)
        item(f"{channel} 문자 번호", bool(number), f"— {number}" if number else "— 청취 화면에서 문자 번호를 넣으세요")
    else:
        label = config.app_label(app)
        try:
            from .gorilla import Gorilla

            from . import finder

            sender = Gorilla(config.GorillaConfig.load(conn, app))
            ok = sender.is_running() or sender.find_window() is not None
            if ok:
                item(f"{label} 창 ({channel})", True, "— 채팅 입력 자리 확인됨")
            elif finder.running(conn, app):
                item(f"{label} 창 ({channel})", False,
                     f"— {label} 앱은 켜져 있지만 채팅 창(입력칸) 자리를 못 찾음: 채팅이 닫혔거나 창을 옮김 → "
                     "'앱 채팅·인식 설정'의 '원버튼 찾기'")
            else:
                item(f"{label} 창 ({channel})", False, f"— {label} PC 앱이 꺼져 있습니다. 켜고 채팅 화면을 여세요")
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


def cmd_find(conn, app: str = "gorilla", every: bool = False) -> int:
    """원버튼 찾기: 앱이 켜져 있는지 → 입력칸·전송 버튼·채팅 목록 자리 → 저장 + 확인 사진 (every: 세 앱 모두)."""
    from . import finder
    from .runner import shared_input_lock

    apps = list(config.CHAT_APPS) if every else [app]
    found_any = False
    for one in apps:
        say(f"{config.app_label(one)} 찾는 중…")
        with shared_input_lock():   # 보내는 중에 창을 앞으로 가져오거나 누르지 않게
            report = finder.find(conn, one, use_claude=True, click_toggle=True)
        report["file"] = finder.save_report(one, report)
        _save(conn, one, report["settings"], "원버튼 찾기: " + report["message"])
        if report["settings"]:
            db.set_setting(conn, f"{one}.auto_found_at", "0")
            found_any = True
    return 0 if found_any else 1


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

    from .. import evidence

    text = config.get(conn, f"{app}.test_message").strip() or config.CHAT_APPS[app]["test"]
    with shared_input_lock():
        result, msg = Gorilla(config.GorillaConfig.load(conn, app)).send_test(text)
    if result.shots:   # 메뉴 '증거 사진'의 '전송 테스트'에도 남긴다
        evidence.save(conn, "tests", 0, app, text, result.status, result.shots)
        msg += f" (증거 사진 화면에 {len(result.shots)}장 저장)"
    _save(conn, app, {}, f"전송 테스트 → {quiz.ENTRY_LABELS.get(result.status, result.status)}: {msg}")
    return 0 if result.status in ("entered", "posted") else 1


def cmd_chat_test(conn, app: str = "gorilla") -> int:
    """채팅창 영역을 지금 찍어 본다 (읽기만 함). 키워드가 들리면 이렇게 찍은 화면을 녹취와 함께 분석한다."""
    from .gorilla import Gorilla

    if not config.get(conn, f"{app}.chat_rect"):
        _save(conn, app, {}, "채팅창 영역이 없습니다. '④ 채팅창 영역 지정'을 먼저 하세요.")
        return 1
    data = Gorilla(config.GorillaConfig.load(conn, app)).chat_image(save_as=f"{app}_chat")
    _save(conn, app, {}, f"채팅창 읽기 테스트: {len(data) // 1024 if data else 0}KB 사진 → {app}_chat.png "
                         "(글자가 읽히는지 관리 화면에서 확인하세요)")
    return 0 if data else 1


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


def level_advice(level: int, quiet: int) -> str:
    """녹음된 소리 크기에 따른 안내. 윈도우 음소거는 녹음에 영향이 없고, 앱 안 볼륨은 그대로 줄어든다."""
    if level <= 0:
        return (" — 소리가 들리지 않습니다. 고릴라가 재생 중인지, 고릴라 앱 안 볼륨이 0이 아닌지, "
                "기본 스피커(이어폰)가 맞는지 확인하세요")
    if level < quiet:
        return (" — 소리가 작습니다. 받아쓰기 전에 자동으로 키우지만, 고릴라 앱 안 볼륨과 윈도우 '볼륨 믹서'의 "
                "고릴라 볼륨은 최대로 두세요. 소리를 듣기 싫으면 볼륨을 내리지 말고 스피커 음소거를 쓰세요")
    return ""


def cmd_stt_test(conn) -> int:
    """받아쓰기 테스트: 녹음 장치 → 10초 녹음 → 모델 불러오기 → 받아쓰기. 단계마다 화면에 남긴다.

    프로그램이 도중에 꺼져도(예: 그래픽카드 라이브러리 문제) 어느 단계에서 멈췄는지 화면에 남도록
    단계를 시작할 때마다 저장한다.
    """
    from . import live

    lock = config.instance_lock(live.STT_TEST_LOCK)   # 화면이 '진행 중'인지 '꺼짐'인지 알 수 있게 잡고 있는다
    if lock is None:
        say("받아쓰기 테스트가 이미 진행 중입니다.")
        return 0
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
        from .audio import QUIET_LEVEL, LoopbackRecorder, rms

        with LoopbackRecorder(10) as rec:
            step("녹음 장치", True, rec.device_name)
            phase("PC 소리 10초 녹음 중")
            audio = rec.read_chunk()
        level = live.level_percent(rms(audio))
        result["level"] = level
        step("10초 녹음", level > 0, f"소리 크기 {level}/100" + level_advice(level, QUIET_LEVEL))
        from . import stt

        config.migrate_stt_defaults(conn)
        config.migrate_defaults(conn)
        model, device = config.get(conn, "quizbot.whisper_model"), config.get(conn, "quizbot.whisper_device")
        phase(f"음성 인식 준비 중 ({device} · {model}, 처음 한 번은 그래픽카드 점검과 모델 내려받기로 몇 분)")
        from . import gpu

        device, note = gpu.choose_device(conn, device)
        tr = stt.WhisperTranscriber(model, "", device, beam_size=config.whisper_beam(conn, device),
                                    hints=stt.build_hints((), config.get(conn, "quizbot.stt_hints")))
        step("음성 인식 모델", True, f"{tr.label} · {tr.load_seconds:.1f}초 · {note}")
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


def cmd_enrich(conn) -> int:
    """관계도 실제 사건을 사연처럼 풀어 쓴다 (Claude — 설정한 방식: 구독 또는 API 키)."""
    from . import enrich
    from .answerer import make, ready

    lock = config.instance_lock("enrich")
    if lock is None:
        say("이미 풀어 쓰는 중입니다.")
        return 0
    ok, why = ready(conn)
    if not ok:
        enrich.save_status(conn, running=False, done=0, kept=0, total=len(enrich.pending(conn)), error=why)
        say(f"[중단] {why}")
        return 2
    answerer = make(conn)
    done, kept = enrich.run(conn, answerer.enrich_events, say)
    say(f"끝: {done}건 풀어 씀, {kept}건은 원래 한 줄 유지")
    return 0


def cmd_gpu_check() -> int:
    """그래픽카드로 받아쓰기가 되는지 시험한다. 실행기와 다른 프로세스에서 돌려, 라이브러리 문제로 꺼져도 영향이 없게 한다."""
    import numpy as np

    from .stt import add_nvidia_dll_dirs

    add_nvidia_dll_dirs()
    import ctranslate2

    if ctranslate2.get_cuda_device_count() < 1:
        print("NO_CUDA_DEVICE", flush=True)
        return 2
    from faster_whisper import WhisperModel

    model = WhisperModel("tiny", device="cuda", compute_type="float16")
    segments, _info = model.transcribe(np.zeros(16_000, dtype=np.float32), language="ko")
    list(segments)
    print("GPU_OK", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="고릴라 퀴즈 자동 참여")
    ap.add_argument("command", choices=["run", "lane", "device-test", "gpu-recheck", "captions", "check", "stt-test",
                                        "auto-setup", "find", "inspect-gorilla",
                                        "select", "calibrate", "send-test", "chat-test", "sms-check", "sms-test",
                                        "gpu-check", "enrich"])
    ap.add_argument("target", nargs="?", choices=["window", "input", "send", "chat"])
    ap.add_argument("--app", choices=list(config.CHAT_APPS), default="gorilla",
                    help="채팅 앱: gorilla(SBS 고릴라) / mini(MBC) / kong(KBS 콩)")
    ap.add_argument("--all", action="store_true", help="find: 세 앱(고릴라·콩·mini) 모두")
    ap.add_argument("--lane", choices=["2", "3"], help="lane: 동시 청취의 보조 채널 칸")
    args = ap.parse_args(argv)
    faulthandler.enable()  # 음성 인식 등 내부 라이브러리가 프로그램을 갑자기 끄면 그 위치를 기록 파일에 남긴다
    ignore_ctrl_c()
    if args.command in ("run", "lane", "stt-test", "gpu-check", "device-test"):
        lower_priority()
    if args.command == "gpu-check":
        return cmd_gpu_check()
    conn = db.connect()
    db.init_db(conn)
    try:
        if args.command == "run":
            return cmd_run(conn)
        if args.command == "lane":
            if not args.lane:
                ap.error("lane 에는 --lane 2 또는 3 이 필요합니다.")
            return cmd_lane(conn, args.lane)
        if args.command == "device-test":
            return cmd_device_test(conn)
        if args.command == "gpu-recheck":
            return cmd_gpu_recheck(conn)
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
        if args.command == "enrich":
            return cmd_enrich(conn)
        if db.is_stopped(conn):
            say("[중단] 일괄 중지가 켜져 있습니다.")
            return 2
        if args.command == "sms-test":
            return cmd_sms_test(conn)
        if args.command == "auto-setup":
            return cmd_auto_setup(conn, args.app)
        if args.command == "find":
            return cmd_find(conn, args.app, args.all)
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
        if args.command == "chat-test":
            return cmd_chat_test(conn, args.app)
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
