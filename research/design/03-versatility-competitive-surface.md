# Lancelot — Versatility & Competitive Feature Surface

> Decision-grade design research. Goal: define the **minimum versatile feature surface** Lancelot needs to be a credible FastAPI/Robyn migration target, and isolate the **defensible differentiation gap** that only Lancelot's architecture (Rust core + compile-once DI + native streaming + AI layer) can occupy.
>
> Sources: live 2026 web research (URLs inline) + the nine source-level teardowns in `research/{fastapi,robyn,blazeio}/` and `research/SYNTHESIS.md` (cited `path:line`). Labels: **KNOWN** (sourced from code/docs/teardown), **INFERRED** (reasoned from sourced facts), **RECOMMENDATION** (Lancelot design call).
>
> Date: 2026-05-18. Framework versions: FastAPI ~0.136, Robyn 0.85, Litestar 2.x/3-dev, Granian 2.7.3, Starlette (Kludex fork), Sanic 24/25, BlackSheep 2.4.5, Django-Ninja 1.x.

---

## Executive Summary (read this first)

**Positioning (the one paragraph):** Lancelot is *the only Python framework where the entire request lifecycle is Rust and the AI streaming path is a first-class native primitive* — Robyn's Rust-core/GIL model with FastAPI's compile-once DI/validation/OpenAPI ergonomics and Blazeio's allocation/backpressure discipline, with the one workload all of them get wrong — **long-lived LLM token streaming with end-to-end backpressure, mid-stream dependency lifetime, client-disconnect → upstream-abort, and an MCP/AI layer that is part of the framework, not a userland bolt-on** — built correctly from day one. FastAPI owns ergonomics but pays a per-request Python tax; Litestar owns the modern-FastAPI ergonomics race (msgspec, plugins, bundled middleware) but is still pure-Python on an ASGI server it doesn't own; Robyn owns the Rust core but marshals eagerly and streams catastrophically; Granian owns the server but is *not a framework*. Lancelot is the union of the four winners minus each one's fatal flaw, and the differentiation is the AI-backend hot path, not generic CRUD throughput.

**v0 table-stakes (a startup migrating from FastAPI will not adopt without these):** typed path/query/body params; Pydantic v2 request/response validation; automatic OpenAPI 3.1 + Swagger/Scalar UI; `Depends()`-style DI with yield-teardown; ASGI-compatible middleware; lifespan startup/shutdown; CORS; sync **and** async handlers (sync offloaded, bounded); JSON + streaming (SSE/JSONL) responses; a `TestClient`; WebSockets; static files; the FastAPI 422 error wire-shape. Without this exact set, "migrate from FastAPI" is a rewrite, and nobody rewrites for speed.

**The differentiator (what nobody else can do):** an **AI-native response/transport tier** — typed `SSEResponse`/`ChunkedResponse` with a Rust SSE/JSONL codec, bounded SPSC ring, timer-wheel keepalive, per-token zero-allocation backpressure, mid-stream DI lifetime (DB/LLM client held open *through* the stream, PEP-789-correct), automatic client-disconnect cancellation that aborts the upstream provider call, plus a built-in MCP JSON-RPC server and a proper pooled async LLM client (HTTP/2, no POST retry, circuit breaker). This is the section to over-invest in; the rest is parity.

---

## 1. Competitive Feature Matrix

