"""Workout calories for the same-day target adjustment.

A workout's net calories are Garmin's activity calories minus the resting burn
you'd have had anyway over its duration. Targets start from a base burn that
leaves workouts out (`TdeeEstimate.base_kcal`), and each food day adds a share
(`EAT_BACK_PREF`) of that day's workouts. So a workout is credited once, on the
food day it happened, not again through the next week's burn average.

Only recorded Garmin activities count; extra walking stays in the base burn.
"""

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from gwc.analysis.food_day import FoodDays
from gwc.db.models import Activity, DailySummary

EAT_BACK_PREF = "targets.exercise_eat_back_pct"
SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class Workout:
    name: str
    start_at: datetime
    duration_s: float | None
    kcal: float  # Garmin's total for the activity
    net_kcal: float  # above resting burn

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "start_at": self.start_at.isoformat(),
            "minutes": round(self.duration_s / 60) if self.duration_s else None,
            "kcal": round(self.kcal),
            "net_kcal": round(self.net_kcal),
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


def workouts_by_food_day(
    s: Session, tz: ZoneInfo, start: date, end: date, fd: FoodDays | None = None
) -> dict[date, list[Workout]]:
    """Workouts grouped by the food day they started in, for food days `start`..`end`."""
    fd = fd or FoodDays.load(s, tz, start, end)
    out: dict[date, list[Workout]] = defaultdict(list)
    for w in _load(s, tz, fd.start_of(start), fd.end_of(end)[0]):
        out[fd.day_of(w.start_at)].append(w)
    return dict(out)


def net_by_calendar_date(s: Session, tz: ZoneInfo, start: date, end: date) -> dict[date, float]:
    """Net workout kcal per local calendar date, to pair with Garmin's daily totals."""
    lo = datetime.combine(start, datetime.min.time(), tzinfo=tz)
    hi = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    out: dict[date, float] = defaultdict(float)
    for w in _load(s, tz, lo, hi):
        out[w.start_at.astimezone(tz).date()] += w.net_kcal
    return dict(out)
