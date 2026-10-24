"""0.67: AI 분석을 GPT(OpenAI API 키)로도 — 직접 고르거나, Claude 로 안 될 때 이어서."""

import json

import pytest
from conftest import csrf
from test_claude_subscription import FakeCli

from radio_helper import db
from radio_helper.quizbot import answerer, claude_cli


def reply(text, status="completed"):
    return {"status": status, "output": [{"type": "reasoning"},
                                         {"type": "message", "content": [{"type": "output_text", "text": text}]}]}


class FakePost:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def __call__(self, url, body, key):
        self.calls.append({"url": url, "body": json.loads(json.dumps(body)), "key": key})
        return self.answers.pop(0)


def gpt(post, **kw):
    return answerer.OpenAIAnswerer("gpt-5", "low", post=post, key="sk-test", **kw)


def test_call_sends_schema_images_and_reads_text():
    post = FakePost((200, reply('{"found": true, "text": "신청곡 받아요"}')))
    data = gpt(post).read_dj_notice("김영철의 파워FM", b"\xff\xd8jpeg")
    assert data == {"found": True, "text": "신청곡 받아요"}
    body = post.calls[0]["body"]
    assert post.calls[0]["url"] == "https://api.openai.com/v1/responses" and post.calls[0]["key"] == "sk-test"
    assert body["model"] == "gpt-5" and body["reasoning"] == {"effort": "low"}
    assert body["text"]["format"]["type"] == "json_schema" and body["text"]["format"]["schema"] == answerer.DJ_NOTICE_SCHEMA
    parts = body["input"][0]["content"]
    assert parts[1]["type"] == "input_image" and parts[1]["image_url"].startswith("data:image/jpeg;base64,")
    assert parts[-1]["type"] == "input_text" and "김영철의 파워FM" in parts[-1]["text"]


def test_model_without_reasoning_is_retried():
    post = FakePost((400, {"error": {"message": "Unsupported parameter: 'reasoning.effort'"}}), (200, reply('{"a": 1}')))
    assert gpt(post)._call("s", "u", {"type": "object"}) == '{"a": 1}'
    assert "reasoning" in post.calls[0]["body"] and "reasoning" not in post.calls[1]["body"]


@pytest.mark.parametrize("status, data, words", [
    (401, {}, "올바르지 않습니다"),
    (404, {}, "찾을 수 없습니다"),
    (429, {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}}, "크레딧"),
    (429, {"error": {"message": "Rate limit"}}, "잠시 뒤"),
    (500, {"error": {"message": "boom"}}, "OpenAI API 오류 500"),
])
def test_errors_are_explained(status, data, words):
    with pytest.raises(answerer.AnswererError, match=words):
        gpt(FakePost((status, data)))._call("s", "u", {})


def test_refusal_and_truncation():
    refusal = {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}
    with pytest.raises(answerer.AnswererError, match="거절"):
        gpt(FakePost((200, refusal)))._call("s", "u", {})
    cut = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []}
    with pytest.raises(answerer.AnswererError, match="잘렸습니다"):
        gpt(FakePost((200, cut)))._call("s", "u", {})


def test_web_research_uses_search_tool_and_falls_back_to_preview_name():
    post = FakePost((400, {"error": {"message": "Invalid tool type web_search"}}), (200, reply("편성표: 07:00 ...")))
    text = gpt(post).research_schedule("MBC FM4U", "2026-10-22")
    assert text == "편성표: 07:00 ..."
    assert [c["body"]["tools"][0]["type"] for c in post.calls] == ["web_search", "web_search_preview"]
    assert "text" not in post.calls[0]["body"]                                     # 조사는 글로 받고 정리는 따로


