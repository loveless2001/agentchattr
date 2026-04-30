"""Ephemeral local file download links extracted from chat messages."""

from __future__ import annotations

import fnmatch
import math
import mimetypes
import re
import secrets
import threading
import time
from pathlib import Path


TEN_MB = 10 * 1024 * 1024

DEFAULT_EXCLUDE_GLOBS = [
    "**/.git/**",
    "**/.hg/**",
    "**/.svn/**",
    ".env*",
    "**/.env*",
    "*id_rsa*",
    "**/*id_rsa*",
    "*id_dsa*",
    "**/*id_dsa*",
    "*id_ed25519*",
    "**/*id_ed25519*",
    "*private*key*",
    "**/*private*key*",
    "*credential*",
    "**/*credential*",
    "*secret*",
    "**/*secret*",
]

_EXPLICIT_PATH_RE = re.compile(
    r"""(?:[A-Za-z]:[\\/]|~[\\/]|/|\.\.?[\\/])[^<>"'`\s\r\n]+"""
)
_QUOTED_TOKEN_RE = re.compile(
    r"""(?P<quote>["'`])(?P<path>[^"'`\r\n]+)(?P=quote)"""
)
_BARE_PATH_RE = re.compile(
    r"""(?<![\w:/])(?P<path>(?:[A-Za-z]:[\\/]|~[\\/]|/|\.\.?[\\/])[^<>"'`\s]+|[A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+)+)(?![\w])"""
)


