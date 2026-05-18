# FastAPI Conformance Spec — Params + Dependency Injection + Validation (the wire contract Lancelot MUST satisfy)

**Target:** FastAPI 0.136.x test suite at `/Users/seventyleven/Desktop/researchfms/fastapi/fastapi/tests/`
exercised against source at `…/fastapi/fastapi/`.
**Purpose:** This is the *literal* behavioral spec. Lancelot dogfoods these tests; every numbered
**Lancelot MUST** rule below is tied to a test or source line. Pass these byte-for-byte and Lancelot is
"stable like FastAPI" for params/DI/validation. Labels: **KNOWN** = asserted by a test or read from
source; **INFERRED** = deduced from code behavior across tests; **DESIGN** = Lancelot implementation
note (not part of the contract, just how to satisfy it).

Companion: `Lancelot/research/fastapi/02-dependency-injection.md` (the two-phase compiler internals);
this file is the *observable contract*, that file is the *mechanism*.

---

## Executive summary — the 12 top conformance rules (read this first)

1. **422 body is exactly `{"detail":[{type,loc,msg,input[,ctx]}]}`** — a list of pydantic-core v2
   error dicts with `url` stripped (`include_url=False`), `loc` as a JSON array, prefixed with the
   source tag. `ctx` present only when pydantic emits it. Status is **422**; JSON parse failure is a
   **422** with `type:"json_invalid"`; unparseable non-JSON body is **400** `"There was an error
   parsing the body"`. (`exception_handlers.py:20-26`, `_compat/v2.py:185-188`, `routing.py:427-449`)
2. **`loc` first element is the source:** `"path"|"query"|"header"|"cookie"|"body"`; then the param
   name (or its **validation alias**); then nested keys/indices for models/sequences
   (`["body",0,"age"]`, `["query","q",1]`). (`utils.py:846-866,976-998`; `test_path.py:51`,
   `test_multi_query_errors.py:29-38`, `test_multi_body_errors.py:60-84`)
3. **`Optional[T]` ≠ optional.** Required-ness is decided by the **default**, not the type.
   `q: str | None` (no default) is **required → 422 missing**. `q: str | None = None` is optional.
   (`test_required_noneable.py:25-27`; `test_request_params/test_query/test_optional_str.py:16-17,53-57`)
4. **Alias precedence:** `validation_alias` (if set) wins for *reading the value* over `alias`; `alias`
   wins over the param name; the param name alone is never accepted once an alias is set. The `loc` and
   OpenAPI `name` use the validation alias. (`params.py:113-116`, `utils.py:1055-1057`;
   `test_request_params/test_query/test_required_str.py:177-415`)
5. **Path params are always required**, never have a default (`Path(default=...)` is an
   `AssertionError` at registration), and only scalar/scalar-sequence types are allowed (model/dict
   path param = `AssertionError`). (`params.py:185`, `test_invalid_path_param.py:6-15`,
   `test_path.py` whole file)
6. **Body embedding rule:** a single non-`embed` body param is the **whole** JSON body
   (`{"name":...}` not `{"item":{"name":...}}`); ≥2 body params, or `Body(embed=True)`, or a
   bare-scalar Form param → each is a **key inside** the JSON/form object, `loc=["body","<name>"]`.
   (`utils.py:888-909,959-998`; `test_tutorial/test_body/test_tutorial001.py:73-85`,
   `test_required_noneable.py:17-19,47-60`, `test_tutorial/test_body_multiple_params/test_tutorial003.py`)
7. **DI cache key is `(callable, sorted(scopes) if scopes-aware else (), computed_scope or "")`**;
   default `use_cache=True` → one call per request shared across the graph; `use_cache=False` → fresh
   call every time; `Security(dep, scopes=[…])` with different scopes → separate cache slots.
   (`models.py:62-71`; `test_dependency_cache.py:54-92`)
8. **`yield` dependency teardown:** code after `yield` runs **after the response is sent** for
   `scope="request"` (the default for yield deps) and **before the response is sent** for
   `scope="function"`. Teardown order is **reverse of setup (LIFO)**. The exception that aborted the
   request is **re-raised into the generator at the `yield`**. (`test_dependency_yield_scope.py`,
   `test_dependency_contextmanager.py`, `test_dependency_after_yield_raise.py`)
9. **A `scope="request"` dep may NOT depend on a `scope="function"` dep** — `FastAPIError` raised at
   **registration/import** (fail-fast), exact message
   `'The dependency "X" has a scope of "request", it cannot depend on dependencies with scope
   "function"'`. (`test_dependency_yield_scope.py:182-190`, `utils.py:319-329`)
10. **`dependency_overrides[orig] = repl`** swaps the callable *and re-derives its sub-graph from the
    replacement's signature* (the replacement's params are validated, the original's are not). Route-
    level and decorator-level deps are overridable identically. Empty dict = no overrides.
    (`test_dependency_overrides.py:208-389`)
11. **Coercion is pydantic-core v2 exactly.** `int`/`float`/`bool`/`Decimal` parsing, constraint
    checks (`gt/ge/lt/le/min_length/max_length/pattern/multiple_of`), and their error
    `type`/`msg`/`ctx` must match pydantic-core's strings verbatim (e.g. `int_parsing`,
    `greater_than` with `ctx:{"gt":3.0}` for float-typed, `{"gt":3}` for int-typed). Repeated
    scalar query/header param → list; empty repeated → `[]` only if a default supplies it.
    (`test_path.py`, `test_query.py`, `test_multi_query_errors.py`)
12. **Sequence params** (`list[int]` etc.) read **all** repeated occurrences via `getlist`; missing
    required sequence → `missing`; per-element coercion error → `loc=[src,name,index]`. Form `""` for
    a required field counts as missing; query/header empty-list counts as missing unless a default
    exists. (`utils.py:753-781`, `test_query.py:251-277`, `test_multi_query_errors.py:22-40`)

---

## 0. The 422 / error wire shape (the single most load-bearing contract)

### 0.1 Envelope (KNOWN — `exception_handlers.py:20-26`)

```python
async def request_validation_exception_handler(request, exc):
    return JSONResponse(status_code=422,
                         content={"detail": jsonable_encoder(exc.errors())})
```

So the response is **HTTP 422**, `Content-Type: application/json`, body literally:

```json
{"detail": [ <err>, <err>, ... ]}
```

`<err>` is one pydantic-core v2 error dict (see §0.2). Multiple errors accumulate into the list in
**discovery order** (path → query → header → cookie → body, and within body in field order — see §0.5).

WebSocket validation failure does **not** send 422; it closes the socket with code
`1008` (`WS_1008_POLICY_VIOLATION`) and `reason = jsonable_encoder(exc.errors())`
(`exception_handlers.py:29-34`). **Lancelot MUST** mirror this for WS.

### 0.2 The per-error dict (KNOWN — `_compat/v2.py:173-188`, `:379-382`, `:486-490`)

`ModelField.validate` calls `self._type_adapter.validate_python(value, from_attributes=True)`; on
`ValidationError` it returns `_regenerate_error_with_loc(errors=exc.errors(include_url=False),
loc_prefix=loc)`. `_regenerate_error_with_loc` is:

```python
def _regenerate_error_with_loc(*, errors, loc_prefix):
    return [{**err, "loc": loc_prefix + err.get("loc", ())} for err in errors]
```

Therefore each error dict is **exactly pydantic-core's `error.errors(include_url=False)` element**,
with `loc` left-prefixed by FastAPI's source tuple. Concretely the keys are:

| key | type | always? | source |
|---|---|---|---|
| `type` | str | yes | pydantic-core error tag (`missing`, `int_parsing`, `greater_than`, `string_too_short`, `json_invalid`, `decimal_parsing`, `bool_parsing`, `float_parsing`, …) |
| `loc`  | array | yes | `[source, name, …nested]` — FastAPI prefix + pydantic path |
| `msg`  | str | yes | pydantic-core's human message, verbatim |
| `input`| any | yes | the rejected input value (may be `None`, `{}`, the raw string, the dict, a list) |
| `ctx`  | object | only if pydantic emits it | constraint context, e.g. `{"gt":3.0}`, `{"min_length":3}`, `{"error":"<jsonmsg>"}` |

