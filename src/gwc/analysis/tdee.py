"""Daily expenditure (TDEE) estimates.

- Garmin: mean of Garmin's total kcal over the last 7 ended food days it has
  final numbers for.
- Adaptive: what you must have burned given what you ate and how your weight
  moved: mean intake on complete food days − ρ × weight slope. The slope is a
  least-squares fit to the daily weigh-ins in the window rather than the
  change in the smoothed trend, which lags real changes by ~10 days.
  Intake and weigh-ins come from the same days: from the first complete food
  day in the window, and when that's the start of logging after a break, from
  a week later, since the first days of a diet shed water and glycogen that
  would otherwise read as fat loss.

Both include whatever workouts you did. `exercise_kcal` is the average net
workout burn over the same days, and `base_kcal` is the estimate without it:
targets start from the base and add each day's own workouts (exercise.py).

Steps likewise: `typical_step_kcal` is the median of Garmin's active kcal outside
workouts on the same days, and `step_kcal` the average amount above it. The base
leaves that out too, so a day that walks more than usual is credited on its own
day (targets.py) instead of again through the next week's average. Using the
median keeps one big day from raising "usual", which errs toward eating less.
"""

import math
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from gwc.analysis.days import DayRecord
from gwc.analysis.trend import Weighin

GARMIN_DAYS = 7
# Garmin's burn figures are known to run several percent off; this keeps the
# projection band from pretending 7 days of its own numbers pin TDEE down.
GARMIN_SYSTEMATIC_PCT = 0.05
ADAPTIVE_MIN_SPAN_DAYS = 14
ADAPTIVE_MIN_COMPLETE_DAYS = 10
ADAPTIVE_MIN_WEIGHINS = 4
# Logging that resumes after this many days without a complete food day is a new
# start, and the fit skips its first ADAPTIVE_SETTLE_DAYS.
ADAPTIVE_BREAK_DAYS = 7
ADAPTIVE_SETTLE_DAYS = 7
# Days with Garmin active kcal needed before steps are judged against "usual".
MIN_STEP_DAYS = 3


@dataclass(frozen=True)
class TdeeEstimate:
    method: str  # "garmin" | "adaptive"
    kcal: float | None
    se: float | None
    ready: bool
    note: str
    days_used: int = 0
    resting_kcal: float | None = None  # Garmin only: mean resting (BMR) kcal
    intake_kcal: float | None = None  # adaptive only: mean intake on complete days
    slope_kg_per_week: float | None = None  # adaptive only
    exercise_kcal: float | None = None  # mean net workout kcal/day over the same days
    typical_step_kcal: float | None = None  # median active kcal/day outside workouts
    step_kcal: float | None = None  # mean active kcal/day above that

    @property
    def base_kcal(self) -> float | None:
        """Burn without workouts or above-usual steps."""
        if self.kcal is None:
            return None
        return self.kcal - (self.exercise_kcal or 0.0) - (self.step_kcal or 0.0)

    @property
    def label(self) -> str:
        return "Garmin" if self.method == "garmin" else "Trend-based"


def _sd(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) > 1 else 0.0


def step_stats(days: list[DayRecord]) -> dict[str, float | None]:
    """Usual (median) active kcal outside workouts, and the mean amount above it."""
    values = [d.step_kcal for d in days if d.has_garmin and d.step_kcal is not None]
    if len(values) < MIN_STEP_DAYS:
        return {"typical_step_kcal": None, "step_kcal": None}
    typical = statistics.median(values)
    return {
        "typical_step_kcal": typical,
        "step_kcal": statistics.fmean(max(0.0, v - typical) for v in values),
    }


def garmin_tdee(ended: list[DayRecord], n: int = GARMIN_DAYS) -> TdeeEstimate:
    """`ended` is food days before today, oldest first."""
    days = [d for d in ended if d.has_garmin][-n:]
    if not days:
        return TdeeEstimate("garmin", None, None, False, "No Garmin daily totals yet.")
    totals = [d.garmin_total for d in days]
    resting = [d.garmin_resting for d in days if d.garmin_resting]
    mean = statistics.fmean(totals)
    se = math.hypot(_sd(totals) / math.sqrt(len(totals)), GARMIN_SYSTEMATIC_PCT * mean)
    return TdeeEstimate(
        "garmin",
        mean,
        se,
        True,
        f"Mean of Garmin's total burn over {len(days)} day{'s' * (len(days) != 1)}.",
        days_used=len(days),
        resting_kcal=statistics.fmean(resting) if resting else None,
        exercise_kcal=statistics.fmean(d.garmin_exercise for d in days),
        **step_stats(days),
    )


