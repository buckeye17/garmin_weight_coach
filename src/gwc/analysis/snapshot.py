"""Everything the analysis pages need, computed once from the database."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from gwc.analysis import energy, tdee
from gwc.analysis.days import DayRecord, load_days
from gwc.analysis.food_day import current_food_day
from gwc.analysis.trend import TrendPoint, Weighin, daily_weighins, weight_trend
from gwc.prefs import get_pref

LAST_DAYS = 7
MIN_LOGGED_FOR_PROJECTION = 3
WARN_BELOW_COMPLETE = 5


@dataclass(frozen=True)
class Snapshot:
    today: date
    records: list[DayRecord]  # oldest first, through today
    weighins: list[Weighin]
    trend: list[TrendPoint]
    current: TrendPoint | None  # trend as of today
    fat_kg: float | None
    body_fat_assumed: bool
    rho: float | None
    garmin: tdee.TdeeEstimate
    adaptive: tdee.TdeeEstimate
    chosen: tdee.TdeeEstimate
    kcal_per_kg_lost: float

    @property
    def weight_kg(self) -> float | None:
        return self.current.trend_kg if self.current else None

    @property
    def lean_kg(self) -> float | None:
        if self.weight_kg is None or self.fat_kg is None:
            return None
        return self.weight_kg - self.fat_kg

    @property
    def ended(self) -> list[DayRecord]:
        return tdee.ended_days(self.records, self.today)

    @property
    def last7(self) -> list[DayRecord]:
        return self.ended[-LAST_DAYS:]

    @property
    def intakes7(self) -> list[float]:
        return [d.kcal for d in self.last7 if d.complete]


def build_snapshot(s: Session, tz: ZoneInfo, now: datetime, history_days: int = 0) -> Snapshot:
    today = current_food_day(s, tz, now)
    window = int(get_pref("analysis.adaptive_window_days", session=s))
    alpha = float(get_pref("analysis.trend_alpha", session=s))
    span = max(window, tdee.GARMIN_DAYS * 2, history_days)
    records = load_days(s, tz, today - timedelta(days=span), today)

    weighins = daily_weighins(s, tz)
    trend = weight_trend(weighins, alpha, today)
    current = trend[-1] if trend else None
    fat_kg = assumed = rho = None
    if current is not None:
        assumed = current.fat_trend_pct is None
        pct = energy.DEFAULT_BODY_FAT_PCT if assumed else current.fat_trend_pct
        fat_kg = current.trend_kg * pct / 100
        rho = energy.rho(fat_kg)

    ended = tdee.ended_days(records, today)
    garmin = tdee.garmin_tdee(ended)
    if rho is None:
        adaptive = tdee.TdeeEstimate("adaptive", None, None, False, "Calibrating: no weigh-ins.")
    else:
        adaptive = tdee.adaptive_tdee(ended, weighins, rho, window)
    method = get_pref("targets.tdee_method", session=s)
    return Snapshot(
        today=today,
        records=records,
        weighins=weighins,
        trend=trend,
        current=current,
        fat_kg=fat_kg,
        body_fat_assumed=bool(assumed),
        rho=rho,
        garmin=garmin,
        adaptive=adaptive,
        chosen=tdee.choose(method, garmin, adaptive),
        kcal_per_kg_lost=float(get_pref("analysis.kcal_per_kg_lost", session=s)),
    )
