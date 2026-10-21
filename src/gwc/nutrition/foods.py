"""Foods, servings and recipes. Nutrients are stored per 100 g."""

from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from gwc.analysis.units import G_PER_OZ
from gwc.db.models import Food, FoodLog, FoodServing, FoodSource, RecipeItem
from gwc.nutrition.nutrients import NUTRIENT_KEYS, add, per100_of, scale, zero
from gwc.nutrition.usda import Serving, UsdaHit


@dataclass(frozen=True)
class UnitOption:
    value: str  # "g", "oz", or "serving:<id>"
    label: str
    grams: float  # grams per 1 of this unit


def unit_options(food: Food) -> list[UnitOption]:
    options = [UnitOption(f"serving:{s.id}", s.label, s.grams) for s in food.servings]
    options += [UnitOption("g", "g", 1.0), UnitOption("oz", "oz", G_PER_OZ)]
    return options


def default_unit(food: Food) -> tuple[str, float]:
    """Unit and amount to prefill: one serving if the food has servings, else 100 g."""
    if food.servings:
        return f"serving:{food.servings[0].id}", 1.0
    return "g", 100.0


def resolve_unit(food: Food, unit: str) -> UnitOption:
    for option in unit_options(food):
        if option.value == unit:
            return option
    raise ValueError(f"Unknown unit {unit!r} for {food.name}")


def to_grams(food: Food, amount: float, unit: str) -> float:
    return amount * resolve_unit(food, unit).grams


def load_food(s: Session, food_id: int) -> Food | None:
    return s.scalars(
        select(Food)
        .where(Food.id == food_id)
        .options(
            selectinload(Food.servings),
            selectinload(Food.recipe_items).selectinload(RecipeItem.ingredient),
        )
    ).first()


# --------------------------------------------------------------------------- USDA cache

# Words that mark a typical single portion, best first, so it becomes the default serving.
_PREFERRED_PORTIONS = ("nlea serving", "serving", "large", "medium", "slice", "piece", "each")


_VOLUME_WORDS = ("cup", "tbsp", "tablespoon", "tsp", "teaspoon", "fl oz", "ml")


_ODD_SIZES = ("extra", "jumbo", "small", "mini")


def _portion_rank(label: str) -> tuple[int, int]:
    text = label.lower()
    if any(word in text for word in _VOLUME_WORDS):
        return len(_PREFERRED_PORTIONS) + 1, 0  # "1 cup (4.86 large eggs)" isn't a typical portion
    rank = next(
        (i for i, word in enumerate(_PREFERRED_PORTIONS) if word in text), len(_PREFERRED_PORTIONS)
    )
    return rank, int(any(word in text for word in _ODD_SIZES))


def order_servings(servings: list[Serving]) -> list[Serving]:
    return sorted(servings, key=lambda sv: _portion_rank(sv.label))


def cache_usda_food(s: Session, hit: UsdaHit, servings: list[Serving]) -> Food:
    """Insert or refresh a USDA food so it can be logged and reused offline."""
    food = s.scalars(
        select(Food).where(Food.fdc_id == hit.fdc_id).options(selectinload(Food.servings))
    ).first()
    if food is None:
        food = Food(source=FoodSource.USDA, fdc_id=hit.fdc_id, name=hit.name)
        s.add(food)
    food.name = hit.name[:255]
    food.brand = (hit.brand or None) and hit.brand[:255]
    food.data_type = hit.data_type
    food.is_archived = False
    for key in NUTRIENT_KEYS:
        setattr(food, key, hit.per100.get(key))
    if servings:
        food.servings = [
            FoodServing(label=sv.label[:128], grams=sv.grams) for sv in order_servings(servings)
        ]
    s.flush()
    return food


# --------------------------------------------------------------------------- custom foods


def save_custom_food(
    s: Session,
    *,
    food_id: int | None,
    name: str,
    brand: str | None,
    per100: dict[str, float | None],
    servings: list[tuple[str, float]],
) -> Food:
    if food_id is not None:
        food = load_food(s, food_id)
        if food is None or food.source is not FoodSource.CUSTOM:
            raise ValueError("Food not found")
    else:
        food = Food(source=FoodSource.CUSTOM, name=name)
        s.add(food)
    food.name = name.strip()[:255]
    food.brand = (brand or "").strip()[:255] or None
    for key in NUTRIENT_KEYS:
        setattr(food, key, per100.get(key))
    _replace_servings(food, servings)
    s.flush()
    return food


