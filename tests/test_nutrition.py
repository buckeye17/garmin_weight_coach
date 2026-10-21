from datetime import date, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select

from fakes import utc
from gwc.analysis.food_day import FoodDays
from gwc.analysis.targets import manual_targets, set_manual_targets
from gwc.analysis.units import G_PER_OZ, fmt_weight, kg_to, to_kg
from gwc.db.migrate import upgrade
from gwc.db.models import Food, FoodLog, FoodSource, LogSource
from gwc.db.session import session_scope
from gwc.nutrition import foods, log
from gwc.nutrition.nutrients import scale
from gwc.nutrition.usda import (
    Serving,
    UsdaClient,
    UsdaError,
    UsdaHit,
    detail_servings,
    parse_search_hit,
)

NY = ZoneInfo("America/New_York")

EGG_SEARCH = {
    "fdcId": 171287,
    "description": "Egg, whole, raw, fresh",
    "dataType": "SR Legacy",
    "foodNutrients": [
        {"nutrientNumber": "268", "unitName": "kJ", "value": 599},
        {"nutrientNumber": "208", "unitName": "KCAL", "value": 143},
        {"nutrientNumber": "203", "unitName": "G", "value": 12.6},
        {"nutrientNumber": "204", "unitName": "G", "value": 9.51},
        {"nutrientNumber": "205", "unitName": "G", "value": 0.72},
        {"nutrientNumber": "291", "unitName": "G", "value": 0},
        {"nutrientNumber": "269", "unitName": "G", "value": 0.37},
        {"nutrientNumber": "307", "unitName": "MG", "value": 142},
    ],
}


def test_parse_generic_hit():
    hit = parse_search_hit(EGG_SEARCH)
    assert hit.per100 == {
        "kcal": 143,
        "protein_g": 12.6,
        "carbs_g": 0.72,
        "fat_g": 9.51,
        "fiber_g": 0,
        "sugar_g": 0.37,
        "sodium_mg": 142,
    }
    assert hit.servings == []


def test_parse_foundation_atwater_energy_and_sugar_variant():
    hit = parse_search_hit(
        {
            "fdcId": 1,
            "description": "Peppers, raw",
            "dataType": "Foundation",
            "foodNutrients": [
                {"nutrientNumber": "957", "unitName": "KCAL", "value": 23.9},
                {"nutrientNumber": "269.3", "unitName": "G", "value": 2.4},
            ],
        }
    )
    assert hit.per100["kcal"] == 23.9
    assert hit.per100["sugar_g"] == 2.4
    assert hit.per100["protein_g"] is None


def test_parse_branded_hit():
    hit = parse_search_hit(
        {
            "fdcId": 2,
            "description": "GREEK YOGURT",
            "dataType": "Branded",
            "brandName": "FAGE",
            "servingSize": 170.0,
            "servingSizeUnit": "g",
            "householdServingFullText": "1 container",
            "foodNutrients": [{"nutrientNumber": "208", "unitName": "KCAL", "value": 59}],
        }
    )
    assert hit.name == "Greek Yogurt"
    assert hit.brand == "Fage"
    assert hit.servings == [Serving("1 container (170 g)", 170.0)]


def test_parse_survey_measures():
    hit = parse_search_hit(
        {
            "fdcId": 3,
            "description": "Banana, raw",
            "dataType": "Survey (FNDDS)",
            "foodNutrients": [],
            "foodMeasures": [
                {"disseminationText": "Quantity not specified", "gramWeight": 126, "rank": 6},
                {"disseminationText": "1 cup", "gramWeight": 150, "rank": 3},
                {"disseminationText": "1 banana", "gramWeight": 126, "rank": 1},
            ],
        }
    )
    assert [s.label for s in hit.servings] == ["1 banana", "1 cup"]


def test_detail_portions():
    servings = detail_servings(
        {
            "foodPortions": [
                {
                    "amount": 1.0,
                    "modifier": "large",
                    "gramWeight": 50,
                    "measureUnit": {"name": "undetermined"},
                },
                {"amount": 1.0, "portionDescription": "1 cup, chopped", "gramWeight": 243},
                {"amount": 2.0, "modifier": "", "gramWeight": 0, "measureUnit": {"name": "tbsp"}},
            ]
        }
    )
    assert servings == [Serving("1 large", 50.0), Serving("1 cup, chopped", 243.0)]


