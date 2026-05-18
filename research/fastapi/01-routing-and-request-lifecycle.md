# Lancelot Research — FastAPI Routing & Request Lifecycle (Source-Level Teardown)

> Source corpus: a *customized* FastAPI fork at `/Users/seventyleven/Desktop/researchfms/fastapi/fastapi/fastapi/` (version line ~0.136.x; has native SSE/JSONL streaming, `EndpointContext` error attribution, dual `AsyncExitStack` per request — NOT vanilla upstream) running on a *customized* Starlette fork resolved in the uv cache at `/Users/seventyleven/.cache/uv/archive-v0/x0H2cc61T9P9ptDKVkChi/starlette/` (Kludex's fork; `on_startup`/`on_shutdown` removed from core, `_DefaultLifespan` is a no-op there). `anyio` resolved at `/Users/seventyleven/.cache/uv/archive-v0/Y-lvyOqDDqHLmRdj9qYrM/anyio/`.
> Every `path:line` below was read line-by-line. KNOWN = literally in source. INFERRED = my performance analysis on top of the source.
> Lens: what to *steal* into Lancelot (Rust core + Python API, FastAPI ergonomics, Blazeio micro-opts), and what to *avoid*.

## Executive Summary — 10 lines

1. **The single biggest steal: build the per-route request→response coroutine ONCE at registration, freeze it as `route.app`.** FastAPI does `self.app = request_response(self.get_route_handler())` in `APIRoute.__init__` (`routing.py:976`); at request time the closure just runs — no signature introspection, no model-field building per request. Lancelot must do the same: registration-time compile, request-time execute.
2. **The single biggest bottleneck: O(N) linear route matching with a fresh regex `.match()` per route per request** (`starlette/routing.py:674-681` + `Route.matches` `:238-254`). 200 routes = up to 200 regex executions + 200 dict allocations *before* the handler runs. Lancelot's Rust core must replace this with a radix/prefix trie (matchit-style).
3. **Sync endpoints are punted to a 40-token anyio thread pool** (`CapacityLimiter(40)`, anyio `_backends/_asyncio.py:2957`). The 41st concurrent blocking handler *queues*. This is FastAPI's hardest concurrency cliff for AI backends that call blocking SDKs.
4. **Every request allocates ≥2 `AsyncExitStack`s + a `Request` object + a `child_scope` dict + a `path_params` dict + `MutableHeaders`**, plus 4 nested middleware coroutine frames minimum (ServerError→user→Exception→AsyncExitStack→Router). ~10-15 object allocations of pure framework overhead before user code.
5. **`@app.middleware("http")` is a trap**: it wraps every request in `BaseHTTPMiddleware` which spins up an `anyio.create_task_group()` + two memory-object streams + a `_CachedRequest` *per request* (`starlette/middleware/base.py:100-199`). Steal the pure-ASGI middleware pattern instead; never the dispatch pattern.
6. **Response serialization is double-encoded by default**: `jsonable_encoder` (recursive Python walk) → `json.dumps` (`starlette/responses.py:194-203`). The fork added a `dump_json` fast path that goes straight through Pydantic-core Rust (`routing.py:692-713`) — steal that, make it the *only* path.
7. **Streaming for AI: this fork is unusually good** — native SSE with a decoupled producer task + keepalive timer (`routing.py:534-609`), JSONL streaming, `anyio.sleep(0)` cancellation checkpoints. The architecture (memory stream between generator and socket) is the correct pattern; steal it wholesale, drop the threadpool fallback for sync generators.
8. **Lifespan is a merged async-CM chain** (`_merge_lifespan_context`, `routing.py:209-223`); `include_router` deep-nests them. Fine semantically, but state is a flat dict merge — steal the contract, keep state typed.
9. **Exception handling is 3 layered ASGI wrappers** with MRO walk per exception (`_lookup_exception_handler`, `_exception_handler.py:16-20`). Cheap on the happy path (try/except has ~0 cost until raised), so keep the structure; just collapse the 3 layers into 1.
10. **`include_router` flattens at registration** (re-creates every `APIRoute`, `routing.py:1732-1796`) — zero runtime mount cost for routers (good), but `Mount`/sub-apps re-run the whole match loop (bad). Lancelot: flatten routers always, never sub-app mount for first-party routes.

---

## 1. The full request lifecycle — every frame, in order (KNOWN)

This is the exact call chain for one HTTP request hitting a normal JSON endpoint. Each `→` is a coroutine `await` into a new frame. Cite is `file:line`.

```
ASGI server (uvicorn/granian/hypercorn) calls app(scope, receive, send)
│
├─ FastAPI.__call__                         applications.py:1156
│    sets scope["root_path"] if self.root_path; then super().__call__
│
├─ Starlette.__call__                       starlette/applications.py:84-88
│    scope["app"] = self
│    if self.middleware_stack is None: self.middleware_stack = build_middleware_stack()   ← LAZY, first request only
│    await self.middleware_stack(scope, receive, send)
│
├─ ServerErrorMiddleware.__call__           starlette/middleware/errors.py:149-185
│    wraps a `_send` that flips response_started; try/except around the whole app
│
├─ [user middleware 0..N]                   each is a coroutine frame
│
├─ ExceptionMiddleware.__call__             starlette/middleware/exceptions.py:45-62
│    scope["starlette.exception_handlers"] = (self._exception_handlers, self._status_handlers)
│    conn = Request(scope, receive, send)            ← ALLOC #1 (Request)
│    await wrap_app_handling_exceptions(self.app, conn)(scope, receive, send)
│
├─ wrap_app_handling_exceptions.wrapped_app  starlette/_exception_handler.py:28-62
│    defines a `sender` closure that tracks response_started; try/except → MRO handler lookup
│
├─ AsyncExitStackMiddleware.__call__        fastapi/middleware/asyncexitstack.py:15-18
│    async with AsyncExitStack() as stack:           ← ALLOC #2 (AsyncExitStack)
│      scope["fastapi_middleware_astack"] = stack
│
├─ Router.__call__ → Router.app             starlette/routing.py:656-710
│    if "router" not in scope: scope["router"] = self
│    for route in self.routes:                        ← ★ O(N) LINEAR SCAN
│       match, child_scope = route.matches(scope)     ← regex .match() per route, builds child_scope dict
│       if match == Match.FULL: scope.update(child_scope); await route.handle(...); return
│       elif PARTIAL: remember first (→ 405)
│    (no match) → redirect_slashes retry loop, then self.default (404)
│
├─ APIRoute.handle (= Route.handle)         starlette/routing.py:267-276
│    if methods and scope["method"] not in self.methods: raise HTTPException(405)
│    else: await self.app(scope, receive, send)        ← self.app frozen at registration
│
├─ fastapi.routing.request_response.app     fastapi/routing.py:110-134
│    request = Request(scope, receive, send)           ← ALLOC #3 (second Request)
│    inner app():
│      async with AsyncExitStack() as request_stack:   ← ALLOC #4 (fastapi_inner_astack)
│        scope["fastapi_inner_astack"] = request_stack
│        async with AsyncExitStack() as function_stack:← ALLOC #5 (fastapi_function_astack)
│          scope["fastapi_function_astack"] = function_stack
│          response = await f(request)                 ← f = get_request_handler closure
│        await response(scope, receive, send)          ← serialize + send
│      response_awaited guard (FastAPIError if dependency-with-yield swallowed exc)
│    wrapped by wrap_app_handling_exceptions AGAIN (fastapi/routing.py:134)  ← 2nd exc wrapper
│
├─ get_request_handler.app                  fastapi/routing.py:382-727
│    file_stack = scope["fastapi_middleware_astack"]    (assert)
│    endpoint_ctx = _extract_endpoint_context(...)      (cached by id(func))
│    body parse (form / json / bytes)        :402-449
│    async_exit_stack = scope["fastapi_inner_astack"]
│    solved_result = await solve_dependencies(...)      :457-464  ← DI resolver (own file, not in scope here)
│    if not errors:
│       branch: SSE | JSONL | raw-gen | normal
│       normal: raw = await run_endpoint_function(...)  :674-678
│               content = await serialize_response(...) :695-707
│               response = Response(...) or actual_response_class(...)
│    return response
│
└─ Response.__call__ / StreamingResponse.__call__   starlette/responses.py:163-171 / 257-285
     send http.response.start (status + raw_headers)
     send http.response.body (body bytes)
     if background: await self.background()             ← BackgroundTasks run AFTER body sent
```

### 1.1 Per-request allocation budget (INFERRED from the trace above)

Minimum framework overhead before user endpoint code runs, *per request*, for a plain `@app.get`:

| Allocation | Where | Cost class |
|---|---|---|
| `Request` #1 (ExceptionMiddleware) | `middleware/exceptions.py:58` | object + scope refs |
| `AsyncExitStack` (middleware) | `asyncexitstack.py:16` | object + `__aenter__/__aexit__` |
| `child_scope` dict (route match) | `starlette/routing.py:249` | dict alloc + `{endpoint, path_params}` |
| `path_params` dict copy | `starlette/routing.py:247-248` | `dict(scope.get(...))` + `.update` |
| `Request` #2 (request_response) | `fastapi/routing.py:111` | second Request for same req |
| `AsyncExitStack` inner | `fastapi/routing.py:116` | object |
| `AsyncExitStack` function | `fastapi/routing.py:118` | object |
| `endpoint_ctx` dict | `fastapi/routing.py:390` | cached (id-keyed) — cheap after warm |
| `solved_result` + values dict | `solve_dependencies` (other file) | dict + Pydantic validation |
| `Response` + `MutableHeaders` + `raw_headers` list | `starlette/responses.py:33-79` | object + list of tuples |
| 4-6 coroutine frames (middleware nest) | each `__call__` | frame alloc + suspension state |

**INFERRED:** ~12-16 heap allocations + ~6 coroutine frames of *pure framework tax* per request, before a single line of user code, for the simplest possible endpoint. This is the number Lancelot must beat. Robyn beats it by doing routing + (de)serialization in Rust and only crossing into Python once with already-parsed args. Blazeio beats it by never allocating a framework Request object at all (it hands you the raw protocol object).

### 1.2 What is done ONCE vs PER-REQUEST (the central optimization axis)

**Done once, at registration (`APIRoute.__init__`, `routing.py:812-976`) — STEAL ALL OF THIS:**
- `compile_path(path)` → `(path_regex, path_format, param_convertors)` — `routing.py:889`. Regex compiled once.
- `get_dependant(...)` — the full DI DAG built once — `routing.py:949`.
- `get_flat_dependant`, `_should_embed_body_fields`, `get_body_field` — `routing.py:957-965`.
- `create_model_field(response_model)` → Pydantic `ModelField` with a cached `TypeAdapter` — `routing.py:909`.
- `create_model_field(stream_item_type)` for SSE/JSONL — `routing.py:918`.
- `is_sse_stream` / `is_json_stream` flags resolved by inspecting the return annotation — `routing.py:966-975`.
- `self.unique_id = operation_id or generate_unique_id(self)` — `routing.py:899`.
- `self.app = request_response(self.get_route_handler())` — **the per-route coroutine is frozen here** — `routing.py:976`. `get_request_handler` closes over `dependant, body_field, status_code, response_class, response_field, *flags*` so none of that is recomputed per request.

**Done per-request (`get_request_handler.app`, `routing.py:382-727`) — MINIMIZE ALL OF THIS:**
- `_extract_endpoint_context` (cached by `id(func)` in module dict, `routing.py:251-274` — first call does `inspect.getsourcefile` + `getsourcelines`, then cached forever).
- body read + content-type sniff via `email.message.Message()` (a full email parser instantiated *per request* just to split a MIME type — `routing.py:417-422`, slow, see §7).
- `solve_dependencies` (re-walks the DAG, re-validates every param with Pydantic every request).
- `serialize_response` (Pydantic `.validate` + `.serialize` of the return value every request).
- `Response(...)` construction → `render()` → `init_headers()`.

**INFERRED:** FastAPI's registration-time freezing is the right primitive and Lancelot must keep it. The waste is that `solve_dependencies` + `serialize_response` re-do Pydantic validation every request even when the type is trivially `int`/`str`. Lancelot's Rust core should classify params at registration into "needs Pydantic" vs "primitive scalar — parse in Rust" and only cross into Python+Pydantic for the former.


## 2. Route registration & APIRoute construction (KNOWN — `routing.py:811-996`)

### 2.1 The decorator path: `@app.get("/x")` → `add_api_route` → `APIRoute(...)`

`@router.get(path, ...)` (`routing.py:1831`) returns a decorator that calls `self.api_route(...)` → `add_api_route(...)` (`routing.py:1336-1417`). `add_api_route` does the **merge of router-level and route-level config**, all at registration:

```python
# routing.py:1368-1417 (verbatim structure)
route_class = route_class_override or self.route_class           # APIRoute by default
combined_responses = {**self.responses, **responses}             # dict merge
current_response_class = get_value_or_default(response_class, self.default_response_class)
current_tags = self.tags.copy();  current_tags.extend(tags or [])
current_dependencies = self.dependencies.copy();  current_dependencies.extend(dependencies or [])
current_callbacks = self.callbacks.copy();  current_callbacks.extend(callbacks or [])
current_generate_unique_id = get_value_or_default(generate_unique_id_function, self.generate_unique_id_function)
route = route_class(self.prefix + path, endpoint=endpoint, ...all merged kwargs...)
self.routes.append(route)                                        # ← appended to a plain list
```

**KNOWN:** `self.routes` is a plain Python `list` (`starlette/routing.py:578` `self.routes = [] if routes is None else list(routes)`). Order of registration = order of matching. There is **no index, no trie, no method map** — pure append + linear scan.

**STEAL:** the registration-time config merge (router prefix/tags/deps/responses fold into each route once). This is clean and correct.
**AVOID:** the `list` of routes as the match structure. Lancelot's Rust core registers into a method-partitioned radix trie keyed on path segments; `{param}` and `{param:path}` become trie wildcards. Registration cost goes from O(1) append to O(path length) trie insert — negligible — and match cost goes from O(N routes × regex) to O(path length).

### 2.2 What `APIRoute.__init__` computes once (`routing.py:812-976`) — the registration-time compiler

In order:

1. `self.path`, `self.endpoint` stored (`:844-845`).
2. **Return-annotation analysis** (`:847-867`): if `response_model` is the `Default(None)` sentinel, introspect `get_typed_return_annotation(endpoint)`. If it returns a `Response` subclass → `response_model=None` (skip validation). Else `get_stream_item_type(return_annotation)` detects `AsyncIterator[T]`/`Iterator[T]`/`Generator` and, if `response_class` is the default placeholder or `EventSourceResponse` (and item ≠ `ServerSentEvent`), stores `self.stream_item_type = T`, sets `response_model=None`. Otherwise `response_model = return_annotation`. → **STEAL: derive streaming-ness and the item model from the type annotation at registration, never sniff at runtime.**
3. `compile_path(path)` → `(path_regex, path_format, param_convertors)` (`:889`). See §3.
4. `self.methods = {m.upper() for m in methods}` (`:892`), default `["GET"]`.
5. `self.unique_id = operation_id or generate_unique_id(self)` (`:899`) — used for OpenAPI + Pydantic model names. **INFERRED:** `generate_unique_id` does string formatting per route at registration — fine (once), but it's pure OpenAPI scaffolding; Lancelot can lazy-defer it until `/openapi.json` is requested.
6. Status-code enum normalization: `if isinstance(status_code, IntEnum): status_code = int(status_code)` (`:901-902`).
7. **Response field build** (`:904-913`): if `response_model`, assert `is_body_allowed_for_status_code`, then `self.response_field = create_model_field(name="Response_"+unique_id, type_=response_model, mode="serialization")`. This builds a Pydantic v2 `TypeAdapter`-backed `ModelField` once. Same for `stream_item_field` (`:916-922`) and each additional `responses[code].model` (`:930-946`).
8. `self.dependant = get_dependant(path=self.path_format, call=self.endpoint, scope="function")` (`:949`) — the full DI DAG (covered in the existing teardown's §4; not re-derived here).
9. Router-level `dependencies` prepended as parameterless sub-dependants in reverse order so they run first (`:952-956`).
10. `self._flat_dependant = get_flat_dependant(self.dependant)` (`:957`); `self._embed_body_fields = _should_embed_body_fields(self._flat_dependant.body_params)` (`:958`); `self.body_field = get_body_field(...)` (`:961`).
11. **Streaming flags resolved once** (`:966-975`): `is_generator = dependant.is_async_gen_callable or dependant.is_gen_callable`; `self.is_sse_stream = is_generator and lenient_issubclass(response_class, EventSourceResponse)`; `self.is_json_stream = is_generator and isinstance(response_class, DefaultPlaceholder)`.
12. **`self.app = request_response(self.get_route_handler())`** (`:976`) — freezes the entire per-request coroutine. `get_route_handler()` (`:978-996`) calls `get_request_handler(dependant=self.dependant, body_field=..., status_code=..., response_class=..., response_field=..., *all the exclude flags*, embed_body_fields=..., strict_content_type=..., stream_item_field=..., is_json_stream=...)`. Everything is closed over; nothing is looked up on `self` at request time.

**INFERRED — the key architectural lesson:** FastAPI pays a relatively heavy registration cost (Pydantic model-field construction, DI DAG walk, regex compile, annotation introspection) to make the request path a closure call. This is *exactly right* and Lancelot must keep this shape. The only registration waste worth removing: OpenAPI `unique_id` + model-name string building should be lazy (deferred to first `/openapi.json`), because most production deploys disable docs.

### 2.3 `get_route_handler` indirection (`routing.py:978-996`)

`get_route_handler` exists as a separate overridable method so users can subclass `APIRoute` and wrap the handler (the documented "custom APIRoute" pattern). It is called exactly once (`:976`). `run_endpoint_function` (`:320-331`) is *also* split into its own top-level function purely so endpoint time shows up as its own frame in profilers (`:323-324` comment: "split into its own function to facilitate profiling endpoints, since inner functions are harder to profile").

**STEAL:** the "one overridable hook that produces the frozen handler" pattern — gives extensibility with zero per-request cost. Keep `run_endpoint_function` as a named function for flamegraph clarity.
**AVOID:** nothing here — this is clean.

### 2.4 WebSocket route construction (`APIWebSocketRoute`, `routing.py:769-808`)

Mirror of `APIRoute` but simpler: `compile_path`, `get_dependant(scope="function")`, prepend router deps, `get_flat_dependant`, `_should_embed_body_fields`, then `self.app = websocket_session(get_websocket_app(dependant=..., ...))` frozen at construction (`:796-802`). `matches()` (`:804-808`) calls `super().matches()` and on a hit stamps `child_scope["route"] = self` (FastAPI adds this so the route object is reachable from scope; vanilla Starlette doesn't). `get_websocket_app` (`:732-766`) solves deps then `await dependant.call(**values)` — no response serialization, the user drives the socket.

**STEAL:** same freeze-at-registration discipline applies to WS. **Note for AI backends:** WS endpoints get the same DI resolution but no response model, so they're cheaper per message — good for token streaming over WS, though SSE (§7) is the more common LLM transport.


## 3. Path matching — the O(N) regex scan (KNOWN — `starlette/routing.py`) — ★ biggest bottleneck

### 3.1 `compile_path` (`starlette/routing.py:110-163`)

Registration-time. `PARAM_REGEX = re.compile("{([a-zA-Z_][a-zA-Z0-9_]*)(:[a-zA-Z_][a-zA-Z0-9_]*)?}")` (`:107`). For each `{name:conv}` in the path it builds:
- `path_regex` string: literal segments `re.escape`'d, params replaced with `(?P<name>{convertor.regex})`. Final `re.compile("^" + ... + re.escape(tail) + "$")`.
- `path_format`: `"/users/{user_id}"` (used for OpenAPI + `url_path_for`).
- `param_convertors`: `{name: Convertor}` — convertors are `str` (regex `[^/]+`), `int` (`[0-9]+`), `float`, `uuid`, `path` (`.*`, matches slashes).

Duplicated param names raise `ValueError` (`:149-152`). Host paths (no leading `/`) compile differently (`:154-159`).

**STEAL:** convertor types and their regexes (`str`→`[^/]+`, `path`→`.*`, `int`→`[0-9]+`, `uuid`, `float`). Lancelot's trie should encode these as typed wildcard nodes, not regex.
**AVOID:** one full anchored regex *per route*. That's the structure that forces a linear scan.

### 3.2 `Route.matches` — per request, per route (`starlette/routing.py:238-254`) — the hot loop body

```python
def matches(self, scope):
    if scope["type"] == "http":
        route_path = get_route_path(scope)               # strips root_path; cheap string slice
        match = self.path_regex.match(route_path)         # ★ FULL REGEX EXECUTION
        if match:
            matched_params = match.groupdict()            # dict alloc
            for key, value in matched_params.items():
                matched_params[key] = self.param_convertors[key].convert(value)  # per-param convert
            path_params = dict(scope.get("path_params", {}))   # dict copy
            path_params.update(matched_params)            # dict merge
            child_scope = {"endpoint": self.endpoint, "path_params": path_params}  # dict alloc
            if self.methods and scope["method"] not in self.methods:
                return Match.PARTIAL, child_scope         # → 405 later
            return Match.FULL, child_scope
    return Match.NONE, {}
```

FastAPI's `APIRoute.matches` (`fastapi/routing.py:998-1002`) wraps this: `super().matches()` then on non-NONE adds `child_scope["route"] = self`.

**The cost (INFERRED):** For an unmatched route, you still pay one `re.match` (the regex engine returns `None` fast for a literal-prefix mismatch, but `^/users/...$` vs `/orders/5` still enters the C regex). For a request to the **last** of N registered routes, the dispatcher (`Router.app`, `starlette/routing.py:674-681`) runs `route.matches` for routes `0..N-1` (all NONE) then `N` (FULL). That's N regex executions + (on the match) ~3 dict allocations + per-param `.convert()`. With 200 routes that's ~200 C-regex calls of pure dispatch overhead on every request. Worse: `redirect_slashes` (`:695-708`) on a 404 re-runs the *entire* loop a second time with a mutated path.

**Match enum** (`:40-43`): `NONE=0, PARTIAL=1, FULL=2`. `PARTIAL` = path matched but method didn't → remembered as `partial` and used to emit `405 Method Not Allowed` with an `Allow` header (`Route.handle`, `:268-274`). Only the *first* partial is kept (`:682-684`).

**STEAL:** the `NONE/PARTIAL/FULL` tri-state — it's how you distinguish 404 from 405 cheaply, and Lancelot's trie must reproduce it (path node exists but no method handler → 405 with `Allow`).
**AVOID:** linear scan + per-route regex + redirect-slashes double-scan. Lancelot Rust router: method-partitioned radix trie, single traversal, params captured into a small `SmallVec`/arena (no per-route dict). Target: O(len(path)) with zero Python allocation until a route is found — params handed to Python as one already-built dict, once.

### 3.3 How Robyn / Blazeio compare on this axis (INFERRED + KNOWN-from-prior-teardowns)

- **Robyn**: routing is done in Rust (`matchit` radix-trie router in the Rust core). The Python decorator just registers `(method, path, handler)` into the Rust router. Match is O(path) in Rust; Python is only entered with the handler + extracted params. This is the model Lancelot should copy. The cost FastAPI pays in `re.match`×N + dict×3 per request is *zero* in Robyn's hot path.
- **Blazeio**: from-scratch asyncio; routing is typically a `dict[path] -> handler` exact-match or a hand-rolled prefix check, no ASGI scope dict, no `Route` objects. Extremely low constant overhead but no typed path params / no trie for `{param:path}` semantics unless hand-built. Lancelot wants Robyn's trie *correctness* with Blazeio's *no-ceremony* request object.
- **Lancelot target**: Rust trie like Robyn; expose FastAPI's convertor/`Match` semantics (so the ergonomics transfer); hand Python a single pre-parsed `(handler, params_dict)` — never an ASGI `scope` linear scan.

### 3.4 The dispatcher loop & redirect-slash (`starlette/routing.py:662-710`)

`Router.app`: assert scope type ∈ {http, websocket, lifespan}; set `scope["router"]`; lifespan branch returns early. Then the `for route in self.routes` scan. After no FULL/PARTIAL match: if `redirect_slashes` and path≠`/`, build `redirect_scope` with toggled trailing slash and **re-run the entire route loop** to see if the slash-variant matches; if so emit a `RedirectResponse` (307). Else `self.default` (404, raises `HTTPException(404)` because `"app" in scope`, caught by `ExceptionMiddleware`).

**AVOID:** `redirect_slashes=True` default doubles worst-case dispatch cost on every 404. Lancelot: make trailing-slash a trie property (a node can mark "accepts optional trailing slash") so the redirect decision is O(1) at the matched node, not a second full scan. Or default it off.


## 4. `get_request_handler` — the frozen per-route coroutine (KNOWN — `routing.py:351-729`)

This is the most important function for Lancelot to mirror. It's a closure factory: called once at registration, returns `app(request) -> Response` which runs per request.

### 4.1 Closure setup (registration time, `routing.py:369-380`) — computed ONCE

```python
is_coroutine = dependant.is_coroutine_callable                       # bool, cached
is_body_form  = body_field and isinstance(body_field.field_info, params.Form)
actual_response_class = response_class.value if isinstance(response_class, DefaultPlaceholder) else response_class
is_sse_stream = lenient_issubclass(actual_response_class, EventSourceResponse)
actual_strict_content_type = strict_content_type.value if isinstance(strict_content_type, DefaultPlaceholder) else strict_content_type
```

**STEAL:** resolve every dispatch-affecting boolean (is the handler a coroutine? a generator? form body? SSE? strict CT?) **once**, into closure locals. At request time these are free reads, not `isinstance` calls. This is the core micro-opt and it's exactly what Blazeio does by hand and what Lancelot's codegen should emit.

### 4.2 Per-request body parsing (`routing.py:402-449`) — a hidden slow spot

```python
if body_field:
  if is_body_form:
    body = await request.form();  file_stack.push_async_callback(body.close)
  else:
    body_bytes = await request.body()
    if body_bytes:
      content_type_value = request.headers.get("content-type")
      if not content_type_value:
        if not actual_strict_content_type: json_body = await request.json()
      else:
        message = email.message.Message()           # ★ per-request email parser instance
        message["content-type"] = content_type_value
        if message.get_content_maintype() == "application":
          subtype = message.get_content_subtype()
          if subtype == "json" or subtype.endswith("+json"):
            json_body = await request.json()
      body = json_body if json_body != Undefined else body_bytes
```

**KNOWN bottleneck:** `email.message.Message()` is instantiated **per request** purely to parse a `Content-Type` header into maintype/subtype (`:417-419`). This is robust (handles `application/vnd.api+json`, params) but heavy — the `email` package builds a full MIME message object. Errors map to: `json.JSONDecodeError` → `RequestValidationError` with `type:"json_invalid"`, `loc:("body", e.pos)` (`:427-441`); `HTTPException` re-raised as-is (`:442-444`); any other exception → `HTTPException(400, "There was an error parsing the body")` (`:445-449`).

**STEAL:** the strict-content-type CSRF guard (`actual_strict_content_type`, default `True`): a body with no `Content-Type` is NOT parsed as JSON (`:413-415`) — defends against simple-request CSRF. Keep this default in Lancelot.
**AVOID:** `email.message.Message()` per request. Lancelot: parse content-type in Rust (split on `;`, lowercase, compare against a tiny set / suffix `+json`) — a few bytes of work, zero Python allocation. Cache the parsed `(maintype, subtype)` on the connection.

### 4.3 The four response branches (`routing.py:467-727`)

After `solved_result = await solve_dependencies(...)` (`:457-464`) and `if not errors:`:

**(a) SSE stream** (`is_sse_stream`, `:496-619`) — see §7. Calls `dependant.call(**values)`, builds a memory-stream producer + keepalive task, wraps in `StreamingResponse(media_type="text/event-stream")`, sets `Cache-Control: no-cache` and `X-Accel-Buffering: no`.

**(b) JSONL stream** (`is_json_stream`, `:620-652`) — `gen = dependant.call(**values)`; async-gen path yields `_serialize_item(item)+b"\n"` with `await anyio.sleep(0)` after each (cancellation checkpoint, ref issue #14680); sync-gen path iterates directly. Wrapped in `StreamingResponse(media_type="application/jsonl")`.

**(c) Raw generator with explicit response class** (`:653-672`) — e.g. user returns an async generator with `response_class=StreamingResponse`. Async-gen wrapped in `_async_stream_raw` (adds `anyio.sleep(0)` checkpoint per chunk, `:658-666`); `response = actual_response_class(content=gen, **response_args)`.

**(d) Normal** (`:673-718`) — `raw_response = await run_endpoint_function(dependant, values, is_coroutine)`. If it's already a `Response`, use it (attach `background_tasks` if none). Else build `response_args` from `_build_response_args` (`:333-348`: pulls `background_tasks`, resolves `status_code` from explicit arg → response-class default → `solved_result.response.status_code`), then:

```python
use_dump_json = response_field is not None and isinstance(response_class, DefaultPlaceholder)   # :692-694
content = await serialize_response(field=response_field, response_content=raw_response, ...,
                                   is_coroutine=is_coroutine, endpoint_ctx=endpoint_ctx,
                                   dump_json=use_dump_json)                                       # :695-707
if use_dump_json:
    response = Response(content=content, media_type="application/json", **response_args)         # :708-713  ★ FAST PATH
else:
    response = actual_response_class(content, **response_args)                                    # :714-715
if not is_body_allowed_for_status_code(response.status_code): response.body = b""                 # :716-717
response.headers.raw.extend(solved_result.response.headers.raw)                                   # :718  merge dep-set headers
```

After the branch, `if errors: raise RequestValidationError(errors, body=body, endpoint_ctx=endpoint_ctx)` (`:719-723`) — note errors are raised *after* the no-error branch is skipped; this ordering means validation errors short-circuit before any endpoint call.

**STEAL:**
- The `use_dump_json` fast path (`:692-713`): when no custom response class, serialize the return value **straight to JSON bytes via Pydantic-core's Rust serializer** (`field.serialize_json`, see §6) and wrap in a bare `Response` with `media_type="application/json"` — skips `actual_response_class.render()`'s second `json.dumps`. In Lancelot make this the *default and only* JSON path.
- `is_body_allowed_for_status_code` zeroing the body for 204/304/1xx (`:716-717`) — correctness detail many frameworks botch.
- Dependency-set response headers merged via `response.headers.raw.extend(...)` (`:718`) — lets a dependency set cookies/headers. Keep the contract; in Lancelot accumulate header mutations in a Rust-side header buffer instead of building `MutableHeaders`.
**AVOID:** the non-fast path that runs `jsonable_encoder` then `JSONResponse.render`'s `json.dumps` (double encode, §6).

### 4.4 `run_endpoint_function` — the sync/async fork (`routing.py:320-331`)

```python
async def run_endpoint_function(*, dependant, values, is_coroutine):
    assert dependant.call is not None
    if is_coroutine:
        return await dependant.call(**values)                 # async def → direct await
    else:
        return await run_in_threadpool(dependant.call, **values)   # def → 40-token threadpool
```

This is the single most consequential line for AI-backend concurrency. See §5.


## 5. Sync vs async endpoint handling — the 40-token threadpool cliff (KNOWN) — ★ critical for AI backends

### 5.1 The mechanism

A `def` (non-async) endpoint, dependency, exception handler, or background task is dispatched via `run_in_threadpool` (`starlette/concurrency.py:30-32`):

```python
async def run_in_threadpool(func, *args, **kwargs):
    func = functools.partial(func, *args, **kwargs)
    return await anyio.to_thread.run_sync(func)
```

`anyio.to_thread.run_sync` with **no `limiter=`** uses the per-event-loop default limiter. From anyio `_backends/_asyncio.py:2953-2958`:

```python
@classmethod
def current_default_thread_limiter(cls):
    try:    return _default_thread_limiter.get()
    except LookupError:
        limiter = CapacityLimiter(40)        # ★ HARD DEFAULT: 40 tokens
        _default_thread_limiter.set(limiter)
        return limiter
```

**KNOWN, exact constant: the default worker thread pool has 40 tokens.** The 41st concurrent blocking call *suspends on the limiter* until a token frees. This is process-wide per event loop, shared across *all* sync endpoints, sync deps, sync exception handlers, and `iterate_in_threadpool` for sync stream generators.

### 5.2 Why this is the FastAPI AI-backend killer (INFERRED)

LLM/agent backends overwhelmingly call **blocking SDKs** in practice: `openai` sync client, `anthropic` sync client, vector-DB drivers, LangChain sync chains, blocking HTTP libs, blocking DB drivers. If a developer writes:

```python
@app.post("/chat")
def chat(body: ChatReq):           # ← sync def, calls blocking openai client
    return openai.chat.completions.create(...)   # blocks ~2-30s for an LLM call
```

then **each in-flight chat request holds one of the 40 threads for the entire LLM latency** (seconds). 40 concurrent slow LLM calls saturate the pool; request 41+ queues *before even entering the handler*, adding unbounded latency. There is no backpressure signal to the client — it just hangs. This is the #1 production FastAPI scaling incident pattern for AI apps.

The async path (`async def` + `await openai_async_client...`) does not have this ceiling — it's bounded only by the event loop + the LLM provider's concurrency. But the framework happily accepts sync handlers and silently routes them through the 40-cap.

### 5.3 `iterate_in_threadpool` — sync generators are worse (`starlette/concurrency.py:39-58`)

```python
async def iterate_in_threadpool(iterator):
    as_iterator = iter(iterator)
    while True:
        try:    yield await anyio.to_thread.run_sync(_next, as_iterator)   # ★ ONE THREAD HOP PER ITEM
        except _StopIteration: break
```

`_next` coerces `StopIteration` → `_StopIteration` (can't propagate StopIteration across the thread boundary, `:35-46`). **Every single yielded chunk of a sync generator costs a full thread dispatch + a default-limiter token acquire/release.** For an LLM token stream (hundreds–thousands of chunks) via a *sync* generator, that's hundreds of thread round-trips and token churn — catastrophic. The fork's SSE path uses this for sync generators (`routing.py:532` `sse_aiter = iterate_in_threadpool(gen)`).

### 5.4 `contextmanager_in_threadpool` (FastAPI `concurrency.py:17-41`)

Sync `with`-style yield-dependencies: `__enter__` runs under the default limiter, but `__exit__` runs under a **fresh `CapacityLimiter(1)`** (`:27`) — deliberately *not* the shared pool, to avoid deadlock when the CM owns its own pool (e.g. a DB connection pool). Comment `:21-26` explains: blocking `__exit__` waiting on a free shared thread can deadlock if the CM internally needs a thread. **STEAL this reasoning** — teardown of sync resources must not contend for the same bounded pool that the resource itself may need.

### 5.5 What to steal / avoid / how Rust-core wins

**STEAL:** the *contract* — let users write `def` handlers and have them not block the loop. That ergonomic is why FastAPI won; Lancelot must keep "sync handlers just work."
**AVOID:**
- The invisible 40-token global cap with no backpressure and no per-route override surfaced. If Lancelot offloads sync work, the pool size must be configurable per-app and per-route, and the framework must emit an explicit 503/`Retry-After` when the offload queue is saturated rather than hanging.
- `iterate_in_threadpool` one-hop-per-item for streams. Lancelot must require async generators for streaming, or drain a sync generator in a single dedicated thread that pushes into an async queue (one thread for the whole stream, not one hop per chunk).

**Rust-core (Robyn) comparison:** Robyn runs a Rust async runtime (Tokio) and a configurable pool of Python worker processes/threads. Sync Python handlers run on Rust-managed workers sized explicitly at startup (`--workers`/`--processes`), and the Rust side applies real backpressure at the connection level (it can stop reading the socket / return 503). Robyn does not silently inherit a magic 40. **Lancelot should adopt Robyn's model:** Rust event loop + Rust-side admission control + explicitly sized Python execution pool, with a hard "pool full → 503 `Retry-After`" path, never an unbounded internal queue.

**Blazeio comparison:** Blazeio is pure asyncio with no threadpool concept — it expects everything async. Lower overhead, but offers the user no escape hatch for blocking libs (you must wrap yourself). Lancelot wants FastAPI's "sync just works" ergonomic but with Robyn's explicit, observable, backpressured execution model — not anyio's silent 40.

**Concrete Lancelot design:** classify each handler at registration: `async` → run on loop; `sync` → run on a named, sized executor (default = `min(32, cpu*4)` but *documented and overridable*, and surfaced in `/metrics`). Wrap the executor with a bounded queue; on full, the Rust core returns `503` with `Retry-After` *before* spawning the Python frame. For streaming, **reject sync generators with a clear error** ("use `async def` + `yield`"), eliminating the per-chunk thread hop entirely.


## 6. Response class selection & serialization handoff (KNOWN — `routing.py:277-318` + `starlette/responses.py:29-285`)

### 6.1 `serialize_response` (`routing.py:277-318`)

```python
async def serialize_response(*, field, response_content, include, exclude, by_alias,
                             exclude_unset, exclude_defaults, exclude_none,
                             is_coroutine, endpoint_ctx, dump_json=False):
    if field:
        if is_coroutine:
            value, errors = field.validate(response_content, {}, loc=("response",))
        else:
            value, errors = await run_in_threadpool(field.validate, response_content, {}, loc=("response",))
        if errors: raise ResponseValidationError(errors=errors, body=response_content, endpoint_ctx=ctx)
        serializer = field.serialize_json if dump_json else field.serialize
        return serializer(value, include=..., exclude=..., by_alias=..., exclude_unset=..., ...)
    else:
        return jsonable_encoder(response_content)
```

**KNOWN, two critical details:**
1. **Response validation runs every request** (`field.validate`, `:293`/`:295`) — the endpoint's return value is re-validated against the response model on the response path. For a sync endpoint this is *itself* punted to the 40-token threadpool (`:295`, `await run_in_threadpool(field.validate, ...)`) — so a sync handler returning a big model pays a *second* threadpool hop just to validate the response. INFERRED: this is a real, double cost for sync endpoints with response models.
2. **`field.serialize_json` vs `field.serialize`** (`:305`): with `dump_json=True`, Pydantic-core serializes directly to **JSON bytes in Rust** (one pass, no intermediate Python dict). With `dump_json=False`, `field.serialize` produces a Python object (dict) that the response class must then `json.dumps` again. The fork wires `dump_json=use_dump_json` (`routing.py:706`) where `use_dump_json = response_field is not None and isinstance(response_class, DefaultPlaceholder)` (`:692-694`).

### 6.2 The double-encode default path (`starlette/responses.py:181-203`)

`JSONResponse.render` (the *default* when there's no response_model or a custom class):

```python
class JSONResponse(Response):
    media_type = "application/json"
    def render(self, content):
        return json.dumps(content, ensure_ascii=False, allow_nan=False,
                          indent=None, separators=(",", ":")).encode("utf-8")
```

With no response model, `serialize_response` returns `jsonable_encoder(response_content)` (`:317`) — a **recursive Python walk** that converts dataclasses/Pydantic/Decimal/datetime/UUID/Enum/set/etc. into json-safe primitives — then `JSONResponse.render` runs `json.dumps` over the result. **Two full passes over the data** (jsonable_encoder walk + json.dumps) in pure Python. For a 100-item list of nested models this is the dominant per-request CPU cost.

The fast path (`use_dump_json`) collapses this to **one Rust pass**: `field.serialize_json(value, ...)` → bytes → `Response(content=bytes, media_type="application/json")`. `Response.render` (`responses.py:48-53`) sees `bytes` and returns it unchanged (no re-encode). This is ~3-10x faster for model responses (INFERRED, consistent with Pydantic-core benchmarks).

**Exact `json.dumps` flags to replicate (KNOWN, `responses.py:194-203`):** `ensure_ascii=False`, `allow_nan=False`, `indent=None`, `separators=(",", ":")`. Lancelot's JSON encoder must match: compact (no spaces), UTF-8 (non-ASCII passed through, not `\uXXXX`-escaped), reject NaN/Inf.

### 6.3 `Response.__init__` / `init_headers` / `__call__` (`starlette/responses.py:33-171`)

- `__init__` (`:33-46`): `self.body = self.render(content)` then `self.init_headers(headers)`.
- `init_headers` (`:55-79`): builds `raw_headers: list[tuple[bytes,bytes]]`; auto-adds `content-length` (= `str(len(body))`, unless status <200 or 204/304) and `content-type` (appends `; charset=utf-8` only for `text/*`). All header keys/values `.encode("latin-1")`.
- `headers` property (`:82-86`): **lazily** builds `MutableHeaders(raw=self.raw_headers)` only on first `.headers` access — so a response that never touches `.headers` skips that allocation. STEAL this laziness.
- `__call__` (`:163-171`): `send(http.response.start, status, raw_headers)` then `send(http.response.body, body)`; then `if self.background: await self.background()`.

**STEAL:** raw header list of `(bytes,bytes)` tuples (no dict, latin-1 pre-encoded) — directly ASGI-shaped; lazy `MutableHeaders`; content-length/type auto-population rules; the `media_type` class attribute pattern (`HTMLResponse.media_type="text/html"`, etc.) so the response type *is* the content-type, resolved at registration.
**AVOID:** the default double-encode. In Lancelot, every response goes through the Pydantic-core / Rust serializer to bytes once; `json.dumps` is never on the hot path; `jsonable_encoder` becomes a fallback only for un-modeled returns and even then should be a Rust-assisted walk.

### 6.4 Response-class resolution chain (KNOWN)

`DefaultPlaceholder`/`Default` (`datastructures.py:153-186`): a sentinel wrapper so "user explicitly passed `JSONResponse`" is distinguishable from "framework default `JSONResponse`". `get_value_or_default` walks placeholders. Resolution order at `add_api_route` (`routing.py:1371-1373`): route `response_class` → router `default_response_class` → app default (`Default(JSONResponse)`). `include_router` adds two more fallback levels (`:1735-1740`). All resolved **at registration**; `actual_response_class` is a closure local in `get_request_handler` (`:372-375`).

**STEAL:** the `Default()` sentinel idea — it cleanly separates "unset" from "set to the same value as the default", which a plain `is None` check cannot. Lancelot needs this for response class, status code defaults, strict-content-type, unique-id fn. It's a 30-line pattern (`datastructures.py:153-186`) worth copying verbatim.


## 7. Streaming for AI backends — SSE / JSONL / long-lived requests (KNOWN — `routing.py:496-672`, `sse.py`) — ★ steal wholesale

This fork's streaming is its best feature and the most directly relevant subsystem for Lancelot's LLM-token-streaming goal. Vanilla FastAPI makes you hand-build `StreamingResponse`; this fork makes `yield` from an endpoint *just stream*.

### 7.1 The SSE pipeline (`routing.py:496-619`) — the decoupled producer pattern

When `is_sse_stream` (response_class is/sub of `EventSourceResponse`, resolved at registration):

```python
gen = dependant.call(**solved_result.values)            # call the (async) generator endpoint
if dependant.is_async_gen_callable: sse_aiter = gen.__aiter__()
else:                               sse_aiter = iterate_in_threadpool(gen)   # ← sync-gen fallback (avoid, §5.3)

@asynccontextmanager
async def _sse_producer_cm():
    send_stream, receive_stream = anyio.create_memory_object_stream[bytes](max_buffer_size=1)
    async def _producer():
        async with send_stream:
            async for raw_item in sse_aiter:
                await send_stream.send(_serialize_sse_item(raw_item))
    send_keepalive, receive_keepalive = anyio.create_memory_object_stream[bytes](max_buffer_size=1)
    async def _keepalive_inserter():
        async with send_keepalive, receive_stream:
            try:
                while True:
                    try:
                        with anyio.fail_after(_PING_INTERVAL):          # _PING_INTERVAL = 15.0 s (sse.py:222)
                            data = await receive_stream.receive()
                        await send_keepalive.send(data)
                    except TimeoutError:
                        await send_keepalive.send(KEEPALIVE_COMMENT)    # b": ping\n\n" (sse.py:218)
            except anyio.EndOfStream: pass
    async with anyio.create_task_group() as tg:
        tg.start_soon(_producer)
        tg.start_soon(_keepalive_inserter)
        yield receive_keepalive
        tg.cancel_scope.cancel()

sse_receive_stream = await async_exit_stack.enter_async_context(_sse_producer_cm())   # request-scoped exit stack
async_exit_stack.push_async_callback(sse_receive_stream.aclose)

async def _sse_with_checkpoints(stream):
    async for data in stream:
        yield data
        await anyio.sleep(0)        # guarantee a cancellation checkpoint even if producer never suspends

response = StreamingResponse(_sse_with_checkpoints(sse_receive_stream),
                             media_type="text/event-stream",
                             background=solved_result.background_tasks)
response.headers["Cache-Control"] = "no-cache"
response.headers["X-Accel-Buffering"] = "no"                  # tell nginx not to buffer SSE
response.headers.raw.extend(solved_result.response.headers.raw)
```

**Why this architecture is correct (the lessons to steal):**

1. **Two-stage pipeline via bounded memory streams (`max_buffer_size=1`).** The generator (producer) runs in its *own task*, decoupled from both the keepalive timer and the socket writer. The keepalive task sits between producer and consumer and injects `: ping\n\n` whenever the producer is idle >15s. **This is the right way to do SSE keepalives** — you cannot wrap `anyio.fail_after` directly around the generator's `__anext__` (it would `CancelledError` the generator and finalize it mid-stream). Decoupling via a memory stream is the only correct solution. `max_buffer_size=1` gives natural backpressure: the producer can be at most one item ahead of the socket.
2. **The producer CM is entered on the *request-scoped* `AsyncExitStack`** (`async_exit_stack` = `scope["fastapi_inner_astack"]`, `routing.py:590`), NOT relied on via async-generator GC finalization. Comment (`:586-595`, refs PEP 789): the exit stack outlives the streaming response, so `__aexit__` (which cancels the task group) runs via *structured* teardown, not via `GeneratorExit` thrown into an async generator. This fixes the notorious "FastAPI streaming + dependency-with-yield cleanup runs at the wrong time / in the wrong task" class of bugs. **STEAL the principle: streaming resource teardown must be structurally scoped to the request, never left to generator GC.**
3. **`await anyio.sleep(0)` after every yielded chunk** (`_sse_with_checkpoints` `:605`, also JSONL `:634`, raw `:665`; ref FastAPI issue #14680). Guarantees a scheduler checkpoint so client-disconnect cancellation is actually delivered even when the producer is faster than the consumer and `receive()` never suspends. Without this, a fast LLM stream to a disconnected client can spin without noticing the disconnect. **STEAL: a mandatory yield point per streamed chunk.**

### 7.2 SSE wire format (`sse.py:146-214`, `format_sse_event`)

Builds lines: each `comment` line → `: {line}`; `event:` ; each `data` line → `data: {line}` (multi-line data split on `\n`); `id:` ; `retry:` ; then two empty lines → `\n\n` terminator, UTF-8 encoded. `ServerSentEvent` (Pydantic model, `sse.py:42-143`): fields `data` (always JSON-serialized — even strings get quoted), `raw_data` (sent verbatim, no JSON), `event`, `id` (no `\0`), `retry` (ms, ≥0), `comment`; `data` and `raw_data` are mutually exclusive (`model_validator`, `:135-143`). `_serialize_sse_item` (`routing.py:500-527`): if item is `ServerSentEvent`, honor its fields (skip `stream_item_field` validation — user may mix types); else validate+serialize via `stream_item_field` and wrap in `data:`.

**Exact constants to replicate (KNOWN):** `_PING_INTERVAL = 15.0` seconds (`sse.py:222`); `KEEPALIVE_COMMENT = b": ping\n\n"` (`sse.py:218`); SSE terminator `\n\n`; `Cache-Control: no-cache` + `X-Accel-Buffering: no` headers (`routing.py:616-618`). These ARE the product — match them.

### 7.3 JSONL streaming (`routing.py:620-652`)

`media_type="application/jsonl"`. Each item: `_serialize_data(item) + b"\n"`. `_serialize_data` (`:471-494`): if `stream_item_field` set → Pydantic validate + `serialize_json` (Rust, with all the include/exclude flags); else `jsonable_encoder(data)` then `json.dumps(...).encode()`. Async-gen path adds `anyio.sleep(0)` per item; sync-gen path iterates directly (no checkpoint — INFERRED minor gap for sync gens). No keepalive (JSONL has no comment concept). **STEAL: typed-item streaming where the item model is derived from `AsyncIterator[T]` at registration and validated+serialized per item via Pydantic-core** — exactly what you want for streaming structured agent steps / tool-call deltas.

### 7.4 Long-lived requests through the lifecycle (INFERRED)

A streaming LLM endpoint holds the *entire* nested coroutine stack open for the full generation (seconds–minutes): `ServerErrorMiddleware` → user mw → `ExceptionMiddleware` → `wrap_app_handling_exceptions` → `AsyncExitStackMiddleware`'s `async with AsyncExitStack()` → `request_response`'s two more `AsyncExitStack`s → `get_request_handler` → the producer task group. **Every layer's coroutine frame + every `AsyncExitStack` stays resident for the whole stream.** For an agent backend with hundreds of concurrent long streams, that's hundreds × (≈8 frames + ≥3 AsyncExitStacks + 2 memory streams + 2 producer tasks) of resident memory. It works (anyio task groups are cheap-ish) but the constant per-stream overhead is high.

**Lancelot target for AI streaming:**
- Keep the two-stage decoupled-producer + keepalive + per-chunk checkpoint architecture **exactly** (it's correct and hard-won).
- Collapse the middleware nesting (§8) so a stream holds ≈2 frames not ≈8.
- Move the byte framing (SSE `data:`/`\n\n`, JSONL `\n`) into the Rust core: the Python generator yields already-serialized item bytes (or a model that Pydantic-core dumps), Rust does the SSE/JSONL framing + socket write + keepalive timer. This removes `format_sse_event`'s per-event Python list-join and the Python keepalive task entirely — the timer lives in Tokio.
- Reject sync generators for streaming (eliminates `iterate_in_threadpool` per-chunk thread hop, §5.3).
- Disconnect detection: Rust core watches the socket; on client disconnect it cancels the Python producer task immediately (no reliance on `anyio.sleep(0)` checkpoint timing). This is strictly better than FastAPI's approach for cost control on AI backends (you stop paying for token generation the instant the client leaves).

### 7.5 How Robyn / Blazeio compare on streaming

- **Robyn**: supports streaming responses; Rust core owns the socket so backpressure and disconnect are Rust-native. But Robyn lacks FastAPI's *typed-item + Pydantic-core-per-item + auto-SSE-from-`yield`* ergonomic. Lancelot = Robyn's Rust socket ownership + this fork's typed-streaming ergonomics.
- **Blazeio**: extremely fast raw streaming (writes bytes straight to the transport, no Response object, no memory-stream indirection) — this is the latency floor. But no keepalive, no typed items, manual framing. Lancelot should match Blazeio's "bytes straight to socket" floor in the Rust core while exposing this fork's `yield model` ergonomic in Python.
- **Net:** the *architecture* (decoupled producer, bounded buffer, structured teardown, mandatory checkpoint, keepalive timer) is the steal. The *implementation* (Python tasks, Python memory streams, Python `format_sse_event`) is the thing to push into Rust.


## 8. Middleware stack assembly & ordering (KNOWN — `applications.py:1018-1066`, `starlette/applications.py:55-99`)

### 8.1 Lazy build, once, on first request

`Starlette.__call__` (`starlette/applications.py:84-88`): `if self.middleware_stack is None: self.middleware_stack = self.build_middleware_stack()`. **Built lazily on the first request, then frozen.** `add_middleware` (`:96-99`) raises `RuntimeError("Cannot add middleware after an application has started")` if `middleware_stack is not None` — i.e. you can only add middleware before the first request. STEAL the lazy-build-once-then-immutable model; it means per-request middleware dispatch is just calling through a pre-built chain of `__call__`s — no per-request stack assembly.

### 8.2 FastAPI's override (`applications.py:1018-1066`) — the exact onion

```python
middleware = (
    [Middleware(ServerErrorMiddleware, handler=error_handler, debug=debug)]   # OUTERMOST
    + self.user_middleware                                                     # user mw, in add order
    + [
        Middleware(ExceptionMiddleware, handlers=exception_handlers, debug=debug),
        Middleware(AsyncExitStackMiddleware),                                  # INNERMOST (FastAPI-only)
      ]
)
app = self.router
for cls, args, kwargs in reversed(middleware):
    app = cls(app, *args, **kwargs)        # wrap inside-out
return app
```

Resulting onion (outer → inner): **`ServerErrorMiddleware` → [user middleware…] → `ExceptionMiddleware` → `AsyncExitStackMiddleware` → `Router`**.

Rationale (verbatim comments `:1040-1058`): `AsyncExitStackMiddleware` is placed *inside* all user middleware because user middleware (esp. `BaseHTTPMiddleware`) creates a new anyio task group → a new `contextvars` context copy. Putting the file-closing `AsyncExitStack` inside that context keeps contextvars consistent. `ServerErrorMiddleware` is outermost so it catches *anything*, including failures in user middleware. `ExceptionMiddleware` is innermost-but-one so it maps known exceptions (HTTPException, RequestValidationError) to responses *after* user middleware but before routing-internal handling.

`user_middleware` order (`starlette/applications.py:96-99`): `add_middleware` does `self.user_middleware.insert(0, ...)` — so **the last-added middleware is outermost** among user middleware. (Counterintuitive; document it.)

### 8.3 Why this is fast on the happy path, costly in count (INFERRED)

Each middleware is one extra coroutine frame + one `await` per request. `ServerErrorMiddleware`/`ExceptionMiddleware`/`wrap_app_handling_exceptions` each wrap `send` in a closure to track `response_started` — cheap (a closure + a bool) but it's 3 layers of `send` indirection on every response chunk. For a streaming response that's 3 extra Python function calls *per chunk*. **INFERRED:** for a 1000-token LLM SSE stream, that's ~3000 extra wrapper `send` calls just from the always-on middleware, before counting user middleware.

**STEAL:** the fixed outer/inner skeleton (catch-all error handler outermost; exception→response mapper inner; resource cleanup innermost). The *concept* is right.
**AVOID:** implementing it as 3+ separate ASGI-callable Python objects each adding a frame and a `send` wrapper. Lancelot: collapse `ServerErrorMiddleware` + `ExceptionMiddleware` + `wrap_app_handling_exceptions` + `AsyncExitStackMiddleware` into **one** request-scope wrapper (a single try/except with the MRO handler lookup + one `AsyncExitStack` + one `send` interceptor). One frame, one `send` wrapper, instead of four.

### 8.4 `@app.middleware("http")` / `BaseHTTPMiddleware` — the per-request cost bomb (KNOWN — `starlette/middleware/base.py`)

`@app.middleware("http")` → `add_middleware(BaseHTTPMiddleware, dispatch=func)` (`applications.py:4640`). `BaseHTTPMiddleware.__call__` (`base.py:100-199`) per request:

- builds a `_CachedRequest(scope, receive)` (a Request subclass with body-caching, `:21-91`),
- creates an `anyio.Event()` (`response_sent`),
- creates an `anyio.create_memory_object_stream[Message]()` (`:188`),
- enters `anyio.create_task_group()` (`:191`),
- `task_group.start_soon(coro)` where `coro` runs the downstream app with a `receive_or_disconnect` wrapper that itself **spawns a nested task group per receive** (`:111-130`) to race the receive against `response_sent`,
- wraps the response in a `_StreamingResponse` that re-streams via the memory stream (`:171-186`),
- `collapse_excgroups()` context to flatten ExceptionGroups.

**KNOWN cost: every single request through a `BaseHTTPMiddleware` allocates a task group + a memory object stream + a cached request + (per `receive()`) another task group.** For a streaming response it proxies every chunk through the memory stream. This is *by far* the most expensive thing a FastAPI app commonly does, and `@app.middleware("http")` is the most-recommended way to add middleware in tutorials. The framework's own docs nudge users straight into the slow path.

**AVOID at all costs in Lancelot.** Never offer a "dispatch(request, call_next)" middleware that requires bidirectional streaming bridging. **STEAL instead:** the pure-ASGI middleware shape — `class MW: def __init__(self, app): ...; async def __call__(self, scope, receive, send): ... await self.app(scope, receive, send)`. Zero extra tasks, zero streams. Lancelot's public middleware API should be pure-ASGI-style (or a Rust-side middleware chain for built-ins like CORS/GZip/trusted-host), and explicitly *not* expose a `call_next` dispatch model. If a `call_next` ergonomic is demanded, implement it without a task group (just `response = await self.app_as_response(...)`), since with a single-runtime model you don't need anyio's task-group race to detect disconnect — the Rust core already knows.

### 8.5 Built-in middleware (FastAPI `middleware/*.py`)

All are thin re-exports of Starlette's: `CORSMiddleware`, `GZipMiddleware`, `HTTPSRedirectMiddleware`, `TrustedHostMiddleware`, `WSGIMiddleware`, plus FastAPI's own `AsyncExitStackMiddleware` (`asyncexitstack.py`, 18 lines: just `async with AsyncExitStack() as stack: scope["fastapi_middleware_astack"] = stack; await self.app(...)`). These are all **pure-ASGI** (no task group) — fast. STEAL their shape; in Lancelot, CORS/GZip/HTTPS-redirect/trusted-host should be **Rust-core middleware** (header checks + gzip are CPU work that has no business in Python) toggled by config, not Python ASGI wrappers.


## 9. Exception handling pipeline (KNOWN — `_exception_handler.py`, `middleware/errors.py`, `middleware/exceptions.py`, `exception_handlers.py`)

### 9.1 Three layers, each a try/except ASGI wrapper

1. **`ServerErrorMiddleware`** (outermost, `middleware/errors.py:149-185`): wraps everything in `try: await self.app(...) except Exception as exc:`. If `debug` → HTML traceback response (`debug_response`, `:249`); elif no installed 500 handler → default 500 (`error_response`, `:258`); else call the installed `Exception`/`500` handler (async direct, sync via `run_in_threadpool`, `:175-178`). Sends the error response **only if `not response_started`** (`:180-181`), then **always re-raises** `raise exc` (`:184`) so the ASGI server can log it / test clients can surface it. Tracks `response_started` via a wrapped `_send` (`:157-162`).

2. **User middleware** (may catch/transform).

3. **`ExceptionMiddleware`** (`middleware/exceptions.py:45-62`): sets `scope["starlette.exception_handlers"] = (self._exception_handlers, self._status_handlers)`, builds a `Request`/`WebSocket`, then `await wrap_app_handling_exceptions(self.app, conn)(scope, receive, send)`. Preloaded handlers: `{HTTPException: self.http_exception, WebSocketException: self.websocket_exception}` (`:27-30`); FastAPI adds `HTTPException → http_exception_handler`, `RequestValidationError → request_validation_exception_handler`, `WebSocketRequestValidationError → ...` via `setdefault` in `FastAPI.__init__` (`applications.py:1001-1010`).

4. **`wrap_app_handling_exceptions`** (`_exception_handler.py:23-62`) — the actual dispatcher:
```python
try:
    await app(scope, receive, sender)               # sender tracks response_started
except Exception as exc:
    handler = None
    if isinstance(exc, HTTPException): handler = status_handlers.get(exc.status_code)
    if handler is None: handler = _lookup_exception_handler(exception_handlers, exc)   # MRO walk
    if handler is None: raise exc
    if response_started: raise RuntimeError("Caught handled exception, but response already started.") from exc
    response = await handler(conn, exc) if is_async_callable(handler) else await run_in_threadpool(handler, conn, exc)
    if response is not None: await response(scope, receive, sender)
```
`_lookup_exception_handler` (`:16-20`): `for cls in type(exc).__mro__: if cls in exc_handlers: return exc_handlers[cls]` — **walks the exception's MRO** to find the most-specific registered handler. Status-code handlers (int keys) checked first for `HTTPException`.

**Note:** in this fork, `request_response` (`fastapi/routing.py:134`) ALSO wraps the inner app in `wrap_app_handling_exceptions` a *second* time (innermost, around the endpoint). So exceptions raised inside the endpoint are caught at the route level first; if no handler, they bubble out to `ExceptionMiddleware`'s wrapper, then `ServerErrorMiddleware`.

### 9.2 FastAPI's default handlers (`exception_handlers.py`)

- `http_exception_handler` (`:11-17`): if body not allowed for status → bare `Response(status_code, headers)`; else `JSONResponse({"detail": exc.detail}, status_code, headers)`.
- `request_validation_exception_handler` (`:20-26`): `JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})` — **422 is the FastAPI validation status, and the body is `{"detail": [...]}`**. Match this exactly in Lancelot (clients/tests depend on it).
- `websocket_request_validation_exception_handler` (`:29-34`): `websocket.close(code=WS_1008_POLICY_VIOLATION, reason=jsonable_encoder(exc.errors()))`.

`EndpointContext` (`exceptions.py:9-13`, `TypedDict` of `function/path/file/line`) is attached to `RequestValidationError`/`ResponseValidationError` in this fork (`routing.py:390-399`, `:720-723`) so validation errors carry the source location of the offending endpoint — a fork-specific DX improvement. `_extract_endpoint_context` caches by `id(func)` in a module dict (`routing.py:251-274`).

### 9.3 Cost analysis (INFERRED)

**Happy path: ~zero.** Python `try/except` has no cost until an exception is raised (CPython zero-cost exceptions on the no-throw path). The three nested `try` blocks add nothing measurable when no exception occurs. The only always-on cost is the per-layer `send`-wrapper closure (a closure alloc + a bool flip per `http.response.start`) — negligible for non-streaming, but 3 wrapper calls per chunk for streaming (see §8.3).

**Error path:** MRO walk (`type(exc).__mro__`, typically 3-6 classes) + dict lookups — cheap. Building a `Request` in `ExceptionMiddleware` even when no error occurs is a small waste (a Request per request just so a *potential* handler has `conn`).

**STEAL:** the MRO-based most-specific-handler lookup + status-code-handler-first-for-HTTPException ordering + the exact 422/`{"detail": [...]}` validation response shape + `EndpointContext` source attribution on validation errors (great DX, cheap, cached).
**AVOID:** three separate ASGI wrappers each with their own `send` interceptor. Collapse to one (see §8.3). Also avoid building a `Request` in the exception layer unconditionally — defer it until an exception actually needs a handler (lazy `conn`).
**Rust-core angle:** the catch-all (ServerError) + the known-exception→response mapping are partly movable to Rust: the Rust core can own "uncaught Python exception → 500 + log" and the SSE/JSON error envelope formatting; only user-registered Python handlers need a Python frame, and only when they fire.


## 10. BackgroundTasks execution model (KNOWN — `background.py`, `starlette/background.py`)

`fastapi.BackgroundTasks` (`background.py:11-61`) subclasses Starlette's only to add type/Doc annotations — behavior is 100% Starlette's:

```python
class BackgroundTask:                                   # one task
    def __init__(self, func, *args, **kwargs):
        self.is_async = is_async_callable(func)         # resolved at add time
    async def __call__(self):
        if self.is_async: await self.func(*a, **kw)
        else:             await run_in_threadpool(self.func, *a, **kw)   # ← 40-token pool again

class BackgroundTasks(BackgroundTask):
    def add_task(self, func, *a, **kw): self.tasks.append(BackgroundTask(func, *a, **kw))
    async def __call__(self):
        for task in self.tasks: await task()            # ★ SEQUENTIAL, awaited one by one
```

**KNOWN, critical semantics:**
1. **Background tasks run AFTER the response body is fully sent**, inside `Response.__call__` / `StreamingResponse.__call__`: `... await self.background()` (`starlette/responses.py:171`, `:285`). They run **in the same request coroutine**, before that coroutine returns. The connection's worker is held until all background tasks complete.
2. **Tasks run strictly sequentially** (`for task in self.tasks: await task()`), not concurrently.
3. **Sync background functions go through the same 40-token threadpool** (`run_in_threadpool`).

**INFERRED — why this is dangerous for AI backends:** the canonical "return fast, do the slow LLM/embedding work in the background" pattern is *broken* here. The client gets the response bytes, but the ASGI server's handling of that connection/coroutine is not released until the background task finishes (the `await self.background()` is inside the response `__call__`, before `request_response`'s `AsyncExitStack` unwinds). A 30s background embedding job keeps a request slot occupied for 30s after the user got their 200. With keep-alive, the connection isn't freed for the next request on it. And sequential execution means N background tasks = sum of their latencies. This is a frequent FastAPI production surprise.

**STEAL:** the ergonomic (`background_tasks.add_task(fn, ...)` on the request) and the "is_async resolved at add time" detail.
**AVOID:** running them in the request coroutine, sequentially, blocking connection release. **Lancelot design:** `background.add_task` enqueues into a Rust-core-owned worker pool (separate from request workers) with its own bounded queue and concurrency; the request coroutine returns *immediately* after the body is sent; tasks run truly detached and concurrently (configurable). Provide an opt-in `await_in_request=True` for the rare case the user actually wants the old semantics. Surface queue depth in `/metrics`. This is a place Lancelot can be dramatically better than FastAPI with a small surface change.

## 11. Lifespan / startup-shutdown + include_router / Mount (KNOWN — `routing.py:169-247`, `1578-1830`, `starlette/routing.py:629-654`)

### 11.1 Lifespan resolution (`routing.py:1270-1287`)

`APIRouter.__init__`: `if lifespan is None: lifespan_context = _DefaultLifespan(self)` (runs `on_startup`/`on_shutdown`, `:1273`); elif async-gen func → `asynccontextmanager(lifespan)`; elif gen func → `_wrap_gen_lifespan_context`; else use as-is (`:1271-1280`). Stored as `self.lifespan_context`, passed to `super().__init__(..., lifespan=lifespan_context)`.

`_DefaultLifespan` (`routing.py:226-247`): `__aenter__ → await self._router._startup()` (runs all `on_startup` handlers, async-aware, `:4873-4887`); `__aexit__ → await self._router._shutdown()`. **This fork re-implements `_DefaultLifespan` because the underlying Starlette fork removed `on_startup`/`on_shutdown` from core** (Starlette's `_DefaultLifespan` is a no-op, `starlette/routing.py:552-563`). FastAPI keeps the deprecated `@app.on_event("startup")` API working via this shim (`routing.py:4905-4956`).

### 11.2 The ASGI lifespan protocol (`starlette/routing.py:629-654`)

`Router.lifespan`: `await receive()` (the `lifespan.startup` message); `async with self.lifespan_context(app) as maybe_state:` → if state returned, `scope["state"].update(maybe_state)`; `send({"type":"lifespan.startup.complete"})`; `await receive()` (blocks until `lifespan.shutdown`); on exit `send({"type":"lifespan.shutdown.complete"})`. Failures send `lifespan.startup.failed`/`lifespan.shutdown.failed` with the traceback (`:646-652`). Dispatched from `Router.app` (`:668-670`) when `scope["type"]=="lifespan"`.

`_merge_lifespan_context` (`routing.py:209-223`): combines two lifespans into one `@asynccontextmanager` that enters `original` then `nested`, yielding the **merged state dict** `{**(nested or {}), **(original or {})}`. `include_router` chains this (`:1826-1829`) so every included router's lifespan nests inside the parent's.

**STEAL:** the lifespan-as-async-CM contract (one place for startup+shutdown, exception-safe via `async with`), state injected into `scope["state"]` so handlers/deps can read app-wide singletons (DB pools, the LLM client) — exactly what AI backends need (warm one shared async OpenAI client at startup, inject via `request.state` / a dependency). Keep `lifespan.startup.failed` aborting boot (don't serve a half-initialized app).
**AVOID:** deep nesting of merged async CMs when many routers each declare a lifespan — N routers = N nested `async with` frames held for the whole process life (cheap but messy; flatten where possible). Keep state **typed** in Lancelot (a declared app-state struct), not an untyped `{}` merge that silently last-wins on key collisions (`{**nested, **original}` means parent overrides child silently — a footgun).

### 11.3 `include_router` — registration-time flattening (`routing.py:1578-1830`) — STEAL

`include_router` does **not** mount a sub-app. It iterates `router.routes` and for each `APIRoute` calls `self.add_api_route(prefix + route.path, route.endpoint, ...all of route's config re-passed...)` (`:1732-1796`), i.e. it **re-creates a brand-new `APIRoute` in the parent's flat `self.routes` list**, folding in prefix/tags/dependencies/responses/`generate_unique_id`/`strict_content_type` via `get_value_or_default` precedence chains (`:1735-1795`). `routing.Route` (plain) → `add_route`; `APIWebSocketRoute` → `add_api_websocket_route`; on/shutdown handlers copied; lifespans merged (`:1822-1829`).

**KNOWN consequence: there is ZERO runtime cost to router nesting.** Ten nested `include_router`s produce one flat `app.routes` list of fully-formed `APIRoute`s. The match loop sees a flat list (still O(N), but no per-mount delegation). Each route's `self.app` is freshly built (so config is correctly baked in). The price is paid once, at import/startup.

**STEAL this entirely.** Lancelot's `include_router` must flatten into the single Rust trie at registration — nested routers are a *Python authoring convenience* with no runtime topology. Prefix/tags/deps fold into each leaf at insert time.

### 11.4 `Mount` / sub-apps — the slow alternative (`starlette/routing.py:359-457`)

`Mount` is a real runtime delegation: `Mount.matches` (`:387-418`) compiles `path + "/{path:path}"`, on match sets `child_scope` with `root_path = root_path + matched_path` and `endpoint = self.app`, then `Mount.handle` (`:447-448`) `await self.app(scope, receive, send)` — the sub-app **re-runs its own full `Router.app` linear scan**. So `app.mount("/api", sub_app)` means every `/api/*` request pays: parent linear scan → Mount regex → child linear scan → child route regex. **Nested mounts multiply.** Used for ASGI sub-apps (a separate FastAPI, a WSGI app, static files).

**AVOID for first-party routes.** Mounts are O(depth × N) and re-allocate scope dicts per level. Lancelot: use `include_router` (flatten) for all first-party routes; reserve `Mount`-like behavior only for genuinely foreign ASGI apps, and even then resolve the mount prefix in the Rust trie (one trie hit → dispatch to the foreign app) rather than a Python regex + child scan.


## 12. Exact constants & defaults a Lancelot replica must match or consciously diverge from (KNOWN)

| Constant / default | Value | Source | Lancelot decision |
|---|---|---|---|
| Default threadpool tokens (sync handlers) | `CapacityLimiter(40)` | anyio `_backends/_asyncio.py:2957` | **Diverge:** configurable, observable, backpressured (§5) |
| SSE keepalive interval | `_PING_INTERVAL = 15.0` s | `sse.py:222` | Match default; configurable; timer in Rust |
| SSE keepalive payload | `b": ping\n\n"` | `sse.py:218` | Match exactly |
| SSE terminator | `\n\n` | `sse.py:212-214` | Match exactly |
| SSE headers | `Cache-Control: no-cache`, `X-Accel-Buffering: no` | `routing.py:616-618` | Match exactly |
| SSE memory stream buffer | `max_buffer_size=1` (producer & keepalive) | `routing.py:552-563` | Match (backpressure); Rust ring buffer |
| Cancellation checkpoint | `await anyio.sleep(0)` after each streamed chunk | `routing.py:605,634,665` | Replace with Rust socket-disconnect watch |
| Default response class | `Default(JSONResponse)` | `routing.py:355,836,1072` | Keep sentinel; default = Rust JSON |
| Default HTTP method | `["GET"]` if `methods is None` | `routing.py:890-891` | Match |
| GET implies HEAD | `if "GET" in methods: methods.add("HEAD")` | `starlette/routing.py:233-234` | Match (HTTP correctness) |
| `redirect_slashes` | `True` (doubles 404 scan) | `starlette/routing.py:570,695` | **Diverge:** O(1) trie node flag, not 2nd scan |
| Validation error status | `422`, body `{"detail": [...]}` | `exception_handlers.py:20-26` | Match exactly (clients depend) |
| `Match` enum | `NONE=0, PARTIAL=1, FULL=2` | `starlette/routing.py:40-43` | Reproduce semantics in trie |
| Strict content-type | `Default(True)` (no CT ⇒ not JSON-parsed, CSRF guard) | `routing.py:365,1268` | Match default |
| JSON encode flags | `ensure_ascii=False, allow_nan=False, indent=None, separators=(",",":")` | `responses.py:194-203` | Match in Rust encoder |
| `content-length` skip | status `<200` or `204`/`304` | `responses.py:67-72` | Match |
| `charset` appended | only for `text/*` media types | `responses.py:77-78`; charset `utf-8` | Match |
| Endpoint ctx cache | module dict keyed `id(func)` | `routing.py:251,256-274` | Keep (cheap source attribution) |
| `RedirectResponse` default status | `307` | `responses.py:205-211` | Match |
| 405 response | `Allow` header = sorted methods | `starlette/routing.py:268-274` | Match (from trie node) |
| `add_middleware` after start | `RuntimeError` | `starlette/applications.py:96-99` | Match (immutable stack post-boot) |
| Middleware onion | ServerError → user → Exception → AsyncExitStack → Router | `applications.py:1031-1066` | **Collapse to 1 wrapper** (§8.3) |
| user_middleware order | `insert(0,...)` ⇒ last-added is outermost | `starlette/applications.py:98` | Document; keep order semantics |
| Background tasks timing | after body sent, sequential, in request coroutine | `responses.py:171,285`; `background.py:31-33` | **Diverge:** detached Rust pool (§10) |
| Lifespan state merge | `{**(nested or {}), **(original or {})}` (parent wins) | `routing.py:221` | Keep contract; typed state, no silent override |

## 13. Lancelot blueprint — the request lifecycle, reimplemented (INFERRED, synthesis)

Putting it together: the Lancelot HTTP request path, marking what comes from FastAPI (steal), Robyn (Rust core), Blazeio (micro-opt floor).

```
Rust core (Tokio) accepts connection, parses HTTP/1.1+2 (hyper)              [Robyn/Blazeio: never touch Python for parse]
│
├─ Rust router: method-partitioned radix trie  →  (handler_id, params)        [STEAL FastAPI Match/convertor semantics; Robyn trie]
│    miss → Rust emits 404; method-miss → Rust emits 405 + Allow              [no Python frame for errors]
│    trailing-slash = trie node flag → 308/redirect decided O(1)              [AVOID FastAPI double-scan]
│
├─ Rust admission control: if route is sync & sync-pool queue full → 503 Retry-After  [AVOID FastAPI silent 40-cap]
│
├─ Rust builds the request view: headers (already bytes), parsed content-type [AVOID FastAPI email.message per req]
│    (maintype/subtype split in Rust), body (lazy; streamed handle for big)
│
├─ ONE Python entry per request:  handler_closure(request_view)               [STEAL FastAPI freeze-at-registration]
│    - closure built once at registration (DI plan, response codec, flags)    [STEAL get_request_handler shape]
│    - ONE request-scope guard: single try/except (MRO handler lookup)        [STEAL+COLLAPSE FastAPI 3 exc layers → 1]
│      + single AsyncExitStack (yield-deps + file close)                      [STEAL dual-stack purpose, merge to 1]
│      + single send-interceptor (response_started)                            [collapse 4 wrappers → 1]
│    - param coercion: primitives parsed in Rust; complex → Pydantic-core     [STEAL FastAPI DI; push scalars to Rust]
│    - async handler → await on loop;  sync handler → sized named executor    [STEAL ergonomic; Robyn execution model]
│    - return value → Pydantic-core serialize_json (Rust) → bytes             [STEAL dump_json fast path; make it the only path]
│
├─ streaming endpoint (LLM/agent):                                            [STEAL this fork's SSE architecture verbatim]
│    Python async-gen yields item (model or bytes)
│    decoupled producer task → bounded channel → Rust framer                  [producer/keepalive/checkpoint pattern]
│    Rust owns: SSE `data:`/`\n\n` framing, JSONL `\n`, 15s keepalive timer,  [push format_sse_event + timer into Rust]
│               socket write, client-disconnect → cancel Python producer NOW  [better than anyio.sleep(0) checkpoints]
│    sync generators for streaming → hard error at registration               [AVOID iterate_in_threadpool per-chunk hop]
│
├─ Rust sends http response.start + body (zero-copy bytes)                    [Blazeio: bytes straight to transport]
│
└─ background tasks → detached Rust-owned worker pool (bounded, concurrent)   [AVOID FastAPI in-request sequential]
     request coroutine returns immediately after body flush
```

### 13.1 Subsystem-by-subsystem verdict

| Axis | FastAPI mechanism | Fast/Slow | Steal | Avoid | Robyn (Rust core) | Blazeio (from-scratch asyncio) |
|---|---|---|---|---|---|---|
| Route registration | freeze `route.app` closure once | **Fast** (once) | freeze-at-registration | lazy OpenAPI id only | same idea, in Rust | manual, minimal |
| Path matching | O(N) regex scan + dicts | **Slow** | `Match` tri-state, convertors | linear scan, regex/route, redirect double-scan | radix trie in Rust ✅ | dict/exact, low ceremony |
| Sync handlers | anyio 40-token pool | **Slow cliff** | "sync just works" ergonomic | silent 40-cap, no backpressure | explicit sized workers + backpressure ✅ | none (all async) |
| Body/content-type | `email.message` per req | **Slow** | strict-CT CSRF guard | per-req email parser | Rust parse | Rust/manual parse |
| Serialization | jsonable+json.dumps (default) / dump_json (fast) | **Slow default / fast opt-in** | `dump_json` Pydantic-core path | double-encode default | Rust serde-ish | manual bytes |
| Middleware | nested ASGI onion; lazy build once | **Fast happy / slow if BaseHTTPMiddleware** | onion concept, lazy-once | `@app.middleware("http")` task-group/streams | Rust middleware chain | pure-ASGI/manual |
| Exceptions | 3 layered wrappers, MRO lookup | **Fast happy** | MRO lookup, 422 shape, EndpointContext | 3 separate `send` wrappers | Rust catch-all + Python handlers | manual |
| Streaming (AI) | decoupled producer + keepalive + checkpoint | **Architecturally excellent** | the entire architecture ✅ | Python tasks/streams/framing (→Rust) | Rust socket ownership | bytes-to-socket floor |
| Background tasks | in-request, sequential | **Slow / footgun** | the API surface | in-request sequential blocking | detached Rust pool ✅ | manual |
| Lifespan | merged async-CM chain | **Fine** | async-CM contract, scope state | untyped `{}` merge precedence | same contract | manual |
| include_router | flatten at registration | **Fast** ✅ | flatten entirely ✅ | — | flatten into trie | manual |
| Mount/sub-app | runtime delegation, child rescan | **Slow** | — | nested mounts for 1st-party | trie prefix dispatch | manual |

### 13.2 The five highest-leverage steals (priority order for Lancelot)

1. **Registration-time handler compilation** (`routing.py:812-996`): every dispatch decision (codec, DI plan, flags, status, methods) resolved once into a closure/struct; request path is execute-only. Non-negotiable; it's *why* FastAPI's per-request Python is as thin as it is.
2. **The SSE/JSONL streaming architecture** (`routing.py:496-672`, `sse.py`): decoupled producer task + bounded channel + separate keepalive timer + structured request-scoped teardown + mandatory cancellation checkpoint. This is hard-won correctness (PEP 789, issue #14680). Copy the shape exactly; move bytes/timer to Rust.
3. **The `dump_json` fast path** (`routing.py:692-713`, `serialize_response:305`): return value → Pydantic-core `serialize_json` → bytes → bare `Response`, one Rust pass, no `jsonable_encoder`, no second `json.dumps`. Make it the *only* JSON path.
4. **`include_router` flattening** (`routing.py:1732-1796`): nested routers are pure authoring sugar; flatten into one structure at registration with prefix/dep/tag fold-in. Zero runtime topology.
5. **The `Default()` sentinel + config-precedence chains + `Match` tri-state + 422/`{"detail":[...]}` + strict-content-type CSRF default**: the ergonomic/correctness details that made FastAPI adoptable and that clients/tests now depend on. Cheap to copy, expensive to get subtly wrong.

### 13.3 The three bottlenecks Lancelot must NOT inherit

1. **O(N) per-route regex scan + per-route scope dicts + redirect-slash double-scan** (`starlette/routing.py:674-710`, `Route.matches:238-254`). → Rust radix trie, single traversal, params handed to Python once.
2. **Silent 40-token sync threadpool with no backpressure** (anyio `:2957`; `run_endpoint_function:330`; `iterate_in_threadpool:39-58`; background `:31-33`; response-validation-of-sync-endpoints `serialize_response:295`). → explicit sized executor + Rust admission control + 503 `Retry-After`; reject sync stream generators.
3. **`@app.middleware("http")` / `BaseHTTPMiddleware` per-request task-group + 2 memory streams + cached request** (`base.py:100-199`), compounded by the 4-layer always-on middleware/exception `send`-wrapper nesting. → one collapsed request-scope wrapper; pure-ASGI/Rust middleware only; never expose a `call_next` dispatch model that needs a task-group disconnect race.

### 13.4 Honest gaps (what this file deliberately did NOT re-derive)

- `solve_dependencies` / `get_dependant` / `analyze_param` (the DI DAG resolver) — covered at depth in `teardowns/FASTAPI.md` §4; referenced here only at the call boundary (`routing.py:457-464`). Lancelot's DI design belongs in a separate research file.
- OpenAPI generation (`openapi/utils.py`) — registration-time, off the request hot path; not a lifecycle concern.
- Pydantic `_compat` v1/v2 seam — relevant to serialization correctness, not routing lifecycle.
- Security scheme `__call__`s — they are dependencies, resolved inside `solve_dependencies`.
These are intentionally out of scope for "routing & request lifecycle" and tracked elsewhere.

