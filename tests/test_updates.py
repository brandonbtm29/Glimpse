"""Versioning, settings-format upgrades, the GitHub release check and git-based updates."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from glimpse import __version__, migrations, update_helper, updates
from glimpse.config import CONFIG_VERSION, ConfigError, load_config
from glimpse.store import ConfigStore

GIT = shutil.which("git")
PANEL_PW = "admin-password-123"
needs_git = pytest.mark.skipif(GIT is None, reason="git not installed")


# --- version numbers ----------------------------------------------------------------------


def test_version_is_single_sourced():
    from importlib.metadata import version

    assert version("glimpse-calendar") == __version__
    assert updates.parse_version(__version__) is not None


@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("v0.3.0", (0, 3, 0)),
        ("0.10.2", (0, 10, 2)),
        ("v1.2", (1, 2, 0)),
        ("v0.4.0-rc1", None),
        ("latest", None),
    ],
)
def test_parse_version(text, parsed):
    assert updates.parse_version(text) == parsed


def test_is_newer():
    assert updates.is_newer("v0.10.0", "0.9.9")
    assert not updates.is_newer("v0.3.0", "0.3.0")
    assert not updates.is_newer("v0.2.9", "0.3.0")
    assert not updates.is_newer("nightly", "0.3.0")


def test_changelog_has_this_version():
    text = (Path(__file__).parent.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{__version__}]" in text


# --- settings format ----------------------------------------------------------------------


def test_migrate_steps_and_newer_config(monkeypatch):
    monkeypatch.setitem(migrations.STEPS, 1, lambda raw: {"renamed": raw["old"]})
    out, changed = migrations.migrate({"old": 5}, 2)
    assert changed and out == {"renamed": 5, "config_version": 2}
    assert migrations.migrate({"config_version": 2}, 2) == ({"config_version": 2}, False)
    with pytest.raises(ValueError, match="only knows up to 2"):
        migrations.migrate({"config_version": 3}, 2)


def test_config_from_newer_glimpse_is_refused(config_path):
    text = config_path.read_text().replace("config_version: 2\n", "")
    config_path.write_text(f"config_version: {CONFIG_VERSION + 1}\n" + text)
    with pytest.raises(ConfigError, match="settings format"):
        load_config(config_path)


def test_upgrade_file_is_noop_when_current(config_path):
    before = config_path.read_text()
    assert ConfigStore(config_path).upgrade_file() is None
    assert config_path.read_text() == before


def test_upgrade_from_v1_adds_suggested_rules_and_backs_up(config_path):
    config_path.write_text(config_path.read_text().replace("config_version: 2\n", ""))
    store = ConfigStore(config_path)
    assert store.upgrade_file() == 1
    text = config_path.read_text()
    assert "config_version: 2" in text and "name: Health" in text
    assert (config_path.parent / "config.yaml.v1.bak").is_file()
    cfg = store.current()
    assert [r.name for r in cfg.rules] == ["Health", "Job search", "Money and legal"]
    assert all(r.enabled for r in cfg.rules)
    assert store.upgrade_file() is None  # only once


def test_upgrade_keeps_existing_rules(config_path):
    text = config_path.read_text().replace("config_version: 2\n", "")
    config_path.write_text(text + "rules:\n  - name: Mine\n    match: [secret]\n")
    store = ConfigStore(config_path)
    store.upgrade_file()
    assert [r.name for r in store.current().rules] == ["Mine"]


def test_new_installs_start_with_suggested_rules(tmp_path):
    from glimpse.store import init_files

    init_files(tmp_path / "config.yaml")
    cfg = load_config(tmp_path / "config.yaml")
    assert cfg.config_version == CONFIG_VERSION and len(cfg.rules) == 3


# --- release check ------------------------------------------------------------------------


def rel(v="9.9.0"):
    return updates.Release(
        version=v, tag=f"v{v}", name=f"v{v}", notes="- New things", url="u", published="2026-11-01"
    )


def test_checker_caches_for_a_day_and_persists(tmp_path):
    calls = []

    def fetch(repo):
        calls.append(repo)
        return rel()

    state = tmp_path / "check.json"
    c = updates.UpdateChecker(fetch=fetch, state_path=state)
    assert c.due()
    r = c.check("me/Glimpse")
    assert r.update_available and calls == ["me/Glimpse"]
    c.check("me/Glimpse")
    assert len(calls) == 1  # cached
    c.check("me/Glimpse", force=True)
    assert len(calls) == 2
    again = updates.UpdateChecker(fetch=fetch, state_path=state)
    assert again.last.release.version == "9.9.0" and not again.due()


def test_checker_keeps_last_release_on_error(tmp_path):
    seq = [rel(), RuntimeError("offline")]

    def fetch(repo):
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    c = updates.UpdateChecker(fetch=fetch)
    c.check("x/y")
    r = c.check("x/y", force=True)
    assert r.error == "Couldn't reach GitHub to check." and r.release.version == "9.9.0"


def test_latest_release_parses_github(monkeypatch):
    import httpx

    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "tag_name": "v0.4.0",
                "name": "Glimpse 0.4.0",
                "body": "notes",
                "html_url": "h",
                "published_at": "2026-11-02T10:00:00Z",
            }

    seen = {}
    monkeypatch.setattr(httpx, "get", lambda url, **kw: seen.setdefault("url", url) and R())
    r = updates.latest_release("brandonbtm29/Glimpse")
    assert seen["url"] == "https://api.github.com/repos/brandonbtm29/Glimpse/releases/latest"
    assert (r.version, r.tag, r.published) == ("0.4.0", "v0.4.0", "2026-11-02")


# --- git installs ---------------------------------------------------------------------------


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """An 'origin' with releases v0.1.0 and v0.2.0, and a clone of it sitting at v0.1.0."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.email", "t@example.com")
    git(origin, "config", "user.name", "t")
    (origin / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = ["a"]\n')
    (origin / "app.txt").write_text("one\n")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "one")
    git(origin, "tag", "v0.1.0")
    (origin / "app.txt").write_text("two\n")
    git(origin, "commit", "-qam", "two")
    git(origin, "tag", "v0.2.0")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    git(clone, "reset", "-q", "--hard", "v0.1.0")
    return origin, clone


