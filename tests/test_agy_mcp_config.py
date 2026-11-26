"""agy (Antigravity CLI) per-instance HOME overlay with its own MCP config."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agy_mcp_config import build_agy_home_overlay  # noqa: E402
import wrapper  # noqa: E402

URL = "http://127.0.0.1:5555/mcp"


@unittest.skipIf(sys.platform == "win32", "overlay uses POSIX symlinks")
class AgyHomeOverlayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.real = root / "home"
        self.overlay = root / "overlay"
        cfg = self.real / ".gemini" / "config"
        cfg.mkdir(parents=True)
        (self.real / ".gitconfig").write_text("[user]\n")
        (self.real / ".gemini" / "oauth_creds.json").write_text("{}")
        (cfg / "config.json").write_text("{}")
        self.real_mcp = cfg / "mcp_config.json"
        self.real_mcp.write_text(json.dumps({"mcpServers": {"other": {"serverUrl": "http://x"}}}))

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, url=URL):
        return build_agy_home_overlay(self.overlay, self.real, "agentchattr", url)

    def test_overlay_mirrors_home_and_owns_mcp_config(self):
        path = self.build()
        self.assertEqual(path, self.overlay / ".gemini" / "config" / "mcp_config.json")
        self.assertFalse(path.is_symlink())
        servers = json.loads(path.read_text())["mcpServers"]
        self.assertEqual(servers["agentchattr"], {"serverUrl": URL, "disabled": False})
        self.assertIn("other", servers)  # real servers stay available
        for rel in (".gitconfig", ".gemini/oauth_creds.json", ".gemini/config/config.json"):
            self.assertTrue((self.overlay / rel).is_symlink(), rel)
        self.assertFalse((self.overlay / ".gemini").is_symlink())
        # the real config is never written
        self.assertNotIn("agentchattr", json.loads(self.real_mcp.read_text())["mcpServers"])

    def test_rebuild_syncs_links_and_keeps_overlay_files(self):
        self.build()
        (self.overlay / "notes.txt").write_text("made by agent")
        (self.real / ".gitconfig").unlink()
        (self.real / ".bashrc").write_text("")
        self.build("http://127.0.0.1:6666/mcp")
        self.assertFalse(os.path.lexists(self.overlay / ".gitconfig"))  # dangling dropped
        self.assertTrue((self.overlay / ".bashrc").is_symlink())        # new entry linked
        self.assertEqual((self.overlay / "notes.txt").read_text(), "made by agent")
        servers = json.loads((self.overlay / ".gemini/config/mcp_config.json").read_text())
        self.assertEqual(servers["mcpServers"]["agentchattr"]["serverUrl"], "http://127.0.0.1:6666/mcp")

    def test_gemini_builtin_sets_home_to_instance_overlay(self):
        cfg = wrapper._resolve_mcp_inject("gemini", {"command": "agy"})
        self.assertEqual(cfg["mcp_inject"], "proxy_agy_config")
        self.assertIn("proxy_agy_config", wrapper._PROXY_INJECT_MODES)
        data_dir = Path(self.tmp.name) / "data"
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.real)
        try:
            args, env, path = wrapper._apply_mcp_inject(cfg, "gemini-2", data_dir, URL)
        finally:
            os.environ["HOME"] = old_home
        overlay = data_dir / "agy-home" / "gemini-2"
        self.assertEqual(args, [])
        self.assertEqual(env, {"HOME": str(overlay)})
        self.assertEqual(path, overlay / ".gemini" / "config" / "mcp_config.json")

    def test_windows_writes_real_config_without_overlay_and_removes_own_entry(self):
        from unittest import mock
        from agy_mcp_config import remove_agy_mcp_entry
        cfg = wrapper._resolve_mcp_inject("gemini", {"command": "agy"})
        data_dir = Path(self.tmp.name) / "data"
        with mock.patch.dict(os.environ, {"HOME": str(self.real)}), \
                mock.patch.object(sys, "platform", "win32"):
            args, env, path = wrapper._apply_mcp_inject(cfg, "gemini", data_dir, URL)
        self.assertEqual((args, env, path), ([], {}, self.real_mcp))
        self.assertFalse((data_dir / "agy-home").exists())
        servers = json.loads(self.real_mcp.read_text())["mcpServers"]
        self.assertEqual(servers["agentchattr"]["serverUrl"], URL)
        self.assertIn("other", servers)
        self.assertFalse(remove_agy_mcp_entry(self.real_mcp, "agentchattr", "http://another-proxy"))
        self.assertTrue(remove_agy_mcp_entry(self.real_mcp, "agentchattr", URL))
        self.assertNotIn("agentchattr", json.loads(self.real_mcp.read_text())["mcpServers"])


if __name__ == "__main__":
    unittest.main()
