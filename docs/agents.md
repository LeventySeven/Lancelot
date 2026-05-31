# Lancelot — conventions for LLM-driven coding agents

When Claude Code / Cursor / Aider / any code agent works in a Lancelot codebase, **read this first**. CLAUDE.md is project-wide engineering notes for humans + agents; this file is the **conventions for writing Lancelot code** that a code agent should honor.

Source-of-truth for divergences from FastAPI: [`docs/migration/from-fastapi.md`](./migration/from-fastapi.md).

## TL;DR — the 5 things to internalize

1. **Lancelot is FastAPI-compatible.** All `from fastapi import …` patterns work with `from lancelot import …`. The migration cookbook has the import-swap diff.
2. **`response_model=None` for streaming.** Routes returning a `StreamingResponse` (or any `Response` subclass) need `response_model=None` if you also use a return-type annotation. (M5 fixture-test finding.)
3. **`@app.stream` over manual `StreamingResponse`** for long-running streams that need keepalive — Lancelot owns SSE framing + the keepalive ping cadence.
4. **`@app.batch` for rate-limited LLM upstreams.** When N concurrent handlers all hit the same upstream provider, decorate with `@app.batch(...)` to coalesce same-key requests into one upstream call.
5. **Tier-D: AI logic stays in your handler.** Lancelot owns HTTP transport / serialization / streaming / cancellation. Agent loops, prompt orchestration, vector stores, LangGraph: write them in handlers, don't ask Lancelot to absorb them. See "Anti-patterns" below.

## Handler shapes — what to prefer

### Async over sync
```python
# Prefer:
@app.get("/items")
async def list_items() -> list[Item]: ...

# Avoid (unless CPU-bound):
@app.get("/items")
def list_items() -> list[Item]: ...
```
Sync handlers run on the bounded blocking pool (M1-P5). They work, but async handlers pay no thread-hop cost.

### Explicit response type annotation
```python
# Good:
@app.get("/users/{user_id}")
async def get_user(user_id: int) -> User: ...

# Acceptable but loses OpenAPI fidelity:
@app.get("/users/{user_id}")
async def get_user(user_id: int): ...
```

### Streaming: pick the right primitive

```python
# Real-time stream of tokens / SSE — use @app.stream:
@app.stream("/v1/chat/completions")
async def chat() -> AsyncIterator[lancelot.stream.Event]:
    async for token in upstream:
        yield lancelot.stream.Event(data=token)

# One-off byte stream (file download, blob) — StreamingResponse + response_model=None:
@app.post("/download", response_model=None)
async def download() -> StreamingResponse:
    return StreamingResponse(file_iter(), media_type="application/octet-stream")
```

