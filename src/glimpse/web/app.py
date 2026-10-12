"""The FastAPI web app: week pages, login, Excel and ICS downloads."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..config import AppConfig, AudienceCfg
from ..core import Glimpse, allowed_week_bounds, week_range, week_start
from ..render.ics import to_ics
from ..render.week import build_week, fmt_day, fmt_time
from ..render.xlsx import week_to_xlsx
from .auth import (
    SESSION_DAYS,
    LoginLimiter,
    cookie_name,
    feed_token,
    make_session,
    session_valid,
    verify_password,
)

log = logging.getLogger("glimpse.web")
HERE = Path(__file__).parent
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",  # links carry secret slugs
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    "Cache-Control": "no-store",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def create_app(cfg: AppConfig, glimpse: Glimpse | None = None, background_refresh: bool = True) -> FastAPI:
    g = glimpse or Glimpse(cfg)
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
                    await asyncio.sleep(cfg.refresh_minutes * 60)

            task = asyncio.create_task(loop())
        yield
        if task:
            task.cancel()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        return resp

    # -- helpers ----------------------------------------------------------------

    def not_found(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "message.html",
            {"heading": "Not found", "body": "There's nothing here."},
            status_code=404,
        )

    def lookup(slug: str) -> tuple[str, AudienceCfg] | None:
        return g.audience_by_slug(slug)

    def authed(request: Request, aid: str, aud: AudienceCfg) -> bool:
        if not aud.password_hash:
            return True
        return session_valid(cfg, aid, request.cookies.get(cookie_name(aid)))

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def parse_day(value: str) -> date | None:
        if not _DATE.match(value):
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    def freshness() -> str:
        times = [st.last_success for st in g.cache.states.values() if st.last_success]
        if not times:
            return ""
        oldest = min(times).astimezone(cfg.tz)
        stale = datetime.now(UTC) - min(times) > timedelta(minutes=cfg.refresh_minutes * 6)
        label = f"Updated {fmt_day(oldest.date())}, {fmt_time(oldest)}"
        return label + (" · some calendars may be out of date" if stale else "")

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

    @app.get("/a/{slug}")
    def audience_home(request: Request, slug: str):
        found = lookup(slug)
        if not found:
            return not_found(request)
        start = week_start(g.today(), cfg)
        return RedirectResponse(f"/a/{slug}/week/{start.isoformat()}", status_code=303)

    @app.get("/a/{slug}/week/{day}", response_class=HTMLResponse)
    def week_page(request: Request, slug: str, day: str):
        found = lookup(slug)
        d = parse_day(day)
        if not found or d is None:
            return not_found(request)
        aid, aud = found
        if not authed(request, aid, aud):
            return RedirectResponse(f"/a/{slug}/login?next={day}", status_code=303)
        start = week_start(d, cfg)
        lo, hi = allowed_week_bounds(aud, g.today(), cfg)
        if start < lo or start > hi:
            clamped = lo if start < lo else hi
            return RedirectResponse(f"/a/{slug}/week/{clamped.isoformat()}", status_code=303)
        if start != d:
            return RedirectResponse(f"/a/{slug}/week/{start.isoformat()}", status_code=303)

        view = build_week(cfg, aud, start, g.week(aud, start), g.today())
        prev_w, next_w = start - timedelta(weeks=1), start + timedelta(weeks=1)
        ctx = {
            "aud": aud,
            "slug": slug,
            "view": view,
            "prev": prev_w.isoformat() if prev_w >= lo else None,
            "next": next_w.isoformat() if next_w <= hi else None,
            "this_week": week_start(g.today(), cfg).isoformat(),
            "is_this_week": start == week_start(g.today(), cfg),
            "excel": aud.allow_excel,
            "ics_url": f"/a/{slug}/feed/{feed_token(cfg, aid)}.ics" if aud.allow_ics else None,
            "has_password": bool(aud.password_hash),
            "freshness": freshness(),
            "hour_px": 44,
        }
        return templates.TemplateResponse(request, "week.html", ctx)

    @app.get("/a/{slug}/login", response_class=HTMLResponse)
    def login_form(request: Request, slug: str, next: str = ""):  # noqa: A002
        found = lookup(slug)
        if not found:
            return not_found(request)
        aid, aud = found
        nxt = next if parse_day(next) else ""
        if authed(request, aid, aud):
            return RedirectResponse(f"/a/{slug}/week/{nxt}" if nxt else f"/a/{slug}", status_code=303)
        return templates.TemplateResponse(
            request, "login.html", {"aud": aud, "slug": slug, "next": nxt, "error": None}
        )

    @app.post("/a/{slug}/login", response_class=HTMLResponse)
    def login_submit(request: Request, slug: str, password: str = Form(""), next: str = Form("")):  # noqa: A002
        found = lookup(slug)
        if not found:
            return not_found(request)
        aid, aud = found
        nxt = next if parse_day(next) else ""
        ip = client_ip(request)
        if limiter.blocked(aid, ip):
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "aud": aud,
                    "slug": slug,
                    "next": nxt,
                    "error": "Too many attempts. Please wait a few minutes and try again.",
                },
                status_code=429,
            )
        if not verify_password(aud, password):
            limiter.failed(aid, ip)
            return templates.TemplateResponse(
                request,
                "login.html",
                {"aud": aud, "slug": slug, "next": nxt, "error": "That password didn't work."},
                status_code=401,
            )
        resp = RedirectResponse(f"/a/{slug}/week/{nxt}" if nxt else f"/a/{slug}", status_code=303)
        resp.set_cookie(
            cookie_name(aid),
            make_session(cfg, aid),
            max_age=SESSION_DAYS * 86400,
            path=f"/a/{slug}",
            httponly=True,
            secure=cfg.server.secure_cookies,
            samesite="lax",
        )
        return resp

    @app.get("/a/{slug}/logout")
    def logout(request: Request, slug: str):
        found = lookup(slug)
        if not found:
            return not_found(request)
        aid, _ = found
        resp = RedirectResponse(f"/a/{slug}/login", status_code=303)
        resp.delete_cookie(cookie_name(aid), path=f"/a/{slug}")
        return resp

    @app.get("/a/{slug}/excel/{day}")
    def excel(request: Request, slug: str, day: str):
        found = lookup(slug)
        d = parse_day(day)
        if not found or d is None:
            return not_found(request)
        aid, aud = found
        if not aud.allow_excel:
            return not_found(request)
        if not authed(request, aid, aud):
            return RedirectResponse(f"/a/{slug}/login?next={day}", status_code=303)
        start = week_start(d, cfg)
        lo, hi = allowed_week_bounds(aud, g.today(), cfg)
        if not lo <= start <= hi:
            return not_found(request)
        view = build_week(cfg, aud, start, g.week(aud, start), g.today())
        data = week_to_xlsx(cfg, view)
        return Response(
            data,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="schedule-{start.isoformat()}.xlsx"'},
        )

    @app.get("/a/{slug}/feed/{token}.ics")
    def feed(request: Request, slug: str, token: str):
        import hmac

        found = lookup(slug)
        if not found:
            return not_found(request)
        aid, aud = found
        if not aud.allow_ics or not hmac.compare_digest(token, feed_token(cfg, aid)):
            return not_found(request)
        lo, hi = allowed_week_bounds(aud, g.today(), cfg)
        s, _ = week_range(lo, cfg)
        _, e = week_range(hi, cfg)
        body = to_ics(aud.title, g.view(aud, s, e), cfg.refresh_minutes)
        return Response(body, media_type="text/calendar; charset=utf-8")

    return app
