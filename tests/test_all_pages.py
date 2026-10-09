"""모든 화면이 실제와 비슷한 데이터가 있을 때도 열리는지 (Internal Server Error 가 나지 않는지).

다른 시험의 흐름으로 데이터를 채운 뒤, 주소에 번호가 필요한 화면은 데이터베이스의 실제 번호로 연다.
"""

import json
import sqlite3
from datetime import datetime

import test_people
import test_sound_stop
import test_story_gift_import
import test_witty_chat
from conftest import make_draft, make_experience

from radio_helper import db

PARAM_TABLE = {"pid": None, "cid": "corners", "eid": "experiences", "did": "drafts", "lid": "story_library",
               "mid": "mock_posts"}


def fill(client, conn, monkeypatch):
    # 다른 시험의 흐름으로 데이터만 채운다 (서로 데이터가 섞여 그 시험의 확인 문장이 틀려도 상관없음)
    for step in (lambda: test_people.test_import_people_and_library(conn),
                 lambda: test_people.test_bulk_import_library_as_unconfirmed(client, conn),
                 lambda: test_people.test_move_all_real_events_confirmed_and_enrich_starts(client, conn, monkeypatch),
                 lambda: test_story_gift_import.test_gifts_recorded_by_channel_and_merged(conn),
                 lambda: test_story_gift_import.test_story_waits_for_confirmation_then_sends(conn),
                 lambda: test_story_gift_import.test_no_fitting_experience_records_topic_for_manual(conn),
                 lambda: test_witty_chat.test_board_only_story_becomes_board_draft(conn),
                 lambda: test_witty_chat.test_low_confidence_quiz_sends_witty_answer(conn),
                 lambda: test_sound_stop.test_cut_after_send_is_logged_with_last_action_and_speaker_change(conn)):
        try:
            step()
        except (AssertionError, TypeError, StopIteration, IndexError):
            pass
    eid = make_experience(conn)
    make_draft(conn, eid)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for k, v in {"live.active": "1", "quizbot.state": "듣는 중", "quizbot.heartbeat": now, "quizbot.level_at": now,
                 "quizbot.level": "40", "quizbot.silent_since": now,
                 "quizbot.last_chunk": json.dumps({"at": now[11:], "level": 40, "seconds": 0.5, "text": "안녕"}),
                 "quizbot.stt_test": json.dumps({"at": now, "running": False, "ok": True, "phase": "",
                                                 "steps": [{"name": "녹음 장치", "ok": True, "detail": "스피커"}],
                                                 "text": "안녕", "level": 40}),
                 "gorilla.region_window": "0,0,640,950", "gorilla.screen_region": "0,0,640,950"}.items():
        db.set_setting(conn, k, v)


