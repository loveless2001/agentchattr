@echo off
setlocal
REM agentchattr — starts server (if not running) + Codex wrapper (auto-approve mode)
cd /d "%~dp0.."

REM Pre-flight: check that codex CLI is installed
where codex >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo   Error: "codex" was not found on PATH.
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

.venv\Scripts\python.exe wrapper.py codex --dangerously-bypass-approvals-and-sandbox
if %errorlevel% neq 0 (
    echo.
    echo   Agent exited unexpectedly. Check the output above.
    pause
)
