"""Confirming and cancelling meeting requests: the calendar entry, the invite file, the wording.

Shared by the public pages (instant booking, requester actions) and the settings panel
(your answers), so a meeting is handled the same way whoever confirms it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import localcal
from .config import AppConfig, AudienceCfg
from .notify import invite_ics
from .requests_store import MeetingRequest, RequestStore

log = logging.getLogger("glimpse.bookings")


def owner_name(cfg: AppConfig) -> str:
    return (cfg.owner_name or "").strip() or "the organiser"


def details(item: MeetingRequest, include_note: bool = True) -> str:
    """Join link, place and note for calendar notes and emails."""
    parts = []
    if item.link:
        parts.append(f"Join: {item.link}")
    if item.location:
        parts.append(f"Where: {item.location}")
    if include_note and item.note:
        parts.append(item.note)
    return "\n\n".join(parts)


def requester_invite(cfg: AppConfig, item: MeetingRequest) -> str:
    """The calendar file the requester gets: titled with your name, link and place included."""
    return invite_ics(
        f"glimpse-{item.id}@glimpse",
        item.start_dt,
        item.end_dt,
        f"Meeting with {owner_name(cfg)}",
        details(item),
        location=item.link or item.location,
        url=item.link or None,
    )


def owner_invite(item: MeetingRequest) -> str:
    desc = f"Requested by {item.name} <{item.email}> through Glimpse."
    more = details(item)
    return invite_ics(
        f"glimpse-{item.id}@glimpse",
        item.start_dt,
        item.end_dt,
        f"Meeting with {item.name}",
        desc + ("\n\n" + more if more else ""),
        location=item.link or item.location,
        url=item.link or None,
    )


def _calendar_path(cfg: AppConfig, cid: str | None) -> Path | None:
    cal = cfg.calendars.get(cid or "")
    return Path(cal.url) if cal and cal.local else None


def add_to_calendar(glimpse, item: MeetingRequest) -> str | None:
    """Put a confirmed meeting on the audience's chosen Glimpse calendar. Returns the calendar id."""
    cfg: AppConfig = glimpse.cfg
    aud: AudienceCfg | None = cfg.audiences.get(item.audience)
    cid = aud.requests.calendar if aud else None
    path = _calendar_path(cfg, cid)
    if path is None:
        return None
    desc = f"{item.name} <{item.email}>, booked through Glimpse."
    more = details(item)
    try:
        localcal.save(
            path,
            localcal.LocalEvent(
                uid=f"glimpse-req-{item.id}@glimpse",
                title=f"Meeting with {item.name}",
                start=item.start_dt.astimezone(cfg.tz),
                end=item.end_dt.astimezone(cfg.tz),
                all_day=False,
                location=item.link or item.location,
                notes=desc + ("\n\n" + more if more else ""),
                source=f"request:{item.id}",
            ),
            cfg.tz,
        )
    except OSError as exc:
        log.warning("couldn't add meeting %s to %s: %s", item.id, cid, exc)
        return None
    glimpse.reload_local(cid)
    return cid


def remove_from_calendar(glimpse, item: MeetingRequest) -> None:
    path = _calendar_path(glimpse.cfg, item.calendar)
    if path is None:
        return
    try:
        if localcal.delete(path, f"glimpse-req-{item.id}@glimpse"):
            glimpse.reload_local(item.calendar)
    except OSError as exc:
        log.warning("couldn't remove meeting %s from %s: %s", item.id, item.calendar, exc)


def confirm(
    glimpse,
    store: RequestStore,
    item: MeetingRequest,
    note: str,
    link: str | None = None,
    location: str | None = None,
    **fields,
) -> MeetingRequest:
    """Mark a request accepted, with the meeting link/place, and add it to the chosen calendar."""
    aud = glimpse.cfg.audiences.get(item.audience)
    r = aud.requests if aud else None
    link = (link if link is not None else (item.link or (r.meeting_link if r else None) or "")).strip()
    location = (
        location if location is not None else (item.location or (r.location if r else None) or "")
    ).strip()
    item = store.change(item.id, "accepted", note, link=link, location=location, **fields)
    cid = add_to_calendar(glimpse, item)
    if cid:
        item = store.change(item.id, "accepted", f"added to {cid}", calendar=cid)
    return item


def cancel(glimpse, store: RequestStore, item: MeetingRequest, note: str) -> MeetingRequest:
    was_on_calendar = item.status == "accepted"
    item = store.change(item.id, "cancelled", note)
    if was_on_calendar:
        remove_from_calendar(glimpse, item)
    return item
