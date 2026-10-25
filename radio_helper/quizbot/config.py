"""퀴즈 자동 참여 설정 (settings 테이블의 quizbot.* / gorilla.* 키)."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, fields
from datetime import datetime, timedelta

from .. import db

DEFAULTS = {
    # 녹음·인식
    "quizbot.chunk_seconds": "10",
    "quizbot.whisper_model": "auto",      # auto: 그래픽카드 large-v3 / CPU large-v3-turbo (코어 16개 미만 small)
    "quizbot.whisper_device": "auto",     # auto: 별도 프로세스로 그래픽카드를 점검해 통과하면 그래픽카드, 아니면 CPU
    "quizbot.whisper_beam": "auto",       # 받아쓰기 후보 수: auto = 그래픽카드 5 · CPU 1 (CPU 에서 5 는 녹음보다 느렸음)
    "quizbot.stt_overlap": "3",           # 겹쳐 듣기: 조각 끝 이 초 안에서 시작한 말은 다음 조각과 이어서 받아씀 (0=끔)
    "quizbot.stt_hints": "",              # 음성 인식 단어 힌트 (자주 틀리는 이름을 쉼표로)
    "quizbot.context_seconds": "180",     # 분석에 넘기는 최근 녹취 길이
    "quizbot.settle_seconds": "40",       # 퀴즈 신호 뒤 문제를 끝까지 듣고 분석하기까지 기다리는 시간
    "quizbot.cooldown_seconds": "90",     # 같은 퀴즈를 되풀이해 분석하지 않게 (60초일 때 1분마다 분석해 구독 한도를 빨리 씀)
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
    "quizbot.max_story_sends_per_hour": "0",   # 0 = 제한 없음 (0.66, 사용자 요청으로 1시간 상한 해제)
    # 선물 정보
    "quizbot.gift_settle_seconds": "30",
    "quizbot.gift_cooldown_seconds": "600",
    "quizbot.max_gift_analyses_per_window": "4",
    # 청취 시작 (한 번에 켜기)
    "live.active": "0",
    "live.ask": "0",                      # 1: 프로그램을 켰을 때 '뭐로 들으시겠어요?'에 아직 답하지 않음 (답하기 전에는 듣지 않음)
    "live.channel": "파워FM",
    "live.auto_quiz": "1",                # 퀴즈 정답 자동 전송
    "live.min_confidence": "0.8",
    "live.auto_story": "1",               # 사연: 검사를 모두 통과한 초안은 자동 전송
    "live.gift": "1",                     # 선물 정보 기록
    "live.witty": "1",                    # 퀴즈에 기발한 오답 섞기
    "live.sms_fallback": "0",             # (0.76 부터 쓰지 않음 — 문자로만 받는 방송이 없어 늘 채팅)
    "live.song_free": "1",                # 신청곡: 경험에 적힌 곡이 없거나 안 맞으면 상황에 맞는 곡을 알아서 고름
    "live.multi": "0",                    # 동시 청취: 보조 채널(live.lanes)도 함께 듣기 (lanes.py)
    "live.device": "",                    # 주 채널 녹음 장치 (비우면 기본 스피커)
    "live.device_mode": "0",              # 동시 청취를 칸에 적은 출력 장치로 녹음 (0 = 앱마다 소리를 직접 받음, 0.56)
    "live.lanes": "[]",                   # 보조 채널 [{"channel": "KBS 쿨FM", "device": "CABLE Input (VB-Audio Virtual Cable)"}, …]
    "live.dj_song": "1",                  # 프로그램마다 DJ 한마디를 읽고, 신청곡을 청하면 날씨·이슈에 맞는 곡을 한 번 이상 신청
    "live.chat_consensus": "1",           # 채팅에서 같은 답이 3개 이상이면 그 답(+어울리는 이유)으로 보냄
    "live.story_approved": "1",           # 승인한(확인·고친) 사연은 다시 확인받지 않음: AI 글에 경고가 나면 승인한 글 그대로 보냄
    "live.board_auto": "1",               # 방송에서 게시판에 남겨 달라는 사연: 검사 경고 없으면 자동 승인·입력·등록
    "live.quiz_always": "1",              # 퀴즈는 확신도와 상관없이 정답 후보를 보냄 (객관식·주관식 모두, 틀려도 손해 없음)
    "live.captions": "1",                 # 자막 창 띄우기
    "live.keep_awake": "1",               # 듣는 동안 화면 꺼짐·절전 막기 (모니터 스피커로 들을 때 화면이 꺼지면 소리도 끊김, 0.61)
    # 분석 (Claude) — 구독 한도·요금을 아끼려고 Sonnet·low (Opus·medium 일 때 몇 시간 만에 구독 한도에 걸렸음)
    "quizbot.model": "claude-sonnet-5-5",
    "quizbot.effort": "low",
    "quizbot.gpt_model": "gpt-4.1-mini",    # GPT(OpenAI API 키)로 쓸 때 모델 (0.67, 0.70 기본 gpt-4.1-mini)
    "quizbot.gpt_fallback": "0",            # Claude 로 안 될 때 이어서 쓸 GPT: 0 / chatgpt(구독) / openai(API 키)
    "quizbot.chatgpt_model": "gpt-6-luna",  # ChatGPT 구독(Codex)으로 쓸 때 모델 (0.72 기본: 가장 가벼운 gpt-6-luna, 비우면 Codex 기본)
    "quizbot.billing": "",                # 비우면 PC 는 subscription(Claude 구독, Claude Code 로그인) · 서버는 api(API 키, 충전 요금)
    "quizbot.billing_fallback": "0",      # 구독으로 안 될 때(한도·로그인 풀림) API 키로 이어서 (충전 요금, 켜야 함)
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
    "gorilla.exe_path": "",               # 채팅 앱 실행 파일 (꺼져 있으면 다시 켜기용, 위치 지정 때 저장)
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
    # 보내는 방법: app(고릴라·mini·콩·레인보우 앱 채팅, 무료) / sms(휴대폰 문자, 건당 요금)
    "send.route": "app",
    # 휴대폰 문자 (안드로이드 + USB)
    # 듣는 채널과 문자 번호 (한 줄에 '채널=문자번호', 번호를 모르면 비움). 첫 화면 '채널 추가'로도 늘어난다
    # 앱은 이름으로 정해진다 (SBS·파워FM·러브FM·고릴라M → 고릴라, MBC → mini, KBS → 콩). '채널=번호=앱'으로 직접 정할 수도 있다
    "channels": "파워FM=#1077\n러브FM=#1035\n고릴라M=\nMBC FM4U=#8000\nKBS 쿨FM=#8910\nKBS 해피FM=#1061\nKBS 1라디오=\n"
                "CBS 음악FM=#9390\nCBS 표준FM=",
    "sms.signature": "1",                 # 끝에 방송용 별명 붙이기
    "sms.quiz_template": "정답 {answer}",
    "sms.verify_number": "1",             # 작성 화면에 받는 번호(#포함)가 보일 때만 보냄
    "sms.adb_path": "",                   # 비우면 이 프로그램 폴더의 platform-tools 등에서 찾음
}

LABELS = {
    "quizbot.chunk_seconds": "녹음 단위(초)",
    "quizbot.whisper_model": "음성 인식 모델 (auto / large-v3 / large-v3-turbo / medium / small / base)",
    "quizbot.whisper_device": "음성 인식 장치 (auto 권장 / cpu / cuda)",
    "quizbot.whisper_beam": "받아쓰기 후보 수 (auto=그래픽카드 5·CPU 1, 또는 1~5. 녹음보다 느리면 저절로 줄임)",
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
    "quizbot.max_story_sends_per_hour": "시간당 최대 사연 전송 수 (0 = 제한 없음)",
    "quizbot.gift_settle_seconds": "선물 안내 신호 후 대기(초)",
    "quizbot.gift_cooldown_seconds": "선물 분석 간 최소 간격(초)",
    "quizbot.max_gift_analyses_per_window": "예약 1회당 최대 선물 분석 횟수",
    "quizbot.witty_ratio": "기발한 오답 비율 (0~1, 정답이 확실할 때도)",
    "quizbot.min_wit_score": "기발한 오답 최소 점수 (0~1)",
    "quizbot.chat_shots": "분석 때 함께 보는 채팅창 사진 수 (0=녹취만)",
    "quizbot.model": "Claude 모델 (claude-sonnet-5-5 권장 · 더 정확하게: claude-opus-5-5, 구독 한도를 빨리 씀)",
    "quizbot.effort": "분석 노력 수준 (low 권장 / medium / high — 높을수록 한도를 빨리 씀)",
    "quizbot.gpt_model": "GPT 모델 (OpenAI API 키로 쓸 때, 기본 gpt-4.1-mini · 예: gpt-4o-mini · gpt-5)",
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
    # 0.55: CBS 레인보우 (PC 레인보우 — 세로 창 아래쪽 '채팅을 입력하세요' 칸 + 파란 전송 단추)
    "rainbow": {"label": "레인보우", "station": "CBS", "title": r"레인보우|rainbow|\bCBS\b",
                "input": "채팅|토크|댓글|메시지|글쓰기|사연", "test": "CBS 화이팅"},
}
APP_ALIASES = {"고릴라": "gorilla", "gorilla": "gorilla", "mini": "mini", "미니": "mini", "콩": "kong", "kong": "kong",
               "레인보우": "rainbow", "rainbow": "rainbow"}
_APP_FIELDS = {  # 앱마다 따로 저장하는 화면 설정 (고릴라는 기존 'gorilla.*' 키를 그대로 쓴다)
    "process_name": "", "window_size": "", "screen_region": "", "region_window": "", "input_rect": "", "send_rect": "",
    "chat_rect": "", "input_point": "", "send_point": "", "point_window": "", "chat_screen": "", "exe_path": "", "input_mode": "uia", "input_auto_id": "", "send_mode": "auto", "send_button_name": "전송|보내기|등록",
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


BROADCASTERS = ("SBS", "MBC", "KBS", "CBS")
STATION_APPS = {"MBC": "mini", "KBS": "kong", "CBS": "rainbow"}   # SBS 는 고릴라
NEW_CHANNELS = ("CBS 음악FM=#9390", "CBS 표준FM=")   # 0.55 에 기본 목록에 더한 채널 (EBS FM·반디는 0.56 에 뺌)
AUTO_EBS = "EBS FM=#1045"                             # 0.55 가 저절로 더했던 줄


# 0.83 사용자 확인 (화면 사진): CBS 레인보우는 글이 입력칸 한 칸(보이는 4줄 정도, 한 줄 17자 안팎)을 넘어가면
# 전송이 안 됨 → 레인보우 채팅 글은 이 글자 수 안에서 만들고, 넘는 글은 문장 단위로 줄여 보낸다.
APP_CHAT_LIMITS = {"rainbow": 60}
SONG_TAIL = re.compile(r"\s*\(신청곡:.*\)\s*$")


def fit_text(text: str, limit: int) -> str:
    """글을 limit 자 안으로: 넘으면 신청곡 꼬리부터 빼고, 그래도 넘으면 뒤 문장부터 뺀다 (문장 하나가 넘으면 그 문장을 자름)."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    body = SONG_TAIL.sub("", text).strip()
    if len(body) <= limit:
        return body
    out = ""
    for sentence in re.findall(r"[^.!?…~]+[.!?…~]*\s*", body):
        if len((out + sentence).strip()) > limit:
            break
        out += sentence
    return out.strip() or body[:limit - 1].rstrip() + "…"


