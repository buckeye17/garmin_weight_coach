import asyncio
import json

import httpx
import pytest

from gwc.coach import label, llm
from gwc.coach.llm import ImageInput, LLMError, LLMRequest, LLMResult, SubscriptionBackend
from gwc.coach.settings import FeatureConfig
from gwc.db.migrate import upgrade
from gwc.db.models import Food, FoodBarcode, FoodSource
from gwc.db.session import session_scope
from gwc.nutrition import barcode
from gwc.nutrition.barcode import BarcodeMatch, OpenFoodFactsClient
from gwc.nutrition.usda import UsdaClient, UsdaError, parse_search_hit

CHEERIOS = "00016000275287"
PER_SERVING = {
    "kcal": 140,
    "protein_g": 5,
    "carbs_g": 29,
    "fat_g": 2.5,
    "fiber_g": 4,
    "sugar_g": 2,
    "sodium_mg": 190,
}


@pytest.fixture
def db(isolated_settings):
    upgrade()


def branded(fdc_id: int, gtin: str, published: str, kcal: float = 359) -> dict:
    return {
        "fdcId": fdc_id,
        "description": "CHEERIOS CEREAL",
        "dataType": "Branded",
        "brandName": "Cheerios",
        "gtinUpc": gtin,
        "publishedDate": published,
        "servingSize": 39,
        "servingSizeUnit": "GRM",
        "householdServingFullText": "1 1/2 cup",
        "foodNutrients": [
            {"nutrientNumber": "208", "unitName": "KCAL", "value": kcal},
            {"nutrientNumber": "203", "unitName": "G", "value": 12.8},
            # A second panel (prepared, or a toddler serving) after the label's own values.
            {"nutrientNumber": "208", "unitName": "KCAL", "value": 117},
            {"nutrientNumber": "203", "unitName": "G", "value": 5.56},
        ],
    }


def usda_client(foods: list[dict] | None = None, status: int = 200) -> UsdaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"foods": foods or []})

    transport = httpx.MockTransport(handler)
    return UsdaClient("k", http=httpx.Client(base_url="https://x", transport=transport))


def off_client(product: dict | None, status: int = 200, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url.path)
        if product is None:
            return httpx.Response(status, json={"status": 0})
        return httpx.Response(status, json={"status": 1, "product": product})

    return OpenFoodFactsClient(http=httpx.Client(transport=httpx.MockTransport(handler)))


OFF_NUTELLA = {
    "code": "3017620422003",
    "product_name": "Nutella",
    "brands": "Nutella, Ferrero",
    "serving_size": "15g",
    "serving_quantity": 15,
    "nutriments": {
        "energy-kj_100g": 2252,
        "proteins_100g": 6.3,
        "carbohydrates_100g": 57.5,
        "fat_100g": 30.9,
        "sugars_100g": 56.3,
        "salt_100g": 0.107,
    },
}


# --------------------------------------------------------------------------- codes


@pytest.mark.parametrize(
    ("raw", "fmt", "expected"),
    [
        ("016000275287", "upc_a", CHEERIOS),
        ("0016000275287", "ean_13", CHEERIOS),
        (" 0 16000 27528 7 ", None, CHEERIOS),
        ("3017620422003", None, "03017620422003"),
        ("96385074", "ean_8", "00000096385074"),
        ("04252614", "upc_e", "00042100005264"),  # UPC-E expands to UPC-A 042100005264
        ("016000275288", None, None),  # bad check digit
        ("12345", None, None),
        ("", None, None),
        (None, None, None),
    ],
)
def test_normalize(raw, fmt, expected):
    assert barcode.normalize(raw, fmt) == expected


def test_forms_and_display():
    assert barcode.written_forms(CHEERIOS) == [CHEERIOS, "0016000275287", "016000275287"]
    assert barcode.display(CHEERIOS) == "016000275287"
    assert barcode.display("03017620422003") == "3017620422003"
    assert barcode.display("00000096385074") == "96385074"


