@echo off
setlocal
REM agentchattr — starts server (if not running) + Kimi wrapper
cd /d "%~dp0.."

REM Pre-flight: check that kimi CLI is installed
where kimi >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo   Error: "kimi" was not found on PATH.
    echo   Install it first, then try again.
    echo.
    pause
    exit /b 1
)

call "%~dp0start.bat"
if errorlevel 1 (
    pause
    exit /b 1
)

.venv\Scripts\python.exe wrapper.py kimi
if %errorlevel% neq 0 (
    echo.
    echo   Agent exited unexpectedly. Check the output above.
    pause
)
