"""Small runtime state kept beside the config: when each link was last opened.

Stored in ``state.json`` (not config.yaml) so viewing a page never rewrites your settings.
Writes are batched: at most one every :data:`FLUSH_SECONDS`.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger("glimpse.state")
FLUSH_SECONDS = 60


class SeenStore:
    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self._seen: dict[str, str] = {}
        self._dirty = False
        self._last_flush = 0.0
        if path and path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._seen = {str(k): str(v) for k, v in (data.get("last_seen") or {}).items()}
            except (OSError, ValueError, AttributeError):
                log.warning("couldn't read %s; starting fresh", path.name)

    def touch(self, key: str, now: datetime | None = None) -> None:
        stamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
        with self._lock:
            self._seen[key] = stamp
            self._dirty = True
            due = time.monotonic() - self._last_flush >= FLUSH_SECONDS
        if due:
            self.flush()

    def get(self, key: str) -> datetime | None:
        with self._lock:
            value = self._seen.get(key)
        try:
            return datetime.fromisoformat(value) if value else None
        except ValueError:
            return None

    def forget(self, key: str) -> None:
        with self._lock:
            if self._seen.pop(key, None) is not None:
                self._dirty = True

    def flush(self) -> None:
        with self._lock:
            if not self._dirty or self.path is None:
                return
            payload = json.dumps({"last_seen": self._seen}, indent=1, sort_keys=True)
            self._dirty = False
            self._last_flush = time.monotonic()
        try:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("couldn't save %s: %s", self.path.name, exc)


def ago(when: datetime | None, now: datetime | None = None) -> str:
    """'just now', '5 min ago', '3 h ago', '2 days ago', or 'never'."""
    if when is None:
        return "never"
    secs = ((now or datetime.now(UTC)) - when).total_seconds()
    if secs < 90:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    days = int(secs // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"
