# Lancelot load benchmark — Rust core vs uvicorn (honest)

`oha` load comparison of Lancelot's Rust server (`lancelot._native.run` =
hyper + tokio + pyo3) against uvicorn baselines, **same machine, loopback, same
`GET /echo`**. This benchmark exists to *honestly* test whether the M3 Rust
hot-path swaps are a throughput win. **The headline answer is: it depends on
response size, and the naive "Rust is faster" framing is wrong.**

> An earlier version of this benchmark claimed the Rust core was ~1.64× faster
> than uvicorn. That was an artifact of an **unfair config** — a 10-thread Rust
> runtime vs single-threaded uvicorn, with uvicorn on its slow pure-Python
> `h11`+asyncio fallback. A fairness review caught it. This version fixes both:
> uvicorn runs its production stack (uvloop + httptools) and worker counts are
> matched. The corrected result is below and is much less flattering.

## How to reproduce

```bash
# deps: oha (cargo install oha) + uvicorn[standard] (uvloop+httptools):
#       pip install -e '.[bench]'. NONE are Lancelot runtime deps — bench-only.
./benchmarks/run_load.sh                       # the 4-variant comparison, 2 shapes
./benchmarks/characterize.sh                   # payload-crossover + streaming sweeps
DURATION=8s CONNECTIONS=500 ./benchmarks/run_load.sh
```

`run_load.sh` measures two fair shapes (raw dumps in
`benchmarks/results/{percore,fullmachine}/`); `characterize.sh` sweeps payload
size and streaming (raw dumps in `benchmarks/results/{crossover,stream}/`).

## Fairness controls

- **uvicorn = production stack:** `--loop uvloop --http httptools`, `--no-access-log`.
- **Matched parallelism**, measured in two shapes:
  - `percore` — 1 worker each: Rust `LANCELOT_WORKERS=1` vs single-process uvicorn.
  - `fullmachine` — N=10 each: Rust default 10-thread tokio vs uvicorn `--workers 10`.
- **Same payload + serializer-by-default per stack**: Rust returns a dict →
  pydantic-core `to_json`; Starlette returns `JSONResponse` (Python `json.dumps`);
  FastAPI/`lancelot.App` return a dict (their default JSON path). This is a
  *defaults* comparison — see caveat 3.
- The Rust path does **no DI/validation**; FastAPI/`lancelot.App` do full DI.

## Results — Apple M5 (10 cores), Python 3.13.13, oha 1.14.0

### Trivial 23-byte response (c=50, 10s, mean of 2 runs)

| Variant | per-core req/s | per-core p50 | full-machine req/s |
|---|---:|---:|---:|
| `uvicorn-starlette` (uvloop) | **69,894** | 0.72 ms | **124,080** |
| `rust` (Lancelot Rust core) | 44,821 | 1.21 ms | 33,994 |
| `uvicorn-lancelot` (full DI) | 42,350 | 1.17 ms | 89,723 |
| `uvicorn-fastapi` (full DI) | 39,850 | 1.24 ms | 84,139 |

**For tiny responses uvicorn+uvloop is faster** — ~1.56× per-core, and far more
at full machine. Two reasons, both real and architectural:

1. **Per-request FFI + asyncio-bridge cost (~0.5 ms).** Every request crosses
   pyo3 and bridges the coroutine through `call_soon_threadsafe` → the single
   asyncio loop (on another thread) → back via a oneshot. uvloop runs the
   coroutine natively on the same thread that parsed the request — no hop.
2. **The Rust server anti-scales with threads.** 1→10 tokio workers makes it
   *slower* (44.8k → 34k): Python execution is GIL-serialized through one event
   loop per process, so extra threads only add GIL contention, not parallelism.
   uvicorn scales by **process** (10 GILs, 10 loops) → real parallelism.

### Rust server scaling shapes (c=500, 8s) — the GIL ceiling

| Rust config | req/s |
|---|---:|
| 1 process × 10 threads | 33,548 |
| 10 processes × 1 thread (`SO_REUSEPORT`) | 42,454 |
| `uvicorn-starlette --workers 10` (for scale) | 145,668 |

The Rust server's *best* config is multiprocess (1 thread each, `SO_REUSEPORT`) —
the 1-process-many-threads model is strictly worse. Even so, on trivial echo it
tops out ~42k vs uvicorn's ~146k.

### Crossover sweep — /echo, growing payload (per-core, `./characterize.sh`)

| dict items | response bytes | rust req/s | starlette req/s | winner |
|---:|---:|---:|---:|:--|
| 5 | 191 B | 43,715 | 63,175 | starlette |
| 25 | 1,012 B | 41,031 | 46,748 | starlette |
| 100 | 4,186 B | 30,733 | 24,431 | **rust** |
| 350 | 15,426 B | 15,879 | 8,566 | **rust** |
| 1500 | 68,937 B | 5,328 | 2,318 | **rust** |

