# Lancelot state: resilience / cloud / observability / DX inventory

Sourced from full reads of: `lancelot/security.py`, `auth.py`, `net.py`, `secrets.py`, `tenant.py`,
`ratelimit.py`, `fleet.py`, `status.py`, `settings.py`, `metrics.py`, `telemetry.py`,
`websockets.py`, `staticfiles.py`, `uploads.py`, `context.py`, `webhook.py`, `compress.py`,
`asgi.py` (targeted grep), `app.py` (targeted grep), `src/service.rs`.

---

## 1. Transport-layer resilience (Rust / `src/service.rs`)

All four Rust-side caps are read once per process via `std::sync::OnceLock` — zero hot-path cost.

| Knob | Default | Constant | file:line |
|---|---|---|---|
| `LANCELOT_MAX_BODY` | 16 MiB | `max_body()` | `src/service.rs:37-46` |
| `LANCELOT_MAX_CONNECTIONS` | 1024 | `max_connections()` | `src/service.rs:54-66` |
| `LANCELOT_MAX_INFLIGHT_BYTES` | 0 (off) | `max_inflight_bytes()` | `src/service.rs:78-87` |
| `LANCELOT_DRAIN_SECS` | 10 | `drain_timeout()` | `src/service.rs:105-117` |
| `LANCELOT_HEADER_TIMEOUT_MS` | 15 000 | `header_timeout_ms()` | `src/service.rs:121-133` |

