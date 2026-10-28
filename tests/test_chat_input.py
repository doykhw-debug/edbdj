"""0.74: MBC mini·CBS 레인보우 채팅 입력이 제대로 안 되던 문제 — 붙여넣은 뒤 기다렸다가 입력칸 글자를 읽어 확인,
안 들어갔으면 한 글자씩 직접 입력, 그래도 안 되면 전송을 누르지 않음, 잘리면 글자 수를 기억."""

import sys
from types import ModuleType, SimpleNamespace

import pytest
import test_sound_stop
from test_sound_stop import Win, coords_gorilla

from radio_helper import db
from radio_helper.quizbot import config, gorilla

desk = test_sound_stop.desk          # 가짜 윈도우 화면 고정물을 같이 씀


@pytest.fixture
def typed(monkeypatch, desk):
    """입력칸 흉내: 붙여넣기·직접 입력이 들어가는지 정하고, 지금 칸 글자를 돌려준다."""
    box = SimpleNamespace(value="", paste_works=True, type_works=True, keep=None, typed=[])

    def keys(k, pause=None):
        desk.keys.append(k)
        if k.endswith("^v") and box.paste_works:                 # 한 번에(^a{BACKSPACE}^v) 또는 따로(^v)
            box.value = box.clip if box.keep is None else box.clip[:box.keep]
        elif k.startswith("^a{BACKSPACE}"):
            box.value = ""

    sys.modules["pywinauto.keyboard"].send_keys = keys
    monkeypatch.setattr(gorilla.Gorilla, "_clipboard", staticmethod(lambda text=None: setattr(box, "clip", text) or text))

    def type_unicode(text, **kw):
        box.typed.append(text)
        if box.type_works:
            box.value = text if box.keep is None else text[:box.keep]
    monkeypatch.setattr(gorilla, "type_unicode", type_unicode)
    monkeypatch.setattr(gorilla, "focused_value", lambda: (box.value, "Edit '채팅을 입력하세요'"))
    return box


def rainbow():
    return coords_gorilla(app="rainbow", process_name="flutter_rainbow.exe")


def test_paste_checked_then_sent(typed, desk):
    g = rainbow()
    g._put_text(Win(), "정답은 사과")
    assert typed.value == "정답은 사과" and typed.typed == [] and g.fixes == []


def test_paste_ignored_falls_back_to_typing(typed, desk):
    typed.paste_works = False                         # 실제 사례: 레인보우 빈 칸 전송 → '채팅을 입력하세요'
    g = rainbow()
    g._put_text(Win(), "정답은 사과")
    assert typed.typed == ["정답은 사과"] and typed.value == "정답은 사과"
    assert "한 글자씩" in g.fixes[0]


def test_nothing_goes_in_then_send_is_not_pressed(typed, desk, monkeypatch):
    typed.paste_works = typed.type_works = False
    g = rainbow()
    monkeypatch.setattr(g, "_with_focus", lambda fn: fn(Win()))
    monkeypatch.setattr(g, "evidence_image", lambda w: None)
    result = g.send("정답은 사과")
    assert result.status == "failed" and "등록을 누르지 않았습니다" in result.detail
    assert (1576, 902) not in desk.clicks             # 입력칸만 눌렀고 전송 버튼은 누르지 않음
    assert {c[:2] for c in desk.clicks} == {(1320, 902)}


def test_cut_text_remembers_app_limit(typed, desk, conn):
    typed.keep = 30
    g = coords_gorilla(app="mini", process_name="mbc mini.exe")
    with pytest.raises(gorilla.GorillaError, match="30자까지만"):
        g._put_text(Win(), "가" * 80)
    assert db.get_setting(conn, "mini.input_limit") == "30"
    assert config.chat_limit(conn, "MBC FM4U") == 30 and config.chat_limit(conn, "SBS 파워FM") == 200


def test_unreadable_input_keeps_old_behaviour(desk, monkeypatch):
    monkeypatch.setattr(gorilla, "focused_value", lambda: (None, ""))
    g = rainbow()
    g._put_text(Win(), "정답")
    assert desk.keys == ["{ESC}", "^a{BACKSPACE}", "^v"]      # 0.81: 레인보우도 지우기 → 복사 → 붙여넣기 따로


