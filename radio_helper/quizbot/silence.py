"""듣는 도중 소리가 끊기면 알아채고, 끊기기 직전에 도우미가 무엇을 했는지 함께 남긴다.

라디오(고릴라)가 멈췄을 때 원인을 가리기 위한 기록이다.
  - 끊기기 직전에 도우미가 고릴라 창을 눌렀다면(채팅 전송) → 그 클릭·키 입력이 원인일 수 있음
  - 도우미가 아무것도 하지 않았다면 → 고릴라 앱(재생 멈춤·방송 끊김)·인터넷·스피커 쪽
  - 기본 스피커가 바뀌었다면 → 고릴라 소리가 다른 장치로 나가는 중 (녹음도 새 장치로 옮긴다)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

SILENT_LEVEL = 5        # 1초 소리 크기(0~100)가 이 이하면 '소리 없음' (약 -57dB)
ALERT_SECONDS = 20      # 소리를 듣다가 이만큼 계속 조용하면 '끊김'으로 기록
TOUCH_LOOKBACK = 90     # 끊기기 전 이 시간(초) 안에 도우미가 한 일을 함께 적는다
RECHECK_SECONDS = 60    # 끊긴 동안 기본 스피커가 바뀌었는지 다시 보는 간격
MAX_TOUCHES = 20


@dataclass
class SilenceWatch:
    """1초마다 소리 크기를 받아 '끊김'과 '다시 들림'을 알려 준다 (처음부터 조용한 것은 끊김이 아님)."""
    silent_since: datetime | None = None
    heard: bool = False
    alerted: bool = False
    last_check: datetime | None = None
    touches: list[tuple[datetime, str]] = field(default_factory=list)

    def feed(self, now: datetime, level: int) -> tuple[str, float] | None:
        """('cut', 조용한 초) / ('back', 끊겼던 초) / None"""
        if level > SILENT_LEVEL:
            back = ("back", (now - self.silent_since).total_seconds()) \
                if self.alerted and self.silent_since is not None else None
            self.heard, self.silent_since, self.alerted = True, None, False
            return back
        if self.silent_since is None:
            self.silent_since = now
        quiet = (now - self.silent_since).total_seconds()
        if self.heard and not self.alerted and quiet >= ALERT_SECONDS:
            self.alerted, self.last_check = True, now
            return ("cut", quiet)
        return None

    def due_recheck(self, now: datetime) -> bool:
        """끊긴 채로 RECHECK_SECONDS 가 지났으면 기본 스피커를 다시 볼 때."""
        if self.alerted and self.last_check is not None and (now - self.last_check).total_seconds() >= RECHECK_SECONDS:
            self.last_check = now
            return True
        return False

    def touch(self, at: datetime, what: str) -> None:
        """도우미가 채팅 앱 창을 건드린 일 (전송·채팅창 사진)."""
        self.touches.append((at, what))
        del self.touches[:-MAX_TOUCHES]

    def touches_before(self, since: datetime) -> list[tuple[datetime, str]]:
        return [(t, w) for t, w in self.touches if 0 <= (since - t).total_seconds() <= TOUCH_LOOKBACK]


def cut_message(since: datetime, quiet: float, touches: list[tuple[datetime, str]], notes: str = "") -> str:
    head = f"소리 끊김 — {since:%H:%M:%S}부터 {quiet:.0f}초째 소리가 없습니다."
    if touches:
        did = ", ".join(f"{t:%H:%M:%S} {w}" for t, w in touches)
        body = (f" 끊기기 직전 도우미가 한 일: {did}. 고릴라 재생이 멈췄다면 이 동작이 원인일 수 있습니다 — "
                "고릴라 창의 재생 버튼·채널을 확인하고, '고릴라·인식 설정'에서 위치를 다시 지정하세요.")
    else:
        body = (f" 그 전 {TOUCH_LOOKBACK}초 동안 도우미는 고릴라 창을 건드리지 않았습니다 → 고릴라 앱(재생 멈춤·"
                "방송 끊김 안내·로그아웃)·인터넷·스피커 쪽을 확인하세요.")
    return head + body + notes


def back_message(quiet: float) -> str:
    return f"소리 다시 들림 — 약 {quiet:.0f}초 끊겼습니다."


def speaker_note(before: str, after: str) -> str:
    if before and after and before != after:
        return (f" 기본 스피커가 '{before}' → '{after}'(으)로 바뀌었습니다 — 고릴라 소리가 새 장치로 나가고 있을 수 "
                "있습니다(이어폰·블루투스 연결이 끊기면 생김). 녹음도 새 장치로 옮겼습니다.")
    return ""


def banner(silent_since: str | None) -> str:
    """관리 화면·자막 창에 띄우는 한 줄. silent_since: 'YYYY-MM-DD HH:MM:SS'"""
    if not silent_since:
        return ""
    return f"소리 끊김: {silent_since[11:19]}부터 소리가 없습니다 — 고릴라가 재생 중인지, 스피커가 바뀌지 않았는지 확인하세요"
