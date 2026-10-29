@echo off
setlocal
cd /d "%~dp0"

if not exist "dumps\booking-local.pid" (
    echo No local booking process is running.
    pause
    exit /b 0
)

set /p RUNNER_PID=<"dumps\booking-local.pid"
if not defined RUNNER_PID (
    echo The local process ID is empty.
    pause
    exit /b 1
)

echo Stopping local booking process %RUNNER_PID%...
taskkill /PID %RUNNER_PID% /T /F
if errorlevel 1 (
    echo Failed to stop the local process. Check the process ID and try again.
    pause
    exit /b 1
)
del /q "dumps\booking-local.pid" >nul 2>nul
echo Local booking process stopped.
pause