**The `url` key pydantic normally adds is ALWAYS stripped** (`include_url=False`). **Lancelot MUST
NOT emit `url`.**

`get_missing_field_error(loc)` (`_compat/v2.py:379-382`) synthesizes a "missing" error by running a
real validation of `{}` against a required field, so the shape is byte-identical to pydantic's:
`{"type":"missing","loc":loc,"msg":"Field required","input":{}}` — note FastAPI then overrides
`input` per-call site (the tests show `input:None` for query/path missing, `input:{}` or the partial
dict for body — see §0.4).

### 0.3 Canonical 422 bodies (KNOWN — verbatim from tests)

Missing scalar query (`test_query.py:11-20`):
```json
{"detail":[{"type":"missing","loc":["query","query"],"msg":"Field required","input":null}]}
```
Int parse fail, query (`test_query.py:86-95`):
```json
{"detail":[{"type":"int_parsing","loc":["query","query"],
 "msg":"Input should be a valid integer, unable to parse string as an integer","input":"42.5"}]}
```
Path int parse fail (`test_path.py:47-56`):
```json
{"detail":[{"type":"int_parsing","loc":["path","item_id"],
 "msg":"Input should be a valid integer, unable to parse string as an integer","input":"foobar"}]}
```
Path constraint, string min length (`test_path.py:233-243`):
```json
{"detail":[{"type":"string_too_short","loc":["path","item_id"],
 "msg":"String should have at least 3 characters","input":"fo","ctx":{"min_length":3}}]}
```
Path numeric `gt` — note **ctx float vs int tracks the declared type** (`test_path.py:315-325` float
→ `{"gt":3.0}`; `:567-577` int → `{"gt":3}`):
```json
{"detail":[{"type":"greater_than","loc":["path","item_id"],
 "msg":"Input should be greater than 3","input":"2","ctx":{"gt":3.0}}]}
```
Body, nested model list element (`test_multi_body_errors.py:56-86`) — `loc` includes the **list
index** and the **input is the offending sub-object / value**:
```json
{"detail":[
 {"type":"missing","loc":["body",0,"name"],"msg":"Field required","input":{"age":"five"}},
 {"type":"decimal_parsing","loc":["body",0,"age"],"msg":"Input should be a valid decimal","input":"five"},
 {"type":"missing","loc":["body",1,"name"],"msg":"Field required","input":{"age":"six"}},
 {"type":"decimal_parsing","loc":["body",1,"age"],"msg":"Input should be a valid decimal","input":"six"}]}
```
Repeated query coercion, per-element index in `loc` (`test_multi_query_errors.py:25-40`):
```json
{"detail":[
 {"type":"int_parsing","loc":["query","q",0],"msg":"Input should be a valid integer, unable to parse string as an integer","input":"five"},
 {"type":"int_parsing","loc":["query","q",1],"msg":"Input should be a valid integer, unable to parse string as an integer","input":"six"}]}
```
Malformed JSON body (`routing.py:427-441`) — **422**, `type:"json_invalid"`, `loc=("body", <byte
offset>)`, `ctx.error` = the JSON lib message, `input:{}`:
```json
{"detail":[{"type":"json_invalid","loc":["body",<pos>],"msg":"JSON decode error","input":{},"ctx":{"error":"<msg>"}}]}
```
Body present but **not parseable at all / wrong container with no content-type** → **HTTP 400**
(NOT 422) `{"detail":"There was an error parsing the body"}` (`routing.py:445-449`).

### 0.4 `input` field semantics (KNOWN — cross-test)

- Missing **scalar query/path/header/cookie** → `input` is `null` (`test_query.py:18`,
  `test_path.py` passim, `test_dependency_overrides.py:60`). NOTE the model-form of the same param
  shows `IsOneOf(None, {})` (`test_request_params/test_query/test_required_str.py:61`) — i.e. when the
  param is a **Pydantic model bound to Query()**, missing → `input:{}`; when it's a **bare scalar**,
  missing → `input:null`. **Lancelot MUST** emit `null` for bare-scalar-missing and `{}` for
  model-bound-missing.
- Missing **body sub-field** when other fields present → `input` is the **partial parsed object**
  (`test_forms_single_model.py:99-109` → `input:{"tags":["foo","bar"],"with":"nothing"}`;
  `test_tutorial/test_body/test_tutorial001.py:79-83` → `input:{"name":"Foo"}`).
- Missing **embedded body key** with `json=None` body → `input:null`
  (`test_tutorial/test_body_multiple_params/test_tutorial003.py` no-data case).
- Coercion failure → `input` is the **raw rejected value** as received (string for query,
  number/obj for JSON body).

### 0.5 Error ordering (KNOWN — `solve_dependencies` + `request_params_to_args`)

