"""모니터 기준 위치: 채팅(공감로그)이 플레이어 오른쪽에 따로 펼쳐지는 고릴라 PC 앱.

예전에는 지정한 위치를 '고릴라 창 안의 비율'로 저장했는데, 보낼 때 찾은 창이 왼쪽 플레이어 창이라
엉뚱하게 플레이어를 눌렀다. 이제 지정한 자리(모니터 좌표)를 그대로 누르고, 그 자리의 창(채팅 창)을 대상으로 한다.
"""

from types import SimpleNamespace

import pytest
import test_sound_stop
from test_sound_stop import Win

from radio_helper.quizbot import config, gorilla

desk = test_sound_stop.desk   # 가짜 윈도우 화면 준비물

PLAYER = {"hwnd": 1001, "title": "고릴라", "process": "gorealra.exe", "rect": (67, 88, 483, 643)}
CHAT = {"hwnd": 2002, "title": "", "process": "gorealra.exe", "rect": (483, 88, 854, 643)}
CHROME = {"hwnd": 3003, "title": "라디오 참여 도우미 - Chrome", "process": "chrome.exe", "rect": (0, 0, 2000, 800)}


class AppWin(Win):
    def __init__(self, info):
        super().__init__(info["rect"])
        self.handle = info["hwnd"]


@pytest.fixture
def screen(desk, monkeypatch):
    """플레이어(왼쪽)와 채팅 창(오른쪽)이 나란히 있는 모니터."""
    state = SimpleNamespace(cover=None, windows={1001: PLAYER, 2002: CHAT, 3003: CHROME})

    def at(x, y):
        if state.cover:
            return dict(state.cover, point=(x, y))
        for info in (CHAT, PLAYER):
            left, top, right, bottom = info["rect"]
            if left <= x < right and top <= y < bottom:
                return dict(info, point=(x, y))
        return {"hwnd": 9, "title": "바탕 화면", "process": "explorer.exe", "rect": (0, 0, 1, 1), "point": (x, y)}

    monkeypatch.setattr(gorilla, "window_at_point", at)
    monkeypatch.setattr(gorilla, "app_window_at", lambda x, y, proc: 2002 if 483 <= x < 854 else None)
    sys_pw = __import__("sys").modules["pywinauto"]
    sys_pw.Desktop = lambda backend: SimpleNamespace(
        window=lambda handle: SimpleNamespace(wrapper_object=lambda: AppWin(state.windows[handle])))
    desk.front = 2002
    return state


def chat_gorilla(**over):
    v = dict(process_name="gorealra.exe", window_size="640,958", input_mode="screen", send_mode="screen",
             input_x=0.4539, input_y=0.9494, send_x=0.9125, send_y=0.953,      # 예전 비율 값은 남아 있어도 안 씀
             input_point="650,617", send_point="819,617", point_window="483,88,854,643")
    v.update(over)
    return gorilla.Gorilla(config.GorillaConfig(**v))


def test_clicks_exactly_where_marked_on_the_chat_window(desk, screen):
    g = chat_gorilla()
    w = g._window()
    assert w.handle == 2002                                   # 왼쪽 플레이어 창이 아니라 오른쪽 채팅 창
    g._put_text(w, "봉숭아학당")
    g._press_send(w)
    assert desk.clicks == [(650, 617), (819, 617)] and desk.keys == ["^a{BACKSPACE}^v"]
    assert g.click_points(w) == [((650, 617), "#ef4444"), ((819, 617), "#22c55e")]   # 전송 테스트 사진의 빨강·초록
    assert g._ref_rect(w) == CHAT["rect"]                     # 증거 사진도 채팅 창을 찍음


def test_chat_window_behind_browser_is_found_then_must_be_uncovered(desk, screen):
    screen.cover = CHROME                                      # 관리 화면(크롬)이 채팅 창을 덮고 있음
    g = chat_gorilla()
    w = g._window()
    assert w.handle == 2002                                   # 가려져 있어도 그 자리의 고릴라 창을 찾아 앞으로 가져옴
    with pytest.raises(gorilla.GorillaError, match="가리고"):   # 앞으로 못 가져오면 크롬을 누르지 않음
        g._put_text(w, "봉숭아학당")
    assert desk.clicks == []


