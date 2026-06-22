# Blazeio core — byte-path mechanism teardown

Source cluster: Blazeio package at `/Users/seventyleven/Desktop/researchfms/blazeio/Blazeio/Blazeio/`

Files read in full:
- `Protocols/__init__.py` — BlazeioProtocol (asyncio.BufferedProtocol base)
- `Protocols/server_protocol.py` — BlazeioServerProtocol + Httpkeepalive
- `Protocols/server_protocol_extratools.py` — ExtraToolset, headers_to_http_bytes, prepare, write_chunked, Rutils
- `Parsers/min_http_parser.py` — MinParser, MinParserClient, HTTP constants
- `Utils/socket_ops.py` — TCPOptimizer
- `Dependencies/__init__.py` — ioConf, SharpEvent, Enqueue, optional_module_importer (uvloop policy), chunk sizes, c_extension_importer
- `Dependencies/alts.py` — ioCondition, Asynchronizer, TaskPool, plog
- `Itools/__init__.py` — memarray, Ringarray, viewarray, zcbuff, ddict, DictView
- `Modules/Server/writers.py` — Batchedwriter
- `Modules/streaming.py` — Context, Deliver, Abort, Enterchunked, StreamPeek
- `Modules/request.py` (grepped) — ContentDecoders/Encoders, HTTPParser, Request
- `Bench/test.py` — actual benchmark driver showing ZERO-COPY read path option

---

## M1. asyncio.BufferedProtocol + pre-allocated memoryview receive buffer

**Mechanism:** `BlazeioProtocol` (inherits `asyncio.BufferedProtocol`) + `BlazeioServerProtocol.__init__` allocates one `bytearray(INBOUND_CHUNK_SIZE)` and a persistent `memoryview` over it on connection creation.

```
Protocols/__init__.py:6-18  (get_buffer / buffer_updated)
Protocols/server_protocol.py:22-24  (__buff__ = bytearray(INBOUND_CHUNK_SIZE), __buff__memory__ = memoryview(__buff__))
Dependencies/__init__.py:132  (INBOUND_CHUNK_SIZE = 102400)
```

`get_buffer` hands the kernel a slice of the existing `memoryview` — no new allocation per read. On `buffer_updated(nbytes)`, it slices `__buff__memory__[:nbytes]` into `bytes()` and appends to a `deque` (`__stream__`). The `memoryview` slice is zero-copy at the C level; the only allocation is the `bytes()` conversion needed to put it on the deque for hand-off (unavoidable without zero-copy hand-off like io_uring).

**Why this matters for Lancelot byte path:** Lancelot's Rust server uses hyper/tokio which already does the OS-level buffer hand-off through Rust's memory safety model — no Python per-read allocation at all. The Blazeio trick is the best Python can do short of a Rust core. **HAVE** — Lancelot's hyper-based receive path is structurally superior (Rust, no Python GIL, no `bytes()` copy). Document as the differentiator.

**lancelot_check_hint:** `src/serve.rs` or equivalent accept loop — confirm no Python-layer receive buffer exists.

---

## M2. pause_reading / resume_reading backpressure — explicit per-chunk flow control

**Mechanism:** `connection_made` calls `transport.pause_reading()` immediately. `ensure_reading` calls `transport.resume_reading()` only when the deque is empty and the coroutine is about to `await`. After `buffer_updated`, reading is paused again. This gives explicit, per-chunk backpressure: kernel buffer → single-chunk deque → coroutine; no uncontrolled flooding.

```
Protocols/__init__.py:7  (transport.pause_reading() in buffer_updated)
Protocols/__init__.py:46-50  (ensure_reading: resume only when deque empty + evt wait)
Protocols/server_protocol.py:47-48  (connection_made: transport.pause_reading() immediately on connect)
```

The deque (`__stream__`) is bounded by this pause/resume discipline — at most one chunk outstanding at a time (plus prepended data). This is structurally equivalent to Lancelot's bounded-mpsc channel for the streaming pump.

**Why this matters for Lancelot byte path:** Lancelot's R3 streaming pump already implements bounded backpressure on the Rust side via `reserve().await` on the channel. The difference is that Blazeio applies this at the inbound read level too (raw bytes from the kernel), not just the outbound streaming path. Lancelot's inbound is handled by hyper/tokio which has its own flow control. **HAVE** — structurally covered in both directions.

