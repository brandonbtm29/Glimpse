"""Month grid and 3-month / year heat maps, built from already-filtered events.

These only ever see :class:`VisibleEvent` objects, which the policy has
already redacted for the audience. The heat maps go further and show only how
busy each day is (hours, never titles); multi-day items such as trips are
listed separately at whatever level the audience may see them.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..config import AppConfig, AudienceCfg
from ..policy import VisibleEvent
from .week import DAY_NAMES, MONTHS, PALETTE_SIZE, LegendItem, _color_for, build_legend, fmt_time

MONTH_NAMES = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
CHIPS_PER_DAY = 3
# Upper bounds (minutes busy) for heat levels 1..3; anything above is level 4.
HEAT_STEPS = (60, 180, 360)


def _week_start(day: date, week_starts: str) -> date:
    offset = day.weekday() if week_starts == "monday" else (day.weekday() + 1) % 7
    return day - timedelta(days=offset)


def _dow_headers(week_starts: str) -> list[str]:
    names = DAY_NAMES if week_starts == "monday" else DAY_NAMES[6:] + DAY_NAMES[:6]
    return list(names)


def _day_bounds(d: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    return datetime.combine(d, time.min, tzinfo=tz), datetime.combine(
        d + timedelta(days=1), time.min, tzinfo=tz
    )


def busy_minutes(events: list[VisibleEvent], d: date, tz: ZoneInfo) -> int:
    """Minutes of ``d`` covered by timed events (overlaps counted once)."""
    d0, d1 = _day_bounds(d, tz)
    spans = sorted(
        (max(e.start, d0), min(e.end, d1)) for e in events if not e.all_day and e.start < d1 and e.end > d0
    )
    total, cur_s, cur_e = 0.0, None, None
    for s, e in spans:
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += (cur_e - cur_s).total_seconds()
    return int(total // 60)


def heat_level(minutes: int) -> int:
    """0 (free) to 4 (very busy)."""
    if minutes <= 0:
        return 0
    return next((i for i, step in enumerate(HEAT_STEPS, 1) if minutes <= step), len(HEAT_STEPS) + 1)


def fmt_hours(minutes: int) -> str:
    if minutes <= 0:
        return "free"
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} h {m} min busy"
    return f"{h} h busy" if h else f"{m} min busy"


# --- month grid -----------------------------------------------------------------------------


@dataclass
class Chip:
    ev: VisibleEvent
    color_class: str
    color: str | None
    cont: bool = False  # started on an earlier day (e.g. a late movie past midnight)

    @property
    def when(self) -> str:
        if self.ev.all_day:
            return ""
        if self.cont:
            return f"until {fmt_time(self.ev.end, compact=True)}"
        return fmt_time(self.ev.start, compact=True)


@dataclass
class MonthCell:
    day: date
    in_month: bool
    in_range: bool
    is_today: bool
    chips: list[Chip] = field(default_factory=list)
    more: int = 0
    minutes: int = 0

    @property
    def all_chips(self) -> list[Chip]:
        return self.chips


@dataclass
class MonthView:
    title: str
    first: date
    weeks: list[list[MonthCell]]
    dows: list[str]
    legend: list[LegendItem]
    has_busy: bool
    tz: ZoneInfo

    @property
    def label(self) -> str:
        return f"{MONTH_NAMES[self.first.month - 1]} {self.first.year}"


def _legend_and_chips(cfg: AppConfig, audience: AudienceCfg, events: list[VisibleEvent]):
    chips = []
    for ev in events:
        cls, custom = _color_for(cfg, ev)
        chips.append(Chip(ev, cls, custom))
    has_busy = any(c.color_class == "busy" for c in chips)
    return chips, build_legend(cfg, audience, events), has_busy


def build_month(
    cfg: AppConfig,
    audience: AudienceCfg,
    first: date,
    events: list[VisibleEvent],
    today: date,
    allowed: tuple[date, date],
    tz: ZoneInfo,
    limit: int | None = CHIPS_PER_DAY,
) -> MonthView:
    """``allowed`` is the first and last day this audience may see (inclusive)."""
    month_end = date(first.year + first.month // 12, first.month % 12 + 1, 1)
    start = _week_start(first, cfg.week_starts)
    chips, legend, has_busy = _legend_and_chips(cfg, audience, events)
    weeks: list[list[MonthCell]] = []
    d = start
    while d < month_end:
        row = []
        for _ in range(7):
            in_range = allowed[0] <= d <= allowed[1]
            cell = MonthCell(day=d, in_month=d.month == first.month, in_range=in_range, is_today=d == today)
            if in_range and cell.in_month:
                d0, d1 = _day_bounds(d, tz)
                todays = [
                    replace(c, cont=not c.ev.all_day and c.ev.start < d0)
                    for c in chips
                    if c.ev.start < d1 and c.ev.end > d0
                ]
                todays.sort(key=lambda c: (not c.ev.all_day, not c.cont, c.ev.start, c.ev.title))
                cell.minutes = busy_minutes([c.ev for c in todays], d, tz)
                shown = todays if limit is None else todays[:limit]
                cell.chips, cell.more = shown, len(todays) - len(shown)
            row.append(cell)
            d += timedelta(days=1)
        weeks.append(row)
    return MonthView(
        title=audience.title,
        first=first,
        weeks=weeks,
        dows=_dow_headers(cfg.week_starts),
        legend=legend,
        has_busy=has_busy,
        tz=tz,
    )


# --- heat maps ------------------------------------------------------------------------------


@dataclass
class HeatDay:
    day: date
    in_month: bool
    in_range: bool
    is_today: bool
    minutes: int = 0

    @property
    def level(self) -> int:
        return heat_level(self.minutes) if self.in_range and self.in_month else 0

    @property
    def tip(self) -> str:
        label = f"{DAY_NAMES[self.day.weekday()]} {MONTHS[self.day.month - 1]} {self.day.day}"
        if not self.in_range:
            return f"{label}: not shared"
        return f"{label}: {fmt_hours(self.minutes)}"


@dataclass
class HeatMonth:
    first: date
    weeks: list[list[HeatDay]]

    @property
    def label(self) -> str:
        return MONTH_NAMES[self.first.month - 1]


@dataclass
class Span:
    """A multi-day item (trip, conference), shown at the audience's level."""

    title: str
    first: date
    last: date
    color_class: str
    color: str | None

    @property
    def when(self) -> str:
        a, b = self.first, self.last
        if a.month == b.month:
            return f"{MONTHS[a.month - 1]} {a.day}–{b.day}"
        return f"{MONTHS[a.month - 1]} {a.day} – {MONTHS[b.month - 1]} {b.day}"

    @property
    def days(self) -> int:
        return (self.last - self.first).days + 1


