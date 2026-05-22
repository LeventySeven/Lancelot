# Lancelot M4 Plan — Prove & Harden

**Date:** 2026-05-22
**Anchor rule (user-restated):** *no bullshit, no useless logic.* Every item below either validates a claim we already make OR fixes a gap that validation exposed. Nothing speculative; no new surface unless a measured gap demands it.

## Why this milestone exists (the M2-M3 reckoning)

M2-M3 shipped the full plan (Tier-A/B/C + the 7 R-swaps) and maxed FastAPI conformance at **974/978 = 99.6%**. Then this session did the honest thing the plan never did: **measured the claimed wins under real load.** Two findings reframe everything:

1. **The "Rust hot-path = faster" premise was only partly true.** A fair `oha` benchmark (uvicorn on its production uvloop+httptools stack, matched worker counts) shows uvicorn+uvloop *beats* our Rust server on small one-shot JSON; the Rust server's real throughput wins are **large-payload (>~4 KB) serialization** and **streaming**, and it **anti-scales with threads** (deploy multiprocess via `SO_REUSEPORT`). See `benchmarks/README.md`.
2. **TWO headline features shipped BROKEN for their real use case** — caught only by under-load validation, because their unit tests exercised the wrong path:
   - **R3 streaming pump:** one cross-thread `call_soon_threadsafe` per chunk → 16-26× slower than Starlette. Re-architected (on-loop drain) → now parity-to-1.3× faster.
   - **`@app.batch` (the marquee):** the timer-window flush cancelled its own task → `CancelledError` → 500 for any **awaiting** handler (i.e. every real one). The tests used non-awaiting handlers. Fixed; validated ~12× throughput / ~16× fewer upstream calls.

**The lesson, and M4's thesis:** *shipped + green unit tests ≠ works under load.* Before building ANY new surface, validate-under-load and harden the wins we already advertise. Two for two were broken; assume the rest are guilty until measured.

## Honest state of the framework (what we can defend today)

| Claim | Status | Evidence |
|---|---|---|
| FastAPI byte-parity | **Proven** 974/978 = 99.6% | `conformance/` ratchet; 4 remaining are documented no-bullshit boundaries |
| Large-payload serialization faster than Python `json.dumps` | **Proven** ~2.3× @ 69 KB | `benchmarks/characterize.sh` crossover sweep (break-even ~1-4 KB) |
| Streaming (Rust `_native`) | **Proven** parity-to-1.3× vs Starlette | `characterize.sh`; bounded backpressure + graceful shutdown verified |
| `@app.batch` coalescing | **Proven** ~12× / ~16× vs rate-limited upstream | `run_batch.sh` (after fixing the self-cancel bug) |
| Small one-shot throughput | **Conceded** — uvicorn+uvloop wins | per-request FFI/asyncio-bridge overhead; not our pitch |
| Resilience (body cap, conn cap, slow-loris, drain, per-route timeout) | **Correctness-tested only** — NOT load/adversarially validated | `tests/test_m3_http_integration.py`, `test_m2_tier_b.py` |
| KV-cache helpers (C1), TransportPool cancel (C2), OTel RED (B2), ContextVar req-id (B3) | **Correctness-tested only** — NOT validated under load | M2 tier tests |

The repositioned pitch (locked in CLAUDE.md): **transport/serialization/streaming for large payloads + AI request coalescing + cloud resilience — NOT a blanket "faster server".**

## M4 items — validate-then-harden (no new features unless a gap forces it)

### Tier-V1 — Resilience under adversarial load (the cloud-native pitch must be real) — ✅ COMPLETE (tag `m4-v1-complete`)

