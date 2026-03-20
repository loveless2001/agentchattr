"""Lightweight security scanner for uploaded text content.

Pure-function module with no external dependencies.
Scans for prompt injection, invisible unicode, and suspicious code patterns.
"""

import re
from typing import Optional


# ---------------------------------------------------------------------------
# Type alias
# ---------------------------------------------------------------------------
Warning = dict  # {"type": str, "detail": str, "severity": "low"|"medium"|"high"}


# ---------------------------------------------------------------------------
# 1. Prompt injection detection
# ---------------------------------------------------------------------------

_PROMPT_INJECTION_PATTERNS = [
    (r"ignore\s+(previous|all)\s+instructions", "high"),
    (r"you\s+are\s+now\b", "high"),
    (r"system\s+prompt", "high"),
    (r"disregard\s+above", "high"),
    (r"forget\s+your\s+instructions", "high"),
    (r"new\s+instructions", "high"),
    # XML / markdown injection tokens
    (r"<system>", "high"),
    (r"<\|im_start\|>", "high"),
    (r"\[INST\]", "high"),
    (r"<<SYS>>", "high"),
    (r"</s>", "high"),
    # Role-play triggers
    (r"act\s+as\s+root", "high"),
    (r"pretend\s+you\s+are", "high"),
    (r"simulate\s+being", "high"),
]

_COMPILED_INJECTION = [
    (re.compile(pattern, re.IGNORECASE), severity)
    for pattern, severity in _PROMPT_INJECTION_PATTERNS
]


def _check_prompt_injection(text: str) -> list[Warning]:
    """Detect prompt injection attempts in text."""
    warnings: list[Warning] = []
    for pattern, severity in _COMPILED_INJECTION:
        match = pattern.search(text)
        if match:
            warnings.append({
                "type": "prompt_injection",
                "detail": f"Matched pattern: {match.group(0)!r}",
                "severity": severity,
            })
    return warnings


# ---------------------------------------------------------------------------
# 2. Invisible unicode detection
# ---------------------------------------------------------------------------

# Zero-width characters (low severity)
_ZERO_WIDTH_CHARS = {
    "\u200b": "zero-width space (U+200B)",
    "\u200c": "zero-width non-joiner (U+200C)",
    "\u200d": "zero-width joiner (U+200D)",
    "\ufeff": "BOM/zero-width no-break space (U+FEFF)",
}

# Bidi override and tag characters (medium severity)
_BIDI_RANGE = range(0x202A, 0x202F)   # U+202A–U+202E
_BIDI_ISOLATE_RANGE = range(0x2066, 0x206A)  # U+2066–U+2069
_TAG_RANGE = range(0xE0001, 0xE0080)  # U+E0001–U+E007F


def _check_invisible_unicode(text: str) -> list[Warning]:
    """Detect invisible / deceptive unicode characters."""
    warnings: list[Warning] = []
    lines = text.splitlines()

    zero_width_hits: list[str] = []
    bidi_hits: list[str] = []
    tag_hits: list[str] = []

    for lineno, line in enumerate(lines, start=1):
        for col, ch in enumerate(line, start=1):
            cp = ord(ch)
            if ch in _ZERO_WIDTH_CHARS:
                zero_width_hits.append(
                    f"line {lineno} col {col}: {_ZERO_WIDTH_CHARS[ch]}"
                )
            elif cp in _BIDI_RANGE or cp in _BIDI_ISOLATE_RANGE:
                bidi_hits.append(f"line {lineno} col {col}: U+{cp:04X}")
            elif cp in _TAG_RANGE:
                tag_hits.append(f"line {lineno} col {col}: tag char U+{cp:04X}")

    if zero_width_hits:
        warnings.append({
            "type": "invisible_unicode",
            "detail": f"{len(zero_width_hits)} zero-width char(s): {'; '.join(zero_width_hits[:5])}",
            "severity": "low",
        })
    if bidi_hits:
        warnings.append({
            "type": "invisible_unicode",
            "detail": f"{len(bidi_hits)} bidi override char(s): {'; '.join(bidi_hits[:5])}",
            "severity": "medium",
        })
    if tag_hits:
        warnings.append({
            "type": "invisible_unicode",
            "detail": f"{len(tag_hits)} tag char(s): {'; '.join(tag_hits[:5])}",
            "severity": "medium",
        })

    return warnings


# ---------------------------------------------------------------------------
# 3. Suspicious code pattern detection
# ---------------------------------------------------------------------------

_BASE64_PATTERN = re.compile(r"[A-Za-z0-9+/]{100,}={0,2}")
_HEX_ESCAPE_PATTERN = re.compile(r"(\\x[0-9a-fA-F]{2}){5,}")

# Extensions where eval/exec/shell constructs are expected
_PYTHON_EXTS = {".py", ".pyw"}
_SHELL_EXTS = {".sh", ".bash", ".zsh", ".fish"}


def _file_ext(filename: str) -> str:
    """Extract lowercased file extension including the dot."""
    dot = filename.rfind(".")
    return filename[dot:].lower() if dot != -1 else ""


def _check_suspicious_code(text: str, filename: str = "") -> list[Warning]:
    """Detect obfuscated or potentially dangerous code patterns."""
    warnings: list[Warning] = []
    ext = _file_ext(filename)

    # Long base64 strings (any file)
    for match in _BASE64_PATTERN.finditer(text):
        warnings.append({
            "type": "suspicious_code",
            "detail": f"Long base64-like string ({len(match.group(0))} chars) at position {match.start()}",
            "severity": "medium",
        })

    # eval/exec/__import__ in non-Python files
    if ext not in _PYTHON_EXTS:
        for func in ("eval(", "exec(", "__import__("):
            if func in text:
                warnings.append({
                    "type": "suspicious_code",
                    "detail": f"Dynamic execution call {func!r} found in non-Python file",
                    "severity": "medium",
                })

    # Shell injection in non-shell files
    if ext not in _SHELL_EXTS:
        if re.search(r"\$\(", text) or "`" in text:
            warnings.append({
                "type": "suspicious_code",
                "detail": "Shell command substitution ($(...) or backtick) in non-shell file",
                "severity": "medium",
            })

    # Obfuscated hex escape sequences (per line)
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _HEX_ESCAPE_PATTERN.search(line):
            warnings.append({
                "type": "suspicious_code",
                "detail": f"Excessive hex escapes (\\xNN) on line {lineno}",
                "severity": "medium",
            })

    return warnings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan_text(text: str, filename: str = "") -> dict:
    """Scan text content for security concerns.

    Args:
        text:     The raw text content to scan.
        filename: Optional filename used to determine file type for
                  context-aware checks (e.g. eval() in .py is normal).

    Returns:
        {
            "safe": bool,
            "warnings": [{"type": str, "detail": str, "severity": "low"|"medium"|"high"}]
        }
        safe is True only when warnings list is empty.
    """
    warnings: list[Warning] = []
    warnings.extend(_check_prompt_injection(text))
    warnings.extend(_check_invisible_unicode(text))
    warnings.extend(_check_suspicious_code(text, filename))
    return {"safe": len(warnings) == 0, "warnings": warnings}