**Body cap** (`LANCELOT_MAX_BODY`): hyper `Limited` wrapper — requests exceeding the cap get 413
before the body is fully buffered; clamp `[1 KiB, 1 GiB]`. Prevents remote memory-DoS (audit
CRITICAL #1).

**Connection cap** (`LANCELOT_MAX_CONNECTIONS`): Semaphore acquired before accept; kernel
backpressure rather than in-memory queue; clamp `[1, 1<<20]`. Validated under adversarial
load (M4 V1.3).

**In-flight bytes budget** (`LANCELOT_MAX_INFLIGHT_BYTES`): AtomicU64 aggregate across ALL
connections — the missing second axis beyond `max_connections × max_body`. RAII `InflightGuard`
releases atomically on drop. Shed = 503 + Retry-After, NOT queue-and-wait (deadlock-free).
Default OFF (zero cost). `src/service.rs:68-101`.

**Graceful drain** (`LANCELOT_DRAIN_SECS`): on SIGTERM, `acquire_many_owned(max_conns)` drains
until every in-flight connection releases its permit; NOT Robyn hard-kill. Validated under M4
V1.4 load. Consistent with Python-seam `LANCELOT_DRAIN_SECS` (one knob, shared). `src/service.rs:103-117`.

**Slow-loris guard** (`LANCELOT_HEADER_TIMEOUT_MS`): header-read deadline per connection;
defaults to 15 s (nginx/Granian class); clamp `[100 ms, 600 s]`. Validated M4 V1.1.

---

## 2. Python ASGI seam resilience (`lancelot/asgi.py`, `lancelot/app.py`)

**`App(max_concurrent_requests=N)`** — ASGI seam Semaphore cap; 503 on saturation.
**`App(drain_timeout=30.0)`** — Python-side graceful drain deadline.
**`App(json_max_depth=128)`** — constructor knob; checked at `asgi.py:765` via `exceeds_depth()`;
returns 400 on overly-nested JSON (DDoS protection).
**`LANCELOT_MAX_QUERY_PARAMS`** env — bounds query-param count (`asgi.py:31-34`).
**`LANCELOT_LIFESPAN`** — 0 disables lifespan (escape hatch); default ON (`app.py:1014-1018`).
**`LANCELOT_CONST_STRICT`** — hard error instead of warn when a const-route path has middleware
that would bypass the prebaked tier (`app.py:1180`).

### Health probes (`app.py:305-335`)
- `/livez` — zero-dep process-alive probe; MUST never touch DB/auth (const-tier safe).
- `/readyz` — traffic-ready; returns 503 + `Retry-After` once `initiate_shutdown()` flips the drain flag; 200 otherwise.
- `/fleet/status` — region + worker_id + status ("ok"/"draining") + in_flight + uptime_s + lancelot_version; auto-registered.
These three are always let through even when draining (`asgi.py:316-325`).

### Drain surface (`app.py:902-948`)
- `App.initiate_shutdown()` — flip the drain flag; /readyz → 503 immediately.
- `App.drain(timeout)` — wait for in-flight requests + lifetime tasks.
- `App.install_signal_handlers()` — SIGTERM + SIGINT → `initiate_shutdown()`.
- `_drain_lifetime_tasks()` — cancel + await background tasks bounded by `LANCELOT_DRAIN_SECS`.

---

## 3. Security / auth (`lancelot/security.py`, `lancelot/auth.py`)

### `security.py` — FastAPI-parity security scheme EXTRACTORS only
`APIKeyHeader`, `APIKeyQuery`, `APIKeyCookie`, `HTTPBearer`, `HTTPBasic`, `OAuth2`,
`OAuth2PasswordBearer`, `SecurityScopes` — these EXTRACT credentials and raise 401 + WWW-Authenticate.
They do NOT compare against a known set (no static-token checking here).

### `auth.py` — `StaticTokenGate` ASGI middleware (M17 P5)
**What it is:** opt-in pure-ASGI constant-time static-token gate. Fills the gap that `security.py` leaves.

Key mechanisms:
- `_digest(token)` → `hashlib.sha256(token.encode()).digest()` — tokens hashed at construction; plaintext not retained. `auth.py:49-52`.
- `_token_ok(presented)` — OR-loop over ALL digests, no early return; `hmac.compare_digest` on 32-byte digests. Leaks neither token value, length, nor match position. `auth.py:106-116`.
- `_is_guarded(path)` — reads `scope['path']` only, NEVER `request.url`/Host; Host-spoof safe. `auth.py:101-104`.
- `open_paths=("/livez", "/readyz")` — health probes always pass.
- `skip_options=True` — CORS preflight passes.
- Shed = 401 + WWW-Authenticate, NO DI entered. `auth.py:153-162`.

Knobs: `tokens`, `scheme`, `guard_prefixes`, `open_paths`, `skip_options`, `header`.

**LACKS:** JWT decode/validation/expiry/scope enforcement — explicitly out of scope ("user-side / lancelot.security"). No per-request key rotation — SIGHUP is secrets-level.

---

## 4. SSRF guard (`lancelot/net.py`)

`assert_safe_url(url)` / `is_safe_public_url(url)` — STDLIB ONLY, never imported on `import lancelot`.
Activated in `TransportPool(block_private_ips=True)`.

Checks:
- Scheme allowlist: only `http`/`https`. `net.py:28,84-86`.
- Hostname: `localhost` / `.localhost` hard-blocked. `net.py:91-93`.
- DNS resolution via `socket.getaddrinfo` — normalizes obfuscated literals (`0x7f000001` etc). `net.py:65-77`.
- ALL resolved IPs checked via `ipaddress`: `is_private`, `is_loopback`, `is_link_local`, `is_reserved`, `is_multicast`, `is_unspecified`. `net.py:56-62`.
- IPv4-mapped IPv6 (`::ffff:a.b.c.d`) unwrapped + re-checked. `net.py:54-55`.
- CGNAT (`100.64.0.0/10`) explicit check — the one stdlib flag gap. `net.py:31,60-62`.
- DNS cache: 30 s TTL (`LANCELOT_SSRF_DNS_TTL_SECS`); narrows DNS-rebinding TOCTOU window. `net.py:35-37,39`.

**LACKS:** pinned-IP transport (full rebinding protection) — documented as out of scope for the webhook/tool-fetch threat model.

---

## 5. Secrets management (`lancelot/secrets.py`, M7 T3.2)

Pluggable backends, ALL lazy-loaded:
- `EnvBackend` (default) — `os.environ.get`. `secrets.py:56-66`.
- `FileBackend` — one-file-per-secret; k8s volume pattern; **traversal guard** via `os.path.realpath` + `os.path.commonpath`. `secrets.py:76-108`.
- `VaultBackend` — HashiCorp Vault KV v2; lazy `hvac` import. `secrets.py:116-159`.
- `InfisicalBackend` — lazy `infisical_sdk`. `secrets.py:164-210`.
- `AwsSecretsManagerBackend` — lazy `boto3`. `secrets.py:215-246`.

API: `get(name)`, `configure(backend)`, `rotate()`, `install_sighup_handler()`, `set_audit_hook(fn)`.

Audit hook: called on every `get()` as `fn(name, found_bool)`; exception in hook silently swallowed (fail-safe). `secrets.py:283-288`.
SIGHUP handler: `signal.signal(SIGHUP, _handler)` calls `rotate()` → `backend.refresh()` clears cache. `secrets.py:302-328`.

---

## 6. Tenant concurrency limiter (`lancelot/tenant.py`, M7 T4.8)

`TenantConcurrencyLimiter` — pure-ASGI opt-in middleware.
- Per-tenant `asyncio.Semaphore` lazy-created on first sight. `tenant.py:96-99`.
- Tenant ID from configurable header (default `x-tenant-id`); anon = `_anon_`. `tenant.py:86-93`.
- `LANCELOT_TENANT_CONCURRENCY` env (default 10); 0 disables. `tenant.py:33-40`.
- Records queue wait time into `lancelot.context.queue_wait_ms` ContextVar; picked up by ASGI seam for `lancelot.http.queue_wait_ms` histogram. `tenant.py:101-116`.
- Dict grows unbounded by design — cardinality control is operator's job (use auth-pinned header, not raw user input).

---

## 7. Inbound rate limiter (`lancelot/ratelimit.py`, M17 P3)

`RateLimit` — pure-ASGI opt-in middleware. Token bucket. Shed = 429 + `Retry-After` + `RateLimit-Limit/Remaining/Reset`.

- Parse: `'100/minute'` → `(100, 60.0)`. `ratelimit.py:54-86`.
- `InProcessTokenBucketBackend` — per-PROCESS (no lock needed: GIL + single asyncio loop); dict of `[tokens, last_refill_monotonic]`. `ratelimit.py:113-151`.
- Key extractors: `'ip'` (`scope['client']`), `'api_key'` (X-API-Key → Authorization → fallback IP), or `callable(scope)->str`. `ratelimit.py:154-188`.
- Pluggable `TokenBucketBackend` for shared/global (Redis). Lancelot ships NO Redis dep. `ratelimit.py:89-111`.

**IMPORTANT caveat (explicit in module docstring):** per-PROCESS only by default. With W workers, effective global limit is W × configured. For a global cap, supply a shared `backend=`. `ratelimit.py:18-38`.

---

## 8. Fleet / deployment (`lancelot/fleet.py`, M7 T2.3)

`LANCELOT_REGION` (default `"default"`), `LANCELOT_WORKER_ID` (default PID). `fleet.py:32-47`.
`fleet_info(app)` → `{region, worker_id, status, in_flight, uptime_s, lancelot_version}`. `fleet.py:57-89`.
`install_otel_resource_attrs()` — patches OTel MeterProvider `_resource` with region + worker_id; best-effort, NOT auto-called. `fleet.py:100-125`.

---

## 9. RED metrics + observability (`lancelot/metrics.py`, `lancelot/context.py`)

### Instruments (lazy-bound on first OTel import)
- `lancelot.http.requests` — counter. `metrics.py:48-51`.
- `lancelot.http.request.duration` — histogram (s). `metrics.py:52-56`.
- `lancelot.http.requests.in_flight` — UpDownCounter. `metrics.py:57-59`.
- `lancelot.http.stream.ttft` — histogram (s); first-chunk time for streaming. `metrics.py:66-71`.
- `lancelot.http.cost.usd` — histogram (USD); per-request, only emitted when > 0. `metrics.py:72-80`.
- `lancelot.http.queue_wait_ms` — histogram (ms); tenant semaphore wait time. `metrics.py:81-92`.

### Attributes on every emit
`http.route` (template, NOT concrete URL — cardinality guard), `http.method`, `http.status_class`, `http.status_code`, `lancelot.tenant`. `metrics.py:148-158`.

### Cardinality controls
- `LANCELOT_TENANT_LABEL_MAX` (default 1000) — over-cap tenant labels → `_overflow_`. `metrics.py:100-135`.
- Route label: `"__not_found__"`, `"__method_not_allowed__"`, `"__unmatched__"` sentinels instead of sprayed URLs. `metrics.py:25-27`.

**SCOPE:** RED metrics fire ONLY on the Python ASGI seam. The Rust native server (`_native.run`) emits NO OTel metrics. `metrics.py:17-25`.

### ContextVars (`context.py`)
`request_id`, `principal`, `trace_id`, `trace_parent`, `cost_usd`, `tokens_in`, `tokens_out`, `agent_id`, `tenant_id`, `queue_wait_ms`. `context.py:19-105`.

W3C `traceparent` propagation: ASGI seam reads incoming header, stores `trace_id` + `trace_parent` in ContextVars; `TransportPool` forwards it on outbound LLM call → joins distributed trace. No OTel dep. `context.py:43-54`.

### Telemetry sink (`lancelot/telemetry.py`)
`Event(kind, attrs)` emitted via `set_sink(callable)`. Default = None (zero cost). For OTel GenAI semconv. `gen_ai.cost.usd` always None — Lancelot doesn't ship rates. `telemetry.py:14-68`.

---

## 10. Upstream resilience (`lancelot/llm.py` — `TransportPool`)

| Knob | Default | Description |
|---|---|---|
| `LANCELOT_UPSTREAM_MAX_CONNECTIONS` | 100 | httpx connection pool |
| `LANCELOT_UPSTREAM_MAX_KEEPALIVE` | 20 | keepalive connections |
| `LANCELOT_UPSTREAM_KEEPALIVE_EXPIRY` | 5.0 s | keepalive expiry |
| `LANCELOT_UPSTREAM_RETRY_BUDGET_MS` | 60 000 | wall-clock retry budget |
| `LANCELOT_UPSTREAM_RATE_LIMIT` | 0 (off) | token-bucket rate limit (req/s) |
| `LANCELOT_UPSTREAM_STREAM_IDLE_SECS` | 0 (off) | streaming idle timeout |

POST is NEVER retried (anti-cascade rule). Idempotent verbs retry on 5xx + 429, honoring `Retry-After`/`retry-after-ms`. `Idempotency-Key` stamped on retries (stable, re-used across retries). `llm.py:433-490`.

No circuit breaker — explicitly documented as omitted.

---

## 11. Webhook (`lancelot/webhook.py`, M14)

Standard Webhooks scheme (Render/Svix compatible). Stdlib only (hmac/hashlib/base64).
- `sign(payload, secret)` → `{webhook-id, webhook-timestamp, webhook-signature}`. `webhook.py:39-61`.
- `verify(payload, headers, secret, tolerance_s=300)` — constant-time compare; timestamp freshness window; multiple `v1,` signatures (key rotation). `webhook.py:65-100`.
- Empty-key fail-OPEN bug was caught + fixed: length check `_MIN_KEY=24` on both sign + verify. `webhook.py:54-55, 85-87`.
- Replay dedup on `webhook-id` is the caller's job.

---

## 12. GZip compression (`lancelot/compress.py`, M18 R1)

`GZipMiddleware` — opt-in ASGI. Gzip encode runs OFF-LOOP via `asyncio.run_in_executor(None, _native.gzip_encode, body, level)`. GIL released in Rust; loop free to serve concurrent requests. `compress.py:187-190`.

**Scope:** one-shot buffered bodies only. Streaming (`more_body=True`) passes through identity — Tier-D ban on per-chunk compression. `compress.py:170-175`.
**Applies ONLY to the ASGI (uvicorn) path.** Rust native server cannot host it. `compress.py:18-28`.
Win: `~2.2× RPS, ~12× p99, co-tenant tiny route ~161×` at `c=200/level-9/45 KB` vs Starlette (per `benchmarks/results/compress/SUMMARY.md`). Explained by the structural GIL serialization Starlette's sync zlib triggers.

---

## 13. WebSockets (`lancelot/websockets.py`)

Starlette-shape surface: `WebSocket.accept/receive_text/receive_bytes/receive_json/send_text/send_bytes/send_json/close`, `WebSocketDisconnect`, `WebSocketException`. Pure Python ASGI path (uvicorn). `websockets.py`.
**NO native Rust WebSocket path** — `src/ws/mod.rs` does not exist; confirmed in M19b recheck. `websockets.py:3-6`.

---

## 14. Static files (`lancelot/staticfiles.py`)

`StaticFiles(directory=...)` — ASGI app.
- **Path traversal guard:** `os.path.realpath + os.path.commonpath`. `staticfiles.py:73-85`.
- **Conditional GET:** ETag (`md5(mtime-size)`, non-security hash) + Last-Modified + `If-None-Match`/`If-Modified-Since` → 304. `staticfiles.py:87-97`.
- **Byte Range:** single-range 206/400/416; multi-range falls back to 200. `staticfiles.py:156-172`.
- **Off-loop read:** `asyncio.to_thread(_read)` — avoids stalling the reactor on large files. `staticfiles.py:147-151`.

---

## 15. Uploads / multipart (`lancelot/uploads.py`)

`UploadFile`: SpooledTemporaryFile with `spool_max_size=1 MiB` before disk spill. `uploads.py:17`.
`parse_multipart(body, ct, max_fields=1000, max_files=1000, max_part_size=16 MiB)` — DoS caps at part count AND per-part size. `uploads.py:56-68`. `python-multipart` lazy-imported. `uploads.py:70-75`.

---

## 16. Settings DI (`lancelot/settings.py`)

`settings_provider(cls, eager=False)` — `lru_cache(maxsize=1)` singleton; `eager=True` instantiates at boot so missing required env crashes at startup, not runtime. `settings.py:8-15`.

---

## 17. Status constants (`lancelot/status.py`)

Integer alias module mirroring `starlette.status` / `fastapi.status`. No behavior. `status.py`.

---

## Complete env-knob inventory (Python + Rust)

### Rust-native server (`src/service.rs`, `src/stream.rs`, `src/cpu.rs`, `src/lib.rs`)
| Knob | Default | Clamp |
|---|---|---|
| `LANCELOT_MAX_BODY` | 16 MiB | [1 KiB, 1 GiB] |
| `LANCELOT_MAX_CONNECTIONS` | 1024 | [1, 1 048 576] |
| `LANCELOT_MAX_INFLIGHT_BYTES` | 0 (off) | any u64 > 0 |
| `LANCELOT_DRAIN_SECS` | 10 | [0, 300] |
| `LANCELOT_HEADER_TIMEOUT_MS` | 15 000 | [100, 600 000] |
| `LANCELOT_STREAM_BUFFER` | 256 | [1, 4096] |
| `LANCELOT_WORKERS` | cgroup/logical | [1, 64] |
| `LANCELOT_LOOP` | uvloop if installed | `"asyncio"` to force stdlib |
| `LANCELOT_CONST_STRICT` | warn | `"1"/"true"` for hard error |

### Python ASGI seam / app
| Knob | Default | Source |
|---|---|---|
| `LANCELOT_HOST` | 127.0.0.1 | `app.py:1005` |
| `LANCELOT_PORT` / `PORT` | 8000 | `app.py:1007` |
| `LANCELOT_LIFESPAN` | 1 (on) | `app.py:1018` |
| `LANCELOT_DRAIN_SECS` | 30.0 (Python) | `app.py:985` |
| `LANCELOT_MAX_QUERY_PARAMS` | 1000 | `asgi.py:31-34` |
| `LANCELOT_TENANT_CONCURRENCY` | 10 | `tenant.py:33-40` |
| `LANCELOT_TENANT_LABEL_MAX` | 1000 | `metrics.py:100-114` |
| `LANCELOT_REGION` | "default" | `fleet.py:32-37` |
| `LANCELOT_WORKER_ID` | PID | `fleet.py:40-47` |
| `LANCELOT_SSRF_DNS_TTL_SECS` | 30.0 | `net.py:35-37` |
| `LANCELOT_LLM_RATES` | `{}` (no rates) | `upstream_metrics.py:48-57` |

### Upstream pool (`lancelot/llm.py`)
| Knob | Default |
|---|---|
| `LANCELOT_UPSTREAM_MAX_CONNECTIONS` | 100 |
| `LANCELOT_UPSTREAM_MAX_KEEPALIVE` | 20 |
| `LANCELOT_UPSTREAM_KEEPALIVE_EXPIRY` | 5.0 s |
| `LANCELOT_UPSTREAM_RETRY_BUDGET_MS` | 60 000 ms |
| `LANCELOT_UPSTREAM_RATE_LIMIT` | 0 (off) |
| `LANCELOT_UPSTREAM_STREAM_IDLE_SECS` | 0 (off) |

---

## LACKS (gaps relative to the byte-path mission)

1. **JWT validation** — `security.py` extracts credentials; no JWT decode/verify/expiry/scope enforcement anywhere. Explicitly user-side.
2. **Global (cross-process) rate limiting** — `RateLimit` is per-process only; `TokenBucketBackend` interface exists but no Redis backend ships. Lancelot documents this clearly.
3. **Native Rust WebSocket path** — Python ASGI only; Rust `src/ws/mod.rs` does not exist.
4. **Circuit breaker on upstream** — `TransportPool` has retry budget + rate limit but no circuit breaker; documented as a deliberate skip (M14 DEFERRED list in CLAUDE.md).
5. **Brotli (`br`) compression** — explicitly Tier-D; `GZipMiddleware` covers gzip only.
6. **`LANCELOT_MAX_INFLIGHT_BYTES` is default OFF** — the most powerful memory cap is opt-in; most deployments don't have it unless they read the service.rs comments.
7. **RED metrics absent from the Rust native server** — operators on the const-tier / native path get zero OTel metrics.
8. **OTel resource attrs not auto-installed** — `fleet.install_otel_resource_attrs()` must be called explicitly by the operator; not wired in `App.run()` or lifespan.
9. **`TenantConcurrencyLimiter` dict unbounded** — intentional by design but documented only in comments; no operator warning when cardinality grows.