**lancelot_check_hint:** Verify `src/serve.rs` hyper body consumption has backpressure at the inbound side when a large POST floods a slow handler.

---

## M3. SharpEvent — custom asyncio.Future-pooled event (eliminates per-wait allocation)

**Mechanism:** `SharpEvent` replaces `asyncio.Event` everywhere. It creates one `Future` and reuses it across `wait()` calls (stored in `_waiters` list). `set()` resolves the future and clears `_waiters`; `get_fut()` only allocates a new Future when `_waiters` is empty. `auto_clear=True` fires once and self-clears.

```
Dependencies/__init__.py:146-201  (SharpEvent.__init__, get_fut, wait, set)
```

Comparison: `asyncio.Event.wait()` allocates a new `Future` on every call via `loop.create_future()` in CPython's `asyncio/locks.py`. Blazeio's `SharpEvent` pools the future (one allocation per signal edge, not per waiter), then `_waiters.clear()` on `.set()`.

**Why this matters for Lancelot byte path:** Each `await r` (await the protocol object) calls `ensure_reading` which calls `SharpEvent.wait()`. Under high RPS (59k req/s) on small responses this fires ~59k times/second. Avoiding a `loop.create_future()` allocation each time measurably reduces GC pressure.

**Real win or micro-opt?** Genuine micro-win in Python for the hot path. Lancelot's Rust core doesn't have a Python-layer event per read — the read path is entirely Rust. However, Lancelot does use Python `asyncio.Event` in some places (e.g., `ShutdownEvent`, gate events). For any Python-side event on the hot path, adopting a pooled-future event would help.

**lancelot_check_hint:** Grep `lancelot/` for `asyncio.Event()` — any that fire on every request (not just shutdown) are candidates. `lancelot/batch.py` uses `asyncio.Event`.

---

## M4. uvloop policy set at module import time (not just soft-dep)

**Mechanism:** `Dependencies/__init__.py:495-498` — `optional_module_importer` is called at module import with `("from uvloop import EventLoopPolicy as uvloop_EventLoopPolicy\nset_event_loop_policy(uvloop_EventLoopPolicy())", "...")`. This calls `set_event_loop_policy` globally during `import Blazeio`. It is not opt-in; it is the default on any install that has uvloop.

```
Dependencies/__init__.py:495-498  (optional_module_importer call)
Dependencies/__init__.py:480-493  (optional_module_importer: try/except ImportError, exec in globals())
```

The `DefaultEventLoopPolicy` import from asyncio is also present as fallback.

**Why this matters for Lancelot byte path:** Lancelot M10 landed uvloop on the embedded loop (`LANCELOT_LOOP` knob; falls back to stdlib asyncio; `make_event_loop` in `src/lib.rs` prefers uvloop). **HAVE.** The implementation approach is identical (try-import, set policy). The main difference: Blazeio sets it at Python-import time (process-global); Lancelot sets it at server startup (`run()`). No byte-path delta; both achieve the same result.

**lancelot_check_hint:** `src/lib.rs` `make_event_loop` — confirm uvloop policy is set before `loop.run_until_complete`.

---

## M5. SO_REUSEPORT + TCPOptimizer: TCP_NODELAY + TCP_QUICKACK + TCP_FASTOPEN in one call

**Mechanism:** `Utils/socket_ops.py:17-33` — `TCPOptimizer.__init__` applies, in order:
1. `TCP_QUICKACK = 1` (Linux-only, suppresses delayed ACK; import-guarded)
2. `TCP_FASTOPEN = 5` (Linux-only, pending-queue depth 5; import-guarded)
3. `TCP_NODELAY = 1` (Nagle off)
4. `SO_KEEPALIVE = 1`
5. `TCP_KEEPIDLE = 30`, `TCP_KEEPINTVL = 10`, `TCP_KEEPCNT = 3`

```
Utils/socket_ops.py:7-33
```

`SO_REUSEPORT` is applied in the benchmark (`Bench/test.py:62-63`), not in `TCPOptimizer` — it is opt-in at the start script level. `with_keepalive()` in `__init__.py:473-479` also sets `TCP_KEEPIDLE=60, TCP_KEEPINTVL=60, TCP_KEEPCNT=10` (different defaults from TCPOptimizer).

