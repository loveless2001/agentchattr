"""End to end: an agent registers a wait over MCP (chat_wait); when it is over
the server posts a notice and queues a wake prompt for the agent's wrapper.

Uses the real server harness from test_e2e_server.py.
"""

import json
import sys
import threading
import time
import unittest

from test_e2e_server import Server


@unittest.skipIf(sys.platform == "win32", "e2e harness is POSIX-only")
class AgentWaitsE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server()
        cls.server.start()
        cls.agent = cls.server.register()
        # Stand in for the wrapper's heartbeat: only an online agent is woken or pinged.
        cls.stop = threading.Event()
        threading.Thread(target=cls._heartbeat, daemon=True).start()

    @classmethod
    def _heartbeat(cls):
        while not cls.stop.is_set():
            try:
                cls.server.http("POST", f"/api/heartbeat/{cls.agent['name']}", {},
                                bearer=cls.agent["token"])
            except Exception:
                pass
            cls.stop.wait(2)

    @classmethod
    def tearDownClass(cls):
        cls.stop.set()
        cls.server.close()

    def test_a_registered_wait_wakes_the_agent(self):
        marker = self.server.dir / "done.flag"
        reply = self.server.mcp(self.agent, "chat_wait", note="train run 7", path=str(marker))
        self.assertIn("Wait #1 registered", reply)
        status = json.loads(self.server.http("GET", "/api/status"))
        waits = status["_channels"]["general"]["claude"]["waits"]
        self.assertEqual([w["note"] for w in waits], ["train run 7"])

        queue = self.server.data / f"{self.agent['name']}_queue.jsonl"
        marker.touch()
        deadline = time.time() + 20
        while time.time() < deadline and not (queue.exists() and queue.read_text().strip()):
            time.sleep(0.2)
        entry = json.loads(queue.read_text().splitlines()[-1])
        self.assertEqual(entry["channel"], "general")
        self.assertIn("wait #1 (train run 7) is over", entry["prompt"])
        msgs = json.loads(self.server.http("GET", "/api/messages?channel=general&limit=50"))
        self.assertTrue(any(m["sender"] == "system" and '"train run 7" is over' in m["text"]
                            for m in msgs))
        self.assertIn("no active waits", self.server.mcp(self.agent, "chat_wait_cancel"))

    def test_keepalive_pings_a_waiting_agent_before_its_cache_expires(self):
        self.server.mcp(self.agent, "chat_wait", note="train run 8", timeout_minutes=600)
        last_request = time.time() - 52 * 60  # the 1h cache has ~8 minutes left
        cache = {"prompt": 9_000_000, "cached": 8_800_000, "written": 100_000, "turns": 40,
                 "cold_turns": 0, "ttl": "1h",
                 "last": {"prompt": 460_000, "cached": 455_000, "written": 2_000,
                          "at": last_request, "cold": False}}
        self.server.http("POST", f"/api/agent_session/{self.agent['name']}",
                         {"event": "context", "tokens": 460_000, "window": 1_000_000,
                          "cache": cache}, bearer=self.agent["token"])

        queue = self.server.data / f"{self.agent['name']}_queue.jsonl"
        pings = []
        deadline = time.time() + 20
        while time.time() < deadline and not pings:
            lines = queue.read_text().splitlines() if queue.exists() else []
            pings = [json.loads(l) for l in lines if "agentchattr keepalive" in l]
            time.sleep(0.2)
        self.assertEqual(len(pings), 1)
        self.assertIn("Still waiting on: train run 8", pings[0]["inject_text"])
        self.server.mcp(self.agent, "chat_wait_cancel")

    def test_an_invalid_wait_is_refused(self):
        reply = self.server.mcp(self.agent, "chat_wait", note="x", path="relative/train.log")
        self.assertEqual(reply, "Error: path must be absolute.")


if __name__ == "__main__":
    unittest.main()
