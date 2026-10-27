"""Describe or scan food: how new food comes in, on the Log, Ask and Library pages.

Claude itemizes a description (and an optional plate photo), matching each item to
the user's library, USDA or a chain's published nutrition. A barcode, scanned or
typed into the description box, is looked up in the library, USDA's branded foods
and Open Food Facts; a Nutrition Facts photo is read by Claude. Each adds rows to
one review list, and the page's mode decides what its buttons do with them:

- "log": log the included rows; each can also be added to the library (off by default).
- "ask": show how the rows would change today's totals, then log or save them.
- "library": save the included rows to the library.

Rows matched to a record scale with their grams input. Rows without one (Claude's
estimates, restaurant items it couldn't find, labels) show nutrient inputs for one
portion, times a portion count. Edits are kept in `ai-edits`, so adding a row
re-renders the list without losing them.

assets/scanner.js does the camera and photo work: a plate photo is downscaled into
`ai-photo` and only goes to the server when Itemize is pressed; a scanned code
goes to `ai-scan-code` and a label photo to `ai-label-photo`.
"""

import re
from datetime import date, datetime
from urllib.parse import urlparse

import dash_mantine_components as dmc
from dash import ALL, Input, Output, State, callback, clientside_callback, ctx, dcc, no_update
from dash.exceptions import PreventUpdate

from gwc.analysis.food_day import current_food_day, food_days_around
from gwc.analysis.targets import day_targets
from gwc.coach.jobs import get_jobs
from gwc.coach.label import read_label
from gwc.coach.parse import (
    confirm_items,
    default_eaten_at,
    first_eaten_at,
    item_nutrients,
    label_row,
    library_row,
    match_row,
    parse_description,
    retime,
    save_items,
)
from gwc.db.session import session_scope
from gwc.nutrition import barcode, foods, log
from gwc.nutrition.nutrients import BY_KEY, NUTRIENT_KEYS, add, per100_of, zero
from gwc.nutrition.service import get_off, get_usda
from gwc.web.components.common import from_picker, icon, now, to_picker, tz, when_inputs
from gwc.web.components.nutrient_bars import nutrient_bars

POLL_MS = 1000
SOURCE_LABELS = {
    "my_food": "Library",
    "usda": "USDA",
    "restaurant": "Restaurant",
    "estimate": "Estimate",
    "barcode": "Barcode",
    "label": "Label",
}
SOURCE_COLORS = {
    "my_food": "brand",
    "usda": "blue",
    "restaurant": "violet",
    "estimate": "orange",
    "barcode": "cyan",
    "label": "teal",
}
SHORT_LABELS = {"kcal": "Calories", "sodium_mg": "Sodium (mg)"}
BARCODE_TEXT = re.compile(r"\d{8,14}")

PLACEHOLDERS = {
    "log": "e.g. 2 scrambled eggs in butter and a slice of sourdough at 1:30am, plus a large "
    "black coffee — or a Chipotle chicken burrito bowl, no cheese",
    "ask": "e.g. a Chick-fil-A spicy deluxe sandwich and medium fries — or 2 slices of "
    "pepperoni pizza",
    "library": "e.g. Fage 2% Greek yogurt, or a Panera broccoli cheddar soup in a bread bowl",
}
HELP = {
    "log": "Claude matches each item to your library or USDA, and looks up chain restaurant "
    "items on the chain's site. For a place without published nutrition, add a photo of your "
    "plate. For packaged food, scan the barcode (or type its number above) or photograph the "
    "Nutrition Facts. Nothing is logged until you confirm, and an item joins your library "
    "only if you tick Add to library.",
    "ask": "Look up food the same way as logging it: describe it, add a photo, scan the "
    "barcode or photograph the label. Nothing is logged or saved unless you choose to.",
    "library": "Describe a food, scan its barcode (or type its number above) or photograph its "
    "Nutrition Facts. Check the numbers, then save. Rename or categorize it in the list "
    "afterwards.",
}
GO_LABELS = {"log": "Itemize", "ask": "Look up", "library": "Look up"}


