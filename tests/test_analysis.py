from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from fakes import utc
from gwc.analysis import energy, targets
from gwc.analysis.days import DayRecord, load_days
from gwc.analysis.exercise import (
    EAT_BACK_PREF,
    add_manual,
    delete_manual,
    manual_by_food_day,
    net_by_calendar_date,
    net_kcal,
    workouts_by_food_day,
)
from gwc.analysis.food_day import FoodDays
from gwc.analysis.projection import project, simulate
from gwc.analysis.snapshot import build_snapshot
from gwc.analysis.tdee import adaptive_tdee, choose, fit_start, garmin_tdee, linear_slope
from gwc.analysis.trend import Weighin, daily_weighins, ewma, weight_trend
from gwc.analysis.units import KG_PER_LB
from gwc.db.migrate import upgrade
from gwc.db.models import (
    Activity,
    BodyComp,
    DailySummary,
    DailyTarget,
    FoodLog,
    LogSource,
)
from gwc.db.session import session_scope
from gwc.nutrition import log
from gwc.prefs import set_pref
from gwc.web.components.analysis_text import plan_summary, step_summary, workout_summary

NY = ZoneInfo("America/New_York")
D0 = date(2026, 10, 1)


@pytest.fixture
def db(isolated_settings):
    upgrade()


def day(n: int) -> date:
    return D0 + timedelta(days=n)


def rec(n: int, kcal: float | None = None, garmin: float | None = 3000, **kw) -> DayRecord:
    fields = {
        "day": day(n),
        "intake": {"kcal": kcal} if kcal is not None else None,
        "entries": 1 if kcal is not None else 0,
        "marked_incomplete": False,
        "garmin_total": garmin,
        "garmin_resting": 2000 if garmin else None,
        "garmin_active": None,
        "steps": None,
        "garmin_provisional": False,
    }
    fields.update(kw)
    return DayRecord(**fields)


# --------------------------------------------------------------------------- energy


def test_rho_matches_plan():
    # ≈ 3,400 kcal/lb at 30 kg of fat mass.
    assert energy.rho(30) * KG_PER_LB == pytest.approx(3400, abs=25)
    assert energy.fat_fraction(0) == 0
    assert energy.rho(0) == energy.LEAN_KCAL_PER_KG
    assert energy.rho(10) < energy.rho(40) < energy.FAT_KCAL_PER_KG


# --------------------------------------------------------------------------- trend


def test_ewma_starts_at_first_value_and_carries_gaps():
    out = ewma({day(0): 100.0, day(3): 90.0}, 0.1, day(4))
    assert list(out) == [day(i) for i in range(5)]
    assert out[day(0)] == out[day(1)] == out[day(2)] == 100
    assert out[day(3)] == pytest.approx(95.0)  # warm-up: running mean of 2
    assert out[day(4)] == pytest.approx(95.0)


def test_ewma_after_warmup():
    values = {day(i): 100.0 for i in range(10)} | {day(10): 90.0}
    assert ewma(values, 0.1, day(10))[day(10)] == pytest.approx(99.0)


def test_weight_trend_tracks_body_fat_separately():
    ws = [Weighin(day(0), 100, None), Weighin(day(1), 99, 30.0), Weighin(day(2), 98, 32.0)]
    pts = weight_trend(ws, 0.5, day(3))
    assert pts[0].fat_trend_pct is None and pts[0].fat_kg is None
    assert pts[2].fat_trend_pct == pytest.approx(31.0)
    assert pts[3].weight_kg is None
    assert pts[3].trend_kg == pts[2].trend_kg
    assert pts[3].fat_kg == pytest.approx(pts[3].trend_kg * 0.31)


