# ruff: noqa: F811 - the panel/store fixtures are imported from test_v050
"""0.6.0: Glimpse calendars, sharing them, instant booking and meeting links."""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from conftest import COWORKER_SLUG, PARTNER_PASSWORD, PARTNER_SLUG, WEEK
from fastapi.testclient import TestClient
from test_v050 import NOW, TZ, csrf_rev, enable_requests, form_token, panel, store  # noqa: F401

from glimpse import localcal
from glimpse.availability import open_slots
from glimpse.core import week_range
from glimpse.web.app import create_app

NY = ZoneInfo(TZ)
ZOOM = "https://zoom.us/j/1234567890?pwd=abc"


def public_for(rt):
    rt.notifier.owner = lambda *a, **k: None
    rt.notifier.requester = lambda *a, **k: None
    app = create_app(
        rt.cfg, glimpse=rt.glimpse, background_refresh=False, requests=rt.requests, notifier=rt.notifier
    )
    return TestClient(app, follow_redirects=False)


def make_calendar(c, name="Plans"):
    csrf, _ = csrf_rev(c, "/my-calendars")
    r = c.post("/my-calendars/add", data={"csrf": csrf, "name": name, "color": "#2f9e63"})
    assert r.status_code == 303
    return r.headers["location"].split("/")[-1].split("?")[0]


def add_event(c, cid, **fields):
    csrf, _ = csrf_rev(c, f"/my-calendars/{cid}")
    data = {
        "csrf": csrf,
        "title": "Dinner with Alex",
        "start_date": "2026-10-27",
        "start_time": "18:00",
        "end_date": "2026-10-27",
        "end_time": "19:30",
        "repeat": "none",
    } | fields
    return c.post(f"/my-calendars/{cid}/events", data=data)


# --- the file format ---------------------------------------------------------------------------------


def test_local_calendar_round_trip(tmp_path: Path):
    p = tmp_path / "plans.ics"
    localcal.create(p, "Plans")
    uid = localcal.save(
        p,
        localcal.LocalEvent(
            uid="",
            title="Run",
            start=datetime(2026, 10, 26, 7, 0, tzinfo=NY),
            end=datetime(2026, 10, 26, 8, 0, tzinfo=NY),
            all_day=False,
            repeat="weekdays",
            until=date(2026, 11, 6),
            location="Park",
        ),
        NY,
    )
    localcal.save(
        p,
        localcal.LocalEvent(
            uid="", title="Trip", start=date(2026, 11, 5), end=date(2026, 11, 8), all_day=True, free=True
        ),
        NY,
    )
    localcal.skip(p, uid, date(2026, 10, 28), NY)
    evs = {e.title: e for e in localcal.events(p, NY)}
    assert (
        evs["Run"].repeat == "weekdays" and evs["Run"].until == date(2026, 11, 6) and evs["Run"].skipped == 1
    )
    assert evs["Trip"].all_day and evs["Trip"].free
    # Editing keeps skipped dates; deleting removes the series.
    run = evs["Run"]
    run.title = "Morning run"
    localcal.save(p, run, NY)
    assert localcal.get(p, uid, NY).skipped == 1
    assert localcal.delete(p, uid) and localcal.get(p, uid, NY) is None
    localcal.rename(p, "Our plans")
    assert "X-WR-CALNAME:Our plans" in p.read_text()
    moved = localcal.retire(p)
    assert moved and moved.exists() and not p.exists()


# --- the panel ---------------------------------------------------------------------------------------


