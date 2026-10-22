"""원버튼 찾기 + 감지된 오류로 바꾼 기본값 (0.50).

- 고릴라 창을 계속 못 찾음: 실제로는 앱이 켜져 있었고 공감로그(채팅)가 닫혀 있었다 (오른쪽에 광고 띠만).
  → 앱 실행 여부·입력칸·전송 버튼·채팅 목록을 한 번에 찾고, 닫혀 있으면 말풍선을 눌러 연다.
- 'You've hit your monthly spend limit … resets 3:30am' 를 한도로 알아보지 못해 1분마다 실패 → 풀리는 시각까지 쉼.
- 받아쓰기가 녹음보다 느림 (23초/10초, 후보 수 5) → CPU 는 후보 수 1, 그래도 느리면 저절로 작은 모델.
"""

import json
import time
from datetime import datetime

from conftest import csrf
from test_live import live_runner, start_live
from test_quizbot import FakeGorilla, ScriptAnswerer, analysis

from radio_helper import db
from radio_helper.quizbot import answerer, claude_cli, config, finder


def win(hwnd, rect, process="gorealra.exe", title="", iconic=False):
    return {"hwnd": hwnd, "title": title, "process": process, "path": f"C:/Apps/{process}", "rect": rect,
            "iconic": iconic}


# 관리자가 보낸 화면 그대로의 창 배치
GORILLA_OPEN = [win(1, (0, 0, 716, 949), title="고릴라"), win(2, (718, 0, 1352, 949))]
GORILLA_CLOSED = [win(1, (38, 108, 533, 772), title="고릴라"), win(3, (537, 108, 668, 772))]   # 광고 띠
KONG = [win(5, (0, 0, 1271, 842), "kong.exe", "KONG")]
MINI = [win(6, (0, 0, 766, 555), "mbcmini.exe", "MBC mini")]
OTHERS = [win(9, (0, 0, 1900, 1000), "chrome.exe", "고릴라 - 라디오 참여 도우미"),
          win(10, (100, 100, 900, 700), "premiere.exe", "Adobe Premiere Pro")]


class FakeDesk:
    def __init__(self, *window_sets, uia=None):
        self.sets, self.clicks, self.fronts, self.photos, self.back = list(window_sets), [], [], [], []
        self.by_uia = uia or finder.Found()
        self.clickable = True

    def windows(self):
        return list(self.sets[0] if len(self.sets) == 1 else self.sets.pop(0)) + OTHERS

    def restore(self, hwnds):
        pass

    def front(self, hwnds):
        self.fronts.append(list(hwnds))

    def foreground(self):
        return 77

    def back_to(self, hwnd):
        self.back.append(hwnd)

    def grab(self, rect):
        from PIL import Image

        return Image.new("RGB", (rect[2] - rect[0], rect[3] - rect[1]), "white")

    def click(self, point, wins, process):
        self.clicks.append(point)
        return self.clickable

    def uia(self, wins):
        return self.by_uia

    def capture(self, rect, name, boxes):
        self.photos.append((name, boxes))
        return f"{name}.png"


def no_claude(conn, app, image, origin):
    return None


def inside(point, rect):
    return rect[0] <= point[0] <= rect[2] and rect[1] <= point[1] <= rect[3]


# ── 앱 고르기 ──────────────────────────────────────────────────────
def test_pick_app_ignores_browser_and_other_programs():
    proc, wins = finder.pick_app("gorilla", GORILLA_OPEN + OTHERS + KONG)
    assert proc == "gorealra.exe" and [w["hwnd"] for w in wins] == [1, 2]
    assert finder.pick_app("kong", OTHERS + KONG)[0] == "kong.exe"
    assert finder.pick_app("mini", OTHERS + KONG) == ("", [])
    renamed = [win(7, (0, 0, 700, 900), "radioapp.exe", "")]     # 이름으로는 모르는 프로그램도 저장한 이름이면 고른다
    assert finder.pick_app("gorilla", renamed + OTHERS, saved_process="radioapp.exe")[0] == "radioapp.exe"


