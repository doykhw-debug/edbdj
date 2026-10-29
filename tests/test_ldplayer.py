"""0.88: LDPlayer(안드로이드 앱 플레이어)로 켠 방송국 앱 — 창 번호로 터치·글자 넣기·화면 찍기, 동시 청취 10곳."""

import io
import json
import subprocess
from types import SimpleNamespace

import pytest
from conftest import csrf

from radio_helper import db
from radio_helper.quizbot import appaudio, config, lanes, ldplayer

LIST2 = "0,반디창,1001,1002,1,4100,4200,900,1600,320\n1,YTN창,2001,2002,0,0,0,900,1600,320\n"


def png(filled: bool, size=(900, 1600)) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, (255, 255, 255))
    if filled:
        ImageDraw.Draw(img).rectangle((150, 1480, 600, 1510), fill=(20, 20, 20))     # 입력칸에 들어간 글
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


class FakeConsole:
    """LDPlayer 창 흉내: 글 넣기·전송 터치·엔터가 듣는지 정하고, 한 일을 기록한다."""

    def __init__(self, started=True, types=True, send_works=True, enter_works=True, shots=True):
        self.started, self.types, self.send_works, self.enter_works, self.shots = started, types, send_works, enter_works, shots
        self.text, self.log, self.sent = "", [], []

    def instances(self):
        return [{"index": 0, "title": "반디창", "started": self.started, "pid": 4100, "vbox_pid": 4200}]

    def instance(self, index):
        return next((i for i in self.instances() if i["index"] == index), None)

    def tap(self, index, point):
        self.log.append(("tap", tuple(point)))
        if tuple(point) == (870, 1495) and self.send_works and self.text:
            self.sent.append(self.text)
            self.text = ""

    def clear(self, index):
        self.log.append(("clear",))
        self.text = ""

    def type_text(self, index, text):
        self.log.append(("type", text))
        if self.types:
            self.text = text

    def keys(self, index, *codes):
        self.log.append(("keys", codes))
        if codes == (ldplayer.KEY_ENTER,) and self.enter_works and self.text:
            self.sent.append(self.text)
            self.text = ""

    def screenshot(self, index):
        return png(bool(self.text)) if self.shots else None


def sender(con, **over):
    cfg = ldplayer.LdConfig(app="bandi", index=0, input=(400, 1495), send=(870, 1495), **over)
    return ldplayer.LdSender(cfg, make_console=lambda: con, sleep=lambda s: None)


def test_send_types_and_touches_send(conn):
    con = FakeConsole()
    r = sender(con).send("정답은 사과")
    assert r.status == "entered" and con.sent == ["정답은 사과"] and "입력칸이 비워져" in r.detail
    kinds = [x[0] for x in con.log]
    assert kinds[:3] == ["tap", "clear", "type"] and ("tap", (870, 1495)) in con.log
    assert r.shots and r.shots[0][0].startswith("보낸 뒤")                      # 증거 사진


def test_text_not_going_in_never_touches_send(conn):
    con = FakeConsole(types=False)
    r = sender(con).send("정답")
    assert r.status == "failed" and "전송을 누르지 않았습니다" in r.detail and con.sent == []
    assert ("tap", (870, 1495)) not in con.log and [x[0] for x in con.log].count("type") == ldplayer.TYPE_TRIES


def test_send_touch_ignored_then_enter(conn):
    con = FakeConsole(send_works=False)
    r = sender(con).send("정답")
    assert r.status == "entered" and con.sent == ["정답"] and "엔터로 다시 보냄" in r.detail


def test_touch_and_enter_both_fail_is_retried_later(conn):
    con = FakeConsole(send_works=False, enter_works=False)
    r = sender(con).send("정답")
    assert r.status == "failed" and "엔터 모두 안 돼" in r.detail


