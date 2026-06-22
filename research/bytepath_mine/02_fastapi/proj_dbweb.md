# DB-backed web-app archetype — byte-path mechanism note

**Source cluster:**
- `researchfms/fastapi/code/prefect/utilities/server.py` (PrefectAPIRoute + PrefectRouter)
- `researchfms/fastapi/code/prefect/api/server.py` (RequestLimitMiddleware, final-app memory, retryable-error 503, content-type compat shim)
- `researchfms/fastapi/code/prefect/api/dependencies.py` (EnforceMinimumAPIVersion, LimitBody)
- `researchfms/fastapi/code/polar/routing.py` + `kit/routing.py` (AutoCommitAPIRoute, 8-mixin composition, get_api_router_class)
- `researchfms/fastapi/code/polar/postgres.py` (AsyncSessionMiddleware, read/write session split)
- `researchfms/fastapi/code/polar/kit/repository/base.py` (RepositoryBase, stream_scalars+yield_per)
- `researchfms/fastapi/code/polar/worker/_enqueue.py` (JobQueueManager deferred-flush pattern)
- `researchfms/fastapi/code/dispatch/main.py` (schema_translate_map multi-tenant session middleware)
- `researchfms/fastapi/code/dispatch/database/service.py` (CommonParameters / search_filter_sort_paginate)
- `researchfms/fastapi/code/dispatch/auth/permissions.py` (BasePermission / PermissionsDependency)
- `researchfms/fastapi/code/bitcart/bootstrap.py` (with_db_rollback, patch_call)
- `researchfms/fastapi/code/bitcart/ioc_starlette.py` (HTTPContainerMiddleware + WebSocketContainerMiddleware)
- `researchfms/fastapi/code/bitcart/utils/routing.py` (create_crud_router)
- `researchfms/fastapi/code/full-stack-fastapi-template/api/deps.py` (Annotated dep aliases)
- `researchfms/teardowns/PREFECT.md`, `researchfms/teardowns/POLAR.md`, `researchfms/teardowns/FULL_STACK_FASTAPI_TEMPLATE.md`

**Date mined:** 2026-06-22

---

## 1. `PrefectAPIRoute` — response-scoped `AsyncExitStack` (pre-response teardown)

**File:line:** `prefect/utilities/server.py:30-56`

```python
class PrefectAPIRoute(APIRoute):
    def get_route_handler(self):
        default_handler = super().get_route_handler()
        async def handle_response_scoped_depends(request: Request) -> Response:
            async with AsyncExitStack() as stack:
                request.state.response_scoped_stack = stack
                response = await default_handler(request)
            return response
        return handle_response_scoped_depends
```

**Exact mechanism:** Overrides `APIRoute.get_route_handler()` (the exact extension point Lancelot exposes on `Route`). Wraps the default handler in a second `AsyncExitStack`. Parks the stack at `request.state.response_scoped_stack`. A yield-dep that registers on this stack tears down BEFORE the response bytes are returned to the caller — specifically before hyper flushes the body to the socket.

FastAPI's standard yield-dep stack (`request.scope["fastapi_astack"]`) tears down AFTER the response is serialized and sent. For a DB session this means the pool connection stays checked out during the entire response-body transmission. For a large payload or streaming response this can be hundreds of milliseconds. The response-scoped stack returns the connection before the body flushes.

