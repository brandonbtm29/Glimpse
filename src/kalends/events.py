"""Parse iCalendar text into plain event objects for a date range.

Recurring events are expanded (including exceptions, moved instances and
EXDATEs) by ``recurring-ical-events``; every time is converted to the
configured local time zone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events

log = logging.getLogger("kalends.events")


@dataclass(frozen=True)
class Event:
    calendar_id: str
    uid: str
    start: datetime  # timezone-aware, local zone
    end: datetime  # timezone-aware, local zone (exclusive)
    all_day: bool
    summary: str
    location: str
    description: str
    private: bool  # CLASS:PRIVATE / CONFIDENTIAL
    free: bool  # TRANSP:TRANSPARENT ("show as free")


def _to_local(value: date | datetime, tz: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:  # "floating" time: treat as local
            return value.replace(tzinfo=tz)
        return value.astimezone(tz)
    return datetime.combine(value, time.min, tzinfo=tz)


def _text(component: icalendar.cal.Component, key: str) -> str:
    value = component.get(key)
    return str(value).strip() if value is not None else ""


def parse_events(
    calendar_id: str, ics_text: str, start: datetime, end: datetime, tz: ZoneInfo
) -> list[Event]:
    """All event occurrences from one feed that overlap ``[start, end)``."""
    try:
        cal = icalendar.Calendar.from_ical(ics_text)
    except Exception as exc:
        log.warning("calendar %s could not be parsed: %s", calendar_id, exc)
        return []

    try:
        occurrences = recurring_ical_events.of(cal, skip_bad_series=True).between(start, end)
    except Exception as exc:
        log.warning("calendar %s: recurrence expansion failed: %s", calendar_id, exc)
        return []

    events: list[Event] = []
    for comp in occurrences:
        if comp.name != "VEVENT":
            continue
        if _text(comp, "STATUS").upper() == "CANCELLED":
            continue
        dtstart = comp.get("DTSTART")
        if dtstart is None:
            continue
        raw_start = dtstart.dt
        all_day = not isinstance(raw_start, datetime)

        dtend = comp.get("DTEND")
        if dtend is not None:
            raw_end = dtend.dt
        elif comp.get("DURATION") is not None:
            raw_end = raw_start + comp.get("DURATION").dt
        else:
            raw_end = raw_start + (timedelta(days=1) if all_day else timedelta(0))

        ev_start = _to_local(raw_start, tz)
        ev_end = _to_local(raw_end, tz)
        if ev_end <= ev_start:
            ev_end = ev_start + (timedelta(days=1) if all_day else timedelta(minutes=15))
        if ev_end <= start or ev_start >= end:
            continue

        events.append(
            Event(
                calendar_id=calendar_id,
                uid=_text(comp, "UID") or f"{calendar_id}-{ev_start.isoformat()}",
                start=ev_start,
                end=ev_end,
                all_day=all_day,
                summary=_text(comp, "SUMMARY") or "(no title)",
                location=_text(comp, "LOCATION"),
                description=_text(comp, "DESCRIPTION"),
                private=_text(comp, "CLASS").upper() in {"PRIVATE", "CONFIDENTIAL"},
                free=_text(comp, "TRANSP").upper() == "TRANSPARENT",
            )
        )
    events.sort(key=lambda e: (e.start, e.end, e.summary))
    return events


def collect_events(feeds: dict[str, str], start: datetime, end: datetime, tz: ZoneInfo) -> list[Event]:
    out: list[Event] = []
    for cid, text in feeds.items():
        out.extend(parse_events(cid, text, start, end, tz))
    out.sort(key=lambda e: (e.start, e.end, e.calendar_id))
    return out
