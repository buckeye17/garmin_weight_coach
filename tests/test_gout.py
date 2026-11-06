import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gwc.coach import llm
from gwc.coach.llm import LLMError, LLMResult
from gwc.db.migrate import upgrade
from gwc.db.models import (
    Activity,
    Food,
    FoodLog,
    FoodSource,
    GoutProfile,
    LogSource,
    RecipeItem,
)
from gwc.db.session import session_scope
from gwc.gout import estimate, flares, profiles, triggers
from gwc.gout.triggers import Level

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 11, 5, 18, 0, tzinfo=UTC)  # 13:00 EST


@pytest.fixture
def db(isolated_settings):
    upgrade()


class Backend:
    name = "fake"

    def __init__(self, respond):
        self.respond = respond
        self.requests = []

    def run(self, req):
        self.requests.append(req)
        return self.respond(req)


def food(s, name, *, source=FoodSource.CUSTOM, sugar=0.0, **kw) -> Food:
    f = Food(source=source, name=name, kcal=100, protein_g=10, sugar_g=sugar, **kw)
    s.add(f)
    s.flush()
    return f


def entry(s, f: Food | None, grams: float, at: datetime, *, sugar=0.0, desc=None) -> FoodLog:
    e = FoodLog(
        eaten_at=at,
        tz="America/New_York",
        food_id=f.id if f else None,
        description=desc or (f.name if f else "meal"),
        grams=grams,
        sugar_g=sugar,
        source=LogSource.CUSTOM if f else LogSource.AI,
    )
    s.add(e)
    s.flush()
    return e


def profile(s, f: Food, purine, animal=0.0, alcohol=0.0, sweet=False, source="ai"):
    return profiles.save_profile(
        s,
        food_id=f.id,
        purine_mg=purine,
        animal_purine_mg=animal,
        alcohol_g=alcohol,
        sweet_drink=sweet,
        source=source,
    )


# --------------------------------------------------------------------------- profiles


def test_food_profile_scales_and_entry_profile_wins(db):
    with session_scope() as s:
        chicken = food(s, "Chicken breast")
        profile(s, chicken, 140, 140)
        e = entry(s, chicken, 200, NOW)
        book = profiles.ProfileBook(s)
        x = book.exposure(e)
        assert x.purine_mg == pytest.approx(280) and x.plant_purine_mg == 0

        profiles.save_profile(
            s,
            food_log_id=e.id,
            purine_mg=50,
            animal_purine_mg=0,
            alcohol_g=0,
            sweet_drink=False,
            source="manual",
        )
        assert profiles.ProfileBook(s).exposure(e).purine_mg == pytest.approx(100)


def test_values_are_clamped():
    assert profiles.clean_values(-5, 50, 200) == (0.0, 0.0, 80.0)
    assert profiles.clean_values(100, 150, None) == (100.0, 100.0, 0.0)


def test_recipe_uses_ingredients(db):
    with session_scope() as s:
        beef = food(s, "Beef")
        beans = food(s, "Beans")
        soda = food(s, "Cola", sugar=10)
        profile(s, beef, 120, 120)
        profile(s, beans, 60)
        profile(s, soda, 0, sweet=True)
        chili = food(s, "Chili", source=FoodSource.RECIPE, recipe_yield_g=500)
        chili.recipe_items = [
            RecipeItem(ingredient_id=beef.id, grams=200),
            RecipeItem(ingredient_id=beans.id, grams=200),
            RecipeItem(ingredient_id=soda.id, grams=100),
        ]
        s.flush()
        p = profiles.ProfileBook(s).for_food(chili.id)
        assert p.source == "recipe"
        assert p.purine_mg == pytest.approx((240 + 120) / 5)
        assert p.animal_purine_mg == pytest.approx(240 / 5)
        assert p.sweet_sugar_g == pytest.approx(10 / 5)

        missing = food(s, "Mystery")
        chili.recipe_items.append(RecipeItem(ingredient_id=missing.id, grams=10))
        s.flush()
        assert profiles.ProfileBook(s).for_food(chili.id) is None


