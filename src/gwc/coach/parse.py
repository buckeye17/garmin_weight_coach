"""Natural-language food logging: Claude itemizes a description, the user reviews, then we log.

Claude grounds each item in a real record (the user's foods or a USDA search
result it actually saw) and returns grams; nutrients are computed here from
that record, not taken from the model. Chain-restaurant items are researched
on the web: Claude returns the chain's published numbers with the page they
came from (official site first, then a third-party site). Items with no
published numbers come back as Claude's own estimate, editable in the review
table. Nothing is saved until the user confirms; confirmed restaurant items
(published, or typed in by the user) become the user's own foods, so the next
order is found without the web.

A photo of the plate can come with the description (or instead of it), for
places with no published nutrition. Claude itemizes what it sees; with no
record behind them those rows are estimates, and they aren't saved as foods.
"""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gwc.coach import prompts
from gwc.coach.jobs import Job
from gwc.coach.label import image_from_data_url
from gwc.coach.llm import LLMRequest, get_backend
from gwc.coach.settings import feature_config
from gwc.coach.tools import FoodLookup
from gwc.db.models import Food, FoodLog, FoodSource, LogSource
from gwc.nutrition import foods
from gwc.nutrition.log import log_food
from gwc.nutrition.nutrients import NUTRIENT_KEYS, per100_of, scale
from gwc.nutrition.service import get_usda
from gwc.nutrition.usda import UsdaHit
from gwc.prefs import get_pref

_NUTRIENTS = {k: {"type": "number"} for k in NUTRIENT_KEYS}
_NUTRIENT_OBJECT = {
    "type": "object",
    "properties": _NUTRIENTS,
    "required": NUTRIENT_KEYS,
    "additionalProperties": False,
}
SOURCE_KINDS = ["official", "third_party", "not_found"]

RESTAURANT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "chain": {"type": "string", "description": "Restaurant chain, e.g. 'Chipotle'"},
        "item": {
            "type": "string",
            "description": "Menu item as listed, plus any changes, e.g. 'Chicken Burrito "
            "Bowl, no cheese'",
        },
        "source_kind": {"type": "string", "enum": SOURCE_KINDS},
        "source_url": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "The page the numbers came from; null when not_found",
        },
        "serving_label": {"type": "string", "description": "e.g. '1 bowl', '1 sandwich'"},
        "serving_grams": {
            "type": "number",
            "description": "Weight of one serving: as published, or your estimate",
        },
        "nutrients": {
            "anyOf": [_NUTRIENT_OBJECT, {"type": "null"}],
            "description": "Published nutrients for one serving as ordered (changes applied); "
            "null when not_found",
        },
    },
    "required": [
        "chain",
        "item",
        "source_kind",
        "source_url",
        "serving_label",
        "serving_grams",
        "nutrients",
    ],
    "additionalProperties": False,
}

ITEMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "The item as the user described it (or as seen in "
                        "the photo), e.g. '2 scrambled eggs'",
                    },
                    "source": {
                        "type": "string",
                        "enum": ["my_food", "usda", "restaurant", "estimate"],
                    },
                    "food_id": {
                        "anyOf": [{"type": "integer"}, {"type": "null"}],
                        "description": "From search_my_foods, when source is my_food",
                    },
                    "fdc_id": {
                        "anyOf": [{"type": "integer"}, {"type": "null"}],
                        "description": "From search_usda, when source is usda",
                    },
                    "amount_label": {
                        "type": "string",
                        "description": "Human amount, e.g. '2 large', '1 slice', '1.5 cups'",
                    },
                    "grams": {"type": "number", "description": "Total grams eaten"},
                    "eaten_at": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "Local time YYYY-MM-DDTHH:MM, or null if not mentioned",
                    },
                    "estimate": {
                        "anyOf": [_NUTRIENT_OBJECT, {"type": "null"}],
                        "description": "Total nutrients as eaten; only when source is estimate",
                    },
                    "restaurant": {
                        "anyOf": [RESTAURANT_SCHEMA, {"type": "null"}],
                        "description": "For chain-restaurant items (source restaurant, or "
                        "estimate when the numbers weren't found); otherwise null",
                    },
                },
                "required": [
                    "description",
                    "source",
                    "food_id",
                    "fdc_id",
                    "amount_label",
                    "grams",
                    "eaten_at",
                    "estimate",
                    "restaurant",
                ],
                "additionalProperties": False,
            },
        },
        "message": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["items", "message"],
    "additionalProperties": False,
}


