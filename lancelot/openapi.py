"""OpenAPI 3.1 assembly — byte-faithful to FastAPI's get_openapi
(research/deep/03 §3-4). Component schemas come from pydantic
GenerateJsonSchema (spec §3/§9 — never reimplemented). The long tail
(Input/Output split, discriminated unions, security schemes, webhooks,
examples, additional responses) is driven up by the P7 conformance ratchet
via conformance/vendor/quarantine/."""
import re

from pydantic import TypeAdapter

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
