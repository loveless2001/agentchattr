#!/usr/bin/env sh
# agentchattr - starts server (if not running) + Claude wrapper
cd "$(dirname "$0")/.."

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

if ! is_server_healthy; then
    if [ "$(uname -s)" = "Darwin" ]; then
        osascript -e "tell app \"Terminal\" to do script \"cd '$(pwd)' && .venv/bin/python -c 'import os,sys; os.environ[\\\"AGENTCHATTR_NETWORK_CONFIRM\\\"]=\\\"YES\\\"; import run; sys.argv=[\\\"run.py\\\",\\\"--allow-network\\\"]; run.main()'\"" > /dev/null 2>&1
    else
        if command -v gnome-terminal >/dev/null 2>&1; then
            gnome-terminal -- sh -c "cd '$(pwd)' && .venv/bin/python -c 'import os,sys; os.environ[\"AGENTCHATTR_NETWORK_CONFIRM\"]=\"YES\"; import run; sys.argv=[\"run.py\",\"--allow-network\"]; run.main()'; printf 'Press Enter to close... '; read _"
        elif command -v xterm >/dev/null 2>&1; then
            xterm -e sh -c "cd '$(pwd)' && .venv/bin/python -c 'import os,sys; os.environ[\"AGENTCHATTR_NETWORK_CONFIRM\"]=\"YES\"; import run; sys.argv=[\"run.py\",\"--allow-network\"]; run.main()'" &
        else
            .venv/bin/python -c 'import os,sys; os.environ["AGENTCHATTR_NETWORK_CONFIRM"]="YES"; import run; sys.argv=["run.py","--allow-network"]; run.main()' > data/server.log 2>&1 &
        fi
    fi

    i=0
    while [ "$i" -lt 30 ]; do
        if is_server_healthy; then
            break
        fi
        sleep 0.5
        i=$((i + 1))
    done
fi

if ! is_server_healthy; then
    echo "Server did not become healthy. Check data/server.log"
    exit 1
fi

.venv/bin/python wrapper.py claude
