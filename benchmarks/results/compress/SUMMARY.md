# M18 R1 gzip — decisive benchmark (KEEP)

Config: single uvicorn worker (uvloop+httptools), 45 KB low-redundancy JSON (LC_BENCH_ITEMS=1000),
gzip level 9, `oha -c 200 -H 'Accept-Encoding: gzip'`. First-hand runs (orchestrator), 2026-06-01.
- A = no compression; B = Starlette GZipMiddleware (held-GIL); C = lancelot.compress (Rust libdeflate via run_in_executor, off-loop GIL release).

## c=200 (3 runs each)
| arm | RPS (3 runs) | p99 |
|---|---|---|
| A no-compress | ~3079 | 0.11 s |
| B Starlette L9 | 695 / 674 / 708 (~692) | ~2.8 s |
| C Rust off-loop L9 | 1537 / 1578 / 1472 (~1529) | ~0.24 s |

**RPS(C)/RPS(B) ≈ 2.2×** (✓ ≥1.5×) · **p99(C)/p99(B) ≈ 0.09×** (✓ ≤0.6×, ~12× better).

## c=1 control (structural-vs-per-call)
B=684 RPS, C=309 RPS → C is per-call SLOWER (run_in_executor thread-hop tax). Confirms the c=200 win is
PURE structural concurrency (GIL release lets peers run), not per-call speed. TRADEOFF: R1 adds per-call
latency at low concurrency.

## Co-tenant starvation (decisive test) — tiny route under /big GIL pressure
B co-tenant: **1.43 RPS** (held-GIL starves peers) · C co-tenant: **230 RPS** (~161× better).

## Verdict: KEEP. Honest envelope:
R1 wins UNDER CONCURRENCY on a single worker with NO compressing proxy (the GIL cliff). It costs per-call
latency at low concurrency (thread hop), and the gain shrinks behind multi-worker uvicorn (separate GILs)
or a TLS-terminating proxy that already compresses. Default off; opt-in via add_middleware.