def test_create_calendar_and_events_show_up_everywhere(panel, store):
    rt, c = panel
    cid = make_calendar(c)
    cal = rt.cfg.calendars[cid]
    assert cal.local and Path(cal.url).is_file() and Path(cal.url).parent.name == "local-calendars"
    assert add_event(c, cid).status_code == 303
    html = c.get(f"/my-calendars/{cid}").text
    assert "Dinner with Alex" in html
    # Works like any calendar: full details for the partner, busy for coworkers (evening is outside hours).
    pub = public_for(rt)
    pub.post(f"/a/{PARTNER_SLUG}/login", data={"password": PARTNER_PASSWORD, "next": ""})
    assert "Dinner with Alex" in pub.get(f"/a/{PARTNER_SLUG}/week/{WEEK}").text
    assert "Dinner with Alex" not in pub.get(f"/a/{COWORKER_SLUG}/week/{WEEK}").text
    # And it blocks open times.
    add_event(
        c,
        cid,
        title="Lunch",
        start_time="12:00",
        end_time="13:00",
        start_date="2026-10-28",
        end_date="2026-10-28",
    )
    store.update(enable_requests)
    rt.apply(store.current(), fetch=False)
    starts = {s.start for s in open_slots(rt.glimpse, rt.cfg.audiences["coworkers"], 30, NOW)}
    assert datetime(2026, 10, 28, 12, 0, tzinfo=NY) not in starts


def test_edit_skip_and_delete_events(panel):
    rt, c = panel
    cid = make_calendar(c)
    add_event(c, cid, title="Yoga", repeat="weekly", until="2026-12-01")
    ev = localcal.events(Path(rt.cfg.calendars[cid].url), NY)[0]
    page = c.get(f"/my-calendars/{cid}/events/{ev.uid}").text
    assert "Skip this date" in page and 'value="Yoga"' in page
    csrf, _ = csrf_rev(c, f"/my-calendars/{cid}/events/{ev.uid}")
    c.post(f"/my-calendars/{cid}/events/{ev.uid}/skip", data={"csrf": csrf, "day": "2026-11-03"})
    s, e = week_range(date(2026, 11, 2), rt.cfg)
    assert not [x for x in rt.glimpse.events_between(s, e) if x.calendar_id == cid]
    c.post(
        f"/my-calendars/{cid}/events/{ev.uid}",
        data={
            "csrf": csrf,
            "title": "Hot yoga",
            "start_date": "2026-10-27",
            "start_time": "18:00",
            "end_date": "2026-10-27",
            "end_time": "19:00",
            "repeat": "weekly",
            "until": "2026-12-01",
        },
    )
    after = localcal.get(Path(rt.cfg.calendars[cid].url), ev.uid, NY)
    assert after.title == "Hot yoga" and after.skipped == 1
    # Bad input is refused with a message.
    r = add_event(c, cid, start_time="19:00", end_time="18:00")
    assert r.status_code == 400 and "end after it starts" in r.text
    c.post(f"/my-calendars/{cid}/events/{ev.uid}/delete", data={"csrf": csrf})
    assert localcal.events(Path(rt.cfg.calendars[cid].url), NY) == []


def test_all_day_event(panel):
    rt, c = panel
    cid = make_calendar(c)
    add_event(
        c,
        cid,
        title="Conference",
        all_day="on",
        start_date="2026-10-29",
        end_date="2026-10-30",
        start_time="",
        end_time="",
    )
    ev = localcal.events(Path(rt.cfg.calendars[cid].url), NY)[0]
    assert ev.all_day and ev.start == date(2026, 10, 29) and ev.end == date(2026, 10, 31)


def test_share_link(panel):
    rt, c = panel
    cid = make_calendar(c)
    add_event(c, cid)
    csrf, _ = csrf_rev(c, f"/my-calendars/{cid}")
    c.post(f"/my-calendars/{cid}/share", data={"csrf": csrf, "action": "on"})
    cal = rt.cfg.calendars[cid]
    assert cal.share and cal.share_slug and cal.share_slug not in rt.store.config_path.read_text()
    pub = public_for(rt)
    r = pub.get(f"/cal/{cal.share_slug}.ics")
    assert (
        r.status_code == 200
        and "Dinner with Alex" in r.text
        and r.headers["content-type"].startswith("text/calendar")
    )
    old = cal.share_slug
    c.post(f"/my-calendars/{cid}/share", data={"csrf": csrf, "action": "rotate"})
    pub = public_for(rt)
    assert pub.get(f"/cal/{old}.ics").status_code == 404
    c.post(f"/my-calendars/{cid}/share", data={"csrf": csrf, "action": "off"})
    pub = public_for(rt)
    assert pub.get(f"/cal/{rt.cfg.calendars[cid].share_slug}.ics").status_code == 404
    # Ordinary (feed) calendars are never served this way, even with a slug set by hand.
    rt.store.update(lambda d: d.set_calendar_share("personal", True))
    rt.apply(rt.store.current(), fetch=False)
    slug = rt.cfg.calendars["personal"].share_slug
    assert public_for(rt).get(f"/cal/{slug}.ics").status_code == 404


