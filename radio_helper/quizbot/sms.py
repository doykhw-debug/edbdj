"""휴대폰 문자로 보내기 (안드로이드 + USB 케이블, ADB).

라디오 문자(#1077 등)는 내 휴대폰 번호로 가야 하므로, PC에 USB로 연결한 휴대폰의 기본 문자 앱을 조작해 보낸다.

1. 받는 번호와 글을 채운 문자 작성 화면을 연다 (SENDTO 인텐트).
2. 화면 구조(uiautomator)를 읽어 받는 번호가 맞는지 확인한다. '#'이 빠진 번호(예: 1077)로 가는 일을 막는다.
3. 전송 버튼을 눌러 보내고, 입력칸이 비었는지로 결과를 판단한다.

준비: 휴대폰 '개발자 옵션 → USB 디버깅' 켜기, PC 에 'platform-tools'(adb) 풀어 두기, 연결 후 휴대폰에서 'USB 디버깅 허용'.
라디오 문자는 건당 요금(보통 50원, 긴 글은 더)이 휴대폰 요금에 붙는다.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import db

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SEND_WORDS = re.compile(r"^(보내기|전송|문자 보내기|SMS 보내기|MMS 보내기|메시지 보내기|Send|Send SMS|Send MMS|Send message)$",
                        re.I)
SEND_ID = re.compile(r"(^|[:/_])send(_|$|[a-z_]*button|_message|_icon)", re.I)
COST_PER_SMS = 50


@dataclass
class SendResult:
    status: str
    detail: str
    shots: list = field(default_factory=list)   # 증거 사진 [(설명, PNG 바이트)]


class SmsError(RuntimeError):
    pass


# ── 설정 ────────────────────────────────────────────────────────────
def parse_channels(text: str) -> dict[str, str]:
    """'채널=문자번호' 줄들 → {채널: 번호} (순서 유지). 번호가 없거나 형식이 틀리면 빈 문자열."""
    out = {}
    for line in (text or "").splitlines():
        parts = line.split("=")   # 채널=번호=앱 (앱은 config.chat_app 에서 읽음)
        channel = parts[0].strip()
        number = parts[1].strip().replace("-", "").replace(" ", "") if len(parts) > 1 else ""
        if channel:
            out[channel] = number if re.fullmatch(r"#?\d{3,12}", number) else ""
    return out


def with_signature(text: str, nickname: str, sign: bool) -> str:
    text = (text or "").strip()
    nickname = (nickname or "").strip()
    if sign and nickname and not text.endswith(nickname):
        return f"{text} - {nickname}"
    return text


def sms_uri(number: str) -> str:
    return "smsto:" + number.replace("#", "%23")


def find_adb(configured: str = "") -> str | None:
    candidates = [configured] if configured else []
    found = shutil.which("adb")
    if found:
        candidates.append(found)
    exe = "adb.exe" if sys.platform == "win32" else "adb"
    candidates += [str(PROJECT_ROOT / "platform-tools" / exe), str(PROJECT_ROOT / "tools" / "platform-tools" / exe)]
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(str(Path(os.environ["LOCALAPPDATA"]) / "Android" / "Sdk" / "platform-tools" / exe))
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


# ── 화면 구조 읽기 ───────────────────────────────────────────────────
def parse_devices(output: str) -> list[tuple[str, str]]:
    """'adb devices' 출력 → [(시리얼, 상태)]. 상태: device / unauthorized / offline"""
    out = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith(("*", "List of devices")):
            continue
        parts = line.split()
        if len(parts) >= 2:
            out.append((parts[0], parts[1]))
    return out


def parse_bounds(text: str) -> tuple[int, int, int, int] | None:
    m = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", text or "")
    return tuple(int(v) for v in m.groups()) if m else None


def _nodes(xml: str):
    try:
        root = ET.fromstring(xml[xml.find("<"):] if "<" in xml else xml)
    except ET.ParseError:
        return []
    return list(root.iter("node"))


def find_send_button(xml: str) -> tuple[int, int] | None:
    """문자 앱의 전송 버튼 가운데 좌표. 이름(보내기·Send)이나 화면 요소 id(send...)로 찾는다."""
    best = None
    for n in _nodes(xml):
        if n.get("enabled") == "false":
            continue
        rid = n.get("resource-id", "")
        label = (n.get("content-desc") or n.get("text") or "").strip()
        score = (2 if SEND_ID.search(rid.split("/")[-1]) and "schedule" not in rid.lower() else 0) + \
                (1 if SEND_WORDS.match(label) else 0)
        b = parse_bounds(n.get("bounds", ""))
        if score and b and (best is None or score > best[0]):
            best = (score, ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2))
    return best[1] if best else None


def compose_text(xml: str) -> str | None:
    """문자 입력칸(EditText)의 글. 입력칸이 없으면 None."""
    texts = [n.get("text", "") for n in _nodes(xml) if n.get("class", "").endswith("EditText")]
    if not texts:
        return None
    return max(texts, key=len)


def shows_number(xml: str, number: str) -> bool:
    """작성 화면에 받는 번호가 그대로 보이는지. '#1077'이면 '#'까지 있어야 한다."""
    flat = " ".join(f"{n.get('text', '')} {n.get('content-desc', '')}" for n in _nodes(xml)).replace("-", "").replace(" ", "")
    return number.replace("-", "") in flat


# ── ADB ─────────────────────────────────────────────────────────────
Runner = Callable[[list[str], float], tuple[int, bytes]]


def _run(args: list[str], timeout: float) -> tuple[int, bytes]:
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    p = subprocess.run(args, capture_output=True, timeout=timeout, **kwargs)
    return p.returncode, p.stdout + p.stderr


class AdbSms:
    def __init__(self, adb: str | None, run: Runner = _run, sleep: Callable[[float], None] = time.sleep,
                 verify_number: bool = True, shots_dir: Path | None = None):
        self.adb, self.run, self.sleep = adb, run, sleep
        self.verify_number = verify_number
        self.shots_dir = shots_dir
        self.serial: str | None = None

    @classmethod
    def from_settings(cls, conn) -> "AdbSms":
        from . import config

        return cls(find_adb(config.get(conn, "sms.adb_path")),
                   verify_number=config.get(conn, "sms.verify_number") != "0",
                   shots_dir=db.data_dir() / "inspect")

    # 기본 명령
    def _adb(self, *args: str, timeout: float = 15) -> str:
        if not self.adb:
            raise SmsError("adb 를 찾지 못했습니다. 문자 설정 화면의 안내대로 platform-tools 를 풀어 두세요.")
        cmd = [self.adb] + (["-s", self.serial] if self.serial else []) + list(args)
        code, out = self.run(cmd, timeout)
        text = out.decode("utf-8", errors="replace")
        if code != 0:
            raise SmsError(f"adb {' '.join(args[:2])} 실패: {text.strip()[:200]}")
        return text

    def shell(self, *words: str, timeout: float = 15) -> str:
        # adb 는 인자를 공백으로 이어 휴대폰 셸에 넘기므로, 한글·특수문자가 깨지지 않게 하나씩 감싼다
        return self._adb("shell", " ".join(shlex.quote(w) for w in words), timeout=timeout)

    def status(self) -> tuple[bool, str]:
        """연결 상태와 설명."""
        if not self.adb:
            return False, "adb 를 찾지 못함 — platform-tools 를 내려받아 이 프로그램 폴더에 풀어 두세요"
        devices = parse_devices(self._adb("devices"))
        ready = [s for s, state in devices if state == "device"]
        if ready:
            self.serial = ready[0]
            return True, f"연결됨 ({ready[0]})"
        if any(state == "unauthorized" for _s, state in devices):
            return False, "휴대폰 화면의 'USB 디버깅을 허용하시겠습니까?'에서 '허용'을 눌러 주세요"
        if devices:
            return False, f"휴대폰 상태: {devices[0][1]} — 케이블을 다시 꽂아 보세요"
        return False, "연결된 휴대폰이 없음 — USB 케이블 연결과 'USB 디버깅' 켜기를 확인하세요"

    def is_ready(self) -> bool:
        try:
            return self.status()[0]
        except Exception:
            return False

    def dump(self) -> str:
        path = "/sdcard/radio_helper_ui.xml"
        self.shell("uiautomator", "dump", path, timeout=20)
        return self.shell("cat", path)

    def screen_png(self) -> bytes | None:
        """지금 휴대폰 화면 (PNG). 못 찍으면 None."""
        if not self.adb:
            return None
        cmd = [self.adb] + (["-s", self.serial] if self.serial else []) + ["exec-out", "screencap", "-p"]
        try:
            code, data = self.run(cmd, 20)
        except Exception:
            return None
        return data if code == 0 and data.startswith(b"\x89PNG") else None

    def screenshot(self, name: str, data: bytes | None = None) -> str | None:
        if not self.shots_dir:
            return None
        data = data or self.screen_png()
        if not data:
            return None
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        (self.shots_dir / f"{name}.png").write_bytes(data)
        return f"{name}.png"

    def info(self) -> dict:
        model = self.shell("getprop", "ro.product.model").strip()
        try:
            app = self.shell("cmd", "role", "get-role-holders", "android.app.role.SMS").strip()
        except SmsError:
            app = self.shell("settings", "get", "secure", "sms_default_application").strip()
        return {"model": model, "sms_app": app}

    # 보내기
    def send(self, number: str, text: str, shot: str | None = None) -> SendResult:
        """보낸다. 작성 화면(받는 번호·글)과 보낸 뒤 화면을 찍어 결과의 shots 로 돌려준다 (증거 사진)."""
        ok, detail = self.status()
        if not ok:
            return SendResult("failed", detail)
        self.shell("input", "keyevent", "KEYCODE_WAKEUP")
        self.shell("wm", "dismiss-keyguard")
        self.shell("am", "start", "-a", "android.intent.action.SENDTO", "-d", sms_uri(number), "--es", "sms_body", text)
        self.sleep(2.5)
        xml = self.dump()
        shots = []
        composed = self.screen_png()
        if composed:
            shots.append(("문자 작성 화면 (받는 번호·글)", composed))
            if shot:
                self.screenshot(shot, composed)
        if self.verify_number and not shows_number(xml, number):
            return SendResult("failed", f"작성 화면에서 받는 번호 {number} 를 확인하지 못해 보내지 않았습니다 "
                                        "(화면 잠금·다른 앱·번호 저장 이름 확인)", shots)
        point = find_send_button(xml)
        if point is None:
            return SendResult("failed", "문자 앱의 전송 버튼을 찾지 못해 보내지 않았습니다 (휴대폰 화면 잠금을 풀어 두세요)",
                              shots)
        self.shell("input", "tap", str(point[0]), str(point[1]))
        self.sleep(2.0)
        try:
            after = compose_text(self.dump())
        except SmsError:
            after = None
        sent = self.screen_png()
        if sent:
            shots.append(("문자 보낸 뒤 화면", sent))
            if shot:
                self.screenshot(shot + "_sent", sent)
        if after is not None and text[:10] and text[:10] in after:
            return SendResult("unknown", "전송 버튼을 눌렀지만 입력칸에 글이 남아 있음 — 휴대폰 확인 필요", shots)
        return SendResult("entered", f"휴대폰 문자로 {number} 에 보냄", shots)
