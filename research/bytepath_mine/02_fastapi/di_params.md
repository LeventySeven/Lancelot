# FastAPI DI / params / _compat / encoders — byte-path mine

**Source cluster:** `fastapi/dependencies/{models.py,utils.py}`, `fastapi/params.py`,
`fastapi/param_functions.py`, `fastapi/_compat/{__init__.py,v2.py,shared.py}`,
`fastapi/encoders.py`.  Version: FastAPI 0.136.1 (the sdist Lancelot vendored).

**Lancelot status going in:** 99.6% conformance (974/978), DI fully implemented
(`lancelot/di.py`, `lancelot/routing.py`, `lancelot/asgi.py`), `_wire_json` fallback
in `lancelot/validation.py`, `TypeAdapter.dump_json` on the 200 path in
`lancelot/asgi.py:1751`.

---

## Mechanism 1 — `Dependant.cache_key` = `(call, sorted_scopes_tuple, computed_scope)`

**File:line.** `dependencies/models.py:63-71`.

```python
@cached_property
def cache_key(self) -> DependencyCacheKey:
    scopes_for_cache = (
        tuple(sorted(set(self.oauth_scopes or []))) if self._uses_scopes else ()
    )
    return (self.call, scopes_for_cache, self.computed_scope or "")
```

The cache key is a 3-tuple: the callable, a **sorted deduplicated** scope tuple (empty
when `_uses_scopes` is False — i.e. the dep and ALL its transitive sub-deps never
consume `SecurityScopes`), and `computed_scope` ("function", "request", or "").

**Lancelot state.** HAVE — `lancelot/di.py:626-629` builds the same 3-tuple
`(sub.call, tuple(sorted(_accum_scopes)) if _uses_scopes else (), _norm_scope)`.
The scope accumulation (grandparent→parent→child) is also implemented at `di.py:616`.
The `_uses_scopes` gate mirrors FastAPI's `Dependant._uses_scopes` which recurses over
transitive deps.

**Byte-path relevance.** Cache correctness on the hot request path — an incorrect key
causes either ghost caching (wrong scope → wrong user) or excessive re-invocation (perf
cliff on auth deps called N times/request). Not a new build target; verify parity only.

---

## Mechanism 2 — `computed_scope` — yield-dep lifetime derived from `scope=` or type-inference

**File:line.** `dependencies/models.py:187-193`.

```python
@cached_property
def computed_scope(self) -> str | None:
    if self.scope:
        return self.scope
    if self.is_gen_callable or self.is_async_gen_callable:
        return "request"  # default: teardown after response
    return None            # plain dep, no async-exit-stack involvement
```

FastAPI auto-assigns `"request"` scope to ANY yield-dep that does not explicitly set
`scope="function"`. Plain async/sync callables get `None` (no teardown needed → not
entered into any `AsyncExitStack`).

**Lancelot state.** HAVE — `di.py:624-625` normalizes:
```python
_norm_scope = "function" if (getattr(sub, "_marker_scope", None) == "function") else "request"
```
and `routing.py:352-390` enforces the scope-incompatibility check at registration time.

**Byte-path relevance.** Ensures `scope="function"` yield-deps tear down BETWEEN
handler-return and response-emit (not after), which is the correct behavior for DB
transaction-like patterns where commit/rollback must precede the 200 bytes on the wire.
Operator experience, not throughput.

---

## Mechanism 3 — `solve_dependencies` — dual `AsyncExitStack` per request

**File:line.** `dependencies/utils.py:612-619, 665-676`.

```python
request_astack = request.scope.get("fastapi_inner_astack")   # "request" scope
function_astack = request.scope.get("fastapi_function_astack")  # "function" scope
...
if sub_dependant.scope == "function":
    use_astack = function_astack
solved = await _solve_generator(dependant=use_sub_dependant, stack=use_astack, ...)
```

Two distinct `AsyncExitStack` objects are stashed in `request.scope` before
`solve_dependencies` is called. The routing layer closes `function_astack` after the
handler returns but before `Response.body` is produced; `request_astack` is closed
after the full response send is complete.

