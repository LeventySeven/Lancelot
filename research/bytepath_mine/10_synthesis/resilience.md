# Resilience / cloud-native — domain synthesis brief

**Domain:** body/conn caps · timeouts · graceful drain · health states · slow-loris ·
idempotency · backpressure · SSRF · rate-limit · tenant isolation · disconnect-cancel.
**Method:** "just build, brutal." Every candidate changes the byte path or the operator
experience, or it doesn't ship.
**Inputs diffed:** `03_blazeio/{core,modules}.md` (safeguards/relay/socket), `04_serving/*.md`
(SGLang/TGI/Ray watchdogs + health + backpressure), `02_fastapi/production_patterns.md`
(disconnect/queue/threadpool anti-patterns), `01_robyn/rust_core.md` (drain/socket), against the
`05_lancelot_state/{resilience,bytepath,core,research_plans_ledger}.md` baseline + verified
against `lancelot/` + `src/` source.
**Date:** 2026-06-22.

---

## Verdict up front

Resilience is **Lancelot's most-built domain.** Every peer safeguard that matters is already
shipped and, in most cases, structurally superior (Rust hyper vs Blazeio's pure-Python
BufferedProtocol; graceful drain vs Robyn's `exit(0)` hard-kill; dual-task disconnect race vs
the `request.is_disconnected()` polling that vLLM explicitly documents as broken under
middleware). The mine surfaced **no large net-new resilience primitive** — the moat is intact.

What it DID surface is a tight set of **small, high-certainty hardening wins**, every one cheap
(S, two M) and on the existing Rust/ASGI seams. They cluster into three buckets:
1. **Connection-slot hygiene** — half-open TCP reap (the one real Blazeio/Robyn delta).
2. **Three-state health** — the `starting_up` window where `/readyz` falsely returns 200.
3. **Egress + shed precision** — Python-seam stream bytes-in-flight cap; name-the-knob in 503
   bodies; per-request escape hatch on the all-or-nothing in-flight-bytes shed.

The bigger AI-serving "resilience-adjacent" items (adaptive batch window, in-batch dedup,
token-cost batch sizing, priority semaphore) are real but belong to the **batch/streaming**
domain synthesis, not here — they are admission-control on `@app.batch`, not transport
resilience. Flagged in `improve`/cross-ref so they aren't lost, but de-scoped from must_build.

---

## must_build (sharp, cheap, in-mission)

### 1. SO_KEEPALIVE + TCP keepalive probe triple on the listen socket  — S
**Mechanism (peer):** Blazeio `Utils/socket_ops.py:19-33` + `Other/proxy.py:493-506`, Robyn
`server.rs` both set `SO_KEEPALIVE=1` + `TCP_KEEPIDLE=30` + `TCP_KEEPINTVL=10` +
`TCP_KEEPCNT=3` — dead peer detected in ~60 s (30 idle + 3×10 probes).
**Lancelot state — MISSING (verified `src/socket.rs`).** socket.rs sets `SO_REUSEPORT`,
`SO_REUSEADDR`, `TCP_NODELAY`, `TCP_QUICKACK` (Linux), `TCP_FASTOPEN` (Linux), `listen(16384)`
— but **no `SO_KEEPALIVE` and no `KEEPIDLE/INTVL/CNT`.** This is the single genuine socket-tuning
delta vs both peers.
**Why it's a real byte-path/resilience win:** half-open TCP connections (client process died
without FIN — the *normal* case in AI deploys: Lambda hard-kill, k8s pod eviction, spot-instance
termination, mobile network drop) are not reaped until the next failed write. hyper holds the
connection and burns a slot against `LANCELOT_MAX_CONNECTIONS` (1024 default) **indefinitely**.
Connection-cap headroom is the bottleneck for concurrent AI workloads; a slow trickle of
half-open leaks erodes capacity until new requests are refused. This is exactly the resilience
axis Lancelot already cares about (conn cap + drain) — keepalive completes it.
**Recommended approach:** in `bind_reuseport` after the FASTOPEN block, use `socket2::TcpKeepalive`:
```rust
#[cfg(unix)]
{
    use socket2::TcpKeepalive;
    let ka = TcpKeepalive::new()
        .with_time(std::time::Duration::from_secs(30))
        .with_interval(std::time::Duration::from_secs(10));
    #[cfg(not(target_os = "macos"))]   // with_retries unsupported on macOS (default 8 probes is fine)
    let ka = ka.with_retries(3);
    let _ = sock.set_tcp_keepalive(&ka);  // best-effort, same fail-silent discipline as QUICKACK
}
```
Optional knobs `LANCELOT_TCP_KEEPALIVE_SECS` (default 30, 0=disable) for parity with the other
socket env knobs. **gate_risk:** none. `socket2` already a dep; `cargo clippy` clean; no
conformance surface (server socket only); no perf hot-path touch (one setsockopt at bind).