def _replace_servings(food: Food, servings: list[tuple[str, float]]) -> None:
    existing = {sv.label: sv for sv in food.servings}
    kept = []
    for label, grams in servings:
        label = label.strip()[:128]
        if not label or not grams or grams <= 0:
            continue
        sv = existing.get(label) or FoodServing(label=label)
        sv.grams = float(grams)
        kept.append(sv)
    food.servings = kept


def per100_from_serving(values: dict[str, float | None], serving_grams: float) -> dict:
    """Convert nutrition-label values for one serving into per-100 g values."""
    if serving_grams <= 0:
        raise ValueError("Serving size must be positive")
    return {
        k: None if values.get(k) is None else values[k] * 100 / serving_grams for k in NUTRIENT_KEYS
    }


# --------------------------------------------------------------------------- recipes


def recipe_totals(s: Session, items: list[tuple[int, float]]) -> tuple[dict[str, float], float]:
    """Total nutrients and raw weight of a list of (ingredient food id, grams)."""
    totals, weight = zero(), 0.0
    for food_id, grams in items:
        food = s.get(Food, food_id)
        if food is None:
            raise ValueError(f"Ingredient {food_id} not found")
        totals = add(totals, scale(per100_of(food), grams))
        weight += grams
    return totals, weight


def save_recipe(
    s: Session,
    *,
    food_id: int | None,
    name: str,
    items: list[tuple[int, float]],
    yield_g: float | None,
    servings: int | None,
) -> Food:
    """Save a recipe; its per-100 g nutrients are computed from the ingredients.

    `yield_g` is the finished (e.g. cooked) weight; it defaults to the raw total.
    """
    items = [(fid, g) for fid, g in items if g and g > 0]
    if not items:
        raise ValueError("Add at least one ingredient")
    if food_id is not None:
        recipe = load_food(s, food_id)
        if recipe is None or recipe.source is not FoodSource.RECIPE:
            raise ValueError("Recipe not found")
    else:
        recipe = Food(source=FoodSource.RECIPE, name=name)
        s.add(recipe)
    if any(fid == recipe.id for fid, _ in items):
        raise ValueError("A recipe can't contain itself")

    totals, raw_weight = recipe_totals(s, items)
    final_weight = yield_g if yield_g and yield_g > 0 else raw_weight
    recipe.name = name.strip()[:255]
    recipe.recipe_yield_g = final_weight
    for key in NUTRIENT_KEYS:
        setattr(recipe, key, totals[key] * 100 / final_weight)
    recipe.recipe_items = [RecipeItem(ingredient_id=fid, grams=g) for fid, g in items]
    _replace_servings(
        recipe,
        [("1 serving", final_weight / servings)] if servings and servings > 0 else [],
    )
    s.flush()
    return recipe


# --------------------------------------------------------------------------- lists


def set_favorite(s: Session, food_id: int, favorite: bool) -> None:
    food = s.get(Food, food_id)
    if food is not None:
        food.is_favorite = favorite


def archive_food(s: Session, food_id: int) -> None:
    """Hide a food from lists; logged entries keep their nutrient snapshot."""
    food = s.get(Food, food_id)
    if food is not None:
        food.is_archived = True
        food.is_favorite = False


def search_foods(
    s: Session, query: str = "", *, sources: list[FoodSource] | None = None, limit: int = 50
) -> list[Food]:
    last_used = (
        select(FoodLog.food_id, func.max(FoodLog.eaten_at).label("last_used"))
        .group_by(FoodLog.food_id)
        .subquery()
    )
    q = (
        select(Food)
        .outerjoin(last_used, last_used.c.food_id == Food.id)
        .where(Food.is_archived.is_(False))
        .options(selectinload(Food.servings))
        .order_by(Food.is_favorite.desc(), last_used.c.last_used.desc().nulls_last(), Food.name)
        .limit(limit)
    )
    if sources:
        q = q.where(Food.source.in_(sources))
    for word in query.split():
        pattern = f"%{word}%"
        q = q.where(or_(Food.name.ilike(pattern), Food.brand.ilike(pattern)))
    return list(s.scalars(q))


def recent_foods(s: Session, limit: int = 8) -> list[Food]:
    q = (
        select(Food)
        .join(FoodLog, FoodLog.food_id == Food.id)
        .where(Food.is_archived.is_(False))
        .group_by(Food.id)
        .order_by(func.max(FoodLog.eaten_at).desc())
        .limit(limit)
        .options(selectinload(Food.servings))
    )
    return list(s.scalars(q))


def favorite_foods(s: Session) -> list[Food]:
    q = (
        select(Food)
        .where(Food.is_favorite.is_(True), Food.is_archived.is_(False))
        .order_by(Food.name)
        .options(selectinload(Food.servings))
    )
    return list(s.scalars(q))
