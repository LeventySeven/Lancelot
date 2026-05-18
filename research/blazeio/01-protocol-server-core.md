# Blazeio — Protocol Layer, Custom HTTP Parser & Server Core (Lancelot RE Mining Dossier)

> Source-level RE of `anonyxbiz/Blazeio` at `/Users/seventyleven/Desktop/researchfms/blazeio/Blazeio/Blazeio`.
> Purpose: extract every micro-optimization in Blazeio's from-scratch pure-asyncio HTTP/1.1 server so they can be (a) ported into Lancelot's Python fast-path verbatim and (b) used as the spec/benchmark target for Lancelot's Rust core.
> Labels: **KNOWN** = read directly from source (file:line cited). **INFERRED** = behavior deduced from code semantics. **DESIGN-NOTE** = Lancelot implication, not a Blazeio claim.
> This file goes *deeper than* `teardowns/BLAZEIO.md` on the receive/parse/backpressure hot path specifically; the teardown is the broad map, this is the protocol-core drill.

---

## 0. Executive summary — the killer micro-optimizations + the hard ceiling of pure Python

1. **`BufferedProtocol` + a single preallocated `bytearray` + `memoryview` window = one kernel→userland copy per `recv`, zero allocation in steady state.** Blazeio implements `get_buffer()`/`buffer_updated()` (PEP 524-era buffered protocol), not `data_received()`. The kernel writes directly into a reused `bytearray(102400)`; the only copy is `bytes(memoryview[:nbytes])` to detach the slice before reuse. (`Protocols/__init__.py:6-18`)
2. **Read is paused by default and pumped on demand — explicit, hand-rolled flow control instead of asyncio's automatic transport pausing.** `connection_made` calls `transport.pause_reading()` immediately; reading only resumes when a coroutine `await`s the protocol and the stream buffer is empty. The socket is *never* read ahead of the consumer. (`server_protocol.py:48`, `Protocols/__init__.py:45-67`)
3. **`SharpEvent` — a pooled-future event that beats `asyncio.Event`.** It reuses one `Future` across all waiters, sets the result directly (no `call_soon` round-trip per waiter where avoidable), and offers a fused `wait_clear()` (await + clear in one call) so the buffer-pump loop does one await per chunk with no separate `.clear()` scheduling. (`Dependencies/__init__.py:146-200`)
4. **The parser is `bytes.find()` all the way down — no regex, no state machine, no per-byte Python loop.** Header terminator, request line, header lines, chunk sizes are all located with C-level `bytes.find` / slicing. Zero Python-level byte iteration on the hot path. (`Parsers/min_http_parser.py:36-97`)
5. **Parsing is deferred and lazy.** The protocol does *not* parse on receive. Bytes pile into `__stream__` (a `deque`); the request line + headers are parsed only when the route handler first iterates the request, and the body is never materialized unless the handler reads it. `HEAD`/no-body methods short-circuit body reading entirely. (`min_http_parser.py:75-87`, `server_protocol_extratools.py:294-305`)
6. **`prepend()` — O(1) pushback of over-read bytes.** When the parser over-reads (body bytes arrive in the same `recv` as headers, or a chunk straddles a boundary) the surplus is `appendleft`'d back onto the same `deque` and reading is re-paused — no re-buffering, no copy of the already-parsed prefix.
7. **`TCP_NODELAY` always, plus `TCP_QUICKACK` + `TCP_FASTOPEN` + tuned keepalive** set on the listening socket so every accepted connection inherits them. Nagle is off so small writes (SSE/LLM tokens) hit the wire immediately. (`Utils/socket_ops.py:17-33`, `multiplexer.py:215-216`)
8. **Writer back-pressure is consumer-cooperative, not buffer-blocking.** `pause_writing()`/`resume_writing()` flip one bool + set an event; `writer()` awaits that event *before* every `transport.write`, so a slow client streaming LLM tokens throttles the producer coroutine without ballooning the asyncio write buffer. (`Protocols/__init__.py:29-43`, `server_protocol.py:64-72`)

**The hard ceiling of pure Python (the case for Lancelot's Rust core):**
- Every `buff.find(b"\r\n\r\n")` re-scans from byte 0 of the accumulated buffer on every `recv` until the terminator arrives — **O(n·k) header scanning** under slow-loris / fragmented headers. A Rust parser scans each byte once with a resumable state machine.
- `bytes(memoryview[:nbytes])` is a mandatory copy + allocation per chunk because the receive buffer must be reused; a Rust core can hand out `&[u8]` / `Bytes` slices with refcounted zero-copy and never allocate.
- Header parsing builds a Python `dict` of `str` (`.decode()` + `.capitalize()` per header) — each header is ≥2 Python object allocations + a UTF-8 decode. httptools/Rust parse into a borrowed/interned representation.
- The per-chunk pump does ≥1 `Future` creation + `loop.create_future()` + done-callback even with `SharpEvent` pooling; in Rust the await point is a single epoll readiness with no per-chunk heap object.
- Chunked decoding (`handle_chunked`) does repeated `bytearray` slicing + `int(s, 16)` + `memarray()` reallocation per chunk — every reslice is a copy. Rust `httparse`/`h1` decodes chunk framing in place.

Net: Blazeio's design is the *best pure-Python can do* — it removes every avoidable allocation and defers all work — but it is structurally bounded by (a) the buffer-reuse copy, (b) re-scanning `find`, (c) Python dict/str header objects, (d) per-chunk future churn. Lancelot's Rust core should own exactly these four; Lancelot's Python layer should copy Blazeio's deferral/backpressure/socket-option choices verbatim.

---

## 1. The asyncio `BufferedProtocol` implementation — the zero-copy receive core

### 1.1 Class topology (KNOWN)

`server_protocol.py:16`:
```python
class BlazeioServerProtocol(BlazeioProtocol, BufferedProtocol, BlazeioPayloadUtils, ExtraToolset):
```

The MRO is the whole story:
- `asyncio.BufferedProtocol` (stdlib) — the *buffered* flavor of the protocol API. asyncio calls `get_buffer(sizehint)` to ask the protocol for a writable buffer, writes the `recv()` result *directly into it*, then calls `buffer_updated(nbytes)`. **No `data_received(data: bytes)` is ever called** — that is the standard path and it forces asyncio to allocate a fresh `bytes` per recv. Blazeio opts out of that allocation.
- `BlazeioProtocol` (`Protocols/__init__.py:4`) — the hand-rolled buffer/flow-control mixin (the actual core).
- `BlazeioPayloadUtils` (`server_protocol.py:4`) — `transporter`, `close`, `control`.
- `ExtraToolset` (`server_protocol_extratools.py:156`) — writers, chunked decode, header serialization.

`BlazeioServerProtocol.__slots__` (`server_protocol.py:17`) — **every** attribute is slotted. No `__dict__` per connection. At 10k concurrent connections this is ~10k× the difference between a slotted object (~fixed C struct of pointers) and a dict-backed object (~232 B dict + rehash). The slot tuple is 33 entries long; memorize that the request object *is* the protocol object — Blazeio does not allocate a separate `Request`/`Scope` per request (contrast: uvicorn allocates an ASGI `scope` dict + send/receive closures per request).

### 1.2 `__init__` — preallocate everything (KNOWN, `server_protocol.py:20-45`)

```python
def __init__(app, on_client_connected, evloop, INBOUND_CHUNK_SIZE=None):
    app.on_client_connected = on_client_connected
    app.__buff__ = bytearray(INBOUND_CHUNK_SIZE)          # <-- the reused receive buffer
    app.__stream__ = deque()                              # <-- chunk queue (FIFO + O(1) pushback)
    app.__buff__memory__ = memoryview(app.__buff__)       # <-- stable window over the buffer
    app.cancel_on_disconnect = True
    app.__is_buffer_over_high_watermark__ = False
    app.__is_at_eof__ = False
    app.__is_alive__ = True
    ...
    app.__evt__ = SharpEvent(evloop = evloop)             # <-- "data available" signal
    app.__overflow_evt__ = SharpEvent(evloop = evloop)    # <-- "write buffer drained" signal
    app.__initialize__()
```

`INBOUND_CHUNK_SIZE` defaults to `102400` (100 KiB) via `ioConf(INBOUND_CHUNK_SIZE = 102400, ...)` (`Dependencies/__init__.py:132`) threaded through `create_server`'s factory (`__init__.py:462`). So **every connection preallocates a 100 KiB `bytearray` once**, at connection-accept time, and reuses it for the entire connection lifetime. `memoryview(app.__buff__)` is created once; slicing a memoryview (`mv[:n]`) is O(1) and allocation-free (it's a new memoryview header pointing at the same buffer — Python still allocates the header object, ~88 B, but not the data).

**KNOWN tradeoff:** 100 KiB × N connections = 100 MiB resident at 1000 conns even when idle. uvicorn/h11 grow buffers lazily. This is a deliberate latency-over-memory trade. (DESIGN-NOTE: Lancelot should make this size adaptive or pool buffers — see §9.)

### 1.3 `get_buffer` — hand asyncio the reusable window (KNOWN, `Protocols/__init__.py:11-18`)

```python
def get_buffer(app, sizehint):
    if sizehint > len(app.__buff__memory__):
        app.__buff__ = bytearray(sizehint)
        app.__buff__memory__ = memoryview(app.__buff__)
    elif sizehint <= 0:
        sizehint = len(app.__buff__memory__)
    return app.__buff__memory__[:sizehint]
```

Mechanics:
- asyncio's `_SelectorSocketTransport` (or uvloop's) calls `get_buffer(sizehint)` before each `sock.recv_into`. Blazeio returns a **slice of the preallocated memoryview**. The kernel writes the TCP segment *straight into `__buff__`*. There is **no intermediate `bytes` object** — this is the single biggest allocation win vs. the default `data_received` path.
- Grow-only policy: the buffer only ever grows (when `sizehint` exceeds current size, e.g. uvloop sometimes hints larger), never shrinks. Amortizes to zero reallocations after warmup.
- `sizehint <= 0` guard: some loops pass `0`/`-1` meaning "give me whatever you have"; Blazeio returns the full buffer window.

**Why it's fast:** the default `Protocol.data_received(data)` path is: kernel → temp buffer in C → `PyBytes_FromStringAndSize` (alloc + copy) → your callback. The `BufferedProtocol` path is: kernel → *your* buffer (one copy, into memory you already own, zero alloc). On a 100k req/s server that's eliminating ~100k allocations/s of variable-size `bytes`.

