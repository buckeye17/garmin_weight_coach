"""Trigger load: what the last day or two adds up to, rated against the literature.

Flares are mostly set off by the 24–48 hours before them, so each factor looks
back over a short window from a moment in time (now, or when a flare started):

- Purines, 48 h. Zhang et al. (2012, case-crossover) found about five times the
  risk of a recurrent attack in the highest two-day purine intake group compared
  with the lowest, with purines from meat and seafood mattering much more than
  plant purines. So plant purines count half here. A low-purine diet is about
  400–500 mg a day; cut-offs are rough.
- Alcohol, 24 h. Zhang et al. (2014): risk rose with each drink in the prior day,
  for beer, wine and spirits alike (beer's own purines also count above).
- Sweet drinks, 24 h. Sugar-sweetened drinks and juice raise uric acid; the
  evidence is about long-term risk more than a single day, so this factor never
  rates "high".
- Weight loss. Fast loss and very large deficits (crash diets, fasting) raise
  uric acid for a while; long-term loss lowers it. Uses the trend over 14 days
  and the last 7 days' logged intake against the estimated burn.
- Hard exercise, 48 h. Long or very hard sessions (dehydration, lactate) are a
  commonly reported trigger with weaker evidence; never rates "high".

The overall rating is high when any factor is high or two are elevated. This is
an exposure score from published findings, not a personal prediction: there are
too few flares to fit one.
"""

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from gwc.analysis.trend import daily_weighins, weight_trend
from gwc.db.models import Activity, FoodLog, GoutFlare, ManualActivity
from gwc.gout.profiles import Exposure, ProfileBook
from gwc.prefs import get_pref

PURINE_HOURS = 48
DRINK_HOURS = 24
EXERCISE_HOURS = 48
PLANT_WEIGHT = 0.5
PURINE_ELEVATED, PURINE_HIGH = 1000.0, 2000.0  # weighted mg over 48 h
DRINKS_ELEVATED, DRINKS_HIGH = 1.0, 3.0  # standard drinks over 24 h
SWEET_ELEVATED = 25.0  # g of sugar from sweet drinks over 24 h (a can of soda is ~39 g)
PACE_ELEVATED, PACE_HIGH = 1.25, 2.0  # % of body weight lost per week
DEFICIT_ELEVATED, DEFICIT_HIGH = 0.40, 0.55  # share of burn not eaten, 7-day average
HARD_WORKOUT_KCAL = 800.0
HARD_WORKOUT_S = 2 * 3600
PACE_DAYS = 14
MIN_WEIGHINS = 3
TYPICAL_DAYS = range(4, 34)  # same clock time, 4–33 days before, for "your typical"
TOP_SOURCES = 5


class Level(IntEnum):
    LOW = 0
    ELEVATED = 1
    HIGH = 2


LEVEL_LABELS = {Level.LOW: "Low", Level.ELEVATED: "Elevated", Level.HIGH: "High"}
LEVEL_COLORS = {Level.LOW: "brand", Level.ELEVATED: "orange", Level.HIGH: "red"}


@dataclass(frozen=True)
class Factor:
    key: str  # purines | alcohol | sweet | weight | exercise
    label: str
    window: str
    value: float | None  # in the factor's own unit, for comparing with typical days
    text: str
    level: Level | None  # None: not enough data
    detail: str | None = None


@dataclass(frozen=True)
class Source:
    """One entry's share of the window, for "where it came from"."""

    entry_id: int
    description: str
    eaten_at: datetime
    exposure: Exposure


@dataclass(frozen=True)
class Report:
    at: datetime
    factors: list[Factor]
    sources: list[Source]  # biggest purine and alcohol contributors
    unestimated: int  # entries in the windows with no estimate yet
    logged: bool  # anything logged in the purine window

    @property
    def level(self) -> Level:
        levels = [f.level for f in self.factors if f.level is not None]
        if Level.HIGH in levels or levels.count(Level.ELEVATED) >= 2:
            return Level.HIGH
        return Level.ELEVATED if Level.ELEVATED in levels else Level.LOW

    def factor(self, key: str) -> Factor | None:
        return next((f for f in self.factors if f.key == key), None)


@dataclass
class Window:
    """Entries eaten in a span, with their exposures (None: not estimated yet)."""

    rows: list[tuple[FoodLog, Exposure | None]] = field(default_factory=list)

    def between(self, start: datetime, end: datetime) -> list[tuple[FoodLog, Exposure | None]]:
        return [(e, x) for e, x in self.rows if start <= e.eaten_at < end]


