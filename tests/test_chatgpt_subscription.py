"""0.68: ChatGPT 구독으로 분석 — 공식 Codex CLI 를 ChatGPT 계정 로그인 그대로 실행 (API 키·충전 요금 없음)."""

import json
import subprocess
from pathlib import Path

import pytest
from conftest import csrf
from test_claude_subscription import FakeCli

from radio_helper import db
from radio_helper.quizbot import answerer, claude_cli, codex_cli


class FakeRun:
    """codex 프로그램 대신: 받은 명령을 적고, -o 파일에 답을 쓰고 JSONL 을 돌려준다."""

    def __init__(self, answer="", events=(), returncode=0, stderr=""):
        self.answer, self.events, self.returncode, self.stderr, self.calls = answer, list(events), returncode, stderr, []

    def __call__(self, cmd, stdin, timeout, cwd):
        self.calls.append({"cmd": list(cmd), "stdin": stdin, "cwd": cwd,
                           "schema": json.loads(Path(cmd[cmd.index("--output-schema") + 1]).read_text(encoding="utf-8"))
                           if "--output-schema" in cmd else None,
                           "images": [Path(cmd[i + 1]).read_bytes() for i, c in enumerate(cmd) if c == "-i"]})
        if self.answer:
            Path(cmd[cmd.index("-o") + 1]).write_text(self.answer, encoding="utf-8")
        out = "\n".join(json.dumps(e) for e in self.events)
        return subprocess.CompletedProcess(cmd, self.returncode, out, self.stderr)


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(codex_cli, "_limited_until", 0.0)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def test_run_builds_safe_exec_command_and_reads_json():
    run = FakeRun('{"found": true, "text": "신청곡 받아요"}')
    schema = {"type": "object", "properties": {"found": {"type": "boolean"}, "text": {"type": "string"},
                                               "note": {"type": "string"}}, "required": ["found", "text"]}
    out = codex_cli.run("시스템 지시", "본문", schema=schema, images=[("채팅 창", b"\xff\xd8jpeg")], model="gpt-6.1-sol",
                        effort="low", exe=["codex"], runner=run)
    assert out["data"] == {"found": True, "text": "신청곡 받아요"}
    call = run.calls[0]
    cmd = call["cmd"]
    assert cmd[:2] == ["codex", "exec"] and "--search" not in cmd
    for flag in ("--skip-git-repo-check", "--ephemeral", "--json"):
        assert flag in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "read-only" and cmd[cmd.index("-m") + 1] == "gpt-6.1-sol"
    assert 'model_reasoning_effort="low"' in cmd and cmd[-1] == "-"
    assert call["images"] == [b"\xff\xd8jpeg"] and "사진 1: 채팅 창" in call["stdin"]
    assert "시스템 지시" in call["stdin"] and "[입력]\n본문" in call["stdin"]
    assert call["schema"]["additionalProperties"] is False and call["schema"]["required"] == ["found", "text", "note"]
    assert call["schema"]["properties"]["note"]["type"] == ["string", "null"]               # 선택 칸은 null 허용
    assert not Path(call["cwd"]).exists()                                                   # 빈 임시 폴더는 지움


def test_web_search_and_null_cleanup():
    run = FakeRun("편성표 글", events=[{"type": "item.completed", "item": {"type": "agent_message", "text": "편성표 글"}}])
    out = codex_cli.run("s", "u", search=True, exe=["codex"], runner=run)
    assert out["text"] == "편성표 글" and run.calls[0]["cmd"][:3] == ["codex", "--search", "exec"]
    assert codex_cli.drop_nulls({"a": 1, "b": None, "c": [{"d": None, "e": 2}]}) == {"a": 1, "c": [{"e": 2}]}


def test_errors_login_limit_missing():
    with pytest.raises(codex_cli.CodexCliError) as e:
        codex_cli.run("s", "u", exe=["codex"], runner=FakeRun(events=[
            {"type": "turn.failed", "error": {"message": "401 Unauthorized: please run codex login"}}], returncode=1))
    assert e.value.kind == "login" and "ChatGPT 계정으로 로그인" in str(e.value)
    with pytest.raises(codex_cli.CodexCliError) as e:
        codex_cli.run("s", "u", exe=["codex"], runner=FakeRun(events=[
            {"type": "error", "message": "Reconnecting... 1/5"},
            {"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again in 2 hours 10 minutes."}}],
            returncode=1))
    assert e.value.kind == "limit" and codex_cli.limited()
    with pytest.raises(codex_cli.CodexCliError, match="쉬는 중"):
        codex_cli.run("s", "u", exe=["codex"], runner=FakeRun("{}"))                         # 한도 동안은 부르지 않음
    codex_cli._limited_until = 0.0
    with pytest.raises(codex_cli.CodexCliError) as e:
        codex_cli.run("s", "u", exe=None, runner=FakeRun("{}")) if not codex_cli.find_codex() else \
            (_ for _ in ()).throw(codex_cli.CodexCliError("x", "missing"))
    assert e.value.kind == "missing"


def test_reset_time_parsing():
    assert codex_cli.reset_time("Try again in 2 hours 13 minutes.", now=1000.0) == 1000 + 2 * 3600 + 13 * 60
    assert codex_cli.reset_time("try again in 45 minutes", now=0.0) == 45 * 60
    assert codex_cli.reset_time("try again at 3:05 PM", now=0.0) is not None
    assert codex_cli.reset_time("no idea", now=0.0) is None


