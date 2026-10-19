"""서버 운영 모드 (Oracle 서버 · radio.haetsallab.com · 127.0.0.1:5002) 와 배포 파일."""

import json
import re
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from radio_helper import __main__ as launcher
from radio_helper import autofill, db, remote, server
from radio_helper.quizbot import config as qconfig

ROOT = Path(__file__).resolve().parent.parent
DOMAIN = "radio.haetsallab.com"


def token_of(html: str) -> str:
    return re.search(r'name="csrf_token" value="([0-9a-f]+)"', html).group(1)


class At:
    def __init__(self, app, base: str, headers=None):
        self.c, self.base, self.headers = app.test_client(), base, headers or {}

    def get(self, path, **kw):
        return self.c.get(path, base_url=self.base, headers=self.headers, **kw)

    def post(self, path, **kw):
        return self.c.post(path, base_url=self.base, headers=self.headers, **kw)


@pytest.fixture
def server_env(monkeypatch, data_dir):
    monkeypatch.setenv(server.SERVER_ENV, "1")
    monkeypatch.delenv("PORT", raising=False)
    return data_dir


@pytest.fixture
def server_app(server_env):
    from radio_helper.app import create_app

    a = create_app()
    a.config["TESTING"] = True
    return a


def login(at, pw="server-pass-1"):
    page = at.get("/login")
    return at.post("/login", data={"csrf_token": token_of(page.get_data(as_text=True)), "password": pw, "next": "/"})


def set_server_password(pw="server-pass-1"):
    conn = db.connect()
    try:
        assert remote.set_password(conn, pw, pw) is None
    finally:
        conn.close()


# ── .env · 포트 ───────────────────────────────────────────────────────
def test_parse_env_reads_values_without_running_anything():
    text = ('# 주석\nexport A=1\nB = "two words"\nC=\'q\'\nD=x  # 끝 주석\nE=\n bad line\n1X=no\nF=a=b\n')
    assert server.parse_env(text) == {"A": "1", "B": "two words", "C": "q", "D": "x", "E": "", "F": "a=b"}