# ── 알려진 배치 (보낸 화면에서 잰 자리) ─────────────────────────────────
def test_layouts_hit_the_real_controls_in_the_screenshots():
    g = finder.template_find("gorilla", GORILLA_OPEN)
    # 공감로그 창: '공감로그를 남기시려면 로그인 해주세요' 칸 (808~1228, 870~945) · 파란 '전송' (1231~1352, 861~949)
    assert inside(finder.center(g.boxes["input"]), (808, 862, 1228, 949))
    assert inside(finder.center(g.boxes["send"]), (1231, 861, 1352, 949))
    assert g.boxes["chat"][1] > 180 and g.boxes["chat"][3] < 861 and g.chat_open
    k = finder.template_find("kong", KONG)
    assert inside(finder.center(k.boxes["input"]), (640, 693, 1260, 772))       # '로그인 후 채팅에 참여할 수 있습니다'
    assert inside(finder.center(k.boxes["send"]), (1147, 774, 1247, 828))       # 초록 버튼
    m = finder.template_find("mini", MINI)
    assert inside(finder.center(m.boxes["input"]), (395, 505, 708, 555))        # 'mini 메시지 작성'
    assert inside(finder.center(m.boxes["send"]), (712, 508, 766, 553))         # 보라 '등록'


def test_closed_chat_gives_the_speech_bubble():
    g = finder.template_find("gorilla", GORILLA_CLOSED)
    assert g.chat_open is False and not g.boxes
    assert inside(g.toggle, (466, 715, 496, 745))                               # 플레이어 오른쪽 아래 말풍선 (481, 730)
    narrow = finder.template_find("kong", [win(5, (0, 0, 635, 842), "kong.exe", "KONG")])
    assert narrow.chat_open is False and inside(narrow.toggle, (570, 110, 610, 150))   # 초록 말풍선 (590, 130)
    assert finder.template_find("mini", [win(6, (0, 0, 385, 555), "mbcmini.exe", "MBC")]).toggle is None


# ── 원버튼 찾기 흐름 ────────────────────────────────────────────────
def test_find_saves_screen_points_and_photo(conn):
    desk = FakeDesk(GORILLA_OPEN)
    report = finder.find(conn, "gorilla", desk=desk, see=no_claude)
    s = report["settings"]
    assert report["running"] and report["process"] == "gorealra.exe"
    assert (s["gorilla.input_mode"], s["gorilla.send_mode"]) == ("screen", "screen")
    ix, iy = (int(v) for v in s["gorilla.input_point"].split(","))
    sx, sy = (int(v) for v in s["gorilla.send_point"].split(","))
    assert inside((ix, iy), (808, 862, 1228, 949)) and inside((sx, sy), (1231, 861, 1352, 949))
    assert s["gorilla.point_window"] == "718,0,1352,949" and s["gorilla.process_name"] == "gorealra.exe"
    assert s["gorilla.chat_screen"] and s["gorilla.exe_path"] == "C:/Apps/gorealra.exe"
    assert s["gorilla.screen_region"] == ""                       # 예전 '창 영역' 방식은 지움
    assert desk.photos[0][0] == "gorilla_found" and len(desk.photos[0][1]) == 3
    assert desk.back == [77] and not desk.clicks                  # 하던 창으로 돌려놓음, 아무것도 안 누름
    assert "알려진 배치" in report["message"] and "확인 사진" in report["message"]


def test_find_opens_closed_gorilla_chat_once(conn):
    desk = FakeDesk(GORILLA_CLOSED, GORILLA_CLOSED, [win(1, (38, 108, 533, 772), title="고릴라"),
                                                     win(2, (537, 108, 1010, 772))])
    report = finder.find(conn, "gorilla", desk=desk, see=no_claude)
    assert len(desk.clicks) == 1 and inside(desk.clicks[0], (466, 715, 496, 745))
    assert report["found"]["opened"] and report["settings"]["gorilla.point_window"] == "537,108,1010,772"
    assert "말풍선 버튼을 눌러 열었습니다" in report["message"]


def test_toggle_not_clicked_when_covered(conn):
    desk = FakeDesk(GORILLA_CLOSED)
    desk.clickable = False      # 말풍선 자리를 다른 프로그램(예: 편집 프로그램)이 가림
    report = finder.find(conn, "gorilla", desk=desk, see=no_claude)
    assert not report["found"]["opened"] and not report["settings"]
    assert "가려 누르지 않음" in " ".join(report["found"]["notes"]) and "닫혀 있습니다" in report["message"]


