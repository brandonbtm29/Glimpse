"""0.5.0: meeting requests, notifications, the website widget, school mode, support link."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from argon2 import PasswordHasher
from conftest import COWORKER_SLUG, PARTNER_SLUG, WEEK
from fastapi.testclient import TestClient

from glimpse.availability import busy_times, open_slots
from glimpse.config import ConfigError
from glimpse.core import Glimpse, week_range
from glimpse.notify import Note, Notifier, invite_ics
from glimpse.requests_store import RequestStore
from glimpse.school import Schedule, read_feed
from glimpse.store import ConfigStore
from glimpse.web.admin import create_admin_app
from glimpse.web.app import create_app
from glimpse.web.runtime import Runtime

ADMIN_PW = "admin-password-123"
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)  # Sat Oct 24, 8:00 AM New York
WIDGET_SLUG = "widgetSlug_0123456789ab"
TZ = "America/New_York"


@pytest.fixture
def store(config_path):
    return ConfigStore(config_path)


def enable_requests(d, aid="coworkers", **extra):
    node = d.audiences[aid]
    node["requests"] = {"enabled": True, "lengths": [30, 60], "notice_hours": 24, "days_ahead": 14, **extra}


def pin(g: Glimpse) -> Glimpse:
    g.today = lambda: NOW.astimezone(g.cfg.tz).date()
    g.now = lambda: NOW
    return g


def public(store, mutate=None, reqs=None, notifier=None):
    if mutate:
        store.update(mutate)
    g = pin(Glimpse(store.current()))
    g.cache.refresh_all()
    reqs = reqs or RequestStore(None)
    app = create_app(g.cfg, glimpse=g, background_refresh=False, requests=reqs, notifier=notifier)
    return g, reqs, TestClient(app, follow_redirects=False)


@pytest.fixture
def panel(store):
    store.update(
        lambda d: d.set_secret(
            d.section("admin"),
            "password_hash",
            PasswordHasher().hash(ADMIN_PW),
            "GLIMPSE_ADMIN_PASSWORD_HASH",
        )
    )
    store.update(lambda d: d.section("admin").__setitem__("secure_cookies", False))
    store.update(enable_requests)
    rt = Runtime(store.current(), store)
    pin(rt.glimpse)
    rt.refresh()
    c = TestClient(create_admin_app(rt), follow_redirects=False)
    c.post("/login", data={"password": ADMIN_PW})
    return rt, c


def csrf_rev(c, path):
    html = c.get(path).text
    rev = re.search(r'name="rev" value="([^"]+)"', html)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1), rev.group(1) if rev else ""


def form_token(html: str) -> str:
    return re.search(r'name="ft" value="([^"]+)"', html).group(1)


# --- settings -------------------------------------------------------------------------------------


def test_new_settings_have_safe_defaults(cfg):
    aud = cfg.audiences["coworkers"]
    assert aud.requests.enabled is False and aud.widget.enabled is False
    assert cfg.school.enabled is False and cfg.support_link is False
    assert cfg.notify.ntfy.enabled is False and cfg.notify.email.enabled is False
    # Requests fall back to the audience's hours, else weekdays 9-5.
    assert aud.request_hours == aud.hours
    assert cfg.audiences["partner"].request_hours.start.hour == 9


def test_widget_needs_a_slug_and_valid_sites(store):
    with pytest.raises(ConfigError, match="widget"):
        store.update(lambda d: d.audiences["coworkers"].__setitem__("widget", {"enabled": True}))
    with pytest.raises(ConfigError, match="example.com"):
        store.update(
            lambda d: d.audiences["coworkers"].__setitem__(
                "widget", {"slug": WIDGET_SLUG, "sites": ["lab site"]}
            )
        )


def test_discord_webhook_must_be_discord(store):
    with pytest.raises(ConfigError, match="discord"):
        store.update(
            lambda d: d.section("notify").__setitem__("discord", {"webhook_url": "https://evil.example/x"})
        )


def test_removing_a_calendar_cleans_new_references(store):
    def mutate(d):
        d.section("availability")["ignore_calendars"] = ["family"]
        d.section("school")["class_calendars"] = ["classes"]

    store.update(mutate)
    store.update(lambda d: d.remove_calendar("family"))
    store.update(lambda d: d.remove_calendar("classes"))
    cfg = store.current()
    assert cfg.availability.ignore_calendars == [] and cfg.school.class_calendars == []


# --- availability ---------------------------------------------------------------------------------


def test_open_slots_skip_every_busy_event_even_hidden_ones(store):
    store.update(enable_requests)
    g = pin(Glimpse(store.current()))
    g.cache.refresh_all()
    aud = g.cfg.audiences["coworkers"]
    slots = open_slots(g, aud, 30, NOW)
    assert slots, "expected some open times"
    tz = g.cfg.tz
    busy = busy_times(g, slots[0].start - timedelta(days=1), slots[-1].end + timedelta(days=1))
    for sl in slots:
        assert all(not (bs < sl.end and sl.start < be) for bs, be in busy)
        local = sl.start.astimezone(tz)
        assert local.weekday() < 5 and 8 <= local.hour < 18  # inside the audience's hours
        assert sl.start >= NOW + timedelta(hours=24)  # notice


def test_ignored_calendars_and_holds_change_open_times(store):
    store.update(enable_requests)
    g = pin(Glimpse(store.current()))
    g.cache.refresh_all()
    aud = g.cfg.audiences["coworkers"]
    base = {s.start for s in open_slots(g, aud, 30, NOW)}
    hold_start = min(base)
    held = {s.start for s in open_slots(g, aud, 30, NOW, [(hold_start, hold_start + timedelta(minutes=30))])}
    assert hold_start not in held
    store.update(lambda d: d.section("availability").__setitem__("ignore_calendars", list(d.calendars)))
    g2 = pin(Glimpse(store.current()))
    g2.cache.refresh_all()
    assert len(open_slots(g2, g2.cfg.audiences["coworkers"], 30, NOW)) >= len(base)


# --- public request flow --------------------------------------------------------------------------


def test_request_button_only_when_enabled(store):
    _, _, c = public(store)
    assert "Request a meeting" not in c.get(f"/a/{COWORKER_SLUG}/week/{WEEK}").text
    assert c.get(f"/a/{COWORKER_SLUG}/request").status_code == 404
    _, _, c = public(store, enable_requests)
    assert "Request a meeting" in c.get(f"/a/{COWORKER_SLUG}/week/{WEEK}").text


def test_pick_a_slot_and_track_it(store):
    sent = []
    note = Notifier(lambda: g.cfg)
    note.owner = lambda n, wait=False: sent.append(n)
    note.requester = lambda *a, **k: sent.append(("requester", a))
    g, reqs, c = public(store, enable_requests, notifier=note)
    page = c.get(f"/a/{COWORKER_SLUG}/request?len=30")
    assert page.status_code == 200
    starts = re.findall(r'name="start" value="([^"]+)"', page.text)
    assert starts
    # No event details ever reach the request page.
    for word in ("Dentist", "Lab meeting", "Family", "Personal"):
        assert word not in page.text
    r = c.post(
        f"/a/{COWORKER_SLUG}/request",
        data={
            "ft": form_token(page.text),
            "mode": "slot",
            "len": "30",
            "start": starts[0],
            "name": "Dana Coworker",
            "email": "dana@example.edu",
            "note": "Project sync",
            "tz": TZ,
        },
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/req/")
    item = reqs.all()[0]
    assert item.status == "pending" and item.audience == "coworkers" and item.kind == "slot"
    assert any(isinstance(x, Note) and "Dana Coworker" in x.body for x in sent)
    status = c.get(r.headers["location"])
    assert "Waiting for an answer" in status.text
    # The held time disappears from the list for the next person.
    again = c.get(f"/a/{COWORKER_SLUG}/request?len=30").text
    assert f'value="{starts[0]}"' not in again
    # Cancelling from the private status page.
    ft = form_token(status.text)
    c.post(r.headers["location"].split("?")[0], data={"action": "cancel", "ft": ft})
    assert reqs.get(item.id).status == "cancelled"


def test_taken_or_invalid_slots_are_refused(store):
    g, reqs, c = public(store, enable_requests)
    page = c.get(f"/a/{COWORKER_SLUG}/request?len=30").text
    ft = form_token(page)
    base = {"ft": ft, "mode": "slot", "len": "30", "name": "A", "email": "a@b.co", "tz": TZ}
    # A time inside a busy event (lab meeting Tue 4pm).
    busy = datetime(2026, 10, 27, 16, 0, tzinfo=g.cfg.tz).isoformat()
    assert c.post(f"/a/{COWORKER_SLUG}/request", data=base | {"start": busy}).status_code == 409
    # Outside the hours, too soon, and a length that isn't offered.
    late = datetime(2026, 10, 27, 20, 0, tzinfo=g.cfg.tz).isoformat()
    assert c.post(f"/a/{COWORKER_SLUG}/request", data=base | {"start": late}).status_code == 400
    soon = datetime(2026, 10, 24, 14, 0, tzinfo=g.cfg.tz).isoformat()
    assert c.post(f"/a/{COWORKER_SLUG}/request", data=base | {"start": soon}).status_code == 400
    starts = re.findall(r'name="start" value="([^"]+)"', page)
    assert (
        c.post(f"/a/{COWORKER_SLUG}/request", data=base | {"start": starts[0], "len": "45"}).status_code
        == 400
    )
    # Bots fill in the hidden field; stale or forged form tokens are rejected.
    assert (
        c.post(f"/a/{COWORKER_SLUG}/request", data=base | {"start": starts[0], "website": "x"}).status_code
        == 400
    )
    assert (
        c.post(f"/a/{COWORKER_SLUG}/request", data=base | {"start": starts[0], "ft": "nope"}).status_code
        == 400
    )
    for bad in ("a,b@c.de", "x@y", "x@y.z\nBcc: v@w.xy", "first last@x.yz"):
        r = c.post(f"/a/{COWORKER_SLUG}/request", data=base | {"start": starts[0], "email": bad})
        assert r.status_code == 400, bad
    assert reqs.all() == []


def test_suggest_a_time(store):
    g, reqs, c = public(store, lambda d: enable_requests(d, slots=False))
    page = c.get(f"/a/{COWORKER_SLUG}/request").text
    assert 'name="start"' not in page and 'name="date"' in page
    r = c.post(
        f"/a/{COWORKER_SLUG}/request",
        data={
            "ft": form_token(page),
            "mode": "suggest",
            "len": "60",
            "date": "2026-10-28",
            "time": "19:30",
            "name": "Sam",
            "email": "sam@example.com",
            "tz": TZ,
        },
    )
    assert r.status_code == 303
    item = reqs.all()[0]
    assert item.kind == "suggested" and item.minutes == 60
    assert item.start_dt == datetime(2026, 10, 28, 19, 30, tzinfo=g.cfg.tz)


def test_password_protected_audience_requires_sign_in_to_request(store):
    _, _, c = public(store, lambda d: enable_requests(d, "partner"))
    r = c.get(f"/a/{PARTNER_SLUG}/request")
    assert r.status_code == 303 and "/login" in r.headers["location"] and "request" in r.headers["location"]


def test_requests_per_ip_are_limited(store):
    g, reqs, c = public(store, lambda d: enable_requests(d, slots=False))
    codes = []
    for i in range(7):
        page = c.get(f"/a/{COWORKER_SLUG}/request").text
        r = c.post(
            f"/a/{COWORKER_SLUG}/request",
            data={
                "ft": form_token(page),
                "mode": "suggest",
                "len": "30",
                "date": "2026-10-29",
                "time": f"{8 + i:02d}:00",
                "name": "Spam",
                "email": "s@example.com",
                "tz": TZ,
            },
        )
        codes.append(r.status_code)
    assert 429 in codes
    assert len(reqs.all()) <= 5


def test_request_store_persists(tmp_path: Path):
    path = tmp_path / "requests.json"
    s = RequestStore(path)
    item = s.add(
        audience="a",
        person=None,
        viewer="a",
        name="N",
        email="n@x.y",
        note="",
        kind="slot",
        start="2026-10-27T10:00:00-04:00",
        end="2026-10-27T10:30:00-04:00",
    )
    again = RequestStore(path)
    assert again.get(item.id).name == "N" and again.by_token(item.token).id == item.id
    assert json.loads(path.read_text())["requests"][0]["status"] == "pending"


# --- the owner's side -----------------------------------------------------------------------------


def make_request(rt, start="2026-10-27T10:00:00-04:00", end="2026-10-27T10:30:00-04:00", **kw):
    fields = {
        "audience": "coworkers",
        "person": None,
        "viewer": "coworkers",
        "name": "Dana",
        "email": "d@x.edu",
        "note": "hello",
        "kind": "slot",
        "start": start,
        "end": end,
    } | kw
    return rt.requests.add(**fields)


def test_panel_lists_and_accepts_requests(panel):
    rt, c = panel
    free = open_slots(rt.glimpse, rt.cfg.audiences["coworkers"], 30, NOW)[0]
    item = make_request(rt, start=free.start.isoformat(), end=free.end.isoformat())
    clash = make_request(rt, name="Clash", start="2026-10-27T16:00:00-04:00", end="2026-10-27T16:30:00-04:00")
    mails = []
    rt.notifier.requester = lambda to, subject, body, ics=None, wait=False: mails.append((to, subject, ics))
    html = c.get("/requests").text
    assert "Dana" in html and "Waiting for you" in html and "re free" in html
    assert '<span class="count">2</span>' in html
    assert re.search(rf'id="r-{clash.id}".*?clashes with your calendar', html, re.S)
    csrf, _ = csrf_rev(c, "/requests")
    r = c.post(f"/requests/{item.id}/answer", data={"csrf": csrf, "action": "accept", "reply": "See you"})
    assert r.status_code == 303
    assert rt.requests.get(item.id).status == "accepted" and rt.requests.get(item.id).reply == "See you"
    assert mails and mails[0][0] == "d@x.edu" and "BEGIN:VEVENT" in mails[0][2]
    ics = c.get(f"/requests/{item.id}/invite.ics")
    assert ics.status_code == 200 and "SUMMARY:Meeting with Dana" in ics.text


def test_counter_proposal_round_trip(panel, store):
    rt, c = panel
    item = make_request(rt)
    rt.notifier.requester = lambda *a, **k: None
    csrf, _ = csrf_rev(c, "/requests")
    c.post(
        f"/requests/{item.id}/answer",
        data={"csrf": csrf, "action": "counter", "date": "2026-10-28", "time": "11:00", "len": "30"},
    )
    got = rt.requests.get(item.id)
    assert got.status == "countered"
    assert got.counter_start_dt == datetime(2026, 10, 28, 11, 0, tzinfo=rt.cfg.tz)
    # The requester accepts on their status page.
    g = rt.glimpse
    pub = TestClient(
        create_app(g.cfg, glimpse=g, background_refresh=False, requests=rt.requests, notifier=rt.notifier),
        follow_redirects=False,
    )
    rt.notifier.owner = lambda *a, **k: None
    page = pub.get(f"/req/{got.token}").text
    assert "suggested another time" in page
    pub.post(f"/req/{got.token}", data={"action": "accept_counter", "ft": form_token(page)})
    final = rt.requests.get(item.id)
    assert final.status == "accepted" and final.start_dt == datetime(2026, 10, 28, 11, 0, tzinfo=rt.cfg.tz)
    assert "Add to your calendar" in pub.get(f"/req/{got.token}").text
    assert pub.get(f"/req/{got.token}/invite.ics").status_code == 200
    assert pub.get("/req/not-a-real-token").status_code == 404


def test_audience_request_settings_save(panel):
    rt, c = panel
    csrf, rev = csrf_rev(c, "/audiences/coworkers")
    r = c.post(
        "/audiences/coworkers/requests",
        data={
            "csrf": csrf,
            "rev": rev,
            "enabled": "on",
            "length": ["15", "45"],
            "slots": "on",
            "notice_hours": "2",
            "days_ahead": "21",
            "step_minutes": "15",
            "message": "Zoom by default",
            "own_hours": "on",
            "rday": ["tue", "thu"],
            "rstart": "13:00",
            "rend": "16:00",
        },
    )
    assert r.status_code == 303
    req = rt.cfg.audiences["coworkers"].requests
    assert req.lengths == [15, 45] and req.suggest is False and req.step_minutes == 15
    assert (
        req.hours.days == ["tue", "thu"] and req.hours.start.hour == 13 and req.message == "Zoom by default"
    )


# --- notifications --------------------------------------------------------------------------------


def test_notification_secrets_go_to_env_not_config(panel, store):
    rt, c = panel
    csrf, rev = csrf_rev(c, "/notifications")
    r = c.post(
        "/notifications",
        data={
            "csrf": csrf,
            "rev": rev,
            "ntfy_enabled": "on",
            "ntfy_topic": "glimpse-secret-topic-123",
            "ntfy_server": "https://ntfy.sh",
            "discord_enabled": "on",
            "webhook_url": "https://discord.com/api/webhooks/123/abc",
            "email_enabled": "on",
            "smtp_host": "smtp.gmail.com",
            "smtp_port": "587",
            "security": "starttls",
            "username": "me@gmail.com",
            "password": "app-password-xyz",
            "notify_requester": "on",
        },
    )
    assert r.status_code == 303
    raw = store.config_path.read_text()
    env = store.env_path.read_text()
    for secret in ("glimpse-secret-topic-123", "123/abc", "app-password-xyz"):
        assert secret not in raw and secret in env
    assert rt.notifier.enabled() == ["ntfy", "email", "discord"]
    # Saving again with the secret fields empty keeps them.
    csrf, rev = csrf_rev(c, "/notifications")
    c.post(
        "/notifications",
        data={"csrf": csrf, "rev": rev, "ntfy_enabled": "on", "ntfy_server": "https://ntfy.sh"},
    )
    assert rt.cfg.notify.ntfy.topic == "glimpse-secret-topic-123"
    assert rt.notifier.enabled() == ["ntfy"]


def test_ntfy_and_discord_requests(monkeypatch, cfg):
    calls = []

    def fake_post(url, **kw):
        calls.append((url, kw))
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    c = cfg.model_copy(deep=True)
    c.notify.ntfy.enabled, c.notify.ntfy.topic = True, "topic-abc"
    c.notify.discord.enabled, c.notify.discord.webhook_url = True, "https://discord.com/api/webhooks/1/x"
    n = Notifier(lambda: c)
    n.owner(
        Note(title="New meeting request", body="Dana · Tue 10:00", url="https://pc:8443/requests"), wait=True
    )
    (u1, k1), (u2, k2) = calls
    assert u1 == "https://ntfy.sh/topic-abc" and k1["headers"]["Click"].endswith("/requests")
    assert k1["content"] == "Dana · Tue 10:00".encode()
    assert u2.startswith("https://discord.com/api/webhooks/") and k2["json"]["allowed_mentions"] == {
        "parse": []
    }
    assert n.last["ntfy"].ok and n.last["discord"].ok


def test_email_via_smtp(monkeypatch, cfg):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"] = (host, port)

        def starttls(self, context=None):
            sent["tls"] = True

        def login(self, u, p):
            sent["login"] = (u, p)

        def send_message(self, msg):
            sent["msg"] = msg

        def quit(self):
            pass

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    c = cfg.model_copy(deep=True)
    e = c.notify.email
    e.enabled, e.smtp_host, e.username, e.password = True, "smtp.example.com", "me@example.com", "pw"
    n = Notifier(lambda: c)
    n.requester(
        "guest@example.org",
        "Confirmed",
        "See you",
        ics=invite_ics("u1", NOW, NOW + timedelta(hours=1), "Meet"),
        wait=True,
    )
    msg = sent["msg"]
    assert sent["tls"] and sent["login"] == ("me@example.com", "pw") and msg["To"] == "guest@example.org"
    assert any(part.get_content_type() == "text/calendar" for part in msg.iter_attachments())


def test_failed_channel_reports_without_secrets(monkeypatch, cfg):
    def boom(url, **kw):
        raise httpx.ConnectError(f"cannot reach {url}")

    monkeypatch.setattr(httpx, "post", boom)
    c = cfg.model_copy(deep=True)
    c.notify.discord.enabled, c.notify.discord.webhook_url = True, "https://discord.com/api/webhooks/9/secret"
    res = Notifier(lambda: c).send("discord", Note("t", "b"))
    assert not res.ok and "secret" not in res.detail


def test_invite_file_shape():
    text = invite_ics("abc@glimpse", NOW, NOW + timedelta(minutes=30), "Meeting, with; Dana", "line1\nline2")
    assert "DTSTART:20261024T120000Z" in text and "SUMMARY:Meeting\\, with\\; Dana" in text
    assert "DESCRIPTION:line1\\nline2" in text and text.count("\r\n") > 5


# --- widget ---------------------------------------------------------------------------------------


def enable_widget(d, sites=None):
    d.audiences["coworkers"]["widget"] = {"enabled": True, "slug": WIDGET_SLUG, "sites": sites or []}


def test_widget_is_busy_only_and_embeddable(store):
    _, _, c = public(store)
    assert c.get(f"/w/{WIDGET_SLUG}").status_code == 404
    _, _, c = public(store, lambda d: enable_widget(d, ["https://lab.example.edu"]))
    r = c.get(f"/w/{WIDGET_SLUG}?tz={TZ}")
    assert r.status_code == 200
    assert "frame-ancestors https://lab.example.edu" in r.headers["Content-Security-Policy"]
    assert "X-Frame-Options" not in r.headers
    for word in ("Dentist", "Lab meeting", "Family", "Class"):
        assert word not in r.text
    assert 'class="w-b"' in r.text and "Request a meeting" not in r.text
    # Ordinary pages still refuse to be framed.
    assert c.get(f"/a/{COWORKER_SLUG}/week/{WEEK}").headers["X-Frame-Options"] == "DENY"


def test_widget_request_button_and_flow(store):
    def both(d):
        enable_widget(d)
        enable_requests(d)

    _, reqs, c = public(store, both)
    w = c.get(f"/w/{WIDGET_SLUG}?tz={TZ}")
    assert "frame-ancestors *" in w.headers["Content-Security-Policy"]
    link = re.search(r'href="(/w/[^"]+/request[^"]*)"', w.text).group(1).replace("&amp;", "&")
    page = c.get(link).text
    start = re.findall(r'name="start" value="([^"]+)"', page)[0]
    r = c.post(
        f"/w/{WIDGET_SLUG}/request",
        data={
            "ft": form_token(page),
            "mode": "slot",
            "len": "30",
            "start": start,
            "name": "Visitor",
            "email": "v@x.org",
            "tz": TZ,
        },
    )
    assert r.status_code == 303 and reqs.all()[0].viewer == "w:coworkers"


def test_widget_panel_section(panel):
    rt, c = panel
    csrf, rev = csrf_rev(c, "/audiences/coworkers")
    c.post(
        "/audiences/coworkers/widget",
        data={"csrf": csrf, "rev": rev, "enabled": "on", "sites": "https://www.uakron.edu", "days": "5"},
    )
    w = rt.cfg.audiences["coworkers"].widget
    assert w.enabled and w.slug and w.sites == ["https://www.uakron.edu"] and w.days == 5
    assert w.slug not in rt.store.config_path.read_text()
    html = c.get("/audiences/coworkers?tab=widget").text
    assert "&lt;iframe" in html and f"/w/{w.slug}" in html


# --- school mode ----------------------------------------------------------------------------------

ACADEMIC = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//Academic//EN
BEGIN:VEVENT
UID:1
DTSTART;VALUE=DATE:20260824
DTEND;VALUE=DATE:20260825
SUMMARY:Fall 2026 Classes Begin
END:VEVENT
BEGIN:VEVENT
UID:2
DTSTART;VALUE=DATE:20260907
DTEND;VALUE=DATE:20260908
SUMMARY:Labor Day - No Classes
END:VEVENT
BEGIN:VEVENT
UID:3
DTSTART;VALUE=DATE:20261025
DTEND;VALUE=DATE:20261102
SUMMARY:Fall Break
END:VEVENT
BEGIN:VEVENT
UID:4
DTSTART;VALUE=DATE:20261205
DTEND;VALUE=DATE:20261206
SUMMARY:Last Day of Classes
END:VEVENT
BEGIN:VEVENT
UID:5
DTSTART;VALUE=DATE:20261207
DTEND;VALUE=DATE:20261213
SUMMARY:Final Examinations
END:VEVENT
BEGIN:VEVENT
UID:6
DTSTART;VALUE=DATE:20270119
DTEND;VALUE=DATE:20270120
SUMMARY:First day of classes - Spring
END:VEVENT
BEGIN:VEVENT
UID:7
DTSTART;VALUE=DATE:20270315
DTEND;VALUE=DATE:20270322
SUMMARY:Spring Break (no classes)
END:VEVENT
BEGIN:VEVENT
UID:8
DTSTART;VALUE=DATE:20270507
DTEND;VALUE=DATE:20270508
SUMMARY:Last day of classes
END:VEVENT
BEGIN:VEVENT
UID:9
DTSTART;VALUE=DATE:20261015
DTEND;VALUE=DATE:20261016
SUMMARY:Last day to drop with a W
END:VEVENT
END:VCALENDAR
"""


