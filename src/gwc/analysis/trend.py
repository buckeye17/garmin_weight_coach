"""Smoothed weight trend that keeps up with a steady loss or gain.

Weigh-ins are grouped by local date and averaged. The weight trend tracks a
level and a daily slope (Brown's double exponential smoothing): each day it
moves along the slope, and each weigh-in corrects both. A plain moving average
trails a steady change by about 1/alpha weigh-ins, which holds the trend above
every weigh-in during a diet; this one settles on the weigh-ins instead. The
price is a brief overshoot when the rate changes, and a few weeks of weigh-ins
before the slope is learned.

Days without a weigh-in follow the slope for up to COAST_DAYS and then hold
flat, so a long break can't carry the trend far from the last reading.

Body fat % moves too slowly for the lag to matter and single scale readings are
noisy, so it keeps the plain exponentially weighted moving average.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from gwc.db.models import BodyComp

COAST_DAYS = 7


@dataclass(frozen=True)
class Weighin:
    day: date
    weight_kg: float
    body_fat_pct: float | None


@dataclass(frozen=True)
class TrendPoint:
    day: date
    weight_kg: float | None  # that day's mean weigh-in, if any
    trend_kg: float
    body_fat_pct: float | None
    fat_trend_pct: float | None

    @property
    def fat_kg(self) -> float | None:
        if self.fat_trend_pct is None:
            return None
        return self.trend_kg * self.fat_trend_pct / 100


def daily_weighins(s: Session, tz: ZoneInfo) -> list[Weighin]:
    rows = s.execute(
        select(BodyComp.measured_at, BodyComp.weight_kg, BodyComp.body_fat_pct).order_by(
            BodyComp.measured_at
        )
    ).all()
    weights: dict[date, list[float]] = {}
    fats: dict[date, list[float]] = {}
    for at, kg, fat in rows:
        d = at.astimezone(tz).date()
        weights.setdefault(d, []).append(kg)
        if fat:
            fats.setdefault(d, []).append(fat)
    return [
        Weighin(d, sum(ws) / len(ws), (sum(fats[d]) / len(fats[d])) if d in fats else None)
        for d, ws in weights.items()
    ]


def ewma(values: dict[date, float], alpha: float, end: date) -> dict[date, float]:
    """Moving-average trend for every day from the first value through `end`."""
    if not values:
        return {}
    day = min(values)
    trend = values[day]
    seen = 0
    out: dict[date, float] = {}
    while day <= end:
        if day in values:
            # Warm-up: a running mean for the first 1/alpha values, so a high or low
            # first reading doesn't hold the trend off for weeks.
            seen += 1
            trend += max(alpha, 1 / seen) * (values[day] - trend)
        out[day] = trend
        day += timedelta(days=1)
    return out


def linear_trend(values: dict[date, float], alpha: float, end: date) -> dict[date, float]:
    """Level-and-slope trend for every day from the first value through `end`.

    Smoothing the `ewma` a second time and extrapolating from the pair cancels its
    lag on a steady change; this is that calculation written as one update per day.
    """
    if not values:
        return {}
    level_gain = alpha * (2 - alpha)
    slope_gain = alpha * alpha
    day = min(values)
    level = values[day]
    slope = 0.0  # kg per day
    seen = 0
    gap = 0  # days since the last value
    out: dict[date, float] = {}
    while day <= end:
        if gap < COAST_DAYS:
            level += slope
        if day in values:
            miss = values[day] - level
            # Warm-up: a running mean for the first few values, as in `ewma`.
            seen += 1
            level += max(level_gain, 1 / seen) * miss
            if seen > 1:
                slope += slope_gain * miss
            gap = 0
        else:
            gap += 1
        out[day] = level
        day += timedelta(days=1)
    return out


def weight_trend(weighins: list[Weighin], alpha: float, end: date) -> list[TrendPoint]:
    by_day = {w.day: w for w in weighins}
    weights = linear_trend({w.day: w.weight_kg for w in weighins}, alpha, end)
    fats = ewma({w.day: w.body_fat_pct for w in weighins if w.body_fat_pct is not None}, alpha, end)
    return [
        TrendPoint(
            day=d,
            weight_kg=by_day[d].weight_kg if d in by_day else None,
            trend_kg=t,
            body_fat_pct=by_day[d].body_fat_pct if d in by_day else None,
            fat_trend_pct=fats.get(d),
        )
        for d, t in weights.items()
    ]
