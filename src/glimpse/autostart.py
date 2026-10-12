"""Start Glimpse automatically when you sign in. No administrator rights needed.

* Windows: a shortcut in your Startup folder that runs ``glimpse-service`` (no console window).
* macOS:   a launchd agent in ~/Library/LaunchAgents (KeepAlive restarts it).
* Linux:   a systemd user service (Restart=always).

``glimpse-service`` itself restarts the server if it ever stops unexpectedly, so the
Windows shortcut doesn't need a separate supervisor.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LABEL = "io.github.glimpse"
WIN_SHORTCUT = "Glimpse.lnk"


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
    for name in ("glimpse-service.exe", "glimpse-service"):
        if (d / name).exists():
            return d / name
    return Path(sys.executable)


def _service_command(config: Path) -> tuple[str, list[str]]:
    exe = service_executable()
    if exe == Path(sys.executable):
        return str(exe), ["-m", "glimpse.service", "-c", str(config)]
    return str(exe), ["-c", str(config)]


# --- Windows ------------------------------------------------------------------------------


def _win_startup_dir() -> Path:
    return (
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    )


def _win_programs_dir() -> Path:
    return (
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Glimpse"
    )


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


def _mac_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _mac_plist_text(exe: str, args: list[str], workdir: Path) -> str:
    from xml.sax.saxutils import escape

    items = "\n".join(f"    <string>{escape(a)}</string>" for a in [exe, *args])
    log = escape(str(workdir / "logs" / "launchd.log"))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{items}
  </array>
  <key>WorkingDirectory</key><string>{escape(str(workdir))}</string>
  <key>EnvironmentVariables</key>
  <dict><key>GLIMPSE_SUPERVISED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


# --- Linux --------------------------------------------------------------------------------


def _linux_unit() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "glimpse.service"


def _linux_unit_text(exe: str, args: list[str], workdir: Path) -> str:
    cmd = " ".join(_sh_quote(a) for a in [exe, *args])
    return f"""[Unit]
Description=Glimpse calendar sharing
After=network-online.target

[Service]
WorkingDirectory={workdir}
Environment=GLIMPSE_SUPERVISED=1
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
        return AutostartStatus(True, lnk.exists(), str(lnk), "Starts when you sign in to Windows")
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
            _win_startup_dir() / WIN_SHORTCUT, exe, _win_args(args), str(workdir), "Glimpse calendar sharing"
        )
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
        subprocess.run(["systemctl", "--user", "enable", "--now", "glimpse.service"], capture_output=True)  # noqa: S603,S607
    return status()


def disable() -> AutostartStatus:
    if sys.platform == "win32":
        (_win_startup_dir() / WIN_SHORTCUT).unlink(missing_ok=True)
    elif sys.platform == "darwin":
        p = _mac_plist()
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(p)], capture_output=True)  # noqa: S603,S607
        p.unlink(missing_ok=True)
    elif sys.platform.startswith("linux"):
        subprocess.run(["systemctl", "--user", "disable", "glimpse.service"], capture_output=True)  # noqa: S603,S607
        _linux_unit().unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)  # noqa: S603,S607
    return status()


def add_start_menu_entry(config: Path) -> Path | None:
    """Windows: Start menu → Glimpse → "Glimpse settings" (opens the panel, starting Glimpse if needed)."""
    if sys.platform != "win32":
        return None
    d = _scripts_dir()
    exe = d / "glimpse-open.exe"
    target, args = (
        (str(exe), ["-c", str(config.resolve())])
        if exe.exists()
        else (sys.executable, ["-m", "glimpse.service", "--open", "-c", str(config.resolve())])
    )
    lnk = _win_programs_dir() / "Glimpse settings.lnk"
    make_windows_shortcut(lnk, target, _win_args(args), str(config.resolve().parent), "Open Glimpse settings")
    return lnk