**Steal-for-Lancelot:** Lancelot's Python fast-path MUST use `BufferedProtocol` + a per-connection reused buffer, not `data_received`. This is non-negotiable for parity. For the Rust core: the equivalent is `recv` into a `BytesMut` with `reserve()` amortization — same idea, but Rust additionally avoids the next copy (see §1.4).

**Where Rust wins:** `get_buffer` returns a memoryview *slice header* (heap object, ~88 B) every recv, and the slice must be detached (`bytes(...)`, see §1.4) before the buffer can be reused for the next recv — that detach is a mandatory full copy. A Rust core using `BytesMut::split_to(n)` hands the consumer an owned, refcounted `Bytes` with **no copy** (cheap atomic refcount bump) and reuses the tail in place. Blazeio *cannot* do refcounted slices because Python's `memoryview` over a mutable `bytearray` that you're about to overwrite is unsafe to retain. So Blazeio's copy at `buffer_updated` is the price of pure Python.

### 1.4 `buffer_updated` — the pause + detach + signal (KNOWN, `Protocols/__init__.py:6-9`)

```python
def buffer_updated(app, nbytes):
    app.transport.pause_reading()
    app.__stream__.append(bytes(app.__buff__memory__[:nbytes]))
    app.__evt__.set()
```

Three operations, in this exact order, every recv:
1. **`transport.pause_reading()` immediately.** The instant data lands, Blazeio stops the transport from reading more. This is *the* flow-control primitive: the socket will not be read again until a consumer explicitly resumes it (§1.6). It guarantees `__stream__` never grows beyond what arrives in flight before the consumer drains it. This is the opposite of asyncio's default (read continuously until the protocol's high-water mark). It makes Blazeio's memory bounded by `(in-flight recv) + (consumer lag)` rather than by an internal high-water threshold.
2. **`bytes(app.__buff__memory__[:nbytes])`** — the mandatory detach copy. `nbytes` is exactly how many bytes the kernel wrote. `__buff__memory__[:nbytes]` is a zero-copy view; wrapping it in `bytes(...)` copies *only the `nbytes` actually received* (not the full 100 KiB) into an immutable object that is safe to keep while `__buff__` is overwritten by the next recv. This is the one copy pure Python cannot remove.
3. **`app.__evt__.set()`** — wake any coroutine awaiting data. `SharpEvent.set` resolves the pooled future synchronously (§2).

`__stream__.append(...)` — appends to the right of the deque (FIFO order: consumer pops left). `deque` is the right structure: O(1) append/append-left/pop/popleft, and the pushback path (`prepend`, §1.7) needs O(1) `appendleft`.

**Why it's fast:** copy is bounded to `nbytes`, not buffer size. Pause-on-arrival means the deque is naturally short (usually 1 element). One event set, no scheduling of the consumer (the consumer is already parked on the future and gets resolved in place).

**Where Rust wins:** step 2 is unavoidable in Python; in Rust it's a refcount bump. Step 1 (`pause_reading`) is a syscall-free flag flip in asyncio but the *resume* later costs an epoll re-arm; a Rust core with a readiness-based reactor amortizes this. Step 3's `SharpEvent.set` still touches a `Future` object; a Rust `Notify`/oneshot is a single atomic.

### 1.5 `eof_received` / `connection_lost` / `pause_writing` / `resume_writing` (KNOWN, `Protocols/__init__.py:20-34`)

```python
def connection_lost(app, exc):
    app.__evt__.set()
    app.__overflow_evt__.set()
    if app.cancel and app.cancel_on_disconnect: app.cancel()

def eof_received(app):
    app.__is_at_eof__ = True
    app.__evt__.set()

def pause_writing(app):
    app.__is_buffer_over_high_watermark__ = True

def resume_writing(app):
    app.__is_buffer_over_high_watermark__ = False
    app.__overflow_evt__.set()
```

- `connection_lost`: wakes *both* events (so any coroutine parked on data **or** on write-drain unblocks and can observe the closed transport), then cancels the connection task (§1.6 `abort_connection`). Returning nothing (not `True`) — standard.
- `eof_received`: sets `__is_at_eof__` and wakes data waiters. **Returns `None`** (not `True`) — meaning asyncio will close the transport after EOF (it does not keep half-open). For a request/response server that's correct; for a half-close streaming protocol you'd `return True`. (INFERRED: Blazeio chooses full-close-on-EOF deliberately for HTTP/1.1.)
- `pause_writing`/`resume_writing`: asyncio calls these when the transport's *write* buffer crosses its high/low water marks. Blazeio reduces this to one bool + one event set. The producer side (`buffer_overflow_manager`, §3) consults the bool. This is the entire write-backpressure mechanism — see §3.

### 1.6 The read-pump: `ensure_reading` + `__await__` + `__aiter__` (KNOWN, `Protocols/__init__.py:45-67`)

```python
async def ensure_reading(app):
    if not app.transport.is_closing():
        if not app.__stream__:
            if not app.transport.is_reading():
                app.transport.resume_reading()
            await app.__evt__.wait_clear()
        if app.transport.is_closing(): app.abort_connection()
    else:
        app.abort_connection()

def __await__(app):
    yield from app.ensure_reading().__await__()
    return app.__stream__.popleft() if app.__stream__ else None

async def __aiter__(app):
    while True:
        await app.ensure_reading()
        while app.__stream__:
            yield app.__stream__.popleft()
```

This is the demand-driven core. Walkthrough of `ensure_reading`:
1. If transport closing → `abort_connection()` (raises `ClientDisconnected`, cancels the task).
2. If `__stream__` is **non-empty**, return immediately — there is already buffered data, do not touch the socket, do not await. (Fast path: no event-loop round-trip when data is already queued.)
3. If `__stream__` is empty: only **now** resume reading (`resume_reading()` re-arms the socket in the selector) *if not already reading*, then `await app.__evt__.wait_clear()` — park until `buffer_updated`/`eof_received`/`connection_lost` sets `__evt__`. `wait_clear()` = await the event then clear it atomically (one call, see §2).
4. After waking, re-check `is_closing()` → abort if closed.

`__await__` makes the protocol object **directly awaitable**: `chunk = await r` yields one chunk (or `None`). `__aiter__` makes it **async-iterable**: `async for chunk in r` yields chunks until... forever (the caller breaks on content-length/EOF — the parser layer enforces termination, §4).

**Why it's fast / the key trick:** *reading is never ahead of the consumer.* The socket is paused on every arrival and only un-paused when a consumer is actually waiting and the queue is empty. This means:
- No unbounded read-ahead → bounded memory, no head-of-line buffering.
- Zero event-loop round-trips when data is already buffered (step 2 — extremely common when a request arrives in one segment).
- The "resume_reading only if not is_reading" guard avoids redundant selector mutations.

**Where Rust wins:** every empty-queue path does `await loop.create_future()` (inside `SharpEvent`) — a heap `Future` + a done-callback + a selector re-arm. Under high-concurrency low-data (many idle keepalive conns waking on a single byte) that's a `Future` per wake. A Rust reactor parks the task on the epoll readiness directly; no per-wake heap object. Also `transport.is_reading()`/`is_closing()` are Python method calls (attribute lookup + call) on the hot path — trivially inlined in Rust.

**FastAPI/uvicorn comparison:** uvicorn's `httptools` protocol uses `data_received` (the allocating path) and *does not pause reading per-segment* — it reads continuously and feeds the httptools parser, relying on httptools' C state machine to buffer. uvicorn's flow control pauses only at the ASGI app's `receive()` boundary via a `FlowControl` helper. Blazeio's per-segment pause is finer-grained (lower memory under bursty load) but costs a `pause/resume` pair per segment vs. uvicorn's amortized continuous read. Robyn bypasses this entirely — its actix/Rust core never surfaces per-segment buffering to Python; the Python handler only sees a fully-parsed request. **DESIGN-NOTE:** Lancelot's Rust core should adopt Robyn's model (Python never sees raw segments for normal requests) but expose Blazeio's per-segment async-iter for *streaming* request bodies (uploads, where backpressure matters).

### 1.7 `prepend` — O(1) pushback (KNOWN, `Protocols/__init__.py:36-39`)

```python
def prepend(app, data):
    if app.transport.is_reading(): app.transport.pause_reading()
    app.__stream__.appendleft(data)
    app.__evt__.set()
```

When a parser over-reads (the recv that completed the header line also contained body bytes; or a chunked frame straddled a segment), the surplus is pushed back to the **front** of the deque with `appendleft` (O(1)), reading is re-paused, and the data-available event is re-set so the next consumer immediately sees it. **No re-buffering, no copy of the already-consumed prefix, no re-parse.** This is how the parser hands the body bytes that arrived early straight to the body reader without a second syscall or a staging buffer. Compare: h11/uvicorn keep a single growable receive buffer and an offset; Blazeio keeps a deque of immutable chunks and does pure pointer surgery.

`__init__.py:154` (`parse_default`) and `min_http_parser.py:172-174` (`aparse`) both call `r.prepend(body)` after locating `\r\n\r\n` — the canonical "headers and body arrived together" fast path.

---

## 2. `SharpEvent` — the pooled-future signal that replaces `asyncio.Event` (KNOWN, `Dependencies/__init__.py:146-200`)

This primitive is used for *every* await in the protocol hot path: data-available (`__evt__`), write-drain (`__overflow_evt__`), stream-closed, stream-ack. Its performance characteristics matter more than the parser's.

```python
class SharpEvent:
    __slots__ = ("_set", "_waiters", "loop", "auto_clear")
    def __init__(app, auto_clear=False, evloop=None):
        app._set, app._waiters, app.loop, app.auto_clear = False, [], evloop or get_event_loop(), auto_clear

    def is_set(app): return app._set

    def get_fut(app):
        if app._waiters:
            return app._waiters[0]                      # <-- REUSE the one outstanding future
        app._waiters.append(fut := app.loop.create_future())
        fut.__is_cleared__ = False
        fut.__acallbacks__ = [app.fut_done]
        fut.add_done_callback(app.done_callback_orchestrator)
        return fut

    async def wait(app):
        if app._set: return True                        # <-- fast path: no future at all if already set
        return await app.get_fut()

    async def wait_clear(app):                          # <-- FUSED await+clear
        result = await app.wait()
        app.clear()
        return result

    def clear(app): app._set = False

    def set(app, item=True):
        app._set = True
        if len(app._waiters) == 1:
            if not app._waiters[0].done(): app._waiters[0].set_result(item)
        else:
            for fut in app._waiters:
                if not fut.done(): fut.set_result(item)
        app._waiters.clear()
```

### 2.1 Why it beats `asyncio.Event` (KNOWN mechanics, INFERRED perf rationale)

