"""Text views of a channel summary tree for agents (MCP tool output).

- render_header: the compressed channel history a fresh agent reads first.
- zoom: open one summary line into its halves, or show one message whole.
- recall: regex search over the channel's raw chat messages.
- context_lines: the summary before a node, as context for its compressor.

Works on a snapshot of one channel's tree state (see summaries.py for the
format) plus messages read from the MessageStore. No state of its own.
"""

import re
import time

from summary_tree_layout import fold

OUTPUT_CHARS = 20000   # cap for zoom/recall output (fits every agent CLI)
MSG_CHARS = 1500       # per-message cap for messages shown next to the one asked for
SNIPPET_CHARS = 300    # recall snippet length
NEIGHBOURS = 8         # messages shown on each side for block='<message id>'
MAX_QUERY_CHARS = 200
DELETED = "(message deleted)"  # the line of a deleted message's leaf
PLACEHOLDER_BYTES = 512        # header budget charged for a line not written yet
# A quantified group that itself contains a quantifier, e.g. (a+)+ or (\w+\s?)*:
# the classic catastrophic-backtracking shape. Python's re has no timeout and
# holds the GIL, so one such query could stall the whole server.
_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*[+*{]")


def chat_only(msgs: list[dict]) -> list[dict]:
    """Messages that form channel history (no joins, system notes, cards)."""
    return [m for m in msgs if m.get("type", "chat") == "chat"]


def _stamp(msg: dict, fmt: str = "%Y-%m-%d %H:%M") -> str:
    ts = msg.get("timestamp")
    return time.strftime(fmt, time.localtime(ts)) if ts else msg.get("time", "")


def attachment_note(msg: dict) -> str:
    """' [attachments: a.png, b.pdf]' for a message with named attachments, else ''."""
    names = [" ".join(a["name"].split()) for a in msg.get("attachments") or [] if a.get("name")]
    return f" [attachments: {', '.join(names)}]" if names else ""


def message_line(msg: dict) -> str:
    """A message on one line, as a free leaf keeps it: `sender: text`, every
    whitespace run (line breaks of any kind included) folded to one space, so
    in a node made of several messages each line is one message and text
    inside a message cannot pass for another sender."""
    sender = " ".join(str(msg.get("sender", "?")).split())
    return f"{sender}: " + " ".join((msg.get("text", "") + attachment_note(msg)).split())


def format_message(msg: dict, limit: int = MSG_CHARS) -> str:
    text = msg.get("text", "")
    if len(text) > limit:
        text = text[:limit] + f"… [+{len(text) - limit} chars]"
    return f"#{msg['id']} [{_stamp(msg)}] {msg.get('sender', '?')}: {text}{attachment_note(msg)}"


def _capped(lines: list[str]) -> str:
    out, size = [], 0
    for line in lines:
        if size + len(line) > OUTPUT_CHARS:
            out.append("(output truncated)")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def shown_text(block: dict) -> str | None:
    """Line shown to agents: the current one, else the stale line it replaces
    while a tree is rebuilt after a TREE_VERSION change."""
    return block["text"] or block.get("old")


def block_id(block: dict) -> str:
    """'#lo-hi' for a node, '#id' for a single message."""
    return f"#{block['lo']}" if block["lo"] == block["hi"] else f"#{block['lo']}-{block['hi']}"


def _part_line(level: int, block: dict) -> str:
    """A node as agents see it: its id, then its line; a free node made of
    several messages continues on indented lines, one message each."""
    text = shown_text(block)
    if not text:
        text = ("(not summarized yet — zoom to read it)" if level == 0
                else f"(summary pending — zoom to read these {1 << level} messages)")
    return f"{block_id(block)} " + text.replace("\n", "\n    ")


def context_lines(levels: list, leaf_end: int, budget: int) -> list[str]:
    """Written summary lines covering leaves [0, leaf_end), coarse old to fine
    recent, within about `budget` bytes, as bare text (no block ids: a
    compressor shown ids copies them). Unwritten nodes are left out, so a
    compressor never sees a placeholder or a stale line."""
    def cost(k: int, j: int) -> tuple[int, bool]:
        text = levels[k][j]["text"]
        return (len(text.encode()) + 1, True) if text else (0, False)

    texts = (levels[k][j]["text"] for k, j in fold(leaf_end, budget, cost))
    return [text for text in texts if text]


def render_header(channel: str, st: dict, store, read_bytes: int) -> str:
    levels = st["levels"]
    leaves = levels[0] if levels else []
    if not leaves:
        return ""
    total = sum(1 for leaf in leaves if leaf["text"] != DELETED)
    first = store.get_since(leaves[0]["lo"] - 1, channel=channel)[:1]
    since = _stamp(first[0], "%Y-%m-%d") if first else "?"
    lines = [
        f"[#{channel} summary — {total} messages since {since}, oldest first. "
        f"Expand a line: chat_summary(action='zoom', channel='{channel}', block='<a-b>'); "
        f"one message whole, with its neighbours: block='<id>'; search history: "
        f"chat_summary(action='recall', channel='{channel}', query='<regex>')]"
    ]

    # A placeholder costs the line it stands for (about PLACEHOLDER_BYTES), not
    # its few bytes of text: otherwise, while a tree is (re)built, cheap
    # placeholders fill the budget and the fold never climbs to the written
    # (or old) lines above them.
    def cost(k: int, j: int) -> tuple[int, bool]:
        block = levels[k][j]
        if not shown_text(block):
            return PLACEHOLDER_BYTES, False
        return len(_part_line(k, block).encode()) + 1, True

    lines += [_part_line(k, levels[k][j]) for k, j in fold(len(leaves), read_bytes, cost)]
    pending = sum(1 for level in levels for b in level if b["text"] is None)
    if pending:
        lines.append(f"({pending} summary lines are still being written in the background)")
    lines.append("[end of summary]")
    return "\n".join(lines)


def _around(channel: str, msg_id: int, store) -> str:
    """One message whole, with up to NEIGHBOURS messages (capped) on each
    side, nearest first while the output stays under OUTPUT_CHARS."""
    msgs = chat_only(store.get_since(-1, channel=channel))
    idx = next((i for i, m in enumerate(msgs) if m["id"] == msg_id), None)
    if idx is None:
        return f"Error: message #{msg_id} not found in #{channel}."
    shown = {idx: format_message(msgs[idx], OUTPUT_CHARS)}
    room = OUTPUT_CHARS - len(shown[idx])
    for step in (-1, 1):
        for i in range(idx + step, idx + step * (NEIGHBOURS + 1), step):
            if not 0 <= i < len(msgs):
                break
            line = format_message(msgs[i])
            if len(line) + 1 > room:
                break
            shown[i] = line
            room -= len(line) + 1
    return "\n".join([f"Message #{msg_id} in #{channel} (whole), with its neighbours:"]
                     + [shown[i] for i in sorted(shown)])


def zoom(channel: str, st: dict, block: str, store) -> str:
    spec = (block or "").strip().lstrip("#")
    match = re.fullmatch(r"(\d+)(?:-(\d+))?", spec)
    if not match:
        return ("Error: block must be a summary line id like '120-151', "
                "or a message id like '137'.")
    lo = int(match.group(1))
    hi = int(match.group(2) or lo)
    if lo == hi:
        return _around(channel, lo, store)
    levels = st["levels"]
    found = next(((k, j) for k, level in enumerate(levels)
                  for j, b in enumerate(level) if b["lo"] == lo and b["hi"] == hi), None)
    if found is None:
        return (f"Error: no summary line #{lo}-{hi} in #{channel}. "
                "Use the ids exactly as the summary prints them.")
    k, j = found
    lines = [f"#{lo}-{hi} in #{channel} splits into:"]
    lines += [_part_line(k - 1, kid) for kid in levels[k - 1][2 * j:2 * j + 2]]
    lines.append("Zoom into either line to go deeper, or into a message id to read it whole.")
    return _capped(lines)


def recall(msgs: list[dict], query: str) -> str:
    if not (query or "").strip():
        return "Error: query (a regex) is required."
    if len(query) > MAX_QUERY_CHARS:
        return f"Error: query too long (max {MAX_QUERY_CHARS} characters)."
    if _NESTED_QUANTIFIER.search(query):
        return "Error: nested quantifiers like (a+)+ are not allowed — simplify the regex."
    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error as exc:
        return f"Error: bad regex: {exc}"
    hits = [(m, hit) for m in msgs if (hit := pattern.search(m.get("text", "")))]
    if not hits:
        return "No match."
    out, size = [], 0
    for msg, hit in reversed(hits):  # newest first until the cap
        text = msg.get("text", "")
        start = max(0, hit.start() - SNIPPET_CHARS // 3)
        snippet = text[start:start + SNIPPET_CHARS].replace("\n", " ")
        snippet = ("…" if start else "") + snippet + ("…" if start + SNIPPET_CHARS < len(text) else "")
        line = f"#{msg['id']} [{_stamp(msg)}] {msg.get('sender', '?')}: {snippet}"
        if size + len(line) > OUTPUT_CHARS:
            break
        out.append(line)
        size += len(line) + 1
    out.reverse()
    footer = (f"{len(hits)} matches." if len(out) == len(hits)
              else f"Newest {len(out)} of {len(hits)} matches — narrow the query.")
    return "\n".join(out + [footer + " Read a hit whole, with its neighbours: "
                                     "chat_summary(action='zoom', block='<id>')."])
