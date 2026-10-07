"""Prompt-cache tracking: transcript parsing (Claude Code, Codex), the wrapper
monitor's report and the server's status view.

Transcript lines are shaped like the real Claude Code / Codex files.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session_state  # noqa: E402
from agent_session_monitor import AgentSessionMonitor, install_claude_session_hook  # noqa: E402
from agent_transcript_readers import ClaudeTranscriptParser, CodexRolloutParser  # noqa: E402


def claude_block(msg_id: str, *, read: int, write: int = 0, ttl: str = "1h", fresh: int = 2,
                 at: str = "2026-10-07T16:00:00.000Z", sidechain=False) -> str:
    """One content block of an assistant message; every block repeats the usage."""
    usage = {"input_tokens": fresh, "cache_creation_input_tokens": write,
             "cache_read_input_tokens": read, "output_tokens": 50,
             "cache_creation": {"ephemeral_1h_input_tokens": write if ttl == "1h" else 0,
                                "ephemeral_5m_input_tokens": write if ttl == "5m" else 0}}
    return json.dumps({"type": "assistant", "isSidechain": sidechain, "timestamp": at,
                       "message": {"id": msg_id, "model": "claude-opus-5-5", "usage": usage,
                                   "content": [{"type": "text", "text": "x"}]}})


def codex_tokens(*, last_in: int, last_cached: int, total_in: int, total_cached: int,
                 total: int, at: str = "2026-10-07T16:39:27.106Z") -> str:
    return json.dumps({"timestamp": at, "type": "event_msg", "payload": {
        "type": "token_count", "info": {
            "total_token_usage": {"input_tokens": total_in, "cached_input_tokens": total_cached,
                                  "cache_write_input_tokens": 0, "output_tokens": 100,
                                  "total_tokens": total},
            "last_token_usage": {"input_tokens": last_in, "cached_input_tokens": last_cached,
                                 "cache_write_input_tokens": 0, "output_tokens": 100,
                                 "total_tokens": last_in + 100},
            "model_context_window": 258_400}}})


class ClaudeCacheStatsTest(unittest.TestCase):
    def test_repeated_blocks_of_one_message_count_once(self):
        parser = ClaudeTranscriptParser()
        for _ in range(3):  # thinking, text, tool_use of one response
            parser.feed(claude_block("msg_1", read=55_681, write=2_611))
        parser.feed(claude_block("msg_2", read=58_292, write=400))
        cache = parser.cache.report()
        self.assertEqual(cache["turns"], 2)
        self.assertEqual(cache["cached"], 55_681 + 58_292)
        self.assertEqual(cache["written"], 2_611 + 400)
        self.assertEqual(cache["prompt"], (2 + 2_611 + 55_681) + (2 + 400 + 58_292))
        self.assertEqual(cache["last"]["cached"], 58_292)
        self.assertEqual(cache["last"]["at"], 1_791_388_800.0)  # 2026-10-07T16:00:00Z

    def test_ttl_comes_from_cache_writes_and_survives_read_only_turns(self):
        parser = ClaudeTranscriptParser()
        parser.feed(claude_block("m1", read=0, write=40_000, ttl="1h"))
        parser.feed(claude_block("m2", read=40_000, write=0))
        self.assertEqual(parser.cache.ttl, "1h")
        parser.feed(claude_block("m3", read=40_000, write=500, ttl="5m"))  # e.g. usage overage
        self.assertEqual(parser.cache.ttl, "5m")

    def test_large_prompt_that_missed_the_cache_is_a_cold_turn(self):
        parser = ClaudeTranscriptParser()
        parser.feed(claude_block("m1", read=0, write=20_000))       # small: not cold
        parser.feed(claude_block("m2", read=20_000, write=500))
        parser.feed(claude_block("m3", read=0, write=460_000))      # cache expired
        cache = parser.cache.report()
        self.assertEqual(cache["cold_turns"], 1)
        self.assertTrue(cache["last"]["cold"])

    def test_subagent_turns_are_not_counted(self):
        parser = ClaudeTranscriptParser()
        parser.feed(claude_block("sub", read=90_000, sidechain=True))
        self.assertIsNone(parser.cache.report())


class CodexCacheStatsTest(unittest.TestCase):
    def test_totals_are_the_cli_cumulative_ones_and_reemits_are_one_turn(self):
        parser = CodexRolloutParser()
        line = codex_tokens(last_in=147_491, last_cached=144_640, total_in=84_590_152,
                            total_cached=81_085_568, total=85_292_523)
        parser.feed(line)
        parser.feed(line)  # token_count is re-emitted with unchanged numbers
        cache = parser.cache.report()
        self.assertEqual(cache["turns"], 1)
        self.assertEqual((cache["prompt"], cache["cached"]), (84_590_152, 81_085_568))
        self.assertEqual((cache["last"]["prompt"], cache["last"]["cached"]), (147_491, 144_640))
        self.assertIsNone(cache["ttl"])  # Codex does not expose its cache lifetime

    def test_cold_turn(self):
        parser = CodexRolloutParser()
        parser.feed(codex_tokens(last_in=150_000, last_cached=4_000, total_in=150_000,
                                 total_cached=4_000, total=150_100))
        self.assertEqual(parser.cache.report()["cold_turns"], 1)


class CacheReportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        agent_session_state.forget("agent")

    def tearDown(self):
        self._tmp.cleanup()

    def test_monitor_reports_session_totals_from_the_whole_transcript(self):
        reports = []
        _, events = install_claude_session_hook(self.tmp / "cfg", "claude-x")
        transcript = self.tmp / "s1.jsonl"
        transcript.write_text("\n".join(claude_block(f"m{i}", read=50_000, write=1_000)
                                        for i in range(200)) + "\n", "utf-8")
        events.write_text(json.dumps({"session_id": "s1", "transcript_path": str(transcript),
                                      "source": "startup"}) + "\n", "utf-8")
        monitor = AgentSessionMonitor("claude", report_fn=reports.append, events_file=events,
                                      context_window=1_000_000)
        monitor.poll()
        cache = [r for r in reports if r["event"] == "context"][-1]["cache"]
        self.assertEqual((cache["turns"], cache["cached"]), (200, 200 * 50_000))

    def apply(self, **body):
        return agent_session_state.apply_event("agent", body, reset_cursors=lambda n: None,
                                               post_notice=lambda t: None)

    def test_status_view_and_a_new_cold_turn_is_broadcast(self):
        cache = {"prompt": 1_000_000, "cached": 960_000, "written": 30_000, "turns": 10,
                 "cold_turns": 0, "ttl": "1h",
                 "last": {"prompt": 100_000, "cached": 98_000, "written": 1_000,
                          "at": 1_791_388_800.0, "cold": False}}
        self.assertTrue(self.apply(event="context", tokens=100_000, window=1_000_000, cache=cache))
        view = agent_session_state.get_context("agent")["cache"]
        self.assertEqual((view["session_pct"], view["last_pct"], view["ttl"]), (96.0, 98.0, "1h"))

        self.assertFalse(self.apply(event="context", tokens=100_500, window=1_000_000, cache=cache))
        cold = {**cache, "cold_turns": 1,
                "last": {**cache["last"], "cached": 0, "written": 100_000, "cold": True}}
        self.assertTrue(self.apply(event="context", tokens=100_600, window=1_000_000, cache=cold))
        self.assertTrue(agent_session_state.get_context("agent")["cache"]["last_cold"])

    def test_malformed_cache_values_are_dropped(self):
        self.apply(event="context", tokens=1_000, window=1_000_000,
                   cache={"prompt": "lots", "cached": -5, "ttl": "forever", "last": "x"})
        self.assertNotIn("cache", agent_session_state.get_context("agent"))


if __name__ == "__main__":
    unittest.main()
