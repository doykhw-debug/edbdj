@echo off
chcp 65001 >nul
cd /d "%~dp0"
set RH_ARGS=
set RH_UPDATED=
if /i "%~1"=="--after-update" (
  set RH_ARGS=--no-browser --restarted
  set RH_UPDATED=1
)
:setup
rem An update brought a new start_windows.bat: swap it in and run the new one in a new window.
if exist "start_windows.new.bat" (
  move /y "start_windows.new.bat" "start_windows.bat" >nul
  start "" "%~f0" --after-update
  exit
)
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
rem Code 3 = a new version was installed: check packages and start again (listening continues).
set RH_TRIES=0
:run
".venv\Scripts\python.exe" -m radio_helper %RH_ARGS%
set RH_CODE=%errorlevel%
if "%RH_CODE%"=="0" goto end
if "%RH_CODE%"=="2" goto end
if "%RH_CODE%"=="3" goto updated
set /a RH_TRIES+=1
if defined RH_UPDATED if %RH_TRIES% GEQ 3 goto rollback
if %RH_TRIES% GEQ 20 goto toomany
echo.
echo [restart] The helper stopped unexpectedly, code %RH_CODE%. Restarting in 5 seconds... Close this window to quit.
timeout /t 5 /nobreak >nul
set RH_ARGS=--no-browser --restarted
goto run
:updated
echo.
echo [update] A new version was installed. Checking packages and starting again...
set RH_ARGS=--no-browser --restarted
set RH_UPDATED=1
goto setup
:rollback
echo.
echo [update] The new version keeps stopping. Going back to the previous version...
".venv\Scripts\python.exe" rollback_update.py
set RH_UPDATED=
set RH_ARGS=--no-browser --restarted
goto setup
:toomany
echo [restart] It stopped too many times. Please send a screenshot of this window.
:end
pause
