"""Start Kalends automatically when you sign in. No administrator rights needed.

* Windows: a shortcut in your Startup folder that runs ``kalends-service`` (no console window).
* macOS:   a launchd agent in ~/Library/LaunchAgents (KeepAlive restarts it).
* Linux:   a systemd user service (Restart=always).

``kalends-service`` itself restarts the server if it ever stops unexpectedly, so the
Windows shortcut doesn't need a separate supervisor.

Kalends used to be called Glimpse. Installs from before the rename have a ``Glimpse.lnk``
shortcut, an ``io.github.glimpse`` agent or a ``glimpse.service`` unit: those are still
recognised, and on Windows they're swapped for the new names the next time Kalends starts.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LABEL = "io.github.kalends"
LEGACY_LABEL = "io.github.glimpse"
WIN_SHORTCUT = "Kalends.lnk"
LEGACY_WIN_SHORTCUT = "Glimpse.lnk"
UNIT = "kalends.service"
LEGACY_UNIT = "glimpse.service"


@dataclass
class AutostartStatus:
    supported: bool
    enabled: bool
    where: str = ""
    detail: str = ""


def _scripts_dir() -> Path:
    return Path(sys.executable).parent


def service_executable() -> Path:
    """The no-console launcher for the background service (falls back to python -m)."""
    d = _scripts_dir()
    for name in ("kalends-service.exe", "kalends-service", "glimpse-service.exe", "glimpse-service"):
        if (d / name).exists():
            return d / name
    return Path(sys.executable)


def _service_command(config: Path) -> tuple[str, list[str]]:
    exe = service_executable()
    if exe == Path(sys.executable):
        return str(exe), ["-m", "kalends.service", "-c", str(config)]
    return str(exe), ["-c", str(config)]


# --- Windows ------------------------------------------------------------------------------


def _win_startup_dir() -> Path:
    return (
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    )


def _win_programs_dir(name: str = "Kalends") -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / name


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def make_windows_shortcut(lnk: Path, target: str, args: str, workdir: str, description: str) -> None:
    lnk.parent.mkdir(parents=True, exist_ok=True)
    script = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps_quote(str(lnk))}); "
        f"$s.TargetPath = {_ps_quote(target)}; $s.Arguments = {_ps_quote(args)}; "
        f"$s.WorkingDirectory = {_ps_quote(workdir)}; "
        f"$s.Description = {_ps_quote(description)}; $s.WindowStyle = 7; $s.Save()"
    )
    subprocess.run(  # noqa: S603
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],  # noqa: S607
        check=True,
        capture_output=True,
        creationflags=0x08000000,
    )


def _win_args(args: list[str]) -> str:
    return subprocess.list2cmdline(args)


# --- macOS --------------------------------------------------------------------------------


def _mac_label() -> str:
    """The agent's label: the old one while an install from before the rename still uses it."""
    legacy = Path.home() / "Library" / "LaunchAgents" / f"{LEGACY_LABEL}.plist"
    return LEGACY_LABEL if legacy.exists() else LABEL


def _mac_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_mac_label()}.plist"


def _mac_plist_text(exe: str, args: list[str], workdir: Path) -> str:
    from xml.sax.saxutils import escape

    items = "\n".join(f"    <string>{escape(a)}</string>" for a in [exe, *args])
    log = escape(str(workdir / "logs" / "launchd.log"))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{_mac_label()}</string>
  <key>ProgramArguments</key>
  <array>
{items}
  </array>
  <key>WorkingDirectory</key><string>{escape(str(workdir))}</string>
  <key>EnvironmentVariables</key>
  <dict><key>KALENDS_SUPERVISED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


# --- Linux --------------------------------------------------------------------------------


def _linux_unit_name() -> str:
    legacy = Path.home() / ".config" / "systemd" / "user" / LEGACY_UNIT
    return LEGACY_UNIT if legacy.exists() else UNIT


def _linux_unit() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / _linux_unit_name()


