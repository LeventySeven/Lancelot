import pydantic

from lancelot import App
from lancelot.openapi import build_openapi


class Item(pydantic.BaseModel):
    name: str


def test_top_level_wire_order_and_paths():
    app = App()

    @app.post("/items/{gid}")
    def create(gid: int, item: Item):
        return {}

    doc = build_openapi(app)
    assert list(doc) == ["openapi", "info", "paths", "components"]
    assert doc["openapi"] == "3.1.0"
    assert doc["info"] == {"title": "Lancelot", "version": "0.1.0"}
    op = doc["paths"]["/items/{gid}"]["post"]
    assert op["operationId"] == "create_items__gid__post"
    assert op["summary"] == "Create"
    assert {"in": "path", "name": "gid"}.items() <= op["parameters"][0].items()
    assert op["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Item"}
    assert "422" in op["responses"]
    assert "Item" in doc["components"]["schemas"]