> Status (2026-05-22): all 5 validated against the real server in a subprocess (`tests/test_m4_resilience_load.py`). V1.1/V1.2/V1.3/V1.5 held as claimed; **V1.4 graceful drain was BROKEN** (in-flight requests 500'd on SIGTERM — Robyn-style hard-kill, the B1 anti-goal; the 3rd headline feature to fail under load) and was FIXED (`serve()` now drains via `acquire_many_owned` bounded by `LANCELOT_DRAIN_SECS`). New knobs: `LANCELOT_HEADER_TIMEOUT_MS`, `LANCELOT_MAX_CONNECTIONS`, `LANCELOT_DRAIN_SECS`.


| # | Item | Why | Done = |
|---|------|-----|--------|
| V1.1 | Slow-loris: a client that dribbles request headers byte-by-byte past 15 s is dropped (R6 `header_read_timeout`); a flood of such connections does NOT exhaust memory/FDs. | We advertise slow-loris defense; never load-tested. | A test harness opens N dribbling conns; assert they're closed at the deadline and healthy requests still serve. |
| V1.2 | Body-cap flood: many concurrent >16 MiB uploads → all 413, RSS stays bounded (no per-conn 16 MiB accumulation). | CRITICAL DoS fix (Limited 16 MiB) — verified for ONE request, not under flood. | Measure RSS under the flood; assert bounded + 413s. |
| V1.3 | Connection-cap behavior: >1024 concurrent conns → kernel-backlog backpressure (not crash, not unbounded task spawn); SIGTERM still responsive when saturated. | `MAX_CONNECTIONS` Semaphore + double-select; never load-tested. | Saturate; assert backpressure + SIGTERM drains. |
| V1.4 | Graceful drain under in-flight streams + requests: SIGTERM mid-load → in-flight finish (or bounded-timeout), user generator `finally` runs, no dropped/corrupted bodies. | Partially done this session (stream `finally` verified); extend to mixed load. | Load + SIGTERM; assert clean completion + cleanup. |
| V1.5 | Per-route `timeout=` (B5): handler past deadline → 504, yield-dep teardown runs, slot freed. Validate under concurrency. | Differentiator; correctness-tested only. | Concurrent slow handlers; assert 504s + no leak. |

### Tier-V2 — AI primitives under load (the part that justifies the pitch)

| # | Item | Why | Done = |
|---|------|-----|--------|
| V2.1 | `@app.batch` edge cases under load: multi-key fairness (no head-of-line blocking across keys), `max_keys` cap behavior under a key-flood, error/wrong-length fanout under concurrency. | Marquee; we validated the happy path + fixed one bug — assume more. | Multi-key + key-flood + error-injection load tests. |
| V2.2 | C2 TransportPool cancellation: client disconnect → in-flight upstream LLM call is actually cancelled (stops the $$). Measure that it propagates under load, not just in a unit test. | "Stops $ on unwatched tokens" is a money claim; must be real. | Disconnect mid-call; assert upstream future cancelled + pool slot freed. |
| V2.3 | C1 KV-cache helpers: `dumps_canonical` determinism across types/orderings (property test) + session-affinity header stability; quantify the serialization cost vs plain. | Cache-hit help is a cost claim; verify determinism + overhead. | Property test + a cost micro-measurement (honest, caveated). |

### Tier-V3 — Observability & deployment (make the operator story true)

| # | Item | Why | Done = |
|---|------|-----|--------|
| V3.1 | B2 OTel RED metrics actually emit correct counts/histograms under concurrent load (no double-count, no lost errors). | Dashboards-out-of-the-box claim. | Load + assert metric totals match request outcomes. |
| V3.2 | B3 ContextVar request-id isolation under concurrency (no cross-request bleed across the thread pool / middleware phases). | Trace correctness; the pool-context fix this milestone-family made. | Concurrent distinct req-ids; assert no bleed. |
| V3.3 | Document + validate the **multiprocess `SO_REUSEPORT` deployment** (the established scaling model: N procs × 1 thread, `LANCELOT_WORKERS=1`). A recipe, NOT a CLI (Tier-D). | The benchmark proved this is the ONLY scaling model; users need the recipe. | A `docs/deploy.md` recipe + a smoke test that N procs share the port and scale. |

### Explicitly NOT in M4 (still Tier-D / declined)

- ❌ Small-response Rust re-architecture (run hyper on the asyncio thread to kill the per-request bridge). High-risk, was declined in favor of repositioning. Revisit only if a deployment proves the small-response regime is a real adoption blocker — and then as a measured spike first.
- ❌ Custom JSON serializer, OpenAPI callbacks, built-in CLI, replacing AI logic, hard-kill shutdown, per-chunk stream validation (see CLAUDE.md Tier-D).
- ❌ New AI-primitive surface. M4 hardens what exists; it does not grow the API.

## How each item is validated (the discipline that found the bugs)

Every V-item ships with: (1) a **load/adversarial harness** (oha or a concurrent Python client driving the REAL server in a subprocess — not an in-process unit shim), (2) the metric it must hit, (3) a regression test that exercises the path the bug would live on (e.g. the *awaiting* handler, the *flooded* cap), and (4) green gates (pytest + conformance 974 + clippy/fmt + alloc). If validation finds a bug (precedent: 2/2 so far), fix-first then re-measure.

## Sequencing

1. **Tier-V1 first** — resilience is the cloud-native floor; if a cap/drain is broken under load, nothing else matters. Tag per sub-item or `m4-v1`.
2. **Tier-V2** — the AI primitives are the pitch; C2 (cancellation = money) is the highest-value validate.
3. **Tier-V3** — observability + the deployment recipe close the operator story.

## Definition of "no bullshit" (unchanged, restated)

Every item: validates a claim we already make OR fixes a measured gap; changes the byte path or operator experience (never the AI logic); is exercised under REAL load (subprocess + HTTP), not an in-process shim that can hide the bug. If an item can't name the metric it must hit, it doesn't belong here.

---

**Cross-references:**
- Honest benchmark + Rust envelope: `benchmarks/README.md`, [[lancelot-rust-bench-honest]]
- `@app.batch` validation + bug: [[lancelot-batch-c5-validated]]
- M2-M3 plan (complete): `docs/plans/M2-M3-PLAN-FROM-RESEARCH.md`
- Positioning + perf envelope + Tier-D: `CLAUDE.md`
