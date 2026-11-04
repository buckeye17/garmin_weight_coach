"""Natural-language food logging: Claude itemizes a description, the user reviews, then we log.

Claude grounds each item in a real record (the user's foods or a USDA search
result it actually saw) and returns grams; nutrients are computed here from
that record, not taken from the model. Chain-restaurant items are researched
on the web: Claude returns the chain's published numbers with the page they
came from (official site first, then a third-party site). Items with no
published numbers come back as Claude's own estimate, editable in the review
table. Nothing is saved until the user confirms.

The user can then reply (`reply_to_review`): Claude sees the current review
list, including their edits, and returns only what changed. It can ask a question
instead of guessing, and rows it doesn't touch stay exactly as they are. The
conversation lives in the review and ends when the rows are logged or discarded;
what it decided is kept in the entry's note (`provenance`).

A photo of the plate can come with the description (or instead of it), for
places with no published nutrition. Claude itemizes what it sees; with no
record behind them those rows are estimates.

Scanned barcodes and nutrition labels become review rows of the same shape
(`library_row`, `match_row`, `label_row`), so every way of bringing food in ends
in one list. A row joins the library only when the user asks (`save`): logged
without it, an item is a snapshot entry with no food behind it.
"""

import json
import re
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse
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
from gwc.nutrition import barcode, chains, foods
from gwc.nutrition.log import log_food, unit_label
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
MAX_THREAD_MESSAGES = 24  # the first message, then the most recent turns

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
        "official_url": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "The chain's own nutrition page or calculator, even if you "
            "couldn't read it; null if you don't know it",
        },
        "official_readable": {
            "type": "boolean",
            "description": "Whether you could read numbers from official_url",
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
        "official_url",
        "official_readable",
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
                        "enum": ["my_food", "usda", "restaurant", "estimate", "user"],
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
                        "description": "Total nutrients as eaten; only when source is "
                        "estimate or user",
                    },
                    "restaurant": {
                        "anyOf": [RESTAURANT_SCHEMA, {"type": "null"}],
                        "description": "For chain-restaurant items (source restaurant, or "
                        "estimate when the numbers weren't found); otherwise null",
                    },
                    "basis": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "The exact item and size as the source lists it, plus "
                        "any calculation, in a short phrase",
                    },
                    "replaces_key": {
                        "anyOf": [{"type": "integer"}, {"type": "null"}],
                        "description": "In a follow-up, the key of the row this replaces; "
                        "null for a new row",
                    },
                    "override": {
                        "anyOf": [
                            {
                                "type": "object",
                                "properties": {
                                    "said_source": {
                                        "anyOf": [{"type": "string"}, {"type": "null"}],
                                        "description": "Where the user got their number",
                                    },
                                    "published_kcal": {
                                        "anyOf": [{"type": "number"}, {"type": "null"}],
                                        "description": "The published calories they're replacing",
                                    },
                                    "published_source": {
                                        "anyOf": [{"type": "string"}, {"type": "null"}],
                                        "description": "Where the published number came from",
                                    },
                                },
                                "required": ["said_source", "published_kcal", "published_source"],
                                "additionalProperties": False,
                            },
                            {"type": "null"},
                        ],
                        "description": "Only when source is user; otherwise null",
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
                    "basis",
                    "replaces_key",
                    "override",
                ],
                "additionalProperties": False,
            },
        },
        "message": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "question": {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "options": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Up to 4 short answers the user can tap",
                        },
                    },
                    "required": ["text", "options"],
                    "additionalProperties": False,
                },
                {"type": "null"},
            ],
            "description": "Ask this instead of guessing a detail that changes the numbers",
        },
        "remove_keys": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "In a follow-up, keys of rows to drop",
        },
        "research_notes": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "Figures you found and their URLs, for your later turns",
        },
    },
    "required": ["items", "message", "question", "remove_keys", "research_notes"],
    "additionalProperties": False,
}