def test_terms_are_read_from_an_academic_calendar(cfg):
    terms = read_feed(ACADEMIC, cfg, today=date(2026, 10, 1))
    assert [t.name for t in terms] == ["Fall 2026", "Spring 2027"]
    fall, spring = terms
    assert fall.start == date(2026, 8, 24) and fall.end == date(2026, 12, 12)
    kinds = {(p.name, p.kind) for p in fall.periods}
    assert ("Fall Break", "break") in kinds and ("Labor Day - No Classes", "break") in kinds
    assert ("Finals", "exams") in kinds
    assert spring.end == date(2027, 5, 7) and spring.periods[0].name.startswith("Spring Break")


def test_schedule_labels_and_pauses(cfg):
    sched = Schedule(terms=read_feed(ACADEMIC, cfg, today=date(2026, 10, 1)))
    assert sched.label(date(2026, 8, 24)) == "Fall 2026 · Week 1"
    assert sched.label(date(2026, 10, 21)) == "Fall 2026 · Week 9"
    assert sched.label(date(2026, 12, 8)) == "Fall 2026 · Finals"
    assert sched.label(date(2026, 12, 25)) == "Between terms"
    assert sched.classes_paused(date(2026, 10, 27)) and sched.classes_paused(date(2026, 12, 25))
    assert not sched.classes_paused(date(2026, 10, 21)) and not sched.classes_paused(date(2026, 12, 8))
    assert sched.label(date(2025, 1, 1)) is None  # before any known term: no guess