def test_load_env_keeps_existing_and_ignores_server_keys_on_windows(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("\ufeffANTHROPIC_API_KEY=from-file\r\nPORT=5002\r\nRADIO_HELPER_SERVER=1\r\n"
                   "RADIO_HELPER_DATA_DIR=/opt/radio/data\r\n", encoding="utf-8")
    for key in ("ANTHROPIC_API_KEY", "PORT", server.SERVER_ENV):
        monkeypatch.setenv(key, "x")      # 끝나면 원래대로 되돌리도록 기록해 두고 지운다
        monkeypatch.delenv(key)
    monkeypatch.setenv("RADIO_HELPER_DATA_DIR", "C:/pc-data")
    loaded = server.load_env(env, windows=True)          # PC 폴더에 서버용 .env 를 잘못 둬도
    assert sorted(loaded) == ["ANTHROPIC_API_KEY"]        # PORT=5002 를 따르면 밖에서 보기(5000번)가 끊김
    assert not server.is_server()                         # PC 가 서버 모드로 바뀌지 않는다
    assert "PORT" not in server.os.environ
    assert server.os.environ["RADIO_HELPER_DATA_DIR"] == "C:/pc-data"   # 이미 있는 값은 그대로
    assert server.load_env(env, windows=False) == ["PORT", server.SERVER_ENV]
    assert server.is_server()
    assert server.load_env(tmp_path / "없음") == []


def test_pc_env_file_may_still_set_port(tmp_path, monkeypatch):
    """서버용이 아닌 PC 의 .env 에 일부러 적은 PORT 는 그대로 따른다."""
    env = tmp_path / ".env"
    env.write_text("PORT=5050\nTZ=Asia/Seoul\n", encoding="utf-8")
    for key in ("PORT", "TZ"):
        monkeypatch.setenv(key, "x")
        monkeypatch.delenv(key)
    assert server.load_env(env, windows=True) == ["PORT", "TZ"] and server.port() == 5050


def test_port_defaults(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv(server.SERVER_ENV, raising=False)
    assert server.port() == 5000                          # PC 는 그대로 5000
    monkeypatch.setenv(server.SERVER_ENV, "1")
    assert server.port() == 5002                          # 서버 기본 5002
    monkeypatch.setenv("PORT", "5111")
    assert server.port() == 5111
    monkeypatch.setenv("PORT", "abc")
    assert server.port() == 5002
    assert server.BIND == "127.0.0.1"


# ── 서버 모드 관리 화면 ───────────────────────────────────────────────
def test_server_requires_login_even_from_127_0_0_1(server_app):
    assert server_app.config["SESSION_COOKIE_SECURE"] is True
    assert server_app.config["SERVER_PORT"] == 5002
    local = At(server_app, "https://127.0.0.1:5002")      # 같은 서버의 다른 프로그램도 127.0.0.1 로 온다
    assert local.get("/").status_code == 403              # 비밀번호 전: 아무도 못 연다
    assert "set-password" in local.get("/").get_data(as_text=True)
    assert local.get("/health").status_code == 200
    set_server_password()
    r = local.get("/experiences")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    # 첫 주소는 그 자리에서 로그인 화면 (curl -I http://127.0.0.1:5002 → 200), 관리 내용은 없음
    first = local.get("/")
    html = first.get_data(as_text=True)
    assert first.status_code == 200 and 'action="/login"' in html and 'name="password"' in html
    assert "서버 운영 모드" not in html and "사연" not in html
    assert local.c.head("/", base_url=local.base).status_code == 200
    assert login(local).status_code == 302
    page = local.get("/")
    assert page.status_code == 200 and "서버 운영 모드" in page.get_data(as_text=True)


def test_tunnel_login_and_pages(server_app):
    set_server_password()
    outside = At(server_app, f"https://{DOMAIN}", {"Cf-Connecting-Ip": "203.0.113.7", "Cf-Ray": "8a1b-ICN"})
    assert login(outside, "wrong-password").status_code == 401
    assert login(outside).status_code == 302
    for path in ("/", "/experiences", "/library", "/drafts", "/quizzes", "/stories", "/settings", "/remote",
                 "/quizbot", "/people"):
        assert outside.get(path).status_code == 200, path
    other = At(server_app, "https://evil.example.com")
    assert other.get("/").status_code == 403              # 다른 주소로 온 요청은 막는다


def test_pc_only_actions_are_refused_on_server(server_app):
    set_server_password()
    at = At(server_app, f"https://{DOMAIN}", {"Cf-Connecting-Ip": "203.0.113.7"})
    login(at)
    token = token_of(at.get("/").get_data(as_text=True))
    for path, data in (("/listen/start", {"channel": "SBS"}), ("/listen/skip", {}), ("/quizbot/start", {}),
                       ("/quizbot/tool", {"tool": "check"}), ("/listen/stt-test", {})):
        r = at.post(path, data={"csrf_token": token, **data})
        assert r.status_code == 302, path
    page = at.get("/").get_data(as_text=True)
    assert "윈도우 PC 프로그램에서만" in page
    conn = db.connect()
    try:
        assert qconfig.get(conn, "live.active") != "1"
    finally:
        conn.close()
    r = at.post("/remote", data={"csrf_token": token, "action": "password", "password": "x" * 9, "password2": "x" * 9},
                follow_redirects=True)
    assert "set-password" in r.get_data(as_text=True)
    r = at.post("/remote", data={"csrf_token": token, "action": "update"}, follow_redirects=True)
    assert "deploy.sh" in r.get_data(as_text=True)


def test_quizbot_tools_limited_on_server(server_env, monkeypatch):
    from radio_helper import app as app_module

    launched = []
    monkeypatch.setattr(app_module, "launch_module", lambda module, args, prefix: launched.append(args) or "x.log")
    with pytest.raises(server.PcOnly):
        app_module.launch_quizbot(["run"])
    assert app_module.launch_quizbot(["enrich"]) == "x.log"   # Claude API 만 쓰는 풀어 쓰기는 서버에서도
    assert launched == [["enrich"]]


def test_error_details_hidden_from_strangers(server_app):
    @server_app.get("/boom")
    def boom():
        raise RuntimeError("secret path /opt/radio/x")

    server_app.config["TESTING"] = False
    set_server_password()
    at = At(server_app, f"https://{DOMAIN}", {"Cf-Connecting-Ip": "203.0.113.7"})
    server_app.config["PROPAGATE_EXCEPTIONS"] = False
    login(at)
    r = at.get("/boom")
    assert r.status_code == 500 and "secret path" in r.get_data(as_text=True)   # 로그인한 본인은 자세히
    stranger = At(server_app, f"https://{DOMAIN}")
    from radio_helper.errors import error_page

    html = error_page(RuntimeError("secret path"), "GET", "/boom", detail=False)
    assert "secret path" not in html
    assert stranger.get("/boom").status_code == 302                           # 로그인 전에는 오류 화면까지 안 감


def test_browser_never_opens_window_on_server(monkeypatch):
    monkeypatch.delenv("RADIO_HELPER_HEADLESS", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv(server.SERVER_ENV, raising=False)
    assert not autofill.no_window() or autofill.sys.platform == "win32"
    monkeypatch.setenv(server.SERVER_ENV, "1")
    assert autofill.no_window()
    seen = {}

    class Chromium:
        def launch(self, **kw):
            seen.update(kw)
            return "browser"

    class P:
        chromium = Chromium()

    autofill.launch_browser(P(), headless=False)
    assert seen["headless"] is True


def test_run_server_uses_waitress_on_localhost(server_env, monkeypatch, capsys):
    conn = db.connect()
    db.init_db(conn)
    db.set_setting(conn, "live.active", "1")
    db.set_setting(conn, "live.ask", "1")
    conn.close()
    monkeypatch.setattr(launcher, "_can_bind", lambda port: True)
    calls = []
    launcher.run_server(5002, serve=lambda app, **kw: calls.append(kw))
    assert calls[0]["host"] == "127.0.0.1" and calls[0]["port"] == 5002
    out = capsys.readouterr().out
    assert "서버 운영 모드" in out and "set-password" in out
    conn = db.connect()
    try:
        assert db.get_setting(conn, "live.active") == "0" and db.get_setting(conn, "live.ask") == "0"
    finally:
        conn.close()


def test_run_server_stops_when_port_taken(server_env, monkeypatch):
    monkeypatch.setattr(launcher, "_can_bind", lambda port: False)
    with pytest.raises(SystemExit) as e:
        launcher.run_server(5002, serve=lambda app, **kw: None)
    assert e.value.code == launcher.EXIT_NO_PORT


def test_password_cli(data_dir):
    out = []
    answers = iter(["server-pass-1", "server-pass-1"])
    assert remote.cli(["set-password"], ask=lambda _p: next(answers), say=out.append) == 0
    assert remote.cli(["status"], say=out.append) == 0
    assert "REMOTE_LOGIN=on" in out
    answers = iter(["short", "short"])
    assert remote.cli(["set-password"], ask=lambda _p: next(answers), say=out.append) == 1
    assert remote.cli(["off"], say=out.append) == 0
    out.clear()
    remote.cli(["status"], say=out.append)
    assert "REMOTE_LOGIN=off" in out
    assert not any("server-pass-1" in line for line in out)


# ── 데이터 백업·옮기기 ───────────────────────────────────────────────
@pytest.fixture
def pc_data(tmp_path, monkeypatch):
    d = tmp_path / "pcdata"
    monkeypatch.setenv("RADIO_HELPER_DATA_DIR", str(d))
    conn = db.connect()
    db.init_db(conn)
    db.set_setting(conn, "remote.password_hash", remote.hash_password("pc-pass-123"))
    conn.close()
    for rel, text in {"browser_profile/Default/Cookies": "c", "browser_profile/Default/Cache/x": "c",
                      "session.key": "k", "evidence/2026/a.jpg": "i", "inspect/corner1_a.json": "{}",
                      "inspect/gorilla.png": "p", "logs/server.log": "l", "updates/last.json": "{}",
                      "radio_helper.sqlite3-wal": ""}.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    return d


def _backup_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("backup_data", ROOT / "deploy" / "backup_data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_server_package_leaves_out_login_cookies_and_keys(pc_data, tmp_path):
    bd = _backup_module()
    full = bd.pack(pc_data, tmp_path / "full.zip")
    names = set(zipfile.ZipFile(full["file"]).namelist())
    assert {"radio_helper.sqlite3", "session.key", "browser_profile/Default/Cookies", "logs/server.log"} <= names
    assert "browser_profile/Default/Cache/x" not in names and "radio_helper.sqlite3-wal" not in names
    pkg = bd.pack(pc_data, tmp_path / "server.zip", server_package=True)
    assert set(zipfile.ZipFile(pkg["file"]).namelist()) == {
        "radio_helper.sqlite3", "evidence/2026/a.jpg", "inspect/corner1_a.json"}
    target = tmp_path / "srv" / "data"
    bd.restore(Path(pkg["file"]), target)
    conn = db.connect(target / "radio_helper.sqlite3")
    try:
        assert remote.enabled(conn)                        # PC 에서 정한 원격 비밀번호도 함께
    finally:
        conn.close()
    again = bd.restore(Path(pkg["file"]), target)          # 두 번째: 예전 것은 옆에 보관
    assert again["previous"] and Path(again["previous"], "radio_helper.sqlite3").exists()
    tgz = bd.pack(target, tmp_path / "b.tar.gz")
    assert "radio_helper.sqlite3" in tarfile.open(tgz["file"]).getnames()


def test_restore_rejects_bad_paths(tmp_path):
    bd = _backup_module()
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("../evil.txt", "x")
    with pytest.raises(SystemExit):
        bd.restore(bad, tmp_path / "data")


def test_pc_prepare_fills_key_without_printing_it(pc_data, tmp_path, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("pc_prepare", ROOT / "deploy" / "pc_prepare.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    key = "sk-ant-api03-" + "a" * 40
    monkeypatch.setattr(mod, "pc_api_key", lambda: (key, "keyring"))
    env_file = tmp_path / ".env.server"
    assert mod.main(["--env", str(env_file), "--stage", str(tmp_path / "stage"),
                     "--backup-dir", str(tmp_path / "bk")]) == 0
    out = capsys.readouterr().out
    assert key not in out
    info = json.loads(out.strip().splitlines()[-1])
    assert info["has_key"] and info["key_source"] == "keyring" and Path(info["package"]).exists()
    text = env_file.read_text(encoding="utf-8")
    assert f"ANTHROPIC_API_KEY={key}" in text and "RADIO_HELPER_SERVER=1" in text and "\r" not in text
    assert mod.ensure_env(env_file, key_source=lambda: ("", "")) == {"has_key": True, "key_source": "file"}


# ── 배포 파일 ─────────────────────────────────────────────────────────
def test_systemd_unit():
    unit = (ROOT / "deploy" / "radio.service").read_text(encoding="utf-8")
    for line in ("User=ubuntu", "Restart=always", "WorkingDirectory=/opt/radio", "EnvironmentFile=/opt/radio/.env",
                 "ExecStart=/opt/radio/.venv/bin/python -m radio_helper --server", "WantedBy=multi-user.target",
                 "Environment=PORT=5002", "Environment=TZ=Asia/Seoul"):
        assert line in unit, line


def test_shell_scripts():
    for name in ("deploy.sh", "deploy/server_setup.sh"):
        path = ROOT / name
        text = path.read_text(encoding="utf-8")
        assert "\r" not in text and text.startswith("#!/usr/bin/env bash")
        assert subprocess.run(["bash", "-n", str(path)]).returncode == 0
        assert not re.search(r"\b(ufw|iptables|firewall-cmd|nft)\b", text)     # 방화벽은 건드리지 않는다
    deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
    order = ["backup_data.py", "git fetch", "pip install", "systemctl restart", "/health"]
    assert [deploy.index(s) for s in order] == sorted(deploy.index(s) for s in order)
    setup = (ROOT / "deploy" / "server_setup.sh").read_text(encoding="utf-8")
    assert "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU" in setup     # github.com 호스트 키 확인
    assert "systemctl enable" in setup and "-m venv .venv" in setup and "install -m 600" in setup


def test_windows_files():
    ps1 = (ROOT / "deploy" / "pc_deploy.ps1").read_bytes()
    assert ps1.startswith(b"\xef\xbb\xbf")                                     # 윈도우 PowerShell 5.1 한글
    text = ps1.decode("utf-8-sig")
    for line in text.splitlines():
        if line.lstrip().startswith("& ssh") or "(& ssh" in line:
            for cmd in re.findall(r"'([^']*)'", line):
                assert '"' not in cmd, line                                     # PS 5.1 따옴표 버그 피함
    assert "scp" in text and "server_setup.sh" in text and "set-password" in text
    bat = (ROOT / "deploy_to_server.bat").read_bytes()
    bat.decode("ascii")
    assert b"\r\n" in bat and b"pc_deploy.ps1" in bat


def test_secrets_stay_out_of_git():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for pattern in (".env", "data/", "*.sqlite3", "backups/", "!.env.example"):
        assert pattern in ignore, pattern
    example = server.parse_env((ROOT / ".env.example").read_text(encoding="utf-8"))
    assert example["ANTHROPIC_API_KEY"] == "" and example["PORT"] == "5002"
    assert example["RADIO_HELPER_DATA_DIR"] == "/opt/radio/data" and example["PUBLIC_URL"] == f"https://{DOMAIN}"
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert not [f for f in tracked if f == ".env" or f.endswith(".sqlite3") or f.startswith("data/")]
