"""A new online meeting for each booking: Zoom, Microsoft Teams, Google Meet, Webex and Jitsi Meet.

Each service is set up once in the settings panel:

* **Zoom**: a Server-to-Server OAuth app (account ID, client ID, client secret). No sign-in step;
  Glimpse asks Zoom for a short-lived token whenever it needs one.
* **Google Meet**: an OAuth client in Google Cloud. You sign in once in the panel; Glimpse keeps
  the refresh token and makes meeting spaces with the Meet REST API.
* **Microsoft Teams**: an app registration in Microsoft Entra. You sign in once with a code
  (device sign-in, so no redirect address is needed); work or school accounts only, because
  Microsoft's meeting API doesn't support personal accounts.
* **Webex**: an integration made at developer.webex.com. You sign in once (like Google).
* **Jitsi Meet**: nothing to set up. Each meeting is a new room with a long random name on
  meet.jit.si or your own Jitsi server.

Sign-ins that use refresh tokens are renewed about weekly (``keep_alive``) so they don't lapse
when nobody books for a while.

Sign-in tokens live in ``video-accounts.json`` beside the config (never in config.yaml, and git
ignores the file). Errors are reported in plain words without tokens or secrets.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .sources import USER_AGENT

log = logging.getLogger("glimpse.video")

FILE = "video-accounts.json"
PLATFORMS = {
    "zoom": "Zoom",
    "teams": "Microsoft Teams",
    "google": "Google Meet",
    "webex": "Webex",
    "jitsi": "Jitsi Meet",
}
SHORT = {"zoom": "Zoom", "teams": "Teams", "google": "Google Meet", "webex": "Webex", "jitsi": "Jitsi"}
SIGN_IN = ("google", "webex")  # signed in with a redirect back to the panel
KEEP_ALIVE_DAYS = 7
ROOM_CHARS = "abcdefghijkmnpqrstuvwxyz23456789"  # 20 of these: about 100 random bits

ZOOM_TOKEN = "https://zoom.us/oauth/token"  # noqa: S105 - an address, not a secret
ZOOM_API = "https://api.zoom.us/v2"
GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"  # noqa: S105
GOOGLE_REVOKE = "https://oauth2.googleapis.com/revoke"
GOOGLE_SCOPES = "openid email https://www.googleapis.com/auth/meetings.space.created"
MEET_API = "https://meet.googleapis.com/v2"
MS_LOGIN = "https://login.microsoftonline.com"
TEAMS_SCOPES = "openid profile offline_access OnlineMeetings.ReadWrite"
GRAPH = "https://graph.microsoft.com/v1.0"
WEBEX_API = "https://webexapis.com/v1"
WEBEX_AUTH = f"{WEBEX_API}/authorize"
WEBEX_TOKEN = f"{WEBEX_API}/access_token"  # noqa: S105
WEBEX_SCOPES = "meeting:schedules_read meeting:schedules_write"


class VideoError(Exception):
    """Something went wrong talking to a meeting service; the message is safe to show."""


@dataclass
class Meeting:
    platform: str
    id: str
    url: str


def client() -> httpx.Client:
    """The HTTP client every call uses (tests swap this for one with a mock transport)."""
    return httpx.Client(timeout=20.0, headers={"User-Agent": USER_AGENT})


def _why(resp: httpx.Response) -> str:
    """The service's own short explanation of an error, without echoing anything we sent."""
    try:
        data = resp.json()
    except ValueError:
        return f"HTTP {resp.status_code}"
    msg = ""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message") or err.get("code") or "")
        msg = msg or str(
            data.get("error_description") or data.get("message") or err or data.get("reason") or ""
        )
    msg = " ".join(msg.split())[:240]
    # Microsoft prefixes errors with codes and adds trace ids on new lines; keep the first sentence
    if msg.startswith("AADSTS"):
        msg = msg.split(" Trace ID")[0]
    return f"HTTP {resp.status_code}" + (f": {msg}" if msg else "")


def _jwt_claims(token: str | None) -> dict:
    """Read (not verify) an ID token's claims; only used to show which account is connected."""
    try:
        part = (token or "").split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


