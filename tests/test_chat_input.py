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
        if k == "^a{BACKSPACE}^v" and box.paste_works:
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
    assert result.status == "failed" and "전송을 누르지 않았습니다" in result.detail
    assert all(len(c) == 3 for c in desk.clicks)      # 입력칸(두 번 클릭)만 눌렀고 전송 버튼은 누르지 않음


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
    assert desk.keys == ["{ESC}", "^a{BACKSPACE}^v"]


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
    shots = iter([mini_window(), mini_window(dialog=False)])
    monkeypatch.setattr(gorilla, "window_image", lambda handle: next(shots, mini_window(dialog=False)))
    monkeypatch.setattr(gorilla, "focused_value", lambda: (None, ""))
    g = coords_gorilla(app="mini", process_name="mbc mini.exe")
    g._put_text(Win(), "정답은 사과")
    assert near(desk.clicks[0], (1340, 502))                                    # 먼저 '확인'을 눌러 창을 닫고
    assert len(desk.clicks) == 2 and desk.keys == ["^a{BACKSPACE}^v"]           # 입력칸 → 붙여넣기 (Esc 는 안 씀)
    assert g._refound and "확인 창을 닫음" in g.fixes[0]


def test_dialog_after_send_means_not_sent(desk, monkeypatch):
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
