"""/sleep teardown (server_launcher.kill_session): the detached wrapper for a
channel must be found and stopped, or it sees its tmux session vanish, treats
that as a crash and recreates it. Uses real child processes whose argv looks
like a server-spawned wrapper's."""

import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server_launcher import ServerLauncher


def spawn_fake_wrapper(session_name: str) -> subprocess.Popen:
    """A long-lived process carrying the same --session-name argv as wrapper.py."""
    return subprocess.Popen([
        sys.executable, "-c", "import time; time.sleep(60)",
        "--detached", "--session-name", session_name, "--channel", "x",
    ])


@unittest.skipIf(sys.platform == "win32", "server launcher is POSIX-only")
class KillSessionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.launcher = ServerLauncher(Path(self._tmp.name), {})
        # Unique names so a concurrently running server is never touched.
        self.session = f"agentchattr-codex-t{uuid.uuid4().hex[:8]}"
        self.procs: list[subprocess.Popen] = []

    def tearDown(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        self._tmp.cleanup()

    def _spawn(self, session_name: str) -> subprocess.Popen:
        proc = spawn_fake_wrapper(session_name)
        self.procs.append(proc)
        time.sleep(0.2)  # let the exec land so pgrep sees the final argv
        return proc

    def test_finds_wrapper_by_session_name(self):
        proc = self._spawn(self.session)
        self.assertEqual(self.launcher._wrapper_pids(self.session), [str(proc.pid)])

    def test_does_not_match_channel_sharing_a_prefix(self):
        # #ai must not pick up the #ai-os wrapper.
        other = self._spawn(f"{self.session}-os")
        self.assertEqual(self.launcher._wrapper_pids(self.session), [])
        self.assertEqual(self.launcher._wrapper_pids(f"{self.session}-os"), [str(other.pid)])

    def test_kill_session_stops_wrapper_only_for_that_channel(self):
        target = self._spawn(self.session)
        other = self._spawn(f"{self.session}-os")
        self.launcher.kill_session(self.session)
        self.assertIsNotNone(target.wait(timeout=5))
        self.assertIsNone(other.poll(), "wrapper for a different channel was killed")


if __name__ == "__main__":
    unittest.main()