@needs_git
def test_readiness(repos, tmp_path):
    origin, clone = repos
    assert not updates.update_readiness(tmp_path).ok  # not a git checkout
    assert updates.update_readiness(clone).ok
    assert updates.update_readiness(clone, "v0.2.0").ok
    assert "isn't available" in updates.update_readiness(clone, "v9.9.9").reason
    (clone / "app.txt").write_text("edited by hand\n")
    assert "changed by hand" in updates.update_readiness(clone, "v0.2.0").reason
    git(clone, "checkout", "-q", "app.txt")
    (clone / "local.txt").write_text("x")  # untracked files (config, .env) are fine
    assert updates.update_readiness(clone, "v0.2.0").ok


def run_helper(clone, tmp_path, validate_ok=True, extra=()):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("x: 1\n")
    result = tmp_path / "result.json"
    fake_python = tmp_path / "fakepy.py"
    # Stands in for "python -m glimpse.cli validate" (and pip): exit 0 or 1.
    fake_python.write_text(f"import sys; sys.exit({0 if validate_ok else 1})\n")
    wrapper = tmp_path / ("fakepy.cmd" if sys.platform == "win32" else "fakepy.sh")
    if sys.platform == "win32":
        wrapper.write_text(f'@"{sys.executable}" "{fake_python}" %*\n')
    else:
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake_python}" "$@"\n')
        wrapper.chmod(0o755)
    args = [
        "--root",
        str(clone),
        "--config",
        str(cfg),
        "--tag",
        "v0.2.0",
        "--python",
        str(wrapper),
        "--result",
        str(result),
        "--log",
        str(tmp_path / "update.log"),
        *extra,
    ]
    rc = update_helper.update(update_helper.argparse.Namespace(**vars(_parse(args))))
    return rc, json.loads(result.read_text())


def _parse(args):
    import argparse

    p = argparse.ArgumentParser()
    for name in ("root", "config", "tag", "python", "start", "health", "log", "result"):
        p.add_argument(f"--{name}", default="")
    p.add_argument("--pid", type=int, default=0)
    p.add_argument("--wait", type=float, default=2)
    p.add_argument("--no-restart", action="store_true")
    return p.parse_args(args)


@needs_git
def test_helper_updates_to_the_tag(repos, tmp_path):
    origin, clone = repos
    git(clone, "fetch", "-q", "--tags")
    rc, res = run_helper(clone, tmp_path, extra=["--no-restart"])
    assert rc == 0 and res["ok"] and (clone / "app.txt").read_text() == "two\n"


@needs_git
def test_helper_rolls_back_when_settings_dont_load(repos, tmp_path):
    origin, clone = repos
    git(clone, "fetch", "-q", "--tags")
    before = git(clone, "rev-parse", "HEAD")
    rc, res = run_helper(clone, tmp_path, validate_ok=False, extra=["--no-restart"])
    assert rc == 1 and not res["ok"] and "settings" in res["message"]
    assert git(clone, "rev-parse", "HEAD") == before and (clone / "app.txt").read_text() == "one\n"


