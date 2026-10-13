"""Lay out one week of visible events for the HTML grid and phone agenda."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ..config import AppConfig, AudienceCfg, Level
from ..policy import VisibleEvent

PALETTE_SIZE = 12
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt_time(t: time | datetime, compact: bool = False) -> str:
    """12-hour clock without platform-specific strftime flags (Windows-safe)."""
    h, m = t.hour, t.minute
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    if compact:
        return f"{h12}{'' if m == 0 else f':{m:02d}'}{suffix[0].lower()}"
    return f"{h12}:{m:02d} {suffix}"


def fmt_day(d: date) -> str:
    return f"{DAY_NAMES[d.weekday()]} {MONTHS[d.month - 1]} {d.day}"


def fmt_range(start: date, end_inclusive: date) -> str:
    if start.month == end_inclusive.month:
        return f"{MONTHS[start.month - 1]} {start.day}–{end_inclusive.day}, {start.year}"
    if start.year == end_inclusive.year:
        return (
            f"{MONTHS[start.month - 1]} {start.day} – "
            f"{MONTHS[end_inclusive.month - 1]} {end_inclusive.day}, {start.year}"
        )
    return (
        f"{MONTHS[start.month - 1]} {start.day}, {start.year} – "
        f"{MONTHS[end_inclusive.month - 1]} {end_inclusive.day}, {end_inclusive.year}"
    )


@dataclass
class Block:
    ev: VisibleEvent
    start: datetime  # clipped to the day
    end: datetime
    top: float = 0.0  # % of the day column
    height: float = 0.0
    col: int = 0
    ncols: int = 1
    color_class: str = "busy"
    color: str | None = None

    @property
    def left(self) -> float:
        return 100.0 * self.col / self.ncols

    @property
    def width(self) -> float:
        return 100.0 / self.ncols

    @property
    def time_label(self) -> str:
        return f"{fmt_time(self.start)} – {fmt_time(self.end)}"

    @property
    def short(self) -> bool:
        return (self.end - self.start) <= timedelta(minutes=40)


@dataclass
class DayView:
    day: date
    is_today: bool
    all_day: list[Block] = field(default_factory=list)
    timed: list[Block] = field(default_factory=list)

    @property
    def label(self) -> str:
        return fmt_day(self.day)

    @property
    def dow(self) -> str:
        return DAY_NAMES[self.day.weekday()]


@dataclass
class WeekView:
    title: str
    start: date
    days: list[DayView]
    hour_start: int
    hour_end: int
    legend: list[LegendItem]
    has_busy: bool
    tz: ZoneInfo | None = None  # zone the times are shown in
    n_days: int = 7
    owner_tz: ZoneInfo | None = None  # the calendar owner's zone, when different

    def owner_time(self, dt: datetime) -> str:
        """The same moment on the owner's clock, for hover text when zones differ."""
        if self.owner_tz is None:
            return ""
        o = dt.astimezone(self.owner_tz)
        return f"{fmt_time(o)} {o.tzname()}"

    @property
    def range_label(self) -> str:
        if self.n_days == 1:
            return f"{fmt_day(self.start)}, {self.start.year}"
        return fmt_range(self.start, self.start + timedelta(days=self.n_days - 1))

    @property
    def hours(self) -> list[str]:
        return [fmt_time(time(h % 24), compact=True) for h in range(self.hour_start, self.hour_end)]

    @property
    def n_hours(self) -> int:
        return self.hour_end - self.hour_start


def _color_for(cfg: AppConfig, ev: VisibleEvent) -> tuple[str, str | None]:
    if ev.level is Level.BUSY or ev.calendar_id is None:
        return "busy", None
    idx = list(cfg.calendars).index(ev.calendar_id) % PALETTE_SIZE
    return f"c{idx}", ev.color


@dataclass
class LegendItem:
    name: str
    color_class: str
    color: str | None
    marker: str
    active: bool  # has something in the period shown


SHOWN_LEVELS = (Level.FULL, Level.TITLE, Level.LABEL)


def build_legend(cfg: AppConfig, audience: AudienceCfg, events: list[VisibleEvent]) -> list[LegendItem]:
    """Every calendar (or group) this audience can see by name, in config order.

    Ones with nothing in the period are still listed (faded), so a quiet calendar
    doesn't look missing. Busy-only and hidden calendars are never named.
    """
    used = {ev.calendar_name for ev in events if ev.level is not Level.BUSY and ev.calendar_name}
    order = list(cfg.calendars)
    items: dict[str, LegendItem] = {}
    for cid in order:
        level = cfg.level_for(audience, cid)
        if level not in SHOWN_LEVELS:
            continue
        d = cfg.display_for(audience, cid)
        name = d.label if level is Level.LABEL else d.name
        if name not in items:
            idx = order.index(d.palette_id) % PALETTE_SIZE
            items[name] = LegendItem(name, f"c{idx}", d.color, d.glyph, name in used)
    for ev in events:  # e.g. a label shared by several calendars, or levels raised per event
        if ev.level is not Level.BUSY and ev.calendar_name and ev.calendar_name not in items:
            cls, custom = _color_for(cfg, ev)
            items[ev.calendar_name] = LegendItem(ev.calendar_name, cls, custom, ev.marker, True)
    return list(items.values())


