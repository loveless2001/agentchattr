"""Attachment normalization for chat uploads.

Normalizes supported uploads into safe browser/UI metadata and text that agents
can consume consistently. Images stay as images. Text-like documents are
converted to markdown text plus an on-disk `.md` artifact.
"""

from __future__ import annotations

import io
import mimetypes
import re
import uuid
import zipfile
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET

try:
    import pymupdf
except ImportError:  # pragma: no cover - optional dependency
    pymupdf = None


INLINE_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
IMAGE_EXTS = INLINE_IMAGE_EXTS | {".svg"}
TEXT_EXTS = {".md", ".markdown", ".txt"}
# Code and config file extensions — wrapped in fenced code blocks
CODE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml",
    ".toml", ".csv", ".xml", ".html", ".css", ".sh", ".bash",
    ".go", ".rs", ".java", ".rb", ".lua", ".c", ".cpp", ".h",
    ".log", ".ini", ".cfg", ".sql", ".env", ".conf", ".ipynb",
}
# Extension → markdown language hint for fenced code blocks
_EXT_TO_LANG = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".jsx": "jsx", ".tsx": "tsx", ".json": "json", ".yaml": "yaml",
    ".yml": "yaml", ".toml": "toml", ".csv": "csv", ".xml": "xml",
    ".html": "html", ".css": "css", ".sh": "bash", ".bash": "bash",
    ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby",
    ".lua": "lua", ".c": "c", ".cpp": "cpp", ".h": "c",
    ".log": "log", ".ini": "ini", ".cfg": "ini", ".sql": "sql",
    ".env": "bash", ".conf": "conf",
    ".ipynb": "json",
}
# Conservative MIME fallback for unknown extensions
_ALLOWED_TEXT_MIMES = {"text/", "application/json", "application/xml",
                       "application/x-yaml", "application/toml"}
DOC_EXTS = {".pdf", ".docx"} | TEXT_EXTS
ALLOWED_UPLOAD_EXTS = IMAGE_EXTS | DOC_EXTS | CODE_EXTS
MAX_INLINE_TEXT_CHARS = 20_000
MAX_SUMMARY_CHARS = 280


def _is_text_mime(ext: str, content: bytes) -> bool:
    """Check if an unknown extension has a text-like MIME type."""
    mime, _ = mimetypes.guess_type(f"file{ext}")
    if not mime:
        return False
    return any(mime.startswith(prefix) if prefix.endswith("/")
               else mime == prefix for prefix in _ALLOWED_TEXT_MIMES)


def _code_to_markdown(text: str, ext: str) -> str:
    """Wrap code/config text in a fenced code block with language hint."""
    lang = _EXT_TO_LANG.get(ext, "")
    return f"```{lang}\n{text}\n```"


def _run_security_scan(text: str, filename: str) -> dict | None:
    """Run lightweight security scan on text content. Returns scan result or None."""
    try:
        from upload_security_scanner import scan_text
        return scan_text(text, filename)
    except ImportError:
        return None


