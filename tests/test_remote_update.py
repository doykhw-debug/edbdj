"""밖에서 보기(radio.haetsallab.com · Cloudflare 터널)와 자동 업데이트."""

import io
import json
import re
import urllib.error
import zipfile
from datetime import datetime
from pathlib import Path

import pytest
from conftest import csrf

import rollback_update
from radio_helper import __version__, db, remote, updater

DOMAIN = "radio.haetsallab.com"


def token_of(html: str) -> str:
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', html).group(1)


class At:
    """그 주소로 여는 브라우저 (시험 도구가 Host 를 localhost 로 바꾸지 않게 주소를 직접 준다)."""

    def __init__(self, app, base: str, cloudflare: bool = True):
        self.c, self.base = app.test_client(), base
        self.headers = {"Cf-Connecting-Ip": "203.0.113.7", "Cf-Ray": "8a1b-ICN"} if cloudflare else {}

    def get(self, path, **kw):
        return self.c.get(path, base_url=self.base, headers=self.headers, **kw)

    def post(self, path, **kw):
        return self.c.post(path, base_url=self.base, headers=self.headers, **kw)


@pytest.fixture
def outside(app):
    """밖에서 터널 주소로 여는 브라우저."""
    return At(app, f"https://{DOMAIN}")


def set_password(client, pw="radio-1234"):
    token = csrf(client, "/remote")
    return client.post("/remote", data={"csrf_token": token, "action": "password", "password": pw, "password2": pw},
                       follow_redirects=True)


def login(outside, pw):
    page = outside.get("/login")
    return outside.post("/login", data={"csrf_token": token_of(page.get_data(as_text=True)), "password": pw, "next": "/"})


# ── 원격 접속 ─────────────────────────────────────────────────────────
def test_remote_is_off_until_password_is_set(client, outside, app):
    r = outside.get("/")
    assert r.status_code == 403 and "원격 접속이 꺼져 있습니다" in r.get_data(as_text=True)
    assert "경험" not in r.get_data(as_text=True)
    assert outside.get("/health").get_json()["app"] == "radio_helper"          # 연결 확인용은 열림
    assert At(app, "http://evil.example.com", cloudflare=False).get("/").status_code == 403
    assert At(app, f"https://{DOMAIN}", cloudflare=False).get("/").status_code == 403   # 주소만으로도 원격
    assert client.get("/").status_code == 200                                   # PC 앞은 그대로


def test_remote_login_flow(client, outside, conn):
    page = set_password(client).get_data(as_text=True)
    assert "비밀번호를 정했습니다" in page
    stored = db.get_setting(conn, "remote.password_hash")
    assert stored.startswith("pbkdf2_sha256$") and "radio-1234" not in stored          # 원본은 저장하지 않음

    r = outside.get("/quizzes")
    assert r.status_code == 302 and "/login?next=/quizzes" in r.headers["Location"]
    assert outside.get("/evidence").status_code == 302 and outside.get("/nope").status_code == 302
    bad = login(outside, "wrong-pass")
    assert bad.status_code == 401 and "맞지 않습니다" in bad.get_data(as_text=True)
    ok = login(outside, "radio-1234")
    assert ok.status_code == 302 and ok.headers["Location"].endswith("/")
    home = outside.get("/").get_data(as_text=True)
    assert "원격 · 로그아웃" in home and "원격 · 로그아웃" not in client.get("/").get_data(as_text=True)
    events = " ".join(m for (m,) in conn.execute("SELECT message FROM events"))
    assert "원격 로그인 (203.0.113.7)" in events and "radio-1234" not in events

    # 밖에서는 비밀번호를 바꾸거나 끌 수 없음
    token = token_of(outside.get("/remote").get_data(as_text=True))
    r = outside.post("/remote", data={"csrf_token": token, "action": "password", "password": "hacked-999",
                                      "password2": "hacked-999"}, follow_redirects=True)
    assert "PC 앞" in r.get_data(as_text=True) and db.get_setting(conn, "remote.password_hash") == stored
    outside.post("/remote", data={"csrf_token": token, "action": "off"})
    assert remote.enabled(conn)

    # PC에서 비밀번호를 바꾸면 밖의 로그인이 풀림
    set_password(client, "new-pass-5678")
    assert outside.get("/").status_code == 302
    login(outside, "new-pass-5678")
    assert outside.get("/").status_code == 200
    token = token_of(outside.get("/").get_data(as_text=True))
    outside.post("/logout", data={"csrf_token": token})
    assert outside.get("/").status_code == 302


