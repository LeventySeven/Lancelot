# FastAPI Dependency Injection — Full Source RE for Lancelot

**Target:** FastAPI 0.136.x (local checkout at `/Users/seventyleven/Desktop/researchfms/fastapi/fastapi/fastapi`).
**Goal:** reimplement FastAPI's DI *faster* in Lancelot (Rust core + Python ergonomics), by compiling the
resolution graph once and eliminating per-request Python reflection.
**Method:** every file in scope read line-by-line. Every claim cites `path:line`. Labels: **KNOWN** = read
directly from source; **INFERRED** = deduced from code behavior; **DESIGNED** = Lancelot proposal.

Files dissected:
- `fastapi/dependencies/models.py` (194 L) — the `Dependant` DAG node + cached graph metadata.
- `fastapi/dependencies/utils.py` (1058 L) — the compiler (`get_dependant`) + the per-request resolver (`solve_dependencies`).
- `fastapi/params.py` (754 L) — `Param/Query/Path/Header/Cookie/Body/Form/File/Depends/Security` FieldInfo subclasses.
- `fastapi/param_functions.py` (2460 L) — thin `Annotated`-friendly constructor wrappers (90% docstrings).
- `fastapi/security/*` (base, utils, api_key, http, oauth2, open_id_connect_url) — security deps are just callables that are `SecurityBase` instances.
- `fastapi/routing.py` (4956 L) — call sites: `APIRoute.__init__` (registration), `get_request_handler` / `get_websocket_app` (per request), the 3 `AsyncExitStack`s.
- `fastapi/_compat/v2.py` — `ModelField` = a `TypeAdapter` wrapper; `get_cached_model_fields` (`@lru_cache`).
- `fastapi/utils.py` — `create_model_field`, `get_path_param_names`.
- `fastapi/types.py` — `DependencyCacheKey = tuple[Callable|None, tuple[str,...], str]`.

---

## 0. Executive summary — the steal-worthy core + the per-request tax (read this first)

1. **The whole DI system is a two-phase compiler.** Phase A (registration, once per route at import):
   reflect the endpoint signature into a recursive immutable `Dependant` tree (the DI DAG). Phase B (per
   request): a recursive coroutine `solve_dependencies()` walks that tree, pulls values out of the ASGI
   request, calls each dependency, and memoizes by a cache key. **Phase A is pure metadata and is the
   thing Lancelot must move to Rust/codegen. Phase B is the hot path and is where FastAPI pays a Python
   tax it cannot avoid.**

2. **The `Dependant` (`models.py:31-194`) is the entire primitive.** A frozen-ish dataclass holding
   classified param lists (`path_params/query_params/header_params/cookie_params/body_params`), a list of
   child `Dependant`s, the special-injection param *names* (request/response/bg/websocket/security_scopes),
   and a pile of `@cached_property`s (`cache_key`, `is_coroutine_callable`, `is_gen_callable`,
   `is_async_gen_callable`, `computed_scope`, `_uses_scopes`). Remove this and FastAPI is just Starlette.

3. **The dependency cache key is a 3-tuple** `(call, sorted_oauth_scopes_or_(), computed_scope or "")`
   (`models.py:62-71`, `types.py:12`). Per-request memo dict keyed by this. **Scope-free deps collapse to
   `(call, (), "")`** — the common case. This is trivially representable as an interned integer in Rust.

4. **Per-request overhead Lancelot must eliminate** (all happen *every request*, none cached across
   requests): (a) recursive Python coroutine descent over the DAG (`solve_dependencies` recurses for each
   child, `utils.py:628-684`); (b) `request_params_to_args` rebuilding dicts + per-field Pydantic
   `validate_python` calls (`utils.py:784-866`); (c) `run_in_threadpool` hop for every sync dependency
   and sync endpoint (`utils.py:680`, `routing.py:330`); (d) two `AsyncExitStack`s constructed per request
   even when there are zero `yield` deps (`routing.py:116-119`); (e) building a fresh `Response()` object
   inside `solve_dependencies` every call (`utils.py:622-625`); (f) the dependency-override branch does a
   **full `get_dependant()` rebuild per request** when overrides are active (`utils.py:641-647`).

5. **The expensive once-only cost is the Pydantic `TypeAdapter` build per field.** Every `ModelField`
   `__post_init__` builds a `TypeAdapter(Annotated[...])` (`_compat/v2.py:141-166`) — schema compilation
   in pydantic-core (Rust). FastAPI does this at registration, not per request. **Steal this exactly:
   compile validators once; never per request.** Lancelot's equivalent: compile a Rust deserializer per
   field at route registration.

6. **Top steal:** the *classification-at-registration* idea. FastAPI decides "this param is a query
   param / this is a sub-dependency / this is the Request object" exactly once and stores the verdict as
   a list membership on `Dependant`. The request path never re-introspects a signature. Lancelot should
   do the same but emit a flat instruction list (a "resolution plan") instead of a tree to walk.

7. **Avoid:** FastAPI's recursion-per-request, its `inspect.signature(eval_str=True)` cost (mitigated by
   being once-only), the threadpool hop for every sync dep, the `deepcopy(field.default)` on every
   missing optional (`utils.py:745,780`), and the `_solve_generator`/`AsyncExitStack` machinery for
   non-yield deps. Also avoid rebuilding `Dependant` on dependency override.

8. **Codegen target:** the `Dependant` tree → a topologically-sorted flat plan: `[(slot, kind, source,
   validator_id, call_id, cache_key_id, is_async, scope)]`. Rust executes the plan; Python is only
   invoked for the user dependency callables themselves (unavoidable — user code is Python). Everything
   between callables (param extraction, validation, cache lookup, ordering) is Rust.

9. **AI-relevant:** `yield` deps + the request/function `AsyncExitStack` split is exactly the primitive
   for per-request LLM clients, DB sessions/transactions, and **streaming-scoped resources that must
   close *after* the stream ends** — FastAPI already solved the "close the DB session after the SSE
   stream finishes, not when the generator is GC'd" problem (`routing.py:586-595`, PEP 789). Lancelot
   must keep this semantic; it is load-bearing for agent/LLM backends.

10. **The "done once vs done per request" split is the entire optimization thesis** and is given
    precisely in §9. Short version: *graph shape, param classification, validators, callable-kind
    detection, cache-key tuples, body model* = once. *Graph traversal, value extraction, validation
    execution, callable invocation, cache dict, exit stacks, Response object* = per request. Lancelot
    moves the per-request scaffolding into Rust and keeps only Python callable invocation in Python.

---

## 1. The `Dependant` — the DI DAG node (KNOWN, `dependencies/models.py:31-194`)

This single `@dataclass` IS the dependency-injection primitive. The request path, the OpenAPI generator,
and the security model all read off this one tree. It is built once per route at import time.

### 1.1 The fields (`models.py:31-51`)

```python
@dataclass
class Dependant:
    path_params:    list[ModelField] = field(default_factory=list)   # :33
    query_params:   list[ModelField] = field(default_factory=list)   # :34
    header_params:  list[ModelField] = field(default_factory=list)   # :35
    cookie_params:  list[ModelField] = field(default_factory=list)   # :36
    body_params:    list[ModelField] = field(default_factory=list)   # :37
    dependencies:   list["Dependant"] = field(default_factory=list)  # :38  ← recursive DAG edges
    name:           str | None = None                                # :39  param name in PARENT signature
    call:           Callable[..., Any] | None = None                 # :40  the actual function/class to call
    request_param_name:           str | None = None                  # :41  special injections — store the
    websocket_param_name:         str | None = None                  # :42  *parameter name*, not the value;
    http_connection_param_name:   str | None = None                  # :43  value is wired in per request
    response_param_name:          str | None = None                  # :44
    background_tasks_param_name:  str | None = None                  # :45
    security_scopes_param_name:   str | None = None                  # :46
    own_oauth_scopes:    list[str] | None = None                     # :47  scopes declared on THIS Security()
    parent_oauth_scopes: list[str] | None = None                     # :48  scopes inherited from ancestors
    use_cache:  bool = True                                          # :49  per-request memoization toggle
    path:       str | None = None                                    # :50  route path (for override rebuild)
    scope:      Literal["function", "request"] | None = None         # :51  yield-dep teardown scope
```

**Key design fact (steal):** the special framework injections (Request, WebSocket, HTTPConnection,
Response, BackgroundTasks, SecurityScopes) are stored as the *parameter name string*, never as the value.
At request time `solve_dependencies` does `values[dependant.request_param_name] = request` etc.
(`utils.py:713-728`). This means **detecting "this endpoint wants the Request object" is a
registration-time decision; the per-request work is one dict assignment.** Lancelot: encode these as
fixed slots in the resolution plan ("slot 3 = bind request object"), zero reflection at request time.

**`name` semantics:** `name` is the *parameter name in the parent's signature* that this dependency's
return value should be bound to (`utils.py:681-682`: `values[sub_dependant.name] = solved`). The root
endpoint `Dependant` has `name=None`. Parameterless route-level dependencies (from
`dependencies=[Depends(x)]`) also have `name=None` so their value is computed (for side effects /
auth) but **not injected** into the endpoint signature (`utils.py:681` guards `if name is not None`).

### 1.2 The cached graph metadata (`@cached_property`, computed lazily once, then frozen)

These are the heart of "compile once". Each is a `functools.cached_property` so it is computed on first
access (during registration / first request) and then memoized on the instance forever.

#### `cache_key` — THE dependency-cache key (`models.py:62-71`)

```python
@cached_property
def cache_key(self) -> DependencyCacheKey:
    scopes_for_cache = (
        tuple(sorted(set(self.oauth_scopes or []))) if self._uses_scopes else ()
    )
    return (
        self.call,                  # the callable identity (function/class object)
        scopes_for_cache,           # () for the overwhelmingly common scope-free case
        self.computed_scope or "",  # "" | "request" | "function"
    )
```

`types.py:12`: `DependencyCacheKey = tuple[Callable[..., Any] | None, tuple[str, ...], str]`.