def load_window(s: Session, start: datetime, end: datetime, book: ProfileBook) -> Window:
    entries = s.scalars(
        select(FoodLog)
        .where(FoodLog.eaten_at >= start, FoodLog.eaten_at < end)
        .order_by(FoodLog.eaten_at)
    )
    return Window([(e, book.exposure(e)) for e in entries])


def _sum(rows: list[tuple[FoodLog, Exposure | None]]) -> tuple[Exposure, int]:
    total, missing = Exposure(), 0
    for _e, x in rows:
        if x is None:
            missing += 1
        else:
            total += x
    return total, missing


def weighted_purines(x: Exposure) -> float:
    return x.animal_purine_mg + PLANT_WEIGHT * x.plant_purine_mg


def _rate(value: float, elevated: float, high: float | None = None) -> Level:
    if high is not None and value >= high:
        return Level.HIGH
    return Level.ELEVATED if value >= elevated else Level.LOW


def _missing_note(n: int) -> str | None:
    if not n:
        return None
    return f"{n} entr{'y' if n == 1 else 'ies'} not estimated yet"


def food_factors(window: Window, at: datetime) -> tuple[list[Factor], bool]:
    """Purine, alcohol and sweet-drink factors as of `at`, and whether anything was logged."""
    p_rows = window.between(at - timedelta(hours=PURINE_HOURS), at)
    d_rows = window.between(at - timedelta(hours=DRINK_HOURS), at)
    p, p_missing = _sum(p_rows)
    d, d_missing = _sum(d_rows)
    logged = bool(p_rows)
    if not logged:
        unknown = "Nothing logged"
        return [
            Factor("purines", "Purines", "last 48 h", None, unknown, None),
            Factor("alcohol", "Alcohol", "last 24 h", None, unknown, None),
            Factor("sweet", "Sweet drinks", "last 24 h", None, unknown, None),
        ], False
    purine_text = f"{p.purine_mg:,.0f} mg"
    if p.purine_mg:
        purine_text += f" ({p.animal_purine_mg:,.0f} from meat & seafood)"
    drinks = d.drinks
    sweet_text = f"{d.sweet_sugar_g:,.0f} g sugar" if d.sweet_sugar_g else "None"
    return [
        Factor(
            "purines",
            "Purines",
            "last 48 h",
            p.purine_mg,
            purine_text,
            _rate(weighted_purines(p), PURINE_ELEVATED, PURINE_HIGH),
            _missing_note(p_missing),
        ),
        Factor(
            "alcohol",
            "Alcohol",
            "last 24 h",
            drinks,
            "None" if not drinks else f"{drinks:.1f} drink{'' if round(drinks, 1) == 1 else 's'}",
            _rate(drinks, DRINKS_ELEVATED, DRINKS_HIGH),
            _missing_note(d_missing),
        ),
        Factor(
            "sweet",
            "Sweet drinks",
            "last 24 h",
            d.sweet_sugar_g,
            sweet_text,
            _rate(d.sweet_sugar_g, SWEET_ELEVATED),
            _missing_note(d_missing),
        ),
    ], True


def exercise_factor(s: Session, tz: ZoneInfo, at: datetime) -> Factor:
    start = at - timedelta(hours=EXERCISE_HOURS)
    hard: list[str] = []
    for a in s.scalars(select(Activity).where(Activity.start_at >= start, Activity.start_at < at)):
        if (a.kcal or 0) >= HARD_WORKOUT_KCAL or (a.duration_s or 0) >= HARD_WORKOUT_S:
            hard.append(a.name or a.activity_type.replace("_", " "))
    # Workouts entered in the app have a food day but no time.
    days = {start.astimezone(tz).date(), at.astimezone(tz).date()}
    for m in s.scalars(select(ManualActivity).where(ManualActivity.food_day.in_(days))):
        if m.kcal >= HARD_WORKOUT_KCAL:
            hard.append(m.name)
    text = ", ".join(hard) if hard else "No long or very hard workouts"
    return Factor(
        "exercise",
        "Hard exercise",
        "last 48 h",
        float(len(hard)),
        text,
        Level.ELEVATED if hard else Level.LOW,
    )


