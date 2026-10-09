"""보낸 증거 사진: 채팅 앱·문자·게시판으로 보낼 때 찍은 화면을 저장하고 화면에서 본다."""

import io

import pytest
from test_quizbot import FakeGorilla, add_schedule, analysis, auto_quizzes, make_runner, run_one_window
from test_sms_apps import SAMSUNG_AFTER, SAMSUNG_COMPOSE, FakeAdb
import test_sound_stop
from test_sound_stop import Win, coords_gorilla

from radio_helper import db, evidence
from radio_helper.quizbot import sms

PIL = pytest.importorskip("PIL.Image")
desk = test_sound_stop.desk   # 가짜 윈도우 화면 준비물을 같이 쓴다


def png(w=2000, h=1000, color=(200, 30, 30)) -> bytes:
    out = io.BytesIO()
    PIL.new("RGB", (w, h), color).save(out, format="PNG")
    return out.getvalue()


class ShotGorilla(FakeGorilla):
    """보낸 뒤 창 사진을 함께 돌려주는 가짜 고릴라."""

    def send(self, text):
        res = super().send(text)
        res.shots = [("보낸 뒤 고릴라 창", png(900, 600))]
        return res


def test_save_shrinks_to_jpeg_and_serves_only_inside_folder(client, conn, data_dir):
    [eid] = evidence.save(conn, "quizzes", 7, "gorilla", "사과", "posted", [("보낸 뒤 고릴라 창", png())])
    row = conn.execute("SELECT * FROM evidence WHERE id = ?", (eid,)).fetchone()
    path = data_dir / "evidence" / row["file"]
    assert path.suffix == ".jpg" and PIL.open(path).size == (1600, 800)          # 가로 1600 으로 줄임
    assert (row["item_table"], row["item_id"], row["via"], row["status"]) == ("quizzes", 7, "gorilla", "posted")
    r = client.get(f"/evidence/{eid}/image")
    assert r.status_code == 200 and r.mimetype == "image/jpeg" and r.data[:2] == b"\xff\xd8"
    conn.execute("UPDATE evidence SET file = '../radio_helper.sqlite3' WHERE id = ?", (eid,))  # 폴더 밖은 열지 않음
    conn.commit()
    assert evidence.file_of(conn, eid) is None and client.get(f"/evidence/{eid}/image").status_code == 404
    assert evidence.save(conn, "quizzes", 7, "gorilla", "", "posted", [("빈 사진", b""), ("없음", None)]) == []


def test_quiz_answer_sent_with_evidence_and_shown(client, conn):
    add_schedule(conn)
    r, clock, _ans, sender = make_runner(conn, {2: "오늘의 퀴즈 철수가 좋아하는 과일은"}, [analysis()], sender=ShotGorilla())
    run_one_window(conn, r, clock)
    q = auto_quizzes(conn)[0]
    assert sender.sent == ["사과"] and "증거 사진 1장" in q["note"]
    [ev] = conn.execute("SELECT * FROM evidence WHERE item_table = 'quizzes' AND item_id = ?", (q["id"],)).fetchall()
    assert ev["text"] == "사과" and ev["label"] == "보낸 뒤 고릴라 창" and ev["status"] == "entered"

    assert "📷 증거 1장" in client.get("/quizzes").get_data(as_text=True)
    page = client.get(f"/evidence?t=quizzes&id={q['id']}").get_data(as_text=True)
    assert "보낸 뒤 고릴라 창" in page and "사과" in page and f"/evidence/{ev['id']}/image" in page
    assert f"퀴즈 정답 #{q['id']}" in page and "채팅 입력함" in page
    assert "증거 사진" in client.get("/").get_data(as_text=True)                         # 메뉴
    day = ev["taken_at"][:10]
    assert f"/evidence/{ev['id']}/image" in client.get(f"/evidence?day={day}").get_data(as_text=True)
    assert f"/evidence/{ev['id']}/image" not in client.get("/evidence?day=2000-01-01").get_data(as_text=True)


def test_story_sent_by_chat_keeps_evidence(client, conn):
    pid = conn.execute(
        "INSERT INTO story_posts (channel, program, broadcast_date, topic, message, status, created_at, updated_at) "
        "VALUES ('파워FM', '김영철의 파워FM', '2026-10-09', '첫 출근', '첫 출근 날 커피를 쏟았어요', 'pending', ?, ?)",
        (db.now(), db.now())).lastrowid
    conn.commit()
    r, *_ = make_runner(conn, {}, [], sender=ShotGorilla())
    assert r.deliver("story_posts", "status", pid, "app", "파워FM", "첫 출근 날 커피를 쏟았어요", "자동", f"사연 #{pid}") \
        == "entered"
    assert evidence.counts(conn, "story_posts") == {pid: 1}
    sent = client.get("/stories/sent").get_data(as_text=True)
    assert f"/evidence?t=story_posts&amp;id={pid}" in sent and "📷 증거 1장" in sent


def test_phone_sms_shows_compose_and_sent_screens():
    res = sms.AdbSms("adb", run=FakeAdb([SAMSUNG_COMPOSE, SAMSUNG_AFTER]), sleep=lambda s: None).send("#1077", "정답 사과")
    assert res.status == "entered"
    assert [label for label, _ in res.shots] == ["문자 작성 화면 (받는 번호·글)", "문자 보낸 뒤 화면"]
    refused = sms.AdbSms("adb", run=FakeAdb([SAMSUNG_COMPOSE.replace("#1077", "1077")]), sleep=lambda s: None)
    res = refused.send("#1077", "정답")
    assert res.status == "failed" and [label for label, _ in res.shots] == ["문자 작성 화면 (받는 번호·글)"]


def test_gorilla_send_photographs_window_even_when_it_refuses(desk, monkeypatch):
    g = coords_gorilla()
    monkeypatch.setattr(g, "_with_focus", lambda fn: fn(Win()))
    monkeypatch.setattr(g, "evidence_image", lambda w: b"\xff\xd8jpeg")
    res = g.send("사과")
    assert res.status == "entered" and res.shots == [("보낸 뒤 고릴라 창", b"\xff\xd8jpeg")]
    desk.at_point = {"hwnd": 2002, "title": "라디오 자막", "process": "python.exe"}
    res = g.send("사과")
    assert res.status == "failed" and res.shots == [("보내지 못했을 때 고릴라 창", b"\xff\xd8jpeg")]


def test_evidence_page_empty_and_filters(client):
    page = client.get("/evidence").get_data(as_text=True)
    assert "아직 찍어 둔 사진이 없습니다" in page and "모두 0장" in page
    assert client.get("/evidence?t=../etc&day=x").status_code == 200
    assert client.get("/evidence/999/image").status_code == 404