def _client(handler) -> UsdaClient:
    return UsdaClient(
        "k", http=httpx.Client(base_url="https://x", transport=httpx.MockTransport(handler))
    )


def test_search_caches_and_fetches_details_only_when_needed():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/foods/search":
            return httpx.Response(200, json={"foods": [EGG_SEARCH]})
        return httpx.Response(
            200, json={"foodPortions": [{"amount": 1, "modifier": "large", "gramWeight": 50}]}
        )

    client = _client(handler)
    hits = client.search("egg")
    assert client.search(" EGG ") == hits
    assert calls == ["/foods/search"]
    assert client.servings_for(hits[0]) == [Serving("1 large", 50.0)]
    assert calls[-1] == "/food/171287"


@pytest.mark.parametrize(
    ("status", "message"), [(429, "rate limit"), (403, "rejected"), (500, r"error \(500\)")]
)
def test_search_errors(status, message):
    client = _client(lambda r: httpx.Response(status, json={}))
    with pytest.raises(UsdaError, match=message):
        client.search("egg")


def test_missing_key():
    with pytest.raises(UsdaError, match="USDA_API_KEY"):
        UsdaClient(None).search("egg")


def test_units():
    assert kg_to(100, "lb") == pytest.approx(220.462, abs=1e-3)
    assert to_kg(220.462, "lb") == pytest.approx(100, abs=1e-3)
    assert fmt_weight(124.05, "lb") == "273.5 lb"
    assert fmt_weight(124.05, "kg") == "124.0 kg"


# --------------------------------------------------------------------------- with a database


@pytest.fixture
def db(isolated_settings):
    upgrade()


def _egg(s) -> Food:
    hit = parse_search_hit(EGG_SEARCH)
    return foods.cache_usda_food(s, hit, [Serving("1 large", 50.0)])


def test_cache_usda_food_is_idempotent(db):
    with session_scope() as s:
        first = _egg(s).id
    with session_scope() as s:
        second = _egg(s)
        assert second.id == first
        assert [sv.label for sv in second.servings] == ["1 large"]
        assert s.scalar(select(Food.data_type)) == "SR Legacy"


def test_log_food_with_serving_and_oz(db):
    with session_scope() as s:
        egg = _egg(s)
        unit, amount = foods.default_unit(egg)
        assert amount == 1.0
        entry = log.log_food(
            s,
            food_id=egg.id,
            amount=2,
            unit=unit,
            eaten_at=utc(2026, 10, 20, 6, 30),
            tz="America/New_York",
        )
        assert entry.grams == 100
        assert entry.kcal == 143
        assert entry.unit_label == "2 × 1 large"
        single = log.log_food(
            s, food_id=egg.id, amount=1, unit=unit, eaten_at=utc(2026, 10, 20, 6, 0), tz="x"
        )
        assert single.unit_label == "1 large"
        assert log.entry_unit(s, single) == (1, unit)
        assert entry.source is LogSource.SEARCH

        by_oz = log.log_food(
            s,
            food_id=egg.id,
            amount=1,
            unit="oz",
            eaten_at=utc(2026, 10, 20, 6, 30),
            tz="America/New_York",
        )
        assert by_oz.grams == pytest.approx(G_PER_OZ)
        assert by_oz.unit_label == "1 oz"
        assert log.entry_unit(s, entry) == (2, unit)
        assert log.entry_unit(s, by_oz) == (1, "oz")


def test_snapshot_survives_food_changes(db):
    with session_scope() as s:
        egg = _egg(s)
        log.log_food(
            s, food_id=egg.id, amount=100, unit="g", eaten_at=utc(2026, 10, 20, 6, 0), tz="UTC"
        )
        egg.kcal = 999
    with session_scope() as s:
        assert s.scalars(select(FoodLog)).one().kcal == 143


def test_update_and_delete_entry(db):
    with session_scope() as s:
        egg = _egg(s)
        entry = log.log_food(
            s, food_id=egg.id, amount=1, unit="g", eaten_at=utc(2026, 10, 20, 6, 0), tz="UTC"
        )
        entry_id = entry.id
    with session_scope() as s:
        updated = log.update_entry(
            s,
            entry_id,
            amount=150,
            unit="g",
            eaten_at=utc(2026, 10, 20, 7, 0),
            food_day_override=date(2026, 10, 19),
            note="post-run",
        )
        assert updated.kcal == pytest.approx(214.5)
        assert updated.food_day_override == date(2026, 10, 19)
    with session_scope() as s:
        log.delete_entry(s, entry_id)
    with session_scope() as s:
        assert s.scalars(select(FoodLog)).all() == []


