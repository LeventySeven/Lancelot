import pydantic

from lancelot import App
from lancelot.openapi import components_schemas


class Beta(pydantic.BaseModel):
    b: int


class Alpha(pydantic.BaseModel):
    a: str


def test_components_sorted_and_validation_error_present():
    app = App()

    @app.post("/z")
    def z(beta: Beta):
        return {}

    @app.post("/y")
    def y(alpha: Alpha):
        return {}

    schemas = components_schemas(app)
    keys = list(schemas)
    assert keys == sorted(keys)  # MUST-17 lexicographic
    assert "Beta" in schemas and schemas["Beta"]["properties"]["b"]["type"] == "integer"
    assert "Alpha" in schemas
    assert "HTTPValidationError" in schemas and "ValidationError" in schemas
