"""'Describe' tab on Add food: Claude itemizes a meal, the user reviews, then it's logged.

Rows matched to a record (the user's foods, USDA, a chain's published nutrition)
scale with the grams input. Rows without one (Claude's estimates, restaurant items
it couldn't find) show their nutrients as inputs for the amount eaten instead.
"""

from datetime import datetime
from urllib.parse import urlparse

import dash_mantine_components as dmc
from dash import ALL, Input, Output, State, callback, ctx, dcc, no_update
from dash.exceptions import PreventUpdate

from gwc.analysis.food_day import current_food_day
from gwc.coach.jobs import get_jobs
from gwc.coach.parse import confirm_items, item_nutrients, parse_description, saves_food
from gwc.db.session import session_scope
from gwc.nutrition.nutrients import BY_KEY, NUTRIENT_KEYS
from gwc.web.components.common import fmt_clock, icon, now, tz

POLL_MS = 1000
SOURCE_LABELS = {
    "my_food": "My food",
    "usda": "USDA",
    "restaurant": "Restaurant",
    "estimate": "Estimate",
}
SOURCE_COLORS = {"my_food": "brand", "usda": "blue", "restaurant": "violet", "estimate": "orange"}
SHORT_LABELS = {"kcal": "Calories", "sodium_mg": "Sodium (mg)"}