**Lancelot state.** HAVE — `asgi.py:880` creates `_func_stack`; `di.py:688-690`
routes yield-deps to the correct stack. The stacks are opened and closed in
`asgi.py:1529` (function stack close) and the ASGI seam finally block (request stack).

**Byte-path relevance.** This is the implementation contract for the ONLY known
FastAPI conformance ceiling bug #1 (test_dependency_contextmanager::test_background_tasks
— the middleware race around `BaseHTTPMiddleware` streaming). The dual-stack model
itself is correctly implemented; the one remaining failure is the Starlette
`BaseHTTPMiddleware` outer wrapper race, not the stack.

---

## Mechanism 4 — `is_gen_callable` / `is_async_gen_callable` — unwrap partial + callable-instance

**File:line.** `dependencies/models.py:106-155`.

The check walks: `_impartial(call)` → `_unwrapped_call(call)` (both remove `functools.partial`
wrappers and `inspect.unwrap()` decorators) before calling `inspect.isgeneratorfunction` /
`inspect.isasyncgenfunction`. If those fail (e.g. the callable is an instance, not a
function), it falls back to `getattr(call, "__call__", None)` and checks that too —
and then repeats with `_unwrapped_call` of `__call__`. The result is cached via
`@cached_property`.

**Lancelot state.** HAVE — `di.py:667-680` implements the same unwrap walk (depth-8
loop over `_ft.partial` and `__call__`, terminating at function/method/class). The
`@cached_property` equivalent is not explicitly cached per-`Dependant` but the
`Plan` object is registered once at route-build time and reused per request (same
effect).

**Byte-path relevance.** Correctness: callable instances with `__call__` as async-gen
(common DI pattern: `class DBSession: async def __call__(self): ... yield db`) must
be detected as yield-deps. If they aren't, they bypass the `AsyncExitStack`, the
teardown never runs, and the resource leaks on every request. Not a throughput item.

---

## Mechanism 5 — `run_in_threadpool` for sync deps on the async loop

**File:line.** `dependencies/utils.py:678-680`.

```python
elif use_sub_dependant.is_coroutine_callable:
    solved = await call(**solved_result.values)
else:
    solved = await run_in_threadpool(call, **solved_result.values)
```

Plain sync callable deps are run in `starlette.concurrency.run_in_threadpool` (via
`asyncio.get_event_loop().run_in_executor(None, partial(call, **kwargs))`) so they do
not block the event loop. Async deps are `await`-ed directly.

**Lancelot state.** HAVE — `di.py:704-711`:
```python
# Plain sync dep → bounded blocking pool, never the loop
from lancelot.pool import get_pool
val = await get_pool().offload(target, **sr.kwargs)
```
Lancelot routes sync deps through its own `OffloadPool` (bounded threadpool) rather
than `run_in_threadpool`, which is correct and arguably better (bounded vs unbounded).

**Byte-path relevance.** Blocking a sync dep on the async loop blocks every in-flight
request for the duration of the sync call (GIL-held CPU or blocking I/O). Lancelot
already handles this better than FastAPI's unbounded executor.

---

## Mechanism 6 — `analyze_param` — Annotated[T, FieldInfo/Depends] extraction + PEP 695 unwrap

**File:line.** `dependencies/utils.py:393-559`.

The parameter analysis pipeline:
1. If `annotation` is a `TypeAliasType` (PEP 695 `type Foo = ...`), unwrap to `annotation.__value__` (line 404-406).
2. If `get_origin(use_annotation) is Annotated`, extract all `FieldInfo` and `params.Depends` from `get_args(annotation)[1:]` (lines 411-457). The LAST `fastapi_specific_annotation` wins (allows stacking multiple `Annotated` metadata).
3. Multi-`Annotated` constraint merge: e.g. `Annotated[int, Query(gt=2), Query(lt=10)]` — only the last `Param` is used (line 432 `fastapi_specific_annotations[-1]`). Each `FieldInfo` in the metadata contributes its validation metadata separately via `copy_field_info`.
4. Body vs Query inference (lines 510-517): `is_uploadfile_or_nonable_uploadfile_annotation` → `File`; `not field_annotation_is_scalar` → `Body`; else → `Query`.
5. `Header.convert_underscores=True` default: alias is `param_name.replace("_", "-")` when no explicit alias set (line 536-538).

