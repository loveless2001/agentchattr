"""End to end: session events a wrapper reports (POST /api/agent_session/{name})
change what the agent reads over MCP and what the status pill shows.

Uses the real server harness from test_e2e_server.py.
"""

import json
import sys
import unittest
import urllib.error

from test_e2e_server import Server

SUMMARY = "[#general summary —"


@unittest.skipIf(sys.platform == "win32", "e2e harness is POSIX-only")
class AgentSessionEventsE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server()
        cls.server.start()
        cls.agent = cls.server.register()
        for i in range(3):
            cls.server.send(cls.agent, f"before compaction {i}")

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def event(self, **body) -> dict:
        return json.loads(self.server.http("POST", f"/api/agent_session/{self.agent['name']}",
                                           body, bearer=self.agent["token"]))

    def read(self) -> str:
        return self.server.mcp(self.agent, "chat_read", channel="general")

    def test_session_events(self):
        # Steps share one agent and cursor, so they run in order in one test.
        self.assertIn(SUMMARY, self.read())         # fresh: summary + recent messages
        self.assertNotIn(SUMMARY, self.read())

        # Compaction: the next read carries the summary but only new messages.
        self.event(event="compact", pre_tokens=900_000, post_tokens=20_000)
        self.server.send(self.agent, "after compaction")
        after_compact = self.read()
        self.assertIn(SUMMARY, after_compact)
        self.assertIn("after compaction", after_compact)
        delivered = json.loads(after_compact.rsplit("\n", 1)[-1])  # the messages follow the summary
        self.assertEqual([m["text"] for m in delivered], ["after compaction"])  # none twice
        self.assertNotIn(SUMMARY, self.read())

        # Context usage reaches the channel status the pills render.
        cache = {"prompt": 2_000_000, "cached": 1_900_000, "written": 50_000, "turns": 12,
                 "cold_turns": 1, "ttl": "1h",
                 "last": {"prompt": 500_000, "cached": 495_000, "written": 4_000,
                          "at": 1_791_388_800.0, "cold": False}}
        self.event(event="context", tokens=500_000, window=1_000_000, cache=cache)
        status = json.loads(self.server.http("GET", "/api/status"))
        context = status["_channels"]["general"]["claude"]["context"]
        self.assertEqual((context["pct"], context["compactions"]), (50.0, 1))
        self.assertEqual((context["cache"]["session_pct"], context["cache"]["last_pct"],
                          context["cache"]["cold_turns"]), (95.0, 99.0, 1))

        # A crash restarted in a fresh session: cursor reset, notice in the channel.
        self.event(event="restart", exit_code=137, crashed=True, resumed=False)
        fresh = self.read()
        self.assertIn(SUMMARY, fresh)
        self.assertIn("before compaction 0", fresh)
        msgs = json.loads(self.server.http("GET", "/api/messages?channel=general&limit=50"))
        self.assertTrue(any("exited with code 137" in m["text"] and m["sender"] == "system"
                            for m in msgs))

    def test_events_need_the_agent_token_and_a_known_event(self):
        with self.assertRaises(urllib.error.HTTPError) as unauthenticated:
            self.server.http("POST", f"/api/agent_session/{self.agent['name']}",
                             {"event": "clear"}, auth=False)
        self.assertEqual(unauthenticated.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as unknown:
            self.event(event="explode")
        self.assertEqual(unknown.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
