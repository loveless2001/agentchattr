"""End to end: per-channel dashboards on a real server (see test_e2e_server.Server).

An agent edits through MCP, the browser sees a ws "dashboard" event, the human
clears / deletes / undoes through REST. Agent edits post nothing to chat; a
human clear posts one system notice and wakes no agent.
"""

import asyncio
import json
import sys
import threading
import unittest
import urllib.error

import websockets

from test_e2e_server import Server


def collect_ws_events(server: Server, action, settle: float = 1.5) -> list[dict]:
    """Run action() while a browser ws is connected; return events after it."""
    async def run():
        async with websockets.connect(f"ws://127.0.0.1:{server.port}/ws?token={server.token}") as ws:
            while True:  # drain the initial state burst
                try:
                    await asyncio.wait_for(ws.recv(), 0.5)
                except asyncio.TimeoutError:
                    break
            done = threading.Event()
            threading.Thread(target=lambda: (action(), done.set()), daemon=True).start()
            events = []
            while True:
                try:
                    events.append(json.loads(await asyncio.wait_for(ws.recv(), settle)))
                except asyncio.TimeoutError:
                    if done.is_set():
                        return events
    return asyncio.run(run())


@unittest.skipIf(sys.platform == "win32", "e2e harness is POSIX-only")
class DashboardE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server()
        cls.server.start()
        cls.agent = cls.server.register()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def get(self, ch="general") -> dict:
        return json.loads(self.server.http("GET", f"/api/dashboards/{ch}"))

    def chat_count(self) -> int:
        return len(json.loads(self.server.http("GET", "/api/messages?channel=general&limit=500")))

    def status_of(self, method, path, **kw) -> int:
        try:
            self.server.http(method, path, **kw)
            return 200
        except urllib.error.HTTPError as e:
            e.close()
            return e.code

    def setUp(self):
        self.server.http("POST", "/api/dashboards/general/clear")

    def test_agent_set_reaches_browser_and_posts_nothing(self):
        before = self.chat_count()
        events = collect_ws_events(self.server, lambda: self.server.mcp(
            self.agent, "chat_dashboard_set", block="build", html="<div class='stat'><b>3</b></div>"))
        dash = [e for e in events if e.get("type") == "dashboard"]
        self.assertEqual(len(dash), 1)
        self.assertEqual(dash[0]["channel"], "general")
        block = dash[0]["data"]["blocks"][0]
        self.assertEqual((block["name"], block["editor"]), ("build", self.agent["name"]))
        self.assertEqual(self.get()["blocks"][0]["html"], "<div class='stat'><b>3</b></div>")
        self.server.mcp(self.agent, "chat_dashboard_remove", block="build")
        self.assertEqual(self.chat_count(), before)
        self.assertFalse([e for e in events if e.get("type") == "message"])

    def test_identity_comes_from_token(self):
        self.server.mcp(dict(self.agent, name="someone-else"), "chat_dashboard_set",
                        block="who", html="x")
        self.assertEqual(self.get()["blocks"][0]["editor"], self.agent["name"])

    def test_conflict_and_read(self):
        out = self.server.mcp(self.agent, "chat_dashboard_set", block="tests", html="- a",
                              kind="markdown")
        self.assertTrue(out.startswith("Set block 'tests'"), out)
        ver = self.get()["blocks"][0]["version"]
        self.server.mcp(self.agent, "chat_dashboard_set", block="tests", html="- b", base_version=ver)
        stale = self.server.mcp(self.agent, "chat_dashboard_set", block="tests", html="- c",
                                base_version=ver)
        self.assertTrue(stale.startswith("Error: conflict"), stale)
        self.server.mcp(self.agent, "chat_dashboard_set", block="other", html="<i>o</i>")
        text = self.server.mcp(self.agent, "chat_dashboard_read", block="tests")
        self.assertIn("--- tests · v", text)
        self.assertIn("- b", text)
        self.assertNotIn("<i>o</i>", text)
        self.assertIn("dashboard: 2 blocks (tests, other)",
                      self.server.mcp(self.agent, "chat_join", name=self.agent["name"]))

    def test_agents_edit_only_their_own_channel(self):
        # An unbound agent belongs to #general: no channel means #general, others are refused.
        ok = self.server.mcp(self.agent, "chat_dashboard_set", block="mine", html="x")
        self.assertIn("on #general dashboard", ok)
        for tool, extra in (("chat_dashboard_set", {"html": "y"}), ("chat_dashboard_remove", {})):
            out = self.server.mcp(self.agent, tool, block="mine", channel="other", **extra)
            self.assertIn("may only edit the #general dashboard, not #other", out)
        self.assertEqual([b["name"] for b in self.get()["blocks"]], ["mine"])
        self.assertIn("--- mine", self.server.mcp(self.agent, "chat_dashboard_read", channel="general"))

    def test_human_clear_notice_undo_and_delete(self):
        for name in ("a", "b"):
            self.server.mcp(self.agent, "chat_dashboard_set", block=name, html=name)
        queue = self.server.data / f"{self.agent['name']}_queue.jsonl"
        queued = queue.read_text().count("\n") if queue.exists() else 0
        res = json.loads(self.server.http("POST", "/api/dashboards/general/clear"))
        self.assertEqual(res["removed"], 2)
        msgs = json.loads(self.server.http("GET", "/api/messages?channel=general&limit=5"))
        self.assertEqual(msgs[-1]["sender"], "system")
        self.assertIn("cleared the dashboard (2 blocks)", msgs[-1]["text"])
        self.assertEqual(queue.read_text().count("\n") if queue.exists() else 0, queued)
        json.loads(self.server.http("POST", "/api/dashboards/general/undo"))
        self.assertEqual([b["name"] for b in self.get()["blocks"]], ["a", "b"])
        self.server.http("DELETE", "/api/dashboards/general/blocks/a")
        self.assertEqual([b["name"] for b in self.get()["blocks"]], ["b"])
        self.assertEqual(self.status_of("DELETE", "/api/dashboards/general/blocks/zzz"), 404)
        self.assertEqual(self.status_of("GET", "/api/dashboards/nope"), 404)

    def test_channel_clear_also_clears_dashboard(self):
        from test_rules_usability_e2e import browser_events
        self.server.mcp(self.agent, "chat_dashboard_set", block="a", html="a")
        browser_events(self.server, [{"type": "message", "text": "/clear", "channel": "general"}])
        self.assertEqual(self.get()["blocks"], [])
        self.server.http("POST", "/api/dashboards/general/undo")
        self.assertEqual([b["name"] for b in self.get()["blocks"]], ["a"])

    def test_requested_badge_is_verified_against_the_human(self):
        from test_rules_usability_e2e import browser_events
        say = lambda text: browser_events(self.server, [
            {"type": "message", "text": text, "channel": "general"}], settle=0.5)
        say("just thinking out loud, no mention here")
        out = self.server.mcp(self.agent, "chat_dashboard_set", block="r", html="x", requested=True)
        self.assertIn("Not marked as requested", out)
        self.assertIsNone(self.get()["blocks"][0]["requested_by"])
        say(f"@{self.agent['name'].split('-')[0]} please track the loss on the dashboard")
        out = self.server.mcp(self.agent, "chat_dashboard_set", block="r", html="y", requested=True)
        self.assertIn("Marked as per user's request", out)
        human = self.get()["blocks"][0]["requested_by"]
        self.assertTrue(human)
        self.assertIn(f"requested by {human}",
                      self.server.mcp(self.agent, "chat_dashboard_read", block="r"))

    def test_undo_with_nothing_left_is_409(self):
        while self.status_of("POST", "/api/dashboards/general/undo") == 200:
            pass
        self.assertEqual(self.status_of("POST", "/api/dashboards/general/undo"), 409)

    def test_mutations_are_human_only(self):
        self.assertEqual(self.status_of("POST", "/api/dashboards/general/clear",
                                        bearer=self.agent["token"]), 403)
        self.assertEqual(self.status_of("GET", "/api/dashboards/general", auth=False), 403)

    def test_popout_page(self):
        html = self.server.http("GET", "/dashboard/general", auth=False).decode()
        self.assertIn('__SESSION_TOKEN__="', html)
        self.assertIn('sandbox=""', html)
        self.assertEqual(self.status_of("GET", "/dashboard/Bad!", auth=False), 404)


if __name__ == "__main__":
    unittest.main()
