"""Glue: weeks, ranges, and the cached 'what does audience X see this week' query."""

from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .config import ALL_WEEKS, AppConfig, AudienceCfg
from .events import Event, collect_events
from .policy import VisibleEvent, visible_events
from .sources import FeedCache


def week_start(day: date, cfg: AppConfig) -> date:
    offset = day.weekday() if cfg.week_starts == "monday" else (day.weekday() + 1) % 7
    return day - timedelta(days=offset)


def week_range(start: date, cfg: AppConfig, tz: ZoneInfo | None = None) -> tuple[datetime, datetime]:
    return day_range(start, start + timedelta(days=7), tz or cfg.tz)


def day_range(first: date, end_exclusive: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    return datetime.combine(first, time.min, tzinfo=tz), datetime.combine(end_exclusive, time.min, tzinfo=tz)


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def period_bounds(view: str, day: date) -> tuple[date, date]:
    """First day and the day after the last, for a month, quarter (3 months from that month) or year."""
    if view == "year":
        return date(day.year, 1, 1), date(day.year + 1, 1, 1)
    first = date(day.year, day.month, 1)
    return first, add_months(first, 3 if view == "quarter" else 1)


def allowed_week_bounds(audience: AudienceCfg, today: date, cfg: AppConfig) -> tuple[date, date]:
    """First and last week start this audience may look at."""
    this_week = week_start(today, cfg)
    return (
        this_week - timedelta(weeks=ALL_WEEKS if audience.weeks_behind is None else audience.weeks_behind),
        this_week + timedelta(weeks=ALL_WEEKS if audience.weeks_ahead is None else audience.weeks_ahead),
    )


class Glimpse:
    """Holds config + feed cache and answers view queries."""

    def __init__(self, cfg: AppConfig, cache: FeedCache | None = None):
        self.cfg = cfg
        self.cache = cache or FeedCache(cfg)
        self._events: OrderedDict[tuple, list[Event]] = OrderedDict()
        self.generation = 0

    def apply(self, new_cfg: AppConfig) -> list[str]:
        """Switch to a new config without a restart.

        Feeds whose link didn't change keep their downloaded copy; returns the ids
        of calendars that are new or whose link changed (they need a fetch).
        """
        old_states, old_cals = self.cache.states, self.cfg.calendars
        cache = FeedCache(new_cfg)
        changed = []
        for cid, cal in new_cfg.calendars.items():
            old = old_cals.get(cid)
            if old is not None and old.url == cal.url and cid in old_states:
                cache.states[cid] = old_states[cid]
            else:
                cache.forget(cid)  # never show the old link's events under a new link
                changed.append(cid)
        self.cfg, self.cache = new_cfg, cache
        self.generation += 1
        self._events.clear()
        return changed

    def today(self) -> date:
        return datetime.now(self.cfg.tz).date()

    def today_in(self, tz: ZoneInfo) -> date:
        """Today's date in ``tz`` (consistent with :meth:`today`, which tests may pin)."""
        shift = datetime.now(tz).date() - datetime.now(self.cfg.tz).date()
        return self.today() + shift

    def audience_by_slug(self, slug: str) -> tuple[str, AudienceCfg] | None:
        import hmac

        found = None
        for aid, aud in self.cfg.audiences.items():
            # compare every slug in constant time; don't stop early
            if hmac.compare_digest(aud.slug.encode(), slug.encode()):
                found = (aid, aud)
        return found

    def events_between(self, start: datetime, end: datetime) -> list[Event]:
        key = (self.generation, self.cache.fingerprint(), start, end)
        if key in self._events:
            self._events.move_to_end(key)
            return self._events[key]
        evs = collect_events(self.cache.texts(), start, end, self.cfg.tz)
        self._events[key] = evs
        while len(self._events) > 32:
            self._events.popitem(last=False)
        return evs

    def view(
        self, audience: AudienceCfg, start: datetime, end: datetime, tz: ZoneInfo | None = None
    ) -> list[VisibleEvent]:
        if tz is None or tz.key == self.cfg.timezone:
            return visible_events(self.cfg, audience, self.events_between(start, end), start, end)
        # Another zone's days don't line up with ours: fetch a day either side, then clip.
        pad = timedelta(days=1)
        own = self.cfg.tz
        events = self.events_between(
            datetime.combine((start - pad).astimezone(own).date(), time.min, tzinfo=own),
            datetime.combine((end + pad).astimezone(own).date(), time.min, tzinfo=own),
        )
        return visible_events(self.cfg, audience, events, start, end, tz)

    def week(self, audience: AudienceCfg, start_day: date, tz: ZoneInfo | None = None) -> list[VisibleEvent]:
        s, e = week_range(start_day, self.cfg, tz)
        return self.view(audience, s, e, tz)