def test_scale_entry(db):
    with session_scope() as s:
        egg = _egg(s)
        unit, _ = foods.default_unit(egg)
        two = log.log_food(
            s, food_id=egg.id, amount=2, unit=unit, eaten_at=utc(2026, 10, 20, 6, 0), tz="UTC"
        )
        log.scale_entry(s, two.id, 0.5)
        assert (two.grams, two.kcal, two.unit_label) == (50, 71.5, "1 large")
        assert log.entry_unit(s, two) == (1, unit)
        log.scale_entry(s, two.id, 1.5)
        assert two.unit_label == "1.5 × 1 large"

        by_g = log.log_food(
            s, food_id=egg.id, amount=150, unit="g", eaten_at=utc(2026, 10, 20, 6, 0), tz="UTC"
        )
        log.scale_entry(s, by_g.id, 2)
        assert (by_g.grams, by_g.unit_label) == (300, "300 g")

        meal = FoodLog(
            eaten_at=utc(2026, 10, 20, 12, 0),
            tz="UTC",
            description="Burrito",
            grams=350,
            amount=350,
            unit_label="1 burrito (350 g)",
            source=LogSource.AI,
            kcal=900,
        )
        s.add(meal)
        s.flush()
        log.scale_entry(s, meal.id, 0.5)
        assert (meal.kcal, meal.unit_label) == (450, "0.5 × 1 burrito (175 g)")
        log.scale_entry(s, meal.id, 2)
        assert meal.unit_label == "1 burrito (350 g)"
        with pytest.raises(ValueError):
            log.scale_entry(s, meal.id, 0)


def test_entries_for_day_uses_boundaries_and_overrides(db):
    mon, tue = date(2026, 10, 19), date(2026, 10, 20)
    fd = FoodDays(NY, 9, {tue: utc(2026, 10, 20, 8, 15)})  # asleep 04:15 Tue
    with session_scope() as s:
        egg = _egg(s)
        late_snack = log.log_food(
            s, food_id=egg.id, amount=50, unit="g", eaten_at=utc(2026, 10, 20, 6, 30), tz="x"
        )
        breakfast = log.log_food(
            s, food_id=egg.id, amount=100, unit="g", eaten_at=utc(2026, 10, 20, 17, 0), tz="x"
        )
        moved = log.log_food(
            s, food_id=egg.id, amount=10, unit="g", eaten_at=utc(2026, 10, 20, 18, 0), tz="x"
        )
        moved.food_day_override = mon
        s.flush()
        assert [e.id for e in log.entries_for_day(s, mon, fd)] == [late_snack.id, moved.id]
        tue_entries = log.entries_for_day(s, tue, fd)
        assert [e.id for e in tue_entries] == [breakfast.id]
        assert log.totals(tue_entries)["kcal"] == pytest.approx(143)


def test_custom_food_from_label(db):
    per100 = foods.per100_from_serving({"kcal": 120, "protein_g": 24, "sodium_mg": 150}, 31)
    assert per100["kcal"] == pytest.approx(387.1, abs=0.1)
    with session_scope() as s:
        shake = foods.save_custom_food(
            s,
            food_id=None,
            name="Whey shake",
            brand="Brand",
            per100=per100,
            servings=[("1 scoop", 31)],
        )
        shake_id = shake.id
    with session_scope() as s:
        shake = foods.save_custom_food(
            s,
            food_id=shake_id,
            name="Whey shake",
            brand=None,
            per100=per100,
            servings=[("1 scoop", 31), ("2 scoops", 62), ("", 5)],
        )
        assert [sv.label for sv in shake.servings] == ["1 scoop", "2 scoops"]
        assert shake.brand is None


def test_recipe_nutrients_per_100g(db):
    with session_scope() as s:
        egg = _egg(s)
        oats = foods.save_custom_food(
            s,
            food_id=None,
            name="Oats",
            brand=None,
            per100={"kcal": 380, "protein_g": 13},
            servings=[],
        )
        recipe = foods.save_recipe(
            s,
            food_id=None,
            name="Egg oats",
            items=[(egg.id, 100), (oats.id, 50)],
            yield_g=300,  # cooked with water
            servings=2,
        )
        # 143 + 190 kcal across 300 g finished weight
        assert recipe.kcal == pytest.approx(333 / 3)
        assert recipe.source is FoodSource.RECIPE
        assert recipe.servings[0].grams == 150
        assert scale({"kcal": recipe.kcal}, 150)["kcal"] == pytest.approx(166.5)
        with pytest.raises(ValueError):
            foods.save_recipe(
                s, food_id=recipe.id, name="x", items=[(recipe.id, 10)], yield_g=None, servings=None
            )


