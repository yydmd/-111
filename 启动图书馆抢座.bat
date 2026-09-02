@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=%~dp0.venv\Scripts\python.exe"

if exist "%PYTHON%" goto start_server
echo Python virtual environment not found.
echo Run: .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
pause
exit /b 1

:start_server
start "" /min "%PYTHON%" scripts\watchdog.py
powershell.exe -NoProfile -Command "Start-Sleep -Seconds 2"
start "" "http://127.0.0.1:8787/"
endlocal
