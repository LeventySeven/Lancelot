# Lancelot M1-P3 — OpenAPI 3.1 + Swagger/ReDoc/Scalar (U17)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (inline serial, like M0/P1/P2). `- [ ]` steps. Same discipline: RED → verify-fail → GREEN → verify-pass → commit per task; `git -C "$LC"` (`LC=/Users/seventyleven/Desktop/Lancelot`); `export PATH="$HOME/.cargo/bin:$PATH"` + `export PYO3_PYTHON="$LC/.venv/bin/python"`; verify pytest exit code directly (`rc=$?; test $rc -eq 0 && …`, never `| tail`-masked). Sub-plan close: clean-sweep + review agent + tag/push + memory. RED conformance seed for not-yet-implemented shapes goes in `conformance/vendor/quarantine/` (excluded from the ratchet) and graduates to `vendor/` when green.

**Goal:** Generate an OpenAPI **3.1** document from the registered routes' P2-compiled plans that is byte-comparable to FastAPI's `/openapi.json` for the common shapes, validates against the OpenAPI 3.1 meta-schema, and serve `/openapi.json` + `/docs` (Swagger) + `/redoc` + `/scalar`.

**Architecture:** `lancelot/openapi.py` assembles the document exactly per `research/deep/03 §3-4` (MUST-17→38): component schemas come from **pydantic's `GenerateJsonSchema(ref_template="#/components/schemas/{model}")`** (never reimplemented — spec §3/§9), `operationId = re.sub(r"\W","_", name+path)+"_"+method.lower()`, parameters/requestBody/responses in the documented key order, 422 injected with the **verbatim** `ValidationError`/`HTTPValidationError` consts, `components.schemas` keys **lexicographically sorted**, top-level wire order = the pydantic OpenAPI-model field order (`openapi, info, paths, components`). `App.openapi()` builds once (memoized) and is served as a normal GET route; the 3 doc UIs are verbatim CDN HTML shells pointing at `/openapi.json`. Correctness is proven by a **real-FastAPI byte-differential** oracle (the M1-P2 discipline that caught the moat being unverified) — common shapes byte-identical now; the long tail (Input/Output split, discriminated unions, security schemes, webhooks, examples, additional responses) is driven up by the P7 ratchet via `quarantine/`.

**Tech Stack:** Python 3.13 `.venv`; `pydantic` `GenerateJsonSchema` (already a dep); the `_native._to_json` wire serializer (reuse `validation._wire_json`); test deps `fastapi==0.136.1` (byte oracle, already installed P2), `openapi-spec-validator` (3.1 meta-schema check — install in T9), pytest/starlette/httpx. No new Rust.

**Grounding (cited, no re-derivation):** `research/deep/03 §3.1-3.6, §4.1-4.3, §6A.2, §6A.4-6A.5` (the assembly algorithm + verbatim 422 consts + doc HTML templates); WBS `research/deep/09` U17; spec §3 (`openapi` component). M1-P2 gave every `Route` a `.plan` (`fields: list[FieldInfo]`, `validators`, `dependencies`) and `.body = (name, BodyValidator)` — the OpenAPI builder reads those (no new reflection).

**M1-P3 Definition of Done (verified in Task 12):**
1. `App.openapi()` returns a dict with wire key order `openapi, info, paths, components`; `openapi == "3.1.0"`.
2. `operationId` byte-matches FastAPI's `generate_unique_id` for representative routes (`read_items_items__get` etc.).
3. Parameters: `{"name","in","required","schema"}` key order; header `_`→`-`; schema via pydantic.
4. `requestBody` for a pydantic-model body → `{"content":{"application/json":{"schema":{"$ref":"#/components/schemas/<Model>"}}},"required":true}`.
5. `responses` has the success code (`"Successful Response"`) and, when params/body exist, an injected `"422"` referencing `#/components/schemas/HTTPValidationError`; `components.schemas` includes the **verbatim** `ValidationError`+`HTTPValidationError` and is **lexicographically key-sorted**.
6. The generated document **validates against the OpenAPI 3.1 meta-schema** (`openapi-spec-validator`).
7. `/openapi.json` (JSON), `/docs` (Swagger UI HTML), `/redoc` (ReDoc HTML), `/scalar` (Scalar HTML) all 200.
8. **Byte-differential**: for ≥3 representative apps, Lancelot `/openapi.json` raw bytes == real FastAPI's (covered shapes); any divergence is either fixed or a documented `quarantine/` long-tail item — never silently passing.
9. No regression: `make m0` 7/7, `make m1-spine`, full pytest green; clippy/cargo pristine (Rust unchanged); `import lancelot` dependency-light; ratchet ≥ floor.

