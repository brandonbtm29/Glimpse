"""First-run wizard, Setup page, Tailscale helper, auto-start and service mode."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kalends import autostart, service, tailscale
from kalends.config import load_config
from kalends.store import ConfigStore, ensure_setup_token, init_files
from kalends.web.admin import create_admin_app
from kalends.web.runtime import Runtime

PW = "first-run-password-1"


@pytest.fixture
def fresh(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    init_files(cfg_path)  # no admin password: first-run mode
    store = ConfigStore(cfg_path)
    store.update(lambda d: d.section("admin").__setitem__("secure_cookies", False))
    rt = Runtime(store.current(), store)
    return rt, TestClient(create_admin_app(rt), follow_redirects=False, client=("127.0.0.1", 50000))


def token_of(rt) -> str:
    return rt.cfg.admin.setup_token


# --- first run ------------------------------------------------------------------------------


def test_init_without_password_creates_setup_token(fresh):
    rt, _ = fresh
    assert rt.cfg.admin.password_hash is None
    assert len(token_of(rt)) >= 24
    assert token_of(rt) not in rt.store.config_path.read_text()  # secret stays in .env


def test_first_run_requires_token(fresh):
    rt, c = fresh
    assert c.get("/setup/start").status_code == 403
    assert c.get("/setup/start?token=wrong").status_code == 403
    r = c.get(f"/setup/start?token={token_of(rt)}")
    assert r.status_code == 200 and "Welcome to Kalends" in r.text


def test_first_run_refuses_requests_through_tailscale(fresh):
    rt, c = fresh
    r = c.get(f"/setup/start?token={token_of(rt)}", headers={"Tailscale-User-Login": "x@example.com"})
    assert r.status_code == 403
    remote = TestClient(create_admin_app(rt), client=("100.90.1.2", 1234))
    assert remote.get(f"/setup/start?token={token_of(rt)}").status_code == 403


def test_first_run_sets_password_and_retires_token(fresh):
    rt, c = fresh
    tok = token_of(rt)
    r = c.post("/setup/start", data={"token": tok, "password": "short", "password2": "short"})
    assert r.status_code == 400
    r = c.post("/setup/start", data={"token": tok, "password": PW, "password2": PW})
    assert r.status_code == 303 and r.headers["location"].startswith("/setup")
    assert rt.cfg.admin.password_hash and rt.cfg.admin.setup_token is None
    assert "KALENDS_SETUP_TOKEN" not in (rt.store.config_path.parent / ".env").read_text()
    assert c.get("/setup").status_code == 200  # signed in by the wizard
    # the old token is useless now
    assert c.get(f"/setup/start?token={tok}").headers["location"] == "/login"
    assert c.post("/setup/start", data={"token": tok, "password": PW, "password2": PW}).status_code == 303
    c.cookies.clear()
    assert c.post("/login", data={"password": PW}).status_code == 303


def test_ensure_setup_token_is_stable(fresh):
    rt, _ = fresh
    assert ensure_setup_token(rt.store) == token_of(rt)


def test_panel_url_points_to_setup_until_password_set(fresh):
    rt, c = fresh
    url = service.panel_url(rt.store.config_path)
    assert url.startswith("http://127.0.0.1:8001/setup/start?token=")
    c.post("/setup/start", data={"token": token_of(rt), "password": PW, "password2": PW})
    assert service.panel_url(rt.store.config_path) == "http://127.0.0.1:8001/"


# --- setup page --------------------------------------------------------------------------------


@pytest.fixture
def signed_in(fresh):
    rt, c = fresh
    c.post("/setup/start", data={"token": token_of(rt), "password": PW, "password2": PW})
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/setup").text).group(1)
    return rt, c, csrf


def test_setup_page_without_tailscale(signed_in, monkeypatch):
    monkeypatch.setattr(tailscale, "find_cli", lambda: None)
    _, c, _ = signed_in
    html = c.get("/setup?refresh=1").text
    assert "Download Tailscale" in html and "1 of 6 done" in html


def fake_ts(monkeypatch, serve_cfg=None, approve=False, calls=None):
    calls = calls if calls is not None else []
    monkeypatch.setattr(tailscale, "find_cli", lambda: "/usr/bin/tailscale")

    def run(args, timeout=tailscale.TIMEOUT):
        calls.append(args)
        if args[:2] == ["status", "--json"]:
            return tailscale.Result(
                True,
                json.dumps(
                    {
                        "BackendState": "Running",
                        "Self": {"DNSName": "my-pc.tail1234.ts.net."},
                        "CurrentTailnet": {"MagicDNSEnabled": True},
                    }
                ),
            )
        if args[:3] == ["serve", "status", "--json"]:
            return tailscale.Result(True, json.dumps(serve_cfg or {}))
        if approve:
            return tailscale.Result(
                False,
                "Funnel is not enabled. To enable, visit:\nhttps://login.tailscale.com/f/funnel?node=abc123",
                timed_out=True,
                approve_url="https://login.tailscale.com/f/funnel?node=abc123",
            )
        return tailscale.Result(True, "ok")

    monkeypatch.setattr(tailscale, "_run", run)
    return calls


def test_turn_on_panel_and_public_links(signed_in, monkeypatch):
    rt, c, csrf = signed_in
    calls = fake_ts(monkeypatch)
    r = c.post("/setup/tailscale/panel", data={"csrf": csrf})
    assert r.status_code == 303 and "ts_panel" in r.headers["location"]
    assert ["serve", "--yes", "--bg", "--https=8443", "http://127.0.0.1:8001"] in calls
    r = c.post("/setup/tailscale/public", data={"csrf": csrf})
    assert ["funnel", "--yes", "--bg", "--https=443", "http://127.0.0.1:8000"] in calls
    assert rt.cfg.server.base_url == "https://my-pc.tail1234.ts.net"  # filled in automatically


def test_approval_link_is_shown(signed_in, monkeypatch):
    _, c, csrf = signed_in
    fake_ts(monkeypatch, approve=True)
    r = c.post("/setup/tailscale/public", data={"csrf": csrf})
    assert r.status_code == 200 and "https://login.tailscale.com/f/funnel?node=abc123" in r.text


def test_setup_actions_need_csrf(signed_in, monkeypatch):
    _, c, _ = signed_in
    calls = fake_ts(monkeypatch)
    assert c.post("/setup/tailscale/public", data={}).headers["location"] == "/login"
    assert not any(a[0] == "funnel" for a in calls)


def test_status_parsing(monkeypatch):
    serve_cfg = {
        "TCP": {"443": {"HTTPS": True}, "8443": {"HTTPS": True}},
        "Web": {
            "my-pc.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8000"}}},
            "my-pc.tail1234.ts.net:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8001"}}},
        },
        "AllowFunnel": {"my-pc.tail1234.ts.net:443": True},
    }
    fake_ts(monkeypatch, serve_cfg)
    st = tailscale.status()
    assert st.running and st.dns_name == "my-pc.tail1234.ts.net"
    assert st.panel_published and st.public_published
    assert st.panel_url == "https://my-pc.tail1234.ts.net:8443"


def test_public_not_counted_without_funnel(monkeypatch):
    fake_ts(monkeypatch, {"Web": {"h:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8000"}}}}})
    assert not tailscale.status().public_published


def test_yes_flag_fallback_for_old_clients(monkeypatch):
    seen = []

    def run(args, timeout=tailscale.TIMEOUT):
        seen.append(args)
        if "--yes" in args:
            return tailscale.Result(False, "flag provided but not defined: -yes")
        return tailscale.Result(True, "")

    monkeypatch.setattr(tailscale, "_run", run)
    assert tailscale.publish_panel().ok
    assert seen[-1] == ["serve", "--bg", "--https=8443", "http://127.0.0.1:8001"]


posix_only = pytest.mark.skipif(sys.platform == "win32", reason="fake Tailscale CLI is a bash script")


def _fake_cli(tmp_path, monkeypatch, body: str) -> None:
    script = tmp_path / "tailscale"
    script.write_text("#!/usr/bin/env bash\n" + body)
    script.chmod(0o755)
    monkeypatch.setattr(tailscale, "find_cli", lambda: str(script))


@posix_only
def test_run_returns_early_with_approval_url(tmp_path, monkeypatch):
    _fake_cli(
        tmp_path,
        monkeypatch,
        'echo "To enable, visit:"; echo "  https://login.tailscale.com/f/serve?node=n1."; sleep 30\n',
    )
    import time as _t

    t0 = _t.monotonic()
    res = tailscale._run(["serve"])
    assert _t.monotonic() - t0 < 5
    assert not res.ok and res.approve_url == "https://login.tailscale.com/f/serve?node=n1"


@posix_only
def test_run_timeout_and_success(tmp_path, monkeypatch):
    _fake_cli(tmp_path, monkeypatch, "sleep 5\n")
    res = tailscale._run(["x"], timeout=0.5)
    assert res.timed_out and not res.ok
    _fake_cli(tmp_path, monkeypatch, 'echo "all good"\n')
    res = tailscale._run(["x"])
    assert res.ok and res.output == "all good"


# --- auto-start -------------------------------------------------------------------------------


def test_linux_unit_and_mac_plist_text(tmp_path):
    unit = autostart._linux_unit_text(
        "/opt/g/.venv/bin/glimpse-service", ["-c", "/opt/g/config.yaml"], tmp_path
    )
    assert "Restart=always" in unit and "KALENDS_SUPERVISED=1" in unit
    assert "ExecStart=/opt/g/.venv/bin/glimpse-service -c /opt/g/config.yaml" in unit
    plist = autostart._mac_plist_text(
        "/Users/b/My Kalends/.venv/bin/glimpse-service", ["-c", "x & y"], tmp_path
    )
    assert "<string>x &amp; y</string>" in plist and "KeepAlive" in plist and "KALENDS_SUPERVISED" in plist


def test_windows_shortcut_script_quotes(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(subprocess, "run", lambda args, **k: seen.setdefault("args", args))
    autostart.make_windows_shortcut(
        tmp_path / "Kalends.lnk",
        r"C:\Users\Alex\GitHub Projects\kalends\.venv\Scripts\glimpse-service.exe",
        '-c "C:\\Users\\Alex\\GitHub Projects\\kalends\\config.yaml"',
        r"C:\it's here",
        "Kalends",
    )
    script = seen["args"][-1]
    assert "'C:\\it''s here'" in script  # single quotes doubled for PowerShell
    assert "WindowStyle = 7" in script


def test_autostart_toggle_from_panel(signed_in, monkeypatch):
    rt, c, csrf = signed_in
    state = {"on": False}
    monkeypatch.setattr(
        autostart, "status", lambda: autostart.AutostartStatus(True, state["on"], "x", "Starts at login")
    )
    monkeypatch.setattr(autostart, "enable", lambda p: state.update(on=True) or autostart.status())
    monkeypatch.setattr(autostart, "add_start_menu_entry", lambda p: None)
    monkeypatch.setattr(autostart, "disable", lambda: state.update(on=False) or autostart.status())
    c.post("/setup/autostart", data={"csrf": csrf, "action": "on"})
    assert state["on"]
    c.post("/setup/autostart", data={"csrf": csrf, "action": "off"})
    assert not state["on"]


# --- service mode ------------------------------------------------------------------------------


def test_service_exits_quietly_when_already_running(fresh, monkeypatch):
    rt, _ = fresh
    monkeypatch.setattr("kalends.server.is_running", lambda cfg, timeout=1.5: True)
    monkeypatch.delenv("KALENDS_SUPERVISED", raising=False)
    assert service.run_service(rt.store.config_path) == 0
    log = (rt.store.config_path.parent / "logs" / "kalends.log").read_text()
    assert "already running" in log


def test_service_reports_broken_config(tmp_path, monkeypatch):
    p = tmp_path / "config.yaml"
    p.write_text("server: {}\n")
    monkeypatch.delenv("KALENDS_SUPERVISED", raising=False)
    assert service.run_service(p) == 2
    assert "can't start" in (tmp_path / "logs" / "kalends.log").read_text()


def test_cli_init_setup_later(tmp_path):
    from kalends.cli import main

    cfg = tmp_path / "config.yaml"
    main(["-c", str(cfg), "init", "--setup-later"])
    loaded = load_config(cfg)
    assert loaded.admin.password_hash is None and loaded.admin.setup_token
    assert Path(cfg.parent / ".env").exists()


# --- public reachability check ------------------------------------------------------------


class _Resp:
    def __init__(self, status, text):
        self.status_code, self.text = status, text


def test_check_public_paths(monkeypatch):
    import httpx

    from kalends import tailscale as t

    host = "pc.tail1234.ts.net"
    monkeypatch.setattr(t, "public_dns", lambda h: None)
    assert not t.check_public(host).ok
    monkeypatch.setattr(t, "public_dns", lambda h: [])
    assert "doesn't know" in t.check_public(host).summary
    monkeypatch.setattr(t, "public_dns", lambda h: ["100.90.1.2"])
    assert "private" in t.check_public(host).summary

    seen = {}

    def fake_get(self, url, headers=None, extensions=None):
        seen.update(url=url, host=headers["Host"], sni=extensions["sni_hostname"])
        return _Resp(200, '{"ok":true}')

    monkeypatch.setattr(t, "public_dns", lambda h: ["209.177.145.97"])
    monkeypatch.setattr(httpx.Client, "get", fake_get)
    res = t.check_public(host)
    assert res.ok and seen == {"url": "https://209.177.145.97/healthz", "host": host, "sni": host}

    def boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx.Client, "get", boom)
    res = t.check_public(host)
    assert not res.ok and "refused" in res.detail
