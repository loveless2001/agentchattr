"""Per-channel summary tree over chat history (OptMem-style memory).

The chat log itself is the memory. Every BLOCK_MESSAGES consecutive chat
messages form a leaf block that is compressed into one line; each pair of
neighbouring blocks is merged into a parent line, recursively, so old history
collapses into a few lines while recent history stays detailed.

This module does bookkeeping only. Compression (LLM calls) is done by
summary_compressor_worker.py, which pulls jobs via next_job()/complete_job().
Text output for agents (header, zoom, recall) lives in summary_tree_views.py.

State per channel in <dir>/<channel>.json:
    {"start_id": int,
     "levels": [[{"lo": id, "hi": id, "text": str | None, "ids": [...]}, ...], ...]}
levels[0] holds leaves (with their message ids); levels[k + 1][j] merges
levels[k][2j] and levels[k][2j + 1]. text=None means "needs (re)compression".
Blocks are keyed by message ids, so deleting a message never shifts blocks; it
only marks the leaf containing it, and that leaf's ancestors, stale.

A channel is initialized lazily by ensure() (when an agent starts in, or first
reads, the channel). Only then are up to `backfill_days` of history indexed.
"""

import copy
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import summary_tree_views as views

log = logging.getLogger(__name__)

BLOCK_MESSAGES = 16
RETRY_SECONDS = 300          # per-block retry delay after a failed compression
BREAKER_FAILURES = 3         # consecutive failures that pause all compression
BREAKER_MAX_SECONDS = 3600   # pause doubles per further failure, up to this
# Same rule as app.py's channel names; channel names become file names here.
_CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,19}$")


