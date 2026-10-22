from datetime import datetime, time

import pytest
from conftest import WEEK

from kalends.config import HoursCfg, Level
from kalends.policy import BUSY_TITLE, clip_to_hours

SECRETS_FROM_COWORKERS = [
    "Doctor",
    "Clinic",
    "Luigi",
    "Dinner",
    "Grandma",
    "Family",
    "Surprise",
    "advisor",
    "POLY",
    "ASEC",
    "Mom",
    "Gym",
    "Morning run",
    "Late movie",
    "Saturday",
    "Focus",
    "Personal",
    "Polymer Lab 214",
    "#private",
    "#hide",
]


def week(kalends, audience):
    return kalends.week(kalends.cfg.audiences[audience], WEEK)


def titles(evs):
    return [e.title for e in evs]


# --- coworkers: busy default, lab=title, classes=label, family=hidden, Mon-Fri 8-18 -------


def test_coworkers_see_expected_blocks(kalends):
    evs = week(kalends, "coworkers")
    got = [(e.start.strftime("%a %H:%M"), e.end.strftime("%H:%M"), e.title) for e in evs]
    assert got == [
        ("Mon 09:00", "12:00", "Lab work"),
        ("Tue 09:00", "12:00", "Lab work"),
        ("Tue 10:00", "11:00", BUSY_TITLE),
        ("Tue 11:00", "12:15", "Class"),
        ("Tue 16:00", "17:00", "Group meeting (moved)"),
        ("Wed 09:00", "12:00", "Lab work"),
        ("Wed 13:00", "13:30", BUSY_TITLE),
        ("Thu 11:00", "12:15", "Class"),
        ("Thu 17:30", "18:00", "Evening seminar"),
    ]


def test_coworkers_never_receive_hidden_text(kalends):
    blob = repr(week(kalends, "coworkers"))
    for word in SECRETS_FROM_COWORKERS:
        assert word not in blob, word


def test_title_level_drops_location_and_notes(kalends):
    lab = [e for e in week(kalends, "coworkers") if e.title == "Lab work"]
    assert lab and all(e.location == "" and e.notes == "" for e in lab)


def test_busy_blocks_are_anonymous(kalends):
    busy = [e for e in week(kalends, "coworkers") if e.level is Level.BUSY]
    assert busy
    for b in busy:
        assert b.calendar_id is None and b.calendar_name is None and b.color is None
        assert "test" not in b.key  # source UIDs never leak


def test_window_drops_weekend_and_evening(kalends):
    evs = week(kalends, "coworkers")
    assert all(e.start.weekday() < 5 for e in evs)
    assert all(time(8) <= e.start.time() and e.end.time() <= time(18) for e in evs)


# --- partner: full default, lab=title, no window ----------------------------------------


def test_partner_sees_full_details(kalends):
    evs = week(kalends, "partner")
    dinner = next(e for e in evs if e.title == "Dinner with Sam")
    assert dinner.location == "Luigi's" and dinner.notes == "Reservation under Brandon"
    assert "Mom's birthday" in titles(evs)
    assert "Family dinner" in titles(evs)


def test_private_tag_and_flag_reduce_to_busy_for_everyone(kalends):
    evs = week(kalends, "partner")
    blob = repr(evs)
    assert "Doctor" not in blob and "Clinic" not in blob and "advisor" not in blob
    assert "#private" not in blob
    busy = [e for e in evs if e.level is Level.BUSY]
    assert {(b.start.day, b.start.hour) for b in busy} == {(27, 10), (28, 13)}


def test_hide_tag_hides_from_everyone(kalends):
    for aud in ("partner", "coworkers"):
        assert "Surprise" not in repr(week(kalends, aud))


def test_free_events_shown_only_with_titles(kalends):
    assert "Focus time" in titles(week(kalends, "partner"))
    cfg = kalends.cfg
    cfg.audiences["partner"].calendars["personal"] = Level.BUSY
    evs = week(kalends, "partner")
    mon = [e for e in evs if e.start.day == 26 and e.start.hour == 14]
    assert mon == []  # a "free" event never turns into a Busy block


