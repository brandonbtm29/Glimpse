# ruff: noqa: F811 - the panel/store fixtures are imported from test_v050
"""0.8.0: Webex and Jitsi Meet, and sign-ins that renew themselves."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from conftest import COWORKER_SLUG
from test_v050 import csrf_rev, panel, store  # noqa: F401
from test_v060 import public_for
from test_v070 import Services, book, offer, setup_teams

from glimpse import conferencing
from glimpse.config import ConfigError

W_SECRET = "webex-secret-value-789"


class MoreServices(Services):
    def __call__(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.startswith(conferencing.WEBEX_TOKEN):
            self.calls.append(req)
            form = parse_qs(req.content.decode())
            if form.get("grant_type") == ["authorization_code"]:
                return httpx.Response(
                    200,
                    json={
                        "access_token": "w-at",
                        "expires_in": 1209599,
                        "refresh_token": "w-rt",
                        "refresh_token_expires_in": 7775999,
                        "scope": "meeting:schedules_read meeting:schedules_write spark:kms",
                    },
                )
            return httpx.Response(
                200, json={"access_token": "w-at2", "expires_in": 1209599, "refresh_token": "w-rt"}
            )
        if url == f"{conferencing.WEBEX_API}/meetings":
            self.calls.append(req)
            return httpx.Response(
                200,
                json={
                    "id": "wx123",
                    "webLink": "https://acme.webex.com/meet/j?MTID=wx123",
                    "hostEmail": "b@acme.com",
                },
            )
        if url.startswith(f"{conferencing.WEBEX_API}/meetings/"):
            self.calls.append(req)
            return httpx.Response(204)
        return super().__call__(req)


@pytest.fixture
def services(monkeypatch):
    svc = MoreServices()
    monkeypatch.setattr(conferencing, "client", lambda: httpx.Client(transport=httpx.MockTransport(svc)))
    return svc


def setup_webex(rt, sign_in=True):
    def mutate(d):
        node = d.section("video_calls").setdefault("webex", {})
        node.update({"enabled": True, "client_id": "wcid"})
        d.set_secret(node, "client_secret", W_SECRET, "GLIMPSE_WEBEX_CLIENT_SECRET")

    rt.apply(rt.store.update(mutate), fetch=False)
    if sign_in:
        rt.glimpse.video.accounts.set("webex", {"refresh_token": "w-rt", "client_id": "wcid"})


def setup_jitsi(rt, server=None):
    def mutate(d):
        node = d.section("video_calls").setdefault("jitsi", {})
        node["enabled"] = True
        if server:
            node["server"] = server

    rt.apply(rt.store.update(mutate), fetch=False)


def test_jitsi_server_must_be_https(store):
    assert store.current().video_calls.jitsi.server == "https://meet.jit.si"
    with pytest.raises(ConfigError):
        store.update(
            lambda d: d.section("video_calls").setdefault("jitsi", {}).update({"server": "http://x.org"})
        )
    store.update(
        lambda d: (
            d.section("video_calls").setdefault("jitsi", {}).update({"server": "https://jitsi.uakron.edu/"})
        )
    )
    assert store.current().video_calls.jitsi.server == "https://jitsi.uakron.edu"


def test_jitsi_rooms_are_new_random_and_need_nothing(panel, services):
    rt, c = panel
    setup_jitsi(rt)
    assert rt.glimpse.video.ready("jitsi")
    offer(rt, ["jitsi"], auto_confirm=True)
    pub = public_for(rt)
    book(pub)
    first = rt.requests.all()[0]
    assert (
        re.fullmatch(r"https://meet\.jit\.si/Glimpse[a-z2-9]{20}", first.link) and first.platform == "jitsi"
    )
    assert not services.calls  # no service to talk to
    other = rt.glimpse.video.create("jitsi", "x", first.start_dt, first.end_dt)
    assert other.url != first.link
    status = pub.get(f"/req/{first.token}").text
    assert "Join the meeting on Jitsi" in status
    # the panel saves another server, and a test just shows a room
    csrf, rev = csrf_rev(c, "/video-calls")
    c.post(
        "/video-calls/jitsi",
        data={"csrf": csrf, "rev": rev, "enabled": "on", "server": "https://jitsi.example.org"},
    )
    assert rt.cfg.video_calls.jitsi.server == "https://jitsi.example.org"
    csrf, _ = csrf_rev(c, "/video-calls")
    html = c.post("/video-calls/jitsi/test", data={"csrf": csrf}).text
    assert (
        "It works" in html and "https://jitsi.example.org/Glimpse" in html and "deleted it again" not in html
    )


def test_webex_sign_in_book_and_cancel(panel, services):
    rt, c = panel
    setup_webex(rt, sign_in=False)
    assert rt.glimpse.video.status("webex") == "sign_in"
    assert W_SECRET not in rt.store.config_path.read_text()
    html = c.get("/video-calls").text
    assert "Sign in with Webex" in html and "/video-calls/webex/callback" in html
    csrf, _ = csrf_rev(c, "/video-calls")
    r = c.get(f"/video-calls/webex/connect?csrf={csrf}")
    loc = urlparse(r.headers["location"])
    q = parse_qs(loc.query)
    assert loc.netloc == "webexapis.com" and q["scope"] == [conferencing.WEBEX_SCOPES]
    # a Google sign-in state can't finish a Webex sign-in
    g = parse_qs(urlparse(c.get(f"/video-calls/webex/connect?csrf={csrf}").headers["location"]).query)[
        "state"
    ][0]
    assert "signin_stale" in c.get(f"/video-calls/google/callback?state={g}&code=x").headers["location"]
    ok = c.get(f"/video-calls/webex/callback?state={q['state'][0]}&code=abc")
    assert "webex_on" in ok.headers["location"] and rt.glimpse.video.ready("webex")
    # book it
    offer(rt, ["webex", "zoom"], auto_confirm=True)
    pub = public_for(rt)
    page, _ = book(pub)
    assert "How would you like to meet?" not in page  # zoom isn't set up, so only Webex is offered
    item = rt.requests.all()[0]
    assert item.link == "https://acme.webex.com/meet/j?MTID=wx123" and item.meeting_id == "wx123"
    body = json.loads(services.hits("POST", f"{conferencing.WEBEX_API}/meetings")[0].content)
    assert body["start"].endswith("Z") and body["timezone"] == "UTC"
    assert rt.glimpse.video.account("webex") == "b@acme.com"
    status = pub.get(f"/req/{item.token}").text
    pub.post(
        f"/req/{item.token}",
        data={"action": "cancel", "ft": re.search(r'name="ft" value="([^"]+)"', status).group(1)},
    )
    assert services.hits("DELETE", f"{conferencing.WEBEX_API}/meetings/wx123")


def test_sign_ins_are_renewed_weekly(panel, services):
    rt, _ = panel
    setup_webex(rt)
    setup_teams(rt)
    video = rt.glimpse.video
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat(timespec="seconds")
    fresh = datetime.now(UTC).isoformat(timespec="seconds")
    video.accounts.set("webex", video.accounts.get("webex") | {"refreshed": old})
    video.accounts.set("teams", video.accounts.get("teams") | {"refreshed": fresh})
    video.keep_alive()
    assert [str(x.url) for x in services.calls if "token" in str(x.url)] == [conferencing.WEBEX_TOKEN]
    assert video.accounts.get("webex")["refreshed"] > old
    video.keep_alive()  # nothing due now
    assert len([x for x in services.calls if "token" in str(x.url)]) == 1


def test_video_calls_page_has_no_mangled_text(panel):
    _, c = panel
    html = c.get("/video-calls").text
    assert html.count("Settings panel address") == 1 and "Link in notifications" not in html
    for name in ("Zoom", "Microsoft Teams", "Google Meet", "Webex", "Jitsi Meet"):
        assert f"<h2>{name}</h2>" in html
    assert "/video-calls/webex/callback" in html


def test_audience_offers_all_five(panel):
    rt, c = panel
    setup_jitsi(rt)
    html = c.get("/audiences/coworkers").text
    for p in conferencing.PLATFORMS:
        assert f'name="platform" value="{p}"' in html
    page = c.get("/audiences/coworkers").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    form = {"csrf": csrf, "enabled": "on", "length": ["30"], "slots": "on", "platform": ["jitsi", "webex"]}
    r = c.post("/audiences/coworkers/requests", data=form)
    assert r.status_code == 303
    assert rt.cfg.audiences["coworkers"].requests.platforms == ["webex", "jitsi"]
    assert "Jitsi Meet" in public_for(rt).get(f"/a/{COWORKER_SLUG}/request").text
