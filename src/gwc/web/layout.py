"""App chrome: header, desktop sidebar, and phone bottom navigation."""

import dash
import dash_mantine_components as dmc
from dash import ALL, Input, Output, callback, clientside_callback, dcc
from dash_iconify import DashIconify

from gwc.web.nav import NAV_ITEMS, is_active

HEADER_HEIGHT = 56
NAVBAR_WIDTH = 220
BOTTOM_NAV_HEIGHT = 64
# Mantine's "md" breakpoint (62em). Below it the sidebar collapses and the bottom bar shows.
# "md" rather than "sm" so an unfolded foldable (~750-900 px wide) gets touch navigation and
# full-width content instead of a sidebar squeezing the page.
MOBILE_BREAKPOINT = "md"
MOBILE_BREAKPOINT_PX = 992
HEADER_POLL_MS = 30_000

THEME = {
    "primaryColor": "teal",
    "defaultRadius": "md",
    "fontFamily": "Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
}


def _icon(name: str, size: int = 20) -> DashIconify:
    return DashIconify(icon=name, width=size, height=size)


def _header() -> dmc.AppShellHeader:
    return dmc.AppShellHeader(
        dmc.Group(
            [
                dmc.Group(
                    [_icon("tabler:scale", 24), dmc.Title("Weight Coach", order=4)],
                    gap="xs",
                ),
                dmc.Anchor(
                    dmc.Badge(
                        "Garmin",
                        id="sync-status-badge",
                        variant="light",
                        color="gray",
                        maw={"base": 200, "sm": 420},
                    ),
                    href="/settings",
                    underline="never",
                ),
            ],
            h="100%",
            px="md",
            justify="space-between",
        )
    )


def _navbar() -> dmc.AppShellNavbar:
    return dmc.AppShellNavbar(
        [
            dmc.NavLink(
                label=item.label,
                href=item.href,
                leftSection=_icon(item.icon),
                active="exact" if item.href == "/" else "partial",
            )
            for item in NAV_ITEMS
        ],
        p="xs",
    )


def _bottom_nav() -> dmc.Paper:
    def tab(label: str, icon: str, href: str) -> dmc.Anchor:
        return dmc.Anchor(
            dmc.Stack([_icon(icon, 22), dmc.Text(label, size="xs")], gap=2, align="center"),
            href=href,
            id={"type": "bottom-nav", "href": href},
            underline="never",
            c="dimmed",
        )

    primary = [tab(i.label, i.icon, i.href) for i in NAV_ITEMS if i.mobile_primary]
    more = dmc.Menu(
        [
            dmc.MenuTarget(
                dmc.UnstyledButton(
                    dmc.Stack(
                        [_icon("tabler:dots", 22), dmc.Text("More", size="xs")],
                        gap=2,
                        align="center",
                    ),
                    c="dimmed",
                )
            ),
            dmc.MenuDropdown(
                [
                    dmc.MenuItem(i.label, href=i.href, leftSection=_icon(i.icon, 18))
                    for i in NAV_ITEMS
                    if not i.mobile_primary
                ]
            ),
        ],
        position="top-end",
    )
    return dmc.Paper(
        dmc.Group(primary + [more], justify="space-around", h="100%", px="xs"),
        hiddenFrom=MOBILE_BREAKPOINT,
        withBorder=True,
        radius=0,
        h=BOTTOM_NAV_HEIGHT,
        pos="fixed",
        bottom=0,
        left=0,
        right=0,
        style={"zIndex": 200, "paddingBottom": "env(safe-area-inset-bottom)"},
    )


def build_layout() -> dmc.MantineProvider:
    return dmc.MantineProvider(
        dmc.AppShell(
            [
                dcc.Location(id="url"),
                # Which scheme Mantine is showing ("light"/"dark"), for chart colors.
                dcc.Store(id="color-scheme"),
                dcc.Interval(id="header-poll", interval=HEADER_POLL_MS),
                _header(),
                _navbar(),
                dmc.AppShellMain(
                    [
                        dmc.Container(
                            dash.page_container, size="lg", px={"base": "xs", "sm": "md"}
                        ),
                        # Keeps page content clear of the fixed bottom bar on phones.
                        dmc.Box(h=BOTTOM_NAV_HEIGHT + 16, hiddenFrom=MOBILE_BREAKPOINT),
                    ]
                ),
                _bottom_nav(),
            ],
            header={"height": HEADER_HEIGHT},
            navbar={
                "width": NAVBAR_WIDTH,
                "breakpoint": MOBILE_BREAKPOINT_PX,
                "collapsed": {"mobile": True},
            },
            padding={"base": "xs", MOBILE_BREAKPOINT: "md"},
        ),
        theme=THEME,
        defaultColorScheme="auto",
    )


clientside_callback(
    "(_path) => document.documentElement.getAttribute('data-mantine-color-scheme') || 'light'",
    Output("color-scheme", "data"),
    Input("url", "pathname"),
)


@callback(
    Output({"type": "bottom-nav", "href": ALL}, "c"),
    Input("url", "pathname"),
    dash.State({"type": "bottom-nav", "href": ALL}, "id"),
)
def _highlight_bottom_nav(pathname: str | None, ids: list[dict]) -> list[str]:
    return ["teal" if is_active(i["href"], pathname) else "dimmed" for i in ids]


@callback(
    Output("sync-status-badge", "children"),
    Output("sync-status-badge", "color"),
    Input("header-poll", "n_intervals"),
    Input("url", "pathname"),
)
def _update_sync_badge(_n, _pathname) -> tuple[str, str]:
    from gwc.garmin.service import get_sync_service
    from gwc.garmin.status import badge, get_overview
    from gwc.web.format import fmt_when

    service = get_sync_service()
    now = service.clock()
    return badge(get_overview(), lambda dt: fmt_when(dt, service.tz, now))
