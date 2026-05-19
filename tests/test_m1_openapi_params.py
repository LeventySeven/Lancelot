import typing

from lancelot import App
from lancelot.params import Header
from lancelot.openapi import parameters_for


def test_parameters_shape_and_header_dash():
    app = App()

    @app.get("/items/{item_id}")
    def read(item_id: int, q: str, x_tok: typing.Annotated[str, Header()] = "d"):
        return {}

    route = next(r for r in app.routes if r.path == "/items/{item_id}")
    params = parameters_for(route)
    by = {(p["in"], p["name"]): p for p in params}
    assert list(by[("path", "item_id")]) == ["name", "in", "required", "schema"]
    assert by[("path", "item_id")]["required"] is True
    assert by[("query", "q")]["required"] is True
    assert ("header", "x-tok") in by  # underscore -> dash (MUST-33)
    assert by[("header", "x-tok")]["required"] is False
    assert by[("path", "item_id")]["schema"]["type"] == "integer"
