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

is_port_listening() {
    lsof -i :"$1" -sTCP:LISTEN >/dev/null 2>&1 || \
    ss -tlnp 2>/dev/null | grep -q ":$1 "
}

is_server_healthy() {
    is_port_listening 8300 && is_port_listening 8200 && is_port_listening 8201
}

ensure_venv

mkdir -p data
LOG_FILE="data/server.log"
SERVER_SESSION="agentchattr-server"

if is_server_healthy; then
    echo "agentchattr is already running."
else
    if command -v tmux >/dev/null 2>&1; then
        tmux kill-session -t "$SERVER_SESSION" >/dev/null 2>&1 || true
    fi
    if [ "$AUTO_APPROVE" -eq 1 ]; then
        tmux new-session -d -s "$SERVER_SESSION" "cd '$(pwd)' && AGENTCHATTR_AUTO_APPROVE=1 .venv/bin/python -c 'import os,sys; os.environ[\"AGENTCHATTR_NETWORK_CONFIRM\"]=\"YES\"; import run; sys.argv=[\"run.py\",\"--allow-network\"]; run.main()' >>'$LOG_FILE' 2>&1"
    else
        tmux new-session -d -s "$SERVER_SESSION" "cd '$(pwd)' && .venv/bin/python -c 'import os,sys; os.environ[\"AGENTCHATTR_NETWORK_CONFIRM\"]=\"YES\"; import run; sys.argv=[\"run.py\",\"--allow-network\"]; run.main()' >>'$LOG_FILE' 2>&1"
    fi
    # Record the tmux server pane pid for compatibility with stop.sh.
    tmux list-panes -t "$SERVER_SESSION" -F '#{pane_pid}' | head -n 1 > data/server.pid

    i=0
    started=0
    while [ "$i" -lt 30 ]; do
        if is_server_healthy; then
            started=1
            break
        fi
        if ! tmux has-session -t "$SERVER_SESSION" >/dev/null 2>&1; then
            echo "ERROR: Server process exited unexpectedly. Check $LOG_FILE for details."
            tail -10 "$LOG_FILE"
            exit 1
        fi
        sleep 0.5
        i=$((i + 1))
    done

    if [ "$started" -eq 0 ]; then
        echo "ERROR: Server did not start within 15 seconds. Check $LOG_FILE for details."
        tail -10 "$LOG_FILE"
        exit 1
    fi
    echo "Server started in tmux session $SERVER_SESSION."
fi

echo "Logs: $LOG_FILE"
if [ "$AUTO_APPROVE" -eq 1 ]; then
    echo "Auto-started Claude/Codex/Gemini instances will use their skip-permissions / bypass modes."
fi
echo "Attach with: tmux attach -t $SERVER_SESSION"
echo "WSL IPs:"
hostname -I 2>/dev/null || true
echo ""
echo "Run this in Windows PowerShell as Administrator to refresh the LAN proxy:"
echo "powershell.exe -ExecutionPolicy Bypass -File windows\\setup_wsl_lan.ps1"
