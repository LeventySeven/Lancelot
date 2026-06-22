# Domain brief: dx_conformance

**Domain:** Developer experience, FastAPI byte-parity, adoption friction  
**Synthesized:** 2026-06-22  
**Source corpus:** `01_robyn/`, `02_fastapi/`, `04_serving/`, `05_lancelot_state/` mine notes (all files read)  
**Conformance baseline:** 974/978 = 99.6% (4 remaining divergences are documented no-bullshit-boundary failures)

---

## Summary

Lancelot's DI surface, security extractors, route-class ergonomics, and CORS/GZip middleware are full FastAPI parity or better. The 4 remaining conformance failures are intentional. The migration friction that remains is concentrated in three clusters: (1) OpenAPI output quality — streaming routes show `application/json` in docs/SDK output instead of `text/event-stream`, and `servers`/`description`/`tags` are silently dropped from `build_openapi`; (2) security-scheme import coverage — `OAuth2AuthorizationCodeBearer` (OIDC providers), `OpenIdConnect`, and `OAuth2PasswordRequestFormStrict` are missing from `lancelot.security`; (3) three correctness bugs — `Decimal` always serialized as `float` instead of `int` when `exponent≥0`, `inspect.signature` called without `eval_str=True` (silent parameter skipping under `from __future__ import annotations`), and no `contextmanager_in_threadpool` with an unlimited CapacityLimiter for sync yield-dep `__exit__` (real deadlock risk under load). All three clusters have small fixes (~1-30 lines each). Everything else is either HAVE or correctly REFUSE.

---

## must_build

### 1. SSE/JSONL streaming media type in OpenAPI

**win:** `@app.stream` routes show `application/json` in `/openapi.json` and Swagger UI. Speakeasy, openapi-typescript, and any SDK codegen that consumes the spec will generate wrong client code — streaming consumers instead of one-shot JSON parsers. This is the marquee feature.  
**why:** The generated spec is the contract for SDK consumers. `application/json` on a streaming endpoint causes generated clients to buffer the full body and call `response.json()`, breaking them silently.  
**source_cite:** `fastapi/openapi/utils.py:357-409`; `02_fastapi/openapi_security.md §SSE/JSONL streaming media type`  
**lancelot_state:** MISSING. `lancelot/openapi.py:build_openapi` emits `200: {content: {application/json: ...}}` unconditionally. `@app.stream`-decorated routes are not detected.  
**buildability:** S (~20-30 lines). Detect `stream=True` flag on the route object; emit `text/event-stream` with `{schema: {type: string}}` or `application/jsonl` with `{schema: itemSchema}` depending on `sse=True`. No new dep.  
**gate_risk:** LOW. OpenAPI output change; no hot-path impact. Run conformance: the 4 known divergences are test-file-environmental, not OpenAPI-surface tests.

---

### 2. `servers` / `description` / `tags` forwarding in `build_openapi`

**win:** `app = App(description="My AI API", servers=[{"url": "https://api.example.com"}], tags=[...])` passes description and server list into the generated OpenAPI spec. Currently all three are silently dropped.  
**why:** `servers` is load-bearing for SDK codegen base URL — without it, every generated SDK client targets `localhost`. `description` shows in Swagger UI and readme pages. `tags` control operation grouping.  
**source_cite:** `fastapi/openapi/utils.py:514-530`; `02_fastapi/openapi_security.md §servers/description/tags`  
**lancelot_state:** MISSING. `lancelot/openapi.py:314` (`build_openapi` signature) takes only `title` and `version`. App constructor does not store these.  
**buildability:** S (~10-15 lines). Add `description`, `servers`, `openapi_tags` kwargs to `App.__init__`; pass through to `build_openapi`; merge into the returned dict. No dep.  
**gate_risk:** LOW. Additive to the schema; existing tests do not assert the absence of these keys.

---

### 3. `OAuth2AuthorizationCodeBearer` stub in `lancelot.security`

