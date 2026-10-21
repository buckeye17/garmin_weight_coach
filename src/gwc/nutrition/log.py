"""Logging food and reading a food day's entries and totals."""

import re
from datetime import date, datetime
from fractions import Fraction

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from gwc.analysis.food_day import FoodDays
from gwc.db.models import Food, FoodDayStatus, FoodLog, LogSource
from gwc.nutrition.foods import load_food, resolve_unit
from gwc.nutrition.nutrients import NUTRIENT_KEYS, add, per100_of, scale, zero

SOURCE_FOR = {"usda": LogSource.SEARCH, "custom": LogSource.CUSTOM, "recipe": LogSource.RECIPE}


def _apply(entry: FoodLog, totals: dict[str, float]) -> None:
    for key in NUTRIENT_KEYS:
        setattr(entry, key, round(totals[key], 3))


_COUNT = r"\d+ \d+/\d+|\d+/\d+|\d*\.?\d+"  # '1 1/2', '1/8', '0.75'
_UNICODE_FRACTIONS = {"¼": "1/4", "½": "1/2", "¾": "3/4", "⅓": "1/3", "⅔": "2/3", "⅛": "1/8"}


def parse_count(text: str) -> Fraction:
    """'1/8', '1 1/2', '1.5', '.75', '½', '2x' or '50%' as an exact fraction."""
    s = text.strip().lower().replace("×", "x").replace(",", ".")
    for char, frac in _UNICODE_FRACTIONS.items():
        s = re.sub(rf"(?<=\d)\s*{char}", f" {frac}", s).replace(char, frac)
    s = re.sub(r"^x\s*|\s*x$", "", s)
    percent = s.endswith("%")
    s = re.sub(r"\s+", " ", s.removesuffix("%").strip())
    s = re.sub(r"\s*/\s*", "/", s)
    if not re.fullmatch(_COUNT, s):
        raise ValueError(f"Can't read {text.strip()!r} as a number; try 1/8, 1/3 or 1.5")
    whole, _, frac = s.rpartition(" ")
    try:
        value = Fraction(whole or 0) + Fraction(frac)
    except ZeroDivisionError:
        raise ValueError("Can't divide by zero") from None
    return value / 100 if percent else value


def fmt_count(amount: float) -> str:
    """Counts as simple fractions where they are one: '1/8', '2/3', '1 1/2', else '0.37'."""
    frac = Fraction(amount).limit_denominator(64)
    if abs(frac - Fraction(amount)) > 1e-4:
        return f"{round(amount, 2):g}"
    whole, rest = divmod(frac, 1)
    if not rest:
        return str(whole)
    return f"{whole} {rest}" if whole else str(rest)


def unit_label(amount: float, label: str) -> str:
    """'150 g', '2.5 oz', '1 large', '2 × 1 large', '1/8 × 1 pizza'."""
    if label in ("g", "oz"):
        return f"{amount:g} {label}"
    return label if amount == 1 else f"{fmt_count(amount)} × {label}"


def log_food(
    s: Session,
    *,
    food_id: int,
    amount: float,
    unit: str,
    eaten_at: datetime,
    tz: str,
    source: LogSource | None = None,
    note: str | None = None,
) -> FoodLog:
    if amount <= 0:
        raise ValueError("Amount must be positive")
    food = load_food(s, food_id)
    if food is None:
        raise ValueError("Food not found")
    option = resolve_unit(food, unit)
    grams = amount * option.grams
    entry = FoodLog(
        eaten_at=eaten_at,
        tz=tz,
        food_id=food.id,
        description=food.name,
        grams=grams,
        amount=amount,
        unit_label=unit_label(amount, option.label),
        source=source or SOURCE_FOR[food.source.value],
        note=note or None,
    )
    _apply(entry, scale(per100_of(food), grams))
    s.add(entry)
    s.flush()
    return entry


def update_entry(
    s: Session,
    entry_id: int,
    *,
    amount: float,
    unit: str,
    eaten_at: datetime,
    food_day_override: date | None,
    note: str | None,
) -> FoodLog:
    entry = s.get(FoodLog, entry_id)
    if entry is None:
        raise ValueError("Entry not found")
    if amount <= 0:
        raise ValueError("Amount must be positive")
    food = load_food(s, entry.food_id) if entry.food_id else None
    if food is not None:
        option = resolve_unit(food, unit)
        grams = amount * option.grams
        _apply(entry, scale(per100_of(food), grams))
        entry.unit_label = unit_label(amount, option.label)
    else:
        # Food was deleted: rescale the snapshot.
        grams = amount if unit == "g" else amount * 28.349523125
        factor = grams / entry.grams if entry.grams else 0
        _apply(entry, {k: getattr(entry, k) * factor for k in NUTRIENT_KEYS})
        entry.unit_label = unit_label(amount, unit)
    entry.grams = grams
    entry.amount = amount
    entry.eaten_at = eaten_at
    entry.food_day_override = food_day_override
    entry.note = note or None
    return entry


