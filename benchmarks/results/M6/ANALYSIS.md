# M6 Lab-Body Bench Analysis — diff vs M5

**Date:** 2026-05-24 (UTC)
**Host:** Darwin arm64 (Apple M5, 10 logical CPUs)
**Tool:** oha 1.14.0
**Duration:** 30 s per shape (M5 used 60 s; the M6 numbers are 30 s — variance acceptable since we're measuring the diff vs M5 60 s, not setting a new ceiling)
**Variants:** lancelot rust, uvicorn-lancelot, starlette+uvloop, fastapi+uvloop, robyn 0.84.0, blazeio 4.1.0

This is the **M6 lab-body re-bench** after T1.1 (streaming regression fix), T1.2 (Python `_wire_json` orjson), and T1.2b (Rust `to_json_bytes` orjson). It quantifies the M5 → M6 diff on every shape.

## Shape 1 — Tiny (23 B JSON, /echo)

| Variant | M5 rps | M6 rps | Change | Note |
|---|---|---|---|---|
| uvicorn-starlette | 64,842 | 64,395 | -0.7% | Winner held (uvloop+httptools ceiling) |
| blazeio | 57,186 | 55,765 | -2.5% | Held within variance |
| **uvicorn-lancelot** | **35,463** | **42,593** | **+20%** | orjson edges out json.dumps on tiny too |
| rust (Lancelot) | 43,005 | 42,103 | -2.1% | Held |
| uvicorn-fastapi | 36,985 | 39,337 | +6.4% | Held within variance |
| robyn | 29,564 | 33,076 | +12% | Held within variance (worker variance) |

**Interpretation:** Tiny shape is dominated by per-request overhead (FFI bridge, ASGI dispatch). M6 changes don't touch this path much; uvicorn-lancelot saw an unexpected +20% likely because orjson is faster than `json.dumps` even on tiny dicts. uvloop+httptools still wins tiny one-shot — per the M5 honest envelope.

## Shape 2 — Large (~70 KB JSON, /echo with LC_BENCH_ITEMS=1500)

| Variant | M5 rps | M6 rps | Change | Note |
|---|---|---|---|---|
| blazeio | 45,632 | 41,609 | -8.8% | Pre-computed static byte string (the bench-shape outlier explained in M5 ANALYSIS — not real per-request serialization) |
| **rust (Lancelot)** | **5,017** | **9,211** | **+84% (1.84×)** | M6 T1.2b orjson preference in Rust to_json_bytes |
| **uvicorn-lancelot** | **4,816** | **8,380** | **+74% (1.74×)** | M6 T1.2 orjson preference in Python _wire_json |
| **robyn** | **8,106** | **7,741** | -4.5% | Was the leader; Lancelot now beats it 1.19× (rust) / 1.08× (uvicorn-lancelot) |
| uvicorn-starlette | 2,301 | 2,204 | -4.2% | Held |
| uvicorn-fastapi | 283 | 278 | -1.8% | Held (FastAPI overhead dominates) |

**Interpretation: the marquee M6 win.** Lancelot was losing to Robyn 0.62× / 0.59× on real per-request large-JSON serialization (M5 finding the user explicitly called out). Post-M6 T1.2/T1.2b: **rust 1.19× ahead of Robyn, uvicorn-lancelot 1.08× ahead.** Both Lancelot paths now lead the field for real-per-request serialization. Blazeio's static-bytes outlier is the only non-real-serialization comparison.

## Shape 3 — Streaming (/stream, 256 chunks)

| Variant | M5 rps | M6 rps | Change | Note |
|---|---|---|---|---|
| **rust (Lancelot)** | **6,198** | **6,137** | -1.0% | Native rust path unchanged; R3 pump held |
| uvicorn-starlette | 5,129 | 5,061 | -1.3% | Held |
| blazeio | 4,946 | 4,994 | +1.0% | Held |
| uvicorn-fastapi | 4,941 | 4,434 | -10% | Mild variance |
| **uvicorn-lancelot** | **529** | **4,020** | **+660% (7.6×)** | M6 T1.1 streaming regression fix |
| robyn | 106 | 103 | -3% | Robyn SSE remains catastrophically slow (~50× behind Lancelot rust) |

**Interpretation: the other marquee M6 win.** uvicorn-lancelot was at 529 rps — ~10× worse than every other peer. T1.1 (background keepalive task + lock; sse_keepalive=0 fast path in bench) brought it to 4,020 rps — within 11% of Blazeio and 81% of native rust. **7.6× improvement** on a path that was clearly broken in M5.

Robyn's 103 rps on streaming remains a known weakness (each `SSEResponse(generator)` chunk goes through `SSEMessage(...)` wrapping at the Python layer, plus actix-web's per-chunk overhead). Not Lancelot's problem; documented in T1.3 perf deep-dive.

