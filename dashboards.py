"""Per-channel live dashboards: named HTML/markdown blocks agents keep up to date.

One file per channel, data/dashboards/<channel>.json:
  {"channel", "version", "blocks": [{name, kind, html, editor, updated_at, version,
                                     requested_by}],
   "history": [inverse ops, newest last]}
The channel version only goes up; a block's version is the channel version at
its last write, so agents editing different blocks never conflict. History holds
inverse ops (the previous block / block list), not full copies, so Undo is cheap.
The store never interprets HTML; the browser renders it in a sandboxed iframe.
"""

import copy
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

MAX_BLOCK_BYTES = 100_000
MAX_BLOCKS = 30
MAX_TOTAL_BYTES = 1_000_000
HISTORY_LIMIT = 20
KINDS = {"html", "markdown"}
# Same rule as app.py's channel names; channel names become file names here.
_CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,19}$")
_BLOCK_RE = re.compile(r"^\w[\w .:\-]{0,39}$")


class DashboardError(ValueError):
    """A rejected dashboard change; the message is shown to agents and humans."""


class DashboardConflict(DashboardError):
    """base_version did not match; .block is the current block (or None)."""

    def __init__(self, message: str, block: dict | None):
        super().__init__(message)
        self.block = block


def _size(html: str) -> int:
    return len(html.encode("utf-8"))


