"""Gout: trigger load right now, the current flare day by day, and past flares.

Each flare records when it started, where, and how bad each day was (1–5, with
anything taken for it). Past flares show the 48 hours before them next to the
same hours on ordinary days. Purine numbers are Claude's estimates; tap a source
to correct one.
"""

from datetime import date, timedelta

import dash
import dash_mantine_components as dmc
from dash import ALL, Input, Output, State, callback, ctx, dcc, no_update
from dash.exceptions import PreventUpdate

from gwc.analysis.units import food_unit
from gwc.coach.jobs import get_jobs
from gwc.db.models import FoodLog
from gwc.db.session import session_scope
from gwc.gout import estimate, flares, profiles, triggers
from gwc.web.components.common import (
    card,
    fmt_clock,
    from_picker,
    icon,
    now,
    page_header,
    to_picker,
    triggered_click,
    tz,
    when_inputs,
)
from gwc.web.components.gout_panels import (
    day_entry,
    factor_row,
    flare_summary,
    flare_title,
    level_badge,
    lookback_panel,
    severity_badge,
    severity_data,
    severity_guide,
    sources_list,
)
from gwc.web.format import fmt_day

POLL_MS = 1500
DISCLAIMER = (
    "A rough guide from published studies of what precedes flares, not a prediction "
    "or medical advice. Purines are Claude's estimates."
)


def _flare_modal() -> dmc.Modal:
    return dmc.Modal(
        dmc.Stack(
            [
                when_inputs("flare", label="Started"),
                dmc.TagsInput(
                    id="flare-joints",
                    label="Where",
                    data=flares.JOINTS,
                    placeholder="Big toe, ankle…",
                    clearable=True,
                ),
                dmc.Stack(
                    [
                        dmc.Text("How bad is it?", size="sm", fw=500),
                        dmc.SegmentedControl(id="flare-sev", data=severity_data(), fullWidth=True),
                        severity_guide(),
                        dmc.TagsInput(
                            id="flare-meds",
                            label="Taken for it",
                            data=flares.MEDICATIONS,
                            placeholder="Colchicine, ibuprofen…",
                            clearable=True,
                        ),
                    ],
                    id="flare-first-day",
                    gap=6,
                ),
                dmc.Textarea(id="flare-note", label="Note", autosize=True, minRows=1),
                dmc.Text(id="flare-error", c="red", size="sm"),
                dmc.Group(
                    [
                        dmc.Button("Delete", id="flare-delete", color="red", variant="subtle"),
                        dmc.Button("Save", id="flare-save"),
                    ],
                    justify="space-between",
                ),
            ]
        ),
        id="flare-modal",
        title="Log a flare",
        centered=True,
    )


def _day_modal() -> dmc.Modal:
    return dmc.Modal(
        dmc.Stack(
            [
                dmc.Text("How bad was it?", size="sm", fw=500),
                dmc.SegmentedControl(
                    id="gday-sev", data=severity_data(with_none=True), fullWidth=True
                ),
                severity_guide(),
                dmc.TagsInput(
                    id="gday-meds",
                    label="Taken for it",
                    data=flares.MEDICATIONS,
                    placeholder="Colchicine, ibuprofen…",
                    clearable=True,
                ),
                dmc.TextInput(id="gday-note", label="Note"),
                dmc.Text(id="gday-error", c="red", size="sm"),
                dmc.Group([dmc.Button("Save", id="gday-save")], justify="flex-end"),
            ]
        ),
        id="gday-modal",
        title="Flare day",
        centered=True,
    )


def _profile_modal() -> dmc.Modal:
    def number(id: str, label: str, suffix: str) -> dmc.NumberInput:
        return dmc.NumberInput(
            id=id,
            label=label,
            min=0,
            step=10 if suffix == " mg" else 1,
            decimalScale=1,
            allowNegative=False,
            thousandSeparator=",",
            suffix=suffix,
        )

    return dmc.Modal(
        dmc.Stack(
            [
                dmc.Text(id="prof-portion", size="sm", c="dimmed"),
                number("prof-purine", "Purines in this portion", " mg"),
                number("prof-animal", "…of which from meat and seafood", " mg"),
                number("prof-alcohol", "Alcohol in this portion", " g"),
                dmc.Text("A standard drink is 14 g of alcohol.", size="xs", c="dimmed"),
                dmc.Checkbox(id="prof-sweet", label="A sugar-sweetened drink or fruit juice"),
                dmc.Text(id="prof-scope", size="xs", c="dimmed"),
                dmc.Text(id="prof-source", size="xs", c="dimmed"),
                dmc.Text(id="prof-error", c="red", size="sm"),
                dmc.Group([dmc.Button("Save", id="prof-save")], justify="flex-end"),
            ]
        ),
        id="prof-modal",
        title="Gout numbers",
        centered=True,
    )


