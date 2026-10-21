"""동시 청취 — 한 PC 에서 여러 채널(방송국마다 하나: SBS 고릴라 · KBS 콩 · MBC mini · CBS 레인보우)을
함께 듣고 받아쓰고 보낸다.

소리 나누기 (0.54): 녹음 장치 칸을 비워 두면 '앱 소리'를 직접 받는다 (appaudio — 윈도우 프로세스 루프백,
설치·볼륨 믹서 설정 필요 없음, 여러 앱이 같은 스피커로 나와도 섞이지 않음). 그게 안 되는 PC 는 앱마다 출력 장치를
나누고(볼륨 믹서: 고릴라 → 스피커, 콩 → CABLE Input …) 그 장치 이름을 칸에 적는다. 가상 장치는 무료 VB-CABLE
(vbcable 이 자동 설치)로 하나 생긴다. 한 장치에 두 앱 소리가 섞이면 받아쓰기가 망가진다.

- 주 채널(1번): 지금까지처럼 '청취 시작'의 채널, 녹음 장치는 live.device (비우면 기본 스피커).
- 보조 채널(2~4번): live.lanes 에 {channel, device}. 청취 중이고 '동시 청취'가 켜져 있으면 채널마다
  보조 청취기(python -m radio_helper.quizbot lane --lane N)를 띄운다. 받아쓰기·자막 기록·퀴즈·사연·DJ 한마디·신청곡이
  채널마다 따로 돌고, 채팅 보내기는 공용 입력 잠금으로 하나씩 차례로 한다.
- 한 앱은 한 번에 한 채널만 재생·채팅하므로 같은 앱(예: 파워FM·러브FM 둘 다 고릴라)을 두 번 고르면 막는다.
- 상태는 lane2.* ~ lane4.* 설정에 따로 남긴다 (주 채널은 예전처럼 quizbot.*).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime

from .. import db
from . import config

EXTRA_LANES = ("2", "3", "4")   # 주 채널 + 3칸 = 방송국 넷 (SBS·MBC·KBS·CBS)
GPU_LARGE_LANES = 3       # 그래픽카드 large-v3 는 채널당 약 3~4GB 라 3칸까지 — 넘으면 auto 를 large-v3-turbo(약 2GB)로
LAUNCH_GRACE = 120        # 막 띄운 보조 청취기는 이 시간 동안 다시 띄우지 않는다
QUIET = 8                 # 장치 점검: 소리 크기(0~100)가 이보다 작으면 '소리 없음'
SAME_SOUND = 0.85         # 장치 점검: 두 장치 소리 흐름의 상관이 이보다 크면 '같은 소리'
TEST_KEY = "lanes.test"


def enabled(conn: sqlite3.Connection) -> bool:
    return config.get(conn, "live.multi") == "1"


def extras(conn: sqlite3.Connection) -> list[dict]:
    """보조 채널 설정 [{lane, channel, device}] (채널을 고른 것만)."""
    try:
        raw = json.loads(config.get(conn, "live.lanes") or "[]")
    except ValueError:
        raw = []
    out = []
    for lane, item in zip(EXTRA_LANES, raw if isinstance(raw, list) else []):
        if isinstance(item, dict) and (item.get("channel") or "").strip():
            out.append({"lane": lane, "channel": item["channel"].strip(), "device": (item.get("device") or "").strip()})
    return out


def all_lanes(conn: sqlite3.Connection) -> list[dict]:
    """듣는 채널 모두 (동시 청취를 끄면 주 채널 하나)."""
    first = {"lane": "", "channel": config.get(conn, "live.channel"), "device": config.get(conn, "live.device").strip()}
    return [first] + (extras(conn) if enabled(conn) else [])


def lane_of(conn: sqlite3.Connection, lane: str) -> dict | None:
    return next((x for x in all_lanes(conn) if x["lane"] == lane), None)


def stt_model(conn: sqlite3.Connection, model: str, device: str) -> str:
    """받아쓰기 모델: 칸마다 모델을 하나씩 올리므로, 그래픽카드로 4칸 이상 들으면 auto 를 작은 모델로 (16GB 에 맞게)."""
    if (model or "auto") == "auto" and device == "cuda" and len(all_lanes(conn)) > GPU_LARGE_LANES:
        return "large-v3-turbo"
    return model


# ── 녹음 방식 ────────────────────────────────────────────────────────
SOURCES = {"app": "앱 소리 (설치 없음)", "device": "출력 장치", "speaker": "기본 스피커"}


def app_audio_supported() -> bool:
    try:
        from . import appaudio
    except ImportError:          # 음성 인식 부품(numpy 등)이 아직 없음
        return False
    return appaudio.supported()


def device_mode(conn: sqlite3.Connection) -> bool:
    """동시 청취에서 칸에 적은 출력 장치로 녹음할지 (볼륨 믹서로 앱마다 장치를 나눴을 때만 켬, 기본 꺼짐)."""
    return config.get(conn, "live.device_mode") == "1"


def source(conn: sqlite3.Connection, item: dict, multi: bool | None = None) -> str:
    """녹음 방식: app(그 채널 앱 소리만) · device(칸에 적은 출력 장치) · speaker(기본 스피커).
    동시 청취는 앱 소리를 받을 수 있으면 칸에 장치를 적어 두었어도 앱 소리 (0.56 — 모든 앱이 스피커로 나올 때
    다른 장치를 고르면 소리 0, 스피커를 고르면 여러 방송이 섞였음). '출력 장치로 나눠 녹음'을 켰을 때만 장치.
    한 채널만 들을 때는 예전처럼 적은 장치, 비우면 기본 스피커."""
    multi = enabled(conn) if multi is None else multi
    if multi and app_audio_supported() and not device_mode(conn):
        return "app"
    return "device" if (item.get("device") or "").strip() else "speaker"


def source_label(conn: sqlite3.Connection, item: dict) -> str:
    kind = source(conn, item)
    return item["device"].strip() if kind == "device" else SOURCES[kind]


def recorder(conn: sqlite3.Connection, item: dict, chunk_seconds: int):
    """그 칸의 녹음기 (with 로 열고 read_chunk)."""
    from .audio import LoopbackRecorder

    kind = source(conn, item)
    if kind == "app":
        from . import appaudio

        app = config.chat_app(conn, item["channel"])
        return appaudio.AppLoopbackRecorder(chunk_seconds, lambda: appaudio.app_process(conn, app))
    return LoopbackRecorder(chunk_seconds, device=item["device"] if kind == "device" else "")


def problems(conn: sqlite3.Connection, lanes: list[dict]) -> list[str]:
    """같이 들을 수 없는 조합 (순수 계산에 가까움): 같은 채널·같은 앱·같은 녹음 장치."""
    out, apps, devices, channels = [], {}, {}, set()
    for x in lanes:
        name = x["channel"]
        if name in channels:
            out.append(f"'{name}'을(를) 두 번 골랐습니다")
            continue
        channels.add(name)
        app = config.chat_app(conn, name)
        if app and app in apps:
            out.append(f"'{apps[app]}'와(과) '{name}'은(는) 둘 다 {config.app_label(app)} 앱이라 함께 들을 수 없습니다 "
                       "(한 앱은 한 번에 한 채널만 재생·채팅)")
        elif app:
            apps[app] = name
        kind = source(conn, x, multi=True)
        device = f"app:{app or name}" if kind == "app" else ((x.get("device") or "").strip().lower() or "(기본 스피커)")
        if device in devices:
            out.append(f"'{devices[device]}'와(과) '{name}'이(가) 같은 녹음 장치를 씁니다 — 소리가 섞입니다. "
                       "이 PC 는 앱 소리 직접 받기가 안 되니, '가상 장치 자동 설치'로 장치를 만들고 볼륨 믹서에서 "
                       "앱마다 출력 장치를 나눈 뒤 여기서도 다른 장치를 적으세요")
        else:
            devices[device] = name
    return out


def key(lane: str, name: str) -> str:
    """상태 설정 이름: 주 채널은 quizbot.<name>, 보조 채널은 lane<N>.<name>."""
    return f"quizbot.{name}" if not lane else f"lane{lane}.{name}"


def should_run(conn: sqlite3.Connection, lane: str) -> bool:
    """보조 청취기가 계속 들어야 하는지: 청취 중 · 동시 청취 켬 · 그 칸에 채널이 있음 · 멈춤 요청 없음."""
    return (config.get(conn, "live.active") == "1" and enabled(conn) and not db.is_stopped(conn)
            and db.get_setting(conn, "quizbot.stop") != "1" and lane_of(conn, lane) is not None)


def status(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """화면용: 채널마다 지금 상태."""
    now = now or datetime.now()
    out = []
    for x in all_lanes(conn):
        hb = db.get_setting(conn, key(x["lane"], "heartbeat")) or ""
        try:
            age = (now - datetime.strptime(hb, "%Y-%m-%d %H:%M:%S")).total_seconds()
        except ValueError:
            age = None
        out.append({**x, "label": "주 채널" if not x["lane"] else f"보조 {x['lane']}",
                    "source": source(conn, x), "source_label": source_label(conn, x),
                    "recording": db.get_setting(conn, key(x["lane"], "recording")) or "",
                    "app": config.app_label(config.chat_app(conn, x["channel"])),
                    "state": db.get_setting(conn, key(x["lane"], "state")) or "",
                    "alive": age is not None and age < config.HEARTBEAT_FRESH_SECONDS,
                    "level": int(db.get_setting(conn, key(x["lane"], "level")) or 0)})
    return out


def ensure(conn: sqlite3.Connection, launch, now: float | None = None) -> list[str]:
    """청취 중이고 동시 청취가 켜져 있으면 꺼져 있는 보조 청취기를 띄운다 (관리 화면이 30초마다 부름). 띄운 칸들."""
    now = now if now is not None else time.time()
    started = []
    if config.get(conn, "live.active") != "1" or not enabled(conn) or db.is_stopped(conn):
        return started
    for x in extras(conn):
        lane = x["lane"]
        try:
            last = float(db.get_setting(conn, key(lane, "launched")) or 0)
        except ValueError:
            last = 0.0
        if now - last < LAUNCH_GRACE:
            continue
        lock = config.instance_lock(f"quizbot_lane{lane}")
        if lock is None:
            continue              # 그 칸의 보조 청취기가 살아 있음
        lock.close()
        db.set_setting(conn, key(lane, "launched"), str(now))
        db.set_setting(conn, key(lane, "state"), "시작 중")
        log_name = launch(["lane", "--lane", lane])
        db.log(conn, "quizbot", f"동시 청취: 보조 {lane} '{x['channel']}' 청취기를 띄웠습니다 (기록: {log_name})")
        started.append(lane)
    return started


# ── 녹음 장치 ────────────────────────────────────────────────────────
def devices() -> list[str]:
    """루프백 녹음할 수 있는 출력 장치 이름 (윈도우 전용, 못 읽으면 빈 목록)."""
    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        return []
    pa = pyaudio.PyAudio()
    try:
        return [d["name"].replace(" [Loopback]", "") for d in pa.get_loopback_device_info_generator()]
    except Exception:
        return []
    finally:
        pa.terminate()


def envelope(samples, step: int = 1600) -> list[float]:
    """0.1초마다의 소리 크기 흐름 (두 장치 소리가 같은지 견주기용)."""
    import numpy as np

    if samples is None or len(samples) < step:
        return []
    usable = len(samples) - len(samples) % step
    return [float(v) for v in np.sqrt(np.mean(np.square(samples[:usable].reshape(-1, step), dtype=np.float64), axis=1))]


def similarity(a: list[float], b: list[float]) -> float:
    """두 소리 흐름의 상관 (-1~1). 길이가 모자라거나 한쪽이 조용하면 0."""
    import numpy as np

    n = min(len(a), len(b))
    if n < 10:
        return 0.0
    x, y = np.array(a[:n]), np.array(b[:n])
    if x.std() < 1e-6 or y.std() < 1e-6:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def judge(items: list[dict], gpu_ok: bool) -> list[str]:
    """장치 점검 결과 → 안내 (순수 계산). items: [{label, channel, device, source, level, envelope}]"""
    out = []
    for it in items:
        if it.get("error"):
            out.append(f"{it['label']} '{it['channel']}': 녹음 장치를 열지 못함 — {it['error']}")
        elif it["level"] < QUIET and it.get("source") == "app" and (it.get("app_peak") or 0) >= QUIET:
            out.append(f"{it['label']} '{it['channel']}': {it.get('app_process') or '앱'} 은(는) 소리를 내는데(소리 "
                       f"{it['app_peak']}) 받지 못했습니다 — 이 PC 에서 앱 소리 받기가 안 되는 것 같습니다. 이 화면을 찍어 "
                       "보내 주세요 (그동안은 '출력 장치로 나눠 녹음' + 가상 장치로 들을 수 있음)")
        elif it["level"] < QUIET and it.get("source") == "app":
            out.append(f"{it['label']} '{it['channel']}': 소리가 없습니다 — 그 앱이 켜져 있고 채널을 재생 중인지 확인하세요 "
                       "(앱 안의 볼륨·음소거도)" + ("" if it.get("app_process") else
                                                  ". 그 앱의 소리 길이 아래 '지금 소리 내는 프로그램'에 없습니다"))
        elif it["level"] < QUIET:
            out.append(f"{it['label']} '{it['channel']}': 소리가 없습니다 — 그 앱이 재생 중인지, 윈도우 볼륨 믹서에서 그 앱의 "
                       f"출력 장치를 '{it['device'] or '기본 스피커'}'(으)로 지정했는지 확인하세요")
    loud = [it for it in items if not it.get("error") and it["level"] >= QUIET]
    for i, a in enumerate(loud):
        for b in loud[i + 1:]:
            if similarity(a.get("envelope") or [], b.get("envelope") or []) >= SAME_SOUND:
                if a.get("source") == "app" and b.get("source") == "app":
                    out.append(f"'{a['channel']}'와(과) '{b['channel']}'에 같은 소리가 들어옵니다 — 두 앱이 같은 방송을 "
                               "틀고 있는지 확인하세요")
                else:
                    out.append(f"'{a['channel']}'와(과) '{b['channel']}' 장치에 같은 소리가 들어옵니다 — 앱별 출력 장치가 "
                               "제대로 나뉘지 않았습니다 (볼륨 믹서에서 앱마다 다른 장치)")
    if len(items) > 1 and not gpu_ok:
        out.append("그래픽카드 받아쓰기가 꺼져 있어(CPU) 여러 채널을 함께 받아쓰면 녹음보다 느릴 수 있습니다 — "
                   "'그래픽카드 다시 점검'을 해 보고, 그래도 CPU면 모델을 small 로 낮추세요")
    return out


def scan_sessions() -> tuple[dict, list[dict]]:
    """(프로세스 표, 지금 소리 길을 연 프로그램들) — 윈도우가 아니거나 못 읽으면 빈 것."""
    try:
        from . import appaudio

        return appaudio.processes(), appaudio.audio_sessions()
    except Exception:
        return {}, []


def device_test(conn: sqlite3.Connection, seconds: int = 10, recorder_factory=None, transcriber=None,
                gpu_ok: bool | None = None, scan=None) -> dict:
    """채널마다 그 녹음(앱 소리 또는 장치)을 동시에 seconds 초 → 소리 크기 · 받아쓴 말 · 같은 소리 겹침 · 그래픽카드 안내.
    녹음하는 동안 '지금 소리 내는 프로그램'(볼륨 믹서의 앱별 소리 크기)도 함께 재서, 앱이 소리를 내는데 못 받는지
    앱이 소리를 안 내는지 가린다. recorder_factory(칸) → 녹음기 (기본: recorder), scan() → (프로세스 표, 세션들)."""
    from . import live

    lanes = all_lanes(conn)
    scanned: dict = {}

    def do_scan():
        time.sleep(min(2.0, seconds / 2))          # 녹음이 시작된 뒤 재기
        scanned["out"] = (scan or scan_sessions)()

    scanner = threading.Thread(target=do_scan, daemon=True)
    if recorder_factory is None:
        def recorder_factory(item):
            return recorder(conn, item, seconds)
    items = [{"label": "주 채널" if not x["lane"] else f"보조 {x['lane']}", **x, "source": source(conn, x),
              "level": 0, "text": "", "error": ""} for x in lanes]
    audio: dict[int, object] = {}

    def record(i, item):
        try:
            with recorder_factory(item) as rec:
                items[i]["device_name"] = getattr(rec, "device_name", item["device"])
                audio[i] = rec.read_chunk()
        except Exception as e:
            items[i]["error"] = f"{type(e).__name__}: {str(e)[:200]}"

    threads = [threading.Thread(target=record, args=(i, x), daemon=True) for i, x in enumerate(lanes)]
    for t in threads + [scanner]:
        t.start()
    for t in threads + [scanner]:
        t.join(seconds + 20)
    procs, sessions = scanned.get("out") or ({}, [])
    if any(it["source"] == "app" for it in items):
        from .appaudio import _app_hint, pick_session

        for it in items:
            if it["source"] != "app":
                continue
            app = config.chat_app(conn, it["channel"])
            hit = pick_session(procs, sessions, (config.get(conn, f"{app}.process_name") or "").lower(),
                               _app_hint(app)) if app else None
            it["app_process"] = procs[hit["pid"]][1] if hit else ""
            it["app_peak"] = int(round(hit["peak"] * 100)) if hit else None
    from .audio import rms

    for i, it in enumerate(items):
        samples = audio.get(i)
        if samples is None:
            continue
        it["level"] = live.level_percent(rms(samples))
        it["envelope"] = envelope(samples)
        if transcriber is not None and it["level"] >= QUIET:
            try:
                it["text"] = (transcriber(samples) or "")[:200]
            except Exception as e:
                it["text"] = f"(받아쓰기 실패: {type(e).__name__})"
    if gpu_ok is None:
        from . import gpu

        gpu_ok = bool((gpu.saved_check(conn) or {}).get("ok"))
    result = {"at": db.now(), "items": [{k: v for k, v in it.items() if k != "envelope"} for it in items],
              "warnings": judge(items, gpu_ok), "gpu_ok": gpu_ok,
              "sessions": [{"exe": procs.get(x["pid"], (0, "?"))[1], "pid": x["pid"], "peak": int(round(x["peak"] * 100)),
                            "active": x["active"]} for x in sessions[:12]]}
    db.set_setting(conn, TEST_KEY, json.dumps(result, ensure_ascii=False))
    db.log(conn, "quizbot", "동시 청취 장치 점검: " + " / ".join(
        f"{it['channel']} 소리 {it['level']}" + (f" '{it['text'][:20]}'" if it.get("text") else "") for it in items)
        + (" — " + " · ".join(result["warnings"])[:300] if result["warnings"] else " — 문제 없음"))
    return result


def saved_test(conn: sqlite3.Connection) -> dict | None:
    try:
        data = json.loads(db.get_setting(conn, TEST_KEY) or "null")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
