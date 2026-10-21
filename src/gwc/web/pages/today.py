import dash

from gwc.web.components.placeholder import placeholder_page


def layout(**_kwargs):
    return placeholder_page(
        "Today",
        "tabler:layout-dashboard",
        milestone=3,
        items=[
            "Progress bars for calories, protein, carbs, fat, fiber, sugar and sodium",
            "Remaining calories for the current food day",
            "Timeline of entries, with quick add from favorites and recents",
        ],
    )


dash.register_page(
    __name__, path="/", name="Today", title="Today · Weight Coach", order=0, layout=layout
)