def school_on(tmp_path, feed=True):
    path = tmp_path / "academic.ics"
    path.write_text(ACADEMIC, encoding="utf-8")

    def mutate(d):
        node = d.section("school")
        node.update({"enabled": True, "class_calendars": ["classes"], "pause_classes": True})
        if feed:
            node["calendar_url"] = str(path)

    return mutate


def test_school_mode_pauses_classes_and_labels_weeks(store, tmp_path):
    g0, _, c0 = public(store)
    before = c0.get(f"/a/{COWORKER_SLUG}/week/{WEEK}").text
    assert ">Class<" in before or "Class" in before
    store.update(school_on(tmp_path))
    g = pin(Glimpse(store.current()))
    g.cache.refresh_all()
    with httpx.Client() as client:
        g.school.refresh(client)
    assert [t.name for t in g.school.feed_terms][:1] == ["Fall 2026"]
    c = TestClient(create_app(g.cfg, glimpse=g, background_refresh=False), follow_redirects=False)
    html = c.get(f"/a/{COWORKER_SLUG}/week/{WEEK}").text
    assert "Fall 2026" in html and "Fall Break" in html
    # The class during fall break is gone, other calendars are untouched.
    evs = [e for e in g.events_between(*week_range(WEEK, g.cfg)) if e.calendar_id == "classes"]
    assert evs == []


