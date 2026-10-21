from datetime import timedelta

import dash
import dash_mantine_components as dmc
import plotly.graph_objects as go
from dash import Input, Output, callback

from gwc.analysis import snapshot as snap_mod
from gwc.analysis.energy import DAYS_PER_MONTH
from gwc.analysis.projection import Projection, scenarios
from gwc.analysis.snapshot import Snapshot, build_snapshot
from gwc.analysis.targets import day_targets, goal_kg_per_month
from gwc.analysis.units import WeightUnit, fmt_weight, kg_to, weight_unit
from gwc.db.session import session_scope
from gwc.web.components import charts
from gwc.web.components.analysis_text import fmt_kcal, fmt_rate, tdee_line
from gwc.web.components.common import card, icon, now, tz
from gwc.web.format import fmt_day

HISTORY_DAYS = 28


def _stat(label: str, value_id: str, sub_id: str) -> dmc.Card:
    return card(
        dmc.Stack(
            [
                dmc.Text(label, size="xs", c="dimmed", tt="uppercase", fw=600),
                dmc.Text(id=value_id, size="xl", fw=700),
                dmc.Text(id=sub_id, size="xs", c="dimmed"),
            ],
            gap=2,
        ),
        padding="sm",
    )


def layout(**_kwargs):
    return dmc.Stack(
        [
            dmc.Group([icon("tabler:chart-line", 28), dmc.Title("Projection", order=2)], gap="sm"),
            dmc.Stack(id="proj-alerts", gap="xs"),
            dmc.SimpleGrid(
                [
                    _stat("Trend weight", "proj-weight", "proj-weight-sub"),
                    _stat("Daily burn", "proj-burn", "proj-burn-sub"),
                    _stat("Eaten, last 7 days", "proj-intake", "proj-intake-sub"),
                    _stat("In 4 weeks", "proj-end", "proj-end-sub"),
                ],
                cols={"base": 2, "sm": 4},
                spacing="sm",
            ),
            card(
                dmc.Stack(
                    [
                        dmc.Title("Next 4 weeks", order=5),
                        charts.graph("proj-graph"),
                        dmc.Text(
                            "Shaded bands: 10th–90th percentile. Lines start at today's trend "
                            "weight; dots are weigh-ins.",
                            size="xs",
                            c="dimmed",
                        ),
                        dmc.Box(id="proj-table"),
                    ],
                    gap="xs",
                )
            ),
            dmc.SimpleGrid(
                [
                    card(
                        dmc.Stack(
                            [dmc.Title("Burn estimates", order=5), dmc.Stack(id="proj-tdee")],
                            gap="xs",
                        )
                    ),
                    card(
                        dmc.Stack(
                            [
                                dmc.Title("Last 7 food days", order=5),
                                dmc.Stack(id="proj-days", gap=4),
                            ],
                            gap="xs",
                        )
                    ),
                ],
                cols={"base": 1, "md": 2},
                spacing="md",
            ),
        ],
        gap="md",
    )


SLOTS = {"target": 2, "garmin": 3, "adaptive": 4}


