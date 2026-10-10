"""로컬 관리 화면 (Flask). 이 PC(127.0.0.1)에서만 열린다."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, send_from_directory, session, url_for

from . import db, evidence, gates, generator, quiz
from .errors import error_page
from .gates import STATUS_LABELS
from .quizbot import config as qconfig
from .quizbot import live
from .quizbot import schedule as qschedule

PROJECT_ROOT = Path(__file__).resolve().parent.parent
YES_NO = {"unknown": "미확인", "yes": "예", "no": "아니오"}
RECRUITING = {"unknown": "미확인", "open": "모집 중", "closed": "모집 안 함"}
AI_POLICY = {"unknown": "미확인", "allowed": "제한 없음 확인", "restricted": "제한 있음"}
DRAFT_STATUS = {"draft": "작성 중", "approved": "승인됨", "archived": "보관"}
SOURCE = {"template": "템플릿 초안", "pasted": "AI 결과 붙여넣음", "manual": "직접 작성"}
STORY_SOURCE = {"ai": "AI 초안", "user_line": "직접 쓴 한 줄", "edited": "고친 글", "manual": "직접 씀"}
LIVE_OPTIONS = (("live.auto_quiz", "auto_quiz"), ("live.auto_story", "auto_story"), ("live.gift", "gift"),
                ("live.captions", "captions"), ("live.witty", "witty"), ("live.quiz_always", "quiz_always"))
TEST_MESSAGE_MAX = 200   # 채팅 앱 입력칸 글자 수 (고릴라 공감로그 200자 내외)
INSPECT_IMAGE = re.compile(r"(gorilla|mini|kong|sms)_[a-z0-9_]+\.png")


def create_app(data_dir: str | None = None) -> Flask:
    if data_dir:
        os.environ["RADIO_HELPER_DATA_DIR"] = data_dir
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=session_key(),   # 데이터 폴더에 고정: 관리 화면을 다시 켜도 열려 있던 화면이 그대로 동작
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_HTTPONLY=True,
    )
    conn = db.connect()
    db.init_db(conn)
    conn.close()

    app.jinja_env.globals.update(
        YES_NO=YES_NO, RECRUITING=RECRUITING, AI_POLICY=AI_POLICY, DRAFT_STATUS=DRAFT_STATUS,
        SOURCE=SOURCE, POST_STATUS=STATUS_LABELS, QUIZ_KIND=quiz.KIND_LABELS, QUIZ_ENTRY=quiz.ENTRY_LABELS,
        entry_label=quiz.entry_label,
        STORY_SOURCE=STORY_SOURCE,
        VIA={"sms": "문자", **{k: v["label"] for k, v in qconfig.CHAT_APPS.items()}},
    )

    # ── 보안: 로컬 전용 + 위조 요청 차단 ─────────────────────────────
    @app.before_request
    def guard():
        host = (request.host or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            abort(403)
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        if request.method == "POST" and request.form.get("csrf_token") != session["csrf"]:
            # 다시 켜기 전에 열어 둔 화면 등에서 누른 것: 처리하지 않고 원래 화면으로 돌려보낸다 (외부 위조 요청도 마찬가지)
            flash("화면이 오래되어 방금 누른 것은 처리하지 않았습니다 (관리 화면이 다시 켜졌거나 브라우저를 다시 열었을 때 생김). "
                  "새로 불러온 이 화면에서 한 번 더 눌러 주세요.", "warn")
            back = request.referrer or ""
            return redirect(back if back.startswith(request.host_url) else url_for("home"))
        g.conn = db.connect()

    @app.teardown_request
    def close_conn(_exc):
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    @app.errorhandler(500)
    def server_error(e):
        """'Internal Server Error' 대신 무엇이 어디서 잘못됐는지 보여 주고 기록 파일에 남긴다 (이 PC 안에서만 보임)."""
        err = getattr(e, "original_exception", None) or e
        return error_page(err, request.method, request.full_path.rstrip("?")), 500

    @app.context_processor
    def inject():
        if "conn" not in g:
            return {"csrf_token": session.get("csrf", ""), "stopped": False, "nav_pending": {}, "listening": False}
        return {"csrf_token": session.get("csrf", ""), "stopped": db.is_stopped(g.conn),
                "nav_pending": pending_counts(g.conn), "listening": live.is_active(g.conn)}

    @app.get("/health")
    def health():
        """실행할 때 이 번호를 이미 이 도우미가 쓰는지, 서버가 떴는지 확인하는 용도."""
        return {"app": "radio_helper", "pid": os.getpid()}

    def one(sql, *params):
        row = g.conn.execute(sql, params).fetchone()
        if row is None:
            abort(404)
        return row

    def form(name: str, default: str = "") -> str:
        return (request.form.get(name) or default).strip()

    # ── 청취 (첫 화면) ─────────────────────────────────────────────
    def channel_list() -> list[str]:
        listed = list(qconfig.channels(g.conn))
        found = [r[0] for r in g.conn.execute(
            "SELECT DISTINCT channel FROM programs WHERE on_air = 1 AND channel NOT IN ('', '미확인')")]
        return listed + sorted(c for c in found if c not in listed)

    def channel_meta() -> list[dict]:
        """듣는 채널 목록: 이름·문자 번호·채팅 앱."""
        numbers = qconfig.channels(g.conn)
        out = []
        for name in channel_list():
            app_ = qconfig.chat_app(g.conn, name)
            out.append({"name": name, "number": numbers.get(name, ""), "app": app_ or "",
                        "app_label": qconfig.app_label(app_) if app_ else ""})
        return out

    def setup_items() -> list[dict]:
        from .quizbot.answerer import get_api_key

        c = g.conn
        try:
            check = json.loads(db.get_setting(c, "quizbot.check") or "null")
        except ValueError:
            check = None
        failed = [i["name"] for i in (check or {}).get("items", []) if not i.get("ok")]
        confirmed = c.execute("SELECT COUNT(*) FROM experiences WHERE user_confirmed = 1").fetchone()[0]
        channel, route = qconfig.get(c, "live.channel"), qconfig.route(c)
        app_ = qconfig.chat_app(c, channel)
        stt = live.view_model(c)["stt_test"] or {}
        if route == "sms":
            sms_check = _json(db.get_setting(c, "sms.check")) or {}
            sms_failed = [i["name"] for i in sms_check.get("items", []) if not i.get("ok")]
            send_item = {"name": "휴대폰 문자 연결 (USB)", "ok": bool(sms_check) and not sms_failed,
                         "url": url_for("sms_page"),
                         "detail": ("확인 필요: " + ", ".join(sms_failed)) if sms_failed else
                         ("" if sms_check else "문자 설정에서 '연결 점검'을 눌러 주세요")}
        elif app_:
            label = qconfig.app_label(app_)
            send_item = {"name": f"{label} 입력칸 지정", "url": url_for("gorilla", app=app_),
                         "ok": bool(qconfig.get(c, f"{app_}.input_rect") or qconfig.get(c, f"{app_}.input_x")),
                         "detail": f"{label} 채팅 입력칸·전송 버튼을 네모로 지정하고 전송 테스트"}
        else:
            send_item = {"name": "보내는 방법", "ok": False, "url": url_for("home"),
                         "detail": f"{channel}은(는) 채팅 앱이 정해지지 않았습니다 — '문자'를 고르거나 채널에 앱을 지정하세요"}
        heard = live.stt_from_listening(c)
        if stt.get("ok"):
            stt_detail = "성공 " + stt.get("at", "")[5:16]
        elif heard:
            stt_detail = f"청취 중 받아쓰기 확인됨 (마지막 {heard[5:16]})"
        elif stt.get("running") and not stt.get("stuck"):
            stt_detail = f"진행 중 — {stt.get('phase') or ''}".rstrip(" —")
        elif stt.get("stuck"):
            stt_detail = "도중에 멈춤 — 첫 화면에서 다시 해 보세요"
        else:
            stt_detail = "실패 — 결과 확인" if stt else "PC 소리 10초를 받아써 보기"
        return [
            {"name": "받아쓰기 테스트", "ok": bool(stt.get("ok") or heard), "url": url_for("home") + "#stt",
             "detail": stt_detail},
            {"name": "Claude API 키", "ok": bool(get_api_key()), "url": url_for("quizbot"),
             "detail": "퀴즈·사연 분석에 필요"},
            send_item,
            {"name": "환경 점검", "ok": bool(check) and not failed, "url": url_for("quizbot"),
             "detail": ("확인 필요: " + ", ".join(failed)) if failed else ("" if check else "아직 안 함")},
            {"name": "사연용 실제 경험", "ok": confirmed > 0, "url": url_for("experiences"),
             "detail": f"확인한 경험 {confirmed}건" if confirmed else "없으면 사연은 보내지 않음 (퀴즈는 됨)"},
        ]

    def home_vm() -> dict:
        vm = live.view_model(g.conn, lines=12)
        vm["captions_open"] = _fresh(db.get_setting(g.conn, "captions.heartbeat"), 15)
        vm["chunk_text"] = live.chunk_text(vm["last_chunk"])
        vm["log_tail"] = runner_log_tail(g.conn) if vm["status"] in ("stalled", "launching") else ""
        vm["sms_today"] = sms_today(g.conn)
        return vm

    @app.get("/")
    def home():
        c = g.conn
        return render_template(
            "home.html", vm=home_vm(), setup=setup_items(), channels=channel_meta(),
            route=qconfig.route(c), options={name: live.flag(c, key) for key, name in LIVE_OPTIONS})

    @app.get("/live.json")
    def live_json():
        return home_vm()

    @app.post("/listen/stt-test")
    def stt_test():
        c = g.conn
        current = live.view_model(c)["stt_test"]
        if current and current.get("running") and not current.get("stuck"):
            flash("받아쓰기 테스트가 이미 진행 중입니다.", "warn")
            return redirect(url_for("home") + "#stt")
        db.set_setting(c, "quizbot.stt_test", json.dumps(
            {"at": db.now(), "running": True, "ok": False, "phase": "시작하는 중", "steps": [], "text": ""},
            ensure_ascii=False))
        launch_quizbot(["stt-test"])
        flash("받아쓰기 테스트를 시작합니다. 고릴라에서 진행자 목소리가 나올 때 해 보세요. 결과는 아래에 단계별로 나옵니다.")
        return redirect(url_for("home") + "#stt")

    @app.post("/listen/start")
    def listen_start():
        c = g.conn
        if db.is_stopped(c):
            flash("일괄 중지가 켜져 있습니다. 설정에서 해제한 뒤 청취를 시작하세요.", "error")
            return redirect(url_for("home"))
        channel = form("channel") or qconfig.get(c, "live.channel")
        if channel not in channel_list():
            abort(400)
        db.set_setting(c, "live.channel", channel)
        for key, name in LIVE_OPTIONS:
            db.set_setting(c, key, "1" if request.form.get(name) else "0")
        route = form("route") or qconfig.route(c)
        if route not in qconfig.ROUTES:
            abort(400)
        if route == "app" and qconfig.chat_app(c, channel) is None:
            route = "sms"
            flash(f"{channel}은(는) 채팅 앱이 정해지지 않아 문자로 보냅니다.", "warn")
        if route == "sms" and not qconfig.sms_number(c, channel):
            flash(f"{channel}의 문자 번호가 없습니다. 아래 '채널 추가·번호 바꾸기'에서 넣어 주세요. 그전까지 보낼 글은 확인 대기에 남습니다.",
                  "warn")
        db.set_setting(c, "send.route", route)
        was_active = live.is_active(c)
        db.set_setting(c, "live.active", "1")
        db.set_setting(c, "quizbot.stop", "0")
        if not qconfig.runner_alive(c) and not _fresh(db.get_setting(c, "quizbot.launched_at"),
                                                      live.LAUNCH_GRACE_SECONDS):
            start_runner(c)  # 실행기는 하나만 뜬다 (두 번째는 잠금을 못 얻고 바로 끝남)
        if live.flag(c, "live.captions") and not _fresh(db.get_setting(c, "captions.heartbeat"), 15):
            launch_quizbot(["captions"])
        if was_active:
            flash(f"청취 설정을 바꿨습니다 — {channel}")
        else:
            db.log(c, "quizbot", f"청취 시작 요청 — {channel}")
            flash(f"{channel} 청취를 시작합니다. 처음 한 번은 음성 인식 모델을 내려받느라 몇 분 걸릴 수 있습니다.")
        return redirect(url_for("home"))

    @app.post("/listen/channels")
    def listen_channels():
        """듣는 채널 추가·번호 바꾸기: '채널=문자번호=앱' 한 줄을 넣거나 고친다."""
        name = form("name")
        number = form("number").replace("-", "").replace(" ", "")
        app_ = form("app")
        if not name or len(name) > 20 or "=" in name or "\n" in name:
            flash("채널 이름은 20자 이내로, '='은 빼고 적어 주세요.", "error")
            return redirect(url_for("home"))
        if number and not re.fullmatch(r"#?\d{3,12}", number):
            flash("문자 번호는 #1077 처럼 적어 주세요.", "error")
            return redirect(url_for("home"))
        if app_ and app_ not in qconfig.CHAT_APPS:
            abort(400)
        lines = [ln for ln in qconfig.get(g.conn, "channels").splitlines()
                 if ln.strip() and ln.split("=", 1)[0].strip() != name]
        lines.append(f"{name}={number}" + (f"={qconfig.app_label(app_)}" if app_ else ""))
        db.set_setting(g.conn, "channels", "\n".join(lines))
        db.set_setting(g.conn, "live.channel", name)
        app_ = qconfig.chat_app(g.conn, name)
        flash(f"채널 '{name}'을(를) 저장했습니다 — 문자 {number or '번호 없음'}, 채팅 앱 "
              f"{qconfig.app_label(app_) if app_ else '없음(문자로만)'}.")
        return redirect(url_for("home"))

    @app.post("/listen/stop")
    def listen_stop():
        c = g.conn
        db.set_setting(c, "live.active", "0")
        if not c.execute("SELECT 1 FROM quiz_schedules WHERE enabled = 1").fetchone():
            db.set_setting(c, "quizbot.stop", "1")  # 예약이 없으면 실행기도 끝낸다
        db.log(c, "quizbot", "청취 중지 요청 (사용자)")
        flash("청취를 멈춥니다. 지금 듣고 있는 10초 조각이 끝나면 멈추고, 자막 창은 몇 초 뒤 닫힙니다.")
        return redirect(url_for("home"))

    # ── 게시판 사연 (원고·코너 제출 순서) ─────────────────────────
    @app.get("/board")
    def dashboard():
        c = g.conn
        counts = {
            "experiences": c.execute("SELECT COUNT(*) FROM experiences").fetchone()[0],
            "confirmed": c.execute("SELECT COUNT(*) FROM experiences WHERE user_confirmed = 1").fetchone()[0],
            "drafts": c.execute("SELECT COUNT(*) FROM drafts WHERE status = 'draft'").fetchone()[0],
            "approved": c.execute("SELECT COUNT(*) FROM drafts WHERE status = 'approved'").fetchone()[0],
            "unresolved": c.execute(
                "SELECT COUNT(*) FROM submissions WHERE post_status IN ('filled','unknown')").fetchone()[0],
            "quizzes": c.execute("SELECT COUNT(*) FROM quizzes WHERE entry_status = 'pending'").fetchone()[0],
        }
        targets = c.execute("SELECT * FROM corners WHERE is_target = 1").fetchall()
        target_status = [(t, gates.corner_blockers(t)) for t in targets]
        events = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT 8").fetchall()
        profile = db.get_profile(c)
        return render_template("dashboard.html", counts=counts, target_status=target_status,
                               events=events, profile_ready=bool(profile["nickname"]))

    # ── 기본 정보 ─────────────────────────────────────────────────
    @app.route("/profile", methods=["GET", "POST"])
    def profile():
        if request.method == "POST":
            db.save_profile(g.conn, {k: request.form.get(k, "") for k in db.PROFILE_KEYS})
            flash("기본 정보를 저장했습니다.")
            return redirect(url_for("profile"))
        return render_template("profile.html", p=db.get_profile(g.conn))

    # ── 코너 ──────────────────────────────────────────────────────
    @app.get("/corners")
    def corners():
        program = request.args.get("program", "")
        if program:
            rows = g.conn.execute("SELECT * FROM corners WHERE program = ? ORDER BY is_target DESC, id",
                                  (program,)).fetchall()
        else:
            rows = g.conn.execute("SELECT * FROM corners ORDER BY is_target DESC, program, id").fetchall()
        programs_ = [r["program"] for r in g.conn.execute(
            "SELECT DISTINCT program FROM corners ORDER BY program").fetchall()]
        counts = g.conn.execute(
            """SELECT p.title, p.channel, p.checked_at, (SELECT COUNT(*) FROM corners c WHERE c.program = p.title) AS boards
               FROM programs p WHERE p.on_air = 1
               ORDER BY CASE p.channel WHEN '파워FM' THEN 0 WHEN '러브FM' THEN 1 ELSE 2 END, p.channel, p.start_time"""
        ).fetchall()
        groups: dict[str, list] = {}
        for c in counts:
            groups.setdefault(c["channel"], []).append(c)
        last = g.conn.execute("SELECT * FROM events WHERE kind = 'programs' ORDER BY id DESC LIMIT 1").fetchone()
        return render_template("corners.html", corners=rows, programs=programs_, program=program,
                               groups=groups, total=sum(c["boards"] for c in counts), last=last)

    # ── 파워FM 프로그램 ───────────────────────────────────────────
    @app.get("/programs")
    def programs():
        rows = g.conn.execute(
            """SELECT p.*, (SELECT COUNT(*) FROM corners c WHERE c.program = p.title) AS boards
               FROM programs p WHERE p.on_air = 1
               ORDER BY CASE p.channel WHEN '파워FM' THEN 0 WHEN '러브FM' THEN 1 ELSE 2 END, p.channel, p.start_time"""
        ).fetchall()
        candidates = g.conn.execute("SELECT * FROM programs WHERE on_air = 0 ORDER BY id").fetchall()
        last = g.conn.execute(
            "SELECT * FROM events WHERE kind = 'programs' ORDER BY id DESC LIMIT 1").fetchone()
        return render_template("programs.html", rows=rows, candidates=candidates, last=last)

    @app.post("/programs/refresh")
    def programs_refresh():
        back = "corners" if request.form.get("back") == "corners" else "programs"
        if db.is_stopped(g.conn):
            flash("일괄 중지가 켜져 있습니다.", "error")
            return redirect(url_for(back))
        log_name = launch_programs_refresh()
        flash("공식 페이지에서 방송 시간과 코너·게시판을 읽는 중입니다. 프로그램이 많아 3~5분 걸립니다. "
              f"끝나면 이 화면을 새로 고치세요. (기록: {log_name})")
        return redirect(url_for(back))

    @app.route("/programs/<int:pid>", methods=["GET", "POST"])
    def program_edit(pid):
        prog = one("SELECT * FROM programs WHERE id = ?", pid)
        if request.method == "POST":
            title = form("title") or prog["title"]
            g.conn.execute(
                """UPDATE programs SET channel = ?, title = ?, host = ?, start_time = ?, end_time = ?, days = ?,
                       on_air = ?, source = ?, updated_at = ? WHERE id = ?""",
                (form("channel", prog["channel"]), title, form("host") or None, form("start_time") or None,
                 form("end_time") or None, form("days") or None, 1 if request.form.get("on_air") else 0,
                 "사용자 수정", db.now(), pid))
            if title != prog["title"]:
                g.conn.execute("UPDATE corners SET program = ? WHERE program = ?", (title, prog["title"]))
            g.conn.commit()
            flash("프로그램 정보를 저장했습니다.")
            return redirect(url_for("programs"))
        return render_template("program_edit.html", p=prog)

    @app.route("/corners/<int:cid>", methods=["GET", "POST"])
    def corner_edit(cid):
        corner = one("SELECT * FROM corners WHERE id = ?", cid)
        if request.method == "POST":
            limit = form("char_limit")
            g.conn.execute(
                """UPDATE corners SET title = ?, recruiting = ?, char_limit = ?, deadline = ?, required_fields = ?,
                       ai_assist_policy = ?, is_target = ?, write_url = ?, title_selector = ?, body_selector = ?,
                       song_selector = ?, updated_at = ? WHERE id = ?""",
                ((form("corner_title") or corner["title"])[:80],
                 form("recruiting", "unknown"), int(limit) if limit.isdigit() else None, form("deadline") or None,
                 form("required_fields") or None, form("ai_assist_policy", "unknown"),
                 1 if request.form.get("is_target") else 0, form("write_url") or None,
                 form("title_selector") or None, form("body_selector") or None, form("song_selector") or None,
                 db.now(), cid))
            g.conn.commit()
            if db.update_corner_notice(g.conn, cid, request.form.get("notice_text", "")):
                db.log(g.conn, "notice", f"{corner['title']} 공지 변경 감지 → 재검토 전 제출 중지")
                flash("공지 내용이 이전과 다릅니다. 재검토 후 '공지 확인 완료'를 눌러야 제출할 수 있습니다.", "warn")
            else:
                flash("코너 정보를 저장했습니다.")
            return redirect(url_for("corner_edit", cid=cid))
        blockers, warnings = gates.corner_blockers(corner)
        reports = sorted((db.data_dir() / "inspect").glob(f"corner{cid}_*.json"), reverse=True)[:3]
        return render_template("corner_edit.html", c=corner, blockers=blockers, warnings=warnings,
                               reports=[(r.name, r.read_text(encoding="utf-8")) for r in reports])

    @app.post("/corners/<int:cid>/confirm-notice")
    def corner_confirm_notice(cid):
        corner = one("SELECT * FROM corners WHERE id = ?", cid)
        g.conn.execute("UPDATE corners SET notice_checked_at = ?, notice_changed = 0, updated_at = ? WHERE id = ?",
                       (db.now(), db.now(), cid))
        g.conn.commit()
        db.log(g.conn, "notice", f"{corner['title']} 모집 공지 확인 완료 (사용자)")
        flash("공지 확인 시각을 기록했습니다.")
        return redirect(url_for("corner_edit", cid=cid))

    @app.post("/corners/<int:cid>/inspect")
    def corner_inspect(cid):
        corner = one("SELECT * FROM corners WHERE id = ?", cid)
        result = gates.evaluate_inspect(g.conn, cid)
        if not result.ok:
            for b in result.blockers:
                flash(b, "error")
            return redirect(url_for("corner_edit", cid=cid))
        log_name = launch_autofill(["--corner", str(cid), "--mode", gates.MODE_INSPECT])
        flash(f"{corner['title']} 글쓰기 화면 점검용 브라우저를 엽니다. 직접 로그인하면 입력 요소만 읽고 닫습니다. "
              f"(기록: {log_name})")
        return redirect(url_for("corner_edit", cid=cid))

    # ── 실제 경험 ─────────────────────────────────────────────────
    EXP_FIELDS = ["label", "when_text", "people", "story", "quotes", "quote_kind", "highlight", "ending",
                  "fixed_facts", "hide", "song", "prior_history", "gorilla_line", "about_person_id"]

    def people_options():
        return g.conn.execute("SELECT id, name, alias, grp, side FROM people ORDER BY sort, id").fetchall()

    @app.get("/experiences")
    def experiences():
        only = request.args.get("only", "")
        rows = g.conn.execute(
            """SELECT e.*, p.alias AS about_alias, (SELECT COUNT(*) FROM submissions s WHERE s.experience_id = e.id
                            AND s.post_status IN ('filled','posted','unknown'))
                         + (SELECT COUNT(*) FROM story_posts sp WHERE sp.experience_id = e.id
                            AND sp.status IN ('entered','posted','unknown')) AS used
               FROM experiences e LEFT JOIN people p ON p.id = e.about_person_id"""
            + (" WHERE e.from_event IS NOT NULL AND e.user_confirmed = 0" if only == "events" else
               " WHERE e.from_library_id IS NOT NULL AND e.user_confirmed = 0" if only == "library" else "")
            + " ORDER BY e.id DESC").fetchall()
        from .quizbot import enrich

        pending_events = g.conn.execute(
            "SELECT COUNT(*) FROM experiences WHERE from_event IS NOT NULL AND user_confirmed = 0").fetchone()[0]
        pending_library = g.conn.execute(
            "SELECT COUNT(*) FROM experiences WHERE from_library_id IS NOT NULL AND user_confirmed = 0").fetchone()[0]
        ev = enrich.view(g.conn)
        if ev["state"] in ("stalled", "done") or ev.get("error"):
            ev["log"] = log_tail(db.get_setting(g.conn, enrich.LOG_KEY) or "", 1500)
        return render_template("experiences.html", rows=rows, only=only, pending_events=pending_events,
                               pending_library=pending_library, enrich_left=ev["left"], enrich_status=ev)

    REVIEW_PAGE = 10

    def review_where(src: str) -> str:
        return ("e.user_confirmed = 0" + (" AND e.from_library_id IS NOT NULL" if src == "library" else ""))

    @app.get("/experiences/review")
    def experiences_review():
        """확인 전 경험을 10개씩 펼쳐 보고, 그 자리에서 고치고, 맞는 것만 체크해 다음 10개로 넘긴다."""
        src = "all" if request.args.get("src") == "all" else "library"
        after = request.args.get("after", 0, type=int)
        rows = g.conn.execute(
            f"""SELECT e.*, p.alias AS about_alias, l.code AS lib_code, l.origin AS lib_origin FROM experiences e
                LEFT JOIN people p ON p.id = e.about_person_id LEFT JOIN story_library l ON l.id = e.from_library_id
                WHERE {review_where(src)} AND e.id > ? ORDER BY e.id LIMIT ?""", (after, REVIEW_PAGE)).fetchall()
        left = g.conn.execute(f"SELECT COUNT(*) FROM experiences e WHERE {review_where(src)}").fetchone()[0]
        later = g.conn.execute(f"SELECT COUNT(*) FROM experiences e WHERE {review_where(src)} AND e.id > ?",
                               (rows[-1]["id"] if rows else after,)).fetchone()[0]
        return render_template("experiences_review.html", rows=rows, src=src, after=after, left=left, later=later,
                               page_size=REVIEW_PAGE)

    @app.post("/experiences/review")
    def experiences_review_save():
        src = "all" if form("src") == "all" else "library"
        ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()]
        confirmed = edited = 0
        for eid in ids:
            e = g.conn.execute("SELECT * FROM experiences WHERE id = ? AND user_confirmed = 0", (eid,)).fetchone()
            if e is None:
                continue
            values = {k: (request.form.get(f"{k}_{eid}") or "").strip() for k in ("label", "when_text", "story")}
            values["story"] = values["story"] or e["story"]          # 본문을 지우면 원래 글을 둔다
            if any((e[k] or "") != values[k] for k in values):
                g.conn.execute("UPDATE experiences SET label = ?, when_text = ?, story = ?, updated_at = ? WHERE id = ?",
                               (values["label"], values["when_text"], values["story"], db.now(), eid))
                edited += 1
            if request.form.get(f"ok_{eid}"):
                g.conn.execute("UPDATE experiences SET user_confirmed = 1, updated_at = ? WHERE id = ?", (db.now(), eid))
                confirmed += 1
        g.conn.commit()
        if confirmed:
            db.log(g.conn, "record", f"경험 {confirmed}건을 '실제로 있었던 일'로 확인 (10개씩 확인, 사용자)")
        flash(f"{confirmed}건 확인" + (f" · {edited}건 고침" if edited else "")
              + (f" · {len(ids) - confirmed}건은 확인 전으로 남김(나중에 다시 볼 수 있음)" if len(ids) > confirmed else "") + ".")
        return redirect(url_for("experiences_review", src=src, after=max(ids) if ids else 0))

    @app.post("/experiences/confirm")
    def experiences_confirm():
        """관계도 실제 사건에서 옮긴 경험 중 사용자가 하나씩 체크한 것만 '실제로 있었던 일'로 확인한다."""
        ids = [int(x) for x in request.form.getlist("eid") if x.isdigit()]
        n = 0
        for eid in ids:
            n += g.conn.execute("UPDATE experiences SET user_confirmed = 1, updated_at = ? WHERE id = ? "
                                "AND from_event IS NOT NULL AND user_confirmed = 0", (db.now(), eid)).rowcount
        g.conn.commit()
        if n:
            db.log(g.conn, "record", f"관계도 실제 사건 경험 {n}건을 '실제로 있었던 일'로 확인 (사용자)")
        flash(f"{n}건을 '실제로 있었던 일'로 확인했습니다. 이제 사연 재료로 씁니다." if n else "체크한 사건이 없습니다.",
              "message" if n else "warn")
        return redirect(url_for("experiences", only=form("only")))

    @app.route("/experiences/new", methods=["GET", "POST"])
    @app.route("/experiences/<int:eid>", methods=["GET", "POST"])
    def experience_edit(eid=None):
        exp = one("SELECT * FROM experiences WHERE id = ?", eid) if eid else None
        corners_ = g.conn.execute("SELECT id, title, is_target FROM corners ORDER BY is_target DESC, id").fetchall()
        if request.method == "POST":
            values = {f: form(f) for f in EXP_FIELDS}
            about = request.form.get("about_person_id", type=int)
            values["about_person_id"] = about if about and g.conn.execute(
                "SELECT 1 FROM people WHERE id = ? AND side != 'self'", (about,)).fetchone() else None
            if not values["story"]:
                flash("'실제로 있었던 일'은 비워 둘 수 없습니다.", "error")
                return render_template("experience_edit.html", e=values, eid=eid, drafts=[], corners=corners_,
                                       people=people_options())
            values["quote_kind"] = values["quote_kind"] if values["quote_kind"] in ("exact", "gist") else "none"
            confirmed = 1 if request.form.get("user_confirmed") else 0
            if exp:
                changed = any((exp[f] or "") != (values[f] or "") for f in EXP_FIELDS)
                g.conn.execute(
                    f"UPDATE experiences SET {', '.join(f'{f} = ?' for f in EXP_FIELDS)}, user_confirmed = ?, "
                    "updated_at = ? WHERE id = ?",
                    (*values.values(), confirmed, db.now(), eid))
                if changed:
                    # 재료가 바뀌면 그 재료로 만든 승인 원고는 다시 확인한다.
                    g.conn.execute("UPDATE drafts SET status = 'draft', fact_confirmed = 0, approved_at = NULL "
                                   "WHERE experience_id = ? AND status = 'approved'", (eid,))
            else:
                cur = g.conn.execute(
                    f"INSERT INTO experiences ({', '.join(EXP_FIELDS)}, user_confirmed, created_at, updated_at) "
                    f"VALUES ({', '.join('?' * len(EXP_FIELDS))}, ?, ?, ?)",
                    (*values.values(), confirmed, db.now(), db.now()))
                eid = cur.lastrowid
            g.conn.commit()
            flash("경험을 저장했습니다.")
            return redirect(url_for("experience_edit", eid=eid))
        drafts = g.conn.execute(
            "SELECT d.*, c.title AS corner_title FROM drafts d JOIN corners c ON c.id = d.corner_id "
            "WHERE d.experience_id = ? ORDER BY d.id DESC", (eid,)).fetchall() if eid else []
        prefill = None
        if exp is None and request.args.get("about", type=int):
            # 인물 화면의 '실제 경험 만들기': 그 사람 이야기로 미리 채움
            prefill = {f: "" for f in EXP_FIELDS}
            prefill.update({"about_person_id": request.args.get("about", type=int),
                            "when_text": request.args.get("when", ""), "story": request.args.get("text", ""),
                            "label": request.args.get("text", "")[:30], "quote_kind": "none"})
        return render_template("experience_edit.html", e=exp or prefill, eid=eid, drafts=drafts, corners=corners_,
                               people=people_options())

    # ── 원고 ──────────────────────────────────────────────────────
    @app.get("/drafts")
    def drafts():
        rows = g.conn.execute(
            """SELECT d.*, c.title AS corner_title, e.label AS exp_label, e.story AS exp_story
               FROM drafts d JOIN corners c ON c.id = d.corner_id JOIN experiences e ON e.id = d.experience_id
               WHERE d.status != 'archived' ORDER BY d.id DESC""").fetchall()
        return render_template("drafts.html", rows=rows)

    @app.post("/drafts/new")
    def draft_new():
        exp = one("SELECT * FROM experiences WHERE id = ?", int(form("experience_id") or 0))
        corner = one("SELECT * FROM corners WHERE id = ?", int(form("corner_id") or 0))
        from . import people as people_mod
        from .quizbot import story as story_mod

        d = generator.template_draft(exp, db.get_profile(g.conn), corner, about=story_mod.about_of(g.conn, exp),
                                     private=people_mod.private_terms(g.conn))
        cur = g.conn.execute(
            "INSERT INTO drafts (experience_id, corner_id, title, body, song, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'template', ?, ?)",
            (exp["id"], corner["id"], d["title"], d["body"], d["song"], db.now(), db.now()))
        g.conn.commit()
        flash("템플릿 초안을 만들었습니다. 그대로 다듬거나, 아래 'AI 요청문'으로 다듬은 결과를 붙여 넣으세요.")
        return redirect(url_for("draft_edit", did=cur.lastrowid))

    @app.route("/drafts/<int:did>", methods=["GET", "POST"])
    def draft_edit(did):
        d = one("SELECT * FROM drafts WHERE id = ?", did)
        if request.method == "POST":
            title, body, song = form("title"), request.form.get("body", "").strip(), form("song")
            source = form("source", d["source"])
            if source not in SOURCE:
                source = d["source"]
            if (title, body, song) != (d["title"], d["body"], d["song"]):
                g.conn.execute(
                    "UPDATE drafts SET title = ?, body = ?, song = ?, source = ?, status = 'draft', fact_confirmed = 0, "
                    "approved_at = NULL, updated_at = ? WHERE id = ?", (title, body, song, source, db.now(), did))
                g.conn.commit()
                flash("원고를 저장했습니다. 내용이 바뀌어 승인이 해제되었습니다." if d["status"] == "approved"
                      else "원고를 저장했습니다.")
            else:
                g.conn.execute("UPDATE drafts SET source = ?, updated_at = ? WHERE id = ?", (source, db.now(), did))
                g.conn.commit()
                flash("바뀐 내용이 없습니다.")
            return redirect(url_for("draft_edit", did=did))
        exp = one("SELECT * FROM experiences WHERE id = ?", d["experience_id"])
        corner = one("SELECT * FROM corners WHERE id = ?", d["corner_id"])
        findings = gates.draft_findings(g.conn, d)
        fill_gate = gates.evaluate(g.conn, did, gates.MODE_FILL)
        subs = g.conn.execute("SELECT * FROM submissions WHERE draft_id = ? ORDER BY id DESC", (did,)).fetchall()
        prompt = generator.chat_prompt(exp, db.get_profile(g.conn), corner)
        return render_template("draft_edit.html", d=d, exp=exp, corner=corner, findings=findings,
                               fill_gate=fill_gate, subs=subs, prompt=prompt, length=len(d["body"]))

    @app.post("/drafts/<int:did>/paste")
    def draft_paste(did):
        d = one("SELECT * FROM drafts WHERE id = ?", did)
        parsed = generator.parse_chat_result(request.form.get("result", ""))
        if not parsed["body"]:
            flash("'본문:' 부분을 찾지 못했습니다. 요청문의 출력 형식대로 받은 결과를 붙여 넣으세요.", "error")
            return redirect(url_for("draft_edit", did=did))
        title = parsed["titles"][0] if parsed["titles"] else d["title"]
        g.conn.execute(
            "UPDATE drafts SET title = ?, body = ?, song = ?, source = 'pasted', status = 'draft', fact_confirmed = 0, "
            "approved_at = NULL, updated_at = ? WHERE id = ?",
            (title, parsed["body"], parsed["song"] or d["song"], db.now(), did))
        g.conn.commit()
        if len(parsed["titles"]) > 1:
            flash("다른 제목 후보: " + " / ".join(parsed["titles"][1:]))
        if parsed["needs_check"]:
            flash("AI가 '확인 필요'로 표시한 항목 — 실제와 다르면 본문에 넣지 마세요: "
                  + " / ".join(parsed["needs_check"]), "warn")
        flash("붙여 넣은 결과로 원고를 바꿨습니다. 사실 확인 후 승인하세요.")
        return redirect(url_for("draft_edit", did=did))

    @app.post("/drafts/<int:did>/approve")
    def draft_approve(did):
        d = one("SELECT * FROM drafts WHERE id = ?", did)
        if not request.form.get("fact_confirmed"):
            flash("'실제와 같음' 확인란을 체크해야 승인할 수 있습니다.", "error")
            return redirect(url_for("draft_edit", did=did))
        blocking = [f.message for f in gates.draft_findings(g.conn, d) if f.level == "block"]
        if blocking:
            for m in blocking:
                flash(m, "error")
            flash("위 문제를 고친 뒤 승인할 수 있습니다.", "error")
            return redirect(url_for("draft_edit", did=did))
        g.conn.execute("UPDATE drafts SET status = 'approved', fact_confirmed = 1, approved_at = ?, updated_at = ? "
                       "WHERE id = ?", (db.now(), db.now(), did))
        g.conn.commit()
        db.log(g.conn, "approve", f"원고 #{did} 승인 (사용자)")
        flash("승인했습니다.")
        return redirect(url_for("draft_edit", did=did))

    @app.post("/drafts/<int:did>/unapprove")
    def draft_unapprove(did):
        one("SELECT id FROM drafts WHERE id = ?", did)
        g.conn.execute("UPDATE drafts SET status = 'draft', fact_confirmed = 0, approved_at = NULL, updated_at = ? "
                       "WHERE id = ?", (db.now(), did))
        g.conn.commit()
        flash("승인을 취소했습니다.")
        return redirect(url_for("draft_edit", did=did))

    @app.post("/drafts/<int:did>/archive")
    def draft_archive(did):
        one("SELECT id FROM drafts WHERE id = ?", did)
        g.conn.execute("UPDATE drafts SET status = 'archived', updated_at = ? WHERE id = ?", (db.now(), did))
        g.conn.commit()
        flash(f"원고 #{did}을(를) 보관했습니다.")
        return redirect(url_for("drafts"))

    @app.post("/drafts/<int:did>/run")
    def draft_run(did):
        one("SELECT id FROM drafts WHERE id = ?", did)
        mode = form("mode")
        if mode not in (gates.MODE_MOCK, gates.MODE_FILL):
            abort(400)
        result = gates.evaluate(g.conn, did, mode)
        if not result.ok:
            for b in result.blockers:
                flash(b, "error")
            return redirect(url_for("draft_edit", did=did))
        args = ["--draft", str(did), "--mode", mode]
        if mode == gates.MODE_MOCK:
            args += ["--base-url", request.host_url]
        log_name = launch_autofill(args)
        if mode == gates.MODE_MOCK:
            flash(f"모의 글쓰기 화면에 입력 시험을 시작합니다. (기록: {log_name})")
        else:
            flash("실제 글쓰기 화면용 브라우저를 엽니다. 직접 로그인하면 원고를 입력만 합니다. "
                  f"등록 버튼은 내용을 확인한 뒤 직접 누르세요. (기록: {log_name})")
        return redirect(url_for("draft_edit", did=did))

    @app.post("/drafts/<int:did>/record")
    def draft_record(did):
        """도구 없이 직접 복사·붙여넣기로 올린 경우의 기록."""
        d = one("SELECT * FROM drafts WHERE id = ?", did)
        status = form("post_status", "unknown")
        if status not in STATUS_LABELS:
            abort(400)
        cur = g.conn.execute(
            "INSERT INTO submissions (draft_id, experience_id, corner_id, title, body, post_status, post_url, note, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (did, d["experience_id"], d["corner_id"], d["title"], d["body"], status, form("post_url") or None,
             "사용자가 직접 등록 후 기록", db.now(), db.now()))
        g.conn.commit()
        db.log(g.conn, "record", f"원고 #{did} 직접 등록 기록 → 제출 이력 #{cur.lastrowid} ({STATUS_LABELS[status]})")
        flash("제출 이력에 기록했습니다.")
        return redirect(url_for("submissions"))

    # ── 제출 이력 ─────────────────────────────────────────────────
    @app.get("/submissions")
    def submissions():
        rows = g.conn.execute(
            """SELECT s.*, c.title AS corner_title, e.label AS exp_label FROM submissions s
               JOIN corners c ON c.id = s.corner_id JOIN experiences e ON e.id = s.experience_id
               ORDER BY s.id DESC""").fetchall()
        return render_template("submissions.html", rows=rows, shots=evidence.counts(g.conn, "submissions"))

    @app.post("/submissions/<int:sid>")
    def submission_update(sid):
        one("SELECT id FROM submissions WHERE id = ?", sid)
        vals = {k: form(k) for k in ("post_status", "adopted", "won", "prize_received")}
        if vals["post_status"] not in STATUS_LABELS or any(vals[k] not in YES_NO for k in ("adopted", "won", "prize_received")):
            abort(400)
        g.conn.execute(
            "UPDATE submissions SET post_status = ?, post_url = ?, adopted = ?, won = ?, prize_received = ?, note = ?, "
            "updated_at = ? WHERE id = ?",
            (vals["post_status"], form("post_url") or None, vals["adopted"], vals["won"], vals["prize_received"],
             form("note") or None, db.now(), sid))
        g.conn.commit()
        flash(f"제출 이력 #{sid}을(를) 저장했습니다.")
        return redirect(url_for("submissions"))

    # ── 퀴즈 ──────────────────────────────────────────────────────
    @app.get("/quizzes")
    def quizzes():
        c = g.conn
        rows = c.execute("SELECT * FROM quizzes WHERE source IS NULL OR source != 'auto' "
                         "ORDER BY broadcast_date DESC, id DESC").fetchall()
        stopped = db.is_stopped(c)
        on_air = c.execute("SELECT title FROM programs WHERE on_air = 1 ORDER BY start_time").fetchall()
        pending = c.execute("SELECT * FROM quizzes WHERE source = 'auto' AND entry_status IN ('pending', 'failed') "
                            "ORDER BY id DESC LIMIT 30").fetchall()
        auto = c.execute("SELECT * FROM quizzes WHERE source = 'auto' AND entry_status NOT IN ('pending', 'failed') "
                         "ORDER BY id DESC LIMIT 100").fetchall()
        return render_template("quizzes.html", rows=[(q, quiz.hold_reasons(q, stopped)) for q in rows],
                               pending=pending, auto=auto, YES_NO_KEYS=list(YES_NO),
                               today=time.strftime("%Y-%m-%d"), programs=[r["title"] for r in on_air],
                               channels=channel_list(), shots=evidence.counts(g.conn, "quizzes"))

    @app.post("/quizzes/new")
    def quiz_new():
        fields = {k: form(k) for k in ("account", "channel", "program", "broadcast_date", "question_key", "kind",
                                       "question", "options", "deadline", "entry_channel", "gorilla_accepted")}
        if not fields["program"] or not fields["broadcast_date"]:
            flash("프로그램과 방송일은 꼭 적어야 합니다.", "error")
            return redirect(url_for("quizzes"))
        try:
            qid, created = quiz.add_or_bump(g.conn, fields)
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("quizzes"))
        flash(f"퀴즈 #{qid}을(를) 추가했습니다." if created
              else f"같은 문제(#{qid})가 이미 있습니다. 재안내로 보고 한 건으로 묶었습니다.",
              "message" if created else "warn")
        return redirect(url_for("quizzes"))

    @app.post("/quizzes/<int:qid>")
    def quiz_update(qid):
        one("SELECT id FROM quizzes WHERE id = ?", qid)
        vals = {k: form(k) for k in ("kind", "gorilla_accepted", "entry_status", "answer_accepted", "won",
                                     "prize_received")}
        if (vals["kind"] not in quiz.KIND_LABELS or vals["entry_status"] not in quiz.ENTRY_LABELS
                or any(vals[k] not in YES_NO for k in ("gorilla_accepted", "answer_accepted", "won", "prize_received"))):
            abort(400)
        g.conn.execute(
            """UPDATE quizzes SET kind = ?, question = ?, options = ?, deadline = ?, gorilla_accepted = ?, answer = ?,
                   answer_verified = ?, entry_status = ?, answer_accepted = ?, won = ?, prize_received = ?, note = ?,
                   updated_at = ? WHERE id = ?""",
            (vals["kind"], form("question") or None, form("options") or None, form("deadline") or None,
             vals["gorilla_accepted"], form("answer") or None, 1 if request.form.get("answer_verified") else 0,
             vals["entry_status"], vals["answer_accepted"], vals["won"], vals["prize_received"],
             form("note") or None, db.now(), qid))
        g.conn.commit()
        flash(f"퀴즈 #{qid}을(를) 저장했습니다.")
        return redirect(url_for("quizzes"))

    @app.post("/quizzes/<int:qid>/result")
    def quiz_result(qid):
        """자동으로 보낸 퀴즈의 결과(정답 인정·당첨·상품 수령)만 기록한다."""
        one("SELECT id FROM quizzes WHERE id = ?", qid)
        vals = {k: form(k, "unknown") for k in ("answer_accepted", "won", "prize_received")}
        if any(v not in YES_NO for v in vals.values()):
            abort(400)
        g.conn.execute("UPDATE quizzes SET answer_accepted = ?, won = ?, prize_received = ?, updated_at = ? WHERE id = ?",
                       (vals["answer_accepted"], vals["won"], vals["prize_received"], db.now(), qid))
        g.conn.commit()
        flash(f"퀴즈 #{qid} 결과를 저장했습니다.")
        return redirect(url_for("quizzes") + "#auto")

    # ── 설정·일괄 중지 ────────────────────────────────────────────
    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        if request.method == "POST":
            stop = "1" if form("global_stop") == "1" else "0"
            db.set_setting(g.conn, "global_stop", stop)
            if stop == "1":
                db.set_setting(g.conn, "quizbot.stop", "1")  # 퀴즈 자동 참여도 멈춘다
            db.log(g.conn, "stop", "일괄 중지 켬" if stop == "1" else "일괄 중지 해제")
            flash("일괄 중지를 켰습니다. 모든 입력 작업이 멈춥니다." if stop == "1" else "일괄 중지를 해제했습니다.")
            return redirect(url_for("settings"))
        events = g.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100").fetchall()
        log_dir = db.data_dir() / "logs"
        logs = sorted(log_dir.glob("*.log"), key=lambda f: f.stat().st_mtime, reverse=True)[:6] if log_dir.exists() else []
        return render_template("settings.html", events=events,
                               logs=[(p.name, p.read_text(encoding="utf-8", errors="replace")[-4000:]) for p in logs],
                               data_dir=db.data_dir())

    # ── 퀴즈 자동 참여 ────────────────────────────────────────────
    def schedule_rows():
        return g.conn.execute(
            """SELECT s.*, p.title AS program FROM quiz_schedules s JOIN programs p ON p.id = s.program_id
               ORDER BY s.start_time""").fetchall()

    @app.get("/quizbot")
    def quizbot():
        from .quizbot.answerer import get_api_key

        c = g.conn
        schedules = schedule_rows()
        now = datetime.now()
        active = qschedule.active_window(schedules, now)
        nxt = qschedule.next_window(schedules, now)
        by_id = {s["id"]: s for s in schedules}
        try:
            check = json.loads(db.get_setting(c, "quizbot.check") or "null")
        except ValueError:
            check = None
        programs_ = c.execute("SELECT id, channel, title, start_time, end_time FROM programs WHERE on_air = 1 "
                              "ORDER BY channel, start_time").fetchall()
        return render_template(
            "quizbot.html", schedules=schedules, active=active, nxt=nxt, by_id=by_id,
            alive=qconfig.runner_alive(c), state=db.get_setting(c, "quizbot.state"),
            heartbeat=db.get_setting(c, "quizbot.heartbeat"), check=check, has_key=bool(get_api_key()),
            programs=programs_, days_label=qschedule.days_label, day_names=qschedule.DAY_NAMES,
            autostart=qconfig.get(c, "quizbot.autostart") == "1",
            live_min_confidence=qconfig.get(c, "live.min_confidence"))

    @app.post("/quizbot/start")
    def quizbot_start():
        if db.is_stopped(g.conn):
            flash("일괄 중지가 켜져 있습니다. 설정에서 해제한 뒤 시작하세요.", "error")
        elif qconfig.runner_alive(g.conn):
            flash("이미 실행 중입니다.", "warn")
        else:
            db.set_setting(g.conn, "quizbot.stop", "0")
            log_name = start_runner(g.conn)
            flash(f"예약 듣기 실행기를 시작합니다. 예약 시간에만 듣습니다. 바로 들으려면 첫 화면의 '청취 시작'을 누르세요. (기록: {log_name})")
        return redirect(url_for("quizbot"))

    @app.post("/quizbot/stop")
    def quizbot_stop():
        db.set_setting(g.conn, "quizbot.stop", "1")
        db.set_setting(g.conn, "live.active", "0")
        db.log(g.conn, "quizbot", "멈춤 요청 (사용자)")
        flash("멈춤을 요청했습니다. 지금 듣고 있는 구간이 끝나면 멈춥니다 (최대 30초 정도).")
        return redirect(url_for("quizbot"))

    @app.post("/quizbot/settings")
    def quizbot_settings():
        db.set_setting(g.conn, "quizbot.autostart", "1" if request.form.get("autostart") else "0")
        if form("live_min_confidence"):
            db.set_setting(g.conn, "live.min_confidence", str(_confidence(form("live_min_confidence"))))
        key = (request.form.get("api_key") or "").strip()
        if key:
            from .quizbot.answerer import save_api_key

            try:
                save_api_key(key)
                db.log(g.conn, "quizbot", "Claude API 키를 윈도우 자격 증명 관리자에 저장함")
                flash("API 키를 저장했습니다. (이 PC의 자격 증명 관리자에만 저장되며 화면·기록에 남지 않습니다)")
            except Exception as e:
                flash(f"API 키를 저장하지 못했습니다: {type(e).__name__}. 환경 변수 ANTHROPIC_API_KEY 로 설정할 수도 있습니다.",
                      "error")
        else:
            flash("설정을 저장했습니다.")
        return redirect(url_for("quizbot"))

    @app.post("/quizbot/schedules")
    def quizbot_schedule_new():
        prog = one("SELECT * FROM programs WHERE id = ?", int(form("program_id") or 0))
        start, end = form("start_time") or prog["start_time"], form("end_time") or prog["end_time"]
        days = "".join(sorted(d for d in request.form.getlist("days") if d in "0123456"))
        if not (start and end and days):
            flash("시작·끝 시간과 요일을 정하세요.", "error")
            return redirect(url_for("quizbot"))
        if not (qschedule.valid_hm(start) and qschedule.valid_hm(end)) or start == end:
            flash("시간은 HH:MM 형식(00:00~23:59)으로, 시작과 끝을 다르게 적어 주세요.", "error")
            return redirect(url_for("quizbot"))
        g.conn.execute(
            """INSERT INTO quiz_schedules (program_id, days, start_time, end_time, auto_submit, min_confidence,
                   gorilla_confirmed, story_enabled, story_auto_user_line, story_auto_ai, gift_enabled, enabled,
                   created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (prog["id"], days, start, end, 1 if request.form.get("auto_submit") else 0,
             _confidence(form("min_confidence")), 1 if request.form.get("gorilla_confirmed") else 0,
             1 if request.form.get("story_enabled") else 0, 1 if request.form.get("story_auto_user_line") else 0,
             1 if request.form.get("story_auto_ai") else 0,
             1 if request.form.get("gift_enabled") else 0, db.now(), db.now()))
        g.conn.commit()
        db.log(g.conn, "quizbot", f"예약 추가: {prog['title']} {qschedule.days_label(days)} {start}~{end}")
        flash(f"{prog['title']} 예약을 추가했습니다.")
        return redirect(url_for("quizbot"))

    @app.post("/quizbot/schedules/<int:sid>")
    def quizbot_schedule_update(sid):
        sch = one("SELECT * FROM quiz_schedules WHERE id = ?", sid)
        if form("action") == "delete":
            g.conn.execute("DELETE FROM quiz_schedules WHERE id = ?", (sid,))
            g.conn.commit()
            flash("예약을 지웠습니다.")
            return redirect(url_for("quizbot"))
        g.conn.execute(
            """UPDATE quiz_schedules SET enabled = ?, auto_submit = ?, min_confidence = ?, gorilla_confirmed = ?,
                   story_enabled = ?, story_auto_user_line = ?, story_auto_ai = ?, gift_enabled = ?, updated_at = ?
               WHERE id = ?""",
            (1 if request.form.get("enabled") else 0, 1 if request.form.get("auto_submit") else 0,
             _confidence(form("min_confidence"), sch["min_confidence"]),
             1 if request.form.get("gorilla_confirmed") else 0, 1 if request.form.get("story_enabled") else 0,
             1 if request.form.get("story_auto_user_line") else 0, 1 if request.form.get("story_auto_ai") else 0,
             1 if request.form.get("gift_enabled") else 0, db.now(), sid))
        g.conn.commit()
        flash("예약을 저장했습니다.")
        return redirect(url_for("quizbot"))

    def back_to(default: str) -> str:
        """확인 대기 글을 처리한 뒤 돌아갈 화면 (첫 화면에서 눌렀으면 첫 화면으로)."""
        return "home" if form("back") == "home" else default

    @app.post("/quizbot/quizzes/<int:qid>/approve")
    def quizbot_approve(qid):
        q = one("SELECT * FROM quizzes WHERE id = ?", qid)
        answer = form("answer") or (q["answer"] or "")
        if q["entry_status"] not in ("pending", "failed"):
            flash("이미 보냈거나 결과 불명인 문제는 다시 보내지 않습니다.", "error")
            return redirect(url_for(back_to("quizzes")) + "#pending")
        if not answer:
            flash("정답을 적어 주세요.", "error")
            return redirect(url_for(back_to("quizzes")) + "#pending")
        send_text = form("send_text") or None
        route = form("route") if form("route") in qconfig.ROUTES else None
        kind = "witty" if send_text and q["witty_answer"] and q["witty_answer"] in send_text else "correct"
        g.conn.execute(
            "UPDATE quizzes SET answer = ?, send_text = ?, route = ?, answer_kind = ?, approved = 1, kind = 'new', "
            "entry_status = 'pending', decision = '승인됨 — 실행기가 곧 보냄', updated_at = ? WHERE id = ?",
            (answer, send_text, route, kind, db.now(), qid))
        g.conn.commit()
        db.log(g.conn, "quizbot", f"퀴즈 #{qid} 보내기 승인 (사용자): {answer}")
        if qconfig.runner_alive(g.conn):
            flash("승인했습니다. 실행기가 고릴라로 보냅니다.")
        else:
            flash("승인했습니다. 청취가 꺼져 있어 첫 화면에서 '청취 시작'을 눌러야 보내집니다.", "warn")
        return redirect(url_for(back_to("quizzes")) + "#pending")

    @app.post("/quizbot/quizzes/<int:qid>/skip")
    def quizbot_skip(qid):
        one("SELECT id FROM quizzes WHERE id = ?", qid)
        g.conn.execute("UPDATE quizzes SET entry_status = 'skipped', approved = 0, updated_at = ? "
                       "WHERE id = ? AND entry_status IN ('pending', 'failed')", (db.now(), qid))
        g.conn.commit()
        flash(f"퀴즈 #{qid}은(는) 보내지 않습니다.")
        return redirect(url_for(back_to("quizzes")) + "#pending")

    @app.post("/quizbot/stories/<int:pid>/approve")
    def story_approve(pid):
        from .quizbot import story

        post = one("SELECT * FROM story_posts WHERE id = ?", pid)
        if post["status"] not in ("pending", "failed"):
            flash("이미 보냈거나 결과 불명인 글은 다시 보내지 않습니다.", "error")
            return redirect(url_for(back_to("stories")))
        message = (request.form.get("message") or "").strip()
        exp = g.conn.execute("SELECT * FROM experiences WHERE id = ?", (post["experience_id"],)).fetchone() \
            if post["experience_id"] else None
        if exp is None:
            source = "manual"          # 맞는 경험이 없어 사용자가 직접 쓴 글
        elif message == (exp["gorilla_line"] or "").strip():
            source = "user_line"
        else:
            source = "ai" if message == post["message"] else "edited"
        warnings = story.message_checks(g.conn, message, exp, source)
        if exp is not None:
            here = story.post_broadcaster(g.conn, post)
            for other in g.conn.execute("SELECT * FROM story_posts WHERE experience_id = ? AND id != ? "
                                        "AND status IN ('entered','posted','unknown')", (exp["id"], pid)):
                if story.post_broadcaster(g.conn, other) == here:
                    warnings.append({"level": "block", "message": f"이 경험은 {here}에 이미 보냈습니다 (사연 #{other['id']}, "
                                                                  f"{other['program']}). 같은 방송국에는 다시 보내지 않습니다."})
                    break
        g.conn.execute("UPDATE story_posts SET message = ?, source = ?, warnings = ?, updated_at = ? WHERE id = ?",
                       (message, source, json.dumps(warnings, ensure_ascii=False), db.now(), pid))
        if story.has_block(warnings):
            g.conn.commit()
            for w in warnings:
                if w["level"] == "block":
                    flash(w["message"], "error")
            flash("위 문제를 고친 뒤 다시 보내기를 누르세요.", "error")
            return redirect(url_for(back_to("stories")))
        route = form("route") if form("route") in qconfig.ROUTES else None
        g.conn.execute("UPDATE story_posts SET approved = 1, status = 'pending', route = ?, decision = ?, updated_at = ? "
                       "WHERE id = ?", (route, "승인됨 — 실행기가 곧 보냄", db.now(), pid))
        g.conn.commit()
        db.log(g.conn, "quizbot", f"사연 #{pid} 보내기 승인 (사용자)")
        flash("승인했습니다. 실행기가 고릴라로 보냅니다." if qconfig.runner_alive(g.conn)
              else "승인했습니다. 청취가 꺼져 있어 첫 화면에서 '청취 시작'을 눌러야 보내집니다.",
              "message" if qconfig.runner_alive(g.conn) else "warn")
        return redirect(url_for(back_to("stories")))

    @app.post("/quizbot/stories/<int:pid>/skip")
    def story_skip(pid):
        one("SELECT id FROM story_posts WHERE id = ?", pid)
        g.conn.execute("UPDATE story_posts SET status = 'skipped', approved = 0, updated_at = ? "
                       "WHERE id = ? AND status IN ('pending', 'failed')", (db.now(), pid))
        g.conn.commit()
        flash(f"사연 #{pid}은(는) 보내지 않습니다.")
        return redirect(url_for(back_to("stories")))

    @app.get("/quizbot/pending.json")
    def quizbot_pending():
        return {**pending_counts(g.conn), "state": db.get_setting(g.conn, "quizbot.state")}

    @app.get("/stories")
    def stories():
        c = g.conn
        pending = c.execute(
            """SELECT sp.*, e.label AS exp_label, e.story AS exp_story, c.title AS board_name FROM story_posts sp
               LEFT JOIN experiences e ON e.id = sp.experience_id
               LEFT JOIN drafts d ON d.id = sp.draft_id LEFT JOIN corners c ON c.id = d.corner_id
               WHERE sp.status IN ('pending', 'failed') ORDER BY sp.id DESC LIMIT 30""").fetchall()
        recent = c.execute(
            """SELECT sp.*, e.label AS exp_label FROM story_posts sp LEFT JOIN experiences e ON e.id = sp.experience_id
               WHERE sp.status NOT IN ('pending', 'failed') ORDER BY sp.id DESC LIMIT 100""").fetchall()
        counts = {
            "confirmed": c.execute("SELECT COUNT(*) FROM experiences WHERE user_confirmed = 1").fetchone()[0],
            "experiences": c.execute("SELECT COUNT(*) FROM experiences").fetchone()[0],
            "drafts": c.execute("SELECT COUNT(*) FROM drafts WHERE status = 'draft'").fetchone()[0],
            "unresolved": c.execute(
                "SELECT COUNT(*) FROM submissions WHERE post_status IN ('filled','unknown')").fetchone()[0],
        }
        return render_template("stories.html", pending=[(p, json.loads(p["warnings"] or "[]")) for p in pending],
                               recent=recent, counts=counts)

    @app.get("/stories/sent")
    def stories_sent():
        """보낸 사연 목록: 앱 채팅·문자·게시판으로 보낸 글을 방송국별로, 경험마다 보낸 방송국을 한눈에."""
        from .quizbot import story

        who = request.args.get("b", "")
        rows = []
        for sp in g.conn.execute(
                """SELECT sp.*, e.label AS exp_label FROM story_posts sp LEFT JOIN experiences e ON e.id = sp.experience_id
                   WHERE sp.status IN ('entered', 'posted', 'unknown')"""):
            rows.append({"at": sp["sent_at"] or sp["updated_at"], "broadcaster": story.post_broadcaster(g.conn, sp),
                         "where": " · ".join(x for x in (sp["channel"], sp["program"]) if x),
                         "how": qconfig.app_label(sp["sent_via"]) if sp["sent_via"] in qconfig.CHAT_APPS
                         else ("문자" if sp["sent_via"] == "sms" else "앱 채팅"),
                         "status": quiz.entry_label(sp["status"], sp["sent_via"]), "ok": sp["status"] == "posted",
                         "topic": sp["topic"], "text": sp["message"], "exp_id": sp["experience_id"],
                         "exp_label": sp["exp_label"], "ev_t": "story_posts", "ev_id": sp["id"]})
        for sub in g.conn.execute(
                """SELECT s.*, c.program, c.title AS corner_title, e.label AS exp_label FROM submissions s
                   JOIN corners c ON c.id = s.corner_id LEFT JOIN experiences e ON e.id = s.experience_id
                   WHERE s.post_status IN ('filled', 'posted', 'unknown')"""):
            rows.append({"at": sub["updated_at"], "broadcaster": "SBS", "where": sub["program"],
                         "how": f"게시판 · {sub['corner_title']}", "status": STATUS_LABELS.get(sub["post_status"],
                                                                                        sub["post_status"]),
                         "ok": sub["post_status"] == "posted", "topic": sub["title"] or "", "text": sub["body"] or "",
                         "exp_id": sub["experience_id"], "exp_label": sub["exp_label"],
                         "ev_t": "submissions", "ev_id": sub["id"]})
        shot_counts = {t: evidence.counts(g.conn, t) for t in ("story_posts", "submissions")}
        for r in rows:
            r["shots"] = shot_counts[r["ev_t"]].get(r["ev_id"], 0)
        rows.sort(key=lambda r: r["at"] or "", reverse=True)
        order = list(qconfig.BROADCASTERS)
        counts = [(b, sum(1 for r in rows if r["broadcaster"] == b))
                  for b in sorted({r["broadcaster"] for r in rows}, key=lambda b: (order.index(b) if b in order else 9, b))]
        shown = [r for r in rows if not who or r["broadcaster"] == who]
        used = story.sent_broadcasters(g.conn)
        others = sorted({b for where in used.values() for b in where} - set(qconfig.BROADCASTERS))
        columns = list(qconfig.BROADCASTERS) + others
        matrix = [{"id": e["id"], "label": e["label"] or (e["story"] or "")[:30], "sent": used.get(e["id"], set())}
                  for e in g.conn.execute("SELECT * FROM experiences WHERE user_confirmed = 1 ORDER BY id")]
        return render_template("stories_sent.html", rows=shown, total=len(rows), counts=counts, who=who,
                               columns=columns, matrix=matrix)

    @app.get("/inspect-image/<name>")
    def inspect_image(name):
        if not INSPECT_IMAGE.fullmatch(name):
            abort(404)
        return send_from_directory(db.data_dir() / "inspect", name, max_age=0)

    @app.get("/gifts")
    def gifts():
        channel, program = request.args.get("channel", ""), request.args.get("program", "")
        where, params = [], []
        if channel:
            where.append("channel = ?")
            params.append(channel)
        if program:
            where.append("program = ?")
            params.append(program)
        rows = g.conn.execute(
            "SELECT * FROM gift_events" + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY channel, program, broadcast_date DESC, id DESC LIMIT 300", params).fetchall()
        groups: dict[tuple[str, str], list] = {}
        for r in rows:
            groups.setdefault((r["channel"], r["program"]), []).append(r)
        channels = [r[0] for r in g.conn.execute("SELECT DISTINCT channel FROM gift_events ORDER BY channel")]
        programs_ = [r[0] for r in g.conn.execute("SELECT DISTINCT program FROM gift_events ORDER BY program")]
        return render_template("gifts.html", groups=groups, channels=channels, programs=programs_,
                               channel=channel, program=program, total=len(rows))

    @app.route("/import", methods=["GET", "POST"])
    def import_page():
        from . import importer

        if request.method == "POST":
            upload = request.files.get("file")
            text = upload.read().decode("utf-8", errors="replace") if upload and upload.filename else \
                request.form.get("text", "")
            if not text.strip():
                flash("가져올 파일을 고르거나 내용을 붙여 넣으세요.", "error")
                return redirect(url_for("import_page"))
            from . import people as people_mod

            kind = people_mod.detect(text)
            try:
                if kind == "people":
                    added, updated = people_mod.import_people(g.conn, people_mod.parse_character_map(text))
                    db.log(g.conn, "people", f"인물 관계도 가져오기: 새 인물 {added}명, 고침 {updated}명")
                    flash(f"인물 관계도를 가져왔습니다: 새 인물 {added}명, 고침 {updated}명. 사연에는 실명 대신 '호칭'을 씁니다 — "
                          "인물 화면에서 호칭을 확인하세요.")
                    return redirect(url_for("people_page"))
                if kind == "library":
                    rows = people_mod.parse_story_log(text)
                    added, updated = people_mod.import_library(g.conn, rows)
                    fiction = sum(1 for r in rows if r["kind"] == "fiction")
                    db.log(g.conn, "people", f"사연 보관함 가져오기: 새 사연 {added}편, 고침 {updated}편")
                    flash(f"사연 보관함에 {added + updated}편을 넣었습니다 (새 {added} · 고침 {updated}).")
                    if fiction:
                        flash(f"그중 {fiction}편은 원본 사연을 각색한 가상 사연이라 읽기용으로만 둡니다. "
                              "실제로 있었던 일이 아니므로 이 프로그램은 보내지 않습니다.", "warn")
                    return redirect(url_for("library"))
            except people_mod.PeopleImportError as e:
                flash(str(e), "error")
                return redirect(url_for("import_page"))
            try:
                report = importer.import_data(g.conn, importer.parse(text),
                                              overwrite_profile=bool(request.form.get("overwrite")))
            except importer.ImportError_ as e:
                flash(str(e), "error")
                return redirect(url_for("import_page"))
            flash("가져왔습니다: " + report.summary())
            for note in report.notes:
                flash(note, "warn")
            if report.added:
                flash("가져온 경험은 '실제로 있었던 일' 확인이 꺼져 있습니다. 실제 경험 화면에서 하나씩 읽고 체크해 주세요.", "warn")
            return redirect(url_for("experiences"))
        return render_template("import.html",
                               template=json.dumps(importer.TEMPLATE, ensure_ascii=False, indent=2))

    # ── 인물 관계도 · 사연 보관함 ─────────────────────────────────
    def person_rows():
        return g.conn.execute(
            """SELECT p.*,
                      (SELECT COUNT(*) FROM experiences e WHERE e.about_person_id = p.id) AS exp_count,
                      (SELECT COUNT(*) FROM story_library l WHERE l.person_id = p.id) AS lib_count
               FROM people p ORDER BY p.sort, p.id""").fetchall()

    @app.get("/people")
    def people_page():
        from . import people as people_mod

        rows = person_rows()
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(r["grp"] or "기타", []).append({**dict(r), "events_n": len(json.loads(r["events"] or "[]"))})
        own = g.conn.execute("SELECT COUNT(*) FROM experiences WHERE about_person_id IS NULL").fetchone()[0]
        return render_template("people.html", d=people_mod.diagram(rows) if rows else None, groups=groups,
                               sides=people_mod.SIDES, total=len(rows), own_experiences=own,
                               library_n=g.conn.execute("SELECT COUNT(*) FROM story_library").fetchone()[0],
                               events_left=people_mod.events_left(g.conn) if rows else 0)

    @app.post("/people/events-to-experiences")
    def people_events_to_experiences():
        from . import people as people_mod

        pid = request.form.get("person", type=int)
        if pid:
            one("SELECT id FROM people WHERE id = ?", pid)
        added, skipped = people_mod.events_to_experiences(g.conn, pid)
        earlier = people_mod.confirm_moved_events(g.conn)   # 예전에 '확인 전'으로 옮긴 사건도 확인
        if added or earlier:
            db.log(g.conn, "record", f"관계도 실제 사건 {added + earlier}건 → 실제 경험 (확인됨)")
        msg = (f"실제 사건 {added}건을 실제 경험으로 옮겼습니다" if added else "새로 옮길 실제 사건이 없습니다") \
            + (f" (이미 있던 {skipped}건은 건너뜀)" if skipped else "") + "."
        flash(msg + " " + start_enrich(g.conn), "message" if added or earlier else "warn")
        return redirect(url_for("experiences"))

    @app.post("/experiences/enrich")
    def experiences_enrich():
        flash(start_enrich(g.conn) or "풀어 쓸 사건이 없습니다.")
        return redirect(url_for("experiences"))

    @app.route("/people/<int:pid>", methods=["GET", "POST"])
    def person_page(pid):
        from . import people as people_mod

        p = one("SELECT * FROM people WHERE id = ?", pid)
        if request.method == "POST":
            alias = form("alias") or p["alias"]
            closeness = form("closeness") if form("closeness") in ("", "가까움", "보통", "서먹") else p["closeness"]
            g.conn.execute("UPDATE people SET alias = ?, closeness = ?, note = ?, updated_at = ? WHERE id = ?",
                           (alias[:20], closeness, form("note") or None, db.now(), pid))
            g.conn.commit()
            flash("저장했습니다.")
            return redirect(url_for("person_page", pid=pid))
        exps = g.conn.execute(
            "SELECT * FROM experiences WHERE " + ("about_person_id IS NULL" if p["side"] == "self" else "about_person_id = ?")
            + " ORDER BY id DESC", () if p["side"] == "self" else (pid,)).fetchall()
        library_rows = g.conn.execute("SELECT id, code, title, event_date, kind FROM story_library WHERE person_id = ? "
                                      "ORDER BY code", (pid,)).fetchall()
        events = json.loads(p["events"] or "[]")
        # 사건에서 만든 경험: 날짜 + 미리 채운 짧은 이름(사건 앞 30자)으로 알아본다 (본문은 고쳐도 됨)
        made = {(e["when_text"] or "", (e["label"] or "")[:30]) for e in exps}
        return render_template("person.html", p=p, details=json.loads(p["details"] or "[]"), events=events, made=made,
                               exps=exps, library=library_rows, sides=people_mod.SIDES,
                               events_left=people_mod.events_left(g.conn, pid),
                               intro=people_mod.intro_for(p["alias"] if p["side"] != "self" else None))

    @app.get("/library")
    def library():
        person = request.args.get("person", type=int)
        kind = request.args.get("kind", "")
        q = (request.args.get("q") or "").strip()
        where, params = [], []
        if person:
            where.append("l.person_id = ?")
            params.append(person)
        if kind in ("fiction", "real"):
            where.append("l.kind = ?")
            params.append(kind)
        if q:
            where.append("(l.title LIKE ? OR l.body LIKE ? OR l.summary LIKE ?)")
            params += [f"%{q}%"] * 3
        rows = g.conn.execute(
            "SELECT l.*, p.name AS person_name, p.alias AS person_alias, "
            "(SELECT MAX(user_confirmed) FROM experiences e WHERE e.from_library_id = l.id) AS moved "
            "FROM story_library l "
            "LEFT JOIN people p ON p.id = l.person_id" + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY l.code, l.id", params).fetchall()
        not_moved = g.conn.execute("SELECT COUNT(*) FROM story_library l WHERE NOT EXISTS "
                                   "(SELECT 1 FROM experiences e WHERE e.from_library_id = l.id)").fetchone()[0]
        return render_template("library.html", rows=rows, people=people_options(), person=person, kind=kind, q=q,
                               not_moved=not_moved,
                               counts={k: g.conn.execute("SELECT COUNT(*) FROM story_library WHERE kind = ?",
                                                         (k,)).fetchone()[0] for k in ("fiction", "real")})

    @app.get("/library/<int:lid>")
    def library_item(lid):
        item = one("SELECT l.*, p.name AS person_name, p.alias AS person_alias FROM story_library l "
                   "LEFT JOIN people p ON p.id = l.person_id WHERE l.id = ?", lid)
        moved = g.conn.execute("SELECT id, user_confirmed FROM experiences WHERE from_library_id = ? ORDER BY id LIMIT 1",
                               (lid,)).fetchone()
        return render_template("library_item.html", s=item, moved=moved)

    @app.post("/library/<int:lid>/to-experience")
    def library_to_experience(lid):
        """보관함 사연 하나를 '확인 전' 실제 경험으로 옮긴다. 사용자가 실제와 다른 부분을 고치고 확인해야 쓰인다."""
        from . import people as people_mod

        item = one("SELECT * FROM story_library WHERE id = ?", lid)
        eid, created = people_mod.library_to_experience(g.conn, item)
        if not created:
            return redirect(url_for("experience_edit", eid=eid))
        g.conn.commit()
        db.log(g.conn, "record", f"사연 보관함 {item['code'] or lid} → 경험 #{eid} (확인 전)")
        flash("경험으로 옮겼습니다. 실제로 있었던 일과 다른 부분을 고친 뒤, 맞을 때만 '실제로 있었던 일'을 체크하고 저장하세요. "
              "체크하기 전에는 보내지 않습니다.", "warn")
        return redirect(url_for("experience_edit", eid=eid))

    @app.post("/library/to-experiences")
    def library_to_experiences():
        """보관함 사연을 모두 '확인 전' 경험으로 가져온다. 사용자가 하나씩 열어 고치고 확인한 것만 보낸다."""
        from . import people as people_mod

        added, skipped = people_mod.library_to_experiences(g.conn)
        if added:
            db.log(g.conn, "record", f"사연 보관함 {added}편 → 확인 전 경험 (일괄 가져오기)")
        flash((f"보관함 사연 {added}편을 '확인 전' 경험으로 가져왔습니다" if added else "새로 가져올 보관함 사연이 없습니다")
              + (f" (이미 옮긴 {skipped}편은 건너뜀)" if skipped else "")
              + ". 하나씩 열어 실제와 다른 부분을 고치고 '실제로 있었던 일'을 체크한 것만 보냅니다.", "warn")
        return redirect(url_for("experiences", only="library"))

    @app.post("/quizbot/tool")
    def quizbot_tool():
        tool = form("tool")
        app_ = form("app", "gorilla")
        if app_ not in qconfig.CHAT_APPS:
            abort(400)
        if tool in ("sms-check", "sms-test"):
            if tool == "sms-test" and db.is_stopped(g.conn):
                flash("일괄 중지가 켜져 있습니다.", "error")
                return redirect(url_for("sms_page"))
            launch_quizbot([tool])
            flash("휴대폰 문자 연결을 점검합니다. 10초쯤 뒤 이 화면이 새로 고쳐집니다." if tool == "sms-check" else
                  "시험 문자를 실제로 보냅니다 (요금 발생). 20초쯤 뒤 휴대폰 화면 사진이 아래에 나옵니다.")
            return redirect(url_for("sms_page", wait=10 if tool == "sms-check" else 20))
        args = {"check": ["check"], "auto-setup": ["auto-setup"], "inspect": ["inspect-gorilla"],
                "select-window": ["select", "window"], "select-input": ["select", "input"],
                "select-send": ["select", "send"], "select-chat": ["select", "chat"], "chat-test": ["chat-test"],
                "calibrate-input": ["calibrate", "input"],
                "calibrate-send": ["calibrate", "send"], "send-test": ["send-test"]}.get(tool)
        if args is None:
            abort(400)
        if tool != "check" and db.is_stopped(g.conn):
            flash("일괄 중지가 켜져 있습니다.", "error")
            return redirect(url_for("gorilla", app=app_))
        if tool == "send-test" and "test_message" in request.form:
            # 전송 테스트 버튼 옆 칸에 적은 글을 보낸다 (다음에도 그 글이 칸에 남음)
            text = " ".join(form("test_message").split())
            if not text:
                flash("보낼 시험 글을 적어 주세요.", "error")
                return redirect(url_for("gorilla", app=app_))
            if len(text) > TEST_MESSAGE_MAX:
                flash(f"시험 글이 너무 깁니다 ({len(text)}자). {TEST_MESSAGE_MAX}자 안으로 적어 주세요.", "error")
                return redirect(url_for("gorilla", app=app_))
            db.set_setting(g.conn, f"{app_}.test_message", text)
        if tool != "check":
            args = args + ["--app", app_]
        log_name = launch_quizbot(args)
        messages = {
            "check": "환경을 점검합니다. 10~30초 뒤 이 화면을 새로 고치세요.",
            "auto-setup": "열린 창 중에서 고릴라 채팅창을 찾습니다 (읽기만 함). 10~30초 뒤 이 화면을 새로 고치세요.",
            "inspect": "고릴라 창의 화면 요소를 읽습니다 (아무것도 누르지 않음).",
            "select-window": "화면이 어두워지면 고릴라 창 전체를 마우스로 끌어 네모로 감싸세요. 손을 떼면 저장됩니다.",
            "select-input": "화면이 어두워지면 고릴라의 '공감로그 글쓰기' 칸을 마우스로 끌어 네모로 그리세요.",
            "select-send": "화면이 어두워지면 고릴라의 파란 '전송' 버튼을 마우스로 끌어 네모로 그리세요.",
            "select-chat": "화면이 어두워지면 고릴라의 채팅 목록(다른 청취자 글이 올라오는 곳)을 크게 네모로 감싸세요.",
            "chat-test": "고릴라 채팅 목록 영역을 지금 찍어 봅니다 (읽기만 함).",
            "calibrate-input": "지금 7초 안에 마우스를 고릴라의 '공감로그 글쓰기' 칸 위에 올려 두고 움직이지 마세요.",
            "calibrate-send": "지금 7초 안에 마우스를 고릴라의 파란 '전송' 버튼 위에 올려 두고 움직이지 마세요.",
            "send-test": f"고릴라 채팅에 '{qconfig.get(g.conn, app_ + '.test_message')}'를 입력하고 전송을 누릅니다. "
                         "끝나면 누르기 직전·보낸 뒤 사진이 아래에 나옵니다.",
        }
        flash(f"{qconfig.localize(app_, messages[tool])} (기록: {log_name})")
        wait = 25 if tool.startswith(("select", "calibrate")) else 12
        if tool == "check":
            return redirect(url_for("quizbot", wait=wait))
        return redirect(url_for("gorilla", app=app_, wait=wait))

    # ── 보낸 증거 사진 ──────────────────────────────────────────
    @app.get("/evidence")
    def evidence_page():
        """퀴즈 정답·사연을 보낼 때 찍어 둔 화면. ?t=quizzes|story_posts|submissions &id=번호 &day=YYYY-MM-DD"""
        table = request.args.get("t", "")
        table = table if table in evidence.ITEM_LABELS else ""
        item_id = request.args.get("id", type=int) if table else None
        day = request.args.get("day", "")
        day = day if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else ""
        rows = evidence.listing(g.conn, table or None, item_id, day or None, limit=200 if item_id is None else 50)
        items = []
        for r in rows:
            if r["item_table"] == "submissions":
                status, link = STATUS_LABELS.get(r["status"], r["status"]), url_for("submissions")
            elif r["item_table"] == "tests":
                status = quiz.entry_label(r["status"], r["via"])
                link = url_for("gorilla", app=r["via"] if r["via"] in qconfig.CHAT_APPS else "gorilla")
            else:
                status = quiz.entry_label(r["status"], r["via"])
                link = url_for("quizzes") + "#auto" if r["item_table"] == "quizzes" else url_for("stories_sent")
            items.append({**dict(r), "kind": evidence.ITEM_LABELS.get(r["item_table"], r["item_table"]),
                          "via_label": "게시판" if r["via"] == "board" else
                          ("문자" if r["via"] == "sms" else qconfig.app_label(r["via"]) if r["via"] else ""),
                          "status_label": status, "ok": r["status"] == "posted", "link": link})
        n, size = evidence.usage(g.conn)
        return render_template("evidence.html", items=items, table=table, item_id=item_id, day=day,
                               labels=evidence.ITEM_LABELS, total=n, size_mb=size / 1_000_000,
                               folder=str(evidence.root()))

    @app.get("/evidence/<int:eid>/image")
    def evidence_image(eid):
        path = evidence.file_of(g.conn, eid)
        if path is None:
            abort(404)
        return send_from_directory(path.parent, path.name, max_age=0)

    @app.route("/gorilla", methods=["GET", "POST"])
    def gorilla():
        """채팅 앱(고릴라·mini·콩) 창 맞추기와 인식 설정. ?app= 으로 앱을 고른다."""
        app_ = request.args.get("app", "gorilla")
        if app_ not in qconfig.CHAT_APPS:
            abort(404)
        keys = [k for k in qconfig.DEFAULTS if k.startswith(f"{app_}.") and not k.endswith(("_x", "_y"))] + \
               [k for k in qconfig.DEFAULTS if k.startswith("quizbot.") and k != "quizbot.autostart"]
        if request.method == "POST":
            for k in keys:
                if k in request.form:
                    db.set_setting(g.conn, k, request.form[k].strip())
            flash(f"{qconfig.app_label(app_)}·인식 설정을 저장했습니다. 청취 중이면 '설정 바꾸기'나 다시 시작하면 적용됩니다.")
            return redirect(url_for("gorilla", app=app_))

        def latest(pattern):
            files = sorted((db.data_dir() / "inspect").glob(pattern), reverse=True)[:1]
            if not files:
                return None
            try:
                data = json.loads(files[0].read_text(encoding="utf-8"))
            except ValueError:
                return None
            data["file"] = files[0].name
            return data

        report = latest(f"{app_}_2*.json")       # 창 점검
        auto = latest(f"{app_}_auto_*.json")     # 자동 찾기
        if report and auto and report["file"][len(app_) + 1:] < auto["file"][len(app_) + 6:]:
            report = None  # 자동 찾기 이전의 점검 결과는 다른 창을 봤을 수 있다
        from .quizbot.gorilla import is_excluded

        if report and is_excluded(report.get("title") or "", report.get("process") or ""):
            report = None  # 브라우저(이 도우미 화면)를 점검한 결과는 채팅 앱 설정에 쓸모가 없어 보여 주지 않는다
        coords = {k: qconfig.get(g.conn, f"{app_}.{k}")
                  for k in ("input_x", "input_y", "send_x", "send_y", "input_point", "send_point")}
        from .quizbot.gorilla import parse_rect

        rects = {k: parse_rect(qconfig.get(g.conn, f"{app_}.{k}_rect")) for k in ("input", "send")}
        region = parse_rect(qconfig.get(g.conn, f"{app_}.screen_region"))
        log_dir = db.data_dir() / "logs"
        tool_logs = sorted(log_dir.glob("quizbot_*.log"), key=lambda f: f.stat().st_mtime, reverse=True) \
            if log_dir.exists() else []
        last_tool = None
        for f in tool_logs[:5]:
            text = f.read_text(encoding="utf-8", errors="replace").strip()
            if "퀴즈 자동 참여를 시작합니다" in text or "받아쓰기" in text or "문자" in text[:200]:
                continue  # 실행기·받아쓰기·문자 기록은 제외하고 화면 설정 도구 결과만
            last_tool = (f.name, text[-1500:] or "(아직 진행 중이거나 출력 없음)")
            break
        label = qconfig.app_label(app_)
        shots = image_list([(f"{app_}_chat", "④ 채팅창 읽기 — 키워드가 들리면 이 영역을 녹취와 함께 분석 (글자가 읽혀야 함)"),
                            (f"{app_}_select_chat", "④ 채팅 목록으로 지정한 영역"),
                            (f"{app_}_test", "③ 전송 테스트 — 누르기 직전, 또는 보내지 못했을 때 (빨강: 입력칸 클릭, 초록: 전송 버튼 클릭 위치)"),
                            (f"{app_}_test_sent", "③ 전송 테스트 — 보낸 뒤 (채팅에 글이 올라왔는지 확인)"),
                            (f"{app_}_select_input", "① 입력칸으로 지정한 영역"),
                            (f"{app_}_select_send", "② 전송 버튼으로 지정한 영역"),
                            (f"{app_}_select_window", f"{label} 창으로 지정한 영역")])
        return render_template("gorilla.html", app=app_, label=label, apps=qconfig.CHAT_APPS,
                               keys=keys, values={k: qconfig.get(g.conn, k) for k in keys},
                               labels=qconfig.LABELS, report=report, auto=auto, coords=coords, last_tool=last_tool,
                               rects=rects, region=region, shots=shots,
                               waiting=request.args.get("wait", type=int))

    @app.route("/sms", methods=["GET", "POST"])
    def sms_page():
        """휴대폰 문자 설정 (안드로이드 + USB)."""
        keys = ["channels", "sms.signature", "sms.quiz_template", "sms.verify_number", "sms.adb_path"]
        if request.method == "POST":
            for k in keys:
                if k in request.form:
                    db.set_setting(g.conn, k, request.form[k].replace("\r\n", "\n").strip())
            flash("문자 설정을 저장했습니다.")
            return redirect(url_for("sms_page"))
        from .quizbot.sms import find_adb

        return render_template(
            "sms.html", keys=keys, values={k: qconfig.get(g.conn, k) for k in keys}, labels=qconfig.LABELS,
            check=_json(db.get_setting(g.conn, "sms.check")), adb=find_adb(qconfig.get(g.conn, "sms.adb_path")),
            today=sms_today(g.conn), channel=qconfig.get(g.conn, "live.channel"),
            number=qconfig.sms_number(g.conn, qconfig.get(g.conn, "live.channel")),
            nickname=db.get_profile(g.conn).get("nickname"), route=qconfig.route(g.conn),
            events=g.conn.execute("SELECT * FROM events WHERE kind = 'sms' OR message LIKE '%문자(%' "
                                  "ORDER BY id DESC LIMIT 10").fetchall(),
            shots=image_list([("sms_test", "시험 문자 — 전송 버튼 누르기 직전 휴대폰 화면"),
                              ("sms_test_sent", "시험 문자 — 보낸 뒤 휴대폰 화면")]),
            waiting=request.args.get("wait", type=int))

    def image_list(items) -> list[dict]:
        shots = []
        for name, label in items:
            f = db.data_dir() / "inspect" / f"{name}.png"
            if f.exists():
                stamp = datetime.fromtimestamp(f.stat().st_mtime)
                shots.append({"file": f.name, "label": label, "at": f"{stamp:%m/%d %H:%M:%S}",
                              "v": int(stamp.timestamp())})
        return shots

    # ── 로컬 모의 글쓰기 화면 ─────────────────────────────────────
    @app.route("/mock/write", methods=["GET", "POST"])
    def mock_write():
        if request.method == "POST":
            cur = g.conn.execute(
                "INSERT INTO mock_posts (cornerid, title, content, song, created_at) VALUES (?, ?, ?, ?, ?)",
                (request.args.get("cornerid"), request.form.get("title", ""), request.form.get("content", ""),
                 request.form.get("song", ""), db.now()))
            g.conn.commit()
            return redirect(url_for("mock_view", mid=cur.lastrowid))
        return render_template("mock_write.html", cornerid=request.args.get("cornerid", ""))

    @app.get("/mock/view/<int:mid>")
    def mock_view(mid):
        return render_template("mock_view.html", post=one("SELECT * FROM mock_posts WHERE id = ?", mid))

    return app


def session_key() -> str:
    """세션 서명 키. 데이터 폴더에 한 번 만들어 계속 쓴다 (프로그램 폴더·GitHub 에는 남지 않음).
    예전에는 켤 때마다 새로 만들어, 다시 켠 뒤 열려 있던 화면에서 누르면 'Bad Request' 가 났다."""
    path = db.data_dir() / "session.key"
    try:
        key = path.read_text(encoding="ascii").strip()
        if len(key) >= 32:
            return key
    except (OSError, ValueError):
        pass
    key = secrets.token_hex(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key, encoding="ascii")
    except OSError:
        pass
    return key


def launch_module(module: str, args: list[str], prefix: str) -> str:
    """도구를 별도 프로세스로 실행한다. 출력은 data/logs 에 남는다."""
    log_dir = db.data_dir() / "logs"
    log_dir.mkdir(exist_ok=True)
    name = f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}.log"
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    with open(log_dir / name, "w", encoding="utf-8") as out:
        subprocess.Popen([sys.executable, "-m", module, *args],
                         stdout=out, stderr=subprocess.STDOUT, cwd=str(PROJECT_ROOT), env=env)
    return name


def launch_autofill(args: list[str]) -> str:
    return launch_module("radio_helper.autofill", args, "autofill")


def launch_programs_refresh() -> str:
    return launch_module("radio_helper.programs", ["refresh"], "programs")


def start_enrich(conn) -> str:
    """관계도 사건 풀어 쓰기를 뒤에서 시작하고 안내 글을 돌려준다 (풀어 쓸 것이 없으면 빈 글)."""
    from .quizbot import enrich
    from .quizbot.answerer import get_api_key

    left = len(enrich.pending(conn))
    if not left:
        return ""
    if not get_api_key():
        return f"사연처럼 풀어 쓰기({left}건)는 Claude API 키를 설정 화면에 저장하면 시작할 수 있습니다. 그전에도 한 줄 그대로 사연 재료로 씁니다."
    v = enrich.view(conn)
    if v["state"] == "running":
        return f"사연처럼 풀어 쓰는 중입니다 ({v['progress']}/{v.get('total', left)})."
    enrich.save_status(conn, running=True, done=0, kept=0, total=left, started=db.now())
    db.set_setting(conn, enrich.LOG_KEY, launch_quizbot(["enrich"]))
    return f"{left}건을 사연처럼 풀어 쓰기 시작했습니다 (Claude API, 8건씩 · 첫 숫자가 바뀌는 데 1~2분). 끝난 사건부터 바로 사연에 씁니다."


def log_tail(name: str, chars: int = 2500) -> str:
    """data/logs 의 실행 기록 끝부분 (꺼졌을 때 원인 확인용)."""
    if not re.fullmatch(r"quizbot_[0-9_a-f]+\.log", name or ""):
        return ""
    path = db.data_dir() / "logs" / name
    if not path.exists():
        return ""
    return f"[{name}]\n" + path.read_text(encoding="utf-8", errors="replace")[-chars:]


def launch_quizbot(args: list[str]) -> str:
    return launch_module("radio_helper.quizbot", args, "quizbot")


def start_runner(conn) -> str:
    """듣기 실행기를 띄우고, 화면이 상태·기록을 찾을 수 있게 남긴다."""
    db.set_setting(conn, "quizbot.state", "시작 중")
    db.set_setting(conn, "quizbot.launched_at", db.now())
    log_name = launch_quizbot(["run"])
    db.set_setting(conn, "quizbot.run_log", log_name)
    return log_name


def runner_log_tail(conn, chars: int = 2500) -> str:
    """듣기 실행기 기록 파일의 끝부분 (꺼졌을 때 원인 확인용)."""
    name = db.get_setting(conn, "quizbot.run_log") or ""
    if not re.fullmatch(r"quizbot_[0-9_a-f]+\.log", name):
        return ""
    path = db.data_dir() / "logs" / name
    if not path.exists():
        return ""
    return f"[{name}]\n" + path.read_text(encoding="utf-8", errors="replace")[-chars:]


def sms_today(conn) -> dict:
    """오늘 문자로 보낸 수와 대략의 요금."""
    from .quizbot.sms import COST_PER_SMS

    day = datetime.now().strftime("%Y-%m-%d")
    n = sum(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE sent_via = 'sms' AND sent_at >= ?", (day,)).fetchone()[0]
            for t in ("quizzes", "story_posts"))
    return {"count": n, "won": n * COST_PER_SMS}


def _json(text: str | None):
    try:
        return json.loads(text or "null")
    except ValueError:
        return None


def pending_counts(conn) -> dict:
    """확인을 기다리는 자동 감지 퀴즈·사연 수 (메뉴 배지·알림용)."""
    return {
        "quizzes": conn.execute("SELECT COUNT(*) FROM quizzes WHERE source = 'auto' AND entry_status = 'pending' "
                                "AND approved = 0").fetchone()[0],
        "stories": conn.execute("SELECT COUNT(*) FROM story_posts WHERE status = 'pending' AND approved = 0"
                                ).fetchone()[0],
    }


def _fresh(stamp: str | None, seconds: int) -> bool:
    """'YYYY-MM-DD HH:MM:SS' 기록이 지금부터 seconds 초 안쪽인지."""
    try:
        return (datetime.now() - datetime.strptime(stamp or "", "%Y-%m-%d %H:%M:%S")).total_seconds() < seconds
    except ValueError:
        return False


def _confidence(text: str, default: float = 0.8) -> float:
    try:
        return min(1.0, max(0.0, float(text)))
    except (TypeError, ValueError):
        return default