def test_overrides_never_increase_detail(kalends):
    cfg = kalends.cfg
    cfg.audiences["coworkers"].calendars["personal"] = Level.HIDDEN
    assert "Doctor" not in repr(week(kalends, "coworkers"))
    assert all(e.start.hour != 10 or e.level is not Level.BUSY for e in week(kalends, "coworkers"))


def test_all_day_busy_hidden_by_default_and_optional(kalends):
    cfg = kalends.cfg
    aud = cfg.audiences["partner"]
    aud.calendars["personal"] = Level.BUSY
    assert not [e for e in week(kalends, "partner") if e.all_day]
    aud.show_all_day_busy = True
    all_day = [e for e in week(kalends, "partner") if e.all_day]
    assert [(e.title, e.start.day) for e in all_day] == [(BUSY_TITLE, 30)]


def test_overlapping_busy_blocks_merge(kalends):
    cfg = kalends.cfg
    aud = cfg.audiences["coworkers"]
    aud.calendars = {}  # everything busy
    evs = week(kalends, "coworkers")
    tue = [(e.start.strftime("%H:%M"), e.end.strftime("%H:%M")) for e in evs if e.start.day == 27]
    # Lab 9-12, doctor 10-11 and class 11-12:15 collapse into one block
    assert tue == [("09:00", "12:15"), ("16:00", "17:00")]


def test_label_level_uses_calendar_label(kalends):
    cls = [e for e in week(kalends, "coworkers") if e.level is Level.LABEL]
    assert {e.title for e in cls} == {"Class"}
    assert all(e.location == "" for e in cls)


@pytest.mark.parametrize(
    ("s", "e", "expected"),
    [
        ((7, 0), (9, 0), [((8, 0), (9, 0))]),
        ((17, 30), (19, 0), [((17, 30), (18, 0))]),
        ((6, 0), (7, 0), []),
        ((8, 0), (18, 0), [((8, 0), (18, 0))]),
    ],
)
def test_clip_to_hours(cfg, s, e, expected):
    tz = cfg.tz
    hours = HoursCfg(days=["mon"], start=time(8), end=time(18))
    a = datetime(2026, 10, 26, *s, tzinfo=tz)
    b = datetime(2026, 10, 26, *e, tzinfo=tz)
    got = [((x.hour, x.minute), (y.hour, y.minute)) for x, y in clip_to_hours(a, b, hours, tz)]
    assert got == expected


def test_multi_day_event_clipped_per_day(cfg):
    tz = cfg.tz
    hours = HoursCfg(days=["mon", "tue", "wed", "thu", "fri"], start=time(8), end=time(18))
    a = datetime(2026, 10, 30, 12, tzinfo=tz)  # Fri noon
    b = datetime(2026, 11, 2, 10, tzinfo=tz)  # Mon 10am
    parts = clip_to_hours(a, b, hours, tz)
    assert [(x.day, x.hour, y.hour) for x, y in parts] == [(30, 12, 18), (2, 8, 10)]


def test_overnight_tail_does_not_stretch_grid(kalends):
    from kalends.render.week import build_week

    aud = kalends.cfg.audiences["partner"]
    view = build_week(kalends.cfg, aud, WEEK, kalends.week(aud, WEEK), WEEK)
    assert view.hour_start == 6  # earliest real start is the 6:30 run, not the 12 AM movie tail
    sat = next(d for d in view.days if d.day.day == 31)
    assert [b.ev.title for b in sat.all_day] == ["Late movie"]


def test_tags_stripped_case_insensitively_including_unicode():
    from kalends.policy import _strip_tags

    assert _strip_tags("İstanbul trip #PRIVATE", ["#private"]) == "İstanbul trip"
    assert _strip_tags("Dinner #Private with #hide Sam", ["#private", "#hide"]) == "Dinner with Sam"
