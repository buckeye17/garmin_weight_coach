"""'Scan' tab on Add food: barcode lookups and nutrition-label photos.

The camera and photo handling live in assets/scanner.js, which writes a scanned
code to `scan-code` and a downscaled label photo to `label-photo`. A barcode the
user has confirmed before opens the log dialog straight away; otherwise it's
looked up (USDA, then Open Food Facts) and shown for the user to confirm. A label
photo is read by Claude into a form the user checks before the food is saved.
"""

import time

import dash_mantine_components as dmc
from dash import Input, Output, State, callback, clientside_callback, ctx, dcc, no_update
from dash.exceptions import PreventUpdate

from gwc.coach.jobs import get_jobs
from gwc.coach.label import read_label
from gwc.db.session import session_scope
from gwc.nutrition import barcode
from gwc.nutrition.nutrients import BY_KEY, NUTRIENT_KEYS, scale
from gwc.nutrition.service import get_off, get_usda
from gwc.web.components.ai_log import POLL_MS, SHORT_LABELS
from gwc.web.components.common import icon

SOURCE_BADGES = {"usda": ("USDA", "blue"), "off": ("Open Food Facts", "orange")}


def _nut_id(key: str) -> str:
    return f"label-nut-{key}"


def scan_panel() -> dmc.Stack:
    return dmc.Stack(
        [
            dmc.SimpleGrid(
                [
                    dmc.Button(
                        "Scan barcode",
                        id="scan-open",
                        size="md",
                        px="xs",
                        leftSection=icon("tabler:barcode", 20),
                    ),
                    dmc.Button(
                        "Read label",
                        id="label-open",
                        size="md",
                        px="xs",
                        variant="light",
                        leftSection=icon("tabler:camera", 20),
                    ),
                ],
                cols=2,
                spacing="xs",
            ),
            dmc.Group(
                [
                    dmc.TextInput(
                        id="scan-manual",
                        placeholder="Or type the number",
                        flex=1,
                        leftSection=icon("tabler:number", 16),
                        inputProps={"inputMode": "numeric", "autoComplete": "off"},
                    ),
                    dmc.Button("Look up", id="scan-lookup", variant="default"),
                ],
                wrap="nowrap",
            ),
            dmc.Text(id="scan-js-msg", size="sm", c="orange"),
            dcc.Loading(dmc.Box(id="scan-result"), type="dot"),
            dmc.Group(
                [dmc.Loader(size="xs"), dmc.Text(id="label-activity", size="sm", c="dimmed")],
                id="label-status",
                gap="xs",
                display="none",
            ),
            dmc.Box(id="label-error"),
            _label_form(),
            dmc.Text(
                "Barcodes are looked up in USDA's branded foods, then Open Food Facts. "
                "Products you log are remembered, so scanning them again goes straight to "
                "logging. No barcode, or the wrong product? Use Read label to photograph "
                "the Nutrition Facts.",
                size="xs",
                c="dimmed",
            ),
            dcc.Store(id="scan-js"),
            dcc.Store(id="scan-code"),
            dcc.Store(id="scan-match"),
            dcc.Store(id="scan-target"),  # {"gtin", "hint"}: the code a label photo is for
            dcc.Store(id="label-photo"),
            dcc.Store(id="label-job"),
            dcc.Interval(id="label-poll", interval=POLL_MS, disabled=True),
        ],
        gap="sm",
        pt="sm",
    )


