"""Ask: look up food you might eat and see what it would do to today's totals.

Nothing is logged or saved unless the user presses Log or Save to library
(components/describe.py in "ask" mode).
"""

import dash
import dash_mantine_components as dmc

from gwc.web.components.common import page_header
from gwc.web.components.describe import describe_panel


def layout(**_kwargs):
    return dmc.Stack(
        [
            page_header(
                "Ask",
                "tabler:zoom-question",
                "Thinking of eating something? See its nutrition and what it would leave you "
                "for today.",
            ),
            describe_panel("ask"),
        ],
        gap="md",
    )


dash.register_page(
    __name__,
    path="/ask",
    name="Ask",
    title="Ask · Weight Coach",
    order=2,
    layout=layout,
)