def test_player_off_or_unset(conn):
    assert sender(FakeConsole(started=False)).send("정답").status == "failed"
    assert not sender(FakeConsole(started=False)).is_running() and sender(FakeConsole()).is_running()
    s = ldplayer.LdSender(ldplayer.LdConfig(app="bandi", index=0), make_console=FakeConsole, sleep=lambda s: None)
    assert "자리를 정하지 않았습니다" in s.send("정답").detail
    assert sender(FakeConsole()).send("  ").status == "failed"                   # 빈 글은 넣지 않음


def test_no_screenshot_still_sends_unverified(conn):
    con = FakeConsole(shots=False)
    r = sender(con).send("정답")
    assert r.status == "entered" and con.sent == ["정답"] and "확인하지 못함" in r.detail


def test_chat_image_crops_saved_area(conn, data_dir):
    s = sender(FakeConsole(), chat=(0, 100, 900, 1400))
    from PIL import Image

    img = Image.open(io.BytesIO(s.chat_image(max_side=2000)))
    assert img.size == (900, 1300)


# ── ldconsole 명령 ──────────────────────────────────────────────────
def test_parse_list2_and_find_console():
    rows = ldplayer.parse_list2(LIST2 + "잘못된 줄\n")
    assert [r["index"] for r in rows] == [0, 1] and rows[0]["started"] and not rows[1]["started"]
    assert rows[0]["pid"] == 4100 and rows[0]["vbox_pid"] == 4200 and rows[0]["size"] == (900, 1600)
    assert ldplayer.find_console("", exists=lambda p: p == ldplayer.CONSOLE_PATHS[1]) == ldplayer.CONSOLE_PATHS[1]
    assert ldplayer.find_console(r"E:\LD\ldconsole.exe", exists=lambda p: True) == r"E:\LD\ldconsole.exe"
    assert ldplayer.find_console("", exists=lambda p: False) is None


def test_console_commands(tmp_path):
    calls = []

    def run(args, **kw):
        calls.append(args[1:])
        out = LIST2.encode("cp949") if args[1] == "list2" else b"package:kr.co.ebs.bandi\npackage:com.ytn.radio\n"
        if args[1] == "adb" and args[-1].startswith("pull"):
            (tmp_path / "ld0_screen.png").write_bytes(png(False))
        return SimpleNamespace(stdout=out, returncode=0)

    con = ldplayer.Console("ldconsole.exe", run=run, shot_dir=tmp_path)
    assert con.instance(0)["title"] == "반디창"                                     # cp949 로 나온 한글도 읽음
    con.tap(0, (10, 20))
    con.type_text(0, "정답 사과")
    con.keys(0, 66)
    assert ["adb", "--index", "0", "--command", "shell input tap 10 20"] in calls
    assert ["action", "--index", "0", "--key", "call.input", "--value", "정답 사과"] in calls
    assert con.packages(0) == ["com.ytn.radio", "kr.co.ebs.bandi"]
    assert con.screenshot(0)[:4] == b"\x89PNG"

    def slow(args, **kw):
        raise subprocess.TimeoutExpired(args, 1)
    with pytest.raises(ldplayer.LdError):
        ldplayer.Console("x", run=slow).instances()


def test_audio_from_that_player_only():
    con = FakeConsole()
    procs = {4100: (1, "dnplayer.exe"), 4200: (4100, "ld9boxheadless.exe"), 5000: (4200, "audio.exe"),
             7000: (1, "chrome.exe")}
    sessions = [{"pid": 7000, "peak": 0.9}, {"pid": 5000, "peak": 0.3}]
    assert ldplayer.audio_process(con, 0, sessions, procs) == (5000, "audio.exe")   # 그 창 아래의 소리 길
    assert ldplayer.audio_process(con, 0, [], procs) == (4100, "dnplayer.exe")      # 아직 소리 전: 플레이어 프로그램
    with pytest.raises(RuntimeError):
        ldplayer.audio_process(FakeConsole(started=False), 0, [], procs)


