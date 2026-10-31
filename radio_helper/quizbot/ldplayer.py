"""LDPlayer(안드로이드 앱 플레이어)에서 켠 방송국 휴대폰 앱으로 듣고 보내기 (0.88).

PC 앱처럼 화면을 마우스로 누르지 않고, LDPlayer 에 딸린 ldconsole.exe(안드로이드 연결 도구)로 그 플레이어 창(번호)에
터치·글자 입력·화면 찍기를 한다. 그래서 마우스·키보드를 빼앗지 않고, 창이 가려져 있거나 최소화돼 있어도 된다.

- 한 플레이어 창(LDPlayer 다중 실행의 번호)에 방송국 앱 하나를 켜 둔다 (예: 0번 반디, 1번 YTN …).
- 입력칸·전송 버튼·채팅 목록 자리는 안드로이드 화면 픽셀로 저장한다 (관리 화면에서 그 창 화면 사진을 눌러 정함).
- 보내기: 입력칸 터치 → 남은 글 지우기 → 글 넣기 → 입력칸이 바뀌었는지 사진으로 확인 → 전송 터치 →
  글이 그대로면 엔터 → 그래도 그대로면 '못 보냄'(다시 보내기). 입력칸에 글이 들어간 것을 확인하지 못하면 전송을 누르지 않는다.
- 소리: 그 플레이어 창의 프로그램(dnplayer)만 녹음한다 (창마다 프로그램이 따로라 섞이지 않음).

ldconsole 명령 (LDPlayer 9): list2 · launch · runapp · action --key call.input · adb --command "shell input …".
실제 PC 에서 확인하지 못한 부분이 있어, 명령 결과를 그대로 기록에 남긴다.
"""

from __future__ import annotations

import io
import ntpath
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .gorilla import SendResult, flat_text, region_changed, to_jpeg

CONSOLE_PATHS = (r"C:\LDPlayer\LDPlayer9\ldconsole.exe", r"D:\LDPlayer\LDPlayer9\ldconsole.exe",
                 r"C:\Program Files\LDPlayer\LDPlayer9\ldconsole.exe", r"C:\LDPlayer\LDPlayer4.0\ldconsole.exe",
                 r"D:\LDPlayer\LDPlayer4.0\ldconsole.exe")
# 0.90: 사용자 창 제목이 'LDPlayer 14.0.32.0' — 설치 폴더 이름을 몰라 흔한 위치 아래를 모두 찾는다
CONSOLE_ROOTS = (r"C:\LDPlayer", r"D:\LDPlayer", r"E:\LDPlayer", r"C:\Program Files\LDPlayer",
                 r"C:\Program Files (x86)\LDPlayer", r"C:\Program Files\ldplayer9box")
CONSOLE_NAMES = ("ldconsole.exe", "dnconsole.exe")

# 0.90 사용자가 보낸 LDPlayer 6창 화면 사진(9:16)으로 잰 자리 — 안드로이드 화면 대비 비율 (x, y) / 채팅 목록 (왼·위·오른·아래).
# 화면 사진을 눌러 자리를 정하지 않았으면 이 자리를 쓴다 (정하면 그 자리가 우선).
LD_LAYOUTS = {
    "ifm": {"input": (0.38, 0.945), "send": (0.87, 0.945), "chat": (0.0, 0.24, 1.0, 0.89)},     # '…채팅 내용을 입력하세요' · 파란 '글쓰기'
    "ytn": {"input": (0.52, 0.965), "send": (0.965, 0.965), "chat": (0.0, 0.60, 1.0, 0.91)},    # '채팅을 입력해주세요(200자이내)' · 종이비행기
    "bandi": {"input": (0.38, 0.945), "send": (0.84, 0.945), "chat": (0.0, 0.10, 1.0, 0.84)},   # 반디톡 입력칸 · 파란 '등록'
    "kfn": {"input": (0.42, 0.775), "send": (0.82, 0.775), "chat": (0.0, 0.10, 1.0, 0.72)},     # '메시지를 입력해 주세요.' · 노란 화살표
    "cpbc": {"input": (0.40, 0.89), "send": (0.915, 0.89), "chat": (0.0, 0.40, 1.0, 0.86)},     # 빵톡 '메세지를 입력하세요' · 화살표
    "febc": {"input": (0.45, 0.85), "send": (0.92, 0.85), "chat": (0.0, 0.58, 1.0, 0.81)},      # '실시간 채팅에 참여해 보세요' · 화살표
}
# 창 이름으로 그 앱의 LDPlayer 창 찾기 (사용자는 창 이름을 앱 이름으로 바꿔 둠: '경인방송 iFM', 'YTN 라디오' …)
TITLE_HINTS = {"ifm": ("ifm", "경인"), "ytn": ("ytn",), "bandi": ("반디", "bandi", "ebs"), "kfn": ("kfn", "별사탕", "국방"),
               "cpbc": ("cpbc", "빵"), "febc": ("극동", "febc")}
