#!/usr/bin/env sh
# agentchattr - stops the background server and agent tmux sessions
cd "$(dirname "$0")/.."

PID_FILE="data/server.pid"
SERVER_SESSION="agentchattr-server"

stop_server_pid() {
    pid="$1"
    if [ -z "$pid" ]; then
        return 1
    fi
    if ! kill -0 "$pid" >/dev/null 2>&1; then
        return 1
    fi
    kill "$pid" >/dev/null 2>&1 || return 1
    return 0
}

SERVER_STOPPED=0

if [ -f "$PID_FILE" ]; then
    SERVER_PID="$(cat "$PID_FILE" 2>/dev/null)"
    if stop_server_pid "$SERVER_PID"; then
        echo "Stopped server process $SERVER_PID"
        SERVER_STOPPED=1
    fi
    rm -f "$PID_FILE"
fi

if command -v tmux >/dev/null 2>&1; then
    if tmux has-session -t "$SERVER_SESSION" >/dev/null 2>&1; then
        tmux kill-session -t "$SERVER_SESSION" >/dev/null 2>&1 || true
        echo "Stopped tmux session $SERVER_SESSION"
        SERVER_STOPPED=1
    fi
fi

if [ "$SERVER_STOPPED" -eq 0 ]; then
    FALLBACK_PID="$(ps -ef | grep '[p]ython run.py' | awk 'NR==1 {print $2}')"
    if stop_server_pid "$FALLBACK_PID"; then
        echo "Stopped server process $FALLBACK_PID"
        SERVER_STOPPED=1
    fi
fi

if [ "$SERVER_STOPPED" -eq 0 ]; then
    FALLBACK_PID="$(ps -ef | grep '[p]ython -c import os,sys; os.environ' | awk 'NR==1 {print $2}')"
    if stop_server_pid "$FALLBACK_PID"; then
        echo "Stopped server process $FALLBACK_PID"
        SERVER_STOPPED=1
    fi
fi

# Kill detached wrapper processes (they survive server stop and recreate tmux)
for pid in $(pgrep -f 'wrapper\.py .* --detached' 2>/dev/null); do
    kill "$pid" >/dev/null 2>&1 && echo "Stopped wrapper process $pid"
done

if command -v tmux >/dev/null 2>&1; then
    tmux ls 2>/dev/null | awk -F: '/^agentchattr-/{print $1}' | while IFS= read -r session; do
        if [ -n "$session" ]; then
            tmux kill-session -t "$session" >/dev/null 2>&1 || true
            echo "Stopped tmux session $session"
        fi
    done
fi

if [ "$SERVER_STOPPED" -eq 0 ]; then
    echo "No running server process found."
fi