**The serialization crossover is ~1–4 KB.** Below it, uvicorn+uvloop's lower
per-request overhead wins; above it, Rust-native pydantic-core `to_json` beats
Python `json.dumps` and the margin grows to ~2.3× at 69 KB. This is the
defensible serialization win, and it IS Lancelot's positioning.

### Streaming — /stream, N small chunks (per-core, `./characterize.sh`)

AI token-stream shape (many small chunks). The R3 pump was **re-architected**
after this benchmark first exposed it as a 16–26× regression (the original
design did a cross-thread `call_soon_threadsafe` round-trip per `__anext__`,
~0.8 ms/chunk). The fix drains the generator in ONE coroutine running on the
asyncio loop, pushing chunks same-thread into a bounded channel (depth
`LANCELOT_STREAM_BUFFER`, default 256; backpressure preserved via an
event-driven `await_capacity` only when the channel fills):

| chunks | rust `_native` req/s | starlette req/s | result |
|---:|---:|---:|:--|
| 64 | 15,220 | 15,068 | parity |
| 256 | **6,776** | 5,267 | rust ~1.29× |
| 1024 | **1,788** | 1,498 | rust ~1.19× |

Before the fix these were 937 / 237 / 62 — a **16–30× recovery**. The buffer
depth is the knob: 16 → 2.4k, 64 → 4.9k, 256 → 6.9k (knee), beyond 256 plateaus.
256 recovers full unbounded-channel throughput while keeping memory bounded
(≤ `depth × chunk_size` per in-flight stream).

## What this benchmark actually shows

- **One-shot serialization crossover ~1–4 KB.** Small bodies → uvicorn+uvloop
  wins (per-request FFI/bridge overhead dominates). Large bodies → Lancelot's
  Rust serialization wins (Python `json.dumps` dominates), up to ~2.3× at 69 KB.
- **Streaming is a Rust win after the re-architecture** — parity at 64 chunks,
  ~1.2–1.3× above Starlette at 256–1024. The earlier "streaming is broken" result
  was a real pump defect, now fixed (the per-chunk cross-thread bridge is gone).
- **For small one-shot JSON, uvicorn+uvloop still wins** (per-request FFI/bridge
  overhead). The Rust server is NOT a universal "faster server"; its throughput
  wins are **large-payload serialization + streaming**.
- **Deploy the Rust server multiprocess** (`SO_REUSEPORT`, 1 thread/proc), never
  1-process-many-threads (it anti-scales on threads — GIL).
- The Rust core's other value is **resilience**: 16 MiB body cap (413), connection
  cap, `header_read_timeout` (slow-loris), http1-only, graceful drain — all in
  `tests/test_m3_http_integration.py`, orthogonal to throughput.

## `@app.batch` (C5 marquee) effectiveness — `./run_batch.sh`

Does prefix-aware request grouping actually coalesce concurrent same-key requests
into fewer upstream calls, and does it win when the upstream is the bottleneck?
Model: a simulated upstream LLM with a shared **concurrency cap** (the realistic
constraint — providers rate-limit concurrent calls and bill per call). Both the
batched route and the per-request `/solo` baseline hit the SAME capped upstream.

Upstream cap=8, 50 ms/call, window=50 ms, max_batch=16, c=200:

| route | req/s | upstream calls | coalescing (items/call) |
|---|---:|---:|---:|
| `/solo` (one call per request) | 179 | 1432 | 1.0 |
| `/batch` (`@app.batch`) | **2,165** | 1084 (for 17,320 items) | **15.98** |

**~12× throughput and ~16× fewer upstream calls** against a rate-limited backend
— the coalescing ratio sits right at `max_batch`, i.e. batches fill nearly every
time under load. For a per-call-billed LLM API this is a direct ~16× cost cut on
the batched path. The win is real whenever the upstream (not the framework) is
the bottleneck; with an unbounded/free upstream the two converge.

> This benchmark also EXPOSED a critical bug: the timer-window flush cancelled
> its own task, poisoning any **awaiting** batch handler (the real use case) with
> `CancelledError` → 500. The existing tests used non-awaiting handlers and
> missed it. Fixed (`lancelot/batch.py`) + regression-tested before these numbers.

## Caveats — read before quoting any number

1. **Loopback only**, no real network RTT/TLS/NIC. Compresses relative gaps.
2. **Single machine, single client.** `oha` + servers share 10 cores; absolute
   numbers are machine-specific and understate every variant.
3. **Defaults comparison for serialization.** Starlette's *default* `JSONResponse`
   is `json.dumps`; uvicorn+orjson (also Rust-based) would narrow the large-payload
   gap. The point is "Lancelot is fast by default", not "Python can't serialize fast".
4. **c=50 is latency-bound** (throughput ≈ connections ÷ latency); the c=500 rows
   are the throughput-ceiling view. Both are reported.
5. No "Nx faster than FastAPI" marketing claim is made. The only defensible speed
   claims are the two crossover regimes above, with these caveats attached.
