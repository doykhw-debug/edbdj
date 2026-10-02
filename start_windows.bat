@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [setup] First run: creating .venv ...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
)
echo [setup] Checking packages...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt -r requirements-quiz.txt
".venv\Scripts\python.exe" -m radio_helper
pause
