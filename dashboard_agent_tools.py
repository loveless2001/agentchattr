"""MCP tools that let agents read and edit a channel's live dashboard.

Registered by mcp_bridge (_ALL_TOOLS). Edits post nothing to chat and wake no
one; the browser gets a websocket "dashboard" event through the store callback.
"""

import re
import time

from mcp.server.fastmcp import Context

from dashboards import DashboardConflict, DashboardError

DASHBOARD_INSTRUCTIONS = (
    "Each channel has a live dashboard humans watch beside the chat. Use it for state that changes "
    "over time — progress, status, checklists, result tables — not for conversation. Keep one block "
    "per topic and update it in place with chat_dashboard_set (pass base_version from "
    "chat_dashboard_read when others may edit that block); remove stale blocks with "
    "chat_dashboard_remove. When the human asked you for a block, pass requested=true: it is "
    "verified against their latest message and the block is badged 'per user's request'. "
    "You can read any channel's dashboard but only edit your own channel's. "
    "Dashboard edits post nothing to chat and wake no one — mention results "
    "that need attention in chat. Update at a sane rate (on real changes, not every second): "
    "each change is pushed to every open browser."
)


def _bridge():
    # Lazy: mcp_bridge imports this module; its globals are set by run.py later.
    import mcp_bridge
    return mcp_bridge


def relative_age(ts: float, now: float | None = None) -> str:
    secs = max(0, int((now or time.time()) - (ts or 0)))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit} ago"
    return "just now"


def render_for_agent(snap: dict, block: str = "", now: float | None = None) -> str:
    """Token-lean text view of a dashboard snapshot for chat_dashboard_read."""
    ch, blocks = snap["channel"], snap["blocks"]
    if block:
        blocks = [b for b in blocks if b["name"] == block]
        if not blocks:
            return f"No block '{block}' on #{ch} dashboard."
    elif not blocks:
        return f"#{ch} dashboard is empty."
    lines = [f"#{ch} dashboard v{snap['version']} ({len(snap['blocks'])} blocks). "
             "Pass base_version=<block v> to chat_dashboard_set to avoid overwriting others."]
    for b in blocks:
        req = f" · requested by {b['requested_by']}" if b.get("requested_by") else ""
        lines.append(f"--- {b['name']} · v{b['version']} · {b['editor']} · "
                     f"{relative_age(b['updated_at'], now)} · {b['kind']}{req}")
        lines.append(b["html"])
    return "\n".join(lines)


def join_hint(channel: str) -> str:
    """One line for chat_join when the channel's dashboard has blocks."""
    dashboards = _bridge().dashboards
    if not dashboards:
        return ""
    try:
        names = [b["name"] for b in dashboards.get(channel)["blocks"]]
    except DashboardError:
        return ""
    if not names:
        return ""
    shown = ", ".join(names[:5]) + (", …" if len(names) > 5 else "")
    return f"\n#{channel} dashboard: {len(names)} blocks ({shown}) — chat_dashboard_read to view."


def _home_channel(agent: str) -> str:
    """The channel an agent instance belongs to (binding, then name suffix, else general)."""
    import app
    return app._channel_for_agent_instance(agent)


def _resolve_edit_channel(sender: str, channel: str) -> tuple[str, str | None]:
    """Agents may only edit their own channel's dashboard; '' means that channel."""
    home = _home_channel(sender)
    channel = (channel or "").strip().lstrip("#") or home
    if channel != home:
        return channel, (f"Error: {sender} belongs to #{home} and may only edit the #{home} "
                         f"dashboard, not #{channel}. Ask in #{channel} instead.")
    return channel, _check_channel(channel)


def _verify_request(agent: str, channel: str) -> tuple[str | None, str]:
    """(human, "") when the human's latest message in channel @mentions agent
    (its own name, its base family, @all or @both); else (None, reason)."""
    b = _bridge()
    human = (b.room_settings or {}).get("username", "user")
    latest = next((m for m in reversed(b.store.get_recent(200, channel=channel))
                   if m.get("sender", "").lower() == human.lower()), None)
    if latest is None:
        return None, f"{human} has not written in #{channel}"
    inst = b.registry.get_instance(agent) if b.registry else None
    names = {agent, (inst or {}).get("base") or agent.split("-")[0], "all", "both"}
    pattern = r"@(" + "|".join(re.escape(n) for n in names if n) + r")\b"
    if not re.search(pattern, latest.get("text", ""), re.IGNORECASE):
        return None, f"{human}'s latest message in #{channel} does not @mention you"
    return human, ""


