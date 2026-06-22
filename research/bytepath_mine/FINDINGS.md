# Lancelot byte-path mine — RANKED FINDINGS (the single build-plan source)

**Role:** findings ranker. This is the one prioritized, **additive, buildable** list a build
plan is written from. Every row survived: (a) dedupe across all 9 domains, (b) drop if Lancelot
already HAS it, (c) drop if on the Tier-D refuse list / violates a cardinal rule.

**Inputs:** `00_BYTEPATH_LENS.md` + 7 synthesis briefs (`10_synthesis/{ai_bytepath, concurrency_workers,
deployment, observability, resilience, serialization, streaming}.md`) + 4 baseline notes
(`05_lancelot_state/{bytepath, resilience, core, research_plans_ledger}.md`) + direct source
re-verification of every load-bearing "MISSING" claim (see "Source verification" below).

**Scoring.** IMPACT (byte-path/operator value, weighted to the AI/cost-effective/streaming/
resilience positioning) · BUILDABILITY (S/M/L) · GATE_RISK (conformance floor 974 / clippy /
perf-regression / pydantic-only runtime) · CONFIDENCE (grounded in a real peer mechanism AND a
real, source-verified Lancelot gap). Ranked by **impact × buildability ÷ risk**.

**Brutal-honesty header.** The streaming / resilience / serialization / observability cores are
**mature and ahead of every peer.** There is **no new headline primitive** in this mine — the
moat (one Rust-owned, backpressured, cancellation-correct in+out byte path) is intact. The wins
are **completions of `@app.batch`** (the one place with real new AI-serving value), **a handful
of source-verified correctness bugs** (readyz false-ready, 499 mislabel, body-stall slow-loris),
and **cheap hardening/observability edges**. Most are S. Two are M (both Rust). Nothing is L that
isn't deferred. Anyone padding this list with "faster serializer / tiny-throughput / circuit
breaker / agent loop" is relitigating a settled REFUSE — see the appendix.

---

## Source verification (done this pass — claims are NOT taken from the briefs alone)

| Claim | Verified in source | Verdict |
|---|---|---|
| `/readyz` has no startup gate | `app.py:314` `_readyz()` checks only `self._shutting_down`; no `_starting_up`/`_lifespan_started`; `lifespan.startup.complete` sent at `asgi.py:228,272` (the hook point) | **GAP real** |
| No TCP keepalive on listen socket | `src/socket.rs`: sets `set_nodelay`, `TCP_QUICKACK`, `TCP_FASTOPEN`, `listen(16384)` — **no `set_tcp_keepalive`/`SO_KEEPALIVE`** | **GAP real** |
| No body-read deadline | `src/service.rs:330` `Limited::new(req.into_body(), max_body()).collect().await` — **no `tokio::time::timeout` wrap**; only `header_read_timeout` exists | **GAP real** |
| `@app.batch` sends all payloads (no dedup) | `batch.py` `_run_batch`: `payloads = [p for p, _ in batch]; results = await self.handler(payloads)` — fans 1:1, no in-batch dedup | **GAP real** |
| 499 anticipated, never produced | `metrics.py:149` comment "status=499 for client-disconnect"; code only records the committed status | **GAP real** |
| `gc.freeze()` not called | grep `gc.freeze` in `lancelot/` → **zero matches** | **GAP real** |
| Azure header not parsed | `llm.py:376-390` `_retry_after_seconds` parses `retry-after` + `retry-after-ms` only; **no `x-ms-retry-after-ms`** | **GAP real** |

Two **doc-honesty** corrections also confirmed against source (serialization brief): ledger line 59
"already have simd-json" is **false** (no simd-json in `Cargo.toml`/`src/`); ledger line 72 "orjson
swap deferred" is **stale** (orjson IS shipped as a soft-dep in `validation.py` + `serialize.rs`).

---

## TIER 1 — BUILD NOW (high impact, S/M, low gate-risk, high confidence)

Ranked. These are the headline of the pass: the `@app.batch` completion bundle (the only real new
AI-serving value) + the source-verified correctness bugs + the one genuine socket-resilience gap.

