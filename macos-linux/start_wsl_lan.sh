#!/usr/bin/env sh
# agentchattr - starts the server for WSL/LAN access and prints the setup command
cd "$(dirname "$0")/.."

AUTO_APPROVE=0
for arg in "$@"; do
    case "$arg" in
        --skip-permissions)
            AUTO_APPROVE=1
            ;;
        *)
            echo "Unknown argument: $arg"
            echo "Usage: sh macos-linux/start_wsl_lan.sh [--skip-permissions]"
            exit 1
            ;;
    esac
done

PYTHON_BIN=""
if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
else
    echo "Python 3 is required but was not found on PATH."
    exit 1
fi

ensure_venv() {
    if [ -d ".venv" ] && [ ! -x ".venv/bin/python" ]; then
        echo "Recreating .venv for this platform..."
        rm -rf .venv
    fi

    if [ ! -x ".venv/bin/python" ]; then
        echo "Creating virtual environment..."
        "$PYTHON_BIN" -m venv .venv || {
            echo "Error: failed to create .venv with $PYTHON_BIN."
            exit 1
        }
        .venv/bin/python -m pip install -q -r requirements.txt || {
            echo "Error: failed to install Python dependencies."
            exit 1
        }
    fi
}

is_server_running() {
    lsof -i :8300 -sTCP:LISTEN >/dev/null 2>&1 || \
    ss -tlnp 2>/dev/null | grep -q ':8300 '
}

ensure_venv

mkdir -p data
LOG_FILE="data/server.log"

if is_server_running; then
    echo "agentchattr is already running."
else
    if [ "$AUTO_APPROVE" -eq 1 ]; then
        AGENTCHATTR_NETWORK_CONFIRM=YES AGENTCHATTR_AUTO_APPROVE=1 nohup .venv/bin/python run.py --allow-network >>"$LOG_FILE" 2>&1 &
    else
        AGENTCHATTR_NETWORK_CONFIRM=YES nohup .venv/bin/python run.py --allow-network >>"$LOG_FILE" 2>&1 &
    fi
    SERVER_PID=$!
    echo "$SERVER_PID" > data/server.pid

    i=0
    while [ "$i" -lt 30 ]; do
        if is_server_running; then
            break
        fi
        sleep 0.5
        i=$((i + 1))
    done
fi

echo "Logs: $LOG_FILE"
if [ "$AUTO_APPROVE" -eq 1 ]; then
    echo "Auto-started Claude/Codex/Gemini instances will use their skip-permissions / bypass modes."
fi
echo "WSL IPs:"
hostname -I 2>/dev/null || true
echo ""
echo "Run this in Windows PowerShell as Administrator to refresh the LAN proxy:"
echo "powershell.exe -ExecutionPolicy Bypass -File windows\\setup_wsl_lan.ps1"