**Lancelot state.** HAVE — implemented in `lancelot/routing.py` (analyze_param mirror)
and `lancelot/di.py`. PEP 695 unwrap, Annotated extraction, Body/Query inference,
Header underscore-to-hyphen, and `convert_underscores` are all confirmed at 99.6%
conformance. The `fastapi_specific_annotations[-1]` semantics (last wins for multi-Param)
are present and covered by `test_annotated_params`.

**Byte-path relevance.** Correctness / conformance only. Not a throughput item.

---

## Mechanism 7 — `get_cached_model_fields` — `@lru_cache` on model field introspection

**File:line.** `_compat/v2.py:413-415`.

```python
@lru_cache
def get_cached_model_fields(model: type[BaseModel]) -> list[ModelField]:
    return get_model_fields(model)
```

`get_model_fields` calls `model.model_fields` (a pydantic dict) and wraps each
field in a `ModelField` dataclass (which creates a `TypeAdapter` in `__post_init__`).
The `@lru_cache` means per-model introspection + TypeAdapter construction is paid once
across all requests, not once per request.

**Lancelot state.** HAVE — `lancelot/routing.py` pre-compiles per-route `Plan` objects
at registration time (the Lancelot equivalent of the `Dependant` + `ModelField` tree).
The TypeAdapter for each field is baked into the `Validator` inside the `Plan` at
`App.include_router` / route-registration time, not at request time. This is strictly
better than `@lru_cache`: the cache entry is never evicted, and the construction cost
is zero on the request hot-path.

**Byte-path relevance.** Per-request TypeAdapter construction is expensive (Pydantic
core-schema compilation). FastAPI moved this to `@lru_cache`; Lancelot moved it to
route-registration time. Lancelot has the stronger guarantee. Verify by checking that
no `TypeAdapter(T)` call occurs inside `solve_async` / `_raw_for` / `validate` on the
hot path.

---

## Mechanism 8 — `ModelField.serialize_json` — `TypeAdapter.dump_json` single-pass Rust path

**File:line.** `_compat/v2.py:215-239`.

```python
def serialize_json(self, value: Any, ...) -> bytes:
    # Uses Pydantic's dump_json() which serializes directly to JSON
    # bytes in one pass (via Rust), avoiding the intermediate Python dict
    # step of dump_python(mode="json") + json.dumps().
    return self._type_adapter.dump_json(value, ...)
```

FastAPI exposes `ModelField.serialize_json` as the recommended fast path. Internally
this calls pydantic-core's `TypeAdapter.dump_json`, which:
- Validates + serializes in a single Rust pass.
- Avoids a `dict` intermediate (no Python allocation of the intermediate model_dump dict).
- Produces `bytes` directly (no Python str → bytes encode step).

The older `serialize()` (line 202-213) uses `dump_python(mode="json")` which returns a
Python dict and then requires a separate `json.dumps` call.

**Lancelot state.** HAVE — `asgi.py:1751`:
```python
payload = _ta.dump_json(...)
```
`routing.py:118` also caches the TypeAdapter: "the cached TypeAdapter's `dump_json`
(~4x cheaper on a large read)". Lancelot goes directly to `dump_json` on the 200 path.

**Byte-path relevance.** This IS one of Lancelot's core proven wins (large-payload
serialization, pydantic-core `to_json` / `dump_json` ~2.3× at 69 KB). Confirmed HAVE
and working. Do not regress.

---

## Mechanism 9 — `jsonable_encoder` — recursive Python fallback with `ENCODERS_BY_TYPE` table

**File:line.** `encoders.py:84-112` (table), `encoders.py:129-364` (function).

