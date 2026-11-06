"""Purine, alcohol and sweet-drink values for foods and logged entries.

USDA's data has no purines, so every food gets a `GoutProfile` estimated by
Claude (estimate.py) that the user can override. Values are per 100 g. An entry
uses, in order: its own profile (one-off entries, or an override for just that
entry), its food's profile, or for a recipe without one, its ingredients'
profiles weighted by grams. An entry with none of those is "not estimated yet".
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from gwc.db.models import Food, FoodLog, FoodSource, GoutProfile

# One US standard drink.
GRAMS_PER_DRINK = 14.0
MAX_PURINE_PER_100G = 3000.0  # brewer's yeast is around 2,000
MAX_ALCOHOL_PER_100G = 80.0


@dataclass(frozen=True)
class Per100:
    purine_mg: float
    animal_purine_mg: float
    alcohol_g: float
    sweet_sugar_g: float  # sugar from sweet drinks and juice
    sweet_drink: bool
    source: str  # ai | manual | recipe
    note: str | None = None


@dataclass(frozen=True)
class Exposure:
    """What an entry (or a sum of entries) adds up to."""

    purine_mg: float = 0.0
    animal_purine_mg: float = 0.0
    alcohol_g: float = 0.0
    sweet_sugar_g: float = 0.0

    @property
    def plant_purine_mg(self) -> float:
        return max(0.0, self.purine_mg - self.animal_purine_mg)

    @property
    def drinks(self) -> float:
        return self.alcohol_g / GRAMS_PER_DRINK

    def __add__(self, other: "Exposure") -> "Exposure":
        return Exposure(
            self.purine_mg + other.purine_mg,
            self.animal_purine_mg + other.animal_purine_mg,
            self.alcohol_g + other.alcohol_g,
            self.sweet_sugar_g + other.sweet_sugar_g,
        )


def scaled(p: Per100, grams: float) -> Exposure:
    f = grams / 100
    return Exposure(p.purine_mg * f, p.animal_purine_mg * f, p.alcohol_g * f, p.sweet_sugar_g * f)


def clean_values(
    purine_mg: float | None, animal_purine_mg: float | None, alcohol_g: float | None
) -> tuple[float, float, float]:
    """Clamp to sensible ranges; meat-and-seafood purines can't exceed the total."""
    purine = min(MAX_PURINE_PER_100G, max(0.0, float(purine_mg or 0)))
    animal = min(purine, max(0.0, float(animal_purine_mg or 0)))
    alcohol = min(MAX_ALCOHOL_PER_100G, max(0.0, float(alcohol_g or 0)))
    return purine, animal, alcohol


class ProfileBook:
    """Every profile, loaded once, for resolving many entries in one session."""

    def __init__(self, s: Session) -> None:
        self.s = s
        rows = list(s.scalars(select(GoutProfile)))
        self.by_food = {r.food_id: r for r in rows if r.food_id is not None}
        self.by_log = {r.food_log_id: r for r in rows if r.food_log_id is not None}
        self._foods: dict[int, Per100 | None] = {}

    def for_food(self, food_id: int, _seen: frozenset[int] = frozenset()) -> Per100 | None:
        if food_id in self._foods:
            return self._foods[food_id]
        food = self.s.get(Food, food_id)
        result = None if food is None or food_id in _seen else self._resolve(food, _seen)
        self._foods[food_id] = result
        return result

    def _resolve(self, food: Food, seen: frozenset[int]) -> Per100 | None:
        row = self.by_food.get(food.id)
        if row is not None:
            sweet = (food.sugar_g or 0.0) if row.sweet_drink else 0.0
            return _per100(row, sweet)
        if food.source is not FoodSource.RECIPE or not food.recipe_items:
            return None
        total, weight = Exposure(), 0.0
        for item in food.recipe_items:
            p = self.for_food(item.ingredient_id, seen | {food.id})
            if p is None:
                return None
            total += scaled(p, item.grams)
            weight += item.grams
        final = food.recipe_yield_g or weight
        if final <= 0:
            return None
        f = 100 / final
        return Per100(
            total.purine_mg * f,
            total.animal_purine_mg * f,
            total.alcohol_g * f,
            total.sweet_sugar_g * f,
            sweet_drink=total.sweet_sugar_g > 0,
            source="recipe",
            note="From the recipe's ingredients",
        )

    def for_entry(self, entry: FoodLog) -> Per100 | None:
        row = self.by_log.get(entry.id)
        if row is not None:
            sweet = entry.sugar_g * 100 / entry.grams if row.sweet_drink and entry.grams else 0.0
            return _per100(row, sweet)
        if entry.food_id is None:
            return None
        return self.for_food(entry.food_id)

    def exposure(self, entry: FoodLog) -> Exposure | None:
        p = self.for_entry(entry)
        return None if p is None else scaled(p, entry.grams)


def _per100(row: GoutProfile, sweet_sugar_g: float) -> Per100:
    return Per100(
        row.purine_mg,
        row.animal_purine_mg,
        row.alcohol_g,
        sweet_sugar_g,
        sweet_drink=row.sweet_drink,
        source=row.source,
        note=row.note,
    )


def save_profile(
    s: Session,
    *,
    food_id: int | None = None,
    food_log_id: int | None = None,
    purine_mg: float | None,
    animal_purine_mg: float | None,
    alcohol_g: float | None,
    sweet_drink: bool,
    source: str,
    note: str | None = None,
) -> GoutProfile:
    """Insert or replace the profile of a food or of one entry (exactly one of the two)."""
    if (food_id is None) == (food_log_id is None):
        raise ValueError("Give a food or an entry")
    q = select(GoutProfile).where(
        GoutProfile.food_id == food_id
        if food_id is not None
        else GoutProfile.food_log_id == food_log_id
    )
    row = s.scalars(q).first()
    if row is None:
        row = GoutProfile(food_id=food_id, food_log_id=food_log_id)
        s.add(row)
    row.purine_mg, row.animal_purine_mg, row.alcohol_g = clean_values(
        purine_mg, animal_purine_mg, alcohol_g
    )
    row.sweet_drink = bool(sweet_drink)
    row.source = source
    row.note = (note or "").strip()[:500] or None
    s.flush()
    return row


def save_entry_override(
    s: Session,
    entry_id: int,
    *,
    purine_mg: float | None,
    animal_purine_mg: float | None,
    alcohol_g: float | None,
    sweet_drink: bool,
) -> GoutProfile:
    """The user's numbers for one entry as eaten, saved per 100 g.

    Entries from a library food update the food, so later entries of it agree;
    one-off entries get their own profile. Recipes get a profile of their own,
    which then takes precedence over their ingredients.
    """
    entry = s.get(FoodLog, entry_id)
    if entry is None:
        raise ValueError("Entry not found")
    if entry.grams <= 0:
        raise ValueError("This entry has no weight to work from")
    f = 100 / entry.grams
    values = {
        "purine_mg": (purine_mg or 0) * f,
        "animal_purine_mg": (animal_purine_mg or 0) * f,
        "alcohol_g": (alcohol_g or 0) * f,
        "sweet_drink": sweet_drink,
        "source": "manual",
        "note": "Set by you",
    }
    food = s.get(Food, entry.food_id) if entry.food_id is not None else None
    if food is not None:
        stale = s.scalars(select(GoutProfile).where(GoutProfile.food_log_id == entry.id)).first()
        if stale is not None:
            s.delete(stale)
        return save_profile(s, food_id=food.id, **values)
    return save_profile(s, food_log_id=entry.id, **values)
