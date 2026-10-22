"""Decide what each audience may see. This module is the privacy boundary.

Everything downstream (HTML, Excel, ICS) only ever receives ``VisibleEvent``
objects produced here, and a ``VisibleEvent`` carries only the fields its
level allows. Busy blocks carry no calendar name, colour or original UID,
and overlapping busy blocks are merged so viewers can't count your events.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from .config import AppConfig, AudienceCfg, HoursCfg, Level, RuleCfg
from .events import Event

BUSY_TITLE = "Busy"
MAX_NOTES = 600


@dataclass(frozen=True)
class VisibleEvent:
    key: str  # stable, opaque id (never the source UID)
    start: datetime
    end: datetime
    all_day: bool
    level: Level
    title: str
    calendar_id: str | None = None  # None for busy blocks
    calendar_name: str | None = None
    color: str | None = None
    location: str = ""
    notes: str = ""
    copies: int = 1  # >1 when duplicates on several calendars were merged (shown only to you)
    marker: str = ""  # shape glyph for the calendar (or group), if one is set


def _strip_tags(text: str, tags: list[str]) -> str:
    for tag in tags:
        if tag:
            text = re.sub(re.escape(tag), "", text, flags=re.IGNORECASE)
    return " ".join(text.split()) if "\n" not in text else text.strip()


def _contains(text: str, tag: str) -> bool:
    return bool(tag) and tag.lower() in text.lower()


def _opaque(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:20]


def fold(text: str) -> str:
    """Lower-case and strip accents, for keyword matching ("Thérapy" -> "therapy")."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


@lru_cache(maxsize=256)
def _keyword_pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    alts = "|".join(re.escape(" ".join(fold(w).split())) for w in words)
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)")


def rule_matches(rule: RuleCfg, ev: Event) -> bool:
    if not rule.enabled or (rule.calendars and ev.calendar_id not in rule.calendars):
        return False
    texts = {"title": ev.summary, "notes": ev.description, "location": ev.location}
    pattern = _keyword_pattern(tuple(rule.match))
    return any(pattern.search(" ".join(fold(texts[f]).split())) for f in rule.fields)


def restriction(cfg: AppConfig, ev: Event, audience: AudienceCfg | None = None) -> Level:
    """The most this event may ever reveal, from tags, the Private flag and keyword rules.

    FULL means "no restriction". These apply to every audience and only reduce detail, except
    your own view: it's you, so tags, the Private flag and rules don't hide anything from it.
    """
    if audience is not None and audience.self_view:
        return Level.FULL
    ov = cfg.overrides
    blob = f"{ev.summary}\n{ev.description}"
    if _contains(blob, ov.hide_tag):
        return Level.HIDDEN
    cap = Level.FULL
    if _contains(blob, ov.private_tag) or (ov.honor_private_flag and ev.private):
        cap = Level.BUSY
    aid = cfg.audience_id(audience) if audience is not None else None
    for rule in cfg.rules:
        if aid is not None and aid in rule.except_audiences:
            continue
        if rule_matches(rule, ev):
            cap = cap.at_most(rule.level)
            if cap is Level.HIDDEN:
                break
    return cap


def effective_level(cfg: AppConfig, audience: AudienceCfg, ev: Event) -> Level:
    """The level an event is shown at: its calendar's level, capped by any restriction."""
    return cfg.level_for(audience, ev.calendar_id).at_most(restriction(cfg, ev, audience))


# --- duplicate merging ----------------------------------------------------------------


@dataclass
class Cluster:
    """One real-world event, possibly present on several calendars."""

    copies: list[Event]

    def resolve(self, cfg: AppConfig, audience: AudienceCfg) -> tuple[Event, Level]:
        """Most detailed copy this audience may see, capped by every copy's restrictions."""
        best = max(self.copies, key=lambda e: cfg.level_for(audience, e.calendar_id).rank)
        level = cfg.level_for(audience, best.calendar_id)
        for ev in self.copies:
            level = level.at_most(restriction(cfg, ev, audience))
        if len(self.copies) > 1 and not all(e.free for e in self.copies):
            best = replace(best, free=False)
        return best, level


def _title_key(ev: Event) -> tuple:
    return (" ".join(fold(ev.summary).split()), ev.start, ev.end, ev.all_day)


