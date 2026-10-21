"""'Describe' tab on Add food: Claude itemizes a meal, the user reviews, then it's logged."""

from datetime import datetime

import dash_mantine_components as dmc
from dash import ALL, Input, Output, State, callback, ctx, dcc, no_update
from dash.exceptions import PreventUpdate

from gwc.analysis.food_day import current_food_day
from gwc.coach.jobs import get_jobs
from gwc.coach.parse import confirm_items, item_nutrients, parse_description
from gwc.db.session import session_scope
from gwc.web.components.common import fmt_clock, icon, now, tz

POLL_MS = 1000
SOURCE_LABELS = {"my_food": "My food", "usda": "USDA", "estimate": "Estimate"}


def ai_panel() -> dmc.Stack:
    return dmc.Stack(
        [
            dmc.Textarea(
                id="ai-text",
                placeholder="e.g. 2 scrambled eggs in butter and a slice of sourdough at 1:30am, "
                "plus a large black coffee",
                autosize=True,
                minRows=3,
                maxRows=8,
            ),
            dmc.Group(
                [
                    dmc.Text(
                        "Claude matches each item to your foods or USDA. Nothing is saved until "
                        "you confirm.",
                        size="xs",
                        c="dimmed",
                        flex=1,
                    ),
                    dmc.Button("Itemize", id="ai-go", leftSection=icon("tabler:sparkles", 16)),
                ],
                wrap="nowrap",
                align="flex-start",
            ),
            dmc.Group(
                [dmc.Loader(size="xs"), dmc.Text(id="ai-activity", size="sm", c="dimmed")],
                id="ai-status",
                gap="xs",
                display="none",
            ),
            dmc.Box(id="ai-error"),
            dmc.Stack(id="ai-review", gap="xs"),
            dmc.Group(
                [
                    dmc.Text(id="ai-total", size="sm", fw=600),
                    dmc.Group(
                        [
                            dmc.Button("Discard", id="ai-discard", variant="subtle", color="gray"),
                            dmc.Button("Log items", id="ai-confirm"),
                        ],
                        gap="xs",
                    ),
                ],
                id="ai-actions",
                justify="space-between",
                display="none",
            ),
            dcc.Store(id="ai-job"),
            dcc.Store(id="ai-items"),
            dcc.Interval(id="ai-poll", interval=POLL_MS, disabled=True),
        ],
        gap="sm",
        pt="sm",
    )


def _item_card(row: dict, food_day) -> dmc.Paper:
    source = row.get("source")
    badge_color = {"my_food": "teal", "usda": "blue", "estimate": "orange"}.get(source, "red")
    when = datetime.fromisoformat(row["eaten_at"])
    header = [
        dmc.Text(row["name"], size="sm", fw=600, lineClamp=2, flex=1),
        dmc.Badge(
            SOURCE_LABELS.get(source, "No match") if not row.get("error") else "No match",
            color=badge_color if not row.get("error") else "red",
            variant="light",
            size="sm",
        ),
    ]
    detail = f"“{row['description']}” · {row['amount_label']} · {fmt_clock(when, food_day)}"
    if row.get("error"):
        body = dmc.Text(f"{row['error']} Add it from the search tabs instead.", size="xs", c="red")
        controls = None
    else:
        body = dmc.Text(id={"type": "ai-kcal", "index": row["key"]}, size="xs", c="dimmed")
        controls = dmc.Group(
            [
                dmc.NumberInput(
                    id={"type": "ai-grams", "index": row["key"]},
                    value=row["grams"],
                    min=0,
                    step=5,
                    suffix=" g",
                    w=120,
                    size="xs",
                    inputProps={"inputMode": "decimal"},
                ),
                dmc.Checkbox(
                    id={"type": "ai-include", "index": row["key"]},
                    label="Include",
                    checked=True,
                    size="xs",
                ),
            ],
            justify="space-between",
        )
    children = [dmc.Group(header, wrap="nowrap", gap="xs"), dmc.Text(detail, size="xs", c="dimmed")]
    if source == "estimate" and not row.get("error"):
        children.append(
            dmc.Text("Claude's estimate — no matching food record.", size="xs", c="orange")
        )
    children.append(body)
    if controls is not None:
        children.append(controls)
    return dmc.Paper(dmc.Stack(children, gap=4), p="xs", withBorder=True)


@callback(
    Output("ai-job", "data"),
    Output("ai-poll", "disabled"),
    Output("ai-status", "display"),
    Output("ai-activity", "children"),
    Output("ai-error", "children"),
    Output("ai-items", "data", allow_duplicate=True),
    Input("ai-go", "n_clicks"),
    State("ai-text", "value"),
    prevent_initial_call=True,
)
def start_parse(_clicks, text):
    if not text or not text.strip():
        return (
            no_update,
            True,
            "none",
            "",
            dmc.Text("Describe what you ate first.", size="sm"),
            None,
        )
    zone, at = tz(), now()
    job = get_jobs().start("parse", lambda j: parse_description(text, zone, at, j))
    return job.id, False, "flex", "Reading your description…", None, None


