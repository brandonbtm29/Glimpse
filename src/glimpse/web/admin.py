"""The settings panel: add calendars, choose who sees what, preview every view.

Runs as a second web server in the same process, bound to 127.0.0.1 (default
port 8001). Reach it from other devices on your tailnet with
``tailscale serve --bg --https=8443 http://127.0.0.1:8001``; it is never part of
the public Funnel address. It always requires the admin password.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import time
import zoneinfo
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from markupsafe import Markup, escape
from starlette.concurrency import run_in_threadpool

from .. import __version__, autostart, tailscale, updates
from ..config import (
    MARKERS,
    MAX_WEEKS,
    VIEW_ORDER,
    WEEKDAYS,
    AppConfig,
    AudienceCfg,
    ConfigError,
    Level,
    RuleCfg,
)
from ..core import add_months, allowed_week_bounds, day_range, period_bounds, week_range, week_start
from ..events import parse_events
from ..policy import rule_matches
from ..render.periods import build_heat, build_month
from ..render.week import PALETTE_SIZE, build_week, fmt_day, fmt_range, fmt_time
from ..sources import USER_AGENT, normalize_url
from ..store import Draft, _env_name, finish_setup
from .app import COMMON_ZONES, VIEW_LABELS
from .auth import LoginLimiter, feed_token
from .runtime import Runtime
from .state import ago
from .viewer import person_viewer

log = logging.getLogger("glimpse.admin")
HERE = Path(__file__).parent
COOKIE = "glimpse_admin"
SESSION_DAYS = 7
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
PALETTE_HEX = ["#3b6fd8", "#2f9e63", "#e07b26", "#8a5cd6", "#d6456a", "#1f9aa3", "#b8901a", "#6b7584"]
LEVELS = [lv.value for lv in Level]
LEVEL_HELP = {
    "full": "Title, time, location and notes",
    "title": "Title and time only",
    "label": "The calendar's label (e.g. “Class”) and time",
    "busy": "An anonymous “Busy” block",
    "hidden": "Nothing at all",
}
DAY_LABELS = dict(zip(WEEKDAYS, ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], strict=True))

FLASH = {
    "saved": "Saved. Changes are live.",
    "cal_added": (
        "Calendar added. Each audience sees it at its default level until you change it on Who sees what."
    ),
    "cal_deleted": "Calendar removed.",
    "aud_added": "Audience created. Copy its link below when you're ready to share it.",
    "aud_deleted": "Audience removed. Its link no longer works.",
    "rotated": "New link created. The old link has stopped working.",
    "pw_set": "Password set. Anyone already signed in will need the new password.",
    "pw_removed": "Password removed. The link alone now opens this page.",
    "refreshed": "Calendars refreshed.",
    "admin_pw": "Admin password changed.",
    "welcome": "Password saved. Next, put Glimpse on Tailscale so you can reach it from your other devices.",
    "ts_panel": "Settings panel published to your tailnet.",
    "ts_public": "Share links are now public.",
    "ts_public_off": "Share links are no longer public.",
    "auto_on": "Glimpse will start automatically when you sign in.",
    "auto_off": "Automatic start turned off.",
    "grp_added": "Group created. Put calendars in it below, then set its level on Who sees what.",
    "grp_deleted": "Group removed. Its calendars are still here, now without a group.",
    "rule_added": "Rule added. It applies to every audience right away.",
    "rule_deleted": "Rule removed.",
    "person_added": "Person added. Copy their link below and send it to them.",
    "person_deleted": "Person removed. Their link no longer works.",
    "person_rotated": "New link created for them. Their old link has stopped working.",
    "person_moved": "Moved. Their link now shows the new audience's view.",
    "checked": "Checked GitHub for updates.",
}
VIEW_HELP = {
    "week": "Week: the hour-by-hour grid",
    "month": "Month: a calendar with each day's items",
    "quarter": "3 months: how busy each day is, no titles",
    "year": "Year: how busy each day is, no titles",
}
RULE_FIELDS = {"title": "Title", "notes": "Notes", "location": "Location"}
RULE_PREVIEW_WEEKS = 4

ADMIN_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'; connect-src 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "Cache-Control": "no-store",
}

_hasher = PasswordHasher()


class SaveConflict(Exception):
    """The settings changed (another device, or a hand edit) after this page was opened."""


def browse_label(aud: AudienceCfg, lo: date, hi: date) -> str:
    if aud.weeks_ahead is None and aud.weeks_behind is None:
        return "any week"
    start = "all past weeks" if aud.weeks_behind is None else fmt_day(lo)
    end = "all future weeks" if aud.weeks_ahead is None else fmt_day(hi + timedelta(days=6))
    if aud.weeks_ahead is not None and aud.weeks_behind is not None:
        return fmt_range(lo, hi + timedelta(days=6))
    return f"{start} to {end}"


def release_notes(text: str) -> Markup:
    """A tiny, safe Markdown subset for release notes: headings, bullets, **bold**, `code`."""
    out = []
    for line in (text or "").splitlines():
        h = re.match(r"^#{1,4}\s+(.*)", line)
        body = escape(h.group(1) if h else line)
        body = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", str(body))
        body = re.sub(r"`([^`]+)`", r"<code>\1</code>", body)
        if h:
            out.append(f"<b class='nh'>{body}</b>")
        elif re.match(r"^\s*[-*]\s+", line):
            out.append("• " + re.sub(r"^\s*[-*]\s+", "", body))
        else:
            out.append(body)
    return Markup("\n".join(out))  # noqa: S704 - every piece above was escaped first


def mask_url(url: str) -> str:
    """Show enough of a feed link to recognise it, not enough to use it."""
    try:
        parts = urlsplit(normalize_url(url))
    except ValueError:
        return "(invalid link)"
    if not parts.netloc:
        return "local file …" + url[-18:]
    tail = (parts.path + ("?" + parts.query if parts.query else ""))[-4:]
    return f"{parts.netloc}/…{tail}"


def provider_of(url: str) -> str:
    host = urlsplit(normalize_url(url)).netloc.lower()
    if "google" in host:
        return "Google"
    if "outlook" in host or "office365" in host or "live.com" in host or "microsoft" in host:
        return "Outlook"
    if "icloud" in host or "apple" in host:
        return "iCloud"
    return "Local file" if not host else host


def test_feed(url: str, cfg: AppConfig) -> dict[str, Any]:
    """Fetch a feed link once and summarise it (used by 'Test link')."""
    url = normalize_url(url.strip())
    if not url:
        return {"ok": False, "error": "Paste a link first."}
    if not re.match(r"^https?://", url):
        return {"ok": False, "error": "Links should start with https:// or webcal://"}
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as c:
            resp = c.get(url)
        if resp.status_code != 200:
            return {"ok": False, "error": f"The server answered HTTP {resp.status_code}. Check the link."}
        text = resp.text
        if "BEGIN:VCALENDAR" not in text[:2000]:
            return {"ok": False, "error": "That link didn't return a calendar feed (ICS)."}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"Couldn't reach it ({type(exc).__name__})."}
    s, _ = week_range(week_start(date.today(), cfg), cfg)
    e = s + timedelta(weeks=4)
    events = parse_events("test", text, s, e, cfg.tz)
    m = re.search(r"^X-WR-CALNAME:(.+)$", text, re.MULTILINE)
    return {
        "ok": True,
        "events": len(events),
        "name": m.group(1).strip() if m else "",
        "provider": provider_of(url),
    }


def create_admin_app(rt: Runtime) -> FastAPI:
    if rt.store is None:
        raise ValueError("the settings panel needs a ConfigStore")
    store = rt.store
    limiter = LoginLimiter(per_ip=5, per_audience=15)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals.update(
        version=__version__,
        fmt_time=fmt_time,
        fmt_day=fmt_day,
        LEVELS=LEVELS,
        LEVEL_HELP=LEVEL_HELP,
        WEEKDAYS=WEEKDAYS,
        DAY_LABELS=DAY_LABELS,
        MARKERS=MARKERS,
        MAX_WEEKS=MAX_WEEKS,
        mask_url=mask_url,
        provider_of=provider_of,
    )
    templates.env.filters["release_notes"] = release_notes

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def cfg() -> AppConfig:
        return rt.cfg

    # -- access control ----------------------------------------------------------------

    @app.exception_handler(SaveConflict)
    async def save_conflict(request: Request, exc: SaveConflict):
        back = urlsplit(request.headers.get("referer", "")).path or "/"
        if not back.startswith("/") or back.startswith("//"):
            back = "/"
        return page(
            request,
            "admin/conflict.html",
            {"back": back, "error": None},
            session(request),
            status=409,
        )

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not request.url.path.startswith("/static/"):
            await run_in_threadpool(rt.reload_if_changed)
        allowed = cfg().admin.allowed_tailscale_users
        if allowed and not request.url.path.startswith("/static/"):
            login = request.headers.get("Tailscale-User-Login", "")
            if login.lower() not in {u.lower() for u in allowed}:
                resp = templates.TemplateResponse(
                    request,
                    "admin/message.html",
                    {
                        "heading": "Not allowed",
                        "body": "This settings panel is limited to specific Tailscale accounts.",
                    },
                    status_code=403,
                )
                for k, v in ADMIN_HEADERS.items():
                    resp.headers[k] = v
                return resp
        resp = await call_next(request)
        for k, v in ADMIN_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp

    def serializer() -> URLSafeTimedSerializer:
        return URLSafeTimedSerializer(cfg().server.secret_key, salt="glimpse-admin")

    def admin_fp() -> str:
        return hashlib.sha256((cfg().admin.password_hash or "").encode()).hexdigest()[:16]

    def session(request: Request) -> dict | None:
        token = request.cookies.get(COOKIE)
        if not token or not cfg().admin.password_hash:
            return None
        try:
            data = serializer().loads(token, max_age=SESSION_DAYS * 86400)
        except BadSignature:
            return None
        if not isinstance(data, dict) or not hmac.compare_digest(str(data.get("f", "")), admin_fp()):
            return None
        return data

    def set_session(resp: Response, csrf: str | None = None) -> None:
        token = serializer().dumps({"f": admin_fp(), "c": csrf or secrets.token_urlsafe(24)})
        resp.set_cookie(
            COOKIE,
            token,
            max_age=SESSION_DAYS * 86400,
            httponly=True,
            secure=cfg().admin.secure_cookies,
            samesite="strict",
            path="/",
        )

    def page(request: Request, name: str, ctx: dict, sess: dict | None = None, status: int = 200):
        base = {
            "cfg": cfg(),
            "csrf": (sess or {}).get("c", ""),
            "flash": FLASH.get(request.query_params.get("ok", "")),
            "nav": name.split("/")[-1].split(".")[0],
            "reload_error": rt.reload_error,
            "update": rt.updates.last if cfg().updates.check and rt.updates.last.update_available else None,
            "rev": store.revision(),
        }
        base.update(ctx)
        return templates.TemplateResponse(request, name, base, status_code=status)

    def to_login() -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    def done(path: str, flash: str) -> RedirectResponse:
        sep = "&" if "?" in path else "?"
        return RedirectResponse(f"{path}{sep}ok={flash}", status_code=303)

    async def checked_form(request: Request) -> tuple[dict | None, Any]:
        """Session + parsed form, or (None, None) if not signed in or the CSRF token is wrong."""
        sess = session(request)
        if sess is None:
            return None, None
        form = await request.form()
        if not hmac.compare_digest(str(form.get("csrf", "")), str(sess.get("c", ""))):
            return None, None
        rev = str(form.get("rev", ""))
        if rev and rev != store.revision():
            raise SaveConflict
        return sess, form

    async def save(mutate) -> AppConfig:
        new_cfg = await run_in_threadpool(store.update, mutate)
        await run_in_threadpool(rt.apply, new_cfg)
        return new_cfg

    def public_link(aud: AudienceCfg) -> str:
        return f"{cfg().server.base_url.rstrip('/')}/a/{aud.slug}"

    def feed_link(aid: str) -> str:
        aud = cfg().audiences[aid]
        return f"{cfg().server.base_url.rstrip('/')}/a/{aud.slug}/feed/{feed_token(cfg(), aid)}.ics"

    def person_link(pid: str) -> str:
        return f"{cfg().server.base_url.rstrip('/')}/p/{cfg().people[pid].slug}"

    def person_feed_link(pid: str) -> str:
        v = person_viewer(cfg(), pid)
        return f"{cfg().server.base_url.rstrip('/')}{v.base}/feed/{feed_token(cfg(), v)}.ics"

    def people_rows(audience: str | None = None) -> list[dict]:
        c = cfg()
        today = rt.glimpse.today()
        rows = []
        for pid, p in c.people.items():
            if audience and p.audience != audience:
                continue
            last = rt.seen.get(f"person:{pid}")
            synced = rt.seen.get(f"person:{pid}:feed")
            if not p.enabled:
                status = ("off", "Switched off")
            elif p.expires and today > p.expires:
                status = ("expired", f"Expired {fmt_day(p.expires)}")
            elif p.expires:
                status = ("ok", f"Until {fmt_day(p.expires)}")
            else:
                status = ("ok", "Active")
            rows.append(
                {
                    "id": pid,
                    "p": p,
                    "aud": c.audiences[p.audience],
                    "link": person_link(pid),
                    "status": status,
                    "seen": ago(last),
                    "synced": ago(synced) if synced else None,
                    "password": "own"
                    if p.password_hash
                    else (
                        "audience"
                        if p.use_audience_password and c.audiences[p.audience].password_hash
                        else "none"
                    ),
                }
            )
        return rows

    def feed_rows() -> list[dict]:
        c = cfg()
        g = rt.glimpse
        s, e = week_range(week_start(g.today(), c), c)
        events = g.events_between(s, e)
        rows = []
        for i, (cid, cal) in enumerate(c.calendars.items()):
            st = g.cache.states.get(cid)
            status = "pending"
            if st and st.last_error:
                status = "error"
            elif st and st.text:
                status = "ok"
            rows.append(
                {
                    "id": cid,
                    "cal": cal,
                    "color": cal.color or PALETTE_HEX[i % PALETTE_SIZE],
                    "status": status,
                    "error": st.last_error if st else None,
                    "updated": st.last_success.astimezone(c.tz) if st and st.last_success else None,
                    "count": sum(1 for ev in events if ev.calendar_id == cid),
                    "masked": mask_url(cal.url),
                    "provider": provider_of(cal.url),
                    "group": c.group_of(cid),
                }
            )
        return rows

    def sharing_rows() -> list[dict]:
        """Calendars ordered by group: a header row per group, then its calendars, then the ungrouped."""
        c = cfg()
        feeds = {r["id"]: r for r in feed_rows()}
        rows: list[dict] = []
        for gid, grp in c.groups.items():
            members = [feeds[cid] for cid in grp.calendars if cid in feeds]
            rows.append({"kind": "group", "id": gid, "name": grp.name, "count": len(members)})
            rows.extend({"kind": "cal", "feed": f, "group": gid} for f in members)
        loose = [f for f in feeds.values() if f["group"] is None]
        if loose and c.groups:
            rows.append({"kind": "loose"})
        rows.extend({"kind": "cal", "feed": f, "group": None} for f in loose)
        return rows

    # -- sign in -------------------------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        if not cfg().admin.password_hash:
            return page(request, "admin/setup_needed.html", {})
        if session(request):
            return RedirectResponse("/", status_code=303)
        return page(request, "admin/login.html", {"error": None})

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request):
        c = cfg()
        if not c.admin.password_hash:
            return page(request, "admin/setup_needed.html", {})
        form = await request.form()
        ip = request.client.host if request.client else "unknown"
        if limiter.blocked("admin", ip):
            return page(
                request, "admin/login.html", {"error": "Too many attempts. Wait a few minutes."}, status=429
            )
        try:
            ok = _hasher.verify(c.admin.password_hash, str(form.get("password", "")))
        except (VerificationError, InvalidHashError):
            ok = False
        if not ok:
            limiter.failed("admin", ip)
            return page(request, "admin/login.html", {"error": "That password didn't work."}, status=401)
        resp = RedirectResponse("/", status_code=303)
        set_session(resp)
        return resp

    @app.post("/logout")
    async def logout(request: Request):
        sess, _ = await checked_form(request)
        resp = to_login()
        if sess:
            resp.delete_cookie(COOKIE, path="/")
        return resp

    # -- first run: choose the admin password ------------------------------------------------
    # Only before a password exists, only with the one-time token from `glimpse open`
    # (stored in .env on this computer), and only from this computer: requests that come
    # through `tailscale serve` carry Tailscale headers / a forwarded client address.

    def is_local(request: Request) -> bool:
        host = request.client.host if request.client else ""
        via_tailscale = any(h.lower().startswith("tailscale-") for h in request.headers)
        return host in ("127.0.0.1", "::1", "localhost") and not via_tailscale

    def token_ok(token: str) -> bool:
        expected = cfg().admin.setup_token
        return bool(expected) and hmac.compare_digest(token.encode(), expected.encode())

    @app.get("/setup/start", response_class=HTMLResponse)
    def setup_start(request: Request, token: str = ""):
        if cfg().admin.password_hash:
            return to_login()
        if not (is_local(request) and token_ok(token)):
            return page(request, "admin/setup_needed.html", {}, status=403)
        return page(request, "admin/first_run.html", {"token": token, "error": None})

    @app.post("/setup/start", response_class=HTMLResponse)
    async def setup_start_submit(request: Request):
        if cfg().admin.password_hash:
            return to_login()
        form = await request.form()
        token = str(form.get("token", ""))
        if not (is_local(request) and token_ok(token)):
            return page(request, "admin/setup_needed.html", {}, status=403)
        pw, pw2 = str(form.get("password", "")), str(form.get("password2", ""))
        error = None
        if len(pw) < 10:
            error = "Use at least 10 characters."
        elif pw != pw2:
            error = "The two passwords didn't match."
        if error:
            return page(request, "admin/first_run.html", {"token": token, "error": error}, status=400)
        h = await run_in_threadpool(_hasher.hash, pw)
        await save(lambda d: finish_setup(d, h))
        resp = done("/setup", "welcome")
        set_session(resp)
        return resp

    # -- setup page: Tailscale + automatic start ------------------------------------------------

    ts_cache: dict[str, Any] = {"at": 0.0, "st": None}

    def ts_status(force: bool = False) -> tailscale.Status:
        if force or ts_cache["st"] is None or time.monotonic() - ts_cache["at"] > 60:
            ts_cache["st"] = tailscale.status(cfg().admin.port, cfg().server.port)
            ts_cache["at"] = time.monotonic()
        return ts_cache["st"]

    def checklist(ts: tailscale.Status) -> list[dict]:
        c = cfg()
        auto = autostart.status()
        return [
            {"label": "Admin password", "done": bool(c.admin.password_hash), "href": "/settings"},
            {"label": "Settings panel on your devices", "done": ts.panel_published, "href": "/setup"},
            {"label": "Public share links", "done": ts.public_published, "href": "/setup"},
            {"label": "Start automatically", "done": auto.enabled or not auto.supported, "href": "/setup"},
            {"label": "Add a calendar", "done": bool(c.calendars), "href": "/calendars"},
            {"label": "Create an audience", "done": bool(c.audiences), "href": "/sharing"},
        ]

    @app.get("/setup", response_class=HTMLResponse)
    def setup(request: Request, refresh: str = ""):
        sess = session(request)
        if not sess:
            return to_login()
        ts = ts_status(force=bool(refresh))
        return page(
            request,
            "admin/setup.html",
            {"ts": ts, "auto": autostart.status(), "steps": checklist(ts), "result": None, **update_ctx()},
            sess,
        )

    def update_ctx() -> dict:
        last = rt.updates.last
        ready = updates.update_readiness(updates.PROJECT_ROOT, fetch=False)
        result = updates.read_result(store.config_path) if store else None
        return {
            "upd": last,
            "upd_ready": ready,
            "upd_result": result,
            "repo": cfg().updates.repo,
        }

    @app.post("/setup/updates/check")
    async def updates_check(request: Request):
        sess, _ = await checked_form(request)
        if not sess:
            return to_login()
        await run_in_threadpool(rt.updates.check, cfg().updates.repo, True)
        return done("/setup#updates", "checked")

    @app.post("/setup/updates/apply", response_class=HTMLResponse)
    async def updates_apply(request: Request):
        sess, _ = await checked_form(request)
        if not sess:
            return to_login()
        last = rt.updates.last
        if not (last.release and last.update_available) or store is None:
            return done("/setup#updates", "checked")
        tag = last.release.tag
        ready = await run_in_threadpool(updates.update_readiness, updates.PROJECT_ROOT, tag)
        ts = ts_status()
        if not ready.ok:
            ctx = {
                "ts": ts,
                "auto": autostart.status(),
                "steps": checklist(ts),
                "result": None,
                **update_ctx(),
            }
            ctx["upd_error"] = ready.reason
            return page(request, "admin/setup.html", ctx, sess, 400)
        from ..service import service_command

        backup = await run_in_threadpool(updates.backup_settings, store.config_path, store.env_path)
        updates.result_path(store.config_path).unlink(missing_ok=True)
        await run_in_threadpool(
            updates.start_update,
            updates.PROJECT_ROOT,
            store.config_path,
            tag,
            service_command(store.config_path),
            f"http://127.0.0.1:{cfg().server.port}/healthz",
        )
        log.info("updating to %s (settings backed up to %s)", tag, backup)
        return page(
            request,
            "admin/updating.html",
            {"tag": tag, "backup": backup.name, "version_now": __version__},
            sess,
        )

    async def ts_action(request: Request, action, flash: str):
        sess, _ = await checked_form(request)
        if not sess:
            return to_login()
        res: tailscale.Result = await run_in_threadpool(action)
        ts = await run_in_threadpool(ts_status, True)
        if res.ok:
            if ts.dns_name and not cfg().server.base_url.startswith(f"https://{ts.dns_name}"):
                await save(lambda d: d.section("server").__setitem__("base_url", f"https://{ts.dns_name}"))
            return done("/setup", flash)
        return page(
            request,
            "admin/setup.html",
            {"ts": ts, "auto": autostart.status(), "steps": checklist(ts), "result": res, **update_ctx()},
            sess,
            status=200,
        )

    @app.post("/setup/check-public", response_class=HTMLResponse)
    async def setup_check_public(request: Request):
        sess, _ = await checked_form(request)
        if not sess:
            return to_login()
        ts = await run_in_threadpool(ts_status, True)
        check = await run_in_threadpool(tailscale.check_public, ts.dns_name)
        return page(
            request,
            "admin/setup.html",
            {
                "ts": ts,
                "auto": autostart.status(),
                "steps": checklist(ts),
                "result": None,
                "check": check,
                **update_ctx(),
            },
            sess,
        )

    @app.post("/setup/tailscale/panel")
    async def setup_ts_panel(request: Request):
        return await ts_action(request, lambda: tailscale.publish_panel(cfg().admin.port), "ts_panel")

    @app.post("/setup/tailscale/public")
    async def setup_ts_public(request: Request):
        return await ts_action(request, lambda: tailscale.publish_public(cfg().server.port), "ts_public")

    @app.post("/setup/tailscale/public-off")
    async def setup_ts_public_off(request: Request):
        return await ts_action(request, tailscale.unpublish_public, "ts_public_off")

    @app.post("/setup/autostart")
    async def setup_autostart(request: Request):
        sess, form = await checked_form(request)
        if not sess:
            return to_login()
        if form.get("action") == "off":
            await run_in_threadpool(autostart.disable)
            return done("/setup", "auto_off")
        await run_in_threadpool(autostart.enable, store.config_path)
        await run_in_threadpool(autostart.add_start_menu_entry, store.config_path)
        return done("/setup", "auto_on")

    # -- overview ------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        sess = session(request)
        if not sess:
            return to_login()
        c = cfg()
        auds = [
            {
                "id": aid,
                "aud": aud,
                "link": public_link(aud),
                "feed": feed_link(aid) if aud.allow_ics else None,
                "overrides": len(aud.calendars),
            }
            for aid, aud in c.audiences.items()
        ]
        local_base = any(h in c.server.base_url for h in ("127.0.0.1", "localhost"))
        steps = checklist(ts_status())
        return page(
            request,
            "admin/overview.html",
            {
                "feeds": feed_rows(),
                "auds": auds,
                "local_base": local_base,
                "steps": steps,
                "steps_done": sum(1 for st in steps if st["done"]),
            },
            sess,
        )

    @app.post("/api/refresh/{cid}")
    async def api_refresh_one(request: Request, cid: str):
        """Refresh one calendar now; used by the live progress list on Overview."""
        sess = session(request)
        if not sess or not hmac.compare_digest(request.headers.get("X-CSRF", ""), sess.get("c", "")):
            return Response(
                '{"ok": false, "error": "Please sign in again."}', 403, media_type="application/json"
            )
        c = cfg()
        if cid not in c.calendars:
            return Response('{"ok": false, "error": "Unknown calendar."}', 404, media_type="application/json")

        def work() -> dict:
            g = rt.glimpse
            with httpx.Client(
                timeout=20.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}
            ) as client:
                g.cache.refresh(cid, client)
            st = g.cache.states[cid]
            row = next(r for r in feed_rows() if r["id"] == cid)
            return {
                "ok": st.last_error is None,
                "error": st.last_error,
                "count": row["count"],
                "updated": f"{fmt_day(row['updated'].date())}, {fmt_time(row['updated'])}"
                if row["updated"]
                else "",
            }

        import json as _json

        return Response(_json.dumps(await run_in_threadpool(work)), media_type="application/json")

    @app.post("/refresh")
    async def refresh(request: Request):
        sess, form = await checked_form(request)
        if not sess:
            return to_login()
        await run_in_threadpool(rt.refresh)
        back = str(form.get("back", "/"))
        return done(back if back.startswith("/") and not back.startswith("//") else "/", "refreshed")

    # -- calendars -------------------------------------------------------------------------

    def calendars_page(request, sess, form_values=None, test=None, error=None, status=200):
        n = len(cfg().calendars)
        values = form_values or {"name": "", "url": "", "label": "", "color": PALETTE_HEX[n % PALETTE_SIZE]}
        return page(
            request,
            "admin/calendars.html",
            {"feeds": feed_rows(), "form": values, "test": test, "error": error},
            sess,
            status,
        )

    @app.get("/calendars", response_class=HTMLResponse)
    def calendars(request: Request):
        sess = session(request)
        if not sess:
            return to_login()
        return calendars_page(request, sess)

    def _cal_values(form) -> dict[str, str]:
        return {k: str(form.get(k, "")).strip() for k in ("name", "url", "label", "color", "group", "marker")}

    @app.post("/calendars/test", response_class=HTMLResponse)
    async def calendars_test(request: Request):
        sess, form = await checked_form(request)
        if not sess:
            return to_login()
        values = _cal_values(form)
        result = await run_in_threadpool(test_feed, values["url"], cfg())
        if result.get("ok") and not values["name"] and result.get("name"):
            values["name"] = result["name"]
        return calendars_page(request, sess, values, test=result)

    @app.post("/calendars/add", response_class=HTMLResponse)
    async def calendars_add(request: Request):
        sess, form = await checked_form(request)
        if not sess:
            return to_login()
        v = _cal_values(form)
        if not v["name"] or not v["url"]:
            return calendars_page(
                request, sess, v, error="A name and a feed link are both needed.", status=400
            )
        try:

            def mutate(d: Draft) -> None:
                cid = d.add_calendar(v["name"], v["url"], v["label"] or None, v["color"] or None)
                if v["group"] in d.groups:
                    d.set_calendar_group(cid, v["group"])
                if v["marker"] in MARKERS:
                    d.calendars[cid]["marker"] = v["marker"]

            await save(mutate)
        except ConfigError as exc:
            return calendars_page(request, sess, v, error=str(exc), status=400)
        return done("/calendars", "cal_added")

    @app.get("/calendars/{cid}", response_class=HTMLResponse)
    def calendar_edit(request: Request, cid: str):
        sess = session(request)
        if not sess:
            return to_login()
        row = next((r for r in feed_rows() if r["id"] == cid), None)
        if row is None:
            return RedirectResponse("/calendars", status_code=303)
        used_by = [
            (aud.title, cfg().level_for(aud, cid).value, cfg().level_source(aud, cid))
            for aud in cfg().audiences.values()
        ]
        return page(
            request, "admin/calendar_edit.html", {"row": row, "used_by": used_by, "error": None}, sess
        )

    @app.post("/calendars/{cid}", response_class=HTMLResponse)
    async def calendar_save(request: Request, cid: str):
        sess, form = await checked_form(request)
        if not sess:
            return to_login()
        if cid not in cfg().calendars:
            return RedirectResponse("/calendars", status_code=303)
        v = _cal_values(form)

        def mutate(d: Draft) -> None:
            node = d.calendars[cid]
            if v["name"]:
                node["name"] = v["name"]
            if v["label"]:
                node["label"] = v["label"]
            else:
                node.pop("label", None)
            if v["color"]:
                node["color"] = v["color"]
            if v["url"]:
                d.set_secret(node, "url", v["url"], _env_name("CAL", cid, "URL"))
            if v["marker"] in MARKERS:
                node["marker"] = v["marker"]
            else:
                node.pop("marker", None)
            d.set_calendar_group(cid, v["group"] if v["group"] in d.groups else None)

        try:
            await save(mutate)
        except ConfigError as exc:
            row = next(r for r in feed_rows() if r["id"] == cid)
            return page(
                request, "admin/calendar_edit.html", {"row": row, "used_by": [], "error": str(exc)}, sess, 400
            )
        return done(f"/calendars/{cid}", "saved")

    @app.post("/calendars/{cid}/delete")
    async def calendar_delete(request: Request, cid: str):
        sess, _ = await checked_form(request)
        if not sess:
            return to_login()
        await save(lambda d: d.remove_calendar(cid))
        return done("/calendars", "cal_deleted")

    # -- groups ----------------------------------------------------------------------------

    @app.post("/groups/add", response_class=HTMLResponse)
    async def group_add(request: Request):
        sess, form = await checked_form(request)
        if not sess:
            return to_login()
        name = str(form.get("name", "")).strip()[:60]
        if not name:
            return calendars_page(request, sess, error="Give the group a name.", status=400)
        members = [cid for cid in form.getlist("calendar") if cid in cfg().calendars]

        def mutate(d: Draft) -> None:
            gid = d.add_group(name)
            for cid in members:
                d.set_calendar_group(cid, gid)

        try:
            await save(mutate)
        except ConfigError as exc:
            return calendars_page(request, sess, error=str(exc), status=400)
        return done("/calendars", "grp_added")

    @app.post("/groups/{gid}", response_class=HTMLResponse)
    async def group_save(request: Request, gid: str):
        sess, form = await checked_form(request)
        if not sess:
            return to_login()
        if gid not in cfg().groups:
            return RedirectResponse("/calendars", status_code=303)
        name = str(form.get("name", "")).strip()[:60]
        members = {cid for cid in form.getlist("calendar") if cid in cfg().calendars}

        extras = {k: str(form.get(k, "")).strip() for k in ("label", "color", "marker")}
        use_color = form.get("use_color") == "on"

        def mutate(d: Draft) -> None:
            if name:
                d.rename_group(gid, name)
            node = d.groups[gid]
            if extras["label"]:
                node["label"] = extras["label"][:40]
            else:
                node.pop("label", None)
            if use_color and extras["color"]:
                node["color"] = extras["color"]
            else:
                node.pop("color", None)
            if extras["marker"] in MARKERS:
                node["marker"] = extras["marker"]
            else:
                node.pop("marker", None)
            for cid in d.calendars:
                if cid in members:
                    d.set_calendar_group(cid, gid)
                elif d.group_of(cid) == gid:
                    d.set_calendar_group(cid, None)

        try:
            await save(mutate)
        except ConfigError as exc:
            return calendars_page(request, sess, error=str(exc), status=400)
        return done("/calendars", "saved")

    @app.post("/groups/{gid}/delete")
    async def group_delete(request: Request, gid: str):
        sess, _ = await checked_form(request)
        if not sess:
            return to_login()
        await save(lambda d: d.remove_group(gid))
        return done("/calendars", "grp_deleted")

    # -- sharing matrix --------------------------------------------------------------------

    def audience_cards() -> list[dict]:
        c = cfg()
        out = []
        for aid, aud in c.audiences.items():
            out.append(
                {
                    "id": aid,
                    "aud": aud,
                    "link": public_link(aud),
                    "people": [p for p in c.people.values() if p.audience == aid],
                    "seen": ago(rt.seen.get(f"audience:{aid}")),
                }
            )
        return out

    def sharing_page(request, sess, error=None, status=200):
        return page(
            request,
            "admin/sharing.html",
            {"rows": sharing_rows(), "cards": audience_cards(), "error": error, "VIEW_LABELS": VIEW_LABELS},
            sess,
            status,
        )

    @app.get("/sharing", response_class=HTMLResponse)
    def sharing(request: Request):
        sess = session(request)
        if not sess:
            return to_login()
        return sharing_page(request, sess)

    @app.post("/sharing", response_class=HTMLResponse)
    async def sharing_save(request: Request):
        sess, form = await checked_form(request)
        if not sess:
            return to_login()

        def mutate(d: Draft) -> None:
            for aid, node in d.audiences.items():
                dflt = str(form.get(f"default__{aid}", ""))
                if dflt in LEVELS:
                    node["default"] = dflt
                cals = node.get("calendars") or {}
                for cid in d.calendars:
                    val = str(form.get(f"lvl__{aid}__{cid}", "default"))
                    if val in LEVELS:
                        cals[cid] = val
                    elif val == "default":
                        cals.pop(cid, None)
                node["calendars"] = cals
                grps = node.get("groups") or {}
                for gid in d.groups:
                    val = str(form.get(f"grp__{aid}__{gid}", "default"))
                    if val in LEVELS:
                        grps[gid] = val
                    elif val == "default":
                        grps.pop(gid, None)
                if grps:
                    node["groups"] = grps
                else:
                    node.pop("groups", None)

        try:
            await save(mutate)
        except ConfigError as exc:
            return sharing_page(request, sess, str(exc), 400)
        return done("/sharing", "saved")

    # -- audiences ---------------------------------------------------------------------------

    @app.post("/audiences/add")
    async def audience_add(request: Request):
        sess, form = await checked_form(request)
        if not sess:
            return to_login()
        title = str(form.get("title", "")).strip()
        default = str(form.get("default", "busy"))
        if not title or default not in LEVELS:
            return done("/sharing", "saved")
        box: dict[str, str] = {}

        def mutate(d: Draft) -> None:
            box["id"] = d.add_audience(title, default)

        await save(mutate)
        return done(f"/audiences/{box['id']}", "aud_added")

    def audience_page(request, sess, aid, error=None, status=200):
        aud = cfg().audiences[aid]
        lo, hi = allowed_week_bounds(aud, rt.glimpse.today(), cfg())
        return page(
            request,
            "admin/audience.html",
            {
                "aid": aid,
                "aid_current": aid,
                "aud": aud,
                "link": public_link(aud),
                "feed": feed_link(aid) if aud.allow_ics else None,
                "feeds": feed_rows(),
                "range": browse_label(aud, lo, hi),
                "people": people_rows(aid),
                "seen": ago(rt.seen.get(f"audience:{aid}")),
                "view_help": VIEW_HELP,
                "zones": sorted({*COMMON_ZONES, cfg().timezone, *([aud.timezone] if aud.timezone else [])}),
                "error": error,
            },
            sess,
            status,
        )

    @app.get("/audiences/{aid}", response_class=HTMLResponse)
    def audience(request: Request, aid: str):
        sess = session(request)
        if not sess:
            return to_login()
        if aid not in cfg().audiences:
            return RedirectResponse("/sharing", status_code=303)
        return audience_page(request, sess, aid)

    @app.post("/audiences/{aid}", response_class=HTMLResponse)
    async def audience_save(request: Request, aid: str):
        sess, form = await checked_form(request)
        if not sess:
            return to_login()
        if aid not in cfg().audiences:
            return RedirectResponse("/sharing", status_code=303)
        errors = []
        title = str(form.get("title", "")).strip() or cfg().audiences[aid].title
        days = [d for d in form.getlist("day") if d in WEEKDAYS]
        start, end = str(form.get("start", "08:00")), str(form.get("end", "18:00"))
        limit = form.get("limit_hours") == "on"
        if limit and not days:
            errors.append("Pick at least one day, or turn off the hours limit.")
        if limit and not (_TIME.match(start) and _TIME.match(end)):
            errors.append("Times should look like 08:00.")

        def num(name: str, dflt: int) -> int | None:
            if form.get(f"{name}_all") == "on":
                return None  # no limit
            try:
                return max(0, min(MAX_WEEKS, int(str(form.get(name, dflt)))))
            except ValueError:
                return dflt

        if errors:
            return audience_page(request, sess, aid, " ".join(errors), 400)

        def mutate(d: Draft) -> None:
            node = d.audiences[aid]
            node["title"] = title
            dflt = str(form.get("default", ""))
            if dflt in LEVELS:
                node["default"] = dflt
            if limit:
                node["hours"] = {"days": days, "start": start.zfill(5), "end": end.zfill(5)}
            else:
                node.pop("hours", None)
            node["weeks_ahead"] = num("weeks_ahead", 4)
            node["weeks_behind"] = num("weeks_behind", 1)
            for flag in ("show_all_day_busy", "allow_excel", "allow_ics", "shared_link", "group_names"):
                node[flag] = form.get(flag) == "on"
            views = [v for v in VIEW_ORDER if v in form.getlist("view")] or ["week"]
            node["views"] = views
            dv = str(form.get("default_view", "week"))
            node["default_view"] = dv if dv in views else views[0]
            zone = str(form.get("timezone", ""))
            if zone and zone in zoneinfo.available_timezones() and zone != d.raw.get("timezone"):
                node["timezone"] = zone
            else:
                node.pop("timezone", None)

        try:
            await save(mutate)
        except ConfigError as exc:
            return audience_page(request, sess, aid, str(exc), 400)
        return done(f"/audiences/{aid}", "saved")

    @app.post("/audiences/{aid}/password", response_class=HTMLResponse)
    async def audience_password(request: Request, aid: str):
        sess, form = await checked_form(request)
        if not sess:
            return to_login()
        if aid not in cfg().audiences:
            return RedirectResponse("/sharing", status_code=303)
        if form.get("action") == "remove":
            await save(lambda d: d.set_audience_password(aid, None))
            return done(f"/audiences/{aid}", "pw_removed")
        pw, pw2 = str(form.get("password", "")), str(form.get("password2", ""))
        if len(pw) < 8:
            return audience_page(request, sess, aid, "Use at least 8 characters for the password.", 400)
        if pw != pw2:
            return audience_page(request, sess, aid, "The two passwords didn't match.", 400)
        h = await run_in_threadpool(_hasher.hash, pw)
        await save(lambda d: d.set_audience_password(aid, h))
        return done(f"/audiences/{aid}", "pw_set")

    @app.post("/audiences/{aid}/rotate")
    async def audience_rotate(request: Request, aid: str):
        sess, _ = await checked_form(request)
        if not sess:
            return to_login()
        if aid in cfg().audiences:
            await save(lambda d: d.rotate_slug(aid))
        return done(f"/audiences/{aid}", "rotated")

    @app.post("/audiences/{aid}/delete")
    async def audience_delete(request: Request, aid: str):
        sess, _ = await checked_form(request)
        if not sess:
            return to_login()
        await save(lambda d: d.remove_audience(aid))
        return done("/sharing", "aud_deleted")

    # -- privacy rules -----------------------------------------------------------------------

    def rule_matches_upcoming(rule: RuleCfg) -> list[dict]:
        """Upcoming events this rule catches. Shown only to the owner, in the panel."""
        c = cfg()
        g = rt.glimpse
        s, _ = week_range(week_start(g.today(), c), c)
        e = s + timedelta(weeks=RULE_PREVIEW_WEEKS)
        out = []
        for ev in g.events_between(s, e):
            if rule_matches(rule, ev):
                cal = c.calendars.get(ev.calendar_id)
                out.append(
                    {
                        "title": ev.summary or "(no title)",
                        "when": f"{fmt_day(ev.start.date())}"
                        + ("" if ev.all_day else f" {fmt_time(ev.start)}"),
                        "calendar": cal.name if cal else ev.calendar_id,
                    }
                )
        return out

    def rules_page(request, sess, form_values=None, test=None, error=None, status=200):
        c = cfg()
        rules = [{"i": i, "rule": r, "matches": rule_matches_upcoming(r)} for i, r in enumerate(c.rules)]
        values = form_values or {
            "name": "",
            "match": "",
            "fields": list(RULE_FIELDS),
            "action": "busy",
            "calendars": [],
            "enabled": True,
        }
        return page(
            request,
            "admin/rules.html",
            {
                "rules": rules,
                "form": values,
                "test": test,
                "fields": RULE_FIELDS,
                "feeds": feed_rows(),
                "weeks": RULE_PREVIEW_WEEKS,
                "error": error,
            },
            sess,
            status,
        )

    def _rule_values(form) -> dict[str, Any]:
        words = re.split(r"[\n,]+", str(form.get("match", "")))
        return {
            "name": str(form.get("name", "")).strip()[:60],
            "match": "\n".join(w.strip() for w in words if w.strip()),
            "fields": [f for f in form.getlist("field") if f in RULE_FIELDS],
            "action": "hidden" if form.get("action") == "hidden" else "busy",
            "calendars": [cid for cid in form.getlist("calendar") if cid in cfg().calendars],
            "enabled": not form.getlist("enabled") or "on" in form.getlist("enabled"),
        }

    def _rule_raw(v: dict[str, Any]) -> dict[str, Any]:
        raw: dict[str, Any] = {"match": v["match"].splitlines(), "action": v["action"]}
        if v["name"]:
            raw["name"] = v["name"]
        if v["fields"] and set(v["fields"]) != set(RULE_FIELDS):
            raw["fields"] = v["fields"]
        elif not v["fields"]:
            raw["fields"] = []  # rejected by validation with a clear message
        if v["calendars"]:
            raw["calendars"] = v["calendars"]
        if not v["enabled"]:
            raw["enabled"] = False
        return raw

    def _rule_error(v: dict[str, Any]) -> str | None:
        if not v["match"]:
            return "Add at least one word or phrase to look for."
        if not v["fields"]:
            return "Pick at least one place to look (title, notes or location)."
        return None

    @app.get("/rules", response_class=HTMLResponse)
    def rules(request: Request):
        sess = session(request)
        if not sess:
            return to_login()
        return rules_page(request, sess)

    @app.post("/rules/test", response_class=HTMLResponse)
    async def rules_test(request: Request):
        sess, form = await checked_form(request)
        if not sess:
            return to_login()
        v = _rule_values(form)
        err = _rule_error(v)
        if err:
            return rules_page(request, sess, v, error=err, status=400)
        rule = RuleCfg.model_validate({**_rule_raw(v), "enabled": True})
        return rules_page(request, sess, v, test=rule_matches_upcoming(rule))

    @app.post("/rules/add", response_class=HTMLResponse)
    async def rules_add(request: Request):
        sess, form = await checked_form(request)
        if not sess:
            return to_login()
        v = _rule_values(form)
        err = _rule_error(v)
        if err:
            return rules_page(request, sess, v, error=err, status=400)
        try:
            await save(lambda d: d.set_rule(None, _rule_raw(v)))
        except ConfigError as exc:
            return rules_page(request, sess, v, error=str(exc), status=400)
        return done("/rules", "rule_added")

    @app.post("/rules/{index}", response_class=HTMLResponse)
    async def rules_save(request: Request, index: int):
        sess, form = await checked_form(request)
        if not sess:
            return to_login()
        if not 0 <= index < len(cfg().rules):
            return RedirectResponse("/rules", status_code=303)
        v = _rule_values(form)
        err = _rule_error(v)
        if err:
            return rules_page(request, sess, error=f"Rule {index + 1}: {err}", status=400)
        try:
            await save(lambda d: d.set_rule(index, _rule_raw(v)))
        except ConfigError as exc:
            return rules_page(request, sess, error=str(exc), status=400)
        return done("/rules", "saved")

    @app.post("/rules/{index}/delete")
    async def rules_delete(request: Request, index: int):
        sess, _ = await checked_form(request)
        if not sess:
            return to_login()
        if 0 <= index < len(cfg().rules):
            await save(lambda d: d.remove_rule(index))
        return done("/rules", "rule_deleted")

    # -- people ------------------------------------------------------------------------------

    def people_page(request, sess, error=None, status=200, values=None):
        return page(
            request,
            "admin/people.html",
            {
                "rows": people_rows(),
                "form": values or {"name": "", "audience": next(iter(cfg().audiences), ""), "expires": ""},
                "error": error,
            },
            sess,
            status,
        )

    @app.get("/people", response_class=HTMLResponse)
    def people(request: Request):
        sess = session(request)
        if not sess:
            return to_login()
        return people_page(request, sess)

    def _expires(value: str) -> tuple[str | None, str | None]:
        value = value.strip()
        if not value:
            return None, None
        if not _DATE.match(value):
            return None, "Use a date like 2026-12-31 for when the link stops working."
        return value, None

    @app.post("/people/add", response_class=HTMLResponse)
    async def person_add(request: Request):
        sess, form = await checked_form(request)
        if not sess:
            return to_login()
        values = {k: str(form.get(k, "")).strip() for k in ("name", "audience", "expires")}
        expires, err = _expires(values["expires"])
        if not values["name"]:
            err = "Give them a name (only you see it, plus a hello on their page)."
        elif values["audience"] not in cfg().audiences:
            err = "Pick which audience they belong to."
        if err:
            return people_page(request, sess, err, 400, values)
        box: dict[str, str] = {}

        def mutate(d: Draft) -> None:
            box["id"] = d.add_person(values["name"][:60], values["audience"], expires)

        try:
            await save(mutate)
        except ConfigError as exc:
            return people_page(request, sess, str(exc), 400, values)
        return done(f"/people/{box['id']}", "person_added")

    def person_page(request, sess, pid, error=None, status=200):
        row = next(r for r in people_rows() if r["id"] == pid)
        return page(
            request,
            "admin/person.html",
            {
                "pid": pid,
                "row": row,
                "feed": person_feed_link(pid) if row["aud"].allow_ics else None,
                "error": error,
            },
            sess,
            status,
        )

    @app.get("/people/{pid}", response_class=HTMLResponse)
    def person(request: Request, pid: str):
        sess = session(request)
        if not sess:
            return to_login()
        if pid not in cfg().people:
            return RedirectResponse("/people", status_code=303)
        return person_page(request, sess, pid)

    @app.post("/people/{pid}", response_class=HTMLResponse)
    async def person_save(request: Request, pid: str):
        sess, form = await checked_form(request)
        if not sess:
            return to_login()
        if pid not in cfg().people:
            return RedirectResponse("/people", status_code=303)
        expires, err = _expires(str(form.get("expires", "")))
        audience = str(form.get("audience", ""))
        if audience not in cfg().audiences:
            err = "Pick which audience they belong to."
        if err:
            return person_page(request, sess, pid, err, 400)
        moved = audience != cfg().people[pid].audience

        def mutate(d: Draft) -> None:
            node = d.people[pid]
            name = str(form.get("name", "")).strip()[:60]
            if name:
                node["name"] = name
            node["audience"] = audience
            if expires:
                node["expires"] = expires
            else:
                node.pop("expires", None)
            node["enabled"] = form.get("enabled") == "on"
            note = str(form.get("note", "")).strip()[:200]
            if note:
                node["note"] = note
            else:
                node.pop("note", None)

        try:
            await save(mutate)
        except ConfigError as exc:
            return person_page(request, sess, pid, str(exc), 400)
        return done(f"/people/{pid}", "person_moved" if moved else "saved")

    @app.post("/people/{pid}/password", response_class=HTMLResponse)
    async def person_password(request: Request, pid: str):
        sess, form = await checked_form(request)
        if not sess:
            return to_login()
        if pid not in cfg().people:
            return RedirectResponse("/people", status_code=303)
        mode = str(form.get("mode", "audience"))
        if mode == "own":
            pw, pw2 = str(form.get("password", "")), str(form.get("password2", ""))
            if len(pw) < 8:
                return person_page(request, sess, pid, "Use at least 8 characters for the password.", 400)
            if pw != pw2:
                return person_page(request, sess, pid, "The two passwords didn't match.", 400)
            h = await run_in_threadpool(_hasher.hash, pw)
            await save(lambda d: d.set_person_password(pid, h))
            return done(f"/people/{pid}", "pw_set")

        def mutate(d: Draft) -> None:
            d.set_person_password(pid, None)
            d.people[pid]["use_audience_password"] = mode == "audience"

        await save(mutate)
        return done(f"/people/{pid}", "saved")

    @app.post("/people/{pid}/rotate")
    async def person_rotate(request: Request, pid: str):
        sess, _ = await checked_form(request)
        if not sess:
            return to_login()
        if pid in cfg().people:
            await save(lambda d: d.rotate_person_slug(pid))
        return done(f"/people/{pid}", "person_rotated")

    @app.post("/people/{pid}/delete")
    async def person_delete(request: Request, pid: str):
        sess, _ = await checked_form(request)
        if not sess:
            return to_login()
        await save(lambda d: d.remove_person(pid))
        rt.seen.forget(f"person:{pid}")
        rt.seen.forget(f"person:{pid}:feed")
        return done("/people", "person_deleted")

    # -- preview -------------------------------------------------------------------------------

    @app.get("/preview", response_class=HTMLResponse)
    def preview(request: Request, view_as: str = "", week: str = "", mode: str = ""):
        sess = session(request)
        if not sess:
            return to_login()
        c = cfg()
        g = rt.glimpse
        everything = AudienceCfg(
            title="Everything (only you see this)",
            slug="preview-everything-only",
            default=Level.FULL,
            views=list(VIEW_ORDER),
        )
        if view_as not in c.audiences:
            view_as = "_all" if view_as == "_all" or not c.audiences else next(iter(c.audiences))
        aud = everything if view_as == "_all" else c.audiences[view_as]
        mode = mode if mode in aud.views else aud.default_view
        day = date.fromisoformat(week) if _DATE.match(week or "") else g.today()
        today = g.today()
        tz = c.tz
        if view_as == "_all":
            lo, hi = date.min, date.max
        else:
            wlo, whi = allowed_week_bounds(aud, today, c)
            lo, hi = wlo, whi + timedelta(days=6)
        ctx: dict[str, Any] = {
            "view_as": view_as,
            "aud": aud,
            "mode": mode,
            "modes": [(m, VIEW_LABELS[m]) for m in aud.views],
            "link": public_link(aud) if view_as != "_all" else None,
            "hour_px": 40,
            "show_copies": True,
            "base": "",
            "week_link": False,
        }
        if mode == "week":
            start = week_start(day, c)
            view = build_week(c, aud, start, g.week(aud, start), today)
            ctx |= {
                "view": view,
                "anchor": start.isoformat(),
                "range_label": view.range_label,
                "prev": (start - timedelta(weeks=1)).isoformat(),
                "next": (start + timedelta(weeks=1)).isoformat(),
                "now": week_start(today, c).isoformat(),
                "is_now": start == week_start(today, c),
                "outside": not (lo <= start <= hi),
            }
        else:
            first, end = period_bounds(mode, day)
            step = {"month": 1, "quarter": 3, "year": 12}[mode]
            q0, q1 = max(first, lo), min(end, hi + timedelta(days=1)) if hi != date.max else end
            events = g.view(aud, *day_range(q0, q1, tz), tz) if q0 < q1 else []
            if mode == "month":
                view = build_month(c, aud, first, events, today, (lo, hi), tz)
                label = view.label
            else:
                view = build_heat(c, aud, first, end, mode, events, today, (lo, hi), tz)
                label = view.label
            now_first, _ = period_bounds(mode, today)
            ctx |= {
                "view": view,
                "anchor": first.isoformat(),
                "range_label": label,
                "prev": add_months(first, -step).isoformat(),
                "next": add_months(first, step).isoformat(),
                "now": now_first.isoformat(),
                "is_now": first == now_first,
                "outside": end <= lo or first > hi,
            }
        return page(request, "admin/preview.html", ctx, sess)

    # -- settings ------------------------------------------------------------------------------

    def settings_page(request, sess, error=None, status=200):
        return page(
            request,
            "admin/settings.html",
            {"zones": sorted(zoneinfo.available_timezones()), "error": error},
            sess,
            status,
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings(request: Request):
        sess = session(request)
        if not sess:
            return to_login()
        return settings_page(request, sess)

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_save(request: Request):
        sess, form = await checked_form(request)
        if not sess:
            return to_login()
        users = [u.strip() for u in str(form.get("allowed_users", "")).splitlines() if u.strip()]
        me = request.headers.get("Tailscale-User-Login", "")
        if users and me and me.lower() not in {u.lower() for u in users}:
            return settings_page(
                request, sess, f"Add your own account ({me}) or you'd lock yourself out.", 400
            )

        def mutate(d: Draft) -> None:
            r = d.raw
            r["timezone"] = str(form.get("timezone", r.get("timezone", "America/New_York")))
            r["week_starts"] = "sunday" if form.get("week_starts") == "sunday" else "monday"
            try:
                r["refresh_minutes"] = max(1, min(1440, int(str(form.get("refresh_minutes", "10")))))
            except ValueError:
                pass
            server = d.section("server")
            base = str(form.get("base_url", "")).strip().rstrip("/")
            if base:
                server["base_url"] = base
            ov = d.section("overrides")
            ov["private_tag"] = str(form.get("private_tag", "#private")).strip()
            ov["hide_tag"] = str(form.get("hide_tag", "#hide")).strip()
            ov["honor_private_flag"] = form.get("honor_private_flag") == "on"
            ov["merge_duplicates"] = form.get("merge_duplicates") == "on"
            d.section("updates")["check"] = form.get("check_updates") == "on"
            owner = str(form.get("owner_name", "")).strip()[:40]
            if owner:
                r["owner_name"] = owner
            else:
                r.pop("owner_name", None)
            d.section("admin")["allowed_tailscale_users"] = users

        try:
            await save(mutate)
        except ConfigError as exc:
            return settings_page(request, sess, str(exc), 400)
        return done("/settings", "saved")

    @app.post("/settings/password", response_class=HTMLResponse)
    async def admin_password(request: Request):
        sess, form = await checked_form(request)
        if not sess:
            return to_login()
        try:
            ok = _hasher.verify(cfg().admin.password_hash or "", str(form.get("current", "")))
        except (VerificationError, InvalidHashError):
            ok = False
        pw, pw2 = str(form.get("password", "")), str(form.get("password2", ""))
        if not ok:
            return settings_page(request, sess, "Your current admin password was wrong.", 400)
        if len(pw) < 10 or pw != pw2:
            return settings_page(
                request, sess, "New password: at least 10 characters, typed the same twice.", 400
            )
        h = await run_in_threadpool(_hasher.hash, pw)
        await save(
            lambda d: d.set_secret(d.section("admin"), "password_hash", h, "GLIMPSE_ADMIN_PASSWORD_HASH")
        )
        resp = done("/settings", "admin_pw")
        set_session(resp, sess.get("c"))  # keep this browser signed in
        return resp

    return app
