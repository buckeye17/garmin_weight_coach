"""The "log a library food" dialog, plus small pieces shared by food lists.

A page opens it by writing {"food_id": ..., "nonce": ...} to the `logm-open`
store (with allow_duplicate). After a successful log it bumps `logm-done`
and shows a confirmation in `logm-result`. Editing the food's categories
saves right away and bumps `logm-cats-saved`. `logm-day` holds a past food
day being filled in, which moves the default time to midday on that day.
"""

from datetime import date

import dash_mantine_components as dmc
from dash import Input, Output, State, callback, dcc, no_update
from dash.exceptions import PreventUpdate

from gwc.coach.parse import default_eaten_at
from gwc.db.models import Food
from gwc.db.session import session_scope
from gwc.nutrition import foods, log
from gwc.nutrition.nutrients import NUTRIENTS, per100_of, scale
from gwc.web.components.common import (
    amount_input,
    amount_text,
    from_picker,
    icon,
    now,
    prefill_amount,
    to_picker,
    tz,
    unit_select_data,
    when_inputs,
)


def preview_line(totals: dict[str, float]) -> str:
    parts = [f"{totals['kcal']:,.0f} kcal"]
    for n in NUTRIENTS[1:]:
        parts.append(f"{n.label} {totals[n.key]:,.{1 if n.unit == 'g' else 0}f}{n.unit}")
    return " · ".join(parts)


def category_input(id: str) -> dmc.TagsInput:
    """Pick or type categories for a food; `data` is filled with `foods.category_choices`."""
    return dmc.TagsInput(
        id=id,
        label="Categories",
        placeholder="Snack, Restaurant, …",
        data=[],
        clearable=True,
        selectFirstOptionOnChange=True,  # Enter on "sna" picks "Snack" rather than adding "sna"
    )


ALL_CATEGORIES = "__all__"


def category_chips(categories: list[str], empty_hint: str) -> list:
    """'All' plus one chip per category, for a ChipGroup whose value is ALL_CATEGORIES or a name."""
    if not categories:
        return [dmc.Text(empty_hint, size="xs", c="dimmed")]
    return [
        dmc.Chip(name, value=value, size="xs")
        for name, value in [("All", ALL_CATEGORIES), *((c, c) for c in categories)]
    ]


def serving_summary(food: Food) -> str:
    """'1 large · 72 kcal': the food's usual portion, for lists."""
    if food.kcal is None:
        return "calories unknown"
    unit, amount = foods.default_unit(food)
    option = foods.resolve_unit(food, unit)
    kcal = food.kcal * amount * option.grams / 100
    return f"{log.unit_label(amount, option.label)} · {kcal:,.0f} kcal"


def log_modal(day: date | None = None) -> list:
    return [
        dcc.Store(id="logm-open"),
        dcc.Store(id="logm-day", data=day.isoformat() if day else None),
        dcc.Store(id="logm-food-id"),
        dcc.Store(id="logm-done", data=0),
        dcc.Store(id="logm-cats-saved", data=0),
        dmc.Modal(
            dmc.Stack(
                [
                    dmc.Text(id="logm-sub", size="sm", c="dimmed"),
                    dmc.Group(
                        [
                            amount_input("logm-amount"),
                            dmc.Select(
                                id="logm-unit", label="Unit", data=[], flex=1, allowDeselect=False
                            ),
                        ],
                        align="flex-end",
                        wrap="nowrap",
                    ),
                    when_inputs("logm"),
                    dmc.Paper(dmc.Text(id="logm-preview", size="sm"), p="xs", withBorder=True),
                    category_input("logm-cats"),
                    dmc.Group(
                        [
                            dmc.Switch(id="logm-fav", label="Favorite"),
                            dmc.Button(
                                "Log food", id="logm-submit", leftSection=icon("tabler:check")
                            ),
                        ],
                        justify="space-between",
                    ),
                    dmc.Text(id="logm-error", c="red", size="sm"),
                ]
            ),
            id="logm-modal",
            title="Log food",
            centered=True,
            size="md",
        ),
    ]


