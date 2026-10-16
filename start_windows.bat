@echo off
chcp 65001 >nul
cd /d "%~dp0"
set RH_ARGS=
set RH_UPDATED=
set RH_REBUILT=
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
rem Prefer Python 3.12/3.11: the newest Python (e.g. 3.15) has no speech recognition parts yet.
call :findsupported
if not defined RH_PY call :installpython
if not defined RH_PY call :findany
if not defined RH_PY goto nopython
"%RH_PY%" %RH_PYV% -m venv .venv
if not exist ".venv\Scripts\python.exe" goto nopython
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
:packages
echo [setup] Checking packages...
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
:newpython
rem The .venv was made with a Python that is too new: make it again with Python 3.12/3.11 (installed if needed).
set RH_REBUILT=1
echo.
echo [setup] This Python is too new for the quiz speech recognition parts.
echo [setup] Making .venv again with Python 3.12 or 3.11 - a few minutes...
call :findsupported
if not defined RH_PY call :installpython
if not defined RH_PY goto oldpythonmissing
"%RH_PY%" %RH_PYV% -m venv --clear .venv
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

:findsupported
rem Sets RH_PY (python.exe or the py launcher) and RH_PYV (its version option) to a Python 3.12/3.11/3.13/3.14 that works.
rem Paths are kept without quotes inside the variables so folders with spaces or brackets are safe.
set RH_PY=
set RH_PYV=
for %%V in (3.12 3.11 3.13 3.14) do (
  if not defined RH_PY (
    py -%%V -c "pass" <nul >nul 2>nul
    if not errorlevel 1 (
      set "RH_PY=py"
      set "RH_PYV=-%%V"
    )
  )
)
for %%D in (Python312 Python311 Python313 Python314) do (
  if not defined RH_PY if exist "%LOCALAPPDATA%\Programs\Python\%%D\python.exe" set "RH_PY=%LOCALAPPDATA%\Programs\Python\%%D\python.exe"
  if not defined RH_PY if exist "%ProgramFiles%\%%D\python.exe" set "RH_PY=%ProgramFiles%\%%D\python.exe"
)
for %%D in (pythoncore-3.12-64 pythoncore-3.11-64 pythoncore-3.13-64 pythoncore-3.14-64) do (
  if not defined RH_PY if exist "%LOCALAPPDATA%\Python\%%D\python.exe" set "RH_PY=%LOCALAPPDATA%\Python\%%D\python.exe"
)
goto :eof

:findany
rem Last resort: any Python on PATH (the admin screen works; quiz parts are checked later).
set RH_PY=
set RH_PYV=
py -3 -c "pass" <nul >nul 2>nul
if not errorlevel 1 (
  set "RH_PY=py"
  set "RH_PYV=-3"
)
if defined RH_PY goto :eof
python -c "pass" <nul >nul 2>nul
if not errorlevel 1 set "RH_PY=python"
goto :eof

:installpython
rem winget (built into Windows), or the new Python install manager ("py install") when winget is missing or fails.
where winget >nul 2>nul
if errorlevel 1 goto installpymanager
echo [setup] Installing Python 3.11 with winget - a few minutes. Allow it if Windows asks.
winget install -e --id Python.Python.3.11 --scope user --accept-package-agreements --accept-source-agreements
call :findsupported
if defined RH_PY goto :eof
:installpymanager
py list <nul >nul 2>nul
if errorlevel 1 goto :eof
echo [setup] Installing Python 3.12 with the Python install manager - a few minutes...
py install 3.12 <nul
call :findsupported
goto :eof
