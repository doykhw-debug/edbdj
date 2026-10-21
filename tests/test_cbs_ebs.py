"""CBS(레인보우)·EBS(반디) 추가 (0.55): 채널·문자 번호·채팅 앱·게시판 주소·편성표·원버튼 찾기·동시 청취 다섯 칸."""

import json

from conftest import csrf

from radio_helper import db, schedules, stations
from radio_helper.quizbot import config, finder, lanes


def win(hwnd, rect, process, title=""):
    return {"hwnd": hwnd, "title": title, "process": process, "path": f"C:/Apps/{process}", "rect": rect, "iconic": False}


def test_channels_apps_and_numbers(conn):
    chans = config.channels(conn)
    assert chans["CBS 음악FM"] == "#9390" and chans["EBS FM"] == "#1045" and chans["CBS 표준FM"] == ""
    assert config.chat_app(conn, "CBS 음악FM") == "rainbow" and config.chat_app(conn, "CBS 표준FM") == "rainbow"
    assert config.chat_app(conn, "EBS FM") == "bandi" and config.chat_app(conn, "KBS 쿨FM") == "kong"
    assert config.broadcaster_of("CBS 음악FM") == "CBS" and config.broadcaster_of("EBS FM") == "EBS"
    assert config.app_label("rainbow") == "레인보우" and config.app_label("bandi") == "반디"
    cfg = config.GorillaConfig.load(conn, "bandi")
    assert cfg.label == "반디" and "반디" in cfg.default_title and config.get(conn, "bandi.test_message") == "EBS 화이팅"
    db.set_setting(conn, "channels", "파워FM=#1077\nEBS 잉글리시==반디")
    assert config.chat_app(conn, "EBS 잉글리시") == "bandi"                     # '채널=번호=앱' 으로 직접 정함


def test_saved_channel_list_gets_cbs_ebs_once(conn):
    db.set_setting(conn, "channels", "파워FM=#1077\nMBC FM4U=#8000\nEBS FM=#1045\n")
    assert config.migrate_channels(conn) == ["CBS 음악FM=#9390", "CBS 표준FM="]
    assert db.get_setting(conn, "channels") == "파워FM=#1077\nMBC FM4U=#8000\nEBS FM=#1045\nCBS 음악FM=#9390\nCBS 표준FM="
    db.set_setting(conn, "channels", "파워FM=#1077")                            # 사용자가 지운 뒤에는 다시 넣지 않음
    assert config.migrate_channels(conn) == [] and db.get_setting(conn, "channels") == "파워FM=#1077"


def test_new_pc_uses_default_list_without_saving(conn):
    assert config.migrate_channels(conn) == [] and db.get_setting(conn, "channels", None) is None
    assert "CBS 음악FM" in config.channels(conn)


def test_board_addresses_for_cbs_ebs():
    assert stations.station_of("https://www.cbs.co.kr/board/view/cbs_P000219_notice?no=1") == "CBS"
    assert stations.station_of("https://www.ebs.co.kr/radio/home") == "EBS"
    assert stations.station_of("http://www.cbs.co.kr/board/write") is None             # https 만
    assert stations.station_of("https://www.notcbs.co.kr/board") is None                # 비슷한 다른 주소는 안 됨
    assert stations.station_of("https://cbs.co.kr.evil.com/board") is None


def test_schedule_search_covers_cbs_ebs(conn):
    assert "93.9MHz" in schedules.describe("CBS 음악FM") and "104.5MHz" in schedules.describe("EBS FM")
    assert {"CBS 음악FM", "CBS 표준FM", "EBS FM"} <= set(schedules.channels_to_fetch(conn))


