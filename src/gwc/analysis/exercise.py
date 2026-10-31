"""Workout calories for the same-day target adjustment.

A workout's net calories are Garmin's activity calories minus the resting burn
you'd have had anyway over its duration. Targets start from a base burn that
leaves workouts out (`TdeeEstimate.base_kcal`), and each food day adds a share
(`EAT_BACK_PREF`) of that day's workouts. So a workout is credited once, on the
food day it happened, not again through the next week's burn average.

Steps outside workouts are credited the same way but more cautiously: a share
(`STEPS_PREF`, default 50 %) of the day's active kcal outside workouts above your
usual amount (tdee.py), rounded down. Garmin's active kcal accrues through the
day, so a day in progress only earns credit once it has passed a whole usual day.

Workouts entered in the app (`ManualActivity`) are the user's guess at calories
above resting, so that guess is their net. They belong to a food day directly and
count toward its target like Garmin's. Garmin's daily totals don't include them,
so they stay out of `net_by_calendar_date` (steps, and the Garmin burn's base).
"""

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from gwc.analysis.food_day import FoodDays
from gwc.db.models import Activity, DailySummary, ManualActivity

EAT_BACK_PREF = "targets.exercise_eat_back_pct"
STEPS_PREF = "targets.steps_eat_back_pct"
SECONDS_PER_DAY = 86_400
MAX_MANUAL_KCAL = 3000


@dataclass(frozen=True)
class Workout:
    name: str
    start_at: datetime | None  # None for workouts entered in the app
    duration_s: float | None
    kcal: float  # Garmin's total for the activity
    net_kcal: float  # above resting burn
    manual_id: int | None = None  # the `ManualActivity` behind one entered in the app

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "minutes": round(self.duration_s / 60) if self.duration_s else None,
            "kcal": round(self.kcal),
            "net_kcal": round(self.net_kcal),
            "entered_in_app": self.manual_id is not None,
        }


def net_kcal(kcal: float | None, duration_s: float | None, resting_per_day: float | None) -> float:
    """Activity calories above the resting burn for its duration."""
    if not kcal:
        return 0.0
    resting = (resting_per_day or 0.0) * (duration_s or 0.0) / SECONDS_PER_DAY
    return max(0.0, kcal - resting)


def _load(s: Session, tz: ZoneInfo, start: datetime, end: datetime) -> list[Workout]:
    """Workouts starting in [start, end), oldest first."""
    rows = list(
        s.scalars(
            select(Activity)
            .where(Activity.start_at >= start, Activity.start_at < end)
            .order_by(Activity.start_at)
        )
    )
    if not rows:
        return []
    dates = {a.start_at.astimezone(tz).date() for a in rows}
    resting = dict(
        s.execute(
            select(DailySummary.calendar_date, DailySummary.resting_kcal).where(
                DailySummary.calendar_date >= min(dates) - timedelta(days=7),
                DailySummary.calendar_date <= max(dates),
                DailySummary.resting_kcal.is_not(None),
                # Garmin accrues resting kcal through the day, so a day still in progress
                # reads low; only finished days give the full-day rate.
                DailySummary.is_provisional.is_(False),
            )
        ).all()
    )
    # Days without a finished summary (like today) use the average of the ones that have it.
    fallback = statistics.fmean(resting.values()) if resting else None
    return [
        Workout(
            name=a.name or a.activity_type.replace("_", " "),
            start_at=a.start_at,
            duration_s=a.duration_s,
            kcal=a.kcal or 0.0,
            net_kcal=net_kcal(
                a.kcal, a.duration_s, resting.get(a.start_at.astimezone(tz).date(), fallback)
            ),
        )
        for a in rows
    ]


def _manual(s: Session, start: date, end: date) -> list[ManualActivity]:
    return list(
        s.scalars(
            select(ManualActivity)
            .where(ManualActivity.food_day >= start, ManualActivity.food_day <= end)
            .order_by(ManualActivity.food_day, ManualActivity.id)
        )
    )


def workouts_by_food_day(
    s: Session, tz: ZoneInfo, start: date, end: date, fd: FoodDays | None = None
) -> dict[date, list[Workout]]:
    """Workouts grouped by the food day they started in, for food days `start`..`end`.

    Garmin's come first, oldest first, then any entered in the app.
    """
    fd = fd or FoodDays.load(s, tz, start, end)
    out: dict[date, list[Workout]] = defaultdict(list)
    for w in _load(s, tz, fd.start_of(start), fd.end_of(end)[0]):
        out[fd.day_of(w.start_at)].append(w)
    for m in _manual(s, start, end):
        out[m.food_day].append(Workout(m.name, None, None, m.kcal, m.kcal, manual_id=m.id))
    return dict(out)


def manual_by_food_day(s: Session, start: date, end: date) -> dict[date, float]:
    """Kcal above resting of workouts entered in the app, per food day."""
    out: dict[date, float] = defaultdict(float)
    for m in _manual(s, start, end):
        out[m.food_day] += m.kcal
    return dict(out)


def _clean_manual(name: str | None, kcal: float | None) -> tuple[str, float]:
    if kcal is None or kcal == "":
        raise ValueError("Enter the extra calories you think it burned.")
    kcal = float(kcal)
    if kcal <= 0:
        raise ValueError("Extra calories must be more than 0.")
    if kcal > MAX_MANUAL_KCAL:
        raise ValueError(f"Enter a guess of {MAX_MANUAL_KCAL:,} kcal or less.")
    return ((name or "").strip() or "Activity")[:255], kcal


def add_manual(s: Session, food_day: date, name: str | None, kcal: float | None) -> ManualActivity:
    name, kcal = _clean_manual(name, kcal)
    activity = ManualActivity(food_day=food_day, name=name, kcal=kcal)
    s.add(activity)
    s.flush()
    return activity


def update_manual(s: Session, activity_id: int, name: str | None, kcal: float | None) -> None:
    activity = s.get(ManualActivity, activity_id)
    if activity is None:
        raise ValueError("That activity no longer exists.")
    activity.name, activity.kcal = _clean_manual(name, kcal)


def delete_manual(s: Session, activity_id: int) -> None:
    activity = s.get(ManualActivity, activity_id)
    if activity is not None:
        s.delete(activity)


def net_by_calendar_date(s: Session, tz: ZoneInfo, start: date, end: date) -> dict[date, float]:
    """Net workout kcal per local calendar date, to pair with Garmin's daily totals."""
    lo = datetime.combine(start, datetime.min.time(), tzinfo=tz)
    hi = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    out: dict[date, float] = defaultdict(float)
    for w in _load(s, tz, lo, hi):
        out[w.start_at.astimezone(tz).date()] += w.net_kcal
    return dict(out)


def step_kcal_by_date(s: Session, tz: ZoneInfo, start: date, end: date) -> dict[date, float]:
    """Garmin's active kcal outside workouts per local calendar date, so far for today."""
    active = dict(
        s.execute(
            select(DailySummary.calendar_date, DailySummary.active_kcal).where(
                DailySummary.calendar_date >= start,
                DailySummary.calendar_date <= end,
                DailySummary.active_kcal.is_not(None),
            )
        ).all()
    )
    if not active:
        return {}
    workouts = net_by_calendar_date(s, tz, start, end)
    return {d: max(0.0, kcal - workouts.get(d, 0.0)) for d, kcal in active.items()}
