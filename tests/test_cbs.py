"""CBS(레인보우) (0.55·0.56): 채널·문자 번호·채팅 앱·게시판 주소·편성표·원버튼 찾기·동시 청취 네 칸.
EBS(반디)는 0.56 에서 뺐다 — 0.55 가 저절로 더한 EBS FM 줄도 한 번 지운다."""

import json

from conftest import csrf

from radio_helper import db, schedules, stations
from radio_helper.quizbot import config, finder, lanes


def win(hwnd, rect, process, title=""):
    return {"hwnd": hwnd, "title": title, "process": process, "path": f"C:/Apps/{process}", "rect": rect, "iconic": False}


def inside(point, box):
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def test_channels_apps_and_numbers(conn):
    chans = config.channels(conn)
    assert chans["CBS 음악FM"] == "#9390" and chans["CBS 표준FM"] == "" and chans["EBS FM"] == ""   # 0.88 EBS 다시
    assert config.chat_app(conn, "CBS 음악FM") == "rainbow" and config.chat_app(conn, "CBS 표준FM") == "rainbow"
    assert config.chat_app(conn, "KBS 쿨FM") == "kong" and config.chat_app(conn, "EBS FM") == "bandi"
    assert config.broadcaster_of("CBS 음악FM") == "CBS" and config.broadcaster_of("EBS FM") == "EBS"
    assert config.app_label("rainbow") == "레인보우" and config.platform(conn, "bandi") == "ldplayer"
    cfg = config.GorillaConfig.load(conn, "rainbow")
    assert cfg.label == "레인보우" and "레인보우" in cfg.default_title and config.get(conn, "rainbow.test_message") == "CBS 화이팅"


def test_saved_channel_list_gets_cbs_once(conn):
    db.set_setting(conn, config.LD_CHANNELS_ADDED, "1")                         # (0.88 LDPlayer 채널은 따로 시험)
    db.set_setting(conn, "channels", "파워FM=#1077\nMBC FM4U=#8000\nCBS 음악FM=#9390\n")
    assert config.migrate_channels(conn) == ["CBS 표준FM="]
    assert db.get_setting(conn, "channels") == "파워FM=#1077\nMBC FM4U=#8000\nCBS 음악FM=#9390\nCBS 표준FM="
    db.set_setting(conn, "channels", "파워FM=#1077")                            # 사용자가 지운 뒤에는 다시 넣지 않음
    assert config.migrate_channels(conn) == [] and db.get_setting(conn, "channels") == "파워FM=#1077"


def test_new_pc_uses_default_list_without_saving(conn):
    assert config.migrate_channels(conn) == [] and db.get_setting(conn, "channels", None) is None
    assert "CBS 음악FM" in config.channels(conn)


def test_ebs_added_by_055_is_removed_once(conn):
    # 0.55 를 쓴 PC: 채널 목록에 EBS FM 을 저절로 더했고, 동시 청취 칸에도 넣어 둠
    db.set_setting(conn, config.CHANNELS_MIGRATED, "1")
    db.set_setting(conn, config.LD_CHANNELS_ADDED, "1")
    db.set_setting(conn, "channels", "파워FM=#1077\nCBS 음악FM=#9390\nCBS 표준FM=\nEBS FM=#1045")
    db.log(conn, "quizbot", "채널 목록에 CBS·EBS 를 더했습니다: CBS 음악FM, CBS 표준FM, EBS FM")
    db.set_setting(conn, "live.lanes", json.dumps([{"channel": "CBS 음악FM", "device": ""}, {"channel": "EBS FM", "device": ""}],
                                                  ensure_ascii=False))
    config.migrate_channels(conn)
    assert db.get_setting(conn, "channels") == "파워FM=#1077\nCBS 음악FM=#9390\nCBS 표준FM="
    assert [x["channel"] for x in json.loads(db.get_setting(conn, "live.lanes"))] == ["CBS 음악FM", ""]
    db.set_setting(conn, "channels", "파워FM=#1077\nEBS FM=#1045")              # 그 뒤 직접 다시 넣으면 그대로 둠
    config.migrate_channels(conn)
    assert "EBS FM=#1045" in db.get_setting(conn, "channels")


