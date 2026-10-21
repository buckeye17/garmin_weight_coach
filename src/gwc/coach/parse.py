"""Natural-language food logging: Claude itemizes a description, the user reviews, then we log.

Claude grounds each item in a real record (the user's foods or a USDA search
result it actually saw) and returns grams; nutrients are computed here from
that record, not taken from the model. Only items with no reasonable match
come back as Claude's own estimate, and they're flagged in the review table.
Nothing is saved until the user confirms.
"""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from gwc.coach import prompts
from gwc.coach.jobs import Job
from gwc.coach.llm import LLMRequest, get_backend
from gwc.coach.settings import feature_config
from gwc.coach.tools import FoodLookup
from gwc.db.models import FoodLog, LogSource
from gwc.nutrition import foods
from gwc.nutrition.log import log_food
from gwc.nutrition.nutrients import NUTRIENT_KEYS, per100_of, scale
from gwc.nutrition.service import get_usda
from gwc.nutrition.usda import UsdaHit

_NUTRIENTS = {k: {"type": "number"} for k in NUTRIENT_KEYS}

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
                        "description": "The item as the user described it, e.g. '2 scrambled eggs'",
                    },
                    "source": {"type": "string", "enum": ["my_food", "usda", "estimate"]},
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
                        "anyOf": [
                            {
                                "type": "object",
                                "properties": _NUTRIENTS,
                                "required": NUTRIENT_KEYS,
                                "additionalProperties": False,
                            },
                            {"type": "null"},
                        ],
                        "description": "Total nutrients as eaten; only when source is estimate",
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


def resolve_items(
    s: Session, raw: list[dict[str, Any]], lookup: FoodLookup, tz: ZoneInfo, now: datetime
) -> list[dict[str, Any]]:
    """Turn Claude's items into review rows with per-100 g nutrients from real records."""
    out = []
    for i, item in enumerate(raw):
        grams = max(float(item.get("grams") or 0), 0.0)
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
        }
        source = item.get("source")
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
        elif source == "estimate" and item.get("estimate") and grams > 0:
            est = item["estimate"]
            row.update(
                source="estimate",
                per100={k: (est.get(k) or 0) * 100 / grams for k in NUTRIENT_KEYS},
            )
        else:
            row["error"] = "No matching food."
        out.append(row)
    return out


def item_nutrients(row: dict[str, Any], grams: float | None = None) -> dict[str, float]:
    per100 = row.get("per100") or {}
    return scale(per100, row["grams"] if grams is None else grams)


def parse_description(text: str, tz: ZoneInfo, now: datetime, job: Job | None = None) -> dict:
    """Run Claude over `text`; returns {"items": [...review rows], "message": str | None}."""
    from gwc.db.session import session_scope

    lookup = FoodLookup()
    local = now.astimezone(tz)
    prompt = f"Current local time: {local:%A %Y-%m-%d %H:%M}.\n\nWhat I ate:\n{text.strip()}"
    req = LLMRequest(
        system=prompts.FOOD_PARSER,
        messages=[{"role": "user", "content": prompt}],
        config=feature_config("parse"),
        tools=lookup.specs(),
        output_schema=ITEMS_SCHEMA,
        on_activity=job.on_activity if job else None,
    )
    result = get_backend().run(req)
    data = result.data or {}
    with session_scope() as s:
        items = resolve_items(s, data.get("items") or [], lookup, tz, now)
    return {"items": items, "message": data.get("message"), "model": result.model}


def confirm_items(s: Session, rows: list[dict[str, Any]], tz: ZoneInfo) -> int:
    """Log the reviewed rows; returns how many were logged."""
    logged = 0
    for row in rows:
        grams = float(row.get("grams") or 0)
        if row.get("error") or grams <= 0:
            continue
        eaten_at = datetime.fromisoformat(row["eaten_at"])
        label = row.get("amount_label") or f"{grams:.0f} g"
        note = f"From: {row['description']}" if row.get("description") else None
        food_id = row.get("food_id")
        if row.get("hit"):
            hit = UsdaHit.from_dict(row["hit"])
            food_id = foods.cache_usda_food(s, hit, get_usda().servings_for(hit)).id
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
            totals = item_nutrients(row, grams)
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
