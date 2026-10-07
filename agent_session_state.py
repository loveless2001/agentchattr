"""Server-side state for agent CLI sessions, fed by wrapper session events.

Wrappers follow their agent CLI's transcript and report what happens to the
agent's context (see agent_session_monitor.py / agent_crash_recovery.py) via
POST /api/agent_session/{name}. This module keeps the per-agent result:

  - context usage (tokens in the model's context, its window, compactions)
    and prompt-cache statistics, shown on the agent's status pill;
  - "summary pending": after a compaction the agent still remembers the chat,
    only less of it, so its next channel read keeps the cursor (no message is
    delivered twice) but starts with the channel summary to re-ground it.

Events:
  context  {tokens, window, model?, cache?}
                                         update context usage / cache statistics
                                         (cache: see agent_cache_stats.CacheStats.report)
  compact  {pre_tokens?, post_tokens?}   summary on the next channel read
  clear    {}                            new empty session: reset read cursors
  restart  {exit_code, crashed, resumed} fresh: reset cursors; crash: notice
  attention {reason: resume_dialog, tmux_session?}
                                         the CLI is stuck at Claude's resume dialog:
                                         ask a human to answer it (prompts are held)
"""

import re
import threading
import time

_lock = threading.Lock()
_sessions: dict[str, dict] = {}   # agent name -> context info
_summary_pending: set[str] = set()

_TMUX_SESSION = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")

# Smallest change in context usage (fraction of the window) worth a status broadcast.
_BROADCAST_STEP = 0.01


def _int_or_none(value) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _clean_cache(raw) -> dict | None:
    """Wrapper-reported cache statistics, reduced to known fields and safe values."""
    if not isinstance(raw, dict):
        return None
    last = raw.get("last") if isinstance(raw.get("last"), dict) else {}
    clean = {k: _int_or_none(raw.get(k)) or 0
             for k in ("prompt", "cached", "written", "turns", "cold_turns")}
    at = last.get("at")
    clean["last"] = {
        **{k: _int_or_none(last.get(k)) or 0 for k in ("prompt", "cached", "written")},
        "at": float(at) if isinstance(at, (int, float)) and at > 0 else None,
        "cold": bool(last.get("cold")),
    }
    clean["ttl"] = raw.get("ttl") if raw.get("ttl") in ("1h", "5m") else None
    return clean


def _pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def set_context(name: str, tokens: int | None, window: int | None, model: str = "",
                cache: dict | None = None) -> bool:
    """Record context usage (and cache statistics). Returns True when the change
    is worth broadcasting."""
    if tokens is None:
        return False
    cache = _clean_cache(cache)
    with _lock:
        info = _sessions.setdefault(name, {"compactions": 0})
        old_tokens, old_window = info.get("tokens"), info.get("window")
        old_cold = (info.get("cache") or {}).get("cold_turns")
        info["tokens"] = tokens
        if window:
            info["window"] = window
        if model:
            info["model"] = model
        if cache:
            info["cache"] = cache
        info["updated_at"] = time.time()
        new_window = info.get("window")
    if old_tokens is None or old_window != new_window:
        return True
    if cache and old_cold is not None and cache["cold_turns"] != old_cold:
        return True  # a cache miss is news
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
    cache = info.get("cache")
    if cache and cache["prompt"]:
        last = cache["last"]
        view["cache"] = {
            "session_pct": _pct(cache["cached"], cache["prompt"]),
            "last_pct": _pct(last["cached"], last["prompt"]),
            "turns": cache["turns"],
            "cold_turns": cache["cold_turns"],
            "last_cold": last["cold"],
            "last_at": last["at"],
            "ttl": cache["ttl"],
        }
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


def _attention_notice(name: str, body: dict) -> str:
    reason = body.get("reason")
    if reason != "resume_dialog":
        raise ValueError(f"unknown attention reason: {reason!r}")
    session = str(body.get("tmux_session") or "")
    where = (f" Open its terminal with `tmux attach -t {session}` and pick an option."
             if _TMUX_SESSION.match(session) else " Open its terminal and pick an option.")
    return (f"{name} is waiting at Claude's \"resume from summary / full session\" prompt, "
            f"which could not be answered automatically. Messages for it are held until "
            f"it is answered.{where}")


def apply_event(name: str, body: dict, *, reset_cursors, post_notice) -> bool:
    """Apply one wrapper-reported session event for agent `name`.

    reset_cursors(name) forgets the agent's read cursors (mcp_bridge);
    post_notice(text) posts a system message in the agent's channel.
    Returns True when the agent's status should be re-broadcast.
    """
    event = str(body.get("event", "")).strip().lower()

    if event == "context":
        return set_context(name, _int_or_none(body.get("tokens")),
                           _int_or_none(body.get("window")), str(body.get("model") or ""),
                           body.get("cache"))

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

    if event == "attention":
        post_notice(_attention_notice(name, body))
        return False

    raise ValueError(f"unknown session event: {event!r}")
