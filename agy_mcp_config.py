"""MCP config for the Antigravity CLI (agy), isolated per agent instance.

agy has no per-session MCP flag, env var or --settings key, and it does not
expand env vars in its config. It reads MCP servers from
$HOME/.gemini/config/mcp_config.json and reloads that file while running.

So each instance gets its own HOME: an overlay directory that mirrors the real
home through symlinks (sign-in, settings, conversations, ~/.gitconfig, ...)
except for one real file, .gemini/config/mcp_config.json, which points at that
instance's identity proxy. The wrapper launches agy with HOME set to it.
Several agy instances can then run at once without sharing an MCP entry, and
the user's real agy config is never touched.

Mirrored levels: $HOME/*, $HOME/.gemini/*, $HOME/.gemini/config/*. A new file
created at one of those levels lands in the overlay, not the real home.

Windows has no overlay (agy finds home via USERPROFILE, and symlinks need
Developer Mode); wrappers there are started by hand, one per agent, so the
wrapper writes the entry into the real config and removes it on exit.

File format, as written by `agy mcp add`:
    {"mcpServers": {"<name>": {"serverUrl": "<url>", "disabled": false}}}
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Path chain from HOME to the MCP config; each level is mirrored except the
# next item in the chain, which becomes a real directory (or the real file).
_CONFIG_CHAIN = (".gemini", "config", "mcp_config.json")


def _mirror_level(real_dir: Path, overlay_dir: Path, skip: str) -> None:
    """Symlink every entry of real_dir into overlay_dir except `skip`.

    Keeps real files the agent created in the overlay, replaces symlinks that
    point elsewhere, and drops symlinks whose real entry is gone.
    """
    overlay_dir.mkdir(parents=True, exist_ok=True)
    for entry in real_dir.iterdir():
        if entry.name == skip:
            continue
        link = overlay_dir / entry.name
        if link.is_symlink():
            if os.readlink(link) == str(entry):
                continue
            link.unlink()
        elif link.exists():
            continue  # a real file/dir created inside the overlay: keep it
        link.symlink_to(entry)
    for link in overlay_dir.iterdir():
        if link.is_symlink() and not link.exists():
            link.unlink()


def build_agy_home_overlay(overlay_home: Path, real_home: Path,
                           server_name: str, url: str) -> Path:
    """(Re)build overlay_home and point its agy MCP entry at url.

    Returns the overlay's mcp_config.json path. Other MCP servers from the
    real config are kept, so the instance still has them.
    """
    real_dir, overlay_dir = real_home, overlay_home
    for name in _CONFIG_CHAIN[:-1]:
        _mirror_level(real_dir, overlay_dir, skip=name)
        real_dir, overlay_dir = real_dir / name, overlay_dir / name
    _mirror_level(real_dir, overlay_dir, skip=_CONFIG_CHAIN[-1])

    # Start from the real config so the instance keeps the user's other servers
    config_file = overlay_dir / _CONFIG_CHAIN[-1]
    config_file.write_text(json.dumps(_read(real_dir / _CONFIG_CHAIN[-1])), "utf-8")
    return write_agy_mcp_entry(config_file, server_name, url)


def agy_mcp_config_path(home: Path) -> Path:
    return home.joinpath(*_CONFIG_CHAIN)


def write_agy_mcp_entry(config_file: Path, server_name: str, url: str) -> Path:
    """Point server_name at url in an agy MCP config, keeping other servers.

    Used directly on Windows, where there is no HOME overlay: the wrapper
    writes into the user's real config (single instance per agent there).
    """
    config_file.parent.mkdir(parents=True, exist_ok=True)
    data = _read(config_file)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers[server_name] = {"serverUrl": url, "disabled": False}
    data["mcpServers"] = servers
    config_file.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    return config_file


def remove_agy_mcp_entry(config_file: Path, server_name: str, url: str) -> bool:
    """Remove server_name if it still points at url. True when removed."""
    data = _read(config_file)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return False
    entry = servers.get(server_name)
    if not isinstance(entry, dict) or entry.get("serverUrl") != url:
        return False
    del servers[server_name]
    config_file.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
    return True


def _read(config_file: Path) -> dict:
    """Read the config; a missing, empty or unparseable file reads as {}."""
    try:
        data = json.loads(config_file.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