class DashboardStore:
    def __init__(self, dir_path: str):
        self._dir = Path(dir_path)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._cache: dict[str, dict] = {}
        self._callbacks: list = []

    def on_change(self, cb):
        """Register cb(channel, snapshot), called after every real change."""
        self._callbacks.append(cb)

    def _fire(self, channel: str, snapshot: dict):
        for cb in self._callbacks:
            try:
                cb(channel, snapshot)
            except Exception:
                log.exception("dashboard on_change callback failed")

    # --- persistence ---

    def _path(self, channel: str) -> Path:
        return self._dir / f"{channel}.json"

    def _state(self, channel: str) -> dict:
        if not isinstance(channel, str) or not _CHANNEL_RE.fullmatch(channel):
            raise DashboardError(f"Invalid channel name: {channel!r}")
        state = self._cache.get(channel)
        if state is None:
            state = {"channel": channel, "version": 0, "blocks": [], "history": []}
            path = self._path(channel)
            if path.exists():
                try:
                    raw = json.loads(path.read_text("utf-8"))
                    state.update(version=int(raw.get("version", 0)),
                                 blocks=list(raw.get("blocks", [])),
                                 history=list(raw.get("history", [])))
                except (OSError, ValueError, TypeError, AttributeError):
                    log.exception("dashboard %s unreadable; starting empty", path)
                    try:
                        os.replace(path, path.with_name(path.name + ".bad"))
                    except OSError:
                        pass
            self._cache[channel] = state
        return state

    def _save(self, state: dict):
        """Temp file + fsync + swap: a crash mid-write never truncates the file."""
        path = self._path(state["channel"])
        tmp = path.with_name(path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError:
            log.exception("failed to save dashboard %s", path)

    @staticmethod
    def _snapshot(state: dict) -> dict:
        return {"channel": state["channel"], "version": state["version"],
                "blocks": copy.deepcopy(state["blocks"]),
                "can_undo": bool(state["history"])}

    def _commit(self, state: dict, op: dict | None) -> dict:
        """Record an inverse op (None for undo), save, and return a snapshot."""
        if op is not None:
            state["history"].append(op)
            del state["history"][:-HISTORY_LIMIT]
        self._save(state)
        return self._snapshot(state)

    @staticmethod
    def _find(state: dict, name: str) -> int:
        for i, b in enumerate(state["blocks"]):
            if b["name"] == name:
                return i
        return -1

    # --- reads ---

    def get(self, channel: str) -> dict:
        with self._lock:
            return self._snapshot(self._state(channel))

    # --- mutations ---

    def set_block(self, channel: str, name: str, html: str, editor: str,
                  kind: str = "html", base_version: int | None = None,
                  requested_by: str | None = None) -> dict:
        """Create or replace a block in place. base_version: None = no check,
        0 = must not exist yet, N = must equal the block's current version.
        requested_by: the human who asked for this block (verified by the caller);
        once set it stays with the block through later updates."""
        name = (name or "").strip()
        if not _BLOCK_RE.fullmatch(name):
            raise DashboardError("Block name must be 1-40 chars: letters, digits, space, _ . : -")
        if kind not in KINDS:
            raise DashboardError(f"kind must be one of: {', '.join(sorted(KINDS))}")
        if not isinstance(html, str):
            raise DashboardError("html must be a string")
        if _size(html) > MAX_BLOCK_BYTES:
            raise DashboardError(f"Block too large: max {MAX_BLOCK_BYTES} bytes")
        with self._lock:
            state = self._state(channel)
            idx = self._find(state, name)
            prev = state["blocks"][idx] if idx >= 0 else None
            requested_by = requested_by or (prev or {}).get("requested_by")
            if prev and prev["html"] == html and prev["kind"] == kind and \
                    prev.get("requested_by") == requested_by and \
                    base_version in (None, prev["version"]):
                # Unchanged: no version bump, broadcast or history (keeps the undo trail).
                return self._snapshot(state)
            if base_version is not None:
                current = prev["version"] if prev else 0
                if current != base_version:
                    raise DashboardConflict(
                        f"Block '{name}' is at version {current}, not {base_version}; "
                        "re-read it and retry.", copy.deepcopy(prev))
            if prev is None and len(state["blocks"]) >= MAX_BLOCKS:
                raise DashboardError(f"Dashboard is full: max {MAX_BLOCKS} blocks")
            total = sum(_size(b["html"]) for b in state["blocks"])
            total += _size(html) - (_size(prev["html"]) if prev else 0)
            if total > MAX_TOTAL_BYTES:
                raise DashboardError(f"Dashboard too large: max {MAX_TOTAL_BYTES} bytes in all")
            state["version"] += 1
            block = {"name": name, "kind": kind, "html": html, "editor": editor,
                     "updated_at": time.time(), "version": state["version"],
                     "requested_by": requested_by}
            if prev is None:
                idx = len(state["blocks"])
                state["blocks"].append(block)
            else:
                state["blocks"][idx] = block
            snap = self._commit(state, {"op": "set", "by": editor, "at": time.time(),
                                        "index": idx, "name": name, "prev": prev})
        self._fire(channel, snap)
        return snap

    def remove_block(self, channel: str, name: str, editor: str) -> dict:
        with self._lock:
            state = self._state(channel)
            idx = self._find(state, name)
            if idx < 0:
                raise DashboardError(f"No block named '{name}'")
            prev = state["blocks"].pop(idx)
            state["version"] += 1
            snap = self._commit(state, {"op": "remove", "by": editor, "at": time.time(),
                                        "index": idx, "name": name, "prev": prev})
        self._fire(channel, snap)
        return snap

    def clear(self, channel: str, editor: str) -> tuple[dict, int]:
        """Remove every block. Returns (snapshot, removed count); no-op when empty."""
        with self._lock:
            state = self._state(channel)
            removed = len(state["blocks"])
            if not removed:
                return self._snapshot(state), 0
            prev_blocks, state["blocks"] = state["blocks"], []
            state["version"] += 1
            snap = self._commit(state, {"op": "clear", "by": editor, "at": time.time(),
                                        "prev_blocks": prev_blocks})
        self._fire(channel, snap)
        return snap, removed

    def undo(self, channel: str, editor: str) -> dict:
        """Reverse the newest change. Restored blocks get the new channel version."""
        with self._lock:
            state = self._state(channel)
            if not state["history"]:
                raise DashboardError("Nothing to undo.")
            op = state["history"].pop()
            state["version"] += 1
            ver, blocks = state["version"], state["blocks"]
            if op["op"] == "clear":
                state["blocks"] = [dict(b, version=ver) for b in op["prev_blocks"]]
            else:
                idx = self._find(state, op["name"])
                if idx >= 0:
                    blocks.pop(idx)
                if op["prev"] is not None:
                    pos = min(op["index"], len(blocks)) if op["op"] == "remove" or idx < 0 \
                        else idx
                    blocks.insert(pos, dict(op["prev"], version=ver))
            log.info("dashboard %s: %s undid %s by %s", channel, editor, op["op"], op.get("by"))
            snap = self._commit(state, None)
        self._fire(channel, snap)
        return snap

    def rename(self, old: str, new: str):
        """Follow a channel rename: move the file and the cached state."""
        with self._lock:
            if not (_CHANNEL_RE.fullmatch(old or "") and _CHANNEL_RE.fullmatch(new or "")):
                return
            state = self._cache.pop(old, None)
            self._cache.pop(new, None)
            src, dst = self._path(old), self._path(new)
            try:
                # A leftover file under the new name (e.g. a deleted channel's) must
                # not survive: the renamed channel's dashboard replaces it.
                if src.exists():
                    os.replace(src, dst)
                elif dst.exists():
                    dst.unlink()
            except OSError:
                log.exception("failed to move dashboard %s -> %s", src, dst)
            if state is not None:
                state["channel"] = new
                self._cache[new] = state
                self._save(state)
