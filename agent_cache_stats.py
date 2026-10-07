"""Prompt-cache statistics for one agent CLI session (Claude Code, Codex).

Both CLIs record, per model request, how much of the prompt the provider served
from its prompt cache. The transcript parsers (agent_transcript_readers.py)
normalize each request to:
  prompt  - every input token sent: uncached + cache writes + cache reads
  cached  - tokens read from the cache (billed at a small fraction of input)
  written - tokens written to the cache (Claude bills these above input price)

A turn is "cold" when a large prompt mostly missed the cache: typically the
first request after the cache expired (Claude Code: 1 hour of idle on a
subscription, 5 minutes on an API key; Codex: best effort, ~30-45 minutes).
"""

COLD_MIN_PROMPT = 30_000       # smaller prompts are cheap to resend anyway
COLD_MAX_CACHED_SHARE = 0.5    # cold = less than half of the prompt was cached


def is_cold(prompt: int, cached: int) -> bool:
    return prompt >= COLD_MIN_PROMPT and cached < prompt * COLD_MAX_CACHED_SHARE


class CacheStats:
    """Session totals plus the latest request, ready to report to the server."""

    def __init__(self):
        self.prompt = self.cached = self.written = 0
        self.turns = self.cold_turns = 0
        self.last: dict | None = None
        self.ttl: str | None = None   # Claude only: "1h" or "5m", the cache lifetime in use

    def add_turn(self, prompt: int, cached: int, written: int,
                 at: float | None = None, ttl: str | None = None):
        """Count one model request."""
        cold = is_cold(prompt, cached)
        self.prompt += prompt
        self.cached += cached
        self.written += written
        self.turns += 1
        self.cold_turns += cold
        self.last = {"prompt": prompt, "cached": cached, "written": written,
                     "at": at, "cold": cold}
        if ttl:  # a request that only read the cache keeps the lifetime seen before
            self.ttl = ttl

    def set_totals(self, prompt: int, cached: int, written: int):
        """Replace the summed totals with the CLI's own cumulative ones (Codex)."""
        self.prompt, self.cached, self.written = prompt, cached, written

    def report(self) -> dict | None:
        if not self.turns:
            return None
        return {"prompt": self.prompt, "cached": self.cached, "written": self.written,
                "turns": self.turns, "cold_turns": self.cold_turns, "ttl": self.ttl,
                "last": dict(self.last)}