**win:** `from lancelot.security import OAuth2AuthorizationCodeBearer` works. This is the scheme used by Auth0, Okta, and AWS Cognito — the three most common OIDC providers in production FastAPI apps. Without it, any migration that uses OIDC fails at import time with a hard `ImportError`.  
**why:** Auth0/Okta/Cognito all use Authorization Code flow. Any FastAPI app authenticating with these providers imports `OAuth2AuthorizationCodeBearer`. It's in the FastAPI public API. Missing it is a migration blocker for a large fraction of real apps.  
**source_cite:** `fastapi/security/oauth2.py:547-650`; `02_fastapi/openapi_security.md §OAuth2AuthorizationCodeBearer`  
**lancelot_state:** MISSING. `lancelot/security.py` has `OAuth2PasswordBearer` but not `OAuth2AuthorizationCodeBearer`. Grep confirms the class is not present and the file header explicitly lists it in a "P7 quarantine" comment.  
**buildability:** S (~20 lines). The class is a thin stub: `authorizationUrl`, `tokenUrl`, `scopes` kwargs → OpenAPI security scheme `oauth2` with `authorizationCode` flow. No network calls. No new dep.  
**gate_risk:** LOW. Pure surface addition. Does not touch routing or hot path. Run conformance gate to confirm no collision with vendored tests.

---

### 4. `Decimal` encoder: emit `int` when `exponent >= 0`

**win:** `Decimal("100")`, `Decimal("1.23E+5")` serialize to `100`, `123000` (integers) instead of `100.0`, `123000.0`. Fixes financial-value round-trip correctness for APIs that handle monetary amounts.  
**why:** FastAPI's jsonable encoder (`fastapi/encoders.py:170-172`) does `int(obj)` when `obj.as_tuple().exponent >= 0`. Lancelot always does `float(obj)`. A value like `Decimal("100")` becomes `100.0` in Lancelot vs `100` in FastAPI — JSON-Schema `integer` validators reject `100.0`, and financial APIs that round-trip through JSON lose type fidelity.  
**source_cite:** `fastapi/encoders.py:170-172`; `02_fastapi/di_params.md §Decimal encoder`  
**lancelot_state:** BUG. `lancelot/validation.py:254-255`: `if isinstance(obj, _dcm.Decimal): return float(obj)`. Missing the `exponent >= 0` branch.  
**buildability:** TRIVIAL (2 lines). Replace with: `exp = obj.as_tuple().exponent; return int(obj) if exp >= 0 else float(obj)`.  
**gate_risk:** VERY LOW. One-line change in `_jsonable_default`. Run conformance; this matches FastAPI behavior exactly.

---

### 5. `inspect.signature(fn, eval_str=True)` for stringified annotations

**win:** Route handlers annotated with `from __future__ import annotations` (PEP 563 deferred evaluation) have their type hints correctly resolved during DI wiring. Without this, `inspect.signature` sees the string literal `"Request"` instead of the class, and the parameter is silently skipped — the route never receives the injected value.  
**why:** `from __future__ import annotations` turns all annotations into strings until evaluated. `inspect.signature` without `eval_str=True` returns `<class 'inspect._empty'>` for string-annotated parameters. FastAPI calls `inspect.signature(fn, eval_str=True)` to resolve them before DI analysis (`fastapi/dependencies/utils.py:213-225`). Lancelot calls `inspect.signature(fn)` (no `eval_str`). The bug is silent: the parameter is not wired, and no error is raised.  
**source_cite:** `fastapi/dependencies/utils.py:213-225`; `02_fastapi/di_params.md §eval_str`  
**lancelot_state:** BUG. `lancelot/di.py:117,123`: both `inspect.signature(_call)` and `inspect.signature(fn)` calls are missing `eval_str=True`.  
**buildability:** TRIVIAL (2 lines). Add `eval_str=True` to both call sites.  
**gate_risk:** LOW. `eval_str` requires Python 3.10+. Lancelot requires Python 3.13. No compatibility risk. Run conformance + test with `from __future__ import annotations` route.

---

### 6. `contextmanager_in_threadpool` with unlimited CapacityLimiter