Within one source bucket, errors are appended **in declared field order** (`utils.py:852-865` loops
`for field in fields`). Across buckets the order is the dependency-solve order: sub-dependencies
first (depth-first), then path, query, header, cookie, then body
(`test_multi_body_errors.py:59-85` shows body field-order; `test_dependency_duplicates.py` shows sub-
dep params surface with `loc=["body","item2"]` before/with the endpoint's own). **Lancelot MUST**
preserve this stable order (tests assert exact list equality, not set equality).

> **Lancelot MUST (1):** Return HTTP 422 with body `{"detail":[…]}` where each element is a
> pydantic-core v2 error dict, `url` removed, `loc` = source-prefixed array. (`test_query.py:11`)
> **Lancelot MUST (2):** Use `loc[0] ∈ {path,query,header,cookie,body}` then validation-alias name
> then nested keys/indices. (`test_path.py:51`, `test_multi_query_errors.py:29`)
> **Lancelot MUST (3):** Emit `ctx` only when pydantic-core would, and track numeric ctx number type
> to the declared field type (`{"gt":3.0}` for float, `{"gt":3}` for int). (`test_path.py:537,322`)
> **Lancelot MUST (4):** Use byte-exact pydantic-core `type` tags and `msg` strings (do not invent
> messages). (`test_path.py:103`, `test_query.py:91`)
> **Lancelot MUST (5):** `input` = `null` for missing bare scalar; `{}` for missing model-bound
> param; the partial parsed object for missing body sub-fields; the raw value for coercion fails.
> (`test_query.py:18`, `test_request_params/test_query/test_required_str.py:61`,
> `test_forms_single_model.py:102`)
> **Lancelot MUST (6):** Malformed JSON → 422 `type:"json_invalid"` `loc=["body",pos]`
> `ctx:{"error":…}`; unparseable body → 400 `"There was an error parsing the body"`.
> (`routing.py:427-449`)
> **Lancelot MUST (7):** WebSocket validation failure closes with code 1008 and
> `reason=json(errors())`, never an HTTP body. (`exception_handlers.py:29-34`)
> **Lancelot MUST (8):** Accumulate all errors for a request (do not fail-fast on first) and emit
> them in stable declared order; tests assert list equality. (`test_multi_body_errors.py:56`)

---

## 1. Param coercion + required / optional / default / None semantics

### 1.1 Required-ness is decided by the DEFAULT, not the type (KNOWN)

This is the single most counter-intuitive rule and the one most likely to break a naive reimpl.

| signature | required? | missing → | evidence |
|---|---|---|---|
| `def f(query)` (no annotation, no default) | **required** | 422 missing | `main.py:146`, `test_query.py:8-20` |
| `def f(query=None)` | optional, default `None` | 200, `None` | `main.py:151`, `test_query.py:44-47` |
| `def f(query: int)` | **required** | 422 missing | `main.py:158`, `test_query.py:62-74` |
| `def f(query: int \| None = None)` | optional | 200 | `main.py:163`, `test_query.py:128-131` |
| `def f(query: int = 10)` | optional, default `10` | 200, `10` | `main.py:170`, `test_query.py:155-158` |
| `def f(q: str \| None)` (Optional, **no** default) | **required** | 422 | `test_required_noneable.py:8-9,25-27` |
| `def f(q=Query(default=None))` | optional | 200 | `main.py:175`, `test_query.py:182-185` |
| `def f(q=Query())` / `Query(...)` | **required** | 422 | `main.py:182`, `test_query.py:194-206` |
| `def f(p: Annotated[str, Query()])` | **required** | 422 | `test_request_params/test_query/test_required_str.py:16-17,51-64` |
| `def f(p: Annotated[str\|None, Query()] = None)` | optional | 200 | `…/test_optional_str.py:75-79,115-119` |

Mechanism (`utils.py:738-746`): `_validate_value_with_model_field` — if extracted `value is None`:
if `field.field_info.is_required()` → emit `get_missing_field_error(loc)`; else
`return deepcopy(field.default), []`. `is_required()` is pydantic's: a field is required iff it has
**no default and no default_factory**. `T | None` does **not** set a default.

> **Lancelot MUST (9):** Treat a param as required iff it has no `=default`, no
> `Query/Path/...(default=…)`, no `default_factory` — **independent of whether the type is
> `Optional`/`| None`**. (`test_required_noneable.py:25-27`)
> **Lancelot MUST (10):** When an optional param is absent, inject a **deep copy** of the declared
> default (not a shared reference) so mutable defaults like `Query(default=[])` are not aliased
> across requests. (`utils.py:745,780`; `test_query.py:274-277` returns `[]`)

### 1.2 Default-value injection details (KNOWN)

- Absent optional → handler receives `deepcopy(field.default)`. `Query(default=[])` →
  handler gets a fresh `[]` each call (`test_query.py:268-277`).
- `query: int = 10` absent → `10`; present-but-bad (`?query=foo`) → **422** (the default does NOT
  rescue a present-but-invalid value) (`test_query.py:167-179`).
- `Path(default=...)` is illegal: `assert default is ..., "Path parameters cannot have a default
  value"` raised at class construction → registration `AssertionError`
  (`params.py:185`, `test_invalid_path_param.py:11-18`).
- In `Annotated`, you may **not** put the default inside the marker:
  `Annotated[int, Query(default=1)]` → `AssertionError` `"`Query` default value cannot be set in
  `Annotated` for 'item_id'. Set the default value with `=` instead."`
  (`test_invalid_path_param.py:20-30`).

### 1.3 Type coercion = pydantic-core v2, verbatim (KNOWN)

All scalar coercion and all error `type`/`msg` strings are produced by pydantic-core's
`validate_python` on a per-field `TypeAdapter` (`_compat/v2.py:163-188`). The test suite pins the
exact behavior:

`int` (`test_path.py:44-92`, `test_query.py:83-110`):
- `"42"` → `42`; `"foobar"`/`"True"`/`"42.5"`/`"2.7"` → 422 `int_parsing`
  `msg:"Input should be a valid integer, unable to parse string as an integer"`.
- Note `"42.5"` for an int param → `int_parsing` (NOT float-then-truncate).

`float` (`test_path.py:95-134`): `"42"` → `42`, `"42.5"` → `42.5`, `"foobar"`/`"True"` → 422
`float_parsing` `"Input should be a valid number, unable to parse string as a number"`.

`bool` (`test_path.py:137-215`): truthy set `{"1","true","True"}` → `true`; falsy
`{"0","false","False"}` → `false`; `"foobar"`/`"42"`/`"42.5"` → 422 `bool_parsing`
`"Input should be a valid boolean, unable to interpret input"`. (pydantic-core's bool string set.)

`str` accepts anything from path/query (`"42"`, `"True"` stay strings — `test_path.py:32-41`).

Numeric constraints (`test_path.py`):
- `gt` → `greater_than` `"Input should be greater than {n}"` `ctx:{"gt":n}`
- `ge` → `greater_than_equal` `"Input should be greater than or equal to {n}"` `ctx:{"ge":n}`
- `lt` → `less_than` ; `le` → `less_than_equal`
- The **ctx number is float `n.0` when the field type is float**, plain int `n` when int
  (`test_path.py:322` `{"gt":3.0}` vs `:574` `{"gt":3}`). This is pydantic-core behavior and
  **must be reproduced** — Lancelot cannot just stringify the constraint.
- Combined `lt`+`gt`: the failing bound's error is emitted (whichever the value violates;
  `test_path.py:456-485` shows `lt` for too-big, `gt` for too-small).

String constraints: `min_length` → `string_too_short` `"String should have at least {n}
characters"` `ctx:{"min_length":n}`; `max_length` → `string_too_long`
`"String should have at most {n} characters"` `ctx:{"max_length":n}`
(`test_path.py:230-303`).

`Decimal` (`condecimal`) parse fail → `decimal_parsing` `"Input should be a valid decimal"`;
constraint `gt=Decimal(0)` → `greater_than` `ctx:{"gt":0}`, and a valid decimal serializes back as a
**string** `"5"` (`test_multi_body_errors.py:25-53`).

> **Lancelot MUST (11):** Delegate scalar coercion + constraint validation to pydantic-core v2 (FFI),
> not a hand-rolled parser, so `type`/`msg`/`ctx`/string-set behavior is byte-identical.
> (`test_path.py` whole file, `test_query.py`)
> **Lancelot MUST (12):** For an `int`-typed param, a fractional string (`"2.7"`,`"42.5"`) is
> `int_parsing` (not float-truncate); a present-but-invalid value never falls back to the default.
> (`test_path.py:543-555`, `test_query.py:167-179`)
> **Lancelot MUST (13):** Constraint ctx numbers must carry the declared field's numeric type
> (float field → `3.0`, int field → `3`). (`test_path.py:322` vs `:574`)

### 1.4 Sequence / list / set params (KNOWN)

`_get_multidict_value` (`utils.py:753-781`): if the field annotation is a sequence **and** the source
is a multidict/Headers, use `values.getlist(alias)` (collect ALL repeats); else `values.get(alias)`.

- `device_ids: list[int] = Query()` with `?device_ids=1&device_ids=2` → `[1,2]`
  (`main.py:202`, `test_query.py:257-260`).
- Required `list` with **no occurrences** → 422 missing (`test_query.py:263-266`); empty list with a
  default `Query(default=[])` → `[]` (`test_query.py:274-277`).
- `frozenset[int]` with `?query=1&query=1&query=2` → dedup `{1,2}`
  (`main.py:197`, `test_query.py:251-254`).
- `q: list[int] = Query(default=None)` with `?q=five&q=six` → 422 with **per-index loc**
  `["query","q",0]` and `["query","q",1]` (`test_multi_query_errors.py:25-40`).
- A required sequence behaves "missing" if the collected list is **empty**: `_get_multidict_value`
  treats `len(value)==0` as missing (`utils.py:772-776`).
- Only scalar / scalar-sequence types are valid for query: `list[Item]`/`tuple[..]`/`dict[..]`/bare
  `dict` as a query param → registration `AssertionError` `"Query parameter 'q' must be one of the
  supported types"` (`test_invalid_sequence_param.py:6-62`). For path params the same restriction
  raises a plain `AssertionError` (`test_invalid_path_param.py:6-15`, `utils.py:549-555`).

> **Lancelot MUST (14):** For sequence-typed query/header/form params, collect **every** repeated
> occurrence (`getlist` semantics), coerce per element, and index errors as `[src,name,i]`.
> (`test_multi_query_errors.py:29-38`, `test_query.py:257-260`)
> **Lancelot MUST (15):** A required sequence with zero occurrences is `missing`; an optional one
> falls back to `deepcopy(default)`; `frozenset`/`set` dedup as pydantic-core does.
> (`test_query.py:263-277,251-254`)
> **Lancelot MUST (16):** Reject non-scalar query/path param types at **registration** with
> `AssertionError` (message `"Query parameter '<n>' must be one of the supported types"` for query).
> (`test_invalid_sequence_param.py:8-9`)

### 1.5 `None` / `Undefined` / explicit `Query(...)` (KNOWN)

- `Query()` and `Query(...)` (Ellipsis) are both **required** (Ellipsis is the FastAPI "required"
  sentinel via pydantic `Undefined`) (`test_query.py:194-206`, `main.py:182,197`).
- `Query(default=None)` is **optional**, value `None` if absent (`test_query.py:182-185`).
- `b: str | None = Body(embed=True)` with empty body or wrong key → 422 (the embed key is required
  because `str|None` has no default) (`test_required_noneable.py:47-55`).

> **Lancelot MUST (17):** Treat `Query()`/`Query(...)`/`Path()` (no default) as required;
> `Query(default=None)` as optional-with-`None`; `Undefined`/Ellipsis is the required sentinel.
> (`test_query.py:194-206,182-185`)

---

## 2. Alias + `Annotated` precedence (the rule almost everyone gets subtly wrong)

### 2.1 The alias mechanism (KNOWN — `params.py:113-116`, `utils.py:1055-1057`)

In `Param.__init__`:
```python
if serialization_alias in (_Unset, None) and isinstance(alias, str):
    serialization_alias = alias
if validation_alias in (_Unset, None):
    validation_alias = alias
```
So setting only `alias="x"` propagates to **both** `validation_alias` and `serialization_alias`.
Reading uses `get_validation_alias(field)` = `field.validation_alias or field.alias`
(`utils.py:1055-1057`). `_get_multidict_value` reads `values.getlist(alias)` / `values.get(alias)`
with `alias = alias or get_validation_alias(field)` (`utils.py:756`).

### 2.2 The precedence matrix (KNOWN — `test_request_params/test_query/test_required_str.py`)

For a query param `p` declared four ways, with request keys `p`, `p_alias`, `p_val_alias`:

| declaration | accepted request key | OpenAPI `name` / `loc` name | source lines |
|---|---|---|---|
| `Query()` (no alias) | `p` only | `p` | `:16-75` |
| `Query(alias="p_alias")` | `p_alias` only (`?p=…` → 422 missing `loc:["query","p_alias"]`) | `p_alias` | `:82-170` |
| `Query(validation_alias="p_val_alias")` | `p_val_alias` only (`?p=…` → 422) | `p_val_alias` | `:177-274` |
| `Query(alias="p_alias", validation_alias="p_val_alias")` | **`p_val_alias` only** — `?p_alias=…` → **422 missing** `loc:["query","p_val_alias"]` | `p_val_alias` | `:281-415` |

Key conclusions:
- When **both** `alias` and `validation_alias` are set, **`validation_alias` wins for reading**, and
  it is also what appears in `loc` and the OpenAPI parameter `name` (`:306-316,382-400`). `alias` in
  that case only affects serialization.
- The bare param name is **never** accepted once any alias is set (`:140-157` `?p=hello` → 422).
- This holds **identically** whether the param is a bare scalar (`Annotated[str, Query(alias=…)]`) or
  a **Pydantic model bound to `Query()`** whose field uses `Field(alias=…)` — the test parametrizes
  both `/required-alias` and `/model-required-alias` against the same assertions
  (`:96-99,113-115`). **Lancelot MUST** apply alias resolution to model-bound query/header/cookie
  params too, by delegating to pydantic-core's `validate_python(..., from_attributes=True)` on the
  collected dict (`utils.py:847-850`).

### 2.3 `Annotated` resolution rules (KNOWN — `utils.py:411-443` + `test_invalid_path_param.py`,
`test_ambiguous_params.py`)

