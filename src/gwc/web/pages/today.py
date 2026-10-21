import time
from datetime import date, timedelta

import dash
import dash_mantine_components as dmc
from dash import ALL, Input, Output, State, callback, ctx, dcc, no_update
from dash.exceptions import PreventUpdate

from gwc.analysis.food_day import current_food_day, food_days_around
from gwc.analysis.targets import NO_GOAL, day_targets
from gwc.analysis.units import weight_unit
from gwc.db.models import FoodLog
from gwc.db.session import session_scope
from gwc.nutrition import foods, log
from gwc.web.components.analysis_text import flag_messages, plan_summary
from gwc.web.components.common import (
    fmt_clock,
    from_picker,
    icon,
    now,
    to_picker,
    triggered_click,
    tz,
    unit_select_data,
    when_inputs,
)
from gwc.web.components.log_modal import log_modal
from gwc.web.components.nutrient_bars import nutrient_bars
from gwc.web.format import fmt_day


def _edit_modal() -> dmc.Modal:
    return dmc.Modal(
        dmc.Stack(
            [
                dmc.Group(
                    [
                        dmc.NumberInput(
                            id="edit-amount",
                            label="Amount",
                            min=0,
                            step=0.25,
                            decimalScale=2,
                            w=110,
                            inputProps={"inputMode": "decimal"},
                        ),
                        dmc.Select(
                            id="edit-unit", label="Unit", data=[], flex=1, allowDeselect=False
                        ),
                    ],
                    align="flex-end",
                    wrap="nowrap",
                ),
                when_inputs("edit"),
                dmc.Select(
                    id="edit-day",
                    label="Counts toward",
                    description="Set by when you fell asleep; override if needed.",
                    data=[],
                    allowDeselect=False,
                ),
                dmc.TextInput(id="edit-note", label="Note"),
                dmc.Text(id="edit-error", c="red", size="sm"),
                dmc.Group(
                    [
                        dmc.Button("Delete", id="edit-delete", color="red", variant="subtle"),
                        dmc.Button("Save", id="edit-save"),
                    ],
                    justify="space-between",
                ),
            ]
        ),
        id="edit-modal",
        title="Edit entry",
        centered=True,
    )


def layout(**_kwargs):
    return dmc.Stack(
        [
            dmc.Group(
                [
                    dmc.ActionIcon(
                        icon("tabler:chevron-left"),
                        id="day-prev",
                        variant="subtle",
                        size="lg",
                        **{"aria-label": "Previous day"},
                    ),
                    dmc.Stack(
                        [
                            dmc.Title(id="day-title", order=3, ta="center"),
                            dmc.Text(id="day-sub", size="xs", c="dimmed", ta="center"),
                        ],
                        gap=0,
                        flex=1,
                    ),
                    dmc.ActionIcon(
                        icon("tabler:chevron-right"),
                        id="day-next",
                        variant="subtle",
                        size="lg",
                        **{"aria-label": "Next day"},
                    ),
                ],
                wrap="nowrap",
            ),
            dmc.Group(
                [
                    dmc.Button("Today", id="day-today", variant="default", size="xs"),
                    dmc.Anchor(
                        dmc.Button("Add food", size="xs", leftSection=icon("tabler:plus", 16)),
                        href="/add",
                    ),
                ],
                justify="center",
            ),
            dmc.Alert(
                id="logm-result",
                color="teal",
                variant="light",
                display="none",
                withCloseButton=True,
                icon=icon("tabler:circle-check"),
            ),
            dmc.SimpleGrid(
                [
                    dmc.Card(
                        dmc.Stack(
                            [
                                dmc.Group(
                                    [
                                        dmc.Title("Totals", order=5),
                                        dmc.Text(id="day-remaining", size="sm", fw=600),
                                    ],
                                    justify="space-between",
                                ),
                                dmc.Stack(id="day-bars", gap="sm"),
                                dmc.Text(id="day-target-note", size="xs", c="dimmed"),
                                dmc.Alert(
                                    id="targets-hint",
                                    color="gray",
                                    variant="light",
                                    display="none",
                                ),
                                dmc.Switch(
                                    id="day-incomplete",
                                    label="Not fully logged (leave out of averages)",
                                    size="sm",
                                ),
                            ],
                            gap="sm",
                        ),
                        withBorder=True,
                    ),
                    dmc.Stack(
                        [
                            dmc.Card(
                                dmc.Stack(
                                    [
                                        dmc.Title("Quick add", order=5),
                                        dmc.Group(id="quick-add", gap="xs"),
                                    ],
                                    gap="xs",
                                ),
                                withBorder=True,
                            ),
                            dmc.Card(
                                dmc.Stack(
                                    [
                                        dmc.Title("Entries", order=5),
                                        dmc.Stack(id="day-entries", gap="xs"),
                                    ],
                                    gap="xs",
                                ),
                                withBorder=True,
                            ),
                        ],
                        gap="md",
                    ),
                ],
                cols={"base": 1, "md": 2},
                spacing="md",
            ),
            dcc.Store(id="day-selected"),
            dcc.Store(id="edit-entry-id"),
            dcc.Store(id="edit-done", data=0),
            _edit_modal(),
            *log_modal(),
        ],
        gap="sm",
    )


