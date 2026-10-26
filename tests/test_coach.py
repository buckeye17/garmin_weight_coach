import json
import time
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

from fakes import utc
from gwc.coach import chat, llm, parse, summary
from gwc.coach import settings as coach_settings
from gwc.coach.jobs import JobRunner
from gwc.coach.llm import ApiBackend, LLMError, LLMRequest, LLMResult, SubscriptionBackend
from gwc.coach.settings import FeatureConfig
from gwc.coach.tools import FoodLookup, data_tools
from gwc.db.migrate import upgrade
from gwc.db.models import (
    Activity,
    BodyComp,
    CoachMessage,
    CoachSummary,
    DailySummary,
    Food,
    FoodLog,
    FoodSource,
    LogSource,
    Sleep,
)
from gwc.db.session import session_scope
from gwc.nutrition.usda import Serving, UsdaHit

NY = ZoneInfo("America/New_York")
NOW = utc(2026, 10, 20, 18, 0)  # 14:00 EDT
CFG = FeatureConfig("chat", "opus", "medium", None)


@pytest.fixture
def db(isolated_settings):
    upgrade()


class FakeBackend:
    """Scripted backend: `respond(req)` decides the result; every request is recorded."""

    name = "fake"

    def __init__(self, respond):
        self.respond = respond
        self.requests: list[LLMRequest] = []

    def run(self, req: LLMRequest) -> LLMResult:
        self.requests.append(req)
        return self.respond(req)


def use_backend(respond) -> FakeBackend:
    backend = FakeBackend(respond)
    llm.set_backend_override(backend)
    return backend


def wait(job, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while job.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not job.running
    return job


def tool(req: LLMRequest, name: str):
    return next(t for t in req.tools if t.name == name)


def seed(s, days: int = 10):
    today = NOW.astimezone(NY).date()
    for i in range(days, 0, -1):
        d = today - timedelta(days=i)
        noon = datetime(d.year, d.month, d.day, 12, tzinfo=NY).astimezone(UTC)
        s.add(
            DailySummary(
                calendar_date=d,
                total_kcal=3000,
                resting_kcal=2200,
                steps=7000,
                is_provisional=False,
            )
        )
        s.add(BodyComp(measured_at=noon, weight_kg=110 - 0.05 * (days - i), body_fat_pct=30))
        s.add(
            Sleep(
                calendar_date=d,
                start_at=noon - timedelta(hours=8),
                end_at=noon - timedelta(hours=1),
                duration_s=7 * 3600,
                sleep_score=80,
            )
        )
        s.add(
            FoodLog(
                eaten_at=noon,
                tz="America/New_York",
                description="Oats",
                grams=100,
                kcal=2500,
                protein_g=150,
                source=LogSource.CUSTOM,
            )
        )
    s.add(
        Activity(
            garmin_activity_id=1,
            activity_type="walking",
            start_at=NOW - timedelta(days=1),
            duration_s=1800,
            kcal=200,
        )
    )


@pytest.fixture
def clock(monkeypatch, isolated_settings):
    monkeypatch.setattr(isolated_settings, "tz_name", "America/New_York")
    for target in ("gwc.coach.tools.utcnow", "gwc.coach.summary.utcnow"):
        monkeypatch.setattr(target, lambda: NOW)


# --------------------------------------------------------------------------- settings


def test_settings_defaults_and_save(db):
    cfg = coach_settings.feature_config("parse")
    assert (cfg.model, cfg.effort, cfg.fallback_model) == ("opus", "medium", None)
    assert coach_settings.backend_name() == "subscription"
    assert coach_settings.summary_enabled() and coach_settings.summary_weekday() == 0
    coach_settings.save_coach_settings(
        backend="api",
        models={"chat": "sonnet", "parse": " claude-haiku-5-5 ", "summary": "fable"},
        efforts={"chat": "low", "parse": "bogus", "summary": "max"},
        fallback_model="haiku",
        summary_enabled=False,
        summary_weekday=9,
    )
    assert coach_settings.feature_config("chat") == FeatureConfig("chat", "sonnet", "low", "haiku")
    assert coach_settings.feature_config("parse").model == "claude-haiku-5-5"
    assert coach_settings.feature_config("parse").effort == "medium"
    assert coach_settings.backend_name() == "api"
    assert not coach_settings.summary_enabled() and coach_settings.summary_weekday() == 6
    assert coach_settings.full_model_id("fable") == "claude-fable-5-1"
    assert coach_settings.full_model_id("claude-x") == "claude-x"


# --------------------------------------------------------------------------- backends


def _req(**kw) -> LLMRequest:
    spec = llm.ToolSpec("get_x", "X", {"type": "object", "properties": {}}, lambda a: {"x": 1})
    return LLMRequest(
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        config=kw.pop("config", CFG),
        tools=[spec],
        **kw,
    )


def test_api_request_kwargs():
    backend = ApiBackend("key")
    schema = {"type": "object"}
    kw = backend.request_kwargs(_req(output_schema=schema), [{"role": "user", "content": "hi"}])
    assert kw["model"] == "claude-opus-5-5"
    assert kw["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": schema},
    }
    assert kw["tools"][0]["name"] == "get_x" and "input_schema" in kw["tools"][0]
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    haiku = backend.request_kwargs(_req(config=FeatureConfig("chat", "haiku", "low", None)), [])
    assert haiku["model"] == "claude-haiku-5-5" and "fallbacks" not in haiku


