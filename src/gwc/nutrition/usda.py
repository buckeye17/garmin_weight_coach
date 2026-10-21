"""USDA FoodData Central client.

Search results carry nutrients per 100 g (Branded foods too). Survey (FNDDS)
results include portion sizes and Branded results a label serving; SR Legacy
and Foundation portions need one detail request, made only when a food is
picked. See https://fdc.nal.usda.gov/api-guide
"""

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.nal.usda.gov/fdc/v1"
GENERIC_TYPES = ["Foundation", "SR Legacy", "Survey (FNDDS)"]
BRANDED_TYPES = ["Branded"]
DETAIL_TYPES = {"Foundation", "SR Legacy"}

# FDC nutrient numbers. Energy falls back to Atwater factors for some Foundation foods.
ENERGY_NUMBERS = ("208", "958", "957")
NUMBER_TO_KEY = {
    "203": "protein_g",
    "205": "carbs_g",
    "204": "fat_g",
    "291": "fiber_g",
    "307": "sodium_mg",
}
SUGAR_PREFIX = "269"  # "269" Total Sugars, "269.3" Sugars, Total (Foundation)
_HAS_METRIC = re.compile(r"\d\s*(g|ml)\b", re.IGNORECASE)  # "3/4 cup (20g)"


class UsdaError(Exception):
    """A user-presentable failure talking to FoodData Central."""


@dataclass
class Serving:
    label: str
    grams: float


@dataclass
class UsdaHit:
    fdc_id: int
    name: str
    data_type: str
    brand: str | None
    per100: dict[str, float | None]
    servings: list[Serving] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fdc_id": self.fdc_id,
            "name": self.name,
            "data_type": self.data_type,
            "brand": self.brand,
            "per100": self.per100,
            "servings": [{"label": s.label, "grams": s.grams} for s in self.servings],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "UsdaHit":
        return cls(
            fdc_id=d["fdc_id"],
            name=d["name"],
            data_type=d["data_type"],
            brand=d.get("brand"),
            per100=d["per100"],
            servings=[Serving(s["label"], s["grams"]) for s in d.get("servings", [])],
        )


def _nutrients(rows: list[dict[str, Any]], *, nested: bool) -> dict[str, float | None]:
    """Map FDC nutrient rows to our keys. `nested` is the detail-endpoint shape.

    The first row for a nutrient wins: Branded foods can list a second panel
    (e.g. prepared, or a toddler serving) after the label's as-sold values.
    """
    values: dict[str, float] = {}
    energy: dict[str, float] = {}
    for row in rows or []:
        if nested:
            meta = row.get("nutrient") or {}
            number, unit, value = meta.get("number"), meta.get("unitName"), row.get("amount")
        else:
            number, unit, value = row.get("nutrientNumber"), row.get("unitName"), row.get("value")
        if number is None or value is None:
            continue
        number = str(number)
        if number in ENERGY_NUMBERS and str(unit).lower() == "kcal":
            energy.setdefault(number, float(value))
        elif number in NUMBER_TO_KEY:
            values.setdefault(NUMBER_TO_KEY[number], float(value))
        elif number.startswith(SUGAR_PREFIX) and "sugar_g" not in values:
            values["sugar_g"] = float(value)
    kcal = next((energy[n] for n in ENERGY_NUMBERS if n in energy), None)
    out: dict[str, float | None] = {"kcal": kcal}
    for key in ("protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg"):
        out[key] = values.get(key)
    return out


def _clean_name(description: str, data_type: str) -> str:
    # Branded descriptions are often ALL CAPS.
    if data_type == "Branded" and description.isupper():
        return description.title()
    return description


def _branded_serving(food: dict[str, Any]) -> list[Serving]:
    size, unit = food.get("servingSize"), (food.get("servingSizeUnit") or "").lower()
    if not size or unit not in {"g", "grm", "ml", "mlt"}:
        return []
    household = (food.get("householdServingFullText") or "").strip()
    unit_label = "ml" if unit.startswith("m") else "g"
    if household and _HAS_METRIC.search(household):
        label = household
    elif household:
        label = f"{household} ({size:g} {unit_label})"
    else:
        label = f"1 serving ({size:g} {unit_label})"
    # Liquids are treated as 1 g/ml.
    return [Serving(label, float(size))]


def _survey_servings(food: dict[str, Any]) -> list[Serving]:
    out = []
    for m in sorted(food.get("foodMeasures") or [], key=lambda m: m.get("rank") or 99):
        text, grams = m.get("disseminationText"), m.get("gramWeight")
        if text and grams and text.lower() != "quantity not specified":
            out.append(Serving(text, float(grams)))
    return out