### 2. Three-state health: `starting_up` → `/readyz` 503 until lifespan startup completes  — S
**Mechanism (peer):** SGLang/TGI gate traffic until the engine is warm — TGI runs a `Warmup`
generate before accepting; SGLang `/health_generate`. Generalized: a worker must report
**not-ready** during the window between bind and startup-complete, not just during drain.
**Lancelot state — MISSING (verified `lancelot/app.py` + `asgi.py`).** Only `_shutting_down`
exists (`app.py:137`, `/readyz`→503 on drain at `app.py:314`). There is **no `_starting_up`
flag**: between `run()` binding the port and the app's own `startup` lifespan completing,
`/readyz` returns 200 (the route is a handler route, registered at construction). An LB/k8s
readiness probe gets a **false-positive 200** and routes traffic to a worker whose DB pool /
LLM client / `app.state.engine` (the canonical vLLM Archetype-2 injection,
`02_fastapi/production_patterns.md §6`) is not yet built.
**Why it's a real correctness win:** deploy-and-roll. Every new replica can receive real
traffic before it can serve it → cold 500s on every rollout. This is the classic missing third
health state, and Lancelot already owns the `/livez`+`/readyz` contract — closing it is a flag,
not a feature.
**Recommended approach:** add `self._starting_up: bool = True` in `App.__init__`; the lifespan
driver clears it after `lifespan.startup.complete` is acked (the native-server lifespan path in
`app.py:_make_lifespan_callbacks` and the ASGI `_lifespan` in `asgi.py:76-77`). `_readyz`
returns `503 + Retry-After: 1` when `_starting_up OR _shutting_down`. `/livez` stays a
zero-dep const-tier 200 throughout (process-alive ≠ traffic-ready — the whole point of the
split). Escape hatch `LANCELOT_READYZ_REQUIRE_STARTUP=0` for users who run without lifespan.
**gate_risk:** low. `/readyz` is not in the vendored conformance corpus (Lancelot-specific
route). One internal test (probe during a slow startup lifespan → 503, then 200). No perf
surface. Watch: don't let the flag deadlock if lifespan is disabled (`LANCELOT_LIFESPAN=0`) —
then `_starting_up` must start False.

