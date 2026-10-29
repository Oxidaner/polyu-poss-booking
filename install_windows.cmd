@echo off
setlocal
cd /d "%~dp0"

echo [1/3] Checking Python 3.14...
set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 (
    py -3.14 --version >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py -3.14"
)
if not defined PYTHON_CMD (
    where python >nul 2>nul
    if not errorlevel 1 (
        python --version | findstr /B /C:"Python 3.14" >nul
        if not errorlevel 1 set "PYTHON_CMD=python"
    )
)
if not defined PYTHON_CMD (
    echo Python 3.14 was not found.
    echo Install Python 3.14, enable "Add python.exe to PATH", and run this file again.
    pause
    exit /b 1
)

echo [2/3] Creating the local environment...
if not exist ".venv\Scripts\python.exe" (
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 goto :failed
)

echo [3/3] Installing the required packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

if not exist "dumps" mkdir dumps
echo.
echo Installation completed.
echo Double-click start_windows.cmd to open the local POSS console.
pause
exit /b 0

:failed
echo.
echo Installation failed. Check your network connection and try again.
pause
exit /b 1
