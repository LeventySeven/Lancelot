# Blazeio RE-02 — Request / Response / Streaming / Static / Exception-Control-Flow

**Target:** `anonyxbiz/Blazeio` (local clone at `/Users/seventyleven/Desktop/researchfms/blazeio/Blazeio/Blazeio`)
**Purpose:** Source-level pattern extraction for **Lancelot** (the user's "faster-FastAPI", startups + AI). Lancelot's headline workload is **LLM token streaming**, so the streaming write/drain path is the load-bearing section here.
**Method:** Every file in scope read line-by-line + the protocol layer it depends on (`Protocols/__init__.py`, `Protocols/server_protocol.py`, `Protocols/server_protocol_extratools.py`, `Modules/Server/writers.py`, `Dependencies/__init__.py`, `Itools/__init__.py`, `Dependencies/alts.py`). Every claim is `path:line` cited and labelled **KNOWN** (read from source) / **INFERRED** (deduced from source behaviour) / **DESIGNED** (my recommendation for Lancelot).

---

## Executive summary (the 10-line verdict)

1. **Zero-copy receive.** Blazeio uses `asyncio.BufferedProtocol` with **one persistent `bytearray(102400)` + a cached `memoryview`** (`Protocols/__init__.py:6-18`, `server_protocol.py:22-24`). `get_buffer` hands the kernel the same memoryview every read; `buffer_updated` slices it once. No `Protocol.data_received(bytes)` allocation per packet. **Steal this for Lancelot.**
2. **Receive backpressure is explicit and manual.** Every `buffer_updated` calls `transport.pause_reading()` immediately (`Protocols/__init__.py:7`); reading only resumes when the handler has drained `__stream__` and `await`s the next chunk (`ensure_reading`, `:45-54`). The socket is paused by default — pull-driven, not push-driven. This is the single most important pattern for AI back-channel safety.
3. **Send backpressure is a 2-event flip-flop, not `drain()`.** `pause_writing`/`resume_writing` toggle `__is_buffer_over_high_watermark__` and set `__overflow_evt__`; every `writer()` first `await app.buffer_overflow_manager()` which blocks on `__overflow_evt__.wait_clear()` only when the transport is over the high-water mark (`server_protocol.py:64-72`, `Protocols/__init__.py:29-43`). Cheaper than asyncio's `StreamWriter.drain()` because there is no Future created on the fast path.
4. **`SharpEvent` is a pooled-future Event.** One reused `Future`, manual `set_result`, `auto_clear` semantics, no `asyncio.Event` lock churn (`Dependencies/__init__.py:146-200`). This is why per-token flush stays cheap.
5. **Response writer is selected once at `prepare()` time**, then `app.write` is rebound to one of `write_raw` / `write_chunked` / `write_event_stream` (`server_protocol_extratools.py:330-338`). No per-chunk branching — the handler just calls `r.write(token)`.
6. **Chunked framing is hand-rolled hex.** `b"%X\r\n%s\r\n" % (len(data), data)` (`:204,207`). No `Content-Length`, no buffering of the whole body — the response is never materialized. SSE writer is even cheaper (`b"data: %b\n\n"`, `:213-222`).
7. **Exception-as-response control flow.** `Abort`/`NotFoundErr` are `BlazeioException`s caught in the keep-alive loop (`server_protocol.py:96-121`); the handler raises `Abort("msg",404)` and the loop calls `e.text(r)` to emit the response. `HEAD` short-circuits by `raise Eof()` from inside `prepare()` (`:341`). **Verdict: steal the ergonomics, reject the mechanism — see §7.**
8. **Static fast-path is `Simpleserve`** (`server_tools.py:7-149`): real ETag/Last-Modified/Range/conditional-304, `aiofile` async chunked read, no app cache. **`static.py` + `IN_MEMORY_STATIC_CACHE.py` are dead code** — they `from .streaming import Stream` and `streaming.py` exports no `Stream` (verified: `grep` finds only `StreamJson`/`StreamPeek`). Cite the live one, ignore the twins.
9. **No `sendfile(2)`.** Blazeio never uses `transport.sendfile` / `os.sendfile`; static serving is userland `read(CHUNK)`→`transport.write()`. This is a *missed* zero-copy win Lancelot should add for static, but it is **irrelevant to LLM streaming** (tokens are generated, not file-backed).
10. **Templating is a streaming string-replace over `<!--app.x-->` markers** (`templatify.py`, `asynctemplate.py`) — async-generator pipeline, never builds the page in memory. Useful pattern, low priority for an AI framework.

**Bottom line for Lancelot:** adopt the BufferedProtocol zero-copy receive, the pause-by-default pull model, the pooled-future event, and the prepare-time writer selection. Use chunked/SSE hand-framing for token streaming. **Do not** adopt exception-driven response delivery as the *primary* return mechanism (keep it for `abort()`-style early exits only). Add real `sendfile` for static.

---

## 1. The Request object — lazy everything, pull-driven body

### 1.1 The protocol IS the request object (KNOWN)

There is **no separate `Request` class instance per call**. The `asyncio` protocol object `BlazeioServerProtocol` *is* what handlers receive as `r` (`server_protocol.py:16-72`). It carries both the socket plumbing (`transport`, `__buff__`, `__stream__`) and the parsed request surface (`method`, `path`, `tail`, `headers`, `content_length`). `Request` (`request.py:130-364`) is a **bag of `@classmethod`s** that operate *on* `r`, never instantiated.

`__slots__` (`server_protocol.py:17`) — every attribute is slotted, zero `__dict__`:
```
on_client_connected, __stream__, __is_buffer_over_high_watermark__, __is_at_eof__,
__is_alive__, transport, method, tail, path, headers, __is_prepared__, __status__,
content_length, current_length, __perf_counter__, ip_host, ip_port, identifier,
__prepared_headers__, __miscellaneous__, __timeout__, __buff__, __buff__memory__,
store, transfer_encoding, pull, write, encoder, encoder_obj, __evt__,
__overflow_evt__, cancel, cancel_on_disconnect
```
Note `pull` and `write` are **instance attributes that get rebound to bound methods at parse/prepare time** (`request.py:247-254`, `server_protocol_extratools.py:331-337`). This is the central trick: no dispatch branch in the hot loop, the attribute *is* the chosen implementation.

Initial state at connection (`server_protocol.py:20-45`): `method=None`, `path="handle_all_middleware"`, `tail="handle_all_middleware"`, `headers=None`, `content_length=None`, `pull=None`, `__status__=0`, `current_length=0`. `__buff__ = bytearray(INBOUND_CHUNK_SIZE)` and `__buff__memory__ = memoryview(app.__buff__)` are allocated **once per connection** and survive across keep-alive requests (reset, not realloc, `server_protocol_extratools.py:33-58`).

### 1.2 Header line parsing (KNOWN — `request.py:41-63, 225-256`)

`prepare_http_request` (`request.py:264-286`) is the entry parser:
- `__buff__ = memarray()` — a `bytearray` subclass whose slicing returns `bytes(memoryview(...))` (zero-copy slice view, `Itools/__init__.py:104-116`).
- `async for chunk in r:` — iterates the protocol's own `__aiter__` (the receive stream, §3).
- Accumulate into `__buff__` until `__buff__.find(b"\r\n\r\n")` (`__http_request_heading_end_seperator__`, `Dependencies/__init__.py:72-73`) ≠ -1.
- Split heading off, the **remainder is pushed back via `r.prepend(__buff__)`** (`:277`) so the body parser sees it — this is the key "no data lost between header parse and body read" mechanism.
- Limit: `__http_request_max_buff_size__ = 102400` (`Dependencies/__init__.py:74`); exceeding it without `Content-Length`/`Transfer-Encoding` → `raise Abort("You have sent too much data...", 413)` (`request.py:259-262`).
- If `not r.method` after the loop → `raise Eof("Request has no headers")` (`:283-284`).

`set_method` (`request.py:225-256`): splits the request line on `b' '` (`__http_request_initial_separatir__`, `Dependencies/__init__.py:76`): `method`, then `tail` (raw target), then `r.path = tail[:tail.find('?')]` (query stripped lazily, **query string is NOT parsed here**). Headers parsed by `HTTPParser.header_parser` (`request.py:53-63`): repeatedly `data.find(b"\r\n")`, each line split on the first `b": "` (`:42, 47`), key `.capitalize()`d when `normalize_headers` (default True). Header values are `.decode("utf-8")` eagerly; keys capitalized → `r.headers["Content-length"]` not `Content-Length`.

### 1.3 Body reader selection — the `r.pull` rebind (KNOWN — `request.py:243-256`)

This is the lazy-body core. After headers are parsed, *once*:
```python
if r.headers and not r.pull:
    if (content_length := r.headers.get("Content-length")):
        r.content_length = int(content_length); r.pull = r.handle_raw
    elif (transfer_encoding := r.headers.get("Transfer-encoding")):
        r.transfer_encoding = transfer_encoding; r.pull = r.handle_chunked
    else:
        r.content_length = 0
        if not hasattr(r,"pull"): r.pull = r.handle_raw
```
`r.pull` becomes a **bound async-generator method chosen by the headers**. The handler never inspects `Content-Length` vs `Transfer-Encoding`; it just `async for chunk in r.pull():`. Body is **never read until the handler iterates** — fully lazy, fully streaming.

- `handle_raw` (`server_protocol_extratools.py:294-304`): if `method in non_bodied_methods` (`GET/HEAD/OPTIONS/DELETE`, `server_protocol.py:18`) or already consumed, return immediately (empty body). Else iterate `async for chunk in app:` (the raw receive stream), `app.current_length += len(chunk)`, `yield chunk`, stop when `current_length >= content_length`. **No size cap on the body itself** beyond what the handler chooses to read.
- `handle_chunked` (`server_protocol_extratools.py:241-275`): a hand-written chunked-transfer decoder. State machine: read until `\r\n` to get the hex size line, `size = int(s,16)`, if `size==0` set `end=True`, slice exactly `size` bytes, push the trailing `\r\n`+remainder back with `app.prepend(__buff__)` (`:269`), `yield chunk`, repeat. This is a streaming decoder — never buffers the whole chunked body.

### 1.4 JSON / form / multipart (KNOWN — `request.py:189-356`)

- `read_exactly(r, size)` (`request.py:171-187`): allocates `memarray(size)` **once**, fills it by `async for chunk in r.pull()` with a write-pointer and a remainder trim (`chunk[:rem]`). This is a **single-allocation body read** — no `bytearray` growth/realloc. Returns the buffer (optionally `.decode()`).
- `get_json` (`request.py:189-199`): if `content_length` known → `read_exactly` (single alloc); else accumulate via `payload.extend(chunk)` into a `bytearray`. Parse with `loads(payload.decode("utf-8"))` (ujson if available, `Dependencies/__init__.py:46-50`), wrap in `ddict` (dot-access dict, `Itools/__init__.py:14-15,35`). Bad JSON → `raise Err("No valid JSON found in the stream.")`.
- `body_or_params` / `bop` (`request.py:288-313`): if method in `non_bodied_methods` return `r.params()` (query dict) else `await get_json`. This is the FastAPI-ish "give me the payload regardless of verb".
- `params` (`request.py:213-223`): query string parsed **lazily and memoized** on `r.__miscellaneous__["params"]`. Parser is `get_params_sync` (`request.py:103-128`) — manual `find('=')`/`find('&')` scan with a `URL_DECODE_MAP` lookup table (`request.py:5`, 90 entries). The pure-Python impl is swapped for a C extension at import if `c_request_util` is available (`request.py:357-364`, `Dependencies/__init__.py:134-144`).
- `form_data` (`request.py:315-347`): naive single-file multipart — finds boundary markers by `rfind`, slices name/value/filename. **Not a robust multipart parser** (assumes one file, WebKit boundary). `Multipartdemux` (`request.py:366-457`) is the streaming multipart: an async-CM that parses the boundary header block, then `pull()` yields body parts trimming the trailing boundary. Both are weaker than FastAPI's `python-multipart`.

### 1.5 Why this is fast (INFERRED)

- **No request object allocation** — the protocol object is reused across keep-alive (`reset()` zeroes fields, `server_protocol_extratools.py:33-58`), so a hot connection allocates nothing per request except the body buffer.
- **Lazy query/body** — query string and body are not touched unless the handler asks. A pure routing 404 never decodes a single percent-escape.
- **Single-allocation body** via `read_exactly` + `memarray`.
- **Header values decoded eagerly** is the one non-lazy cost (acceptable; headers are small).

### 1.6 Steal-for-Lancelot

- **STEAL:** the `r.pull` rebind pattern — pick the body reader once from headers, hand the handler a uniform `async for chunk in request.stream():`. This is strictly better than FastAPI's `await request.body()` (which buffers the whole body) for AI upload/streaming-input workloads.
- **STEAL:** `read_exactly` single-allocation body read when `Content-Length` is known.
- **STEAL:** lazy memoized query parsing.
- **AVOID:** the hand-rolled multipart (`form_data`) — use `python-multipart` or a real streaming multipart. Blazeio's is a correctness liability.
- **AVOID:** capitalizing header keys in place (`"Content-length"`) — it breaks case-insensitive `Content-Length` lookups and forces every call site to guess the casing (`Deliver` literally checks 3 casings, `streaming.py:89,93`). Use a true case-insensitive multidict.

### 1.7 vs FastAPI / Robyn

- **FastAPI:** `Request.json()` calls `await self.body()` which **buffers the entire body** into memory before parsing; `request.stream()` exists but `json()`/form parsing do not use it. Pydantic validation then re-walks the parsed structure. Blazeio's `get_json` with known `Content-Length` is one alloc + one `ujson.loads`; no validation layer. Lancelot wants Blazeio's streaming body + *optional* (not mandatory) validation.
- **Robyn:** request parsing is in Rust (`robyn` core); the Python handler gets an already-materialized `Request` with `.body` as a string/bytes. Fast to parse but **fully buffered** — bad for large AI inputs. Blazeio's pure-Python streaming beats Robyn on memory for big bodies, loses on raw parse throughput for tiny ones.

## 2. The receive stream — zero-copy BufferedProtocol + pause-by-default pull model

This is the substrate the body reader and the request iterator sit on. `BlazeioProtocol` (`Protocols/__init__.py:4-67`) is mixed into `BlazeioServerProtocol` alongside `asyncio.BufferedProtocol` (`server_protocol.py:16`).

### 2.1 The buffer protocol callbacks (KNOWN — `Protocols/__init__.py:6-34`)

```python
def get_buffer(app, sizehint):
    if sizehint > len(app.__buff__memory__):
        app.__buff__ = bytearray(sizehint)
        app.__buff__memory__ = memoryview(app.__buff__)
    elif sizehint <= 0:
        sizehint = len(app.__buff__memory__)
    return app.__buff__memory__[:sizehint]

def buffer_updated(app, nbytes):
    app.transport.pause_reading()
    app.__stream__.append(bytes(app.__buff__memory__[:nbytes]))
    app.__evt__.set()
```
- `get_buffer` returns a **slice of the same persistent memoryview** every time (only reallocates if the kernel asks for more than 102400; `INBOUND_CHUNK_SIZE`, `Dependencies/__init__.py:132`). asyncio's `BufferedProtocol` writes the socket bytes *directly into this buffer* — **no intermediate `bytes` object on the read path** (this is the entire point of `BufferedProtocol` vs plain `Protocol.data_received`).
- `buffer_updated(nbytes)`:
  1. **`transport.pause_reading()` immediately** — the socket is paused the instant a chunk lands. The protocol does not keep reading ahead.
  2. `bytes(app.__buff__memory__[:nbytes])` — *one* copy out of the shared buffer into an immutable `bytes` appended to `__stream__` (a `deque`). This copy is unavoidable (the shared buffer must be freed for the next `get_buffer`), but it is exactly one copy of exactly the bytes received, no over-read.
  3. `app.__evt__.set()` — wake any coroutine blocked in `ensure_reading`.
- `eof_received` (`:25-27`): `__is_at_eof__=True`, wake. `connection_lost` (`:20-23`): wake both events, cancel the handler task if `cancel_on_disconnect`.

### 2.2 The pull engine — `ensure_reading` + `__aiter__` (KNOWN — `Protocols/__init__.py:36-67`)

```python
def prepend(app, data):
    if app.transport.is_reading(): app.transport.pause_reading()
    app.__stream__.appendleft(data)
    app.__evt__.set()

async def ensure_reading(app):
    if not app.transport.is_closing():
        if not app.__stream__:
            if not app.transport.is_reading():
                app.transport.resume_reading()
            await app.__evt__.wait_clear()
        if app.transport.is_closing(): app.abort_connection()
    else:
        app.abort_connection()

async def __aiter__(app):
    while True:
        await app.ensure_reading()
        while app.__stream__:
            yield app.__stream__.popleft()
```
**The model:** the socket is paused unless a consumer is actively waiting for the next chunk. `ensure_reading` only calls `resume_reading()` when `__stream__` is empty *and* a coroutine is about to `await`. As soon as one chunk arrives, `buffer_updated` pauses again. So at most one kernel-buffer's worth of unconsumed data exists in userland at any time. This is **strict pull-driven flow control** — the antithesis of the push model where `data_received` floods an unbounded queue.

`prepend` (`:36-39`) is the "un-read" primitive: header parser over-reads into the body, pushes the excess back to the *front* of `__stream__`, pauses reading. Used by `prepare_http_request` (`request.py:277`), `handle_chunked` (`server_protocol_extratools.py:269`), `StreamPeek` (`streaming.py:222`). It is what makes the layered parsers composable without a shared ring buffer.

`__await__` (`:59-61`): `await r` (the protocol object itself) returns the next chunk or `None` — used by `StreamPeek` (`streaming.py:212`).

### 2.3 Why this is fast (INFERRED)

- **`BufferedProtocol` eliminates the per-read `bytes` allocation** that plain `asyncio.Protocol.data_received` incurs. The kernel DMAs into Blazeio's own buffer.
- **One copy on receive** (`bytes(memoryview[:n])`), not two.
- **No unbounded queue.** Memory per connection is bounded by `INBOUND_CHUNK_SIZE` (102400) + whatever the handler holds. A slow handler cannot be memory-DoS'd by a fast sender because reading is paused until the handler pulls.
- **`deque` not `Queue`** — no asyncio `Queue` lock/condition overhead; wakeups are a single `SharpEvent.set()`.

### 2.4 Steal-for-Lancelot

- **STEAL (highest priority):** `BufferedProtocol` + one persistent buffer + cached memoryview. For an AI gateway proxying large request bodies (RAG documents, audio, images) this is the difference between O(1) and O(n) allocations.
- **STEAL:** pause-by-default + `resume_reading()` only when a consumer waits. This gives natural receive-side backpressure for free — critical when Lancelot proxies a streaming upload to an upstream LLM that is slower than the client.
- **STEAL:** `prepend` for composable parsers (header → body → multipart layering without copying).
- **NUANCE:** the `transport.pause_reading()` on *every* `buffer_updated` adds a syscall-ish toggle per chunk. For tiny chunks this is overhead. Lancelot could use a high-water threshold (pause only when `__stream__` exceeds N) instead of pause-always — a tunable.

### 2.5 vs FastAPI / Robyn

- **FastAPI/Starlette/uvicorn:** uvicorn's `httptools` protocol uses plain `Protocol.data_received(bytes)` — a fresh `bytes` per packet, fed into an `asyncio` flow with `h11`/`httptools` state. Starlette's `Request.stream()` reads from a queue uvicorn fills. Blazeio's `BufferedProtocol` is strictly lower-allocation on receive.
- **Robyn:** receive is in Rust (tokio/hyper); zero-copy-ish in Rust land but the body crosses the Rust→Python boundary as a materialized buffer. Blazeio keeps it in Python but never materializes the whole body.

## 3. The response writer + send backpressure — THE CRITICAL SECTION (LLM token streaming)

This is the path every generated token travels. Everything here matters for time-to-first-byte (TTFB), per-token flush cost, and slow-client backpressure.

### 3.1 `prepare()` — header emission + one-time writer selection (KNOWN — `server_protocol_extratools.py:306-341`)

```python
async def prepare(app, headers={}, status=200, reason="", encode_resp=True,
                  encode_event_stream=True, server_phrase="Blazeio/%s (%s)"%(...)):
    payload = ('HTTP/1.1 %d %s\r\n' % (status, StatusReason.reasons.get(status,"Unknown"))).encode()
    if app.__prepared_headers__:
        payload += app.__prepared_headers__
        app.__prepared_headers__.clear()
    await app.writer(payload)                       # status line + pre-staged headers go out FIRST

    headers_view = DictView(headers)
    app.__is_prepared__ = True
    app.__status__ = status
    ... # Server header injection
    if (val := headers_view.get("Content-encoding")) and encode_resp:
        app.encoder = getattr(app, val, None)       # 'gzip'/'br' bound method, else None
    else:
        app.encoder = None

    if headers_view.get("Transfer-encoding") == "chunked":      app.write = app.write_chunked
    elif headers_view.get("Content-length"):                     app.write = app.write_raw
    elif headers_view.get("Content-type") == "text/event-stream" and encode_event_stream:
                                                                 app.write = app.write_event_stream
    else:                                                        app.write = app.write_raw

    await app.writer(app.headers_to_http_bytes(headers))         # header block + final \r\n
    if app.method == "HEAD": raise Eof()                         # HEAD short-circuit via exception
```
Key facts for AI streaming:
- **Status line is flushed to the socket immediately** (`await app.writer(payload)` at `:313`) — *before* the body, before headers even. This minimizes TTFB: the client sees `HTTP/1.1 200` the moment the handler calls `prepare()`.
- The **writer is chosen exactly once** and `app.write` is rebound. For SSE LLM streaming you set `Content-Type: text/event-stream` → `app.write = write_event_stream`. For chunked JSON token streaming you set `Transfer-Encoding: chunked` → `write_chunked`. The handler then loops `await r.write(token)` with **zero per-token branching**.
- `DictView` (`Itools/__init__.py:260-287`) is a case-insensitive wrapper used only for the lookups here (it builds a `{Capitalized: original}` map once). Cheap, one-shot.
- `headers_to_http_bytes` (`server_protocol_extratools.py:165-175`): `b"%s: %s\r\n"` per header, supports list values (multi-header), ends with `\r\n`. One bytes concatenation, then one `writer()`.
- **`raise Eof()` for HEAD** (`:341`) — the response is terminated by raising an exception out of `prepare()`. This is the exception-control-flow pattern in its purest form (see §7).

### 3.2 `writer()` — the single socket-write chokepoint + send backpressure (KNOWN — `server_protocol.py:64-72`)

**Every byte that leaves the server goes through this:**
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
And the backpressure gate (`Protocols/__init__.py:29-43`):
```python
def pause_writing(app):  app.__is_buffer_over_high_watermark__ = True
def resume_writing(app):
    app.__is_buffer_over_high_watermark__ = False
    app.__overflow_evt__.set()

async def buffer_overflow_manager(app):
    if not app.__is_buffer_over_high_watermark__: return
    await app.__overflow_evt__.wait_clear()
```
**This is the heart of slow-client backpressure for LLM streaming.** Mechanics:
1. asyncio's transport calls `pause_writing()` when its internal write buffer exceeds the high-water mark (default 64 KiB) — i.e. when the **client is reading slower than the server is generating tokens**.
2. The next `await r.write(token)` → `writer()` → `buffer_overflow_manager()`. On the **fast path (client keeping up)** `__is_buffer_over_high_watermark__` is `False` → the function returns immediately, **no Future, no await suspension, no allocation**. This is the dominant case and it is essentially free (one attribute read + one boolean test).
3. On the **slow path (client behind)** it `await __overflow_evt__.wait_clear()` — the handler coroutine *suspends* until `resume_writing()` fires (transport buffer drained below low-water mark). The token generation loop naturally stalls: it cannot produce the next token until the current one is accepted by the OS socket buffer. **No unbounded server-side token buffer ever forms.** Memory is bounded by the transport's high-water mark, not by how far ahead the LLM has generated.
4. `transport.write(data)` — asyncio buffers and schedules the actual `send(2)`. `transport.is_closing()` guard → `abort_connection()` (raises `ClientDisconnected`) if the client vanished mid-stream — the handler/generator is then cancelled (see §3.6).

**Why this beats `asyncio.StreamWriter.drain()`** (which Starlette/uvicorn-based streaming effectively relies on): `drain()` *always* creates and awaits a Future even when not paused (it checks `_paused` but the call still goes through the coroutine machinery and the `StreamWriter`/`StreamReaderProtocol` indirection). Blazeio's fast path is a **synchronous early return on a boolean** — the `await buffer_overflow_manager()` resolves without ever suspending when the client is fast. At thousands of tokens/sec across many concurrent streams this removes a Future allocation + event-loop reschedule **per token**.

### 3.3 The three writers (KNOWN — `server_protocol_extratools.py:195-228`)

```python
async def write_raw(app, data):
    if app.encoder: data = await app.encoder(data)
    return await app.writer(data)

async def write_chunked(app, data):
    if app.encoder: data = await app.encoder(data)
    if isinstance(data, (bytes, bytearray)):
        await app.writer(b"%X\r\n%s\r\n" % (len(data), data))
    elif isinstance(data, AsyncIterable):
        async for chunk in data:
            await app.writer(b"%X\r\n%s\r\n" % (len(chunk), chunk))
        await app.write_chunked_eof()
    else:
        raise Err("Only (bytes, bytearray, AsyncIterable) are accepted...")

async def write_event_stream(app, data, start=b"data: ", end=b"\n\n"):
    if app.encoder: data = await app.encoder(data)
    if isinstance(data, (dict, list)): data = dumps(data, indent=0).encode()
    if data[:len(start)] != start:
        data = b"%b%b%b" % (start, data, end)
    return await app.writer(data)

async def write_chunked_eof(app, data=None):
    if data: await app.write(data)
    await app.writer(app.handle_chunked_endsig)     # b"0\r\n\r\n"
```
For LLM token streaming, the two relevant writers:

**`write_chunked` (Transfer-Encoding: chunked):** each token → `b"%X\r\n%s\r\n" % (len(token), token)`. That is **one bytes-format op per token** producing one framed chunk, then one `transport.write`. The hex-size + CRLF framing is ~10–12 bytes overhead per token (acceptable; an LLM token is typically 2–6 bytes so framing overhead is ~2x — see §3.5 for the batching mitigation). It also natively accepts an `AsyncIterable`: you can hand `write_chunked` the *token generator itself* and it drains it and emits the `0\r\n\r\n` terminator automatically. **This is the cleanest token-streaming entry point in the codebase.**

**`write_event_stream` (Content-Type: text/event-stream, SSE):** wraps each payload as `b"data: <payload>\n\n"` unless it already starts with `data: `. dict/list auto-`dumps(indent=0)` (compact JSON). This is the **OpenAI-compatible SSE shape** (`data: {json}\n\n`, the `[DONE]` sentinel you send yourself). Lowest per-token framing overhead beyond raw: 8 bytes (`data: ` + `\n\n`). No hex length prefix. **This is what Lancelot should default to for OpenAI-compatible streaming endpoints.**

`eof()` (`server_protocol_extratools.py:230-239`): if `app.write == app.write_chunked` it emits `write_chunked_eof()` (`0\r\n\r\n`); otherwise nothing (raw/SSE need no terminator — SSE just closes the connection or the handler sends `data: [DONE]\n\n`). Called by the keep-alive loop's `finally` (`server_protocol.py:111-112`).

### 3.4 `Batchedwriter` — the per-token-flush-cost mitigation (KNOWN — `Modules/Server/writers.py:7-29`)

This is the single most AI-relevant utility in the whole module set:
```python
class Batchedwriter:
    def __init__(app, r=None, min_chunk_size=1024*10):
        app.r, app.buff, app.min_chunk_size, app.rem_chunk_size = r, bytearray(), min_chunk_size, min_chunk_size*3
    async def __aenter__(app):
        if not app.r: app.r = Context._r()
        app.parent_write, app.r.write = app.r.write, app.write   # monkey-patch r.write
        return app
    async def __aexit__(app, *a):
        if app.buff: await app.parent_write(bytes(app.buff))      # flush remainder
        app.r.write = app.parent_write
        return False
    async def write(app, chunk):
        app.buff.extend(chunk)
        if len(app.buff) >= app.rem_chunk_size:                   # >= 30 KiB
            view = memoryview(app.buff)
            _, app.buff = await app.parent_write(bytes(view[:app.min_chunk_size])), bytearray(bytes(view[app.min_chunk_size:]))
```
**What it does:** an async-CM that transparently swaps `r.write` for a buffering writer. Tokens accumulate in `app.buff`; only when the buffer reaches `min_chunk_size*3` (default **30720 bytes**, `rem_chunk_size`) does it flush the first `min_chunk_size` (**10240 bytes**) and keep the tail. On exit, flush the remainder and restore the original `r.write`. Constants: `min_chunk_size = 1024*10 = 10240`, `rem_chunk_size = min_chunk_size*3 = 30720` (`writers.py:9-10`).

**Why this matters for LLM streaming:** flushing every single 3-byte token through `transport.write` + framing + a possible event-loop tick is expensive at high token rates and produces tiny TCP segments (Nagle/latency tradeoffs). `Batchedwriter` coalesces tokens into ~10 KiB writes, trading a small amount of TTFB-per-token latency for far fewer syscalls and better wire efficiency. **The catch (see §3.5): a 30 KiB threshold is far too coarse for interactive chat** — it would hold ~5000 tokens before the first flush, destroying perceived streaming. The *pattern* is right, the *constant* is wrong for AI.

### 3.5 AI / LLM token-streaming deep analysis (the most important subsection)

**(a) Time-to-first-byte (TTFB).** Excellent by construction. `prepare()` flushes the status line *and* headers to the socket synchronously before the body (`:313, 339`). The client receives `HTTP/1.1 200` + headers the instant the handler calls `await Prepare.json(chunked=True)` / sets up SSE — typically *before* the first LLM token even arrives from the upstream model. There is no header buffering, no `Content-Length` computation forcing a full-body materialization (the trap FastAPI's non-streaming `JSONResponse` falls into). **Verdict: best-in-class TTFB; replicate exactly.**

**(b) Per-token flush cost.** On the fast path, one `await r.write(token)` costs: one `isinstance` check + one `b"%X\r\n%s\r\n" % (...)` format (chunked) or one prefix check + `b"data: %b\n\n"` format (SSE), then `buffer_overflow_manager()` (synchronous boolean early-return — **no suspension**), then `transport.write()` (a buffered, non-blocking enqueue — does not await the kernel). **No Future is allocated per token on the hot path.** This is materially cheaper than Starlette's `StreamingResponse`, where each chunk goes through `await send({...})` over the ASGI interface (a dict allocation + ASGI app/server boundary + uvicorn's `drain()`), and far cheaper than any framework that re-serializes via `jsonable_encoder` per chunk. Estimated hot-path per-token overhead: ~1 bytes-format + ~3 attribute reads + 1 `transport.write`. **This is the number Lancelot must match or beat.**

**(c) Slow-client backpressure (the safety-critical property).** This is where Blazeio's design is genuinely strong for AI. An LLM can generate faster than a mobile client on 3G can receive. Without backpressure the server buffers the entire (potentially 100 KB) completion in RAM per stalled client → OOM under load. Blazeio's mechanism: when the client lags, asyncio calls `pause_writing()` → next `r.write(token)` suspends on `__overflow_evt__.wait_clear()` → **the token-generation coroutine itself stalls** (it's the same coroutine doing `async for tok in llm: await r.write(tok)`). If the upstream LLM is an HTTP stream, *that* stream's own receive backpressure (§2, pause-by-default) then stops pulling from the model. **Backpressure propagates end-to-end: slow client → stalled handler → paused upstream-LLM read.** Server-side unbuffered memory per stream is bounded by the transport high-water mark (~64 KiB), not by completion length or client speed. **This is the property to copy verbatim.**

One caveat (INFERRED): if the upstream LLM provider charges for generated tokens and does *not* support pause/resume on its SSE stream, stalling the read does not stop billing (the provider keeps generating). That is a provider-protocol limitation, not a Blazeio flaw — but Lancelot's design should add an explicit "client gone → cancel upstream request" path (Blazeio gets this via `abort_connection()` → task cancel, §3.6).

**(d) Async-generator overhead.** The idiomatic Blazeio LLM handler is:
```python
async def chat(r):
    await Prepare.json(chunked=True)            # Transfer-Encoding: chunked
    async for token in llm_stream(prompt):
        await r.write(token.encode())
    # eof() emits 0\r\n\r\n in the keep-alive finally
```
or, even cleaner, hand the generator directly to `write_chunked` (it accepts `AsyncIterable` and auto-terminates, `:205-209`):
```python
async def chat(r):
    await Prepare.json(chunked=True)
    await r.write(llm_stream(prompt))           # write_chunked drains the gen + emits 0\r\n\r\n
```
Async-generator overhead in CPython is real (~0.5–1 µs per `__anext__` + the `await`), but it is *unavoidable* for streaming and is dwarfed by network and model latency. Blazeio adds **no extra generator layers** on the hot path — the handler's generator drives `write_chunked` directly. Contrast: Starlette's `StreamingResponse` wraps the user generator, then ASGI wraps that, then uvicorn wraps that — 3 generator/coroutine layers per chunk. Blazeio: 1.

**(e) The `Batchedwriter` tension for interactive AI.** As noted in §3.4, the 30 KiB flush threshold is correct for bulk transfer, catastrophic for chat (holds thousands of tokens). **DESIGNED recommendation for Lancelot:** make the coalescing threshold a *time-or-size* flush (flush when buffer ≥ N bytes **OR** ≥ T milliseconds since last flush, default T≈20–50 ms, N≈1–4 KiB). This gives wire efficiency without sacrificing perceived streaming. Blazeio has only the size trigger — a real gap to improve on.

**(f) Encoder interaction.** If `Content-Encoding: gzip/br` is set, `app.encoder` is bound and `write_*` calls `data = await app.encoder(data)` *per chunk* (`:196, 201, 214`). `br` offloads to a thread (`to_thread(brotlicffi_compress,...)`, `server_protocol_extratools.py:343-344`); `gzip` is sync per-chunk (`:346-350`). **Per-token gzip is a disaster for streaming** (each tiny token compressed independently → negative compression + CPU per token). DESIGNED: Lancelot should *disable* response compression on token streams (SSE/chunked LLM) or use a streaming compressor with a shared context + periodic flush, never per-token independent compression. Blazeio's per-chunk model is wrong here; do not copy it for streams.

### 3.6 Disconnect handling during a stream (KNOWN — `server_protocol.py:53-56`, `Protocols/__init__.py:20-23`)

`connection_lost` → `app.cancel()` if `cancel_on_disconnect` (default True). `app.cancel` is the handler **task's `.cancel`** (`server_protocol.py:50`). So when the client disconnects mid-stream, the handler coroutine (the one running `async for tok in llm: await r.write(tok)`) is **cancelled at its next await** — which propagates `CancelledError` into the LLM generator, closing the upstream connection (if the generator is written to clean up on cancel). `ConnectionTaskShield` (`server_protocol.py:123-138`) is an async-CM to *temporarily* disable this (`cancel_on_disconnect=False`) for critical sections that must finish even if the client leaves (e.g. committing a billing record). **STEAL both:** auto-cancel-on-disconnect is exactly what an AI gateway needs to stop paying for tokens nobody will read; the shield CM is the escape hatch for must-complete work.

### 3.7 Steal-for-Lancelot (streaming)

- **STEAL:** flush status+headers synchronously in `prepare()` before any body (TTFB).
- **STEAL:** select the writer once, rebind `r.write`; no per-token branch.
- **STEAL:** the boolean-fast-path backpressure gate (`buffer_overflow_manager`) instead of `drain()` — no Future per token on the hot path.
- **STEAL:** `write_chunked` accepting an `AsyncIterable` and auto-emitting the terminator — hand it the LLM generator directly.
- **STEAL:** SSE writer shape `b"data: %b\n\n"` (OpenAI-compatible) + dict auto-`dumps(indent=0)`.
- **STEAL:** cancel-handler-task-on-disconnect + a shield CM escape hatch.
- **STEAL the pattern, FIX the constant:** `Batchedwriter` coalescing — but make it time+size, default ~20–50 ms / ~1–4 KiB, not 30 KiB size-only.
- **AVOID:** per-chunk independent gzip/br on token streams.
- **AVOID:** `raise Eof()` inside `prepare()` for HEAD as the *only* mechanism (works, but exceptions for normal control flow — see §7).

### 3.8 vs FastAPI `StreamingResponse` / Robyn

- **FastAPI/Starlette `StreamingResponse`:** wraps the user's async generator; sends each chunk via the ASGI `send({"type":"http.response.body","body":chunk,"more_body":True})` — a dict allocation per chunk, crossing the ASGI app↔server boundary, then uvicorn frames it and calls `drain()` (Future every time). Headers are sent on the first body chunk (slightly worse TTFB than Blazeio's send-at-prepare). No built-in token-batching. `jsonable_encoder` is *not* on the streaming path (good) but `JSONResponse` (non-streaming) buffers + `json.dumps` the whole body. Net: Blazeio is ~2–3 fewer layers and no per-chunk Future on the fast path. **This is the concrete perf gap Lancelot's "faster-FastAPI" pitch rests on for AI.**
- **Robyn:** streaming responses marshalled through Rust; the Python generator yields to Rust which frames + writes. Lower Python overhead per chunk than Starlette, but the GIL boundary crossing per chunk and Robyn's less mature streaming/SSE story (and weaker backpressure exposure) make Blazeio's pure-async model more controllable for end-to-end AI backpressure. Lancelot can match Robyn's per-chunk cost in pure Python by copying Blazeio's no-Future fast path, while keeping full async backpressure visibility.

## 4. `SharpEvent` — the pooled-future event that makes per-token flush cheap (KNOWN — `Dependencies/__init__.py:146-200`)

Every wakeup in the streaming/backpressure path (`__evt__`, `__overflow_evt__`) is a `SharpEvent`, not `asyncio.Event`. This is why §3.2's slow path is also cheap:
```python
class SharpEvent:
    __slots__ = ("_set","_waiters","loop","auto_clear")
    def __init__(app, auto_clear=False, evloop=None):
        app._set, app._waiters, app.loop, app.auto_clear = False, [], evloop or get_event_loop(), auto_clear
    def get_fut(app):
        if app._waiters: return app._waiters[0]                  # reuse the single in-flight future
        app._waiters.append(fut := app.loop.create_future())
        fut.__is_cleared__ = False; fut.__acallbacks__ = [app.fut_done]
        fut.add_done_callback(app.done_callback_orchestrator)
        return fut
    async def wait(app):
        if app._set: return True                                 # fast path: no future at all
        return await app.get_fut()
    async def wait_clear(app):
        result = await app.wait(); app.clear(); return result
    def set(app, item=True):
        app._set = True
        if len(app._waiters) == 1:
            if not app._waiters[0].done(): app._waiters[0].set_result(item)
        else:
            for fut in app._waiters:
                if not fut.done(): fut.set_result(item)
        app._waiters.clear()
```
**Mechanics & why fast:**
- `wait()` returns `True` synchronously if already set — **no future, no suspension** (mirrors the `buffer_overflow_manager` early return).
- When a wait *is* needed, a **single future is shared by all waiters** (`get_fut` returns `_waiters[0]` if present). `set()` resolves it once and clears the list. `asyncio.Event` by contrast creates a fresh future per `wait()` call and manages a waiter set with more bookkeeping.
- `wait_clear()` = wait then immediately `clear()` — the exact "consume one wakeup" semantic the receive loop (`ensure_reading`) and backpressure gate need, in one call.
- No lock. `set()` is O(waiters) with the common case being 0 or 1 waiter.

**Steal-for-Lancelot:** use a pooled-future event for the receive-wake and write-backpressure signals. Under thousands of concurrent token streams the difference between `asyncio.Event` (future-per-wait) and `SharpEvent` (future-pooled, sync fast path) is real allocator + GC pressure. This is a low-risk, high-leverage steal.

`ioCondition` (`Dependencies/alts.py:103-191`) is a hand-built lock+condition on top of `SharpEvent` (used by the queue/log subsystems, not the hot streaming path) — note it exists but it is **not** on the per-token path; do not over-index on it for Lancelot's streaming.

---

## 5. Exception-as-response control flow — mechanics, why, and the verdict

### 5.1 The exception tree (KNOWN — `Exceptions/__init__.py`)

All inherit `BlazeioException(Exception)` with `__slots__` (zero `__dict__`, cheap to construct):
- `Err(message)` — generic; `__str__` returns the message.
- `Abort(*args, **kwargs)` — **the response-as-exception** (defined in `streaming.py:117-130`, not Exceptions/): `args = (message, status, headers)`. Has `.text(r)` which calls `Deliver.text(r, memoryview(message), status, headers)` — i.e. *raising `Abort` and having it caught emits the actual HTTP response*.
- `NotFoundErr(Abort)` (`streaming.py:132-133`) — semantic 404 subclass.
- `Eof()` — slotted, no-arg; used to signal "response is finished / no body" (HEAD short-circuit, empty request).
- `ClientDisconnected`, `ServerDisconnected`, `CloseConnection`, `Protocoltimeout`, `ProtocolError`, `Missingdependency`, `ModuleError`, `ServerGotInTrouble`, `ClientGotInTrouble` — control/lifecycle signals, all `BlazeioException`.

### 5.2 The catch site — the keep-alive loop (KNOWN — `server_protocol.py:96-121`)

```python
async def __main_handler__(app, r):
    while not r.transport.is_closing():
        exc = None
        try:
            r += app.keepalive_headers                  # __iadd__ pre-stages bytes (:177-181)
            await app.get_handler()(r)                   # run the user handler
        except Abort as e:
            await e.text(r)                              # <-- Abort IS the response
        except (Eof, ServerGotInTrouble, BrokenPipeError):
            ...                                          # swallow: response already (or intentionally) done
        except (CancelledError, KeyboardInterrupt, CloseConnection, ClientDisconnected, ServerDisconnected):
            raise                                        # propagate: connection teardown
        except Exception as e:
            exc = e                                      # unexpected: log + 500 in finally
        finally:
            if r.__is_prepared__: await r.eof()          # emit chunked terminator if needed
            else: await Abort("Server Got In Trouble",500).text(r)
            if app.after_middleware: await app.after_middleware(r)
            if exc: await traceback_logger(exc, frame=4)
            ServerProtocolEssentials.reset(r)            # zero fields, reuse protocol for next keep-alive req
```
**How a handler "returns a response":** it doesn't, primarily. The patterns are:
1. **Imperative:** `await r.prepare(headers,status); await r.write(body)` then return normally → `finally` calls `r.eof()`.
2. **`Deliver` one-shot:** `await io.Deliver.json(data, status)` (`streaming.py:70-115`) which internally `prepare()` + `write()`.
3. **Exception:** `raise Abort("nope", 403)` / `raise NotFoundErr("Not Found",404)` from *anywhere* in the call stack → unwinds to the loop → `await e.text(r)` emits it. `raise Eof()` ends the response (HEAD: `prepare()` itself raises it, `:341`).

So the framework uses exceptions for **early-exit / error responses and HEAD termination**, while the success path is imperative writes. It is *not* "every response is a raised exception" — it's "abort/404/short-circuit are raised; streamed/normal responses are written."

### 5.3 Why they did it (INFERRED)

- **Ergonomics / depth-escape:** `raise Abort(403)` from 5 frames deep in a validator immediately produces the response without threading a return value back up through every caller. This is the same reason Starlette has `HTTPException` and Flask has `abort()`.
- **`__slots__` exceptions are cheap:** `Abort.__init__` just stores `args`/`kwargs` (`streaming.py:119-122`) — construction cost is negligible vs the response I/O it triggers.
- **Uniform error funnel:** one `except Abort` in the loop centralizes all error-response emission + the `finally` guarantees `eof()`/cleanup/`reset()` run on *every* path (success, abort, crash, disconnect). This is genuinely robust — the keep-alive protocol object is always reset for the next request.
- **HEAD via `raise Eof()`** avoids sprinkling `if method=="HEAD": return` after every `prepare()`.

### 5.4 The tradeoffs / pitfalls (INFERRED — the "avoid" analysis)

- **Exceptions as normal control flow are slower than returns** in CPython when actually raised+caught (frame unwinding, traceback construction unless suppressed). For *error* paths (403/404/500) this is fine — they're rare. For anything on the **hot path it would be a real cost**. Blazeio mostly avoids this (success = imperative write), *except* `raise Eof()` for every HEAD request (`:341`) and `raise Eof("Request has no headers")` for every malformed/probe request — those are exception-per-request on non-negligible traffic classes (health probes, HEAD checks). Measurable under load.
- **Control flow is non-local and invisible at the call site.** `raise NotFoundErr(...)` deep in a helper produces an HTTP 404 with side-effecting I/O (`e.text(r)`) — the helper's signature gives no hint it can terminate the request. Hard to reason about, hard to unit-test handlers in isolation.
- **`except Exception as e: exc = e` then 500 in `finally`** swallows the original traceback location unless `traceback_logger` is reached; a bug in `after_middleware` or `eof()` inside `finally` can mask the real error.
- **Casing fragility leaks from this design:** because there's no response object, `Deliver`/`Abort` must hand-check `"Content-type"`/`"Content-Type"`/`"content-type"` (`streaming.py:89,93`) — symptomatic of "no typed response, just dicts and exceptions."
- **`Abort` defined in `streaming.py` not `Exceptions/`** — the response-bearing exception lives in the streaming module while the rest of the tree is in `Exceptions/`. Organizational smell that reflects the conflation of "exception" and "response."

### 5.5 Verdict for Lancelot

**Steal the ergonomics, reject it as the primary return mechanism.**
- **DO:** provide an `abort(status, detail)` / `raise HTTPException(...)` for error/early-exit, caught in one central place, with a guaranteed `finally` that runs cleanup + emits the chunked terminator + resets pooled state. Blazeio's centralized funnel + `finally`-guaranteed `eof()/reset()` is genuinely good and worth copying *for the error path*.
- **DO NOT:** make normal/success responses flow through `raise`. Lancelot handlers should `return` a response (or stream via `async for`/generator), like FastAPI. Returns are faster, locally reasoned, and unit-testable.
- **DO NOT:** terminate HEAD via a raised exception out of header preparation. Handle HEAD with an explicit `if r.method == "HEAD": <send headers, no body, return>` branch — same effect, no per-request exception on a real traffic class.
- **DO:** keep the `__slots__` + zero-`__dict__` exception classes for the *error* path (cheap construction is a legit micro-win there).
- **DO:** introduce a real typed `Response`/`StreamingResponse` object (FastAPI-style) so header casing, content-type, and content-length are handled once, not hand-checked in 3 casings everywhere.

## 6. Static file serving — the live path, the dead twins, and the missing sendfile

### 6.1 `Simpleserve` — the real, live static server (KNOWN — `server_tools.py:7-149`)

This is the one actually wired into routing (`StaticServer.handler` → `Simpleserve`, `server_tools.py:226-244`). It is a proper HTTP static server:

- **Metadata** (`prepare_metadata`, `:51-109`): `file_size = path.getsize`, `file_stats = stat(file)`, `last_modified = st_mtime`.
- **ETag** (`:62-64`): `app.etag = "BlazeIO--%s--%s" % (filename, file_size)`. **Weak ETag based only on name+size** — does NOT include mtime/inode/content hash. *Pitfall:* a file edited without size change keeps the same ETag → stale 304s. (Note: `handle_all_middleware` in `static.py` *does* re-check size, but `static.py` is dead — see §6.3.)
- **Last-Modified** (`:65`): `strftime("%a, %d %b %Y %H:%M:%S GMT", gmtime(mtime))` — RFC1123.
- **Conditional requests** (`:23-37`): `is_modified()` compares `If-Modified-Since` (parsed via `strptime` same format) to `last_modified_str`; equal → `raise Abort("",304,headers)`. `is_match()` compares `If-None-Match` to `etag` → `raise Abort("",304,headers)`. `validate_cache` (`:35-37`): try modified-since first, fall back to etag. **304 is emitted by raising `Abort` — the exception-as-response pattern again.**
- **Range requests** (`:87-98`): parses `Range: bytes=start-end` via `rfind('=')` then `find('-')`. `end` defaults to `file_size-1`. If `end > file_size` → `raise Abort("Range Not Satisfiable",416,{"Content-Range":"bytes */<size>"})`. Sets `Content-Range: bytes start-end/size`, status 206. No `Range` → status 200, full file. **Single-range only; no multipart/byteranges.**
- **Content-Length** (`:103-104`): `str(app.end - app.start)` (note: off-by-one vs the usual `end-start+1` for inclusive ranges — a latent range bug for exact-byte requests).
- **Content-Type** via `guess_type`; unknown → `application/octet-stream` + `attachment` disposition (`:67-72`). `image/webp` is registered (`:9`).
- **Cache-Control** (`:83-85`): `public, max-age=<n>, must-revalidate` (default `max-age=3600`); `image`/`video` types forced to 3600 (`rare_validation`, `:12`).
- **Method guard** (`validate_method`, `:39-42`): only `GET`/`HEAD`; else `raise Abort(405, Allow: "GET, HEAD")`.
- **The actual file streaming** (`pull`, `:127-135`):
```python
async def pull(app):
    async with async_open(app.file, "rb") as fd:
        if app.start: fd.seek(app.start)
        while (chunk := await fd.read(app.CHUNK_SIZE)):     # CHUNK_SIZE default 1024
            yield chunk
            if app.start >= app.end: break
            app.start += len(chunk); fd.seek(app.start)
```
`async_open` is `aiofile`'s async file IO (`Dependencies/__init__.py:497`). Default `CHUNK_SIZE = 1024` (`server_tools.py:13`) — **very small for static; 64 KiB+ would be better**. The handler does `async for chunk in f: await r.write(chunk)` (`:240-241`) → goes through the same `write_raw`/backpressure path as everything else (so static serving also gets slow-client backpressure for free).
- **Path traversal guard** (`:232-233`): `if not abspath(file_path).startswith(abspath(root_dir)): raise Abort("Access denied",403)`. Good — present and correct.
- `push` (classmethod, `:111-126`): one-shot variant that also negotiates `Accept-Encoding` gzip/br for compressable types and `prepare()`s then drains `pull()`.

### 6.2 `IN_MEMORY_STATIC_CACHE` — RAM cache design (KNOWN but DEAD — `IN_MEMORY_STATIC_CACHE.py`)

Documented for completeness because the brief asks, but **this code cannot run**: line 7 `from .streaming import Stream, Abort` — `streaming.py` exports `Abort` but **no `Stream`** (verified by grep: only `StreamJson`, `StreamPeek`, `StreamJson` in `streaming.py`; no `class Stream`). Importing this module raises `ImportError`. Same for `static.py` (`from .streaming import *` then uses `Stream.init`/`Stream.write` which don't exist; `Smart_Static_Server` calls `Stream.init`/`Stream.write` at `static.py:226-279`). **Treat both as abandoned prototypes.** The design *intent* (still worth noting as a pattern):
- `cache = {route: {"headers", "path", "raw_chunks":[bytes...], "compressed_chunks":[bytes]}}` (`IN_MEMORY_STATIC_CACHE.py:57-74`). Key = route string.
- First serve reads file in `chunk_size=1024` chunks (`:14, 119-124`), appends each to `raw_chunks` while streaming → file is cached in RAM *as a list of 1 KiB byte chunks* on first request.
- After caching, if `Content-Type ∈ compress_for` (16 text types, `:16-33`), a background `create_task(compressed_chunk(...))` gzips the whole concatenation once into `compressed_chunks` (`:50-55, 132-134`).
- Subsequent requests with `Accept-Encoding: gzip` serve `compressed_chunks`; else `raw_chunks` (`:82-92`). Staleness check: `Content-Length == path.getsize` (size-only, same weak invalidation).
- `Smart_Static_Server.cache_watch` (`static.py:187-201`): a `loop.create_task` background loop, every `cache_check_interval=60`s evicts entries whose `last_hit` age ≥ `cache_aliveness=60.0`s (`static.py:156-158`). `max_cachable_file_size = 1024` (only files ≤ 1 KiB cached — almost nothing; clearly unfinished). `static_compressed_chunk_size = 1024*1024`.
- **TTL eviction by idle time, LRU-ish via `last_hit`, no total-memory cap.** No size ceiling beyond the (broken) 1 KiB per-file gate, no global byte budget — would be a memory-leak risk if it ran.

### 6.3 Static verdict + steal/avoid

- **STEAL:** the conditional-request handling (ETag + If-None-Match + If-Modified-Since → 304), Range parsing, path-traversal guard, and the fact that static streaming reuses the *same* backpressured `write` path.
- **STEAL the intent of:** an in-RAM static cache with gzip-once + idle TTL eviction — but with a **content-hash or mtime+size ETag**, a **global memory budget with real LRU eviction**, and a sane `max_cachable_file_size`.
- **AVOID:** `CHUNK_SIZE=1024` for static (use ≥64 KiB). Name+size-only ETag (use mtime+size or content hash). The `end-start` (no `+1`) Content-Length for ranges. Shipping two dead modules (`static.py`, `IN_MEMORY_STATIC_CACHE.py`) that `ImportError` — Lancelot must not ship code that can't import.
- **ADD (Blazeio is missing it):** real `sendfile(2)` via `transport.sendfile()` / `loop.sendfile()` / `os.sendfile` for non-range, non-compressed static responses — true kernel zero-copy disk→socket, eliminating the userland `read→write` bounce entirely. **Irrelevant to LLM streaming** (generated tokens have no fd) but a clear win for serving model weights, datasets, or static assets from an AI product's web tier.

### 6.4 vs FastAPI / Robyn (static)

- **FastAPI/Starlette `FileResponse`:** *does* use `loop.sendfile()` when available (zero-copy), supports Range, ETag (from mtime+size), Last-Modified. Starlette's static is more correct than Blazeio's live `Simpleserve` on ETag and uses sendfile. **Here FastAPI/Starlette is ahead of Blazeio** — Lancelot should follow Starlette's static design, not Blazeio's.
- **Robyn:** static files served from Rust (actix/hyper static) — fast, sendfile-class. Again ahead of Blazeio on static. Blazeio's static is its weakest subsystem; copy it the least.

---

## 7. Async templating — streaming marker substitution (KNOWN — `templatify.py`, `asynctemplate.py`)

Low priority for an AI framework but in scope. Two implementations:

**`TemplateEngine` (`templatify.py`):** marker syntax `<!--app.method-->`, `<!--r.attr.path-->`, `<!--metadata_key-->` (`:23-67`). `templatify()` is an **async generator**: scan chunk for `b"<!--"`..`b"-->"`, yield the literal prefix, resolve the marker (call `app.<method>(r,ctx)` async-gen, or walk `r.<attr>.<path>` with `await sleep(0)` cooperative yields per segment, or look up a `metadata` callable), yield the substitution, continue. Page composed of: `template_ctx.pull()` (the layout file streamed in 1024-byte chunks) with `<!--main-->` replaced by the content file's `ctx.pull()` (`:91-101`). Sets `Transfer-Encoding: chunked` and `r.write_chunked` per resolved fragment, `r.write_chunked_eof()` at the end (`:88-103`). **The whole page is never assembled in memory** — it streams the layout, splicing the body generator at the marker. `escape()` (html-escape) applied to interpolated values (`:14-17, 61`) — XSS-safe by default for `r.`/`ayield` paths.

**`Asynctemplate` (`asynctemplate.py`):** richer — markers can carry inline instructions `<!--var(instruction)-->` and encoders `<!--var(::strip_newlines::)-->`. `stream_file` reads in `static_chunk_size` (= `ioConf.INBOUND_CHUNK_SIZE` = 102400) chunks and pipes through `templatify` recursively (`:64-69`). Resolution order: instruction-map (e.g. `stream_file(path)`), then `metadata`, then `r.store` (`:84-95`). Same streaming-generator-no-buffer design.

**Steal-for-Lancelot:** the *pattern* (stream the template, splice generators at markers, never build the page) is sound and composes with the token-streaming model — you could stream an LLM response *into* a `<!--llm_output-->` marker of an HTML shell. But marker-as-HTML-comment is fragile and the resolver does `hasattr/getattr` reflection on `r` (a security smell — arbitrary attribute walk from template text). For Lancelot: if templating is needed, use a real engine (Jinja2 async) or a typed component model; only borrow the "splice an async generator into a streamed shell" trick for AI-output-in-page use cases. **Do not** copy the `getattr(r, x)` template-driven attribute walk (`templatify.py:45-58`) — it's an injection vector.

---

## 8. Every numeric constant (KNOWN — file:line)

| Constant | Value | Where | Meaning / AI relevance |
|---|---|---|---|
| `INBOUND_CHUNK_SIZE` | `102400` | `Dependencies/__init__.py:132` | Persistent receive buffer size + `memarray` reuse threshold. Per-connection RAM ceiling for unconsumed receive. |
| `OUTBOUND_CHUNK_SIZE` | `102400` | `Dependencies/__init__.py:132` | Declared; not directly used in the write path (writes are caller-sized). |
| `__http_request_max_buff_size__` | `102400` | `Dependencies/__init__.py:74` | Max header bytes before `Abort(413)`. |
| `__http_request_heading_end_seperator__` | `b"\r\n\r\n"` (len `4`) | `Dependencies/__init__.py:72-73` | Header/body split token. |
| `__http_request_buff_size` | `4096*2` = `8192` | `Dependencies/__init__.py:75` | Declared request buff size (legacy/unused on the BufferedProtocol path). |
| `__http_request_initial_separatir__` | `b' '` | `Dependencies/__init__.py:76` | Request-line splitter. |
| `StaticFileHandler.CHUNK_SIZE` | `1024` | `static.py:7` (DEAD) | Dead-path static chunk. |
| `Simpleserve` default `CHUNK_SIZE` | `1024` | `server_tools.py:13` | **LIVE** static read chunk — too small; recommend ≥65536. |
| `Simpleserve` default `cache_control` | `{"max-age":"3600"}` | `server_tools.py:13` | Static Cache-Control max-age. |
| `Batchedwriter.min_chunk_size` | `1024*10` = `10240` | `Modules/Server/writers.py:9` | Coalesced flush unit. **Token-stream relevant — too coarse for chat.** |
| `Batchedwriter.rem_chunk_size` | `min*3` = `30720` | `Modules/Server/writers.py:10` | Buffer-full flush trigger. **30 KiB → ~thousands of tokens held; fix to time+size.** |
| `IN_MEMORY_STATIC_CACHE.chunk_size` | `1024` | `IN_MEMORY_STATIC_CACHE.py:14` (DEAD) | RAM-cache read chunk. |
| `Smart_Static_Server.static_chunk_size` | `1024` | `static.py:153` (DEAD) | — |
| `Smart_Static_Server.max_cachable_file_size` | `1024` | `static.py:154` (DEAD) | Only ≤1 KiB cached (unfinished). |
| `Smart_Static_Server.static_compressed_chunk_size` | `1024*1024` | `static.py:155` (DEAD) | — |
| `Smart_Static_Server.cache_aliveness` | `60.0` s | `static.py:156` (DEAD) | Idle TTL before eviction. |
| `Smart_Static_Server.cache_check_interval` | `60` s | `static.py:157` (DEAD) | Eviction sweep period. |
| `handle_chunked_endsig` | `b"0\r\n\r\n"` | `server_protocol_extratools.py:161` | Chunked-transfer terminator (sent by `eof()`). |
| chunked frame fmt | `b"%X\r\n%s\r\n"` | `server_protocol_extratools.py:204,207` | Per-chunk/per-token framing (~10–12 B overhead). |
| SSE frame | `start=b"data: "`, `end=b"\n\n"` | `server_protocol_extratools.py:213` | Per-token SSE framing (8 B overhead). OpenAI-compatible. |
| `Httpkeepalive.timeout` | `60*60*5` = `18000` s | `server_protocol.py:76` | Keep-Alive idle timeout (5 h — generous; fine for long AI streams). |
| `Httpkeepalive._max` | `1000**2` = `1_000_000` | `server_protocol.py:76` | Keep-Alive max requests/conn. |
| watchdog `default_payload_timeout` | `60*60` = `3600` s | `Dependencies/__init__.py:548` | Default per-request timeout (long — won't kill slow LLM streams prematurely). |
| watchdog `default__timeout_check_freq__` | `30` s | `Dependencies/__init__.py:549` | Stuck-task sweep period. |
| `set_cookie` default expiry | `Tue, 07 Jan 2030 01:48:07 GMT` | `server_protocol_extratools.py:277` | Hardcoded far-future cookie expiry. |
| `br_decoder` thread threshold | `102400` | `request.py:13` | Brotli decode offloaded to thread if ≥100 KiB. |
| `dumps` default indent | `4` (override `0` for SSE/chunked) | `Dependencies/__init__.py:48`; `server_protocol_extratools.py:217` | SSE/event-stream uses compact `indent=0`. |

---

## 9. Recommended Lancelot streaming-response design for AI workloads (DESIGNED)

This is the synthesized blueprint — the whole point of the teardown.

### 9.1 Transport / receive layer

Adopt Blazeio's substrate verbatim:
- `asyncio.BufferedProtocol`, one persistent `bytearray` + cached `memoryview` per connection, reallocated only if the kernel asks for more (Blazeio's `INBOUND_CHUNK_SIZE = 102400` is a fine default; expose it as a tunable).
- `get_buffer` returns a slice of the cached memoryview; `buffer_updated` does exactly one `bytes(mv[:n])` copy into a `deque`, then `pause_reading()`.
- Pull-driven receive: `resume_reading()` only when a consumer awaits and the queue is empty. This gives end-to-end receive backpressure for free (critical when proxying a streaming upload to a slower upstream model).
- `prepend()` for composable layered parsers (header → body → multipart) without a shared ring buffer.
- Pooled-future event (`SharpEvent`-equivalent) for both the receive-wake and the write-backpressure signals — synchronous fast path, single shared future, no per-wait allocation.

### 9.2 Response model — typed, NOT exception-driven for success

Diverge from Blazeio here:
- A real `StreamingResponse` / `SSEResponse` object the handler **returns** (FastAPI ergonomics). The framework calls `prepare()` then drains.
- Keep Blazeio's **prepare-time writer selection**: choose `write_chunked` vs `write_sse` vs `write_raw` once from the response object's content-type/encoding, rebind the bound method, no per-token branch.
- Keep the **status+headers-flushed-before-body** behaviour (TTFB) — emit `HTTP/1.1 200` + headers synchronously the instant the streaming response starts, before the first token.
- `abort(status, detail)` / `HTTPException` as a *raised* exception caught in one central place — but ONLY for errors/early-exit, never success. Guarantee a `finally` that emits the chunked terminator + runs cleanup + resets pooled connection state (copy Blazeio's `finally`-block discipline exactly — it is the robust part).
- Handle `HEAD` with an explicit branch, not `raise Eof()`.
- A true case-insensitive header multidict so content-type/length are handled once (kill Blazeio's 3-casing hand-checks).

### 9.3 The token-streaming write path (the core)

```python
# Lancelot LLM streaming handler (target ergonomics)
async def chat(req) -> SSEResponse:
    async def tokens():
        async for delta in llm.stream(req.json["messages"]):
            yield {"choices":[{"delta":{"content": delta}}]}     # OpenAI shape
        yield "[DONE]"
    return SSEResponse(tokens())          # framework: prepare() flushes headers, then drains
```
Implementation rules derived from Blazeio:
1. **One socket-write chokepoint** equivalent to `writer()`: `await backpressure_gate(); transport.write(framed)`.
2. **Backpressure gate = boolean fast path** (Blazeio's `buffer_overflow_manager`): on the common case (client keeping up) it is a synchronous `if not over_hwm: return` — **no Future, no suspension per token**. Only when `pause_writing` fired does it `await overflow_event.wait_clear()`. This is the single biggest per-token win vs Starlette's `drain()`.
3. **SSE framing** `b"data: %b\n\n"` with dict→`json.dumps(...,separators=(",",":"))` (compact). Accept the generator directly and auto-emit `data: [DONE]\n\n` + close.
4. **Chunked framing** `b"%X\r\n%s\r\n"` for non-SSE token JSON; accept an `AsyncIterable`, auto-emit `0\r\n\r\n`.
5. **Adaptive coalescing** (the Blazeio `Batchedwriter` pattern, fixed): buffer tokens, flush when `buffered_bytes >= N` **OR** `now - last_flush >= T`. Defaults: `T = 25 ms`, `N = 2048 B`. This keeps wire/syscall efficiency at high token rates without hurting perceived interactivity (Blazeio's size-only 30 KiB is the bug to not repeat). Make `T`/`N` per-route tunable; `T=0` (flush every token) for ultra-low-latency endpoints.
6. **No per-token compression.** Disable Content-Encoding on token streams (or use a single streaming compressor context with periodic `Z_SYNC_FLUSH`, never independent per-token gzip). Blazeio's per-chunk `await encoder(data)` is wrong for streams — do not copy.
7. **Cancel-on-disconnect:** `connection_lost` → cancel the handler task → `CancelledError` propagates into `llm.stream()` → upstream LLM request is closed (stop paying for unreceived tokens). Provide a `shield()` async-CM (Blazeio's `ConnectionTaskShield`) for must-complete sections (billing, audit).
8. **End-to-end backpressure:** because the handler coroutine is the same one doing `async for tok in llm: await write(tok)`, a stalled `write` (slow client) stalls the `async for`, which (if `llm.stream` is itself an HTTP stream over the same pull-driven receive substrate) stops pulling from the model. Bounded server memory per stream regardless of completion length or client speed. This is the property to verify with a load test (slow-client + fast-model → server RSS must stay flat).

### 9.4 Static (follow Starlette, not Blazeio)

- Use `loop.sendfile()` / `os.sendfile` for non-range, non-encoded responses (kernel zero-copy). Blazeio lacks this; Starlette has it.
- ETag from `mtime+size` or content hash (not Blazeio's name+size-only).
- Range with correct inclusive `Content-Length = end-start+1`.
- ≥64 KiB read chunks, not 1 KiB.
- Optional in-RAM cache with a **global byte budget + real LRU**, gzip-once, mtime-invalidated — the Blazeio `IN_MEMORY_STATIC_CACHE` intent, done correctly and actually importable.

### 9.5 What to copy / fix / reject — one-glance table

| Subsystem | Copy verbatim | Copy w/ fix | Reject |
|---|---|---|---|
| Receive | BufferedProtocol + persistent buf + memoryview; pull-driven pause-by-default; `prepend`; pooled-future event | pause-threshold tunable (vs pause-always) | — |
| Body read | `r.pull` rebind by headers; `read_exactly` single-alloc; lazy memoized query | — | hand-rolled multipart; in-place header capitalization |
| Writer select | prepare-time bind of `r.write`; status+headers before body | typed Response object instead of dict+exception | — |
| Backpressure | boolean fast-path gate (no Future/token); end-to-end propagation; cancel-on-disconnect + shield CM | — | `drain()`-style always-Future (don't regress to it) |
| Token framing | SSE `data: %b\n\n`; chunked `%X\r\n%s\r\n`; accept AsyncIterable + auto-terminate | — | per-token independent gzip/br |
| Coalescing | the `Batchedwriter` swap-`r.write` pattern | **time+size flush** (25 ms / 2 KiB) not 30 KiB size-only | size-only trigger |
| Control flow | central error funnel + `finally`-guaranteed cleanup/eof/reset; `__slots__` exceptions for errors | `abort()`/`HTTPException` for errors only | success-via-raise; HEAD-via-`raise Eof()` |
| Static | conditional 304 logic; path-traversal guard; reuse backpressured write | ETag mtime+size; Range `+1`; ≥64 KiB chunks | shipping non-importable dead modules; 1 KiB chunk; sendfile-absence → ADD sendfile |
| Templating | "splice async-gen into streamed shell" pattern (for LLM-in-page) | — | `getattr(r, x)` template-driven attr walk (injection) |

---

## 10. Top 5 steals (ranked) + verdict recap

1. **BufferedProtocol zero-copy receive + pull-driven pause-by-default** (`Protocols/__init__.py:6-67`). Bounded per-connection memory, no per-packet `bytes` alloc, free end-to-end receive backpressure. The foundation.
2. **Boolean fast-path send-backpressure gate** (`server_protocol.py:64-72` + `Protocols/__init__.py:29-43`). No Future allocated per token when the client keeps up — the concrete per-token win over Starlette's `drain()`. The single most AI-relevant perf steal.
3. **Prepare-time writer selection + status/headers-before-body** (`server_protocol_extratools.py:306-341`). Best-in-class TTFB; zero per-token dispatch branch; native SSE/chunked framing that accepts the generator directly.
4. **Pooled-future `SharpEvent`** (`Dependencies/__init__.py:146-200`). Removes allocator/GC pressure from every wake under thousands of concurrent streams.
5. **`Batchedwriter` swap-`r.write` coalescing pattern** (`Modules/Server/writers.py:7-29`) — adopt the transparent-buffering CM, but replace the 30 KiB size-only trigger with a 25 ms / 2 KiB time-or-size trigger for interactive AI.

**Exception-control-flow verdict:** Blazeio conflates "exception" and "response" — `Abort.text(r)` *is* the response, caught in a central keep-alive funnel with a robust `finally`. The **funnel + guaranteed-cleanup `finally` is excellent and worth stealing for the error path.** The **success-path-via-raise and HEAD-via-`raise Eof()` are anti-patterns** (slower when actually raised, non-local control flow, untestable handlers, casing fragility from having no typed response). Lancelot: typed `return`ed responses for success, central raised `HTTPException` for errors only, explicit HEAD branch, real case-insensitive header type — keep Blazeio's `finally`-discipline.

**Static verdict:** Blazeio's weakest subsystem. Live `Simpleserve` is decent on conditional/range but uses a weak ETag, a 1 KiB chunk, an off-by-one range length, and **no sendfile**; two of three static modules are dead non-importable code. Follow Starlette's static design here, not Blazeio's — only borrow the conditional-request logic, path-traversal guard, and the fact that static reuses the backpressured write path.

---

## 11. End-to-end data-flow diagrams (KNOWN — traced from source)

### 11.1 Receive path (socket → handler-consumable chunk)

```
kernel socket recv
   │
   ▼
asyncio BufferedProtocol asks: get_buffer(sizehint)        Protocols/__init__.py:11-18
   │   returns app.__buff__memory__[:sizehint]   (SAME persistent mv, realloc only if sizehint>102400)
   ▼
kernel DMAs bytes directly into app.__buff__              (no intermediate bytes object)
   │
   ▼
buffer_updated(nbytes)                                     Protocols/__init__.py:6-9
   │   transport.pause_reading()          ← socket paused IMMEDIATELY
   │   __stream__.append(bytes(mv[:nbytes]))  ← exactly ONE copy out
   │   __evt__.set()                       ← wake a waiting consumer (SharpEvent)
   ▼
handler:  async for chunk in r.pull():                     r.pull bound at request.py:247-254
   │            (handle_raw / handle_chunked)
   ▼
__aiter__ → ensure_reading()                               Protocols/__init__.py:45-67
   │   if __stream__ empty AND not reading: transport.resume_reading()
   │   await __evt__.wait_clear()          ← suspend until buffer_updated fires
   │   while __stream__: yield popleft()
   ▼
handler consumes chunk;  loop repeats (socket re-paused on next buffer_updated)
```
Invariant: at most one kernel buffer of unconsumed data in userland; reading is paused unless a consumer is actively awaiting. **This is the receive-side backpressure guarantee.**

### 11.2 Send path (handler token → wire), the LLM-streaming hot loop

```
handler:  async for tok in llm.stream(): await r.write(tok)
   │   r.write was bound ONCE at prepare():                server_protocol_extratools.py:330-338
   │     Transfer-Encoding: chunked  → write_chunked
   │     Content-Type: text/event-stream → write_event_stream
   ▼
write_event_stream(tok)                                    server_protocol_extratools.py:213-222
   │   (encoder? usually None for streams)
   │   frame = b"data: %b\n\n" % tok        ← 8B framing, no length prefix
   ▼
writer(frame)                                              server_protocol.py:64-72
   │   await buffer_overflow_manager()                     Protocols/__init__.py:41-43
   │      if not __is_buffer_over_high_watermark__: return ← FAST PATH: sync, no Future
   │      else: await __overflow_evt__.wait_clear()        ← SLOW PATH: suspend till resume_writing()
   │   if not transport.is_closing(): transport.write(frame)  ← buffered non-blocking enqueue
   │   else: abort_connection() → ClientDisconnected → task cancelled
   ▼
asyncio transport buffers; when over high-water → pause_writing() → __is_buffer_over_high_watermark__=True
   when drained < low-water  → resume_writing() → __overflow_evt__.set()  → handler resumes
   ▼
kernel send(2);  client receives `data: <tok>\n\n`
   │
(loop)  next token...   on generator exhaustion: keep-alive finally → r.eof() → (chunked) 0\r\n\r\n
   client disconnect mid-stream → connection_lost → app.cancel() → handler task cancelled → llm.stream() closed
```
Hot-path cost per token (client keeping up): 1 `isinstance`/prefix check + 1 bytes-format + 3 attribute reads + 1 boolean test (backpressure early-return, **no Future**) + 1 `transport.write`. No suspension, no allocation beyond the framed bytes. **This is the number Lancelot targets.**

### 11.3 Control-flow funnel (every request outcome)

```
keep-alive loop  (while not transport.is_closing())        server_protocol.py:96-121
   try:
       r += keepalive_headers ; await handler(r)
   except Abort as e:        await e.text(r)        ← error/404/304/413/416 RESPONSE emitted here
   except (Eof, ServerGotInTrouble, BrokenPipeError): pass  ← HEAD / intentional end / pipe gone
   except (Cancelled, KbdInt, CloseConnection, ClientDisconnected, ServerDisconnected): raise  ← teardown
   except Exception as e:    exc = e               ← unexpected → 500 below
   finally:
       if r.__is_prepared__: await r.eof()         ← emit chunked terminator if streaming
       else: await Abort("Server Got In Trouble",500).text(r)
       if after_middleware: await after_middleware(r)
       if exc: traceback_logger(exc)
       ServerProtocolEssentials.reset(r)           ← zero fields, REUSE protocol for next keep-alive req
```
The `finally` is the genuinely robust part: eof/cleanup/reset run on **every** exit path (success, abort, crash, disconnect). Copy this discipline; reject success-via-`raise`.

---

## 12. Annotated Lancelot reference skeleton (DESIGNED — reimplementable)

This is the concrete starting code, distilling §9 into something runnable. Pure-asyncio, no Blazeio dependency. Labelled where each idea is sourced.

```python
# lancelot/transport.py  — receive substrate (steals: Protocols/__init__.py:6-67)
import asyncio
from collections import deque

class PooledEvent:                              # ← SharpEvent  (Dependencies/__init__.py:146-200)
    __slots__ = ("_set", "_fut", "_loop")
    def __init__(self, loop): self._set=False; self._fut=None; self._loop=loop
    def set(self):
        self._set=True
        if self._fut is not None and not self._fut.done():
            self._fut.set_result(True)
        self._fut=None
    def clear(self): self._set=False
    async def wait_clear(self):
        if self._set: self._set=False; return True
        if self._fut is None: self._fut=self._loop.create_future()
        await self._fut                          # single shared future, reused
        self._set=False; return True

class Conn(asyncio.BufferedProtocol):
    BUF = 102400                                 # ← INBOUND_CHUNK_SIZE
    def __init__(self, loop, handler):
        self._buf = bytearray(self.BUF)
        self._mv  = memoryview(self._buf)        # ← cached memoryview, reused every get_buffer
        self._q   = deque()
        self._loop = loop
        self._recv_evt = PooledEvent(loop)
        self._wbp_evt  = PooledEvent(loop)       # write-backpressure
        self._over_hwm = False
        self._handler = handler
        self._task = None
    # --- receive (zero-copy) ---
    def get_buffer(self, hint):
        if hint > len(self._mv):
            self._buf = bytearray(hint); self._mv = memoryview(self._buf)
        return self._mv if hint <= 0 else self._mv[:hint]
    def buffer_updated(self, n):
        self.transport.pause_reading()           # ← pause-by-default
        self._q.append(bytes(self._mv[:n]))      # ← exactly one copy out
        self._recv_evt.set()
    def eof_received(self): self._recv_evt.set()
    def connection_made(self, t):
        self.transport=t; t.pause_reading()
        self._task=self._loop.create_task(self._run())
        self._task._conn=self
    def connection_lost(self, exc):
        self._recv_evt.set(); self._wbp_evt.set()
        if self._task and not self._task.done(): self._task.cancel()   # ← cancel-on-disconnect
    # --- send backpressure (boolean fast path) ---
    def pause_writing(self): self._over_hwm=True
    def resume_writing(self): self._over_hwm=False; self._wbp_evt.set()
    async def _drain_gate(self):
        if not self._over_hwm: return            # ← FAST PATH: sync, no future per token
        await self._wbp_evt.wait_clear()         # ← SLOW PATH only when client is behind
    # --- pull-driven request stream ---
    async def stream(self):
        while True:
            if not self._q:
                if not self.transport.is_reading(): self.transport.resume_reading()
                await self._recv_evt.wait_clear()
                if self.transport.is_closing(): raise ConnectionResetError
            while self._q: yield self._q.popleft()
    def prepend(self, data):                     # ← un-read primitive
        if self.transport.is_reading(): self.transport.pause_reading()
        self._q.appendleft(data); self._recv_evt.set()
    # --- write chokepoint ---
    async def _write(self, framed: bytes):
        await self._drain_gate()
        if self.transport.is_closing(): raise ConnectionResetError
        self.transport.write(framed)
```

```python
# lancelot/responses.py  — typed responses (fixes Blazeio's exception-as-response)
import json, time

class SSEResponse:                               # OpenAI-compatible token streaming
    def __init__(self, agen, *, status=200, headers=None,
                 coalesce_ms=25, coalesce_bytes=2048):   # ← Batchedwriter pattern, FIXED trigger
        self.agen, self.status = agen, status
        self.headers = {"Content-Type":"text/event-stream",
                        "Cache-Control":"no-cache","Connection":"keep-alive",
                        **(headers or {})}
        self.cms, self.cb = coalesce_ms/1000.0, coalesce_bytes
    async def __call__(self, conn):
        # status + headers flushed BEFORE body  ← Blazeio TTFB win (extratools:313)
        head = (f"HTTP/1.1 {self.status} OK\r\n" +
                "".join(f"{k}: {v}\r\n" for k,v in self.headers.items()) + "\r\n").encode()
        await conn._write(head)
        buf = bytearray(); last = time.monotonic()
        async for item in self.agen:
            if item == "[DONE]":
                if buf: await conn._write(bytes(buf)); buf.clear()
                await conn._write(b"data: [DONE]\n\n"); break
            payload = item if isinstance(item, (bytes,bytearray)) else \
                      json.dumps(item, separators=(",",":")).encode()
            buf += b"data: " + payload + b"\n\n"             # ← SSE framing, 8B (extratools:213)
            now = time.monotonic()
            if len(buf) >= self.cb or (now-last) >= self.cms:  # ← time-OR-size flush (the FIX)
                await conn._write(bytes(buf)); buf.clear(); last = now
        if buf: await conn._write(bytes(buf))

class ChunkedResponse:                           # Transfer-Encoding: chunked, accepts AsyncIterable
    def __init__(self, agen, *, status=200, headers=None):
        self.agen, self.status = agen, status
        self.headers = {"Transfer-Encoding":"chunked", **(headers or {})}
    async def __call__(self, conn):
        head = (f"HTTP/1.1 {self.status} OK\r\n" +
                "".join(f"{k}: {v}\r\n" for k,v in self.headers.items())+"\r\n").encode()
        await conn._write(head)
        async for ch in self.agen:
            if isinstance(ch, str): ch = ch.encode()
            await conn._write(b"%X\r\n%s\r\n" % (len(ch), ch))   # ← extratools:204,207
        await conn._write(b"0\r\n\r\n")          # ← terminator (extratools:161)

class HTTPException(Exception):                  # ERRORS ONLY (not success)  ← Abort, scoped down
    __slots__=("status","detail","headers")
    def __init__(self, status, detail="", headers=None):
        self.status, self.detail, self.headers = status, detail, headers or {}
```

```python
# lancelot/loop.py  — the keep-alive funnel (steals Blazeio's finally-discipline)
async def serve_request(conn, handler):
    prepared = False
    try:
        resp = await handler(conn)               # handler RETURNS a response (not raise)
        if resp is not None:
            prepared = True
            await resp(conn)                     # SSEResponse/ChunkedResponse/...
    except HTTPException as e:                    # ← Abort funnel, errors only
        body = e.detail.encode() if isinstance(e.detail,str) else e.detail
        hdrs = {"Content-Length":str(len(body)), **e.headers}
        head = (f"HTTP/1.1 {e.status} -\r\n" +
                "".join(f"{k}: {v}\r\n" for k,v in hdrs.items())+"\r\n").encode()
        await conn._write(head+body)
    except (asyncio.CancelledError, ConnectionResetError):
        raise                                    # client gone → teardown (cancels upstream LLM)
    except Exception:
        if not prepared:
            await conn._write(b"HTTP/1.1 500 -\r\nContent-Length: 0\r\n\r\n")
        # log...
    finally:
        # ← Blazeio's robust bit: cleanup ALWAYS runs (reset pooled conn state, etc.)
        pass
```

Notes on the skeleton vs Blazeio:
- `PooledEvent` is a minimal `SharpEvent`: single reused future, sync fast path. (Blazeio's also supports multi-waiter + done-callbacks; add only if a signal can have >1 waiter — the receive/backpressure events have exactly one.)
- The handler **returns** `SSEResponse(tokens())`; `HTTPException` is *only* raised for errors and caught centrally. This is the §5.5 verdict in code.
- `coalesce_ms=25, coalesce_bytes=2048` is the §3.5(e)/§9.3(5) fix to Blazeio's 30 KiB size-only `Batchedwriter`.
- `_drain_gate` is the literal §3.2 win: synchronous early return when the client keeps up, suspend only when `pause_writing` fired. No Future per token on the hot path.
- Cancel-on-disconnect (`connection_lost`→`task.cancel()`) propagates `CancelledError` into `tokens()` → into `llm.stream()`, closing the upstream model request (stop paying for unseen tokens) — §3.6.

This skeleton is the deliverable's reimplementable core; flesh out HTTP request-line/header parsing (Blazeio's `HTTPParser`, §1.2), routing, and static (`loop.sendfile`, §9.4) around it.

---

## 13. Open questions / things to verify against the live product (CALIBRATE)

1. **Per-token wall-clock overhead** — micro-benchmark `_drain_gate` fast path vs Starlette's `StreamingResponse`+uvicorn `drain()` at 1k/5k tokens/sec, single + 1k concurrent streams. Confirm the "no Future per token" claim translates to measurable RSS/CPU/latency wins. (INFERRED here from source; needs a number.)
2. **End-to-end backpressure under slow client** — slow-client + fast-model load test; assert server RSS stays flat (bounded by transport HWM) regardless of completion length. This is the safety property; verify it empirically.
3. **`pause_reading()` per `buffer_updated` cost** — measure whether pause-always vs pause-on-threshold matters for many small chunks (Blazeio pauses every chunk; §2.4 nuance).
4. **Coalescing default `T`** — A/B 0 ms / 25 ms / 50 ms for perceived chat latency vs syscall count; pick Lancelot's default empirically (Blazeio gives no guidance — its constant is wrong for chat).
5. **uvloop interaction** — Blazeio enables uvloop opportunistically (`Dependencies/__init__.py:495-498`); confirm `BufferedProtocol.get_buffer`/`buffer_updated` zero-copy semantics hold under uvloop (they do for asyncio; uvloop implements the buffered protocol too, but verify the memoryview reuse isn't defeated).
6. **Brotli/gzip on streams** — confirm the §3.5(f) recommendation (no per-token compression) by measuring per-token gzip cost + compression ratio on real LLM token sizes.

---

*End of RE-02. All `path:` references are relative to `/Users/seventyleven/Desktop/researchfms/blazeio/Blazeio/Blazeio/`. KNOWN = read from source; INFERRED = deduced from source behaviour; DESIGNED = Lancelot recommendation.*
