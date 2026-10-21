import time

import dash
import dash_mantine_components as dmc
from dash import ALL, Input, Output, State, callback, dcc, no_update
from dash.exceptions import PreventUpdate

from gwc.db.models import Food, FoodSource
from gwc.db.session import session_scope
from gwc.nutrition import foods
from gwc.nutrition.service import get_usda
from gwc.nutrition.usda import UsdaError, UsdaHit
from gwc.web.components.ai_log import ai_panel
from gwc.web.components.common import icon, triggered_click
from gwc.web.components.log_modal import log_modal

SOURCE_BADGE = {FoodSource.USDA: "USDA", FoodSource.CUSTOM: "Mine", FoodSource.RECIPE: "Recipe"}


def _result_row(title: str, subtitle: str, kcal: float | None, button_id: dict) -> dmc.Paper:
    return dmc.Paper(
        dmc.Group(
            [
                dmc.Stack(
                    [
                        dmc.Text(title, size="sm", fw=500, lineClamp=2),
                        dmc.Text(subtitle, size="xs", c="dimmed", lineClamp=1),
                    ],
                    gap=0,
                    flex=1,
                    miw=0,
                ),
                dmc.Text(
                    f"{kcal:,.0f} kcal/100 g" if kcal is not None else "—",
                    size="xs",
                    c="dimmed",
                    style={"whiteSpace": "nowrap"},
                ),
                dmc.ActionIcon(
                    icon("tabler:plus"),
                    id=button_id,
                    variant="light",
                    size="lg",
                    **{"aria-label": f"Log {title}"},
                ),
            ],
            wrap="nowrap",
            gap="sm",
        ),
        p="xs",
        withBorder=True,
    )


def _food_rows(items: list[Food]) -> list:
    if not items:
        return [dmc.Text("No matching foods yet.", size="sm", c="dimmed")]
    rows = []
    for f in items:
        bits = [SOURCE_BADGE[f.source]]
        if f.is_favorite:
            bits.insert(0, "★")
        if f.brand:
            bits.append(f.brand)
        rows.append(
            _result_row(f.name, " · ".join(bits), f.kcal, {"type": "my-food", "index": f.id})
        )
    return rows


def layout(**_kwargs):
    return dmc.Stack(
        [
            dmc.Group([icon("tabler:circle-plus", 28), dmc.Title("Add food", order=2)], gap="sm"),
            dmc.Alert(
                id="logm-result",
                color="teal",
                variant="light",
                display="none",
                withCloseButton=True,
                icon=icon("tabler:circle-check"),
            ),
            dmc.Tabs(
                [
                    dmc.TabsList(
                        [
                            dmc.TabsTab(
                                "Search USDA", value="usda", leftSection=icon("tabler:search")
                            ),
                            dmc.TabsTab("My foods", value="mine", leftSection=icon("tabler:star")),
                            dmc.TabsTab(
                                "Describe", value="ai", leftSection=icon("tabler:sparkles")
                            ),
                        ],
                        grow=True,
                    ),
                    dmc.TabsPanel(
                        dmc.Stack(
                            [
                                dmc.Group(
                                    [
                                        dmc.TextInput(
                                            id="usda-query",
                                            placeholder="e.g. greek yogurt, banana, ribeye",
                                            flex=1,
                                            leftSection=icon("tabler:search", 16),
                                        ),
                                        dmc.Button("Search", id="usda-search-btn"),
                                    ],
                                    wrap="nowrap",
                                ),
                                dmc.SegmentedControl(
                                    id="usda-kind",
                                    data=["Generic", "Branded"],
                                    value="Generic",
                                    fullWidth=True,
                                ),
                                dmc.Text(id="usda-status", size="xs", c="dimmed"),
                                dcc.Loading(dmc.Stack(id="usda-results", gap="xs"), type="dot"),
                            ],
                            pt="sm",
                        ),
                        value="usda",
                    ),
                    dmc.TabsPanel(
                        dmc.Stack(
                            [
                                dmc.TextInput(
                                    id="mine-query",
                                    placeholder="Filter your foods, recipes and saved USDA foods",
                                    leftSection=icon("tabler:filter", 16),
                                    debounce=300,
                                ),
                                dmc.Stack(id="mine-results", gap="xs"),
                            ],
                            pt="sm",
                        ),
                        value="mine",
                    ),
                    dmc.TabsPanel(ai_panel(), value="ai"),
                ],
                id="add-tabs",
                value="usda",
                persistence=True,
            ),
            dmc.Anchor("Create a custom food or recipe →", href="/foods", size="sm"),
            dcc.Store(id="usda-hits"),
            *log_modal(),
        ],
        gap="md",
    )


@callback(
    Output("usda-results", "children"),
    Output("usda-status", "children"),
    Output("usda-hits", "data"),
    Input("usda-search-btn", "n_clicks"),
    Input("usda-query", "n_submit"),
    Input("usda-kind", "value"),
    State("usda-query", "value"),
    prevent_initial_call=True,
)
def search_usda(_clicks, _submit, kind, query):
    if not query or not query.strip():
        return [], "", None
    try:
        hits = get_usda().search(query, branded=kind == "Branded")
    except UsdaError as e:
        return [dmc.Alert(str(e), color="orange", variant="light")], "", None
    rows = []
    for h in hits:
        sub = " · ".join(x for x in (h.brand, h.data_type) if x)
        rows.append(
            _result_row(h.name, sub, h.per100.get("kcal"), {"type": "usda-hit", "index": h.fdc_id})
        )
    status = f"{len(hits)} results" if hits else "No results — try fewer words or the other tab."
    return rows, status, [h.to_dict() for h in hits]


@callback(
    Output("logm-open", "data", allow_duplicate=True),
    Input({"type": "usda-hit", "index": ALL}, "n_clicks"),
    State("usda-hits", "data"),
    prevent_initial_call=True,
)
def pick_usda(_clicks, hits):
    clicked = triggered_click()
    if clicked is None or not hits:
        raise PreventUpdate
    hit_data = next((h for h in hits if h["fdc_id"] == clicked["index"]), None)
    if hit_data is None:
        raise PreventUpdate
    hit = UsdaHit.from_dict(hit_data)
    servings = get_usda().servings_for(hit)
    with session_scope() as s:
        food = foods.cache_usda_food(s, hit, servings)
        food_id = food.id
    return {"food_id": food_id, "nonce": time.time()}


@callback(
    Output("mine-results", "children"),
    Input("mine-query", "value"),
    Input("add-tabs", "value"),
    Input("logm-done", "data"),
)
def list_mine(query, tab, _done):
    if tab != "mine":
        return no_update
    with session_scope() as s:
        return _food_rows(foods.search_foods(s, query or ""))


@callback(
    Output("logm-open", "data", allow_duplicate=True),
    Input({"type": "my-food", "index": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def pick_mine(_clicks):
    clicked = triggered_click()
    if clicked is None:
        raise PreventUpdate
    return {"food_id": clicked["index"], "nonce": time.time()}


dash.register_page(
    __name__,
    path="/add",
    name="Add food",
    title="Add food · Weight Coach",
    order=1,
    layout=layout,
)