def test_typed_in_terms_win_and_viewers_can_be_kept_out(store, tmp_path):
    def mutate(d):
        school_on(tmp_path)(d)
        d.section("school")["terms"] = [
            {"name": "Autumn Term", "start": "2026-08-20", "end": "2026-12-15", "periods": []}
        ]
        d.section("school")["show_viewers"] = False

    store.update(mutate)
    g = pin(Glimpse(store.current()))
    with httpx.Client() as client:
        g.school.refresh(client)
    assert g.school.schedule.terms[0].name == "Autumn Term"
    assert len([t for t in g.school.schedule.terms if t.start.year == 2026]) == 1
    assert g.school.viewer_context(WEEK, WEEK + timedelta(days=7), WEEK) == {
        "term_label": None,
        "school_days": {},
    }


def test_school_panel_pages(panel, tmp_path):
    rt, c = panel
    assert c.get("/school").status_code == 200
    csrf, rev = csrf_rev(c, "/school")
    r = c.post(
        "/school/terms/add",
        data={"csrf": csrf, "name": "Fall 2026", "start": "2026-08-24", "end": "2026-12-12"},
    )
    assert r.status_code == 303 and rt.cfg.school.terms[0].name == "Fall 2026"
    csrf, rev = csrf_rev(c, "/school")
    c.post(
        "/school/terms/0/periods/add",
        data={
            "csrf": csrf,
            "name": "Thanksgiving",
            "kind": "break",
            "start": "2026-11-25",
            "end": "2026-11-29",
        },
    )
    assert rt.cfg.school.terms[0].periods[0].name == "Thanksgiving"
    csrf, rev = csrf_rev(c, "/school")
    c.post(
        "/school",
        data={
            "csrf": csrf,
            "rev": rev,
            "enabled": "on",
            "name": "University of Akron",
            "class_calendar": ["classes"],
            "pause_classes": "on",
            "show_viewers": "on",
        },
    )
    assert rt.cfg.school.enabled and rt.cfg.school.class_calendars == ["classes"]
    html = c.get("/school").text
    assert "Thanksgiving" in html and "University of Akron" in html