def test_flat_text_and_unicode_typing():
    assert gorilla.flat_text("첫 줄\n\n 둘째 줄 \n") == "첫 줄 둘째 줄"
    sent = []
    gorilla.type_unicode("가A😀", send=lambda unit, up: sent.append((unit, up)), sleep=lambda s: None)
    units = [u for u, up in sent if not up]
    assert units == [0xAC00, 0x41, 0xD83D, 0xDE00] and len(sent) == 8          # 이모지는 서로게이트 두 개


def test_clipboard_busy_is_retried(monkeypatch):
    calls = {"open": 0}
    clip = ModuleType("win32clipboard")

    def open_():
        calls["open"] += 1
        if calls["open"] < 3:
            raise OSError("busy")
    clip.OpenClipboard = open_
    clip.EmptyClipboard = clip.CloseClipboard = lambda: None
    clip.SetClipboardText = lambda text, fmt: None
    con = ModuleType("win32con")
    con.CF_UNICODETEXT = 13
    monkeypatch.setitem(sys.modules, "win32clipboard", clip)
    monkeypatch.setitem(sys.modules, "win32con", con)
    monkeypatch.setattr(gorilla.time, "sleep", lambda s: None)
    assert gorilla.Gorilla._clipboard("글") == "글" and calls["open"] == 3


# ── 0.77: MBC mini 'mini 메세지를 작성해주세요.' 확인 창 — 닫고 자리 다시 찾기, 더 천천히 ────────────────
def mini_window(dialog=True, size=(640, 950)):
    """mini 창 흉내 그림: 어두운 바탕, 아래 오른쪽 보라 '등록', 얇은 보라 진행 막대, (있으면) 가운데 흰 확인 창 + 보라 '확인'."""
    from PIL import Image, ImageDraw

    w, h = size
    img = Image.new("RGB", size, (20, 28, 48))
    d = ImageDraw.Draw(img)
    d.rectangle((w - 80, h - 60, w - 1, h - 1), fill=(123, 69, 207))          # 등록 (오른쪽 아래)
    d.rectangle((40, int(h * 0.7), 300, int(h * 0.7) + 5), fill=(123, 69, 207))  # 진행 막대 (얇음)
    if dialog:
        d.rectangle((180, 380, 500, 560), fill=(255, 255, 255))
        d.rectangle((300, 480, 380, 525), fill=(123, 69, 207))                 # 확인
    return img


def near(a, b, px=3):
    return len(a) == 2 and abs(a[0] - b[0]) <= px and abs(a[1] - b[1]) <= px


def test_find_dialog_button_only_for_the_confirm_box():
    frame = (1000, 0, 1640, 950)
    x, y = gorilla.find_dialog_button(mini_window(), frame)
    assert abs(x - 1340) <= 3 and abs(y - 502) <= 3                           # '확인' 단추 가운데
    assert gorilla.find_dialog_button(mini_window(dialog=False), frame) is None    # 등록·진행 막대는 아님


def test_mini_dialog_closed_before_typing_and_points_refound(desk, monkeypatch):
    monkeypatch.setattr(gorilla, "PASTE_CHECK_SECONDS", 0)
    monkeypatch.setattr(gorilla, "CLICK_GAP_SECONDS", 0)
    shots = iter([mini_window(), mini_window(dialog=False)])
    monkeypatch.setattr(gorilla, "window_image", lambda handle: next(shots, mini_window(dialog=False)))
    monkeypatch.setattr(gorilla, "focused_value", lambda: (None, ""))
    g = coords_gorilla(app="mini", process_name="mbc mini.exe")
    g._put_text(Win(), "정답은 사과")
    assert near(desk.clicks[0], (1340, 502))                                    # 먼저 '확인'을 눌러 창을 닫고
    assert len(desk.clicks) == 3 and desk.keys == ["^a{BACKSPACE}", "^v"]       # 입력칸 두 번 → 지우고 → 붙여넣기 (Esc 는 안 씀)
    assert g._refound and "확인 창을 닫음" in g.fixes[0]


