"""Meeting requests people send through a shared page, kept in ``requests.json``.

Like ``state.json`` this sits beside the config and never inside it: a request is
something that happened, not a setting. Each request has a secret token that
works as the requester's private status link.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger("glimpse.requests")

STATUSES = ("pending", "accepted", "declined", "countered", "cancelled")
KEEP_DAYS = 120  # finished requests older than this are dropped
MAX_PENDING_PER_VIEWER = 5
MAX_PENDING_TOTAL = 50


@dataclass
class MeetingRequest:
    id: str
    token: str  # the requester's private status link
    created: str  # ISO, UTC
    audience: str
    person: str | None
    viewer: str  # who sent it, e.g. "a:coworkers" or "p:alex"
    name: str
    email: str
    note: str
    start: str  # ISO with offset
    end: str
    kind: str = "slot"  # "slot" (picked an open time) or "suggested"
    status: str = "pending"
    reply: str = ""  # your message back
    counter_start: str | None = None  # a time you proposed instead
    counter_end: str | None = None
    updated: str = ""
    history: list[str] = field(default_factory=list)
    link: str = ""  # video meeting link (Zoom, Teams, Meet...), shared once confirmed
    location: str = ""  # a room or address, shared once confirmed
    calendar: str | None = None  # Glimpse calendar the confirmed meeting was added to

    @property
    def start_dt(self) -> datetime:
        return datetime.fromisoformat(self.start)

    @property
    def end_dt(self) -> datetime:
        return datetime.fromisoformat(self.end)

    @property
    def counter_start_dt(self) -> datetime | None:
        return datetime.fromisoformat(self.counter_start) if self.counter_start else None

    @property
    def counter_end_dt(self) -> datetime | None:
        return datetime.fromisoformat(self.counter_end) if self.counter_end else None

    @property
    def created_dt(self) -> datetime:
        return datetime.fromisoformat(self.created)

    @property
    def minutes(self) -> int:
        return int((self.end_dt - self.start_dt).total_seconds() // 60)

    @property
    def open(self) -> bool:
        return self.status in ("pending", "countered")

    def holds(self) -> list[tuple[datetime, datetime]]:
        """Times this request keeps off the open-slots list."""
        if self.status == "accepted" or self.status == "pending":
            return [(self.start_dt, self.end_dt)]
        if self.status == "countered" and self.counter_start_dt and self.counter_end_dt:
            return [(self.counter_start_dt, self.counter_end_dt)]
        return []


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class RequestStore:
    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self._items: dict[str, MeetingRequest] = {}
        if path and path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for raw in data.get("requests", []):
                    known = {k: v for k, v in raw.items() if k in MeetingRequest.__dataclass_fields__}
                    r = MeetingRequest(**known)
                    self._items[r.id] = r
            except (OSError, ValueError, TypeError):
                log.warning("couldn't read %s; starting with no requests", path.name)

    # -- reading ---------------------------------------------------------------------------

    def all(self) -> list[MeetingRequest]:
        with self._lock:
            return sorted(self._items.values(), key=lambda r: r.created, reverse=True)

    def get(self, rid: str) -> MeetingRequest | None:
        with self._lock:
            return self._items.get(rid)

    def by_token(self, token: str) -> MeetingRequest | None:
        import hmac

        found = None
        with self._lock:
            for r in self._items.values():
                if hmac.compare_digest(r.token.encode(), token.encode()):
                    found = r
        return found

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._items.values() if r.status == "pending")

    def holds(self) -> list[tuple[datetime, datetime]]:
        with self._lock:
            return [h for r in self._items.values() for h in r.holds()]

    def open_from(self, viewer: str) -> int:
        with self._lock:
            return sum(1 for r in self._items.values() if r.viewer == viewer and r.open)

    # -- writing ---------------------------------------------------------------------------

    def add(self, **fields) -> MeetingRequest:
        with self._lock:
            pending = sum(1 for r in self._items.values() if r.open)
            if pending >= MAX_PENDING_TOTAL:
                raise OverflowError("too many open requests")
            rid = secrets.token_hex(6)
            r = MeetingRequest(
                id=rid, token=secrets.token_urlsafe(24), created=_now(), updated=_now(), **fields
            )
            r.history.append(f"{r.created} sent")
            self._items[rid] = r
        self.save()
        return r

    def change(self, rid: str, status: str, note: str = "", **fields) -> MeetingRequest | None:
        if status not in STATUSES:
            raise ValueError(status)
        with self._lock:
            r = self._items.get(rid)
            if r is None:
                return None
            r.status = status
            for k, v in fields.items():
                setattr(r, k, v)
            r.updated = _now()
            r.history.append(f"{r.updated} {status}{(' ' + note) if note else ''}")
        self.save()
        return r

    def remove(self, rid: str) -> None:
        with self._lock:
            self._items.pop(rid, None)
        self.save()

    def prune(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=KEEP_DAYS)
        with self._lock:
            old = [
                rid
                for rid, r in self._items.items()
                if not r.open and r.end_dt < cutoff or (r.open and r.end_dt < now - timedelta(days=7))
            ]
            for rid in old:
                del self._items[rid]
        if old:
            self.save()

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            payload = json.dumps({"requests": [asdict(r) for r in self._items.values()]}, indent=1)
        try:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".requests-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            log.warning("couldn't save %s: %s", self.path.name, exc)
