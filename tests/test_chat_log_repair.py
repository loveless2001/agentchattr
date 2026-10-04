"""Chat log durability (store.py + chat_log_repair.py): a log torn by a crash
mid-append loads with every complete message recovered, a backup of the
damaged file, the torn ids never reused, and a clean rewritten log."""

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


def message_line(msg_id: int, text: str, sender: str = "user") -> str:
    """A log line as MessageStore.add writes it."""
    return json.dumps({"id": msg_id, "sender": sender, "text": text, "type": "chat",
                       "timestamp": time.time(), "time": time.strftime("%H:%M:%S"),
                       "attachments": [], "channel": "general"})


class ChatLogRepairTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "agentchattr_log.jsonl"

    def test_torn_lines_are_repaired_on_load(self):
        good = [message_line(i, f"message {i}") for i in range(3)]
        torn_mid = message_line(3, "cut off mid-line")[:40]  # crash, then the next append
        glued = message_line(4, "glued onto the torn line")
        torn_end = message_line(5, "cut off at the end")[:30]  # crash on the last append
        damaged = "\n".join(good + [torn_mid + glued, torn_end])  # no final newline
        self.path.write_text(damaged, "utf-8")

        with self.assertLogs("chat_log_repair", "WARNING") as logs:
            store = MessageStore(str(self.path))
        self.assertIn("recovered 1 messages from torn lines, 2 fragments unrecoverable", logs.output[0])

        self.assertEqual([m["id"] for m in store.get_recent(10)], [0, 1, 2, 4])
        self.assertEqual(store.get_recent(10)[-1]["text"], "glued onto the torn line")
        self.assertEqual(sorted(p["ids"][0] for p in store.load_problems), [3, 5])
        self.assertIsNotNone(store.load_backup)
        self.assertEqual(store.load_backup.read_text("utf-8"), damaged)  # untouched copy
        rewritten = self.path.read_text("utf-8")
        self.assertTrue(rewritten.endswith("\n"))
        self.assertEqual([json.loads(l)["id"] for l in rewritten.splitlines()], [0, 1, 2, 4])

        new = store.add("user", "after the repair")
        self.assertEqual(new["id"], 6)  # torn ids 3 and 5 are never reused
        reloaded = MessageStore(str(self.path))
        self.assertEqual([m["id"] for m in reloaded.get_recent(10)], [0, 1, 2, 4, 6])
        self.assertEqual(reloaded.load_problems, [])

    def test_missing_final_newline_is_restored(self):
        self.path.write_text("\n".join(message_line(i, f"m{i}") for i in range(2)), "utf-8")
        store = MessageStore(str(self.path))
        self.assertIsNone(store.load_backup)  # nothing damaged, nothing copied
        store.add("user", "next")
        lines = self.path.read_text("utf-8").splitlines()
        self.assertEqual([json.loads(l)["id"] for l in lines], [0, 1, 2])

    def test_delete_rewrites_the_log_atomically(self):
        store = MessageStore(str(self.path))
        for i in range(5):
            store.add("user", f"m{i}")
        store.delete([1, 3])
        lines = self.path.read_text("utf-8").splitlines()
        self.assertEqual([json.loads(l)["id"] for l in lines], [0, 2, 4])
        self.assertEqual([p.name for p in self.dir.iterdir() if p.suffix == ".tmp"], [])  # no temp left


if __name__ == "__main__":
    unittest.main()
