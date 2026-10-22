"""0.4.0: shapes, group names, fuller legend, browse limits, welcome banner, audiences hub,
save conflicts and live refresh."""

from __future__ import annotations

import json
import re
from datetime import timedelta

import pytest
from argon2 import PasswordHasher
from conftest import COWORKER_SLUG, PARTNER_PASSWORD, PARTNER_SLUG, WEEK
from fastapi.testclient import TestClient

from kalends.config import ALL_WEEKS, MARKERS
from kalends.core import Kalends, allowed_week_bounds
from kalends.store import ConfigStore
from kalends.web.admin import create_admin_app
from kalends.web.app import create_app
from kalends.web.runtime import Runtime

W = WEEK.isoformat()
ADMIN_PW = "admin-password-123"


def public(store, mutate=None):
    if mutate:
        store.update(mutate)
    g = Kalends(store.current())
    g.cache.refresh_all()
    g.today = lambda: WEEK
    return g, TestClient(create_app(g.cfg, kalends=g, background_refresh=False), follow_redirects=False)


@pytest.fixture
def store(config_path):
    return ConfigStore(config_path)


def partner_html(c, path=f"week/{W}"):
    c.post(f"/a/{PARTNER_SLUG}/login", data={"password": PARTNER_PASSWORD, "next": W})
    return c.get(f"/a/{PARTNER_SLUG}/{path}").text


# --- shapes ---------------------------------------------------------------------------------


def test_calendar_shape_shows_on_events_and_legend(store):
    g, c = public(store, lambda d: d.calendars["lab"].__setitem__("marker", "triangle"))
    html = partner_html(c)
    tri = MARKERS["triangle"]
    assert f'<span class="mk-key" aria-hidden="true">{tri}</span>Lab' in html
    assert f'<span class="mk" aria-hidden="true">{tri}</span>' in html  # on Lab events
    cw = c.get(f"/a/{COWORKER_SLUG}/week/{W}").text
    assert tri in cw  # coworkers see Lab at title level, so the shape too


def test_busy_blocks_never_carry_a_shape(store):
    g, c = public(store, lambda d: d.calendars["personal"].__setitem__("marker", "star"))
    cw = c.get(f"/a/{COWORKER_SLUG}/week/{W}").text  # personal is busy for coworkers
    assert MARKERS["star"] not in cw


def test_bad_marker_rejected(store):
    from kalends.config import ConfigError

    with pytest.raises(ConfigError):
        store.update(lambda d: d.calendars["lab"].__setitem__("marker", "skull"))


# --- legend ---------------------------------------------------------------------------------


def test_legend_lists_quiet_calendars_faded(store):
    g, c = public(store)
    html = partner_html(c, f"week/{(WEEK + timedelta(weeks=8)).isoformat()}")  # mostly empty week
    legend = html.split('<ul class="legend">')[1].split("</ul>")[0]
    for name in ("Personal", "Lab", "Classes", "Family"):
        assert name in legend, name
    assert "quiet" in legend


def test_legend_never_names_hidden_or_busy_calendars(store):
    g, c = public(store)
    legend = c.get(f"/a/{COWORKER_SLUG}/week/{W}").text.split('<ul class="legend">')[1].split("</ul>")[0]
    assert "Family" not in legend and "Personal" not in legend  # hidden / busy for coworkers
    assert "Lab" in legend and "Class" in legend


# --- group names ----------------------------------------------------------------------------


def grouped(d, names=True):
    gid = d.add_group("Work")
    d.set_calendar_group("lab", gid)
    d.set_calendar_group("classes", gid)
    d.groups[gid]["marker"] = "diamond"
    d.audiences["coworkers"]["group_names"] = names
    d.audiences["coworkers"]["calendars"] = {"lab": "title", "classes": "title", "family": "hidden"}


