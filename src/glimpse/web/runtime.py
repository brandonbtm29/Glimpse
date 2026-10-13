"""State shared by the public site and the settings panel in one process."""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import AppConfig, ConfigError
from ..core import Glimpse
from ..sources import USER_AGENT
from ..store import ConfigStore
from ..updates import UpdateChecker
from .state import SeenStore

log = logging.getLogger("glimpse.runtime")


class Runtime:
    def __init__(self, cfg: AppConfig, store: ConfigStore | None = None, glimpse: Glimpse | None = None):
        self.glimpse = glimpse or Glimpse(cfg)
        self.store = store
        self.seen = SeenStore(store.config_path.parent / "state.json" if store else None)
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
        """Pick up edits made outside the panel (hand edits, `glimpse admin-password`)."""
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
        return self.glimpse.cfg

    def apply(self, cfg: AppConfig, fetch: bool = True) -> None:
        """Make a freshly saved config live, and fetch any new/changed feeds now."""
        changed = self.glimpse.apply(cfg)
        self._stamp = self._file_stamp()
        if fetch and changed:
            self.refresh(changed)

    def refresh(self, calendar_ids: list[str] | None = None) -> None:
        cache = self.glimpse.cache
        ids = calendar_ids if calendar_ids is not None else list(cache.config.calendars)
        with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as c:
            for cid in ids:
                if cid in cache.config.calendars:
                    cache.refresh(cid, c)

    async def refresh_loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.reload_if_changed)
                await asyncio.to_thread(self.refresh)
                if self.cfg.updates.check and self.updates.due():
                    await asyncio.to_thread(self.updates.check, self.cfg.updates.repo)
            except Exception:
                log.exception("refresh failed")
            await asyncio.sleep(self.cfg.refresh_minutes * 60)