- `Annotated[T, marker]`: `T` is the real type (`get_args(...)[0]`); among the metadata args, only
  `FieldInfo`/`params.Depends` instances are FastAPI markers; **the LAST `Param/Body/Depends` marker
  wins** (`utils.py:419-436` `fastapi_specific[-1]`). `test_ambiguous_params.py:37-38` shows
  `Annotated[int, Query(gt=2), Query(lt=10)]` — both constraints apply (FastAPI copies the field
  info; in practice the constraints merge through the metadata; the test asserts `foo=5`→200,
  `foo=123`→422, `foo=1`→422).
- **Default goes on the `=`, not in `Annotated`.** `Annotated[int, Query(default=1)]` →
  `AssertionError` "`Query` default value cannot be set in `Annotated`…" (`test_invalid_path_param.py:20-30`).
- **Cannot combine `Depends` in `Annotated` and as default:** `Annotated[int, Depends(dep)] =
  Depends(dep)` → `AssertionError` "Cannot specify `Depends` in `Annotated` and default value
  together for 'foo'" (`test_ambiguous_params.py:41-51`).
- **Cannot combine a FastAPI annotation marker with a `Depends()` default:**
  `Annotated[int, Query(min_length=1)] = Depends(dep)` → `AssertionError` "Cannot specify a FastAPI
  annotation in `Annotated` and `Depends` as a default value together for 'foo'"
  (`test_ambiguous_params.py:53-63`).
- Repeated alias across two *different* sources is allowed: `path: str = Path(alias="repeated_alias")`
  + `query: str = Query(alias="repeated_alias")` coexist; the path value comes from the URL segment,
  the query value from `?repeated_alias=` (`test_repeated_parameter_alias.py:8-22`). Two OpenAPI
  params with the same `name` but different `in` are emitted.

> **Lancelot MUST (18):** `validation_alias` (or `alias` if no validation_alias) is the **only** key
> used to read query/header/cookie/form values; the bare param name is rejected when an alias is set;
> `loc` and OpenAPI `name` use that same alias. (`test_request_params/test_query/test_required_str.py:140-157,306-316`)
> **Lancelot MUST (19):** When both `alias` and `validation_alias` are present, read by
> `validation_alias`; `alias` then governs only serialization. (`…/test_required_str.py:382-400`)
> **Lancelot MUST (20):** Apply alias resolution to Pydantic-model-bound params identically to bare
> scalars (delegate to pydantic-core with `from_attributes=True` over the collected multidict).
> (`…/test_required_str.py:91-99`, `utils.py:798-850`)
> **Lancelot MUST (21):** In `Annotated[T, m1, m2,…]` the **last** `Param/Body/Depends` marker wins;
> `T = get_args[0]`; non-FieldInfo metadata is ignored as a marker. (`utils.py:419-436`,
> `test_ambiguous_params.py:37`)
> **Lancelot MUST (22):** Raise `AssertionError` at registration for: default-in-`Annotated`;
> `Depends` both in `Annotated` and as default; FastAPI marker in `Annotated` with `Depends` default
> — with the exact messages. (`test_invalid_path_param.py:20-30`, `test_ambiguous_params.py:41-63`)
> **Lancelot MUST (23):** Allow the same alias on params of different `in` sources (path+query) and
> emit both OpenAPI params. (`test_repeated_parameter_alias.py:8-22`)

### 2.4 Header `convert_underscores` (KNOWN — `params.py:316,352`, `utils.py:797-828`)

`Header(convert_underscores=True)` (default): a param `user_agent: str = Header()` reads the HTTP
header `user-agent` (underscores → hyphens) — `utils.py:822-824`
(`alias.replace("_","-")`). For a Pydantic model bound to `Header()`, the model-level
`Header(convert_underscores=False)` disables it (`utils.py:803-807`). `Header(convert_underscores=
False)` keeps the literal underscore name. Header lookups are case-insensitive (Starlette `Headers`).

> **Lancelot MUST (24):** For `Header` params, default behavior maps `snake_case` param name to
> `kebab-case` header (only when no explicit alias is set: `if alias == field.name`), case-insensitive;
> `convert_underscores=False` disables the rewrite. (`utils.py:818-824`, `test_tutorial/test_header_params/test_tutorial001.py`)

---

## 3. Body vs query embedding; form / file / multipart rules & errors

### 3.1 The embed decision (KNOWN — `utils.py:888-909`, `_should_embed_body_fields`)

```python
def _should_embed_body_fields(fields):
    if not fields: return False
    body_param_names_set = {f.name for f in fields}
    if len(body_param_names_set) > 1: return True            # ≥2 distinct body params → embed
    first = fields[0]
    if getattr(first.field_info, "embed", None): return True # Body(embed=True) → embed
    if (isinstance(first.field_info, params.Form)
        and not lenient_issubclass(first.field_info.annotation, BaseModel)
        and not is_union_of_base_models(first.field_info.annotation)):
        return True                                          # bare-scalar Form → embed
    return False
```

Consequences (the contract):

- **Single body param, a Pydantic model, no `embed`** → the request JSON body **is** that model:
  `POST /items {"name":"Foo","price":50.5}` → `Item(name=…,price=…)`. Missing field →
  `loc:["body","price"]`, `input` = the partial object (`test_tutorial/test_body/test_tutorial001.py:73-85`).
  This is `single_not_embedded_field` path in `request_body_to_args` (`utils.py:959,975-980`):
  `loc=("body",)`, validate the whole body against the one field.
