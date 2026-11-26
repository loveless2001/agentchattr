"""REST endpoints for the per-channel dashboard: human-only view, clear, delete, undo.

The security middleware requires the browser session token here (agent Bearer
tokens are only accepted on /api/messages, /api/send and /api/rules/), so these
mutations are human-only. Agents edit through the MCP tools in mcp_bridge.py.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from dashboards import DashboardError

router = APIRouter()


def _app():
    # Lazy: app.py imports this module before its globals are configured.
    import app as _a
    return _a


def _check_channel(channel: str):
    """Return an error response for an unknown channel, else None."""
    a = _app()
    if a.dashboards is None:
        return JSONResponse({"error": "dashboards not configured"}, status_code=503)
    if not a._CHANNEL_NAME_RE.match(channel or "") or channel not in a.room_settings["channels"]:
        return JSONResponse({"error": f"unknown channel '{channel}'"}, status_code=404)
    return None


def _human() -> str:
    return _app().room_settings.get("username", "user")


@router.get("/api/dashboards/{channel}")
async def get_dashboard(channel: str):
    if err := _check_channel(channel):
        return err
    return _app().dashboards.get(channel)


@router.post("/api/dashboards/{channel}/clear")
async def clear_dashboard(channel: str):
    if err := _check_channel(channel):
        return err
    a = _app()
    user = _human()
    snap, removed = a.dashboards.clear(channel, user)
    if removed:
        # System sender: shown in the channel, never routed, so no agent wakes.
        a.store.add("system", f"{user} cleared the dashboard ({removed} block"
                    f"{'s' if removed != 1 else ''}). Undo is in the Dashboard panel.",
                    msg_type="system", channel=channel)
    return {"ok": True, "removed": removed, "dashboard": snap}


@router.delete("/api/dashboards/{channel}/blocks/{name}")
async def delete_dashboard_block(channel: str, name: str):
    if err := _check_channel(channel):
        return err
    try:
        snap = _app().dashboards.remove_block(channel, name, _human())
    except DashboardError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return {"ok": True, "dashboard": snap}


@router.post("/api/dashboards/{channel}/undo")
async def undo_dashboard(channel: str):
    if err := _check_channel(channel):
        return err
    try:
        snap = _app().dashboards.undo(channel, _human())
    except DashboardError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    return {"ok": True, "dashboard": snap}