---

## File Structure

- `lancelot/openapi.py` — `build_openapi(app) -> dict` (top-level assembly) + helpers: `operation_id(route)`, `summary(route)`, `parameters_for(route)`, `request_body_for(route)`, `responses_for(route)`, `components_schemas(models)`, the verbatim `_VALIDATION_ERROR`/`_HTTP_VALIDATION_ERROR` consts.
- `lancelot/docs.py` — `swagger_ui_html(openapi_url, title)`, `redoc_html(...)`, `scalar_html(...)` (verbatim CDN templates, deep/03 §6A.4-6A.5).
- Modify `lancelot/app.py` — `openapi()` builds via `build_openapi(self)` once (memoized on `self._openapi_cache`); `__init__` registers the 4 doc routes (`/openapi.json`, `/docs`, `/redoc`, `/scalar`) unless disabled.
- Modify `lancelot/asgi.py` — allow a route handler to return a pre-serialized `Response` (the doc HTML / the openapi JSON) without re-`_to_json`-ing — minimal `HTMLResponse`/`JSONResponse` passthrough.
- `lancelot/responses.py` — tiny `Response`/`HTMLResponse`/`JSONResponse` (status, media_type, body bytes) so doc routes return HTML not JSON.
- Tests: `tests/test_m1_openapi_*.py` (operationid, params, body, responses, components, assembly), `tests/test_m1_docs.py`, `tests/test_m1_p3_metaschema.py`, `tests/test_m1_p3_byte_parity.py`, `tests/test_m1_p3_acceptance.py`; conformance: `conformance/vendor/test_seed_openapi.py`.

---

### Task 1: `operation_id` + `summary` (FastAPI `generate_unique_id`)

**Files:** Create `lancelot/openapi.py`; Test `tests/test_m1_openapi_opid.py`

- [ ] **Step 1: failing test**

```python
# tests/test_m1_openapi_opid.py
from lancelot.routing import Route
from lancelot.openapi import operation_id, summary


def test_operation_id_and_summary():
    r = Route("GET", "/items/", lambda: None, name="read_items")
    assert operation_id(r) == "read_items_items__get"
    rp = Route("GET", "/path_examples/{item_id}", lambda: None, name="path_examples")
    assert operation_id(rp) == "path_examples_path_examples__item_id__get"
    assert summary(Route("GET", "/x", lambda: None, name="read_items")) == "Read Items"
```

- [ ] **Step 2: verify fail** — `cd $LC && .venv/bin/python -m pytest tests/test_m1_openapi_opid.py -q` → `ModuleNotFoundError: lancelot.openapi`.

- [ ] **Step 3: implement**

```python
# lancelot/openapi.py
"""OpenAPI 3.1 assembly — byte-faithful to FastAPI's get_openapi
(research/deep/03 §3-4). Component schemas come from pydantic
GenerateJsonSchema (spec §3/§9 — never reimplemented). The long tail
(Input/Output split, discriminated unions, security schemes, webhooks,
examples, additional responses) is driven up by the P7 conformance
ratchet via conformance/vendor/quarantine/."""
import re


def operation_id(route) -> str:
    # MUST-32: re.sub(r"\W","_", name + path_format) + "_" + method.lower()
    base = re.sub(r"\W", "_", f"{route.name}{route.path}")
    method = sorted(m for m in [route.method])[0]
    return f"{base}_{method.lower()}"


def summary(route) -> str:
    # MUST-18: route.summary or route.name.replace("_"," ").title()
    return route.name.replace("_", " ").title()
```

- [ ] **Step 4: verify pass** — same pytest → PASS.
- [ ] **Step 5: commit** — `git -C "$LC" add -A && git -C "$LC" commit -q -m "feat(m1-p3): operationId + summary (FastAPI generate_unique_id, MUST-32/18)\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"`

---

### Task 2: Parameter objects (path/query/header/cookie)

**Files:** Modify `lancelot/openapi.py`; Test `tests/test_m1_openapi_params.py`

- [ ] **Step 1: failing test**

