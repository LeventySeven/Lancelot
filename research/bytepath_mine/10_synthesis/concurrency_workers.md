# Byte-path synthesis — domain: concurrency / workers / GIL

**Scope.** The asyncio↔Rust bridge, GIL release discipline, the tokio runtime, the
connection-accept concurrency model, multiprocess + `SO_REUSEPORT`, the sync-handler
blocking pool, and the residual-FFI-cost gap to raw Starlette. Diff: Robyn
(`asyncio.rs` / `callbacks.rs` / `blocking.rs` / `processpool.py` / `runtime.rs`) +
Blazeio (async core, `SharpEvent`, `TCPOptimizer`) vs **verified** Lancelot source
(`src/lib.rs`, `src/dispatch.rs`, `src/service.rs`, `src/socket.rs`, `src/cpu.rs`,
`src/gil.rs`, `lancelot/pool.py`, `lancelot/batch.py`).

**Synthesizer discipline.** Every Lancelot-state claim below was confirmed by reading
the cited source this pass — not the mine notes alone. Where a mine note assumed a
fact that the source contradicts, the source wins and it is flagged.

---

## 0. Verified Lancelot concurrency model (ground truth this pass)

The architecture, confirmed against source:

- **One process-wide multi-thread tokio runtime.** `src/lib.rs:131` —
  `Builder::new_multi_thread().worker_threads(cpu::worker_count()).enable_all()`.
  NOT current-thread-per-worker (Robyn's actix model). pyo3-async-runtimes is fed
  this runtime so `into_future` / `run_until_complete` use it.
- **One embedded asyncio loop per process, uvloop-preferred.** `src/lib.rs:31-37`
  `make_event_loop` tries `uvloop.new_event_loop()`, falls back to stdlib;
  `LANCELOT_LOOP=asyncio` forces stdlib. Soft-dep (cardinal-rule clean).
- **The bridge is the stdlib pyo3-async-runtimes path.** `src/dispatch.rs:60,143` —
  `into_future_with_locals(locals, result)`. There are NO Granian-style custom
  awaitables. Sync-vs-async is decided by `hasattr(__await__)` after `call1` ran the
  body inline (`dispatch.rs:56,126`); a non-awaitable result is serialized in-place
  (no bridge), an awaitable keeps the `into_future` → GIL-dropped-await path.
- **Connection concurrency = bounded-semaphore + task-per-connection.**
  `src/service.rs:429` `Semaphore::new(max_conns)`; `:473-477` acquire a permit
  BEFORE `listener.accept()`; `:494` `tokio::spawn` per connection; `:520-524`
  `http1::Builder…serve_connection`. SIGTERM/SIGINT in the same `select!` so accept
  yields to shutdown. Graceful drain `:456` `acquire_many_owned(max_conns)`.
- **GIL discipline is type-enforced.** `src/gil.rs:23` `with_gil(f: impl FnOnce(Python)
  -> R)` — `f` is a *synchronous* `FnOnce`, so `.await` inside the GIL scope is a
  compile error; backed by `await_holding_lock = "deny"` (`Cargo.toml`). pyo3 0.27
  `with_gil` wraps `attach`.
- **cgroup-aware worker count.** `src/cpu.rs:9` `worker_count()` = `LANCELOT_WORKERS`
  override → cgroup-v2 `cpu.max` quota → logical CPUs, clamped [1,64]. Fixes Robyn's
  `os.cpu_count()` container meltdown.
- **Socket: `SO_REUSEPORT`+`SO_REUSEADDR`+`TCP_NODELAY`+`TCP_QUICKACK`+`TCP_FASTOPEN(5)`,
  backlog 16384.** `src/socket.rs:20-60`. REUSEPORT is `#[cfg(unix)]`-gated;
  QUICKACK/FASTOPEN are `#[cfg(target_os="linux")]` best-effort.
- **Sync-handler offload is Python-side.** `lancelot/pool.py:29` `ThreadPoolExecutor(
  max_workers=40)` + a Python `_inflight` counter capped at `max_inflight=10_000` →
  `PoolFull` → 503. NOT a Rust crossbeam-bounded GIL-aware pool. `contextvars.copy_context().run(...)`
  propagates per-request ContextVars into the worker thread.
- **`panic = "unwind"`** (`Cargo.toml`), NOT `panic = "abort"`. Deliberate — paired
  with `catch_unwind` at the FFI seam (R7). The Robyn `rust_core` mine asserted
  Lancelot "HAVE panic=abort"; **that is wrong** — Lancelot chose unwind so an FFI
  panic becomes a Python exception instead of aborting the process. Do not "fix" it.
- **No built-in multiprocess spawner, no Rust supervisor.** `app.run()` is
  single-process by design (`app.py:263`). There is no `processpool.py` analogue
  (no socket-FD cloning into children) and no Rust respawn loop (grep for
  supervisor/respawn/waitpid/SIGCHLD → empty). Scaling = operator fronts N processes
  via `SO_REUSEPORT` (Linux) or a proxy (macOS), under a process manager.

This model is sound and, on the things that matter, ahead of Robyn (lazy request,
no per-match GIL clone of a `FunctionInfo`, lock-free post-build router, graceful
drain, slow-loris guard, body cap, conn cap). The remaining deltas are below.

---

## 1. HAVE — already shipped, do NOT rebuild (tag so nothing is redundant)

| # | Mechanism | Peer cite | Lancelot evidence |
|---|---|---|---|
| H1 | Multi-thread tokio runtime, worker_threads from CPU count | Robyn `runtime.rs:138` (current_thread-per-worker — different tradeoff) | `src/lib.rs:131-133` `new_multi_thread().worker_threads(cpu::worker_count())` |
| H2 | uvloop on the embedded loop, soft-dep + escape hatch | Robyn `processpool.py:133` (hard dep); Blazeio `Dependencies/__init__.py:495` (import-time policy) | `src/lib.rs:31-37` `make_event_loop`; `LANCELOT_LOOP=asyncio` |
| H3 | GIL dropped across the handler await (one bridge crossing) | Robyn `executors/mod.rs:519` allow_threads-across-await | `src/dispatch.rs:60` `into_future_with_locals` + `gil::with_gil` sync-FnOnce type guard |
| H4 | GIL-across-await is a *compile error*, not convention | Robyn "obeyed by convention" (deep/07) | `src/gil.rs:23` + `Cargo.toml` `await_holding_lock="deny"` |
| H5 | Bounded-semaphore + task-per-connection accept; conn cap → 503 | Robyn unbounded actix workers; FastAPI `CapacityLimiter(40)` hang | `src/service.rs:429,473-494`; `LANCELOT_MAX_CONNECTIONS` (1024) |
| H6 | Graceful drain (`acquire_many_owned`), no hard-kill | Robyn `processpool.py:55` `process.kill()` (anti-goal B1) | `src/service.rs:456`; `LANCELOT_DRAIN_SECS` (10) |
| H7 | cgroup-aware worker count, clamp [1,64] | Robyn `os.cpu_count()` meltdown | `src/cpu.rs:9-11`; `LANCELOT_WORKERS` |
| H8 | `SO_REUSEPORT`+backlog 16384 multiprocess socket; Linux-only LB caveat | Robyn `shared_socket.rs:23`; Blazeio `proxy.py:13` | `src/socket.rs:22,60`; `#[cfg(unix)]` guard |
| H9 | `TCP_QUICKACK`+`TCP_FASTOPEN(5)`+`TCP_NODELAY` | Blazeio `TCPOptimizer`; Robyn sets none of QUICKACK/FASTOPEN | `src/socket.rs:23-58` (Lancelot AHEAD of Robyn) |
| H10 | Listen backlog ≥ Blazeio's 5000 | Blazeio `__init__.py:551` backlog=5000 | `src/socket.rs:60` `listen(16384)` (3.2× higher; the mine's "check backlog" → HAVE, ahead) |
| H11 | R3 on-loop `_drain` stream pump (one bridge crossing/stream, same-thread `try_push`, event-driven backpressure) | Robyn per-chunk `spawn_blocking`+`with_gil` (`response.rs:123`) — the cliff Lancelot fixed | `src/stream.rs`; Tier-D forbids regressing it |
| H12 | Sync-handler offload bounded → 503 (not a 40-cliff hang) | Robyn `blocking.rs` Granian pool; FastAPI anyio limiter | `lancelot/pool.py:29-39` ThreadPoolExecutor + inflight cap → `PoolFull` |
| H13 | Per-request ContextVar propagation into offloaded threads | Robyn `asyncio.rs:47` per-req Context | `lancelot/pool.py:48` `copy_context().run`; `background.py:66` snapshot at spawn |
| H14 | `panic="unwind"` + `catch_unwind` FFI seam (panic → Python exc, not abort) | Robyn `panic="abort"` (loses the boundary) | `Cargo.toml`; `dispatch.rs` R7 — **deliberate divergence, keep** |
| H15 | Lock-free router post-build (no per-request RwLock or GIL `FunctionInfo` clone-under-lock on routing) | Robyn `http_router.rs:53` `with_gil(\|_\| value.to_owned())` per match | `src/router.rs` immutable `Arc`; handler clone_ref is a single refcount bump |
| H16 | Release profile fat-LTO + codegen-units=1 + opt-level=3 | Robyn `Cargo.toml:44` | `Cargo.toml` `[profile.release]` (panic differs by design — H14) |