- **Single body param, a scalar, `Body(embed=True)`** → body must be `{"<name>": value}`:
  `b: str|None = Body(embed=True)` → `POST {"b":"foo"}` ok; `POST {}`/`POST {"invalid":…}` → 422
  (`test_required_noneable.py:17-19,47-60`).
- **≥2 body params** → each is a top-level key in one JSON object; `Body_<opid>` model is synthesized
  (`utils.py:1019-1052`). `PUT /items/5 {"item":{…},"user":{…},"importance":2}`; `json=None` →
  three `missing` errors `loc:["body","item"]`,`["body","user"]`,`["body","importance"]` with
  `input:null`, in **declared order** (`test_tutorial/test_body_multiple_params/test_tutorial003.py`).
- **Body + query/path coexist:** path/query come from URL, body from JSON.
  `PUT /items/{item_id}?q=bar` with JSON `Item` → `item_id` path, `q` query, `Item` body
  (`test_tutorial/test_body_multiple_params/test_tutorial001.py:24-36`). `json=None` with optional
  body model → handler omits it (`{"item_id":5,"q":"bar"}`) (`:39-48`); bad path → only the path
  `int_parsing` error (`:51-63`).
- A non-body sub-dependency that itself declares a body param contributes that param to the **same**
  combined body model: `Depends(dep_with_body_model)` → its `item2` shows as `loc:["body","item2"]`
  (`test_dependency_duplicates.py:33-54`). Two deps requiring the **same** model param dedupe by name
  (one body field, validated once) — `test_dependency_duplicates.py` `with-duplicates`.

### 3.2 Body container / content-type rules (KNOWN — `routing.py:401-449`)

- Body is read only if the route has body params (`if body_field:`). Form fields → `await
  request.form()` and the form is closed via the exit stack (`routing.py:405-407`).
- JSON path: `body_bytes = await request.body()`. If empty → `body=None` (so a required body model →
  `missing`). If non-empty:
  - No `Content-Type` header: parse as JSON **only if `strict_content_type` is False**
    (`routing.py:413-415`). With strict (default `True`) and no content-type, the bytes are passed
    as raw `body_bytes` (→ validation against a model fails → 422, since a dict is expected).
  - With `Content-Type`: parsed as JSON iff maintype `application` and subtype `json` or `*+json`
    (`routing.py:417-422`). Otherwise raw bytes.
- `json.JSONDecodeError` → **422** `json_invalid` (see §0.3).
- Any other body-read exception → **400** `{"detail":"There was an error parsing the body"}`
  (`routing.py:445-449`). An `HTTPException` raised during body read is re-raised as-is
  (`routing.py:442-444`).
- `GET` with a JSON body is allowed (`test_get_request_body.py:22-25`); body semantics do not depend
  on method.
- The `single_not_embedded_field` body path: if the JSON root is the **wrong container** (e.g. a list
  when a dict-model is expected), `request_body_to_args` catches `AttributeError` on `.get()` and
  appends `get_missing_field_error(loc)` per field (`utils.py:984-990`).

### 3.3 Form / multipart (KNOWN — `utils.py:912-948`, `test_forms_*`, `test_multipart_installation.py`)

- A bare scalar `Form()` param is **embedded** (form key = param name): `username: Annotated[str,
  Form()]` → `data={"username":"Rick"}` → `"Rick"`; OpenAPI uses
  `application/x-www-form-urlencoded` and a `Body_<opid>` schema (`test_forms_single_param.py:10-73`).
- A Pydantic model bound to `Form()` is **not** embedded — form keys map to model fields directly;
  model defaults and `Field(alias=…)` apply; `model_config={"extra":"allow"}` keeps unknown form
  keys; repeated form key → list for a `list[...]` field (`test_forms_single_model.py` whole file,
  `utils.py:965-973`). Missing required model fields → multiple `missing` errors with
  `loc:["body","<field>"]` and `input` = the partial form dict
  (`test_forms_single_model.py:93-111`).
- File: `f: UploadFile = File()` → `UploadFile`; `f: bytes = File()` → the file is `await
  value.read()` into bytes (`utils.py:921-926`); `list[bytes]`/`list[UploadFile]` → each read
  (`utils.py:927-937`, `serialize_sequence_value`). Form `""` for a **required** field counts as
  **missing** (`utils.py:768-771`: empty string on a `Form` field → required → missing).
- Form + File can be mixed in one multipart request (`test_multipart_installation.py:63-73`).
- **python-multipart dependency gating (registration-time):**
  - not installed at all → `RuntimeError(multipart_not_installed_error)` raised when a route with
    Form/File is registered (`test_multipart_installation.py:76-138`).
  - the wrong `multipart` package installed (no `parse_options_header`) →
    `RuntimeError(multipart_incorrect_install_error)` (`:11-73`).
  - These fire at `@app.post(...)` decoration, **fail-fast**, not per request.

> **Lancelot MUST (25):** Single non-embed body param (model) consumes the **entire** JSON body;
> `loc=("body",)` for whole-body validation, `loc=("body",<field>)` for its sub-fields; missing
> sub-field `input` = the partial parsed object. (`test_tutorial/test_body/test_tutorial001.py:73-85`,
> `utils.py:975-980`)
> **Lancelot MUST (26):** Embed (key-inside-object) iff ≥2 distinct body params OR `Body(embed=True)`
> OR a bare-scalar `Form` param; synthesize a `Body_<operationId>` model for the combined case.
> (`utils.py:888-909`, `test_required_noneable.py:47-60`)
> **Lancelot MUST (27):** Multiple missing embedded body keys produce one `missing` error each, in
> declared order, `input:null` when body is `null`. (`test_tutorial/test_body_multiple_params/test_tutorial003.py`)
> **Lancelot MUST (28):** Content-type gate: parse JSON only for `application/json` or `*+json`
> (or any non-empty body when `strict_content_type=False` and no content-type); JSON syntax error →
> 422 `json_invalid`; other parse failure → 400 `"There was an error parsing the body"`;
> `HTTPException` during body read propagates unchanged. (`routing.py:413-449`)
> **Lancelot MUST (29):** Pydantic-model-bound `Form()` maps keys to fields (defaults, alias,
> `extra:"allow"`, repeated→list); bare-scalar `Form()` embeds under the param name; empty-string
> form value for a required field = `missing`. (`test_forms_single_model.py`, `utils.py:768-771`)
> **Lancelot MUST (30):** `bytes`/`list[bytes]` File params are `.read()` to bytes;
> `UploadFile`/`list[UploadFile]` passed through; the form is closed after the response
> (exit-stack). (`utils.py:912-948`, `routing.py:405-407`)
> **Lancelot MUST (31):** Missing multipart support → `RuntimeError` at **route registration**
> (fail-fast), distinct messages for not-installed vs wrong-package. (`test_multipart_installation.py`)
> **Lancelot MUST (32):** Sub-dependency body params merge into the combined body model and dedupe by
> name; their errors carry `loc:["body","<param>"]`. (`test_dependency_duplicates.py:33-54`)

---

## 4. Dependency injection — resolution order, caching, overrides

### 4.1 `Depends` / `Security` surface (KNOWN — `params.py:745-755`)

```python
@dataclass(frozen=True)
class Depends:
    dependency: Callable[..., Any] | None = None
    use_cache: bool = True
    scope: Literal["function","request"] | None = None

@dataclass(frozen=True)
class Security(Depends):
    scopes: Sequence[str] | None = None
```

- `Depends()` with no arg → the **annotation type is the callable** (class used as its own factory):
  `instance: CallableDependency = Depends()` constructs `CallableDependency()`
  (`test_dependency_class.py:51-55`). A class dependency's `__init__`/`__call__` params are
  themselves resolved (e.g. `value: str` becomes a required query param)
  (`test_dependency_class.py:10-12,51-56`).
- Any callable kind works as a dependency: plain func, `async def`, sync generator (`yield`),
  async generator, class, callable instance, bound method, `functools.partial`, classmethod —
  all 12 combinations pass (`test_dependency_class.py:134-155`). The callable-kind is detected once
  at registration (`models.py:105-185`).