def test_cloudflare_header_on_local_host_is_still_remote(client, app):
    set_password(client)
    c = app.test_client()
    r = c.get("/", base_url="http://127.0.0.1:5000", headers={"Cf-Ray": "abc123-ICN"})   # 터널이 Host 를 바꿔도
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    r = c.get("/", base_url="http://localhost:5000", headers={"X-Forwarded-For": "203.0.113.7"})
    assert r.status_code == 302 and "/login" in r.headers["Location"]


def test_lock_after_five_failures(client, outside, conn):
    set_password(client)
    for _ in range(remote.MAX_FAILS):
        login(outside, "wrong-pass")
    r = login(outside, "radio-1234")                                               # 맞아도 잠김
    assert r.status_code == 401 and "잠겨" in r.get_data(as_text=True)
    assert remote.locked_until(conn, datetime.now()) is not None
    db.set_setting(conn, "remote.locked_until", "2000-01-01 00:00:00")
    assert login(outside, "radio-1234").status_code == 302


def test_short_or_mismatched_password_rejected(client, conn):
    token = csrf(client, "/remote")
    r = client.post("/remote", data={"csrf_token": token, "action": "password", "password": "short",
                                     "password2": "short"}, follow_redirects=True)
    assert "8자 이상" in r.get_data(as_text=True) and not remote.enabled(conn)
    r = client.post("/remote", data={"csrf_token": token, "action": "password", "password": "long-enough-1",
                                     "password2": "long-enough-2"}, follow_redirects=True)
    assert "다릅니다" in r.get_data(as_text=True) and not remote.enabled(conn)


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, url: str = ""):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def open(self, url, timeout=None):
        self.calls.append(url)
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value() if callable(value) else FakeResponse(value, url)
        raise urllib.error.URLError("Name or service not known")


def test_probe_reports_each_state():
    ok = FakeOpener({"/health": b'{"app": "radio_helper", "pid": 1}'})
    assert remote.probe(DOMAIN, ok)[0] == "ok" and ok.calls == [f"https://{DOMAIN}/health"]
    access = FakeOpener({"/health": lambda: FakeResponse(b"<html>", "https://haetsal.cloudflareaccess.com/login")})
    assert remote.probe(DOMAIN, access)[0] == "access"
    down = FakeOpener({"/health": urllib.error.HTTPError("u", 530, "x", {}, None)})
    assert remote.probe(DOMAIN, down)[0] == "tunnel_down"
    assert remote.probe(DOMAIN, FakeOpener({}))[0] == "dns"


# ── 자동 업데이트 ─────────────────────────────────────────────────────
def make_zip(version="9.1", extra=None, prefix="edbdj-claude-funny-knuth-yzaoi1/"):
    files = {"VERSION": f"{version}\n", "radio_helper/__init__.py": "# init\n", "radio_helper/app.py": "# new app\n",
             "radio_helper/__main__.py": "# main\n", "radio_helper/new_mod.py": "# new\n",
             "start_windows.bat": "@echo off\r\nrem new\r\n", ".venv/evil.py": "x", "data/radio_helper.sqlite3": "x"}
    files.update(extra or {})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(prefix + name, text)
    return buf.getvalue()


@pytest.fixture
def program(tmp_path):
    root = tmp_path / "program"
    (root / "radio_helper").mkdir(parents=True)
    (root / ".venv").mkdir()
    (root / "VERSION").write_text("9.0\n", encoding="utf-8")
    (root / "radio_helper" / "__init__.py").write_text("# init\n", encoding="utf-8")
    (root / "radio_helper" / "app.py").write_text("# old app\n", encoding="utf-8")
    (root / "radio_helper" / "__main__.py").write_text("# main\n", encoding="utf-8")
    (root / "start_windows.bat").write_text("@echo off\r\nrem old\r\n", encoding="utf-8")
    (root / ".venv" / "keep.py").write_text("keep", encoding="utf-8")
    return root