def test_delete_calendar_keeps_file_and_clears_references(panel, store):
    rt, c = panel
    cid = make_calendar(c)
    path = Path(rt.cfg.calendars[cid].url)
    store.update(lambda d: d.requests_node("coworkers").__setitem__("calendar", cid))
    rt.apply(store.current(), fetch=False)
    csrf, _ = csrf_rev(c, f"/my-calendars/{cid}")
    c.post(f"/my-calendars/{cid}/delete", data={"csrf": csrf})
    assert cid not in rt.cfg.calendars and not path.exists()
    assert list((path.parent / "deleted").glob(f"{cid}-*.ics"))
    assert rt.cfg.audiences["coworkers"].requests.calendar is None


# --- booking ----------------------------------------------------------------------------------------


def setup_booking(c, rt, auto=True, booking=True, link=ZOOM):
    cid = make_calendar(c, "Meetings")
    csrf, rev = csrf_rev(c, "/audiences/coworkers")
    data = {
        "csrf": csrf,
        "rev": rev,
        "enabled": "on",
        "length": ["30", "60"],
        "slots": "on",
        "suggest": "on",
        "notice_hours": "24",
        "days_ahead": "14",
        "step_minutes": "30",
        "auto_confirm": "on" if auto else "off",
        "calendar": cid,
        "meeting_link": link,
        "location": "Room 301",
    }
    if booking:
        data["booking"] = "on"
    r = c.post("/audiences/coworkers/requests", data=data)
    assert r.status_code == 303, r.text[:300]
    return cid


def test_booking_page_books_instantly_with_link(panel):
    rt, c = panel
    cid = setup_booking(c, rt)
    req = rt.cfg.audiences["coworkers"].requests
    assert (
        req.auto_confirm
        and req.calendar == cid
        and req.meeting_link == ZOOM
        and req.booking
        and req.booking_slug
    )
    assert req.booking_slug not in rt.store.config_path.read_text()
    html = c.get("/audiences/coworkers").text
    assert f"/b/{req.booking_slug}" in html
    pub = public_for(rt)
    page = pub.get(f"/b/{req.booking_slug}?len=30").text
    assert "Book this time" in page and ZOOM not in page and "Back to the schedule" not in page
    start = re.findall(r'name="start" value="([^"]+)"', page)[0]
    r = pub.post(
        f"/b/{req.booking_slug}",
        data={
            "ft": form_token(page),
            "mode": "slot",
            "len": "30",
            "start": start,
            "name": "Dana",
            "email": "dana@uakron.edu",
            "tz": TZ,
        },
    )
    assert r.status_code == 303
    item = rt.requests.all()[0]
    assert (
        item.status == "accepted"
        and item.link == ZOOM
        and item.location == "Room 301"
        and item.calendar == cid
    )
    assert item.viewer == "b:coworkers"
    # On the Glimpse calendar, so it shows up and blocks time.
    evs = localcal.events(Path(rt.cfg.calendars[cid].url), NY)
    assert [e.title for e in evs] == ["Meeting with Dana"] and evs[0].location == ZOOM
    assert evs[0].source == f"request:{item.id}"
    status = pub.get(r.headers["location"]).text
    assert "Booked!" in status and "Join the meeting" in status and ZOOM.replace("&", "&amp;") in status
    invite = pub.get(f"/req/{item.token}/invite.ics").text
    assert "LOCATION:" in invite and "URL:https://zoom.us/j/" in invite and "Meeting with" in invite
    # The requester cancels: off the calendar too.
    pub.post(f"/req/{item.token}", data={"action": "cancel", "ft": form_token(status)})
    assert rt.requests.get(item.id).status == "cancelled"
    assert localcal.events(Path(rt.cfg.calendars[cid].url), NY) == []


