from dataclasses import dataclass


@dataclass(frozen=True)
class NavItem:
    label: str
    href: str
    icon: str
    # Shown directly in the phone bottom bar; the rest go under "More".
    mobile_primary: bool = False


NAV_ITEMS: list[NavItem] = [
    NavItem("Today", "/", "tabler:layout-dashboard", mobile_primary=True),
    NavItem("Log", "/log", "tabler:circle-plus", mobile_primary=True),
    NavItem("Ask", "/ask", "tabler:zoom-question", mobile_primary=True),
    NavItem("Library", "/library", "tabler:books", mobile_primary=True),
    NavItem("Projection", "/projection", "tabler:chart-line"),
    NavItem("Coach", "/coach", "tabler:message-chatbot"),
    NavItem("Trends", "/trends", "tabler:chart-dots"),
    NavItem("Settings", "/settings", "tabler:settings"),
]


def is_active(item_href: str, pathname: str | None) -> bool:
    pathname = (pathname or "/").rstrip("/") or "/"
    if item_href == "/":
        return pathname == "/"
    return pathname == item_href or pathname.startswith(item_href + "/")