# --- support link ---------------------------------------------------------------------------------


def test_support_link_is_opt_in(store):
    _, _, c = public(store)
    assert "Support the project" not in c.get(f"/a/{COWORKER_SLUG}/week/{WEEK}").text
    _, _, c = public(store, lambda d: d.raw.__setitem__("support_link", True))
    assert "Support the project" in c.get(f"/a/{COWORKER_SLUG}/week/{WEEK}").text


def test_funding_file_and_readme_section():
    root = Path(__file__).resolve().parent.parent
    assert (root / ".github" / "FUNDING.yml").is_file()
    assert re.search(r"^## Support", (root / "README.md").read_text(encoding="utf-8"), re.M)


def test_new_admin_pages_render(panel):
    _, c = panel
    for path in ("/requests", "/notifications", "/school", "/audiences/coworkers", "/settings", "/"):
        assert c.get(path).status_code == 200, path


def test_date_ranges_across_years_keep_both_years():
    from glimpse.render.week import fmt_range

    assert fmt_range(date(2025, 10, 20), date(2027, 10, 24)) == "Oct 20, 2025 – Oct 24, 2027"


# --- 0.5.1: subscription leaves out shared calendars; event details ------------------------------


def test_feed_can_leave_out_calendars_viewers_already_have(store):
    from conftest import PARTNER_PASSWORD  # noqa: F401 - the partner feed needs no password

    from glimpse.web.auth import feed_token
    from glimpse.web.viewer import audience_viewer

    g, _, c = public(store)
    v = audience_viewer(g.cfg, "partner")
    url = f"/a/{PARTNER_SLUG}/feed/{feed_token(g.cfg, v)}.ics"
    before = c.get(url).text
    assert "Grandma visit" in before  # from the Family calendar
    store.update(lambda d: d.audiences["partner"].__setitem__("feed_skip", ["family"]))
    g, _, c = public(store)
    v = audience_viewer(g.cfg, "partner")
    after = c.get(f"/a/{PARTNER_SLUG}/feed/{feed_token(g.cfg, v)}.ics").text
    assert "Grandma visit" not in after and after.count("BEGIN:VEVENT") > 0
    # Their page still shows it.
    login = c.post(f"/a/{PARTNER_SLUG}/login", data={"password": "correct horse battery", "next": ""})
    assert login.status_code == 303
    assert "Grandma visit" in c.get(f"/a/{PARTNER_SLUG}/week/{WEEK}").text


