"""방송사 앱 채팅창에 입력·전송 (윈도우 화면 요소 자동화, pywinauto).

SBS 고릴라(공감로그)·MBC mini·KBS 콩 모두 같은 방식이다. 설정은 앱마다 따로 저장한다 (gorilla.* / mini.* / kong.*).

창 찾기
  브라우저·이 도우미 화면처럼 제목에 '고릴라'가 들어갈 수 있는 창은 빼고,
  '공감로그 글쓰기' 입력칸과 '전송' 버튼이 있는 창을 고릴라 채팅창으로 본다.
  '자동 찾기'를 한 번 하면 그 창의 프로그램(실행 파일) 이름을 저장해 이후에는 그 프로그램 창만 본다.

입력 방법
  uia    : 화면 요소(Edit 입력칸)를 찾아 클릭한 뒤 붙여넣는다. 전송 후 입력칸이 비었는지로 전송 여부를 확인한다.
  coords : '위치 지정'으로 저장한 입력칸·전송 버튼 위치(창 크기 대비 비율)를 클릭한다. 결과 확인은 못 한다.

전송 중에는 고릴라 창이 잠깐 맨 앞으로 나온다. 끝나면 원래 쓰던 창과 클립보드를 되돌린다.

엉뚱한 곳을 누르지 않게 (라디오가 멈추거나 다른 프로그램에 키가 들어가는 것을 막음)
  - 누를 자리를 다른 창(자막 창·플레이어 창·다른 프로그램)이 가리고 있으면 누르지 않는다.
  - 위치 클릭 방식은 창 크기·자리가 위치를 지정할 때와 많이 다르면 누르지 않는다.
  - 지우기·붙여넣기·Enter 키는 그 창이 맨 앞에 있을 때만 보낸다.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

from .config import GorillaConfig, localize

# 제목에 '고릴라'가 들어갈 수 있지만 고릴라 앱이 아닌 창
OWN_TITLE = "라디오 참여 도우미"
CAPTION_TITLE = "라디오 자막 · 라디오 참여 도우미"   # 항상 위에 뜨는 자막 창 (captions.py)
EXCLUDED_PROCESSES = {
    "msedge.exe", "chrome.exe", "firefox.exe", "whale.exe", "opera.exe", "brave.exe", "vivaldi.exe",
    "iexplore.exe", "arc.exe", "aside.exe", "python.exe", "pythonw.exe", "explorer.exe", "cmd.exe",
    "conhost.exe", "windowsterminal.exe", "claude.exe", "code.exe", "notepad.exe", "kakaotalk.exe",
}
TITLE_HINT = re.compile(r"고릴라|gorealra|공감로그|\bmini\b|미니|\bkong\b|콩", re.I)
PROCESS_HINT = re.compile(r"gorealra|gorilla|고릴라|mbcmini|\bmini|kong", re.I)
CHAT_INPUT_HINT = re.compile(r"공감로그|글쓰기")
SEND_BUTTON_HINT = re.compile(r"^\s*(전송|보내기|등록)\s*$")
LAYOUT_TOLERANCE = 0.35  # 창 크기가 위치 지정 때보다 이 비율(그리고 LAYOUT_MIN_PX)보다 많이 달라지면 위치 클릭을 하지 않는다
LAYOUT_MIN_PX = 40       # (그보다 작은 변화는 가장자리에 붙은 입력칸·전송 버튼을 따라가 누른다 — anchored_point)
EDGE_ZONE = 0.25         # 창 가장자리에서 이 비율 안쪽에 지정한 위치는 그 가장자리에 붙은 것으로 본다
FRONT_WAIT_SECONDS = 1.0  # 클릭한 창이 맨 앞으로 올 때까지 기다리는 시간


@dataclass
class SendResult:
    status: str   # posted / entered / unknown / failed
    detail: str
    shots: list = field(default_factory=list)   # 증거 사진 [(설명, JPEG 바이트)]


class GorillaError(RuntimeError):
    pass


# ── 순수 계산 (테스트 가능) ─────────────────────────────────────────
def fraction_to_point(rect: tuple[int, int, int, int], fx: float, fy: float) -> tuple[int, int]:
    left, top, right, bottom = rect
    return int(left + (right - left) * fx), int(top + (bottom - top) * fy)


def anchored_point(rect: tuple[int, int, int, int], fx: float, fy: float, saved_size: str | None) -> tuple[int, int]:
    """저장한 비율 위치를 지금 창 크기에 맞춘다. 채팅 입력줄·전송 버튼은 보통 창 아래·오른쪽에 붙어 있어서,
    창 크기가 바뀌면 비율이 아니라 그 가장자리에서의 거리가 그대로다. 그래서 위치를 지정할 때 창 크기를 알면
    가장자리 가까이(바깥 25%) 지정한 것은 그 가장자리에서의 픽셀 거리를 지키고, 가운데 것은 비율로 옮긴다."""
    left, top, right, bottom = rect
    try:
        sw, sh = (int(float(v)) for v in (saved_size or "").split(","))
    except ValueError:
        sw = sh = 0

    def axis(frac: float, size: int, saved: int) -> float:
        if saved <= 0:
            return size * frac
        if frac >= 1 - EDGE_ZONE:
            return size - (1 - frac) * saved
        if frac <= EDGE_ZONE:
            return frac * saved
        return size * frac

    return int(left + axis(fx, right - left, sw)), int(top + axis(fy, bottom - top, sh))


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
    if mode in ("coords", "screen"):
        return SendResult("entered", "위치 클릭 방식이라 채팅에 올라갔는지는 확인하지 못함")
    if input_after is not None and text not in input_after:
        return SendResult("entered", "입력칸이 비워져 전송된 것으로 보임 (채팅 목록에서는 확인 못 함)")
    return SendResult("unknown", "전송 후에도 입력칸에 글자가 남아 있거나 확인할 수 없음")


def size_changed(saved: str | None, rect: tuple[int, int, int, int]) -> str | None:
    """위치를 지정할 때 잰 창 크기("너비,높이")와 지금 크기가 크게(35% 넘게) 다르면 안내 문구. 같거나 모르면 None.
    그보다 작은 변화는 anchored_point 가 가장자리에 붙은 입력칸·전송 버튼을 따라가 누른다."""
    try:
        sw, sh = (int(float(v)) for v in (saved or "").split(","))
    except ValueError:
        return None
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    if abs(w - sw) > max(LAYOUT_MIN_PX, sw * LAYOUT_TOLERANCE) or \
            abs(h - sh) > max(LAYOUT_MIN_PX, sh * LAYOUT_TOLERANCE):
        return f"창 크기가 위치를 지정할 때({sw}×{sh})와 크게 다릅니다(지금 {w}×{h}, 화면 모양이 바뀌었을 수 있음)"
    return None


def window_moved(saved: str | None, rect: tuple[int, int, int, int]) -> str | None:
    """영역을 지정할 때 그 자리에 있던 창("왼,위,오른,아래")이 옮겨졌거나 크기가 바뀌었으면 안내 문구."""
    before = parse_rect(saved)
    if not before:
        return None
    if any(abs(a - b) > LAYOUT_MIN_PX for a, b in zip(before, rect)):
        return "창이 영역을 지정할 때와 다른 자리·크기에 있습니다"
    return None


def covered_by(info: dict, handle: int | None) -> str | None:
    """누를 자리(window_at_point 결과)에 대상 창이 아닌 다른 창이 있으면 그 창 설명. 대상 창이면 None."""
    if not handle or info.get("hwnd") == handle:
        return None
    return f"'{(info.get('title') or '제목 없음')[:30]}'({info.get('process') or '프로그램 미확인'})"


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


def _dpi_aware() -> None:
    """클릭(pywinauto)과 같은 물리 화면 좌표를 쓰도록 DPI 인식을 켠다."""
    import importlib

    importlib.import_module("pywinauto")
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def _user32():
    """창 번호(HWND)가 잘리지 않도록 형식을 지정한 user32."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    return user32


