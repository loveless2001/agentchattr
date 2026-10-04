"""Server log cap (server_log_trimmer.py): trimming real files in place, under
a live appending writer, and the whole start() path in a child process whose
output is redirected the way the launchers do it (`>` and `>>`)."""

import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server_log_trimmer

LINE_BYTES = 54  # "line 0000000 " + 40 filler bytes + "\n"


def write_lines(path: Path, count: int, filler: str = "x"):
    with open(path, "w") as f:
        for i in range(count):
            f.write(f"line {i:07d} {filler * 40}\n")


def line_numbers(data: bytes) -> list[int]:
    rows = data.decode().splitlines()
    assert all(len(r) == LINE_BYTES - 1 and r.startswith("line ") for r in rows), "torn line"
    return [int(r.split()[1]) for r in rows]


def last_data_line(data: bytes) -> str:
    """The child's last printed line (the trimmer logs its own lines too)."""
    return [r for r in data.decode().splitlines() if r.startswith("line ")][-1]


@unittest.skipIf(sys.platform == "win32", "the log cap needs fcntl (POSIX)")
class ServerLogTrimmerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_under_cap_leaves_file_untouched(self):
        path = self.dir / "server.log"
        write_lines(path, 100)
        before = path.read_bytes()
        self.assertEqual(server_log_trimmer.trim_oldest_lines(path, 10**6, 5000), 0)
        self.assertEqual(path.read_bytes(), before)

    def test_over_cap_keeps_newest_whole_lines_in_order(self):
        path = self.dir / "server.log"
        write_lines(path, 20000)
        size = path.stat().st_size
        dropped = server_log_trimmer.trim_oldest_lines(path, 500_000, 375_000)
        data = path.read_bytes()
        self.assertLessEqual(len(data), 375_000)
        self.assertEqual(dropped, size - len(data))
        nums = line_numbers(data)
        self.assertEqual(nums, list(range(nums[0], 20000)))  # newest kept, none missing

    def test_trims_under_a_live_appending_writer(self):
        path = self.dir / "server.log"
        write_lines(path, 50000)
        writer = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
            import os, time
            fd = os.open({str(path)!r}, os.O_WRONLY | os.O_APPEND)
            i, end = 1_000_000, time.time() + 2
            while time.time() < end:
                os.write(fd, f"line {{i:07d}} {{'y' * 40}}\\n".encode())
                i += 1
            print(i - 1)
        """)], stdout=subprocess.PIPE, text=True)
        trims = 0
        while writer.poll() is None:
            trims += bool(server_log_trimmer.trim_oldest_lines(path, 400_000, 300_000))
            time.sleep(0.01)
        last = int(writer.stdout.read())
        writer.stdout.close()
        data = path.read_bytes()
        self.assertNotIn(b"\0", data)
        nums = line_numbers(data)
        self.assertGreater(trims, 10)
        self.assertEqual(nums[-1], last)
        written = [n for n in nums if n >= 1_000_000]
        lost = sum(b - a - 1 for a, b in zip(written, written[1:]))
        self.assertLessEqual(lost, trims, "at most one line lost per trim")

    def _run_capped_child(self, mode: str) -> bytes:
        """A child that calls start() with stdout redirected to a stale,
        oversized log (opened with `mode`), then writes a lot."""
        path = self.dir / f"server-{mode}.log"
        write_lines(path, 30000)  # ~1.6 MB, far over the 0.2 MB cap
        child = textwrap.dedent(f"""
            import logging, sys, time
            sys.path.insert(0, {str(ROOT)!r})
            logging.basicConfig(level=logging.INFO)
            import server_log_trimmer as t
            t.CHECK_SECONDS = 0.05
            assert t.start({{"log_max_mb": 0.2}}), "no file capped"
            for i in range(40000):
                print(f"line {{i:07d}} {{'z' * 40}}", flush=True)
                if i % 2000 == 0:
                    time.sleep(0.06)
            time.sleep(0.3)
        """)
        with open(path, mode) as out:
            proc = subprocess.run([sys.executable, "-c", child], stdout=out,
                                  stderr=subprocess.STDOUT, timeout=60)
        data = path.read_bytes()
        self.assertEqual(proc.returncode, 0, data[-2000:])
        return data

    def test_start_caps_output_redirected_with_truncate(self):
        # `>` gives the child its own file offset: without O_APPEND a trim
        # would leave a zero-filled hole.
        data = self._run_capped_child("w")
        self.assertNotIn(b"\0", data)
        self.assertLessEqual(len(data), int(0.2 * 2**20) + 120_000)
        self.assertEqual(last_data_line(data), f"line 0039999 {'z' * 40}")

    def test_start_caps_output_redirected_with_append(self):
        data = self._run_capped_child("a")
        self.assertNotIn(b"\0", data)
        self.assertLessEqual(len(data), int(0.2 * 2**20) + 120_000)
        self.assertEqual(last_data_line(data), f"line 0039999 {'z' * 40}")

    def test_no_cap_for_pipe_output_or_zero(self):
        code = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); import server_log_trimmer as t; "
                "print(t.start({}), t.start({'log_max_mb': 0}))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "[] []", out.stderr)


if __name__ == "__main__":
    unittest.main()