**Why this matters for Lancelot byte path:** Lancelot's Rust `src/socket.rs` already applies `TCP_QUICKACK` and `TCP_FASTOPEN` via `libc::setsockopt`. **HAVE.** The values (QUICKACK=1, FASTOPEN queue depth) should match or exceed Blazeio's. The keepalive defaults (KEEPIDLE/INTVL/CNT) differ — worth cross-checking Lancelot's chosen values.

**lancelot_check_hint:** `src/socket.rs` — verify `TCP_KEEPIDLE`, `TCP_KEEPINTVL`, `TCP_KEEPCNT` are set (Blazeio sets 30/10/3; Lancelot may not set keepalive at all on the server socket).

---

## M6. Zero-copy benchmark path: direct `writer()` bypass of parse+prepare

**Mechanism:** `Bench/test.py:35-55` demonstrates a key performance trick in the benchmark path: the `--enable_parsing=0` branch does NOT parse headers; it waits only until `b"\r\n"` is in the raw `r.__buff__` (the pre-allocated memoryview), then drops chunks. The response is a single `r.writer()` call with a pre-built `bytes` literal containing the entire HTTP response (status + all headers + `Content-length` + body concatenated).

This is the "prebaked bytes" trick at the application level: **the user constructs the full response as a `bytes` literal at definition time** and calls `r.writer()` once with no header-building overhead at runtime.

More importantly, `writer()` in `BlazeioServerProtocol.writer` is:
```python
async def writer(app, data):
    if not app.__is_prepared__:
        app.__is_prepared__ = True
    await app.buffer_overflow_manager()
    if not app.transport.is_closing():
        app.transport.write(data)
    else:
        app.abort_connection()
```

`transport.write(data)` is asyncio's non-blocking write (buffers in the transport's send buffer; `pause_writing`/`resume_writing` manage backpressure). No Rust FFI, no GIL release needed — it is a synchronous C-level call from Python to the asyncio transport.

**Why this matters for Lancelot byte path:** Lancelot's const tier (`const_routes` on `_native.run`) serves prebaked `Bytes` from Rust with zero GIL/Request alloc/handler call (M4, `service::Prebaked`). **HAVE — and Lancelot's version is decisively better**: serving 183k req/s vs Blazeio's measured ~59k because the Rust path avoids all Python overhead, not just header building. The Blazeio approach is the best Python can do; Lancelot's const tier supersedes it entirely.

**lancelot_check_hint:** `src/service.rs` `Prebaked` — confirm it bypasses the Python call entirely (no `handle_client` / no GIL acquire).

---

## M7. Batchedwriter — write coalescing to avoid small-chunk syscalls

**Mechanism:** `Modules/Server/writers.py:7-28` — `Batchedwriter` is an async context manager that intercepts `r.write`. It accumulates chunks in a `bytearray` buffer (`buff`). Only when `len(buff) >= rem_chunk_size` (= `min_chunk_size * 3` = 30 KB by default) does it flush a `min_chunk_size`-sized slice to the real `parent_write`. On `__aexit__`, any remainder is flushed.

```
Modules/Server/writers.py:8  (min_chunk_size = 1024*10 = 10 KB)
Modules/Server/writers.py:10  (rem_chunk_size = min_chunk_size*3 = 30 KB)
Modules/Server/writers.py:24-28  (accumulate; flush when >= 30 KB)
```

The coalescing logic uses `memoryview` slicing (`view[:min_chunk_size]`, `view[min_chunk_size:]`) for the flush to avoid a full copy. The flush sends exactly `min_chunk_size` bytes (10 KB), keeping `buff` trimmed to the overflow.

**Why this matters for Lancelot byte path:** Reduces `transport.write()` calls (and thus syscalls) for chunked streaming responses. Small-chunk streams (e.g., SSE token-by-token at ~30 bytes/chunk) would otherwise issue one syscall per chunk. At 200 tok/s that's 200 syscalls/s/conn — coalescing to 10 KB reduces that ~333×.

**Is this a genuine Lancelot gap?** Possibly. Lancelot's R3 pump writes each chunk as it arrives from the bounded-mpsc channel. For LLM token streaming (small chunks, upstream-rate-limited), the bottleneck is the LLM not the write path, so this is low priority. But for large-file or high-throughput chunked responses it matters.

