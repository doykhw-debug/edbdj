"""start_windows.bat 의 패키지 확인 (setup_packages.py) — 퀴즈 부품 하나가 실패해도 관리 화면은 켜지고,
너무 새 Python 이면 3.12/3.11 로 .venv 를 다시 만들게 알린다 (0.40)."""

import re
import sys
from pathlib import Path

import pytest

import rollback_update
import setup_packages as sp

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def program(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask>=3.0\nplaywright>=1.45\n", encoding="utf-8")
    (tmp_path / "requirements-quiz.txt").write_text(
        '# 퀴즈\nanthropic>=1.11\nfaster-whisper>=1.2\nnumpy>=1.26  # 숫자\n\n'
        'PyAudioWPatch>=0.2.12; sys_platform == "win32"\n', encoding="utf-8")
    (tmp_path / "requirements-gpu.txt").write_text("nvidia-cublas-cu12\n", encoding="utf-8")
    return tmp_path


class Pip:
    """pip 대신: fail 에 든 글자가 명령에 있으면 실패. 부른 명령을 모아 둔다."""

    def __init__(self, fail=()):
        self.fail, self.calls = fail, []

    def __call__(self, cmd):
        self.calls.append(cmd[6:])            # python -m pip install -q --disable-pip-version-check 뒤
        joined = " ".join(cmd)
        return type("R", (), {"returncode": 1 if any(f in joined for f in self.fail) else 0})()


def finder(absent=()):
    return lambda mod: None if mod in absent else object()


def test_requirement_lines_skip_comments(program):
    assert sp.requirement_lines(program / "requirements-quiz.txt") == [
        "anthropic>=1.11", "faster-whisper>=1.2", "numpy>=1.26", 'PyAudioWPatch>=0.2.12; sys_platform == "win32"']


def test_all_installed(program):
    pip = Pip()
    assert sp.main(program, run=pip, find=finder(), nvidia=False) == sp.OK
    assert [c[-1].split("requirements")[-1] for c in pip.calls] == [".txt", "-quiz.txt"]   # 관리 화면 먼저, 퀴즈 따로


def test_one_quiz_package_fails_admin_still_starts(program, monkeypatch, capsys):
    """화면에서 본 문제: faster-whisper 하나 때문에 한꺼번에 설치가 실패해 Flask 까지 빠졌다."""
    monkeypatch.setattr(sp, "supported", lambda version=None: True)
    pip = Pip(fail=("requirements-quiz.txt", "faster-whisper"))
    code = sp.main(program, run=pip, find=finder({"faster_whisper"}), nvidia=False)
    assert code == sp.QUIZ_MISSING
    tried = [c[0] for c in pip.calls[2:]]
    assert tried == ["anthropic>=1.11", "faster-whisper>=1.2", "numpy>=1.26", 'PyAudioWPatch>=0.2.12; sys_platform == "win32"']
    out = capsys.readouterr().out
    assert "faster-whisper>=1.2" in out and "관리 화면은 그대로 켭니다" in out


def test_too_new_python_asks_for_rebuild(program, monkeypatch, capsys):
    monkeypatch.setattr(sp, "supported", lambda version=None: False)
    pip = Pip(fail=("requirements-quiz.txt", "faster-whisper"))
    assert sp.main(program, run=pip, find=finder({"faster_whisper"}), nvidia=False) == sp.QUIZ_MISSING_PYTHON
    assert "3.12 또는 3.11" in capsys.readouterr().out


def test_core_missing_stops_instead_of_restart_loop(program, monkeypatch):
    monkeypatch.setattr(sp, "supported", lambda version=None: True)
    pip = Pip(fail=("requirements",))
    assert sp.main(program, run=pip, find=finder({"flask", "werkzeug"}), nvidia=False) == sp.CORE_MISSING
    monkeypatch.setattr(sp, "supported", lambda version=None: False)
    assert sp.main(program, run=pip, find=finder({"flask", "werkzeug"}), nvidia=False) == sp.CORE_MISSING_PYTHON


def test_failed_pip_but_already_installed_still_runs(program):
    """업데이트 뒤 인터넷이 잠깐 끊겨 설치가 실패해도, 이미 설치돼 있으면 그대로 켠다."""
    pip = Pip(fail=("install",))
    assert sp.main(program, run=pip, find=finder(), nvidia=False) == sp.OK


def test_gpu_parts_only_with_nvidia(program):
    pip = Pip(fail=("requirements-gpu.txt",))
    assert sp.main(program, run=pip, find=finder(), nvidia=True) == sp.OK       # GPU 실패는 CPU 로 돈다
    assert any("requirements-gpu.txt" in c[-1] for c in pip.calls)
    pip = Pip()
    sp.main(program, run=pip, find=finder(), nvidia=False)
    assert not any("requirements-gpu.txt" in c[-1] for c in pip.calls)


def test_supported_versions():
    assert sp.supported((3, 11)) and sp.supported((3, 12)) and sp.supported((3, 14))
    assert not sp.supported((3, 15)) and not sp.supported((3, 9))
    assert sp.supported(sys.version_info)                                        # 이 테스트를 돌리는 Python


def test_real_requirement_files_parse():
    for name in ("requirements.txt", "requirements-quiz.txt", "requirements-gpu.txt"):
        lines = sp.requirement_lines(ROOT / name)
        assert lines and all("#" not in line for line in lines)


def test_launcher_uses_setup_packages_and_prefers_older_python():
    text = (ROOT / "start_windows.bat").read_bytes().decode("ascii")
    assert '".venv\\Scripts\\python.exe" setup_packages.py' in text
    assert 'if "%RH_SETUP%"=="2" goto nopackages' in text                        # 끝없이 다시 켜지 않음
    assert 'if "%RH_SETUP%"=="3" if not defined RH_REBUILT goto newpython' in text
    assert "-m venv --clear .venv" in text                                       # 3.12/3.11 로 다시 만들기
    # 버전은 find_python.py 가 실제로 실행해 확인하고 고른다 (py 의 종료 코드를 믿지 않음 — 0.41 까지의 문제)
    assert "find_python.py --install --out .python_choice" in text and 'set /p RH_PY=<".python_choice"' in text
    assert "open('.pyok','w')" in text and 'if exist ".pyok"' in text      # 파이썬이 실제로 돌았는지 파일로 확인
    assert 'if exist "rebuild_venv.flag"' in text                          # 관리 화면이 고치기를 요청함
    assert "Stop-Process" in text and text.index("Stop-Process") < text.index("-m venv --clear .venv")
    assert '<nul >nul 2>nul' in text                                             # 묻는 화면에서 멈추지 않음
    assert 'if not exist "setup_packages.py" goto oldpackages' in text
    # 모든 goto/call 대상이 있는 이름표인지
    labels = set(re.findall(r"^:(\w+)", text, re.M))
    targets = set(re.findall(r"(?:goto|call) :?(\w+)", text, re.I)) - {"eof"}
    assert targets <= labels, targets - labels
    # 경로를 변수 안에 따옴표로 겹쳐 넣지 않는다 (사용자 폴더 이름에 괄호가 있어도 안전)
    assert 'set "RH_PY="' not in text


def test_rollback_keeps_launcher_helpers(tmp_path, monkeypatch):
    """되돌려도 새 start_windows.bat 은 그대로라, 그 배치 파일이 부르는 setup_packages.py 는 남긴다."""
    data = tmp_path / "data"
    (data / "updates").mkdir(parents=True)
    monkeypatch.setenv("RADIO_HELPER_DATA_DIR", str(data))
    program = tmp_path / "program"
    program.mkdir()
    (program / "setup_packages.py").write_text("# new\n", encoding="utf-8")
    (program / "new_mod.py").write_text("# new\n", encoding="utf-8")
    (data / "updates" / "last.json").write_text(
        '{"from": "0.39", "to": "0.40", "backup": "", "changed": [], "created": ["setup_packages.py", "new_mod.py"]}',
        encoding="utf-8")
    assert rollback_update.main(program) == 0
    assert (program / "setup_packages.py").exists() and not (program / "new_mod.py").exists()