**win:** Sync yield-dep `__exit__`/`__aexit__` (cleanup in a sync contextmanager used as a Depends) runs in a worker thread with an unbounded CapacityLimiter, matching FastAPI's behavior. Without this, a thread pool that fills under load causes `anyio.from_thread.run_sync` to block waiting for a slot — while holding a DB connection or lock — deadlocking the event loop.  
**why:** FastAPI's `fastapi/concurrency.py:17-41` wraps sync contextmanager exit in a new `CapacityLimiter(math.inf)` — effectively unlimited — to avoid blocking on anyio's default thread pool limit when exiting a sync yield-dep under high concurrency. Without the override, the default anyio limit (40 threads) becomes the bottleneck for closing DB connections or file handles, producing a deadlock if 40+ requests all have yield-deps exiting simultaneously.  
**source_cite:** `fastapi/concurrency.py:17-41`; `02_fastapi/core.md §contextmanager_in_threadpool`  
**lancelot_state:** MISSING. `lancelot/di.py` has no `contextmanager_in_threadpool`. Sync yield-dep `__exit__` is run via `anyio.to_thread.run_sync` without a custom CapacityLimiter.  
**buildability:** S (~15 lines). Port `contextmanager_in_threadpool` from FastAPI: wrap the sync `__exit__` call in `anyio.to_thread.run_sync(..., limiter=CapacityLimiter(math.inf))`. The `anyio` dep is already present (Starlette uses it). No new dep.  
**gate_risk:** MEDIUM. Touches the yield-dep teardown path. Must run against the 11 passing yield-dep conformance tests after the change. No hot-path impact (only affects sync contextmanager yield-deps).

---

## improve

### I1. `Event.retry` field in `lancelot.stream.Event`

**what:** Add `retry: int | None = None` to the `Event` dataclass and emit `retry: {ms}\n` in `render()` when set.  
**why:** The SSE spec `retry:` field tells the client how long to wait before reconnecting after a disconnect. Robyn's `SSEMessage` ships it (`robyn/responses.py:167-206`). Clients using the browser `EventSource` API (including Cursor, Claude.ai, and most AI streaming frontends) use this to tune reconnect behavior. Without it, clients use browser defaults (typically 3s).  
**source_cite:** `01_robyn/python_bridge.md §SSEMessage.retry`; SSE spec RFC; `01_robyn/python_bridge.md §SSEMessage`  
**lancelot_state:** PARTIAL. `lancelot/stream.py:24-40` has `Event(name, data, id)` — `retry` absent.  
**buildability:** TRIVIAL (~5 lines). Add field, add render branch.  
**gate_risk:** VERY LOW. Additive to Event; no existing tests check absence of `retry`.

---

### I2. `_html_safe_json` in docs.py (XSS fix for Swagger UI)

**what:** Escape `<`, `>`, and `&` in the OpenAPI JSON blob injected into the Swagger/ReDoc HTML page (`/docs`, `/redoc`).  
**why:** The OpenAPI spec may contain user-supplied strings (route descriptions, example values, enum labels). If they contain `</script>`, a schema stored in an `APIRoute.description` field can inject script tags into the Swagger UI page. FastAPI escapes these via `_html_safe_json` (`fastapi/openapi/docs.py:45`). Lancelot's `lancelot/openapi.py` docs functions do not.  
**source_cite:** `02_fastapi/openapi_security.md §_html_safe_json`; `fastapi/openapi/docs.py:45`  
**lancelot_state:** MISSING. `lancelot/openapi.py` `_docs_html` inlines the spec JSON without escaping.  
**buildability:** TRIVIAL (3 lines). `spec_json.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")`.  
**gate_risk:** VERY LOW. The output is still valid JSON (Unicode escapes are equivalent). No spec consumers are affected.

---

### I3. `OpenIdConnect` stub in `lancelot.security`

