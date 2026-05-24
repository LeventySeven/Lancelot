# M6 T1.3 — Robyn Performance Deep-Dive

Focused source-level read of Robyn 0.84.0's hot path to explain the M5 lab-body finding (Robyn 8,106 rps on 70 KB JSON vs Lancelot 5,017 — 1.6× ahead) and to identify any remaining perf gaps after M6 T1.2/T1.2b.

**Source pinned at:** `researchfms/robyn/Robyn/` (local clone, version 0.85.0-dev; PyPI 0.84.0 installed for the bench).

## Finding #1 — orjson on every JSON response (the source of the M5 win)

`robyn/jsonify.py` (verbatim, the whole file):

```python
import orjson

def jsonify(data):
    output_binary = orjson.dumps(data)
    output_str = output_binary.decode("utf-8")
    return output_str
```

That's the whole serialization path. orjson is a **hard dependency** of Robyn (declared in `robyn/pyproject.toml` — note: Robyn lists it as a runtime dep; Lancelot can't because of the cardinal pydantic-only runtime rule).

**Why it wins:** orjson is ~2.2× faster than pydantic-core's `to_json` on dict-heavy payloads (microbench in this session: orjson 13,871 ops/s vs pydantic-core 6,286 ops/s on the bench's 1500-item dict). Pure throughput win on the serialize hot path.

