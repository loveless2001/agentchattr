"""Per-channel summary tree over chat history (OptChat-style memory).

The chat log itself is the memory. The tree is purely binary (OptChat spec
§3): each chat message is a leaf with one line, and each pair of neighbouring
nodes is merged into a parent line, recursively, so old history collapses
into a few lines while recent history stays detailed.

Free nodes need no model call: a message that fits in NODE_BYTES is its own
leaf line, verbatim (`sender: text`), and a parent whose two children's lines
fit in NODE_BYTES together is just those lines. So short messages, the
human's above all, stay word for word until they are merged.

This module does bookkeeping only. Compression (LLM calls) is done by
summary_compressor_worker.py, which pulls jobs via next_job()/complete_job().
Text output for agents (header, zoom, recall) lives in summary_tree_views.py.

State per channel in <dir>/<channel>.json:
    {"version": TREE_VERSION, "start_id": int,
     "levels": [[{"lo": id, "hi": id, "text": str | None}, ...], ...]}
levels[0] holds leaves (lo == hi == the message id); levels[k + 1][j] merges
levels[k][2j] and levels[k][2j + 1]. text=None means "needs (re)compression".
Nodes are keyed by message ids, so deleting a message never shifts the tree:
its leaf becomes the free line DELETED and its ancestors are rewritten.
A tree is rebuilt from the chat log on load when it was written under
another TREE_VERSION, or when its leaves no longer match the log (a message
lost to a crash-torn line, or one restored by hand). Same-version lines whose
node covers the same messages are kept; an older version's line is shown to
agents as "old" until its node is rewritten (never used as compressor context).

Lines are written in history order (OptChat's rule): a node is compressed only
once every leaf before it has a line, so its compressor gets the summary up to
it as context.

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
from summary_compressor_prompts import NODE_BYTES

log = logging.getLogger(__name__)

TREE_VERSION = 3             # bump when the tree shape, line format or prompt changes
CONTEXT_BYTES = 32000        # summary given to a compressor as context
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
        self._read_bytes = int(cfg.get("read_bytes", 8000))
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
            except (OSError, json.JSONDecodeError):
                log.warning("Ignoring unreadable summary tree %s", path)
                continue
            if not (isinstance(raw, dict) and isinstance(raw.get("levels"), list)):
                continue
            if raw.get("version") == TREE_VERSION and self._matches_log(path.stem, raw):
                self._state[path.stem] = raw
                continue
            try:
                self._rebuild(path.stem, raw)
            except Exception:  # a malformed old tree must not stop the server
                log.exception("Could not rebuild summary tree %s; ignoring it", path)
                self._state.pop(path.stem, None)

    def _matches_log(self, channel: str, st: dict) -> bool:
        """True when the tree's leaves are exactly the log's chat messages up to
        its last leaf (deleted leaves aside): no message lost, none restored."""
        leaves = st["levels"][0] if st["levels"] else []
        if not leaves:
            return True
        last = leaves[-1]["hi"]
        logged = {m["id"] for m in views.chat_only(
            self._store.get_since(st["start_id"] - 1, channel=channel)) if m["id"] <= last}
        return logged == {leaf["lo"] for leaf in leaves if leaf["text"] != views.DELETED}

    def _rebuild(self, channel: str, old: dict):
        """Re-index a tree from the chat log. A new node at the same level and
        id range as a node of the same TREE_VERSION covers the same messages,
        so it keeps that line. Under another version, a new node covering
        exactly an old node's messages (e.g. a level-4 node and an old
        16-message leaf) shows the old line until it is rewritten."""
        same = old.get("version") == TREE_VERSION
        log.info("Summary tree #%s %s; rebuilding it", channel,
                 "does not match the chat log" if same else "is from another version")
        prev = {(k if same else None, b["lo"], b["hi"]): b
                for k, level in enumerate(old["levels"]) for b in level}
        self._state[channel] = self._new_tree(int(old.get("start_id", 0)))
        self._sync(channel)
        levels = self._state[channel]["levels"]
        # Messages deleted in the old tree but back in the log: lines written
        # while they were gone must not be kept.
        old_leaves = old["levels"][0] if same and old["levels"] else []
        back = {leaf["lo"] for leaf in old_leaves if leaf["text"] == views.DELETED} & {
            leaf["lo"] for leaf in (levels[0] if levels else [])}
        for k, level in enumerate(levels):
            for j, block in enumerate(level):
                was = prev.get((k if same else None, block["lo"], block["hi"]))
                if not was or block["text"] is not None:
                    continue
                if any(block["lo"] <= msg_id <= block["hi"] for msg_id in back):
                    continue
                if same and was["text"]:
                    block["text"] = was["text"]
                    _fill_free(levels, k, j)
                elif was.get("text") or was.get("old"):
                    block["old"] = was.get("text") or was.get("old")
        self._save(channel)

    @staticmethod
    def _new_tree(start_id: int) -> dict:
        return {"version": TREE_VERSION, "start_id": start_id, "levels": []}

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
                self._state[channel] = self._new_tree(start_id)
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

    def _sync(self, channel: str) -> bool:
        """Add a leaf per new chat message and a parent per new complete pair,
        then write the free lines among them."""
        st = self._state[channel]
        levels = st["levels"]
        after = levels[0][-1]["hi"] if levels and levels[0] else st["start_id"] - 1
        fresh = views.chat_only(self._store.get_since(after, channel=channel))
        if not fresh:
            return False
        if not levels:
            levels.append([])
        first = len(levels[0])
        for msg in fresh:
            line = views.message_line(msg)
            levels[0].append({"lo": msg["id"], "hi": msg["id"],
                              "text": line if _fits(line) else None})
        k = 0
        while k < len(levels) and len(levels[k]) >= 2:
            if k + 1 == len(levels):
                levels.append([])
            upper = levels[k + 1]
            while len(levels[k]) >= 2 * (len(upper) + 1):
                a, b = levels[k][2 * len(upper)], levels[k][2 * len(upper) + 1]
                upper.append({"lo": a["lo"], "hi": b["hi"], "text": None})
            k += 1
        for i in range(first, len(levels[0])):
            _fill_free(levels, 0, i)
        return True

    # ------------------------------------------------------------ invalidation

    def _invalidate(self, channel: str, k: int, block: dict):
        block["text"] = None
        block.pop("old", None)  # it may quote the deleted message
        key = (channel, k, block["lo"], block["hi"])
        self._retry_at.pop(key, None)
        if key in self._claimed:
            self._dirty.add(key)

    def _forget_jobs(self, channel: str):
        """Drop job bookkeeping for a channel whose tree is reset or moved."""
        self._dirty.update(key for key in self._claimed if key[0] == channel)
        self._retry_at = {k: v for k, v in self._retry_at.items() if k[0] != channel}

    def on_delete(self, msg_ids: list[int]):
        """Store callback: a deleted message's leaf becomes DELETED and its
        ancestors are rewritten (for free where the lines still fit)."""
        gone = set(msg_ids)
        if not gone:
            return
        touched = False
        with self._lock:
            for channel, st in self._state.items():
                levels = st["levels"]
                hit = [i for i, leaf in enumerate(levels[0] if levels else [])
                       if leaf["lo"] in gone]
                for i in hit:
                    for k in range(len(levels)):
                        if (i >> k) < len(levels[k]):
                            self._invalidate(channel, k, levels[k][i >> k])
                    levels[0][i]["text"] = views.DELETED
                for i in hit:
                    _fill_free(levels, 0, i)
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
            self._state[channel] = self._new_tree(0)
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

    def _gate(self, channel: str, leaves: list, now: float) -> int:
        """Index of the first leaf still to be written (len(leaves) if none).
        Nodes starting after it wait for it. A leaf backing off after a failed
        compression does not count, so one bad block cannot stall the tree."""
        for i, leaf in enumerate(leaves):
            if (leaf["text"] is None
                    and self._retry_at.get((channel, 0, leaf["lo"], leaf["hi"]), 0) <= now):
                return i
        return len(leaves)

    def next_job(self) -> dict | None:
        """Claim the next compression in history order: lowest level first, then
        oldest block, skipping nodes that start after the channel's gate. The
        job carries the summary up to the node as context."""
        now = time.time()
        with self._lock:
            if now < self._paused_until:
                return None
            best = None
            for channel, st in self._state.items():
                levels = st["levels"]
                gate = self._gate(channel, levels[0] if levels else [], now)
                for k, level in enumerate(levels):
                    for j, block in enumerate(level):
                        if (j << k) > gate:  # the node starts past the gate
                            break
                        key = (channel, k, block["lo"], block["hi"])
                        if (block["text"] is not None or key in self._claimed
                                or self._retry_at.get(key, 0) > now):
                            continue
                        kids = levels[k - 1][2 * j:2 * j + 2] if k else []
                        if any(c["text"] is None for c in kids):
                            continue
                        rank = (k, block["lo"])
                        if best is None or rank < best[0]:
                            best = (rank, key, block, kids, levels, j)
            if best is None:
                return None
            _, key, block, kids, levels, j = best
            self._claimed.add(key)
            k = key[1]
            # Context (spec §4.2): a leaf sees the lines before it; a merge, the
            # lines up to its end (its own halves included).
            leaf_end = (j + 1) << k if k else j
            job = {"key": key, "channel": key[0], "level": k,
                   "context": views.context_lines(levels, leaf_end, CONTEXT_BYTES)}
            if kids:
                job["parts"] = [c["text"] for c in kids]
            else:
                msg = self._store.get_by_id(block["lo"])
                job["message"] = dict(msg) if msg else None
            return job

    def complete_job(self, job: dict, text: str | None, cli_failed: bool = True):
        """Store a compression result (None = failed, retry later). Only a CLI
        failure counts toward the breaker: one message the model cannot fit
        must not pause every channel."""
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
                if not cli_failed:
                    return
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
            levels = st["levels"] if st else []
            level = levels[k] if k < len(levels) else []
            j = next((j for j, b in enumerate(level) if b["lo"] == lo and b["hi"] == hi), None)
            if j is None or level[j]["text"] is not None:
                return
            level[j]["text"] = text
            level[j].pop("old", None)
            _fill_free(levels, k, j)
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
        return views.render_header(channel, st, self._store, self._read_bytes)

    def zoom(self, channel: str, block: str) -> str:
        st = self._snapshot(channel, ensure=True) or {"start_id": 0, "levels": []}
        return views.zoom(channel, st, block, self._store)

    def recall(self, channel: str, query: str) -> str:
        msgs = views.chat_only(self._store.get_since(-1, channel=channel))
        return views.recall(msgs, query)


def _fits(text: str) -> bool:
    return len(text.encode()) <= NODE_BYTES


def _fill_free(levels: list, k: int, j: int):
    """Write the free ancestors of node (k, j): a parent whose two children's
    lines fit in NODE_BYTES together is just those lines, one under the other
    (OptChat's free nodes). Stops at the first parent that needs a model."""
    while k + 1 < len(levels) and (j >> 1) < len(levels[k + 1]):
        parent = levels[k + 1][j >> 1]
        a, b = levels[k][j & ~1], levels[k][j | 1]
        if parent["text"] is not None or a["text"] is None or b["text"] is None:
            return
        text = f"{a['text']}\n{b['text']}"
        if not _fits(text):
            return
        parent["text"] = text
        parent.pop("old", None)
        k, j = k + 1, j >> 1
