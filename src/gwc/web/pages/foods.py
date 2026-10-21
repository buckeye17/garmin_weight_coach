import dash

from gwc.web.components.placeholder import placeholder_page


def layout(**_kwargs):
    return placeholder_page(
        "Foods",
        "tabler:tools-kitchen-2",
        milestone=3,
        items=[
            "Create and edit custom foods with servings",
            "Build recipes from ingredients",
        ],
    )


dash.register_page(
    __name__, path="/foods", name="Foods", title="Foods · Weight Coach", order=5, layout=layout
)
