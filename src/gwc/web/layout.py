"""App chrome: header, desktop sidebar, and phone bottom navigation."""

import dash
import dash_mantine_components as dmc
from dash import ALL, Input, Output, callback, clientside_callback, dcc
from dash_iconify import DashIconify

from gwc.web.nav import NAV_ITEMS, NavItem, is_active

HEADER_HEIGHT = 56
NAVBAR_WIDTH = 220
BOTTOM_NAV_HEIGHT = 64
# Mantine's "md" breakpoint (62em). Below it the sidebar collapses and the bottom bar shows.
# "md" rather than "sm" so an unfolded foldable (~750-900 px wide) gets touch navigation and
# full-width content instead of a sidebar squeezing the page.
MOBILE_BREAKPOINT = "md"
MOBILE_BREAKPOINT_PX = 992
HEADER_POLL_MS = 30_000

# Evergreen brand scale, light to dark. Shade 7 carries white text at AA contrast.
BRAND = [
    "#e7f7f0",
    "#cdeee0",
    "#9fdcc2",
    "#6cc9a2",
    "#43b886",
    "#28ab74",
    "#16a068",
    "#008a59",
    "#00794d",
    "#00663f",
]
BODY_FONT = "Inter, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
HEADING_FONT = "'Plus Jakarta Sans', " + BODY_FONT


def _bar_breakpoint(width: int) -> str:
    return f"bar{width}"


# Extra Mantine breakpoints so visibleFrom/hiddenFrom can move nav items between the bottom
# bar and its "More" menu as the screen widens. Mantine merges these with its defaults.
BAR_BREAKPOINTS = {
    _bar_breakpoint(i.bar_min_width): f"{i.bar_min_width}px" for i in NAV_ITEMS if i.bar_min_width
}

THEME = {
    "breakpoints": BAR_BREAKPOINTS,
    "primaryColor": "brand",
    "primaryShade": {"light": 7, "dark": 6},
    "colors": {"brand": BRAND},
    "defaultRadius": "md",
    "fontFamily": BODY_FONT,
    "headings": {"fontFamily": HEADING_FONT, "fontWeight": "700"},
    "components": {
        "Card": {"defaultProps": {"radius": "lg", "padding": "lg", "shadow": "xs"}},
        "Button": {"styles": {"root": {"fontWeight": 600}}},
        "Modal": {
            "defaultProps": {
                "radius": "lg",
                "overlayProps": {"backgroundOpacity": 0.45, "blur": 3},
            }
        },
        "Menu": {"defaultProps": {"radius": "md", "shadow": "md"}},
        "Tooltip": {"defaultProps": {"radius": "sm"}},
    },
}


def _icon(name: str, size: int = 20) -> DashIconify:
    return DashIconify(icon=name, width=size, height=size)


def _header() -> dmc.AppShellHeader:
    return dmc.AppShellHeader(
        dmc.Group(
            [
                dmc.Anchor(
                    dmc.Group(
                        [
                            dmc.ThemeIcon(
                                _icon("tabler:scale", 20),
                                size=34,
                                radius="md",
                                variant="gradient",
                                gradient={"from": "brand.5", "to": "brand.8", "deg": 135},
                            ),
                            # Phones show just the mark so the sync badge keeps its row.
                            dmc.Title(
                                "Weight Coach", order=4, className="gwc-brand", visibleFrom="xs"
                            ),
                        ],
                        gap=10,
                        wrap="nowrap",
                    ),
                    href="/",
                    underline="never",
                    c="inherit",
                ),
                dmc.Anchor(
                    dmc.Badge(
                        "Garmin",
                        id="sync-status-badge",
                        variant="light",
                        color="gray",
                        size="lg",
                        radius="xl",
                        leftSection=_icon("tabler:refresh", 14),
                        maw={"base": 280, "sm": 420},
                    ),
                    href="/settings",
                    underline="never",
                    miw=0,
                ),
            ],
            h="100%",
            px="md",
            justify="space-between",
            wrap="nowrap",
        ),
        className="gwc-header",
    )


def _navbar() -> dmc.AppShellNavbar:
    return dmc.AppShellNavbar(
        [
            dmc.NavLink(
                label=item.label,
                href=item.href,
                leftSection=_icon(item.icon),
                active="exact" if item.href == "/" else "partial",
                className="gwc-navlink",
            )
            for item in NAV_ITEMS
        ],
        p="sm",
        className="gwc-navbar",
    )


def _bottom_nav() -> dmc.Paper:
    def tab(item: NavItem) -> dmc.Anchor:
        return dmc.Anchor(
            dmc.Stack(
                [
                    dmc.Box(_icon(item.icon, 22), className="gwc-tab-icon"),
                    dmc.Text(item.label, size="xs", fw=500, className="gwc-tab-label"),
                ],
                gap=2,
                align="center",
            ),
            href=item.href,
            id={"type": "bottom-nav", "href": item.href},
            underline="never",
            c="dimmed",
            className="gwc-tab",
            visibleFrom=_bar_breakpoint(item.bar_min_width) if item.bar_min_width else None,
        )

    # Each item is either a bar tab or a "More" entry at any width, never both.
    tabs = [tab(i) for i in NAV_ITEMS if i.bar_min_width is not None]
    more = dmc.Menu(
        [
            dmc.MenuTarget(
                dmc.UnstyledButton(
                    dmc.Stack(
                        [
                            dmc.Box(_icon("tabler:dots", 22), className="gwc-tab-icon"),
                            dmc.Text("More", size="xs", fw=500),
                        ],
                        gap=2,
                        align="center",
                    ),
                    c="dimmed",
                    className="gwc-tab",
                    w="100%",
                ),
                # DMC wraps the target in a fit-content div; that div is the flex child.
                boxWrapperProps={"className": "gwc-tab"},
            ),
            dmc.MenuDropdown(
                [
                    dmc.MenuItem(
                        i.label,
                        href=i.href,
                        leftSection=_icon(i.icon, 18),
                        hiddenFrom=_bar_breakpoint(i.bar_min_width) if i.bar_min_width else None,
                    )
                    for i in NAV_ITEMS
                    if i.bar_min_width != 0
                ]
            ),
        ],
        position="top-end",
    )
    return dmc.Paper(
        # nowrap: a wrapped tab lands on a second row below the fixed bar, out of sight.
        dmc.Group(tabs + [more], gap=0, wrap="nowrap", h="100%", px=4),
        hiddenFrom=MOBILE_BREAKPOINT,
        radius=0,
        h=BOTTOM_NAV_HEIGHT,
        pos="fixed",
        bottom=0,
        left=0,
        right=0,
        className="gwc-bottom-nav",
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
                    ],
                    className="gwc-main",
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
    return ["brand" if is_active(i["href"], pathname) else "dimmed" for i in ids]


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