def _parse_time(
    value: str | None, tz: ZoneInfo, now: datetime, default: datetime | None = None
) -> datetime:
    if not value:
        return default or now
    try:
        local = datetime.fromisoformat(value.strip().replace(" ", "T")[:16])
    except ValueError:
        return default or now
    if local.tzinfo is None:
        local = local.replace(tzinfo=tz)
    return min(local, now)  # never in the future


def default_eaten_at(day: date | None, tz: ZoneInfo, now: datetime) -> datetime:
    """When an item with no time was eaten: now, or midday when logging for a past day."""
    if day is None or day >= now.astimezone(tz).date():
        return now
    return datetime(day.year, day.month, day.day, 12, tzinfo=tz)


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
        "official_url": raw.get("official_url") or None,
    }


def resolve_items(
    s: Session,
    raw: list[dict[str, Any]],
    lookup: FoodLookup,
    tz: ZoneInfo,
    now: datetime,
    default_at: datetime | None = None,
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
            "origin": "parse",
            "description": item.get("description") or "",
            "amount_label": item.get("amount_label") or f"{grams:.0f} g",
            "grams": round(grams, 1),
            "eaten_at": _parse_time(item.get("eaten_at"), tz, now, default_at).isoformat(),
            "source": item.get("source"),
            "food_id": None,
            "hit": None,
            "name": item.get("description") or "Food",
            "per100": None,
            "error": None,
            "restaurant": rest,
            "editable": False,
            "basis": (item.get("basis") or "").strip() or None,
            "replaces_key": item.get("replaces_key"),
            "override": item.get("override") if item.get("source") == "user" else None,
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
            row.update(source="user" if source == "user" else "estimate", editable=True)
            if rest and rest["item"]:
                row["name"] = rest["item"]
            if est and grams > 0:
                row["per100"] = {k: (est.get(k) or 0) * 100 / grams for k in NUTRIENT_KEYS}
        out.append(row)
    return out


def item_nutrients(row: dict[str, Any], grams: float | None = None) -> dict[str, float]:
    per100 = row.get("per100") or {}
    return scale(per100, row["grams"] if grams is None else grams)


def _row(name: str, source: str, at: datetime, origin: str, **fields: Any) -> dict[str, Any]:
    """A review row that didn't come from Claude's itemizing (a scan or a label)."""
    return {
        "key": 0,
        "origin": origin,
        "description": "",
        "amount_label": "100 g",
        "grams": 100.0,
        "eaten_at": at.isoformat(),
        "source": source,
        "food_id": None,
        "hit": None,
        "name": name,
        "per100": None,
        "error": None,
        "restaurant": None,
        "editable": False,
        **fields,
    }


def library_row(food: Food, at: datetime) -> dict[str, Any]:
    """A food already in the library (a barcode scanned before), at one serving."""
    unit, amount = foods.default_unit(food)
    option = foods.resolve_unit(food, unit)
    return _row(
        food.name,
        "my_food",
        at,
        "scan",
        food_id=food.id,
        per100=per100_of(food),
        grams=round(amount * option.grams, 1),
        amount_label=unit_label(amount, option.label),
    )


def match_row(match: barcode.BarcodeMatch, at: datetime) -> dict[str, Any]:
    """A product found for a scanned barcode, at one serving (or 100 g)."""
    serving = match.servings[0] if match.servings else None
    return _row(
        match.name,
        "barcode",
        at,
        "scan",
        brand=match.brand,
        match=match.to_dict(),
        per100=match.per100,
        grams=round(serving.grams, 1) if serving else 100.0,
        # '1 1/2 cup (39 g)' -> '1 1/2 cup': the weight is shown (and logged) beside it.
        amount_label=re.sub(r" \([\d,.]+ ?g\)$", "", serving.label) if serving else "100 g",
        description=f"Barcode {barcode.display(match.gtin)}",
    )


def label_row(result: dict[str, Any], gtin: str | None, hint: str | None, at: datetime) -> dict:
    """A Nutrition Facts label Claude read: editable, prefilled with one serving's numbers."""
    grams = result.get("serving_grams")
    per_serving = result.get("per_serving") or {}
    return _row(
        result.get("name") or hint or "Packaged food",
        "label",
        at,
        "label",
        brand=result.get("brand"),
        gtin=gtin,
        editable=True,
        per_serving=per_serving,
        per100=foods.per100_from_serving(per_serving, grams) if grams else None,
        grams=grams or 0.0,
        amount_label=result.get("serving_label") or "1 serving",
        label_message=result.get("message"),
        description="Nutrition label",
    )


def parse_description(
    text: str,
    tz: ZoneInfo,
    now: datetime,
    job: Job | None = None,
    photo: str | None = None,
    day: date | None = None,
) -> dict:
    """Run Claude over `text` and an optional plate `photo` (a data: URL).

    `day` is a past food day being filled in: times without a date fall on it.
    Returns the review rows and what Claude said (see `_turn_result`).
    """
    images = [image_from_data_url(photo)] if photo else []
    what = text.strip() or "(nothing typed: itemize the photo)"
    default_at = default_eaten_at(day, tz, now)
    past = ""
    if default_at != now:
        past = (
            f"I'm filling in {default_at:%A %Y-%m-%d}, a past day: times I give without a "
            "date are on that day.\n\n"
        )
    prompt = (
        _time_line(tz, now)
        + "\n\n"
        + past
        + ("A photo of my plate is attached.\n\n" if images else "")
        + f"What I ate:\n{what}"
    )
    lookup = FoodLookup()
    first = [{"role": "user", "content": prompt}]
    data, model = _run_parser(first, lookup, images, job)
    items = _resolve(data, lookup, tz, now, default_at, images)
    return _turn_result(data, items, model, first, [])


def reply_to_review(
    thread: list[dict[str, str]],
    chat: list[dict[str, str]],
    reply: str,
    rows: list[dict[str, Any]],
    edits: dict[str, dict],
    tz: ZoneInfo,
    now: datetime,
    job: Job | None = None,
    photo: str | None = None,
    day: date | None = None,
) -> dict:
    """The user's reply to the review list: Claude returns only what changes.

    `thread` is the conversation so far, and `rows` and `edits` are the list as the
    user sees it now. The result's `items` carry `replaces_key`, and its `remove_keys`
    name rows to drop; anything else stays untouched (`describe.apply_reply`).
    """
    images = [image_from_data_url(photo)] if photo else []
    lookup = FoodLookup()
    for row in rows:  # rows from earlier turns stay usable without searching USDA again
        if row.get("hit"):
            hit = UsdaHit.from_dict(row["hit"])
            lookup.usda_hits[hit.fdc_id] = hit
    state = json.dumps(rows_for_model(rows, edits), ensure_ascii=False)
    prompt = f"{_time_line(tz, now)}\n\nThe review list now:\n{state}\n\nMy reply:\n{reply.strip()}"
    earlier = _trim_thread(thread)
    data, model = _run_parser([*earlier, {"role": "user", "content": prompt}], lookup, images, job)
    items = _resolve(data, lookup, tz, now, default_eaten_at(day, tz, now), images)
    by_key = {r["key"]: r for r in rows}
    raw = data.get("items") or []
    for item, row in zip(raw, items, strict=False):
        replaced = by_key.get(row.get("replaces_key"))
        if replaced and not item.get("eaten_at"):
            row["eaten_at"] = replaced["eaten_at"]  # changing an item doesn't move it in time
    said = [{"role": "user", "content": reply.strip()}]
    result = _turn_result(data, items, model, [*thread, *said], chat)
    result["chat"] = [*chat, {"role": "user", "text": reply.strip()}, *result["chat"][len(chat) :]]
    known = {r["key"] for r in rows}
    result["remove_keys"] = [k for k in data.get("remove_keys") or [] if k in known]
    return result


def _time_line(tz: ZoneInfo, now: datetime) -> str:
    local = now.astimezone(tz)
    return (
        f"Current local time: {local:%A %Y-%m-%d %H:%M}. Country for restaurant menus: "
        f"{get_pref('food.country')}."
    )


def _trim_thread(thread: list[dict[str, str]]) -> list[dict[str, str]]:
    """The first message and the latest turns, still alternating user and assistant."""
    if len(thread) <= MAX_THREAD_MESSAGES:
        return list(thread)
    tail = thread[-(MAX_THREAD_MESSAGES - 2) :]
    if tail[0]["role"] == "assistant":  # the placeholder below already takes that turn
        tail = tail[1:]
    return [thread[0], {"role": "assistant", "content": "(Earlier turns left out.)"}, *tail]


def _run_parser(
    messages: list[dict[str, str]], lookup: FoodLookup, images: list, job: Job | None
) -> tuple[dict, str | None]:
    req = LLMRequest(
        system=prompts.FOOD_PARSER,
        messages=messages,
        config=feature_config("parse"),
        tools=lookup.specs(),
        output_schema=ITEMS_SCHEMA,
        on_activity=job.on_activity if job else None,
        web=True,  # chain-restaurant items are looked up on the chain's site
        images=images,
    )
    result = get_backend().run(req)
    return result.data or {}, result.model


def _resolve(
    data: dict, lookup: FoodLookup, tz: ZoneInfo, now: datetime, default_at: datetime, images: list
) -> list[dict[str, Any]]:
    from gwc.db.session import session_scope

    raw = data.get("items") or []
    with session_scope() as s:
        items = resolve_items(s, raw, lookup, tz, now, default_at)
        for item in raw:
            rest = item.get("restaurant") or {}
            if rest.get("official_url") and rest.get("chain"):
                chains.remember(
                    s, rest["chain"], rest["official_url"], bool(rest.get("official_readable"))
                )
    for row in items:
        row["photo"] = bool(images)
    return items


def _question(data: dict) -> dict[str, Any] | None:
    question = data.get("question") or {}
    text = (question.get("text") or "").strip()
    if not text:
        return None
    options = [str(o).strip() for o in question.get("options") or [] if str(o).strip()]
    return {"text": text, "options": options[:4]}


def _turn_result(
    data: dict,
    items: list[dict[str, Any]],
    model: str | None,
    thread: list[dict[str, str]],
    chat: list[dict[str, str]],
) -> dict:
    """The review's next state: what Claude said, for the user and for its own later turns."""
    message = data.get("message")
    question = _question(data)
    visible = "\n\n".join(x for x in (message, question and question["text"]) if x)
    remembered = [visible]
    if question and question["options"]:
        remembered.append("Options: " + "; ".join(question["options"]))
    if data.get("research_notes"):
        remembered.append(f"My research notes: {data['research_notes']}")
    said = "\n\n".join(x for x in remembered if x) or "(I updated the list.)"
    return {
        "items": items,
        "message": message,
        "question": question,
        "remove_keys": [],
        "model": model,
        "thread": [*thread, {"role": "assistant", "content": said}],
        "chat": [*chat, *([{"role": "assistant", "text": visible}] if visible else [])],
    }


def rows_for_model(rows: list[dict[str, Any]], edits: dict[str, dict]) -> list[dict[str, Any]]:
    """The review list as the user sees it, with their edits and each row's evidence."""
    out = []
    for row in rows:
        edit = edits.get(str(row["key"]), {})
        rest = row.get("restaurant") or {}
        grams = row.get("grams") or 0
        if row.get("editable"):
            values = edit.get("nuts") or row.get("per_serving")
            count = float(log_count(edit.get("count")))
            if values:
                nutrients = {k: (values.get(k) or 0) * count for k in NUTRIENT_KEYS}
            elif row.get("per100"):
                nutrients = {k: v * count for k, v in item_nutrients(row).items()}
            else:
                nutrients = None
            grams *= count
        else:
            grams = float(edit.get("grams") if edit.get("grams") is not None else grams)
            nutrients = item_nutrients(row, grams) if row.get("per100") else None
        out.append(
            {
                "key": row["key"],
                "name": row.get("name"),
                "as_described": row.get("description"),
                "source": row.get("source"),
                "amount": row.get("amount_label"),
                "grams": round(grams, 1),
                "included": edit.get("include", not row.get("error")),
                "nutrients": nutrients and {k: round(v, 1) for k, v in nutrients.items()},
                "basis": row.get("basis"),
                "chain": rest.get("chain"),
                "source_kind": rest.get("source_kind"),
                "source_url": rest.get("source_url"),
                "official_url": rest.get("official_url"),
                "override": row.get("override"),
                "error": row.get("error"),
            }
        )
    return out


def log_count(value: Any) -> float:
    """A row's portion count as typed; anything unreadable counts as one."""
    from gwc.nutrition.log import parse_count

    try:
        return float(parse_count(str(value or "1")))
    except ValueError:
        return 1.0


def override_row(row: dict[str, Any], grams: float | None = None) -> dict[str, Any]:
    """A row whose numbers the user will type themselves, remembering what they replace."""
    weight = grams if grams else row.get("grams") or 0
    published = item_nutrients(row, weight)["kcal"] if row.get("per100") else None
    rest = row.get("restaurant") or {}
    return {
        **row,
        "grams": weight,
        "source": "user",
        "editable": True,
        "food_id": None,
        "hit": None,
        "match": None,
        "per_serving": None,
        "override": {
            "said_source": None,
            "published_kcal": published,
            "published_source": _host(rest.get("source_url")) or row.get("source"),
        },
    }


def _host(url: str | None) -> str | None:
    return (urlparse(url).netloc.removeprefix("www.") or None) if url else None


def provenance(row: dict[str, Any], totals: dict[str, float] | None = None) -> str | None:
    """Where a row's numbers came from, in words, for its log note or library food."""
    rest = row.get("restaurant") or {}
    basis = row.get("basis")
    parts = []
    if row.get("source") == "user":
        said = (row.get("override") or {}).get("said_source")
        kcal = (totals or {}).get("kcal")
        mine = "Your numbers" + (f" ({kcal:,.0f} kcal)" if kcal else "")
        parts.append(mine + (f" from {said}" if said else ""))
        was = (row.get("override") or {}).get("published_kcal")
        if was:
            where = (row.get("override") or {}).get("published_source")
            parts.append(f"published {was:,.0f} kcal" + (f" ({where})" if where else ""))
    elif row.get("source") == "restaurant" and rest.get("source_url"):
        kind = "Official" if rest.get("source_kind") == "official" else "Third-party"
        parts.append(f"{kind} nutrition: {_host(rest['source_url'])}")
    if basis:
        parts.append(basis)
    return " · ".join(parts) or None


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
        serving = (
            row.get("amount_label") or rest["serving_label"],
            row.get("serving_grams") or grams,
        )
    food = foods.save_custom_food(
        s,
        food_id=existing.id if existing else None,
        name=name,
        brand=rest["chain"],
        per100={k: totals.get(k, 0) * 100 / grams for k in NUTRIENT_KEYS},
        servings=[serving],
    )
    food.source_url = rest.get("source_url") if row.get("source") == "restaurant" else None
    food.source_note = provenance(row, totals)
    s.flush()
    return food


def save_row_food(s: Session, row: dict[str, Any], totals: dict[str, float], grams: float) -> Food:
    """Add a reviewed row to the library: the record it came from, or a food made from its numbers.

    `totals` are the nutrients for `grams`; an editable row's `serving_grams` (one
    portion, before any multiplier) becomes the new food's serving.
    """
    if row.get("food_id"):
        food = s.get(Food, int(row["food_id"]))
        if food is not None:
            food.is_archived = False
            return food
    if row.get("hit"):
        hit = UsdaHit.from_dict(row["hit"])
        return foods.cache_usda_food(s, hit, get_usda().servings_for(hit))
    if row.get("match"):
        return barcode.save_match(s, barcode.BarcodeMatch.from_dict(row["match"]), get_usda())
    if row.get("restaurant"):
        return save_restaurant_food(s, row, totals, grams)
    label = row.get("serving_label") or row.get("amount_label") or "1 serving"
    food = foods.save_custom_food(
        s,
        food_id=None,
        name=(row.get("name") or "Food").strip()[:255],
        brand=row.get("brand"),
        per100={k: totals.get(k, 0) * 100 / grams for k in NUTRIENT_KEYS},
        servings=[(label, row.get("serving_grams") or grams)],
    )
    food.source_note = provenance(row, totals)
    if row.get("gtin"):
        barcode.remember(s, row["gtin"], food.id, "label")
    return food


def _totals(row: dict[str, Any], grams: float) -> dict[str, float]:
    return row.get("totals") or item_nutrients(row, grams)


def save_items(s: Session, rows: list[dict[str, Any]]) -> dict[int, Food]:
    """Add the reviewed rows to the library without logging them; returns {row key: food}."""
    saved = {}
    for row in rows:
        grams = float(row.get("grams") or 0)
        if row.get("error") or grams <= 0:
            continue
        saved[row["key"]] = save_row_food(s, row, _totals(row, grams), grams)
    s.flush()
    return saved


def first_eaten_at(rows: list[dict[str, Any]]) -> datetime | None:
    """When the earliest row was eaten; the review's "Eaten at" starts here."""
    times = [datetime.fromisoformat(r["eaten_at"]) for r in rows if r.get("eaten_at")]
    return min(times, default=None)


def retime(rows: list[dict[str, Any]], first: datetime, at: datetime) -> list[dict[str, Any]]:
    """Move the meal that started at `first` to start at `at`, keeping the gaps between items."""
    shift = at - first
    return [
        {**r, "eaten_at": (datetime.fromisoformat(r["eaten_at"]) + shift).isoformat()} for r in rows
    ]


def confirm_items(s: Session, rows: list[dict[str, Any]], tz: ZoneInfo) -> int:
    """Log the reviewed rows; returns how many were logged.

    Editable rows may carry `totals` (nutrients as entered). A row with `save` is
    added to the library first and logged as that food; any other row without a
    library food is logged as a snapshot.
    """
    logged = 0
    for row in rows:
        grams = float(row.get("grams") or 0)
        if row.get("error") or grams <= 0:
            continue
        if row.get("editable") and not row.get("totals") and not row.get("per100"):
            continue  # nothing entered for it
        totals = _totals(row, grams)
        eaten_at = datetime.fromisoformat(row["eaten_at"])
        label = row.get("amount_label") or f"{grams:.0f} g"
        source = LogSource.SEARCH if row.get("source") == "barcode" else LogSource.AI
        note = None
        if row.get("origin") in ("scan", "label"):
            note = row.get("description") or None  # "Barcode …", "Nutrition label"
        elif row.get("description"):
            note = f"From{' photo' if row.get('photo') else ''}: {row['description']}"
        if where := provenance(row, totals):
            note = f"{note} · {where}" if note else where
        food_id = row.get("food_id")
        if row.get("save"):
            food_id = save_row_food(s, row, totals, grams).id
        if food_id:
            entry = log_food(
                s,
                food_id=food_id,
                amount=grams,
                unit="g",
                eaten_at=eaten_at,
                tz=str(tz),
                source=source,
                note=note,
            )
            entry.unit_label = f"{label} ({grams:.0f} g)"
        else:
            if row.get("source") == "estimate":
                note = "Estimated by Claude" + (f" · {note}" if note else "")
            entry = FoodLog(
                eaten_at=eaten_at,
                tz=str(tz),
                description=(row.get("name") or "Food")[:255],
                grams=grams,
                amount=grams,
                unit_label=f"{label} ({grams:.0f} g)",
                source=source,
                note=note,
                **{k: round(v, 3) for k, v in totals.items()},
            )
            s.add(entry)
        logged += 1
    s.flush()
    return logged
