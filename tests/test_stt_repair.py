"""음성 인식 부품(faster-whisper)이 없을 때 (0.42): Python 3.12 를 실제로 확인해 고르고, 관리 화면이 스스로 고친다."""

import sys
from datetime import datetime
from pathlib import Path

import pytest

import find_python as fp
from radio_helper import db, repair, updater

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 19, 9, 0)


# ── find_python.py ────────────────────────────────────────────────────
def fake_probe(table):
    """명령 → (경로, 버전). 없는 것은 None (예: py -3.12 가 '성공'만 하고 아무것도 실행하지 않음)."""
    return lambda cmd: table.get(" ".join(cmd))


def test_py_manager_false_success_is_not_trusted(tmp_path):
    exe311 = tmp_path / "Python311" / "python.exe"
    exe311.parent.mkdir()
    exe311.write_text("")
    probe = fake_probe({f"{exe311}": (str(exe311), "3.11"), "py -3.15": ("C:/x/python.exe", "3.15")})
    # py -3.12 는 실행되지 않음(None) → 실제로 돌아가는 3.11 을 고른다
    assert fp.find(probe, folders=[exe311], registry=[], has_py=True) == str(exe311)


def test_preference_order(tmp_path):
    probe = fake_probe({"py -3.13": ("C:/p313/python.exe", "3.13"), "py -3.12": ("C:/p312/python.exe", "3.12"),
                        "py -3.11": ("C:/p311/python.exe", "3.11")})
    assert fp.find(probe, folders=[], registry=[], has_py=True) == "C:/p312/python.exe"
    probe = fake_probe({"py -3.12": ("C:/wrong/python.exe", "3.15")})   # 버전이 다르면 버림
    assert fp.find(probe, folders=[], registry=[], has_py=True) is None


def test_main_installs_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(fp.sys, "platform", "win32")
    out = tmp_path / ".python_choice"
    state = {"installed": False}
    tried = []

    def installer(name, works):
        def run():
            tried.append(name)
            state["installed"] = state["installed"] or works
        return run

    finder = lambda: "C:/Py312/python.exe" if state["installed"] else None   # noqa: E731
    code = fp.main(["--out", str(out), "--install"], finder=finder,
                   installers=[installer("py install", False), installer("winget", True), installer("web", True)])
    assert code == 0 and out.read_text() == "C:/Py312/python.exe"
    assert tried == ["py install", "winget"]                             # 되면 거기서 멈춤
    assert fp.main(["--out", str(out)], finder=lambda: None) == 1 and not out.exists()


def test_probe_really_runs_python():
    assert fp.probe([sys.executable])[1] == "%d.%d" % sys.version_info[:2]
    assert fp.probe(["no-such-python-here"]) is None