def test_subscription_options(isolated_settings):
    opts = SubscriptionBackend().options(
        _req(
            output_schema={"type": "object"},
            on_text=lambda _t: None,
            config=FeatureConfig("chat", "sonnet", "high", "haiku"),
        ),
        tool_server=object(),
    )
    assert opts.tools == [] and opts.setting_sources == [] and opts.strict_mcp_config
    assert opts.allowed_tools == ["mcp__gwc__get_x"]
    assert (opts.model, opts.effort, opts.fallback_model) == ("sonnet", "high", "haiku")
    assert opts.env["ANTHROPIC_API_KEY"] == "" and opts.env["ENABLE_TOOL_SEARCH"] == "false"
    assert opts.output_format == {"type": "json_schema", "schema": {"type": "object"}}
    assert opts.include_partial_messages


def test_web_tools_per_backend(isolated_settings):
    opts = SubscriptionBackend().options(_req(web=True), tool_server=object())
    assert opts.tools == ["WebSearch", "WebFetch"]
    assert opts.allowed_tools == ["mcp__gwc__get_x", "WebSearch", "WebFetch"]

    backend = ApiBackend("key")
    names = [t["name"] for t in backend.request_kwargs(_req(web=True), [])["tools"]]
    assert names == ["get_x", "web_research"]
    assert [t["name"] for t in backend.request_kwargs(_req(), [])["tools"]] == ["get_x"]
    assert [t["type"] for t in backend.web_tools("claude-opus-5-5")] == [
        "web_search_20260209",
        "web_fetch_20260209",
    ]
    assert [t["type"] for t in backend.web_tools("claude-haiku-5-5")] == ["web_search_20250305"]


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        return self.responses.pop(0)


def test_api_research_resumes_paused_turns():
    paused = NS(stop_reason="pause_turn", content=[NS(type="server_tool_use")])
    url = "https://www.chipotle.com/nutrition-calculator"
    done = NS(
        stop_reason="end_turn",
        content=[
            NS(type="web_search_tool_result"),
            NS(type="text", text="Chicken bowl: 650 kcal", citations=[NS(url=url)]),
            NS(type="text", text=".", citations=None),
        ],
    )
    messages = FakeMessages([paused, done])
    out = ApiBackend("key").research(NS(beta=NS(messages=messages)), _req(web=True), "bowl?")
    assert out == {"findings": "Chicken bowl: 650 kcal.", "sources": [url]}
    first, second = messages.calls
    assert first["tools"][0]["type"] == "web_search_20260209"
    assert "format" not in first["output_config"]  # citations can't mix with a JSON answer
    # The paused turn goes back unchanged, with no extra user message.
    assert second["messages"][1] == {"role": "assistant", "content": paused.content}
    assert len(second["messages"]) == 2