| # | Win | Domain(s) | Mechanism (1 line) | Source cite | Lancelot gap | Size | Gate-risk | Why it matters |
|---|---|---|---|---|---|---|---|---|
| **T1.1** | **`@app.batch` in-batch dedup** — one representative per identical `dedup_key`, fan result to all waiters | ai_bytepath #1 (streaming improve) | SGLang `schedule_policy.py:160,283` simulated-RadixCache deprioritizes >=32-token-shared prefixes so one runs first; HTTP analog: identical-key waiters reuse one upstream call | SGLang `schedule_policy.py:160,283` | `batch.py` `_run_batch` sends all N payloads (verified) — 100 identical prompts -> 100 upstream calls | M | none (Lancelot-only surface; no conformance/clippy) | **The single highest-value AI item in the whole mine.** Turns ~16x coalescing into **Nx dedup** for repeated-prompt RAG / retry storms / identical polls. Pure cost-effectiveness — the positioning. |
| **T1.2** | **Three-state `/readyz`** — 503 + Retry-After until lifespan `startup` completes | deployment B1 · ai_bytepath #6 · resilience #2 · observability IMP4 | SGLang `/health_generate` + TGI `Warmup` gate traffic until the engine can serve; HTTP translation = the startup window | SGLang `http_server.py:656`; 5-project corpus convergence | `_readyz` returns 200 from registration; only 503 on `_shutting_down` (verified app.py:314). `docs/deploy.md` already *wrongly* claims it's fixed | S | none (`include_in_schema=False`, not a conformance route; pure-Python flag) | **Code-verified deploy-and-roll bug.** Every LB/k8s probe gets false-ready 200 during startup -> routes real traffic to a worker whose DB/LLM client isn't up -> cold 500s on every rollout. |
| **T1.3** | **`@app.batch(max_concurrent_batches=N)`** — pipeline window k+1 while batch k is upstream | ai_bytepath #2 (streaming improve) | Ray Serve `batching.py:439-444` wraps `_process_sub_batches` in `Semaphore(max_concurrent_batches)`, fills next batch while current runs | Ray Serve `batching.py:439`; BentoML `dispatcher.py:253` | `_run_batch` awaited inside `_flush`; next window idle during the 200-800ms upstream call (verified) | S | perf-positive; none on conformance/clippy | Doubles throughput whenever upstream latency > window. Default 1 = exact current behavior. The backpressure anchor for T1.4/T1.5. |
| **T1.4** | **`@app.batch(request_timeout_ms=N)`** — drop stale waiters (504) before the handler runs | ai_bytepath #3 (streaming improve) | BentoML `dispatcher.py:227-230` cancels a queued future past `max_latency`; LitServe `loops/base.py:111-130` returns 504 for over-aged items | BentoML `dispatcher.py:227`; LitServe `base.py:111` | `submit` stores `(payload, Future)` with no timestamp (verified batch.py:95) — stuck items still served under overload | S | none; **same bug-class as the M4 timer-self-cancel -> mandatory real-server awaiting-handler test** | Stops queue run-away under overload. Must set the future *exception* (not skip) or the waiter hangs — the exact M4 failure mode. |
| **T1.5** | **`@app.batch` compute-overload shed** — 503 + Retry-After when the pipeline is saturated | ai_bytepath #4 · resilience #4 | BentoML `dispatcher.py:121-122` raises `ServiceUnavailable(503)` when the concurrency sema is held at the cap | BentoML `dispatcher.py:121` | `BatchOverloadError`->503 fires ONLY on `max_keys` cap (verified batch.py:224); no shed for *compute* pileup | S | none (reuses `BatchOverloadError`->503 path) | Completes the backpressure terminus T1.3's semaphore enables. Bundle PR with T1.3/T1.4. Name the knob in the 503 body. |
| **T1.6** | **TCP keepalive triple on the listen socket** (`SO_KEEPALIVE` + idle/intvl/cnt) | concurrency MB1 · resilience #1 | Blazeio `Utils/socket_ops.py:19-33` + proxy `:493-506` set KEEPALIVE + IDLE=30 + INTVL=10 + CNT=3 -> dead peer reaped in ~60s; Robyn first probe at 2h | Blazeio `socket_ops.py:19`; Robyn `server.rs` | `src/socket.rs` has no keepalive (verified) — half-open conns (k8s evict, Lambda kill, spot term, mobile drop) burn a `MAX_CONNECTIONS` slot indefinitely | S | none (transport-only, socket2 already a dep, `set_tcp_keepalive` returns ignored `io::Result` like the existing setsockopts) | **The one genuine socket-tuning delta vs both peers.** Reclaims conn-cap capacity — the exact resource that gates concurrent AI workloads. ~6 lines + `LANCELOT_TCP_KEEPALIVE_SECS`. |
| **T1.7** | **Emit `499` to RED metrics on a won client-disconnect race** | observability MB1 · streaming S2 · resilience improve | nginx-convention client-closed-request label; vLLM/SGLang treat client-cancel as first-class terminal, not 200/500 | ledger ROUND-2 MED; `asgi.py:599-630`; sentry O3 | disconnect race cancels handler correctly, but `_final_status_holder` stays 500 default -> metric records 500 not 499 (verified metrics.py:149 anticipates, never produced) | S | none (metric label only; **real-server disconnect test required**) | Dashboards currently miscount client aborts as 5xx server errors — the single most important streaming-health signal for an LLM gateway is wrong. |
| **T1.8** | **`gc.freeze()` after lifespan startup completes** | streaming S4 · ai_bytepath I5 | vLLM `server_utils.py:467` `freeze_gc_heap()` before `yield` — startup allocations (routes, validators, OTel instruments, warmed clients) move to permanent gen, never re-scanned | vLLM `server_utils.py:467` | not called anywhere (verified) — route table + validators + middleware scanned on every gen-2 cycle; a mid-stream gen-2 pause is a visible TTFT hiccup | S | none (correctness-neutral; GC still runs, just skips frozen gen). `LANCELOT_NO_GC_FREEZE=1` opt-out | One guarded line; cuts GC-pause jitter on the latency-sensitive stream path. Soak-validate the TTFT-histogram delta (don't ship on TestClient green). |
| **T1.9** | **Decorrelated/full-jitter backoff in `TransportPool`** | resilience improve · ai_bytepath I1 · ledger BUILD | Ray Serve `request_router.py:592` `uniform(T,2T)`; LanceDB decorrelated-jitter — kills synchronized retry spikes | Ray Serve `request_router.py:592`; ledger TIER-3 BUILD | `llm.py:480` adds *additive* jitter on the full `2**attempt` term (verified) — the exponential floor dominates, so 16 fanout retriers re-fire near-lockstep on a 429'd LLM | S | none (no test asserts the exact sleep value) | Thundering-herd on a rate-limited LLM under `@app.batch` fanout is exactly the AI failure mode. One-line change to the sleep formula; preserves `retry_budget_ms`. |

**T1 rationale.** T1.1-T1.5 are one coherent PR (the batch primitive's missing axes — they share
the `_BatchRegistry` semaphore + per-item timestamp; the M4 lesson makes each get a real-server
awaiting-handler test). T1.2 + T1.7 are source-verified correctness bugs (false-ready, mislabeled
metric). T1.6 is the one real socket gap. T1.8 + T1.9 are one-liners with measurable wins. Every T1
item is S except T1.1 (M, pure-Python), conformance-safe, and respects the pydantic-only runtime.

---

## TIER 2 — STRONG, NEEDS VALIDATION (real wins; M-effort or needs a load/bench gate)

| # | Win | Domain(s) | Mechanism (1 line) | Source cite | Lancelot gap | Size | Gate-risk | Why it matters |
|---|---|---|---|---|---|---|---|---|
| **T2.1** | **`LANCELOT_BODY_TIMEOUT_MS` -> 408** (post-header body-stall slow-loris) | deployment B4 · ai_bytepath #5 · streaming S5 | Modal `_runtime/asgi.py:21` `FIRST_MESSAGE_TIMEOUT_SECONDS=5.0` -> synthesizes 408 on body stall | Modal `_runtime/asgi.py:121-160` | only `header_read_timeout`; `service.rs:330` body-collect has no `timeout()` (verified) — headers-complete-body-stalled holds a slot indefinitely, bypassing the header guard | M (Rust) | clippy (Rust on request path) + perf (must be OnceLock-read, zero-cost when off) + **mandatory adversarial test** (headers-then-stall -> 408 + slot release; AND slow-but-progressing upload NOT killed). **Default 0=off** so legit slow uploads unaffected | Closes the one missing slow-loris axis. M not S only because it touches the hyper body path + needs the M4-style adversarial harness before shipping. |
| **T2.2** | **`@app.stream(max_stream_bytes=N)`** — egress output-bytes cap | streaming S1 | SGLang `CLIP_MAX_NEW_TOKENS=4096` (`schedule_policy.py:62`) bounds one runaway request; HTTP analog = bytes-emitted cap | SGLang `schedule_policy.py:62`; re-verdicted serving_frontier §10 | `max_stream_silence` bounds idle, `max_stream_lifetime` bounds wall-clock — **neither bounds size** (verified) — a fast infinite-body gen within the lifetime window has no byte cap | S | none (additive kwarg, off by default, ASGI-seam only — reuses the `max_stream_silence` abort path) + runaway-gen load test | The egress mirror of the `LANCELOT_MAX_BODY` 413 cap. Real DoS axis for the long-lived-SSE AI use case; cheap because it reuses an existing abort path. |
| **T2.3** | **User-extensible metric-attr hook** `lancelot.metrics.set_attr_hook(fn)` | observability MB3 | The STEP-3 instrumentation seam: sentry survives because Starlette exposes scope+send hooks (O1/O2/O3); Lancelot must expose the symmetric seam for its OWN meter | sentry O1/O2/O3; `metrics.py:152-158` | RED attr set is hardcoded; no `set_attr_hook`/`extra_attr` (verified absent) — operators must fork the module to add `model`/`customer_tier`/`feature_flag` labels | S-M | perf (one dict-merge per metrics-enabled request) + **the cardinality guard is load-bearing** (run the returned dict through `_bounded_tenant`-style capping or a careless hook blows up the time-series) | The one genuinely-missing observability seam. Without it the only escape is "re-implement RED in your own middleware," which defeats the built-in. Wrap in try/except — telemetry must never kill a request. |
| **T2.4** | **Python-seam streaming bytes-in-flight cap** (`@app.stream(max_buffered_bytes=)` / env) | resilience #3 | SGLang/TGI never emit more than the pool sustains; Blazeio `pause_writing`/`resume_writing` structural backpressure | serving_frontier §9; Blazeio core M8 | Rust path has `LANCELOT_STREAM_BUFFER` + `MAX_INFLIGHT_BYTES`; **the Python ASGI/uvicorn seam** (the one hosting `@app.stream`/DI/`StreamingResponse`) has no bytes-in-flight bound (verified grep) — a slow SSE reader lets the producer run ahead into uvicorn's buffer | M | perf (must be a single branch gated on `cap>0`, zero cost on default path) + **mandatory slow-reader load test** (recv-then-sleep client -> assert RSS bounded + producer parks) | Completes the backpressure picture on the seam that actually serves streams. Overlaps T2.2 (T2.2 caps total bytes; this caps *resident* bytes under a slow reader) — consider building together. The "stalled client" path the cardinal rule names. |
| **T2.5** | **Truncate (~2 KB) + NUL/CRLF-strip any captured upstream error body** | observability MB4 · ledger BUILD | Polar pattern; extend the request-id CR/LF/NUL+128-char discipline (`asgi.py:457`) to captured upstream bodies | ledger TIER-3 BUILD; `llm.py:498-507` | `upstream_metrics`/`llm.py` parse upstream bodies for tokens; an unbounded/un-sanitized error body is a memory-amplification + log-injection vector (verified: body not currently capped) | S | none (extends existing sanitization; off by default — don't retain bodies on the happy path) | Security + log-injection hardening for the AI-gateway path; off-happy-path so zero cost when not capturing. |
| **T2.6** | **Per-event slow-consumer / backpressure stream metric** (`lancelot.http.stream.backpressure` or `drain_block_ms`) | observability MB6 · streaming S3 | vLLM `RequestOutputCollector` exists *because* fast-producer/slow-consumer is the canonical streaming failure; TGI/SGLang track egress backpressure | serving_frontier §9; `src/stream.rs` `await_capacity` | the mechanism is HAVE (bounded-mpsc `await_capacity` fires only on full) but the **event is never counted** (verified) — operators have no visibility into stream stalls before an OOM incident | S (Python-seam counter) / M (Rust-surfaced) | none (lazy OTel instrument) but **must NOT add a `perf_counter` per chunk when OTel absent** — gate behind the `_maybe_bind() is not None` early check + slow-reader load test | TTFT's twin: time-to-first-*backpressure*. Honest caveat: rarely fires for upstream-rate-limited LLM streams; the win is large-payload/high-throughput streaming where the client is the bottleneck. Cheapest proof of whether the 1-slot coalescing mailbox (REFUSED) is ever needed. |
| **T2.7** | **Method-gate RED metrics** (exclude OPTIONS/HEAD by default) | observability MB2 · ledger BUILD | R2R "log only POST/DELETE" instinct; CORS preflight + HEAD probes double the count and pollute the duration histogram | ledger ROUND-2; R2R S8 | `record_request` fires for every method (verified no filter at `asgi.py:431`) -> OPTIONS preflights skew P99 + inflate the rate panel | S | none, but **default-on changes the metric stream** — document as a behavior change. Keep the in-flight gauge counting everything; gate only counter+duration | `LANCELOT_METRICS_EXCLUDE_METHODS` (default `OPTIONS,HEAD`; empty = count all). Cheap accuracy win for CORS-heavy AI frontends. |
| **T2.8** | **`freelist=128` + 0-param `call0` dynamic-handler fast path** | concurrency MB3 | Robyn `#[pyclass(frozen, freelist=128)]` on `PyEmptyAwaitable` (`callbacks.rs:12`) + `if number_of_params==0 { handler.call0() }` (`executors/mod.rs:168`) skips Request build | Robyn `callbacks.rs:12`, `executors/mod.rs:168` | `LazyRequest`/`ChunkSink` have no `freelist`; dispatch always builds the request + `call1` regardless of arity (verified bytepath.md) | S (freelist) / S-M (arity branch) | **BENCH-GATE: ship only if the oha tiny-shape delta is real** — the GIL-serialized single loop may wash it out (same caveat as mimalloc). Do NOT pitch as a throughput headline (Tier-D) | The only in-mission lever on the one shape Lancelot trails raw Starlette (~88%). Free + correctness-neutral *if* it measures. Drop if neutral. |

**T2 rationale.** Every T2 item is a real byte-path/operator win but earns "needs validation" because
it's M-effort on the Rust/ASGI hot path (T2.1, T2.4), or its value is conditional and must be proven
by a load/bench gate before it ships (T2.6 slow-consumer rarely fires for LLM; T2.8 may wash out
under the GIL). The cardinal-rule-2 "validate on the path the bug lives on" applies hardest here.

---

## TIER 3 — SPECULATIVE / LARGE / DEFERRED (real but lower-priority; don't block T1/T2)

| # | Win | Domain(s) | Mechanism (1 line) | Source cite | Lancelot gap | Size | Gate-risk | Why it's T3 |
|---|---|---|---|---|---|---|---|---|
| **T3.1** | **Mid-stream upstream-failure -> terminal `finish_reason:"error"` SSE frame on the RELAY path** | ai_bytepath #7 · resilience improve | vLLM `chat_completion/serving.py:989-996` — after 200+`text/event-stream`, a mid-stream failure MUST be a terminal SSE frame + `[DONE]` so OpenAI-SDK clients parse both failure modes identically | vLLM `serving.py:989`; LangServe scrub rule `api_handler.py:1213` | `@app.stream(error_frame=True)` covers the *inbound* path; the `TransportPool.relay`/`idle_guarded_chunks` path emits a bare `CancelledError` close on upstream failure after first chunk -> client sees a truncated 200 | S | none (relay is Lancelot-only) | Real client-SDK-compat correctness, but only correct for OpenAI-shaped relays -> must gate behind opt-in `relay(error_frame=True)` and document the envelope shape. Lower frequency than T1 bugs. |
| **T3.2** | **`/fleet/status` `queue_depth` field** | deployment I2 · observability MB5 · ai_bytepath I3 · resilience improve | SGLang `/get_load` -> `{num_requests_waiting, num_requests_running}` so an LB sheds to the less-loaded replica | serving_frontier §17; `fleet.py:57-89` | `/fleet/status` reports `in_flight` but not waiting-behind-the-semaphore depth (the `queue_wait_ms` machinery already tracks the wait) | S | none | One-liner LB signal; ride the next fleet touch. T3 only because it's a nice-to-have, not a bug. |
| **T3.3** | **Honor Azure `x-ms-retry-after-ms`** in `_retry_after_seconds` | observability MB7 · ai_bytepath I2 · resilience improve | Portkey `retryHandler.ts:49` parses the Azure-OpenAI-specific header | Portkey `retryHandler.ts`; `llm.py:380-387` | parses `retry-after` + `retry-after-ms` only (verified) — Azure OpenAI (top-3 backend) silently falls back to exponential backoff and over-waits | S | none | One-line retry-accuracy fix. T3 only by frequency; fold into the T1.9 retry-formula PR (same function). |
| **T3.4** | **Name the responsible knob in every 503/413/429 shed body/header** (`X-Lancelot-Shed-Reason`) | resilience #4 · concurrency I2 · ledger BUILD | Operator-experience principle: a shed response tells which cap fired | ledger ROUND-2 ADAPT; `asgi.py:338,1876,1885` | every shed emits generic `{"detail":"Service Unavailable"}` (verified) — operator can't tell conn-cap from pool-full from batch-overflow from draining | S | none (header only; do NOT leak internal limit numbers — info-disclosure) | Completes the "every shed is graceful + observable" pitch. T3 only because it's polish; bundle into T1.5 (which already names its knob). |
| **T3.5** | **Honor inbound `x-should-retry` in the outbound retry classifier** | resilience improve · ledger BUILD | response-side `set_retry_hint` exists; wiring the *inbound* hint into the retry decision closes the loop | ledger ROUND-2 ADAPT; `llm.py:471` | classifier ignores the inbound hint | S | none | Tiny correctness completion; fold into the T1.9/T3.3 retry PR. |
| **T3.6** | **Readiness-callback hook** `App(readyz_check=fn)` | resilience improve · observability IMP4 · ai_bytepath #6 | 5-project convergence; SGLang `health_generate` deep-probe shape — let the operator gate readiness on their own predicate (DB ping, model warm) | ledger ROUND-2 ADAPT | no user readiness predicate | S | none (verify a user route can still override the builtin — M19b lesson) | Natural pair with T1.2 (framework owns `_starting_up`; this lets the user add a condition). Ship as a hook, not built-in AI logic. Build *with* T1.2. |
| **T3.7** | **Decimal int/float fidelity + `_json_default` table completeness** (`deque`/`Pattern`/`_sa` filter) | serialization #1,#2 | FastAPI `decimal_encoder` (`encoders.py:84`): exponent>=0->int else float; `ENCODERS_BY_TYPE` covers `deque`/`Pattern`/`_sa`-key drop | FastAPI `encoders.py:84-112` | `validation.py:254` always `float(Decimal)` -> `Decimal(2)->2.0`; `_json_default` missing `deque`/`Pattern`/the `_sa` SQLAlchemy-leak filter | S | none (fallback path only; the vendored conformance test imports FastAPI's own encoder -> passes regardless; zero hot-path cost) | Correctness polish on the *untyped* fallback path. The Decimal one-liner prevents silent corruption for financial/untyped APIs; the `_sa` filter kills the classic ORM-serialize footgun. Real but narrow (typed routes already go through pydantic-core). |
| **T3.8** | **Doc-honesty corrections** (simd-json claim; orjson-deferred-is-done) | serialization #3 | — | `Cargo.toml`/`src/serialize.rs` (orjson IS wired, simd-json is NOT) | ledger line 59 falsely says "have simd-json"; line 72 says "orjson deferred" but it's shipped | S | none (docs) | Prevents a future agent from re-deriving orjson or over-claiming SIMD perf. Cardinal-rule-1 hygiene. Do as part of any PR that touches the ledger. |
| **T3.9** | **`python -m lancelot run` entrypoint** (3-line `__main__.py` + copy-paste PaaS docs) | deployment B2 | Railway Railpack emits `python main.py` fallback; a Lancelot app needs `__main__` to call `app.run()` | gateways_deploy §M14 (RAILWAY.md:272) | no `lancelot/__main__.py`/`cli.py` (verified) — Railway/Render zero-config start fails | S-M | **Tier-D wall** — the ledger REFUSES a built-in CLI. ONLY the 3-line env-discovery entrypoint (`LANCELOT_APP=mod:app` -> `app.run()`) + docs. If it grows arg-parsing it has crossed the line — stop | Removes a real PaaS friction point without building the refused CLI/scaffolder. T3 because it rides a Tier-D boundary and must be scoped to one file. |
| **T3.10** | **`bake_global_headers` into the const-tier `Prebaked` response** (opt-in) | deployment I1 | Robyn bakes global headers into each `CachedResponse`'s pre-flattened `Arc<Vec<(name,value)>>` at startup (no-middleware gate) | Robyn `const_router.rs:172-199` | `Prebaked` bakes status+CT+body but not framework/global headers (verified bytepath.md) — a const `/livez` returns a bare response | S | none (startup-time bake; keep default header-bare to preserve the 183k zero-work property) | Operator-convenience, NOT a throughput win — scope accordingly. Lets operators get a consistent header set on the zero-GIL tier if they want it. |
| **T3.11** | **Drain-timing doc rule** (`platform_grace >= LANCELOT_DRAIN_SECS + 10`) | deployment B3 | Render SIGTERM +60s then SIGKILL at `maxShutdownDelaySeconds`; raising `DRAIN_SECS` above platform grace silently re-creates the hard-kill anti-goal | gateways_deploy §M12 (RENDER.md:249) | docs document the drain but never the platform-grace relationship | S | none (docs) | Prevents a silent SIGKILL-mid-drain that undoes the graceful-drain thesis. Docs-only; bundle with T3.9. |
| **T3.12** | **Per-request escape hatch / soft-band for `LANCELOT_MAX_INFLIGHT_BYTES`** | resilience #5 · ledger BUILD | SGLang adaptive admission is the sophisticated version; the cheap version is "don't make the most powerful memory cap all-or-nothing" | serving_frontier §5; ledger ROUND-2 HIGH | `MAX_INFLIGHT_BYTES` sheds flat 503 the instant the budget breaks (verified service.rs) — a `/livez` gets shed alongside a 50MB flood; default 0=off so most never benefit | M | low-perf (one extra atomic load, already in path) + **flood load test** (assert health probes still 200) | Always-exempt const-tier + health routes; optional soft watermark that 429s only sheddable requests. What gets operators to actually *enable* the cap. M because it needs the flooded-cap load test. |
| **T3.13** | **Free-threaded wheels** (`cp313t`/`cp314t`, build-only first) | deployment I3 · ledger U45 | abi3 and free-threaded are mutually exclusive -> a separate matrix arm; the one path that could change the GIL-bound "deploy multiprocess" calculus | ledger §8 U45 | `wheels.yml` builds abi3 only (verified) | M | CI matrix grows; **risk of overselling** — treat the perf claim as a bench-gate, ship as packaging readiness until `python3.13t` measurably lifts the GIL serialization | Forward-looking. Build-only arm + `sys._is_gil_enabled()` assertion now; pitch "scales on threads" ONLY after measured numbers. Genuinely large + speculative -> T3. |

**T3 rationale.** These are real and in-mission but either lower-frequency than the T1 bugs (T3.1,
T3.3, T3.5), pure polish/docs (T3.4, T3.7, T3.8, T3.10, T3.11), ride a Tier-D boundary that must be
scoped tightly (T3.9), or are M/L + speculative (T3.12, T3.13). Several should be *folded into* T1/T2
PRs that touch the same file (T3.3+T3.5+T1.9 = one retry PR; T3.6+T1.2 = one readyz PR; T3.4+T1.5 =
one batch-shed PR) rather than shipped standalone.

---

## REFUSED / already-HAVE — do NOT relitigate (the build plan must not re-propose these)

### Already HAVE (shipped M1-M19, source-confirmed — re-proposing is redundant)
- **Streaming core** — R3 on-loop `_drain` pump (one bridge crossing/stream, same-thread `try_push`,
  GIL-dropped per chunk, event-driven `await_capacity` backpressure, `LANCELOT_STREAM_BUFFER`); SSE
  framing + `[DONE]` sentinel; `stream_interval`; `defer_status` eager-first-chunk;
  `max_stream_lifetime` reconnect sentinel; `max_stream_silence` idle-abort (separate DATA/PING
  tracking); single-task 15s keepalive; SSE anti-buffering headers; mid-stream `error_frame` (inbound)
  + guaranteed `[DONE]`; per-item JSONL `dump_json`; SSE id/name CR/LF/NUL strip. **Ahead of every peer.**
- **`@app.batch` core** — window/count/`size_fn`-budget flush, `key=` bucketing, `max_keys`->503,
  defer-to-tail, timer-self-cancel fix, BaseException fanout. (T1.1-T1.5 add the missing axes.)
- **Resilience** — body cap->413 (`MAX_BODY`), conn cap->503 (`MAX_CONNECTIONS`), in-flight-bytes
  budget (`MAX_INFLIGHT_BYTES`, AtomicI64+RAII), graceful drain (`acquire_many_owned`, NOT hard-kill),
  slow-loris **header** deadline (`HEADER_TIMEOUT_MS`), per-route `timeout=`->504, client-disconnect->
  cancel->upstream-abort (dual-task race, NOT `is_disconnected()` polling), bounded sync offload->503,
  `TenantConcurrencyLimiter`, inbound `RateLimit`->429, SSRF guard, webhook sign/verify, multipart/
  query DoS caps, JSON depth guard, cookie-bomb/CRLF defense, path-traversal guard, `MemoryPool`.
- **TransportPool** — retry (POST never, `retry_budget_ms`, Retry-After + `retry-after-ms` honored,
  jitter), Idempotency-Key, traceparent+request-id+agent-id propagation, cancel-on-disconnect
  (unary+streaming), `idle_guarded_chunks`+`relay` (streamed cost/token/TTFT scan), `fanout`,
  `spawn_child`, mTLS + SIGHUP reload, `_TokenBucket`. (T1.9 sharpens jitter; T3.1/T3.3/T3.5 are edges.)
- **Observability** — RED metrics (route-template labels, `http.status_code` on every emit), dual TTFT
  (inbound stream + outbound upstream), cost/token ContextVars + `X-Lancelot-Cost-USD`, 8-provider
  upstream taxonomy, queue_wait_ms histogram, request-id correlation, W3C traceparent (no OTel dep),
  telemetry `set_sink` seam, tenant-label cardinality cap, fleet rollup + region tagging, OTel-is-never-
  a-runtime-dep. (T1.7/T2.3/T2.5/T2.6/T2.7 are precise edges, not a new system.)
- **Serialization** — pydantic-core `dump_json` typed 200 fast path (the ~2.3x@69KB marquee),
  `response_model_skip_validation` (hard-fenced), orjson soft-dep (Python + Rust), one serializer for
  200-untyped AND 422, `_json_default` fallback, `PyOnceLock` GIL-safe memoize, `canonical.dumps_canonical`,
  204/304 body-wipe, ORJSONResponse/UJSONResponse pass-throughs. **The "faster serializer" space is closed.**
- **Concurrency** — multi-thread tokio runtime, uvloop embedded loop (soft-dep + `LANCELOT_LOOP`),
  GIL-across-await is a *compile error* (`await_holding_lock=deny`), bounded-sema+task-per-conn accept,
  graceful drain, cgroup-aware workers, `SO_REUSEPORT`+QUICKACK+FASTOPEN+backlog-16384, Python-side
  bounded sync pool + ContextVar propagation, `panic="unwind"`+`catch_unwind` (deliberate, NOT a gap),
  lock-free post-build router. (T1.6 adds keepalive; T2.8 is a bench-gated micro.)
- **Deployment** — abi3 wheels CI (5 platforms, Trusted Publishing), `$PORT`/`LANCELOT_HOST` binding,
  multiprocess matrix (`SO_REUSEPORT` Linux-only documented), lean cold-start, honest platform matrix,
  secrets (5 lazy backends + SIGHUP), full env-knob inventory, const-tier `/livez`+`/readyz`+`/fleet/status`.
  (T1.2 fixes the readyz startup gate; T3.9/T3.10/T3.11 are edges.)
- **`Response.set_session_affinity`**, **`lancelot.mcp.mount`** (minimal-return WARN), **`@app.tool`
  output_schema**, **region-pinned routes**, **`@app.get(region=)`**.

### REFUSED (Tier-D / cardinal-rule / boundary violation — sourced rejections; do not propose)
| Item | Why refused |
|---|---|
| Custom JSON serializer (orjson/ujson/**simd-json**/Arrow/pickle/msgpack/CBOR) | pydantic-core already Rust-fast; orjson already a soft-dep; orjson/ujson deprecated in FastAPI. Cardinal rule 3. (simd-json "already have" claim is *false* but the conclusion "don't build" stands -> T3.8.) |
| Claim small-request/tiny-handler throughput as the selling point | uvicorn+uvloop wins there; const tier (183k) is the answer for fixed responses. Tier-D. (T2.8 bench-gated, never pitched as headline.) |
| Regress R3 pump to per-`__anext__` / per-chunk `spawn_blocking` bridging | Re-creates the 16-26x cliff. The on-loop `_drain` + same-thread `try_push` must stay. Tier-D. |
| Optimize `@app.stream` per-chunk overhead | LLM token streaming is upstream-rate-limited (~50-200 tok/s); the framework is never the bottleneck. Tier-D. |
| Per-chunk Pydantic validation on streams | Perf cliff. Streaming is raw-bytes + lightweight typing. Tier-D. |
| Per-chunk / streaming gzip · Brotli | Per-chunk compression banned; `GZipMiddleware` (one-shot, off-loop) is the right shape; Brotli = second dep + negotiation branch (proxy's job). Tier-D. |
| Failure-rate **circuit breaker** on `TransportPool` | `TransportPool` is single-provider per instance; cooldown belongs to a future `app.llm` router layer. Explicit Tier-D / M14 deliberate omission. POST-never-retry + `retry_budget_ms` already prevent the cascade. |
| Provider request/response **transform** layer (OpenAI<->Anthropic, tool-name sanitize, reasoning_effort->budget) | AI-logic; "AI logic stays user-side." Document the quirks, build nothing. |
| OpenAI-compat **model gateway / model routing / multi-key rotation / P2C live-queue routing / TTFT-latency provider selection / cache-warm key stickiness / per-replica LRU model cache** | Multi-deployment LB logic = `app.llm`/operator's LB + `SO_REUSEPORT` multiprocess. Out of scope for the single-provider transport pool. |
| Adaptive SLA-aware batch window / dynamic model batching / **CorkDispatcher learned (numpy lstsq) batching** / chunked-prefill / `new_token_ratio` admission / FSM-grammar token masking / weight hot-swap / `pause_generation` | Model-server KV/scheduler logic + numpy dep (cardinal rule 3). Lancelot doesn't own the KV cache. The non-learned axes (T1.3/T1.4/T1.5) give the real win without the dep. Tier-D. |
| **Streaming batch demux** (per-caller generator fan from one batched generator) | Crosses into AI-logic; the throughput win (fewer upstream calls) is already delivered by `@app.batch` + T1.1 dedup. Tier-D. |
| `RequestOutputCollector` **1-slot coalescing mailbox** | Only helps when producer faster than consumer, which LLM streaming never is. Bounded-mpsc already gives correct backpressure. DEFERRED/speculative; T2.6 is the cheap proof of whether it's ever needed. |
| **`@app.job` durable async-task-poll + persisted store** / Snapshot-restore / durable execution | Durable/checkpointed execution = orchestration = explicit Tier-D. The 202+poll *shape* is fine; the persisted store crosses the line. Not this pass. |
| Granian-style **custom awaitables** (`PyIterAwaitable`/`PyFutureAwaitable`) | Large build+maintenance for a bridge-isolation win on the one shape Tier-D forbids pitching; washes out on AI workloads (LLM latency dominates). Revisit only if a free-threaded build lands. |
| Dedicated **Rust crossbeam sync pool** / built-in **multiprocess spawner / process supervisor** (respawn loop) / `io-uring` runtime / `dill`-fork+pickled-socket | Lancelot's Python `BlockingPool` + no-fork "N independent processes + `SO_REUSEPORT`" + systemd/k8s supervision are deliberate, cardinal-rule-clean, and ahead of Robyn. Owning process lifecycle edges into the refused CLI territory. Ship docs (T3.9/T3.11), not a Rust supervisor. |
| `panic="abort"` in the release profile | Lancelot deliberately uses `panic="unwind"` + `catch_unwind` so an FFI panic -> Python exception, not process abort. The mine mis-tagged this as a gap. Do NOT "fix" it. |
| Built-in **CLI** (`lancelot run ...`) / full `lancelot init` scaffolder + `templates/` | FastAPI delegates to fastapi-cli; uvicorn works. T3.9 keeps ONLY the 3-line `__main__` entrypoint + docs. Anything more = Tier-D. |
| Bundle an **OTel/Prometheus exporter** as a runtime dep / re-implement OTel spans / built-in structured access log / cross-worker metric aggregation + leader election | Cardinal rule 3 (pydantic-only runtime). The correct shape is T2.3's hook + `set_sink` + lazy-bound meter; the operator installs their own exporter. uvicorn already emits access logs. |
| Local tiktoken / heuristic token estimation / **cost RATE table shipped in Lancelot** | Provider-reported tokens only; rates change weekly (stale table silently diverges from billing). Ship the accumulator; operator supplies rates. |
| `Last-Event-ID` SSE replay / resumable streams / `POST /abort_request` by-id / engine-death watchdog / `Page[T]` envelope / `X-Priority` priority-queue / global Redis rate-limit backend / TLS-on-the-server / h2c / Windows / TypeScript SDK | Stateful resume = user-side; abort-by-id needs a control surface with no byte-path win (`cancel_on_disconnect` covers the common case); watchdog = k8s liveness; `Page[T]` = `fastapi-pagination`; priority = LB policy; Redis/TLS/h2c/Windows/TS = explicit Tier-D + documented divergences. |

---

## Counts per tier

- **T1 (build-now): 9** — T1.1 batch dedup · T1.2 readyz startup gate · T1.3 batch pipeline ·
  T1.4 batch per-item timeout · T1.5 batch overload shed · T1.6 TCP keepalive · T1.7 emit-499 ·
  T1.8 gc.freeze · T1.9 decorrelated jitter.
- **T2 (strong, needs validation): 8** — T2.1 body timeout->408 · T2.2 max_stream_bytes · T2.3
  metric-attr hook · T2.4 Python-seam stream bytes-in-flight · T2.5 truncate upstream body · T2.6
  slow-consumer metric · T2.7 method-gate metrics · T2.8 freelist+0-param (bench-gate).
- **T3 (speculative / large / deferred): 13** — T3.1 relay error frame · T3.2 queue_depth · T3.3
  Azure header · T3.4 name-the-knob · T3.5 x-should-retry · T3.6 readyz_check hook · T3.7 Decimal +
  encoder table · T3.8 doc honesty · T3.9 `__main__` entrypoint · T3.10 const-tier headers · T3.11
  drain-timing doc · T3.12 inflight-bytes escape hatch · T3.13 free-threaded wheels.

**Total surviving findings: 30** (after dedupe from ~50 raw must_build/improve hits across 9 domains;
~20 collapsed into the items above as duplicates; the entire "faster serializer / circuit breaker /
model gateway / agent loop / durable job / custom awaitables" class dropped to REFUSED).

**Suggested PR grouping for the build plan:**
1. **Batch-completion bundle** (T1.1+T1.3+T1.4+T1.5, + T3.4 name-the-knob) — one PR, the headline,
   each with a real-server awaiting-handler test (M4 lesson).
2. **Readyz + readiness hook** (T1.2 + T3.6) — one PR, fixes the verified false-ready bug + the doc.
3. **Socket + metrics correctness** (T1.6 keepalive, T1.7 emit-499, T2.7 method-gate) — small, mostly
   independent; T1.6 + T2.1 are the two Rust touches (gate together on clippy + adversarial tests).
4. **Retry sharpening** (T1.9 jitter + T3.3 Azure header + T3.5 x-should-retry) — one PR, same function.
5. **One-liners** (T1.8 gc.freeze, T3.7 Decimal, T3.8 docs) — fold into any pass.
6. **Validation-gated** (T2.1, T2.2, T2.4, T2.6, T2.8) — each needs its named load/bench test before merge.

Gate every PR: `pytest tests/ -q` + `make m1-conformance` (floor **passes=974**) +
`cargo clippy --release --all-targets -- -D warnings` + fmt. Only T1.6 + T2.1 (+ T2.8 arity branch)
move the Rust gate; everything else is Python-only / docs and conformance-safe.

---

## ADDENDUM — routing_request + dx_conformance (late domains, integrated 2026-06-22)

**Sources:** `research/bytepath_mine/10_synthesis/routing_request.md` (routing engine / request
lifecycle / const tier / lazy proxy) + `research/bytepath_mine/10_synthesis/dx_conformance.md`
(developer experience / FastAPI byte-parity / adoption friction). These two briefs landed after the
original ranker ran; this addendum integrates their net-new findings, deduped against the 30 items
already ranked above.

**Brutal-honesty header.** The routing/request domain confirmed that Lancelot is already ahead of all
peers on every structural axis (matchit slot router, lazy R2 proxy, const tier, pydantic-core fast
path, GIL-dropping R3 pump). The DX/conformance domain surfaces the one remaining migration-friction
cluster: OpenAPI output quality, security-scheme import coverage, and two correctness correctness bugs
in the DI/serialization seam. All net-new items here are S. None touch the Rust gate. Nothing new is
L or headline-throughput.

---

### ADDENDUM SOURCE-VERIFICATION

| Claim | Verified in source | Verdict |
|---|---|---|
| SSE routes emit `application/json` in OpenAPI (not `text/event-stream`) | `lancelot/openapi.py` — `build_openapi` emits `200: {content: {application/json:...}}` unconditionally; no `stream=True` detection | **GAP real** |
| `build_openapi` drops `servers`/`description`/`openapi_tags` | `openapi.py:314` signature `build_openapi(app, title, version)` — no `description`/`servers`/`tags` params | **GAP real** |
| `OAuth2AuthorizationCodeBearer` missing from `lancelot.security` | `lancelot/security.py:4` quarantine comment confirms absence; grep confirms no such class | **GAP real** |
| `contextmanager_in_threadpool` not present | `lancelot/di.py` grep for `CapacityLimiter` → zero matches | **GAP real** |
| `Event.retry` field missing | `lancelot/stream.py:24–31` `Event` dataclass has `name`, `data`, `id` only | **GAP real** |
| SSE missing `pragma: no-cache` / `connection: keep-alive` / `expires: 0` | `asgi.py:1041–1042` confirmed: only `cache-control: no-cache` + `x-accel-buffering: no` | **GAP real** |
| `FileResponse`/`RedirectResponse` re-export | `lancelot/responses.py:167–172` — lazy re-exports SHIPPED (M20 Batch 1) | **DONE** |
| `inspect.signature` eval_str | `lancelot/di.py:116,122` uses `typing.get_type_hints(fn, include_extras=True)` which already evaluates stringified annotations; the `inspect.signature` calls are for parameter introspection only (names/defaults), not type resolution | **FALSE-POSITIVE** |
| Swagger/ReDoc XSS via unescaped OpenAPI JSON | `lancelot/openapi.py` docs rendering — `html.escape` + `json.dumps` already in use; `json.dumps` Unicode-escapes `<>/&` by default | **FALSE-POSITIVE** |

---

### ADDENDUM TIER 1 — BUILD NOW (net-new DX/conformance correctness, all S, all Python-only)

| # | Win | Domain(s) | Mechanism (1 line) | Source cite | Lancelot gap | Size | Gate-risk | Why it matters |
|---|---|---|---|---|---|---|---|---|
| **A-T1.1** | **SSE media type in OpenAPI** — `@app.stream` routes emit `text/event-stream` (or `application/jsonl` for JSONL mode) in the generated spec | dx_conformance must_build #1 | Detect `stream=True` flag on the route object in `build_openapi`; emit `text/event-stream` + `{type:string}` schema (or `application/jsonl` + item schema for JSONL mode) instead of the default `application/json` | FastAPI `openapi/utils.py:357–409`; `lancelot/openapi.py:build_openapi` | MISSING — `build_openapi` emits `application/json` unconditionally; `@app.stream` routes are not detected (verified) | S (~25 lines) | LOW — OpenAPI output only; no hot-path impact; run conformance to verify no vendored test asserts the old shape | **SDK codegen correctness.** Speakeasy/openapi-typescript/Stainless generate broken one-shot JSON clients for streaming endpoints when the spec says `application/json`. This is the marquee feature: a wrong spec poisons every SDK consumer silently. |
| **A-T1.2** | **`servers` / `description` / `openapi_tags` forwarding in `build_openapi`** | dx_conformance must_build #2 | Add `description`, `servers`, `openapi_tags` kwargs to `App.__init__` + pass through to `build_openapi`; merge into the returned OpenAPI dict | FastAPI `openapi/utils.py:514–530`; `lancelot/openapi.py:314` | MISSING — `build_openapi(app, title, version)` has no `description`/`servers`/`tags` params; App constructor does not store them (verified) | S (~12 lines) | LOW — additive to the schema; existing tests do not assert the absence of these keys | **SDK codegen base URL.** Without `servers`, every generated client targets `localhost`. Without `tags`, operation grouping in Swagger UI and readme pages is missing. |
| **A-T1.3** | **`OAuth2AuthorizationCodeBearer` in `lancelot.security`** | dx_conformance must_build #3 | Thin stub class (`authorizationUrl`, `tokenUrl`, `scopes` kwargs → OpenAPI `oauth2/authorizationCode` flow); no network calls; same pattern as `OAuth2PasswordBearer` | FastAPI `security/oauth2.py:547–650`; `lancelot/security.py` quarantine comment | MISSING — confirmed absent, listed in quarantine comment (verified) | S (~20 lines) | LOW — pure surface addition; no routing or hot-path touch; run conformance to confirm no vendored-test collision | **Migration blocker.** Auth0/Okta/AWS Cognito — the three most common OIDC providers — all use Authorization Code flow. Any FastAPI app authenticating with these providers fails at import time. |

---

### ADDENDUM TIER 2 — STRONG, NEEDS VALIDATION (touches yield-dep teardown; must run all 11 yield-dep conformance tests)

| # | Win | Domain(s) | Mechanism (1 line) | Source cite | Lancelot gap | Size | Gate-risk | Why it matters |
|---|---|---|---|---|---|---|---|---|
| **A-T2.1** | **`contextmanager_in_threadpool` with unlimited `CapacityLimiter` for sync yield-dep `__exit__`** | dx_conformance must_build #6 | Port FastAPI's `concurrency.py:17–41`: wrap sync contextmanager `__exit__` in `anyio.to_thread.run_sync(..., limiter=CapacityLimiter(math.inf))` to bypass the default 40-slot anyio thread pool limit on cleanup | FastAPI `concurrency.py:17–41`; `lancelot/di.py:704–711` | MISSING — `di.py` has no `contextmanager_in_threadpool`; sync yield-dep `__exit__` runs via `anyio.to_thread.run_sync` without a custom limiter (verified: zero `CapacityLimiter` hits) | S (~15 lines) | MEDIUM — touches yield-dep teardown; must pass all 11 yield-dep conformance tests post-change; `anyio` is already a dep (Starlette uses it) — no new runtime dep | **Real deadlock risk under load.** With 40+ concurrent requests each with a DB-connection-holding sync yield-dep exiting simultaneously, the default anyio pool saturates → all cleanup threads queue behind each other while holding connections/locks → event loop stalls. FastAPI resolved this with an unlimited limiter on the teardown path specifically. |

---

### ADDENDUM TIER 3 — DEFERRED / POLISH (real but lower-priority; fold into existing PRs where noted)

| # | Win | Domain(s) | Mechanism (1 line) | Source cite | Lancelot gap | Size | Gate-risk | Why it's T3 |
|---|---|---|---|---|---|---|---|---|
| **A-T3.1** | **`Event.retry` field** — emit `retry: {ms}\n` in SSE frames when set | dx_conformance I1 | Add `retry: int \| None = None` to `Event` dataclass; `render()` emits `retry: {ms}\n` when set | Robyn `robyn/responses.py:167–206`; SSE spec; `lancelot/stream.py:24–31` | MISSING — `Event(name,data,id)` has no `retry` field (verified) | S (~5 lines) | VERY LOW — additive to `Event`; no existing tests assert absence of `retry` | Clients using browser `EventSource` (Cursor, Claude.ai, most AI streaming frontends) use `retry:` to tune reconnect behavior. Without it, clients use browser defaults (typically 3s). Ship as a standalone micro-PR or fold into next stream touch. |
| **A-T3.2** | **`OpenIdConnect` stub in `lancelot.security`** | dx_conformance I3 | `class OpenIdConnect(SecurityBase)` with `openIdConnectUrl` kwarg → OpenAPI `openIdConnect` scheme; no validation logic (same as FastAPI's stub) | FastAPI `security/open_id_connect_url.py`; `lancelot/security.py` | MISSING | S (~15 lines) | VERY LOW | Import-compatibility stub; some FastAPI apps import it for spec generation only. Fold into A-T1.3 (same file, same quarantine comment). |
| **A-T3.3** | **`OAuth2PasswordRequestFormStrict` in `lancelot.security`** | dx_conformance I4 | Variant of `OAuth2PasswordRequestForm` with `grant_type` field `Literal["password"]`; rejects non-password grant types | FastAPI `security/oauth2.py:162–327`; `lancelot/security.py` | MISSING | S (~5 lines) | VERY LOW | Import-compatibility stub. Apps following strict OAuth2 spec import it. Fold into A-T1.3 (same file). |
| **A-T3.4** | **`HTTPDigest` stub in `lancelot.security`** | dx_conformance I5 | `class HTTPDigest(HTTPBase)` with `scheme="digest"`; raises 401 if `Authorization: Digest` absent; registers OpenAPI security scheme | FastAPI `security/http.py:319–417`; `lancelot/security.py` | MISSING | S (~10 lines) | VERY LOW | Import-compatibility stub; actual digest challenge-response is user-side. Fold into A-T1.3 (same file). |
| **A-T3.5** | **`get_api_router_class(route_class)` factory utility** | dx_conformance I6 | 10-line pure-Python factory that returns an `APIRouter` subclass with `route_class` locked; propagates a custom `APIRoute` subclass across a module hierarchy without per-`include_router` repetition | Polar `polar/kit/routing.py:187–198`; `lancelot/routing.py` | MISSING — `route_class` kwarg HAVE; factory NOT present | S (~10 lines) | VERY LOW — does not touch routing internals | Ergonomic complement to the already-shipped `route_class` kwarg. Ships standalone or folds into any routing touch. |
| **A-T3.6** | **SSE `pragma: no-cache`, `expires: 0`, `connection: keep-alive` headers** | dx_conformance I8 | Complete the anti-buffering header set beyond `x-accel-buffering: no` + `cache-control: no-cache` already present | Robyn `response.rs:107–117`; `lancelot/asgi.py:1041–1042` | PARTIAL — missing `pragma: no-cache`, `expires: 0`, `connection: keep-alive` (verified) | S (~3 lines in the SSE header set) | VERY LOW — additive headers | HAProxy + older CDN edges check `Pragma: no-cache`; Safari `EventSource` uses `Connection: keep-alive`. Fold into next SSE or stream touch. |
| **A-T3.7** | **`PydanticUnsupportedFieldAttributeWarning` suppression for pydantic ≥2.12** | dx_conformance I11 | Wrap `TypeAdapter(...)` calls in `warnings.catch_warnings()`; filter category to suppress spurious warnings on startup for routes with `Field(...)` deprecated attrs | FastAPI `_compat/v2.py:141–166`; `lancelot/di.py`, `lancelot/validation.py` | MISSING — no suppression in either file | S (~5 lines) | VERY LOW — correctness-neutral | Prevents log pollution for users on pydantic 2.12+. Fold into any pass touching `di.py` or `validation.py`. |
| **A-T3.8** | **Single-pass header scan in ASGI seam** — fold `_scan_request_context` + content-type sniff into one `_scan_headers` pass | routing_request 2.6 | `asgi.py` iterates `scope["headers"]` twice per request (lines ~353 and ~815–822); one unified pass halves header iterations (~10–20 comparisons/request saved = ~590k–1.18M/s at 59k req/s) | `lancelot/asgi.py:353,815–822` | PARTIAL — two independent scans (verified by route_request brief) | M | LOW — behavior-identical refactor; gate on `pytest tests/ -q` + conformance | Not urgent (header scan is not a bottleneck); file as a medium cleanup PR. |
| **A-T3.9** | **`strip = true` in Cargo.toml `[profile.release]`** | routing_request 4.2 | Removes debug symbols from the `.so` (smaller binary; Robyn + Granian both strip); no behavioral change | Robyn `Cargo.toml:44–53`; `lancelot/Cargo.toml` | MISSING (`[profile.release]` has no `strip` key) | S (1 line) | NONE — strips symbols from binary only; `cfg(debug)` builds unaffected | Fold into any next Rust rebuild. |
| **A-T3.10** | **Document SSE `metadata/data/end/error` event envelope recipe** | dx_conformance I12 | Add recipe to `docs/operator/streaming.md`: the four-event SSE envelope used by LangServe/R2R/most AI streaming APIs | dx_conformance I12; `02_fastapi/proj_auth_obs_ai.md §LangServe/R2R SSE pattern` | MISSING as documented recipe | S (docs) | NONE | Without it, every Lancelot AI-streaming user re-invents the envelope inconsistently. No code change. |
| **A-T3.11** | **Document cursor-leak hazard on `@app.stream` + SQLAlchemy server-side cursors** | dx_conformance I13 | Note in `@app.stream` docs: generators holding DB cursors MUST close them in `finally:` — R3 pump leaves a disconnected-client generator suspended (≤1 buffered chunk) | dx_conformance I13; `02_fastapi/proj_dbweb.md §RepositoryBase.stream` | UNDOCUMENTED | S (docs) | NONE | Prevents a real production resource-leak pattern confirmed in the FastAPI corpus. Fold into T3.10 (same doc file). |

---

### ADDENDUM RECONCILIATIONS

**Already shipped in M20 Batch 1 — mark DONE, do not propose:**
- `FileResponse`/`RedirectResponse` lazy re-export in `lancelot.responses` — shipped; `responses.py:167–172` confirmed. DONE.
- `Decimal` primary wire path as precision-preserving string (pydantic-core) — deliberate divergence. The `Decimal` int/float *fallback* fix (`validation.py:254`) is already captured as **T3.7** in the main findings and remains there.

**Verified false-positives — mark REJECTED:**
- `di.py` "inspect.signature eval_str" (dx_conformance must_build #5) — REJECTED. Lancelot resolves annotations via `typing.get_type_hints(fn, include_extras=True)` at `di.py:116,122`, which already evaluates stringified annotations. The `inspect.signature` calls are for parameter introspection only (names/defaults). No gap.
- `docs.py` "XSS via unescaped OpenAPI JSON" (dx_conformance I2) — REJECTED. `json.dumps` Unicode-escapes `<`, `>`, `&` by default (`ensure_ascii=True`), and `html.escape` is already applied in the docs rendering path. The injected spec JSON is safe.

**Deduped against existing findings (already ranked above):**
- `Decimal` int/float fidelity — DEDUPED. Already **T3.7** (exact same fix: `validation.py:254`, exponent-check one-liner). dx_conformance #4 and routing_request §2.5 are the same item.
- Azure `x-ms-retry-after-ms` — DEDUPED. Already **T3.3** (source-verified in the original pass; same `llm.py:376–390` fix). dx_conformance I14 is the same item.
- `/readyz` `starting_up` third state — DEDUPED. Already **T1.2** (code-verified false-ready bug). dx_conformance I9 is the same item.
- `finish_reason:"error"` terminal SSE frame — DEDUPED. Already **T3.1** (relay path, opt-in `relay(error_frame=True)`). dx_conformance I10 is the same item (scope note: T3.1 covers the `TransportPool.relay` path; dx_conformance scopes to the R3 pump error path — same architectural fix, same T3.1 resolution).
- `freelist=N` on `Request` pyclass — DEDUPED. Already in **T2.8** (bench-gated micro-opt).

---

### GRANIAN TENSION RESOLUTION

**routing_request §1.2** proposes the Granian-style custom awaitable bridge (`PyIterAwaitable`/`PyFutureAwaitable`) with an explicit bench-gate: "if gain < 5% end-to-end, defer; if gain ≥ 10%, ship."

**Existing FINDINGS REFUSED:** "Large build+maintenance for a bridge-isolation win on the one shape Tier-D forbids pitching; washes out on AI workloads (LLM latency dominates). Revisit only if a free-threaded build lands."

**Resolution: KEEP REFUSED.** The routing_request brief's own honest-caveats section validates the existing verdict: the 38–55% claim is bridge-isolation, not end-to-end; end-to-end on Lancelot's 59k req/s baseline is estimated at "10–25%" at best. The shape where this helps (tiny, GIL-dominated, no upstream I/O) is the shape FINDINGS Tier-D explicitly prohibits pitching as a throughput win (uvicorn+uvloop wins that shape; const tier is the answer for fixed responses). The M/L implementation cost + correctness-sensitive bridge replacement + maintenance burden is not justified by a bench-conditional 10–25% on the one shape that's not the positioning. **If and only if free-threaded wheels (T3.13) land and a real Lancelot oha bench shows ≥10% on tiny-handler shape, re-open.** Until then: REFUSED.

---

### ADDENDUM COUNTS

Net-new items (after dedup, false-positives removed, shipped items marked DONE):

- **T1 additive: +3** — A-T1.1 SSE media type in OpenAPI · A-T1.2 `servers`/`description`/`openapi_tags` · A-T1.3 `OAuth2AuthorizationCodeBearer`
- **T2 additive: +1** — A-T2.1 `contextmanager_in_threadpool` CapacityLimiter
- **T3 additive: +11** — A-T3.1–A-T3.11 (security stubs, SSE headers, header-scan cleanup, strip, docs)

**Revised totals: T1 = 12, T2 = 9, T3 = 24. Grand total: 45 surviving findings.**

**Suggested PR grouping for addendum items:**
1. **OpenAPI quality** (A-T1.1 + A-T1.2) — one PR; Python-only; run conformance gate.
2. **Security surface completion** (A-T1.3 + A-T3.2 + A-T3.3 + A-T3.4) — one PR; all in `lancelot/security.py`; the quarantine comment drives them.
3. **`contextmanager_in_threadpool`** (A-T2.1) — standalone PR; gate on all 11 yield-dep conformance tests.
4. **SSE completions** (A-T3.1 Event.retry + A-T3.6 SSE headers) — fold into next stream touch.
5. **Ergonomics + micro** (A-T3.5 router class factory + A-T3.7 pydantic warning + A-T3.9 strip) — fold into next general pass.
6. **Docs** (A-T3.10 + A-T3.11) — fold into next `docs/operator/streaming.md` touch; no gate needed.
