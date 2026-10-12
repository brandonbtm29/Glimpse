import re

import pytest
from argon2 import PasswordHasher
from conftest import COWORKER_SLUG, FIXTURES, PARTNER_PASSWORD, PARTNER_SLUG, WEEK
from fastapi.testclient import TestClient

from glimpse.config import Level
from glimpse.store import ConfigStore
from glimpse.web.admin import create_admin_app, mask_url
from glimpse.web.app import create_app
from glimpse.web.runtime import Runtime

ADMIN_PW = "admin-password-123"


@pytest.fixture
def rt(config_path):
    store = ConfigStore(config_path)
    h = PasswordHasher().hash(ADMIN_PW)
    cfg = store.update(
        lambda d: d.set_secret(d.section("admin"), "password_hash", h, "GLIMPSE_ADMIN_PASSWORD_HASH")
    )
    cfg_secure_off = store.update(lambda d: d.section("admin").__setitem__("secure_cookies", False))
    r = Runtime(cfg_secure_off, store)
    r.refresh()
    assert cfg.admin.password_hash
    return r


@pytest.fixture
def admin(rt):
    return TestClient(create_admin_app(rt), follow_redirects=False)


@pytest.fixture
def public(rt):
    return TestClient(
        create_app(rt.cfg, glimpse=rt.glimpse, background_refresh=False), follow_redirects=False
    )


def sign_in(admin) -> str:
    r = admin.post("/login", data={"password": ADMIN_PW})
    assert r.status_code == 303
    return csrf_of(admin, "/")


def csrf_of(admin, path: str) -> str:
    html = admin.get(path).text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


# --- access --------------------------------------------------------------------------


def test_requires_login(admin):
    for path in ("/", "/calendars", "/sharing", "/preview", "/settings", "/audiences/partner"):
        r = admin.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path


def test_locked_until_admin_password_set(config_path):
    store = ConfigStore(config_path)
    rt = Runtime(store.current(), store)
    c = TestClient(create_admin_app(rt))
    assert "glimpse open" in c.get("/login").text
    assert c.post("/login", data={"password": "anything"}).status_code == 200  # setup page, no session
    assert "glimpse_admin" not in c.cookies


def test_wrong_password_and_rate_limit(admin):
    for _ in range(5):
        assert admin.post("/login", data={"password": "nope"}).status_code == 401
    assert admin.post("/login", data={"password": ADMIN_PW}).status_code == 429


def test_session_cookie_flags(admin):
    r = admin.post("/login", data={"password": ADMIN_PW})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_post_without_csrf_changes_nothing(admin, rt):
    sign_in(admin)
    r = admin.post("/audiences/partner/delete", data={})
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "partner" in rt.cfg.audiences


def test_tailscale_allowlist(admin, rt):
    rt.store.update(lambda d: d.section("admin").__setitem__("allowed_tailscale_users", ["me@example.com"]))
    rt.apply(rt.store.current(), fetch=False)
    assert admin.get("/login").status_code == 403
    assert admin.get("/login", headers={"Tailscale-User-Login": "someone@else.com"}).status_code == 403
    assert admin.get("/login", headers={"Tailscale-User-Login": "Me@Example.com"}).status_code == 200


def test_admin_headers(admin):
    r = admin.get("/login")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"


# --- pages never reveal feed links ------------------------------------------------------


def test_feed_links_are_masked(admin, rt):
    sign_in(admin)
    secret_url = "https://calendar.google.com/calendar/ical/x/private-TOPSECRET99/basic.ics"
    rt.store.update(lambda d: d.calendars["personal"].__setitem__("url", secret_url))
    rt.apply(rt.store.current(), fetch=False)
    for path in ("/", "/calendars", "/calendars/personal", "/sharing", "/settings"):
        assert "TOPSECRET" not in admin.get(path).text, path
    assert mask_url(secret_url) == "calendar.google.com/….ics"


# --- editing flows, live in the public app ---------------------------------------------------


def test_add_calendar_goes_live(admin, rt):
    csrf = sign_in(admin)
    r = admin.post(
        "/calendars/add",
        data={
            "csrf": csrf,
            "name": "Side work",
            "url": str(FIXTURES / "classes.ics"),
            "label": "",
            "color": "#123456",
        },
    )
    assert r.status_code == 303 and "ok=cal_added" in r.headers["location"]
    assert "side_work" in rt.cfg.calendars
    assert rt.glimpse.cache.states["side_work"].text  # fetched right away
    assert "Side work" in admin.get("/calendars").text


def test_sharing_matrix_changes_public_view(admin, public, rt):
    csrf = sign_in(admin)
    url = f"/a/{COWORKER_SLUG}/week/{WEEK.isoformat()}"
    assert "POLY" not in public.get(url).text
    form = {"csrf": csrf}
    for aid in rt.cfg.audiences:
        form[f"default__{aid}"] = rt.cfg.audiences[aid].default.value
        for cid in rt.cfg.calendars:
            form[f"lvl__{aid}__{cid}"] = "default"
    form["lvl__coworkers__classes"] = "title"
    r = admin.post("/sharing", data=form)
    assert r.status_code == 303
    assert "POLY 701" in public.get(url).text  # live, no restart


