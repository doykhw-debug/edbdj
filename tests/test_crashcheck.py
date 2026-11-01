"""1.00: PC 다운 원인 찾기 — 윈도우 시스템 기록(wevtutil)·tasklist 를 흉내 내어 쉬운 말 결론까지."""

from collections import namedtuple
from types import SimpleNamespace

from conftest import csrf

from radio_helper import crashcheck

NS = 'xmlns="http://schemas.microsoft.com/win/2004/08/events/event"'


def event(provider, eid, when, data="", message=""):
    return (f'<Event {NS}><System><Provider Name="{provider}"/><EventID>{eid}</EventID>'
            f'<TimeCreated SystemTime="{when}"/></System><EventData>{data}</EventData>'
            f'<RenderingInfo Culture="ko-KR"><Message>{message}</Message></RenderingInfo></Event>')


LOGS = {
    "Microsoft-Windows-Kernel-Power": event("Microsoft-Windows-Kernel-Power", 41, "2026-11-01T03:10:00.0000000Z",
                                            '<Data Name="BugcheckCode">0</Data>')
    + event("Microsoft-Windows-Kernel-Power", 41, "2026-10-30T01:00:00.0000000Z", '<Data Name="BugcheckCode">278</Data>'),
    "Microsoft-Windows-Resource-Exhaustion-Detector": event(
        "Microsoft-Windows-Resource-Exhaustion-Detector", 2004, "2026-11-01T03:05:00.0000000Z",
        message="Windows에서 가상 메모리 부족 상태를 진단했습니다. Ld9BoxHeadless.exe(9000), python.exe(4000)"),
    "Application Error": event("Application Error", 1000, "2026-11-01T02:00:00.0000000Z",
                               "<Data>VBoxManage.exe</Data><Data>1</Data><Data>0</Data><Data>KERNELBASE.dll</Data>"),
}
TASKS = ('"Ld9BoxHeadless.exe","100","Console","1","2,000,000 K"\r\n"Ld9BoxHeadless.exe","101","Console","1","1,000,000 K"\r\n'
         '"python.exe","200","Console","1","800,000 K"\r\n"System","4","Services","0","1,000 K"\r\n')


def fake_run(args, **kw):
    if args[0] == "tasklist":
        return SimpleNamespace(stdout=TASKS.encode("cp949"))
    q = args[3]
    hit = next((v for k, v in LOGS.items() if f"@Name='{k}'" in q), "")
    return SimpleNamespace(stdout=hit.encode("utf-8"))


Disk = namedtuple("Disk", "total used free")
REAL_SCAN = crashcheck.scan


def scan(**kw):
    return REAL_SCAN(run=fake_run, platform="win32", minidump="/없는/폴더",
                           mem=lambda: {"phys_total": 32000, "phys_free": 900, "commit_total": 40000, "commit_free": 1500},
                           disk=lambda p: Disk(500 * 1024 ** 3, 492 * 1024 ** 3, 8 * 1024 ** 3), **kw)


def test_scan_reads_windows_records_and_explains():
    r = scan()
    by = {c["key"]: c for c in r["cats"]}
    assert by["power41"]["count"] == 2 and by["power41"]["items"][0]["detail"].startswith("블루스크린 없이")
    assert "0x116" in by["power41"]["items"][1]["detail"] and "그래픽" in by["power41"]["items"][1]["detail"]
    assert by["memory"]["count"] == 1 and "Ld9BoxHeadless" in by["memory"]["items"][0]["detail"]
    assert by["apperror"]["items"][0]["detail"] == "VBoxManage.exe (오류 부분: KERNELBASE.dll)"
    text = " ".join(r["verdicts"])
    assert "블루스크린이 1번" in text and "블루스크린 없이 꺼진 기록 1번" in text and "메모리 부족 기록 1번" in text
    assert "1500MB" in text and "C 드라이브 남은 공간 8.0GB" in text
    top = {t["name"]: t for t in r["top"]}
    assert top["LDPlayer"]["mb"] == 2929 and top["LDPlayer"]["n"] == 2 and top["도우미·청취기(python)"]["mb"] == 781


def test_quiet_pc_and_not_windows():
    global LOGS
    saved, LOGS = LOGS, {}
    try:
        r = crashcheck.scan(run=fake_run, platform="win32", mem=lambda: None, disk=lambda p: Disk(1, 0, 100 * 1024 ** 3))
        assert r["verdicts"][0].startswith("최근 7일 윈도우 기록에 꺼짐")
    finally:
        LOGS = saved
    assert crashcheck.scan(platform="linux")["error"]


def test_pc_check_page(client, data_dir, monkeypatch):
    monkeypatch.setattr(crashcheck, "scan", lambda days=7: scan(days=days))
    assert "원인 찾기" in client.get("/pc-check").get_data(as_text=True)
    token = csrf(client, "/pc-check")
    client.post("/pc-check", data={"csrf_token": token, "days": "3"})
    page = client.get("/pc-check").get_data(as_text=True)
    assert "블루스크린 없이 꺼진 기록" in page and "Ld9BoxHeadless" in page and "C 드라이브 남음" in page
    assert "PC 다운 원인 찾기" in client.get("/quizbot").get_data(as_text=True)
