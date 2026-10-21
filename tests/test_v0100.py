# ruff: noqa: F811 - the panel/store fixtures are imported from test_v050
"""0.10.0: your own view ("My calendar"): everything, locked until it has a password."""

from __future__ import annotations

import re

import pytest
from conftest import WEEK
from fastapi.testclient import TestClient
from test_v050 import csrf_rev, panel, store  # noqa: F401

from glimpse.config import ConfigError
from glimpse.web.app import create_app
from glimpse.web.auth import feed_token

W = WEEK.isoformat()
PW = "my own long password"


def with_self(rt):
    cfg = rt.store.update(lambda d: d.ensure_self())
    rt.apply(cfg, fetch=False)
    rt.refresh()
    return rt.cfg.self_audience


def public(rt):
    g = rt.glimpse
    return TestClient(create_app(rt.cfg, glimpse=g, background_refresh=False), follow_redirects=False)


def test_ensure_self_is_first_secret_and_only_once(store):
    store.update(lambda d: d.ensure_self())
    store.update(lambda d: d.ensure_self())
    cfg = store.current()
    aid, aud = cfg.self_audience
    assert list(cfg.audiences)[0] == aid == "me" and aud.self_view
    assert aud.default.value == "full" and aud.weeks_ahead is None and aud.password_hash is None
    assert aud.slug not in store.config_path.read_text() and aud.slug in store.env_path.read_text()
    assert list(cfg.shared_audiences) == ["partner", "coworkers"]
    with pytest.raises(ConfigError):
        store.update(lambda d: d.audiences["partner"].update({"self_view": True}))


def test_self_view_ignores_levels_rules_and_hours(store):
    def mutate(d):
        d.ensure_self()
        d.audiences["me"].update(
            {"default": "busy", "hours": {"days": ["mon"], "start": "09:00", "end": "10:00"}}
        )
        d.audiences["me"]["calendars"] = {"lab": "title", "family": "hidden"}

    store.update(mutate)
    aud = store.current().audiences["me"]
    assert aud.default.value == "full" and aud.hours is None
    assert {k: v.value for k, v in aud.calendars.items()} == {"family": "hidden"}  # only "leave out" stays
    with pytest.raises(ConfigError):
        store.update(
            lambda d: (
                d.people["alex"].update({"audience": "me"})
                if "alex" in d.people
                else d.add_person("Al", "me")
            )
        )


def test_locked_until_it_has_a_password(panel):
    rt, c = panel
    aid, aud = with_self(rt)
    pub = public(rt)
    assert pub.get(f"/a/{aud.slug}").status_code == 404
    assert pub.get(f"/a/{aud.slug}/feed/{feed_token(rt.cfg, aid)}.ics").status_code == 404
    html = c.get(f"/audiences/{aid}").text
    assert "Set a password below to switch on" in html
    assert "Delete audience" not in html and "<h2>People</h2>" not in html and "Remove password" not in html
    assert re.findall(
        r'href="/audiences/me\?tab=(\w+)"', re.search(r'<nav class="tabs".*?</nav>', html, re.S).group(0)
    ) == ["access", "see"]
    csrf, _ = csrf_rev(c, f"/audiences/{aid}")
    c.post(
        f"/audiences/{aid}/password", data={"csrf": csrf, "action": "set", "password": PW, "password2": PW}
    )
    assert rt.cfg.audiences[aid].password_hash
    assert (
        c.post(f"/audiences/{aid}/delete", data={"csrf": csrf}).status_code == 303 and aid in rt.cfg.audiences
    )


def test_self_page_shows_everything_in_full(panel):
    rt, c = panel
    aid, _ = with_self(rt)
    csrf, _ = csrf_rev(c, f"/audiences/{aid}")
    c.post(
        f"/audiences/{aid}/password", data={"csrf": csrf, "action": "set", "password": PW, "password2": PW}
    )
    slug = rt.cfg.audiences[aid].slug
    pub = public(rt)
    assert "login" in pub.get(f"/a/{slug}/week/{W}").headers["location"]
    pub.post(f"/a/{slug}/login", data={"password": PW, "next": f"/a/{slug}/week/{W}"})
    page = pub.get(f"/a/{slug}/week/{W}").text
    assert "Doctor appointment" in page  # tagged #private: Busy for everyone else, but not for you
    feed = pub.get(f"/a/{slug}/feed/{feed_token(rt.cfg, aid)}.ics")
    assert feed.status_code == 200 and "Doctor appointment" in feed.text


def test_what_you_see_tab_leaves_out_calendars_and_feed_skip(panel):
    rt, c = panel
    aid, _ = with_self(rt)
    html = c.get(f"/audiences/{aid}?tab=see").text
    assert (
        "Calendars on your page" in html
        and 'name="default"' not in html
        and "Only show certain days" not in html
    )
    csrf, rev = csrf_rev(c, f"/audiences/{aid}?tab=see")
    cals = [cid for cid in rt.cfg.calendars if cid != "family"]
    form = {
        "csrf": csrf,
        "rev": rev,
        "title": "Everything",
        "show_cal": cals,
        "view": ["week", "month"],
        "default_view": "week",
        "allow_ics": "on",
        "feed_skip": ["lab"],
        "weeks_ahead_all": "on",
        "weeks_behind_all": "on",
    }
    r = c.post(f"/audiences/{aid}", data=form)
    assert r.status_code == 303
    aud = rt.cfg.audiences[aid]
    assert aud.title == "Everything" and {k: v.value for k, v in aud.calendars.items()} == {
        "family": "hidden"
    }
    assert aud.feed_skip == ["lab"] and aud.shared_link and aud.default.value == "full"
    # the Who sees what matrix leaves your own view alone
    csrf, rev = csrf_rev(c, "/sharing")
    c.post("/sharing", data={"csrf": csrf, "rev": rev})
    assert {k: v.value for k, v in rt.cfg.audiences[aid].calendars.items()} == {"family": "hidden"}


def test_self_is_kept_out_of_sharing_lists(panel):
    rt, c = panel
    aid, aud = with_self(rt)
    sharing = c.get("/sharing").text
    assert "always shows everything, so it isn" in sharing
    assert f'<h2><a href="/audiences/{aid}">' not in sharing
    people = c.get("/people").text
    assert f'<option value="{aid}"' not in people
    nav = re.search(r'<nav class="sidenav".*?</nav>', c.get("/").text, re.S).group(0)
    assert f'href="/audiences/{aid}"' in nav  # still easy to reach from the menu
    assert aud.title in c.get("/preview").text
