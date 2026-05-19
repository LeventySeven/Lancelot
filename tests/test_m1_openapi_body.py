import pydantic

from lancelot import App
from lancelot.openapi import request_body_for, _model_name


class Item(pydantic.BaseModel):
    name: str


def _route(app, path):
    return next(r for r in app.routes if r.path == path)


def test_request_body_ref():
    app = App()

    @app.post("/items")
    def create(item: Item):
        return {}

    rb = request_body_for(_route(app, "/items"))
    assert rb == {"content": {"application/json": {"schema": {
        "$ref": "#/components/schemas/Item"}}}, "required": True}
    assert _model_name(Item) == "Item"


def test_request_body_none_when_no_body():
    app = App()

    @app.get("/x")
    def x(q: int):
        return {}

    assert request_body_for(_route(app, "/x")) is None