def layout(**_kwargs):
    return dmc.Stack(
        [
            page_header(
                "Gout",
                "tabler:flame",
                "Flares, and the food and habits that can set them off",
                right=dmc.Button("Log a flare", id="gout-new", leftSection=icon("tabler:plus", 16)),
            ),
            dmc.SimpleGrid(
                [
                    card(dmc.Stack(id="gout-triggers", gap="sm")),
                    dmc.Stack(
                        [
                            card(dmc.Stack(id="gout-active", gap="sm")),
                            card(dmc.Stack(id="gout-history", gap="sm")),
                        ],
                        gap="md",
                    ),
                ],
                cols={"base": 1, "md": 2},
                spacing="md",
            ),
            dcc.Store(id="gout-changed", data=0),
            dcc.Store(id="gout-flare-id"),
            dcc.Store(id="gout-day-key"),
            dcc.Store(id="gout-entry-id"),
            dcc.Store(id="gout-job"),
            dcc.Interval(id="gout-poll", interval=POLL_MS, disabled=True),
            _flare_modal(),
            _day_modal(),
            _profile_modal(),
        ],
        gap="sm",
    )


# --------------------------------------------------------------------------- render


def _trigger_panel(report: triggers.Report, today: date, waiting: int, job) -> list:
    children: list = [
        dmc.Group(
            [dmc.Title("Trigger load right now", order=5), level_badge(report.level)],
            justify="space-between",
            align="center",
        ),
        *[factor_row(f) for f in report.factors],
    ]
    if report.sources:
        children += [
            dmc.Divider(),
            dmc.Group(
                [
                    dmc.Text("Biggest sources, last 48 h", size="sm", fw=500),
                    dmc.Text("Tap one to correct it", size="xs", c="dimmed"),
                ],
                justify="space-between",
                align="baseline",
            ),
            *sources_list(report, today, clickable=True),
        ]
    if job is not None and job.running:
        children.append(
            dmc.Group([dmc.Loader(size="xs"), dmc.Text(job.activity, size="sm")], gap="xs")
        )
    elif waiting:
        error = job.error if job is not None and job.status == "error" else None
        children.append(
            dmc.Group(
                [
                    dmc.Text(
                        f"{waiting} food{'s' if waiting != 1 else ''} waiting for estimates "
                        "(they run every few minutes).",
                        size="sm",
                        c="dimmed",
                        flex=1,
                    ),
                    dmc.Button("Estimate now", id="gout-estimate", size="xs", variant="light"),
                ],
                wrap="nowrap",
            )
        )
        if error:
            children.append(dmc.Text(error, size="xs", c="red"))
    children.append(dmc.Text(DISCLAIMER, size="xs", c="dimmed"))
    return children


def _day_row(flare, day: date):
    row = day_entry(flare, day)
    meds = ", ".join(row.medications) if row and row.medications else None
    detail = " · ".join(x for x in (meds, row.note if row else None) if x) or "Tap to record"
    return dmc.UnstyledButton(
        dmc.Paper(
            dmc.Group(
                [
                    dmc.Stack(
                        [
                            dmc.Text(
                                f"{fmt_day(day)} · day {flares.day_number(flare, day)}",
                                size="sm",
                                fw=500,
                            ),
                            dmc.Text(detail, size="xs", c="dimmed", lineClamp=1),
                        ],
                        gap=0,
                        flex=1,
                        miw=0,
                    ),
                    severity_badge(row.severity if row else None),
                ],
                wrap="nowrap",
                gap="sm",
            ),
            p="xs",
            withBorder=True,
            className="gwc-entry",
        ),
        id={"type": "gout-day", "flare": flare.id, "day": day.isoformat()},
        w="100%",
    )


