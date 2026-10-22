"""Talk to the local Tailscale client: is it installed/signed in, and publish Kalends.

Two publications, both persistent across reboots (``--bg``):

* settings panel, tailnet only:  ``tailscale serve  --https=8443 http://127.0.0.1:8001``
* share links, public internet:  ``tailscale funnel --https=443  http://127.0.0.1:8000``

Every command here is fixed; nothing a user types is ever passed to the CLI.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

PANEL_HTTPS_PORT = 8443
PUBLIC_HTTPS_PORT = 443
TIMEOUT = 25  # seconds; Tailscale waits for approval in the browser on first use
_URL = re.compile(r"https://login\.tailscale\.com/\S+|https://\S*tailscale\.com/\S+")

_CANDIDATES = {
    "win32": [r"C:\Program Files\Tailscale\tailscale.exe", r"C:\Program Files (x86)\Tailscale\tailscale.exe"],
    "darwin": [
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        "/usr/local/bin/tailscale",
        "/opt/homebrew/bin/tailscale",
    ],
}


def find_cli() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    for cand in _CANDIDATES.get(sys.platform, []):
        if Path(cand).is_file():
            return cand
    return None


@dataclass
class Result:
    ok: bool
    output: str = ""
    approve_url: str | None = None
    timed_out: bool = False


def _run(args: list[str], timeout: float = TIMEOUT) -> Result:
    """Run the CLI. Returns early (stopping it) once it prints an approval link to open."""
    cli = find_cli()
    if cli is None:
        return Result(False, "Tailscale isn't installed on this computer.")
    kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",  # Windows would otherwise decode with the ANSI code page
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: no console flash
    try:
        proc = subprocess.Popen([cli, *args], **kwargs)  # noqa: S603 - fixed arguments only
    except OSError as exc:
        return Result(False, f"Couldn't run Tailscale: {exc}")

    lines: list[str] = []
    reader = threading.Thread(target=lambda: lines.extend(proc.stdout), daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    while proc.poll() is None:
        if _URL.search("".join(lines)):
            time.sleep(0.3)  # let the rest of the message arrive
            proc.kill()
            break
        if time.monotonic() > deadline:
            timed_out = True
            proc.kill()
            break
        time.sleep(0.1)
    reader.join(timeout=2)
    out = "".join(lines).strip()
    res = Result(proc.returncode == 0 and not timed_out, out, timed_out=timed_out)
    m = _URL.search(out)
    if m:
        res.approve_url = m.group(0).rstrip(".,)")
        res.ok = False
    return res


def _text(v) -> str:
    if v is None:
        return ""
    return v.decode(errors="replace") if isinstance(v, bytes) else v


@dataclass
class Status:
    installed: bool = False
    running: bool = False
    state: str = ""
    dns_name: str = ""  # e.g. my-pc.tail1234.ts.net (no trailing dot)
    magic_dns: bool | None = None
    panel_published: bool = False
    public_published: bool = False
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def panel_url(self) -> str | None:
        return f"https://{self.dns_name}:{PANEL_HTTPS_PORT}" if self.dns_name else None

    @property
    def public_url(self) -> str | None:
        return f"https://{self.dns_name}" if self.dns_name else None


def status(admin_port: int = 8001, public_port: int = 8000) -> Status:
    st = Status(installed=find_cli() is not None)
    if not st.installed:
        return st
    res = _run(["status", "--json"], timeout=10)
    try:
        data = json.loads(res.output) if res.output else {}
    except json.JSONDecodeError:
        data = {}
    st.state = str(data.get("BackendState", "") or "")
    st.running = st.state == "Running"
    st.dns_name = str((data.get("Self") or {}).get("DNSName", "") or "").rstrip(".")
    tailnet = data.get("CurrentTailnet") or {}
    if "MagicDNSEnabled" in tailnet:
        st.magic_dns = bool(tailnet["MagicDNSEnabled"])
    if not data and not res.ok:
        st.error = res.output or "Tailscale didn't answer."
        return st
    if st.running:
        serve = _serve_config()
        st.panel_published = _has_proxy(serve, PANEL_HTTPS_PORT, admin_port)
        st.public_published = _has_proxy(serve, PUBLIC_HTTPS_PORT, public_port) and _funnel_on(
            serve, PUBLIC_HTTPS_PORT
        )
    return st


def _serve_config() -> dict:
    res = _run(["serve", "status", "--json"], timeout=10)
    try:
        data = json.loads(res.output) if res.output else {}
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _has_proxy(cfg: dict, https_port: int, local_port: int) -> bool:
    """Does any web handler on :https_port proxy to 127.0.0.1/localhost:local_port?"""
    targets = (f"127.0.0.1:{local_port}", f"localhost:{local_port}")
    for hostport, web in (cfg.get("Web") or {}).items():
        if not str(hostport).endswith(f":{https_port}"):
            continue
        for handler in ((web or {}).get("Handlers") or {}).values():
            proxy = str((handler or {}).get("Proxy", ""))
            if any(t in proxy for t in targets) or proxy.strip() == str(local_port):
                return True
    return False


def _funnel_on(cfg: dict, https_port: int) -> bool:
    return any(str(k).endswith(f":{https_port}") and v for k, v in (cfg.get("AllowFunnel") or {}).items())


def _with_yes(args: list[str]) -> Result:
    """Run with --yes (no interactive prompts); retry without it on older clients."""
    res = _run([args[0], "--yes", *args[1:]])
    if not res.ok and "flag provided but not defined" in res.output and "yes" in res.output:
        res = _run(args)
    return res


def publish_panel(admin_port: int = 8001) -> Result:
    return _with_yes(["serve", "--bg", f"--https={PANEL_HTTPS_PORT}", f"http://127.0.0.1:{admin_port}"])


def publish_public(public_port: int = 8000) -> Result:
    return _with_yes(["funnel", "--bg", f"--https={PUBLIC_HTTPS_PORT}", f"http://127.0.0.1:{public_port}"])


def unpublish_public() -> Result:
    return _run(["funnel", f"--https={PUBLIC_HTTPS_PORT}", "off"])


# --- is the public link reachable from the internet? -----------------------------------------

_DOH = [
    "https://cloudflare-dns.com/dns-query?name={host}&type=A",
    "https://dns.google/resolve?name={host}&type=A",
]
_TAILNET_NET = ipaddress.ip_network("100.64.0.0/10")


@dataclass
class PublicCheck:
    ok: bool
    summary: str
    detail: str = ""
    addresses: list[str] = field(default_factory=list)


def public_dns(host: str, timeout: float = 8) -> list[str] | None:
    """A records for ``host`` as the internet sees them (DNS over HTTPS, so not this PC's Tailscale DNS)."""
    import httpx

    for url in _DOH:
        try:
            r = httpx.get(url.format(host=host), headers={"accept": "application/dns-json"}, timeout=timeout)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError):
            continue
        return [a["data"] for a in data.get("Answer") or [] if a.get("type") == 1 and a.get("data")]
    return None


