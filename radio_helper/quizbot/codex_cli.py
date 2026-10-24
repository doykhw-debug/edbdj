"""ChatGPT 구독으로 분석하기 (0.68) — 공식 OpenAI Codex CLI(codex 프로그램)를 화면 없이 실행해 결과만 받는다.

- 사용자가 이 PC 에서 Codex 에 'ChatGPT 로 로그인'(Plus·Pro 등 구독 계정)을 한 번 해 두면, codex 프로그램이 그 로그인으로
  GPT 를 부른다 → ChatGPT 구독 사용량에서 나가고 API 충전 요금은 들지 않는다.
- 이 프로그램은 로그인 정보(토큰·비밀번호·쿠키)를 읽거나 저장하거나 넘기지 않는다. codex 프로그램만 실행한다.
- 자식 프로세스 환경에서 OPENAI_API_KEY·CODEX_API_KEY 를 지운다 (있으면 구독 대신 API 키 요금으로 나갈 수 있음).
- 빈 임시 폴더에서 읽기 전용 샌드박스로 실행하고, 대화 기록은 남기지 않는다(--ephemeral). 편성표·신청곡 확인만 웹 검색(--search).
- 확인한 명령 모양 (codex-cli 0.163): `codex [--search] exec --skip-git-repo-check --ephemeral --sandbox read-only --json
  -o <마지막 답 파일> [--output-schema <파일>] [-m 모델] [-c model_reasoning_effort="low"] [-i 사진] -`,
  로그인 확인은 `codex login status`, 로그인은 `codex login` (브라우저에서 ChatGPT 계정).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

API_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN")
CHECK_KEY = "codex_cli.check"
TIMEOUT = 300
LIMIT_PAUSE = 1800
MAX_LIMIT_PAUSE = 8 * 3600
# 'try again in 2 hours 13 minutes' / 'try again in 45 minutes' / 'try again at 3:05 PM'
AGAIN_IN = re.compile(r"try again in\s+(?:(\d+)\s*(?:hours?|hrs?|h))?\s*(?:(\d+)\s*(?:minutes?|mins?|m))?", re.I)
AGAIN_AT = re.compile(r"(?:try again at|resets? (?:at)?)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)
_limited_until = 0.0


class CodexCliError(RuntimeError):
    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind   # missing / login / limit / timeout / error


MESSAGES = {
    "missing": "Codex(codex 프로그램)가 설치되어 있지 않습니다 — ChatGPT 구독으로 쓰려면 Codex 를 설치하세요.",
    "login": ("Codex 에 ChatGPT 계정으로 로그인되어 있지 않습니다. ChatGPT 웹·앱의 로그인과는 따로라서 Codex 에도 한 번 "
              "로그인해야 합니다 — 'ChatGPT 로그인' 버튼(또는 명령 창에서 codex login)."),
    "limit": "ChatGPT 구독 사용량 한도에 걸렸습니다 (시간이 지나면 풀림).",
    "timeout": "Codex 응답이 너무 늦어 멈췄습니다.",
}


def _windows() -> bool:
    return sys.platform == "win32"


def _npm_target(shim: Path) -> list[str] | None:
    """npm 으로 설치한 codex.cmd 는 cmd.exe 를 거치므로, 그 안의 bin/codex.js 를 node 로 바로 실행한다."""
    script = shim.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    node = shutil.which("node")
    if script.is_file() and node:
        return [node, str(script)]
    return None


def candidates() -> list[Path]:
    home = Path.home()
    out = []
    override = os.environ.get("RADIO_HELPER_CODEX")
    if override:
        out.append(Path(override))
    found = shutil.which("codex")
    if found:
        out.append(Path(found))
    if _windows():
        out += [Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "npm" / "codex.cmd",
                Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "Programs" / "codex" / "codex.exe",
                home / ".local" / "bin" / "codex.exe"]
    else:
        out += [home / ".local" / "bin" / "codex", Path("/usr/local/bin/codex")]
    return out


def find_codex() -> list[str] | None:
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
    return {k: v for k, v in os.environ.items() if k not in API_ENV}


def _run(cmd: list[str], stdin: str = "", timeout: float = TIMEOUT, cwd: str | None = None) -> subprocess.CompletedProcess:
    kwargs = {}
    if _windows():
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, env=child_env(), cwd=cwd, **kwargs)


def _kind(text: str) -> str:
    low = (text or "").lower()
    if any(w in low for w in ("usage limit", "hit your", "limit reached", "rate limit", "too many requests", "429",
                              "quota", "사용량 한도")):
        return "limit"
    if any(w in low for w in ("not logged in", "codex login", "unauthorized", "401", "authentication", "sign in",
                              "log in", "login", "로그인")):
        return "login"
    return "error"


def limited() -> bool:
    return time.time() < _limited_until


def pause_note() -> str:
    if not limited():
        return ""
    return time.strftime("%H:%M", time.localtime(_limited_until)) + " 까지"


def reset_time(text: str, now: float | None = None) -> float | None:
    """한도 안내에서 다시 쓸 수 있는 시각. 모르면 None."""
    from datetime import datetime, timedelta

    now = now if now is not None else time.time()
    m = AGAIN_IN.search(text or "")
    if m and (m.group(1) or m.group(2)):
        return now + int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
    m = AGAIN_AT.search(text or "")
    if not m:
        return None
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if half:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if half == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    base = datetime.fromtimestamp(now)
    at = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at <= base:
        at += timedelta(days=1)
    return at.timestamp()


def pause_for_limit(why: str, now: float | None = None) -> float:
    global _limited_until
    now = now if now is not None else time.time()
    at = reset_time(why, now)
    _limited_until = min(at + 60, now + MAX_LIMIT_PAUSE) if at else now + LIMIT_PAUSE
    return _limited_until


def strict_schema(schema):
    """구조화 답 규칙(strict)에 맞게: 객체마다 additionalProperties false, 모든 칸 필수(원래 선택 칸은 null 도 허용)."""
    if isinstance(schema, list):
        return [strict_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: strict_schema(v) for k, v in schema.items() if k not in ("default", "examples")}
    if out.get("type") == "object" or "properties" in out:
        props = out.get("properties") or {}
        required = set(schema.get("required") or [])
        for name, sub in props.items():
            if name not in required and isinstance(sub, dict):
                props[name] = _nullable(sub)
        out["properties"] = props
        out["required"] = list(props)
        out["additionalProperties"] = False
    return out


def _nullable(sub: dict) -> dict:
    t = sub.get("type")
    if isinstance(t, str) and t != "null":
        sub = {**sub, "type": [t, "null"]}
    elif isinstance(t, list) and "null" not in t:
        sub = {**sub, "type": [*t, "null"]}
    if "enum" in sub and None not in sub["enum"]:
        sub = {**sub, "enum": [*sub["enum"], None]}
    return sub


def drop_nulls(value):
    """선택 칸을 null 로 채운 답에서 null 칸을 뺀다 (원래 모양대로)."""
    if isinstance(value, dict):
        return {k: drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [drop_nulls(v) for v in value]
    return value


# 사용량 줄이기 (0.72, codex-cli 0.164 에서 받는 설정 확인): '빠른 모드'(우선 처리 — 사용량이 더 듦) 끄기,
# 우리 일과 상관없는 환경·앱 안내문을 지시에 붙이지 않기. 모르는 키면 Codex 가 무시만 한다.
LEAN = ["-c", "features.fast_mode=false", "-c", "include_environment_context=false",
        "-c", "include_apps_instructions=false", "-c", "include_collaboration_mode_instructions=false"]
# Codex 가 매번 붙이는 긴 코딩용 기본 지시 대신 쓰는 짧은 지시 (model_instructions_file — 입력 토큰을 크게 줄임)
BASE_INSTRUCTIONS = ("당신은 라디오 참여 도우미의 분석기입니다. 명령 실행·파일 읽기·수정 같은 도구는 쓰지 않고, "
                     "사용자 메시지의 [지시]를 따라 [입력]을 보고 바로 답합니다. 형식이 정해져 있으면 그 형식(JSON)만 답합니다.")
DEFAULT_MODEL = "gpt-6-luna"     # Codex 목록에서 가장 가볍고 싼 모델 ('빠르고 저렴, 쉬운 일용'). 비우면 Codex 기본(gpt-6.1-sol, 무거움)
MODEL_PROBLEM = ("not supported", "unsupported", "does not exist", "not found", "not available", "invalid model",
                 "unknown model")


def _model_problem(text: str) -> bool:
    low = (text or "").lower()
    return "model" in low and any(w in low for w in MODEL_PROBLEM)


def _json_text(text: str):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)


def run(system: str, user: str, schema: dict | None = None, images=(), search: bool = False, model: str | None = None,
        effort: str | None = None, timeout: float = TIMEOUT, exe: list[str] | None = None, runner=None) -> dict:
    """codex exec 한 번. images: [(설명, JPEG 바이트)]. → {text, data(구조화 결과), usage(토큰 수), model(쓴 모델)}
    고른 모델을 이 계정에서 못 쓰면 Codex 기본 모델로 한 번 다시 한다 (model 칸에 그렇게 적음)."""
    try:
        return _run_once(system, user, schema, images, search, model, effort, timeout, exe, runner)
    except CodexCliError as e:
        if not model or not _model_problem(str(e)):
            raise
        done = _run_once(system, user, schema, images, search, None, effort, timeout, exe, runner)
        return {**done, "model": f"Codex 기본 ({model} 은 이 계정에서 못 씀)"}


def _run_once(system, user, schema, images, search, model, effort, timeout, exe, runner) -> dict:
    if limited():
        raise CodexCliError(f"{MESSAGES['limit']} {pause_note()} 쉬는 중.", "limit")
    exe = exe or find_codex()
    if not exe:
        raise CodexCliError(MESSAGES["missing"], "missing")
    work = tempfile.mkdtemp(prefix="radio_codex_")   # 빈 폴더: 이 프로그램 파일·데이터를 Codex 가 보지 않게
    try:
        last = os.path.join(work, "last.txt")
        cmd = exe + (["--search"] if search else []) + [
            "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "read-only", "--json", "--color", "never",
            "-C", work, "-o", last] + LEAN
        base = os.path.join(work, "instructions.md")
        Path(base).write_text(BASE_INSTRUCTIONS, encoding="utf-8")
        cmd += ["-c", f"model_instructions_file={json.dumps(base)}"]
        if schema is not None:
            path = os.path.join(work, "schema.json")
            Path(path).write_text(json.dumps(strict_schema(schema), ensure_ascii=False), encoding="utf-8")
            cmd += ["--output-schema", path]
        if model:
            cmd += ["-m", model]
        if effort:
            cmd += ["-c", f'model_reasoning_effort="{effort}"']
        labels = []
        for n, (label, data) in enumerate(images or ()):
            path = os.path.join(work, f"image{n + 1}.jpg")
            Path(path).write_bytes(data)
            cmd += ["-i", path]
            labels.append(f"사진 {n + 1}: {label}")
        prompt = ("[지시]\n" + system + "\n- 명령 실행·파일 읽기 같은 도구는 쓰지 말고"
                  + (" (웹 검색만 써도 됨)" if search else "") + " 바로 답합니다.\n\n"
                  + ("[붙인 사진]\n" + "\n".join(labels) + "\n\n" if labels else "") + "[입력]\n" + user)
        cmd.append("-")
        try:
            done = (runner or _run)(cmd, prompt, timeout, work)
        except subprocess.TimeoutExpired:
            raise CodexCliError(MESSAGES["timeout"], "timeout")
        except OSError as e:
            raise CodexCliError(f"Codex 를 실행하지 못했습니다: {type(e).__name__}", "missing")
        try:
            answer = Path(last).read_text(encoding="utf-8")
        except OSError:
            answer = ""
    finally:
        shutil.rmtree(work, ignore_errors=True)
    failure, messages, usage = "", [], {}
    for line in (done.stdout or "").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = event["usage"]
        if event.get("type") == "turn.failed":
            failure = str((event.get("error") or {}).get("message") or event)
        elif event.get("type") == "error" and not str(event.get("message", "")).startswith("Reconnecting"):
            failure = failure or str(event.get("message") or "")
        elif event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and (item.get("text") or "").strip():
                messages.append(item["text"])
    if not answer.strip() and messages:
        answer = messages[-1]
    if failure or (done.returncode not in (0, None) and not answer.strip()):
        why = " ".join(x for x in (failure, (done.stderr or "")[-400:]) if x).strip()
        kind = _kind(why) if why else "error"
        if kind == "limit":
            pause_for_limit(why)
        detail = MESSAGES.get(kind) or f"Codex 오류 (종료 코드 {done.returncode})"
        raise CodexCliError(f"{detail} {why[:200]}".strip(), kind)
    if not answer.strip():
        raise CodexCliError("Codex 가 빈 답을 냈습니다.")
    data = None
    if schema is not None:
        try:
            data = drop_nulls(_json_text(answer))
        except ValueError:
            raise CodexCliError("Codex 가 정해진 형식(JSON)으로 답하지 않았습니다.")
    return {"text": answer, "data": data, "usage": usage, "model": model or "Codex 기본"}


def status(exe: list[str] | None = None, timeout: float = 60, runner=None) -> dict:
    """설치·로그인 확인 (모델은 부르지 않음). 로그인 방식만 보고 계정 정보는 남기지 않는다."""
    exe = exe or find_codex()
    out = {"found": bool(exe), "path": exe[-1] if exe else "", "version": "", "logged_in": None, "method": "",
           "subscription": None, "detail": ""}
    if not exe:
        out["detail"] = MESSAGES["missing"]
        return out
    run_ = runner or _run
    try:
        out["version"] = (run_(exe + ["--version"], "", timeout, None).stdout or "").strip()[:60]
        done = run_(exe + ["login", "status"], "", timeout, None)
    except (OSError, subprocess.TimeoutExpired) as e:
        out["detail"] = f"Codex 를 실행하지 못했습니다: {type(e).__name__}"
        return out
    text = " ".join(((done.stdout or "") + " " + (done.stderr or "")).split())
    low = text.lower()
    if "not logged in" in low or not low:
        out.update(logged_in=False, subscription=False, detail=MESSAGES["login"] + f" (확인한 프로그램: {out['path']})")
    elif "chatgpt" in low:
        out.update(logged_in=True, method="chatgpt", subscription=True, detail="ChatGPT 구독으로 연결됨")
    elif "api key" in low or "api_key" in low:
        out.update(logged_in=True, method="api_key", subscription=False,
                   detail="Codex 가 ChatGPT 구독이 아닌 API 키로 로그인되어 있어 충전 요금에서 나갑니다 — 'ChatGPT 로그인'으로 "
                          "다시 로그인하세요 (codex logout 뒤 codex login).")
    else:
        out.update(logged_in=None, detail=f"로그인 상태를 읽지 못했습니다: {text[:120]}")
    return out


def open_login(exe: list[str] | None = None, popen=None) -> None:
    """PC 에 'ChatGPT 로그인' 창(명령 창)을 연다: codex login → 브라우저에서 사용자가 ChatGPT 계정으로 직접 로그인.
    이 프로그램은 로그인 과정·결과(토큰)를 보지 않는다."""
    exe = exe or find_codex()
    if not exe:
        raise CodexCliError(MESSAGES["missing"], "missing")
    if not _windows():
        raise CodexCliError("이 컴퓨터에서는 터미널에서 직접 실행하세요: codex login", "error")
    command = subprocess.list2cmdline(exe + ["login"])
    (popen or subprocess.Popen)('cmd /s /k "' + command + '"', env=child_env(),
                                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10))


def save_check(conn, info: dict) -> None:
    from .. import db

    keep = {k: info.get(k) for k in ("found", "path", "version", "logged_in", "method", "subscription", "detail")}
    db.set_setting(conn, CHECK_KEY, json.dumps({**keep, "at": db.now()}, ensure_ascii=False))


def saved_check(conn) -> dict | None:
    from .. import db

    try:
        data = json.loads(db.get_setting(conn, CHECK_KEY) or "null")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
