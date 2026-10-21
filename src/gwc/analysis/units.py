"""Unit conversion. Everything is stored metric; these convert for entry and display."""

from typing import Literal

from gwc.prefs import get_pref, set_pref

KG_PER_LB = 0.45359237
G_PER_OZ = 28.349523125

WeightUnit = Literal["lb", "kg"]
FoodUnit = Literal["g", "oz"]

WEIGHT_UNIT_PREF = "units.weight"
FOOD_UNIT_PREF = "units.food"


def kg_to(value_kg: float, unit: WeightUnit) -> float:
    return value_kg / KG_PER_LB if unit == "lb" else value_kg


def to_kg(value: float, unit: WeightUnit) -> float:
    return value * KG_PER_LB if unit == "lb" else value


def g_to(value_g: float, unit: FoodUnit) -> float:
    return value_g / G_PER_OZ if unit == "oz" else value_g


def to_g(value: float, unit: FoodUnit) -> float:
    return value * G_PER_OZ if unit == "oz" else value


def fmt_weight(value_kg: float | None, unit: WeightUnit, decimals: int = 1) -> str:
    if value_kg is None:
        return "—"
    return f"{kg_to(value_kg, unit):,.{decimals}f} {unit}"


def fmt_food_amount(grams: float, unit: FoodUnit) -> str:
    if unit == "oz":
        return f"{g_to(grams, 'oz'):.1f} oz"
    return f"{grams:,.0f} g"


def weight_unit() -> WeightUnit:
    return get_pref(WEIGHT_UNIT_PREF, "lb")


def food_unit() -> FoodUnit:
    return get_pref(FOOD_UNIT_PREF, "g")


def set_units(weight: WeightUnit, food: FoodUnit) -> None:
    set_pref(WEIGHT_UNIT_PREF, weight)
    set_pref(FOOD_UNIT_PREF, food)
