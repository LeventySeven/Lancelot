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
# deps: oha (cargo install oha) + uvicorn[standard] (uvloop+httptools) in .venv.
#       NONE are Lancelot runtime deps — all dev/bench-only.
./benchmarks/run_load.sh                       # both shapes, 10s, c=50, 2 runs
DURATION=8s CONNECTIONS=500 ./benchmarks/run_load.sh
```

`run_load.sh` measures two fair shapes; raw `oha` dumps land in
`benchmarks/results/{percore,fullmachine}/<variant>.<run>.txt`.

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

### Large ~69 KB JSON response (c=50, 8s, single process/thread)

| Variant | req/s | p50 |
|---|---:|---:|
| `rust` (pydantic-core `to_json`) | **5,258** | 9.7 ms |
| `uvicorn-starlette` (`json.dumps`) | 2,339 | 21.0 ms |

**For large responses the Rust path wins ~2.25×** — once serialization cost
dominates the per-request overhead, Rust-native JSON beats Python `json.dumps`.
This is the crossover, and it's exactly Lancelot's positioning
(transport/serialization), *not* a generic "faster server" claim.

## What this benchmark actually shows

- **There is a crossover by response size.** Small bodies → uvicorn+uvloop wins
  (FFI/bridge overhead dominates). Large bodies → Lancelot's Rust serialization
  wins (Python `json.dumps` dominates). The break-even is workload-specific and
  not characterized precisely here.
- **The M3 "Rust hot-path = more throughput" premise is NOT universally true.**
  For the common small-JSON API case it's a regression vs uvicorn+uvloop. The
  defensible win is **serialization of large payloads** (and, by the same logic,
  streaming — untested here).
- **Deploy the Rust server multiprocess** (`SO_REUSEPORT`, 1 thread/proc), never
  1-process-many-threads.
- The Rust core's other value is **resilience, not req/s**: 16 MiB body cap (413),
  connection cap, `header_read_timeout` (slow-loris), http1-only hardening,
  graceful drain, backpressured streaming pump — all verified by
  `tests/test_m3_http_integration.py`, all orthogonal to throughput.

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
