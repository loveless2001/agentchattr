#!/usr/bin/env sh
# agentchattr - starts the server in the background
cd "$(dirname "$0")/.."

AUTO_APPROVE=0
for arg in "$@"; do
    case "$arg" in
        --skip-permissions)
            AUTO_APPROVE=1
            ;;
        *)
            echo "Unknown argument: $arg"
            echo "Usage: sh macos-linux/start.sh [--skip-permissions]"
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

ensure_venv

is_port_listening() {
    lsof -i :"$1" -sTCP:LISTEN >/dev/null 2>&1 || \
    ss -tlnp 2>/dev/null | grep -q ":$1 "
}

is_server_healthy() {
    is_port_listening 8300 && is_port_listening 8200 && is_port_listening 8201
}

mkdir -p data
LOG_FILE="data/server.log"
SERVER_SESSION="agentchattr-server"
SERVER_PID=""
STARTED_WITH_TMUX=0

start_server() {
    if command -v tmux >/dev/null 2>&1; then
        tmux kill-session -t "$SERVER_SESSION" >/dev/null 2>&1 || true
        if [ "$AUTO_APPROVE" -eq 1 ]; then
            tmux new-session -d -s "$SERVER_SESSION" "cd '$(pwd)' && AGENTCHATTR_AUTO_APPROVE=1 .venv/bin/python -c 'import os,sys; os.environ[\"AGENTCHATTR_NETWORK_CONFIRM\"]=\"YES\"; import run; sys.argv=[\"run.py\",\"--allow-network\"]; run.main()' >>'$LOG_FILE' 2>&1"
        else
            tmux new-session -d -s "$SERVER_SESSION" "cd '$(pwd)' && .venv/bin/python -c 'import os,sys; os.environ[\"AGENTCHATTR_NETWORK_CONFIRM\"]=\"YES\"; import run; sys.argv=[\"run.py\",\"--allow-network\"]; run.main()' >>'$LOG_FILE' 2>&1"
        fi
        SERVER_PID="$(tmux list-panes -t "$SERVER_SESSION" -F '#{pane_pid}' | head -n 1)"
        STARTED_WITH_TMUX=1
    else
        if [ "$AUTO_APPROVE" -eq 1 ]; then
            AGENTCHATTR_AUTO_APPROVE=1 nohup .venv/bin/python -c 'import os,sys; os.environ["AGENTCHATTR_NETWORK_CONFIRM"]="YES"; import run; sys.argv=["run.py","--allow-network"]; run.main()' >>"$LOG_FILE" 2>&1 &
        else
            nohup .venv/bin/python -c 'import os,sys; os.environ["AGENTCHATTR_NETWORK_CONFIRM"]="YES"; import run; sys.argv=["run.py","--allow-network"]; run.main()' >>"$LOG_FILE" 2>&1 &
        fi
        SERVER_PID=$!
    fi

    echo "$SERVER_PID" > data/server.pid
}

if is_server_healthy; then
    echo "agentchattr is already running."
    echo "Web UI: http://127.0.0.1:8300"
    echo "Logs: $LOG_FILE"
    exit 0
fi

start_server

i=0
while [ "$i" -lt 30 ]; do
    if is_server_healthy; then
        break
    fi
    if [ "$STARTED_WITH_TMUX" -eq 1 ]; then
        if ! tmux has-session -t "$SERVER_SESSION" >/dev/null 2>&1; then
            echo "Server exited unexpectedly. Check $LOG_FILE"
            exit 1
        fi
    else
        if ! kill -0 "$SERVER_PID" 2>/dev/null; then
            echo "Server exited unexpectedly. Check $LOG_FILE"
            exit 1
        fi
    fi
    sleep 0.5
    i=$((i + 1))
done

if is_server_healthy; then
    echo "agentchattr started in the background."
    echo "Web UI: http://127.0.0.1:8300"
    echo "Logs: $LOG_FILE"
    echo "Agents now auto-start on first @mention and run in background tmux sessions."
    if [ "$AUTO_APPROVE" -eq 1 ]; then
        echo "Auto-started Claude/Codex/Gemini instances will use their skip-permissions / bypass modes."
    fi
    if [ "$STARTED_WITH_TMUX" -eq 1 ]; then
        echo "Attach with: tmux attach -t $SERVER_SESSION"
    fi
    exit 0
fi

echo "Server did not come up. Check $LOG_FILE"
exit 1