def _utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Accounts:
    """Sign-in tokens per service, kept in a small JSON file (or only in memory)."""

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        if path and path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                self._data = {k: v for k, v in raw.items() if k in PLATFORMS and isinstance(v, dict)}
            except (OSError, ValueError):
                log.warning("couldn't read %s; video-call services will need signing in again", path.name)

    def get(self, platform: str) -> dict:
        with self._lock:
            return dict(self._data.get(platform) or {})

    def set(self, platform: str, data: dict) -> None:
        with self._lock:
            self._data[platform] = dict(data)
            self._save()

    def drop(self, platform: str) -> None:
        with self._lock:
            if self._data.pop(platform, None) is not None:
                self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        text = json.dumps(self._data, indent=2, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".video-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            if os.name != "nt":
                os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("couldn't save %s", self.path.name)
            try:
                os.unlink(tmp)
            except OSError:
                pass


class VideoCalls:
    """Makes and removes meetings on whichever services are set up."""

    def __init__(self, cfg: Callable, path: Path | None = None):
        self._cfg = cfg  # returns the current AppConfig
        self.accounts = Accounts(path)
        self._tokens: dict[tuple, tuple[str, float]] = {}
        self._lock = threading.Lock()
        self.pending_teams: dict | None = None  # an unfinished Teams sign-in

    # -- state -------------------------------------------------------------------------------------

    def _vc(self, platform: str):
        return getattr(self._cfg().video_calls, platform)

    def configured(self, platform: str) -> bool:
        c = self._vc(platform)
        if platform == "zoom":
            return bool(c.account_id and c.client_id and c.client_secret and c.user)
        if platform in SIGN_IN:
            return bool(c.client_id and c.client_secret)
        if platform == "jitsi":
            return bool(c.server)
        return bool(c.client_id)

    def status(self, platform: str) -> str:
        """off, setup (details missing), sign_in (needs connecting) or ready."""
        if platform not in PLATFORMS or not self._vc(platform).enabled:
            return "off"
        if not self.configured(platform):
            return "setup"
        if platform in ("zoom", "jitsi"):
            return "ready"
        acct = self.accounts.get(platform)
        if not acct.get("refresh_token") or acct.get("client_id") != self._vc(platform).client_id:
            return "sign_in"
        return "ready"

    def ready(self, platform: str) -> bool:
        return self.status(platform) == "ready"

    def offered(self, wanted: list[str]) -> list[str]:
        """The services an audience offers that are ready to use right now."""
        return [p for p in wanted if self.ready(p)]

    def account(self, platform: str) -> str | None:
        if platform == "zoom":
            return self._vc("zoom").user
        if platform == "jitsi":
            return self._vc("jitsi").server.split("//", 1)[-1]
        return self.accounts.get(platform).get("account")

    # -- tokens ------------------------------------------------------------------------------------

    def _cached(self, key: tuple) -> str | None:
        with self._lock:
            hit = self._tokens.get(key)
        return hit[0] if hit and hit[1] > time.time() + 60 else None

    def _remember(self, key: tuple, token: str, expires_in) -> str:
        try:
            life = float(expires_in)
        except (TypeError, ValueError):
            life = 3000.0
        with self._lock:
            self._tokens[key] = (token, time.time() + life)
        return token

    def _forget_tokens(self, platform: str) -> None:
        with self._lock:
            for k in [k for k in self._tokens if k[0] == platform]:
                del self._tokens[k]

    def _token(self, platform: str, http: httpx.Client) -> str:
        c = self._vc(platform)
        if platform == "zoom":
            key = ("zoom", c.account_id, c.client_id)
            if hit := self._cached(key):
                return hit
            resp = http.post(
                ZOOM_TOKEN,
                data={"grant_type": "account_credentials", "account_id": c.account_id},
                auth=(c.client_id, c.client_secret),
            )
            if resp.status_code != 200:
                raise VideoError(f"Zoom didn't accept the app details ({_why(resp)}).")
            data = resp.json()
            return self._remember(key, data["access_token"], data.get("expires_in"))

        acct = self.accounts.get(platform)
        if not acct.get("refresh_token"):
            raise VideoError(f"{PLATFORMS[platform]} isn't signed in yet.")
        key = (platform, c.client_id, acct["refresh_token"][-12:])
        if hit := self._cached(key):
            return hit
        if platform in SIGN_IN:
            resp = http.post(
                GOOGLE_TOKEN if platform == "google" else WEBEX_TOKEN,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": acct["refresh_token"],
                    "client_id": c.client_id,
                    "client_secret": c.client_secret,
                },
            )
        else:
            resp = http.post(
                f"{MS_LOGIN}/{c.tenant}/oauth2/v2.0/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": acct["refresh_token"],
                    "client_id": c.client_id,
                    "scope": TEAMS_SCOPES,
                },
            )
        if resp.status_code != 200:
            hint = " Sign in again on the Video calls page." if resp.status_code in (400, 401) else ""
            raise VideoError(f"{PLATFORMS[platform]} sign-in has stopped working ({_why(resp)}).{hint}")
        data = resp.json()
        if data.get("refresh_token") and data["refresh_token"] != acct["refresh_token"]:
            acct["refresh_token"] = data["refresh_token"]  # Microsoft hands out a new one each time
            key = (platform, c.client_id, acct["refresh_token"][-12:])
        acct["refreshed"] = datetime.now(UTC).isoformat(timespec="seconds")
        self.accounts.set(platform, acct)
        return self._remember(key, data["access_token"], data.get("expires_in"))

    def keep_alive(self) -> None:
        """Renew sign-ins that haven't been used for a week. Microsoft and Webex sign-ins lapse
        after about 90 days unused, which would otherwise surprise you on the next booking."""
        now = datetime.now(UTC)
        for platform in ("google", "teams", "webex"):
            if self.status(platform) != "ready":
                continue
            acct = self.accounts.get(platform)
            last = acct.get("refreshed") or acct.get("connected") or ""
            try:
                age = now - datetime.fromisoformat(last)
            except ValueError:
                age = timedelta(days=KEEP_ALIVE_DAYS)
            if age < timedelta(days=KEEP_ALIVE_DAYS):
                continue
            self._forget_tokens(platform)
            try:
                with client() as http:
                    self._token(platform, http)
            except (VideoError, httpx.HTTPError) as exc:
                log.warning("couldn't renew the %s sign-in: %s", PLATFORMS[platform], exc)

    # -- Google and Webex sign-in (redirect back to the panel) ------------------------------------------

    def auth_url(self, platform: str, redirect_uri: str, state: str) -> str:
        c = self._vc(platform)
        params = {
            "client_id": c.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
        }
        if platform == "google":
            params |= {
                "scope": GOOGLE_SCOPES,
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "true",
            }
            return f"{GOOGLE_AUTH}?{urlencode(params)}"
        params["scope"] = WEBEX_SCOPES
        return f"{WEBEX_AUTH}?{urlencode(params)}"

    def finish_sign_in(self, platform: str, code: str, redirect_uri: str) -> str:
        c = self._vc(platform)
        name = PLATFORMS[platform]
        with client() as http:
            resp = http.post(
                GOOGLE_TOKEN if platform == "google" else WEBEX_TOKEN,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": c.client_id,
                    "client_secret": c.client_secret,
                },
            )
        if resp.status_code != 200:
            raise VideoError(f"{name} didn't finish the sign-in ({_why(resp)}).")
        data = resp.json()
        needed = "meetings.space.created" if platform == "google" else "meeting:schedules_write"
        if needed not in str(data.get("scope", needed)):
            raise VideoError(f"{name} sign-in worked, but permission to create meetings wasn't given.")
        if not data.get("refresh_token"):
            raise VideoError(f"{name} didn't send a lasting sign-in. Remove Glimpse's access and try again.")
        email = _jwt_claims(data.get("id_token")).get("email")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self.accounts.set(
            platform,
            {
                "refresh_token": data["refresh_token"],
                "client_id": c.client_id,
                "account": email,
                "connected": now,
                "refreshed": now,
            },
        )
        self._forget_tokens(platform)
        self._remember(
            (platform, c.client_id, data["refresh_token"][-12:]), data["access_token"], data.get("expires_in")
        )
        return email or f"your {name} account"

    # -- Teams sign-in (device code) -----------------------------------------------------------------

    def teams_start(self) -> dict:
        c = self._vc("teams")
        with client() as http:
            resp = http.post(
                f"{MS_LOGIN}/{c.tenant}/oauth2/v2.0/devicecode",
                data={"client_id": c.client_id, "scope": TEAMS_SCOPES},
            )
        if resp.status_code != 200:
            raise VideoError(f"Microsoft didn't start the sign-in ({_why(resp)}).")
        data = resp.json()
        self.pending_teams = {
            "device_code": data["device_code"],
            "user_code": data["user_code"],
            "url": data.get("verification_uri") or "https://microsoft.com/devicelogin",
            "expires": time.time() + float(data.get("expires_in", 900)),
            "interval": max(2, int(data.get("interval", 5))),
            "next": 0.0,
            "client_id": c.client_id,
        }
        return self.pending_teams

    def teams_poll(self) -> str:
        """pending, done or none (no sign-in going on). Raises VideoError if it failed."""
        p = self.pending_teams
        if not p:
            return "none"
        c = self._vc("teams")
        if time.time() > p["expires"] or p["client_id"] != c.client_id:
            self.pending_teams = None
            raise VideoError("The sign-in code expired. Start again.")
        if time.time() < p["next"]:
            return "pending"
        p["next"] = time.time() + p["interval"]
        with client() as http:
            resp = http.post(
                f"{MS_LOGIN}/{c.tenant}/oauth2/v2.0/token",
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": c.client_id,
                    "device_code": p["device_code"],
                },
            )
        if resp.status_code != 200:
            try:
                err = resp.json().get("error")
            except ValueError:
                err = None
            if err in ("authorization_pending", "slow_down"):
                if err == "slow_down":
                    p["interval"] += 5
                return "pending"
            self.pending_teams = None
            if err == "authorization_declined":
                raise VideoError("The sign-in was cancelled.")
            raise VideoError(f"Microsoft didn't finish the sign-in ({_why(resp)}).")
        data = resp.json()
        claims = _jwt_claims(data.get("id_token"))
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self.accounts.set(
            "teams",
            {
                "refresh_token": data["refresh_token"],
                "client_id": c.client_id,
                "account": claims.get("preferred_username") or claims.get("email") or claims.get("name"),
                "connected": now,
                "refreshed": now,
            },
        )
        self.pending_teams = None
        self._forget_tokens("teams")
        return "done"

    def disconnect(self, platform: str) -> None:
        acct = self.accounts.get(platform)
        if platform == "google" and acct.get("refresh_token"):
            try:
                with client() as http:
                    http.post(GOOGLE_REVOKE, data={"token": acct["refresh_token"]})
            except httpx.HTTPError as exc:
                log.info("couldn't revoke the Google sign-in: %s", type(exc).__name__)
        if platform == "teams":
            self.pending_teams = None
        self.accounts.drop(platform)
        self._forget_tokens(platform)

    # -- meetings ----------------------------------------------------------------------------------

    def create(self, platform: str, title: str, start: datetime, end: datetime, agenda: str = "") -> Meeting:
        """Make a meeting; raises VideoError (with a message you can show) if it can't."""
        if not self.ready(platform):
            raise VideoError(f"{PLATFORMS.get(platform, platform)} isn't set up.")
        if platform == "jitsi":
            # A long random room name is the meeting's only lock, like any Jitsi link.
            room = "Glimpse" + "".join(secrets.choice(ROOM_CHARS) for _ in range(20))
            return Meeting("jitsi", room, f"{self._vc('jitsi').server}/{room}")
        try:
            with client() as http:
                token = self._token(platform, http)
                head = {"Authorization": f"Bearer {token}"}
                if platform == "zoom":
                    c = self._vc("zoom")
                    resp = http.post(
                        f"{ZOOM_API}/users/{c.user}/meetings",
                        headers=head,
                        json={
                            "topic": title[:200],
                            "type": 2,
                            "start_time": _utc(start),
                            "duration": max(1, int((end - start).total_seconds() // 60)),
                            "timezone": "UTC",
                            "agenda": agenda[:2000],
                            "settings": {"waiting_room": c.waiting_room, "join_before_host": False},
                        },
                    )
                    if resp.status_code != 201:
                        raise VideoError(f"Zoom couldn't make the meeting ({_why(resp)}).")
                    data = resp.json()
                    return Meeting("zoom", str(data["id"]), data["join_url"])
                if platform == "teams":
                    resp = http.post(
                        f"{GRAPH}/me/onlineMeetings",
                        headers=head,
                        json={"subject": title[:200], "startDateTime": _utc(start), "endDateTime": _utc(end)},
                    )
                    if resp.status_code != 201:
                        raise VideoError(f"Teams couldn't make the meeting ({_why(resp)}).")
                    data = resp.json()
                    return Meeting("teams", str(data["id"]), data["joinWebUrl"])
                if platform == "webex":
                    resp = http.post(
                        f"{WEBEX_API}/meetings",
                        headers=head,
                        json={
                            "title": title[:128],
                            "agenda": agenda[:1300],
                            "start": _utc(start),
                            "end": _utc(end),
                            "timezone": "UTC",
                        },
                    )
                    if resp.status_code not in (200, 201):
                        raise VideoError(f"Webex couldn't make the meeting ({_why(resp)}).")
                    data = resp.json()
                    if data.get("hostEmail") and not self.accounts.get("webex").get("account"):
                        acct = self.accounts.get("webex")
                        acct["account"] = data["hostEmail"]
                        self.accounts.set("webex", acct)
                    return Meeting("webex", str(data["id"]), data["webLink"])
                open_access = self._vc("google").open_access
                resp = http.post(
                    f"{MEET_API}/spaces",
                    headers=head,
                    json={"config": {"accessType": "OPEN" if open_access else "TRUSTED"}},
                )
                if resp.status_code != 200:
                    raise VideoError(f"Google Meet couldn't make the meeting ({_why(resp)}).")
                data = resp.json()
                return Meeting("google", str(data["name"]), data["meetingUri"])
        except httpx.HTTPError as exc:
            raise VideoError(f"Couldn't reach {PLATFORMS[platform]} ({type(exc).__name__}).") from exc
        except (KeyError, ValueError) as exc:
            raise VideoError(f"{PLATFORMS[platform]} sent an answer Glimpse didn't understand.") from exc

    def delete(self, platform: str, meeting_id: str) -> None:
        """Remove a meeting that's no longer happening. Google Meet and Jitsi links can't be
        deleted: they simply go unused. Best effort; raises VideoError if the service refused."""
        if not meeting_id or platform in ("google", "jitsi") or self.status(platform) != "ready":
            return
        try:
            with client() as http:
                token = self._token(platform, http)
                head = {"Authorization": f"Bearer {token}"}
                if platform == "zoom":
                    resp = http.delete(f"{ZOOM_API}/meetings/{meeting_id}", headers=head)
                elif platform == "webex":
                    resp = http.delete(f"{WEBEX_API}/meetings/{meeting_id}", headers=head)
                else:
                    resp = http.delete(f"{GRAPH}/me/onlineMeetings/{meeting_id}", headers=head)
        except httpx.HTTPError as exc:
            raise VideoError(f"Couldn't reach {PLATFORMS[platform]} ({type(exc).__name__}).") from exc
        if resp.status_code not in (200, 204, 404):
            raise VideoError(f"{PLATFORMS[platform]} didn't delete the meeting ({_why(resp)}).")

    def test(self, platform: str) -> Meeting:
        """Make a short meeting tomorrow and remove it again, to check everything works."""
        start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
        end = start + timedelta(minutes=15)
        m = self.create(platform, "Glimpse test meeting", start, end, "A test; safe to delete.")
        try:
            self.delete(platform, m.id)
        except VideoError as exc:
            log.info("test meeting not removed: %s", exc)
        return m
