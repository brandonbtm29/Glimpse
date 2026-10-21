"""Load and validate Glimpse's YAML config.

Secrets (feed links, slugs, password hashes) are written as ``${VAR}`` in the
YAML and filled from environment variables / a ``.env`` file at load time, so
``config.yaml`` itself never needs to hold anything sensitive.
"""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass
from datetime import date, time
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
CONFIG_VERSION = 2  # settings format; bump with a step in migrations.py when config.yaml's shape changes
DEFAULT_UPDATE_REPO = "brandonbtm29/Glimpse"
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class ConfigError(Exception):
    """Raised for any problem in config.yaml or .env, with a readable message."""


class Level(StrEnum):
    """How much of a calendar an audience may see, most to least revealing."""

    FULL = "full"  # title, time, location, notes
    TITLE = "title"  # title and time
    LABEL = "label"  # the calendar's fixed label (e.g. "Class") and time
    BUSY = "busy"  # an anonymous "Busy" block
    HIDDEN = "hidden"  # nothing at all

    @property
    def rank(self) -> int:
        return _RANK[self]

    def at_most(self, other: Level) -> Level:
        """Return the less revealing of the two levels."""
        return self if self.rank <= other.rank else other


_RANK = {Level.HIDDEN: 0, Level.BUSY: 1, Level.LABEL: 2, Level.TITLE: 3, Level.FULL: 4}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Small shapes drawn in an event's corner and in the legend, so calendars can be told
# apart without relying on colour alone.
MARKERS: dict[str, str] = {
    "circle": "\u25cf",
    "square": "\u25a0",
    "triangle": "\u25b2",
    "diamond": "\u25c6",
    "star": "\u2605",
    "ring": "\u25cb",
    "cross": "\u271a",
    "heart": "\u2665",
}
# Suggested emoji markers; any single emoji (or other symbol) also works.
EMOJI_MARKERS = [
    "\U0001f393",
    "\U0001f4da",
    "\U0001f52c",
    "\U0001f9ea",
    "\U0001f4bc",
    "\U0001f4bb",
    "\U0001f3e0",
    "\u2764\ufe0f",
    "\U0001f3cb\ufe0f",
    "\u26bd",
    "\U0001f3b5",
    "\U0001f37d\ufe0f",
    "\u2708\ufe0f",
    "\U0001fa7a",
    "\U0001f331",
    "\U0001f389",
    "\u2b50",
    "\U0001f4c5",
    "\U0001f91d",
    "\U0001f6b2",
]


def _blank_none(v: str | None) -> str | None:
    return None if v is None or not str(v).strip() or str(v).strip().lower() in ("null", "none") else v


def _check_marker(v: str | None) -> str | None:
    """A named shape (circle, star...) or a short emoji/symbol."""
    if v is None or not v.strip():
        return None
    v = v.strip()
    if v in MARKERS:
        return v
    if len(v) > 8 or any(c.isascii() for c in v):
        raise ValueError("marker must be one of " + ", ".join(MARKERS) + ", or a single emoji")
    return v


def marker_glyph(marker: str | None) -> str:
    return MARKERS.get(marker or "", marker or "")


def _check_color(v: str | None) -> str | None:
    if v is None or not v.strip():
        return None
    if not re.fullmatch(r"#[0-9a-fA-F]{3,8}|[a-zA-Z]{3,30}", v.strip()):
        raise ValueError("color must be a hex colour like '#2f9e44' or a CSS colour name")
    return v.strip()


class CalendarCfg(_Strict):
    name: str
    url: str
    label: str | None = None  # shown at the `label` level; defaults to `name`
    color: str | None = None  # CSS colour; a palette colour is used when unset
    marker: str | None = None  # optional shape or emoji shown with its events
    local: bool = False  # a Glimpse calendar: events made in the panel, kept in a file beside the config
    share: bool = False  # local calendars: offer a full-detail subscription link
    share_slug: str | None = None  # that link's secret part

    @field_validator("color")
    @classmethod
    def _color(cls, v: str | None) -> str | None:
        return _check_color(v)

    @field_validator("marker")
    @classmethod
    def _marker(cls, v: str | None) -> str | None:
        return _check_marker(v)

    @field_validator("share_slug")
    @classmethod
    def _share_slug(cls, v: str | None) -> str | None:
        return None if v is None or not v.strip() or v.strip().lower() in ("null", "none") else _check_slug(v)

    @property
    def display_label(self) -> str:
        return self.label or self.name


