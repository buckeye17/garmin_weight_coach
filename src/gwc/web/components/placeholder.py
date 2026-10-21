import dash_mantine_components as dmc
from dash_iconify import DashIconify


def placeholder_page(title: str, icon: str, milestone: int, items: list[str]) -> dmc.Stack:
    """Stand-in content for pages that later milestones fill in."""
    return dmc.Stack(
        [
            dmc.Group([DashIconify(icon=icon, width=28), dmc.Title(title, order=2)], gap="sm"),
            dmc.Alert(
                f"Coming in milestone {milestone}.",
                color="gray",
                variant="light",
                icon=DashIconify(icon="tabler:hammer"),
            ),
            dmc.Card(
                [
                    dmc.Text("Planned for this page", fw=600, mb="xs"),
                    dmc.List([dmc.ListItem(i) for i in items], size="sm", spacing="xs"),
                ],
                withBorder=True,
            ),
        ],
        gap="md",
    )
