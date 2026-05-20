# Lancelot — Claude session notes

This file is read by Claude Code on every session. Stays short and operational; deep history lives in `docs/plans/` and the memory store.

## What Lancelot is (positioning lock — do not drift)

A backend framework like FastAPI / Robyn that optimizes **HTTP transport / serialization / streaming** for **AI / cloud / web / startups**.

**AI logic stays on the user side** — LangGraph, agent loops, vector stores, prompt orchestration. We own the byte path; the user owns the agent. This is the user's restated rule; every "should we build X" decision flows from it.

## Cardinal rules

1. **No bullshit, no useless logic.** Every change must change the byte path or the operator experience. If it doesn't, it shouldn't ship.
2. **Runtime dep = `pydantic>=2.7` only.** Everything else is lazy-imported or test-only. Never let `import lancelot` pull `httpx`, `opentelemetry`, `python_multipart`, etc.
3. **Vendored FastAPI tests are SACRED.** `conformance/vendor/fastapi/` is sha256-verified against the FastAPI 0.136.1 sdist. NEVER edit those test files. The "swap" in `conformance/conftest.py` is the only legal indirection.
4. **`researchfms/.git` is OFF-LIMITS.** That's a separate nested repo. Always use `git -C "$LC"` so the cwd hazard never bites.
5. **Don't tag with a red gate.** Conformance ratchet + clippy + internal tests all green before a tag is pushed.

## After every chunk

```bash
.venv/bin/python -m pytest tests/ -q               # internal smoke
make m1-conformance                                 # FastAPI byte-parity gate
$HOME/.cargo/bin/cargo clippy --release --all-targets -- -D warnings
```

If any of these go red, the chunk does not ship. The conformance floor is `passes=971` (= 99.3% of 978).

## Current milestone state (as of m2-conformance-push3)