def test_find_reports_app_off(conn):
    report = finder.find(conn, "mini", desk=FakeDesk([]), see=no_claude)
    assert not report["running"] and not report["settings"] and "켜져 있지 않습니다" in report["message"]


def test_uia_and_claude_come_before_layout(conn):
    by_uia = finder.Found(boxes={"input": (800, 870, 1200, 940)}, how={"input": "uia"})

    def claude_sees(conn_, app, image, origin):
        data = {"app_visible": True, "chat_open": True, "login_required": True, "input_box": [1, 1, 2, 2],
                "send_box": [1238, 860, 1348, 945], "chat_box": [730, 200, 1340, 850], "toggle_box": [],
                "note": "로그인 전"}
        return finder.vision_find(data, image.size, origin, 1.0)

    desk = FakeDesk(GORILLA_OPEN, uia=by_uia)
    report = finder.find(conn, "gorilla", desk=desk, see=claude_sees)
    how = report["found"]["how"]
    assert how == {"input": "uia", "send": "claude", "chat": "claude"}
    assert report["found"]["boxes"]["send"] == [1238, 860, 1348, 945]       # 사진 좌표 = 화면 (찍은 자리 0, 0)
    assert report["found"]["login_required"] and "로그인이 필요합니다" in report["message"]


def test_claude_says_closed_so_layout_is_not_trusted(conn):
    def closed(conn_, app, image, origin):
        return finder.vision_find({"chat_open": False, "toggle_box": [440, 600, 470, 630], "input_box": [],
                                   "send_box": [], "chat_box": [], "note": ""}, image.size, origin, 1.0)

    kong_closed_but_wide = [win(5, (0, 0, 1271, 842), "kong.exe", "KONG")]
    desk = FakeDesk(kong_closed_but_wide)
    report = finder.find(conn, "kong", desk=desk, see=closed)
    assert not report["settings"] and desk.clicks == [(455, 615)]     # 한 번만 눌러 보고, 그래도 닫혀 있으면 알림
    assert "닫혀 있습니다" in report["message"]


def test_vision_mapping_and_bad_boxes():
    data = {"chat_open": True, "input_box": [100, 400, 500, 440], "send_box": [100, 400, 500, 440],
            "chat_box": [0, 0, 5, 5], "toggle_box": "x", "login_required": False, "note": ""}
    f = finder.vision_find(data, (700, 500), (1000, 200), 0.5)
    assert f.boxes == {"input": (1200, 1000, 2000, 1080)}          # 줄인 사진(절반) → 화면, 같은 네모인 전송·작은 채팅은 버림
    assert finder.valid_box([10, 10, 900, 20], (700, 500)) == (10, 10, 700, 20)
    assert finder.valid_box([1, 2, 3], (700, 500)) is None


def test_auto_refind_every_five_minutes_saves_app_keys(conn):
    logs = []
    desk = FakeDesk(KONG)
    report = finder.auto(conn, "kong", now=1000.0, desk=desk, log=logs.append)
    assert report["settings"] and config.get(conn, "kong.input_mode") == "screen"
    assert config.get(conn, "kong.send_point") and "자동 다시 찾기" in logs[0]
    assert not desk.fronts and not desk.photos                     # 듣는 중에는 창을 앞으로 가져오거나 찍지 않음
    assert finder.auto(conn, "kong", now=1100.0, desk=desk, log=logs.append) is None
    assert finder.auto(conn, "kong", now=1400.0, desk=desk, log=logs.append) is not None


def test_runner_refinds_chat_and_reloads_sender(conn):
    start_live(conn)
    sender = FakeGorilla(running=False)
    sender.cfg = None
    calls = []

    def refind(c, app):
        calls.append(app)
        sender.running = True        # 다시 찾아 저장하자 그 자리에서 채팅 창이 보임
        return {"settings": {"gorilla.input_point": "1,1"}}

    r, _ = live_runner(conn, {3: "오늘의 퀴즈 철수가 좋아하는 과일 정답은 고릴라로"}, ScriptAnswerer([analysis()]),
                       sender, n=14)
    r.deps.refind = refind
    r.run_live()
    assert calls == ["gorilla"] and sender.sent == ["사과"] and sender.cfg is not None


