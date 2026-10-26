from types import SimpleNamespace as NS

import dash
import pytest

from gwc.web.app import create_app
from gwc.web.nav import NAV_ITEMS, is_active


@pytest.fixture(scope="module")
def app():
    return create_app()


def test_every_nav_item_has_a_page(app):
    paths = {page["path"] for page in dash.page_registry.values()}
    assert {item.href for item in NAV_ITEMS} <= paths


def test_page_layouts_render(app):
    for page in dash.page_registry.values():
        layout = page["layout"]
        component = layout() if callable(layout) else layout
        assert component is not None, page["path"]


@pytest.mark.parametrize("path", [item.href for item in NAV_ITEMS])
def test_routes_serve_html(app, path):
    resp = app.server.test_client().get(path)
    assert resp.status_code == 200
    assert b"Weight Coach" in resp.data


def test_dash_layout_endpoint(app):
    resp = app.server.test_client().get("/_dash-layout")
    assert resp.status_code == 200
    assert b"MantineProvider" in resp.data


@pytest.mark.parametrize(
    ("href", "pathname", "expected"),
    [
        ("/", "/", True),
        ("/", "", True),
        ("/", None, True),
        ("/", "/add", False),
        ("/add", "/add", True),
        ("/add", "/add/", True),
        ("/foods", "/foods/12", True),
        ("/foods", "/foodstuff", False),
    ],
)
def test_is_active(href, pathname, expected):
    assert is_active(href, pathname) is expected


def test_bottom_nav_highlight():
    from gwc.web.layout import _highlight_bottom_nav

    ids = [{"type": "bottom-nav", "href": i.href} for i in NAV_ITEMS if i.mobile_primary]
    colors = _highlight_bottom_nav("/projection", ids)
    assert colors == ["dimmed", "dimmed", "brand", "dimmed"]


def test_settings_refresh_without_garmin(app):
    from gwc.db.migrate import upgrade
    from gwc.web.pages import settings

    upgrade()
    out = settings.refresh(None, 0)
    assert len(out) == 21
    badge_text, _color, conn_text, creds_display, connect_label, connect_disabled = out[:6]
    assert badge_text == "Not connected"
    assert creds_display == "block"  # no credentials in the test environment
    assert connect_label == "Connect" and connect_disabled is True
    sync_disabled, hint = out[7], out[8]
    assert sync_disabled is True
    assert hint == "Connect Garmin first"


def test_header_badge_without_garmin(app):
    from gwc.db.migrate import upgrade
    from gwc.web.layout import _update_sync_badge

    upgrade()
    assert _update_sync_badge(None, "/") == ("Garmin: not connected", "gray")


def test_serve_warns_when_garmin_disconnected(caplog):
    import logging

    from gwc.cli import _warn_if_garmin_disconnected

    with caplog.at_level(logging.WARNING, logger="gwc"):
        _warn_if_garmin_disconnected("127.0.0.1", 8060)
    assert "http://127.0.0.1:8060/settings" in caplog.text


@pytest.fixture
def seeded(app, isolated_settings):
    from datetime import UTC, datetime, timedelta

    from gwc.analysis.food_day import current_food_day
    from gwc.db.migrate import upgrade
    from gwc.db.models import Activity, BodyComp, DailySummary, FoodLog, LogSource, Sleep
    from gwc.db.session import session_scope
    from gwc.db.types import utcnow

    upgrade()
    tz = isolated_settings.tz
    with session_scope() as s:
        today = current_food_day(s, tz, utcnow())
        for i in range(21, 0, -1):
            d = today - timedelta(days=i)
            noon = datetime(d.year, d.month, d.day, 12, tzinfo=tz).astimezone(UTC)
            s.add(
                DailySummary(
                    calendar_date=d,
                    total_kcal=3100,
                    resting_kcal=2300,
                    steps=9000,
                    is_provisional=i == 1,
                )
            )
            s.add(BodyComp(measured_at=noon, weight_kg=120 - 0.05 * (21 - i), body_fat_pct=30))
            s.add(
                Sleep(
                    calendar_date=d,
                    start_at=noon - timedelta(hours=8),
                    end_at=noon - timedelta(hours=1),
                    duration_s=7 * 3600,
                )
            )
            if i % 3:
                s.add(
                    FoodLog(
                        eaten_at=noon + timedelta(hours=2),
                        tz=str(tz),
                        description="x",
                        grams=100,
                        kcal=2600,
                        source=LogSource.CUSTOM,
                    )
                )
        s.add(
            Activity(
                garmin_activity_id=1,
                activity_type="walking",
                start_at=utcnow() - timedelta(days=2),
                duration_s=1800,
                kcal=200,
            )
        )