Legend: **● strong** (first-class, idiomatic, documented) · **◐ partial** (exists but limited / plugin / awkward) · **○ absent** (not provided; userland or N/A). Server-only tools (Granian/Socketify) are scored on what they provide at the *server* layer; framework dimensions are marked ▦ (N/A — it's not a framework).

Frameworks: **FA** FastAPI · **RB** Robyn · **LS** Litestar · **ST** Starlette · **QT** Quart · **SA** Sanic · **BS** BlackSheep · **FL** Falcon · **EM** Emmett · **GR** Granian (server) · **SK** Socketify (server/framework hybrid) · **DN** Django-Ninja.

### 1.1 Routing

| Capability | FA | RB | LS | ST | QT | SA | BS | FL | EM | GR | SK | DN |
|---|--|--|--|--|--|--|--|--|--|--|--|--|
| Path params (typed) | ● | ◐ | ● | ◐ | ◐ | ● | ● | ◐ | ● | ▦ | ◐ | ● |
| Query params (typed/validated) | ● | ◐ | ● | ○ | ○ | ◐ | ● | ○ | ◐ | ▦ | ○ | ● |
| Typed body binding | ● | ◐ | ● | ○ | ○ | ○ | ● | ○ | ◐ | ▦ | ○ | ● |
| Route groups / routers | ● `APIRouter` | ◐ subrouter | ● Router/Controller | ● Mount | ● Blueprint | ● Blueprint | ● Router | ◐ | ● | ▦ | ○ | ● Router |
| Class-based controllers | ○ | ○ | ● | ○ | ◐ MethodView | ◐ HTTPMethodView | ● | ● Resource | ○ | ▦ | ○ | ◐ |
| API versioning | ◐ prefix-only | ◐ prefix | ◐ prefix | ◐ | ◐ | ◐ | ◐ | ○ | ◐ | ▦ | ○ | ◐ |
| Radix/trie match (perf) | ○ O(N) regex | ● matchit trie | ◐ | ○ | ○ | ● router | ● regex-trie | ● compiled | ◐ | ● hyper | ● | ○ (Django resolver) |
| Const/static-response tier | ○ | ● `const=True` | ○ | ○ | ○ | ○ | ○ | ○ | ○ | ◐ static files | ○ | ○ |

**KNOWN.** FastAPI routing is O(N) per-route regex `.match()` with a *second* full scan on 404 when `redirect_slashes` (`research/fastapi/01-routing-and-request-lifecycle.md:247`); the SYNTHESIS marks this AVOID (`research/SYNTHESIS.md:30,86`). Robyn uses a per-method `matchit 0.7.3` radix trie plus a unique two-tier **const router** (exact `HashMap` + trie) that pre-renders responses at registration and serves them with zero GIL/zero Python (`research/robyn/02-rust-routing-types-websockets.md:13,82`) — the single best idea in the corpus (`research/SYNTHESIS.md:31`). Litestar offers both function handlers and class-based `Controller`s with layered routers ([github.com/litestar-org/litestar](https://github.com/litestar-org/litestar)). Falcon and Sanic compile routers but neither validates types. Django-Ninja inherits Django's URL resolver (no trie) but adds FastAPI-style typed params via Pydantic ([django-ninja.dev](https://django-ninja.dev/)). Granian has no routing — hyper dispatches to the ASGI/RSGI app ([github.com/emmett-framework/granian](https://github.com/emmett-framework/granian)).

### 1.2 Dependency Injection

| Capability | FA | RB | LS | ST | QT | SA | BS | FL | EM | DN |
|---|--|--|--|--|--|--|--|--|--|--|--|
| DI system | ● `Depends()` | ◐ param-resolver | ● layered DI | ○ | ○ | ◐ `add_dependency` | ● typed services | ○ | ◐ | ◐ |
| Nested deps (3+ levels) | ● | ○ | ● | ○ | ○ | ◐ | ● | ○ | ○ | ◐ |
| Sync **and** async deps | ● | ◐ | ● | ○ | ○ | ◐ | ● | ○ | ○ | ● |
| `yield` deps + teardown | ● exit-stack | ○ | ● cleanup gen | ○ | ○ | ○ | ◐ | ○ | ○ | ◐ |
| Compile-once DI DAG | ● (the moat) | ◐ FunctionInfo | ◐ resolved/req | ○ | ○ | ○ | ◐ | ○ | ○ | ○ |
| App/router/route scoping | ● | ○ | ● 4 layers | ○ | ○ | ◐ | ● | ○ | ○ | ◐ |

**KNOWN.** FastAPI's two-phase DI compiler — reflect signature → immutable `Dependant` DAG once at `APIRoute.__init__`, recursive per-request solve — is *the* ergonomics moat (`research/fastapi/02-dependency-injection.md`; `research/SYNTHESIS.md:33,57`). It is also the chief residual tax: `solve_dependencies` re-runs Pydantic per request even for trivial `int`/`str`, `deepcopy(default)` per missing optional, and a full `get_dependant()` rebuild per request when dependency overrides are active (`research/fastapi/01-routing-and-request-lifecycle.md:136`; `research/SYNTHESIS.md:91`). Robyn has **no `Depends()`** — a 3-phase type-identity resolver (type-annotation match → reserved-name → path/query coercion), reflection snapshotted once at registration into `FunctionInfo` (`research/robyn/03-python-api-pyo3-bridge.md:16,79`). Litestar's DI is pytest-inspired, async-native, layered at app/router/controller/handler, with cleanup generators — widely cited as cleaner and more strongly typed than FastAPI's ([betterstack.com/community/guides/scaling-python/litestar-vs-fastapi](https://betterstack.com/community/guides/scaling-python/litestar-vs-fastapi/)). BlackSheep ships a real IoC container (`rodi`) bound to handlers and middleware ([neoteroi.dev/blacksheep](https://www.neoteroi.dev/blacksheep/)). Starlette, Quart, Falcon, Emmett have no DI — userland.

### 1.3 Validation / Serialization

| Capability | FA | RB | LS | ST | QT | SA | BS | FL | EM | DN |
|---|--|--|--|--|--|--|--|--|--|--|--|
| Request validation | ● Pydantic v2 | ◐ opt-in Pydantic | ● msgspec/Pydantic/attrs | ○ | ○ | ◐ | ● | ○ | ◐ | ● Pydantic |
| Response validation/model | ● | ○ | ● DTOs | ○ | ○ | ○ | ● | ○ | ◐ | ● |
| Serialization backend | pydantic-core (Rust) | orjson (Rust call) | **msgspec** | json | json | json (ujson opt) | orjson opt | json | json | pydantic-core |
| Single-pass JSON→model | ◐ double-parse | ● `validate_json` | ● msgspec decode | ○ | ○ | ○ | ◐ | ○ | ○ | ◐ |
| Multiple model libs | ○ Pydantic only | ◐ Pydantic | ● dataclass/TypedDict/attrs/Pydantic/msgspec | ○ | ○ | ○ | ◐ | ○ | ○ | ◐ |
| DTO layer (in/out shaping) | ◐ `response_model` | ○ | ● `DTOFactory` | ○ | ○ | ○ | ◐ | ○ | ○ | ◐ schema |

**KNOWN.** FastAPI's hot serialization is `dump_json()` (one pydantic-core Rust pass) on typed routes, **but** `jsonable_encoder` (recursive Python isinstance walk, 2–3× memory, two passes) is still on untyped/SSE/OpenAPI paths — marked AVOID (`research/fastapi/03-validation-serialization-openapi.md:448`; `research/SYNTHESIS.md:35,65,89`). Its input path is a double-parse: `request.json()`→`json.loads`→`validate_python`. Robyn's opt-in `model_validate_json` is single-pass (`research/robyn/03-python-api-pyo3-bridge.md:16`; `research/SYNTHESIS.md:34,66`) — a free Lancelot win. Litestar defaults to **msgspec** (claimed ~6× faster simple / ~24× nested vs Pydantic v1-era, ~2× framework-level vs FastAPI on synthetic JSON serialization) and accepts dataclass/TypedDict/attrs/Pydantic/msgspec interchangeably ([byteiota.com/litestar-vs-fastapi-python-speed-test-2026-analysis](https://byteiota.com/litestar-vs-fastapi-python-speed-test-2026-analysis/); [docs.litestar.dev/2/benchmarks.html](https://docs.litestar.dev/2/benchmarks.html)). Reality check from the same sources: under DB-paginated production workloads the framework gap collapses to network/DB variance — "Litestar wins synthetic charts but FastAPI wins almost every real production decision."

### 1.4 API Docs (OpenAPI / Swagger / Scalar / ReDoc)

| Capability | FA | RB | LS | ST | QT | SA | BS | FL | EM | DN |
|---|--|--|--|--|--|--|--|--|--|--|--|
| Auto OpenAPI from types | ● 3.1 | ◐ manual-ish | ● 3.1 | ○ | ○ | ◐ ext | ● | ○ | ○ | ● |
| Swagger UI | ● | ● | ● | ○ | ○ | ◐ | ● | ○ | ○ | ● |
| ReDoc | ● | ◐ | ● | ○ | ○ | ◐ | ● | ○ | ○ | ● |
| Scalar / RapiDoc / Stoplight | ◐ Scalar | ○ | ● 5 UIs | ○ | ○ | ○ | ◐ | ○ | ○ | ◐ |
| `$ref` dedup / in-out split | ● | ○ | ● | ○ | ○ | ○ | ● | ○ | ○ | ◐ |

**KNOWN.** OpenAPI is a *free byproduct* of FastAPI's type compile — `$ref` dedup, input/output model split, built once at boot from the same registry (`research/fastapi/03-validation-serialization-openapi.md` §5-6; `research/SYNTHESIS.md:44`). This is consistently cited as the #1 FastAPI adoption driver (see §2). Litestar generates OpenAPI 3.1 with optional example generation (polyfactory) and ships **five** doc UIs — Swagger, ReDoc, RapiDoc, Scalar, Stoplight ([github.com/litestar-org/litestar](https://github.com/litestar-org/litestar)). Robyn has built-in `/openapi.json` + `/docs` but the schema is largely hand-assembled (`research/robyn/03-python-api-pyo3-bridge.md:18`). Django-Ninja auto-generates Swagger/ReDoc from type hints ([django-ninja.dev/guides/api-docs](https://django-ninja.dev/guides/api-docs/)). Starlette/Quart/Falcon/Emmett: no auto OpenAPI (external `apispec`/`spectree` only).

### 1.5 Middleware / Lifespan / Background Tasks

| Capability | FA | RB | LS | ST | QT | SA | BS | FL | EM | DN |
|---|--|--|--|--|--|--|--|--|--|--|--|
| ASGI/HTTP middleware | ● | ◐ before/after | ● layered | ● | ● | ● | ● | ● WSGI | ◐ | ● Django |
| `call_next` dispatch | ● (trap) | ○ | ○ ASGI-pure | ● `BaseHTTPMiddleware` | ● | ◐ | ○ | ○ | ○ | ○ |
| Pure-ASGI middleware | ● | ○ Rust | ● | ● | ● | ◐ | ● | ▦ | ○ | ○ |
| Lifespan startup/shutdown | ● merged CM | ◐ event handlers | ● lifespan + hooks | ● | ● | ● listeners | ● | ○ | ◐ | ◐ Django apps |
| Background tasks | ◐ sequential post-send | ○ | ● + cleanup | ◐ `BackgroundTask` | ◐ | ● `add_task` | ● | ○ | ◐ | ◐ |
| Durable jobs / queue | ○ | ○ | ◐ via plugin | ○ | ○ | ○ | ○ | ○ | ◐ scheduler | ○ |

**KNOWN.** FastAPI's `@app.middleware("http")` is a trap: `BaseHTTPMiddleware` spins an `anyio` task group + 2 memory streams + a `_CachedRequest` *per request* (`research/fastapi/01-routing-and-request-lifecycle.md:13`; `research/SYNTHESIS.md:88`). Its `BackgroundTasks` run **sequentially inside the request coroutine after body send**, blocking connection release — "return fast, work later" is silently broken (`research/SYNTHESIS.md:90`), confirmed by the wider ecosystem: no persistence, no retry, lost on restart, blocks the loop ([medium.com/@garg10may/fastapi-background-tasks-arent-really-background-tasks](https://medium.com/@garg10may/fastapi-background-tasks-arent-really-background-tasks-6f2cfbf39e0c); [github.com/fastapi/fastapi/issues/1313](https://github.com/fastapi/fastapi/issues/1313)). Lifespan is a merged async-CM chain; `include_router` deep-nests them (`research/fastapi/01-routing-and-request-lifecycle.md:16`). Litestar provides layered middleware (pure-ASGI, no `call_next` dispatch tax), lifespan + before/after request hooks, and proper background tasks with cleanup ([github.com/litestar-org/litestar](https://github.com/litestar-org/litestar)). Sanic has first-class `add_task` and listener-based lifecycle ([sanic.dev]). **RECOMMENDATION:** Lancelot ships a Rust-owned **detached** background pool (not post-send sequential) — see §5.

### 1.6 Auth / CORS / Sessions

| Capability | FA | RB | LS | ST | QT | SA | BS | FL | EM | DN |
|---|--|--|--|--|--|--|--|--|--|--|--|
| OAuth2 / JWT helpers | ● `OAuth2PasswordBearer` | ○ | ● security backends | ○ | ○ | ◐ ext | ● JWT/APIKey/Basic | ○ | ◐ | ● auth ext |
| API-key auth | ● | ◐ auth handler | ● | ○ | ○ | ◐ | ● | ○ | ◐ | ● |
| Sessions (client/server) | ◐ via Starlette | ○ | ● client+server | ◐ `SessionMiddleware` | ● | ◐ | ● | ○ | ● | ● Django |
| Route guards / scopes | ● `Security(scopes)` | ◐ `auth_required` | ● guards | ○ | ○ | ◐ | ● | ○ | ◐ | ◐ |
| CORS | ● | ● | ● | ● | ● | ● | ● | ◐ | ◐ | ● |

**KNOWN.** FastAPI ships OAuth2 password/bearer flows, `Security()` scopes, API-key helpers — all auto-documented in OpenAPI (a major adoption driver, §2). Litestar ships JWT/session/API-key backends + guards as built-ins, not plugins ([github.com/litestar-org/litestar](https://github.com/litestar-org/litestar)). BlackSheep added (v2.4.2+) built-in Basic/API-key/JWT-Bearer with auto OpenAPI security schemes ([neoteroi.dev/blacksheep](https://www.neoteroi.dev/blacksheep/)). Robyn has a single pluggable `authentication_handler` + per-route `auth_required` (`research/robyn/03-python-api-pyo3-bridge.md:96,104`) — far thinner than FastAPI's surface.

### 1.7 WebSockets / SSE / Streaming

| Capability | FA | RB | LS | ST | QT | SA | BS | FL | EM | DN |
|---|--|--|--|--|--|--|--|--|--|--|--|
| WebSockets | ● | ● Rust channels | ● | ● | ● | ● | ● | ○ | ● | ◐ |
| WS pub/sub channels | ○ | ◐ broadcast | ● `channels` plugin | ○ | ○ | ◐ PubSub | ○ | ○ | ○ | ○ |
| SSE | ● `EventSourceResponse` | ◐ stream | ● `ServerSentEvent` | ◐ `StreamingResponse` | ◐ | ◐ | ● SSE | ○ | ◐ | ○ |
| JSONL / chunked stream | ● native | ◐ per-token GIL (AVOID) | ◐ | ◐ | ◐ | ◐ | ◐ | ○ | ○ | ○ |
| Streaming + DI lifetime | ● PEP-789 correct | ○ | ◐ | ○ | ○ | ○ | ○ | ○ | ○ | ○ |
| Backpressure (per-token) | ◐ `drain()` Future | ○ spawn_blocking | ○ | ◐ | ○ | ○ | ○ | ○ | ○ | ○ |
| Client-disconnect → cancel | ◐ checkpoints | ○ | ◐ | ◐ | ○ | ○ | ○ | ○ | ○ | ○ |

**KNOWN — this row is the differentiation surface.** FastAPI's fork has an unusually good SSE *architecture*: decoupled producer task + bounded channel + a **separate keepalive timer wrapping the channel receive, never the generator `__anext__`** (PEP-789: wrapping the generator kills the LLM stream via `GeneratorExit`), forced `Cache-Control: no-cache` + `X-Accel-Buffering: no`, `anyio.sleep(0)` cancellation checkpoints — steal wholesale (`research/fastapi/03-validation-serialization-openapi.md:471-520`; `research/SYNTHESIS.md:36,68`). Its weakness: sync-generator SSE does one thread hop *per chunk* (`research/fastapi/01-routing-and-request-lifecycle.md:392`). Robyn's streaming is the **one thing it must not be copied on**: `gen.__next__()` in `spawn_blocking` + `with_gil` per chunk, sync-gen only, `String` copy (`research/SYNTHESIS.md:36,95`). Blazeio is the per-token cost winner: boolean send-backpressure gate (no Future per token), pause-by-default pull receive, prepare-time writer rebind, chunked/SSE hand-framing `b"data: %b\n\n"` OpenAI-compatible (`research/blazeio/02-request-response-streaming-static.md:12-22`; `research/SYNTHESIS.md:37,38,67`). **No competitor combines all of: per-token zero-alloc backpressure + Rust SSE codec + mid-stream DI lifetime + disconnect→upstream-abort.** That gap is Lancelot's.

### 1.8 Testing / Plugins / ORM / Config / CLI / Static / Templating / Rate-limit / Cache

| Capability | FA | RB | LS | ST | QT | SA | BS | FL | EM | DN |
|---|--|--|--|--|--|--|--|--|--|--|--|
| Test client | ● `TestClient` | ◐ | ● `TestClient`+factory | ● | ● | ● | ● | ● `testing` | ◐ | ● Django |
| Plugin/extension protocol | ○ (DI/middleware only) | ○ | ● 4 plugin protocols | ○ | ◐ ext | ◐ | ● bindings | ○ | ◐ | ◐ |
| ORM/DB integration story | ○ userland | ○ | ● SQLAlchemy plugin | ○ | ○ | ◐ ext | ◐ | ○ | ● built-in ORM | ● Django ORM |
| Settings/config | ◐ Pydantic-settings | ◐ `Config` | ● typed settings | ○ | ◐ | ● `app.config` | ● | ○ | ● | ● Django settings |
| CLI / scaffolding | ◐ `fastapi` CLI | ● `robyn` CLI | ● `litestar` CLI | ○ | ○ | ● `sanic` CLI | ◐ | ○ | ● `emmett` CLI | ● Django mgmt |
| Static files | ◐ Starlette | ◐ weak | ● | ● | ● | ● | ● | ○ | ● | ● Django |
| Templating | ◐ Jinja via Starlette | ◐ | ● | ● Jinja | ● Jinja | ● Jinja | ● | ○ | ● Renoir | ● Django |
| Rate limiting | ○ userland | ○ | ● built-in | ○ | ○ | ◐ ext | ● | ○ | ○ | ◐ ext |
| Caching / response cache | ○ userland | ◐ const-tier | ● response cache | ○ | ○ | ◐ | ● | ○ | ● | ◐ Django cache |
| Compression (gzip/br/zstd) | ◐ Starlette gzip | ○ | ● gzip/brotli/zstd | ◐ gzip | ◐ | ◐ | ● | ○ | ◐ | ◐ |

**KNOWN.** This row is where Litestar's "batteries included vs FastAPI's bring-your-own" thesis is decisive: Litestar bundles first-class SQLAlchemy, client+server sessions, response caching, rate-limiting, CORS/CSRF, compression (gzip/brotli/zstd), OpenTelemetry — without third-party packages ([byteiota.com/litestar-vs-fastapi-python-speed-test-2026-analysis](https://byteiota.com/litestar-vs-fastapi-python-speed-test-2026-analysis/); [github.com/litestar-org/litestar](https://github.com/litestar-org/litestar)). FastAPI deliberately ships **no ORM, no admin, no auth backend, no rate-limit, no cache** — pushed to the team ([uvik.net/blog/python-api-framework](https://uvik.net/blog/python-api-framework/)). Django-Ninja inherits Django's entire batteries (ORM, admin, sessions, cache, mgmt CLI) — its whole pitch is "FastAPI ergonomics on Django's foundation" ([django-ninja.dev](https://django-ninja.dev/)). Robyn's static subsystem is weak (`research/SYNTHESIS.md:47`); Blazeio's is dead code with off-by-one range bugs and no `sendfile(2)` (`research/blazeio/02-request-response-streaming-static.md:18-19`; `research/SYNTHESIS.md:104`).

### 1.9 The server-layer entrants (not frameworks)

| | Granian | Socketify |
|---|---|---|
| What it is | Rust HTTP server (hyper+tokio) for ASGI/WSGI/RSGI apps | µWebSockets-backed server + thin framework |
| HTTP/2, mTLS | ● | ◐ |
| WebSockets | ● | ● (its strength) |
| Routing/DI/validation/OpenAPI | ▦ none — not a framework | ◐ minimal |
| RSGI (low-overhead protocol) | ● Rust-memory-backed request, no dict | ○ |
| Workers / threading modes | ● st/mt, worker procs, backpressure | ◐ |
| Prometheus metrics | ● | ○ |

**KNOWN.** Granian is *explicitly a server, not a framework* — "your application handles business logic; Granian handles HTTP plumbing"; it has no routing/DI/validation/OpenAPI/middleware ([github.com/emmett-framework/granian](https://github.com/emmett-framework/granian)). Its RSGI protocol passes a lightweight Rust-memory-backed object instead of a giant ASGI dict; benchmarks (2026-04-07, Granian 2.7.3, Ryzen 7 5700X) show RSGI > ASGI > Uvicorn-h11, ~20-50% higher throughput than uvicorn for CPU-bound, narrowing for I/O ([github.com/emmett-framework/granian/blob/master/benchmarks/vs.md](https://github.com/emmett-framework/granian/blob/master/benchmarks/vs.md); [blog.hashhackers.com/blog/granian-uvicorn-asgi](https://blog.hashhackers.com/blog/granian-uvicorn-asgi/)). **This is the key positioning fact for §3:** Granian proves the Rust-server thesis but stops at the server boundary; the per-request Python framework tax (DI re-solve, double-parse validation, jsonable_encoder) is untouched because Granian doesn't own the framework. Lancelot owns both — that is the entire point.

---

## 2. The Adoption-Critical Subset — Table-Stakes vs Nice-to-Have vs Differentiator

The migration question is not "is it faster?" It is **"can I move my FastAPI app over a weekend without rewriting how I think?"** Speed alone never moves anyone — Litestar is faster than FastAPI on synthetic benchmarks and still loses "almost every real production decision" ([byteiota.com/litestar-vs-fastapi-python-speed-test-2026-analysis](https://byteiota.com/litestar-vs-fastapi-python-speed-test-2026-analysis/)). So the table-stakes set is defined by *what would force a rewrite if missing*, not by what's nice.

### 2.1 What actually drives FastAPI adoption (so we know what we must match)

**KNOWN — from 2026 adoption research:** FastAPI went 29%→38% of professional Python devs in one year ([programming-helper.com/.../fastapi-2026-python-api-framework-ai-ml-adoption-enterprise](https://www.programming-helper.com/tech/fastapi-2026-python-api-framework-ai-ml-adoption-enterprise); JetBrains survey). The cited drivers, in order:

1. **One type annotation → validation + DI + OpenAPI.** "Type hints operational rather than merely documentary" ([zyneto.com/blog/python-api-development-with-fastapi](https://zyneto.com/blog/python-api-development-with-fastapi)). This is *the* hook. It is the FastAPI moat (`research/SYNTHESIS.md:49,57`). **Table-stakes — non-negotiable.** A framework that makes you declare schemas twice (request model + OpenAPI) has already lost.
2. **Auto OpenAPI + Swagger/Scalar.** "API-first, headless frontends" need a contract for free ([uvik.net/blog/python-api-framework](https://uvik.net/blog/python-api-framework/)). **Table-stakes.**
3. **`Depends()` DI.** The mental model people carry between projects; teaches testability (override deps in tests). **Table-stakes** — Robyn's *lack* of `Depends()` is its single biggest adoption blocker for FastAPI migrants (`research/robyn/03-python-api-pyo3-bridge.md:16`).
4. **AI/ML serving is the dominant new workload.** "FastAPI is the de facto choice for ML model serving — async inference, batch + streaming endpoints, HF/PyTorch/TF integration" ([programming-helper.com/...](https://www.programming-helper.com/tech/fastapi-2026-python-api-framework-ai-ml-adoption-enterprise)). **This is Lancelot's wedge — match the ergonomics, *exceed* on the AI hot path.**
5. **Async-native, sync still works.** The expectation is async-first but a blocking `def` handler must still be safe (offloaded). FastAPI's `CapacityLimiter(40)` cliff is the #1 production footgun ([zestminds.com/blog/fastapi-production-issues-under-load](https://www.zestminds.com/blog/fastapi-production-issues-under-load/); `research/SYNTHESIS.md:87`). **Table-stakes to support sync; differentiator to do it without the cliff.**

### 2.2 The classification (opinionated, with reasoning)

**TABLE-STAKES (v0 — absence = "this is a rewrite, not a migration" → no adoption):**

| Feature | Why it's table-stakes (not optional) |
|---|---|
| Typed path/query/body params | The FastAPI mental model. Without it the migrant rewrites every handler signature. (`research/SYNTHESIS.md:57`) |
| Pydantic v2 request/response validation | Their models are *already written in Pydantic*. A different model lib (msgspec-only) = rewrite all schemas. Must accept Pydantic even if msgspec is the fast path. |
| `Depends()` DI + `yield` teardown | The unit of code reuse and testability across the entire FastAPI ecosystem. Robyn proves omitting it blocks migration. |
| Auto OpenAPI 3.1 + Swagger + Scalar/ReDoc | Their frontend/SDK gen/contract tests consume `/openapi.json`. Break it = break their whole pipeline. |
| ASGI-compatible middleware | Their auth/logging/Sentry/OTel middleware is ASGI. Must run unmodified or it's a rewrite + risk. |
| Lifespan startup/shutdown | DB pools, model loading, warm caches. Universal. |
| CORS | Every browser-facing API. One missing config = broken prod. |
| Sync **and** async handlers (sync offloaded, bounded) | ML code is full of blocking calls (`model.predict`). Must not stall the loop *and* must not silently cliff at 40. |
| JSON responses with the **exact** FastAPI 422 wire-shape `{"detail":[{type,loc,msg,input,ctx}]}` | Frontends and client SDKs parse this byte-for-byte. (`research/SYNTHESIS.md:79`) |
| SSE + JSONL streaming responses | The AI workload. Not optional for the target market — it *is* the target market. |
| WebSockets | Real-time/agent UIs, token streaming alt-transport. Migrants have these. |
| `TestClient` (httpx-compatible) | Their entire test suite is `TestClient(app)`. No test client = can't even evaluate the migration. |
| Static files (correct: ETag/Range/304/`sendfile`) | Docs, assets, model cards. Robyn/Blazeio both fail here (`research/SYNTHESIS.md:47,104`) — don't repeat. |
| `include_router`/route groups + prefix | App structure. Flatten-at-registration like FastAPI (`research/SYNTHESIS.md:79`). |
| Settings/config (Pydantic-settings compatible) | 12-factor env config is universal; must read their existing `Settings`. |

**NICE-TO-HAVE (v0.x — speeds adoption, not a blocker; ship within 6 months):**

| Feature | Reasoning |
|---|---|
| OAuth2/JWT/API-key/session helpers | FastAPI ships these; migrants use `OAuth2PasswordBearer`. But many use external (Auth0/Clerk) — survivable short-term, painful long-term. v0.x. |
| Built-in rate limiting | Litestar/BlackSheep bundle it; FastAPI users use slowapi/nginx today, so they won't *block* on it — but it's a strong "batteries" differentiator (§3). |
| Response caching | Same: nice, not blocking; const-tier (§5) gives a superior story. |
| CLI/scaffolding | Helps DX/first-impression; not a migration blocker. |
| Templating (Jinja) | AI backends are mostly JSON APIs; low priority for the target market (`research/SYNTHESIS.md` blazeio templating note). |
| Compression (gzip/brotli/zstd) | Often done at the proxy (nginx/CF). Nice; not blocking. |
| Class-based controllers | Litestar/BlackSheep have them; FastAPI doesn't and is the #1 framework — proof this is not table-stakes. |
| WS pub/sub channels | Useful for multi-client agent UIs; v0.x. |
| ORM plugin (SQLAlchemy) | FastAPI ships *none* and dominates → not table-stakes. But a first-class SQLAlchemy/async-DB story is a Litestar-parity differentiator (§3). |

**DIFFERENTIATORS (the reason to switch — must be visibly better, not just present):**

| Feature | Why only Lancelot can own it (forward-ref to §5) |
|---|---|
| Per-token zero-alloc streaming backpressure (Rust SSE/JSONL codec) | FastAPI uses `drain()` Future/token; Robyn does `spawn_blocking`+GIL/token (catastrophic); nobody else has Rust framing. (`research/SYNTHESIS.md:36,67,68`) |
| Mid-stream DI lifetime (DB/LLM client open *through* the SSE stream, PEP-789-correct) | Only FastAPI's *fork* gets the architecture right and it's still Python; no other framework keeps yield-deps alive across the stream correctly. (`research/SYNTHESIS.md:141`) |
| Client-disconnect → cancel handler → **abort upstream LLM call** | Nobody does this. It's pure margin: stop paying provider tokens nobody reads. (`research/SYNTHESIS.md:148`) |
| Built-in MCP JSON-RPC 2.0 server (auto schema-from-signature) | Robyn's `mcp.py` is the only framework-level precedent; make it native. (`research/SYNTHESIS.md:45,70,151`) |
| Proper pooled async LLM client (HTTP/2, no POST-retry, circuit breaker, in-band timeout) | No framework ships an outbound AI client; Blazeio's design is the best base but has security defects to fix. (`research/SYNTHESIS.md:43,71,152`) |
| Const/static-response tier (zero GIL/Python/alloc) | Only Robyn has it; improve it (middleware-compatible, slash-normalized). ~10–50× on health/cached/docs. (`research/SYNTHESIS.md:31,61`) |
| No 40-thread sync cliff: Rust admission control + 503 `Retry-After` | FastAPI's #1 production footgun fixed structurally. (`research/SYNTHESIS.md:87`) |
| Lazy request marshalling (~1–3 allocs/req vs FA ~10–15, RB ~30–66) | Architectural; can't be retrofitted onto ASGI. (`research/SYNTHESIS.md:60,136`) |

**INFERRED — the strategic point:** the table-stakes list is almost entirely *FastAPI-API-shape compatibility*. The cheapest path to adoption is **be FastAPI's API surface, byte-compatible on the wire (422 shape, OpenAPI, Depends), on a faster core** — then the differentiators give a *reason* beyond speed. A migrant evaluates in this order: (1) does my code run mostly unchanged? (2) does my test suite pass? (3) is `/openapi.json` identical enough for my SDK gen? (4) *then* is it faster / does it do the AI thing better? Fail (1)–(3) and (4) is never reached. This is why "FastAPI-compatible surface" is the product, and the Rust core + AI tier is the *wedge*, not the pitch.

---

## 3. Deep-Dive: Litestar (the credible challenger) + Granian (the server angle)

### 3.1 Litestar — what it does better than FastAPI

**KNOWN.** Litestar 2.x is the only framework with both FastAPI-class ergonomics *and* a real "FastAPI's problems, fixed" narrative. What it genuinely beats FastAPI on:

1. **Pluggable serialization, msgspec-default.** Litestar isn't married to Pydantic — dataclass/TypedDict/attrs/Pydantic(v1+v2)/msgspec are all first-class via the serialization plugin protocol ([github.com/litestar-org/litestar](https://github.com/litestar-org/litestar)). msgspec is cited at 10–20× faster than Pydantic v2, 150× faster than v1, with ~40–120% production improvements reported on FastAPI→Litestar migrations ([byteiota.com/...](https://byteiota.com/litestar-vs-fastapi-python-speed-test-2026-analysis/); [betterstack.com/.../litestar-vs-fastapi](https://betterstack.com/community/guides/scaling-python/litestar-vs-fastapi/)). **Lesson for Lancelot:** *don't* be Pydantic-only at the wire (lock-in) but *do* make Pydantic the table-stakes default (migration). The fast path can be a Rust validator; the compatibility path must accept their existing Pydantic models. This is exactly the SYNTHESIS "classify params at registration: needs-Pydantic vs primitive-scalar-parse-in-Rust" call (`research/fastapi/01-routing-and-request-lifecycle.md:136`).
2. **DI is cleaner and layered.** pytest-inspired injection, scoped at app/router/controller/handler, async-native, cleanup generators — broadly judged better-typed than FastAPI's ([betterstack.com/.../litestar-vs-fastapi](https://betterstack.com/community/guides/scaling-python/litestar-vs-fastapi/)). **Lesson:** Lancelot's compile-once DI must keep FastAPI's `Depends()` *surface* (migration) but adopt Litestar's *layering* (app/router/route scoping) — it's a strict ergonomics superset and cheap to add at registration.
3. **DTOs as a first-class concept.** `DTOFactory` separates the wire shape from the domain/ORM model (in/out projections, field exclusion, renaming) — solves the "I don't want to hand-write a Pydantic mirror of my SQLAlchemy model" pain FastAPI pushes to the user ([byteiota.com/...](https://byteiota.com/litestar-vs-fastapi-python-speed-test-2026-analysis/)). **Lesson:** a DTO/projection layer is a v0.x differentiator that directly attacks FastAPI's worst boilerplate; design the DI/validation compiler so DTO projections compile to the same Rust plan.
4. **Batteries included.** SQLAlchemy plugin, client+server sessions, response caching, rate-limiting, CORS/CSRF, compression (gzip/brotli/zstd), OpenTelemetry, 5 doc UIs — all built-in, no third-party ([github.com/litestar-org/litestar](https://github.com/litestar-org/litestar)). FastAPI ships *none* of these. **Lesson:** "batteries" is real adoption leverage *but is not table-stakes* (FastAPI proves you can dominate with none) — so it's v0.x, behind the AI differentiators.
5. **Plugin protocol (the ecosystem multiplier).** Four typed protocols — `InitPlugin` (mutate `AppConfig` at startup), `SerializationPlugin`/DTO, `DIPlugin` (teach DI about non-annotated `__init__`), `OpenAPISchemaPlugin` (schema for non-library types) ([docs.litestar.dev/latest/reference/plugins](https://docs.litestar.dev/latest/reference/plugins/index.html)). This is the single biggest structural thing Litestar has that FastAPI lacks — see §4.

**KNOWN — what Lancelot should *beat* Litestar on (its weaknesses):**

- **It's pure-Python on an ASGI server it doesn't own.** All msgspec speed is still inside a Python event loop behind uvicorn/Granian. The per-request Python frame, the ASGI dict marshalling, the GIL — untouched. Lancelot's Rust core is *categorically* below where Litestar can reach. The synthetic-vs-production collapse ("Litestar wins charts, FastAPI wins production decisions", [byteiota.com/...](https://byteiota.com/litestar-vs-fastapi-python-speed-test-2026-analysis/)) happens because the framework was never the bottleneck *at Litestar's layer* — it is at Lancelot's (allocation count, GIL hold, double-parse).
- **Ecosystem & hiring gap.** "Litestar has a small candidate pool"; FastAPI has 4.5M daily downloads, OpenAI/Anthropic/Microsoft in prod ([betterstack.com/.../litestar-vs-fastapi](https://betterstack.com/community/guides/scaling-python/litestar-vs-fastapi/)). **Strategic read:** competing on "better framework design" (Litestar's pitch) loses to FastAPI's gravity. Competing on **"your FastAPI code, unchanged, on a Rust core, with an AI tier nobody else has"** sidesteps the ecosystem fight — you inherit FastAPI's ecosystem by being wire/API-compatible instead of asking people to relearn.
- **No AI-native layer.** Litestar has SSE and websockets but no MCP server, no pooled LLM client, no per-token Rust backpressure, no disconnect→upstream-abort. It treats streaming as generic HTTP, not as the headline workload. This is the open lane.

### 3.2 Granian — "framework, not just server"

**KNOWN.** Granian is the strongest proof of the Rust-server thesis and simultaneously the strongest argument *for* Lancelot's framework-level positioning:

- Granian is *explicitly not a framework*: no routing, DI, validation, OpenAPI, middleware — "your application handles business logic; Granian handles HTTP plumbing" ([github.com/emmett-framework/granian](https://github.com/emmett-framework/granian)).
- Its RSGI protocol's whole point is that the ASGI dict is wasteful — it passes a lightweight Rust-memory-backed request object instead ([github.com/emmett-framework/granian/blob/master/docs/spec/RSGI.md](https://github.com/emmett-framework/granian/blob/master/docs/spec/RSGI.md)). This validates Lancelot's lazy-proxy thesis (`research/SYNTHESIS.md:60`) — *but Granian still hands that object to a pure-Python framework that immediately re-marshals it through ASGI/Pydantic.* RSGI only removes the server↔framework dict; it cannot remove the framework's own per-request DI re-solve, double-parse validation, or `jsonable_encoder`. Those live *above* the server boundary, in FastAPI/Litestar.
- Benchmarks (2026-04-07, Granian 2.7.3): RSGI > ASGI > uvicorn-h11, ~20–50% throughput over uvicorn CPU-bound, narrowing I/O-bound; tighter avg/max latency ratio (~2.8× vs uvicorn's ~6.8×) ([github.com/emmett-framework/granian/blob/master/benchmarks/vs.md](https://github.com/emmett-framework/granian/blob/master/benchmarks/vs.md); [blog.hashhackers.com/blog/granian-uvicorn-asgi](https://blog.hashhackers.com/blog/granian-uvicorn-asgi/)).

**INFERRED — the positioning Lancelot owns vs Granian:** Granian optimizes the *bottom* of the stack and leaves the framework tax fully intact. FastAPI-on-Granian is still FastAPI's O(N) regex routing, per-request DI re-solve, double-parse validation, `jsonable_encoder`, and 40-thread sync cliff — Granian can't touch any of it because it's not the framework. **Lancelot collapses the server *and* the framework into one Rust core, so the optimization is end-to-end: the same Rust pass that accepted the bytes also routes (trie + const tier), binds the compiled DI plan, validates via pydantic-core, and frames the SSE stream — no ASGI/RSGI boundary, no Python re-marshal.** The one-liner: *Granian makes your framework's server fast; Lancelot makes the framework itself disappear into Rust.* And critically — a "fast server" does nothing for the AI streaming hot path (per-token backpressure, mid-stream DI, disconnect→abort, MCP) because that logic lives in the framework, which Granian explicitly is not.

---

## 4. Extensibility / Plugin Architecture — the Ecosystem Multiplier

A framework's ceiling is its plugin model. FastAPI's dominance is partly *despite* a weak one (it has none — extension happens only via DI, middleware, and `include_router`); Litestar's bet is that a typed plugin protocol is the moat FastAPI left open. For Lancelot this is strategic: an ecosystem forms only if third parties can extend **without forking the Rust core**.

### 4.1 How the incumbents let third parties extend (KNOWN)

| Framework | Extension mechanism | Ceiling |
|---|---|---|
| **FastAPI** | (a) DI: `Depends()` is the universal extension point — auth, db sessions, rate-limit all ride DI; (b) ASGI middleware; (c) `APIRouter` + `include_router` for modular mounting; (d) `dependency_overrides` for test/extension swap; (e) custom `APIRoute` subclass (rare, powerful — used by libs to inject behavior per route). No formal plugin protocol; no startup-config hook beyond lifespan. (`research/fastapi/01-routing-and-request-lifecycle.md:191`, `research/fastapi/02-dependency-injection.md`) | Extension = "write a dependency or middleware." Can't extend serialization, OpenAPI generation, or the type system. Ecosystem libs (sqlmodel, fastapi-users, slowapi) are all DI/middleware shaped — proves the model *works at scale* but caps what a plugin can do. |
| **Litestar** | Four typed plugin protocols: `InitPlugin.on_app_init(AppConfig)` (mutate routes/DI/middleware/config at startup — the master hook), `SerializationPlugin`/DTO (teach (de)serialization for new types), `DIPlugin` (teach DI to construct types whose `__init__` isn't annotation-extractable), `OpenAPISchemaPlugin` (emit schema for non-library types). Plugins compose; the SQLAlchemy plugin *is* an `InitPlugin`+`SerializationPlugin` ([docs.litestar.dev/latest/reference/plugins](https://docs.litestar.dev/latest/reference/plugins/index.html)). | The most extensible Python API framework. A plugin can add types, change serialization, inject routes/middleware, extend OpenAPI — without touching core. This is the model to learn from. |
| **Django (Ninja)** | Django "apps" = the original plugin system: installed-apps registry, app `ready()` hook, signals, middleware stack, pluggable backends (db/cache/auth/storage). Ninja layers typed routers on top. Maturity here is *why* Django-Ninja's pitch ("FastAPI ergonomics, Django batteries") works. | Heavyweight, framework-wide, not API-shaped — but the deepest ecosystem in Python. The lesson is *registry + lifecycle hook + pluggable backend*, not the specifics. |
| **BlackSheep** | DI container (`rodi`) is the extension point + ASGI middleware + binders (custom param sources). Closer to FastAPI's "DI is the plugin model" than to Litestar's protocols. | Mid. |
| **Robyn** | Effectively none — subclassing `BaseRobyn`, the single `authentication_handler`, and `include_router`. `mcp.py` is bolted on, not a plugin. (`research/robyn/03-python-api-pyo3-bridge.md:91,96`) | Low. A reason Robyn has no ecosystem. |

### 4.2 What plugin model Lancelot should ship (RECOMMENDATION)

The constraint that makes this hard and important: **the hot path is Rust, compiled once at registration.** A plugin that needs to run per-request in Python on the hot path defeats the entire thesis. So Lancelot's plugin model must be **registration-time-heavy, request-time-thin** — plugins contribute to the *compiled plan*, they don't intercept the *execution*.

Adopt Litestar's protocol shape, mapped onto the Rust-compile model:

1. **`InitPlugin.on_app_init(AppConfig) -> AppConfig`** — the master startup hook. Runs *before* the registration compiler. A plugin can add routes, DI providers, middleware, settings, OpenAPI metadata. Everything it adds is then compiled into the Rust plan like first-party code. (Litestar-shaped; cheap; the single most important one.)
2. **`SerializationPlugin` / DTO protocol** — register a (type → validate_fn, serialize_fn) pair. At registration the compiler classifies the type: if the plugin provides a pydantic-core/msgspec/Rust-native codec → fast path; else a Python-callable codec slot in the compiled plan (slow but correct). This is how Lancelot accepts SQLAlchemy models, attrs, msgspec *without* being Pydantic-only — directly counters Litestar's main advantage (§3.1).
3. **`DIPlugin`** — teach the compile-once DI graph to construct a type whose `__init__` isn't annotation-derivable (ORM sessions, clients). Contributes a node to the immutable DAG at registration; zero per-request cost (the SYNTHESIS DI-compiler thesis, `research/SYNTHESIS.md:139`).
4. **`OpenAPISchemaPlugin`** — emit JSON-Schema for non-library types, folded into the single boot-time schema build (`research/fastapi/03-validation-serialization-openapi.md` §5-6).
5. **`MiddlewarePlugin` — two tiers (the key Lancelot-specific design):** (a) **Rust/ASGI-pure middleware** that compiles into the Rust chain (zero Python on hot path; const-tier-compatible) for the 90% case (CORS, auth header check, rate-limit, logging); (b) an **ASGI-compat escape hatch** so the existing FastAPI/Starlette middleware ecosystem runs unmodified (migration table-stakes, §2). Never a `call_next`/`BaseHTTPMiddleware` dispatch model (`research/SYNTHESIS.md:88`).
6. **`LifecyclePlugin`** — typed startup/shutdown + the SYNTHESIS PEP-789 yield-teardown ordering, so a plugin can own a resource whose lifetime spans an SSE stream correctly (`research/SYNTHESIS.md:141`).

**INFERRED — why this specific shape:** every protocol contributes to the *registration-time compile* and nothing forces a per-request Python frame on the hot path. That preserves the Rust-core thesis while giving third parties the same extension surface Litestar has — the SQLAlchemy/auth/rate-limit ecosystem can form *without* anyone needing to write Rust. The plugin author writes Python at registration; the request path stays Rust. This is the unique synthesis: **Litestar's extensibility surface with Robyn's "all reflection at boot" ABI** (`research/robyn/03-python-api-pyo3-bridge.md:81`). Ship protocols 1–4 in v0.x, the two-tier middleware in v0 (the ASGI escape hatch is table-stakes), `LifecyclePlugin` in v0 (lifespan is table-stakes).

---

## 5. The Gap — Lancelot's Unique, Defensible Position

The question is not "what feature is missing from FastAPI" (the answer is "nothing critical — that's why it dominates"). The question is **"what does a startup building an AI backend need that NO framework on the matrix can structurally provide, and that Lancelot's architecture (Rust core + compile-once DI + native streaming + AI layer) uniquely can?"**

### 5.1 The structural truth nobody else can match

Every competitor is on the wrong side of one of two architectural lines:

- **Pure-Python frameworks (FastAPI, Litestar, Starlette, Quart, Sanic, BlackSheep, Falcon, Emmett, Django-Ninja):** the request lifecycle is Python on an event loop behind a server they don't own. They *cannot* make the lifecycle GIL-free, cannot get to ~1–3 allocs/req, cannot frame SSE in Rust, cannot abort an upstream LLM call from the transport layer without Python coroutine machinery. Litestar's msgspec is the ceiling of what's reachable here, and it's still above Lancelot's floor.
- **Server-only Rust tools (Granian, Socketify):** own the metal but not the framework, so the DI/validation/streaming/AI logic — the part that matters for AI backends — runs in the pure-Python framework on top, untouched.
- **Robyn:** the only one on the right side of *both* lines (Rust core + framework) — but it has no `Depends()` (migration blocker, §2), eager marshalling (~30–66 allocs/req, `research/SYNTHESIS.md:32`), and streams catastrophically (`spawn_blocking`+GIL/token, `research/SYNTHESIS.md:95`). It got the architecture and fumbled the two things that matter: ergonomics and the AI hot path.

**Lancelot is the only point in the design space that is Rust-core + FastAPI-API-compatible + AI-hot-path-correct.** That is not a feature; it is a structural position no incumbent can reach without rewriting their foundation (pure-Python frameworks) or building a framework (Granian) or fixing both their fatal flaws (Robyn).

### 5.2 The five concrete things only Lancelot can do (the defensible moat)

**1. End-to-end backpressured LLM streaming with zero per-token Python/alloc.** A `SSEResponse`/`ChunkedResponse` whose codec is Rust: `b"data: %b\n\n"` framed in Rust, bounded SPSC ring (size-1 default = lowest latency + backpressure onto the model), timer-wheel keepalive that wraps the *channel receive* not the generator (PEP-789), boolean send-backpressure gate (no Future/token, Blazeio's pattern, `research/SYNTHESIS.md:38,67`). Slow client → stalled ring → stalled handler → stalled upstream LLM read → **bounded server RAM regardless of completion length.** FastAPI does `drain()` (a Future per token); Robyn does `spawn_blocking`+GIL per token; Litestar/others have no Rust framing at all. **Concrete need it serves:** a startup streaming GPT-4-class completions to thousands of concurrent slow mobile clients without OOM or token-rate collapse. *Nobody on the matrix can do this.* (`research/SYNTHESIS.md:36,37,38,67,145`)

**2. Mid-stream dependency lifetime — DB/LLM client open *through* the SSE stream, closed after.** The compile-once DI exit-stack must keep yield-deps alive across the entire stream and tear down in correct order *after* the last token, not when the handler returns. FastAPI's *fork* gets this right and it's still Python-coroutine machinery; no other framework gets it right at all (they close the dependency when the handler function returns, *before* the stream finishes — a silent correctness bug for "open DB cursor, stream rows as SSE" and "hold provider client, stream tokens"). Lancelot does it in the Rust-owned request scope. **Concrete need:** "stream rows from a server-side cursor as they arrive" / "hold one provider connection for the whole completion" — both broken on every framework except FastAPI's fork. (`research/SYNTHESIS.md:141`)

**3. Client-disconnect → cancel handler → abort the upstream provider call.** When the browser closes the SSE connection, Lancelot cancels the handler *and propagates the cancel into the outbound LLM client to abort the in-flight provider request.* This is pure money: every token generated after the user navigated away is billed and wasted. No framework does this — they keep generating to a dead socket. With Lancelot's built-in pooled LLM client (next point) the cancel path is owned end-to-end. **Concrete need:** chat UIs where users abandon mid-generation constantly; this is a direct, measurable cloud-bill reduction nobody else offers. (`research/SYNTHESIS.md:148`)

**4. A built-in MCP JSON-RPC 2.0 server as a framework primitive.** `@app.mcp.tool/.resource/.prompt` with auto JSON-Schema-from-signature, URI-template resources, protocol `2024-11-05` — Robyn's `mcp.py` shape made native (`research/SYNTHESIS.md:45,70,151`; `research/robyn/03-python-api-pyo3-bridge.md:18`). Every other framework forces you to hand-build an MCP server in userland. As agentic backends become standard, "your API is also an MCP server for free, from the same type annotations that generate OpenAPI" is the same trick FastAPI won on (one annotation → many artifacts) applied to the agentic era. **Concrete need:** AI startups increasingly must expose tools to agents (Claude/MCP clients) *and* a REST API — Lancelot makes them the same registration.

**5. A proper pooled async LLM client as framework infrastructure.** Per-(scheme,host,port) pool, stale-detect-before-handout, least-in-flight, **POST never auto-retried** (no duplicate completions/double billing), HTTP/2, verify=True, DNS cache, in-band `asyncio.timeout`, per-host circuit breaker, concurrency semaphore, orjson bytes-in/out (Blazeio's design minus its security defects, `research/SYNTHESIS.md:43,71,152`). No framework ships an outbound AI client; everyone bring-your-own httpx and re-implements retry/timeout/pooling wrong (auto-retried POSTs = duplicate charges is a real, common production bug). Owning this *and* the disconnect→abort path (#3) makes the whole inbound-stream ↔ outbound-LLM lifecycle one coherent, backpressured, cancellation-correct system — the single thing AI backends need most and the thing every generic framework structurally cannot provide because they don't own the transport.

### 5.3 Supporting moat (not unique alone, but compounding)

- **Const/static-response tier** (Robyn-only idea, improved): zero GIL/Python/alloc for health checks, cached JSON, `/openapi.json`, `/docs` — ~10–50× (`research/SYNTHESIS.md:31,61`).
- **No 40-thread sync cliff:** Rust admission control + bounded load-shedding blocking pool + 503 `Retry-After` — structurally fixes FastAPI's #1 production footgun ([zestminds.com/blog/fastapi-production-issues-under-load](https://www.zestminds.com/blog/fastapi-production-issues-under-load/); `research/SYNTHESIS.md:87,122`). Critical because ML handlers are full of blocking `model.predict()`.
- **Proper background tasks:** Rust-owned detached pool, not FastAPI's broken post-send-sequential model ([github.com/fastapi/fastapi/issues/1313](https://github.com/fastapi/fastapi/issues/1313); `research/SYNTHESIS.md:90`).
- **Lazy request marshalling** (~1–3 allocs/req): can't be retrofitted onto ASGI; fixes Robyn's worst flaw (`research/SYNTHESIS.md:60,136`).

### 5.4 The defensible-position statement (INFERRED)

The moat is not "faster" (Litestar is faster than FastAPI and still loses) and not "Rust" (Granian is Rust and isn't a framework). **The moat is: the only framework where the inbound HTTP stream and the outbound LLM call are one Rust-owned, backpressured, cancellation-correct system, exposed through FastAPI's exact ergonomics so migration is near-zero-cost.** A startup building an AI backend gets: their FastAPI code runs mostly unchanged, their tests pass, their OpenAPI is identical — *and* token streaming doesn't OOM under slow clients, abandoned generations stop billing immediately, the same annotations expose an MCP server, and the outbound LLM client can't double-charge. No competitor can offer that combination without rebuilding their foundation. That is the gap, and it is exactly the architecture in `research/SYNTHESIS.md` §4.

---

## 6. Prioritized Feature Surface — v0 / v0.x / v1

> Sequencing rule: **v0 = "a FastAPI app migrates over a weekend and the tests pass"** (table-stakes, the price of entry) **+ the two cheapest differentiators that demo the thesis** (streaming, const-tier). v0.x = close the Litestar batteries gap + ship the plugin protocols (ecosystem). v1 = the full AI moat + the long-tail parity. Differentiators are interleaved early on purpose: without a visible reason-to-switch in v0, there is no v0 adoption — FastAPI-parity-but-faster is necessary, not sufficient.

### v0 — Table-stakes + thesis-proving differentiators (the migration MVP)

**Table-stakes (no adoption without ALL of these):**
1. Typed path/query/body params (FastAPI-shaped signatures)
2. Pydantic v2 request + response validation (accept users' existing models; Rust fast-path where possible)
3. `Depends()` DI with `yield`-teardown + `dependency_overrides` (for their tests)
4. Auto OpenAPI 3.1 + Swagger + Scalar/ReDoc, byte-compatible enough for SDK gen
5. `include_router`/route groups + prefix (flatten-at-registration)
6. Two-tier middleware: Rust/ASGI-pure chain **+ ASGI-compat escape hatch** (their existing middleware runs unmodified)
7. Lifespan startup/shutdown (PEP-789-correct yield ordering)
8. CORS (built-in)
9. Sync **and** async handlers — sync offloaded to a **bounded, load-shedding** pool + 503 `Retry-After` (no 40-cliff)
10. JSON responses + the **exact** FastAPI 422 wire-shape `{"detail":[{type,loc,msg,input,ctx}]}`
11. `TestClient` (httpx-compatible API)
12. WebSockets (Rust-channel backed)
13. Static files done correctly (ETag/Last-Modified/Range/304 + real `sendfile(2)`)
14. Settings/config readable from existing Pydantic-settings
15. Background tasks that actually background (Rust-owned detached pool — fixes the FastAPI footgun; table-stakes because FastAPI users *think* `BackgroundTasks` works)

**Differentiators shipped in v0 (the reason to switch, cheapest two):**
16. **AI streaming tier:** typed `SSEResponse`/`ChunkedResponse`, Rust SSE/JSONL codec, per-token zero-alloc backpressure, mid-stream DI lifetime, client-disconnect → cancel
17. **Const/static-response tier:** zero-GIL/Python/alloc for health/cached/`/openapi.json`/`/docs`

### v0.x — Close the batteries gap + ship the ecosystem hooks

18. Plugin protocols: `InitPlugin`, `SerializationPlugin`/DTO, `DIPlugin`, `OpenAPISchemaPlugin` (Litestar-shaped, registration-time-only)
19. OAuth2 / JWT / API-key / session helpers (FastAPI-surface-compatible) + route guards/scopes
20. DTO/projection layer (attack FastAPI's "hand-write a Pydantic mirror of my ORM model" pain; directly counters Litestar §3.1)
21. Pluggable serialization beyond Pydantic (msgspec/attrs/dataclass/SQLAlchemy via the SerializationPlugin) — neutralizes Litestar's main edge
22. Built-in rate limiting + response caching
23. First-class SQLAlchemy/async-DB integration story (Litestar parity)
24. CLI/scaffolding (in-process hot reload, not CLI-locked — fixes Robyn's `--dev` lock-in, `research/SYNTHESIS.md:47`)
25. Layered DI scoping (app/router/route — Litestar's ergonomic superset over FastAPI)
26. **Disconnect → abort upstream LLM call** (needs the pooled client; the pure-margin differentiator)

### v1 — Full AI moat + long-tail parity

27. **Built-in MCP JSON-RPC 2.0 server** (`@app.mcp.tool/.resource/.prompt`, auto schema-from-signature) — the agentic-era OpenAPI
28. **Proper pooled async LLM client** (HTTP/2, no POST-retry, circuit breaker, in-band timeout, DNS cache, semaphore) — completing the inbound↔outbound coherent system
29. WS pub/sub channels (multi-client agent UIs)
30. Compression (gzip/brotli/zstd), templating (Jinja) — low priority for AI-API target, parity only
31. API versioning helpers, class-based controllers (parity with Litestar/BlackSheep; FastAPI proves non-critical)
32. OpenTelemetry/metrics built-in (Granian-parity observability; ops, not architecture)
33. Durable/queue-backed background jobs (beyond detached pool — for the heavyweight ML-task complaints, [leapcell.io/blog/managing-background-tasks-and-long-running-operations-in-fastapi](https://leapcell.io/blog/managing-background-tasks-and-long-running-operations-in-fastapi))

### One-line differentiation statement

**Lancelot is the only Python web framework where your unchanged FastAPI code runs on a Rust core *and* the inbound HTTP stream + outbound LLM call are one backpressured, cancellation-correct, MCP-exposing system — migration cost near zero, an AI hot path no pure-Python framework or Rust-server can structurally match.**

---

## 7. Sources

Web (2026): [byteiota Litestar vs FastAPI 2026](https://byteiota.com/litestar-vs-fastapi-python-speed-test-2026-analysis/) · [Litestar benchmarks](https://docs.litestar.dev/2/benchmarks.html) · [Better Stack Litestar vs FastAPI](https://betterstack.com/community/guides/scaling-python/litestar-vs-fastapi/) · [github litestar](https://github.com/litestar-org/litestar) · [Litestar plugins ref](https://docs.litestar.dev/latest/reference/plugins/index.html) · [Granian repo](https://github.com/emmett-framework/granian) · [Granian RSGI spec](https://github.com/emmett-framework/granian/blob/master/docs/spec/RSGI.md) · [Granian benchmarks](https://github.com/emmett-framework/granian/blob/master/benchmarks/vs.md) · [hashhackers Granian vs uvicorn](https://blog.hashhackers.com/blog/granian-uvicorn-asgi/) · [FastAPI 2026 adoption](https://www.programming-helper.com/tech/fastapi-2026-python-api-framework-ai-ml-adoption-enterprise) · [zyneto FastAPI 2026](https://zyneto.com/blog/python-api-development-with-fastapi) · [uvik Python API framework 2026](https://uvik.net/blog/python-api-framework/) · [zestminds FastAPI production issues](https://www.zestminds.com/blog/fastapi-production-issues-under-load/) · [FastAPI background tasks aren't background](https://medium.com/@garg10may/fastapi-background-tasks-arent-really-background-tasks-6f2cfbf39e0c) · [fastapi#1313](https://github.com/fastapi/fastapi/issues/1313) · [leapcell long-running ops](https://leapcell.io/blog/managing-background-tasks-and-long-running-operations-in-fastapi) · [Django Ninja](https://django-ninja.dev/) · [Django Ninja API docs](https://django-ninja.dev/guides/api-docs/) · [BlackSheep docs](https://www.neoteroi.dev/blacksheep/) · [Robyn](https://robyn.tech/)

Teardowns/SYNTHESIS (`research/`): `SYNTHESIS.md` (§1 head-to-head, §2 steal-list, §3 anti-patterns, §4 architecture) · `fastapi/01-routing-and-request-lifecycle.md` · `fastapi/02-dependency-injection.md` · `fastapi/03-validation-serialization-openapi.md` · `robyn/02-rust-routing-types-websockets.md` · `robyn/03-python-api-pyo3-bridge.md` · `blazeio/02-request-response-streaming-static.md`
