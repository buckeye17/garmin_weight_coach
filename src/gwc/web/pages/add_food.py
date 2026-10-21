import dash

from gwc.web.components.placeholder import placeholder_page


def layout(**_kwargs):
    return placeholder_page(
        "Add food",
        "tabler:circle-plus",
        milestone=3,
        items=[
            "Search USDA FoodData Central",
            "Pick from your foods and recipes",
            "Describe a meal in plain English and review what Claude parsed (milestone 5)",
        ],
    )


dash.register_page(
    __name__, path="/add", name="Add food", title="Add food · Weight Coach", order=1, layout=layout
)