def chat_limit(conn: sqlite3.Connection, channel: str | None, default: int = 200) -> int:
    """채팅 사연 글자 수: 그 채널 채팅 앱 입력칸이 더 적게 받는다고 알아낸 수가 있으면 그것 (0.74), 아니면 default.
    레인보우처럼 한 칸을 넘으면 전송이 안 되는 앱은 APP_CHAT_LIMITS (0.83)."""
    app = chat_app(conn, channel) or "gorilla"
    default = min(default, APP_CHAT_LIMITS.get(app, default))
    try:
        learned = int(db.get_setting(conn, f"{app}.input_limit") or 0)
    except ValueError:
        learned = 0
    return min(default, learned) if learned >= 20 else default


def broadcaster_of(channel: str | None) -> str:
    """채널 → 방송국 (SBS·MBC·KBS·CBS). 모르는 채널은 채널 이름 그대로."""
    name = (channel or "").strip()
    upper = name.upper()
    if not name or name in SBS_CHANNELS or upper.startswith("SBS"):
        return "SBS"   # 채널을 적지 않던 예전 기록은 모두 SBS(고릴라)
    for b in BROADCASTERS[1:]:
        if upper.startswith(b):
            return b
    return name


def chat_app(conn: sqlite3.Connection, channel: str | None) -> str | None:
    """채널의 채팅 앱: '채널=번호=앱'으로 정했으면 그것, 아니면 이름으로
    (SBS→고릴라, MBC→mini, KBS→콩, CBS→레인보우)."""
    if not channel:
        return None
    for line in get(conn, "channels").splitlines():
        parts = [p.strip() for p in line.split("=")]
        if len(parts) >= 3 and parts[0] == channel and parts[2]:
            return APP_ALIASES.get(parts[2].lower(), APP_ALIASES.get(parts[2]))
    name = channel.upper()
    if channel in SBS_CHANNELS or name.startswith("SBS"):
        return "gorilla"
    return next((app for station, app in STATION_APPS.items() if name.startswith(station)), None)


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