TYPE_TRIES = 3           # 글 넣기를 다시 하는 횟수 (처음 포함)
CLEAR_KEYS = 400         # 입력칸에 남은 글 지우기: 끝으로 → 지우기 키 이만큼 (0.96: 120 → 400 — 긴 사연이 남아 새 글과 섞였음)
INPUT_BOX = (0.35, 45)   # 입력칸 확인 네모: 입력칸 자리 좌우로 화면 너비의 이 비율, 위아래로 이 픽셀
KEY_ENTER, KEY_MOVE_END, KEY_DEL, KEY_BACK = 66, 123, 67, 4


class LdError(RuntimeError):
    pass


def find_console(saved: str = "", exists=os.path.isfile, glob=None) -> str | None:
    """ldconsole.exe 위치: 저장한 것 → 흔한 설치 위치 → 흔한 폴더 아래 (LDPlayer9·LDPlayer14 등 어느 폴더든)."""
    for path in ([saved.strip()] if saved and saved.strip() else []) + list(CONSOLE_PATHS):
        if exists(path):
            return path
    if glob is None:
        import glob as glob_mod

        glob = glob_mod.glob
    for root in CONSOLE_ROOTS:
        for name in CONSOLE_NAMES:
            for path in sorted(glob(ntpath.join(root, name)) + glob(ntpath.join(root, "*", name)), reverse=True):
                if exists(path):
                    return path
    return None


def match_instance(app: str, instances: list[dict]) -> dict | None:
    """창 이름으로 그 앱의 LDPlayer 창 (딱 하나일 때만)."""
    hints = TITLE_HINTS.get(app, ())
    hits = [i for i in instances if any(h in (i.get("title") or "").lower() for h in hints)]
    return hits[0] if len(hits) == 1 else None


def layout_point(app: str, target: str, size: tuple[int, int] | None) -> tuple[int, int] | None:
    """알려진 자리(비율) → 그 화면 크기의 픽셀."""
    lay = LD_LAYOUTS.get(app, {}).get(target)
    if lay is None or not size:
        return None
    return int(lay[0] * size[0]), int(lay[1] * size[1])


def layout_box(app: str, size: tuple[int, int] | None) -> tuple[int, int, int, int] | None:
    lay = LD_LAYOUTS.get(app, {}).get("chat")
    if lay is None or not size:
        return None
    return int(lay[0] * size[0]), int(lay[1] * size[1]), int(lay[2] * size[0]), int(lay[3] * size[1])


def parse_list2(text: str) -> list[dict]:
    """ldconsole list2: '번호,제목,창,연결창,켜짐(1/0),프로그램번호,가상머신번호[,너비,높이,dpi]' 한 줄씩."""
    out = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7 or not parts[0].lstrip("-").isdigit():
            continue
        try:
            item = {"index": int(parts[0]), "title": parts[1], "hwnd": int(parts[2] or 0), "bind_hwnd": int(parts[3] or 0),
                    "started": parts[4] == "1", "pid": int(parts[5] or 0), "vbox_pid": int(parts[6] or 0)}
        except ValueError:
            continue
        if len(parts) >= 9 and parts[7].isdigit() and parts[8].isdigit():
            item["size"] = (int(parts[7]), int(parts[8]))
        out.append(item)
    return out