def _hour_bounds(audience: AudienceCfg, blocks: list[Block]) -> tuple[int, int]:
    if audience.hours:
        lo = audience.hours.start.hour
        hi = audience.hours.end.hour + (1 if audience.hours.end.minute else 0)
        return lo, max(hi, lo + 1)
    lo, hi = 8, 20
    for b in blocks:
        if b.start.time() == time.min and b.ev.start < b.start:
            continue  # tail of last night's event: shown as a pill, don't stretch the grid
        lo = min(lo, b.start.hour)
        end_h = 24 if b.end.date() > b.start.date() else b.end.hour + (1 if b.end.minute else 0)
        hi = max(hi, end_h)
    return max(0, lo), min(24, hi)


def _layout_columns(blocks: list[Block]) -> None:
    """Side-by-side columns for overlapping events within one day."""
    blocks.sort(key=lambda b: (b.start, -(b.end - b.start).total_seconds()))
    cluster: list[Block] = []
    cluster_end: datetime | None = None
    col_ends: list[datetime] = []

    def close() -> None:
        n = max((b.col for b in cluster), default=0) + 1
        for b in cluster:
            b.ncols = n

    for b in blocks:
        if cluster_end is not None and b.start >= cluster_end:
            close()
            cluster, col_ends = [], []
        for i, ce in enumerate(col_ends):
            if b.start >= ce:
                b.col = i
                col_ends[i] = b.end
                break
        else:
            b.col = len(col_ends)
            col_ends.append(b.end)
        cluster.append(b)
        cluster_end = b.end if cluster_end is None else max(cluster_end, b.end)
    if cluster:
        close()


def build_week(
    cfg: AppConfig,
    audience: AudienceCfg,
    start: date,
    events: list[VisibleEvent],
    today: date,
    tz: ZoneInfo | None = None,
    n_days: int = 7,
) -> WeekView:
    """Lay out ``n_days`` days from ``start`` (7 for the week view, 1 for the day view)."""
    tz = tz or cfg.tz
    shifted = tz.key != cfg.timezone
    dates = [start + timedelta(days=i) for i in range(n_days)]
    days = [DayView(day=d, is_today=d == today) for d in dates]
    if audience.hours and not shifted and n_days > 1:
        # Hour limits are on the owner's clock; in another zone they can land on any weekday.
        allowed = audience.hours.weekday_numbers
        days = [d for d in days if d.day.weekday() in allowed]

    timed_all: list[Block] = []
    has_busy = False
    for ev in events:
        cls, custom = _color_for(cfg, ev)
        has_busy = has_busy or cls == "busy"
        for dv in days:
            day0 = datetime.combine(dv.day, time.min, tzinfo=tz)
            day1 = datetime.combine(dv.day + timedelta(days=1), time.min, tzinfo=tz)
            s, e = max(ev.start, day0), min(ev.end, day1)
            if s >= e:
                continue
            blk = Block(ev=ev, start=s, end=e, color_class=cls, color=custom)
            if ev.all_day:
                dv.all_day.append(blk)
            else:
                dv.timed.append(blk)
                timed_all.append(blk)

    h0, h1 = _hour_bounds(audience if not shifted else audience.model_copy(update={"hours": None}), timed_all)
    span = (h1 - h0) * 60
    for dv in days:
        visible = []
        for b in dv.timed:
            day0 = datetime.combine(dv.day, time(h0), tzinfo=tz)
            mins_s = (b.start - day0).total_seconds() / 60
            mins_e = (b.end - day0).total_seconds() / 60
            mins_s, mins_e = max(0.0, mins_s), min(float(span), mins_e)
            if mins_e <= mins_s:
                dv.all_day.append(b)  # outside the visible hours: show as a pill with its time
                continue
            b.top = 100.0 * mins_s / span
            b.height = max(100.0 * (mins_e - mins_s) / span, 100.0 * 15 / span)
            visible.append(b)
        dv.timed = visible
        _layout_columns(dv.timed)

    legend = build_legend(cfg, audience, events)
    return WeekView(
        title=audience.title,
        start=start,
        days=days,
        hour_start=h0,
        hour_end=max(h1, h0 + 1),
        legend=legend,
        has_busy=has_busy,
        tz=tz,
        owner_tz=cfg.tz if shifted else None,
        n_days=n_days,
    )