def test_own_ebs_channel_is_kept(conn):
    db.set_setting(conn, config.CHANNELS_MIGRATED, "1")
    db.set_setting(conn, "channels", "파워FM=#1077\nEBS FM=#1045")                # 0.55 이전부터 직접 넣어 둔 채널
    assert config.drop_auto_ebs(conn) is False and "EBS FM" in db.get_setting(conn, "channels")


def test_board_addresses_for_cbs():
    assert stations.station_of("https://www.cbs.co.kr/board/view/cbs_P000219_notice?no=1") == "CBS"
    assert stations.station_of("https://www.ebs.co.kr/radio/home") is None
    assert stations.station_of("http://www.cbs.co.kr/board/write") is None             # https 만
    assert stations.station_of("https://www.notcbs.co.kr/board") is None                # 비슷한 다른 주소는 안 됨
    assert stations.station_of("https://cbs.co.kr.evil.com/board") is None


def test_schedule_search_covers_cbs(conn):
    assert "93.9MHz" in schedules.describe("CBS 음악FM") and "98.1MHz" in schedules.describe("CBS 표준FM")
    assert {"CBS 음악FM", "CBS 표준FM"} <= set(schedules.channels_to_fetch(conn))


# ── 레인보우 화면 (사용자가 보낸 사진, 창 409×754) ───────────────────────
RAINBOW = [win(1, (1000, 100, 1409, 854), "CBSRainbow.exe", "CBS 레인보우")]
# 사진에서 잰 자리 (창 기준): '채팅을 입력하세요' 칸 x 68~326 · y 492~536, 파란 전송 단추 363~408 · 489~537,
# 채팅 목록(공지 아래 글들) y 172~486, '일반 ∨' 고르기 x 8~53, 웃는 얼굴 단추 x 330~354
INPUT_TEXT, SEND_BTN, CHAT_ROWS, CATEGORY, EMOJI = (68, 492, 326, 536), (363, 489, 408, 537), (5, 172, 404, 486), \
    (8, 495, 53, 535), (330, 495, 354, 535)


def to_window(box):
    return tuple(int(v) for v in (box[0] - 1000, box[1] - 100, box[2] - 1000, box[3] - 100))


def test_rainbow_layout_hits_the_real_controls():
    found = finder.template_find("rainbow", RAINBOW)
    assert found.chat_open and found.toggle is None
    inp, send, chat = (to_window(found.boxes[t]) for t in ("input", "send", "chat"))
    assert inside(finder.center(inp), INPUT_TEXT) and inside(finder.center(send), SEND_BTN)
    assert not inside(finder.center(inp), CATEGORY) and inp[0] > CATEGORY[2] and inp[2] < EMOJI[0]   # '일반'·이모티콘 안 누름
    assert chat[1] <= CHAT_ROWS[1] and chat[3] <= INPUT_TEXT[1] and chat[3] >= CHAT_ROWS[3] - 5
    assert all(found.how[t] == "template" for t in ("input", "send", "chat"))


def test_rainbow_layout_scales_with_window():
    big = [win(1, (0, 0, 818, 1508), "CBSRainbow.exe", "CBS 레인보우")]           # 화면 배율 200%
    found = finder.template_find("rainbow", big)
    cx, cy = finder.center(found.boxes["send"])
    assert inside((cx / 2, cy / 2), SEND_BTN)


