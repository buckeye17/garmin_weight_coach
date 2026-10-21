import dash
import dash_mantine_components as dmc
from dash import ALL, Input, Output, State, callback, clientside_callback, ctx, dcc, no_update
from dash.exceptions import PreventUpdate

from gwc.db.models import Food, FoodSource
from gwc.db.session import session_scope
from gwc.nutrition import barcode, foods
from gwc.nutrition.log import unit_label
from gwc.nutrition.nutrients import NUTRIENTS
from gwc.nutrition.service import get_off, get_usda
from gwc.nutrition.usda import UsdaError, UsdaHit
from gwc.web.components.common import icon, page_header, triggered_click, unit_select_data
from gwc.web.components.log_modal import preview_line

BASIS_SERVING, BASIS_100G = "Per serving", "Per 100 g"


# --------------------------------------------------------------------------- layout


def _food_editor() -> dmc.Modal:
    nutrient_inputs = [
        dmc.NumberInput(
            id={"type": "fe-nutrient", "key": n.key},
            label=f"{n.label} ({n.unit})",
            min=0,
            decimalScale=2,
            hideControls=True,
            inputProps={"inputMode": "decimal"},
        )
        for n in NUTRIENTS
    ]
    return dmc.Modal(
        dmc.Stack(
            [
                dmc.TextInput(id="fe-name", label="Name", required=True),
                dmc.TextInput(id="fe-brand", label="Brand"),
                dmc.SegmentedControl(
                    id="fe-basis",
                    data=[BASIS_SERVING, BASIS_100G],
                    value=BASIS_SERVING,
                    fullWidth=True,
                ),
                dmc.Group(
                    [
                        dmc.TextInput(
                            id="fe-serving-label", label="Serving", placeholder="1 scoop", flex=1
                        ),
                        dmc.NumberInput(
                            id="fe-serving-grams",
                            label="Serving size (g)",
                            min=0,
                            decimalScale=1,
                            w=150,
                            hideControls=True,
                            inputProps={"inputMode": "decimal"},
                        ),
                    ],
                    wrap="nowrap",
                    align="flex-end",
                ),
                dmc.Text(id="fe-basis-hint", size="xs", c="dimmed"),
                dmc.SimpleGrid(nutrient_inputs, cols=2, spacing="xs"),
                dmc.Text(id="fe-error", c="red", size="sm"),
                dmc.Group(
                    [
                        dmc.Button(
                            "Remove", id="fe-archive", color="red", variant="subtle", display="none"
                        ),
                        dmc.Button("Save food", id="fe-save"),
                    ],
                    justify="space-between",
                ),
            ]
        ),
        id="fe-modal",
        title="Custom food",
        centered=True,
        size="lg",
    )


def _recipe_editor() -> dmc.Modal:
    return dmc.Modal(
        dmc.Stack(
            [
                dmc.TextInput(id="re-name", label="Recipe name", required=True),
                dmc.Divider(label="Add ingredient", labelPosition="left"),
                dmc.Select(
                    id="re-food",
                    placeholder="Search your foods…",
                    data=[],
                    searchable=True,
                    clearable=True,
                    nothingFoundMessage="Not in your foods — search USDA below",
                ),
                dmc.Group(
                    [
                        dmc.TextInput(id="re-usda-q", placeholder="…or search USDA", flex=1),
                        dmc.Button("Search", id="re-usda-btn", variant="default"),
                        dmc.ActionIcon(
                            icon("tabler:barcode", 20),
                            id="re-scan",
                            variant="light",
                            size="input-sm",
                            **{"aria-label": "Scan an ingredient's barcode"},
                        ),
                    ],
                    wrap="nowrap",
                    gap="xs",
                ),
                dmc.Text(id="re-scan-msg", size="sm"),
                dmc.Select(id="re-usda-hit", placeholder="USDA results", data=[], display="none"),
                dmc.Group(
                    [
                        dmc.NumberInput(
                            id="re-amount",
                            label="Amount",
                            min=0,
                            decimalScale=2,
                            w=110,
                            inputProps={"inputMode": "decimal"},
                        ),
                        dmc.Select(
                            id="re-unit", label="Unit", data=[], flex=1, allowDeselect=False
                        ),
                        dmc.Button("Add", id="re-add", leftSection=icon("tabler:plus", 16)),
                    ],
                    align="flex-end",
                    wrap="nowrap",
                ),
                dmc.Stack(id="re-items-list", gap=4),
                dmc.Paper(dmc.Text(id="re-totals", size="sm"), p="xs", withBorder=True),
                dmc.Group(
                    [
                        dmc.NumberInput(
                            id="re-yield",
                            label="Finished weight (g)",
                            description="Optional — weigh it after cooking",
                            min=0,
                            hideControls=True,
                            flex=1,
                        ),
                        dmc.NumberInput(
                            id="re-servings",
                            label="Servings",
                            description="Optional",
                            min=0,
                            hideControls=True,
                            flex=1,
                        ),
                    ],
                    grow=True,
                ),
                dmc.Text(id="re-error", c="red", size="sm"),
                dmc.Group(
                    [
                        dmc.Button(
                            "Remove", id="re-archive", color="red", variant="subtle", display="none"
                        ),
                        dmc.Button("Save recipe", id="re-save"),
                    ],
                    justify="space-between",
                ),
            ]
        ),
        id="re-modal",
        title="Recipe",
        centered=True,
        size="lg",
    )


