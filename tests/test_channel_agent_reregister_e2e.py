"""End to end: a channel agent (claude-gravity) whose name is deregistered, as
/sleep does, gets 409 on its next heartbeat and re-registers. Re-registering
under its channel name keeps its notices in #gravity; without it the wrapper
came back as a channel-less claude-2 and every notice landed in #general.

Uses the real server harness from test_e2e_server.py.
"""

import json
import sys
import time
import unittest
import urllib.error

from test_e2e_server import Server

NAME = "claude-gravity"


@unittest.skipIf(sys.platform == "win32", "e2e harness is POSIX-only")
class ChannelAgentReregisterE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server()
        (cls.server.data / "settings.json").write_text(
            json.dumps({"channels": ["general", "gravity"]}), "utf-8")
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def register(self, **body) -> dict:
        return json.loads(self.server.http("POST", "/api/register",
                                           {"base": "claude", **body}, auth=False))

    def messages(self, channel: str) -> list[dict]:
        return json.loads(self.server.http("GET", f"/api/messages?channel={channel}&limit=100"))

    def test_reregistered_channel_agent_keeps_its_channel(self):
        agent = self.register(requested_name=NAME)
        self.assertEqual(agent["name"], NAME)

        # /sleep deregisters the instance; the wrapper's next heartbeat is refused.
        self.server.http("POST", f"/api/deregister/{NAME}", bearer=agent["token"])
        with self.assertRaises(urllib.error.HTTPError) as refused:
            self.server.http("POST", f"/api/heartbeat/{NAME}", bearer=agent["token"])
        self.assertEqual(refused.exception.code, 409)

        # The wrapper's 409 path re-registers with its channel name and flags recovery.
        again = self.register(label="Claude", requested_name=NAME)
        self.assertEqual(again["name"], NAME)
        (self.server.data / f"{NAME}_recovered").write_text(NAME, "utf-8")
        self.server.http("POST", f"/api/agent_session/{NAME}",
                         {"event": "restart", "crashed": True, "resumed": True},
                         bearer=again["token"])

        deadline = time.time() + 10  # the recovery flag is picked up every 3s
        while time.time() < deadline:
            gravity = [m["text"] for m in self.messages("gravity") if m["sender"] == "system"]
            if any("interrupted" in t for t in gravity):
                break
            time.sleep(0.3)
        self.assertTrue(any(f"Agent routing for {NAME} interrupted" in t for t in gravity), gravity)
        self.assertTrue(any(f"{NAME}'s CLI stopped unexpectedly" in t for t in gravity), gravity)
        leaked = [m["text"] for m in self.messages("general") if NAME in m["text"]]
        self.assertEqual(leaked, [])


if __name__ == "__main__":
    unittest.main()
