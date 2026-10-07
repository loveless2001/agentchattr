"""Conditions of a registered wait (agent_waits.py) and the texts about it.

  pid             ends when the process exits; its start time (/proc) tells a
                  reused pid apart, and a zombie counts as exited;
  path            ends when the file appears;
  path + contains ends when a line appended to the file after registration
                  contains one of the |-separated texts. Plain text, not a
                  regex: an agent-supplied regex could hang the server. The log
                  is read incrementally from a stored offset; \\r ends a line too
                  (progress bars), an unterminated last line counts once the file
                  stops growing, and a replaced file is read from its start;
  deadline        ends when the timeout passes.

check() works on a copy of the wait and may update its log position fields
(offset, inode, tail_at); WaitStore writes them back.
"""

import os
import re

MAX_LINE_CHARS = 4096             # a log line is matched on its first 4 KB
MAX_READ_BYTES = 8 * 1024 * 1024  # log bytes read per check
_LINE_END = re.compile(rb"[\r\n]")


def proc_stat(pid: int) -> tuple[str, str] | None:
    """(state, start time) from /proc/<pid>/stat, or None if there is no such process."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            fields = f.read().decode("utf-8", "replace").rsplit(")", 1)[1].split()
        return fields[0], fields[19]  # fields 3 and 22 of proc(5)
    except (OSError, IndexError):
        return None


def pid_alive(pid: int, start_time: str | None = None) -> bool:
    if os.path.isdir("/proc"):
        stat = proc_stat(pid)
        return bool(stat) and stat[0] != "Z" and start_time in (None, stat[1])
    try:  # no /proc (macOS): existence only
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def contains_texts(contains: str) -> list[str]:
    return [text for text in contains.split("|") if text]


def _scan_log(wait: dict) -> str | None:
    """The first line appended to the wait's log since the last check that
    contains one of its texts; advances the wait's offset past the lines read."""
    try:
        stat = os.stat(wait["path"])
        with open(wait["path"], "rb") as f:
            offset = wait.get("offset", 0)
            if stat.st_ino != wait.get("inode") or stat.st_size < offset:
                offset = 0  # replaced or truncated: read it from the start
                wait["inode"] = stat.st_ino
            if stat.st_size == offset:
                return None
            f.seek(offset)
            data = f.read(min(stat.st_size - offset, MAX_READ_BYTES))
    except OSError:
        return None  # not created yet, or unreadable for now
    ends = [m.end() for m in _LINE_END.finditer(data)]
    cut = ends[-1] if ends else 0  # bytes of complete lines
    if cut < len(data):  # an unterminated tail
        if wait.get("tail_at") == stat.st_size or (not ends and len(data) == MAX_READ_BYTES):
            cut = len(data)  # the file stopped growing (or one huge line): take it as it is
        else:
            wait["tail_at"] = stat.st_size
    if not cut:
        return None
    wait["offset"] = offset + cut
    texts = contains_texts(wait["contains"])
    for raw in _LINE_END.split(data[:cut]):
        line = raw[:MAX_LINE_CHARS].decode("utf-8", "replace")
        if any(text in line for text in texts):
            return line
    return None


def check(wait: dict, now: float) -> str | None:
    """Why the wait is over, or None while it still holds."""
    if wait.get("pid") and not pid_alive(wait["pid"], wait.get("pid_start")):
        return f"process {wait['pid']} exited"
    if wait.get("path"):
        if wait.get("contains"):
            line = _scan_log(wait)
            if line is not None:
                return f'{wait["path"]} logged: "{" ".join(line.split())[:200]}"'
        elif os.path.exists(wait["path"]):
            return f"{wait['path']} appeared"
    if now >= wait["deadline"]:
        return f"timed out after {_duration(wait['deadline'] - wait['created_at'])}"
    return None


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def describe(wait: dict) -> str:
    """'until process 4242 exits, or 24h00m pass' — for tool replies."""
    parts = []
    if wait.get("pid"):
        parts.append(f"process {wait['pid']} exits")
    if wait.get("path"):
        parts.append(f"{wait['path']} logs a line containing {wait['contains']!r}"
                     if wait.get("contains") else f"{wait['path']} appears")
    parts.append(f"{_duration(wait['deadline'] - wait['created_at'])} pass")
    return "until " + ", or ".join(parts)


def wake_prompt(ended: list[tuple[dict, str]]) -> str:
    """One prompt for the waits of one agent and channel that ended together
    (the wrapper keeps only one prompt per channel and batch)."""
    over = "; ".join(f"wait #{w['id']} ({w['note']}) is over: {reason}" for w, reason in ended)
    return (f"Your {over}. Check the result, carry on with the task, and post an update "
            f"in #{ended[0][0]['channel']} with chat_send.")


def ended_notice(wait: dict, reason: str, online: bool) -> str:
    # The note is the agent's own text: quoted and stripped of @, so it can't mention anyone.
    why = "a watched log line matched" if " logged: " in reason else reason.replace("@", "")
    tail = "" if online else f" {wait['agent']} is offline, so it was not woken."
    return f"{wait['agent']}'s wait #{wait['id']} \"{wait['note']}\" is over: {why}.{tail}"
