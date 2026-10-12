"""보낼 때 채팅 창이 그 자리에 없으면 (최소화·옮겨짐·꺼짐) 되살려서 보낸다 — '무조건 보내기'.
실제 사례: 입력칸 자리(모니터 1384, 785)에 'Program Manager'(바탕화면)만 있어 1분마다 다시 시도만 하다 못 보냄."""

import os
from types import SimpleNamespace

import pytest
import test_screen_points
import test_sound_stop
from test_screen_points import CHAT, AppWin, chat_gorilla

from radio_helper.quizbot import answerer, gorilla

desk = test_sound_stop.desk
screen = test_screen_points.screen
DESKTOP = {"hwnd": 65552, "title": "Program Manager", "process": "explorer.exe", "rect": (0, 0, 1920, 1080)}


@pytest.fixture
def gone(desk, screen, monkeypatch):
    """채팅 창이 그 자리에 없는 모니터: restore/launch 가 일어나면 원래 자리(CHAT)에 창이 생긴다."""
    st = SimpleNamespace(back=False, restored=[], launched=[], wins=[])

    def at(x, y):
        left, top, right, bottom = CHAT["rect"]
        if st.back and left <= x < right and top <= y < bottom:
            return dict(CHAT, point=(x, y))
        return dict(DESKTOP, point=(x, y))

    def restore(hwnd, rect, iconic, visible):
        st.restored.append((hwnd, rect, iconic, visible))
        st.back = True

    monkeypatch.setattr(gorilla, "window_at_point", at)
    monkeypatch.setattr(gorilla, "app_window_at", lambda x, y, proc: 2002 if st.back and at(x, y)["hwnd"] == 2002 else None)
    monkeypatch.setattr(gorilla, "app_windows", lambda proc: st.wins)
    monkeypatch.setattr(gorilla, "restore_window", restore)
    monkeypatch.setattr(gorilla.os, "startfile", lambda path: st.launched.append(path), raising=False)
    return st


def test_minimized_and_moved_chat_window_is_restored_then_sent(desk, gone):
    gone.wins = [{"hwnd": 2002, "title": "", "visible": True, "iconic": True, "normal": (100, 100, 471, 655)}]
    g = chat_gorilla()
    w = g._window()
    assert w.handle == 2002
    assert gone.restored == [(2002, (483.0, 88.0, 854.0, 643.0), True, True)]       # 지정 때 자리·크기로 되돌림
    assert g.fixes == ["고릴라 채팅 창이 그 자리에 없어 최소화돼 있어 되살림 · 위치 지정 때 자리로 옮김"]
    g._put_text(w, "봉숭아학당")
    g._press_send(w)
    assert desk.clicks == [(650, 617), (819, 617)]                                    # 되살린 채팅 창을 눌러 보냄


def test_hidden_to_tray_window_is_shown_in_place(desk, gone):
    gone.wins = [{"hwnd": 1001, "title": "고릴라", "visible": False, "iconic": False, "normal": (67, 88, 483, 643)},
                 {"hwnd": 2002, "title": "공감로그", "visible": False, "iconic": False, "normal": (483, 88, 854, 643)}]
    g = chat_gorilla()
    assert g._window().handle == 2002
    assert gone.restored == [(2002, None, False, False)]                              # 크기가 맞는 채팅 창, 자리는 그대로
    assert g.fixes == ["고릴라 채팅 창이 그 자리에 없어 숨어 있어 다시 띄움"]


def test_closed_app_is_relaunched_from_saved_exe(desk, gone, tmp_path, monkeypatch):
    exe = tmp_path / "gorealra.exe"
    exe.write_bytes(b"")

    def startfile(path):
        gone.launched.append(path)
        gone.back = True                                                   # 다시 켜자 채팅 창이 원래 자리에 뜸
        gone.wins = [{"hwnd": 2002, "title": "", "visible": True, "iconic": False, "normal": CHAT["rect"]}]
    monkeypatch.setattr(gorilla.os, "startfile", startfile, raising=False)
    g = chat_gorilla(exe_path=str(exe))
    assert g._window().handle == 2002
    assert gone.launched == [str(exe)] and gone.restored == []
    assert g.fixes == ["고릴라 앱이 꺼져 있어 다시 켬 (gorealra.exe)"]


def test_closed_app_without_known_exe_is_not_clicked(desk, gone, monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    g = chat_gorilla()
    with pytest.raises(gorilla.GorillaError, match="Program Manager.*실행 파일을 몰라"):
        g._window()
    assert desk.clicks == [] and desk.keys == [] and gone.launched == []              # 바탕화면에 Enter 를 누르지 않음


def test_start_menu_shortcut(tmp_path):
    progs = tmp_path / "Programs" / "SBS"
    progs.mkdir(parents=True)
    for name in ("고릴라 제거.lnk", "SBS 고릴라.lnk", "메모장.lnk"):
        (progs / name).write_bytes(b"")
    found = gorilla.start_menu_shortcut(["gorealra", "고릴라"], roots=[str(tmp_path / "Programs")])
    assert os.path.basename(found) == "SBS 고릴라.lnk"
    assert gorilla.start_menu_shortcut(["kong"], roots=[str(tmp_path / "Programs")]) is None


def test_pick_app_window():
    wins = [{"hwnd": 1, "visible": True, "normal": (0, 0, 416, 555)},
            {"hwnd": 2, "visible": False, "normal": (0, 0, 371, 555)}]
    assert gorilla.pick_app_window(wins, (483, 88, 854, 643))["hwnd"] == 2           # 지정 때 크기와 같은 창
    assert gorilla.pick_app_window(wins, None)["hwnd"] == 1                           # 모르면 보이는 창
    assert gorilla.pick_app_window([], None) is None


def test_send_detail_mentions_what_was_fixed(desk, gone, monkeypatch):
    gone.wins = [{"hwnd": 2002, "title": "", "visible": True, "iconic": True, "normal": CHAT["rect"]}]
    g = chat_gorilla()
    monkeypatch.setattr(g, "_with_focus", lambda fn: fn(g._window()))
    monkeypatch.setattr(g, "evidence_image", lambda w: None)
    result = g.send("봉숭아학당")
    assert result.status != "failed" and result.detail.startswith("고릴라 채팅 창이 그 자리에 없어 최소화돼 있어 되살림 → ")
    assert isinstance(g._window(), AppWin)


def test_marking_saves_exe_path_and_answer_always_guessed():
    info = {"hwnd": 2002, "title": "공감로그", "process": "gorealra.exe", "path": r"C:\\Gorealra\\gorealra.exe",
            "rect": CHAT["rect"], "point": (650, 617)}
    settings, _ = gorilla.region_settings("input", (600, 600, 700, 630), info, None)
    assert settings["gorilla.exe_path"] == r"C:\\Gorealra\\gorealra.exe"
    assert gorilla.calibration_settings("send", info)["gorilla.exe_path"] == r"C:\\Gorealra\\gorealra.exe"
    assert "반드시 하나" in answerer.SYSTEM and "삼행시" in answerer.SYSTEM
