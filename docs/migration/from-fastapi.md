# Migrating from FastAPI to Lancelot

Lancelot's byte-parity with FastAPI is 99.6% (974/978 of FastAPI's own test suite). For most apps the migration is **a single-line import swap**. This page covers (a) the swap, (b) the four behaviors that DO differ (and why), (c) deploy recipes for Render and VPS, and (d) what NOT to expect.

## TL;DR — the import swap

For most apps:

```diff
- from fastapi import FastAPI, Depends, HTTPException, Request, Response
- from fastapi.responses import StreamingResponse, JSONResponse
+ from lancelot import App as FastAPI
+ from lancelot import Depends, HTTPException
+ from lancelot.requests import Request
+ from lancelot.responses import Response, StreamingResponse, JSONResponse
```

If your code imports from `fastapi.routing`, `fastapi.security`, `fastapi.params`, etc., the equivalents live under `lancelot.*` — the `from fastapi import X` paths are covered by the surface adapter.

Run your test suite — if it passes, you're done. If it doesn't, the next section names every plausible reason.

## The four documented divergences

These are the **only** behaviors where Lancelot deliberately differs from FastAPI. Each was a conscious "no bullshit" decision, not an oversight.

### 1. `BaseHTTPMiddleware` streaming semantics

**FastAPI behavior:** `BaseHTTPMiddleware` (Starlette's anyio-task-group middleware) lets `call_next` return while the inner app keeps running. A user middleware can read global state set during the request *before* a request-scope yield-dep's teardown runs.

**Lancelot behavior:** Our middleware adapter is synchronous-capture. The same user middleware sees global state *after* teardown.

**Impact:** affects ONE FastAPI conformance test (`test_dependency_contextmanager::test_background_tasks`). If you rely on this ordering subtlety, file an issue — for 99.9% of apps the difference is invisible.

### 2. `response.json.dumps` mock-call expectation

**FastAPI:** the Starlette `JSONResponse.render` path calls `json.dumps`; some tests mock it and assert the call.

**Lancelot:** we serialize via pydantic-core (faster, no double-encode). `json.dumps` is never called.

**Impact:** if your test suite mocks `starlette.responses.json.dumps` and asserts the call, that assertion fails on Lancelot. The fix is to update the test (or trust the response bytes).

### 3. `docs_src/templates` cwd assumption

**FastAPI vendored test:** does `shutil.copytree("./docs_src/...")` assuming cwd = FastAPI repo root. The conformance harness runs from `conformance/`.

**Lancelot:** same. This is environmental, not behavioral — apps don't see it.

### 4. `a2wsgi` optional dependency

**FastAPI:** `fastapi.middleware.wsgi.WSGIMiddleware` requires the optional `a2wsgi` package.

**Lancelot:** we don't auto-import `a2wsgi`. If you use `WSGIMiddleware`, `pip install a2wsgi`.

## Subtleties that AREN'T divergences but trip people anyway

### `Depends(scope="function" | "request")`

Lancelot supports both yield-dep scopes (per FastAPI 0.115+). `scope="function"` teardown runs BETWEEN handler-return and response-emit; `scope="request"` (the default) runs AFTER the body. Identical to FastAPI. Don't change anything.

### Background tasks ordering

Background tasks fire after the response. Identical to FastAPI.

### `@app.on_event("startup"/"shutdown")`

Supported (emits the same FastAPIDeprecationWarning FastAPI does). Prefer the `lifespan` context manager for new code.

## New surface Lancelot adds (opt-in only)

Three primitives that don't exist in FastAPI; using them is opt-in:

```python
@app.batch("/lookup", batch_window_ms=50, max_batch=16, key=lambda it: it.k)
async def lookup(items: list[Item]) -> list[dict]:
    """Coalesces concurrent same-key requests into ONE handler call."""
    ...

@app.stream("/v1/chat/completions", stream_interval=1, sse_keepalive=15.0)
async def chat() -> AsyncIterator[lancelot.stream.Event]:
    """SSE-shaped streaming with framework-owned framing + keepalive."""
    ...

from lancelot.canonical import dumps_canonical
# Byte-stable JSON serialization for KV-cache-stable prompt prefixes.
body = dumps_canonical({"messages": [...]})
```

You don't have to use any of these. They're tools for AI/cloud workloads.

## Deploy — Render (the easy-onboarding path)

If your existing FastAPI app already deploys to Render via Docker, Lancelot drops in. Use the same Dockerfile, swap the import, redeploy.

For a reference deployment of the M5 exit-gate workload (the official validation), see [`benchmarks/m5/exit_gate/render.yaml`](../../benchmarks/m5/exit_gate/render.yaml) + the Dockerfile next to it.

```yaml
# render.yaml — minimum viable Lancelot service
services:
  - type: web
    name: my-app
    runtime: image
    dockerfilePath: ./Dockerfile
    plan: starter
    healthCheckPath: /healthz
    envVars:
      - key: PORT
        value: "8000"
```

The Dockerfile must build the Lancelot wheel via maturin (Rust toolchain in the builder stage). The M5 exit-gate `Dockerfile` is a working template.

## Deploy — VPS multiprocess `SO_REUSEPORT`

Lancelot's Rust server **anti-scales with tokio threads** (one Python GIL per process). The documented production model is **multiprocess via `SO_REUSEPORT`**, one Lancelot process per core, kernel-distributed load.

**Critical platform caveat:** `SO_REUSEPORT` load-balancing is **Linux-only**. macOS/BSD will bind N sockets but deliver to ONE — the scaling curve is single-process-floor, not the Linux ceiling. Deploy to Linux.

A working recipe (Ubuntu 22.04/24.04, systemd, nginx TLS termination) lives in [`benchmarks/m5/exit_gate/deploy_vps.sh`](../../benchmarks/m5/exit_gate/deploy_vps.sh) + [`nginx.conf`](../../benchmarks/m5/exit_gate/nginx.conf). For N processes per core:

```ini
# /etc/systemd/system/my-app@.service  (template; N instances)
[Service]
Environment="LANCELOT_WORKERS=1" "PORT=8910"
ExecStart=/opt/my-app/.venv/bin/python /opt/my-app/main.py
```

```bash
sudo systemctl enable --now my-app@{1..8}.service
```

Then nginx upstream with all 8 instances, or rely on `SO_REUSEPORT` if all instances bind the same port.

## What NOT to expect

These are honest concessions — Lancelot is positioned around AI/cloud byte-path wins, not blanket "faster than FastAPI everywhere."

- **No tiny-one-shot throughput win.** uvicorn+uvloop beats us on small JSON `/echo` responses (we pay per-request FFI overhead). Our wins are mid-payload (>~4 KB JSON), streaming TTFT, batch coalescing, and the prebaked-bytes const tier for fixed responses (e.g. `/healthz`).
- **No Windows support.** Linux + macOS are the supported platforms. Windows is Tier-D in the project plan.
- **No built-in CLI (`lancelot run …`).** Use `uvicorn`, `hypercorn`, or `lancelot._native.run` directly. FastAPI itself delegates to a separate `fastapi-cli` package.
- **HTTP/1.1 only.** No HTTP/2 cleartext (h2c) on the Rust core. Terminate TLS+HTTP/2 at the proxy (nginx, Caddy, Cloudflare); the proxy speaks HTTP/1.1 to Lancelot.
- **`@app.stream` per-chunk overhead is ~6× higher than raw Starlette** at the max chunk rate (~14k vs ~128k chunks/sec). Real LLM streaming is upstream-rate-limited (~50-200 tok/s/stream), so the framework is never the bottleneck. We optimized for that real-world shape, not the microbench.

## Observability

Lancelot emits four OpenTelemetry metrics out of the box (no config needed; `pip install opentelemetry-sdk opentelemetry-exporter-otlp` to wire to your collector):

| Metric | Type | Use |
|---|---|---|
| `lancelot.http.requests` | counter | request count by `route`, `method`, `status` |
| `lancelot.http.request.duration` | histogram (sec) | RED duration panel; p50/p95/p99 |
| `lancelot.http.requests.in_flight` | up-down counter | in-flight gauge |
| `lancelot.http.stream.ttft` | histogram (sec) | streaming time-to-first-byte (the AI-streaming UX metric) |

A pre-built Grafana dashboard lives at [`docs/operator/dashboards/lancelot-red.json`](../operator/dashboards/lancelot-red.json) — import it directly.

For `traceparent` propagation, set `app.transport_pool` (see `lancelot.llm`); the propagation is automatic. Request-ID echoes via `X-Request-ID` (CRLF-sanitized, 128-char bounded).

## When something doesn't migrate cleanly

1. Read the error carefully — most "Lancelot bug" reports are actually subtle pydantic v2 differences (FastAPI 0.115+ is on pydantic v2; if your codebase still uses v1 patterns, they break on FastAPI itself too).
2. Check the [conformance ratchet](../../conformance/) — if a FastAPI test exists for the behavior, it's probably passing on Lancelot.
3. Run the test suite (`pytest tests/`) to confirm the regression is in YOUR code path, not ours.
4. File an issue with: minimal repro, the FastAPI behavior expected, the Lancelot behavior observed.

The 4 documented divergences above are the **only** known cases of legitimate behavior difference. Everything else is a bug we'd like to fix.