- **Common case:** a plain `Depends(get_db)` with no security scopes → key = `(get_db, (), "")`.
- A `Security(get_user, scopes=["items:read","admin"])` → key = `(get_user, ("admin","items:read"), "")`
  (scopes are `sorted(set(...))` so order-independent and dedup'd).
- A `yield` dep with default scope → `computed_scope == "request"` → key third element `"request"`.

**Why scopes are only in the key when `_uses_scopes`:** the same `get_current_user` callable can be
required with different scope sets by different sub-dependencies in one request. If it ignores scopes
(no `SecurityScopes` param, not a security scheme), its result is scope-independent so all callers share
one cache entry. If it *does* read scopes, two callers with different scope sets get different results
and must NOT share the cache → scopes go into the key. This is a correctness optimization, not just
perf. **Steal this exactly.**

**Lancelot Rust mapping:** at registration, assign every distinct `cache_key` an integer id. The
per-request cache becomes a small `Vec<Option<PyObject>>` indexed by id (dense array, not a hashmap) —
O(1) with no hashing, no tuple allocation, no `Callable` hashing per request. The scope-free common
case means most plans have 1 cache slot per distinct callable.

#### `_uses_scopes` (`models.py:73-84`) — recursive, cached

```python
@cached_property
def _uses_scopes(self) -> bool:
    if self.own_oauth_scopes: return True
    if self.security_scopes_param_name is not None: return True
    if self._is_security_scheme: return True
    for sub_dep in self.dependencies:
        if sub_dep._uses_scopes: return True
    return False
```

Recursively true if this dep or any descendant cares about OAuth2 scopes. Computed once, cached. In
Lancelot this is a static boolean flag on the compiled plan node.

#### `oauth_scopes` (`models.py:53-60`) — order-preserving merge

```python
@cached_property
def oauth_scopes(self) -> list[str]:
    scopes = self.parent_oauth_scopes.copy() if self.parent_oauth_scopes else []
    for scope in self.own_oauth_scopes or []:        # order preserved deliberately
        if scope not in scopes:
            scopes.append(scope)
    return scopes
```

Effective scope list = parent scopes + own scopes (dedup, order preserved — the comment at `:56`
explicitly says "doesn't use a set to preserve order, just in case"). This is the value injected into a
`SecurityScopes` parameter (`utils.py:725-728`).

#### `_is_security_scheme` / `_security_scheme` / `_security_dependencies` (`models.py:86-103`)

`_is_security_scheme`: unwraps `self.call` through `partial`/`functools.wraps` (`_unwrapped_call`,
`models.py:18-28`) and checks `isinstance(unwrapped, SecurityBase)`. Security deps are *just callables
that happen to be instances of `SecurityBase`* — there is no special security resolver. The only
difference: they contribute to the OpenAPI security scheme doc and to scope propagation.

#### `is_coroutine_callable` / `is_gen_callable` / `is_async_gen_callable` (`models.py:105-185`)

These three are the **callable-kind classifier** — the single most reused per-request decision, and
FastAPI correctly computes it **once** then caches. Each is ~25 lines because it must handle: plain
functions, `functools.partial`, `functools.wraps`-decorated functions, classes (a class used as a
dependency is constructed, so it's "not a coroutine"), and **callable instances** (objects with
`__call__`) — including async/gen `__call__`. Representative (`is_coroutine_callable`,
`models.py:157-185`):

```python
@cached_property
def is_coroutine_callable(self) -> bool:
    if self.call is None: return False
    if inspect.isroutine(_impartial(self.call)) and iscoroutinefunction(_impartial(self.call)):
        return True
    if inspect.isroutine(_unwrapped_call(self.call)) and iscoroutinefunction(_unwrapped_call(self.call)):
        return True
    if inspect.isclass(_unwrapped_call(self.call)): return False
    dunder_call = getattr(_impartial(self.call), "__call__", None)
    if dunder_call is None: return False
    if iscoroutinefunction(_impartial(dunder_call)) or iscoroutinefunction(_unwrapped_call(dunder_call)):
        return True
    dunder_unwrapped_call = getattr(_unwrapped_call(self.call), "__call__", None)
    if dunder_unwrapped_call is None: return False
    if iscoroutinefunction(_impartial(dunder_unwrapped_call)) or iscoroutinefunction(_unwrapped_call(dunder_unwrapped_call)):
        return True
    return False
```

`_impartial` (`models.py:25-28`) strips `functools.partial` layers; `_unwrapped_call` (`:18-22`) also
applies `inspect.unwrap` (strips `@functools.wraps`). Python ≥3.13 uses `inspect.iscoroutinefunction`,
else `asyncio.iscoroutinefunction` (`models.py:12-15`). **This is ~75 lines of `inspect` introspection
that FastAPI executes exactly once per callable and caches.** Reproducing this *per request* would be a
catastrophic perf bug. Lancelot: classify each callable's kind at registration, store a 2-bit enum
(`SYNC | ASYNC | SYNC_GEN | ASYNC_GEN`) in the plan node.

#### `computed_scope` (`models.py:187-193`) — the yield-teardown scope

```python
@cached_property
def computed_scope(self) -> str | None:
    if self.scope:                                              # explicit Depends(scope="function"|"request")
        return self.scope
    if self.is_gen_callable or self.is_async_gen_callable:      # any yield dep defaults to "request"
        return "request"
    return None                                                 # plain dep: no teardown scope
```

So: a non-generator dep has `computed_scope=None` (third cache-key element `""`). A `yield` dep
defaults to `"request"` (teardown after response sent) unless explicitly `scope="function"` (teardown
before response sent — see §6). This is the only place the yield/exit-stack routing decision is made,
and it is cached.

### 1.3 `Dependant` perf characterization

- **(b) perf cost:** zero per request for the structure; the `@cached_property`s are computed during
  registration (or first request, then frozen). The cost is paid in `get_dependant` (§2) which is
  registration-time. Per request the tree is only *read*.
- **(c) steal-for-Lancelot:** the entire concept — a precomputed immutable node with classified param
  buckets + cached callable-kind + cached cache-key. This is the correct architecture. Keep it.
- **(d) avoid:** the `list[ModelField]` per category and the `dict`-based wiring at request time
  (`values[name] = ...`). Lancelot should compile to positional slots, not name-keyed dicts.
- **(e) Rust/codegen:** lower `Dependant` to a `struct PlanNode { call: PyObject, kind: CallableKind,
  cache_slot: u32, scope: Scope, params: Vec<ParamSpec>, children: Vec<u32> }` and store the whole
  plan as a `Vec<PlanNode>` in topo order. All the `@cached_property` results become plain struct
  fields filled at registration. No `inspect`, no `functools.partial` unwrapping at request time.

---

## 2. Phase A — registration-time graph build (KNOWN, `dependencies/utils.py`)

This is the compiler. It runs once per route inside `APIRoute.__init__` / `APIWebSocketRoute.__init__`
(`routing.py:949-965` and `:784-795`). **Everything here is the optimization target: it should be
Rust/codegen, never per-request.**

### 2.1 Where registration is triggered (`routing.py:949-976`)

```python
# APIRoute.__init__  (HTTP route)
self.dependant = get_dependant(path=self.path_format, call=self.endpoint, scope="function")  # :949
for depends in self.dependencies[::-1]:                                                       # :952
    self.dependant.dependencies.insert(
        0, get_parameterless_sub_dependant(depends=depends, path=self.path_format))           # :953-956
self._flat_dependant = get_flat_dependant(self.dependant)                                     # :957
self._embed_body_fields = _should_embed_body_fields(self._flat_dependant.body_params)         # :958
self.body_field = get_body_field(flat_dependant=self._flat_dependant,
                                 name=self.unique_id, embed_body_fields=self._embed_body_fields) # :961
is_generator = self.dependant.is_async_gen_callable or self.dependant.is_gen_callable          # :967
self.is_sse_stream  = is_generator and lenient_issubclass(response_class, EventSourceResponse)  # :970
self.is_json_stream = is_generator and isinstance(response_class, DefaultPlaceholder)           # :973
self.app = request_response(self.get_route_handler())                                           # :976
```

So at import time per route FastAPI: (1) builds the `Dependant` tree, (2) prepends route-level
`dependencies=[...]` as parameterless sub-deps **in reverse so they run in declared order** (`[::-1]` +
`insert(0,...)`), (3) flattens the tree (`get_flat_dependant`) for body/param schema purposes,
(4) decides whether body fields are embedded, (5) builds the combined body `ModelField`, (6) classifies
the endpoint as JSONL/SSE/plain stream, (7) closes over the resolver via `get_request_handler`.
**`route.endpoint` callable note (`routing.py:785,950`): the route-level `scope="function"` is passed
to the endpoint's own `Dependant`** so the endpoint itself participates in the function-scope exit-stack
boundary.

The endpoint `Dependant` is the *root*; route-level `dependencies=[Depends(auth)]` become child
`Dependant`s with `name=None` (computed, not injected — §1.1).

### 2.2 `get_dependant()` — recursive tree build (KNOWN, `utils.py:286-359`)

```python
def get_dependant(*, path, call, name=None, own_oauth_scopes=None,
                   parent_oauth_scopes=None, use_cache=True, scope=None) -> Dependant:
    dependant = Dependant(call=call, name=name, path=path, use_cache=use_cache,
                          scope=scope, own_oauth_scopes=own_oauth_scopes,
                          parent_oauth_scopes=parent_oauth_scopes)               # :296-304
    current_scopes = (parent_oauth_scopes or []) + (own_oauth_scopes or [])      # :305
    path_param_names = get_path_param_names(path)                                # :306  set(re.findall("{(.*?)}", path))  utils.py:43-44
    endpoint_signature = get_typed_signature(call)                               # :307  ← inspect.signature, the costly bit
    signature_params = endpoint_signature.parameters                            # :308
    for param_name, param in signature_params.items():                          # :309  walk each parameter
        is_path_param = param_name in path_param_names                          # :310
        param_details = analyze_param(param_name=param_name,
                                      annotation=param.annotation,
                                      value=param.default,
                                      is_path_param=is_path_param)              # :311-316
        if param_details.depends is not None:                                  # :317  → it's a sub-dependency
            # scope-mismatch guard: a request-scoped yield dep cannot depend on a function-scoped one
            if ((dependant.is_gen_callable or dependant.is_async_gen_callable)
                and dependant.computed_scope == "request"
                and param_details.depends.scope == "function"):                # :319-323
                raise DependencyScopeError(...)                                # :326-329
            sub_own_oauth_scopes = list(param_details.depends.scopes) \
                if isinstance(param_details.depends, params.Security) and param_details.depends.scopes else []  # :330-333
            sub_dependant = get_dependant(path=path,
                                          call=param_details.depends.dependency,
                                          name=param_name,
                                          own_oauth_scopes=sub_own_oauth_scopes,
                                          parent_oauth_scopes=current_scopes,
                                          use_cache=param_details.depends.use_cache,
                                          scope=param_details.depends.scope)    # :334-342  ← RECURSION
            dependant.dependencies.append(sub_dependant)                       # :343
            continue
        if add_non_field_param_to_dependency(param_name=param_name,
                type_annotation=param_details.type_annotation,
                dependant=dependant):                                          # :345  Request/WS/Response/BG/SecurityScopes
            assert param_details.field is None
            continue
        assert param_details.field is not None
        if isinstance(param_details.field.field_info, params.Body):            # :355  body param
            dependant.body_params.append(param_details.field)
        else:
            add_param_to_fields(field=param_details.field, dependant=dependant) # :358  path/query/header/cookie
    return dependant
```

**Recursion structure:** depth-first, pre-order. There is no explicit recursion limit — depth is
Python's stack (≈1000). Each sub-`Depends` triggers a recursive `get_dependant` call. The DAG is built
as a *tree* (a shared dependency appears as multiple `Dependant` nodes); deduplication happens only at
request time via the cache key, and in `get_flat_dependant(skip_repeats=True)` for schema purposes.

**The scope-mismatch guard (`utils.py:319-329`):** a `scope="request"` yield dependency may NOT depend
on a `scope="function"` dependency (the function-scoped one would be torn down before the request-scoped
one finishes — a lifetime violation). Raised at *registration* (import time) — fail fast. Lancelot must
replicate this static check during plan compilation.

`add_non_field_param_to_dependency` (`utils.py:362-383`): `lenient_issubclass` chain mapping the
parameter type to one of `request_param_name / websocket_param_name / http_connection_param_name /
response_param_name / background_tasks_param_name / security_scopes_param_name`. Pure type check, once.

`add_param_to_fields` (`utils.py:562-575`): routes a classified field into the right bucket by
`field_info.in_` (`ParamTypes.path/query/header/cookie`); body is handled separately at `:355`.

### 2.3 `analyze_param()` — the param classifier (KNOWN, `utils.py:393-559`) — the hairiest registration code

This is the function that decides, for a single parameter, *what it is*. It is ~165 lines and runs once
per parameter per route. Reproduced as the decision algorithm:

```
analyze_param(param_name, annotation, value, is_path_param):
  type_annotation = Any; use_annotation = Any; field_info = None; depends = None
  if is_typealiastype(annotation):                       # PEP 695  type X = ...   :404-406
      annotation = annotation.__value__
  if annotation is not Signature.empty:                  # :407-409
      use_annotation = type_annotation = annotation
  # ---- unwrap Annotated[...] ----
  if get_origin(use_annotation) is Annotated:            # :411
      annotated_args = get_args(annotation)              # :412
      type_annotation = annotated_args[0]                # the real type           :413
      fastapi_annotations = [a for a in annotated_args[1:]
                             if isinstance(a, (FieldInfo, params.Depends))]        # :414-418
      fastapi_specific = [a for a in fastapi_annotations
                          if isinstance(a, (params.Param, params.Body, params.Depends))]  # :419-430
      fastapi_annotation = fastapi_specific[-1] if fastapi_specific else None      # LAST wins  :431-436
      if isinstance(fastapi_annotation, FieldInfo):                                # :438
          field_info = copy_field_info(field_info=fastapi_annotation,
                                       annotation=use_annotation)                  # :440-443  COPY (mutated below)
          assert field_info.default in (Undefined, RequiredParam), \
              "default value cannot be set in Annotated; use = instead"            # :444-449
          if value is not Signature.empty:
              assert not is_path_param, "Path params cannot have default values"   # :451
              field_info.default = value                                          # :452
          else:
              field_info.default = RequiredParam                                   # :454
      elif isinstance(fastapi_annotation, params.Depends):                         # :456
          depends = fastapi_annotation
  # ---- Depends / FieldInfo from the DEFAULT value (the `= Depends(...)` style) ----
  if isinstance(value, params.Depends):                  # :459
      assert depends is None ; assert field_info is None                          # mutual-exclusion asserts
      depends = value
  elif isinstance(value, FieldInfo):                     # :470
      assert field_info is None
      field_info = value ; field_info.annotation = type_annotation
  # ---- Depends with no explicit dependency  →  the TYPE is the dependency (class) ----
  if depends is not None and depends.dependency is None: # :480
      depends = copy(depends)
      depends = dataclasses.replace(depends, dependency=type_annotation)          # :482-483
  # ---- special framework types (Request, WS, Response, BG, SecurityScopes) ----
  if depends is None and lenient_issubclass(type_annotation,
        (Request, WebSocket, HTTPConnection, Response,
         StarletteBackgroundTasks, SecurityScopes)):                              # :488-498
      assert field_info is None
  # ---- nothing declared → INFER param kind from the type ----
  elif field_info is None and depends is None:                                    # :503
      default_value = value if value is not Signature.empty else RequiredParam     # :504
      if is_path_param:
          field_info = params.Path(annotation=use_annotation)                     # :509
      elif is_uploadfile_or_nonable_uploadfile_annotation(type_annotation) \
           or is_uploadfile_sequence_annotation(type_annotation):
          field_info = params.File(annotation=use_annotation, default=default_value)  # :513
      elif not field_annotation_is_scalar(annotation=type_annotation):            # complex type → body  :514
          field_info = params.Body(annotation=use_annotation, default=default_value)  # :515
      else:
          field_info = params.Query(annotation=use_annotation, default=default_value) # :517  scalar → query
  # ---- finalize the ModelField ----
  field = None
  if field_info is not None:
      if is_path_param:
          assert isinstance(field_info, params.Path)                              # :524
      elif isinstance(field_info, params.Param) and field_info.in_ is None:
          field_info.in_ = params.ParamTypes.query                                # :532  default Param→query
      if isinstance(field_info, params.Form):
          ensure_multipart_is_installed()                                         # :535  import check, once
      if not field_info.alias and getattr(field_info, "convert_underscores", None):
          alias = param_name.replace("_", "-")                                    # :537  header underscore→dash
      else:
          alias = field_info.alias or param_name
      field_info.alias = alias                                                    # :540
      field = create_model_field(name=param_name, type_=use_annotation,
                                 default=field_info.default, alias=alias,
                                 field_info=field_info)                           # :541-547  ← builds TypeAdapter
      if is_path_param: assert is_scalar_field(field)                             # :549
      elif isinstance(field_info, params.Query):
          assert is_scalar_field(field) or field_annotation_is_scalar_sequence(...) \
                 or lenient_issubclass(field.field_info.annotation, BaseModel)    # :552-557
  return ParamDetails(type_annotation=type_annotation, depends=depends, field=field)  # :559
```

**The classification rule (memorize this — Lancelot must reproduce it byte-for-byte for compat):**
1. Explicit `Annotated[T, Depends()]` or `= Depends()` → sub-dependency.
2. `Annotated[T, Query()/Path()/Header()/Cookie()/Body()/Form()/File()]` or `= Query(...)` → that kind.
   **The LAST FastAPI annotation in `Annotated[...]` wins** (`:431-433` `[-1]`).
3. Bare type that is `Request/WebSocket/HTTPConnection/Response/BackgroundTasks/SecurityScopes` →
   special injection (only when no `Depends`).
4. Nothing declared, **inference order** (`:503-517`): path param → `Path`; `UploadFile`-ish → `File`;
   non-scalar (complex/BaseModel/dict/list-of-models) → `Body`; scalar → `Query`. *This default
   "scalar=query, complex=body" rule is FastAPI's single most ergonomic decision and the #1 thing
   Lancelot must copy exactly.*
5. `Depends()` with no callable arg → the **type annotation itself** is the dependency (class-as-dep):
   `Annotated[MyService, Depends()]` constructs `MyService(...)` (`:480-483`).

**`field_annotation_is_scalar`** (`_compat/shared.py:104-106`): `annotation is Ellipsis or not
field_annotation_is_complex(annotation)`. Complex = BaseModel / dataclass / TypedDict / sequence /
mapping / has `__pydantic_core_schema__`. **This is the scalar↔body discriminator.** Lancelot needs the
exact same predicate or request bodies vs query params will be misclassified.

**Mutation hazard to avoid:** `analyze_param` *mutates* `field_info` (`:452 .default=`, `:540 .alias=`,
`:477 .annotation=`). It calls `copy_field_info` only for the `Annotated` branch (`:440`) — the
`= Query(...)` default-value branch mutates the user's object in place. This is a latent footgun (reuse
the same `Query(...)` object across two params and aliases collide). Lancelot: treat all param specs as
immutable; copy-on-classify.

