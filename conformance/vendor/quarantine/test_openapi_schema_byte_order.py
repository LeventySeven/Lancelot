"""P7 LEDGER (quarantined, excluded from the ratchet): exact RAW-wire key
order of pydantic-generated component schemas. FastAPI re-emits every
schema through its openapi.models.Schema pydantic model (field order:
`type` before `title`, `properties, type, required, title` at model
level); vanilla pydantic (model_json_schema / generate_definitions) gives
`title` first. Lancelot's OpenAPI is STRUCTURALLY identical (values
byte-identical — see tests/test_m1_p3_byte_parity.py); only this nested
key ORDER differs. P7 must re-emit schemas through the FastAPI
Schema-model field order (deep/03 §7A) for full raw-byte parity. Graduate
this file into conformance/vendor/ when green."""
import pydantic
import fastapi
from fastapi.testclient import TestClient as FA
import lancelot
from lancelot.testclient import TestClient as LC


class Item(pydantic.BaseModel):
    name: str


def _b(F):
    app = F.FastAPI(title="T") if hasattr(F, "FastAPI") else F.App(title="T")

    @app.post("/x")
    def x(i: Item):
        return {}

    return app


def test_component_schema_raw_byte_key_order_matches_fastapi():
    with FA(_b(fastapi)) as fa, LC(_b(lancelot)) as lc:
        assert fa.get("/openapi.json").content == lc.get("/openapi.json").content