def test_installers_use_py_install_then_winget_then_python_org(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(fp.shutil, "which", lambda name: f"C:/bin/{name}.exe")
    fp.install_with_pymanager(run=lambda cmd, t: calls.append(cmd) or True)
    fp.install_with_winget(run=lambda cmd, t: calls.append(cmd) or True)
    assert calls[0] == ["py", "install", "3.12"]
    assert calls[1][:5] == ["winget", "install", "-e", "--id", "Python.Python.3.12"] and "--scope" in calls[1]

    class Opener:
        def open(self, url, timeout):
            import io
            assert url == fp.INSTALLER_URL and url.startswith("https://www.python.org/")
            return io.BytesIO(b"MZ fake installer")

    monkeypatch.setattr(fp.tempfile, "gettempdir", lambda: str(tmp_path))
    ran = []
    assert fp.install_from_python_org(run=lambda cmd, t: ran.append(cmd) or True, opener=Opener())
    assert "/quiet" in ran[0] and "InstallAllUsers=0" in ran[0] and not (tmp_path / Path(fp.INSTALLER_URL).name).exists()


# ── 관리 화면이 스스로 고치기 ─────────────────────────────────────────
@pytest.fixture
def missing():
    return lambda mod: None


@pytest.fixture
def listening(conn):
    db.set_setting(conn, "live.active", "1")
    return conn


def test_auto_installs_with_pip_when_python_fits(listening, missing, monkeypatch, tmp_path):
    monkeypatch.setattr(repair, "python_ok", lambda version=None: True)
    started = []
    popen = lambda cmd, **kw: started.append(cmd)   # noqa: E731
    assert repair.auto(listening, NOW, root=tmp_path, find=missing, popen=popen) == "pip"
    assert started[0][1:4] == ["-m", "pip", "install"] and started[0][-1].endswith("requirements-quiz.txt")
    assert repair.auto(listening, NOW, root=tmp_path, find=missing, popen=popen) is None   # 6시간에 한 번
    assert len(started) == 1


def test_auto_rebuilds_once_a_day_when_python_too_new(listening, missing, monkeypatch, tmp_path):
    monkeypatch.setattr(repair, "python_ok", lambda version=None: False)
    monkeypatch.setattr(updater, "stop_helpers", lambda conn, wait_s=20: None)
    restarted = []
    monkeypatch.delenv("RADIO_HELPER_LAUNCHER", raising=False)
    assert repair.auto(listening, NOW, root=tmp_path, find=missing) is None          # start_windows.bat 이 아니면 못 함
    monkeypatch.setenv("RADIO_HELPER_LAUNCHER", "bat")
    assert repair.auto(listening, NOW, root=tmp_path, find=missing, restart=lambda: restarted.append(1)) == "rebuild"
    assert (tmp_path / repair.REBUILD_FLAG).exists() and (tmp_path / updater.RESTART_FLAG).exists()
    assert repair.auto(listening, NOW, root=tmp_path, find=missing) is None          # 같은 날 다시 안 함 (되풀이 방지)
    assert repair.auto(listening, NOW.replace(day=20), root=tmp_path, find=missing,
                       restart=lambda: None) == "rebuild"


def test_auto_does_nothing_when_fine_or_not_listening(conn, missing, monkeypatch, tmp_path):
    monkeypatch.setattr(repair, "python_ok", lambda version=None: True)
    assert repair.auto(conn, NOW, root=tmp_path, find=missing) is None               # 듣지 않음
    db.set_setting(conn, "live.active", "1")
    assert repair.auto(conn, NOW, root=tmp_path, find=lambda mod: object()) is None  # 설치돼 있음
    monkeypatch.setenv("RADIO_HELPER_SERVER", "1")
    assert repair.auto(conn, NOW, root=tmp_path, find=missing) is None               # 서버는 듣지 않음


def test_home_shows_fix_button_and_it_works(client, conn, monkeypatch, tmp_path):
    from tests.conftest import csrf

    monkeypatch.setattr(repair, "stt_installed", lambda find=None: False)
    monkeypatch.setattr(repair, "python_ok", lambda version=None: False)
    monkeypatch.setattr(repair, "python_version", lambda: "3.15.0")
    page = client.get("/").get_data(as_text=True)
    assert "지금 고치기" in page and "3.15.0" in page
    asked = []
    monkeypatch.setattr(repair, "repair_now", lambda c: asked.append(1) or "Python 3.12 를 설치하고 다시 켭니다")
    r = client.post("/repair/stt", data={"csrf_token": csrf(client)}, follow_redirects=True)
    assert asked and "Python 3.12 를 설치하고" in r.get_data(as_text=True)
    monkeypatch.setattr(repair, "stt_installed", lambda find=None: True)
    assert "지금 고치기" not in client.get("/").get_data(as_text=True)


def test_repair_now_without_launcher_explains(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(repair, "python_ok", lambda version=None: False)
    monkeypatch.delenv("RADIO_HELPER_LAUNCHER", raising=False)
    assert "start_windows.bat" in repair.repair_now(conn, root=tmp_path)
    assert not (tmp_path / repair.REBUILD_FLAG).exists()


def test_runner_explains_missing_speech_parts(conn):
    from radio_helper.quizbot import runner as runner_mod

    class R:
        conn = None
        states = []

        def state(self, text):
            self.states.append(text)

        class deps:
            sleep = staticmethod(lambda s: None)
            now = staticmethod(datetime.now)
            log = staticmethod(lambda text: None)

    r = R()
    r.conn = conn

    def boom():
        raise ModuleNotFoundError("No module named 'faster_whisper'")

    runner_mod.Runner._guarded(r, boom)
    assert "음성 인식 부품(faster-whisper)" in r.states[-1] and "지금 고치기" in r.states[-1]
