"""동시 청취 — 한 PC 에서 여러 채널(방송국마다 하나: SBS 고릴라 · KBS 콩 · MBC mini)을 함께 듣고 받아쓰고 보낸다.

소리 나누기: 윈도우 설정 → 시스템 → 소리 → 볼륨 믹서에서 앱마다 출력 장치를 따로 고른다
(예: 고릴라 → 스피커, 콩 → 가상 장치 A, mini → 가상 장치 B). 가상 장치는 VB-Audio Virtual Cable 등을 설치하면 생긴다.
이 프로그램은 장치마다 따로 루프백 녹음한다. 한 장치에 두 앱 소리가 섞이면 받아쓰기가 망가진다.

- 주 채널(1번): 지금까지처럼 '청취 시작'의 채널, 녹음 장치는 live.device (비우면 기본 스피커).
- 보조 채널(2·3번): live.lanes 에 {channel, device}. 청취 중이고 '동시 청취'가 켜져 있으면 채널마다
  보조 청취기(python -m radio_helper.quizbot lane --lane N)를 띄운다. 받아쓰기·자막 기록·퀴즈·사연·DJ 한마디·신청곡이
  채널마다 따로 돌고, 채팅 보내기는 공용 입력 잠금으로 하나씩 차례로 한다.
- 한 앱은 한 번에 한 채널만 재생·채팅하므로 같은 앱(예: 파워FM·러브FM 둘 다 고릴라)을 두 번 고르면 막는다.
- 상태는 lane2.* / lane3.* 설정에 따로 남긴다 (주 채널은 예전처럼 quizbot.*).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime

from .. import db
from . import config

EXTRA_LANES = ("2", "3")
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
        device = (x.get("device") or "").strip().lower() or "(기본 스피커)"
        if device in devices:
            out.append(f"'{devices[device]}'와(과) '{name}'이(가) 같은 녹음 장치를 씁니다 — 소리가 섞입니다. "
                       "앱마다 출력 장치를 따로 지정하고 여기서도 다른 장치를 고르세요")
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
    """장치 점검 결과 → 안내 (순수 계산). items: [{label, channel, device, level, envelope}]"""
    out = []
    for it in items:
        if it.get("error"):
            out.append(f"{it['label']} '{it['channel']}': 녹음 장치를 열지 못함 — {it['error']}")
        elif it["level"] < QUIET:
            out.append(f"{it['label']} '{it['channel']}': 소리가 없습니다 — 그 앱이 재생 중인지, 윈도우 볼륨 믹서에서 그 앱의 "
                       f"출력 장치를 '{it['device'] or '기본 스피커'}'(으)로 지정했는지 확인하세요")
    loud = [it for it in items if not it.get("error") and it["level"] >= QUIET]
    for i, a in enumerate(loud):
        for b in loud[i + 1:]:
            if similarity(a.get("envelope") or [], b.get("envelope") or []) >= SAME_SOUND:
                out.append(f"'{a['channel']}'와(과) '{b['channel']}' 장치에 같은 소리가 들어옵니다 — 앱별 출력 장치가 "
                           "제대로 나뉘지 않았습니다 (볼륨 믹서에서 앱마다 다른 장치)")
    if len(items) > 1 and not gpu_ok:
        out.append("그래픽카드 받아쓰기가 꺼져 있어(CPU) 여러 채널을 함께 받아쓰면 녹음보다 느릴 수 있습니다 — "
                   "'그래픽카드 다시 점검'을 해 보고, 그래도 CPU면 모델을 small 로 낮추세요")
    return out


def device_test(conn: sqlite3.Connection, seconds: int = 10, recorder_factory=None, transcriber=None,
                gpu_ok: bool | None = None) -> dict:
    """채널마다 그 장치를 동시에 seconds 초 녹음 → 소리 크기 · 받아쓴 말 · 같은 소리 겹침 · 그래픽카드 안내."""
    from . import live

    lanes = all_lanes(conn)
    if recorder_factory is None:
        from .audio import LoopbackRecorder

        def recorder_factory(device):
            return LoopbackRecorder(seconds, device=device)
    items = [{"label": "주 채널" if not x["lane"] else f"보조 {x['lane']}", **x, "level": 0, "text": "", "error": ""}
             for x in lanes]
    audio: dict[int, object] = {}

    def record(i, device):
        try:
            with recorder_factory(device) as rec:
                items[i]["device_name"] = getattr(rec, "device_name", device)
                audio[i] = rec.read_chunk()
        except Exception as e:
            items[i]["error"] = f"{type(e).__name__}: {str(e)[:120]}"

    threads = [threading.Thread(target=record, args=(i, x["device"]), daemon=True) for i, x in enumerate(lanes)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(seconds + 20)
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
              "warnings": judge(items, gpu_ok), "gpu_ok": gpu_ok}
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