def _figure(
    snap: Snapshot, projections: list[Projection], goal: float | None, unit: WeightUnit, scheme
) -> go.Figure:
    if not snap.trend:
        return charts.empty(scheme, "No weigh-ins yet.")
    fig = charts.figure(scheme, y_title=unit, height=340)
    since = snap.today - timedelta(days=HISTORY_DAYS)
    hist = [p for p in snap.trend if p.day >= since]
    fig.add_scatter(
        x=[p.day for p in hist],
        y=[kg_to(p.trend_kg, unit) for p in hist],
        mode="lines",
        name="Trend",
        line={"color": charts.series(scheme, 1), "width": 2},
        hovertemplate="%{y:.1f}",
    )
    raw = [w for w in snap.weighins if w.day >= since]
    fig.add_scatter(
        x=[w.day for w in raw],
        y=[kg_to(w.weight_kg, unit) for w in raw],
        mode="markers",
        name="Weigh-ins",
        marker={"color": charts.ink(scheme, "muted"), "size": 8},
        hovertemplate="%{y:.1f}",
        showlegend=False,
    )
    for p in projections:
        color = charts.series(scheme, SLOTS[p.key])
        fig.add_scatter(
            x=p.days + p.days[::-1],
            y=[kg_to(v, unit) for v in p.p90 + p.p10[::-1]],
            fill="toself",
            fillcolor=charts.rgba(color, 0.14),
            line={"width": 0},
            hoverinfo="skip",
            showlegend=False,
        )
        fig.add_scatter(
            x=p.days,
            y=[kg_to(v, unit) for v in p.expected],
            mode="lines",
            name=p.label,
            line={"color": color, "width": 2},
            hovertemplate="%{y:.1f}",
        )
        fig.add_annotation(
            x=p.days[-1],
            y=kg_to(p.expected[-1], unit),
            text=fmt_weight(p.expected[-1], unit),
            showarrow=False,
            xanchor="left",
            xshift=6,
            font={"size": 11, "color": charts.ink(scheme, "text")},
        )
    if goal and snap.weight_kg is not None:
        end = snap.today + timedelta(days=28)
        fig.add_scatter(
            x=[snap.today, end],
            y=[
                kg_to(snap.weight_kg, unit),
                kg_to(snap.weight_kg - goal * 28 / DAYS_PER_MONTH, unit),
            ],
            mode="lines",
            name="Goal pace",
            line={"color": charts.ink(scheme, "muted"), "width": 1.5, "dash": "dot"},
            hovertemplate="%{y:.1f}",
        )
    fig.update_xaxes(range=[since, snap.today + timedelta(days=33)])
    return fig


def _table(projections: list[Projection], unit: WeightUnit):
    if not projections:
        return None
    head = dmc.TableThead(
        dmc.TableTr(
            [
                dmc.TableTh(h)
                for h in ("Scenario", "Eating", "Burn", "In 4 weeks", "10–90%", "Per week")
            ]
        )
    )
    rows = [
        dmc.TableTr(
            [
                dmc.TableTd(p.label),
                dmc.TableTd(f"{p.intake_kcal:,.0f}"),
                dmc.TableTd(f"{p.tdee_kcal:,.0f}"),
                dmc.TableTd(fmt_weight(p.expected[-1], unit)),
                dmc.TableTd(
                    f"{kg_to(p.p10[-1], unit):,.1f}–{kg_to(p.p90[-1], unit):,.1f}", c="dimmed"
                ),
                dmc.TableTd(f"{kg_to(p.change_kg / 4, unit):+.1f} {unit}"),
            ]
        )
        for p in projections
    ]
    return dmc.TableScrollContainer(
        dmc.Table([head, dmc.TableTbody(rows)], fz="sm", highlightOnHover=True),
        minWidth=520,
    )


def _tdee_rows(snap: Snapshot) -> list:
    out = []
    for est in (snap.garmin, snap.adaptive):
        badge = (
            dmc.Badge("Sets targets", size="xs", variant="light")
            if est is snap.chosen and est.ready
            else dmc.Badge("Calibrating", size="xs", color="gray", variant="light")
            if not est.ready
            else None
        )
        out.append(
            dmc.Stack(
                [
                    dmc.Group([dmc.Text(est.label, fw=600, size="sm"), badge], gap="xs"),
                    dmc.Text(tdee_line(est), size="sm", c="dimmed"),
                ],
                gap=0,
            )
        )
    if snap.garmin.resting_kcal:
        out.append(
            dmc.Text(
                f"Resting burn (calorie floor): {snap.garmin.resting_kcal:,.0f} kcal",
                size="xs",
                c="dimmed",
            )
        )
    if snap.rho:
        out.append(
            dmc.Text(
                f"Energy per kg of change (ρ): {snap.rho:,.0f} kcal "
                f"({snap.rho * 0.45359237:,.0f} kcal/lb) at {snap.fat_kg:,.1f} kg fat mass",
                size="xs",
                c="dimmed",
            )
        )
    return out


