# Deploying Lancelot

Two deployable surfaces:

1. **`lancelot.App` under uvicorn** — the FastAPI-parity Python framework (99.6%
   conformance). Deploy exactly like FastAPI: `uvicorn yourmod:app --workers N`
   (uvicorn forks N workers that share one inherited socket via `fork`+`accept`,
   which load-balances on **every** platform). Use `--loop uvloop --http httptools`
   for the production stack. This is the right default for most apps.

2. **The Rust `_native` server** (`lancelot._native.run(routes, host, port)`) —
   the hyper+tokio transport. Its throughput wins are **large-payload
   serialization** and **streaming** (see `benchmarks/README.md`); for tiny
   one-shot JSON, uvicorn+uvloop is faster. Deploy notes below.

## Scaling the Rust server: MULTIPROCESS, not multi-thread

The Rust server **anti-scales with tokio worker threads** — Python execution is
GIL-serialized through one asyncio loop per process, so extra threads only add
GIL contention. The scaling model is **N processes × 1 worker thread each**:

```bash
# one process per core, each pinned to a single tokio worker
for i in $(seq 1 "$(nproc)"); do
  LANCELOT_WORKERS=1 python -m yourmod_runner &   # calls lancelot._native.run(...)
done
```

How the N processes share the port differs by platform:

- **Linux — `SO_REUSEPORT` (built in).** Each process binds the same `host:port`;
  the kernel load-balances incoming connections across them (4-tuple hash). This
  is the intended production model. `bind_reuseport` sets `SO_REUSEPORT` for you.
- **macOS / BSD — `SO_REUSEPORT` does NOT load-balance.** Multiple sockets may
  bind, but the kernel delivers connections to **one**. For local multi-core on
  macOS, run each process on a **distinct port** behind a load balancer
  (nginx / HAProxy / Caddy), or just develop single-process. (This is why the
  benchmark's macOS multiprocess numbers are a no-distribution *floor*, not the
  Linux ceiling.)

## TLS

The Rust server speaks **cleartext HTTP/1.1 only** (deliberate — see CLAUDE.md).
Terminate TLS at a reverse proxy / load balancer in front of it (the standard
cloud shape). h2/h2c is not served.

## Compression

Don't reach for a custom encoder. For `lancelot.App` (under uvicorn), add
Starlette's gzip middleware — identical to FastAPI:

```python
from starlette.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=1000)
```

For the Rust `_native` server, let the TLS-terminating proxy in front compress
(nginx `gzip on;`, Cloudflare, etc.) — that's the standard cloud shape and
avoids spending app CPU on what the edge does better.

## Operator knobs (env)

| Var | Default | Meaning |
|---|---|---|
| `LANCELOT_WORKERS` | logical CPUs (cgroup-aware) | tokio worker threads. **Set to `1` for the multiprocess model.** |
| `LANCELOT_MAX_CONNECTIONS` | 1024 | concurrent-connection cap (backpressure, not crash, when exceeded). Match to the container FD limit. |
| `LANCELOT_HEADER_TIMEOUT_MS` | 15000 | slow-loris deadline: drop a client that dribbles request headers past this. |
| `LANCELOT_DRAIN_SECS` | 10 | graceful-shutdown drain: on SIGTERM, finish in-flight requests up to this long before exiting (0 = exit immediately). |
| `LANCELOT_STREAM_BUFFER` | 256 | streaming channel depth (backpressure vs throughput; see `benchmarks/README.md` for the knee). |

## Graceful shutdown

Send **SIGTERM**. The server stops accepting, then drains in-flight connections
(bounded by `LANCELOT_DRAIN_SECS`) so in-flight requests finish and streaming
generators' `finally`/`aclose` run — it does NOT hard-kill (validated:
`tests/test_m4_resilience_load.py::test_v1_4_*`). Send a second SIGTERM / SIGKILL
to force-exit.

## Observability / distributed tracing

`lancelot.App` reads an incoming W3C `traceparent` and exposes its trace-id via
`lancelot.context.trace_id.get()` — include it in your log lines / metric labels
to correlate this service into the caller's distributed trace. `TransportPool`
forwards the `traceparent` onto outbound LLM calls automatically, so the upstream
joins the same trace. This is lightweight, no-dep context propagation; for full
span export, add OpenTelemetry (`opentelemetry-instrumentation-asgi`) yourself —
the two compose (we don't reimplement spans). RED metrics auto-emit when
`opentelemetry-api` is installed (see `lancelot/metrics.py`).

## Health checks

`lancelot.App` auto-registers `/livez` (process alive) and `/readyz` (lifespan
started + in-flight under threshold) for Kubernetes probes.

On the Rust `_native` server, serve fixed-response endpoints (health probes,
static config, cached payloads) via the **const-bytes tier** — prebaked bytes
returned straight from Rust with zero GIL / Request alloc / handler call
(~183k req/s per-core vs ~66k for uvicorn+uvloop on the same payload):

```python
lancelot._native.run(
    routes,                       # your handler routes
    host="127.0.0.1", port=8000,
    const_routes=[                # (method, path, body, content_type, status)
        ("GET", "/livez",  b'{"status":"ok"}', "application/json", 200),
        ("GET", "/readyz", b'{"status":"ok"}', "application/json", 200),
    ],
)
```