def test_dialog_after_send_means_not_sent(desk, monkeypatch):
    monkeypatch.setattr(gorilla, "PASTE_CHECK_SECONDS", 0)
    monkeypatch.setattr(gorilla, "CLICK_GAP_SECONDS", 0)
    state = {"sent": False}

    def image(handle):
        return mini_window(dialog=state["sent"])
    monkeypatch.setattr(gorilla, "window_image", image)
    monkeypatch.setattr(gorilla, "focused_value", lambda: (None, ""))
    g = coords_gorilla(app="mini", process_name="mbc mini.exe")
    monkeypatch.setattr(g, "_with_focus", lambda fn: fn(Win()))
    monkeypatch.setattr(g, "evidence_image", lambda w: None)
    real_press = g._press_send

    def press(w):
        real_press(w)
        state["sent"] = True                                                    # 등록을 누르자 빈 칸이라 확인 창이 뜸
    monkeypatch.setattr(g, "_press_send", press)
    result = g.send("정답은 사과")
    assert result.status == "failed" and "다시 보냅니다" in result.detail
    assert near(desk.clicks[-1], (1340, 502))                                   # 확인 창은 닫아 둠


def test_mini_and_rainbow_wait_longer(monkeypatch):
    waited = []
    monkeypatch.setattr(gorilla.time, "sleep", waited.append)
    coords_gorilla(app="mini")._wait(0.3)
    coords_gorilla(app="gorilla")._wait(0.3)
    assert waited == [0.6, 0.3]



# ── 0.80: MBC mini — 간격 두고 두 번 클릭 → 붙여넣기 → 2초 뒤 확인 → 안 됐으면 복사·붙여넣기부터 다시 ──────────
class InputBox:
    """mini 입력칸 흉내: 붙여넣기가 몇 번째부터 들어가는지 정하고, 입력칸 사진(글이 있으면 다름)을 돌려준다."""

    def __init__(self, works_from=1):
        from PIL import Image

        self.works_from, self.pastes, self.value, self.clip = works_from, 0, "", None
        self.empty = Image.new("L", (440, 32), 240)
        self.full = Image.new("L", (440, 32), 240)
        self.full.paste(20, (10, 8, 300, 24))                                       # 글자 줄

    def keys(self, k, pause=None):
        if k == "^a{BACKSPACE}":
            self.value = ""
        elif k == "^v":
            self.pastes += 1
            if self.pastes >= self.works_from:
                self.value = self.clip or ""

    def grab(self, box):
        return self.full if self.value else self.empty


@pytest.fixture
def mini_box(desk, monkeypatch):
    box = InputBox()
    sys.modules["pywinauto.keyboard"].send_keys = lambda k, pause=None: (desk.keys.append(k), box.keys(k))
    monkeypatch.setattr(gorilla.Gorilla, "_clipboard", staticmethod(lambda text=None: setattr(box, "clip", text) or text))
    monkeypatch.setattr(gorilla, "grab_region", box.grab)
    monkeypatch.setattr(gorilla, "focused_value", lambda: (None, ""))              # mini 는 입력칸 글을 못 읽는다고 봄
    monkeypatch.setattr(gorilla, "window_image", lambda h: None)
    monkeypatch.setattr(gorilla, "PASTE_CHECK_SECONDS", 0)
    monkeypatch.setattr(gorilla, "CLICK_GAP_SECONDS", 0)
    waited = []
    monkeypatch.setattr(gorilla.time, "sleep", waited.append)
    box.waited = waited
    return box


def test_mini_clicks_twice_pastes_and_checks(mini_box, desk):
    g = coords_gorilla(app="mini", process_name="mbc mini.exe")
    g._put_text(Win(), "정답은 사과")
    assert len(desk.clicks) == 2 and all(len(c) == 2 for c in desk.clicks)        # 두 번 따로 클릭 (더블클릭 아님)
    assert desk.keys == ["^a{BACKSPACE}", "^v"] and mini_box.value == "정답은 사과" and g.fixes == []


def test_mini_paste_retried_from_copy(mini_box, desk):
    mini_box.works_from = 3                                                         # 붙여넣기가 두 번 안 들어감
    g = coords_gorilla(app="mini", process_name="mbc mini.exe")
    g._put_text(Win(), "정답은 사과")
    assert mini_box.pastes == 3 and len(desk.clicks) == 6 and "2번 안 들어가 다시 붙여넣음" in g.fixes[0]


def test_mini_never_pasted_does_not_press_register(mini_box, desk, monkeypatch):
    mini_box.works_from = 99
    monkeypatch.setattr(gorilla, "type_unicode", lambda text, **kw: None)          # 직접 입력도 안 됨
    g = coords_gorilla(app="mini", process_name="mbc mini.exe")
    with pytest.raises(gorilla.GorillaError, match="등록을 누르지 않았습니다"):
        g._put_text(Win(), "정답은 사과")
    assert mini_box.pastes == gorilla.PASTE_TRIES


