# FastAPI — Validation / Serialization / OpenAPI / SSE — Source-Level RE for Lancelot

**Target version:** FastAPI 0.136-class (Pydantic v2 ONLY — `pydantic.v1` is hard-removed; `PydanticV1NotSupportedError` is raised at field-build time, `utils.py:66-70`).
**Source root:** `/Users/seventyleven/Desktop/researchfms/fastapi/fastapi/fastapi/`
**Scope:** `_compat/`, `encoders.py`, `openapi/`, `sse.py`, `responses.py`, `exceptions.py`, plus the real hot path in `routing.py` + `dependencies/utils.py` that wires them together.
**Label key:** KNOWN = read from source this session with `path:line`. INFERRED = behavior deduced from code structure. DESIGNED = Lancelot recommendation.

---

## 0. Executive summary (10 lines)

1. **Request validation is NOT done by FastAPI** — every scalar/body param is wrapped in a per-field `pydantic.TypeAdapter` built once at startup (`_compat/v2.py:163-166`); the actual validate call is `TypeAdapter.validate_python(value, from_attributes=True)` (`v2.py:182`) executing in **pydantic-core Rust**. FastAPI's Python cost is only field extraction + loc-prefixing of errors.
2. **The real serialization hot path has two branches** (`routing.py:687-715`): a **fast path** `field.serialize_json()` → `TypeAdapter.dump_json()` → JSON bytes in **one Rust pass** (no intermediate dict, no `json.dumps`), used only when `response_class` is the default and a `response_field` exists; and a **slow path** `serialize()` → `dump_python(mode="json")` then the chosen Response class re-serializes.
3. **`jsonable_encoder` is the legacy slow path** (`encoders.py:129-365`): a fully recursive, isinstance-ladder, per-node Python tree-walk. It is now bypassed for the common typed-response case but is still hit for: untyped responses, OpenAPI generation itself, SSE/JSONL items without a `stream_item_field`, and `ServerSentEvent.data` that isn't a Pydantic model.
4. **`response_model` filtering = a second full validate+serialize round-trip on the way out** (`routing.py:291-314`): the endpoint return value is *re-validated* through the response `TypeAdapter` (`from_attributes=True` lets it read ORM objects), errors become `ResponseValidationError` (500), then dumped. This is double Rust work but Python-cheap.
5. **OpenAPI generation is a one-time-ish, O(routes × fields) Python algorithm** (`openapi/utils.py:514-606`) delegating JSON-Schema emission to a subclassed `pydantic.json_schema.GenerateJsonSchema` (`_compat/v2.py:57-70`); `$ref` dedup is achieved by collecting **flat models** into a name map and a single `schema_generator.generate_definitions(inputs=[...])` call (`v2.py:336`) — Pydantic owns ref-templating via `REF_TEMPLATE = "#/components/schemas/{model}"`.
6. **Separate input/output schemas**: a model used as both request and response emits two schemas (`-Input`/`-Output`) unless `separate_input_output_schemas=False`; controlled per-field by `mode` ("validation" vs "serialization") and `_has_computed_fields` (`v2.py:247-282`).
7. **SSE is a first-class native feature** (`sse.py` + `routing.py:496-619`), not a userland recipe: `EventSourceResponse` is a marker subclass; encoding happens in the routing layer with a **memory-stream producer/consumer split** so a 15 s keepalive timer (`_PING_INTERVAL=15.0`) can be injected without ever wrapping the generator's `__anext__` in a cancel scope (PEP-789 correctness).
8. **SSE framing** (`sse.py:146-214`): multi-line `data:` split on `splitlines()`, `event:`/`id:`/`retry:`/`:comment` fields, terminator is exactly `"\n".join(lines)` with two trailing empties → ends `\n\n`. `data` is **always JSON** (string `"hi"` → `data: "hi"`); `raw_data` bypasses JSON. `KEEPALIVE_COMMENT = b": ping\n\n"`.
9. **For LLM token streaming**: response-model validation is **structurally bypassed** for `ServerSentEvent` items (`routing.py:504-505`) — the user mixes types intentionally; only plain (non-SSE-wrapper) yields get validated through `stream_item_field`. Headers forced: `Cache-Control: no-cache`, `X-Accel-Buffering: no` (defeats nginx buffering — critical for token latency).
10. **Steal-for-Lancelot headline**: keep the per-field compiled-validator model (it's the right primitive) but move it to Rust; make the *one-pass typed `dump_json`* the **only** response path (delete `jsonable_encoder` from the hot path entirely); generate OpenAPI from the same Rust schema registry once at boot; implement SSE/JSONL framing in Rust with a lock-free producer ring buffer + timer-wheel keepalive instead of two anyio memory streams.

---

## 1. The `_compat` seam — `ModelField`, `TypeAdapter` per field, model rebuild machinery (KNOWN)

The `_compat` package is the entire Pydantic-version abstraction. Post-v1-removal it is **thin**: `__init__.py` (40 lines) is pure re-exports, `shared.py` (214) is annotation-introspection helpers copied from Pydantic internals, `v2.py` (493) is the actual machinery. There is no `v1.py` anymore; v1 is detected only to raise.

### 1.1 `ModelField` — the per-field compiled validator (`_compat/v2.py:113-244`)

This is the load-bearing primitive. Every path/query/header/cookie/body param **and** every response model becomes one `ModelField`:

```python
@dataclass
class ModelField:
    field_info: FieldInfo                                  # pydantic.fields.FieldInfo
    name: str
    mode: Literal["validation", "serialization"] = "validation"
    config: ConfigDict | None = None
```

**The expensive part is `__post_init__` (`v2.py:141-166`)** — runs once per field at app construction:

```python
field_dict = asdict(self.field_info)                       # {annotation, metadata, attributes}
annotated_args = (
    field_dict["annotation"],
    *field_dict["metadata"],
    Field(**field_dict["attributes"]),                     # rebuilt FieldInfo, metadata-stripped
)
self._type_adapter: TypeAdapter[Any] = TypeAdapter(
    Annotated[annotated_args],
    config=self.config,
)
```

- **Mechanics:** It tears `FieldInfo` apart into `(annotation, metadata, attributes)` via a local `asdict()` (`v2.py:100-110`, a forward-compat shim for Pydantic < 2.12.3 which lacks `FieldInfo.asdict()`), then **rebuilds a fresh `Field(**attributes)`** so the new `TypeAdapter` doesn't double-carry the old metadata, and constructs `TypeAdapter(Annotated[annotation, *metadata, Field(...)])`. Constructing a `TypeAdapter` triggers pydantic-core to **compile a Rust validator + serializer schema** for that annotation. This is the model-rebuild machinery: FastAPI does not use `BaseModel.model_validate` for params — it builds standalone adapters so a bare `int`, `list[Item]`, `Annotated[str, Query(min_length=3)]` all get the same Rust-backed treatment as a full model.
- `_Attrs` (`v2.py:74-96`) is the explicit allowlist of `FieldInfo` attributes copied across (`default`, `default_factory`, `alias`, `validation_alias`, `serialization_alias`, `title`, `description`, `examples`, `exclude`, `discriminator`, `deprecated`, `json_schema_extra`, `frozen`, `validate_default`, `repr`, `init`, `kw_only`, …). Anything not in this dict is dropped on rebuild — a subtle correctness surface.
- A `warnings.catch_warnings()` block (`v2.py:142-152`) suppresses Pydantic ≥2.12's `UnsupportedFieldAttributeWarning` (raised because building a `TypeAdapter` from a model-field annotation can carry field-only metadata like `alias` that is meaningless on an adapter). **INFERRED:** this warning suppression is itself a small per-field cost (context-manager entry) at startup, not per-request.
- `__hash__ = id(self)` (`v2.py:241-244`): every `ModelField` is identity-unique so it can key a `dict[ModelField, JsonSchemaValue]` during OpenAPI generation. **This means model-field instances are never deduped by value — dedup happens at the model level (§3).**

**Perf cost:**
- **Startup:** O(total fields across all routes) `TypeAdapter` constructions. Each is a Rust schema compile — non-trivial (μs–ms per complex model) but **amortized to zero per request**. For a 200-route app with ~5 params each + response models, ~1500 adapter compiles at import. This is FastAPI's "slow cold start" reputation.
- **Per request:** zero rebuild. The compiled adapter is reused.

**Steal-for-Lancelot:** The per-field-compiled-validator model is **correct and worth stealing**. The annotation→validator compile belongs at route-registration time, cached on the route. Lancelot's Rust core should compile a validator+serializer pair per param/response *once* and store a handle (pointer) on the route struct. **Avoid** FastAPI's tear-apart-and-rebuild dance (`asdict` → `Field(**attrs)` → `Annotated[...]`) — that exists only because `FieldInfo` is Pydantic's data model and TypeAdapter is a separate entry point. In Lancelot you own the type system; compile the annotation directly.

**vs Rust-core serializer:** This *is* already Rust-core (pydantic-core). The lesson: the right boundary is "Python declares the type, Rust compiles+executes the validator." Lancelot should keep exactly this boundary but eliminate the Python `FieldInfo`↔`TypeAdapter` impedance layer.

### 1.2 `validate()` — the request-validation entry point (`v2.py:173-188`)

```python
def validate(self, value, values={}, *, loc=()):
    try:
        return (self._type_adapter.validate_python(value, from_attributes=True), [])
    except ValidationError as exc:
        return None, _regenerate_error_with_loc(
            errors=exc.errors(include_url=False), loc_prefix=loc
        )
```

- **What FastAPI delegates vs does itself:** Delegated to Rust: the entire coercion/validation (`validate_python`). Done in Python: (a) the try/except, (b) `exc.errors(include_url=False)` — `include_url=False` suppresses the `https://errors.pydantic.dev/...` URL per error (saves string work + leaks less), (c) `_regenerate_error_with_loc` (`v2.py:486-493`) which **rebuilds every error dict to prepend a loc prefix**: `[{**err, "loc": loc_prefix + err.get("loc", ())} for err in errors]`. That dict-spread per error is pure Python but only on the *error* path (cold).
- `from_attributes=True` is critical and easy to miss: it lets the same adapter validate **ORM/arbitrary objects by attribute access**, which is how `response_model` can validate a SQLAlchemy row, and how header/path Pydantic models read from `Headers`/multidict-like objects.
- `values={}` param is a vestigial Pydantic-v1 cross-field-validation interface — **unused in v2** (passed but never consumed by `validate_python`). DESIGNED: Lancelot should not carry this dead parameter.

**Perf cost:** Happy path = 1 Rust call + 1 tuple alloc + (empty list literal — note `[]` is re-created each call; cheap). Error path = Python list-comp over N errors with dict-spread. Lancelot: the happy path is already optimal; the error path should build the error payload in Rust and hand back a ready-to-serialize structure (FastAPI re-walks it for loc-prefixing — avoidable if the validator is told the loc prefix up front).

### 1.3 `serialize()` / `serialize_json()` — the two output modes (`v2.py:190-239`)

```python
def serialize(self, value, *, mode="json", include=None, exclude=None,
              by_alias=True, exclude_unset=False, exclude_defaults=False,
              exclude_none=False):
    return self._type_adapter.dump_python(value, mode=mode, include=..., ...)

def serialize_json(self, value, *, include=None, ...):
    return self._type_adapter.dump_json(value, include=..., ...)
```

The source comment (`v2.py:228-230`) is the entire thesis for Lancelot:

> "This uses Pydantic's `dump_json()` which serializes directly to JSON bytes in one pass (via Rust), avoiding the intermediate Python dict step of `dump_python(mode="json") + json.dumps()`."

- `serialize()` → `dump_python(mode="json")` returns a **Python dict of JSON-safe primitives** (still needs a `json.dumps` afterward, done by the Response class).
- `serialize_json()` → `dump_json()` returns **`bytes`** — the JSON document, produced entirely inside pydantic-core's Rust serializer (serde-json-class speed). No Python dict ever materializes.
- Both accept the full Pydantic `include`/`exclude`/`by_alias`/`exclude_unset`/`exclude_defaults`/`exclude_none` filter set — these map to `response_model_*` on the route (`routing.py:357-362`).
- **Critical caller contract** (comment `v2.py:226-227`): the value passed to `serialize_json` **must already have been through `validate_python`**. FastAPI guarantees this in `serialize_response` (validate then serialize, §4.1). So the round-trip is: endpoint returns object → Rust validate (coerce to response model) → Rust dump_json → bytes. Two Rust passes, zero Python object graph walk.

**Steal-for-Lancelot (highest priority):** Make `serialize_json`-equivalent the **default and only** response path. The single-pass typed→bytes serializer is the entire performance story. Lancelot should never produce an intermediate Python dict for a typed response. **Avoid** ever calling a `jsonable_encoder`-style recursive Python walk for typed responses (FastAPI still falls back to it — §2 — and that fallback is the slow path).

### 1.4 Helper introspection in `shared.py` — the reflection tax (KNOWN)

`shared.py` is ~200 lines of pure `typing` reflection used at **route-registration time** to classify annotations (scalar vs sequence vs complex vs bytes vs UploadFile). The hot functions:

- `lenient_issubclass(cls, class_or_tuple)` (`shared.py:47-55`) — `isinstance(cls, type) and issubclass(cls, ...)` wrapped in try/except TypeError. Called *constantly* during classification.
- `field_annotation_is_sequence` / `_annotation_is_sequence` (`shared.py:58-77`) — recursive over `Union`/`UnionType` args via `get_origin`/`get_args`, checks membership in `sequence_types = (Sequence, list, tuple, set, frozenset, deque)` excluding `str`/`bytes`.
- `field_annotation_is_complex` (`shared.py:88-101`) — recursive; complex = `BaseModel | Mapping | UploadFile | sequence | dataclass | has __pydantic_core_schema__`. Recurses into `Annotated` and `Union`.
- `field_annotation_is_scalar` (`shared.py:104-106`) — `annotation is Ellipsis or not field_annotation_is_complex(...)`. The `Ellipsis` special-case is for `tuple[int, ...]`.
- `is_bytes_or_nonable_bytes_annotation`, `is_uploadfile_*_annotation`, `is_bytes_sequence_annotation`, `is_uploadfile_sequence_annotation` (`shared.py:126-175`) — each independently re-walks the Union args. **Redundant traversals**: the same `get_args(annotation)` Union expansion is done by 4–6 separate functions on the same annotation during one param classification.
- `annotation_is_pydantic_v1` (`shared.py:202-214`) — recursively checks for `pydantic.v1.BaseModel` to *raise an error*; an `import pydantic.v1` is attempted inside a `warnings.catch_warnings()` (`shared.py:181-187`, `190-199`). **INFERRED:** this import-guard runs per `create_model_field` call — a measurable startup cost across hundreds of fields (the import is cached by Python after first, but the `catch_warnings` context + `isinstance` still runs each time).

**Perf cost:** All of this is **registration-time** (cold), not per-request. But it's O(routes × params × annotation-tree-size) of pure-Python `typing` reflection, and the reflection is **duplicated** (multiple predicate functions re-expanding the same Union). This is a real chunk of FastAPI's import/startup time.

**Steal/avoid for Lancelot:** Keep the *idea* of classifying annotations once at registration. **Avoid** the 6-functions-each-re-walking-the-Union pattern — do a **single recursive descent** that emits a classification record (`{is_scalar, is_sequence, is_bytes, is_upload, inner_types}`) in one pass and cache it on the param. If Lancelot's core is Rust, do this classification in Rust during route registration by walking the type once and producing a tagged enum; never re-reflect at request time. The Python `typing` module's `get_origin`/`get_args` are notoriously slow — a single-pass classifier is a free win.

---

## 2. `jsonable_encoder` — the recursive Python tree-walk (KNOWN — `encoders.py:129-365`)

This is the function FastAPI used to call on *every* response. The 0.136-class code path now bypasses it for typed responses (§4), but it is **still on the hot path** for: untyped endpoints (return value with no `response_model` and no return annotation → `serialize_response` falls to `else: return jsonable_encoder(response_content)` at `routing.py:316-317`), the entire OpenAPI document (`openapi/utils.py:606`), Swagger param/example encoding (`openapi/utils.py:171-211`), and SSE/JSONL items lacking a `stream_item_field` (`routing.py:493`, `512`). Understanding its cost is essential because Lancelot's design decision is *whether to have such a function at all*.

### 2.1 The dispatch ladder (`encoders.py:231-364`) — order matters, it's a linear isinstance cascade

The function is one giant ordered `if`-cascade. Order is significant (first match wins). Reproduced as the algorithm:

1. **custom_encoder** (`231-238`): `custom_encoder or {}`; if non-empty, `type(obj) in custom_encoder` exact-type lookup first, else a **linear scan** `for encoder_type, encoder_instance in custom_encoder.items(): if isinstance(obj, encoder_type)`. O(custom encoders) per node.
2. **include/exclude normalization** (`239-242`): if not a set/dict, coerce to `set(...)`. Done **at every recursion level** (the params are threaded down recursively → repeated set construction, see 2.3).
3. **`isinstance(obj, BaseModel)`** (`243-258`): calls `obj.model_dump(mode="json", include=, exclude=, by_alias=, exclude_unset=, exclude_none=, exclude_defaults=)` — **this delegates the actual model walk to pydantic-core Rust** — then **recurses `jsonable_encoder` on the resulting dict** with `exclude_none`/`exclude_defaults`/`sqlalchemy_safe` (note: `include`/`exclude`/`by_alias` are NOT re-passed on the recursion, `253-258` — they were already applied by `model_dump`). So a Pydantic model = 1 Rust dump + 1 full Python re-walk of the dumped dict. **The re-walk is pure waste for the common case** (the dict is already JSON-safe after `mode="json"`); it exists to catch nested non-Pydantic objects.
4. **`dataclasses.is_dataclass(obj)`** (`259-272`): `dataclasses.asdict(obj)` (recursive deep copy!) then recurse with the **full** include/exclude set.
5. **`isinstance(obj, Enum)`** → `obj.value` (`273-274`).
6. **`isinstance(obj, PurePath)`** → `str(obj)` (`275-276`).
7. **`isinstance(obj, (str, int, float, type(None)))`** → return as-is (`277-278`). **This is the leaf fast path** but it's the *7th* isinstance check — every string in a payload pays for 6 failed isinstance checks (custom_encoder skip, set checks, BaseModel, dataclass, Enum, PurePath) before being returned.
8. **`isinstance(obj, PydanticUndefinedType)`** → `None` (`279-280`).
9. **`isinstance(obj, dict)`** (`281-315`): builds `encoded_dict` from scratch. `allowed_keys = set(obj.keys())` then `&= set(include)` / `-= set(exclude)`. For each k,v: skips `_sa`-prefixed string keys if `sqlalchemy_safe`, skips `None` values if `exclude_none`, then **recursively `jsonable_encoder`s BOTH the key and the value** (separate recursive calls — `298-313`). Every dict key (already a string 99% of the time) gets a full recursive encode.
10. **`isinstance(obj, (list, set, frozenset, GeneratorType, tuple, deque))`** (`316-332`): new list, recurse each item with full include/exclude threaded down. **Note `GeneratorType` is consumed here** — returning a generator from an untyped endpoint silently materializes it into a list.
11. **`type(obj) in ENCODERS_BY_TYPE`** (`334-335`): exact-type dict lookup into the 25-entry `ENCODERS_BY_TYPE` map (`encoders.py:84-112`) — `bytes`→`.decode()`, `datetime`→`.isoformat()`, `Decimal`→`decimal_encoder`, `UUID`→`str`, `Path`→`str`, `set`→`list`, `IPv4Address`→`str`, `Url`→`str`, etc.
12. **`for encoder, classes_tuple in encoders_by_class_tuples.items(): if isinstance(obj, classes_tuple)`** (`336-338`): the **subclass fallback** — a linear scan over the *inverted* map (`generate_encoders_by_class_tuples`, `115-126`, groups types by shared encoder into `{encoder: (types...)}`). O(distinct encoders) isinstance checks for any type that's a *subclass* of a known type but not an exact match.
13. **`is_pydantic_v1_model_instance(obj)`** → raise `PydanticV1NotSupportedError` (`339-343`).
14. **Last resort** (`344-364`): `dict(obj)` then fall back to `vars(obj)`; whichever works, recurse on it. If both raise, `raise ValueError([errors])`. This is how arbitrary objects (ORM rows without response_model) get serialized — by `vars()`-walking `__dict__`.

### 2.2 `ENCODERS_BY_TYPE` and the inverted tuple map (`encoders.py:84-126`)

- `ENCODERS_BY_TYPE: dict[type, Callable]` — 25 entries, exact-type keyed. `decimal_encoder` (`59-81`) is non-trivial: `dec_value.as_tuple().exponent`; if int and ≥0 → `int()` else `float()` (prevents `Decimal("1")` round-tripping as `1.0`).
- `encoders_by_class_tuples` (`126`) is `defaultdict(tuple)` inverted: `{lambda o:o.decode(): (bytes,), str: (Color, PyExtraColor, IPv4Address, ..., UUID, Url, AnyUrl), list: (frozenset, deque, GeneratorType, set), ...}`. The subclass scan (step 12) iterates this — typically ~8 distinct encoder callables, so ≤8 isinstance(tuple) checks for the subclass case.

### 2.3 Perf cost — quantified

**The cost is the recursion shape, not any single op.** For a response of N total nodes (every scalar, every dict key, every list element counts as a node):

- **Function-call overhead:** `jsonable_encoder` is called **once per node, including once per dict key** (step 9 encodes keys recursively). A dict of 10 string keys = 21 calls (1 dict + 10 keys + 10 values). Python function call ≈ 50–100 ns minimum; the body does 6–9 isinstance checks before the leaf return for a string → realistically **~0.5–2 μs per scalar node** in CPython.
- **Repeated set construction:** `include`/`exclude` are re-checked and potentially re-`set()`-ed at *every* recursion frame (`239-242`), and `set(obj.keys())`/`set(include)`/`set(exclude)` rebuilt per dict (`283-287`). For a deeply nested payload with an active `exclude`, this is O(nodes × |exclude|) set allocations.
- **Double work for Pydantic models:** `model_dump(mode="json")` (Rust, fast) **then** a full Python re-walk of the produced dict (step 3 recursion). For a 50-field model with nested models, the Rust dump is ~microseconds; the redundant Python re-walk is ~50–200 μs. **This is the single biggest waste** and the reason FastAPI added the `dump_json` fast path.
- **Worst case — list of models:** `list[Item]` of 1000 items, no fast path: 1000 × (`model_dump` Rust + full Python re-walk of each item's dict). Empirically this is where FastAPI's "slow JSON" reputation comes from; the `dump_json` path (§4) collapses it to one Rust call.

**Memory:** every level allocates a fresh `dict`/`list` (`encoded_dict = {}`, `encoded_list = []`) — the entire structure is **duplicated** in memory before `json.dumps` duplicates it again into a string. Peak memory ≈ 2–3× the logical payload size.

### 2.4 Steal / Avoid / Rust comparison

- **AVOID entirely for typed responses.** `jsonable_encoder` is a Python object-graph walker; it is the antithesis of Lancelot's performance goal. The whole point of a Rust core is that this function should not exist on the hot path.
- **Steal the type→encoder table concept**, not the recursion. The `ENCODERS_BY_TYPE` map (datetime→isoformat, UUID→str, Decimal→int|float, set→list, IP→str, Path→str) is the **canonical "Python types that aren't JSON-native" list** — Lancelot's Rust serializer must handle exactly these (orjson handles datetime/UUID/numpy natively; it does NOT handle Decimal-as-int, custom Color, or `Pattern.pattern` — Lancelot's serializer needs a small extension hook for these). The `decimal_encoder` int-vs-float rule is a real correctness detail worth replicating (financial round-tripping).
- **Rust-core comparison:** Pydantic v2's `dump_json` already does in Rust what `jsonable_encoder`+`json.dumps` does in Python, in one pass, ~10–50× faster on model-heavy payloads. orjson does it for plain dicts/lists ~5–10× faster than stdlib `json` and handles datetime/UUID/dataclass/numpy. **Lancelot's design:** the response serializer is a Rust function that takes (a) the compiled serializer handle for the declared type (typed path → like pydantic-core `dump_json`), OR (b) for untyped/dynamic returns, a Rust value-walker with a builtin type→encoder table (like orjson + the `ENCODERS_BY_TYPE` extensions). There should be **zero Python recursion** in either case. The only Python that should touch a response body is `None` (it's all in Rust).
- **The one thing Python must keep:** the `custom_encoder` user hook and arbitrary-object fallback (`dict(obj)`/`vars(obj)`). DESIGNED: expose this as an optional Rust→Python callback for unknown types only (rare, cold path), so 99.9% of nodes never cross the FFI boundary back into Python.

---

## 3. Request body / param validation — the real per-request pipeline (KNOWN)

This section traces what *actually executes* when a request body arrives, from
`routing.py:get_request_handler.app()` down to pydantic-core. This is the
request-side hot path.

### 3.1 Body read + content-type sniffing (`routing.py:401-449`)

Before any validation, the handler reads the body:

- `if body_field:` — only if the route declared a body. No body param ⇒ the
  request body is never read (zero cost).
- **Form path** (`is_body_form`, set at `routing.py:371` =
  `isinstance(body_field.field_info, params.Form)`): `await request.form()`,
  push `body.close` onto the file AsyncExitStack (`405-407`).
- **JSON path** (`409-426`): `body_bytes = await request.body()`. Then a
  **content-type sniff using `email.message.Message`** (not a hand-rolled
  parser):
  ```python
  message = email.message.Message()
  message["content-type"] = content_type_value
  if message.get_content_maintype() == "application":
      subtype = message.get_content_subtype()
      if subtype == "json" or subtype.endswith("+json"):
          json_body = await request.json()
  ```
  - If no content-type header at all AND `strict_content_type` is False
    (`414-415`): still attempt `request.json()`. If strict (the default —
    `Default(True)`, `routing.py:365`): a missing content-type means the body
    is passed through as **raw bytes** (`body = body_bytes`, `426`), which then
    fails validation downstream with a clear error.
  - `subtype.endswith("+json")` handles `application/vnd.api+json`,
    `application/ld+json`, etc. — a correctness detail Lancelot must replicate.
- **Error framing** (`427-441`): a `json.JSONDecodeError` becomes a
  `RequestValidationError` with a hand-built error dict
  `{"type":"json_invalid","loc":("body", e.pos),"msg":"JSON decode error",
  "input":{},"ctx":{"error": e.msg}}` — note `loc` carries the **byte offset**
  `e.pos`. Any other body-read exception → `HTTPException(400, "There was an
  error parsing the body")` (`445-449`). A middleware-raised `HTTPException`
  is re-raised untouched (`442-444`).

**Perf cost:** `await request.body()` buffers the entire body in memory
(Starlette). `request.json()` uses stdlib `json.loads` (Starlette default) —
**this is a Python JSON parse before pydantic-core ever sees it.** The parsed
Python object is then handed to `validate_python` which **re-walks it** to
coerce. So a JSON body is parsed *twice* in effect: once by `json.loads`
(bytes→Python objects), once by pydantic-core (Python objects→validated model).

**Steal/avoid for Lancelot:** This double-parse is the request-side equivalent
of the response-side double-walk. pydantic-core has `validate_json(bytes)` that
parses+validates in **one Rust pass** (jiter) — FastAPI does NOT use it here
(it calls `request.json()` → `json.loads` → then `validate_python`). **This is
a concrete FastAPI bottleneck Lancelot should beat:** for a typed body, go
straight `bytes → validate_json → model`, never materializing an intermediate
Python dict. The content-type sniff is worth stealing verbatim (the
`email.message.Message` trick is robust; the `+json` suffix rule is essential).

### 3.2 `solve_dependencies` → `request_body_to_args` (`dependencies/utils.py:951-998`)

`solve_dependencies` (not in scope files but the caller) eventually calls
`request_body_to_args(body_fields, received_body, embed_body_fields)`:

- `single_not_embedded_field` (`959`) = exactly one body param and not embedded.
  This is the common `def create(item: Item)` case. Then:
  ```python
  loc = ("body",)
  v_, errors_ = _validate_value_with_model_field(
      field=first_field, value=body_to_process, values=values, loc=loc)
  return {first_field.name: v_}, errors_
  ```
  One `ModelField.validate` call → one pydantic-core `validate_python`. Clean.
- **Embedded / multi-body** (`981-997`): loops each body field, pulls
  `body_to_process.get(get_validation_alias(field))`, validates each
  separately, accumulates errors. `loc = ("body", alias)`. If the received body
  is a list not a dict, `.get` raises `AttributeError` → a synthesized
  `get_missing_field_error(loc)` (`988-990`).
- **Form + Pydantic model** (`965-973`): if a single non-embedded BaseModel
  body arrives as `FormData`, the model is **exploded into its component
  fields** via `get_cached_model_fields` and `_extract_form_body` before
  validation (so `multipart/form-data` maps onto a Pydantic model field-by-
  field).

`_validate_value_with_model_field` (`utils.py:738-746`):
```python
if value is None:
    if field.field_info.is_required():
        return None, [get_missing_field_error(loc=loc)]
    else:
        return deepcopy(field.default), []          # NB: deepcopy of default
return field.validate(value, values, loc=loc)
```
- **`deepcopy(field.default)`** for an absent optional with a default
  (`utils.py:745`, also `780`) — a defensive copy so a mutable default
  (`[]`, `{}`) can't be shared across requests. `deepcopy` is **expensive**
  (it's the classic Python mutable-default footgun handled correctly but
  slowly). Lancelot: defaults should be cloned in Rust (cheap structural copy)
  or, better, default *factories* invoked per request.
- `get_missing_field_error(loc)` (`v2.py:379-384`) builds a real pydantic
  `ValidationError.from_exception_data("Field required",
  [{"type":"missing","loc":loc,"input":{}}])` then `.errors(include_url=False)
  [0]` and overwrites `input` to `None`. So even "field missing" goes through
  pydantic-core to get a *consistent* error shape. Cost: one pydantic-core
  error construction per missing field (cold path only).

### 3.3 Query/header/cookie params — `request_params_to_args` (`utils.py:784-...`)

- For a **single Pydantic-model param** (e.g. `q: Annotated[Filters, Query()]`),
  `get_cached_model_fields(first_field.field_info.annotation)` (`utils.py:801`)
  explodes the model into its fields, extracts each from the multidict, then
  validates the *whole assembled dict* against the model field once
  (`single_not_embedded_field`, `841-850`).
- `get_cached_model_fields` is `@lru_cache`-d (`_compat/v2.py:413-415`) — the
  model→[ModelField] decomposition is memoized. **Steal this:** model field
  decomposition is pure (model class is immutable) → cache it. Lancelot should
  compute this once at route registration, not lazily-lru per call.
- `_get_multidict_value` (`utils.py:753-781`) handles sequence params via
  `values.getlist(alias)` vs scalar `values.get(alias)`, the empty-string-Form
  → default rule, and the empty-sequence → default rule. Again `deepcopy
  (field.default)` for absent optionals (`780`).
- **Header underscore→hyphen**: if a header field's alias equals its name and
  `convert_underscores` is on, `alias = alias.replace("_","-")`
  (`utils.py:821-824`) so `user_agent` reads the `user-agent` header.

### 3.4 The 422 error shape — `RequestValidationError` (KNOWN — `exceptions.py:174-243`)

`ValidationException` (`exceptions.py:174-209`) is the base. It stores
`self._errors` (the list of pydantic-style error dicts) and an optional
`EndpointContext` TypedDict (`exceptions.py:10-15`):
`{function, path, file, line}` extracted from the endpoint via
`_extract_endpoint_context` (`routing.py:391`). `__str__` (`204-209`) renders
`"N validation error(s):\n  {err}\n..."` plus a Python-traceback-style
`File "...", line N, in func` block (`_format_endpoint_context`, `193-202`) —
a DX nicety pointing at the exact endpoint source location.

Subclasses:
- `RequestValidationError(errors, *, body=None, endpoint_ctx=None)`
  (`212-222`) — adds `.body` (the raw request body, echoed back in the 422
  payload by the default exception handler).
- `WebSocketRequestValidationError` (`224-232`).
- `ResponseValidationError(errors, *, body=None, endpoint_ctx=None)`
  (`234-243`) — raised when the **endpoint's own return value** fails the
  response model. This is a **500**, not 422 (a server bug, not client
  error). Same shape, different semantics.

The wire format of each error dict is the **pydantic-core error dict**:
`{"type", "loc", "msg", "input", "ctx", "url"?}`. FastAPI strips `url`
(`include_url=False`) and prepends `loc` (`_regenerate_error_with_loc`). The
OpenAPI schema for this is hardcoded (`openapi/utils.py:42-69`):
```python
validation_error_definition = {
  "title":"ValidationError","type":"object","properties":{
    "loc":{"type":"array","items":{"anyOf":[{"type":"string"},{"type":"integer"}]}},
    "msg":{"type":"string"},"type":{"type":"string"},
    "input":{"title":"Input"},"ctx":{"title":"Context","type":"object"}},
  "required":["loc","msg","type"]}
validation_error_response_definition = {
  "title":"HTTPValidationError","type":"object","properties":{
    "detail":{"type":"array","items":{"$ref":"#/components/schemas/ValidationError"}}}}
```
This is injected into the 422 response of any route that has params or a body
(`openapi/utils.py:454-474`) and only added to `components.schemas` once
(idempotent `if "ValidationError" not in definitions`).

**Steal for Lancelot:** Adopt the pydantic-core error dict shape verbatim
(`type/loc/msg/input/ctx`) — it's an ecosystem standard now (clients parse it).
The traceback-style `EndpointContext` in error messages is a cheap, high-value
DX steal. Build the error list in Rust with the loc prefix already applied
(FastAPI re-walks to prefix — avoidable).

---

## 4. Response serialization — THE bottleneck analysis (KNOWN — `routing.py:277-317, 673-718`)

This is the most important section for Lancelot. It documents exactly where
Python reflection dominates and where FastAPI already escaped to Rust.

### 4.1 `serialize_response` — validate-then-serialize round-trip (`routing.py:277-317`)

```python
async def serialize_response(*, field, response_content, include, exclude,
        by_alias=True, exclude_unset=False, exclude_defaults=False,
        exclude_none=False, is_coroutine=True, endpoint_ctx=None,
        dump_json=False):
    if field:
        if is_coroutine:
            value, errors = field.validate(response_content, {}, loc=("response",))
        else:
            value, errors = await run_in_threadpool(
                field.validate, response_content, {}, loc=("response",))
        if errors:
            raise ResponseValidationError(errors=errors, body=response_content,
                                          endpoint_ctx=ctx)
        serializer = field.serialize_json if dump_json else field.serialize
        return serializer(value, include=include, exclude=exclude,
            by_alias=by_alias, exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults, exclude_none=exclude_none)
    else:
        return jsonable_encoder(response_content)
```

**The algorithm when a `response_model`/return-annotation exists (`field`):**

1. **Re-validate the endpoint's return value** through the response field's
   `TypeAdapter.validate_python(value, from_attributes=True)`. This is a
   **full second validation pass** — the endpoint already produced the data,
   but FastAPI runs it back through pydantic-core to (a) coerce ORM objects
   into the declared response model (`from_attributes=True` reads attributes),
   (b) enforce that the endpoint actually returns what it claims, (c) apply
   field filtering. Cost: one full pydantic-core validate over the entire
   response object graph. For an ORM list of 1000 rows → 1000 attribute-walks
   in Rust (fast, but real).
2. If validation fails → `ResponseValidationError` → **500** (the
   `body=response_content` is attached but the default handler does NOT echo
   it to the client for security; it's logged).
3. **Serialize** via either `serialize_json` (→ `dump_json`, bytes, one Rust
   pass) or `serialize` (→ `dump_python(mode="json")`, a Python dict).
4. **No field ⇒ `jsonable_encoder(response_content)`** — the full slow Python
   walk of §2, then the JSONResponse re-runs `json.dumps`.

- **Sync endpoints:** the `field.validate` runs in a **threadpool**
  (`run_in_threadpool`, `295-297`) — because a sync endpoint already occupies a
  worker thread and FastAPI keeps response validation off the event loop for
  it. Async endpoints validate inline (Rust releases the GIL during
  `validate_python`, so it doesn't block other tasks meaningfully).

### 4.2 The fast-path gate (`routing.py:673-715`) — exactly when Rust one-pass kicks in

```python
raw_response = await run_endpoint_function(dependant=, values=, is_coroutine=)
if isinstance(raw_response, Response):
    ...                                  # user returned a Response → passthrough
else:
    response_args = _build_response_args(...)
    use_dump_json = response_field is not None and isinstance(
        response_class, DefaultPlaceholder)
    content = await serialize_response(
        field=response_field, response_content=raw_response, ...,
        dump_json=use_dump_json)
    if use_dump_json:
        response = Response(content=content, media_type="application/json",
                            **response_args)
    else:
        response = actual_response_class(content, **response_args)
```

**The fast path (`dump_json` → JSON bytes in one Rust pass, NO
`jsonable_encoder`, NO `json.dumps`) is taken iff BOTH:**
1. `response_field is not None` — there is a `response_model` or a return type
   annotation that resolved to a model/type (`routing.py:904-913`), AND
2. `isinstance(response_class, DefaultPlaceholder)` — the user did **NOT**
   override `response_class` (still the default `JSONResponse`).

If the user sets `response_class=ORJSONResponse` (now deprecated — see §6) or
any custom class, `use_dump_json=False` → the slow `serialize()` →
`dump_python(mode="json")` → `actual_response_class.render()` →
`json.dumps`/`orjson.dumps` path. **The deprecation of ORJSONResponse is
deliberate:** the built-in `dump_json` (Rust) now beats orjson-on-a-Python-
dict because it skips the dict materialization entirely.

`_build_response_args` (`routing.py:333-348`): resolves status code precedence
(explicit `status_code` > response-object status > class default; redirect
defaults 307).

### 4.3 Where Python reflection dominates — the quantified verdict

| Path | When | Rust work | Python work | Verdict |
|---|---|---|---|---|
| **Fast: `dump_json`** | typed return + default response_class | validate_python (coerce/check) + **dump_json (parse-free, one pass to bytes)** | gate booleans, `Response(content=bytes)` ctor | **Optimal.** ~1–2 Rust passes, near-zero Python. This is the target. |
| **Slow-typed: `serialize`** | typed return + custom response_class | validate_python + dump_python(mode=json) | response_class.render → `json.dumps`/`orjson.dumps` over the Python dict | Python dict materialized then re-serialized. 2–5× slower than fast path on model-heavy data. |
| **Untyped: `jsonable_encoder`** | no return annotation / no response_model | model_dump per nested model only | **full recursive tree-walk (§2)** + `json.dumps` | Slowest. Pure-Python O(nodes) recursion + double memory. Avoid in Lancelot. |
| **Raw Response** | endpoint returns `Response(...)` | none | none (bytes passthrough) | Fast but no validation/schema. |

**Concrete numbers (INFERRED from algorithm shape; calibrate empirically):**
- Fast path on `list[Item]` (1000 items, 8 fields): ~1 Rust validate + 1 Rust
  dump_json. Dominated by Rust; Python overhead is O(1) (a handful of
  attribute accesses + one `Response` construction).
- `jsonable_encoder` path, same payload: ~1000 `model_dump` calls + ~1000
  full Python re-walks of the dumped dicts + 1 `json.dumps` over a 1000-elem
  list-of-dicts. Python frame overhead alone ≈ tens of thousands of function
  calls. **This is the 5–20× gap** between FastAPI-typed and FastAPI-untyped,
  and the entire reason the project added `dump_json`.
- **The double-validate tax:** even on the fast path, the response is
  validated *again* (it was constructed by the endpoint, validated on the way
  out). For trusted internal data this is pure overhead. FastAPI accepts it
  for the ORM-coercion + contract-enforcement value.

### 4.4 Steal / Avoid / Rust-core design for Lancelot

**STEAL (highest-value steal in this whole document):**
- The **`dump_json` one-pass model**: declared type → compiled Rust serializer
  → JSON **bytes** directly into the HTTP response body. No intermediate
  Python object, no second JSON encoder. Lancelot's *only* typed-response path
  should be this.
- The **`from_attributes`-style response coercion**: letting the response
  serializer read arbitrary objects (ORM rows, dataclasses) by attribute
  against the declared schema is a huge ergonomics win — keep it, do it in
  Rust (a field-by-field attribute getter compiled per response type).
- The **filter set** (`include/exclude/by_alias/exclude_unset/
  exclude_defaults/exclude_none`) belongs *inside* the Rust serializer (it's
  free there) — never post-process a Python dict to apply it.

**AVOID:**
- Two response paths. FastAPI has fast/slow/untyped because of backward compat
  with custom Response classes and `jsonable_encoder`'s history. Lancelot
  should have **one** typed path (Rust dump) + one explicit escape hatch
  (`return RawResponse(bytes)`) and **delete the recursive encoder**.
- The unconditional **double validation**. DESIGNED: make response re-
  validation *opt-out* (e.g. `response_model_validate=False` per route) for
  hot internal endpoints where the type is already guaranteed — keep it on by
  default for safety, but the bytes-dump can run without the validate pass
  when the endpoint's return type is statically known to match.
- `run_in_threadpool` for validation. If validation is Rust and releases the
  GIL (or Lancelot is not GIL-bound), there's no reason to bounce to a
  threadpool — that's a Python-GIL workaround.

**Rust-core comparison / Lancelot serializer design:**
- Pydantic v2 `dump_json` ≈ serde-derived serializer specialized per type at
  schema-compile time. orjson ≈ a general fast value-walker (no schema).
  Lancelot should ship **both modes**: (1) **schema-specialized** serializer
  compiled per response type at route registration (the
  `dump_json` equivalent — fastest, used for every typed route), and
  (2) a **schemaless fast value-walker** in Rust (the orjson equivalent +
  the `ENCODERS_BY_TYPE` extensions: Decimal-int-vs-float, IP→str, Path→str,
  custom Color) for `dict`/`Any` returns. Both emit bytes. Zero Python
  recursion. The Python layer's only job: hold the compiled-serializer handle
  on the route and call it with a pointer to the return value.
- **Eliminate the double JSON parse on the request side too** (§3.1): use a
  `validate_json(bytes)` (jiter-class) so request bodies are parsed+validated
  in one Rust pass. FastAPI does NOT do this (it `json.loads` then
  `validate_python`). This is a free win Lancelot should take on day one.

---

## 5. OpenAPI 3.1 generation algorithm + JSON-Schema $ref dedup (KNOWN — `openapi/utils.py`, `_compat/v2.py:254-346`)

### 5.1 Top-level algorithm `get_openapi()` (`openapi/utils.py:514-606`)

The whole document is built once (FastAPI caches it on `app.openapi_schema`
after first call — that caching is in `applications.py`, not in scope, but the
generator itself is stateless and re-runnable). Algorithm:

1. Build `info` dict from title/version/summary/etc (`531-542`).
2. `all_fields = get_fields_from_routes(routes + webhooks)` (`549`) — collects
   **every** `ModelField` that contributes to the schema: per route, the
   `body_field`, `response_field`, all `response_fields` (additional status
   codes), `stream_item_field`, recursively `callbacks`, and all flat params
   (`get_flat_params(route.dependant)`) (`openapi/utils.py:481-511`).
3. `flat_models = get_flat_models_from_fields(all_fields, known_models=set())`
   (`550`) — transitively walks every field's annotation to collect the set of
   all referenced `BaseModel`/`Enum` classes (the `$ref` targets).
4. `model_name_map = get_model_name_map(flat_models)` (`551`) — assigns each
   model a unique JSON-Schema name.
5. `field_mapping, definitions = get_definitions(fields=all_fields,
   model_name_map=, separate_input_output_schemas=)` (`552-556`) — **the one
   call that produces all `$defs`** (see 5.3).
6. For each route: `get_openapi_path(...)` (`557-575`) → builds the
   `paths[path][method]` operation object, merges returned
   `security_schemes` and `path_definitions` (the hardcoded
   ValidationError defs) into `components`.
7. Same loop for `webhooks` → `output["webhooks"]` (`576-594`).
8. `components["schemas"] = {k: definitions[k] for k in sorted(definitions)}`
   (`595-596`) — **schemas are emitted in sorted key order** (stable, diff-
   friendly OpenAPI output — a deliberate, steal-worthy detail).
9. **Final line** (`606`):
   `return jsonable_encoder(OpenAPI(**output), by_alias=True,
   exclude_none=True)` — the assembled dict is fed through the `OpenAPI`
   Pydantic model (`openapi/models.py`) for **validation of FastAPI's own
   output**, then `jsonable_encoder`'d back to a plain dict. So the OpenAPI
   doc itself pays the full `jsonable_encoder` cost — acceptable because it's
   generated once and cached.

`OpenAPI`/`Info`/`Contact`/etc models (`openapi/models.py:57-...`) all subclass
`BaseModelWithConfig` = `model_config = {"extra":"allow"}` (`models.py:57-58`)
so `openapi_extra` / vendor `x-` extensions pass through. `Contact.email` uses
`EmailStr` with a graceful fallback class if `email-validator` is missing
(`models.py:21-54`) that still emits `{"type":"string","format":"email"}` in
the schema via `__get_pydantic_json_schema__`.

### 5.2 Per-operation build `get_openapi_path()` (`openapi/utils.py:260-478`)

For each HTTP method on a route:
- `get_openapi_operation_metadata` (`236-257`) → `tags`, `summary`
  (route.summary or `route.name.replace("_"," ").title()`), `description`,
  `operationId` (= `route.operation_id or route.unique_id`). **Duplicate
  operationId detection**: warns (with file name) if the id was already used
  (`246-253`) — DX steal.
- `_get_openapi_operation_parameters` (`107-177`): path/query/header/cookie
  params → OpenAPI `parameters[]`. Schema per param via
  `get_schema_from_model_field` (5.4). Header name underscore→hyphen applied
  here too (`153-158`). Examples encoded via `jsonable_encoder`.
- Parameter de-dup (`301-313`): parameters keyed by `(in, name)`; a required
  definition overrides a non-required one for the same `(in,name)`.
- `get_openapi_operation_request_body` (`180-212`): only for
  `METHODS_WITH_BODY = {"GET","HEAD","POST","PUT","DELETE","PATCH"}`
  (`constants.py:1`). Uses `field_info.media_type` as the content-type key.
- **Streaming responses get bespoke OpenAPI** (`356-392`):
  - `route.is_json_stream` → content `application/jsonl` with
    `{"itemSchema": <schema of stream_item_field, or {}>}`.
  - `route.is_sse_stream` → content `text/event-stream` with
    `{"itemSchema": deepcopy(_SSE_EVENT_SCHEMA)}`; if a `stream_item_field`
    exists, the schema's `data` property becomes
    `{"type":"string","contentMediaType":"application/json",
    "contentSchema": <item schema>}` and `required=["data"]` (`383-388`).
    **This is OpenAPI 3.2 SSE modeling** (the comment cites "Section 4.14.4
    Special Considerations for Server-Sent Events", `sse.py:7-8`). Steal:
    correct, spec-aligned SSE schema generation is rare and valuable.
  - else JSON: `response_schema = {}` or the `response_field` schema; nested
    under `responses[code].content[media_type].schema` (`393-409`).
- `responses` for additional status codes (`410-453`): deep-merged via
  `deep_dict_update` (`utils.py:103-...`, recursive dict/list merge). Status
  text from `status_code_ranges` (`1XX`..`5XX`,`DEFAULT`) or
  `http.client.responses`.
- 422 injection (`454-474`): if the route has params or a body and no
  4XX/422/default already declared, inject the
  `HTTPValidationError` `$ref` response and (idempotently) add the two
  hardcoded ValidationError defs.
- `route.openapi_extra` deep-merged last (`475-476`) — user override wins.

### 5.3 `get_definitions()` — the single-pass $defs builder + dedup (`_compat/v2.py:285-346`)

This is the heart of `$ref` dedup. Reproduced as the algorithm:

```python
schema_generator = GenerateJsonSchema(ref_template=REF_TEMPLATE)   # subclass, v2.py:57
validation_fields    = [f for f in fields if f.mode == "validation"]
serialization_fields = [f for f in fields if f.mode == "serialization"]
flat_validation_models    = get_flat_models_from_fields(validation_fields, set())
flat_serialization_models = get_flat_models_from_fields(serialization_fields, set())
# wrap each transitively-referenced model as its own ModelField:
flat_*_model_fields = [ModelField(FieldInfo(annotation=m), name=m.__name__,
                                  mode=...) for m in flat_*_models]
flat_model_fields = flat_validation_model_fields + flat_serialization_model_fields
input_types = {f.field_info.annotation for f in fields}
unique_flat_model_fields = {f for f in flat_model_fields
                            if f.field_info.annotation not in input_types}
inputs = [(field,
           field.mode if (separate_input_output_schemas or _has_computed_fields(field))
                       else "validation",
           field._type_adapter.core_schema)
          for field in list(fields) + list(unique_flat_model_fields)]
field_mapping, definitions = schema_generator.generate_definitions(inputs=inputs)
# strip form-feed-truncated descriptions:
for item_def in definitions.values():
    if "description" in item_def:
        item_def["description"] = item_def["description"].split("\f")[0]
return field_mapping, definitions
```

**Dedup mechanics:**
- `get_flat_models_from_fields` / `_from_field` / `_from_annotation` /
  `_from_model` (`v2.py:437-483`) do a **transitive closure** over annotations
  collecting every `BaseModel`/`Enum` into a `set` (`known_models`) — set
  membership is the dedup. A model referenced 50 times is collected once.
- `unique_flat_model_fields` (`v2.py:321-323`): excludes flat models whose
  annotation is already a top-level input field (don't emit a model twice).
  The set comprehension over `ModelField` works because `__hash__=id(self)` —
  but the dedup key here is `f.field_info.annotation` (the class), not the
  field identity. So **dedup is by model class, definitions by class**.
- `generate_definitions(inputs=[...])` is **Pydantic's own** batch JSON-Schema
  generator — passing ALL core schemas in one call lets pydantic-core emit a
  single `$defs` table with `$ref`s deduped across the entire API. FastAPI
  does **not** hand-merge schemas; it delegates the whole `$ref` graph to
  pydantic. The `ref_template=REF_TEMPLATE="#/components/schemas/{model}"`
  (`constants.py:3`) tells Pydantic to emit refs pointing at OpenAPI's
  `components/schemas` location instead of the JSON-Schema-default `$defs`.
- **Separate input/output schemas**: `field.mode` ("validation" for request,
  "serialization" for response, set at field creation `routing.py:912`,`921`,
  `940`). If `separate_input_output_schemas=True` (default) OR the field has
  computed fields (`_has_computed_fields`, `v2.py:247-251`, peeks
  `core_schema["schema"]["computed_fields"]`), the validation and
  serialization variants are emitted **separately** (e.g. `Item-Input` vs
  `Item-Output`) because a model with defaults / computed fields /
  `exclude`-on-dump has a different shape on input vs output. Setting it
  `False` collapses to one schema (`override_mode="validation"`,
  `v2.py:263-267`).
- `GenerateJsonSchema` subclass (`v2.py:57-70`) only overrides `bytes_schema`
  to emit `{"type":"string","contentMediaType":"application/octet-stream",
  "contentEncoding":"base64"?}` (a forward-port of an unmerged Pydantic PR) —
  everything else is stock Pydantic JSON-Schema generation.
- `\f` (form-feed) truncation of descriptions (`v2.py:337-340`, also
  `routing.py:927-929`): docstring text after a form-feed char is dropped from
  the schema (lets you keep internal docs out of the public OpenAPI). Niche
  but a real steal-worthy convention.

### 5.4 `get_schema_from_model_field()` — per-field schema lookup (`v2.py:254-282`)

```python
override_mode = None if (separate_input_output_schemas or _has_computed_fields(field)) else "validation"
field_alias = (field.validation_alias or field.alias) if field.mode=="validation" \
              else (field.serialization_alias or field.alias)
json_schema = field_mapping[(field, override_mode or field.mode)]
if "$ref" not in json_schema:
    json_schema["title"] = field.field_info.title or field_alias.title().replace("_"," ")
return json_schema
```
- The `field_mapping` is the `dict[(ModelField, mode), JsonSchemaValue]`
  returned by `generate_definitions`. Lookup is O(1) by `(field, mode)` (field
  hashing by id).
- If the resolved schema is **not** a `$ref` (i.e. it's an inline scalar
  schema), FastAPI injects a human `title` (`field.title` or the alias
  Title-Cased with underscores→spaces). `$ref`-bearing schemas keep their ref
  (the title lives on the referenced definition).

### 5.5 Perf cost + steal/avoid

- **Cost:** all of this is **boot-time / first-`/openapi.json`-time**, then
  cached. O(routes × fields) Python with one big pydantic-core
  `generate_definitions` call doing the heavy schema graph. For a 300-route
  app this is tens-to-hundreds of ms once. Not a per-request concern — unless
  `/openapi.json` is uncached (it isn't by default).
- **Steal:** (1) the single batch `generate_definitions(inputs=[all core
  schemas])` → one deduped `$defs` graph — Lancelot should likewise generate
  the entire schema registry in **one Rust pass** over all registered route
  types, not per-route. (2) `ref_template` to point refs at
  `#/components/schemas/{model}`. (3) sorted-key schema emission for stable
  diffs. (4) separate input/output schema modes (genuinely correct — a model
  with a default-valued field is `required` on output but optional on input).
  (5) `\f` description truncation + duplicate-operationId warning (DX).
- **Avoid:** running the final assembled doc through a Pydantic model +
  `jsonable_encoder` (`utils.py:606`) — fine for a cached one-time doc, but if
  Lancelot generates OpenAPI lazily it should emit bytes directly from the
  Rust schema registry (it already has every type compiled — the JSON-Schema
  is a byproduct of the same schema compile, exactly as pydantic-core does it).
- **Rust-core angle:** pydantic-core *already* produces JSON Schema from the
  same `core_schema` it compiled the validator from. Lancelot's win: the
  validator, the serializer, AND the JSON-Schema all derive from **one**
  compiled type representation built at route registration. FastAPI gets this
  for free from Pydantic; Lancelot must build the equivalent unified Rust type
  registry so OpenAPI is `O(1)` derivable from already-compiled types.

---

## 6. SSE + streaming JSON for LLM token streams — EXHAUSTIVE (KNOWN — `sse.py`, `routing.py:466-672`)

This is the CRITICAL section for Lancelot's AI workloads. FastAPI 0.136-class
made SSE and JSONL **native, first-class, framework-level** features (not a
userland `StreamingResponse` recipe). Every detail below matters for token
streaming latency and correctness.

### 6.1 The marker class `EventSourceResponse` (`sse.py:20-33`)

```python
class EventSourceResponse(StreamingResponse):
    media_type = "text/event-stream"
```
- It is **only a marker** — "The actual encoding logic lives in the FastAPI
  routing layer. This class serves mainly as a marker and sets the correct
  Content-Type." (`sse.py:29-31`).
- Detection: `is_sse_stream = is_generator and lenient_issubclass(
  response_class, EventSourceResponse)` (`routing.py:970-972`), and in the
  handler `is_sse_stream = lenient_issubclass(actual_response_class,
  EventSourceResponse)` (`routing.py:376`). So SSE engages **iff** the endpoint
  is a generator (`def`/`async def` with `yield`) AND
  `response_class=EventSourceResponse`.
- Works with **any HTTP method** (docstring `sse.py:26-27`): "compatible with
  protocols like MCP that stream SSE over POST." This is essential for
  AI/agent protocols (MCP, OpenAI-style streaming over POST). Lancelot **must**
  support SSE-over-POST — many AI clients do this.

### 6.2 `ServerSentEvent` model (`sse.py:42-143`) — the per-event contract

A Pydantic model the endpoint may `yield`:
- `data: Any = None` — "always serialized to JSON: strings are quoted
  (`"hello"` becomes `data: "hello"`)" (`sse.py:59-72`).
- `raw_data: str | None = None` — sent **without** JSON encoding, placed in
  `data:` as-is (pre-formatted text/HTML/CSV). (`sse.py:73-86`).
- `event: str | None` — maps to browser `addEventListener(event,…)`; omitted →
  generic `message` event (`sse.py:87-97`).
- `id: str | None` with `AfterValidator(_check_id_no_null)` — **must not
  contain `\0`** (`sse.py:36-39`, `98-109`); browser echoes it as
  `Last-Event-ID` on reconnect.
- `retry: int | None` with `Field(ge=0)` — reconnection delay in **ms**
  (`sse.py:110-121`).
- `comment: str | None` — `:`-prefixed lines, ignored by clients, used for
  keep-alive (`sse.py:122-133`).
- `@model_validator(mode="after") _check_data_exclusive` (`sse.py:135-143`):
  raises if **both** `data` and `raw_data` set. Mutually exclusive.

### 6.3 `format_sse_event()` — exact wire framing (`sse.py:146-214`)

THE framing algorithm. Reproduce **byte-exact** for Lancelot:

```python
def format_sse_event(*, data_str=None, event=None, id=None, retry=None,
                      comment=None) -> bytes:
    lines: list[str] = []
    if comment is not None:
        for line in comment.splitlines():
            lines.append(f": {line}")
    if event is not None:
        lines.append(f"event: {event}")
    if data_str is not None:
        for line in data_str.splitlines():
            lines.append(f"data: {line}")
    if id is not None:
        lines.append(f"id: {id}")
    if retry is not None:
        lines.append(f"retry: {retry}")
    lines.append("")
    lines.append("")
    return "\n".join(lines).encode("utf-8")
```

Byte-exact rules a Lancelot Rust encoder must match:
- **Field order is fixed:** comment(s) → `event:` → `data:` line(s) → `id:` →
  `retry:`. (SSE spec is order-tolerant but matching exactly aids
  client/test parity.)
- **Multi-line `data:`**: `data_str.splitlines()` → one `data: ` line per
  source line. A JSON payload with embedded newlines (rare) or a multi-line
  text chunk emits multiple `data:` lines (the browser concatenates them with
  `\n`). Comments likewise split per line.
- **Separator is `\n`** (LF), NOT `\r\n`. Spec allows CR/LF/CRLF; FastAPI
  emits LF only.
- **Terminator**: two empty strings appended → `"\n".join([...,"",""])`
  guarantees the event ends with **exactly `\n\n`** (the SSE event boundary).
  E.g. one data line "x" → `"data: x" + "\n" + "" + "\n" + ""` =
  `b"data: x\n\n"`.
- **Encoding**: UTF-8. Field labels have a single trailing space
  (`f"data: {line}"`, `f": {line}"` — note the space after `:` for comments
  too).
- **Empty event** (all None except e.g. comment): still valid; a bare
  keep-alive comment.

`KEEPALIVE_COMMENT = b": ping\n\n"` (`sse.py:218`) — a pre-encoded constant
keep-alive event (a comment line `:ping` + terminator). `_PING_INTERVAL =
15.0` seconds (`sse.py:221-222`) — "Private but importable so tests can
monkeypatch it."

### 6.4 The routing-layer SSE engine (`routing.py:496-619`) — the hard part

This is the most subtle code in the file and the part most worth getting right
for AI token streaming. Walk-through:

**(a) Per-item serializer `_serialize_sse_item` (`routing.py:500-527`):**
```python
def _serialize_sse_item(item) -> bytes:
    if isinstance(item, ServerSentEvent):
        if item.raw_data is not None:
            data_str = item.raw_data                  # NO json encoding
        elif item.data is not None:
            if hasattr(item.data, "model_dump_json"):
                data_str = item.data.model_dump_json()  # Pydantic → Rust JSON, 1 pass
            else:
                data_str = json.dumps(jsonable_encoder(item.data))  # slow path
        else:
            data_str = None
        return format_sse_event(data_str=data_str, event=item.event,
                                id=item.id, retry=item.retry,
                                comment=item.comment)
    else:
        # plain object: validate+serialize via stream_item_field, wrap in data
        return format_sse_event(data_str=_serialize_data(item).decode("utf-8"))
```
- **CRITICAL for AI:** when the user yields a `ServerSentEvent`, **response-
  model validation is skipped** ("the user may mix types intentionally",
  `routing.py:504-505`). This is exactly right for LLM streams where you
  interleave token deltas, tool-call events, `[DONE]` sentinels, errors — all
  different shapes on the same stream. Lancelot must allow heterogeneous
  event types on one SSE stream without forcing a single schema.
- Pydantic `data` → `model_dump_json()` (one Rust pass to a JSON **str**).
  Non-Pydantic `data` → `json.dumps(jsonable_encoder(item.data))` (the SLOW
  §2 path). **For token streaming where each delta is a small dict/str, this
  per-token `jsonable_encoder`+`json.dumps` is the per-token Python tax.**
  DESIGNED for Lancelot: a Rust SSE encoder that takes the raw value and emits
  framed bytes directly; for the overwhelmingly common "token = string" case,
  `data: <json-string>\n\n` should be a single Rust call, no Python.
- Plain (non-SSE-wrapper) yields → `_serialize_data(item)`
  (`routing.py:471-494`): if a `stream_item_field` exists, validate +
  `serialize_json` (Rust one-pass, with full `response_model_*` filters);
  else `json.dumps(jsonable_encoder(data))`. So a typed SSE stream
  (`AsyncIterator[TokenChunk]`) gets Rust serialization per item; an untyped
  one gets the slow path per item.

**(b) The producer/consumer/keepalive split (`routing.py:529-609`) — PEP-789:**

This is the cleverest and most important design decision. Naively you'd wrap
`anext(gen)` in `anyio.fail_after(15)` to inject keepalives on idle. **That is
a bug**: a timeout cancellation thrown into a generator's `__anext__` finalizes
the generator (GeneratorExit), killing the LLM stream. FastAPI's solution
(`_sse_producer_cm`, `routing.py:534-584`):

1. `send_stream, receive_stream = anyio.create_memory_object_stream[bytes]
   (max_buffer_size=1)` — a bounded (size-1) channel.
2. **`_producer`** task (`556-559`): `async for raw_item in sse_aiter:
   await send_stream.send(_serialize_sse_item(raw_item))`. This task owns the
   generator; its `__anext__` is **never** wrapped in a cancel scope. Sync
   generators run via `iterate_in_threadpool(gen)` (`531-532`); async via
   `gen.__aiter__()` (`529-530`).
3. **`_keepalive_inserter`** task (`565-578`): loops
   `with anyio.fail_after(_PING_INTERVAL): data = await
   receive_stream.receive()` → forwards data; on `TimeoutError` sends
   `KEEPALIVE_COMMENT` instead. **The 15 s timeout wraps the channel
   `receive()`, not the generator** — so an idle LLM (thinking, slow first
   token) gets `: ping\n\n` every 15 s without ever touching the generator.
4. Both tasks run in an `anyio.create_task_group()` (`580-584`); the CM
   `yield receive_keepalive`s the keepalive-merged stream; on exit
   `tg.cancel_scope.cancel()`.
5. **PEP-789 correctness** (`routing.py:586-595`): the CM is entered on the
   **request-scoped `AsyncExitStack`** (`async_exit_stack.enter_async_context
   (_sse_producer_cm())`), so its `__aexit__` (which cancels the task group)
   runs via structured exit-stack teardown **after** the streaming response
   completes — NOT via `GeneratorExit` thrown into an async generator. A
   `push_async_callback(sse_receive_stream.aclose)` prevents a
   `ResourceWarning`.
6. **`_sse_with_checkpoints`** (`597-609`): `async for data in stream: yield
   data; await anyio.sleep(0)`. The `sleep(0)` is a **mandatory cancellation
   checkpoint** — if the producer outruns the consumer, `receive()` never
   suspends, so without the explicit `sleep(0)` client-disconnect cancellation
   could never be delivered. (Same `anyio.sleep(0)` checkpoint appears in the
   JSONL async path, `routing.py:632-634`, citing fastapi issue #14680.)

**(c) Response + headers (`routing.py:611-619`):**
```python
response = StreamingResponse(sse_stream_content, media_type="text/event-stream",
                             background=solved_result.background_tasks)
response.headers["Cache-Control"] = "no-cache"
response.headers["X-Accel-Buffering"] = "no"       # nginx: do NOT buffer SSE
response.headers.raw.extend(solved_result.response.headers.raw)
```
- **`X-Accel-Buffering: no`** is *the* critical header for LLM token latency:
  without it, nginx (and many reverse proxies) buffer the response and the
  client sees tokens in bursts or only at end-of-stream. Lancelot MUST emit
  this on every SSE/streaming response by default.
- `Cache-Control: no-cache` prevents intermediary caching of the event stream.
- User-set headers from `solved_result.response` are appended (so endpoints
  can add `X-Request-ID` etc.).

### 6.5 JSONL streaming (`routing.py:620-652`) — the typed-stream sibling

`is_json_stream = is_generator and isinstance(response_class,
DefaultPlaceholder)` (`routing.py:973-975`) — a generator endpoint with the
**default** response class (no `EventSourceResponse`) auto-streams **JSON
Lines** (`media_type="application/jsonl"`). Each item:
`_serialize_item = _serialize_data(item) + b"\n"` (`routing.py:624-625`) —
validate (if `stream_item_field`) + `serialize_json` (Rust) + `\n`. Async path
has the `anyio.sleep(0)` cancellation checkpoint; sync path iterates directly
(`641-645`). No keepalive machinery (JSONL has no comment concept). OpenAPI
emits `application/jsonl` with `itemSchema` (§5.2).

**This is a great pattern for AI batch/structured streaming** (e.g. streaming
structured records, RAG chunks): return `AsyncIterator[Record]`, get typed
Rust-serialized NDJSON for free. Steal it.

### 6.6 The stream-item-type extraction (`routing.py:846-924`, `dependencies/utils.py:266-283`)

`get_stream_item_type(annotation)` (`utils.py:276-283`): if
`get_origin(annotation) in {AsyncIterable, AsyncIterator, AsyncGenerator,
Iterable, Iterator, Generator}`, return the first type arg (or `Any`). So
`async def stream() -> AsyncIterator[TokenChunk]` → `stream_item_type =
TokenChunk` → a `stream_item_field` ModelField (`mode="serialization"`,
`routing.py:916-922`) → per-item Rust validate+serialize + an OpenAPI
`itemSchema`. **`ServerSentEvent` is explicitly excluded** as a stream item
type (`routing.py:857-863`): "it's a transport wrapper, not a data model, so
it shouldn't feed into validation or OpenAPI schema generation." So
`AsyncIterator[ServerSentEvent]` streams events without imposing a data schema;
`AsyncIterator[TokenChunk]` + `EventSourceResponse` validates each token.

### 6.7 Streaming — perf, steal, avoid, Rust design

**Perf cost (FastAPI):**
- Per item: `_serialize_sse_item` → either Pydantic `model_dump_json` (Rust,
  fast) or `json.dumps(jsonable_encoder(...))` (Python slow). Plus
  `format_sse_event` builds a `list[str]`, does `splitlines()`, an f-string
  per line, `"\n".join`, `.encode()` — **all pure Python, per event**. For
  high-frequency token streams (e.g. 100 tokens/s) this is ~ a few μs of
  Python per token just for framing, plus the serialize.
- The producer/consumer split adds **two anyio tasks + two memory-object-
  streams (size 1) + a task group per request**. For thousands of concurrent
  SSE connections (typical for an LLM gateway) that's 2× tasks and 2× channels
  per connection — non-trivial event-loop scheduling + memory overhead.
- `anyio.sleep(0)` after every yielded chunk = a forced event-loop trip per
  token (necessary for cancellation, but it's scheduler pressure at high
  token rates).

**STEAL:**
- The **PEP-789-correct keepalive design** (timeout wraps the *channel*, never
  the generator) — this is a genuinely hard correctness lesson; reproduce the
  *semantics* even if the mechanism differs.
- **`X-Accel-Buffering: no` + `Cache-Control: no-cache` by default** on every
  stream. Non-negotiable for token latency.
- **Heterogeneous events un-validated** (`ServerSentEvent` skips schema) but
  **typed items Rust-validated** (`stream_item_field`) — offer both modes.
- **SSE-over-any-method** (POST especially) for MCP/OpenAI-style protocols.
- Native **JSONL auto-streaming** for typed `AsyncIterator[T]`.
- The `data_str.splitlines()` multi-line `data:` rule, LF separators, exact
  `\n\n` terminator, `: ` comment prefix, `id`-no-`\0`, `retry` ms, `data`
  always-JSON / `raw_data` raw — all byte-exact framing rules.

**AVOID:**
- Per-event Python framing. `format_sse_event`'s list-build + f-strings +
  join + encode should be **one Rust function** producing the framed bytes
  directly. For the dominant "token delta = short string" case it's literally
  `b"data: " + json_escape(s) + b"\n\n"` — zero allocation churn in Rust.
- Two anyio memory streams + a task group per connection. DESIGNED: a single
  Rust async stream with an internal **timer-wheel** for the 15 s keepalive
  (one shared timer wheel for all connections, not a per-connection timeout
  task) and a lock-free SPSC ring buffer between the user generator and the
  socket writer. This scales to 10k+ concurrent token streams where FastAPI's
  per-connection-2-tasks model becomes scheduler-bound.
- `json.dumps(jsonable_encoder(...))` per token. The per-token serializer must
  be Rust. The single most common AI case (`yield ServerSentEvent(data=str)`
  or `yield {"delta": "..."}`) must never enter Python recursion.

**Rust-core design for Lancelot AI streaming (DESIGNED):**
1. SSE/JSONL framing = a Rust codec. Input: an item (string / bytes /
   typed-struct handle / dict). Output: framed bytes written straight to the
   ASGI/socket send. The Python endpoint generator yields Python objects; a
   thin Rust shim serializes+frames each without a Python recursion.
2. Keepalive via a global timer wheel; a connection registers its "last write"
   timestamp; a single background task emits `: ping\n\n` to idle connections.
   No per-connection timeout task.
3. Cancellation: a Rust-side select between (socket-writable | client-
   disconnect | generator-item | keepalive-tick) — replaces the anyio task-
   group + `sleep(0)` checkpoint dance with one explicit select loop.
4. Bypass response_model validation for SSE by default (heterogeneous
   streams); opt-in `stream_item_model=` for typed JSONL/SSE → Rust per-item
   validate+dump_json.
5. Backpressure: bounded SPSC ring (FastAPI uses `max_buffer_size=1` — a
   strict rendezvous; Lancelot can tune this; size-1 gives lowest latency +
   natural backpressure onto the LLM producer, which is usually the right
   default for token streams).

---

## 7. Swagger / ReDoc HTML + `responses.py` deprecations (KNOWN — `openapi/docs.py`, `responses.py`)

### 7.1 The docs HTML (`openapi/docs.py:40-389`)

- **Pure f-string template injection**, no Jinja, no templating engine. The
  entire Swagger UI page is an f-string (`docs.py:152-193`); ReDoc likewise
  (`264-298`). Assets default to **jsDelivr CDN**:
  `swagger-ui-dist@5/swagger-ui-bundle.js` + `.css`,
  `redoc@2/bundles/redoc.standalone.js`, favicon
  `fastapi.tiangolo.com/img/favicon.png`.
- **XSS hardening — the one steal-worthy detail**: `_html_safe_json()`
  (`docs.py:9-19`):
  ```python
  return json.dumps(value).replace("<","\\u003c").replace(">","\\u003e").replace("&","\\u0026")
  ```
  Every Swagger UI config value embedded in the inline `<script>` is
  `json.dumps`'d then `<`,`>`,`&` are unicode-escaped to prevent
  `</script>`-breakout injection. Config is also `jsonable_encoder`'d first
  (`docs.py:172`). `swagger_ui_default_parameters` (`docs.py:22-37`):
  `{dom_id:"#swagger-ui", layout:"BaseLayout", deepLinking:True,
  showExtensions:True, showCommonExtensions:True}`.
- `get_swagger_ui_oauth2_redirect_html()` (`docs.py:301-389`) is a static
  copied-from-swagger-ui OAuth2 redirect page (state validation, code/token
  extraction from hash/query, `window.opener.swaggerUIRedirectOauth2`
  callback).
- **Relevance to Lancelot:** docs HTML is *not* architecture — it's a static
  template. The only thing to steal is the `</script>`-breakout escaping
  (`<>&` → `\uXXXX` after `json.dumps`). Everything else is a 30-line template
  Lancelot writes once. Do NOT spend design effort here.

### 7.2 `responses.py` — the deprecation that proves the thesis (`responses.py:39-99`)

`UJSONResponse` and `ORJSONResponse` are both **`@deprecated`** with the exact
message: *"FastAPI now serializes data directly to JSON bytes via Pydantic
when a return type or response model is set, which is faster and doesn't need
a custom response class."* This is the project itself confirming §4's
conclusion: the Rust `dump_json` one-pass beats orjson-on-a-Python-dict
because it never builds the dict. `ORJSONResponse.render` =
`orjson.dumps(content, option=OPT_NON_STR_KEYS | OPT_SERIALIZE_NUMPY)`;
`UJSONResponse.render` = `ujson.dumps(content, ensure_ascii=False).encode()`.
Both are now legacy. `EventSourceResponse` is re-exported here too
(`responses.py:5`). **Lancelot lesson:** do not offer pluggable JSON-encoder
response classes as a feature — make the one Rust serializer the only path;
"swap in orjson" stops being meaningful when serialization is already native.

---

## 8. Consolidated Lancelot synthesis — the build sheet (DESIGNED)

### 8.1 Top 5 steals (ranked)

1. **One-pass typed→bytes serialization** (`v2.py:215-239`, `routing.py:692-
   713`). The compiled-per-type Rust serializer that emits JSON **bytes**
   directly into the response body, no intermediate Python dict, no second
   encoder. Make this Lancelot's *only* typed-response path. This single
   decision is the entire performance story; FastAPI deprecated
   ORJSONResponse to push everyone onto it.
2. **Per-route compiled validator/serializer/schema from one type
   representation** (`v2.py:113-166`, `v2.py:285-346`). Compile the
   annotation once at route registration into a Rust handle that yields the
   validator, the serializer, AND the JSON-Schema. OpenAPI becomes a free
   byproduct, not a separate pass.
3. **PEP-789-correct SSE keepalive** (`routing.py:534-595`). The timeout must
   wrap the *channel/select*, never the generator's `__anext__`. Plus
   `X-Accel-Buffering: no` + `Cache-Control: no-cache` by default on every
   stream. This is hard-won correctness + the make-or-break latency header for
   LLM token streaming.
4. **Heterogeneous-event SSE (no schema) + typed JSONL (Rust per-item)**
   (`routing.py:500-527`, `620-652`, `857-863`). Token streams need mixed
   event shapes (delta/tool_call/done/error) un-validated; structured streams
   want typed Rust NDJSON. Offer both; default SSE = un-validated, opt-in
   `stream_item_model=` = typed.
5. **The pydantic-core error-dict wire shape + traceback-style endpoint
   context** (`exceptions.py:174-243`, `v2.py:486-493`,
   `openapi/utils.py:42-69`). Adopt `{type,loc,msg,input,ctx}` verbatim
   (ecosystem standard) and the `File "...", line N, in func` DX in error
   bodies/logs. Build errors in Rust with the loc prefix already applied.

### 8.2 The serialization bottleneck — verdict

The bottleneck in FastAPI is **`jsonable_encoder` recursion + the
double-JSON-parse on input + double-validate on output**, and FastAPI itself
escaped most of it by delegating to pydantic-core's Rust `validate_python` /
`dump_json`. The residual Python tax that remains in FastAPI 0.136 and that
Lancelot must eliminate:

- **Input:** `request.json()` (`json.loads`, Python) → then
  `validate_python` (Rust). Two parses. **Fix:** `validate_json(bytes)`
  one-pass (jiter-class) in Lancelot's core. (FastAPI does NOT do this — a
  free Lancelot win.)
- **Output (typed, default class):** already optimal in FastAPI (`dump_json`).
  Keep — but make it the *only* path and consider making the redundant
  response re-validate opt-out for hot trusted endpoints.
- **Output (untyped / custom response class / SSE-non-Pydantic /
  OpenAPI doc):** falls into `jsonable_encoder` — O(nodes) Python recursion,
  6–9 isinstance checks per scalar, set re-construction per frame, double
  memory, per-dict-key recursion. **This function should not exist on
  Lancelot's hot path.** Replace with: (a) schema-specialized Rust serializer
  for typed, (b) schemaless Rust value-walker (orjson-class + the
  `ENCODERS_BY_TYPE` extensions: Decimal int-vs-float, IP→str, Path→str,
  custom Color, `Pattern.pattern`) for `dict`/`Any`, (c) a rare Python
  callback only for genuinely unknown user types (`custom_encoder` /
  `vars(obj)` fallback).

Where Python reflection dominates: (1) startup — `shared.py`'s 6-predicates-
each-re-walk-the-Union annotation classification (O(routes×params×typetree),
duplicated); fix with a single-pass Rust classifier at registration. (2)
per-request error path — `_regenerate_error_with_loc` dict-spread per error;
fix by emitting loc-prefixed errors from Rust. (3) untyped/streaming serialize
— `jsonable_encoder` per node; fix per above. The **happy path of a typed
JSON request→response in FastAPI 0.136 is already ~90% Rust** — Lancelot's
gains are: kill the input double-parse, kill `jsonable_encoder` for the
remaining cases, kill the per-connection 2-task SSE model, and remove the
Python↔TypeAdapter impedance layer (`__post_init__` tear-apart) by owning the
type system end to end.

### 8.3 SSE / streaming findings for AI workloads — summary

- SSE is **native and first-class** in FastAPI 0.136 (`EventSourceResponse`
  marker + routing-layer encoder), works **over POST** (MCP/OpenAI-style).
  Lancelot must match this.
- Framing is byte-exact and documented in §6.3 — LF separators, `\n\n`
  terminator, multi-line `data:` via `splitlines()`, `data` always-JSON vs
  `raw_data` raw, `id` no-`\0`, `retry` ms, `: ` comment prefix.
  `KEEPALIVE_COMMENT=b": ping\n\n"`, `_PING_INTERVAL=15.0`s.
- The keepalive is injected via a **producer→channel→keepalive-inserter**
  split so the 15 s `fail_after` wraps the channel `receive()`, never the LLM
  generator (PEP-789); entered on the request `AsyncExitStack` so teardown is
  structured, not `GeneratorExit`. An `anyio.sleep(0)` per chunk is a
  mandatory cancellation checkpoint.
- For AI: heterogeneous events bypass validation (`ServerSentEvent` excluded
  from `stream_item_field`); typed streams get Rust per-item
  validate+`serialize_json`. Per-token serialization of non-Pydantic data
  still hits the slow `json.dumps(jsonable_encoder(...))` path — **the one
  per-token Python tax Lancelot must remove** with a Rust SSE codec.
- Lancelot streaming design: Rust SSE/JSONL codec (no Python framing), global
  timer-wheel keepalive (not per-connection task), explicit Rust select loop
  for cancellation, bounded SPSC ring (size-1 default = lowest latency +
  backpressure onto the model), opt-in typed per-item Rust validation.

### 8.4 Avoid list (FastAPI mistakes Lancelot must not inherit)

- Two/three response serialization paths (fast/slow/untyped) for backward
  compat. One typed Rust path + one raw escape hatch only.
- Pluggable JSON-encoder response classes (`ORJSONResponse` etc.) — already
  deprecated by FastAPI; don't add the concept.
- `json.loads` then `validate_python` (input double-parse).
- Per-field `FieldInfo`→`asdict`→`Field(**attrs)`→`TypeAdapter(Annotated[…])`
  rebuild dance — an artifact of Pydantic being a separate library.
- `shared.py`-style multiple predicate functions each re-expanding the same
  `Union` annotation; single-pass classify instead.
- Per-connection 2-anyio-tasks + 2-memory-streams SSE model at scale.
- Running the final OpenAPI doc back through a Pydantic model +
  `jsonable_encoder` (fine when cached once; bad if ever lazy).
- `deepcopy(field.default)` per request for mutable optional defaults — use
  Rust structural clone or per-request default factories.

---

## Appendix — file:line evidence index (KNOWN, for reimplementation cross-check)

- `_compat/__init__.py:1-40` — re-export surface (the public `_compat` API).
- `_compat/shared.py:47-214` — annotation classification predicates;
  `lenient_issubclass:47`, `field_annotation_is_sequence:64`,
  `field_annotation_is_complex:88`, `field_annotation_is_scalar:104`,
  `annotation_is_pydantic_v1:202` (v1-detect-to-raise).
- `_compat/v2.py:57-70` — `GenerateJsonSchema.bytes_schema` override.
- `_compat/v2.py:100-110` — `asdict(FieldInfo)` shim (`_Attrs` allowlist
  `74-96`).
- `_compat/v2.py:113-244` — `ModelField`; `__post_init__:141`
  (TypeAdapter compile `163`), `validate:173`, `serialize:190`,
  `serialize_json:215` (one-pass dump_json comment `226-230`), `__hash__:241`.
- `_compat/v2.py:254-282` — `get_schema_from_model_field` (mode/alias/title).
- `_compat/v2.py:285-346` — `get_definitions` (flat models, batch
  `generate_definitions`, separate input/output, `\f` desc strip).
- `_compat/v2.py:413-415` — `get_cached_model_fields` (`@lru_cache`).
- `_compat/v2.py:425-483` — name map + flat-model transitive closure (set
  dedup).
- `_compat/v2.py:486-493` — `_regenerate_error_with_loc`.
- `encoders.py:84-126` — `ENCODERS_BY_TYPE` (25 entries) + inverted tuple map.
- `encoders.py:129-365` — `jsonable_encoder` dispatch ladder (order in §2.1).
- `sse.py:9-17` — `_SSE_EVENT_SCHEMA` (OpenAPI 3.2).
- `sse.py:20-33` — `EventSourceResponse` marker.
- `sse.py:42-143` — `ServerSentEvent` model (data/raw_data exclusivity
  `135-143`, id-no-null `36-39`).
- `sse.py:146-214` — `format_sse_event` byte-exact framing.
- `sse.py:218,221-222` — `KEEPALIVE_COMMENT`, `_PING_INTERVAL=15.0`.
- `responses.py:39-99` — deprecated `UJSONResponse`/`ORJSONResponse`.
- `exceptions.py:174-257` — `ValidationException` family + 422/500 shapes +
  `EndpointContext`.
- `openapi/constants.py:1-3` — `METHODS_WITH_BODY`, `REF_PREFIX`,
  `REF_TEMPLATE`.
- `openapi/utils.py:42-69` — hardcoded ValidationError defs.
- `openapi/utils.py:260-478` — `get_openapi_path` (SSE/JSONL OpenAPI
  `356-392`, 422 injection `454-474`).
- `openapi/utils.py:481-606` — `get_fields_from_routes` + `get_openapi`
  (sorted schemas `595-596`, final jsonable_encoder `606`).
- `openapi/docs.py:9-19` — `_html_safe_json` (`</script>` breakout escape).
- `openapi/models.py:57-58` — `BaseModelWithConfig` extra=allow.
- `routing.py:277-317` — `serialize_response` (validate→serialize round-trip).
- `routing.py:351-729` — `get_request_handler.app` (body sniff `401-449`;
  SSE engine `496-619`; JSONL `620-652`; raw stream `653-672`; typed fast/slow
  `673-718`).
- `routing.py:846-924` — route construction (stream item extraction,
  response/stream fields, mode=serialization).
- `routing.py:967-996` — `is_sse_stream`/`is_json_stream` detection +
  handler wiring.
- `dependencies/utils.py:266-283` — `get_stream_item_type` (`_STREAM_ORIGINS`).
- `dependencies/utils.py:738-781` — `_validate_value_with_model_field`,
  `_get_multidict_value` (`deepcopy(default)`).
- `dependencies/utils.py:784-1058` — `request_params_to_args`,
  `request_body_to_args`, `get_body_field`, `get_validation_alias`.
- `utils.py:58-78` — `create_model_field` (v1-raise gate).