def test_daily_weighins_average_by_local_day(db):
    with session_scope() as s:
        # 23:30 EDT on Oct 1 and 07:00 EDT on Oct 2 are different local days.
        s.add(BodyComp(measured_at=utc(2026, 10, 2, 3, 30), weight_kg=100, body_fat_pct=30))
        s.add(BodyComp(measured_at=utc(2026, 10, 2, 11, 0), weight_kg=99))
        s.add(BodyComp(measured_at=utc(2026, 10, 2, 12, 0), weight_kg=98, body_fat_pct=28))
    with session_scope() as s:
        ws = daily_weighins(s, NY)
    assert ws == [Weighin(day(0), 100, 30), Weighin(day(1), 98.5, 28)]


# --------------------------------------------------------------------------- TDEE


def test_linear_slope():
    slope, se = linear_slope([(0, 10), (1, 12), (2, 14)])
    assert slope == pytest.approx(2) and se == pytest.approx(0)
    slope, se = linear_slope([(0, 0), (1, 1), (2, 0), (3, 1)])
    assert slope == pytest.approx(0.2) and se > 0


def test_garmin_tdee_uses_last_7_final_days():
    days = [rec(i, garmin=2000) for i in range(5)] + [rec(i, garmin=3000) for i in range(5, 12)]
    days.append(rec(12, garmin=9999, garmin_provisional=True))
    days.append(rec(13, garmin=None))
    est = garmin_tdee(days)
    assert est.ready and est.kcal == 3000 and est.days_used == 7
    assert est.resting_kcal == 2000
    assert est.se == pytest.approx(150)  # only the 5% systematic part; the days agree
    assert not garmin_tdee([rec(0, garmin=None)]).ready


def test_garmin_tdee_base_leaves_out_workouts():
    days = [rec(i, garmin=3000, garmin_exercise=700 if i == 3 else 0) for i in range(7)]
    est = garmin_tdee(days)
    assert est.kcal == 3000 and est.exercise_kcal == pytest.approx(100)
    assert est.base_kcal == pytest.approx(2900)


def test_only_adaptive_burn_counts_workouts_entered_in_the_app():
    # Garmin's totals never saw them, so its base mustn't take them out.
    days = [rec(i, garmin=3000, manual_exercise=700 if i == 3 else 0) for i in range(7)]
    assert garmin_tdee(days).base_kcal == pytest.approx(3000)
    # Weight change does reflect them, so the trend-based base leaves them out.
    records, weighins = _losing(21, 2500, -0.1)
    records[5] = rec(5, kcal=2500, manual_exercise=420)
    est = adaptive_tdee(records, weighins, rho=7700, window=21)
    assert est.exercise_kcal == pytest.approx(20)
    assert est.base_kcal == pytest.approx(2500 + 770 - 20)


def test_garmin_tdee_base_leaves_out_above_usual_steps():
    active = [300, 300, 300, 800, 300, 600, 900]
    exercise = [0, 0, 0, 500, 0, 0, 0]  # day 3's 800 is mostly a workout
    days = [
        rec(i, garmin=3000, garmin_active=a, garmin_exercise=e)
        for i, (a, e) in enumerate(zip(active, exercise, strict=True))
    ]
    est = garmin_tdee(days)
    # Outside workouts: 300 ×5, 600, 900 → usual 300 (median), 900/7 above it on average.
    assert est.typical_step_kcal == 300
    assert est.step_kcal == pytest.approx(900 / 7)
    assert est.base_kcal == pytest.approx(3000 - 500 / 7 - 900 / 7)
    # Too few days to know what's usual: no step adjustment either way.
    short = garmin_tdee(days[:2])
    assert short.typical_step_kcal is None and short.base_kcal == pytest.approx(3000)


def test_step_credit_is_a_share_above_usual_rounded_down():
    assert targets.step_credit(50, 300, 385) == 40  # 42.5 → 40
    assert targets.step_credit(50, 300, 250) == 0  # a quiet day takes nothing away
    assert targets.step_credit(0, 300, 900) == 0
    assert targets.step_credit(None, 300, 900) == 0
    assert targets.step_credit(50, None, 900) == 0
    assert targets.step_credit(50, 300, None) == 0