def test_store_codes():
    assert barcode.is_store_code(barcode.normalize("212345678909"))  # UPC-A number system 2
    assert barcode.is_store_code(barcode.normalize("2012345678903"))  # EAN-13 prefix 20
    assert not barcode.is_store_code(CHEERIOS)
    assert not barcode.is_store_code("03017620422003")


# --------------------------------------------------------------------------- USDA


def test_branded_hit_keeps_first_panel():
    hit = parse_search_hit(branded(1, "016000275287", "2023-04-27"))
    assert hit.per100["kcal"] == 359 and hit.per100["protein_g"] == 12.8
    assert hit.servings[0].label == "1 1/2 cup (39 g)"
    food = {**branded(1, "016000275287", "2023"), "householdServingFullText": "3/4 cup (20g)"}
    assert parse_search_hit(food).servings[0].label == "3/4 cup (20g)"  # grams already there


def test_find_gtin_matches_exact_code_and_newest():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "foods": [
                    branded(1, "016000275287", "2023-04-27"),
                    branded(2, "00016000275287", "2026-09-24", kcal=370),
                    branded(3, "016000275294", "2026-10-01", kcal=999),  # another product
                ]
            },
        )

    client = UsdaClient(
        "k", http=httpx.Client(base_url="https://x", transport=httpx.MockTransport(handler))
    )
    hit = client.find_gtin(CHEERIOS, barcode.written_forms(CHEERIOS))
    assert hit.fdc_id == 2 and hit.per100["kcal"] == 370
    assert sent[0]["query"] == "00016000275287 OR 0016000275287 OR 016000275287"
    assert sent[0]["dataType"] == ["Branded"]
    assert usda_client([branded(3, "016000275294", "2026-10-01")]).find_gtin(CHEERIOS, []) is None


# --------------------------------------------------------------------------- Open Food Facts


def test_parse_off_product_converts_units():
    match = barcode.parse_off_product("03017620422003", OFF_NUTELLA)
    assert match.source == "off" and match.name == "Nutella" and match.brand == "Nutella"
    assert match.per100["kcal"] == pytest.approx(2252 / 4.184)
    assert match.per100["sodium_mg"] == pytest.approx(0.107 / 2.5 * 1000)
    assert match.per100["fiber_g"] is None
    assert [(s.label, s.grams) for s in match.servings] == [("1 serving (15 g)", 15.0)]
    assert match.url == "https://world.openfoodfacts.org/product/3017620422003"


@pytest.mark.parametrize(
    ("product", "expected"),
    [
        ({"serving_size": "1 cup (39 g)", "serving_quantity": "39"}, [("1 cup (39 g)", 39.0)]),
        ({"serving_size": "2 cookies", "serving_quantity": 28}, [("2 cookies (28 g)", 28.0)]),
        (
            {"serving_size": "1 can", "serving_quantity": 355, "serving_quantity_unit": "ml"},
            [("1 can (355 ml)", 355.0)],
        ),
        ({"serving_size": "1 cup"}, []),
        ({"serving_size": "a lot", "serving_quantity": 9000}, []),
    ],
)
def test_off_servings(product, expected):
    p = {"product_name": "X", "nutriments": {"energy-kcal_100g": 100}, **product}
    match = barcode.parse_off_product(CHEERIOS, p)
    assert [(s.label, s.grams) for s in match.servings] == expected


def test_off_product_without_calories_is_no_match():
    assert barcode.parse_off_product(CHEERIOS, {"product_name": "X", "nutriments": {}}) is None


def test_off_client_uses_short_code_and_handles_missing():
    seen = []
    assert off_client(OFF_NUTELLA, seen=seen).product("03017620422003")["code"]
    assert seen == ["/api/v2/product/3017620422003"]
    assert off_client(None, status=404).product(CHEERIOS) is None
    assert off_client(None).product(CHEERIOS) is None


# --------------------------------------------------------------------------- lookup


def test_lookup_prefers_usda():
    result = barcode.lookup(
        CHEERIOS, usda_client([branded(1, "016000275287", "2023")]), off_client(OFF_NUTELLA)
    )
    assert result.match.source == "usda" and result.match.usda.fdc_id == 1
    assert result.match.name == "Cheerios Cereal" and result.problems == []


