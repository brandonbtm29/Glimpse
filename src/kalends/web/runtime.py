"""State shared by the public site and the settings panel in one process."""

from __future__ import annotations

import asyncio
import logging

import httpx

from .. import conferencing
from ..config import AppConfig, ConfigError
from ..core import Kalends
from ..notify import Notifier
from ..requests_store import RequestStore
from ..sources import USER_AGENT
from ..store import ConfigStore
from ..updates import UpdateChecker
from .state import SeenStore

log = logging.getLogger("kalends.runtime")


class Runtime:
    def __init__(self, cfg: AppConfig, store: ConfigStore | None = None, kalends: Kalends | None = None):
        self.kalends = kalends or Kalends(cfg)
        self.store = store
        self.seen = SeenStore(store.config_path.parent / "state.json" if store else None)
        self.requests = RequestStore(store.config_path.parent / "requests.json" if store else None)
        self.notifier = Notifier(lambda: self.cfg)
        self.kalends.video = conferencing.VideoCalls(
            lambda: self.cfg, store.config_path.parent / conferencing.FILE if store else None
        )
        self.updates = UpdateChecker(
            state_path=store.config_path.parent / "logs" / "update-check.json" if store else None
        )
        self.reload_error: str | None = None
        self._stamp = self._file_stamp()

    def _file_stamp(self) -> tuple:
        if self.store is None:
            return ()
        out = []
        for path in (self.store.config_path, self.store.env_path):
            try:
                st = path.stat()
                out.append((st.st_mtime_ns, st.st_size))
            except OSError:
                out.append(None)
        return tuple(out)

    def reload_if_changed(self) -> None:
        """Pick up edits made outside the panel (hand edits, `kalends admin-password`)."""
        stamp = self._file_stamp()
        if stamp == self._stamp or self.store is None:
            return
        self._stamp = stamp
        try:
            cfg = self.store.current()
        except ConfigError as exc:
            self.reload_error = str(exc)
            log.warning("config changed on disk but is invalid; still using the previous one: %s", exc)
            return
        self.reload_error = None
        self.apply(cfg)
        log.info("config reloaded from disk")

    @property
    def cfg(self) -> AppConfig:
        return self.kalends.cfg

    def apply(self, cfg: AppConfig, fetch: bool = True) -> None:
        """Make a freshly saved config live, and fetch any new/changed feeds now."""
        changed = self.kalends.apply(cfg)
        self._stamp = self._file_stamp()
        if fetch and changed:
            self.refresh(changed)

    def refresh(self, calendar_ids: list[str] | None = None) -> None:
        cache = self.kalends.cache
        ids = calendar_ids if calendar_ids is not None else list(cache.config.calendars)
        with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as c:
            for cid in ids:
                if cid in cache.config.calendars:
                    cache.refresh(cid, c)
            if calendar_ids is None:
                self.kalends.school.refresh(c)

    async def refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.reload_if_changed)
                await asyncio.to_thread(self.refresh)
                await asyncio.to_thread(self.requests.prune)
                await asyncio.to_thread(self.kalends.video.keep_alive)
                if self.cfg.updates.check and self.updates.due():
                    await asyncio.to_thread(self.updates.check, self.cfg.updates.repo)
            except Exception:
                log.exception("refresh failed")
            await asyncio.sleep(self.cfg.refresh_minutes * 60)