def window_at_point(x: int, y: int) -> dict:
    """화면 좌표 (x, y)에 있는 최상위 창 정보 (화면 요소가 안 보이는 앱도 됨)."""
    from ctypes import wintypes

    _dpi_aware()  # 클릭(pywinauto)과 같은 물리 좌표로 본다
    user32 = _user32()
    hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
    root = user32.GetAncestor(hwnd, 2) if hwnd else None  # GA_ROOT
    if not root:
        raise GorillaError("그 위치에서 창을 찾지 못했습니다.")
    info = window_info(root)
    info["point"] = (x, y)
    return info


def window_info(root: int) -> dict:
    """최상위 창 번호 → 제목·프로그램 이름·화면 좌표."""
    import ctypes
    from ctypes import wintypes

    user32 = _user32()
    h = wintypes.HWND(root)
    rect = wintypes.RECT()
    user32.GetWindowRect(h, ctypes.byref(rect))
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(h, buf, 512)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
    return {"hwnd": root, "title": buf.value, "process": process_name(pid.value),
            "rect": (rect.left, rect.top, rect.right, rect.bottom)}


def app_window_at(x: int, y: int, process: str) -> int | None:
    """모니터 좌표 (x, y)를 덮고 있는 그 프로그램의 보이는 최상위 창 중 맨 위의 것. 다른 창에 가려져 있어도 찾는다
    (앞으로 가져온 뒤 누르기 위해). 없으면 None."""
    import ctypes
    from ctypes import wintypes

    user32 = _user32()
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        if r.left <= x < r.right and r.top <= y < r.bottom:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if process_name(pid.value) == process:
                found.append(hwnd)
                return False
        return True

    user32.EnumWindows(each, 0)   # 위에 있는 창부터 차례로
    return found[0] if found else None


def foreground_root() -> int:
    """지금 맨 앞(키보드 입력을 받는) 최상위 창 번호. 없으면 0."""
    user32 = _user32()
    fg = user32.GetForegroundWindow()
    return (user32.GetAncestor(fg, 2) or fg) if fg else 0  # GA_ROOT