def test_lookup_falls_back_to_off_and_reports_problems():
    result = barcode.lookup("03017620422003", usda_client(status=429), off_client(OFF_NUTELLA))
    assert result.match.source == "off"
    assert result.problems and result.problems[0].startswith("USDA:")

    def down(request):
        raise httpx.ConnectError("offline")

    offline = OpenFoodFactsClient(http=httpx.Client(transport=httpx.MockTransport(down)))
    result = barcode.lookup(CHEERIOS, usda_client(), offline)
    assert result.match is None and "Open Food Facts" in result.problems[0]


def test_lookup_name_hint_when_no_nutrition():
    result = barcode.lookup(
        CHEERIOS, usda_client(), off_client({"product_name": "Mystery Bar", "nutriments": {}})
    )
    assert result.match is None and result.name_hint == "Mystery Bar"


def test_match_roundtrip():
    hit = parse_search_hit(branded(1, "016000275287", "2023"))
    match = BarcodeMatch(CHEERIOS, "usda", hit.name, hit.brand, hit.per100, hit.servings, usda=hit)
    assert BarcodeMatch.from_dict(json.loads(json.dumps(match.to_dict()))) == match


# --------------------------------------------------------------------------- saved barcodes


def test_save_match_and_rescan(db):
    usda = usda_client()
    off = barcode.parse_off_product("03017620422003", OFF_NUTELLA)
    with session_scope() as s:
        food = barcode.save_match(s, off, usda)
        assert food.source is FoodSource.CUSTOM and food.brand == "Nutella"
        assert food.source_url.endswith("/3017620422003")
        assert [sv.label for sv in food.servings] == ["1 serving (15 g)"]
        food_id = food.id
    with session_scope() as s:
        assert barcode.saved_food(s, "03017620422003").id == food_id
        assert s.get(FoodBarcode, "03017620422003").source == "off"

    hit = parse_search_hit(branded(1, "016000275287", "2023"))
    match = BarcodeMatch(CHEERIOS, "usda", hit.name, hit.brand, hit.per100, hit.servings, usda=hit)
    with session_scope() as s:
        food = barcode.save_match(s, match, usda)
        assert food.source is FoodSource.USDA and food.fdc_id == 1
    with session_scope() as s:
        assert barcode.saved_food(s, CHEERIOS).fdc_id == 1
        s.get(Food, barcode.saved_food(s, CHEERIOS).id).is_archived = True
    with session_scope() as s:
        assert barcode.saved_food(s, CHEERIOS) is None  # removed foods don't come back


def test_label_food_relinks_barcode(db):
    with session_scope() as s:
        first = barcode.save_match(
            s, barcode.parse_off_product(CHEERIOS, OFF_NUTELLA), usda_client()
        )
        food = barcode.save_label_food(
            s,
            gtin=CHEERIOS,
            name="Cheerios",
            brand="General Mills",
            serving_label="1 1/2 cup",
            serving_grams=39,
            per_serving=PER_SERVING,
        )
        assert food.id != first.id
        assert food.kcal == pytest.approx(140 * 100 / 39)
        assert food.servings[0].label == "1 1/2 cup" and food.servings[0].grams == 39
    with session_scope() as s:
        assert barcode.saved_food(s, CHEERIOS).name == "Cheerios"
        assert s.get(FoodBarcode, CHEERIOS).source == "label"


# --------------------------------------------------------------------------- label photos

PHOTO = "data:image/jpeg;base64,/9j/AAAA"