def weight_factor(s: Session, tz: ZoneInfo, at: datetime) -> Factor:
    """Weight-loss pace from the trend, and the last week's deficit against the burn."""
    from gwc.analysis.snapshot import build_snapshot

    day = at.astimezone(tz).date()
    alpha = float(get_pref("analysis.trend_alpha", session=s))
    weighins = [w for w in daily_weighins(s, tz) if w.day <= day]
    recent = [w for w in weighins if w.day > day - timedelta(days=PACE_DAYS)]
    parts: list[str] = []
    levels: list[Level] = []
    pace = None
    trend = {p.day: p.trend_kg for p in weight_trend(weighins, alpha, day)}
    then = trend.get(day - timedelta(days=PACE_DAYS))
    if len(recent) >= MIN_WEIGHINS and then and day in trend:
        pace = (then - trend[day]) / then * 100 / (PACE_DAYS / 7)
        parts.append(f"losing {pace:.1f}% a week" if pace > 0 else "not losing")
        levels.append(_rate(pace, PACE_ELEVATED, PACE_HIGH))
    snap = build_snapshot(s, tz, at)
    burn = snap.chosen.kcal
    intakes = snap.intakes7
    if burn and len(intakes) >= 3:
        deficit = 1 - statistics.fmean(intakes) / burn
        if deficit > 0:
            parts.append(f"eating {deficit:.0%} under your burn")
        levels.append(_rate(deficit, DEFICIT_ELEVATED, DEFICIT_HIGH))
    if not levels:
        return Factor("weight", "Weight loss", "last 1–2 weeks", None, "Not enough data", None)
    text = "; ".join(parts) or "Eating about what you burn"
    return Factor(
        "weight", "Weight loss", "last 1–2 weeks", pace, text[0].upper() + text[1:], max(levels)
    )


def _sources(rows: list[tuple[FoodLog, Exposure | None]]) -> list[Source]:
    scored = [
        Source(e.id, e.description, e.eaten_at, x)
        for e, x in rows
        if x is not None and (x.purine_mg >= 1 or x.alcohol_g >= 1 or x.sweet_sugar_g >= 1)
    ]
    scored.sort(key=lambda src: -(weighted_purines(src.exposure) + 100 * src.exposure.drinks))
    return scored[:TOP_SOURCES]


def report(s: Session, tz: ZoneInfo, at: datetime, book: ProfileBook | None = None) -> Report:
    book = book or ProfileBook(s)
    window = load_window(s, at - timedelta(hours=PURINE_HOURS), at, book)
    factors, logged = food_factors(window, at)
    factors += [weight_factor(s, tz, at), exercise_factor(s, tz, at)]
    rows = window.between(at - timedelta(hours=PURINE_HOURS), at)
    return Report(
        at=at,
        factors=factors,
        sources=_sources(rows),
        unestimated=sum(1 for _e, x in rows if x is None),
        logged=logged,
    )


# --------------------------------------------------------------------------- look-back


@dataclass(frozen=True)
class Lookback:
    """The 48 hours before a flare, next to the same hours on ordinary days."""

    flare_id: int
    report: Report
    typical: dict[str, float]  # factor key -> median on comparison days
    samples: int  # comparison days with food logged


def _quiet(at: datetime, flares: list[GoutFlare]) -> bool:
    """Not within a few days before or during any flare, so it's an ordinary day."""
    for f in flares:
        start = f.started_at
        end = (
            datetime.combine(f.ended_on, datetime.min.time(), ZoneInfo(f.tz)) + timedelta(days=1)
            if f.ended_on
            else start + timedelta(days=14)
        )
        if start - timedelta(days=3) <= at <= end:
            return False
    return True


def lookback(s: Session, tz: ZoneInfo, flare: GoutFlare, flares: list[GoutFlare]) -> Lookback:
    book = ProfileBook(s)
    at = flare.started_at
    rep = report(s, tz, at, book)
    span_start = at - timedelta(days=max(TYPICAL_DAYS) + 2)
    window = load_window(s, span_start, at, book)
    values: dict[str, list[float]] = {}
    samples = 0
    for k in TYPICAL_DAYS:
        t = at - timedelta(days=k)
        if not _quiet(t, flares):
            continue
        factors, logged = food_factors(window, t)
        if not logged:
            continue
        samples += 1
        for f in factors:
            if f.value is not None:
                values.setdefault(f.key, []).append(f.value)
    typical = {key: statistics.median(v) for key, v in values.items() if v}
    return Lookback(flare.id, rep, typical, samples)
