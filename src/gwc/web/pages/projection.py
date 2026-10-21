import dash

from gwc.web.components.placeholder import placeholder_page


def layout(**_kwargs):
    return placeholder_page(
        "Projection",
        "tabler:chart-line",
        milestone=4,
        items=[
            "4-week projection from Garmin and trend-based methods, with uncertainty bands",
            "Expenditure (TDEE) estimates",
            "Data completeness for the last 7 food days",
        ],
    )


dash.register_page(
    __name__,
    path="/projection",
    name="Projection",
    title="Projection · Weight Coach",
    order=2,
    layout=layout,
)
