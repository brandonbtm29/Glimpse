"""0.4.1: colours, emoji shapes, group board, default privacy rules with exceptions,
day view, downloads and the About section."""

from __future__ import annotations

import json
import re
from datetime import timedelta

import pytest
from argon2 import PasswordHasher
from conftest import COWORKER_SLUG, PARTNER_PASSWORD, PARTNER_SLUG, WEEK
from fastapi.testclient import TestClient

from kalends.config import ConfigError
from kalends.core import Kalends
from kalends.policy import BUSY_TITLE
from kalends.store import ConfigStore
from kalends.web.admin import PALETTE_HEX, create_admin_app
from kalends.web.app import create_app
from kalends.web.runtime import Runtime

W = WEEK.isoformat()
ADMIN_PW = "admin-password-123"


@pytest.fixture
def store(config_path):
    return ConfigStore(config_path)


def public(store, mutate=None):
    if mutate:
        store.update(mutate)
    g = Kalends(store.current())
    g.cache.refresh_all()
    g.today = lambda: WEEK
    return g, TestClient(create_app(g.cfg, kalends=g, background_refresh=False), follow_redirects=False)


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


def csrf_rev(c, path):
    html = c.get(path).text
    rev = re.search(r'name="rev" value="([^"]+)"', html)
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1), rev.group(1) if rev else ""


# --- colours and shapes ----------------------------------------------------------------------


def test_twelve_distinct_palette_colours():
    assert len(set(PALETTE_HEX)) == 12


def test_new_calendar_gets_an_unused_colour(panel):
    rt, c = panel
    html = c.get("/calendars").text
    default = re.search(r'name="color" value="(#[0-9a-f]{6})"', html).group(1)
    used = {(cal.color or PALETTE_HEX[i]).lower() for i, cal in enumerate(rt.cfg.calendars.values())}
    assert default not in used


def test_recolour_gives_every_calendar_its_own_colour(panel):
    rt, c = panel
    csrf, _ = csrf_rev(c, "/calendars")
    assert c.post("/recolor-calendars", data={"csrf": csrf}).status_code == 303
    colours = [cal.color for cal in rt.cfg.calendars.values()]
    assert len(set(colours)) == len(colours) == 4


def test_emoji_marker(store):
    g, c = public(store, lambda d: d.calendars["lab"].__setitem__("marker", "\U0001f52c"))
    c.post(f"/a/{PARTNER_SLUG}/login", data={"password": PARTNER_PASSWORD, "next": W})
    html = c.get(f"/a/{PARTNER_SLUG}/week/{W}").text
    assert '<span class="mk" aria-hidden="true">\U0001f52c</span>' in html


@pytest.mark.parametrize("bad", ["skull", "<b>", "abc", "\U0001f52c" * 9])
def test_marker_validation(store, bad):
    with pytest.raises(ConfigError):
        store.update(lambda d: d.calendars["lab"].__setitem__("marker", bad))


def test_typed_emoji_wins_in_the_form(panel):
    rt, c = panel
    csrf, rev = csrf_rev(c, "/calendars/lab")
    form = {
        "csrf": csrf,
        "rev": rev,
        "name": "Lab",
        "color": "#2f9e63",
        "marker": "star",
        "marker_custom": "\U0001f9ec",
    }
    assert c.post("/calendars/lab", data=form).status_code == 303
    assert rt.cfg.calendars["lab"].marker == "\U0001f9ec"
    csrf, rev = csrf_rev(c, "/calendars/lab")
    r = c.post("/calendars/lab", data={**form, "csrf": csrf, "rev": rev, "marker_custom": "nope"})
    assert r.status_code == 400 and "single emoji" in r.text


# --- group board -----------------------------------------------------------------------------


def test_board_moves_calendars(panel):
    rt, c = panel
    csrf, _ = csrf_rev(c, "/calendars")
    c.post("/groups/add", data={"csrf": csrf, "name": "Work"})
    html = c.get("/calendars").text
    assert 'class="board"' in html and 'data-gid="work"' in html and 'data-cid="lab"' in html
    assert c.post("/api/calendar-group", data={"cid": "lab", "gid": "work"}).status_code == 403
    r = c.post("/api/calendar-group", data={"cid": "lab", "gid": "work"}, headers={"X-CSRF": csrf})
    assert json.loads(r.text)["ok"] and rt.cfg.group_of("lab") == "work"
    r = c.post("/api/calendar-group", data={"cid": "lab", "gid": "ghost"}, headers={"X-CSRF": csrf})
    assert r.status_code == 400
    # the menu on each card works without JavaScript
    c.post("/calendars/lab/group", data={"csrf": csrf, "group": ""})
    assert rt.cfg.group_of("lab") is None