def _active_panel(flare, lb, today: date) -> list:
    if flare is None:
        return [
            dmc.Title("Current flare", order=5),
            dmc.Text(
                "No flare going on. When one starts, log it with “Log a flare”, then rate each "
                "day until it's over.",
                size="sm",
                c="dimmed",
            ),
        ]
    started = f"Started {fmt_day(flares.start_day(flare))}, {fmt_clock(flare.started_at)}"
    days = list(reversed(flares.flare_dates(flare, today)))
    return [
        dmc.Group(
            [
                dmc.Title("Current flare", order=5),
                dmc.Group(
                    [
                        dmc.Button(
                            "Edit",
                            id={"type": "gout-edit", "index": flare.id},
                            size="compact-sm",
                            variant="subtle",
                        ),
                        dmc.Button(
                            "It's over",
                            id={"type": "gout-end", "index": flare.id},
                            size="compact-sm",
                            variant="light",
                        ),
                    ],
                    gap=4,
                ),
            ],
            justify="space-between",
            align="center",
        ),
        dmc.Text(f"{started} · {', '.join(flare.joints) or 'joint not noted'}", size="sm"),
        dmc.Text(flare.note, size="xs", c="dimmed") if flare.note else None,
        *[_day_row(flare, d) for d in days],
        dmc.Text("“It's over” ends it on the last day you rated.", size="xs", c="dimmed"),
        dmc.Divider(),
        lookback_panel(lb, today),
    ]


def _history_panel(past: list, lookbacks: dict, today: date) -> list:
    children: list = [dmc.Title("Past flares", order=5)]
    if not past:
        children.append(dmc.Text("None logged yet.", size="sm", c="dimmed"))
        return children
    items = []
    for flare in past:
        items.append(
            dmc.AccordionItem(
                [
                    dmc.AccordionControl(
                        dmc.Stack(
                            [
                                dmc.Text(flare_title(flare), size="sm", fw=500),
                                dmc.Text(flare_summary(flare), size="xs", c="dimmed"),
                            ],
                            gap=0,
                        )
                    ),
                    dmc.AccordionPanel(
                        dmc.Stack(
                            [
                                *[_day_row(flare, d) for d in flares.flare_dates(flare, today)],
                                dmc.Text(flare.note, size="xs", c="dimmed") if flare.note else None,
                                dmc.Group(
                                    [
                                        dmc.Button(
                                            "Edit",
                                            id={"type": "gout-edit", "index": flare.id},
                                            size="compact-sm",
                                            variant="subtle",
                                        ),
                                        dmc.Button(
                                            "Still going",
                                            id={"type": "gout-reopen", "index": flare.id},
                                            size="compact-sm",
                                            variant="subtle",
                                            color="gray",
                                        ),
                                    ],
                                    gap=4,
                                ),
                                dmc.Divider(),
                                lookback_panel(lookbacks[flare.id], today),
                            ],
                            gap=6,
                        )
                    ),
                ],
                value=str(flare.id),
            )
        )
    children.append(dmc.Accordion(items, variant="separated", chevronPosition="right"))
    return children


@callback(
    Output("gout-triggers", "children"),
    Output("gout-active", "children"),
    Output("gout-history", "children"),
    Input("gout-changed", "data"),
)
def render(_changed):
    zone, at = tz(), now()
    today = at.astimezone(zone).date()
    with session_scope() as s:
        report = triggers.report(s, zone, at)
        all_flares = flares.list_flares(s)
        active = next((f for f in all_flares if f.ended_on is None), None)
        lookbacks = {f.id: triggers.lookback(s, zone, f, all_flares) for f in all_flares}
        waiting = len(estimate.pending(s, at))
        job = next(iter(get_jobs().running("gout")), None)
        return (
            _trigger_panel(report, today, waiting, job),
            _active_panel(active, lookbacks.get(active.id) if active else None, today),
            _history_panel([f for f in all_flares if f.ended_on is not None], lookbacks, today),
        )


# --------------------------------------------------------------------------- estimates


@callback(
    Output("gout-job", "data"),
    Output("gout-poll", "disabled"),
    Input("gout-estimate", "n_clicks"),
    prevent_initial_call=True,
)
def start_estimate(clicks):
    if not clicks:
        raise PreventUpdate
    job = estimate.start_estimate()
    return job.id, False


@callback(
    Output("gout-poll", "disabled", allow_duplicate=True),
    Output("gout-changed", "data", allow_duplicate=True),
    Input("gout-poll", "n_intervals"),
    State("gout-job", "data"),
    State("gout-changed", "data"),
    prevent_initial_call=True,
)
def poll_estimate(_n, job_id, changed):
    job = get_jobs().get(job_id)
    if job is not None and job.running:
        return False, (changed or 0) + 1  # re-render to show progress
    return True, (changed or 0) + 1


# --------------------------------------------------------------------------- flares


