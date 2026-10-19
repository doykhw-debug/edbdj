"""Claude 구독으로 분석하기 (0.45): API 키(충전 요금) 대신 이 PC 에 로그인해 둔 공식 Claude Code 를 실행한다.

- 로그인 정보는 다루지 않는다. 자식 프로세스에서 ANTHROPIC_API_KEY 를 지워 구독으로 결제되게 한다.
- 빈 임시 폴더에서 실행하고 도구는 끈다 (편성표 찾기만 웹 검색).
- 구독으로 안 될 때 API 키로 이어 가는 것은 켜야만 한다 (충전 요금).
여기서는 가짜 claude 프로그램을 쓴다 (진짜는 conftest 가 막음).
"""

import json
import os
import sys
from pathlib import Path

import pytest

from conftest import csrf
from test_quizbot import analysis

from radio_helper import boards, db, schedules
from radio_helper.quizbot import answerer, claude_cli, config
from radio_helper.quizbot.answerer import AnswererError, ClaudeAnswerer

FAKE = r'''#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
def read(name, default=""):
    try:
        return open(os.path.join(here, name), encoding="utf-8").read()
    except OSError:
        return default
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("2.1.300 (Claude Code)"); sys.exit(0)
if args[:2] == ["auth", "status"]:
    print(read("auth.json", "{{}}")); sys.exit(0)
stdin = sys.stdin.read()
with open(os.path.join(here, "record.json"), "w", encoding="utf-8") as f:
    json.dump({{"args": args, "cwd": os.getcwd(), "files": os.listdir("."), "has_key": "ANTHROPIC_API_KEY" in os.environ,
               "stdin": stdin}}, f, ensure_ascii=False)
mode = read("mode.txt", "ok").strip()
print(json.dumps({{"type": "system", "subtype": "init"}}))
if mode == "ok":
    data = json.loads(read("answer.json", "null"))
    schema = "--json-schema" in args
    print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "찾아볼게요"}}]}}}}))
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                      "result": json.dumps(data, ensure_ascii=False) if schema else data,
                      "structured_output": data if schema else None}}, ensure_ascii=False))
elif mode == "limit":
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": True,
                      "result": "Claude AI usage limit reached|1760000000"}}))
    sys.exit(1)
elif mode == "login":
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": True,
                      "result": "Invalid API key · Please run /login"}}))
    sys.exit(1)
else:
    sys.stderr.write("boom"); sys.exit(2)
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("가짜 claude 스크립트는 리눅스·맥에서만")
    folder = tmp_path / "fakeclaude"
    folder.mkdir()
    exe = folder / "claude"
    exe.write_text(FAKE.format(python=sys.executable), encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setattr(claude_cli, "candidates", lambda: [exe])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")

    class Fake:
        path = exe

        def mode(self, value):
            (folder / "mode.txt").write_text(value, encoding="utf-8")

        def answer(self, data):
            (folder / "answer.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        def auth(self, data):
            (folder / "auth.json").write_text(json.dumps(data), encoding="utf-8")

        def record(self):
            return json.loads((folder / "record.json").read_text(encoding="utf-8"))

    return Fake()


SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


# ── claude 프로그램 실행 ───────────────────────────────────────────
def test_run_uses_official_cli_without_api_key_in_empty_folder(fake_claude):
    fake_claude.answer({"answer": "사과"})
    out = claude_cli.run("시스템 글", [{"type": "text", "text": "[채팅창 화면 12:00]"},
                                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                                 "data": "AAAA"}},
                                    {"type": "text", "text": "정답은?"}],
                         schema=SCHEMA, model="claude-opus-5-5", effort="medium")
    assert out["data"] == {"answer": "사과"}
    rec = fake_claude.record()
    args = rec["args"]
    assert args[0] == "-p" and args[args.index("--system-prompt") + 1] == "시스템 글"
    assert args[args.index("--tools") + 1] == "" and "--allowedTools" not in args   # 도구 모두 끔
    assert json.loads(args[args.index("--json-schema") + 1]) == SCHEMA
    assert args[args.index("--model") + 1] == "claude-opus-5-5" and args[args.index("--effort") + 1] == "medium"
    assert {"--no-session-persistence", "--strict-mcp-config"} <= set(args) and "--bare" not in args
    assert rec["has_key"] is False                  # API 키를 지워야 구독으로 결제됨
    assert rec["files"] == [] and Path(rec["cwd"]).name.startswith("radio_claude_")   # 빈 임시 폴더
    assert not Path(rec["cwd"]).exists()            # 끝나면 지움
    message = json.loads(rec["stdin"])
    assert message["type"] == "user" and message["message"]["content"][1]["type"] == "image"   # 사진은 그대로 전달
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test-not-real"   # 이 프로그램 쪽 환경은 그대로


def test_run_web_search_only_when_asked(fake_claude):
    fake_claude.answer("편성표 글")
    out = claude_cli.run("편성표", "찾아 주세요", tools=("WebSearch",))
    args = fake_claude.record()["args"]
    assert args[args.index("--tools") + 1] == "WebSearch" and args[args.index("--allowedTools") + 1] == "WebSearch"
    assert "--json-schema" not in args and out["text"] == "편성표 글" and out["texts"] == ["찾아볼게요"]


def test_run_errors_are_named(fake_claude, monkeypatch):
    fake_claude.mode("login")
    with pytest.raises(claude_cli.ClaudeCliError) as e:
        claude_cli.run("s", "u")
    assert e.value.kind == "login" and "로그인" in str(e.value)
    fake_claude.mode("crash")
    with pytest.raises(claude_cli.ClaudeCliError) as e:
        claude_cli.run("s", "u")
    assert e.value.kind == "error" and "boom" in str(e.value)
    fake_claude.mode("limit")
    with pytest.raises(claude_cli.ClaudeCliError) as e:
        claude_cli.run("s", "u")
    assert e.value.kind == "limit" and "한도" in str(e.value)
    # 한도에 걸리면 잠시 다시 부르지 않음 (매번 실행해 시간 쓰지 않게)
    fake_claude.mode("ok")
    fake_claude.answer("x")
    with pytest.raises(claude_cli.ClaudeCliError, match="쉬는 중"):
        claude_cli.run("s", "u")
    monkeypatch.setattr(claude_cli, "_limited_until", 0.0)
    assert claude_cli.run("s", "u")["text"] == "x"


def test_missing_cli():
    assert claude_cli.find_claude() is None
    with pytest.raises(claude_cli.ClaudeCliError) as e:
        claude_cli.run("s", "u")
    assert e.value.kind == "missing"
    assert claude_cli.status()["found"] is False


def test_npm_shim_runs_real_program_not_cmd(tmp_path, monkeypatch):
    npm = tmp_path / "npm"
    pkg = npm / "node_modules" / "@anthropic-ai" / "claude-code"
    pkg.mkdir(parents=True)
    (npm / "claude.cmd").write_text("@echo off", encoding="utf-8")
    (pkg / "cli.js").write_text("// cli", encoding="utf-8")
    monkeypatch.setattr(claude_cli, "candidates", lambda: [npm / "claude.cmd"])
    monkeypatch.setattr(claude_cli.shutil, "which", lambda name: "/usr/bin/node" if name == "node" else None)
    assert claude_cli.find_claude() == ["/usr/bin/node", str(pkg / "cli.js")]   # cmd.exe 를 거치지 않음
    (pkg / "bin").mkdir()
    (pkg / "bin" / "claude.exe").write_text("", encoding="utf-8")
    assert claude_cli.find_claude() == [str(pkg / "bin" / "claude.exe")]


def test_status_reads_login_method_only(fake_claude):
    fake_claude.auth({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                      "email": "someone@example.com", "subscriptionType": "max"})
    s = claude_cli.status()
    assert s["found"] and s["subscription"] is True and "max" in s["detail"] and s["version"].startswith("2.1")
    assert "email" not in s and "someone" not in json.dumps(s)        # 계정 정보는 가져오지 않음
    fake_claude.auth({"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"})
    s = claude_cli.status()
    assert s["subscription"] is False and "충전 요금" in s["detail"]
    fake_claude.auth({"loggedIn": False})
    assert claude_cli.status()["subscription"] is False and "로그인" in claude_cli.status()["detail"]


# ── 분석기 ──────────────────────────────────────────────────────────
class FakeCli:
    ClaudeCliError = claude_cli.ClaudeCliError

    def __init__(self, results):
        self.results, self.calls = list(results), []

    def run(self, system, content, schema=None, tools=(), model=None, effort=None):
        self.calls.append({"system": system, "content": content, "schema": schema, "tools": tools, "model": model})
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeApi:
    def __init__(self, text):
        self.calls = []
        outer = self

        class Messages:
            def create(self, **kw):
                outer.calls.append(kw)

                class Block:
                    type = "text"
                    text = outer.text

                class Response:
                    stop_reason = "end_turn"
                    content = [Block()]

                return Response()

        class Beta:
            messages = Messages()

        self.text, self.beta, self.messages = text, Beta(), Messages()


def test_subscription_analysis_goes_through_cli_not_api(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cli = FakeCli([{"data": analysis(answer="배"), "text": "", "texts": []}])
    a = ClaudeAnswerer("claude-opus-5-5", "high", billing="subscription", cli=cli)
    result = a.analyze("정오의 희망곡", "녹취", [], images=[("12:00:01", b"jpeg")])
    assert result.answer == "배" and a._client is None              # API 는 만들지도 않음
    call = cli.calls[0]
    assert call["schema"] == answerer.SCHEMA and call["model"] == "claude-opus-5-5" and call["tools"] == ()
    assert [b["type"] for b in call["content"]] == ["text", "image", "text"]


def test_subscription_schedule_research_uses_web_search_tool():
    cli = FakeCli([{"data": None, "text": "프로그램 | 진행자 | 12:00 | 14:00", "texts": []}])
    a = ClaudeAnswerer(billing="subscription", cli=cli)
    assert "12:00" in a.research_schedule("MBC FM4U", "2026-10-19", max_searches=5)
    assert cli.calls[0]["tools"] == ("WebSearch",) and "5번 안에서" in cli.calls[0]["system"]


def test_fallback_to_api_key_only_when_turned_on(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    limit = claude_cli.ClaudeCliError("Claude 구독 사용량 한도에 걸렸습니다", "limit")
    a = ClaudeAnswerer(billing="subscription", cli=FakeCli([limit]))
    with pytest.raises(AnswererError, match="한도"):
        a.analyze("p", "t", [])                                       # 끔: 충전 요금으로 넘어가지 않음

    notes, api = [], FakeApi(json.dumps(analysis(answer="감")))
    a = ClaudeAnswerer(billing="subscription", fallback=True, cli=FakeCli([limit]), client=api, note=notes.append)
    assert a.analyze("p", "t", []).answer == "감" and len(api.calls) == 1
    assert "API 키(충전 요금)로 대신" in notes[0]

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    a = ClaudeAnswerer(billing="subscription", fallback=True, cli=FakeCli([limit]), client=api)
    with pytest.raises(AnswererError):
        a.analyze("p", "t", [])                                       # 키가 없으면 대신할 수도 없음


def test_api_billing_unchanged():
    api = FakeApi(json.dumps(analysis(answer="귤")))
    cli = FakeCli([])
    a = ClaudeAnswerer(client=api, cli=cli)
    assert a.billing == "api" and a.analyze("p", "t", []).answer == "귤" and cli.calls == []


def test_make_and_ready_follow_settings(conn, monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(answerer, "get_api_key", lambda: None)
    assert answerer.billing(conn) == "api" and answerer.ready(conn)[0] is False
    db.set_setting(conn, "quizbot.billing", "subscription")
    ok, why = answerer.ready(conn)
    assert ok is False and "Claude Code 를 설치" in why
    exe = tmp_path / "claude"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(claude_cli, "candidates", lambda: [exe])
    assert answerer.ready(conn) == (True, "")
    db.set_setting(conn, "quizbot.billing_fallback", "1")
    db.set_setting(conn, "quizbot.effort", "low")
    a = answerer.make(conn)
    assert (a.billing, a.fallback, a.effort, a._client) == ("subscription", True, "low", None)
    db.set_setting(conn, "quizbot.billing", "weird")
    assert answerer.billing(conn) == "api"


def test_schedules_and_boards_use_chosen_billing(conn, monkeypatch):
    monkeypatch.setattr(answerer, "get_api_key", lambda: None)
    db.set_setting(conn, "quizbot.billing", "subscription")
    out = schedules.refresh(conn, channels=["MBC FM4U"], say=lambda m: None)
    assert "Claude Code 를 설치" in out[0]
    made = []
    monkeypatch.setattr(answerer, "ready", lambda c: (True, ""))
    monkeypatch.setattr(answerer, "make", lambda c: made.append(answerer.billing(c)) or ScheduleOnly())
    schedules.refresh(conn, channels=["MBC FM4U"], say=lambda m: None)
    assert made == ["subscription"]
    cid = boards.add_corner(conn, "https://www.imbc.com/broad/radio/fm4u/musicparty/v2/setting/corner/daily/1_2.html")
    boards.run(conn, cid, test=True, reader=lambda corner: {"text": "공지", "links": []}, poster=None)
    assert made == ["subscription", "subscription"]


class ScheduleOnly:
    def research_schedule(self, channel, today):
        return "글"

    def parse_schedule(self, text):
        return {"programs": []}

    def read_notice(self, *a):
        raise AnswererError("테스트: 여기까지")


# ── 화면 ──────────────────────────────────────────────────────────
def test_settings_page_switches_billing_and_checks(client, data_dir, monkeypatch):
    html = client.get("/quizbot").get_data(as_text=True)
    assert "Claude 연결" in html and 'value="subscription"' in html and "install.ps1" in html
    token = csrf(client, "/quizbot")
    client.post("/quizbot/settings", data={"csrf_token": token, "billing": "subscription",
                                           "live_min_confidence": "0.8"})
    conn = db.connect()
    assert config.get(conn, "quizbot.billing") == "subscription" and config.get(conn, "quizbot.billing_fallback") == "0"
    home = client.get("/").get_data(as_text=True)
    assert "Claude 구독 (Claude Code)" in home

    monkeypatch.setattr(claude_cli, "status", lambda: {
        "found": True, "path": "claude", "version": "2.1.300", "logged_in": True, "method": "claude.ai",
        "plan": "pro", "subscription": True, "detail": "Claude 구독으로 연결됨 (pro)"})
    page = client.post("/quizbot/claude-check", data={"csrf_token": token}, follow_redirects=True).get_data(as_text=True)
    assert "Claude 구독으로 연결됨 (pro)" in page
    assert claude_cli.saved_check(conn)["subscription"] is True
    client.post("/quizbot/settings", data={"csrf_token": token, "billing": "subscription", "billing_fallback": "1"})
    assert config.get(conn, "quizbot.billing_fallback") == "1"
    client.post("/quizbot/settings", data={"csrf_token": token, "billing": "api"})
    assert config.get(conn, "quizbot.billing") == "api"
    conn.close()