def ai_panel() -> dmc.Stack:
    return dmc.Stack(
        [
            dmc.Textarea(
                id="ai-text",
                placeholder="e.g. 2 scrambled eggs in butter and a slice of sourdough at 1:30am, "
                "plus a large black coffee — or a Chipotle chicken burrito bowl, no cheese",
                autosize=True,
                minRows=3,
                maxRows=8,
            ),
            dmc.Group(
                [
                    dmc.Text(
                        "Claude matches each item to your foods or USDA, and looks up chain "
                        "restaurant items on the chain's site. Nothing is saved until you "
                        "confirm.",
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


def prefill(row: dict) -> dict[str, float | None]:
    """An editable row's starting values: Claude's estimate for the amount eaten, if any."""
    if not row.get("per100"):
        return dict.fromkeys(NUTRIENT_KEYS)
    totals = item_nutrients(row)
    return {k: round(totals[k], BY_KEY[k].decimals) for k in NUTRIENT_KEYS}


def _source_line(row: dict) -> dmc.Text | dmc.Group | None:
    rest = row.get("restaurant") or {}
    if row.get("source") == "restaurant" and rest.get("source_url"):
        host = urlparse(rest["source_url"]).netloc.removeprefix("www.") or "source"
        official = rest.get("source_kind") == "official"
        return dmc.Group(
            [
                dmc.Text(
                    "Official nutrition:" if official else "Third-party nutrition:",
                    size="xs",
                    c="violet" if official else "orange",
                ),
                dmc.Anchor(host, href=rest["source_url"], target="_blank", size="xs"),
            ],
            gap=4,
        )
    if not row.get("editable"):
        return None
    if rest:
        text = f"Couldn't find published numbers from {rest['chain']}. "
        text += (
            "Check Claude's estimate below against the menu or receipt."
            if row.get("per100")
            else "Enter them from the menu or receipt."
        )
    elif row.get("per100"):
        text = "Claude's estimate (no matching food record). Correct anything that's off."
    else:
        text = "No matching food. Enter its nutrients, or leave it out."
    return dmc.Text(text, size="xs", c="orange")


def _nutrient_inputs(row: dict) -> dmc.SimpleGrid:
    values = prefill(row)
    return dmc.SimpleGrid(
        [
            dmc.NumberInput(
                id={"type": "ai-nut", "index": row["key"], "key": k},
                label=SHORT_LABELS.get(k, f"{BY_KEY[k].label} (g)"),
                value=values[k],
                min=0,
                size="xs",
                hideControls=True,
                inputProps={"inputMode": "decimal"},
            )
            for k in NUTRIENT_KEYS
        ],
        cols={"base": 2, "sm": 4},
        spacing="xs",
        verticalSpacing=4,
    )


def _item_card(row: dict, food_day) -> dmc.Paper:
    source = row.get("source")
    error = row.get("error")
    not_found = row.get("editable") and not row.get("per100")
    header = [
        dmc.Text(row["name"], size="sm", fw=600, lineClamp=2, flex=1),
        dmc.Badge(
            "No match" if error else "Not found" if not_found else SOURCE_LABELS.get(source, "?"),
            color="red" if error else SOURCE_COLORS.get(source, "red"),
            variant="light",
            size="sm",
        ),
    ]
    when = datetime.fromisoformat(row["eaten_at"])
    detail = f"“{row['description']}” · {row['amount_label']} · {fmt_clock(when, food_day)}"
    include = dmc.Checkbox(
        id={"type": "ai-include", "index": row["key"]},
        label="Include",
        checked=not error and not not_found,
        disabled=bool(error),
        size="xs",
    )
    children = [dmc.Group(header, wrap="nowrap", gap="xs"), dmc.Text(detail, size="xs", c="dimmed")]
    if error:
        children.append(
            dmc.Text(f"{error} Add it from the search tabs instead.", size="xs", c="red")
        )
        children.append(dmc.Box(include, display="none"))
    elif row.get("editable"):
        children += [_source_line(row), _nutrient_inputs(row), include]
    else:
        line = _source_line(row)
        if line is not None:
            children.append(line)
        children.append(
            dmc.Text(id={"type": "ai-kcal", "index": row["key"]}, size="xs", c="dimmed")
        )
        children.append(
            dmc.Group(
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
                    include,
                ],
                justify="space-between",
            )
        )
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


def _by_index(values, ids) -> dict:
    return {i["index"]: v for v, i in zip(values or [], ids or [], strict=False)}


def _entered(nuts, nut_ids) -> dict[int, dict[str, float | None]]:
    out: dict[int, dict[str, float | None]] = {}
    for v, i in zip(nuts or [], nut_ids or [], strict=False):
        out.setdefault(i["index"], {})[i["key"]] = None if v in (None, "") else float(v)
    return out


def _chosen(data, grams, gram_ids, included, include_ids, nuts, nut_ids) -> list[dict]:
    """The included rows with the amounts and nutrients entered in the review."""
    by_grams = _by_index(grams, gram_ids)
    by_include = _by_index(included, include_ids)
    entered = _entered(nuts, nut_ids)
    out = []
    for row in (data or {}).get("items", []):
        key = row["key"]
        if not by_include.get(key) or row.get("error"):
            continue
        if row.get("editable"):
            values = entered.get(key, {})
            if not values.get("kcal"):
                continue
            totals = {k: values.get(k) or 0.0 for k in NUTRIENT_KEYS}
            start = prefill(row)
            edited = any(values.get(k) != start[k] for k in NUTRIENT_KEYS)
            # Claude's weight for it, if any, is only kept for the record.
            grams = row.get("grams") or 100.0
            out.append({**row, "grams": grams, "totals": totals, "edited": edited})
        else:
            g = float(by_grams.get(key) or 0)
            if g > 0:
                out.append({**row, "grams": g, "totals": item_nutrients(row, g)})
    return out


@callback(
    Output({"type": "ai-kcal", "index": ALL}, "children"),
    Output("ai-total", "children"),
    Output("ai-confirm", "children"),
    Output("ai-confirm", "disabled"),
    Input({"type": "ai-grams", "index": ALL}, "value"),
    Input({"type": "ai-include", "index": ALL}, "checked"),
    Input({"type": "ai-nut", "index": ALL, "key": ALL}, "value"),
    State({"type": "ai-grams", "index": ALL}, "id"),
    State({"type": "ai-include", "index": ALL}, "id"),
    State({"type": "ai-nut", "index": ALL, "key": ALL}, "id"),
    State("ai-items", "data"),
)
def update_totals(grams, included, nuts, gram_ids, include_ids, nut_ids, data):
    rows = {r["key"]: r for r in (data or {}).get("items", [])}
    lines = []
    for g, gid in zip(grams, gram_ids, strict=False):
        row = rows.get(gid["index"])
        if row is None:
            lines.append("")
            continue
        n = item_nutrients(row, float(g or 0))
        lines.append(
            f"{n['kcal']:,.0f} kcal · P {n['protein_g']:,.0f} · C {n['carbs_g']:,.0f} · "
            f"F {n['fat_g']:,.0f}"
        )
    chosen = _chosen(data, grams, gram_ids, included, include_ids, nuts, nut_ids)
    total = sum(r["totals"]["kcal"] for r in chosen)
    count = len(chosen)
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
    State({"type": "ai-nut", "index": ALL, "key": ALL}, "value"),
    State({"type": "ai-grams", "index": ALL}, "id"),
    State({"type": "ai-include", "index": ALL}, "id"),
    State({"type": "ai-nut", "index": ALL, "key": ALL}, "id"),
    State("ai-items", "data"),
    State("logm-done", "data"),
    prevent_initial_call=True,
)
def confirm(_ok, _discard, grams, included, nuts, gram_ids, include_ids, nut_ids, data, done):
    if not data:
        raise PreventUpdate
    if ctx.triggered_id == "ai-discard":
        return None, no_update, no_update, no_update, no_update, None
    chosen = _chosen(data, grams, gram_ids, included, include_ids, nuts, nut_ids)
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
    saved = sum(1 for r in chosen if saves_food(r))
    message = f"Logged {count} item{'s' * (count != 1)} from your description."
    if saved:
        message += f" Saved {saved} restaurant item{'s' * (saved != 1)} to your foods."
    return None, "", message, "block", (done or 0) + 1, None
