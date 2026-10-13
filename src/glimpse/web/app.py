"""The public web app: schedule pages, login, Excel and ICS downloads.

Every page belongs to a *viewer*: an audience's shared link (``/a/<slug>``) or
one person's own link (``/p/<slug>``). Both show the audience's filtered view.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import re
import zoneinfo
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..config import AppConfig
from ..core import Glimpse, add_months, allowed_week_bounds, day_range, period_bounds, week_range, week_start
from ..render.ics import to_ics
from ..render.periods import build_heat, build_month
from ..render.week import build_week, fmt_day, fmt_time
from ..render.xlsx import month_to_xlsx, week_to_xlsx
from .auth import (
    SESSION_DAYS,
    LoginLimiter,
    cookie_name,
    feed_token,
    make_session,
    session_valid,
    verify_password,
)
from .state import SeenStore
from .viewer import Viewer, resolve

log = logging.getLogger("glimpse.web")
HERE = Path(__file__).parent
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TZ_COOKIE = "glimpse_tz"
FEED_WEEKS_AHEAD, FEED_WEEKS_BEHIND = 104, 52
VIEW_LABELS = {"week": "Week", "month": "Month", "quarter": "3 months", "year": "Year"}
COMMON_ZONES = [
    "Pacific/Honolulu",
    "America/Anchorage",
    "America/Los_Angeles",
    "America/Denver",
    "America/Phoenix",
    "America/Chicago",
    "America/New_York",
    "America/Halifax",
    "America/Sao_Paulo",
    "Atlantic/Reykjavik",
    "Europe/London",
    "Europe/Dublin",
    "Europe/Lisbon",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Madrid",
    "Europe/Rome",
    "Europe/Amsterdam",
    "Europe/Stockholm",
    "Europe/Athens",
    "Europe/Istanbul",
    "Africa/Lagos",
    "Africa/Cairo",
    "Africa/Johannesburg",
    "Asia/Dubai",
    "Asia/Karachi",
    "Asia/Kolkata",
    "Asia/Dhaka",
    "Asia/Bangkok",
    "Asia/Singapore",
    "Asia/Shanghai",
    "Asia/Hong_Kong",
    "Asia/Taipei",
    "Asia/Seoul",
    "Asia/Tokyo",
    "Australia/Perth",
    "Australia/Brisbane",
    "Australia/Sydney",
    "Pacific/Auckland",
    "UTC",
]

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; script-src 'self'; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",  # links carry secret slugs
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    "Cache-Control": "no-store",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def valid_zone(name: str | None) -> ZoneInfo | None:
    if not name or name not in zoneinfo.available_timezones():
        return None
    return ZoneInfo(name)


def zone_label(tz: ZoneInfo, when: datetime | None = None) -> str:
    """'Paris (CEST)' style label."""
    city = tz.key.split("/")[-1].replace("_", " ")
    abbr = (when or datetime.now(UTC)).astimezone(tz).tzname() or ""
    return f"{city} ({abbr})" if abbr and abbr != city and not abbr.startswith(("+", "-")) else city


def create_app(
    cfg: AppConfig,
    glimpse: Glimpse | None = None,
    background_refresh: bool = True,
    seen: SeenStore | None = None,
) -> FastAPI:
    g = glimpse or Glimpse(cfg)
    seen = seen or SeenStore(None)
    limiter = LoginLimiter()
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals.update(version=__version__, fmt_time=fmt_time, fmt_day=fmt_day)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        task = None
        if background_refresh:

            async def loop() -> None:
                while True:
                    try:
                        await asyncio.to_thread(g.cache.refresh_all)
                    except Exception:
                        log.exception("refresh failed")
                    await asyncio.sleep(g.cfg.refresh_minutes * 60)

            task = asyncio.create_task(loop())
        yield
        if task:
            task.cancel()
        seen.flush()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp

    # -- helpers ----------------------------------------------------------------

    def message(request: Request, heading: str, body: str, status: int) -> Response:
        return templates.TemplateResponse(
            request, "message.html", {"heading": heading, "body": body}, status_code=status
        )

    def not_found(request: Request) -> Response:
        return message(request, "Not found", "There's nothing here.", 404)

    def find(request: Request, kind: str, slug: str) -> Viewer | Response:
        found = resolve(g.cfg, kind, slug, g.today())
        if found == "expired":
            who = g.cfg.owner_name or "the person who sent it"
            return message(
                request, "This link has expired", f"Ask {who} for a new link if you still need it.", 410
            )
        if found is None:
            return not_found(request)
        return found

    def authed(request: Request, v: Viewer) -> bool:
        if not v.password_hash:
            return True
        return session_valid(g.cfg, v, request.cookies.get(cookie_name(v.key)))

    def to_login(v: Viewer, nxt: str) -> RedirectResponse:
        return RedirectResponse(f"{v.base}/login?{urlencode({'next': nxt})}", status_code=303)

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def parse_day(value: str) -> date | None:
        if not _DATE.match(value):
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    def safe_next(v: Viewer, value: str) -> str:
        """Only paths inside this viewer's own pages."""
        m = re.fullmatch(r"(week|month|quarter|year)/(\d{4}-\d{2}-\d{2})", value or "")
        if m and parse_day(m.group(2)):
            return value
        return f"week/{value}" if parse_day(value or "") else ""

    def display_zone(request: Request, v: Viewer) -> ZoneInfo:
        return valid_zone(request.cookies.get(TZ_COOKIE)) or valid_zone(v.audience.timezone) or g.cfg.tz

    def freshness() -> str:
        times = [st.last_success for st in g.cache.states.values() if st.last_success]
        if not times:
            return ""
        oldest = min(times).astimezone(g.cfg.tz)
        stale = datetime.now(UTC) - min(times) > timedelta(minutes=g.cfg.refresh_minutes * 6)
        label = f"Updated {fmt_day(oldest.date())}, {fmt_time(oldest)}"
        return label + (" · some calendars may be out of date" if stale else "")

    def allowed_days(v: Viewer) -> tuple[date, date]:
        lo, hi = allowed_week_bounds(v.audience, g.today(), g.cfg)
        return lo, hi + timedelta(days=6)

    def zone_ctx(request: Request, v: Viewer, tz: ZoneInfo) -> dict:
        own = g.cfg.tz
        zones = sorted(
            {*COMMON_ZONES, own.key, tz.key, *([v.audience.timezone] if v.audience.timezone else [])}
        )
        return {
            "tz": tz,
            "tz_label": zone_label(tz),
            "owner_tz": own,
            "owner_label": g.cfg.owner_label,
            "owner_zone_label": zone_label(own),
            "is_owner_zone": tz.key == own.key,
            "zones": zones,
            "here": request.url.path,
        }

    def common_ctx(request: Request, v: Viewer, tz: ZoneInfo, view: str) -> dict:
        aud = v.audience
        today = g.today_in(tz)
        return {
            "aud": aud,
            "base": v.base,
            "views": [(name, VIEW_LABELS[name]) for name in aud.views],
            "current_view": view,
            "today": today.isoformat(),
            "excel": aud.allow_excel,
            "ics_url": f"{v.base}/feed/{feed_token(g.cfg, v)}.ics" if aud.allow_ics else None,
            "has_password": bool(v.password_hash),
            "freshness": freshness(),
            "person": v.person.name if v.person else None,
            "owner_possessive": g.cfg.owner_possessive,
            **zone_ctx(request, v, tz),
        }

    # -- routes -------------------------------------------------------------------

    @app.get("/healthz")
    def healthz() -> dict:
        states = g.cache.states.values()
        return {
            "ok": True,
            "feeds_ok": sum(1 for s in states if s.text and not s.last_error),
            "feeds_total": len(g.cache.states),
        }

    @app.get("/", response_class=HTMLResponse)
    def root(request: Request):
        return not_found(request)

    @app.get("/{kind}/{slug}")
    def home(request: Request, kind: str, slug: str, tz: str = ""):
        v = find(request, kind, slug)
        if not isinstance(v, Viewer):
            return v
        resp = RedirectResponse(
            f"{v.base}/{v.audience.default_view}/{g.today_in(display_zone(request, v))}", 303
        )
        if tz:
            set_zone(resp, v, tz)
        return resp

    def set_zone(resp: Response, v: Viewer, name: str) -> None:
        if name == "owner":
            name = g.cfg.timezone
        if valid_zone(name):
            resp.set_cookie(
                TZ_COOKIE,
                name,
                max_age=365 * 86400,
                path=v.base,
                secure=g.cfg.server.secure_cookies,
                samesite="lax",
            )

    def period_page(request: Request, kind: str, slug: str, view: str, day: str, tz: str = ""):
        v = find(request, kind, slug)
        if not isinstance(v, Viewer):
            return v
        d = parse_day(day)
        aud = v.audience
        if view not in VIEW_LABELS or d is None or view not in aud.views:
            return not_found(request)
        if tz:  # zone switch: remember it, then show the same page without the parameter
            resp = RedirectResponse(request.url.path, status_code=303)
            set_zone(resp, v, tz)
            return resp
        if not authed(request, v):
            return to_login(v, f"{view}/{day}")
        zone = display_zone(request, v)
        seen.touch(v.seen_key)
        if view == "week":
            return week_view(request, v, d, zone)
        return period_view(request, v, view, d, zone)

    def week_view(request: Request, v: Viewer, d: date, tz: ZoneInfo) -> Response:
        aud, c = v.audience, g.cfg
        start = week_start(d, c)
        lo, hi = allowed_week_bounds(aud, g.today(), c)
        if start < lo or start > hi:
            clamped = lo if start < lo else hi
            return RedirectResponse(f"{v.base}/week/{clamped.isoformat()}", status_code=303)
        if start != d:
            return RedirectResponse(f"{v.base}/week/{start.isoformat()}", status_code=303)
        today = g.today_in(tz)
        view = build_week(c, aud, start, g.week(aud, start, tz), today, tz)
        prev_w, next_w = start - timedelta(weeks=1), start + timedelta(weeks=1)
        this = week_start(today, c)
        ctx = common_ctx(request, v, tz, "week") | {
            "view": view,
            "range_label": view.range_label,
            "prev": f"{v.base}/week/{prev_w}" if prev_w >= lo else None,
            "next": f"{v.base}/week/{next_w}" if next_w <= hi else None,
            "now_link": None if start == this else f"{v.base}/week/{this}",
            "now_label": "This week",
            "excel_url": f"{v.base}/excel/{start}" if aud.allow_excel else None,
            "hour_px": 44,
        }
        return templates.TemplateResponse(request, "week.html", ctx)

    def period_view(request: Request, v: Viewer, kind: str, d: date, tz: ZoneInfo) -> Response:
        aud, c = v.audience, g.cfg
        first, end = period_bounds(kind, d)
        if first != d:
            return RedirectResponse(f"{v.base}/{kind}/{first}", status_code=303)
        lo, hi = allowed_days(v)
        step = {"month": 1, "quarter": 3, "year": 12}[kind]
        if end <= lo or first > hi:  # nothing shareable in this period: go to the nearest one
            target = lo if end <= lo else hi
            nearest, _ = period_bounds(kind, target)
            return RedirectResponse(f"{v.base}/{kind}/{nearest}", status_code=303)
        # Only fetch what this audience may see.
        q0, q1 = max(first, lo), min(end, hi + timedelta(days=1))
        s, e = day_range(q0, q1, tz)
        events = g.view(aud, s, e, tz)
        today = g.today_in(tz)
        prev_first, next_first = add_months(first, -step), add_months(first, step)
        this_first, _ = period_bounds(kind, today)
        ctx = common_ctx(request, v, tz, kind) | {
            "prev": f"{v.base}/{kind}/{prev_first}" if first > lo else None,
            "next": f"{v.base}/{kind}/{next_first}" if next_first <= hi else None,
            "now_link": None if first == this_first else f"{v.base}/{kind}/{this_first}",
            "now_label": {"month": "This month", "quarter": "Now", "year": "This year"}[kind],
            "week_link": "week" in aud.views,
            "week_lo": week_start(lo, c),
            "week_hi": hi,
        }
        if kind == "month":
            view = build_month(c, aud, first, events, today, (lo, hi), tz)
            ctx |= {
                "view": view,
                "range_label": view.label,
                "excel_url": f"{v.base}/excel-month/{first}" if aud.allow_excel else None,
            }
            return templates.TemplateResponse(request, "month.html", ctx)
        view = build_heat(c, aud, first, end, kind, events, today, (lo, hi), tz)
        ctx |= {"view": view, "range_label": view.label, "excel_url": None}
        return templates.TemplateResponse(request, "heat.html", ctx)

    @app.get("/{kind}/{slug}/login", response_class=HTMLResponse)
    def login_form(request: Request, kind: str, slug: str, next: str = ""):  # noqa: A002
        v = find(request, kind, slug)
        if not isinstance(v, Viewer):
            return v
        nxt = safe_next(v, next)
        if authed(request, v):
            return RedirectResponse(f"{v.base}/{nxt}" if nxt else v.base, status_code=303)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"aud": v.audience, "base": v.base, "next": nxt, "error": None, "person": v.person},
        )

    @app.post("/{kind}/{slug}/login", response_class=HTMLResponse)
    def login_submit(
        request: Request,
        kind: str,
        slug: str,
        password: str = Form(""),
        next: str = Form(""),  # noqa: A002
    ):
        v = find(request, kind, slug)
        if not isinstance(v, Viewer):
            return v
        nxt = safe_next(v, next)
        ip = client_ip(request)
        ctx = {"aud": v.audience, "base": v.base, "next": nxt, "person": v.person}

        def locked() -> Response:
            mins = max(1, round(limiter.retry_after(v.key, ip) / 60))
            ctx["error"] = (
                f"Too many attempts. Please wait about {mins} minute{'s' if mins != 1 else ''} and try again."
            )
            return templates.TemplateResponse(request, "login.html", ctx, status_code=429)

        if limiter.blocked(v.key, ip):
            return locked()
        if not verify_password(v, password):
            limiter.failed(v.key, ip)
            if limiter.blocked(v.key, ip):
                return locked()
            left = limiter.remaining(v.key, ip)
            ctx["error"] = "That password didn't work." + (
                f" {left} more tr{'y' if left == 1 else 'ies'} before a 15-minute wait." if left <= 2 else ""
            )
            return templates.TemplateResponse(request, "login.html", ctx, status_code=401)
        resp = RedirectResponse(f"{v.base}/{nxt}" if nxt else v.base, status_code=303)
        resp.set_cookie(
            cookie_name(v.key),
            make_session(g.cfg, v),
            max_age=SESSION_DAYS * 86400,
            path=v.base,
            httponly=True,
            secure=g.cfg.server.secure_cookies,
            samesite="lax",
        )
        return resp

    @app.get("/{kind}/{slug}/logout")
    def logout(request: Request, kind: str, slug: str):
        v = find(request, kind, slug)
        if not isinstance(v, Viewer):
            return v
        resp = RedirectResponse(f"{v.base}/login", status_code=303)
        resp.delete_cookie(cookie_name(v.key), path=v.base)
        return resp

    def xlsx_response(data: bytes, name: str) -> Response:
        return Response(
            data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.get("/{kind}/{slug}/excel/{day}")
    def excel(request: Request, kind: str, slug: str, day: str):
        v = find(request, kind, slug)
        if not isinstance(v, Viewer):
            return v
        d = parse_day(day)
        aud = v.audience
        if d is None or not aud.allow_excel or "week" not in aud.views:
            return not_found(request)
        if not authed(request, v):
            return to_login(v, f"week/{day}")
        tz = display_zone(request, v)
        start = week_start(d, g.cfg)
        lo, hi = allowed_week_bounds(aud, g.today(), g.cfg)
        if not lo <= start <= hi:
            return not_found(request)
        view = build_week(g.cfg, aud, start, g.week(aud, start, tz), g.today_in(tz), tz)
        return xlsx_response(week_to_xlsx(g.cfg, view), f"schedule-{start.isoformat()}.xlsx")

    @app.get("/{kind}/{slug}/excel-month/{day}")
    def excel_month(request: Request, kind: str, slug: str, day: str):
        v = find(request, kind, slug)
        if not isinstance(v, Viewer):
            return v
        d = parse_day(day)
        aud = v.audience
        if d is None or not aud.allow_excel or "month" not in aud.views:
            return not_found(request)
        if not authed(request, v):
            return to_login(v, f"month/{day}")
        tz = display_zone(request, v)
        first, end = period_bounds("month", d)
        lo, hi = allowed_days(v)
        if end <= lo or first > hi:
            return not_found(request)
        s, e = day_range(max(first, lo), min(end, hi + timedelta(days=1)), tz)
        view = build_month(g.cfg, aud, first, g.view(aud, s, e, tz), g.today_in(tz), (lo, hi), tz, limit=None)
        return xlsx_response(month_to_xlsx(g.cfg, view), f"schedule-{first.strftime('%Y-%m')}.xlsx")

    @app.get("/{kind}/{slug}/feed/{token}.ics")
    def feed(request: Request, kind: str, slug: str, token: str):
        v = find(request, kind, slug)
        if not isinstance(v, Viewer):
            return v if v.status_code != 410 else not_found(request)
        aud = v.audience
        if not aud.allow_ics or not hmac.compare_digest(token, feed_token(g.cfg, v)):
            return not_found(request)
        seen.touch(v.seen_key + ":feed")
        lo, hi = allowed_week_bounds(aud, g.today(), g.cfg)
        this = week_start(g.today(), g.cfg)
        # Calendar apps re-download the whole feed often: keep it to about two years ahead, one back.
        lo, hi = (
            max(lo, this - timedelta(weeks=FEED_WEEKS_BEHIND)),
            min(hi, this + timedelta(weeks=FEED_WEEKS_AHEAD)),
        )
        s, _ = week_range(lo, g.cfg)
        _, e = week_range(hi, g.cfg)
        body = to_ics(aud.title, g.view(aud, s, e), g.cfg.refresh_minutes)
        return Response(body, media_type="text/calendar; charset=utf-8")

    # Registered last so the fixed paths above (login, excel, feed) win.
    app.get("/{kind}/{slug}/{view}/{day}", response_class=HTMLResponse)(period_page)
    return app