### 3. Python-seam streaming bytes-in-flight cap (`@app.stream(max_buffered_bytes=)` / env)  — M
**Mechanism (peer):** SGLang/TGI never emit more than the pool can sustain
(`04_serving/serving_frontier.md §9`; TGI `max_batch_total_tokens`). The HTTP analog: bound
total un-ACKed streamed bytes so a slow/stalled client can't OOM the worker. Blazeio enforces
this structurally via `pause_writing`/`resume_writing` + `buffer_overflow_manager`
(`03_blazeio/core.md` M8).
**Lancelot state — PARTIAL (verified).** The **Rust** native path has `LANCELOT_STREAM_BUFFER`
(bounded mpsc, default 256 chunks) AND `LANCELOT_MAX_INFLIGHT_BYTES` (aggregate body budget,
`src/service.rs`). But the **Python ASGI/uvicorn seam** — the path that hosts `@app.stream`,
DI, `StreamingResponse`, and `GZipMiddleware` — has **no bytes-in-flight bound** (grep:
zero matches for `bytes_in_flight`/`max_stream_bytes`/`buffered_bytes` in `asgi.py`/`stream.py`).
A slow SSE consumer that stops reading lets the producer coroutine run ahead; uvicorn buffers
into the transport without a framework-level cap. This is the one resilience axis where the
native and ASGI paths diverge.
**Why it's a real DoS-resilience win:** the AI-streaming use case is precisely long-lived SSE to
possibly-flaky clients. `max_stream_lifetime` (HAVE) bounds *duration*; `max_stream_silence`
(HAVE) bounds *idle*; neither bounds *resident bytes under a slow reader*. Completing the
backpressure picture on the seam that actually serves streams is in-mission.
**Recommended approach:** track bytes handed to `send()` minus bytes the transport reports
flushed; when over the cap, await before pulling the next chunk from the user generator (the
`@app.stream` consume loop in `asgi.py:1196+` already serializes sends under `_send_lock` — gate
there). Per-route `@app.stream(max_buffered_bytes=N)` + `LANCELOT_STREAM_MAX_BUFFERED_BYTES`
env, default 0 = off (zero cost on the common path, same posture as `LANCELOT_MAX_INFLIGHT_BYTES`).
**Validate-under-load (mandatory):** subprocess + a deliberately-slow-reading HTTP client
(`recv` then sleep); assert RSS stays bounded and the producer parks. This is exactly the kind
of feature the cardinal rule says is green-on-TestClient but must be proven on the *stalled
client* path.
**gate_risk:** medium-perf — must NOT add a per-chunk cost on the default (cap=0) path; the
check must be a single branch gated on `cap > 0`. No conformance surface. clippy n/a (Python).

### 4. Name the responsible knob in the 503 / 413 / 429 shed body  — S
**Mechanism (peer/pattern):** SGLang `/get_load` + the universal operator-experience principle
that a shed response should tell the operator *which* cap fired. Ledger BUILD item ("Name the
responsible knob in the 503 shed body", ROUND 2 ADAPT).
**Lancelot state — MISSING (verified `asgi.py`).** Every shed path emits a generic body:
`{"detail":"Service Unavailable"}` (conn cap `asgi.py:338`, PoolFull `:1876`, BatchOverload
`:1885`) and the drain 503 has no body. An operator triaging a 503 storm can't tell
connection-cap from sync-pool-full from batch-overflow from draining without correlating logs.
**Why it's a real operator-experience win:** Lancelot's whole resilience pitch is "every shed is
graceful + observable." A typed shed reason is the cheap completion of that. Same discipline as
`set_retry_hint` and `X-Lancelot-Cost-USD` — server tells the client/operator the truth.
**Recommended approach:** keep the body minimal + safe, add a machine-readable reason: header
`X-Lancelot-Shed-Reason: max_connections | sync_pool_full | batch_overload | draining |
inflight_bytes` (+ optionally the knob name). Do NOT leak internal numbers/limits in the body
(info-disclosure). The drain 503 already has `Retry-After: 1`; just add the reason header.
**gate_risk:** none. Headers only; conformance corpus doesn't assert these (Lancelot-specific
shed paths). Trivial.

