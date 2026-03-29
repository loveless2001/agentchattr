@echo off
setlocal
REM agentchattr — starts the server in the background
cd /d "%~dp0.."

set "AUTO_APPROVE=0"
if /i "%~1"=="--skip-permissions" (
    set "AUTO_APPROVE=1"
    shift
)
if not "%~1"=="" (
    echo Unknown argument: %~1
    echo Usage: start.bat [--skip-permissions]
    exit /b 1
)

if exist ".venv" if not exist ".venv\Scripts\python.exe" (
    echo Recreating .venv for this platform...
    rmdir /s /q ".venv"
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo Error: failed to create .venv with python.
        exit /b 1
    )
    .venv\Scripts\python.exe -m pip install -q -r requirements.txt >nul
    if errorlevel 1 (
        echo Error: failed to install Python dependencies.
        exit /b 1
    )
)

if not exist "data" mkdir "data"
set "LOG_FILE=data\server.log"

call :is_server_healthy
if %errorlevel% equ 0 (
    echo agentchattr is already running.
    echo Web UI: http://127.0.0.1:8300
    echo Logs: %LOG_FILE%
    exit /b 0
)

set "SERVER_CMD=cd /d ""%CD%"" && set AGENTCHATTR_NETWORK_CONFIRM=YES&& "
if "%AUTO_APPROVE%"=="1" (
    set "SERVER_CMD=%SERVER_CMD%set AGENTCHATTR_AUTO_APPROVE=1&& "
)
set "SERVER_CMD=%SERVER_CMD%.venv\Scripts\python.exe server_entry.py --allow-network >> ""%LOG_FILE%"" 2>&1"
start "agentchattr server" /min cmd /c "%SERVER_CMD%"

set "WAIT_COUNT=0"
:wait_server
call :is_server_healthy
if %errorlevel% equ 0 goto :server_ready
set /a WAIT_COUNT+=1
if %WAIT_COUNT% geq 30 (
    echo Server did not come up. Check %LOG_FILE%
    exit /b 1
)
timeout /t 1 /nobreak >nul
goto :wait_server

:server_ready
echo agentchattr started in the background.
echo Web UI: http://127.0.0.1:8300
echo Logs: %LOG_FILE%
echo Start agents separately with windows\start_claude.bat, windows\start_codex.bat, windows\start_gemini.bat, or windows\start_kimi.bat.
if "%AUTO_APPROVE%"=="1" (
    echo Auto-started Claude/Codex/Gemini instances will use their skip-permissions / bypass modes.
)
exit /b 0

:is_server_healthy
netstat -ano | findstr :8300 | findstr LISTENING >nul 2>&1
if errorlevel 1 exit /b 1
netstat -ano | findstr :8200 | findstr LISTENING >nul 2>&1
if errorlevel 1 exit /b 1
netstat -ano | findstr :8201 | findstr LISTENING >nul 2>&1
if errorlevel 1 exit /b 1
exit /b 0
