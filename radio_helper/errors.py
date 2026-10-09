"""관리 화면 오류: 'Internal Server Error' 대신 무엇이 어디서 잘못됐는지 보여 주고, 데이터 폴더의
logs/errors.log 에 전체 내용을 남긴다. 관리 화면은 이 PC(127.0.0.1)에서만 열리므로 화면에 보여도 된다."""

from __future__ import annotations

import sqlite3
import traceback
from datetime import datetime

from markupsafe import escape

from . import db

LOG_NAME = "errors.log"
LOG_MAX_BYTES = 1_000_000


def _short(path: str) -> str:
    path = path.replace("\\", "/")
    for mark in ("/radio_helper/", "/templates/"):
        if mark in path:
            return mark.strip("/") + "/" + path.split(mark, 1)[1]
    return "/".join(path.rsplit("/", 2)[-2:])   # 예: flask/app.py (이 도우미 파일과 헷갈리지 않게)


def hint_for(err: BaseException, message: str) -> str:
    if isinstance(err, sqlite3.OperationalError) and "locked" in message:
        return ("다른 작업(프로그램·게시판 목록 다시 읽기, 청취, 풀어 쓰기 등)이 데이터를 쓰는 중이라 10초 넘게 기다리다 "
                "멈췄습니다. 잠시 뒤 새로 고침(F5) 하세요. 계속되면 이 화면을 캡처해 보내 주세요.")
    if isinstance(err, ImportError):
        return ("업데이트 파일이 일부만 풀렸을 수 있습니다. 업데이트 압축 파일을 다시 전부 풀고(덮어쓰기) "
                "검은 창을 모두 닫은 뒤 start_windows.bat 을 다시 실행하세요.")
    if type(err).__name__ == "UndefinedError":
        return ("예전에 켜 둔 검은 창(예전 파일로 도는 서버)이 새 화면 파일을 읽었을 수 있습니다. "
                "검은 창을 모두 닫고 start_windows.bat 을 다시 실행하세요.")
    return "이 화면을 캡처해 보내 주세요. 아래 '기록 파일'에 전체 내용이 남아 있습니다."


def describe(err: BaseException) -> dict:
    frames = traceback.extract_tb(err.__traceback__) if err.__traceback__ else []
    ours = [f for f in frames if "radio_helper" in f.filename.replace("\\", "/") or f.filename.endswith(".html")]
    text = str(err).strip()
    message = text.splitlines()[0][:300] if text else ""
    return {"type": type(err).__name__, "message": message,
            "where": [f"{_short(f.filename)}:{f.lineno} ({f.name})" for f in (ours or frames)[-4:]],
            "hint": hint_for(err, message)}


def log_error(err: BaseException, method: str, path: str) -> str | None:
    """전체 오류 내용을 기록 파일에 덧붙인다. 기록 파일 위치(실패하면 None)."""
    try:
        log_dir = db.data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log = log_dir / LOG_NAME
        if log.exists() and log.stat().st_size > LOG_MAX_BYTES:
            log.replace(log_dir / "errors.old.log")
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {method} {path}\n")
            f.write("".join(traceback.format_exception(type(err), err, err.__traceback__)))
            f.write("\n")
        return str(log)
    except Exception:
        return None


def error_page(err: BaseException, method: str, path: str) -> str:
    info = describe(err)
    logged = log_error(err, method, path)
    where = "".join(f"<li><code>{escape(w)}</code></li>" for w in info["where"])
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>오류 · 라디오 참여 도우미</title>
<style>body{{font-family:'Malgun Gothic',sans-serif;max-width:860px;margin:32px auto;padding:0 16px;line-height:1.6;
background:#fff;color:#111}}.box{{background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:12px 16px}}
code{{background:#f3f4f6;padding:1px 4px;border-radius:4px;word-break:break-all}}.muted{{color:#6b7280}}</style></head>
<body><h1>화면을 여는 중 오류가 났습니다</h1>
<div class="box"><p><b>주소</b> <code>{escape(method)} {escape(path)}</code></p>
<p><b>오류</b> <code>{escape(info['type'])}: {escape(info['message'])}</code></p>
<p><b>위치</b></p><ul>{where}</ul></div>
<p>{escape(info['hint'])}</p>
<p class="muted">기록 파일: <code>{escape(logged or '(남기지 못함)')}</code></p>
<p><a href="javascript:history.back()">← 뒤로</a> · <a href="/">첫 화면</a></p></body></html>"""
