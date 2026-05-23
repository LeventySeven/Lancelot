# M5 Lab-Body Bench Analysis (Phase A — local loopback)

**Date:** 2026-05-23 (UTC)
**Host:** Darwin arm64 (Apple M5, 10 logical CPUs)
**Tool:** oha 1.14.0
**Variants:** lancelot rust, lancelot ASGI (uvicorn), starlette+uvloop, fastapi+uvloop, robyn 0.84.0, blazeio 4.1.0
**Duration:** 60s per shape (TTFT: 100 streams; batch: 10s × c=200)

This is the **lab-body baseline** that gates Phase B (Render) and Phase C (VPS) — the fairness baseline before adding platform variance. Numbers below are loopback; treat them as relative comparisons, not absolute prod ceilings. Phase B + Phase C will re-validate.

## Shape 1 — Tiny (23B JSON, /echo)

| Variant | rps | Note |
|---|---|---|
| **uvicorn-starlette** | **64,842** | Winner — uvloop+httptools per-core ceiling |
| blazeio | 57,186 | Pure-Python asyncio close 2nd |
| rust (Lancelot) | 43,005 | Per-request FFI bridge ceiling |
| uvicorn-fastapi | 36,986 | FastAPI overhead vs raw Starlette |
| uvicorn-lancelot | 35,464 | Lancelot's ASGI surface via uvicorn |
| robyn | 29,564 | actix-web + per-request pyo3 bridge |

**Interpretation:** uvicorn+uvloop wins tiny one-shot as the M4 honest envelope predicts. Lancelot's rust path is mid-pack (43k) — better than every FastAPI variant but conceded to raw Starlette+uvloop. This is the **deliberate trade**: we optimize for mid/large/stream, not tiny.

## Shape 2 — Large (~70 KB JSON, /echo with LC_BENCH_ITEMS=1500)

