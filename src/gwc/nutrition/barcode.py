"""Product barcodes: normalizing scans, the saved-barcode cache, and lookups.

A scan is checked against the user's saved barcodes first, then USDA Branded
foods (manufacturer label data), then Open Food Facts (crowd-sourced: wider
but patchier). A barcode is only saved once the user picks the product, so a
wrong match never sticks. Codes are kept as zero-padded GTIN-14.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from gwc import __version__
from gwc.db.models import Food, FoodBarcode
from gwc.nutrition import foods
from gwc.nutrition.nutrients import NUTRIENT_KEYS
from gwc.nutrition.usda import Serving, UsdaClient, UsdaError, UsdaHit

log = logging.getLogger(__name__)

OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{code}"
OFF_PAGE = "https://world.openfoodfacts.org/product/{code}"
OFF_FIELDS = (
    "code,product_name,generic_name,brands,serving_size,serving_quantity,"
    "serving_quantity_unit,nutriments"
)
OFF_HEADERS = {"User-Agent": f"GarminWeightCoach/{__version__} (personal nutrition tracker)"}
KJ_PER_KCAL = 4.184


# --------------------------------------------------------------------------- codes


def _check_digit(body: str) -> int:
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10


def expand_upce(code: str) -> str | None:
    """An 8-digit UPC-E (number system, 6 digits, check) as its 12-digit UPC-A."""
    if len(code) != 8 or code[0] not in "01":
        return None
    d = code[1:7]
    last = d[5]
    if last in "012":
        middle = d[0:2] + last + "0000" + d[2:5]
    elif last == "3":
        middle = d[0:3] + "00000" + d[3:5]
    elif last == "4":
        middle = d[0:4] + "00000" + d[4]
    else:
        middle = d[0:5] + "0000" + last
    return code[0] + middle + code[7]


def normalize(raw: str | None, fmt: str | None = None) -> str | None:
    """A scanned or typed code as GTIN-14, or None if it isn't a valid product code.

    `fmt` is the scanner's format name; "upc_e" marks an 8-digit code as UPC-E
    rather than EAN-8.
    """
    digits = "".join(c for c in raw or "" if c.isdigit())
    if fmt == "upc_e" and len(digits) == 8:
        digits = expand_upce(digits) or ""
    if len(digits) not in (8, 12, 13, 14):
        return None
    gtin = digits.zfill(14)
    if _check_digit(gtin[:-1]) != int(gtin[-1]):
        return None
    return gtin


def written_forms(gtin: str) -> list[str]:
    """The ways databases write this code: GTIN-14, EAN-13, UPC-A, EAN-8."""
    forms = [gtin]
    for width in (13, 12, 8):
        if gtin.startswith("0" * (14 - width)):
            forms.append(gtin[14 - width :])
    return forms


def display(gtin: str) -> str:
    """The shortest standard form, as printed under the bars."""
    return written_forms(gtin)[-1]


def is_store_code(gtin: str) -> bool:
    """In-store labels (deli, meat, bakery): codes with a price or weight inside, no database."""
    return gtin.startswith(("02", "002"))  # EAN-13 20-29, UPC-A number system 2


# --------------------------------------------------------------------------- matches


@dataclass
class BarcodeMatch:
    """A product found for a barcode, before the user confirms it."""

    gtin: str
    source: str  # "usda" | "off"
    name: str
    brand: str | None
    per100: dict[str, float | None]
    servings: list[Serving] = field(default_factory=list)
    url: str | None = None
    usda: UsdaHit | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "gtin": self.gtin,
            "source": self.source,
            "name": self.name,
            "brand": self.brand,
            "per100": self.per100,
            "servings": [{"label": s.label, "grams": s.grams} for s in self.servings],
            "url": self.url,
            "usda": self.usda.to_dict() if self.usda else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BarcodeMatch":
        return cls(
            gtin=d["gtin"],
            source=d["source"],
            name=d["name"],
            brand=d.get("brand"),
            per100=d["per100"],
            servings=[Serving(s["label"], s["grams"]) for s in d.get("servings", [])],
            url=d.get("url"),
            usda=UsdaHit.from_dict(d["usda"]) if d.get("usda") else None,
        )


@dataclass
class LookupResult:
    match: BarcodeMatch | None = None
    # A product name a database knew without usable nutrition, to prefill the label form.
    name_hint: str | None = None
    problems: list[str] = field(default_factory=list)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _off_per100(n: dict[str, Any]) -> dict[str, float | None]:
    kcal = _number(n.get("energy-kcal_100g"))
    if kcal is None:
        kj = _number(n.get("energy-kj_100g"))
        if kj is None and n.get("energy_unit", "kJ") == "kJ":
            kj = _number(n.get("energy_100g"))
        kcal = None if kj is None else kj / KJ_PER_KCAL
    sodium_g = _number(n.get("sodium_100g"))
    if sodium_g is None:
        salt = _number(n.get("salt_100g"))
        sodium_g = None if salt is None else salt / 2.5
    return {
        "kcal": kcal,
        "protein_g": _number(n.get("proteins_100g")),
        "carbs_g": _number(n.get("carbohydrates_100g")),
        "fat_g": _number(n.get("fat_100g")),
        "fiber_g": _number(n.get("fiber_100g")),
        "sugar_g": _number(n.get("sugars_100g")),
        "sodium_mg": None if sodium_g is None else sodium_g * 1000,
    }


_BARE_QUANTITY = re.compile(r"^\s*[\d.,]+\s*(g|ml)\s*$", re.IGNORECASE)
_HAS_GRAMS = re.compile(r"\d\s*(g|ml)\b", re.IGNORECASE)


def _off_servings(p: dict[str, Any]) -> list[Serving]:
    grams = _number(p.get("serving_quantity"))
    if not grams or grams > 2000:
        return []
    unit = "ml" if str(p.get("serving_quantity_unit") or "").lower() == "ml" else "g"
    text = " ".join(str(p.get("serving_size") or "").split())
    if not text or _BARE_QUANTITY.match(text):
        label = f"1 serving ({grams:g} {unit})"
    elif _HAS_GRAMS.search(text):
        label = text
    else:
        label = f"{text} ({grams:g} {unit})"
    # Liquids are treated as 1 g/ml, as for USDA.
    return [Serving(label, grams)]


def parse_off_product(gtin: str, p: dict[str, Any]) -> BarcodeMatch | None:
    """An Open Food Facts product, or None when it has no calories to log."""
    per100 = _off_per100(p.get("nutriments") or {})
    if per100["kcal"] is None:
        return None
    name = (p.get("product_name") or p.get("generic_name") or "").strip()
    brand = (str(p.get("brands") or "").split(",")[0]).strip() or None
    return BarcodeMatch(
        gtin=gtin,
        source="off",
        name=name or f"Product {display(gtin)}",
        brand=brand,
        per100=per100,
        servings=_off_servings(p),
        url=OFF_PAGE.format(code=p.get("code") or display(gtin)),
    )


class OpenFoodFactsClient:
    def __init__(self, http: httpx.Client | None = None) -> None:
        self.http = http or httpx.Client(timeout=15, headers=OFF_HEADERS)

    def product(self, gtin: str) -> dict[str, Any] | None:
        """The raw product for this code, or None if Open Food Facts doesn't have it.

        Raises httpx.HTTPError when the service can't be reached.
        """
        resp = self.http.get(OFF_URL.format(code=display(gtin)), params={"fields": OFF_FIELDS})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        return data.get("product") if data.get("status") == 1 else None


def lookup(gtin: str, usda: UsdaClient, off: OpenFoodFactsClient) -> LookupResult:
    """USDA Branded first (label data), then Open Food Facts."""
    result = LookupResult()
    try:
        hit = usda.find_gtin(gtin, written_forms(gtin))
    except UsdaError as e:
        result.problems.append(f"USDA: {e}")
        hit = None
    if hit is not None:
        if hit.per100.get("kcal") is not None:
            result.match = BarcodeMatch(
                gtin=gtin,
                source="usda",
                name=hit.name,
                brand=hit.brand,
                per100=hit.per100,
                servings=hit.servings,
                usda=hit,
            )
            return result
        result.name_hint = hit.name
    try:
        product = off.product(gtin)
    except httpx.HTTPError as e:
        log.warning("Open Food Facts lookup failed: %s", e)
        result.problems.append(f"Couldn't reach Open Food Facts ({type(e).__name__}).")
        return result
    if product is not None:
        result.match = parse_off_product(gtin, product)
        if result.match is None:
            result.name_hint = result.name_hint or (product.get("product_name") or "").strip()
    return result


# --------------------------------------------------------------------------- saved barcodes


def saved_food(s: Session, gtin: str) -> Food | None:
    """The food this barcode was confirmed as, unless that food has been removed."""
    return s.scalars(
        select(Food)
        .join(FoodBarcode, FoodBarcode.food_id == Food.id)
        .where(FoodBarcode.gtin == gtin, Food.is_archived.is_(False))
    ).first()


def remember(s: Session, gtin: str, food_id: int, source: str) -> None:
    row = s.get(FoodBarcode, gtin)
    if row is None:
        s.add(FoodBarcode(gtin=gtin, food_id=food_id, source=source))
    else:
        row.food_id, row.source = food_id, source
    s.flush()


def save_match(s: Session, match: BarcodeMatch, usda: UsdaClient) -> Food:
    """Store the confirmed product as a food and remember its barcode."""
    if match.usda is not None:
        food = foods.cache_usda_food(s, match.usda, usda.servings_for(match.usda))
    else:
        food = foods.save_custom_food(
            s,
            food_id=None,
            name=match.name,
            brand=match.brand,
            per100=match.per100,
            servings=[(sv.label, sv.grams) for sv in match.servings],
        )
        food.source_url = match.url
    remember(s, match.gtin, food.id, match.source)
    return food


def save_label_food(
    s: Session,
    *,
    gtin: str | None,
    name: str,
    brand: str | None,
    serving_label: str,
    serving_grams: float,
    per_serving: dict[str, float | None],
) -> Food:
    """Save a food read from a nutrition label (values for one serving)."""
    food = foods.save_custom_food(
        s,
        food_id=None,
        name=name,
        brand=brand,
        per100=foods.per100_from_serving(
            {k: per_serving.get(k) for k in NUTRIENT_KEYS}, serving_grams
        ),
        servings=[(serving_label, serving_grams)],
    )
    if gtin:
        remember(s, gtin, food.id, "label")
    return food