---

## 2. MUST_BUILD — new or improvable wins (every one is in-mission, cheap, gate-safe)

Ordered by value × confidence. All respect the cardinal rules + Tier-D.

### MB1 — TCP keepalive probe triple on the listening socket (`SO_KEEPALIVE` + idle/intvl/cnt)
- **Mechanism.** Blazeio `TCPOptimizer` (`Utils/socket_ops.py:19-33`) and the Blazeio
  proxy (`proxy.py:503`) both set `SO_KEEPALIVE=1` + `TCP_KEEPIDLE=30` +
  `TCP_KEEPINTVL=10` + `TCP_KEEPCNT=3` → a dead peer is detected in ~60s. Robyn relies
  on `KeepAlive::Os` (Linux default first probe at **2 hours**).
- **Lancelot state: MISSING.** Verified: `src/socket.rs` sets NODELAY/QUICKACK/FASTOPEN
  but no keepalive. Half-open connections (k8s pod eviction, Lambda hard-kill,
  spot-instance termination, client crash without FIN) are not reaped until the next
  failed write — each one burns a `LANCELOT_MAX_CONNECTIONS` slot indefinitely. The
  conn-cap is the headroom bottleneck for concurrent AI workloads, so a half-open leak
  directly shrinks live capacity.
- **Why it's the byte-path/operator win.** Resilience: it reclaims connection-cap
  capacity that today leaks. This is the single highest-value item in the domain
  because it touches the resource (conn slots) that gates concurrency.
