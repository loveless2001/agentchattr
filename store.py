"""JSONL message persistence for the chat room with observer callbacks.

Supports clear markers: /clear inserts an internal marker instead of deleting
messages. Read methods return only messages after the latest marker per channel.
When the JSONL file grows past a size threshold, older hidden messages are
archived into numbered files.
"""

import json
import logging
import os
import time
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# Archive when active JSONL exceeds this size (bytes). Default 5 MB.
ARCHIVE_THRESHOLD_BYTES = 5 * 1024 * 1024
CLEAR_MARKER_TYPE = "clear_marker"


class MessageStore:
    def __init__(self, path: str):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._todos_path = self._path.parent / "todos.json"
        self._messages: list[dict] = []
        self._next_id: int = 0  # monotonically increasing, survives deletions
        self._todos: dict[int, str] = {}  # msg_id → "todo" | "done"
        self._lock = threading.Lock()
        self._callbacks: list = []  # called on each new message
        self._todo_callbacks: list = []  # called on todo changes
        self._delete_callbacks: list = []  # called on message deletion
        self.upload_dir = self._path.parent.parent / "uploads"  # Default fallback
        self._load()
        self._load_todos()

    def _load(self):
        if not self._path.exists():
            return
        max_id = -1
        with open(self._path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    # Preserve persisted ID; fall back to line number for legacy data
                    if "id" not in msg:
                        msg["id"] = i
                    if msg["id"] > max_id:
                        max_id = msg["id"]
                    self._messages.append(msg)
                except json.JSONDecodeError:
                    continue
        self._next_id = max_id + 1

    def on_message(self, callback):
        """Register a callback(msg) called whenever a message is added."""
        self._callbacks.append(callback)

    def _add_internal(self, msg: dict) -> dict:
        """Append a message to storage without firing observer callbacks.

        Used for internal bookkeeping entries (e.g. clear markers) that should
        be persisted but never broadcast to clients or agents.
        """
        with self._lock:
            msg["id"] = self._next_id
            msg["timestamp"] = time.time()
            msg["time"] = time.strftime("%H:%M:%S")
            self._next_id += 1
            self._messages.append(msg)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        return msg

    def add(self, sender: str, text: str, msg_type: str = "chat",
            attachments: list | None = None, reply_to: int | None = None,
            channel: str = "general",
            metadata: dict | None = None) -> dict:
        with self._lock:
            msg = {
                "id": self._next_id,
                "sender": sender,
                "text": text,
                "type": msg_type,
                "timestamp": time.time(),
                "time": time.strftime("%H:%M:%S"),
                "attachments": attachments or [],
                "channel": channel,
            }
            if reply_to is not None:
                msg["reply_to"] = reply_to
            if metadata:
                msg["metadata"] = metadata
            self._next_id += 1
            self._messages.append(msg)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())

        # Fire callbacks outside the lock
        for cb in self._callbacks:
            try:
                cb(msg)
            except Exception:
                pass

        # Check if archival is needed after adding a message
        self._maybe_archive()

        return msg

    def get_by_id(self, msg_id: int) -> dict | None:
        """Lookup any message by ID (including hidden/pre-marker messages)."""
        with self._lock:
            for m in self._messages:
                if m["id"] == msg_id:
                    return m
            return None

    def _visible_messages(self, channel: str | None = None) -> list[dict]:
        """Return messages visible after the latest clear marker per channel.

        Must be called while holding self._lock. Clear markers themselves are
        always excluded from the result.
        """
        msgs = self._messages
        if channel:
            msgs = [m for m in msgs if m.get("channel", "general") == channel]

        # Find the latest clear marker for the target channel(s)
        if channel:
            # Single channel: find last marker in that channel
            marker_idx = -1
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i].get("type") == CLEAR_MARKER_TYPE:
                    marker_idx = i
                    break
            if marker_idx >= 0:
                msgs = msgs[marker_idx + 1:]
        else:
            # All channels: find latest marker per channel, keep only post-marker
            latest_marker_id: dict[str, int] = {}
            for m in msgs:
                if m.get("type") == CLEAR_MARKER_TYPE:
                    ch = m.get("channel", "general")
                    latest_marker_id[ch] = m["id"]
            if latest_marker_id:
                msgs = [
                    m for m in msgs
                    if m.get("type") != CLEAR_MARKER_TYPE
                    and m["id"] > latest_marker_id.get(
                        m.get("channel", "general"), -1
                    )
                ]
            else:
                # No markers at all — just strip any marker type (defensive)
                msgs = [m for m in msgs if m.get("type") != CLEAR_MARKER_TYPE]

        # Final filter: never return clear markers to callers
        return [m for m in msgs if m.get("type") != CLEAR_MARKER_TYPE]

    def get_recent(self, count: int = 50, channel: str | None = None) -> list[dict]:
        """Get the most recent visible messages, respecting clear markers."""
        with self._lock:
            return list(self._visible_messages(channel)[-count:])

    def get_since(self, since_id: int = 0, channel: str | None = None) -> list[dict]:
        """Get visible messages after a given ID, respecting clear markers."""
        with self._lock:
            msgs = self._visible_messages(channel)
            return [m for m in msgs if m["id"] > since_id]

    def get_before(self, before_id: int, limit: int = 50,
                   channel: str | None = None) -> list[dict]:
        """Get older visible messages before a given ID (for backward pagination).

        Returns up to `limit` visible messages whose ID is strictly less than
        `before_id`, ordered oldest-to-newest (same as get_recent/get_since).
        """
        with self._lock:
            msgs = self._visible_messages(channel)
            older = [m for m in msgs if m["id"] < before_id]
            return older[-limit:]

    def delete(self, msg_ids: list[int]) -> list[int]:
        """Delete messages by ID. Returns list of IDs actually deleted."""
        deleted = []
        deleted_attachments = []
        with self._lock:
            for mid in msg_ids:
                for i, m in enumerate(self._messages):
                    if m["id"] == mid:
                        # Collect attachment files for cleanup
                        for att in m.get("attachments", []):
                            for key in ("url", "download_url", "markdown_url"):
                                url = att.get(key, "")
                                clean = url.split("?", 1)[0]
                                if clean.startswith("/uploads/"):
                                    deleted_attachments.append(clean.split("/")[-1])
                        # Remove any associated todo
                        if mid in self._todos:
                            del self._todos[mid]
                        self._messages.pop(i)
                        deleted.append(mid)
                        break
            if deleted:
                self._rewrite_jsonl()
                self._save_todos()

        # Clean up uploaded images outside the lock
        for filename in deleted_attachments:
            filepath = self.upload_dir / filename
            if filepath.exists():
                try:
                    filepath.unlink()
                except Exception:
                    pass

        # Fire callbacks
        for cb in self._delete_callbacks:
            try:
                cb(deleted)
            except Exception:
                pass

        return deleted

    def on_delete(self, callback):
        """Register a callback(ids) called when messages are deleted."""
        self._delete_callbacks.append(callback)

    def update_message(self, msg_id: int, updates: dict) -> dict | None:
        """Update fields on a message in-place. Returns the updated message or None."""
        with self._lock:
            for m in self._messages:
                if m["id"] == msg_id:
                    m.update(updates)
                    self._rewrite_jsonl()
                    return dict(m)
            return None

    def _rewrite_jsonl(self):
        """Rewrite the JSONL file from current in-memory messages."""
        with open(self._path, "w", encoding="utf-8") as f:
            for m in self._messages:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def clear(self, channel: str | None = None):
        """Insert a clear marker instead of deleting messages.

        Messages before the marker become invisible to get_recent/get_since
        but remain in the JSONL for archival. If channel is given, only that
        channel is cleared. If None, markers are inserted for every channel
        that has messages.
        """
        if channel:
            self._add_internal({
                "sender": "system",
                "text": "",
                "type": CLEAR_MARKER_TYPE,
                "channel": channel,
                "attachments": [],
            })
        else:
            # Insert a marker for each channel that has messages
            channels = set()
            with self._lock:
                for m in self._messages:
                    channels.add(m.get("channel", "general"))
            for ch in channels:
                self._add_internal({
                    "sender": "system",
                    "text": "",
                    "type": CLEAR_MARKER_TYPE,
                    "channel": ch,
                    "attachments": [],
                })

    def _maybe_archive(self):
        """Archive hidden (pre-clear-marker) messages when file gets too large.

        Moves messages that are invisible for their own channel into a numbered
        archive file. The active file keeps only visible messages + markers.
        """
        try:
            size = self._path.stat().st_size
        except OSError:
            return
        if size < ARCHIVE_THRESHOLD_BYTES:
            return

        with self._lock:
            # Build per-channel latest clear marker ID
            latest_marker_id: dict[str, int] = {}
            for m in self._messages:
                if m.get("type") == CLEAR_MARKER_TYPE:
                    ch = m.get("channel", "general")
                    latest_marker_id[ch] = m["id"]

            if not latest_marker_id:
                return  # No markers — nothing to archive

            # Split: archivable = hidden for their own channel
            keep: list[dict] = []
            archive: list[dict] = []
            for m in self._messages:
                ch = m.get("channel", "general")
                marker_id = latest_marker_id.get(ch, -1)
                if m["id"] <= marker_id and m.get("type") != CLEAR_MARKER_TYPE:
                    archive.append(m)
                elif m.get("type") == CLEAR_MARKER_TYPE and m["id"] <= marker_id and m["id"] != marker_id:
                    # Old markers (not the latest per channel) can be archived
                    archive.append(m)
                else:
                    keep.append(m)

            if not archive:
                return

            # Find next archive number
            stem = self._path.stem
            suffix = self._path.suffix
            parent = self._path.parent
            n = 1
            while (parent / f"{stem}.{n}{suffix}").exists():
                n += 1
            archive_path = parent / f"{stem}.{n}{suffix}"

            # Write archive file
            with open(archive_path, "w", encoding="utf-8") as f:
                for m in archive:
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())

            # Update active file with only kept messages
            self._messages = keep
            self._rewrite_jsonl()

            # Clean up todos that referenced archived messages
            archived_ids = {m["id"] for m in archive}
            changed = False
            for tid in list(self._todos.keys()):
                if tid in archived_ids:
                    del self._todos[tid]
                    changed = True
            if changed:
                self._save_todos()

            log.info("Archived %d messages to %s", len(archive), archive_path)

    def rename_channel(self, old_name: str, new_name: str):
        """Migrate all messages from old_name to new_name."""
        with self._lock:
            modified = False
            for m in self._messages:
                if m.get("channel") == old_name:
                    m["channel"] = new_name
                    modified = True
            if modified:
                self._rewrite_jsonl()

    def rename_sender(self, old_name: str, new_name: str) -> int:
        """Rename sender on all messages from old_name to new_name. Returns count updated."""
        with self._lock:
            count = 0
            for m in self._messages:
                if m.get("sender") == old_name:
                    m["sender"] = new_name
                    count += 1
            if count:
                self._rewrite_jsonl()
        return count

    def delete_channel(self, name: str):
        """Remove all messages belonging to a deleted channel."""
        with self._lock:
            original_len = len(self._messages)
            # Collect IDs of messages being removed so we can clean up their todos
            removed_ids = {m["id"] for m in self._messages if m.get("channel") == name}
            self._messages = [m for m in self._messages if m.get("channel") != name]
            if len(self._messages) != original_len:
                self._rewrite_jsonl()
                # Clean up todos that referenced deleted messages
                for tid in list(self._todos.keys()):
                    if tid in removed_ids:
                        del self._todos[tid]
                self._save_todos()

    # --- Todos ---

    def _load_todos(self):
        # Migrate old pins.json (list of ints) → todos.json (dict of id→status)
        old_pins = self._todos_path.parent / "pins.json"
        if old_pins.exists() and not self._todos_path.exists():
            try:
                ids = json.loads(old_pins.read_text("utf-8"))
                if isinstance(ids, list):
                    self._todos = {int(i): "todo" for i in ids}
                    self._save_todos()
                    old_pins.unlink()
            except Exception:
                pass

        if self._todos_path.exists():
            try:
                raw = json.loads(self._todos_path.read_text("utf-8"))
                self._todos = {int(k): v for k, v in raw.items()}
            except Exception:
                self._todos = {}

    def _save_todos(self):
        self._todos_path.write_text(
            json.dumps({str(k): v for k, v in self._todos.items()}, indent=2),
            "utf-8"
        )

    def on_todo(self, callback):
        """Register a callback(msg_id, status) called on todo changes.
        status is 'todo', 'done', or None (removed)."""
        self._todo_callbacks.append(callback)

    def _fire_todo(self, msg_id: int, status: str | None):
        for cb in self._todo_callbacks:
            try:
                cb(msg_id, status)
            except Exception:
                pass

    def add_todo(self, msg_id: int) -> bool:
        with self._lock:
            if not any(m["id"] == msg_id for m in self._messages):
                return False
            self._todos[msg_id] = "todo"
            self._save_todos()
        self._fire_todo(msg_id, "todo")
        return True

    def complete_todo(self, msg_id: int) -> bool:
        with self._lock:
            if msg_id not in self._todos:
                return False
            self._todos[msg_id] = "done"
            self._save_todos()
        self._fire_todo(msg_id, "done")
        return True

    def reopen_todo(self, msg_id: int) -> bool:
        with self._lock:
            if msg_id not in self._todos:
                return False
            self._todos[msg_id] = "todo"
            self._save_todos()
        self._fire_todo(msg_id, "todo")
        return True

    def remove_todo(self, msg_id: int) -> bool:
        with self._lock:
            if msg_id not in self._todos:
                return False
            del self._todos[msg_id]
            self._save_todos()
        self._fire_todo(msg_id, None)
        return True

    def get_todo_status(self, msg_id: int) -> str | None:
        return self._todos.get(msg_id)

    def get_todos(self) -> dict[int, str]:
        """Returns {msg_id: status} for all todos."""
        return dict(self._todos)

    def get_todo_messages(self, status: str | None = None) -> list[dict]:
        """Get todo messages, optionally filtered by status."""
        with self._lock:
            if status:
                ids = {k for k, v in self._todos.items() if v == status}
            else:
                ids = set(self._todos.keys())
            return [m for m in self._messages if m["id"] in ids]

    @property
    def last_id(self) -> int:
        with self._lock:
            return self._messages[-1]["id"] if self._messages else -1