def layout(**_kwargs):
    return dmc.Stack(
        [
            page_header(
                "Foods",
                "tabler:tools-kitchen-2",
                "Your custom foods, recipes and saved USDA items.",
                dmc.Group(
                    [
                        dmc.Button(
                            "New food",
                            id="new-food",
                            leftSection=icon("tabler:plus", 16),
                            size="xs",
                        ),
                        dmc.Button(
                            "New recipe",
                            id="new-recipe",
                            leftSection=icon("tabler:chef-hat", 16),
                            size="xs",
                            variant="light",
                        ),
                    ],
                    gap="xs",
                ),
            ),
            dmc.TextInput(
                id="foods-filter",
                placeholder="Filter",
                leftSection=icon("tabler:filter", 16),
                debounce=300,
            ),
            dmc.Tabs(
                [
                    dmc.TabsList(
                        [
                            dmc.TabsTab("Custom", value="custom"),
                            dmc.TabsTab("Recipes", value="recipe"),
                            dmc.TabsTab("Saved USDA", value="usda"),
                        ],
                        grow=True,
                    ),
                ],
                id="foods-tab",
                value="custom",
            ),
            dmc.Stack(id="foods-list", gap="xs"),
            dcc.Store(id="fe-id"),
            dcc.Store(id="re-id"),
            dcc.Store(id="re-items", data=[]),
            dcc.Store(id="re-scan-code"),
            dcc.Store(id="re-scan-js"),
            dcc.Store(id="re-usda-hits"),
            dcc.Store(id="foods-changed", data=0),
            _food_editor(),
            _recipe_editor(),
        ],
        gap="sm",
    )


# --------------------------------------------------------------------------- list


def _food_row(f: Food) -> dmc.Paper:
    details = []
    if f.kcal is not None:
        details.append(f"{f.kcal:,.0f} kcal/100 g")
    if f.protein_g is not None:
        details.append(f"P {f.protein_g:,.1f} g")
    if f.servings:
        details.append(
            f.servings[0].label + (f" +{len(f.servings) - 1}" if len(f.servings) > 1 else "")
        )
    if f.brand:
        details.insert(0, f.brand)
    edit = (
        [
            dmc.ActionIcon(
                icon("tabler:pencil"),
                id={"type": "food-edit", "index": f.id},
                variant="subtle",
                **{"aria-label": f"Edit {f.name}"},
            )
        ]
        if f.source is not FoodSource.USDA
        else [
            dmc.ActionIcon(
                icon("tabler:trash"),
                id={"type": "food-remove", "index": f.id},
                variant="subtle",
                color="red",
                **{"aria-label": f"Remove {f.name}"},
            )
        ]
    )
    return dmc.Paper(
        dmc.Group(
            [
                dmc.ActionIcon(
                    icon("tabler:star-filled" if f.is_favorite else "tabler:star"),
                    id={"type": "food-fav", "index": f.id},
                    variant="subtle",
                    color="yellow" if f.is_favorite else "gray",
                    **{"aria-label": "Toggle favorite"},
                ),
                dmc.Stack(
                    [
                        dmc.Text(f.name, size="sm", fw=500, lineClamp=1),
                        dmc.Text(" · ".join(details), size="xs", c="dimmed", lineClamp=1),
                    ],
                    gap=0,
                    flex=1,
                    miw=0,
                ),
                *edit,
            ],
            wrap="nowrap",
            gap="xs",
        ),
        p="xs",
        withBorder=True,
    )