**what:** Add `class OpenIdConnect(SecurityBase)` with `openIdConnectUrl` kwarg → OpenAPI `openIdConnect` scheme. Does not perform any validation (same pattern as FastAPI's stub).  
**why:** Some FastAPI apps import `from fastapi.security import OpenIdConnect` to get OpenAPI spec generation for OIDC discovery document URLs. The class itself is a stub (no token validation logic — that is user-side). Absence causes an `ImportError` on migration.  
**source_cite:** `fastapi/security/open_id_connect_url.py`; `02_fastapi/openapi_security.md §OpenIdConnect`  
**lancelot_state:** MISSING. Not in `lancelot/security.py`.  
**buildability:** TRIVIAL (~15 lines). Thin stub class.  
**gate_risk:** VERY LOW.

---

### I4. `OAuth2PasswordRequestFormStrict` in `lancelot.security`

**what:** Add `OAuth2PasswordRequestFormStrict` as a strict-mode variant of `OAuth2PasswordRequestForm` that rejects requests not explicitly setting `grant_type="password"`.  
**why:** FastAPI ships this in `fastapi/security/oauth2.py:162-327`. Apps that follow OAuth2 spec strictly (checking `grant_type`) import it. Missing causes `ImportError` on migration.  
**source_cite:** `fastapi/security/oauth2.py:162-327`; `02_fastapi/openapi_security.md §OAuth2PasswordRequestFormStrict`  
**lancelot_state:** MISSING.  
**buildability:** TRIVIAL (~5 lines). Variant of `OAuth2PasswordRequestForm` with `grant_type` field set to `Literal["password"]`.  
**gate_risk:** VERY LOW.

---

### I5. `HTTPDigest` stub in `lancelot.security`

**what:** Add `class HTTPDigest(HTTPBase)` stub that registers the `http/digest` OpenAPI security scheme and extracts the `Authorization: Digest ...` header, raising 401 if absent.  
**why:** Some FastAPI apps import `HTTPDigest` for OpenAPI scheme registration only (the actual digest challenge/response is handled elsewhere or they're migrating legacy code). Missing causes `ImportError`.  
**source_cite:** `fastapi/security/http.py:319-417`; `02_fastapi/openapi_security.md §HTTPDigest`  
**lancelot_state:** MISSING. `lancelot/security.py` has `HTTPBearer` and `HTTPBasic` but not `HTTPDigest`.  
**buildability:** TRIVIAL (~10 lines). Copy `HTTPBearer` shape; scheme name = `"digest"`.  
**gate_risk:** VERY LOW.

---

### I6. `get_api_router_class` factory utility

**what:** A 10-line factory `get_api_router_class(route_class)` that returns an `APIRouter` subclass with `route_class` locked to the given class. Lets users propagate a custom `APIRoute` subclass across a module hierarchy without passing `route_class=` to every `include_router` call.  
**why:** Polar and Prefect both have this pattern (`polar/kit/routing.py:187-198`). It is the ergonomic complement to the `route_class` kwarg that Lancelot already ships. Without it, a user with `AutoCommitAPIRoute` must remember to pass `route_class=AutoCommitAPIRoute` to every `APIRouter()` call in their app. One missed call silently omits the DB commit.  
**source_cite:** `02_fastapi/proj_dbweb.md §get_api_router_class`; `polar/kit/routing.py:187-198`  
**lancelot_state:** MISSING. `route_class` kwarg is HAVE; factory utility is not.  
**buildability:** TRIVIAL (~10 lines). Pure Python factory; no dep.  
**gate_risk:** VERY LOW. Does not touch routing internals.

---

### I7. `FileResponse` and `RedirectResponse` importable from `lancelot.responses`

**what:** Re-export `FileResponse` and `RedirectResponse` from Starlette so `from lancelot.responses import FileResponse` works.  
**why:** FastAPI re-exports these: `from fastapi.responses import FileResponse` is the documented pattern. Lancelot works via ASGI delegation (confirmed: `asgi.py:545`, `1617-1625`, `1805-1806`, `2093`) but the import fails. Any FastAPI app that imports from `fastapi.responses` hits `ImportError` on migration.  
**source_cite:** `05_lancelot_state/core.md §FileResponse/RedirectResponse`; Python `ImportError` confirmed via `python3 -c "from lancelot.responses import FileResponse"`.  
**lancelot_state:** BUG (import fails). Delegation logic HAVE; export MISSING.  
**buildability:** TRIVIAL (2 lines in `lancelot/responses.py`). `from starlette.responses import FileResponse, RedirectResponse`.  
**gate_risk:** VERY LOW. Starlette is already a dep.

---

### I8. SSE `Connection: keep-alive` / `Pragma: no-cache` headers

**what:** Add `Connection: keep-alive`, `Pragma: no-cache`, `Expires: 0` to the SSE response header set to complete the anti-buffering set.  
**why:** `x-accel-buffering: no` (already present, confirmed at `asgi.py:1042`) covers nginx. But HAProxy and older CDN edges also check `Cache-Control: no-cache` and `Pragma: no-cache`. Some browsers (especially Safari) read `Connection: keep-alive` for connection persistence on EventSource. Robyn ships all four headers (`robyn/response.rs:107-117`).  
**source_cite:** `01_robyn/teardown.md §SSE headers`; `robyn/response.rs:107-117`  
**lancelot_state:** PARTIAL. `asgi.py:1041-1042` emits `cache-control: no-cache` and `x-accel-buffering: no`. Missing: `pragma: no-cache`, `expires: 0`, `connection: keep-alive`.  
**buildability:** TRIVIAL (3 lines in the SSE header set in `asgi.py`).  
**gate_risk:** VERY LOW.

---

### I9. `/readyz` `starting_up` third state

**what:** When the app is in its lifespan startup phase (between process start and first `/livez` pass), `/readyz` returns `503 {"status":"starting_up"}` instead of `200 {"status":"ok"}`. After startup completes, `/readyz` returns `200` as today.  
**why:** SGLang, TGI, and Ray Serve all distinguish `starting_up` from `draining` (`04_serving/serving_frontier.md §health_generate`). k8s `startupProbe` and Render's health-check window benefit from this: if `/readyz` returns `200` before model/DB warm-up completes, traffic arrives before the handler is ready. The current two-state `ok/draining` forces operators to add `preDeployCommand` or pad `startCommand` timeouts as workarounds.  
**source_cite:** `04_serving/serving_frontier.md §three-state health`  
**lancelot_state:** PARTIAL. `app.py:313-321` has only `ok` and `draining`. No `starting_up` state.  
**buildability:** S (~10 lines). Add a `_starting_up` flag initialized `True` at `App.__init__`; cleared when lifespan `startup` completes; `/readyz` emits `503 {"status":"starting_up"}` while the flag is set.  
**gate_risk:** LOW. Touches health-probe path only. Existing tests that call `/readyz` before lifespan startup could fail if they expect `200`. Check `tests/` for pre-startup health assertions.

---

### I10. `finish_reason:"error"` terminal SSE chunk on mid-stream failure

**what:** When the R3 streaming pump encounters an error after the first chunk has been sent (HTTP 200 committed), emit a synthetic terminal SSE chunk `data: {"choices":[{"delta":{},"finish_reason":"error","index":0}]}\n\n` before closing instead of silently terminating.  
**why:** Once SSE has started, the HTTP status code cannot change. Clients using OpenAI-SDK-compatible streaming (`finish_reason` check) will hang or silently discard partial output if the stream terminates without a final chunk. This affects any vLLM-compatible consumer of Lancelot streaming endpoints.  
**source_cite:** `04_serving/gateways_deploy.md §M7`; `external/01-ai-gateways.md §OpenRouter pattern`  
**lancelot_state:** MISSING. R3 pump on error emits `CancelledError` → stream terminates without a terminal chunk. Confirmed in `gateways_deploy.md §M7`.  
**buildability:** S (~10 lines in `lancelot/stream.py` or `src/stream.rs` pump error path).  
**gate_risk:** LOW. Only fires on error paths. Confirm against `test_r3_streaming_backpressure_order_and_completeness` and `test_r3_stream_disconnect_then_server_still_serves`.

---

### I11. `UnsupportedFieldAttributeWarning` suppression for pydantic ≥2.12

**what:** Wrap `TypeAdapter(...)` calls in `warnings.catch_warnings()` to suppress `PydanticUnsupportedFieldAttributeWarning` emitted by pydantic ≥2.12 when `TypeAdapter` receives certain field kwargs.  
**why:** FastAPI suppresses this warning via `warnings.filterwarnings("ignore", category=..., module="fastapi")` (`fastapi/_compat/v2.py:141-166`). Lancelot users on pydantic 2.12+ will see spurious warnings on startup for routes that use `Field(...)` with deprecated attributes. While not a correctness issue, it pollutes operator logs.  
**source_cite:** `02_fastapi/di_params.md §UnsupportedFieldAttributeWarning`; `fastapi/_compat/v2.py:141-166`  
**lancelot_state:** MISSING. No suppression in `lancelot/di.py` or `lancelot/validation.py`.  
**buildability:** TRIVIAL (3-5 lines). Add filter at module level or at `TypeAdapter` construction sites.  
**gate_risk:** VERY LOW.

---

### I12. Document SSE `metadata/data/end/error` event envelope recipe

**what:** Add a short recipe to `docs/operator/streaming.md` (or similar) showing the standard four-event SSE envelope used by LangServe, R2R, and most AI streaming APIs: `metadata` (request metadata, before first token), `data` (token delta), `end` (final chunk with usage), `error` (if upstream fails after stream start).  
**why:** This is the de-facto event taxonomy for AI streaming (`02_fastapi/proj_auth_obs_ai.md §S3 SSE envelope`). Users building LangServe-compatible or standard AI streaming APIs need this pattern. Without documentation, every Lancelot AI-streaming user re-invents it inconsistently. No code change needed.  
**source_cite:** `02_fastapi/proj_auth_obs_ai.md §LangServe/R2R SSE pattern`  
**lancelot_state:** MISSING as documented recipe.  
**buildability:** TRIVIAL (docs only).  
**gate_risk:** NONE.

---

### I13. Document cursor-leak hazard on `@app.stream` + SQLAlchemy server-side cursors

**what:** Add a note to `@app.stream` docs: async generators that hold open DB cursors or connections MUST close them in a `finally:` block. When a client disconnects, the R3 pump leaves the generator suspended (≤1 extra chunk buffered). If the generator's `finally` is not written, the cursor leaks.  
**why:** The R3 pump's disconnect cancellation design (`src/stream.rs`, confirmed: next `try_push` sees `Closed` → coro returns, gen left suspended) is correct for performance but has a resource-management implication. The pattern is present in `02_fastapi/proj_dbweb.md §RepositoryBase.stream` as a real production hazard in FastAPI apps.  
**source_cite:** `02_fastapi/proj_dbweb.md §RepositoryBase.stream cursor-leak`  
**lancelot_state:** UNDOCUMENTED.  
**buildability:** TRIVIAL (docs only).  
**gate_risk:** NONE.

---

### I14. `x-ms-retry-after-ms` Azure header in `TransportPool._retry_after_seconds`

**what:** Add `x-ms-retry-after-ms` (Azure-specific) header parsing alongside the existing `retry-after` and `retry-after-ms` in `lancelot/llm.py:376-390`.  
**why:** Azure OpenAI endpoints return `x-ms-retry-after-ms` (milliseconds) instead of the standard `retry-after` (seconds). Without it, Lancelot ignores the Azure hint and retries immediately, hitting 429 again. One-liner; confirmed in LiteLLM `retryHandler.ts:49` and Portkey as the Azure standard.  
**source_cite:** `04_serving/gateways_deploy.md §M1-delta`; Portkey `retryHandler.ts:49`  
**lancelot_state:** MISSING. `lancelot/llm.py:376-390` handles `retry-after` and `retry-after-ms`, not `x-ms-retry-after-ms`.  
**buildability:** TRIVIAL (1 line).  
**gate_risk:** VERY LOW.

---

## have

The following are confirmed HAVE in Lancelot (verified via grep/read in this session):

- `APIKeyHeader`, `APIKeyQuery`, `APIKeyCookie`, `HTTPBearer`, `HTTPBasic`, `OAuth2`, `OAuth2PasswordBearer`, `SecurityScopes` — `security.py`, full parity  
- `auto_error=False` on all security extractors — HAVE  
- Dual `AsyncExitStack` for `scope="function"` vs `scope="request"` yield-deps — `di.py`  
- App-level `dependencies=[Depends(...)]` prepended to every route — `app.py`  
- `APIRoute.get_route_handler()` + `route_class` kwarg on `App` + `APIRouter` — M2 push 4  
- Custom `APIRoute` subclass support (AutoCommitAPIRoute/PrefectAPIRoute patterns) — ASGI delegation works  
- `Annotated[T, Depends(fn)]` DI alias — HAVE  
- `is_body_allowed_for_status_code` (204/205/304/1xx no-body enforcement) — HAVE (M2)  
- `@app.exception_handler(ExcCls)` + `App.add_exception_handler` — HAVE  
- WS validation failure → close 1008 — HAVE  
- `CORS` via Starlette `CORSMiddleware` — HAVE  
- `GZipMiddleware` (Python path) + `lancelot.compress.GZipMiddleware` (Rust, GIL-off) — HAVE (M18)  
- `ORJSONResponse`/`UJSONResponse` pass-throughs — HAVE  
- `cancel_on_disconnect` dual-task (`_watch_disconnect` reads `receive` directly) — HAVE  
- Metric route-template naming (`_lancelot_route`, cardinality-safe) — HAVE  
- `request.app.state` via Starlette `State` passthrough — HAVE  
- `@app.on_event("startup"/"shutdown")` (deprecated, emits `FastAPIDeprecationWarning`) — HAVE  
- `stream_interval`, TTFT metric (`lancelot.http.stream.ttft`), error frame, done sentinel — HAVE  
- OpenAPI security definitions (nested Security, M19b nested-Security fix) — HAVE  
- Lifespan merging on `include_router` — HAVE (M15 fix)  
- Const-tier auth guardrail (`ConstRouteAuthBypassWarning` + `LANCELOT_CONST_STRICT`) — HAVE (M19)  
- `ProblemDetail` on streams, disconnect cancellation — HAVE  
- `x-accel-buffering: no` + `cache-control: no-cache` on SSE — HAVE (`asgi.py:1041-1042`)  
- `/livez`, `/readyz` (two-state: ok/draining), `/fleet/status` — HAVE  
- `StaticTokenGate` ASGI middleware (constant-time hmac, SHA-256 hashed tokens) — HAVE (`auth.py`)  
- `lancelot.net.assert_safe_url` SSRF guard, `TransportPool(block_private_ips=True)` — HAVE  
- `lancelot.secrets` (5 backends + SIGHUP rotation) — HAVE  
- `TransportPool.spawn_child`, `.fanout`, mTLS, `reload_mtls()` — HAVE  
- `lancelot.upstream_metrics` (6 OTel instruments, `set_rate_table()`) — HAVE  
- `lancelot.mcp.mount` (tools/list + tools/call) — HAVE  
- `lancelot.webhook` (Standard Webhooks sign/verify) — HAVE  
- `settings_provider(cls, eager=False)` singleton DI pattern — HAVE  
- `StaticFiles` with ETag, Range, off-loop read, traversal guard — HAVE  
- `UploadFile` SpooledTemporary + multipart DoS caps — HAVE  
- `TenantConcurrencyLimiter` + `RateLimit` (token bucket) — HAVE  
- W3C `traceparent` propagation (no OTel dep) — HAVE  
- `lancelot.memory_pool.MemoryPool` with named-consumer OOM — HAVE  
- Retry-After honoring + retry budget + `Idempotency-Key` on retries — HAVE (`llm.py`)  

---

## refuse

All items in this list would violate a cardinal rule or are on the Tier-D list:

- `separate_input_output_schemas` — conformance edge case; 99.6% without it; touching it risks the 11 yield-dep tests  
- `webhooks` field in OpenAPI spec — pure DX annotation, no byte path, no production project uses it in the corpus  
- `WSGIMiddleware` — burns GIL; anti-positioning; use ASGI-native middleware or put WSGI behind a proxy  
- `AsyncExitStackMiddleware` scope injection — the dual AsyncExitStack DI seam already covers the use case  
- `HTTPSRedirectMiddleware` — TLS is the proxy's job; adding it encourages running Lancelot without a proxy  
- `TrustedHostMiddleware` — minimal security improvement over existing `lancelot.net` SSRF guard; encourages relying on Host headers for security  
- Live route registration after startup — Rust matchit router is built at startup; runtime mutation would require a lock on the const-tier serving path  
- Per-chunk response compression (streaming gzip) — Tier-D; `GZipMiddleware` covers buffered bodies; per-chunk is a perf cliff  
- `WSGIMiddleware` / `a2wsgi` dep — Tier-D (burns GIL)  
- `ROBYN_BROWSER_OPEN` dev knob — pure ergonomics, not byte path  
- Dynamic `create_model` + route registration — fights matchit indexing and the const-tier prebake model  
- CorkDispatcher / adaptive learned batching — requires numpy, violates cardinal rule #3 (pydantic-only runtime dep)  
- Full Digest auth protocol (challenge-response) — complex, rare, no byte path; the `HTTPDigest` stub (I5) covers the import-compatibility case  
- `SINGLE_DEPLOYMENT_TRAFFIC_FAILURE_THRESHOLD` circuit breaker in TransportPool — the breaker belongs in the `app.llm` multi-deployment router layer, not TransportPool (single-provider by design)  
- LiteLLM-style provider request/response transformation — explicitly user-side per CLAUDE.md "AI logic stays user-side"  
- Spend flush DB (Redis incr → Postgres) — out of framework scope; `cost_usd` ContextVar + OTel histogram is the correct primitive  
- `deepcopy(field.default)` on missing optional — likely HAVE; verify at `di.py` before treating as a gap (not confirmed missing)  

---

## Coverage note

`04_serving/gateways_deploy.md` and `05_lancelot_state/resilience.md` were read in full as part of this synthesis session (they were unread at the time of context compaction). Their additive findings are incorporated: M7 `finish_reason:"error"` (I10), Azure `x-ms-retry-after-ms` (I14), three-state `/readyz` (I9). No other coverage gaps remain.
