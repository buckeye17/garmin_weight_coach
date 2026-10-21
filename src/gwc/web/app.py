import importlib
import pkgutil

import dash

from gwc import __version__

ASSETS_DIR = __file__.rsplit("app.py", 1)[0] + "assets"


def create_app() -> dash.Dash:
    app = dash.Dash(
        __name__,
        use_pages=True,
        pages_folder="",  # pages are registered explicitly below
        assets_folder=ASSETS_DIR,
        title="Weight Coach",
        update_title=None,
        suppress_callback_exceptions=True,
        meta_tags=[
            {
                "name": "viewport",
                "content": "width=device-width, initial-scale=1, viewport-fit=cover",
            },
            {"name": "theme-color", "content": "#0ca678"},
        ],
    )
    _register_pages()

    from gwc.web.layout import build_layout

    app.layout = build_layout
    app.server.config["GWC_VERSION"] = __version__
    return app


def _register_pages() -> None:
    from gwc.web import pages

    for module in pkgutil.iter_modules(pages.__path__):
        importlib.import_module(f"{pages.__name__}.{module.name}")
