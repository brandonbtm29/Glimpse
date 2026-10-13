"""Switch a git install of Glimpse to a release, with automatic rollback.

Started by the settings panel (see :func:`glimpse.updates.start_update`) as a
*copy* of this file, in its own process, using only the standard library, so it
keeps working while Glimpse's own files change underneath it.

Steps:
1. Note the current commit, then fast-forward to the release tag.
2. If the release changes Glimpse's dependencies, stop Glimpse and reinstall.
3. Check the new version can read your settings (``glimpse validate``).
4. Stop the running Glimpse and start the new one.
5. Wait for it to answer. If any step fails, go back to the previous commit and
   start the old version again.

The outcome is written to ``logs/update-result.json`` for the panel to show.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
LOG: Path | None = None


def say(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    if LOG:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    else:
        print(line)


def run(cmd: list[str], cwd: Path, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    say("$ " + " ".join(cmd))
    r = subprocess.run(  # noqa: S603 - arguments come from the panel, not users
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=NO_WINDOW,
    )
    out = (r.stdout + r.stderr).strip()
    if out:
        say(out[-2000:])
    return r


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], root)


def head(root: Path) -> str:
    return git(root, "rev-parse", "HEAD").stdout.strip()


def deps_signature(root: Path, rev: str) -> str:
    """The parts of pyproject.toml that need a reinstall when they change."""
    r = subprocess.run(  # noqa: S603
        ["git", "show", f"{rev}:pyproject.toml"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=NO_WINDOW,
    )
    text = r.stdout
    keep, out = False, []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            keep = s in ("[project]", "[project.scripts]", "[project.gui-scripts]", "[build-system]")
        if keep and not s.startswith(("version", "description", "keywords", "classifiers", "#")):
            out.append(s)
    return "\n".join(out)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        r = subprocess.run(  # noqa: S603
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            creationflags=NO_WINDOW,
        )
        return str(pid) in r.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def stop(pid: int, wait: float = 20) -> None:
    if not pid_alive(pid):
        return
    say(f"stopping Glimpse (pid {pid})")
    try:
        os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
    except OSError as exc:
        say(f"couldn't stop it: {exc}")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.5)


def healthy(url: str, wait: float) -> bool:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:  # noqa: S310 - local URL
                if r.status == 200 and b'"ok"' in r.read():
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW
        )  # noqa: S603, S607
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass


def start(cmd: list[str], cwd: Path) -> int | None:
    if not cmd:
        return None
    say("starting: " + " ".join(cmd))
    kwargs: dict = {
        "cwd": str(cwd),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs).pid  # noqa: S603


def write_result(path: Path | None, **data) -> None:
    data["at"] = datetime.now().isoformat(timespec="seconds")
    say("result: " + json.dumps(data))
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def update(a: argparse.Namespace) -> int:
    root, cfg = Path(a.root), Path(a.config)
    start_cmd = json.loads(a.start) if a.start else []
    result = Path(a.result) if a.result else None
    validate = [a.python, "-m", "glimpse.cli", "-c", str(cfg), "validate"]

    old = head(root)
    old_deps = deps_signature(root, old)
    say(f"updating from {old[:10]} to {a.tag}")

    def rollback(reason: str, stopped: bool) -> int:
        say(f"rolling back: {reason}")
        git(root, "reset", "--keep", old)
        if deps_signature(root, "HEAD") != deps_signature(root, a.tag):
            if not stopped:
                stop(a.pid)
            run(
                [a.python, "-m", "pip", "install", "-e", str(root), "--quiet", "--disable-pip-version-check"],
                root,
            )
            stopped = True
        if stopped:
            start(start_cmd, cfg.parent)
            healthy(a.health, 60)
        write_result(result, ok=False, tag=a.tag, message=reason, rolled_back_to=old[:10])
        return 1

    r = git(root, "merge", "--ff-only", a.tag)
    if r.returncode != 0:
        r = git(root, "checkout", "--detach", a.tag)  # not on a branch: follow the tag itself
        if r.returncode != 0:
            write_result(result, ok=False, tag=a.tag, message="Couldn't switch to the new version.")
            return 1

    stopped = False
    if deps_signature(root, "HEAD") != old_deps:
        say("dependencies changed: reinstalling")
        stop(a.pid)  # Windows locks the program files while Glimpse runs
        stopped = True
        r = run(
            [a.python, "-m", "pip", "install", "-e", str(root), "--quiet", "--disable-pip-version-check"],
            root,
        )
        if r.returncode != 0:
            return rollback("Installing the new version's packages failed.", stopped)

    if run(validate, root, timeout=120).returncode != 0:
        return rollback("The new version couldn't read your settings.", stopped)

    if a.no_restart:
        write_result(result, ok=True, tag=a.tag, previous=old[:10], message="Updated (not restarted).")
        return 0

    stop(a.pid)
    new_pid = start(start_cmd, cfg.parent)
    if not healthy(a.health, a.wait):
        if new_pid:
            kill_tree(new_pid)  # don't leave it retrying in the background
        return rollback("The new version didn't start.", True)
    write_result(result, ok=True, tag=a.tag, previous=old[:10], message=f"Updated to {a.tag}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    global LOG
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--root", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--pid", type=int, default=0)
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--start", default="")
    p.add_argument("--health", default="http://127.0.0.1:8000/healthz")
    p.add_argument("--log", default="")
    p.add_argument("--result", default="")
    p.add_argument("--wait", type=float, default=90)
    p.add_argument("--no-restart", action="store_true", help="switch versions only (for testing)")
    a = p.parse_args(argv)
    LOG = Path(a.log) if a.log else None
    time.sleep(1.5)  # let the panel finish answering the "Update now" request
    try:
        return update(a)
    except Exception as exc:  # last resort: never leave without a result
        write_result(
            Path(a.result) if a.result else None, ok=False, tag=a.tag, message=f"Update failed: {exc}"
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
