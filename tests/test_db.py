from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select
from sqlalchemy.exc import StatementError

from gwc.db.migrate import upgrade
from gwc.db.models import Base, Food, FoodLog, FoodServing, FoodSource, LogSource, Setting
from gwc.db.session import get_engine, session_scope


@pytest.fixture
def migrated_db(isolated_settings):
    upgrade()
    return get_engine()


def test_migrations_match_models(migrated_db):
    with migrated_db.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_migrations_create_all_tables(migrated_db):
    tables = set(inspect(migrated_db).get_table_names())
    assert set(Base.metadata.tables) <= tables


def test_utc_datetime_roundtrip(migrated_db):
    eastern = timezone(timedelta(hours=-4))
    eaten = datetime(2026, 10, 21, 2, 30, tzinfo=eastern)
    with session_scope() as s:
        s.add(
            FoodLog(
                eaten_at=eaten,
                tz="America/New_York",
                description="Sourdough",
                grams=50,
                source=LogSource.SEARCH,
            )
        )
    with session_scope() as s:
        row = s.scalars(select(FoodLog)).one()
    assert row.eaten_at.tzinfo is UTC
    assert row.eaten_at == eaten
    assert row.eaten_at.hour == 6


def test_naive_datetime_rejected(migrated_db):
    with pytest.raises(StatementError), session_scope() as s:
        s.add(
            FoodLog(
                eaten_at=datetime(2026, 10, 21, 2, 30),
                tz="America/New_York",
                description="x",
                grams=1,
                source=LogSource.AI,
            )
        )


def test_json_setting_and_relationships(migrated_db):
    with session_scope() as s:
        s.add(Setting(key="units", value={"weight": "lb", "food": "g"}))
        food = Food(source=FoodSource.CUSTOM, name="Protein shake", kcal=380, protein_g=75)
        food.servings.append(FoodServing(label="1 scoop", grams=31))
        s.add(food)
    with session_scope() as s:
        assert s.get(Setting, "units").value == {"weight": "lb", "food": "g"}
        food = s.scalars(select(Food)).one()
        assert food.source is FoodSource.CUSTOM
        assert [sv.label for sv in food.servings] == ["1 scoop"]
        assert isinstance(food.created_at, datetime)


def test_upgrade_is_idempotent(migrated_db):
    upgrade()
    with session_scope() as s:
        s.add(Setting(key="goal_loss_per_month_kg", value=1.8))
    upgrade()
    with session_scope() as s:
        assert s.get(Setting, "goal_loss_per_month_kg").value == 1.8


def test_date_columns(migrated_db):
    from gwc.db.models import DailySummary

    with session_scope() as s:
        s.add(DailySummary(calendar_date=date(2026, 10, 20), total_kcal=2650))
    with session_scope() as s:
        row = s.scalars(select(DailySummary)).one()
    assert row.calendar_date == date(2026, 10, 20)
    assert row.is_provisional is True