def test_images_on_requests():
    img = ImageInput("image/jpeg", "AAAA")
    assert llm.with_images("hi", []) == "hi"
    assert llm.with_images("hi", [img]) == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}},
        {"type": "text", "text": "hi"},
    ]
    req = LLMRequest(
        system="s",
        messages=[{"role": "user", "content": "Read this."}],
        config=FeatureConfig("parse", "opus", "medium", None),
    )
    assert SubscriptionBackend.prompt(req) == "Read this."
    req.images = [img]

    async def collect():
        return [m async for m in SubscriptionBackend.prompt(req)]

    (msg,) = asyncio.run(collect())
    assert msg["type"] == "user" and msg["message"]["content"][0]["type"] == "image"
    assert msg["message"]["content"][-1] == {"type": "text", "text": "Read this."}


def test_image_from_data_url():
    assert label.image_from_data_url(PHOTO) == ImageInput("image/jpeg", "/9j/AAAA")
    with pytest.raises(LLMError):
        label.image_from_data_url("data:application/pdf;base64,AAAA")
    with pytest.raises(LLMError):
        label.image_from_data_url("not a photo")


def test_read_label(isolated_settings):
    upgrade()
    seen = []

    class Backend:
        name = "fake"

        def run(self, req):
            seen.append(req)
            return LLMResult(
                text="",
                model="m",
                data={
                    "readable": True,
                    "name": " Cheerios ",
                    "brand": None,
                    "serving_label": "1 1/2 cup",
                    "serving_grams": 39,
                    "per_serving": {**PER_SERVING, "fiber_g": -1, "sugar_g": "2"},
                    "message": "",
                },
            )

    llm.set_backend_override(Backend())
    result = label.read_label(PHOTO)
    assert result["name"] == "Cheerios" and result["brand"] is None and result["message"] is None
    assert result["serving_grams"] == 39 and result["model"] == "m"
    assert result["per_serving"]["fiber_g"] is None and result["per_serving"]["sugar_g"] == 2
    (req,) = seen
    assert req.images == [ImageInput("image/jpeg", "/9j/AAAA")]
    assert req.output_schema is label.LABEL_SCHEMA and req.config.feature == "parse"


# --------------------------------------------------------------------------- describe-or-scan panel


@pytest.fixture
def scan(db, monkeypatch):
    from gwc.web.app import create_app
    from gwc.web.components import describe

    create_app()
    usda = usda_client([branded(1, "016000275287", "2023")])
    monkeypatch.setattr(describe, "get_usda", lambda: usda)
    monkeypatch.setattr("gwc.coach.parse.get_usda", lambda: usda)
    monkeypatch.setattr(describe, "get_off", lambda: off_client(None))
    return describe


def test_scan_adds_rows_and_rescan(scan):
    data, note, target = scan.on_scan({"code": "016000275287", "format": "upc_a"}, None, None)
    (row,) = data["items"]
    assert row["source"] == "barcode" and row["match"]["source"] == "usda"
    assert row["amount_label"] == "1 1/2 cup" and row["grams"] == 39
    assert target == {"gtin": CHEERIOS, "hint": None} and note is None

    # Saved to the library, the barcode finds it next time, without a lookup.
    with session_scope() as s:
        food_id = scan.save_items(s, [row])[0].id
    data, note, target = scan.on_scan({"code": "0016000275287"}, data, None)
    assert [r["key"] for r in data["items"]] == [0, 1]
    assert data["items"][1]["source"] == "my_food" and data["items"][1]["food_id"] == food_id
    assert target is None

    # A number typed into the description box is looked up the same way.
    out = scan.start_parse(1, " 016000275287 ", None, None, None)
    assert out[0] is scan.no_update and out[5]["items"][0]["food_id"] == food_id
    assert out[7] == ""  # the number is cleared once found


def test_scan_lookup_messages(scan, monkeypatch):
    data, note, _ = scan.lookup_barcode("123", None, None, None)
    assert data is scan.no_update and "isn't a valid product barcode" in str(note)
    _, note, _ = scan.lookup_barcode("212345678909", None, None, None)
    assert "store's own label" in str(note)
    monkeypatch.setattr(scan, "get_usda", lambda: usda_client())
    data, note, target = scan.lookup_barcode("3017620422003", None, None, None)
    assert "Not found" in str(note) and data is scan.no_update
    assert target["gtin"] == "03017620422003"


