@echo off
REM agentchattr — starts the server only
cd /d "%~dp0.."

REM Auto-create venv and install deps on first run
if not exist ".venv" (
    python -m venv .venv
    .venv\Scripts\pip install -q -r requirements.txt >nul 2>nul
)
call .venv\Scripts\activate.bat

set AGENTCHATTR_NETWORK_CONFIRM=YES
python server_entry.py --allow-network
echo.
echo === Server exited with code %ERRORLEVEL% ===
pause
