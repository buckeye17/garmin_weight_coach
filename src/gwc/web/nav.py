from dataclasses import dataclass


@dataclass(frozen=True)
class NavItem:
    label: str
    href: str
    icon: str
    # Narrowest screen width (px) at which this item gets its own tab in the phone bottom bar.
    # 0 means always; narrower screens, and every screen when None, list it under "More".
    bar_min_width: int | None = None


NAV_ITEMS: list[NavItem] = [
    # Roughly 80 px per tab, counting the always-present "More" tab.
    NavItem("Today", "/", "tabler:layout-dashboard", bar_min_width=0),
    NavItem("Log", "/log", "tabler:circle-plus", bar_min_width=0),
    NavItem("Ask", "/ask", "tabler:zoom-question", bar_min_width=0),
    NavItem("Library", "/library", "tabler:books", bar_min_width=0),
    NavItem("Projection", "/projection", "tabler:chart-line", bar_min_width=480),
    NavItem("Coach", "/coach", "tabler:message-chatbot", bar_min_width=560),
    NavItem("Trends", "/trends", "tabler:chart-dots", bar_min_width=640),
    NavItem("Settings", "/settings", "tabler:settings"),
]


def is_active(item_href: str, pathname: str | None) -> bool:
    pathname = (pathname or "/").rstrip("/") or "/"
    if item_href == "/":
        return pathname == "/"
    return pathname == item_href or pathname.startswith(item_href + "/")