def _losing(days: int, intake: float, kg_per_day: float, start: float = 120):
    records = [rec(i, kcal=intake) for i in range(days)]
    weighins = [Weighin(day(i), start + kg_per_day * i, None) for i in range(days)]
    return records, weighins


def test_adaptive_tdee_from_intake_and_slope():
    records, weighins = _losing(21, 2500, -0.1)
    est = adaptive_tdee(records, weighins, rho=7700, window=21)
    assert est.ready
    assert est.kcal == pytest.approx(2500 + 770)
    assert est.slope_kg_per_week == pytest.approx(-0.7)
    assert est.se == pytest.approx(0, abs=1e-6)


def test_adaptive_tdee_calibrating():
    records, weighins = _losing(21, 2500, -0.1)
    # Only 9 complete days.
    sparse = [rec(i, kcal=None) if i < 12 else r for i, r in enumerate(records)]
    est = adaptive_tdee(sparse, weighins, rho=7700)
    assert not est.ready and "complete food days" in est.note
    # Weigh-ins over only 10 days.
    est = adaptive_tdee(records, weighins[-10:], rho=7700)
    assert not est.ready and "weigh-ins spanning 10 of 14" in est.note
    # Incomplete days don't count.
    flagged = [DayRecord(**{**r.__dict__, "marked_incomplete": True}) for r in records]
    assert not adaptive_tdee(flagged, weighins, rho=7700).ready


def _started_logging(through: int, start: int = 7):
    """No logging before day `start`, then 2,500 kcal/day: 2 kg of water off in the first
    two days, and 0.1 kg/day after."""
    records = [rec(i, kcal=2500 if i >= start else None) for i in range(through + 1)]

    def kg(i: int) -> float:
        if i <= start:
            return 122.0
        return 122.0 - min(i - start, 2) - 0.1 * (i - start)

    return records, [Weighin(day(i), kg(i), None) for i in range(through + 1)]


def test_adaptive_tdee_skips_water_weight_after_starting_to_log():
    # Too soon: only days 14–20 count, a week after logging started on day 7.
    est = adaptive_tdee(*_started_logging(20), rho=7700, window=21)
    assert not est.ready and "7 of 10 complete food days" in est.note and "since" in est.note
    # Days 14–27 count; the water drop on days 8–9 doesn't read as fat loss.
    est = adaptive_tdee(*_started_logging(27), rho=7700, window=21)
    assert est.ready and est.days_used == 14
    assert est.kcal == pytest.approx(2500 + 770)
    # Still skipped once the start of logging has left the window.
    est = adaptive_tdee(*_started_logging(30), rho=7700, window=21)
    assert est.days_used == 17 and est.kcal == pytest.approx(2500 + 770)


def test_adaptive_fit_start():
    records, _ = _started_logging(30)
    assert fit_start(records, 21) == (day(14), True)
    # A short gap isn't a new start.
    gap = [rec(i) if 18 <= i <= 20 else r for i, r in enumerate(records)]
    assert fit_start(gap, 10) == (day(21), False)
    # Without history before the window, logging counts as ongoing.
    assert fit_start(records[7:], 21) == (day(10), False)
    # Weigh-ins from before the first complete food day are left out.
    records, weighins = _losing(21, 2500, -0.1)
    early = [Weighin(day(i - 3), 130.0, None) for i in range(3)] + weighins
    est = adaptive_tdee([rec(i - 3) for i in range(3)] + records, early, rho=7700, window=24)
    assert est.kcal == pytest.approx(2500 + 770)


def test_choose():
    g = garmin_tdee([rec(0)])
    a_ready = adaptive_tdee(*_losing(21, 2500, -0.1), rho=7700)
    a_not = adaptive_tdee([], [], rho=7700)
    assert choose("auto", g, a_ready) is a_ready
    assert choose("auto", g, a_not) is g
    assert choose("garmin", g, a_ready) is g
    assert choose("adaptive", g, a_not) is g


# --------------------------------------------------------------------------- projection