def test_transcript_prompt():
    assert llm.transcript_prompt([{"role": "user", "content": "hi"}]) == "hi"
    text = llm.transcript_prompt(
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]
    )
    assert "User:\na" in text and "Coach (you):\nb" in text and text.endswith("c")


def test_get_backend_selection(db, monkeypatch, isolated_settings):
    assert isinstance(llm.get_backend(), SubscriptionBackend)
    coach_settings.save_coach_settings(
        backend="api",
        models={},
        efforts={},
        fallback_model=None,
        summary_enabled=True,
        summary_weekday=0,
    )
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        llm.get_backend()
    from pydantic import SecretStr

    monkeypatch.setattr(isolated_settings, "anthropic_api_key", SecretStr("sk-test"))
    assert isinstance(llm.get_backend(), ApiBackend)


# --------------------------------------------------------------------------- jobs


def test_job_runner():
    runner = JobRunner()

    def work(job):
        job.on_activity("Searching…")
        job.on_text("Hel")
        job.on_text("lo")
        return "done"

    job = wait(runner.start("chat", work, key="k"))
    assert job.status == "done" and job.result == "done" and job.text == "Hello"

    def boom(_job):
        raise LLMError("nope")

    failed = wait(runner.start("chat", boom))
    assert failed.status == "error" and failed.error == "nope"

    def crash(_job):
        raise RuntimeError("bad")

    assert "bad" in wait(runner.start("x", crash)).error


def test_job_runner_one_per_key():
    runner = JobRunner()
    gate = {"go": False}

    def slow(_job):
        while not gate["go"]:
            time.sleep(0.01)

    first = runner.start("chat", slow, key="c1")
    assert runner.start("chat", slow, key="c1") is first
    assert runner.running("chat") == [first]
    gate["go"] = True
    wait(first)


# --------------------------------------------------------------------------- tools


def test_data_tools(db, clock):
    with session_scope() as s:
        seed(s)
    tools = {t.name: t for t in data_tools()}
    overview = json.loads(tools["get_overview"].call({}))
    assert overview["today_food_day"] == "2026-10-20"
    assert overview["weight_unit"] == "lb" and overview["trend_weight"] > 200
    assert overview["last_7_food_days_complete"] == 7
    assert overview["targets_today"]["kcal"] is not None

    nutrition = json.loads(tools["get_daily_nutrition"].call({}))["days"]
    assert len(nutrition) == 7
    assert nutrition[-1]["status"] == "in progress" and "targets" in nutrition[-1]
    assert nutrition[0]["status"] == "complete" and nutrition[0]["intake"]["kcal"] == 2500

    entries = json.loads(tools["get_food_entries"].call({"food_day": "2026-10-19"}))
    assert entries["entries"][0]["food"] == "Oats"

    trend = json.loads(tools["get_weight_trend"].call({"days": 5}))
    assert len(trend["days"]) == 5
    assert json.loads(tools["get_sleep"].call({}))["sleeps"][0]["score"] == 80
    assert json.loads(tools["get_activities"].call({}))["activities"][0]["kcal"] == 200
    assert len(json.loads(tools["get_garmin_daily"].call({"days": 3}))["days"]) == 2
    assert json.loads(tools["get_body_comp"].call({"days": 3}))["weigh_ins"]
    targets = json.loads(tools["get_targets"].call({}))
    assert targets["recorded_for_day"] and targets["burn_method"]
    projection = json.loads(tools["get_projection"].call({}))
    assert {s["name"] for s in projection["scenarios"]} >= {"At your target"}
    # Out-of-range day counts are clamped rather than rejected.
    assert json.loads(tools["get_sleep"].call({"days": 9999}))["sleeps"]


class FakeUsda:
    def __init__(self, hits):
        self.hits = hits

    def search(self, query, *, branded=False, page_size=25):
        return [h for h in self.hits if query.lower() in h.name.lower()]

    def servings_for(self, hit):
        return hit.servings


