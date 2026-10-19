"""원버튼 찾기 — 채팅 앱(SBS 고릴라·KBS 콩·MBC mini)이 켜져 있는지, 입력칸·전송 버튼·채팅 목록이 어디인지 한 번에 찾아 저장한다.

    python -m radio_helper.quizbot find --app gorilla      (관리 화면: 앱 채팅·인식 설정 → '원버튼 찾기')

1. 켜져 있는지: 보이는(최소화 포함) 최상위 창을 모두 보고 프로그램 이름·창 제목으로 그 앱을 고른다.
   브라우저·이 도우미 창은 뺀다. 예전에 저장한 프로그램 이름이 있으면 그것을 먼저 본다.
2. 그 앱의 창들(고릴라는 플레이어와 공감로그 창이 따로 뜬다)을 최소화에서 풀고 앞으로 가져와 한 장으로 찍는다.
3. 자리 찾기 — 앞의 방법으로 찾은 것은 그대로 쓰고, 못 찾은 것만 다음 방법으로:
   a. 화면 요소(UIA): 입력칸(Edit)·'전송/등록/보내기' 버튼이 보이면 그 자리 (가장 정확)
   b. Claude: 찍은 사진을 보고 입력칸·전송 버튼·채팅 목록 네모를 알려 준다 (Claude 연결이 있을 때)
   c. 알려진 배치: 세 앱의 화면(관리자가 보낸 사진)에서 잰 위치 (창 크기 대비 비율)
4. 채팅이 닫혀 있으면(고릴라 공감로그·콩 채팅) 여는 말풍선 버튼을 한 번 누르고 다시 찾는다.
5. 로그인 안내가 보이면 알려 준다 (로그인은 사용자가 직접 한다).
6. 모니터 기준 위치로 저장하고(보낼 때 그 자리를 누름), 네모를 그린 확인 사진(<app>_found.png)을 남긴다.

듣는 중에 채팅 창을 못 찾으면 실행기가 Claude 없이(화면 요소·알려진 배치) 조용히 다시 찾는다 (auto).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from . import config
from .gorilla import GorillaError, is_excluded, region_settings

TARGETS = ("input", "send", "chat")
COLORS = {"input": "#2554c7", "send": "#18794e", "chat": "#a855f7"}
MIN_WINDOW = 100          # 이보다 작은 창은 보지 않는다 (알림·숨은 도우미 창)
WIDE_WINDOW = 300         # 고릴라 공감로그 창은 이보다 넓다 (오른쪽 광고 띠는 좁음)
VISION_MAX_SIDE = 1400    # Claude 에 보내는 사진의 긴 변 (좌표를 정확히 받으려고 많이 줄이지 않음)
AUTO_EVERY_SECONDS = 300  # 듣는 중 다시 찾기는 5분에 한 번까지

APP_HINTS = {
    "gorilla": {"process": re.compile(r"gorealra|gorilla|고릴라|sbs", re.I),
                "title": re.compile(r"고릴라|gorealra|공감로그", re.I)},
    "kong": {"process": re.compile(r"kong|kbs", re.I), "title": re.compile(r"\bkong\b|콩|KBS", re.I)},
    "mini": {"process": re.compile(r"mbc|\bmini", re.I), "title": re.compile(r"MBC|\bmini\b|미니", re.I)},
}

# 알려진 배치 (창 크기 대비 비율 왼·위·오른·아래). 관리자가 보낸 화면에서 잼:
#   고릴라: 공감로그가 플레이어 오른쪽에 따로 뜬 창 (634×949). 닫혀 있으면 플레이어 오른쪽 아래 말풍선으로 연다.
#   콩: 한 창의 왼쪽 플레이어·오른쪽 채팅 (1271×842). 닫혀 있으면(세로로 긴 창) 오른쪽 위 초록 말풍선.
#   mini: 한 창의 왼쪽 플레이어·오른쪽 mini 메시지 (766×555).
TEMPLATES = {
    "gorilla": {"input": (0.14, 0.915, 0.79, 0.99), "send": (0.82, 0.915, 0.99, 0.99),
                "chat": (0.02, 0.20, 0.98, 0.90), "toggle": (0.895, 0.94), "combined_split": 0.533},
    "kong": {"input": (0.52, 0.83, 0.97, 0.91), "send": (0.905, 0.922, 0.978, 0.98),
             "chat": (0.51, 0.10, 0.99, 0.81), "toggle": (0.929, 0.154)},
    "mini": {"input": (0.53, 0.915, 0.91, 0.99), "send": (0.935, 0.92, 0.995, 0.99),
             "chat": (0.52, 0.19, 0.99, 0.89), "toggle": None},
}
OPEN_RATIO = 1.2          # 콩·mini: 가로/세로가 이보다 크면 채팅이 열린 넓은 창
SEND_NAME = re.compile(r"^\s*(전송|보내기|등록|로그인)\s*$")
LOGIN_HINT = re.compile(r"로그인")

Rect = tuple[int, int, int, int]


@dataclass
class Found:
    boxes: dict = field(default_factory=dict)      # target → 화면 네모
    how: dict = field(default_factory=dict)        # target → uia / claude / template
    chat_open: bool | None = None
    toggle: tuple[int, int] | None = None          # 채팅을 여는 버튼 (화면 좌표)
    login_required: bool = False
    notes: list = field(default_factory=list)
    looked: bool = False                           # Claude 가 사진을 실제로 보고 답함


# ── 순수 계산 ────────────────────────────────────────────────────────
def width(r) -> int:
    return int(r[2] - r[0])


def height(r) -> int:
    return int(r[3] - r[1])


def union(rects) -> Rect:
    rects = list(rects)
    return (min(r[0] for r in rects), min(r[1] for r in rects), max(r[2] for r in rects), max(r[3] for r in rects))


def scale_box(frac, ref) -> Rect:
    l, t = ref[0], ref[1]
    w, h = width(ref), height(ref)
    return (int(round(l + frac[0] * w)), int(round(t + frac[1] * h)), int(round(l + frac[2] * w)),
            int(round(t + frac[3] * h)))


def center(r) -> tuple[int, int]:
    return (int((r[0] + r[2]) // 2), int((r[1] + r[3]) // 2))


def contains(r, point) -> bool:
    return r[0] <= point[0] < r[2] and r[1] <= point[1] < r[3]


def score_app(app: str, win: dict, saved_process: str = "") -> int:
    """그 앱 창일 가능성 점수 (0 이하는 아님)."""
    title, process = win.get("title") or "", (win.get("process") or "").lower()
    if is_excluded(title, process):
        return -1
    hints = APP_HINTS[app]
    score = 0
    if saved_process and process == saved_process.lower():
        score += 6
    if hints["process"].search(process):
        score += 4
    if hints["title"].search(title):
        score += 3
    return score


def pick_app(app: str, windows: list[dict], saved_process: str = "") -> tuple[str, list[dict]]:
    """그 앱의 프로그램 이름과 창들 (왼쪽부터). 못 찾으면 ('', [])."""
    best: dict[str, int] = {}
    for w in windows:
        s = score_app(app, w, saved_process)
        proc = (w.get("process") or "").lower()
        if s >= 3 and proc:
            best[proc] = max(best.get(proc, 0), s)
    if not best:
        return "", []
    proc = max(best, key=lambda p: best[p])
    wins = [w for w in windows if (w.get("process") or "").lower() == proc]
    return proc, sorted(wins, key=lambda w: (w["rect"][0], w["rect"][1]))


def chat_window(app: str, wins: list[dict]) -> tuple[Rect | None, Rect | None]:
    """알려진 배치 기준: (채팅 부분의 화면 네모, 채팅이 닫혔을 때 여는 버튼이 있는 창 네모)."""
    shown = [w for w in wins if not w.get("iconic") and width(w["rect"]) >= MIN_WINDOW and height(w["rect"]) >= MIN_WINDOW]
    if not shown:
        return None, None
    if app == "gorilla":
        wide = [w for w in shown if width(w["rect"]) >= WIDE_WINDOW]
        if len(wide) >= 2:
            return max(wide, key=lambda w: w["rect"][0])["rect"], None   # 오른쪽에 따로 뜬 공감로그 창
        player = max(shown, key=lambda w: width(w["rect"]) * height(w["rect"]))["rect"]
        if width(player) > height(player) * OPEN_RATIO:      # 플레이어와 공감로그가 한 창
            split = player[0] + int(width(player) * TEMPLATES["gorilla"]["combined_split"])
            return (split, player[1], player[2], player[3]), None
        return None, player                                  # 공감로그가 닫혀 있음 (광고 띠만)
    main = max(shown, key=lambda w: width(w["rect"]) * height(w["rect"]))["rect"]
    if width(main) >= height(main) * OPEN_RATIO:
        return main, None
    return None, main


def template_find(app: str, wins: list[dict]) -> Found:
    """알려진 배치로 찾기 (순수 계산)."""
    t = TEMPLATES[app]
    chat, closed = chat_window(app, wins)
    found = Found()
    if chat is not None:
        found.chat_open = True
        for target in TARGETS:
            found.boxes[target] = scale_box(t[target], chat)
            found.how[target] = "template"
    elif closed is not None:
        found.chat_open = False
        if t.get("toggle"):
            fx, fy = t["toggle"]
            found.toggle = (int(closed[0] + fx * width(closed)), int(closed[1] + fy * height(closed)))
    return found


def valid_box(box, limit: tuple[int, int], min_size: int = 6) -> Rect | None:
    """Claude 가 준 네모 확인: 정수 4개, 사진 안, 너무 작지 않음."""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    try:
        l, t, r, b = (int(v) for v in box)
    except (TypeError, ValueError):
        return None
    w, h = limit
    l, t, r, b = max(0, l), max(0, t), min(w, r), min(h, b)
    if r - l < min_size or b - t < min_size:
        return None
    return l, t, r, b


def vision_find(data: dict, size: tuple[int, int], origin: tuple[int, int], scale: float) -> Found:
    """Claude 결과(줄인 사진 좌표) → 화면 좌표. 이상한 네모는 버린다."""
    found = Found(chat_open=bool(data.get("chat_open")), login_required=bool(data.get("login_required")), looked=True)

    def to_screen(box):
        return (int(origin[0] + box[0] / scale), int(origin[1] + box[1] / scale),
                int(origin[0] + box[2] / scale), int(origin[1] + box[3] / scale))

    for target, key in (("input", "input_box"), ("send", "send_box"), ("chat", "chat_box")):
        box = valid_box(data.get(key), size)
        if box:
            found.boxes[target] = to_screen(box)
            found.how[target] = "claude"
    inp, send = found.boxes.get("input"), found.boxes.get("send")
    if inp and send and (send == inp or width(send) > width(inp) * 3):
        found.boxes.pop("send")    # 입력칸과 같은 네모거나 입력칸보다 훨씬 넓으면 전송 버튼이 아님
        found.how.pop("send")
    chat = found.boxes.get("chat")
    if chat and inp and width(chat) * height(chat) < width(inp) * height(inp):
        found.boxes.pop("chat")    # 채팅 목록은 입력칸보다 넓다
        found.how.pop("chat")
    toggle = valid_box(data.get("toggle_box"), size, min_size=4)
    if toggle and not found.chat_open:
        found.toggle = center(to_screen(toggle))
    if data.get("note"):
        found.notes.append(f"Claude: {str(data['note'])[:120]}")
    return found


def merge(*founds: Found) -> Found:
    """앞의 결과를 우선해 합친다 (못 찾은 것만 뒤의 결과로)."""
    out = Found()
    for f in founds:
        if f is None:
            continue
        for target, box in f.boxes.items():
            if target not in out.boxes:
                out.boxes[target], out.how[target] = box, f.how.get(target, "?")
        if out.chat_open is None and f.chat_open is not None:
            out.chat_open = f.chat_open
        out.toggle = out.toggle or f.toggle
        out.login_required = out.login_required or f.login_required
        out.notes += f.notes
    if out.boxes.get("input") and out.boxes.get("send"):
        out.chat_open = True
    return out


def window_of(point, wins: list[dict]) -> dict | None:
    """그 자리를 덮는 앱 창 (가장 작은 것 — 플레이어 위에 뜬 공감로그 창 등)."""
    hits = [w for w in wins if not w.get("iconic") and contains(w["rect"], point)]
    return min(hits, key=lambda w: width(w["rect"]) * height(w["rect"])) if hits else None


def build_settings(found: Found, wins: list[dict]) -> tuple[dict, list[str]]:
    """찾은 네모 → 저장할 설정 (gorilla.* 키, 모니터 기준). 못 쓴 것은 이유 목록."""
    settings: dict[str, str] = {"gorilla.screen_region": "", "gorilla.region_window": ""}
    problems = []
    for target in TARGETS:
        box = found.boxes.get(target)
        if not box:
            continue
        info = window_of(center(box), wins)
        if info is None:
            problems.append(f"{target}: 앱 창 밖")
            continue
        try:
            part, _msg = region_settings(target, box, info, None)
        except GorillaError as e:
            problems.append(f"{target}: {e}")
            continue
        settings.update(part)
    # 입력칸·전송 버튼이 서로 다른 창에 있으면 마지막 것이 point_window 가 된다 → 입력칸 창 기준으로 맞춘다
    inp = found.boxes.get("input")
    if inp and window_of(center(inp), wins):
        settings["gorilla.point_window"] = ",".join(str(v) for v in window_of(center(inp), wins)["rect"])
    return settings, problems


def summary(app: str, found: Found, running: bool, opened: bool, photo: str | None, problems: list[str]) -> str:
    label = config.app_label(app)
    if not running:
        return (f"{label} 앱이 켜져 있지 않습니다 (창을 찾지 못함). {label} PC 앱을 켜고 듣는 채널의 채팅 화면을 연 뒤 "
                "다시 누르세요.")
    names = {"input": "입력칸", "send": "전송 버튼", "chat": "채팅 목록"}
    how = {"uia": "화면 요소", "claude": "Claude 가 화면을 보고", "template": "알려진 배치"}
    got = [f"{names[t]}({how.get(found.how.get(t), '?')})" for t in TARGETS if t in found.boxes]
    missing = [names[t] for t in TARGETS if t not in found.boxes]
    parts = [f"{label} 앱 켜져 있음."]
    if opened:
        parts.append("채팅이 닫혀 있어 말풍선 버튼을 눌러 열었습니다.")
    if got:
        parts.append("찾음: " + ", ".join(got) + ".")
    if missing:
        parts.append("못 찾음: " + ", ".join(missing) + ".")
    if found.chat_open is False and "input" not in found.boxes:
        parts.append(f"채팅 창이 닫혀 있습니다 — {label}에서 채팅(말풍선)을 연 뒤 다시 누르세요.")
    if found.login_required:
        parts.append(f"로그인이 필요합니다 — {label} 앱에서 직접 로그인하세요 (로그인해야 글이 올라갑니다).")
    if any(found.how.get(t) == "template" for t in ("input", "send")):
        parts.append("알려진 배치로 찾은 것은 앱 화면이 다르면 어긋날 수 있으니 확인 사진을 꼭 보고 전송 테스트를 하세요.")
    if problems:
        parts.append("저장하지 못함: " + "; ".join(problems)[:200] + ".")
    if photo:
        parts.append(f"확인 사진: {photo} (파랑 입력칸 · 초록 전송 버튼 · 보라 채팅 목록).")
    return " ".join(parts)


# ── 윈도우 (실제 화면) ────────────────────────────────────────────────
class Desk:
    """화면·창 조작 (윈도우 전용). 시험에서는 가짜로 바꾼다."""

    def windows(self) -> list[dict]:
        import ctypes
        from ctypes import wintypes

        from .gorilla import _dpi_aware, _user32, window_info

        _dpi_aware()
        user32 = _user32()
        out: list[dict] = []

        def cloaked(hwnd) -> bool:
            try:
                value = ctypes.c_int(0)
                ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 14, ctypes.byref(value),
                                                           ctypes.sizeof(value))
                return bool(value.value)
            except Exception:
                return False

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(hwnd, _lparam):
            visible, iconic = bool(user32.IsWindowVisible(hwnd)), bool(user32.IsIconic(hwnd))
            if not visible or cloaked(hwnd):
                return True
            try:
                info = window_info(hwnd)
            except Exception:
                return True
            info["iconic"] = iconic
            r = info["rect"]
            if iconic or (width(r) >= MIN_WINDOW and height(r) >= MIN_WINDOW):
                out.append(info)
            return True

        user32.EnumWindows(each, 0)
        return out

    def restore(self, hwnds: list[int]) -> None:
        from .gorilla import _user32

        user32 = _user32()
        for h in hwnds:
            if user32.IsIconic(h):
                user32.ShowWindow(h, 9)   # SW_RESTORE
        time.sleep(0.6)

    def front(self, hwnds: list[int]) -> None:
        from pywinauto.controls.hwndwrapper import HwndWrapper

        for h in hwnds:
            try:
                HwndWrapper(h).set_focus()
            except Exception:
                pass
        time.sleep(0.5)

    def foreground(self) -> int:
        from .gorilla import foreground_root

        try:
            return foreground_root()
        except Exception:
            return 0

    def back_to(self, hwnd: int) -> None:
        if not hwnd:
            return
        try:
            from pywinauto.controls.hwndwrapper import HwndWrapper

            HwndWrapper(hwnd).set_focus()
        except Exception:
            pass

    def grab(self, rect: Rect):
        from PIL import ImageGrab

        return ImageGrab.grab(bbox=tuple(int(v) for v in rect), all_screens=True).convert("RGB")

    def click(self, point: tuple[int, int], wins: list[dict], process: str) -> bool:
        """그 자리의 앱 창을 앞으로 가져온 뒤, 그 자리가 정말 그 앱일 때만 누른다 (다른 프로그램을 누르지 않게)."""
        from pywinauto import mouse

        from .gorilla import window_at_point

        owner = window_of(point, wins)
        if owner is None:
            return False
        self.front([owner["hwnd"]])
        try:
            if window_at_point(*point)["process"] != process:
                return False
        except Exception:
            return False
        mouse.click(coords=(int(point[0]), int(point[1])))
        time.sleep(2.0)   # 채팅 창이 펼쳐질 시간
        return True

    def uia(self, wins: list[dict]) -> Found:
        """화면 요소로 입력칸·전송 버튼 찾기 (보이는 것만). 못 찾으면 빈 결과."""
        found = Found()
        try:
            from pywinauto import Desktop
        except ImportError:
            return found
        edits, buttons = [], []
        for w in wins:
            if w.get("iconic"):
                continue
            try:
                root = Desktop(backend="uia").window(handle=w["hwnd"]).wrapper_object()
                for c in root.descendants(control_type="Edit")[:200]:
                    r = c.element_info.rectangle
                    if c.is_visible() and r.width() >= 80 and r.height() >= 12:
                        edits.append((r.left, r.top, r.right, r.bottom))
                for c in root.descendants(control_type="Button")[:400]:
                    r = c.element_info.rectangle
                    name = c.element_info.name or ""
                    if c.is_visible() and SEND_NAME.search(name) and r.width() >= 10:
                        buttons.append(((r.left, r.top, r.right, r.bottom), name))
            except Exception:
                continue
        if edits:
            inp = max(edits, key=lambda r: r[3])         # 채팅 입력칸은 보통 가장 아래 입력칸
            found.boxes["input"], found.how["input"] = inp, "uia"
            near = [b for b in buttons if b[0][1] < inp[3] + 80 and b[0][3] > inp[1] - 80 and b[0][0] >= inp[0]]
            if near:
                box, name = min(near, key=lambda b: abs(b[0][0] - inp[2]))
                found.boxes["send"], found.how["send"] = box, "uia"
                found.login_required = bool(LOGIN_HINT.search(name))
        return found

    def capture(self, rect: Rect, name: str, boxes) -> str | None:
        from .gorilla import capture

        try:
            return capture(rect, name, boxes=boxes)
        except Exception:
            return None


def vision(conn, app: str, image, origin: tuple[int, int]) -> Found | None:
    """Claude 가 사진을 보고 찾기. Claude 연결이 없거나 실패하면 None."""
    from .answerer import AnswererError, make, ready
    from .gorilla import to_jpeg

    ok, _why = ready(conn)
    if not ok:
        return None
    scale = min(1.0, VISION_MAX_SIDE / max(image.size))
    shown = image if scale == 1.0 else image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))))
    try:
        data = make(conn).locate_chat_ui(config.app_label(app), to_jpeg(shown, VISION_MAX_SIDE), shown.width,
                                         shown.height)
    except (AnswererError, Exception) as e:   # 한도·연결 문제면 알려진 배치로
        f = Found()
        f.notes.append(f"Claude 로 찾지 못함: {str(e)[:120]}")
        return f
    return vision_find(data, shown.size, origin, scale)


def find(conn, app: str, desk: Desk | None = None, use_claude: bool = True, click_toggle: bool = True,
         photo: bool = True, see=None) -> dict:
    """원버튼 찾기. 보고서 {app, running, process, windows, found, settings, message, photo}. 저장은 하지 않는다."""
    desk = desk or Desk()
    see = see or vision
    saved = config.get(conn, f"{app}.process_name")
    windows = desk.windows()
    process, wins = pick_app(app, windows, saved)
    report = {"app": app, "label": config.app_label(app), "at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "running": bool(wins), "process": process,
              "windows": [{"title": w.get("title", ""), "process": w.get("process", ""), "rect": list(w["rect"]),
                           "score": score_app(app, w, saved)} for w in windows if score_app(app, w, saved) > 0][:20],
              "settings": {}, "found": {}, "photo": None}
    if not wins:
        report["message"] = summary(app, Found(), False, False, None, [])
        return report
    was_front = desk.foreground()
    opened = False
    found = Found()
    try:
        desk.restore([w["hwnd"] for w in wins if w.get("iconic")])
        for attempt in (1, 2):
            wins = pick_app(app, desk.windows(), process)[1] or wins
            shown = [w for w in wins if not w.get("iconic")]
            if not shown:
                break
            area = union(w["rect"] for w in shown)
            image = None
            if use_claude or photo:
                desk.front([w["hwnd"] for w in shown])
                try:
                    image = desk.grab(area)
                except Exception:
                    image = None
            seen = see(conn, app, image, area[:2]) if use_claude and image is not None else None
            by_uia, by_layout = desk.uia(shown), template_find(app, shown)
            if seen is not None and seen.looked and seen.chat_open is False and "input" not in by_uia.boxes:
                # Claude 가 사진에서 채팅이 닫혀 있다고 보면 창 모양만 보고 정한 배치는 쓰지 않는다
                by_layout = Found(chat_open=False, toggle=by_layout.toggle)
            found = merge(by_uia, seen, by_layout)
            if found.boxes.get("input") and found.boxes.get("send"):
                break
            if attempt == 1 and click_toggle and found.toggle and found.chat_open is False:
                opened = desk.click(found.toggle, shown, process)
                if opened:
                    continue
                found.notes.append("채팅을 여는 버튼 자리가 다른 창에 가려 누르지 않음")
            break
        settings, problems = build_settings(found, wins) if found.boxes.get("input") and found.boxes.get("send") \
            else ({}, [])
        if photo and found.boxes:
            shown = [w for w in wins if not w.get("iconic")]
            area = union([w["rect"] for w in shown] + list(found.boxes.values()))
            report["photo"] = desk.capture(area, f"{app}_found",
                                           [(box, COLORS[t]) for t, box in found.boxes.items()])
    finally:
        desk.back_to(was_front)
    report.update(settings=settings, found={"boxes": {k: list(v) for k, v in found.boxes.items()}, "how": found.how,
                                            "chat_open": found.chat_open, "login_required": found.login_required,
                                            "opened": opened, "notes": found.notes})
    report["message"] = summary(app, found, True, opened, report["photo"], problems)
    return report


def save_report(app: str, report: dict) -> str:
    from .. import db

    out_dir = db.data_dir() / "inspect"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"{app}_found_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return path.name


def running(conn, app: str, desk: Desk | None = None) -> bool:
    """그 앱이 켜져 있는지 (창 하나라도 — 채팅 창 자리와 상관없이)."""
    try:
        windows = (desk or Desk()).windows()
    except Exception:
        return False
    return bool(pick_app(app, windows, config.get(conn, f"{app}.process_name"))[1])


def auto(conn, app: str, now: float | None = None, desk: Desk | None = None, log=None) -> dict | None:
    """듣는 중 채팅 창을 못 찾을 때: 5분에 한 번까지 Claude 없이 다시 찾아 저장한다. 한 일(보고서) 또는 None."""
    from .. import db

    now = now if now is not None else time.time()
    key = f"{app}.auto_found_at"
    try:
        last = float(db.get_setting(conn, key) or 0)
    except ValueError:
        last = 0.0
    if now - last < AUTO_EVERY_SECONDS:
        return None
    db.set_setting(conn, key, str(now))
    report = find(conn, app, desk=desk, use_claude=False, click_toggle=True, photo=False)
    if report["settings"]:
        for k, v in config.app_settings(app, report["settings"]).items():
            db.set_setting(conn, k, v)
    (log or (lambda m: db.log(conn, "gorilla", m)))(f"[{config.app_label(app)}] 자동 다시 찾기: {report['message']}")
    return report
