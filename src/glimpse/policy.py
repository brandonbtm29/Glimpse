"""Decide what each audience may see. This module is the privacy boundary.

Everything downstream (HTML, Excel, ICS) only ever receives ``VisibleEvent``
objects produced here, and a ``VisibleEvent`` carries only the fields its
level allows. Busy blocks carry no calendar name, colour or original UID,
and overlapping busy blocks are merged so viewers can't count your events.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import AppConfig, AudienceCfg, HoursCfg, Level
from .events import Event

BUSY_TITLE = "Busy"
MAX_NOTES = 600


@dataclass(frozen=True)
class VisibleEvent:
    key: str  # stable, opaque id (never the source UID)
    start: datetime
    end: datetime
    all_day: bool
    level: Level
    title: str
    calendar_id: str | None = None  # None for busy blocks
    calendar_name: str | None = None
    color: str | None = None
    location: str = ""
    notes: str = ""


def _strip_tags(text: str, tags: list[str]) -> str:
    for tag in tags:
        if tag:
            text = re.sub(re.escape(tag), "", text, flags=re.IGNORECASE)
    return " ".join(text.split()) if "\n" not in text else text.strip()


def _contains(text: str, tag: str) -> bool:
    return bool(tag) and tag.lower() in text.lower()


def _opaque(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:20]


def effective_level(cfg: AppConfig, audience: AudienceCfg, ev: Event) -> Level:
    """The level an event is shown at, after per-event overrides.

    Overrides only ever reduce detail.
    """
    ov = cfg.overrides
    level = cfg.level_for(audience, ev.calendar_id)
    blob = f"{ev.summary}\n{ev.description}"
    if _contains(blob, ov.hide_tag):
        return Level.HIDDEN
    if _contains(blob, ov.private_tag) or (ov.honor_private_flag and ev.private):
        level = level.at_most(Level.BUSY)
    return level


def _days(start: datetime, end: datetime) -> list[date]:
    last = (end - timedelta(microseconds=1)).date()
    d, out = start.date(), []
    while d <= last:
        out.append(d)
        d += timedelta(days=1)
    return out


def clip_to_hours(
    start: datetime, end: datetime, hours: HoursCfg, tz: ZoneInfo
) -> list[tuple[datetime, datetime]]:
    """Split a timed event into the parts that fall inside the allowed hours."""
    allowed = hours.weekday_numbers
    out = []
    for d in _days(start, end):
        if d.weekday() not in allowed:
            continue
        ws = datetime.combine(d, hours.start, tzinfo=tz)
        we = datetime.combine(d, hours.end, tzinfo=tz)
        s, e = max(start, ws), min(end, we)
        if s < e:
            out.append((s, e))
    return out


def _all_day_parts(ev: Event, audience: AudienceCfg, tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    allowed = audience.hours.weekday_numbers if audience.hours else set(range(7))
    out = []
    for d in _days(ev.start, ev.end):
        if d.weekday() in allowed:
            s = datetime.combine(d, datetime.min.time(), tzinfo=tz)
            out.append((s, s + timedelta(days=1)))
    return out


def visible_events(
    cfg: AppConfig,
    audience: AudienceCfg,
    events: list[Event],
    range_start: datetime,
    range_end: datetime,
) -> list[VisibleEvent]:
    """Filter and redact ``events`` for one audience within a date range."""
    tz = cfg.tz
    tags = [cfg.overrides.private_tag, cfg.overrides.hide_tag]
    out: list[VisibleEvent] = []

    for ev in events:
        level = effective_level(cfg, audience, ev)
        if level is Level.HIDDEN:
            continue
        if ev.free and level in (Level.BUSY, Level.LABEL):
            continue  # "show as free" events don't make you busy
        if ev.all_day and level is Level.BUSY and not audience.show_all_day_busy:
            continue

        if ev.all_day:
            parts = _all_day_parts(ev, audience, tz)
        elif audience.hours:
            parts = clip_to_hours(ev.start, ev.end, audience.hours, tz)
        else:
            parts = [(ev.start, ev.end)]

        cal = cfg.calendars[ev.calendar_id]
        for s, e in parts:
            s, e = max(s, range_start), min(e, range_end)
            if s >= e:
                continue
            if level is Level.FULL:
                ve = VisibleEvent(
                    key=_opaque(ev.calendar_id, ev.uid, s.isoformat(), "full"),
                    start=s,
                    end=e,
                    all_day=ev.all_day,
                    level=level,
                    title=_strip_tags(ev.summary, tags) or "(no title)",
                    calendar_id=ev.calendar_id,
                    calendar_name=cal.name,
                    color=cal.color,
                    location=_strip_tags(ev.location, tags),
                    notes=_strip_tags(ev.description, tags)[:MAX_NOTES],
                )
            elif level is Level.TITLE:
                ve = VisibleEvent(
                    key=_opaque(ev.calendar_id, ev.uid, s.isoformat(), "title"),
                    start=s,
                    end=e,
                    all_day=ev.all_day,
                    level=level,
                    title=_strip_tags(ev.summary, tags) or "(no title)",
                    calendar_id=ev.calendar_id,
                    calendar_name=cal.name,
                    color=cal.color,
                )
            elif level is Level.LABEL:
                ve = VisibleEvent(
                    key=_opaque("label", cal.display_label, s.isoformat(), e.isoformat()),
                    start=s,
                    end=e,
                    all_day=ev.all_day,
                    level=level,
                    title=cal.display_label,
                    calendar_id=ev.calendar_id,
                    calendar_name=cal.display_label,
                    color=cal.color,
                )
            else:  # BUSY
                ve = VisibleEvent(
                    key=_opaque("busy", s.isoformat(), e.isoformat()),
                    start=s,
                    end=e,
                    all_day=ev.all_day,
                    level=level,
                    title=BUSY_TITLE,
                )
            out.append(ve)

    return _merge_anonymous(out)


def _merge_anonymous(items: list[VisibleEvent]) -> list[VisibleEvent]:
    """Merge overlapping busy blocks (and same-label blocks) into single blocks."""
    keep = [v for v in items if v.level not in (Level.BUSY, Level.LABEL)]
    groups: dict[tuple[str, bool], list[VisibleEvent]] = {}
    for v in items:
        if v.level in (Level.BUSY, Level.LABEL):
            groups.setdefault((v.title if v.level is Level.LABEL else "", v.all_day), []).append(v)

    for (_, _all_day), vs in groups.items():
        vs.sort(key=lambda v: (v.start, v.end))
        cur = vs[0]
        for v in vs[1:]:
            if v.start <= cur.end:  # overlapping or touching
                if v.end > cur.end:
                    cur = replace(cur, end=v.end)
            else:
                keep.append(_rekey(cur))
                cur = v
        keep.append(_rekey(cur))

    keep.sort(key=lambda v: (v.start, not v.all_day, v.end, v.title))
    return keep


def _rekey(v: VisibleEvent) -> VisibleEvent:
    return replace(v, key=_opaque(v.level.value, v.title, v.start.isoformat(), v.end.isoformat()))