def test_audience_can_see_group_names(store):
    g, c = public(store, grouped)
    html = c.get(f"/a/{COWORKER_SLUG}/week/{W}").text
    legend = html.split('<ul class="legend">')[1].split("</ul>")[0]
    assert "Work" in legend and ">Lab<" not in legend and "Classes" not in legend
    assert MARKERS["diamond"] in legend
    assert "Lab work" in html  # titles still shown at the title level
    # the hover text names the group, not the calendar
    assert "· Work" in html and "· Lab" not in html


def test_group_names_off_keeps_calendar_names(store):
    g, c = public(store, lambda d: grouped(d, names=False))
    legend = c.get(f"/a/{COWORKER_SLUG}/week/{W}").text.split('<ul class="legend">')[1].split("</ul>")[0]
    assert "Lab" in legend and "Classes" in legend and "Work" not in legend


def test_group_label_used_at_label_level(store):
    def setup(d):
        grouped(d)
        d.groups["work"]["label"] = "At work"
        d.audiences["coworkers"]["calendars"] = {"lab": "label", "classes": "label", "family": "hidden"}

    g, c = public(store, setup)
    html = c.get(f"/a/{COWORKER_SLUG}/week/{W}").text
    assert "At work" in html and "Lab work" not in html and "Class<" not in html


# --- browse limits ----------------------------------------------------------------------------


def test_all_weeks_and_zero(store):
    def setup(d):
        d.audiences["coworkers"].update(weeks_ahead=None, weeks_behind=0)

    g, c = public(store, setup)
    aud = g.cfg.audiences["coworkers"]
    lo, hi = allowed_week_bounds(aud, WEEK, g.cfg)
    assert lo == WEEK and hi == WEEK + timedelta(weeks=ALL_WEEKS)
    far = (WEEK + timedelta(weeks=150)).isoformat()
    assert c.get(f"/a/{COWORKER_SLUG}/week/{far}").status_code == 200
    r = c.get(f"/a/{COWORKER_SLUG}/week/{(WEEK - timedelta(weeks=1)).isoformat()}")
    assert r.status_code == 303 and r.headers["location"].endswith(W)


def test_feed_is_capped_even_with_all_weeks(store):
    from kalends.web.auth import feed_token

    g, c = public(store, lambda d: d.audiences["coworkers"].update(weeks_ahead=None, weeks_behind=None))
    body = c.get(f"/a/{COWORKER_SLUG}/feed/{feed_token(g.cfg, 'coworkers')}.ics").text
    years = {int(y) for y in re.findall(r"DTSTART[^:]*:(\d{4})", body)}
    assert years and max(years) <= WEEK.year + 2 and min(years) >= WEEK.year - 1


# --- welcome banner ---------------------------------------------------------------------------


def test_welcome_banner(store):
    def setup(d):
        d.raw["owner_name"] = "Brandon"
        d.add_person("Alex", "coworkers")

    g, c = public(store, setup)
    html = c.get(f"/a/{COWORKER_SLUG}/week/{W}").text
    assert "Welcome!" in html and "Brandon&#39;s schedule, shared with you" in html
    person = g.cfg.people["alex"].slug
    assert "Welcome, Alex!" in c.get(f"/p/{person}/week/{W}").text


# --- panel -----------------------------------------------------------------------------------


@pytest.fixture
def panel(store):
    store.update(
        lambda d: d.set_secret(
            d.section("admin"),
            "password_hash",
            PasswordHasher().hash(ADMIN_PW),
            "KALENDS_ADMIN_PASSWORD_HASH",
        )
    )
    store.update(lambda d: d.section("admin").__setitem__("secure_cookies", False))
    rt = Runtime(store.current(), store)
    rt.refresh()
    c = TestClient(create_admin_app(rt), follow_redirects=False)
    c.post("/login", data={"password": ADMIN_PW})
    return rt, c


def tokens(c, path):
    html = c.get(path).text
    csrf = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    rev = re.search(r'name="rev" value="([^"]+)"', html)
    return csrf, rev.group(1) if rev else None


