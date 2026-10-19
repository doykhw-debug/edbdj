"""Claude 구독으로 분석하기 — 공식 Claude Code(claude 프로그램)를 화면 없이 실행해 결과만 받는다.

- 사용자가 이 PC(또는 서버)에서 Claude Code 에 claude.ai 계정으로 한 번 로그인해 두면, claude 프로그램이 그 로그인으로
  Claude 를 부른다 → claude.ai 구독 사용량(5시간·주간 한도, claude.ai·Claude Code 와 함께 씀)에서 나가고 충전 요금은 들지 않는다.
- 이 프로그램은 로그인 정보(토큰·비밀번호·쿠키)를 읽거나 저장하거나 넘기지 않는다. claude 프로그램만 실행한다.
- 자식 프로세스 환경에서 ANTHROPIC_API_KEY 를 지운다: 키가 있으면 claude 가 구독 대신 API 키(충전 요금)를 쓰기 때문.
- 빈 임시 폴더에서 실행하고, 도구는 모두 끈다 (편성표 찾기만 웹 검색 허용). 대화 기록은 남기지 않는다.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

API_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")   # 있으면 구독 대신 이것으로 결제됨
CHECK_KEY = "claude_cli.check"
TIMEOUT = 300
LIMIT_PAUSE = 600          # 구독 한도에 걸리면 10분 동안은 다시 부르지 않음 (매번 실패하며 시간 쓰지 않게)
_limited_until = 0.0


class ClaudeCliError(RuntimeError):
    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind   # missing / login / limit / timeout / error


def _windows() -> bool:
    return sys.platform == "win32"


def _npm_target(shim: Path) -> list[str] | None:
    """npm 으로 설치한 claude.cmd 는 cmd.exe 를 거쳐 따옴표가 깨지므로, 그 안의 실제 프로그램을 바로 실행한다."""
    pkg = shim.parent / "node_modules" / "@anthropic-ai" / "claude-code"
    for exe in (pkg / "bin" / "claude.exe", pkg / "bin" / "claude"):
        if exe.is_file():
            return [str(exe)]
    script = pkg / "cli.js"
    node = shutil.which("node")
    if script.is_file() and node:
        return [node, str(script)]
    return None


def candidates() -> list[Path]:
    home = Path.home()
    out = []
    override = os.environ.get("RADIO_HELPER_CLAUDE")
    if override:
        out.append(Path(override))
    found = shutil.which("claude")
    if found:
        out.append(Path(found))
    if _windows():
        out += [home / ".local" / "bin" / "claude.exe",
                Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "Programs" / "claude" / "claude.exe",
                Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "npm" / "claude.cmd"]
    else:
        out += [home / ".local" / "bin" / "claude", home / ".claude" / "local" / "claude", Path("/usr/local/bin/claude")]
    return out


def find_claude() -> list[str] | None:
    """실행할 명령 (claude 프로그램), 없으면 None."""
    for path in candidates():
        if not path.is_file():
            continue
        if path.suffix.lower() in (".cmd", ".bat"):
            target = _npm_target(path)
            if target:
                return target
            continue
        return [str(path)]
    return None


def child_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in API_ENV}
    env.setdefault("CLAUDE_CODE_DISABLE_TERMINAL_TITLE", "1")
    return env


def _run(cmd: list[str], stdin: str = "", timeout: float = TIMEOUT, cwd: str | None = None) -> subprocess.CompletedProcess:
    kwargs = {}
    if _windows():
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # 검은 창이 번쩍이지 않게
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env=child_env(), cwd=cwd, **kwargs)


def _kind(text: str) -> str:
    low = (text or "").lower()
    if any(w in low for w in ("usage limit", "limit reached", "hit your limit", "rate limit", "rate_limit",
                              "사용량 한도", "한도")):
        return "limit"
    if any(w in low for w in ("not logged in", "/login", "log in", "login", "authentication", "unauthorized",
                              "oauth", "invalid api key", "로그인")):
        return "login"
    return "error"


MESSAGES = {
    "missing": "Claude Code(claude 프로그램)가 설치되어 있지 않습니다.",
    "login": "Claude Code 에 로그인되어 있지 않습니다 — 명령 창에서 claude 를 한 번 실행해 로그인하세요.",
    "limit": "Claude 구독 사용량 한도에 걸렸습니다 (5시간·주간 한도, 시간이 지나면 풀림).",
    "timeout": "Claude Code 응답이 너무 늦어 멈췄습니다.",
}


def limited() -> bool:
    return time.time() < _limited_until


def run(system: str, content, schema: dict | None = None, tools: tuple[str, ...] = (), model: str | None = None,
        effort: str | None = None, timeout: float = TIMEOUT, exe: list[str] | None = None) -> dict:
    """claude -p 한 번. content: 글 또는 메시지 블록 목록(사진 포함). → {text, data(구조화 결과), texts(중간 글 포함)}"""
    global _limited_until
    if limited():
        raise ClaudeCliError(MESSAGES["limit"] + " 잠시 쉬는 중.", "limit")
    exe = exe or find_claude()
    if not exe:
        raise ClaudeCliError(MESSAGES["missing"], "missing")
    cmd = exe + ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                 "--system-prompt", system, "--tools", ",".join(tools), "--strict-mcp-config",
                 "--no-session-persistence"]
    if tools:
        cmd += ["--allowedTools", ",".join(tools)]
    if schema is not None:
        cmd += ["--json-schema", json.dumps(schema)]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    message = {"type": "user", "message": {"role": "user", "content": content}}
    work = tempfile.mkdtemp(prefix="radio_claude_")   # 빈 폴더: 이 프로그램 파일·데이터를 Claude Code 가 보지 않게
    try:
        try:
            done = _run(cmd, json.dumps(message, ensure_ascii=False) + "\n", timeout, work)
        except subprocess.TimeoutExpired:
            raise ClaudeCliError(MESSAGES["timeout"], "timeout")
        except OSError as e:
            raise ClaudeCliError(f"Claude Code 를 실행하지 못했습니다: {type(e).__name__}", "missing")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    result, texts = None, []
    for line in (done.stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text" and (block.get("text") or "").strip():
                    texts.append(block["text"])
        elif event.get("type") == "result":
            result = event
    if result is None or result.get("is_error") or result.get("subtype") != "success":
        why = " ".join(str(x) for x in ((result or {}).get("result"), (result or {}).get("errors"),
                                         (done.stderr or "")[-400:]) if x).strip()
        kind = _kind(why) if why else "error"
        if kind == "limit":
            _limited_until = time.time() + LIMIT_PAUSE
        detail = MESSAGES.get(kind) or f"Claude Code 오류 (종료 코드 {done.returncode})"
        raise ClaudeCliError(f"{detail} {why[:200]}".strip(), kind)
    data = result.get("structured_output")
    if schema is not None and data is None:
        try:
            data = json.loads(result.get("result") or "")
        except ValueError:
            raise ClaudeCliError("Claude Code 가 정해진 형식(JSON)으로 답하지 않았습니다.")
    return {"text": result.get("result") or "", "data": data, "texts": texts}


def status(exe: list[str] | None = None, timeout: float = 60) -> dict:
    """설치·로그인 확인 (모델은 부르지 않음). 로그인 정보는 저장하지 않고 방식만 본다."""
    exe = exe or find_claude()
    out = {"found": bool(exe), "path": exe[-1] if exe else "", "version": "", "logged_in": None, "method": "",
           "plan": "", "subscription": None, "detail": ""}
    if not exe:
        out["detail"] = MESSAGES["missing"]
        return out
    try:
        out["version"] = (_run(exe + ["--version"], timeout=timeout).stdout or "").strip()[:60]
        done = _run(exe + ["auth", "status", "--json"], timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        out["detail"] = f"Claude Code 를 실행하지 못했습니다: {type(e).__name__}"
        return out
    try:
        info = json.loads(done.stdout or "")
    except ValueError:
        info = None
    if not isinstance(info, dict):
        out["detail"] = "로그인 상태를 읽지 못했습니다 (Claude Code 를 최신으로 업데이트해 보세요)."
        return out
    out["logged_in"] = bool(info.get("loggedIn"))
    method = str(info.get("authMethod") or "")
    plan = str(info.get("subscriptionType") or "")
    out["method"], out["plan"] = method, plan
    if not out["logged_in"]:
        out["subscription"] = False
        out["detail"] = MESSAGES["login"]
    elif method in ("api_key", "apiKey", "console") or info.get("apiProvider") not in (None, "", "firstParty"):
        out["subscription"] = False
        out["detail"] = ("Claude Code 가 구독이 아닌 방식(" + (method or str(info.get("apiProvider"))) +
                         ")으로 로그인되어 있어 충전 요금에서 나갑니다 — claude 를 열고 /login 으로 claude.ai 계정을 고르세요.")
    else:
        out["subscription"] = True
        out["detail"] = "Claude 구독으로 연결됨" + (f" ({plan})" if plan else "")
    return out


def save_check(conn, info: dict) -> None:
    """확인 결과를 설정 화면에 보여 주려고 남긴다 (설치·로그인 여부와 방식만, 계정 정보는 남기지 않음)."""
    from .. import db

    keep = {k: info.get(k) for k in ("found", "path", "version", "logged_in", "method", "plan", "subscription", "detail")}
    db.set_setting(conn, CHECK_KEY, json.dumps({**keep, "at": db.now()}, ensure_ascii=False))


def saved_check(conn) -> dict | None:
    from .. import db

    try:
        data = json.loads(db.get_setting(conn, CHECK_KEY) or "null")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