def hide_caption_window() -> list[int]:
    """보내는 동안 이 도우미의 자막 창(항상 위)을 잠깐 숨긴다. 채팅 앱의 입력칸·전송 버튼을 가리면
    오클릭 방지로 보내지 못하기 때문이다. 숨긴 창 번호들."""
    try:
        user32 = _user32()
        hwnd = user32.FindWindowW(None, CAPTION_TITLE)
        if hwnd and user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, 0)   # SW_HIDE
            return [hwnd]
    except Exception:
        pass
    return []


def show_windows(hwnds: list[int]) -> None:
    for hwnd in hwnds:
        try:
            _user32().ShowWindow(hwnd, 4)   # SW_SHOWNOACTIVATE: 다시 보이되 지금 쓰는 창을 빼앗지 않음
        except Exception:
            pass


def bring_to_front(w) -> None:
    """set_focus 로 앞으로 오지 않는 창을 윈도우 방식으로 한 번 더 앞으로 가져온다."""
    handle = getattr(w, "handle", None)
    if not handle or foreground_root() == handle:
        return
    try:
        from pywinauto.controls.hwndwrapper import HwndWrapper

        HwndWrapper(handle).set_focus()
    except Exception:
        pass


def window_under_cursor() -> dict:
    import ctypes
    from ctypes import wintypes

    _dpi_aware()
    pt = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return window_at_point(pt.x, pt.y)


def select_rectangle(prompt: str, timeout_s: int = 90) -> tuple[int, int, int, int] | None:
    """화면을 어둡게 덮고 마우스로 네모를 끌게 한다. 손을 떼면 바로 저장. 취소는 Esc 또는 오른쪽 클릭."""
    import ctypes
    import tkinter as tk

    _dpi_aware()
    user32 = ctypes.windll.user32
    vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)   # 가상 화면(여러 모니터) 왼쪽 위
    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    state = {"start": None, "box": None, "result": None}

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.35)
    root.geometry(f"{vw}x{vh}+{vx}+{vy}")
    canvas = tk.Canvas(root, cursor="crosshair", bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_text(vw // 2, 60, fill="white", font=("Malgun Gothic", 20, "bold"), justify="center",
                       text=f"{prompt}\n마우스로 끌어서 네모를 그리면 바로 저장됩니다 · 취소: Esc 또는 오른쪽 클릭")

    def press(e):
        state["start"] = (e.x, e.y)
        if state["box"]:
            canvas.delete(state["box"])
        state["box"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#ffeb3b", width=4)

    def drag(e):
        if state["start"]:
            x0, y0 = state["start"]
            canvas.coords(state["box"], x0, y0, e.x, e.y)

    def release(e):
        if not state["start"]:
            return
        x0, y0 = state["start"]
        l, r = sorted((x0, e.x))
        t, b = sorted((y0, e.y))
        if r - l >= 4 and b - t >= 4:
            state["result"] = (l + vx, t + vy, r + vx, b + vy)
            root.destroy()

    def cancel(_e=None):
        root.destroy()

    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<B1-Motion>", drag)
    canvas.bind("<ButtonRelease-1>", release)
    canvas.bind("<ButtonPress-3>", cancel)
    root.bind("<Escape>", cancel)
    root.after(timeout_s * 1000, cancel)
    root.focus_force()
    root.mainloop()
    return state["result"]


def capture(rect: tuple[int, int, int, int], name: str, boxes=(), points=()) -> str | None:
    """화면의 rect 영역을 찍어 data/inspect/<name>.png 로 저장한다. boxes·points 는 화면 좌표로 표시.
    Pillow 가 없으면 건너뛴다."""
    try:
        from PIL import ImageDraw, ImageGrab
    except ImportError:
        return None
    from .. import db

    left, top, right, bottom = (int(v) for v in rect)
    img = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).convert("RGB")
    draw = ImageDraw.Draw(img)
    for (bl, bt, br, bb), color in boxes:
        draw.rectangle((bl - left, bt - top, br - left, bb - top), outline=color, width=4)
    for (x, y), color in points:
        cx, cy = x - left, y - top
        draw.ellipse((cx - 12, cy - 12, cx + 12, cy + 12), outline=color, width=4)
        draw.line((cx - 18, cy, cx + 18, cy), fill=color, width=2)
        draw.line((cx, cy - 18, cx, cy + 18), fill=color, width=2)
    out_dir = db.data_dir() / "inspect"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{name}.png"
    img.save(path)
    return path.name


def window_image(hwnd: int):
    """창 하나를 찍는다 (다른 창에 가려져 있어도 됨, 최소화는 안 됨). 실패하면 None. Windows 전용."""
    import ctypes
    from ctypes import wintypes

    from PIL import Image

    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    if user32.IsIconic(hwnd):
        return None
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return None

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    win_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(win_dc)
    bmp = gdi32.CreateCompatibleBitmap(win_dc, w, h)
    old = gdi32.SelectObject(mem_dc, bmp)
    try:
        if not user32.PrintWindow(hwnd, mem_dc, 2):  # PW_RENDERFULLCONTENT: 크롬 기반 앱도 그려 준다
            return None
        header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(header), 0)
        img = Image.frombuffer("RGB", (w, h), buf.raw, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, win_dc)
    lo, hi = img.convert("L").getextrema()
    return None if hi - lo < 8 else img   # 새까맣거나 한 색이면 못 찍은 것


