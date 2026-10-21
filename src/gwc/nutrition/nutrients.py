"""The seven tracked nutrients and how they're displayed and judged."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class TargetKind(StrEnum):
    TARGET = "target"  # aim for it (calories, carbs, fat)
    MINIMUM = "minimum"  # at least (protein, fiber)
    LIMIT = "limit"  # at most (sugar, sodium)


@dataclass(frozen=True)
class Nutrient:
    key: str
    label: str
    unit: str
    kind: TargetKind
    color: str
    decimals: int = 0


NUTRIENTS: list[Nutrient] = [
    Nutrient("kcal", "Calories", "kcal", TargetKind.TARGET, "brand"),
    Nutrient("protein_g", "Protein", "g", TargetKind.MINIMUM, "blue"),
    Nutrient("carbs_g", "Carbs", "g", TargetKind.TARGET, "yellow"),
    Nutrient("fat_g", "Fat", "g", TargetKind.TARGET, "grape"),
    Nutrient("fiber_g", "Fiber", "g", TargetKind.MINIMUM, "lime"),
    Nutrient("sugar_g", "Sugar", "g", TargetKind.LIMIT, "pink"),
    Nutrient("sodium_mg", "Sodium", "mg", TargetKind.LIMIT, "orange"),
]
NUTRIENT_KEYS = [n.key for n in NUTRIENTS]
BY_KEY = {n.key: n for n in NUTRIENTS}


def per100_of(obj: Any) -> dict[str, float | None]:
    """Per-100 g nutrient values from a Food (or anything with the same attributes)."""
    return {k: getattr(obj, k) for k in NUTRIENT_KEYS}


def scale(per100: dict[str, float | None], grams: float) -> dict[str, float]:
    """Nutrient totals for `grams` of a food; unknown values count as 0."""
    factor = grams / 100
    return {k: (per100.get(k) or 0) * factor for k in NUTRIENT_KEYS}


def add(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    return {k: (a.get(k) or 0) + (b.get(k) or 0) for k in NUTRIENT_KEYS}


def zero() -> dict[str, float]:
    return dict.fromkeys(NUTRIENT_KEYS, 0.0)


def fmt_amount(key: str, value: float | None) -> str:
    if value is None:
        return "—"
    n = BY_KEY[key]
    text = f"{value:,.{n.decimals}f}"
    return f"{text} {n.unit}"
