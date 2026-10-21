"""Small helpers shared by pages."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import dash_mantine_components as dmc
from dash import ctx
from dash_iconify import DashIconify
from sqlalchemy import select
from sqlalchemy.orm import Session

from gwc.analysis.units import food_unit
from gwc.config import get_settings
from gwc.db.models import Food, FoodLog
from gwc.db.types import utcnow
from gwc.nutrition.foods import default_unit, unit_options
from gwc.nutrition.log import entry_unit


def icon(name: str, size: int = 18) -> DashIconify:
    return DashIconify(icon=name, width=size, height=size)


def tz() -> ZoneInfo:
    return get_settings().tz


def now() -> datetime:
    return utcnow()


def when_inputs(prefix: str, label: str = "Eaten at") -> dmc.Group:
    """Date picker + native time input; ids `<prefix>-date` and `<prefix>-clock`."""
    return dmc.Group(
        [
            dmc.DatePickerInput(id=f"{prefix}-date", label=label, valueFormat="ddd MMM D", flex=1),
            dmc.TimeInput(id=f"{prefix}-clock", label="Time", w=130),
        ],
        wrap="nowrap",
        align="flex-end",
    )


def to_picker(dt: datetime) -> tuple[str, str]:
    local = dt.astimezone(tz())
    return local.date().isoformat(), local.strftime("%H:%M")


def from_picker(day: str | None, clock: str | None) -> datetime:
    """Combine the date and time inputs (local wall time) into an aware datetime."""
    if not day:
        return now()
    d = date.fromisoformat(day[:10])
    hour, minute = (int(x) for x in (clock or "12:00").split(":")[:2])
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz())


def fmt_clock(dt: datetime, food_day: date | None = None) -> str:
    """'1:30 AM', prefixed with the weekday when it isn't on the food day's date."""
    local = dt.astimezone(tz())
    clock = local.strftime("%I:%M %p").lstrip("0")
    if food_day is not None and local.date() != food_day:
        return f"{local:%a} {clock}"
    return clock


def unit_select_data(food: Food | None) -> list[dict[str, str]]:
    if food is None:
        return [{"value": "g", "label": "g"}, {"value": "oz", "label": "oz"}]
    return [{"value": o.value, "label": o.label} for o in unit_options(food)]


def prefill_amount(s: Session, food: Food) -> tuple[str, float]:
    """The amount last logged for this food, else one serving (or 100 g / 3.5 oz)."""
    last = s.scalars(
        select(FoodLog).where(FoodLog.food_id == food.id).order_by(FoodLog.eaten_at.desc()).limit(1)
    ).first()
    if last is not None:
        amount, unit = entry_unit(s, last)
        return unit, amount
    unit, amount = default_unit(food)
    if unit == "g" and food_unit() == "oz":
        return "oz", 3.5
    return unit, amount


def triggered_click() -> dict | None:
    """The pattern-matching id that was actually clicked, ignoring list re-renders."""
    trig = ctx.triggered[0] if ctx.triggered else None
    if not trig or not trig.get("value"):
        return None
    tid = ctx.triggered_id
    return tid if isinstance(tid, dict) else None


def card(children, **kwargs) -> dmc.Card:
    return dmc.Card(children, withBorder=True, **kwargs)
