"""Validation delegated to pydantic-core (spec §2/§9 — never reimplement;
pinned version). The 422 envelope is byte-identical to FastAPI:
{"detail":[ pydantic-core error dict, url stripped, loc source-prefixed ]}
(deep/01 §0). `input` semantics: null for missing bare scalar, raw value for
coercion fail (deep/01 §0.4)."""
import json as _json
import typing

import annotated_types as at
from pydantic import TypeAdapter
from pydantic_core import ValidationError

from lancelot.params import FieldInfo

# Constraints expressible as annotated-types metadata. pydantic-core coerces
# the bound to the field's declared type, so numeric `ctx` tracks int/float
# automatically (deep/01 §0.3). pattern/max_digits/decimal_places are
# long-tail — driven by the P7 conformance ratchet, not hardcoded here.
_CONSTRAINT_TO_AT = {
    "gt": at.Gt, "ge": at.Ge, "lt": at.Lt, "le": at.Le,
    "min_length": at.MinLen, "max_length": at.MaxLen, "multiple_of": at.MultipleOf,
}


class ParamValidator:
    def __init__(self, fi: FieldInfo):
        self.fi = fi
        metas = [
            _CONSTRAINT_TO_AT[k](v)
            for k, v in fi.marker.constraints.items()
            if k in _CONSTRAINT_TO_AT
        ]
        ann = fi.annotation
        if metas:
            ann = typing.Annotated[tuple([ann, *metas])]
        self._adapter = TypeAdapter(ann)

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


class BodyValidator:
    """Single pydantic-core pass over the raw bytes — NOT json.loads then
    validate_python (deep/01 §0.3 / spec §2 "one Rust pass"). Malformed JSON
    surfaces as pydantic-core's own `json_invalid` error (loc=("body",pos),
    ctx.error) — byte-identical to FastAPI routing.py:427-441."""

    def __init__(self, model):
        self._adapter = TypeAdapter(model)

    def validate_json(self, raw: bytes):
        try:
            return self._adapter.validate_json(raw), []
        except ValidationError as e:
            return None, _regenerate(e, ["body"])


def compile_validator(fi: FieldInfo) -> ParamValidator:
    return ParamValidator(fi)


def collect_errors(items):
    """items: list of (validator, raw, present) ALREADY in dependency-solve +
    declared order. Accumulate ALL errors (never fail-fast on the first),
    preserving the caller's order — the DI solver feeds validators in the
    deep/01 §0.5 order (sub-deps→path→query→header→cookie→body, declared
    field order). This helper only flattens; it must NOT re-sort (tests
    assert list equality, not set equality)."""
    out = []
    for v, raw, present in items:
        _, errs = v.validate(raw, present=present)
        out.extend(errs)
    return out


def errors_to_422(errors: list) -> tuple[int, bytes]:
    return 422, _json.dumps({"detail": errors}).encode()
