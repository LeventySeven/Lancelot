# M6 T1.4 — Blazeio Performance Deep-Dive

Focused source-level read of Blazeio 4.1.0's hot path to explain the M5 lab-body findings (Blazeio 57,186 rps on tiny JSON, 0.78 ms p50 TTFT @ 64 chunks — tied with native rust) and to identify what's adoptable vs documented-divergent.

**Source pinned at:** `researchfms/blazeio/Blazeio/Blazeio/` (local clone; installed via editable install because PyPI's latest 2.2.2.5 is on the older API).

## Finding #1 — `BufferedProtocol.get_buffer` / `buffer_updated`: receive path uses asyncio's low-level BufferedProtocol API

`Protocols/__init__.py` (verbatim, the whole `BlazeioProtocol` base):

```python
class BlazeioProtocol:
    __slots__ = ()
    def buffer_updated(app, nbytes):
        app.transport.pause_reading()
        app.__stream__.append(bytes(app.__buff__memory__[:nbytes]))
        app.__evt__.set()

    def get_buffer(app, sizehint):
        if sizehint > len(app.__buff__memory__):
            app.__buff__ = bytearray(sizehint)
            app.__buff__memory__ = memoryview(app.__buff__)
        elif sizehint <= 0:
            sizehint = len(app.__buff__memory__)
        return app.__buff__memory__[:sizehint]
```

Blazeio uses `asyncio.BufferedProtocol` (not `asyncio.Protocol`). The transport calls `get_buffer(sizehint)` to ASK for a writable buffer, then writes data into it directly, then calls `buffer_updated(nbytes)` to tell the protocol how many bytes were written. This skips the per-receive `bytes` allocation that `data_received(data)` on the standard `Protocol` API does.

**Important nuance — it's NOT fully zero-copy.** `buffer_updated` does:
```python
app.__stream__.append(bytes(app.__buff__memory__[:nbytes]))
```
That `bytes(...)` is a **copy** into a new immutable `bytes` object before appending to the stream deque. The memoryview enables zero-copy READ from the transport into the buffer; the data is then copied to the stream-deque entries.

Net win: 1 copy avoided per receive (the `bytes` constructor in `data_received`); 1 copy still happens (into the deque). Compared to a naive `asyncio.Protocol` which forces 1-2 copies per receive, this is a modest improvement.

**Why Lancelot can't easily adopt:** Lancelot's HTTP layer is `hyper` (Rust), not asyncio.Protocol. Hyper has its own zero-copy `Bytes` reference-counted buffer (literally the `Bytes` crate Lancelot uses for response bodies). Hyper's receive path is at least as efficient as Blazeio's BufferedProtocol pattern — probably more so, since `Bytes` is genuinely reference-counted while Blazeio's pattern still does a final copy into the deque.

**Status:** **AVOID** (architectural mismatch). The pattern is the right idea for an asyncio-Protocol-based server; Lancelot's hyper-based server already has a more zero-copy receive path via `Bytes`.

## Finding #2 — `transport.pause_reading()` after every buffer fill (backpressure trigger)

In `buffer_updated`:
```python
app.transport.pause_reading()
```

After each buffer fill, Blazeio immediately pauses the transport. This forces backpressure into the kernel TCP receive window: don't fill more bytes until the handler has consumed them. The handler must explicitly call `transport.resume_reading()` (in `ensure_reading` per `Protocols/server_protocol.py`) when ready for more.

**Why it's clever:** prevents unbounded memory growth if the handler is slow. Same idea as Lancelot's bounded mpsc channel + `await_capacity` (R3 pump) — only fire receive when the downstream is ready.

**Status:** **ALREADY HAVE** equivalent. Lancelot's `Bytes`-via-hyper path has its own backpressure (hyper's connection state machine pauses reads when the body channel is full). Not a borrow.

## Finding #3 — Single asyncio loop per process + uvloop opt-in

Blazeio's design: one asyncio event loop per process. Uvloop is optional (env-knob to enable). No worker pool; native asyncio task-per-connection.

The shape:
```python
web = io.App("0.0.0.0", 8000, with_keepalive=1)
with web: web.runner()   # blocks; runs the event loop
```

**Why Blazeio is fast at tiny JSON (57k rps):** the entire path lives on one asyncio loop with uvloop underneath; no GIL contention, no cross-thread bridging, no thread pool dispatch. For the tiny-payload case, this is pure asyncio + uvloop's reactor speed.

**Why Lancelot loses here (43k rps on tiny):** Lancelot's pyo3 + tokio path pays an FFI bridge cost per request. For tiny payloads, the bridge is a meaningful fraction of total request time. Documented in CLAUDE.md's "honest perf envelope" — this is the M4 const-tier's reason-for-being (the const tier bypasses the bridge entirely for fixed responses, hitting 183k rps).

**Status:** **ALREADY HAVE** the architectural answer (const-tier). Switching Lancelot's primary path to a Python+uvloop-only design would lose every other Lancelot win (R3 pump, large-payload serialization, batch coalescing). Not a borrow.

## Finding #4 — `SharpEvent` (single-waiter event primitive)

`Dependencies/__init__.py` defines `SharpEvent`: a pooled `asyncio.Future`-like primitive optimized for the single-waiter case (one coroutine waiting on this event). Standard `asyncio.Event` allocates a fresh `Future` per `set()`; `SharpEvent` reuses one.