def check_public(host: str, timeout: float = 12) -> PublicCheck:
    """Fetch /healthz the way an outside visitor would: public DNS, then Tailscale's Funnel relay."""
    import httpx

    if not host:
        return PublicCheck(False, "Tailscale hasn't given this computer a name yet.")
    addrs = public_dns(host)
    if addrs is None:
        return PublicCheck(False, "Couldn't reach a public DNS service to check.", "Is this computer online?")
    if not addrs:
        return PublicCheck(
            False,
            "The internet doesn't know this address yet.",
            "Turn on public share links above. Right after turning them on, it can take a few minutes.",
        )
    public = [a for a in addrs if ipaddress.ip_address(a) not in _TAILNET_NET]
    if not public:
        return PublicCheck(
            False,
            "Public DNS points at your private Tailscale address, so only your own devices can open links.",
            "Turn public share links off and on again above, then wait a few minutes.",
            addrs,
        )
    last_error = ""
    for ip in public:
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.get(
                    f"https://{ip}/healthz", headers={"Host": host}, extensions={"sni_hostname": host}
                )
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"[:300]
            continue
        if r.status_code == 200 and '"ok"' in r.text:
            return PublicCheck(
                True,
                "Share links work from the internet.",
                "If one doesn't open on your own laptop with Tailscale switched off, your laptop is "
                "still remembering the private address: try a phone on mobile data, or clear the "
                "browser's DNS cache.",
                public,
            )
        last_error = f"Got HTTP {r.status_code} instead of Kalends."
    return PublicCheck(
        False,
        "The internet can find this computer but couldn't open Kalends.",
        last_error or "Check that Kalends is running and public share links are on.",
        public,
    )