def parse_point(text: str | None) -> tuple[int, int] | None:
    try:
        x, y = (int(float(v)) for v in (text or "").split(","))
    except ValueError:
        return None
    return x, y


def parse_box(text: str | None) -> tuple[int, int, int, int] | None:
    try:
        v = tuple(int(float(p)) for p in (text or "").split(","))
    except ValueError:
        return None
    return v if len(v) == 4 and v[2] > v[0] and v[3] > v[1] else None


LIST_TTL = 10            # 0.95: 창 목록(list2)은 10초 동안 다시 묻지 않는다
SERIAL_TTL = 3600        # 창 번호 → adb 연결 이름은 한 시간 동안 기억
SERIAL_FAIL_TTL = 60     # 못 물었으면 1분 뒤 다시
SERIAL_RE = re.compile(r"^(emulator-\d+|127\.0\.0\.1:\d+|localhost:\d+)$")
_cache: dict = {}        # 이 프로그램 안에서 함께 씀 (보낼 때마다 Console 을 새로 만들어도)
BOOT_WAIT = 180          # 0.97: PC 를 켠 뒤 이만큼(초)은 ldconsole 을 부르지 않는다 (LDPlayer 가 자리 잡을 때까지)
LOCK_FILE = "ldconsole.lock"
LOCK_WAIT = 30           # 0.97: 다른 청취기의 ldconsole 명령이 끝나기를 기다리는 최대 시간 (넘으면 그냥 실행)