SOURDOUGH = UsdaHit(
    fdc_id=555,
    name="Bread, sourdough",
    data_type="Survey (FNDDS)",
    brand=None,
    per100={
        "kcal": 270,
        "protein_g": 10,
        "carbs_g": 50,
        "fat_g": 3,
        "fiber_g": 2,
        "sugar_g": 3,
        "sodium_mg": 500,
    },
    servings=[Serving("1 slice", 40)],
)


@pytest.fixture
def usda(monkeypatch):
    fake = FakeUsda([SOURDOUGH])
    monkeypatch.setattr("gwc.coach.tools.get_usda", lambda: fake)
    monkeypatch.setattr("gwc.coach.parse.get_usda", lambda: fake)
    return fake


def _egg(s) -> Food:
    egg = Food(
        source=FoodSource.CUSTOM,
        name="Egg",
        kcal=143,
        protein_g=12.6,
        carbs_g=0.7,
        fat_g=9.5,
        fiber_g=0,
        sugar_g=0.4,
        sodium_mg=142,
    )
    s.add(egg)
    s.flush()
    return egg


def test_food_lookup(db, usda):
    with session_scope() as s:
        _egg(s)
    lookup = FoodLookup()
    mine = lookup.search_my_foods({"query": "egg"})
    assert mine["foods"][0]["name"] == "Egg" and mine["foods"][0]["per_100g"]["kcal"] == 143
    found = lookup.search_usda({"query": "sourdough"})
    assert found["results"][0]["fdc_id"] == 555
    assert lookup.usda_hits[555] is SOURDOUGH


# --------------------------------------------------------------------------- parsing


def test_parse_and_confirm(db, usda):
    with session_scope() as s:
        egg_id = _egg(s).id

    def respond(req):
        assert req.output_schema is parse.ITEMS_SCHEMA
        assert "13:00" not in req.messages[0]["content"]  # local time is given, not UTC
        tool(req, "search_my_foods").call({"query": "egg"})
        tool(req, "search_usda").call({"query": "sourdough"})
        item = {"food_id": None, "fdc_id": None, "eaten_at": None, "estimate": None}
        return LLMResult(
            text="",
            model="claude-opus-5-5",
            data={
                "items": [
                    {
                        **item,
                        "description": "2 eggs",
                        "source": "my_food",
                        "food_id": egg_id,
                        "amount_label": "2 large",
                        "grams": 100,
                        "eaten_at": "2026-10-20T01:30",
                    },
                    {
                        **item,
                        "description": "slice of sourdough",
                        "source": "usda",
                        "fdc_id": 555,
                        "amount_label": "1 slice",
                        "grams": 40,
                    },
                    {
                        **item,
                        "description": "chef's special",
                        "source": "estimate",
                        "amount_label": "1 plate",
                        "grams": 400,
                        "estimate": {
                            "kcal": 800,
                            "protein_g": 40,
                            "carbs_g": 60,
                            "fat_g": 40,
                            "fiber_g": 5,
                            "sugar_g": 10,
                            "sodium_mg": 1200,
                        },
                    },
                    {
                        **item,
                        "description": "mystery",
                        "source": "usda",
                        "fdc_id": 999,
                        "amount_label": "1",
                        "grams": 10,
                    },
                    {
                        **item,
                        "description": "tomorrow snack",
                        "source": "my_food",
                        "food_id": egg_id,
                        "amount_label": "1",
                        "grams": 50,
                        "eaten_at": "2026-10-21T10:00",
                    },
                ],
                "message": "Assumed large eggs.",
            },
        )

    use_backend(respond)
    out = parse.parse_description("2 eggs at 1:30am, sourdough, the chef's special", NY, NOW)
    items = out["items"]
    assert out["message"] == "Assumed large eggs."
    assert items[0]["name"] == "Egg" and items[0]["eaten_at"] == "2026-10-20T01:30:00-04:00"
    assert parse.item_nutrients(items[0])["kcal"] == pytest.approx(143)
    assert items[1]["hit"]["fdc_id"] == 555
    assert parse.item_nutrients(items[1])["kcal"] == pytest.approx(108)
    assert parse.item_nutrients(items[2])["kcal"] == pytest.approx(800)
    assert parse.item_nutrients(items[2], 200)["kcal"] == pytest.approx(400)
    assert "didn't look up" in items[3]["error"]
    assert datetime.fromisoformat(items[4]["eaten_at"]) == NOW  # future times clamp to now

    with session_scope() as s:
        logged = parse.confirm_items(s, items, NY)
    assert logged == 4  # the unmatched one is skipped
    with session_scope() as s:
        rows = s.query(FoodLog).order_by(FoodLog.id).all()
        assert all(r.source is LogSource.AI for r in rows)
        assert rows[0].unit_label == "2 large (100 g)" and rows[0].kcal == pytest.approx(143)
        assert rows[1].food.fdc_id == 555 and rows[1].food.servings[0].label == "1 slice"
        assert rows[2].food_id is None and rows[2].kcal == pytest.approx(800)
        assert rows[2].note.startswith("Estimated by Claude")