### 5. Per-request escape hatch / 429-throttle for `LANCELOT_MAX_INFLIGHT_BYTES`  — M
**Mechanism (peer/pattern):** SGLang's adaptive admission (`new_token_ratio` jump/decay,
`04_serving/serving_frontier.md §5`) is the sophisticated version; the cheap version is simply
"don't make the most powerful memory cap all-or-nothing." Ledger ROUND 2 HIGH BUILD item.
**Lancelot state — PARTIAL (verified `src/service.rs`).** `LANCELOT_MAX_INFLIGHT_BYTES`
(M19, AtomicI64 + RAII `InflightGuard`) sheds with a flat 503+Retry-After the instant the
aggregate budget is breached — **no per-route exemption, no soft/hard band.** A single
const-tier `/livez` or a tiny critical control-plane call gets shed alongside a 50 MB upload
flood. Default 0 (off), so most deployments never benefit at all (also the standing gap #6 in
`05_lancelot_state/resilience.md`).
**Why it's in-mission:** the in-flight-bytes budget is the "missing second axis" beyond
`max_connections × max_body` — Lancelot built it but it's too blunt to turn on. Making it
selective (health/const routes always pass; a soft band that 429s only non-critical paths) is
what gets operators to actually enable it.
**Recommended approach:** (a) always exempt the const-tier + `/livez`/`/readyz`/`/fleet/status`
from the in-flight budget (they're prebaked, ~zero bytes anyway); (b) optional soft watermark
`LANCELOT_MAX_INFLIGHT_BYTES_SOFT` — above soft, shed only idempotent/sheddable requests
(carry a header or route flag); above hard, shed all. Keep it a single atomic compare on the
hot path; default both 0 = off.
**gate_risk:** low-perf (one extra atomic load, already in the path); no conformance surface;
clippy clean. Keep the **load test** (flood + assert health probes still 200) — this is the
"flooded cap" path the cardinal rule names explicitly.

---

## improve (cheap deltas on existing surface; lower than must_build)

- **De-correlated ("full-jitter") retry backoff in `TransportPool`** — `llm.py:481` already adds
  `random.random()*backoff_base` (HAVE partial jitter), but it's *additive* jitter on a
  synchronized exponential base; competing retriers still cluster. The ledger BUILD item
  (LanceDB pattern) + Ray Serve `uniform(T,2T)` (`04_serving §23`) want full de-correlated
  jitter (`sleep = random.uniform(base, prev*3)`), which matters under `@app.batch` fan-out
  hitting a 429'd LLM. ~3-line change in the backoff formula. **S, no gate risk.**
- **Honor inbound `x-should-retry` in the outbound retry classifier** — ledger ROUND 2 ADAPT;
  `llm.py:471`. The response-side `set_retry_hint` exists; wiring the *inbound* hint into the
  retry decision closes the loop. **S.**
- **Emit `499` through RED metrics on a won client-disconnect race** — `metrics.py:149`
  anticipates the status but it's never produced; the disconnect-cancel path (HAVE) should tag
  the metric so operators can see disconnect rate distinct from 5xx. **S.**
- **Mid-stream upstream failure → terminal SSE chunk** — `04_serving`/gateways M7: OpenAI-SDK
  clients expect a final `finish_reason:"error"` (or Lancelot's `ProblemDetail` frame, which is
  HAVE for `@app.stream(error_frame=True)`). Confirm the *relay* path (`llm.py` relay/
  `idle_guarded_chunks`) also emits a terminal error frame on upstream failure after first
  chunk, not just a bare CancelledError close. **S, correctness.**
- **`/fleet/status` queue_depth field** — `04_serving §17` (SGLang `/get_load`). Expose the
  `TenantConcurrencyLimiter` semaphore wait depth (already tracked as `queue_wait_ms`) as a
  `queue_depth` int in the existing `/fleet/status` JSON so an LB can shed to less-loaded
  workers. One field, zero new code path. **S.**
- **Readiness-callback hook → `/readyz` 503** — ledger ROUND 2 ADAPT (5-project convergence):
  `App(readyz_check=fn)` lets the operator gate readiness on their own predicate (DB ping,
  model warm). Pairs naturally with must_build #2 (the framework owns `_starting_up`; this lets
  the user add their own condition). The SGLang `health_generate` deep-probe
  (`04_serving §7`) is the same shape — ship as a hook, not built-in AI logic. **S.**