def linear_slope(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Least-squares slope and its standard error."""
    n = len(points)
    mx = statistics.fmean(x for x, _ in points)
    my = statistics.fmean(y for _, y in points)
    sxx = sum((x - mx) ** 2 for x, _ in points)
    slope = sum((x - mx) * (y - my) for x, y in points) / sxx
    if n <= 2:
        return slope, 0.0
    resid = sum((y - (my + slope * (x - mx))) ** 2 for x, y in points)
    return slope, math.sqrt(resid / (n - 2) / sxx)


def adaptive_history_days(window: int) -> int:
    """Ended days `adaptive_tdee` needs to see a logging start that is still settling."""
    return window + ADAPTIVE_SETTLE_DAYS + ADAPTIVE_BREAK_DAYS


def fit_start(ended: list[DayRecord], window: int) -> tuple[date | None, bool]:
    """First day the adaptive fit uses, and whether it skips a new start's settling days.

    That's the first complete food day in the window, or a week after logging resumed
    following a break, whichever is later. History that doesn't reach back far enough
    to see a break counts as logging that's been going all along.
    """
    first = next((d.day for d in ended[-window:] if d.complete), None)
    if first is None:
        return None, False
    known = {d.day for d in ended}
    logged = {d.day for d in ended if d.complete}
    start = first
    while start + timedelta(days=ADAPTIVE_SETTLE_DAYS) > first:
        before = [start - timedelta(days=k) for k in range(1, ADAPTIVE_BREAK_DAYS + 1)]
        if not all(d in known for d in before):
            return first, False
        earlier = [d for d in before if d in logged]
        if not earlier:
            return start + timedelta(days=ADAPTIVE_SETTLE_DAYS), True
        start = max(earlier)
    return first, False


def adaptive_tdee(
    ended: list[DayRecord],
    weighins: list[Weighin],
    rho: float,
    window: int = 21,
) -> TdeeEstimate:
    start, settling = fit_start(ended, window)
    days = [d for d in ended[-window:] if start is not None and d.day >= start]
    if not days:
        note = "settling after you started logging" if settling else "no food days yet"
        return TdeeEstimate("adaptive", None, None, False, f"Calibrating: {note}.")
    first, last = days[0].day, days[-1].day
    complete = [d.kcal for d in days if d.complete]
    points = [((w.day - first).days, w.weight_kg) for w in weighins if first <= w.day <= last]
    span = (points[-1][0] - points[0][0] + 1) if points else 0

    missing = []
    if len(complete) < ADAPTIVE_MIN_COMPLETE_DAYS:
        missing.append(f"{len(complete)} of {ADAPTIVE_MIN_COMPLETE_DAYS} complete food days")
    if span < ADAPTIVE_MIN_SPAN_DAYS or len(points) < ADAPTIVE_MIN_WEIGHINS:
        missing.append(f"weigh-ins spanning {span} of {ADAPTIVE_MIN_SPAN_DAYS} days")
    if missing:
        return TdeeEstimate(
            "adaptive",
            None,
            None,
            False,
            "Calibrating: "
            + ", ".join(missing)
            + (f" since {first:%b} {first.day}" if settling else f" in the last {window}")
            + ".",
            days_used=len(complete),
        )

    intake = statistics.fmean(complete)
    slope, slope_se = linear_slope(points)
    kcal = intake - rho * slope
    se = math.hypot(_sd(complete) / math.sqrt(len(complete)), rho * slope_se)
    return TdeeEstimate(
        "adaptive",
        kcal,
        se,
        True,
        f"From {len(complete)} complete food days and {len(points)} weigh-ins "
        f"in the last {window} days.",
        days_used=len(complete),
        intake_kcal=intake,
        slope_kg_per_week=slope * 7,
        exercise_kcal=statistics.fmean(d.garmin_exercise for d in days),
        **step_stats(days),
    )


def choose(method: str, garmin: TdeeEstimate, adaptive: TdeeEstimate) -> TdeeEstimate:
    """The estimate that sets targets: `auto` prefers adaptive once it's calibrated."""
    if method == "garmin":
        return garmin
    if method == "adaptive":
        return adaptive if adaptive.ready else garmin
    return adaptive if adaptive.ready else garmin


def ended_days(records: list[DayRecord], today: date) -> list[DayRecord]:
    return [r for r in records if r.day < today]