@needs_git
def test_helper_rolls_back_when_new_version_doesnt_start(repos, tmp_path):
    origin, clone = repos
    git(clone, "fetch", "-q", "--tags")
    before = git(clone, "rev-parse", "HEAD")
    start = json.dumps([sys.executable, "-c", "pass"])  # "starts" but never answers
    rc, res = run_helper(clone, tmp_path, extra=["--start", start, "--health", "http://127.0.0.1:9/healthz"])
    assert rc == 1 and "didn't start" in res["message"]
    assert git(clone, "rev-parse", "HEAD") == before


def test_backup_settings(config_path):
    dest = updates.backup_settings(config_path, config_path.with_name(".env"))
    assert (dest / "config.yaml").is_file() and (dest / ".env").is_file()


# --- panel ------------------------------------------------------------------------------------


@pytest.fixture
def panel(config_path, monkeypatch):
    from argon2 import PasswordHasher
    from fastapi.testclient import TestClient

    from glimpse.web.admin import create_admin_app
    from glimpse.web.runtime import Runtime

    store = ConfigStore(config_path)
    store.update(
        lambda d: d.set_secret(
            d.section("admin"),
            "password_hash",
            PasswordHasher().hash(PANEL_PW),
            "GLIMPSE_ADMIN_PASSWORD_HASH",
        )
    )
    store.update(lambda d: d.section("admin").__setitem__("secure_cookies", False))
    rt = Runtime(store.current(), store)
    rt.updates = updates.UpdateChecker(fetch=lambda repo: rel("9.0.0"))
    client = TestClient(create_admin_app(rt), follow_redirects=False)
    client.post("/login", data={"password": PANEL_PW})
    import re

    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/").text).group(1)
    return rt, client, csrf


def test_panel_shows_version_and_available_update(panel):
    rt, c, csrf = panel
    assert f"Glimpse {__version__}" in c.get("/").text
    c.post("/setup/updates/check", data={"csrf": csrf})
    assert rt.updates.last.update_available
    html = c.get("/").text
    assert "Glimpse 9.0.0 is available" in html
    setup = c.get("/setup").text
    assert "Version 9.0.0 available" in setup and "New things" in setup


def test_panel_update_refuses_when_not_ready(panel, monkeypatch):
    rt, c, csrf = panel
    c.post("/setup/updates/check", data={"csrf": csrf})
    monkeypatch.setattr(
        updates,
        "update_readiness",
        lambda root, tag=None, fetch=True: updates.Readiness(False, "Nope, not git."),
    )
    r = c.post("/setup/updates/apply", data={"csrf": csrf})
    assert r.status_code == 400 and "Nope, not git." in r.text


def test_panel_update_starts_helper(panel, monkeypatch):
    rt, c, csrf = panel
    c.post("/setup/updates/check", data={"csrf": csrf})
    monkeypatch.setattr(
        updates, "update_readiness", lambda root, tag=None, fetch=True: updates.Readiness(True, kind="git")
    )
    started = {}
    monkeypatch.setattr(
        updates,
        "start_update",
        lambda root, cfg, tag, cmd, health, python=None: started.update(tag=tag, cmd=cmd, health=health),
    )
    r = c.post("/setup/updates/apply", data={"csrf": csrf})
    assert r.status_code == 200 and "Updating to v9.0.0" in r.text
    assert started["tag"] == "v9.0.0" and started["health"].endswith(":8000/healthz")
    assert any((rt.store.config_path.parent / "backups").iterdir())


def test_turning_off_update_checks(panel):
    rt, c, csrf = panel
    form = {
        "csrf": csrf,
        "timezone": "America/New_York",
        "week_starts": "monday",
        "refresh_minutes": "10",
        "private_tag": "#private",
        "hide_tag": "#hide",
    }
    c.post("/settings", data=form)
    assert rt.cfg.updates.check is False
    c.post("/setup/updates/check", data={"csrf": csrf})
    assert "is available" not in c.get("/").text  # banner hidden when checks are off


def test_result_file_is_shown(panel):
    rt, c, csrf = panel
    p = updates.result_path(rt.store.config_path)
    p.parent.mkdir(exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "ok": False,
                "tag": "v9.0.0",
                "message": "The new version didn't start.",
                "rolled_back_to": "abc",
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
        )
    )
    html = c.get("/setup").text
    assert "The new version didn&#39;t start." in html and "went back to the version you had" in html


def test_release_notes_are_escaped():
    from glimpse.web.admin import release_notes

    html = str(release_notes("### Added\n- **People** links\n- <script>x</script> `code`"))
    assert "<b class='nh'>Added</b>" in html and "• <b>People</b> links" in html
    assert "<script>" not in html and "&lt;script&gt;" in html and "<code>code</code>" in html
