"""Download calendar feeds and keep the last good copy of each.

Feeds are fetched with conditional requests (ETag / Last-Modified) so an
unchanged feed costs almost nothing. If a fetch fails, the previous copy
(in memory, or on disk from an earlier run) keeps being served.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from .config import AppConfig

log = logging.getLogger("glimpse.sources")

USER_AGENT = "Glimpse/0.1 (+https://github.com/; self-hosted calendar viewer)"


@dataclass
class FeedState:
    text: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    last_attempt: datetime | None = None
    last_success: datetime | None = None
    last_error: str | None = None
    version: int = 0  # bumps whenever the text changes


@dataclass
class FeedCache:
    config: AppConfig
    states: dict[str, FeedState] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.cache_dir = Path(self.config.cache_dir)
        for cid in self.config.calendars:
            st = FeedState()
            disk = self._disk_path(cid)
            if disk.is_file():
                with disk.open(encoding="utf-8", newline="") as fh:  # keep CRLF as-is
                    st.text = fh.read()
                st.version = 1
            self.states[cid] = st

    # -- public -------------------------------------------------------------

    def refresh_all(self, client: httpx.Client | None = None) -> None:
        own = client is None
        client = client or httpx.Client(
            timeout=20.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )
        try:
            for cid in self.config.calendars:
                self.refresh(cid, client)
        finally:
            if own:
                client.close()

    def refresh(self, cid: str, client: httpx.Client) -> None:
        url = normalize_url(self.config.calendars[cid].url)
        st = self.states[cid]
        st.last_attempt = datetime.now(UTC)
        try:
            if url.startswith("file://") or "://" not in url:
                text = Path(url.removeprefix("file://")).read_text(encoding="utf-8")
                self._store(cid, text, None, None)
            else:
                headers = {}
                if st.etag:
                    headers["If-None-Match"] = st.etag
                if st.last_modified:
                    headers["If-Modified-Since"] = st.last_modified
                resp = client.get(url, headers=headers)
                if resp.status_code == 304:
                    st.last_success = datetime.now(UTC)
                    st.last_error = None
                    return
                resp.raise_for_status()
                text = resp.text
                if "BEGIN:VCALENDAR" not in text[:2000]:
                    raise ValueError("response is not an iCalendar feed")
                self._store(cid, text, resp.headers.get("ETag"), resp.headers.get("Last-Modified"))
            st.last_success = datetime.now(UTC)
            st.last_error = None
        except Exception as exc:  # keep serving the last good copy
            # Never log the URL: it is a secret.
            st.last_error = f"{type(exc).__name__}: {_redact(str(exc), url)}"
            log.warning("feed %s failed: %s", cid, st.last_error)

    def texts(self) -> dict[str, str]:
        """Calendar id -> latest iCalendar text, for feeds that have any."""
        return {cid: st.text for cid, st in self.states.items() if st.text}

    def fingerprint(self) -> tuple[int, ...]:
        return tuple(self.states[c].version for c in self.config.calendars)

    # -- internals -----------------------------------------------------------

    def _store(self, cid: str, text: str, etag: str | None, last_modified: str | None) -> None:
        st = self.states[cid]
        with self._lock:
            if text != st.text:
                st.text = text
                st.version += 1
                self._write_disk(cid, text)
            st.etag = etag
            st.last_modified = last_modified

    def _disk_path(self, cid: str) -> Path:
        # File name is a hash so the cache folder doesn't reveal calendar names.
        h = hashlib.sha256(cid.encode()).hexdigest()[:16]
        return self.cache_dir / f"{h}.ics.cache"

    def _write_disk(self, cid: str, text: str) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._disk_path(cid).with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            tmp.replace(self._disk_path(cid))
        except OSError as exc:
            log.warning("could not write cache for %s: %s", cid, exc)


def normalize_url(url: str) -> str:
    """iCloud hands out ``webcal://`` links; they are plain HTTPS underneath."""
    if url.lower().startswith("webcal://"):
        return "https://" + url[len("webcal://") :]
    return url


_ANY_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")


def _redact(message: str, url: str) -> str:
    """Strip every URL from an error message; feed URLs are secrets."""
    if url:
        message = message.replace(url, "<feed url>")
    return _ANY_URL.sub("<url>", message)
