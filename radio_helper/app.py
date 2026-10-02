"""로컬 관리 화면 (Flask). 이 PC(127.0.0.1)에서만 열린다."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for

from . import db, gates, generator, quiz
from .gates import STATUS_LABELS
from .quizbot import config as qconfig
from .quizbot import schedule as qschedule

PROJECT_ROOT = Path(__file__).resolve().parent.parent
YES_NO = {"unknown": "미확인", "yes": "예", "no": "아니오"}
RECRUITING = {"unknown": "미확인", "open": "모집 중", "closed": "모집 안 함"}
AI_POLICY = {"unknown": "미확인", "allowed": "제한 없음 확인", "restricted": "제한 있음"}
DRAFT_STATUS = {"draft": "작성 중", "approved": "승인됨", "archived": "보관"}
SOURCE = {"template": "템플릿 초안", "pasted": "AI 결과 붙여넣음", "manual": "직접 작성"}


def create_app(data_dir: str | None = None) -> Flask:
    if data_dir:
        os.environ["RADIO_HELPER_DATA_DIR"] = data_dir
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secrets.token_hex(32),
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_HTTPONLY=True,
    )
    conn = db.connect()
    db.init_db(conn)
    conn.close()

    app.jinja_env.globals.update(
        YES_NO=YES_NO, RECRUITING=RECRUITING, AI_POLICY=AI_POLICY, DRAFT_STATUS=DRAFT_STATUS,
        SOURCE=SOURCE, POST_STATUS=STATUS_LABELS, QUIZ_KIND=quiz.KIND_LABELS, QUIZ_ENTRY=quiz.ENTRY_LABELS,
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
            abort(400, "요청 확인값이 맞지 않습니다. 화면을 새로 고친 뒤 다시 시도하세요.")
        g.conn = db.connect()

    @app.teardown_request
    def close_conn(_exc):
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    @app.context_processor
    def inject():
        return {"csrf_token": session.get("csrf", ""), "stopped": db.is_stopped(g.conn) if "conn" in g else False}

    def one(sql, *params):
        row = g.conn.execute(sql, params).fetchone()
        if row is None:
            abort(404)
        return row

    def form(name: str, default: str = "") -> str:
        return (request.form.get(name) or default).strip()

    # ── 대시보드 ───────────────────────────────────────────────────
    @app.get("/")
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
        return render_template("corners.html", corners=rows, programs=programs_, program=program)

    # ── 파워FM 프로그램 ───────────────────────────────────────────
    @app.get("/programs")
    def programs():
        rows = g.conn.execute(
            """SELECT p.*, (SELECT COUNT(*) FROM corners c WHERE c.program = p.title) AS boards
               FROM programs p WHERE p.on_air = 1 ORDER BY p.start_time""").fetchall()
        candidates = g.conn.execute("SELECT * FROM programs WHERE on_air = 0 ORDER BY id").fetchall()
        last = g.conn.execute(
            "SELECT * FROM events WHERE kind = 'programs' ORDER BY id DESC LIMIT 1").fetchone()
        return render_template("programs.html", rows=rows, candidates=candidates, last=last)

    @app.post("/programs/refresh")
    def programs_refresh():
        if db.is_stopped(g.conn):
            flash("일괄 중지가 켜져 있습니다.", "error")
            return redirect(url_for("programs"))
        log_name = launch_programs_refresh()
        flash("공식 페이지에서 방송 시간과 게시판을 읽는 중입니다. 1~2분 뒤 이 화면을 새로 고치세요. "
              f"(기록: {log_name})")
        return redirect(url_for("programs"))

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
                """UPDATE corners SET recruiting = ?, char_limit = ?, deadline = ?, required_fields = ?,
                       ai_assist_policy = ?, is_target = ?, write_url = ?, title_selector = ?, body_selector = ?,
                       song_selector = ?, updated_at = ? WHERE id = ?""",
                (form("recruiting", "unknown"), int(limit) if limit.isdigit() else None, form("deadline") or None,
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
                  "fixed_facts", "hide", "song", "prior_history"]

    @app.get("/experiences")
    def experiences():
        rows = g.conn.execute(
            """SELECT e.*, (SELECT COUNT(*) FROM submissions s WHERE s.experience_id = e.id
                            AND s.post_status IN ('filled','posted','unknown')) AS used
               FROM experiences e ORDER BY e.id DESC""").fetchall()
        return render_template("experiences.html", rows=rows)

    @app.route("/experiences/new", methods=["GET", "POST"])
    @app.route("/experiences/<int:eid>", methods=["GET", "POST"])
    def experience_edit(eid=None):
        exp = one("SELECT * FROM experiences WHERE id = ?", eid) if eid else None
        corners_ = g.conn.execute("SELECT id, title, is_target FROM corners ORDER BY is_target DESC, id").fetchall()
        if request.method == "POST":
            values = {f: form(f) for f in EXP_FIELDS}
            if not values["story"]:
                flash("'실제로 있었던 일'은 비워 둘 수 없습니다.", "error")
                return render_template("experience_edit.html", e=values, eid=eid, drafts=[], corners=corners_)
            values["quote_kind"] = values["quote_kind"] if values["quote_kind"] in ("exact", "gist") else "none"
            confirmed = 1 if request.form.get("user_confirmed") else 0
            if exp:
                changed = any((exp[f] or "") != values[f] for f in EXP_FIELDS)
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
        return render_template("experience_edit.html", e=exp, eid=eid, drafts=drafts, corners=corners_)

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
        d = generator.template_draft(exp, db.get_profile(g.conn), corner)
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
        return render_template("submissions.html", rows=rows)

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
        rows = g.conn.execute("SELECT * FROM quizzes ORDER BY broadcast_date DESC, id DESC").fetchall()
        stopped = db.is_stopped(g.conn)
        on_air = g.conn.execute("SELECT title FROM programs WHERE on_air = 1 ORDER BY start_time").fetchall()
        return render_template("quizzes.html", rows=[(q, quiz.hold_reasons(q, stopped)) for q in rows],
                               today=time.strftime("%Y-%m-%d"), programs=[r["title"] for r in on_air])

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
        pending = c.execute("SELECT * FROM quizzes WHERE source = 'auto' AND entry_status IN ('pending', 'failed') "
                            "ORDER BY id DESC LIMIT 20").fetchall()
        recent = c.execute("SELECT * FROM quizzes WHERE source = 'auto' AND entry_status NOT IN ('pending', 'failed') "
                           "ORDER BY id DESC LIMIT 20").fetchall()
        lines = c.execute("SELECT * FROM transcripts ORDER BY id DESC LIMIT 30").fetchall()
        programs_ = c.execute("SELECT id, title, start_time, end_time FROM programs WHERE on_air = 1 "
                              "ORDER BY start_time").fetchall()
        return render_template(
            "quizbot.html", schedules=schedules, active=active, nxt=nxt, by_id=by_id,
            alive=qconfig.runner_alive(c), state=db.get_setting(c, "quizbot.state"),
            heartbeat=db.get_setting(c, "quizbot.heartbeat"), check=check, has_key=bool(get_api_key()),
            pending=pending, recent=recent, lines=list(reversed(lines)), programs=programs_,
            days_label=qschedule.days_label, day_names=qschedule.DAY_NAMES,
            autostart=qconfig.get(c, "quizbot.autostart") == "1")

    @app.post("/quizbot/start")
    def quizbot_start():
        if db.is_stopped(g.conn):
            flash("일괄 중지가 켜져 있습니다. 설정에서 해제한 뒤 시작하세요.", "error")
        elif qconfig.runner_alive(g.conn):
            flash("이미 실행 중입니다.", "warn")
        else:
            db.set_setting(g.conn, "quizbot.stop", "0")
            db.set_setting(g.conn, "quizbot.state", "시작 중")
            log_name = launch_quizbot(["run"])
            flash(f"퀴즈 자동 참여를 시작합니다. 이 PC가 켜져 있고 관리 화면 창이 열려 있는 동안 동작합니다. (기록: {log_name})")
        return redirect(url_for("quizbot"))

    @app.post("/quizbot/stop")
    def quizbot_stop():
        db.set_setting(g.conn, "quizbot.stop", "1")
        db.log(g.conn, "quizbot", "멈춤 요청 (사용자)")
        flash("멈춤을 요청했습니다. 지금 듣고 있는 구간이 끝나면 멈춥니다 (최대 30초 정도).")
        return redirect(url_for("quizbot"))

    @app.post("/quizbot/settings")
    def quizbot_settings():
        db.set_setting(g.conn, "quizbot.autostart", "1" if request.form.get("autostart") else "0")
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
                   gorilla_confirmed, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (prog["id"], days, start, end, 1 if request.form.get("auto_submit") else 0,
             _confidence(form("min_confidence")), 1 if request.form.get("gorilla_confirmed") else 0, db.now(), db.now()))
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
                   updated_at = ? WHERE id = ?""",
            (1 if request.form.get("enabled") else 0, 1 if request.form.get("auto_submit") else 0,
             _confidence(form("min_confidence"), sch["min_confidence"]),
             1 if request.form.get("gorilla_confirmed") else 0, db.now(), sid))
        g.conn.commit()
        flash("예약을 저장했습니다.")
        return redirect(url_for("quizbot"))

    @app.post("/quizbot/quizzes/<int:qid>/approve")
    def quizbot_approve(qid):
        q = one("SELECT * FROM quizzes WHERE id = ?", qid)
        answer = form("answer") or (q["answer"] or "")
        if q["entry_status"] not in ("pending", "failed"):
            flash("이미 보냈거나 결과 불명인 문제는 다시 보내지 않습니다.", "error")
            return redirect(url_for("quizbot"))
        if not answer:
            flash("정답을 적어 주세요.", "error")
            return redirect(url_for("quizbot"))
        send_text = form("send_text") or None
        g.conn.execute(
            "UPDATE quizzes SET answer = ?, send_text = ?, approved = 1, kind = 'new', entry_status = 'pending', "
            "decision = '승인됨 — 실행기가 곧 보냄', updated_at = ? WHERE id = ?", (answer, send_text, db.now(), qid))
        g.conn.commit()
        db.log(g.conn, "quizbot", f"퀴즈 #{qid} 보내기 승인 (사용자): {answer}")
        if qconfig.runner_alive(g.conn):
            flash("승인했습니다. 실행기가 고릴라로 보냅니다.")
        else:
            flash("승인했습니다. 퀴즈 자동 참여가 꺼져 있어 '시작'을 눌러야 보내집니다.", "warn")
        return redirect(url_for("quizbot"))

    @app.post("/quizbot/quizzes/<int:qid>/skip")
    def quizbot_skip(qid):
        one("SELECT id FROM quizzes WHERE id = ?", qid)
        g.conn.execute("UPDATE quizzes SET entry_status = 'skipped', approved = 0, updated_at = ? "
                       "WHERE id = ? AND entry_status IN ('pending', 'failed')", (db.now(), qid))
        g.conn.commit()
        flash(f"퀴즈 #{qid}은(는) 보내지 않습니다.")
        return redirect(url_for("quizbot"))

    @app.post("/quizbot/tool")
    def quizbot_tool():
        tool = form("tool")
        args = {"check": ["check"], "auto-setup": ["auto-setup"], "inspect": ["inspect-gorilla"],
                "calibrate-input": ["calibrate", "input"],
                "calibrate-send": ["calibrate", "send"], "type-test": ["type-test"]}.get(tool)
        if args is None:
            abort(400)
        if tool != "check" and db.is_stopped(g.conn):
            flash("일괄 중지가 켜져 있습니다.", "error")
            return redirect(url_for("gorilla"))
        log_name = launch_quizbot(args)
        messages = {
            "check": "환경을 점검합니다. 10~30초 뒤 이 화면을 새로 고치세요.",
            "auto-setup": "열린 창 중에서 고릴라 채팅창을 찾습니다 (읽기만 함). 10~30초 뒤 이 화면을 새로 고치세요.",
            "inspect": "고릴라 창의 화면 요소를 읽습니다 (아무것도 누르지 않음).",
            "calibrate-input": "5초 안에 마우스를 고릴라 채팅 입력칸 위에 올려 두세요.",
            "calibrate-send": "5초 안에 마우스를 고릴라 전송 버튼 위에 올려 두세요.",
            "type-test": "고릴라 입력칸에 '입력 테스트'를 넣습니다. 보내지 않으니 확인 후 직접 지우세요.",
        }
        flash(f"{messages[tool]} (기록: {log_name})")
        return redirect(url_for("quizbot" if tool == "check" else "gorilla"))

    @app.route("/gorilla", methods=["GET", "POST"])
    def gorilla():
        keys = [k for k in qconfig.DEFAULTS if k.startswith("gorilla.") and not k.endswith(("_x", "_y"))] + \
               [k for k in qconfig.DEFAULTS if k.startswith("quizbot.") and k != "quizbot.autostart"]
        if request.method == "POST":
            for k in keys:
                if k in request.form:
                    db.set_setting(g.conn, k, request.form[k].strip())
            flash("고릴라·인식 설정을 저장했습니다. 실행 중이면 '멈춤' 후 다시 '시작'하면 적용됩니다.")
            return redirect(url_for("gorilla"))
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

        report = latest("gorilla_2*.json")       # 창 점검
        auto = latest("gorilla_auto_*.json")     # 자동 찾기
        if report and auto and report["file"][len("gorilla_"):] < auto["file"][len("gorilla_auto_"):]:
            report = None  # 자동 찾기 이전의 점검 결과는 다른 창을 봤을 수 있다
        coords = {k: qconfig.get(g.conn, f"gorilla.{k}") for k in ("input_x", "input_y", "send_x", "send_y")}
        return render_template("gorilla.html", keys=keys, values={k: qconfig.get(g.conn, k) for k in keys},
                               labels=qconfig.LABELS, report=report, auto=auto, coords=coords)

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


def launch_quizbot(args: list[str]) -> str:
    return launch_module("radio_helper.quizbot", args, "quizbot")


def _confidence(text: str, default: float = 0.8) -> float:
    try:
        return min(1.0, max(0.0, float(text)))
    except (TypeError, ValueError):
        return default
