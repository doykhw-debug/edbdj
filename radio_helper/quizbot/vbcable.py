"""무료 가상 오디오 장치 VB-CABLE 자동 설치 (앱 소리 직접 받기가 안 되는 PC 용, 0.54).

VB-CABLE(VB-Audio, 기부형 무료 — 장치 1개)을 공식 사이트에서 그때 내려받는다 (이 프로그램에 넣어 다시 배포하지 않음).
설치 파일의 디지털 서명이 VB-Audio 것인지 확인한 뒤 관리자 권한으로 실행한다. 윈도우가 묻는 '관리자 권한 허용'과
'장치 소프트웨어 설치'는 사용자가 직접 누른다 (우회하지 않음). 설치 뒤 PC 를 다시 켜야 장치가 보일 수 있다.
A+B·C+D(장치 2개 이상)는 기부해야 받을 수 있어 쓰지 않는다.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path

from .. import db

PAGE_URL = "https://vb-audio.com/Cable/"
DOWNLOAD_BASE = "https://download.vb-audio.com/Download_CABLE/"
KNOWN_PACK = "VBCABLE_Driver_Pack45.zip"     # 페이지를 못 읽을 때 (2026년 기준 최신)
SIGNERS = ("vb-audio", "burel")              # 서명한 곳 (VB-Audio Software · Vincent Burel)
STATE_KEY = "vbcable.install"
MAX_BYTES = 50 * 1024 * 1024
SETUP_TIMEOUT = 900
CANCELLED = 1223                             # ERROR_CANCELLED — 관리자 권한 허용 창에서 '아니요'
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RadioHelper"


def pack_url(html: str) -> str:
    """사이트 페이지에서 무료 VB-CABLE 드라이버 묶음 주소 (판 번호가 가장 큰 것). A+B·C+D 는 고르지 않는다."""
    found = []
    for m in re.finditer(r'(https?://[^\s"\'<>]*?/)?(VBCABLE_Driver_Pack(\d+)\.zip)', html or ""):
        found.append((int(m.group(3)), (m.group(1) or DOWNLOAD_BASE) + m.group(2)))
    return max(found)[1] if found else DOWNLOAD_BASE + KNOWN_PACK


def installed(names: list[str]) -> bool:
    """출력 장치 목록에 VB-CABLE('CABLE Input (VB-Audio Virtual Cable)')이 있는지."""
    return any("vb-audio virtual cable" in n.lower() or n.lower().startswith("cable input") for n in names)


def safe_extract(zip_path: Path, dest: Path) -> list[Path]:
    """압축을 dest 안에만 푼다 (폴더 밖으로 나가는 이름은 건너뜀)."""
    out = []
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if info.is_dir() or root not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                dst.write(src.read())
            out.append(target)
    return out


def setup_exe(files: list[Path], x64: bool = True) -> Path | None:
    by_name = {f.name.lower(): f for f in files}
    names = ["vbcable_setup_x64.exe", "vbcable_setup.exe"] if x64 else ["vbcable_setup.exe"]
    return next((by_name[n] for n in names if n in by_name), None)


def ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def powershell(command: str, run=subprocess.run, timeout: int = 60):
    return run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def check_signature(path: Path, run=subprocess.run) -> tuple[bool, str]:
    """설치 파일 서명이 유효하고 VB-Audio 것인지 → (통과, '상태 · 서명한 곳')."""
    done = powershell(f"$s = Get-AuthenticodeSignature -LiteralPath {ps_quote(path)}; "
                      "Write-Output $s.Status.ToString(); Write-Output $s.SignerCertificate.Subject", run)
    lines = [x.strip() for x in (done.stdout or "").splitlines() if x.strip()]
    status = lines[0] if lines else "확인 못 함"
    subject = lines[1] if len(lines) > 1 else ""
    ok = status == "Valid" and any(s in subject.lower() for s in SIGNERS)
    return ok, f"{status} · {subject or '서명 없음'}"


def run_setup(path: Path, run=subprocess.run) -> tuple[int, str]:
    """관리자 권한으로 설치 프로그램 실행 (-i 설치, -h 창 숨김 — 윈도우의 확인 창은 그대로 뜸). 끝날 때까지 기다린다."""
    # 관리자 권한 창에서 '아니요'를 누르면 Start-Process 가 오류를 내고 $p 가 비므로, 그때는 1223 으로 끝낸다
    done = powershell(f"try {{ $p = Start-Process -FilePath {ps_quote(path)} -ArgumentList '-i','-h' "
                      f"-WorkingDirectory {ps_quote(path.parent)} -Verb RunAs -Wait -PassThru -ErrorAction Stop; "
                      f"exit $p.ExitCode }} catch {{ Write-Output $_.Exception.Message; exit {CANCELLED} }}",
                      run, timeout=SETUP_TIMEOUT)
    return done.returncode, (done.stderr or done.stdout or "").strip()[:300]


def fetch(url: str, limit: int = MAX_BYTES) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("내려받은 파일이 너무 큽니다")
    return data


def saved(conn: sqlite3.Connection) -> dict | None:
    try:
        data = json.loads(db.get_setting(conn, STATE_KEY) or "null")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _save(conn: sqlite3.Connection, ok: bool, note: str, running: bool = False) -> dict:
    state = {"at": db.now(), "ok": ok, "note": note, "running": running}
    db.set_setting(conn, STATE_KEY, json.dumps(state, ensure_ascii=False))
    if not running:
        db.log(conn, "quizbot", f"가상 장치(VB-CABLE) 자동 설치: {'완료' if ok else '안 됨'} — {note}")
    return state


def install(conn: sqlite3.Connection, list_devices=None, get=fetch, run=subprocess.run, workdir: Path | None = None,
            x64: bool = True, say=print) -> dict:
    """내려받기 → 압축 풀기 → 서명 확인 → 관리자 권한으로 설치 실행 → 장치가 생겼는지 확인."""
    if list_devices is None:
        from .lanes import devices as list_devices
    if installed(list_devices()):
        return _save(conn, True, "이미 설치되어 있습니다 (CABLE Input)")
    _save(conn, False, "내려받는 중", running=True)
    try:
        html = get(PAGE_URL).decode("utf-8", "replace")
    except Exception as e:
        say(f"사이트 페이지를 읽지 못해 알려진 주소로 받습니다 ({type(e).__name__})")
        html = ""
    url = pack_url(html)
    dest = workdir or db.data_dir() / "vbcable" / time.strftime("%Y%m%d_%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    say(f"내려받기: {url}")
    try:
        zip_path = dest / Path(url).name
        zip_path.write_bytes(get(url))
        files = safe_extract(zip_path, dest / "files")
    except Exception as e:
        return _save(conn, False, f"내려받지 못했습니다 ({type(e).__name__}: {str(e)[:120]}) — 인터넷을 확인하고 다시 누르세요")
    exe = setup_exe(files, x64)
    if exe is None:
        return _save(conn, False, "받은 파일에 설치 프로그램(VBCABLE_Setup_x64.exe)이 없습니다")
    ok, who = check_signature(exe, run)
    say(f"서명 확인: {who}")
    if not ok:
        return _save(conn, False, f"설치 파일 서명을 확인하지 못해 실행하지 않았습니다 ({who})")
    _save(conn, False, "설치 중 — 윈도우가 묻는 창에서 '예'·'설치'를 누르세요", running=True)
    say("설치 프로그램을 관리자 권한으로 엽니다. 윈도우가 묻는 창에서 '예'·'설치'를 누르세요.")
    try:
        code, detail = run_setup(exe, run)
    except subprocess.TimeoutExpired:
        return _save(conn, False, "설치 창이 15분 넘게 닫히지 않았습니다 — 설치 창을 확인하세요")
    if installed(list_devices()):
        return _save(conn, True, "설치했습니다 — 출력 장치에 'CABLE Input' 이 생겼습니다")
    if code == CANCELLED or (code != 0 and ("cancel" in detail.lower() or "취소" in detail)):
        return _save(conn, False, "관리자 권한 허용을 누르지 않아 설치하지 않았습니다")
    return _save(conn, code == 0, "설치 프로그램이 끝났습니다 — PC 를 다시 켜면 'CABLE Input' 장치가 보입니다"
                 if code == 0 else f"설치 프로그램이 오류로 끝났습니다 (코드 {code}) {detail}")