def test_search_orders_favorites_then_recent(db):
    with session_scope() as s:
        a = foods.save_custom_food(
            s, food_id=None, name="Apple", brand=None, per100={}, servings=[]
        )
        b = foods.save_custom_food(
            s, food_id=None, name="Bagel", brand=None, per100={}, servings=[]
        )
        c = foods.save_custom_food(
            s, food_id=None, name="Cheese", brand="Tillamook", per100={}, servings=[]
        )
        log.log_food(s, food_id=a.id, amount=1, unit="g", eaten_at=utc(2026, 10, 19), tz="x")
        log.log_food(s, food_id=b.id, amount=1, unit="g", eaten_at=utc(2026, 10, 20), tz="x")
        foods.set_favorite(s, c.id, True)
        s.flush()
        assert [f.name for f in foods.search_foods(s)] == ["Cheese", "Bagel", "Apple"]
        assert [f.name for f in foods.search_foods(s, "tilla")] == ["Cheese"]
        assert [f.name for f in foods.recent_foods(s)] == ["Bagel", "Apple"]
        foods.archive_food(s, c.id)
        s.flush()
        assert [f.name for f in foods.search_foods(s)] == ["Bagel", "Apple"]


def test_incomplete_flag(db):
    d = date(2026, 10, 20)
    with session_scope() as s:
        assert not log.is_incomplete(s, d)
        log.set_incomplete(s, d, True)
    with session_scope() as s:
        assert log.is_incomplete(s, d)


def test_manual_targets(db):
    assert manual_targets()["kcal"] is None
    set_manual_targets({"kcal": "2200", "protein_g": 180, "bogus": 1, "fat_g": ""})
    targets = manual_targets()
    assert targets["kcal"] == 2200 and targets["protein_g"] == 180 and targets["fat_g"] is None
    assert "bogus" not in targets


def test_hit_roundtrip():
    hit = parse_search_hit(EGG_SEARCH)
    assert UsdaHit.from_dict(hit.to_dict()) == hit
    assert timedelta  # keep import used


def test_preferred_serving_becomes_default(db):
    hit = parse_search_hit(EGG_SEARCH)
    with session_scope() as s:
        egg = foods.cache_usda_food(
            s,
            hit,
            [
                Serving("1 cup (4.86 large eggs)", 243),
                Serving("1 medium", 44),
                Serving("1 large", 50),
            ],
        )
        egg_id = egg.id
    with session_scope() as s:
        egg = foods.load_food(s, egg_id)
        assert [sv.label for sv in egg.servings] == [
            "1 large",
            "1 medium",
            "1 cup (4.86 large eggs)",
        ]
        assert foods.default_unit(egg) == (f"serving:{egg.servings[0].id}", 1.0)


def test_prefill_uses_last_logged_amount(db):
    from gwc.web.components.common import prefill_amount

    with session_scope() as s:
        egg = _egg(s)
        assert prefill_amount(s, egg) == (f"serving:{egg.servings[0].id}", 1.0)
        log.log_food(
            s,
            food_id=egg.id,
            amount=3,
            unit=f"serving:{egg.servings[0].id}",
            eaten_at=utc(2026, 10, 20),
            tz="x",
        )
        log.log_food(s, food_id=egg.id, amount=2.5, unit="oz", eaten_at=utc(2026, 10, 21), tz="x")
        s.flush()
        assert prefill_amount(s, egg) == ("oz", 2.5)


def test_picker_roundtrip(db):
    from gwc.web.components.common import from_picker, to_picker

    when = utc(2026, 10, 21, 5, 30)  # 01:30 New York... in whatever zone the test machine uses
    day, clock = to_picker(when)
    assert from_picker(day, clock) == when


def test_search_posts_json_body():
    import json as _json

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"foods": []})

    _client(handler).search("banana", branded=False)
    assert seen["method"] == "POST"
    assert seen["body"]["dataType"] == ["Foundation", "SR Legacy", "Survey (FNDDS)"]
    assert seen["body"]["query"] == "banana"