class HoursCfg(_Strict):
    days: list[str] = Field(default_factory=lambda: WEEKDAYS[:5])
    start: time = time(8, 0)
    end: time = time(18, 0)

    @field_validator("start", "end", mode="before")
    @classmethod
    def _yaml_sexagesimal(cls, v: Any) -> Any:
        # YAML 1.1 reads an unquoted 8:00 as the number 480 (base-60). Undo that.
        if isinstance(v, int) and not isinstance(v, bool):
            if v == 1440:
                return time(23, 59)
            return time(v // 60, v % 60)
        return v

    @field_validator("days")
    @classmethod
    def _days(cls, v: list[str]) -> list[str]:
        out = []
        for d in v:
            key = d.strip().lower()[:3]
            if key not in WEEKDAYS:
                raise ValueError(f"unknown day {d!r}; use mon, tue, wed, thu, fri, sat, sun")
            out.append(key)
        return out

    @model_validator(mode="after")
    def _order(self) -> HoursCfg:
        if self.end <= self.start:
            raise ValueError("hours.end must be later than hours.start")
        return self

    @property
    def weekday_numbers(self) -> set[int]:
        return {WEEKDAYS.index(d) for d in self.days}


class OverridesCfg(_Strict):
    private_tag: str = "#private"  # in an event's title/notes -> at most `busy`
    hide_tag: str = "#hide"  # -> hidden from every audience
    honor_private_flag: bool = True  # events marked Private in Outlook/Google -> at most `busy`
    merge_duplicates: bool = True  # the same event on several calendars is shown once


class GroupCfg(_Strict):
    """A set of calendars that share one level per audience (e.g. "Work")."""

    name: str
    calendars: list[str] = Field(default_factory=list)
    # Used when an audience is set to see group names instead of calendar names.
    label: str | None = None  # at the `label` level; defaults to the name
    color: str | None = None  # defaults to the first calendar's colour
    marker: str | None = None  # defaults to the first calendar's shape

    @field_validator("color")
    @classmethod
    def _color(cls, v: str | None) -> str | None:
        return _check_color(v)

    @field_validator("marker")
    @classmethod
    def _marker(cls, v: str | None) -> str | None:
        return _check_marker(v)


RuleField = Literal["title", "notes", "location"]


class RuleCfg(_Strict):
    """A keyword rule: matching events are shown as Busy (or hidden) to everyone.

    Rules only ever reduce detail. Keywords match whole words, ignoring case and accents.
    """

    name: str | None = None
    match: list[str]
    fields: list[RuleField] = Field(default_factory=lambda: ["title", "notes", "location"])
    action: Literal["busy", "hidden"] = "busy"
    calendars: list[str] = Field(default_factory=list)  # empty = every calendar
    except_audiences: list[str] = Field(default_factory=list)  # audiences this rule leaves alone
    enabled: bool = True

    @field_validator("match")
    @classmethod
    def _match(cls, v: list[str]) -> list[str]:
        words = [w.strip() for w in v if w and w.strip()]
        if not words:
            raise ValueError("a rule needs at least one keyword")
        return words

    @field_validator("fields")
    @classmethod
    def _fields(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("a rule must look in at least one of title, notes, location")
        return v

    @property
    def level(self) -> Level:
        return Level.HIDDEN if self.action == "hidden" else Level.BUSY

    @property
    def display_name(self) -> str:
        return self.name or ", ".join(self.match)


MAX_WEEKS = 104  # the most a number can say; None ("all") goes further
ALL_WEEKS = 520  # what "all" means in practice: ten years either way

ViewName = Literal["day", "week", "month", "quarter", "year"]
VIEW_ORDER: list[str] = ["day", "week", "month", "quarter", "year"]


def _check_slug(v: str) -> str:
    if len(v) < 16 or not re.fullmatch(r"[A-Za-z0-9_-]+", v):
        raise ValueError(
            "slug must be at least 16 characters of letters, digits, - or _ "
            "(run `glimpse new-slug` to make one)"
        )
    return v


def _check_hash(v: str | None) -> str | None:
    if v is None or v.strip().lower() in ("null", "none"):
        return None
    if not v.strip():
        # An empty value usually means a forgotten .env entry. Failing loudly beats
        # silently turning a password-protected page into a link-only one.
        raise ValueError("password_hash is empty; set it with `glimpse hash-password` or use null")
    if not v.startswith("$argon2"):
        raise ValueError("password_hash must be an Argon2 hash (run `glimpse hash-password`)")
    return v


def _check_zone(v: str | None) -> str | None:
    if v is None or not v.strip():
        return None
    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown time zone {v!r}") from exc
    return v


REQUEST_LENGTHS = [15, 30, 45, 60, 90, 120]


class RequestsCfg(_Strict):
    """Lets an audience ask for a meeting: pick an open slot, or suggest a time."""

    enabled: bool = False
    lengths: list[int] = Field(default_factory=lambda: [30, 60])  # minutes offered
    slots: bool = True  # show open times to pick from
    suggest: bool = True  # allow "suggest another time"
    hours: HoursCfg | None = None  # when meetings can happen; None = the audience's hours, else 9-5 weekdays
    notice_hours: int = Field(default=24, ge=0, le=24 * 30)  # earliest a meeting can start from now
    days_ahead: int = Field(default=14, ge=1, le=120)  # how far ahead open times are offered
    step_minutes: Literal[15, 30, 60] = 30  # start times every N minutes
    message: str | None = Field(default=None, max_length=500)  # shown above the request form
    auto_confirm: bool = False  # picked open times are booked straight away (suggestions still wait)
    calendar: str | None = None  # Glimpse calendar that confirmed meetings are added to
    meeting_link: str | None = None  # e.g. your Zoom or Teams link; shared only once confirmed
    location: str | None = Field(default=None, max_length=200)  # e.g. a room; shared only once confirmed
    booking: bool = False  # a public booking page with its own link (no password, open times only)
    booking_slug: str | None = None
    # online-meeting services people can choose from; each booking gets a new meeting there
    # (set them up under video_calls). Empty: the fixed meeting_link, if any.
    platforms: list[Literal["zoom", "teams", "google", "webex", "jitsi"]] = Field(default_factory=list)

    @field_validator("calendar", "location")
    @classmethod
    def _blank(cls, v: str | None) -> str | None:
        return _blank_none(v)

    @field_validator("meeting_link")
    @classmethod
    def _link(cls, v: str | None) -> str | None:
        v = _blank_none(v)
        if v and not re.fullmatch(r"https?://\S+", v.strip()):
            raise ValueError("the meeting link should start with https://")
        return v.strip() if v else v

    @field_validator("booking_slug")
    @classmethod
    def _booking_slug(cls, v: str | None) -> str | None:
        return None if v is None or not v.strip() or v.strip().lower() in ("null", "none") else _check_slug(v)

    @field_validator("platforms")
    @classmethod
    def _platforms(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))

    @field_validator("lengths")
    @classmethod
    def _lengths(cls, v: list[int]) -> list[int]:
        out = sorted({int(x) for x in v if 5 <= int(x) <= 480})
        if not out:
            raise ValueError("offer at least one meeting length")
        return out

    @model_validator(mode="after")
    def _some_way(self) -> RequestsCfg:
        if self.enabled and not (self.slots or self.suggest):
            raise ValueError("requests need open times, suggested times, or both")
        return self


class WidgetCfg(_Strict):
    """A small free/busy view (no titles) that can be embedded in another website."""

    enabled: bool = False
    slug: str | None = None  # its own link, separate from the audience's
    sites: list[str] = Field(default_factory=list)  # websites allowed to embed it; empty = any
    days: int = Field(default=7, ge=1, le=14)

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str | None) -> str | None:
        return None if v is None or not v.strip() or v.strip().lower() in ("null", "none") else _check_slug(v)

    @field_validator("sites")
    @classmethod
    def _sites(cls, v: list[str]) -> list[str]:
        out = []
        for s in v:
            s = s.strip().rstrip("/")
            if not s:
                continue
            if not re.fullmatch(r"https?://[A-Za-z0-9.-]+(:\d+)?", s):
                raise ValueError(f"site {s!r} should look like https://example.com (no path)")
            out.append(s)
        return out


