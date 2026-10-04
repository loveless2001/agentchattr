"""Channel summary tree, integrated: MessageStore -> SummaryStore -> the
background SummaryCompressor workers running a summary CLI subprocess (the
stand-in stub_summary_cli.py), then the agent-facing header, zoom and recall,
deletes, and the rebuild after a message is lost from the log."""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from store import MessageStore
from summaries import SummaryStore
from summary_compressor_worker import SummaryCompressor
from summary_tree_views import DELETED

STUB_CLI = [sys.executable, str(Path(__file__).with_name("stub_summary_cli.py"))]
CHANNEL = "lab"


def long_text(i: int) -> str:
    """Over the 512-byte line size, so its leaf needs the compressor."""
    return f"Report {i}: " + " ".join(f"finding{i}-{j}" for j in range(70))


class SummaryTreeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.log_path = self.dir / "agentchattr_log.jsonl"
        self.store, self.summaries = self._open(start_compressor=True)
        # 8 messages: the human's short ones (kept verbatim) between long ones
        for i in range(4):
            self.store.add("user", f"question {i}: what did you find?", channel=CHANNEL)
            self.store.add("claude", long_text(i), channel=CHANNEL)
        self.summaries.ensure(CHANNEL)
        self.tree = self._wait_until_written()

    def _open(self, start_compressor: bool):
        store = MessageStore(str(self.log_path))
        cfg = {"command": STUB_CLI, "workers": 2}
        summaries = SummaryStore(str(self.dir / "summaries"), store, cfg)
        store.on_message(summaries.on_message)
        store.on_delete(summaries.on_delete)
        if start_compressor:
            compressor = SummaryCompressor(summaries, self.dir / "summaries" / ".work", cfg,
                                           human=lambda: "user")
            self.assertTrue(compressor.start())
        return store, summaries

    def _read_tree(self) -> dict:
        return json.loads((self.dir / "summaries" / f"{CHANNEL}.json").read_text("utf-8"))

    def _wait_until_written(self, timeout: float = 30) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            tree = self._read_tree()
            if tree["levels"] and all(n["text"] for level in tree["levels"] for n in level):
                return tree
            time.sleep(0.1)
        self.fail(f"summary tree not written within {timeout}s: {self._read_tree()}")

    def test_tree_is_written_and_read_back(self):
        leaves, root = self.tree["levels"][0], self.tree["levels"][-1]
        self.assertEqual(len(leaves), 8)
        self.assertEqual(leaves[0]["text"], "user: question 0: what did you find?")  # verbatim
        self.assertTrue(leaves[1]["text"].startswith("stub-summary"))  # compressed
        self.assertEqual(len(root), 1)
        self.assertEqual((root[0]["lo"], root[0]["hi"]), (leaves[0]["lo"], leaves[-1]["hi"]))

        header = self.summaries.render(CHANNEL)
        self.assertTrue(header.startswith(f"[#{CHANNEL} summary — 8 messages since"), header)

        lo, hi = root[0]["lo"], root[0]["hi"]
        halves = self.summaries.zoom(CHANNEL, f"{lo}-{hi}")
        for half in self.tree["levels"][-2]:
            self.assertIn(f"#{half['lo']}-{half['hi']}", halves)
        self.assertIn(long_text(2), self.summaries.zoom(CHANNEL, str(leaves[5]["lo"])))
        self.assertIn(f"#{leaves[7]['lo']}", self.summaries.recall(CHANNEL, r"finding3-69\b"))

    def test_delete_rewrites_the_lines_above_it(self):
        leaves = self.tree["levels"][0]
        parent_before = self.tree["levels"][1][0]["text"]
        self.store.delete([leaves[1]["lo"]])
        tree = self._wait_until_written()
        self.assertEqual(tree["levels"][0][1]["text"], DELETED)
        self.assertNotEqual(tree["levels"][1][0]["text"], parent_before)
        self.assertNotIn(long_text(0), json.dumps(tree))

    def test_rebuild_after_a_lost_message_keeps_unaffected_lines(self):
        before = self.tree["levels"]
        lost = before[0][5]["lo"]  # message 6 of 8 vanishes from the log (torn line)
        lines = self.log_path.read_text("utf-8").splitlines()
        self.log_path.write_text(
            "".join(l + "\n" for l in lines if json.loads(l)["id"] != lost), "utf-8")

        self._open(start_compressor=False)  # a restart: the tree is checked on load
        after = self._read_tree()["levels"]

        self.assertNotIn(lost, [leaf["lo"] for leaf in after[0]])
        self.assertEqual([n["text"] for n in after[0]],
                         [n["text"] for n in before[0] if n["lo"] != lost])
        # Nodes covering only messages before the lost one keep their lines.
        kept = [n for n in before[1] + before[2] if n["hi"] < lost]
        self.assertTrue(kept)
        after_by_range = {(n["lo"], n["hi"]): n["text"] for level in after for n in level}
        for node in kept:
            self.assertEqual(after_by_range.get((node["lo"], node["hi"])), node["text"])


if __name__ == "__main__":
    unittest.main()