**Microbench potential:** for the R3 pump's `_drain` → channel signaling, a single-waiter event is plausible. But our `tokio::sync::mpsc` already uses a notification primitive that's efficient at the Rust level — adding `SharpEvent` would be at the Python coroutine boundary, where the gain is marginal.

**Status:** **DEFER to M7** (BORROW low-risk, low-marginal-benefit). The M5 T3 recheck flagged this; M6 lab body doesn't surface a measurable gap that `SharpEvent` would close. Revisit if Phase B/C bench shows a real perf cliff at the coroutine signaling layer.

## Finding #5 — `__ReMonitor__` threaded watchdog (cross-thread deadlines)

Blazeio uses a separate watchdog thread (`__ReMonitor__`) for connection deadlines (header-read timeout, idle timeout). Cross-thread state with the asyncio loop.

**Why it's fragile:** cross-thread synchronization with the GIL is error-prone. Lancelot's `LANCELOT_HEADER_TIMEOUT_MS` is enforced at the hyper/tokio layer (Rust) — simpler + more correct.

**Status:** **AVOID.** Lancelot's approach (Rust-layer deadlines) is structurally cleaner.

## Finding #6 — Manual chunked-encoding writer (`r.writer(b"%X\r\n%s\r\n")`)

Blazeio's streaming idiom (from the bench fixture I wrote in M5 T1.1):
```python
await r.prepare(headers={...}, status=200, encode_resp=False)
for i in range(chunks):
    data = _chunk(i)
    await r.writer(b"%X\r\n%s\r\n" % (len(data), data))
await r.writer(b"0\r\n\r\n")
```

The user manually frames chunked transfer encoding. This is more error-prone (the M5 T1.1 commit notes that I had to add a no-op `before_middleware` to force the with-middleware code path because Blazeio's `serve_route_no_middleware` does `declared_routes.get(r)` instead of `declared_routes.get(r.path)` — a bug-bordering routing path).

**Why Blazeio's TTFT is fast (0.78 ms p50 @ 64 chunks):** all the framing happens on one asyncio loop with no per-chunk async overhead. Compare to Lancelot's ASGI streaming pre-M6 T1.1 which had per-chunk `wait_for(shield(ensure_future(...)))` machinery; post-T1.1 the overhead is gone but the path still goes through ASGI's dict-message protocol.

**Status:** **ALREADY HAVE** equivalent simplicity on the native rust path (R3 `_drain` — single loop, no per-chunk overhead). The post-T1.1 ASGI path is also competitive (4,439 rps vs Blazeio 4,946 — within 11%).

## Summary table — Blazeio perf characteristics relative to Lancelot M6

| Axis | Blazeio 4.1.0 | Lancelot M6 | Verdict |
|---|---|---|---|
| Receive path | `BufferedProtocol.get_buffer` (memoryview-based, 1 copy avoided) | `hyper` + `Bytes` (ref-counted, fully zero-copy) | Lancelot equivalent-or-better |
| Backpressure | `transport.pause_reading()` per buffer fill | hyper's connection-state-machine backpressure | Equivalent |
| Event loop | One asyncio + uvloop per process | tokio (Rust) + uvloop on ASGI variant | Different stacks; Blazeio wins tiny one-shot, Lancelot wins everything else |
| Single-waiter event | `SharpEvent` (pooled Future) | `tokio::sync::mpsc::Notify` | Deferred to M7 (low marginal benefit) |
| Deadlines | `__ReMonitor__` threaded watchdog | hyper/tokio-layer deadlines | Lancelot cleaner |
| Streaming | Manual chunked writer (user-side framing) | `@app.stream` (framework-owned framing) | Lancelot more ergonomic |
| Routing | `declared_routes.get(r)` bug on no-middleware path | matchit (Rust) | Lancelot correct + faster |
| TTFT @ 64 chunks (M5 lab) | 0.78 ms p50 | 0.88 ms p50 (rust) | Tied within noise |
| Streaming rps @ 256 chunks (M5 lab) | 4,946 | 6,198 (rust native), 4,439 (ASGI post-T1.1) | Lancelot rust wins; ASGI within 11% |

## What this deep-dive did NOT add to M6

- No code changes from Blazeio's source. Every adoptable pattern (BufferedProtocol, SharpEvent) has a structural mismatch with Lancelot's hyper-based architecture OR has a marginal benefit that doesn't justify M6 scope expansion.
- The two M5 T3 BORROW candidates (zero-copy memoryview, SharpEvent) remain **deferred to M7** with documented rationale.

## What's confirmed by reading Blazeio's source

- The 57k tiny rps win is asyncio + uvloop's per-iteration speed — **structurally Lancelot can't match it on the per-request bridge path** without giving up the wins that DO matter (streaming, large-payload, batch). The const-tier (M4) is the architectural answer for the tiny-fixed-response case (183k rps, ~3× Blazeio's tiny number).
- The TTFT tie (~0.8 ms p50) is real and architecturally explainable — both Blazeio and Lancelot rust have minimal per-chunk overhead at the framework layer.
- Streaming rps difference (~25% in Lancelot rust's favor at 256 chunks) is the R3 pump win.

## Cross-references

- M5 peer source recheck: `docs/peer-source-recheck/M5-peers.md` (Blazeio section)
- M6 plan T1.4: `docs/plans/M6-PLAN-SPEED-STABILITY-ADOPTION.md`
- M5 lab body analysis: `benchmarks/results/M5/ANALYSIS.md`
- M6 T1.1 streaming fix commit: `1c8a4d8`
- Blazeio source: `researchfms/blazeio/Blazeio/Blazeio/`
