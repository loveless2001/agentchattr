"""Load the chat log (JSONL) defensively, for MessageStore._load.

Each message is appended as one line, so a crash mid-write can leave a torn
line: a message cut off, often with the next message appended onto the same
line (the cut also lost the newline). Loading used to skip any line that was
not valid JSON, silently dropping the torn message and the one glued to it.

Here every complete message found in a damaged line is recovered and the
torn remainder is reported (never guessed at: the rest of that message was
never written). The damaged file is copied aside before anything touches it;
MessageStore then rewrites the log clean. A missing final newline is restored
so the next append starts on its own line.
"""

import json
import logging
import re
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

_DECODER = json.JSONDecoder()
# Keys every stored message has (MessageStore.add and _add_internal); used to
# tell a recovered message from an object nested inside a torn one.
_MESSAGE_KEYS = {"id", "sender", "type", "timestamp"}
_ID_RE = re.compile(r'"id":\s*(\d+)')


def _is_message(obj) -> bool:
    return isinstance(obj, dict) and _MESSAGE_KEYS <= obj.keys() and isinstance(obj["id"], int)


def _salvage(line: str) -> tuple[list[dict], list[str]]:
    """Complete messages inside a damaged line, and the fragments around them."""
    found, junk, pos, start = [], [], 0, 0
    while (brace := line.find("{", pos)) >= 0:
        try:
            obj, end = _DECODER.raw_decode(line, brace)
        except json.JSONDecodeError:
            pos = brace + 1
            continue
        # A whole message ends the line or is followed by the next one; an
        # object nested in a torn message is followed by more of that message.
        rest = line[end:].lstrip()
        if not _is_message(obj) or (rest and rest[0] != "{"):
            pos = brace + 1
            continue
        if line[start:brace].strip():
            junk.append(line[start:brace])
        found.append(obj)
        pos = start = end
    if line[start:].strip():
        junk.append(line[start:])
    return found, junk


def load_chat_log(path: Path) -> tuple[list[dict], list[dict], int, Path | None]:
    """Read the log: (messages, problems, next_id, backup).

    problems lists the unrecoverable fragments as {"line", "preview", "ids"}
    (ids that look like the torn message's own, so they are never reused);
    backup is the copy of the file taken when any line was damaged.
    """
    raw = path.read_bytes()
    messages, problems, recovered = [], [], 0
    max_id = -1
    for i, chunk in enumerate(raw.split(b"\n")):
        if not chunk.strip():
            continue
        try:
            msg = json.loads(chunk.decode("utf-8"))
            if not isinstance(msg, dict):
                raise ValueError("not an object")
            msg.setdefault("id", i)  # legacy lines had no id: use the line number
            found = [msg]
        except ValueError:  # bad JSON or bad UTF-8: salvage what is whole
            found, junk = _salvage(chunk.decode("utf-8", errors="replace"))
            recovered += len(found)
            for fragment in junk:
                ids = [int(x) for x in _ID_RE.findall(fragment)]
                problems.append({"line": i + 1, "preview": fragment[:120], "ids": ids})
                max_id = max([max_id, *ids])
        for msg in found:
            max_id = max(max_id, msg["id"])
        messages.extend(found)

    backup = None
    if recovered or problems:
        backup = path.with_name(f"{path.name}.damaged-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        log.warning("Chat log %s was damaged: recovered %d messages from torn lines, "
                    "%d fragments unrecoverable; original saved as %s",
                    path, recovered, len(problems), backup)
    if raw and not raw.endswith(b"\n"):
        with open(path, "ab") as f:  # the next append must not glue onto this line
            f.write(b"\n")
    return messages, problems, max_id + 1, backup