def test_suggested_times_still_wait_even_with_instant_booking(panel):
    rt, c = panel
    setup_booking(c, rt)
    pub = public_for(rt)
    page = pub.get(f"/a/{COWORKER_SLUG}/request").text
    pub.post(
        f"/a/{COWORKER_SLUG}/request",
        data={
            "ft": form_token(page),
            "mode": "suggest",
            "len": "30",
            "date": "2026-10-29",
            "time": "19:00",
            "name": "Sam",
            "email": "sam@example.com",
            "tz": TZ,
        },
    )
    item = rt.requests.all()[0]
    assert item.status == "pending" and not item.link
    status = pub.get(f"/req/{item.token}").text
    assert ZOOM not in status


def test_manual_accept_with_link_override_and_owner_cancel(panel):
    rt, c = panel
    cid = setup_booking(c, rt, auto=False, booking=False)
    pub = public_for(rt)
    page = pub.get(f"/a/{COWORKER_SLUG}/request?len=30").text
    start = re.findall(r'name="start" value="([^"]+)"', page)[0]
    pub.post(
        f"/a/{COWORKER_SLUG}/request",
        data={
            "ft": form_token(page),
            "mode": "slot",
            "len": "30",
            "start": start,
            "name": "Priya",
            "email": "p@x.org",
            "tz": TZ,
        },
    )
    item = rt.requests.all()[0]
    assert item.status == "pending"
    html = c.get("/requests").text
    assert f'value="{ZOOM.replace("&", "&amp;")}"' in html and "Meetings" in html
    csrf, _ = csrf_rev(c, "/requests")
    bad = c.post(
        f"/requests/{item.id}/answer",
        data={"csrf": csrf, "action": "accept", "meeting_link": "javascript:alert(1)"},
    )
    assert bad.status_code == 400 and rt.requests.get(item.id).status == "pending"
    teams = "https://teams.microsoft.com/l/meetup-join/abc"
    c.post(
        f"/requests/{item.id}/answer",
        data={"csrf": csrf, "action": "accept", "meeting_link": teams, "location": ""},
    )
    got = rt.requests.get(item.id)
    assert got.status == "accepted" and got.link == teams and got.location == "" and got.calendar == cid
    assert localcal.events(Path(rt.cfg.calendars[cid].url), NY)[0].location == teams
    c.post(f"/requests/{item.id}/answer", data={"csrf": csrf, "action": "cancel"})
    assert rt.requests.get(item.id).status == "cancelled"
    assert localcal.events(Path(rt.cfg.calendars[cid].url), NY) == []


def test_bad_meeting_link_in_settings_is_refused(panel):
    rt, c = panel
    csrf, rev = csrf_rev(c, "/audiences/coworkers")
    r = c.post(
        "/audiences/coworkers/requests",
        data={
            "csrf": csrf,
            "rev": rev,
            "enabled": "on",
            "length": ["30"],
            "slots": "on",
            "meeting_link": "zoom.us/j/1",
        },
    )
    assert r.status_code == 400 and "https://" in r.text


def test_booking_page_off_or_unknown_is_404(panel):
    rt, c = panel
    pub = public_for(rt)
    assert pub.get("/b/unknownBookingSlug_123456").status_code == 404
    setup_booking(c, rt, booking=False)
    assert rt.cfg.audiences["coworkers"].requests.booking is False
    assert public_for(rt).get("/b/unknownBookingSlug_123456").status_code == 404


def test_my_calendars_pages_render(panel):
    rt, c = panel
    assert c.get("/my-calendars").status_code == 200
    cid = make_calendar(c)
    for path in (f"/my-calendars/{cid}", f"/calendars/{cid}", "/calendars", "/audiences/coworkers"):
        assert c.get(path).status_code == 200, path
    assert "Glimpse calendar" in c.get("/calendars").text


def test_backups_include_glimpse_calendars(panel, tmp_path):
    from glimpse import updates

    rt, c = panel
    cid = make_calendar(c)
    add_event(c, cid)
    dest = updates.backup_settings(rt.store.config_path, rt.store.env_path)
    assert (dest / "local-calendars" / f"{cid}.ics").is_file()