def test_simulate_balance_and_adaptation():
    assert simulate(120, 40, 3000, 3000, 28, 22) == [120] * 29
    flat = simulate(120, 40, 2500, 3000, 28, 0)
    adapting = simulate(120, 40, 2500, 3000, 28, 22)
    assert flat[1] == pytest.approx(120 - 500 / energy.rho(40))
    assert adapting[-1] > flat[-1]  # burn drops as weight drops, so loss slows
    assert all(b < a for a, b in zip(adapting, adapting[1:], strict=False))


def test_project_band_and_determinism():
    kwargs = {
        "key": "garmin",
        "label": "x",
        "start_day": day(0),
        "start_kg": 120,
        "fat_kg": 40,
        "tdee_kcal": 3000,
        "tdee_se": 150,
        "intakes": [2200, 2600, 2400, 2500, 2300, 2700, 2400],
    }
    a, b = project(**kwargs), project(**kwargs)
    assert a == b
    assert len(a.days) == len(a.expected) == 29 and a.days[-1] == day(28)
    assert a.p10[0] == a.p90[0] == 120
    assert a.p10[-1] < a.expected[-1] < a.p90[-1]
    assert a.change_kg < 0
    fixed = project(**{**kwargs, "intakes": None, "fixed_intake": 2500})
    assert fixed.intake_kcal == 2500
    with pytest.raises(ValueError):
        project(**{**kwargs, "intakes": None})


# --------------------------------------------------------------------------- targets


BASE = {
    "tdee_kcal": 3200,
    "resting_kcal": 2200,
    "weight_kg": 120,
    "lean_kg": 80,
    "rho": 7600,
}


def test_suggest_macros():
    plan = targets.suggest(goal_kg_per_month=2, **BASE)
    deficit = 2 * 7600 / energy.DAYS_PER_MONTH
    assert plan.values["kcal"] == round((3200 - deficit) / 10) * 10
    k = plan.values["kcal"]
    assert plan.values["protein_g"] == 192  # 2.4 g × 80 kg lean
    assert plan.values["fat_g"] == round(max(0.25 * k / 9, 60))
    expected_carbs = (k - 4 * 192 - 9 * plan.values["fat_g"]) / 4
    assert plan.values["carbs_g"] == pytest.approx(expected_carbs, abs=0.5)
    assert plan.values["fiber_g"] == round(14 * k / 1000)
    assert plan.values["sugar_g"] == round(0.1 * k / 4)
    assert plan.values["sodium_mg"] == 2300
    assert plan.flags == [] and plan.complete
    assert plan.effective_kg_per_month == pytest.approx(2, abs=0.01)


def test_suggest_no_goal_is_maintenance():
    plan = targets.suggest(goal_kg_per_month=None, **BASE)
    assert plan.values["kcal"] == 3200 and targets.NO_GOAL in plan.flags


def test_suggest_resting_floor():
    plan = targets.suggest(goal_kg_per_month=5, **{**BASE, "resting_kcal": 2905})
    assert plan.values["kcal"] == 2180  # 75 % of 2,905 = 2,178.75, rounded up
    assert plan.floor_kcal == pytest.approx(2178.75)
    assert targets.RESTING_FLOOR in plan.flags
    assert plan.effective_kg_per_month == pytest.approx(1020 * energy.DAYS_PER_MONTH / 7600)


def test_suggest_below_resting_is_allowed():
    # 3,200 − 2 kg × 7,600 / 30.44 ≈ 2,700: under resting burn, over the floor.
    plan = targets.suggest(goal_kg_per_month=2, **{**BASE, "resting_kcal": 2905})
    assert plan.values["kcal"] == 2700 and targets.RESTING_FLOOR not in plan.flags


def test_suggest_minimum_kcal():
    for resting in (1400, None):
        plan = targets.suggest(
            goal_kg_per_month=4, **{**BASE, "tdee_kcal": 2000, "resting_kcal": resting}
        )
        assert plan.values["kcal"] == 1200 and targets.RESTING_FLOOR in plan.flags


