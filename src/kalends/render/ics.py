"""A filtered iCalendar feed, so a viewer can subscribe in their own calendar app."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import icalendar

from ..policy import VisibleEvent


def to_ics(title: str, events: list[VisibleEvent], refresh_minutes: int = 30) -> bytes:
    cal = icalendar.Calendar()
    cal.add("prodid", "-//Kalends//Filtered calendar//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", title)
    ttl = timedelta(minutes=max(refresh_minutes, 15))
    cal.add("refresh-interval", ttl, parameters={"VALUE": "DURATION"})
    cal.add("x-published-ttl", ttl)
    stamp = datetime.now(UTC)

    for ev in events:
        v = icalendar.Event()
        v.add("uid", f"{ev.key}@glimpse")  # kept since the rename, so subscribed apps see the same events
        v.add("dtstamp", stamp)
        if ev.all_day:
            v.add("dtstart", ev.start.date())
            v.add("dtend", ev.end.date())
        else:
            v.add("dtstart", ev.start.astimezone(UTC))
            v.add("dtend", ev.end.astimezone(UTC))
        v.add("summary", ev.title)
        if ev.location:
            v.add("location", ev.location)
        if ev.notes:
            v.add("description", ev.notes)
        v.add("transp", "OPAQUE")
        cal.add_component(v)
    return cal.to_ical()