def test_audience_create_password_rotate_delete(admin, public, rt):
    csrf = sign_in(admin)
    r = admin.post("/audiences/add", data={"csrf": csrf, "title": "Lab group", "default": "busy"})
    assert r.headers["location"].startswith("/audiences/lab_group")
    slug = rt.cfg.audiences["lab_group"].slug
    assert public.get(f"/a/{slug}").status_code == 303

    csrf = csrf_of(admin, "/audiences/lab_group")
    r = admin.post(
        "/audiences/lab_group/password",
        data={"csrf": csrf, "action": "set", "password": "lab-pass-123", "password2": "lab-pass-123"},
    )
    assert "pw_set" in r.headers["location"]
    page = public.get(f"/a/{slug}/week/{WEEK.isoformat()}")
    assert page.status_code == 303 and "/login" in page.headers["location"]

    admin.post("/audiences/lab_group/rotate", data={"csrf": csrf})
    assert public.get(f"/a/{slug}").status_code == 404  # old link dead
    new_slug = rt.cfg.audiences["lab_group"].slug
    assert new_slug != slug and public.get(f"/a/{new_slug}").status_code == 303

    admin.post("/audiences/lab_group/delete", data={"csrf": csrf})
    assert "lab_group" not in rt.cfg.audiences
    assert public.get(f"/a/{new_slug}").status_code == 404


def test_audience_hours_validation_and_save(admin, rt):
    csrf = sign_in(admin)
    base = {"csrf": csrf, "title": "Partner", "default": "full", "weeks_ahead": "6", "weeks_behind": "1"}
    r = admin.post("/audiences/partner", data={**base, "limit_hours": "on"})
    assert r.status_code == 400 and "at least one day" in r.text
    r = admin.post(
        "/audiences/partner",
        data={
            **base,
            "limit_hours": "on",
            "day": ["sat", "sun"],
            "start": "09:00",
            "end": "21:00",
            "allow_ics": "on",
        },
    )
    assert r.status_code == 303
    aud = rt.cfg.audiences["partner"]
    assert aud.hours.days == ["sat", "sun"] and aud.hours.end.hour == 21
    assert aud.allow_ics and not aud.allow_excel and aud.weeks_ahead == 6


def test_preview_shows_exact_audience_view(admin):
    sign_in(admin)
    html = admin.get(f"/preview?view_as=coworkers&week={WEEK.isoformat()}").text
    assert "Lab work" in html and "Busy" in html
    for word in ("Doctor", "Grandma", "Dinner with Sam", "POLY"):
        assert word not in html.split('<nav class="tabs"')[1], word
    everything = admin.get(f"/preview?view_as=_all&week={WEEK.isoformat()}").text
    assert "Dinner with Sam" in everything and "Grandma visit" in everything


def test_settings_save_and_self_lockout_guard(admin, rt):
    csrf = sign_in(admin)
    data = {
        "csrf": csrf,
        "timezone": "America/Chicago",
        "week_starts": "sunday",
        "refresh_minutes": "15",
        "base_url": "https://my-pc.tail1234.ts.net/",
        "private_tag": "#p",
        "hide_tag": "#h",
        "allowed_users": "",
    }
    assert admin.post("/settings", data=data).status_code == 303
    c = rt.cfg
    assert c.timezone == "America/Chicago" and c.week_starts == "sunday" and c.refresh_minutes == 15
    assert c.server.base_url == "https://my-pc.tail1234.ts.net" and not c.overrides.honor_private_flag
    r = admin.post(
        "/settings",
        data={**data, "allowed_users": "other@example.com"},
        headers={"Tailscale-User-Login": "me@example.com"},
    )
    assert r.status_code == 400 and "lock yourself out" in r.text
    assert rt.cfg.admin.allowed_tailscale_users == []


def test_change_admin_password_keeps_session(admin, rt):
    csrf = sign_in(admin)
    r = admin.post(
        "/settings/password",
        data={
            "csrf": csrf,
            "current": ADMIN_PW,
            "password": "brand-new-pass-1",
            "password2": "brand-new-pass-1",
        },
    )
    assert r.status_code == 303
    assert admin.get("/").status_code == 200  # still signed in
    admin.cookies.clear()
    assert admin.post("/login", data={"password": ADMIN_PW}).status_code == 401
    assert admin.post("/login", data={"password": "brand-new-pass-1"}).status_code == 303


def test_hand_edits_are_picked_up(admin, rt):
    sign_in(admin)
    path = rt.store.config_path
    path.write_text(path.read_text().replace("title: Brandon's week", "title: Edited by hand"))
    admin.get("/")
    assert rt.cfg.audiences["partner"].title == "Edited by hand"
    path.write_text(path.read_text().replace("default: full", "default: everything"))
    html = admin.get("/").text
    assert "has a problem" in html and rt.cfg.audiences["partner"].default.value == "full"


