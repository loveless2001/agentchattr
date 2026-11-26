"""MCP tools that let agents read and edit a channel's live dashboard.

Registered by mcp_bridge (_ALL_TOOLS). Edits post nothing to chat and wake no
one; the browser gets a websocket "dashboard" event through the store callback.
"""

import time

from mcp.server.fastmcp import Context

from dashboards import DashboardConflict, DashboardError

DASHBOARD_INSTRUCTIONS = (
    "Each channel has a live dashboard humans watch beside the chat. Use it for state that changes "
    "over time — progress, status, checklists, result tables — not for conversation. Keep one block "
    "per topic and update it in place with chat_dashboard_set (pass base_version from "
    "chat_dashboard_read when others may edit that block); remove stale blocks with "
    "chat_dashboard_remove. Dashboard edits post nothing to chat and wake no one — mention results "
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
        lines.append(f"--- {b['name']} · v{b['version']} · {b['editor']} · "
                     f"{relative_age(b['updated_at'], now)} · {b['kind']}")
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


def chat_dashboard_set(sender: str, block: str, html: str, channel: str = "general",
                       kind: str = "html", base_version: int | None = None,
                       ctx: Context | None = None) -> str:
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
        channel: Channel whose dashboard to edit
        kind: "html" or "markdown"
        base_version: Optional. The block's version from chat_dashboard_read; the edit is
            refused if someone changed it since. 0 = only create, fail if it exists.
    """
    b = _bridge()
    sender, err = b._resolve_tool_identity(sender, ctx, field_name="sender", required=True)
    if err:
        return err
    if err := _check_channel(channel):
        return err
    try:
        snap = b.dashboards.set_block(channel, block, html, sender, kind=kind,
                                      base_version=base_version)
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
    ver = next(x["version"] for x in snap["blocks"] if x["name"] == block.strip())
    return (f"Set block '{block.strip()}' (v{ver}) on #{channel} dashboard "
            f"({len(snap['blocks'])} blocks, v{snap['version']}).")


def chat_dashboard_remove(sender: str, block: str, channel: str = "general",
                          ctx: Context | None = None) -> str:
    """Remove a stale block from a channel's live dashboard.

    Args:
        block: Name of the block to remove
        channel: Channel whose dashboard to edit
    """
    b = _bridge()
    sender, err = b._resolve_tool_identity(sender, ctx, field_name="sender", required=True)
    if err:
        return err
    if err := _check_channel(channel):
        return err
    try:
        snap = b.dashboards.remove_block(channel, block.strip(), sender)
    except DashboardError as e:
        return f"Error: {e}"
    return f"Removed block '{block.strip()}' from #{channel} dashboard ({len(snap['blocks'])} left)."
