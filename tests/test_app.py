from conftest import csrf, make_experience, open_corner, target_corner_id

from radio_helper import app as app_module
from radio_helper import db


def test_pages_render(client, conn):
    eid = make_experience(conn)
    for path in ["/", "/profile", "/corners", f"/corners/{target_corner_id(conn)}", "/experiences",
                 "/experiences/new", f"/experiences/{eid}", "/drafts", "/submissions", "/quizzes", "/settings",
                 "/mock/write?cornerid=3002"]:
        r = client.get(path)
        assert r.status_code == 200, path


def test_rejects_foreign_host(client):
    r = client.get("/", headers={"Host": "evil.example.com"})
    assert r.status_code == 403


def test_post_requires_csrf(client, conn):
    from radio_helper import db

    r = client.post("/settings", data={"global_stop": "1"})                  # 확인값 없음 → 처리 안 하고 돌려보냄
    assert r.status_code == 302 and not db.is_stopped(conn)
    token = csrf(client, "/settings")
    assert client.post("/settings", data={"global_stop": "1", "csrf_token": token}).status_code == 302


def test_story_flow_until_approval(client, conn, monkeypatch):
    token = csrf(client)
    client.post("/profile", data={"csrf_token": token, "nickname": "편집하는 아빠", "tone": "유쾌함",
                                  "banned_words": "김약사"})
    r = client.post("/experiences/new", data={
        "csrf_token": token, "story": "김약사님이 제 편집 부업 얘기를 듣고 놀라셨어요.", "quote_kind": "none",
        "user_confirmed": "1"})
    assert r.status_code == 302
    eid = conn.execute("SELECT MAX(id) FROM experiences").fetchone()[0]
    r = client.post("/drafts/new", data={"csrf_token": token, "experience_id": eid,
                                         "corner_id": target_corner_id(conn)})
    did = int(r.headers["Location"].rsplit("/", 1)[-1])
    d = conn.execute("SELECT * FROM drafts WHERE id = ?", (did,)).fetchone()
    assert "김약사" not in d["body"] and "편집하는 아빠" in d["body"]

    # 사실 확인 체크 없이 승인 불가
    client.post(f"/drafts/{did}/approve", data={"csrf_token": token})
    assert conn.execute("SELECT status FROM drafts WHERE id = ?", (did,)).fetchone()[0] == "draft"
    client.post(f"/drafts/{did}/approve", data={"csrf_token": token, "fact_confirmed": "1"})
    assert conn.execute("SELECT status FROM drafts WHERE id = ?", (did,)).fetchone()[0] == "approved"

    # 내용을 고치면 승인이 해제된다
    client.post(f"/drafts/{did}", data={"csrf_token": token, "title": d["title"], "body": d["body"] + " 추가",
                                        "song": "", "source": "manual"})
    assert conn.execute("SELECT status, fact_confirmed FROM drafts WHERE id = ?", (did,)).fetchone()[:] == ("draft", 0)

    # 실제 화면 입력은 코너 조건이 확인되지 않아 막힌다
    launched = []
    monkeypatch.setattr(app_module, "launch_autofill", lambda args: launched.append(args) or "x.log")
    r = client.post(f"/drafts/{did}/run", data={"csrf_token": token, "mode": "fill"}, follow_redirects=True)
    assert launched == [] and "모집 중" in r.get_data(as_text=True)


def test_approve_blocked_by_personal_info(client, conn):
    token = csrf(client)
    eid = make_experience(conn)
    r = client.post("/drafts/new", data={"csrf_token": token, "experience_id": eid,
                                         "corner_id": target_corner_id(conn)})
    did = int(r.headers["Location"].rsplit("/", 1)[-1])
    client.post(f"/drafts/{did}", data={"csrf_token": token, "title": "제목", "body": "제 번호는 010-1111-2222",
                                        "song": "", "source": "manual"})
    client.post(f"/drafts/{did}/approve", data={"csrf_token": token, "fact_confirmed": "1"})
    assert conn.execute("SELECT status FROM drafts WHERE id = ?", (did,)).fetchone()[0] == "draft"


def test_paste_chat_result(client, conn):
    token = csrf(client)
    eid = make_experience(conn)
    r = client.post("/drafts/new", data={"csrf_token": token, "experience_id": eid,
                                         "corner_id": target_corner_id(conn)})
    did = int(r.headers["Location"].rsplit("/", 1)[-1])
    client.post(f"/drafts/{did}/paste", data={"csrf_token": token, "result":
        "제목 후보:\n1. 아빠는 유튜버?\n2. 둘째\n본문:\n영철 씨, 다듬은 본문입니다.\n신청곡: 없음\n확인 필요:\n- 아이 나이"})
    d = conn.execute("SELECT * FROM drafts WHERE id = ?", (did,)).fetchone()
    assert d["title"] == "아빠는 유튜버?" and d["body"] == "영철 씨, 다듬은 본문입니다." and d["source"] == "pasted"


def test_notice_change_via_form(client, conn):
    token = csrf(client)
    cid = target_corner_id(conn)
    form = {"csrf_token": token, "recruiting": "open", "ai_assist_policy": "allowed", "is_target": "1",
            "write_url": "https://programs.sbs.co.kr/radio/0chulpowerfm/cornerboardwrite/57577/?cornerid=3002",
            "notice_text": "공지 A"}
    client.post(f"/corners/{cid}", data=form)
    client.post(f"/corners/{cid}/confirm-notice", data={"csrf_token": token})
    assert conn.execute("SELECT notice_checked_at FROM corners WHERE id = ?", (cid,)).fetchone()[0]
    client.post(f"/corners/{cid}", data={**form, "notice_text": "공지 B (변경)"})
    row = conn.execute("SELECT notice_changed, notice_checked_at FROM corners WHERE id = ?", (cid,)).fetchone()
    assert row["notice_changed"] == 1 and row["notice_checked_at"] is None


def test_quiz_dedupe_via_form(client, conn):
    token = csrf(client)
    data = {"csrf_token": token, "program": "김영철의 파워FM", "broadcast_date": "2026-10-02",
            "question_key": "1부", "kind": "new", "gorilla_accepted": "unknown"}
    client.post("/quizzes/new", data=data)
    r = client.post("/quizzes/new", data=data, follow_redirects=True)
    assert "재안내" in r.get_data(as_text=True)
    assert conn.execute("SELECT COUNT(*) FROM quizzes").fetchone()[0] == 1


def test_fill_launches_only_when_all_confirmed(client, conn, monkeypatch):
    token = csrf(client)
    open_corner(conn)
    eid = make_experience(conn)
    r = client.post("/drafts/new", data={"csrf_token": token, "experience_id": eid,
                                         "corner_id": target_corner_id(conn)})
    did = int(r.headers["Location"].rsplit("/", 1)[-1])
    client.post(f"/drafts/{did}/approve", data={"csrf_token": token, "fact_confirmed": "1"})
    launched = []
    monkeypatch.setattr(app_module, "launch_autofill", lambda args: launched.append(args) or "x.log")
    client.post(f"/drafts/{did}/run", data={"csrf_token": token, "mode": "fill"})
    assert launched == [["--draft", str(did), "--mode", "fill"]]
    db.set_setting(conn, "global_stop", "1")
    client.post(f"/drafts/{did}/run", data={"csrf_token": token, "mode": "fill"})
    assert len(launched) == 1