### 4.2 Sub-dependency resolution order (KNOWN — `test_dependency_cache.py`,
`test_dependency_contextmanager.py`)

- Resolution is **depth-first, declared order**. A dep's own sub-deps resolve before the dep runs.
- Route/decorator/app-level `dependencies=[Depends(x)]` resolve **before** the endpoint, in declared
  order; they are parameterless (`name=None`) so their return is computed for side-effects but **not
  injected** (`test_dependency_yield_scope.py:19-31`, prior teardown §1.1). Their validation errors
  still surface (`test_dependency_overrides.py:84-96` decorator-dep missing `q` → 422).
- Multi-level chain `context_a → context_b → endpoint`: setup order a, b; teardown order **b then
  a** (LIFO) — `context_b` finalizer sees `context_a` still "started a"
  (`test_dependency_contextmanager.py:73-86,265-271`).

### 4.3 Caching / `use_cache` (KNOWN — `test_dependency_cache.py:54-92`, `models.py:62-71`)

Cache key = `(call, sorted(set(scopes)) if _uses_scopes else (), computed_scope or "")`. Per-request
memo dict keyed by this. Tests pin the exact counting:

- `/counter/` calls `dep_counter` once → `{"counter":1}`; second request → `{"counter":2}` (counter
  is process-global; cache is per-request) (`:54-61`).
- `/sub-counter/`: endpoint depends on `super_dep`(→`dep_counter`) **and** `dep_counter` directly.
  Both share one cache entry → `dep_counter` called **once** → `{"counter":1,"subcounter":1}`
  (`:64-71`).
- `/sub-counter-no-cache/`: the direct `Depends(dep_counter, use_cache=False)` is **not** cached, so
  `dep_counter` runs twice that request (once for `super_dep`'s cached copy, once fresh) →
  `{"counter":2,"subcounter":1}`, next request `{"counter":4,"subcounter":3}` (`:74-81`).
- `/scope-counter/`: `Security(dep_counter)` (no scopes) vs `Security(dep_counter, scopes=["scope"])`
  ×2. The two scoped ones **share** a cache slot (same callable + same sorted scopes), the unscoped
  one is separate → `{"counter":1,"scope_counter_1":2,"scope_counter_2":2}` (3 distinct executions
  total, scoped ones cached together) (`:84-91`). This proves the scope component of the cache key.

> **Lancelot MUST (33):** Default `use_cache=True`: a dependency callable runs **once per request**
> and its result is shared by every dependant in the graph; key = `(callable, sorted scopes if
> scope-aware else (), computed_scope or "")`. (`test_dependency_cache.py:64-71`, `models.py:62-71`)
> **Lancelot MUST (34):** `use_cache=False` on a `Depends` → that edge re-invokes the callable every
> time it's needed, not memoized; other (cached) edges to the same callable still share. (`:74-81`)
> **Lancelot MUST (35):** `Security(dep, scopes=A)` and `Security(dep, scopes=B)` are distinct cache
> entries iff the dep is scope-aware; identical sorted scope sets share. Scopes are
> `sorted(set(...))`. (`test_dependency_cache.py:84-91`, `models.py:62-71`)
> **Lancelot MUST (36):** Sub-deps resolve depth-first in declared order; yield-dep teardown is LIFO
> (reverse of setup); route/app-level `dependencies=[…]` run before the endpoint, are not injected,
> but their errors still 422. (`test_dependency_contextmanager.py:265-271`,
> `test_dependency_overrides.py:84-96`)
> **Lancelot MUST (37):** `Depends()` with no arg uses the param's annotation as the callable;
> a class dependency's constructor/`__call__` params are themselves resolved as params.
> (`test_dependency_class.py:51-56`)

### 4.4 `dependency_overrides` (KNOWN — `test_dependency_overrides.py`)

`app.dependency_overrides` is a mutable dict `{original_callable: replacement_callable}`.

- When set, the resolver substitutes the replacement and **re-derives the sub-graph from the
  replacement's signature** — the replacement's parameters are validated, the original's are not.
  `common_parameters(q:str,…)` overridden by `overrider_dependency_simple(q:str|None=None)`:
  `/main-depends/` (no `q`) now returns **200** with the overrider's defaults
  (`{"q":None,"skip":5,"limit":10}`) instead of 422 (`:208-213,171-188`).
- Override works for **main `Depends()`**, **decorator/route `dependencies=[…]`**, and **router-
  included** routes identically (`:189-205` parametrized over all four).
- Override with a replacement that has its **own sub-dependency**: the sub-dep's params become
  required. `overrider_dependency_with_sub(msg=Depends(overrider_sub_dependency))` where
  `overrider_sub_dependency(k:str)` → `/main-depends/` now needs `?k=bar`; without it → 422 missing
  `loc:["query","k"]` (`:216-256`).
- `app.dependency_overrides = {}` (or per-key delete) restores original behavior. The tests reset
  after each case.
- Security overrides: same mechanism keyed by the security callable
  (`test_dependency_security_overrides.py`); the SecurityScopes are still propagated.

> **Lancelot MUST (38):** `dependency_overrides[orig]=repl` swaps the callable **and** recomputes its
> dependency sub-tree from `repl`'s signature (validate repl's params, not orig's); applies to main,
> decorator, router-level, and security deps uniformly. (`test_dependency_overrides.py:208-256`)
> **Lancelot MUST (39):** Overrides are dynamic (read per request from a mutable dict); empty dict =
> no overrides; the override must be re-resolvable each request (do not freeze the plan against the
> original callable when an override is present). (`test_dependency_overrides.py:208-213` reset
> pattern; prior teardown §0.7 perf note)
> **Lancelot MUST (40):** An overriding dep's own sub-deps add their params (a missing one → 422
> with the sub-dep param's `loc`). (`test_dependency_overrides.py:216-229`)

### 4.5 Special framework injections (KNOWN — prior teardown §1.1, `utils.py:713-728`)

These parameter **types** are auto-injected by type (never validated as params): `Request`,
`WebSocket`, `HTTPConnection`, `Response`, `BackgroundTasks`, `SecurityScopes`. Detected at
registration (`add_non_field_param_to_dependency`, `utils.py:362-383`); per request it's a single
binding. `SecurityScopes.scopes` = ordered, deduped union of ancestor `Security(scopes=…)`
(`test_dependency_paramless.py:15-25,54-57` → `["a","b"]`; order preserved, prior teardown §1.2
`oauth_scopes`). A fresh `Response` object is created per request and passed where requested
(prior teardown §0.4(e)).

> **Lancelot MUST (41):** Inject `Request`/`WebSocket`/`HTTPConnection`/`Response`/`BackgroundTasks`/
> `SecurityScopes` by parameter **type** without validating them as request params; `SecurityScopes
> .scopes` = order-preserving deduped union of all ancestor `Security(scopes=…)`.
> (`test_dependency_paramless.py:54-65`, `utils.py:713-728`)

---

## 5. `yield`-dependency teardown — success, exception, streaming (the hardest part)

This is the most subtle subsystem and the one Lancelot must not get wrong (PEP-789 / SSE lifetime).

### 5.1 Scope semantics (KNOWN — `test_dependency_yield_scope.py`)

`Depends(dep, scope=...)`. `computed_scope` (`models.py:187-193`): explicit `scope` wins; else any
yield dep defaults to `"request"`; non-yield dep → `None`.

- **`scope="request"`** (default for yield deps): the post-`yield` teardown runs **after the
  response/stream is fully sent**. So a `StreamingResponse` body generator that reads the
  session still sees it **open**: `/request-scope` → `{"is_open": True}`
  (`test_dependency_yield_scope.py:87-92,159-163`).
- **`scope="function"`**: teardown runs **before the response is sent** (right after the endpoint
  function returns, before body streaming). The streaming body sees it **closed**:
  `/function-scope` → `{"is_open": False}` (`:79-84,152-156`).
- Mixed: `/two-scopes` → `{"func_is_open": False, "req_is_open": True}` (`:95-104,166-171`).
- Nested: a request-scope yield dep depending on another → both stay open through the stream
  (`/sub` → both `True`, `:107-114,174-179`). A function-scope dep wrapping a request param: the
  request param stays open until response sent, the function-scoped wrapper closes early
  (`/named-function-scope` both False after-the-fact; `/regular-function-scope` shows the non-yield
  wrapper does not extend lifetime) (`:117-134,193-206`).

### 5.2 The registration-time scope guard (KNOWN — `:182-190`, `utils.py:319-329`)

A `scope="request"` yield dep that declares a dependency with `scope="function"` is a lifetime
violation (the function-scoped resource would be torn down before the request-scoped one finishes).
Raised **at route registration / import** (fail-fast):

```
FastAPIError: The dependency "get_named_func_session" has a scope of "request",
it cannot depend on dependencies with scope "function"
```

(`test_dependency_yield_scope.py:182-190`). It fires when the decorator runs, before any request.

### 5.3 Exception propagation into the generator (KNOWN — `test_dependency_contextmanager.py`,
`test_dependency_after_yield_raise.py`, `test_dependency_yield_except_httpexception.py`)