def test_retime_moves_the_meal_and_keeps_gaps():
    rows = [
        {"key": 0, "eaten_at": "2026-10-20T08:15:00-04:00"},
        {"key": 1, "eaten_at": "2026-10-20T07:30:00-04:00"},
    ]
    first = parse.first_eaten_at(rows)
    assert first == datetime(2026, 10, 20, 7, 30, tzinfo=NY)
    assert parse.first_eaten_at([]) is None

    moved = parse.retime(rows, first, datetime(2026, 10, 19, 19, 0, tzinfo=NY))
    assert [datetime.fromisoformat(r["eaten_at"]) for r in moved] == [
        datetime(2026, 10, 19, 19, 45, tzinfo=NY),
        datetime(2026, 10, 19, 19, 0, tzinfo=NY),
    ]
    assert rows[0]["eaten_at"] == "2026-10-20T08:15:00-04:00"  # input untouched


BOWL = {
    "kcal": 650,
    "protein_g": 50,
    "carbs_g": 70,
    "fat_g": 18,
    "fiber_g": 12,
    "sugar_g": 5,
    "sodium_mg": 1900,
}


def _restaurant(item: str, kind: str = "official", nutrients=BOWL, grams: float = 500):
    found = kind != "not_found"
    return {
        "chain": "Chipotle",
        "item": item,
        "source_kind": kind,
        "source_url": "https://www.chipotle.com/nutrition-calculator" if found else None,
        "serving_label": "1 bowl",
        "serving_grams": grams,
        "nutrients": nutrients if found else None,
    }


