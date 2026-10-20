# ruff: noqa: F811 - the panel/store fixtures are imported from test_v050
"""0.9.0: a shorter menu with tabs, and the audience page split into tabs."""

from __future__ import annotations

import re

from test_v050 import csrf_rev, make_request, panel, store  # noqa: F401


def sidebar(html: str) -> list[str]:
    nav = re.search(r'<nav class="sidenav".*?</nav>', html, re.S).group(0)
    return [
        " ".join(re.sub(r"<.*?>", " ", a).split())
        for a in re.findall(r'<a href="[^"]+"[^>]*>(.*?)</a>', nav, re.S)
    ]


def tabs(html: str) -> list[str]:
    m = re.search(r'<nav class="section-tabs".*?</nav>', html, re.S)
    return re.findall(r'<a href="([^"]+)"', m.group(0)) if m else []


def current_tab(html: str) -> str | None:
    m = re.search(r'<nav class="section-tabs".*?</nav>', html, re.S)
    hit = m and re.search(r'href="([^"]+)" aria-current="page"', m.group(0))
    return hit.group(1) if hit else None


def test_sidebar_has_five_sections(panel):
    rt, c = panel
    make_request(rt)
    html = c.get("/").text
    items = sidebar(html)
    assert items[:2] == ["Overview", "Calendars"] and "Sharing" in items and "Settings" in items
    assert "Meetings 1" in items  # the waiting request shows on Meetings
    for gone in ("People", "Privacy rules", "Video calls", "Notifications", "School", "Setup"):
        assert gone not in items
    assert tabs(html) == []  # the overview has no tabs


def test_each_page_shows_its_sections_tabs(panel):
    _, c = panel
    expect = {
        "/calendars": ["/calendars", "/my-calendars", "/school"],
        "/school": ["/calendars", "/my-calendars", "/school"],
        "/people": ["/sharing", "/people", "/rules", "/preview"],
        "/preview": ["/sharing", "/people", "/rules", "/preview"],
        "/video-calls": ["/requests", "/video-calls", "/notifications"],
        "/setup": ["/settings", "/setup"],
    }
    for path, want in expect.items():
        html = c.get(path).text
        assert tabs(html) == want, path
        assert current_tab(html) == path, path
    html = c.get("/settings").text
    assert ">This computer<" in html and ">General<" in html
    assert re.search(r'href="/sharing" aria-current="page">Sharing', c.get("/rules").text)


def test_detail_pages_use_a_breadcrumb_not_tabs(panel):
    _, c = panel
    for path in ("/audiences/coworkers", "/calendars/lab"):
        html = c.get(path).text
        assert tabs(html) == [] and 'class="crumb"' in html, path
    assert 'href="/sharing" aria-current="page"' in c.get("/audiences/coworkers").text


def test_audience_page_tabs(panel):
    _, c = panel
    base = "/audiences/coworkers"
    html = c.get(base).text
    assert "<h2>Shared link</h2>" in html and "<h2>Delete audience</h2>" in html
    assert "<h2>What they see</h2>" not in html and "<h2>Meeting requests</h2>" not in html
    assert f'href="{base}?tab=access" aria-current="page"' in html
    see = c.get(f"{base}?tab=see").text
    assert "<h2>What they see</h2>" in see and "<h2>Shared link</h2>" not in see
    req = c.get(f"{base}?tab=requests").text
    assert "<h2>Meeting requests</h2>" in req and "<h2>Website widget</h2>" not in req
    assert re.search(r"Meeting requests <span class=\"status ok\">On</span>", req)  # requests are on
    assert "<h2>Website widget</h2>" in c.get(f"{base}?tab=widget").text
    assert "<h2>Shared link</h2>" in c.get(f"{base}?tab=nonsense").text


def test_saving_returns_to_the_same_tab(panel):
    rt, c = panel
    base = "/audiences/coworkers"
    csrf, rev = csrf_rev(c, f"{base}?tab=widget")
    r = c.post(f"{base}/widget", data={"csrf": csrf, "rev": rev, "enabled": "on", "days": "5"})
    assert r.headers["location"].startswith(f"{base}?tab=widget")
    csrf, _ = csrf_rev(c, base)
    r = c.post(f"{base}/password", data={"csrf": csrf, "password": "short", "password2": "short"})
    assert r.status_code == 400 and "<h2>Password</h2>" in r.text and "8 characters" in r.text
    csrf, _ = csrf_rev(c, f"{base}?tab=requests")
    r = c.post(f"{base}/requests", data={"csrf": csrf, "enabled": "on"})
    assert r.status_code == 400 and "<h2>Meeting requests</h2>" in r.text