When the endpoint (or a later dep) raises, the exception is **thrown back into each yield dep at its
`yield`** (standard contextmanager semantics), LIFO:

- `try/except SpecificError: errors.append(...); raise` + `finally:` — the `except` *does* see the
  matching error, re-raises it, `finally` always runs. `/async_raise` raising `AsyncDependencyError`
  → dep's `except AsyncDependencyError` records it, re-raises; state → `"finalized"`; the client gets
  the propagated error (test asserts `pytest.raises(AsyncDependencyError)`), or **500** with
  `raise_server_exceptions=False` (`test_dependency_contextmanager.py:51-59,99-108,248-262`).
- Endpoint raises `OtherDependencyError` that the dep's `except SpecificError` does **not** catch:
  the `finally` still runs (state finalized), the error is **not** recorded, and it propagates as
  500 / re-raises (`:111-120,232-245,327-338`).
- A yield dep can **translate** an exception: `except CustomError as err: raise HTTPException(418,
  …) from err` → response is **418** `{"detail":"Session error"}` (the HTTPException raised from
  inside teardown becomes the response) (`test_dependency_after_yield_raise.py:12-16,40-43`).
- `except HTTPException: state["except"]=True; raise` then `finally` — DB-rollback pattern: endpoint
  `raise HTTPException(400)`; dep sees it in `except`, the post-yield `fake_database.update` is
  **skipped** (rollback), `finally` runs, response is the **400** `{"detail":"Invalid user"}`
  (`test_dependency_yield_except_httpexception.py:16-39,53-61`). On success path the `except` is not
  hit, `finally` runs, the update commits, 200 (`:64-71`).

### 5.4 Raise *after* yield with no active exception (KNOWN —
`test_dependency_after_yield_raise.py:19-22,46-68`)

`def broken_dep(): yield "s"; raise ValueError(...)` — the endpoint returns normally; the post-yield
`raise` happens **during teardown, after the response is already prepared**:
- With `raise_server_exceptions=True` (TestClient default) the error surfaces to the test
  (`pytest.raises(ValueError)`).
- With `raise_server_exceptions=False`: the client gets the **already-sent 200** body
  (`{"message":"all good?"}`) and the server logs/captures the error afterward (`:51-68`).
This confirms: **the response is committed before request-scope teardown runs**; a teardown error
does not retro-actively change an already-sent response.

### 5.5 Teardown error during/after a streaming response (KNOWN —
`test_dependency_contextmanager.py` broken-session, `test_dependency_after_yield_streaming.py`)

- `test_dependency_after_yield_streaming.py`: a context-managed session yielded as a dep; the
  `StreamingResponse` iterates it. `scope` default (request) → the generator streams fine
  (`/stream-session` → `"foobarbaz"`), session closed only after stream ends (`:64-69,98-101`).
- `broken_dep_session` closes the session **before** yield → iterating it mid-stream raises
  `ValueError("Session closed")`. Non-stream path → 500 / raises (`:103-112`). Stream path: the
  **200 + headers are already sent**, so the client sees status 200 with a **truncated/empty body**
  and the server raises/captures afterward (`:115-130`). This is the PEP-789 boundary: the keepalive/
  channel wraps the *consumer*, the generator's own teardown errors cannot rewrite the status line.

### 5.6 Background tasks vs teardown ordering (KNOWN —
`test_dependency_contextmanager.py:135-142,281-293`)

`BackgroundTasks` added in the endpoint run **after** the response is sent but their relation to
yield-teardown is pinned: in `/context_b_bg`, at response time `state["bg"]=="not set"` and the
middleware (which wraps the response) also sees `"not set"`; **after** everything, `state["bg"]` is
set and `state["context_b"]=="finished b with a: started a"` — i.e. request-scope dep teardown and
background tasks both run post-response; the test asserts the dep finalizers ran with the expected
LIFO state. (FastAPI's known bug — bg tasks run sequentially inside the request coroutine after body
send — is *not* something Lancelot should copy; see SYNTHESIS §3. But the **observable state
ordering** `dep-teardown sees pre-bg state` must hold for these tests.)

### 5.7 WebSocket yield-scope (KNOWN — `test_dependency_yield_scope_websockets.py`,
`test_dependency_after_yield_websockets.py`)

Same scope rules for WS: request-scope yield deps stay open for the WS session duration, function-
scope close after the handler returns; teardown LIFO; exceptions thrown into the generator.

> **Lancelot MUST (42):** `scope="request"` yield deps tear down **after** the full response/stream
> is sent; `scope="function"` tear down **before** the response is sent; default scope for any yield
> dep is `"request"`. (`test_dependency_yield_scope.py:152-171`)
> **Lancelot MUST (43):** Teardown order is strict LIFO (reverse of setup); a later dep's finalizer
> observes earlier deps not yet finalized. (`test_dependency_contextmanager.py:265-271`)
> **Lancelot MUST (44):** On request failure, throw the exception into each yield dep **at its
> `yield`** (so `try/except/finally` around `yield` works); a dep may translate it (e.g. raise
> `HTTPException(418)`) and that becomes the response; `finally` always runs.
> (`test_dependency_after_yield_raise.py:40-43`, `test_dependency_yield_except_httpexception.py:53-61`)
> **Lancelot MUST (45):** Raise the registration-time `FastAPIError` (exact message) when a
> `scope="request"` dep depends on a `scope="function"` dep — at import, before any request.
> (`test_dependency_yield_scope.py:182-190`)
> **Lancelot MUST (46):** The response status/headers are committed before request-scope teardown;
> a post-`yield` teardown error must NOT rewrite an already-sent response — it surfaces as a server-
> side error (raise with `raise_server_exceptions=True`, logged 200/empty-body otherwise).
> (`test_dependency_after_yield_raise.py:51-68`, `test_dependency_contextmanager.py` broken-stream)
> **Lancelot MUST (47):** A streaming-response body generator may read a request-scope yield dep's
> resource for the entire stream (resource stays open); the resource closes only after the last byte.
> (`test_dependency_yield_scope.py:159-163`, `test_dependency_after_yield_streaming.py:98-101`)
> **Lancelot MUST (48):** Mid-stream teardown/iteration errors cannot change the already-sent 200;
> client sees truncated/empty body, server captures the error. (`test_dependency_contextmanager.py:121-130`)
> **Lancelot MUST (49):** Same scope/teardown/throw rules for WebSocket-scoped yield deps.
> (`test_dependency_yield_scope_websockets.py`, `test_dependency_after_yield_websockets.py`)

---

## 6. `response_model` (output validation/serialization contract)

(KNOWN — `test_validate_response.py`, `test_serialize_response_model.py`)

- `response_model=Item` validates **and re-serializes** the handler's return through a per-route
  output `ModelField` (pydantic-core `dump_python`/`dump_json`). Invalid return → **raises
  `ResponseValidationError`** (a server-side error → 500, NOT 422; the client never controls it)
  (`test_validate_response.py:51-58,75-82`).
- Output serialization is **by alias by default** (`response_model_by_alias=True`): `Field(alias=
  "aliased_name")` → JSON key `aliased_name` (`test_serialize_response_model.py:7-15`). Type coercion
  applies on output too (`price="1.0"` → `1.0`).