def _label_form() -> dmc.Paper:
    return dmc.Paper(
        dmc.Stack(
            [
                dmc.Text("Check the label", fw=600),
                dmc.Alert(id="label-msg", color="blue", variant="light", display="none"),
                dmc.TextInput(id="label-name", label="Name", required=True),
                dmc.TextInput(id="label-brand", label="Brand"),
                dmc.Group(
                    [
                        dmc.TextInput(
                            id="label-serving", label="Serving", placeholder="2/3 cup", flex=1
                        ),
                        dmc.NumberInput(
                            id="label-grams",
                            label="Serving size",
                            suffix=" g",
                            min=0,
                            w=130,
                            hideControls=True,
                            inputProps={"inputMode": "decimal"},
                        ),
                    ],
                    wrap="nowrap",
                    align="flex-end",
                ),
                dmc.Text("Per serving", size="sm", fw=500),
                dmc.SimpleGrid(
                    [
                        dmc.NumberInput(
                            id=_nut_id(k),
                            label=SHORT_LABELS.get(k, f"{BY_KEY[k].label} (g)"),
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
                ),
                dmc.Checkbox(id="label-link", checked=True, size="sm", display="none"),
                dmc.Text(id="label-form-error", size="sm", c="red"),
                dmc.Group(
                    [
                        dmc.Button("Discard", id="label-discard", variant="subtle", color="gray"),
                        dmc.Button("Save & log", id="label-save", leftSection=icon("tabler:check")),
                    ],
                    justify="flex-end",
                    gap="xs",
                ),
            ],
            gap="xs",
        ),
        id="label-form",
        p="sm",
        withBorder=True,
        display="none",
    )


def _per_serving_line(per100: dict, grams: float) -> str:
    n = scale(per100, grams)
    return (
        f"{n['kcal']:,.0f} kcal · P {n['protein_g']:,.0f} · C {n['carbs_g']:,.0f} · "
        f"F {n['fat_g']:,.0f}"
    )


def match_card(match: barcode.BarcodeMatch) -> dmc.Paper:
    badge, color = SOURCE_BADGES[match.source]
    if match.servings:
        sv = match.servings[0]
        nutrition = f"{sv.label}: {_per_serving_line(match.per100, sv.grams)}"
    else:
        nutrition = f"100 g: {_per_serving_line(match.per100, 100)}"
    children = [
        dmc.Group(
            [
                dmc.Text(match.name, size="sm", fw=600, lineClamp=2, flex=1),
                dmc.Badge(badge, color=color, variant="light", size="sm"),
            ],
            wrap="nowrap",
            gap="xs",
            align="flex-start",
        ),
        dmc.Text(
            " · ".join(x for x in (match.brand, f"Barcode {barcode.display(match.gtin)}") if x),
            size="xs",
            c="dimmed",
        ),
        dmc.Text(nutrition, size="sm"),
    ]
    if match.source == "off":
        children.append(
            dmc.Group(
                [
                    dmc.Text("Crowd-sourced: check it against the package.", size="xs", c="orange"),
                    dmc.Anchor("View", href=match.url, target="_blank", size="xs"),
                ],
                gap=6,
            )
        )
    children.append(
        dmc.Group(
            [
                dmc.Text("Not right? Use Read label.", size="xs", c="dimmed"),
                dmc.Button("Log it", id="scan-pick", leftSection=icon("tabler:plus", 16)),
            ],
            justify="space-between",
        )
    )
    return dmc.Paper(dmc.Stack(children, gap=6), p="sm", withBorder=True)


def _note(text: str, color: str = "gray", title: str | None = None) -> dmc.Alert:
    return dmc.Alert(text, title=title, color=color, variant="light")


def _open_log(food_id: int) -> dict:
    return {"food_id": food_id, "nonce": time.time()}


clientside_callback(
    "function (n) { if (n) { window.gwcScanner.open(); }"
    " return window.dash_clientside.no_update; }",
    Output("scan-js", "data"),
    Input("scan-open", "n_clicks"),
    prevent_initial_call=True,
)

clientside_callback(
    "function (n) { if (n) { window.gwcScanner.pickPhoto(); }"
    " return window.dash_clientside.no_update; }",
    Output("scan-js", "data", allow_duplicate=True),
    Input("label-open", "n_clicks"),
    prevent_initial_call=True,
)


@callback(
    Output("scan-result", "children"),
    Output("scan-match", "data"),
    Output("scan-target", "data"),
    Output("scan-manual", "value"),
    Output("logm-open", "data", allow_duplicate=True),
    Output("label-form", "display", allow_duplicate=True),
    Output("label-error", "children", allow_duplicate=True),
    Input("scan-code", "data"),
    Input("scan-lookup", "n_clicks"),
    Input("scan-manual", "n_submit"),
    State("scan-manual", "value"),
    prevent_initial_call=True,
)
def lookup_code(scanned, _clicks, _submit, typed):
    if ctx.triggered_id == "scan-code":
        if not scanned:
            raise PreventUpdate
        raw, fmt = scanned.get("code"), scanned.get("format")
    else:
        raw, fmt = typed, None
    if not raw or not raw.strip():
        raise PreventUpdate
    with session_scope() as s:
        scan = barcode.resolve(s, raw, fmt, get_usda(), get_off())
    if scan.kind == "invalid":
        note = _note(
            f"“{raw.strip()}” isn't a valid product barcode. Check the digits, or scan it again.",
            "orange",
        )
        return note, None, None, no_update, no_update, "none", None
    shown = barcode.display(scan.gtin)
    if scan.kind == "saved":
        note = _note(f"{scan.food_name} — from your saved barcodes.", "brand")
        return note, None, None, shown, _open_log(scan.food_id), "none", None
    if scan.kind == "store":
        note = _note(
            "This is the store's own label (deli, meat or bakery), so no database has it. "
            "Use Read label, or search for the food.",
            "orange",
        )
        return note, None, None, shown, no_update, "none", None
    result = scan.result
    problems = [dmc.Text(p, size="xs", c="dimmed") for p in result.problems]
    target = {"gtin": scan.gtin, "hint": result.name_hint}
    if result.match is None:
        found_name = f" ({result.name_hint})" if result.name_hint else ""
        note = _note(
            f"Neither USDA nor Open Food Facts has nutrition for barcode {shown}{found_name}. "
            "Use Read label to photograph the Nutrition Facts; it'll be saved under this "
            "barcode for next time.",
            "orange",
            "Not found",
        )
        return dmc.Stack([note, *problems], gap=4), None, target, shown, no_update, "none", None
    card = dmc.Stack([match_card(result.match), *problems], gap=4)
    return card, result.match.to_dict(), target, shown, no_update, "none", None


@callback(
    Output("logm-open", "data", allow_duplicate=True),
    Output("scan-result", "children", allow_duplicate=True),
    Output("scan-match", "data", allow_duplicate=True),
    Output("scan-target", "data", allow_duplicate=True),
    Input("scan-pick", "n_clicks"),
    State("scan-match", "data"),
    prevent_initial_call=True,
)
def pick_match(n_clicks, data):
    if not n_clicks or not data:
        raise PreventUpdate
    match = barcode.BarcodeMatch.from_dict(data)
    with session_scope() as s:
        food = barcode.save_match(s, match, get_usda())
        food_id, name = food.id, food.name
    note = _note(f"Saved {name}. Next time this barcode opens it straight away.", "brand")
    return _open_log(food_id), note, None, None


@callback(
    Output("label-job", "data"),
    Output("label-poll", "disabled"),
    Output("label-status", "display"),
    Output("label-activity", "children"),
    Output("label-error", "children", allow_duplicate=True),
    Output("label-form", "display", allow_duplicate=True),
    Input("label-photo", "data"),
    prevent_initial_call=True,
)
def start_label(photo):
    if not photo or not photo.get("image"):
        raise PreventUpdate
    image = photo["image"]
    job = get_jobs().start("label", lambda j: read_label(image, j))
    return job.id, False, "flex", "Sending the photo to Claude…", None, "none"


LABEL_FIELDS = [
    Output("label-name", "value"),
    Output("label-brand", "value"),
    Output("label-serving", "value"),
    Output("label-grams", "value"),
    *(Output(_nut_id(k), "value") for k in NUTRIENT_KEYS),
    Output("label-msg", "children"),
    Output("label-msg", "display"),
    Output("label-link", "label"),
    Output("label-link", "checked"),
    Output("label-link", "display"),
    Output("label-form-error", "children"),
]


def form_values(result: dict, target: dict | None) -> list:
    """Values for LABEL_FIELDS from Claude's reading of the label."""
    per = result.get("per_serving") or {}
    gtin = (target or {}).get("gtin")
    message = result.get("message")
    return [
        result.get("name") or (target or {}).get("hint") or "",
        result.get("brand") or "",
        result.get("serving_label") or "1 serving",
        result.get("serving_grams"),
        *(per.get(k) for k in NUTRIENT_KEYS),
        message or "",
        "block" if message else "none",
        f"Save it for barcode {barcode.display(gtin)}" if gtin else "",
        bool(gtin),
        "flex" if gtin else "none",
        "",
    ]


@callback(
    Output("label-activity", "children", allow_duplicate=True),
    Output("label-poll", "disabled", allow_duplicate=True),
    Output("label-status", "display", allow_duplicate=True),
    Output("label-error", "children", allow_duplicate=True),
    Output("label-form", "display", allow_duplicate=True),
    *LABEL_FIELDS,
    Input("label-poll", "n_intervals"),
    State("label-job", "data"),
    State("scan-target", "data"),
    prevent_initial_call=True,
)
def poll_label(_n, job_id, target):
    unchanged = [no_update] * len(LABEL_FIELDS)
    job = get_jobs().get(job_id)
    if job is None:
        return "", True, "none", no_update, no_update, *unchanged
    if job.running:
        return f"{job.activity} ({job.elapsed:.0f}s)", False, "flex", None, "none", *unchanged
    if job.status == "error":
        alert = dmc.Alert(job.error, color="red", variant="light", title="Couldn't read the label")
        return "", True, "none", alert, "none", *unchanged
    result = job.result or {}
    if not result.get("readable") or result["per_serving"].get("kcal") is None:
        why = result.get("message") or "Claude couldn't read the calories in that photo."
        alert = dmc.Alert(
            f"{why} Try again with the label filling the frame and no glare.",
            color="orange",
            variant="light",
            title="Couldn't read the label",
        )
        return "", True, "none", alert, "none", *unchanged
    return "", True, "none", None, "block", *form_values(result, target)


@callback(
    Output("logm-open", "data", allow_duplicate=True),
    Output("label-form", "display", allow_duplicate=True),
    Output("label-form-error", "children", allow_duplicate=True),
    Output("scan-result", "children", allow_duplicate=True),
    Output("scan-target", "data", allow_duplicate=True),
    Input("label-save", "n_clicks"),
    Input("label-discard", "n_clicks"),
    State("label-name", "value"),
    State("label-brand", "value"),
    State("label-serving", "value"),
    State("label-grams", "value"),
    *(State(_nut_id(k), "value") for k in NUTRIENT_KEYS),
    State("label-link", "checked"),
    State("scan-target", "data"),
    prevent_initial_call=True,
)
def save_label(_save, _discard, name, brand, serving, grams, *rest):
    *values, link, target = rest
    if ctx.triggered_id == "label-discard":
        return no_update, "none", "", no_update, no_update
    if not (name or "").strip():
        return no_update, no_update, "Enter a name.", no_update, no_update
    if not grams or float(grams) <= 0:
        return (
            no_update,
            no_update,
            "Enter the serving size in grams (it's on the label).",
            no_update,
            no_update,
        )
    per_serving = {
        k: None if v in (None, "") else float(v) for k, v in zip(NUTRIENT_KEYS, values, strict=True)
    }
    if per_serving["kcal"] is None:
        return no_update, no_update, "Enter the calories.", no_update, no_update
    gtin = (target or {}).get("gtin") if link else None
    with session_scope() as s:
        food = barcode.save_label_food(
            s,
            gtin=gtin,
            name=name,
            brand=brand,
            serving_label=(serving or "").strip() or "1 serving",
            serving_grams=float(grams),
            per_serving=per_serving,
        )
        food_id, saved = food.id, food.name
    text = f"Saved {saved} to your foods"
    text += f" under barcode {barcode.display(gtin)}." if gtin else "."
    return _open_log(food_id), "none", "", _note(text, "brand"), None