```python
# tests/test_m1_openapi_params.py
import typing
from lancelot import App
from lancelot.openapi import parameters_for


def test_parameters_shape_and_header_dash():
    app = App()

    @app.get("/items/{item_id}")
    def read(item_id: int, q: str, x_tok: typing.Annotated[
            str, __import__("lancelot.params", fromlist=["Header"]).Header()] = "d"):
        return {}

    params = parameters_for(app.routes[0])
    by = {(p["in"], p["name"]): p for p in params}
    assert list(by[("path", "item_id")]) == ["name", "in", "required", "schema"]
    assert by[("path", "item_id")]["required"] is True
    assert by[("query", "q")]["required"] is True
    assert ("header", "x-tok") in by  # underscore -> dash (MUST-33)
    assert by[("header", "x-tok")]["required"] is False
    assert by[("path", "item_id")]["schema"]["type"] == "integer"
```

- [ ] **Step 2: verify fail** — `cannot import name 'parameters_for'`.

- [ ] **Step 3: implement** (schema via pydantic `TypeAdapter.json_schema`)

```python
# add to lancelot/openapi.py
from pydantic import TypeAdapter

_REF_TEMPLATE = "#/components/schemas/{model}"


def _field_schema(fi):
    # pydantic produces the JSON-Schema; FastAPI injects a title for inline
    # (non-$ref) schemas (MUST-26). $ref schemas get no injected title.
    s = TypeAdapter(fi.annotation).json_schema(ref_template=_REF_TEMPLATE)
    if "$ref" not in s and "title" not in s:
        s["title"] = (fi.name.replace("_", " ").title())
    return s


def parameters_for(route) -> list:
    out = {}
    for fi in route.plan.fields:
        loc = fi.marker.in_
        if loc not in ("path", "query", "header", "cookie"):
            continue
        name = fi.alias or fi.name
        if loc == "header" and getattr(fi.marker, "convert_underscores", True):
            name = name.replace("_", "-")
        out[(loc, name)] = {
            "name": name,
            "in": loc,
            "required": fi.required,
            "schema": _field_schema(fi),
        }
    return list(out.values())
```

- [ ] **Step 4: verify pass.**
- [ ] **Step 5: commit** — `feat(m1-p3): OpenAPI parameter objects (MUST-33/34, key order name/in/required/schema)`

---

### Task 3: `requestBody` (pydantic-model body → $ref)

**Files:** Modify `lancelot/openapi.py`; Test `tests/test_m1_openapi_body.py`

- [ ] **Step 1: failing test**

```python
# tests/test_m1_openapi_body.py
import pydantic
from lancelot import App
from lancelot.openapi import request_body_for, _model_name


class Item(pydantic.BaseModel):
    name: str


def test_request_body_ref():
    app = App()

    @app.post("/items")
    def create(item: Item):
        return {}

    rb = request_body_for(app.routes[0])
    assert rb == {"content": {"application/json": {"schema": {
        "$ref": "#/components/schemas/Item"}}}, "required": True}
    assert _model_name(Item) == "Item"
```

- [ ] **Step 2: verify fail** — `cannot import name 'request_body_for'`.

- [ ] **Step 3: implement**

```python
# add to lancelot/openapi.py
import typing


def _model_name(model) -> str:
    # MUST-27 normalize_name; container -> element model
    origin = typing.get_origin(model)
    if origin in (list, set, frozenset, tuple):
        args = [a for a in typing.get_args(model)
                if isinstance(a, type) and hasattr(a, "model_json_schema")]
        if args:
            return _model_name(args[0])
    nm = getattr(model, "__name__", str(model))
    return re.sub(r"[^a-zA-Z0-9.\-_]", "_", nm)


def request_body_for(route):
    if route.body is None:
        return None
    _, bval = route.body
    model = bval._adapter._type  # the annotation passed to BodyValidator
    origin = typing.get_origin(model)
    if origin in (list, set, frozenset, tuple):
        inner = {"type": "array", "items": {
            "$ref": f"#/components/schemas/{_model_name(model)}"}}
        schema = inner
    else:
        schema = {"$ref": f"#/components/schemas/{_model_name(model)}"}
    return {"content": {"application/json": {"schema": schema}}, "required": True}
```

