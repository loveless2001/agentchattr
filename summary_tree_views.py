"""Text views of a channel summary tree for agents (MCP tool output).

- render_header: the compressed channel history a fresh agent reads first.
- zoom: open one summary line into its halves, or into raw messages.
- recall: regex search over the channel's raw chat messages.

Works on a snapshot of one channel's tree state (see summaries.py for the
format) plus messages read from the MessageStore. No state of its own.
"""

import re
import time

from summary_tree_layout import node_level, pick_nodes

OUTPUT_CHARS = 20000   # cap for zoom/recall output (fits every agent CLI)
MSG_CHARS = 1500       # per-message cap when showing raw messages
SNIPPET_CHARS = 300    # recall snippet length
NEIGHBOURS = 8         # messages shown on each side for block='<message id>'
MAX_QUERY_CHARS = 200
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


def format_message(msg: dict, limit: int = MSG_CHARS) -> str:
    text = msg.get("text", "")
    if len(text) > limit:
        text = text[:limit] + f"… [+{len(text) - limit} chars]"
    names = [a.get("name", "") for a in msg.get("attachments") or [] if a.get("name")]
    if names:
        text += f" [attachments: {', '.join(names)}]"
    return f"#{msg['id']} [{_stamp(msg)}] {msg.get('sender', '?')}: {text}"


def _capped(lines: list[str]) -> str:
    out, size = [], 0
    for line in lines:
        if size + len(line) > OUTPUT_CHARS:
            out.append("(output truncated)")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def _node_lines(levels: list, lo: int, hi: int, out: list[str]):
    """One layout node; a node still being compressed shows its halves."""
    k = node_level(lo, hi)
    block = levels[k][lo >> k]
    if block["text"]:
        out.append(f"#{block['lo']}-{block['hi']} {block['text']}")
    elif k == 0:
        out.append(f"#{block['lo']}-{block['hi']} (summary pending — zoom to read "
                   f"these {len(block.get('ids', []))} messages)")
    else:
        mid = (lo + hi) // 2
        _node_lines(levels, lo, mid, out)
        _node_lines(levels, mid, hi, out)


def render_header(channel: str, st: dict, tail: list[dict], store, read_lines: int) -> str:
    levels = st["levels"]
    leaves = levels[0] if levels else []
    if not leaves and not tail:
        return ""
    total = sum(len(leaf.get("ids", [])) for leaf in leaves) + len(tail)
    first_id = leaves[0]["lo"] if leaves else tail[0]["id"]
    first = store.get_since(first_id - 1, channel=channel)[:1]
    since = _stamp(first[0], "%Y-%m-%d") if first else "?"
    lines = [
        f"[#{channel} summary — {total} messages since {since}, oldest first. "
        f"Expand a line: chat_summary(action='zoom', channel='{channel}', block='<a-b>'); "
        f"messages around an id: block='<id>'; search history: "
        f"chat_summary(action='recall', channel='{channel}', query='<regex>')]"
    ]
    for lo, hi in pick_nodes(len(leaves), read_lines):
        _node_lines(levels, lo, hi, lines)
    if not leaves:
        lines.append("(too few messages for a summary block yet)")
    if tail:
        lines.append(f"(+{len(tail)} newest messages not summarized yet — see the messages below)")
    pending = sum(1 for level in levels for b in level if b["text"] is None)
    if pending:
        lines.append(f"({pending} summary lines are still being written in the background)")
    lines.append("[end of summary]")
    return "\n".join(lines)


def _around(channel: str, msg_id: int, store) -> str:
    msgs = chat_only(store.get_since(-1, channel=channel))
    idx = next((i for i, m in enumerate(msgs) if m["id"] == msg_id), None)
    if idx is None:
        return f"Error: message #{msg_id} not found in #{channel}."
    window = msgs[max(0, idx - NEIGHBOURS): idx + NEIGHBOURS + 1]
    return _capped([f"Messages around #{msg_id} in #{channel}:"] + [format_message(m) for m in window])


def zoom(channel: str, st: dict, block: str, store) -> str:
    spec = (block or "").strip().lstrip("#")
    match = re.fullmatch(r"(\d+)(?:-(\d+))?", spec)
    if not match:
        return ("Error: block must be a summary line id like '120-151', "
                "or a message id like '137'.")
    if match.group(2) is None:
        return _around(channel, int(match.group(1)), store)
    lo, hi = int(match.group(1)), int(match.group(2))
    levels = st["levels"]
    found = next(((k, j) for k, level in enumerate(levels)
                  for j, b in enumerate(level) if b["lo"] == lo and b["hi"] == hi), None)
    if found is None:
        return (f"Error: no summary line #{lo}-{hi} in #{channel}. "
                "Use the ids exactly as the summary prints them.")
    k, j = found
    if k == 0:
        ids = set(levels[0][j].get("ids", []))
        msgs = [m for m in store.get_since(lo - 1, channel=channel) if m["id"] in ids]
        lines = [f"Raw messages of #{lo}-{hi} in #{channel}:"] + [format_message(m) for m in msgs]
        return _capped(lines)
    lines = [f"#{lo}-{hi} in #{channel} splits into:"]
    for kid in levels[k - 1][2 * j:2 * j + 2]:
        lines.append(f"#{kid['lo']}-{kid['hi']} {kid['text'] or '(summary pending)'}")
    lines.append("Zoom into either line to go deeper.")
    return "\n".join(lines)


def recall(st: dict, msgs: list[dict], query: str) -> str:
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
    leaf_of = {}
    for leaf in (st["levels"][0] if st["levels"] else []):
        for msg_id in leaf.get("ids", []):
            leaf_of[msg_id] = f" (in #{leaf['lo']}-{leaf['hi']})"
    hits = [(m, hit) for m in msgs if (hit := pattern.search(m.get("text", "")))]
    if not hits:
        return "No match."
    out, size = [], 0
    for msg, hit in reversed(hits):  # newest first until the cap
        text = msg.get("text", "")
        start = max(0, hit.start() - SNIPPET_CHARS // 3)
        snippet = text[start:start + SNIPPET_CHARS].replace("\n", " ")
        snippet = ("…" if start else "") + snippet + ("…" if start + SNIPPET_CHARS < len(text) else "")
        line = (f"#{msg['id']} [{_stamp(msg)}] {msg.get('sender', '?')}: {snippet}"
                f"{leaf_of.get(msg['id'], '')}")
        if size + len(line) > OUTPUT_CHARS:
            break
        out.append(line)
        size += len(line) + 1
    out.reverse()
    footer = (f"{len(hits)} matches." if len(out) == len(hits)
              else f"Newest {len(out)} of {len(hits)} matches — narrow the query.")
    return "\n".join(out + [footer + " Read around a hit: chat_summary(action='zoom', block='<id>')."])
