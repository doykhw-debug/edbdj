@echo off
chcp 65001 >nul
cd /d "%~dp0"
set RH_ARGS=
set RH_UPDATED=
set RH_REBUILT=
rem The helper uses this to know it can restart through this window (e.g. to rebuild .venv).
set RADIO_HELPER_LAUNCHER=bat
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
rem Any Python that really runs is enough to start find_python.py, which picks Python 3.12/3.11
rem (the newest Python, e.g. 3.15, has no speech recognition parts yet) and installs it if needed.
call :bootpython
if not defined RH_BOOT call :installpython
if not defined RH_BOOT goto nopython
call :choosepython
if defined RH_PY goto makevenv
echo [setup] Using the Python that is here: the admin screen works, quiz listening may not.
set "RH_PY=%RH_BOOT%"
set "RH_PYV=%RH_BOOTV%"
:makevenv
"%RH_PY%" %RH_PYV% -m venv .venv
if not exist ".venv\Scripts\python.exe" goto nopython
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
:packages
echo [setup] Checking packages...
rem A rebuild was asked for by the helper (speech recognition parts missing because this Python is too new).
if exist "rebuild_venv.flag" (
  del /q "rebuild_venv.flag" >nul 2>nul
  set RH_REBUILT=
  goto newpython
)
rem setup_packages.py installs the admin screen and quiz parts separately and checks what is really there.
rem 0 all ok, 1 some quiz parts missing, 2 admin screen parts missing, 3/4 = 1/2 because this Python is too new.
if not exist "setup_packages.py" goto oldpackages
".venv\Scripts\python.exe" setup_packages.py
set RH_SETUP=%errorlevel%
if "%RH_SETUP%"=="3" if not defined RH_REBUILT goto newpython
if "%RH_SETUP%"=="4" if not defined RH_REBUILT goto newpython
if "%RH_SETUP%"=="4" goto nopackages
if "%RH_SETUP%"=="2" goto nopackages
goto runstart
:oldpackages
rem setup_packages.py is missing (an older version was put back): install the old way.
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
".venv\Scripts\python.exe" -m pip install -q -r requirements-quiz.txt
:runstart
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
echo [update] A new version was installed (or a repair was asked for). Checking packages and starting again...
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
:newpython
rem The .venv was made with a Python that is too new: make it again with Python 3.12/3.11 (installed if needed).
set RH_REBUILT=1
echo.
echo [setup] This Python is too new for the quiz speech recognition parts.
echo [setup] Making .venv again with Python 3.12 or 3.11 - a few minutes...
set "RH_BOOT=%CD%\.venv\Scripts\python.exe"
set RH_BOOTV=
if not exist "%RH_BOOT%" call :bootpython
if not defined RH_BOOT goto oldpythonmissing
call :choosepython
if not defined RH_PY goto oldpythonmissing
rem Close anything still running from .venv (e.g. the captions window) so it can be cleared.
powershell -NoProfile -Command "Get-Process | Where-Object { $_.Path -like (Join-Path (Get-Location) '.venv\*') } | Stop-Process -Force" >nul 2>nul
"%RH_PY%" -m venv --clear .venv
if not exist ".venv\Scripts\python.exe" goto setup
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
goto packages
:oldpythonmissing
echo [setup] Could not get Python 3.12 or 3.11.
echo   Install Python 3.12 from https://www.python.org/downloads/ - "Looking for a specific release?"
echo   then close this window and double-click start_windows.bat again.
if "%RH_SETUP%"=="4" goto nopackages
echo [setup] Starting anyway: the admin screen works, but quiz listening will not.
set RH_TRIES=0
goto run
:nopackages
echo.
echo [setup] The admin screen packages (Flask) could not be installed. See the red ERROR lines above.
echo   Check the internet connection, then close this window and double-click start_windows.bat again.
echo   If it happens again, send a screenshot of this window.
goto end
:nopython
echo.
echo [setup] Python 3.12 or 3.11 is needed but was not found on this PC.
echo   1. Install Python 3.12 from https://www.python.org/downloads/ - "Looking for a specific release?"
echo      On the first install screen, CHECK "Add python.exe to PATH".
echo   2. Close this window and double-click start_windows.bat again.
start "" "https://www.python.org/downloads/"
goto end
:toomany
echo [restart] It stopped too many times. Please send a screenshot of this window.
:end
pause
exit /b

:choosepython
rem find_python.py runs each candidate to check its real version (the new "py" install manager can
rem report success for a version that is not installed) and installs Python 3.12 if none fits.
rem Sets RH_PY to the full path of python.exe, or leaves it empty.
set RH_PY=
set RH_PYV=
del /q ".python_choice" >nul 2>nul
if not exist "find_python.py" goto :eof
"%RH_BOOT%" %RH_BOOTV% find_python.py --install --out .python_choice
if exist ".python_choice" set /p RH_PY=<".python_choice"
del /q ".python_choice" >nul 2>nul
goto :eof

:bootpython
rem Sets RH_BOOT/RH_BOOTV to any Python that really runs (checked by making a file, not by the exit code).
set RH_BOOT=
set RH_BOOTV=
call :trypython "py" "-3"
if not defined RH_BOOT call :trypython "python" ""
for %%D in (Python312 Python311 Python313 Python314 Python315) do (
  if not defined RH_BOOT if exist "%LOCALAPPDATA%\Programs\Python\%%D\python.exe" call :trypython "%LOCALAPPDATA%\Programs\Python\%%D\python.exe" ""
)
for /d %%D in ("%LOCALAPPDATA%\Python\pythoncore-*") do (
  if not defined RH_BOOT if exist "%%~D\python.exe" call :trypython "%%~D\python.exe" ""
)
goto :eof

:trypython
del /q ".pyok" >nul 2>nul
"%~1" %~2 -c "open('.pyok','w').write('ok')" <nul >nul 2>nul
if exist ".pyok" (
  set "RH_BOOT=%~1"
  set "RH_BOOTV=%~2"
)
del /q ".pyok" >nul 2>nul
goto :eof

:installpython
rem No Python at all: winget (built into Windows). find_python.py then picks it up.
where winget >nul 2>nul
if errorlevel 1 goto :eof
echo [setup] Installing Python 3.12 with winget - a few minutes. Allow it if Windows asks.
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
call :bootpython
goto :eof
