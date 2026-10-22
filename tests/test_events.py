from datetime import datetime, time

from conftest import WEEK

from kalends.core import week_range


def _by_title(kalends, title):
    s, e = week_range(WEEK, kalends.cfg)
    return [ev for ev in kalends.events_between(s, e) if ev.summary == title]


def test_weekly_recurrence_expands(kalends):
    gym = _by_title(kalends, "Gym")
    assert [g.start.weekday() for g in gym] == [0, 2, 4]  # Mon, Wed, Fri
    assert all(g.start.time() == time(7, 0) for g in gym)


def test_moved_instance_replaces_original(kalends):
    moved = _by_title(kalends, "Group meeting (moved)")
    assert len(moved) == 1
    assert moved[0].start.time() == time(16, 0)
    assert _by_title(kalends, "Group meeting") == []  # original 3 PM slot is gone


def test_exdate_removes_occurrence(kalends):
    s, e = week_range(WEEK.replace(day=19), kalends.cfg)  # week of Oct 19
    titles = [ev.summary for ev in kalends.events_between(s, e)]
    assert "Group meeting" not in titles


def test_outlook_windows_timezone_name(kalends):
    lab = _by_title(kalends, "Lab work")
    assert lab and all(ev.start.time() == time(9, 0) for ev in lab)
    assert str(lab[0].start.tzinfo) == "America/New_York"


def test_daily_recurrence_keeps_wall_clock_across_dst(kalends):
    runs = _by_title(kalends, "Morning run")
    assert [r.start.day for r in runs] == [29, 30, 31, 1]
    assert all(r.start.time() == time(6, 30) for r in runs)
    # Nov 1 is standard time (UTC-5), Oct 31 daylight time (UTC-4)
    assert runs[-1].start.utcoffset() != runs[-2].start.utcoffset()


def test_all_day_and_flags(kalends):
    (bday,) = _by_title(kalends, "Mom's birthday")
    assert bday.all_day and bday.start.time() == time(0) and (bday.end - bday.start).days == 1
    (focus,) = _by_title(kalends, "Focus time")
    assert focus.free
    (adv,) = _by_title(kalends, "1:1 with advisor")
    assert adv.private


def test_cancelled_events_skipped(kalends):
    assert _by_title(kalends, "Cancelled coffee") == []


def test_bad_feed_is_ignored(cfg):
    from kalends.events import parse_events

    s = datetime(2026, 10, 26, tzinfo=cfg.tz)
    e = datetime(2026, 11, 2, tzinfo=cfg.tz)
    assert parse_events("x", "not a calendar", s, e, cfg.tz) == []
