import pydantic
from openapi_spec_validator import validate

from lancelot import App


class Item(pydantic.BaseModel):
    name: str
    price: float


def test_generated_doc_validates_against_openapi_31_metaschema():
    app = App()

    @app.post("/items/{gid}")
    def create(gid: int, item: Item, q: str = "x"):
        return {}

    validate(app.openapi())  # raises on any OpenAPI 3.1 violation
