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
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .. import __version__, bookings
from ..availability import busy_blocks_for_days, busy_times, fits_rules, is_free, open_slots, request_range
from ..config import AppConfig, AudienceCfg, Level
from ..core import Glimpse, add_months, allowed_week_bounds, day_range, period_bounds, week_range, week_start
from ..notify import Note, Notifier
from ..render.ics import to_ics
from ..render.periods import build_heat, build_month
from ..render.week import DAY_NAMES, build_week, fmt_day, fmt_time
from ..render.xlsx import month_to_xlsx, week_to_xlsx
from ..requests_store import MAX_PENDING_PER_VIEWER, MeetingRequest, RequestStore
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
VIEW_LABELS = {"day": "Day", "week": "Week", "month": "Month", "quarter": "3 months", "year": "Year"}
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
    requests: RequestStore | None = None,
    notifier: Notifier | None = None,
) -> FastAPI:
    g = glimpse or Glimpse(cfg)
    seen = seen or SeenStore(None)
    reqs = requests or RequestStore(None)
    notes = notifier or Notifier(lambda: g.cfg)
    limiter = LoginLimiter()
    sent_by_ip: dict[str, list[float]] = {}
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
                        await asyncio.to_thread(refresh_school)
                    except Exception:
                        log.exception("refresh failed")
                    await asyncio.sleep(g.cfg.refresh_minutes * 60)

            task = asyncio.create_task(loop())
        yield
        if task:
            task.cancel()
        seen.flush()

    def refresh_school() -> None:
        import httpx

        from ..sources import USER_AGENT

        with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as c:
            g.school.refresh(c)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        embeddable = "frame-ancestors 'none'" not in resp.headers.get(
            "Content-Security-Policy", "frame-ancestors 'none'"
        )
        for k, v in SECURITY_HEADERS.items():
            if k == "X-Frame-Options" and embeddable:
                continue  # the widget sets its own frame-ancestors list
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
        if value == "request":
            return value
        m = re.fullmatch(r"(day|week|month|quarter|year)/(\d{4}-\d{2}-\d{2})", value or "")
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
            "owner_first": (g.cfg.owner_name or "").strip() or None,
            "refresh_minutes": g.cfg.refresh_minutes,
            "repo_url": f"https://github.com/{g.cfg.updates.repo}",
            "support_url": f"https://github.com/{g.cfg.updates.repo}#support" if g.cfg.support_link else None,
            "request_url": f"{v.base}/request" if aud.requests.enabled else None,
            "term_label": None,
            "school_days": {},
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

    # -- meeting requests and the widget ---------------------------------------------------------
    # Registered before the /{kind}/{slug} pages so /req/... and /w/... aren't taken for viewer links.

    REQ_SALT = "glimpse-request-form"
    SEND_LIMIT, SEND_WINDOW = 5, 3600.0  # requests per IP per hour

    def form_token(key: str) -> str:
        return URLSafeTimedSerializer(g.cfg.server.secret_key, salt=REQ_SALT).dumps(key)

    def form_token_ok(token: str, key: str) -> bool:
        try:
            got = URLSafeTimedSerializer(g.cfg.server.secret_key, salt=REQ_SALT).loads(
                token, max_age=6 * 3600
            )
        except BadSignature:
            return False
        return hmac.compare_digest(str(got), key)

    def send_allowed(ip: str) -> bool:
        import time as _time

        now = _time.monotonic()
        recent = [t for t in sent_by_ip.get(ip, []) if now - t < SEND_WINDOW]
        sent_by_ip[ip] = recent
        if len(sent_by_ip) > 5000:  # forget the oldest callers rather than grow forever
            for k in list(sent_by_ip)[:1000]:
                sent_by_ip.pop(k, None)
        return len(recent) < SEND_LIMIT

    def note_sent(ip: str) -> None:
        import time as _time

        sent_by_ip.setdefault(ip, []).append(_time.monotonic())

    class Asker:
        """Who a request page is for: a viewer link, or a widget (no password, busy-only)."""

        def __init__(
            self, audience_id: str, audience: AudienceCfg, base: str, key: str, person=None, person_id=None
        ):
            self.audience_id, self.audience, self.base, self.key = audience_id, audience, base, key
            self.person, self.person_id = person, person_id

    def widget_audience(wslug: str) -> tuple[str, AudienceCfg] | None:
        found = None
        for aid, aud in g.cfg.audiences.items():
            w = aud.widget
            if w.enabled and w.slug and hmac.compare_digest(w.slug.encode(), wslug.encode()):
                found = (aid, aud)
        return found

    def owner_name() -> str:
        return (g.cfg.owner_name or "").strip() or "the owner"

    def panel_url() -> str:
        n = g.cfg.notify
        if n.panel_url:
            return n.panel_url.rstrip("/")
        base = g.cfg.server.base_url.rstrip("/")
        return (
            f"{base}:8443"
            if base.startswith("https://") and base.count(":") == 1
            else "http://127.0.0.1:8001"
        )

    def status_link(r: MeetingRequest) -> str:
        return f"{g.cfg.server.base_url.rstrip('/')}/req/{r.token}"

    def when_text(start: datetime, end: datetime, tz: ZoneInfo) -> str:
        s, e = start.astimezone(tz), end.astimezone(tz)
        return f"{fmt_day(s.date())}, {fmt_time(s)} – {fmt_time(e)} · {zone_label(tz, s)}"

    def request_zone(request: Request, asker: Asker, tz_param: str = "") -> ZoneInfo:
        return (
            valid_zone(tz_param)
            or valid_zone(request.cookies.get(TZ_COOKIE))
            or valid_zone(asker.audience.timezone)
            or g.cfg.tz
        )

    def request_page(
        request: Request, asker: Asker, tz: ZoneInfo, minutes: int | None, ctx: dict, status: int = 200
    ):
        aud = asker.audience
        r = aud.requests
        minutes = minutes if minutes in r.lengths else r.lengths[0]
        now = g.now()
        days: list[tuple[date, list]] = []
        if r.slots:
            slots = open_slots(g, aud, minutes, now, reqs.holds())
            for sl in slots:
                d = sl.start.astimezone(tz).date()
                if not days or days[-1][0] != d:
                    days.append((d, []))
                days[-1][1].append(sl)
        earliest, latest = request_range(g.cfg, aud, now)
        wanted = parse_day(request.query_params.get("day", "") or str(ctx.get("values", {}).get("day", "")))
        chosen = next((d for d, _ in days if d == wanted), days[0][0] if days else None)
        base_ctx = {
            "aud": aud,
            "base": asker.base,
            "embedded": asker.key.startswith("w:"),
            "person": asker.person.name if asker.person else None,
            "owner_first": (g.cfg.owner_name or "").strip() or None,
            "owner_possessive": g.cfg.owner_possessive,
            "r": r,
            "minutes": minutes,
            "days": days,
            "chosen": chosen,
            "chosen_slots": next((sl for d, sl in days if d == chosen), []),
            "tz": tz,
            "tz_label": zone_label(tz),
            "ft": form_token(asker.key),
            "min_day": earliest.astimezone(tz).date().isoformat(),
            "max_day": (latest - timedelta(seconds=1)).astimezone(tz).date().isoformat(),
            "error": None,
            "values": {},
            "back": asker.base if not asker.key.startswith(("w:", "b:")) else None,
            "zones": sorted({*COMMON_ZONES, g.cfg.timezone, tz.key}),
        }
        base_ctx.update(ctx)
        return templates.TemplateResponse(request, "request.html", base_ctx, status_code=status)

    async def submit_request(request: Request, asker: Asker) -> Response:
        aud = asker.audience
        r = aud.requests
        form = await request.form()
        tz = request_zone(request, asker, str(form.get("tz", "")))
        ip = client_ip(request)
        values = {
            k: str(form.get(k, ""))[:1000]
            for k in ("name", "email", "note", "date", "time", "start", "mode", "day")
        }
        try:
            minutes = int(str(form.get("len", "")))
        except ValueError:
            minutes = r.lengths[0]

        def again(msg: str, status: int = 400) -> Response:
            return request_page(request, asker, tz, minutes, {"error": msg, "values": values}, status)

        if str(form.get("website", "")):  # a field people never see; bots fill it in
            return again("Something went wrong. Please try again.")
        if not form_token_ok(str(form.get("ft", "")), asker.key):
            return again("This page was open too long. Please pick a time again.")
        name = " ".join(values["name"].split())[:80]
        email = values["email"].strip()[:120]
        note = values["note"].strip()[:1000]
        if not name:
            return again("Please add your name.")
        if not re.fullmatch(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}", email):
            return again("Please add an email address so you can hear back.")
        if minutes not in r.lengths:
            return again("Please pick one of the offered meeting lengths.")
        now = g.now()
        mode = values["mode"]
        if mode == "suggest" and r.suggest:
            d, t = parse_day(values["date"]), values["time"]
            if d is None or not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", t):
                return again("Please pick a day and a time to suggest.")
            hh, mm = (int(x) for x in t.split(":"))
            start = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
            end = start + timedelta(minutes=minutes)
            earliest, latest = request_range(g.cfg, aud, now)
            if start < earliest:
                return again("That time is too soon. Please suggest a later one.")
            if end > latest:
                return again("That's further ahead than you can ask for here.")
            kind = "suggested"
        elif mode == "slot" and r.slots:
            try:
                start = datetime.fromisoformat(values["start"])
            except ValueError:
                return again("Please pick one of the open times.")
            if start.tzinfo is None:
                return again("Please pick one of the open times.")
            end = start + timedelta(minutes=minutes)
            why = fits_rules(g.cfg, aud, start, end, now)
            if why:
                return again(why)
            if not is_free(busy_times(g, start, end, reqs.holds()), start, end):
                return again("Sorry, that time was just taken. Please pick another.", 409)
            kind = "slot"
        else:
            return again("Please pick a time.")
        if not send_allowed(ip):
            return again("You've sent several requests already. Please try again later.", 429)
        if reqs.open_from(asker.key) >= MAX_PENDING_PER_VIEWER:
            return again(
                f"There are already {MAX_PENDING_PER_VIEWER} requests from this link waiting for an answer.",
                429,
            )
        try:
            item = reqs.add(
                audience=asker.audience_id,
                person=asker.person_id,
                viewer=asker.key,
                name=name,
                email=email,
                note=note,
                start=start.isoformat(),
                end=end.isoformat(),
                kind=kind,
            )
        except OverflowError:
            return again("Too many requests are waiting right now. Please try again later.", 429)
        note_sent(ip)
        own = g.cfg.tz
        who = f"{name}" + (
            f" ({asker.person.name}'s link)" if asker.person and asker.person.name != name else ""
        )
        body = f"{who} · {aud.title}\n{when_text(start, end, own)}" + (
            " (suggested time)" if kind == "suggested" else ""
        )
        if note:
            body += f"\n“{note[:300]}”"
        if aud.requests.auto_confirm and kind == "slot":
            item = bookings.confirm(g, reqs, item, "booked instantly")
            extra = f"\nAdded to {g.cfg.calendars[item.calendar].name}." if item.calendar else ""
            notes.owner(
                Note(
                    title="New meeting booked",
                    body=body + extra,
                    url=f"{panel_url()}/requests#r-{item.id}",
                    tags="white_check_mark",
                )
            )
            notes.requester(
                email,
                f"Confirmed: meeting with {owner_name()}",
                f"Hi {name},\n\nYour meeting with {owner_name()} is booked:\n{when_text(start, end, tz)}\n"
                + (f"\n{bookings.details(item, include_note=False)}\n" if item.link or item.location else "")
                + "\nThe attached file adds it to your calendar. To see it again or cancel:\n"
                f"{status_link(item)}\n",
                ics=bookings.requester_invite(g.cfg, item),
            )
            return RedirectResponse(f"/req/{item.token}?sent=1", status_code=303)
        notes.owner(Note(title="New meeting request", body=body, url=f"{panel_url()}/requests#r-{item.id}"))
        notes.requester(
            email,
            f"Your meeting request to {owner_name()}",
            f"Hi {name},\n\nYour request to meet {owner_name()} was sent:\n{when_text(start, end, tz)}\n\n"
            "You'll get an email when they answer. You can also check or cancel it here:\n"
            f"{status_link(item)}\n",
        )
        resp = RedirectResponse(f"/req/{item.token}?sent=1", status_code=303)
        return resp

    def status_page(
        request: Request, item: MeetingRequest, tz: ZoneInfo, error: str | None = None, status: int = 200
    ):
        aud = g.cfg.audiences.get(item.audience)
        ctx = {
            "item": item,
            "aud": aud,
            "tz": tz,
            "when": when_text(item.start_dt, item.end_dt, tz),
            "counter": when_text(item.counter_start_dt, item.counter_end_dt, tz)
            if item.counter_start_dt and item.counter_end_dt
            else None,
            "owner_first": (g.cfg.owner_name or "").strip() or None,
            "owner_possessive": g.cfg.owner_possessive,
            "ft": form_token(f"req:{item.id}"),
            "sent": request.query_params.get("sent") == "1",
            "emailed": notes.can_email_requesters(),
            "error": error,
            "past": item.end_dt < g.now(),
        }
        return templates.TemplateResponse(request, "request_status.html", ctx, status_code=status)

    @app.get("/req/{token}", response_class=HTMLResponse)
    def request_status(request: Request, token: str):
        item = reqs.by_token(token)
        if item is None:
            return not_found(request)
        aud = g.cfg.audiences.get(item.audience)
        tz = (
            valid_zone(request.cookies.get(TZ_COOKIE))
            or valid_zone(aud.timezone if aud else None)
            or g.cfg.tz
        )
        return status_page(request, item, tz)

    @app.post("/req/{token}", response_class=HTMLResponse)
    def request_status_action(request: Request, token: str, action: str = Form(""), ft: str = Form("")):
        item = reqs.by_token(token)
        if item is None:
            return not_found(request)
        tz = g.cfg.tz
        if not form_token_ok(ft, f"req:{item.id}"):
            return status_page(request, item, tz, "This page was open too long. Please try again.", 400)
        who = item.name
        if action == "cancel" and item.status in ("pending", "countered", "accepted"):
            bookings.cancel(g, reqs, item, "by requester")
            notes.owner(
                Note(
                    title="Meeting request cancelled",
                    body=f"{who} cancelled: {when_text(item.start_dt, item.end_dt, g.cfg.tz)}",
                    url=f"{panel_url()}/requests",
                    tags="x",
                )
            )
        elif (
            action == "accept_counter"
            and item.status == "countered"
            and item.counter_start
            and item.counter_end
        ):
            item = bookings.confirm(
                g,
                reqs,
                item,
                "new time accepted",
                start=item.counter_start,
                end=item.counter_end,
                counter_start=None,
                counter_end=None,
            )
            where = f" Added to {g.cfg.calendars[item.calendar].name}." if item.calendar else ""
            notes.owner(
                Note(
                    title="New time accepted",
                    body=f"{who} accepted {when_text(item.start_dt, item.end_dt, g.cfg.tz)}.{where}",
                    url=f"{panel_url()}/requests#r-{item.id}",
                    tags="white_check_mark",
                )
            )
        elif action == "decline_counter" and item.status == "countered":
            reqs.change(item.id, "cancelled", "new time declined", counter_start=None, counter_end=None)
            notes.owner(
                Note(
                    title="Proposed time declined",
                    body=f"{who} declined the time you proposed.",
                    url=f"{panel_url()}/requests",
                    tags="x",
                )
            )
        return RedirectResponse(f"/req/{token}", status_code=303)

    @app.get("/req/{token}/invite.ics")
    def request_invite(request: Request, token: str):
        item = reqs.by_token(token)
        if item is None or item.status != "accepted":
            return not_found(request)
        body = bookings.requester_invite(g.cfg, item)
        return Response(
            body,
            media_type="text/calendar; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="meeting.ics"'},
        )

    def widget_headers(aud: AudienceCfg) -> dict[str, str]:
        sites = " ".join(aud.widget.sites) if aud.widget.sites else "*"
        csp = SECURITY_HEADERS["Content-Security-Policy"].replace(
            "frame-ancestors 'none'", f"frame-ancestors {sites}"
        )
        return {"Content-Security-Policy": csp, "Cache-Control": "max-age=60"}

    @app.get("/w/{wslug}", response_class=HTMLResponse)
    def widget(request: Request, wslug: str, tz: str = ""):
        found = widget_audience(wslug)
        if found is None:
            return not_found(request)
        aid, aud = found
        zone = valid_zone(tz) or valid_zone(aud.timezone) or g.cfg.tz
        today = g.today_in(zone)
        hours = aud.request_hours if aud.requests.enabled else (aud.hours or None)
        days = busy_blocks_for_days(g, today, aud.widget.days, hours, zone, reqs.holds())
        if hours:
            days = [(d, parts) for d, parts in days if d.weekday() in hours.weekday_numbers] or days
        h0, h1 = (hours.start, hours.end) if hours else (time(8, 0), time(18, 0))
        ctx = {
            "aud": aud,
            "days": days,
            "today": today,
            "hours_label": f"{fmt_time(h0, True)}–{fmt_time(h1, True)}",
            "tz_label": zone_label(zone),
            "tz": zone,
            "owner_possessive": g.cfg.owner_possessive,
            "request_url": f"/w/{wslug}/request?tz={zone.key}" if aud.requests.enabled else None,
            "freshness": freshness(),
            "self_url": f"/w/{wslug}",
            "has_tz": bool(tz),
            "repo_url": f"https://github.com/{g.cfg.updates.repo}",
        }
        resp = templates.TemplateResponse(request, "widget.html", ctx)
        for k, v in widget_headers(aud).items():
            resp.headers[k] = v
        return resp

    @app.get("/w/{wslug}/request", response_class=HTMLResponse)
    def widget_request(request: Request, wslug: str, tz: str = "", len: int = 0):  # noqa: A002
        found = widget_audience(wslug)
        if found is None or not found[1].requests.enabled:
            return not_found(request)
        aid, aud = found
        asker = Asker(aid, aud, f"/w/{wslug}", f"w:{aid}")
        return request_page(request, asker, request_zone(request, asker, tz), len, {})

    @app.post("/w/{wslug}/request", response_class=HTMLResponse)
    async def widget_request_send(request: Request, wslug: str):
        found = widget_audience(wslug)
        if found is None or not found[1].requests.enabled:
            return not_found(request)
        aid, aud = found
        return await submit_request(request, Asker(aid, aud, f"/w/{wslug}", f"w:{aid}"))

    def booking_audience(bslug: str) -> tuple[str, AudienceCfg] | None:
        found = None
        for aid, aud in g.cfg.audiences.items():
            r = aud.requests
            if r.enabled and r.booking and r.booking_slug:
                if hmac.compare_digest(r.booking_slug.encode(), bslug.encode()):
                    found = (aid, aud)
        return found

    @app.get("/b/{bslug}", response_class=HTMLResponse)
    def booking_page(request: Request, bslug: str, tz: str = "", len: int = 0):  # noqa: A002
        found = booking_audience(bslug)
        if found is None:
            return not_found(request)
        aid, aud = found
        asker = Asker(aid, aud, f"/b/{bslug}", f"b:{aid}")
        return request_page(request, asker, request_zone(request, asker, tz), len, {})

    @app.post("/b/{bslug}", response_class=HTMLResponse)
    async def booking_send(request: Request, bslug: str):
        found = booking_audience(bslug)
        if found is None:
            return not_found(request)
        aid, aud = found
        return await submit_request(request, Asker(aid, aud, f"/b/{bslug}", f"b:{aid}"))

    @app.get("/cal/{name}")
    def shared_calendar(request: Request, name: str):
        """A Glimpse calendar's own subscription link (full details, as its owner chose)."""
        slug = name.removesuffix(".ics")
        found = None
        for cal in g.cfg.calendars.values():
            if cal.local and cal.share and cal.share_slug:
                if hmac.compare_digest(cal.share_slug.encode(), slug.encode()):
                    found = cal
        if found is None:
            return not_found(request)
        try:
            body = Path(found.url).read_text(encoding="utf-8")
        except OSError:
            return not_found(request)
        return Response(body, media_type="text/calendar; charset=utf-8")

    def viewer_asker(request: Request, kind: str, slug: str) -> Asker | Response:
        v = find(request, kind, slug)
        if not isinstance(v, Viewer):
            return v
        if not v.audience.requests.enabled:
            return not_found(request)
        if not authed(request, v):
            return to_login(v, "request")
        return Asker(v.audience_id, v.audience, v.base, v.key, v.person, v.person_id)

    @app.get("/{kind}/{slug}/request", response_class=HTMLResponse)
    def viewer_request(request: Request, kind: str, slug: str, tz: str = "", len: int = 0):  # noqa: A002
        asker = viewer_asker(request, kind, slug)
        if not isinstance(asker, Asker):
            return asker
        return request_page(request, asker, request_zone(request, asker, tz), len, {})

    @app.post("/{kind}/{slug}/request", response_class=HTMLResponse)
    async def viewer_request_send(request: Request, kind: str, slug: str):
        asker = viewer_asker(request, kind, slug)
        if not isinstance(asker, Asker):
            return asker
        return await submit_request(request, asker)

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
        if view == "day":
            return day_view(request, v, d, zone)
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
            "excel_label": "Download this week (Excel)",
            "hour_px": 44,
            **g.school.viewer_context(start, start + timedelta(days=7), start),
        }
        return templates.TemplateResponse(request, "week.html", ctx)

    def day_view(request: Request, v: Viewer, d: date, tz: ZoneInfo) -> Response:
        aud, c = v.audience, g.cfg
        lo, hi = allowed_days(v)
        if not lo <= d <= hi:
            return RedirectResponse(f"{v.base}/day/{lo if d < lo else hi}", status_code=303)
        today = g.today_in(tz)
        events = g.view(aud, *day_range(d, d + timedelta(days=1), tz), tz)
        view = build_week(c, aud, d, events, today, tz, n_days=1)
        prev_d, next_d = d - timedelta(days=1), d + timedelta(days=1)
        note = None
        if aud.hours and tz.key == c.timezone and d.weekday() not in aud.hours.weekday_numbers:
            days = ", ".join(DAY_NAMES[i] for i in sorted(aud.hours.weekday_numbers))
            note = f"Only {days} are shared here."
        ctx = common_ctx(request, v, tz, "day") | {
            "view": view,
            "range_label": view.range_label,
            "prev": f"{v.base}/day/{prev_d}" if prev_d >= lo else None,
            "next": f"{v.base}/day/{next_d}" if next_d <= hi else None,
            "now_link": None if d == today else f"{v.base}/day/{today}",
            "now_label": "Today",
            "excel_url": f"{v.base}/excel/{week_start(d, c)}"
            if aud.allow_excel and "week" in aud.views
            else None,
            "excel_label": "Download this week (Excel)",
            "day_note": note,
            "hour_px": 52,
            **g.school.viewer_context(d, d + timedelta(days=1), d),
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
            school = g.school.viewer_context(first - timedelta(days=7), end + timedelta(days=7), first)
            ctx |= {
                "school_days": school["school_days"],
                "view": view,
                "range_label": view.label,
                "excel_url": f"{v.base}/excel-month/{first}" if aud.allow_excel else None,
                "excel_label": "Download this month (Excel)",
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
        if aud.feed_skip:
            # Calendars they already have in their own calendar app (e.g. a shared iCloud
            # calendar) would show twice there, so the subscription leaves them out.
            hidden = dict.fromkeys(aud.feed_skip, Level.HIDDEN)
            aud = aud.model_copy(update={"calendars": {**aud.calendars, **hidden}})
        body = to_ics(aud.title, g.view(aud, s, e), g.cfg.refresh_minutes)
        return Response(body, media_type="text/calendar; charset=utf-8")

    # Registered last so the fixed paths above (login, excel, feed) win.
    app.get("/{kind}/{slug}/{view}/{day}", response_class=HTMLResponse)(period_page)
    return app