- **`x-ms-retry-after-ms` (Azure) in `_retry_after_seconds`** — gateways M1-delta; `llm.py:376`.
  Portkey parses it; Lancelot only parses `retry-after`/`retry-after-ms`. One line. **S.**

---

## have (already shipped — do NOT rebuild; tag so nothing is redundant)

| Resilience mechanism | Lancelot HAVE | Evidence |
|---|---|---|
| Body cap → 413 (pre-buffer) | `LANCELOT_MAX_BODY` 16 MiB, hyper `Limited` | `src/service.rs:37`; also ASGI seam 413 `asgi.py:728` |
| Connection cap → 503+Retry-After | `LANCELOT_MAX_CONNECTIONS` 1024, semaphore-before-accept | `src/service.rs:54`; M4 V1.3 load-validated |
| In-flight bytes budget (the 2nd axis) | `LANCELOT_MAX_INFLIGHT_BYTES` AtomicI64 + RAII guard | `src/service.rs:68` (improving in must_build #5) |
| Graceful drain (NOT hard-kill) | `acquire_many_owned(max_conns)` + `LANCELOT_DRAIN_SECS` | `src/service.rs:103`; M4 V1.4 fixed-under-load; beats Robyn `exit(0)` (`01_robyn §10`) |
| Slow-loris header deadline | `LANCELOT_HEADER_TIMEOUT_MS` 15 000, http1-only builder | `src/service.rs:121`; M4 V1.1; beats Robyn `client_request_timeout(0)` (`01_robyn §1`) |
| Per-route timeout → 504 (yield-dep teardown preserved) | `timeout=` kwarg, `asyncio.wait_for` in solve | `asgi.py:1448`; FastAPI parity |
| `/livez` (const-tier, zero-GIL) + `/readyz` (503 on drain) | both auto-registered, bypass drain gate | `app.py:305-335`, `asgi.py:316-325` (completing 3rd state in must_build #2) |
| `/fleet/status` (region/worker/in_flight/uptime/status) | auto-registered | `fleet.py:57` (adding queue_depth in improve) |
| Client-disconnect → cancel (unary + streaming) → upstream abort | dual-task `FIRST_COMPLETED` race, NOT `is_disconnected()` polling | `asgi.py:599-635`, `llm.py:36-67`; vLLM-validated correct (`02_fastapi §1,15`) |
| Bounded sync-handler offload + 503 valve (no 40-thread cliff) | `BlockingPool(max_inflight)` → `PoolFull`→503 | `pool.py`; beats FastAPI unbounded threadpool (`02_fastapi §5,16`) |
| Per-tenant concurrency isolation | `TenantConcurrencyLimiter` semaphore + `queue_wait_ms` | `tenant.py`; anti-monopoly (`02_fastapi §16`) |
| Inbound rate-limit → 429+Retry-After+RateLimit-* | `RateLimit` token bucket, pluggable backend | `ratelimit.py` (per-process caveat documented) |
| SSRF guard (opt-in, stdlib-only) | `assert_safe_url` + `TransportPool(block_private_ips)` | `net.py`; CGNAT + IPv4-mapped + DNS-TTL covered |
| SSE keepalive = spec-valid comment `":\n\n"` (not data frame) | verified | `asgi.py:1047`; gateways M8 satisfied |
| `X-Accel-Buffering: no` + `Cache-Control: no-cache` on SSE | verified | `asgi.py` SSE headers; Blazeio §12 satisfied |
| RED metrics with route-TEMPLATE labels (cardinality-safe) | `_route_label` not `scope["path"]` | `asgi.py:431`; Sentry 2-phase pattern satisfied (`02_fastapi §9`) |
| Streaming TTFT (inbound + upstream) | `lancelot.http.stream.ttft` + `lancelot.upstream.ttft` | `metrics.py:67`, `upstream_metrics.py`; the AI-streaming UX metric |
| `max_stream_lifetime` reconnect sentinel + `max_stream_silence` abort | both shipped | `asgi.py` watchdog; `04_serving §20,21` satisfied |
| Post-200 SSE error as `ProblemDetail` frame + guaranteed `done_sentinel` | `@app.stream(error_frame=True)` | `asgi.py:1156`; `02_fastapi §2` satisfied (relay-path confirm in improve) |
| POST never auto-retried; idempotent retry honors Retry-After + budget | `_IDEMPOTENT` + `retry_budget_ms` | `llm.py:443`; double-charge-safe |
| Outbound `Idempotency-Key` (stable across retries) | `lancelot-{uuid4}` setdefault | `llm.py:752` |
| W3C traceparent + request-id propagation (in + out) | one-scan in; auto-inject out | `asgi.py:440`, `llm.py:764`; no OTel dep |
| Per-request ContextVar isolation (no cross-request bleed) | per-request Context; M10 verified not-a-bug | `context.py`; beats Robyn pre-#1380 leak |
| SIGHUP secret/cert rotation | `secrets.install_sighup_handler`, `TransportPool.reload_mtls` | `secrets.py`, `llm.py:350` |
| Webhook sign/verify (constant-time, freshness window) | `lancelot.webhook` Standard Webhooks | `webhook.py`; empty-key fail-open bug fixed |
| Multipart/query-string DoS caps | `max_fields/max_files/max_part_size`, `LANCELOT_MAX_QUERY_PARAMS` | `uploads.py`, `asgi.py:31` |
| JSON depth guard → 400 | `json_max_depth` 128 | `asgi.py:752` |
| Cookie-bomb / response-splitting defense | first-wins lenient parser; CRLF strip on all set fields | `asgi.py:49`, `responses.py:5` |
| Path-traversal guard (static + FileBackend) | `realpath`+`commonpath` | `staticfiles.py:73`, `secrets.py:76` |
| Retry backoff jitter (additive) | `random.random()*backoff_base` | `llm.py:481` (full de-correlation in improve) |
| Socket: REUSEPORT (unix-guard) + NODELAY + QUICKACK + FASTOPEN + backlog 16384 | verified | `src/socket.rs` (adding keepalive in must_build #1) |
| `cancel_on_disconnect` dual-task, zero-cost when off | route + app flag | `asgi.py:1461`; `02_fastapi §1` |
| Memory budget with named-consumer OOM | `MemoryPool` + `ResourcesExhausted(top_consumer=)` | cooperative opt-in |

---

## refuse (violates a cardinal rule or the Tier-D ledger — do NOT propose)

| Item | Source | Why refuse |
|---|---|---|
| Per-host **circuit breaker** on `TransportPool` (half-open state) | LiteLLM failure-rate breaker (`04_serving`/gateways M2); ledger §7.2 §10 | Explicit Tier-D / deliberate omission (CLAUDE.md M14 DEFERRED). `TransportPool` is single-provider per instance; failure-rate cooldown belongs to a future multi-deployment `app.llm` router layer (document constants there), NOT the per-call pool. POST-never-retry + retry_budget already prevent the cascade. |
| **CorkDispatcher** adaptive learned batching (numpy `lstsq` latency fit) | BentoML (`02_fastapi §8`) | Adds `numpy` runtime dep → violates cardinal rule #3 (pydantic-only). Fixed-window `@app.batch` is the right shape. |
| **ZMQ / mp.Queue** response-demux broker | LitServe (`02_fastapi §8`) | Separate dep + inference-specific; cross-process model-worker broker is Tier-D (ledger §1: "hosts the model = user-side"). |
| Adaptive batch backoff from upstream error-rate feedback loop | SGLang `new_token_ratio` (`04_serving §5`) | Borderline AI-admission-control; complex feedback loop; belongs to batch domain if anywhere, and adaptive *SLA-aware batch window* is on the Tier-D refuse list (ledger §1). |
| Priority-queue semaphore + `X-Priority` header tier preemption | Groq `service_tier` (`04_serving §19`) | Queue preemption / SLA tiering is operator/LB policy, adjacent to "adaptive SLA-aware batching" (Tier-D). Re-orders fairness in a way that's user-policy, not transport. Revisit only on demonstrated demand. |
| `pause_generation`/`continue_generation` global scheduler suspend | SGLang (`04_serving §8`) | Model-server control plane; Lancelot doesn't own a scheduler. |
| Abort-by-request-id control-plane endpoint | SGLang `/abort_request` (`04_serving §8`) | Needs a thread-safe in-flight `{request_id: Task}` registry + a mutating control endpoint; cancel-on-disconnect (HAVE) covers the high-frequency case; net-new control surface with no byte-path win. Defer until operators ask. |
| Global "no-requests-in-N-seconds" hung-server watchdog → self-restart | SGLang `watchdog_timeout=300s` (`04_serving §21`) | Infra/orchestrator concern (k8s liveness probe restarts the pod); duplicating it in-process is out of scope. `max_stream_silence` (HAVE) covers the per-stream case. |
| **TLS context hardening / SNI per-host cert dispatch** on the server | Blazeio `Sslproxy` (`03_blazeio/modules.md §9`) | Lancelot is HTTP/1.1 cleartext behind a TLS-terminating proxy (documented divergence). TLS terminates at the edge. The hardening defaults are noted only for the *outbound* mTLS surface (already has `reload_mtls`). |
| Global cross-process rate limit shipped with a Redis backend | design/03 (`05` ledger) | The `TokenBucketBackend` interface exists; shipping Redis violates pydantic-only runtime. Per-process bucket + documented caveat is correct; the shared backend is the operator's to supply. |
| Brotli streaming compression / per-chunk gzip | ledger Tier-D | Per-chunk compression is a perf cliff; brotli is a second dep + negotiation branch (proxy's job). gzip (M18, off-loop) is the right shape. |
| Robyn-style hard-kill shutdown | ledger Tier-D | Explicit anti-goal; graceful drain (HAVE) is the thesis. |
| `request.is_disconnected()` polling for disconnect | vLLM rejects it (`02_fastapi §1,15`) | Broken under middleware (BaseHTTPMiddleware wraps `receive`). Lancelot's direct-`receive` dual-task race is the correct, already-shipped design. Anti-pattern to avoid, not a thing to build. |

---

## Cross-cutting notes for the build-order owner

- **Bundle the two S-socket/health items first** (must_build #1 keepalive + #2 starting_up):
  both are pure operator-correctness, near-zero risk, no conformance/perf surface, and together
  they close the two most-cited deploy-time resilience holes (slot leak on dead clients +
  false-ready on rollout). Highest certainty-to-effort in the whole pass.
- **must_build #3 (stream bytes-in-flight) and #5 (in-flight-bytes precision) both need the
  real-load test** the cardinal rule mandates — the *stalled client* and the *flooded cap*
  paths respectively. Don't ship either on TestClient green alone; this domain has burned the
  project four times that way.
- **The AI-serving admission items (adaptive window, in-batch dedup, token-cost batch sizing,
  `max_concurrent_batches`, priority) are NOT resilience-domain** — they are `@app.batch`
  admission control. Listed in `04_serving/serving_frontier.md §3,4,5,15,19` and cross-ref'd in
  `improve` only so the batch/streaming synthesizer picks them up. In-batch dedup (#4 there:
  "fan one identical-key result to all waiters") is the single highest-value AI item across the
  whole serving mine — make sure it lands in the batch brief, not lost between domains.
- **Nothing here regresses a HAVE.** Every must_build is additive on an existing seam; the moat
  (one Rust-owned, backpressured, cancellation-correct in+out byte path) is untouched and, per
  this diff, ahead of every peer on resilience.
