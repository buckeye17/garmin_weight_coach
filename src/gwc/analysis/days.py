"""Per-food-day records: logged intake, completeness, and Garmin's totals for that day."""

from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from gwc.analysis.exercise import net_by_calendar_date
from gwc.analysis.food_day import FoodDays
from gwc.db.models import DailySummary
from gwc.nutrition import log


@dataclass(frozen=True)
class DayRecord:
    day: date
    intake: dict[str, float] | None  # None when nothing was logged
    entries: int
    marked_incomplete: bool
    garmin_total: float | None
    garmin_resting: float | None
    garmin_active: float | None
    steps: int | None
    garmin_provisional: bool
    # Net kcal of Garmin workouts on the same calendar date as `garmin_total`.
    garmin_exercise: float = 0.0

    @property
    def complete(self) -> bool:
        """Logged and not flagged as partial: counts toward averages."""
        return self.entries > 0 and not self.marked_incomplete

    @property
    def kcal(self) -> float | None:
        return self.intake["kcal"] if self.intake else None

    @property
    def has_garmin(self) -> bool:
        return self.garmin_total is not None and not self.garmin_provisional


def load_days(s: Session, tz: ZoneInfo, start: date, end: date) -> list[DayRecord]:
    """One record per food day from `start` through `end`, oldest first.

    Garmin's per-calendar-day totals (and the workouts behind them) are mapped to the
    food day of the same date.
    """
    fd = FoodDays.load(s, tz, start, end)
    entries = log.entries_by_day(s, start, end, fd)
    incomplete = log.incomplete_days(s, start, end)
    garmin = {
        row.calendar_date: row
        for row in s.scalars(
            select(DailySummary).where(
                DailySummary.calendar_date >= start, DailySummary.calendar_date <= end
            )
        )
    }
    exercise = net_by_calendar_date(s, tz, start, end)
    out = []
    day = start
    while day <= end:
        es = entries.get(day, [])
        g = garmin.get(day)
        out.append(
            DayRecord(
                day=day,
                intake=log.totals(es) if es else None,
                entries=len(es),
                marked_incomplete=day in incomplete,
                garmin_total=g.total_kcal if g else None,
                garmin_resting=g.resting_kcal if g else None,
                garmin_active=g.active_kcal if g else None,
                steps=g.steps if g else None,
                garmin_provisional=bool(g and g.is_provisional),
                garmin_exercise=exercise.get(day, 0.0),
            )
        )
        day += timedelta(days=1)
    return out