def test_suggest_weekly_cap():
    plan = targets.suggest(goal_kg_per_month=20, **{**BASE, "resting_kcal": None})
    cap = 120 * 0.01 * energy.DAYS_PER_MONTH / 7
    assert targets.WEEKLY_CAP in plan.flags
    assert plan.effective_kg_per_month == pytest.approx(cap, abs=0.05)
    gain = targets.suggest(goal_kg_per_month=-20, **{**BASE, "resting_kcal": None})
    assert gain.values["kcal"] > 3200 and targets.WEEKLY_CAP in gain.flags


def test_overrides_feed_derived_macros():
    plan = targets.suggest(goal_kg_per_month=2, overrides={"kcal": 2000, "protein_g": 150}, **BASE)
    assert plan.values["kcal"] == 2000 and plan.values["protein_g"] == 150
    assert plan.overridden == ["kcal", "protein_g"]
    assert plan.suggested["protein_g"] == 192  # what a blank box would get
    assert plan.values["fiber_g"] == 28
    assert plan.values["fat_g"] == 60  # 0.5 g/kg beats 25% of 2,000 kcal
    assert plan.values["carbs_g"] == round((2000 - 600 - 540) / 4)


def test_suggest_without_data():
    plan = targets.suggest(
        goal_kg_per_month=2,
        tdee_kcal=None,
        resting_kcal=None,
        weight_kg=None,
        lean_kg=None,
        rho=None,
        overrides={"kcal": 2100},
    )
    assert plan.values["kcal"] == 2100 and plan.values["protein_g"] is None
    assert plan.values["sodium_mg"] == 2300
    assert {targets.NO_TDEE, targets.NO_WEIGHT} <= set(plan.flags)
    assert not plan.complete


# --------------------------------------------------------------------------- database


NOW = utc(2026, 10, 20, 18, 0)  # 14:00 EDT on food day Oct 20


def _seed(s, days: int = 35):  # long enough that logging isn't a fresh start
    today = NOW.astimezone(NY).date()
    for i in range(days, 0, -1):
        d = today - timedelta(days=i)
        s.add(
            DailySummary(
                calendar_date=d,
                total_kcal=3100,
                resting_kcal=2300,
                steps=8000,
                is_provisional=False,
            )
        )
        at = datetime(d.year, d.month, d.day, 8, 0, tzinfo=NY).astimezone(UTC)
        s.add(BodyComp(measured_at=at, weight_kg=120 - 0.05 * (days - i), body_fat_pct=30))
        s.add(
            FoodLog(
                eaten_at=at + timedelta(hours=4),
                tz="America/New_York",
                description="x",
                grams=100,
                kcal=2600,
                protein_g=150,
                source=LogSource.CUSTOM,
            )
        )


def test_entries_by_day_and_load_days(db):
    with session_scope() as s:
        base = dict(tz="America/New_York", description="x", grams=1, source=LogSource.CUSTOM)
        s.add(FoodLog(eaten_at=utc(2026, 10, 2, 6, 0), kcal=100, **base))  # 02:00 Oct 2 → Oct 1
        s.add(FoodLog(eaten_at=utc(2026, 10, 2, 16, 0), kcal=200, **base))
        s.add(FoodLog(eaten_at=utc(2026, 10, 2, 17, 0), kcal=50, food_day_override=day(0), **base))
        s.add(DailySummary(calendar_date=day(1), total_kcal=2900, is_provisional=True))
        log.set_incomplete(s, day(1), True)
    with session_scope() as s:
        grouped = log.entries_by_day(s, day(0), day(1), FoodDays.load(s, NY, day(0), day(1)))
        assert [e.kcal for e in grouped[day(0)]] == [100, 50]
        assert [e.kcal for e in grouped[day(1)]] == [200]
        recs = load_days(s, NY, day(0), day(2))
    assert [r.kcal for r in recs] == [150, 200, None]
    assert recs[0].complete and not recs[1].complete and not recs[2].complete
    assert recs[1].garmin_total == 2900 and not recs[1].has_garmin


