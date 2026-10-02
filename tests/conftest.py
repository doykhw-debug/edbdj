import re

import pytest

from radio_helper import db


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RADIO_HELPER_DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def conn(data_dir):
    c = db.connect()
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture
def app(data_dir):
    from radio_helper.app import create_app

    a = create_app()
    a.config["TESTING"] = True
    return a


@pytest.fixture
def client(app):
    c = app.test_client()
    c.environ_base["HTTP_HOST"] = "127.0.0.1:5000"
    return c


def csrf(client, path="/"):
    html = client.get(path).get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', html)
    if m:
        return m.group(1)
    with client.session_transaction() as s:
        return s["csrf"]


def make_experience(conn, **over):
    fields = dict(label="편집 화면", when_text="지난주 토요일", people="나, 옆지기, 첫째",
                  story="주말에 집에서 영상 편집을 하는데 첫째가 옆에 와서 한참 화면을 봤습니다.",
                  quotes="아빠 유튜버야?", quote_kind="gist", highlight="첫째가 아빠를 유튜버로 생각한 게 웃겼어요.",
                  ending="그날 이후 첫째가 편집할 때마다 옆에 앉아 있어요.", fixed_facts="", hide="", song="",
                  prior_history="", user_confirmed=1)
    fields.update(over)
    cols = ", ".join(fields)
    cur = conn.execute(
        f"INSERT INTO experiences ({cols}, created_at, updated_at) VALUES ({', '.join('?' * len(fields))}, ?, ?)",
        (*fields.values(), db.now(), db.now()))
    conn.commit()
    return cur.lastrowid


def target_corner_id(conn):
    return conn.execute("SELECT id FROM corners WHERE is_target = 1").fetchone()["id"]


def make_draft(conn, exp_id, corner_id=None, **over):
    fields = dict(title="편집하는 아빠", body="영철 씨 안녕하세요. 첫째가 제 편집 화면을 봤어요.", song="",
                  source="template", status="draft", fact_confirmed=0)
    fields.update(over)
    cur = conn.execute(
        f"INSERT INTO drafts (experience_id, corner_id, {', '.join(fields)}, created_at, updated_at) "
        f"VALUES (?, ?, {', '.join('?' * len(fields))}, ?, ?)",
        (exp_id, corner_id or target_corner_id(conn), *fields.values(), db.now(), db.now()))
    conn.commit()
    return cur.lastrowid


def open_corner(conn, corner_id=None, **over):
    fields = dict(recruiting="open", notice_checked_at=db.now(), notice_changed=0, deadline="2099-12-31",
                  ai_assist_policy="allowed")
    fields.update(over)
    conn.execute(f"UPDATE corners SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                 (*fields.values(), corner_id or target_corner_id(conn)))
    conn.commit()