@callback(
    Output("foods-list", "children"),
    Input("foods-tab", "value"),
    Input("foods-filter", "value"),
    Input("foods-changed", "data"),
)
def render_list(tab, query, _changed):
    source = FoodSource(tab)
    with session_scope() as s:
        items = foods.search_foods(s, query or "", sources=[source], limit=200)
        rows = [_food_row(f) for f in items]
    if rows:
        return rows
    empty = {
        FoodSource.CUSTOM: "No custom foods yet. Use New food to enter one from a nutrition label.",
        FoodSource.RECIPE: "No recipes yet. Use New recipe to combine ingredients.",
        FoodSource.USDA: "USDA foods you log are saved here for quick reuse.",
    }[source]
    return [dmc.Text(empty, size="sm", c="dimmed")]


@callback(
    Output("foods-changed", "data", allow_duplicate=True),
    Input({"type": "food-fav", "index": ALL}, "n_clicks"),
    Input({"type": "food-remove", "index": ALL}, "n_clicks"),
    State("foods-changed", "data"),
    prevent_initial_call=True,
)
def toggle_fav_or_remove(_fav, _remove, changed):
    clicked = triggered_click()
    if clicked is None:
        raise PreventUpdate
    with session_scope() as s:
        if clicked["type"] == "food-fav":
            food = s.get(Food, clicked["index"])
            if food is not None:
                foods.set_favorite(s, food.id, not food.is_favorite)
        else:
            foods.archive_food(s, clicked["index"])
    return (changed or 0) + 1


# --------------------------------------------------------------------------- food editor


def _nutrient_outputs():
    return Output({"type": "fe-nutrient", "key": ALL}, "value")


