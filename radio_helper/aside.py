"""게시판 확인·글쓰기를 Aside 브라우저로 (0.62).

코너 게시판 공지 읽기·코너 게시판 모으기·로그인 확인·글 입력/등록에 쓰는 도우미 브라우저를, 이 PC 에 깔린 Aside
(크로미움 기반 AI 브라우저)로 연다. 못 찾거나 열리지 않으면 예전처럼 Edge 로 연다.

- 도우미 전용 프로필(데이터 폴더 browser_profile_aside)로 연다. 평소 쓰는 Aside 프로필의 로그인(쿠키)은 암호화돼 있고,
  크로미움은 평소 프로필을 자동화하지 못하게 막으며, 이 도우미는 로그인 정보를 옮겨 담지 않는다 → '미리 로그인'으로 뜨는
  Aside 창에서 방송국마다 한 번 로그인하면 그 프로필에 남아 다음부터 창 없이 쓴다.
- 찾는 순서: 설정에 적은 경로 → 윈도우 App Paths 등록 → 흔한 설치 폴더 → 시작 메뉴 바로 가기.
"""

from __future__ import annotations

import ntpath
import os
import subprocess
import sys

BROWSER_KEY = "boards.browser"        # auto(Aside 있으면 Aside) / aside / edge
PATH_KEY = "boards.aside_path"        # 직접 적은 Aside 실행 파일 경로
ERROR_KEY = "boards.aside_error"      # 마지막으로 Aside 를 열지 못한 까닭 (그때는 Edge 로 엶)
LOGINS_KEY = "boards.logins"          # boards.LOGIN_KEY (방송국별 로그인 상태)
FOUND_KEY = "boards.aside_found"      # 찾아 둔 경로 (화면을 열 때마다 다시 찾지 않게)
CHOICES = {"auto": "자동 (확장이 연결되면 내 Aside 새 탭, 아니면 Edge)", "tab": "내 Aside 새 탭 (확장 연결 — 로그인 그대로)",
           "aside": "도우미 전용 Aside 창 (따로 로그인)", "edge": "Edge"}
EXE_NAMES = ("aside.exe", "Aside.exe")


def candidates(env=os.environ) -> list[str]:
    out = []
    for var, sub in (("LOCALAPPDATA", ("Aside", "Application")), ("LOCALAPPDATA", ("Programs", "Aside")),
                     ("LOCALAPPDATA", ("Aside",)), ("PROGRAMFILES", ("Aside", "Application")),
                     ("PROGRAMFILES", ("Aside",)), ("PROGRAMFILES(X86)", ("Aside", "Application"))):
        base = env.get(var)
        if base:
            out += [os.path.join(base, *sub, name) for name in EXE_NAMES[:1]]
    return out


def registered() -> str | None:
    """윈도우 App Paths 에 등록된 aside.exe."""
    if sys.platform != "win32":
        return None
    import winreg

    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\aside.exe") as k:
                value = winreg.QueryValue(k, None)
                if value:
                    return value.strip('"')
        except OSError:
            continue
    return None


def shortcut_target(link: str) -> str | None:
    """바로 가기(.lnk)가 가리키는 실행 파일 (PowerShell 로 읽음)."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(New-Object -ComObject WScript.Shell).CreateShortcut($args[0]).TargetPath", link],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
                             creationflags=0x08000000 if sys.platform == "win32" else 0)
    except (OSError, subprocess.SubprocessError):
        return None
    target = (out.stdout or "").strip()
    return target or None


def find(saved: str = "", exists=os.path.isfile, env=os.environ, reg=registered, link=None,
         target=shortcut_target) -> str | None:
    """이 PC 의 Aside 실행 파일 경로. 없으면 None."""
    if saved and exists(saved):
        return saved
    if sys.platform != "win32" and reg is registered and link is None:
        return None                      # 윈도우 전용
    found = reg()
    if found and exists(found):
        return found
    for path in candidates(env):
        if exists(path):
            return path
    if link is None:
        from .quizbot.gorilla import start_menu_shortcut

        link = start_menu_shortcut(["Aside"])
    if link:
        path = target(link)
        if path and exists(path) and ntpath.basename(path).lower().startswith("aside"):
            return path
    return None


def settings(conn) -> dict:
    from . import db

    choice = db.get_setting(conn, BROWSER_KEY) or "auto"
    return {"choice": choice if choice in CHOICES else "auto", "path": db.get_setting(conn, PATH_KEY) or "",
            "error": db.get_setting(conn, ERROR_KEY) or ""}


def chosen(conn, finder=None) -> str | None:
    """도우미 전용 창(Playwright)을 Aside 로 열 경로: '도우미 전용 Aside 창'을 고른 경우만 (0.64 — 그 창이 뜨지 않는 PC 가
    있어 '자동'은 확장이 연결되면 내 Aside 새 탭, 아니면 예전처럼 Edge)."""
    return exe_path(conn, finder) if settings(conn)["choice"] == "aside" else None


def exe_path(conn, finder=None) -> str | None:
    """이 PC 의 Aside 실행 파일 (Edge 를 고른 경우·못 찾은 경우 None). 새 탭 열기에 쓴다."""
    from . import db

    s = settings(conn)
    if s["choice"] == "edge":
        return None
    cached = db.get_setting(conn, FOUND_KEY) or ""
    path = (finder or find)(s["path"] or cached)
    if (path or "") != cached:
        db.set_setting(conn, FOUND_KEY, path or "")
    return path


def describe(conn, finder=None) -> dict:
    """화면용: {choice, path, found, error}"""
    return {**settings(conn), "found": exe_path(conn, finder)}


def profile_dir():
    from . import db

    path = db.data_dir() / "browser_profile_aside"     # Edge 프로필과 따로 (판이 다른 크로미움끼리 섞지 않음)
    path.mkdir(parents=True, exist_ok=True)
    return path