def test_versions():
    assert updater.newer("0.36", "0.35") and updater.newer("0.40", "0.36") and updater.newer("1.0", "0.99")
    assert not updater.newer("0.36", "0.36") and not updater.newer("0.9", "0.10")
    assert __version__ == Path("VERSION").read_text(encoding="utf-8").strip()


def test_install_overwrites_program_files_only(data_dir, program):
    report = updater.install(make_zip(), program, expect="9.1")
    assert report["version"] == "9.1" and report["new_bat"]
    assert (program / "radio_helper" / "app.py").read_text() == "# new app\n"
    assert (program / "radio_helper" / "new_mod.py").exists() and (program / "VERSION").read_text() == "9.1\n"
    assert (program / ".venv" / "keep.py").read_text() == "keep" and not (program / ".venv" / "evil.py").exists()
    assert not (program / "data").exists()
    assert "rem old" in (program / "start_windows.bat").read_text()                 # 실행 중인 배치 파일은 그대로
    assert "rem new" in (program / "start_windows.new.bat").read_text()             # 다시 켤 때 바꿈
    rec = updater.last_record()
    assert rec["from"] == "9.0" and rec["to"] == "9.1" and not rec["confirmed"]
    assert set(rec["changed"]) == {"VERSION", "radio_helper/app.py", "radio_helper/new_mod.py"}
    assert rec["created"] == ["radio_helper/new_mod.py"]
    assert (Path(rec["backup"]) / "radio_helper" / "app.py").read_text() == "# old app\n"


def test_install_rejects_bad_archives(data_dir, program):
    with pytest.raises(updater.UpdateError, match="잘못된 경로"):
        updater.install(make_zip(extra={"../evil.py": "x"}), program)
    with pytest.raises(updater.UpdateError, match="다릅니다"):
        updater.install(make_zip("9.2"), program, expect="9.1")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("x/VERSION", "9.1")
    with pytest.raises(updater.UpdateError, match="없습니다"):
        updater.install(buf.getvalue(), program)
    with pytest.raises(updater.UpdateError, match="zip"):
        updater.install(b"not a zip", program)
    assert (program / "radio_helper" / "app.py").read_text() == "# old app\n"      # 아무것도 바꾸지 않음


def test_rollback_when_new_version_keeps_crashing(data_dir, program):
    updater.install(make_zip(), program, expect="9.1")
    assert rollback_update.main(program) == 0
    assert (program / "radio_helper" / "app.py").read_text() == "# old app\n"
    assert (program / "VERSION").read_text() == "9.0\n" and not (program / "radio_helper" / "new_mod.py").exists()
    assert not (program / "start_windows.new.bat").exists()
    assert "9.1" in updater.bad_versions()
    with pytest.raises(updater.UpdateError, match="되돌린 버전"):                  # 같은 버전은 다시 받지 않음
        updater.install(make_zip(), program, expect="9.1")
    assert rollback_update.main(program) == 1                                       # 두 번 되돌리지 않음


def test_confirmed_update_is_not_rolled_back(data_dir, program):
    updater.install(make_zip(), program, expect="9.1")
    updater.confirm_running("9.1")
    assert rollback_update.main(program) == 1
    assert (program / "radio_helper" / "app.py").read_text() == "# new app\n"


def test_auto_install_time(conn):
    now = datetime(2026, 10, 16, 4, 10)
    assert not updater.should_auto_install(conn, now, current="0.36")              # 아직 확인 안 함
    db.set_setting(conn, "update.latest", "0.37")
    assert updater.should_auto_install(conn, now, current="0.36")
    assert not updater.should_auto_install(conn, now.replace(hour=14), current="0.36")
    assert not updater.should_auto_install(conn, now, current="0.37")
    db.set_setting(conn, "update.tried_on", "2026-10-16")
    assert not updater.should_auto_install(conn, now, current="0.36")                # 하루 한 번
    db.set_setting(conn, "update.tried_on", "")
    db.set_setting(conn, "update.auto", "0")
    assert not updater.should_auto_install(conn, now, current="0.36")
    assert updater.due_check(conn, now)
    db.set_setting(conn, "update.checked_at", "2026-10-16 03:00:00")
    assert not updater.due_check(conn, now) and updater.due_check(conn, now.replace(hour=10))