def test_app_audio_uses_ldplayer_for_ld_apps(conn):
    db.set_setting(conn, "bandi.ld_index", "0")
    pid = appaudio.ld_process(conn, "bandi", sessions=[], procs={4100: (1, "dnplayer.exe")}, con=FakeConsole())
    assert pid == (4100, "dnplayer.exe")


# ── 방송국·채널·동시 청취 10곳 ─────────────────────────────────────────
def test_new_stations_and_apps(conn):
    for channel, app, station in [("EBS FM", "bandi", "EBS"), ("YTN 라디오", "ytn", "YTN"), ("KFN 국방FM", "kfn", "KFN"),
                                  ("CPBC 서울", "cpbc", "CPBC"), ("FEBC 서울", "febc", "FEBC"),
                                  ("경인방송 iFM", "ifm", "iFM")]:
        assert config.chat_app(conn, channel) == app and config.broadcaster_of(channel) == station
        assert config.platform(conn, app) == "ldplayer" and config.chat_only(conn, channel)
    assert sorted(config.LD_APPS) == ["bandi", "cpbc", "febc", "ifm", "kfn", "ytn"]          # 0.89 사용자 정리: 6곳만
    for gone in ("tbs", "gugak", "btn", "arirang"):
        assert gone not in config.CHAT_APPS
    assert config.chat_app(conn, "국악방송") is None and not config.chat_only(conn, "파워FM")
    assert config.platform(conn, "gorilla") == "pc"
    assert isinstance(ldplayer.sender_for(conn, "bandi"), ldplayer.LdSender)
    db.set_setting(conn, "kong.platform", "ldplayer")                                    # PC 앱도 LDPlayer 로 바꿀 수 있음
    assert isinstance(ldplayer.sender_for(conn, "kong"), ldplayer.LdSender)


def test_saved_channel_list_gets_ld_stations_once(conn):
    db.set_setting(conn, "channels", "파워FM=#1077\nEBS FM=#1045")
    added = config.add_ld_channels(conn)
    assert "EBS FM=" not in added and "YTN 라디오=" in added and len(added) == len(config.LD_CHANNELS) - 1
    db.set_setting(conn, "channels", "파워FM=#1077")
    assert config.add_ld_channels(conn) == [] and db.get_setting(conn, "channels") == "파워FM=#1077"   # 지운 뒤엔 그대로


def test_ten_lanes_and_gpu_memory(conn, monkeypatch):
    monkeypatch.setattr(lanes, "app_audio_supported", lambda: True)          # 윈도우: 칸마다 그 앱 소리만
    assert lanes.MAX_LANES == 10 and len(lanes.EXTRA_LANES) == 9
    names = ["MBC FM4U", "KBS 쿨FM", "CBS 음악FM", "EBS FM", "YTN 라디오", "KFN 국방FM", "CPBC 서울", "FEBC 서울", "경인방송 iFM"]
    db.set_setting(conn, "live.multi", "1")
    db.set_setting(conn, "live.lanes", json.dumps([{"channel": n, "device": ""} for n in names], ensure_ascii=False))
    assert len(lanes.all_lanes(conn)) == 10 and lanes.problems(conn, lanes.all_lanes(conn)) == []
    assert lanes.stt_compute(conn, "cuda") == "int8_float16" and lanes.stt_model(conn, "auto", "cuda") == "large-v3-turbo"
    db.set_setting(conn, "live.lanes", json.dumps([{"channel": "MBC FM4U", "device": ""}], ensure_ascii=False))
    assert lanes.stt_compute(conn, "cuda") == "float16" and lanes.stt_compute(conn, "cpu") == "int8"