def test_sweet_drink_counts_its_sugar(db):
    with session_scope() as s:
        cola = food(s, "Cola", sugar=10.6)
        profile(s, cola, 0, sweet=True)
        e = entry(s, cola, 368, NOW)
        assert profiles.ProfileBook(s).exposure(e).sweet_sugar_g == pytest.approx(39, abs=0.1)
        one_off = entry(s, None, 300, NOW, sugar=30, desc="Lemonade")
        profiles.save_profile(
            s,
            food_log_id=one_off.id,
            purine_mg=0,
            animal_purine_mg=0,
            alcohol_g=0,
            sweet_drink=True,
            source="ai",
        )
        assert profiles.ProfileBook(s).exposure(one_off).sweet_sugar_g == pytest.approx(30)


def test_entry_override_updates_food_or_entry(db):
    with session_scope() as s:
        steak = food(s, "Steak")
        e = entry(s, steak, 250, NOW)
        profiles.save_entry_override(
            s, e.id, purine_mg=300, animal_purine_mg=300, alcohol_g=0, sweet_drink=False
        )
        row = s.query(GoutProfile).filter_by(food_id=steak.id).one()
        assert row.purine_mg == pytest.approx(120) and row.source == "manual"

        meal = entry(s, None, 400, NOW, desc="Ramen")
        profiles.save_entry_override(
            s, meal.id, purine_mg=200, animal_purine_mg=100, alcohol_g=0, sweet_drink=False
        )
        row = s.query(GoutProfile).filter_by(food_log_id=meal.id).one()
        assert row.purine_mg == pytest.approx(50) and row.animal_purine_mg == pytest.approx(25)


# --------------------------------------------------------------------------- estimates


def test_pending_lists_foods_ingredients_and_one_offs(db):
    with session_scope() as s:
        a = food(s, "Tuna")
        b = food(s, "Rice")
        done = food(s, "Egg")
        profile(s, done, 5)
        bowl = food(s, "Poke bowl", source=FoodSource.RECIPE, recipe_yield_g=300)
        bowl.recipe_items = [
            RecipeItem(ingredient_id=a.id, grams=150),
            RecipeItem(ingredient_id=b.id, grams=150),
        ]
        old = food(s, "Old", is_archived=True)
        entry(s, old, 100, NOW - timedelta(days=200))
        entry(s, None, 350, NOW - timedelta(hours=3), desc="Pho")
        keys = [i["key"] for i in estimate.pending(s, NOW)]
    assert sorted(k for k in keys if k.startswith("food")) == sorted(
        [f"food:{a.id}", f"food:{b.id}"]
    )
    assert len([k for k in keys if k.startswith("entry")]) == 1


def test_estimate_saves_answers_and_keeps_manual(db, monkeypatch):
    monkeypatch.setattr(estimate, "utcnow", lambda: NOW)
    with session_scope() as s:
        sardines = food(s, "Sardines")
        beer = food(s, "Beer")
        meal = entry(s, None, 400, NOW - timedelta(hours=2), desc="Ramen")
        ids = (sardines.id, beer.id, meal.id)

    FIELDS = ("purine_mg", "animal_purine_mg", "alcohol_g", "sweet_drink")

    def respond(req):
        items = json.loads(req.messages[0]["content"])["items"]
        assert req.output_schema is estimate.SCHEMA and req.config.effort == "low"
        values = {
            f"food:{ids[0]}": (300, 300, 0, False),
            f"food:{ids[1]}": (10, 0, 3.9, False),
            f"entry:{ids[2]}": (60, 40, 0, False),
        }
        rows = [
            dict(zip(FIELDS, values[i["key"]], strict=True)) | {"key": i["key"], "note": "typical"}
            for i in items
        ]
        rows.append(
            {
                "key": "food:9999",
                "purine_mg": 1,
                "animal_purine_mg": 0,
                "alcohol_g": 0,
                "sweet_drink": False,
                "note": "not asked",
            }
        )
        return LLMResult(text="", data={"items": rows})

    backend = Backend(respond)
    llm.set_backend_override(backend)
    assert estimate.estimate_pending() == 3
    assert estimate.pending_count(NOW) == 0
    with session_scope() as s:
        assert s.query(GoutProfile).count() == 3
        beer_row = s.query(GoutProfile).filter_by(food_id=ids[1]).one()
        assert beer_row.alcohol_g == pytest.approx(3.9) and beer_row.source == "ai"
    assert estimate.estimate_pending() == 0
    assert len(backend.requests) == 1