def route_urls(app, conn):
    urls = []
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.endpoint == "static" or rule.rule.startswith("/inspect-image"):
            continue
        args = {}
        for name in rule.arguments:
            table = PARAM_TABLE.get(name)
            if name == "pid":
                table = "people" if rule.rule.startswith("/people") else "programs"
            row = conn.execute(f"SELECT id FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
            if row is None:
                break
            args[name] = row[0]
        else:
            urls.append(rule.rule.replace("<int:", "<").format(**{}) if not args else
                        _build(rule.rule, args))
    return urls


def _build(rule, args):
    for k, v in args.items():
        rule = rule.replace(f"<int:{k}>", str(v))
    return rule


def test_refresh_does_not_lock_data_while_opening_pages(conn):
    """프로그램·게시판 목록을 다시 읽는 몇 분 동안에도 관리 화면·청취가 데이터를 쓸 수 있어야 한다
    (예전에는 게시판 페이지를 여는 동안 잠가 두어 'database is locked' → Internal Server Error)."""
    from test_programs import CB, MAIN

    from radio_helper import programs

    pages = {p: ("", []) for (p,) in conn.execute("SELECT main_url FROM programs")}
    pages[MAIN] = ("매일 07:00 ~ 09:00", [("/radio/0chulpowerfm/cornerboards/57577?cornerid=3002", "사연")])
    pages[CB + "?cornerid=3002"] = ("", [("?cornerid=3099", "새 코너")])
    pages[programs.RADIO_HOME_URL] = ("", [("https://programs.sbs.co.kr/radio/lovenew/main", "러브 새 프로그램")])
    pages["https://programs.sbs.co.kr/radio/lovenew/main"] = (
        "SBS 러브FM 103.5 · 러브FM 매일 오후 2시 ~ 4시", [("/radio/lovenew/cornerboards/1?cornerid=2", "사연")])
    other = sqlite3.connect(str(db.db_path()), timeout=0.2)    # 관리 화면·청취 실행기 쪽 연결
    blocked, opened = [], []

    def fetch(url):
        opened.append(url)
        try:
            other.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('probe', ?)", (url,))
            other.commit()
        except sqlite3.OperationalError as e:
            blocked.append((url, str(e)))
        return pages.get(url, ("", []))

    programs.refresh(conn, fetch)
    other.close()
    assert CB + "?cornerid=3002" in opened and blocked == []


def test_listener_survives_locked_data(conn, monkeypatch):
    """데이터가 잠시 잠겨 오류 기록조차 못 해도 듣기 실행기가 꺼지지 않고 다시 시도한다."""
    from test_quizbot import MON_0700, Clock, FakeGorilla, ScriptAnswerer, ScriptTranscriber, add_schedule

    from radio_helper.quizbot import runner

    add_schedule(conn)
    clock, sleeps, printed = Clock(MON_0700), [], []
    real_log = db.log

    def locked_log(c, kind, message):
        if len(sleeps) < 1:
            raise sqlite3.OperationalError("database is locked")
        real_log(c, kind, message)

    def sleep(sec):
        sleeps.append(sec)
        clock.sleep(sec)
        if len(sleeps) >= 2:
            db.set_setting(conn, "quizbot.stop", "1")

    def locked_recorder(chunk):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "log", locked_log)
    deps = runner.Deps(recorder_factory=locked_recorder, transcriber_factory=lambda m, p: ScriptTranscriber({}),
                       answerer=ScriptAnswerer([]), sender=FakeGorilla(), now=clock.now, sleep=sleep,
                       log=printed.append)
    runner.Runner(conn, deps).run_forever()                     # 예전에는 여기서 오류로 실행기가 꺼짐
    assert sleeps[:2] == [30, 30] and db.get_setting(conn, "quizbot.state") == "멈춤"
    assert any("database is locked" in p for p in printed)


def test_error_page_shows_cause_and_logs(app, client, data_dir):
    app.config["PROPAGATE_EXCEPTIONS"] = False

    def locked():
        raise sqlite3.OperationalError("database is locked")

    def broken():
        return {}["없는 칸"]

    app.add_url_rule("/test-locked", "test_locked", locked)
    app.add_url_rule("/test-broken", "test_broken", broken)
    r = client.get("/test-locked")
    html = r.get_data(as_text=True)
    assert r.status_code == 500 and "OperationalError: database is locked" in html
    assert "데이터를 쓰는 중" in html and "새로 고침(F5)" in html and "test_all_pages.py:" in html  # 위치 표시
    r = client.get("/test-broken?x=1")
    html = r.get_data(as_text=True)
    assert "KeyError" in html and "GET /test-broken?x=1" in html and "캡처해 보내" in html
    log = (data_dir / "logs" / "errors.log").read_text(encoding="utf-8")
    assert "GET /test-locked" in log and "Traceback" in log and "KeyError" in log


def test_every_page_opens_with_data(client, conn, app, monkeypatch):
    fill(client, conn, monkeypatch)
    broken = []
    for url in route_urls(app, conn):
        if url in ("/shutdown",) or "/mock/view" in url and "<" in url:
            continue
        r = client.get(url)
        if r.status_code >= 500:
            broken.append(url)
    assert broken == []
