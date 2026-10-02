"""퀴즈 자동 참여 실행기.

예약 구간 동안: 고릴라 실행 확인 → 녹음 → 음성 인식 → 퀴즈 신호 → 분석 → 자동 전송 판단 → 전송 → 기록.

한 번 보낸(또는 보냈는지 모르는) 문제는 다시 보내지 않는다. 전송 직전에 상태를 '결과 불명'으로 먼저
바꿔 두므로, 전송 도중 프로그램이 멈춰도 재시작 후 같은 문제를 또 보내지 않는다.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Callable, Protocol

from .. import db, quiz
from . import config
from .answerer import AnswererError, QuizAnalysis
from .audio import rms
from .detector import Detector, TranscriptBuffer
from .schedule import Window, active_window, next_window

SILENCE_RMS = 0.002
SILENT_CHUNKS_WARN = 4
SIMILAR_QUESTION = 0.85  # 짧은 한국어 문장은 0.6이면 다른 문제도 같다고 본다


class Recorder(Protocol):
    def read_chunk(self): ...


class Transcriber(Protocol):
    def transcribe(self, audio) -> str: ...


class Answerer(Protocol):
    def analyze(self, program: str, transcript: str, known: list[tuple[int, str]]) -> QuizAnalysis: ...


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


# ── 판단 ────────────────────────────────────────────────────────────
def decide(conn: sqlite3.Connection, q: sqlite3.Row, schedule: sqlite3.Row | None, now: datetime) -> list[str]:
    """자동 전송을 막는 이유 목록. 비어 있으면 바로 보낸다."""
    reasons = []
    if db.is_stopped(conn):
        reasons.append("일괄 중지가 켜져 있음")
    if q["entry_status"] != "pending":
        reasons.append(f"이미 처리됨({quiz.ENTRY_LABELS.get(q['entry_status'], q['entry_status'])})")
    if q["kind"] != "new":
        reasons.append("새 문제가 아님")
    if not (q["answer"] or "").strip():
        reasons.append("정답 후보 없음")
    approved = bool(q["approved"])
    if not approved:
        if schedule is None or not schedule["auto_submit"]:
            reasons.append("이 예약은 자동 전송이 꺼져 있음 (확인 후 전송)")
        elif (q["confidence"] or 0) < schedule["min_confidence"]:
            reasons.append(f"확신도 {q['confidence']:.2f} < 기준 {schedule['min_confidence']:.2f}")
        if q["gorilla_accepted"] == "no":
            reasons.append("진행자가 고릴라가 아닌 다른 방법으로 받는다고 함")
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


# ── 실행기 ──────────────────────────────────────────────────────────
@dataclass
class Runner:
    conn: sqlite3.Connection
    deps: Deps
    analyses: int = 0

    def state(self, text: str) -> None:
        db.set_setting(self.conn, "quizbot.state", text)
        db.set_setting(self.conn, "quizbot.heartbeat", self.deps.now().strftime("%Y-%m-%d %H:%M:%S"))

    def should_stop(self) -> bool:
        return db.get_setting(self.conn, "quizbot.stop", "0") == "1"

    def schedules(self):
        return self.conn.execute(
            "SELECT s.*, p.title AS program FROM quiz_schedules s JOIN programs p ON p.id = s.program_id").fetchall()

    def run_forever(self, idle_seconds: int = 20) -> None:
        self.state("시작함")
        while not self.should_stop():
            now = self.deps.now()
            schedules = self.schedules()
            w = active_window(schedules, now)
            if w is None:
                nxt = next_window(schedules, now)
                self.state(f"대기 중 — 다음 예약 {nxt.start:%m/%d %H:%M}" if nxt else "대기 중 — 예약 없음")
                self.process_approved(None)
                self.deps.sleep(idle_seconds)
                continue
            try:
                self.run_window(w)
            except Exception as e:  # 녹음 장치·모델 내려받기 등 실패 → 기록하고 30초 뒤 다시 시도
                msg = f"{type(e).__name__}: {str(e)[:150]}"
                db.log(self.conn, "quizbot", f"예약 실행 중 오류 — 30초 뒤 다시 시도: {msg}")
                self.state(f"오류 후 대기 중 — {msg}")
                self.deps.sleep(30)
        db.set_setting(self.conn, "quizbot.stop", "0")
        self.state("멈춤")

    def run_window(self, w: Window) -> None:
        schedule = self.conn.execute(
            "SELECT s.*, p.title AS program FROM quiz_schedules s JOIN programs p ON p.id = s.program_id "
            "WHERE s.id = ?", (w.schedule_id,)).fetchone()
        program = schedule["program"]
        # 고릴라가 켜질 때까지 기다린다
        while not self.deps.sender.is_running():
            if self.should_stop() or self.deps.now() >= w.end:
                return
            self.state(f"{program} 예약 중 — 고릴라가 실행 중이 아님 (30초마다 확인)")
            self.deps.sleep(30)

        chunk_seconds = config.get_int(self.conn, "quizbot.chunk_seconds")
        transcriber = self.deps.transcriber_factory(config.get(self.conn, "quizbot.whisper_model"), program)
        buffer = TranscriptBuffer()
        detector = Detector(settle_seconds=config.get_int(self.conn, "quizbot.settle_seconds"),
                            cooldown_seconds=config.get_int(self.conn, "quizbot.cooldown_seconds"))
        max_analyses = config.get_int(self.conn, "quizbot.max_analyses_per_window")
        self.analyses = 0
        silent = 0
        db.log(self.conn, "quizbot", f"{program} 예약 시작 ({w.start:%H:%M}~{w.end:%H:%M})")
        with self.deps.recorder_factory(chunk_seconds) as recorder:
            while self.deps.now() < w.end and not self.should_stop():
                self.state(f"{program} 듣는 중 ({w.start:%H:%M}~{w.end:%H:%M}) · 분석 {self.analyses}회")
                audio = recorder.read_chunk()
                now = self.deps.now()
                if rms(audio) < SILENCE_RMS:
                    silent += 1
                    if silent == SILENT_CHUNKS_WARN:
                        db.log(self.conn, "quizbot", "소리가 들리지 않습니다. 고릴라 재생·음소거·기본 스피커를 확인하세요.")
                else:
                    silent = 0
                text = transcriber.transcribe(audio)
                if text:
                    buffer.add(now, text)
                    self.conn.execute("INSERT INTO transcripts (schedule_id, broadcast_date, at, text) VALUES (?, ?, ?, ?)",
                                      (w.schedule_id, w.broadcast_date, now.strftime("%Y-%m-%d %H:%M:%S"), text))
                    self.conn.commit()
                    detector.feed(now, text)
                if detector.is_due(now):
                    detector.mark_analyzed(now)
                    if self.analyses < max_analyses:
                        self.analyze(schedule, w, buffer.window(now, config.get_int(self.conn, "quizbot.context_seconds")))
                    elif self.analyses == max_analyses:
                        db.log(self.conn, "quizbot", f"{program}: 분석 횟수 상한({max_analyses}) 도달 — 이번 예약에서는 더 분석하지 않음")
                        self.analyses += 1
                self.process_approved(schedule)
        db.log(self.conn, "quizbot", f"{program} 예약 끝 · 분석 {min(self.analyses, max_analyses)}회")

    def known_quizzes(self, program: str, date: str):
        return self.conn.execute(
            "SELECT * FROM quizzes WHERE program = ? AND broadcast_date = ? AND source = 'auto' ORDER BY id",
            (program, date)).fetchall()

    def analyze(self, schedule, w: Window, transcript: str) -> int | None:
        program = schedule["program"]
        known = self.known_quizzes(program, w.broadcast_date)
        self.analyses += 1
        try:
            res = self.deps.answerer.analyze(program, transcript, [(r["id"], r["question"] or "") for r in known])
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
            if not (row["answer"] or "") and res.answer:
                self.conn.execute("UPDATE quizzes SET answer = ?, confidence = ? WHERE id = ?",
                                  (res.answer, res.confidence, dup))
            self.conn.commit()
            if row["entry_status"] == "pending":
                self.try_send(dup, schedule)
            return dup
        return self.record_new(schedule, w, res, transcript)

    def record_new(self, schedule, w: Window, res: QuizAnalysis, transcript: str) -> int:
        account = db.get_setting(self.conn, "profile.account_label") or "기본"
        qkey = f"auto-{self.deps.now():%H%M%S}-{quiz.normalize_question(res.question)[:30]}"
        key = quiz.dedupe_key(account, schedule["program"], w.broadcast_date, qkey)
        cur = self.conn.execute(
            """INSERT INTO quizzes (dedupe_key, account, channel, program, broadcast_date, question_key, kind, question,
                   options, deadline, entry_channel, gorilla_accepted, answer, answer_verified, source, schedule_id,
                   confidence, excerpt, note, created_at, updated_at)
               VALUES (?, ?, '파워FM', ?, ?, ?, 'new', ?, ?, ?, '고릴라', ?, ?, 0, 'auto', ?, ?, ?, ?, ?, ?)""",
            (key, account, schedule["program"], w.broadcast_date, qkey, res.question, " / ".join(res.options),
             res.deadline_hint or None, res.gorilla_accepted, res.answer or None, schedule["id"], res.confidence,
             transcript[-2000:], (res.formatted_message and f"전송 형식: {res.formatted_message}") or res.reasoning_note,
             db.now(), db.now()))
        self.conn.commit()
        qid = cur.lastrowid
        if res.formatted_message and res.answer and res.answer in res.formatted_message:
            self.conn.execute("UPDATE quizzes SET send_text = ? WHERE id = ?", (res.formatted_message[:100], qid))
            self.conn.commit()
        db.log(self.conn, "quizbot", f"{schedule['program']} 퀴즈 감지 #{qid}: {res.question[:40]} → {res.answer or '?'} "
                                     f"(확신도 {res.confidence:.2f})")
        self.try_send(qid, schedule)
        return qid

    def process_approved(self, schedule) -> None:
        """관리 화면에서 '이 답으로 보내기'를 누른 문제를 보낸다."""
        rows = self.conn.execute(
            "SELECT id, schedule_id FROM quizzes WHERE approved = 1 AND entry_status = 'pending'").fetchall()
        if rows and not self.deps.sender.is_running():
            self.conn.execute("UPDATE quizzes SET decision = ? WHERE approved = 1 AND entry_status = 'pending'",
                              ("승인됨 — 고릴라가 켜지면 보냄",))
            self.conn.commit()
            return
        for r in rows:
            sch = schedule if schedule is not None and schedule["id"] == r["schedule_id"] else self.conn.execute(
                "SELECT s.*, p.title AS program FROM quiz_schedules s JOIN programs p ON p.id = s.program_id "
                "WHERE s.id = ?", (r["schedule_id"],)).fetchone()
            self.try_send(r["id"], sch)

    def try_send(self, qid: int, schedule) -> str | None:
        q = self.conn.execute("SELECT * FROM quizzes WHERE id = ?", (qid,)).fetchone()
        reasons = decide(self.conn, q, schedule, self.deps.now())
        if reasons:
            self.conn.execute("UPDATE quizzes SET decision = ?, updated_at = ? WHERE id = ?",
                              ("보류: " + " / ".join(reasons), db.now(), qid))
            self.conn.commit()
            return None
        text = q["send_text"] or config.format_message(config.get(self.conn, "gorilla.message_template"), q["answer"])
        # 보내기 전에 먼저 '결과 불명'으로 바꿔 두어, 도중에 멈춰도 다시 보내지 않게 한다.
        self.conn.execute(
            "UPDATE quizzes SET entry_status = 'unknown', send_text = ?, sent_at = ?, decision = ?, updated_at = ? "
            "WHERE id = ? AND entry_status = 'pending'",
            (text, db.now(), "사용자 승인 후 전송" if q["approved"] else "자동 전송", db.now(), qid))
        self.conn.commit()
        try:
            if self.deps.lock is not None:
                with self.deps.lock():
                    result = self.deps.sender.send(text)
            else:
                result = self.deps.sender.send(text)
            status, detail = result.status, result.detail
        except Exception as e:  # 보냈는지 알 수 없다 → 결과 불명 유지
            status, detail = "unknown", f"전송 중 오류: {type(e).__name__}: {str(e)[:120]}"
        self.conn.execute("UPDATE quizzes SET entry_status = ?, note = COALESCE(note || ' / ', '') || ?, "
                          "updated_at = ? WHERE id = ?", (status, detail, db.now(), qid))
        self.conn.commit()
        db.log(self.conn, "quizbot", f"퀴즈 #{qid} 고릴라 전송 '{text}' → {quiz.ENTRY_LABELS.get(status, status)} ({detail})")
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


def build_real_deps(conn: sqlite3.Connection) -> Deps:
    """윈도우에서 실제로 쓰는 구성."""
    from .answerer import ClaudeAnswerer
    from .audio import LoopbackRecorder
    from .gorilla import Gorilla
    from .stt import WhisperTranscriber

    return Deps(
        recorder_factory=lambda chunk: LoopbackRecorder(chunk),
        transcriber_factory=lambda model, program: WhisperTranscriber(model, program),
        answerer=ClaudeAnswerer(config.get(conn, "quizbot.model"), config.get(conn, "quizbot.effort")),
        sender=Gorilla(config.GorillaConfig.load(conn)),
        lock=shared_input_lock,
    )
