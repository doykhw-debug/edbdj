"""소리가 끊겼을 때 원인을 가릴 자료 (0.61): 이 채널 앱의 소리 · 다른 앱들 · 윈도우 기본 스피커 · 마지막 입력 ·
그래픽카드 드라이버 재시작. '소리 끊김' 기록에 덧붙인다.

동시 청취에서 '모든 라디오가 함께' 끊기면 한 앱이 아니라 공통 원인(출력 장치·화면 꺼짐·그래픽카드·인터넷)이다.
"""

from __future__ import annotations

import re

from . import power

QUIET = 3            # 볼륨 믹서 소리 크기(0~100)가 이보다 작으면 소리 없음
IDLE_HINT = 300      # 마지막 입력이 이보다 오래면(초) 화면 꺼짐을 의심
VIRTUAL = re.compile(r"CABLE|VB-Audio|Virtual|가상", re.I)
OURS = ("python.exe", "pythonw.exe")   # 도우미의 녹음 (스피커 녹음은 그 장치 소리 전체를 재므로 앱 판단에서 뺌)


def is_virtual(name: str) -> bool:
    return bool(name and VIRTUAL.search(name))


def describe(app_label: str, mine: dict | None, others: list[dict], default_name: str = "",
             idle: float | None = None, resets: int | None = None, scanned: bool = True) -> str:
    """mine·others: {exe, peak(0~100), device}. scanned: 볼륨 믹서를 읽었는지 (못 읽었으면 앱 이야기는 뺌)."""
    out = []
    apps = [o for o in others if o["exe"] not in OURS]
    if scanned:
        if mine is None:
            out.append(f"지금 볼륨 믹서에 {app_label} 소리 길이 없습니다 — 앱이 재생을 멈췄거나 앱이 쓰던 출력 장치가 "
                       f"사라졌습니다(모니터가 꺼지면 모니터 스피커 장치도 사라짐). {app_label}에서 재생(▶)을 다시 누르세요.")
        elif mine["peak"] >= QUIET:
            out.append(f"지금 {mine['exe']} 은(는) 소리를 내고 있습니다(볼륨 믹서 {mine['peak']}) — 앱은 정상이고 "
                       "녹음 연결이 끊긴 것이라 다시 연결했습니다.")
        else:
            out.append(f"지금 {mine['exe']} 소리 {mine['peak']}"
                       + (f" ('{mine['device']}')" if mine["device"] else "") + " — 앱 자체가 소리를 내지 않습니다.")
        if len(apps) >= 2 and not any(o["peak"] >= QUIET for o in apps):
            out.append("볼륨 믹서에서 소리 내는 라디오 앱이 하나도 없습니다 — 모두 함께 멈췄다면 한 앱이 아니라 공통 원인"
                       "(출력 장치·화면 꺼짐·그래픽카드·인터넷)입니다.")
        if apps:
            out.append("볼륨 믹서: " + " · ".join(f"{o['exe']} {o['peak']}" + (f"({o['device']})" if o["device"] else "")
                                                for o in apps[:6]) + ".")
    if default_name:
        out.append(f"윈도우 기본 스피커: '{default_name}'"
                   + (" — 가상 장치라 출력을 '기본값'으로 둔 앱 소리는 귀에 들리지 않습니다." if is_virtual(default_name) else "."))
    if idle is not None:
        if idle >= IDLE_HINT:
            out.append(f"마지막 키보드·마우스 입력이 {idle / 60:.0f}분 전입니다 — 화면 꺼짐 시간이 지나 모니터가 꺼지면 "
                       "모니터 스피커 소리도 끊깁니다.")
        else:
            out.append(f"마지막 키보드·마우스 입력 {idle:.0f}초 전.")
    if resets:
        out.append(f"최근 10분 안에 그래픽카드 드라이버가 {resets}번 다시 시작됐습니다(윈도우 시스템 기록) — 이때 "
                   "모니터 스피커(HDMI·DP) 소리가 끊기고 앱이 재생을 멈출 수 있습니다. 동시 청취 칸 수를 줄이거나 "
                   "받아쓰기 모델을 낮춰 보세요.")
    return (" " + " ".join(out)) if out else ""


def collect(conn, channel: str | None, scan=None, idle=power.idle_seconds, resets=power.gpu_resets) -> str:
    """지금 PC 상태를 읽어 describe 로 (윈도우가 아니거나 못 읽은 것은 뺌)."""
    from . import config
    from .appaudio import _app_hint, pick_session

    try:
        if scan is None:
            from . import appaudio
            procs, sessions = appaudio.processes(), appaudio.audio_sessions()
        else:
            procs, sessions = scan()
    except Exception:
        procs, sessions = {}, []
    app = config.chat_app(conn, channel)
    label = config.app_label(app) if app else "그 채널 앱"

    def view(s):
        return {"exe": procs.get(s["pid"], (0, "?"))[1], "peak": int(round(s["peak"] * 100)), "device": s.get("device") or ""}

    hit = pick_session(procs, sessions, (config.get(conn, f"{app}.process_name") or "").lower(),
                       _app_hint(app)) if app and procs else None
    default_name = next((s["default_name"] for s in sessions if s.get("default_name")), "")
    try:
        quiet_for = idle()
    except Exception:
        quiet_for = None
    try:
        tdr = resets()
    except Exception:
        tdr = None
    return describe(label, view(hit) if hit else None, [view(s) for s in sessions], default_name, quiet_for, tdr,
                    scanned=bool(procs) and app is not None)