def _entry_row(e: FoodLog, day: date) -> dmc.UnstyledButton:
    macros = f"P {e.protein_g:,.0f} · C {e.carbs_g:,.0f} · F {e.fat_g:,.0f}"
    amount = e.unit_label or f"{e.grams:,.0f} g"
    return dmc.UnstyledButton(
        dmc.Paper(
            dmc.Group(
                [
                    dmc.Text(fmt_clock(e.eaten_at, day), size="xs", c="dimmed", w=72),
                    dmc.Stack(
                        [
                            dmc.Text(e.description, size="sm", fw=500, lineClamp=1),
                            dmc.Text(f"{amount} · {macros}", size="xs", c="dimmed", lineClamp=1),
                        ],
                        gap=0,
                        flex=1,
                        miw=0,
                    ),
                    dmc.Text(f"{e.kcal:,.0f}", size="sm", fw=600),
                ],
                wrap="nowrap",
                gap="sm",
            ),
            p="xs",
            withBorder=True,
        ),
        id={"type": "entry-row", "index": e.id},
        w="100%",
    )


@callback(
    Output("day-selected", "data"),
    Input("day-prev", "n_clicks"),
    Input("day-next", "n_clicks"),
    Input("day-today", "n_clicks"),
    State("day-selected", "data"),
    prevent_initial_call=True,
)
def change_day(_prev, _next, _today, selected):
    if ctx.triggered_id == "day-today":
        return None
    with session_scope() as s:
        base = date.fromisoformat(selected) if selected else current_food_day(s, tz(), now())
    step = -1 if ctx.triggered_id == "day-prev" else 1
    return (base + timedelta(days=step)).isoformat()


@callback(
    Output("day-title", "children"),
    Output("day-sub", "children"),
    Output("day-bars", "children"),
    Output("day-remaining", "children"),
    Output("day-target-note", "children"),
    Output("targets-hint", "display"),
    Output("targets-hint", "children"),
    Output("day-incomplete", "checked"),
    Output("day-entries", "children"),
    Output("quick-add", "children"),
    Output("day-next", "disabled"),
    Input("day-selected", "data"),
    Input("logm-done", "data"),
    Input("edit-done", "data"),
)
def render_day(selected, _done, _edited):
    zone = tz()
    with session_scope() as s:
        today = current_food_day(s, zone, now())
        day = date.fromisoformat(selected) if selected else today
        fd = food_days_around(s, zone, day)
        start, (end, from_sleep) = fd.start_of(day), fd.end_of(day)
        entries = log.entries_for_day(s, day, fd)
        totals = log.totals(entries)
        plan = day_targets(s, zone, now(), day)
        targets = plan.values
        incomplete = log.is_incomplete(s, day)
        quick = foods.favorite_foods(s)[:6]
        quick += [f for f in foods.recent_foods(s, 10) if f not in quick][: 10 - len(quick)]
        quick_buttons = [
            dmc.Button(
                f.name if len(f.name) <= 28 else f.name[:26] + "…",
                id={"type": "quick-food", "index": f.id},
                variant="light",
                size="compact-sm",
                leftSection=icon("tabler:star-filled", 12) if f.is_favorite else None,
            )
            for f in quick
        ] or [dmc.Text("Foods you log or favorite show up here.", size="sm", c="dimmed")]
        entry_rows = [_entry_row(e, day) for e in entries] or [
            dmc.Text("Nothing logged for this day yet.", size="sm", c="dimmed")
        ]

    if day == today:
        title = f"Today · {fmt_day(day)}"
    elif day == today - timedelta(days=1):
        title = f"Yesterday · {fmt_day(day)}"
    else:
        title = fmt_day(day)
    end_text = fmt_clock(end, day)
    if from_sleep:
        sub = f"From {fmt_clock(start, day)} until you fell asleep at {end_text}"
    else:
        sub = (
            f"From {fmt_clock(start, day)} until you fall asleep "
            f"(or {end_text} if Garmin has no sleep)"
        )

    remaining = ""
    if targets.get("kcal"):
        left = targets["kcal"] - totals["kcal"]
        remaining = f"{left:,.0f} kcal left" if left >= 0 else f"{-left:,.0f} kcal over"
    unit = weight_unit()
    hint = []
    if NO_GOAL in plan.flags and "kcal" not in plan.overridden:
        hint = ["No monthly goal set, so calories are for maintenance. "]
    hint += [f"{m} " for m in flag_messages(plan, unit)]
    if hint:
        hint.append(dmc.Anchor("Goal and targets in Settings", href="/settings"))
    return (
        title,
        sub,
        nutrient_bars(totals, targets),
        remaining,
        plan_summary(plan, unit),
        "block" if hint else "none",
        hint,
        incomplete,
        entry_rows,
        quick_buttons,
        day >= today,
    )