def test_scheduled_estimate_backs_off_after_failure(db, monkeypatch):
    with session_scope() as s:
        food(s, "Liver")

    def fail(req):
        raise LLMError("down")

    llm.set_backend_override(Backend(fail))
    monkeypatch.setattr(estimate, "_last_failure", None)
    estimate.run_if_pending()
    job = next(iter(estimate.get_jobs()._jobs.values()))
    deadline = datetime.now() + timedelta(seconds=5)
    while job.running and datetime.now() < deadline:
        pass
    assert job.status == "error" and estimate._last_failure is not None
    started = []
    monkeypatch.setattr(estimate, "start_estimate", lambda: started.append(1))
    estimate.run_if_pending()
    assert not started


# --------------------------------------------------------------------------- flares


def test_flare_lifecycle(db):
    start = datetime(2026, 11, 2, 21, 0, tzinfo=NY)
    with session_scope() as s:
        with pytest.raises(ValueError, match="how bad"):
            flares.save_flare(s, None, started_at=start, tz="America/New_York", joints=[], note="")
        flare = flares.save_flare(
            s,
            None,
            started_at=start,
            tz="America/New_York",
            joints=["Big toe", "big toe", " Ankle "],
            note="woke up with it",
            severity=3,
            medications=["Colchicine"],
        )
        fid = flare.id
    with session_scope() as s:
        flare = flares.load_flare(s, fid)
        assert flare.joints == ["Big toe", "Ankle"]
        assert [(d.day, d.severity, d.medications) for d in flare.days] == [
            (date(2026, 11, 2), 3, ["Colchicine"])
        ]
        assert flares.active_flare(s).id == fid
        flares.set_day(s, fid, date(2026, 11, 3), 4, ["Ibuprofen"], None)
        flares.set_day(s, fid, date(2026, 11, 4), 2, [], None)
        flares.set_day(s, fid, date(2026, 11, 3), 5, ["Ibuprofen"], "worst night")
        with pytest.raises(ValueError, match="before"):
            flares.set_day(s, fid, date(2026, 11, 1), 2, [], None)
    with session_scope() as s:
        flare = flares.load_flare(s, fid)
        assert flares.peak(flare) == 5
        assert len(flares.flare_dates(flare, date(2026, 11, 5))) == 4
        flares.set_day(s, fid, date(2026, 11, 4), 0, [], None)  # symptom-free after all
        flares.end_flare(s, fid, date(2026, 11, 3))
    with session_scope() as s:
        flare = flares.load_flare(s, fid)
        assert flare.ended_on == date(2026, 11, 3) and len(flare.days) == 2
        assert flares.active_flare(s) is None
        assert len(flares.flare_dates(flare, date(2026, 11, 20))) == 2
        # Moving the start later drops the days before it.
        flares.save_flare(
            s, fid, started_at=start + timedelta(days=1), tz="America/New_York", joints=[], note=""
        )
        assert [d.day for d in flares.load_flare(s, fid).days] == [date(2026, 11, 3)]
        flares.delete_flare(s, fid)
    with session_scope() as s:
        assert flares.list_flares(s) == []


# --------------------------------------------------------------------------- triggers


def test_food_factors_rate_the_windows(db):
    with session_scope() as s:
        liver = food(s, "Liver")
        lentils = food(s, "Lentils")
        beer = food(s, "Beer")
        cola = food(s, "Cola", sugar=10)
        profile(s, liver, 300, 300)
        profile(s, lentils, 80)
        profile(s, beer, 10, 0, 3.9)
        profile(s, cola, 0, sweet=True)
        entry(s, liver, 400, NOW - timedelta(hours=30))  # 1,200 mg animal
        entry(s, lentils, 300, NOW - timedelta(hours=5))  # 240 mg plant → 120 weighted
        entry(s, beer, 355, NOW - timedelta(hours=3))  # ~1 drink
        entry(s, beer, 355, NOW - timedelta(hours=30))  # outside the 24 h drink window
        entry(s, cola, 355, NOW - timedelta(hours=2))  # 35.5 g sweet sugar
        entry(s, None, 300, NOW - timedelta(hours=1), desc="Mystery soup")
        rep = triggers.report(s, NY, NOW)
    purines = rep.factor("purines")
    assert purines.value == pytest.approx(1200 + 240 + 71, abs=1)
    assert purines.level is Level.ELEVATED and "1 entry not estimated" in purines.detail
    alcohol = rep.factor("alcohol")
    assert alcohol.value == pytest.approx(355 * 0.039 / 14, abs=0.01)
    assert alcohol.level is Level.LOW  # just under one drink
    assert rep.factor("sweet").level is Level.ELEVATED
    assert rep.level is Level.HIGH  # two factors elevated
    assert rep.unestimated == 1 and rep.sources[0].description == "Liver"
    assert rep.factor("weight").level is None


