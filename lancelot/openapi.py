"""OpenAPI 3.1 assembly — byte-faithful to FastAPI's get_openapi
(research/deep/03 §3-4). Component schemas come from pydantic
GenerateJsonSchema (spec §3/§9 — never reimplemented). The long tail
(Input/Output split, discriminated unions, security schemes, webhooks,
examples, additional responses) is driven up by the P7 conformance ratchet
via conformance/vendor/quarantine/."""
import re
import typing

from pydantic import TypeAdapter
from pydantic.json_schema import GenerateJsonSchema

_REF_TEMPLATE = "#/components/schemas/{model}"


def operation_id(route) -> str:
    # MUST-32: re.sub(r"\W","_", name + path_format) + "_" + method.lower()
    base = re.sub(r"\W", "_", f"{route.name}{route.path}")
    return f"{base}_{route.method.lower()}"


def summary(route) -> str:
    # MUST-18: route.summary or route.name.replace("_"," ").title()
    return route.name.replace("_", " ").title()


def _field_schema(fi) -> dict:
    # pydantic produces the JSON-Schema; FastAPI injects a title for inline
    # (non-$ref) schemas (MUST-26); $ref schemas get no injected title.
    s = TypeAdapter(fi.annotation).json_schema(ref_template=_REF_TEMPLATE)
    if "$ref" not in s and "title" not in s:
        s["title"] = fi.name.replace("_", " ").title()
    # Optional params carry their default in the schema (FastAPI/pydantic).
    if not fi.required and fi.default is not None and "default" not in s:
        s["default"] = fi.default
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


def _model_name(model) -> str:
    # MUST-27 normalize_name; container -> element model name
    origin = typing.get_origin(model)
    if origin in (list, set, frozenset, tuple):
        for a in typing.get_args(model):
            if isinstance(a, type) and hasattr(a, "model_json_schema"):
                return _model_name(a)
    nm = getattr(model, "__name__", str(model))
    return re.sub(r"[^a-zA-Z0-9.\-_]", "_", nm)


def request_body_for(route):
    if route.body is None:
        return None
    model = route.body[1]._model
    origin = typing.get_origin(model)
    if origin in (list, set, frozenset, tuple):
        schema = {"type": "array",
                  "items": {"$ref": f"#/components/schemas/{_model_name(model)}"}}
    else:
        schema = {"$ref": f"#/components/schemas/{_model_name(model)}"}
    return {"content": {"application/json": {"schema": schema}}, "required": True}


# Verbatim FastAPI consts (deep/03 §4.3 / openapi/utils.py:42-69).
_VALIDATION_ERROR = {
    "title": "ValidationError",
    "type": "object",
    "properties": {
        "loc": {"title": "Location", "type": "array",
                "items": {"anyOf": [{"type": "string"}, {"type": "integer"}]}},
        "msg": {"title": "Message", "type": "string"},
        "type": {"title": "Error Type", "type": "string"},
        "input": {"title": "Input"},
        "ctx": {"title": "Context", "type": "object"},
    },
    "required": ["loc", "msg", "type"],
}
_HTTP_VALIDATION_ERROR = {
    "title": "HTTPValidationError",
    "type": "object",
    "properties": {"detail": {"title": "Detail", "type": "array",
                              "items": {"$ref": "#/components/schemas/ValidationError"}}},
}


def _has_params_or_body(route) -> bool:
    return bool(route.plan.fields) or route.body is not None or bool(
        route.plan.dependencies)


def responses_for(route) -> dict:
    resp = {"200": {"description": "Successful Response",
                    "content": {"application/json": {"schema": {}}}}}
    if _has_params_or_body(route):  # MUST-20/36
        resp["422"] = {"description": "Validation Error",
                       "content": {"application/json": {"schema": {
                           "$ref": "#/components/schemas/HTTPValidationError"}}}}
    return resp


def _route_models(app) -> list:
    models, seen = [], set()
    for r in app.routes:
        if r.body is None:
            continue
        m = r.body[1]._model
        cands = []
        if typing.get_origin(m) in (list, set, frozenset, tuple):
            cands = list(typing.get_args(m))
        else:
            cands = [m]
        for c in cands:
            if isinstance(c, type) and hasattr(c, "model_json_schema") and c not in seen:
                seen.add(c)
                models.append(c)
    return models


def components_schemas(app) -> dict:
    defs = {}
    models = _route_models(app)
    if models:
        gen = GenerateJsonSchema(ref_template=_REF_TEMPLATE)
        inputs = [(m, "validation", TypeAdapter(m).core_schema) for m in models]
        _, definitions = gen.generate_definitions(inputs=inputs)
        defs.update(definitions)
    if any(_has_params_or_body(r) for r in app.routes):
        defs["ValidationError"] = _VALIDATION_ERROR
        defs["HTTPValidationError"] = _HTTP_VALIDATION_ERROR
    return {k: defs[k] for k in sorted(defs)}  # MUST-17 lexicographic


def build_openapi(app, title="Lancelot", version="0.1.0") -> dict:
    paths: dict = {}
    for r in app.routes:
        if not getattr(r, "include_in_schema", True):
            continue  # FastAPI: /docs /redoc /openapi.json are not self-documented
        op = {"summary": summary(r), "operationId": operation_id(r)}
        ps = parameters_for(r)
        if ps:
            op["parameters"] = ps
        rb = request_body_for(r)
        if rb is not None:
            op["requestBody"] = rb
        op["responses"] = responses_for(r)
        paths.setdefault(r.path, {})[r.method.lower()] = op
    doc = {  # MUST-28: wire key order = pydantic OpenAPI model field order
        "openapi": "3.1.0",
        "info": {"title": title, "version": version},
        "paths": paths,
    }
    schemas = components_schemas(app)
    if schemas:
        doc["components"] = {"schemas": schemas}
    return doc