@callback(
    Output("edit-done", "data", allow_duplicate=True),
    Input("day-incomplete", "checked"),
    State("day-selected", "data"),
    State("edit-done", "data"),
    prevent_initial_call=True,
)
def toggle_incomplete(checked, selected, done):
    if ctx.triggered_id != "day-incomplete":
        raise PreventUpdate
    with session_scope() as s:
        day = date.fromisoformat(selected) if selected else current_food_day(s, tz(), now())
        if log.is_incomplete(s, day) == bool(checked):
            raise PreventUpdate
        log.set_incomplete(s, day, bool(checked))
    return (done or 0) + 1


@callback(
    Output("logm-open", "data", allow_duplicate=True),
    Input({"type": "quick-food", "index": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def quick_add(_clicks):
    clicked = triggered_click()
    if clicked is None:
        raise PreventUpdate
    return {"food_id": clicked["index"], "nonce": time.time()}


@callback(
    Output("edit-modal", "opened", allow_duplicate=True),
    Output("edit-entry-id", "data"),
    Output("edit-modal", "title"),
    Output("edit-amount", "value"),
    Output("edit-unit", "data"),
    Output("edit-unit", "value"),
    Output("edit-date", "value"),
    Output("edit-clock", "value"),
    Output("edit-day", "data"),
    Output("edit-day", "value"),
    Output("edit-note", "value"),
    Output("edit-error", "children", allow_duplicate=True),
    Input({"type": "entry-row", "index": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def open_edit(_clicks):
    clicked = triggered_click()
    if clicked is None:
        raise PreventUpdate
    with session_scope() as s:
        entry = s.get(FoodLog, clicked["index"])
        if entry is None:
            raise PreventUpdate
        food = foods.load_food(s, entry.food_id) if entry.food_id else None
        amount, unit = log.entry_unit(s, entry)
        fd = food_days_around(s, tz(), entry.eaten_at.astimezone(tz()).date())
        natural = fd.day_of(entry.eaten_at)
        days = [natural - timedelta(days=1), natural, natural + timedelta(days=1)]
        if entry.food_day_override and entry.food_day_override not in days:
            days.append(entry.food_day_override)
        day_options = [{"value": "auto", "label": f"Automatic ({fmt_day(natural)})"}] + [
            {"value": d.isoformat(), "label": fmt_day(d)} for d in days
        ]
        return (
            True,
            entry.id,
            entry.description,
            amount,
            unit_select_data(food),
            unit,
            *to_picker(entry.eaten_at),
            day_options,
            entry.food_day_override.isoformat() if entry.food_day_override else "auto",
            entry.note or "",
            "",
        )


@callback(
    Output("edit-modal", "opened", allow_duplicate=True),
    Output("edit-done", "data", allow_duplicate=True),
    Output("edit-error", "children", allow_duplicate=True),
    Input("edit-save", "n_clicks"),
    Input("edit-delete", "n_clicks"),
    State("edit-entry-id", "data"),
    State("edit-amount", "value"),
    State("edit-unit", "value"),
    State("edit-date", "value"),
    State("edit-clock", "value"),
    State("edit-day", "value"),
    State("edit-note", "value"),
    State("edit-done", "data"),
    prevent_initial_call=True,
)
def save_edit(_save, _delete, entry_id, amount, unit, day, clock, day_value, note, done):
    if not entry_id:
        raise PreventUpdate
    try:
        with session_scope() as s:
            if ctx.triggered_id == "edit-delete":
                log.delete_entry(s, int(entry_id))
            else:
                log.update_entry(
                    s,
                    int(entry_id),
                    amount=float(amount or 0),
                    unit=unit or "g",
                    eaten_at=from_picker(day, clock),
                    food_day_override=None
                    if day_value in (None, "auto")
                    else date.fromisoformat(day_value),
                    note=note,
                )
    except ValueError as e:
        return no_update, no_update, str(e)
    return False, (done or 0) + 1, ""


dash.register_page(
    __name__,
    path="/",
    name="Today",
    title="Today · Weight Coach",
    order=0,
    layout=layout,
)
