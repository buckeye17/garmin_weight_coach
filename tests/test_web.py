import dash
import pytest

from gwc.web.app import create_app
from gwc.web.nav import NAV_ITEMS, is_active


@pytest.fixture(scope="module")
def app():
    return create_app()


def test_every_nav_item_has_a_page(app):
    paths = {page["path"] for page in dash.page_registry.values()}
    assert {item.href for item in NAV_ITEMS} <= paths


def test_page_layouts_render(app):
    for page in dash.page_registry.values():
        layout = page["layout"]
        component = layout() if callable(layout) else layout
        assert component is not None, page["path"]


@pytest.mark.parametrize("path", [item.href for item in NAV_ITEMS])
def test_routes_serve_html(app, path):
    resp = app.server.test_client().get(path)
    assert resp.status_code == 200
    assert b"Weight Coach" in resp.data


def test_dash_layout_endpoint(app):
    resp = app.server.test_client().get("/_dash-layout")
    assert resp.status_code == 200
    assert b"MantineProvider" in resp.data


@pytest.mark.parametrize(
    ("href", "pathname", "expected"),
    [
        ("/", "/", True),
        ("/", "", True),
        ("/", None, True),
        ("/", "/add", False),
        ("/add", "/add", True),
        ("/add", "/add/", True),
        ("/foods", "/foods/12", True),
        ("/foods", "/foodstuff", False),
    ],
)
def test_is_active(href, pathname, expected):
    assert is_active(href, pathname) is expected


def test_bottom_nav_highlight():
    from gwc.web.layout import _highlight_bottom_nav

    ids = [{"type": "bottom-nav", "href": i.href} for i in NAV_ITEMS if i.mobile_primary]
    colors = _highlight_bottom_nav("/projection", ids)
    assert colors == ["dimmed", "dimmed", "teal", "dimmed"]