def _check_channel(channel: str) -> str | None:
    b = _bridge()
    if b.dashboards is None:
        return "Error: dashboards are not available."
    if channel not in (b.room_settings or {}).get("channels", []):
        return f"Error: unknown channel '#{channel}'."
    return None


def chat_dashboard_read(channel: str = "general", block: str = "") -> str:
    """Read a channel's live dashboard: every block (or one named block) with its
    version, last editor, age and HTML/markdown source.

    Args:
        channel: Channel whose dashboard to read
        block: Optional block name to read only that block
    """
    if err := _check_channel(channel):
        return err
    return render_for_agent(_bridge().dashboards.get(channel), block.strip())


def chat_dashboard_set(sender: str, block: str, html: str, channel: str = "",
                       kind: str = "html", base_version: int | None = None,
                       requested: bool = False, ctx: Context | None = None) -> str:
    """Create or replace one named block on a channel's live dashboard (humans see it
    update instantly beside the chat). Use for state that changes over time: progress,
    status, checklists, result tables. Update the same block in place; don't post chat.

    Static HTML/SVG only: scripts, forms, iframes and external resources are stripped or
    blocked; images only as data: URIs. Limits: 100KB per block, 30 blocks per channel.
    Built-in CSS classes:
      <div class="grid"> ... </div>                         responsive card grid
      <div class="stat"><b>42</b><span>label</span></div>   big number tile
      <div class="progress"><span style="width:40%"></span></div>
      <span class="badge ok|warn|err">text</span>
      <ul class="checklist"><li class="done">x</li><li>y</li></ul>
      plain <table> is styled; inline SVG works for charts.

    Args:
        block: Block name (1-40 chars), e.g. "training" or "test results"
        html: Block content (HTML fragment, or markdown when kind="markdown")
        channel: Your own channel (default). Agents can only edit their channel's dashboard.
        kind: "html" or "markdown"
        base_version: Optional. The block's version from chat_dashboard_read; the edit is
            refused if someone changed it since. 0 = only create, fail if it exists.
        requested: True only when the human asked you for this block. Verified against
            their latest message in the channel (it must @mention you); the block is then
            badged "per user's request" for good. Leave False for your own initiative.
    """
    b = _bridge()
    sender, err = b._resolve_tool_identity(sender, ctx, field_name="sender", required=True)
    if err:
        return err
    channel, err = _resolve_edit_channel(sender, channel)
    if err:
        return err
    requested_by, why_not = _verify_request(sender, channel) if requested else (None, "")
    try:
        snap = b.dashboards.set_block(channel, block, html, sender, kind=kind,
                                      base_version=base_version, requested_by=requested_by)
    except DashboardConflict as e:
        cur = e.block
        if cur is None:
            return (f"Error: conflict — block '{block}' no longer exists (you sent "
                    f"base_version={base_version}). Re-read with chat_dashboard_read and retry.")
        return (f"Error: conflict — block '{block}' is at v{cur['version']} (last edited by "
                f"{cur['editor']}). You sent base_version={base_version}. "
                "Re-read with chat_dashboard_read and retry.")
    except DashboardError as e:
        return f"Error: {e}"
    cur = next(x for x in snap["blocks"] if x["name"] == block.strip())
    note = " Marked as per user's request." if requested_by else ""
    if why_not:
        note = f" Not marked as requested: {why_not}."
    return (f"Set block '{block.strip()}' (v{cur['version']}) on #{channel} dashboard "
            f"({len(snap['blocks'])} blocks, v{snap['version']}).{note}")


def chat_dashboard_remove(sender: str, block: str, channel: str = "",
                          ctx: Context | None = None) -> str:
    """Remove a stale block from a channel's live dashboard.

    Args:
        block: Name of the block to remove
        channel: Your own channel (default). Agents can only edit their channel's dashboard.
    """
    b = _bridge()
    sender, err = b._resolve_tool_identity(sender, ctx, field_name="sender", required=True)
    if err:
        return err
    channel, err = _resolve_edit_channel(sender, channel)
    if err:
        return err
    try:
        snap = b.dashboards.remove_block(channel, block.strip(), sender)
    except DashboardError as e:
        return f"Error: {e}"
    return f"Removed block '{block.strip()}' from #{channel} dashboard ({len(snap['blocks'])} left)."
