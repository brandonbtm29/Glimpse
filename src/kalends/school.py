"""School mode: which term a day is in, its breaks and exam weeks.

Terms come from two places: ones typed in on the School page, and ones read from
the school's academic calendar feed. Feeds differ between schools, so reading
one is a best guess from event titles ("Classes begin", "Thanksgiving Break",
"Final Exams"...). Typed-in terms always win.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import httpx

from .config import AppConfig, PeriodCfg, TermCfg
from .events import Event, parse_events
from .sources import normalize_url

log = logging.getLogger("kalends.school")

SEASONS = {1: "Spring", 2: "Spring", 3: "Spring", 4: "Spring", 5: "Summer", 6: "Summer", 7: "Summer"}
_START = re.compile(
    r"\b(classes|instruction|semester|term|session)\s+(begin|begins|start|starts|commence)\b"
    r"|\bfirst\s+day\s+of\s+(classes|instruction|the\s+semester|the\s+term|semester|term)\b",
    re.I,
)
_END = re.compile(
    r"\blast\s+day\s+of\s+(classes|instruction)\b|\b(classes|instruction)\s+(end|ends)\b",
    re.I,
)
_EXAMS = re.compile(
    r"\bfinal\s+exam|\bfinals?\s+week\b|\bexam(ination)?s?\s+week\b|\bfinal\s+examinations?\b", re.I
)
_TERM_END = re.compile(
    r"\b(semester|term|session)\s+(ends|end)\b|\blast\s+day\s+of\s+(the\s+)?(semester|term)\b", re.I
)
_BREAK = re.compile(
    r"\b(break|recess|vacation|holiday|thanksgiving|reading\s+day|no\s+classes|study\s+day)\b",
    re.I,
)
_CLOSED = re.compile(r"\b(university|campus|offices?)\s+(is\s+)?closed\b|\bclosed\b", re.I)
_NAMED = re.compile(r"\b(fall|spring|summer|winter)\b(?:\s+(?:semester|term|session))?\s*(\d{4})?", re.I)


def season_of(start: date) -> str:
    return SEASONS.get(start.month, "Fall" if start.month >= 8 else "Spring")


@dataclass
class Schedule:
    """Every known term, from the feed and from the School page."""

    terms: list[TermCfg] = field(default_factory=list)
    week_starts: str = "monday"

    def term_for(self, day: date) -> TermCfg | None:
        return next((t for t in self.terms if t.start <= day <= t.end), None)

    def week_of(self, term: TermCfg, day: date) -> int:
        def ws(d: date) -> date:
            off = d.weekday() if self.week_starts == "monday" else (d.weekday() + 1) % 7
            return d - timedelta(days=off)

        return (ws(day) - ws(term.start)).days // 7 + 1

    def period_for(self, day: date) -> PeriodCfg | None:
        term = self.term_for(day)
        candidates = term.periods if term else [p for t in self.terms for p in t.periods]
        return next((p for p in candidates if p.start <= day <= p.end), None)

    def between_terms(self, day: date) -> bool:
        """True on days after one known term ends and before the next begins."""
        if self.term_for(day) is not None:
            return False
        before = any(t.end < day for t in self.terms)
        after = any(t.start > day for t in self.terms)
        return before and after

    def classes_paused(self, day: date) -> bool:
        p = self.period_for(day)
        if p is not None and p.kind in ("break", "closed"):
            return True
        return self.between_terms(day)

    def label(self, day: date) -> str | None:
        """'Fall 2026 · Week 7', 'Thanksgiving Break', or 'Between terms'."""
        term = self.term_for(day)
        if term is None:
            return "Between terms" if self.between_terms(day) else None
        p = self.period_for(day)
        if p is not None and p.kind == "exams":
            return f"{term.name} · {p.name}"
        return f"{term.name} · Week {self.week_of(term, day)}"

    def day_notes(self, first: date, end_exclusive: date) -> dict[date, str]:
        """Break, exam and closed days in a range: {day: 'Thanksgiving Break'}."""
        out: dict[date, str] = {}
        d = first
        while d < end_exclusive:
            p = self.period_for(d)
            if p is not None:
                out[d] = p.name
            elif self.between_terms(d):
                out[d] = "Between terms"
            d += timedelta(days=1)
        return out


def _span(ev: Event) -> tuple[date, date]:
    """First and last date an event covers (all-day ends are exclusive)."""
    first = ev.start.date()
    last = (ev.end - timedelta(seconds=1)).date() if ev.end > ev.start else first
    return first, max(first, last)


def _clean(title: str) -> str:
    title = re.sub(r"\s+", " ", title).strip(" -–:")
    return title[:60]


def read_feed(text: str, cfg: AppConfig, today: date | None = None) -> list[TermCfg]:
    """Best-guess terms from an academic calendar feed (about two years either side of today)."""
    today = today or datetime.now(cfg.tz).date()
    tz = cfg.tz
    s = datetime.combine(today - timedelta(days=730), time.min, tzinfo=tz)
    e = datetime.combine(today + timedelta(days=730), time.min, tzinfo=tz)
    events = sorted(parse_events("_school", text, s, e, tz), key=lambda ev: ev.start)

    starts: list[tuple[date, str]] = []
    ends: list[date] = []
    periods: list[PeriodCfg] = []
    for ev in events:
        title = ev.summary
        first, last = _span(ev)
        if _START.search(title):
            starts.append((first, title))
        elif _END.search(title) or _TERM_END.search(title):
            ends.append(last)
        if _EXAMS.search(title):
            periods.append(
                PeriodCfg(
                    name="Finals" if last > first else _clean(title), start=first, end=last, kind="exams"
                )
            )
        elif _BREAK.search(title) and not re.search(r"\b(begin|begins|resume|resumes|ends?)\b", title, re.I):
            periods.append(PeriodCfg(name=_clean(title), start=first, end=last, kind="break"))
        elif _CLOSED.search(title):
            periods.append(PeriodCfg(name=_clean(title), start=first, end=last, kind="closed"))

    # One term per start; it ends at the latest "classes end"/exam day before the next start.
    starts = sorted({d: t for d, t in starts}.items())
    terms: list[TermCfg] = []
    for i, (start, title) in enumerate(starts):
        nxt = starts[i + 1][0] if i + 1 < len(starts) else start + timedelta(days=150)
        window_ends = [d for d in ends if start < d < nxt]
        window_ends += [p.end for p in periods if p.kind == "exams" and start < p.start < nxt]
        if not window_ends:
            continue
        end = max(window_ends)
        if (end - start).days > 160:  # not one term; probably a stray match
            continue
        m = _NAMED.search(title)
        season = m.group(1).title() if m else season_of(start)
        year = m.group(2) if m and m.group(2) else str(start.year)
        inside = [p for p in periods if start <= p.start and p.end <= end]
        # Merge duplicates (feeds often list the same break twice).
        uniq: dict[tuple, PeriodCfg] = {}
        for p in inside:
            uniq.setdefault((p.start, p.end, p.kind), p)
        terms.append(TermCfg(name=f"{season} {year}", start=start, end=end, periods=list(uniq.values())))
    return terms


def build_schedule(cfg: AppConfig, feed_terms: list[TermCfg]) -> Schedule:
    manual = list(cfg.school.terms)
    merged = list(manual)
    for t in feed_terms:
        if not any(m.start <= t.end and t.start <= m.end for m in manual):
            merged.append(t)
    merged.sort(key=lambda t: t.start)
    return Schedule(terms=merged, week_starts=cfg.week_starts)


class SchoolCalendar:
    """Downloads the academic calendar feed (if any) and answers term questions."""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.text: str | None = None
        self.etag: str | None = None
        self.last_success: datetime | None = None
        self.last_error: str | None = None
        self.feed_terms: list[TermCfg] = []
        self._schedule: Schedule | None = None
        self.version = 0  # bumps when the feed's terms change
        self._load_disk()

    # -- feed ---------------------------------------------------------------------------------

    def _disk_path(self) -> Path:
        h = hashlib.sha256(b"_school").hexdigest()[:16]
        return Path(self.cfg.cache_dir) / f"{h}.ics.cache"

    def _load_disk(self) -> None:
        url = self.cfg.school.calendar_url
        p = self._disk_path()
        if url and p.is_file():
            try:
                stored = p.read_text(encoding="utf-8")
                tag, _, body = stored.partition("\n")
                if tag == hashlib.sha256(url.encode()).hexdigest():
                    self._set_text(body)
            except OSError:
                pass

    def _set_text(self, text: str) -> None:
        self.text = text
        try:
            self.feed_terms = read_feed(text, self.cfg)
        except Exception as exc:  # a strange feed should never break the pages
            log.warning("couldn't read the academic calendar: %s", exc)
            self.feed_terms = []
        self._schedule = None
        self.version += 1

    def refresh(self, client: httpx.Client) -> None:
        sc = self.cfg.school
        if not (sc.enabled and sc.calendar_url):
            return
        url = normalize_url(sc.calendar_url)
        try:
            headers = {"If-None-Match": self.etag} if self.etag else {}
            if url.startswith("file://") or "://" not in url:
                text = Path(url.removeprefix("file://")).read_text(encoding="utf-8")
            else:
                resp = client.get(url, headers=headers)
                if resp.status_code == 304:
                    self.last_success, self.last_error = datetime.now(UTC), None
                    return
                resp.raise_for_status()
                text = resp.text
                if "BEGIN:VCALENDAR" not in text[:2000]:
                    raise ValueError("that link didn't return a calendar feed (ICS)")
                self.etag = resp.headers.get("ETag")
            if text != self.text:
                self._set_text(text)
                try:
                    self._disk_path().parent.mkdir(parents=True, exist_ok=True)
                    tag = hashlib.sha256(sc.calendar_url.encode()).hexdigest()
                    self._disk_path().write_text(tag + "\n" + text, encoding="utf-8")
                except OSError as exc:
                    log.warning("couldn't cache the academic calendar: %s", exc)
            self.last_success, self.last_error = datetime.now(UTC), None
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: " + re.sub(r"\w+://\S+", "<url>", str(exc))
            log.warning("academic calendar: %s", self.last_error)

    # -- questions ----------------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self.cfg.school.enabled

    @property
    def schedule(self) -> Schedule:
        if self._schedule is None:
            terms = self.feed_terms if self.cfg.school.calendar_url else []
            self._schedule = build_schedule(self.cfg, terms)
        return self._schedule

    def filter(self, events: list[Event]) -> list[Event]:
        """Drop class events on break days and between terms (when school mode says so)."""
        sc = self.cfg.school
        if not (sc.enabled and sc.pause_classes and sc.class_calendars):
            return events
        sched = self.schedule
        if not sched.terms:
            return events
        classes = set(sc.class_calendars)
        return [
            ev for ev in events if ev.calendar_id not in classes or not sched.classes_paused(ev.start.date())
        ]

    def viewer_context(self, first: date, end_exclusive: date, anchor: date) -> dict:
        """What viewers see: a term label for the period and notes on break/exam days."""
        sc = self.cfg.school
        if not (sc.enabled and sc.show_viewers):
            return {"term_label": None, "school_days": {}}
        sched = self.schedule
        return {"term_label": sched.label(anchor), "school_days": sched.day_notes(first, end_exclusive)}