```python
ENCODERS_BY_TYPE: dict[type[Any], Callable[[Any], Any]] = {
    bytes: lambda o: o.decode(),
    datetime.date: isoformat,
    datetime.datetime: isoformat,
    datetime.time: isoformat,
    datetime.timedelta: lambda td: td.total_seconds(),
    Decimal: decimal_encoder,   # int if exponent>=0, else float
    Enum: lambda o: o.value,
    frozenset: list,
    deque: list,
    GeneratorType: list,
    IPv4Address: str, IPv4Interface: str, IPv4Network: str,
    IPv6Address: str, IPv6Interface: str, IPv6Network: str,
    NameEmail: str,
    Path: str,
    Pattern: lambda o: o.pattern,
    SecretBytes: str, SecretStr: str,
    set: list,
    UUID: str,
    Url: str,
    AnyUrl: str,
}
```

The function is fully recursive: `BaseModel` → `model_dump(mode="json")` → recurse;
`dataclass` → `dataclasses.asdict()` → recurse; `dict` → key/value recurse; `list/set/frozenset`
→ item recurse. Final fallback: `dict(obj)` then `vars(obj)`. Raises `ValueError` with
all errors if both fail.

The `Decimal` encoder logic is non-obvious: `Decimal("1") → 1` (int, exponent=0),
`Decimal("1.0") → 1.0` (float, exponent=-1). This preserves round-trip fidelity for
`Numeric(10,0)` columns.

**PydanticV1 guard** (line 339): raises `PydanticV1NotSupportedError` if `obj` is a
pydantic.v1 model instance (from the compatibility shim `pydantic.v1`).

**SQLAlchemy hack** (lines 288-297): keys starting with `_sa` are excluded when
`sqlalchemy_safe=True` (default). This prevents serializing SQLAlchemy internal state.

**Lancelot state.** HAVE (partial) — `lancelot/validation.py:213-263` implements
`_json_default` with: `BaseModel.model_dump(mode="json", by_alias=True)`, dataclass,
datetime/date/time/timedelta, UUID, Enum, Decimal (→ float, not the int/float
precision-preserving logic), PurePath, set/frozenset, bytes. **GAPS vs FastAPI:**
1. `Decimal` encodes as `float(obj)` always — FastAPI's `decimal_encoder` returns
   `int` when `exponent >= 0`. Matters for `Numeric(10,0)` round-trip.
2. No `NameEmail` / `SecretBytes` / `SecretStr` / `Pattern` / `IPv4*` / `IPv6*` / `Url`
   / `AnyUrl` entries (Pydantic extras). These are only hit when users use those Pydantic
   types directly; not a common path but worth noting for completeness.
3. No `deque` / `GeneratorType` handling.
4. No `sqlalchemy_safe` / `_sa` key filtering.
5. No `PydanticV1NotSupportedError` guard (Lancelot already raises this at route
   registration time via `_compat` check, so runtime path may not matter).

