"""Registered waits: an agent parks itself on a long job and is woken when it ends.

The agent starts something long (a training run, a big build), registers a wait
with the MCP tool chat_wait and ends its turn. The server checks every wait
every few seconds (app.py wait runner) and, on the first condition that holds,
posts a notice and wakes the agent with a prompt through its queue file. While
it waits the agent spends no tokens; agent_cache_keepalive.py may keep its
prompt cache warm.

A wait ends on the first of (checked in agent_wait_conditions.py):
  pid            the process exits (its start time is recorded, so a reused pid
                 does not count as the same process; a zombie counts as exited);
  path           the file appears;
  path + contains a line appended to the file after registration contains one of
                 the |-separated texts (plain text: a regex could hang the server);
  deadline       its timeout passes (a wait with only a timeout is a timer).

Waits persist in data/waits.json, so a training run outlives a server restart,
and they stay when the agent goes offline: its wake is then a notice only.
"""

import json
import logging
import os
import threading
import time
from pathlib import Path

from agent_wait_conditions import check, contains_texts, pid_alive, proc_stat

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MINUTES = 24 * 60
MAX_TIMEOUT_MINUTES = 7 * 24 * 60
MAX_WAITS_PER_AGENT = 10
MAX_NOTE_CHARS = 120
MAX_CONTAINS_CHARS = 200
MAX_PATH_CHARS = 1000
_REQUIRED_KEYS = {"id", "agent", "channel", "note", "created_at", "deadline"}
_LOG_POSITION_KEYS = ("offset", "inode", "tail_at")  # updated by check() on its copy


class WaitStore:
    def __init__(self, path, on_change=None):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._on_change = on_change
        self._waits: list[dict] = []
        self._next_id = 1
        try:
            data = json.loads(self._path.read_text("utf-8"))
            self._waits = [w for w in data.get("waits", [])
                           if isinstance(w, dict) and _REQUIRED_KEYS <= w.keys()]
            self._next_id = max([int(data.get("next_id", 1))] + [w["id"] + 1 for w in self._waits])
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def create(self, agent: str, channel: str, *, note: str, pid: int = 0, path: str = "",
               contains: str = "", timeout_minutes: int = 0) -> dict:
        """Register a wait; raises ValueError with a message meant for the agent."""
        note = " ".join(note.replace("@", "").split())[:MAX_NOTE_CHARS]
        if not note:
            raise ValueError("note is required: say what you are waiting for")
        if not (pid or path or timeout_minutes):
            raise ValueError("give a pid, a path or timeout_minutes")
        if timeout_minutes < 0 or timeout_minutes > MAX_TIMEOUT_MINUTES:
            raise ValueError(f"timeout_minutes is 1 to {MAX_TIMEOUT_MINUTES} (7 days), "
                             f"or 0 for the default {DEFAULT_TIMEOUT_MINUTES}")
        now = time.time()
        wait = {"agent": agent, "channel": channel, "note": note, "created_at": now,
                "deadline": now + (timeout_minutes or DEFAULT_TIMEOUT_MINUTES) * 60}
        if pid:
            if pid <= 1 or pid == os.getpid() or not pid_alive(pid):
                raise ValueError(f"process {pid} is not running")
            stat = proc_stat(pid) if os.path.isdir("/proc") else None
            wait.update(pid=int(pid), pid_start=stat[1] if stat else None)
        if contains and not path:
            raise ValueError("contains needs path: the log file to watch")
        if path:
            wait.update(self._path_condition(path, contains))
        with self._lock:
            if sum(w["agent"] == agent for w in self._waits) >= MAX_WAITS_PER_AGENT:
                raise ValueError(f"you already have {MAX_WAITS_PER_AGENT} waits; cancel one first")
            wait["id"] = self._next_id
            self._next_id += 1
            self._waits.append(wait)
            self._save()
        self._changed()
        return dict(wait)

    @staticmethod
    def _path_condition(path: str, contains: str) -> dict:
        expanded = os.path.expanduser(path)
        if not os.path.isabs(expanded):
            raise ValueError("path must be absolute")
        if len(expanded) > MAX_PATH_CHARS or any(ord(c) < 32 for c in expanded):
            raise ValueError("path is too long or contains control characters")
        path = os.path.normpath(expanded)
        if not contains:
            if os.path.exists(path):
                raise ValueError(f"{path} already exists: nothing to wait for")
            return {"path": path}
        if len(contains) > MAX_CONTAINS_CHARS or not contains_texts(contains):
            raise ValueError(f"contains is 1 to {MAX_CONTAINS_CHARS} characters of text, "
                             "alternatives separated by |")
        if os.path.exists(path) and not os.path.isfile(path):
            raise ValueError(f"{path} is not a file")
        condition = {"path": path, "contains": contains, "offset": 0, "inode": None}
        if os.path.isfile(path):  # only lines written from now on count
            stat = os.stat(path)
            condition.update(offset=stat.st_size, inode=stat.st_ino)
        return condition

    def cancel(self, agent: str, wait_id: int = 0) -> int:
        """Cancel one of the agent's waits (all of them for wait_id 0); returns the count."""
        with self._lock:
            keep = [w for w in self._waits
                    if w["agent"] != agent or (wait_id and w["id"] != wait_id)]
            cancelled = len(self._waits) - len(keep)
            if cancelled:
                self._waits = keep
                self._save()
        if cancelled:
            self._changed()
        return cancelled

    def list_for(self, agent: str) -> list[dict]:
        with self._lock:
            return [dict(w) for w in self._waits if w["agent"] == agent]

    def pop_due(self, now: float | None = None) -> list[tuple[dict, str]]:
        """Remove and return the waits that are over, with the reason.

        Conditions are checked on copies outside the lock (log reads can take a
        while), then ended waits are removed and log positions written back."""
        now = time.time() if now is None else now
        with self._lock:
            snapshot = [dict(w) for w in self._waits]
        due, positions = [], {}
        for wait in snapshot:
            try:
                reason = check(wait, now)
            except Exception as exc:  # a broken wait must not stall the others every tick
                log.exception("wait #%s could not be checked", wait.get("id"))
                reason = f"it could not be checked ({type(exc).__name__}); register it again"
            if reason:
                due.append((wait, reason))
            else:
                positions[wait["id"]] = {k: wait[k] for k in _LOG_POSITION_KEYS if k in wait}
        if not due:
            with self._lock:
                for stored in self._waits:
                    stored.update(positions.get(stored["id"], {}))
            return []
        with self._lock:
            present = {w["id"] for w in self._waits}
            due = [(w, reason) for w, reason in due if w["id"] in present]  # not cancelled meanwhile
            ended = {w["id"] for w, _ in due}
            self._waits = [w for w in self._waits if w["id"] not in ended]
            for stored in self._waits:
                stored.update(positions.get(stored["id"], {}))
            self._save()
        self._changed()
        return due

    def rename(self, old: str, new: str):
        with self._lock:
            renamed = [w for w in self._waits if w["agent"] == old]
            for wait in renamed:
                wait["agent"] = new
            if renamed:
                self._save()
        if renamed:
            self._changed()

    def _save(self):
        """Persist (caller holds the lock). Log positions ride along with other
        saves; they need none of their own. A failed write is logged, not
        raised: the waits stay in memory and the next save retries."""
        try:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"next_id": self._next_id, "waits": self._waits}, indent=2),
                           "utf-8")
            os.replace(tmp, self._path)
        except OSError:
            log.exception("could not save %s", self._path)

    def _changed(self):
        if self._on_change:
            self._on_change()