- **Approach.** In `bind_reuseport`, after the QUICKACK/FASTOPEN block, use socket2's
  portable builder (no raw libc needed):
  ```rust
  #[cfg(unix)]
  {
      use socket2::TcpKeepalive;
      let ka = TcpKeepalive::new()
          .with_time(std::time::Duration::from_secs(30))
          .with_interval(std::time::Duration::from_secs(10));
      #[cfg(not(target_os = "macos"))]
      let ka = ka.with_retries(3);          // with_retries unsupported on macOS
      let _ = sock.set_tcp_keepalive(&ka);  // best-effort, same discipline as QUICKACK
  }
  ```
  Make the idle time an operator knob `LANCELOT_TCP_KEEPALIVE_SECS` (default 30, 0=off)
  for parity with the FASTOPEN/QUICKACK best-effort style.
- **Buildability: S.** ~6 lines + one env knob + a doc line. socket2 0.5 already a dep.
- **Gate risk: none.** No conformance surface (transport-only), no clippy risk
  (`set_tcp_keepalive` returns `io::Result`, ignored like the existing setsockopts).
  Validate with a real-server test that opens a connection, kills the peer ungracefully,
  and asserts the slot is reclaimed within the keepalive window (the cardinal-rule-2
  "validate under load on the path the bug lives on").

