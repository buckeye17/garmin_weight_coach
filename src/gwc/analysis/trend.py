"""Smoothed weight trend: an exponentially weighted moving average (Hacker's Diet style).

Weigh-ins are grouped by local date and averaged. The trend moves `alpha` of
the way toward each new day's weight (a running mean over the first 1/alpha
weigh-ins) and is carried unchanged through days without a weigh-in. Body
fat % gets the same treatment, since single scale readings are noisy.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from gwc.db.models import BodyComp


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
    """Trend for every day from the first value through `end`."""
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


def weight_trend(weighins: list[Weighin], alpha: float, end: date) -> list[TrendPoint]:
    by_day = {w.day: w for w in weighins}
    weights = ewma({w.day: w.weight_kg for w in weighins}, alpha, end)
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
