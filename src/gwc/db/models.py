"""Database schema.

All quantities are stored in metric units (kg, g, mg, m, kcal) and all
timestamps as UTC. Conversion to display units happens in the UI layer.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    Enum,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from gwc.db.types import UTCDateTime, utcnow

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {datetime: UTCDateTime(), dict[str, Any]: JSON, Any: JSON}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


def _enum(cls: type[StrEnum]) -> Enum:
    return Enum(cls, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e])


# --------------------------------------------------------------------------- Garmin


class GarminRaw(Base):
    """Raw Garmin API payloads, kept so data can be re-parsed without re-fetching."""

    __tablename__ = "garmin_raw"
    __table_args__ = (UniqueConstraint("endpoint", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow)
    payload: Mapped[Any]


class BodyComp(TimestampMixin, Base):
    __tablename__ = "body_comp"

    id: Mapped[int] = mapped_column(primary_key=True)
    measured_at: Mapped[datetime] = mapped_column(unique=True)
    garmin_sample_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    weight_kg: Mapped[float] = mapped_column(Float)
    body_fat_pct: Mapped[float | None] = mapped_column(Float)
    muscle_mass_kg: Mapped[float | None] = mapped_column(Float)
    bone_mass_kg: Mapped[float | None] = mapped_column(Float)
    body_water_pct: Mapped[float | None] = mapped_column(Float)
    bmi: Mapped[float | None] = mapped_column(Float)


class DailySummary(TimestampMixin, Base):
    """Garmin per-calendar-day totals."""

    __tablename__ = "daily_summary"

    id: Mapped[int] = mapped_column(primary_key=True)
    calendar_date: Mapped[date] = mapped_column(Date, unique=True)
    total_kcal: Mapped[float | None] = mapped_column(Float)
    active_kcal: Mapped[float | None] = mapped_column(Float)
    resting_kcal: Mapped[float | None] = mapped_column(Float)
    steps: Mapped[int | None] = mapped_column(Integer)
    resting_hr: Mapped[int | None] = mapped_column(Integer)
    avg_stress: Mapped[int | None] = mapped_column(Integer)
    body_battery_high: Mapped[int | None] = mapped_column(Integer)
    body_battery_low: Mapped[int | None] = mapped_column(Integer)
    is_provisional: Mapped[bool] = mapped_column(Boolean, default=True)


class Sleep(TimestampMixin, Base):
    __tablename__ = "sleep"

    id: Mapped[int] = mapped_column(primary_key=True)
    garmin_sleep_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    calendar_date: Mapped[date] = mapped_column(
        Date, index=True
    )  # Garmin labels sleep by wake date
    start_at: Mapped[datetime] = mapped_column(unique=True)
    end_at: Mapped[datetime]
    is_nap: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_s: Mapped[int | None] = mapped_column(Integer)
    deep_s: Mapped[int | None] = mapped_column(Integer)
    light_s: Mapped[int | None] = mapped_column(Integer)
    rem_s: Mapped[int | None] = mapped_column(Integer)
    awake_s: Mapped[int | None] = mapped_column(Integer)
    sleep_score: Mapped[int | None] = mapped_column(Integer)
    avg_overnight_hrv: Mapped[float | None] = mapped_column(Float)


class Activity(TimestampMixin, Base):
    __tablename__ = "activity"

    id: Mapped[int] = mapped_column(primary_key=True)
    garmin_activity_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    activity_type: Mapped[str] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(255))
    start_at: Mapped[datetime] = mapped_column(index=True)
    duration_s: Mapped[float | None] = mapped_column(Float)
    distance_m: Mapped[float | None] = mapped_column(Float)
    kcal: Mapped[float | None] = mapped_column(Float)
    avg_hr: Mapped[int | None] = mapped_column(Integer)


class SyncKind(StrEnum):
    BACKFILL = "backfill"
    SCHEDULED = "scheduled"
    CATCHUP = "catchup"
    MANUAL = "manual"


class SyncStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"
    AUTH_REQUIRED = "auth_required"


class SyncRun(Base):
    __tablename__ = "sync_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[SyncKind] = mapped_column(_enum(SyncKind))
    status: Mapped[SyncStatus] = mapped_column(_enum(SyncStatus), default=SyncStatus.RUNNING)
    started_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    finished_at: Mapped[datetime | None]
    window_start: Mapped[date | None] = mapped_column(Date)
    window_end: Mapped[date | None] = mapped_column(Date)
    requests_made: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class GarminRequest(Base):
    """One row per request sent to Garmin, for the rolling request/login budgets."""

    __tablename__ = "garmin_request"

    id: Mapped[int] = mapped_column(primary_key=True)
    sent_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    endpoint: Mapped[str] = mapped_column(String(64))
    is_login: Mapped[bool] = mapped_column(Boolean, default=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    sync_run_id: Mapped[int | None] = mapped_column(ForeignKey("sync_run.id"))


class GarminLockout(Base):
    """Self-imposed pause after Garmin rate-limits us."""

    __tablename__ = "garmin_lockout"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    until: Mapped[datetime] = mapped_column(index=True)
    strike: Mapped[int] = mapped_column(Integer, default=1)
    reason: Mapped[str] = mapped_column(String(255))


# --------------------------------------------------------------------------- Nutrition


class FoodSource(StrEnum):
    USDA = "usda"
    CUSTOM = "custom"
    RECIPE = "recipe"


class NutrientsPer100gMixin:
    kcal: Mapped[float | None] = mapped_column(Float)
    protein_g: Mapped[float | None] = mapped_column(Float)
    carbs_g: Mapped[float | None] = mapped_column(Float)
    fat_g: Mapped[float | None] = mapped_column(Float)
    fiber_g: Mapped[float | None] = mapped_column(Float)
    sugar_g: Mapped[float | None] = mapped_column(Float)
    sodium_mg: Mapped[float | None] = mapped_column(Float)


class Food(TimestampMixin, NutrientsPer100gMixin, Base):
    """A food with nutrients per 100 g. Recipes are foods whose nutrients derive from items."""

    __tablename__ = "food"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[FoodSource] = mapped_column(_enum(FoodSource))
    fdc_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    brand: Mapped[str | None] = mapped_column(String(255))
    data_type: Mapped[str | None] = mapped_column(String(32))  # USDA dataType
    recipe_yield_g: Mapped[float | None] = mapped_column(Float)
    # Where the nutrition came from, e.g. a restaurant's nutrition page.
    source_url: Mapped[str | None] = mapped_column(String(1024))
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)

    servings: Mapped[list["FoodServing"]] = relationship(
        back_populates="food", cascade="all, delete-orphan", order_by="FoodServing.id"
    )
    recipe_items: Mapped[list["RecipeItem"]] = relationship(
        back_populates="recipe",
        cascade="all, delete-orphan",
        foreign_keys="RecipeItem.recipe_id",
    )
    categories: Mapped[list["FoodCategory"]] = relationship(
        back_populates="food", cascade="all, delete-orphan"
    )

    @property
    def category_names(self) -> list[str]:
        return sorted((c.name for c in self.categories), key=str.casefold)


class FoodCategory(Base):
    """A user-chosen label such as "snack" or "restaurant"; a food can have several."""

    __tablename__ = "food_category"

    food_id: Mapped[int] = mapped_column(
        ForeignKey("food.id", ondelete="CASCADE"), primary_key=True
    )
    name: Mapped[str] = mapped_column(String(64), primary_key=True, index=True)

    food: Mapped[Food] = relationship(back_populates="categories")


class FoodServing(Base):
    __tablename__ = "food_serving"

    id: Mapped[int] = mapped_column(primary_key=True)
    food_id: Mapped[int] = mapped_column(ForeignKey("food.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(128))
    grams: Mapped[float] = mapped_column(Float)

    food: Mapped[Food] = relationship(back_populates="servings")


class FoodBarcode(TimestampMixin, Base):
    """A scanned product barcode and the food it was confirmed as, so a rescan needs no lookup."""

    __tablename__ = "food_barcode"

    gtin: Mapped[str] = mapped_column(String(14), primary_key=True)  # zero-padded GTIN-14
    food_id: Mapped[int] = mapped_column(ForeignKey("food.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(16))  # usda | off | label | manual

    food: Mapped[Food] = relationship()


class RecipeItem(Base):
    __tablename__ = "recipe_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("food.id", ondelete="CASCADE"), index=True)
    ingredient_id: Mapped[int] = mapped_column(ForeignKey("food.id"))
    grams: Mapped[float] = mapped_column(Float)

    recipe: Mapped[Food] = relationship(back_populates="recipe_items", foreign_keys=[recipe_id])
    ingredient: Mapped[Food] = relationship(foreign_keys=[ingredient_id])


class LogSource(StrEnum):
    SEARCH = "search"
    CUSTOM = "custom"
    RECIPE = "recipe"
    AI = "ai"


class FoodLog(TimestampMixin, Base):
    """A logged food. Nutrient columns are totals for this entry, snapshotted at log time."""

    __tablename__ = "food_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    eaten_at: Mapped[datetime] = mapped_column(index=True)
    tz: Mapped[str] = mapped_column(String(64))
    food_id: Mapped[int | None] = mapped_column(ForeignKey("food.id", ondelete="SET NULL"))
    description: Mapped[str] = mapped_column(String(255))
    grams: Mapped[float] = mapped_column(Float)
    # How the amount was entered, e.g. 2 × "1 large" — for display; `grams` is authoritative.
    amount: Mapped[float | None] = mapped_column(Float)
    unit_label: Mapped[str | None] = mapped_column(String(128))
    kcal: Mapped[float] = mapped_column(Float, default=0)
    protein_g: Mapped[float] = mapped_column(Float, default=0)
    carbs_g: Mapped[float] = mapped_column(Float, default=0)
    fat_g: Mapped[float] = mapped_column(Float, default=0)
    fiber_g: Mapped[float] = mapped_column(Float, default=0)
    sugar_g: Mapped[float] = mapped_column(Float, default=0)
    sodium_mg: Mapped[float] = mapped_column(Float, default=0)
    food_day_override: Mapped[date | None] = mapped_column(Date)
    source: Mapped[LogSource] = mapped_column(_enum(LogSource))
    note: Mapped[str | None] = mapped_column(Text)

    food: Mapped[Food | None] = relationship()


class FoodDayStatus(TimestampMixin, Base):
    """Per-food-day flags set by the user."""

    __tablename__ = "food_day_status"

    food_day: Mapped[date] = mapped_column(Date, primary_key=True)
    is_incomplete: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(Text)


class DailyTarget(TimestampMixin, Base):
    """Targets in effect for a food day, fixed once the day starts."""

    __tablename__ = "daily_target"

    food_day: Mapped[date] = mapped_column(Date, primary_key=True)
    kcal: Mapped[float] = mapped_column(Float)
    protein_g: Mapped[float] = mapped_column(Float)
    carbs_g: Mapped[float] = mapped_column(Float)
    fat_g: Mapped[float] = mapped_column(Float)
    fiber_g: Mapped[float] = mapped_column(Float)
    sugar_g: Mapped[float] = mapped_column(Float)
    sodium_mg: Mapped[float] = mapped_column(Float)
    tdee_method: Mapped[str] = mapped_column(String(32))
    tdee_kcal: Mapped[float] = mapped_column(Float)
    details: Mapped[dict[str, Any] | None]


# --------------------------------------------------------------------------- App state


class Setting(TimestampMixin, Base):
    """User preferences as JSON values, edited on the Settings page."""

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any]


class CoachConversation(TimestampMixin, Base):
    __tablename__ = "coach_conversation"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))

    messages: Mapped[list["CoachMessage"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="CoachMessage.id",
    )


class CoachMessage(Base):
    __tablename__ = "coach_message"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("coach_conversation.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    conversation: Mapped[CoachConversation] = relationship(back_populates="messages")


class CoachSummary(Base):
    __tablename__ = "coach_summary"

    id: Mapped[int] = mapped_column(primary_key=True)
    week_start: Mapped[date] = mapped_column(Date, unique=True)
    content: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