# ── 화면 ────────────────────────────────────────────────────────────
def test_ldplayer_page_and_calibration(client, conn, data_dir, monkeypatch):
    con = FakeConsole()
    monkeypatch.setattr(ldplayer, "console", lambda c=None, run=None: con)
    html = client.get("/ldplayer").get_data(as_text=True)
    assert "EBS 반디" in html and "KFN 별사탕" in html and "Arirang" not in html and "TBS" not in html and "반디창" in html
    token = csrf(client, "/ldplayer")
    client.post("/ldplayer/save", data={"csrf_token": token, "console": "", "bandi_platform": "ldplayer",
                                        "bandi_index": "0", "bandi_package": "kr.co.ebs.bandi"})
    assert config.get(conn, "bandi.ld_index") == "0" and config.get(conn, "bandi.ld_package") == "kr.co.ebs.bandi"
    client.post("/ldplayer/bandi/shot", data={"csrf_token": token})
    assert (data_dir / "inspect" / "ld_bandi.png").exists()
    page = client.get("/ldplayer?app=bandi").get_data(as_text=True)
    assert "ld_bandi.png" in page and "① 입력칸" in page
    client.post("/ldplayer/bandi/point", data={"csrf_token": token, "target": "input", "value": "400,1495"})
    client.post("/ldplayer/bandi/point", data={"csrf_token": token, "target": "chat", "value": "800,1400,10,100"})
    assert config.get(conn, "bandi.ld_input") == "400,1495" and config.get(conn, "bandi.ld_chat") == "10,100,800,1400"
    assert client.post("/ldplayer/bandi/point", data={"csrf_token": token, "target": "send", "value": "x"}).status_code == 400
    assert client.get("/gorilla?app=bandi").status_code == 302                           # LDPlayer 앱은 그 화면으로
    assert client.get("/inspect-image/ld_bandi.png").status_code == 200


# ── 0.89: 6곳만 · 채팅만 (게시판 찾기·쓰기에서 뺌) ─────────────────────────────────
def test_extra_stations_from_088_are_dropped_once(conn):
    db.set_setting(conn, "channels", "파워FM=#1077\nEBS FM=\n국악방송=\nBTN 울림=\n아리랑라디오=")
    db.set_setting(conn, "live.lanes", json.dumps([{"channel": "국악방송", "device": ""}, {"channel": "EBS FM", "device": ""}],
                                                  ensure_ascii=False))
    assert config.drop_ld_extras(conn) == ["국악방송=", "BTN 울림=", "아리랑라디오="]
    assert db.get_setting(conn, "channels") == "파워FM=#1077\nEBS FM="
    assert [x["channel"] for x in json.loads(db.get_setting(conn, "live.lanes"))] == ["", "EBS FM"]
    db.set_setting(conn, "channels", "국악방송=")                                      # 그 뒤 직접 넣으면 그대로
    assert config.drop_ld_extras(conn) == [] and db.get_setting(conn, "channels") == "국악방송="


def test_chat_only_stations_skip_boards(conn, monkeypatch):
    from datetime import datetime

    from radio_helper import boards, collect, stations
    from radio_helper.quizbot import live

    db.set_setting(conn, "live.channel", "YTN 라디오")
    db.set_setting(conn, "live.multi", "1")
    db.set_setting(conn, "live.lanes", json.dumps([{"channel": "MBC FM4U", "device": ""}], ensure_ascii=False))
    monkeypatch.setattr(live, "current_program", lambda c, channel, now: f"{channel} 지금 프로그램")
    monkeypatch.setattr(stations, "station_of", lambda url: "X")
    for n, (ch, url) in enumerate([("YTN 라디오", "https://radio.ytn.co.kr/p1"), ("MBC FM4U", "https://www.imbc.com/p2")]):
        conn.execute("INSERT INTO programs (channel, code, title, main_url, on_air, updated_at) VALUES (?, ?, ?, ?, 1, ?)",
                     (ch, f"c{n}", f"{ch} 프로", url, db.now()))
    conn.commit()
    assert [x["channel"] for x in boards.onair_now(conn, datetime.now())] == ["MBC FM4U"]   # 게시판 쓰기: 채팅만 방송국 뺌
    found = [p["channel"] for p in collect.start_pages(conn)]
    assert "MBC FM4U" in found and "YTN 라디오" not in found                                 # 게시판 찾기에서도 뺌
