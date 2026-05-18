# Lancelot M1-P2 — Validation + Exact-422 + DI Two-Phase Compiler (the moat)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (chosen: inline serial, like M0/P1). Steps use `- [ ]`. Same discipline: RED → verify-fail → GREEN → verify-pass → commit per task; `git -C "$LC"` always (`LC=/Users/seventyleven/Desktop/Lancelot`); `export PATH="$HOME/.cargo/bin:$PATH"` + `export PYO3_PYTHON="$LC/.venv/bin/python"` for cargo/maturin; verify pytest exit code directly (never let `| tail` mask it). At sub-plan close: rust-skills clean-sweep (now genuinely applies — heavy FFI Rust) + a bug/vuln/plan-alignment review agent + tag + commit/push, then update project memory.

**Goal:** Build the validation/DI engine so typed params + Pydantic v2 bodies coerce exactly like FastAPI, validation failures return the **byte-identical `{"detail":[{type,loc,msg,input,ctx}]}` 422 wire-shape**, and `Depends()`/`Security()`/sub-deps/yield-teardown/`dependency_overrides` work via a registration-time-compiled two-phase plan (zero per-request signature introspection).

**Architecture:** Validation is **delegated to pydantic-core** (spec §2/§9 — never reimplement; pinned version) — per-param/body `TypeAdapter` compiled **once at registration**; the 422 envelope is assembled from `ValidationError.errors(include_url=False)` with `loc` left-prefixed by the source bucket (exactly FastAPI's `_regenerate_error_with_loc`). DI is **two-phase**: `compile_dependant(fn)->Plan` (signature → immutable DAG → flat topological order + interned cache slots, registration-time only) and `solve(plan, request)->kwargs|errors` (execute-only; `AsyncExitStack` yield-teardown PEP-789; `dependency_overrides`; `use_cache`). Runs on the M1-P1 `app.asgi()` path (same pinned pydantic-core as the future Rust seam ⇒ no wire drift; unifying onto the FFI `process_one` stays the tracked carry-forward). The 5 nastiest edge cases + long-tail are driven up by the **conformance ratchet** (the instrument is live from M1-P1); P2 delivers the engine + the common-case byte-exactness, P7 ramps the %.

**Tech Stack:** Python 3.13 `.venv`; `pydantic>=2.7` / `pydantic_core` (already a runtime dep); the reused `_native._to_json` seam; `pytest`/`starlette`/`httpx` (test). No new Rust this sub-plan (the ASGI path validates in Python via pydantic-core; the byte-exact-422-in-Rust-at-the-seam is the fast-path concern, tracked carry-forward — same pinned pydantic-core so identical bytes).

**Grounding (authoritative — cited, no re-derivation):** `research/deep/01-fastapi-spec-params-di-validation.md` §0 (the 422 contract — MUST 1-8), §1 (required-ness = default not type), §2 (alias/Annotated precedence), §4 (DI resolution/cache/overrides), §5 (yield teardown); WBS `research/deep/09` §C.2 U16/U15; spec §2/§7/§9.

**M1-P2 Definition of Done (verified in Task 12):**
1. Byte-exact 422 for the §0.3 canonical bodies: missing scalar query (`input:null`), int_parsing query/path, `string_too_short`+`ctx.min_length`, `greater_than`+`ctx.gt` (numeric ctx type tracks declared type), nested-body-list-index loc, multi-error stable order, malformed-JSON `json_invalid`, unparseable→400.
2. Required-ness decided by the **default**, not the type (`def f(q)` required; `q=None` optional; `q: int = 0` optional).
3. `Depends()`/`Security()`: 3-level nested resolves; `use_cache` (same dep once/request); `app.dependency_overrides` swaps; **no per-request signature introspection** (plan compiled once at registration).
4. `yield`-dependency teardown runs **after** the response, in reverse order, with exception propagation into the generator (PEP-789 `AsyncExitStack`).
5. Single-pass body validation (`validate_json(bytes)` — not `json.loads` then `validate_python`).
6. Conformance ratchet rises: P2 adds real-shaped 422/Depends seed tests; `make m1-conformance` floor bumped to the new % (parity may only rise).
7. No regression: `make m1-spine` + `make m0` green; `import lancelot` still starlette-free; clippy/cargo pristine (Rust unchanged).

---

## File Structure

- `lancelot/params.py` — `Query/Path/Header/Cookie/Body/Form/File` marker classes, `Depends`, `Security`, `Default()` sentinel, `Required` sentinel; `Annotated`-metadata extraction (`_extract_field(param)`).
- `lancelot/validation.py` — `compile_validator(annotation, default) -> ParamValidator`; `ParamValidator.validate(raw) -> (value, errors)`; `errors_to_422(buckets) -> (status, body_bytes)`; the `_regenerate_error_with_loc` + `input`-semantics rules; single-pass `validate_json` for body models.
- `lancelot/di.py` — `Dependant`, `compile_dependant(fn) -> Plan`, `solve(plan, request) -> SolveResult` (`.kwargs` | `.errors`), `AsyncExitStack` teardown, `use_cache`, `dependency_overrides` lookup.
- `lancelot/requests.py` — a minimal `Request` (method, url/path, `query_params` multidict, `headers`, `cookies`, `path_params`, `await body()/json()`, `.app`, `.state`) the DI/validation layer reads (ASGI scope/receive backed).
- Modify `lancelot/asgi.py` — `_http` builds `Request`, runs the route's compiled `Plan`, returns 422 on errors else calls handler with solved kwargs; registration compiles the plan once.
- Modify `lancelot/routing.py` — `Route` compiles `Plan` + per-param validators at construction (registration-time), stores `route.plan`.
- Tests: `tests/test_m1_params.py`, `test_m1_validation_422.py`, `test_m1_validation_body.py`, `test_m1_di.py`, `test_m1_di_yield.py`, `test_m1_p2_end2end.py`, `test_m1_p2_acceptance.py`; conformance: extend `conformance/vendor/` with `test_seed_422.py`, `test_seed_depends.py`.

---

### Task 1: Param markers + `Default`/`Required` sentinels + `Annotated` extraction

**Files:** Create `lancelot/params.py`; Test `tests/test_m1_params.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_m1_params.py
import typing

from lancelot.params import Query, Path, Depends, Default, Required, extract_field


def test_markers_and_annotated_extraction():
    q = Query(default=5, ge=1)
    assert q.default == 5 and q.constraints == {"ge": 1} and q.in_ == "query"
    assert Path().default is Required          # Path has no default => required
    d = Depends(lambda: 1)
    assert callable(d.dependency)

    def handler(a: int, b: typing.Annotated[int, Query(default=2)], c: str = "x"):
        ...

    sig = typing.get_type_hints(handler, include_extras=True)
    fa = extract_field("a", int, Required)
    assert fa.annotation is int and fa.required is True and fa.marker.in_ == "query"
    fb_ann = sig["b"]
    fb = extract_field("b", fb_ann, Required)
    assert fb.required is False and fb.default == 2 and fb.marker.in_ == "query"
    fc = extract_field("c", str, "x")
    assert fc.required is False and fc.default == "x"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /Users/seventyleven/Desktop/Lancelot && .venv/bin/python -m pytest tests/test_m1_params.py -q`; Expected: FAIL — `ModuleNotFoundError: lancelot.params`.

- [ ] **Step 3: Implement `lancelot/params.py`**

```python
# lancelot/params.py
"""FastAPI-compatible parameter markers. Required-ness is decided by the
DEFAULT, not the type (deep/01 §1.1). `Annotated[T, Query(...)]` is the
modern form; a bare default `= Query(...)` is the legacy form (both supported)."""
import typing
from dataclasses import dataclass, field


class _Sentinel:
    def __init__(self, name): self._n = name
    def __repr__(self): return self._n


Required = _Sentinel("Required")   # FastAPI's PydanticUndefined analogue
Default = _Sentinel("Default")     # "no explicit marker" sentinel

_CONSTRAINT_KEYS = (
    "gt", "ge", "lt", "le", "min_length", "max_length", "pattern",
    "multiple_of", "max_digits", "decimal_places",
)


class Param:
    in_: str = "query"

    def __init__(self, default=Required, *, alias=None, **constraints):
        self.default = default
        self.alias = alias
        self.constraints = {k: v for k, v in constraints.items()
                            if k in _CONSTRAINT_KEYS and v is not None}


class Query(Param): in_ = "query"
class Path(Param):
    in_ = "path"
    def __init__(self, default=Required, **kw): super().__init__(default, **kw)
class Header(Param):
    in_ = "header"
    def __init__(self, default=Required, *, convert_underscores=True, **kw):
        super().__init__(default, **kw); self.convert_underscores = convert_underscores
class Cookie(Param): in_ = "cookie"
class Body(Param): in_ = "body"
class Form(Body): in_ = "form"
class File(Body): in_ = "file"


@dataclass
class Depends:
    dependency: typing.Callable
    use_cache: bool = True


@dataclass
class Security(Depends):
    scopes: tuple = ()


@dataclass
class FieldInfo:
    name: str
    annotation: typing.Any
    required: bool
    default: typing.Any
    marker: Param
    alias: str | None = None


def _scalar_default_marker(annotation) -> Param:
    return Query()  # bare scalar with no marker defaults to Query (deep/01 §1)


def extract_field(name: str, annotation, default) -> FieldInfo:
    marker = None
    real_ann = annotation
    if typing.get_origin(annotation) is typing.Annotated:
        args = typing.get_args(annotation)
        real_ann = args[0]
        for meta in args[1:]:
            if isinstance(meta, (Param, Depends)):
                marker = meta
    if isinstance(default, (Param, Depends)):
        marker = default
        default = marker.default if isinstance(marker, Param) else Required
    if marker is None:
        marker = _scalar_default_marker(real_ann)
    eff_default = default
    if isinstance(marker, Param) and marker.default is not Required:
        eff_default = marker.default
    required = eff_default is Required
    return FieldInfo(name, real_ann, required, None if required else eff_default,
                     marker, getattr(marker, "alias", None))
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_m1_params.py -q`; Expected: PASS.

- [ ] **Step 5: Commit**

```bash
LC=/Users/seventyleven/Desktop/Lancelot
git -C "$LC" add -A && git -C "$LC" commit -q -m "feat(m1-p2): param markers + Default/Required + Annotated extraction (U16 part 1)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Per-param validator (pydantic-core) + the exact per-error dict (url-stripped, loc-prefixed)

**Files:** Create `lancelot/validation.py`; Test `tests/test_m1_validation_422.py`

- [ ] **Step 1: Write the failing test (the linchpin — byte-exact missing + int_parsing)**

```python
# tests/test_m1_validation_422.py
import json

from lancelot.params import extract_field, Required
from lancelot.validation import compile_validator, errors_to_422


def test_missing_scalar_query_is_byte_exact():
    fi = extract_field("query", int, Required)
    v = compile_validator(fi)
    value, errs = v.validate(None, present=False)   # missing
    assert errs == [{"type": "missing", "loc": ["query", "query"],
                     "msg": "Field required", "input": None}]


def test_int_parsing_query_is_byte_exact():
    fi = extract_field("query", int, Required)
    v = compile_validator(fi)
    value, errs = v.validate("42.5", present=True)
    assert errs == [{"type": "int_parsing", "loc": ["query", "query"],
                     "msg": "Input should be a valid integer, unable to parse string as an integer",
                     "input": "42.5"}]


def test_envelope_is_detail_list_422():
    status, body = errors_to_422([{"type": "missing", "loc": ["query", "q"],
                                   "msg": "Field required", "input": None}])
    assert status == 422
    assert json.loads(body) == {"detail": [{"type": "missing", "loc": ["query", "q"],
                                            "msg": "Field required", "input": None}]}
    assert b'"url"' not in body  # MUST NOT emit url (deep/01 §0.2)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_m1_validation_422.py -q`; Expected: FAIL — `ModuleNotFoundError: lancelot.validation`.

- [ ] **Step 3: Implement `lancelot/validation.py`**

```python
# lancelot/validation.py
"""Validation delegated to pydantic-core (spec §2/§9 — never reimplement;
pinned version). The 422 envelope is byte-identical to FastAPI:
{"detail":[ pydantic-core error dict with url stripped, loc source-prefixed ]}
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
        """Returns (value, errors). errors = [] on success. `present` is False
        when the param was absent from the request (→ missing vs coercion)."""
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_m1_validation_422.py -q`; Expected: PASS (pydantic-core emits `type:"int_parsing"`, the verbatim msg, `input:"42.5"`).

- [ ] **Step 5: Commit**

```bash
git -C "$LC" add -A && git -C "$LC" commit -q -m "feat(m1-p2): pydantic-core per-param validator + byte-exact 422 per-error dict (U16 part 2)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Constraints + `ctx` (numeric ctx type tracks declared type) + `input` semantics

**Files:** Modify `lancelot/validation.py`; Test extend `tests/test_m1_validation_422.py`

- [ ] **Step 1: Add failing tests (the §0.3 canonical constraint bodies)**

```python
# append to tests/test_m1_validation_422.py
import typing
from lancelot.params import Path


def test_string_too_short_ctx():
    fi = extract_field("item_id", typing.Annotated[str, Path(min_length=3)], Required)
    _, errs = compile_validator(fi).validate("fo", present=True)
    assert errs == [{"type": "string_too_short", "loc": ["path", "item_id"],
                     "msg": "String should have at least 3 characters",
                     "input": "fo", "ctx": {"min_length": 3}}]


def test_gt_ctx_type_tracks_declared_type():
    fi_f = extract_field("item_id", typing.Annotated[float, Path(gt=3)], Required)
    _, ef = compile_validator(fi_f).validate("2", present=True)
    assert ef[0]["ctx"] == {"gt": 3.0}                 # float → 3.0
    fi_i = extract_field("item_id", typing.Annotated[int, Path(gt=3)], Required)
    _, ei = compile_validator(fi_i).validate("2", present=True)
    assert ei[0]["ctx"] == {"gt": 3}                   # int → 3
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_m1_validation_422.py -q -k "ctx"`; Expected: FAIL — constraints are on the `Param` marker but `compile_validator` builds `TypeAdapter(annotation)` ignoring them, so no `string_too_short`/`gt` error is produced (test sees `[]` or wrong shape).

- [ ] **Step 3: Apply the constraints to the adapter (pydantic `Field`/annotated-types)**

Replace `ParamValidator.__init__`:

```python
import annotated_types as at
import typing

_CONSTRAINT_TO_AT = {
    "gt": at.Gt, "ge": at.Ge, "lt": at.Lt, "le": at.Le,
    "min_length": at.MinLen, "max_length": at.MaxLen, "multiple_of": at.MultipleOf,
}

class ParamValidator:
    def __init__(self, fi: FieldInfo):
        self.fi = fi
        metas = []
        for k, val in fi.marker.constraints.items():
            if k in _CONSTRAINT_TO_AT:
                metas.append(_CONSTRAINT_TO_AT[k](val))
            elif k == "pattern":
                metas.append(at.Predicate.__class__)  # pattern via Field below
        ann = fi.annotation
        if metas:
            ann = typing.Annotated[tuple([ann, *metas])]
        self._adapter = TypeAdapter(ann)
```

(`pattern`/`max_digits`/`decimal_places` route through pydantic `Field` — add only if a conformance test needs them; P7 ratchet drives that. Numeric `ctx` type already tracks the declared type because pydantic-core coerces the constraint to the field's type — verified by the test.)

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_m1_validation_422.py -q`; Expected: PASS (all 422 tests incl. ctx).

- [ ] **Step 5: Commit**

```bash
git -C "$LC" add -A && git -C "$LC" commit -q -m "feat(m1-p2): constraints via annotated-types + ctx numeric-type tracking (U16 part 3)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Body model — single-pass `validate_json(bytes)` + nested loc + json_invalid/400

**Files:** Modify `lancelot/validation.py`; Test `tests/test_m1_validation_body.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_m1_validation_body.py
import pydantic

from lancelot.validation import BodyValidator


class Item(pydantic.BaseModel):
    name: str
    age: int


def test_body_single_pass_and_nested_loc():
    v = BodyValidator(list[Item])
    val, errs = v.validate_json(b'[{"age":"five"}]')
    assert {"type": "missing", "loc": ["body", 0, "name"],
            "msg": "Field required", "input": {"age": "five"}} in errs
    assert any(e["type"] == "int_parsing" and e["loc"] == ["body", 0, "age"]
               for e in errs)


def test_malformed_json_is_json_invalid_422():
    v = BodyValidator(Item)
    val, errs = v.validate_json(b'{bad')
    assert errs[0]["type"] == "json_invalid"
    assert errs[0]["loc"][0] == "body"
    assert "ctx" in errs[0] and "error" in errs[0]["ctx"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_m1_validation_body.py -q`; Expected: FAIL — `ImportError: cannot import name 'BodyValidator'`.

- [ ] **Step 3: Implement `BodyValidator` (single-pass; no json.loads-then-validate)**

```python
# add to lancelot/validation.py
class BodyValidator:
    def __init__(self, model):
        self._adapter = TypeAdapter(model)

    def validate_json(self, raw: bytes):
        try:
            return self._adapter.validate_json(raw), []   # SINGLE pydantic-core pass
        except ValidationError as e:
            return None, _regenerate(e, ["body"])
```

(pydantic-core's `validate_json` emits `type:"json_invalid"` with `loc=("body",<pos>)` and `ctx.error` for malformed JSON — byte-identical to FastAPI's `routing.py:427-441`; nested model errors carry the list index in `loc` and the offending sub-object as `input`, per deep/01 §0.3. Unparseable-body→HTTP-400 is handled at the ASGI seam in Task 11, not here.)

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_m1_validation_body.py -q`; Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C "$LC" add -A && git -C "$LC" commit -q -m "feat(m1-p2): single-pass body validate_json + nested loc + json_invalid (U16 part 4)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Error ordering — accumulate all, stable order (path→query→header→cookie→body)

**Files:** Modify `lancelot/validation.py` (add `collect_errors`); Test `tests/test_m1_validation_422.py`

- [ ] **Step 1: Add failing test**

```python
# append to tests/test_m1_validation_422.py
from lancelot.validation import collect_errors


def test_errors_accumulate_in_bucket_order():
    # two failing params, declared query before header; result must be query-first
    q = compile_validator(extract_field("q", int, Required))
    import typing as _t
    from lancelot.params import Header
    h = compile_validator(extract_field("h", _t.Annotated[int, Header()], Required))
    errs = collect_errors([(q, "x", True), (h, "y", True)])
    assert [e["loc"][0] for e in errs] == ["query", "header"]
    assert len(errs) == 2  # all accumulated, not fail-fast
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_m1_validation_422.py -q -k accumulate`; Expected: FAIL — `cannot import name 'collect_errors'`.

- [ ] **Step 3: Implement `collect_errors`**

```python
# add to lancelot/validation.py
_BUCKET_ORDER = {"path": 0, "query": 1, "header": 2, "cookie": 3, "body": 4}

def collect_errors(items):
    """items: list of (validator, raw, present) already in dependency-solve +
    declared order. Accumulate ALL errors (never fail-fast), preserving the
    caller's order (the caller emits sub-deps→path→query→header→cookie→body
    in declared field order — deep/01 §0.5). This helper only flattens."""
    out = []
    for v, raw, present in items:
        _, errs = v.validate(raw, present=present)
        out.extend(errs)
    return out
```

(Ordering is the *caller's* responsibility — the DI solver in Task 7/11 feeds validators in the deep/01 §0.5 order; `collect_errors` must not re-sort, tests assert list equality.)

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_m1_validation_422.py -q`; Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C "$LC" add -A && git -C "$LC" commit -q -m "feat(m1-p2): error accumulation (no fail-fast, caller-ordered) (U16 part 5)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: `compile_dependant(fn) -> Plan` — registration-time signature → immutable DAG

**Files:** Create `lancelot/di.py`; Test `tests/test_m1_di.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_m1_di.py
import typing

from lancelot.params import Depends, Query
from lancelot.di import compile_dependant


def dep_a():
    return "a"


def test_compile_dependant_builds_plan_at_registration():
    def handler(x: int, y: typing.Annotated[str, Query()] = "d",
                a: str = Depends(dep_a)):
        ...

    plan = compile_dependant(handler)
    # params split by kind, dependency captured, NO per-request introspection
    assert plan.handler is handler
    names = {f.name for f in plan.fields}
    assert names == {"x", "y"}                     # a is a dependency, not a field
    assert [d.call for d in plan.dependencies] == [dep_a]
    assert plan.field("x").required is True and plan.field("y").required is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_m1_di.py -q`; Expected: FAIL — `ModuleNotFoundError: lancelot.di`.

- [ ] **Step 3: Implement `compile_dependant` (registration-time only)**

```python
# lancelot/di.py
"""Two-phase DI. compile_dependant: signature → immutable DAG + flat
topological plan + interned cache slots, AT REGISTRATION (deep/01 §4, WBS
U15). solve: execute-only, into Python only for the N user callables."""
import inspect
import typing
from dataclasses import dataclass, field

from lancelot.params import Depends, FieldInfo, Required, extract_field
from lancelot.validation import compile_validator


@dataclass
class SubDep:
    call: typing.Callable
    plan: "Plan"
    use_cache: bool
    cache_slot: int


@dataclass
class Plan:
    handler: typing.Callable
    fields: list                       # list[FieldInfo] (validated params)
    validators: dict                   # name -> ParamValidator
    dependencies: list                 # list[SubDep] (topological order)
    is_async: bool

    def field(self, name): return next(f for f in self.fields if f.name == name)


def compile_dependant(fn: typing.Callable, _slots=None) -> Plan:
    slots = _slots if _slots is not None else [0]
    hints = typing.get_type_hints(fn, include_extras=True)
    sig = inspect.signature(fn)
    fields, validators, deps = [], {}, []
    for name, p in sig.parameters.items():
        ann = hints.get(name, p.annotation if p.annotation is not inspect._empty else str)
        default = p.default if p.default is not inspect._empty else Required
        dep_marker = default if isinstance(default, Depends) else None
        if typing.get_origin(ann) is typing.Annotated:
            for m in typing.get_args(ann)[1:]:
                if isinstance(m, Depends):
                    dep_marker = m
        if dep_marker is not None:
            sub = compile_dependant(dep_marker.dependency, slots)
            slot = slots[0]; slots[0] += 1
            deps.append(SubDep(dep_marker.dependency, sub, dep_marker.use_cache, slot))
            continue
        fi = extract_field(name, ann, default)
        fields.append(fi)
        validators[name] = compile_validator(fi)
    return Plan(fn, fields, validators, deps, inspect.iscoroutinefunction(fn))
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_m1_di.py -q`; Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C "$LC" add -A && git -C "$LC" commit -q -m "feat(m1-p2): compile_dependant — registration-time signature->plan DAG (U15 part 1)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: `solve(plan, request) -> kwargs|errors` — 1-level Depends, execute-only

**Files:** Modify `lancelot/di.py`; Create `lancelot/requests.py`; Test `tests/test_m1_di.py`

- [ ] **Step 1: Write the failing test**

```python
# append tests/test_m1_di.py
from lancelot.di import solve
from lancelot.requests import Request


def _req(query=None):
    return Request(method="GET", path="/", query_params=query or {}, headers={},
                   cookies={}, path_params={}, body=b"", app=None)


def test_solve_binds_params_and_one_level_depends():
    def dep(): return 99

    def handler(x: int, a: int = Depends(dep)):
        return x + a

    plan = compile_dependant(handler)
    res = solve(plan, _req(query={"x": "1"}))
    assert res.errors == []
    assert res.kwargs == {"x": 1, "a": 99}


def test_solve_missing_required_is_422_error():
    def handler(x: int): ...
    plan = compile_dependant(handler)
    res = solve(plan, _req(query={}))
    assert res.kwargs is None
    assert res.errors == [{"type": "missing", "loc": ["query", "x"],
                           "msg": "Field required", "input": None}]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_m1_di.py -q -k solve`; Expected: FAIL — `ModuleNotFoundError: lancelot.requests` / `cannot import name 'solve'`.

- [ ] **Step 3: Implement `lancelot/requests.py` + `solve`**

```python
# lancelot/requests.py
from dataclasses import dataclass, field


@dataclass
class Request:
    method: str
    path: str
    query_params: dict
    headers: dict
    cookies: dict
    path_params: dict
    body: bytes
    app: object
    _json_cache: object = None

    @property
    def state(self): return getattr(self.app, "state", None)
```

```python
# add to lancelot/di.py
@dataclass
class SolveResult:
    kwargs: dict | None
    errors: list


def _raw_for(fi, request):
    src = fi.marker.in_
    key = fi.alias or fi.name
    if src == "path":
        return request.path_params.get(key), key in request.path_params
    if src == "query":
        return request.query_params.get(key), key in request.query_params
    if src == "header":
        k = key.replace("_", "-").lower() if getattr(fi.marker, "convert_underscores", True) else key
        h = {kk.lower(): vv for kk, vv in request.headers.items()}
        return h.get(k), k in h
    if src == "cookie":
        return request.cookies.get(key), key in request.cookies
    return request.body, bool(request.body)  # body handled specially in Task 11


def solve(plan, request, _cache=None) -> SolveResult:
    cache = _cache if _cache is not None else {}
    kwargs, errors = {}, []
    # deep/01 §0.5: sub-deps depth-first first, then this fn's params
    for sub in plan.dependencies:
        if sub.use_cache and sub.call in cache:
            kwargs_name = sub
        sub_res = solve(sub.plan, request, cache)
        errors.extend(sub_res.errors)
        if sub_res.errors:
            continue
        if sub.use_cache and sub.call in cache:
            val = cache[sub.call]
        else:
            val = sub.call(**sub_res.kwargs)
            if sub.use_cache:
                cache[sub.call] = val
        # bind by the dependency's declared param name in the parent sig
        # (FastAPI binds by param name; we recorded order, map positionally)
    # bind validated params
    for fi in plan.fields:
        v = plan.validators[fi.name]
        raw, present = _raw_for(fi, request)
        value, errs = v.validate(raw, present=present)
        if errs:
            errors.extend(errs)
        else:
            kwargs[fi.name] = value
    # re-walk deps to assign by name (kept simple + correct for M1 cases)
    for sub, pname in _dep_param_names(plan):
        if sub.use_cache and sub.call in cache:
            kwargs[pname] = cache[sub.call]
        else:
            sr = solve(sub.plan, request, cache)
            if sr.errors:
                continue
            val = sub.call(**sr.kwargs)
            if sub.use_cache:
                cache[sub.call] = val
            kwargs[pname] = val
    if errors:
        return SolveResult(None, errors)
    return SolveResult(kwargs, [])


def _dep_param_names(plan):
    """Map each SubDep back to the parent handler param name (the name the
    Depends() default was attached to)."""
    import inspect
    out, i = [], 0
    sig = inspect.signature(plan.handler)
    for name, p in sig.parameters.items():
        if isinstance(p.default, Depends):
            out.append((plan.dependencies[i], name)); i += 1
    return out
```

> **Refactor note (do in Step 3, not later):** the double-walk above is intentional scaffolding for the RED→GREEN of *this* task only. Before committing, collapse to a single dependency pass that binds by `_dep_param_names` and caches once — the test asserts behavior, keep it green while simplifying. Final `solve` must walk deps once, bind by name, cache by `sub.call` when `use_cache`.

- [ ] **Step 4: Run to verify it passes (then simplify, keep green)**

Run: `.venv/bin/python -m pytest tests/test_m1_di.py -q`; Expected: PASS. Then simplify `solve` to one dep pass; re-run — still PASS.

- [ ] **Step 5: Commit**

```bash
git -C "$LC" add -A && git -C "$LC" commit -q -m "feat(m1-p2): solve() — bind params + 1-level Depends, 422 on missing (U15 part 2)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Nested sub-dependencies (3-level) + `use_cache`

**Files:** Test `tests/test_m1_di.py` (solve already recursive — this proves depth + caching; if it passes immediately, the genuine RED is the cache-identity assertion below)

- [ ] **Step 1: Write the failing test**

```python
# append tests/test_m1_di.py
def test_three_level_nested_and_use_cache():
    calls = []

    def level3(): calls.append("l3"); return 3
    def level2(a: int = Depends(level3)): return a + 1
    def level1(b: int = Depends(level2), c: int = Depends(level3)): return (b, c)

    def handler(r=Depends(level1)):
        return r

    plan = compile_dependant(handler)
    res = solve(plan, _req())
    assert res.errors == []
    assert res.kwargs == {"r": (4, 3)}
    assert calls == ["l3"]      # use_cache: level3 invoked ONCE per request
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_m1_di.py -q -k three_level`; Expected: FAIL — `calls == ["l3", "l3"]` (level3 invoked twice — cache not keyed across the shared sub-dep) OR a binding error. If it already passes, that is a RED violation: tighten to assert `len(calls) == 1` AND `res.kwargs == {"r": (4, 3)}` — re-derive until it fails for the cache reason.

- [ ] **Step 3: Fix caching in `solve`**

Ensure the single dependency pass: resolve `sub.plan` recursively passing the **shared** `cache` dict; when `sub.use_cache`, key the resolved value by `sub.call` and reuse across the whole request tree (so the diamond `level1→{level2→level3, level3}` invokes `level3` once). Concretely the final `solve` (one pass):

```python
def solve(plan, request, _cache=None) -> SolveResult:
    cache = _cache if _cache is not None else {}
    kwargs, errors = {}, []
    for sub, pname in _dep_param_names(plan):
        if sub.use_cache and sub.call in cache:
            kwargs[pname] = cache[sub.call]; continue
        sr = solve(sub.plan, request, cache)
        if sr.errors:
            errors.extend(sr.errors); continue
        val = sub.call(**sr.kwargs)
        if sub.use_cache:
            cache[sub.call] = val
        kwargs[pname] = val
    for fi in plan.fields:
        raw, present = _raw_for(fi, request)
        value, errs = plan.validators[fi.name].validate(raw, present=present)
        (errors.extend(errs) if errs else kwargs.__setitem__(fi.name, value))
    return SolveResult(None, errors) if errors else SolveResult(kwargs, [])
```

(Delete the earlier double-walk + dead `kwargs_name` line from Task 7 — this is the final single-pass form.)

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_m1_di.py -q`; Expected: PASS (`calls == ["l3"]`, `r == (4,3)`).

- [ ] **Step 5: Commit**

```bash
git -C "$LC" add -A && git -C "$LC" commit -q -m "feat(m1-p2): 3-level nested Depends + use_cache (single dep pass) (U15 part 3)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: `yield`-dependency teardown — AsyncExitStack, reverse order, after response, exception propagation

**Files:** Modify `lancelot/di.py` (async `solve_async` + exit stack); Test `tests/test_m1_di_yield.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_m1_di_yield.py
import asyncio

from lancelot.params import Depends
from lancelot.di import compile_dependant, solve_async


def test_yield_teardown_after_and_reverse_order():
    events = []

    def outer():
        events.append("outer-setup")
        try:
            yield "O"
        finally:
            events.append("outer-teardown")

    def inner(o: str = Depends(outer)):
        events.append("inner-setup")
        try:
            yield o + "I"
        finally:
            events.append("inner-teardown")

    def handler(v: str = Depends(inner)):
        events.append("handler")
        return v

    async def go():
        plan = compile_dependant(handler)
        async with solve_async(plan, _REQ) as res:
            assert res.kwargs == {"v": "OI"}
            handler_ret = handler(**res.kwargs)
            events.append("response-sent")
        return handler_ret

    from lancelot.requests import Request
    global _REQ
    _REQ = Request("GET", "/", {}, {}, {}, {}, b"", None)
    asyncio.new_event_loop().run_until_complete(go())
    assert events == ["outer-setup", "inner-setup", "handler", "response-sent",
                      "inner-teardown", "outer-teardown"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_m1_di_yield.py -q`; Expected: FAIL — `cannot import name 'solve_async'`.

- [ ] **Step 3: Implement `solve_async` (AsyncExitStack; generators = yield-deps)**

```python
# add to lancelot/di.py
import contextlib
import inspect as _inspect


def _is_gen_dep(call):
    return _inspect.isgeneratorfunction(call) or _inspect.isasyncgenfunction(call)


@contextlib.asynccontextmanager
async def solve_async(plan, request, _cache=None, _stack=None):
    """Like solve(), but yield-dependencies are entered as (async) context
    managers on a shared AsyncExitStack so teardown runs AFTER the response,
    in reverse order (PEP-789; deep/01 §5)."""
    own = _stack is None
    stack = _stack or contextlib.AsyncExitStack()
    cache = _cache if _cache is not None else {}
    if own:
        await stack.__aenter__()
    try:
        kwargs, errors = {}, []
        for sub, pname in _dep_param_names(plan):
            if sub.use_cache and sub.call in cache:
                kwargs[pname] = cache[sub.call]; continue
            async with solve_async(sub.plan, request, cache, stack) as sr:
                if sr.errors:
                    errors.extend(sr.errors); continue
                if _is_gen_dep(sub.call):
                    cm = (contextlib.asynccontextmanager(sub.call)
                          if _inspect.isasyncgenfunction(sub.call)
                          else _sync_gen_cm(sub.call))
                    val = await stack.enter_async_context(cm(**sr.kwargs))
                else:
                    val = sub.call(**sr.kwargs)
            if sub.use_cache:
                cache[sub.call] = val
            kwargs[pname] = val
        for fi in plan.fields:
            raw, present = _raw_for(fi, request)
            value, errs = plan.validators[fi.name].validate(raw, present=present)
            (errors.extend(errs) if errs else kwargs.__setitem__(fi.name, value))
        yield SolveResult(None, errors) if errors else SolveResult(kwargs, [])
    finally:
        if own:
            await stack.__aexit__(None, None, None)   # teardown AFTER response


@contextlib.asynccontextmanager
async def _sync_gen_cm(genfn, **kw):
    gen = genfn(**kw)
    val = next(gen)
    try:
        yield val
    finally:
        try:
            next(gen)
        except StopIteration:
            pass
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_m1_di_yield.py -q`; Expected: PASS — events end `…response-sent, inner-teardown, outer-teardown` (reverse, after response).

- [ ] **Step 5: Commit**

```bash
git -C "$LC" add -A && git -C "$LC" commit -q -m "feat(m1-p2): yield-dep teardown — AsyncExitStack, reverse, after response (U15 part 4)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: `dependency_overrides`

**Files:** Modify `lancelot/di.py` (consult `request.app.dependency_overrides`); Test `tests/test_m1_di.py`

- [ ] **Step 1: Write the failing test**

```python
# append tests/test_m1_di.py
def test_dependency_overrides():
    def real(): return "real"
    def fake(): return "fake"

    def handler(v: str = Depends(real)):
        return v

    class _App:
        dependency_overrides = {real: fake}
        state = None

    plan = compile_dependant(handler)
    req = Request("GET", "/", {}, {}, {}, {}, b"", _App())
    res = solve(plan, req)
    assert res.kwargs == {"v": "fake"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_m1_di.py -q -k overrides`; Expected: FAIL — returns `"real"` (overrides not consulted).

- [ ] **Step 3: Apply overrides at resolve time (both `solve` and `solve_async`)**

In the dependency loop, before invoking, resolve the effective callable:

```python
def _effective(call, request):
    ov = getattr(getattr(request, "app", None), "dependency_overrides", {}) or {}
    return ov.get(call, call)
```

Use `target = _effective(sub.call, request)` for the invoke AND recompile its plan on first use (cache the compiled override plan on the SubDep by identity), or — simpler and correct for M1 — when an override exists, `compile_dependant(target)` once and memoize on a module dict keyed by `(id(plan), sub.call)`. Cache key for `use_cache` stays `sub.call` (FastAPI keys the cache by the *original* dependency, so an override is still cached once).

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_m1_di.py -q`; Expected: PASS (all DI tests, override returns `"fake"`).

- [ ] **Step 5: Commit**

```bash
git -C "$LC" add -A && git -C "$LC" commit -q -m "feat(m1-p2): dependency_overrides (app.dependency_overrides) (U15 part 5)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: Wire into `asgi.py` — Request build → compiled Plan → 422 | handler; registration compiles once

**Files:** Modify `lancelot/routing.py` (compile plan at `Route` construction), `lancelot/asgi.py` (`_http` uses the plan); Test `tests/test_m1_p2_end2end.py`

- [ ] **Step 1: Write the failing test (end-to-end via TestClient)**

```python
# tests/test_m1_p2_end2end.py
import pydantic

from lancelot import App
from lancelot.testclient import TestClient
from lancelot.params import Depends


class Item(pydantic.BaseModel):
    name: str
    price: float


def test_typed_query_body_depends_and_422():
    app = App()

    def auth():
        return "user1"

    @app.get("/n/{n}")
    def read(n: int, q: int = 0, who: str = Depends(auth)):
        return {"n": n, "q": q, "who": who}

    @app.post("/items")
    def create(item: Item):
        return {"name": item.name, "price": item.price}

    with TestClient(app) as c:
        r = c.get("/n/5?q=3")
        assert r.status_code == 200 and r.json() == {"n": 5, "q": 3, "who": "user1"}
        bad = c.get("/n/notint")
        assert bad.status_code == 422
        assert bad.json() == {"detail": [{"type": "int_parsing",
            "loc": ["path", "n"],
            "msg": "Input should be a valid integer, unable to parse string as an integer",
            "input": "notint"}]}
        ok = c.post("/items", json={"name": "x", "price": 1.5})
        assert ok.json() == {"name": "x", "price": 1.5}
        miss = c.post("/items", json={"name": "x"})
        assert miss.status_code == 422
        assert miss.json()["detail"][0]["loc"] == ["body", "price"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_m1_p2_end2end.py -q`; Expected: FAIL — M1-P1 `_http` ignores types/Depends/body (returns 200 with un-coerced str path param / no 422).

- [ ] **Step 3: Compile the plan at registration (`routing.py`) + use it in `asgi.py`**

`lancelot/routing.py` — in `Route.__post_init__`, after the regex, compile once:

```python
        from lancelot.di import compile_dependant
        from lancelot.validation import BodyValidator
        from lancelot.params import extract_field, Required
        import inspect, typing
        self.plan = compile_dependant(self.handler)
        # body model param: a field whose annotation is a pydantic BaseModel
        self.body = None
        for fi in self.plan.fields:
            ann = fi.annotation
            if isinstance(ann, type) and hasattr(ann, "model_validate_json"):
                self.body = (fi.name, BodyValidator(ann))
        # non-body fields keep their scalar validators (already in plan)
```

`lancelot/asgi.py` `_http` — replace the M1-P1 "bind path params the handler declares" block with:

```python
    from urllib.parse import parse_qs
    from lancelot.requests import Request as LRequest
    from lancelot.di import solve_async
    from lancelot.validation import errors_to_422

    qs = parse_qs(scope.get("query_string", b"").decode())
    query = {k: v[-1] for k, v in qs.items()}
    headers = {k.decode(): val.decode() for k, val in scope.get("headers", [])}
    body = b""
    while True:
        msg = await receive()
        if msg["type"] == "http.disconnect":
            return
        body += msg.get("body", b"")
        if not msg.get("more_body", False):
            break
    params = route.match(path) or {}
    lreq = LRequest(method, path, query, headers, {}, params, body, scope["app"])

    # body model (single-pass) — its errors join the 422 bucket
    body_errors = []
    if route.body is not None:
        bname, bval = route.body
        if body:
            bv, berr = bval.validate_json(body)
            body_errors = berr
        else:
            bv, body_errors = None, [{"type": "missing", "loc": ["body", bname],
                                      "msg": "Field required", "input": None}]

    async with solve_async(route.plan, lreq) as res:
        errs = list(res.errors) + body_errors
        if errs:
            status, out = errors_to_422(errs)
            await _send_json(send, status, out)
            return
        kwargs = dict(res.kwargs)
        if route.body is not None and not body_errors:
            kwargs[route.body[0]] = bv
        result = route.handler(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        try:
            payload = bytes(_native._to_json(result))
        except Exception:  # noqa: BLE001
            payload = _json.dumps(result).encode()
        await _send_json(send, 200, payload)
```

(The body-model field must be removed from `plan.fields` validation so it is not double-validated as a scalar — in `routing.py` Step 3, after detecting `self.body`, drop that `FieldInfo` from `self.plan.fields` and its `validators` entry. Add that line: `self.plan.fields = [f for f in self.plan.fields if f.name != self.body[0]]` when `self.body`.)

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_m1_p2_end2end.py -q`; Expected: PASS (typed coercion, Depends, body model, byte-exact path int_parsing 422, body missing loc `["body","price"]`).

- [ ] **Step 5: Run the full M1 + M0 regression**

Run: `.venv/bin/python -m pytest tests/ -q` then `make m1-spine`; Expected: all green (M1-P1 spine tests still pass — they use untyped handlers which still bind; the seed 4/4 holds).

- [ ] **Step 6: Commit**

```bash
git -C "$LC" add -A && git -C "$LC" commit -q -m "feat(m1-p2): wire validation+DI into app.asgi() — registration-compiled plan, 422 or kwargs (U15/U16 closed)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: Conformance ramp + acceptance + clean-sweep + no-regression + tag

**Files:** Create `conformance/vendor/test_seed_422.py`, `conformance/vendor/test_seed_depends.py`, `tests/test_m1_p2_acceptance.py`; Modify `conformance/parity_floor.txt`

- [ ] **Step 1: Add real-shaped conformance seed (FastAPI-byte-exact, via the swap)**

```python
# conformance/vendor/test_seed_422.py
from fastapi import FastAPI
from fastapi.testclient import TestClient

app = FastAPI()


@app.get("/items/{item_id}")
def read(item_id: int, q: int):
    return {"item_id": item_id, "q": q}


client = TestClient(app)


def test_missing_query_422_byte_exact():
    r = client.get("/items/3")
    assert r.status_code == 422
    assert r.json() == {"detail": [{"type": "missing", "loc": ["query", "q"],
                                    "msg": "Field required", "input": None}]}


def test_path_int_parsing_422_byte_exact():
    r = client.get("/items/foo?q=1")
    assert r.status_code == 422
    assert r.json() == {"detail": [{"type": "int_parsing", "loc": ["path", "item_id"],
        "msg": "Input should be a valid integer, unable to parse string as an integer",
        "input": "foo"}]}
```

```python
# conformance/vendor/test_seed_depends.py
from fastapi import FastAPI
from fastapi.testclient import TestClient

app = FastAPI()


def common(skip: int = 0):
    return {"skip": skip}


@app.get("/x")
def x(c: dict = __import__("fastapi").Depends(common)):
    return c


client = TestClient(app)


def test_depends_default():
    assert client.get("/x").json() == {"skip": 0}
    assert client.get("/x?skip=5").json() == {"skip": 5}
```

- [ ] **Step 2: Run the conformance instrument; record the new %**

Run: `.venv/bin/python conformance/report.py`; Expected: prints `conformance: N/M = P%` with M now ≥ 8 (4 spine + 2 422 + 1 depends + …) and P high (target: these byte-exact tests pass). If a 422 test fails, the message/loc is not byte-exact — fix `validation.py`/`asgi.py` until `report.py` shows them green (this is the real conformance-driven loop).

- [ ] **Step 3: Bump the ratchet floor (parity may only rise)**

Set `conformance/parity_floor.txt` to the integer floor just achieved (e.g. if `report.py` shows `100.0%`, write `100`; if `87.5%`, write `87`). Run `make m1-conformance`; Expected: exit 0, `(floor <new>%)`.

- [ ] **Step 4: Acceptance + full regression**

```python
# tests/test_m1_p2_acceptance.py
from lancelot import App
from lancelot.testclient import TestClient
from lancelot.params import Depends


def test_p2_dod():
    app = App()

    def d(): return 7

    @app.get("/a/{i}")
    def a(i: int, n: int = 1, x: int = Depends(d)):
        return {"i": i, "n": n, "x": x}

    with TestClient(app) as c:
        assert c.get("/a/2?n=9").json() == {"i": 2, "n": 9, "x": 7}
        assert c.get("/a/bad").status_code == 422
```

Run, in order, each exit-checked:
```bash
cd /Users/seventyleven/Desktop/Lancelot
export PATH="$HOME/.cargo/bin:$PATH"; export PYO3_PYTHON="$PWD/.venv/bin/python"
.venv/bin/python -m pytest tests/ -q                       # all M1 + M0 py tests
make m1-spine                                              # spine + ratchet
make m0                                                    # NO M0 regression (all 7 gates)
cargo clippy --all-targets --features dhat-heap 2>&1 | tail -1   # pristine (Rust unchanged)
.venv/bin/python -c "import sys,lancelot; assert 'starlette' not in sys.modules; print('import clean')"
```
Expected: all green; `make m0` prints "M0 ACCEPTANCE: all 7 gates green"; clippy clean; import clean.

- [ ] **Step 5: rust-skills clean-sweep + review agent (now applies — heavy logic; FFI seam unchanged but the engine is correctness-critical)**

Invoke the applicable skills against the new code and dispatch a review agent (bugs / vulnerabilities / plan-spec-alignment, esp. byte-exact-422 calibration vs real FastAPI, DI cache/teardown correctness, ReDoS/unbounded-body still bounded). Fix blockers (TDD), ledger advisories to project memory.

- [ ] **Step 6: Tag + commit + push**

```bash
LC=/Users/seventyleven/Desktop/Lancelot
git -C "$LC" add -A && git -C "$LC" commit -q -m "test(m1-p2): conformance ramp + acceptance + clean-sweep (the moat green)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
git -C "$LC" tag -a m1-p2-green -m "M1-P2: byte-exact 422 + two-phase DI compiler + yield-teardown + overrides; conformance ratchet bumped. M0/M1-P1 intact."
git -C "$LC" push origin main --follow-tags
```
Then update `~/.claude/.../memory/lancelot-m1-progress.md` (P2 done; refresh the P3..P7 ledger).

---

## Self-Review

**1. Spec/WBS coverage:** U16 (pydantic-core seam, byte-exact 422, single-pass) → T2,T3,T4,T5,T11; the §0 MUST-1..8 → T2 (envelope, url-strip, loc-prefix), T3 (ctx + numeric-type), T4 (json_invalid, nested loc, single-pass), T5 (accumulate/order), T11 (input null/raw end-to-end, 400 unparseable noted). U15 (compile_dependant→Plan registration-time; solve execute-only; 3-level; use_cache; yield-teardown PEP-789; dependency_overrides; no per-request introspection) → T6,T7,T8,T9,T10,T11. Required-ness=default (deep/01 §1.1) → T1. Conformance ratchet rise (M1 gate steering) → T12. No-regression + clean-sweep → T12.

**2. Placeholder scan:** No TBD. Task 7 deliberately ships scaffolding then simplifies *within the same task* (explicit refactor note + the final single-pass form is given verbatim in Task 8 Step 3) — not a placeholder, a staged RED→GREEN with the final code present. `pattern`/`max_digits` constraints are explicitly deferred to the P7 ratchet (named, not "TODO") — they are long-tail, the WBS gates U16 on "the conformance error corpus", which the ratchet drives. `except Exception` around `_to_json` is the spec-required robustness fallback (unchanged from M1-P1).

**3. Type consistency:** `FieldInfo`/`extract_field`/`Required`/`Param.in_` (T1) consumed unchanged in T2/T6/T7/T11; `ParamValidator.validate(raw,*,present)->(value,errors)` (T2) used identically in T5/T7/T9/T11; `_regenerate(exc,loc_prefix)` (T2) reused by `BodyValidator` (T4); `Plan`/`SubDep`/`compile_dependant` (T6) consumed by `solve` (T7), `solve` superseded by the single-pass form (T8) then `solve_async` (T9) — both share `_dep_param_names`/`_raw_for`/`_effective`; `SolveResult.kwargs|errors` stable T7→T11; `route.plan`/`route.body` (T11) set at registration, read in `asgi._http`. `errors_to_422` (T2) used in T11. Consistent.

**4. Risk notes:** (a) byte-exact 422 hinges on the **pinned** pydantic-core (spec §9) — the conformance ratchet (T12) is the calibration guard; if a `msg`/`ctx` string differs, T12 Step 2 catches it before the floor bumps. (b) `solve`/`solve_async` duplication is intentional (sync fast path + async yield-teardown path); a future unification is a P-any carry-forward, not M1-P2 scope. (c) Body-model detection via `hasattr(ann,'model_validate_json')` covers pydantic BaseModel; dataclass/TypedDict bodies are long-tail → P7 ratchet. (d) the unbounded-body drain (M1-P1 A4 carry-forward) is *re-introduced here* (we now accumulate `body`); **max-body-limit must be added** — added to the P-any security ledger and flagged in T12 Step 5's review. **No issues requiring rework.**

---

*Next on completion: M1-P3 (OpenAPI 3.1 + Swagger/ReDoc/Scalar — U17, depends U16) gets its own plan, conformance-%-gated. Same approach (inline serial, spine-first-iterate, clean-sweep + review + tag/push per sub-plan).*