def test_no_key_is_explained(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(answerer, "get_openai_key", lambda: None)
    with pytest.raises(answerer.AnswererError, match="OpenAI API 키가 없습니다"):
        answerer.OpenAIAnswerer(post=FakePost())._call("s", "u", {})


# ── 설정대로 만들기 ────────────────────────────────────────────────────
def test_make_and_ready_follow_settings(conn, monkeypatch):
    monkeypatch.setattr(answerer, "get_openai_key", lambda: "sk-x")
    db.set_setting(conn, "quizbot.billing", "openai")
    db.set_setting(conn, "quizbot.gpt_model", "gpt-5-mini")
    a = answerer.make(conn)
    assert isinstance(a, answerer.OpenAIAnswerer) and a.model == "gpt-5-mini" and a.effort == "low"
    assert answerer.ready(conn) == (True, "")
    monkeypatch.setattr(answerer, "get_openai_key", lambda: None)
    ok, why = answerer.ready(conn)
    assert not ok and "OpenAI API 키" in why
    monkeypatch.setattr(answerer, "get_openai_key", lambda: "sk-x")
    db.set_setting(conn, "quizbot.billing", "subscription")
    assert answerer.make(conn, cli=FakeCli([])).backup is None                     # 꺼 두면 GPT 로 이어 가지 않음
    db.set_setting(conn, "quizbot.gpt_fallback", "1")
    assert isinstance(answerer.make(conn, cli=FakeCli([])).backup, answerer.OpenAIAnswerer)


def test_claude_subscription_failure_continues_with_gpt():
    notes = []
    post = FakePost((200, reply('{"found": false, "text": ""}')), (200, reply("조사 결과")))
    a = answerer.ClaudeAnswerer(billing="subscription", cli=FakeCli([claude_cli.ClaudeCliError("구독 한도"),
                                                                      claude_cli.ClaudeCliError("구독 한도")]),
                                note=notes.append, backup=gpt(post))
    assert a.read_dj_notice("프로그램", b"jpeg") == {"found": False, "text": ""}
    assert a.research_today("2026-10-22") == "조사 결과"
    assert len(post.calls) == 2 and any("GPT(OpenAI API 키)(으)로 대신함" in n for n in notes)


def test_paused_only_without_backup(monkeypatch):
    class LimitedCli(FakeCli):
        def limited(self):
            return True

        def pause_note(self):
            return "3시까지"
    monkeypatch.setattr(answerer, "get_api_key", lambda: None)
    assert "구독 한도" in answerer.ClaudeAnswerer(billing="subscription", cli=LimitedCli([])).paused()
    assert answerer.ClaudeAnswerer(billing="subscription", cli=LimitedCli([]), backup=gpt(FakePost())).paused() == ""


def test_settings_screen_saves_gpt(client, conn, monkeypatch):
    saved = []
    monkeypatch.setattr(answerer, "save_openai_key", saved.append)
    html = client.get("/quizbot").get_data(as_text=True)
    assert "GPT (OpenAI API 키" in html and 'name="openai_key"' in html
    r = client.post("/quizbot/settings", data={"csrf_token": csrf(client, "/quizbot"), "billing": "openai",
                                               "openai_key": "sk-new", "gpt_model": "gpt-5-mini", "gpt_fallback": "1"},
                    follow_redirects=True)
    assert "GPT(OpenAI) API 키를 저장했습니다" in r.get_data(as_text=True) and saved == ["sk-new"]
    assert (db.get_setting(conn, "quizbot.billing"), db.get_setting(conn, "quizbot.gpt_model"),
            db.get_setting(conn, "quizbot.gpt_fallback")) == ("openai", "gpt-5-mini", "openai")
    assert "sk-new" not in json.dumps([dict(r) for r in conn.execute("SELECT * FROM events")], ensure_ascii=False)


# ── 0.70: 기본 모델 gpt-4.1-mini (사용자 지정 'GPT-4-mini') ────────────────────
def test_default_model_is_gpt_4_1_mini_without_reasoning_setting(conn):
    from radio_helper.quizbot import config

    assert answerer.OPENAI_MODEL == "gpt-4.1-mini" and config.get(conn, "quizbot.gpt_model") == "gpt-4.1-mini"
    post = FakePost((200, reply('{"a": 1}')))
    a = answerer.OpenAIAnswerer("", "low", post=post, key="sk-test")
    assert a._call("s", "u", {"type": "object"}) == '{"a": 1}'
    body = post.calls[0]["body"]
    assert body["model"] == "gpt-4.1-mini" and "reasoning" not in body       # 4.1 계열은 추론 설정을 받지 않음


def test_070_moves_saved_gpt_model_once(conn):
    from radio_helper.quizbot import config

    db.set_setting(conn, "quizbot.gpt_model", "gpt-5")
    assert config.apply_070(conn) and db.get_setting(conn, "quizbot.gpt_model") == "gpt-4.1-mini"
    db.set_setting(conn, "quizbot.gpt_model", "gpt-5-mini")                    # 그 뒤 사용자가 바꾼 값은 그대로
    assert not config.apply_070(conn) and db.get_setting(conn, "quizbot.gpt_model") == "gpt-5-mini"