> NOTE: `BodyValidator` (P2) stores `self._adapter = TypeAdapter(model)`. Add `self._model = model` to `BodyValidator.__init__` in `lancelot/validation.py` (one line) and use `bval._model` here instead of the private `_adapter._type` — do that in Step 3.

- [ ] **Step 4: verify pass.**
- [ ] **Step 5: commit** — `feat(m1-p3): requestBody ($ref / array-of-$ref) + model name normalize (MUST-27/35)`

---

### Task 4: `responses` + 422 injection (verbatim consts)

**Files:** Modify `lancelot/openapi.py`; Test `tests/test_m1_openapi_responses.py`

- [ ] **Step 1: failing test**

```python
# tests/test_m1_openapi_responses.py
from lancelot import App
from lancelot.openapi import responses_for, _HTTP_VALIDATION_ERROR, _VALIDATION_ERROR


def test_responses_success_and_422_injection():
    app = App()

    @app.get("/x/{i}")
    def x(i: int):
        return {}

    @app.get("/noparams")
    def n():
        return {}

    r1 = responses_for(app.routes[0])
    assert r1["200"]["description"] == "Successful Response"
    assert r1["200"]["content"]["application/json"]["schema"] == {}
    assert r1["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/HTTPValidationError"}
    # no params/body -> no 422 (MUST-36)
    assert "422" not in responses_for(app.routes[1])
    assert _VALIDATION_ERROR["title"] == "ValidationError"
    assert _HTTP_VALIDATION_ERROR["title"] == "HTTPValidationError"
```

- [ ] **Step 2: verify fail.**

- [ ] **Step 3: implement** (verbatim consts from deep/03 §4.3 / utils.py:42-69)

```python
# add to lancelot/openapi.py
_VALIDATION_ERROR = {
    "title": "ValidationError", "type": "object",
    "properties": {
        "loc": {"title": "Location", "type": "array",
                "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]}},
        "msg": {"title": "Message", "type": "string"},
        "type": {"title": "Error Type", "type": "string"},
    },
    "required": ["loc", "msg", "type"],
}
_HTTP_VALIDATION_ERROR = {
    "title": "HTTPValidationError", "type": "object",
    "properties": {"detail": {"title": "Detail", "type": "array",
                              "items": {"$ref": "#/components/schemas/ValidationError"}}},
}


def _has_params_or_body(route) -> bool:
    return bool(route.plan.fields) or route.body is not None or bool(
        route.plan.dependencies)


def responses_for(route) -> dict:
    resp = {"200": {"description": "Successful Response",
                    "content": {"application/json": {"schema": {}}}}}
    if _has_params_or_body(route):
        resp["422"] = {"description": "Validation Error",
                       "content": {"application/json": {"schema": {
                           "$ref": "#/components/schemas/HTTPValidationError"}}}}
    return resp
```

- [ ] **Step 4: verify pass.**
- [ ] **Step 5: commit** — `feat(m1-p3): responses + 422 injection w/ verbatim consts (MUST-20/36)`

---

### Task 5: `components.schemas` via pydantic `GenerateJsonSchema` (sorted)

**Files:** Modify `lancelot/openapi.py`; Test `tests/test_m1_openapi_components.py`

- [ ] **Step 1: failing test**

```python
# tests/test_m1_openapi_components.py
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

    schemas = components_schemas(app)
    keys = list(schemas)
    assert keys == sorted(keys)  # MUST-17 lexicographic
    assert "Beta" in schemas and schemas["Beta"]["properties"]["b"]["type"] == "integer"
    assert "HTTPValidationError" in schemas and "ValidationError" in schemas
```

- [ ] **Step 2: verify fail.**

- [ ] **Step 3: implement** (collect body/field models → pydantic `GenerateJsonSchema`)

