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
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .gorilla import SendResult, flat_text, region_changed, to_jpeg

CONSOLE_PATHS = (r"C:\LDPlayer\LDPlayer9\ldconsole.exe", r"D:\LDPlayer\LDPlayer9\ldconsole.exe",
                 r"C:\Program Files\LDPlayer\LDPlayer9\ldconsole.exe", r"C:\LDPlayer\LDPlayer4.0\ldconsole.exe",
                 r"D:\LDPlayer\LDPlayer4.0\ldconsole.exe")
TYPE_TRIES = 3           # 글 넣기를 다시 하는 횟수 (처음 포함)
CLEAR_KEYS = 120         # 입력칸에 남은 글 지우기: 끝으로 → 지우기 키 이만큼
INPUT_BOX = (0.35, 45)   # 입력칸 확인 네모: 입력칸 자리 좌우로 화면 너비의 이 비율, 위아래로 이 픽셀
KEY_ENTER, KEY_MOVE_END, KEY_DEL = 66, 123, 67


class LdError(RuntimeError):
    pass


def find_console(saved: str = "", exists=os.path.isfile) -> str | None:
    """ldconsole.exe 위치: 저장한 것 → 흔한 설치 위치."""
    for path in ([saved.strip()] if saved and saved.strip() else []) + list(CONSOLE_PATHS):
        if exists(path):
            return path
    return None


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


class Console:
    """ldconsole.exe 부르기. run 은 시험에서 가짜로 바꾼다 (subprocess.run 과 같은 모양)."""

    def __init__(self, path: str, run=subprocess.run, shot_dir: Path | None = None):
        self.path, self.run, self.shot_dir = path, run, shot_dir

    def call(self, *args: str, timeout: int = 20) -> str:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            r = self.run([self.path, *args], capture_output=True, timeout=timeout, creationflags=flags)
        except subprocess.TimeoutExpired:
            raise LdError(f"LDPlayer 명령이 {timeout}초 안에 끝나지 않음: {' '.join(args[:3])}")
        except OSError as e:
            raise LdError(f"ldconsole 을 실행하지 못함: {e}")
        out = r.stdout if isinstance(r.stdout, (bytes, bytearray)) else (r.stdout or "").encode("utf-8")
        for enc in ("utf-8", "cp949"):
            try:
                return out.decode(enc)
            except UnicodeDecodeError:
                continue
        return out.decode("utf-8", errors="replace")

    def instances(self) -> list[dict]:
        return parse_list2(self.call("list2"))

    def instance(self, index: int) -> dict | None:
        return next((i for i in self.instances() if i["index"] == index), None)

    def adb(self, index: int, command: str, timeout: int = 20) -> str:
        return self.call("adb", "--index", str(index), "--command", command, timeout=timeout)

    def tap(self, index: int, point: tuple[int, int]) -> None:
        self.adb(index, f"shell input tap {int(point[0])} {int(point[1])}")

    def keys(self, index: int, *codes: int) -> None:
        self.adb(index, "shell input keyevent " + " ".join(str(c) for c in codes))

    def clear(self, index: int) -> None:
        self.keys(index, KEY_MOVE_END, *([KEY_DEL] * CLEAR_KEYS))

    def type_text(self, index: int, text: str) -> None:
        """한글도 넣을 수 있는 LDPlayer 글자 넣기 (지금 입력칸에)."""
        self.call("action", "--index", str(index), "--key", "call.input", "--value", text)

    def screenshot(self, index: int) -> bytes | None:
        """그 창의 안드로이드 화면 사진(PNG). 못 찍으면 None."""
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
    return Console(path, run=run, shot_dir=db.data_dir() / "inspect")


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
        if self.cfg.index is None:
            raise LdError(f"{self.cfg.label}: 몇 번 LDPlayer 창에서 켰는지 정하지 않았습니다 ('LDPlayer 설정'에서 창 번호를 고르세요)")
        con = self._console()
        inst = con.instance(self.cfg.index)
        if inst is None:
            raise LdError(f"{self.cfg.label}: LDPlayer {self.cfg.index}번 창이 없습니다 (다중 실행기에서 번호를 확인하세요)")
        if not inst["started"]:
            raise LdError(f"{self.cfg.label}: LDPlayer {self.cfg.index}번 창이 꺼져 있습니다 — 켜고 {self.cfg.label} 앱을 열어 두세요")
        return con, self.cfg.index

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
        if self.cfg.input is None or self.cfg.send is None:
            raise LdError(f"{self.cfg.label}: 입력칸·전송 버튼 자리를 정하지 않았습니다 ('LDPlayer 설정'에서 화면 사진을 눌러 정하세요)")
        label = self.cfg.label
        before = self._shot(con, index)
        box = input_box(before.size, self.cfg.input) if before is not None else None
        filled, verified = None, False
        for n in range(1, TYPE_TRIES + 1):
            con.tap(index, self.cfg.input)
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
        con.tap(index, self.cfg.send)
        self.sleep(1.5)
        after = self._shot(con, index)
        if verified and self._changed(filled, after, box) is False:      # 전송을 눌렀는데 입력칸이 그대로
            con.tap(index, self.cfg.input)
            self.sleep(0.4)
            con.keys(index, KEY_ENTER)
            self.sleep(1.5)
            self.fixes.append("전송 터치가 안 돼 엔터로 다시 보냄")
            after = self._shot(con, index)
            if self._changed(filled, after, box) is False:
                shots.append((f"보내지 못했을 때 {label} 화면", self._evidence(after)))
                return SendResult("failed", f"{label} 전송 터치·엔터 모두 안 돼 입력칸에 글이 남아 있습니다 — 잠시 뒤 다시 보냅니다 "
                                            "(전송 버튼 자리를 확인해 보세요)")
        if after is not None:
            shots.append((f"보낸 뒤 {label} 화면", self._evidence(after)))
        how = "입력칸이 비워져 전송된 것으로 보임" if verified else "확인 사진을 못 찍어 전송 여부는 확인하지 못함"
        return SendResult("entered", f"LDPlayer {index}번 창 {label}: {how}")

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
        if self.cfg.chat:
            img = self._crop(img, self.cfg.chat) or img
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