def test_empty_text_is_never_pasted(desk):
    r = coords_gorilla(app="mini").send("  \n ")
    assert r.status == "failed" and "비어" in r.detail and desk.clicks == [] and desk.keys == []


# ── 0.81: CBS 레인보우도 mini 와 같은 방식 (간격 두고 두 번 클릭 → 붙여넣기 → 2초 뒤 확인 → 안 됐으면 다시) ─────
def test_rainbow_clicks_twice_pastes_and_checks(mini_box, desk):
    g = rainbow()
    g._put_text(Win(), "정답은 사과")
    assert desk.clicks == [(1320, 902, 2), (1320, 902)]          # 더블클릭(커서 넣기) → 간격 → 한 번 더
    assert desk.keys == ["{ESC}", "^a{BACKSPACE}", "^v"] and mini_box.value == "정답은 사과"
    assert gorilla.PASTE_CHECK_SECONDS in mini_box.waited and g.fixes == []


def test_rainbow_paste_retried_from_copy(mini_box, desk):
    mini_box.works_from = 2
    g = rainbow()
    g._put_text(Win(), "정답은 사과")
    assert mini_box.pastes == 2 and len(desk.clicks) == 4 and "1번 안 들어가 다시 붙여넣음" in g.fixes[0]


def test_rainbow_never_pasted_does_not_press_send(mini_box, desk, monkeypatch):
    mini_box.works_from = 99
    monkeypatch.setattr(gorilla, "type_unicode", lambda text, **kw: None)
    g = rainbow()
    monkeypatch.setattr(g, "_with_focus", lambda fn: fn(Win()))
    monkeypatch.setattr(g, "evidence_image", lambda w: None)
    result = g.send("정답은 사과")
    assert result.status == "failed" and "등록을 누르지 않았습니다" in result.detail
    assert mini_box.pastes == gorilla.PASTE_TRIES and (1576, 902) not in desk.clicks


def test_unreadable_and_no_screenshot_still_sends(desk, monkeypatch):
    """입력칸 글도 못 읽고 사진도 못 찍으면 확인할 길이 없어 예전처럼 한 번에 넣고 보낸다 (막지 않음)."""
    monkeypatch.setattr(gorilla, "focused_value", lambda: (None, ""))
    monkeypatch.setattr(gorilla, "grab_region", lambda box: (_ for _ in ()).throw(OSError("no screen")))
    g = coords_gorilla(app="mini", process_name="mbc mini.exe")
    g._put_text(Win(), "정답")
    assert desk.keys == ["^a{BACKSPACE}", "^v"] and g.fixes == []


# ── 0.82: 보내기 클릭이 안 되면(입력칸에 글이 그대로) 입력칸을 눌러 '엔터'로 다시 ─────────────────────
SEND_POINT = (1576, 902)


def sending(mini_box, desk, monkeypatch, click_works: bool, enter_works: bool, app="mini"):
    """보내기 클릭·엔터가 입력칸을 비우는지(=전송됐는지) 정해 두고 한 번 보낸다."""
    def click(x, y, clicks=1):
        desk.clicks.append((x, y) if clicks == 1 else (x, y, clicks))
        if (x, y) == SEND_POINT and click_works:
            mini_box.value = ""
    monkeypatch.setattr(gorilla, "click_at", click)

    def keys(k, pause=None):
        desk.keys.append(k)
        mini_box.keys(k)
        if k == "{ENTER}" and enter_works:
            mini_box.value = ""
    sys.modules["pywinauto.keyboard"].send_keys = keys
    g = coords_gorilla(app=app, process_name="flutter_rainbow.exe" if app == "rainbow" else "mbc mini.exe")
    monkeypatch.setattr(g, "_with_focus", lambda fn: fn(Win()))
    monkeypatch.setattr(g, "evidence_image", lambda w: None)
    return g, g.send("정답은 사과")


def test_send_click_works_no_enter(mini_box, desk, monkeypatch):
    g, r = sending(mini_box, desk, monkeypatch, click_works=True, enter_works=True)
    assert r.status == "entered" and "{ENTER}" not in desk.keys and g.fixes == []