def cluster_duplicates(events: list[Event], enabled: bool = True) -> list[Cluster]:
    """Group copies of the same event: same UID and start, or same title, start and end."""
    if not enabled:
        return [Cluster([e]) for e in events]
    parent = list(range(len(events)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    seen: dict[tuple, int] = {}
    for i, ev in enumerate(events):
        for key in (("uid", ev.uid, ev.start, ev.all_day), ("title", *_title_key(ev))):
            if key in seen:
                if events[seen[key]].calendar_id != ev.calendar_id or key[0] == "uid":
                    union(seen[key], i)
            else:
                seen[key] = i
    clusters: dict[int, list[Event]] = {}
    for i, ev in enumerate(events):
        clusters.setdefault(find(i), []).append(ev)
    return [Cluster(c) for _, c in sorted(clusters.items())]


def _days(start: datetime, end: datetime) -> list[date]:
    last = (end - timedelta(microseconds=1)).date()
    d, out = start.date(), []
    while d <= last:
        out.append(d)
        d += timedelta(days=1)
    return out


def clip_to_hours(
    start: datetime, end: datetime, hours: HoursCfg, tz: ZoneInfo
) -> list[tuple[datetime, datetime]]:
    """Split a timed event into the parts that fall inside the allowed hours."""
    allowed = hours.weekday_numbers
    out = []
    for d in _days(start, end):
        if d.weekday() not in allowed:
            continue
        ws = datetime.combine(d, hours.start, tzinfo=tz)
        we = datetime.combine(d, hours.end, tzinfo=tz)
        s, e = max(start, ws), min(end, we)
        if s < e:
            out.append((s, e))
    return out


def _all_day_parts(ev: Event, audience: AudienceCfg, tz: ZoneInfo) -> list[tuple[datetime, datetime]]:
    """One midnight-to-midnight part per date, in ``tz``: all-day events are tied to dates, not instants."""
    allowed = audience.hours.weekday_numbers if audience.hours else set(range(7))
    out = []
    for d in _days(ev.start, ev.end):
        if d.weekday() in allowed:
            s = datetime.combine(d, datetime.min.time(), tzinfo=tz)
            out.append((s, s + timedelta(days=1)))
    return out


def visible_events(
    cfg: AppConfig,
    audience: AudienceCfg,
    events: list[Event],
    range_start: datetime,
    range_end: datetime,
    display_tz: ZoneInfo | None = None,
) -> list[VisibleEvent]:
    """Filter and redact ``events`` for one audience within a date range.

    Hour limits always apply in the owner's zone. Results are expressed in
    ``display_tz`` (the viewer's zone); all-day events keep their calendar date.
    """
    tz = cfg.tz
    dtz = display_tz or tz
    tags = [cfg.overrides.private_tag, cfg.overrides.hide_tag]
    out: list[VisibleEvent] = []

    for cluster in cluster_duplicates(events, cfg.overrides.merge_duplicates):
        ev, level = cluster.resolve(cfg, audience)
        copies = len(cluster.copies)
        if level is Level.HIDDEN:
            continue
        if ev.free and level in (Level.BUSY, Level.LABEL):
            continue  # "show as free" events don't make you busy
        if ev.all_day and level is Level.BUSY and not audience.show_all_day_busy:
            continue

        if ev.all_day:
            parts = _all_day_parts(ev, audience, dtz)
        elif audience.hours:
            parts = clip_to_hours(ev.start, ev.end, audience.hours, tz)
        else:
            parts = [(ev.start, ev.end)]
        parts = [(s.astimezone(dtz), e.astimezone(dtz)) for s, e in parts]

        cal = cfg.display_for(audience, ev.calendar_id)
        for s, e in parts:
            s, e = max(s, range_start), min(e, range_end)
            if s >= e:
                continue
            if level is Level.FULL:
                ve = VisibleEvent(
                    key=_opaque(ev.calendar_id, ev.uid, s.isoformat(), "full"),
                    start=s,
                    end=e,
                    all_day=ev.all_day,
                    level=level,
                    title=_strip_tags(ev.summary, tags) or "(no title)",
                    calendar_id=cal.palette_id,
                    calendar_name=cal.name,
                    color=cal.color,
                    location=_strip_tags(ev.location, tags),
                    notes=_strip_tags(ev.description, tags)[:MAX_NOTES],
                    copies=copies,
                    marker=cal.glyph,
                )
            elif level is Level.TITLE:
                ve = VisibleEvent(
                    key=_opaque(ev.calendar_id, ev.uid, s.isoformat(), "title"),
                    start=s,
                    end=e,
                    all_day=ev.all_day,
                    level=level,
                    title=_strip_tags(ev.summary, tags) or "(no title)",
                    calendar_id=cal.palette_id,
                    calendar_name=cal.name,
                    color=cal.color,
                    copies=copies,
                    marker=cal.glyph,
                )
            elif level is Level.LABEL:
                ve = VisibleEvent(
                    key=_opaque("label", cal.label, s.isoformat(), e.isoformat()),
                    start=s,
                    end=e,
                    all_day=ev.all_day,
                    level=level,
                    title=cal.label,
                    calendar_id=cal.palette_id,
                    calendar_name=cal.label,
                    color=cal.color,
                    marker=cal.glyph,
                )
            else:  # BUSY
                ve = VisibleEvent(
                    key=_opaque("busy", s.isoformat(), e.isoformat()),
                    start=s,
                    end=e,
                    all_day=ev.all_day,
                    level=level,
                    title=BUSY_TITLE,
                )
            out.append(ve)

    return _merge_anonymous(out)


def _merge_anonymous(items: list[VisibleEvent]) -> list[VisibleEvent]:
    """Merge overlapping busy blocks (and same-label blocks) into single blocks."""
    keep = [v for v in items if v.level not in (Level.BUSY, Level.LABEL)]
    groups: dict[tuple[str, bool], list[VisibleEvent]] = {}
    for v in items:
        if v.level in (Level.BUSY, Level.LABEL):
            groups.setdefault((v.title if v.level is Level.LABEL else "", v.all_day), []).append(v)

    for (_, _all_day), vs in groups.items():
        vs.sort(key=lambda v: (v.start, v.end))
        cur = vs[0]
        for v in vs[1:]:
            if v.start <= cur.end:  # overlapping or touching
                if v.end > cur.end:
                    cur = replace(cur, end=v.end)
            else:
                keep.append(_rekey(cur))
                cur = v
        keep.append(_rekey(cur))

    keep.sort(key=lambda v: (v.start, not v.all_day, v.end, v.title))
    return keep


def _rekey(v: VisibleEvent) -> VisibleEvent:
    return replace(v, key=_opaque(v.level.value, v.title, v.start.isoformat(), v.end.isoformat()))