def to_jpeg(img, max_side: int = 900) -> bytes:
    """분석용으로 줄여서 JPEG 로 (이미지 토큰 ≈ 가로×세로/750)."""
    import io

    img = img.convert("RGB")
    scale = min(1.0, max_side / max(img.size))
    if scale < 1.0:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=80)
    return out.getvalue()


def parse_rect(text: str | None) -> tuple[float, ...] | None:
    try:
        parts = tuple(float(v) for v in (text or "").split(","))
    except ValueError:
        return None
    return parts if len(parts) == 4 else None


def rect_fraction(ref: tuple[int, int, int, int], rect: tuple[int, int, int, int]) -> tuple[float, float, float, float]:
    left, top, right, bottom = ref
    w, h = max(1, right - left), max(1, bottom - top)
    return tuple(round(v, 4) for v in ((rect[0] - left) / w, (rect[1] - top) / h,
                                        (rect[2] - left) / w, (rect[3] - top) / h))


def region_settings(target: str, rect: tuple[int, int, int, int], info: dict,
                    screen_region: tuple[int, ...] | None) -> tuple[dict, str]:
    """끌어서 그린 네모 → 저장할 설정 (순수 계산).
    target: window(고릴라 창 전체 영역) / input(입력칸) / send(전송 버튼) / chat(채팅 목록, 읽기 전용)
    info: 네모 가운데에 있는 창 정보"""
    l, t, r, b = rect
    if r - l < 4 or b - t < 4:
        raise GorillaError("네모가 너무 작습니다. 다시 그려 주세요.")
    win_ok = not is_excluded(info["title"], info["process"])
    settings: dict[str, str] = {}
    if win_ok and info["process"]:
        settings["gorilla.process_name"] = info["process"]
    if target == "window":
        settings.update({
            "gorilla.screen_region": f"{l},{t},{r},{b}",
            # 누르기 전에 고릴라 창이 그 자리 그대로인지 확인하려고 지금 창 자리를 같이 저장한다
            "gorilla.region_window": ",".join(str(v) for v in info["rect"]) if win_ok else "",
            "gorilla.input_mode": "coords", "gorilla.send_mode": "coords",
            # 기준이 바뀌었으니 예전 입력칸·버튼 위치는 지운다
            "gorilla.input_x": "", "gorilla.input_y": "", "gorilla.input_rect": "",
            "gorilla.send_x": "", "gorilla.send_y": "", "gorilla.send_rect": "",
            "gorilla.input_point": "", "gorilla.send_point": "", "gorilla.point_window": "",
        })
        if win_ok:
            settings["gorilla.window_title"] = re.escape(info["title"]) if info["title"] else ""
        return settings, (f"고릴라 창 영역을 저장했습니다 ({r - l}×{b - t}). 이제 입력칸과 전송 버튼 영역을 지정하세요. "
                          "이 방식은 고릴라 창을 옮기면 영역을 다시 지정해야 합니다.")
    if screen_region:
        ref = tuple(int(v) for v in screen_region)
        basis = "저장한 고릴라 창 영역"
    else:
        if not win_ok:
            raise GorillaError(f"네모 가운데가 고릴라가 아닌 창('{info['title'][:40]}', {info['process']}) 위에 있습니다. "
                               "고릴라 창 위에 그려 주세요.")
        ref = info["rect"]
        wl, wt, wr, wb = ref
        settings.update({"gorilla.window_title": re.escape(info["title"]) if info["title"] else "",
                         "gorilla.window_size": f"{wr - wl},{wb - wt}"})
        basis = f"'{info['title'] or '(제목 없음)'}' 창"
    fx1, fy1, fx2, fy2 = rect_fraction(ref, rect)
    cx, cy, _, _ = rect_fraction(ref, ((l + r) / 2, (t + b) / 2, r, b))
    if not (0 <= cx <= 1 and 0 <= cy <= 1):
        raise GorillaError("네모가 고릴라 창(영역) 밖에 있습니다.")
    if target == "chat":
        # 읽기만 하는 영역: 누를 위치나 입력 방식은 바꾸지 않는다. 모니터 좌표도 같이 저장 (채팅이 따로 뜨는 앱)
        settings["gorilla.chat_rect"] = f"{fx1},{fy1},{fx2},{fy2}"
        settings["gorilla.chat_screen"] = f"{l},{t},{r},{b}"
        return settings, (f"채팅창 영역을 저장했습니다: {basis}의 {fx2 - fx1:.0%}×{fy2 - fy1:.0%}. "
                          "퀴즈·사연 키워드가 들리면 이 부분에 올라오는 글도 함께 읽어 분석합니다.")
    if not win_ok:
        raise GorillaError(f"네모 가운데가 고릴라가 아닌 창('{info['title'][:40]}', {info['process']}) 위에 있습니다. "
                           "고릴라 채팅 창의 그 자리에 그려 주세요.")
    px, py = (l + r) // 2, (t + b) // 2
    settings.update({
        f"gorilla.{target}_rect": f"{fx1},{fy1},{fx2},{fy2}",
        f"gorilla.{target}_x": str(cx), f"gorilla.{target}_y": str(cy),
        # 모니터 기준: 채팅 창이 플레이어 옆에 따로 뜨는 앱도 그 자리를 그대로 누른다
        f"gorilla.{target}_point": f"{px},{py}",
        "gorilla.point_window": ",".join(str(v) for v in info["rect"]),
        "gorilla.input_mode" if target == "input" else "gorilla.send_mode": "screen",
    })
    label = "입력칸" if target == "input" else "전송 버튼"
    return settings, (f"{label} 영역을 저장했습니다: 모니터 화면의 ({px}, {py}) 지점을 그대로 누릅니다 "
                      f"('{info['title'] or '(제목 없음)'}' 창). 고릴라 창을 옮기면 다시 지정하세요.")


