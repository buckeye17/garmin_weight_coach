import importlib
import pkgutil
import uuid

import dash
import flask

from gwc import __version__

ASSETS_DIR = __file__.rsplit("app.py", 1)[0] + "assets"
# Falls back to system fonts when offline.
FONTS_URL = (
    "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700"
    "&family=Plus+Jakarta+Sans:wght@600;700;800&display=swap"
)
# New on every start. Open pages poll it (assets/reload.js) and reload when it
# changes, since a page from before a restart sends callbacks the server no
# longer knows and its buttons silently stop working.
BOOT_ID = uuid.uuid4().hex
BOOT_PATH = "/_gwc/boot"


def create_app() -> dash.Dash:
    app = dash.Dash(
        __name__,
        use_pages=True,
        pages_folder="",  # pages are registered explicitly below
        assets_folder=ASSETS_DIR,
        external_stylesheets=[FONTS_URL],
        title="Weight Coach",
        update_title=None,
        suppress_callback_exceptions=True,
        meta_tags=[
            {
                "name": "viewport",
                "content": "width=device-width, initial-scale=1, viewport-fit=cover",
            },
            {"name": "theme-color", "content": "#008a59"},
        ],
    )
    _register_pages()

    from gwc.web.layout import build_layout

    app.layout = build_layout
    app.server.config["GWC_VERSION"] = __version__
    app.server.add_url_rule(BOOT_PATH, "gwc_boot", _boot)
    return app


def _boot() -> flask.Response:
    response = flask.jsonify(boot=BOOT_ID, version=__version__)
    response.headers["Cache-Control"] = "no-store"
    return response


def _register_pages() -> None:
    from gwc.web import pages

    for module in pkgutil.iter_modules(pages.__path__):
        importlib.import_module(f"{pages.__name__}.{module.name}")
