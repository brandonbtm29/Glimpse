"""Background mode: what auto-start and the Start-menu shortcut run.

* ``kalends-service -c config.yaml``   run Kalends with no console window, logging to
  ``logs/kalends.log`` next to the config; restarts the server if it stops unexpectedly.
* ``kalends-open -c config.yaml``      make sure Kalends is running, then open the settings
  panel in the browser (the first-run setup page if no admin password is set yet).
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from .config import ConfigError, load_config

log = logging.getLogger("kalends")
RETRY_SECONDS = 30


def _setup_logging(config_path: Path) -> None:
    logs = config_path.resolve().parent / "logs"
    logs.mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        logs / "kalends.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines would print feed URLs
    # No console under pythonw/launchd: send any stray prints to the log instead of nowhere.
    if sys.stdout is None or sys.stderr is None:
        stream = open(logs / "kalends.log", "a", encoding="utf-8")  # noqa: SIM115
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream


def run_service(config_path: Path, env_path: Path | None = None) -> int:
    from .server import is_running, run

    _setup_logging(config_path)
    supervised = "1" in (os.environ.get("KALENDS_SUPERVISED"), os.environ.get("GLIMPSE_SUPERVISED"))
    try:
        from .autostart import migrate_legacy

        moved = migrate_legacy(config_path)
        if moved:
            log.info("renamed to Kalends: updated the %s", " and ".join(moved))
    except Exception:  # never let tidying shortcuts stop Kalends from starting
        log.exception("couldn't update shortcuts from before the rename")
    while True:
        try:
            cfg = load_config(config_path, env_path)
        except ConfigError as exc:
            log.error("can't start: %s", exc)
            if not supervised:
                return 2
            time.sleep(RETRY_SECONDS)
            continue
        if is_running(cfg):
            if not supervised:
                log.info("Kalends is already running; nothing to do")
                return 0
            time.sleep(RETRY_SECONDS)  # launchd/systemd would just restart us; wait instead
            continue
        try:
            log.info("starting Kalends")
            run(config_path, env_path, log_config=None)
            log.info("Kalends stopped")
            return 0
        except KeyboardInterrupt:
            return 0
        except (Exception, SystemExit):  # uvicorn exits via SystemExit when a port is taken
            log.exception("Kalends stopped unexpectedly; restarting in %s seconds", RETRY_SECONDS)
            time.sleep(RETRY_SECONDS)


def service_command(config_path: Path) -> list[str]:
    """The command that runs Kalends in the background for this config."""
    from .autostart import service_executable

    exe = service_executable()
    if exe == Path(sys.executable):
        return [sys.executable, "-m", "kalends.service", "-c", str(config_path.resolve())]
    return [str(exe), "-c", str(config_path.resolve())]


def _spawn_background(config_path: Path) -> None:
    args = service_command(config_path)
    kwargs: dict = {
        "cwd": str(config_path.resolve().parent),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # detached, new group, no window
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)  # noqa: S603


def panel_url(config_path: Path, env_path: Path | None = None) -> str:
    """Where to send the browser: the panel, or the one-time setup page."""
    from .store import ConfigStore, ensure_setup_token

    cfg = load_config(config_path, env_path)
    base = f"http://127.0.0.1:{cfg.admin.port}"
    if cfg.admin.password_hash:
        return base + "/"
    token = ensure_setup_token(ConfigStore(config_path, env_path))
    return f"{base}/setup/start?token={token}"


def open_panel(config_path: Path, env_path: Path | None = None, wait: float = 25.0) -> str:
    from .server import is_running

    cfg = load_config(config_path, env_path)
    if not is_running(cfg):
        _spawn_background(config_path)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline and not is_running(cfg):
            time.sleep(0.5)
    url = panel_url(config_path, env_path)
    webbrowser.open(url)
    return url


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kalends-service")
    p.add_argument("-c", "--config", default="config.yaml", type=Path)
    p.add_argument("-e", "--env", default=None, type=Path)
    p.add_argument("--open", action="store_true", help="open the settings panel instead")
    args = p.parse_args(argv)
    if args.open:
        try:
            open_panel(args.config, args.env)
            return 0
        except ConfigError as exc:
            _setup_logging(args.config)
            log.error("can't open the panel: %s", exc)
            return 2
    return run_service(args.config, args.env)


def main_open() -> int:
    return main([*sys.argv[1:], "--open"])


if __name__ == "__main__":
    sys.exit(main())
