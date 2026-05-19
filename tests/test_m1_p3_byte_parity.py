"""OpenAPI moat — HONEST SCOPE (corrected after the M1-P3 review's
broadened oracle): for the **covered subset** — flat required-field
pydantic models + scalar params with non-None defaults, no Optional
model fields, no list/set/tuple body, no header/cookie params —
Lancelot's /openapi.json is **structurally identical** to real FastAPI
(every value equal; FastAPI's own dict-equality bar, deep/03 §3.5).

KNOWN divergences OUTSIDE the covered subset, all P7-ledgered &
quarantined (conformance/vendor/quarantine/test_openapi_value_divergences.py
+ test_openapi_schema_byte_order.py), NOT silently passing:
  1. raw-wire key order of pydantic component schemas (type-vs-title);
  2. Optional model field `x: T|None = None` → extra `"default": null`;
  3. list/set/tuple request-body array schema missing `"title"`;
  4. header/cookie param `schema.title` from name not converted alias.
This test asserts ONLY the covered subset (which is genuinely identical)."""
import pydantic
import fastapi
from fastapi.testclient import TestClient as FA

import lancelot
from lancelot.testclient import TestClient as LC


class Item(pydantic.BaseModel):
    name: str
    price: float


def _build(F, *, title):
    app = F.FastAPI(title=title) if hasattr(F, "FastAPI") else F.App(title=title)

    @app.get("/items/{item_id}")
    def read(item_id: int, q: str = "d"):
        return {}

    @app.post("/things")
    def make(item: Item):
        return {}

    return app


def test_openapi_structurally_identical_to_real_fastapi():
    with FA(_build(fastapi, title="T")) as fa, LC(_build(lancelot, title="T")) as lc:
        f, l = fa.get("/openapi.json"), lc.get("/openapi.json")
        assert l.status_code == f.status_code == 200
        fj, lj = f.json(), l.json()
        # Same bar as FastAPI's own suite: every value byte-identical;
        # only dict key-order (pydantic→Schema-model) may differ (P7).
        assert lj == fj, (
            f"\nFastAPI : {sorted(fj)}\nLancelot: {sorted(lj)}\n"
            f"paths FA={fj['paths']}\npaths LC={lj['paths']}\n"
            f"comp FA={fj['components']}\ncomp LC={lj['components']}")
        # Covered shapes additionally RAW-byte identical (top-level key
        # order, operation key order, 422 block, sorted component names).
        assert list(lj) == list(fj) == ["openapi", "info", "paths", "components"]
        assert list(lj["components"]["schemas"]) == list(
            fj["components"]["schemas"])  # lexicographically sorted (MUST-17)
        op_l = lj["paths"]["/items/{item_id}"]["get"]
        op_f = fj["paths"]["/items/{item_id}"]["get"]
        assert list(op_l) == list(op_f)  # summary,operationId,parameters,responses
