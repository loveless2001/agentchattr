"""Dashboard store (dashboards.py) and the agent text view (dashboard_agent_tools):
ordered blocks, per-block optimistic concurrency, undo of every change kind,
caps, persistence that survives a restart, and change callbacks."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dashboards as dash
from dashboards import DashboardConflict, DashboardError, DashboardStore
from dashboard_agent_tools import relative_age, render_for_agent


class DashboardStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.store = DashboardStore(str(self.dir))
        self.events = []
        self.store.on_change(lambda ch, snap: self.events.append((ch, snap["version"])))

    def tearDown(self):
        self._tmp.cleanup()

    def names(self, ch="general"):
        return [b["name"] for b in self.store.get(ch)["blocks"]]

    def test_set_appends_and_replaces_in_place(self):
        self.store.set_block("general", "a", "<p>1</p>", "claude")
        self.store.set_block("general", "b", "<p>2</p>", "codex")
        snap = self.store.set_block("general", "a", "<p>3</p>", "codex", kind="markdown")
        self.assertEqual(self.names(), ["a", "b"])
        a = snap["blocks"][0]
        self.assertEqual((a["html"], a["editor"], a["kind"], a["version"]), ("<p>3</p>", "codex", "markdown", 3))
        self.assertEqual(snap["version"], 3)
        self.assertTrue(snap["can_undo"])

    def test_remove_and_unknown(self):
        self.store.set_block("general", "a", "x", "claude")
        self.store.remove_block("general", "a", "claude")
        self.assertEqual(self.names(), [])
        with self.assertRaises(DashboardError):
            self.store.remove_block("general", "a", "claude")

    def test_base_version(self):
        self.store.set_block("general", "a", "x", "claude", base_version=0)       # create only
        with self.assertRaises(DashboardConflict) as cm:
            self.store.set_block("general", "a", "y", "codex", base_version=0)    # exists
        self.assertEqual(cm.exception.block["editor"], "claude")
        self.store.set_block("general", "b", "x", "codex")                        # channel v2
        self.store.set_block("general", "a", "y", "codex", base_version=1)        # other block moved: ok
        with self.assertRaises(DashboardConflict):
            self.store.set_block("general", "a", "z", "claude", base_version=1)   # stale
        self.store.set_block("general", "a", "z", "claude")                       # None: no check
        self.assertEqual(self.store.get("general")["blocks"][0]["html"], "z")

    def test_undo_each_op_and_versions_only_go_up(self):
        s = self.store
        s.set_block("general", "a", "a1", "claude")
        s.set_block("general", "b", "b1", "claude")
        s.set_block("general", "c", "c1", "claude")
        s.set_block("general", "b", "b2", "codex")
        s.remove_block("general", "a", "user")
        s.clear("general", "user")
        v = s.get("general")["version"]
        self.assertEqual(s.undo("general", "user")["version"], v + 1)    # undo clear
        self.assertEqual(self.names(), ["b", "c"])
        self.assertTrue(all(b["version"] == v + 1 for b in s.get("general")["blocks"]))
        s.undo("general", "user")                                         # undo remove: back at index 0
        self.assertEqual(self.names(), ["a", "b", "c"])
        s.undo("general", "user")                                         # undo replace: same spot
        self.assertEqual([b["html"] for b in s.get("general")["blocks"]], ["a1", "b1", "c1"])
        s.undo("general", "user")                                         # undo create
        self.assertEqual(self.names(), ["a", "b"])
        s.undo("general", "user")
        s.undo("general", "user")
        self.assertFalse(s.get("general")["can_undo"])
        with self.assertRaisesRegex(DashboardError, "Nothing to undo"):
            s.undo("general", "user")

    def test_history_trimmed(self):
        for i in range(dash.HISTORY_LIMIT + 5):
            self.store.set_block("general", "a", str(i), "claude")
        for _ in range(dash.HISTORY_LIMIT):
            self.store.undo("general", "user")
        self.assertEqual(self.store.get("general")["blocks"][0]["html"], "4")
        with self.assertRaises(DashboardError):
            self.store.undo("general", "user")

    def test_caps_and_validation(self):
        with self.assertRaisesRegex(DashboardError, "too large"):
            self.store.set_block("general", "a", "x" * (dash.MAX_BLOCK_BYTES + 1), "claude")
        for i in range(dash.MAX_BLOCKS):
            self.store.set_block("general", f"b{i}", "x", "claude")
        with self.assertRaisesRegex(DashboardError, "full"):
            self.store.set_block("general", "extra", "x", "claude")
        self.store.set_block("general", "b0", "y", "claude")  # replacing is still fine when full
        big = "x" * dash.MAX_BLOCK_BYTES
        with self.assertRaisesRegex(DashboardError, "in all"):
            for i in range(11):
                self.store.set_block("other", f"big{i}", big, "claude")
        for bad in ("", "a/b", "x" * 41, "../up"):
            with self.assertRaises(DashboardError):
                self.store.set_block("general", bad, "x", "claude")
        with self.assertRaises(DashboardError):
            self.store.set_block("general", "a", "x", "claude", kind="js")
        for bad_ch in ("../etc", "Upper", "", "general\n"):
            with self.assertRaises(DashboardError):
                self.store.get(bad_ch)

    def test_persists_across_restart(self):
        self.store.set_block("general", "a", "<b>é</b>", "claude")
        self.store.set_block("general", "b", "x", "claude")
        again = DashboardStore(str(self.dir))
        self.assertEqual([b["name"] for b in again.get("general")["blocks"]], ["a", "b"])
        again.undo("general", "user")   # history survives too
        self.assertEqual([b["name"] for b in again.get("general")["blocks"]], ["a"])
        self.assertEqual(list(self.dir.glob("*.tmp")), [])

    def test_corrupt_file_starts_empty_and_keeps_backup(self):
        (self.dir / "general.json").write_text("{not json", "utf-8")
        with self.assertLogs("dashboards", "ERROR"):
            self.assertEqual(self.store.get("general")["blocks"], [])
        self.assertTrue((self.dir / "general.json.bad").exists())

    def test_callbacks_once_per_change_not_for_noop_clear(self):
        self.assertEqual(self.store.clear("general", "user")[1], 0)
        self.assertEqual(self.events, [])
        self.store.set_block("general", "a", "x", "claude")
        snap, removed = self.store.clear("general", "user")
        self.assertEqual(removed, 1)
        self.assertEqual(self.events, [("general", 1), ("general", 2)])

    def test_unchanged_set_is_a_noop(self):
        self.store.set_block("general", "a", "x", "claude")
        snap = self.store.set_block("general", "a", "x", "codex", base_version=1)
        self.assertEqual((snap["version"], snap["blocks"][0]["editor"]), (1, "claude"))
        self.assertEqual(len(self.events), 1)
        self.store.undo("general", "user")
        self.assertFalse(self.store.get("general")["can_undo"])

    def test_rename_replaces_leftover_file(self):
        self.store.set_block("gone", "stale", "x", "claude")    # a deleted channel's leftover
        self.store.set_block("old", "a", "x", "claude")
        self.store.rename("old", "gone")
        self.assertEqual(self.names("gone"), ["a"])
        self.assertEqual(DashboardStore(str(self.dir)).get("gone")["blocks"][0]["name"], "a")
        self.store.rename("empty", "gone")                        # no dashboard: leftover removed
        self.assertFalse((self.dir / "gone.json").exists())

    def test_requested_by_sticks_through_updates_and_undo(self):
        self.store.set_block("general", "a", "x", "claude")
        self.assertIsNone(self.store.get("general")["blocks"][0]["requested_by"])
        snap = self.store.set_block("general", "a", "x", "claude", requested_by="ben")
        self.assertEqual(snap["version"], 2)                      # marking alone is a change
        self.store.set_block("general", "a", "y", "claude")       # later update keeps the mark
        self.assertEqual(self.store.get("general")["blocks"][0]["requested_by"], "ben")
        self.store.undo("general", "user")
        self.store.undo("general", "user")
        self.assertIsNone(self.store.get("general")["blocks"][0]["requested_by"])

    def test_rename_moves_file(self):
        self.store.set_block("old", "a", "x", "claude")
        self.store.rename("old", "new")
        self.assertFalse((self.dir / "old.json").exists())
        self.assertEqual(json.loads((self.dir / "new.json").read_text())["channel"], "new")
        self.assertEqual(DashboardStore(str(self.dir)).get("new")["blocks"][0]["name"], "a")


class AgentTextTest(unittest.TestCase):
    SNAP = {"channel": "general", "version": 7, "can_undo": True, "blocks": [
        {"name": "build", "kind": "html", "html": "<b>ok</b>", "editor": "claude",
         "updated_at": 1000.0, "version": 7},
        {"name": "tests", "kind": "markdown", "html": "- a", "editor": "codex",
         "updated_at": 0.0, "version": 3}]}

    def test_full_and_filtered(self):
        text = render_for_agent(self.SNAP, now=1120.0)
        self.assertIn("#general dashboard v7 (2 blocks)", text)
        self.assertIn("--- build · v7 · claude · 2m ago · html\n<b>ok</b>", text)
        one = render_for_agent(self.SNAP, block="tests", now=1120.0)
        self.assertIn("--- tests · v3 · codex", one)
        self.assertNotIn("build ·", one)
        self.assertIn("No block 'nope'", render_for_agent(self.SNAP, block="nope"))
        self.assertEqual(render_for_agent(dict(self.SNAP, blocks=[])), "#general dashboard is empty.")

    def test_relative_age(self):
        self.assertEqual(relative_age(100, 130), "just now")
        self.assertEqual(relative_age(0, 7200), "2h ago")
        self.assertEqual(relative_age(0, 3 * 86400), "3d ago")


if __name__ == "__main__":
    unittest.main()