def uptime_seconds() -> float | None:
    """PC 를 켠 지 몇 초 지났는지 (윈도우만). 모르면 None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        k = ctypes.WinDLL("kernel32")
        k.GetTickCount64.restype = ctypes.c_uint64
        return k.GetTickCount64() / 1000.0
    except Exception:
        return None


def _lock_file(f) -> None:
    f.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(f) -> None:
    try:
        f.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def one_at_a_time(path: Path | None, wait: float = LOCK_WAIT, sleep=time.sleep, clock=time.monotonic):
    """컴퓨터 전체에서 ldconsole 명령을 한 번에 하나씩 (청취기·보조 칸·관리 화면이 따로 프로그램이라 파일 잠금으로).
    윈도우가 잠금을 프로그램이 꺼질 때 함께 풀어 주므로 멈춘 잠금이 남지 않는다. wait 초 안에 못 잡으면 그냥 실행 → 잡았는지"""
    if path is None:
        yield False
        return
    try:
        f = open(path, "a+b")
    except OSError:
        yield False
        return
    got, deadline = False, clock() + wait
    try:
        while True:
            try:
                _lock_file(f)
                got = True
                break
            except OSError:
                if clock() >= deadline:
                    break
                sleep(0.2)
        yield got
    finally:
        if got:
            _unlock_file(f)
        f.close()


class Console:
    """ldconsole.exe 부르기. run 은 시험에서 가짜로 바꾼다 (subprocess.run 과 같은 모양).
    0.95: ldconsole 명령마다 LDPlayer 의 VBoxManage.exe 가 새로 떠서, 창 6개 × 보낼 때마다 여러 번이면 윈도우 자원이 모자라
    'VBoxManage.exe 응용 프로그램 오류 (0xc0000142)'가 났다 → 창 목록은 잠깐 기억하고, 터치·키·화면 찍기는
    LDPlayer 폴더의 adb.exe 로 그 창에 바로 보낸다 (창 번호의 adb 연결 이름은 ldconsole 에 한 번만 물음). 안 되면 예전처럼 ldconsole.
    0.97: PC 를 켤 때 같은 오류 창이 떴다 (LDPlayer 창 6개·도우미·청취기가 한꺼번에 시작) → PC 를 켠 뒤 BOOT_WAIT 초는
    ldconsole 을 부르지 않고, ldconsole 명령은 컴퓨터 전체에서 한 번에 하나씩 (lock_path 잠금 파일)."""

    def __init__(self, path: str, run=subprocess.run, shot_dir: Path | None = None, exists=os.path.isfile,
                 lock_path: Path | None = None, uptime=uptime_seconds):
        self.path, self.run, self.shot_dir = path, run, shot_dir
        self.lock_path, self.uptime = lock_path, uptime
        adb = ntpath.join(ntpath.dirname(path), "adb.exe")
        self.adb_path = adb if exists(adb) else None

    def _run(self, exe: str, args: list[str], timeout: int) -> bytes:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            r = self.run([exe, *args], capture_output=True, timeout=timeout, creationflags=flags)
        except subprocess.TimeoutExpired:
            raise LdError(f"LDPlayer 명령이 {timeout}초 안에 끝나지 않음: {' '.join(str(a) for a in args[:3])}")
        except OSError as e:
            raise LdError(f"{ntpath.basename(exe)} 을 실행하지 못함: {e}")
        out = r.stdout
        return out if isinstance(out, (bytes, bytearray)) else (out or "").encode("utf-8")

    def booting(self) -> float | None:
        """PC 를 켠 지 BOOT_WAIT 초가 안 됐으면 남은 초, 아니면 None."""
        up = self.uptime() if self.uptime else None
        return None if up is None or up >= BOOT_WAIT else BOOT_WAIT - up

    def call(self, *args: str, timeout: int = 20) -> str:
        left = self.booting()
        if left is not None:
            raise LdError(f"PC 를 켠 지 얼마 안 돼 LDPlayer 가 자리 잡기를 기다리는 중 ({left:.0f}초 뒤부터 사용)")
        with one_at_a_time(self.lock_path):
            out = self._run(self.path, list(args), timeout)
        for enc in ("utf-8", "cp949"):
            try:
                return out.decode(enc)
            except UnicodeDecodeError:
                continue
        return out.decode("utf-8", errors="replace")

    def instances(self, now: float | None = None) -> list[dict]:
        now = time.monotonic() if now is None else now
        hit = _cache.get(("list2", self.path))
        if hit and now - hit[0] < LIST_TTL:
            return hit[1]
        rows = parse_list2(self.call("list2"))
        _cache[("list2", self.path)] = (now, rows)
        return rows

    def instance(self, index: int) -> dict | None:
        return next((i for i in self.instances() if i["index"] == index), None)

    def adb(self, index: int, command: str, timeout: int = 10) -> str:
        return self.call("adb", "--index", str(index), "--command", command, timeout=timeout)

    def serial(self, index: int, now: float | None = None) -> str | None:
        """그 창의 adb 연결 이름 (예: emulator-5556). adb.exe 가 없거나 모르면 None."""
        if not self.adb_path:
            return None
        now = time.monotonic() if now is None else now
        hit = _cache.get(("serial", self.path, index))
        if hit and now - hit[0] < SERIAL_TTL:
            return hit[1]
        try:
            got = self.adb(index, "get-serialno").strip().splitlines()
        except LdError:
            # 0.97: 켠 직후 기다림은 기억하지 않고, 다른 실패는 1분만 기억 (예전엔 한 시간 동안 ldconsole 로만 보냈음)
            if self.booting() is None:
                _cache[("serial", self.path, index)] = (now - SERIAL_TTL + SERIAL_FAIL_TTL, None)
            return None
        serial = got[-1].strip() if got and SERIAL_RE.match(got[-1].strip()) else None
        _cache[("serial", self.path, index)] = (now, serial)
        return serial

    def direct(self, index: int, args: list[str], timeout: int = 10) -> bytes | None:
        """adb.exe 로 그 창에 바로 (VBoxManage 를 거치지 않음). 바로 보낼 수 없으면 None."""
        serial = self.serial(index)
        if serial is None:
            return None
        try:
            return self._run(self.adb_path, ["-s", serial, *args], timeout)
        except LdError:
            _cache.pop(("serial", self.path, index), None)      # 연결 이름이 바뀌었을 수 있음 → 다음에 다시 물음
            return None

    def tap(self, index: int, point: tuple[int, int]) -> None:
        args = ["shell", "input", "tap", str(int(point[0])), str(int(point[1]))]
        if self.direct(index, args) is None:
            self.adb(index, " ".join(args))

    def keys(self, index: int, *codes: int) -> None:
        args = ["shell", "input", "keyevent", *(str(c) for c in codes)]
        if self.direct(index, args) is None:
            self.adb(index, " ".join(args))

    def clear(self, index: int) -> None:
        self.keys(index, KEY_MOVE_END, *([KEY_DEL] * CLEAR_KEYS))

    def shell(self, index: int, *args: str) -> str:
        """그 창에서 안드로이드 명령 (글자로 결과)."""
        out = self.direct(index, ["shell", *args])
        if out is None:
            return self.adb(index, "shell " + " ".join(args))
        return out.decode("utf-8", errors="replace")

    def screen_size(self, index: int) -> tuple[int, int] | None:
        """안드로이드 화면 크기 (wm size — 바꾼 크기가 있으면 그것). 모르면 None. LDPlayer 창 목록(list2)의 크기는
        가로·세로가 앱 화면과 다를 수 있어 (태블릿 설정에 세로 앱) 쓰지 않는다 (0.96)."""
        hit = _cache.get(("size", self.path, index))
        if hit:
            return hit
        try:
            text = self.shell(index, "wm", "size")
        except LdError:
            return None
        found = re.findall(r"(\d+)x(\d+)", text)
        if not found:
            return None
        size = tuple(int(v) for v in found[-1])     # Override size 가 있으면 마지막 줄
        _cache[("size", self.path, index)] = size
        return size

    def keyboard_shown(self, index: int) -> bool:
        """화면 자판(IME)이 떠 있는지 — 떠 있으면 입력줄·전송 버튼이 위로 밀려 알려진 자리가 어긋난다."""
        try:
            return "mInputShown=true" in self.shell(index, "dumpsys", "input_method")
        except LdError:
            return False

    def type_text(self, index: int, text: str) -> None:
        """한글도 넣을 수 있는 LDPlayer 글자 넣기 (지금 입력칸에)."""
        self.call("action", "--index", str(index), "--key", "call.input", "--value", text)

    def screenshot(self, index: int) -> bytes | None:
        """그 창의 안드로이드 화면 사진(PNG). 못 찍으면 None. adb.exe 로 바로 받을 수 있으면 한 번에 (파일 없이)."""
        data = self.direct(index, ["exec-out", "screencap", "-p"], timeout=15)
        if data and data[:4] == b"\x89PNG":
            return bytes(data)
        folder = self.shot_dir or Path(".")
        folder.mkdir(parents=True, exist_ok=True)
        local = folder / f"ld{index}_screen.png"
        try:
            local.unlink()
        except OSError:
            pass
        try:
            self.adb(index, "shell screencap -p /sdcard/rh_screen.png")
            self.adb(index, f'pull /sdcard/rh_screen.png "{local}"')
            return local.read_bytes() if local.exists() else None
        except (LdError, OSError):
            return None

    def packages(self, index: int) -> list[str]:
        text = self.adb(index, "shell pm list packages -3")
        return sorted(line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("package:"))

    def run_app(self, index: int, package: str) -> None:
        self.call("runapp", "--index", str(index), "--packagename", package)

    def launch(self, index: int) -> None:
        self.call("launch", "--index", str(index), timeout=60)


def console(conn=None, run=subprocess.run) -> Console:
    """설정대로 ldconsole 을 연다. 없으면 LdError."""
    from .. import db

    saved = config.get(conn, "ldplayer.console") if conn is not None else ""
    path = find_console(saved)
    if path is None:
        raise LdError("LDPlayer 의 ldconsole.exe 를 찾지 못했습니다 — LDPlayer 를 설치했으면 'LDPlayer 설정'에서 위치를 적어 주세요 "
                      r"(보통 C:\LDPlayer\LDPlayer9\ldconsole.exe)")
    return Console(path, run=run, shot_dir=db.data_dir() / "inspect", lock_path=db.data_dir() / LOCK_FILE)


@dataclass
class LdConfig:
    app: str
    index: int | None = None
    input: tuple[int, int] | None = None
    send: tuple[int, int] | None = None
    chat: tuple[int, int, int, int] | None = None
    package: str = ""
    test_message: str = ""

    @property
    def label(self) -> str:
        return config.app_label(self.app)

    @classmethod
    def load(cls, conn, app: str) -> "LdConfig":
        raw = (config.get(conn, f"{app}.ld_index") or "").strip()
        return cls(app=app, index=int(raw) if raw.lstrip("-").isdigit() else None,
                   input=parse_point(config.get(conn, f"{app}.ld_input")),
                   send=parse_point(config.get(conn, f"{app}.ld_send")),
                   chat=parse_box(config.get(conn, f"{app}.ld_chat")),
                   package=(config.get(conn, f"{app}.ld_package") or "").strip(),
                   test_message=(config.get(conn, f"{app}.test_message") or "").strip())


def _image(png: bytes | None):
    if not png:
        return None
    try:
        from PIL import Image

        return Image.open(io.BytesIO(png)).convert("RGB")
    except Exception:
        return None


def input_box(img_size: tuple[int, int], point: tuple[int, int]) -> tuple[int, int, int, int]:
    w, h = img_size
    dx, dy = int(w * INPUT_BOX[0]), INPUT_BOX[1]
    return max(0, point[0] - dx), max(0, point[1] - dy), min(w, point[0] + dx), min(h, point[1] + dy)


@dataclass
class LdSender:
    """LDPlayer 창 하나의 방송국 앱으로 보내는 도구 (Gorilla 와 같은 모양: send · is_running · chat_image · send_test)."""
    cfg: LdConfig
    make_console: object = None            # () → Console
    sleep: object = time.sleep
    fixes: list = field(default_factory=list)

    def _console(self) -> Console:
        if self.make_console is None:
            from .. import db

            conn = db.connect()
            try:
                return console(conn)
            finally:
                conn.close()
        return self.make_console()

    def _ready(self) -> tuple[Console, int]:
        con = self._console()
        if self.cfg.index is None:                 # 0.90: 창 이름(앱 이름)으로 찾음
            found = match_instance(self.cfg.app, con.instances())
            if found is None:
                raise LdError(f"{self.cfg.label}: 몇 번 LDPlayer 창인지 모릅니다 — 창 이름을 앱 이름으로 바꾸거나 "
                              "'LDPlayer 설정'에서 창 번호를 고르세요")
            self.cfg.index = found["index"]
            self._size = found.get("size")
        inst = con.instance(self.cfg.index)
        if inst is None:
            raise LdError(f"{self.cfg.label}: LDPlayer {self.cfg.index}번 창이 없습니다 (다중 실행기에서 번호를 확인하세요)")
        if not inst["started"]:
            raise LdError(f"{self.cfg.label}: LDPlayer {self.cfg.index}번 창이 꺼져 있습니다 — 켜고 {self.cfg.label} 앱을 열어 두세요")
        self._size = inst.get("size") or getattr(self, "_size", None)
        return con, self.cfg.index

    def _points(self, size) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
        """입력칸·전송 버튼: 화면 사진을 눌러 정한 자리, 없으면 알려진 자리 (화면 크기로 환산)."""
        inp = self.cfg.input or layout_point(self.cfg.app, "input", size)
        snd = self.cfg.send or layout_point(self.cfg.app, "send", size)
        if (inp, snd) != (self.cfg.input, self.cfg.send) and inp and snd:
            self.fixes.append(f"정해 둔 자리가 없어 {self.cfg.label} 알려진 자리({inp[0]},{inp[1]} / {snd[0]},{snd[1]})를 누름")
        return inp, snd

    def is_running(self) -> bool:
        try:
            self._ready()
            return True
        except Exception:
            return False

    def _shot(self, con: Console, index: int):
        return _image(con.screenshot(index))

    @staticmethod
    def _crop(img, box):
        try:
            return img.crop(box) if img is not None else None
        except Exception:
            return None

    def _changed(self, before, after, box) -> bool | None:
        """입력칸 네모가 바뀌었는지 (사진이 없으면 None = 모름)."""
        a, b = self._crop(before, box), self._crop(after, box)
        if a is None or b is None:
            return None
        return region_changed(a, b)

    def _evidence(self, img) -> bytes | None:
        try:
            return to_jpeg(img, max_side=1600) if img is not None else None
        except Exception:
            return None

    def send(self, text: str) -> SendResult:
        text = flat_text(text)
        if not text:
            return SendResult("failed", "보낼 글이 비어 있어 입력하지 않았습니다")
        self.fixes = []
        limit = config.APP_CHAT_LIMITS.get(self.cfg.app)
        if limit and len(text) > limit:
            text = config.fit_text(text, limit)
        shots: list = []
        try:
            result = self._send(text, shots)
        except LdError as e:
            result = SendResult("failed", str(e))
        result.shots = shots
        if self.fixes:
            result.detail = " · ".join(self.fixes) + " → " + result.detail
        return result

    def _send(self, text: str, shots: list) -> SendResult:
        con, index = self._ready()
        label = self.cfg.label
        before = self._shot(con, index)
        size = before.size if before is not None else self._screen_size(con, index)
        inp, snd = self._points(size)
        if inp is None or snd is None:
            raise LdError(f"{label}: 입력칸·전송 버튼 자리를 정하지 않았습니다 ('LDPlayer 설정'에서 화면 사진을 눌러 정하세요)")
        # 0.96: 어디까지 했는지 결과에 남긴다 (보내지 못했을 때 원인을 바로 찾게)
        self.trace = [f"화면 {size[0]}×{size[1]}" if size else "화면 크기 모름",
                      "사진 확인 가능" if before is not None else "사진 못 찍음(확인 없이)",
                      f"입력칸 {inp[0]},{inp[1]} · 전송 {snd[0]},{snd[1]}", f"글 {len(text)}자"]
        box = input_box(before.size, inp) if before is not None else None
        filled, verified = None, False
        for n in range(1, TYPE_TRIES + 1):
            con.tap(index, inp)
            self.sleep(0.6)
            con.clear(index)
            self.sleep(0.3)
            empty = self._shot(con, index) if box else None
            con.type_text(index, text)
            self.sleep(1.2)
            filled = self._shot(con, index) if box else None
            went = self._changed(empty, filled, box) if box else None
            if went is None:                     # 사진을 못 찍어 확인할 길이 없음 → 예전처럼 보냄
                break
            if went:
                verified = True
                if n > 1:
                    self.fixes.append(f"글 넣기가 {n - 1}번 안 들어가 다시 넣음")
                break
        else:
            if filled is not None:
                shots.append((f"보내지 못했을 때 {label} 화면", self._evidence(filled)))
            raise LdError(f"{label} 입력칸에 글이 들어가지 않아 전송을 누르지 않았습니다 (글 넣기 {TYPE_TRIES}번) — "
                          "입력칸 자리를 다시 정하거나 LDPlayer 의 글자 입력을 확인하세요")
        self.trace.append("글 들어감 확인" if verified else "글 들어감 확인 못 함")
        if getattr(con, "keyboard_shown", lambda i: False)(index):   # 화면 자판이 입력줄·전송 버튼을 밀어 올림 → 자판만 내림
            con.keys(index, KEY_BACK)
            self.sleep(0.6)
            self.trace.append("화면 자판 내림")
            filled = self._shot(con, index) if box else filled
        con.tap(index, snd)
        self.sleep(1.5)
        after = self._shot(con, index)
        if verified and self._changed(filled, after, box) is False:      # 전송을 눌렀는데 입력칸이 그대로
            self.trace.append("전송 터치 뒤 글이 그대로")
            con.tap(index, inp)
            self.sleep(0.4)
            con.keys(index, KEY_ENTER)
            self.sleep(1.5)
            self.fixes.append("전송 터치가 안 돼 엔터로 다시 보냄")
            after = self._shot(con, index)
            if self._changed(filled, after, box) is False:
                self.trace.append("엔터 뒤에도 그대로")
                shots.append((f"보내지 못했을 때 {label} 화면", self._evidence(after)))
                return SendResult("failed", f"{label} 전송 터치·엔터 모두 안 돼 입력칸에 글이 남아 있습니다 — 잠시 뒤 다시 보냅니다 "
                                            f"(전송 버튼 자리를 확인해 보세요) [{' · '.join(self.trace)}]")
        if after is not None:
            shots.append((f"보낸 뒤 {label} 화면", self._evidence(after)))
        how = "입력칸이 비워져 전송된 것으로 보임" if verified else "확인 사진을 못 찍어 전송 여부는 확인하지 못함"
        return SendResult("entered" if verified else "unknown",
                          f"LDPlayer {index}번 창 {label}: {how} [{' · '.join(self.trace)}]")

    def _screen_size(self, con, index: int) -> tuple[int, int] | None:
        try:
            return con.screen_size(index) if hasattr(con, "screen_size") else None
        except Exception:
            return None

    def send_test(self, text: str) -> tuple[SendResult, str]:
        result = self.send(text)
        return result, f"LDPlayer {self.cfg.index}번 창 {self.cfg.label}에 '{flat_text(text)}' 보내기 → {result.detail}"

    def screen_png(self) -> bytes | None:
        con, index = self._ready()
        return con.screenshot(index)

    def chat_image(self, max_side: int = 900, save_as: str | None = None, click_ok: bool = True) -> bytes | None:
        """채팅 목록 부분 (정하지 않았으면 화면 전체). 못 찍으면 None."""
        try:
            con, index = self._ready()
        except LdError:
            return None
        img = self._shot(con, index)
        if img is None:
            return None
        chat = self.cfg.chat or layout_box(self.cfg.app, img.size)
        if chat:
            img = self._crop(img, chat) or img
        self._save(img, save_as)
        return to_jpeg(img, max_side)

    def notice_image(self, max_side: int = 1100, save_as: str | None = None) -> bytes | None:
        try:
            con, index = self._ready()
        except LdError:
            return None
        img = self._shot(con, index)
        if img is None:
            return None
        self._save(img, save_as)
        return to_jpeg(img, max_side)

    def evidence_image(self, w=None) -> bytes | None:
        try:
            con, index = self._ready()
        except LdError:
            return None
        return self._evidence(self._shot(con, index))

    @staticmethod
    def _save(img, save_as: str | None) -> None:
        if not save_as:
            return
        from .. import db

        out_dir = db.data_dir() / "inspect"
        out_dir.mkdir(exist_ok=True)
        img.save(out_dir / f"{save_as}.png")


def sender_for(conn, app: str):
    """그 앱으로 보내는 도구: LDPlayer 로 쓰면 LdSender, 아니면 PC 앱(Gorilla)."""
    if config.platform(conn, app) == "ldplayer":
        return LdSender(LdConfig.load(conn, app))
    from .gorilla import Gorilla

    return Gorilla(config.GorillaConfig.load(conn, app))


def audio_process(con: Console, index: int, sessions: list[dict], procs: dict) -> tuple[int, str]:
    """그 플레이어 창의 소리를 내는 프로세스 → (번호, 이름). 소리 길(오디오 세션)을 연 쪽(플레이어·가상머신)을 먼저,
    없으면 플레이어 프로그램(dnplayer — 하위 프로세스까지 함께 녹음)."""
    inst = con.instance(index)
    if inst is None or not inst["started"]:
        raise RuntimeError(f"LDPlayer {index}번 창이 켜져 있지 않습니다 — 켜고 방송을 재생하세요")
    mine = {p for p in (inst["pid"], inst["vbox_pid"]) if p}
    from .appaudio import chain

    hits = [s for s in sessions if mine & {p for p, _ in chain(procs, s["pid"])}]
    if hits:
        s = max(hits, key=lambda s: s.get("peak", 0))
        return s["pid"], procs.get(s["pid"], (0, "ldplayer"))[1]
    pid = inst["pid"] or inst["vbox_pid"]
    return pid, procs.get(pid, (0, "dnplayer.exe"))[1]
