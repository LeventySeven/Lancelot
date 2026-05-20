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
        # pydantic v2's `allow_inf_nan` is a numeric Field flag, not an
        # annotated_types metadata. Pass it via a pydantic.Field annotation.
        ainf = fi.marker.constraints.get("allow_inf_nan")
        ann = fi.annotation
        if metas:
            ann = typing.Annotated[tuple([ann, *metas])]
        if ainf is not None:
            from pydantic import Field as _Pf

            ann = typing.Annotated[ann, _Pf(allow_inf_nan=ainf)]
        self._adapter = TypeAdapter(ann)

    def validate(self, raw, *, present: bool):
        """Returns (value, errors). errors == [] on success. `present` is False
        when the param was absent (→ missing vs coercion failure)."""
        # FastAPI: form/file params share the body loc bucket — a bare
        # scalar Form error is loc=["body","<name>"] (deep/01 §1 / MUST-6),
        # not ["form",...]. query/path/header/cookie keep their own bucket.
        _src = self.fi.marker.in_
        _bucket = "body" if _src in ("form", "file", "body") else _src
        # FastAPI/Pydantic v2: loc uses the *validation* key, so
        # validation_alias takes precedence over alias.
        loc_prefix = [_bucket,
                      self.fi.validation_alias or self.fi.alias or self.fi.name]
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
        inp = err.get("input")
        if isinstance(inp, (bytes, bytearray)):
            inp = {}  # defensive: never let raw bytes reach the JSON serializer
        e = {"type": err["type"],
             "loc": list(loc_prefix) + list(err.get("loc", ())),
             "msg": err["msg"],
             "input": inp}
        if "ctx" in err:
            ctx = dict(err["ctx"])
            for k, v in list(ctx.items()):
                if isinstance(v, (bytes, bytearray)):
                    ctx[k] = v.decode("utf-8", "replace")
            e["ctx"] = ctx
        out.append(e)
    return out


class BodyValidator:
    """Single pydantic-core pass over the raw bytes — NOT json.loads then
    validate_python (deep/01 §0.3 / spec §2 "one Rust pass"). Malformed JSON
    surfaces as pydantic-core's own `json_invalid` error (loc=("body",pos),
    ctx.error) — byte-identical to FastAPI routing.py:427-441."""

    def __init__(self, model):
        self._model = model  # the annotation (used by openapi.py)
        self._adapter = TypeAdapter(model)

    def validate_json(self, raw: bytes):
        try:
            return self._adapter.validate_json(raw), []  # single pass (valid path)
        except ValidationError as e:
            errs = _regenerate(e, ["body"])
            # Malformed JSON: FastAPI synthesizes this from STDLIB json
            # (json.loads → JSONDecodeError), not pydantic-core, so the
            # msg/pos/ctx are byte-identical only if we do the same. The
            # extra json.loads is on the error path only (valid bodies stay
            # single-pass, per spec §2).
            if any(x["type"] == "json_invalid" for x in errs):
                try:
                    _json.loads(raw)
                except _json.JSONDecodeError as je:
                    return None, [{"type": "json_invalid",
                                   "loc": ["body", je.pos],
                                   "msg": "JSON decode error",
                                   "input": {},
                                   "ctx": {"error": je.msg}}]
            return None, errs


