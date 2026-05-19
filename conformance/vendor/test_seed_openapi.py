"""OpenAPI conformance (covered shapes), run UNMODIFIED through the swap."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

app = FastAPI()


@app.get("/items/{item_id}")
def read(item_id: int, q: str = "d"):
    return {}


client = TestClient(app)


def test_openapi_json_well_formed():
    d = client.get("/openapi.json").json()
    assert d["openapi"] == "3.1.0"
    assert list(d) == ["openapi", "info", "paths", "components"]
    op = d["paths"]["/items/{item_id}"]["get"]
    assert op["operationId"] == "read_items__item_id__get"
    assert "422" in op["responses"]
    assert op["responses"]["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/HTTPValidationError"}
    schemas = d["components"]["schemas"]
    assert list(schemas) == sorted(schemas)  # MUST-17


def test_docs_ui_served():
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
