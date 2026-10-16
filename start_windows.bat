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
if exist ".venv\Scripts\python.exe" goto packages
echo [setup] First run: creating .venv ...
py -3 -m venv .venv >nul 2>nul
if not exist ".venv\Scripts\python.exe" python -m venv .venv >nul 2>nul
if not exist ".venv\Scripts\python.exe" call :findpython
if not exist ".venv\Scripts\python.exe" call :installpython
if not exist ".venv\Scripts\python.exe" goto nopython
".venv\Scripts\python.exe" -m pip install --upgrade pip
:packages
echo [setup] Checking packages...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt -r requirements-quiz.txt
where nvidia-smi >nul 2>nul
if %errorlevel%==0 (
  echo [setup] NVIDIA graphics card found - checking GPU speech libraries, about 1GB the first time...
  ".venv\Scripts\python.exe" -m pip install -q -r requirements-gpu.txt
)
rem Run the helper. If it stops unexpectedly (not Ctrl+C), start it again automatically.
rem Code 3 with update_restart.flag = a new version was installed: check packages and start again (listening continues).
rem (Windows also uses code 3 for "path not found", so the flag file is required.)
set RH_TRIES=0
:run
if not exist ".venv\Scripts\python.exe" goto nopython
".venv\Scripts\python.exe" -m radio_helper %RH_ARGS%
set RH_CODE=%errorlevel%
if "%RH_CODE%"=="0" goto end
if "%RH_CODE%"=="2" goto end
if "%RH_CODE%"=="3" if exist "update_restart.flag" goto updated
set /a RH_TRIES+=1
if defined RH_UPDATED if %RH_TRIES% GEQ 3 goto rollback
if %RH_TRIES% GEQ 20 goto toomany
echo.
echo [restart] The helper stopped unexpectedly, code %RH_CODE%. Restarting in 5 seconds... Close this window to quit.
timeout /t 5 /nobreak >nul
set RH_ARGS=--no-browser --restarted
goto run
:updated
del /q "update_restart.flag" >nul 2>nul
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
:nopython
echo.
echo [setup] Python 3.11 or newer is needed but was not found on this PC.
echo   1. Install it from https://www.python.org/downloads/
echo      On the first install screen, CHECK "Add python.exe to PATH".
echo   2. Close this window and double-click start_windows.bat again.
start "" "https://www.python.org/downloads/"
goto end
:toomany
echo [restart] It stopped too many times. Please send a screenshot of this window.
:end
pause
exit /b

:findpython
rem Python is installed but not on PATH (or was just installed by winget): look in the usual folders.
for %%P in ("%LOCALAPPDATA%\Programs\Python\Python311\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%ProgramFiles%\Python311\python.exe" "%ProgramFiles%\Python312\python.exe" "%ProgramFiles%\Python313\python.exe") do (
  if not exist ".venv\Scripts\python.exe" if exist %%P %%P -m venv .venv >nul 2>nul
)
goto :eof

:installpython
where winget >nul 2>nul
if errorlevel 1 goto :eof
echo [setup] Python is not installed. Installing Python 3.11 with winget - a few minutes. Allow it if Windows asks.
winget install -e --id Python.Python.3.11 --scope user --accept-package-agreements --accept-source-agreements
call :findpython
goto :eof
