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
CONFIG_VERSION = 1  # settings format; bump with a step in migrations.py when config.yaml's shape changes
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
Marker = Literal["circle", "square", "triangle", "diamond", "star", "ring", "cross", "heart"]


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
    marker: Marker | None = None  # optional shape shown with its events

    @field_validator("color")
    @classmethod
    def _color(cls, v: str | None) -> str | None:
        return _check_color(v)

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
    marker: Marker | None = None  # defaults to the first calendar's shape

    @field_validator("color")
    @classmethod
    def _color(cls, v: str | None) -> str | None:
        return _check_color(v)


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

ViewName = Literal["week", "month", "quarter", "year"]
VIEW_ORDER: list[str] = ["week", "month", "quarter", "year"]


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
    shared_link: bool = True  # False: only people's own links work (see `people`)
    timezone: str | None = None  # zone viewers see by default; None = yours
    views: list[ViewName] = Field(default_factory=lambda: list(VIEW_ORDER))
    default_view: ViewName = "week"

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
            raise ValueError("an audience needs at least one view (week, month, quarter or year)")
        return seen

    @model_validator(mode="after")
    def _default_view_allowed(self) -> AudienceCfg:
        if self.default_view not in self.views:
            self.default_view = self.views[0]
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
        return MARKERS.get(self.marker or "", "")


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
    owner_name: str | None = None  # shown to viewers, e.g. "Brandon's time"

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
        slugs: dict[str, str] = {}
        for aid, aud in self.audiences.items():
            for cid in aud.calendars:
                if cid not in self.calendars:
                    raise ValueError(
                        f"audience {aid!r} refers to calendar {cid!r}, which is not defined "
                        f"(known: {', '.join(self.calendars)})"
                    )
            for gid in aud.groups:
                if gid not in self.groups:
                    raise ValueError(f"audience {aid!r} refers to group {gid!r}, which is not defined")
            if aud.slug in slugs:
                raise ValueError(f"audiences {slugs[aud.slug]!r} and {aid!r} share the same slug")
            slugs[aud.slug] = aid
        for pid, person in self.people.items():
            if person.audience not in self.audiences:
                raise ValueError(f"person {pid!r} is in audience {person.audience!r}, which is not defined")
            if person.slug in slugs:
                raise ValueError(f"person {pid!r} has the same link as {slugs[person.slug]!r}")
            slugs[person.slug] = pid
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