def test_update_now_downloads_installs_and_restarts(conn, data_dir, program, monkeypatch):
    latest = updater.parse_version(__version__)
    newer = ".".join(map(str, (*latest[:-1], latest[-1] + 1)))
    opener = FakeOpener({"/VERSION": newer.encode(), "codeload.github.com": make_zip(newer)})
    restarted = []
    monkeypatch.setattr(updater.threading, "Timer", lambda s, fn: type("T", (), {"start": lambda self: fn()})())
    report = updater.update_now(conn, program, opener, restart=lambda: restarted.append(1))
    assert report["version"] == newer and restarted == [1]
    assert opener.calls[0] == f"https://raw.githubusercontent.com/{updater.REPO}/{updater.BRANCH}/VERSION"
    assert opener.calls[1] == f"https://codeload.github.com/{updater.REPO}/zip/refs/heads/{updater.BRANCH}"
    assert db.get_setting(conn, "quizbot.stop") == "1"                              # 실행기를 멈추고 다시 켬
    assert (program / updater.RESTART_FLAG).read_text() == newer                     # 배치 파일이 업데이트로 알아봄
    with pytest.raises(updater.UpdateError, match="최신"):
        updater.update_now(conn, program, FakeOpener({"/VERSION": __version__.encode()}), restart=lambda: None)


def test_update_screen_and_buttons(client, outside, conn, app, monkeypatch):
    started = []
    app.config["START_UPDATE"] = lambda: started.append(1)
    monkeypatch.setattr(updater, "fetch_latest", lambda c, opener=None: db.set_setting(c, "update.latest", "99.0") or "99.0")
    page = client.get("/remote").get_data(as_text=True)
    assert f"지금 버전</th><td>{__version__}" in page and "처음 한 번 설정" in page and "localhost:5000" in page
    token = csrf(client, "/remote")
    r = client.post("/remote", data={"csrf_token": token, "action": "check"}, follow_redirects=True)
    assert "새 버전 99.0" in r.get_data(as_text=True) and "지금 업데이트 (99.0)" in r.get_data(as_text=True)
    client.post("/remote", data={"csrf_token": token, "action": "auto", "hour": "5"})
    assert db.get_setting(conn, "update.auto") == "0" and db.get_setting(conn, "update.hour") == "5"

    # 밖에서도 업데이트를 누를 수 있음 (로그인 뒤)
    set_password(client)
    login(outside, "radio-1234")
    token = token_of(outside.get("/remote").get_data(as_text=True))
    r = outside.post("/remote", data={"csrf_token": token, "action": "update"}, follow_redirects=True)
    assert started == [1] and "업데이트를 시작했습니다" in r.get_data(as_text=True)
    r = outside.post("/remote", data={"csrf_token": token, "action": "update"}, follow_redirects=True)
    assert started == [1] and "이미 진행 중" in r.get_data(as_text=True)          # 두 번 시작하지 않음


def test_windows_launcher_handles_updates():
    raw = Path("start_windows.bat").read_bytes()
    text = raw.decode("utf-8")
    assert b"\r\n" in raw and "\n" not in raw.replace(b"\r\n", b"").decode("utf-8")
    # 코드 3 은 윈도우의 '경로를 찾을 수 없음'(Python 이 없을 때)과 같아서 표시 파일이 있을 때만 업데이트로 본다
    assert 'if "%RH_CODE%"=="3" if exist "update_restart.flag" goto updated' in text
    assert ":updated" in text and "goto setup" in text and 'del /q "update_restart.flag"' in text
    # Python 이 없으면 끝없이 되풀이하지 않고 찾기 → winget 으로 설치 → 안내하고 멈춤
    assert 'if not exist ".venv\\Scripts\\python.exe" goto nopython' in text
    assert "call :bootpython" in text and "call :installpython" in text and "Python.Python.3.12" in text
    assert ":nopython" in text and "python.org/downloads" in text
    assert text.index("\r\n:end\r\n") < text.index("\r\n:choosepython\r\n") and "pause\r\nexit /b" in text   # 함수로 흘러가지 않음
    assert 'if exist "start_windows.new.bat"' in text and '--after-update' in text
    assert "rollback_update.py" in text and "if defined RH_UPDATED if %RH_TRIES% GEQ 3 goto rollback" in text
    assert json.loads(json.dumps(updater.SKIP_TOP, default=list))                    # .venv·data 는 건드리지 않음
    assert {".venv", "data", ".git"} <= updater.SKIP_TOP
