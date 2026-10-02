import inspect
import os
import threading
from pathlib import Path

import pytest
from conftest import make_draft, make_experience

from radio_helper import autofill, db

CHROMIUM = os.environ.get("RADIO_HELPER_CHROMIUM") or "/opt/pw-browsers/chromium"


def test_real_modes_never_click():
    for fn in (autofill.run_fill, autofill.run_inspect):
        assert ".click(" not in inspect.getsource(fn)


def test_real_url_allowlist():
    ok = "https://programs.sbs.co.kr/radio/0chulpowerfm/cornerboardwrite/57577/?cornerid=3002"
    assert autofill._allowed_real_url(ok) and autofill._is_write_page(ok)
    assert autofill._allowed_real_url("https://m.programs.sbs.co.kr/radio/x/cornerboardwrite/1")
    assert not autofill._allowed_real_url("http://programs.sbs.co.kr/radio/")
    assert not autofill._allowed_real_url("https://programs.sbs.co.kr.evil.com/")
    assert not autofill._is_write_page("https://programs.sbs.co.kr/radio/0chulpowerfm/cornerboards/57577")


def test_input_lock_serializes(data_dir):
    with autofill.input_lock():
        with pytest.raises(autofill.AutofillError):
            with autofill.input_lock():
                pass
    with autofill.input_lock():  # 끝나면 다시 잡을 수 있다
        pass


def test_blocked_run_exits_without_browser(conn, capsys):
    did = make_draft(conn, make_experience(conn))
    assert autofill.main(["--draft", str(did), "--mode", "fill"]) == 2
    assert "[차단]" in capsys.readouterr().out


@pytest.mark.skipif(not Path(CHROMIUM).exists(), reason="Chromium 없음")
def test_mock_end_to_end(conn, app, monkeypatch):
    pytest.importorskip("playwright")
    from werkzeug.serving import make_server

    monkeypatch.setenv("RADIO_HELPER_CHROMIUM", CHROMIUM)
    did = make_draft(conn, make_experience(conn), title="모의 제목",
                     body="영철 씨 안녕하세요.\n\n여러 줄 본문입니다.", song="아이유 - 좋은 날")
    server = make_server("127.0.0.1", 0, app)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        rc = autofill.main(["--draft", str(did), "--mode", "mock", "--headless",
                            "--base-url", f"http://127.0.0.1:{server.server_port}/"])
    finally:
        server.shutdown()
    assert rc == 0
    post = conn.execute("SELECT * FROM mock_posts ORDER BY id DESC").fetchone()
    assert post["title"] == "모의 제목"
    assert post["content"].replace("\r\n", "\n") == "영철 씨 안녕하세요.\n\n여러 줄 본문입니다."
    assert post["song"] == "아이유 - 좋은 날" and post["cornerid"] == "3002"
    # 모의 시험은 실제 제출 이력을 만들지 않는다
    assert conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0] == 0
    assert not (db.data_dir() / "input.lock").exists()
