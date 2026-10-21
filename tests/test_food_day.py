from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest

from fakes import utc
from gwc.analysis.food_day import FoodDays, current_food_day
from gwc.db.migrate import upgrade
from gwc.db.models import Sleep
from gwc.db.session import session_scope

NY = ZoneInfo("America/New_York")
MON = date(2026, 10, 19)
TUE = MON + timedelta(days=1)
WED = MON + timedelta(days=2)


def days(sleeps: dict[date, object]) -> FoodDays:
    return FoodDays(NY, 9, sleeps)


def test_day_ends_when_garmin_sleep_starts():
    # Fell asleep 04:15 Tuesday (EDT = UTC-4); Garmin labels that sleep with Tuesday.
    fd = days({TUE: utc(2026, 10, 20, 8, 15)})
    assert fd.end_of(MON) == (utc(2026, 10, 20, 8, 15), True)
    assert fd.day_of(utc(2026, 10, 20, 6, 30)) == MON  # 02:30 Tue counts toward Monday
    assert fd.day_of(utc(2026, 10, 20, 8, 15)) == TUE


def test_early_bedtime_before_midnight():
    # Asleep 23:00 Monday local, labeled Tuesday.
    fd = days({TUE: utc(2026, 10, 20, 3, 0)})
    assert fd.end_of(MON)[0] == utc(2026, 10, 20, 3, 0)
    assert fd.day_of(utc(2026, 10, 20, 3, 30)) == TUE  # 23:30 Monday local is already Tuesday


def test_fallback_hour_without_sleep():
    fd = days({})
    assert fd.end_of(MON) == (utc(2026, 10, 20, 13, 0), False)  # 09:00 EDT Tuesday
    assert fd.day_of(utc(2026, 10, 20, 12, 59)) == MON
    assert fd.day_of(utc(2026, 10, 20, 13, 0)) == TUE


def test_implausible_sleep_is_ignored():
    # A "main sleep" starting 20:00 on Tuesday can't end Monday's food day.
    fd = days({TUE: utc(2026, 10, 21, 0, 0)})
    assert fd.end_of(MON) == (utc(2026, 10, 20, 13, 0), False)


def test_bounds_are_contiguous():
    fd = days({TUE: utc(2026, 10, 20, 8, 15), WED: utc(2026, 10, 21, 7, 0)})
    assert fd.bounds(TUE) == (utc(2026, 10, 20, 8, 15), utc(2026, 10, 21, 7, 0))
    assert fd.bounds(MON)[1] == fd.bounds(TUE)[0]


def test_dst_fallback_uses_local_clock():
    sun = date(2026, 11, 1)  # DST ends 02:00 Nov 1
    fd = days({})
    assert fd.end_of(sun)[0] == utc(2026, 11, 2, 14, 0)  # 09:00 EST


@pytest.fixture
def db(isolated_settings):
    upgrade()


def test_load_and_current_day(db):
    with session_scope() as s:
        s.add(
            Sleep(
                calendar_date=TUE,
                start_at=utc(2026, 10, 20, 8, 15),
                end_at=utc(2026, 10, 20, 16, 0),
                is_nap=False,
            )
        )
        s.add(
            Sleep(
                calendar_date=TUE,
                start_at=utc(2026, 10, 20, 20, 0),
                end_at=utc(2026, 10, 20, 21, 0),
                is_nap=True,
            )
        )
    with session_scope() as s:
        fd = FoodDays.load(s, NY, MON, TUE)
        assert fd.end_of(MON) == (utc(2026, 10, 20, 8, 15), True)
        # Tuesday afternoon, Wednesday's sleep not synced: still Tuesday until 09:00 Wed.
        assert current_food_day(s, NY, utc(2026, 10, 21, 5, 0)) == TUE
        assert current_food_day(s, NY, utc(2026, 10, 20, 7, 0)) == MON
