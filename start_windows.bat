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
where nvidia-smi >nul 2>nul
if %errorlevel%==0 (
  echo [setup] NVIDIA graphics card found - checking GPU speech libraries, about 1GB the first time...
  ".venv\Scripts\python.exe" -m pip install -q -r requirements-gpu.txt
)
rem Run the helper. If it stops unexpectedly (not Ctrl+C), start it again automatically.
set RH_ARGS=
set RH_TRIES=0
:run
".venv\Scripts\python.exe" -m radio_helper %RH_ARGS%
set RH_CODE=%errorlevel%
if "%RH_CODE%"=="0" goto end
if "%RH_CODE%"=="2" goto end
set /a RH_TRIES+=1
if %RH_TRIES% GEQ 20 goto toomany
echo.
echo [restart] The helper stopped unexpectedly, code %RH_CODE%. Restarting in 5 seconds... Close this window to quit.
timeout /t 5 /nobreak >nul
set RH_ARGS=--no-browser --restarted
goto run
:toomany
echo [restart] It stopped too many times. Please send a screenshot of this window.
:end
pause
