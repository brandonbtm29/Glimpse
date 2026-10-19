# ruff: noqa: F811 - the panel/store fixtures are imported from test_v050
"""0.7.0: a new Zoom, Teams or Google Meet meeting for each booking."""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from conftest import COWORKER_SLUG
from test_v050 import NOW, TZ, csrf_rev, form_token, make_request, panel, store  # noqa: F401
from test_v060 import public_for

from glimpse import conferencing
from glimpse.availability import open_slots
from glimpse.config import ConfigError

SECRET = "zoom-secret-value-123"
G_SECRET = "google-secret-value-456"


def id_token(**claims) -> str:
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"x.{body}.y"


class Services:
    """Pretends to be Zoom, Microsoft and Google, and records what Glimpse asked for."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.fail: set[str] = set()
        self.teams_pending = 1
        self.n = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        url = str(req.url)
        form = parse_qs(req.content.decode()) if req.method == "POST" and b"=" in req.content else {}
        if url.startswith(conferencing.ZOOM_TOKEN):
            if "zoom_token" in self.fail:
                return httpx.Response(
                    400, json={"reason": "Invalid client_id or client_secret", "error": "invalid_client"}
                )
            return httpx.Response(200, json={"access_token": "zoom-at", "expires_in": 3599})
        if url.startswith(f"{conferencing.ZOOM_API}/users/"):
            if "zoom" in self.fail:
                return httpx.Response(400, json={"code": 300, "message": "Invalid start_time"})
            self.n += 1
            return httpx.Response(
                201, json={"id": 8800 + self.n, "join_url": f"https://zoom.us/j/8800{self.n}"}
            )
        if url.startswith(f"{conferencing.ZOOM_API}/meetings/"):
            return httpx.Response(204)
        if url.startswith(conferencing.GOOGLE_TOKEN):
            if form.get("grant_type") == ["authorization_code"]:
                return httpx.Response(
                    200,
                    json={
                        "access_token": "g-at",
                        "refresh_token": "g-rt",
                        "expires_in": 3599,
                        "scope": conferencing.GOOGLE_SCOPES,
                        "id_token": id_token(email="brandon@gmail.com"),
                    },
                )
            return httpx.Response(200, json={"access_token": "g-at2", "expires_in": 3599})
        if url.startswith(conferencing.GOOGLE_REVOKE):
            return httpx.Response(200)
        if url == f"{conferencing.MEET_API}/spaces":
            return httpx.Response(
                200, json={"name": "spaces/abc", "meetingUri": "https://meet.google.com/abc-defg-hij"}
            )
        if "/oauth2/v2.0/devicecode" in url:
            return httpx.Response(
                200,
                json={
                    "device_code": "dev-code",
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://microsoft.com/devicelogin",
                    "expires_in": 900,
                    "interval": 5,
                },
            )
        if "/oauth2/v2.0/token" in url:
            if form.get("grant_type") == ["refresh_token"]:
                return httpx.Response(
                    200, json={"access_token": "ms-at", "refresh_token": "ms-rt-2", "expires_in": 3599}
                )
            if self.teams_pending:
                self.teams_pending -= 1
                return httpx.Response(400, json={"error": "authorization_pending"})
            return httpx.Response(
                200,
                json={
                    "access_token": "ms-at",
                    "refresh_token": "ms-rt",
                    "expires_in": 3599,
                    "id_token": id_token(preferred_username="bm@uakron.edu"),
                },
            )
        if url == f"{conferencing.GRAPH}/me/onlineMeetings":
            return httpx.Response(
                201, json={"id": "teams-1", "joinWebUrl": "https://teams.microsoft.com/l/meetup-join/1"}
            )
        if url.startswith(f"{conferencing.GRAPH}/me/onlineMeetings/"):
            return httpx.Response(204)
        return httpx.Response(404, json={"error": "unexpected " + url})

    def hits(self, method: str, start: str) -> list[httpx.Request]:
        return [c for c in self.calls if c.method == method and str(c.url).startswith(start)]


@pytest.fixture
def services(monkeypatch):
    svc = Services()
    monkeypatch.setattr(conferencing, "client", lambda: httpx.Client(transport=httpx.MockTransport(svc)))
    return svc


def setup_zoom(rt, **extra):
    def mutate(d):
        node = d.section("video_calls").setdefault("zoom", {})
        node.update(
            {"enabled": True, "account_id": "acct1", "client_id": "cid1", "user": "me@example.com"} | extra
        )
        d.set_secret(node, "client_secret", SECRET, "GLIMPSE_ZOOM_CLIENT_SECRET")

    rt.apply(rt.store.update(mutate), fetch=False)


def setup_google(rt):
    def mutate(d):
        node = d.section("video_calls").setdefault("google", {})
        node.update({"enabled": True, "client_id": "gcid.apps.googleusercontent.com"})
        d.set_secret(node, "client_secret", G_SECRET, "GLIMPSE_GOOGLE_CLIENT_SECRET")

    rt.apply(rt.store.update(mutate), fetch=False)
    rt.glimpse.video.accounts.set(
        "google",
        {"refresh_token": "g-rt", "client_id": "gcid.apps.googleusercontent.com", "account": "b@gmail.com"},
    )


def setup_teams(rt, sign_in=True):
    def mutate(d):
        d.section("video_calls").setdefault("teams", {}).update({"enabled": True, "client_id": "ms-app-id"})

    rt.apply(rt.store.update(mutate), fetch=False)
    if sign_in:
        rt.glimpse.video.accounts.set(
            "teams", {"refresh_token": "ms-rt", "client_id": "ms-app-id", "account": "bm@uakron.edu"}
        )


def offer(rt, platforms, **extra):
    def mutate(d):
        d.audiences["coworkers"]["requests"].update({"platforms": platforms, **extra})

    rt.apply(rt.store.update(mutate), fetch=False)


def book(pub, platform=None, start=None):
    page = pub.get(f"/a/{COWORKER_SLUG}/request?len=30").text
    start = start or re.findall(r'name="start" value="([^"]+)"', page)[0]
    data = {
        "ft": form_token(page),
        "mode": "slot",
        "len": "30",
        "start": start,
        "name": "Dana",
        "email": "dana@uakron.edu",
        "tz": TZ,
    }
    if platform:
        data["platform"] = platform
    return page, pub.post(f"/a/{COWORKER_SLUG}/request", data=data)


# --- settings ----------------------------------------------------------------------------------------


def test_video_settings_defaults_and_validation(store):
    cfg = store.current()
    assert not cfg.video_calls.zoom.enabled and cfg.video_calls.teams.tenant == "organizations"
    assert cfg.audiences["coworkers"].requests.platforms == []
    with pytest.raises(ConfigError):
        store.update(
            lambda d: d.section("video_calls").setdefault("teams", {}).update({"tenant": "bad tenant!"})
        )
    with pytest.raises(ConfigError):
        store.update(
            lambda d: d.audiences["coworkers"].setdefault("requests", {}).update({"platforms": ["skype"]})
        )


def test_panel_saves_secrets_to_env_and_shows_status(panel):
    rt, c = panel
    csrf, rev = csrf_rev(c, "/video-calls")
    r = c.post(
        "/video-calls/zoom",
        data={
            "csrf": csrf,
            "rev": rev,
            "enabled": "on",
            "account_id": "acct1",
            "client_id": "cid1",
            "client_secret": SECRET,
            "user": "me@example.com",
            "waiting_room": "on",
        },
    )
    assert r.status_code == 303
    z = rt.cfg.video_calls.zoom
    assert z.enabled and z.client_secret == SECRET and z.waiting_room
    assert SECRET not in rt.store.config_path.read_text()
    assert SECRET in rt.store.env_path.read_text()
    html = c.get("/video-calls").text
    assert "Connected" in html and SECRET not in html and "Saved (paste to change)" in html
    # the audience page offers it, and saving the form keeps the choice
    html = c.get("/audiences/coworkers").text
    assert 'name="platform" value="zoom"' in html


def test_only_connected_services_are_offered(panel, services):
    rt, c = panel
    setup_teams(rt, sign_in=False)
    offer(rt, ["teams", "zoom"])
    page = public_for(rt).get(f"/a/{COWORKER_SLUG}/request").text
    assert 'name="platform"' not in page  # neither is ready: nothing to choose
    setup_zoom(rt)
    page = public_for(rt).get(f"/a/{COWORKER_SLUG}/request").text
    assert 'type="hidden" name="platform" value="zoom"' in page and "The meeting is on Zoom" in page
    setup_teams(rt)
    page = public_for(rt).get(f"/a/{COWORKER_SLUG}/request").text
    assert "How would you like to meet?" in page and 'value="teams"' in page


# --- booking makes, and cancelling removes, a meeting ----------------------------------------------


def test_instant_booking_makes_a_zoom_meeting_and_cancel_deletes_it(panel, services):
    rt, c = panel
    setup_zoom(rt)
    offer(rt, ["zoom"], auto_confirm=True, meeting_link="https://zoom.us/j/fixed")
    pub = public_for(rt)
    _, r = book(pub)
    assert r.status_code == 303
    item = rt.requests.all()[0]
    assert item.status == "accepted" and item.platform == "zoom"
    assert item.link == "https://zoom.us/j/88001" and item.meeting_id == "8801" and not item.meeting_note
    token = services.hits("POST", conferencing.ZOOM_TOKEN)[0]
    assert token.headers["authorization"].startswith("Basic ") and b"account_id=acct1" in token.content
    made = services.hits("POST", f"{conferencing.ZOOM_API}/users/me@example.com/meetings")[0]
    body = json.loads(made.content)
    assert body["duration"] == 30 and body["start_time"].endswith("Z") and body["settings"]["waiting_room"]
    assert made.headers["authorization"] == "Bearer zoom-at"
    status = pub.get(f"/req/{item.token}").text
    assert "Join the meeting on Zoom" in status and "https://zoom.us/j/88001" in status
    # cancelling (by the requester) deletes the Zoom meeting
    pub.post(f"/req/{item.token}", data={"action": "cancel", "ft": form_token(status)})
    assert rt.requests.get(item.id).status == "cancelled"
    assert services.hits("DELETE", f"{conferencing.ZOOM_API}/meetings/8801")


def test_a_failed_meeting_falls_back_to_the_fixed_link(panel, services):
    rt, c = panel
    setup_zoom(rt)
    offer(rt, ["zoom"], auto_confirm=True, meeting_link="https://zoom.us/j/fixed")
    owner = []
    rt.notifier.owner = lambda note, **k: owner.append(note)
    services.fail.add("zoom")
    pub = public_for(rt)
    _, r = book(pub)
    item = rt.requests.all()[0]
    assert item.status == "accepted" and item.link == "https://zoom.us/j/fixed" and not item.meeting_id
    assert "Invalid start_time" in item.meeting_note
    html = c.get("/requests").text
    assert "Invalid start_time" in html and "fixed link was sent instead" in html


def test_requester_picks_teams_and_owner_accepts(panel, services):
    rt, c = panel
    setup_zoom(rt)
    setup_teams(rt)
    offer(rt, ["zoom", "teams"])
    pub = public_for(rt)
    book(pub, platform="teams")
    item = rt.requests.all()[0]
    assert item.status == "pending" and item.platform == "teams" and not item.link
    status = pub.get(f"/req/{item.token}").text
    assert "On Teams" in status
    html = c.get("/requests").text
    assert "Make a new Teams meeting" in html
    rt.notifier.requester = lambda *a, **k: None
    csrf, _ = csrf_rev(c, "/requests")
    c.post(
        f"/requests/{item.id}/answer",
        data={"csrf": csrf, "action": "accept", "call_choice": "1", "new_call": "on", "meeting_link": ""},
    )
    item = rt.requests.get(item.id)
    assert item.link == "https://teams.microsoft.com/l/meetup-join/1" and item.meeting_id == "teams-1"
    made = services.hits("POST", f"{conferencing.GRAPH}/me/onlineMeetings")[0]
    assert json.loads(made.content)["subject"] == "Meeting with Dana"
    # Microsoft rotated the refresh token; the new one is kept
    assert rt.glimpse.video.accounts.get("teams")["refresh_token"] == "ms-rt-2"
    csrf, _ = csrf_rev(c, "/requests")
    c.post(f"/requests/{item.id}/answer", data={"csrf": csrf, "action": "cancel"})
    assert services.hits("DELETE", f"{conferencing.GRAPH}/me/onlineMeetings/teams-1")


def test_owner_can_send_their_own_link_instead(panel, services):
    rt, c = panel
    setup_google(rt)
    offer(rt, ["google"])
    free = open_slots(rt.glimpse, rt.cfg.audiences["coworkers"], 30, NOW)[0]
    item = make_request(rt, start=free.start.isoformat(), end=free.end.isoformat(), platform="google")
    rt.notifier.requester = lambda *a, **k: None
    csrf, _ = csrf_rev(c, "/requests")
    c.post(
        f"/requests/{item.id}/answer",
        data={
            "csrf": csrf,
            "action": "accept",
            "call_choice": "1",
            "meeting_link": "https://meet.google.com/my-room",
        },
    )
    item = rt.requests.get(item.id)
    assert item.link == "https://meet.google.com/my-room" and not item.meeting_id
    assert not services.hits("POST", f"{conferencing.MEET_API}/spaces")


def test_google_meet_space_and_access(panel, services):
    rt, _ = panel
    setup_google(rt)
    m = rt.glimpse.video.create("google", "Test", NOW, NOW.replace(hour=13), "")
    assert m.url == "https://meet.google.com/abc-defg-hij" and m.id == "spaces/abc"
    req = services.hits("POST", f"{conferencing.MEET_API}/spaces")[0]
    assert json.loads(req.content) == {"config": {"accessType": "TRUSTED"}}
    refresh = services.hits("POST", conferencing.GOOGLE_TOKEN)[0]
    assert b"grant_type=refresh_token" in refresh.content
    rt.glimpse.video.delete("google", m.id)  # nothing to delete on Google; no call made
    assert not [c for c in services.calls if c.method == "DELETE"]


# --- signing in ----------------------------------------------------------------------------------------


def test_google_sign_in_round_trip(panel, services):
    rt, c = panel
    setup_google(rt)
    rt.glimpse.video.accounts.drop("google")
    html = c.get("/video-calls").text
    assert "Sign in with Google" in html and "/video-calls/google/callback" in html
    csrf, _ = csrf_rev(c, "/video-calls")
    assert c.get("/video-calls/google/connect?csrf=wrong").headers["location"] == "/video-calls"
    r = c.get(f"/video-calls/google/connect?csrf={csrf}")
    loc = urlparse(r.headers["location"])
    q = parse_qs(loc.query)
    assert loc.netloc == "accounts.google.com" and q["access_type"] == ["offline"]
    assert "meetings.space.created" in q["scope"][0] and q["redirect_uri"][0].endswith(
        "/video-calls/google/callback"
    )
    # a made-up state is refused, so no one can attach their own account with a link
    bad = c.get("/video-calls/google/callback?state=forged&code=x")
    assert "google_stale" in bad.headers["location"] and not rt.glimpse.video.accounts.get("google")
    ok = c.get(f"/video-calls/google/callback?state={q['state'][0]}&code=auth-code")
    assert "google_on" in ok.headers["location"]
    acct = rt.glimpse.video.accounts.get("google")
    assert acct["refresh_token"] == "g-rt" and acct["account"] == "brandon@gmail.com"
    assert rt.glimpse.video.ready("google")
    saved = (rt.store.config_path.parent / conferencing.FILE).read_text()
    assert "g-rt" in saved and "g-rt" not in rt.store.config_path.read_text()
    assert "brandon@gmail.com" in c.get("/video-calls").text
    csrf, _ = csrf_rev(c, "/video-calls")
    c.post("/video-calls/google/disconnect", data={"csrf": csrf})
    assert not rt.glimpse.video.ready("google") and services.hits("POST", conferencing.GOOGLE_REVOKE)


def test_teams_device_sign_in(panel, services, monkeypatch):
    rt, c = panel
    setup_teams(rt, sign_in=False)
    assert rt.glimpse.video.status("teams") == "sign_in"
    csrf, _ = csrf_rev(c, "/video-calls")
    c.post("/video-calls/teams/connect", data={"csrf": csrf})
    html = c.get("/video-calls").text  # first check: still waiting
    assert "ABCD-1234" in html and 'http-equiv="refresh"' in html
    rt.glimpse.video.pending_teams["next"] = 0
    r = c.get("/video-calls")
    assert r.status_code == 303 and "teams_on" in r.headers["location"]
    assert rt.glimpse.video.ready("teams") and rt.glimpse.video.account("teams") == "bm@uakron.edu"
    poll = [x for x in services.calls if "/oauth2/v2.0/token" in str(x.url)][-1]
    assert b"device_code=dev-code" in poll.content and "/organizations/" in str(poll.url)


def test_errors_are_plain_and_keep_secrets_out(panel, services):
    rt, c = panel
    setup_zoom(rt)
    services.fail.add("zoom_token")
    csrf, _ = csrf_rev(c, "/video-calls")
    html = c.post("/video-calls/zoom/test", data={"csrf": csrf}).text
    assert "Test failed: Zoom didn&#39;t accept the app details" in html and "invalid_client" in html
    assert SECRET not in html


def test_test_meeting_is_made_and_removed(panel, services):
    rt, c = panel
    setup_zoom(rt)
    csrf, _ = csrf_rev(c, "/video-calls")
    html = c.post("/video-calls/zoom/test", data={"csrf": csrf}).text
    assert "It works" in html
    assert services.hits("DELETE", f"{conferencing.ZOOM_API}/meetings/8801")


def test_accounts_file_survives_restart(tmp_path: Path):
    p = tmp_path / conferencing.FILE
    a = conferencing.Accounts(p)
    a.set("teams", {"refresh_token": "r1", "client_id": "x"})
    a.set("bogus", {"x": 1})
    b = conferencing.Accounts(p)
    assert b.get("teams")["refresh_token"] == "r1" and b.get("bogus") == {}
    b.drop("teams")
    assert conferencing.Accounts(p).get("teams") == {}


def test_backups_include_sign_ins(tmp_path: Path):
    from glimpse.updates import backup_settings

    cfgp = tmp_path / "config.yaml"
    cfgp.write_text("x")
    (tmp_path / conferencing.FILE).write_text("{}")
    dest = backup_settings(cfgp, tmp_path / ".env")
    assert (dest / conferencing.FILE).is_file()


def test_zoom_times_are_sent_in_utc():
    assert conferencing._utc(datetime(2026, 10, 27, 10, 0, tzinfo=UTC)) == "2026-10-27T10:00:00Z"