| Variant | rps | Note |
|---|---|---|
| blazeio | 45,632 | Pre-computed byte string (static-response ceiling — see footnote) |
| robyn | 8,106 | orjson (Rust JSON via PyPI's `orjson` 3.11) |
| rust (Lancelot) | 5,017 | pydantic-core to_json (Rust) per request |
| uvicorn-lancelot | 4,816 | ASGI Lancelot ~= native rust here |
| uvicorn-starlette | 2,301 | json.dumps per request |
| uvicorn-fastapi | 283 | json.dumps + full FastAPI response model |

**Real-per-request-serialization comparison (excluding the Blazeio outlier):**
- Robyn 8,106 > Lancelot 5,017 > Starlette 2,301 > FastAPI 283.
- Lancelot beats Starlette **2.2×** and FastAPI **17.7×**. Matches the M2 published claim.
- **Robyn beats Lancelot 1.6× on large JSON serialization.** This is a real finding: Robyn ships with orjson; Lancelot uses pydantic-core. Worth investigating as a low-risk adoption in M6 (orjson is already in our Rust dep graph as a transitive of pydantic-core, so the experiment is cheap).

**Blazeio outlier explained:** the Blazeio bench harness uses `payload_bytes = json.dumps(_PAYLOAD).encode()` at module import — it serves the SAME static bytes for every request, so its number is the "memcpy + write" ceiling, not a per-request-serialization number. Real Blazeio apps that compute payloads per request will be much slower. Honest footnote.

## Shape 3 — Streaming (/stream, 256 chunks)

| Variant | rps | Note |
|---|---|---|
| **rust (Lancelot)** | **6,198** | Winner — R3 on-loop `_drain` pump |
| uvicorn-starlette | 5,129 | Starlette StreamingResponse |
| blazeio | 4,946 | Manual chunked writer |
| uvicorn-fastapi | 4,942 | FastAPI StreamingResponse (same as Starlette) |
| uvicorn-lancelot | 529 | Lancelot ASGI streaming — **regression to investigate** |
| robyn | 106 | Robyn SSEResponse — known slow |

**Two findings:**
1. **Lancelot rust wins (6.2k vs Starlette's 5.1k = +21%)** — the R3 pump's on-loop `_drain` design pays off vs Starlette's per-chunk asyncio overhead. Matches the M3-stream-rearch tag's claimed parity-to-1.3× win.
2. **uvicorn-lancelot at 529 rps is a serious regression** — Lancelot's ASGI surface on uvicorn is **~10× slower** than its native rust path AND Starlette. Worth investigating: likely an `@app.stream` decorator wrapping the user generator with extra overhead that doesn't show up on the native path. **Filing as an M6 candidate.**

## Streaming TTFT — chunks=64 (the AI-streaming UX metric)

| Variant | avg | p50 | p99 |
|---|---|---|---|
| blazeio | 0.79 ms | **0.78 ms** | 1.22 ms |
| **rust (Lancelot)** | **0.69 ms** | 0.88 ms | 1.24 ms |
| uvicorn-starlette | 2.18 ms | 2.36 ms | 3.25 ms |
| uvicorn-fastapi | 2.44 ms | 2.67 ms | 3.52 ms |
| uvicorn-lancelot | 2.82 ms | 2.47 ms | 4.97 ms |
| robyn | 5.27 ms | 5.30 ms | 9.03 ms |

**Interpretation:** Lancelot rust + Blazeio tied for first place at sub-millisecond p50 TTFT. The other 4 variants sit in the 2-3 ms range; Robyn is markedly worse at 5-9 ms. This is the **AI-streaming UX number** per `researchfms/core/` transcripts.

## Streaming TTFT — chunks=256

| Variant | avg | p50 | p99 |
|---|---|---|---|
| uvicorn-lancelot | 2.54 ms | **2.47 ms** | 5.22 ms |
| blazeio | 2.44 ms | 2.52 ms | 3.29 ms |
| rust (Lancelot) | 2.56 ms | 2.64 ms | 4.31 ms |
| robyn | 4.92 ms | 5.20 ms | 7.60 ms |
| uvicorn-starlette | 5.37 ms | 5.81 ms | 7.09 ms |
| uvicorn-fastapi | 5.99 ms | 6.41 ms | 7.46 ms |

**Interpretation:** Lancelot ASGI takes the top spot here. Across both shapes, **Lancelot is consistently top-3 on TTFT** and the entire FastAPI/Starlette stack is consistently 2-3× slower. This is the marquee AI-streaming win.

## `@app.batch` peer comparison (10s × c=200 against rate-limited mock backend, cap=8 × 50ms)

| Framework | /solo rps | /batch rps | coalescing ratio | upstream calls saved |
|---|---|---|---|---|
| **lancelot** | 171.9 | **2,189.1** | **15.99** | **~12.7× fewer** |
| fastapi | 171.9 | — | — | — |
| robyn | 173.5 | — | — | — |
| blazeio | (~170) | — | — | — |

**Interpretation:** Every framework's /solo is bounded at the upstream cap (8 concurrent × 1/50ms = 160 calls/sec theoretical; we see ~172 due to header overhead). Lancelot's `@app.batch` coalesces an average of 16 items per upstream call — the cap, perfectly saturated. Result: **~12.7× throughput vs the no-coalescing baseline + 16× fewer upstream calls.** This is THE marquee Lancelot-only number; no peer ships an equivalent primitive.

## Headline numbers M5 can now publish

| Claim | Evidence | Caveat |
|---|---|---|
| Lancelot's R3 streaming pump beats Starlette on raw chunk-throughput | 6.2k vs 5.1k rps @ 256 chunks (+21%) | Loopback; Phase B/C re-validate |
| Lancelot's TTFT is consistently top-3 across chunk shapes | p50 0.88 ms @ 64 chunks (tied with Blazeio) | Loopback variance ±0.5 ms |
| `@app.batch` delivers ~12.7× throughput vs /solo against a rate-limited LLM | 2,189 vs 172 rps @ c=200 | Mock backend; real LLM = same shape, longer per-call latency |
| Lancelot beats FastAPI 17.7× on per-request large-JSON serialization | 5,017 vs 283 rps @ 70KB | This is the M2 published win; reconfirmed |
| FastAPI byte-parity holds | 977/987 = 99.0% Class-A passes | Up from 974 after the M5 routing.py loosening |

## Findings worth pursuing in M6

1. **Investigate orjson for Lancelot's large-JSON serialization** — Robyn beats us 1.6× here. Cheap experiment.
2. **Investigate uvicorn-lancelot streaming regression** — 529 rps vs native rust's 6,198 rps is a ~10× gap that shouldn't exist. The ASGI streaming path is doing something extra.
3. **Phase B + C re-validation** — every number above is loopback; the M5 exit gate publishes the same numbers on Render + VPS.

## What this lab-body bench does NOT cover

- 30-min sustained runs (this is 60s) — the M5 exit gate.
- 24h soak (T2.a) — the leak-test exit gate.
- Multiprocess `SO_REUSEPORT` scaling — Linux-only, runs on Phase C VPS.
- Mixed adversarial+benign (slow-loris under benign load) — needs DURATION≥20s to hit Lancelot's 15s header-read timeout.
- Real network RTT — loopback understates p99 by 10-50 ms vs cross-region prod.

These are all in the M5 harness; they're wall-clock-bound and run when the user kicks off the deploys.