@dataclass
class HeatView:
    title: str
    first: date
    end: date  # exclusive
    kind: str  # "quarter" or "year"
    months: list[HeatMonth]
    dows: list[str]
    spans: list[Span]
    total_minutes: int
    busiest: list[HeatDay]

    @property
    def label(self) -> str:
        last = self.end - timedelta(days=1)
        if self.kind == "year":
            return str(self.first.year)
        if self.first.year == last.year:
            return f"{MONTHS[self.first.month - 1]} – {MONTHS[last.month - 1]} {last.year}"
        return f"{MONTHS[self.first.month - 1]} {self.first.year} – {MONTHS[last.month - 1]} {last.year}"


def build_heat(
    cfg: AppConfig,
    audience: AudienceCfg,
    first: date,
    end: date,
    kind: str,
    events: list[VisibleEvent],
    today: date,
    allowed: tuple[date, date],
    tz: ZoneInfo,
) -> HeatView:
    timed = [e for e in events if not e.all_day]
    by_day: dict[date, list[VisibleEvent]] = {}
    for e in timed:
        d = e.start.astimezone(tz).date()
        last = (e.end.astimezone(tz) - timedelta(microseconds=1)).date()
        while d <= last:
            by_day.setdefault(d, []).append(e)
            d += timedelta(days=1)

    months: list[HeatMonth] = []
    all_days: list[HeatDay] = []
    m = first
    while m < end:
        nxt = date(m.year + m.month // 12, m.month % 12 + 1, 1)
        d = _week_start(m, cfg.week_starts)
        weeks = []
        while d < nxt:
            row = []
            for _ in range(7):
                hd = HeatDay(
                    day=d,
                    in_month=d.month == m.month,
                    in_range=allowed[0] <= d <= allowed[1],
                    is_today=d == today,
                )
                if hd.in_month and hd.in_range:
                    hd.minutes = busy_minutes(by_day.get(d, []), d, tz)
                    all_days.append(hd)
                row.append(hd)
                d += timedelta(days=1)
            weeks.append(row)
        months.append(HeatMonth(first=m, weeks=weeks))
        m = nxt

    # Multi-day all-day items become spans. Busy-level ones are already titled "Busy".
    runs: dict[tuple, list[tuple[date, date, str | None]]] = {}
    for e in events:
        if e.all_day:
            cls, custom = _color_for(cfg, e)
            a = e.start.astimezone(tz).date()
            b = (e.end.astimezone(tz) - timedelta(microseconds=1)).date()
            runs.setdefault((e.title, cls), []).append((a, b, custom))
    span_list: list[Span] = []
    for (title, cls), parts in runs.items():
        parts.sort()
        cur: Span | None = None
        for a, b, custom in parts:  # all-day events arrive one part per day: stitch them back
            if cur and (a - cur.last).days <= 1:
                cur.last = max(cur.last, b)
                continue
            if cur and cur.days >= 2:
                span_list.append(cur)
            cur = Span(title, a, b, cls, custom)
        if cur and cur.days >= 2:
            span_list.append(cur)
    span_list.sort(key=lambda sp: (sp.first, sp.title))

    busiest = sorted((d for d in all_days if d.minutes > 0), key=lambda d: -d.minutes)[:5]
    return HeatView(
        title=audience.title,
        first=first,
        end=end,
        kind=kind,
        months=months,
        dows=_dow_headers(cfg.week_starts),
        spans=span_list,
        total_minutes=sum(d.minutes for d in all_days),
        busiest=busiest,
    )


__all__ = [
    "PALETTE_SIZE",
    "HeatView",
    "MonthView",
    "build_heat",
    "build_month",
    "busy_minutes",
    "fmt_hours",
    "heat_level",
]