**lancelot_check_hint:** `src/stream.rs` — check if there is any write coalescing before `transport.write`. If each chunk is a separate hyper body poll, there may be no coalescing. For SSE/streaming, check whether Nagle + TCP_NODELAY effectively coalesces at the kernel level (they don't when TCP_NODELAY=1).

---

## M8. Write-path backpressure: pause_writing / resume_writing → __overflow_evt__

**Mechanism:** `BlazeioProtocol.pause_writing` sets `__is_buffer_over_high_watermark__ = True`; `resume_writing` clears it and fires `__overflow_evt__.set()`. Every `writer()` call starts with `await app.buffer_overflow_manager()` which awaits `__overflow_evt__.wait_clear()` when the flag is set.

```
Protocols/__init__.py:29-34  (pause_writing, resume_writing)
Protocols/__init__.py:41-43  (buffer_overflow_manager: wait only when flagged)
Protocols/server_protocol.py:64-72  (writer: buffer_overflow_manager before transport.write)
```

This is the asyncio transport high-water-mark mechanism: when the asyncio write buffer exceeds `high_water`, the transport calls `pause_writing` on the protocol, preventing further writes until the buffer drains. Without this, a slow client + fast server causes unbounded memory growth.

**Why this matters for Lancelot byte path:** Lancelot's R3 pump uses a bounded mpsc channel (`LANCELOT_STREAM_BUFFER` default 256 chunks) as backpressure. But this is inbound-to-channel backpressure; the Blazeio pattern is channel-to-socket backpressure. Lancelot should also respect asyncio's `pause_writing` / `resume_writing` on the write path to prevent unbounded send buffer growth under slow clients.

**Real or already-have?** Hyper/tokio manages the TCP send buffer internally and applies flow control at the Rust level. Python `transport.write` on the asyncio side does go through the write buffer. **Lancelot check needed**: does the Rust→Python bridge write path respect the asyncio transport high-watermark?

**lancelot_check_hint:** `src/stream.rs` and `src/serve.rs` — verify that the hyper response body is not written unboundedly if the TCP send buffer fills (tokio select! should apply backpressure).

---

## M9. Header serialization: direct bytes concatenation (no framework overhead)

**Mechanism:** `ExtraToolset.headers_to_http_bytes` (`server_protocol_extratools.py:165-175`) builds the response header bytes by iterating `headers` dict and concatenating `b"%s: %s\r\n" % (key.encode(), val.encode())` per header. This uses Python's bytes `%` operator (which internally allocates one `bytes` object per header). `prepare()` sends the status line first (`r.writer(status_line_bytes)`), then sends the header block as a second `r.writer()` call.

```
server_protocol_extratools.py:165-175  (headers_to_http_bytes)
server_protocol_extratools.py:306-339  (prepare: status + pre-headers + header dict → write)
```

The `__prepared_headers__` (`bytearray`) accumulates extra headers (e.g. `Set-Cookie`, `Connection: Keep-Alive`) added via `r += data` (`__iadd__`). These are prepended before the dict-based headers in `prepare()`.

**Why this matters for Lancelot byte path:** Lancelot's Rust side builds response headers natively in Rust (hyper `HeaderMap`). **HAVE — and superior.** The Python header-dict approach is the best pure-Python can do; Lancelot's Rust-level header building has no per-header Python object allocation.

---

## M10. ujson as primary JSON serializer (runtime dep)

**Mechanism:** `Dependencies/__init__.py:47-51` — `try: from ujson import dumps as ujson_dumps, loads, JSONDecodeError` with a fallback to `json`. `dumps` is wrapped to expose `indent`, `reject_bytes`, `escape_forward_slashes` kwargs matching ujson's API.

```
Dependencies/__init__.py:47-51
```

**Why this matters for Lancelot byte path:** Blazeio makes `ujson` a de facto runtime dep (installed as a sibling dep). Lancelot uses pydantic-core `to_json` for serialization (**HAVE — and superior**: pydantic-core's Rust serializer at the large-payload crossover beats ujson). The cardinal rule bans runtime deps beyond pydantic. **Refuse** — ujson is slower than pydantic-core `to_json` for typed Pydantic models; adding it as a dep violates the cardinal rule.

---

## M11. brotlicffi compression: off-loop for large payloads, inline for small

**Mechanism:** `Modules/request.py:12-16` — `br_decoder` dispatches based on payload size: `>= 102400` bytes uses `await to_thread(brotlicffi_decompress, data)` (off-loop thread); `< 102400` runs inline (sync). `Modules/request.py:32-33` — `br_encoder` always uses `to_thread(brotlicffi_compress, bytes(data))`.

`ExtraToolset.br` (`server_protocol_extratools.py:343-344`): `return to_thread(brotlicffi_compress, bytes(data))` — always off-loop.

```
Modules/request.py:12-17  (br_decoder size-threshold)
Modules/request.py:32-33  (br_encoder always to_thread)
server_protocol_extratools.py:343-344  (ExtraToolset.br)
Dependencies/__init__.py:37  (from brotlicffi import Decompressor, Compressor, compress, decompress)
```

brotlicffi is a hard runtime dep for Blazeio. The `gzip` encoder (`server_protocol_extratools.py:346-350`) runs inline on the event loop using `compressobj(wbits=31)` — same GIL-holding pattern that Lancelot M18 identified as the concurrency bottleneck.

**Why this matters for Lancelot byte path:** M18 shipped `lancelot.compress.GZipMiddleware` — Rust `libdeflater` off-loop via `run_in_executor`. **HAVE** (for gzip). Blazeio's pattern (brotli off-loop, gzip inline) confirms the GIL issue for inline compression. The size threshold on brotli decode (`102400` = INBOUND_CHUNK_SIZE) is noteworthy: below that, sync is faster (avoids thread-hop overhead). Lancelot's M18 gzip uses `run_in_executor` always — could add a similar size threshold to avoid the thread-hop for small responses (< ~4 KB).

**lancelot_check_hint:** `lancelot/compress.py` — check if there's a `content-length` threshold below which gzip runs inline. If not, consider adding one (say, < 4 KB = skip executor).

---

## M12. Keep-alive: single-connection protocol-reset loop

**Mechanism:** `Httpkeepalive.__main_handler__` (`server_protocol.py:96-121`) runs a `while not r.transport.is_closing()` loop. After each response:
1. `r.eof()` — sends chunked terminator if chunked, else no-op.
2. `app.after_middleware(r)` — post-response middleware.
3. `ServerProtocolEssentials.reset(r)` — resets ALL connection state fields (method, path, headers, pull, write, status, etc.) plus `__stream__.clear()` + `__evt__.clear()`.

```
server_protocol.py:96-121  (Httpkeepalive.__main_handler__)
server_protocol_extratools.py:34-58  (ServerProtocolEssentials.reset)
```

The reset runs in-place without deallocating the protocol object — the same `BlazeioServerProtocol` instance and its pre-allocated `bytearray` buffer serve the next request over the same TCP connection.

**Why this matters for Lancelot byte path:** Lancelot's current HTTP/1.1 path likely creates a new Python request object per keepalive request (check `src/dispatch.rs`). If the Rust protocol object is reused per connection (hyper handles keepalive), but a new Python `Request` object is allocated per request, there's a per-keepalive allocation. Blazeio avoids this by resetting state on the same object.

**Real win?** At 59k req/s tiny responses, most requests are new connections (no keepalive). Under keepalive load (persistent connections, many sequential requests) this is a genuine allocation reduction.

**lancelot_check_hint:** `src/dispatch.rs` or `src/serve.rs` — check if a new Python `Request` (`lancelot.Request` or Rust-backed `PyRequest`) is allocated per keepalive request. If yes, the reset-in-place pattern is a candidate improvement.

---

## M13. Default TCP backlog = 5000

**Mechanism:** `__init__.py:551-553` (server_run): `if not kwargs.get("backlog"): kwargs["backlog"] = 5000`. This is the `asyncio.create_server(backlog=5000)` call.

```
__init__.py:551-553
```

Default Python asyncio `create_server` backlog is 100. Blazeio explicitly sets 5000 — critical for high-RPS servers where the SYN queue fills under load.

**Why this matters for Lancelot byte path:** Lancelot's `asyncio.create_server` call should use a matching or higher backlog. Under benchmark load with `oha -c 200`, a backlog=100 causes connection refusals at the kernel level.

**lancelot_check_hint:** `src/lib.rs` or wherever `asyncio.create_server` or the hyper `TcpListener` is set up — verify the listen backlog. The Rust `TcpListener::bind` uses `socket2` and `listen(backlog)` — check what value is passed. Should be `>=1024` for production (5000 is generous but acceptable).

---

## M14. ioCondition — custom asyncio Condition (avoids asyncio.Lock allocation overhead)

**Mechanism:** `Dependencies/alts.py:103-188` — `ioCondition` replaces `asyncio.Condition`. It uses `SharpEvent` instances for lock + wait signaling instead of `asyncio.Lock` + `asyncio.Event`. The `wait()` method releases the lock, increments `waiter_count`, awaits the `SharpEvent`, checks `notify_count`, and re-decrements — no `loop.create_future()` per waiter because `SharpEvent.wait()` reuses futures.

Used in `Enqueue.check_overflow` and `TaskPool.create_task`.

```
Dependencies/alts.py:103-188  (ioCondition)
Dependencies/__init__.py:290-353  (Enqueue using ioCondition)
Dependencies/alts.py:290-358  (TaskPool using ioCondition)
```

**Why this matters for Lancelot byte path:** `lancelot/batch.py` uses `asyncio.Event` and `asyncio.sleep` for the batch window timer. Under high batch throughput (thousands of coalesce cycles/second), per-event allocation matters. The `SharpEvent` pooling pattern is a direct optimization for `lancelot/batch.py`'s hot path.

**Real win or micro-opt?** Genuine micro-win for `@app.batch` under load (the validated 12× throughput scenario). Low implementation effort — port `SharpEvent` pattern.

**lancelot_check_hint:** `lancelot/batch.py` — `self._flush_event = asyncio.Event()` and any `asyncio.Event()` in the batch window loop. Replace with a pooled-future event.

---

## Summary table: HAVE vs GAP vs REFUSE

| # | Mechanism | Status | Priority |
|---|-----------|--------|----------|
| M1 | BufferedProtocol + memoryview receive buffer | HAVE (Rust, superior) | — |
| M2 | pause_reading backpressure per-chunk | HAVE (Rust mpsc, superior) | — |
| M3 | SharpEvent pooled-future event | GAP: batch.py hot path | Low |
| M4 | uvloop policy at import | HAVE (M10) | — |
| M5 | TCP_QUICKACK + TCP_FASTOPEN + TCP_NODELAY | HAVE (src/socket.rs) | — |
| M6 | Prebaked bytes const tier | HAVE (M4, 183k req/s) | — |
| M7 | Batchedwriter write coalescing | GAP: streaming small chunks | Low-medium |
| M8 | pause_writing / resume_writing backpressure | NEED CHECK: Rust path | Medium |
| M9 | Rust header serialization | HAVE (hyper HeaderMap) | — |
| M10 | ujson runtime dep | REFUSE (cardinal rule + pydantic-core wins) | — |
| M11 | brotli off-loop + size threshold | HAVE gzip off-loop (M18); size threshold MISSING | Low |
| M12 | Keep-alive in-place protocol reset | NEED CHECK: per-request alloc? | Low-medium |
| M13 | TCP backlog = 5000 | NEED CHECK | Medium |
| M14 | ioCondition pooled Condition | GAP: batch.py | Low |

---

## How Blazeio achieves ~59k req/s in pure Python

The tiny-response benchmark uses the full stack:
1. `uvloop` policy set at import — replaces stdlib asyncio event loop (~20-30% faster I/O callbacks).
2. `asyncio.BufferedProtocol` — eliminates per-read `bytearray()` allocation (kernel writes directly into pre-allocated buffer; one `bytes()` copy per chunk).
3. `pause_reading` immediately in `connection_made` — prevents TCP flooding the deque; kernel buffers until the coroutine is ready.
4. `SharpEvent` — reuses `asyncio.Future` objects across `await r` calls, reducing GC churn.
5. Single `transport.write()` call with pre-built bytes — `writer()` method directly calls `transport.write(data)`, which is a non-blocking `loop.call_soon(transport._add_to_write_buffer, ...)` in asyncio's `_SelectorDatagramTransport`.
6. `SO_KEEPALIVE` + server-side keepalive loop — avoids TCP handshake cost across requests (5000 backlog, `TCP_NODELAY`).
7. Minimal per-request state: `__slots__` on protocol, pre-allocated deque, `ddict` for headers.

The key constraint vs Lancelot: **everything is still in Python on the GIL**. The coroutine-per-connection model means 59k req/s is effectively the GIL throughput ceiling for one Python event loop when each request requires `await r` (one future per request) + `transport.write`. Lancelot's Rust path breaks this by doing hyper/tokio handling in Rust threads while Python only runs the handler — which is why the `const_routes` tier reaches 183k (Python handler is the bottleneck; with zero-Python const routes it disappears).
