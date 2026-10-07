"""Incremental readers for agent CLI transcripts (Claude Code, Codex).

A TranscriptTail follows one growing JSONL file and hands out complete lines.
A parser turns those lines into:
  - context usage: tokens in the model's context after the latest turn;
  - prompt-cache statistics (agent_cache_stats.CacheStats);
  - compaction events: the CLI summarized its own conversation.

Claude Code (~/.claude/projects/<cwd>/<session>.jsonl):
  assistant lines carry message.usage; context = input + cache writes +
  cache reads + output. One response is logged as one line per content block,
  each repeating its usage, so cache statistics count each message.id once.
  usage.cache_creation tells the cache lifetime in use (ephemeral_1h / _5m).
  A compaction is a system line with subtype compact_boundary and
  compactMetadata {trigger, preTokens, postTokens}.
Codex (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl):
  event_msg/token_count lines carry info.last_token_usage (input_tokens
  includes cached_input_tokens), the cumulative info.total_token_usage and
  info.model_context_window; a compaction is a line of type "compacted".
"""

import json
import re
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from agent_cache_stats import CacheStats

_SEEN_IDS_KEPT = 256  # Claude message ids remembered for de-duplication
_CODEX_COMPACTED = re.compile(r'"type"\s*:\s*"compacted"')


class TranscriptTail:
    """Follows a growing JSONL file and yields complete lines only."""

    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self._partial = b""

    def read_existing(self):
        """Yield the complete lines already in the file, streaming (transcripts
        reach tens of MB); once exhausted, read_new() follows from the end."""
        try:
            f = open(self.path, "rb")
        except OSError:
            return
        with f:
            for raw in f:
                self.offset += len(raw)  # kept current, so a reader that stops early resumes here
                if not raw.endswith(b"\n"):  # still being written
                    self._partial = raw
                    break
                if raw.strip():
                    yield raw[:-1].decode("utf-8", "replace")

    def read_new(self) -> list[str]:
        """Lines appended since the last read."""
        try:
            size = self.path.stat().st_size
            if size < self.offset:  # truncated or replaced: start over
                self.offset, self._partial = 0, b""
            if size == self.offset:
                return []
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read(size - self.offset)
        except OSError:
            return []
        self.offset += len(data)
        return self._split(data)

    def _split(self, data: bytes) -> list[str]:
        data = self._partial + data
        lines = data.split(b"\n")
        self._partial = lines.pop()  # incomplete last line (b"" if data ended in \n)
        return [line.decode("utf-8", "replace") for line in lines if line.strip()]


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _timestamp(value) -> float | None:
    """Epoch seconds from a transcript's ISO-8601 timestamp."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _claude_cache_ttl(usage: dict) -> str | None:
    creation = usage.get("cache_creation")
    if not isinstance(creation, dict):
        return None
    if _int(creation.get("ephemeral_1h_input_tokens")):
        return "1h"
    if _int(creation.get("ephemeral_5m_input_tokens")):
        return "5m"
    return None


class ClaudeTranscriptParser:
    """Claude Code transcript lines -> context usage, cache statistics, compactions."""

    def __init__(self):
        self.context_tokens: int | None = None
        self.model = ""
        self.cache = CacheStats()
        self._seen_ids: OrderedDict[str, None] = OrderedDict()

    def _first_sighting(self, message_id) -> bool:
        if not message_id:
            return True
        if message_id in self._seen_ids:
            return False
        self._seen_ids[message_id] = None
        if len(self._seen_ids) > _SEEN_IDS_KEPT:
            self._seen_ids.popitem(last=False)
        return True

    def feed(self, line: str) -> dict | None:
        """Update context usage from one line; return a compaction event or None."""
        is_usage = '"usage"' in line
        is_compact = "compact_boundary" in line
        if not (is_usage or is_compact):
            return None
        try:
            entry = json.loads(line)
        except ValueError:
            return None
        if not isinstance(entry, dict):
            return None

        if (is_compact and entry.get("type") == "system"
                and entry.get("subtype") == "compact_boundary"):
            meta = entry.get("compactMetadata") or {}
            post = meta.get("postTokens")
            if isinstance(post, int):
                self.context_tokens = post
            return {
                "trigger": meta.get("trigger"),
                "pre_tokens": meta.get("preTokens"),
                "post_tokens": post,
            }

        if entry.get("type") == "assistant" and not entry.get("isSidechain"):
            message = entry.get("message") or {}
            usage = message.get("usage") if isinstance(message, dict) else None
            model = message.get("model", "") if isinstance(message, dict) else ""
            if isinstance(usage, dict) and model != "<synthetic>":
                cached = _int(usage.get("cache_read_input_tokens"))
                written = _int(usage.get("cache_creation_input_tokens"))
                prompt = _int(usage.get("input_tokens")) + written + cached
                tokens = prompt + _int(usage.get("output_tokens"))
                if tokens:
                    self.context_tokens = tokens
                    self.model = model or self.model
                if prompt and self._first_sighting(message.get("id")):
                    self.cache.add_turn(prompt, cached, written, _timestamp(entry.get("timestamp")),
                                        _claude_cache_ttl(usage))
        return None


class CodexRolloutParser:
    """Codex rollout lines -> context usage and window, cache statistics, compactions."""

    def __init__(self):
        self.context_tokens: int | None = None
        self.context_window: int | None = None
        self.model = ""
        self.cache = CacheStats()
        self._last_turn_key: tuple | None = None

    def feed(self, line: str) -> dict | None:
        head = line[:300]  # the type comes first; compacted lines can be huge
        if _CODEX_COMPACTED.search(head):
            return {"trigger": None, "pre_tokens": self.context_tokens, "post_tokens": None}
        if '"token_count"' not in head:
            return None
        try:
            entry = json.loads(line)
        except ValueError:
            return None
        payload = entry.get("payload") if isinstance(entry, dict) else None
        info = payload.get("info") if isinstance(payload, dict) else None
        if not isinstance(info, dict):
            return None
        last = info.get("last_token_usage")
        last = last if isinstance(last, dict) else {}
        tokens = _int(last.get("total_tokens")) or (
            _int(last.get("input_tokens")) + _int(last.get("output_tokens")))
        if tokens:
            self.context_tokens = tokens
        total = info.get("total_token_usage")
        self._count_turn(entry, last, total if isinstance(total, dict) else {})
        window = _int(info.get("model_context_window"))
        if window:
            self.context_window = window
        return None

    def _count_turn(self, entry: dict, last: dict, total: dict):
        """token_count is re-emitted with unchanged numbers; a new request moves them."""
        key = (_int(total.get("total_tokens")), _int(last.get("total_tokens")))
        if not _int(last.get("input_tokens")) or key == self._last_turn_key:
            return
        self._last_turn_key = key
        self.cache.add_turn(_int(last.get("input_tokens")), _int(last.get("cached_input_tokens")),
                            _int(last.get("cache_write_input_tokens")),
                            _timestamp(entry.get("timestamp")))
        if _int(total.get("input_tokens")):
            self.cache.set_totals(_int(total.get("input_tokens")),
                                  _int(total.get("cached_input_tokens")),
                                  _int(total.get("cache_write_input_tokens")))


def make_parser(provider: str):
    if provider == "claude":
        return ClaudeTranscriptParser()
    if provider == "codex":
        return CodexRolloutParser()
    raise ValueError(f"no transcript parser for {provider!r}")
