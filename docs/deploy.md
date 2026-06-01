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

> **Lifecycle hooks run on BOTH surfaces (M18).** The native `app.run()` server
> now **drives the ASGI lifespan protocol**: `lifespan=`, `@app.on_event`,
> `@app.lifetime_task`, and mounted sub-app lifespans all run, on the server's
> own event loop. Startup runs **before the socket binds** — a startup that
> raises **refuses to serve** (the process exits non-zero without ever binding,
> matching uvicorn's `startup.failed`). Shutdown runs **after** the SIGTERM
> connection drain and is bounded by `LANCELOT_DRAIN_SECS` (a hung user shutdown
> can't wedge process exit). Disable with `LANCELOT_LIFESPAN=0`.
>
> **What the native server still does NOT do: dependency injection and
> request-body parsing.** This is lifespan-only — handlers under `app.run()`
> receive a raw `lancelot.Request`, not FastAPI-style injected dependencies or a
> parsed/validated body. For full FastAPI parity (DI + body parsing + validation
> error responses), deploy under uvicorn (surface #1). Also unavoidable: SIGKILL
> (not SIGTERM) skips shutdown, and a single `app.run()` is one process (scale
> multiprocess — see below).

## Containers & PaaS — bind address ($PORT, 0.0.0.0)

Every PaaS injects a `$PORT` and expects you to bind `0.0.0.0:$PORT`. The two
deploy commands:

```bash
# FastAPI-parity path — works on EVERY ASGI platform (recommended default):
uvicorn yourmod:app --host 0.0.0.0 --port $PORT --workers $WEB_CONCURRENCY \
        --loop uvloop --http httptools

# Native Rust server path (large-payload + streaming wins). app.run() reads the
# bind from env, so this needs no flags:
#   LANCELOT_HOST=0.0.0.0   (set it in the container; default is 127.0.0.1)
#   PORT / LANCELOT_PORT    (PORT is the platform's; LANCELOT_PORT overrides)
python yourmod.py     # where yourmod.py ends with `app.run()`
```

`app.run()` / `lancelot.run(app)` resolve `host`/`port` from env (explicit args
win): `LANCELOT_HOST` → else `127.0.0.1`; `LANCELOT_PORT` → `PORT` → else `8000`.
Bare `$HOST` is **ignored on purpose** (shells/PaaS often set it to the machine
hostname). The default host stays loopback (`127.0.0.1`) so you don't
accidentally expose a dev server — **you must set `LANCELOT_HOST=0.0.0.0` for a
container to receive traffic** (the Dockerfile below does this).

## Docker

Lancelot ships **prebuilt abi3 wheels** (manylinux x86_64/aarch64, macOS,
Windows — one wheel per arch covers Python 3.11+). So a production image is the
same slim, Rust-free image you'd write for FastAPI — `pip install lancelot`
pulls the compiled extension, no Rust toolchain in the build:

```dockerfile
FROM python:3.13-slim
WORKDIR /app
# Prebuilt wheel — no Rust toolchain needed in the image.
RUN pip install --no-cache-dir lancelot "uvicorn[standard]"
COPY . .
ENV LANCELOT_HOST=0.0.0.0
# FastAPI-parity path (portable, load-balances via --workers):
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-1} --loop uvloop --http httptools"]
# …or the native server (app.py ends with `app.run()`):  CMD ["python", "app.py"]
```

Notes that make this image production-clean:
- **Worker count is cgroup-aware.** The native server reads the container's CPU
  quota (cgroup v2 `cpu.max`), not the host's core count — no thread explosion
  when the platform caps you (the failure mode that bites `os.cpu_count()`-based
  servers in containers). Override with `LANCELOT_WORKERS`.
- **`SIGTERM` is graceful.** `docker stop` / a rolling deploy sends SIGTERM; the
  server stops accepting and drains in-flight requests (bounded by
  `LANCELOT_DRAIN_SECS`, default 10) — no dropped requests, no hard-kill.
- **Lean import.** `import lancelot` pulls only `pydantic` (no httpx / starlette
  / otel at import) → fast cold start, small layer.
- **Scaling:** one process per core is the model — on Linux the native server
  uses `SO_REUSEPORT`; with uvicorn use `--workers N`. (See multiprocess section.)

If you'd rather build from source (bleeding edge / a platform without a matching
wheel), add a builder stage: `FROM rust + maturin` → `maturin build --release`,
then `pip install` the wheel in the slim runtime stage.

## Platform recipes (honest matrix)

**First-class — container / long-running-process PaaS** (Lancelot runs as a
normal ASGI/HTTP server; deploy is identical to FastAPI):

| Platform | How |
|---|---|
| **Render / Railway / Fly.io / Heroku / DigitalOcean App Platform** | Push the Dockerfile above, or set the start command to the `uvicorn … --port $PORT` line. They inject `$PORT`; set `LANCELOT_HOST=0.0.0.0`. |
| **Google Cloud Run** | Container image above. Cloud Run sets `$PORT` (8080); listen on `0.0.0.0:$PORT`. One process; let Cloud Run scale horizontally. |
| **AWS (ECS / Fargate / App Runner / Elastic Beanstalk) / GKE / any Kubernetes** | Same container image. Front it with the platform's load balancer (terminates TLS — the server is cleartext HTTP/1.1 by design). Use `/livez` + `/readyz` for probes. |

**Serverless / edge — possible but a poorer fit for ANY persistent server**
(be honest with yourself before reaching for these):

- **AWS Lambda:** works via an ASGI→Lambda adapter (`mangum`) + a container
  image or a layer carrying the manylinux wheel. Same as running FastAPI on
  Lambda. The native Rust server is not for Lambda (no persistent socket); use
  `app` (the ASGI callable) through Mangum.
- **Vercel / Netlify:** these are optimized for JS/edge functions; their Python
  function runtimes are constrained and not built for a compiled-extension ASGI
  server. If you must, use their container/serverless-function path with the
  ASGI adapter — but a container PaaS above is the right home for a Lancelot app.

**Not a host — a backend you connect TO:**

- **Supabase:** Supabase is Postgres + Auth + Edge Functions (Deno/TS). You don't
  *host* a Lancelot app on Supabase; you run Lancelot on a container PaaS above
  and **connect to** Supabase Postgres from your handlers. (Supabase Edge
  Functions are Deno, not a Python host.)

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
| `LANCELOT_UPSTREAM_RATE_LIMIT` | 0 (off) | outbound LLM calls/second cap (token bucket) on `TransportPool` — keeps bursty `fanout()` from tripping a shared API key's rate limit. Also `TransportPool(rate_limit=...)`. |
| `LANCELOT_UPSTREAM_STREAM_IDLE_SECS` | 0 (off) | per-stream inter-chunk idle deadline for `pool.idle_guarded_chunks()` — a stalled upstream SSE (no bytes, no close) is cut instead of holding the connection open (and blocking drain) forever. 60 is a sane production value. |

## Outbound LLM transport hardening (`lancelot.llm.TransportPool`)

- **SSRF guard** (opt-in): `TransportPool(block_private_ips=True)` validates every
  outbound URL via `lancelot.net.assert_safe_url` before any I/O — blocks
  private / loopback / link-local / CGNAT / the cloud-metadata endpoint
  (`169.254.169.254`), incl. IPv4-mapped-IPv6 and obfuscated literals. Default
  off (fixed-provider LLM calls are unaffected). Use it when a handler forwards
  a **user-supplied** URL (webhook delivery, "fetch this URL" tools).
- **Stalled-stream watchdog**: `async for c in pool.idle_guarded_chunks(r): …`
  raises `UpstreamIdleTimeout` if the upstream goes silent past the idle window.
- **Outbound rate cap**: `rate_limit=` (above) — orthogonal to `fanout(concurrency=)`
  (concurrency = burst width; rate = sustained calls/s).

## Webhooks (`lancelot.webhook` — stdlib, sign/verify only)

`sign(body, secret)` / `verify(body, headers, secret)` implement the Standard
Webhooks scheme (HMAC-SHA256 over `{id}.{ts}.{body}`, constant-time compare,
timestamp freshness, key-rotation). Sign outbound deliveries so the receiver can
trust them; verify inbound ones. This is the crypto only — delivery/retries are
yours (or the proxy's). Secret: `"whsec_" + base64.b64encode(os.urandom(32)).decode()`.

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
started + in-flight under threshold) for Kubernetes probes. As of M18 the native
`app.run()` server drives the lifespan, so the "lifespan started" gate is now
meaningful on BOTH surfaces. For the lowest-overhead probe on the native server
you can still prebake `/livez`/`/readyz` as const-bytes routes (zero GIL — see
below); a prebaked route OVERRIDES the auto-registered handler on `app.run()`.

On the Rust `_native` server, serve fixed-response endpoints (health probes,
static config, cached payloads) via the **const-bytes tier** — prebaked bytes
returned straight from Rust with zero GIL / Request alloc / handler call
(~183k req/s per-core vs ~66k for uvicorn+uvloop on the same payload). Register
them on the `App` with `app.const_route(...)` (forwarded by `App.run()`):

```python
app.const_route("GET", "/livez",  b'{"status":"ok"}', "application/json", 200)
app.const_route("GET", "/readyz", b'{"status":"ok"}', "application/json", 200)
app.run(host="0.0.0.0", port=8000)   # const routes ride through to the server
```

The low-level form (`lancelot._native.run(routes, host, port, const_routes=...)`,
where each entry is `(method, path, body, content_type, status)`) is still
available if you drive the native server directly. A const route OVERRIDES a
handler route on the same `(method, path)` for the native deploy.
