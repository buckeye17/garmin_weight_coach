"""Today: the food log for any day, with its targets.

Step through days or pick one; `?day=YYYY-MM-DD` opens a past day (Log's "Back"
link). Entries open an editor for amount, portion, time and food day, and a
one-off entry can be saved to the library from there.

The Activity card lists the day's workouts: Garmin's after a sync, and any added
here with a guess at the calories they burned beyond resting.
"""

from datetime import date, timedelta

import dash
import dash_mantine_components as dmc
from dash import ALL, Input, Output, State, callback, ctx, dcc, no_update
from dash.exceptions import PreventUpdate

from gwc.analysis import exercise
from gwc.analysis.exercise import MAX_MANUAL_KCAL, Workout
from gwc.analysis.food_day import current_food_day, food_days_around
from gwc.analysis.targets import NO_GOAL, TargetPlan, day_targets
from gwc.analysis.units import weight_unit
from gwc.db.models import FoodLog, ManualActivity
from gwc.db.session import session_scope
from gwc.nutrition import foods, log
from gwc.web.components.analysis_text import flag_messages, plan_summary
from gwc.web.components.common import (
    amount_input,
    amount_text,
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
from gwc.web.components.nutrient_bars import nutrient_bars
from gwc.web.format import fmt_day

PORTION_PRESETS = ["1/8", "1/4", "1/3", "1/2", "2/3", "3/4", "2"]
_GLYPHS = {"1/8": "⅛", "1/4": "¼", "1/3": "⅓", "1/2": "½", "2/3": "⅔", "3/4": "¾"}


def _portion_controls() -> dmc.Stack:
    """Multiply the whole entry by any fraction: 1/8 of a pizza, a third of a Blizzard, two."""
    presets = [
        dmc.Button(
            _GLYPHS.get(value, f"{value}×"),
            id={"type": "edit-scale", "index": value},
            variant="light",
            size="compact-md",
            px=10,
        )
        for value in PORTION_PRESETS
    ]
    custom = dmc.Group(
        [
            dmc.TextInput(
                id="edit-factor",
                placeholder="Other: 3/8, 1 1/2, 0.4",
                flex=1,
                miw=0,
                size="sm",
                debounce=250,
                inputProps={"autoComplete": "off"},
            ),
            dmc.Button("Apply", id="edit-factor-apply", variant="default"),
        ],
        gap=6,
        wrap="nowrap",
    )
    return dmc.Stack(
        [
            dmc.Text("Portion eaten", size="sm", fw=500),
            dmc.Group(presets, gap=6),
            custom,
            dmc.Text(id="edit-factor-preview", size="xs", c="dimmed"),
        ],
        gap=6,
    )


def _edit_modal() -> dmc.Modal:
    return dmc.Modal(
        dmc.Stack(
            [
                dmc.Group(
                    [
                        amount_input("edit-amount"),
                        dmc.Select(
                            id="edit-unit", label="Unit", data=[], flex=1, allowDeselect=False
                        ),
                    ],
                    align="flex-end",
                    wrap="nowrap",
                ),
                _portion_controls(),
                when_inputs("edit"),
                dmc.Select(
                    id="edit-day",
                    label="Counts toward",
                    description="Set by when you fell asleep; override if needed.",
                    data=[],
                    allowDeselect=False,
                ),
                dmc.TextInput(id="edit-note", label="Note"),
                dmc.Group(
                    [
                        dmc.Text(
                            "A one-off: not in your library.",
                            id="edit-oneoff",
                            size="xs",
                            c="dimmed",
                        ),
                        dmc.Button(
                            "Save to library",
                            id="edit-to-library",
                            variant="light",
                            size="xs",
                            leftSection=icon("tabler:books", 14),
                        ),
                    ],
                    id="edit-library",
                    justify="space-between",
                    display="none",
                ),
                dmc.Text(id="edit-msg", c="brand", size="sm"),
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


def _activity_modal() -> dmc.Modal:
    return dmc.Modal(
        dmc.Stack(
            [
                dmc.TextInput(
                    id="act-name",
                    label="Activity",
                    placeholder="Sand volleyball",
                    inputProps={"autoComplete": "off"},
                ),
                dmc.NumberInput(
                    id="act-kcal",
                    label="Extra calories burned",
                    description="Your guess beyond what you'd burn resting. The target adds "
                    "the same share as for Garmin workouts.",
                    min=0,
                    max=MAX_MANUAL_KCAL,
                    step=50,
                    allowDecimal=False,
                    allowNegative=False,
                    thousandSeparator=",",
                    suffix=" kcal",
                ),
                dmc.Text(id="act-error", c="red", size="sm"),
                dmc.Group(
                    [
                        dmc.Button("Delete", id="act-delete", color="red", variant="subtle"),
                        dmc.Button("Save", id="act-save"),
                    ],
                    justify="space-between",
                ),
            ]
        ),
        id="act-modal",
        title="Add activity",
        centered=True,
    )


def _past_day(value: str | None) -> str | None:
    """The `day` query parameter, if it names a food day before the current one."""
    try:
        day = date.fromisoformat(value) if value else None
    except ValueError:
        return None
    if day is None:
        return None
    with session_scope() as s:
        return day.isoformat() if day < current_food_day(s, tz(), now()) else None


def _day_controls() -> dmc.Group:
    return dmc.Group(
        [
            dmc.Button(
                "Today",
                id="day-today",
                variant="subtle",
                color="gray",
                size="compact-sm",
                display="none",
            ),
            dmc.Paper(
                dmc.Group(
                    [
                        dmc.ActionIcon(
                            icon("tabler:chevron-left"),
                            id="day-prev",
                            variant="subtle",
                            color="gray",
                            size="lg",
                            **{"aria-label": "Previous day"},
                        ),
                        dmc.DatePickerInput(
                            id="day-pick",
                            valueFormat="MMM D",
                            variant="unstyled",
                            size="sm",
                            w=82,
                            leftSection=icon("tabler:calendar", 16),
                            leftSectionPointerEvents="none",
                            allowDeselect=False,
                            **{"aria-label": "Pick a day"},
                        ),
                        dmc.ActionIcon(
                            icon("tabler:chevron-right"),
                            id="day-next",
                            variant="subtle",
                            color="gray",
                            size="lg",
                            **{"aria-label": "Next day"},
                        ),
                    ],
                    gap=2,
                    wrap="nowrap",
                ),
                p=2,
                radius="md",
                withBorder=True,
            ),
            dmc.Anchor(
                dmc.Button("Log food", id="day-add-btn", leftSection=icon("tabler:plus", 16)),
                id="day-add",
                href="/log",
            ),
        ],
        gap="xs",
        justify="flex-end",
    )


def layout(day: str | None = None, **_kwargs):
    return dmc.Stack(
        [
            dmc.Group(
                [
                    dmc.Stack(
                        [
                            dmc.Title(id="day-title", order=2, className="gwc-page-title"),
                            dmc.Text(id="day-sub", size="sm", c="dimmed"),
                        ],
                        gap=0,
                        miw=0,
                    ),
                    _day_controls(),
                ],
                justify="space-between",
                align="center",
                mb=4,
            ),
            dmc.SimpleGrid(
                [
                    dmc.Card(
                        dmc.Stack(
                            [
                                dmc.Group(
                                    [
                                        dmc.RingProgress(
                                            id="day-ring",
                                            size=150,
                                            thickness=13,
                                            roundCaps=True,
                                            sections=[],
                                            label=dmc.Stack(
                                                id="day-remaining", gap=0, align="center"
                                            ),
                                        ),
                                        dmc.Stack(id="day-ring-stats", gap="sm", flex=1),
                                    ],
                                    gap="lg",
                                    wrap="nowrap",
                                ),
                                dmc.Divider(),
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
                                        dmc.Group(
                                            [
                                                dmc.Title("Entries", order=5),
                                                dmc.Text(
                                                    "Tap one to change its amount or time",
                                                    size="xs",
                                                    c="dimmed",
                                                ),
                                            ],
                                            justify="space-between",
                                            align="baseline",
                                            gap="xs",
                                        ),
                                        dmc.Stack(id="day-entries", gap="xs"),
                                    ],
                                    gap="xs",
                                ),
                                withBorder=True,
                            ),
                            dmc.Card(
                                dmc.Stack(
                                    [
                                        dmc.Group(
                                            [
                                                dmc.Title("Activity", order=5),
                                                dmc.Button(
                                                    "Add activity",
                                                    id="act-add",
                                                    variant="light",
                                                    size="compact-sm",
                                                    leftSection=icon("tabler:plus", 14),
                                                ),
                                            ],
                                            justify="space-between",
                                            align="center",
                                            gap="xs",
                                        ),
                                        dmc.Stack(id="day-activities", gap="xs"),
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
            dcc.Store(id="day-selected", data=_past_day(day)),
            dcc.Store(id="edit-entry-id"),
            dcc.Store(id="edit-done", data=0),
            dcc.Store(id="act-id"),
            _edit_modal(),
            _activity_modal(),
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
                    dmc.Stack(
                        [
                            dmc.Text(f"{e.kcal:,.0f}", size="sm", fw=700, ta="right"),
                            dmc.Text("kcal", size="xs", c="dimmed", ta="right"),
                        ],
                        gap=0,
                    ),
                ],
                wrap="nowrap",
                gap="sm",
            ),
            p="xs",
            withBorder=True,
            className="gwc-entry",
        ),
        id={"type": "entry-row", "index": e.id},
        w="100%",
    )


def _activity_row(w: Workout, day: date):
    """A workout and its calories above resting; ones added here open an editor."""
    if w.manual_id is None:
        mins = f" · {w.duration_s / 60:,.0f} min" if w.duration_s else ""
        when, detail = fmt_clock(w.start_at, day), f"Garmin{mins} · {w.kcal:,.0f} kcal total"
    else:
        when, detail = "", "Added here · tap to edit"
    row = dmc.Paper(
        dmc.Group(
            [
                dmc.Text(when, size="xs", c="dimmed", w=72),
                dmc.Stack(
                    [
                        dmc.Text(w.name, size="sm", fw=500, lineClamp=1),
                        dmc.Text(detail, size="xs", c="dimmed", lineClamp=1),
                    ],
                    gap=0,
                    flex=1,
                    miw=0,
                ),
                dmc.Stack(
                    [
                        dmc.Text(f"{w.net_kcal:,.0f}", size="sm", fw=700, ta="right"),
                        dmc.Text("kcal extra", size="xs", c="dimmed", ta="right"),
                    ],
                    gap=0,
                ),
            ],
            wrap="nowrap",
            gap="sm",
        ),
        p="xs",
        withBorder=True,
        className="gwc-entry" if w.manual_id is not None else None,
    )
    if w.manual_id is None:
        return row
    return dmc.UnstyledButton(row, id={"type": "act-row", "index": w.manual_id}, w="100%")


def _ring_stat(label: str, value: str, note: str | None = None) -> dmc.Stack:
    return dmc.Stack(
        [
            dmc.Text(label, size="xs", c="dimmed", tt="uppercase", fw=600, lts=0.4),
            dmc.Text(value, fw=700, className="gwc-stat-value-sm"),
            dmc.Text(note, size="xs", c="dimmed") if note else None,
        ],
        gap=0,
    )


def _activity_note(plan: TargetPlan) -> str | None:
    """E.g. "2,100 base + 380 workouts + 40 steps": what the day's activity adds to base."""
    base = plan.base_values.get("kcal")
    if base is None or plan.eat_back_pct is None:
        return None
    note = f"{base:,.0f} base + {plan.workout_kcal:,.0f} workouts"
    if plan.step_pct is not None:
        note += f" + {plan.step_credit_kcal:,.0f} steps"
    return note


def _calorie_ring(
    totals: dict[str, float], plan: TargetPlan, n_entries: int
) -> tuple[list[dict], list, list]:
    """Ring sections, the label inside the ring, and the stats beside it."""
    eaten = totals.get("kcal") or 0
    target = plan.values.get("kcal")
    stats = [_ring_stat("Eaten", f"{eaten:,.0f} kcal")]
    if not target:
        label = [
            dmc.Text(f"{eaten:,.0f}", className="gwc-ring-value"),
            dmc.Text("kcal eaten", size="xs", c="dimmed"),
        ]
        return [], label, stats + [_ring_stat("Entries", f"{n_entries}")]
    left = target - eaten
    over = left < 0
    color = "red" if eaten > target * 1.05 else "orange" if over else "brand"
    value = dmc.Text(f"{abs(left):,.0f}", className="gwc-ring-value")
    if over:
        value.c = color
    label = [
        value,
        dmc.Text("kcal over" if over else "kcal left", size="xs", c="dimmed"),
    ]
    # A zero-width section still draws a round-cap dot, so leave the ring empty.
    sections = [{"value": min(100.0, 100 * eaten / target), "color": color}] if eaten else []
    stats += [
        _ring_stat("Target", f"{target:,.0f} kcal", _activity_note(plan)),
        _ring_stat("Entries", f"{n_entries}"),
    ]
    return sections, label, stats


@callback(
    Output("day-selected", "data"),
    Input("day-prev", "n_clicks"),
    Input("day-next", "n_clicks"),
    Input("day-today", "n_clicks"),
    Input("day-pick", "value"),
    State("day-selected", "data"),
    prevent_initial_call=True,
)
def change_day(_prev, _next, _today, picked, selected):
    with session_scope() as s:
        today = current_food_day(s, tz(), now())
    if ctx.triggered_id == "day-today":
        return None
    if ctx.triggered_id == "day-pick":
        if not picked:
            raise PreventUpdate
        day = min(date.fromisoformat(picked[:10]), today)
        if day.isoformat() == (selected or today.isoformat()):
            raise PreventUpdate  # the picker just followed the day shown
        return None if day == today else day.isoformat()
    base = date.fromisoformat(selected) if selected else today
    step = -1 if ctx.triggered_id == "day-prev" else 1
    day = base + timedelta(days=step)
    return None if day >= today else day.isoformat()


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
    Output("day-activities", "children"),
    Output("day-next", "disabled"),
    Output("day-ring", "sections"),
    Output("day-ring-stats", "children"),
    Output("day-pick", "value"),
    Output("day-pick", "maxDate"),
    Output("day-today", "display"),
    Output("day-add", "href"),
    Output("day-add-btn", "children"),
    Input("day-selected", "data"),
    Input("edit-done", "data"),
)
def render_day(selected, _edited):
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
        entry_rows = [_entry_row(e, day) for e in entries] or [
            dmc.Text("Nothing logged for this day yet.", size="sm", c="dimmed")
        ]
        activity_rows = [_activity_row(w, day) for w in plan.workouts] or [
            dmc.Text(
                "No workouts. Garmin's appear after a sync; add anything else here.",
                size="sm",
                c="dimmed",
            )
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

    ring, remaining, ring_stats = _calorie_ring(totals, plan, len(entries))
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
        nutrient_bars(totals, targets, skip=("kcal",)),
        remaining,
        plan_summary(plan, unit),
        "block" if hint else "none",
        hint,
        incomplete,
        entry_rows,
        activity_rows,
        day >= today,
        ring,
        ring_stats,
        day.isoformat(),
        today.isoformat(),
        "none" if day == today else "block",
        "/log" if day == today else f"/log?day={day.isoformat()}",
        "Log food" if day == today else "Add to this day",
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
    Output("act-modal", "opened", allow_duplicate=True),
    Output("act-id", "data"),
    Output("act-modal", "title"),
    Output("act-name", "value"),
    Output("act-kcal", "value"),
    Output("act-delete", "display"),
    Output("act-error", "children", allow_duplicate=True),
    Input("act-add", "n_clicks"),
    Input({"type": "act-row", "index": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def open_activity(add_clicks, _rows):
    if ctx.triggered_id == "act-add":
        if not add_clicks:
            raise PreventUpdate
        return True, None, "Add activity", "", "", "none", ""
    clicked = triggered_click()
    if clicked is None:
        raise PreventUpdate
    with session_scope() as s:
        activity = s.get(ManualActivity, clicked["index"])
        if activity is None:
            raise PreventUpdate
        return True, activity.id, "Edit activity", activity.name, round(activity.kcal), "block", ""


@callback(
    Output("act-modal", "opened", allow_duplicate=True),
    Output("edit-done", "data", allow_duplicate=True),
    Output("act-error", "children", allow_duplicate=True),
    Input("act-save", "n_clicks"),
    Input("act-delete", "n_clicks"),
    State("act-id", "data"),
    State("act-name", "value"),
    State("act-kcal", "value"),
    State("day-selected", "data"),
    State("edit-done", "data"),
    prevent_initial_call=True,
)
def save_activity(save, delete, activity_id, name, kcal, selected, done):
    if not (save or delete):
        raise PreventUpdate
    try:
        with session_scope() as s:
            if ctx.triggered_id == "act-delete":
                if activity_id:
                    exercise.delete_manual(s, int(activity_id))
            elif activity_id:
                exercise.update_manual(s, int(activity_id), name, kcal)
            else:
                day = date.fromisoformat(selected) if selected else current_food_day(s, tz(), now())
                exercise.add_manual(s, day, name, kcal)
    except ValueError as e:
        return no_update, no_update, str(e)
    return False, (done or 0) + 1, ""


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
    Output("edit-factor", "value"),
    Output("edit-error", "children", allow_duplicate=True),
    Output("edit-library", "display"),
    Output("edit-msg", "children", allow_duplicate=True),
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
            amount_text(amount),
            unit_select_data(food),
            unit,
            *to_picker(entry.eaten_at),
            day_options,
            entry.food_day_override.isoformat() if entry.food_day_override else "auto",
            entry.note or "",
            "",
            "",
            "none" if food is not None and not food.is_archived else "flex",
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
                    amount=log.parse_amount(amount),
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


@callback(
    Output("edit-library", "display", allow_duplicate=True),
    Output("edit-msg", "children", allow_duplicate=True),
    Output("edit-unit", "data", allow_duplicate=True),
    Output("edit-unit", "value", allow_duplicate=True),
    Output("edit-amount", "value", allow_duplicate=True),
    Output("edit-error", "children", allow_duplicate=True),
    Output("edit-done", "data", allow_duplicate=True),
    Input("edit-to-library", "n_clicks"),
    State("edit-entry-id", "data"),
    State("edit-done", "data"),
    prevent_initial_call=True,
)
def save_to_library(clicks, entry_id, done):
    if not clicks or not entry_id:
        raise PreventUpdate
    try:
        with session_scope() as s:
            food = log.save_entry_as_food(s, int(entry_id))
            message = f"Saved to your library as {food.name}. Rename it in the Library."
            units = unit_select_data(food)
            amount, unit = log.entry_unit(s, s.get(FoodLog, int(entry_id)))
    except ValueError as e:
        return no_update, "", no_update, no_update, no_update, str(e), no_update
    return "none", message, units, unit, amount_text(amount), "", (done or 0) + 1


@callback(
    Output("edit-modal", "opened", allow_duplicate=True),
    Output("edit-done", "data", allow_duplicate=True),
    Output("edit-error", "children", allow_duplicate=True),
    Input({"type": "edit-scale", "index": ALL}, "n_clicks"),
    Input("edit-factor-apply", "n_clicks"),
    Input("edit-factor", "n_submit"),
    State("edit-factor", "value"),
    State("edit-entry-id", "data"),
    State("edit-done", "data"),
    prevent_initial_call=True,
)
def scale_edit(_presets, _apply, _submit, custom, entry_id, done):
    if not entry_id:
        raise PreventUpdate
    if ctx.triggered_id in ("edit-factor-apply", "edit-factor"):
        text = custom or ""
    elif clicked := triggered_click():
        text = clicked["index"]
    else:
        raise PreventUpdate
    try:
        with session_scope() as s:
            log.scale_entry(s, int(entry_id), log.parse_count(text))
    except ValueError as e:
        return no_update, no_update, str(e)
    return False, (done or 0) + 1, ""


@callback(
    Output("edit-factor-preview", "children"),
    Input("edit-factor", "value"),
    State("edit-entry-id", "data"),
)
def preview_factor(text, entry_id):
    if not text or not text.strip() or not entry_id:
        return ""
    try:
        factor = log.parse_count(text)
    except ValueError as e:
        return str(e)
    with session_scope() as s:
        entry = s.get(FoodLog, int(entry_id))
        if entry is None:
            raise PreventUpdate
        return f"× {log.fmt_count(float(factor))} → {entry.kcal * factor:,.0f} kcal"


dash.register_page(
    __name__,
    path="/",
    name="Today",
    title="Today · Weight Coach",
    order=0,
    layout=layout,
)