def _day_rows(snap: Snapshot) -> list:
    rows = []
    for d in reversed(snap.last7):
        if d.complete:
            food = dmc.Text(fmt_kcal(d.kcal), size="sm")
        elif d.entries:
            food = dmc.Badge("Partial", size="sm", color="yellow", variant="light")
        else:
            food = dmc.Badge("Not logged", size="sm", color="gray", variant="light")
        if d.garmin_total is None:
            burn = dmc.Text("no Garmin data", size="xs", c="dimmed")
        else:
            burn = dmc.Group(
                [
                    dmc.Text(f"burn {d.garmin_total:,.0f}", size="xs", c="dimmed"),
                    dmc.Badge("provisional", size="xs", color="orange", variant="light")
                    if d.garmin_provisional
                    else None,
                ],
                gap=4,
            )
        rows.append(
            dmc.Group(
                [dmc.Text(fmt_day(d.day), size="sm", w=90), food, burn],
                justify="space-between",
                wrap="nowrap",
            )
        )
    return rows


def _alerts(snap: Snapshot) -> list:
    out = []
    n = len(snap.intakes7)
    if n < snap_mod.WARN_BELOW_COMPLETE:
        shown = (
            "isn't shown yet"
            if n < snap_mod.MIN_LOGGED_FOR_PROJECTION
            else "is rough until 5 are logged"
        )
        out.append(
            dmc.Alert(
                f"{n} of the last 7 food days are fully logged, so the 'as eaten' "
                f"projection {shown}. The 'at your target' line works without them.",
                color="yellow",
                variant="light",
                icon=icon("tabler:alert-triangle"),
            )
        )
    if snap.body_fat_assumed:
        out.append(
            dmc.Alert(
                "No body fat readings from your scale; 30% is assumed.",
                color="gray",
                variant="light",
            )
        )
    return out


@callback(
    Output("proj-alerts", "children"),
    Output("proj-weight", "children"),
    Output("proj-weight-sub", "children"),
    Output("proj-burn", "children"),
    Output("proj-burn-sub", "children"),
    Output("proj-intake", "children"),
    Output("proj-intake-sub", "children"),
    Output("proj-end", "children"),
    Output("proj-end-sub", "children"),
    Output("proj-graph", "figure"),
    Output("proj-table", "children"),
    Output("proj-tdee", "children"),
    Output("proj-days", "children"),
    Input("color-scheme", "data"),
)
def render(scheme):
    unit = weight_unit()
    zone, at = tz(), now()
    with session_scope() as s:
        snap = build_snapshot(s, zone, at)
        plan = day_targets(s, zone, at, snap.today, snap=snap)
        goal = goal_kg_per_month(s)
    projections = scenarios(snap, plan.values.get("kcal"))

    week_ago = next((p for p in snap.trend if p.day == snap.today - timedelta(days=7)), None)
    if snap.weight_kg is not None and week_ago is not None:
        weight_sub = f"{kg_to(snap.weight_kg - week_ago.trend_kg, unit):+.1f} {unit} vs. 7 days ago"
    else:
        weight_sub = "Needs a week of weigh-ins"
    burn = snap.chosen
    intakes = snap.intakes7
    first = projections[0] if projections else None
    return (
        _alerts(snap),
        fmt_weight(snap.weight_kg, unit),
        weight_sub,
        fmt_kcal(burn.kcal),
        f"{burn.label} · ± {burn.se:,.0f}" if burn.ready else burn.note,
        fmt_kcal(sum(intakes) / len(intakes)) if intakes else "—",
        f"{len(intakes)}/7 days logged",
        fmt_weight(first.expected[-1], unit) if first else "—",
        (
            f"{first.label} · {fmt_rate(first.change_kg * DAYS_PER_MONTH / 28, unit)}"
            if first
            else "Needs weigh-ins and Garmin burn"
        ),
        _figure(snap, projections, goal, unit, scheme),
        _table(projections, unit),
        _tdee_rows(snap),
        _day_rows(snap),
    )


dash.register_page(
    __name__,
    path="/projection",
    name="Projection",
    title="Projection · Weight Coach",
    order=2,
    layout=layout,
)
