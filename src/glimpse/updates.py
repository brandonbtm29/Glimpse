"""Check GitHub for new releases, and update a git-based install in one click.

Checking only asks GitHub's public API for the latest *release* of the configured
repository (no account, nothing about you is sent beyond a normal web request).

Updating (git installs only) never follows unreleased work on ``main``: it
fast-forwards to a release tag, after backing up config.yaml and .env. The
actual switch is done by :mod:`glimpse.update_helper`, a stdlib-only script
copied out of the install and run in its own process, so it can stop this
server, start the new version, and roll back if the new version doesn't come up.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import __version__

log = logging.getLogger("glimpse.updates")
CHECK_EVERY = 24 * 3600
API = "https://api.github.com/repos/{repo}/releases/latest"
_VER = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?$")
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # .../src/glimpse/updates.py -> repo root


def parse_version(text: str) -> tuple[int, int, int] | None:
    """'v0.3.0' -> (0, 3, 0). Pre-releases ('0.4.0-rc1') and odd tags give None."""
    m = _VER.match(text.strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def is_newer(candidate: str, current: str = __version__) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and b is not None and a > b


@dataclass
class Release:
    version: str  # "0.4.0"
    tag: str  # "v0.4.0"
    name: str
    notes: str
    url: str
    published: str  # ISO date


def latest_release(repo: str, timeout: float = 10) -> Release:
    """The newest published (non-draft, non-pre-release) release. Raises on any problem."""
    import httpx

    r = httpx.get(
        API.format(repo=repo),
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"Glimpse/{__version__}"},
        timeout=timeout,
        follow_redirects=True,
    )
    if r.status_code == 404:
        raise LookupError(f"No releases found for {repo} yet.")
    r.raise_for_status()
    data = r.json()
    tag = str(data.get("tag_name") or "")
    if parse_version(tag) is None:
        raise LookupError(f"The latest release tag {tag!r} isn't a version number.")
    v = parse_version(tag)
    return Release(
        version=".".join(map(str, v)),
        tag=tag,
        name=str(data.get("name") or tag),
        notes=str(data.get("body") or "")[:20000],
        url=str(data.get("html_url") or f"https://github.com/{repo}/releases"),
        published=str(data.get("published_at") or "")[:10],
    )


@dataclass
class CheckResult:
    checked_at: float
    release: Release | None = None
    error: str | None = None

    @property
    def update_available(self) -> bool:
        return self.release is not None and is_newer(self.release.version)

    @property
    def checked_label(self) -> str:
        if not self.checked_at:
            return "never"
        return datetime.fromtimestamp(self.checked_at, UTC).astimezone().strftime("%b %d, %I:%M %p")


class UpdateChecker:
    """Remembers the last check; :meth:`check` hits GitHub at most once a day unless forced."""

    def __init__(self, fetch=latest_release, state_path: Path | None = None):
        self._fetch = fetch
        self._lock = threading.Lock()
        self.state_path = state_path
        self.last: CheckResult = self._load()

    def _load(self) -> CheckResult:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path else {}
            rel = data.get("release")
            return CheckResult(
                checked_at=float(data.get("checked_at", 0)),
                release=Release(**rel) if rel else None,
                error=data.get("error"),
            )
        except (OSError, ValueError, TypeError):
            return CheckResult(checked_at=0)

    def _save(self) -> None:
        if not self.state_path:
            return
        data = asdict(self.last)
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except OSError as exc:
            log.warning("couldn't save update check: %s", exc)

    def due(self) -> bool:
        return time.time() - self.last.checked_at >= CHECK_EVERY

    def check(self, repo: str, force: bool = False) -> CheckResult:
        with self._lock:
            if not force and not self.due():
                return self.last
            try:
                rel = self._fetch(repo)
                self.last = CheckResult(checked_at=time.time(), release=rel)
                if self.last.update_available:
                    log.info("Glimpse %s is available (running %s)", rel.version, __version__)
            except Exception as exc:  # network, rate limit, no releases yet...
                msg = str(exc) if isinstance(exc, LookupError) else "Couldn't reach GitHub to check."
                self.last = CheckResult(checked_at=time.time(), release=self.last.release, error=msg)
                log.info("update check failed: %s", exc)
            self._save()
            return self.last


# --- updating a git install ---------------------------------------------------------------


def _git(root: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # no console window
    return subprocess.run(  # noqa: S603 - fixed arguments
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        **kwargs,
    )


@dataclass
class Readiness:
    ok: bool
    reason: str = ""
    kind: str = "none"  # "git" or "none"


def update_readiness(root: Path, tag: str | None = None, fetch: bool = True) -> Readiness:
    """Can this install update itself to ``tag``? Explains why not, in plain words."""
    if not (root / ".git").exists():
        return Readiness(
            False,
            "This copy wasn't installed with git, so update by downloading the new "
            "version and running the installer again.",
        )
    if shutil.which("git") is None:
        return Readiness(False, "Git isn't installed on this computer, so Glimpse can't update itself.")
    r = _git(root, "status", "--porcelain", "--untracked-files=no")
    if r.returncode != 0:
        return Readiness(False, "This folder's git information looks broken: " + r.stderr.strip()[:200])
    if r.stdout.strip():
        return Readiness(
            False,
            "Some of Glimpse's own files were changed by hand, so updating could "
            "overwrite them. Ask for help, or reinstall.",
            "git",
        )
    if tag is None:
        return Readiness(True, kind="git")
    if fetch:
        f = _git(root, "fetch", "--tags", "--force", "origin", timeout=120)
        if f.returncode != 0:
            return Readiness(False, "Couldn't download from GitHub: " + f.stderr.strip()[:200], "git")
    if _git(root, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}").returncode != 0:
        return Readiness(False, f"Release {tag} isn't available from GitHub yet.", "git")
    if _git(root, "merge-base", "--is-ancestor", "HEAD", tag).returncode != 0:
        return Readiness(
            False,
            "This copy has changes that aren't in the new release, so it can't update automatically.",
            "git",
        )
    return Readiness(True, kind="git")


def backup_settings(config_path: Path, env_path: Path) -> Path:
    """Copy config.yaml, .env, state.json, requests.json and Glimpse calendars to backups/<timestamp>/."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = config_path.resolve().parent / "backups" / f"before-update-{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    for src in (
        config_path,
        env_path,
        config_path.with_name("state.json"),
        config_path.with_name("requests.json"),
    ):
        if src.is_file():
            shutil.copy2(src, dest / src.name)
    local = config_path.resolve().parent / "local-calendars"
    if local.is_dir():
        shutil.copytree(local, dest / "local-calendars", ignore=shutil.ignore_patterns("deleted"))
    if os.name != "nt":
        os.chmod(dest, 0o700)
    return dest


def result_path(config_path: Path) -> Path:
    return config_path.resolve().parent / "logs" / "update-result.json"


def read_result(config_path: Path) -> dict | None:
    try:
        return json.loads(result_path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def start_update(
    root: Path,
    config_path: Path,
    tag: str,
    start_cmd: list[str],
    health_url: str,
    python: str | None = None,
) -> Path:
    """Copy the helper out of the install and launch it detached. Returns the log path."""
    from . import update_helper

    logs = config_path.resolve().parent / "logs"
    logs.mkdir(exist_ok=True)
    helper = logs / "update-helper.py"
    shutil.copy2(update_helper.__file__, helper)  # run a copy: the original changes during the update
    log_file = logs / "update.log"
    py = python or sys.executable  # pythonw on Windows: no console window, the helper logs to a file
    args = [
        py,
        str(helper),
        "--root",
        str(root),
        "--config",
        str(config_path.resolve()),
        "--tag",
        tag,
        "--pid",
        str(os.getpid()),
        "--python",
        py,
        "--start",
        json.dumps(start_cmd),
        "--health",
        health_url,
        "--log",
        str(log_file),
        "--result",
        str(result_path(config_path)),
    ]
    kwargs: dict = {
        "cwd": str(root),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # detached, own group, no window
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(args, **kwargs)  # noqa: S603
    return log_file