def test_nothing_logged_is_unknown(db):
    with session_scope() as s:
        rep = triggers.report(s, NY, NOW)
    assert not rep.logged and rep.level is Level.LOW
    assert rep.factor("purines").level is None


def test_hard_exercise_is_elevated(db):
    with session_scope() as s:
        s.add(
            Activity(
                garmin_activity_id=1,
                activity_type="cycling",
                name="Century ride",
                start_at=NOW - timedelta(hours=20),
                duration_s=5 * 3600,
                kcal=3000,
            )
        )
        s.flush()
        f = triggers.exercise_factor(s, NY, NOW)
    assert f.level is Level.ELEVATED and "Century ride" in f.text


def test_lookback_compares_with_ordinary_days(db):
    start = datetime(2026, 11, 3, 3, 0, tzinfo=UTC)
    with session_scope() as s:
        shrimp = food(s, "Shrimp")
        rice = food(s, "Rice")
        profile(s, shrimp, 200, 200)
        profile(s, rice, 25)
        for k in range(1, 40):
            entry(s, rice, 300, start - timedelta(days=k, hours=6))
        entry(s, shrimp, 600, start - timedelta(hours=10))
        flare = flares.save_flare(
            s,
            None,
            started_at=start,
            tz="America/New_York",
            joints=["Big toe"],
            note="",
            severity=4,
        )
        lb = triggers.lookback(s, NY, flare, [flare])
    assert lb.samples == len(triggers.TYPICAL_DAYS)
    assert lb.typical["purines"] == pytest.approx(150)  # two rice portions in 48 h
    assert lb.report.factor("purines").value == pytest.approx(1200 + 75)
    assert lb.report.factor("purines").level is Level.ELEVATED


# --------------------------------------------------------------------------- web


@pytest.fixture
def app():
    from gwc.web.app import create_app

    return create_app()


def test_gout_page_and_today_card_render(app, db):
    from gwc.db.types import utcnow
    from gwc.web.pages import gout, today

    at = utcnow()
    with session_scope() as s:
        steak = food(s, "Steak")
        profile(s, steak, 120, 120)
        e = entry(s, steak, 300, at - timedelta(hours=2))
        entry(s, None, 300, at - timedelta(hours=1), desc="Pho")
        flare = flares.save_flare(
            s,
            None,
            started_at=at - timedelta(days=1),
            tz=str(NY),
            joints=["Big toe"],
            note="",
            severity=3,
        )
        past = flares.save_flare(
            s,
            None,
            started_at=at - timedelta(days=30),
            tz=str(NY),
            joints=["Ankle"],
            note="",
            severity=2,
        )
        flares.end_flare(s, past.id, flares.start_day(past) + timedelta(days=2))
        eid, fid = e.id, flare.id

    panel, active, history = gout.render(0)
    assert any("Estimate now" in str(c) for c in panel)
    assert "Current flare" in str(active) and "It's over" in str(active)
    assert "Past flares" in str(history) and "Ankle" in str(history)

    assert gout.save_profile(1, eid, 100, 200, 0, False, 0)[2].startswith("Meat")
    assert gout.save_profile(1, eid, 400, 300, 0, False, 0)[0] is False
    with session_scope() as s:
        assert s.query(GoutProfile).filter_by(food_id=steak.id).one().source == "manual"

    key = {"flare": fid, "day": utcnow().astimezone(NY).date().isoformat()}
    assert gout.save_day(1, key, "4", [], "", 0)[0] is False

    card = today.render_day(None, 0)[-1]
    text = str(card)
    assert "Gout triggers" in text and "how bad is it today" in text