@callback(
    Output("ai-activity", "children", allow_duplicate=True),
    Output("ai-poll", "disabled", allow_duplicate=True),
    Output("ai-status", "display", allow_duplicate=True),
    Output("ai-items", "data", allow_duplicate=True),
    Output("ai-error", "children", allow_duplicate=True),
    Input("ai-poll", "n_intervals"),
    State("ai-job", "data"),
    prevent_initial_call=True,
)
def poll_parse(_n, job_id):
    job = get_jobs().get(job_id)
    if job is None:
        return "", True, "none", no_update, no_update
    if job.running:
        return f"{job.activity} ({job.elapsed:.0f}s)", False, "flex", no_update, no_update
    if job.status == "error":
        alert = dmc.Alert(job.error, color="red", variant="light", title="Couldn't itemize")
        return "", True, "none", None, alert
    return "", True, "none", job.result, None


@callback(
    Output("ai-review", "children"),
    Output("ai-actions", "display"),
    Input("ai-items", "data"),
)
def render_review(data):
    if not data:
        return [], "none"
    children = []
    if data.get("message"):
        children.append(
            dmc.Alert(
                data["message"], color="blue", variant="light", icon=icon("tabler:info-circle")
            )
        )
    if not data.get("items"):
        children.append(dmc.Text("Claude didn't find any foods in that.", size="sm", c="dimmed"))
        return children, "none"
    with session_scope() as s:
        food_day = current_food_day(s, tz(), now())
    children += [_item_card(row, food_day) for row in data["items"]]
    return children, "flex"


@callback(
    Output({"type": "ai-kcal", "index": ALL}, "children"),
    Output("ai-total", "children"),
    Output("ai-confirm", "children"),
    Output("ai-confirm", "disabled"),
    Input({"type": "ai-grams", "index": ALL}, "value"),
    Input({"type": "ai-include", "index": ALL}, "checked"),
    State({"type": "ai-grams", "index": ALL}, "id"),
    State("ai-items", "data"),
)
def update_totals(grams, included, ids, data):
    rows = {r["key"]: r for r in (data or {}).get("items", [])}
    lines, total, count = [], 0.0, 0
    for g, inc, gid in zip(grams, included, ids, strict=False):
        row = rows.get(gid["index"])
        if row is None:
            lines.append("")
            continue
        n = item_nutrients(row, float(g or 0))
        lines.append(
            f"{n['kcal']:,.0f} kcal · P {n['protein_g']:,.0f} · C {n['carbs_g']:,.0f} · "
            f"F {n['fat_g']:,.0f}"
        )
        if inc and (g or 0) > 0:
            total += n["kcal"]
            count += 1
    label = f"Log {count} item{'s' * (count != 1)}"
    return lines, f"{total:,.0f} kcal total", label, count == 0


@callback(
    Output("ai-items", "data", allow_duplicate=True),
    Output("ai-text", "value"),
    Output("logm-result", "children", allow_duplicate=True),
    Output("logm-result", "display", allow_duplicate=True),
    Output("logm-done", "data", allow_duplicate=True),
    Output("ai-error", "children", allow_duplicate=True),
    Input("ai-confirm", "n_clicks"),
    Input("ai-discard", "n_clicks"),
    State({"type": "ai-grams", "index": ALL}, "value"),
    State({"type": "ai-include", "index": ALL}, "checked"),
    State({"type": "ai-grams", "index": ALL}, "id"),
    State("ai-items", "data"),
    State("logm-done", "data"),
    prevent_initial_call=True,
)
def confirm(_ok, _discard, grams, included, ids, data, done):
    if not data:
        raise PreventUpdate
    if ctx.triggered_id == "ai-discard":
        return None, no_update, no_update, no_update, no_update, None
    rows = {r["key"]: r for r in data.get("items", [])}
    chosen = []
    for g, inc, gid in zip(grams, included, ids, strict=False):
        row = rows.get(gid["index"])
        if row is not None and inc:
            chosen.append({**row, "grams": float(g or 0)})
    try:
        with session_scope() as s:
            count = confirm_items(s, chosen, tz())
    except Exception as e:
        return (
            no_update,
            no_update,
            no_update,
            no_update,
            no_update,
            dmc.Alert(f"Couldn't log: {e}", color="red", variant="light"),
        )
    message = f"Logged {count} item{'s' * (count != 1)} from your description."
    return None, "", message, "block", (done or 0) + 1, None
