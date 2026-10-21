"""Plotly figure styling shared by the Trends and Projection pages.

Series colors are categorical slots in a fixed order, stepped separately for
light and dark (the page passes which one Mantine is showing). Charts use one
y-axis each; values and labels stay in text colors, not series colors.
"""

import plotly.graph_objects as go
from dash import dcc

SERIES = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"],
    "dark": ["#3987e5", "#d95926", "#199e70", "#c98500"],
}
INK = {
    "light": {"text": "#52514e", "muted": "#8a8984", "grid": "rgba(82,81,78,0.14)"},
    "dark": {"text": "#c3c2b7", "muted": "#8f8e86", "grid": "rgba(195,194,183,0.14)"},
}


def mode(scheme: str | None) -> str:
    return "dark" if scheme == "dark" else "light"


def series(scheme: str | None, slot: int) -> str:
    return SERIES[mode(scheme)][slot - 1]


def ink(scheme: str | None, role: str) -> str:
    return INK[mode(scheme)][role]


def rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r},{g},{b},{alpha})"


def figure(scheme: str | None, y_title: str = "", height: int = 300) -> go.Figure:
    text, grid = ink(scheme, "text"), ink(scheme, "grid")
    fig = go.Figure()
    fig.update_layout(
        height=height,
        margin={"l": 8, "r": 8, "t": 8, "b": 8},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": "Inter, system-ui, sans-serif", "size": 12, "color": text},
        hovermode="x unified",
        hoverlabel={"font": {"size": 12}},
        legend={"orientation": "h", "x": 0, "y": 1.02, "yanchor": "bottom", "title": None},
        showlegend=True,
        bargap=0.25,
    )
    fig.update_xaxes(showgrid=False, linecolor=grid, ticks="", automargin=True)
    fig.update_yaxes(
        title={"text": y_title, "font": {"size": 11}},
        gridcolor=grid,
        zeroline=False,
        automargin=True,
    )
    return fig


def empty(scheme: str | None, message: str, height: int = 200) -> go.Figure:
    fig = figure(scheme, height=height)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    fig.add_annotation(
        text=message,
        showarrow=False,
        xref="paper",
        yref="paper",
        x=0.5,
        y=0.5,
        font={"color": ink(scheme, "muted")},
    )
    return fig


def graph(graph_id: str) -> dcc.Graph:
    return dcc.Graph(
        id=graph_id,
        config={"displayModeBar": False, "responsive": True},
        figure=go.Figure(layout={"paper_bgcolor": "rgba(0,0,0,0)", "height": 300}),
    )