def test_feed_skip_saved_from_audience_page(panel):
    rt, c = panel
    csrf, rev = csrf_rev(c, "/audiences/partner")
    html = c.get("/audiences/partner?tab=see").text
    assert 'name="feed_skip"' in html
    form = {
        "csrf": csrf,
        "rev": rev,
        "title": "Partner",
        "default": "full",
        "weeks_ahead": "52",
        "weeks_behind": "52",
        "view": ["week"],
        "allow_ics": "on",
        "feed_skip": ["family"],
    }
    assert c.post("/audiences/partner", data=form).status_code == 303
    assert rt.cfg.audiences["partner"].feed_skip == ["family"]


def test_events_carry_details_for_the_click_window(store):
    import html as htmllib

    _, _, c = public(store)
    page = c.get(f"/a/{COWORKER_SLUG}/week/{WEEK}").text
    blobs = [json.loads(htmllib.unescape(x)) for x in re.findall(r"data-ev='([^']*)'", page)]
    assert blobs and all({"title", "when"} <= set(b) for b in blobs)
    assert "/static/events.js" in page
    # Busy blocks reveal nothing more in the window than on the grid.
    for b in blobs:
        if b["title"] == "Busy":
            assert not (b["notes"] or b["loc"] or b["cal"])
    month = c.get(f"/a/{COWORKER_SLUG}/month/2026-10-01").text
    assert "data-ev='" in month
