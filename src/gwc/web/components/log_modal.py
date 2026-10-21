"""The "log a food" dialog, shared by the Add food and Today pages.

A page opens it by writing {"food_id": ..., "nonce": ...} to the `logm-open`
store (with allow_duplicate). After a successful log it bumps `logm-done`
and shows a confirmation in `logm-result`.
"""

import dash_mantine_components as dmc
from dash import Input, Output, State, callback, dcc, no_update
from dash.exceptions import PreventUpdate

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


def log_modal() -> list:
    return [
        dcc.Store(id="logm-open"),
        dcc.Store(id="logm-food-id"),
        dcc.Store(id="logm-done", data=0),
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
    Output("logm-error", "children", allow_duplicate=True),
    Input("logm-open", "data"),
    prevent_initial_call=True,
)
def _open(request):
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
            *to_picker(now()),
            food.is_favorite,
            "",
        )


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
