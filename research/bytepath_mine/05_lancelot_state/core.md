# Lancelot core HTTP path — HAS / LACKS inventory
Source: lancelot/{app,asgi,routing,requests,responses,params,di,validation,middleware,openapi,docs}.py  
Read date: 2026-06-22  

---

## 1. ROUTING (`lancelot/routing.py`)

### HAVE

| Mechanism | File:line | Notes |
|---|---|---|
| O(1) dict const-tier for static paths | `routing.py:762-767` | `Router._const: dict[(method,path) → Route]`; O(1) lookup before regex walk |
| Regex-tier for parameterized paths | `routing.py:768-778` | Linear walk of `_dyn: list[Route]`; sequential regex match |
| `Route._rx` compiled at registration | `routing.py:160-168` | `re.compile("^" + rx + "$")` in `Route.__post_init__`; zero hot-path introspection |
| URL converters: `int`, `float`, `str`, `uuid`, `path` | `routing.py:151-158` | `_CONV` dict in `__post_init__`; regex groups per converter |
| 404 vs 405 discrimination | `routing.py:778` | `_const_paths` set tracks path-only presence; 405 if path known but method unmatched |
| First-wins static dedup; user overrides builtin | `routing.py:753-758` | `existing.builtin` check; user route wins over `/livez` etc. |
| WS-only `match_ws` | `routing.py:781-788` | Separate `match_ws(path)` method; WS routes stored in `_dyn` only |
| Live route add (`add_route_live`) + remove (`remove`) | `routing.py:790-820`, `app.py:702-723` | Evicts from all three tiers (_const, _const_paths, _dyn, routes); OpenAPI cache invalidated |
| Region-pinned routes | `routing.py:729-737` | `Route.region` kwarg; dropped silently at `Router.add()` if `LANCELOT_REGION` mismatch; zero hot-path cost |
| `Route._rx.groupindex` drives body-method inference | `routing.py:344-346 (app)` | Path params extracted from `_rx.groupindex` at compile time; passed to `compile_dependant` |
| `APIRoute` alias | `routing.py:593` | `APIRoute = Route`; OSS subclassing compatibility |
| Custom `route_class` on Router and APIRouter | `routing.py:617, 726` | `self.route_class` kwarg; used in `App._add`, `APIRouter._add` |
| `get_route_handler()` extension point | `routing.py:511-587` | Full body-parse + DI + handler + serialize; dispatched only when overridden (base check at `asgi.py:896`) |

### LACKS / LIMITATIONS

- **No matchit / radix tree for the dynamic tier** — `_dyn` is a linear list; O(N) param-route lookup on every miss of the const-tier. For a small route table this is negligible; for an app with 500+ parameterized routes it becomes visible under load.
- **No method-level const-tier fast-reject** — a 405 on a static path still hits the dict then falls through to set membership; a separate `_const_methods` pre-check could short-circuit one dict lookup.
- **`redirect_slashes` is ASGI-seam only** — `App.remove_route` does not update `_const_paths` for slash-redirect paths (edge case; the existing test coverage is on the ASGI path).

---

## 2. APP (`lancelot/app.py`)

### HAVE