@callback(
    Output("logm-modal", "opened", allow_duplicate=True),
    Output("logm-modal", "title"),
    Output("logm-sub", "children"),
    Output("logm-food-id", "data"),
    Output("logm-unit", "data"),
    Output("logm-unit", "value"),
    Output("logm-amount", "value"),
    Output("logm-date", "value"),
    Output("logm-clock", "value"),
    Output("logm-fav", "checked"),
    Output("logm-cats", "data"),
    Output("logm-cats", "value"),
    Output("logm-error", "children", allow_duplicate=True),
    Input("logm-open", "data"),
    State("logm-day", "data"),
    prevent_initial_call=True,
)
def _open(request, day):
    if not request or not request.get("food_id"):
        raise PreventUpdate
    with session_scope() as s:
        food = foods.load_food(s, int(request["food_id"]))
        if food is None:
            raise PreventUpdate
        unit, amount = prefill_amount(s, food)
        kcal = f"{food.kcal:,.0f} kcal / 100 g" if food.kcal is not None else "calories unknown"
        sub = " · ".join(x for x in (food.brand, food.data_type, kcal) if x)
        return (
            True,
            food.name,
            sub,
            food.id,
            unit_select_data(food),
            unit,
            amount_text(amount),
            *to_picker(default_eaten_at(date.fromisoformat(day) if day else None, tz(), now())),
            food.is_favorite,
            foods.category_choices(s),
            food.category_names,
            "",
        )


@callback(
    Output("logm-cats-saved", "data"),
    Output("logm-cats", "value", allow_duplicate=True),
    Output("logm-cats", "data", allow_duplicate=True),
    Input("logm-cats", "value"),
    State("logm-food-id", "data"),
    State("logm-cats-saved", "data"),
    prevent_initial_call=True,
)
def _save_categories(names, food_id, saved):
    if not food_id:
        raise PreventUpdate
    with session_scope() as s:
        food = foods.load_food(s, int(food_id))
        if food is None or food.category_names == (names or []):
            raise PreventUpdate
        foods.set_categories(s, food.id, names or [])
        return (saved or 0) + 1, food.category_names, foods.category_choices(s)


@callback(
    Output("logm-preview", "children"),
    Input("logm-amount", "value"),
    Input("logm-unit", "value"),
    State("logm-food-id", "data"),
    prevent_initial_call=True,
)
def _preview(amount, unit, food_id):
    if not food_id or not amount or not unit:
        return "Enter an amount."
    try:
        amount = log.parse_amount(amount)
    except ValueError as e:
        return str(e)
    with session_scope() as s:
        food = foods.load_food(s, int(food_id))
        if food is None:
            return ""
        grams = foods.to_grams(food, amount, unit)
        return f"{grams:,.0f} g — " + preview_line(scale(per100_of(food), grams))


@callback(
    Output("logm-modal", "opened", allow_duplicate=True),
    Output("logm-done", "data"),
    Output("logm-result", "children"),
    Output("logm-result", "display"),
    Output("logm-error", "children", allow_duplicate=True),
    Input("logm-submit", "n_clicks"),
    State("logm-food-id", "data"),
    State("logm-amount", "value"),
    State("logm-unit", "value"),
    State("logm-date", "value"),
    State("logm-clock", "value"),
    State("logm-fav", "checked"),
    State("logm-done", "data"),
    prevent_initial_call=True,
)
def _submit(_clicks, food_id, amount, unit, day, clock, favorite, done):
    if not food_id:
        raise PreventUpdate
    try:
        amount = log.parse_amount(amount)
        with session_scope() as s:
            entry = log.log_food(
                s,
                food_id=int(food_id),
                amount=amount,
                unit=unit,
                eaten_at=from_picker(day, clock),
                tz=str(tz()),
            )
            foods.set_favorite(s, int(food_id), bool(favorite))
            message = f"Logged {entry.unit_label} {entry.description} ({entry.kcal:,.0f} kcal)."
    except ValueError as e:
        return no_update, no_update, no_update, no_update, str(e)
    return False, (done or 0) + 1, message, "block", ""