DEFAULTS_MIGRATED = "meta.defaults_050"
# 0.50: 감지된 오류로 바꾼 기본값. 예전 기본값을 그대로 쓰던 PC 만 한 번 바꾼다 (직접 고른 다른 값은 그대로)
CHANGED_DEFAULTS = (
    ("quizbot.model", "claude-opus-5-5", "claude-sonnet-5-5"),   # 구독 한도(monthly spend limit)에 걸림
    ("quizbot.effort", "medium", "low"),
    ("quizbot.cooldown_seconds", "60", "90"),                     # 1분마다 같은 퀴즈를 다시 분석
    ("quizbot.whisper_beam", "5", "auto"),                        # 받아쓰기가 녹음보다 느림 (23초/10초)
)


FORCED_066 = "meta.defaults_066"
# 0.66: 사용자가 직접 정함 — 사연 1시간 상한 해제, 작업 모델은 Sonnet 5.5 · low (저장된 값과 상관없이 한 번 맞춤)
FORCED_SETTINGS_066 = (("quizbot.max_story_sends_per_hour", "0"), ("quizbot.model", "claude-sonnet-5-5"),
                       ("quizbot.effort", "low"))


def apply_066(conn: sqlite3.Connection) -> list[str]:
    if db.get_setting(conn, FORCED_066) == "1":
        return []
    changed = []
    for key, value in FORCED_SETTINGS_066:
        old = db.get_setting(conn, key, None)
        if old is not None and old != value:
            changed.append(f"{LABELS.get(key, key).split(' (')[0]} {old}→{value}")
        db.set_setting(conn, key, value)
    db.set_setting(conn, FORCED_066, "1")
    db.log(conn, "quizbot", "사연 1시간 상한을 풀고(제한 없음) 작업 모델을 Sonnet 5.5 · low 로 맞췄습니다"
           + (f": {', '.join(changed)}" if changed else ""))
    return changed