```python
# add to lancelot/openapi.py
from pydantic.json_schema import GenerateJsonSchema


def _route_models(app):
    models = []
    for r in app.routes:
        if r.body is not None:
            m = r.body[1]._model
            origin = typing.get_origin(m)
            if origin in (list, set, frozenset, tuple):
                for a in typing.get_args(m):
                    if isinstance(a, type) and hasattr(a, "model_json_schema"):
                        models.append(a)
            elif isinstance(m, type) and hasattr(m, "model_json_schema"):
                models.append(m)
    # de-dup, stable
    seen, uniq = set(), []
    for m in models:
        if m not in seen:
            seen.add(m)
            uniq.append(m)
    return uniq


def components_schemas(app) -> dict:
    defs = {}
    gen = GenerateJsonSchema(ref_template=_REF_TEMPLATE)
    inputs = [(TypeAdapter(m), "validation", TypeAdapter(m).core_schema)
              for m in _route_models(app)]
    if inputs:
        _, definitions = gen.generate_definitions(inputs=inputs)
        defs.update(definitions)
    if any(_has_params_or_body(r) for r in app.routes):
        defs["ValidationError"] = _VALIDATION_ERROR
        defs["HTTPValidationError"] = _HTTP_VALIDATION_ERROR
    return {k: defs[k] for k in sorted(defs)}  # MUST-17
```

> If `generate_definitions(inputs=...)` signature differs in the installed pydantic, fall back to `{m.__name__: m.model_json_schema(ref_template=_REF_TEMPLATE) for m in _route_models(app)}` (merging any nested `$defs`) — verify against the byte oracle (Task 10), which is the calibration authority.

- [ ] **Step 4: verify pass.**
- [ ] **Step 5: commit** — `feat(m1-p3): components.schemas via pydantic GenerateJsonSchema, lexicographically sorted (MUST-17/21)`

---

### Task 6: Top-level `build_openapi` (wire key order)

**Files:** Modify `lancelot/openapi.py`; Test `tests/test_m1_openapi_assembly.py`

- [ ] **Step 1: failing test**

```python
# tests/test_m1_openapi_assembly.py
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
```

- [ ] **Step 2: verify fail.**

- [ ] **Step 3: implement** (MUST-28 wire order: `openapi, info, paths, components`)

```python
# add to lancelot/openapi.py
def build_openapi(app, title="Lancelot", version="0.1.0") -> dict:
    paths: dict = {}
    for r in app.routes:
        op = {
            "summary": summary(r),
            "operationId": operation_id(r),
        }
        ps = parameters_for(r)
        if ps:
            op["parameters"] = ps
        rb = request_body_for(r)
        if rb is not None:
            op["requestBody"] = rb
        op["responses"] = responses_for(r)
        paths.setdefault(r.path, {})[r.method.lower()] = op
    doc = {
        "openapi": "3.1.0",
        "info": {"title": title, "version": version},
        "paths": paths,
    }
    schemas = components_schemas(app)
    if schemas:
        doc["components"] = {"schemas": schemas}
    return doc
```

- [ ] **Step 4: verify pass.**
- [ ] **Step 5: commit** — `feat(m1-p3): build_openapi top-level assembly, wire key order (MUST-28)`

---

### Task 7: `App.openapi()` + `/openapi.json` route + Response types

**Files:** Create `lancelot/responses.py`; Modify `lancelot/app.py`, `lancelot/asgi.py`; Test `tests/test_m1_openapi_route.py`

- [ ] **Step 1: failing test**

```python
# tests/test_m1_openapi_route.py
from lancelot import App
from lancelot.testclient import TestClient


def test_openapi_json_served():
    app = App()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    with TestClient(app) as c:
        r = c.get("/openapi.json")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        d = r.json()
        assert d["openapi"] == "3.1.0"
        assert "/ping" in d["paths"]
        # memoized: same object
        assert app.openapi() is app.openapi()
```

- [ ] **Step 2: verify fail.**

- [ ] **Step 3: implement**

`lancelot/responses.py`:
```python
class Response:
    media_type = "text/plain"

    def __init__(self, content=b"", status_code=200, media_type=None):
        self.status_code = status_code
        if media_type:
            self.media_type = media_type
        self.body = content if isinstance(content, (bytes, bytearray)) else \
            str(content).encode()


class HTMLResponse(Response):
    media_type = "text/html; charset=utf-8"


class JSONResponse(Response):
    media_type = "application/json"
```