def test_label_job_adds_a_row(scan):
    from gwc.coach.jobs import get_jobs

    target = {"gtin": CHEERIOS, "hint": "Cheerios"}

    def run(fn):
        job = get_jobs().start("label", fn)
        while job.running:
            pass
        return scan.poll_job(1, {"id": job.id, "kind": "label"}, None, target, None)

    out = run(lambda j: (_ for _ in ()).throw(LLMError("No login.")))
    assert "No login." in str(out[4]) and out[3] is scan.no_update
    out = run(lambda j: {"readable": False, "per_serving": {}, "message": None})
    assert "Couldn't read" in str(out[4])

    result = label.clean_label(
        {
            "readable": True,
            "name": None,
            "serving_label": "1 1/2 cup",
            "serving_grams": 39,
            "per_serving": PER_SERVING,
            "message": "Sodium was hard to read.",
        }
    )
    out = run(lambda j: result)
    (row,) = out[3]["items"]
    assert row["source"] == "label" and row["editable"] and row["name"] == "Cheerios"
    assert row["gtin"] == CHEERIOS and scan.prefill(row)["kcal"] == 140
    card = scan._item_card(row, scan.now(), "log", {})
    assert "Sodium was hard to read." in str(card) and "Add to library" in str(card)


def test_usda_find_gtin_errors():
    with pytest.raises(UsdaError):
        usda_client(status=401).find_gtin(CHEERIOS, [CHEERIOS])


def test_resolve_kinds(db):
    usda, off = usda_client([branded(1, "016000275287", "2023")]), off_client(None)
    with session_scope() as s:
        assert barcode.resolve(s, "123", None, usda, off).kind == "invalid"
        assert barcode.resolve(s, "212345678909", None, usda, off).kind == "store"
        scan = barcode.resolve(s, "016000275287", "upc_a", usda, off)
        assert scan.kind == "lookup" and scan.gtin == CHEERIOS and scan.result.match
        food = barcode.save_match(s, scan.result.match, usda)
        saved = barcode.resolve(s, "0016000275287", "ean_13", usda, off)
        assert (saved.kind, saved.food_id, saved.food_name) == ("saved", food.id, food.name)


@pytest.fixture
def recipe_page(db, monkeypatch):
    from gwc.web.app import create_app
    from gwc.web.pages import library as page

    create_app()
    monkeypatch.setattr(page, "get_usda", lambda: usda_client([branded(1, "016000275287", "2023")]))
    monkeypatch.setattr(page, "get_off", lambda: off_client(None))
    return page


def test_recipe_scan_selects_ingredient(recipe_page, monkeypatch):
    options, value, text, color = recipe_page.recipe_scan(
        {"code": "016000275287", "format": "upc_a"}
    )
    assert text == "Cheerios Cereal: from USDA." and color == "dimmed"
    assert {"value": value, "label": "Cheerios Cereal (Cheerios)"} in options
    with session_scope() as s:
        assert barcode.saved_food(s, CHEERIOS).id == int(value)

    # Scanning it again finds the saved food without a lookup.
    monkeypatch.setattr(recipe_page, "get_usda", lambda: usda_client(status=500))
    _, again, text, _ = recipe_page.recipe_scan({"code": "016000275287", "format": "upc_a"})
    assert again == value and "saved barcodes" in text


def test_recipe_scan_misses(recipe_page, monkeypatch):
    monkeypatch.setattr(recipe_page, "get_usda", lambda: usda_client())
    options, value, text, color = recipe_page.recipe_scan({"code": "3017620422003"})
    assert options is recipe_page.no_update and value is recipe_page.no_update
    assert "with Label" in text and color == "orange"
    _, _, text, _ = recipe_page.recipe_scan({"code": "016000275288"})
    assert "isn't a valid product barcode" in text

    monkeypatch.setattr(recipe_page, "get_off", lambda: off_client(OFF_NUTELLA))
    _, value, text, _ = recipe_page.recipe_scan({"code": "3017620422003"})
    assert value and "Open Food Facts (crowd-sourced" in text
