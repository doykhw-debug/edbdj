"""고릴라 PC 앱 채팅(공감로그)창에 입력·전송 (윈도우 화면 요소 자동화, pywinauto).

창 찾기
  브라우저·이 도우미 화면처럼 제목에 '고릴라'가 들어갈 수 있는 창은 빼고,
  '공감로그 글쓰기' 입력칸과 '전송' 버튼이 있는 창을 고릴라 채팅창으로 본다.
  '자동 찾기'를 한 번 하면 그 창의 프로그램(실행 파일) 이름을 저장해 이후에는 그 프로그램 창만 본다.

입력 방법
  uia    : 화면 요소(Edit 입력칸)를 찾아 클릭한 뒤 붙여넣는다. 전송 후 입력칸이 비었는지로 전송 여부를 확인한다.
  coords : '위치 지정'으로 저장한 입력칸·전송 버튼 위치(창 크기 대비 비율)를 클릭한다. 결과 확인은 못 한다.

전송 중에는 고릴라 창이 잠깐 맨 앞으로 나온다. 끝나면 원래 쓰던 창과 클립보드를 되돌린다.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

from .config import DEFAULTS, GorillaConfig

DEFAULT_TITLE = DEFAULTS["gorilla.window_title"]

# 제목에 '고릴라'가 들어갈 수 있지만 고릴라 앱이 아닌 창
OWN_TITLE = "라디오 참여 도우미"
EXCLUDED_PROCESSES = {
    "msedge.exe", "chrome.exe", "firefox.exe", "whale.exe", "opera.exe", "brave.exe", "vivaldi.exe",
    "iexplore.exe", "arc.exe", "aside.exe", "python.exe", "pythonw.exe", "explorer.exe", "cmd.exe",
    "conhost.exe", "windowsterminal.exe", "claude.exe", "code.exe", "notepad.exe", "kakaotalk.exe",
}
TITLE_HINT = re.compile(r"고릴라|gorealra|공감로그", re.I)
PROCESS_HINT = re.compile(r"gorealra|gorilla|고릴라", re.I)
CHAT_INPUT_HINT = re.compile(r"공감로그|글쓰기")
SEND_BUTTON_HINT = re.compile(r"^\s*(전송|보내기|등록)\s*$")


@dataclass
class SendResult:
    status: str   # posted / entered / unknown / failed
    detail: str


class GorillaError(RuntimeError):
    pass


# ── 순수 계산 (테스트 가능) ─────────────────────────────────────────
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


def is_excluded(title: str, process: str) -> bool:
    return OWN_TITLE in (title or "") or (process or "").lower() in EXCLUDED_PROCESSES


def score_window(title: str, process: str, edit_names: list[str], button_names: list[str]) -> int:
    """고릴라 채팅창일 가능성 점수. 0 이하는 후보가 아니다."""
    if is_excluded(title, process):
        return -1
    score = 0
    if PROCESS_HINT.search(process or ""):
        score += 3
    if TITLE_HINT.search(title or ""):
        score += 2
    if any(CHAT_INPUT_HINT.search(n or "") for n in edit_names):
        score += 5
    elif edit_names:
        score += 1
    if any(SEND_BUTTON_HINT.search(n or "") for n in button_names):
        score += 3
    return score


# ── 윈도우 조작 ─────────────────────────────────────────────────────
def process_name(pid: int) -> str:
    try:
        from pywinauto.application import process_module

        return os.path.basename(process_module(pid)).lower()
    except Exception:
        pass
    try:
        import ctypes

        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_uint32(len(buf))
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        pass
    return ""


def window_under_cursor() -> dict:
    """마우스 아래에 있는 최상위 창 정보 (화면 요소가 안 보이는 앱도 됨)."""
    import ctypes
    from ctypes import wintypes

    import importlib

    importlib.import_module("pywinauto")  # 클릭과 같은 화면 좌표계(DPI 인식)를 쓰도록 먼저 불러온다

    user32 = ctypes.windll.user32
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    hwnd = user32.WindowFromPoint(pt)
    root = user32.GetAncestor(hwnd, 2) if hwnd else None  # GA_ROOT
    if not root:
        raise GorillaError("마우스 아래에서 창을 찾지 못했습니다.")
    rect = wintypes.RECT()
    user32.GetWindowRect(root, ctypes.byref(rect))
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(root, buf, 512)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(root, ctypes.byref(pid))
    return {"title": buf.value, "process": process_name(pid.value),
            "rect": (rect.left, rect.top, rect.right, rect.bottom), "point": (pt.x, pt.y)}


def calibration_settings(target: str, info: dict) -> dict:
    """마우스 위치로 잰 창 정보 → 저장할 설정 (순수 계산)."""
    if is_excluded(info["title"], info["process"]):
        raise GorillaError(f"마우스가 고릴라가 아닌 창('{info['title'][:40]}', {info['process']}) 위에 있습니다. "
                           "고릴라 창의 해당 위치에 마우스를 올려 두세요.")
    fx, fy = point_to_fraction(info["rect"], *info["point"])
    left, top, right, bottom = info["rect"]
    settings = {
        f"gorilla.{target}_x": str(fx), f"gorilla.{target}_y": str(fy),
        "gorilla.input_mode" if target == "input" else "gorilla.send_mode": "coords",
        "gorilla.window_title": re.escape(info["title"]) if info["title"] else "",
        "gorilla.window_size": f"{right - left},{bottom - top}",
    }
    if info["process"]:
        settings["gorilla.process_name"] = info["process"]
    return settings


def _top_windows():
    """(창, 제목, 프로그램 이름) 목록"""
    from pywinauto import Desktop

    out = []
    for w in Desktop(backend="uia").windows():
        try:
            title = w.window_text() or ""
            pid = w.element_info.process_id
        except Exception:
            continue
        out.append((w, title, process_name(pid) if pid else ""))
    return out


def _control_names(w, control_type: str, limit: int = 300) -> list[str]:
    try:
        return [(c.element_info.name or "") for c in w.descendants(control_type=control_type)[:limit]]
    except Exception:
        return []


class Gorilla:
    def __init__(self, cfg: GorillaConfig):
        self.cfg = cfg

    # ── 창 찾기 ─────────────────────────────────────────────────
    def _candidates(self):
        wins = [(w, t, p) for w, t, p in _top_windows() if not is_excluded(t, p)]
        if self.cfg.process_name:
            cands = [x for x in wins if x[2] == self.cfg.process_name.lower()]
            # 자동 찾기·위치 지정으로 저장한 제목일 때만 좁힌다 (기본 패턴 '고릴라'는 플레이어 창만 고르게 됨)
            if len(cands) > 1 and self.cfg.window_title and self.cfg.window_title != DEFAULT_TITLE:
                titled = [x for x in cands if re.search(self.cfg.window_title, x[1] or "", re.I)]
                cands = titled or cands
            return cands
        pattern = re.compile(self.cfg.window_title or "고릴라", re.I)
        matched = [x for x in wins if x[1] and pattern.search(x[1])]
        # 제목이 맞은 창과 같은 프로그램의 다른 창(예: 따로 뜬 공감로그 창)도 후보에 넣는다
        procs = {x[2] for x in matched if x[2]}
        return matched + [x for x in wins if x[2] in procs and x not in matched]

    def find_window(self):
        cands = self._candidates()
        if len(cands) <= 1:
            return cands[0][0] if cands else None
        if self.cfg.window_size:
            # 위치 지정 때 잰 창과 크기가 가장 비슷한 창 (제목이 바뀌는 앱 대비)
            try:
                tw, th = (int(v) for v in self.cfg.window_size.split(","))
                def size_gap(x):
                    left, top, right, bottom = self._rect(x[0])
                    return abs((right - left) - tw) + abs((bottom - top) - th)
                return min(cands, key=size_gap)[0]
            except (ValueError, AttributeError):
                pass
        # 같은 프로그램 창이 여럿이면(플레이어/공감로그) 채팅 입력칸이 있는 창을 고른다
        best = max(cands, key=lambda x: score_window(x[1], x[2], _control_names(x[0], "Edit"),
                                                     _control_names(x[0], "Button")))
        return best[0]

    def is_running(self) -> bool:
        try:
            return bool(self._candidates())
        except Exception:
            return False

    def _window(self):
        w = self.find_window()
        if w is None:
            raise GorillaError("고릴라 창을 찾지 못했습니다. 고릴라 PC 앱을 켜고 '자동 찾기'를 다시 하세요.")
        return w

    @staticmethod
    def _rect(w) -> tuple[int, int, int, int]:
        r = w.rectangle()
        return r.left, r.top, r.right, r.bottom

    # ── 자동 찾기·점검·위치 지정 ─────────────────────────────────
    def auto_setup(self) -> dict:
        """열린 창을 모두 살펴 고릴라 채팅창을 고르고, 저장할 설정을 돌려준다."""
        rows = []
        for w, title, proc in _top_windows():
            if is_excluded(title, proc) or not (title or proc):
                rows.append({"title": title, "process": proc, "score": -1, "excluded": True})
                continue
            edits = _control_names(w, "Edit")
            buttons = _control_names(w, "Button")
            rows.append({"title": title, "process": proc, "edits": edits[:10],
                         "buttons": [b for b in buttons if b][:20],
                         "score": score_window(title, proc, edits, buttons), "excluded": False})
        candidates = sorted((r for r in rows if r["score"] > 0), key=lambda r: -r["score"])
        report = {"windows": rows, "chosen": candidates[0] if candidates else None, "settings": {}}
        best = report["chosen"]
        if not best or best["score"] < 2:
            report["message"] = ("고릴라 창을 찾지 못했습니다. 고릴라 PC 앱을 켜고 공감로그 화면을 연 뒤 다시 하거나, "
                                 "'입력칸 위치 지정'으로 고릴라 입력칸 위에 마우스를 올려 바로 지정하세요.")
            return report
        if best["score"] < 5 and not best["edits"]:
            # 고릴라 창은 맞지만 화면 요소(입력칸)가 보이지 않는 앱 → 위치 지정 방식
            settings = {"gorilla.window_title": re.escape(best["title"]) if best["title"] else "",
                        "gorilla.input_mode": "coords", "gorilla.send_mode": "coords"}
            if best["process"]:
                settings["gorilla.process_name"] = best["process"]
            report["settings"] = settings
            report["message"] = (f"고릴라 창 '{best['title']}' ({best['process'] or '프로그램 이름 미확인'})을 찾았지만, "
                                 "이 앱은 입력칸이 화면 요소로 보이지 않습니다. 이제 '입력칸 위치 지정'과 "
                                 "'전송 버튼 위치 지정'을 해 주세요.")
            return report
        settings = {"gorilla.window_title": re.escape(best["title"]) if best["title"] else "고릴라"}
        if best["process"]:
            settings["gorilla.process_name"] = best["process"]
        chat_edit = next((n for n in best["edits"] if CHAT_INPUT_HINT.search(n)), None)
        if chat_edit is not None or best["edits"]:
            settings.update({"gorilla.input_mode": "uia",
                             "gorilla.input_name": CHAT_INPUT_HINT.pattern if chat_edit is not None else ""})
            note = "입력칸을 화면 요소로 찾았습니다."
        else:
            settings["gorilla.input_mode"] = "coords"
            note = "입력칸이 화면 요소로 보이지 않습니다. '입력칸 위치 지정'과 '전송 버튼 위치 지정'을 해 주세요."
        has_send = any(SEND_BUTTON_HINT.search(b) for b in best["buttons"])
        settings["gorilla.send_mode"] = "auto" if has_send or settings["gorilla.input_mode"] == "uia" else "coords"
        settings.update({"gorilla.input_x": "", "gorilla.input_y": "", "gorilla.send_x": "", "gorilla.send_y": ""})
        report["settings"] = settings
        report["message"] = (f"고릴라 채팅창: '{best['title']}' ({best['process'] or '프로그램 이름 미확인'}). {note} "
                             + ("전송 버튼도 찾았습니다." if has_send else "전송은 Enter 키로 합니다."))
        return report

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
        try:
            proc = process_name(w.element_info.process_id)
        except Exception:
            proc = ""
        return {"title": w.window_text(), "process": proc, "window_rect": [left, top, right, bottom],
                "edit_count": len(edits), "edit_names": [e["name"] for e in edits][:10],
                "button_names": sorted({i["name"] for i in items if i["control_type"] == "Button" and i["name"]})[:50],
                "controls": items}

    @staticmethod
    def calibrate(target: str) -> tuple[dict, str]:
        info = window_under_cursor()
        settings = calibration_settings(target, info)
        label = "채팅 입력칸" if target == "input" else "전송 버튼"
        return settings, (f"{label} 위치를 저장했습니다: '{info['title'] or '(제목 없음)'}' 창"
                          f" ({info['process'] or '프로그램 미확인'})의 가로 {float(settings[f'gorilla.{target}_x']):.0%},"
                          f" 세로 {float(settings[f'gorilla.{target}_y']):.0%} 지점")

    # ── 입력·전송 ───────────────────────────────────────────────
    def _find_input(self, w):
        edits = [e for e in w.descendants(control_type="Edit") if e.is_visible()]
        if self.cfg.input_auto_id:
            edits = [e for e in edits if e.element_info.automation_id == self.cfg.input_auto_id]
        elif self.cfg.input_name:
            named = [e for e in edits if re.search(self.cfg.input_name, e.element_info.name or "")]
            edits = named or edits  # 이름이 안 보이면 보이는 입력칸 중 마지막
        if not edits:
            raise GorillaError("고릴라 창에서 채팅 입력칸을 찾지 못했습니다. '입력칸 위치 지정' 방식을 쓰세요.")
        return edits[-1]  # 채팅 입력칸은 보통 화면 아래쪽 마지막 입력칸

    @staticmethod
    def _value(edit) -> str | None:
        for getter in ("get_value", "window_text"):
            try:
                return getattr(edit, getter)() or ""
            except Exception:
                continue
        return None

    @staticmethod
    def _clipboard(text: str | None = None) -> str | None:
        """text 가 있으면 클립보드에 넣고, 없으면 현재 클립보드 글자를 돌려준다."""
        import win32clipboard
        import win32con

        win32clipboard.OpenClipboard()
        try:
            if text is None:
                try:
                    return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                except Exception:
                    return None
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
            return text
        finally:
            win32clipboard.CloseClipboard()

    def _paste(self, text: str) -> None:
        from pywinauto.keyboard import send_keys

        self._clipboard(text)
        send_keys("^a{BACKSPACE}^v", pause=0.05)

    def _put_text(self, w, text: str):
        """입력칸에 글자를 넣고 입력칸 객체(uia) 또는 None(coords)을 돌려준다.
        앱이 키 입력으로 글자를 인식하도록 실제 클릭 후 붙여넣기를 먼저 쓴다."""
        from pywinauto import mouse

        if self.cfg.input_mode == "coords":
            if self.cfg.input_x is None or self.cfg.input_y is None:
                raise GorillaError("입력칸 위치가 지정되지 않았습니다. '입력칸 위치 지정'을 하세요.")
            mouse.click(coords=fraction_to_point(self._rect(w), self.cfg.input_x, self.cfg.input_y))
            time.sleep(0.2)
            self._paste(text)
            return None
        edit = self._find_input(w)
        edit.click_input()
        time.sleep(0.2)
        self._paste(text)
        time.sleep(0.2)
        if text not in (self._value(edit) or ""):
            try:
                edit.set_edit_text(text)
            except Exception:
                pass
        if text not in (self._value(edit) or ""):
            raise GorillaError("입력칸에 글자를 넣지 못했습니다. '입력칸 위치 지정' 방식을 써 보세요.")
        return edit

    def _press_send(self, w) -> None:
        from pywinauto import mouse
        from pywinauto.keyboard import send_keys

        mode = self.cfg.send_mode or "auto"
        if mode == "coords":
            if self.cfg.send_x is None or self.cfg.send_y is None:
                raise GorillaError("전송 버튼 위치가 지정되지 않았습니다.")
            mouse.click(coords=fraction_to_point(self._rect(w), self.cfg.send_x, self.cfg.send_y))
            return
        if mode in ("button", "auto"):
            pattern = re.compile(self.cfg.send_button_name or SEND_BUTTON_HINT.pattern)
            buttons = [b for b in w.descendants(control_type="Button")
                       if pattern.search(b.element_info.name or "") and b.is_visible()]
            if buttons:
                buttons[-1].click_input()
                return
            if mode == "button":
                raise GorillaError("전송 버튼을 찾지 못했습니다.")
        send_keys("{ENTER}")

    def _seen_in_chat(self, w, text: str) -> bool:
        try:
            for t in w.descendants(control_type="Text")[-60:]:
                if text in (t.element_info.name or ""):
                    return True
        except Exception:
            pass
        return False

    def _with_focus(self, fn):
        import ctypes

        user32 = ctypes.windll.user32
        previous = user32.GetForegroundWindow()
        try:
            saved_clip = self._clipboard()
        except Exception:
            saved_clip = None
        w = self._window()
        try:
            if w.is_minimized():
                w.restore()
            w.set_focus()
            time.sleep(0.3)
            return fn(w)
        finally:
            if saved_clip is not None:
                try:
                    self._clipboard(saved_clip)
                except Exception:
                    pass
            if previous:
                user32.SetForegroundWindow(previous)

    def type_only(self, text: str) -> str:
        """입력칸에 글자만 넣고 보내지 않는다 (설정 확인용)."""
        def run(w):
            self._put_text(w, text)
            return f"'{w.window_text()}' 창 입력칸에 글자를 넣었습니다. 보내지 않았으니 고릴라에서 직접 지워 주세요."
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
