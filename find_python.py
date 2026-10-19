"""start_windows.bat 이 부른다: 음성 인식 부품이 설치되는 Python(3.12·3.11·3.13·3.14)을 찾고, 없으면 설치한다.

    python find_python.py --out .python_choice [--install]

- 표준 라이브러리만 쓴다 (지금 .venv 가 너무 새 Python 3.15 여도 돈다).
- 후보를 실제로 실행해 버전을 확인한다. 새 Python 설치 관리자(py)는 3.12 가 없는데도 '성공'을 돌려줄 때가 있어서
  종료 코드만 믿지 않는다 (0.41 까지 이 때문에 3.12 를 설치하지 않고 3.15 로 다시 켰음).
- --install: 없으면 설치한다 — ① py install 3.12 (Python 설치 관리자) ② winget ③ python.org 설치 파일(조용히, 내 계정에만).
- 찾은 python.exe 경로를 --out 파일에 적는다. 찾으면 0, 못 찾으면 1.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

PREFERRED = ("3.12", "3.11", "3.13", "3.14")      # 처음은 3.12 → 3.11 (가장 오래 검증된 조합)
INSTALL_VERSION = "3.12"
INSTALLER_URL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"   # 3.12 의 마지막 설치 파일
PROBE = "import sys; print(sys.executable); print('%d.%d' % sys.version_info[:2])"
HIDE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def say(text: str) -> None:
    print(f"[setup] {text}", flush=True)


def probe(cmd: list[str], timeout: float = 60) -> tuple[str, str] | None:
    """그 명령으로 Python 을 실제로 실행해 (python.exe 경로, '3.12') — 안 되면 None."""
    try:
        out = subprocess.run(cmd + ["-c", PROBE], capture_output=True, text=True, timeout=timeout,
                             stdin=subprocess.DEVNULL, creationflags=HIDE)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line.strip() for line in (out.stdout or "").splitlines() if line.strip()]
    if out.returncode != 0 or len(lines) < 2 or not Path(lines[-2]).is_file():
        return None
    return lines[-2], lines[-1]


def folder_candidates(env=os.environ) -> list[Path]:
    local = Path(env.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    roots = [local / "Programs" / "Python", Path(env.get("ProgramFiles") or r"C:\Program Files"), Path("C:\\")]
    out = []
    for v in PREFERRED:
        tag = v.replace(".", "")
        out += [root / f"Python{tag}" / "python.exe" for root in roots]
        out.append(local / "Python" / f"pythoncore-{v}-64" / "python.exe")   # 새 Python 설치 관리자
    return out


def registry_candidates() -> list[Path]:
    """윈도우 등록부 (python.org·winget 설치가 남김)."""
    try:
        import winreg
    except ImportError:
        return []
    out = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for v in PREFERRED:
            try:
                with winreg.OpenKey(hive, rf"Software\Python\PythonCore\{v}\InstallPath") as key:
                    path = winreg.QueryValueEx(key, "ExecutablePath")[0]
            except OSError:
                continue
            out.append(Path(path))
    return out


def find(probe_fn=probe, folders=None, registry=None, has_py=None) -> str | None:
    """원하는 순서대로 실제 실행해 보고 처음 맞는 python.exe."""
    found: dict[str, str] = {}

    def consider(result):
        if result and result[1] in PREFERRED and result[1] not in found:
            found[result[1]] = result[0]

    if has_py if has_py is not None else shutil.which("py"):
        for v in PREFERRED:
            consider(probe_fn(["py", f"-{v}"]))
    for path in (folders if folders is not None else folder_candidates()) + \
            (registry if registry is not None else registry_candidates()):
        if Path(path).is_file():
            consider(probe_fn([str(path)]))
    for v in PREFERRED:
        if v in found:
            return found[v]
    return None


def run_visible(cmd: list[str], timeout: float) -> bool:
    """설치 명령은 화면에 보이게 (묻는 것이 있으면 사용자가 답함)."""
    try:
        return subprocess.run(cmd, timeout=timeout).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def install_with_pymanager(run=run_visible) -> bool:
    if not shutil.which("py"):
        return False
    say(f"Python 설치 관리자로 Python {INSTALL_VERSION} 을(를) 설치합니다 (py install {INSTALL_VERSION}) — 몇 분. "
        "묻는 것이 있으면 Y 를 누르세요.")
    return run(["py", "install", INSTALL_VERSION], 1200)


def install_with_winget(run=run_visible) -> bool:
    if not shutil.which("winget"):
        return False
    say(f"winget 으로 Python {INSTALL_VERSION} 을(를) 설치합니다 — 몇 분. 윈도우가 물으면 허용하세요.")
    return run(["winget", "install", "-e", "--id", f"Python.Python.{INSTALL_VERSION}", "--scope", "user",
                "--accept-package-agreements", "--accept-source-agreements"], 1200)


def install_from_python_org(run=run_visible, opener=None) -> bool:
    say(f"python.org 에서 Python {INSTALL_VERSION} 설치 파일을 받아 조용히 설치합니다 (이 계정에만) — 몇 분.")
    opener = opener or urllib.request.build_opener()
    target = Path(tempfile.gettempdir()) / Path(INSTALLER_URL).name
    try:
        with opener.open(INSTALLER_URL, timeout=300) as r, open(target, "wb") as f:
            shutil.copyfileobj(r, f)
    except Exception as e:
        say(f"설치 파일을 받지 못했습니다: {type(e).__name__}")
        return False
    try:
        return run([str(target), "/quiet", "InstallAllUsers=0", "Include_launcher=0", "Include_test=0",
                    "PrependPath=0", "Shortcuts=0", "Include_doc=0"], 1200)
    finally:
        try:
            target.unlink()
        except OSError:
            pass


def main(argv: list[str] | None = None, finder=find, installers=None) -> int:
    ap = argparse.ArgumentParser(description="음성 인식 부품이 설치되는 Python 찾기·설치")
    ap.add_argument("--out", required=True)
    ap.add_argument("--install", action="store_true")
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.unlink(missing_ok=True)
    found = finder()
    if found is None and args.install and sys.platform == "win32":
        for install in installers or (install_with_pymanager, install_with_winget, install_from_python_org):
            install()
            found = finder()
            if found:
                break
    if found is None:
        say(f"Python {' / '.join(PREFERRED[:2])} 을(를) 찾지도 설치하지도 못했습니다.")
        return 1
    out.write_text(found, encoding="utf-8")
    say(f"사용할 Python: {found}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