`lancelot/app.py` — add memoized `openapi()` + register doc routes in `__init__` (after router init):
```python
        self._openapi_cache = None
        from lancelot.docs import swagger_ui_html, redoc_html, scalar_html

        def _openapi_route():
            from lancelot.responses import JSONResponse
            from lancelot.validation import _wire_json
            return JSONResponse(_wire_json(self.openapi()))
        self.get("/openapi.json")(_openapi_route)
        self.get("/docs")(lambda: __import__("lancelot.responses",
            fromlist=["HTMLResponse"]).HTMLResponse(
            swagger_ui_html("/openapi.json", "Lancelot - Swagger UI")))
        self.get("/redoc")(lambda: __import__("lancelot.responses",
            fromlist=["HTMLResponse"]).HTMLResponse(
            redoc_html("/openapi.json", "Lancelot - ReDoc")))
        self.get("/scalar")(lambda: __import__("lancelot.responses",
            fromlist=["HTMLResponse"]).HTMLResponse(
            scalar_html("/openapi.json", "Lancelot - Scalar")))
```
and:
```python
    def openapi(self) -> dict:
        if self._openapi_cache is None:
            from lancelot.openapi import build_openapi
            self._openapi_cache = build_openapi(self)
        return self._openapi_cache
```
(Remove the old stub `openapi()` body.)

`lancelot/asgi.py` `_http` — before `_wire_json(result)`, honor a `Response`:
```python
            from lancelot.responses import Response
            if isinstance(result, Response):
                await send({"type": "http.response.start",
                            "status": result.status_code,
                            "headers": [(b"content-type",
                                         result.media_type.encode())]})
                await send({"type": "http.response.body", "body": result.body})
                sent = True
                return
            payload = _wire_json(result)
```

- [ ] **Step 4: verify pass.** (Note: registering doc routes in `__init__` means every App has `/openapi.json` etc. — the M0/M1 echo/spine tests still pass because their routes are distinct; verify in Step 4 + Task 12 regression.)
- [ ] **Step 5: commit** — `feat(m1-p3): App.openapi() memoized + /openapi.json route + Response types (U17)`

---

### Task 8: Doc UIs — Swagger / ReDoc / Scalar HTML

**Files:** Create `lancelot/docs.py`; Test `tests/test_m1_docs.py`

- [ ] **Step 1: failing test**

```python
# tests/test_m1_docs.py
from lancelot import App
from lancelot.testclient import TestClient


def test_doc_uis_serve_html_pointing_at_openapi():
    with TestClient(App()) as c:
        for path, marker in [("/docs", b"swagger-ui"), ("/redoc", b"redoc"),
                             ("/scalar", b"scalar")]:
            r = c.get(path)
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/html")
            assert b"/openapi.json" in r.content
            assert marker in r.content.lower()
```

- [ ] **Step 2: verify fail** — `ModuleNotFoundError: lancelot.docs`.

- [ ] **Step 3: implement** (verbatim CDN templates, deep/03 §6A.4-6A.5 skeleton)

```python
# lancelot/docs.py
"""Doc-UI HTML shells (deep/03 §5/§6A.4-6A.5). CDN-pinned; the bodies
are the standard Swagger/ReDoc/Scalar bootstraps pointing at openapi_url."""


def swagger_ui_html(openapi_url: str, title: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>{title}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
</head><body><div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({{url:"{openapi_url}",dom_id:"#swagger-ui"}})</script>
</body></html>"""


def redoc_html(openapi_url: str, title: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>{title}</title></head>
<body><div id="redoc"></div>
<redoc spec-url="{openapi_url}"></redoc>
<script src="https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js"></script>
</body></html>"""


def scalar_html(openapi_url: str, title: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>{title}</title></head>
<body><script id="api-reference" data-url="{openapi_url}"></script>
<script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference"></script>
</body></html>"""
```

- [ ] **Step 4: verify pass.**
- [ ] **Step 5: commit** — `feat(m1-p3): Swagger/ReDoc/Scalar doc-UI HTML (deep/03 §6A)`

---

### Task 9: OpenAPI 3.1 meta-schema validation (WBS done-when)

**Files:** Test `tests/test_m1_p3_metaschema.py`

- [ ] **Step 1: install validator** — `cd $LC && .venv/bin/python -m pip install -q "openapi-spec-validator"` (test-only).

- [ ] **Step 2: failing test**

```python
# tests/test_m1_p3_metaschema.py
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

    validate(app.openapi())  # raises on any 3.1 violation
```

- [ ] **Step 3: verify** — run; if `validate` raises, the assembled doc violates 3.1 — fix `openapi.py` (the error names the offending path) until it validates. This is the WBS U17 done-when gate.
- [ ] **Step 4: verify pass.**
- [ ] **Step 5: commit** — `test(m1-p3): generated doc validates vs OpenAPI 3.1 meta-schema (U17 done-when)`