def _parse_time(value: str | None, tz: ZoneInfo, now: datetime) -> datetime:
    if not value:
        return now
    try:
        local = datetime.fromisoformat(value.strip().replace(" ", "T")[:16])
    except ValueError:
        return now
    if local.tzinfo is None:
        local = local.replace(tzinfo=tz)
    return min(local, now)  # never in the future


def _restaurant(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw or not (raw.get("chain") or "").strip():
        return None
    kind = raw.get("source_kind")
    return {
        "chain": raw["chain"].strip(),
        "item": (raw.get("item") or "").strip(),
        "source_kind": kind if kind in SOURCE_KINDS else "not_found",
        "source_url": raw.get("source_url") or None,
        "serving_label": (raw.get("serving_label") or "1 serving").strip(),
        "serving_grams": float(raw.get("serving_grams") or 0) or None,
    }


def resolve_items(
    s: Session, raw: list[dict[str, Any]], lookup: FoodLookup, tz: ZoneInfo, now: datetime
) -> list[dict[str, Any]]:
    """Turn Claude's items into review rows with per-100 g nutrients from real records.

    Rows without a record (estimates, restaurant items Claude couldn't find) are
    `editable`: the review shows their nutrients as inputs, prefilled when Claude
    estimated them.
    """
    out = []
    for i, item in enumerate(raw):
        grams = max(float(item.get("grams") or 0), 0.0)
        rest = _restaurant(item.get("restaurant"))
        row: dict[str, Any] = {
            "key": i,
            "description": item.get("description") or "",
            "amount_label": item.get("amount_label") or f"{grams:.0f} g",
            "grams": round(grams, 1),
            "eaten_at": _parse_time(item.get("eaten_at"), tz, now).isoformat(),
            "source": item.get("source"),
            "food_id": None,
            "hit": None,
            "name": item.get("description") or "Food",
            "per100": None,
            "error": None,
            "restaurant": rest,
            "editable": False,
        }
        source = item.get("source")
        published = (item.get("restaurant") or {}).get("nutrients")
        if source == "my_food" and item.get("food_id"):
            food = foods.load_food(s, int(item["food_id"]))
            if food is None:
                row["error"] = "Claude picked a food that doesn't exist."
            else:
                row.update(food_id=food.id, name=food.name, per100=per100_of(food))
        elif source == "usda" and item.get("fdc_id"):
            hit = lookup.usda_hits.get(int(item["fdc_id"]))
            if hit is None:
                row["error"] = "Claude picked a USDA food it didn't look up."
            else:
                row.update(hit=hit.to_dict(), name=hit.name, per100=hit.per100)
        elif source == "restaurant" and rest and published and rest["serving_grams"]:
            serving = rest["serving_grams"]
            if grams <= 0:
                row["grams"] = round(serving, 1)
            row.update(
                name=rest["item"] or row["name"],
                per100={k: (published.get(k) or 0) * 100 / serving for k in NUTRIENT_KEYS},
            )
        else:
            # Claude's estimate, or nothing: the user fills in or corrects the numbers.
            est = item.get("estimate")
            if grams <= 0 and rest and rest["serving_grams"]:
                row["grams"] = grams = round(rest["serving_grams"], 1)
            row.update(source="estimate", editable=True)
            if rest and rest["item"]:
                row["name"] = rest["item"]
            if est and grams > 0:
                row["per100"] = {k: (est.get(k) or 0) * 100 / grams for k in NUTRIENT_KEYS}
        out.append(row)
    return out


def item_nutrients(row: dict[str, Any], grams: float | None = None) -> dict[str, float]:
    per100 = row.get("per100") or {}
    return scale(per100, row["grams"] if grams is None else grams)


def parse_description(
    text: str, tz: ZoneInfo, now: datetime, job: Job | None = None, photo: str | None = None
) -> dict:
    """Run Claude over `text` and an optional plate `photo` (a data: URL).

    Returns {"items": [...review rows], "message": str | None, "model": str}.
    """
    from gwc.db.session import session_scope

    lookup = FoodLookup()
    local = now.astimezone(tz)
    images = [image_from_data_url(photo)] if photo else []
    what = text.strip() or "(nothing typed: itemize the photo)"
    prompt = (
        f"Current local time: {local:%A %Y-%m-%d %H:%M}. Country for restaurant menus: "
        f"{get_pref('food.country')}.\n\n"
        + ("A photo of my plate is attached.\n\n" if images else "")
        + f"What I ate:\n{what}"
    )
    req = LLMRequest(
        system=prompts.FOOD_PARSER,
        messages=[{"role": "user", "content": prompt}],
        config=feature_config("parse"),
        tools=lookup.specs(),
        output_schema=ITEMS_SCHEMA,
        on_activity=job.on_activity if job else None,
        web=True,  # chain-restaurant items are looked up on the chain's site
        images=images,
    )
    result = get_backend().run(req)
    data = result.data or {}
    with session_scope() as s:
        items = resolve_items(s, data.get("items") or [], lookup, tz, now)
    for row in items:
        row["photo"] = bool(images)
    return {"items": items, "message": data.get("message"), "model": result.model}


def save_restaurant_food(
    s: Session, row: dict[str, Any], totals: dict[str, float], grams: float
) -> Food:
    """Save (or refresh) a restaurant item as the user's own food, keyed by name and chain."""
    rest = row["restaurant"]
    name = (rest["item"] or row.get("name") or "Food").strip()[:255]
    existing = s.scalars(
        select(Food).where(
            Food.source == FoodSource.CUSTOM,
            Food.is_archived.is_(False),
            func.lower(Food.name) == name.lower(),
            func.lower(Food.brand) == rest["chain"].lower(),
        )
    ).first()
    serving = (rest["serving_label"], rest["serving_grams"] or grams)
    if row.get("source") != "restaurant":  # typed in: the amount entered is the serving
        serving = (row.get("amount_label") or rest["serving_label"], grams)
    food = foods.save_custom_food(
        s,
        food_id=existing.id if existing else None,
        name=name,
        brand=rest["chain"],
        per100={k: totals.get(k, 0) * 100 / grams for k in NUTRIENT_KEYS},
        servings=[serving],
    )
    food.source_url = rest.get("source_url") if row.get("source") == "restaurant" else None
    s.flush()
    return food


def saves_food(row: dict[str, Any]) -> bool:
    """Published restaurant numbers, or ones the user typed in, but not Claude's guesses."""
    if not row.get("restaurant"):
        return False
    return row.get("source") == "restaurant" or bool(row.get("edited"))


def confirm_items(s: Session, rows: list[dict[str, Any]], tz: ZoneInfo) -> int:
    """Log the reviewed rows; returns how many were logged.

    Editable rows may carry `totals` (nutrients as entered) and `edited` (they differ
    from Claude's prefill); restaurant rows are saved as the user's foods first.
    """
    logged = 0
    for row in rows:
        grams = float(row.get("grams") or 0)
        if row.get("error") or grams <= 0:
            continue
        if row.get("editable") and not row.get("totals") and not row.get("per100"):
            continue  # nothing entered for it
        totals = row.get("totals") or item_nutrients(row, grams)
        eaten_at = datetime.fromisoformat(row["eaten_at"])
        label = row.get("amount_label") or f"{grams:.0f} g"
        note = None
        if row.get("description"):
            note = f"From{' photo' if row.get('photo') else ''}: {row['description']}"
        food_id = row.get("food_id")
        if row.get("hit"):
            hit = UsdaHit.from_dict(row["hit"])
            food_id = foods.cache_usda_food(s, hit, get_usda().servings_for(hit)).id
        elif saves_food(row):
            food_id = save_restaurant_food(s, row, totals, grams).id
        if food_id:
            entry = log_food(
                s,
                food_id=food_id,
                amount=grams,
                unit="g",
                eaten_at=eaten_at,
                tz=str(tz),
                source=LogSource.AI,
                note=note,
            )
            entry.unit_label = f"{label} ({grams:.0f} g)"
        else:
            entry = FoodLog(
                eaten_at=eaten_at,
                tz=str(tz),
                description=(row.get("name") or "Food")[:255],
                grams=grams,
                amount=grams,
                unit_label=f"{label} ({grams:.0f} g)",
                source=LogSource.AI,
                note="Estimated by Claude" + (f" · {note}" if note else ""),
                **{k: round(v, 3) for k, v in totals.items()},
            )
            s.add(entry)
        logged += 1
    s.flush()
    return logged