def _input_button(label: str, id: str, icon_name: str) -> dmc.Button:
    return dmc.Button(
        label, id=id, variant="default", size="sm", px=10, leftSection=icon(icon_name, 16)
    )


def describe_panel(mode: str, day: date | None = None) -> dmc.Stack:
    """The describe-or-scan input and its review list; `day` is a past food day to log into."""
    actions = [dmc.Button("Discard", id="ai-discard", variant="subtle", color="gray")]
    actions.append(
        dmc.Button(
            "Save to library",
            id="ai-save",
            variant="light" if mode == "ask" else "filled",
            leftSection=icon("tabler:books", 16),
            **({"display": "none"} if mode == "log" else {}),
        )
    )
    actions.append(
        dmc.Button(
            "Log items",
            id="ai-confirm",
            leftSection=icon("tabler:check", 16),
            **({"display": "none"} if mode == "library" else {}),
        )
    )
    return dmc.Stack(
        [
            dmc.Alert(
                id="ai-result",
                color="brand",
                variant="light",
                display="none",
                withCloseButton=True,
                icon=icon("tabler:circle-check"),
            ),
            dmc.Textarea(
                id="ai-text",
                placeholder=PLACEHOLDERS[mode],
                autosize=True,
                minRows=3,
                maxRows=8,
            ),
            dmc.Group(
                [
                    dmc.Image(id="ai-photo-img", h=72, w="auto", radius="sm"),
                    dmc.Button(
                        "Remove photo",
                        id="ai-photo-clear",
                        variant="subtle",
                        color="gray",
                        size="xs",
                        leftSection=icon("tabler:x", 14),
                    ),
                ],
                id="ai-photo-preview",
                gap="xs",
                display="none",
            ),
            dmc.Text(id="ai-msg", size="sm", c="orange"),
            dmc.Group(
                [
                    dmc.Group(
                        [
                            _input_button("Plate photo", "ai-photo-open", "tabler:photo"),
                            _input_button("Barcode", "ai-scan-open", "tabler:barcode"),
                            _input_button("Label", "ai-label-open", "tabler:file-description"),
                        ],
                        gap=6,
                    ),
                    dmc.Button(
                        GO_LABELS[mode],
                        id="ai-go",
                        leftSection=icon("tabler:sparkles", 16),
                        ml="auto",  # stays on the right when it wraps on a phone
                    ),
                ],
                justify="space-between",
                gap="xs",
            ),
            dmc.Text(HELP[mode], size="xs", c="dimmed"),
            dmc.Group(
                [dmc.Loader(size="xs"), dmc.Text(id="ai-activity", size="sm", c="dimmed")],
                id="ai-status",
                gap="xs",
                display="none",
            ),
            dmc.Box(id="ai-error"),
            dmc.Stack(id="ai-review", gap="xs"),
            dmc.Box(when_inputs("ai"), id="ai-when", display="none"),
            dmc.Box(id="ai-effect"),
            dmc.Group(
                [
                    dmc.Text(id="ai-total", size="sm", fw=600),
                    dmc.Group(actions, gap="xs", justify="flex-end"),
                ],
                id="ai-actions",
                justify="space-between",
                display="none",
            ),
            dcc.Store(id="ai-mode", data=mode),
            dcc.Store(id="ai-day", data=day.isoformat() if day else None),
            dcc.Store(id="ai-job"),  # {"id", "kind": "parse" | "label"}
            dcc.Store(id="ai-items"),  # {"items": [review rows], "message"}
            dcc.Store(id="ai-edits", data={}),  # {row key: what's entered in its inputs}
            dcc.Store(id="ai-first"),  # the earliest row's time, last put in the picker
            dcc.Store(id="ai-today"),  # {"so_far", "targets"}, for Ask's preview
            dcc.Store(id="ai-photo"),  # {"image": data URL, "nonce"}
            dcc.Store(id="ai-label-photo"),
            dcc.Store(id="ai-scan-code"),
            dcc.Store(id="ai-scan-target"),  # {"gtin", "hint"}: the code a label photo is for
            dcc.Store(id="ai-js"),
            dcc.Store(id="ai-done", data=0),  # bumped after logging or saving
            dcc.Interval(id="ai-poll", interval=POLL_MS, disabled=True),
        ],
        gap="sm",
    )