def test_parse_restaurant_items(db, usda):
    def respond(req):
        assert req.web  # restaurant items are researched on the web
        assert "Country for restaurant menus: United States" in req.messages[0]["content"]
        base = {"food_id": None, "fdc_id": None, "eaten_at": None, "amount_label": "1 bowl"}
        return LLMResult(
            text="",
            data={
                "items": [
                    {
                        **base,
                        "description": "chicken bowl no cheese",
                        "source": "restaurant",
                        "grams": 500,
                        "estimate": None,
                        "restaurant": _restaurant("Chicken Burrito Bowl, no cheese"),
                    },
                    {
                        **base,
                        "description": "secret menu quesarito",
                        "source": "estimate",
                        "grams": 0,
                        "estimate": {**BOWL, "kcal": 900},
                        "restaurant": _restaurant("Quesarito", "not_found", grams=300),
                    },
                    {
                        **base,
                        "description": "seasonal special",
                        "source": "estimate",
                        "grams": 0,
                        "estimate": None,
                        "restaurant": _restaurant("Seasonal Special", "not_found", grams=250),
                    },
                ],
                "message": None,
            },
        )

    use_backend(respond)
    found, guessed, blank = parse.parse_description("chipotle", NY, NOW)["items"]
    assert found["source"] == "restaurant" and not found["editable"]
    assert found["name"] == "Chicken Burrito Bowl, no cheese"
    assert parse.item_nutrients(found)["kcal"] == pytest.approx(650)
    assert parse.item_nutrients(found, 250)["kcal"] == pytest.approx(325)
    # Not found: editable, weighed at the serving, prefilled with Claude's guess if any.
    assert guessed["editable"] and guessed["grams"] == 300
    assert parse.item_nutrients(guessed)["kcal"] == pytest.approx(900)
    assert blank["editable"] and blank["per100"] is None and blank["grams"] == 250

    with session_scope() as s:
        parse.confirm_items(s, [found], NY)
    with session_scope() as s:  # ordered again: the saved food is refreshed, not duplicated
        parse.confirm_items(s, [{**found, "grams": 250}], NY)
    typed = {**BOWL, "kcal": 450}
    with session_scope() as s:
        parse.confirm_items(
            s,
            [
                {**blank, "totals": typed, "edited": True},
                {**guessed, "totals": parse.item_nutrients(guessed), "edited": False},
            ],
            NY,
        )
    with session_scope() as s:
        saved = {f.name: f for f in s.query(Food).filter(Food.brand == "Chipotle")}
        assert set(saved) == {"Chicken Burrito Bowl, no cheese", "Seasonal Special"}
        bowl = saved["Chicken Burrito Bowl, no cheese"]
        assert bowl.source_url == "https://www.chipotle.com/nutrition-calculator"
        assert [(sv.label, sv.grams) for sv in bowl.servings] == [("1 bowl", 500)]
        assert saved["Seasonal Special"].kcal == pytest.approx(450 * 100 / 250)
        kcal = [r.kcal for r in s.query(FoodLog).order_by(FoodLog.id)]
        assert kcal == pytest.approx([650, 325, 450, 900])
    # Next time Claude finds the bowl among the user's foods, with its source.
    (hit,) = FoodLookup().search_my_foods({"query": "chipotle bowl"})["foods"]
    assert hit["brand"] == "Chipotle" and hit["source_url"].endswith("nutrition-calculator")


def test_parse_plate_photo(db, usda):
    from gwc.coach.llm import ImageInput

    def respond(req):
        assert "photo of my plate" in req.messages[0]["content"]
        assert "itemize the photo" in req.messages[0]["content"]  # nothing was typed
        item = {"food_id": None, "fdc_id": None, "eaten_at": None, "restaurant": None}
        return LLMResult(
            text="",
            data={
                "items": [
                    {
                        **item,
                        "description": "pad thai",
                        "source": "estimate",
                        "amount_label": "1 plate",
                        "grams": 450,
                        "estimate": {**BOWL, "kcal": 900},
                    },
                    {
                        **item,
                        "description": "cooking oil (est.)",
                        "source": "estimate",
                        "amount_label": "1 tbsp",
                        "grams": 14,
                        "estimate": {**dict.fromkeys(BOWL, 0), "kcal": 120, "fat_g": 14},
                    },
                ],
                "message": "Sized by the dinner plate.",
            },
        )

    backend = use_backend(respond)
    out = parse.parse_description("  ", NY, NOW, photo="data:image/jpeg;base64,/9j/AAAA")
    (req,) = backend.requests
    assert req.images == [ImageInput("image/jpeg", "/9j/AAAA")]
    assert all(row["photo"] and row["editable"] for row in out["items"])
    with session_scope() as s:
        assert parse.confirm_items(s, out["items"], NY) == 2
    with session_scope() as s:
        rows = s.query(FoodLog).order_by(FoodLog.id).all()
        assert [r.kcal for r in rows] == pytest.approx([900, 120])
        assert rows[0].note == "Estimated by Claude · From photo: pad thai"
        assert rows[0].food_id is None and s.query(Food).count() == 0  # not saved as foods

    with pytest.raises(LLMError):
        parse.parse_description("lunch", NY, NOW, photo="data:application/pdf;base64,AAAA")
    assert len(backend.requests) == 1  # a bad file never reaches Claude

    # Without a photo, nothing about one is said or sent.
    use_backend(lambda req: LLMResult(text="", data={"items": [], "message": None}))
    parse.parse_description("lunch", NY, NOW)
    req = llm.get_backend().requests[0]
    assert req.images == [] and "photo" not in req.messages[0]["content"]