def test_send_click_ignored_then_enter(mini_box, desk, monkeypatch):
    g, r = sending(mini_box, desk, monkeypatch, click_works=False, enter_works=True)
    assert r.status == "entered" and desk.keys[-1] == "{ENTER}"
    assert "엔터로 다시 보냄" in r.detail
    after_send = desk.clicks[desk.clicks.index(SEND_POINT) + 1:]
    assert after_send == [(1320, 902), (1320, 902)]                  # 입력칸을 다시 눌러 커서를 두고 엔터


def test_rainbow_send_click_ignored_then_enter(mini_box, desk, monkeypatch):
    g, r = sending(mini_box, desk, monkeypatch, click_works=False, enter_works=True, app="rainbow")
    assert r.status == "entered" and "{ENTER}" in desk.keys and "엔터로 다시 보냄" in r.detail


def test_click_and_enter_both_fail_is_retried_later(mini_box, desk, monkeypatch):
    g, r = sending(mini_box, desk, monkeypatch, click_works=False, enter_works=False)
    assert r.status == "failed" and "엔터 모두 안 돼" in r.detail and desk.keys.count("{ENTER}") == 1


def test_unknown_input_state_does_not_press_enter(desk, monkeypatch):
    """입력칸 사진을 못 찍는 등 남았는지 모르면 엔터를 누르지 않는다 (두 번 보내지 않게)."""
    monkeypatch.setattr(gorilla, "focused_value", lambda: (None, ""))
    monkeypatch.setattr(gorilla, "grab_region", lambda box: (_ for _ in ()).throw(OSError("no screen")))
    g = coords_gorilla(app="mini", process_name="mbc mini.exe")
    monkeypatch.setattr(g, "_with_focus", lambda fn: fn(Win()))
    monkeypatch.setattr(g, "evidence_image", lambda w: None)
    r = g.send("정답")
    assert r.status == "entered" and "{ENTER}" not in desk.keys


def test_chat_list_focus_is_not_taken_for_input(desk, monkeypatch):
    monkeypatch.setattr(gorilla, "focused_value", lambda: ("…정답은 사과", "List '채팅'"))
    g = coords_gorilla(app="mini", process_name="mbc mini.exe")
    g._filled = None
    assert g._text_left(None, "정답은 사과") is False
    monkeypatch.setattr(gorilla, "focused_value", lambda: ("정답은 사과", "Edit '메시지'"))
    assert g._text_left(None, "정답은 사과") is True


# ── 0.83: 레인보우는 글이 입력칸 한 칸을 넘으면 전송이 안 됨 (사용자 화면 사진) → 한 칸 안으로 ───────────────
LONG = ("오늘 아침 커피를 내리다 보니 예전 생각이 나네요. 저는 커피를 마실 때 잠깐 숨을 고르고 창밖을 바라보곤 해요. "
        "오늘도 커피 향처럼 포근한 하루 보내세요. (신청곡: 브라운 아이즈 - With Coffee)")


def test_fit_text_drops_song_then_last_sentences():
    limit = config.APP_CHAT_LIMITS["rainbow"]
    out = config.fit_text(LONG, limit)
    assert len(out) <= limit and out == "오늘 아침 커피를 내리다 보니 예전 생각이 나네요."
    assert config.fit_text("짧은 글 (신청곡: 가수 - 노래)", 60) == "짧은 글 (신청곡: 가수 - 노래)"   # 들어가면 그대로
    assert config.fit_text("한 문장이 아주 길어서 문장 단위로는 줄일 수 없는 글", 10) == "한 문장이 아주…"


def test_rainbow_chat_limit(conn):
    assert config.chat_limit(conn, "CBS 음악FM") == config.APP_CHAT_LIMITS["rainbow"]
    assert config.chat_limit(conn, "SBS 파워FM") == 200


def test_rainbow_send_shortens_long_text(typed, desk, monkeypatch):
    g = rainbow()
    monkeypatch.setattr(g, "_with_focus", lambda fn: fn(Win()))
    monkeypatch.setattr(g, "evidence_image", lambda w: None)
    r = g.send(LONG)
    assert typed.clip == "오늘 아침 커피를 내리다 보니 예전 생각이 나네요." and "한 칸(60자)에 맞게 줄임" in r.detail
    typed.clip = None
    m = coords_gorilla(app="mini", process_name="mbc mini.exe")
    monkeypatch.setattr(m, "_with_focus", lambda fn: fn(Win()))
    monkeypatch.setattr(m, "evidence_image", lambda w: None)
    m.send(LONG)
    assert typed.clip == LONG                                                       # mini 는 줄이지 않음