- `response_model=Item | None` allows `None` → JSON `null` (200); plain `response_model=Item`
  returning `None` → `ResponseValidationError` (`test_validate_response.py:21-31,56-72`).
- Flags: `response_model_exclude_unset`, `_exclude_defaults`, `_exclude_none`, `_include`/`_exclude`,
  `_by_alias` map to pydantic-core dump params (`_compat/v2.py:190-213`); each is honored per route
  (`test_serialize_response_model.py:40-66`, `test_response_model_include_exclude.py`).
- The default response (no explicit `response_class`) uses pydantic-core `dump_json` → bytes
  directly and **never calls `json.dumps`** (one Rust pass); an explicit `JSONResponse` goes through
  `json.dumps` (`test_dump_json_fast_path.py:30-51`). Lancelot keeps the single-pass typed path.
- `response_model` schema is split input/output in OpenAPI when they differ; nested model filtering
  drops fields not on the response model (`test_response_model_data_filter.py`,
  `test_filter_pydantic_sub_model_pv2.py`).

> **Lancelot MUST (50):** `response_model` validates+reserializes the return via pydantic-core;
> failure raises `ResponseValidationError` → 500 (server fault), not a 422. (`test_validate_response.py:51-58`)
> **Lancelot MUST (51):** Output serialization is by-alias by default and honors
> `exclude_unset/defaults/none`, `include/exclude`, `by_alias` per route; `Optional` response models
> permit `null`. (`test_serialize_response_model.py`, `test_validate_response.py:61-72`)
> **Lancelot MUST (52):** Default response = single-pass `dump_json` to bytes (no `json.dumps`);
> explicit `JSONResponse` uses the json.dumps path. (`test_dump_json_fast_path.py:30-51`)

---

## 7. Registration-time (import-time) guards — must fail fast, before any request

Consolidated list of conditions that must raise at `@app.<verb>(...)`/`add_api_route` time, never as
a runtime 500/422:

| condition | exception | message (verbatim / match) | evidence |
|---|---|---|---|
| `Path(default=…)` set | `AssertionError` | `Path parameters cannot have a default value` | `params.py:185`, `test_invalid_path_param.py:11-18` |
| default inside `Annotated` | `AssertionError` | `` `Query` default value cannot be set in `Annotated` for 'X'. Set the default value with `=` instead.`` | `test_invalid_path_param.py:20-30` |
| `Depends` in `Annotated` **and** as default | `AssertionError` | `Cannot specify `Depends` in `Annotated` and default value together for 'X'` | `test_ambiguous_params.py:41-51` |
| FastAPI marker in `Annotated` + `Depends` default | `AssertionError` | `Cannot specify a FastAPI annotation in `Annotated` and `Depends` as a default value together for 'X'` | `test_ambiguous_params.py:53-63` |
| non-scalar query/path param type (`list[Model]`, `dict`, …) | `AssertionError` | query: `Query parameter 'q' must be one of the supported types`; path: bare `AssertionError` | `test_invalid_sequence_param.py`, `test_invalid_path_param.py:6-15`, `utils.py:549-555` |
| `scope="request"` dep → `scope="function"` dep | `FastAPIError` (subclass `DependencyScopeError`) | `The dependency "X" has a scope of "request", it cannot depend on dependencies with scope "function"` | `test_dependency_yield_scope.py:182-190`, `utils.py:319-329` |
| Form/File param but python-multipart missing | `RuntimeError` | `multipart_not_installed_error` | `test_multipart_installation.py:76-138` |
| Form/File param but wrong `multipart` pkg | `RuntimeError` | `multipart_incorrect_install_error` | `test_multipart_installation.py:11-73` |

> **Lancelot MUST (53):** All eight conditions above raise the specified exception **at route
> registration / import**, with byte-matching messages — never deferred to request time.

---

## 8. The nastiest edge cases (where naive reimplementations break)

1. **`q: str | None` with no default is REQUIRED.** Type-Optional does not imply optional; only a
   default does. A reimpl that maps `Optional[T]` → not-required will pass 90% of tests and fail
   `test_required_noneable.py`. (Rule 9.)
2. **`validation_alias` beats `alias` for reading, AND becomes the `loc`/OpenAPI name; the bare param
   name is rejected the instant any alias is set.** Both-aliases-set is the trap: only
   `p_val_alias` works, `?p=…` and `?p_alias=…` both 422 with `loc:["query","p_val_alias"]`.
   (Rules 18-20; `test_request_params/test_query/test_required_str.py:281-415`.)
3. **`input` in the 422 differs by call site:** `null` for missing bare scalar, `{}` for missing
   model-bound param, the **partial parsed object** for a missing body sub-field, the raw value for a
   coercion failure — and constraint `ctx` numbers carry the field's numeric type (`{"gt":3.0}` vs
   `{"gt":3}`). Getting `input`/`ctx` wrong fails dozens of exact-equality assertions. (Rules 3,5,13.)
4. **Single non-embed body model = the whole JSON body; ≥2 body params / `embed=True` / bare Form =
   keyed-inside.** Same handler shape, completely different wire body and different `loc` (`("body",)`
   vs `("body","name")`). Sub-dependency body params silently merge into the combined model. (Rules
   25-26,32.)
5. **yield-dep teardown timing is scope-dependent and the response is committed before request-scope
   teardown:** `scope="request"` (default) closes **after** the stream; `scope="function"` closes
   **before** the response; teardown is LIFO; exceptions are thrown back *into* the generator at
   `yield` (so a dep can convert an error into a 418 response); a post-yield error cannot rewrite an
   already-sent 200. This is the PEP-789 SSE-lifetime correctness core — Lancelot's whole AI moat
   depends on getting exactly this right. (Rules 42-49; `test_dependency_yield_scope.py`,
   `test_dependency_contextmanager.py`, `test_dependency_after_yield_streaming.py`.)

---

## 9. Consolidated conformance checklist (Lancelot MUST 1–53)

All 53 numbered **Lancelot MUST** rules above form the executable conformance suite. Grouped:

- **422 wire shape:** 1-8 (envelope, loc, ctx, type/msg strings, input semantics, json_invalid vs
  400, WS close-1008, error accumulation/order).
- **Required/optional/coercion:** 9-17 (default decides required-ness; deepcopy defaults; pydantic-
  core coercion; int-not-float-truncate; ctx numeric typing; sequence getlist; reg-time type guard;
  Undefined sentinel).
- **Alias / Annotated / Header:** 18-24 (validation_alias precedence, model-bound parity,
  last-marker-wins, ambiguity AssertionErrors, cross-source repeated alias, convert_underscores).
- **Body/Form/File:** 25-32 (single-vs-embed, combined Body_<opid> model, content-type gate,
  json_invalid/400, model-Form mapping, bytes read, multipart fail-fast, sub-dep body merge).
- **DI order/cache/overrides:** 33-41 (use_cache key, no-cache, scope cache split, DFS order + LIFO
  teardown, Depends()-no-arg, override re-derivation, dynamic overrides, special injections).
- **yield teardown:** 42-49 (request vs function scope timing, LIFO, throw-into-generator,
  reg-time scope guard, response-committed-before-teardown, streaming resource lifetime, mid-stream
  error can't rewrite 200, WS parity).
- **response_model:** 50-52 (ResponseValidationError→500, by-alias+flags, single-pass dump_json).
- **Registration guards:** 53 (eight import-time fail-fast conditions with exact messages).

**Calibration plan:** point a Lancelot `TestClient` at the *same* test files
(`fastapi/fastapi/tests/`). The pydantic-core-derived assertions (`type`/`msg`/`ctx`) require
Lancelot to FFI into the *same pydantic-core version* the original used — pin it. Any divergence in
422 bodies is a hard fail; run `test_path.py`, `test_query.py`, `test_request_params/**`,
`test_dependency_*`, `test_multi_*_errors.py`, `test_forms_*`, `test_validate_response.py` as the
gate. A green run on these = "stable like FastAPI" for this surface.

*Compiled 2026-05-19 from line-by-line read of the FastAPI 0.136.x test suite + `dependencies/`,
`params.py`, `routing.py`, `exception_handlers.py`, `exceptions.py`, `_compat/v2.py`. `Lancelot/` is
gitignored.*