def _scaled_label(label: str, factor: Fraction, grams: float) -> str:
    """Rescale a unit label: '150 g' -> '75 g', '1 burrito (350 g)' -> '1/2 × 1 burrito (175 g)'."""
    core, grams_suffix = re.subn(r" \([\d,.]+ g\)$", "", label)
    if m := re.fullmatch(r"([\d.]+) (g|oz)", core):
        core = unit_label(round(float(m[1]) * factor, 2), m[2])
    else:
        count = Fraction(1)
        if m := re.fullmatch(rf"({_COUNT}) × (.+)", core):
            count, core = parse_count(m[1]), m[2]
        core = unit_label(float(count * factor), core)
    return f"{core} ({grams:.0f} g)" if grams_suffix else core


def scale_entry(s: Session, entry_id: int, factor: Fraction | float) -> FoodLog:
    """Multiply an entry's portion (ate half, had two) without touching its other fields."""
    entry = s.get(FoodLog, entry_id)
    if entry is None:
        raise ValueError("Entry not found")
    if factor <= 0:
        raise ValueError("Multiplier must be positive")
    exact, factor = Fraction(factor), float(factor)
    _apply(entry, {k: getattr(entry, k) * factor for k in NUTRIENT_KEYS})
    entry.grams *= factor
    if entry.amount:
        entry.amount *= factor
    if entry.unit_label:
        entry.unit_label = _scaled_label(entry.unit_label, exact, entry.grams)
    return entry


def delete_entry(s: Session, entry_id: int) -> None:
    entry = s.get(FoodLog, entry_id)
    if entry is not None:
        s.delete(entry)


def entries_for_day(s: Session, day: date, days: FoodDays) -> list[FoodLog]:
    start, end = days.bounds(day)
    q = (
        select(FoodLog)
        .where(
            or_(
                and_(
                    FoodLog.food_day_override.is_(None),
                    FoodLog.eaten_at >= start,
                    FoodLog.eaten_at < end,
                ),
                FoodLog.food_day_override == day,
            )
        )
        .order_by(FoodLog.eaten_at)
    )
    return list(s.scalars(q))


def entries_by_day(s: Session, start: date, end: date, days: FoodDays) -> dict[date, list[FoodLog]]:
    """Entries for every food day from `start` through `end`, keyed by food day."""
    lo, hi = days.start_of(start), days.end_of(end)[0]
    q = (
        select(FoodLog)
        .where(
            or_(
                and_(FoodLog.eaten_at >= lo, FoodLog.eaten_at < hi),
                and_(FoodLog.food_day_override >= start, FoodLog.food_day_override <= end),
            )
        )
        .order_by(FoodLog.eaten_at)
    )
    out: dict[date, list[FoodLog]] = {}
    for e in s.scalars(q):
        day = e.food_day_override or days.day_of(e.eaten_at)
        if start <= day <= end:
            out.setdefault(day, []).append(e)
    return out


def incomplete_days(s: Session, start: date, end: date) -> set[date]:
    rows = s.scalars(
        select(FoodDayStatus.food_day).where(
            FoodDayStatus.is_incomplete.is_(True),
            FoodDayStatus.food_day >= start,
            FoodDayStatus.food_day <= end,
        )
    )
    return set(rows)


def totals(entries: list[FoodLog]) -> dict[str, float]:
    out = zero()
    for e in entries:
        out = add(out, {k: getattr(e, k) for k in NUTRIENT_KEYS})
    return out


def entry_unit(s: Session, entry: FoodLog) -> tuple[float, str]:
    """Best guess at the unit an entry was logged in, for prefilling the edit form."""
    food: Food | None = load_food(s, entry.food_id) if entry.food_id else None
    label = entry.unit_label or ""
    if food is not None and entry.amount:
        for sv in food.servings:
            if label == sv.label or label.endswith(f"× {sv.label}"):
                return entry.amount, f"serving:{sv.id}"
        if label.endswith(" oz"):
            return entry.amount, "oz"
    return round(entry.grams, 1), "g"


def is_incomplete(s: Session, day: date) -> bool:
    row = s.get(FoodDayStatus, day)
    return bool(row and row.is_incomplete)


def set_incomplete(s: Session, day: date, incomplete: bool) -> None:
    row = s.get(FoodDayStatus, day)
    if row is None:
        row = FoodDayStatus(food_day=day)
        s.add(row)
    row.is_incomplete = incomplete
