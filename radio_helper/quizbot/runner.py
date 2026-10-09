"""퀴즈 자동 참여 실행기.

예약 구간 동안: 고릴라 실행 확인 → 녹음 → 음성 인식 → 퀴즈 신호 → 분석 → 자동 전송 판단 → 전송 → 기록.

한 번 보낸(또는 보냈는지 모르는) 문제는 다시 보내지 않는다. 전송 직전에 상태를 '결과 불명'으로 먼저
바꿔 두므로, 전송 도중 프로그램이 멈춰도 재시작 후 같은 문제를 또 보내지 않는다.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Callable, Protocol

from .. import db, quiz
from . import config, live, silence, story
from . import sms as sms_mod
from . import answerer as answerer_mod
from .answerer import AnswererError, QuizAnalysis, StoryAnalysis
from .audio import rms
from .detector import Detector, TranscriptBuffer, is_gift_signal, is_story_signal
from .schedule import Window, active_window, next_window
from .stt import build_hints

SILENCE_RMS = 0.002
SILENT_CHUNKS_WARN = 4
SIMILAR_QUESTION = 0.85  # 짧은 한국어 문장은 0.6이면 다른 문제도 같다고 본다


def stt_hints(conn: sqlite3.Connection, program: str) -> str:
    """음성 인식 단어 힌트: 사용자 힌트 + 듣는 프로그램의 진행자·코너 이름 + 방송 낱말·초성 자음 이름."""
    names: list[str] = []
    row = conn.execute("SELECT host FROM programs WHERE title = ?", (program,)).fetchone()
    if row and row["host"]:
        names += [h.strip() for h in re.split(r"[,·/]", row["host"]) if h.strip() and "요일별" not in h]
    for r in conn.execute("SELECT title FROM corners WHERE program = ? ORDER BY is_target DESC, id LIMIT 12",
                          (program,)):
        if r["title"].startswith("(이름 확인 필요)"):
            continue
        title = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", r["title"]).strip(" -~!,.")
        if 1 < len(title) <= 20:
            names.append(title)
    return build_hints(names, config.get(conn, "quizbot.stt_hints"))
def app_hold(app: str) -> str:
    return f"대기: {config.app_label(app)} 창을 찾지 못함 — 찾으면 보냄"


GORILLA_HOLD = app_hold("gorilla")
APP_HOLDS = [app_hold(a) for a in config.CHAT_APPS]
SMS_HOLD = "대기: 휴대폰(USB)이 연결되지 않음 — 연결되면 보냄"
HELD_RETRY_MINUTES = 20  # 이보다 오래된 보류 글은 늦었으므로 자동으로 보내지 않는다


class Recorder(Protocol):
    def read_chunk(self): ...


class Transcriber(Protocol):
    def transcribe(self, audio) -> str: ...


class Answerer(Protocol):
    def analyze(self, program: str, transcript: str, known: list[tuple[int, str]]) -> QuizAnalysis: ...
    def analyze_story(self, program: str, transcript: str, profile: dict, experiences: list[dict]) -> StoryAnalysis: ...
    # 채팅창 사진이 있으면 analyze(..., images=[(시각, JPEG)]) / analyze_story(..., images=...) 로 함께 넘긴다
    def analyze_gifts(self, program: str, transcript: str) -> list[dict]: ...


class Sender(Protocol):
    def is_running(self) -> bool: ...
    def send(self, text: str): ...


@dataclass
class Deps:
    """실제 실행 때는 윈도우 녹음·Whisper·Claude·고릴라 구현을, 테스트 때는 가짜를 넣는다."""
    recorder_factory: Callable[[int], object]          # chunk_seconds → context manager with read_chunk()
    transcriber_factory: Callable[[str, str], Transcriber]  # (model_size, program_title)
    answerer: Answerer
    sender: Sender
    now: Callable[[], datetime] = datetime.now
    sleep: Callable[[float], None] = time.sleep
    lock: Callable[[], object] | None = None            # 입력 작업 직렬화 (웹 입력 도구와 공유)
    log: Callable[[str], None] = lambda m: print(m, flush=True)
    notify: Callable[[str], None] = lambda m: None      # 확인할 글이 생겼을 때 알림 (윈도우: 알림음)
    sms: object | None = None                           # 휴대폰 문자: is_ready(), send(number, text)
    chat_senders: dict | None = None                    # 앱별 채팅 전송 {"mini": ..., "kong": ...} (없으면 sender)


# ── 판단 ────────────────────────────────────────────────────────────
def decide(conn: sqlite3.Connection, q: sqlite3.Row, schedule: sqlite3.Row | None, now: datetime,
           route: str = "app") -> list[str]:
    """자동 전송을 막는 이유 목록. 비어 있으면 바로 보낸다. 문자로 보낼 때는 고릴라 응모 조건을 보지 않는다."""
    reasons = []
    if db.is_stopped(conn):
        reasons.append("일괄 중지가 켜져 있음")
    if q["entry_status"] != "pending":
        reasons.append(f"이미 처리됨({quiz.ENTRY_LABELS.get(q['entry_status'], q['entry_status'])})")
    if q["kind"] != "new":
        reasons.append("새 문제가 아님")
    witty = q["answer_kind"] == "witty"
    if not (q["send_text"] or (q["witty_answer"] if witty else q["answer"]) or "").strip():
        reasons.append("보낼 답이 없음" if witty else "정답 후보 없음")
    approved = bool(q["approved"])
    if not approved:
        if schedule is None or not schedule["auto_submit"]:
            reasons.append("이 예약은 자동 전송이 꺼져 있음 (확인 후 전송)")
        elif witty:
            min_wit = config.get_float(conn, "quizbot.min_wit_score") or 0.7
            if (q["wit_score"] or 0) < min_wit:
                reasons.append(f"기발한 정도 {q['wit_score'] or 0:.2f} < 기준 {min_wit:.2f}")
        elif (q["confidence"] or 0) < schedule["min_confidence"]:
            reasons.append(f"확신도 {q['confidence']:.2f} < 기준 {schedule['min_confidence']:.2f}")
        if route == "sms":
            pass
        elif q["gorilla_accepted"] == "no":
            reasons.append("진행자가 고릴라가 아닌 다른 방법(문자 등)으로 받는다고 함 — '문자로 보내기'를 누르면 보냄")
        elif q["gorilla_accepted"] != "yes" and not (schedule and schedule["gorilla_confirmed"]):
            reasons.append("고릴라 응모 인정 여부 미확인")
    limit = config.get_int(conn, "quizbot.max_sends_per_hour")
    since = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    sent = conn.execute("SELECT COUNT(*) FROM quizzes WHERE sent_at >= ?", (since,)).fetchone()[0]
    if sent >= limit:
        reasons.append(f"최근 1시간 전송 {sent}건으로 상한({limit}) 도달")
    return reasons


def find_duplicate(known: list[sqlite3.Row], analysis: QuizAnalysis) -> int | None:
    ids = {r["id"] for r in known}
    if analysis.duplicate_of and analysis.duplicate_of in ids:
        return analysis.duplicate_of
    target = quiz.normalize_question(analysis.question)
    if not target:
        return None
    for r in known:
        other = quiz.normalize_question(r["question"] or "")
        if not other:
            continue
        shorter = min(len(target), len(other))
        if shorter >= 6 and (target in other or other in target):
            return r["id"]
        if SequenceMatcher(None, target, other).ratio() >= SIMILAR_QUESTION:
            return r["id"]
    return None


def choose_answer(res: QuizAnalysis, min_confidence: float, ratio: float, min_wit: float, roll: float) -> str:
    """정답(correct)과 기발한 오답(witty) 중 무엇을 보낼지.

    - 웃긴 포인트가 있고 기준 이상으로 기발한 오답이 있을 때만 오답을 고려한다.
    - 진행자가 재밌는 오답을 환영하거나, 정답이 확실하지 않으면 오답을 보낸다.
    - 정답이 확실하면 ratio 비율(roll < ratio)만큼만 오답을 섞는다. ratio 0 이면 항상 정답.
    """
    if not (res.witty_answer and res.witty_point) or res.wit_score < min_wit or ratio <= 0:
        return "correct"
    if res.fun_welcome or not res.answer or res.confidence < min_confidence:
        return "witty"
    return "witty" if roll < ratio else "correct"


# ── 실행기 ──────────────────────────────────────────────────────────
@dataclass
class Runner:
    conn: sqlite3.Connection
    deps: Deps
    analyses: int = 0
    story_analyses: int = 0
    gift_analyses: int = 0

    def state(self, text: str) -> None:
        if text != getattr(self, "_last_state", None):  # 실행 기록 파일에도 남긴다 (바뀔 때만)
            self._last_state = text
            self.deps.log(f"[{self.deps.now():%H:%M:%S}] {text}")
        db.set_setting(self.conn, "quizbot.state", text)
        db.set_setting(self.conn, "quizbot.heartbeat", self.deps.now().strftime("%Y-%m-%d %H:%M:%S"))

    def event(self, message: str) -> None:
        db.log(self.conn, "quizbot", message)
        self.deps.log(f"[{self.deps.now():%H:%M:%S}] {message}")

    def should_stop(self) -> bool:
        return db.get_setting(self.conn, "quizbot.stop", "0") == "1"

    def schedules(self):
        return self.conn.execute(
            "SELECT s.*, p.title AS program, p.channel AS channel FROM quiz_schedules s JOIN programs p ON p.id = s.program_id").fetchall()

    def report_level(self, level: int) -> None:
        """녹음 중 약 1초마다 불린다. 소리 크기와 함께 '살아 있음' 신호도 갱신한다."""
        stamp = self.deps.now().strftime("%Y-%m-%d %H:%M:%S")
        db.set_setting(self.conn, "quizbot.level", str(level))
        db.set_setting(self.conn, "quizbot.level_at", stamp)
        db.set_setting(self.conn, "quizbot.heartbeat", stamp)

    # ── 소리 끊김 감시 ───────────────────────────────────────────
    def on_second(self, level: int) -> None:
        """녹음 중 약 1초마다: 소리 크기 표시 + 끊김 감시."""
        self.report_level(level)
        self.watch_level(self.deps.now(), level)

    def watch_level(self, now: datetime, level: int) -> None:
        watch = getattr(self, "silence", None)
        if watch is None:
            return
        since = watch.silent_since or now
        got = watch.feed(now, level)
        if got:
            self._silence_events.append((got[0], got[1], since))

    def touch(self, what: str) -> None:
        """도우미가 채팅 앱 창을 건드린 일을 남긴다 (소리가 끊기면 '직전에 한 일'로 함께 기록)."""
        watch = getattr(self, "silence", None)
        if watch is not None:
            watch.touch(self.deps.now(), what)

    def speaker_check(self, recorder) -> str:
        """녹음 장치를 다시 열어 기본 스피커가 바뀌었는지 본다 (바뀌었으면 새 장치로 녹음)."""
        reopen = getattr(recorder, "reopen", None)
        if reopen is None:
            return ""
        before = getattr(recorder, "device_name", "")
        try:
            after = reopen()
        except Exception as e:
            return f" 녹음 장치를 다시 열지 못했습니다({type(e).__name__}: {str(e)[:80]}) — 스피커 연결을 확인하세요."
        return silence.speaker_note(before, after)

    def handle_silence(self, recorder, now: datetime) -> None:
        events, self._silence_events = self._silence_events, []
        for kind, quiet, since in events:
            if kind == "cut":
                notes = self.speaker_check(recorder)
                if live.stt_test_alive():
                    notes += " (받아쓰기 테스트가 함께 돌고 있었습니다.)"
                self.event(silence.cut_message(since, quiet, self.silence.touches_before(since), notes))
                db.set_setting(self.conn, "quizbot.silent_since", since.strftime("%Y-%m-%d %H:%M:%S"))
            else:
                self.event(silence.back_message(quiet))
                db.set_setting(self.conn, "quizbot.silent_since", "")
        if self.silence.due_recheck(now):
            note = self.speaker_check(recorder)
            if note:
                self.event("소리 끊김 중 확인:" + note)

    def run_forever(self, idle_seconds: int = 5) -> None:
        self.state("시작함")
        while not self.should_stop():
            now = self.deps.now()
            if live.is_active(self.conn):
                self._guarded(self.run_live)
                continue
            schedules = self.schedules()
            w = active_window(schedules, now)
            if w is None:
                nxt = next_window(schedules, now)
                self.state(f"대기 중 — 다음 예약 {nxt.start:%m/%d %H:%M}" if nxt else "대기 중 — '청취 시작'을 누르세요")
                self.process_approved(None)
                self.deps.sleep(idle_seconds)
                continue
            self._guarded(lambda: self.run_window(w))
        db.set_setting(self.conn, "quizbot.stop", "0")
        self.state("멈춤")

    def _guarded(self, fn) -> None:
        try:
            fn()
        except Exception as e:  # 녹음 장치·모델 내려받기 등 실패 → 기록하고 30초 뒤 다시 시도
            msg = f"{type(e).__name__}: {str(e)[:150]}"
            db.log(self.conn, "quizbot", f"듣기 중 오류 — 30초 뒤 다시 시도: {msg}")
            self.state(f"오류 후 대기 중 — {msg}")
            self.deps.sleep(30)

    def run_window(self, w: Window) -> None:
        schedule = self.conn.execute(
            "SELECT s.*, p.title AS program, p.channel AS channel FROM quiz_schedules s "
            "JOIN programs p ON p.id = s.program_id WHERE s.id = ?", (w.schedule_id,)).fetchone()
        db.log(self.conn, "quizbot", f"{schedule['program']} 예약 시작 ({w.start:%H:%M}~{w.end:%H:%M})")
        self._listen(lambda now: (schedule, w), lambda now: now < w.end)

    def run_live(self) -> None:
        """'청취 시작': 예약과 상관없이 지금 듣는 채널을 '청취 중지'까지 듣는다."""
        def get_session(now):
            day = now.replace(hour=0, minute=0, second=0, microsecond=0)
            return live.session(self.conn, now), Window(None, day, day + timedelta(days=1))

        db.log(self.conn, "quizbot", f"청취 시작 — {config.get(self.conn, 'live.channel')}")
        self._listen(get_session, lambda now: live.is_active(self.conn), wait_for_gorilla=False)
        db.log(self.conn, "quizbot", "청취 멈춤")

    def _listen(self, get_session, keep_going, wait_for_gorilla: bool = True) -> None:
        """녹음 → 음성 인식 → 퀴즈·사연·선물 감지 → 분석 → 전송. get_session(now) → (설정, 구간).

        예약 듣기는 고릴라가 켜질 때까지 기다린다. '청취 시작'은 고릴라 창을 못 찾아도 듣기·자막을 계속하고,
        보낼 글은 고릴라 창을 찾을 때까지 보류한다.
        """
        session, w = get_session(self.deps.now())
        while wait_for_gorilla and not self.chat_ok(session["channel"]):
            if self.should_stop() or not keep_going(self.deps.now()):
                return
            label = config.app_label(config.chat_app(self.conn, session["channel"]))
            self.state(f"{session['program']} — {label}가 실행 중이 아님 (30초마다 확인)")
            self.deps.sleep(30)
        gorilla_ok = self.chat_ok(session["channel"])
        gorilla_checked = self.deps.now()
        if not gorilla_ok and config.route(self.conn) == "app":
            label = config.app_label(config.chat_app(self.conn, session["channel"]))
            db.log(self.conn, "quizbot", f"{label} 창을 찾지 못했습니다 — 듣기·자막은 계속하고, 보낼 글은 {label} 창을 찾으면 보냅니다.")

        config.migrate_stt_defaults(self.conn)
        model = config.get(self.conn, "quizbot.whisper_model")
        self.state(f"음성 인식 준비 중 ({config.get(self.conn, 'quizbot.whisper_device')} · {model}, "
                   "처음 한 번은 그래픽카드 점검과 모델 내려받기 약 1.6~3GB로 몇 분 걸릴 수 있음)")
        transcriber = self.deps.transcriber_factory(model, session["program"])
        if getattr(transcriber, "label", None):
            note = getattr(transcriber, "device_note", "")
            self.event(f"음성 인식 준비 완료 ({transcriber.label}, {transcriber.load_seconds:.1f}초"
                       + (f" · {note}" if note else "") + ")")
        transcribe = getattr(transcriber, "feed", None) or transcriber.transcribe  # 겹쳐 듣기가 되면 feed
        chunk_seconds = config.get_int(self.conn, "quizbot.chunk_seconds")
        context = config.get_int(self.conn, "quizbot.context_seconds")
        buffer = TranscriptBuffer()
        detector = Detector(settle_seconds=config.get_int(self.conn, "quizbot.settle_seconds"),
                            cooldown_seconds=config.get_int(self.conn, "quizbot.cooldown_seconds"))
        story_detector = Detector(settle_seconds=config.get_int(self.conn, "quizbot.story_settle_seconds"),
                                  cooldown_seconds=config.get_int(self.conn, "quizbot.story_cooldown_seconds"),
                                  signal=is_story_signal)
        gift_detector = Detector(settle_seconds=config.get_int(self.conn, "quizbot.gift_settle_seconds"),
                                 cooldown_seconds=config.get_int(self.conn, "quizbot.gift_cooldown_seconds"),
                                 signal=is_gift_signal)
        max_analyses = config.get_int(self.conn, "quizbot.max_analyses_per_window")
        max_story = config.get_int(self.conn, "quizbot.max_story_analyses_per_window")
        max_gift = config.get_int(self.conn, "quizbot.max_gift_analyses_per_window")
        current, silent, chunks, slow = None, 0, 0, 0
        self.shots: list[tuple[str, bytes]] = []   # 키워드가 들린 뒤 찍은 채팅창 사진 (녹취와 교차 분석)
        self._shot_error = False
        self.silence, self._silence_events = silence.SilenceWatch(), []
        db.set_setting(self.conn, "quizbot.silent_since", "")
        self.state("녹음 장치 여는 중")
        with self.deps.recorder_factory(chunk_seconds) as recorder:
            per_second = hasattr(recorder, "on_level")
            if per_second:
                recorder.on_level = self.on_second  # 녹음 중 1초마다 소리 크기를 화면에 알리고 끊김을 살핀다
            if getattr(recorder, "device_name", ""):
                self.event(f"녹음 시작 — {recorder.device_name}")
            while keep_going(self.deps.now()) and not self.should_stop():
                session, w = get_session(self.deps.now())
                program = session["program"]
                if program != current:
                    # 프로그램이 바뀌면 프로그램당 분석 상한을 새로 세고, 음성 인식 힌트(진행자·코너)를 바꾼다
                    current, self.analyses, self.story_analyses, self.gift_analyses = program, 0, 0, 0
                    if hasattr(transcriber, "set_context"):
                        transcriber.set_context(program, stt_hints(self.conn, program))
                if not wait_for_gorilla and (self.deps.now() - gorilla_checked).total_seconds() >= 60:
                    gorilla_checked, was_ok = self.deps.now(), gorilla_ok
                    gorilla_ok = self.chat_ok(session["channel"])
                    if gorilla_ok and not was_ok:
                        label = config.app_label(config.chat_app(self.conn, session["channel"]))
                        db.log(self.conn, "quizbot", f"{label} 창을 찾았습니다 — 보류한 글을 보냅니다.")
                needs_app = config.route(self.conn) == "app"
                label = config.app_label(config.chat_app(self.conn, session["channel"]))
                self.state(f"{program} 듣는 중 · 분석 퀴즈 {self.analyses}·사연 {self.story_analyses}·선물 {self.gift_analyses}회"
                           + (f" · {label} 창 못 찾음(전송 보류)" if needs_app and not gorilla_ok else ""))
                audio = recorder.read_chunk()
                now = self.deps.now()
                level = rms(audio)
                self.report_level(live.level_percent(level))
                if not per_second:
                    self.watch_level(now, live.level_percent(level))
                self.handle_silence(recorder, now)
                if level < SILENCE_RMS:
                    silent += 1
                    if silent == SILENT_CHUNKS_WARN and not self.silence.heard:  # 듣다가 끊긴 것은 위에서 따로 남긴다
                        db.log(self.conn, "quizbot", "소리가 들리지 않습니다. 고릴라 재생·음소거·기본 스피커를 확인하세요.")
                else:
                    silent = 0
                started = time.monotonic()
                text = transcribe(audio)
                took = time.monotonic() - started
                chunks += 1
                self.record_chunk(now, live.level_percent(level), took, text, first=chunks == 1)
                slow = slow + 1 if took > chunk_seconds * 1.5 else 0
                if slow == 3:
                    self.event(f"받아쓰기가 녹음보다 느립니다 ({took:.0f}초/{chunk_seconds}초). "
                               "고릴라·인식 설정에서 '받아쓰기 후보 수'를 1로 줄이세요. 그래도 느리면 모델을 small 로.")
                if text:
                    buffer.add(now, text)
                    self.conn.execute(
                        "INSERT INTO transcripts (schedule_id, broadcast_date, at, text, channel, program) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (w.schedule_id, w.broadcast_date, now.strftime("%Y-%m-%d %H:%M:%S"), text,
                         session["channel"], program))
                    self.conn.commit()
                    detector.feed(now, text)
                    if session["story_enabled"]:
                        story_detector.feed(now, text)
                    if session["gift_enabled"]:
                        gift_detector.feed(now, text)
                if detector.armed or story_detector.armed:
                    self.collect_chat(session["channel"], now)
                if session["gift_enabled"] and gift_detector.is_due(now):
                    gift_detector.mark_analyzed(now)
                    if self.gift_analyses < max_gift:
                        self.analyze_gifts(session, w, buffer.window(now, context))
                if session["story_enabled"] and story_detector.is_due(now):
                    story_detector.mark_analyzed(now)
                    if self.story_analyses < max_story:
                        self.analyze_story(session, w, buffer.window(now, context), self.shots_for_analysis())
                if detector.is_due(now):
                    detector.mark_analyzed(now)
                    if self.analyses < max_analyses:
                        self.analyze(session, w, buffer.window(now, context), self.shots_for_analysis())
                    elif self.analyses == max_analyses:
                        db.log(self.conn, "quizbot", f"{program}: 분석 횟수 상한({max_analyses}) 도달 — 이 프로그램에서는 더 분석하지 않음")
                        self.analyses += 1
                if not (detector.armed or story_detector.armed):
                    self.shots = []   # 다음 키워드가 들리면 새로 모은다
                self.process_approved(session)
                self.retry_held(session, gorilla_ok)
        db.set_setting(self.conn, "quizbot.silent_since", "")
        db.log(self.conn, "quizbot", f"{current or session['program']} 듣기 끝 · 분석 {min(self.analyses, max_analyses)}회")

    def record_chunk(self, now: datetime, level: int, took: float, text: str, first: bool = False) -> None:
        """화면의 '마지막 10초' 줄: 말소리가 없어도 녹음·받아쓰기가 돌고 있다는 것을 보여 준다."""
        db.set_setting(self.conn, "quizbot.last_chunk", json.dumps(
            {"at": now.strftime("%H:%M:%S"), "level": level, "seconds": round(took, 1), "text": text[:120]},
            ensure_ascii=False))
        if first:
            self.event(f"첫 받아쓰기 ({took:.1f}초): '{text[:60]}'" if text
                       else f"첫 받아쓰기 ({took:.1f}초): 말소리 없음 (소리 크기 {level}/100)")

    def collect_chat(self, channel: str | None, now: datetime) -> None:
        """채팅창 영역을 지정했으면 지금 화면을 찍어 모은다 (처음 1장 + 최근 몇 장)."""
        keep = config.get_int(self.conn, "quizbot.chat_shots")
        app = config.chat_app(self.conn, channel)
        sender = self.chat_sender(app) if app else None
        if keep <= 0 or sender is None or not hasattr(sender, "chat_image"):
            return
        if not config.get(self.conn, f"{app}.chat_rect"):
            return
        try:
            data = sender.chat_image(save_as=f"{app}_chat")
        except Exception as e:
            if not self._shot_error:
                self._shot_error = True
                self.event(f"{config.app_label(app)} 채팅창을 읽지 못했습니다 (녹취만으로 분석): {type(e).__name__}: {str(e)[:80]}")
            return
        if data:
            self.touch(f"{config.app_label(app)} 채팅창 사진(읽기만)")
            self.shots.append((now.strftime("%H:%M:%S"), data))
            if len(self.shots) > keep:
                self.shots = self.shots[:1] + self.shots[-(keep - 1):] if keep > 1 else self.shots[-1:]

    def shots_for_analysis(self) -> list[tuple[str, bytes]]:
        return list(getattr(self, "shots", []))

    def known_quizzes(self, program: str, date: str):
        return self.conn.execute(
            "SELECT * FROM quizzes WHERE program = ? AND broadcast_date = ? AND source = 'auto' ORDER BY id",
            (program, date)).fetchall()

    def analyze(self, schedule, w: Window, transcript: str, images=()) -> int | None:
        program = schedule["program"]
        known = self.known_quizzes(program, w.broadcast_date)
        self.analyses += 1
        self._images_used = len(images)
        try:
            extra = {"images": list(images)} if images else {}
            res = self.deps.answerer.analyze(program, transcript, [(r["id"], r["question"] or "") for r in known], **extra)
        except AnswererError as e:
            db.log(self.conn, "quizbot", f"{program} 분석 실패: {e}")
            return None
        except Exception as e:
            db.log(self.conn, "quizbot", f"{program} 분석 중 오류: {type(e).__name__}: {str(e)[:120]}")
            return None
        if res.kind == "not_quiz":
            return None
        dup = find_duplicate(known, res) if res.kind in ("reannouncement", "new_question", "answer_reveal") else None
        if res.kind == "answer_reveal":
            if dup:
                self.conn.execute("UPDATE quizzes SET note = COALESCE(note || ' / ', '') || ?, updated_at = ? WHERE id = ?",
                                  (f"방송 정답 발표: {res.answer or '?'}", db.now(), dup))
                self.conn.commit()
            db.log(self.conn, "quizbot", f"{program} 정답 발표 감지: {res.answer or '?'}")
            return dup
        if dup:
            self.conn.execute("UPDATE quizzes SET repeat_count = repeat_count + 1, updated_at = ? WHERE id = ?",
                              (db.now(), dup))
            row = self.conn.execute("SELECT * FROM quizzes WHERE id = ?", (dup,)).fetchone()
            if row["entry_status"] == "pending" and not row["approved"] and res.answer and \
                    (not (row["answer"] or "") or res.confidence > (row["confidence"] or 0)):
                # 재안내·힌트로 답이 더 확실해졌으면 새 답으로 다시 판단한다
                kind = self.pick_kind(schedule, res)
                self.conn.execute(
                    "UPDATE quizzes SET answer = ?, confidence = ?, witty_answer = ?, witty_point = ?, wit_score = ?, "
                    "fun_welcome = ?, answer_kind = ? WHERE id = ?",
                    (res.answer, res.confidence, res.witty_answer or row["witty_answer"],
                     res.witty_point or row["witty_point"], res.wit_score or row["wit_score"],
                     1 if (res.fun_welcome or row["fun_welcome"]) else 0, kind, dup))
            self.conn.commit()
            if row["entry_status"] == "pending":
                self.try_send(dup, schedule)
            return dup
        return self.record_new(schedule, w, res, transcript)

    def pick_kind(self, schedule, res: QuizAnalysis) -> str:
        import random

        ratio = config.get_float(self.conn, "quizbot.witty_ratio")
        if config.get(self.conn, "live.witty") != "1" and live.is_active(self.conn):
            ratio = 0.0   # 청취 화면에서 '기발한 오답 섞기'를 끔
        roll = random.Random(f"{schedule['program']}|{quiz.normalize_question(res.question)}").random()
        return choose_answer(res, schedule["min_confidence"] if schedule is not None else 0.8,
                             ratio if ratio is not None else 0.3,
                             config.get_float(self.conn, "quizbot.min_wit_score") or 0.7, roll)

    def record_new(self, schedule, w: Window, res: QuizAnalysis, transcript: str) -> int:
        account = db.get_setting(self.conn, "profile.account_label") or "기본"
        qkey = f"auto-{self.deps.now():%H%M%S}-{quiz.normalize_question(res.question)[:30]}"
        key = quiz.dedupe_key(account, schedule["program"], w.broadcast_date, qkey)
        cur = self.conn.execute(
            """INSERT INTO quizzes (dedupe_key, account, channel, program, broadcast_date, question_key, kind, question,
                   options, deadline, entry_channel, gorilla_accepted, answer, answer_verified, source, schedule_id,
                   confidence, excerpt, note, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, '고릴라', ?, ?, 0, 'auto', ?, ?, ?, ?, ?, ?)""",
            (key, account, schedule["channel"] or "파워FM", schedule["program"], w.broadcast_date, qkey, res.question, " / ".join(res.options),
             res.deadline_hint or None, res.gorilla_accepted, res.answer or None, schedule["id"], res.confidence,
             transcript[-2000:], (res.formatted_message and f"전송 형식: {res.formatted_message}") or res.reasoning_note,
             db.now(), db.now()))
        self.conn.commit()
        qid = cur.lastrowid
        kind = self.pick_kind(schedule, res)
        self.conn.execute(
            "UPDATE quizzes SET answer_kind = ?, witty_answer = ?, witty_point = ?, wit_score = ?, fun_welcome = ?, "
            "chat_shots = ? WHERE id = ?",
            (kind, res.witty_answer or None, res.witty_point or None, res.wit_score, 1 if res.fun_welcome else 0,
             getattr(self, "_images_used", 0), qid))
        if res.formatted_message and res.answer and res.answer in res.formatted_message:
            # 진행자가 정한 형식 (기발한 오답을 보낼 때도 같은 형식으로)
            text = res.formatted_message.replace(res.answer, res.witty_answer) if kind == "witty" else res.formatted_message
            self.conn.execute("UPDATE quizzes SET send_text = ? WHERE id = ?", (text[:100], qid))
        self.conn.commit()
        chosen = f" · 기발한 오답 '{res.witty_answer}'으로 보냄 ({res.witty_point[:40]})" if kind == "witty" else ""
        shots = f" · 채팅창 {self._images_used}장 함께 분석" if getattr(self, "_images_used", 0) else ""
        db.log(self.conn, "quizbot", f"{schedule['program']} 퀴즈 감지 #{qid}: {res.question[:40]} → {res.answer or '?'} "
                                     f"(확신도 {res.confidence:.2f}){chosen}{shots}")
        self.try_send(qid, schedule)
        return qid

    def _schedule(self, schedule, schedule_id):
        if schedule is not None and schedule["id"] == schedule_id:
            return schedule
        return self.conn.execute(
            "SELECT s.*, p.title AS program, p.channel AS channel FROM quiz_schedules s JOIN programs p ON p.id = s.program_id "
            "WHERE s.id = ?", (schedule_id,)).fetchone()

    def process_approved(self, schedule) -> None:
        """관리 화면에서 '보내기'를 누른 퀴즈 정답·사연 글을 보낸다 (고른 방법으로)."""
        for r in self.conn.execute("SELECT id, schedule_id FROM quizzes WHERE approved = 1 AND entry_status = 'pending'"
                                   ).fetchall():
            self.try_send(r["id"], self._schedule(schedule, r["schedule_id"]))
        for r in self.conn.execute("SELECT id, schedule_id FROM story_posts WHERE approved = 1 AND status = 'pending'"
                                   ).fetchall():
            self.try_send_story(r["id"], self._schedule(schedule, r["schedule_id"]))

    def retry_held(self, schedule, app_ok: bool = True) -> None:
        """앱 창·휴대폰이 없어 보류한 자동 전송 글을 다시 판단해 보낸다 (최근 것만)."""
        holds = [SMS_HOLD] + (APP_HOLDS if app_ok else [])
        marks = ",".join("?" * len(holds))
        # created_at 은 db.now()(실제 시각)로 남으므로 같은 기준으로 비교한다
        since = (datetime.now() - timedelta(minutes=HELD_RETRY_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
        for r in self.conn.execute(f"SELECT id, schedule_id FROM quizzes WHERE entry_status = 'pending' AND approved = 0 "
                                   f"AND decision IN ({marks}) AND created_at >= ?", (*holds, since)).fetchall():
            self.try_send(r["id"], self._schedule(schedule, r["schedule_id"]))
        for r in self.conn.execute(f"SELECT id, schedule_id FROM story_posts WHERE status = 'pending' AND approved = 0 "
                                   f"AND decision IN ({marks}) AND created_at >= ?", (*holds, since)).fetchall():
            self.try_send_story(r["id"], self._schedule(schedule, r["schedule_id"]))

    # ── 선물 정보 ────────────────────────────────────────────────
    def analyze_gifts(self, schedule, w: Window, transcript: str) -> list[int]:
        program = schedule["program"]
        self.gift_analyses += 1
        try:
            gifts = self.deps.answerer.analyze_gifts(program, transcript)
        except Exception as e:
            db.log(self.conn, "quizbot", f"{program} 선물 분석 실패: {str(e)[:120]}")
            return []
        ids = []
        for g in gifts:
            ids.append(self.record_gift(schedule, w, g, transcript))
        return ids

    def record_gift(self, schedule, w: Window, g: dict, transcript: str) -> int:
        existing = self.conn.execute("SELECT * FROM gift_events WHERE program = ? AND broadcast_date = ?",
                                     (schedule["program"], w.broadcast_date)).fetchall()
        target = quiz.normalize_question(g["gift"])
        for r in existing:
            other = quiz.normalize_question(r["gift"])
            if other and (target in other or other in target or SequenceMatcher(None, target, other).ratio() >= 0.7):
                # 같은 선물을 다시 안내 → 빈 칸만 채우고 횟수 올림
                fills = {k: g[k] for k in ("condition", "entry_method", "deadline", "winners", "announce")
                         if g.get(k) and not r[k]}
                sets = ", ".join(f"{k} = ?" for k in fills)
                self.conn.execute(f"UPDATE gift_events SET repeat_count = repeat_count + 1, "
                                  f"{sets + ', ' if sets else ''}updated_at = ? WHERE id = ?",
                                  (*fills.values(), db.now(), r["id"]))
                self.conn.commit()
                return r["id"]
        cur = self.conn.execute(
            """INSERT INTO gift_events (channel, program, broadcast_date, heard_at, gift, condition, entry_method, related,
                   deadline, winners, announce, excerpt, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (schedule["channel"] or "파워FM", schedule["program"], w.broadcast_date,
             self.deps.now().strftime("%Y-%m-%d %H:%M:%S"), g["gift"], g["condition"] or None,
             g["entry_method"] or None, g["related"], g["deadline"] or None, g["winners"] or None,
             g["announce"] or None, transcript[-1500:], db.now(), db.now()))
        self.conn.commit()
        db.log(self.conn, "quizbot", f"{schedule['program']} 선물 기록 #{cur.lastrowid}: {g['gift'][:30]}"
                                     + (f" — {g['condition'][:40]}" if g["condition"] else ""))
        return cur.lastrowid

    # ── 사연·주제 모집 ───────────────────────────────────────────
    def analyze_story(self, schedule, w: Window, transcript: str, images=()) -> int | None:
        program = schedule["program"]
        exps = story.candidate_experiences(self.conn, config.broadcaster_of(schedule["channel"]))
        if not exps:
            return None  # 쓸 수 있는 실제 경험이 없으면 유료 분석을 하지 않는다
        self.story_analyses += 1
        try:
            extra = {"images": list(images)} if images else {}
            res = self.deps.answerer.analyze_story(program, transcript, story.masked_profile(self.conn), exps, **extra)
        except Exception as e:
            db.log(self.conn, "quizbot", f"{program} 사연 분석 실패: {str(e)[:120]}")
            return None
        if not res.is_call or not res.topic.strip():
            return None
        existing = self.conn.execute("SELECT * FROM story_posts WHERE program = ? AND broadcast_date = ?",
                                     (program, w.broadcast_date)).fetchall()
        dup = story.find_duplicate(existing, res.topic)
        if dup:
            self.conn.execute("UPDATE story_posts SET repeat_count = repeat_count + 1, updated_at = ? WHERE id = ?",
                              (db.now(), dup))
            self.conn.commit()
            return dup
        exp = None
        if res.experience_id in {e["id"] for e in exps}:
            exp = self.conn.execute("SELECT * FROM experiences WHERE id = ?", (res.experience_id,)).fetchone()
        target = "board" if res.board_only else "chat"
        song = (res.song or "").strip()
        if song and exp is not None and song != (exp["song"] or "").strip() and \
                song != (db.get_profile(self.conn).get("song") or "").strip():
            song = ""   # 경험·내 정보에 적힌 신청곡만 쓴다
        message, source = (res.message or "").strip(), "ai"
        if exp is not None and res.use_user_line and (exp["gorilla_line"] or "").strip():
            message, source = exp["gorilla_line"].strip(), "user_line"
        if target == "board":
            message, source = (res.board_body or "").strip(), "ai"
        elif song and message and len(message) + len(song) + 8 <= answerer_mod.STORY_LIMIT:
            message = f"{message} (신청곡: {song})"
        if exp is None:
            message = ""
        warnings = story.message_checks(self.conn, message, exp, source, res.added_facts,
                                        limit=2000 if target == "board" else None) if exp is not None else []
        note = res.fit_reason if exp is not None else "주제에 맞는 실제 경험이 없음 — 직접 써서 보내거나 건너뛰세요"
        draft_id = self.make_board_draft(program, exp, res.board_title, message, song) \
            if target == "board" and exp is not None and message else None
        cur = self.conn.execute(
            """INSERT INTO story_posts (schedule_id, channel, program, broadcast_date, topic, experience_id, message, source,
                   gorilla_accepted, deadline, warnings, excerpt, note, target, board_title, draft_id, song, chat_shots,
                   created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (schedule["id"], self._channel_of(schedule), program, w.broadcast_date, res.topic.strip(), exp["id"] if exp is not None else None,
             message, source, res.gorilla_accepted, res.deadline_hint or None,
             json.dumps(warnings, ensure_ascii=False), transcript[-2000:], note, target, res.board_title or None,
             draft_id, song or None, len(images), db.now(), db.now()))
        self.conn.commit()
        pid = cur.lastrowid
        db.log(self.conn, "quizbot", f"{program} 사연 주제 감지 #{pid}: {res.topic[:40]} → "
                                     + (f"경험 #{exp['id']} 사용" if exp is not None else "맞는 경험 없음")
                                     + (" · 게시판에만 받음" if target == "board" else "")
                                     + (f" · 채팅창 {len(images)}장 함께 분석" if images else ""))
        if target == "board":
            self._hold("story_posts", pid, (f"게시판용 — 원고 검토함 #{draft_id}에서 확인하고 게시판에 입력하세요 (등록은 직접)"
                                            if draft_id else "게시판용 — 이 프로그램의 게시판(코너)을 몰라 원고를 만들지 못함. "
                                                             "코너 화면에서 게시판을 추가하세요"))
            self.deps.notify(f"게시판 사연 '{res.topic[:30]}' — 원고 검토함 확인")
            return pid
        if self.try_send_story(pid, schedule) is None:
            self.deps.notify(f"사연 주제 '{res.topic[:30]}' — 확인 대기")
        return pid

    def make_board_draft(self, program: str, exp, title: str, body: str, song: str) -> int | None:
        """진행자가 '게시판에만' 받는다고 하면 그 프로그램 게시판용 원고를 원고 검토함에 만든다.
        게시판 등록은 로그인·보안 문자가 있어 사용자가 직접 한다 (원고 화면의 '실제 화면에 입력하기')."""
        corner = self.conn.execute(
            "SELECT id FROM corners WHERE program = ? ORDER BY is_target DESC, (title LIKE '%사연%') DESC, id LIMIT 1",
            (program,)).fetchone()
        if corner is None:
            return None
        cur = self.conn.execute(
            """INSERT INTO drafts (experience_id, corner_id, title, body, song, source, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pasted', 'draft', ?, ?)""",
            (exp["id"], corner["id"], (title or "")[:60], body, song or "", db.now(), db.now()))
        self.conn.commit()
        return cur.lastrowid

    def try_send_story(self, pid: int, schedule) -> str | None:
        post = self.conn.execute("SELECT * FROM story_posts WHERE id = ?", (pid,)).fetchone()
        route = self.route_of(post)
        reasons = story.decide(self.conn, post, schedule, self.deps.now(),
                               config.get_int(self.conn, "quizbot.max_story_sends_per_hour"), route)
        if reasons:
            self._hold("story_posts", pid, "보류: " + " / ".join(reasons))
            return None
        how = "사용자 승인 후 전송" if post["approved"] else (
            "직접 쓴 한 줄 자동 전송" if post["source"] == "user_line" else "검사 통과 자동 전송")
        channel = post["channel"] or self._channel_of(schedule) or config.get(self.conn, "live.channel")
        return self.deliver("story_posts", "status", pid, route, channel, post["message"].strip(), how,
                            f"사연 #{pid}")

    def try_send(self, qid: int, schedule) -> str | None:
        q = self.conn.execute("SELECT * FROM quizzes WHERE id = ?", (qid,)).fetchone()
        route = self.route_of(q)
        reasons = decide(self.conn, q, schedule, self.deps.now(), route)
        if reasons:
            self._hold("quizzes", qid, "보류: " + " / ".join(reasons))
            return None
        template = config.get(self.conn, "sms.quiz_template" if route == "sms" else "gorilla.message_template")
        answer = q["witty_answer"] if q["answer_kind"] == "witty" and q["witty_answer"] else q["answer"]
        text = q["send_text"] or config.format_message(template, answer)
        return self.deliver("quizzes", "entry_status", qid, route, q["channel"], text,
                            "사용자 승인 후 전송" if q["approved"] else "자동 전송", f"퀴즈 #{qid}")

    # ── 보내기 (고릴라 채팅 / 휴대폰 문자) ────────────────────────
    def route_of(self, item) -> str:
        """글마다 고른 방법이 있으면 그것, 없으면 기본 설정."""
        chosen = item["route"] if "route" in item.keys() else None
        if chosen == "gorilla":
            chosen = "app"
        return chosen if chosen in config.ROUTES else config.route(self.conn)

    def chat_sender(self, app: str | None):
        """앱 채팅 전송 도구 (고릴라·mini·콩). 테스트처럼 앱별 도구가 없으면 기본 sender."""
        if self.deps.chat_senders and app in self.deps.chat_senders:
            return self.deps.chat_senders[app]
        return self.deps.sender

    def chat_ok(self, channel: str | None) -> bool:
        app = config.chat_app(self.conn, channel)
        return app is None or self.chat_sender(app).is_running()

    def _channel_of(self, schedule) -> str | None:
        if schedule is not None and "channel" in schedule.keys():
            return schedule["channel"]
        return None

    def _hold(self, table: str, item_id: int, decision: str) -> None:
        self.conn.execute(f"UPDATE {table} SET decision = ?, updated_at = ? WHERE id = ?", (decision, db.now(), item_id))
        self.conn.commit()

    def not_ready(self, route: str, channel: str | None) -> str | None:
        """지금 보낼 수 없는 이유 (보류 문구). 보낼 수 있으면 None."""
        if route == "sms":
            if not config.sms_number(self.conn, channel):
                return f"보류: '{channel or '채널 미확인'}' 문자 번호가 없음 (청취 화면의 채널 목록에서 입력)"
            if self.deps.sms is None or not self.deps.sms.is_ready():
                return SMS_HOLD
            return None
        app = config.chat_app(self.conn, channel)
        if app is None:
            return f"보류: '{channel or '채널 미확인'}'은(는) 채팅 앱이 정해지지 않음 — '문자로 보내기'를 누르면 문자로 보냄"
        return None if self.chat_sender(app).is_running() else app_hold(app)

    def deliver(self, table: str, status_col: str, item_id: int, route: str, channel: str | None, text: str,
                how: str, label: str) -> str | None:
        hold = self.not_ready(route, channel)
        if hold:
            self._hold(table, item_id, hold)
            return None
        number, app = None, config.chat_app(self.conn, channel)
        if route == "sms":
            number = config.sms_number(self.conn, channel)
            text = sms_mod.with_signature(text, db.get_profile(self.conn).get("nickname"),
                                          config.get(self.conn, "sms.signature") != "0")
        # 보내기 전에 먼저 '결과 불명'으로 바꿔 두어, 도중에 멈춰도 다시 보내지 않게 한다.
        sent_text = ", send_text = ?" if table == "quizzes" else ""
        self.conn.execute(
            f"UPDATE {table} SET {status_col} = 'unknown', sent_at = ?, sent_via = ?, decision = ?{sent_text}, "
            f"updated_at = ? WHERE id = ? AND {status_col} = 'pending'",
            (db.now(), "sms" if route == "sms" else app, how, *((text,) if sent_text else ()), db.now(), item_id))
        self.conn.commit()
        if route != "sms":
            cfg = getattr(self.chat_sender(app), "cfg", None)
            how_clicked = "" if cfg is None else \
                " (위치 클릭)" if "coords" in (cfg.input_mode, cfg.send_mode) else " (화면 요소)"
            self.touch(f"{config.app_label(app)} 채팅 전송{how_clicked}")
        try:
            if route == "sms":
                result = self.deps.sms.send(number, text)
            elif self.deps.lock is not None:
                with self.deps.lock():
                    result = self.chat_sender(app).send(text)
            else:
                result = self.chat_sender(app).send(text)
            status, detail = result.status, result.detail
        except Exception as e:  # 보냈는지 알 수 없다 → 결과 불명 유지
            status, detail = "unknown", f"전송 중 오류: {type(e).__name__}: {str(e)[:120]}"
        self.conn.execute(f"UPDATE {table} SET {status_col} = ?, note = COALESCE(note || ' / ', '') || ?, "
                          "updated_at = ? WHERE id = ?", (status, detail, db.now(), item_id))
        self.conn.commit()
        via = f"문자({number})" if route == "sms" else config.app_label(app)
        db.log(self.conn, "quizbot", f"{label} {via} 전송 '{text[:60]}' → {quiz.entry_label(status, route)} ({detail})")
        return status


@contextmanager
def shared_input_lock(wait_seconds: int = 20):
    """웹 입력 도구와 같은 잠금. 다른 입력 작업이 돌고 있으면 잠깐 기다린다."""
    from ..autofill import AutofillError, input_lock

    deadline = time.monotonic() + wait_seconds
    while True:
        cm = input_lock()
        try:
            cm.__enter__()
            break
        except AutofillError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)
    try:
        yield
    finally:
        cm.__exit__(None, None, None)


