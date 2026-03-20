"""Background launcher for server-managed agent wrappers on macOS/Linux."""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
import os
from pathlib import Path


class ServerLauncher:
    """Starts wrapper processes in the background and writes logs to disk."""

    def __init__(self, root: Path, config: dict):
        self._root = Path(root)
        self._config = config
        data_dir = config.get("server", {}).get("data_dir", "./data")
        self._logs_dir = (self._root / data_dir / "launcher-logs").resolve()
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_start: dict[str, float] = {}
        self._cooldown_seconds = 15

    def can_auto_spawn(self, agent_name: str) -> bool:
        if sys.platform == "win32":
            return False
        cfg = self._config.get("agents", {}).get(agent_name)
        if not cfg:
            return False
        if cfg.get("type") == "api":
            return False
        return True

    def ensure_started(
        self,
        agent_name: str,
        *,
        label: str | None = None,
        session_name: str | None = None,
        channel: str | None = None,
    ) -> dict:
        if not self.can_auto_spawn(agent_name):
            return {"ok": False, "error": f"auto-spawn is not supported for '{agent_name}'"}

        agent_cfg = self._config.get("agents", {}).get(agent_name, {})
        command = agent_cfg.get("command", agent_name)
        if not shutil.which(command):
            return {"ok": False, "error": f"'{command}' is not on PATH for @{agent_name}"}

        start_key = session_name or agent_name
        now = time.time()
        with self._lock:
            last = self._last_start.get(start_key, 0.0)
            if now - last < self._cooldown_seconds:
                return {
                    "ok": True,
                    "started": False,
                    "starting": True,
                    "attach_command": self._attach_command(
                        session_name or agent_name, is_session_name=bool(session_name)),
                    "log_path": str(self._log_path(agent_name)),
                }
            self._last_start[start_key] = now

        log_path = self._log_path(agent_name)
        cmd = [
            sys.executable,
            str((self._root / "wrapper.py").resolve()),
            agent_name,
            "--detached",
        ]
        if label:
            cmd.extend(["--label", label])
        if session_name:
            cmd.extend(["--session-name", session_name])
        if channel:
            cmd.extend(["--channel", channel])
        cmd.extend(self._auto_approve_args(agent_name))
        try:
            with open(log_path, "ab") as log_file:
                subprocess.Popen(
                    cmd,
                    cwd=str(self._root),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except Exception as exc:
            with self._lock:
                self._last_start.pop(start_key, None)
            return {"ok": False, "error": str(exc)}

        return {
            "ok": True,
            "started": True,
            "starting": False,
            "attach_command": self._attach_command(
                session_name or agent_name, is_session_name=bool(session_name)),
            "log_path": str(log_path),
        }

    def _attach_command(self, name: str, *, is_session_name: bool = False) -> str:
        """Build tmux attach command. If is_session_name, use name as-is (already prefixed)."""
        session = name if is_session_name else f"agentchattr-{name}"
        return f"tmux attach -t {session}"

    def _log_path(self, agent_name: str) -> Path:
        return self._logs_dir / f"{agent_name}.log"

    def kill_session(self, session_name: str):
        """Kill a wrapper process and its tmux session."""
        # Kill wrapper processes matching this session name
        try:
            result = subprocess.run(
                ["pgrep", "-f", f"--session-name {session_name}"],
                capture_output=True, text=True,
            )
            for pid in result.stdout.strip().split("\n"):
                pid = pid.strip()
                if pid:
                    subprocess.run(["kill", pid], capture_output=True)
        except Exception:
            pass
        # Kill tmux session
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", session_name],
                capture_output=True,
            )
        except Exception:
            pass

    def cleanup_stale_sessions(self, active_channels: list[str]):
        """Kill wrapper/tmux sessions for channels that no longer exist."""
        if sys.platform == "win32":
            return
        try:
            result = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                return
            prefix = "agentchattr-"
            for session_name in result.stdout.strip().split("\n"):
                session_name = session_name.strip()
                if not session_name.startswith(prefix):
                    continue
                # Parse channel from session name: agentchattr-{agent}-{channel}
                rest = session_name[len(prefix):]
                parts = rest.split("-", 1)
                if len(parts) < 2:
                    continue
                channel = parts[1]
                if channel not in active_channels:
                    self.kill_session(session_name)
        except Exception:
            pass

    def _auto_approve_args(self, agent_name: str) -> list[str]:
        if os.environ.get("AGENTCHATTR_AUTO_APPROVE", "").strip().lower() not in ("1", "true", "yes", "on"):
            return []
        if agent_name == "claude":
            return ["--dangerously-skip-permissions"]
        if agent_name == "codex":
            return ["--dangerously-bypass-approvals-and-sandbox"]
        if agent_name == "gemini":
            return ["--yolo"]
        return []
