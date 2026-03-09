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

    def ensure_started(self, agent_name: str) -> dict:
        if not self.can_auto_spawn(agent_name):
            return {"ok": False, "error": f"auto-spawn is not supported for '{agent_name}'"}

        agent_cfg = self._config.get("agents", {}).get(agent_name, {})
        command = agent_cfg.get("command", agent_name)
        if not shutil.which(command):
            return {"ok": False, "error": f"'{command}' is not on PATH for @{agent_name}"}

        now = time.time()
        with self._lock:
            last = self._last_start.get(agent_name, 0.0)
            if now - last < self._cooldown_seconds:
                return {
                    "ok": True,
                    "started": False,
                    "starting": True,
                    "attach_command": self._attach_command(agent_name),
                    "log_path": str(self._log_path(agent_name)),
                }
            self._last_start[agent_name] = now

        log_path = self._log_path(agent_name)
        cmd = [
            sys.executable,
            str((self._root / "wrapper.py").resolve()),
            agent_name,
            "--detached",
        ]
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
                self._last_start.pop(agent_name, None)
            return {"ok": False, "error": str(exc)}

        return {
            "ok": True,
            "started": True,
            "starting": False,
            "attach_command": self._attach_command(agent_name),
            "log_path": str(log_path),
        }

    def _attach_command(self, agent_name: str) -> str:
        return f"tmux attach -t agentchattr-{agent_name}"

    def _log_path(self, agent_name: str) -> Path:
        return self._logs_dir / f"{agent_name}.log"

    def _auto_approve_args(self, agent_name: str) -> list[str]:
        if os.environ.get("AGENTCHATTR_AUTO_APPROVE", "").strip().lower() not in ("1", "true", "yes", "on"):
            return []
        if agent_name == "claude":
            return ["--dangerously-skip-permissions"]
        if agent_name == "codex":
            return ["--", "--dangerously-bypass-approvals-and-sandbox"]
        if agent_name == "gemini":
            return ["--", "--yolo"]
        return []