| Mechanism | File:line | Notes |
|---|---|---|
| `App.__call__` as ASGI app | `app.py:153-159` | Lazy builds `_asgi` via `asgi()` on first call; TestClient works without `app.asgi()` unwrap |
| `App.run()` native Rust server | `app.py:239-264` | Delegates to `_native.run`; env-driven host/port (`LANCELOT_HOST`, `PORT`, `LANCELOT_PORT`) |
| Const-tier prebaked routes | `app.py:725-746`, `run()` 1173-1203 | `app.const_route(method, path, body, content_type, status)` → `_native.run(const_routes=...)` → zero-GIL Rust serve |
| Const-route middleware-bypass guardrail | `app.py:1119-1158` | `ConstRouteAuthBypassWarning` + `LANCELOT_CONST_STRICT=1` hard error; named bypass paths in log |
| `@app.batch` | `app.py:442-537` | Prefix-aware coalescing, `batch_window_ms`, `max_batch`, `max_keys`, `size_fn`/`max_size` (token budget); 503+Retry-After on `BatchOverloadError` |
| `@app.stream` | `app.py:539-647` | SSE, `stream_interval`, `sse_keepalive`, `defer_status`, `max_stream_silence`, `max_stream_lifetime`, `error_frame`, `done_sentinel` |
| `@app.tool` + `tool_schemas()` + `call_tool()` + `mcp()` | `app.py:376-428` | LLM tool registry; Anthropic-tool-format `input_schema`+`output_schema`; Streamable-HTTP MCP mount |
| App-level `dependencies` (prepended to every route) | `app.py:126, 363-366` | Prepended in `_add()` and `include_router()` |
| `default_response_class` propagation | `app.py:147-149, 355-358` | Route → router → app fallback chain |
| `strict_content_type` hierarchy | `app.py:118, 359-361, 802-829 (asgi)` | Per-route → per-router → app default; resolved to `bool` at `App.include_router` |
| `json_max_depth` DoS guard | `app.py:121, asgi.py:765` | Default 128 (Robyn's value); checked in ASGI seam before parse; `LANCELOT_MAX_BODY`/`LANCELOT_MAX_QUERY_PARAMS` also DoS-guard |
| `cancel_on_disconnect` App-level default | `app.py:131-132` | Inherited by routes with `cancel_on_disconnect=None` (the per-route default); fast-path: no watcher armed unless opted in |
| `@app.middleware("http")` | `app.py:841-850` | Stores user fns; wraps via `HTTPMiddlewareAdapter` at `asgi()` build time |
| `add_middleware(cls, **opts)` | `app.py:266-284` | Supports pure-ASGI and `BaseHTTPMiddleware` subclasses; last-added = outermost |
| `mount(prefix, app)` | `app.py:161-166` | Sub-ASGI delegation; `root_path` accumulation for nested mounts |
| Lifespan: `lifespan=` + `@app.on_event` + `@app.lifetime_task` | `app.py:168-237, 1022-1116 (_make_lifespan_callbacks)` | Native server drives ASGI lifespan via in-memory queue; partial-startup unwind; `_drain_lifetime_tasks` on shutdown |
| Graceful shutdown API | `app.py:903-955` | `initiate_shutdown()`, `drain(timeout)`, `install_signal_handlers()`; `/readyz` 503+Retry-After while draining |
| `@app.exception_handler` / `add_exception_handler` | `app.py:885-900` | MRO walk; registered `Exception:` catch-all also fires for subclasses |
| `url_path_for(name, /, **path_params)` | `app.py:865-882` | Starlette-parity; raises `NoMatchFound` on miss |
| `openapi()` memoized | `app.py:853-863` | Built once; `add_route_live`/`remove_route` invalidate cache |
| `State` (attribute+mapping bag) | `app.py:9-54` | Writes through to `scope['state']` dict; both `request.state.x` and `request.state['x']` idioms |

### LACKS / LIMITATIONS

- **`App.run()` does NOT support DI or body parsing** — documented: native server dispatches `(method, path, handler)` only; full DI requires uvicorn. This is an intentional boundary.
- **`const_routes` and `remove_route` are not wired to the running native server** — the Rust router is built once at startup; live mutations are ASGI-tier only.
- **No `App.mount_live()`** — mounts are also fixed at `asgi()` build time; a mount added after `asgi()` is first called requires `_asgi = None` reset (done by `mount()` itself, but only if caller uses that method).

---

## 3. ASGI SEAM (`lancelot/asgi.py`)

### HAVE

| Mechanism | File:line | Notes |
|---|---|---|
| Drain-while-shutting-down (503) | `asgi.py:319-326` | Checks `app._shutting_down`; /livez+/readyz bypass |
| Global concurrent-request cap (503+Retry-After) | `asgi.py:328-343` | `app._max_concurrent_requests`; health probes bypass |
| Body cap (413) | `asgi.py:728-740` | `_MAX_BODY` = `LANCELOT_MAX_BODY` env (default 16 MiB, clamped [1 KiB, 1 GiB]); returns 413 on overflow |
| Query-param count cap (400) | `asgi.py:770-775` | `_MAX_QUERY_PARAMS` = `LANCELOT_MAX_QUERY_PARAMS` env (default 512, clamped [16, 65536]) |
| JSON depth guard (400) | `asgi.py:752-768` | `json_max_depth` via `_json_guard.exceeds_depth`; only for `*json*` content-types |
| Single-scan header read for request-id + traceparent | `asgi.py:353-353, 440-467` | `_scan_request_context` iterates headers once; X-Request-ID / X-Correlation-ID (CRLF-sanitized, 128-char bound); W3C traceparent |
| ContextVar set/reset per-request | `asgi.py:400-401, 437, 470-503` | `_set_request_context` / `_reset_request_context`; token-based reset; `request_id`, `trace_id`, `trace_parent` |
| RED metrics per-request | `asgi.py:407-435` | `record_request(route_template, method, status, duration, tenant, cost_usd, queue_wait_ms)`; route template label (not concrete URL — cardinality safe) |
| TTFT on first streamed chunk | `asgi.py:524-571, 1131-1148` | `_record_ttft_now` / `_ttft_send_wrapper`; fires on `text/event-stream` content-type OR `more_body=True` first chunk; covers `@app.stream`, `StreamingResponse`, Starlette ASGI-callable responses |
| `@app.stream` keepalive task + silence-abort + lifetime cap | `asgi.py:1089-1305` | Separate `_watchdog_loop` task (one per stream, not per-chunk); `_send_lock` serializes ping vs data; last-DATA and last-PING tracked separately (silence timer not reset by pings) |
| `@app.stream` disconnect cancel | `asgi.py:1277-1301, 1292-1300` | `_cancel_on_disconnect()` task awaits `lreq._cancel_event.wait()` and cancels the consume task; M18: proactive cancel on plain stream |
| `@app.stream` error frame (M17 P6) | `asgi.py:1156-1194` | Opt-in `error_frame=True`: scrubbed RFC-9457 ProblemDetail frame; guaranteed `done_sentinel` on every non-disconnect end |
| `@app.stream` `defer_status` (M16 P1) | `asgi.py:986-1036` | Pre-200 first-item pull; pre-yield HTTPException → real 4xx/5xx; ASGI-seam only |
| Per-route timeout → 504 | `asgi.py:1448-1521` | `asyncio.wait_for(handler, timeout=route.timeout)` inside `solve_async`; yield-dep teardown preserved |
| Unary `cancel_on_disconnect` | `asgi.py:1461-1528` | `_race_handler_vs_disconnect` dual-task pattern; only armed when flag is True AND there's an async await to race; zero cost on default (flag=False) path |
| Sync handler offload | `asgi.py:1481-1495` | `get_pool().offload(route.handler, **kwargs)` → bounded blocking pool; never pins the event loop |
| StreamingResponse auto-wrap for async-gen returns | `asgi.py:1538-1615` | Detects `isasyncgen`/`isgenerator`/`__aiter__`; per-item `TypeAdapter` validation when `return` hint is `AsyncIterable[Model]`; wraps as `application/jsonl` |
| Starlette ASGI-callable response delegation | `asgi.py:1617-1635` | Duck-type check: `media_type` present + no `body_iterator` + callable → `result(scope, receive, _ttft_send_wrapper(send, scope))` |
| `response_model` serialize via cached `TypeAdapter` | `asgi.py:1724-1759` | `route._response_ta` built at registration (P1: no per-request rebuild); `skip_validation` opt-in (M17 P4) bypasses `model_validate` pass |
| Response scratch merge (`response: Response` DI) | `asgi.py:1773-1790` | `lreq._resp_scratch`; merges cookies/headers/status_code after handler return |
| `response_class` dispatch | `asgi.py:1793-1833` | Instantiates the response class with `result`; delegates ASGI-callable responses; emits Lancelot Response bodies manually |
| BackgroundTasks post-send + `StreamingResponse.background` | `asgi.py:1715-1716, 1681-1690` | `lreq._background` run after body; `response.background` also run (R1: was silently dropped) |
| HTTPException → MRO-walked user handler | `asgi.py:1839-1873` | `_find_exc_handler` walks `type(exc).__mro__`; `ProblemDetail` emits `application/problem+json` (RFC 9457) |
| `PoolFull` → 503+Retry-After | `asgi.py:1874-1882` | Sync-pool overflow → 503 (not a hang) |
| `BatchOverloadError` → 503+Retry-After | `asgi.py:1883-1892` | `max_keys` hit on `@app.batch` → sheddable 503 |
| Unhandled exception → Starlette-parity plaintext 500 | `asgi.py:1893-1924` | `"Internal Server Error"` body (PlainText, not JSON); user handler called first; original exception re-raised |
| Function-scope yield-dep stack | `asgi.py:876-933` | `_func_stack`: closes BETWEEN handler-return and response-emit (FastAPI `scope="function"`) |
| Custom `get_route_handler` dispatch | `asgi.py:892-924` | Only triggered when `type(route).get_route_handler is not _BaseRoute.get_route_handler` |
| WebSocket seam: DI + traceparent + cookie | `asgi.py:1935-2020` | Same `_scan_request_context`/`_set_request_context` as HTTP; validation failure → close 1008 |
| Mount sub-ASGI + lifespan drive | `asgi.py:108-183` | `_sub_lifespan_cm` drives any ASGI-compliant sub-app; LIFO teardown via `AsyncExitStack`; `root_path` accumulation |
| X-Lancelot-Cost-USD response header | `asgi.py:370-390` | Emitted when `cost_usd > 0` (6-decimal); cost read from `lancelot.context.cost_usd` ContextVar |
| Lenient cookie parser | `asgi.py:49-71, 794` | `_parse_cookies`: split on `;`, first `=` boundary, quoted-value de-quote; first-wins (cookie-bomb defense); bypasses `http.cookies.SimpleCookie` |

### LACKS / LIMITATIONS

- **ASGI path body drain is unbounded-chunk-loop** — `asgi.py:728-739`: `while True: body += msg.get("body", b"")` until capped or `more_body=False`. This does a `+=` on bytes (realloc per chunk). For very large streaming uploads this is O(N²) in practice; a `bytearray` pre-allocated to the declared `content-length` would avoid reallocs. Not a hot path (upload APIs are rare vs. JSON API calls).
- **Query-string parse (`parse_qs`) on EVERY request** — `asgi.py:778`: even when `query_string=b""` the `if _raw_qs` guard prevents the call, but the `.decode()` still runs; the guard is on `_raw_qs` truthiness (empty bytes is falsy — OK). This is already guarded.
- **Content-Type scan inside body dispatch is a second header scan** — `asgi.py:815-822`: iterates `scope["headers"]` for `content-type` AGAIN after `_scan_request_context` already scanned once for request-id + traceparent. Could be folded into the single `_scan_request_context` pass to halve header iterations.
- **`_MAX_BODY` resolved once at module import** — `asgi.py:27`: `LANCELOT_MAX_BODY` is read at import, not per-request or per-app. A reconfigured value after import is ignored (this is intentional but worth noting for operators).

---

## 4. REQUEST (`lancelot/requests.py`)

### HAVE

| Mechanism | File:line | Notes |
|---|---|---|
| `_BodyAccessor` bytes-subclass | `requests.py:4-17` | `bytes` + `async def __call__()` coroutine; zero hot-path cost (only invoked when `await request.body()`) |
| `request.cancelled` property | `requests.py:39-48` | Reads `_cancel_event.is_set()`; False when watcher not armed (default) |
| `request.is_disconnected()` | `requests.py:50-57` | Async alias for `cancelled`; drop-in for FastAPI migration |
| `request.state` property | `requests.py:59-70` | Wraps `scope_state` in `State` (attribute+mapping); writes through to the dict |
| `request.scope_state` | `requests.py:33` | Raw ASGI `scope["state"]` dict |
| `request.query_params_multi` | `requests.py:35` | Full `{k: [v, ...]}` for multi-value params |
| `request.headers_multi` | `requests.py:36` | Full `{k: [v, ...]}` for multi-value headers |
| `request._ws` | `requests.py:34` | Live WebSocket, set by WS seam |
| `request._background` | `requests.py:33` | Fresh `BackgroundTasks` per request |

### LACKS / LIMITATIONS

- **No `request.url` / `request.base_url` / `request.scope`** — these are Starlette `Request` attributes some migration code expects. The raw ASGI `scope` is not directly exposed on the Lancelot `Request` (handlers receive the dataclass, not the scope). Starlette migration code that reads `request.url.scheme` or `request.scope["app"]` via the Starlette path won't work without the ASGI seam's `_AppProxy`.
- **`request.form()` / `request.json()` async methods absent** — FastAPI/Starlette provide `await request.json()` and `await request.form()` as async methods on Request. Lancelot's Request has `body: bytes` and DI handles parsing; there is no public `async def json()` method. Handlers that call `await request.json()` directly (non-DI pattern) must read `request.body` and parse themselves.
- **`request.receive` not stored** — the raw ASGI `receive` callable is not accessible from `Request`; Starlette code that calls `await request.receive()` directly (e.g. for server-sent events or streaming uploads) cannot be ported without modification.

---

## 5. RESPONSES (`lancelot/responses.py`)

### HAVE

| Mechanism | File:line | Notes |
|---|---|---|
| `Response`, `HTMLResponse`, `JSONResponse`, `PlainTextResponse` | `responses.py:13-133` | Standard response classes; `JSONResponse` accepts pre-serialized bytes OR Python object |
| `StreamingResponse` | `responses.py:137-152` | `body_iterator` + optional `background`; ASGI seam streams chunks; `background` now runs (R1 fix) |
| `ORJSONResponse`, `UJSONResponse` pass-throughs | `responses.py:159-164` | Alias to `JSONResponse`; contract (class exists, returns JSON) works without orjson/ujson |
| `Response.set_cookie` | `responses.py:31-67` | Full RFC-6265 Set-Cookie; CRLF/control-char strip on all string fields; SameSite validation |
| `Response.delete_cookie` | `responses.py:69-71` | Max-Age=0 + Expires=0 |
| `Response.set_session_affinity` | `responses.py:74-84` | `X-Session-Affinity: <key>` hint; CRLF-stripped; KV-cache-aware LB routing |
| `Response.set_retry_hint` | `responses.py:86-102` | `X-Should-Retry: true/false`; server-controlled retry overrides client SDK defaults |
| `_strip_ctl` | `responses.py:5-10` | Drop CR/LF/control chars from Set-Cookie fields (response-splitting guard) |

### LACKS / LIMITATIONS

- **`FileResponse`** — not implemented natively; Starlette's `FileResponse` works via ASGI-callable delegation in the seam (it IS an ASGI app). But `from lancelot.responses import FileResponse` fails. Users must `from starlette.responses import FileResponse`.
- **`RedirectResponse`** — same as FileResponse: works as a returned Starlette object via ASGI delegation, but not importable from `lancelot.responses`.
- **`Response.media_type` default is `"text/plain"`** — inherited by plain `Response()`. `JSONResponse` overrides this to `"application/json"`, which is correct. But a user returning `Response(b"...")` without specifying `media_type` gets `text/plain`; FastAPI's default is also `text/plain` (parity correct).

---

## 6. PARAMS (`lancelot/params.py`)

### HAVE

| Mechanism | File:line | Notes |
|---|---|---|
| `Query`, `Path`, `Header`, `Cookie`, `Body`, `Form`, `File` | `params.py:42-82` | Standard FastAPI-shape parameter markers |
| `Depends`, `Security` | `params.py:87-108` | Hash-comparable (`__hash__`); `Security.scopes` tuple |
| `FieldInfo` with `validation_alias`, `body_scalar`, `is_bucket`, `explicit_marker` | `params.py:111-122` | Pydantic v2 `validation_alias` (input-only); single-Body-scalar detection; bucket-model flag |
| Constraint keys: `gt`, `ge`, `lt`, `le`, `min_length`, `max_length`, `pattern`, `multiple_of`, `max_digits`, `decimal_places`, `allow_inf_nan` | `params.py:18-23` | Passed to `ParamValidator` via `fi.marker.constraints` |
| Multi-Param constraint merge | `params.py:160-170 (di.py)` | `Annotated[int, Query(gt=2), Query(lt=10)]` → merged constraints dict |
| `Body(embed=True)` | `params.py:71-76` | Wraps scalar in JSON object keyed by name |
| `Header(convert_underscores=True)` | `params.py:56-59` | Name → `name.replace("_","-").lower()` |

### LACKS / LIMITATIONS

- **`Query(include_in_schema=False)` not wired** — the `include_in_schema` kwarg on individual parameter markers is not consumed by `openapi.py`; it's accepted but silently ignored. A route with a "hidden" query param will still have it in the OpenAPI spec.
- **`examples=` on parameter markers** — accepted by FastAPI's `Query(examples={...})` for per-field OpenAPI examples; not plumbed into `openapi.py` for individual parameter schemas (only `openapi_extra` at the route level works).

---

## 7. DI (`lancelot/di.py`)

### HAVE

| Mechanism | File:line | Notes |
|---|---|---|
| `compile_dependant` registration-time plan | `di.py:87-279` | Signature introspection ONCE at registration; `Plan` is an immutable DAG |
| Two-phase: plan (registration) / solve (per-request) | `di.py:4, 491` | Hot path: no `inspect` calls, no `get_type_hints` |
| Diamond-dep dedup via `_scoped_key` cache | `di.py:626-631` | Key: `(call, sorted_scopes_if_uses_scopes, norm_scope)`; same dep runs once per request (use_cache=True) |
| Scope-based cache-key separation | `di.py:622-631` | `Depends(scope="function")` gets separate cache entry from `Depends()` (different lifetime) |
| Yield-dep AsyncExitStack teardown | `di.py:576-754` | `solve_async` is an asynccontextmanager; async-gen and sync-gen deps entered via `AsyncExitStack` |
| Function-scope vs request-scope teardown | `di.py:687-695` | `scope="function"` → `_func_stack` (closes between handler-return and response); else main stack |
| Dependency override per-app memoization | `di.py:470-488` | `app.override_plans` dict (not process-global); `compile_dependant` called once per override target per app |
| Magic injectables: `Request`, `BackgroundTasks`, `WebSocket`, `Response`, `SecurityScopes` | `di.py:147-179, 527-567` | Detected by type; bound in `_bind_req_params` |
| SecurityScopes chain propagation | `di.py:616, 653-654` | `_parent_scopes` accumulates down the dep graph; injected into `SecurityScopes` for the scheme |
| `functools.partial` + `@wraps` / `lru_cache` unwrap | `di.py:92-109` | 8-iteration peel; `__wrapped__` + `partial.func` chain |
| Callable-instance support | `di.py:109-121` | `not isfunction and not ismethod → __call__` introspection |
| Security scheme invoked directly (not via DI graph) | `di.py:634-647` | `SecurityBase` → called with `(request)`, not compiled; awaitable result awaited |
| Sync dep offload | `di.py:707-711` | `get_pool().offload(target, **sr.kwargs)` → bounded pool (FastAPI run_in_threadpool parity) |
| PEP 695 / `TypeAliasType` unwrap | `di.py:143-148` | `__value__` attribute check + `TypeAliasType` name check |
| `Annotated` Depends/Param inside Depends: ambiguity raises | `di.py:212-225` | Registration-time `AssertionError` for conflicting annotation+default |
| `Response` return annotation → `response_model=None` auto | `routing.py:306-309` | `_contains_response` walk; silently clears `response_model` (FastAPI parity) |
| `Union[Response, X]` return annotation → FastAPIError | `routing.py:310-320` | Ambiguous return annotation raises at registration |
| Scope-incompatibility check | `routing.py:384-406` | `request-scope` yield-dep cannot depend on `function-scope` yield-dep; `FastAPIError` at registration |
| Pydantic v1 detection → `PydanticV1NotSupportedError` | `routing.py:172-219` | Recursively checks all type hints + `response_model` + `responses` models |
| Path-param primitive check | `routing.py:430-436` | Path param annotated as collection/Model → `AssertionError` at registration |
| Body inference: method-aware | `routing.py:468-504` | `list[T]` on POST/PUT/PATCH/DELETE → body; on GET stays query |
| `response_model_skip_validation` (M17 P4) | `routing.py:238-255, asgi.py:1738-1740` | Skip `model_validate`; fence: incompatible with `exclude_unset` |
| `response_model` `TypeAdapter` built at registration | `routing.py:231-237` | `route._response_ta`; no per-request rebuild |

### LACKS / LIMITATIONS

- **`Depends(use_cache=False)` does not bypass `_scoped_key` in the cache** — actually it does: `di.py:628-631` checks `sub.use_cache and _scoped_key in cache`. If `use_cache=False` the check is skipped and the dep always runs. This is CORRECT and complete.
- **Callable INSTANCE sync handler via DI** — `di.py:109-121`: introspection works for `Depends(instance)` via `__call__`, but a ROUTE handler that is a callable instance (not a function/class) is logged as a known gap in `asgi.py:953-958`. The `plan.is_async` flag is based on `iscoroutinefunction(fn)` applied to the outer callable, not `__call__`.
- **`StreamingResponse` per-item validation** — `asgi.py:1589-1613`: the auto-wrap validates each item via `TypeAdapter`; but this is per-chunk, not pre-serialized. For a high-rate stream the per-item `validate_python` + `dump_json` is a hot path. Currently acceptable (upstream-rate-limited), but noted.

---

## 8. VALIDATION (`lancelot/validation.py`)

### HAVE

| Mechanism | File:line | Notes |
|---|---|---|
| `BodyValidator.validate_json` single pydantic-core pass | `validation.py:107-126` | `TypeAdapter.validate_json(raw_bytes)` — one Rust pass; malformed JSON fallback to stdlib for byte-identical error loc |
| `FormBodyValidator.validate_form` | `validation.py:129-192` | Multi-value form → scalar/list per field; `_seq_keys` set for sequence-typed fields; defaults pre-populated |
| `ParamValidator` with annotated-types constraints | `validation.py:25-72` | `at.Gt/Ge/Lt/Le/MinLen/MaxLen/MultipleOf`; `pydantic.Field(allow_inf_nan=)` path |
| `_wire_json` serializer | `validation.py:266-310` | orjson (lazy, soft-dep) → pydantic-core `_native._to_json` → stdlib json with `_json_default` fallback |
| `_json_default` fallback | `validation.py:213-263` | UUID, datetime, Enum, Decimal, Path, BaseModel, dataclass, set/frozenset, bytes; asyncpg-style lying-UUID via `str()` |
| `_regenerate` pydantic-core error reformatting | `validation.py:75-94` | FastAPI `_regenerate_error_with_loc`; `include_url=False`; bytes in `input` → `{}` guard; `ctx` bytes decode |
| `errors_to_422` | `validation.py:313-314` | Single-call 422 body builder |

### LACKS / LIMITATIONS

- **`pattern`, `max_digits`, `decimal_places` constraints** — in `_CONSTRAINT_KEYS` (params.py:20-22) but NOT in `_CONSTRAINT_TO_AT` (validation.py:19-22). They are accepted on markers but SILENTLY DROPPED in `ParamValidator.__init__`. The P7 conformance ratchet is supposed to drive these up; they are not yet wired.
- **`alias` OpenAPI title** — `openapi.py:34` uses `validation_alias or alias or name` for the schema title, which is correct. But `ParamValidator.validate` uses `fi.validation_alias or fi.alias or fi.name` for `loc_prefix`, which is also correct (parity).

---

## 9. MIDDLEWARE (`lancelot/middleware.py`)

### HAVE

| Mechanism | File:line | Notes |
|---|---|---|
| `HTTPMiddlewareAdapter` (streaming, no anyio dep) | `middleware.py:40-153` | Pure asyncio queue-based streaming; no full-body buffering; `call_next` returns `StreamingResponse` on first `http.response.start`; inner task started lazily (short-circuit safe) |
| `BaseHTTPMiddleware` pass-through via `add_middleware` | `app.py:266-284, middleware.py` | Starlette's `BaseHTTPMiddleware` wraps correctly as ASGI-callable; `scope['state']` propagated to handler via `request.scope_state` |
| `CORSMiddleware` | `middleware.py:211-330` | Byte-faithful to Starlette: literal `*` for wildcard; `Vary: Origin` for explicit list; preflight 400 "Disallowed CORS..."; `sorted(SAFELISTED ∪ allow_headers)`; ACAC before ACAO/Vary; credentials+wildcard hard rejection |
| `Middleware` config-shape deferred constructor | `middleware.py:25-37` | `Middleware(cls, **opts)` → unpacked at app-build time |

### LACKS / LIMITATIONS

- **`CORSMiddleware` unimplemented tail** — `allow_origin_regex`, wildcard-subdomain origins, `expose_headers`, `access-control-request-private-network` (documented in the class docstring as ledgered RED).
- **`@app.middleware("http")` does NOT get the real body** — `HTTPMiddlewareAdapter` builds a `Request` with `body=b""` (line 81); a middleware fn that reads `request.body` gets empty bytes. The inner app gets the real `receive()`. This is intentional (FastAPI's `call_next` also doesn't give the middleware the raw body without consuming it first).
- **`BaseHTTPMiddleware` streaming semantics gap** — conformance ceiling item 1 (`asgi.py:270-284 comment`): the `@app.middleware` fn sees global state BEFORE request-scope yield-dep teardown only via Starlette's anyio task-group streaming model, not replicated here. One vendored test RED for this reason.

---

## 10. OPENAPI (`lancelot/openapi.py`) + DOCS (`lancelot/docs.py`)

### HAVE

| Mechanism | File:line | Notes |
|---|---|---|
| OpenAPI 3.1.0 schema assembly | `openapi.py:314-355` | Paths → operations → parameters → requestBody → responses; component schemas from `pydantic.GenerateJsonSchema` |
| `operationId` precedence chain | `openapi.py:301-311` | Explicit per-route > `generate_unique_id_function` > built-in MUST-32 formula |
| `openapi_extra` shallow merge | `openapi.py:337-340` | User dict merged LAST; can override generated keys or add `x-*` extensions |
| Transitive security scheme collection | `openapi.py:189-213` | `_collect_security` walks full dep graph; nested `Security()` inside `Depends()` now included (M19b fix) |
| `responses[STATUS]["model"]` TypeAdapter validation | `routing.py:322-336` | Validates at registration; `PydanticV1NotSupportedError` on v1 models |
| Form/File `Body_<operation_id>` schema synthesis | `openapi.py:271-298` | Embed scalars, Form() params, File() params all get a synthetic schema |
| OpenAPI binary format fix | `openapi.py:48-58` | `format: binary` → `contentMediaType: application/octet-stream` for File() params (OAS 3.1) |
| Optional `default: null` strip | `openapi.py:244-257` | `Optional[T]=None` → `anyOf[T, null]` without `default: null`; matches FastAPI 0.136.1 |
| `/openapi.json`, `/docs` (Swagger UI), `/redoc`, `/scalar` routes | `app.py:289-337, docs.py` | CDN-pinned shells; HTML-escaped title; `json.dumps(openapi_url)` for safe JS string |
| Schema lexicographic sort | `openapi.py:298` | `{k: defs[k] for k in sorted(defs)}`; MUST-17 |

### LACKS / LIMITATIONS

- **`Query(include_in_schema=False)` not wired** — noted above in params section.
- **`examples=` on parameter markers** — per-parameter OpenAPI examples not plumbed.
- **Response schema for streaming routes** — `@app.stream` routes get no `content` schema in the OpenAPI response (the 200 response object has `{"schema": {}}`). A correct `text/event-stream` schema with the yielded model type would need to be synthesized.
- **`responses[STATUS]` descriptions** — `responses_for` uses a hardcoded "Successful Response" for 200 and "Validation Error" for 422. The user-supplied `responses={200: {"description": "..."}}` is in `Route.responses` but `build_openapi` does not merge it into the response object (it only validates the model at registration). This is a known conformance gap.

---

## Summary: HAVE / LACKS at the byte-path level

### Core byte-path HAVE (shipped, proven)
1. Rust const-tier prebaked zero-GIL responses at ~183k req/s
2. O(1) Python-tier static routing (dict) + regex dynamic tier
3. R3 on-loop `_drain` streaming pump: bounded-mpsc, GIL dropped per chunk, TTFT metric, backpressure, silence-abort, lifetime cap, error_frame, defer_status
4. Single-pass pydantic-core `validate_json` for body (one Rust pass)
5. Cached `TypeAdapter` per route for response_model (P1: no per-request rebuild)
6. Registration-time DI plan compilation (zero hot-path introspection)
7. Request-id + W3C traceparent in one header scan per request
8. Body cap (16 MiB) + query-param cap (512) + JSON depth guard (128) — three DoS guards
9. Graceful drain: Rust `acquire_many_owned(max_conns)` + ASGI `_shutting_down` + `/readyz` 503 + drain(timeout)
10. Cancel-on-disconnect for streaming (always) and unary (opt-in, zero cost when off)
11. Per-route timeout → 504 with yield-dep teardown preserved
12. `@app.batch` prefix-aware coalescing with size_fn token-budget knob
13. RED metrics with route-template labels (cardinality-safe)
14. `set_session_affinity` → `X-Session-Affinity` for KV-cache-aware LB routing
15. W3C traceparent propagation to upstream LLM calls via `TransportPool`

### Core byte-path LACKS (not yet built)
1. **Radix tree / matchit for the dynamic route tier** — linear O(N) param-route scan; not the bottleneck for typical route tables (<100 routes) but a real win at scale
2. **`request.json()` / `request.form()` async methods** — handlers that call these directly (non-DI) must parse `request.body` manually; migration friction
3. **`FileResponse` / `RedirectResponse` in `lancelot.responses`** — importable from Starlette, ASGI-delegated at the seam, but not first-class in Lancelot's response namespace
4. **`pattern`, `max_digits`, `decimal_places` constraints** — accepted on markers, silently dropped in ParamValidator; P7 ratchet drives these
5. **Content-Type second header scan** — `asgi.py:815-822` re-scans headers for `content-type` after the request-context scan already ran; could be folded into one pass (micro-optimization, not a bottleneck)
6. **Per-parameter `include_in_schema=False` and `examples=`** — OpenAPI generation gaps; no byte-path impact
7. **Streaming route OpenAPI response schema** — `@app.stream` routes show `{"schema": {}}` for the 200; the yielded model type is not wired into the schema