def test_find_command_and_page(client, data_dir, monkeypatch):
    from radio_helper import app as app_module

    launched = []
    monkeypatch.setattr(app_module, "launch_quizbot", lambda args: launched.append(args) or "quizbot_x.log")
    html = client.get("/gorilla?app=kong").get_data(as_text=True)
    assert "원버튼 찾기" in html and "모든 앱 찾기" in html
    token = csrf(client, "/gorilla?app=kong")
    client.post("/quizbot/tool", data={"csrf_token": token, "tool": "find", "app": "kong"})
    client.post("/quizbot/tool", data={"csrf_token": token, "tool": "find-all", "app": "kong"})
    assert launched == [["find", "--app", "kong"], ["find", "--all", "--app", "kong"]]
    (data_dir / "inspect").mkdir(exist_ok=True)
    (data_dir / "inspect" / "kong_found_20261020_013000.json").write_text(json.dumps(
        {"at": "2026-10-20 01:30:00", "running": True, "process": "kong.exe", "settings": {"x": 1},
         "message": "콩 앱 켜져 있음. 찾음: 입력칸(알려진 배치)", "found": {"how": {"input": "template"}}}), encoding="utf-8")
    html = client.get("/gorilla?app=kong").get_data(as_text=True)
    assert "콩 앱 켜져 있음" in html and "입력칸 알려진 배치" in html


# ── Claude 구독 한도 ────────────────────────────────────────────────
LIMIT = ("You've hit your monthly spend limit · raise it at https://claude.ai/settings/usage?from=cc_cli_limit_message"
         " · your session limit resets 3:30am (Asia/Seoul)")


def test_spend_limit_message_is_a_limit_and_pauses_until_reset():
    assert claude_cli._kind(LIMIT) == "limit"
    now = datetime(2026, 10, 20, 1, 32).timestamp()
    until = claude_cli.pause_for_limit(LIMIT, now=now)
    assert datetime.fromtimestamp(until) == datetime(2026, 10, 20, 3, 31)
    later = datetime(2026, 10, 20, 4, 0).timestamp()
    assert datetime.fromtimestamp(claude_cli.reset_time(LIMIT, later)) == datetime(2026, 10, 21, 3, 30)
    assert claude_cli.reset_time("limit reached", now) is None
    assert claude_cli.pause_for_limit("usage limit reached", now=now) == now + claude_cli.LIMIT_PAUSE


def test_paused_answerer_skips_analysis_without_spending_the_cap(conn, monkeypatch):
    monkeypatch.setattr(claude_cli, "_limited_until", time.time() + 3600)
    ans = answerer.ClaudeAnswerer(billing="subscription", cli=claude_cli)
    assert "분석을 쉽니다" in ans.paused()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert answerer.ClaudeAnswerer(billing="subscription", fallback=True, cli=claude_cli).paused() == ""

    class Paused(ScriptAnswerer):
        def paused(self):
            return "Claude 구독 한도에 걸려 03:31 까지 분석을 쉽니다"

    start_live(conn)
    ans = Paused([analysis()])
    r, _ = live_runner(conn, {2: "오늘의 퀴즈 정답은 고릴라로", 8: "다시 한 번 퀴즈 정답은 고릴라로"}, ans, FakeGorilla(), n=16)
    r.run_live()
    assert ans.calls == [] and r.analyses == 0
    notes = [m for (m,) in conn.execute("SELECT message FROM events") if "분석을 쉽니다" in m]
    assert len(notes) == 1                                          # 같은 안내는 한 번만