`asyncio.Event` internally: `wait()` creates a **new `Future` per waiter every call**, appends to a list, awaits it; `set()` iterates all waiters and `fut.set_result(True)` each. `clear()` is separate. For a tight pump loop `while: await evt.wait(); evt.clear()` that is **one Future allocation + one list append + one done-callback registration + one list-remove per iteration**, plus the `set_result` schedules the waiter via the loop's ready queue (`call_soon`).

`SharpEvent`'s wins, each measurable on the per-chunk path:
1. **Already-set fast path (`wait`):** if `_set` is already `True` (data arrived before the consumer parked — extremely common: request fits in one segment), `wait()` returns `True` **without ever creating a Future**. Zero allocation, zero loop interaction. `asyncio.Event` still allocates+awaits a future even when set (it short-circuits with `return True` only in 3.10+, but still no pooling for the unset case).
2. **Single pooled future (`get_fut`):** when multiple coroutines wait on the same event, they all share `_waiters[0]` — *one* `Future`, not N. The protocol only ever has one consumer per event in practice, but the pooling means consecutive `wait()` calls in a loop reuse the future created by `get_fut` until it's resolved.
3. **Fused `wait_clear()`:** the pump does `await app.__evt__.wait_clear()` — await + clear in a single coroutine call, instead of two statements. Saves one Python-level call and removes the window where the event is set-but-not-cleared (which would make the next `wait()` wrongly fast-path).
4. **`set()` clears the waiter list itself:** after resolving, `_waiters.clear()`. The done-callback (`fut_done`) auto-clears `_set` when `auto_clear=True`. So the steady state after a wake is: future resolved, list empty, ready for the next `get_fut` to allocate exactly one new future.

**Net per-chunk cost when data is buffered ahead (common case):** `wait()` → `_set is True` → `return True`. **Zero futures, zero loop.create_future, zero call_soon.** This is the single most important micro-optimization in the whole receive path — it means a pipelined/keepalive client whose next request is already in the buffer is served with **no event-loop round-trip at all**.

**Where Rust wins:** even the slow path (`get_fut`) costs `loop.create_future()` (a `Future` object: ~`__slots__` C struct but still a PyObject + `add_done_callback` list), and the done-callback machinery (`done_callback_orchestrator` iterating `__acallbacks__`). A Rust `tokio::sync::Notify` or a oneshot is a single `AtomicWaker` store — no heap object per wait. Under 100k idle keepalive connections each waking once, that's 100k `Future` objects/s for SharpEvent vs. zero for a Rust reactor.

**Steal-for-Lancelot:** if Lancelot keeps a Python event-loop fast-path (it should, for the 99% of handlers that are pure-Python async), `SharpEvent` is a drop-in upgrade over `asyncio.Event` and is **mandatory** — copy it verbatim. The fast-path `if _set: return True` and fused `wait_clear()` are free wins. (Caveat in §2.2.)

### 2.2 The `SharpEvent` correctness caveat (INFERRED — important for Lancelot)

`SharpEvent.set()` resolves the future **synchronously, then clears `_waiters`**. The auto-clear of `_set` happens in the future's done-callback (`fut_done`), which fires when the awaiting coroutine is *scheduled*, not when `set()` is called. This is a deliberate single-producer/single-consumer design: it is correct **only** when there is at most one logical waiter per event and sets are not lost between `set()` and the waiter resuming. The protocol guarantees this (one pump coroutine per connection per event). **DESIGN-NOTE:** if Lancelot reuses `SharpEvent` for multi-waiter fan-out (e.g. broadcast to many SSE subscribers) it must NOT use this class as-is — the `_waiters.clear()` + shared-future model will drop wakeups. Use it only for the 1:1 protocol signals (data-available, write-drain) exactly as Blazeio does. There is also a sibling `SharpEventLab` (`alts.py:50-101`) that is nearly identical — the `alts.py` copy is the lab/experimental variant; the `Dependencies/__init__.py` one is canonical.

### 2.3 `ioCondition` — the lock/condition variable (KNOWN, `alts.py:103-191`)

Used by the multiplexer for write serialization (`__busy_write__`, `__sync__`, `__io_create_lock__`) and by `Stream.__busy_stream__`. It's a hand-rolled condition built from four `SharpEvent`s (`event`, `_lock_event`, `_waiter_event`, `_acquire_event`) plus integer counters. Key tricks:
- `initial` flag (`alts.py:157-159`): the first `wait()` on a fresh condition while `event` is set returns immediately by just clearing the event — no future, no park. This is a "first iteration is free" optimization for producer/consumer loops where the first item is already available.
- `acquire()` (`alts.py:141-149`) spins on `_lock_event.wait_clear()` rather than a fair queue — simple, low-overhead, but **not FIFO-fair** (INFERRED: acceptable because contention is per-connection, near-zero in practice).

**DESIGN-NOTE:** Lancelot's Rust core makes `ioCondition` unnecessary for the protocol path (write serialization is a Rust mutex/`mpsc`); keep `ioCondition` only if the Python layer needs in-process async coordination.

---

## 3. Write-path backpressure — consumer-cooperative flow control (KNOWN)

This is the section that matters most for **AI / LLM token streaming** — see §8 for the AI-specific analysis; here is the mechanism.

### 3.1 `writer()` — await drain *before* every write (KNOWN, `server_protocol.py:64-72`)

```python
async def writer(app, data: (bytes, bytearray)):
    if not app.__is_prepared__:
        app.__is_prepared__ = True
    await app.buffer_overflow_manager()        # <-- park here if the client is slow
    if not app.transport.is_closing():
        app.transport.write(data)
    else:
        app.abort_connection()
```

### 3.2 `buffer_overflow_manager` (KNOWN, `Protocols/__init__.py:41-43`)

```python
async def buffer_overflow_manager(app):
    if not app.__is_buffer_over_high_watermark__: return
    await app.__overflow_evt__.wait_clear()
```

The complete write-backpressure loop:
1. asyncio calls `pause_writing()` when the **transport's** write buffer exceeds its high-water mark (default asyncio: 64 KiB; Blazeio overrides via `set_write_buffer_limits`, §3.3). Blazeio sets `__is_buffer_over_high_watermark__ = True`.
2. Next time the handler calls `writer(data)`, `buffer_overflow_manager` sees the flag → `await __overflow_evt__.wait_clear()` → the **producing coroutine parks**. The handler (e.g. the LLM token loop) stops producing.
3. asyncio drains the OS socket buffer to the slow client; when the transport write buffer falls below the low-water mark it calls `resume_writing()` → flag cleared, `__overflow_evt__.set()`.
4. The parked producer wakes, finishes the `await`, writes the next chunk.

**Why it's fast / why it's correct:** the fast path (`if not app.__is_buffer_over_high_watermark__: return`) is a single attribute read and an early return — **zero overhead when the client is keeping up** (the overwhelming common case). Backpressure only engages when genuinely needed. The producer is throttled at the *application coroutine* level, so a slow client cannot make the server accumulate an unbounded write buffer (the classic asyncio footgun: `transport.write()` in a loop with no `drain()` → OOM). Blazeio's `writer` *is* the drain, fused into every write.

**Where Rust wins:** marginally — the flag check is cheap in both. But Rust's `AsyncWrite` + `poll_flush` integrates backpressure into the write call itself with no separate event object; Blazeio's `__overflow_evt__` is a `SharpEvent` (heap future on the slow path). The difference is negligible vs. the receive path; this design is *good enough* and Lancelot should copy it for the Python fast-path verbatim.

### 3.3 Write buffer limits — explicit, large (KNOWN, `multiplexer.py:7,90,218-219`)

```python
# BlazeioMultiplexerConf.conf  (multiplexer.py:7)
_write_buffer_limits = ((1024**2)*10, 0)     # high=10 MiB, low=0
# applied at multiplexer.py:218-219
def update_protocol_write_buffer_limits(app):
    app.protocol.transport.set_write_buffer_limits(*app._write_buffer_limits)
```

`set_write_buffer_limits(high=10*1024*1024, low=0)`:
- **high = 10 MiB:** `pause_writing` only fires when 10 MiB is queued in the transport. This is *huge* vs. asyncio's 64 KiB default. Rationale (INFERRED): tolerate large bursts (a 10 MiB response can be `transport.write()`'d in one shot without ever parking the producer) — favors throughput/latency for fast clients at the cost of memory for slow ones.
- **low = 0:** `resume_writing` only fires when the buffer is **completely drained**. This means once backpressure engages, the producer stays parked until the *entire* transport buffer is flushed — a deliberate hysteresis that avoids rapid pause/resume thrashing (each pause/resume is a Python callback). Wide gap (0 → 10 MiB) = at most one pause and one resume per 10 MiB, not per segment.

**KNOWN:** these limits are only applied on the **multiplexer** path (`multiplexer.py` constructs `BlazeioMultiplexer` which calls `update_protocol_write_buffer_limits`). The plain `BlazeioServerProtocol` HTTP path does **not** call `set_write_buffer_limits` anywhere I found — so a non-multiplexed HTTP connection uses asyncio's default 64 KiB high / 16 KiB low. (INFERRED gap: the HTTP server relies on asyncio defaults for write backpressure; only the mux/stream protocol opts into the 10 MiB window.) **DESIGN-NOTE for Lancelot:** make the write-buffer-limit a tunable on the HTTP server too; 10 MiB high / 0 low is a good default for LLM streaming (few pause/resume events), 64 KiB for memory-constrained multi-tenant.

---

## 4. The from-scratch HTTP/1.1 parser — `bytes.find` all the way down (KNOWN, `Parsers/min_http_parser.py`)

Blazeio ships **three** parser implementations of the same algorithm:
- `MinParser` (`min_http_parser.py:36-97`) — the canonical server request parser.
- `Request.prepare_http_request`/`set_method`/`HTTPParser` (`Modules/request.py:41-286`) — an older/alternate server parser used by `parse_default` / `serve_route_*`.
- `MinParserClient` (`min_http_parser.py:99-175`) — the response parser for the HTTP client.

They are algorithmically identical; I analyze `MinParser` as the reference and note divergences.

### 4.1 The config table (KNOWN, `min_http_parser.py:6-34`)

```python
network_config.http.one_point_one = ddict(
    crlf              = b"\r\n",
    dcrlf             = b"\r\n\r\n",
    protocol          = b"HTTP/1.1",
    initial_delimiter = b' ',
    methods           = (b"GET",b"HEAD",b"POST",b"PUT",b"DELETE",b"CONNECT",b"OPTIONS",b"TRACE",b"PATCH"),
    headers           = ddict(delimiter = b': '),
)
```
All delimiters are **byte literals**, precomputed once at class load. `len(...)` of each is recomputed inline at every use site (e.g. `idx + len(app.network_config.http.one_point_one.crlf)`) — a micro-inefficiency (constant `len(b"\r\n")==2` re-evaluated each header; ddict attribute chain walked each time). **DESIGN-NOTE:** trivial Lancelot win — hoist `CRLF=2`, `DCRLF=4`, `SP=1` as module ints; Blazeio leaves perf on the table here via the `ddict` attribute-chain lookups (`app.network_config.http.one_point_one.crlf` is 4 attribute lookups per header line).