---

### Task 10: Real-FastAPI byte-differential oracle (the moat for OpenAPI)

**Files:** Test `tests/test_m1_p3_byte_parity.py`

- [ ] **Step 1: failing test** (same discipline that caught M1-P2's unverified moat)

```python
# tests/test_m1_p3_byte_parity.py
import pydantic
import pytest
import fastapi
from fastapi.testclient import TestClient as FA
import lancelot
from lancelot.testclient import TestClient as LC


class Item(pydantic.BaseModel):
    name: str
    price: float


def _build(F):
    app = F.FastAPI() if hasattr(F, "FastAPI") else F.App()
    @app.get("/items/{item_id}")
    def read(item_id: int, q: str = "d"):
        return {}
    @app.post("/things")
    def make(item: Item):
        return {}
    return app


@pytest.mark.parametrize("path", ["/openapi.json"])
def test_openapi_json_byte_identical_to_fastapi(path):
    with FA(_build(fastapi)) as fa, LC(_build(lancelot)) as lc:
        f, l = fa.get(path), lc.get(path)
        assert l.status_code == f.status_code
        # Covered-shape byte parity. Any structural divergence here that is a
        # genuine long-tail (Input/Output split, security, examples) → move
        # the offending construct into a quarantine seed + ledger; it must
        # NOT silently differ. Whitespace/key-order/$ref MUST match.
        assert l.content == f.content, (
            f"\nFastAPI : {f.content!r}\nLancelot: {l.content!r}")
```

- [ ] **Step 2: verify fail** — capture the exact byte diff.
- [ ] **Step 3: reconcile** — for each divergence: if a covered shape (key order, `$ref` prefix, operationId, 422 block, sorted schemas, parameter key order, info `version` last) → fix `openapi.py`. If a genuine long-tail (Input/Output `-Input`/`-Output` suffix, discriminated unions, security schemes, examples, additional responses, `jsonSchemaDialect`) → simplify the differential app to the covered subset for M1-P3 **and** add the divergent shape as a `conformance/vendor/quarantine/test_openapi_<shape>.py` RED seed with a one-line ledger entry. Iterate until the (covered-subset) byte assertion is green and every excluded shape is ledgered (never silently passing).
- [ ] **Step 4: verify pass** (covered subset byte-identical; long-tail quarantined+ledgered).
- [ ] **Step 5: commit** — `test(m1-p3): real-FastAPI /openapi.json byte-differential oracle; long-tail quarantined`

---

### Task 11: Conformance seed + acceptance + ratchet

**Files:** `conformance/vendor/test_seed_openapi.py`; `tests/test_m1_p3_acceptance.py`

- [ ] **Step 1: conformance seed (covered shapes, via the swap)**

```python
# conformance/vendor/test_seed_openapi.py
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
    assert "/items/{item_id}" in d["paths"]
    op = d["paths"]["/items/{item_id}"]["get"]
    assert op["operationId"] == "read_items__item_id__get"
    assert "422" in op["responses"]


def test_docs_ui_served():
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
```

- [ ] **Step 2: acceptance**

```python
# tests/test_m1_p3_acceptance.py
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

    validate(app.openapi())
    with TestClient(app) as c:
        assert c.get("/openapi.json").json()["openapi"] == "3.1.0"
        assert c.get("/docs").status_code == 200
        assert list(app.openapi()) == ["openapi", "info", "paths", "components"]
```

- [ ] **Step 3: run + bump ratchet** — `.venv/bin/python conformance/report.py` (active corpus must rise: 7 → 9 with the 2 new seed tests, all green). Set `conformance/parity_floor.txt` to the achieved integer. `make m1-conformance` → exit 0.
- [ ] **Step 4: verify** acceptance + conformance green.
- [ ] **Step 5: commit** — `test(m1-p3): conformance seed + acceptance; ratchet bumped`

---

### Task 12: Clean-sweep + review + no-regression + tag

- [ ] **Step 1:** confirm zero Rust delta since `m1-p2-green` (`git -C "$LC" diff --stat m1-p2-green..HEAD -- 'src/**' Cargo.toml`); `cargo clippy --all-targets --features dhat-heap` clean; `cargo test` green.
- [ ] **Step 2:** full regression — `.venv/bin/python -m pytest tests/ -q` (all green), `make m1-spine`, `make m0` (prints "all 7 gates green"), `import lancelot` starlette/fastapi-free.
- [ ] **Step 3:** dispatch a review agent (bugs / vulnerabilities / plan-spec alignment + **byte-calibration vs real FastAPI** for the covered OpenAPI shapes; confirm long-tail is quarantined+ledgered, not silently passing; check the doc-UI HTML has no injection via title; verify `App.openapi()` memoization isn't stale when routes added post-first-call — document the contract). Fix blockers via TDD; ledger advisories.
- [ ] **Step 4:** tag + push + memory:
```bash
LC=/Users/seventyleven/Desktop/Lancelot
git -C "$LC" add -A && git -C "$LC" commit -q -m "test(m1-p3): clean-sweep + no-regression (OpenAPI/docs green)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
git -C "$LC" tag -a m1-p3-green -m "M1-P3: OpenAPI 3.1 (byte-differential vs real FastAPI on covered shapes; validates vs 3.1 meta-schema) + Swagger/ReDoc/Scalar. M0/M1-P1/P2 intact."
git -C "$LC" push origin main --follow-tags
```
Then update `~/.claude/.../memory/lancelot-m1-progress.md` (P3 done; refresh P4..P7 ledger + the OpenAPI long-tail quarantine list).

---

## Self-Review

**1. Spec/WBS coverage:** U17 (OpenAPI 3.1 from the same type registry as U16 via pydantic GenerateJsonSchema; `/openapi.json`+`/docs`+`/redoc`+`/scalar`; validates vs 3.1 meta-schema; FastAPI-SDK-consumable) → T1 (operationId MUST-32), T2 (params MUST-33/34), T3 (requestBody MUST-35), T4 (responses+422 MUST-20/36 verbatim consts), T5 (components GenerateJsonSchema MUST-17/21 sorted), T6 (assembly MUST-28 wire order), T7 (`App.openapi()`+route), T8 (doc UIs §6A), T9 (3.1 meta-schema = the WBS done-when), T10 (real-FastAPI byte oracle = the moat discipline), T11 (conformance+ratchet), T12 (review+tag). spec §3 `openapi` line satisfied; §9 "never reimplement schema in Rust" honored (pydantic GenerateJsonSchema).

**2. Placeholder scan:** No TBD. Two explicit fallbacks (Task 5 `generate_definitions` signature drift → `model_json_schema`; Task 3 `bval._model` added in Step 3) are concrete, with the byte oracle (T10) as the calibration authority — not placeholders. Long-tail (Input/Output split, discriminated unions, security schemes, webhooks, examples, additional responses) is explicitly deferred to the P7 ratchet via `quarantine/` with a named ledger requirement in T10/T12 — named, not "TODO".

**3. Type consistency:** `operation_id`/`summary`(T1) used by `build_openapi`(T6); `parameters_for`(T2)/`request_body_for`(T3)/`responses_for`(T4)/`components_schemas`(T5) all consumed by `build_openapi`(T6) and `App.openapi()`(T7); `_REF_TEMPLATE`/`_model_name`/`_has_params_or_body` defined once (T2/T3/T4) reused (T5/T6); `_VALIDATION_ERROR`/`_HTTP_VALIDATION_ERROR`(T4) used by `components_schemas`(T5); `BodyValidator._model` added T3 Step 3, read T3/T5; `Response`/`HTMLResponse`/`JSONResponse`(T7) used by the doc routes (T7) + asgi passthrough (T7) + tested T8. `route.plan`/`route.body` are the P2 contract (unchanged). Consistent.

**4. Risks:** (a) byte-identity hinges on pydantic `GenerateJsonSchema` matching FastAPI's call — T10's real-FastAPI oracle is the guard (same discipline that exposed P2's gap). (b) Registering 4 doc routes in every `App.__init__` could collide with a user route at the same path — acceptable for M1 (FastAPI does the same; user can disable later); the M0/M1 spine tests use distinct paths (verified in T7 S4 + T12 regression). (c) `App.openapi()` memoization: if routes are added after the first `.openapi()`/first request the cache is stale — documented contract (build at first access; FastAPI has the same "schema built once" behavior), flagged for the T12 review. **No issues requiring rework.**

---

*Next on completion: M1-P4 (two-tier middleware + CORS + Security surface — U20/U21/U23) gets its own plan, conformance-ratchet-gated. Same approach.*
