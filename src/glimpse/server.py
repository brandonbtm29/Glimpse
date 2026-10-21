"""Build and run the two web servers (public pages + settings panel) in one process."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from .config import AppConfig, ConfigError, load_config
from .store import ConfigStore

log = logging.getLogger("glimpse")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def is_running(cfg: AppConfig, timeout: float = 1.5) -> bool:
    """Is a Glimpse already answering on this config's public port?"""
    try:
        r = httpx.get(f"http://127.0.0.1:{cfg.server.port}/healthz", timeout=timeout)
        return r.status_code == 200 and r.json().get("ok") is True
    except (httpx.HTTPError, ValueError):
        return False


def run(
    config_path: Path,
    env_path: Path | None = None,
    host: str | None = None,
    port: int | None = None,
    admin_port: int | None = None,
    no_admin: bool = False,
    log_config: dict | None | bool = False,
) -> None:
    """Run until stopped. ``log_config=None`` leaves logging to the caller (service mode)."""
    import uvicorn

    from .web.app import create_app
    from .web.runtime import Runtime

    store = ConfigStore(config_path, env_path)
    try:
        old = store.upgrade_file()
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if old:
        log.info("upgraded config.yaml from settings format %s (original kept as .v%s.bak)", old, old)
    cfg = load_config(config_path, env_path)
    if cfg.self_audience is None:
        cfg = store.update(
            lambda d: d.ensure_self()
        )  # your own "My calendar" view, locked until it has a password
        log.info("added your own view (My calendar); set its password in the settings panel to use it")
    rt = Runtime(cfg, store)
    host = host or cfg.server.host
    port = port or cfg.server.port
    admin_port = admin_port or cfg.admin.port
    for h in (host, cfg.admin.host):
        if h not in LOCAL_HOSTS:
            log.warning("listening on %s: anyone on that network can reach it directly", h)

    extra = {} if log_config is False else {"log_config": log_config}
    common = dict(log_level="warning", access_log=False, forwarded_allow_ips="127.0.0.1", **extra)
    servers = [
        uvicorn.Server(
            uvicorn.Config(
                create_app(
                    cfg,
                    glimpse=rt.glimpse,
                    background_refresh=False,
                    seen=rt.seen,
                    requests=rt.requests,
                    notifier=rt.notifier,
                ),
                host=host,
                port=port,
                proxy_headers=cfg.server.trust_proxy_headers,
                **common,
            )
        )
    ]
    if cfg.admin.enabled and not no_admin:
        from .web.admin import create_admin_app

        servers.append(
            uvicorn.Server(
                uvicorn.Config(
                    create_admin_app(rt), host=cfg.admin.host, port=admin_port, proxy_headers=True, **common
                )
            )
        )
        if not cfg.admin.password_hash:
            log.warning("settings panel needs first-time setup: run `glimpse open` on this computer")
    log.info("public pages on http://%s:%s", host, port)
    if len(servers) > 1:
        log.info("settings panel on http://%s:%s", cfg.admin.host, admin_port)

    async def main() -> None:
        refresher = asyncio.create_task(rt.refresh_loop())
        try:
            await asyncio.gather(*(s.serve() for s in servers))
        finally:
            refresher.cancel()
            rt.seen.flush()

    asyncio.run(main())
    if any(not s.started for s in servers):
        raise RuntimeError("a server failed to start (is the port already in use?)")