# --------------------------------------------------------------------------- review cards


def prefill(row: dict) -> dict[str, float | None]:
    """An editable row's starting values: one portion's numbers (a label, Claude's estimate)."""
    if row.get("per_serving"):
        return {k: row["per_serving"].get(k) for k in NUTRIENT_KEYS}
    if not row.get("per100"):
        return dict.fromkeys(NUTRIENT_KEYS)
    totals = item_nutrients(row)
    return {k: round(totals[k], BY_KEY[k].decimals) for k in NUTRIENT_KEYS}


def _source_line(row: dict) -> dmc.Text | dmc.Group | None:
    rest = row.get("restaurant") or {}
    match = row.get("match") or {}
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
    if match.get("source") == "off":
        return dmc.Group(
            [
                dmc.Text(
                    "From Open Food Facts (crowd-sourced): check it against the package.",
                    size="xs",
                    c="orange",
                ),
                dmc.Anchor("View", href=match.get("url"), target="_blank", size="xs"),
            ],
            gap=6,
        )
    if match:
        return dmc.Text(
            "From USDA's branded foods. Not right? Photograph the label instead.",
            size="xs",
            c="dimmed",
        )
    if row.get("source") == "label":
        text = "Read from the label: check it against the package. Numbers are for one serving."
        if row.get("label_message"):
            text = f"{row['label_message']} {text}"
        return dmc.Text(text, size="xs", c="orange")
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


def _nutrient_inputs(row: dict, entered: dict) -> dmc.SimpleGrid:
    values = {**prefill(row), **entered}
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


def _later(when: datetime, first: datetime) -> str | None:
    """'45 min later', '1 h 30 min later': how long after the first item this one was eaten."""
    minutes = round((when - first).total_seconds() / 60)
    if minutes <= 0:
        return None
    hours, minutes = divmod(minutes, 60)
    parts = ([f"{hours} h"] if hours else []) + ([f"{minutes} min"] if minutes else [])
    return " ".join(parts) + " later"


def _checks(row: dict, mode: str, edit: dict, include: dmc.Checkbox) -> dmc.Group:
    """Include, plus Add to library on the Log page (a library food is already there)."""
    children = [include]
    if row.get("source") == "my_food":
        if mode != "ask":
            children.append(dmc.Text("In your library", size="xs", c="dimmed"))
    elif mode == "log":
        children.append(
            dmc.Checkbox(
                id={"type": "ai-save-row", "index": row["key"]},
                label="Add to library",
                checked=bool(edit.get("save")),
                size="xs",
            )
        )
    return dmc.Group(children, gap="md")


def _item_card(row: dict, first: datetime, mode: str, edit: dict) -> dmc.Paper:
    key, source, error = row["key"], row.get("source"), row.get("error")
    editable = row.get("editable")
    not_found = editable and not row.get("per100") and not row.get("per_serving")
    header = [
        dmc.Text(row["name"], size="sm", fw=600, lineClamp=2, flex=1),
        dmc.Badge(
            "No match" if error else "Not found" if not_found else SOURCE_LABELS.get(source, "?"),
            color="red" if error else SOURCE_COLORS.get(source, "red"),
            variant="light",
            size="sm",
        ),
    ]
    later = _later(datetime.fromisoformat(row["eaten_at"]), first)
    said = row.get("description")
    if said and row.get("origin") == "parse":
        said = f"“{said}”"
    detail = " · ".join(x for x in (said, row["amount_label"], later) if x)
    include = dmc.Checkbox(
        id={"type": "ai-include", "index": key},
        label="Include",
        checked=edit.get("include", not error and not not_found),
        disabled=bool(error),
        size="xs",
    )
    children = [dmc.Group(header, wrap="nowrap", gap="xs"), dmc.Text(detail, size="xs", c="dimmed")]
    if error:
        children.append(
            dmc.Text(f"{error} Describe it differently, or scan it instead.", size="xs", c="red")
        )
        children.append(dmc.Box(include, display="none"))
        return dmc.Paper(dmc.Stack(children, gap=4), p="xs", withBorder=True)
    line = _source_line(row)
    if line is not None:
        children.append(line)
    if editable:
        children.append(_nutrient_inputs(row, edit.get("nuts") or {}))
        children.append(
            dmc.Group(
                [
                    dmc.Group(
                        [
                            dmc.TextInput(
                                id={"type": "ai-count", "index": key},
                                value=edit.get("count", "1"),
                                w=64,
                                size="xs",
                                inputProps={"autoComplete": "off", "aria-label": "Portions"},
                            ),
                            dmc.Text(f"× {row['amount_label']}", size="xs", c="dimmed"),
                        ],
                        gap=6,
                        wrap="nowrap",
                    ),
                    _checks(row, mode, edit, include),
                ],
                justify="space-between",
            )
        )
    else:
        children.append(dmc.Text(id={"type": "ai-kcal", "index": key}, size="xs", c="dimmed"))
        children.append(
            dmc.Group(
                [
                    dmc.NumberInput(
                        id={"type": "ai-grams", "index": key},
                        value=edit.get("grams", row["grams"]),
                        min=0,
                        step=5,
                        suffix=" g",
                        w=120,
                        size="xs",
                        inputProps={"inputMode": "decimal"},
                    ),
                    _checks(row, mode, edit, include),
                ],
                justify="space-between",
            )
        )
    return dmc.Paper(dmc.Stack(children, gap=4), p="xs", withBorder=True)