def process_upload(filename: str, content: bytes, upload_dir: Path) -> dict:
    upload_dir.mkdir(parents=True, exist_ok=True)

    original_name = Path(filename or "attachment").name or "attachment"
    ext = (Path(original_name).suffix or "").lower()
    sniffed_ext = sniff_extension(content)
    if sniffed_ext:
        ext = sniffed_ext

    # Accept known extensions, or unknown extensions with text-like MIME
    if ext not in ALLOWED_UPLOAD_EXTS and not _is_text_mime(ext, content):
        raise ValueError(f"unsupported file type: {ext or 'unknown'}")

    upload_id = uuid.uuid4().hex[:8]
    media_type = guess_media_type(original_name, ext)
    original_path = upload_dir / f"{upload_id}{ext}"
    original_path.write_bytes(content)

    if ext in INLINE_IMAGE_EXTS:
        return {
            "id": upload_id,
            "name": original_name,
            "kind": "image",
            "status": "ready",
            "content_type": media_type,
            "size_bytes": len(content),
            "url": f"/uploads/{original_path.name}",
            "download_url": f"/uploads/{original_path.name}?download=1",
        }

    if ext == ".svg":
        return {
            "id": upload_id,
            "name": original_name,
            "kind": "file",
            "status": "ready",
            "content_type": media_type,
            "size_bytes": len(content),
            "url": f"/uploads/{original_path.name}?download=1",
            "download_url": f"/uploads/{original_path.name}?download=1",
            "summary": "SVG uploaded. Inline rendering is disabled for safety.",
        }

    # Extract text content based on file type
    if ext in TEXT_EXTS:
        raw_text = decode_text_bytes(content).strip()
        markdown_text = raw_text
    elif ext in CODE_EXTS or _is_text_mime(ext, content):
        raw_text = decode_text_bytes(content).strip()
        markdown_text = _code_to_markdown(raw_text, ext)
    elif ext == ".docx":
        raw_text = docx_to_markdown(content).strip()
        markdown_text = raw_text
    elif ext == ".pdf":
        raw_text = pdf_to_markdown(content).strip()
        markdown_text = raw_text
    else:
        raise ValueError(f"unsupported file type: {ext}")

    if not markdown_text:
        raise ValueError(f"could not extract text from {ext}")

    # Run security scan on extracted text (advisory only)
    scan_result = _run_security_scan(raw_text, original_name)

    normalized_text = normalize_markdown(markdown_text)
    truncated = len(normalized_text) > MAX_INLINE_TEXT_CHARS
    inline_text = normalized_text[:MAX_INLINE_TEXT_CHARS]
    if truncated:
        inline_text = inline_text.rstrip() + "\n\n[truncated for chat preview]"

    markdown_path = upload_dir / f"{upload_id}.md"
    markdown_path.write_text(normalized_text, "utf-8")

    result = {
        "id": upload_id,
        "name": original_name,
        "kind": "markdown",
        "status": "ready",
        "content_type": media_type,
        "size_bytes": len(content),
        "url": f"/uploads/{markdown_path.name}?download=1",
        "download_url": f"/uploads/{original_path.name}?download=1",
        "markdown_url": f"/uploads/{markdown_path.name}?download=1",
        "markdown_text": inline_text,
        "summary": summarize_text(normalized_text),
        "truncated": truncated,
    }

    # Attach security warnings if any found (advisory, never blocking)
    if scan_result and not scan_result.get("safe", True):
        result["security_warnings"] = scan_result["warnings"]

    return result


def guess_media_type(filename: str, ext: str) -> str:
    if ext == ".md":
        return "text/markdown"
    media_type, _ = mimetypes.guess_type(filename)
    return media_type or "application/octet-stream"


def sniff_extension(content: bytes) -> str | None:
    head = content[:64]
    if content.startswith(b"%PDF-"):
        return ".pdf"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"GIF87a") or content.startswith(b"GIF89a"):
        return ".gif"
    if content.startswith(b"BM"):
        return ".bmp"
    if head.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp"
    stripped = content.lstrip()
    if stripped.startswith(b"<svg") or stripped.startswith(b"<?xml"):
        lower = stripped[:256].lower()
        if b"<svg" in lower:
            return ".svg"
    if content.startswith(b"PK"):
        return None
    return None


