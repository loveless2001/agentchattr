"""Prompt-cache keepalive for Claude Code agents parked on a registered wait.

Claude Code caches its prompt for 1 hour on a subscription (5 minutes on an API
key or past the usage limits). An agent that waits on a job (agent_waits.py)
for longer wakes to an expired cache and re-caches its whole context at 2x the
input price, while reading a warm cache costs 0.05x on Opus 5.5. So while an
agent waits, the server injects a tiny prompt shortly before its cache would
expire: a ping costs about 1/40 of a cold wake-up.

Through the CLI a ping is a real (short) turn, so it is sent only when it pays:
  - scope "waits": the agent has an active wait younger than max_hours;
  - the cache lifetime seen in its transcript is 1 hour (Codex reports none and
    is never pinged; a 5-minute cache cannot be kept by pings ~50 minutes apart);
  - it has been idle since its last model request for [idle_minutes, 58 min):
    later than that the cache is already gone and a ping would only pay the
    rewrite early;
  - its context is at least min_context_tokens, it is online and not mid-turn;
  - it was not pinged yet in this idle stretch.
"""

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

SCOPES = ("off", "waits")
CACHE_TTL_SECONDS = 3600
LATEST_PING_SECONDS = CACHE_TTL_SECONDS - 120  # leave the ping time to reach the API


@dataclass(frozen=True)
class KeepaliveConfig:
    scope: str = "waits"
    idle_minutes: int = 50
    min_context_tokens: int = 50_000
    max_hours: float = 24.0

    @classmethod
    def from_config(cls, cfg: dict | None) -> "KeepaliveConfig":
        """From the [cache_keepalive] table of config.toml (all keys optional)."""
        cfg = cfg if isinstance(cfg, dict) else {}
        scope = str(cfg.get("scope", cls.scope)).strip().lower()
        if scope not in SCOPES:
            log.warning("cache_keepalive.scope %r is not one of %s; keepalive is off", scope, SCOPES)
            scope = "off"
        try:
            idle = min(55, max(5, int(cfg.get("idle_minutes", cls.idle_minutes))))
            min_tokens = max(0, int(cfg.get("min_context_tokens", cls.min_context_tokens)))
            max_hours = max(0.0, float(cfg.get("max_hours", cls.max_hours)))
        except (TypeError, ValueError):
            log.warning("cache_keepalive has a non-numeric setting; using the defaults")
            return cls(scope=scope)
        return cls(scope, idle, min_tokens, max_hours)


def ping_text(notes: list[str]) -> str:
    return ("[agentchattr keepalive] Still waiting on: " + "; ".join(notes) + ". This message "
            "only refreshes your prompt cache. Reply with the single word ok, and nothing else: "
            "no tools, no chat, do not resume work.")


class CacheKeepalive:
    def __init__(self, config: KeepaliveConfig):
        self.config = config
        self._pinged_for: dict[str, float] = {}  # agent -> last request time it was pinged after

    def due(self, now: float, waits_by_agent: dict[str, list[dict]], cache_snapshot,
            ready) -> list[tuple[str, str]]:
        """[(agent, ping text)] to send now.

        cache_snapshot(agent) -> {"tokens", "ttl", "last_at"} or None
        (agent_session_state); ready(agent) -> online and not mid-turn.
        """
        cfg = self.config
        if cfg.scope == "off":
            return []
        pings = []
        for agent, waits in waits_by_agent.items():
            fresh = [w for w in waits if now - w["created_at"] < cfg.max_hours * 3600]
            snap = cache_snapshot(agent) if fresh else None
            if not snap or snap.get("ttl") != "1h" or not snap.get("last_at"):
                continue
            idle = now - snap["last_at"]
            if not cfg.idle_minutes * 60 <= idle < LATEST_PING_SECONDS:
                continue
            if (snap.get("tokens") or 0) < cfg.min_context_tokens:
                continue
            if self._pinged_for.get(agent) == snap["last_at"] or not ready(agent):
                continue
            self._pinged_for[agent] = snap["last_at"]
            pings.append((agent, ping_text([w["note"] for w in fresh])))
        return pings