---

## 3. Phase A continued — flattening, the body model, and the real once-only cost

### 3.1 `get_flat_dependant()` — collapse the tree for schema/body (KNOWN, `utils.py:138-189`)

Builds a *flat* `Dependant` by recursively concatenating every descendant's param lists into one node.
`visited` is a `list[DependencyCacheKey]`; `skip_repeats=True` (used by `get_flat_params`,
`utils.py:204-210`) drops a sub-dep whose `cache_key` was already seen — so shared deps' params/schema
appear once. Crucially `skip_repeats` is **False** in `APIRoute.__init__` (`routing.py:957`) — the flat
body params keep duplicates, deduped later by name in `_should_embed_body_fields` (`utils.py:893`).

`parent_oauth_scopes` is threaded down so each flat node's `oauth_scopes` is the fully-merged effective
scope set — this is how the OpenAPI security requirement and `SecurityScopes` injection get the complete
ancestor scope chain. **All registration-time; per request this is never recomputed.**

### 3.2 `_should_embed_body_fields()` (KNOWN, `utils.py:888-909`)

Decides whether the request body is a single top-level model or a dict of named fields:

```python
def _should_embed_body_fields(fields):
    if not fields: return False
    body_param_names_set = {f.name for f in fields}        # dedup by name across deps
    if len(body_param_names_set) > 1: return True          # >1 distinct body field → embed
    first = fields[0]
    if getattr(first.field_info, "embed", None): return True               # explicit Body(embed=True)
    if (isinstance(first.field_info, params.Form)
        and not lenient_issubclass(first.field_info.annotation, BaseModel)
        and not is_union_of_base_models(first.field_info.annotation)):
        return True                                        # scalar Form field → must embed
    return False
```

Result cached as `route._embed_body_fields`. Per request `solve_dependencies` only *reads* the boolean.

### 3.3 `get_body_field()` — synthesize the combined body `ModelField` (KNOWN, `utils.py:1001-1052`)

When embedded, FastAPI dynamically builds a Pydantic model `Body_<unique_id>` from all body params
(`create_body_model`, `_compat/v2.py:387-392`: `pydantic.create_model(name, **{f.name:(ann,info)})`),
wraps it in a `Body`/`Form`/`File` `FieldInfo` (whichever the params are), and makes one `ModelField`.
This is **one `pydantic.create_model` + one `TypeAdapter` build per route at import** — moderately
expensive (Rust schema compile) but amortized. Per request the body is validated through this single
prebuilt field. Not embedded → the first body param's own `ModelField` is reused directly (`:1017-1018`).

### 3.4 `ModelField` = a compiled `TypeAdapter` (KNOWN, `_compat/v2.py:113-242`) — THE expensive bit