def decode_text_bytes(content: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "utf-16-le", "utf-16-be", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def normalize_markdown(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def summarize_text(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= MAX_SUMMARY_CHARS:
        return collapsed
    return collapsed[: MAX_SUMMARY_CHARS - 3].rstrip() + "..."


def docx_to_markdown(content: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            xml_bytes = zf.read("word/document.xml")
    except Exception as exc:  # pragma: no cover - defensive
        raise ValueError("invalid docx file") from exc

    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    root = ET.fromstring(xml_bytes)
    blocks: list[str] = []

    for child in root.findall(".//w:body/*", ns):
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = "".join(t.text or "" for t in child.findall(".//w:t", ns)).strip()
            if text:
                blocks.append(text)
        elif tag == "tbl":
            rows = []
            for tr in child.findall(".//w:tr", ns):
                cols = []
                for tc in tr.findall("./w:tc", ns):
                    cell = "".join(t.text or "" for t in tc.findall(".//w:t", ns)).strip()
                    cols.append(cell or " ")
                if cols:
                    rows.append(cols)
            if rows:
                header = "| " + " | ".join(rows[0]) + " |"
                divider = "| " + " | ".join("---" for _ in rows[0]) + " |"
                blocks.extend([header, divider])
                for row in rows[1:]:
                    padded = row + [" "] * (len(rows[0]) - len(row))
                    blocks.append("| " + " | ".join(padded[: len(rows[0])]) + " |")
                blocks.append("")

    return "\n\n".join(blocks).strip()


def pdf_to_markdown(content: bytes) -> str:
    pymupdf_text = pdf_to_markdown_pymupdf(content)
    if pymupdf_text:
        return pymupdf_text

    streams = re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", content, re.DOTALL)
    extracted_parts: list[str] = []
    for match in streams:
        raw_stream = match.group(1)
        candidates = [raw_stream]
        try:
            candidates.insert(0, zlib.decompress(raw_stream))
        except Exception:
            pass
        for candidate in candidates:
            text = extract_pdf_text_from_stream(candidate)
            if text:
                extracted_parts.append(text)
                break

    joined = "\n\n".join(part for part in extracted_parts if part.strip()).strip()
    if joined:
        return joined

    # Last-resort fallback for simple text-only PDFs.
    ascii_runs = re.findall(rb"[A-Za-z0-9][A-Za-z0-9 ,.;:?!()\/'\"_-]{8,}", content)
    fallback = "\n".join(run.decode("latin-1", errors="ignore") for run in ascii_runs[:200])
    return fallback.strip()


def pdf_to_markdown_pymupdf(content: bytes) -> str:
    if pymupdf is None:
        return ""

    pages: list[str] = []
    try:
        with pymupdf.open(stream=content) as doc:
            for page in doc:
                page_text = page.get_text("text", sort=True).strip()
                if page_text:
                    pages.append(page_text)
    except Exception:
        return ""

    return normalize_markdown("\n\n".join(pages))


def extract_pdf_text_from_stream(stream: bytes) -> str:
    text = stream.decode("latin-1", errors="ignore")
    pieces: list[str] = []

    for match in re.finditer(r"\((?:\\.|[^\\)])*\)\s*Tj", text):
        token = match.group(0).rsplit(")", 1)[0][1:]
        decoded = decode_pdf_string(token)
        if decoded.strip():
            pieces.append(decoded)

    for match in re.finditer(r"\[(.*?)\]\s*TJ", text, re.DOTALL):
        array_content = match.group(1)
        chunk_parts = []
        for part in re.finditer(r"\((?:\\.|[^\\)])*\)", array_content):
            decoded = decode_pdf_string(part.group(0)[1:-1])
            if decoded:
                chunk_parts.append(decoded)
        joined = "".join(chunk_parts).strip()
        if joined:
            pieces.append(joined)

    cleaned: list[str] = []
    for piece in pieces:
        piece = re.sub(r"\s+", " ", piece).strip()
        if piece and piece not in cleaned:
            cleaned.append(piece)
    return "\n".join(cleaned)


def decode_pdf_string(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue

        i += 1
        if i >= len(value):
            break
        esc = value[i]
        mapping = {
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "b": "\b",
            "f": "\f",
            "\\": "\\",
            "(": "(",
            ")": ")",
        }
        if esc in mapping:
            out.append(mapping[esc])
            i += 1
            continue
        if esc.isdigit():
            octal = esc
            i += 1
            for _ in range(2):
                if i < len(value) and value[i].isdigit():
                    octal += value[i]
                    i += 1
                else:
                    break
            out.append(chr(int(octal, 8)))
            continue
        out.append(esc)
        i += 1

    return "".join(out)
