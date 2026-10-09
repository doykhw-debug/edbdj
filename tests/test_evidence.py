"""보낸 증거 사진: 채팅 앱·문자·게시판으로 보낼 때 찍은 화면을 저장하고 화면에서 본다."""

import io
from types import SimpleNamespace

import pytest
from test_quizbot import FakeGorilla, add_schedule, analysis, auto_quizzes, make_runner, run_one_window
from test_sms_apps import SAMSUNG_AFTER, SAMSUNG_COMPOSE, FakeAdb
import test_sound_stop
from test_sound_stop import Win, coords_gorilla

from radio_helper import db, evidence
from radio_helper.quizbot import gorilla, sms

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


# ── 전송 테스트도 증거 사진으로 ──────────────────────────────────────
class TitledWin(Win):
    def window_text(self):
        return "공감로그"


def fake_capture(data_dir):
    """화면 찍기 흉내: inspect/<name>.png 를 만들고 이름을 돌려준다 (누를 자리 표시 수를 기록)."""
    marks = {}

    def capture(rect, name, boxes=(), points=()):
        (data_dir / "inspect").mkdir(exist_ok=True)
        (data_dir / "inspect" / f"{name}.png").write_bytes(png(64, 32))
        marks[name] = len(points)
        return f"{name}.png"
    return capture, marks


def test_send_test_returns_photos_even_when_refused(desk, monkeypatch, data_dir):
    capture, marks = fake_capture(data_dir)
    monkeypatch.setattr(gorilla, "capture", capture)
    g = coords_gorilla()
    monkeypatch.setattr(g, "_with_focus", lambda fn: fn(TitledWin()))
    res, msg = g.send_test("파워 FM 화이팅")
    assert res.status == "entered" and [label[:6] for label, _ in res.shots] == ["누르기 직전", "보낸 뒤"]
    assert marks["gorilla_test"] == 2                                         # 빨강(입력칸)·초록(전송 버튼)

    desk.at_point = {"hwnd": 3003, "title": "고릴라", "process": "gorealra.exe"}   # 플레이어 창이 가림
    res, msg = g.send_test("파워 FM 화이팅")
    assert res.status == "failed" and "가리고" in msg and "보내지 못했을 때" in msg
    assert [label[:9] for label, _ in res.shots] == ["보내지 못했을 때"] and marks["gorilla_test"] == 2


def test_send_test_command_saves_evidence(client, conn, monkeypatch):
    from contextlib import nullcontext

    from radio_helper.quizbot import __main__ as qmain
    from radio_helper.quizbot import runner

    shots = [("누르기 직전 (빨간 원 = 입력칸으로 누른 곳, 초록 원 = 전송 버튼)", png(300, 200)), ("보낸 뒤", png(300, 200))]
    monkeypatch.setattr(runner, "shared_input_lock", lambda: nullcontext())
    monkeypatch.setattr(gorilla.Gorilla, "send_test",
                        lambda self, text: (gorilla.SendResult("posted", "채팅 목록에서 확인", shots), "보냈습니다"))
    assert qmain.cmd_send_test(conn, "gorilla") == 0
    rows = conn.execute("SELECT * FROM evidence WHERE item_table = 'tests'").fetchall()
    assert [r["via"] for r in rows] == ["gorilla", "gorilla"] and rows[0]["text"] == "파워 FM 화이팅"
    assert "증거 사진 화면에 2장 저장" in conn.execute("SELECT message FROM events ORDER BY id DESC").fetchone()[0]
    page = client.get("/evidence?t=tests").get_data(as_text=True)
    assert "전송 테스트" in page and "전송 테스트 #0" not in page and "/gorilla?app=gorilla" in page


def test_caption_window_hidden_while_sending(monkeypatch):
    calls = []
    fake = SimpleNamespace(FindWindowW=lambda cls, title: 77 if title == gorilla.CAPTION_TITLE else 0,
                           IsWindowVisible=lambda h: True, ShowWindow=lambda h, cmd: calls.append((h, cmd)))
    monkeypatch.setattr(gorilla, "_user32", lambda: fake)
    hidden = gorilla.hide_caption_window()
    gorilla.show_windows(hidden)
    assert hidden == [77] and calls == [(77, 0), (77, 4)]                    # 숨김 → 다시 보임(포커스는 안 뺏음)
    fake.IsWindowVisible = lambda h: False                                    # 자막 창을 닫아 두었으면 손대지 않음
    assert gorilla.hide_caption_window() == [] and len(calls) == 2


def test_browser_window_inspection_is_not_shown(client, data_dir):
    import json

    (data_dir / "inspect").mkdir(exist_ok=True)
    report = {"title": "고릴라·인식 설정 · 라디오 참여 도우미 - Chrome", "process": "chrome.exe", "edit_count": 17,
              "edit_names": [], "button_names": ["Claude", "Papago"], "controls": []}
    (data_dir / "inspect" / "gorilla_20261002_231833.json").write_text(json.dumps(report, ensure_ascii=False),
                                                                       encoding="utf-8")
    assert "마지막 창 점검" not in client.get("/gorilla").get_data(as_text=True)
    report.update(title="공감로그", process="gorealra.exe")
    (data_dir / "inspect" / "gorilla_20261009_120000.json").write_text(json.dumps(report, ensure_ascii=False),
                                                                       encoding="utf-8")
    assert "마지막 창 점검" in client.get("/gorilla").get_data(as_text=True)
