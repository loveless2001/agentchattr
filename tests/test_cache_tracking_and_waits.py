"""Prompt-cache tracking: transcript parsing (Claude Code, Codex), the wrapper
monitor's report and the server's status view. Registered waits (chat_wait):
the store, each condition, validation and persistence.

Transcript lines are shaped like the real Claude Code / Codex files.
"""

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import agent_session_state  # noqa: E402
import agent_wait_conditions  # noqa: E402
from agent_waits import MAX_WAITS_PER_AGENT, WaitStore  # noqa: E402
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


class WaitStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.changes = []
        self.store = WaitStore(self.tmp / "waits.json", on_change=lambda: self.changes.append(1))

    def tearDown(self):
        self._tmp.cleanup()

    def wait(self, **kw):
        return self.store.create("claude-lab", "lab", note=kw.pop("note", "train run 7"), **kw)

    def sleeper(self):
        proc = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        return proc

    def test_pid_wait_ends_when_the_process_exits(self):
        proc = self.sleeper()
        self.wait(pid=proc.pid)
        self.assertEqual(self.store.pop_due(), [])
        proc.kill()
        proc.wait()
        [(wait, reason)] = self.store.pop_due()
        self.assertEqual(reason, f"process {proc.pid} exited")
        self.assertEqual(self.store.list_for("claude-lab"), [])

    def test_an_unreaped_zombie_counts_as_exited(self):
        proc = self.sleeper()
        self.wait(pid=proc.pid)
        proc.kill()
        time.sleep(0.3)  # killed but not waited for: a zombie
        self.assertEqual(len(self.store.pop_due()), 1)

    def test_a_reused_pid_is_not_the_same_process(self):
        proc = self.sleeper()
        self.wait(pid=proc.pid)
        self.store._waits[0]["pid_start"] = "1"  # as if another process now had the pid
        self.assertEqual(len(self.store.pop_due()), 1)

    def test_file_wait_ends_when_the_file_appears(self):
        marker = self.tmp / "done.flag"
        self.wait(path=str(marker))
        self.assertEqual(self.store.pop_due(), [])
        marker.touch()
        [(_, reason)] = self.store.pop_due()
        self.assertEqual(reason, f"{marker} appeared")

    def test_log_text_matches_only_lines_written_after_registration(self):
        log = self.tmp / "train.log"
        log.write_text("FINISHED an older run\n", "utf-8")
        self.wait(path=str(log), contains="FINISHED|Traceback")
        self.assertEqual(self.store.pop_due(), [])
        with open(log, "a", encoding="utf-8") as f:
            f.write("epoch 1 loss 0.4\nFINI")  # the match is still being written
        self.assertEqual(self.store.pop_due(), [])
        with open(log, "a", encoding="utf-8") as f:
            f.write("SHED run 7\n")
        [(_, reason)] = self.store.pop_due()
        self.assertEqual(reason, f'{log} logged: "FINISHED run 7"')

    def test_regex_characters_are_plain_text(self):
        log = self.tmp / "train.log"
        self.wait(path=str(log), contains="(a+)+$")
        log.write_text("a" * 4000 + "!\n", "utf-8")  # would hang a backtracking regex
        self.assertEqual(self.store.pop_due(), [])
        with open(log, "a", encoding="utf-8") as f:
            f.write("matched (a+)+$ literally\n")
        self.assertEqual(len(self.store.pop_due()), 1)

    def test_progress_bars_and_an_unterminated_last_line(self):
        log = self.tmp / "train.log"
        self.wait(path=str(log), contains="FINISHED")
        log.write_text("epoch 1\r 50%\r FINISHED", "utf-8")  # \r-only progress output
        self.assertEqual(self.store.pop_due(), [])            # the last line may still grow
        [(_, reason)] = self.store.pop_due()                   # the file stopped growing
        self.assertEqual(reason, f'{log} logged: "FINISHED"')

    def test_a_truncated_or_replaced_log_is_read_again_from_the_start(self):
        for replace in (False, True):
            with self.subTest(replace=replace):
                log = self.tmp / f"train-{replace}.log"
                log.write_text("x" * 100 + "\n", "utf-8")
                self.wait(path=str(log), contains="Traceback")
                text = "Traceback (most recent call last)\n" + ("y" * 200 + "\n" if replace else "")
                if replace:  # a new file, longer than the old offset
                    (self.tmp / "new.log").write_text(text, "utf-8")
                    (self.tmp / "new.log").replace(log)
                else:
                    log.write_text(text, "utf-8")
                self.assertEqual(len(self.store.pop_due()), 1)

    def test_timeout_and_a_plain_timer(self):
        wait = self.wait(timeout_minutes=90)
        self.assertEqual(self.store.pop_due(now=wait["created_at"] + 89 * 60), [])
        [(_, reason)] = self.store.pop_due(now=wait["created_at"] + 90 * 60)
        self.assertEqual(reason, "timed out after 1h30m")

    def test_a_pid_or_path_wait_gets_the_default_timeout(self):
        wait = self.wait(path=str(self.tmp / "ckpt.pt"))
        self.assertEqual(wait["deadline"] - wait["created_at"], 24 * 3600)

    def test_invalid_waits_are_refused_with_a_reason(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        existing = self.tmp / "exists"
        existing.touch()
        cases = [
            (dict(), "give a pid, a path or timeout_minutes"),
            (dict(note="  ", timeout_minutes=5), "note is required"),
            (dict(pid=dead.pid), "is not running"),
            (dict(pid=1), "is not running"),
            (dict(path="relative/train.log"), "must be absolute"),
            (dict(path=str(self.tmp / "a\nb")), "control characters"),
            (dict(contains="done", timeout_minutes=5), "contains needs path"),
            (dict(path=str(self.tmp / "t.log"), contains="||"), "contains is 1 to"),
            (dict(path=str(self.tmp), contains="x"), "is not a file"),
            (dict(path=str(existing)), "already exists"),
            (dict(timeout_minutes=-5), "timeout_minutes is 1 to"),
            (dict(timeout_minutes=7 * 24 * 60 + 1), "timeout_minutes is 1 to"),
        ]
        for kwargs, message in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError) as caught:
                self.wait(**kwargs)
            self.assertIn(message, str(caught.exception))
        self.assertEqual(self.store.list_for("claude-lab"), [])

    def test_notes_cannot_mention_anyone_and_waits_per_agent_are_capped(self):
        wait = self.wait(note="@codex ping\nme", timeout_minutes=5)
        self.assertEqual(wait["note"], "codex ping me")
        for _ in range(MAX_WAITS_PER_AGENT - 1):
            self.wait(timeout_minutes=5)
        with self.assertRaises(ValueError):
            self.wait(timeout_minutes=5)

    def test_waits_persist_and_can_be_cancelled_or_follow_a_rename(self):
        first = self.wait(timeout_minutes=60)
        self.wait(path=str(self.tmp / "ckpt.pt"))
        reloaded = WaitStore(self.tmp / "waits.json")
        self.assertEqual([w["id"] for w in reloaded.list_for("claude-lab")], [1, 2])
        self.assertEqual(reloaded.create("codex", "general", note="x", timeout_minutes=1)["id"], 3)

        reloaded.rename("claude-lab", "claude-lab-2")
        self.assertEqual(reloaded.cancel("claude-lab", 0), 0)
        self.assertEqual(reloaded.cancel("claude-lab-2", first["id"]), 1)
        self.assertEqual(reloaded.cancel("claude-lab-2", 0), 1)
        self.assertEqual([w["agent"] for w in WaitStore(self.tmp / "waits.json").list_for("codex")],
                         ["codex"])

    def test_a_wait_that_cannot_be_checked_ends_with_the_reason(self):
        self.wait(timeout_minutes=5)
        self.store._waits[0]["deadline"] = "soon"  # e.g. a hand-edited waits.json
        with self.assertLogs("agent_waits", "ERROR"):
            [(_, reason)] = self.store.pop_due()
        self.assertIn("could not be checked (TypeError)", reason)

    def test_a_failed_save_keeps_the_waits_in_memory(self):
        self.store._path = self.tmp / "missing-dir" / "waits.json"
        with self.assertLogs("agent_waits", "ERROR"):
            wait = self.wait(timeout_minutes=1)
            self.assertEqual(len(self.store.pop_due(now=wait["created_at"] + 61)), 1)

    def test_changes_are_announced(self):
        wait = self.wait(timeout_minutes=1)
        self.store.pop_due(now=wait["created_at"] + 61)
        self.wait(timeout_minutes=1)
        self.store.cancel("claude-lab")
        self.wait(timeout_minutes=1)
        self.store.rename("claude-lab", "claude-lab-2")
        self.assertEqual(len(self.changes), 6)

    def test_texts(self):
        wait = self.wait(path=str(self.tmp / "train.log"), contains="FINISHED")
        reason = f'{wait["path"]} logged: "FINISHED secret-ish line"'
        notice = agent_wait_conditions.ended_notice(wait, reason, online=False)
        self.assertNotIn("secret-ish", notice)  # log lines go to the agent, not the channel
        self.assertIn("is offline", notice)
        timer = self.wait(note="check in", timeout_minutes=30)
        prompt = agent_wait_conditions.wake_prompt([(wait, reason), (timer, "timed out after 30m")])
        self.assertIn("FINISHED secret-ish line", prompt)
        self.assertIn("wait #2 (check in) is over: timed out after 30m", prompt)  # both, one prompt
        self.assertIn("#lab", prompt)
        self.assertIn("logs a line containing 'FINISHED'", agent_wait_conditions.describe(wait))


if __name__ == "__main__":
    unittest.main()