FORCED_070 = "meta.defaults_070"


def apply_070(conn: sqlite3.Connection) -> bool:
    """0.70: 사용자가 직접 정함 — GPT 기본 모델 gpt-4.1-mini ('GPT-4-mini' 라는 모델은 없어 가장 가까운 것). 한 번만."""
    if db.get_setting(conn, FORCED_070) == "1":
        return False
    old = db.get_setting(conn, "quizbot.gpt_model", None)
    db.set_setting(conn, "quizbot.gpt_model", "gpt-4.1-mini")
    db.set_setting(conn, FORCED_070, "1")
    if old and old != "gpt-4.1-mini":
        db.log(conn, "quizbot", f"GPT 모델을 gpt-4.1-mini 로 맞췄습니다 ({old}→gpt-4.1-mini)")
    return True


FORCED_072 = "meta.defaults_072"


def apply_072(conn: sqlite3.Connection) -> bool:
    """0.72: ChatGPT 구독(Codex) 모델이 비어 있으면(= Codex 기본 gpt-6.1-sol, 무거움) 가장 가벼운 gpt-6-luna 로. 한 번만.
    (사용자: 'GPT 사용량이 여전히 과도' — Codex 에는 gpt-4.1-mini 가 없음)"""
    if db.get_setting(conn, FORCED_072) == "1":
        return False
    db.set_setting(conn, FORCED_072, "1")
    if (db.get_setting(conn, "quizbot.chatgpt_model") or "").strip():
        return False
    db.set_setting(conn, "quizbot.chatgpt_model", "gpt-6-luna")
    db.log(conn, "quizbot", "ChatGPT 구독(Codex) 모델을 Codex 기본(gpt-6.1-sol)에서 가장 가벼운 gpt-6-luna 로 바꿨습니다")
    return True