# --------------------------------------------------------------------------- inputs


clientside_callback(
    "function (n) { if (n) { window.gwcScanner.pickPhoto("
    "{store: 'ai-photo', msg: 'ai-msg', camera: false}); }"
    " return window.dash_clientside.no_update; }",
    Output("ai-js", "data"),
    Input("ai-photo-open", "n_clicks"),
    prevent_initial_call=True,
)

clientside_callback(
    "function (n) { if (n) { window.gwcScanner.open({code: 'ai-scan-code', msg: 'ai-msg'}); }"
    " return window.dash_clientside.no_update; }",
    Output("ai-js", "data", allow_duplicate=True),
    Input("ai-scan-open", "n_clicks"),
    prevent_initial_call=True,
)

clientside_callback(
    "function (n) { if (n) { window.gwcScanner.pickPhoto("
    "{store: 'ai-label-photo', msg: 'ai-msg', camera: true}); }"
    " return window.dash_clientside.no_update; }",
    Output("ai-js", "data", allow_duplicate=True),
    Input("ai-label-open", "n_clicks"),
    prevent_initial_call=True,
)

clientside_callback(
    "function (photo) { const src = photo && photo.image;"
    " return [src || null, src ? 'flex' : 'none']; }",
    Output("ai-photo-img", "src"),
    Output("ai-photo-preview", "display"),
    Input("ai-photo", "data"),
)

clientside_callback(
    "function (n) { return n ? null : window.dash_clientside.no_update; }",
    Output("ai-photo", "data", allow_duplicate=True),
    Input("ai-photo-clear", "n_clicks"),
    prevent_initial_call=True,
)