### MB2 — `LANCELOT_KEEP_ALIVE_TIMEOUT_SECS` idle-connection knob (hyper http1)
- **Mechanism.** Robyn exposes `keep_alive_timeout` (`__init__.py:705`, default 20s) and
  `client_timeout`. Lancelot's `http1::Builder` (`src/service.rs:520`) sets only
  `header_read_timeout`; there is no idle/keep-alive deadline — an idle keep-alive
  connection holds its semaphore permit until the OS or a write reaps it.
- **Lancelot state: MISSING (operator knob).** Distinct from MB1 (MB1 = dead-peer TCP
  probes; MB2 = *application-level* idle keep-alive reaper). For an AI gateway holding
  many long-lived SSE connections plus short keep-alive JSON calls, a per-connection idle
  deadline returns conn-cap headroom for connections that are alive-but-silent.
- **Approach.** hyper 1's `http1::Builder` does not expose a keep-alive idle timeout
  directly the way actix does; the clean implementation is to wrap each spawned
  `serve_connection` future in a tokio idle-timer that resets on byte activity, or use
  `serve_connection_with_upgrades` + a `timeout` around the connection when no request is
  in flight. Simplest correct shape: a per-connection `tokio::time::timeout` on the *next
  request head* (idle gap between responses), gated by an env knob, default off (preserve
  current behavior; opt-in for tight deployments). **Honest caveat:** this is partly a
  proxy responsibility — only ship it as opt-in, document that an edge proxy usually owns
  idle reaping.
- **Buildability: M.** Connection-loop wrapping is finicky (must not cut an in-flight
  request — the exact class of bug M4 found 4 times). Needs a real-server adversarial test
  (idle keep-alive client vs in-flight client).
- **Gate risk: perf/correctness, not conformance.** No conformance surface. Risk is
  cutting an in-flight request → MUST be validated under load before shipping. If the
  validation is non-trivial, MB1 alone covers most of the real leak (dead peers); MB2 is
  the lower-confidence half. Ship MB1 first; gate MB2 on a measured idle-leak case.

### MB3 — `freelist` on the per-connection/per-stream hot pyclasses + a 0-param handler fast path
- **Mechanism.** Robyn: `#[pyclass(frozen, freelist = 128)]` on `PyEmptyAwaitable`
  (`callbacks.rs:12`) recycles up to 128 instances → zero alloc under churn; and the
  0-param shortcut `if number_of_params == 0 { handler.call0() }` (`executors/mod.rs:168`)
  skips Request construction for probe/health handlers.
- **Lancelot state: MISSING (micro).** Verified: only 3 pyclasses (`stream.rs:202`
  `ChunkSink`, `ffi/fninfo.rs:5` `FunctionInfo` frozen, `ffi/request.rs:16` `LazyRequest`),
  none with `freelist`. Dispatch always builds the request and `call1((req,))`
  (`dispatch.rs:56,126`) regardless of arity.
- **Why (honest).** Both are micro-opts on the *tiny one-shot* shape — exactly the shape
  Tier-D says NOT to pitch as the selling point. BUT this is the one shape where Lancelot
  still trails raw Starlette+uvloop (~88%, the residual FFI/asyncio-bridge cost the lens
  calls out), and the const tier only covers *fixed* responses. For a 0-param dynamic
  handler (a `/healthz` that runs a real check, a cheap status endpoint), skipping the
  `LazyRequest` alloc + using `call0` is a free, correctness-neutral shave. `freelist` on
  `LazyRequest` (allocated once per request on the dynamic path) and `ChunkSink`
  (allocated once per stream) cuts allocator pressure under high RPS.