class SummaryStore:
    def __init__(self, dir_path: str, store, cfg: dict | None = None):
        cfg = cfg or {}
        self._dir = Path(dir_path)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._store = store
        self._backfill_days = float(cfg.get("backfill_days", 30))
        self._read_lines = int(cfg.get("read_lines", 12))
        self._lock = threading.RLock()
        self._state: dict[str, dict] = {}
        self._claimed: set[tuple] = set()  # jobs being compressed right now
        self._dirty: set[tuple] = set()    # claimed jobs invalidated mid-flight
        self._retry_at: dict[tuple, float] = {}
        self._fail_streak = 0        # consecutive CLI failures (any block)
        self._paused_until = 0.0     # circuit breaker: CLI down / logged out
        self._work_callbacks: list = []
        # False when no compressor runs (disabled / CLI missing): no trees are
        # built and no header is shown, but recall and zoom-by-id still work.
        self.active = True
        self._load()

    # ------------------------------------------------------------ persistence

    def _load(self):
        for path in self._dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text("utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("levels"), list):
                    self._state[path.stem] = raw
            except (OSError, json.JSONDecodeError):
                log.warning("Ignoring unreadable summary tree %s", path)

    def _save(self, channel: str):
        path = self._dir / f"{channel}.json"
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self._state[channel], ensure_ascii=False), "utf-8")
            os.replace(tmp, path)
        except OSError:
            log.exception("Failed to save summary tree for #%s", channel)

    # ------------------------------------------------------------ work signal

    def on_work(self, callback):
        """Register callback() fired when new compression work may exist."""
        self._work_callbacks.append(callback)

    def _notify(self):
        for cb in self._work_callbacks:
            try:
                cb()
            except Exception:
                log.exception("summary work callback failed")

    # ------------------------------------------------------------ indexing

    def ensure(self, channel: str):
        """Initialize the channel's tree (backfilling recent history) if needed,
        then index any new messages. Idempotent and cheap once initialized."""
        if not self.active or not _CHANNEL_RE.match(channel or ""):
            return
        with self._lock:
            created = channel not in self._state
            if created:
                if not self._store.get_recent(1, channel=channel):
                    return  # empty or unknown channel: nothing to remember yet
                cutoff = time.time() - self._backfill_days * 86400
                recent = [m for m in views.chat_only(self._store.get_since(-1, channel=channel))
                          if m.get("timestamp", 0) >= cutoff]
                if recent:
                    start_id = recent[0]["id"]
                else:  # nothing in the window: start after the latest message
                    start_id = self._store.get_recent(1, channel=channel)[-1]["id"] + 1
                self._state[channel] = {"start_id": start_id, "levels": []}
            grew = self._sync(channel)
            if created or grew:
                self._save(channel)
        if grew:
            self._notify()

    def on_message(self, msg: dict):
        """Store callback: index new chat messages of initialized channels."""
        channel = msg.get("channel", "general")
        if not self.active or msg.get("type", "chat") != "chat" or channel not in self._state:
            return
        with self._lock:
            if channel not in self._state or not self._sync(channel):
                return
            self._save(channel)
        self._notify()

    def unsummarized(self, channel: str) -> list[dict]:
        """Chat messages after the last leaf (not yet in any block)."""
        st = self._state.get(channel)
        if not st:
            return []
        leaves = st["levels"][0] if st["levels"] else []
        after = leaves[-1]["hi"] if leaves else st["start_id"] - 1
        return views.chat_only(self._store.get_since(after, channel=channel))

    def _sync(self, channel: str) -> bool:
        """Cut full leaf blocks from new messages; add parent placeholders."""
        levels = self._state[channel]["levels"]
        fresh = self.unsummarized(channel)
        grew = False
        while len(fresh) >= BLOCK_MESSAGES:
            chunk, fresh = fresh[:BLOCK_MESSAGES], fresh[BLOCK_MESSAGES:]
            if not levels:
                levels.append([])
            levels[0].append({"lo": chunk[0]["id"], "hi": chunk[-1]["id"], "text": None,
                              "ids": [m["id"] for m in chunk]})
            grew = True
        k = 0
        while k < len(levels) and len(levels[k]) >= 2:
            if k + 1 == len(levels):
                levels.append([])
            upper = levels[k + 1]
            while len(levels[k]) >= 2 * (len(upper) + 1):
                a, b = levels[k][2 * len(upper)], levels[k][2 * len(upper) + 1]
                upper.append({"lo": a["lo"], "hi": b["hi"], "text": None})
            k += 1
        return grew

    # ------------------------------------------------------------ invalidation

    def _invalidate(self, channel: str, k: int, block: dict):
        block["text"] = None
        key = (channel, k, block["lo"], block["hi"])
        self._retry_at.pop(key, None)
        if key in self._claimed:
            self._dirty.add(key)

    def _forget_jobs(self, channel: str):
        """Drop job bookkeeping for a channel whose tree is reset or moved."""
        self._dirty.update(key for key in self._claimed if key[0] == channel)
        self._retry_at = {k: v for k, v in self._retry_at.items() if k[0] != channel}

    def on_delete(self, msg_ids: list[int]):
        """Store callback: deleted messages make their leaf and its ancestors stale."""
        gone = set(msg_ids)
        if not gone:
            return
        touched = False
        with self._lock:
            for channel, st in self._state.items():
                levels = st["levels"]
                hit = [i for i, leaf in enumerate(levels[0] if levels else [])
                       if gone.intersection(leaf.get("ids", ()))]
                for i in hit:
                    levels[0][i]["ids"] = [x for x in levels[0][i]["ids"] if x not in gone]
                    for k in range(len(levels)):
                        if (i >> k) < len(levels[k]):
                            self._invalidate(channel, k, levels[k][i >> k])
                if hit:
                    self._save(channel)
                    touched = True
        if touched:
            self._notify()

    def reset(self, channel: str):
        """Channel was /clear-ed: forget its tree and start over from now on."""
        with self._lock:
            if channel not in self._state:
                return
            self._forget_jobs(channel)
            # Visible history starts after the clear marker, so 0 is safe.
            self._state[channel] = {"start_id": 0, "levels": []}
            self._save(channel)

    def rename(self, old: str, new: str):
        with self._lock:
            if old not in self._state or not _CHANNEL_RE.match(new or ""):
                return
            self._forget_jobs(old)
            self._state[new] = self._state.pop(old)
            self._save(new)
            try:
                (self._dir / f"{old}.json").unlink()
            except OSError:
                pass

    # ------------------------------------------------------------ jobs

    def next_job(self) -> dict | None:
        """Claim the most useful pending compression: lowest level first, then
        newest block (recent detail matters most to a newly arriving agent)."""
        now = time.time()
        with self._lock:
            if now < self._paused_until:
                return None
            best = None
            for channel, st in self._state.items():
                for k, level in enumerate(st["levels"]):
                    for j, block in enumerate(level):
                        key = (channel, k, block["lo"], block["hi"])
                        if (block["text"] is not None or key in self._claimed
                                or self._retry_at.get(key, 0) > now):
                            continue
                        kids = st["levels"][k - 1][2 * j:2 * j + 2] if k else []
                        if any(c["text"] is None for c in kids):
                            continue
                        rank = (k, -block["hi"])
                        if best is None or rank < best[0]:
                            best = (rank, key, block, kids)
            if best is None:
                return None
            _, key, block, kids = best
            self._claimed.add(key)
            job = {"key": key, "channel": key[0], "level": key[1]}
            if kids:
                job["parts"] = [{"lo": c["lo"], "hi": c["hi"], "text": c["text"]} for c in kids]
            else:
                ids = set(block["ids"])
                job["messages"] = [m for m in self._store.get_since(block["lo"] - 1, channel=key[0])
                                   if m["id"] in ids]
            return job

    def complete_job(self, job: dict, text: str | None):
        """Store a compression result (None = failed, retry later)."""
        key = job["key"]
        channel, k, lo, hi = key
        with self._lock:
            self._claimed.discard(key)
            if key in self._dirty:
                self._dirty.discard(key)
                return  # source changed mid-flight; the block is still pending
            if text is None:
                now = time.time()
                self._retry_at[key] = now + RETRY_SECONDS
                self._fail_streak += 1
                if self._fail_streak >= BREAKER_FAILURES:
                    pause = min(BREAKER_MAX_SECONDS,
                                RETRY_SECONDS * 2 ** (self._fail_streak - BREAKER_FAILURES))
                    self._paused_until = now + pause
                    log.warning("Summary CLI failed %d times in a row; pausing compression for %ds",
                                self._fail_streak, pause)
                return
            self._fail_streak = 0
            self._retry_at.pop(key, None)
            st = self._state.get(channel)
            level = st["levels"][k] if st and k < len(st["levels"]) else []
            block = next((b for b in level if b["lo"] == lo and b["hi"] == hi), None)
            if block is None or block["text"] is not None:
                return
            block["text"] = text
            self._save(channel)
        self._notify()

    # ------------------------------------------------------------ agent views

    def _snapshot(self, channel: str, ensure: bool) -> dict | None:
        if ensure:
            self.ensure(channel)
        with self._lock:
            st = self._state.get(channel)
            return copy.deepcopy(st) if st else None

    def render(self, channel: str) -> str:
        """Summary header for a fresh agent ('' when there is nothing yet)."""
        if not self.active:
            return ""
        st = self._snapshot(channel, ensure=True)
        if not st:
            return ""
        return views.render_header(channel, st, self.unsummarized(channel),
                                   self._store, self._read_lines)

    def zoom(self, channel: str, block: str) -> str:
        st = self._snapshot(channel, ensure=True) or {"start_id": 0, "levels": []}
        return views.zoom(channel, st, block, self._store)

    def recall(self, channel: str, query: str) -> str:
        st = self._snapshot(channel, ensure=False) or {"start_id": 0, "levels": []}
        msgs = views.chat_only(self._store.get_since(-1, channel=channel))
        return views.recall(st, msgs, query)