def _day(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _default_at(day_value: str | None) -> datetime:
    return default_eaten_at(_day(day_value), tz(), now())


def append_rows(
    data: dict | None, rows: list[dict], replace_origin: str | None = None
) -> dict[str, object]:
    """The review list plus `rows`, re-keyed so a key is never reused (edits are kept by key).

    `replace_origin` drops earlier rows from the same source, so itemizing again
    replaces what the last description gave rather than adding to it.
    """
    data = data or {"items": [], "message": None}
    old = data.get("items") or []
    kept = [r for r in old if not replace_origin or r.get("origin") != replace_origin]
    start = max((r["key"] for r in old), default=-1) + 1
    return {**data, "items": kept + [{**r, "key": start + i} for i, r in enumerate(rows)]}


def _note(text: str, color: str = "gray", title: str | None = None) -> dmc.Alert:
    return dmc.Alert(text, title=title, color=color, variant="light")


def lookup_barcode(raw: str, fmt: str | None, data: dict | None, day_value: str | None) -> tuple:
    """(new review data or no_update, a note for `ai-error`, the scan target)."""
    at = _default_at(day_value)
    with session_scope() as s:
        scan = barcode.resolve(s, raw, fmt, get_usda(), get_off())
        if scan.kind == "saved":
            row = library_row(foods.load_food(s, scan.food_id), at)
            return append_rows(data, [row]), None, None
    if scan.kind == "invalid":
        text = f"“{raw.strip()}” isn't a valid product barcode. Check the digits, or scan it again."
        return no_update, _note(text, "orange"), None
    if scan.kind == "store":
        text = (
            "This is the store's own label (deli, meat or bakery), so no database has it. "
            "Photograph the label, or describe the food."
        )
        return no_update, _note(text, "orange"), None
    result = scan.result
    target = {"gtin": scan.gtin, "hint": result.name_hint}
    problems = [dmc.Text(p, size="xs", c="dimmed") for p in result.problems]
    if result.match is None:
        found = f" ({result.name_hint})" if result.name_hint else ""
        note = _note(
            f"Neither USDA nor Open Food Facts has nutrition for barcode "
            f"{barcode.display(scan.gtin)}{found}. Photograph its Nutrition Facts with Label; "
            "if you add it to your library, this barcode will find it next time.",
            "orange",
            "Not found",
        )
        return no_update, dmc.Stack([note, *problems], gap=4), target
    note = dmc.Stack(problems, gap=4) if problems else None
    return append_rows(data, [match_row(result.match, at)]), note, target


@callback(
    Output("ai-job", "data"),
    Output("ai-poll", "disabled"),
    Output("ai-status", "display"),
    Output("ai-activity", "children"),
    Output("ai-error", "children"),
    Output("ai-items", "data", allow_duplicate=True),
    Output("ai-scan-target", "data"),
    Output("ai-text", "value", allow_duplicate=True),
    Input("ai-go", "n_clicks"),
    State("ai-text", "value"),
    State("ai-photo", "data"),
    State("ai-items", "data"),
    State("ai-day", "data"),
    prevent_initial_call=True,
)
def start_parse(_clicks, text, photo, data, day_value):
    text = (text or "").strip()
    image = (photo or {}).get("image")
    if not text and not image:
        hint = "Describe the food, add a photo of it, or scan its barcode first."
        return (no_update, True, "none", "", dmc.Text(hint, size="sm"), *[no_update] * 3)
    if not image and BARCODE_TEXT.fullmatch(text):
        items, note, target = lookup_barcode(text, None, data, day_value)
        found = items is not no_update
        return no_update, True, "none", "", note, items, target, "" if found else no_update
    zone, at, day = tz(), now(), _day(day_value)
    job = get_jobs().start("parse", lambda j: parse_description(text, zone, at, j, image, day))
    activity = "Looking at your photo…" if image else "Reading your description…"
    job_ref = {"id": job.id, "kind": "parse"}
    return job_ref, False, "flex", activity, None, no_update, no_update, no_update


@callback(
    Output("ai-items", "data", allow_duplicate=True),
    Output("ai-error", "children", allow_duplicate=True),
    Output("ai-scan-target", "data", allow_duplicate=True),
    Input("ai-scan-code", "data"),
    State("ai-items", "data"),
    State("ai-day", "data"),
    prevent_initial_call=True,
)
def on_scan(scanned, data, day_value):
    if not scanned or not scanned.get("code"):
        raise PreventUpdate
    return lookup_barcode(scanned["code"], scanned.get("format"), data, day_value)


@callback(
    Output("ai-job", "data", allow_duplicate=True),
    Output("ai-poll", "disabled", allow_duplicate=True),
    Output("ai-status", "display", allow_duplicate=True),
    Output("ai-activity", "children", allow_duplicate=True),
    Output("ai-error", "children", allow_duplicate=True),
    Input("ai-label-photo", "data"),
    prevent_initial_call=True,
)
def start_label(photo):
    if not photo or not photo.get("image"):
        raise PreventUpdate
    image = photo["image"]
    job = get_jobs().start("label", lambda j: read_label(image, j))
    return {"id": job.id, "kind": "label"}, False, "flex", "Sending the photo to Claude…", None


@callback(
    Output("ai-activity", "children", allow_duplicate=True),
    Output("ai-poll", "disabled", allow_duplicate=True),
    Output("ai-status", "display", allow_duplicate=True),
    Output("ai-items", "data", allow_duplicate=True),
    Output("ai-error", "children", allow_duplicate=True),
    Input("ai-poll", "n_intervals"),
    State("ai-job", "data"),
    State("ai-items", "data"),
    State("ai-scan-target", "data"),
    State("ai-day", "data"),
    prevent_initial_call=True,
)
def poll_job(_n, job_ref, data, target, day_value):
    job = get_jobs().get((job_ref or {}).get("id"))
    if job is None:
        return "", True, "none", no_update, no_update
    if job.running:
        return f"{job.activity} ({job.elapsed:.0f}s)", False, "flex", no_update, no_update
    is_label = job_ref.get("kind") == "label"
    if job.status == "error":
        title = "Couldn't read the label" if is_label else "Couldn't itemize"
        return (
            "",
            True,
            "none",
            no_update,
            dmc.Alert(job.error, color="red", variant="light", title=title),
        )
    result = job.result or {}
    if is_label:
        if not result.get("readable") or (result.get("per_serving") or {}).get("kcal") is None:
            why = result.get("message") or "Claude couldn't read the calories in that photo."
            alert = dmc.Alert(
                f"{why} Try again with the label filling the frame and no glare.",
                color="orange",
                variant="light",
                title="Couldn't read the label",
            )
            return "", True, "none", no_update, alert
        target = target or {}
        row = label_row(result, target.get("gtin"), target.get("hint"), _default_at(day_value))
        return "", True, "none", append_rows(data, [row]), None
    merged = append_rows(data, result.get("items") or [], replace_origin="parse")
    merged["message"] = result.get("message")
    if not result.get("items"):
        merged["message"] = merged["message"] or "Claude didn't find any foods in that."
    return "", True, "none", merged, None


# --------------------------------------------------------------------------- review


def today_so_far() -> dict:
    """What's logged on the current food day so far, and its targets, for Ask's preview."""
    zone, at = tz(), now()
    with session_scope() as s:
        day = current_food_day(s, zone, at)
        so_far = log.totals(log.entries_for_day(s, day, food_days_around(s, zone, day)))
        targets = day_targets(s, zone, at, day).values
    return {"so_far": so_far, "targets": targets}


@callback(
    Output("ai-review", "children"),
    Output("ai-actions", "display"),
    Output("ai-when", "display"),
    Output("ai-date", "value"),
    Output("ai-clock", "value"),
    Output("ai-first", "data"),
    Output("ai-today", "data"),
    Input("ai-items", "data"),
    State("ai-edits", "data"),
    State("ai-mode", "data"),
    State("ai-first", "data"),
)
def render_review(data, edits, mode, shown_first):
    children = []
    if data and data.get("message"):
        children.append(
            dmc.Alert(
                data["message"], color="blue", variant="light", icon=icon("tabler:info-circle")
            )
        )
    items = (data or {}).get("items") or []
    if not items:
        return children, "none", "none", no_update, no_update, None, no_update
    first = first_eaten_at(items) or now()
    edits = edits or {}
    children += [_item_card(r, first, mode, edits.get(str(r["key"]), {})) for r in items]
    # The picker follows the meal's first item; adding a later scan leaves the user's choice.
    picker = to_picker(first) if first.isoformat() != shown_first else (no_update, no_update)
    when = "none" if mode == "library" else "block"
    today = today_so_far() if mode == "ask" else no_update
    return children, "flex", when, *picker, first.isoformat(), today


REVIEW_VALUES = [
    ({"type": "ai-grams", "index": ALL}, "value"),
    ({"type": "ai-include", "index": ALL}, "checked"),
    ({"type": "ai-save-row", "index": ALL}, "checked"),
    ({"type": "ai-count", "index": ALL}, "value"),
    ({"type": "ai-nut", "index": ALL, "key": ALL}, "value"),
]
REVIEW_FIELDS = ["grams", "include", "save", "count"]


def collect_edits(values: list, ids: list) -> dict[str, dict]:
    """What's entered in each card, keyed by row key: grams, include, save, count, nuts."""
    edits: dict[str, dict] = {}
    for field, vals, idents in zip(REVIEW_FIELDS, values[:4], ids[:4], strict=True):
        for v, i in zip(vals or [], idents or [], strict=False):
            edits.setdefault(str(i["index"]), {})[field] = v
    for v, i in zip(values[4] or [], ids[4] or [], strict=False):
        nuts = edits.setdefault(str(i["index"]), {}).setdefault("nuts", {})
        nuts[i["key"]] = None if v in (None, "") else float(v)
    return edits


def chosen_rows(data: dict | None, edits: dict[str, dict]) -> list[dict]:
    """The included rows with the amounts and nutrients entered in the review."""
    out = []
    for row in (data or {}).get("items", []):
        e = edits.get(str(row["key"]), {})
        if not e.get("include") or row.get("error"):
            continue
        save = bool(e.get("save"))
        if row.get("editable"):
            values = e.get("nuts") or {}
            if not values.get("kcal"):
                continue
            try:
                count = float(log.parse_count(str(e.get("count") or "1")))
            except ValueError:
                continue
            # One portion's weight, if known, is only kept for the record and the library.
            portion = row.get("grams") or 100.0
            label = row["amount_label"]
            if count != 1:
                label = f"{log.fmt_count(count)} × {label}"
            out.append(
                {
                    **row,
                    "grams": portion * count,
                    "serving_grams": portion,
                    "serving_label": row["amount_label"],
                    "amount_label": label,
                    "totals": {k: (values.get(k) or 0.0) * count for k in NUTRIENT_KEYS},
                    "save": save,
                }
            )
        else:
            g = float(e.get("grams") or 0)
            if g > 0:
                out.append({**row, "grams": g, "totals": item_nutrients(row, g), "save": save})
    return out


def _effect(today: dict | None, chosen: list[dict]) -> dmc.Paper | None:
    """Ask's preview: today's totals with the chosen items added, against the targets."""
    if not today or not chosen:
        return None
    adds = zero()
    for row in chosen:
        adds = add(adds, row["totals"])
    so_far, targets = today["so_far"], today["targets"]
    after = add(so_far, adds)
    line = f"Adds {adds['kcal']:,.0f} kcal to the {so_far['kcal']:,.0f} you've logged today"
    if target := targets.get("kcal"):
        left = target - after["kcal"]
        line += f", leaving {left:,.0f} kcal." if left >= 0 else f": {-left:,.0f} kcal over target."
    else:
        line += "."
    return dmc.Paper(
        dmc.Stack(
            [
                dmc.Text("If you eat this today", fw=600, size="sm"),
                dmc.Text(line, size="sm", c="dimmed"),
                *nutrient_bars(after, targets),
            ],
            gap="xs",
        ),
        p="sm",
        withBorder=True,
    )


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' * (n != 1)}"


@callback(
    Output({"type": "ai-kcal", "index": ALL}, "children"),
    Output("ai-total", "children"),
    Output("ai-confirm", "children"),
    Output("ai-confirm", "disabled"),
    Output("ai-save", "children"),
    Output("ai-save", "disabled"),
    Output("ai-effect", "children"),
    Output("ai-edits", "data"),
    *(Input(i, p) for i, p in REVIEW_VALUES),
    *(State(i, "id") for i, _ in REVIEW_VALUES),
    State("ai-items", "data"),
    State("ai-today", "data"),
)
def update_totals(*args):
    values, ids, (data, today) = list(args[:5]), list(args[5:10]), args[10:]
    edits = collect_edits(values, ids)
    rows = {r["key"]: r for r in (data or {}).get("items", [])}
    lines = []
    for g, gid in zip(values[0] or [], ids[0] or [], strict=False):
        row = rows.get(gid["index"])
        if row is None:
            lines.append("")
            continue
        n = item_nutrients(row, float(g or 0))
        lines.append(
            f"{n['kcal']:,.0f} kcal · P {n['protein_g']:,.0f} · C {n['carbs_g']:,.0f} · "
            f"F {n['fat_g']:,.0f}"
        )
    chosen = chosen_rows(data, edits)
    total = sum(r["totals"]["kcal"] for r in chosen)
    savable = sum(1 for r in chosen if r.get("source") != "my_food")
    return (
        lines,
        f"{total:,.0f} kcal total",
        f"Log {_plural(len(chosen), 'item')}",
        not chosen,
        f"Save {savable} to library" if savable else "Save to library",
        savable == 0,
        _effect(today, chosen),
        edits,
    )


def _logged_message(count: int, added: int, mode: str) -> list:
    text = f"Logged {_plural(count, 'item')}."
    if added:
        text += f" Added {added} to your library."
    if mode == "ask":
        return [text + " ", dmc.Anchor("See today", href="/")]
    return [text]


@callback(
    Output("ai-items", "data", allow_duplicate=True),
    Output("ai-edits", "data", allow_duplicate=True),
    Output("ai-text", "value"),
    Output("ai-photo", "data", allow_duplicate=True),
    Output("ai-result", "children"),
    Output("ai-result", "display"),
    Output("ai-done", "data"),
    Output("ai-error", "children", allow_duplicate=True),
    Input("ai-confirm", "n_clicks"),
    Input("ai-save", "n_clicks"),
    Input("ai-discard", "n_clicks"),
    *(State(i, p) for i, p in REVIEW_VALUES),
    *(State(i, "id") for i, _ in REVIEW_VALUES),
    State("ai-items", "data"),
    State("ai-mode", "data"),
    State("ai-date", "value"),
    State("ai-clock", "value"),
    State("ai-done", "data"),
    prevent_initial_call=True,
)
def finish(_log, _save, _discard, *args):
    values, ids = list(args[:5]), list(args[5:10])
    data, mode, day, clock, done = args[10:]
    if not data:
        raise PreventUpdate
    unchanged = [no_update] * 8
    if ctx.triggered_id == "ai-discard":
        return None, {}, no_update, no_update, no_update, "none", no_update, None
    chosen = chosen_rows(data, collect_edits(values, ids))
    try:
        if ctx.triggered_id == "ai-save":
            return _save_to_library(data, chosen, mode, done)
        if mode != "log":
            chosen = [{**r, "save": False} for r in chosen]
        # The "Eaten at" picker moves the whole meal; items keep their gaps from the first.
        first = first_eaten_at(data.get("items", []))
        if first is not None:
            chosen = retime(chosen, first, from_picker(day, clock))
        with session_scope() as s:
            count = confirm_items(s, chosen, tz())
    except Exception as e:
        unchanged[-1] = dmc.Alert(f"Couldn't finish: {e}", color="red", variant="light")
        return tuple(unchanged)
    added = sum(1 for r in chosen if r.get("save"))
    message = _logged_message(count, added, mode)
    return None, {}, "", None, message, "block", (done or 0) + 1, None


def _save_to_library(data: dict, chosen: list[dict], mode: str, done: int | None) -> tuple:
    """Add the chosen rows to the library. On Ask they stay in the review, ready to log."""
    rows = [r for r in chosen if r.get("source") != "my_food"]
    with session_scope() as s:
        saved = save_items(s, rows)
        names = [food.name for food in saved.values()]
        as_library = {
            key: {
                "source": "my_food",
                "food_id": food.id,
                "name": food.name,
                "per100": per100_of(food),
                "editable": False,
                "hit": None,
                "match": None,
                "restaurant": None,
                "per_serving": None,
            }
            for key, food in saved.items()
        }
    message = f"Saved {_plural(len(names), 'food')} to your library: {', '.join(names)}."
    if mode != "ask":
        return None, {}, "", None, message, "block", (done or 0) + 1, None
    grams = {r["key"]: r["grams"] for r in rows}
    items = [
        {**r, **as_library[r["key"]], "grams": round(grams[r["key"]], 1)}
        if r["key"] in as_library
        else r
        for r in data["items"]
    ]
    them = "it" if len(names) == 1 else "them"
    message += f" Log {them} below if you eat {them} now."
    return (
        {**data, "items": items},
        no_update,
        no_update,
        no_update,
        message,
        "block",
        no_update,
        None,
    )
