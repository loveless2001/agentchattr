"""Keep the server's log file under a size cap by dropping its oldest lines.

The launchers redirect the server's stdout and stderr to a file
(data/server.log), and nothing ever shrinks it: heartbeat access lines alone
add hundreds of MB a day. Once that file is over [server].log_max_mb, a
background thread drops its oldest lines in place (FIFO), keeping the newest
KEEP_FRACTION of the cap, starting at a whole line. The file keeps its inode,
so the shell redirection goes on writing to it.

Trimming in place is only safe while every writer appends (O_APPEND): a writer
with its own file offset would go on writing past the new end, leaving a hole
of zero bytes. `>>` already appends; after `>`, the server turns O_APPEND on
for its own stdout/stderr. Where it cannot (Windows has no fcntl), there is no
cap. The file is checked every CHECK_SECONDS, so it can run over the cap by
what is written in that time; a line written in the instant between the last
copy and the truncate is lost.
"""

import logging
import os
import stat
import sys
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

log = logging.getLogger(__name__)

DEFAULT_MAX_MB = 50
KEEP_FRACTION = 0.75  # a trim keeps the newest 3/4 of the cap, so trims stay rare
CHECK_SECONDS = 10
_CHUNK = 1 << 20


def trim_oldest_lines(path, max_bytes: int, keep_bytes: int) -> int:
    """If the file is over max_bytes, drop its oldest lines so that at most
    keep_bytes of the newest whole lines remain, rewriting it in place.
    Returns the number of bytes dropped (0 when under the cap)."""
    with open(path, "r+b") as f:
        size = f.seek(0, os.SEEK_END)
        if size <= max_bytes:
            return 0
        f.seek(size - keep_bytes)
        f.readline()  # skip the line cut in two: keep whole lines only
        src, dst = f.tell(), 0
        while True:  # copy forward, including lines appended meanwhile
            f.seek(src)
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            src += len(chunk)
            f.seek(dst)
            f.write(chunk)
            dst += len(chunk)
        f.truncate(dst)
        return src - dst


def _output_file(fd: int) -> Path | None:
    """The regular file fd writes to, or None (a terminal, a pipe, a deleted
    file, or a platform where the path cannot be found)."""
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        if sys.platform.startswith("linux"):
            path = os.readlink(f"/proc/self/fd/{fd}")
        elif hasattr(fcntl, "F_GETPATH"):  # macOS
            path = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0].decode()
        else:
            return None
        return Path(path) if os.path.samestat(st, os.stat(path)) else None
    except (OSError, ValueError):
        return None


def _ensure_append(fd: int) -> bool:
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        if not flags & os.O_APPEND:
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_APPEND)
        return bool(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_APPEND)
    except OSError:
        return False


def start(server_cfg: dict) -> list[Path]:
    """Cap the file(s) the server's stdout/stderr are redirected to at
    server_cfg["log_max_mb"] (0 = no cap). Returns the capped files."""
    max_mb = float(server_cfg.get("log_max_mb", DEFAULT_MAX_MB))
    if max_mb <= 0 or fcntl is None:
        return []
    fds = [fd for fd in (1, 2) if _output_file(fd)]
    for fd in fds:
        if not _ensure_append(fd):
            log.warning("No server log cap: cannot make fd %d append-only", fd)
            return []
    files = sorted({_output_file(fd) for fd in fds} - {None})
    if not files:
        return []  # output goes to a terminal or pipe: nothing to cap
    max_bytes = int(max_mb * 1024 * 1024)
    keep_bytes = int(max_bytes * KEEP_FRACTION)
    threading.Thread(target=_loop, args=(fds, max_bytes, keep_bytes),
                     daemon=True, name="server-log-trimmer").start()
    log.info("Server log capped at %g MB (oldest lines dropped first): %s",
             max_mb, ", ".join(map(str, files)))
    return files


def _loop(fds: list[int], max_bytes: int, keep_bytes: int):
    last_error = None
    while True:
        # Re-resolved every time: a file deleted or replaced under its name
        # is left alone.
        for path in {_output_file(fd) for fd in fds} - {None}:
            try:
                dropped = trim_oldest_lines(path, max_bytes, keep_bytes)
                last_error = None
            except OSError as exc:
                if str(exc) != last_error:  # don't repeat it every check
                    log.warning("Could not trim server log %s: %s", path, exc)
                last_error = str(exc)
                continue
            if dropped:
                log.info("Trimmed server log %s: dropped the oldest %.1f MB",
                         path, dropped / (1024 * 1024))
        time.sleep(CHECK_SECONDS)