@callback(
    Output("fe-modal", "opened", allow_duplicate=True),
    Output("fe-id", "data"),
    Output("fe-name", "value"),
    Output("fe-brand", "value"),
    Output("fe-basis", "value"),
    Output("fe-serving-label", "value"),
    Output("fe-serving-grams", "value"),
    _nutrient_outputs(),
    Output("fe-archive", "display"),
    Output("fe-error", "children", allow_duplicate=True),
    Output("fe-modal", "title"),
    Input("new-food", "n_clicks"),
    Input({"type": "food-edit", "index": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def open_food_editor(_new, _edit):
    blank = [None] * len(NUTRIENTS)
    if ctx.triggered_id == "new-food":
        return True, None, "", "", BASIS_SERVING, "1 serving", None, blank, "none", "", "New food"
    clicked = triggered_click()
    if clicked is None:
        raise PreventUpdate
    with session_scope() as s:
        food = foods.load_food(s, clicked["index"])
        if food is None:
            raise PreventUpdate
        if food.source is FoodSource.RECIPE:
            raise PreventUpdate  # handled by the recipe editor
        serving = food.servings[0] if food.servings else None
        factor = serving.grams / 100 if serving else 1
        values = [
            None if getattr(food, n.key) is None else round(getattr(food, n.key) * factor, 2)
            for n in NUTRIENTS
        ]
        return (
            True,
            food.id,
            food.name,
            food.brand or "",
            BASIS_SERVING if serving else BASIS_100G,
            serving.label if serving else "",
            serving.grams if serving else None,
            values,
            "block",
            "",
            "Edit food",
        )


@callback(
    Output("fe-basis-hint", "children"),
    Input("fe-basis", "value"),
)
def basis_hint(basis):
    if basis == BASIS_SERVING:
        return "Copy the numbers from the nutrition label for one serving."
    return "Enter values per 100 g. The serving above is optional."


@callback(
    Output("fe-modal", "opened", allow_duplicate=True),
    Output("foods-changed", "data", allow_duplicate=True),
    Output("fe-error", "children", allow_duplicate=True),
    Input("fe-save", "n_clicks"),
    Input("fe-archive", "n_clicks"),
    State("fe-id", "data"),
    State("fe-name", "value"),
    State("fe-brand", "value"),
    State("fe-basis", "value"),
    State("fe-serving-label", "value"),
    State("fe-serving-grams", "value"),
    State({"type": "fe-nutrient", "key": ALL}, "value"),
    State("foods-changed", "data"),
    prevent_initial_call=True,
)
def save_food(_save, _archive, food_id, name, brand, basis, s_label, s_grams, values, changed):
    if ctx.triggered_id == "fe-archive":
        if food_id:
            with session_scope() as s:
                foods.archive_food(s, int(food_id))
        return False, (changed or 0) + 1, ""
    if not name or not name.strip():
        return no_update, no_update, "Give the food a name."
    entered = {
        n.key: (None if v in (None, "") else float(v))
        for n, v in zip(NUTRIENTS, values, strict=True)
    }
    grams = float(s_grams) if s_grams not in (None, "") else None
    servings = [(s_label or "1 serving", grams)] if grams else []
    if basis == BASIS_SERVING:
        if not grams:
            return no_update, no_update, "Enter the serving size in grams (it's on the label)."
        per100 = foods.per100_from_serving(entered, grams)
    else:
        per100 = entered
    try:
        with session_scope() as s:
            foods.save_custom_food(
                s,
                food_id=int(food_id) if food_id else None,
                name=name,
                brand=brand,
                per100=per100,
                servings=servings,
            )
    except ValueError as e:
        return no_update, no_update, str(e)
    return False, (changed or 0) + 1, ""


# --------------------------------------------------------------------------- recipe editor


def _ingredient_options(s) -> list[dict[str, str]]:
    items = foods.search_foods(s, "", sources=[FoodSource.CUSTOM, FoodSource.USDA], limit=500)
    return [
        {"value": str(f.id), "label": f.name + (f" ({f.brand})" if f.brand else "")} for f in items
    ]


@callback(
    Output("re-modal", "opened", allow_duplicate=True),
    Output("re-id", "data"),
    Output("re-name", "value"),
    Output("re-items", "data"),
    Output("re-yield", "value"),
    Output("re-servings", "value"),
    Output("re-food", "data"),
    Output("re-archive", "display"),
    Output("re-error", "children", allow_duplicate=True),
    Output("re-modal", "title"),
    Output("re-scan-msg", "children", allow_duplicate=True),
    Input("new-recipe", "n_clicks"),
    Input({"type": "food-edit", "index": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def open_recipe_editor(_new, _edit):
    with session_scope() as s:
        options = _ingredient_options(s)
        if ctx.triggered_id == "new-recipe":
            return True, None, "", [], None, None, options, "none", "", "New recipe", ""
        clicked = triggered_click()
        if clicked is None:
            raise PreventUpdate
        recipe = foods.load_food(s, clicked["index"])
        if recipe is None or recipe.source is not FoodSource.RECIPE:
            raise PreventUpdate
        items = [
            {
                "food_id": it.ingredient_id,
                "name": it.ingredient.name,
                "grams": it.grams,
                "label": f"{it.grams:,.0f} g",
            }
            for it in recipe.recipe_items
        ]
        raw = sum(i["grams"] for i in items)
        yield_g = (
            recipe.recipe_yield_g
            if recipe.recipe_yield_g and abs(recipe.recipe_yield_g - raw) > 0.5
            else None
        )
        n_servings = (
            round(recipe.recipe_yield_g / recipe.servings[0].grams)
            if recipe.servings and recipe.recipe_yield_g
            else None
        )
        return (
            True,
            recipe.id,
            recipe.name,
            items,
            yield_g,
            n_servings,
            options,
            "block",
            "",
            "Edit recipe",
            "",
        )


@callback(
    Output("re-usda-hit", "data"),
    Output("re-usda-hit", "display"),
    Output("re-usda-hits", "data"),
    Output("re-error", "children", allow_duplicate=True),
    Input("re-usda-btn", "n_clicks"),
    Input("re-usda-q", "n_submit"),
    State("re-usda-q", "value"),
    prevent_initial_call=True,
)
def recipe_usda_search(_c, _s, query):
    if not query:
        raise PreventUpdate
    try:
        hits = get_usda().search(query)
    except UsdaError as e:
        return [], "none", None, str(e)
    data = [{"value": str(h.fdc_id), "label": f"{h.name} ({h.data_type})"} for h in hits]
    return (
        data,
        "block" if data else "none",
        [h.to_dict() for h in hits],
        "" if data else "No USDA results.",
    )


@callback(
    Output("re-food", "data", allow_duplicate=True),
    Output("re-food", "value", allow_duplicate=True),
    Output("re-usda-hit", "display", allow_duplicate=True),
    Input("re-usda-hit", "value"),
    State("re-usda-hits", "data"),
    prevent_initial_call=True,
)
def recipe_pick_usda(fdc_id, hits):
    if not fdc_id or not hits:
        raise PreventUpdate
    hit_data = next((h for h in hits if str(h["fdc_id"]) == fdc_id), None)
    if hit_data is None:
        raise PreventUpdate
    hit = UsdaHit.from_dict(hit_data)
    with session_scope() as s:
        food = foods.cache_usda_food(s, hit, get_usda().servings_for(hit))
        food_id = food.id
        options = _ingredient_options(s)
    return options, str(food_id), "none"


clientside_callback(
    "function (n) { if (n) { window.gwcScanner.open({code: 're-scan-code', msg: 're-scan-msg'}); }"
    " return window.dash_clientside.no_update; }",
    Output("re-scan-js", "data"),
    Input("re-scan", "n_clicks"),
    prevent_initial_call=True,
)


def scan_message(scan: barcode.Scan) -> tuple[str, str]:
    """Text and color for the recipe builder's note on a scanned ingredient."""
    if scan.kind == "invalid":
        return "That isn't a valid product barcode. Try scanning it again.", "orange"
    shown = barcode.display(scan.gtin)
    if scan.kind == "saved":
        return f"{scan.food_name}: from your saved barcodes.", "dimmed"
    if scan.kind == "store":
        return (
            "That's the store's own label (deli, meat or bakery), so no database has it. "
            "Search for the food instead.",
            "orange",
        )
    match = scan.result.match
    if match is None:
        return (
            f"Neither USDA nor Open Food Facts has nutrition for barcode {shown}. Add it on "
            "Add food › Scan with Read label, then pick it here.",
            "orange",
        )
    if match.source == "off":
        return (
            f"{match.name}: from Open Food Facts (crowd-sourced; check it against the package).",
            "dimmed",
        )
    return f"{match.name}: from USDA.", "dimmed"


@callback(
    Output("re-food", "data", allow_duplicate=True),
    Output("re-food", "value", allow_duplicate=True),
    Output("re-scan-msg", "children", allow_duplicate=True),
    Output("re-scan-msg", "c"),
    Input("re-scan-code", "data"),
    prevent_initial_call=True,
)
def recipe_scan(scanned):
    """Look up a scanned ingredient and select it; a new product is saved to the user's foods."""
    if not scanned or not scanned.get("code"):
        raise PreventUpdate
    with session_scope() as s:
        scan = barcode.resolve(s, scanned["code"], scanned.get("format"), get_usda(), get_off())
        text, color = scan_message(scan)
        food_id = scan.food_id
        match = scan.result.match if scan.result else None
        if match is not None:
            food_id = barcode.save_match(s, match, get_usda()).id
        if food_id is None:
            return no_update, no_update, text, color
        return _ingredient_options(s), str(food_id), text, color


@callback(
    Output("re-unit", "data"),
    Output("re-unit", "value"),
    Output("re-amount", "value"),
    Input("re-food", "value"),
    prevent_initial_call=True,
)
def recipe_food_units(food_id):
    if not food_id:
        return [], None, None
    with session_scope() as s:
        food = foods.load_food(s, int(food_id))
        if food is None:
            raise PreventUpdate
        unit, amount = foods.default_unit(food)
        return unit_select_data(food), unit, amount


@callback(
    Output("re-items", "data", allow_duplicate=True),
    Output("re-food", "value", allow_duplicate=True),
    Output("re-error", "children", allow_duplicate=True),
    Input("re-add", "n_clicks"),
    Input({"type": "re-rm", "index": ALL}, "n_clicks"),
    State("re-food", "value"),
    State("re-amount", "value"),
    State("re-unit", "value"),
    State("re-items", "data"),
    prevent_initial_call=True,
)
def recipe_items(_add, _rm, food_id, amount, unit, items):
    items = list(items or [])
    if ctx.triggered_id == "re-add":
        if not food_id or not amount or not unit:
            return no_update, no_update, "Pick an ingredient and amount first."
        with session_scope() as s:
            food = foods.load_food(s, int(food_id))
            option = foods.resolve_unit(food, unit)
            grams = float(amount) * option.grams
            label = unit_label(float(amount), option.label)
            items.append({"food_id": food.id, "name": food.name, "grams": grams, "label": label})
        return items, None, ""
    clicked = triggered_click()
    if clicked is None:
        raise PreventUpdate
    del items[clicked["index"]]
    return items, no_update, ""


@callback(
    Output("re-items-list", "children"),
    Output("re-totals", "children"),
    Input("re-items", "data"),
    Input("re-yield", "value"),
    Input("re-servings", "value"),
)
def recipe_preview(items, yield_g, n_servings):
    items = items or []
    rows = [
        dmc.Group(
            [
                dmc.Text(f"{it['name']} — {it['label']}", size="sm", flex=1, lineClamp=1),
                dmc.ActionIcon(
                    icon("tabler:x", 14),
                    id={"type": "re-rm", "index": i},
                    variant="subtle",
                    color="gray",
                    size="sm",
                ),
            ],
            wrap="nowrap",
        )
        for i, it in enumerate(items)
    ] or [dmc.Text("No ingredients yet.", size="sm", c="dimmed")]
    if not items:
        return rows, "Totals appear as you add ingredients."
    with session_scope() as s:
        totals, raw = foods.recipe_totals(s, [(it["food_id"], it["grams"]) for it in items])
    final = float(yield_g) if yield_g else raw
    text = f"Whole recipe ({final:,.0f} g): " + preview_line(totals)
    if n_servings:
        per = {k: v / float(n_servings) for k, v in totals.items()}
        text += f"  |  Per serving ({final / float(n_servings):,.0f} g): {per['kcal']:,.0f} kcal"
    return rows, text


@callback(
    Output("re-modal", "opened", allow_duplicate=True),
    Output("foods-changed", "data", allow_duplicate=True),
    Output("re-error", "children", allow_duplicate=True),
    Output("foods-tab", "value"),
    Input("re-save", "n_clicks"),
    Input("re-archive", "n_clicks"),
    State("re-id", "data"),
    State("re-name", "value"),
    State("re-items", "data"),
    State("re-yield", "value"),
    State("re-servings", "value"),
    State("foods-changed", "data"),
    prevent_initial_call=True,
)
def save_recipe(_save, _archive, recipe_id, name, items, yield_g, n_servings, changed):
    if ctx.triggered_id == "re-archive":
        if recipe_id:
            with session_scope() as s:
                foods.archive_food(s, int(recipe_id))
        return False, (changed or 0) + 1, "", "recipe"
    if not name or not name.strip():
        return no_update, no_update, "Give the recipe a name.", no_update
    try:
        with session_scope() as s:
            foods.save_recipe(
                s,
                food_id=int(recipe_id) if recipe_id else None,
                name=name,
                items=[(it["food_id"], it["grams"]) for it in items or []],
                yield_g=float(yield_g) if yield_g else None,
                servings=int(n_servings) if n_servings else None,
            )
    except ValueError as e:
        return no_update, no_update, str(e), no_update
    return False, (changed or 0) + 1, "", "recipe"


dash.register_page(
    __name__,
    path="/foods",
    name="Foods",
    title="Foods · Weight Coach",
    order=5,
    layout=layout,
)