def test_snapshot_end_to_end(db):
    with session_scope() as s:
        _seed(s)
    with session_scope() as s:
        snap = build_snapshot(s, NY, NOW)
    assert snap.today == date(2026, 10, 20)
    assert snap.garmin.kcal == 3100 and snap.garmin.resting_kcal == 2300
    assert len(snap.intakes7) == 7
    assert snap.adaptive.ready
    # Losing 0.05 kg/day while eating 2,600 → burn ≈ 2,600 + 0.05 ρ.
    assert snap.adaptive.kcal == pytest.approx(2600 + 0.05 * snap.rho, rel=1e-6)
    assert snap.chosen is snap.adaptive
    assert not snap.body_fat_assumed
    assert snap.lean_kg == pytest.approx(snap.weight_kg * 0.7, rel=0.01)


def test_day_targets_freeze_and_refreeze(db):
    with session_scope() as s:
        _seed(s)
        targets.set_goal(2.0)
    today = date(2026, 10, 20)
    with session_scope() as s:
        plan = targets.day_targets(s, NY, NOW, today)
        assert plan.frozen and plan.complete
        first_kcal = plan.values["kcal"]
        # Past days aren't recorded.
        targets.day_targets(s, NY, NOW, today - timedelta(days=3))
    with session_scope() as s:
        assert s.get(DailyTarget, today) is not None
        assert s.get(DailyTarget, today - timedelta(days=3)) is None
        # New Garmin data doesn't move today's frozen targets…
        s.get(DailySummary, 1).total_kcal = 5000
    targets.set_goal(0.0)
    with session_scope() as s:
        assert targets.day_targets(s, NY, NOW, today).values["kcal"] == first_kcal
    # …but changing settings re-computes them.
    targets.refreeze_today(NY, NOW)
    with session_scope() as s:
        again = targets.day_targets(s, NY, NOW, today)
        assert again.values["kcal"] > first_kcal
        assert targets.frozen_targets(s, NY, today, today) == {today: again.values["kcal"]}


# --------------------------------------------------------------------------- workouts


def _workout(s, at: datetime, kcal: float, minutes: float, n: int = 1, name: str = "Run"):
    s.add(
        Activity(
            garmin_activity_id=n,
            activity_type="running",
            name=name,
            start_at=at,
            duration_s=minutes * 60,
            kcal=kcal,
        )
    )


def test_net_kcal():
    assert net_kcal(500, 3600, 2400) == pytest.approx(400)
    assert net_kcal(50, 3600, 2400) == 0
    assert net_kcal(None, 3600, 2400) == 0
    assert net_kcal(300, None, None) == 300


def test_workouts_follow_food_days(db):
    with session_scope() as s:
        s.add(DailySummary(calendar_date=day(1), resting_kcal=2400, is_provisional=False))
        # 01:00 on Oct 2 is still food day Oct 1 for a night owl; 10:00 is Oct 2.
        _workout(s, datetime(2026, 10, 2, 1, 0, tzinfo=NY), 500, 60, n=1)
        _workout(s, datetime(2026, 10, 2, 10, 0, tzinfo=NY), 300, 30, n=2)
    with session_scope() as s:
        by_day = workouts_by_food_day(s, NY, day(0), day(1))
        by_date = net_by_calendar_date(s, NY, day(0), day(1))
    assert [w.kcal for w in by_day[day(0)]] == [500]
    assert [w.kcal for w in by_day[day(1)]] == [300]
    assert by_day[day(0)][0].net_kcal == pytest.approx(400)
    # Garmin's totals are by calendar date, so the base burn pairs both with Oct 2.
    assert by_date == {day(1): pytest.approx(400 + 250)}


