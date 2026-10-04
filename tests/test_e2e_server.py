"""End to end: the real server (run.main) in its own process, on free ports,
with a temp config and data dir, its output redirected to a log file the way
the launchers do it. Driven only from outside: HTTP (as the browser and the
wrappers do) and MCP over streamable HTTP (as agents do).

The summary CLI is the stand-in stub_summary_cli.py; set
AGENTCHATTR_TEST_LUNA=1 to use the real default (`codex exec` with Luna).

Safety: on startup the server kills `agentchattr-*` tmux sessions of channels
it does not know, so the server runs against a private, empty tmux socket
directory (TMUX_TMPDIR) and never sees the real sessions.
"""

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STUB_CLI = [sys.executable, str(Path(__file__).with_name("stub_summary_cli.py"))]
USE_LUNA = os.environ.get("AGENTCHATTR_TEST_LUNA") == "1"
SUMMARY_TIMEOUT = 300 if USE_LUNA else 30

# Runs the real entry point, reading config.toml from the temp dir instead of
# the repo (run.py always passes its own directory to load_config).
BOOT = """
import sys
from pathlib import Path
sys.path.insert(0, {root!r})
import config_loader
_load = config_loader.load_config
config_loader.load_config = lambda root=None: _load(Path({cfg_dir!r}))
import run
sys.argv = ["run.py"]
run.main()
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def long_text(i: int) -> str:
    return f"Report {i}: " + " ".join(f"finding{i}-{j}" for j in range(70))


class Server:
    """One agentchattr server in a temp dir; restartable with new settings."""

    def __init__(self, **server_settings):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.data = self.dir / "data"
        self.log = self.data / "server.log"
        self.data.mkdir()
        (self.dir / "tmux").mkdir()
        self.port, self.mcp_port, sse_port = free_port(), free_port(), free_port()
        self._sse_port = sse_port
        self.proc = None
        self.token = ""
        self.write_config(**server_settings)

    def write_config(self, log_max_mb=50, log_level="info"):
        command = "" if USE_LUNA else f"command = {json.dumps(STUB_CLI)}\n"
        (self.dir / "config.toml").write_text(f"""
[server]
port = {self.port}
host = "127.0.0.1"
data_dir = {json.dumps(str(self.data))}
log_max_mb = {log_max_mb}
log_level = "{log_level}"

[agents.claude]
command = "claude"
cwd = {json.dumps(str(self.dir))}
color = "#a78bfa"
label = "Claude"

[mcp]
http_port = {self.mcp_port}
sse_port = {self._sse_port}

[images]
upload_dir = {json.dumps(str(self.dir / "uploads"))}

