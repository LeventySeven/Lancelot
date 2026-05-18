"""Validation delegated to pydantic-core (spec §2/§9 — never reimplement;
pinned version). The 422 envelope is byte-identical to FastAPI:
{"detail":[ pydantic-core error dict, url stripped, loc source-prefixed ]}
(deep/01 §0). `input` semantics: null for missing bare scalar, raw value for
coercion fail (deep/01 §0.4)."""
import json as _json

from pydantic import TypeAdapter
from pydantic_core import ValidationError

from lancelot.params import FieldInfo


class ParamValidator:
    def __init__(self, fi: FieldInfo):
        self.fi = fi
        self._adapter = TypeAdapter(fi.annotation)

    def validate(self, raw, *, present: bool):
        """Returns (value, errors). errors == [] on success. `present` is False
        when the param was absent (→ missing vs coercion failure)."""
        loc_prefix = [self.fi.marker.in_, self.fi.alias or self.fi.name]
        if not present:
            if self.fi.required:
                return None, [{"type": "missing", "loc": loc_prefix,
                               "msg": "Field required", "input": None}]
            return self.fi.default, []
        try:
            return self._adapter.validate_python(raw), []
        except ValidationError as e:
            return None, _regenerate(e, loc_prefix)


def _regenerate(exc: ValidationError, loc_prefix):
    """FastAPI _compat.v2._regenerate_error_with_loc: pydantic-core errors,
    url stripped (include_url=False), loc left-prefixed (deep/01 §0.2)."""
    out = []
    for err in exc.errors(include_url=False):
        e = {"type": err["type"],
             "loc": list(loc_prefix) + list(err.get("loc", ())),
             "msg": err["msg"],
             "input": err.get("input")}
        if "ctx" in err:
            e["ctx"] = err["ctx"]
        out.append(e)
    return out


def compile_validator(fi: FieldInfo) -> ParamValidator:
    return ParamValidator(fi)


def errors_to_422(errors: list) -> tuple[int, bytes]:
    return 422, _json.dumps({"detail": errors}).encode()
