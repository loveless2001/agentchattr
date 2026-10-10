"""End-to-end tests for rules: the human's rules take effect at once, the
active-rule cap is reported instead of failing silently, and agents get the
active rules with their channel header whenever their context starts fresh.

Runs a real server (see test_e2e_server.Server) and talks to it the way the
browser (WebSocket) and an agent CLI (MCP, session events) do.
"""

import asyncio
import json
import sys
import unittest
import urllib.error

import websockets

from test_e2e_server import Server

RULES_HEADER = "ACTIVE RULES (approved by the human; follow them):"


def browser_events(server: Server, events: list[dict], settle: float = 1.0) -> list[dict]:
    """Send events over the browser WebSocket; return what came back after them."""
    async def run():
        async with websockets.connect(f"ws://127.0.0.1:{server.port}/ws?token={server.token}") as ws:
            # Drain the initial state burst before acting.
            while True:
                try:
                    await asyncio.wait_for(ws.recv(), 0.5)
                except asyncio.TimeoutError:
                    break
            for event in events:
                await ws.send(json.dumps(event))
            replies = []
            while True:
                try:
                    replies.append(json.loads(await asyncio.wait_for(ws.recv(), settle)))
                except asyncio.TimeoutError:
                    return replies
    return asyncio.run(run())


def rule_errors(replies: list[dict]) -> list[str]:
    return [r["message"] for r in replies if r.get("type") == "rule_error"]


@unittest.skipIf(sys.platform == "win32", "e2e harness is POSIX-only")
class RuleCapE2E(unittest.TestCase):
    """Ten active rules fill the cap; every way past it says why."""

    @classmethod
    def setUpClass(cls):
        cls.server = Server()
        cls.server.start()
        cls.agent = cls.server.register()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def rules(self) -> dict[str, dict]:
        return {r["text"]: r for r in json.loads(self.server.http("GET", "/api/rules"))}

    def proposal_card(self, rule_text: str) -> dict:
        msgs = json.loads(self.server.http("GET", "/api/messages?channel=general&limit=50"))
        return next(m for m in msgs if m["type"] == "rule_proposal"
                    and m["metadata"]["text"] == rule_text)

    def test_cap_is_reported_and_proposal_cards_stay_pending(self):
        # The human's rules go active at once; the 11th waits in drafts, with a notice.
        replies = browser_events(self.server, [{"type": "rule_propose", "text": f"rule {i}"}
                                               for i in range(11)])
        rules = self.rules()
        self.assertEqual([rules[f"rule {i}"]["status"] for i in range(10)], ["active"] * 10)
        self.assertEqual(rules["rule 10"]["status"], "draft")
        self.assertEqual(len(rule_errors(replies)), 1)
        self.assertIn("Saved to drafts", rule_errors(replies)[0])

        # Activating it from the panel is refused with the reason.
        replies = browser_events(self.server, [{"type": "rule_activate", "id": rules["rule 10"]["id"]}])
        self.assertIn("10 rules are already active", rule_errors(replies)[0])
        self.assertEqual(self.rules()["rule 10"]["status"], "draft")

        # An agent's proposal card: Activate is refused (409) and the card stays pending.
        self.server.mcp(self.agent, "chat_rules", action="propose", rule="agent rule",
                        channel="general")
        card = self.proposal_card("agent rule")
        with self.assertRaises(urllib.error.HTTPError) as refused:
            self.server.http("POST", f"/api/messages/{card['id']}/resolve_rule_proposal",
                             {"action": "activate"})
        self.assertEqual(refused.exception.code, 409)
        self.assertIn("already active", json.loads(refused.exception.read())["error"])
        self.assertEqual(self.proposal_card("agent rule")["metadata"]["status"], "pending")

        # With room again, the same card activates.
        browser_events(self.server, [{"type": "rule_deactivate", "id": rules["rule 0"]["id"]}])
        self.server.http("POST", f"/api/messages/{card['id']}/resolve_rule_proposal",
                         {"action": "activate"})
        self.assertEqual(self.rules()["agent rule"]["status"], "active")
        self.assertEqual(self.proposal_card("agent rule")["metadata"]["status"], "activated")


@unittest.skipIf(sys.platform == "win32", "e2e harness is POSIX-only")
class RulesInAgentReadsE2E(unittest.TestCase):
    """Active rules ride with the channel header on every fresh context."""

    @classmethod
    def setUpClass(cls):
        cls.server = Server()
        cls.server.start()
        browser_events(cls.server, [{"type": "rule_propose", "text": "Reply in English"}])
        cls.agent = cls.server.register()
        cls.server.send(cls.agent, "hello")

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def read(self) -> str:
        return self.server.mcp(self.agent, "chat_read", channel="general")

    def session_event(self, event: str):
        self.server.http("POST", f"/api/agent_session/{self.agent['name']}", {"event": event},
                         bearer=self.agent["token"])

    def test_rules_come_with_fresh_reads_compaction_and_resync(self):
        first = self.read()
        self.assertIn(f"{RULES_HEADER}\n1. Reply in English", first)
        self.assertNotIn(RULES_HEADER, self.read())  # cursor kept: no header

        self.session_event("clear")  # CLI /clear: new empty session
        self.assertIn(RULES_HEADER, self.read())

        self.session_event("compact")
        self.assertIn(RULES_HEADER, self.read())
        self.assertNotIn(RULES_HEADER, self.read())

        resync = self.server.mcp(self.agent, "chat_resync", channel="general")
        self.assertIn(RULES_HEADER, resync)


if __name__ == "__main__":
    unittest.main()
