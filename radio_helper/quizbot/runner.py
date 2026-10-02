"""퀴즈 자동 참여 실행기.

예약 구간 동안: 고릴라 실행 확인 → 녹음 → 음성 인식 → 퀴즈 신호 → 분석 → 자동 전송 판단 → 전송 → 기록.

한 번 보낸(또는 보냈는지 모르는) 문제는 다시 보내지 않는다. 전송 직전에 상태를 '결과 불명'으로 먼저
바꿔 두므로, 전송 도중 프로그램이 멈춰도 재시작 후 같은 문제를 또 보내지 않는다.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Callable, Protocol

from .. import db, quiz
from . import config, live, story
from .answerer import AnswererError, QuizAnalysis, StoryAnalysis
from .audio import rms
from .detector import Detector, TranscriptBuffer, is_gift_signal, is_story_signal
from .schedule import Window, active_window, next_window

SILENCE_RMS = 0.002
SILENT_CHUNKS_WARN = 4
SIMILAR_QUESTION = 0.85  # 짧은 한국어 문장은 0.6이면 다른 문제도 같다고 본다
GORILLA_HOLD = "대기: 고릴라 창을 찾지 못함 — 찾으면 보냄"
HELD_RETRY_MINUTES = 20  # 이보다 오래된 보류 글은 늦었으므로 자동으로 보내지 않는다


class Recorder(Protocol):
    def read_chunk(self): ...


class Transcriber(Protocol):
    def transcribe(self, audio) -> str: ...


class Answerer(Protocol):
    def analyze(self, program: str, transcript: str, known: list[tuple[int, str]]) -> QuizAnalysis: ...
    def analyze_story(self, program: str, transcript: str, profile: dict, experiences: list[dict]) -> StoryAnalysis: ...
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
    story_analyses: int = 0
    gift_analyses: int = 0

    def state(self, text: str) -> None:
        db.set_setting(self.conn, "quizbot.state", text)
        db.set_setting(self.conn, "quizbot.heartbeat", self.deps.now().strftime("%Y-%m-%d %H:%M:%S"))

    def should_stop(self) -> bool:
        return db.get_setting(self.conn, "quizbot.stop", "0") == "1"

    def schedules(self):
        return self.conn.execute(
            "SELECT s.*, p.title AS program, p.channel AS channel FROM quiz_schedules s JOIN programs p ON p.id = s.program_id").fetchall()

    def report_level(self, level: int) -> None:
        db.set_setting(self.conn, "quizbot.level", str(level))
        db.set_setting(self.conn, "quizbot.level_at", self.deps.now().strftime("%Y-%m-%d %H:%M:%S"))

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
        while wait_for_gorilla and not self.deps.sender.is_running():
            if self.should_stop() or not keep_going(self.deps.now()):
                return
            self.state(f"{session['program']} — 고릴라가 실행 중이 아님 (30초마다 확인)")
            self.deps.sleep(30)
        gorilla_ok = self.deps.sender.is_running()
        gorilla_checked = self.deps.now()
        if not gorilla_ok:
            db.log(self.conn, "quizbot", "고릴라 창을 찾지 못했습니다 — 듣기·자막은 계속하고, 보낼 글은 고릴라 창을 찾으면 보냅니다.")

        self.state("음성 인식 준비 중 (처음 한 번은 모델 내려받기로 몇 분 걸릴 수 있음)")
        transcriber = self.deps.transcriber_factory(config.get(self.conn, "quizbot.whisper_model"), session["program"])
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
        current, silent = None, 0
        with self.deps.recorder_factory(chunk_seconds) as recorder:
            if hasattr(recorder, "on_level"):
                recorder.on_level = self.report_level  # 녹음 중 1초마다 소리 크기를 화면에 알린다
            while keep_going(self.deps.now()) and not self.should_stop():
                session, w = get_session(self.deps.now())
                program = session["program"]
                if program != current:
                    # 프로그램이 바뀌면 프로그램당 분석 상한을 새로 센다
                    current, self.analyses, self.story_analyses, self.gift_analyses = program, 0, 0, 0
                if not wait_for_gorilla and (self.deps.now() - gorilla_checked).total_seconds() >= 60:
                    gorilla_checked, was_ok = self.deps.now(), gorilla_ok
                    gorilla_ok = self.deps.sender.is_running()
                    if gorilla_ok and not was_ok:
                        db.log(self.conn, "quizbot", "고릴라 창을 찾았습니다 — 보류한 글을 보냅니다.")
                self.state(f"{program} 듣는 중 · 분석 퀴즈 {self.analyses}·사연 {self.story_analyses}·선물 {self.gift_analyses}회"
                           + ("" if gorilla_ok else " · 고릴라 창 못 찾음(전송 보류)"))
                audio = recorder.read_chunk()
                now = self.deps.now()
                level = rms(audio)
                self.report_level(live.level_percent(level))
                if level < SILENCE_RMS:
                    silent += 1
                    if silent == SILENT_CHUNKS_WARN:
                        db.log(self.conn, "quizbot", "소리가 들리지 않습니다. 고릴라 재생·음소거·기본 스피커를 확인하세요.")
                else:
                    silent = 0
                text = transcriber.transcribe(audio)
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
                if session["gift_enabled"] and gift_detector.is_due(now):
                    gift_detector.mark_analyzed(now)
                    if self.gift_analyses < max_gift:
                        self.analyze_gifts(session, w, buffer.window(now, context))
                if session["story_enabled"] and story_detector.is_due(now):
                    story_detector.mark_analyzed(now)
                    if self.story_analyses < max_story:
                        self.analyze_story(session, w, buffer.window(now, context))
                if detector.is_due(now):
                    detector.mark_analyzed(now)
                    if self.analyses < max_analyses:
                        self.analyze(session, w, buffer.window(now, context))
                    elif self.analyses == max_analyses:
                        db.log(self.conn, "quizbot", f"{program}: 분석 횟수 상한({max_analyses}) 도달 — 이 프로그램에서는 더 분석하지 않음")
                        self.analyses += 1
                self.process_approved(session)
                if gorilla_ok:
                    self.retry_held(session)
        db.log(self.conn, "quizbot", f"{current or session['program']} 듣기 끝 · 분석 {min(self.analyses, max_analyses)}회")

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
               VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, '고릴라', ?, ?, 0, 'auto', ?, ?, ?, ?, ?, ?)""",
            (key, account, schedule["channel"] or "파워FM", schedule["program"], w.broadcast_date, qkey, res.question, " / ".join(res.options),
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

    def _schedule(self, schedule, schedule_id):
        if schedule is not None and schedule["id"] == schedule_id:
            return schedule
        return self.conn.execute(
            "SELECT s.*, p.title AS program, p.channel AS channel FROM quiz_schedules s JOIN programs p ON p.id = s.program_id "
            "WHERE s.id = ?", (schedule_id,)).fetchone()

    def process_approved(self, schedule) -> None:
        """관리 화면에서 '보내기'를 누른 퀴즈 정답·사연 글을 보낸다."""
        quizzes = self.conn.execute(
            "SELECT id, schedule_id FROM quizzes WHERE approved = 1 AND entry_status = 'pending'").fetchall()
        stories = self.conn.execute(
            "SELECT id, schedule_id FROM story_posts WHERE approved = 1 AND status = 'pending'").fetchall()
        if (quizzes or stories) and not self.deps.sender.is_running():
            self.conn.execute("UPDATE quizzes SET decision = ? WHERE approved = 1 AND entry_status = 'pending'",
                              ("승인됨 — 고릴라가 켜지면 보냄",))
            self.conn.execute("UPDATE story_posts SET decision = ? WHERE approved = 1 AND status = 'pending'",
                              ("승인됨 — 고릴라가 켜지면 보냄",))
            self.conn.commit()
            return
        for r in quizzes:
            self.try_send(r["id"], self._schedule(schedule, r["schedule_id"]))
        for r in stories:
            self.try_send_story(r["id"], self._schedule(schedule, r["schedule_id"]))

    def retry_held(self, schedule) -> None:
        """고릴라 창을 못 찾아 보류한 자동 전송 글을 다시 판단해 보낸다 (최근 것만)."""
        # created_at 은 db.now()(실제 시각)로 남으므로 같은 기준으로 비교한다
        since = (datetime.now() - timedelta(minutes=HELD_RETRY_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
        for r in self.conn.execute("SELECT id, schedule_id FROM quizzes WHERE entry_status = 'pending' AND approved = 0 "
                                   "AND decision = ? AND created_at >= ?", (GORILLA_HOLD, since)).fetchall():
            self.try_send(r["id"], self._schedule(schedule, r["schedule_id"]))
        for r in self.conn.execute("SELECT id, schedule_id FROM story_posts WHERE status = 'pending' AND approved = 0 "
                                   "AND decision = ? AND created_at >= ?", (GORILLA_HOLD, since)).fetchall():
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
    def analyze_story(self, schedule, w: Window, transcript: str) -> int | None:
        program = schedule["program"]
        exps = story.candidate_experiences(self.conn)
        if not exps:
            return None  # 쓸 수 있는 실제 경험이 없으면 유료 분석을 하지 않는다
        self.story_analyses += 1
        try:
            res = self.deps.answerer.analyze_story(program, transcript, story.masked_profile(self.conn), exps)
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
        message, source = (res.message or "").strip(), "ai"
        if exp is not None and res.use_user_line and (exp["gorilla_line"] or "").strip():
            message, source = exp["gorilla_line"].strip(), "user_line"
        if exp is None:
            message = ""
        warnings = story.message_checks(self.conn, message, exp, source, res.added_facts) if exp is not None else []
        note = res.fit_reason if exp is not None else "주제에 맞는 실제 경험이 없음 — 직접 써서 보내거나 건너뛰세요"
        cur = self.conn.execute(
            """INSERT INTO story_posts (schedule_id, program, broadcast_date, topic, experience_id, message, source,
                   gorilla_accepted, deadline, warnings, excerpt, note, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (schedule["id"], program, w.broadcast_date, res.topic.strip(), exp["id"] if exp is not None else None,
             message, source, res.gorilla_accepted, res.deadline_hint or None,
             json.dumps(warnings, ensure_ascii=False), transcript[-2000:], note, db.now(), db.now()))
        self.conn.commit()
        pid = cur.lastrowid
        db.log(self.conn, "quizbot", f"{program} 사연 주제 감지 #{pid}: {res.topic[:40]} → "
                                     + (f"경험 #{exp['id']} 사용" if exp is not None else "맞는 경험 없음"))
        if self.try_send_story(pid, schedule) is None:
            self.deps.notify(f"사연 주제 '{res.topic[:30]}' — 확인 대기")
        return pid

    def try_send_story(self, pid: int, schedule) -> str | None:
        post = self.conn.execute("SELECT * FROM story_posts WHERE id = ?", (pid,)).fetchone()
        reasons = story.decide(self.conn, post, schedule, self.deps.now(),
                               config.get_int(self.conn, "quizbot.max_story_sends_per_hour"))
        if reasons:
            self.conn.execute("UPDATE story_posts SET decision = ?, updated_at = ? WHERE id = ?",
                              ("보류: " + " / ".join(reasons), db.now(), pid))
            self.conn.commit()
            return None
        if not self.deps.sender.is_running():
            self.conn.execute("UPDATE story_posts SET decision = ?, updated_at = ? WHERE id = ?",
                              (GORILLA_HOLD, db.now(), pid))
            self.conn.commit()
            return None
        text = post["message"].strip()
        self.conn.execute(
            "UPDATE story_posts SET status = 'unknown', sent_at = ?, decision = ?, updated_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (db.now(), "사용자 승인 후 전송" if post["approved"] else "직접 쓴 한 줄 자동 전송", db.now(), pid))
        self.conn.commit()
        try:
            if self.deps.lock is not None:
                with self.deps.lock():
                    result = self.deps.sender.send(text)
            else:
                result = self.deps.sender.send(text)
            status, detail = result.status, result.detail
        except Exception as e:
            status, detail = "unknown", f"전송 중 오류: {type(e).__name__}: {str(e)[:120]}"
        self.conn.execute("UPDATE story_posts SET status = ?, note = COALESCE(note || ' / ', '') || ?, updated_at = ? "
                          "WHERE id = ?", (status, detail, db.now(), pid))
        self.conn.commit()
        db.log(self.conn, "quizbot", f"사연 #{pid} 고릴라 전송 → {quiz.ENTRY_LABELS.get(status, status)} ({detail})")
        return status

    def try_send(self, qid: int, schedule) -> str | None:
        q = self.conn.execute("SELECT * FROM quizzes WHERE id = ?", (qid,)).fetchone()
        reasons = decide(self.conn, q, schedule, self.deps.now())
        if reasons:
            self.conn.execute("UPDATE quizzes SET decision = ?, updated_at = ? WHERE id = ?",
                              ("보류: " + " / ".join(reasons), db.now(), qid))
            self.conn.commit()
            return None
        if not self.deps.sender.is_running():
            self.conn.execute("UPDATE quizzes SET decision = ?, updated_at = ? WHERE id = ?",
                              (GORILLA_HOLD, db.now(), qid))
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

    def notify(_message: str) -> None:
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass

    return Deps(
        notify=notify,
        recorder_factory=lambda chunk: LoopbackRecorder(chunk),
        transcriber_factory=lambda model, program: WhisperTranscriber(model, program),
        answerer=ClaudeAnswerer(config.get(conn, "quizbot.model"), config.get(conn, "quizbot.effort")),
        sender=Gorilla(config.GorillaConfig.load(conn)),
        lock=shared_input_lock,
    )
