import dash

from gwc.web.components.placeholder import placeholder_page


def layout(**_kwargs):
    return placeholder_page(
        "Settings",
        "tabler:settings",
        milestone=2,
        items=[
            "Garmin connection, 2FA, sync status and Sync now",
            "Units, monthly goal and target overrides",
            "Food-day fallback hour, sync time, LLM backend",
        ],
    )


dash.register_page(
    __name__,
    path="/settings",
    name="Settings",
    title="Settings · Weight Coach",
    order=6,
    layout=layout,
)
