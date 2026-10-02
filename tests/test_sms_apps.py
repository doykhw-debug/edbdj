"""휴대폰 문자(ADB) · 채널별 채팅 앱(고릴라·mini·콩) · 보내는 방법 고르기."""

from datetime import datetime

from conftest import csrf
from test_live import live_runner, start_live
from test_quizbot import FakeGorilla, ScriptAnswerer, analysis

import radio_helper.app as app_module
from radio_helper import db
from radio_helper.quizbot import config, live, runner, sms

SAMSUNG_COMPOSE = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.samsung.android.messaging" content-desc="" clickable="false" enabled="true" bounds="[0,0][1080,2340]">
<node index="1" text="#1077" resource-id="com.samsung.android.messaging:id/recipient_chip" class="android.widget.TextView" content-desc="" clickable="true" enabled="true" bounds="[40,200][300,260]" />
<node index="2" text="정답 사과 - 편집하는 아빠" resource-id="com.samsung.android.messaging:id/message_edit_text" class="android.widget.EditText" content-desc="" clickable="true" enabled="true" bounds="[100,2100][900,2200]" />
<node index="3" text="" resource-id="com.samsung.android.messaging:id/send_button1" class="android.widget.ImageButton" content-desc="보내기" clickable="true" enabled="true" bounds="[920,2100][1060,2220]" />
</node></hierarchy>"""
SAMSUNG_AFTER = SAMSUNG_COMPOSE.replace('text="정답 사과 - 편집하는 아빠" resource-id="com.samsung.android.messaging:id/message_edit_text"',
                                        'text="" resource-id="com.samsung.android.messaging:id/message_edit_text"')
GOOGLE_COMPOSE = """<hierarchy><node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
<node text="#1077" class="android.widget.TextView" resource-id="com.google.android.apps.messaging:id/conversation_title" bounds="[0,100][500,160]" enabled="true" />
<node text="안녕" class="android.widget.EditText" resource-id="com.google.android.apps.messaging:id/compose_message_text" bounds="[50,2200][850,2300]" enabled="true" />
<node text="" content-desc="SMS 보내기" class="android.view.View" resource-id="com.google.android.apps.messaging:id/send_message_button_icon" bounds="[880,2200][1040,2320]" enabled="true" />
<node text="" content-desc="보내기 예약" class="android.view.View" resource-id="com.google.android.apps.messaging:id/schedule_send_button" bounds="[700,2200][860,2320]" enabled="true" />
</node></hierarchy>"""


# ── 문자: 화면 읽기 ──────────────────────────────────────────────────
def test_sms_helpers():
    assert sms.parse_channels("파워FM=#1077\nMBC FM4U = #8000 = mini\nKBS 1라디오=\n잘못=12\n\n") == \
        {"파워FM": "#1077", "MBC FM4U": "#8000", "KBS 1라디오": "", "잘못": ""}
    assert sms.with_signature("정답 사과", "편집하는 아빠", True) == "정답 사과 - 편집하는 아빠"
    assert sms.with_signature("정답 사과", "편집하는 아빠", False) == "정답 사과"
    assert sms.with_signature("정답 사과", "", True) == "정답 사과"
    assert sms.sms_uri("#1077") == "smsto:%231077"
    assert sms.parse_devices("* daemon not running; starting now\nList of devices attached\nR5CT123\tdevice\nX\tunauthorized\n") == \
        [("R5CT123", "device"), ("X", "unauthorized")]
    assert sms.find_send_button(SAMSUNG_COMPOSE) == (990, 2160)
    assert sms.find_send_button(GOOGLE_COMPOSE) == (960, 2260)       # '보내기 예약'은 누르지 않는다
    assert sms.compose_text(SAMSUNG_AFTER) == "" and sms.compose_text(GOOGLE_COMPOSE) == "안녕"
    assert sms.shows_number(SAMSUNG_COMPOSE, "#1077")
    assert not sms.shows_number(SAMSUNG_COMPOSE.replace("#1077", "1077"), "#1077")  # '#'이 빠지면 다른 번호


class FakeAdb:
    """adb 명령 흉내: 화면 구조를 차례로 돌려준다."""
    def __init__(self, dumps, devices="List of devices attached\nR5CT123\tdevice\n"):
        self.dumps, self.devices, self.calls = list(dumps), devices, []

    def __call__(self, args, timeout):
        self.calls.append(args)
        if args[-1] == "devices":
            return 0, self.devices.encode()
        cmd = args[-1]
        if cmd.startswith("cat "):
            return 0, self.dumps.pop(0).encode()
        if "screencap" in args:
            return 0, b"\x89PNG\r\n\x1a\nfake"
        return 0, b""

    def shell_cmds(self):
        return [a[-1] for a in self.calls if "shell" in a]


def test_adb_send_checks_number_then_taps(tmp_path):
    fake = FakeAdb([SAMSUNG_COMPOSE, SAMSUNG_AFTER])
    phone = sms.AdbSms("adb", run=fake, sleep=lambda s: None, shots_dir=tmp_path)
    res = phone.send("#1077", "정답 사과 - 편집하는 아빠", shot="sms_test")
    assert res.status == "entered" and "#1077" in res.detail
    cmds = fake.shell_cmds()
    start = next(c for c in cmds if c.startswith("am start"))
    assert "smsto:%231077" in start and "'정답 사과 - 편집하는 아빠'" in start   # 한글·공백이 한 덩어리로 넘어감
    assert "input tap 990 2160" in cmds
    assert (tmp_path / "sms_test.png").exists() and (tmp_path / "sms_test_sent.png").exists()


def test_adb_send_refuses_wrong_number_and_unauthorized():
    fake = FakeAdb([SAMSUNG_COMPOSE.replace("#1077", "1077")])
    res = sms.AdbSms("adb", run=fake, sleep=lambda s: None).send("#1077", "정답")
    assert res.status == "failed" and "받는 번호" in res.detail
    assert not any(c.startswith("input tap") for c in fake.shell_cmds())
    res = sms.AdbSms("adb", run=FakeAdb([], devices="List of devices attached\nX\tunauthorized\n"),
                     sleep=lambda s: None).send("#1077", "정답")
    assert res.status == "failed" and "허용" in res.detail
    assert sms.AdbSms(None).status()[0] is False


# ── 채널 → 채팅 앱 ───────────────────────────────────────────────────
def test_channel_apps(conn):
    assert config.chat_app(conn, "파워FM") == "gorilla" and config.chat_app(conn, "SBS 라디오") == "gorilla"
    assert config.chat_app(conn, "MBC FM4U") == "mini" and config.chat_app(conn, "KBS 쿨FM") == "kong"
    assert config.chat_app(conn, "EBS FM") is None
    db.set_setting(conn, "channels", config.DEFAULTS["channels"] + "\nEBS FM=#1045=콩")
    assert config.chat_app(conn, "EBS FM") == "kong"
    assert config.sms_number(conn, "MBC FM4U") == "#8000" and config.sms_number(conn, "KBS 1라디오") is None
    assert config.app_settings("mini", {"gorilla.input_rect": "1", "quizbot.x": "2"}) == \
        {"mini.input_rect": "1", "quizbot.x": "2"}
    cfg = config.GorillaConfig.load(conn, "kong")
    assert (cfg.app, cfg.label, cfg.window_title) == ("kong", "콩", config.CHAT_APPS["kong"]["title"])


# ── 보내는 방법 ──────────────────────────────────────────────────────
class FakePhone:
    def __init__(self, ready=True):
        self.ready, self.sent = ready, []

    def is_ready(self):
        return self.ready

    def send(self, number, text):
        self.sent.append((number, text))
        return sms.SendResult("entered", "휴대폰 문자로 보냄")


def runner_with(conn, script, results, n=10, channel="MBC FM4U", phone=None, chats=None, **flags):
    start_live(conn, channel=channel, **flags)
    r, _ = live_runner(conn, script, ScriptAnswerer(results), FakeGorilla(), n=n)
    r.deps.sms = phone
    r.deps.chat_senders = chats
    return r


QUIZ = {3: "오늘의 퀴즈 철수가 좋아하는 과일 정답은 보내주세요"}


def test_mbc_quiz_goes_to_mini_app(conn):
    mini, gorilla = FakeGorilla(), FakeGorilla()
    r = runner_with(conn, QUIZ, [analysis()], chats={"mini": mini, "gorilla": gorilla})
    r.run_live()
    q = conn.execute("SELECT * FROM quizzes WHERE source = 'auto'").fetchone()
    assert mini.sent == ["사과"] and gorilla.sent == [] and q["sent_via"] == "mini"
    assert any("mini 전송" in e["message"] for e in conn.execute("SELECT message FROM events"))


def test_sms_route_sends_text_with_nickname(conn):
    db.save_profile(conn, {"nickname": "편집하는 아빠"})
    db.set_setting(conn, "send.route", "sms")
    phone = FakePhone()
    # 진행자가 '문자로만 받는다'고 해도(앱 응모 no) 문자로는 보낸다
    r = runner_with(conn, QUIZ, [analysis(gorilla_accepted="no")], phone=phone)
    r.run_live()
    q = conn.execute("SELECT * FROM quizzes WHERE source = 'auto'").fetchone()
    assert phone.sent == [("#8000", "정답 사과 - 편집하는 아빠")]
    assert (q["sent_via"], q["send_text"], q["entry_status"]) == ("sms", "정답 사과 - 편집하는 아빠", "entered")


def test_sms_route_holds_until_phone_connected(conn):
    db.set_setting(conn, "send.route", "sms")
    phone = FakePhone(ready=False)
    r = runner_with(conn, QUIZ, [analysis()], phone=phone)
    r.run_live()
    q = conn.execute("SELECT * FROM quizzes WHERE source = 'auto'").fetchone()
    assert phone.sent == [] and q["decision"] == runner.SMS_HOLD and q["entry_status"] == "pending"
    phone.ready = True
    r.retry_held(live.session(conn, datetime.now()))   # 청취 중 10초마다 하는 재시도
    assert phone.sent and conn.execute("SELECT entry_status FROM quizzes").fetchone()[0] == "entered"


def test_holds_without_number_or_app(conn):
    db.set_setting(conn, "send.route", "sms")
    r = runner_with(conn, QUIZ, [analysis()], channel="KBS 1라디오", phone=FakePhone())
    r.run_live()
    assert "문자 번호가 없음" in conn.execute("SELECT decision FROM quizzes").fetchone()[0]

    conn.execute("DELETE FROM quizzes")
    db.set_setting(conn, "channels", config.DEFAULTS["channels"] + "\nEBS FM=#1045")
    db.set_setting(conn, "send.route", "app")
    r = runner_with(conn, QUIZ, [analysis()], channel="EBS FM")
    r.run_live()
    assert "채팅 앱이 정해지지 않음" in conn.execute("SELECT decision FROM quizzes").fetchone()[0]


def test_approve_with_sms_route(client, conn, monkeypatch):
    monkeypatch.setattr(app_module, "launch_quizbot", lambda args: "q.log")
    conn.execute("""INSERT INTO quizzes (dedupe_key, account, channel, program, broadcast_date, question_key, question,
                    source, created_at, updated_at) VALUES ('k', 'a', '파워FM', 'p', '2026-10-05', 'q', '문제', 'auto', ?, ?)""",
                 (db.now(), db.now()))
    conn.commit()
    token = csrf(client, "/quizzes")
    client.post("/quizbot/quizzes/1/approve", data={"csrf_token": token, "answer": "사과", "route": "sms"})
    assert conn.execute("SELECT route, approved FROM quizzes").fetchone()[:] == ("sms", 1)
    phone = FakePhone()
    r, _ = live_runner(conn, {}, ScriptAnswerer([]), FakeGorilla(), n=1)
    r.deps.sms = phone
    r.process_approved(None)
    assert phone.sent == [("#1077", "정답 사과")]


# ── 화면 ────────────────────────────────────────────────────────────
def test_channel_add_and_route_choice(client, conn, monkeypatch):
    launched = []
    monkeypatch.setattr(app_module, "launch_quizbot", lambda args: launched.append(args) or "q.log")
    page = client.get("/").get_data(as_text=True)
    assert "MBC FM4U" in page and "KBS 쿨FM" in page and "보내는 방법" in page
    token = csrf(client, "/")
    client.post("/listen/channels", data={"csrf_token": token, "name": "MBC 표준FM", "number": "#8001", "app": ""})
    assert config.sms_number(conn, "MBC 표준FM") == "#8001" and config.chat_app(conn, "MBC 표준FM") == "mini"
    client.post("/listen/channels", data={"csrf_token": token, "name": "EBS FM", "number": "#1045", "app": "kong"})
    assert config.chat_app(conn, "EBS FM") == "kong"
    assert client.post("/listen/channels", data={"csrf_token": token, "name": "a=b"}).status_code == 302
    assert "a=b" not in config.get(conn, "channels")

    client.post("/listen/start", data={"csrf_token": token, "channel": "MBC FM4U", "route": "sms"})
    assert config.route(conn) == "sms" and db.get_setting(conn, "live.channel") == "MBC FM4U"
    db.set_setting(conn, "channels", config.DEFAULTS["channels"] + "\n동네라디오=#1234")
    client.post("/listen/start", data={"csrf_token": token, "channel": "동네라디오", "route": "app"})
    assert config.route(conn) == "sms"   # 채팅 앱이 없는 채널은 문자로


def test_sms_and_app_pages(client, conn, monkeypatch, data_dir):
    launched = []
    monkeypatch.setattr(app_module, "launch_quizbot", lambda args: launched.append(args) or "q.log")
    assert "USB 디버깅" in client.get("/sms").get_data(as_text=True)
    token = csrf(client, "/sms")
    client.post("/quizbot/tool", data={"csrf_token": token, "tool": "sms-check"})
    client.post("/quizbot/tool", data={"csrf_token": token, "tool": "sms-test"})
    assert launched[-2:] == [["sms-check"], ["sms-test"]]
    client.post("/sms", data={"csrf_token": token, "channels": "파워FM=#1077\r\nMBC FM4U=#8000", "sms.signature": "0"})
    assert config.channels(conn) == {"파워FM": "#1077", "MBC FM4U": "#8000"} and config.get(conn, "sms.signature") == "0"

    page = client.get("/gorilla?app=mini").get_data(as_text=True)
    assert "mini 창 맞추기" in page and "MBC FM4U 화이팅" in page
    client.post("/quizbot/tool", data={"csrf_token": token, "tool": "select-input", "app": "kong"})
    assert launched[-1] == ["select", "input", "--app", "kong"]
    assert client.post("/quizbot/tool", data={"csrf_token": token, "tool": "select-input", "app": "x"}).status_code == 400
    (data_dir / "inspect").mkdir(exist_ok=True)
    (data_dir / "inspect" / "sms_test.png").write_bytes(b"\x89PNG")
    assert client.get("/inspect-image/sms_test.png").status_code == 200
    assert "시험 문자" in client.get("/sms").get_data(as_text=True)


def test_cli_saves_settings_for_chosen_app(conn, monkeypatch):
    from radio_helper.quizbot import __main__ as cli
    from radio_helper.quizbot import gorilla

    class FakeG:
        def __init__(self, cfg):
            self.cfg = cfg

        def select(self, target):
            return {"gorilla.input_rect": "0.1,0.8,0.7,0.9", "gorilla.input_mode": "coords"}, "고릴라 입력칸을 저장했습니다"

    monkeypatch.setattr(gorilla, "Gorilla", FakeG)
    assert cli.cmd_select(conn, "input", "kong") == 0
    assert config.get(conn, "kong.input_rect") == "0.1,0.8,0.7,0.9" and config.get(conn, "gorilla.input_rect") == ""
    assert any("콩 입력칸을 저장했습니다" in e["message"] for e in conn.execute("SELECT message FROM events"))