def test_analysis_callbacks_render(seeded):
    import plotly.graph_objects as go

    from gwc.web.pages import projection, settings, today, trends

    out = projection.render("light")
    fig = out[9]
    assert isinstance(fig, go.Figure)
    names = {t.name for t in fig.data if t.name}
    assert {"Trend", "At your target", "As eaten · Garmin burn"} <= names
    assert out[11] and out[12]  # burn estimates, last 7 days

    for key in ("30", "all"):
        figs = trends.render(key, "dark")
        assert all(isinstance(f, go.Figure) and f.data for f in figs[:5])

    label, _goal, summary, placeholders = settings.load_plan()
    assert label.endswith("(lb)") and summary
    assert all(p.startswith("Suggested") for p in placeholders)

    rendered = today.render_day(None, 0, 0)
    assert "kcal" in rendered[4]  # where the calorie target came from
    assert rendered[5] == "block"  # no goal set → maintenance hint


def test_analysis_callbacks_render_empty(app, isolated_settings):
    from gwc.db.migrate import upgrade
    from gwc.web.pages import projection, trends

    upgrade()
    out = projection.render(None)
    assert out[1] == "—" and out[10] is None
    assert len(trends.render("90", None)) == 6


def test_coach_callbacks(app, isolated_settings):
    from datetime import date

    from gwc.coach import llm
    from gwc.coach.llm import LLMResult
    from gwc.db.migrate import upgrade
    from gwc.db.models import CoachConversation, CoachMessage, CoachSummary
    from gwc.db.session import session_scope
    from gwc.web.components import coach_settings
    from gwc.web.pages import coach

    upgrade()
    data, value, bubbles, suggestions = coach.render_chat(None, 0)
    assert data == [] and value is None and suggestions == "flex"

    with session_scope() as s:
        conv = CoachConversation(title="Hi")
        s.add(conv)
        s.flush()
        s.add(CoachMessage(conversation_id=conv.id, role="user", content="Hi"))
        s.add(CoachMessage(conversation_id=conv.id, role="assistant", content="**Hello**"))
        s.add(CoachSummary(week_start=date(2026, 10, 12), content="Good week"))
        conv_id = conv.id
    data, value, bubbles, suggestions = coach.render_chat(conv_id, 1)
    assert value == str(conv_id) and len(bubbles) == 2 and suggestions == "none"
    accordion, schedule = coach.render_summaries(0)
    assert "Monday" in schedule and accordion.value == "2026-10-12"

    loaded = coach_settings.load_coach(0)
    assert loaded[0] == "subscription" and loaded[1] == ["opus", "opus", "opus"]
    assert coach_settings.show_custom(["opus", "custom", "haiku"]) == ["none", "block", "none"]
    bad = coach_settings.save_coach(
        1,
        "subscription",
        ["opus", "custom", "opus"],
        ["", " ", ""],
        ["low", "medium", "high"],
        "",
        True,
        "2",
    )
    assert "Enter a model ID" in bad.children
    ok = coach_settings.save_coach(
        1,
        "subscription",
        ["sonnet", "custom", "opus"],
        ["", "claude-haiku-5-5", ""],
        ["low", "medium", "high"],
        "haiku",
        True,
        "2",
    )
    assert ok.children == "Saved."
    loaded = coach_settings.load_coach(0)
    assert loaded[1] == ["sonnet", "custom", "opus"] and loaded[2][1] == "claude-haiku-5-5"
    assert loaded[4] == "haiku" and loaded[6] == "2"

    llm.set_backend_override(
        type("B", (), {"run": lambda self, req: LLMResult(text="OK", model="m")})()
    )
    assert coach_settings.test_coach(1).title == "Connected"


