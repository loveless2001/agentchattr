"""Background workers that write channel summary lines with a headless LLM CLI.

Pulls jobs from SummaryStore (summaries.py) and answers each with one line:
a leaf job compresses one chat message too long to keep verbatim, a merge job
compresses two neighbouring stretches whose lines no longer fit together. Prompts live in summary_compressor_prompts.py.
Nothing is posted to chat.

The default CLI is Codex with the Luna model, run non-interactively. Chat text
is untrusted input, so the CLI runs with a read-only sandbox, no user config
(no MCP servers, plugins or memories), no saved session, in an empty working
directory, and the prompt frames the messages as data. Override the command
with [summaries].command in config.toml; the prompt is sent on stdin and the
summary is read from stdout.
"""

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from summary_compressor_prompts import NODE_BYTES, leaf_prompt, merge_prompt, retry_prompt
from summary_tree_views import DELETED

log = logging.getLogger(__name__)

DEFAULT_COMMAND = [
    "codex", "exec", "-m", "gpt-6-luna", "-c", 'model_reasoning_effort="low"',
    "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
    "-s", "read-only", "--color", "never", "-",
]
TIMEOUT_SECONDS = 180
IDLE_POLL_SECONDS = 60      # also retries failed jobs once their backoff ends
TRIES = 5                   # attempts per line to get under ACCEPT_BYTES
# NODE_BYTES is what the prompt asks for, but Luna mostly lands 5-40% over it
# and rarely gets under on a retry; a line this close ends the retries.
ACCEPT_BYTES = 600
# NODE_BYTES is a target: a stubborn line keeps its shortest try, a few bytes
# over. A line never gets cut; one this far over counts as a failed job.
MAX_LINE_BYTES = 2 * NODE_BYTES


def flatten(raw: str) -> str:
    """CLI output as one line, without quotes wrapping the whole line."""
    text = re.sub(r"\s+", " ", raw or "").strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in "`\"'":
        text = text[1:-1].strip()
    return text


class SummaryCompressor:
    def __init__(self, summaries, work_dir: Path, cfg: dict | None = None,
                 human=lambda: "user"):
        cfg = cfg or {}
        self._summaries = summaries
        self._human = human  # () -> the human's chat name; their words rank first
        self._cmd = list(cfg.get("command") or DEFAULT_COMMAND)
        self._workers = max(1, int(cfg.get("workers", 4)))
        self._work_dir = Path(work_dir)
        self._cv = threading.Condition()
        self._generation = 0  # bumped by kick(); avoids lost wake-ups

    def start(self) -> bool:
        exe = shutil.which(self._cmd[0])
        if not exe:
            log.warning("Channel summaries disabled: '%s' not found on PATH", self._cmd[0])
            return False
        self._cmd[0] = exe
        self._work_dir.mkdir(parents=True, exist_ok=True)
        self._summaries.on_work(self.kick)
        for i in range(self._workers):
            threading.Thread(target=self._loop, daemon=True,
                             name=f"summary-compressor-{i}").start()
        self.kick()
        return True

    def kick(self):
        with self._cv:
            self._generation += 1
            self._cv.notify_all()

    def _loop(self):
        while True:  # a worker must never die: log, back off, keep going
            try:
                self._step()
            except Exception:
                log.exception("Summary worker error")
                time.sleep(30)

    def _step(self):
        with self._cv:
            seen = self._generation
        job = self._summaries.next_job()
        if job is None:
            with self._cv:
                if self._generation == seen:
                    self._cv.wait(timeout=IDLE_POLL_SECONDS)
            return
        text, cli_failed = None, True
        try:
            text, cli_failed = self._compress(job)
        except Exception:
            log.exception("Summary compression failed for %s", job["key"])
        self._summaries.complete_job(job, text, cli_failed)

    def _compress(self, job: dict) -> tuple[str | None, bool]:
        """Ask for the line up to TRIES times, feeding back how far over it is;
        keep the shortest whole answer. Returns (line, cli_failed): line None
        = failed, the store retries later; cli_failed = no answer at all."""
        context, human = job.get("context", []), self._human() or "user"
        if "parts" in job:
            prompt = merge_prompt(job["channel"], context, job["parts"], human)
        elif job.get("message"):
            prompt = leaf_prompt(job["channel"], context, job["message"], human)
        else:
            return DELETED, False  # deleted after the leaf was indexed
        tries = []
        for _ in range(TRIES):
            line = flatten(self._run(prompt) or "")
            if not line:
                break  # CLI error or empty answer: settle for what we have
            if any(t.startswith(line.rstrip(" .,;:…")) for t in tries):
                continue  # just an earlier try cut off: never keep cut text
            tries.append(line)
            if len(line.encode()) <= ACCEPT_BYTES:
                break
            prompt = retry_prompt(prompt, line)
        best = min(tries, key=lambda t: len(t.encode()), default=None)
        if best is not None and len(best.encode()) > MAX_LINE_BYTES:
            log.warning("Summary line for %s still %d bytes after %d tries",
                        job["key"], len(best.encode()), len(tries))
            return None, False
        return best, best is None

    def _run(self, prompt: str) -> str | None:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            proc = subprocess.run(
                self._cmd, input=prompt, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=TIMEOUT_SECONDS,
                cwd=self._work_dir, creationflags=flags,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("Summary CLI failed to run: %s", exc)
            return None
        if proc.returncode != 0:
            log.warning("Summary CLI exited %s: %s", proc.returncode, (proc.stderr or "")[-500:])
            return None
        return proc.stdout