**Byte-path relevance:** DB connection pool is returned earlier → lower peak connection usage under load → fewer "pool exhausted" failures at sustained concurrency. Particularly meaningful for large-payload routes (Lancelot's positioning: large-payload is the win shape). This is a real byte-path win: changes WHEN the resource moves through the system, not a micro-opt.

**Lancelot check hint:** `lancelot/routing.py:511-587` — `Route.get_route_handler()` already exists and is invoked when `route_class` is customized. The current `_handler` inside `get_route_handler` closes `fstack` (the function-scoped AsyncExitStack) before returning the result — that is a "function-scoped" close (per Lancelot's M2 yield-dep scope semantics, `scope="function"`). What's NOT present: a separate `request.state.response_scoped_stack` that the user/framework can register resources onto for "close before body flush" timing. Lancelot would expose this as a named attribute on `request.state` within the same `get_route_handler` wrapper pattern.

**Status:** NOT in Lancelot. Additive.

---

## 2. `AutoCommitAPIRoute` — route-level `session.commit()` so handlers never commit

**File:line:** `polar/kit/routing.py:15-44`

```python
class AutoCommitAPIRoute(APIRoute):
    def __init__(self, path, endpoint, **kwargs):
        endpoint = self.wrap_endpoint(endpoint)
        super().__init__(path, endpoint, **kwargs)
    def wrap_endpoint(self, endpoint):
        @functools.wraps(endpoint)
        async def wrapped_endpoint(*args, **kwargs):
            session: AsyncSession | None = None
            for arg in (args, *kwargs.values()):
                if isinstance(arg, AsyncSession):
                    session = arg; break
            response = await endpoint(*args, **kwargs)
            if session is not None:
                await session.commit()
            return response
        return wrapped_endpoint
```

**Exact mechanism:** Wraps the endpoint function (at `__init__` time, not at dispatch time) via `functools.wraps`. After the endpoint returns, scans all positional+keyword args for an `AsyncSession` instance and commits it. The scan is O(n_args) where n_args is always tiny (typically 2-4 DI-resolved args). Commits ONLY the write session (the type-check `isinstance(arg, AsyncSession)` excludes `AsyncReadSession` which is a different type alias). The endpoint can `return orm_object` — the auto-commit runs after and the ORM object's state is valid before serialization.

**Byte-path relevance:** Operator experience: removes an entire class of "forgot to commit" bugs. Every real Polar endpoint returns an ORM object directly, no `await session.commit()` in handler code. The route class is the transaction boundary. This is the cleanest pattern for the DB-backed web-app archetype and a significant DX win.

**Lancelot check hint:** `lancelot/routing.py` — `APIRoute = Route` alias is present. Lancelot's `Route.__init__` does not wrap the endpoint. A user subclassing `APIRoute` can add their own wrapping via `get_route_handler()`, but no built-in auto-commit behavior exists. To add: a `route_class` mixin or a documented pattern for `app = Lancelot(route_class=MyAutoCommitRoute)`.

**Status:** NOT in Lancelot. Additive (operator experience; not hot-path).

---

## 3. 8-mixin `APIRoute` composition via `get_api_router_class`

**File:line:** `polar/routing.py:14-27`, `polar/kit/routing.py:187-198`

```python
class APIRoute(AutoCommitAPIRoute, IncludedInSchemaAPIRoute,
               DocumentedAuthSubjectAPIRoute, SpeakeasyIgnoreAPIRoute,
               SpeakeasyNameOverrideAPIRoute, SpeakeasyGroupAPIRoute,
               SpeakeasyMCPAPIRoute, SpeakeasyPaginationAPIRoute): pass

APIRouter = get_api_router_class(APIRoute)   # returns an APIRouter subclass that locks route_class
```

```python
def get_api_router_class(route_class):
    class _CustomAPIRouter(_APIRouter):
        def __init__(self, *args, **kwargs):
            kwargs["route_class"] = route_class
            super().__init__(*args, **kwargs)
    return _CustomAPIRouter
```

**Exact mechanism:** Each behavioral concern is a separate single-responsibility `APIRoute` subclass that runs its logic in `__init__` (OpenAPI annotation classes) or wraps the endpoint (AutoCommit). The project's `APIRoute` is a pure MRO composition: no `__init__` override needed, Python's MRO calls each mixin's `__init__` in order. `get_api_router_class` returns an `APIRouter` subclass that forces `route_class` on every router construction — so `from polar.routing import APIRouter` means every route in the project automatically gets all 8 behaviors with no per-route annotation.

The Speakeasy mixins are pure OpenAPI mutation in `__init__`: they inspect `self.tags`, `self.methods`, `self.response_model` (already set by the parent `__init__`) and write to `self.openapi_extra`. Zero hot-path cost.

**Byte-path relevance:** Operator experience / SDK-generation. The `SpeakeasyPaginationAPIRoute` (`:113-149`) auto-injects `x-speakeasy-pagination` when `response_model` subclasses `ListResource` — SDK generates auto-paginating iterators from this. `SpeakeasyMCPAPIRoute` (`:152-178`) exposes routes as MCP tools based on `APITag.mcp`. Both are zero-hot-path. For Lancelot specifically: the pattern of using `get_api_router_class` to lock a custom route class is exactly how Lancelot users should wire `AutoCommitAPIRoute` or `PrefectAPIRoute` — a documented helper to produce a locked `APIRouter` subclass is the right surface.

**Lancelot check hint:** `lancelot/routing.py:596-617` — `APIRouter.__init__` already accepts `route_class=` kwarg. Lancelot does NOT have a `get_api_router_class` factory. A user must remember to pass `route_class=MyRoute` on every `APIRouter(...)` call. Providing `get_api_router_class` as a first-class utility would let users lock the class once. Low implementation cost, high DX value.

**Status:** NOT in Lancelot. Additive (operator experience helper).

---

## 4. `IncludedInSchemaAPIRoute` — tag-driven OpenAPI visibility control

**File:line:** `polar/kit/routing.py:47-62`

```python
class IncludedInSchemaAPIRoute(APIRoute):
    def __init__(self, path, endpoint, **kwargs):
        super().__init__(path, endpoint, **kwargs)
        if self.include_in_schema:
            if APITag.private in self.tags:
                self.include_in_schema = settings.is_development()
            elif APITag.public in self.tags:
                self.include_in_schema = True
            else:
                self.include_in_schema = False
```

**Exact mechanism:** After the parent `__init__` (which sets `self.tags` from kwargs), checks the tag set. Routes with `APITag.private` are visible only in development (`settings.is_development()` = env-flag). Routes without `APITag.public` default to `include_in_schema=False` (hidden from the public OpenAPI doc but still routing). This means the production OpenAPI schema only shows `public`-tagged routes — no accidental exposure of internal admin endpoints.

**Byte-path relevance:** Operator experience: production OpenAPI schema is clean (only public routes). Internal/admin routes exist and route but are invisible to SDK codegen and external consumers. Zero hot-path cost (all computed at route-registration time, single field on the route object).

**Lancelot check hint:** `lancelot/routing.py` — `Route.__init__` takes `include_in_schema` kwarg but does not apply tag-based defaulting. A user can pass `include_in_schema=False` per route but there's no convention/mixin for "public vs private" tag-based visibility. Adding as a standalone mixin matching Polar's pattern would be clean.

**Status:** NOT in Lancelot. Additive (operator experience).

---

## 5. `PrefectRouter.add_api_route` — `response_model` inferred from return annotation

**File:line:** `prefect/utilities/server.py:68-87`

```python
class PrefectRouter(APIRouter):
    def add_api_route(self, path, endpoint, **kwargs):
        if kwargs.get("status_code") == status.HTTP_204_NO_CONTENT:
            kwargs["response_class"] = Response
        if kwargs.get("response_model") is None:
            kwargs["response_model"] = get_type_hints(endpoint).get("return")
        return super().add_api_route(path, endpoint, **kwargs)
```

**Exact mechanism:** Overrides `add_api_route` (NOT `__init__` — this fires at `@router.get(...)` decoration time). Uses `get_type_hints(endpoint)["return"]` to derive `response_model` when not explicitly provided. `get_type_hints` resolves `from __future__ import annotations` (string annotations) as real types, unlike `endpoint.__annotations__`. For 204 routes, forces `response_class=Response` (bare Starlette Response, not JSONResponse) so the response body is empty — avoids the "204 with null JSON body" foot-gun.

Every Prefect handler can write `async def list_flows() -> list[Flow]:` with no `response_model=` kwarg and still get full response validation + correct OpenAPI schema.

**Byte-path relevance:** Operator experience: eliminates redundant `response_model=` in decorator kwargs when the return type annotation is the source of truth. The 204 fix is a correctness win (FastAPI emits `null` for 204s otherwise, which is technically wrong). Zero hot-path cost (fires at registration).

**Lancelot check hint:** `lancelot/routing.py:596ff` (`APIRouter`) — Lancelot's `APIRouter` does not have an `add_api_route` override that infers `response_model` from the return annotation. Lancelot's DI compiler (`di.py`) does inspect return types for serialization, but the `response_model` field on the Route isn't auto-populated from the annotation. A user can add it via a custom `APIRouter` subclass following this exact pattern.

**Status:** NOT in Lancelot as a built-in. Additive (operator experience; could be a subclassable router mixin).

---

## 6. `RequestLimitMiddleware` — raw-ASGI `anyio.CapacityLimiter` concurrency cap

**File:line:** `prefect/api/server.py:277-292`

```python
class RequestLimitMiddleware:
    def __init__(self, app, limit: float):
        self.app = app
        self._limiter = anyio.CapacityLimiter(limit)
    async def __call__(self, scope, receive, send):
        async with self._limiter:
            await self.app(scope, receive, send)
```

**Exact mechanism:** Raw ASGI middleware (3-arg `__call__`), NOT `BaseHTTPMiddleware`. The distinction is critical: `BaseHTTPMiddleware` buffers the response body (breaks streaming/SSE); raw ASGI is transparent to streaming. Uses `anyio.CapacityLimiter(100)` — installed only for SQLite dialect (`:989-994`), where concurrent writes cause SQLITE_BUSY. The Prefect comment is honest: "blunt tool for limiting SQLite concurrent writes."

**Byte-path relevance:** The RAW ASGI form is the correct pattern — it does not break streaming. Lancelot already has `App(max_concurrent_requests=N)` which is also a raw-ASGI semaphore cap at the ASGI seam. **Lancelot ALREADY HAS THIS.** What Lancelot may want to verify: the implementation is raw-ASGI (not BaseHTTPMiddleware) — confirmed in `lancelot/asgi.py:327` ("Tier-B.B4: global concurrent-request cap → 503 + Retry-After"). HAVE.

**Status:** HAVE (Lancelot's `max_concurrent_requests` + Retry-After 503). Prefect's version is less capable (no Retry-After header).

---

## 7. Retryable-error → 503 exception handler (`is_client_retryable_exception`)

**File:line:** `prefect/api/server.py:326-433`

```python
def is_client_retryable_exception(exc: Exception) -> bool:
    if isinstance(exc, sqlalchemy.exc.OperationalError) and isinstance(exc.orig, sqlite3.OperationalError):
        if getattr(exc.orig, "sqlite_errorname", None) in {"SQLITE_BUSY", "SQLITE_BUSY_SNAPSHOT"}:
            return True
    if isinstance(exc, (sqlalchemy.exc.DBAPIError, asyncpg.exceptions.QueryCanceledError,
                        asyncpg.exceptions.ConnectionDoesNotExistError,
                        asyncpg.exc.CannotConnectNowError, sqlalchemy.exc.InvalidRequestError,
                        sqlalchemy.orm.exc.DetachedInstanceError)):
        return True
    return False

async def custom_internal_exception_handler(request, exc):
    if is_client_retryable_exception(exc):
        return JSONResponse({"exception_message": "Service Unavailable"},
                            status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    ...
```

**Exact mechanism:** A taxonomy of DB exceptions that are client-retryable is encoded as a boolean function. DB contention errors (SQLITE_BUSY, asyncpg.QueryCanceled, asyncpg.ConnectionDoesNotExist) return 503 (client should retry) rather than 500 (permanent failure). This is surfaced via `app.add_exception_handler(Exception, custom_internal_exception_handler)`.

**Byte-path relevance:** Operator experience for DB-backed apps: propagates the "retry-able" signal correctly to clients and load-balancers. Not a transport-layer win but a resilience win for the DB layer. Lancelot's `upstream_metrics.py` already has a structured-error taxonomy (`quota/timeout/permanent/transient/upstream_5xx`) for LLM upstreams; extending the same taxonomy to DB-layer exceptions (if users add DB exception handlers) follows the same pattern.

**Lancelot check hint:** `lancelot/upstream_metrics.py:155-185` — structured error taxonomy for LLM upstreams. No equivalent for SQL/DB layer (Lancelot doesn't bundle a DB layer). This is a doc/cookbook pattern, not a built-in.

**Status:** NOT in Lancelot (Lancelot has no DB layer). Document as a cookbook pattern for `@app.exception_handler` users.

---

## 8. `final` flag — delete source router routes after `include_router` to reclaim ~50-55 MB

**File:line:** `prefect/api/server.py:494-512`

```python
for router in API_ROUTERS:
    api_app.include_router(router, dependencies=dependencies)
    if final:
        del router.routes   # FastAPI deep-copies routes + rebuilds Pydantic models on include_router;
                            # the source router holds a duplicate set that will never be used again.
if final:
    gc.collect()
```

**Exact mechanism:** `app.include_router(router)` deep-copies every route and rebuilds new Pydantic request-body models (because the same router could be included again with different dependencies). The source router retains a full duplicate set of Pydantic models forever. When this is the final app in the process (routers never re-used), `del router.routes` + `gc.collect()` drops ~50-55 MB (measured, cited in the Prefect comment at `:494-512`).

**Byte-path relevance:** Operator experience / memory: for large apps with many routes (Prefect has 30+ routers), this is a real memory saving at startup. Lancelot's `include_router` behavior (in `lancelot/routing.py`) does copy routes via `app.include_router`. Whether Lancelot holds duplicate Pydantic model trees the same way depends on implementation — worth investigating for large Lancelot apps.

**Lancelot check hint:** `lancelot/routing.py:655ff` (`include_router`) — check whether route registration duplicates the plan/validators objects. If so, exposing a `final=True` flag on `include_router` to drop the originals would reclaim memory for large apps.

**Status:** NOT in Lancelot. Additive for large-app operator experience (low priority vs core byte path).

---

## 9. `AsyncSessionMiddleware` + `get_db_session`/`get_db_read_session` — read/write session split

**File:line:** `polar/postgres.py:61-100`

```python
class AsyncSessionMiddleware:  # raw ASGI
    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        sessionmaker = scope["state"]["async_sessionmaker"]
        async with sessionmaker() as session:          # one write session per request
            scope["state"]["async_session"] = session
            await self.app(scope, receive, send)

async def get_db_session(request) -> AsyncGenerator[AsyncSession]:
    session = request.state.async_session
    try:    yield session
    except: await session.rollback(); raise
    else:   await session.commit()

async def get_db_read_session(request) -> AsyncGenerator[AsyncReadSession]:
    sessionmaker = request.state.async_read_sessionmaker
    async with sessionmaker() as session:   # fresh replica session per call
        yield session
```

**Exact mechanism:** The write session is opened ONCE per request by raw-ASGI middleware and shared via `scope["state"]`. All `Depends(get_db_session)` calls in the same request get the same session (one unit of work). The read-replica session is opened fresh on each call and never committed. The read vs write distinction is a type alias: `AsyncSession` vs `AsyncReadSession` (both `AsyncSession` under the hood but distinct Python types so `isinstance` in `AutoCommitAPIRoute` only commits the write session).

**Byte-path relevance:** Read replica offloads read-heavy endpoints to a separate PostgreSQL instance — real throughput win for read-heavy workloads (product listings, dashboards). Single-session-per-request = one DB connection from the pool per request on the write path = predictable pool usage. Lancelot doesn't bundle a DB layer but could document this exact pattern as the recommended integration shape for SQLAlchemy 2.0 + Lancelot.

**Lancelot check hint:** Lancelot has no DB layer. The pattern is a cookbook concern. The raw-ASGI middleware form (not BaseHTTPMiddleware) is the right shape for Lancelot compatibility — confirmed by Prefect/Polar both using raw ASGI for session middleware.

**Status:** NOT in Lancelot (out of scope — DB layer). Document as integration cookbook.

---

## 10. `RepositoryBase.stream` — `stream_scalars(yield_per=N)` for DB streaming into HTTP response

**File:line:** `polar/kit/repository/base.py:79-104`

```python
async def stream(self, statement) -> AsyncGenerator[M, None]:
    results = await self.session.stream_scalars(
        statement,
        execution_options={"yield_per": settings.DATABASE_STREAM_YIELD_PER},
    )
    try:
        async for result in results: yield result
    finally:
        await results.close()
```

**Exact mechanism:** `session.stream_scalars` opens a server-side cursor (PostgreSQL `CURSOR ... FETCH`). `yield_per=N` (Polar's default from `settings.DATABASE_STREAM_YIELD_PER`) controls how many rows are fetched per round-trip. The repository yields rows as an async generator, which the endpoint can feed directly into Lancelot's `@app.stream` or a `StreamingResponse`. The `finally: results.close()` ensures the cursor is always released.

**Byte-path relevance:** For large query results, server-side cursor avoids loading all rows into memory before serialization. Rows arrive as they stream from the DB, feeding into the HTTP response body without peak-memory spike. This is the DB-to-HTTP streaming pipe. Lancelot's R3 pump can accept any async generator — `RepositoryBase.stream()` feeds directly into `@app.stream`. The pattern shows how to close the upstream cursor on client disconnect: the R3 pump's cancellation (client disconnect → next `try_push` sees Closed → `_drain` coro returns, generator left suspended) naturally stops pulling from the cursor.

**Lancelot check hint:** Lancelot's `@app.stream` accepts `AsyncGenerator`. Verify: when the client disconnects mid-stream, does the `_drain` coroutine's exit cause the SQLAlchemy cursor to be closed? It depends on whether the generator's `aclose()` is called. Lancelot's R3 pump leaves the generator suspended on disconnect (per CLAUDE.md: "gen left suspended ≤1 extra chunk"). SQLAlchemy cursors hold a DB connection open until closed — if the generator is left suspended after client disconnect, the server-side cursor leaks until GC. Document the `finally: results.close()` pattern as mandatory for DB-streaming generators.

**Status:** NOT a built-in (Lancelot has no DB layer) but an important cursor-leak hazard to document for `@app.stream` users who feed SQLAlchemy streaming generators.

---

## 11. `JobQueueManager` — defer+batch-flush dramatiq enqueue to after HTTP commit

**File:line:** `polar/worker/_enqueue.py:34-182`

**Exact mechanism:** A `ContextVar`-backed `JobQueueManager` accumulates job-enqueue calls during a request in a plain list (`_enqueued_jobs`). The middleware `FlushEnqueuedWorkerJobsMiddleware` opens the manager at request start (`async with JobQueueManager.open(broker, redis)`). The `open()` context manager flushes on clean exit (after the response) — specifically after `AutoCommitAPIRoute` commits the DB write session. The flush batches all messages into Redis using `HSET` + `RPUSH` in batches of 50 (`FLUSH_BATCH_SIZE = 50`), with correlation-id propagation and debounce-key support.

**Byte-path relevance:** Critical ordering guarantee: jobs are enqueued to Redis AFTER the DB transaction commits. This prevents race conditions where the worker picks up the job before the data it depends on is visible in the DB. The batched Redis write (`HSET mapping={...}` for all messages at once, then `RPUSH queue, *all_ids`) reduces the number of round-trips to Redis for bursty enqueue patterns. The `ContextVar` isolation means concurrent requests don't see each other's queued jobs.

**Lancelot check hint:** Lancelot doesn't bundle a task queue. But the `ContextVar`-backed deferred-flush pattern for "side effects that must happen after the response write-path" is structurally identical to how Lancelot accumulates `cost_usd`/`tokens_in` in `lancelot/context.py` ContextVars and reads them in the ASGI seam `finally` block. The ordering principle (side effects after commit, not during handler) is a documented pattern for Lancelot users integrating task queues.

**Status:** NOT in Lancelot (task queue is user-side). Cookbook/documentation concern.

---

## 12. `EnforceMinimumAPIVersion` — callable-class API-version gate as a global dependency

**File:line:** `prefect/api/dependencies.py:43-102`

```python
class EnforceMinimumAPIVersion:
    def __init__(self, minimum_api_version: str, logger):
        versions = [int(v) for v in minimum_api_version.split(".")]
        self.api_major, self.api_minor, self.api_patch = versions
        self.logger = logger
    async def __call__(self, x_prefect_api_version: str = Header(None)) -> None:
        if not x_prefect_api_version: return
        major, minor, patch = [int(v) for v in x_prefect_api_version.split(".")]
        if (major, minor, patch) < (self.api_major, self.api_minor, self.api_patch):
            raise HTTPException(status_code=400, detail=f"Server requires >= {self.minimum_api_version}")
```

Instantiated once module-level: `enforce_minimum_version = EnforceMinimumAPIVersion("0.8.0", logger)`. Applied as a global dependency to every router: `api_app.include_router(router, dependencies=[Depends(enforce_minimum_version)])`.

**Exact mechanism:** Callable class (not a plain function) so it carries the minimum-version constant and logger as instance state — the dependency is instantiated once, not per-request. The `Header(None)` default makes the header optional (clients without the header are assumed to be the latest version). The 400 (not 403 or 426) is deliberate: an outdated client is a bad request, not an auth failure.

**Byte-path relevance:** Operator experience: version gates prevent protocol mismatches from silent data corruption. Using `include_router(router, dependencies=[...])` to apply it globally costs one header-check per request — negligible overhead, zero hot-path impact on the byte path.

**Lancelot check hint:** Lancelot's `App(dependencies=[...])` (M2 "app-level deps prepended to every route") can host this pattern directly. No built-in version-gate dependency exists. A user can build it exactly as Prefect did. Document as a cookbook pattern for API versioning.

**Status:** NOT in Lancelot as built-in. Operator experience cookbook.

---

## 13. `LimitBody` — settings-defaulted, clamped body `limit` param factory

**File:line:** `prefect/api/dependencies.py:105-131`

```python
def LimitBody() -> Any:
    def get_limit(limit: int = Body(None, description="...")):
        default_limit = PREFECT_API_DEFAULT_LIMIT.value()
        limit = limit if limit is not None else default_limit
        if not limit >= 0:
            raise HTTPException(422, detail="Invalid limit: must be >= 0.")
        if limit > default_limit:
            raise HTTPException(422, detail=f"must be <= {default_limit}.")
        return limit
    return Depends(get_limit)
```

**Exact mechanism:** A factory (function returning `Depends(...)`) rather than a fixed dependency. The default is read from a settings object at call time (not at module import time), so the default can be changed via environment variables without restarting. Both floor (0) and ceiling (default_limit) are enforced — the ceiling prevents a client from requesting an unbounded result set.

**Byte-path relevance:** Prevents unbounded DB queries / large response payloads from single requests. The ceiling is the transport-protection mechanism. Lancelot already has body-size caps at the transport layer (`LANCELOT_MAX_BODY_BYTES`); this is the application-layer complement (caps the number of DB rows returned). Not a Lancelot built-in but a good cookbook pattern for pagination endpoints.

**Status:** NOT in Lancelot as built-in. Cookbook for pagination.

---

## 14. Dispatch `db_session_middleware` — `schema_translate_map` multi-tenant DB session

**File:line:** `dispatch/main.py:112-189`

```python
@api.middleware("http")
async def db_session_middleware(request, call_next):
    organization_slug = path_params.get("organization", "default")
    schema = f"dispatch_organization_{organization_slug}"
    schema_engine = engine.execution_options(
        schema_translate_map={None: schema}
    )
    session = scoped_session(sessionmaker(bind=schema_engine), scopefunc=get_request_id)
    request.state.db = session()
    response = await call_next(request)
    if request.state.db.is_active:
        request.state.db.commit()
    return response
```

**Exact mechanism:** SQLAlchemy `execution_options(schema_translate_map={None: schema})` rewrites every unqualified table reference in SQL to the tenant-specific schema (`dispatch_organization_{slug}`). The session is created per-request, scoped to a ContextVar-backed request-id (`scoped_session(scopefunc=get_request_id)`). The organization slug is extracted by route-regex matching against the path BEFORE the handler runs. Commit-on-success / rollback-on-error is handled in the middleware.

**Byte-path relevance:** Multi-tenant pattern: each tenant gets a separate PostgreSQL schema (schema-per-tenant, not row-level). The `schema_translate_map` is the SQLAlchemy mechanism that makes this transparent to every query in the system. Relevant for Lancelot users building multi-tenant SaaS — pairs naturally with Lancelot's `TenantConcurrencyLimiter` and `X-Tenant-Id` header. The pattern is DB-layer not transport-layer but operator experience for multi-tenant deployment.

**Anti-pattern noted:** `get_path_params_from_request` (`:87-95`) iterates ALL routes and recompiles each route's regex on every request to extract the organization slug from the path. This is O(n_routes) per request with regex recompilation. Lancelot's `matchit`-based router already does path matching in O(log n) — for multi-tenant slug extraction, the right approach is to let the router match the route first and then read `request.path_params["organization"]` from the already-matched result.

**Lancelot check hint:** Lancelot's `context.py` already has `tenant_id` ContextVar. The `schema_translate_map` pattern is DB-layer. No action needed on Lancelot's transport layer for this. Document as a cookbook for SQLAlchemy + Lancelot multi-tenant apps.

**Status:** NOT in Lancelot (DB layer). The anti-pattern (per-request route-regex recompile) is a Dispatch-specific bug, not a pattern to copy.

---

## 15. `BasePermission` + `PermissionsDependency` — composable class-based RBAC as DI

**File:line:** `dispatch/auth/permissions.py:35-134`

```python
class BasePermission(ABC):
    @abstractmethod
    def has_required_permissions(self, request: Request) -> bool: ...
    def __init__(self, request: Request):  # raises HTTPException on failure
        ...
        if not self.has_required_permissions(request):
            raise HTTPException(status_code=403, ...)

class PermissionsDependency:
    def __init__(self, permissions_classes: list): ...
    def __call__(self, request: Request):
        for p in self.permissions_classes: p(request=request)

# usage:
@router.get("/{id}", dependencies=[Depends(PermissionsDependency([IncidentViewPermission]))])
```

**Exact mechanism:** Each permission is a class whose `__init__` runs the check and raises on failure (no return value — exceptions are the success/fail signal). `PermissionsDependency` is a callable class that iterates a list of permission classes and instantiates each. Composition of permissions is via `any_permission([ClassA, ClassB], request)` — a standalone function that tries each class and returns True if any passes without exception. The role ladder is `Owner ⊃ Manager ⊃ Admin ⊃ Member` — each higher role's `has_required_permissions` calls `any_permission` with the lower roles first.

**Byte-path relevance:** Operator experience: permission logic is composable, testable, and not duplicated per route. Zero hot-path cost (runs at DI resolution time, same as any other dependency). FastAPI's `Security(dependency, scopes=[...])` is the alternative but Dispatch's class-based approach allows object-level permission checks (e.g. `IncidentViewPermission` fetches the incident from DB and checks visibility). Security scopes are string-matching; class-based permissions can do arbitrary DB lookups.

**Lancelot check hint:** Lancelot's `security.py` + `auth.py` handle OAuth2 bearer + `SecurityScopes`. Class-based RBAC over the `PermissionsDependency` pattern works out-of-the-box with Lancelot's DI. No built-in needed.

**Status:** NOT in Lancelot as built-in (user-side concern). Pattern works with existing Lancelot DI.

---

## 16. Bitcart `with_db_rollback` — decorator ensuring session rollback before exception-handler response

**File:line:** `bitcart/bootstrap.py:85-100`

```python
def with_db_rollback(handler):
    async def wrapper(request, exc):
        session = await request.state.dishka_container.get(AsyncSession)
        await session.rollback()
        return await handler(request, exc)
    return wrapper

@with_db_rollback
async def db_exception_handler(request, exc: AdvancedAlchemyError): ...
```

**Exact mechanism:** Exception handlers registered via `app.add_exception_handler` run AFTER the normal handler raises. The DI container (dishka) would otherwise commit the session when it closes (on normal exit from the request-scope context). If the exception handler returns a response without rolling back first, dishka commits a transaction that may be in a broken state (`PendingRollbackError`). The decorator explicitly rollbacks before returning the error response.

**Byte-path relevance:** Correctness: prevents "commit on exception path" — a subtle DI lifecycle bug. Lancelot's ASGI seam (`asgi.py`) handles `RequestValidationError` and calls rollback if there's a yield-dep stack. But for user-defined `@app.exception_handler` that catch domain exceptions while a DB session is in scope, the same rollback-before-response issue applies. Lancelot should document: if a user's exception handler runs with a transaction in scope, rollback before returning.

**Lancelot check hint:** `lancelot/asgi.py` — check the path where `@app.exception_handler` fires: does Lancelot call `_fstack.aclose()` (which tears down the yield-dep that holds the DB session) before or after the exception handler's response is built? If after, and the user's exception handler accesses the session, it may see a stale/broken state.

**Status:** NOT in Lancelot as built-in. Document as a cookbook hazard for `@app.exception_handler` users with session-per-request DI.

---

## 17. `patch_call` root_path monkeypatch — ASGI scope path correction for deployed sub-path

**File:line:** `bitcart/bootstrap.py:61-81`

```python
def patch_call(instance: FastAPI) -> None:
    class _(type(instance)):
        async def __call__(self, scope, receive, send):
            if self.root_path:
                scope["root_path"] = self.root_path
                if not scope.get("path", "").startswith(self.root_path):
                    scope["path"] = self.root_path + scope.get("path", "")
                raw_path = scope.get("raw_path")
                if raw_path and not raw_path.startswith(self.root_path.encode()):
                    scope["raw_path"] = self.root_path.encode() + raw_path
            await Starlette.__call__(self, scope, receive, send)
    instance.__class__ = _
```

**Exact mechanism:** When FastAPI is deployed behind a reverse proxy at a sub-path (e.g. `/api`), the ASGI server may not always prepend `root_path` to `scope["path"]`. This monkeypatches the `__call__` to normalize `scope["path"]` and `scope["raw_path"]` to always start with `root_path`. The comment cites `fastapi/fastapi#11160` as the upstream fix (not yet merged at time of writing).

**Byte-path relevance:** Operator experience for sub-path deployments. Lancelot's `run()` accepts `root_path` from env (`LANCELOT_ROOT_PATH` or `ROOT_PATH`). Verify that Lancelot's ASGI seam handles the same `scope["path"]` normalization when `root_path` is set — if the upstream server doesn't set it, requests route to the wrong handler.

**Lancelot check hint:** `lancelot/asgi.py` — search for `root_path` handling. If Lancelot doesn't normalize `scope["path"]` when `root_path` is set, sub-path deployments would silently 404.

**Status:** Worth checking in Lancelot. Not confirmed as a current gap.

---

## 18. `Annotated` DI aliases — `SessionDep`, `CurrentUser`, `TokenDep`

**File:line:** `full-stack-fastapi-template/api/deps.py:26,27,49`

```python
SessionDep = Annotated[Session, Depends(get_db)]
TokenDep   = Annotated[str, Depends(oauth2_scheme)]
CurrentUser = Annotated[User, Depends(get_current_user)]
```

**Exact mechanism:** Defines `Annotated[T, Depends(fn)]` aliases once in the deps module. Every route handler imports the alias and uses it as a type annotation, eliminating `= Depends(get_db)` boilerplate in every signature. The alias is the source of truth for the dependency chain.

**Byte-path relevance:** Zero hot-path impact. This is the idiomatic FastAPI pattern for DI aliases (the official FastAPI docs were rewritten around it). Lancelot supports `Annotated[T, Depends(fn)]` per the conformance suite (M2). The pattern works out of the box.

**Lancelot check hint:** `lancelot/di.py` — verify `Annotated[T, Depends(fn)]` is recognized as a dependency alias. Conformance tests in `conformance/vendor/fastapi/tests/test_annotated.py` or similar should cover this.

**Status:** HAVE (conformance 99.6%, Annotated DI aliases work).

---

## Summary: Lancelot gap / action map

| # | Mechanism | Status | Action |
|---|---|---|---|
| 1 | PrefectAPIRoute response-scoped AsyncExitStack | NOT HAVE | Consider: expose `request.state.response_scoped_stack` in `get_route_handler` wrapper — lets users close DB connections before body flush |
| 2 | AutoCommitAPIRoute — route-level `session.commit()` | NOT HAVE | Document as a `route_class` mixin pattern; no built-in needed |
| 3 | `get_api_router_class` — factory to produce locked APIRouter subclass | NOT HAVE | Add as a utility function alongside `APIRouter`; 10-line impl |
| 4 | Tag-driven `include_in_schema` (public/private tags) | NOT HAVE | Document as a route-class mixin |
| 5 | `response_model` inferred from return annotation in `add_api_route` | NOT HAVE | Document as a custom `APIRouter` subclass pattern |
| 6 | Raw-ASGI concurrency cap (`RequestLimitMiddleware`) | HAVE | Lancelot's `max_concurrent_requests` + 503 Retry-After is superior |
| 7 | Retryable-error 503 exception handler | NOT HAVE | Cookbook for DB users; not Lancelot built-in |
| 8 | `final` flag to delete source router routes post-include | NOT HAVE | Low-priority; only matters for 30+ router apps |
| 9 | Read/write session split (AsyncSessionMiddleware + two session deps) | NOT HAVE | Cookbook for SQLAlchemy integration |
| 10 | `RepositoryBase.stream` + cursor leak on client disconnect | NOT HAVE | Document cursor-leak hazard for `@app.stream` users |
| 11 | `JobQueueManager` deferred-flush pattern | NOT HAVE | Cookbook; matches Lancelot's ContextVar pattern |
| 12 | `EnforceMinimumAPIVersion` global dep | NOT HAVE | Cookbook for API versioning |
| 13 | `LimitBody` settings-defaulted clamped body param | NOT HAVE | Cookbook for pagination |
| 14 | `schema_translate_map` multi-tenant session | NOT HAVE | Cookbook for multi-tenant SQLAlchemy |
| 15 | `BasePermission` + `PermissionsDependency` RBAC | NOT HAVE | Works out-of-box with Lancelot DI; user-side |
| 16 | `with_db_rollback` on exception handlers | NOT HAVE | Document hazard for `@app.exception_handler` users |
| 17 | `patch_call` root_path scope normalization | CHECK | Verify Lancelot's `asgi.py` handles `root_path` path normalization |
| 18 | `Annotated` DI aliases | HAVE | Works; conformance-verified |

**Top additive candidates for Lancelot operator experience:**
1. `get_api_router_class` utility (mechanism #3) — ~10 lines, high DX value, lets users lock a custom route class across an entire module hierarchy
2. Document cursor-leak hazard for `@app.stream` + SQLAlchemy server-side cursors (mechanism #10) — real bug risk
3. Verify/document `root_path` ASGI scope handling (mechanism #17) — potential silent 404 in production sub-path deployments
