"""Server-side state for agent CLI sessions, fed by wrapper session events.

Wrappers follow their agent CLI's transcript and report what happens to the
agent's context (see agent_session_monitor.py / agent_crash_recovery.py) via
POST /api/agent_session/{name}. This module keeps the per-agent result:

  - context usage (tokens in the model's context, its window, compactions),
    shown on the agent's status pill;
  - "summary pending": after a compaction the agent still remembers the chat,
    only less of it, so its next channel read keeps the cursor (no message is
    delivered twice) but starts with the channel summary to re-ground it.

Events:
  context  {tokens, window, model?}      update context usage
  compact  {pre_tokens?, post_tokens?}   summary on the next channel read
  clear    {}                            new empty session: reset read cursors
  restart  {exit_code, crashed, resumed} fresh: reset cursors; crash: notice
"""

import threading
import time

_lock = threading.Lock()
_sessions: dict[str, dict] = {}   # agent name -> context info
_summary_pending: set[str] = set()

# Smallest change in context usage (fraction of the window) worth a status broadcast.
_BROADCAST_STEP = 0.01


def _int_or_none(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def set_context(name: str, tokens: int | None, window: int | None, model: str = "") -> bool:
    """Record context usage. Returns True when the change is worth broadcasting."""
    if tokens is None:
        return False
    with _lock:
        info = _sessions.setdefault(name, {"compactions": 0})
        old_tokens, old_window = info.get("tokens"), info.get("window")
        info["tokens"] = tokens
        if window:
            info["window"] = window
        if model:
            info["model"] = model
        info["updated_at"] = time.time()
        new_window = info.get("window")
    if old_tokens is None or old_window != new_window:
        return True
    if not new_window:
        return abs(tokens - old_tokens) >= 1000
    return abs(tokens - old_tokens) / new_window >= _BROADCAST_STEP


def note_compaction(name: str, pre_tokens: int | None, post_tokens: int | None):
    """Count a compaction; the post-compaction size becomes the current usage."""
    with _lock:
        info = _sessions.setdefault(name, {"compactions": 0})
        info["compactions"] = info.get("compactions", 0) + 1
        info["last_compact_at"] = time.time()
        if pre_tokens is not None:
            info["last_compact_from"] = pre_tokens
        if post_tokens is not None:
            info["tokens"] = post_tokens
        _summary_pending.add(name)


def take_summary_pending(name: str) -> bool:
    """True once after a compaction: the caller should prepend the summary."""
    with _lock:
        if name in _summary_pending:
            _summary_pending.discard(name)
            return True
        return False


def clear_summary_pending(name: str):
    with _lock:
        _summary_pending.discard(name)


def get_context(name: str) -> dict:
    """Public view for the status payload ({} when nothing was reported)."""
    with _lock:
        info = dict(_sessions.get(name) or {})
    tokens = info.get("tokens")
    if tokens is None:
        return {}
    window = info.get("window")
    view = {
        "tokens": tokens,
        "window": window,
        "pct": round(100 * tokens / window, 1) if window else None,
        "compactions": info.get("compactions", 0),
    }
    if info.get("last_compact_at"):
        view["last_compact_at"] = info["last_compact_at"]
    return view


def rename(old_name: str, new_name: str):
    with _lock:
        if old_name in _sessions:
            _sessions[new_name] = _sessions.pop(old_name)
        if old_name in _summary_pending:
            _summary_pending.discard(old_name)
            _summary_pending.add(new_name)


def forget(name: str):
    with _lock:
        _sessions.pop(name, None)
        _summary_pending.discard(name)


def _reset_session(name: str):
    """A new, empty session: old context numbers and pending summaries are void."""
    with _lock:
        _sessions.pop(name, None)
        _summary_pending.discard(name)


def _restart_notice(name: str, body: dict) -> str:
    code = _int_or_none(body.get("exit_code"))  # never echo free text into a system message
    how = f"exited with code {code}" if code is not None else "stopped unexpectedly"
    if body.get("resumed"):
        outcome = "restarted with its previous session resumed"
    else:
        outcome = ("restarted in a fresh session; its next read starts with the "
                   "channel summary")
    return f"{name}'s CLI {how} and was {outcome}."


def apply_event(name: str, body: dict, *, reset_cursors, post_notice) -> bool:
    """Apply one wrapper-reported session event for agent `name`.

    reset_cursors(name) forgets the agent's read cursors (mcp_bridge);
    post_notice(text) posts a system message in the agent's channel.
    Returns True when the agent's status should be re-broadcast.
    """
    event = str(body.get("event", "")).strip().lower()

    if event == "context":
        return set_context(name, _int_or_none(body.get("tokens")),
                           _int_or_none(body.get("window")), str(body.get("model") or ""))

    if event == "compact":
        note_compaction(name, _int_or_none(body.get("pre_tokens")),
                        _int_or_none(body.get("post_tokens")))
        return True

    if event == "clear":
        _reset_session(name)
        reset_cursors(name)
        return True

    if event == "restart":
        if not body.get("resumed"):
            _reset_session(name)
            reset_cursors(name)
        if body.get("crashed"):
            post_notice(_restart_notice(name, body))
        return True

    raise ValueError(f"unknown session event: {event!r}")
