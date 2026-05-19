import pydantic
from openapi_spec_validator import validate

from lancelot import App
from lancelot.testclient import TestClient


class M(pydantic.BaseModel):
    x: int


def test_p3_dod():
    app = App()

    @app.post("/m/{i}")
    def h(i: int, m: M):
        return {}

    validate(app.openapi())  # 3.1 meta-schema
    with TestClient(app) as c:
        d = c.get("/openapi.json").json()
        assert d["openapi"] == "3.1.0"
        assert list(d) == ["openapi", "info", "paths", "components"]
        assert c.get("/docs").status_code == 200
        assert c.get("/redoc").status_code == 200
        assert c.get("/scalar").status_code == 200
        op = d["paths"]["/m/{i}"]["post"]
        assert op["operationId"] == "h_m__i__post"
        assert "422" in op["responses"]
        assert "M" in d["components"]["schemas"]
