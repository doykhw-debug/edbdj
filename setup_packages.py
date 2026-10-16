"""start_windows.bat 이 켤 때마다 부른다: 필요한 패키지를 설치하고, 무엇이 빠졌는지 종료 코드로 알려 준다.

radio_helper 패키지를 쓰지 않는다 (패키지가 아직 없을 때 도는 것이라 표준 라이브러리만 쓴다).

- 기본(관리 화면: Flask·Playwright)과 퀴즈 자동(음성 인식 등)을 따로 설치한다. 퀴즈 쪽 하나가 실패해도
  관리 화면 패키지는 설치되고, 퀴즈 쪽은 하나씩 다시 설치해 되는 것만이라도 넣는다.
- 인터넷이 잠깐 끊겨 설치가 실패해도, 이미 설치돼 있으면 그대로 켠다 (설치 명령의 성공이 아니라 실제로 있는지를 본다).
- 너무 새 Python(예: 3.15)에는 음성 인식(faster-whisper)이 아직 없다. 그때는 종료 코드 3·4 로 알려
  start_windows.bat 이 Python 3.12/3.11 로 .venv 를 다시 만든다.

종료 코드: 0 모두 있음 · 1 퀴즈 부품 일부 없음(관리 화면은 됨) · 2 관리 화면 패키지 없음(켤 수 없음)
          3 = 1 이면서 Python 버전이 맞지 않음 · 4 = 2 이면서 Python 버전이 맞지 않음
"""

import importlib
import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

OK, QUIZ_MISSING, CORE_MISSING, QUIZ_MISSING_PYTHON, CORE_MISSING_PYTHON = 0, 1, 2, 3, 4
SUPPORTED = ((3, 10), (3, 14))   # 퀴즈 부품까지 모두 설치되는 Python (처음 .venv 는 3.12/3.11 로 만든다)
CORE = {"flask": "Flask", "werkzeug": "Flask", "playwright": "Playwright"}
QUIZ = {"anthropic": "anthropic", "faster_whisper": "faster-whisper (음성 인식)", "numpy": "numpy",
        "keyring": "keyring", "PIL": "Pillow"}
QUIZ_WINDOWS = {"pyaudiowpatch": "PyAudioWPatch (PC 소리 녹음)", "pywinauto": "pywinauto (고릴라 화면 조작)"}


def supported(version=None) -> bool:
    v = tuple((version or sys.version_info)[:2])
    return SUPPORTED[0] <= v <= SUPPORTED[1]


def pip(args, run=subprocess.run) -> bool:
    return run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", *args]).returncode == 0


def requirement_lines(path: Path) -> list[str]:
    """requirements 파일의 한 줄 한 줄 (주석·빈 줄 빼고). 하나씩 다시 설치할 때 쓴다."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = re.sub(r"(^|\s)#.*$", "", line).strip()
        if line:
            out.append(line)
    return out


def missing(modules: dict, find=importlib.util.find_spec) -> list[str]:
    importlib.invalidate_caches()   # 방금 설치한 것도 보이게
    out = []
    for mod, label in modules.items():
        try:
            found = find(mod) is not None
        except (ImportError, ValueError):
            found = False
        if not found and label not in out:
            out.append(label)
    return out


def has_nvidia() -> bool:
    return sys.platform == "win32" and shutil.which("nvidia-smi") is not None


def main(root=None, run=subprocess.run, find=importlib.util.find_spec, nvidia=None) -> int:
    root = Path(root or Path(__file__).resolve().parent)
    version = "%d.%d.%d" % sys.version_info[:3]
    if not pip(["-r", str(root / "requirements.txt")], run):
        print("[setup] 관리 화면 패키지(Flask 등) 설치가 실패했습니다. 위의 ERROR 줄을 보세요.")
    quiz_file = root / "requirements-quiz.txt"
    if quiz_file.exists() and not pip(["-r", str(quiz_file)], run):
        print("[setup] 퀴즈 자동 패키지를 한꺼번에 설치하지 못해 하나씩 다시 설치합니다...")
        failed = [line for line in requirement_lines(quiz_file) if not pip([line], run)]
        if failed:
            print("[setup] 설치하지 못한 것: " + ", ".join(failed))
    if nvidia is None:
        nvidia = has_nvidia()
    if nvidia and (root / "requirements-gpu.txt").exists():
        print("[setup] NVIDIA 그래픽카드가 있어 GPU 음성 인식 부품을 확인합니다 (처음엔 약 1GB)...")
        if not pip(["-r", str(root / "requirements-gpu.txt")], run):
            print("[setup] GPU 부품을 설치하지 못했습니다. 음성 인식은 CPU로 합니다 (조금 느림).")

    core = missing(CORE, find)
    quiz = missing({**QUIZ, **(QUIZ_WINDOWS if sys.platform == "win32" else {})}, find)
    fits = supported()
    if core:
        print(f"[setup] 관리 화면을 켤 수 없습니다 — 없는 것: {', '.join(core)} (Python {version})")
        return CORE_MISSING if fits else CORE_MISSING_PYTHON
    if quiz:
        print(f"[setup] 퀴즈 자동에 필요한 것이 빠졌습니다: {', '.join(quiz)} (Python {version})")
        if not fits:
            print(f"[setup] Python {version} 은(는) 너무 새 버전이라 아직 음성 인식 부품이 없습니다. "
                  f"Python 3.12 또는 3.11 이 필요합니다.")
            return QUIZ_MISSING_PYTHON
        print("[setup] 관리 화면은 그대로 켭니다. 인터넷을 확인하고 다음에 켤 때 다시 설치합니다.")
        return QUIZ_MISSING
    return OK


if __name__ == "__main__":
    sys.exit(main())
