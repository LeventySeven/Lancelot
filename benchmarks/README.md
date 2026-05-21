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

AI token-stream shape (many small chunks). **This refutes the assumption that
streaming is a Rust win:**

| chunks | rust `_native` req/s | starlette req/s | gap |
|---:|---:|---:|:--|
| 64 | 937 | 14,968 | starlette ~16× |
| 256 | 237 | 5,443 | starlette ~23× |
| 1024 | 62 | 1,537 | starlette ~25× |

Full-machine (64 chunks): rust 10×1 = **925** vs starlette `--workers 10` =
**24,378**. Multiprocess does NOT help — each stream's wall-time is fixed by the
pump's **per-chunk cross-thread asyncio-bridge round-trip (~0.8 ms/chunk)**, so
concurrency can't hide it.

**Important scope:** this is specific to the Rust **`_native` server's R3 pump**.
Lancelot's normal streaming path — `@app.stream` under uvicorn — runs the async
generator natively in the event loop and does **5,572 req/s** (64 chunks), ~6×
the Rust pump. So *Lancelot streaming is fine; the Rust `_native` pump is not*.

## What this benchmark actually shows

- **One-shot serialization crossover ~1–4 KB.** Small bodies → uvicorn+uvloop
  wins (per-request FFI/bridge overhead dominates). Large bodies → Lancelot's
  Rust serialization wins (Python `json.dumps` dominates), up to ~2.3× at 69 KB.
- **The M3 "Rust hot-path = more throughput" premise is NOT universally true.**
  For small JSON it's a regression vs uvicorn+uvloop. The **only** substantiated
  Rust throughput win is **large (>~4 KB) one-shot serialization**.
- **Streaming via the Rust `_native` R3 pump is a severe regression (16–26×)** —
  the per-chunk asyncio bridge is a perf cliff, fatal for high-chunk-count
  streams (e.g. LLM token streaming). **Stream via `lancelot.App`/uvicorn
  instead.** The R3 pump needs re-architecture before it can carry that claim.
- **Deploy the Rust server multiprocess** (`SO_REUSEPORT`, 1 thread/proc), never
  1-process-many-threads.
- The Rust core's other value is **resilience, not req/s**: 16 MiB body cap (413),
  connection cap, `header_read_timeout` (slow-loris), http1-only hardening,
  graceful drain — all verified by `tests/test_m3_http_integration.py`,
  orthogonal to throughput.

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