def calibration_settings(target: str, info: dict) -> dict:
    """마우스 위치로 잰 창 정보 → 저장할 설정 (순수 계산)."""
    if is_excluded(info["title"], info["process"]):
        raise GorillaError(f"마우스가 고릴라가 아닌 창('{info['title'][:40]}', {info['process']}) 위에 있습니다. "
                           "고릴라 창의 해당 위치에 마우스를 올려 두세요.")
    fx, fy = point_to_fraction(info["rect"], *info["point"])
    left, top, right, bottom = info["rect"]
    settings = {
        f"gorilla.{target}_x": str(fx), f"gorilla.{target}_y": str(fy),
        f"gorilla.{target}_point": "{},{}".format(*info["point"]),         # 모니터 기준 (그 자리를 그대로 누름)
        "gorilla.point_window": ",".join(str(v) for v in info["rect"]),
        "gorilla.input_mode" if target == "input" else "gorilla.send_mode": "screen",
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
            if len(cands) > 1 and self.cfg.window_title and self.cfg.window_title != self.cfg.default_title:
                titled = [x for x in cands if re.search(self.cfg.window_title, x[1] or "", re.I)]
                cands = titled or cands
            return cands
        pattern = re.compile(self.cfg.window_title or self.cfg.default_title, re.I)
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
                    left, top, right, bottom = self._frame(x[0])
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
            if self._screen_mode():
                # 모니터 기준이면 보낼 때와 똑같이 그 자리의 창으로 본다 (창 목록 검색은 채팅이 따로 뜨는 앱을 못 찾을 수 있음)
                point = self._screen_point("input") or self._screen_point("send")
                if self._is_app(window_at_point(*point)):
                    return True
                proc = (self.cfg.process_name or "").lower()
                return bool(proc and app_window_at(*point, proc))
            if self._region() is not None:
                return self._region_window_info() is not None
            return bool(self._candidates())
        except Exception:
            return False

    def _region(self) -> tuple[int, int, int, int] | None:
        r = parse_rect(self.cfg.screen_region)
        return tuple(int(v) for v in r) if r else None

    def _region_window_info(self) -> dict | None:
        region = self._region()
        if region is None:
            return None
        info = window_at_point((region[0] + region[2]) // 2, (region[1] + region[3]) // 2)
        if is_excluded(info["title"], info["process"]) or (
                self.cfg.process_name and info["process"] and info["process"] != self.cfg.process_name.lower()):
            raise GorillaError("저장한 고릴라 창 영역에 다른 창이 있습니다. 고릴라 창을 그 자리에 두거나 영역을 다시 지정하세요.")
        return info

    def _screen_point(self, target: str) -> tuple[int, int] | None:
        """모니터 기준으로 저장한 자리 (그 방식일 때만)."""
        mode = self.cfg.input_mode if target == "input" else self.cfg.send_mode
        raw = self.cfg.input_point if target == "input" else self.cfg.send_point
        if mode != "screen":
            return None
        try:
            x, y = (int(float(v)) for v in (raw or "").split(","))
        except ValueError:
            return None
        return x, y

    def _is_app(self, info: dict) -> bool:
        proc = (self.cfg.process_name or "").lower()
        return not is_excluded(info["title"], info["process"]) and (not proc or not info["process"]
                                                                    or info["process"] == proc)

    def _window_at_point(self, point: tuple[int, int], label: str):
        """모니터 기준: 그 자리의 채팅 창. 다른 창이 덮고 있으면 그 자리를 포함하는 앱 창을 찾아 앞으로 가져온다."""
        from pywinauto import Desktop

        info = window_at_point(*point)
        if self._is_app(info):
            hwnd = info["hwnd"]
        else:
            proc = (self.cfg.process_name or "").lower()
            hwnd = app_window_at(*point, proc) if proc else None
            if not hwnd:
                raise GorillaError(f"{label} 자리(모니터 {point[0]}, {point[1]})에 {self.cfg.label} 창이 없습니다 "
                                   f"(지금 그 자리: '{(info['title'] or '제목 없음')[:30]}', {info['process'] or '?'}). "
                                   f"{self.cfg.label} 채팅 창을 지정할 때 자리에 두거나 ①② 위치를 다시 지정하세요. 최소화하지 마세요.")
        return Desktop(backend="uia").window(handle=hwnd).wrapper_object()

    def _window(self):
        point = self._screen_point("input") or self._screen_point("send")
        if point is not None:
            return self._window_at_point(point, "입력칸" if self._screen_point("input") else "전송 버튼")
        if self._region() is not None:
            from pywinauto import Desktop

            info = self._region_window_info()
            return Desktop(backend="uia").window(handle=info["hwnd"]).wrapper_object()
        w = self.find_window()
        if w is None:
            raise GorillaError("고릴라 창을 찾지 못했습니다. 고릴라 PC 앱을 켜고 '자동 찾기'를 다시 하세요.")
        return w

    @staticmethod
    def _rect(w) -> tuple[int, int, int, int]:
        r = w.rectangle()
        return r.left, r.top, r.right, r.bottom

    def _frame(self, w) -> tuple[int, int, int, int]:
        """지금 창의 화면 좌표. 위치를 지정할 때(window_at_point)와 같은 방법(GetWindowRect)으로 재야
        저장한 비율·크기와 맞는다 (화면 요소 정보로 재면 앱에 따라 크기가 다르게 나옴)."""
        handle = getattr(w, "handle", None)
        if handle:
            try:
                return tuple(window_info(handle)["rect"])
            except Exception:
                pass
        return self._rect(w)

    def _screen_mode(self) -> bool:
        return self._screen_point("input") is not None or self._screen_point("send") is not None

    def _ref_rect(self, w) -> tuple[int, int, int, int]:
        """사진·위치 비율의 기준: 모니터 기준이면 그 자리의 채팅 창, 지정한 화면 영역이 있으면 그 영역, 없으면 고릴라 창."""
        if self._screen_mode():
            return self._frame(w)
        return self._region() or self._frame(w)

    def _has_point(self, target: str) -> bool:
        if (self.cfg.input_mode if target == "input" else self.cfg.send_mode) == "screen":
            return self._screen_point(target) is not None
        fx, fy = (self.cfg.input_x, self.cfg.input_y) if target == "input" else (self.cfg.send_x, self.cfg.send_y)
        return fx is not None and fy is not None

    def _target_point(self, w, target: str) -> tuple[int, int]:
        """입력칸(input)·전송 버튼(send)을 누를 화면 좌표: 모니터 기준이면 저장한 자리 그대로, 아니면 창 안 비율."""
        point = self._screen_point(target)
        if point is not None:
            return point
        if target == "input":
            return self._point(w, self.cfg.input_x, self.cfg.input_y)
        return self._point(w, self.cfg.send_x, self.cfg.send_y)

    def _point(self, w, fx: float, fy: float) -> tuple[int, int]:
        """저장한 위치(비율) → 지금 누를 화면 좌표. 창 기준이면 가장자리에 붙은 입력칸·전송 버튼을 따라간다."""
        region = self._region()
        if region is not None:
            return fraction_to_point(region, fx, fy)
        return anchored_point(self._frame(w), fx, fy, self.cfg.window_size)

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

    def select(self, target: str) -> tuple[dict, str] | None:
        prompts = {"window": "고릴라 창 전체를 네모로 감싸 주세요",
                   "input": "고릴라의 채팅 입력칸('공감로그 글쓰기' 등)을 네모로 그려 주세요",
                   "send": "고릴라의 '전송' 버튼을 네모로 그려 주세요",
                   "chat": "고릴라의 채팅 목록(다른 청취자 글이 올라오는 곳)을 네모로 크게 감싸 주세요"}
        rect = select_rectangle(localize(self.cfg.app, prompts[target]))
        if rect is None:
            return None
        time.sleep(0.4)  # 어둡게 덮은 창이 완전히 사라진 뒤 아래 창을 본다
        info = window_at_point((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
        settings, message = region_settings(target, rect, info, None if target == "window" else self._region())
        ref = self._region() if target != "window" and self._region() else info["rect"]
        if target == "window":
            ref = rect
        color = {"input": "#2554c7", "send": "#18794e", "window": "#f59e0b", "chat": "#a855f7"}[target]
        try:
            shot = capture(ref, f"{self.cfg.app}_select_{target}", boxes=[(rect, color)])
            if shot:
                message += f" (저장한 부분 사진: {shot})"
        except Exception:
            pass
        return settings, message

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

    # ── 엉뚱한 곳을 누르지 않게 ─────────────────────────────────
    def _check_layout(self, w) -> None:
        """위치 클릭 전에: 창 크기·자리가 위치를 지정할 때와 같은지. 많이 다르면 저장한 비율 위치가
        재생·채널 버튼 같은 다른 곳을 가리킬 수 있으므로 누르지 않는다."""
        if self._screen_mode():
            problem = window_moved(self.cfg.point_window, self._frame(w))
        elif self._region() is not None:
            problem = window_moved(self.cfg.region_window, self._frame(w))
        else:
            problem = size_changed(self.cfg.window_size, self._frame(w))
        if problem:
            raise GorillaError(f"{self.cfg.label} {problem}. 엉뚱한 곳(재생·채널 버튼 등)을 누르지 않도록 보내지 "
                               "않았습니다. 창을 위치 지정 때처럼 두거나 위치(영역)를 다시 지정하세요.")

    def _check_point(self, w, point: tuple[int, int], label: str) -> None:
        """누를 자리에 정말 그 창이 있는지. 자막 창·플레이어 창·다른 프로그램이 가리고 있으면 누르지 않는다."""
        other = covered_by(window_at_point(*point), getattr(w, "handle", None))
        if other:
            raise GorillaError(f"{self.cfg.label} {label} 자리를 다른 창 {other}이 가리고 있어 누르지 않았습니다. "
                               "그 창을 옮기거나 위치를 다시 지정하세요.")

    def _check_front(self, w) -> None:
        """지우기·붙여넣기·Enter 키를 보내기 전에: 그 창이 맨 앞인지. 아니면 맨 앞의 다른 프로그램
        (편집 프로그램 등)에 키가 들어가므로 보내지 않는다."""
        handle = getattr(w, "handle", None)
        if not handle:
            return
        deadline = time.monotonic() + FRONT_WAIT_SECONDS
        while (front := foreground_root()) != handle:
            if time.monotonic() > deadline:
                try:
                    info = window_info(front) if front else {}
                    who = f" (맨 앞: {covered_by(info, handle)})" if info else ""
                except Exception:
                    who = ""
                raise GorillaError(f"{self.cfg.label} 창이 맨 앞으로 오지 않아 키 입력을 하지 않았습니다{who}. "
                                   "다른 프로그램에 글자·지우기 키가 들어가는 것을 막았습니다. 다른 창을 잠시 내려 두세요.")
            time.sleep(0.1)

    def _click(self, w, point: tuple[int, int], label: str) -> None:
        from pywinauto import mouse

        self._check_point(w, point, label)
        mouse.click(coords=point)

    @staticmethod
    def _center(control) -> tuple[int, int]:
        r = control.rectangle()
        return (r.left + r.right) // 2, (r.top + r.bottom) // 2

    def _paste(self, w, text: str) -> None:
        from pywinauto.keyboard import send_keys

        self._check_front(w)
        self._clipboard(text)
        send_keys("^a{BACKSPACE}^v", pause=0.05)

    def _put_text(self, w, text: str):
        """입력칸에 글자를 넣고 입력칸 객체(uia) 또는 None(coords)을 돌려준다.
        앱이 키 입력으로 글자를 인식하도록 실제 클릭 후 붙여넣기를 먼저 쓴다."""
        if self.cfg.input_mode in ("coords", "screen"):
            if not self._has_point("input"):
                raise GorillaError("입력칸 위치가 지정되지 않았습니다. '① 입력칸 영역 지정'을 하세요.")
            self._check_layout(w)
            self._click(w, self._target_point(w, "input"), "입력칸")
            time.sleep(0.2)
            self._paste(w, text)
            return None
        edit = self._find_input(w)
        self._check_point(w, self._center(edit), "입력칸")
        edit.click_input()
        time.sleep(0.2)
        self._paste(w, text)
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
        from pywinauto.keyboard import send_keys

        mode = self.cfg.send_mode or "auto"
        if mode in ("coords", "screen"):
            if not self._has_point("send"):
                raise GorillaError("전송 버튼 위치가 지정되지 않았습니다. '② 전송 버튼 영역 지정'을 하세요.")
            self._check_layout(w)
            self._click(w, self._target_point(w, "send"), "전송 버튼")
            return
        if mode in ("button", "auto"):
            pattern = re.compile(self.cfg.send_button_name or SEND_BUTTON_HINT.pattern)
            buttons = [b for b in w.descendants(control_type="Button")
                       if pattern.search(b.element_info.name or "") and b.is_visible()]
            if buttons:
                self._check_point(w, self._center(buttons[-1]), "전송 버튼")
                buttons[-1].click_input()
                return
            if mode == "button":
                raise GorillaError("전송 버튼을 찾지 못했습니다.")
        self._check_front(w)
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
        user32 = _user32()
        previous = user32.GetForegroundWindow()
        try:
            saved_clip = self._clipboard()
        except Exception:
            saved_clip = None
        hidden = hide_caption_window()   # 자막 창이 입력칸·전송 버튼을 가리지 않게 (끝나면 다시 보임)
        try:
            w = self._window()
        except Exception:
            show_windows(hidden)
            raise
        try:
            try:
                # 최소화돼 있으면 set_focus 가 원래 모양(최대화였으면 최대화)으로 되돌린다.
                # restore() 를 먼저 부르면 최대화가 풀려 창 크기가 바뀌므로 쓰지 않는다.
                w.set_focus()
            except Exception:
                pass  # 일부 앱은 포커스 요청을 거부한다 → 아래에서 한 번 더, 그래도 안 되면 입력칸을 직접 클릭
            bring_to_front(w)
            time.sleep(0.3)
            return fn(w)
        finally:
            show_windows(hidden)
            if saved_clip is not None:
                try:
                    self._clipboard(saved_clip)
                except Exception:
                    pass
            if previous:
                user32.SetForegroundWindow(previous)

    def click_points(self, w) -> list[tuple[tuple[int, int], str]]:
        points = []
        if self.cfg.input_mode in ("coords", "screen") and self._has_point("input"):
            points.append((self._target_point(w, "input"), "#ef4444"))
        if self.cfg.send_mode in ("coords", "screen") and self._has_point("send"):
            points.append((self._target_point(w, "send"), "#22c55e"))
        return points

    def chat_image(self, max_side: int = 900, save_as: str | None = None) -> bytes | None:
        """채팅 목록 영역을 찍어 JPEG 로 돌려준다 (읽기만 함, 창을 앞으로 가져오지 않음).
        영역을 지정하지 않았거나 창을 찾지 못하면 None."""
        screen = parse_rect(self.cfg.chat_screen)
        if screen:   # 모니터 기준으로 지정한 채팅 목록 (채팅이 플레이어 옆에 따로 뜨는 앱)
            area = tuple(int(v) for v in screen)
            w = self._window_at_point(((area[0] + area[2]) // 2, (area[1] + area[3]) // 2), "채팅 목록")
            by_window = True
        else:
            frac = parse_rect(self.cfg.chat_rect)
            if not frac:
                return None
            w = self._window()
            rl, rt, rr, rb = self._ref_rect(w)
            rw, rh = rr - rl, rb - rt
            area = (rl + int(rw * frac[0]), rt + int(rh * frac[1]), rl + int(rw * frac[2]), rt + int(rh * frac[3]))
            by_window = self._region() is None
        img = None
        if by_window:
            try:
                full = window_image(w.handle)   # 다른 창에 가려져 있어도 그 창만 찍힘
                if full is not None:
                    fl, ft, _fr, _fb = self._frame(w)
                    img = full.crop((area[0] - fl, area[1] - ft, area[2] - fl, area[3] - ft))
            except Exception:
                img = None
        if img is None:  # 창 사진을 못 찍는 앱이면 화면에서 그 부분을 찍는다 (가려져 있으면 가린 창이 찍힘)
            from PIL import ImageGrab

            img = ImageGrab.grab(bbox=area, all_screens=True)
        if save_as:
            from .. import db

            out_dir = db.data_dir() / "inspect"
            out_dir.mkdir(exist_ok=True)
            img.convert("RGB").save(out_dir / f"{save_as}.png")
        return to_jpeg(img, max_side)

    def send_test(self, text: str) -> tuple[SendResult, str]:
        """설정 확인용으로 실제로 한 번 보낸다. 누르기 직전·보낸 직후 창을 찍어 둔다. 오클릭 방지로 보내지 못하면
        그때 화면에 누를 자리(빨강·초록 원)를 표시해 찍는다. 사진은 결과의 shots 로도 돌려준다 (증거 사진)."""
        shots, notes = [], []

        def run(w):
            def snap(name: str, label: str, points=()) -> None:
                from .. import db

                try:
                    saved = capture(self._ref_rect(w), f"{self.cfg.app}_{name}", points=points)
                except Exception as e:
                    notes.append(f"사진 실패: {type(e).__name__}")
                    return
                if saved:
                    notes.append(f"{label} 사진 {saved}")
                    shots.append((label, (db.data_dir() / "inspect" / saved).read_bytes()))

            try:
                edit = self._put_text(w, text)
                time.sleep(0.5)
                snap("test", "누르기 직전 (빨간 원 = 입력칸으로 누른 곳, 초록 원 = 전송 버튼)", self.click_points(w))
                self._press_send(w)
                time.sleep(1.5)
                after = self._value(edit) if edit is not None else None
                result = judge_result(self.cfg.input_mode, after, text, self._seen_in_chat(w, text))
                snap("test_sent", "보낸 뒤")
            except GorillaError:
                snap("test", "보내지 못했을 때 (빨간 원 = 입력칸 자리, 초록 원 = 전송 버튼 자리)", self.click_points(w))
                raise
            msg = (f"'{w.window_text() or '(제목 없음)'}' 창에 '{text}'를 입력하고 전송을 눌렀습니다 → {result.detail}. "
                   + " / ".join(notes))
            return result, msg
        try:
            result, msg = self._with_focus(run)
        except GorillaError as e:
            result, msg = SendResult("failed", str(e)), f"전송 테스트 실패: {e}" + "".join(f" / {n}" for n in notes)
        result.shots = shots
        return result, msg

    def evidence_image(self, w) -> bytes | None:
        """증거 사진: 지금 채팅 앱 창 (JPEG). 다른 창에 가려져도 찍히는 창 사진을 먼저, 안 되면 화면에서 그 영역."""
        try:
            img = window_image(w.handle) if self._region() is None or self._screen_mode() else None
            if img is None:
                from PIL import ImageGrab

                img = ImageGrab.grab(bbox=self._ref_rect(w), all_screens=True)
            return to_jpeg(img, max_side=1600)
        except Exception:
            return None

    def send(self, text: str) -> SendResult:
        shots = []

        def run(w):
            label = f"보내지 못했을 때 {self.cfg.label} 창"
            try:
                edit = self._put_text(w, text)
                self._press_send(w)
                time.sleep(1.5)
                after = self._value(edit) if edit is not None else None
                label = f"보낸 뒤 {self.cfg.label} 창"
                return judge_result(self.cfg.input_mode, after, text, self._seen_in_chat(w, text))
            finally:   # 창이 아직 앞에 있을 때 찍는다 (실패해도 그때 화면을 남김)
                shot = self.evidence_image(w)
                if shot:
                    shots.append((label, shot))
        try:
            result = self._with_focus(run)
        except GorillaError as e:
            result = SendResult("failed", str(e))
        result.shots = shots
        return result


def dump(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)