def test_story_prompt_tells_rainbow_limit():
    from radio_helper.quizbot import answerer

    msg = answerer.build_story_message("아침", "녹취", {"chat_limit": 60}, [])
    assert "60자 이내" in msg and "60자" not in answerer.build_story_message("아침", "녹취", {}, [])


# ── 0.84: 같은 답을 두 번 보냄 (사용자 화면 사진) — 내가 보낸 보라색 말풍선을 '확인' 창으로 잘못 보고 '못 보냄' → 다시 보냄 ──
def bubble_window(light_bg: bool, size=(640, 950)):
    """mini 창 흉내: 확인 창 없이, 가운데에 내가 보낸 글의 보라색 말풍선(흰 글씨 줄)만."""
    from PIL import Image, ImageDraw

    w, h = size
    img = Image.new("RGB", size, (250, 250, 252) if light_bg else (20, 28, 48))
    d = ImageDraw.Draw(img)
    d.rectangle((150, 420, 450, 490), fill=(123, 69, 207))                    # 말풍선 300×70
    for y in (435, 460):
        d.rectangle((165, y, 420, y + 8), fill=(255, 255, 255))                # 글자 줄
    d.rectangle((w - 80, h - 60, w - 1, h - 1), fill=(123, 69, 207))          # 등록
    return img


def test_own_purple_bubble_is_not_the_confirm_dialog():
    frame = (1000, 0, 1640, 950)
    assert gorilla.find_dialog_button(bubble_window(light_bg=True), frame) is None
    assert gorilla.find_dialog_button(bubble_window(light_bg=False), frame) is None
    assert near(gorilla.find_dialog_button(mini_window(), frame), (1340, 502))   # 진짜 확인 창은 그대로 찾음


def test_dialog_after_verified_paste_is_not_resent(mini_box, desk, monkeypatch):
    """등록 전에 입력칸에 글이 든 것을 확인했는데 등록 뒤 확인 창이 보이면, 보내진 것으로 보고 다시 보내지 않는다."""
    state = {"sent": False}
    monkeypatch.setattr(gorilla, "window_image", lambda h: mini_window(dialog=state["sent"]))

    def click(x, y, clicks=1):
        desk.clicks.append((x, y) if clicks == 1 else (x, y, clicks))
        if (x, y) == SEND_POINT:
            mini_box.value, state["sent"] = "", True
    monkeypatch.setattr(gorilla, "click_at", click)
    g = coords_gorilla(app="mini", process_name="mbc mini.exe")
    monkeypatch.setattr(g, "_with_focus", lambda fn: fn(Win()))
    monkeypatch.setattr(g, "evidence_image", lambda w: None)
    r = g.send("2번")
    assert r.status == "unknown" and "다시 보내지 않습니다" in r.detail


def test_enter_on_empty_box_means_click_already_sent(mini_box, desk, monkeypatch):
    """보내기 클릭 뒤 글이 남은 것처럼 보여 엔터를 눌렀는데 확인 창이 뜨면 = 클릭으로 이미 보내졌음 → 다시 보내지 않음."""
    state = {"enter": False}
    monkeypatch.setattr(gorilla, "window_image", lambda h: mini_window(dialog=state["enter"]))
    monkeypatch.setattr(gorilla.Gorilla, "_text_left", lambda self, edit, text: not state["enter"])

    def keys(k, pause=None):
        desk.keys.append(k)
        mini_box.keys(k)
        if k == "{ENTER}":
            state["enter"] = True
    sys.modules["pywinauto.keyboard"].send_keys = keys
    g = coords_gorilla(app="mini", process_name="mbc mini.exe")
    monkeypatch.setattr(g, "_with_focus", lambda fn: fn(Win()))
    monkeypatch.setattr(g, "evidence_image", lambda w: None)
    r = g.send("2번")
    assert r.status == "entered" and "이미 보내진" in r.detail


# ── 0.86: 입력칸·전송 버튼·채팅 창 자리를 잘못 찾던 문제 — 정해 둔 자리를 어림한 자리로 바꾸지 않음 ──────────
def test_closed_dialog_keeps_saved_points(desk, monkeypatch):
    """확인 창을 닫은 뒤에도 위치 클릭 방식이면 화면 요소로 '마지막 입력칸·등록 단추'를 어림하지 않고 정해 둔 자리를 누른다."""
    g = coords_gorilla(app="mini", process_name="mbc mini.exe")
    g._refound = True
    w = Win()
    w.descendants = lambda control_type=None: (_ for _ in ()).throw(AssertionError("화면 요소로 찾으면 안 됨"))
    assert g._fresh_point(w, "input") == (1320, 902) and g._fresh_point(w, "send") == (1576, 902)