def test_ai_review_callbacks(app, isolated_settings, monkeypatch):
    from datetime import UTC, datetime

    from gwc.db.migrate import upgrade
    from gwc.db.models import Food, FoodLog
    from gwc.db.session import session_scope
    from gwc.web.components import ai_log
    from gwc.web.components.common import from_picker, to_picker

    upgrade()
    per100 = {
        "kcal": 200,
        "protein_g": 10,
        "carbs_g": 20,
        "fat_g": 5,
        "fiber_g": 1,
        "sugar_g": 2,
        "sodium_mg": 100,
    }
    when = datetime(2026, 10, 20, 12, tzinfo=UTC).isoformat()
    base = {
        "description": "thing",
        "amount_label": "1 bowl",
        "eaten_at": when,
        "food_id": None,
        "hit": None,
        "error": None,
    }
    rest = {
        "chain": "Chipotle",
        "item": "Chicken Burrito Bowl, no cheese",
        "source_kind": "official",
        "source_url": "https://www.chipotle.com/nutrition-calculator",
        "serving_label": "1 bowl",
        "serving_grams": 500,
    }
    data = {
        "items": [
            {**base, "key": 0, "source": "usda", "name": "Rice", "grams": 300, "per100": per100},
            {
                **base,
                "key": 1,
                "source": "estimate",
                "name": "Stew",
                "grams": 300,
                "per100": per100,
                "editable": True,
            },
            {
                **base,
                "key": 2,
                "source": "estimate",
                "name": "Mystery Wrap",
                "grams": 0,
                "per100": None,
                "editable": True,
                "restaurant": {**rest, "item": "Mystery Wrap", "source_kind": "not_found"},
            },
            {
                **base,
                "key": 3,
                "source": "restaurant",
                "name": rest["item"],
                "grams": 500,
                "per100": per100,
                "restaurant": rest,
            },
            {**base, "key": 4, "source": "usda", "name": "?", "grams": 10, "error": "Bad id."},
        ],
        "message": "Check the stew portion.",
    }
    review, display, when_display, day, clock = ai_log.render_review(data)
    assert display == "flex" and len(review) == 6  # message + 5 cards
    assert when_display == "block"
    assert (day, clock) == to_picker(datetime.fromisoformat(when))
    assert "chipotle.com" in str(review[4])  # the source link

    gram_ids = [{"type": "ai-grams", "index": i} for i in (0, 3)]
    include_ids = [{"type": "ai-include", "index": i} for i in range(5)]
    nut_ids = [
        {"type": "ai-nut", "index": i, "key": k}
        for i in (1, 2)
        for k in ("kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg")
    ]
    # Stew keeps Claude's estimate (600 kcal for 300 g); the wrap is typed in.
    nuts = [600, 30, 60, 15, 3, 6, 300] + [450, 20, 50, 18, 4, 3, 900]
    included = [True, True, True, True, False]
    lines, total, label, disabled = ai_log.update_totals(
        [150, 500], included, nuts, gram_ids, include_ids, nut_ids, data
    )
    assert lines[0].startswith("300 kcal") and lines[1].startswith("1,000 kcal")
    assert total == "2,350 kcal total" and label == "Log 4 items" and not disabled
    none = [False] * 5
    assert ai_log.update_totals([150, 500], none, nuts, gram_ids, include_ids, nut_ids, data)[3]

    monkeypatch.setattr(ai_log, "ctx", type("Ctx", (), {"triggered_id": "ai-confirm"})())
    # Logged at the time picked in the review, not the time Claude read.
    out = ai_log.confirm(
        1,
        None,
        [150, 500],
        included,
        nuts,
        gram_ids,
        include_ids,
        nut_ids,
        data,
        "2026-10-18",
        "07:45",
        0,
    )
    assert out[0] is None and out[2] is None and out[5] == 1  # text and photo cleared
    assert out[3] == (
        "Logged 4 items from your description. Saved 2 restaurant items to your foods."
    )
    with session_scope() as s:
        logged = {r.description: r for r in s.query(FoodLog)}
        assert logged["Rice"].grams == 150 and logged["Rice"].kcal == 300
        picked = from_picker("2026-10-18", "07:45")
        assert all(r.eaten_at == picked for r in logged.values())
        assert logged["Stew"].kcal == 600  # an unedited estimate: logged, not saved
        assert logged["Mystery Wrap"].kcal == pytest.approx(450)
        assert logged[rest["item"]].kcal == pytest.approx(1000)
        saved = {f.name: f for f in s.query(Food).filter(Food.brand == "Chipotle")}
        assert set(saved) == {rest["item"], "Mystery Wrap"}
        assert saved[rest["item"]].source_url == rest["source_url"]
        assert saved["Mystery Wrap"].source_url is None


def test_ai_parse_needs_text_or_photo(app, monkeypatch):
    from gwc.web.components import ai_log

    started = []
    jobs = type("Jobs", (), {"start": lambda self, kind, fn: started.append(kind) or NS(id="j")})
    monkeypatch.setattr(ai_log, "get_jobs", lambda: jobs())
    assert "add a photo" in str(ai_log.start_parse(1, "  ", None)[4]) and not started
    out = ai_log.start_parse(1, "", {"image": "data:image/jpeg;base64,/9j/AAAA"})
    assert out[0] == "j" and out[3] == "Looking at your photo…" and started == ["parse"]