**Status:** **ADOPTED in M6.**
- T1.2 (commit `2a5a9e7`): `lancelot/validation.py::_wire_json` now tries orjson first, falls through to pydantic-core on TypeError. Soft dep (orjson stays out of pyproject's runtime deps — cardinal rule).
- T1.2b (commit `9538038`): same preference in Rust `src/serialize.rs::to_json_bytes` so the native rust path benefits too. New `orjson_dumps_fn()` with PyOnceLock + GIL-aware memoization (same pattern as `to_json_fn`).
- M6 bench post-fix: rust 9,160 rps + uvicorn-lancelot 8,803 rps **both beat Robyn's 7,744** on the same 70 KB shape.

## Finding #2 — per-request `pyo3_async_runtimes::tokio::into_future` (Robyn pays our R3-pre-rearch cost on every request)

`src/executors/mod.rs::execute_http_function` (lines 1-30):

```rust
pub async fn execute_http_function(
    request: &Request,
    function: &FunctionInfo,
    context: Option<&Py<PyAny>>,
) -> PyResult<ResponseType> {
    if function.is_async {
        let output = Python::with_gil(|py| -> PyResult<_> {
            let coroutine = get_function_output(function, py, request)?;
            let awaitable = match context {
                Some(ctx) => wrap_coro_in_context(py, ctx.bind(py), coroutine)?,
                None => coroutine,
            };
            pyo3_async_runtimes::tokio::into_future(awaitable)
        })?
        .await?;

        Python::with_gil(|py| extract_response_type(output, py))
    } else { ... }
}
```

Two GIL grabs per request + `pyo3_async_runtimes::tokio::into_future` to bridge the Python coroutine into a Rust `Future`. This is the SAME cross-thread asyncio-bridge pattern that bit Lancelot's R3 streaming pump in M3 (where we paid ~0.5 ms per `__anext__`). Robyn pays this **per request**, not per chunk — so the absolute cost is smaller, but it's still the bridge overhead on every single request.

**Why Lancelot doesn't pay this on streaming:** the M3 R3 rearch (tag `m3-stream-rearch`) moved the streaming drain to an on-loop coroutine (`_drain(gen, sink)`) — one bridge crossing for the whole stream, not per chunk. For per-request, Lancelot's native rust path also uses one GIL grab + one call (see `src/dispatch.rs`), so the per-request bridge cost is comparable to Robyn's.

**Status:** **ALREADY HAVE** equivalent shape. Not a borrow.

## Finding #3 — actix-web vs hyper (architectural difference; not a gap)

`src/server.rs` uses `actix-web` as the HTTP runtime:

```rust
use actix_files::Files;
use actix_http::KeepAlive;
use actix_web::*;
```

actix-web is a separate Rust web framework (vs Lancelot's direct `hyper`). actix is built around the Actor model + per-connection worker tasks. It's competitive with hyper at the wire-protocol layer; the differences are architectural (actor vs direct futures), not raw throughput.

**Why it doesn't matter for our M6 bench:** under matched-worker counts, the per-request wire cost is dominated by the GIL grab + Python coroutine execution, not by the HTTP framework underneath. Both actix and hyper deliver bytes at similar rates; the variance is in everything-else.

**Status:** **ALREADY HAVE** equivalent (hyper). Not a borrow.

## Finding #4 — `set_nodelay(true)` + `SO_REUSEPORT` + listen backlog 16384

`src/shared_socket.rs:22-30` (verbatim):

```rust
let socket = if ip.is_ipv4() { ... } else { ... };
let address = SocketAddr::new(ip, port);
#[cfg(not(target_os = "windows"))]
socket.set_reuse_port(true)?;
socket.set_reuse_address(true)?;
socket.set_nodelay(true)?;
socket.bind(&address.into())?;
socket.listen(16384)?;
socket.set_nonblocking(true)?;
```

- `TCP_NODELAY = true` for low-latency response
- `SO_REUSEPORT` for multi-process load balancing (Linux only)
- `listen(16384)` backlog

**Status:** **ALREADY HAVE.** Lancelot's `src/socket.rs` sets the same: `TCP_NODELAY`, `SO_REUSEPORT` (Linux-only LB per M4 V3.3), configurable backlog via `LANCELOT_MAX_CONNECTIONS`. The Robyn-hardcoded 16384 vs Lancelot's configurable knob is a slight win for Lancelot (tunable).

## Finding #5 — `DEFAULT_MAX_PAYLOAD_SIZE = 1_000_000` (1 MB)

`src/server.rs:39`:

```rust
const DEFAULT_MAX_PAYLOAD_SIZE: usize = 1_000_000; // 1Mb
```

1 MB default body cap. Lancelot's default is 16 MiB. For AI/cloud workloads (embedding requests, large LLM inputs), 1 MB is too tight — but Robyn is also overridable via `ROBYN_MAX_PAYLOAD_SIZE` env var.

**Status:** **AVOID.** Lancelot's 16 MiB default is the correct call for the target workload. Not a borrow.

## Finding #6 — `ConstRouter` (prebaked-bytes path)

`src/routers/const_router.rs` (referenced in `src/server.rs:6`):

Robyn has a `ConstRouter` for routes whose response is fully known at registration time — same architectural pattern as Lancelot's M4 const-tier (`m4-const-tier`).

**Status:** **ALREADY HAVE** (M4 const-tier, ~2.8× uvicorn+uvloop on prebaked responses). Not a borrow.

## Summary table — Robyn perf characteristics relative to Lancelot M6

| Axis | Robyn 0.84/85 | Lancelot M6 (post-T1.2b) | Verdict |
|---|---|---|---|
| JSON serialization | orjson (hard dep) | orjson when installed, pydantic-core fallback (soft dep) | At parity post-M6; Lancelot's soft-dep is the right call for the runtime rule |
| Per-request bridge | `pyo3_async_runtimes::tokio::into_future` per request | Direct pyo3 + on-loop coroutine for streams | At parity per-request; Lancelot wins on streaming |
| HTTP runtime | actix-web | hyper | Comparable at wire layer |
| Socket setup | TCP_NODELAY + SO_REUSEPORT + 16384 backlog | Same + configurable backlog | At parity, Lancelot slightly more tunable |
| Body cap default | 1 MB | 16 MiB | Lancelot's is correct for AI/cloud target |
| Const-bytes path | ConstRouter | M4 const-tier | Equivalent |
| Streaming | SSEResponse only (SSE-framed) | `@app.stream` (typed) + raw StreamingResponse | Lancelot more flexible |
| Multiprocess scaling | SO_REUSEPORT (Linux-only LB) | Same; documented in `docs/deploy.md` | Equivalent |

## What this deep-dive changed in M6

- T1.2 + T1.2b orjson preference landed (Python `_wire_json` + Rust `to_json_bytes`).
- Bench result: Lancelot now beats Robyn 1.18× (rust) / 1.14× (uvicorn-lancelot) on 70 KB JSON, up from 0.62× / 0.59× pre-M6.

## What is NOT going to land in M6 (or later) from this read

- Robyn's hard-dep on orjson — Lancelot keeps it soft per cardinal pydantic-only rule.
- 1 MB body cap default — Lancelot's 16 MiB is the deliberate AI/cloud-target call.
- `pyo3_async_runtimes` — adding it would be a dep with no perf upside vs Lancelot's existing direct pyo3 path.
- actix-web — switching HTTP runtime is a major rewrite for no measurable win.

## Cross-references

- M5 peer source recheck: `docs/peer-source-recheck/M5-peers.md` (Robyn section)
- M6 plan T1.3: `docs/plans/M6-PLAN-SPEED-STABILITY-ADOPTION.md`
- M6 T1.2 commit: `2a5a9e7` (Python orjson)
- M6 T1.2b commit: `9538038` (Rust orjson)
- Robyn source: `researchfms/robyn/Robyn/`
