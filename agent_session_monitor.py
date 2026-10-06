"""Wrapper-side monitor that follows the agent CLI's live transcript.

It finds the transcript of the CLI the wrapper launched, reads what the CLI
appends, and reports to the server (via report_fn):
  - context usage (event "context"), when it moves or once a minute;
  - compactions (event "compact");
  - a new empty session while the CLI keeps running, e.g. /clear (event "clear").
It also remembers the current session id, so a crashed CLI can be resumed
(agent_crash_recovery.py).

Finding the transcript:
  - Claude Code: a SessionStart hook (agent_session_hook.py) appends
    {session_id, transcript_path, source} to `events_file` on every startup,
    resume, /clear and compaction.
  - Codex: the CLI keeps its rollout file open, so it is found among the open
    files (/proc/<pid>/fd) of the processes in the agent's tmux pane. Linux
    only; elsewhere Codex is simply not tracked.
"""

import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

from agent_transcript_readers import TranscriptTail, make_parser

log = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = ("claude", "codex")
# Claude Code transcripts do not record the model's context window.
DEFAULT_CONTEXT_WINDOWS = {"claude": 1_000_000}
SESSION_EVENTS_ENV = "AGENTCHATTR_SESSION_EVENTS"
_HOOK_SCRIPT = Path(__file__).resolve().with_name("agent_session_hook.py")
POLL_SECONDS = 3.0
REPORT_EVERY_SECONDS = 60.0   # re-send context usage (server state is in memory)
_CODEX_ROLLOUT = re.compile(r"/\.codex/sessions/.*/rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                            r"[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")


def install_claude_session_hook(config_dir: Path, instance_name: str) -> tuple[Path, Path]:
    """Write a Claude Code --settings file whose SessionStart hook records each
    session start in an events file. Returns (settings file, events file); the
    events file must reach the CLI's env as SESSION_EVENTS_ENV."""
    config_dir.mkdir(parents=True, exist_ok=True)
    events_file = config_dir / f"{instance_name}-session-events.jsonl"
    events_file.write_text("", "utf-8")  # sessions of an earlier wrapper are not ours
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(_HOOK_SCRIPT))}"
    settings = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}
    settings_file = config_dir / f"{instance_name}-session-settings.json"
    settings_file.write_text(json.dumps(settings, indent=2) + "\n", "utf-8")
    return settings_file, events_file


def _pane_pids(tmux_session: str) -> list[int]:
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", tmux_session, "-F", "#{pane_pid}"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(p) for p in result.stdout.split() if p.isdigit()]


def _descendants(roots: list[int]) -> list[int]:
    """roots plus every process below them, from /proc/<pid>/stat parent ids."""
    children: dict[int, list[int]] = {}
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            with open(f"/proc/{entry.name}/stat", "rb") as f:
                stat = f.read().decode("utf-8", "replace")
            ppid = int(stat.rsplit(")", 1)[1].split()[1])  # field after "(comm) state"
        except (OSError, ValueError, IndexError):
            continue
        children.setdefault(ppid, []).append(int(entry.name))
    found, stack = [], list(roots)
    while stack:
        pid = stack.pop()
        found.append(pid)
        stack.extend(children.get(pid, []))
    return found