def test_moved_window_not_guessed_among_same_size_windows(monkeypatch):
    g = coords_gorilla(app="gorilla", input_mode="screen", send_mode="screen", input_point="1300,900",
                       send_point="1600,900", point_window="1000,0,1640,950")
    two = [{"hwnd": 1, "visible": True, "iconic": False, "normal": (0, 0, 640, 950)},
           {"hwnd": 2, "visible": True, "iconic": False, "normal": (700, 0, 1340, 950)}]
    monkeypatch.setattr(gorilla, "app_windows", lambda proc: two)
    assert g._moved_window("gorealra.exe") is None                                  # 둘 중 어림해 고르지 않음
    monkeypatch.setattr(gorilla, "app_windows", lambda proc: two[1:])
    assert g._moved_window("gorealra.exe") == 2                                     # 하나뿐이면 그 창


# ── 0.87: 전송 버튼 찾기 재점검 — 사용자 화면 사진(네 앱)으로 맞춘 알려진 배치로 정해 둔 자리를 확인 ─────────────
@pytest.mark.parametrize("app,size,inp,send", [
    ("mini", (1250, 897), (900, 854), (1206, 856)),        # 'mini 메시지 작성' / 보라 '등록'
    ("kong", (1197, 789), (891, 686), (1127, 750)),        # '내용 입력하기' / '보내기'
    ("gorilla", (1274, 900), (955, 857), (1217, 857)),     # 플레이어+공감로그 한 화면: '공감로그 글쓰기' / 파란 '전송'
    ("rainbow", (414, 758), (198, 516), (390, 516)),       # '채팅을 입력하세요' / 파란 종이비행기
])
def test_known_boxes_match_user_screens(app, size, inp, send):
    frame = (0, 0) + size
    for target, want in (("input", inp), ("send", send)):
        box = gorilla.known_box(app, target, frame)
        assert box[0] <= want[0] <= box[2] and box[1] <= want[1] <= box[3]


def test_known_box_unknown_shapes():
    assert gorilla.known_box("kong", "send", (0, 0, 420, 840)) is None        # 채팅이 닫힌 좁은 콩 창
    assert gorilla.known_box("gorilla", "send", (0, 0, 634, 949))[0] > 500     # 따로 뜬 공감로그 창은 창 전체 기준
    assert gorilla.known_box(None, "send", (0, 0, 800, 600)) is None


def test_wrong_saved_send_point_is_corrected(desk, monkeypatch):
    """실제 사례: 콩 '보내기 클릭이 안 돼 엔터로 다시 → 클릭·엔터 모두 안 돼' — 저장된 전송 자리가 엉뚱함 → 알려진 자리를 누름."""
    monkeypatch.setattr(gorilla, "known_box", _real_known_box)
    g = coords_gorilla(app="kong", process_name="kong.exe", window_size="1197,789",
                       input_x=0.745, input_y=0.87, send_x=0.5, send_y=0.3)          # 전송 자리가 채팅 목록 한가운데
    w = Win((0, 0, 1197, 789))
    assert g._target_point(w, "input") == (891, 686)                                # 맞는 자리는 그대로
    x, y = g._target_point(w, "send")
    assert 1083 <= x <= 1171 and 727 <= y <= 773 and "알려진 자리" in g.fixes[-1]


def test_flutter_pane_focus_is_not_read_as_empty_input(monkeypatch):
    """실제 사례 (레인보우): '지금 커서 자리: Pane' — 화면 전체의 빈 값을 입력칸 글로 읽어 붙여넣기가 됐어도 안 됐다고 봄."""
    g = rainbow()
    monkeypatch.setattr(gorilla, "focused_value", lambda: ("", "Pane ''"))
    assert g._read_input(None) == (None, "Pane ''")
    monkeypatch.setattr(gorilla, "focused_value", lambda: ("정답", "Edit '채팅'"))
    assert g._read_input(None) == ("정답", "Edit '채팅'")


_real_known_box = gorilla.known_box