`@app.stream` owns the SSE framing + the keepalive (default 15 s ping; pass `sse_keepalive=0` to disable for high-rate streams that don't need it). `StreamingResponse` is the FastAPI-compat path for non-streaming-LLM use cases.

### LLM-upstream calls — use `TransportPool`

```python
from lancelot.llm import TransportPool

pool = TransportPool()  # one per app

@app.post("/v1/chat/completions")
async def chat(request: Request) -> StreamingResponse:
    body = await request.body()
    async def gen():
        async with pool.stream("POST", "https://api.openai.com/v1/...", content=body) as r:
            async for chunk in r.aiter_raw():
                yield chunk
    return StreamingResponse(gen(), media_type="text/event-stream")
```

`TransportPool` propagates W3C `traceparent`, respects the request's cancel event (M4 V2.2 — client disconnects propagate to cancel the upstream call), and pools connections per host.

### Batch coalescing for rate-limited upstreams

```python
from pydantic import BaseModel

class Lookup(BaseModel):
    k: str
    n: int

@app.batch("/api/lookup", batch_window_ms=50, max_batch=16,
           key=lambda it: it.k)
async def batched(items: list[Lookup]) -> list[dict]:
    # ONE upstream call covers the whole batch.
    results = await call_upstream_with(items)
    return [{"ok": it.n, "k": it.k} for it in items]
```

Lancelot collects same-key items within `batch_window_ms` (or until `max_batch` hits) and invokes your handler ONCE with the batch. Validated under M4 V2.1; ~12.7× throughput vs solo per the M5 batch bench.

## Anti-patterns — don't write these

### ❌ Per-chunk Pydantic validation on streams
```python
# DON'T:
@app.stream("/v1/chat/completions")
async def chat() -> AsyncIterator[TokenModel]:
    async for chunk in upstream:
        yield TokenModel.model_validate_json(chunk)  # per-chunk Pydantic = perf cliff
```
Stream raw bytes / strings. If you need structured validation, do it at the chunk boundary your client cares about, not per token.

### ❌ Multi-AI-gateway routing inside the framework
```python
# DON'T (this is what frameworks like Portkey do — it's user-side, not Lancelot's job):
@app.post("/llm")
async def route_to_model():
    if cost_budget_remaining > 100:
        return await openai.chat(...)
    else:
        return await anthropic.chat(...)
```
Build that pattern in your handler. Lancelot's M5/M6/M7 transcripts (Parahelp, Magic Patterns, Giga) all confirm: model routing logic belongs in product code, not framework.

### ❌ Sync I/O in async handlers
```python
# DON'T (blocks the event loop):
@app.get("/data")
async def get_data():
    with open("file.txt") as f:
        return f.read()

# DO (offload to thread):
@app.get("/data")
async def get_data():
    return await asyncio.to_thread(_read_file)
```

### ❌ Per-request connection allocation
```python
# DON'T (creates a new connection per request):
@app.get("/upstream")
async def upstream():
    async with httpx.AsyncClient() as c:
        return await c.get("https://api.example.com/...")

# DO (one client per app, lifespan-managed):
@app.on_event("startup")
async def _start():
    app.state.client = httpx.AsyncClient()

@app.get("/upstream")
async def upstream():
    return await app.state.client.get("https://api.example.com/...")
```
(Or use `TransportPool` for LLM upstreams; it manages this for you.)

> **Deploy caveat:** `@app.on_event`/`lifespan=` (and `@app.lifetime_task`) run
> only under an ASGI server (`uvicorn yourmod:app`). The native `app.run()`
> server does **not** drive the ASGI lifespan protocol, so this startup hook
> never fires there and `app.state.client` stays unset → first request raises
> `AttributeError`. Deploy under uvicorn if you rely on startup/shutdown hooks
> (see `docs/deploy.md`). `TransportPool` lazily initializes on first use, so it
> works on both paths.

### ❌ `BaseHTTPMiddleware` for streaming-sensitive paths
Lancelot's `BaseHTTPMiddleware` adapter does not replicate Starlette's anyio-task-group streaming model (one of the four documented divergences). If your middleware needs to observe `call_next` *while* the inner handler is still streaming, don't use `BaseHTTPMiddleware` — use a plain `@app.middleware("http")` decorator on a sync-capture function.

## Where to put what

| Surface | Module |
|---|---|
| Route handlers | `app.py` or `routers/*.py` (use `lancelot.APIRouter`) |
| DI dependencies | `deps.py` |
| Pydantic models | `models.py` |
| Middleware | top of `app.py` (just after route registration) |
| Lifespan startup/shutdown | `lifespan.py` (or inline if small) |
| Upstream LLM clients | `pool = lancelot.llm.TransportPool()` at module level; reuse across handlers |
| Tests | `tests/test_*.py`; use `lancelot.testclient.TestClient` |

## How to run tests

```bash
# Single test file:
.venv/bin/python -m pytest tests/test_my_app.py -q

# Full suite + conformance:
.venv/bin/python -m pytest tests/ -q
make m1-conformance         # FastAPI byte-parity ratchet (≥974 passing)

# Rust gate (if Rust changes):
$HOME/.cargo/bin/cargo clippy --release --all-targets -- -D warnings
PATH="$HOME/.cargo/bin:$PATH" .venv/bin/maturin develop --release
```

## Deploy recipes (quick links)

- **Render:** [`benchmarks/m5/exit_gate/render.yaml`](../benchmarks/m5/exit_gate/render.yaml) — Docker-based, multi-service blueprint.
- **VPS (Ubuntu 22.04/24.04):** [`benchmarks/m5/exit_gate/deploy_vps.sh`](../benchmarks/m5/exit_gate/deploy_vps.sh) — idempotent provisioning with systemd + nginx + optional certbot.
- **Multiprocess scaling:** `LANCELOT_WORKERS=1` per process, N processes via `SO_REUSEPORT` (Linux only — see [`docs/deploy.md`](./deploy.md)).

## Observability

Lancelot emits four OpenTelemetry metrics out of the box:

| Metric | Use |
|---|---|
| `lancelot.http.requests` | request count |
| `lancelot.http.request.duration` | RED duration histogram |
| `lancelot.http.requests.in_flight` | concurrency gauge |
| `lancelot.http.stream.ttft` | streaming time-to-first-byte (the AI metric) |

Grafana dashboard: [`docs/operator/dashboards/lancelot-red.json`](./operator/dashboards/lancelot-red.json).

## When things go wrong

- **Slow handler / stuck drain / leak suspect / batch flood / TTFT regression / 413 flood / connection cap / slow-loris:** see [`docs/operator/runbook.md`](./operator/runbook.md). Each has signal + first-action + fix + escalation.
- **All known failure modes at the edges:** [`docs/operator/failure-modes.md`](./operator/failure-modes.md). Single-page synthesis with metric signatures + recovery actions.

## What to NOT modify

These files are sha256-verified or scope-locked — don't edit them unless you know exactly why:

- `conformance/vendor/fastapi/` — sha256-verified against the FastAPI 0.136.1 sdist. The conformance ratchet validates byte-parity. **Never** edit the vendored tests.
- `researchfms/` — separate nested research repo. Use `git -C` to scope git operations away from the outer Lancelot repo (CLAUDE.md cardinal rule #5).
- `conformance/parity_floor.txt` — current floor `passes=974`. Don't lower; raising requires the ratchet pipeline.

## What changes when

- **Streaming behavior changed?** Update both `lancelot/asgi.py` (the ASGI dispatch path) AND the native rust path in `src/dispatch.rs` / `src/stream.rs`. They diverge silently if you forget.
- **New OTel metric?** Add to `lancelot/metrics.py`, update the Grafana dashboard (`docs/operator/dashboards/lancelot-red.json`), document in the migration cookbook's observability section.
- **New decorator / app surface?** Add to `lancelot/__init__.py`'s `__all__` (lazy in `__getattr__` if it pulls a heavy dep — runtime guard: only `pydantic>=2.7` is a runtime dep).
- **New cargo dep?** Re-run `$HOME/.cargo/bin/cargo clippy --release --all-targets -- -D warnings` before commit; the gate is RED-on-warning.

## The cardinal rule for AI-built code

**Validate under real load, not just unit tests.** Four headline M4 features shipped with green unit tests AND broke under real load (R3 streaming pump, `@app.batch` self-cancel, graceful drain, C2 cancellation). Two more M5 features had the same issue (the streaming regression that M6 T1.1 just fixed; the orjson gap M6 T1.2 closed). Any transport/streaming/resilience change gets a real-server test (subprocess + HTTP) that exercises the path the bug would live on (the *awaiting* handler, the *flooded* cap, the *disconnecting* client). If a test ships without that, it's not done.

## Cross-references

- [`CLAUDE.md`](../CLAUDE.md) — engineering notes; positioning lock; cardinal rules; current-milestone state.
- [`docs/migration/from-fastapi.md`](./migration/from-fastapi.md) — the import-swap diff + the four documented divergences.
- [`docs/operator/{runbook,failure-modes}.md`](./operator/) — operator playbook + failure-mode table.
- [`docs/plans/M6-PLAN-SPEED-STABILITY-ADOPTION.md`](./plans/M6-PLAN-SPEED-STABILITY-ADOPTION.md) — current milestone.
