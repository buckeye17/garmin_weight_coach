from datetime import date, datetime, time, timedelta

import dash
import dash_mantine_components as dmc
import plotly.graph_objects as go
from dash import Input, Output, callback
from sqlalchemy import func, select

from gwc.analysis.food_day import current_food_day
from gwc.analysis.snapshot import Snapshot, build_snapshot
from gwc.analysis.targets import frozen_targets
from gwc.analysis.units import WeightUnit, kg_to, weight_unit
from gwc.db.models import Activity, BodyComp, DailySummary, Sleep
from gwc.db.session import session_scope
from gwc.web.components import charts
from gwc.web.components.common import card, icon, now, tz
from gwc.web.format import fmt_day

RANGES = {"30": 30, "90": 90, "365": 365, "all": None}


def _chart_card(title: str, graph_id: str, note: str | None = None) -> dmc.Card:
    children = [dmc.Title(title, order=5), charts.graph(graph_id)]
    if note:
        children.append(dmc.Text(note, size="xs", c="dimmed"))
    return card(dmc.Stack(children, gap="xs"))


def layout(**_kwargs):
    return dmc.Stack(
        [
            dmc.Group(
                [
                    dmc.Group(
                        [icon("tabler:chart-dots", 28), dmc.Title("Trends", order=2)], gap="sm"
                    ),
                    dmc.SegmentedControl(
                        id="trends-range",
                        data=[
                            {"value": "30", "label": "30d"},
                            {"value": "90", "label": "90d"},
                            {"value": "365", "label": "1y"},
                            {"value": "all", "label": "All"},
                        ],
                        value="30",
                        size="xs",
                        persistence=True,
                    ),
                ],
                justify="space-between",
            ),
            dmc.SimpleGrid(
                [
                    _chart_card(
                        "Weight",
                        "trends-weight",
                        "Dots are daily weigh-ins; the line is the smoothed trend.",
                    ),
                    _chart_card("Body fat", "trends-fat", "Scale readings and smoothed trend."),
                    _chart_card(
                        "Eaten vs. Garmin burn",
                        "trends-kcal",
                        "Faded bars are days marked as not fully logged.",
                    ),
                    _chart_card("Sleep", "trends-sleep"),
                    _chart_card("Steps", "trends-steps"),
                    card(
                        dmc.Stack(
                            [dmc.Title("Activities", order=5), dmc.Box(id="trends-activities")],
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


def _start(s, today: date, key: str) -> date:
    days = RANGES.get(key, 30)
    if days is not None:
        return today - timedelta(days=days - 1)
    firsts = [
        s.scalar(select(func.min(DailySummary.calendar_date))),
        s.scalar(select(func.min(Sleep.calendar_date))),
    ]
    first_weigh = s.scalar(select(func.min(BodyComp.measured_at)))
    if first_weigh is not None:
        firsts.append(first_weigh.astimezone(tz()).date())
    known = [d for d in firsts if d is not None]
    return min(known) if known else today - timedelta(days=29)


def _weight_fig(snap: Snapshot, start: date, unit: WeightUnit, scheme) -> go.Figure:
    trend = [p for p in snap.trend if p.day >= start]
    if not trend:
        return charts.empty(scheme, "No weigh-ins in this range.")
    fig = charts.figure(scheme, y_title=unit)
    fig.add_scatter(
        x=[p.day for p in trend],
        y=[kg_to(p.trend_kg, unit) for p in trend],
        mode="lines",
        name="Trend",
        line={"color": charts.series(scheme, 1), "width": 2},
        hovertemplate="Trend %{y:.1f}<extra></extra>",
    )
    raw = [w for w in snap.weighins if w.day >= start]
    fig.add_scatter(
        x=[w.day for w in raw],
        y=[kg_to(w.weight_kg, unit) for w in raw],
        mode="markers",
        name="Weigh-in",
        marker={"color": charts.ink(scheme, "muted"), "size": 8},
        hovertemplate="Weigh-in %{y:.1f}<extra></extra>",
    )
    fig.update_layout(showlegend=False)
    return fig


def _fat_fig(snap: Snapshot, start: date, scheme) -> go.Figure:
    trend = [p for p in snap.trend if p.day >= start and p.fat_trend_pct is not None]
    if not trend:
        return charts.empty(scheme, "No body fat readings in this range.")
    fig = charts.figure(scheme, y_title="%")
    fig.add_scatter(
        x=[p.day for p in trend],
        y=[p.fat_trend_pct for p in trend],
        mode="lines",
        line={"color": charts.series(scheme, 1), "width": 2},
        hovertemplate="Trend %{y:.1f}%<extra></extra>",
    )
    raw = [w for w in snap.weighins if w.day >= start and w.body_fat_pct is not None]
    fig.add_scatter(
        x=[w.day for w in raw],
        y=[w.body_fat_pct for w in raw],
        mode="markers",
        marker={"color": charts.ink(scheme, "muted"), "size": 8},
        hovertemplate="Reading %{y:.1f}%<extra></extra>",
    )
    fig.update_layout(showlegend=False)
    return fig


def _kcal_fig(snap: Snapshot, start: date, targets: dict[date, float], scheme) -> go.Figure:
    days = [d for d in snap.records if d.day >= start]
    if not any(d.garmin_total or d.entries for d in days):
        return charts.empty(scheme, "No food logs or Garmin totals in this range.")
    fig = charts.figure(scheme, y_title="kcal")
    blue = charts.series(scheme, 1)
    logged = [d for d in days if d.entries]
    fig.add_bar(
        x=[d.day for d in logged],
        y=[d.kcal for d in logged],
        name="Eaten",
        marker={
            "color": [blue if d.complete else charts.rgba(blue, 0.35) for d in logged],
            "cornerradius": 4,
        },
        hovertemplate="Eaten %{y:,.0f}<extra></extra>",
    )
    burned = [d for d in days if d.garmin_total is not None]
    fig.add_scatter(
        x=[d.day for d in burned],
        y=[d.garmin_total for d in burned],
        mode="lines+markers",
        name="Garmin burn",
        line={"color": charts.series(scheme, 2), "width": 2},
        marker={"size": 6},
        hovertemplate="Burn %{y:,.0f}<extra></extra>",
    )
    if targets:
        ts = sorted(targets.items())
        fig.add_scatter(
            x=[d for d, _ in ts],
            y=[v for _, v in ts],
            mode="lines+markers",
            name="Target",
            line={"color": charts.ink(scheme, "muted"), "width": 1.5, "dash": "dot", "shape": "hv"},
            marker={"size": 6},
            hovertemplate="Target %{y:,.0f}<extra></extra>",
        )
    return fig


def _sleep_fig(s, start: date, end: date, scheme) -> go.Figure:
    rows = s.scalars(
        select(Sleep)
        .where(Sleep.is_nap.is_(False), Sleep.calendar_date >= start, Sleep.calendar_date <= end)
        .order_by(Sleep.calendar_date)
    ).all()
    if not rows:
        return charts.empty(scheme, "No sleep data in this range.")
    fig = charts.figure(scheme, y_title="hours")
    hours = [((r.duration_s or (r.end_at - r.start_at).total_seconds()) / 3600) for r in rows]
    fig.add_bar(
        x=[r.calendar_date for r in rows],
        y=hours,
        marker={"color": charts.series(scheme, 1), "cornerradius": 4},
        customdata=[
            [
                r.sleep_score if r.sleep_score is not None else "—",
                f"{r.start_at.astimezone(tz()):%H:%M}",
            ]
            for r in rows
        ],
        hovertemplate=(
            "%{y:.1f} h · score %{customdata[0]} · asleep %{customdata[1]}<extra></extra>"
        ),
    )
    fig.update_layout(showlegend=False)
    return fig


def _steps_fig(snap: Snapshot, start: date, scheme) -> go.Figure:
    days = [d for d in snap.records if d.day >= start and d.steps is not None]
    if not days:
        return charts.empty(scheme, "No step counts in this range.")
    fig = charts.figure(scheme, y_title="steps")
    fig.add_bar(
        x=[d.day for d in days],
        y=[d.steps for d in days],
        marker={"color": charts.series(scheme, 1), "cornerradius": 4},
        hovertemplate="%{y:,} steps<extra></extra>",
    )
    fig.update_layout(showlegend=False)
    return fig


def _activities(s, start: date):
    zone = tz()
    since = datetime.combine(start, time(0), tzinfo=zone)
    rows = s.scalars(
        select(Activity)
        .where(Activity.start_at >= since)
        .order_by(Activity.start_at.desc())
        .limit(50)
    ).all()
    if not rows:
        return dmc.Text("No activities in this range.", size="sm", c="dimmed")
    body = []
    for a in rows:
        mins = f"{a.duration_s / 60:,.0f} min" if a.duration_s else "—"
        kcal = f"{a.kcal:,.0f}" if a.kcal else "—"
        body.append(
            dmc.TableTr(
                [
                    dmc.TableTd(fmt_day(a.start_at.astimezone(zone).date())),
                    dmc.TableTd(a.name or a.activity_type.replace("_", " ")),
                    dmc.TableTd(mins),
                    dmc.TableTd(kcal),
                ]
            )
        )
    head = dmc.TableThead(
        dmc.TableTr([dmc.TableTh(h) for h in ("Day", "Activity", "Time", "kcal")])
    )
    return dmc.TableScrollContainer(
        dmc.Table([head, dmc.TableTbody(body)], fz="sm", highlightOnHover=True), minWidth=360
    )


@callback(
    Output("trends-weight", "figure"),
    Output("trends-fat", "figure"),
    Output("trends-kcal", "figure"),
    Output("trends-sleep", "figure"),
    Output("trends-steps", "figure"),
    Output("trends-activities", "children"),
    Input("trends-range", "value"),
    Input("color-scheme", "data"),
)
def render(range_key, scheme):
    unit = weight_unit()
    zone, at = tz(), now()
    with session_scope() as s:
        today = current_food_day(s, zone, at)
        start = _start(s, today, range_key or "30")
        snap = build_snapshot(s, zone, at, history_days=(today - start).days)
        targets = frozen_targets(s, start, snap.today)
        sleep = _sleep_fig(s, start, snap.today, scheme)
        activities = _activities(s, start)
    figs = [
        _weight_fig(snap, start, unit, scheme),
        _fat_fig(snap, start, scheme),
        _kcal_fig(snap, start, targets, scheme),
        sleep,
        _steps_fig(snap, start, scheme),
    ]
    for fig in figs:
        if fig.layout.xaxis.visible is not False:
            fig.update_xaxes(range=[start - timedelta(hours=12), snap.today + timedelta(hours=12)])
    return *figs, activities


dash.register_page(
    __name__, path="/trends", name="Trends", title="Trends · Weight Coach", order=4, layout=layout
)
