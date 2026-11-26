@echo off
setlocal
REM agentchattr — starts server (if not running) + Gemini wrapper (auto-approve mode)
cd /d "%~dp0.."

REM Pre-flight: check that the Antigravity CLI (agy) is installed
where agy >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo   Error: "agy" (Antigravity CLI) was not found on PATH.
    echo   Install it with:  winget install Google.AntigravityCLI
    echo   or in PowerShell: irm https://antigravity.google/cli/install.ps1 ^| iex
    echo.
    pause
    exit /b 1
)

call "%~dp0start.bat" --skip-permissions
if errorlevel 1 (
    pause
    exit /b 1
)

.venv\Scripts\python.exe wrapper.py gemini --dangerously-skip-permissions
if %errorlevel% neq 0 (
    echo.
    echo   Agent exited unexpectedly. Check the output above.
    pause
)