class FormBodyValidator:
    """Form-body bucket: `Annotated[FormModel, Form()]` parses the
    urlencoded body into a Pydantic model. FastAPI rule: per-field, if
    the annotation is a sequence, use multi-values; else scalar (last).
    `model_config={"extra":"allow"}` passes through unknown keys
    automatically (pydantic handles it). Multipart is ledgered RED."""

    def __init__(self, model):
        import types as _types
        import typing as _t
        from pydantic_core import PydanticUndefined

        self._model = model
        self._adapter = TypeAdapter(model)
        seq, defaults = set(), {}

        def _is_seq_ann(ann) -> bool:
            """Detect list/tuple/set — including Optional[list[T]] /
            Union[list[T], None] / list[T] | None forms. FastAPI form
            parsing wraps a single-value field as `["x"]` when typed."""
            origin = _t.get_origin(ann) or ann
            if origin in (list, tuple, set, frozenset):
                return True
            if origin is _t.Union or origin is _types.UnionType:
                non_none = [a for a in _t.get_args(ann)
                            if a is not type(None)]
                if len(non_none) == 1:
                    return _is_seq_ann(non_none[0])
            return False

        for fname, finfo in model.model_fields.items():
            keys = {fname}
            if finfo.alias:
                keys.add(finfo.alias)
            # Pydantic v2 validation_alias (input-only) — also used by the
            # form parser to look up the incoming key. Include it.
            va = getattr(finfo, "validation_alias", None)
            if isinstance(va, str):
                keys.add(va)
            if _is_seq_ann(finfo.annotation):
                seq.update(keys)
            # FastAPI Form-model: non-None defaults are pre-populated into
            # the validation input so a missing-required error's `input`
            # reflects the model state, matching test_no_data byte-shape.
            if finfo.default is not PydanticUndefined and finfo.default is not None:
                key = finfo.alias or fname
                defaults[key] = finfo.default
        self._seq_keys = seq
        self._defaults = defaults

    def validate_form(self, pairs):
        """pairs: list[(name, value)] from urllib.parse.parse_qsl.
        Per-key: if the field is sequence-typed OR multiple values were
        sent (extra="allow" lossless rule), pass a list; else scalar."""
        multi: dict = {}
        for k, v in pairs:
            multi.setdefault(k, []).append(v)
        data: dict = dict(self._defaults)
        for k, vs in multi.items():
            data[k] = vs if (k in self._seq_keys or len(vs) > 1) else vs[0]
        try:
            return self._adapter.validate_python(data), []
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


def _json_default(obj):
    """FastAPI `jsonable_encoder`-shape fallback for json.dumps default=.
    Handles UUID, datetime, Enum, Path, BaseModel, dataclass, set/frozenset.
    Order matters: BaseModel before isinstance(uuid.UUID) so a Pydantic
    field-serializer-driven encoding wins over the UUID branch.

    Defensive: classes that LIE about being UUID via __class__ property
    (asyncpg.pgproto.UUID — see test_inherited_custom_class) → str(obj).
    Pydantic's to_jsonable_python tries `obj.int` for those and crashes."""
    # Pydantic BaseModel — model_dump returns a JSON-safe dict.
    try:
        from pydantic import BaseModel as _BM

        if isinstance(obj, _BM):
            return obj.model_dump(mode="json", by_alias=True)
    except Exception:  # noqa: BLE001
        pass
    # Dataclass instances.
    try:
        import dataclasses as _dc

        if _dc.is_dataclass(obj) and not isinstance(obj, type):
            return _dc.asdict(obj)
    except Exception:  # noqa: BLE001
        pass
    import datetime as _dt
    import decimal as _dcm
    import enum as _enum
    import pathlib as _pl
    import uuid as _uuid

    if isinstance(obj, (_dt.datetime, _dt.date, _dt.time)):
        return obj.isoformat()
    if isinstance(obj, _dt.timedelta):
        return obj.total_seconds()
    if isinstance(obj, _uuid.UUID):
        # Includes types that LIE about __class__ → str() returns their
        # display form (asyncpg pgproto.UUID, etc.).
        return str(obj)
    if isinstance(obj, _enum.Enum):
        return obj.value
    if isinstance(obj, _dcm.Decimal):
        return float(obj)
    if isinstance(obj, _pl.PurePath):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    raise TypeError(f"Object of type {type(obj).__name__} "
                    f"is not JSON serializable")


def _wire_json(obj) -> bytes:
    """The ONE wire serializer — byte-identical to Starlette/FastAPI's
    JSONResponse (compact separators, ensure_ascii=False, allow_nan=False).
    Routes through the Rust pydantic-core seam (same as the 200 path) so
    the 422 bytes provably cannot drift from the success bytes. When the
    fast path or stdlib chokes on a UUID/datetime/BaseModel value, falls
    back to a FastAPI-jsonable-encoder-shape `default=` function."""
    try:
        from lancelot import _native

        return bytes(_native._to_json(obj))
    except Exception:  # noqa: BLE001
        pass
    return _json.dumps(
        obj, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")


def errors_to_422(errors: list) -> tuple[int, bytes]:
    return 422, _wire_json({"detail": errors})