# ── 기본값 ────────────────────────────────────────────────────────
def test_old_defaults_move_once_custom_kept(conn):
    db.set_setting(conn, config.FORCED_066, "1")                    # 0.66 강제 설정은 아래 시험에서 따로
    db.set_setting(conn, "quizbot.model", "claude-opus-5-5")
    db.set_setting(conn, "quizbot.effort", "high")                  # 직접 고른 값은 그대로
    db.set_setting(conn, "quizbot.whisper_beam", "5")
    db.set_setting(conn, "quizbot.cooldown_seconds", "60")
    changed = config.migrate_defaults(conn)
    assert len(changed) == 3
    assert (config.get(conn, "quizbot.model"), config.get(conn, "quizbot.effort"),
            config.get(conn, "quizbot.whisper_beam"), config.get(conn, "quizbot.cooldown_seconds")) == \
        ("claude-sonnet-5-5", "high", "auto", "90")
    db.set_setting(conn, "quizbot.model", "claude-opus-5-5")        # 바꾼 뒤 다시 고르면 그대로
    assert config.migrate_defaults(conn) == [] and config.get(conn, "quizbot.model") == "claude-opus-5-5"


def test_066_lifts_story_cap_and_sets_sonnet_low_once(conn):
    # 사용자 요청: 사연 1시간 상한 해제 + 작업 모델 Sonnet 5.5 · low (직접 고른 값이 있어도 한 번 맞춤)
    db.set_setting(conn, "quizbot.max_story_sends_per_hour", "2")
    db.set_setting(conn, "quizbot.model", "claude-opus-5-5")
    db.set_setting(conn, "quizbot.effort", "high")
    changed = config.migrate_defaults(conn)
    assert (config.get(conn, "quizbot.max_story_sends_per_hour"), config.get(conn, "quizbot.model"),
            config.get(conn, "quizbot.effort")) == ("0", "claude-sonnet-5-5", "low")
    assert isinstance(changed, list)
    db.set_setting(conn, "quizbot.effort", "medium")                # 그 뒤에 다시 고르면 그대로
    config.migrate_defaults(conn)
    assert config.get(conn, "quizbot.effort") == "medium"
    assert config.DEFAULTS["quizbot.max_story_sends_per_hour"] == "0"


def test_story_hour_cap_zero_means_unlimited(conn):
    from datetime import datetime, timedelta

    from radio_helper.quizbot import story

    now = datetime(2026, 10, 22, 12, 0)
    for i in range(5):
        at = (now - timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT INTO story_posts (program, broadcast_date, topic, message, source, status, sent_at, "
                     "created_at, updated_at) VALUES ('파워FM', '2026-10-22', '가을', 'x', 'approved', 'posted', ?, ?, ?)",
                     (at, at, at))
    conn.commit()
    post = conn.execute("SELECT * FROM story_posts LIMIT 1").fetchone()
    reasons = story.decide(conn, post, None, now, 0, "app")
    assert not any("1시간" in r for r in reasons)
    assert any("1시간" in r for r in story.decide(conn, post, None, now, 2, "app"))


def test_beam_auto_and_speedup_steps(conn):
    assert config.whisper_beam(conn, "cpu") == 1 and config.whisper_beam(conn, "cuda") == 5
    db.set_setting(conn, "quizbot.whisper_beam", "3")
    assert config.whisper_beam(conn, "cpu") == 3
    assert config.stt_speedup(5, "large-v3-turbo") == ("beam", None)
    assert config.stt_speedup(1, "large-v3-turbo") == ("model", "small")
    assert config.stt_speedup(1, "tiny") == ("none", None)


class SlowWhisper:
    def __init__(self, model="large-v3-turbo", beam=5):
        self.model_size, self.beam_size = model, beam

    def transcribe(self, audio):
        return ""


def test_runner_speeds_up_slow_transcription(conn):
    start_live(conn)
    made = []
    r, _ = live_runner(conn, {}, ScriptAnswerer([]), FakeGorilla(), n=1)
    r.deps.transcriber_factory = lambda model, program: made.append(model) or SlowWhisper(model, 1)
    tr = SlowWhisper()
    assert r.speed_up_stt(tr, "애프터클럽", 23, 10) is tr and tr.beam_size == 1
    assert config.get(conn, "quizbot.whisper_beam") == "1"
    new = r.speed_up_stt(tr, "애프터클럽", 20, 10)
    assert made == ["small"] and new.model_size == "small" and config.get(conn, "quizbot.whisper_model") == "small"
    msgs = " ".join(m for (m,) in conn.execute("SELECT message FROM events"))
    assert "후보 수를 5에서 1로" in msgs and "large-v3-turbo에서 small" in msgs
