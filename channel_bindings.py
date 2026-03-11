"""Persistent channel -> agent-family instance bindings."""

from __future__ import annotations

import json
import threading
from pathlib import Path


class ChannelBindings:
    """Track which concrete agent instance is assigned to each channel/family."""

    def __init__(self, path: str):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._bindings: dict[str, dict[str, str]] = {}
        self._load()

    def _load(self):
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text("utf-8"))
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        cleaned: dict[str, dict[str, str]] = {}
        for channel, mapping in raw.items():
            if not isinstance(channel, str) or not isinstance(mapping, dict):
                continue
            entries = {}
            for family, instance in mapping.items():
                if isinstance(family, str) and isinstance(instance, str) and family and instance:
                    entries[family] = instance
            if entries:
                cleaned[channel] = entries
        self._bindings = cleaned

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._bindings, indent=2, sort_keys=True), "utf-8")
        tmp.replace(self._path)

    def get(self, channel: str, family: str) -> str | None:
        with self._lock:
            return self._bindings.get(channel, {}).get(family)

    def set(self, channel: str, family: str, instance: str):
        with self._lock:
            self._bindings.setdefault(channel, {})[family] = instance
            self._save()

    def resolve(self, channel: str, family: str, registry) -> str | None:
        with self._lock:
            instance = self._bindings.get(channel, {}).get(family)
        if not instance:
            return None
        if not registry:
            return instance
        canonical = registry.resolve_name(instance)
        if registry.is_registered(canonical):
            if canonical != instance:
                self.set(channel, family, canonical)
            return canonical
        return None

    def rename_channel(self, old_name: str, new_name: str):
        with self._lock:
            if old_name not in self._bindings:
                return
            self._bindings[new_name] = self._bindings.pop(old_name)
            self._save()

    def delete_channel(self, name: str):
        with self._lock:
            if name not in self._bindings:
                return
            del self._bindings[name]
            self._save()