def test_day_in_progress_uses_finished_days_resting_rate(db):
    with session_scope() as s:
        s.add(DailySummary(calendar_date=day(0), resting_kcal=2400, is_provisional=False))
        # Garmin accrues resting kcal through the day, so today's reads low until it ends.
        s.add(DailySummary(calendar_date=day(1), resting_kcal=1200, is_provisional=True))
        _workout(s, datetime(2026, 10, 2, 18, 0, tzinfo=NY), 500, 60)
    with session_scope() as s:
        (w,) = workouts_by_food_day(s, NY, day(1), day(1))[day(1)]
    assert w.net_kcal == pytest.approx(400)


def test_todays_workouts_raise_todays_targets(db):
    today = date(2026, 10, 20)
    with session_scope() as s:
        _seed(s)
        _workout(s, NOW - timedelta(days=3), 600, 60, n=1)  # in the burn window
        targets.set_goal(2.0)
    with session_scope() as s:
        snap = build_snapshot(s, NY, NOW)
        avg = (600 - 2300 / 24) / 21  # one workout over the 21-day trend window
        assert snap.chosen.exercise_kcal == pytest.approx(avg)
        before = targets.day_targets(s, NY, NOW, today, snap=snap)
        assert before.workout_kcal == 0 and before.values == before.base_values
        assert before.tdee_kcal == pytest.approx(snap.chosen.kcal - avg)
        assert before.typical_kcal == pytest.approx(before.values["kcal"] + 0.75 * avg)
    with session_scope() as s:
        _workout(s, NOW - timedelta(hours=2), 600, 60, n=2)  # today, after the freeze
    with session_scope() as s:
        after = targets.day_targets(s, NY, NOW, today)
        # 75 % of 600 − 1 h of resting (2,300/24) = 378 → 380.
        assert after.workout_kcal == 380
        assert after.values["kcal"] == before.values["kcal"] + 380
        assert after.values["carbs_g"] == before.values["carbs_g"] + 71  # 75 % of 380 / 4
        assert after.values["fat_g"] == before.values["fat_g"] + 11  # 25 % of 380 / 9
        assert after.values["protein_g"] == before.values["protein_g"]
        assert after.values["fiber_g"] == before.values["fiber_g"] + 5
        # The frozen row keeps the base, so later syncs keep adjusting it.
        assert s.get(DailyTarget, today).kcal == before.values["kcal"]
        assert targets.frozen_targets(s, NY, today, today) == {today: after.values["kcal"]}
    assert workout_summary(after).startswith("+380 kcal for Run (60 min): 75% of 504 kcal")
    assert "without workouts" in plan_summary(after, "lb")


def test_workouts_add_on_top_of_overrides(db):
    today = date(2026, 10, 20)
    with session_scope() as s:
        _seed(s)
        _workout(s, NOW - timedelta(hours=2), 600, 60)
    targets.set_manual_targets({"kcal": 2500, "fat_g": 80})
    set_pref(EAT_BACK_PREF, 100)
    with session_scope() as s:
        plan = targets.day_targets(s, NY, NOW, today)
    assert plan.workout_kcal == 500  # 504 net at 100 %
    assert plan.values["kcal"] == 3000
    assert plan.values["fat_g"] == 80  # overridden, so carbs take it all
    assert plan.values["carbs_g"] == plan.base_values["carbs_g"] + 125