def detail_servings(detail: dict[str, Any]) -> list[Serving]:
    out = []
    for p in detail.get("foodPortions") or []:
        grams = p.get("gramWeight")
        if not grams:
            continue
        label = (p.get("portionDescription") or "").strip()
        if not label:
            amount = p.get("amount") or 1
            unit = ((p.get("measureUnit") or {}).get("name") or "").strip()
            modifier = (p.get("modifier") or "").strip()
            parts = [f"{amount:g}"]
            if unit and unit.lower() != "undetermined":
                parts.append(unit)
            if modifier:
                parts.append(modifier)
            label = " ".join(parts)
        out.append(Serving(label, float(grams)))
    return out


def parse_search_hit(food: dict[str, Any]) -> UsdaHit:
    data_type = food.get("dataType") or ""
    servings = (
        _branded_serving(food)
        if data_type == "Branded"
        else _survey_servings(food)
        if data_type.startswith("Survey")
        else []
    )
    brand = food.get("brandName") or food.get("brandOwner")
    return UsdaHit(
        fdc_id=int(food["fdcId"]),
        name=_clean_name(food.get("description") or f"Food {food['fdcId']}", data_type),
        data_type=data_type,
        brand=brand.title() if brand and brand.isupper() else brand,
        per100=_nutrients(food.get("foodNutrients") or [], nested=False),
        servings=servings,
    )


class UsdaClient:
    def __init__(
        self,
        api_key: str | None,
        http: httpx.Client | None = None,
        cache_size: int = 64,
    ) -> None:
        self.api_key = api_key
        self.http = http or httpx.Client(base_url=BASE_URL, timeout=15)
        self._cache: OrderedDict[tuple, list[UsdaHit]] = OrderedDict()
        self._cache_size = cache_size

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if not self.api_key:
            raise UsdaError("Add a USDA_API_KEY to .env to search FoodData Central.")
        try:
            resp = self.http.request(method, path, params={"api_key": self.api_key}, json=body)
        except httpx.HTTPError as e:
            raise UsdaError(f"Couldn't reach FoodData Central ({type(e).__name__}).") from e
        if resp.status_code == 429:
            raise UsdaError("FoodData Central rate limit reached; try again in a few minutes.")
        if resp.status_code in (401, 403):
            raise UsdaError("FoodData Central rejected the API key in .env.")
        if resp.status_code >= 400:
            raise UsdaError(f"FoodData Central returned an error ({resp.status_code}).")
        return resp.json()

    def search(self, query: str, *, branded: bool = False, page_size: int = 25) -> list[UsdaHit]:
        query = " ".join(query.split())
        if not query:
            return []
        key = (query.lower(), branded, page_size)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        # POST: FDC's front end rejects GET searches whose dataType contains "(FNDDS)".
        data = self._request(
            "POST",
            "/foods/search",
            {
                "query": query,
                "dataType": BRANDED_TYPES if branded else GENERIC_TYPES,
                "pageSize": page_size,
            },
        )
        hits = [parse_search_hit(f) for f in data.get("foods") or [] if f.get("fdcId")]
        self._cache[key] = hits
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return hits

    def find_gtin(self, gtin: str, codes: list[str]) -> UsdaHit | None:
        """The newest Branded food whose UPC is `gtin` (GTIN-14), searching each written form.

        FDC stores a UPC as 12, 13 or 14 digits and only matches the exact string.
        """
        data = self._request(
            "POST",
            "/foods/search",
            {"query": " OR ".join(codes), "dataType": BRANDED_TYPES, "pageSize": 10},
        )
        matches = [
            f
            for f in data.get("foods") or []
            if f.get("fdcId")
            and "".join(c for c in str(f.get("gtinUpc") or "") if c.isdigit()).zfill(14) == gtin
        ]
        if not matches:
            return None
        newest = max(matches, key=lambda f: f.get("publishedDate") or "")
        return parse_search_hit(newest)

    def servings_for(self, hit: UsdaHit) -> list[Serving]:
        """Hit servings, fetching portions from the detail endpoint when search has none."""
        if hit.servings or hit.data_type not in DETAIL_TYPES:
            return hit.servings
        try:
            detail = self._request("GET", f"/food/{hit.fdc_id}")
        except UsdaError:
            log.warning("Couldn't fetch portions for FDC %s", hit.fdc_id)
            return []
        return detail_servings(detail)