```python
@dataclass
class ModelField:                       # _compat/v2.py:113-118
    field_info: FieldInfo
    name: str
    mode: Literal["validation","serialization"] = "validation"
    config: ConfigDict | None = None

    def __post_init__(self):            # :141-166  RUNS ONCE per field at construction (registration)
        field_dict = asdict(self.field_info)                         # {annotation, metadata, attributes}
        annotated_args = (field_dict["annotation"], *field_dict["metadata"],
                          Field(**field_dict["attributes"]))
        self._type_adapter = TypeAdapter(Annotated[annotated_args], config=self.config)  # :163  ← pydantic-core schema compile

    def validate(self, value, values={}, *, loc=()):                 # :173-188  RUNS PER REQUEST
        try:
            return self._type_adapter.validate_python(value, from_attributes=True), []
        except ValidationError as exc:
            return None, _regenerate_error_with_loc(errors=exc.errors(include_url=False), loc_prefix=loc)

    def serialize_json(self, value, *, include=None, exclude=None, by_alias=True,
                       exclude_unset=False, exclude_defaults=False, exclude_none=False) -> bytes:  # :215-239
        return self._type_adapter.dump_json(value, ...)              # pydantic-core Rust → bytes, one pass
```

**This is the single most important perf fact in the whole DI system.** Building a `TypeAdapter`
compiles a pydantic-core validation+serialization schema (in Rust). FastAPI does this **once per field
at registration** (inside `create_model_field`, `utils.py:58-77` → `v2.ModelField(...)` →
`__post_init__`). Per request it only calls `validate_python` (Rust, fast) and `dump_json` (Rust,
fast). **Steal this exactly**: compile the validator once, store the handle, reuse forever.
`get_cached_model_fields` (`_compat/v2.py:413-415`) wraps `get_model_fields` in `@lru_cache` so the
field list of a Pydantic model used as a single param/body is computed once process-wide.

`create_model_field` (`utils.py:58-77`) also enforces "no pydantic v1" (`annotation_is_pydantic_v1` →
`PydanticV1NotSupportedError`) and converts a pydantic schema-gen failure into a friendly
`FastAPIError`. All registration-time.

### 3.5 What §2-3 cost, and the Lancelot lowering

- **(b) perf cost (registration, paid once):** `inspect.signature(call, eval_str=True)`
  (`utils.py:213-225`) — slow (string annotation eval, `__globals__` resolution), but once.
  `analyze_param` per param. One `TypeAdapter` schema-compile per field (the dominant cost; tens to
  hundreds of µs each in pydantic-core). `create_model` + `TypeAdapter` for the embedded body. All
  amortized over the process lifetime → irrelevant at steady state.
- **(b) perf cost (per request):** *zero* from this section — the request path only reads the prebuilt
  `ModelField`s and calls their Rust `validate_python`.
- **(c) steal-for-Lancelot:** compile-once validators; the `@lru_cache` on model-field extraction; the
  "build a synthetic body model once per route" trick; classify-once param buckets.
- **(d) avoid:** `inspect.signature(eval_str=True)` at any point that could recur per request; the
  per-route `pydantic.create_model` if Lancelot can instead emit a Rust struct deserializer via codegen.
- **(e) Rust/codegen:** at registration, walk the (Python) signature once, and **emit a Rust
  deserializer per field** (serde-style) keyed to the field's source (query/path/header/cookie/body
  slot). The "ModelField" becomes a `ValidatorId` (u32) into a `Vec<CompiledValidator>` in the Rust
  core. `validate_python` → a Rust function call with no Python frame. The synthetic body model becomes
  a generated Rust struct. Net: registration produces a fully-typed Rust resolution plan; pydantic-core
  is replaced by Lancelot's own Rust validators (or pydantic-core is FFI-called directly, skipping the
  Python `ModelField.validate` wrapper frame).

---

## 4. Phase B — the per-request resolver `solve_dependencies()` (KNOWN, `utils.py:598-735`) — THE HOT PATH

This is the function Lancelot must out-run. It is an `async def` recursive coroutine, called once per
request with the route's root `Dependant`, recursing for every sub-dependency.

### 4.1 Signature & invocation (`utils.py:598-611`, `routing.py:457-464`)

```python
async def solve_dependencies(*, request, dependant, body=None, background_tasks=None,
        response=None, dependency_overrides_provider=None, dependency_cache=None,
        async_exit_stack: AsyncExitStack, embed_body_fields: bool) -> SolvedDependency
```

Called from `get_request_handler.app` (`routing.py:457`) after the body is read, and from
`get_websocket_app.app` (`routing.py:751`). `async_exit_stack` param is **vestigial** (comment
`utils.py:607-608`: "no longer used, not removing it… some people might be monkey-patching"). The real
stacks are pulled from `request.scope` (next).

### 4.2 The full algorithm (reproduced as exhaustive pseudocode from `utils.py:612-735`)

```
solve_dependencies(request, dependant, body, background_tasks, response,
                   overrides_provider, dependency_cache, embed_body_fields):

  # ---- pull the two request-lifetime exit stacks out of the ASGI scope ----
  request_astack  = request.scope["fastapi_inner_astack"]      # AsyncExitStack  :612  request-scoped
  function_astack = request.scope["fastapi_function_astack"]   # AsyncExitStack  :616  function-scoped
  values = {}                                                  # name -> resolved value   :620
  errors = []                                                  # accumulated validation errors  :621
  if response is None:                                         # :622  FRESH Response built EVERY top call
      response = Response(); del response.headers["content-length"]; response.status_code = None
  if dependency_cache is None: dependency_cache = {}            # :626  per-request memo dict

  # ===== 1. RESOLVE SUB-DEPENDENCIES (depth-first, in declared order) =====
  for sub_dependant in dependant.dependencies:                 # :628
      call = sub_dependant.call
      use_sub_dependant = sub_dependant
      # ---- dependency override (test/DI swap) — REBUILDS the Dependant PER REQUEST ----
      if overrides_provider and overrides_provider.dependency_overrides:           # :632
          original_call = sub_dependant.call
          call = overrides_provider.dependency_overrides.get(original_call, original_call)  # :637
          use_sub_dependant = get_dependant(path=sub_dependant.path, call=call,
                                            name=sub_dependant.name,
                                            parent_oauth_scopes=sub_dependant.oauth_scopes,
                                            scope=sub_dependant.scope)             # :641-647  ← FULL REBUILD
      # ---- RECURSE (resolve this child's own sub-deps + params first) ----
      solved_result = await solve_dependencies(request=request,
              dependant=use_sub_dependant, body=body, background_tasks=background_tasks,
              response=response, dependency_overrides_provider=overrides_provider,
              dependency_cache=dependency_cache, async_exit_stack=async_exit_stack,
              embed_body_fields=embed_body_fields)                                 # :649-659  ← RECURSION
      background_tasks = solved_result.background_tasks                            # :660  thread BG tasks down/up
      if solved_result.errors:
          errors.extend(solved_result.errors); continue                           # :661-663  skip calling on error
      # ---- CACHE LOOKUP ----
      if sub_dependant.use_cache and sub_dependant.cache_key in dependency_cache:  # :664
          solved = dependency_cache[sub_dependant.cache_key]                       # :665  HIT — skip the call
      elif use_sub_dependant.is_gen_callable or use_sub_dependant.is_async_gen_callable:  # :666  yield dep
          use_astack = request_astack
          if sub_dependant.scope == "function":
              use_astack = function_astack                                        # :670-671  scope routing
          solved = await _solve_generator(dependant=use_sub_dependant,
                          stack=use_astack, sub_values=solved_result.values)       # :672  enter ctx mgr
      elif use_sub_dependant.is_coroutine_callable:                               # :677  async def dep
          solved = await call(**solved_result.values)                             # :678  direct await
      else:                                                                       # sync dep
          solved = await run_in_threadpool(call, **solved_result.values)          # :680  THREADPOOL HOP
      # ---- BIND + MEMOIZE ----
      if sub_dependant.name is not None:                                          # :681  None = route-level dep
          values[sub_dependant.name] = solved                                     # :682  inject into parent kwargs
      if sub_dependant.cache_key not in dependency_cache:
          dependency_cache[sub_dependant.cache_key] = solved                      # :683-684  store (even if use_cache False? see note)

  # ===== 2. EXTRACT + VALIDATE THIS NODE'S OWN PARAMS =====
  path_values,   path_errors   = request_params_to_args(dependant.path_params,   request.path_params)   # :685
  query_values,  query_errors  = request_params_to_args(dependant.query_params,  request.query_params)  # :688
  header_values, header_errors = request_params_to_args(dependant.header_params, request.headers)       # :691
  cookie_values, cookie_errors = request_params_to_args(dependant.cookie_params, request.cookies)       # :694
  values.update(path_values); values.update(query_values)
  values.update(header_values); values.update(cookie_values)                       # :697-700
  errors += path_errors + query_errors + header_errors + cookie_errors             # :701
  if dependant.body_params:                                                        # :702
      body_values, body_errors = await request_body_to_args(
          body_fields=dependant.body_params, received_body=body,
          embed_body_fields=embed_body_fields)                                      # :703-710
      values.update(body_values); errors.extend(body_errors)                        # :711-712

  # ===== 3. WIRE SPECIAL FRAMEWORK INJECTIONS (one dict assignment each) =====
  if dependant.http_connection_param_name: values[...] = request                    # :713-714
  if dependant.request_param_name and isinstance(request, Request): values[...] = request  # :715-716
  elif dependant.websocket_param_name and isinstance(request, WebSocket): values[...] = request  # :717-718
  if dependant.background_tasks_param_name:
      if background_tasks is None: background_tasks = BackgroundTasks()             # :719-721
      values[dependant.background_tasks_param_name] = background_tasks
  if dependant.response_param_name: values[...] = response                          # :723-724
  if dependant.security_scopes_param_name:
      values[...] = SecurityScopes(scopes=dependant.oauth_scopes)                   # :725-728

  return SolvedDependency(values=values, errors=errors,
                          background_tasks=background_tasks, response=response,
                          dependency_cache=dependency_cache)                        # :729-735
```

### 4.3 Critical semantics (each is a Lancelot requirement or anti-pattern)

**Ordering — depth-first, pre-order, sub-deps before own params.** A node's children are fully resolved
*before* its own path/query/header/cookie/body params are extracted (`:628` loop precedes `:685`).
Children resolve left-to-right in *signature declaration order* (dict insertion order of
`signature.parameters`). Route-level `dependencies=[a,b]` were prepended in declared order (§2.1) so
they run first, in order, before signature deps. **This ordering is observable** (a dep can raise
`HTTPException` and short-circuit). Lancelot's compiled plan must preserve exact topological + declared
order.

