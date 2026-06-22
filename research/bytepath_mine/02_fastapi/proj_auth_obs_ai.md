# auth / observability / AI-app serving — mechanism-level mine

**Sources:** `fastapi-users` v15.0.5 · `sentry-python` (HEAD 2026-05-30) · `langserve` (HEAD 27e57af) · `r2r` (extracted) · `hayhooks` (extracted).
**Teardowns read:** `FASTAPI_USERS.md`, `SENTRY_FASTAPI.md`, `LANGSERVE.md`.
**Lancelot state cross-check:** `lancelot/auth.py`, `lancelot/security.py`, `lancelot/asgi.py`, `lancelot/metrics.py`.

---

## AUTH — fastapi-users transport × strategy

### A1. `auto_error=False` on security schemes for multi-backend fall-through
**Mechanism:** `BearerTransport.__init__` sets `OAuth2PasswordBearer(tokenUrl, auto_error=False)` (transport/bearer.py:22); `CookieTransport.__init__` sets `APIKeyCookie(name, auto_error=False)` (transport/cookie.py:30). When `auto_error=False`, a missing credential yields `None` instead of raising 401 at extraction time — letting `Authenticator._authenticate` loop through multiple backends. A backend whose credential is absent simply gets `token=None` and is skipped (authenticator.py:176-177). All FastAPIUsers transports set this.

**Byte-path relevance:** Lancelot's `HTTPBearer`, `APIKeyHeader`, `APIKeyCookie`, `APIKeyQuery` (security.py:59-133) already have `auto_error` params. Lancelot's HAVE already parallels this. **HAVE.**

**Lancelot check hint:** `lancelot/security.py` — all `_APIKeyBase`/`HTTPBearer`/`HTTPBasic` constructors accept `auto_error=` (lines 59-133).

---

### A2. Runtime-synthesized multi-scheme dependency via `makefun.with_signature`
**Mechanism:** `Authenticator._get_dependency_signature` (authenticator.py:193-237) builds `list[inspect.Parameter]` at runtime — one `Depends(backend.transport.scheme)` + one `Depends(backend.get_strategy)` per configured backend — then calls `makefun.with_signature(Signature(parameters))` to materialize a `*args/**kwargs` coroutine as a typed function that FastAPI's OpenAPI introspector sees as having N named `Depends`-defaults. Without this, a `**kwargs` function exposes zero security schemes in `/docs`. The `name_to_variable_name` sanitizer strips non-`[0-9a-zA-Z_]` chars; `DuplicateBackendNamesError` is raised on name collision. (authenticator.py:193-237, 107-151)

**Byte-path relevance:** This is a FastAPI DI/OpenAPI trick. It has zero byte-path weight — it only affects `/docs` rendering and static introspection. Not a transport concern.

**VERDICT: SKIP for Lancelot.** Lancelot owns transport, not auth library infrastructure. Implementing a makefun-style multi-backend authenticator is AI-logic territory (user-side auth config).

---

### A3. 401-vs-403 policy gate on authenticated-but-forbidden
**Mechanism:** `Authenticator._authenticate` (authenticator.py:552-563): status starts 401; if a user is found it flips to 403; if `active=True` and user is inactive → back to 401+drop user (re-authenticate); if `verified` or `superuser` check fails → drop user but keep 403. So the 401/403 distinction is: "no valid credential / inactive" = 401, "valid credential but policy failed" = 403. This is intentional and correct per HTTP semantics.

**Byte-path relevance:** Pure auth logic. Not a byte-path concern for Lancelot. No action.

---

### A4. R2R dual-credential `auth_wrapper`: JWT-then-API-key fallback in one Depends
**Mechanism:** `AuthProvider.auth_wrapper()` (auth_provider_base.py:122-207) returns a single `Security(self.security)` dependency (`HTTPBearer(auto_error=False)`) that tries: (1) JWT decode, (2) API key from Bearer token (format `key_id.raw_api_key`), (3) `X-API-Key` header. All three in one handler, each try/except independently. If no credentials AND `require_authentication=False` → returns default admin user (useful for dev). Uses `APIKeyHeader(name="X-API-Key", auto_error=False)` (auth_provider_base.py:24) combined with `HTTPBearer(auto_error=False)` (line 45).