def migrate_defaults(conn: sqlite3.Connection) -> list[str]:
    """예전 기본값을 그대로 쓰던 설정만 새 기본값으로 (한 번만). 바꾼 이름 목록."""
    apply_066(conn)
    apply_070(conn)
    apply_072(conn)
    if db.get_setting(conn, DEFAULTS_MIGRATED) == "1":
        return []
    changed = []
    for key, old, new in CHANGED_DEFAULTS:
        if db.get_setting(conn, key) == old:
            db.set_setting(conn, key, new)
            changed.append(f"{LABELS.get(key, key).split(' (')[0]} {old}→{new}")
    db.set_setting(conn, DEFAULTS_MIGRATED, "1")
    if changed:
        db.log(conn, "quizbot", "감지된 오류(구독 한도·느린 받아쓰기)에 맞춰 예전 기본값을 바꿨습니다: " + ", ".join(changed))
    return changed


CHANNELS_MIGRATED = "meta.channels_055"
EBS_DROPPED = "meta.channels_056"


def migrate_channels(conn: sqlite3.Connection) -> list[str]:
    """0.55: 채널 목록을 저장해 둔 PC 에 CBS 채널을 한 번 더한다 (이미 있거나 지운 뒤에는 건드리지 않음)."""
    drop_auto_ebs(conn)
    if db.get_setting(conn, CHANNELS_MIGRATED) == "1":
        return []
    db.set_setting(conn, CHANNELS_MIGRATED, "1")
    saved = db.get_setting(conn, "channels", None)
    if not saved:
        return []                       # 저장한 적이 없으면 기본 목록(이미 CBS 포함)을 씀
    have = {line.split("=")[0].strip() for line in saved.splitlines() if line.strip()}
    added = [line for line in NEW_CHANNELS if line.split("=")[0] not in have]
    if added:
        db.set_setting(conn, "channels", saved.rstrip("\n") + "\n" + "\n".join(added))
        db.log(conn, "quizbot", "채널 목록에 CBS 를 더했습니다: " + ", ".join(a.split("=")[0] for a in added))
    return added


