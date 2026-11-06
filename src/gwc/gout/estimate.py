"""Claude estimates purines, alcohol and sweet-drink status for foods that lack them.

Runs in the background: every few minutes from the scheduler, and on demand from
the Gout page. It covers the library (recipes through their ingredients) and
anything logged in the last `LOOKBACK_DAYS`, including one-off entries that
aren't in the library. Estimates are marked "ai"; the user's own numbers
("manual") are never overwritten.
"""

import dataclasses
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from gwc.coach.jobs import Job, get_jobs
from gwc.coach.llm import LLMError, LLMRequest, get_backend
from gwc.coach.settings import feature_config
from gwc.db.models import Food, FoodLog, FoodSource
from gwc.db.session import session_scope
from gwc.db.types import utcnow
from gwc.gout.profiles import ProfileBook, save_profile

log = logging.getLogger(__name__)

LOOKBACK_DAYS = 120
BATCH = 20
MAX_PER_RUN = 200
JOB_KEY = "gout-estimate"
# After a failed run, scheduled runs wait this long before trying again.
RETRY_AFTER = timedelta(minutes=30)
INTERVAL_MIN = 10

SYSTEM = """\
You estimate gout-related values for foods in a personal nutrition tracker. For each \
item, give values per 100 g of the food as eaten (cooked, if it's a cooked dish):

- purine_mg: total purines in mg per 100 g, the way published purine tables report \
them (for example Kaneko et al.'s Japanese tables or the Souci-Fachmann-Kraut tables). \
Rough anchors: organ meats about 300+, sardines and anchovies about 300, shrimp \
150-270, chicken breast about 140, beef and pork about 100-130, salmon about 120-170, \
cooked lentils and beans about 50-80, tofu about 30-70, bread about 20-30, most fruit \
and vegetables under 30, milk, cheese and eggs under 10, beer about 5-15.
- animal_purine_mg: the part of purine_mg from meat, poultry, fish, seafood, organ \
meat, and meat broth, gravy or extract. 0 for dairy, eggs and plant foods. For mixed \
dishes, split by ingredient.
- alcohol_g: grams of ethanol per 100 g (regular beer about 3.9, wine about 10.5, \
40% spirits about 33). 0 for anything without alcohol.
- sweet_drink: true only for sugar-sweetened drinks (regular soda, sweet tea, \
lemonade, energy and sports drinks, sweetened coffee drinks) and fruit juice. False \
for diet or zero-sugar drinks, plain milk, and solid foods.
- note: a few words on what the estimate is based on.

Judge what each item is from its name, brand, amount and nutrients. When unsure, give \
your best estimate for a typical version; never skip an item. Answer for every key \
you were given."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "purine_mg": {"type": "number"},
                    "animal_purine_mg": {"type": "number"},
                    "alcohol_g": {"type": "number"},
                    "sweet_drink": {"type": "boolean"},
                    "note": {"type": "string"},
                },
                "required": [
                    "key",
                    "purine_mg",
                    "animal_purine_mg",
                    "alcohol_g",
                    "sweet_drink",
                    "note",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


def _per100_text(obj: Any, grams: float | None = None) -> dict[str, float]:
    keys = ("kcal", "protein_g", "carbs_g", "fat_g", "sugar_g")
    if grams is None:
        return {k: round(getattr(obj, k) or 0, 1) for k in keys}
    f = 100 / grams if grams else 0
    return {k: round((getattr(obj, k) or 0) * f, 1) for k in keys}


def _describe_food(food: Food) -> dict[str, Any]:
    item: dict[str, Any] = {"key": f"food:{food.id}", "name": food.name}
    if food.brand:
        item["brand"] = food.brand
    if food.categories:
        item["categories"] = food.category_names
    if food.servings:
        item["serving"] = f"{food.servings[0].label} = {food.servings[0].grams:g} g"
    item["per_100g"] = _per100_text(food)
    return item


def _describe_entry(entry: FoodLog) -> dict[str, Any]:
    item: dict[str, Any] = {
        "key": f"entry:{entry.id}",
        "name": entry.description,
        "amount": f"{entry.unit_label or ''} ({entry.grams:g} g)".strip(),
    }
    if entry.note:
        item["note"] = entry.note
    item["per_100g"] = _per100_text(entry, entry.grams)
    return item


def pending(s: Session, now: datetime) -> list[dict[str, Any]]:
    """Foods and one-off entries that need an estimate, described for Claude."""
    book = ProfileBook(s)
    since = now - timedelta(days=LOOKBACK_DAYS)
    food_ids = set(s.scalars(select(Food.id).where(Food.is_archived.is_(False))))
    food_ids |= set(
        s.scalars(
            select(FoodLog.food_id)
            .where(FoodLog.eaten_at >= since, FoodLog.food_id.is_not(None))
            .distinct()
        )
    )
    wanted: dict[int, Food] = {}
    queue, seen = sorted(food_ids), set()
    while queue:
        food_id = queue.pop()
        if food_id in seen:
            continue
        seen.add(food_id)
        food = s.get(Food, food_id)
        if food is None or food_id in book.by_food:
            continue
        if food.source is FoodSource.RECIPE:
            queue.extend(item.ingredient_id for item in food.recipe_items)
        else:
            wanted[food_id] = food
    items = [_describe_food(wanted[i]) for i in sorted(wanted)]
    entries = s.scalars(
        select(FoodLog)
        .where(FoodLog.eaten_at >= since, FoodLog.food_id.is_(None), FoodLog.grams > 0)
        .order_by(FoodLog.eaten_at.desc())
    )
    items += [_describe_entry(e) for e in entries if e.id not in book.by_log]
    return items


def pending_count(now: datetime | None = None) -> int:
    with session_scope() as s:
        return len(pending(s, now or utcnow()))


def _ask(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
    config = dataclasses.replace(feature_config("parse"), effort="low")
    req = LLMRequest(
        system=SYSTEM,
        messages=[{"role": "user", "content": json.dumps({"items": batch}, indent=1)}],
        config=config,
        output_schema=SCHEMA,
    )
    result = get_backend().run(req)
    data = result.data if isinstance(result.data, dict) else {}
    return [r for r in data.get("items") or [] if isinstance(r, dict)]


def _save(s: Session, row: dict[str, Any], wanted: set[str]) -> bool:
    key = str(row.get("key") or "")
    if key not in wanted:
        return False
    kind, _, raw_id = key.partition(":")
    target = {"food_id": int(raw_id)} if kind == "food" else {"food_log_id": int(raw_id)}
    if kind == "food" and s.get(Food, int(raw_id)) is None:
        return False
    if kind == "entry" and s.get(FoodLog, int(raw_id)) is None:
        return False
    save_profile(
        s,
        **target,
        purine_mg=row.get("purine_mg"),
        animal_purine_mg=row.get("animal_purine_mg"),
        alcohol_g=row.get("alcohol_g"),
        sweet_drink=bool(row.get("sweet_drink")),
        source="ai",
        note=str(row.get("note") or "") or None,
    )
    return True


def estimate_pending(job: Job | None = None, limit: int = MAX_PER_RUN) -> int:
    """Estimate up to `limit` pending items in batches; returns how many were saved."""
    with session_scope() as s:
        items = pending(s, utcnow())[:limit]
    saved = 0
    for start in range(0, len(items), BATCH):
        batch = items[start : start + BATCH]
        if job is not None:
            job.activity = f"Estimating purines: {saved} of {len(items)} done…"
        rows = _ask(batch)
        wanted = {item["key"] for item in batch}
        with session_scope() as s:
            # Manual profiles saved meanwhile win: skip anything that now has one.
            book = ProfileBook(s)
            taken = {f"food:{i}" for i in book.by_food} | {f"entry:{i}" for i in book.by_log}
            for row in rows:
                if _save(s, row, wanted - taken):
                    saved += 1
                    wanted.discard(str(row["key"]))
    return saved


_last_failure: float | None = None


def start_estimate() -> Job:
    """Start (or join) the background estimate run."""

    def run(job: Job) -> int:
        global _last_failure
        try:
            return estimate_pending(job)
        except LLMError:
            _last_failure = time.monotonic()
            raise

    return get_jobs().start("gout", run, key=JOB_KEY)


def run_if_pending() -> None:
    """Scheduler entry point: estimate in the background when something is waiting."""
    try:
        if _last_failure is not None and time.monotonic() - _last_failure < (
            RETRY_AFTER.total_seconds()
        ):
            return
        if pending_count():
            log.info("Estimating gout values for new foods")
            start_estimate()
    except Exception:
        log.exception("Gout estimate check failed")


def register(scheduler) -> None:
    scheduler.add_job(
        run_if_pending,
        "interval",
        minutes=INTERVAL_MIN,
        id="gout-estimate",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
        next_run_time=utcnow() + timedelta(minutes=1),
    )
