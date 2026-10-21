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
    assert colors == ["dimmed", "dimmed", "teal", "dimmed"]


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
    from gwc.db.models import FoodLog
    from gwc.db.session import session_scope
    from gwc.web.components import ai_log

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
    data = {
        "items": [
            {
                **base,
                "key": 0,
                "source": "estimate",
                "name": "Stew",
                "grams": 300,
                "per100": per100,
            },
            {
                **base,
                "key": 1,
                "source": "usda",
                "name": "?",
                "grams": 10,
                "per100": None,
                "error": "No matching food.",
            },
        ],
        "message": "Check the stew portion.",
    }
    review, display = ai_log.render_review(data)
    assert display == "flex" and len(review) == 3  # message + 2 cards
    ids = [{"type": "ai-grams", "index": 0}]
    lines, total, label, disabled = ai_log.update_totals([150], [True], ids, data)
    assert lines[0].startswith("300 kcal") and total == "300 kcal total"
    assert label == "Log 1 item" and not disabled
    assert ai_log.update_totals([150], [False], ids, data)[3] is True

    monkeypatch.setattr(ai_log, "ctx", type("Ctx", (), {"triggered_id": "ai-confirm"})())
    out = ai_log.confirm(1, None, [150], [True], ids, data, 0)
    assert out[0] is None and out[2].startswith("Logged 1 item") and out[4] == 1
    with session_scope() as s:
        row = s.query(FoodLog).one()
        assert row.grams == 150 and row.kcal == 300