def test_steps_above_usual_raise_todays_targets(db):
    today = date(2026, 10, 20)
    with session_scope() as s:
        _seed(s)
        s.flush()
        rows = s.scalars(select(DailySummary).order_by(DailySummary.calendar_date)).all()
        for i, row in enumerate(rows[-21:]):  # the trend window
            row.active_kcal = 1100 if i in (2, 9, 16) else 800
    with session_scope() as s:
        snap = build_snapshot(s, NY, NOW)
        assert snap.chosen.typical_step_kcal == 800
        assert snap.chosen.step_kcal == pytest.approx(900 / 21)
        before = targets.day_targets(s, NY, NOW, today, snap=snap)
        assert before.step_kcal_today is None and before.step_credit_kcal == 0
        assert before.tdee_kcal == pytest.approx(snap.chosen.kcal - 900 / 21)
        assert before.typical_kcal == pytest.approx(before.values["kcal"] + 0.5 * 900 / 21)
    with session_scope() as s:
        # Garmin's running total for today, synced mid-afternoon: 200 above a usual day.
        s.add(
            DailySummary(
                calendar_date=today, active_kcal=1000, resting_kcal=1200, is_provisional=True
            )
        )
    with session_scope() as s:
        after = targets.day_targets(s, NY, NOW, today)
        assert after.step_kcal_today == 1000
        assert after.step_credit_kcal == 100  # 50 % of 200
        assert after.values["kcal"] == before.values["kcal"] + 100
        assert targets.frozen_targets(s, NY, today, today) == {today: after.values["kcal"]}
    assert step_summary(after).startswith("+100 kcal for extra steps: 50% of 200 kcal above")
    summary = plan_summary(after, "lb")
    assert "without extra steps" in summary and "Total" in summary


def test_days_recorded_before_workouts_get_no_adjustment(db):
    today = date(2026, 10, 20)
    with session_scope() as s:
        _seed(s)
        _workout(s, NOW - timedelta(hours=2), 600, 60)
        s.add(DailySummary(calendar_date=today, active_kcal=2000, is_provisional=True))
        values = dict(kcal=2800, protein_g=150, carbs_g=300, fat_g=80, fiber_g=40)
        s.add(
            DailyTarget(
                food_day=today,
                **values,
                sugar_g=70,
                sodium_mg=2300,
                tdee_method="garmin",
                tdee_kcal=3100,
                details={"flags": []},
            )
        )
    with session_scope() as s:
        plan = targets.day_targets(s, NY, NOW, today)
        assert plan.values["kcal"] == 2800 and plan.workout_kcal == 0
        assert targets.frozen_targets(s, NY, today, today) == {today: 2800}


def test_workouts_entered_in_the_app_raise_their_days_targets(db):
    today = date(2026, 10, 20)
    with session_scope() as s:
        _seed(s)
        s.add(DailySummary(calendar_date=today, active_kcal=1000, is_provisional=True))
    with session_scope() as s:
        before = targets.day_targets(s, NY, NOW, today)
        add_manual(s, today, "  Sand volleyball ", 400)
        add_manual(s, today - timedelta(days=1), "", 200)
    with session_scope() as s:
        after = targets.day_targets(s, NY, NOW, today)
        (w,) = after.workouts
        assert (w.name, w.net_kcal, w.start_at) == ("Sand volleyball", 400, None)
        assert w.as_dict()["entered_in_app"]
        assert after.workout_kcal == 300  # 75 % of the guess, used as-is
        assert after.values["kcal"] == before.values["kcal"] + 300
        # Garmin's active kcal doesn't include it, so step credit is unchanged.
        assert after.step_credit_kcal == before.step_credit_kcal
        assert net_by_calendar_date(s, NY, today, today) == {}
        assert targets.frozen_targets(s, NY, today, today) == {today: after.values["kcal"]}
        assert manual_by_food_day(s, today - timedelta(days=1), today) == {
            today - timedelta(days=1): 200,
            today: 400,
        }
        (yesterday,) = load_days(s, NY, today - timedelta(days=1), today - timedelta(days=1))
        assert yesterday.manual_exercise == 200 and yesterday.garmin_exercise == 0
    assert workout_summary(after).startswith("+300 kcal for Sand volleyball: 75% of 400 kcal")
    with session_scope() as s:
        delete_manual(s, w.manual_id)
    with session_scope() as s:
        assert targets.day_targets(s, NY, NOW, today).workout_kcal == 0


@pytest.mark.parametrize("kcal", [None, "", 0, -50, 3001])
def test_manual_activity_needs_a_sensible_guess(db, kcal):
    with session_scope() as s, pytest.raises(ValueError):
        add_manual(s, D0, "Hike", kcal)
