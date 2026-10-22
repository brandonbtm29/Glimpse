"""Kalends calendars: calendars you make in the settings panel.

Each one is a plain iCalendar (.ics) file in ``local-calendars/`` beside your config,
so it works like any other feed (detail levels, groups, rules), can be backed up or
opened by other apps, and needs no account anywhere.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import icalendar

log = logging.getLogger("kalends.localcal")

FOLDER = "local-calendars"
_lock = threading.Lock()

REPEATS = {
    "none": "Doesn't repeat",
    "daily": "Every day",
    "weekdays": "Every weekday (Mon–Fri)",
    "weekly": "Every week",
    "biweekly": "Every 2 weeks",
    "monthly": "Every month",
    "yearly": "Every year",
}
_RRULES = {
    "daily": {"FREQ": "DAILY"},
    "weekdays": {"FREQ": "WEEKLY", "BYDAY": ["MO", "TU", "WE", "TH", "FR"]},
    "weekly": {"FREQ": "WEEKLY"},
    "biweekly": {"FREQ": "WEEKLY", "INTERVAL": 2},
    "monthly": {"FREQ": "MONTHLY"},
    "yearly": {"FREQ": "YEARLY"},
}


@dataclass
class LocalEvent:
    uid: str
    title: str
    start: datetime | date
    end: datetime | date
    all_day: bool
    location: str = ""
    notes: str = ""
    repeat: str = "none"
    until: date | None = None
    free: bool = False
    private: bool = False
    skipped: int = 0  # occurrences removed from a repeating event
    source: str = ""  # "request:<id>" for meetings booked through Kalends

    @property
    def start_day(self) -> date:
        return (
            self.start
            if isinstance(self.start, date) and not isinstance(self.start, datetime)
            else self.start.date()
        )


def path_for(config_dir: Path, cid: str) -> Path:
    return config_dir / FOLDER / f"{cid}.ics"


def _empty(name: str) -> icalendar.Calendar:
    cal = icalendar.Calendar()
    cal.add("prodid", "-//Kalends//Kalends calendar//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", name)
    return cal


def _read(path: Path) -> icalendar.Calendar:
    return icalendar.Calendar.from_ical(path.read_text(encoding="utf-8"))


def _write(path: Path, cal: icalendar.Calendar) -> None:
    if hasattr(cal, "add_missing_timezones"):
        try:
            cal.add_missing_timezones()
        except Exception as exc:  # noqa: BLE001 - VTIMEZONE blocks are a nicety for other apps
            log.debug("couldn't add time zone blocks: %s", exc)
    data = cal.to_ical().decode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".cal-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write(data)
    os.replace(tmp, path)


def create(path: Path, name: str) -> None:
    with _lock:
        if not path.exists():
            _write(path, _empty(name))


def rename(path: Path, name: str) -> None:
    with _lock:
        cal = _read(path) if path.exists() else _empty(name)
        if "X-WR-CALNAME" in cal:
            del cal["X-WR-CALNAME"]
        cal.add("x-wr-calname", name)
        _write(path, cal)


def retire(path: Path) -> Path | None:
    """Move a deleted calendar's file aside (never destroy someone's events outright)."""
    if not path.exists():
        return None
    dest = path.parent / "deleted" / f"{path.stem}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.ics"
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(path, dest)
    return dest


def _repeat_of(rrule, tz: ZoneInfo) -> tuple[str, date | None]:
    if not rrule:
        return "none", None
    freq = str(rrule.get("FREQ", [""])[0]).upper()
    interval = int(rrule.get("INTERVAL", [1])[0])
    byday = [str(x).upper() for x in rrule.get("BYDAY", [])]
    until = rrule.get("UNTIL", [None])[0]
    if isinstance(until, datetime):
        until = (until.astimezone(tz) if until.tzinfo else until).date()
    if freq == "DAILY":
        kind = "daily"
    elif freq == "WEEKLY" and byday == ["MO", "TU", "WE", "TH", "FR"]:
        kind = "weekdays"
    elif freq == "WEEKLY":
        kind = "biweekly" if interval == 2 else "weekly"
    elif freq == "MONTHLY":
        kind = "monthly"
    elif freq == "YEARLY":
        kind = "yearly"
    else:
        kind = "custom"
    return kind, until


def events(path: Path, tz: ZoneInfo) -> list[LocalEvent]:
    """Every event (each repeating series once), soonest first."""
    if not path.exists():
        return []
    out = []
    for comp in _read(path).walk("VEVENT"):
        if comp.get("RECURRENCE-ID") is not None:
            continue  # a changed single occurrence; edited with its series
        start = comp.decoded("DTSTART")
        end = comp.decoded("DTEND") if comp.get("DTEND") is not None else None
        all_day = not isinstance(start, datetime)
        if not all_day:
            start = start.astimezone(tz) if start.tzinfo else start.replace(tzinfo=tz)
        if end is None:
            end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
        elif not all_day:
            end = end.astimezone(tz) if end.tzinfo else end.replace(tzinfo=tz)
        repeat, until = _repeat_of(comp.get("RRULE"), tz)
        exdates = comp.get("EXDATE")
        n_ex = 0
        if exdates is not None:
            for ex in exdates if isinstance(exdates, list) else [exdates]:
                n_ex += len(ex.dts)
        out.append(
            LocalEvent(
                uid=str(comp.get("UID")),
                title=str(comp.get("SUMMARY", "")),
                start=start,
                end=end,
                all_day=all_day,
                location=str(comp.get("LOCATION", "")),
                notes=str(comp.get("DESCRIPTION", "")),
                repeat=repeat,
                until=until,
                free=str(comp.get("TRANSP", "")).upper() == "TRANSPARENT",
                private=str(comp.get("CLASS", "")).upper() in ("PRIVATE", "CONFIDENTIAL"),
                skipped=n_ex,
                source=str(comp.get("X-GLIMPSE-SOURCE", "")),
            )
        )
    out.sort(
        key=lambda e: (
            e.start_day,
            e.start if isinstance(e.start, datetime) else datetime.min.replace(tzinfo=UTC),
        )
    )
    return out


def get(path: Path, uid: str, tz: ZoneInfo) -> LocalEvent | None:
    return next((e for e in events(path, tz) if e.uid == uid), None)


def _build(ev: LocalEvent, tz: ZoneInfo) -> icalendar.Event:
    comp = icalendar.Event()
    comp.add("uid", ev.uid)
    comp.add("dtstamp", datetime.now(UTC))
    comp.add("summary", ev.title or "(no title)")
    if ev.all_day:
        comp.add("dtstart", ev.start if not isinstance(ev.start, datetime) else ev.start.date())
        comp.add("dtend", ev.end if not isinstance(ev.end, datetime) else ev.end.date())
    else:
        comp.add("dtstart", ev.start)
        comp.add("dtend", ev.end)
    if ev.location:
        comp.add("location", ev.location)
    if ev.notes:
        comp.add("description", ev.notes)
    if ev.free:
        comp.add("transp", "TRANSPARENT")
    if ev.private:
        comp.add("class", "PRIVATE")
    if ev.source:
        comp.add("x-glimpse-source", ev.source)  # property name kept from the old project name
    rule = _RRULES.get(ev.repeat)
    if rule:
        rule = dict(rule)
        if ev.until:
            if ev.all_day:
                rule["UNTIL"] = ev.until
            else:
                # Last moment of that day, in UTC as the format requires for timed events.
                rule["UNTIL"] = datetime.combine(ev.until, time(23, 59, 59), tzinfo=tz).astimezone(UTC)
        comp.add("rrule", rule)
    return comp


def save(path: Path, ev: LocalEvent, tz: ZoneInfo, keep_skips: bool = True) -> str:
    """Add or replace an event (by uid). Returns its uid."""
    if not ev.uid:
        ev.uid = f"{uuid.uuid4()}@glimpse"
    with _lock:
        cal = _read(path) if path.exists() else _empty(path.stem)
        old_exdates = None
        keep = []
        for comp in cal.subcomponents:
            if comp.name == "VEVENT" and str(comp.get("UID")) == ev.uid:
                if comp.get("RECURRENCE-ID") is None and comp.get("EXDATE") is not None:
                    old_exdates = comp.get("EXDATE")
                continue  # replaced below (along with any edited single occurrences)
            keep.append(comp)
        cal.subcomponents = keep
        comp = _build(ev, tz)
        if keep_skips and old_exdates is not None and ev.repeat != "none":
            comp["EXDATE"] = old_exdates
        cal.add_component(comp)
        _write(path, cal)
    return ev.uid


def delete(path: Path, uid: str) -> bool:
    with _lock:
        if not path.exists():
            return False
        cal = _read(path)
        before = len(cal.subcomponents)
        cal.subcomponents = [
            c for c in cal.subcomponents if not (c.name == "VEVENT" and str(c.get("UID")) == uid)
        ]
        if len(cal.subcomponents) == before:
            return False
        _write(path, cal)
        return True


def skip(path: Path, uid: str, day: date, tz: ZoneInfo) -> bool:
    """Remove one occurrence of a repeating event."""
    with _lock:
        if not path.exists():
            return False
        cal = _read(path)
        for comp in cal.walk("VEVENT"):
            if str(comp.get("UID")) != uid or comp.get("RECURRENCE-ID") is not None:
                continue
            start = comp.decoded("DTSTART")
            if isinstance(start, datetime):
                st = start.astimezone(tz) if start.tzinfo else start.replace(tzinfo=tz)
                comp.add("exdate", datetime.combine(day, st.timetz().replace(tzinfo=None), tzinfo=tz))
            else:
                comp.add("exdate", day)
            _write(path, cal)
            return True
    return False


def find_by_source(path: Path, source: str) -> str | None:
    if not path.exists():
        return None
    for comp in _read(path).walk("VEVENT"):
        if str(comp.get("X-GLIMPSE-SOURCE", "")) == source:
            return str(comp.get("UID"))
    return None