class DownloadLinkService:
    """Mint short-lived bearer URLs for allowlisted local files."""

    def __init__(self, cfg: dict, root: Path):
        dl_cfg = cfg.get("downloads", {}) if isinstance(cfg, dict) else {}
        self.enabled = bool(dl_cfg.get("enabled", True))
        self.base_ttl_seconds = int(dl_cfg.get("base_ttl_seconds", 300))
        self.extra_ttl_per_10mb_seconds = int(
            dl_cfg.get("extra_ttl_per_10mb_seconds", 60)
        )
        self.max_file_mb = int(dl_cfg.get("max_file_mb", 512))
        self.max_links_per_message = int(dl_cfg.get("max_links_per_message", 8))
        self.allowed_roots = self._resolve_roots(
            dl_cfg.get("allowed_roots", [".."]),
            root,
        )
        self.exclude_globs = [
            str(pat).lower()
            for pat in dl_cfg.get("exclude_globs", DEFAULT_EXCLUDE_GLOBS)
        ]
        self._root = root.resolve()
        self._tokens: dict[str, dict] = {}
        self._lock = threading.Lock()

    def decorate_message(self, msg: dict) -> dict:
        """Return a copy of msg with metadata.downloads attached when found."""
        if not self.enabled or not msg or msg.get("type", "chat") != "chat":
            return msg

        text = str(msg.get("text") or "")
        if not text:
            return msg

        links = self.links_for_text(text)
        if not links:
            return msg

        decorated = dict(msg)
        metadata = dict(decorated.get("metadata") or {})
        metadata["downloads"] = links
        decorated["metadata"] = metadata
        return decorated

    def links_for_text(self, text: str) -> list[dict]:
        if self.max_links_per_message <= 0:
            return []
        self._prune_expired()
        links = []
        seen: set[Path] = set()
        for raw_path in self._extract_candidates(text):
            resolved = self._resolve_candidate(raw_path)
            if not resolved or resolved in seen:
                continue
            seen.add(resolved)
            link = self._mint_link(resolved)
            if link:
                links.append(link)
                if len(links) >= self.max_links_per_message:
                    break
        return links

    def resolve_token(self, token: str) -> dict | None:
        now = time.time()
        with self._lock:
            item = self._tokens.get(token)
            if not item:
                return None
            if item["expires_at"] <= now:
                self._tokens.pop(token, None)
                return None
            path = item["path"]
            try:
                if not path.is_file():
                    self._tokens.pop(token, None)
                    return None
            except OSError:
                self._tokens.pop(token, None)
                return None
            return dict(item)

    def _resolve_roots(self, raw_roots, root: Path) -> list[Path]:
        if isinstance(raw_roots, str):
            raw_roots = [raw_roots]
        roots = []
        for raw in raw_roots or ["."]:
            try:
                path = Path(str(raw)).expanduser()
                if not path.is_absolute():
                    path = root / path
                roots.append(path.resolve())
            except OSError:
                continue
        return roots

    def _extract_candidates(self, text: str) -> list[str]:
        candidates = []
        spans: list[tuple[int, int]] = []
        for match in _QUOTED_TOKEN_RE.finditer(text):
            token = match.group("path").strip()
            if self._looks_like_path(token):
                candidates.append(token)
                spans.append(match.span())
        for match in _BARE_PATH_RE.finditer(text):
            if any(start <= match.start() < end for start, end in spans):
                continue
            token = match.group("path").strip()
            if self._looks_like_path(token):
                candidates.append(token)
        return candidates

    def _looks_like_path(self, token: str) -> bool:
        if not token or "://" in token or token.startswith("\\"):
            return False
        token = token.rstrip(".,;:)]}")
        if _EXPLICIT_PATH_RE.fullmatch(token):
            return True
        if "/" not in token and "\\" not in token:
            return False
        parts = re.split(r"[\\/]+", token)
        if any(part in ("", ".", "..") for part in parts):
            return False
        suffix = Path(parts[-1]).suffix
        return 1 < len(suffix) <= 12 and suffix[1:].isalnum()

    def _resolve_candidate(self, raw_path: str) -> Path | None:
        normalized = raw_path.strip().strip("\u200b")
        if not normalized:
            return None

        attempts = [normalized]
        stripped = normalized.rstrip(".,;:)]}")
        if stripped != normalized:
            attempts.append(stripped)

        line_ref = re.sub(r"(?::\d+|#L\d+(?:-L\d+)?)$", "", stripped)
        if line_ref and line_ref not in attempts:
            attempts.append(line_ref)

        for attempt in attempts:
            path = Path(attempt).expanduser()
            bases = [None] if path.is_absolute() else [self._root, *self.allowed_roots]
            for base in bases:
                candidate = path if base is None else base / path
                try:
                    resolved = candidate.resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                if self._is_allowed_file(resolved):
                    return resolved
            if not path.is_absolute() and not _EXPLICIT_PATH_RE.fullmatch(attempt):
                resolved = self._resolve_project_relative(path)
                if resolved:
                    return resolved
        return None

    def _resolve_project_relative(self, path: Path) -> Path | None:
        """Resolve paths like plans/report.zip under one child of allowed roots."""
        for root in self.allowed_roots:
            try:
                children = list(root.iterdir())
            except OSError:
                continue
            for child in children:
                if not child.is_dir():
                    continue
                try:
                    resolved = (child / path).resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                if self._is_allowed_file(resolved):
                    return resolved
        return None

    def debug_text(self, text: str) -> dict:
        candidates = []
        for raw_path in self._extract_candidates(text):
            resolved = self._resolve_candidate(raw_path)
            candidates.append({
                "candidate": raw_path,
                "resolved": str(resolved) if resolved else "",
                "eligible": bool(resolved),
            })
        links = self.links_for_text(text)
        return {
            "enabled": self.enabled,
            "allowed_roots": [str(root) for root in self.allowed_roots],
            "candidates": candidates,
            "downloads": links,
        }

    def _is_allowed_file(self, path: Path) -> bool:
        try:
            if not path.is_file():
                return False
            size = path.stat().st_size
        except OSError:
            return False

        max_bytes = max(0, self.max_file_mb) * 1024 * 1024
        if max_bytes and size > max_bytes:
            return False

        if not any(path.is_relative_to(root) for root in self.allowed_roots):
            return False

        lowered = path.as_posix().lower()
        rel_lowered = lowered
        for root in self.allowed_roots:
            if path.is_relative_to(root):
                rel_lowered = path.relative_to(root).as_posix().lower()
                break
        return not any(
            fnmatch.fnmatchcase(lowered, pat) or fnmatch.fnmatchcase(rel_lowered, pat)
            for pat in self.exclude_globs
        )

    def _mint_link(self, path: Path) -> dict | None:
        try:
            size = path.stat().st_size
        except OSError:
            return None

        ttl = self.base_ttl_seconds
        ttl += math.floor(size / TEN_MB) * self.extra_ttl_per_10mb_seconds
        ttl = max(1, ttl)
        expires_at = time.time() + ttl
        token = secrets.token_urlsafe(24)
        media_type, _ = mimetypes.guess_type(str(path))
        with self._lock:
            self._tokens[token] = {
                "path": path,
                "filename": path.name,
                "size_bytes": size,
                "content_type": media_type or "application/octet-stream",
                "expires_at": expires_at,
            }

        return {
            "name": path.name,
            "url": f"/downloads/{token}",
            "size_bytes": size,
            "content_type": media_type or "application/octet-stream",
            "expires_at": int(expires_at),
            "ttl_seconds": ttl,
        }

    def _prune_expired(self):
        now = time.time()
        with self._lock:
            expired = [
                token for token, item in self._tokens.items()
                if item["expires_at"] <= now
            ]
            for token in expired:
                self._tokens.pop(token, None)
