"""Log food: pick it from the library, or describe or scan a one-off (components/describe.py).

`?day=YYYY-MM-DD` fills in a past food day (Today's "Add to this day"): new
entries default to midday on it.
"""

import time
from datetime import date

import dash
import dash_mantine_components as dmc
from dash import ALL, Input, Output, callback, no_update
from dash.exceptions import PreventUpdate

from gwc.analysis.food_day import current_food_day
from gwc.db.models import Food, FoodSource
from gwc.db.session import session_scope
from gwc.nutrition import foods
from gwc.web.components.common import icon, now, page_header, triggered_click, tz
from gwc.web.components.describe import describe_panel
from gwc.web.components.log_modal import (
    ALL_CATEGORIES,
    category_chips,
    log_modal,
    serving_summary,
)
from gwc.web.format import fmt_day

SOURCE_BADGE = {FoodSource.USDA: "USDA", FoodSource.CUSTOM: "Mine", FoodSource.RECIPE: "Recipe"}


def past_day(value: str | None) -> date | None:
    """The `day` query parameter, if it names a food day before the current one."""
    try:
        day = date.fromisoformat(value) if value else None
    except ValueError:
        return None
    if day is None:
        return None
    with session_scope() as s:
        return day if day < current_food_day(s, tz(), now()) else None


def _food_row(f: Food) -> dmc.UnstyledButton:
    bits = [serving_summary(f), SOURCE_BADGE[f.source]]
    if f.brand:
        bits.append(f.brand)
    bits += f.category_names
    star = [dmc.Box(icon("tabler:star-filled", 14), c="yellow.6")] if f.is_favorite else []
    return dmc.UnstyledButton(
        dmc.Paper(
            dmc.Group(
                [
                    dmc.Stack(
                        [
                            dmc.Group(
                                [*star, dmc.Text(f.name, size="sm", fw=500, lineClamp=2)],
                                gap=4,
                                wrap="nowrap",
                            ),
                            dmc.Text(" · ".join(bits), size="xs", c="dimmed", lineClamp=1),
                        ],
                        gap=0,
                        flex=1,
                        miw=0,
                    ),
                    dmc.ThemeIcon(icon("tabler:plus", 16), variant="light", size="lg"),
                ],
                wrap="nowrap",
                gap="sm",
            ),
            p="xs",
            withBorder=True,
            className="gwc-entry",
        ),
        id={"type": "my-food", "index": f.id},
        w="100%",
        **{"aria-label": f"Log {f.name}"},
    )


def _library_tab() -> dmc.Stack:
    return dmc.Stack(
        [
            dmc.TextInput(
                id="mine-query",
                placeholder="Search your foods and recipes",
                leftSection=icon("tabler:search", 16),
                debounce=300,
            ),
            dmc.ChipGroup(
                dmc.Group(id="mine-cat-chips", gap=6),
                id="mine-cat",
                value=ALL_CATEGORIES,
                multiple=False,
            ),
            dmc.Stack(id="mine-results", gap="xs"),
        ],
        pt="sm",
        gap="sm",
    )


def layout(day: str | None = None, **_kwargs):
    filling = past_day(day)
    if filling:
        subtitle = f"Adding to {fmt_day(filling)}."
        back = dmc.Anchor(
            dmc.Button(
                f"Back to {fmt_day(filling)}",
                variant="default",
                size="xs",
                leftSection=icon("tabler:arrow-left", 14),
            ),
            href=f"/?day={filling.isoformat()}",
        )
    else:
        subtitle = "From your library, or describe or scan what you ate."
        back = None
    return dmc.Stack(
        [
            page_header("Log food", "tabler:circle-plus", subtitle, back),
            dmc.Alert(
                id="logm-result",
                color="brand",
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
                                "From library", value="library", leftSection=icon("tabler:books")
                            ),
                            dmc.TabsTab(
                                "Describe or scan",
                                value="describe",
                                leftSection=icon("tabler:sparkles"),
                            ),
                        ],
                        grow=True,
                    ),
                    dmc.TabsPanel(_library_tab(), value="library"),
                    dmc.TabsPanel(
                        dmc.Box(describe_panel("log", filling), pt="sm"), value="describe"
                    ),
                ],
                id="log-tabs",
                value="library",
                persistence=True,
            ),
            *log_modal(filling),
        ],
        gap="md",
    )


@callback(
    Output("mine-results", "children"),
    Output("mine-cat-chips", "children"),
    Output("mine-cat", "value"),
    Input("mine-query", "value"),
    Input("mine-cat", "value"),
    Input("log-tabs", "value"),
    Input("logm-done", "data"),
    Input("logm-cats-saved", "data"),
    Input("ai-done", "data"),
)
def list_mine(query, category, tab, _done, _cats_saved, _ai_done):
    if tab != "library":
        return no_update, no_update, no_update
    with session_scope() as s:
        categories = foods.all_categories(s)
        if category not in categories:
            category = ALL_CATEGORIES  # e.g. its last food was recategorized
        chosen = None if category == ALL_CATEGORIES else category
        items = foods.search_foods(s, query or "", category=chosen)
        rows = [_food_row(f) for f in items]
        if not rows:
            text = (
                "No matching foods."
                if query or chosen
                else "Your library is empty. Use Describe or scan, and tick Add to library for "
                "foods you eat often, or add them on the Library page."
            )
            rows = [dmc.Text(text, size="sm", c="dimmed")]
    hint = "Give foods categories like Snack or Restaurant in the log dialog, then filter here."
    return rows, category_chips(categories, hint), category


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
    path="/log",
    redirect_from=["/add"],
    name="Log food",
    title="Log food · Weight Coach",
    order=1,
    layout=layout,
)