# --- phase 2: groups, rules, duplicates ---------------------------------------------------


def test_group_create_and_set_level(admin, rt):
    c = sign_in(admin)
    r = admin.post("/groups/add", data={"csrf": c, "name": "Work", "calendar": ["lab", "classes"]})
    assert r.status_code == 303
    assert rt.cfg.groups["work"].calendars == ["lab", "classes"]
    html = admin.get("/sharing").text
    assert 'name="grp__coworkers__work"' in html and "Group (" not in html
    form = {
        "csrf": c,
        "default__partner": "full",
        "default__coworkers": "busy",
        "grp__coworkers__work": "full",
    }
    form |= {"lvl__coworkers__lab": "default", "lvl__coworkers__classes": "default"}
    assert admin.post("/sharing", data=form).status_code == 303
    aud = rt.cfg.audiences["coworkers"]
    assert aud.groups["work"] is Level.FULL and "lab" not in aud.calendars
    assert rt.cfg.level_for(aud, "classes") is Level.FULL
    assert "Group (full)" in admin.get("/sharing").text
    # edit the group: drop classes
    admin.post("/groups/work", data={"csrf": c, "name": "Job", "calendar": ["lab"]})
    assert rt.cfg.groups["work"].name == "Job" and rt.cfg.groups["work"].calendars == ["lab"]
    # calendar page picker moves it back
    admin.post("/calendars/classes", data={"csrf": c, "name": "Classes", "group": "work", "color": "#3b6fd8"})
    assert rt.cfg.group_of("classes") == "work"
    admin.post("/groups/work/delete", data={"csrf": c})
    assert rt.cfg.groups == {} and rt.cfg.audiences["coworkers"].groups == {}


def test_rules_page_add_test_and_delete(admin, rt):
    c = sign_in(admin)
    assert "Add your first rule" in admin.get("/rules").text
    form = {
        "csrf": c,
        "name": "Health",
        "match": "doctor, clinic",
        "field": ["title", "location"],
        "action": "busy",
    }
    r = admin.post("/rules/test", data=form)
    assert r.status_code == 200 and "Doctor appointment" in r.text  # owner-only preview of matches
    assert rt.cfg.rules == []  # testing doesn't save
    assert admin.post("/rules/add", data=form).status_code == 303
    rule = rt.cfg.rules[0]
    assert rule.match == ["doctor", "clinic"] and rule.fields == ["title", "location"]
    page = admin.get("/rules").text
    assert "Health" in page and "Catches" in page
    # turn it off (hidden field only, checkbox unticked)
    admin.post("/rules/0", data={**form, "enabled": "off"})
    assert rt.cfg.rules[0].enabled is False
    assert admin.post("/rules/add", data={**form, "match": "  "}).status_code == 400
    assert admin.post("/rules/add", data={**form, "field": []}).status_code == 400
    admin.post("/rules/0/delete", data={"csrf": c})
    assert rt.cfg.rules == []


def test_rule_applies_to_public_view_immediately(admin, public, rt):
    c = sign_in(admin)
    admin.post("/rules/add", data={"csrf": c, "match": "lab work", "field": ["title"], "action": "hidden"})
    html = public.get(f"/a/{COWORKER_SLUG}/week/{WEEK.isoformat()}").text
    assert "Lab work" not in html


def test_merge_duplicates_setting_and_preview_badge(admin, rt, public):
    c = sign_in(admin)
    s = admin.get("/settings").text
    assert 'name="merge_duplicates" checked' in s
    form = {"csrf": c, "timezone": "America/New_York", "week_starts": "monday", "refresh_minutes": "10"}
    form |= {"private_tag": "#private", "hide_tag": "#hide", "honor_private_flag": "on"}
    admin.post("/settings", data=form)
    assert rt.cfg.overrides.merge_duplicates is False
    admin.post("/settings", data={**form, "merge_duplicates": "on"})
    assert rt.cfg.overrides.merge_duplicates is True


def test_duplicate_calendar_merged_and_badge_only_in_preview(admin, rt, public):
    c = sign_in(admin)
    url = str(FIXTURES / "personal.ics")
    admin.post("/calendars/add", data={"csrf": c, "name": "Personal copy", "url": url, "color": "#123456"})
    rt.refresh()
    w = WEEK.isoformat()
    prev = admin.get(f"/preview?view_as=_all&week={w}").text
    assert 'class="copies"' in prev and "×2" in prev
    public.post(f"/a/{PARTNER_SLUG}/login", data={"password": PARTNER_PASSWORD, "next": w})
    html = public.get(f"/a/{PARTNER_SLUG}/week/{w}").text
    assert 'class="copies"' not in html
    assert html.count(">Dinner with Sam<") == 2  # once in the grid, once in the phone list
