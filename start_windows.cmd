@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo The local environment is not installed yet.
    echo Run install_windows.cmd first.
    pause
    exit /b 1
)

set "PYTHONUTF8=1"
echo Starting the local POSS console...
echo Keep this window open while you use the local runner.
".venv\Scripts\python.exe" poss_ui.py --local-only
echo.
echo The local POSS console has stopped.
pause