def test_calendars_page_title_is_clean(panel):
    rt, c = panel
    title = re.search(r"<title>(.*?)</title>", c.get("/calendars").text, re.S).group(1)
    assert title == "Calendars · Kalends settings"


def test_audiences_hub_and_sidebar(panel):
    rt, c = panel
    html = c.get("/sharing").text
    assert 'class="aud-cards"' in html and "+ New audience" in html
    assert html.count('href="/audiences/partner"') >= 2  # card + sidebar
    assert 'class="sub" href="/audiences/coworkers"' in c.get("/").text


def test_conflicting_save_is_refused(panel):
    rt, c = panel
    csrf, rev = tokens(c, "/settings")
    assert rev
    # someone else saves in the meantime
    rt.store.update(lambda d: d.raw.__setitem__("owner_name", "Other device"))
    form = {
        "csrf": csrf,
        "rev": rev,
        "timezone": "America/New_York",
        "week_starts": "monday",
        "refresh_minutes": "10",
        "private_tag": "#private",
        "hide_tag": "#hide",
        "owner_name": "Me",
    }
    r = c.post("/settings", data=form, headers={"referer": "http://testserver/settings"})
    assert r.status_code == 409 and "changed somewhere else" in r.text and 'href="/settings"' in r.text
    assert rt.store.current().owner_name == "Other device"
    csrf, rev = tokens(c, "/settings")  # after reloading, the save goes through
    assert c.post("/settings", data={**form, "csrf": csrf, "rev": rev}).status_code == 303
    assert rt.cfg.owner_name == "Me"


def test_calendar_marker_and_group_settings_saved(panel):
    rt, c = panel
    csrf, rev = tokens(c, "/calendars/lab")
    c.post(
        "/calendars/lab", data={"csrf": csrf, "rev": rev, "name": "Lab", "color": "#2f9e63", "marker": "star"}
    )
    assert rt.cfg.calendars["lab"].marker == "star"
    csrf, _ = tokens(c, "/calendars")
    c.post("/groups/add", data={"csrf": csrf, "name": "Work", "calendar": ["lab", "classes"]})
    csrf, rev = tokens(c, "/calendars")
    c.post(
        "/groups/work",
        data={
            "csrf": csrf,
            "rev": rev,
            "name": "Work",
            "calendar": ["lab", "classes"],
            "label": "Busy at work",
            "use_color": "on",
            "color": "#123456",
            "marker": "heart",
        },
    )
    grp = rt.cfg.groups["work"]
    assert (grp.label, grp.color, grp.marker) == ("Busy at work", "#123456", "heart")


def test_audience_all_weeks_and_group_names_form(panel):
    rt, c = panel
    csrf, rev = tokens(c, "/audiences/coworkers")
    form = {
        "csrf": csrf,
        "rev": rev,
        "title": "Availability",
        "default": "busy",
        "weeks_ahead": "6",
        "weeks_behind": "0",
        "weeks_ahead_all": "on",
        "group_names": "on",
        "view": ["week"],
        "shared_link": "on",
    }
    assert c.post("/audiences/coworkers", data=form).status_code == 303
    aud = rt.cfg.audiences["coworkers"]
    assert aud.weeks_ahead is None and aud.weeks_behind == 0 and aud.group_names
    assert (
        "any week" in c.get("/audiences/coworkers").text
        or "all future weeks" in c.get("/audiences/coworkers").text
    )


def test_live_refresh_api(panel):
    rt, c = panel
    csrf, _ = tokens(c, "/")
    assert c.post("/api/refresh/lab").status_code == 403  # needs the CSRF header
    r = c.post("/api/refresh/lab", headers={"X-CSRF": csrf})
    data = json.loads(r.text)
    assert r.status_code == 200 and data["ok"] and "count" in data
    assert c.post("/api/refresh/nope", headers={"X-CSRF": csrf}).status_code == 404
    assert 'data-cid="lab"' in c.get("/").text and "data-live-refresh" in c.get("/").text