class AudienceCfg(_Strict):
    title: str = "Schedule"
    slug: str
    password_hash: str | None = None
    default: Level = Level.BUSY
    calendars: dict[str, Level] = Field(default_factory=dict)  # per-calendar overrides
    groups: dict[str, Level] = Field(default_factory=dict)  # per-group levels
    hours: HoursCfg | None = None
    weeks_ahead: int | None = Field(default=4, ge=0, le=MAX_WEEKS)  # None = no limit
    weeks_behind: int | None = Field(default=1, ge=0, le=MAX_WEEKS)
    group_names: bool = False  # show each group's name instead of its calendars' names
    show_all_day_busy: bool = False  # all-day events at `busy` level are usually reminders
    allow_excel: bool = True
    allow_ics: bool = True
    feed_skip: list[str] = Field(default_factory=list)  # calendars left out of the subscription feed
    shared_link: bool = True  # False: only people's own links work (see `people`)
    timezone: str | None = None  # zone viewers see by default; None = yours
    views: list[ViewName] = Field(default_factory=lambda: list(VIEW_ORDER))
    default_view: ViewName = "week"
    requests: RequestsCfg = Field(default_factory=RequestsCfg)
    widget: WidgetCfg = Field(default_factory=WidgetCfg)
    # Your own view: every calendar in full, no privacy rules or tags, no hours limit. Made
    # automatically; it stays locked until it has a password.
    self_view: bool = False

    @property
    def request_hours(self) -> HoursCfg:
        return self.requests.hours or self.hours or HoursCfg(start=time(9, 0), end=time(17, 0))

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        return _check_slug(v)

    @field_validator("password_hash")
    @classmethod
    def _hash(cls, v: str | None) -> str | None:
        return _check_hash(v)

    @field_validator("timezone")
    @classmethod
    def _zone(cls, v: str | None) -> str | None:
        return _check_zone(v)

    @field_validator("views")
    @classmethod
    def _views(cls, v: list[str]) -> list[str]:
        seen = [x for x in VIEW_ORDER if x in v]
        if not seen:
            raise ValueError("an audience needs at least one view (day, week, month, quarter or year)")
        return seen

    @model_validator(mode="after")
    def _default_view_allowed(self) -> AudienceCfg:
        if self.default_view not in self.views:
            self.default_view = self.views[0]
        if self.self_view:
            # Always everything (a calendar can only be left out), and nothing to share or book.
            self.default = Level.FULL
            self.calendars = {cid: lv for cid, lv in self.calendars.items() if lv is Level.HIDDEN}
            self.groups = {}
            self.hours = None
            self.group_names = False
            self.shared_link = True
            self.requests = RequestsCfg()
            self.widget = WidgetCfg()
        return self


