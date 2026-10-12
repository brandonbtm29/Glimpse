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
from starlette.concurrency import run_in_threadpool

from .. import __version__
from ..config import WEEKDAYS, AppConfig, AudienceCfg, ConfigError, Level
from ..core import allowed_week_bounds, week_range, week_start
from ..events import parse_events
from ..render.week import PALETTE_SIZE, build_week, fmt_day, fmt_time
from ..sources import USER_AGENT, normalize_url
from ..store import Draft, _env_name
from .auth import LoginLimiter, feed_token
from .runtime import Runtime

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
}

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
        mask_url=mask_url,
        provider_of=provider_of,
    )

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def cfg() -> AppConfig:
        return rt.cfg

    # -- access control ----------------------------------------------------------------

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
                }
            )
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
        return page(
            request,
            "admin/overview.html",
            {"feeds": feed_rows(), "auds": auds, "local_base": local_base},
            sess,
        )

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
        return {k: str(form.get(k, "")).strip() for k in ("name", "url", "label", "color")}

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
            await save(lambda d: d.add_calendar(v["name"], v["url"], v["label"] or None, v["color"] or None))
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
        used_by = [(aud.title, cfg().level_for(aud, cid).value) for aud in cfg().audiences.values()]
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

    # -- sharing matrix --------------------------------------------------------------------

    @app.get("/sharing", response_class=HTMLResponse)
    def sharing(request: Request):
        sess = session(request)
        if not sess:
            return to_login()
        return page(request, "admin/sharing.html", {"feeds": feed_rows(), "error": None}, sess)

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

        try:
            await save(mutate)
        except ConfigError as exc:
            return page(request, "admin/sharing.html", {"feeds": feed_rows(), "error": str(exc)}, sess, 400)
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
                "aud": aud,
                "link": public_link(aud),
                "feed": feed_link(aid) if aud.allow_ics else None,
                "feeds": feed_rows(),
                "range": f"{fmt_day(lo)} to {fmt_day(hi + timedelta(days=6))}",
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

        def num(name: str, dflt: int) -> int:
            try:
                return max(0, min(52, int(str(form.get(name, dflt)))))
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
            for flag in ("show_all_day_busy", "allow_excel", "allow_ics"):
                node[flag] = form.get(flag) == "on"

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

    # -- preview -------------------------------------------------------------------------------

    @app.get("/preview", response_class=HTMLResponse)
    def preview(request: Request, view_as: str = "", week: str = ""):
        sess = session(request)
        if not sess:
            return to_login()
        c = cfg()
        g = rt.glimpse
        everything = AudienceCfg(
            title="Everything (only you see this)", slug="preview-everything-only", default=Level.FULL
        )
        if view_as not in c.audiences:
            view_as = "_all" if view_as == "_all" or not c.audiences else next(iter(c.audiences))
        aud = everything if view_as == "_all" else c.audiences[view_as]
        day = date.fromisoformat(week) if _DATE.match(week or "") else g.today()
        start = week_start(day, c)
        outside = False
        if view_as != "_all":
            lo, hi = allowed_week_bounds(aud, g.today(), c)
            outside = not lo <= start <= hi
        view = build_week(c, aud, start, g.week(aud, start), g.today())
        return page(
            request,
            "admin/preview.html",
            {
                "view": view,
                "view_as": view_as,
                "aud": aud,
                "prev": (start - timedelta(weeks=1)).isoformat(),
                "next": (start + timedelta(weeks=1)).isoformat(),
                "this_week": week_start(g.today(), c).isoformat(),
                "is_this_week": start == week_start(g.today(), c),
                "outside": outside,
                "link": public_link(aud) if view_as != "_all" else None,
                "hour_px": 40,
            },
            sess,
        )

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