@pytest.mark.parametrize("out, sub, logged", [
    ("Logged in using ChatGPT", True, True),
    ("Logged in using an API key - sk-proj-***abc", False, True),
    ("Not logged in", False, False),
])
def test_status_reads_login_method(out, sub, logged):
    def runner(cmd, stdin, timeout, cwd):
        text = "codex-cli 0.163.0" if cmd[-1] == "--version" else out
        return subprocess.CompletedProcess(cmd, 0, text, "")
    info = codex_cli.status(exe=["codex"], runner=runner)
    assert info["subscription"] is sub and info["logged_in"] is logged and info["version"] == "codex-cli 0.163.0"
    assert "sk-proj" not in json.dumps(info)                                                # 키 글자는 남기지 않음


def test_child_env_drops_api_keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("CODEX_API_KEY", "sk-y")
    env = codex_cli.child_env()
    assert "OPENAI_API_KEY" not in env and "CODEX_API_KEY" not in env


# ── 분석기 ────────────────────────────────────────────────────────────
class FakeCodex:
    CodexCliError = codex_cli.CodexCliError

    def __init__(self, *results, limited=False):
        self.results, self.calls, self._limited = list(results), [], limited

    def run(self, **kw):
        self.calls.append(kw)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def limited(self):
        return self._limited

    def pause_note(self):
        return "15:00 까지"


def test_codex_answerer_call_and_research():
    cli = FakeCodex({"text": "{}", "data": {"found": False, "text": ""}}, {"text": "오늘은 맑음", "data": None})
    a = answerer.CodexAnswerer("", "low", cli=cli)
    assert a.read_dj_notice("프로그램", b"jpeg") == {"found": False, "text": ""}
    assert a.research_today("2026-10-22") == "오늘은 맑음"
    assert cli.calls[0]["schema"] == answerer.DJ_NOTICE_SCHEMA and cli.calls[0]["images"]
    assert cli.calls[0]["model"] is None and cli.calls[0]["effort"] == "low"               # 모델 비우면 Codex 기본
    assert cli.calls[1]["search"] is True
    assert answerer.CodexAnswerer(cli=FakeCodex(limited=True)).paused().startswith("ChatGPT 구독 한도")


def test_make_ready_and_backup(conn, monkeypatch):
    monkeypatch.setattr(codex_cli, "find_codex", lambda: ["codex"])
    db.set_setting(conn, "quizbot.billing", "chatgpt")
    assert isinstance(answerer.make(conn), answerer.CodexAnswerer) and answerer.ready(conn) == (True, "")
    db.set_setting(conn, "quizbot.billing", "subscription")
    db.set_setting(conn, "quizbot.gpt_fallback", "chatgpt")
    a = answerer.make(conn, cli=FakeCli([]))
    assert isinstance(a.backup, answerer.CodexAnswerer)
    monkeypatch.setattr(codex_cli, "find_codex", lambda: None)
    db.set_setting(conn, "quizbot.billing", "chatgpt")
    ok, why = answerer.ready(conn)
    assert not ok and "Codex 를 설치" in why


def test_claude_limit_continues_with_chatgpt_subscription():
    notes = []
    backup = answerer.CodexAnswerer(cli=FakeCodex({"text": "{}", "data": {"found": True, "text": "공지"}}))
    a = answerer.ClaudeAnswerer(billing="subscription", note=notes.append, backup=backup,
                                cli=FakeCli([claude_cli.ClaudeCliError("Claude 구독 한도", "limit")]))
    assert a.read_dj_notice("프로그램", b"jpeg") == {"found": True, "text": "공지"}
    assert any("ChatGPT 구독(Codex)(으)로 대신함" in n for n in notes)


def test_settings_screen_chatgpt(client, conn, monkeypatch):
    monkeypatch.setattr(codex_cli, "find_codex", lambda: ["codex"])
    monkeypatch.setattr(codex_cli, "status", lambda: {"found": True, "path": "codex", "version": "codex-cli 0.163.0",
                                                       "logged_in": True, "method": "chatgpt", "subscription": True,
                                                       "detail": "ChatGPT 구독으로 연결됨"})
    html = client.get("/quizbot").get_data(as_text=True)
    assert "ChatGPT 구독 (Codex 로그인, 충전 요금 없음)" in html and "npm install -g @openai/codex" in html
    token = csrf(client, "/quizbot")
    client.post("/quizbot/settings", data={"csrf_token": token, "billing": "chatgpt", "gpt_fallback": "0",
                                           "chatgpt_model": ""})
    assert db.get_setting(conn, "quizbot.billing") == "chatgpt"
    r = client.post("/quizbot/chatgpt-check", data={"csrf_token": token}, follow_redirects=True)
    assert "ChatGPT 구독으로 연결됨" in r.get_data(as_text=True)
    assert "ChatGPT 구독 연결됨" in client.get("/quizbot").get_data(as_text=True)
    opened = []
    monkeypatch.setattr(codex_cli, "open_login", lambda: opened.append(1))
    client.post("/quizbot/chatgpt-login", data={"csrf_token": token})
    assert opened == [1]