def test_group_settings_form_keeps_members(panel):
    rt, c = panel
    csrf, _ = csrf_rev(c, "/calendars")
    c.post("/groups/add", data={"csrf": csrf, "name": "Work", "calendar": ["lab", "classes"]})
    csrf, rev = csrf_rev(c, "/calendars")
    c.post("/groups/work", data={"csrf": csrf, "rev": rev, "name": "Job", "marker": "\U0001f4bc"})
    grp = rt.cfg.groups["work"]
    assert grp.name == "Job" and grp.calendars == ["lab", "classes"] and grp.marker == "\U0001f4bc"


# --- privacy rules ---------------------------------------------------------------------------


def test_rule_exceptions_per_audience(store):
    def setup(d):
        d.set_rule(None, {"name": "Night out", "match": ["dinner"], "except_audiences": ["coworkers"]})

    g, c = public(store, setup)
    partner = g.week(g.cfg.audiences["partner"], WEEK)
    assert "Dinner with Sam" not in [e.title for e in partner] and BUSY_TITLE in [e.title for e in partner]


def test_rule_exception_keeps_details_for_that_audience(store):
    g, _ = public(store, lambda d: d.set_rule(None, {"match": ["dinner"], "except_audiences": ["partner"]}))
    assert "Dinner with Sam" in [e.title for e in g.week(g.cfg.audiences["partner"], WEEK)]


def test_removing_an_audience_cleans_rule_exceptions(store):
    store.update(lambda d: d.set_rule(None, {"match": ["x"], "except_audiences": ["coworkers"]}))
    cfg = store.update(lambda d: d.remove_audience("coworkers"))
    assert cfg.rules[0].except_audiences == []


def test_suggested_rules_button(panel):
    rt, c = panel
    html = c.get("/rules").text
    assert "Add suggested rules" in html and "Health" in html
    csrf, _ = csrf_rev(c, "/rules")
    c.post("/rules/suggested", data={"csrf": csrf})
    assert [r.name for r in rt.cfg.rules] == ["Health", "Job search", "Money and legal"]
    assert "Add suggested rules" not in c.get("/rules").text
    c.post("/rules/suggested", data={"csrf": csrf})
    assert len(rt.cfg.rules) == 3  # not added twice


def test_rule_form_saves_exceptions(panel):
    rt, c = panel
    csrf, _ = csrf_rev(c, "/rules")
    c.post(
        "/rules/add",
        data={"csrf": csrf, "match": "doctor", "field": ["title"], "action": "busy", "except": ["partner"]},
    )
    assert rt.cfg.rules[0].except_audiences == ["partner"]
    assert "not for" in c.get("/rules").text


# --- day view --------------------------------------------------------------------------------


def all_views(d):
    for a in d.audiences.values():
        a["views"] = ["day", "week", "month", "quarter", "year"]


def test_day_view(store):
    g, c = public(store, all_views)
    c.post(f"/a/{PARTNER_SLUG}/login", data={"password": PARTNER_PASSWORD, "next": W})
    day = (WEEK + timedelta(days=2)).isoformat()  # Wed Oct 28
    html = c.get(f"/a/{PARTNER_SLUG}/day/{day}").text
    assert "Wed Oct 28, 2026" in html and "Dinner with Sam" in html and "--days: 1" in html
    assert "Lab work" in html and "Grandma visit" not in html  # Tuesday's event isn't here
    assert ">Day<" in html
    assert f'/day/{(WEEK + timedelta(days=1)).isoformat()}"' in html  # previous day link


def test_day_view_respects_hours_and_bounds(store):
    def setup(d):
        all_views(d)
        d.audiences["coworkers"].update(weeks_ahead=0, weeks_behind=0)

    g, c = public(store, setup)
    sat = (WEEK + timedelta(days=5)).isoformat()
    html = c.get(f"/a/{COWORKER_SLUG}/day/{sat}").text
    assert "Only Mon, Tue, Wed, Thu, Fri are shared here." in html and "Saturday experiment" not in html
    r = c.get(f"/a/{COWORKER_SLUG}/day/{(WEEK + timedelta(days=9)).isoformat()}")
    assert r.status_code == 303 and r.headers["location"].endswith(
        f"/day/{(WEEK + timedelta(days=6)).isoformat()}"
    )


# --- downloads and About -------------------------------------------------------------------------


def test_download_links_and_headers(store):
    g, c = public(store, all_views)
    week = c.get(f"/a/{COWORKER_SLUG}/week/{W}").text
    assert "Download this week (Excel)" in week and " download>" in week
    month = c.get(f"/a/{COWORKER_SLUG}/month/2026-10-01").text
    assert "Download this month (Excel)" in month
    r = c.get(f"/a/{COWORKER_SLUG}/excel-month/2026-10-01")
    assert r.headers["content-disposition"] == 'attachment; filename="schedule-2026-10.xlsx"'
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats")


def test_about_and_get_your_own(store):
    g, c = public(store, lambda d: d.raw.__setitem__("owner_name", "Brandon"))
    html = c.get(f"/a/{COWORKER_SLUG}/week/{W}").text
    assert "<summary>About Kalends</summary>" in html and "Brandon chose how much you see" in html
    assert 'href="https://github.com/brandonbtm29/Kalends"' in html and "Get started with Kalends" in html