def _linux_unit_text(exe: str, args: list[str], workdir: Path) -> str:
    cmd = " ".join(_sh_quote(a) for a in [exe, *args])
    return f"""[Unit]
Description=Kalends calendar sharing
After=network-online.target

[Service]
WorkingDirectory={workdir}
Environment=KALENDS_SUPERVISED=1
ExecStart={cmd}
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""


def _sh_quote(s: str) -> str:
    return s if s and all(c.isalnum() or c in "/._-=:" for c in s) else "'" + s.replace("'", "'\\''") + "'"


# --- public API ---------------------------------------------------------------------------


def status() -> AutostartStatus:
    if sys.platform == "win32":
        lnk = _win_startup_dir() / WIN_SHORTCUT
        old = _win_startup_dir() / LEGACY_WIN_SHORTCUT
        where = old if old.exists() and not lnk.exists() else lnk
        return AutostartStatus(True, where.exists(), str(where), "Starts when you sign in to Windows")
    if sys.platform == "darwin":
        p = _mac_plist()
        return AutostartStatus(True, p.exists(), str(p), "Starts when you log in to this Mac")
    if sys.platform.startswith("linux"):
        p = _linux_unit()
        return AutostartStatus(True, p.exists(), str(p), "Runs as a systemd user service")
    return AutostartStatus(False, False, "", "Not supported on this system")


def enable(config: Path) -> AutostartStatus:
    config = config.resolve()
    workdir = config.parent
    exe, args = _service_command(config)
    if sys.platform == "win32":
        make_windows_shortcut(
            _win_startup_dir() / WIN_SHORTCUT, exe, _win_args(args), str(workdir), "Kalends calendar sharing"
        )
        (_win_startup_dir() / LEGACY_WIN_SHORTCUT).unlink(missing_ok=True)
    elif sys.platform == "darwin":
        (workdir / "logs").mkdir(exist_ok=True)
        p = _mac_plist()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_mac_plist_text(exe, args, workdir), encoding="utf-8")
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(p)], capture_output=True)  # noqa: S603,S607
        subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(p)], capture_output=True)  # noqa: S603,S607
    elif sys.platform.startswith("linux"):
        p = _linux_unit()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_linux_unit_text(exe, args, workdir), encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)  # noqa: S603,S607
        unit = _linux_unit_name()
        subprocess.run(["systemctl", "--user", "enable", "--now", unit], capture_output=True)  # noqa: S603,S607
    return status()


def disable() -> AutostartStatus:
    if sys.platform == "win32":
        (_win_startup_dir() / WIN_SHORTCUT).unlink(missing_ok=True)
        (_win_startup_dir() / LEGACY_WIN_SHORTCUT).unlink(missing_ok=True)
    elif sys.platform == "darwin":
        p = _mac_plist()
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(p)], capture_output=True)  # noqa: S603,S607
        p.unlink(missing_ok=True)
    elif sys.platform.startswith("linux"):
        subprocess.run(["systemctl", "--user", "disable", _linux_unit_name()], capture_output=True)  # noqa: S603,S607
        _linux_unit().unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)  # noqa: S603,S607
    return status()


def add_start_menu_entry(config: Path) -> Path | None:
    """Windows: Start menu → Kalends → "Kalends settings" (opens the panel, starting Kalends if needed)."""
    if sys.platform != "win32":
        return None
    d = _scripts_dir()
    exe = next((d / n for n in ("kalends-open.exe", "glimpse-open.exe") if (d / n).exists()), None)
    target, args = (
        (str(exe), ["-c", str(config.resolve())])
        if exe
        else (sys.executable, ["-m", "kalends.service", "--open", "-c", str(config.resolve())])
    )
    lnk = _win_programs_dir() / "Kalends settings.lnk"
    make_windows_shortcut(lnk, target, _win_args(args), str(config.resolve().parent), "Open Kalends settings")
    return lnk


def migrate_legacy(config: Path) -> list[str]:
    """Windows: swap shortcuts from before the rename (Glimpse) for Kalends ones. Safe to repeat.

    Only shortcuts are touched; macOS and Linux keep their existing agent or unit, which
    still works (renaming those would stop the running service).
    """
    done: list[str] = []
    if sys.platform != "win32":
        return done
    if (_win_startup_dir() / LEGACY_WIN_SHORTCUT).exists():
        enable(config)
        done.append("startup shortcut")
    old_menu = _win_programs_dir("Glimpse")
    if old_menu.exists():
        add_start_menu_entry(config)
        for f in old_menu.glob("*.lnk"):
            f.unlink(missing_ok=True)
        try:
            old_menu.rmdir()
        except OSError:
            pass
        done.append("Start menu entry")
    return done
