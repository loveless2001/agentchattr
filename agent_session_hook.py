"""Claude Code SessionStart hook installed by the wrapper (via --settings).

Claude passes the hook a JSON payload on stdin with the session id, the
transcript path and the reason the session started (startup, resume, clear,
compact). This script appends those fields as one JSON line to the file named
by AGENTCHATTR_SESSION_EVENTS, which the wrapper's session monitor follows to
know which transcript belongs to its agent.

It prints nothing (SessionStart stdout would be added to the agent's context)
and never fails the session.
"""

import json
import os
import sys
import time


def main():
    path = os.environ.get("AGENTCHATTR_SESSION_EVENTS")
    if not path:
        return
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    record = {key: payload.get(key) for key in ("session_id", "transcript_path", "source")}
    record["ts"] = time.time()
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    main()