### 4.2 `parse` — split headers from body in one `find` (KNOWN, `min_http_parser.py:89-97`)

```python
def parse(app, r, buff: bytes):
    idx = buff.find(app.network_config.http.one_point_one.dcrlf)   # find b"\r\n\r\n"
    header, body = buff[:idx], buff[idx + 4:]                       # slice (copies)
    try:
        app.set_method(r, header)
    except UnicodeDecodeError:
        raise Abort("Bad Request", 400)
    return body
```

- **One `bytes.find(b"\r\n\r\n")`** locates the end of the header block. This is a C-level Boyer-Moore-ish scan (CPython's `stringlib` find — actually a tuned two-way / Crochemore-Perrin for long needles, memchr-accelerated for the first byte). Far faster than any Python loop.
- `buff[:idx]` and `buff[idx+4:]` each **copy** (slicing `bytes` copies). The body slice copy is then handed back via `prepend`. (Rust would slice without copying.)
- **Caller responsibility:** `parse` assumes the full header block is already in `buff`. The "have we received the full headers yet?" loop lives in the caller (`parse_default` at `__init__.py:142-156`, or `aparse` at `min_http_parser.py:154-175`):

```python
# __init__.py:142  parse_default
buff = bytearray()
while not b"\r\n\r\n" in buff:                                  # <-- the re-scan
    if len(buff) >= 102400:                                     # __http_request_max_buff_size__
        raise Abort("...too much data...", 413)
    if (chunk := await r): buff.extend(chunk)
    else: raise Abort("Bad Request", 400)
if (body := MinParsers.server.parse(r, buff)): r.prepend(body)
```

**The O(n·k) header-scan problem (the Rust motivation):** `while not b"\r\n\r\n" in buff` (or `buff.find` in `aparse`) **re-scans the entire accumulated buffer from byte 0 on every received segment** until the terminator appears. For a request whose headers arrive in one segment (normal): one scan, O(n), fine. For headers fragmented across S segments (slow client, large cookie header, slow-loris attack): the scan runs S times over a growing buffer → **O(S × total_header_bytes)**. The 102400-byte cap (`__http_request_max_buff_size__`) bounds the worst case to ~100 KiB × S but does not eliminate the quadratic. `bytes.__contains__`/`find` is C-fast so the constant is tiny, but the algorithmic shape is wrong. **A resumable Rust parser (httparse / a hand-rolled state machine) scans each byte exactly once across all segments — O(total_header_bytes), period.** This is the single strongest architectural argument for Lancelot's Rust core: the *common case is fine in Python*, the *adversarial/fragmented case is structurally bad in Python and structurally fine in Rust*.

### 4.3 `set_method` — request-line + header split, all `find`/slice (KNOWN, `min_http_parser.py:58-87`)

```python
def set_method(app, r, header: bytes):
    if (idx := header.find(b' ')) == -1:
        raise Abort("Request has no headers", 400)
    r.method, header = header[:idx].decode("utf-8"), header[idx + 1:]      # METHOD

    if (idx := header.find(b' ')) == -1:
        raise Abort("Request has no method", 400)
    tail, header = header[:idx].decode("utf-8"), header[idx + 1:]          # request-target
    r.tail, r.path = tail, tail

    if (idx := r.path.find('?')) != -1:                                    # strip query
        r.path = r.path[:idx]

    app.header_parser(r, header)                                           # rest = headers + "HTTP/1.1\r\n..."

    if r.headers:
        if not r.pull:
            if (content_length := r.headers.get("Content-length")):
                r.content_length = int(content_length)
                r.pull = r.handle_raw
            elif (transfer_encoding := r.headers.get("Transfer-encoding")):
                r.transfer_encoding = transfer_encoding
                r.pull = r.handle_chunked
            else:
                r.content_length = 0
                r.pull = r.handle_raw
    return r
```

Mechanics & micro-optimizations:
- Request line parsed by **two `find(b' ')`** calls: first space → method, second space → target; the remainder (HTTP version + CRLF + headers) is *not parsed for the version at all* — the `HTTP/1.1\r\n` token is simply left as the first "header line" and silently dropped by `header_parser` (it has no `: ` so `header_make` no-ops on it). **The HTTP version is never validated or stored.** (KNOWN: the server treats every request as HTTP/1.1.) This is a deliberate corner-cut: skip version parsing entirely. (INFERRED: acceptable for a perf-first server behind a known client/LB; a spec-conformant server must reject unknown versions.)
- `r.tail` keeps the full target (with query); `r.path` is `tail` truncated at `?` via one `find('?')`. Query parsing is **lazy** — only done if the handler calls `.params()` (`Modules/request.py:104-128,213-223`). Zero query-string work for handlers that don't read params.
- **The pull-method dispatch is the body-strategy decision, made once at parse time:**
  - `Content-Length` present → `r.content_length = int(...)`, `r.pull = r.handle_raw` (length-delimited body reader).
  - `Transfer-Encoding` present → `r.pull = r.handle_chunked` (chunked decoder).
  - neither → `content_length = 0`, `r.pull = r.handle_raw` (no body).
  `r.pull` is bound to a method object **once**; every later body read just calls `r.pull()` with no re-branching. This is a classic "decide once, dispatch via bound method" optimization.
- All string conversions are `.decode("utf-8")` only on the **method** and **target** (small, bounded) — never on the whole buffer.

### 4.4 `header_parser` / `header_make` — header dict construction (KNOWN, `min_http_parser.py:40-56`)

```python
def header_make(app, r, header: bytes):
    if (idx := header.find(b': ')) != -1:
        if (key := header[:idx].decode().capitalize()) in r.headers:
            if not isinstance(r.headers[key], list):
                r.headers[key] = [r.headers[key]]
            r.headers[key].append(header[idx + 2:].decode())
        else:
            r.headers[key] = header[idx + 2:].decode()

def header_parser(app, r, header: bytes):
    r.headers = ddict()
    while header:
        if (idx := header.find(b"\r\n")) == -1:
            app.header_make(r, header)
            break
        else:
            _, header = app.header_make(r, header[:idx]), header[idx + 2:]
```

- One `find(b"\r\n")` per header line; slice the line off the front (`header = header[idx+2:]` — copies the *remaining* buffer each iteration → **O(H²) in header count** for H headers, since each iteration re-copies the tail). This is the second quadratic in the parser (after §4.2's re-scan). For typical 10–20 headers it's negligible; for pathological header counts it degrades. A Rust parser advances an index, never recopies.
- Per header: `header[:idx].decode()` (key bytes→str) + `.capitalize()` (normalize `content-type`→`Content-type`, allocates a new str) + dict insert + value `.decode()`. **≥3 Python object allocations + 2 UTF-8 decodes per header.** For a request with 15 headers that's ~45 allocations + 30 decodes — all on the request hot path. This is precisely what httptools/Rust avoid by parsing into a list of `(slice, slice)` borrowed views and decoding lazily.
- Duplicate headers → value promoted to a `list` and appended (correct multi-value handling, e.g. multiple `Set-Cookie`/`Cookie`).
- `.capitalize()` normalization means lookups must use `"Content-length"` casing (first-letter-upper, rest-lower). `MinParserClient` uses `.lower()` instead (`min_http_parser.py:105`) — response headers are lowercased. Inconsistent casing convention between request and response parsers (KNOWN quirk).

### 4.5 `aparse` — the client-side streaming header accumulator (KNOWN, `min_http_parser.py:154-175`)

```python
async def aparse(app, r):
    valid, buff = 0, bytearray()
    while True:
        if (chunk := await r.protocol): buff.extend(chunk)
        if not valid:
            if (idx := buff.find(b"HTTP/1.1")) != -1 or (idx := buff.find(b"HTTP/1.0")) != -1:
                valid, buff = 1, buff[idx:]                       # <-- discard pre-status garbage
            else:
                buff = buff[-len(b"HTTP/1.1"):]                   # <-- keep only last 8 bytes
                continue
        if b"\r\n\r\n" in buff: break
        if len(buff) >= app.max_buff_size:                        # 102400 default
            raise ClientGotInTrouble("headers exceeded the max_buff_size")
    if (body := app.parse(r, buff)): r.protocol.prepend(body)
    return r
```

Two notable tricks in the client response parser:
- **Sliding 8-byte resync window:** until the `HTTP/1.x` status-line marker is found, the buffer is truncated to its last `len(b"HTTP/1.1") == 8` bytes each iteration (`buff = buff[-8:]`). This bounds memory while waiting for a valid status line and handles the marker straddling a segment boundary (up to 7 bytes of the previous segment retained). Clever constant-memory resync.
- Same `b"\r\n\r\n" in buff` re-scan + `102400` cap as the server side.

### 4.6 `Request.prepare_http_request` — the alternate server parser (KNOWN, `Modules/request.py:264-286`)

The path actually used by `serve_route_*` (`__init__.py:159,182` call `app.__default_parser__`). Same shape: accumulate into `memarray()`, `find(b"\r\n\r\n")`, on hit slice heading/body, `set_method`, `prepend` the body, break. Body-strategy dispatch identical (`request.py:243-255`). Extra: `buff_limit_reached` raises `413` at `__http_request_max_buff_size__` (102400). `memarray()` (`alts.py`) is a `bytearray` factory — same accumulation pattern.

### 4.7 Parser scorecard vs. httptools (uvicorn/FastAPI) and actix (Robyn)

| Aspect | Blazeio `MinParser` | httptools (FastAPI/uvicorn) | actix `httparse` (Robyn) |
|---|---|---|---|
| Language | Pure Python | C (nodejs `http_parser` fork) | Rust |
| Algorithm | `bytes.find` + slice | Resumable byte-at-a-time state machine | SIMD-able resumable state machine |
| Header scan | **O(S·n) re-scan** per segment until `\r\n\r\n` | O(n) single pass, resumable across feeds | O(n) single pass, resumable |
| Header line strip | **O(H²)** tail recopy | O(1) index advance | O(1) index advance |
| Per-header alloc | ≥3 PyObjects + 2 decodes | callback into Python: 2 bytes objects, lazy decode | borrowed `&[u8]`, decode on demand |
| Body framing | bound method dispatch (good) | C state machine | Rust state machine |
| Version validation | **none** (assumes 1.1) | strict | strict |
| Copies | header/body slices copy | minimal (callbacks get views into feed buffer) | zero-copy `Bytes` |
| Robustness | weak (no version check, quadratics) | battle-tested | battle-tested |

**The honest verdict for Lancelot:** Blazeio's parser is *fast in the common case* (single-segment request: one `find`, a handful of `find`s, ~45 allocs) and its design choices (deferred parse, bound-method body dispatch, lazy query/decode) are excellent and should be copied into the Python fast-path. But it has **two quadratics** (segment re-scan, header tail-recopy) and **no version validation** that make it both slower and less safe than a Rust state-machine parser under fragmented/adversarial input. **Lancelot's Rust core should own HTTP parsing**; the Python `MinParser` is a good *fallback* and a good *spec/behavior oracle* for testing the Rust parser produces identical `method/path/headers/body` splits.

---

## 5. Body readers — `handle_raw` (length-delimited) & `handle_chunked` (transfer-encoding) (KNOWN, `server_protocol_extratools.py:241-305`)

`r.pull` is bound to one of these at parse time (§4.3). The handler reads the body via `async for chunk in r.pull():` — bytes are pulled from `__stream__` on demand, never buffered eagerly.

### 5.1 `handle_raw` — Content-Length-delimited streaming (KNOWN, `:294-305`)

```python
async def handle_raw(app, *args, **kwargs):
    if app.headers is None: await app.reprepare()
    if app.method in app.non_bodied_methods or app.current_length >= app.content_length: return
    async for chunk in app:                       # async-iterate the protocol (§1.6)
        if chunk:
            app.current_length += len(chunk)
            yield chunk
        if app.current_length >= app.content_length: break
```

- `non_bodied_methods = ("GET","HEAD","OPTIONS","DELETE")` (`server_protocol.py:18`): **GET/HEAD/OPTIONS/DELETE bodies are never read** — instant return, zero socket interaction even if the client sent a body. This is the "skip body work entirely for bodiless methods" fast path.
- `current_length >= content_length` guard at both top and loop-bottom: if the body already fully arrived (counted), return without awaiting. Termination is *exact byte count*, no sentinel scan.
- `async for chunk in app` is `__aiter__` (§1.6) — pulls whatever segments are queued, each yielded straight through (no copy, no concatenation — the handler gets the raw `bytes` chunks as they came off the wire). `current_length` accumulates `len(chunk)`; loop breaks the moment the count reaches `content_length`. **Over-read is not handled here** — if a chunk overshoots `content_length`, the whole chunk is yielded (the extra bytes belong to the *next* pipelined request and would be mis-attributed). (KNOWN gap: `handle_raw` does not split the final chunk at `content_length`; contrast `Rutils.read_exactly` at `:101-119` which *does* clamp via `chunk[:rem]`. So streaming body reads don't pipeline-correct, but `read_exactly`/`json` do.)

### 5.2 `read_exactly` — clamped exact read into a preallocated buffer (KNOWN, `:101-119`)

```python
@classmethod
async def read_exactly(cls, app=None, size: int = 0, decode=False):
    ...
    data, point = memarray(size), 0                   # preallocate exact-size buffer
    async for chunk in app.pull():
        len_ = len(chunk)
        rem = (size - point)
        if len_ > rem:
            chunk = chunk[:rem]                        # clamp overshoot
            len_ = len(chunk)
        data[point: point + len_] = chunk              # copy into the preallocated buffer
        point += len_
        if point >= size: break
    return data if not decode else data.decode()
```

- `memarray(size)` preallocates the **exact** content-length buffer once; chunks are *copied into it* at `data[point:point+len_]` (slice-assign into a `bytearray` — C memcpy, no realloc). One allocation for the whole body vs. a `bytearray.extend` growth loop (which reallocs O(log n) times). This is the right pattern when the length is known.
- `chunk[:rem]` clamps the final chunk so exactly `size` bytes are consumed. (Note: the *unconsumed* tail of an overshooting chunk is **dropped here too** — `read_exactly` doesn't `prepend` the remainder. INFERRED: fine for request bodies on a non-pipelined connection, lossy under pipelining.)

### 5.3 `handle_chunked` — the from-scratch chunked decoder (KNOWN, `:241-275`)

This is the most intricate parser in the codebase. Verbatim-equivalent walkthrough:

```python
async def handle_chunked(app, *args, **kwargs):
    if app.headers is None: await app.reprepare()
    end, buff = False, memarray()                       # buff = bytearray accumulator
    read, size, idx = 0, False, -1                      # size==False means "need to parse a chunk header"
    async for chunk in app:                             # pull raw segments
        if size == False:
            buff.extend(chunk)                          # accumulate until we have the size line
            if (idx := buff.find(b"\r\n")) == -1: continue          # size line not complete yet
            if not (s := buff[:idx]): continue          # empty before CRLF -> keep waiting
            size, buff = int(s, 16), memarray(buff[idx + 2:])       # parse HEX size, drop "<hex>\r\n"
            if size == 0: end = True                                # last chunk (0\r\n\r\n)
            if len(buff) >= size:
                chunk, buff = buff, memarray(buff[len(buff):])      # whole chunk already buffered
            else:
                chunk, buff = buff[:size], memarray(buff[len(buff):])
        read += len(chunk)
        if read > size:                                 # we read past this chunk's data
            chunk_size = len(chunk) - (read - size)
            chunk, __buff__ = chunk[:chunk_size], memarray(chunk[chunk_size + 2:])  # +2 skips trailing CRLF
            app.prepend(__buff__)                        # push leftover back (next chunk header)
            read, size = 0, False                        # reset -> parse next chunk header
        yield chunk
        if end: break
```

Mechanics & the tricks:
- **`size == False` sentinel** as the "I am in chunk-header state" flag (vs. `size` being an int = "in chunk-body state"). A two-state machine encoded in the type of one variable. `int(s, 16)` parses the hex chunk-size line directly (C-level int parse).
- `buff.find(b"\r\n")` locates the size line; `int(buff[:idx], 16)` is the size. Chunk extensions (`;name=value` after the size) are **not stripped** before `int(s,16)` — `int("1a;foo", 16)` raises `ValueError`. (KNOWN gap: chunk extensions break this decoder. Rare in practice; httptools/Rust handle them.)
- `size == 0 → end = True`: detects the terminating `0\r\n\r\n` chunk. Trailers after the final chunk are **not parsed**.
- **Over-read handling is correct here (unlike `handle_raw`):** when `read > size`, the chunk is split at the true chunk boundary (`chunk[:chunk_size]`), the trailing CRLF is skipped (`chunk[chunk_size + 2:]`), and the remainder is `prepend`'d back so the next iteration re-enters header-parsing state. This is the proper streaming-chunked behavior.
- **The cost:** every state transition does `memarray(buff[...])` — a fresh `bytearray` **copy** of the remaining buffer. For a body of C chunks this is O(C) reallocations + copies of shrinking tails → effectively O(total_body_size) extra copying in the worst case (each leftover recopied). `app.prepend` then pushes a copy back into the deque. **A Rust chunked decoder (`httparse`/`h1`) decodes framing in place with index advancement — zero copies.** This is the third structural Python penalty (after header re-scan and header tail-recopy).

**Why it's still fast enough in practice:** chunked request bodies are uncommon (most clients send `Content-Length`); the common path is `handle_raw` which is allocation-light. The chunked path's copies are bounded by body size and only hurt for large chunked uploads.

**Steal-for-Lancelot:** the *state design* (sentinel-typed state var, prepend-the-remainder, exact-boundary split) is correct and worth copying for a pure-Python fallback. But chunked decoding is a prime candidate for the Rust core (zero-copy framing). Use Blazeio's `handle_chunked` output as the behavioral oracle: feed identical chunked streams to both, assert identical yielded chunk sequences.

### 5.4 `eof` / response chunked writer (KNOWN, `:200-239`)

The response side mirrors the read side:
- `write_chunked` (`:200-211`): `b"%X\r\n%s\r\n" % (len(data), data)` — formats one chunk frame per write. `%X` is C-level hex format. For an `AsyncIterable` (streaming response — **the LLM token case**) it iterates and frames each chunk, then writes `handle_chunked_endsig = b"0\r\n\r\n"`.
- `write_event_stream` (`:213-222`): SSE framing `b"data: %b\n\n"` — prepends `data: ` and appends `\n\n` only if not already present (cheap prefix check `data[:6] != b"data: "`). dict/list auto-`dumps(indent=0)`.
- `eof` (`:230-239`): dispatches to `write_chunked_eof` if the writer is the chunked one, else optionally `write_raw`. Writer selection happens once in `prepare` (`:330-337`) — same "decide once, bind method" pattern as the body reader.

---

## 6. Connection lifecycle, keep-alive & the server bootstrap (KNOWN)

### 6.1 `connection_made` — pause + spawn task (KNOWN, `server_protocol.py:47-51`)

```python
def connection_made(app, transport):
    transport.pause_reading()                           # <-- DON'T read yet
    app.transport = transport
    app.cancel = (task := loop.create_task(app.transporter())).cancel
    task.__BlazeioProtocol__ = app                      # <-- back-reference for Context._r()
```

- **`pause_reading()` first thing** — the socket is created paused; the first byte is only read when the handler reaches `ensure_reading` (§1.6). No data is read into userland until there is a coroutine ready to consume it.
- One `loop.create_task(app.transporter())` per connection — the connection's driver coroutine. `app.cancel` is bound to `task.cancel` (used by `abort_connection`/`connection_lost` to kill the connection task on disconnect — prompt cancellation, no zombie tasks).
- **`task.__BlazeioProtocol__ = app`**: the task carries a back-pointer to its protocol. `Context._r()` (`Modules/streaming.py:7-9`) does `current_task().__BlazeioProtocol__` — this is how handlers get "the current request" **without passing it as an argument and without contextvars** (contextvars have non-trivial per-access cost; attribute on the task object is a single lookup). This is a notable micro-optimization: zero-arg `r = Context._r()` anywhere in the call stack, O(1).

### 6.2 `transporter` — the connection driver (KNOWN, `server_protocol.py:6-8`)

```python
async def transporter(app):
    await app.on_client_connected(app)
    app.close()
```

`on_client_connected` is `app.handle_client` (bound at `create_server` factory, `__init__.py:462`). It runs the full request lifecycle, then `app.close()` → `transport.close()`. Without keep-alive, **one request per connection** then close.

### 6.3 `Httpkeepalive.__main_handler__` — the keep-alive loop (KNOWN, `server_protocol.py:74-122`)

```python
async def __main_handler__(app, r):
    while not r.transport.is_closing():                 # <-- loop until socket dies
        exc = None
        try:
            r += app.keepalive_headers                  # append Connection/Keep-Alive headers
            await app.get_handler()(r)                  # run the route
        except Abort as e: await e.text(r)
        except (Eof, ServerGotInTrouble, BrokenPipeError): ...
        except (CancelledError, KeyboardInterrupt, CloseConnection, ClientDisconnected, ServerDisconnected): raise
        except Exception as e: exc = e
        finally:
            if r.__is_prepared__: await r.eof()
            else: await Abort("Server Got In Trouble", 500).text(r)
            if app.after_middleware: await app.after_middleware(r)
            if exc: await traceback_logger(exc, frame=4)
            ServerProtocolEssentials.reset(r)           # <-- reset state, REUSE the connection object
```

- The connection object (and its 100 KiB buffer, its `deque`, its `SharpEvent`s) is **reused across requests on the same connection**. `ServerProtocolEssentials.reset` (`server_protocol_extratools.py:33-58`) zeroes `method/path/headers/content_length/...`, `__stream__.clear()`, `__evt__.clear()`, `__overflow_evt__.clear()`, then `__initialize__()`. **No new protocol object, no new buffer allocation per request on a keepalive connection.** This is the biggest keepalive win — request N+1 on a connection costs zero connection-level allocation; only the per-request parser allocations (headers dict, etc.).
- `keepalive_headers` precomputed once (`server_protocol.py:88-91`): `b"Connection: Keep-Alive\r\nKeep-Alive: timeout=%d, max=%d\r\n" % (timeout, _max)` with `timeout = 60*60*5 = 18000` s (5 h) and `_max = 1000**2 = 1_000_000` requests. These are formatted **once** at `configure()`, then `r += app.keepalive_headers` (the `__iadd__` at `:177-181` extends a `bytearray`) prepends them to every response — no per-request string formatting of the keepalive header.
- The reset-and-loop is the textbook keep-alive; the perf point is the **object+buffer reuse** and the **precomputed header bytes**.

### 6.4 Server bootstrap, loop & socket options (KNOWN)

**Event loop / uvloop (`Dependencies/__init__.py:480-498`):**
```python
optional_module_importer({
  ("from uvloop import EventLoopPolicy as uvloop_EventLoopPolicy\nset_event_loop_policy(uvloop_EventLoopPolicy())", "..."),
  ("from aiofile import async_open", "async_open = NotImplemented")
})
```
`uvloop` is installed as the event-loop policy **if importable**, silently falling back to stdlib asyncio if not (the `"..."` else-branch = no-op). `requirements.txt` lists `uvloop` (`Create/__main__.py:21`). So the default deployment is **uvloop** (libuv — C event loop, ~2-4× asyncio's selector loop for socket I/O). This matters: Blazeio's `BufferedProtocol` get_buffer/buffer_updated path is fully supported by uvloop and is where uvloop's zero-copy `recv_into` shines.

**Listening socket (`__init__.py:437-442`):**
```python
def sock(app, *args, **kwargs):
    if not (sock := app.ServerConfig.sock):
        app.ServerConfig.sock = (sock := socket(AF_INET, SOCK_STREAM, *args, **kwargs))
        sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
    return sock
```
- `SO_REUSEADDR` on the listen socket (fast rebind after restart). **No `SO_REUSEPORT`** found anywhere — Blazeio does **not** do kernel-level load balancing across multiple listener processes by default. (KNOWN gap vs. a multi-process design; INFERRED single-process-per-port model.) **DESIGN-NOTE:** Lancelot should add `SO_REUSEPORT` for multi-worker scale-out (this is how uvicorn `--workers` and Robyn scale across cores; Blazeio leaves this on the table).

**Per-connection / listen-socket TCP tuning (`Utils/socket_ops.py:17-33`, `multiplexer.py:215-216`, `__init__.py:473-479`):**
```python
# TCPOptimizer (socket_ops.py) — applied to srv.sock()
if TCP_QUICKACK:  socket.setsockopt(IPPROTO_TCP, TCP_QUICKACK, 1)        # disable delayed ACK
if TCP_FASTOPEN:  socket.setsockopt(IPPROTO_TCP, TCP_FASTOPEN, 5)        # TFO, queue 5
socket.setsockopt(IPPROTO_TCP, TCP_NODELAY, 1)                            # Nagle OFF
socket.setsockopt(SOL_SOCKET, SO_KEEPALIVE, 1)
socket.setsockopt(IPPROTO_TCP, TCP_KEEPIDLE, 30)                          # idle 30s
socket.setsockopt(IPPROTO_TCP, TCP_KEEPINTVL, 10)                         # probe every 10s
socket.setsockopt(IPPROTO_TCP, TCP_KEEPCNT, 3)                            # 3 failed probes -> dead
# multiplexer.disable_nagle() — per accepted socket
socket.setsockopt(IPPROTO_TCP, TCP_NODELAY, 1)
# Server.with_keepalive() — listen socket
SO_KEEPALIVE=1, TCP_KEEPIDLE=60, TCP_KEEPINTVL=60, TCP_KEEPCNT=10
```
The optimization cluster:
- **`TCP_NODELAY=1` (Nagle off)** — set on both the listen socket (inherited by accepts) and explicitly per accepted socket in the multiplexer. **Critical for LLM token streaming**: Nagle would coalesce/delay small writes up to 40 ms waiting for an ACK; off means each token chunk hits the wire immediately. This is the single most important socket option for AI streaming latency.
- **`TCP_QUICKACK=1`** — disables delayed ACK on the receive side: ACKs are sent immediately rather than piggybacked/delayed up to 40 ms. Lowers round-trip latency for request/response. (Note: Linux resets QUICKACK after each use; setting it once on the listen socket is a best-effort, not sticky — KNOWN limitation.)
- **`TCP_FASTOPEN=5`** — enables TFO with a SYN-queue of 5: a returning client can send request data in the SYN packet, saving one RTT on connection setup. Helps cold connections.
- **Keepalive probes** (`TCP_KEEPIDLE/INTVL/CNT`): the `TCPOptimizer` defaults (30/10/3 = dead after 30+30s) detect and reap silently-dead clients (e.g. a client that vanished mid-LLM-stream) so the server doesn't hold the connection + its 100 KiB buffer forever. `with_keepalive` uses looser 60/60/10.
- All are guarded by `try: from socket import TCP_QUICKACK / TCP_FASTOPEN except ImportError: = None` and `if X:` — **portable**: Linux gets the full set, macOS/Windows skip the unsupported ones gracefully.

**`create_server` factory (`__init__.py:460-464`):**
```python
app.ServerConfig.set_server_context(lambda protocol, *args, **kwargs:
    app.event_loop.create_server(
        lambda: protocol(app.handle_client, app.event_loop, app.INBOUND_CHUNK_SIZE),
        *args, **kwargs))
```
Standard `loop.create_server` with a protocol factory that builds a fresh `BlazeioServerProtocol(handle_client, loop, 102400)` per accepted connection. No `reuse_port` passed (confirming the no-`SO_REUSEPORT` finding). The `INBOUND_CHUNK_SIZE=102400` is threaded here into every connection's preallocated buffer.

---

## 7. The multiplexer — single-connection stream multiplexing (KNOWN, `Protocols/multiplexer.py`)

The multiplexer is **not** part of the plain HTTP request path. It is a custom binary framing layer that runs **many logical streams over one TCP connection** (Blazeio's own "HTTP/2-ish" / RPC transport, used by `BlazeioClientProtocol`/`BlazeioServerProtocol` mux variants). Relevance to Lancelot: it shows how Blazeio does connection reuse + per-stream backpressure in pure Python, and its limits.

### 7.1 The wire framing (KNOWN, `multiplexer.py:78-86,165-166`)

```python
_data_bounds_ = (b"\x00",  # sof  (start-of-field)
                 b"\x01",  # eof  (end-of-field)
                 b"\x02",  # io_eof
                 b"\x03",  # io_close
                 b"\x04",  # io_heartbeat
                 b"\x05",  # io_permission (ack)
                 b"\x06")  # io_create (handshake)
def metadata(app, _id, sid, _len, __stream_opts=None):
    return b"%b%b%b%b%b%b%b%b%b%b%X%b" % (
        SOF, _id, EOF,  SOF, __stream_opts or b"", EOF,
        SOF, sid or b"", EOF,  SOF, _len, EOF)            # _len is %X (hex)
```
A frame = four `\x00…\x01`-delimited fields: stream-id, stream-opts, sub-id (sid), payload-length (hex), followed by the raw payload. Parsed by `_parse_bounds` (two `bytes.find` for `\x00` and `\x01`) and `_set_stream`/`_chunk_received`. Same `find`+slice philosophy as the HTTP parser; same copy cost (`bytes(memoryview(app.__buff)[a:b])` per field, `app.__buff[b+1:]` recopy per field — the **same O(fields²) tail-recopy quadratic** as the header parser, §4.4).

### 7.2 Per-stream flow control & ack window (KNOWN, `multiplexer.py:515-557`)

```python
# Stream.__writer__
async for chunk in app.__to_chunks__(memoryview(data)):     # split into chunk_size pieces
    async with app.protocol.__busy_write__:                 # serialize writes on the shared conn
        if wait: await app.protocol.protocol.buffer_overflow_manager()   # reuse the conn backpressure
        sid = app.write_mux(chunk, __stream_opts, gen_sid=wait)
    if wait:
        app.inflight_waits.append(app.wfa(sid))
        if len(app.inflight_waits) >= app.inflight_window or app.__initial_handshake:
            await app.wfa_all(app.inflight_waits)           # wait for ACKs
```
- `__to_chunks__` (`:493-498`) splits a `memoryview(data)` into `chunk_size`-sized views (`_chunk_size_base_ = 1024*10 = 10240`). Splitting a `memoryview` is zero-copy (view slices) — only the final `transport.write` materializes. Good pattern.
- **Ack window:** `inflight_window = 1` (`:365`) — the writer sends one chunk, then **waits for an ACK** (`wfa`) before sending the next (window of 1). This is application-level stop-and-wait flow control *on top of* TCP. It's correct but **latency-bound**: throughput ≈ chunk_size / RTT for window=1. (KNOWN: `inflight_window` is a field, settable higher, default 1 — conservative.) **DESIGN-NOTE:** a window of 1 is a throughput killer over high-RTT links; Lancelot's transport (if it has one) should use a larger sliding window or rely on TCP's own window and not re-implement stop-and-wait in Python.
- `__busy_write__` (an `ioCondition`) serializes all writes across all streams on the shared transport — necessary (frames must not interleave) but a contention point: all streams' writes are serialized through one async lock in Python.
- Backpressure **reuses the connection-level `buffer_overflow_manager`** (§3.2) — the per-connection write watermark gates all streams. Plus `set_write_buffer_limits(10 MiB, 0)` is applied here (§3.3).

### 7.3 Multiplexer relevance verdict (DESIGN-NOTE)

The multiplexer is over-engineered for Lancelot's stated goal (faster FastAPI). HTTP/1.1 keep-alive (§6.3) + HTTP/2 (if needed, via the Rust core) cover connection reuse far better than a hand-rolled `\x00\x01`-framed stop-and-wait protocol in Python. **Do not port the multiplexer.** Extract only: (a) the `memoryview` chunk-splitting in `__to_chunks__`, (b) the lesson that pure-Python application-level windowed flow control with window=1 is a throughput trap, (c) the `__busy_write__` write-serialization requirement (relevant if Lancelot ever multiplexes — but Rust should own that).

---

## 8. AI angle — slow clients, long LLM streaming responses, backpressure under token streaming

This is where a pure-Python event loop either shines or breaks. Blazeio's design is, for the most part, **well-suited to LLM token streaming** — here is the precise analysis.

### 8.1 The LLM streaming response path (KNOWN, reconstructed)

A typical AI handler: `async with Enterchunked(): async for token in llm_stream: await r.write(token_bytes)` (or SSE via `write_event_stream`). The write path per token:
1. `r.write` is bound (in `prepare`, `:330-337`) to `write_chunked` or `write_event_stream` — **decided once**, not per token.
2. `write_chunked(token)` → `b"%X\r\n%s\r\n" % (len(token), token)` (one C-level format) → `app.writer(framed)`.
3. `writer` → `await app.buffer_overflow_manager()` → fast path: `if not __is_buffer_over_high_watermark__: return` (one attr read) → `transport.write(framed)`.
4. `TCP_NODELAY=1` → the framed token leaves the NIC immediately (no Nagle 40 ms coalescing).

**Why this is good for AI streaming:**
- **Per-token overhead is tiny in the common (fast client) case:** one `%X` format + one attr-read early-return + one `transport.write`. No `drain()` coroutine, no future, no event-loop round-trip per token when the client keeps up. LLM tokens are small (1-4 bytes typ. after BPE→UTF-8) and frequent (20-100/s/stream); minimizing per-token overhead is exactly right.
- **Nagle off is the killer feature here:** without `TCP_NODELAY` each tiny token write would be delayed up to 40 ms waiting to coalesce → catastrophic perceived latency ("the model is thinking" stutter). Blazeio gets this right by default on every accepted socket.
- **Backpressure protects the server from slow consumers:** if the client (browser, proxy) reads slower than the model generates, the transport write buffer fills → `pause_writing` → `__is_buffer_over_high_watermark__=True` → the next `await r.write(token)` parks the *handler coroutine* (which transitively stops pulling from the LLM, or buffers in the LLM client's own queue). The server does **not** accumulate an unbounded queue of generated tokens in the asyncio write buffer. With `set_write_buffer_limits(10 MiB, 0)` (mux path) the producer only parks after 10 MiB queued and only resumes at full drain — minimal pause/resume thrash during a long stream.

### 8.2 Where pure Python *breaks* under AI workloads (the honest limits)

1. **Many concurrent streams = many `SharpEvent`/`Future` wakeups.** Each streaming connection's read-pump and write-drain park on `SharpEvent`s. With 5k concurrent SSE/LLM streams, every token-write that hits backpressure, and every keepalive read, creates/resolves a `Future`. The GIL serializes all of this. uvloop helps the I/O but the Python-level future churn + GIL is the ceiling. A Rust core handles 100k streams with `tokio` tasks and no GIL.
2. **The 100 KiB preallocated buffer per connection** (`__buff__ = bytearray(102400)`): 5k idle-but-open streaming connections = ~500 MiB resident just for receive buffers, even though a streaming *response* connection receives almost nothing after the request. This is pure waste for the LLM-response case (server mostly writes, rarely reads on that connection). **DESIGN-NOTE:** Lancelot should shrink/pool the receive buffer for connections that have transitioned to response-streaming mode (the request body is done; no need for a 100 KiB receive window).
3. **Slow-loris on the request side (the header re-scan quadratic, §4.2):** an attacker dribbling request headers one byte per second forces `while b"\r\n\r\n" in buff` to re-scan on every byte. The 102400 cap bounds memory but the connection (and its 100 KiB buffer + a pump coroutine + a `Future`) is held hostage for up to the keepalive timeout. With thousands of such connections the Python event loop's per-connection overhead (task + futures + buffer) is the resource that exhausts first. **There is no per-connection request-header timeout in the parser** (only TCP keepalive at 30-60 s and the byte cap). This is a real DoS surface and a strong Rust-core argument: a Rust parser + a tokio timeout handles slow-loris at near-zero per-connection cost.
4. **GIL during token post-processing:** if the handler does any CPU work per token (e.g. JSON-encoding a structured chunk, `dumps(data, indent=0)` in `write_event_stream` at `:217`), that runs under the GIL and stalls *all* other connections' event loop progress. Blazeio offloads compression to threads (`br` via `to_thread`, `:343-344`) but not JSON encoding. A Rust core (or a Rust JSON encoder) removes this.

### 8.3 Net AI verdict (DESIGN-NOTE for Lancelot)

For **moderate concurrency** (hundreds of simultaneous LLM streams), Blazeio's pure-Python design is genuinely competitive: per-token overhead is minimal, Nagle-off + cooperative backpressure are exactly the right primitives, and uvloop carries the I/O. **Copy this design for Lancelot's Python streaming fast-path verbatim** (writer-selection-once, `buffer_overflow_manager` fused into `write`, `TCP_NODELAY` mandatory, 10 MiB/0 write limits for streaming).

For **high concurrency** (thousands+ of streams) or **adversarial clients** (slow-loris), pure Python hits three walls — GIL-serialized future churn, fixed 100 KiB/conn memory, and the header-scan quadratic with no header timeout. These are exactly the workloads Lancelot's **Rust core** must own: the Rust core accepts connections, parses requests (single-pass, with a header-receipt deadline), manages per-connection buffers (refcounted, pooled, zero-copy), and only hands a *fully-parsed request* + a backpressure-aware write sink to Python — Robyn's model, but with Blazeio's streaming-write ergonomics preserved at the Python boundary.

---

## 9. Exact constants table (KNOWN — every numeric constant in scope, with file:line)

| Constant | Value | file:line | Meaning / effect |
|---|---|---|---|
| `INBOUND_CHUNK_SIZE` | `102400` (100 KiB) | `Dependencies/__init__.py:132` | Per-connection preallocated receive `bytearray` size (`server_protocol.py:22`). |
| `OUTBOUND_CHUNK_SIZE` | `102400` (100 KiB) | `Dependencies/__init__.py:132` | Client-side default send/recv chunk (`client_protocol.py:27`). |
| `__http_request_max_buff_size__` | `102400` (100 KiB) | `Dependencies/__init__.py:74` | Max header bytes before `413 Abort` (`__init__.py:145`, `request.py:260`). |
| `__http_request_heading_end_seperator__` | `b"\r\n\r\n"` | `Dependencies/__init__.py:72` | Header/body terminator. |
| `__http_request_heading_end_seperator_len__` | `4` | `Dependencies/__init__.py:73` | Precomputed terminator length (used in body slice). |
| `__http_request_buff_size` | `4096*2` = `8192` | `Dependencies/__init__.py:75` | Declared default request buff size (largely unused in hot path). |
| `__http_request_initial_separatir__` | `b' '` | `Dependencies/__init__.py:76` | Request-line token separator (space). |
| `MinParserClient.max_buff_size` | `102400` | `min_http_parser.py:101` | Client response header cap before `ClientGotInTrouble`. |
| Status-line resync window | `len(b"HTTP/1.1")` = `8` | `min_http_parser.py:164` | Bytes retained while hunting for response status line. |
| `redirection` status set | `(300,301,302,303,307,308)` | `min_http_parser.py:33` | Recognized redirect codes. |
| Keep-Alive `timeout` | `60*60*5` = `18000` s (5 h) | `server_protocol.py:76` | `Keep-Alive: timeout=` header value (advertised). |
| Keep-Alive `_max` | `1000**2` = `1_000_000` | `server_protocol.py:76` | `Keep-Alive: max=` header value (max reqs/conn advertised). |
| `_write_buffer_limits` (mux) | `((1024**2)*10, 0)` = (10 MiB, 0) | `multiplexer.py:7,90` | `set_write_buffer_limits(high=10MiB, low=0)` — wide hysteresis. |
| `BlazeioMultiplexer._min_buff_size_` | `1024*512` = `524288` (512 KiB) | `multiplexer.py:88` | Mux forces protocol `__buff__` ≥ 512 KiB (`:206-209`). |
| `Stream._chunk_size_base_` | `1024*10` = `10240` (10 KiB) | `multiplexer.py:353` | Mux per-write chunk split size (`__to_chunks__`). |
| `Stream.inflight_window` | `1` | `multiplexer.py:365` | Application-level ACK window (stop-and-wait). |
| `mux` retry loop | `range(10)` | `multiplexer.py:69` | Mux pull-loop restart attempts on error. |
| `Batchedwriter.min_chunk_size` | `1024*10` = `10240` | `Server/writers.py:9` | Write-coalescing flush threshold. |
| `Batchedwriter.rem_chunk_size` | `min_chunk_size*3` = `30720` | `Server/writers.py:10` | Buffer-grow trigger for coalesced writes. |
| `br_decoder` thread offload threshold | `102400` (100 KiB) | `Modules/request.py:13` | Brotli decode runs in a thread above this size, inline below. |
| `TCP_NODELAY` | `1` (on) | `socket_ops.py:29`, `multiplexer.py:216`, `Dependencies` | Nagle disabled — critical for token streaming. |
| `TCP_QUICKACK` | `1` (on, if available) | `socket_ops.py:23-24` | Delayed-ACK disabled. |
| `TCP_FASTOPEN` | `5` (queue depth) | `socket_ops.py:26-27` | TFO enabled, 5-deep SYN queue. |
| `SO_KEEPALIVE` | `1` | `socket_ops.py:30`, `__init__.py:476` | TCP keepalive enabled. |
| `TCP_KEEPIDLE` (TCPOptimizer) | `30` s | `socket_ops.py:19,31` | Idle before first keepalive probe. |
| `TCP_KEEPINTVL` (TCPOptimizer) | `10` s | `socket_ops.py:19,32` | Interval between probes. |
| `TCP_KEEPCNT` (TCPOptimizer) | `3` | `socket_ops.py:19,33` | Failed probes before drop (~dead in ~30+30 s). |
| `TCP_KEEPIDLE` (with_keepalive) | `60` s | `__init__.py:477` | Looser keepalive on listen socket path. |
| `TCP_KEEPINTVL` (with_keepalive) | `60` s | `__init__.py:478` | — |
| `TCP_KEEPCNT` (with_keepalive) | `10` | `__init__.py:479` | — |
| `SO_REUSEADDR` | `1` | `__init__.py:440` | Fast rebind on restart. |
| `SO_REUSEPORT` | **absent** | — | **Not set** — no kernel multi-listener load balancing (Lancelot gap to fill). |
| `SharpEvent.auto_clear` default | `False` | `Dependencies/__init__.py:148` | Protocol creates without auto-clear; uses explicit `wait_clear`. |
| `ioCondition.initial` default | `True` | `alts.py:105` | First `wait()` returns free if event set. |
| `non_bodied_methods` | `("GET","HEAD","OPTIONS","DELETE")` | `server_protocol.py:18` | Bodies never read for these. |
| `no_response_body_methods` | `("HEAD",)` | `server_protocol.py:19` | `HEAD` → `prepare` raises `Eof` after headers (`:341`). |
| `handle_chunked_endsig` | `b"0\r\n\r\n"` | `server_protocol_extratools.py:161` | Terminating chunk written on response EOF. |
| Default `set_cookie` expiry | `"Tue, 07 Jan 2030 ..."` | `server_protocol_extratools.py:277` | Hardcoded far-future cookie expiry. |
| SSE frame | start `b"data: "`, end `b"\n\n"` | `server_protocol_extratools.py:213` | `write_event_stream` framing. |
| `gzip` wbits | `31` | `server_protocol_extratools.py:347` | gzip (not raw deflate) — `compressobj(wbits=31)`. |
| `gzip_decoder` wbits | `16 + zlib_MAX_WBITS` | `Modules/request.py:21` | gzip auto-detect decompress. |

---

## 10. The case FOR and AGAINST a Rust parser in Lancelot

### 10.1 The case FOR (what Rust must own)

1. **Single-pass resumable header parsing.** Blazeio's `while b"\r\n\r\n" in buff` re-scans the whole buffer per segment (§4.2) — O(S·n). A Rust state machine (or `httparse`) scans each byte once across all feeds — O(n). This is *the* decisive argument: the common case is equal, the fragmented/adversarial case is categorically better in Rust, and there is no pure-Python fix (Python's `find` has no resumable "remember where I stopped" mode without a Python-level byte loop, which is far slower than the C `find` re-scan).
2. **Zero-copy buffer ownership.** Blazeio must `bytes(memoryview[:nbytes])` every recv to detach the slice before reusing `__buff__` (§1.4). Rust hands out refcounted `Bytes` slices — no copy, ever. At 100k req/s this is 100k eliminated copies/s + 100k eliminated allocations/s.
3. **No per-header Python objects.** `header[:idx].decode().capitalize()` = 3 allocs + 2 UTF-8 decodes *per header* (§4.4). Rust parses to `(&[u8], &[u8])` pairs, decodes only headers the handler reads. For a 15-header request that's ~45 allocs Lancelot can erase.
4. **No header tail-recopy quadratic.** `header = header[idx+2:]` per header line is O(H²) (§4.4); chunked `memarray(buff[...])` per chunk is O(C·body) (§5.3). Index advancement in Rust is O(1) per step.
5. **Slow-loris resistance with a header deadline.** A Rust accept loop + tokio timeout drops a dribbling client at ~zero per-connection cost; Blazeio holds a task + 100 KiB + a future per slow-loris connection until TCP keepalive (30-60 s) (§8.2). The GIL makes thousands of these fatal in pure Python.
6. **No GIL on the parse/IO path.** Thousands of concurrent streams' parse + framing + backpressure bookkeeping all serialize through the GIL in Blazeio. Rust parses off-GIL; Python only runs the handler.

### 10.2 The case AGAINST (what Rust does NOT help, and the risks)

1. **The common case is already fast in pure Python.** A request that arrives in one segment with 10-15 headers costs: one `find(\r\n\r\n)`, two request-line `find`s, ~15 header `find`s, ~45 small allocs, ~30 short decodes — microseconds, dominated by uvloop's syscall, not the parser. For the 95% of real traffic that is not fragmented or adversarial, the Rust parser's win over Blazeio's `find`-based parser is small relative to total request cost. Robyn's own benchmarks show its Rust core's advantage is real but not order-of-magnitude over a well-tuned async Python server for simple JSON endpoints.
2. **The FFI boundary has a cost.** Every Python↔Rust crossing (PyO3) marshals arguments and re-acquires the GIL. If Lancelot hands every parsed request from Rust to Python, the per-request FFI cost partially offsets the parse savings. Net win requires the Rust side to do *enough* work per crossing (parse + route + body framing) to amortize the boundary — i.e. Rust must own a substantial slice, not just `bytes.find`.
3. **Backpressure & streaming-write ergonomics are already optimal in Blazeio's pure-Python design.** `buffer_overflow_manager` fused into `writer`, writer-selected-once, `TCP_NODELAY`, 10 MiB/0 limits — Rust does not meaningfully improve these; it would just reimplement them. Lancelot should *preserve Blazeio's Python streaming-write API shape* even with a Rust core (the Rust core provides a backpressure-aware sink; Python keeps `async for token: await r.write(token)`).
4. **Complexity & build/distribution cost.** A Rust core means cibuildwheel matrices, `maturin`, platform wheels, longer CI, harder contributor onboarding — exactly the operational burden Blazeio avoided by being pure Python (`pip install`, no compiler). For Lancelot this is acceptable (it's the explicit thesis: "faster FastAPI" via a Rust core) but the pure-Python `MinParser` must remain as a **no-compiler fallback** and a **behavioral oracle**.
5. **Correctness risk.** Blazeio's parser is *lenient* (no version check, no chunk-extensions, drops trailers). A new Rust parser must decide its strictness. Using Blazeio's `MinParser` as a differential-testing oracle (feed identical byte streams to both, assert identical `(method, path, headers, body)`) de-risks the Rust rewrite — Blazeio is valuable here precisely because its algorithm is simple enough to be a reference.

### 10.3 Recommended Lancelot architecture (DESIGN-NOTE, synthesized)

- **Rust core owns:** accept loop (`SO_REUSEPORT` multi-worker), per-connection buffer (pooled, refcounted `BytesMut`, zero-copy slices), single-pass resumable HTTP/1.1+chunked parser **with a header-receipt deadline** (slow-loris kill), and a backpressure-aware write sink. Hands Python a fully-parsed request + an `async` write sink.
- **Python fast-path keeps verbatim from Blazeio:** `BufferedProtocol`+reused-buffer (for the pure-Python fallback build), `SharpEvent` (drop-in over `asyncio.Event`), `Context._r()` via `task.__attr__` (no contextvars), writer-selected-once + `buffer_overflow_manager`-fused-into-write, the connection/buffer **reuse on keepalive** (`reset()` not realloc), `TCP_NODELAY`+`TCP_QUICKACK`+`TCP_FASTOPEN`+keepalive socket-option cluster, lazy query/header decode, bodiless-method body short-circuit.
- **Do not port:** the multiplexer (§7.3), the O(H²) header tail-recopy, the segment re-scan loop (Rust replaces these), `SharpEvent` for multi-waiter fan-out (§2.2).
- **Tunables Lancelot must expose that Blazeio hardcodes/omits:** receive-buffer size (adaptive, shrink after request-body done), `SO_REUSEPORT`, write-buffer-limits on the HTTP path (not just mux), a per-request header-receipt timeout, mux ack-window (or drop the mux entirely).

---

## 11. Evidence index (file:line → claim)

- `Protocols/__init__.py:6-18` — `buffer_updated`/`get_buffer` zero-copy receive (§1.3-1.4).
- `Protocols/__init__.py:20-34` — connection_lost/eof_received/pause_writing/resume_writing (§1.5).
- `Protocols/__init__.py:36-39` — `prepend` O(1) pushback (§1.7).
- `Protocols/__init__.py:41-67` — `buffer_overflow_manager`/`ensure_reading`/`__await__`/`__aiter__` (§1.6, §3.2).
- `server_protocol.py:16-72` — protocol class, `__slots__`, `__init__`, `connection_made`, `writer` (§1.1-1.2, §3.1, §6.1).
- `server_protocol.py:74-122` — `Httpkeepalive` loop + precomputed headers + object reuse (§6.3).
- `server_protocol_extratools.py:33-58` — `ServerProtocolEssentials.reset` (keepalive object reuse) (§6.3).
- `server_protocol_extratools.py:200-239` — chunked/SSE/raw response writers, `eof` (§5.4).
- `server_protocol_extratools.py:241-275` — `handle_chunked` decoder (§5.3).
- `server_protocol_extratools.py:294-305` — `handle_raw` length-delimited reader (§5.1).
- `server_protocol_extratools.py:101-119` — `read_exactly` preallocated clamped read (§5.2).
- `Parsers/min_http_parser.py:6-34` — config/delimiter table (§4.1).
- `Parsers/min_http_parser.py:40-97` — `header_make`/`header_parser`/`set_method`/`parse` (§4.2-4.4).
- `Parsers/min_http_parser.py:154-175` — client `aparse` sliding resync window (§4.5).
- `Modules/request.py:264-286` — alternate `prepare_http_request` server parser (§4.6).
- `Dependencies/__init__.py:65-132` — `__ioConf__` defaults, chunk-size constants (§9).
- `Dependencies/__init__.py:146-200` — `SharpEvent` (§2.1-2.2).
- `Dependencies/__init__.py:480-498` — uvloop optional import (§6.4).
- `alts.py:103-191` — `ioCondition` (§2.3).
- `Utils/socket_ops.py:17-33` — `TCPOptimizer` socket-option cluster (§6.4).
- `__init__.py:437-464` — `sock()` SO_REUSEADDR, `create_server` factory (§6.4).
- `__init__.py:473-479` — `with_keepalive` socket options (§6.4).
- `multiplexer.py:7,78-90,165-166,206-219,353-365,493-557` — mux framing/flow-control/constants (§7).
- `Modules/Server/writers.py:7-29` — `Batchedwriter` coalescing (§9 constants).
- `Modules/streaming.py:7-9` — `Context._r()` via `task.__BlazeioProtocol__` (§6.1).

*End of dossier.*
