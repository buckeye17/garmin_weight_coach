import dash

from gwc.web.components.placeholder import placeholder_page


def layout(**_kwargs):
    return placeholder_page(
        "Coach",
        "tabler:message-chatbot",
        milestone=5,
        items=[
            "Chat with Claude about your data",
            "Weekly summaries",
        ],
    )


dash.register_page(
    __name__, path="/coach", name="Coach", title="Coach · Weight Coach", order=3, layout=layout
)