- **Approach.** (a) Add `freelist = 128` to `LazyRequest` and `ChunkSink` pyclass attrs —
  one-line each, measure with the existing oha tiny-shape harness. (b) Capture
  `number_of_params == 0` at registration (the metadata already exists in `FunctionInfo`)
  and branch dispatch to `call0()` skipping the `LazyRequest` build.
- **Buildability: S** for freelist, **S–M** for the 0-param branch (must thread the arity
  flag through dispatch).
- **Gate risk: bench-gate.** No conformance/clippy risk. But it is a micro-opt: ship ONLY
  if the oha tiny-shape bench shows a real delta (the GIL-serialized single loop may wash
  it out, same caveat as mimalloc in the ledger). If neutral, drop. Do NOT pitch it as a
  throughput headline (Tier-D).

### MB4 — Pooled-future event for the `@app.batch` hot path (Blazeio `SharpEvent`/`ioCondition`)
- **Mechanism.** Blazeio's `SharpEvent` (`Dependencies/__init__.py:146`) reuses one
  `asyncio.Future` across `wait()` calls instead of `loop.create_future()` per wait;
  `ioCondition` (`alts.py:103`) does the same for a Condition. CPython's `asyncio.Event`
  allocates a Future per `wait()`.
- **Lancelot state: PARTIAL/improvable.** Verified: `lancelot/batch.py` does NOT use
  `asyncio.Event` (good — no per-edge event), but it DOES `create_future()` per `submit()`
  (`batch.py:96`) and arms a timer via `asyncio.ensure_future(_wait_then_flush)` +
  `asyncio.sleep(window_s)` (`:122,133`) per batch window. Under the validated ~12×
  scenario (thousands of coalesce cycles/s) the per-submit Future + per-window task is the
  churn. The per-submit Future is *intrinsic* (each caller needs its own result future) —
  that one is NOT removable. The removable churn is the per-window timer task.
- **Why (honest).** This is a micro-win on an already-validated win (`@app.batch`), not a
  new capability. The per-submit `create_future` is load-bearing and can't be pooled
  without breaking result delivery. So the realistic gain is small (timer-task reuse).
- **Approach.** Replace the per-window `ensure_future(_wait_then_flush)` + `sleep` with a
  single long-lived timer coroutine per `_BatchGroup` that waits on a pooled event
  (`call_at`/`call_later` + a reusable Future), re-armed instead of re-spawned. Keep the
  timer-self-cancel guard (`batch.py:153`) intact — that fix is sacred.
- **Buildability: S–M.** Pure Python, in `batch.py`. Must not reintroduce the
  timer-self-cancel CancelledError bug (the exact bug M3 fixed) → regression test required.
- **Gate risk: correctness.** No conformance/clippy. The risk is the batch timer bug
  class. Validate with the existing `@app.batch` awaiting-handler load test. **Low
  priority** — only ship if a batch micro-bench shows churn is measurable; otherwise the
  current code is fine.

---

## 3. IMPROVE — sharpen existing surface (no new mechanism, close a real edge)

- **I1 — Document the multiprocess deploy as a first-class story with a reference
  process-manager recipe.** Robyn ships `processpool.py` (socket-FD cloning into N
  children, one `SocketHeld.try_clone()` per worker). Lancelot deliberately has NO
  built-in spawner (`app.run()` single-process; `SO_REUSEPORT` is the scaling model) —
  but the *operator* is left to wire it. The ledger lists "multi-region deploy recipes"
  as DEFERRED for wall-clock infra; this is the cheaper sibling: a documented
  `LANCELOT_WORKERS=1`-per-process + N-process pattern under systemd / a supervisor /
  `tini`+`exec`, with the macOS "front N ports behind a proxy" caveat already known.
  **This is docs, not code** — no Rust supervisor (Tier-D-adjacent: "built-in CLI" is
  refused, and a respawn supervisor edges toward owning process lifecycle). Buildability
  S (docs in `docs/deploy.md`). Gate risk: none.