def drop_auto_ebs(conn: sqlite3.Connection) -> bool:
    """0.56: 반디(EBS)를 쓰지 않기로 해, 0.55 가 저절로 더한 'EBS FM' 줄과 그 채널을 넣은 동시 청취 칸을 한 번 뺀다.
    사용자가 직접 넣은 EBS 채널(0.55 의 '더했습니다' 기록이 없을 때)은 그대로 둔다."""
    if db.get_setting(conn, EBS_DROPPED) == "1":
        return False
    db.set_setting(conn, EBS_DROPPED, "1")
    auto = conn.execute("SELECT 1 FROM events WHERE kind = 'quizbot' AND message LIKE '채널 목록에 CBS·EBS 를 더했습니다:%EBS FM%'"
                        ).fetchone()
    saved = db.get_setting(conn, "channels", None)
    if not auto or not saved or AUTO_EBS not in [line.strip() for line in saved.splitlines()]:
        return False
    db.set_setting(conn, "channels", "\n".join(line for line in saved.splitlines() if line.strip() != AUTO_EBS))
    try:
        lanes = json.loads(db.get_setting(conn, "live.lanes") or "[]")
        if isinstance(lanes, list):
            db.set_setting(conn, "live.lanes", json.dumps(
                [x if not (isinstance(x, dict) and x.get("channel") == "EBS FM") else {"channel": "", "device": ""}
                 for x in lanes], ensure_ascii=False))
    except ValueError:
        pass
    db.log(conn, "quizbot", "채널 목록에서 0.55 가 더한 EBS FM 을 뺐습니다 (반디는 쓰지 않음)")
    return True


BILLING_MIGRATED = "meta.billing_subscription"


def migrate_billing(conn: sqlite3.Connection, server_mode: bool) -> bool:
    """PC 는 Claude 구독이 기본 (0.51). 예전 기본값 'api' 가 저장돼 있던 PC 만 한 번 구독으로 바꾼다
    (예전 설정 화면은 저장할 때 기본값 api 를 같이 저장했음). 그 뒤에 API 키를 고르면 그대로 둔다."""
    if server_mode or db.get_setting(conn, BILLING_MIGRATED) == "1":
        return False
    db.set_setting(conn, BILLING_MIGRATED, "1")
    if db.get_setting(conn, "quizbot.billing") != "api":
        return False
    db.set_setting(conn, "quizbot.billing", "subscription")
    db.log(conn, "quizbot", "Claude 연결을 'Claude 구독(Claude Code 로그인)'으로 바꿨습니다 — PC 기본값. "
                            "API 키(충전 요금)로 쓰려면 설정 → Claude 연결에서 다시 고르세요.")
    return True


def whisper_beam(conn: sqlite3.Connection, device: str) -> int:
    """받아쓰기 후보 수: 숫자면 그대로(1~10), auto 면 그래픽카드 5 · CPU 1."""
    raw = (get(conn, "quizbot.whisper_beam") or "").strip().lower()
    if raw.isdigit():
        return max(1, min(10, int(raw)))
    return 5 if device == "cuda" else 1


# 받아쓰기가 녹음보다 느릴 때 한 단계씩 작은 모델로
SMALLER_MODEL = {"large-v3": "large-v3-turbo", "large-v3-turbo": "small", "medium": "small", "small": "base",
                 "base": "tiny"}


def stt_speedup(beam: int, model: str) -> tuple[str, str | None]:
    """느릴 때 다음 단계 (순수 계산): ('beam', None) 후보 수 1로 / ('model', 더 작은 모델) / ('none', None)."""
    if beam > 1:
        return "beam", None
    smaller = SMALLER_MODEL.get(model)
    return ("model", smaller) if smaller else ("none", None)


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
    exe_path: str = ""
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

    app: str = "gorilla"   # gorilla(SBS 고릴라) / mini(MBC) / kong(KBS 콩) / rainbow(CBS 레인보우)

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