@callback(
    Output("flare-modal", "opened", allow_duplicate=True),
    Output("gout-flare-id", "data"),
    Output("flare-modal", "title"),
    Output("flare-date", "value"),
    Output("flare-clock", "value"),
    Output("flare-joints", "value"),
    Output("flare-sev", "value"),
    Output("flare-meds", "value"),
    Output("flare-note", "value"),
    Output("flare-first-day", "display"),
    Output("flare-delete", "display"),
    Output("flare-error", "children", allow_duplicate=True),
    Input("gout-new", "n_clicks"),
    Input({"type": "gout-edit", "index": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def open_flare(new_clicks, _edits):
    if ctx.triggered_id == "gout-new":
        if not new_clicks:
            raise PreventUpdate
        day, clock = to_picker(now())
        return True, None, "Log a flare", day, clock, [], None, [], "", "flex", "none", ""
    clicked = triggered_click()
    if clicked is None:
        raise PreventUpdate
    with session_scope() as s:
        flare = flares.load_flare(s, clicked["index"])
        if flare is None:
            raise PreventUpdate
        day, clock = to_picker(flare.started_at)
        return (
            True,
            flare.id,
            "Edit flare",
            day,
            clock,
            list(flare.joints),
            None,
            [],
            flare.note or "",
            "none",
            "block",
            "",
        )


@callback(
    Output("flare-modal", "opened", allow_duplicate=True),
    Output("gout-changed", "data", allow_duplicate=True),
    Output("flare-error", "children", allow_duplicate=True),
    Input("flare-save", "n_clicks"),
    Input("flare-delete", "n_clicks"),
    State("gout-flare-id", "data"),
    State("flare-date", "value"),
    State("flare-clock", "value"),
    State("flare-joints", "value"),
    State("flare-sev", "value"),
    State("flare-meds", "value"),
    State("flare-note", "value"),
    State("gout-changed", "data"),
    prevent_initial_call=True,
)
def save_flare(save, delete, flare_id, day, clock, joints, sev, meds, note, changed):
    if not (save or delete):
        raise PreventUpdate
    with session_scope() as s:
        if ctx.triggered_id == "flare-delete":
            if flare_id is None:
                raise PreventUpdate
            flares.delete_flare(s, flare_id)
            return False, (changed or 0) + 1, ""
        started = from_picker(day, clock)
        if started > now() + timedelta(minutes=5):
            return no_update, no_update, "That's in the future"
        try:
            flares.save_flare(
                s,
                flare_id,
                started_at=started,
                tz=tz().key,
                joints=joints,
                note=note,
                severity=int(sev) if sev else None,
                medications=meds,
            )
        except ValueError as e:
            return no_update, no_update, str(e)
    return False, (changed or 0) + 1, ""


@callback(
    Output("gout-changed", "data", allow_duplicate=True),
    Input({"type": "gout-end", "index": ALL}, "n_clicks"),
    Input({"type": "gout-reopen", "index": ALL}, "n_clicks"),
    State("gout-changed", "data"),
    prevent_initial_call=True,
)
def end_or_reopen(_end, _reopen, changed):
    clicked = triggered_click()
    if clicked is None:
        raise PreventUpdate
    with session_scope() as s:
        flare = flares.load_flare(s, clicked["index"])
        if flare is None:
            raise PreventUpdate
        if clicked["type"] == "gout-end":
            last = max((d.day for d in flare.days), default=flares.start_day(flare))
            flares.end_flare(s, flare.id, last)
        else:
            flares.end_flare(s, flare.id, None)
    return (changed or 0) + 1


@callback(
    Output("gday-modal", "opened", allow_duplicate=True),
    Output("gout-day-key", "data"),
    Output("gday-modal", "title"),
    Output("gday-sev", "value"),
    Output("gday-meds", "value"),
    Output("gday-note", "value"),
    Output("gday-error", "children", allow_duplicate=True),
    Input({"type": "gout-day", "flare": ALL, "day": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def open_day(_clicks):
    clicked = triggered_click()
    if clicked is None:
        raise PreventUpdate
    day = date.fromisoformat(clicked["day"])
    with session_scope() as s:
        flare = flares.load_flare(s, clicked["flare"])
        if flare is None:
            raise PreventUpdate
        row = day_entry(flare, day)
        title = f"{fmt_day(day)} · flare day {flares.day_number(flare, day)}"
        return (
            True,
            {"flare": flare.id, "day": day.isoformat()},
            title,
            str(row.severity) if row else "0",
            list(row.medications) if row else [],
            row.note or "" if row else "",
            "",
        )


@callback(
    Output("gday-modal", "opened", allow_duplicate=True),
    Output("gout-changed", "data", allow_duplicate=True),
    Output("gday-error", "children", allow_duplicate=True),
    Input("gday-save", "n_clicks"),
    State("gout-day-key", "data"),
    State("gday-sev", "value"),
    State("gday-meds", "value"),
    State("gday-note", "value"),
    State("gout-changed", "data"),
    prevent_initial_call=True,
)
def save_day(clicks, key, sev, meds, note, changed):
    if not clicks or not key:
        raise PreventUpdate
    with session_scope() as s:
        try:
            flares.set_day(
                s, key["flare"], date.fromisoformat(key["day"]), int(sev or 0), meds, note
            )
        except ValueError as e:
            return no_update, no_update, str(e)
    return False, (changed or 0) + 1, ""


# --------------------------------------------------------------------------- corrections


@callback(
    Output("prof-modal", "opened", allow_duplicate=True),
    Output("gout-entry-id", "data"),
    Output("prof-modal", "title"),
    Output("prof-portion", "children"),
    Output("prof-purine", "value"),
    Output("prof-animal", "value"),
    Output("prof-alcohol", "value"),
    Output("prof-sweet", "checked"),
    Output("prof-scope", "children"),
    Output("prof-source", "children"),
    Output("prof-error", "children", allow_duplicate=True),
    Input({"type": "gout-src", "index": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def open_profile(_clicks):
    clicked = triggered_click()
    if clicked is None:
        raise PreventUpdate
    with session_scope() as s:
        entry = s.get(FoodLog, clicked["index"])
        if entry is None:
            raise PreventUpdate
        book = profiles.ProfileBook(s)
        p = book.for_entry(entry)
        x = book.exposure(entry)
        amount = entry.unit_label or f"{entry.grams:,.0f} g"
        if food_unit() == "oz" and not entry.unit_label:
            amount = f"{entry.grams / 28.349523125:,.1f} oz"
        if not amount.endswith(" g") and not amount.endswith(" g)"):
            amount += f" ({entry.grams:,.0f} g)"  # "1 bowl (500 g)" already says it
        portion = f"{amount}, {fmt_clock(entry.eaten_at)}"
        if entry.food_id is not None and entry.food is not None:
            scope = (
                f"Saves to “{entry.food.name}” in your library, so every entry of it "
                "uses these numbers."
            )
        else:
            scope = "Saves for just this entry."
        source = ""
        if p is not None:
            origin = {"ai": "Estimated by Claude", "manual": "Set by you"}.get(
                p.source, "Worked out from the recipe's ingredients"
            )
            source = f"{origin}" + (f": {p.note}" if p.note and p.source == "ai" else "")
        return (
            True,
            entry.id,
            entry.description,
            portion,
            round(x.purine_mg, 1) if x else None,
            round(x.animal_purine_mg, 1) if x else None,
            round(x.alcohol_g, 1) if x else None,
            bool(p and p.sweet_drink),
            scope,
            source,
            "",
        )


@callback(
    Output("prof-modal", "opened", allow_duplicate=True),
    Output("gout-changed", "data", allow_duplicate=True),
    Output("prof-error", "children", allow_duplicate=True),
    Input("prof-save", "n_clicks"),
    State("gout-entry-id", "data"),
    State("prof-purine", "value"),
    State("prof-animal", "value"),
    State("prof-alcohol", "value"),
    State("prof-sweet", "checked"),
    State("gout-changed", "data"),
    prevent_initial_call=True,
)
def save_profile(clicks, entry_id, purine, animal, alcohol, sweet, changed):
    if not clicks or not entry_id:
        raise PreventUpdate

    def num(v) -> float:
        return float(v) if v not in (None, "") else 0.0

    if num(animal) > num(purine):
        return no_update, no_update, "Meat and seafood purines can't be more than the total"
    with session_scope() as s:
        try:
            profiles.save_entry_override(
                s,
                entry_id,
                purine_mg=num(purine),
                animal_purine_mg=num(animal),
                alcohol_g=num(alcohol),
                sweet_drink=bool(sweet),
            )
        except ValueError as e:
            return no_update, no_update, str(e)
    return False, (changed or 0) + 1, ""


dash.register_page(
    __name__,
    path="/gout",
    name="Gout",
    title="Gout · Weight Coach",
    order=7,
    layout=layout,
)
