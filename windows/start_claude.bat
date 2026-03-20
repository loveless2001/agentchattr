@echo off
REM agentchattr — starts server (if not running) + Claude wrapper
cd /d "%~dp0.."

REM Auto-create venv and install deps on first run
if not exist ".venv" (
    python -m venv .venv
    .venv\Scripts\pip install -q -r requirements.txt >nul 2>nul
)
call .venv\Scripts\activate.bat

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

REM Start server if not already running, then wait for it
netstat -ano | findstr :8300 | findstr LISTENING >nul 2>&1
if %errorlevel% equ 0 (
    netstat -ano | findstr :8200 | findstr LISTENING >nul 2>&1
)
if %errorlevel% equ 0 (
    netstat -ano | findstr :8201 | findstr LISTENING >nul 2>&1
)
if %errorlevel% neq 0 (
    start "agentchattr server" cmd /c "set AGENTCHATTR_NETWORK_CONFIRM=YES&& python server_entry.py --allow-network"
    set WAIT_COUNT=0
)
:wait_server
netstat -ano | findstr :8300 | findstr LISTENING >nul 2>&1
if %errorlevel% neq 0 (
    goto :wait_retry
)
netstat -ano | findstr :8200 | findstr LISTENING >nul 2>&1
if %errorlevel% neq 0 goto :wait_retry
netstat -ano | findstr :8201 | findstr LISTENING >nul 2>&1
if %errorlevel% neq 0 goto :wait_retry
goto :server_ready
:wait_retry
set /a WAIT_COUNT+=1
if %WAIT_COUNT% geq 30 (
    echo.
    echo   Server did not become healthy within 30 seconds.
    pause
    exit /b 1
)
timeout /t 1 /nobreak >nul
goto :wait_server
:server_ready

python wrapper.py claude
if %errorlevel% neq 0 (
    echo.
    echo   Agent exited unexpectedly. Check the output above.
    pause
)