def test_one_button_find_knows_rainbow_and_bandi():
    others = [win(9, (0, 0, 1900, 1000), "chrome.exe", "EBS 반디 - 웹 듣기"),
              win(10, (0, 0, 800, 600), "websitebuilder.exe", "Websites")]
    rainbow = [win(1, (0, 0, 900, 700), "CBSRainbow.exe", "CBS 레인보우")]
    bandi = [win(2, (0, 0, 500, 800), "EBSBandi.exe", "EBS 반디")]
    assert finder.pick_app("rainbow", others + rainbow + bandi)[0] == "cbsrainbow.exe"
    assert finder.pick_app("bandi", others + rainbow + bandi)[0] == "ebsbandi.exe"
    assert finder.pick_app("bandi", others) == ("", [])                         # 브라우저·'Websites' 창은 아님
    found = finder.template_find("rainbow", rainbow)                             # 배치를 모르는 앱은 화면 요소·Claude 로만
    assert found.boxes == {} and found.toggle is None


def test_five_lanes_and_smaller_model_on_gpu(conn, monkeypatch):
    monkeypatch.setattr(lanes, "app_audio_supported", lambda: True)
    db.set_setting(conn, "live.channel", "파워FM")
    db.set_setting(conn, "live.multi", "1")
    extra = [{"channel": c, "device": ""} for c in ("MBC FM4U", "KBS 쿨FM", "CBS 음악FM", "EBS FM")]
    db.set_setting(conn, "live.lanes", json.dumps(extra, ensure_ascii=False))
    all_ = lanes.all_lanes(conn)
    assert [x["lane"] for x in all_] == ["", "2", "3", "4", "5"] and lanes.problems(conn, all_) == []
    assert lanes.stt_model(conn, "auto", "cuda") == "large-v3-turbo"            # 다섯 칸 × large-v3 는 16GB 를 넘음
    assert lanes.stt_model(conn, "auto", "cpu") == "auto" and lanes.stt_model(conn, "large-v3", "cuda") == "large-v3"
    db.set_setting(conn, "live.lanes", json.dumps(extra[:2], ensure_ascii=False))
    assert lanes.stt_model(conn, "auto", "cuda") == "auto"                      # 세 칸까지는 그대로 (large-v3)
    launched = []
    db.set_setting(conn, "live.lanes", json.dumps(extra, ensure_ascii=False))
    db.set_setting(conn, "live.active", "1")
    assert lanes.ensure(conn, lambda a: launched.append(a) or "x.log", now=1000.0) == ["2", "3", "4", "5"]


def test_home_offers_cbs_ebs_and_five_lanes(client, conn, monkeypatch):
    from radio_helper import app as app_module

    monkeypatch.setattr(app_module, "launch_quizbot", lambda args: "quizbot_x.log")
    monkeypatch.setattr(lanes, "app_audio_supported", lambda: True)       # 윈도우 11: 칸을 비우면 앱 소리
    html = client.get("/").get_data(as_text=True)
    assert "CBS 음악FM · 레인보우" in html and "EBS FM · 반디" in html and 'name="lane5_channel"' in html
    assert "CBS 레인보우 · EBS 반디" in html
    r = client.post("/listen/lanes", data={"csrf_token": csrf(client), "multi": "1", "device": "",
                                           "lane2_channel": "CBS 음악FM", "lane2_device": "",
                                           "lane3_channel": "EBS FM", "lane3_device": "CABLE Input",
                                           "lane4_channel": "", "lane4_device": "", "lane5_channel": "", "lane5_device": ""},
                    follow_redirects=True)
    assert config.get(conn, "live.multi") == "1" and "저장했습니다" in r.get_data(as_text=True)
    assert [x["channel"] for x in lanes.extras(conn)] == ["CBS 음악FM", "EBS FM"]
    page = client.get("/gorilla?app=rainbow").get_data(as_text=True)
    assert "레인보우" in page and "모든 앱 찾기" in page


def test_programs_page_reads_old_and_new_schedule_logs(client, conn):
    db.log(conn, "programs", "MBC·KBS 편성표 (웹 검색): MBC FM4U: 프로그램 20개")
    assert "MBC FM4U: 프로그램 20개" in client.get("/programs").get_data(as_text=True)
    db.log(conn, "programs", f"{schedules.LOG_PREFIX} (웹 검색): CBS 음악FM: 프로그램 18개")
    page = client.get("/programs").get_data(as_text=True)
    assert "CBS 음악FM: 프로그램 18개" in page and "MBC·KBS·CBS·EBS 편성표 가져오기" in page