[summaries]
{command}""", "utf-8")

    def _env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        env["TMUX_TMPDIR"] = str(self.dir / "tmux")
        return env

    def start(self):
        env = self._env()
        if shutil.which("tmux"):  # refuse to run where the real sessions are visible
            seen = subprocess.run(["tmux", "list-sessions"], env=env, capture_output=True)
            assert seen.returncode != 0, "isolated tmux socket unexpectedly has sessions"
        boot = BOOT.format(root=str(ROOT), cfg_dir=str(self.dir))
        with open(self.log, "ab") as out:  # like the launchers' `>> data/server.log`
            self.proc = subprocess.Popen([sys.executable, "-c", boot], cwd=self.dir, env=env,
                                         stdout=out, stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError(f"server exited:\n{self.log_text()[-3000:]}")
            try:
                html = self.http("GET", "/", auth=False).decode()
                self.token = re.search(r'__SESSION_TOKEN__="([0-9a-f]+)"', html).group(1)
                with socket.create_connection(("127.0.0.1", self.mcp_port), timeout=1):
                    return
            except OSError:
                time.sleep(0.2)
        raise AssertionError(f"server not ready:\n{self.log_text()[-3000:]}")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def close(self):
        self.stop()
        self._tmp.cleanup()

    def log_text(self) -> str:
        return self.log.read_text("utf-8", errors="replace")

    def http(self, method: str, path: str, body=None, bearer: str = "", auth=True) -> bytes:
        if auth and not bearer:
            path += ("&" if "?" in path else "?") + f"token={self.token}"
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode())
        req.add_header("Content-Type", "application/json")
        if bearer:
            req.add_header("Authorization", f"Bearer {bearer}")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read()

    def register(self) -> dict:
        return json.loads(self.http("POST", "/api/register", {"base": "claude"}, auth=False))

    def send(self, agent: dict, text: str):
        self.http("POST", "/api/send", {"text": text, "channel": "general"}, bearer=agent["token"])

    def mcp(self, agent: dict, tool: str, **args) -> str:
        """Call an MCP tool as `agent`, the way an agent CLI does."""
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async def call():
            headers = {"Authorization": f"Bearer {agent['token']}"}
            async with httpx.AsyncClient(headers=headers, timeout=30) as http, \
                    streamable_http_client(f"http://127.0.0.1:{self.mcp_port}/mcp",
                                           http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool, {"sender": agent["name"], **args})
                    return "".join(getattr(c, "text", "") for c in result.content)

        return asyncio.run(call())

    def wait_for_summaries(self, channel: str = "general") -> dict:
        path = self.data / "summaries" / f"{channel}.json"
        deadline = time.time() + SUMMARY_TIMEOUT
        while time.time() < deadline:
            if path.exists():
                tree = json.loads(path.read_text("utf-8"))
                if tree["levels"] and all(n["text"] for lv in tree["levels"] for n in lv):
                    return tree
            time.sleep(0.2)
        raise AssertionError(f"summaries not written in {SUMMARY_TIMEOUT}s:\n{self.log_text()[-3000:]}")

    def wait_for_log(self, needle: str, timeout: float = 10) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = self.log_text()
            if needle in text:
                return text
            time.sleep(0.1)
        raise AssertionError(f"{needle!r} not in server log:\n{self.log_text()[-3000:]}")


@unittest.skipIf(sys.platform == "win32", "e2e harness and log cap are POSIX-only")
class FreshServerE2E(unittest.TestCase):
    """A server started over an oversized stale log, used by one agent."""

    @classmethod
    def setUpClass(cls):
        cls.server = Server(log_max_mb=1)
        with open(cls.server.log, "w") as f:  # ~2 MB left over from earlier runs
            f.writelines(f"stale line {i:07d} {'x' * 40}\n" for i in range(40000))
        cls.server.start()
        cls.agent = cls.server.register()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_stale_log_is_capped_at_startup(self):
        text = self.server.wait_for_log("Server log capped at 1 MB")
        self.assertLess(len(text.encode()), 1024 * 1024)
        self.assertNotIn("stale line 0000000", text)  # oldest lines went first
        self.assertIn("stale line 0039999", text)

    def test_heartbeats_are_hidden_at_the_default_log_level(self):
        for _ in range(3):
            self.server.http("POST", f"/api/heartbeat/{self.agent['name']}", {},
                             bearer=self.agent["token"])
        self.server.http("GET", "/api/status")
        text = self.server.wait_for_log('"GET /api/status')
        self.assertNotIn("/api/heartbeat/", text)

    def test_agent_gets_summary_on_first_read_and_drills_in(self):
        for i in range(3):
            self.server.send(self.agent, f"note {i}: short and kept word for word")
            self.server.send(self.agent, long_text(i))

        first = self.server.mcp(self.agent, "chat_read", channel="general")
        self.assertIn("[#general summary —", first)
        self.assertNotIn("[#general summary —",
                         self.server.mcp(self.agent, "chat_read", channel="general"))

        tree = self.server.wait_for_summaries()
        resync = self.server.mcp(self.agent, "chat_resync", channel="general")
        self.assertIn("note 0: short and kept word for word", resync)
        if not USE_LUNA:
            self.assertIn("stub-summary", resync)  # compressed by the CLI subprocess
        long_leaf = next(n for n in tree["levels"][0] if n["text"] and "note" not in n["text"])
        zoomed = self.server.mcp(self.agent, "chat_summary", action="zoom",
                                 channel="general", block=str(long_leaf["lo"]))
        self.assertIn("finding", zoomed)
        recalled = self.server.mcp(self.agent, "chat_summary", action="recall",
                                   channel="general", query=r"finding2-69\b")
        self.assertIn("finding2-69", recalled)
        self.assertIn("1 matches", recalled)


@unittest.skipIf(sys.platform == "win32", "e2e harness and log cap are POSIX-only")
class RestartedServerE2E(unittest.TestCase):
    """The server crashes mid-append with an agent connected, and comes back
    at log_level = "debug"; the agent's CLI restarts under the same name."""

    @classmethod
    def setUpClass(cls):
        cls.server = Server()
        cls.server.start()
        agent = cls.server.register()
        cls.server.send(agent, "before the crash")  # message #0
        # Two reads: the first leaves a read cursor at #0, the second resumes it.
        cls.reads_before_crash = [cls.server.mcp(agent, "chat_read", channel="general")
                                  for _ in range(2)]
        cls.server.stop()  # the agent never deregisters: its cursor persists

        chat_log = cls.server.data / "agentchattr_log.jsonl"
        logged = [json.loads(l) for l in chat_log.read_text("utf-8").splitlines()]
        template = next(m for m in logged if m["text"] == "before the crash")
        msg = lambda i, text: json.dumps({**template, "id": i, "text": text})
        last_id = max(m["id"] for m in logged)
        with open(chat_log, "a", encoding="utf-8") as f:  # torn line, next append glued on
            f.write(msg(last_id + 1, "cut off by the crash")[:35] + msg(last_id + 2, "glued on") + "\n")

        cls.server.write_config(log_level="debug")
        cls.server.start()
        cls.agent = cls.server.register()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def test_torn_chat_log_is_repaired_with_a_notice(self):
        msgs = json.loads(self.server.http("GET", "/api/messages?channel=general&limit=50"))
        texts = [m["text"] for m in msgs]
        self.assertIn("before the crash", texts)
        self.assertIn("glued on", texts)
        self.assertTrue(any("A crash cut off 1 message(s)" in t for t in texts), texts)
        self.assertEqual(len(list(self.server.data.glob("agentchattr_log.jsonl.damaged-*"))), 1)

    def test_read_cursor_at_the_first_message_is_kept(self):
        first, second = self.reads_before_crash
        self.assertIn("[#general summary —", first)
        self.assertNotIn("[#general summary —", second)
        self.assertIn("No new messages", second)

    def test_reregistered_agent_gets_the_summary_again(self):
        self.assertEqual(self.agent["name"], "claude")  # same name as before the crash
        self.assertIn("[#general summary —",
                      self.server.mcp(self.agent, "chat_read", channel="general"))

    def test_debug_level_logs_heartbeats(self):
        self.server.http("POST", f"/api/heartbeat/{self.agent['name']}", {},
                         bearer=self.agent["token"])
        text = self.server.wait_for_log(f'"POST /api/heartbeat/{self.agent["name"]}')
        self.assertRegex(text, r"DEBUG.*POST /api/heartbeat/")


if __name__ == "__main__":
    unittest.main()