**Caching is per-request, keyed by `cache_key` (the 3-tuple).** `:664` checks `use_cache AND key in
cache` → reuse. `:683` stores the result if the key isn't present yet. Subtle bug-ish behavior: even
when `sub_dependant.use_cache is False`, the result is *still written* to `dependency_cache` at `:683`
(only the *read* at `:664` is gated by `use_cache`). So `use_cache=False` means "always recompute this
node" but it still pollutes the cache for any *other* node sharing the key that has `use_cache=True`.
INFERRED: this is intentional-ish (last-write-wins) but a Lancelot reimplementation should decide
deliberately; safest is "use_cache=False ⇒ neither read nor write the shared slot, use a private slot".

**Cache scope = one request.** `dependency_cache` is created fresh per top-level `solve_dependencies`
call (`:626`) and threaded through recursion. Nothing persists across requests. A dep shared by 5
sub-deps in one request runs **once**; next request it runs again. **This is the key AI pattern**: a
`get_llm_client`/`get_db` dep used by many sub-deps is instantiated once per request, not 5×.

**Async vs sync dispatch (`:666-680`):**
- yield dep (sync or async generator) → `_solve_generator` enters it as a context manager on an
  exit stack (§6).
- `is_coroutine_callable` (async def, or callable whose `__call__` is async) → `await call(**kwargs)`
  directly on the event loop.
- everything else (plain sync function, sync callable, **class constructor**) → `await
  run_in_threadpool(call, **kwargs)` (`:680`). **Every sync dependency pays a threadpool round-trip
  per request** (anyio capacity-limited worker thread, default 40). This is FastAPI's biggest avoidable
  per-request cost for sync-heavy apps. Lancelot should: run trivially-fast sync deps inline (no
  thread) unless declared blocking, or expose a `blocking=True` opt-in and default to inline.

**Errors are aggregated, not raised eagerly.** Param validation failures become error dicts collected
in `errors`; a sub-dep with errors is *not called* (`:661-663`) but resolution continues so ALL errors
across all params/deps are reported in one `RequestValidationError` → HTTP 422 (raised in
`routing.py:719-723`). A dep that raises `HTTPException` *does* short-circuit (exception propagates out
of the `await`). Lancelot must keep "collect all validation errors, 422 once" — it is a beloved
FastAPI behavior.

**`Response` object built per request inside the resolver (`:622-625`).** Allocated even if the
endpoint never touches a `Response` parameter — needed because deps may set cookies/headers via an
injected `Response`. `del response.headers["content-length"]` + `status_code=None` sentinels. Minor
allocation; Lancelot can lazily allocate only if any node has `response_param_name` set OR a dep
mutates response headers (detected at registration → a static "needs_response" flag).

**The override path rebuilds the graph per request (`:641-647`).** When
`app.dependency_overrides` is non-empty, FastAPI throws away the precompiled `sub_dependant` and calls
`get_dependant()` again *inside the request*. This is acceptable only because overrides are a test/dev
feature. **Lancelot anti-pattern to avoid**: never recompile the plan in the hot path. Instead,
maintain an override map `call_id -> replacement_call_id` and a pre-built plan variant, or compile the
override target's plan once on `dependency_overrides[...] = ...` assignment and swap the subtree
pointer.

### 4.4 `SolvedDependency` (`utils.py:589-595`)

```python
@dataclass
class SolvedDependency:
    values: dict[str, Any]                       # final kwargs for the endpoint / parent dep
    errors: list[Any]                            # validation error dicts
    background_tasks: StarletteBackgroundTasks | None
    response: Response
    dependency_cache: dict[DependencyCacheKey, Any]
```

Returned up the recursion. The root call's `.values` is splatted into the endpoint:
`run_endpoint_function` does `await dependant.call(**values)` (async) or `await
run_in_threadpool(dependant.call, **values)` (sync) (`routing.py:320-330`).

### 4.5 Perf characterization + Lancelot lowering

- **(b) per-request cost:** one Python coroutine frame *per node* in the DAG (recursion); 4 calls to
  `request_params_to_args` per node (even when a list is empty — early-returns at `utils.py:791-792`
  but still a Python call + 4 dict allocations `values`); a `dict` `values` per node + `.update()`s; a
  threadpool hop per sync dep; a fresh `Response`; a fresh `dependency_cache` dict; tuple hashing for
  every cache get/set (`cache_key` is a 3-tuple containing a function object — hashing a function is
  `id`-based, cheap, but the tuple+hash still allocates).
- **(c) steal:** per-request memoization keyed by callable identity; collect-all-errors-then-422;
  request-scoped (not global) cache; depth-first declared-order resolution; sub-deps-before-own-params.
- **(d) avoid:** Python recursion per node; threadpool hop for fast sync deps; per-node dict
  allocations + `.update()` churn; per-request `Response`/cache dict allocation; graph rebuild on
  override; calling `request_params_to_args` for empty buckets.
- **(e) Rust/codegen:** compile the DAG to a **flat topo-ordered instruction vector**
  `Vec<ResolveOp>`. Execute it in Rust with a single arena: `slots: Vec<Option<PyObject>>` indexed by
  cache-slot id (no hashmap, no tuple hashing). Param extraction ops read directly from the ASGI
  scope's headers/query/path Rust-side and call the compiled validator → write the slot. The only
  Python crossings are: (1) `call(**kwargs)` for each user dependency/endpoint (unavoidable), (2)
  pydantic-core validation (already Rust if FFI'd). Recursion → an explicit loop over the vector. The
  "values dict" → a pre-sized `kwargs` builder per node, materialized as a Python dict only at the
  moment of calling that node's Python callable. Errors → a Rust `Vec<ErrSpec>` flushed once to a
  Python 422. Async/sync → the kind enum on the op selects: inline-call / await / threadpool /
  enter-async-cm, dispatched in Rust.

---

## 5. Phase B — param extraction & validation internals (KNOWN, `utils.py:738-998`)

These are the functions `solve_dependencies` calls per node to turn ASGI request data into validated
kwargs. They run **every request, per node**.

### 5.1 `request_params_to_args()` — path/query/header/cookie (KNOWN, `utils.py:784-866`)

Handles three shapes: (a) ordinary list of scalar params, (b) a single Pydantic-model param that
absorbs *all* of that source ("models in query/header"), (c) the model-vs-flat distinction.

```
request_params_to_args(fields, received_params):
  if not fields: return {}, []                                  # :791-792  fast empty exit
  first = fields[0]
  single_not_embedded = (len(fields)==1 and issubclass(first.annotation, BaseModel))  # :798
  if single_not_embedded:
      fields_to_extract = get_cached_model_fields(first.annotation)   # :801  @lru_cache — model's sub-fields
      default_convert_underscores = first.field_info.convert_underscores (default True)  # :805
  # build params_to_process: pull each field's raw value out of the multidict
  for field in fields_to_extract:
      if received is Headers and convert_underscores:  alias = name.replace("_","-")  # :818-824
      value = _get_multidict_value(field, received_params, alias)     # :825
      if value is not None: params_to_process[validation_alias(field)] = value
      processed_keys.add(alias or validation_alias(field))
  for key in received_params.keys():                               # :830  pass through unknown keys
      if key not in processed_keys:
          # getlist() for multidict/headers, single value if len==1
          params_to_process[key] = ...                              # :831-839
  if single_not_embedded:                                           # :841  validate the whole dict as one model
      loc = (field_info.in_.value,)                                 # ('query',) etc.
      return {first.name: validate(first, params_to_process)}        # :847-850
  for field in fields:                                              # :852  ordinary path: per-field validate
      value = _get_multidict_value(field, received_params)
      loc = (field_info.in_.value, validation_alias(field))          # ('query','q')
      v_, errs = _validate_value_with_model_field(field, value, values, loc)  # :859
      if errs: errors.extend(errs)
      else: values[field.name] = v_
  return values, errors
```

`_get_multidict_value` (`utils.py:753-781`): if the field annotation is a sequence and the container is
a multidict/`Headers`, use `.getlist(alias)` (repeated query params → list); else `.get(alias)`.
Empty-string Form value, empty sequence, or missing → return field default (`deepcopy(field.default)`,
`:780`) or `None` if required. **`deepcopy` of the default per missing optional per request** is a
quiet cost — Lancelot should clone defaults cheaply (defaults are usually immutable; deepcopy only if
mutable, or freeze and share).

`get_validation_alias` (`utils.py:1055-1057`): `field.validation_alias or field.alias`. The alias is
the wire name (e.g. header `X-Token` vs param `x_token`; underscore→dash conversion at `:818-824` and
set once at registration `:537`).

**Header underscore→dash** (`:537` registration sets `alias = name.replace("_","-")` when
`convert_underscores`; `:818-824` re-applies for model sub-fields). Lancelot must replicate: header
param `user_agent` reads `User-Agent`.

### 5.2 `request_body_to_args()` — JSON & form bodies (KNOWN, `utils.py:951-998`)

```
request_body_to_args(body_fields, received_body, embed_body_fields):
  single_not_embedded = (len(body_fields)==1 and not embed_body_fields)        # :959
  first = body_fields[0]
  if single_not_embedded and issubclass(first.annotation, BaseModel) and isinstance(received_body, FormData):
      fields_to_extract = get_cached_model_fields(first.annotation)            # :970  model from form
  if isinstance(received_body, FormData):
      body_to_process = await _extract_form_body(fields_to_extract, received_body)  # :973  reads UploadFiles
  if single_not_embedded:                                                       # :975
      return {first.name: validate(first, body_to_process, loc=("body",))}      # whole body = one model
  for field in body_fields:                                                     # :981  embedded: per-key
      loc = ("body", validation_alias(field))
      value = body_to_process.get(validation_alias(field)) if dict-like else error
      v_, errs = _validate_value_with_model_field(field, value, values, loc)
      ...
```

`_extract_form_body` (`utils.py:912-948`): for each form field, `_get_multidict_value`; if it's a
`File` with `bytes` annotation → `await value.read()` (reads the whole upload into memory!); if a
sequence of bytes → read each. Unknown form keys are passed through (`:941-947`). **`await
value.read()` per file per request** is unavoidable for bytes-typed file params; Lancelot should stream
by default and only buffer when the annotation is `bytes`.

`_validate_value_with_model_field` (`utils.py:738-746`): if value is `None` → required ⇒
missing-field error, else `deepcopy(field.default)`; otherwise `field.validate(value, values,
loc=loc)` → pydantic-core. **`field.validate` is the only place pydantic runs per request for
params/body.**

### 5.3 Body reading happens in `get_request_handler`, before `solve_dependencies` (`routing.py:401-449`)

The handler reads the body *once* per request (form → `await request.form()` and registers
`body.close` on the **middleware** exit stack `:407`; else `await request.body()` then content-type
sniff: `application/json` or `*+json` → `await request.json()`, else raw bytes). `JSONDecodeError` →
422 with `type:"json_invalid"` and the byte position. `strict_content_type` (default True) means a
body with no `Content-Type` is *not* JSON-parsed unless disabled. The parsed `body` is then passed into
`solve_dependencies(body=...)`. Lancelot: parse body in Rust (simd-json), pass a borrowed view to
validators; only materialize Python objects for fields actually consumed.

### 5.4 Perf + Lancelot lowering

- **(b) per-request cost:** per node, up to 4 `request_params_to_args` + 1 `request_body_to_args`;
  each builds intermediate dicts (`params_to_process`, `values`), iterates `received_params.keys()`
  (O(all query/header keys) even to read one param — the unknown-key passthrough loop `:830`), and
  calls pydantic `validate_python` per field. `deepcopy` per missing optional. `get_cached_model_fields`
  is `@lru_cache` so model-field extraction is amortized.
- **(c) steal:** the `@lru_cache` on model field lists; one body read per request; collect-all-errors;
  the model-absorbs-a-whole-source feature (query model, header model).
- **(d) avoid:** iterating all request keys per param-bucket; intermediate dict churn; `deepcopy` of
  defaults; reading full uploads into memory unless bytes-typed.
- **(e) Rust/codegen:** the param-extraction plan per node becomes `Vec<Extract { source:
  Query|Path|Header|Cookie|Body, wire_name: InternedStr, validator: ValidatorId, default:
  Option<PyObj>, is_seq: bool, dest_slot: u32 }>`. Rust reads the value directly from the ASGI
  scope's already-parsed structures (Starlette parses headers/query; Lancelot would own this in Rust),
  runs the compiled validator, writes the slot — zero Python dicts until the node's callable is
  invoked. The unknown-key passthrough is only needed for the "Pydantic model absorbs the whole source"
  feature → gate it behind a per-node flag set at registration.

---

## 6. `yield` dependencies & the `AsyncExitStack` teardown machine (KNOWN) — load-bearing for AI

This is FastAPI's most subtle and most AI-relevant subsystem: resources (DB sessions, transactions,
LLM clients, file handles, spans) that must be set up before the endpoint and torn down *after* — with
correct ordering relative to the response being sent.

