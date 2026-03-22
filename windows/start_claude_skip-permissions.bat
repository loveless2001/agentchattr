@echo off
setlocal
REM agentchattr — starts server (if not running) + Claude wrapper (auto-approve mode)
cd /d "%~dp0.."

REM Pre-flight: check that claude CLI is installed
where claude >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo   Error: "claude" was not found on PATH.
    echo   Install it first, then try again.
    echo.
    pause
    exit /b 1
)

call "%~dp0start.bat" --skip-permissions
if errorlevel 1 (
    pause
    exit /b 1
)

.venv\Scripts\python.exe wrapper.py claude --dangerously-skip-permissions
if %errorlevel% neq 0 (
    echo.
    echo   Agent exited unexpectedly. Check the output above.
    pause
)