- **I2 — Name the responsible knob in the conn-cap / pool 503 shed body.** The ledger
  already lists "name the responsible knob in the 503 shed body" as BUILD (trivial). For
  this domain specifically: when `Semaphore` is exhausted (conn cap) or `PoolFull` fires
  (sync pool), the 503 body should name `LANCELOT_MAX_CONNECTIONS` / the blocking-pool
  `max_inflight` so the operator knows which knob to raise. Buildability S. Gate risk:
  none (body text only; 503 status unchanged).

- **I3 — Surface a `lancelot.http.connections.active` / `inflight` gauge for the conn
  semaphore.** RED metrics exist, and `queue_wait_ms` exists for the tenant limiter, but
  there is no gauge for live connection-semaphore occupancy — the exact signal an operator
  watches to know they're near `LANCELOT_MAX_CONNECTIONS` (and the signal MB1's leak would
  show up on). Emit current permits-in-use as an OTel up/down counter from the accept loop.
  Buildability S–M (Rust→metrics seam; the metric emit is Python-side today, so plumb a
  counter through). Gate risk: none (additive metric).

---

## 4. REFUSE — peer mechanisms that violate a cardinal rule or Tier-D

| Item | Peer cite | Why refuse |
|---|---|---|
| Granian-style custom awaitables (`PyIterAwaitable`/`PyFutureAwaitable`, claimed 38–55% faster bridge) | Robyn `callbacks.rs:12-375`, `runtime.rs:163,182` | High build+maintenance cost (5 hand-rolled `#[pyclass]` + a `BlockingRunner` to marshal results off the reactor thread) for a bridge-isolation win that the lens already classifies as residual FFI cost on the ONE shape (tiny one-shot) Tier-D forbids pitching. The 38–55% is Granian's *bridge-in-isolation* number, not end-to-end req/s (GIL + serialization dominate). For AI workloads (LLM latency 800ms+) it's fully amortized. Net: large risk, washes out on the workloads that matter. Bench-gate-only at best; effectively REFUSE under "no chasing 1% wins / must change the byte path." If a future free-threaded (no-GIL) build lands, revisit — the calculus changes. |
| Dedicated Rust crossbeam-bounded GIL-aware sync pool (`src/blocking/runner.rs`) | Robyn `blocking.rs:34-177`; WBS §7.2/§09 | The §07 catalog and WBS *spec* a Rust crossbeam(10_000) pool, but Lancelot shipped the equivalent **in Python** (`pool.py` ThreadPoolExecutor + inflight cap → 503), which is correct, cardinal-rule-clean (no new Rust surface, ContextVar propagation works), and already validated. Porting it to Rust adds a second blocking subsystem + a Rust→Python `BlockingTask` marshal for zero functional gain (the GIL still serializes the sync Python body either way). The only Robyn edge — isolating sync handlers from tokio's shared `spawn_blocking` (fs/DNS) — doesn't apply: Lancelot's pool is already a *separate* ThreadPoolExecutor, not tokio's. REFUSE (would re-build what M-series shipped). |
| `panic = "abort"` in the release profile | Robyn `Cargo.toml:44` | Lancelot deliberately uses `panic="unwind"` + `catch_unwind` at the FFI seam so a handler panic becomes a Python exception, not a process abort. The Robyn mine mis-tagged this as a Lancelot gap. Changing to abort would DELETE the panic-safety boundary (R7). REFUSE. |
| `io-uring` runtime | Robyn `Cargo.toml:53` (`actix-web/experimental-io-uring`) | Lancelot is hyper+tokio; the equivalent is `tokio-uring`, a *separate incompatible runtime* (not tokio multi-thread). Huge integration cost for an epoll-syscall shave that the 183k const tier + uvloop loop already beat Robyn without. Tier-D-adjacent (the ledger lists "HTTP/2 / TLS / connection-setup path" as un-mined future targets; io-uring is below that bar). REFUSE for now. |
| Deploy 1-process-many-tokio-threads to "use the runtime" | n/a (anti-pattern) | Explicit Tier-D: the server anti-scales with threads (GIL); multiprocess via `SO_REUSEPORT` is the only scaling model. The multi-thread tokio runtime is for I/O concurrency, NOT Python parallelism. REFUSE (and keep the deploy doc loud about it). |
| Built-in multiprocess spawner / process supervisor with respawn loop | Robyn `processpool.py`; WBS U7 (`max_restarts=5`) | Owning process lifecycle (fork/respawn/SIGCHLD) edges into the refused "built-in CLI / runner" territory and duplicates what systemd / k8s / a supervisor already do correctly. Lancelot's boundary (single-process `app.run()`, operator fronts N) is deliberate and cardinal-rule-clean. Ship I1 (docs) instead. REFUSE the code. |
| `ujson` / `orjson` as a JSON dep to speed serialization | Blazeio `Dependencies/__init__.py:47` (ujson runtime dep) | Cardinal rule 3 (pydantic-only runtime) + Tier-D (pydantic-core `to_json` already wins on typed models; orjson/ujson deprecated in FastAPI). REFUSE. |

