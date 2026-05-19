"""P7 LEDGER (quarantined, excluded from the ratchet) — executable record
of the OpenAPI VALUE divergences vs real FastAPI 0.136.1 that the M1-P3
review's broadened oracle found (beyond the raw-key-order item). Each is a
genuine FastAPI-fidelity gap producing a still-3.1-valid doc; P7 closes
them by replicating FastAPI's get_definitions/field-mapping + array/alias
titling. Graduate assertions into conformance/vendor/ as P7 fixes them."""
import typing

import pydantic
import fastapi
from fastapi.testclient import TestClient as FA
import lancelot
from lancelot.testclient import TestClient as LC


class Note(pydantic.BaseModel):
    text: str
    tag: typing.Optional[str] = None  # Optional model field


class Pt(pydantic.BaseModel):
    x: int


def _b(F):
    app = F.FastAPI(title="T") if hasattr(F, "FastAPI") else F.App(title="T")

    @app.post("/note")
    def note(n: Note):
        return {}

    @app.post("/pts")
    def pts(pts: list[Pt]):
        return {}

    @app.get("/h")
    def h(x_token: typing.Annotated[
            str, (fastapi.Header if hasattr(F, "Header") else
                  __import__("lancelot.params", fromlist=["Header"]).Header)()] = "d"):
        return {}

    return app


def test_value_parity_optional_default_listbody_headeralias():
    with FA(_b(fastapi)) as fa, LC(_b(lancelot)) as lc:
        assert fa.get("/openapi.json").json() == lc.get("/openapi.json").json()
