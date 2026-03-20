"""Agent trigger — writes to queue files picked up by visible worker terminals."""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class AgentTrigger:
    def __init__(self, registry, data_dir: str = "./data"):
        self._registry = registry
        self._data_dir = Path(data_dir)

    def is_available(self, name: str) -> bool:
        return self._registry.is_registered(name)

    def get_status(self) -> dict:
        from mcp_bridge import is_online, is_active, get_role
        instances = self._registry.get_all()
        status = {
            name: {
                "available": is_online(name),
                "busy": is_active(name),
                "label": info["label"],
                "color": info["color"],
                "role": get_role(name),
            }
            for name, info in instances.items()
        }

        # The UI renders one pill per base family, while runtime instances may be
        # channel-scoped (for example, "codex-general"). Publish an aggregated
        # family status as well so the existing pill design still reflects the
        # live state of any instance in that family.
        family_cfg = self._registry.get_agent_config()
        for family_name, cfg in family_cfg.items():
            family_status = status.setdefault(
                family_name,
                {
                    "available": False,
                    "busy": False,
                    "label": cfg["label"],
                    "color": cfg["color"],
                    "role": get_role(family_name),
                },
            )
            family_status["label"] = cfg["label"]
            family_status["color"] = cfg["color"]

        for name, info in instances.items():
            family_name = info["base"]
            family_status = status.get(family_name)
            if not family_status:
                continue
            family_status["available"] = family_status["available"] or is_online(name)
            family_status["busy"] = family_status["busy"] or is_active(name)

        return status

    async def trigger(self, agent_name: str, message: str = "", channel: str = "general",
                      job_id: int | None = None, **kwargs):
        """Write to the agent's queue file. The worker terminal picks it up."""
        queue_file = self._data_dir / f"{agent_name}_queue.jsonl"
        self._data_dir.mkdir(parents=True, exist_ok=True)

        import time
        entry = {
            "sender": message.split(":")[0].strip() if ":" in message else "?",
            "text": message,
            "time": time.strftime("%H:%M:%S"),
            "channel": channel,
        }
        custom_prompt = kwargs.get("prompt", "")
        if isinstance(custom_prompt, str) and custom_prompt.strip():
            entry["prompt"] = custom_prompt.strip()
        if job_id is not None:
            entry["job_id"] = job_id

        with open(queue_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        log.info("Queued @%s trigger (ch=%s, job=%s): %s", agent_name, channel, job_id, message[:80])

    def trigger_sync(self, agent_name: str, message: str = "", channel: str = "general",
                     job_id: int | None = None, **kwargs):
        """Synchronous version of trigger — writes to queue file without async."""
        queue_file = self._data_dir / f"{agent_name}_queue.jsonl"
        self._data_dir.mkdir(parents=True, exist_ok=True)

        import time
        entry = {
            "sender": message.split(":")[0].strip() if ":" in message else "?",
            "text": message,
            "time": time.strftime("%H:%M:%S"),
            "channel": channel,
        }
        custom_prompt = kwargs.get("prompt", "")
        if isinstance(custom_prompt, str) and custom_prompt.strip():
            entry["prompt"] = custom_prompt.strip()
        if job_id is not None:
            entry["job_id"] = job_id

        with open(queue_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        log.info("Queued @%s trigger (ch=%s, job=%s): %s", agent_name, channel, job_id, message[:80])
