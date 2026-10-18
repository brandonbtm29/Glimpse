"""When you're free: busy times across every calendar, and open slots for meeting requests.

Availability looks at *all* your calendars (except ones you tell it to ignore), not
just what an audience may see, because a hidden event still means you can't meet.
Only start and end times leave this module: never a title or a calendar name.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import AppConfig, AudienceCfg, HoursCfg


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime


def merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def busy_times(
    glimpse,
    start: datetime,
    end: datetime,
    holds: list[tuple[datetime, datetime]] | None = None,
) -> list[tuple[datetime, datetime]]:
    """Merged busy intervals from every calendar, plus held/accepted requests.

    Events marked "show as free" don't count; nor do calendars on the ignore list.
    All-day events (usually birthdays, holidays and reminders) count only when the
    owner switches that on, and then still not when they're marked free.
    """
    cfg: AppConfig = glimpse.cfg
    ignore = set(cfg.availability.ignore_calendars)
    pad = timedelta(minutes=cfg.availability.buffer_minutes)
    out = []
    own = cfg.tz
    lo = datetime.combine((start - timedelta(days=1)).astimezone(own).date(), time.min, tzinfo=own)
    hi = datetime.combine((end + timedelta(days=1)).astimezone(own).date(), time.min, tzinfo=own)
    for ev in glimpse.events_between(lo, hi):
        if ev.free or ev.calendar_id in ignore or (ev.all_day and not cfg.availability.all_day_busy):
            continue
        s, e = (ev.start, ev.end) if ev.all_day else (ev.start - pad, ev.end + pad)
        if e > start and s < end:
            out.append((s, e))
    for s, e in holds or []:
        if e > start and s < end:
            out.append((s - pad, e + pad))
    return merge(out)


def is_free(busy: list[tuple[datetime, datetime]], s: datetime, e: datetime) -> bool:
    return not any(bs < e and s < be for bs, be in busy)


def bookable_windows(
    hours: HoursCfg, first: date, last: date, tz: ZoneInfo
) -> list[tuple[datetime, datetime]]:
    out = []
    d = first
    while d <= last:
        if d.weekday() in hours.weekday_numbers:
            out.append(
                (datetime.combine(d, hours.start, tzinfo=tz), datetime.combine(d, hours.end, tzinfo=tz))
            )
        d += timedelta(days=1)
    return out


def request_range(cfg: AppConfig, audience: AudienceCfg, now: datetime) -> tuple[datetime, datetime]:
    """Earliest and latest moment a requested meeting may fall in."""
    r = audience.requests
    earliest = now + timedelta(hours=r.notice_hours)
    latest_day = now.astimezone(cfg.tz).date() + timedelta(days=r.days_ahead)
    if audience.weeks_ahead is not None:
        # Never offer times beyond the weeks this audience may browse.
        from .core import week_start

        cap = week_start(now.astimezone(cfg.tz).date(), cfg) + timedelta(weeks=audience.weeks_ahead, days=6)
        latest_day = min(latest_day, cap)
    return earliest, datetime.combine(latest_day + timedelta(days=1), time.min, tzinfo=cfg.tz)


def open_slots(
    glimpse,
    audience: AudienceCfg,
    minutes: int,
    now: datetime,
    holds: list[tuple[datetime, datetime]] | None = None,
) -> list[Slot]:
    """Every start time (on the step grid) where a meeting of ``minutes`` fits."""
    cfg = glimpse.cfg
    tz = cfg.tz
    r = audience.requests
    earliest, latest = request_range(cfg, audience, now)
    if earliest >= latest:
        return []
    busy = busy_times(glimpse, earliest, latest, holds)
    length = timedelta(minutes=minutes)
    step = timedelta(minutes=r.step_minutes)
    out = []
    for ws, we in bookable_windows(audience.request_hours, earliest.astimezone(tz).date(), latest.date(), tz):
        t = ws
        while t + length <= we:
            if t >= earliest and t + length <= latest and is_free(busy, t, t + length):
                out.append(Slot(t, t + length))
            t += step
    return out


def fits_rules(
    cfg: AppConfig, audience: AudienceCfg, start: datetime, end: datetime, now: datetime
) -> str | None:
    """Why a requested time isn't allowed (None if it is). Used for picked slots."""
    r = audience.requests
    tz = cfg.tz
    earliest, latest = request_range(cfg, audience, now)
    if start < earliest:
        return "That time is too soon. Please pick a later one."
    if end > latest:
        return "That time is too far ahead."
    s, e = start.astimezone(tz), end.astimezone(tz)
    hours = audience.request_hours
    if s.date() != (e - timedelta(seconds=1)).date() or s.weekday() not in hours.weekday_numbers:
        return "That time is outside the hours you can book."
    if s.time() < hours.start or e.time() > hours.end and e.time() != time(0, 0):
        return "That time is outside the hours you can book."
    if int((end - start).total_seconds() // 60) not in r.lengths:
        return "Please pick one of the offered meeting lengths."
    return None


def busy_blocks_for_days(
    glimpse,
    first: date,
    days: int,
    hours: HoursCfg | None,
    tz: ZoneInfo,
    holds: list[tuple[datetime, datetime]] | None = None,
) -> list[tuple[date, list[tuple[float, float]]]]:
    """For the widget: per day, busy stretches as (top %, height %) within the shown hours."""
    h0, h1 = (hours.start, hours.end) if hours else (time(8, 0), time(18, 0))
    out = []
    start = datetime.combine(first, time.min, tzinfo=tz)
    end = start + timedelta(days=days)
    busy = busy_times(glimpse, start, end, holds)
    for i in range(days):
        d = first + timedelta(days=i)
        ds, de = datetime.combine(d, h0, tzinfo=tz), datetime.combine(d, h1, tzinfo=tz)
        span = (de - ds).total_seconds()
        parts = []
        for bs, be in busy:
            s, e = max(bs, ds), min(be, de)
            if s < e:
                parts.append((100 * (s - ds).total_seconds() / span, 100 * (e - s).total_seconds() / span))
        out.append((d, parts))
    return out