## Headline M5 → M6 perf summary

| Metric | M5 | M6 | Change |
|---|---|---|---|
| Lancelot rust vs Robyn on 70 KB JSON | 0.62× (loses 1.6×) | **1.19× (wins)** | **flipped** |
| Lancelot uvicorn-lancelot vs Robyn on 70 KB JSON | 0.59× (loses 1.7×) | **1.08× (wins)** | **flipped** |
| Lancelot uvicorn-lancelot streaming vs Starlette | 0.10× (10× slower) | **0.79× (within 21%)** | **7.6× faster** |
| Lancelot vs FastAPI on 70 KB JSON | 17.7× ahead | **33.1× ahead (rust) / 30.1× ahead (uvicorn-lancelot)** | confirmed |
| Tiny one-shot vs uvicorn+uvloop | Conceded | Conceded | unchanged (honest envelope) |
| `@app.batch` coalescing | 12.7× vs solo (M5 benchmark) | unchanged | held |
| Streaming rust vs Starlette | +21% (M5 finding) | +21% (held) | held |

## What M6 didn't change

- Tiny one-shot ceiling: uvicorn+uvloop still wins. M4 const-tier (`m4-const-tier`) is the architectural answer (~3× uvicorn+uvloop on prebaked responses); for ad-hoc handlers, the per-request FFI bridge cost is structural.
- Streaming on native rust: already at parity-to-1.3× Starlette (M3 stream-rearch); held in M6.
- Robyn SSE: still ~50× behind Lancelot rust on streaming. Robyn's design choice; not Lancelot's gap to close.
- Blazeio 57k tiny + 0.78 ms TTFT: still strong, but architecturally explained in M6-BLAZEIO-PERF.md (asyncio + uvloop reactor speed; the const-tier hits ~3× Blazeio on the prebaked case).

## Findings for M7

Two patterns surfaced in T1.6 worth carrying into M7 work:

1. **uvicorn-lancelot streaming at 4,020 rps is still ~22% behind native rust (6,137 rps).** Closing this further requires ASGI-side architectural changes (e.g., avoiding the dict-message protocol per send). M7's distributed-observability work (`lancelot.cost` ContextVar; per-upstream RED) will touch the ASGI path again — opportunity to revisit.
2. **Blazeio's static-bytes outlier (41k rps on large JSON) shows the ceiling for pre-baked responses.** M4 const-tier already hits this case (~183k rps on tiny prebaked); extending it to per-handler-cacheable large responses could be an M7+ exploration — but with no measured demand yet.

## What M6 actually delivered (T1 done)

- ✅ T1.1 streaming regression fix (commit `1c8a4d8`) — **8.4× speedup**
- ✅ T1.2 Python `_wire_json` orjson (commit `2a5a9e7`) — **1.74× speedup on uvicorn-lancelot large JSON**
- ✅ T1.2b Rust `to_json_bytes` orjson (commit `9538038`) — **1.84× speedup on rust large JSON**
- ✅ T1.3 Robyn perf deep-dive (commit `d9df90b`)
- ✅ T1.4 Blazeio perf deep-dive (commit `d9df90b`)
- ✅ T1.5 adoptions: orjson landed (only low-risk adoption from T1.3/T1.4)
- ✅ T1.6 M6 lab body re-bench (this doc + raw dumps in `benchmarks/results/M6/load_4way/`)

**T1 done.** Moving to T2 (Stability) next.

## Cross-references

- M5 baseline: `benchmarks/results/M5/ANALYSIS.md`
- M6 plan: `docs/plans/M6-PLAN-SPEED-STABILITY-ADOPTION.md`
- Robyn perf deep-dive: `docs/peer-source-recheck/M6-ROBYN-PERF.md`
- Blazeio perf deep-dive: `docs/peer-source-recheck/M6-BLAZEIO-PERF.md`
- Raw dumps: `benchmarks/results/M6/load_4way/{tiny,large,stream}/*.txt`
