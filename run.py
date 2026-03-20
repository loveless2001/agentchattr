"""Entry point — starts MCP server (port 8200) + web UI (port 8300)."""

import asyncio
import os
import secrets
import socket
import sys
import threading
import time
import logging
from pathlib import Path

# Ensure the project directory is on the import path
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger(__name__)


def _venv_python_path() -> Path:
    if sys.platform == "win32":
        return ROOT / ".venv" / "Scripts" / "python.exe"
    return ROOT / ".venv" / "bin" / "python"


def _friendly_missing_dependency(exc: ModuleNotFoundError) -> bool:
    missing = (exc.name or "").split(".", 1)[0]
    if missing not in {"fastapi", "starlette", "mcp", "fitz"}:
        return False

    print(f"Error: missing Python dependency '{missing}'.")
    venv_python = _venv_python_path()
    if venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
        print(f"Run the server with {venv_python} or use one of the launcher scripts.")
    else:
        print("Install the project dependencies in the active environment and retry.")
    return True


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _start_mcp_thread(name: str, port: int, target):
    state = {"name": name, "port": port, "error": None}

    def runner():
        try:
            target()
        except BaseException as exc:
            state["error"] = exc
            log.exception("Failed to start %s on port %d", name, port)

    thread = threading.Thread(target=runner, daemon=True, name=f"{name}-thread")
    state["thread"] = thread
    thread.start()
    return state


def _wait_for_mcp_servers(states: list[dict], timeout_seconds: float = 5.0) -> tuple[bool, str | None]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        for state in states:
            if state["error"] is not None:
                return False, f"{state['name']} failed to start: {state['error']}"

        if all(_port_open("127.0.0.1", state["port"]) for state in states):
            return True, None

        if any(not state["thread"].is_alive() for state in states):
            break

        time.sleep(0.1)

    failures = []
    for state in states:
        if state["error"] is not None:
            failures.append(f"{state['name']}: {state['error']}")
        elif not _port_open("127.0.0.1", state["port"]):
            failures.append(f"{state['name']}: port {state['port']} did not become ready")
    return False, "; ".join(failures) if failures else "unknown MCP startup failure"


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    from config_loader import load_config
    config_path = ROOT / "config.toml"
    if not config_path.exists():
        print(f"Error: {config_path} not found")
        sys.exit(1)

    config = load_config(ROOT)

    # --- Security: generate a random session token (in-memory only) ---
    session_token = secrets.token_hex(32)

    # Configure the FastAPI app (creates shared store)
    try:
        from app import app, configure, set_event_loop, store as _store_ref
    except ModuleNotFoundError as exc:
        if _friendly_missing_dependency(exc):
            sys.exit(1)
        raise
    configure(config, session_token=session_token)

    # Share stores with the MCP bridge
    from app import store, rules, summaries, jobs, room_settings, registry, router as app_router, agents as app_agents, session_engine, session_store
    import mcp_bridge
    mcp_bridge.store = store
    mcp_bridge.rules = rules
    mcp_bridge.summaries = summaries
    mcp_bridge.jobs = jobs
    mcp_bridge.room_settings = room_settings
    mcp_bridge.registry = registry
    mcp_bridge.config = config
    mcp_bridge.router = app_router
    mcp_bridge.agents = app_agents

    # Enable cursor and role persistence across restarts
    data_dir = ROOT / config.get("server", {}).get("data_dir", "./data")
    mcp_bridge._CURSORS_FILE = data_dir / "mcp_cursors.json"
    mcp_bridge._load_cursors()
    mcp_bridge._ROLES_FILE = data_dir / "roles.json"
    mcp_bridge._load_roles()

    # Clean up stale wrapper/tmux sessions for deleted channels
    from app import launcher
    if launcher:
        active_channels = room_settings.get("channels", ["general"])
        launcher.cleanup_stale_sessions(active_channels)

    # Start MCP servers in background threads
    http_port = config.get("mcp", {}).get("http_port", 8200)
    sse_port = config.get("mcp", {}).get("sse_port", 8201)
    for name, port in (("MCP streamable-http", http_port), ("MCP SSE", sse_port)):
        if _port_open("127.0.0.1", port):
            print(f"Error: {name} port {port} is already in use on 127.0.0.1.")
            sys.exit(1)

    mcp_bridge.mcp_http.settings.port = http_port
    mcp_bridge.mcp_sse.settings.port = sse_port

    mcp_states = [
        _start_mcp_thread("MCP streamable-http", http_port, mcp_bridge.run_http_server),
        _start_mcp_thread("MCP SSE", sse_port, mcp_bridge.run_sse_server),
    ]
    ready, error = _wait_for_mcp_servers(mcp_states)
    if not ready:
        print(f"Error: MCP startup failed: {error}")
        sys.exit(1)
    log.info("MCP streamable-http on port %d, SSE on port %d", http_port, sse_port)

    # Mount static files
    from fastapi.staticfiles import StaticFiles
    from fastapi.responses import HTMLResponse

    static_dir = ROOT / "static"

    @app.get("/")
    async def index():
        # Read index.html fresh each request so changes take effect without restart.
        # Inject the session token into the HTML so the browser client can use it.
        # This is safe: same-origin policy prevents cross-origin pages from reading
        # the response body, so only the user's own browser tab gets the token.
        html = (static_dir / "index.html").read_text("utf-8")
        injected = html.replace(
            "</head>",
            f'<script>window.__SESSION_TOKEN__="{session_token}";</script>\n</head>',
        )
        return HTMLResponse(injected, headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Capture the event loop for the store→WebSocket bridge
    @app.on_event("startup")
    async def on_startup():
        set_event_loop(asyncio.get_running_loop())
        # Resume any sessions that were active before restart
        if session_engine:
            session_engine.resume_active_sessions()

    # Run web server
    import uvicorn
    host = config.get("server", {}).get("host", "127.0.0.1")
    port = config.get("server", {}).get("port", 8300)

    # --- Security: warn if binding to a non-localhost address ---
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"\n  !! SECURITY WARNING — binding to {host} !!")
        print("  This exposes agentchattr to your local network.")
        print()
        print("  Risks:")
        print("  - No TLS: traffic (including session token) is plaintext")
        print("  - Anyone on your network can sniff the token and gain full access")
        print("  - With the token, anyone can @mention agents and trigger tool execution")
        print("  - If agents run with auto-approve, this means remote code execution")
        print()
        print("  Only use this on a trusted home network. Never on public/shared WiFi.")
        if "--allow-network" not in sys.argv:
            print("  Pass --allow-network to start anyway, or set host to 127.0.0.1.\n")
            sys.exit(1)
        else:
            print()
            confirm = os.environ.get("AGENTCHATTR_NETWORK_CONFIRM", "")
            if confirm:
                confirm = confirm.strip()
                print("  Network confirmation provided via AGENTCHATTR_NETWORK_CONFIRM.")
            else:
                try:
                    confirm = input("  Type YES to accept these risks and start: ").strip()
                except (EOFError, KeyboardInterrupt):
                    confirm = ""
            if confirm != "YES":
                print("  Aborted.\n")
                sys.exit(1)

    print(f"\n  agentchattr")
    print(f"  Web UI:  http://{host}:{port}")
    print(f"  MCP HTTP: http://{host}:{http_port}/mcp  (Claude, Codex)")
    print(f"  MCP SSE:  http://{host}:{sse_port}/sse   (Gemini)")
    print(f"  Agents auto-trigger on @mention")
    print(f"\n  Session token: {session_token}\n")

    uvicorn.run(app, host=host, port=port, log_level="info")

if __name__ == "__main__":
    main()