def test_no_gorilla_window_at_marked_spot(desk, screen, monkeypatch):
    monkeypatch.setattr(gorilla, "app_window_at", lambda x, y, proc: None)
    g = chat_gorilla(input_point="1500,700", send_point="1600,700")   # 채팅 창을 닫았거나 다른 자리로 옮김
    with pytest.raises(gorilla.GorillaError, match=r"입력칸 자리\(모니터 1500, 700\)에 고릴라 창이 없습니다"):
        g._window()


def test_moved_chat_window_is_not_clicked(desk, screen):
    g = chat_gorilla(point_window="300,88,671,643")           # 지정할 때는 채팅 창이 다른 자리에 있었음
    w = g._window()
    with pytest.raises(gorilla.GorillaError, match="다른 자리"):
        g._put_text(w, "봉숭아학당")
    assert desk.clicks == []


def test_old_relative_settings_still_work(desk):
    g = test_sound_stop.coords_gorilla()                      # 예전 방식(창 안 비율)은 그대로 동작
    g._put_text(Win(), "사과")
    assert desk.clicks == [(1320, 902)]


def test_chat_list_is_read_from_the_chat_window(desk, screen, monkeypatch):
    image = pytest.importorskip("PIL.Image")
    import io

    shot = image.new("RGB", (371, 555), (255, 255, 255))      # 채팅 창 전체 사진 (가려져 있어도 찍힘)
    shot.paste((200, 0, 0), (17, 12, 357, 512))               # 채팅 목록 자리
    monkeypatch.setattr(gorilla, "window_image", lambda hwnd: shot if hwnd == 2002 else None)
    g = chat_gorilla(chat_rect="0.1,0.1,0.9,0.9", chat_screen="500,100,840,600")
    data = g.chat_image()
    out = image.open(io.BytesIO(data))
    assert out.size == (340, 500) and out.getpixel((170, 250))[0] > 150   # 그 자리만 잘라 냄 (플레이어 창이 아님)


def test_settings_page_asks_to_remark_old_positions_and_shows_points(client, conn):
    from radio_helper import db

    for k, v in {"gorilla.input_mode": "coords", "gorilla.send_mode": "coords", "gorilla.input_x": "0.4539",
                 "gorilla.input_y": "0.9494", "gorilla.process_name": "gorealra.exe"}.items():
        db.set_setting(conn, k, v)
    page = client.get("/gorilla").get_data(as_text=True)
    assert "모니터 화면 기준</b>으로 저장합니다" in page and "다시 해 주세요" in page
    for k, v in {"gorilla.input_mode": "screen", "gorilla.send_mode": "screen", "gorilla.input_point": "650,617",
                 "gorilla.send_point": "819,617", "gorilla.input_rect": "0.4,0.9,0.5,0.95"}.items():
        db.set_setting(conn, k, v)
    page = client.get("/gorilla").get_data(as_text=True)
    assert "다시 해 주세요" not in page and "입력칸 모니터 위치 (650,617)" in page
    assert "전송 버튼 모니터 위치 (819,617)" in page and "모니터 화면 좌표" in page


def test_running_check_uses_marked_spot_like_send(desk, screen, monkeypatch):
    """보낼 때와 같은 방법(표시한 자리의 창)으로 '고릴라가 켜져 있는지' 본다.
    예전엔 창 목록 검색으로 봐서, 채팅이 따로 뜨는 고릴라를 못 찾아 '고릴라 창을 찾지 못함'으로 보류했다."""
    monkeypatch.setattr(gorilla, "_top_windows", lambda: [])          # 창 목록 검색으로는 못 찾는 앱
    g = chat_gorilla()
    assert g.is_running()                                              # 표시한 자리에 채팅 창이 있음
    screen.cover = CHROME                                              # 크롬에 가려져 있어도 그 자리의 고릴라 창을 찾음
    assert g.is_running()
    monkeypatch.setattr(gorilla, "app_window_at", lambda x, y, proc: None)
    assert not g.is_running()                                          # 정말 없을 때만 '못 찾음'
