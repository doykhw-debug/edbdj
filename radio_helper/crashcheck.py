"""PC 다운 원인 찾기 (1.00 사용자 요청: '중간에 피시가 다운되는 현상 — 이유를 찾아줘').

윈도우가 스스로 남기는 시스템 기록(이벤트 뷰어)에서 최근 며칠의 '갑자기 꺼짐·블루스크린·하드웨어 오류·그래픽 드라이버 멈춤·
메모리 부족·프로그램 강제 종료'를 모아 쉬운 말로 보여 주고, 지금 메모리·C 드라이브·많이 쓰는 프로그램을 함께 적는다.
읽기만 하고 아무것도 바꾸지 않는다. 윈도우가 아니면 하지 않는다.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

DAYS = 7
CREATE_NO_WINDOW = 0x08000000
NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"

# (열쇠, 기록, 보낸 곳, 번호, 이름, 뜻)
CATS = [
    ("bugcheck", "System", "Microsoft-Windows-WER-SystemErrorReporting", 1001, "블루스크린",
     "윈도우가 심각한 오류로 멈추고 다시 시작했습니다. 아래 오류 번호로 원인을 짐작합니다."),
    ("power41", "System", "Microsoft-Windows-Kernel-Power", 41, "갑자기 꺼짐(정상 종료 아님)",
     "정상적으로 끄지 않았는데 다시 켜졌습니다. 오류 번호가 0이면 블루스크린 없이 꺼진 것 — 전원이 끊겼거나, "
     "완전히 멈춰 전원 버튼으로 끈 경우입니다."),
    ("whea", "System", "Microsoft-Windows-WHEA-Logger", None, "하드웨어 오류",
     "CPU·메모리·그래픽카드 연결(PCIe)에서 오류가 났습니다. 발열·전원(파워)·오버클럭(XMP 등)을 의심합니다."),
    ("tdr", "System", "Display", 4101, "그래픽 드라이버 멈춤·복구",
     "그래픽카드 드라이버가 응답하지 않아 윈도우가 다시 시작시켰습니다. 자주 나면 화면이 멈추거나 블루스크린으로 이어집니다."),
    ("nvidia", "System", "nvlddmkm", None, "NVIDIA 드라이버 오류",
     "NVIDIA 그래픽 드라이버가 오류를 남겼습니다 (그래픽카드에 일이 몰리거나 드라이버 문제)."),
    ("memory", "System", "Microsoft-Windows-Resource-Exhaustion-Detector", 2004, "메모리 부족",
     "메모리(실제 메모리 + 가상 메모리 파일)가 바닥났습니다. 이때 PC 가 멈춘 것처럼 느려지거나 프로그램이 꺼집니다. "
     "기록에 메모리를 가장 많이 쓴 프로그램이 적혀 있습니다."),
    ("apperror", "Application", "Application Error", 1000, "프로그램 강제 종료",
     "프로그램이 오류로 꺼졌습니다 (어느 프로그램인지 아래에)."),
    ("apphang", "Application", "Application Hang", 1002, "프로그램 응답 없음",
     "프로그램이 멈춰 '응답 없음'으로 닫혔습니다."),
]

BUGCHECKS = {
    0x116: "그래픽 드라이버가 끝내 복구되지 않음 (VIDEO_TDR_FAILURE) — 그래픽카드 과부하·드라이버",
    0x117: "그래픽 드라이버 멈춤 (VIDEO_TDR_TIMEOUT) — 그래픽카드 과부하·드라이버",
    0x119: "그래픽 처리 오류 (VIDEO_SCHEDULER) — 그래픽 드라이버",
    0x124: "하드웨어 오류 (WHEA_UNCORRECTABLE_ERROR) — 발열·전원·오버클럭",
    0x1A: "메모리 관리 오류 (MEMORY_MANAGEMENT) — 메모리(램) 불량·오버클럭(XMP) 또는 드라이버",
    0x50: "잘못된 메모리 접근 (PAGE_FAULT_IN_NONPAGED_AREA) — 드라이버·메모리",
    0x4E: "메모리 목록 오류 (PFN_LIST_CORRUPT) — 메모리(램)",
    0x133: "드라이버가 너무 오래 붙잡음 (DPC_WATCHDOG_VIOLATION) — 드라이버(그래픽·저장장치·가상화)",
    0xEF: "윈도우 핵심 프로그램이 꺼짐 (CRITICAL_PROCESS_DIED) — 메모리 부족·저장장치",
    0x9F: "전원 상태 전환 중 드라이버 멈춤 (DRIVER_POWER_STATE_FAILURE)",
    0x139: "드라이버가 시스템 구조를 망가뜨림 (KERNEL_SECURITY_CHECK_FAILURE) — 드라이버",
    0x3B: "시스템 함수 오류 (SYSTEM_SERVICE_EXCEPTION) — 드라이버(그래픽·가상화)",
    0x7E: "시스템 스레드 오류 (SYSTEM_THREAD_EXCEPTION_NOT_HANDLED) — 드라이버",
    0xD1: "드라이버 메모리 접근 오류 (DRIVER_IRQL_NOT_LESS_OR_EQUAL) — 드라이버(네트워크·가상화 등)",
    0xA: "메모리 접근 오류 (IRQL_NOT_LESS_OR_EQUAL) — 드라이버·메모리",
    0x1E: "처리되지 않은 커널 오류 (KMODE_EXCEPTION_NOT_HANDLED) — 드라이버",
}
# 이 도우미와 함께 도는 프로그램 (강제 종료 기록에서 표시)
OURS = ("python.exe", "pythonw.exe", "vboxmanage.exe", "dnplayer.exe", "ld9boxheadless.exe", "ldconsole.exe", "adb.exe",
        "ld.exe", "flutter_rainbow.exe", "msedge.exe", "chrome.exe")
GROUPS = {"LDPlayer": ("dnplayer.exe", "ld9boxheadless.exe", "ldboxheadless.exe", "vboxmanage.exe", "ldconsole.exe",
                       "ld.exe", "ldvboxheadless.exe", "ldmultiplayer.exe"),
          "도우미·청취기(python)": ("python.exe", "pythonw.exe"),
          "브라우저": ("msedge.exe", "chrome.exe", "msedgewebview2.exe")}


def _decode(out) -> str:
    if isinstance(out, str):
        return out
    for enc in ("utf-8", "cp949"):
        try:
            return (out or b"").decode(enc)
        except UnicodeDecodeError:
            continue
    return (out or b"").decode("utf-8", errors="replace")


def _local(stamp: str) -> str:
    try:
        t = datetime.fromisoformat(stamp[:19]).replace(tzinfo=timezone.utc)
        return t.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return stamp[:19]


def parse_events(text: str) -> list[dict]:
    """wevtutil /f:RenderedXml 결과 → [{time, id, provider, data{이름: 값}, values[], message}] (새것부터)."""
    body = re.sub(r"<\?xml[^>]*\?>", "", text or "").strip()
    if not body:
        return []
    try:
        root = ET.fromstring(f"<Events>{body}</Events>")
    except ET.ParseError:
        return []
    out = []
    for ev in root.iter(f"{NS}Event"):
        sys_ = ev.find(f"{NS}System")
        if sys_ is None:
            continue
        prov = sys_.find(f"{NS}Provider")
        tc = sys_.find(f"{NS}TimeCreated")
        eid = sys_.find(f"{NS}EventID")
        data, values = {}, []
        for d in ev.iter(f"{NS}Data"):
            values.append((d.text or "").strip())
            if d.get("Name"):
                data[d.get("Name")] = (d.text or "").strip()
        msg = ev.find(f"{NS}RenderingInfo/{NS}Message")
        out.append({"time": _local(tc.get("SystemTime", "")) if tc is not None else "",
                    "id": int(eid.text) if eid is not None and (eid.text or "").isdigit() else 0,
                    "provider": prov.get("Name", "") if prov is not None else "", "data": data, "values": values,
                    "message": re.sub(r"\s+", " ", (msg.text or "") if msg is not None else "").strip()})
    return out


def bugcheck_code(ev: dict) -> int | None:
    """블루스크린 번호 (Kernel-Power 41 은 BugcheckCode 십진수, WER 1001 은 param1 '0x…')."""
    if ev["provider"] == "Microsoft-Windows-Kernel-Power":
        try:
            return int(ev["data"].get("BugcheckCode") or 0)
        except ValueError:
            return None
    m = re.search(r"0x([0-9a-fA-F]{1,8})", ev["data"].get("param1") or " ".join(ev["values"]) or ev["message"])
    return int(m.group(1), 16) if m else None


def _detail(key: str, ev: dict) -> str:
    if key in ("bugcheck", "power41"):
        code = bugcheck_code(ev)
        if key == "power41" and not code:
            return "블루스크린 없이 꺼짐 (전원이 끊겼거나 멈춰서 전원 버튼으로 끔)"
        return f"오류 번호 0x{code:X}: {BUGCHECKS.get(code, '흔하지 않은 번호 — 이 번호로 검색해 보세요')}" if code else ev["message"][:200]
    if key in ("apperror", "apphang"):
        app = ev["data"].get("AppName") or (ev["values"][0] if ev["values"] else "")
        mod = ev["data"].get("ModuleName") or (ev["values"][3] if len(ev["values"]) > 3 else "")
        return f"{app}" + (f" (오류 부분: {mod})" if mod and key == "apperror" else "")
    return ev["message"][:400] or " · ".join(v for v in ev["values"] if v)[:400]


def query(log: str, provider: str, event_id: int | None, days: int, run=subprocess.run, count: int = 50) -> list[dict]:
    ms = days * 24 * 3600 * 1000
    cond = f"Provider[@Name='{provider}']" + (f" and EventID={event_id}" if event_id else "")
    xpath = f"*[System[{cond} and TimeCreated[timediff(@SystemTime) <= {ms}]]]"
    try:
        r = run(["wevtutil", "qe", log, f"/q:{xpath}", f"/c:{count}", "/rd:true", "/f:RenderedXml"],
                capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_events(_decode(r.stdout))


def memory_status() -> dict | None:
    """실제 메모리·쓸 수 있는 메모리(가상 메모리 파일 포함) MB. 윈도우가 아니면 None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return None
        mb = 1024 * 1024
        return {"phys_total": st.ullTotalPhys // mb, "phys_free": st.ullAvailPhys // mb,
                "commit_total": st.ullTotalPageFile // mb, "commit_free": st.ullAvailPageFile // mb}
    except Exception:
        return None


def top_programs(run=subprocess.run) -> list[dict]:
    """지금 메모리를 많이 쓰는 프로그램 (같은 이름 묶음) [{name, mb, n}] — tasklist."""
    try:
        r = run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, timeout=20,
                creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except (OSError, subprocess.SubprocessError):
        return []
    sums: dict = {}
    for line in _decode(r.stdout).splitlines():
        cols = [c.strip('"') for c in re.findall(r'"[^"]*"', line)]
        if len(cols) < 5:
            continue
        kb = int(re.sub(r"\D", "", cols[-1]) or 0)
        name = cols[0].lower()
        mb, n = sums.get(name, (0, 0))
        sums[name] = (mb + kb / 1024, n + 1)
    rows = [{"name": k, "mb": int(v[0]), "n": v[1]} for k, v in sums.items()]
    groups = []
    for label, names in GROUPS.items():
        hit = [r for r in rows if r["name"] in names]
        if hit:
            groups.append({"name": label, "mb": sum(r["mb"] for r in hit), "n": sum(r["n"] for r in hit)})
    return groups + sorted(rows, key=lambda r: -r["mb"])[:12]


def scan(days: int = DAYS, run=subprocess.run, mem=memory_status, disk=shutil.disk_usage,
         minidump: str | None = None, platform: str = sys.platform) -> dict:
    """최근 days 일 기록을 모아 쉬운 말 결론까지. → {cats, verdicts, snapshot, top, minidumps}"""
    if platform != "win32":
        return {"error": "윈도우 PC 에서만 볼 수 있습니다", "cats": [], "verdicts": [], "snapshot": {}, "top": []}
    cats = []
    for key, log, provider, eid, label, explain in CATS:
        evs = query(log, provider, eid, days, run=run)
        if key == "apperror":
            evs = [e for e in evs if (_detail(key, e).split(" ")[0].lower() in OURS)] or evs[:10]
        cats.append({"key": key, "label": label, "explain": explain, "count": len(evs),
                     "last": evs[0]["time"] if evs else "", "items": [{"time": e["time"], "detail": _detail(key, e)}
                                                                     for e in evs[:10]]})
    snap = {}
    m = mem()
    if m:
        snap.update(m)
    try:
        du = disk(os.environ.get("SystemDrive", "C:") + "\\")
        snap["disk_free_gb"] = round(du.free / 1024 ** 3, 1)
        snap["disk_total_gb"] = round(du.total / 1024 ** 3, 1)
    except (OSError, ValueError):
        pass
    dumps = []
    folder = minidump or os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Minidump")
    try:
        for name in sorted(os.listdir(folder), reverse=True)[:5]:
            dumps.append(name)
    except OSError:
        pass
    result = {"days": days, "cats": cats, "snapshot": snap, "top": top_programs(run=run), "minidumps": dumps}
    result["verdicts"] = verdicts(result)
    return result


def verdicts(result: dict) -> list[str]:
    """모은 기록 → 가장 그럴듯한 원인부터 쉬운 말로."""
    by = {c["key"]: c for c in result["cats"]}
    out = []
    # 블루스크린 한 번에 두 기록(1001·41)이 함께 남으므로 1001 을 먼저 보고, 없을 때만 41 의 번호
    codes = [it["detail"] for it in by.get("bugcheck", {}).get("items", []) if it["detail"].startswith("오류 번호")] or \
        [it["detail"] for it in by.get("power41", {}).get("items", []) if it["detail"].startswith("오류 번호")]
    if codes:
        out.append(f"블루스크린이 {len(codes)}번 기록됨 — 가장 최근: {codes[0]}")
    silent = [it for it in by.get("power41", {}).get("items", []) if it["detail"].startswith("블루스크린 없이")]
    if silent:
        out.append(f"블루스크린 없이 꺼진 기록 {len(silent)}번 (최근 {silent[0]['time']}) — 화면이 멈춰 전원 버튼으로 끄셨다면 "
                   "'멈춤'(메모리 부족·그래픽), 저절로 꺼지고 다시 켜졌다면 전원(파워)·발열 쪽입니다.")
    if by.get("memory", {}).get("count"):
        out.append(f"메모리 부족 기록 {by['memory']['count']}번 (최근 {by['memory']['last']}) — 동시에 켠 것이 너무 많습니다. "
                   "동시 청취 칸 수·LDPlayer 창 수(또는 창마다 메모리)를 줄이거나 가상 메모리 파일을 늘리세요.")
    if by.get("tdr", {}).get("count") or by.get("nvidia", {}).get("count"):
        n = by.get("tdr", {}).get("count", 0) + by.get("nvidia", {}).get("count", 0)
        out.append(f"그래픽 드라이버 멈춤·오류 {n}번 — LDPlayer 창 여러 개와 받아쓰기(그래픽카드)가 함께 그래픽카드를 씁니다. "
                   "그래픽 드라이버를 새로 깔고, LDPlayer 창의 화면 설정(FPS)을 낮춰 보세요.")
    if by.get("whea", {}).get("count"):
        out.append(f"하드웨어 오류 {by['whea']['count']}번 — 발열·전원(파워)·메모리 오버클럭(XMP)을 점검하세요.")
    snap = result.get("snapshot", {})
    if snap.get("commit_free") is not None and snap["commit_free"] < 4000:
        out.append(f"지금 더 쓸 수 있는 메모리가 {snap['commit_free']}MB 뿐입니다 — 조금만 더 켜도 멈출 수 있습니다.")
    if snap.get("disk_free_gb") is not None and snap["disk_free_gb"] < 15:
        out.append(f"C 드라이브 남은 공간 {snap['disk_free_gb']}GB — 가상 메모리 파일이 커지지 못해 메모리 부족·멈춤이 생길 수 있습니다.")
    if not out:
        out.append(f"최근 {result.get('days', DAYS)}일 윈도우 기록에 꺼짐·블루스크린·메모리 부족이 없습니다. "
                   "다운된 직후(다시 켠 뒤) 한 번 더 눌러 보세요.")
    return out