**Byte-path relevance:** The transport pattern (two schemes, auto_error=False on both) is already HAVE in Lancelot. The JWT-then-API-key fallback logic is user-side auth, not transport.

**VERDICT: SKIP.** AI/user logic.

---

### A5. Self-invalidating reset tokens via embedded password fingerprint
**Mechanism:** `BaseUserManager.forgot_password` (manager.py:358-384) embeds `password_fgpt = password_helper.hash(user.hashed_password)` in the reset JWT. On `reset_password` (manager.py:386-438): `verify_and_update(user.hashed_password, password_fgpt)` — if the password changed since the token was minted, the stored hash differs, fingerprint fails → invalid token. Audience scoping (`aud="fastapi-users:reset"`, enforced by `decode_jwt`) prevents cross-replaying reset tokens as auth tokens.

**Byte-path relevance:** Pure stateless crypto logic. Not a byte-path concern.

---

## OBSERVABILITY — sentry-python ASGI instrumentation seams

### O1. `Starlette.__call__` monkeypatch as outermost ASGI wrapper (zero-user-code injection)
**Mechanism:** `patch_asgi_app()` (starlette.py:439-467) replaces `Starlette.__call__` on the class with a closure that constructs `SentryAsgiMiddleware(lambda *a,**kw: old_app(self, *a, **kw), ...)` and runs the request through it. The lambda re-binds `self` because a class method was patched. The recursion guard (`_asgi_middleware_applied` contextvar, asgi.py:200,220) prevents double-wrapping nested ASGI apps. Lifespan (`scope["type"] == "lifespan"`) is passed straight through.

