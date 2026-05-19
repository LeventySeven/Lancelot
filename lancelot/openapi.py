"""OpenAPI 3.1 assembly — byte-faithful to FastAPI's get_openapi
(research/deep/03 §3-4). Component schemas come from pydantic
GenerateJsonSchema (spec §3/§9 — never reimplemented). The long tail
(Input/Output split, discriminated unions, security schemes, webhooks,
examples, additional responses) is driven up by the P7 conformance ratchet
via conformance/vendor/quarantine/."""
import re


def operation_id(route) -> str:
    # MUST-32: re.sub(r"\W","_", name + path_format) + "_" + method.lower()
    base = re.sub(r"\W", "_", f"{route.name}{route.path}")
    return f"{base}_{route.method.lower()}"


def summary(route) -> str:
    # MUST-18: route.summary or route.name.replace("_"," ").title()
    return route.name.replace("_", " ").title()
