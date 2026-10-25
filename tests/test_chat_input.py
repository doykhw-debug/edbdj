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
