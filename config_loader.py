"""Shared config loader — creates/loads config.toml and merges config.local.toml.

Used by run.py, wrapper.py, and wrapper_api.py so the server and all
wrappers see the same agent definitions.
"""

import tomllib
import shutil
from pathlib import Path

ROOT = Path(__file__).parent


def load_config(root: Path | None = None) -> dict:
    """Load config.toml and merge config.local.toml if it exists.

    config.toml and config.local.toml are gitignored and intended for
    user-specific settings that should not be committed. If config.toml is
    missing, it is created from config.toml.example.
    Only the [agents] section is merged — local entries are added alongside
    (not replacing) the agents defined in config.toml.
    """
    root = root or ROOT
    config_path = root / "config.toml"
    if not config_path.exists():
        example_path = root / "config.toml.example"
        if not example_path.exists():
            raise FileNotFoundError(f"{config_path} not found and {example_path} is missing")
        shutil.copyfile(example_path, config_path)
        print(f"Created {config_path.name} from {example_path.name}")

    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    local_path = root / "config.local.toml"
    if local_path.exists():
        with open(local_path, "rb") as f:
            local = tomllib.load(f)
        
        # Merge [agents] section — local agents are added ONLY if they don't already exist.
        # This protects the "holy trinity" (claude, codex, gemini) from being overridden.
        local_agents = local.get("agents", {})
        config_agents = config.setdefault("agents", {})
        for name, agent_cfg in local_agents.items():
            if name not in config_agents:
                config_agents[name] = agent_cfg
            else:
                print(f"  Warning: Ignoring local agent '{name}' (already defined in config.toml)")

    return config