def make_transcriber(conn: sqlite3.Connection, model: str, program: str):
    """설정대로 음성 인식기를 만든다. 장치 auto 는 그래픽카드를 별도 프로세스로 점검한 뒤 고른다."""
    from . import gpu
    from .stt import WhisperTranscriber

    device, note = gpu.choose_device(conn, config.get(conn, "quizbot.whisper_device"))
    tr = WhisperTranscriber(model, program, device, beam_size=config.get_int(conn, "quizbot.whisper_beam"),
                            overlap_seconds=config.get_int(conn, "quizbot.stt_overlap"),
                            hints=stt_hints(conn, program))
    tr.device_note = note
    return tr


def build_real_deps(conn: sqlite3.Connection) -> Deps:
    """윈도우에서 실제로 쓰는 구성."""
    from .answerer import ClaudeAnswerer
    from .audio import LoopbackRecorder
    from .gorilla import Gorilla
    from .sms import AdbSms

    def notify(_message: str) -> None:
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass

    return Deps(
        notify=notify,
        recorder_factory=lambda chunk: LoopbackRecorder(chunk),
        transcriber_factory=lambda model, program: make_transcriber(conn, model, program),
        answerer=ClaudeAnswerer(config.get(conn, "quizbot.model"), config.get(conn, "quizbot.effort")),
        sender=Gorilla(config.GorillaConfig.load(conn, "gorilla")),
        chat_senders={app: Gorilla(config.GorillaConfig.load(conn, app)) for app in config.CHAT_APPS},
        sms=AdbSms.from_settings(conn),
        lock=shared_input_lock,
    )
