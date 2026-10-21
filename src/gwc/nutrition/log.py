"""Logging food and reading a food day's entries and totals."""

from datetime import date, datetime

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


def unit_label(amount: float, label: str) -> str:
    """'150 g', '2.5 oz', '1 large', '2 × 1 large'."""
    if label in ("g", "oz"):
        return f"{amount:g} {label}"
    return label if amount == 1 else f"{amount:g} × {label}"


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
