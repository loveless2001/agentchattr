"""Incremental readers for agent CLI transcripts (Claude Code, Codex).

A TranscriptTail follows one growing JSONL file and hands out complete lines.
A parser turns those lines into:
  - context usage: tokens in the model's context after the latest turn;
  - compaction events: the CLI summarized its own conversation.

Claude Code (~/.claude/projects/<cwd>/<session>.jsonl):
  assistant lines carry message.usage; context = input + cache writes +
  cache reads + output. A compaction is a system line with subtype
  compact_boundary and compactMetadata {trigger, preTokens, postTokens}.
Codex (~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl):
  event_msg/token_count lines carry info.last_token_usage.total_tokens and
  info.model_context_window; a compaction is a line of type "compacted".
"""

import json
import re
from pathlib import Path

# On attach, only the end of an existing transcript is scanned for the
# current context size (transcripts reach tens of MB).
TAIL_SCAN_BYTES = 4 * 1024 * 1024
_CODEX_COMPACTED = re.compile(r'"type"\s*:\s*"compacted"')


class TranscriptTail:
    """Follows a growing JSONL file and yields complete lines only."""

    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self._partial = b""

    def read_existing_tail(self, max_bytes: int = TAIL_SCAN_BYTES) -> list[str]:
        """Read the last max_bytes of the file (whole lines) and move to its end."""
        try:
            size = self.path.stat().st_size
            start = max(0, size - max_bytes)
            with open(self.path, "rb") as f:
                f.seek(start)
                data = f.read(size - start)
        except OSError:
            return []
        self.offset = start + len(data)
        if start > 0:  # the first line is cut; drop it
            newline = data.find(b"\n")
            data = data[newline + 1:] if newline >= 0 else b""
        return self._split(data)

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


class ClaudeTranscriptParser:
    """Claude Code transcript lines -> context usage and compactions."""

    def __init__(self):
        self.context_tokens: int | None = None
        self.model = ""

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
                tokens = (_int(usage.get("input_tokens"))
                          + _int(usage.get("cache_creation_input_tokens"))
                          + _int(usage.get("cache_read_input_tokens"))
                          + _int(usage.get("output_tokens")))
                if tokens:
                    self.context_tokens = tokens
                    self.model = model or self.model
        return None


class CodexRolloutParser:
    """Codex rollout lines -> context usage, context window and compactions."""

    def __init__(self):
        self.context_tokens: int | None = None
        self.context_window: int | None = None
        self.model = ""

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
        last = info.get("last_token_usage") or {}
        tokens = _int(last.get("total_tokens")) or (
            _int(last.get("input_tokens")) + _int(last.get("output_tokens")))
        if tokens:
            self.context_tokens = tokens
        window = _int(info.get("model_context_window"))
        if window:
            self.context_window = window
        return None


def make_parser(provider: str):
    if provider == "claude":
        return ClaudeTranscriptParser()
    if provider == "codex":
        return CodexRolloutParser()
    raise ValueError(f"no transcript parser for {provider!r}")