class PersonCfg(_Strict):
    """Someone with their own link into an audience. Moving them changes what their link shows."""

    name: str
    audience: str
    slug: str
    password_hash: str | None = None  # their own password; overrides the audience's
    use_audience_password: bool = True  # with no own password: ask for the audience's (if it has one)
    expires: date | None = None  # last day the link works
    enabled: bool = True
    note: str | None = None

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        return _check_slug(v)

    @field_validator("password_hash")
    @classmethod
    def _hash(cls, v: str | None) -> str | None:
        return _check_hash(v)

    def expired(self, today: date) -> bool:
        return not self.enabled or (self.expires is not None and today > self.expires)


class ServerCfg(_Strict):
    host: str = "127.0.0.1"
    port: int = 8000
    base_url: str = "http://127.0.0.1:8000"  # public address, used by `glimpse links`
    secret_key: str = Field(min_length=32)  # signs login cookies and feed tokens
    secure_cookies: bool = True
    trust_proxy_headers: bool = True  # read client IP from X-Forwarded-For (tunnel)


class AdminCfg(_Strict):
    """The settings panel. Bound to localhost; reach it through `tailscale serve`."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8001
    password_hash: str | None = None  # set with `glimpse admin-password` or the first-run page
    setup_token: str | None = None  # one-time secret for the first-run page; removed after setup
    # If non-empty, only these Tailscale logins may use the panel (checked via the
    # Tailscale-User-Login header that `tailscale serve` adds and cannot be spoofed through it).
    allowed_tailscale_users: list[str] = Field(default_factory=list)
    secure_cookies: bool = True

    @field_validator("setup_token")
    @classmethod
    def _token(cls, v: str | None) -> str | None:
        return v if v and v.strip() and v.strip().lower() not in ("null", "none") else None

    @field_validator("password_hash")
    @classmethod
    def _hash(cls, v: str | None) -> str | None:
        if v is None or not v.strip() or v.strip().lower() in ("null", "none"):
            return None
        if not v.startswith("$argon2"):
            raise ValueError("admin.password_hash must be an Argon2 hash (run `glimpse admin-password`)")
        return v


class UpdatesCfg(_Strict):
    check: bool = True  # look for a new release on GitHub once a day
    repo: str = DEFAULT_UPDATE_REPO  # owner/name of the GitHub repository to check

    @field_validator("repo")
    @classmethod
    def _repo(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", v):
            raise ValueError("updates.repo must look like owner/name")
        return v


class NtfyCfg(_Strict):
    """Phone push through ntfy (https://ntfy.sh): install the app and subscribe to the topic."""

    enabled: bool = False
    server: str = "https://ntfy.sh"
    topic: str | None = None  # acts like a password: anyone who knows it can read the messages
    token: str | None = None  # access token, for a private ntfy server

    @field_validator("topic", "token")
    @classmethod
    def _blank(cls, v: str | None) -> str | None:
        return _blank_none(v)

    @field_validator("server")
    @classmethod
    def _server(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", v):
            raise ValueError("ntfy server should look like https://ntfy.sh")
        return v


class EmailCfg(_Strict):
    """Email through any SMTP account (e.g. Gmail with an app password)."""

    enabled: bool = False
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    security: Literal["starttls", "ssl", "none"] = "starttls"
    username: str | None = None
    password: str | None = None
    from_addr: str | None = None  # defaults to the username
    to_addr: str | None = None  # where your notifications go; defaults to from_addr
    notify_requester: bool = True  # also email people about their own requests

    @field_validator("smtp_host", "username", "password", "from_addr", "to_addr")
    @classmethod
    def _blank(cls, v: str | None) -> str | None:
        return _blank_none(v)

    @property
    def sender(self) -> str | None:
        return self.from_addr or self.username

    @property
    def owner_addr(self) -> str | None:
        return self.to_addr or self.sender


class DiscordCfg(_Strict):
    """A message in a Discord channel through a webhook (Channel settings > Integrations)."""

    enabled: bool = False
    webhook_url: str | None = None

    @field_validator("webhook_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        v = _blank_none(v)
        if v and not re.match(
            r"^https://(discord(app)?\.com|canary\.discord\.com|ptb\.discord\.com)/api/webhooks/", v
        ):
            raise ValueError("Discord webhook links start with https://discord.com/api/webhooks/")
        return v


class NotifyCfg(_Strict):
    ntfy: NtfyCfg = Field(default_factory=NtfyCfg)
    email: EmailCfg = Field(default_factory=EmailCfg)
    discord: DiscordCfg = Field(default_factory=DiscordCfg)
    panel_url: str | None = None  # link in notifications; defaults to the tailnet panel address

    @field_validator("panel_url")
    @classmethod
    def _blank(cls, v: str | None) -> str | None:
        return _blank_none(v)


class ZoomCfg(_Strict):
    """Zoom, through a Server-to-Server OAuth app you make in the Zoom App Marketplace."""

    enabled: bool = False
    account_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = None  # keep in .env
    user: str | None = None  # the Zoom login (email) the meetings belong to
    waiting_room: bool = True  # people wait until you let them in

    @field_validator("account_id", "client_id", "client_secret", "user")
    @classmethod
    def _blank(cls, v: str | None) -> str | None:
        v = _blank_none(v)
        return v.strip() if v else v


class GoogleMeetCfg(_Strict):
    """Google Meet, through an OAuth client you make in Google Cloud and then sign in to."""

    enabled: bool = False
    client_id: str | None = None
    client_secret: str | None = None  # keep in .env
    open_access: bool = False  # anyone with the link joins straight away (else they ask to join)

    @field_validator("client_id", "client_secret")
    @classmethod
    def _blank(cls, v: str | None) -> str | None:
        v = _blank_none(v)
        return v.strip() if v else v


class TeamsCfg(_Strict):
    """Microsoft Teams, through an app registration in Microsoft Entra (work or school accounts)."""

    enabled: bool = False
    client_id: str | None = None  # Application (client) ID; no secret needed
    tenant: str = "organizations"  # or your organisation's directory (tenant) ID or domain

    @field_validator("client_id")
    @classmethod
    def _blank(cls, v: str | None) -> str | None:
        v = _blank_none(v)
        return v.strip() if v else v

    @field_validator("tenant")
    @classmethod
    def _tenant(cls, v: str) -> str:
        v = (v or "").strip() or "organizations"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,99}", v):
            raise ValueError("the Teams tenant should be 'organizations', a directory ID or a domain")
        return v


class WebexCfg(_Strict):
    """Webex, through an integration you make at developer.webex.com and then sign in to."""

    enabled: bool = False
    client_id: str | None = None
    client_secret: str | None = None  # keep in .env

    @field_validator("client_id", "client_secret")
    @classmethod
    def _blank(cls, v: str | None) -> str | None:
        v = _blank_none(v)
        return v.strip() if v else v


class JitsiCfg(_Strict):
    """Jitsi Meet: a new room with a long random name for each meeting. Nothing to sign in to."""

    enabled: bool = False
    server: str = "https://meet.jit.si"  # or your own Jitsi server

    @field_validator("server")
    @classmethod
    def _server(cls, v: str) -> str:
        v = (v or "").strip().rstrip("/") or "https://meet.jit.si"
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?(/[A-Za-z0-9._~-]+)*", v):
            raise ValueError("the Jitsi server should be an https:// address, like https://meet.jit.si")
        return v


class VideoCfg(_Strict):
    """Services that make a new online meeting, with its own link, for each booking."""

    zoom: ZoomCfg = Field(default_factory=ZoomCfg)
    teams: TeamsCfg = Field(default_factory=TeamsCfg)
    google: GoogleMeetCfg = Field(default_factory=GoogleMeetCfg)
    webex: WebexCfg = Field(default_factory=WebexCfg)
    jitsi: JitsiCfg = Field(default_factory=JitsiCfg)


class AvailabilityCfg(_Strict):
    """How open times are worked out for meeting requests and widgets."""

    ignore_calendars: list[str] = Field(default_factory=list)  # never make you busy (e.g. Holidays)
    buffer_minutes: int = Field(default=0, ge=0, le=120)  # kept free before and after busy times
    all_day_busy: bool = False  # all-day events (trips, conferences) block the whole day


class PeriodCfg(_Strict):
    """A named stretch of days in a term: a break, exam week, a closed day."""

    name: str
    start: date
    end: date  # last day, inclusive
    kind: Literal["break", "exams", "closed"] = "break"

    @model_validator(mode="after")
    def _order(self) -> PeriodCfg:
        if self.end < self.start:
            raise ValueError(f"{self.name}: end is before start")
        return self


class TermCfg(_Strict):
    name: str  # "Fall 2026"
    start: date  # first day of classes
    end: date  # last day of the term (including exams), inclusive
    periods: list[PeriodCfg] = Field(default_factory=list)

    @model_validator(mode="after")
    def _order(self) -> TermCfg:
        if self.end < self.start:
            raise ValueError(f"{self.name}: end is before start")
        return self


class SchoolCfg(_Strict):
    """School mode: know the semester, its breaks and exam weeks."""

    enabled: bool = False
    name: str | None = None  # e.g. "University of Akron"
    calendar_url: str | None = None  # the school's academic calendar feed; terms are read from it
    terms: list[TermCfg] = Field(default_factory=list)  # typed in by hand; these win over the feed
    class_calendars: list[str] = Field(default_factory=list)  # calendars with your classes
    pause_classes: bool = True  # hide class events during breaks and between terms
    show_viewers: bool = True  # viewers see "Fall 2026 · Week 7" and break days

    @field_validator("name", "calendar_url")
    @classmethod
    def _blank(cls, v: str | None) -> str | None:
        return _blank_none(v)


@dataclass(frozen=True)
class CalDisplay:
    key: str  # calendar id, or "group:<id>"
    name: str
    label: str
    color: str | None
    marker: str | None
    palette_id: str  # calendar whose palette slot the colour comes from

    @property
    def glyph(self) -> str:
        return marker_glyph(self.marker)


class AppConfig(_Strict):
    config_version: int = Field(default=CONFIG_VERSION, ge=1)
    timezone: str = "America/New_York"
    refresh_minutes: int = Field(default=10, ge=1, le=1440)
    week_starts: Literal["monday", "sunday"] = "monday"
    cache_dir: str = "cache"
    server: ServerCfg
    admin: AdminCfg = Field(default_factory=AdminCfg)
    overrides: OverridesCfg = Field(default_factory=OverridesCfg)
    calendars: dict[str, CalendarCfg] = Field(default_factory=dict)
    groups: dict[str, GroupCfg] = Field(default_factory=dict)
    rules: list[RuleCfg] = Field(default_factory=list)
    audiences: dict[str, AudienceCfg] = Field(default_factory=dict)
    people: dict[str, PersonCfg] = Field(default_factory=dict)
    updates: UpdatesCfg = Field(default_factory=UpdatesCfg)
    notify: NotifyCfg = Field(default_factory=NotifyCfg)
    video_calls: VideoCfg = Field(default_factory=VideoCfg)
    availability: AvailabilityCfg = Field(default_factory=AvailabilityCfg)
    school: SchoolCfg = Field(default_factory=SchoolCfg)
    owner_name: str | None = None  # shown to viewers, e.g. "Brandon's time"
    support_link: bool = False  # a small "Support Glimpse" link at the bottom of shared pages

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {v!r}") from exc
        return v

    @model_validator(mode="after")
    def _cross_refs(self) -> AppConfig:
        member_of: dict[str, str] = {}
        for gid, grp in self.groups.items():
            for cid in grp.calendars:
                if cid not in self.calendars:
                    raise ValueError(f"group {gid!r} lists calendar {cid!r}, which is not defined")
                if cid in member_of:
                    raise ValueError(f"calendar {cid!r} is in two groups ({member_of[cid]!r} and {gid!r})")
                member_of[cid] = gid
        for i, rule in enumerate(self.rules, 1):
            for cid in rule.calendars:
                if cid not in self.calendars:
                    raise ValueError(f"rule {i} refers to calendar {cid!r}, which is not defined")
            for aid in rule.except_audiences:
                if aid not in self.audiences:
                    raise ValueError(f"rule {i} refers to audience {aid!r}, which is not defined")
        slugs: dict[str, str] = {}
        for aid, aud in self.audiences.items():
            for cid in aud.calendars:
                if cid not in self.calendars:
                    raise ValueError(
                        f"audience {aid!r} refers to calendar {cid!r}, which is not defined "
                        f"(known: {', '.join(self.calendars)})"
                    )
            for cid in aud.feed_skip:
                if cid not in self.calendars:
                    raise ValueError(f"audience {aid!r} leaves out calendar {cid!r}, which is not defined")
            for gid in aud.groups:
                if gid not in self.groups:
                    raise ValueError(f"audience {aid!r} refers to group {gid!r}, which is not defined")
            if aud.slug in slugs:
                raise ValueError(f"audiences {slugs[aud.slug]!r} and {aid!r} share the same slug")
            slugs[aud.slug] = aid
        if sum(1 for a in self.audiences.values() if a.self_view) > 1:
            raise ValueError("only one audience can be your own view (self_view)")
        for pid, person in self.people.items():
            if person.audience not in self.audiences:
                raise ValueError(f"person {pid!r} is in audience {person.audience!r}, which is not defined")
            if self.audiences[person.audience].self_view:
                raise ValueError(f"person {pid!r} can't be put in your own view; pick another audience")
            if person.slug in slugs:
                raise ValueError(f"person {pid!r} has the same link as {slugs[person.slug]!r}")
            slugs[person.slug] = pid
        for cid, cal in self.calendars.items():
            if cal.share and not cal.share_slug:
                raise ValueError(f"calendar {cid!r} is shared but has no share slug")
            if cal.share_slug:
                if cal.share_slug in slugs:
                    raise ValueError(f"calendar {cid!r} has the same share link as {slugs[cal.share_slug]!r}")
                slugs[cal.share_slug] = f"{cid} share"
        for aid, aud in self.audiences.items():
            r = aud.requests
            if r.calendar and (r.calendar not in self.calendars or not self.calendars[r.calendar].local):
                raise ValueError(f"audience {aid!r}: {r.calendar!r} isn't a Glimpse calendar")
            if r.booking and not r.booking_slug:
                raise ValueError(f"audience {aid!r} has a booking page but no booking slug")
            if r.booking_slug:
                if r.booking_slug in slugs:
                    raise ValueError(f"{aid!r} has the same booking link as {slugs[r.booking_slug]!r}")
                slugs[r.booking_slug] = f"{aid} booking"
            if aud.widget.enabled and not aud.widget.slug:
                raise ValueError(f"audience {aid!r} has a widget turned on but no widget slug")
            if aud.widget.slug:
                if aud.widget.slug in slugs:
                    raise ValueError(
                        f"audience {aid!r}'s widget has the same link as {slugs[aud.widget.slug]!r}"
                    )
                slugs[aud.widget.slug] = f"{aid} widget"
        for where, ids in (
            ("availability.ignore_calendars", self.availability.ignore_calendars),
            ("school.class_calendars", self.school.class_calendars),
        ):
            for cid in ids:
                if cid not in self.calendars:
                    raise ValueError(f"{where} lists calendar {cid!r}, which is not defined")
        return self

    @property
    def owner_possessive(self) -> str | None:
        name = (self.owner_name or "").strip()
        if not name:
            return None
        return f"{name}'" if name.endswith("s") else f"{name}'s"

    @property
    def owner_label(self) -> str:
        name = (self.owner_name or "").strip()
        if not name:
            return "Owner's time"
        return f"{name}' time" if name.endswith("s") else f"{name}'s time"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def self_audience(self) -> tuple[str, AudienceCfg] | None:
        """Your own view (the automatic "My calendar" audience), if there is one."""
        return next(((aid, a) for aid, a in self.audiences.items() if a.self_view), None)

    @property
    def shared_audiences(self) -> dict[str, AudienceCfg]:
        """Every audience except your own view: the ones you share with other people."""
        return {aid: a for aid, a in self.audiences.items() if not a.self_view}

    def audience_id(self, audience: AudienceCfg) -> str | None:
        """The id of an audience object from this config (None for e.g. the preview's "Everything")."""
        return next((aid for aid, a in self.audiences.items() if a is audience), None)

    def group_of(self, calendar_id: str) -> str | None:
        for gid, grp in self.groups.items():
            if calendar_id in grp.calendars:
                return gid
        return None

    def level_for(self, audience: AudienceCfg, calendar_id: str) -> Level:
        """Calendar override, else its group's level, else the audience default."""
        if calendar_id in audience.calendars:
            return audience.calendars[calendar_id]
        gid = self.group_of(calendar_id)
        if gid is not None and gid in audience.groups:
            return audience.groups[gid]
        return audience.default

    def display_for(self, audience: AudienceCfg, calendar_id: str) -> CalDisplay:
        """How a calendar is named, coloured and marked for an audience (group or calendar)."""
        cal = self.calendars[calendar_id]
        gid = self.group_of(calendar_id) if audience.group_names else None
        if gid is not None:
            grp = self.groups[gid]
            first = self.calendars[grp.calendars[0]]
            return CalDisplay(
                key=f"group:{gid}",
                name=grp.name,
                label=grp.label or grp.name,
                color=grp.color or first.color,
                marker=grp.marker or first.marker,
                palette_id=grp.calendars[0],
            )
        return CalDisplay(
            key=calendar_id,
            name=cal.name,
            label=cal.display_label,
            color=cal.color,
            marker=cal.marker,
            palette_id=calendar_id,
        )

    def level_source(self, audience: AudienceCfg, calendar_id: str) -> str:
        """Where a calendar's level comes from: 'calendar', 'group' or 'default'."""
        if calendar_id in audience.calendars:
            return "calendar"
        gid = self.group_of(calendar_id)
        return "group" if gid is not None and gid in audience.groups else "default"


def _substitute(node: Any, env: dict[str, str], missing: set[str]) -> Any:
    if isinstance(node, dict):
        return {k: _substitute(v, env, missing) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute(v, env, missing) for v in node]
    if isinstance(node, str):

        def repl(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in env or env[name] is None:
                missing.add(name)
                return ""
            return env[name]

        return _ENV_REF.sub(repl, node)
    return node


def read_env_file(env_path: Path) -> dict[str, str]:
    if not env_path.is_file():
        return {}
    values = dotenv_values(env_path, interpolate=False, encoding="utf-8-sig")
    return {k: v for k, v in values.items() if v is not None}


def build_config(
    raw: dict[str, Any], env: dict[str, str], base_dir: Path, source: str = "config"
) -> AppConfig:
    """Validate an already-parsed config dict against ``env`` (no file access)."""
    from .migrations import migrate

    try:
        raw, _ = migrate(raw, CONFIG_VERSION)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    missing: set[str] = set()
    filled = _substitute(copy.deepcopy(raw), env, missing)
    if missing:
        raise ConfigError("missing values for " + ", ".join(sorted(missing)) + " (add them to .env)")

    # Relative cache_dir / file feeds resolve beside the config file.
    filled.setdefault("cache_dir", "cache")
    if not Path(filled["cache_dir"]).is_absolute():
        filled["cache_dir"] = str(base_dir / filled["cache_dir"])
    for cal in (filled.get("calendars") or {}).values():
        if isinstance(cal, dict) and isinstance(cal.get("url"), str):
            url = cal["url"]
            if "://" not in url and not Path(url).is_absolute():
                cal["url"] = str((base_dir / url).resolve())
    for key in ("calendars", "audiences"):
        if filled.get(key) is None:
            filled[key] = {}

    try:
        return AppConfig.model_validate(filled)
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            where = ".".join(str(p) for p in err["loc"]) or "(top level)"
            lines.append(f"  {where}: {err['msg']}")
        raise ConfigError(f"{source} has problems:\n" + "\n".join(lines)) from None


def read_raw_config(cfg_path: Path) -> dict[str, Any]:
    if not cfg_path.is_file():
        raise ConfigError(f"config file not found: {cfg_path}")
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8-sig")) or {}  # -sig: Notepad BOMs
    except yaml.YAMLError as exc:
        raise ConfigError(f"{cfg_path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path}: the top level must be a mapping")
    return raw


def default_env_path(cfg_path: Path) -> Path:
    return cfg_path.with_name(".env")


def load_config(path: str | os.PathLike[str], env_file: str | os.PathLike[str] | None = None) -> AppConfig:
    """Read ``config.yaml``, fill ``${VAR}`` references, and validate.

    Values come from the real environment first, then from ``env_file``
    (default: ``.env`` beside the config file).
    """
    cfg_path = Path(path)
    raw = read_raw_config(cfg_path)
    env_path = Path(env_file) if env_file else default_env_path(cfg_path)
    env = read_env_file(env_path)
    env.update(os.environ)
    return build_config(raw, env, cfg_path.parent, source=str(cfg_path))