def test_parse_handles_empty_result(db, usda):
    use_backend(lambda req: LLMResult(text="", data=None))
    assert parse.parse_description("nothing", NY, NOW)["items"] == []


# --------------------------------------------------------------------------- chat


def test_chat_send_and_history(db, clock):
    def respond(req):
        assert "Local time Tuesday 2026-10-20 14:00" in req.messages[-1]["content"]
        assert {t.name for t in req.tools} >= {"get_overview", "get_daily_nutrition"}
        req.on_activity("Reading…")
        req.on_text("You're ")
        req.on_text("on track.")
        return LLMResult(text="You're on track.", model="claude-opus-5-5")

    backend = use_backend(respond)
    conv_id, job = chat.send(None, "How am I doing this week?", NY, NOW)
    assert wait(job).status == "done" and job.text == "You're on track."
    msgs = chat.messages(conv_id)
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "How am I doing this week?"),
        ("assistant", "You're on track."),
    ]
    assert msgs[1].model == "claude-opus-5-5"
    assert chat.list_conversations()[0].title == "How am I doing this week?"

    same_id, job2 = chat.send(conv_id, "And protein?", NY, NOW)
    wait(job2)
    assert same_id == conv_id
    assert [m["role"] for m in backend.requests[-1].messages] == ["user", "assistant", "user"]
    # Stored messages don't include the injected context line.
    assert chat.messages(conv_id)[2].content == "And protein?"

    chat.delete_conversation(conv_id)
    assert chat.list_conversations() == []
    with session_scope() as s:
        assert s.query(CoachMessage).count() == 0


def test_chat_error_keeps_user_message(db, clock):
    def respond(_req):
        raise LLMError("You've hit your Claude plan's usage limit for now.")

    use_backend(respond)
    conv_id, job = chat.send(None, "hi", NY, NOW)
    assert wait(job).status == "error" and "usage limit" in job.error
    assert [m.role for m in chat.messages(conv_id)] == ["user"]


# --------------------------------------------------------------------------- summaries


def test_last_full_week():
    assert summary.last_full_week(date(2026, 10, 20)) == date(2026, 10, 12)  # Tue → prior Mon
    assert summary.last_full_week(date(2026, 10, 19)) == date(2026, 10, 12)  # Mon
    assert summary.last_full_week(date(2026, 10, 25)) == date(2026, 10, 12)  # Sun


def test_summary_due_and_generate(db, clock):
    # Monday Oct 19 09:05 EDT: before the 09:11 due time.
    early = utc(2026, 10, 19, 13, 5)
    assert summary.due_week(NY, early) is None
    assert summary.due_week(NY, utc(2026, 10, 19, 13, 15)) == date(2026, 10, 12)
    assert summary.due_week(NY, NOW) == date(2026, 10, 12)

    def respond(req):
        assert "2026-10-12 to 2026-10-18" in req.messages[0]["content"]
        return LLMResult(text="## Week\nGood week.", model="claude-opus-5-5")

    use_backend(respond)
    job = summary.run_if_due(NY, NOW)
    assert wait(job).status == "done"
    assert summary.has_summary(date(2026, 10, 12))
    assert summary.due_week(NY, NOW) is None
    # Regenerating replaces the week's summary instead of duplicating it.
    wait(summary.start_generate(date(2026, 10, 12), NY, NOW))
    with session_scope() as s:
        assert s.query(CoachSummary).count() == 1

    coach_settings.save_coach_settings(
        backend="subscription",
        models={},
        efforts={},
        fallback_model=None,
        summary_enabled=False,
        summary_weekday=0,
    )
    with session_scope() as s:
        s.query(CoachSummary).delete()
    assert summary.due_week(NY, NOW) is None


def test_check_and_run_survives_errors(db, clock, monkeypatch):
    monkeypatch.setattr("gwc.coach.summary.run_if_due", lambda *_a: 1 / 0)
    summary.check_and_run(NY)  # logs, doesn't raise