**Byte-path relevance.** The fallback encoder is off the hot path for typical typed
routes (pydantic-core handles those via `dump_json`). It matters for untyped/Any-typed
return values, error bodies, or custom objects. The `Decimal` int-vs-float precision
gap (#1) is a correctness issue that could cause silent data corruption in financial
APIs. Worth fixing (one-liner).

---

## Mechanism 10 — `_should_embed_body_fields` — multi-body param wrapping decision

**File:line.** `dependencies/utils.py:888-909`.

```python
def _should_embed_body_fields(fields: list[ModelField]) -> bool:
    if not fields:
        return False
    body_param_names_set = {field.name for field in fields}
    if len(body_param_names_set) > 1:
        return True  # multiple distinct body params → must embed
    first_field = fields[0]
    if getattr(first_field.field_info, "embed", None):
        return True  # explicit Body(embed=True)
    if (isinstance(first_field.field_info, params.Form)
            and not lenient_issubclass(first_field.field_info.annotation, BaseModel)
            and not is_union_of_base_models(first_field.field_info.annotation)):
        return True  # Form with non-BaseModel type needs key/value extraction
    return False
```

When `embed=True`, the body parameter is expected under a JSON key (`{"item": {...}}`)
rather than at the top level (`{...}`). FastAPI auto-enables embedding when there are
multiple body params (OpenAPI compliance).

**Lancelot state.** HAVE — `lancelot/routing.py` and `lancelot/di.py` implement the
`embed_body_fields` flag. Verified by conformance tests for multi-body and
`Body(embed=True)`.

---

## Mechanism 11 — `Header.convert_underscores=True` default

**File:line.** `params.py:316` (class attribute), `dependencies/utils.py:820-824`.

```python
# in request_params_to_args:
convert_underscores = getattr(field.field_info, "convert_underscores", default_convert_underscores)
if convert_underscores:
    alias = get_validation_alias(field)
    if alias == field.name:
        alias = alias.replace("_", "-")
```

HTTP header names use hyphens (`X-Request-Id`) but Python params use underscores
(`x_request_id`). FastAPI auto-aliases when `convert_underscores=True` (the default).
The alias replacement happens at request time in `request_params_to_args`, not at
route registration time — so the original param name is preserved for validation
error `loc` paths.

**Lancelot state.** HAVE — confirmed at 99.6% by `test_header_params` tests.

---

## Mechanism 12 — `_get_signature` — 3.14 annotationlib fallback + eval_str

**File:line.** `dependencies/utils.py:213-225`.

```python
def _get_signature(call):
    try:
        signature = inspect.signature(call, eval_str=True)
    except NameError:
        if sys.version_info >= (3, 14):
            from annotationlib import Format
            signature = inspect.signature(call, annotation_format=Format.FORWARDREF)
        else:
            signature = inspect.signature(call)
    return signature
```

`eval_str=True` (Python 3.10+) evaluates stringified annotations at signature
inspection time. The `NameError` catch handles `if TYPE_CHECKING:` annotations that
refer to types not importable at runtime. Python 3.14 replaces `eval_str` with
`annotationlib.Format`.

**Lancelot state.** Likely HAVE — Lancelot targets Python 3.13+ and uses
`inspect.signature`. The `annotationlib` Python 3.14 path is a future-proofing item.
Check `lancelot/routing.py` and `lancelot/di.py` for `inspect.signature(call,
eval_str=True)`.

**Byte-path relevance.** Correctness only. Missing `eval_str=True` causes silent
parameter-skipping for stringified annotations (`from __future__ import annotations`),
not an exception, making it a hard-to-debug failure.

---

## Mechanism 13 — `_validate_value_with_model_field` — `deepcopy(field.default)` on missing optional

**File:line.** `dependencies/utils.py:738-746`.

```python
def _validate_value_with_model_field(*, field, value, values, loc):
    if value is None:
        if field.field_info.is_required():
            return None, [get_missing_field_error(loc=loc)]
        else:
            return deepcopy(field.default), []
    return field.validate(value, values, loc=loc)
```

Optional parameters with a mutable default (e.g. `default_factory=list`) must be
`deepcopy`-ed on every request to prevent cross-request mutation. FastAPI uses
`deepcopy` here; it is slightly expensive but correct.

**Lancelot state.** Likely HAVE — `lancelot/di.py` uses `plan.validators[fi.name].validate(raw, present=present)`.
Check whether the `Validator.validate` path calls `deepcopy` on `field.default` when
`present=False` and the field has a mutable default.

**Byte-path relevance.** Correctness. Missing `deepcopy` causes shared mutable state
across requests (e.g. list accumulation), which is a subtle data corruption bug. Not
a throughput item.

---

## Mechanism 14 — `request_params_to_args` — single-BaseModel params unwrapping

**File:line.** `dependencies/utils.py:793-850`.

When the route declares a single `Query` param whose annotation is a Pydantic BaseModel
(e.g. `item: Annotated[Item, Query()]`), FastAPI extracts ALL model fields from the
query string and validates the whole model at once (lines 795-808):
```python
if len(fields) == 1 and lenient_issubclass(first_field.field_info.annotation, BaseModel):
    fields_to_extract = get_cached_model_fields(first_field.field_info.annotation)
    single_not_embedded_field = True
```
Then calls `_validate_value_with_model_field` with `params_to_process` (the dict of all
query keys) as the `value`, letting pydantic-core validate the whole model in one call.

**Lancelot state.** HAVE — confirmed by conformance tests for `BaseModel` query params.

---

## Mechanism 15 — `Depends` dataclass — `frozen=True` + `scope` field (added recently)

**File:line.** `params.py:745-749`.

```python
@dataclass(frozen=True)
class Depends:
    dependency: Callable[..., Any] | None = None
    use_cache: bool = True
    scope: Literal["function", "request"] | None = None
```

`frozen=True` means `Depends` instances are immutable and hashable (used as dict keys
in the dependency override map and cache). The `scope` field was added (visible in the
0.136.1 source) to support `Depends(fn, scope="function")` for early yield-dep teardown.

**Lancelot state.** HAVE — `lancelot/di.py` includes `_marker_scope` attribute on the
compiled `SubDep` object, and `lancelot/routing.py` reads `scope=` from the `Depends`
marker at plan-compilation time.

---

## Mechanism 16 — `_compat/v2.py` `ModelField.__post_init__` — per-field TypeAdapter with UnsupportedFieldAttributeWarning suppression

**File:line.** `_compat/v2.py:141-166`.

```python
def __post_init__(self) -> None:
    with warnings.catch_warnings():
        if shared.PYDANTIC_VERSION_MINOR_TUPLE >= (2, 12):
            from pydantic.warnings import UnsupportedFieldAttributeWarning
            warnings.simplefilter("ignore", category=UnsupportedFieldAttributeWarning)
        field_dict = asdict(self.field_info)
        annotated_args = (
            field_dict["annotation"],
            *field_dict["metadata"],
            Field(**field_dict["attributes"]),
        )
        self._type_adapter: TypeAdapter[Any] = TypeAdapter(
            Annotated[annotated_args],
            config=self.config,
        )
```

Pydantic >=2.12 emits `UnsupportedFieldAttributeWarning` when a `TypeAdapter` is built
from an `Annotated` type that includes field-specific metadata (alias, etc.) — those
attributes are no-ops at the TypeAdapter level. FastAPI suppresses this warning per-field.

**Lancelot state.** The `Plan` / `Validator` objects in Lancelot use `TypeAdapter` at
route-registration time. Check `lancelot/routing.py` for the equivalent warning
suppression. Missing it causes warning spam in Pydantic >=2.12, not a functional bug.

---

## Non-byte-path / refused items

- `params.py` `openapi_examples` / `include_in_schema` / `json_schema_extra` — OpenAPI
  schema metadata only, never touches the request/response bytes.
- `get_body_field` (`utils.py:1001`) — builds a synthetic `ModelField` for OpenAPI
  schema generation only; not used in validation.
- `get_flat_models_from_fields` / `get_definitions` / `get_schema_from_model_field` —
  JSON schema generation, not request processing.
- `generate_encoders_by_class_tuples` — precomputes `encoders_by_class_tuples` dict at
  import time to enable `isinstance` dispatch in `jsonable_encoder`. No hot-path
  relevance for Lancelot's `dump_json` path.
- `_compat/v2.py` `GenerateJsonSchema.bytes_schema` override — bytes→base64 JSON
  schema fix for Pydantic PRs not yet merged; schema generation only.

---

## Summary: actionable gaps

| # | Gap | Severity | File to fix |
|---|-----|----------|------------|
| 1 | `Decimal` encoder: `float(obj)` always vs FastAPI's `int` when `exponent>=0` | Correctness / financial round-trip | `lancelot/validation.py:254` |
| 2 | `UnsupportedFieldAttributeWarning` suppression in TypeAdapter construction | Warning spam in pydantic >=2.12 | `lancelot/routing.py` TypeAdapter build sites |
| 3 | `eval_str=True` in `inspect.signature` | Correctness for stringified annotations | `lancelot/routing.py` / `di.py` |
| 4 | `deepcopy(field.default)` on missing optional with mutable default | Cross-request state mutation | `lancelot/di.py` `_raw_for` / validate path |

Gap #1 is a one-liner fix. Gap #2 is cosmetic. Gaps #3 and #4 require verification against existing tests before touching.