**Byte-path relevance for Lancelot:** Lancelot does NOT need this (it's the framework being instrumented, not the SDK that hooks it). However, the `_asgi_middleware_applied` contextvar recursion guard is the right pattern for any outermost ASGI middleware in Lancelot (e.g. `TenantConcurrencyLimiter`, compression middleware) to avoid double-applying when sub-apps are mounted.

**Lancelot check hint:** `lancelot/asgi.py` — check whether sub-app mounting (App.mount) can lead to double-traversal of Lancelot's own middleware stack. **IMPROVE: add contextvar recursion guard to Lancelot's outermost ASGI dispatch path if sub-app mounting is used.**

---

### O2. Two-phase transaction naming: provisional-URL → route-template rename
**Mechanism:** The ASGI middleware opens the transaction with a provisional name (URL or generic string, asgi.py:362-364) before routing. Because `scope["route"]` is only populated **after** routing happens, a second patch (`patch_request_response` starlette.py:481-535, `patch_get_request_handler` fastapi.py:78-159) re-renames from **`request.scope["route"].path`** — the route **template** (e.g. `/items/{id}`), NOT the concrete path `/items/42`. FastAPI intentionally puts the `Route` object in the ASGI scope for exactly this (asgi.py:454-455 comment). The `event_processor` (asgi.py:400-427) applies the same `already_set` guard so an error captured mid-request gets the best available name.

**Byte-path relevance for Lancelot:** CRITICAL for RED metrics correctness. Lancelot already stores the route template in `scope["_lancelot_route"]` at match time (asgi.py:657) and uses it as `_route_label` in `record_request` (asgi.py:430-431). So Lancelot already gets this right — route template, not concrete URL, for cardinality safety.

**VERDICT: HAVE.** `scope["_lancelot_route"]` at match site → `record_request` uses it.

---

### O3. `http.response.start` first-message status capture
**Mechanism:** `_sentry_wrapped_send` (asgi.py:328-348) inspects the ASGI send event: `if event.get("type") == "http.response.start" and "status" in event: span.set_http_status(event["status"])`. This is the only place the HTTP status lives in the ASGI protocol — it's the first `send` message. The span is marked errored on `>= 400`. This exact technique (peek `http.response.start`) is used by Ray Serve and others.

**Byte-path relevance for Lancelot:** Lancelot captures status in `_final_status_holder` via a `send` wrapper in `asgi.py` already. HAVE.

---

### O4. Middleware span via `Middleware.__init__` monkeypatch + receive/send sub-span wrapping
**Mechanism:** `patch_middlewares()` (starlette.py:410-436) replaces `Middleware.__init__` to substitute each middleware class with a span-instrumented subclass (`_enable_span_for_middleware`). The wrapping logic (starlette.py:150-241): opens a span for the middleware `__call__`, wraps the passed `receive` and `send` callables in child spans, but guards re-wrapping with `receive_name == "_sentry_receive"` (string comparison on `__name__`) to avoid infinite nesting when the same callables flow through a stack.

**Byte-path relevance for Lancelot:** Lancelot's `@app.middleware("http")` adapter already provides a span/hook point for user-defined middleware. The interesting piece for Lancelot is the `receive_patched`/`send_patched` guard — if a future Lancelot middleware passes `receive`/`send` through multiple layers, the `__name__` idempotency check avoids double-wrapping. **IMPROVE: document/implement the receive/send re-wrap guard for Lancelot's middleware chaining.**

---

### O5. `ExceptionMiddleware` patch to capture handled exceptions
**Mechanism:** `patch_exception_middleware` (starlette.py:267-354). Starlette's `ExceptionMiddleware` catches exceptions that have registered handlers before they reach the outer ASGI middleware — so an unpatched outer middleware never sees them. Sentry patches each handler via an MRO walk (`for cls in type(exp).__mro__:`) and gates capture on `exp.status_code in integration.failed_request_status_codes` (default: 5xx). Sets `mechanism={"handled": True}`. The patched `__call__` also calls `_add_user_to_sentry_scope(scope)` BEFORE delegating because at the time of a handled error the auth middleware has already run — but user attachment would normally happen after the exception middleware's post-call, which never runs (starlette.py:340-350 comment).

**Byte-path relevance for Lancelot:** Lancelot exposes `@app.exception_handler(ExcCls)` decorators (M2 conformance push). Whether these handlers bubble exceptions or not affects what Lancelot's ASGI outer layer sees. The key insight: if Lancelot's exception handlers swallow 5xx errors, those errors are invisible to any outer ASGI middleware or RED metrics unless the handler layer explicitly records them.

**VERDICT: IMPROVE — verify Lancelot's exception handler layer correctly records status for `record_request` even when a user exception handler returns a custom response (not re-raises).** The asgi.py `send` wrapper already sees the status from `http.response.start`, so if the handler returns a 500 response (even a custom one), `_final_status_holder` captures it. No fix needed unless handlers suppress `http.response.start`.

---

### O6. Idempotency guards on monkeypatches (`"_sentry_*" not in str(old_fn)`)
**Mechanism:** All Sentry patches guard against double-application: `"_sentry_middleware_init" not in str(old_middleware_init)` (starlette.py:417), `not_yet_patched = old_call.__name__ not in [...]` (starlette.py:232-236). The string-in-repr approach is fragile but works.

**Byte-path relevance:** If Lancelot ever monkey-patches its own internals or exposes hooks that might be called multiple times (e.g. SDK init), use a sentinel attribute on the target function rather than string matching. Low priority — Lancelot does not currently monkeypatch.

---

## AI-APP SERVING — LangServe, R2R, Hayhooks

### S1. `include_in_schema=False` real handler + typed no-op docs handler on the SAME path
**Mechanism:** LangServe `add_routes` (server.py:496-503, 762-793) registers two routes at the same path and method. First: `@app.post(path, include_in_schema=False)` with `async def handler(request: Request)` — raw Request for manual/config-aware validation, invisible to OpenAPI. Second (registered later, same path): a typed `async def _invoke_docs(invoke_request: Annotated[InvokeRequest, Body()]) -> InvokeResponse` with `response_model=InvokeResponse` whose body `raise AssertionError("should not be reachable")`. FastAPI matches the first-registered route for a given method+path, so the real handler always serves traffic; the docs stub only contributes OpenAPI metadata. The contract: "manual validation with accurate docs."

**Byte-path relevance for Lancelot:** This is the canonical pattern for any Lancelot route that needs to handle raw `Request` (e.g. streaming, dynamic schema, large body pass-through) but still wants typed OpenAPI docs. Lancelot's MCP tool routes and `@app.stream` endpoints face this same tension.

**VERDICT: IMPROVE — document and potentially implement this pattern for Lancelot's dynamic-schema endpoints (stream, batch, MCP). No new surface; just a documented recipe for users and for Lancelot's own internal route registration where response types are dynamic.**

---

### S2. Dynamic per-handler Pydantic model generation: `pydantic.create_model(f"{namespace}Request", field=(type, Field(...)))` + `model_rebuild()`
**Mechanism:** LangServe `create_invoke_request_model` (validation.py:38-66): `create_model(f"{namespace}InvokeRequest", input=(input_type, Field(...)), config=(config, Field(default_factory=dict)), kwargs=(dict, Field(default_factory=dict)))` then `model_rebuild()`. Hayhooks `create_request_model_from_callable` (pipelines/models.py:60-82): reflects `inspect.signature(func).parameters`, maps each to `(annotation, Field(default=..., description=...))`. The critical detail from LangServe: **`_rename_pydantic_model` recursively namespaces nested models** (api_handler.py:308-305) — top-level rename is not enough, nested `BaseModel` fields also need prefixing or FastAPI's OpenAPI generator silently collides `components/schemas`. Hayhooks additionally infers `response_model=None` for streaming return types (Generator/AsyncGenerator/Response subclasses) and sets `response_class=StreamingResponse` for generators (pipelines/models.py:140-168).

**Byte-path relevance for Lancelot:** Lancelot's `@app.tool` already auto-derives output_schema from return annotations. The gap: if Lancelot ever hosts multiple dynamically-registered handlers with structurally identical nested types, it may produce colliding OpenAPI `components/schemas` names. Hayhooks' `app.openapi_schema = None; app.setup()` after each route addition (deploy_utils.py:553-554) is the flush pattern to invalidate cached schema.

**VERDICT: IMPROVE — if Lancelot adds runtime route registration (e.g. `app.add_api_route` after startup), ensure `app.openapi_schema = None` is called to flush the cached schema.** Lancelot likely doesn't have this today. Note: `app.setup()` is Starlette/FastAPI internal; Lancelot's Python App class may not have it.

---

### S3. SSE event taxonomy: `metadata`/`data`/`end`/`error` with error scrubbing
**Mechanism:** LangServe `_stream()` generator (api_handler.py:1177-1222): first SSE event is `{"event": "metadata", "data": json.dumps({run_id, ...})}`, then `{"event": "data", "data": serializer.dumps(chunk).decode("utf-8")}` per chunk, then `{"event": "end"}`. On `BaseException`: `{"event": "error", "data": json.dumps({"status_code": 500, "message": "Internal Server Error"})}` then `raise` (the real error text is never exposed to the client — comment at api_handler.py:1213-1215). R2R `SSEFormatter` (shared/sse_formatter.py) uses the same `yield_sse_event(event_name, payload)` helper, adding named events: `message`, `thinking`, `citation`, `search_results`, `tool_call`, `tool_result`, `final_answer`, `error`, `done` (the done event payload is the literal string `"[DONE]"`). The `yield_sse_event` helper (sse_formatter.py:6-27) formats as `event: {name}\ndata: {json}\n\n`.

**Byte-path relevance for Lancelot:** Lancelot's `@app.stream` already handles SSE keepalive and TTFT metric. The taxonomy question: Lancelot currently emits raw bytes/str/Event from the user generator. For AI-app operators, a standardized `metadata/data/end/error` envelope (with error scrubbing) is the right default so callers know the contract.

**VERDICT: IMPROVE — document the `metadata`/`data`/`end`/`error` SSE envelope as the recommended Lancelot AI-streaming convention. Could ship as a lightweight `lancelot.sse.SSEEvent` helper that wraps the user's async-generator with the standard envelope + error scrubbing (`generic 500, never leak exception text`).** This is operator-experience, not byte-path performance.

---

### S4. `StreamingResponse` with chunk-size capping (1 KB cap per SSE chunk)
**Mechanism:** R2R `rag_app` handler (deep/retrieval_router.py:408-424):
```python
async def stream_generator():
    async for chunk in response:
        if len(chunk) > 1024:
            for i in range(0, len(chunk), 1024):
                yield chunk[i : i + 1024]
        else:
            yield chunk
return StreamingResponse(stream_generator(), media_type="text/event-stream")
```
The cap is 1 KB per yielded chunk. Purpose: avoid sending enormous single chunks that block the client's SSE parser or hit proxy buffering thresholds.

**Byte-path relevance for Lancelot:** Lancelot's R3 pump uses `LANCELOT_STREAM_BUFFER` (default 256 channel slots) for backpressure, but does not enforce a per-chunk byte cap. For AI apps that produce large intermediate tool-result chunks, a configurable per-chunk size cap could prevent proxy/client parser stalls.

**VERDICT: LOW PRIORITY micro-opt.** Real LLM streaming is token-at-a-time (~few bytes/chunk), so oversized chunks are unlikely unless the user yields large blobs. The R3 pump's backpressure handles throughput; a byte cap addresses a different (rare) pathology. Skip unless a production report flags it.

---

### S5. Per-request config allow-list projection (config-key security boundary)
**Mechanism:** LangServe `_unpack_request_config` (api_handler.py:157-358): `projected_config = {k: config[k] for k in config_keys if k in config}`. Only keys the server explicitly allowlisted in `add_routes(config_keys=[...])` survive. The merge order is: `overridable_defaults` → `projected_client_config` → `server_non_overridable` (run_name, __langserve_* metadata always win). The `per_req_config_modifier` hook (server.py:450) runs AFTER projection and is the sanctioned injection point for auth-derived config (e.g. user_id into configurable).

**Byte-path relevance for Lancelot:** Lancelot's `@app.batch(key=...)` and MCP tool routing already have per-request config merging. The allowlist pattern is the right design for any Lancelot feature that forwards client-supplied config to an upstream (e.g. a future "client hints for LLM model selection"). Not an immediate gap but a design lesson.

---

### S6. Route factory: `add_routes(app, handler, path)` attaches a fixed endpoint set to `FastAPI | APIRouter`
**Mechanism:** LangServe `add_routes` (server.py:248-460): accepts `Union[FastAPI, APIRouter]`, attaches `invoke/batch/stream/stream_log/stream_events/input_schema/output_schema/config_schema/playground` at `{path}/*`. Hayhooks `add_pipeline_api_route` (deploy_utils.py:460-554): `app.add_api_route(f"/{pipeline_name}/run", endpoint, methods=["POST"], name=f"{pipeline_name}_run", response_model=RunResponse, tags=["pipelines"])` + `app.openapi_schema = None; app.setup()` to flush schema cache.

**Byte-path relevance for Lancelot:** Lancelot's `app.include_router` is the existing equivalent. The new signal: Hayhooks does hot-route registration with explicit schema cache invalidation. Lancelot does not currently support runtime route addition after startup (the routes list is materialized at `run()` time). If Lancelot adds a runtime `add_api_route` surface for dynamic pipelines, the `openapi_schema = None` flush is required.

---

### S7. Hayhooks prepare/commit two-phase deploy: expensive I/O off the event loop
**Mechanism:** `deploy_pipeline_yaml_async` / `deploy_pipeline_files_async` (deploy_utils.py:76-121): expensive work (file I/O, YAML parsing, module loading, wrapper `setup()`) runs in `asyncio.to_thread(deploy_pipeline_yaml, ...)`. A `threading.Lock` (not `asyncio.Lock` — comment at line 53: "never on the event loop itself") serializes deploys when `deploy_concurrency == SERIALIZED`. The commit step (registry mutation + `app.routes` mutation) runs on the calling thread. Parallel startup: `ThreadPoolExecutor(max_workers=N)` for prepare, then serial commit + one `rebuild_openapi` at end (app.py:173-192).

**Byte-path relevance for Lancelot:** If Lancelot adds dynamic pipeline/tool registration, the pattern is: (a) `asyncio.to_thread` for expensive setup (file I/O, model loading), (b) `threading.Lock` (not `asyncio.Lock`) for the serialized commit that mutates `app.routes`, (c) single `openapi_schema = None` flush after batch commits. The `threading.Lock` vs `asyncio.Lock` distinction is load-bearing: dynamic deploy from an endpoint handler runs on the event loop, so an asyncio.Lock would deadlock if the handler is also in the pool.

**VERDICT: IMPROVE — document as the Lancelot recipe for any future dynamic-route-registration feature.** Not immediately needed but correct pattern when it arrives.

---

### S8. Rate-limiting as a per-router yield-dep that logs POST/DELETE only
**Mechanism:** R2R `BaseRouterV3.set_rate_limiting` (base_router.py:97-151): builds a yield-dep `rate_limit_dependency` that: (1) skips superusers; (2) fetches user from DB; (3) calls `limits_handler.check_limits(user, route)` → 429 on ValueError; (4) stashes `request.state.user_id` + `request.state.route`; (5) in `finally`, logs only POST/DELETE (skips GET reads). The `route` key is `request.scope["path"]` (concrete path, not template). The dep is attached per-router: `dependencies=[Depends(self.rate_limit_dependency)]` on each route registration (retrieval_router.py:89,241,429).

**Byte-path relevance for Lancelot:** Lancelot has `lancelot.ratelimit` with token-bucket logic. R2R's per-user DB-backed approach is user-side logic (requires a DB). The pattern of logging only mutating methods (POST/DELETE, not GET) is an operator UX improvement — avoid rate-limit log pollution from health probes / read endpoints.

**VERDICT: LOW. The DB-backed per-user rate-limit is user-side. The "log only POST/DELETE" pattern is a minor UX note.**

---

### S9. `openapi_extra={"x-codeSamples": [...]}` on routes for per-language SDK examples
**Mechanism:** R2R retrieval router (retrieval_router.py:91-138) uses FastAPI's `openapi_extra` kwarg to inject `x-codeSamples` (Redocly extension) with Python/JavaScript/Shell examples directly into the OpenAPI spec for each route. This makes the live `/docs` surface also document multi-language SDK usage.

**Byte-path relevance for Lancelot:** Zero byte-path impact. This is a docs-quality feature. SKIP.

---

### S10. OPAL: WebSocket pub/sub with JWT-scoped topic permissions
**Mechanism:** OPAL `PubSub` (opal-server/pubsub.py): mounts `@pubsub_router.websocket("/ws")` with `Depends(WebsocketJWTAuthenticator(signer))`. If claims are None → close WebSocket immediately (line 182). Topic permissions: `_verify_permitted_topics` (pubsub.py:205-217) reads `channel.context["claims"]["permitted_topics"]` — if present, unauthorized topics raise `Unauthorized`. The `ClientTracker` uses `current_client` ContextVar to track per-connection info across async callbacks. The `ClientInfo.refcount` protects against concurrent reconnects from the same host from clobbering tracking state. `EventBroadcaster` enables multi-node fan-out over Redis/Kafka.

**Byte-path relevance for Lancelot:** Lancelot's WebSocket support exists (WS metaclass, conformance pass). The JWT-claim → topic-scoping pattern is a good reference for any Lancelot WebSocket auth if users ask. The `refcount` on ClientTracker is a correctness pattern for multi-connect scenarios. Not an immediate gap.

---

## Summary table

| ID | Mechanism | Lancelot status | Action |
|----|-----------|-----------------|--------|
| A1 | `auto_error=False` multi-backend fall-through | HAVE (security.py) | — |
| A2 | makefun dynamic security dep synthesis | NOT needed (user-side) | SKIP |
| O1 | Recursion guard contextvar in outermost ASGI | PARTIAL (sub-app mount may lack guard) | IMPROVE |
| O2 | Route-template (not URL) for metric labels | HAVE (`_lancelot_route`) | — |
| O3 | `http.response.start` status capture | HAVE | — |
| O4 | Receive/send re-wrap guard in middleware chain | NOT documented | IMPROVE (doc) |
| O5 | Exception-handler-level error visibility for metrics | HAVE via send-wrapper | — |
| S1 | `include_in_schema=False` + typed docs-only stub | NOT documented as pattern | IMPROVE (recipe) |
| S2 | Dynamic `create_model` + `model_rebuild` + namespace | GAP: no runtime route add today | IMPROVE (flush pattern) |
| S3 | `metadata/data/end/error` SSE envelope + error scrubbing | NOT formalized | IMPROVE (helper/doc) |
| S4 | 1 KB per-chunk cap in StreamingResponse | NOT needed (R3 pump has backpressure) | SKIP |
| S5 | Config allow-list projection | DESIGN LESSON for future | NOTE |
| S7 | Two-phase prepare+commit for dynamic routes | NOT needed today | NOTE |
| S8 | POST/DELETE-only rate-limit logging | Minor UX note | LOW |
| S10 | WebSocket JWT-claim topic scoping | Reference only | NOTE |