- **M0** green (Rust core, hyper/tokio/pyo3)
- **M1-EXIT** hit at 90.0% conformance, tag `m1-exit-90`
- **M2 Tier-A** — wire/latency wins (TCP_QUICKACK/FASTOPEN, JSON depth guard, /livez/readyz, SSE keepalive, `stream_interval`), tag `m2-tierA`
- **M2 Tier-B** — cloud-native resilience (graceful shutdown, RED metrics, ContextVar request-id, connection cap, per-route timeout), tag `m2-tierB`
- **M2 Tier-C** — AI primitives (canonical JSON, session-affinity, transport-pool cancellation, tool output_schema, dynamic-write audit), tag `m2-tierC`
- **M2 Tier-C5** — marquee `@app.batch` for prefix-aware request grouping, tag `m2-tierC5`
- **M2 conformance push** — 876 → 910 Class-A passes (93.5%), tag `m2-conformance-push`. Unlocks: `@app.exception_handler` surface, `strict_content_type`, `Json[T]` Annotated preservation, lenient cookie parser, subclass-class-attr media_type, callable-instance + partial DI, single-Body scalar pattern, Starlette plaintext 500, Optional[list[T]] form wrapping.
- **M2 conformance push 2** — 910 → 948 Class-A passes (97.4%), tag `m2-conformance-push2`. Unlocks: yield-dep swallowed-exception → FastAPIError, SecurityScopes chain propagation + per-scope cache_key, stream item validation, lru_cache/wraps unwrap in DI, 204/205/304/1xx no-body emit, Depends/Security hashable, include_router empty-path/self guards, TRACE method, list[Model]/dict[Model] response coercion, Starlette ASGI-callable response delegation (FileResponse/RedirectResponse), @app.on_event decorator, PEP 695 TypeAliasType unwrap, lax-mode explicit-CT enforcement, WS metaclass for Starlette cross-stack disconnect, WS query/cookie at handshake.
- **M2 conformance push 3** — 948 → 971 Class-A passes (99.3%), tag `m2-conformance-push3`. Unlocks: scope="function" / "request" yield-dep semantics with separate AsyncExitStacks (function-scope tears down BETWEEN handler-return and response-emit; request-scope after); scope-aware cache_key normalization; `FastAPI(dependencies=[...])` app-level deps; registration-time scope-incompatibility check (request-scope yield-dep can't depend on function-scope yield-dep); Response-in-return-annotation → FastAPIError; `PydanticV1NotSupportedError` for pydantic.v1 anywhere in route annotations; `Annotated[..., Path/Query(default=...)]` ambiguity assertions; multi-Param constraint merge (`Annotated[int, Query(gt=2), Query(lt=10)]`); absolute Location URLs on 307 redirect_slashes; middleware-receive HTTPException catch; `_wire_json` jsonable-encoder fallback (UUID, datetime, BaseModel, Decimal, Path, set, bytes — and asyncpg-style LYING UUID classes via str()); method-aware body inference (parameterized container on POST/PUT/PATCH is body, on GET stays query); fastapi.param_functions + fastapi.routing swap surfaces; ORJSONResponse / UJSONResponse pass-through.

Public surface added this milestone:
- `lancelot.canonical.dumps_canonical(obj)` → stable JSON bytes for KV-cache stability
- `lancelot.context.request_id` ContextVar (+ `X-Request-ID` echo on incoming, CRLF-sanitized, 128-char bounded)
- `App(max_concurrent_requests=N, drain_timeout=30.0, default_response_class=..., exception_handlers={ExcCls: fn}, strict_content_type=True)`
- `App.initiate_shutdown() / drain(timeout) / install_signal_handlers()`
- `App.add_exception_handler(ExcCls, fn)` / `@app.exception_handler(ExcCls)` decorator
- `@app.stream(stream_interval=N, sse_keepalive=S)`
- `@app.batch(batch_window_ms, max_batch, max_keys, key=...)`
- `@app.middleware("http")`
- `@app.tool(...) → tool_schemas()[i]["output_schema"]` auto-derived
- `Response.set_session_affinity(key)`
- Per-route `timeout=` kwarg → 504 on miss, yield-dep teardown preserved
- Per-route / per-router `strict_content_type=True/False/None` (inherits)
- `@app.on_event("startup"/"shutdown")` (deprecated, emits FastAPIDeprecationWarning)
- `App.add_exception_handler(ExcCls, fn)` / `@app.exception_handler(ExcCls)` decorator
- TRACE HTTP method on App + APIRouter
- `Depends(dep, scope="function")` — yield-dep teardown runs BETWEEN handler-return and response-emit (vs default "request" = after the response body)
- `FastAPI(dependencies=[Depends(...)])` — app-level deps prepended to every route
- Magic-inject types: `Request`, `BackgroundTasks`, `WebSocket`, `Response`, `SecurityScopes`

## Tier-D — explicit DO-NOT-DO list (no-bullshit anchor)

These are tempting; the research teardowns + the no-bullshit rule say not to build them. If a future agent (or human) is about to spend cycles on one of these, **stop and check this list first.**

- ❌ **Custom JSON serializer (orjson / ujson)** — Pydantic v2 `TypeAdapter.dump_json` is already Rust-core fast. FastAPI's own ledger marks orjson/ujson responses deprecated.
- ❌ **OpenAPI callbacks / webhooks** — rare, complex, no payoff for AI/cloud/startup positioning.
- ❌ **Dependency-overrides per-request rebuild** — useful only for tests; the simple override-by-`call` we ship is sufficient.
- ❌ **Built-in CLI (`lancelot run …`)** — FastAPI itself delegates to `fastapi-cli` (separate package). Users invoke uvicorn / hypercorn or our future Rust runner. Not core.
- ❌ **Replacing AI logic** (agents, vector stores, prompt templates) — that's user-side. Lancelot owns transport. Period.
- ❌ **Robyn-style hard-kill shutdown** — explicit anti-goal. We do graceful drain (B1).
- ❌ **Per-chunk Pydantic validation on streams** — streaming is raw-bytes + lightweight typing; any path that re-validates per chunk is a perf cliff and should be killed.

## Where to look for things

- Plan + roadmap: `docs/plans/M2-M3-PLAN-FROM-RESEARCH.md`
- Research distillations: `researchfms/{robyn,fastapi,blazeio,core,teardowns}/` (the `.git` inside this dir is OFF-LIMITS)
- Conformance harness: `conformance/{classify.py,ratchet.py,runner.py,conftest.py}`
- Floor: `conformance/parity_floor.txt` — passes=876
- Internal regression suites: `tests/test_m2_tier_{a,b,c,c5_batch}.py`

## Build env

- Python 3.13.13, venv at `.venv/`
- Cargo at `$HOME/.cargo/bin/cargo` (not on PATH by default — call by full path)
- pyo3 0.27.2 + hyper 1 + tokio + matchit + socket2 (+ libc only on Linux for the TCP knobs)
- maturin builds the cdylib; `cargo check` works on the pure-Rust crate too
