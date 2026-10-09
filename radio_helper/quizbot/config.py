"""퀴즈 자동 참여 설정 (settings 테이블의 quizbot.* / gorilla.* 키)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from datetime import datetime, timedelta

from .. import db

DEFAULTS = {
    # 녹음·인식
    "quizbot.chunk_seconds": "10",
    "quizbot.whisper_model": "auto",      # auto: 그래픽카드 large-v3 / CPU large-v3-turbo (코어 16개 미만 small)
    "quizbot.whisper_device": "auto",     # auto: 별도 프로세스로 그래픽카드를 점검해 통과하면 그래픽카드, 아니면 CPU
    "quizbot.whisper_beam": "5",          # 받아쓰기 후보 수 (느리면 1)
    "quizbot.stt_overlap": "3",           # 겹쳐 듣기: 조각 끝 이 초 안에서 시작한 말은 다음 조각과 이어서 받아씀 (0=끔)
    "quizbot.stt_hints": "",              # 음성 인식 단어 힌트 (자주 틀리는 이름을 쉼표로)
    "quizbot.context_seconds": "180",     # 분석에 넘기는 최근 녹취 길이
    "quizbot.settle_seconds": "40",       # 퀴즈 신호 뒤 문제를 끝까지 듣고 분석하기까지 기다리는 시간
    "quizbot.cooldown_seconds": "60",
    "quizbot.max_analyses_per_window": "15",
    "quizbot.max_sends_per_hour": "4",
    # 기발한 오답: 정답 대신 '웃긴 포인트'가 있는 오답을 섞어 보낸다
    "quizbot.witty_ratio": "0.3",         # 정답이 확실해도 이 비율만큼은 기발한 오답으로 (0~1)
    "quizbot.min_wit_score": "0.7",       # 이 정도 이상 웃기거나 기발할 때만 오답을 씀
    "quizbot.chat_shots": "3",            # 분석 한 번에 함께 보내는 채팅창 사진 수 (0이면 녹취만)
    # 사연·주제 모집
    "quizbot.story_settle_seconds": "30",
    "quizbot.story_cooldown_seconds": "600",
    "quizbot.max_story_analyses_per_window": "4",
    "quizbot.max_story_sends_per_hour": "2",
    # 선물 정보
    "quizbot.gift_settle_seconds": "30",
    "quizbot.gift_cooldown_seconds": "600",
    "quizbot.max_gift_analyses_per_window": "4",
    # 청취 시작 (한 번에 켜기)
    "live.active": "0",
    "live.channel": "파워FM",
    "live.auto_quiz": "1",                # 퀴즈 정답 자동 전송
    "live.min_confidence": "0.8",
    "live.auto_story": "1",               # 사연: 검사를 모두 통과한 초안은 자동 전송
    "live.gift": "1",                     # 선물 정보 기록
    "live.witty": "1",                    # 퀴즈에 기발한 오답 섞기
    "live.choice_always": "1",            # 보기가 있는 단순 퀴즈(객관식·OX)는 확신도와 상관없이 정답 후보를 보냄
    "live.captions": "1",                 # 자막 창 띄우기
    # 분석 (Claude API)
    "quizbot.model": "claude-opus-5-5",
    "quizbot.effort": "medium",
    "quizbot.autostart": "0",
    # 고릴라 화면
    "gorilla.process_name": "",           # '자동 찾기'·'위치 지정'으로 저장되는 고릴라 프로그램(실행 파일) 이름
    "gorilla.window_size": "",            # 위치 지정 때 잰 창 크기 "너비,높이"
    "gorilla.screen_region": "",          # 고릴라 창 영역을 네모로 지정했을 때 화면 좌표 "왼,위,오른,아래"
    "gorilla.region_window": "",          # 그때 그 자리에 있던 고릴라 창의 화면 좌표 (창이 옮겨졌는지 확인용)
    # 모니터 기준 위치 (입력칸·전송 버튼 영역을 지정하면 저장, input_mode/send_mode = screen 일 때 그 자리를 그대로 누름)
    "gorilla.input_point": "",            # 입력칸 네모 가운데의 모니터 좌표 "x,y"
    "gorilla.send_point": "",             # 전송 버튼 네모 가운데의 모니터 좌표 "x,y"
    "gorilla.point_window": "",           # 지정할 때 그 자리에 있던 채팅 창의 모니터 좌표 (창이 옮겨졌는지 확인용)
    "gorilla.chat_screen": "",            # 채팅 목록 네모의 모니터 좌표 "왼,위,오른,아래"
    "gorilla.input_rect": "",             # 입력칸 네모 (기준 대비 비율 "x1,y1,x2,y2")
    "gorilla.send_rect": "",              # 전송 버튼 네모
    "gorilla.chat_rect": "",              # 채팅 목록 네모 (키워드가 들리면 읽어서 녹취와 함께 분석)
    "gorilla.window_title": "고릴라|gorealra",
    "gorilla.input_mode": "uia",          # uia: 화면 요소로 입력칸 찾기 / screen: 모니터의 지정한 자리 클릭 / coords: 창 안 비율 위치
    "gorilla.input_auto_id": "",
    "gorilla.input_name": "공감로그|글쓰기",
    "gorilla.send_mode": "auto",          # auto(전송 버튼, 없으면 Enter) / button / enter / screen / coords
    "gorilla.send_button_name": "전송|보내기|등록",
    "gorilla.input_x": "",
    "gorilla.input_y": "",
    "gorilla.send_x": "",
    "gorilla.send_y": "",
    "gorilla.message_template": "{answer}",
    "gorilla.test_message": "파워 FM 화이팅",   # '전송 테스트'로 실제로 보내 보는 글
    # 보내는 방법: app(고릴라·mini·콩 앱 채팅, 무료) / sms(휴대폰 문자, 건당 요금)
    "send.route": "app",
    # 휴대폰 문자 (안드로이드 + USB)
    # 듣는 채널과 문자 번호 (한 줄에 '채널=문자번호', 번호를 모르면 비움). 첫 화면 '채널 추가'로도 늘어난다
    # 앱은 이름으로 정해진다 (SBS·파워FM·러브FM·고릴라M → 고릴라, MBC → mini, KBS → 콩). '채널=번호=앱'으로 직접 정할 수도 있다
    "channels": "파워FM=#1077\n러브FM=#1035\n고릴라M=\nMBC FM4U=#8000\nKBS 쿨FM=#8910\nKBS 해피FM=#1061\nKBS 1라디오=",
    "sms.signature": "1",                 # 끝에 방송용 별명 붙이기
    "sms.quiz_template": "정답 {answer}",
    "sms.verify_number": "1",             # 작성 화면에 받는 번호(#포함)가 보일 때만 보냄
    "sms.adb_path": "",                   # 비우면 이 프로그램 폴더의 platform-tools 등에서 찾음
}

LABELS = {
    "quizbot.chunk_seconds": "녹음 단위(초)",
    "quizbot.whisper_model": "음성 인식 모델 (auto / large-v3 / large-v3-turbo / medium / small / base)",
    "quizbot.whisper_device": "음성 인식 장치 (auto 권장 / cpu / cuda)",
    "quizbot.whisper_beam": "받아쓰기 후보 수 (1~5, 느리다는 안내가 뜨면 1)",
    "quizbot.stt_overlap": "겹쳐 듣기(초, 0=끔)",
    "quizbot.stt_hints": "음성 인식 단어 힌트 (자주 틀리는 이름을 쉼표로)",
    "quizbot.context_seconds": "분석에 쓰는 최근 녹취(초)",
    "quizbot.settle_seconds": "퀴즈 신호 후 대기(초)",
    "quizbot.cooldown_seconds": "분석 간 최소 간격(초)",
    "quizbot.max_analyses_per_window": "예약 1회당 최대 분석 횟수(비용 상한)",
    "quizbot.max_sends_per_hour": "시간당 최대 퀴즈 전송 수",
    "quizbot.story_settle_seconds": "사연 모집 신호 후 대기(초)",
    "quizbot.story_cooldown_seconds": "사연 분석 간 최소 간격(초)",
    "quizbot.max_story_analyses_per_window": "예약 1회당 최대 사연 분석 횟수",
    "quizbot.max_story_sends_per_hour": "시간당 최대 사연 전송 수",
    "quizbot.gift_settle_seconds": "선물 안내 신호 후 대기(초)",
    "quizbot.gift_cooldown_seconds": "선물 분석 간 최소 간격(초)",
    "quizbot.max_gift_analyses_per_window": "예약 1회당 최대 선물 분석 횟수",
    "quizbot.witty_ratio": "기발한 오답 비율 (0~1, 정답이 확실할 때도)",
    "quizbot.min_wit_score": "기발한 오답 최소 점수 (0~1)",
    "quizbot.chat_shots": "분석 때 함께 보는 채팅창 사진 수 (0=녹취만)",
    "quizbot.model": "Claude 모델",
    "quizbot.effort": "분석 노력 수준 (low/medium/high)",
    "gorilla.process_name": "고릴라 프로그램 이름 (자동 찾기로 채워짐)",
    "gorilla.window_title": "고릴라 창 제목(정규식, 프로그램 이름이 없을 때)",
    "gorilla.input_mode": "입력칸 찾기 (screen=모니터 위치 / uia / coords)",
    "gorilla.input_auto_id": "입력칸 AutomationId (uia)",
    "gorilla.input_name": "입력칸 이름 (uia)",
    "gorilla.send_mode": "전송 방법 (screen=모니터 위치 / auto / button / enter / coords)",
    "gorilla.send_button_name": "전송 버튼 이름(정규식)",
    "gorilla.message_template": "보낼 문구 ({answer} 자리에 정답)",
    "gorilla.test_message": "전송 테스트로 실제로 보낼 글",
    "channels": "채널 목록과 문자 번호 (한 줄에 '채널=번호', 모르면 번호 비움)",
    "sms.signature": "끝에 방송용 별명 붙이기 (1=예, 0=아니오)",
    "sms.quiz_template": "퀴즈 정답 문자 ({answer} 자리에 정답)",
    "sms.verify_number": "받는 번호 확인 후 보내기 (1 권장)",
    "sms.adb_path": "adb 위치 (비우면 자동으로 찾음)",
}

ROUTES = {"app": "앱 채팅", "sms": "문자"}

# 방송사 앱 채팅 (원리는 모두 같다: 앱 창의 입력칸에 글을 넣고 전송 버튼을 누른다)
CHAT_APPS = {
    "gorilla": {"label": "고릴라", "station": "SBS", "title": "고릴라|gorealra", "input": "공감로그|글쓰기",
                "test": "파워 FM 화이팅"},
    "mini": {"label": "mini", "station": "MBC", "title": r"\bmini\b|미니|MBC", "input": "메시지|채팅|글쓰기|댓글",
             "test": "MBC FM4U 화이팅"},
    "kong": {"label": "콩", "station": "KBS", "title": r"콩|\bkong\b|KBS", "input": "메시지|채팅|글쓰기|댓글",
             "test": "KBS 화이팅"},
}
APP_ALIASES = {"고릴라": "gorilla", "gorilla": "gorilla", "mini": "mini", "미니": "mini", "콩": "kong", "kong": "kong"}
_APP_FIELDS = {  # 앱마다 따로 저장하는 화면 설정 (고릴라는 기존 'gorilla.*' 키를 그대로 쓴다)
    "process_name": "", "window_size": "", "screen_region": "", "region_window": "", "input_rect": "", "send_rect": "",
    "chat_rect": "", "input_point": "", "send_point": "", "point_window": "", "chat_screen": "", "input_mode": "uia", "input_auto_id": "", "send_mode": "auto", "send_button_name": "전송|보내기|등록",
    "input_x": "", "input_y": "", "send_x": "", "send_y": "",
}
for _app, _meta in CHAT_APPS.items():
    if _app == "gorilla":
        continue
    for _f, _v in _APP_FIELDS.items():
        DEFAULTS[f"{_app}.{_f}"] = _v
    DEFAULTS[f"{_app}.window_title"] = _meta["title"]
    DEFAULTS[f"{_app}.input_name"] = _meta["input"]
    DEFAULTS[f"{_app}.test_message"] = _meta["test"]
    for _f in ("process_name", "window_title", "input_mode", "input_name", "send_mode", "send_button_name", "test_message"):
        LABELS[f"{_app}.{_f}"] = LABELS.get(f"gorilla.{_f}", _f).replace("고릴라", _meta["label"])


def route(conn: sqlite3.Connection) -> str:
    r = get(conn, "send.route")
    return r if r in ROUTES else "app"   # 예전 값 'gorilla' 도 앱 채팅


def app_label(app: str | None) -> str:
    return CHAT_APPS[app]["label"] if app in CHAT_APPS else "앱"


def app_settings(app: str, settings: dict) -> dict:
    """화면 도구가 돌려주는 'gorilla.*' 설정 키를 그 앱의 키로 바꾼다."""
    if app == "gorilla":
        return settings
    return {(f"{app}." + k[len("gorilla."):] if k.startswith("gorilla.") else k): v for k, v in settings.items()}


def localize(app: str, message: str) -> str:
    return message if app == "gorilla" else message.replace("고릴라", app_label(app))


SBS_CHANNELS = ("파워FM", "러브FM", "고릴라M")


BROADCASTERS = ("SBS", "MBC", "KBS")


def broadcaster_of(channel: str | None) -> str:
    """채널 → 방송국 (SBS·MBC·KBS). 모르는 채널은 채널 이름 그대로."""
    name = (channel or "").strip()
    upper = name.upper()
    if not name or name in SBS_CHANNELS or upper.startswith("SBS"):
        return "SBS"   # 채널을 적지 않던 예전 기록은 모두 SBS(고릴라)
    for b in ("MBC", "KBS"):
        if upper.startswith(b):
            return b
    return name


def chat_app(conn: sqlite3.Connection, channel: str | None) -> str | None:
    """채널의 채팅 앱: '채널=번호=앱'으로 정했으면 그것, 아니면 이름으로 (SBS→고릴라, MBC→mini, KBS→콩)."""
    if not channel:
        return None
    for line in get(conn, "channels").splitlines():
        parts = [p.strip() for p in line.split("=")]
        if len(parts) >= 3 and parts[0] == channel and parts[2]:
            return APP_ALIASES.get(parts[2].lower(), APP_ALIASES.get(parts[2]))
    name = channel.upper()
    if channel in SBS_CHANNELS or name.startswith("SBS"):
        return "gorilla"
    if name.startswith("MBC"):
        return "mini"
    if name.startswith("KBS"):
        return "kong"
    return None


def channels(conn: sqlite3.Connection) -> dict[str, str]:
    """{채널: 문자 번호(없으면 '')} — 설정 순서대로."""
    from .sms import parse_channels

    return parse_channels(get(conn, "channels"))


def sms_number(conn: sqlite3.Connection, channel: str | None) -> str | None:
    return channels(conn).get(channel or "") or None


def get(conn: sqlite3.Connection, key: str) -> str:
    return db.get_setting(conn, key, DEFAULTS.get(key, ""))


def get_int(conn: sqlite3.Connection, key: str) -> int:
    try:
        return int(float(get(conn, key)))
    except ValueError:
        return int(DEFAULTS[key])


STT_MIGRATED = "meta.stt_auto_migrated"


def migrate_stt_defaults(conn: sqlite3.Connection) -> bool:
    """예전 기본값(small · cpu)을 쓰던 PC는 업데이트 후 처음 한 번만 auto 로 바꾼다. 직접 고른 다른 값은 그대로."""
    if db.get_setting(conn, STT_MIGRATED) == "1":
        return False
    changed = False
    for key, old in (("quizbot.whisper_model", "small"), ("quizbot.whisper_device", "cpu")):
        if db.get_setting(conn, key) == old:
            db.set_setting(conn, key, "auto")
            changed = True
    db.set_setting(conn, STT_MIGRATED, "1")
    if changed:
        db.log(conn, "quizbot", "음성 인식 설정을 자동(auto)으로 바꿨습니다 — 예전 기본값(small · CPU)을 쓰던 경우만")
    return changed


def get_float(conn: sqlite3.Connection, key: str) -> float | None:
    v = get(conn, key)
    try:
        return float(v) if v != "" else None
    except ValueError:
        return None


@dataclass
class GorillaConfig:
    process_name: str = ""
    window_size: str = ""
    screen_region: str = ""
    region_window: str = ""
    input_point: str = ""
    send_point: str = ""
    point_window: str = ""
    chat_screen: str = ""
    input_rect: str = ""
    send_rect: str = ""
    chat_rect: str = ""
    window_title: str = DEFAULTS["gorilla.window_title"]
    input_mode: str = "uia"
    input_auto_id: str = ""
    input_name: str = DEFAULTS["gorilla.input_name"]
    send_mode: str = "auto"
    send_button_name: str = DEFAULTS["gorilla.send_button_name"]
    input_x: float | None = None
    input_y: float | None = None
    send_x: float | None = None
    send_y: float | None = None
    message_template: str = "{answer}"

    app: str = "gorilla"   # gorilla(SBS 고릴라) / mini(MBC) / kong(KBS 콩)

    @property
    def label(self) -> str:
        return app_label(self.app)

    @property
    def default_title(self) -> str:
        return CHAT_APPS.get(self.app, CHAT_APPS["gorilla"])["title"]

    @classmethod
    def load(cls, conn: sqlite3.Connection, app: str = "gorilla") -> "GorillaConfig":
        values = {"app": app}
        for f in fields(cls):
            if f.name == "app":
                continue
            key = f"{app}.{f.name}"
            if f.name == "message_template":
                key = "gorilla.message_template"  # 퀴즈 정답 문구는 앱 공통
            values[f.name] = get_float(conn, key) if f.name.endswith(("_x", "_y")) else get(conn, key)
        return cls(**values)


def format_message(template: str, answer: str) -> str:
    template = template or "{answer}"
    if "{answer}" not in template:
        template = template + " {answer}"
    return template.replace("{answer}", answer.strip()).strip()


HEARTBEAT_FRESH_SECONDS = 180  # 음성 인식·분석 한 번에 수십 초가 걸릴 수 있다


def runner_alive(conn: sqlite3.Connection, now: datetime | None = None) -> bool:
    hb = db.get_setting(conn, "quizbot.heartbeat")
    state = db.get_setting(conn, "quizbot.state")
    if not hb or state in ("멈춤", ""):
        return False
    try:
        age = (now or datetime.now()) - datetime.strptime(hb, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return age < timedelta(seconds=HEARTBEAT_FRESH_SECONDS)


def instance_lock(name: str):
    """같은 도구(실행기·자막 창)가 두 개 뜨지 않게 하는 잠금. 이미 실행 중이면 None.

    잠금은 프로세스가 끝나면 OS가 풀어 주므로, 갑자기 꺼져도 다음 실행을 막지 않는다.
    돌려받은 파일 객체를 프로세스가 끝날 때까지 들고 있어야 한다.
    """
    import sys

    f = open(db.data_dir() / f"{name}.lock", "a+")
    try:
        if sys.platform == "win32":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f
