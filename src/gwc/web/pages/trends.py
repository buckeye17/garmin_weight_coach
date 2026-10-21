import dash

from gwc.web.components.placeholder import placeholder_page


def layout(**_kwargs):
    return placeholder_page(
        "Trends",
        "tabler:chart-dots",
        milestone=4,
        items=[
            "Weight and smoothed trend, body composition",
            "Intake vs. Garmin calories burned",
            "Sleep and activities",
        ],
    )


dash.register_page(
    __name__, path="/trends", name="Trends", title="Trends · Weight Coach", order=4, layout=layout
)