### 6.1 Three exit stacks per request, nested (KNOWN, `routing.py` + `middleware/asyncexitstack.py`)

| Stack (scope key) | Entered at | Lifetime | Used for |
|---|---|---|---|
| `fastapi_middleware_astack` | `AsyncExitStackMiddleware.__call__` `middleware/asyncexitstack.py:16` | whole middleware stack (outermost) | closing uploaded form files (`routing.py:407` `file_stack.push_async_callback(body.close)`) |
| `fastapi_inner_astack` | inner `request_response.app` `routing.py:116-117` | request-scoped | default for `yield` deps with `computed_scope=="request"`; SSE producer ctx-mgr (`routing.py:590`) |
| `fastapi_function_astack` | immediately inside inner `routing.py:118-119` | function-scoped (innermost) | `yield` deps explicitly `scope="function"` |

The nesting (`routing.py:113-124`):

```python
async with AsyncExitStack() as request_stack:               # :116  #2 request-scoped
    scope["fastapi_inner_astack"] = request_stack
    async with AsyncExitStack() as function_stack:          # :118  #3 function-scoped (inner)
        scope["fastapi_function_astack"] = function_stack
        response = await f(request)                          # :120  endpoint runs HERE (inside both)
    await response(scope, receive, send)                     # :121  body sent AFTER function_stack closes, BEFORE request_stack closes
    response_awaited = True
```

**Teardown ordering (the whole point):**
- `scope="function"` yield deps finalize **before the response body is sent** (function_stack `__aexit__`
  runs at `:120`→`:121` boundary). Use for: things that must commit/close before the client sees bytes
  (e.g. DB transaction commit you want reflected in the response, or a resource you must not hold
  during streaming).
- `scope="request"` yield deps (the default) finalize **after the response body is fully sent**
  (request_stack `__aexit__` runs after `:121`). Use for: request-scoped DB session, request-scoped
  LLM client, anything safe to hold until the client is done — **including resources a streaming
  response keeps using while streaming**.
- middleware stack closes uploaded files outermost/last.

The `response_awaited` guard (`routing.py:124-131`) raises a `FastAPIError` if the response was never
awaited — catches the classic "a `yield` dep swallowed the exception with bare `except`" bug.

### 6.2 `_solve_generator()` — enter a yield dep as a context manager (KNOWN, `utils.py:578-586`)

```python
async def _solve_generator(*, dependant, stack: AsyncExitStack, sub_values):
    if dependant.is_async_gen_callable:
        cm = asynccontextmanager(dependant.call)(**sub_values)                       # async def ... yield
    elif dependant.is_gen_callable:
        cm = contextmanager_in_threadpool(contextmanager(dependant.call)(**sub_values))  # def ... yield → threadpool
    return await stack.enter_async_context(cm)                                       # registers __aexit__ on the chosen stack
```

So a `def get_db(): db=...; yield db; db.close()` becomes `contextmanager(get_db)` wrapped in
`contextmanager_in_threadpool` (sync teardown runs in the threadpool) and entered on
`request_astack` (default) or `function_astack` (if `scope="function"`). The value yielded is what's
injected. The teardown (`finally`/post-`yield` code) runs when that stack unwinds — i.e. at the
lifetime boundary in §6.1. **The cache (`utils.py:683`) stores the yielded value, so a yield dep shared
across sub-deps is entered once per request and torn down once.**

### 6.3 Streaming-scoped resources — the PEP 789 fix (KNOWN, `routing.py:529-619`)

The hardest case: an SSE/JSONL endpoint that is itself an async generator, whose body streams for
seconds/minutes, and which uses a `yield` dep (DB/LLM client) that must stay open *for the whole stream*
and close *after* the stream ends — **not** when the generator is garbage-collected.