def test_one_button_find_knows_rainbow():
    others = [win(9, (0, 0, 1900, 1000), "chrome.exe", "CBS 레인보우 - 웹 듣기"),
              win(10, (0, 0, 800, 600), "premiere.exe", "Adobe Premiere Pro")]
    assert finder.pick_app("rainbow", others + RAINBOW)[0] == "cbsrainbow.exe"
    assert finder.pick_app("rainbow", others) == ("", [])                       # 브라우저 창은 아님
    assert "bandi" not in finder.APP_HINTS


def test_four_lanes_and_smaller_model_on_gpu(conn, monkeypatch):
    monkeypatch.setattr(lanes, "app_audio_supported", lambda: True)
    db.set_setting(conn, "live.channel", "파워FM")
    db.set_setting(conn, "live.multi", "1")
    extra = [{"channel": c, "device": ""} for c in ("MBC FM4U", "KBS 쿨FM", "CBS 음악FM")]
    db.set_setting(conn, "live.lanes", json.dumps(extra, ensure_ascii=False))
    all_ = lanes.all_lanes(conn)
    assert [x["lane"] for x in all_] == ["", "2", "3", "4"] and lanes.problems(conn, all_) == []
    assert lanes.stt_model(conn, "auto", "cuda") == "large-v3-turbo"            # 네 칸 × large-v3 는 16GB 에 빠듯함
    assert lanes.stt_model(conn, "auto", "cpu") == "auto" and lanes.stt_model(conn, "large-v3", "cuda") == "large-v3"
    db.set_setting(conn, "live.lanes", json.dumps(extra[:2], ensure_ascii=False))
    assert lanes.stt_model(conn, "auto", "cuda") == "auto"                      # 세 칸까지는 그대로 (large-v3)
    launched = []
    db.set_setting(conn, "live.lanes", json.dumps(extra, ensure_ascii=False))
    db.set_setting(conn, "live.active", "1")
    assert lanes.ensure(conn, lambda a: launched.append(a) or "x.log", now=1000.0) == ["2", "3", "4"]


def test_home_offers_cbs_and_ten_lanes(client, conn, monkeypatch):
    from radio_helper import app as app_module

    monkeypatch.setattr(app_module, "launch_quizbot", lambda args: "quizbot_x.log")
    monkeypatch.setattr(lanes, "app_audio_supported", lambda: True)       # 윈도우 11: 칸을 비우면 앱 소리
    html = client.get("/").get_data(as_text=True)
    assert "CBS 음악FM · 레인보우" in html and 'name="lane10_channel"' in html and 'name="lane11_channel"' not in html
    assert "MBC mini · CBS 레인보우)" in html and "EBS FM · EBS 반디" in html                # 0.88: 10곳까지 · LDPlayer 방송국
    r = client.post("/listen/lanes", data={"csrf_token": csrf(client), "multi": "1", "device": "",
                                           "lane2_channel": "CBS 음악FM", "lane2_device": "",
                                           "lane3_channel": "MBC FM4U", "lane3_device": "",
                                           "lane4_channel": "", "lane4_device": ""}, follow_redirects=True)
    assert config.get(conn, "live.multi") == "1" and "저장했습니다" in r.get_data(as_text=True)
    assert [x["channel"] for x in lanes.extras(conn)] == ["CBS 음악FM", "MBC FM4U"]
    page = client.get("/gorilla?app=rainbow").get_data(as_text=True)
    assert "레인보우" in page and "모든 앱 찾기" in page and "반디" not in page


def test_programs_page_reads_old_and_new_schedule_logs(client, conn):
    db.log(conn, "programs", "MBC·KBS 편성표 (웹 검색): MBC FM4U: 프로그램 20개")
    assert "MBC FM4U: 프로그램 20개" in client.get("/programs").get_data(as_text=True)
    db.log(conn, "programs", f"{schedules.LOG_PREFIX} (웹 검색): CBS 음악FM: 프로그램 18개")
    page = client.get("/programs").get_data(as_text=True)
    assert "CBS 음악FM: 프로그램 18개" in page and "MBC·KBS·CBS 편성표 가져오기" in page
