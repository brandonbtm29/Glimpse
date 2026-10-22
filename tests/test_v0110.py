"""0.11.0: Glimpse is now Kalends, and installs from before the rename keep working."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from kalends import __version__, autostart
from kalends.config import DEFAULT_UPDATE_REPO, UpdatesCfg
from kalends.store import ConfigStore


def test_old_module_and_commands_still_work():
    import glimpse
    import glimpse.cli
    import glimpse.service
    import kalends.cli
    import kalends.service

    assert glimpse.__version__ == __version__
    assert glimpse.cli.main is kalends.cli.main
    assert (
        glimpse.service.main is kalends.service.main
        and glimpse.service.main_open is kalends.service.main_open
    )
    for mod in ("glimpse.cli", "glimpse.service", "kalends.cli"):
        r = subprocess.run([sys.executable, "-m", mod, "--help"], capture_output=True, text=True, timeout=60)  # noqa: S603
        assert r.returncode == 0, (mod, r.stderr)


def test_names_that_must_never_change():
    """Changing any of these would sign everyone out, break forms, or duplicate events in apps."""
    from kalends.render import ics
    from kalends.web import admin, app, auth

    assert admin.COOKIE == "glimpse_admin" and app.TZ_COOKIE == "glimpse_tz"
    src = Path(auth.__file__).read_text() + Path(app.__file__).read_text() + Path(admin.__file__).read_text()
    for salt in ("glimpse-login", "glimpse-request-form", "glimpse-admin", "glimpse-video-sign-in"):
        assert f'"{salt}"' in src, salt
    assert '"glimpse_" + hashlib' in Path(auth.__file__).read_text()
    assert '@glimpse"' in Path(ics.__file__).read_text()


def test_existing_secret_names_are_reused(config_path):
    store = ConfigStore(config_path)

    def legacy(d):
        n = d.section("notify").setdefault("ntfy", {})
        d.set_secret(n, "topic", "old-topic", "GLIMPSE_NTFY_TOPIC")

    store.update(legacy)
    store.update(
        lambda d: d.set_secret(d.section("notify")["ntfy"], "topic", "new-topic", "KALENDS_NTFY_TOPIC")
    )
    env = store.env_path.read_text()
    assert "GLIMPSE_NTFY_TOPIC='new-topic'" in env and "KALENDS_NTFY_TOPIC" not in env
    assert store.current().notify.ntfy.topic == "new-topic"
    # brand-new secrets get the new prefix
    store.update(lambda d: d.add_calendar("Gym", "https://example.com/gym.ics"))
    assert "KALENDS_CAL_GYM_URL=" in store.env_path.read_text()


def test_update_checks_follow_the_renamed_repository():
    assert DEFAULT_UPDATE_REPO == "brandonbtm29/Kalends"
    assert UpdatesCfg(repo="brandonbtm29/Glimpse").repo == "brandonbtm29/Kalends"
    assert UpdatesCfg(repo="someone/fork").repo == "someone/fork"


@pytest.fixture
def fake_windows(tmp_path, monkeypatch):
    appdata = tmp_path / "AppData"
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    for exe in ("kalends-service.exe", "kalends-open.exe", "glimpse-service.exe"):
        (scripts / exe).write_text("")
    made = []

    def shortcut(lnk, target, args, workdir, description):
        lnk.parent.mkdir(parents=True, exist_ok=True)
        lnk.write_text(f"{target} {args}")
        made.append(lnk.name)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(autostart, "_scripts_dir", lambda: scripts)
    monkeypatch.setattr(autostart, "make_windows_shortcut", shortcut)
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs", made


def test_windows_shortcuts_move_to_the_new_name(fake_windows, tmp_path):
    programs, made = fake_windows
    startup, old_menu = programs / "Startup", programs / "Glimpse"
    startup.mkdir(parents=True)
    old_menu.mkdir(parents=True)
    (startup / "Glimpse.lnk").write_text("old")
    (old_menu / "Glimpse settings.lnk").write_text("old")
    assert autostart.status().enabled  # the old shortcut still counts as on
    cfg = tmp_path / "config.yaml"
    cfg.write_text("x")
    assert autostart.migrate_legacy(cfg) == ["startup shortcut", "Start menu entry"]
    assert (
        not (startup / "Glimpse.lnk").exists()
        and "kalends-service.exe" in (startup / "Kalends.lnk").read_text()
    )
    assert (
        not old_menu.exists()
        and "kalends-open.exe" in (programs / "Kalends" / "Kalends settings.lnk").read_text()
    )
    assert autostart.status().enabled and autostart.status().where.endswith("Kalends.lnk")
    assert autostart.migrate_legacy(cfg) == []  # nothing left to do


def test_windows_without_old_shortcuts_is_left_alone(fake_windows, tmp_path):
    _, made = fake_windows
    assert autostart.migrate_legacy(tmp_path / "config.yaml") == [] and made == []


def test_linux_and_mac_keep_their_existing_service(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert autostart._linux_unit().name == "kalends.service"
    old = tmp_path / ".config" / "systemd" / "user" / "glimpse.service"
    old.parent.mkdir(parents=True)
    old.write_text("[Unit]")
    assert autostart._linux_unit() == old
    assert autostart._mac_label() == "io.github.kalends"
    plist = tmp_path / "Library" / "LaunchAgents" / "io.github.glimpse.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("x")
    assert autostart._mac_label() == "io.github.glimpse"
    assert "<string>io.github.glimpse</string>" in autostart._mac_plist_text("/x", [], tmp_path)