FastAPI's solution (SSE path, `routing.py:534-595`): the generator is consumed by a **producer task**
inside an `@asynccontextmanager` (`_sse_producer_cm`) that is entered on the **request-scoped exit
stack** (`async_exit_stack.enter_async_context(_sse_producer_cm())` `:590`). A memory object stream
(`anyio.create_memory_object_stream`, buffer 1) decouples generation from the keepalive timer; a second
task inserts keepalive comments on `anyio.fail_after(_PING_INTERVAL)` timeout (`:561-578`). The comment
at `:546-551` and `:586-589` is explicit: *the context manager's `__aexit__` is called by the exit
stack after the streaming response completes — not by async-generator finalization via `GeneratorExit`*
(ref PEP 789). So a request-scoped `yield` DB session injected into the streaming generator stays open
through the whole stream and is closed by `request_stack.__aexit__` **after** the last byte is sent
(`routing.py:121` is after `:590`'s stack, request_stack closes last).

JSONL path (`:620-652`) and raw streaming path (`:653-672`) are simpler (no keepalive) but the same
exit-stack ownership principle holds: the generator is driven inside the request lifetime, deps close
after streaming. `_async_stream_jsonl` inserts `await anyio.sleep(0)` after each item
(`routing.py:634`) so cancellation can be delivered mid-stream (issue #14680).

**This is exactly the primitive an AI backend needs:** stream LLM tokens via an async-generator
endpoint, inject a request-scoped DB session / vector-store client / HTTP client to the model provider
as a `yield` dep, and trust it stays alive for the whole stream and closes cleanly after — even on
client disconnect (the exit stack unwinds on cancellation). Lancelot **must** preserve this exact
ordering or AI streaming apps will leak connections or close sessions mid-stream.

### 6.4 Perf + Lancelot lowering

- **(b) per-request cost:** **two `AsyncExitStack()` objects constructed every request** even with
  zero yield deps (`routing.py:116,118`) — `AsyncExitStack` is cheap but not free (object + internal
  deque). `_solve_generator` wraps each yield dep in `(async)contextmanager` + `enter_async_context`
  per request. SSE path spins a task group + 2 memory streams + 2 tasks per streaming request.
- **(c) steal:** the exact 3-stack model and teardown ordering; the request/function scope split; the
  PEP-789 "drive the generator inside the request lifetime, not via GC" pattern; the `response_awaited`
  guard; `anyio.sleep(0)` checkpoints for cancellation.
- **(d) avoid:** unconditionally allocating exit stacks when the compiled plan has **zero** yield deps
  (registration knows this — set `route.has_yield_deps`; skip stack creation entirely if false). Avoid
  the threadpool wrap for sync-yield-dep teardown if teardown is trivial.
- **(e) Rust/codegen:** registration determines, per route, the set of yield deps and their scopes →
  emit a static teardown schedule: `Vec<TeardownOp{ slot, scope, is_async }>`. Rust holds two
  drop-stacks (request, function) as `Vec`; on the function/request boundary it runs the schedule in
  LIFO. The generator-streaming case: Rust owns the producer task + bounded channel (tokio/async
  equivalent) and the drop-stack is unwound by Rust *after* the response future completes — same
  ordering, no Python `AsyncExitStack`, no `contextmanager` wrapper allocation. Only the user yield
  function's pre-`yield` and post-`yield` segments run in Python (call `next()`/`asend()` twice).

---

## 7. `params.py` / `param_functions.py` / `Depends` / `Annotated` parsing (KNOWN)

### 7.1 The param marker hierarchy (`params.py:19-754`)

```
FieldInfo (pydantic)
└── Param (params.py:26)            in_: ParamTypes              __repr__ = "Class(default)"
    ├── Path   (:137)  in_=path     assert default is ...        (path params have no default; :185)
    ├── Query  (:221)  in_=query
    ├── Header (:303)  in_=header   + convert_underscores=True (:352)
    └── Cookie (:387)  in_=cookie
FieldInfo
└── Body (:469)        media_type="application/json", embed
    └── Form (:581)    media_type="application/x-www-form-urlencoded"
        └── File (:663) media_type="multipart/form-data"

@dataclass(frozen=True)
class Depends:                       # params.py:745-749
    dependency: Callable | None = None
    use_cache: bool = True
    scope: Literal["function","request"] | None = None

@dataclass(frozen=True)
class Security(Depends):             # params.py:752-754
    scopes: Sequence[str] | None = None
```

`ParamTypes` (`params.py:19-23`): `Enum query|header|path|cookie`. `Param.__init__` (`:29-131`) is a
huge kwargs funnel into `FieldInfo` (validation constraints `gt/ge/lt/le/min_length/max_length/pattern`,
`alias`, `validation_alias`, `serialization_alias`, `examples`, `deprecated`, `json_schema_extra`).
Constants worth noting: `serialization_alias` defaults to `alias` if alias is a str (`:113-114`);
`validation_alias` defaults to `alias` (`:115-116`); `pattern = pattern or regex` (regex deprecated,
`:127`); values equal to the `_Unset` sentinel are stripped (`:129`). **All of this executes once when
the user writes `Query(...)` at module import — never per request.**

**`Depends`/`Security` are `@dataclass(frozen=True)`** — immutable, hashable. `analyze_param` `copy()`s
them before mutating (`utils.py:482`). The whole `Depends` API surface is *three fields*: the callable,
`use_cache`, `scope`. `Security` adds `scopes`. That is the entire dependency-declaration vocabulary.
Lancelot can mirror this exactly — it's minimal and proven.

### 7.2 `param_functions.py` — 2460 lines, ~95% docstrings

`Path/Query/Header/Cookie/Body/Form/File/Depends/Security` here are **functions** that just forward to
the `params.*` classes (e.g. `param_functions.py:2369` `return params.Depends(dependency=dependency,
use_cache=use_cache, scope=scope)`; `:2460` `return params.Security(...)`). They exist so users can
write `Annotated[str, Query(max_length=10)]` with full IDE signatures + `Doc()` strings. Zero runtime
significance beyond constructing the marker. **Lancelot: keep the ergonomic function wrappers (great
DX), but they must produce immutable spec objects consumed by the Rust compiler.**

### 7.3 `Annotated` parsing rule (the canonical compat contract — from `analyze_param`, §2.3)

- `Annotated[T, m1, m2, ...]`: `T` is the type; metadata items that are `FieldInfo` or `params.Depends`
  are FastAPI markers; among those, only `Param`/`Body`/`Depends` subtypes count; **the last one
  wins** (`utils.py:431-433`).
- A default value via `=` is allowed *with* `Annotated` only for `FieldInfo` markers (sets
  `field_info.default = value`, `:450-452`); a `FieldInfo` marker may **not** carry its own default
  inside `Annotated` (assert `:444-449`).
- `Depends` may be in `Annotated` **or** as the `=` default, never both (assert `:460-463`).
- `Depends()` with no callable → the parameter's **type** becomes the dependency (class injection),
  `utils.py:480-483`.
- PEP 695 `type X = ...` aliases are unwrapped via `is_typealiastype(...).__value__`
  (`utils.py:404-406`).

Lancelot must implement this resolution order identically for drop-in FastAPI-style code.

---

## 8. Security dependencies (KNOWN, `fastapi/security/*`) — they are just callables

There is **no special security resolver**. A security scheme is an object that (a) subclasses
`SecurityBase` (`security/base.py:4-6`: just `model: SecurityBaseModel; scheme_name: str`), and (b) is
callable. It is used as `Depends(oauth2_scheme)` / `Security(scheme, scopes=[...])`. `solve_dependencies`
calls it like any other dep. The *only* differences vs a plain dep:
1. `Dependant._is_security_scheme` is True (`models.py:86-91`, isinstance check on unwrapped call) →
   contributes to `_uses_scopes` → its `cache_key` includes the scope tuple.
2. It's surfaced in the OpenAPI `securitySchemes` (out of scope here).

### 8.1 The actual call bodies (all are `async def __call__(self, request) -> ...`)

- **`OAuth2` / `OAuth2PasswordBearer`** (`oauth2.py:423-430`, `:536-544`): read
  `request.headers["Authorization"]`; for Bearer, `get_authorization_scheme_param` splits
  `"Bearer xxx"` on the first space (`security/utils.py:1-7`); if missing/!bearer → if `auto_error`
  raise `HTTPException(401, "Not authenticated", {"WWW-Authenticate":"Bearer"})` else return `None`.
- **`OAuth2AuthorizationCodeBearer`** (`oauth2.py:642-650`): same Bearer extraction.
- **`APIKeyQuery/Header/Cookie`** (`api_key.py:142-146`, `:230-234`, `:318-322`): read
  `request.query_params.get(name)` / `request.headers.get(name)` / `request.cookies.get(name)`;
  missing → 403 (`auto_error`) or `None`.
- **`HTTPBasic`** (`http.py:202-...`): parse `Authorization: Basic <b64>`, base64-decode,
  split `user:pass` → `HTTPBasicCredentials`; on failure 401 with `WWW-Authenticate: Basic`.
- **`HTTPBearer`/`HTTPDigest`** (`http.py:303-...`, `:404-...`): split scheme/credentials →
  `HTTPAuthorizationCredentials(scheme, credentials)`.
- **`OpenIdConnect`** (`open_id_connect_url.py:87-...`): read `Authorization`; missing → 403/None.

All are tiny header/query/cookie reads + an `HTTPException` on the `auto_error` path. The `auto_error`
flag is the universal "optional auth" switch (return `None` instead of raising).

### 8.2 `SecurityScopes` injection (KNOWN, `oauth2.py:653-694`, `utils.py:725-728`)

`SecurityScopes` is a special non-callable injection (handled by
`add_non_field_param_to_dependency`, `utils.py:380-382`). At resolve time
`values[security_scopes_param_name] = SecurityScopes(scopes=dependant.oauth_scopes)` — the merged
ancestor+own scope list (`models.py:53-60`). `SecurityScopes.__init__` also precomputes
`scope_str = " ".join(scopes)` (`oauth2.py:693`). So a dep that declares
`security_scopes: SecurityScopes` receives every scope required anywhere in its dependency chain — the
mechanism behind FastAPI's OAuth2-scopes tutorial.

### 8.3 Perf + Lancelot lowering

- **(b) per-request cost:** one extra Python callable per security dep + (for HTTPBasic) a base64
  decode; cached by `cache_key` so a scheme reused across sub-deps runs once/request.
- **(c) steal:** "security = a callable that returns the principal or raises 401/403"; `auto_error`
  optional-auth switch; scope propagation via the `Dependant` tree; `SecurityScopes` aggregation.
- **(d) avoid:** nothing major — these are already minimal. Don't special-case security in the resolver
  (FastAPI doesn't; it's just a dep). The only inefficiency is they're Python callables doing a header
  `.get()`; trivial to move the common ones (Bearer/API-key extraction) into Rust pre-steps.
- **(e) Rust/codegen:** the standard schemes (Bearer/API-key/Basic) are pure header/query/cookie
  parsing → implement as **built-in Rust extractors** that produce the token string/credentials object
  and short-circuit to a 401/403 in Rust before any Python frame. User-custom schemes stay Python
  callables. Scope merge + `SecurityScopes` object built in Rust at registration (static) and only
  materialized to a Python object if a node actually declares the param.

---

## 9. THE SPLIT — exactly what is done once (registration) vs every request (the optimization map)

This table is the entire Lancelot optimization thesis. Left column = move to Rust/codegen, do once.
Right column = the per-request scaffolding Lancelot replaces with Rust.

### 9.1 Done ONCE per route at import (Phase A — `APIRoute.__init__`)

| Work | Source | Cost (one-time) | Lancelot |
|---|---|---|---|
| `inspect.signature(call, eval_str=True)` for endpoint + every dep | `utils.py:213-242` (`get_typed_signature`) | high (string-annotation eval, globalns) | do once; emit a Rust plan; never re-introspect |
| Path-param name set `re.findall("{(.*?)}", path)` | `utils.py:43-44` | trivial | compute once, store interned |
| `analyze_param` per parameter (classify query/path/header/cookie/body/dep/special) | `utils.py:393-559` | medium | the *core* compile step → emit `ParamSpec` |
| Recursive `get_dependant` tree build | `utils.py:286-359` | medium (recursion over all deps) | build flat topo plan once |
| Callable-kind detection (`is_coroutine/gen/async_gen_callable`) | `models.py:105-185` (~75 L of `inspect`) | medium per callable | classify once → 2-bit enum in plan |
| `cache_key` 3-tuple per node, `_uses_scopes`, `oauth_scopes`, `computed_scope` | `models.py:53-71,73-84,187-193` | trivial, cached | assign integer cache-slot ids |
| `TypeAdapter` schema compile per `ModelField` (pydantic-core) | `_compat/v2.py:141-166` | **high** (Rust schema gen) | compile validator once; reuse handle forever |
| Synthetic body model `create_model("Body_…")` + its TypeAdapter | `utils.py:1001-1052`, `_compat/v2.py:387-392` | high | generate Rust struct deserializer once |
| `get_flat_dependant`, `_should_embed_body_fields`, `get_body_field` | `utils.py:138-210,888-909,1001-1052` | medium | compute body-shape flags once |
| Header underscore→dash alias resolution | `utils.py:537` | trivial | bake wire-name into ParamSpec |
| `get_cached_model_fields` (`@lru_cache`) for model-typed params | `_compat/v2.py:413-415` | once per model proc-wide | same: cache field list per type |
| Scope-mismatch static check (request-dep ⇏ function-dep) | `utils.py:319-329` | trivial | static validation at plan compile |
| Stream classification (`is_sse_stream/is_json_stream`) | `routing.py:967-975` | trivial | plan flag |
| Build the closed-over request handler | `routing.py:976-...` | trivial | Rust dispatch fn |
| Decide `route.has_yield_deps` / `needs_response` (DESIGNED — FastAPI doesn't, but trivially derivable from the tree) | — | — | skip exit-stack/Response alloc when false |

### 9.2 Done EVERY request (Phase B — the hot path)

| Work | Source | Per-request cost | Lancelot replacement |
|---|---|---|---|
| Allocate 2–3 `AsyncExitStack`s | `routing.py:116,118`, `asyncexitstack.py:16` | object + deque ×2–3, **even with 0 yield deps** | skip if `has_yield_deps==false`; else Rust drop-stacks |
| Read + parse body (form/json/bytes) + content-type sniff | `routing.py:401-449` | I/O + json parse + email.Message parse | Rust simd-json; borrowed view |
| Recursive `solve_dependencies` coroutine, one frame/node | `utils.py:598-735` | Python coroutine frame per DAG node | flat Rust loop over plan vector |
| Fresh `Response()` (+ header del + status sentinel) | `utils.py:622-625` | object alloc/request | lazy: only if `needs_response` |
| Fresh `dependency_cache` dict + tuple-hash per get/set | `utils.py:626,664,683` | dict + tuple hashing/request | dense `Vec<Option<PyObj>>` by slot id |
| 4× `request_params_to_args` + 1× `request_body_to_args` per node | `utils.py:685-712` | dict churn, iterate all req keys | Rust extract ops, no intermediate dicts |
| `field.validate` (pydantic `validate_python`) per field | `_compat/v2.py:173-188` | Rust (fast) but Python wrapper frame | call pydantic-core FFI directly / Lancelot Rust validator |
| `deepcopy(field.default)` per missing optional | `utils.py:745,780` | deepcopy/request | share immutable defaults; clone only if mutable |
| `run_in_threadpool` per sync dep + sync endpoint | `utils.py:680`, `routing.py:330` | thread round-trip (anyio limiter) | inline fast sync; opt-in `blocking=True` only |
| `await call(**values)` per dep + endpoint | `utils.py:678`, `routing.py:328` | **unavoidable** (user Python) | keep in Python; minimize surrounding overhead |
| `_solve_generator` wrap (`contextmanager`+`enter_async_context`) per yield dep | `utils.py:578-586` | wrapper objects/request | Rust-driven 2-step generator pump |
| Build `values` dict per node + `.update()`×4-5 | `utils.py:697-728` | dict alloc + updates/request | build kwargs once, materialize dict only at call |
| `SecurityScopes(...)` object if declared | `utils.py:725-728` | object/request (rare) | materialize only if node declares it |
| Special-injection dict assignments (request/response/bg/ws) | `utils.py:713-728` | a few dict sets | Rust slot binds |
| **Dependency-override: full `get_dependant()` rebuild** | `utils.py:641-647` | **graph recompile/request** when overrides active | precompile override variant on assignment; never in hot path |

### 9.3 The one-sentence rule

> Everything that depends only on the *route's code shape* (signature, types, dep graph, validators,
> callable kinds, cache keys, body model, scopes) is computed **once**; everything that depends on the
> *request's data* (values, validation execution, callable invocation, resource lifetimes) is **per
> request**. FastAPI already draws this line correctly — its inefficiency is purely that the
> per-request side runs in interpreted Python with per-node recursion, dict churn, and threadpool hops.
> **Lancelot's entire speedup = keep FastAPI's split, but execute the per-request side in Rust, calling
> into Python only for the user's dependency/endpoint callables and (optionally) pydantic-core.**

---

## 10. AI-backend DI patterns + the Lancelot blueprint

### 10.1 DI patterns for AI workloads (how FastAPI's primitive maps to LLM/agent backends)

- **Per-request LLM client / HTTP client to a model provider:** a plain `Depends(get_llm_client)`
  (cached, so shared across sub-deps in one request → one client per request). If it owns a connection
  pool, make it a module singleton returned by the dep (don't recreate per request). If it must close
  (e.g. a per-request `httpx.AsyncClient`), make it a **`yield` dep, `scope="request"`** so it closes
  after the response/stream completes (§6.1).
- **Per-request DB session / transaction:** classic `yield` dep. `scope="request"` (default) → session
  closes after response sent — correct for non-streaming. For "commit must be visible before response
  is serialized", use `scope="function"` so teardown (commit) runs *before* the body is sent
  (`routing.py:120→121`). The scope-mismatch guard (`utils.py:319-329`) statically prevents a
  request-scoped dep from depending on a function-scoped one.
- **Auth / principal:** a `SecurityBase` callable (Bearer/API-key) as `Security(scheme,
  scopes=[...])`; scopes propagate up the tree; a downstream `authorize` dep reads `SecurityScopes`.
  Cached per request → token parsed once even if 5 sub-deps need the user.
- **Rate limiting:** a parameterless route-level `dependencies=[Depends(rate_limit)]` (name=None →
  side-effect only, raises 429). Runs first (prepended in declared order, §2.1) before any param
  validation cost is paid. Steal: this "cheap gate before expensive work" ordering.
- **Streaming-scoped resources (the big one):** an async-generator SSE/JSONL endpoint that yields LLM
  tokens, with a `scope="request"` `yield` dep providing the DB session / vector client / provider
  client. FastAPI guarantees (PEP 789, `routing.py:586-595`) the dep stays open for the entire stream
  and closes **after** the last token, even on client disconnect (exit stack unwinds on cancel).
  **Lancelot must reproduce this exactly** — it is the single most important correctness property for
  agent/RAG/streaming-chat backends. Getting it wrong = connection leaks or sessions closing
  mid-stream.
- **Background work after response:** `BackgroundTasks` injected (special slot, threaded through
  `solve_dependencies`); tasks run after response sent. For AI: kick off embedding/indexing/logging
  post-response without blocking the client.

### 10.2 Lancelot blueprint — the compiled resolution plan (DESIGNED)

Registration (`@app.get` decorator, Python, once) produces, via Rust:

```
struct ResolutionPlan {
    nodes: Vec<PlanNode>,          // topo-ordered (children before parents); index = node id
    cache_slots: u32,              // number of distinct cache keys (dense array size)
    has_yield_deps: bool,          // skip exit stacks entirely if false
    needs_response: bool,          // lazy Response alloc
    body: Option<BodyPlan>,        // simd-json field map → validator ids
    teardown: Vec<TeardownOp>,     // {slot, scope: Req|Fn, is_async} LIFO schedule
}
struct PlanNode {
    call: PyObject,                          // the user callable (Python — unavoidable)
    kind: CallableKind,                      // Sync | Async | SyncGen | AsyncGen  (2 bits)
    cache_slot: Option<u32>,                 // None if use_cache=false → private
    scope: Scope,                            // None | Request | Function
    params: Vec<ParamSpec>,                  // {source, wire_name(interned), validator_id, default, is_seq, dest}
    children: Vec<u32>,                      // node ids, declared order
    bind_name: Option<InternedStr>,          // None = route-level (compute, don't inject)
    special: SpecialBinds,                   // bitset: request/response/bg/ws/httpconn/securityscopes
}
```

Per request (Rust, hot path):
1. If `needs_response` → alloc Response; else skip. If `has_yield_deps` → init 2 Rust drop-stacks;
   else skip entirely.
2. Iterate `nodes` in topo order (no recursion). For each node: gather child results from
   `slots[cache_slot]`; extract+validate its params from the ASGI request **in Rust** (pydantic-core
   FFI or Lancelot Rust validators) into a kwargs builder; if `cache_slot` set and filled → reuse;
   else dispatch by `kind`: Sync→inline (or threadpool only if `blocking`), Async→await, Gen→pump
   2-step + push teardown op; store into `slots[cache_slot]`.
3. Accumulate validation errors in a Rust `Vec`; if non-empty after the plan → one 422 (matching
   FastAPI's `RequestValidationError` shape + `loc` tuples for drop-in error compat).
4. Materialize the endpoint kwargs dict (only now) → `await endpoint(**kwargs)` (Python).
5. Stream/serialize; on completion unwind function then request drop-stacks (LIFO) in Rust, running
   each yield dep's post-`yield` segment in Python. Streaming: Rust owns the producer task + bounded
   channel; drop-stack unwinds after the response future resolves (PEP-789 ordering preserved).

**Net Python crossings per request:** N user-callable invocations (deps + endpoint) + optional
pydantic-core (already Rust). **Everything else FastAPI does in Python — recursion, dicts, exit stacks,
param extraction, cache tuples, threadpool hops — is gone.** This is a 3–10× DI-overhead reduction
target for graphs of realistic depth (5–20 nodes), with the win growing with dep-graph size and
sync-dep count.

### 10.3 Compatibility contract (must match FastAPI byte-for-byte for drop-in adoption)

1. Param inference: scalar→query, complex/BaseModel→body, UploadFile→file, path-name→path
   (`utils.py:503-517`).
2. `Annotated` last-marker-wins; `=` default rules; `Depends()`-no-arg = class injection
   (`utils.py:411-483`).
3. Cache key = `(call, sorted(set(scopes)) or (), computed_scope or "")`; per-request scope
   (`models.py:62-71`).
4. yield-dep teardown ordering: function-scope before body sent, request-scope after; streaming keeps
   request-scope open through the stream (`routing.py:113-124,586-595`).
5. Collect ALL validation errors → single 422 with FastAPI-shaped `loc` tuples (`utils.py` +
   `routing.py:719-723`).
6. Header underscore→dash; repeated query/header → list when annotation is a sequence
   (`utils.py:537,759-762`).
7. `dependencies=[...]` run first in declared order, side-effect only (name=None)
   (`routing.py:952-956`, `utils.py:681`).
8. `auto_error` optional-auth semantics for security schemes (`security/*`).

Match these and existing FastAPI apps run on Lancelot unchanged while the DI overhead moves to Rust.

---

## 11. Evidence index (every file:line cited above is reproduced from the local checkout)

- `dependencies/models.py`: Dependant fields :31-51; oauth_scopes :53-60; cache_key :62-71; _uses_scopes
  :73-84; _is_security_scheme :86-91; is_gen/async_gen/coroutine_callable :105-185; computed_scope
  :187-193; _impartial/_unwrapped_call :18-28.
- `dependencies/utils.py`: get_parameterless_sub_dependant :123-135; get_flat_dependant :138-189;
  get_flat_params :204-210; get_typed_signature :228-242; get_dependant :286-359;
  add_non_field_param_to_dependency :362-383; analyze_param :393-559; add_param_to_fields :562-575;
  _solve_generator :578-586; SolvedDependency :589-595; solve_dependencies :598-735;
  _validate_value_with_model_field :738-746; _get_multidict_value :753-781; request_params_to_args
  :784-866; _should_embed_body_fields :888-909; _extract_form_body :912-948; request_body_to_args
  :951-998; get_body_field :1001-1052; get_validation_alias :1055-1057.
- `params.py`: ParamTypes :19-23; Param :26-134; Path :137; Query :221; Header :303 (+convert_underscores
  :352); Cookie :387; Body :469; Form :581; File :663; Depends :745-749; Security :752-754.
- `param_functions.py`: Depends :2283-2369; Security :2372-2460 (wrappers → params.*).
- `routing.py`: request_response/3-stack nesting :97-136; websocket_session :141-162;
  _extract_endpoint_context :250-274; run_endpoint_function :320-330; get_request_handler :351-729
  (body read :401-449; solve call :457-464; SSE :496-619; JSONL :620-652; raw stream :653-672;
  plain :673-718; 422 :719-723); get_websocket_app :732-766; APIWebSocketRoute.__init__ :769-802;
  APIRoute.__init__ :880-976.
- `_compat/v2.py`: ModelField :113-242 (__post_init__/TypeAdapter :141-166; validate :173-188;
  serialize_json :215-239); create_body_model :387-392; get_model_fields :395-410;
  get_cached_model_fields :413-415.
- `_compat/shared.py`: field_annotation_is_scalar :104-106; field_annotation_is_complex / sequence.
- `utils.py`: get_path_param_names :43-44; create_model_field :58-77.
- `types.py`: DependencyCacheKey :12; UnionType :10.
- `security/base.py` :4-6; `security/utils.py` :1-7; `security/oauth2.py` OAuth2 :330-431,
  OAuth2PasswordBearer :433-544, SecurityScopes :653-694; `security/api_key.py` :142-322;
  `security/http.py` :69-413; `security/open_id_connect_url.py` :87-90;
  `middleware/asyncexitstack.py` :8-18.