---

## 5. The residual-FFI-cost gap to raw Starlette (the lens's explicit ask)

The lens asks specifically about "the residual FFI/asyncio-bridge cost on the tiny
one-shot shape (~88% of raw Starlette+uvloop)." Diagnosis from the verified source:

- The gap is **structural, not a bug.** Every dynamic request pays: (1) one
  `LazyRequest` alloc, (2) `call1((req,))` under the GIL, (3) for async handlers, one
  `into_future_with_locals` bridge crossing + the GIL-dropped await + a re-acquire to
  serialize. Raw Starlette+uvloop has no FFI boundary — it's all one Python loop.
- **What can shave it (all already catalogued above), in honest priority:**
  - The **const tier already flips this shape** for *fixed* responses (183k vs ~59k) —
    that's the answer for health probes / cached / static.
  - For *dynamic* tiny handlers: MB3 (freelist + 0-param `call0`) is the only in-mission
    lever, and it's bench-gated/micro — may wash out under the GIL-serialized single loop
    (same honest caveat as mimalloc in the ledger).
  - The big lever (custom awaitables, MB→REFUSE) is explicitly NOT worth it: it targets
    exactly the shape Tier-D says not to pitch, and the win is bridge-isolation not e2e.
- **Conclusion:** do not spend cycles trying to close the tiny-one-shot gap to Starlette.
  The positioning lock is "beats FastAPI + Robyn on every shape" + const tier + streaming
  + `@app.batch` — NOT "match raw Starlette on tiny." The residual ~12% is the documented,
  accepted cost of owning the byte path in Rust. Ship MB1 (the real resilience win),
  bench-gate MB3, and leave the rest.

---

## 6. Build order (this domain only)

1. **MB1 — TCP keepalive triple** (S, no gate risk, highest value: reclaims conn-cap
   capacity). Ship first.
2. **I2 — name the knob in 503 bodies** + **I3 — conn-active gauge** (S, additive, makes
   MB1's leak observable). Bundle with MB1.
3. **I1 — multiprocess deploy doc** (S, docs only, closes the "how do I scale" gap that
   Robyn answers in code).
4. **MB3 — freelist + 0-param fast path** (S, bench-gate; drop if the oha tiny-shape delta
   is neutral).
5. **MB2 — keep-alive idle timeout knob** (M, opt-in, MUST validate-under-load it never
   cuts an in-flight request; lower confidence than MB1).
6. **MB4 — pooled batch timer** (S–M, lowest priority; only if a batch micro-bench shows
   timer-task churn matters; regression-test the timer-self-cancel bug).

Everything else in the peer concurrency surface is HAVE or REFUSE.
