"""고릴라 PC 앱 채팅창에 입력·전송 (윈도우 화면 요소 자동화, pywinauto).

고릴라 화면 구조는 공개 문서가 없어, 두 가지 방법을 지원한다.
  uia    : 화면 요소(Edit 입력칸)를 찾아 글자를 넣는다. 전송 후 입력칸이 비었는지로 전송 여부를 확인할 수 있다.
  coords : '위치 지정'으로 저장한 입력칸·전송 버튼 위치(창 크기 대비 비율)를 클릭한다. 결과 확인은 못 한다.
'창 점검'으로 화면 요소 목록을 먼저 확인하고 방법을 고른다.

전송 중에는 고릴라 창이 잠깐 맨 앞으로 나온다. 끝나면 원래 쓰던 창으로 되돌린다.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from .config import GorillaConfig


@dataclass
class SendResult:
    status: str   # posted / entered / unknown / failed
    detail: str


class GorillaError(RuntimeError):
    pass


def fraction_to_point(rect: tuple[int, int, int, int], fx: float, fy: float) -> tuple[int, int]:
    left, top, right, bottom = rect
    return int(left + (right - left) * fx), int(top + (bottom - top) * fy)


def point_to_fraction(rect: tuple[int, int, int, int], x: int, y: int) -> tuple[float, float]:
    left, top, right, bottom = rect
    w, h = max(1, right - left), max(1, bottom - top)
    if not (left <= x <= right and top <= y <= bottom):
        raise GorillaError("마우스가 고릴라 창 밖에 있습니다.")
    return round((x - left) / w, 4), round((y - top) / h, 4)


def judge_result(mode: str, input_after: str | None, text: str, seen_in_chat: bool) -> SendResult:
    """전송 뒤 상태 판단. 확인할 수 없으면 '결과 불명'으로 두고 다시 보내지 않는다."""
    if seen_in_chat:
        return SendResult("posted", "채팅 목록에서 보낸 문구를 확인함")
    if mode == "coords":
        return SendResult("entered", "위치 클릭 방식이라 게시 여부는 확인하지 못함")
    if input_after is not None and text not in input_after:
        return SendResult("entered", "입력칸이 비워져 전송된 것으로 보임 (채팅 목록에서는 확인 못 함)")
    return SendResult("unknown", "전송 후에도 입력칸에 글자가 남아 있거나 확인할 수 없음")


class Gorilla:
    def __init__(self, cfg: GorillaConfig):
        self.cfg = cfg

    # ── 창 찾기 ─────────────────────────────────────────────────
    def find_window(self):
        from pywinauto import Desktop

        pattern = re.compile(self.cfg.window_title or "고릴라", re.I)
        for w in Desktop(backend="uia").windows():
            try:
                title = w.window_text()
            except Exception:
                continue
            if title and pattern.search(title):
                return w
        return None

    def is_running(self) -> bool:
        try:
            return self.find_window() is not None
        except Exception:
            return False

    def _window(self):
        w = self.find_window()
        if w is None:
            raise GorillaError("고릴라 창을 찾지 못했습니다. 고릴라 PC 앱이 실행 중인지 확인하세요.")
        return w

    @staticmethod
    def _rect(w) -> tuple[int, int, int, int]:
        r = w.rectangle()
        return r.left, r.top, r.right, r.bottom

    # ── 점검·위치 지정 ───────────────────────────────────────────
    def inspect(self, max_items: int = 400) -> dict:
        w = self._window()
        left, top, right, bottom = self._rect(w)
        items = []
        for c in w.descendants():
            if len(items) >= max_items:
                break
            info = c.element_info
            r = info.rectangle
            items.append({
                "control_type": info.control_type, "name": (info.name or "")[:60],
                "auto_id": info.automation_id or "", "class_name": info.class_name or "",
                "rect_fraction": [round((r.left - left) / max(1, right - left), 3),
                                  round((r.top - top) / max(1, bottom - top), 3),
                                  round((r.right - left) / max(1, right - left), 3),
                                  round((r.bottom - top) / max(1, bottom - top), 3)],
            })
        edits = [i for i in items if i["control_type"] == "Edit"]
        return {"title": w.window_text(), "window_rect": [left, top, right, bottom], "edit_count": len(edits),
                "button_names": sorted({i["name"] for i in items if i["control_type"] == "Button" and i["name"]})[:50],
                "controls": items}

    def cursor_fraction(self) -> tuple[float, float]:
        import ctypes

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        pt = POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return point_to_fraction(self._rect(self._window()), pt.x, pt.y)

    # ── 입력·전송 ───────────────────────────────────────────────
    def _find_input(self, w):
        edits = w.descendants(control_type="Edit")
        if self.cfg.input_auto_id:
            edits = [e for e in edits if e.element_info.automation_id == self.cfg.input_auto_id]
        elif self.cfg.input_name:
            edits = [e for e in edits if re.search(self.cfg.input_name, e.element_info.name or "")]
        edits = [e for e in edits if e.is_visible()]
        if not edits:
            raise GorillaError("고릴라 창에서 채팅 입력칸(Edit)을 찾지 못했습니다. '창 점검' 후 위치 지정 방식을 쓰세요.")
        return edits[-1]  # 채팅 입력칸은 보통 화면 아래쪽 마지막 입력칸

    @staticmethod
    def _value(edit) -> str | None:
        for getter in ("get_value", "window_text"):
            try:
                return getattr(edit, getter)() or ""
            except Exception:
                continue
        return None

    def _paste(self, text: str) -> None:
        import win32clipboard
        import win32con
        from pywinauto.keyboard import send_keys

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        send_keys("^a{BACKSPACE}^v", pause=0.05)

    def _put_text(self, w, text: str):
        """입력칸에 글자를 넣고 입력칸 객체(uia) 또는 None(coords)을 돌려준다."""
        from pywinauto import mouse

        if self.cfg.input_mode == "coords":
            if self.cfg.input_x is None or self.cfg.input_y is None:
                raise GorillaError("입력칸 위치가 지정되지 않았습니다. 고릴라 설정에서 '입력칸 위치 지정'을 하세요.")
            mouse.click(coords=fraction_to_point(self._rect(w), self.cfg.input_x, self.cfg.input_y))
            time.sleep(0.2)
            self._paste(text)
            return None
        edit = self._find_input(w)
        edit.set_focus()
        try:
            edit.set_edit_text(text)
        except Exception:
            self._paste(text)
        if text not in (self._value(edit) or ""):
            self._paste(text)
        if text not in (self._value(edit) or ""):
            raise GorillaError("입력칸에 글자를 넣지 못했습니다.")
        return edit

    def _press_send(self, w) -> None:
        from pywinauto import mouse
        from pywinauto.keyboard import send_keys

        if self.cfg.send_mode == "button":
            pattern = re.compile(self.cfg.send_button_name or "전송")
            buttons = [b for b in w.descendants(control_type="Button")
                       if pattern.search(b.element_info.name or "") and b.is_visible()]
            if not buttons:
                raise GorillaError("전송 버튼을 찾지 못했습니다.")
            buttons[-1].click_input()
        elif self.cfg.send_mode == "coords":
            if self.cfg.send_x is None or self.cfg.send_y is None:
                raise GorillaError("전송 버튼 위치가 지정되지 않았습니다.")
            mouse.click(coords=fraction_to_point(self._rect(w), self.cfg.send_x, self.cfg.send_y))
        else:
            send_keys("{ENTER}")

    def _seen_in_chat(self, w, text: str) -> bool:
        try:
            for t in w.descendants(control_type="Text")[-40:]:
                if text in (t.element_info.name or ""):
                    return True
        except Exception:
            pass
        return False

    def _with_focus(self, fn):
        import ctypes

        user32 = ctypes.windll.user32
        previous = user32.GetForegroundWindow()
        w = self._window()
        try:
            if w.is_minimized():
                w.restore()
            w.set_focus()
            time.sleep(0.3)
            return fn(w)
        finally:
            if previous:
                user32.SetForegroundWindow(previous)

    def type_only(self, text: str) -> str:
        """입력칸에 글자만 넣고 보내지 않는다 (설정 확인용)."""
        def run(w):
            self._put_text(w, text)
            return "입력칸에 글자를 넣었습니다. 보내지 않았으니 고릴라에서 직접 지워 주세요."
        return self._with_focus(run)

    def send(self, text: str) -> SendResult:
        def run(w):
            edit = self._put_text(w, text)
            self._press_send(w)
            time.sleep(1.5)
            after = self._value(edit) if edit is not None else None
            return judge_result(self.cfg.input_mode, after, text, self._seen_in_chat(w, text))
        try:
            return self._with_focus(run)
        except GorillaError as e:
            return SendResult("failed", str(e))


def dump(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)
