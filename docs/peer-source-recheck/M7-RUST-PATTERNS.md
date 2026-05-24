# M7 Rust Patterns — extracted from 8 Rust-project teardowns

Synthesis from 4 parallel agents reading 8 Rust-project teardowns in `researchfms/teardowns/`. Total source: ~78k lines. Each row cites a real `path:line` from one of the teardowns.

| Batch | Sources | Total |
|---|---|---|
| A — pyo3 / Rust-from-Python | POLARS (1,158) + LAKESAIL (9,197) + RIPGREP (1,286) | 11,641 lines |
| B — HTTP + query engine | TURBOPUFFER (11,736) + DATAFUSION (7,489) | 19,225 lines |
| C — vector DB + bindings | LANCEDB (25,062) | 25,062 lines |
| D — async + IPC | TAURI (8,755) + ZED (13,898) | 22,653 lines |

## Top 14 ADOPT-M7 candidates (consolidated, ranked)

| # | Pattern | Source | M7 track | Size / risk |
|---|---|---|---|---|
| 1 | **Asymmetric backpressure** — unbounded outgoing mpsc, bounded incoming mpsc | ZED.md:4803-4817 | T4 (NEW) | small / low |
| 2 | **`cfg(test)` buffer-size = 1** — forces backpressure path under every unit test (would have caught M4's 4 broken-under-load features) | ZED.md:6157-6165 | T4 (NEW) | small / low |
| 3 | **`select_biased!` + `util::defer` Drop-guard** for graceful shutdown — SIGTERM-priority + bracketed cleanup as compile-time invariant | ZED.md:4826-4861 + 10994-11030 | T4 (NEW) | small / med |
| 4 | **`ForegroundWork` dispatch queue** — `Send`-bg-task → mpsc → one foreground drainer (matches R3 on-loop `_drain`) | ZED.md:10283 (acp.rs) | T1.4 (MCP) | medium / low |
| 5 | **Triple-channel RPC model** — unbounded outgoing + oneshot for unary + mpsc for streamed responses, all keyed by request id | ZED.md:4729-4772 (peer.rs) | T1.1 (spawn_child) | medium / med |
| 6 | **Per-tenant fair `MemoryPool` + named-consumer OOM error** — RAII `MemoryReservation` with Drop-free; OOM names top consumer (route + tenant) | DATAFUSION.md:2058-2265 | T2 (new T2.7) | medium / med |
| 7 | **`queue_wait_ms` metric + per-tenant concurrency cap** — `server_total_ms − query_execution_ms` = queue wait | TURBOPUFFER.md:6680-6712 | T2.4 extension | small / low |
| 8 | **`x-should-retry: true/false` header + middleware** — server-controlled retry overrides client status-code defaults | TURBOPUFFER.md:858-906 | T2.2 extension | small / low |
| 9 | **`tokio::task::coop::consume_budget` once per N rows** for CPU-heavy Rust paths called from handlers (e.g. canonical-JSON over MB payloads) | DATAFUSION.md:1301-1318 | T4 (NEW) | small / med |
| 10 | **Levenshtein "did you mean?" on 404** computed lazily in `fmt::Display` — pays nothing on caught errors | LANCEDB.md:4076-4108 | T4 (NEW) | small / low |
| 11 | **KeyboardInterrupt-aware 100ms poll in blocking `App.run()`** — Ctrl+C reaches the runtime via `py.check_signals()` | LANCEDB.md:2569-2607 | T4 (NEW) | small / low |
| 12 | **`snafu` + `#[snafu(implicit)] location: Location`** at the Rust↔Python boundary — file:line on every traceback | LANCEDB.md:4023-4067 | T4 (NEW) | medium / low |
| 13 | **`intern!(py, "name")` Python-string interning** for hot attr lookups (`__anext__`, `body`, handler-call) | LAKESAIL.md:854 | T4 (NEW) | small / low |
| 14 | **Per-worker `Pool<Vec<T>>` scratch buffers** — ripgrep-style reusable header staging via `thread_local!` per tokio worker | RIPGREP.md:247 + 494 | T4 (NEW) | medium / med |

## VALIDATE — patterns that confirm existing Lancelot decisions

| Source | Pattern | Lancelot equivalent |
|---|---|---|
| LAKESAIL.md:836-854 + 897-908 | `PyOnceLock` for one-time Python module init; GIL acquired only for user callable | F4 PyOnceLock memoize; "GIL released across hyper/tokio, reacquired only for handler" |
| POLARS.md:1049 + 593-606 + 580-591 | `py.allow_threads(...)` for CPU-bound; bounded buffers; backpressure-via-channel-depth | M3 R3 `_drain`; bounded mpsc; `await_capacity` |
| LAKESAIL.md:1461-1495 | Bounded mpsc mailbox (cap=8) for actor | `max_concurrent_requests` cap (B1) |
| RIPGREP.md:304-365 + 1087-1089 | mmap OFF by default; honest about envelope | M5 "uvicorn+uvloop wins tiny; const tier for that case" |
| TURBOPUFFER.md:2143-2160 | HTTP/1.1-only at LB to avoid h2 head-of-line | Lancelot http1-only (M3 hardening) |
| DATAFUSION.md:13-29 + 1939-1949 | Engine never owns thread pool; embedder hands `TaskContext::runtime` | `_native.run` takes runtime; user owns the agent |
| DATAFUSION.md:1983-2018 | mpsc-channel streams + `JoinSet`; Drop cancels producers | R3 pump; bounded mpsc; client-disconnect → producer cancel (M4 V2.2) |
| TURBOPUFFER.md:6024-6068 | RAII `InFlightGuard` — gauge correct across `?`, panic, early return | M2 in-flight gauge already RAII-guarded |
| LANCEDB.md:3552-3598 | "Drop the mutex guard before `await`" — never hold sync-mutex across await | M3 R3 same-thread `try_push` + GIL-across-await invariant |
| LANCEDB.md:4318-4339 | Drop closes queue but doesn't await in-flight — let it finish, bounded by deadline | M4 V1.4 graceful drain via `acquire_many_owned` bounded by `LANCELOT_DRAIN_SECS` |
| LANCEDB.md:2628-2630 | `#[pyclass]` wraps `Arc<Mutex<RustStruct>>` for interior mutability | `App` pyclass discipline |
| TURBOPUFFER.md:858-883 + 3142-3180 | Decorrelated exponential backoff w/ 25% jitter; honor `retry-after` | (Tier-D: retry is user-side per M7-plan reject; but VALIDATE if we ever wire a built-in retry) |
| ZED.md:6100-6112 | `Task<T>` drop = cancel; RAII async handles | tokio `JoinHandle` drop |
| ZED.md:4147-4148 | Per-key mpsc fan-in for ordered updates | `@app.batch` per-prefix `_flush` task |

## AVOID — patterns NOT to adopt (Tier-D from these reads)

| Source | Pattern | Why NOT |
|---|---|---|
| POLARS.md:36 + 645-723 | Custom work-stealing executor (not tokio) | Lancelot is HTTP; hyper requires tokio; building a custom executor violates "no useless logic" |
| RIPGREP.md:706-723 | Crossbeam-deque LIFO for parallel filesystem walk | We're request-dispatch, not CPU-walk; tokio's mpsc/JoinSet is the right primitive |
| LANCEDB.md:2609-2624 | Arrow C Data Interface for zero-copy Python↔Rust | Lancelot payloads are JSON/text bytes via pydantic-core, not Arrow batches; adding arrow dep violates pydantic-only runtime rule |
| LANCEDB.md:3607-3611 | `std::sync::LazyLock` for env-driven static config | GIL × OnceLock deadlock memory — only safe when zero py-touch; use `PyOnceLock` for anything Python-adjacent |
| LANCEDB.md:2921-2964 | SaaS control plane (multi-tenancy + ACLs + billing) | Lancelot is a framework, not a SaaS — defer to user-side |
| TURBOPUFFER.md:160-171 | S3 byte-range fetches with SSTable min/max-key metadata | AI logic / storage layer; not byte path |
| ZED.md:4663-4703 | zstd-compressed Protobuf in WebSocket binary frames | Compression is the proxy's job + adding zstd inside `_native` redundant with TLS terminator gzip (per CLAUDE.md Tier-D) |
| ZED.md:6105 | Priority-tagged spawn (e.g. `Priority::RealtimeAudio`) | Out-of-scope for HTTP framework; tokio's single multi-thread runtime + `spawn_blocking` is the right tool |
| TAURI.md:65-100 | Custom URL-scheme transport (`ipc://localhost/cmd`) | Browser/webview-specific; HTTP is already Lancelot's transport |
| TAURI.md:859-877 | Pluggable async-runtime indirection (tokio swappable) | Lancelot is tokio-only by design; runtime-swap layer = useless abstraction with no demand |
| TAURI.md:1490-1502 | Per-session HMAC `Tauri-Invoke-Key` anti-CSRF token | Browser-side; HTTP framework's CSRF surface is different (cookie SameSite + same-origin policy) |
| TAURI.md:280-310 | Capability-based ACL pre-dispatch | Lancelot's auth = user-side middleware; building an ACL = AI-logic-boundary violation + reinventing FastAPI dependencies |

## Cross-cutting findings

1. **Both Polars and Sail use the exact pyo3 patterns Lancelot already does** (PyOnceLock, GIL-release-around-heavy-work, no sync-mutex-across-await). The deadlock-via-`OnceLock` memory we hit was the canonical hazard; we already paid the lesson.

2. **Bounded-channel backpressure** is unanimous across Polars (depth=4 distributor), Sail (mailbox=8), Datafusion (depth=2 mpsc stream), Zed (`peer.rs` 256 / `cfg(test)` 1). The `cfg(test)` buffer=1 trick is the killer feature — would have caught M4's 4-of-4 broken-under-load features.

3. **Drop-as-cancel** is universal: Zed's `Task<T>`, Datafusion's `JoinSet` on stream-drop, LanceDB's I/O scheduler queue-close, Lancelot's R3 `_drive` task. Every async lifetime should be tied to a struct field so Drop guarantees cleanup.

4. **Engine never owns the thread pool**: Datafusion's `TaskContext::runtime` model + Tauri's runtime indirection + Sail's GIL discipline all converge on "embedder hands the runtime in." Lancelot's `_native.run` is this pattern.

5. **`select_biased!` + Drop-guard cleanup** as compile-time-guaranteed invariants is the right answer for graceful shutdown. M4 V1.4 was the THIRD broken feature; `tokio::select! { biased; shutdown => …, work => … }` makes the invariant impossible to forget.

6. **Per-tenant fair memory** (Datafusion `MemoryPool` + `MemoryReservation`) is the missing piece for multi-tenant. OOM error names the top consumer — turns generic 503 into "OOM caused by route=/v1/embed (tenant=acme, 380MB)" with zero new dependencies.

## How this feeds into M7

The M7 plan (`docs/plans/M7-PLAN-DISTRIBUTED-PLUMBING-AND-MCP.md`) gets a new **Track T4 — Rust hot-path discipline** that picks up the 10 cross-cutting / new items above. Items 4 (ForegroundWork), 5 (triple-channel RPC), 6 (per-tenant memory pool), 7 (queue_wait_ms), 8 (x-should-retry) extend existing tracks (T1.4, T1.1, T2). The 14 VALIDATE rows are cited in commit messages where the patterns already shipped (M3/M4 mostly).

Source teardowns referenced:
- `researchfms/teardowns/POLARS.md`
- `researchfms/teardowns/LAKESAIL.md`
- `researchfms/teardowns/RIPGREP.md`
- `researchfms/teardowns/TURBOPUFFER.md`
- `researchfms/teardowns/DATAFUSION.md`
- `researchfms/teardowns/LANCEDB.md`
- `researchfms/teardowns/TAURI.md`
- `researchfms/teardowns/ZED.md`

## Cross-references

- M5 peer source recheck: `docs/peer-source-recheck/M5-peers.md`
- M6 Robyn perf deep-dive: `docs/peer-source-recheck/M6-ROBYN-PERF.md`
- M6 Blazeio perf deep-dive: `docs/peer-source-recheck/M6-BLAZEIO-PERF.md`
- M7 plan (to be extended with Track T4): `docs/plans/M7-PLAN-DISTRIBUTED-PLUMBING-AND-MCP.md`