def _is_subagent_rollout(path: str) -> bool:
    """Codex sub-agents (e.g. the guardian reviewer) write their own rollouts;
    their session_meta has a structured source ({"subagent": ...})."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return True
    return re.search(rb'"source"\s*:\s*\{\s*"subagent"', head) is not None


def find_codex_rollouts(tmux_session: str) -> dict[str, str]:
    """{rollout path: session id} of the Codex rollouts open in the tmux pane."""
    found: dict[str, str] = {}
    if not os.path.isdir("/proc"):
        return found
    for pid in _descendants(_pane_pids(tmux_session)):
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            match = _CODEX_ROLLOUT.search(target)
            if match:
                found[target] = match.group(1)
    return found


class AgentSessionMonitor:
    def __init__(self, provider: str, *, report_fn, events_file: Path | None = None,
                 tmux_session: str = "", context_window: int | None = None,
                 poll_seconds: float = POLL_SECONDS):
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"unsupported provider {provider!r}")
        self.provider = provider
        self.report_fn = report_fn
        self.events_file = Path(events_file) if events_file else None
        self.tmux_session = tmux_session
        self.context_window = context_window
        self.poll_seconds = poll_seconds

        self._lock = threading.Lock()
        self._session_id = ""
        self._path = ""
        self._tail: TranscriptTail | None = None
        self._parser = None
        self._events_tail = TranscriptTail(self.events_file) if self.events_file else None
        self._new_launch = True          # next session found belongs to a new launch
        self._reset_requested = False    # drop the old launch's transcript (monitor thread)
        self._subagent_rollouts: dict[str, bool] = {}
        self._last_reported: tuple | None = None
        self._last_report_at = 0.0

    # -- called by the wrapper ------------------------------------------------

    @property
    def session_id(self) -> str:
        with self._lock:
            return self._session_id

    def mark_launch(self):
        """The CLI exited and will be relaunched: forget its session (a crash
        before the new one is known must not resume the old one), and do not
        take the new launch's first session for a /clear."""
        with self._lock:
            self._session_id = ""
            self._new_launch = True
            self._reset_requested = True

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="agent-session-monitor").start()

    # -- loop -----------------------------------------------------------------

    def _run(self):
        while True:
            try:
                self.poll()
            except Exception:
                log.exception("agent session monitor poll failed")
            time.sleep(self.poll_seconds)

    def poll(self):
        """One step: locate the transcript, read what is new, report."""
        with self._lock:
            reset, self._reset_requested = self._reset_requested, False
        if reset:  # the old launch's numbers must not be reported again
            self._path, self._tail, self._parser, self._last_reported = "", None, None, None
        located = self._locate()
        if located:
            path, session_id, cleared = located
            if path != self._path:
                self._attach(path, session_id, cleared)
        if not self._tail:
            return
        for line in self._tail.read_new():
            event = self._parser.feed(line)
            if event:
                self._report({"event": "compact", **event})
                self._last_reported = None  # usage changed: send it again
        self._report_context()

    def _locate(self) -> tuple[str, str, bool] | None:
        """(path, session id, cleared) of the current transcript, or None if unchanged."""
        if self.provider == "claude":
            located, cleared = None, False
            for line in self._events_tail.read_new() if self._events_tail else []:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                path, sid = record.get("transcript_path"), record.get("session_id")
                if path and sid:
                    located = (path, sid)
                    cleared = cleared or record.get("source") == "clear"
            return (*located, cleared) if located else None
        found = self._main_rollouts()
        if not found:
            return None
        # Several open (e.g. the old one after /new): the most recently written is live.
        path = max(found, key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)
        if path == self._path:
            return None
        with self._lock:
            cleared = bool(self._path) and not self._new_launch
        return path, found[path], cleared

    def _main_rollouts(self) -> dict[str, str]:
        found = find_codex_rollouts(self.tmux_session) if self.tmux_session else {}
        for path in found:
            if path not in self._subagent_rollouts:
                self._subagent_rollouts[path] = _is_subagent_rollout(path)
        return {p: sid for p, sid in found.items() if not self._subagent_rollouts[p]}

    def _attach(self, path: str, session_id: str, cleared: bool):
        with self._lock:
            self._path, self._session_id = path, session_id
            self._new_launch = False
        self._tail = TranscriptTail(path)
        self._parser = make_parser(self.provider)
        # The existing end of the file sets the current usage; old compactions
        # in it are history, not news.
        for line in self._tail.read_existing_tail():
            self._parser.feed(line)
        self._last_reported = None
        if cleared:
            self._report({"event": "clear", "session_id": session_id})

    def _report_context(self):
        if not self._parser:
            return
        tokens = self._parser.context_tokens
        if tokens is None:
            return
        window = getattr(self._parser, "context_window", None) or self.context_window
        current = (tokens, window)
        now = time.time()
        if current == self._last_reported and now - self._last_report_at < REPORT_EVERY_SECONDS:
            return
        self._report({"event": "context", "tokens": tokens, "window": window,
                      "model": getattr(self._parser, "model", ""),
                      "session_id": self.session_id})
        self._last_reported, self._last_report_at = current, now

    def _report(self, body: dict):
        try:
            self.report_fn(body)
        except Exception:
            log.debug("session event report failed", exc_info=True)
