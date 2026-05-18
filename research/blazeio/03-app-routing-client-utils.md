# Blazeio → Lancelot: App Object, Routing, Custom Async Primitives, Async HTTP Client, C-Extension Strategy

Source-level RE pattern extraction for **Lancelot** (the user's "faster FastAPI" Python web/AI framework). Every claim cites `path:line` against the local Blazeio tree at `/Users/seventyleven/Desktop/researchfms/blazeio/Blazeio/Blazeio/`. Labels: **KNOWN** = read directly in source; **INFERRED** = deduced from mechanism, not stated; **DESIGN** = recommendation for Lancelot.

This file is the deep companion to `teardowns/BLAZEIO.md` (1132 lines) — it does **not** repeat the zero-copy receive path or the writer taxonomy except where the client reuses them; it goes deeper on the **App/dispatch object model**, the **`reFuture`/`reTask` custom scheduler**, the **`alts.py` stdlib-replacement layer**, the **async HTTP client as the AI outbound hot path**, and the **C-extension build/fallback strategy**.

---

## 0. Executive summary (the three load-bearing tricks)

1. **`reFuture` is a self-recycling Future (`Other/_refuture.py:9-98`).** Vanilla `asyncio.Future` is single-shot: resolve once, allocate a new one for the next await. Blazeio's `reFuture` has a `_mortality` flag (`_INFINITE`/`_FINITE`); an `_INFINITE` future, after `flush()`, calls `loop.call_soon(self.restart)` which resets `_state` back to `_PENDING` and reuses the **same object** for the next wait cycle. For a long-lived connection that awaits "data available" thousands of times, this removes thousands of `Future` allocations + GC pressure. `reTask` (`:100-148`) is a hand-rolled `Task` (≈40 lines vs CPython's ~300) driving a coroutine via `send`/`throw` with a 2-callback step machine.

2. **The async client is a per-host connection pool of *reusable* `Session` objects with a buffered, zero-copy streaming protocol (`Client/__init__.py` + `Protocols/client_protocol*.py`).** `__SessionPool__` (`Client/__init__.py:456-532`) keys connections by `(host, port, *proxy)`, caps "contexts" per host (`max_contexts`, default 2), reaps idle connections on a background `ReMonitor` timer (`keepalive_interval=30s`, `timeout=60s`), and hands each waiter a `Session` guarded by an `ioCondition` (custom condition variable). The protocol uses `BufferedProtocol.get_buffer`/`buffer_updated` (one preallocated `bytearray`, no per-read allocation) and `pause_reading()`-after-every-chunk backpressure. Response bodies stream via an `async for` over a `deque` of `bytes` slices, with `handle_chunked`/`handle_raw` decoders and gzip/brotli streaming decompressors. **This is the exact shape Lancelot needs for high-throughput concurrent LLM streaming.**

3. **Hot string/byte ops are C extensions with a transparent pure-Python fallback (`Extensions/*.c`, wired in `Dependencies/__init__.py:134-144`).** Five `-O3` C modules: `gen_payload` (build the HTTP request bytes), `url_to_host` (URL → (host,port,path)), `url_decode_sync`/`url_encode_sync`/`get_params_sync` (percent-coding + query parse), `headers_to_http_bytes`, and a `SimpleServe` static type. `c_extension_importer()` tries `import`, on `ImportError` silently continues; `client_protocol_tools.py:339-347` then patches `ioConf.url_to_host`/`ioConf.gen_payload` to pure-Python equivalents and **logs the exact slowdown factor** ("3.0X slower", "6.6X slower"). The framework runs identically with or without a C compiler.

Supporting cast: `SharpEvent` (a one-shot/auto-clear event built on a *single shared* `loop.create_future()`), `ioCondition` (a lock+condition built from `SharpEvent`s with explicit waiter accounting), `__Scope__` (a global service registry that mangles keys onto `main_thread`), the `App` 7-base MRO with `__getattr__`-driven lazy attributes, an O(1) dict route table + O(k) wildcard fallback, and a threaded off-loop logger.

The rest of this document is the reimplementable detail.

---

## 1. The App object & route dispatch (KNOWN — `__init__.py`)

### 1.1 The 7-base MRO and why it matters

`class App(Handler, Rproxy, Server, Taskmng, Deprecated, Callbacks, Protocol_methods)` (`__init__.py:657`). The App is a **mixin tower**, not a god-class. Each base owns one concern:

| Base | File:line | Concern |
|---|---|---|
| `Handler` | `:92-232` | request entrypoint, route serving, middleware dispatch, exception boundary, default parser |
| `Rproxy` | `:365-375` | optional reverse-proxy registration (`bind_to_proxy`) |
| `Server` | `:402-601` | event loop runner, socket setup, TLS, graceful shutdown, `serve_forever` |
| `Taskmng` | `:350-363` | `create_task` tracking + cancel-on-exit |
| `Deprecated` | `:338-348` | legacy `init`/`init_sync` shims (no-op constructors) |
| `Callbacks` | `:603-637` | `on_start`/`on_run` callback registry + runner |
| `Protocol_methods` | `:639-655` | `__getattr__` lazy attributes (`route`, `event_loop`), `__await__` = "wait until server running" |

`Super(app).__init__()` (`:686`, `Super` from `Itools/__init__.py:170-191`) is a **custom cooperative super-init**: it grabs `app.__class__.__mro__[1:]` and calls each base's `__init__(app)` explicitly in MRO order. Blazeio does **not** use Python's `super().__init__()` chain — it manually iterates the MRO. INFERRED reason: every base uses single-arg `__init__(app)` (the instance is named `app`, not `self`, throughout the codebase) and the explicit loop avoids cooperative-`super()` argument-threading bugs across 7 unrelated bases.

**Steal for Lancelot:** the concern-per-mixin split is clean and testable. **Avoid:** the manual MRO walk — it breaks if any base needs constructor args, and it's a foot-gun vs standard `super()`. Use standard `super().__init__()` with `**kwargs` pass-through, or composition (an `App` that *holds* a `Router`, `Lifecycle`, `TaskManager`) instead of a 7-deep inheritance tower.

### 1.2 The route table — a plain `OrderedDict`, O(1) exact match

`app.declared_routes = kwargs.get("declared_routes") or OrderedDict()` (`:665`). **There is no trie and no regex.** Routing is a single dict lookup:

```python
# serve_route_no_middleware (:181-191), the fast path
if route := app.declared_routes.get(r): await route.get("func")(r)        # r.__hash__/__eq__ → r.path
elif app.__secondary_handler__ and (route := app.__secondary_handler__(r.path)):
    await route.get("func")(r)
else:
    raise Abort("Not Found", 404)
```

Note `app.declared_routes.get(r)` passes the **protocol object `r`**, not `r.path` — the `BlazeioServerProtocol` is hashed; INFERRED it falls through to default object identity unless `r.path` is keyed (the with-middleware path at `:166` explicitly uses `app.declared_routes.get(r.path)`, which is the string key). The route value is a dict `{"func", "params", "len", "route_name"}` built in `add_route` (`:748-753`).

`add_route` (`:721-770`):
- Resolves route name from the function name (`parent_func.__name__`) or an annotation-default named `route` (`:737-740`).
- **Funcname normalization** (`:715-719`, table at `Dependencies/__init__.py:78-81`): `__dot__` → `.`, `_` → `/`. So a handler `def api_v2_users(r)` registers as route `/api/v2/users`. A handler named `index` → `/index`. This is the "routes are Python identifiers" trick — no decorator path string needed (though `Add_Route` decorator at `:254-291` also supports explicit `path=`).
- Names ending `_middleware` are special-cased: `before_middleware`, `after_middleware`, `handle_all_middleware` become reserved keys (`:736-746`).
- **Duplicate route name → `Multirouter`** (`Itools/__init__.py:83-102`): if a name is registered twice, the entry is replaced by a `Multirouter` that stores a list and, at `route_validator()` time (`:772-776`, called once in `server_run`), collapses to a single `router` coroutine that awaits every handler in sequence and returns the last result. This is a fan-out/chain primitive baked into registration.

**Why fast:** O(1) dict `get` per request, zero per-request allocation in the match itself, no regex compilation, no path-segment splitting on the hot path. **Compare to FastAPI/Starlette:** Starlette walks an ordered `list[Route]` and calls `route.matches(scope)` on each — every route does a compiled-regex `path_regex.match()` until first hit (O(n) routes × regex). FastAPI adds Pydantic validation per request on top. Blazeio's exact-match dict is dramatically cheaper for static paths but **has no native path parameters** (`/users/{id}`) — that's what the wildcard fallback approximates. **Compare to Robyn:** Robyn's router is Rust (matchit/radix trie) — O(k) in path length with real path params, faster than Starlette and supports `/users/:id`. Blazeio's pure-dict is faster than Starlette for static routes but **less capable** than Robyn for parameterized routes.

### 1.3 Wildcard fallback — O(k) suffix-trim, the `{id}` substitute

`WildcardRouteMatcher` (`Modules/server_utilities.py:5-26`), enabled via `app.enable_wrm()` (`__init__.py:123-125`), set as `app.__secondary_handler__`. Algorithm (`:19-26`):

```python
def resolve(app, r):
    ref = r.path
    del_count = 0
    while (del_count := del_count + 1) <= app.max_del_count and (idx := ref.rfind("/")) != -1 and (ref := ref[:idx]):
        if (route := app.web.declared_routes.get(ref) or app.web.declared_routes.get(ref + "/")):
            if app.urp: r.path = route["route_name"]
            return route
```

For `/api/v2/123/comments`, it tries `/api/v2/123` → `/api/v2` → `/api` (rfind + slice, max 20 iterations). First registered prefix wins. **This is the "no path params, just register the prefix and parse the tail yourself" model.** The handler reads `r.path`/`r.tail` (the C `get_params_sync` parses `?query`). O(k) where k = number of path segments, capped at `max_del_count=20`. Docstring claims "O(1)" — that's marketing; it's O(segments) with a worst-case 20-iteration dict-probe. Still cheap.

**Steal for Lancelot:** the dict-first / prefix-fallback two-tier is a good default — exact routes are free, dynamic routes degrade gracefully. **But Lancelot should add real path params** (radix/trie like Robyn's matchit, or Starlette's convertor regex compiled once at startup) — the prefix-trim model pushes URL parsing into every handler, which is the opposite of "faster FastAPI" ergonomics. **DESIGN: compile a hybrid — exact dict for static routes (the 90% case, O(1)), a compiled segment-trie for `{param}` routes (O(k)), checked only on dict miss.** Keep Blazeio's "function name → route" sugar as an *option*, not the only mechanism.

### 1.4 The request entrypoint and exception boundary

`handle_client` (`:207-232`) is the per-connection coroutine (created in `BlazeioServerProtocol.connection_made` → `transporter` → `on_client_connected` = `handle_client`). Flow:
1. `r.ip_host, r.ip_port = r.transport.get_extra_info('peername')`
2. optional request counting/timing
3. `await app.__main_handler__(r)` — the configurable dispatch root
4. **One exception boundary for the whole request** (`:218-229`):
   - `Abort` → `await e.text(r)` (writes the HTTP error response — `Abort` *is* a response, see §6)
   - `(Err, ServerGotInTrouble)` → `traceback_logger`
   - `(ClientDisconnected, Eof, ServerDisconnected, CloseConnection)` → swallow silently (normal lifecycle, not errors)
   - `KeyboardInterrupt` → re-raise
   - `(ConnectionResetError, BrokenPipeError, CancelledError)` → swallow
   - bare `Exception` → `traceback_logger(e, frame=4)`

`__main_handler__` is selected in `configure_server_handler` (`:127-140`): if no middleware registered → `serve_route_no_middleware` (the fast path, `:181-191`); else `serve_route_with_middleware` (`:158-179`). `__main_handler__` defaults to `__default_handler__` defaults to whichever of those two. This is **pipeline specialization at startup** — the no-middleware path has zero middleware branches in the hot loop.

**Steal:** (a) the single try/except boundary that classifies disconnects as *non-errors* (don't log a stack trace when a client hangs up — Blazeio explicitly swallows `ClientDisconnected`/`BrokenPipeError`); (b) **startup specialization** — pick the dispatch function once based on registered middleware, don't branch per-request. **Avoid:** exceptions-as-responses (`Abort` raised then `.text(r)` writes the body) is clever but couples control flow to I/O; for Lancelot prefer returning a response object and letting the framework write it (testable without a transport). Blazeio's model makes unit-testing a handler require a fake protocol.

### 1.5 The default body parser

`parse_default` (`:142-156`): reads from the protocol (`async for chunk`) into a `bytearray` until `\r\n\r\n` (`MinParsers.server.network_config.http.one_point_one.dcrlf`), bounded by `__http_request_max_buff_size__ = 102400` bytes (`Dependencies/__init__.py:74`) → 413 if exceeded, 400 if connection closes mid-headers. Then `MinParsers.server.parse(r, buff)` splits headers/body and any leftover body bytes are pushed back with `r.prepend(body)` (the zero-copy un-read, `Protocols/__init__.py:36-39`). The parser (`Parsers/min_http_parser.py:36-97`) is hand-rolled `bytes.find()` slicing — no regex, decode only the header key/value spans. Header keys are `.capitalize()`-normalized (`min_http_parser.py:42`); the client parser lowercases them (`:105`).

**Steal:** prepend/un-read of pipelined body bytes via a `deque.appendleft` is the right primitive for keep-alive + request pipelining. `find()`-slice parsing with lazy decode (only decode the spans you keep) beats regex and beats `.split()` (no list allocation).

---

## 2. Custom async primitives — the `reFuture`/`reTask` scheduler & `SharpEvent`

### 2.1 `reFuture` — the self-recycling Future (`Other/_refuture.py:9-98`)

The single most novel primitive. `__slots__`-based (`:10`), 8 slots, no `__dict__`. The mechanism:

```python
_PENDING, _CANCELLED, _FINISHED = "_PENDING", "_CANCELLED", "_FINISHED"
_INFINITE, _FINITE = "_INFINITE", "_FINITE"          # the mortality axis

class reFuture:
    __slots__ = ("_loop","_sleepers","_result","_state","_mortality","_exception","_asyncio_future_blocking","_cancel_message")
    def __init__(app, loop=None, mortality=_INFINITE):
        app._sleepers = deque()                       # waiter callbacks
        app._state = _PENDING; app._mortality = mortality
    def __await__(app):
        if not app.done():
            app._asyncio_future_blocking = True       # the magic attr asyncio's Task looks for
            yield app                                 # yield SELF to the running Task
        if not app.done(): raise RuntimeError("await wasn't used with future")
        return app.result()
    def set_result(app, result):
        if app._state == _CANCELLED: raise app.exception()
        elif app._state == _FINISHED: return
        app._state = _FINISHED; app._result = result; app.flush()
    def flush(app):
        app.awake()                                   # fire all done-callbacks via call_soon
        if app.is_infinite(): app._loop.call_soon(app.restart)   # ← THE TRICK
    def restart(app):
        if app._state == _PENDING: return app
        app._result, app._state, app._asyncio_future_blocking = None, _PENDING, False
        return app
```

**Why it's faster:** A vanilla `asyncio.Future` is **single-use**. Code that waits on the same logical event N times (e.g. "more bytes arrived on this socket" — happens once per TCP segment, thousands of times per large body) must allocate N `Future` objects, each with its own callback list, each GC'd. `reFuture` with `_mortality=_INFINITE` resets itself: after delivering a result and waking waiters, `flush()` schedules `restart()` which flips `_state` back to `_PENDING` and reuses the **same object, same `_sleepers` deque**. The hot loop allocates **one** future for the connection's entire lifetime instead of one per chunk. This kills allocation churn and GC pauses in the streaming path. The `_asyncio_future_blocking = True` + `yield app` pattern is exactly what makes a custom object awaitable by *any* asyncio Task (CPython's `Task.__step` checks `result._asyncio_future_blocking`).

`exception()`/`result()`/`add_done_callback`/`cancel`/`set_exception` mirror the `asyncio.Future` API surface (`:18-72`) so it's a drop-in. `set_exception` reproduces CPython's `StopIteration`-into-future guard (`:27-31`).

### 2.2 `reTask` — a ~40-line Task (`Other/_refuture.py:100-148`)

`reTask(reFuture)` is a hand-rolled Task. CPython's `tasks.py` Task is ~300 lines with name generation, context, eager-start, step-guarding, future-blocking checks, `__del__` warnings. `reTask`:

```python
class reTask(reFuture):
    __slots__ = ("_coro","__taskpool_timer_handle__","__taskpool_callbacks__")
    def __init__(app, coro, *a, **k):
        app._coro = coro
        super().__init__(*a, mortality=_FINITE, **k)   # a Task is FINITE (resolves once)
        app._loop.call_soon(app._step)                 # kick the first step
    def _step_run_and_handle_result(app, exc):
        try:
            result = app._coro.send(None) if exc is None else app._coro.throw(exc)
        except StopIteration as exc:
            app.set_result(exc.value) if not app.cancelled() else app.cancel(exc.value)
        except (CancelledError, BaseException) as e:
            app.set_exception(e)
        else:
            if result and result is not app:
                result.add_done_callback(app._wakeup)  # chain on the awaited future
            else:
                app.set_result(result)
    def _wakeup(app, fut):
        try: fut.result()
        except BaseException as e: app.set_exception(e)
        else: app._step()
        app = None                                     # break ref cycle for GC
```

It's a minimal trampoline: `send(None)` → if the coro yields a future, attach `_wakeup` as its done-callback; when that fires, `result()` (propagate exception) then `_step()` again; on `StopIteration` capture `.value` as the result. `tempFut(result)` (`:150-153`) is a pre-resolved future helper. `app = None` at the end of `_step`/`_wakeup` (`:116, 148`) is a deliberate **ref-cycle break** so the task GC's immediately rather than waiting for the cycle collector.

**Why faster:** no name allocation, no `contextvars` copy, no `_must_cancel`/`_fut_waiter` bookkeeping, no `__del__` finalizer. For spawning thousands of short-lived per-request tasks, the per-task overhead is a fraction of CPython's Task. **Cost:** no `contextvars` propagation (Blazeio uses `Taskscope` keyed on `current_task` instead, see §3.3), no introspection (`asyncio.all_tasks()` won't see `reTask`s — note `ReMonitor.analyze_protocols` at `Dependencies/__init__.py:663-670` iterates `all_tasks(loop)`, so reTasks would be invisible to the watchdog — INFERRED limitation), weaker cancellation semantics (`cancel()` only works while `_PENDING`, `:66`).

**Steal for Lancelot — selectively.** The self-recycling future for the **single hottest wait** (the "bytes available" event on a streaming connection) is a genuine, measurable win for LLM streaming where you `await` per-token-chunk thousands of times per request. **Do NOT replace `asyncio.Task` wholesale** — you lose `contextvars` (needed for request-scoped tracing/auth context in an AI backend), structured cancellation, and debugging. **DESIGN: keep `asyncio.Task`; add one recyclable awaitable (`StreamEvent`) for the byte-ready signal.** That captures ~90% of the allocation win at ~0% of the ecosystem cost. Benchmark it against `asyncio.Event` before committing — on uvloop the gap may be small.

### 2.3 `SharpEvent` — the production event primitive (`Dependencies/__init__.py:146-200`)

This, not `reFuture`, is what's actually wired into the server/client protocols (`__evt__`, `__overflow_evt__`, `__wait_closed__`). `__slots__ = ("_set","_waiters","loop","auto_clear")`.

```python
class SharpEvent:
    def get_fut(app):
        if app._waiters: return app._waiters[0]                  # ONE shared future for all waiters
        app._waiters.append(fut := app.loop.create_future())
        fut.__is_cleared__ = False
        fut.__acallbacks__ = [app.fut_done]
        fut.add_done_callback(app.done_callback_orchestrator)
        return fut
    async def wait(app):
        if app._set: return True                                 # fast path: already set, no await
        return await app.get_fut()
    def set(app, item=True):
        app._set = True
        if len(app._waiters) == 1:
            if not app._waiters[0].done(): app._waiters[0].set_result(item)
        else:
            for fut in app._waiters:
                if not fut.done(): fut.set_result(item)
        app._waiters.clear()
    async def wait_clear(app):                                   # the common idiom
        result = await app.wait(); app.clear(); return result
```

Versus `asyncio.Event`: (1) **one shared future for ALL concurrent waiters** instead of one future per waiter (Blazeio's protocol typically has exactly one reader awaiting `__evt__`, so this is one future, period). (2) **`wait()` fast-paths when already set** — returns `True` without creating a future or suspending. (3) `auto_clear`/`wait_clear` fuses wait+reset into one call (the protocol does `await app.__evt__.wait_clear()` every read cycle — see `Protocols/__init__.py:50`). (4) `__slots__`, no `_value`/`_waiters` deque churn. The `SharpEventLab` variant in `alts.py:50-101` is identical with `auto_clear=True` default.

**Steal:** the "single shared future + already-set fast path + fused wait_clear" event is strictly better than `asyncio.Event` for the 1-waiter socket-readiness case and is **safe** (no Task replacement). Lancelot should use this exact pattern for its connection read-ready signal. This is the lowest-risk highest-value primitive in the whole codebase.

### 2.4 `ioCondition` — lock + condition from SharpEvents (`alts.py:103-191`)

A condition variable used by the connection pool (`Client/__init__.py:490, 523`). `__slots__` of 9. Composed entirely of `SharpEvent`s: `event`, `_lock_event`, `_waiter_event`, `_acquire_event` + integer counters `notify_count`, `waiter_count`, `acquire_waiter_count`, bool `is_locked`, `initial`. `acquire()` (`:141-149`) spins on `_lock_event.wait_clear()` while locked; `wait()` (`:151-174`) releases the lock, increments `waiter_count`, awaits `event`, and on wake decrements `notify_count` (so `notify(n)` wakes exactly n waiters — explicit count-based wakeup, not asyncio.Condition's notify-then-reacquire dance). `wait_for(predicate)` (`:176-180`) loops `wait()` until predicate true. `on_waiter_change`/`on_acquire_change` expose waiter-count change events — used by the pool to pick the least-contended connection.

**Why:** asyncio.Condition wraps a Lock and a deque of futures; every `notify` reschedules. Blazeio's version uses integer counters + a shared event so `notify(n)` is O(1) flag-set, and exposes `waiter_count` so the pool can do **least-connections load balancing** (`Client/__init__.py:520-521`: `waiters = [i.context.waiter_count for i in instances]; instance = instances[waiters.index(min(waiters))]`). That introspection is impossible with stdlib `asyncio.Condition`. **Steal:** if Lancelot pools connections, exposing live `waiter_count` per connection for least-loaded selection is exactly right for spreading concurrent LLM calls across a connection set.

---

## 3. `Dependencies/__init__.py` + `alts.py` — the stdlib-replacement / monkeypatch layer

`alts.py` ("alternatives") is the misc toolbelt: `ioCondition`, `SharpEventLab`, `Asynchronizer`, `TaskPool`, `Ehandler`, `URL`, `plog`/`llog`, `traceback_logger`, `Coro`, file helpers. The *actual* stdlib swaps happen in `Dependencies/__init__.py`. There is **no aggressive monkeypatching of asyncio internals** (no `loop.create_future` override, no `asyncio.Future` replacement at import) — the speedups are *opt-in via composition* (`SharpEvent`, `reFuture`) plus three real swaps:

### 3.1 JSON: ujson preferred, json fallback (`Dependencies/__init__.py:46-50`)

```python
try:
    from ujson import dumps as ujson_dumps, loads, JSONDecodeError
    dumps = lambda *args, indent=4, reject_bytes=False, escape_forward_slashes=False, **kwargs: ujson_dumps(*args, indent=indent, reject_bytes=reject_bytes, escape_forward_slashes=escape_forward_slashes, **kwargs)
except ImportError:
    from json import dumps, loads, JSONDecodeError
```

`ujson` is the JSON codec project-wide (request bodies, log serialization, response `Deliver.json`). `ujson.dumps` is roughly 2–4× `json.dumps` for typical payloads; `loads` similar. The wrapper normalizes ujson's kwargs (`escape_forward_slashes=False` to match `json` output, `reject_bytes=False`). **Fallback is transparent** — `dumps`/`loads` are module-level names; nothing else in the codebase knows which backend won. No measured factor logged here (unlike the C exts), but for an AI backend that serializes large JSON request/response bodies per LLM call, the codec choice is material. **DESIGN for Lancelot: default to `orjson` (faster than ujson, returns `bytes` directly — avoids the `.encode()` Blazeio does everywhere e.g. `Client/__init__.py:335` `dumps(json,indent=0).encode()`), fall back to stdlib `json`.** `orjson.dumps` already returns bytes — wire the response path to accept bytes and skip an encode + copy.

### 3.2 Event loop: uvloop preferred, asyncio fallback (`Dependencies/__init__.py:480-498`)

```python
def optional_module_importer(arg, optional_modules=("uvloop",)):
    for if_, else_ in arg:
        try: exec(if_, globals())
        except Exception as e:
            ...  # if it's a ModuleNotFoundError for an *optional* module, stay silent
            if else_ == NotImplemented: raise Err("A required package is not installed.")
            exec(else_, globals())

optional_module_importer({
    ("from uvloop import EventLoopPolicy as uvloop_EventLoopPolicy\nset_event_loop_policy(uvloop_EventLoopPolicy())", "..."),
    ("from aiofile import async_open", "async_open = NotImplemented"),
})
```

`uvloop` (libuv-backed loop, ~2–4× asyncio throughput) is installed via `set_event_loop_policy` *if importable*; absent, the `"..."` else-branch is a no-op (plain asyncio). `aiofile.async_open` (async file I/O) similarly degrades to `NotImplemented` (file streaming helpers then fail loudly if used, but the server runs). The `exec(if_/else_)`-as-strings pattern is ugly but achieves "optional accelerator with silent graceful fallback." **Steal the *intent* (optional uvloop, never a hard dep), not the `exec` mechanism** — use a normal `try/except ImportError`. For Lancelot: uvloop should be a soft default; on Windows (no uvloop) fall back to `asyncio` / `WindowsProactorEventLoopPolicy`.

### 3.3 `__Scope__` — the global service registry (`Dependencies/__init__.py:202-288`)

Not a stdlib swap but the project's DI/registry substitute for `contextvars`. `__Scope__` mangles keys with a framer char and `setattr`s them onto a target object's attributes:

```python
class __Scope__:
    def __init__(app, framer="\x00", obj=main_thread, unique=True): ...
    def mux(app, key): return framer + key + framer            # "\x00key\x00"
    def __setattr__(app, key, value):
        app.added_keys.append(key)
        setattr(app.obj(), app.mux(key), value); app.set_event.set()
    def __getattr__(app, key, *a): return getattr(app.obj(), app.mux(key), *a)
    async def wait_for_key(app, key):                          # await until a service registers
        while not (value := app.get(key)): await app.set_event.wait_clear()
        return value

Scope        = __Scope__()                                     # obj=main_thread → process-global
InternalScope= __Scope__("\x01")                               # separate namespace, same thread obj
Taskscope    = __Scope__(obj=current_task, unique=False)        # per-Task "contextvars" substitute
createTaskscope = lambda: __Scope__(obj=current_task, unique=False)
```

`Scope`/`InternalScope` store onto `main_thread` (process singletons: SSL contexts at `:693-696`, the session pool at `Client/__init__.py:639-644`, socket limits at `:706-709`). `Taskscope` stores onto `current_task()` — **this is how Blazeio does request-scoped state without `contextvars`** (`plog`/`llog` read `Taskscope.get("plog_lineno_callback")` for per-task log line routing, `alts.py:496`). `wait_for_key` + `set_event` lets a consumer `await` a service that registers later (startup-order-independent DI). `App.register_to_scope` (`__init__.py:706-713`) registers each server instance into `Scope` by name, auto-deduping with a numeric suffix (`gen_server_name`, `:694-704`).

**Steal:** the `wait_for_key` "await until registered" pattern is a nice startup-order-independent DI for an AI backend (e.g. handler awaits the model-client pool which warms up async). **Avoid:** mangling attributes onto `current_task()` as a contextvars replacement — it's invisible to debuggers, leaks if a task holds references, and breaks under `reTask` (which sets `__BlazeioProtocol__` directly anyway). Lancelot should use real `contextvars.ContextVar` for request scope (works with `asyncio.Task`, propagates correctly, debuggable) and a plain dict+`asyncio.Event` for the "await a late-registered service" case.

### 3.4 Off-loop threaded logger (`Dependencies/__init__.py:354-478`)

`Default_logger` runs a **dedicated thread with its own event loop** (`:391-393, 466-471`). Log calls (`Log.info`, `plog.debug`) enqueue `(args, kwargs, event)` into an `Enqueue` (custom queue, `:289-353`) via `run_coroutine_threadsafe` to the logger thread's loop (`:433`); the log worker (`:445-461`) batches-drains the queue and does the blocking `sys_stdout.write` + `flush` **off the main event loop**. `realtime=False` by default → fire-and-forget (the awaiting caller doesn't block on terminal I/O at all). `Enqueue` itself (`:289-353`) is a `deque` + two `SharpEvent`s with an overflow `Condition` and a background `check_overflow` task.

**Why:** terminal/stdout writes are blocking syscalls; doing them inline stalls the event loop under load. Pushing them to a worker thread keeps the request loop pure-async. For an AI backend logging every LLM request/response (large), this matters. **Steal the pattern** (off-loop log sink, batched drain, fire-and-forget) — but use `logging.handlers.QueueHandler`/`QueueListener` (stdlib, battle-tested) rather than a bespoke thread+loop+queue. Same architecture, less code to own.

### 3.5 `Ehandler` — the universal try/except context manager (`alts.py:586-660`)

Sync+async context manager that wraps a block, routes exceptions to `traceback_logger`, supports `ignore=[ExcA]` / `_raise=[ExcB]` / `onerr=cb` / `exit_on_err`. Used everywhere as `with Ehandler(ignore=Eof):` / `async with Ehandler():`. `should_ignore`/`should_raise` compare by `__class__.__name__` string (`:609-616`) — INFERRED to survive duplicate exception classes across reimports. **Steal the ergonomics** (one CM for "log-and-swallow vs log-and-reraise vs ignore-these") — it's genuinely useful for handler boundaries. **Avoid** comparing exceptions by class *name string* — use `isinstance`; the string compare silently mishandles subclasses and is slower.

---

## 4. The async HTTP client — the AI backend outbound hot path (DEEP)

This is the most important section for Lancelot: **an AI backend spends most of its wall-clock time in outbound calls to LLM/embedding/tool APIs.** Every millisecond and every allocation in the client multiplies by concurrency. Blazeio's client is `Client/__init__.py` (672) + `Protocols/client_protocol.py` (71) + `Protocols/client_protocol_tools.py` (907) + the shared `Protocols/__init__.py` (68) buffered base.

### 4.1 Architecture overview

```
getSession / Session / SessionPool   ← user-facing request API (async CM)
        │
   __SessionPool__                    ← per-host connection pool, keyed (host,port,*proxy)
        │  hands out reusable Session instances guarded by ioCondition
   Session  (Pushtools, Pulltools)    ← request builder + response streamer
        │
   BlazeioClient(BlazeioClientProtocol)
        │  create_connection → loop.create_connection(lambda: app, host, port, ssl=...)
   BlazeioClientProtocol(BlazeioProtocol, BufferedProtocol)
        │  get_buffer/buffer_updated zero-copy recv; pause_reading backpressure
   transport (asyncio/uvloop TCP, optional TLS via start_tls)
```

### 4.2 The protocol: zero-copy receive, one preallocated buffer

`BlazeioClientProtocol` (`client_protocol.py:6-69`) subclasses `BlazeioProtocol` (`Protocols/__init__.py:4-67`) and `asyncio.BufferedProtocol`. The receive path (shared with the server):

```python
# Protocols/__init__.py
def get_buffer(app, sizehint):                                  # asyncio asks "where do I write?"
    if sizehint > len(app.__buff__memory__):
        app.__buff__ = bytearray(sizehint); app.__buff__memory__ = memoryview(app.__buff__)
    elif sizehint <= 0: sizehint = len(app.__buff__memory__)
    return app.__buff__memory__[:sizehint]                      # hand back a SLICE of the SAME buffer
def buffer_updated(app, nbytes):
    app.transport.pause_reading()                               # ← backpressure: stop after every read
    app.__stream__.append(bytes(app.__buff__memory__[:nbytes])) # copy out exactly nbytes
    app.__evt__.set()                                           # wake the reader (SharpEvent)
```

`__buff__ = bytearray(__chunk_size__)` is allocated **once per connection** (`client_protocol.py:33`, `__chunk_size__ = ioConf.OUTBOUND_CHUNK_SIZE = 102400` from `Dependencies/__init__.py:132`). `BufferedProtocol` (vs `Protocol.data_received(data: bytes)`) lets asyncio/uvloop write directly into Blazeio's memoryview — **no intermediate bytes object allocated by the loop**. The one unavoidable copy is `bytes(memoryview[:nbytes])` into the `__stream__` deque (needed because the buffer is about to be reused). Reads are consumed by `__aiter__` (`:63-67`): `await ensure_reading()` then drain `__stream__` via `popleft()`.

`pause_reading()` after **every** `buffer_updated` is the backpressure mechanism: the protocol reads exactly one buffer's worth, pauses the transport, and only `resume_reading()`s (in `ensure_reading`, `:45-54`) when the consumer has drained `__stream__` and is ready for more. This bounds memory to ~one chunk per idle connection regardless of how fast the server pushes — **critical for streaming a fast LLM response without unbounded buffering.**

`prepend(data)` (`:36-39`) is zero-copy un-read: `__stream__.appendleft(data)` — used to push back over-read body bytes after header parsing and after chunked-frame boundaries.

### 4.3 Connection establishment, TCP tuning, TLS

`BlazeioClient.create_connection` (`Client/__init__.py:94-122`):
```python
transport, protocol = await app.loop.create_connection(lambda: app, host, port, *args, **kwargs)
app.sock = protocol.transport.get_extra_info("socket")
app.sock.setsockopt(IPPROTO_TCP, TCP_NODELAY, 1)        # disable Nagle — send small frames immediately
app.sock.setsockopt(SOL_SOCKET, SO_KEEPALIVE, 1)
app.sock.setsockopt(IPPROTO_TCP, TCP_KEEPIDLE, 30)      # probe after 30s idle
app.sock.setsockopt(IPPROTO_TCP, TCP_KEEPINTVL, 10)     # 10s between probes
app.sock.setsockopt(IPPROTO_TCP, TCP_KEEPCNT, 3)        # 3 failed probes → dead
```

`TCP_NODELAY=1` is essential for request/response latency (no 40ms Nagle delay on small POST bodies — every LLM request is a smallish JSON POST). The keepalive triad (30/10/3) detects a dead upstream in ~60s and is more aggressive than OS defaults — appropriate for a pool that must not hand out half-open sockets to an LLM provider. `connection_made` (`client_protocol.py:42`) immediately `transport.pause_reading()` so no bytes are read until a request is sent and a reader awaits. TLS: `port == 443` → `ssl = get_ssl_context()` (`Client/__init__.py:347-348`), a cached process-global `SSLContext` with `check_hostname=False, verify_mode=CERT_NONE` (`Dependencies/__init__.py:687-696`) — **INFERRED: TLS verification is disabled by default.** For Lancelot calling api.openai.com etc. this is unacceptable; **DESIGN: verify certs by default**, expose an opt-out. `start_tls` (`Client/__init__.py:124-125`) supports STARTTLS-over-proxy via `loop.start_tls`.

### 4.4 Connection pooling — `__SessionPool__` (`Client/__init__.py:456-532`)

The pool is the heart of the AI hot path. Key data structure: `app.sessions: dict[key, list[instance]]` where `key = (host, port, *socks5proxy.values(), *httpproxy.values())` (`:512`). Each `instance` is a `ddict` (`:486-499`):

```python
instance = ddict(
    acquires=0, key=key,
    available=SharpEvent(),
    context=ioCondition(),                                   # the per-connection mutex+condvar
    perf_counter=perf_counter())                             # last-use timestamp for idle reaping
instance.session = Session(*args, on_exit_callback=(app.release, instance), close_on_exit=False, **kwargs)
instance.clean_cb = ReMonitor.add_callback(app.keepalive_interval, app.clean_instance, instance)
```

**Acquisition** (`get`, `:509-532`):
1. Build the host key, look up `instances` list.
2. No list → create one + first instance. Have a list → `get_instance` (`:501-507`) returns the first instance whose `available` event is set (not in use); if `max_contexts` is 0 it also skips instances with `context.waiter_count >= 1`.
3. No free instance → if under `max_contexts` (default 2) make a new one; else **pick the least-contended**: `waiters = [i.context.waiter_count for i in instances]; instance = instances[waiters.index(min(waiters))]` (`:520-521`). This is least-connections load balancing across the per-host connection set.
4. `async with instance.context:` → `await instance.context.wait()` — serialize use of this physical connection (one in-flight request per connection; HTTP/1.1, no multiplexing).
5. Liveness gate (`:525-531`): if the protocol has no transport, or `is_timed_out`, or `transport.is_closing()`, or `__wait_closed__` is set → drop `instance.session.protocol = None` so the next `create_connection` reconnects. **Stale-connection detection before handing it to a caller** — prevents the classic "pooled socket the server already closed" bug.

**Release** (`release`, `:461-466`): `instance.acquires -= 1; instance.available.set(); instance.perf_counter = perf_counter(); instance.context.notify(1)` — connection returns to pool, timestamp refreshed, one waiter on the condition woken. Wired as the Session's `on_exit_callback` so `Session.__aexit__` returns the connection automatically.

**Idle reaping** (`clean_instance`, `:471-483`): scheduled every `keepalive_interval=30s` on the `ReMonitor` background thread (`Dependencies/__init__.py:611-613, 672-683`). If the instance is `available`, has `acquires < 1`, and `is_timed_out` (`perf_counter - last_use >= timeout`, default `timeout=60s`, `:468-469`) → remove from pool and `protocol.cancel()` (abort the socket). Otherwise re-arm the callback. So a connection idle > 60s is closed; an active or recently-used one is kept alive indefinitely (subject to TCP keepalive). **This is exactly the keep-alive + idle-timeout policy an LLM client needs**: warm connections for burst traffic, reaped before the provider's own idle timeout (typically 60–120s) would silently kill them.

`max_contexts` (default 2 via `__SessionPool__.__init__`, `:458`; `getSession`'s pool uses `socket_limits/2`, `Client/__init__.py:638-644`) caps physical connections **per host**. `max_conns` (default 0 = unlimited) is a global cap. INFERRED: default 2 connections/host is conservative; for high-concurrency LLM workloads you'd raise it.

### 4.5 The request build path — every allocation on the wire

`Session.create_connection` (`Client/__init__.py:273-402`) is the request assembler. Notable hot-path mechanics:
- **C-accelerated URL parse**: `app.host, app.port, app.path = ioConf.url_to_host(url, params)` (`:294`) — the C `url_to_host` (§5). Params are C-`url_encode_sync`'d first (`:292`).
- **Header normalization** via `DictView` (`Itools/__init__.py:260-288`): a case-insensitive view over the user's header dict — capitalizes keys for lookup without copying the dict, pops/repositions the `__important_headers__` (`Content-length`, `Transfer-encoding`, `Content-encoding`, `Content-type`, `Cookies`, `Host`).
- **Body encoding**: `json` kwarg → `dumps(json, indent=0).encode()` + `Content-type: application/json` (`:334-336`); `form_urlencoded` → C `url_encode_sync` per value (`:256-257, 330-332`); `body` sets `Content-length` (`:338-339`); streaming `content` (AsyncIterable) with no length → `Transfer-encoding: chunked` (`:341-346`).
- **C-accelerated payload assembly**: `payload = ioConf.gen_payload(method, stdheaders, app.path, str(app.port))` (`:370`) — the C `gen_payload` (§5) builds the entire `METHOD path HTTP/1.1\r\nHeader: val\r\n...\r\n\r\n` byte string in one C buffer (stack-allocated if ≤2048B). `if body: payload += body` (`:372`) then one `await app.protocol.push(payload)` (`:375`) — **headers+small body sent in a single `transport.write`** (one syscall).
- **Writer selection** (`configure_writer`, `:404-408`): chunked → `write_chunked` (emits `%X\r\n<chunk>\r\n` frames, `client_protocol_tools.py:657-667`); else raw `protocol.push`. Optional content-encoding encoder (gzip/brotli) wired at `:380-381`.
- **Retry loop** (`_connector`, `:259-271`): up to `max_retries=3` (`defaults`, `:172-173`); on `ServerDisconnected` it retries **only for idempotent methods** (`NON_BODIED_HTTP_METHODS = {GET, HEAD, OPTIONS, DELETE}`, `:151-153`) — a POST is *not* auto-retried (correct: never silently replay a non-idempotent LLM call). On success it stashes `task.__BlazeioClientProtocol__ = app.protocol` so `ClientContext.r()` can find the connection from the current task (`:265-266`).

### 4.6 The response streaming path — the LLM streaming core

This is what handles an SSE/chunked LLM response. `prepare_http` → `MinParsers.client.aparse` (`min_http_parser.py:154-175`): reads until `\r\n\r\n`, parses status + lowercased headers, sets `r.received_len, r.content_length, r.content_type`, and selects the body handler (`:139-144`):
- `Transfer-encoding` present → `handler = handle_chunked`
- `Content-length` present → `handler = handle_raw`
- neither → `handler = protocol.pull` (read until close)

Leftover body bytes after headers are `prepend`'d back (`:172-173`) — zero-copy, no re-read.

**`handle_chunked`** (`client_protocol_tools.py:603-639`) — the streaming decoder. State machine in a `ddict` (`end`, `buff`, `read`, `size`, `idx`); `async for chunk in app.protocol.pull()`: parse the hex chunk size up to `\r\n`, slice exactly `size` bytes, `prepend` any over-read tail, `yield chunk`, repeat until the `0\r\n\r\n` terminator. **Every decoded chunk is yielded immediately** — no buffering of the full body. For an LLM streaming `text/event-stream` or chunked JSON-lines, tokens flow to the consumer as they arrive off the socket.

**`handle_raw`** (`:641-648`): `async for chunk in protocol.pull(): received_len += len(chunk); yield chunk; if received_len >= content_length: return` — straight pass-through with a length counter.

**Decompression streaming** (`Decoders`, `client_protocol_tools.py:46-102`): if `Content-encoding: gzip`/`br`, `pull()` (`:710-723`) wraps the handler in `gzip_decoder`/`br_decoder`. These maintain a streaming `decompressobj`/brotli `Decompressor` and **offload large blocks to a thread** (`to_thread(decompressor.decompress, ...)` when buffer ≥ 10240 for br / chunk ≥ 102400 for gzip, `:60-61, 75`) so a big compressed LLM response doesn't block the event loop during inflate. Decoders yield decompressed chunks incrementally.

The consumer API on top of `pull()` (`Pulltools`, `:693-905`):
- `async for chunk in session:` (`__aiter__` → `pull`, `:881-882`) — the streaming primitive. **This is the LLM-token loop.**
- `.aread()` / `.text()` / `.json()` (`:731-763`) — buffer the whole body then decode (`json` → `loads` into `Dot_Dict`).
- `.read_exactly(size)` / `.read_into(memoryview)` (`:740-755, 897-905`) — fixed-size reads into a preallocated `memarray`/view (zero-copy into caller's buffer).
- `.pipe_to(pipe)` / `.pipe_from(pipe)` (`:884-895`) — splice a response stream straight into another writer (e.g. proxy an upstream LLM stream to the downstream client without materializing it — `Modules/streaming.py:170-181` `Protocols.forward_to_client`).
- `.save()` / `.asave()` with **automatic Range-resume** (`__save__`, `:769-795`; `adl`, `:797-835`): on partial transfer it re-issues with `Range: bytes=received-total` — resilient large-file/long-response download.

### 4.7 Why this design is right for concurrent LLM calls — and the gaps

**Right:** (1) one preallocated recv buffer per connection + `BufferedProtocol` → near-zero per-chunk allocation while streaming N concurrent responses; (2) `pause_reading`/`resume_reading` backpressure → bounded memory under fast producers; (3) per-host pool with stale-detection + idle reaping tuned to ~60s → warm connections, no dead-socket hand-outs, no leak; (4) least-`waiter_count` connection selection → even spread of concurrent requests; (5) POST never auto-retried → no silent duplicate completions/charges; (6) decompression offloaded to threads → inflate doesn't stall the loop; (7) streaming `async for` yields tokens with no whole-body buffering → low TTFT passthrough.

**Gaps Lancelot must close:** (a) **HTTP/1.1 only, one request per connection** (`async with instance.context` serializes) — no HTTP/2 multiplexing. LLM providers support HTTP/2; one TCP conn could carry many concurrent streams. Blazeio needs `max_contexts` connections to get `max_contexts` concurrency; HTTP/2 would need far fewer sockets. (b) **TLS verification disabled by default** (`CERT_NONE`) — a security defect for calling external APIs. (c) **No connect/read timeout enforced in the client itself** — `timeout` kwarg exists (`create_connection` default 30.0, `:273`) but enforcement is via the external `ReMonitor` watchdog thread inspecting `__BlazeioProtocol__` tasks (`Dependencies/__init__.py:643-661`), and `reTask`s are invisible to `all_tasks()`. Per-request `asyncio.wait_for`/deadline is cleaner. (d) **No DNS caching** — `loop.create_connection(host, ...)` resolves every connect; under churn that's a getaddrinfo per new connection. (e) No circuit-breaker / per-host concurrency cap beyond the raw connection count.

### 4.8 DESIGN: Lancelot's outbound LLM client

```
LancelotHTTPClient
├─ Transport: httpx-style but BufferedProtocol-based; HTTP/2 by default (h2),
│             HTTP/1.1 fallback. TCP_NODELAY=1, keepalive 30/10/3 (steal Blazeio's).
├─ TLS: verify=True by default (real cert chain), pinned process-global SSLContext,
│        ALPN h2. Opt-out per-call only.
├─ Pool: per-(scheme,host,port) like __SessionPool__. For HTTP/2 keep 1–2 conns/host
│         (many streams each); for HTTP/1.1 size = target concurrency. Steal:
│         stale-detection-before-handout, idle reap < provider idle timeout (use 50s),
│         least-in-flight selection (Blazeio's min(waiter_count)).
├─ DNS: cache getaddrinfo results with TTL (Blazeio lacks this — add it; ~1 RTT saved
│        per cold connect, big at high connection churn).
├─ Recv: ONE preallocated bytearray + memoryview per connection, BufferedProtocol
│         get_buffer/buffer_updated, pause_reading backpressure (steal verbatim — this
│         is the single biggest allocation win for concurrent streaming).
├─ Read primitive: `async for chunk in resp.aiter_bytes()` yielding immediately;
│         SSE/JSONL framing decoder on top that yields parsed events as they arrive
│         (TTFT-optimal). Decompress in a thread for large blocks (steal).
├─ Reliability: per-request deadline via asyncio.wait_for (NOT an external watchdog).
│         Idempotent retry (GET) with jittered backoff; POST retried ONLY on connect
│         failure / pre-first-byte (never after the model started streaming — steal
│         Blazeio's "POST not auto-retried" rule, refine to "retry only pre-first-byte").
│         Per-host circuit breaker + concurrency semaphore.
├─ Codec: orjson (bytes in/out, skip .encode()); request body assembled in one
│          buffer like C gen_payload — or just let httpx/h2 frame it.
└─ Read-ready signal: SharpEvent-style single-shared-future + already-set fast path +
   fused wait_clear (steal §2.3 — safe, no Task replacement). Optionally one
   recyclable awaitable for the byte-ready event (§2.2) after benchmarking vs uvloop.
```

**Net:** steal Blazeio's buffer/backpressure/pool-hygiene/least-loaded/no-POST-retry; reject its TLS default, HTTP/1.1-only, external-watchdog timeout, and no-DNS-cache. The result is "httpx-quality safety with Blazeio-quality allocation behavior, plus HTTP/2."

---

## 5. The C-extension acceleration layer (KNOWN — `Extensions/*.c`)

Five C source files, each a self-contained CPython module compiled with `-O3`. They accelerate **only string/byte hot-path ops**, not control flow.

| Module | C func(s) | What it does | Pure-Py fallback | Logged slowdown |
|---|---|---|---|---|
| `client_payload_gen.c` | `gen_payload` | Build full HTTP request bytes (`METHOD path HTTP/1.1\r\nK: V\r\n…\r\n\r\n`) from method+headers dict in one pre-sized buffer | `OtherUtils.gen_payload` (`client_protocol_tools.py:326-337`) | **6.6X** (`:344-347`) |
| `Blazeio_iourllib.c` | `url_to_host` | Parse URL → `(host, port, path)`, append encoded params | `Urllib.url_to_host` (`client_protocol_tools.py:273-319`) | **3.0X** (`:339-342`) |
| `c_request_util.c` | `url_decode_sync`, `url_encode_sync`, `get_params_sync` | Percent-decode/encode; parse `?a=1&b=2` query → dict | (pure-py request utils) | not logged |
| `server_tools_header_to_payload.c` | `headers_to_http_bytes` | Serialize response header dict → bytes | `ExtraToolset.headers_to_http_bytes` (`server_protocol_extratools.py:165-175`) | not logged |
| `simple_serve_util.c` | `SimpleServe` type (`validate_cache`, `prepare_metadata`) | Static-file metadata/etag/cache validation | `Modules/server_tools.py` Simpleserve | not logged |

### 5.1 How they're built

Root `setup.py:41-47`: declares three `Extension`s (`Blazeio_iourllib`, `client_payload_gen`, `c_request_util`) with `extra_compile_args=['-O3']`, `build_ext` `inplace=False, force=True`. On **Windows** (`setup.py:56-59`) it bypasses setuptools' ext build and shells out to `Extensions/setup_build.py` per module with `--mname/--mpath`. `Extensions/setup_build.py:19-26` does a self-mutating version bump (reads its own source, increments the `version = "x.y.z"` string, rewrites itself) then builds a single `Extension(args.mname, sources=[args.mpath], extra_compile_args=['-O3'])`. The self-rewriting-version trick is a hack to force a fresh build artifact each install (`force=True` + bumped version defeats any caching).

### 5.2 How they're loaded and faded out

`Dependencies/__init__.py:134-144`:
```python
def c_extension_importer():
    try:
        from c_request_util import url_decode_sync, url_encode_sync, get_params_sync
        from client_payload_gen import gen_payload
        from Blazeio_iourllib import url_to_host
        for i in (url_decode_sync, url_encode_sync, get_params_sync, gen_payload, url_to_host):
            setattr(ioConf, i.__name__, i)              # publish onto the global config object
    except ImportError as e:
        print(e)                                        # ← silent-ish: prints, does NOT raise
c_extension_importer()
```
Everything calls `ioConf.gen_payload(...)` / `ioConf.url_to_host(...)` — an attribute on the global `ioConf`. If the C import fails, `ioConf.gen_payload` is `None` (because `__ioConf__.__getattr__` returns `None` for missing attrs, `:85-86`). Then at module-import time in `client_protocol_tools.py:339-347`:
```python
if not ioConf.url_to_host:
    loop.create_task(log.critical("Using pure python implementation of: url_to_host, which is 3.0X slower."))
    ioConf.url_to_host = Urllib.url_to_host
if not ioConf.gen_payload:
    loop.create_task(log.critical("Using pure python implementation of: gen_payload, which is 6.6X slower."))
    ioConf.gen_payload = OtherUtils.gen_payload
```
The fallback is wired and the **measured slowdown is logged at CRITICAL** so an operator running without the C exts knows the exact perf cost (3.0× / 6.6×). The framework is **fully functional pure-Python**; C is a transparent accelerator.

### 5.3 The C technique worth stealing: stack-buffer + single-pass assembly

`gen_payload` (`client_payload_gen.c:64-162`): a first pass (`calculate_total_size_and_extract`, `:17-62`) computes the exact output size *and* extracts every header key/value pointer+length via `PyDict_Next` + `PyUnicode_AsUTF8AndSize` (zero-copy borrow of Python str internals). Then it picks `char stack_buffer[2048]` if the total fits the `STACK_ALLOC_THRESHOLD`, else one `malloc` — and `memcpy`s method/path/version/headers in a single forward pass into that one buffer, returning `PyBytes_FromStringAndSize` once. **No intermediate Python str/bytes, no `%`-format, no list join — one allocation (often a stack buffer, i.e. zero heap allocation), one copy pass, one Python object out.** `url_decode_sync`/`url_encode_sync` (`c_request_util.c:20-84`) similarly use a fixed `char output[MAX_URL_LEN(*3)]` stack scratch. This is ~6.6× the pure-Python `payload += "%s: %s\r\n" % ...` loop which allocates a new `str` per header + final `.encode()`.

**DESIGN for Lancelot:** the *highest-leverage* C/Rust/Cython surface is exactly Blazeio's choice — **request-line/header serialization and URL/percent-coding**, because they run on every single outbound LLM call and are pure byte-shuffling (no business logic, stable, easy to test against a pure-Py oracle). Mirror Blazeio's pattern: (1) one accelerated function = one well-defined byte transform; (2) publish via a single indirection point (`ioConf.X`) so swap is transparent; (3) ship a pure-Python reference impl that's always correct; (4) **log the measured slowdown factor** when running degraded so ops can see the cost. **Avoid** the self-rewriting `setup_build.py` version-bump hack — use proper build isolation / a wheel. Prefer Rust+PyO3 or Cython over hand-written CPython C (memory-safety; the C here uses `strdup`/`realloc`/`malloc` with manual frees and a fixed `MAX_URL_LEN 4096` stack buffer — a >4096-byte URL in `url_decode_sync` **overflows `char output[MAX_URL_LEN]`**, a real buffer-overflow bug at `c_request_util.c:28`). Do not copy the C verbatim; copy the *strategy*.

---

## 6. Itools / algorithms — hot data-structure & string tricks (KNOWN)

`Itools/__init__.py` (366) is the zero-dependency core toolbox (only stdlib imports). The reusable tricks:

- **`Dot_Dict`/`ddict`** (`:6-35`): `dict` subclass with attribute access (`__getattr__` → `__getitem__`, `__setattr__` → `__setitem__`). Used as the universal record everywhere (route entries, pool instances, parsed JSON). Cheaper than `types.SimpleNamespace` for dict-shaped data and JSON round-trips for free. `DotDict.token_urlsafe` (`:23-30`) generates a collision-free token by retrying against itself.
- **`memarray(bytearray)`** (`:104-116`): a `bytearray` subclass whose **slice `__getitem__` returns `bytes(memoryview(self)[key])`** and slice `__setitem__` writes through a `memoryview`. The point: slicing a normal `bytearray` copies; routing every slice through a `memoryview` makes the *intent* explicit and the buffer-mutation zero-copy on assignment. Used as the chunk-assembly buffer in `handle_chunked` decoders so frame slicing doesn't realloc.
- **`zcbuff`** (`:118-148`): a "zero-copy buffer" — fixed `bytearray` + a `pointer`; `extend` writes at `pointer` via `memoryview` slice-assign (optionally growing), `chunk(size)` returns the last `size` bytes and rewinds the pointer. A ring-ish scratch buffer that avoids per-append reallocation. INFERRED: used where a known-bounded amount is accumulated then drained.
- **`DictView` / `dotdictView`** (`:260-313`): a **case-insensitive header map without copying** — builds `{Capitalized: original_key}` once, routes `get/pop/__setitem__/__contains__` through it. HTTP headers are case-insensitive; this gives O(1) case-insensitive access over the user's original dict with one small index dict, no full re-key. `dotdictView` docstring even carries a micro-benchmark (`:291`): create ~7µs, get ~26µs.
- **`Multirouter`** (`:83-102`): see §1.2 — registration-time fan-out (multiple handlers, same route, awaited in sequence).
- **`Super`** (`:170-191`): the manual MRO-walk super-init (see §1.1) — *documented as an anti-pattern to avoid for Lancelot*.
- **`set_from_args`/`get_from_args`/`load_from_type_in_locals`** (`:216-258`): annotation-driven attribute binding — `set_from_args(app, locals(), Utype)` copies every local whose `__init__` annotation is/contains a sentinel type onto `app`. A reflective "bind constructor args by annotated type" used by `Httpkeepalive`. Clever but `locals()`-based reflection is slow and opaque; **avoid in Lancelot's hot path**, fine for one-time config objects.
- `algorithms.py` (80): `Perf.ops_calc` — a `tracemalloc`+`perf_counter` micro-benchmark decorator (runs the fn N times, diffs memory snapshots, logs allocations/duration). Not product code; it's the in-repo profiler used to derive the "3.0X/6.6X" numbers. **Steal the idea**: keep an allocation-aware micro-bench harness in-tree so perf claims are reproducible.

**Top reusable:** `DictView` (case-insensitive headers, no copy) and `memarray` (explicit zero-copy slicing) are directly portable to Lancelot's request/response header and buffer handling.

---

## 7. Comparison matrix — Blazeio vs FastAPI (Starlette + httpx) vs Robyn

| Dimension | Blazeio | FastAPI / Starlette + httpx | Robyn |
|---|---|---|---|
| Route match | `OrderedDict` exact O(1); O(k) prefix-trim fallback; no native path params | Ordered list, compiled-regex per route O(n·regex); full path params + converters | Rust radix/matchit trie O(k); `/users/:id` params |
| Per-request validation | none (handler parses `r` itself) | Pydantic model validation/coercion per request (correctness, cost) | none built-in (you parse) |
| Server I/O | custom `BufferedProtocol`, one preallocated buffer/conn, `pause_reading` backpressure, zero-copy `prepend` | uvicorn/h11 or httptools; `Protocol.data_received(bytes)` (loop allocates bytes per read) | Rust (actix/hyper-class) I/O, no Python recv allocation |
| Async client (built-in) | **yes** — pooled, buffered, streaming, idle-reaped, least-loaded | httpx (separate dep): pooled, HTTP/2, TLS-verify default, timeouts, DNS via anyio | **none** — bring your own (aiohttp/httpx) |
| Client TLS default | `CERT_NONE` (no verify) ⚠️ | verify=True (correct) | n/a |
| Client HTTP/2 | no (HTTP/1.1, 1 req/conn) | httpx: yes (h2) | n/a |
| Custom Future/Task | `reFuture` self-recycling + `reTask` (~40-line Task) | stdlib `asyncio` (uvloop optional) | Rust executor |
| Event primitive | `SharpEvent` (1 shared future, already-set fast path, fused wait_clear) | `asyncio.Event` (future per waiter) | Rust |
| C/native accel | 5 `-O3` C exts (payload/url), transparent pure-Py fallback w/ logged slowdown | Pydantic-core (Rust), httptools (C), uvloop (Cython) — all hard-ish deps | whole router/runtime in Rust |
| JSON | ujson → json fallback | Pydantic/orjson optional | Rust serde / Python |
| Loop | uvloop if present, asyncio else | uvloop via uvicorn | Rust (no Python loop for routing) |
| Failure mode if accelerator missing | runs pure-Python, logs exact X factor | Pydantic-core/httptools effectively required | n/a (Rust always present) |

**Reading:** Blazeio out-allocates Starlette on the server recv path and ships a competent built-in client Robyn lacks; it loses to FastAPI on correctness ergonomics (no validation, no path params, no TLS verify) and to Robyn on raw router speed + real path params. **Lancelot's wedge:** Blazeio's allocation discipline + built-in pooled streaming client, *plus* FastAPI's validation/path-param ergonomics and safe defaults, *plus* a Rust/Cython hot core like Robyn for routing+serialization.

---

## 8. Steal / Avoid — consolidated for Lancelot

### Top 5 STEALS (highest value, lowest risk first)
1. **`SharpEvent`-style read-ready signal** (`Dependencies/__init__.py:146-200`): single shared future + already-set fast path + fused `wait_clear`. Strictly better than `asyncio.Event` for the 1-waiter socket-readiness case, zero ecosystem cost. Use for connection read-ready in both server and client.
2. **Buffered zero-copy recv + `pause_reading` backpressure** (`Protocols/__init__.py:6-67`): one preallocated `bytearray`+`memoryview` per connection via `BufferedProtocol.get_buffer`/`buffer_updated`, pause after each read, `prepend` for un-read. The single biggest allocation win for concurrent LLM streaming. Steal verbatim.
3. **Connection-pool hygiene** (`Client/__init__.py:456-532`): per-(host,port,proxy) keying, **stale-detection before hand-out**, idle reap on a timer set *below* the provider's idle timeout, **least-in-flight selection** (`min(waiter_count)`), and **POST never auto-retried**. This is the correct LLM-client pool policy.
4. **Transparent native-accel pattern** (`Dependencies/__init__.py:134-144` + `client_protocol_tools.py:339-347`): one byte-transform per accelerated fn, single indirection point (`ioConf.X`), always-correct pure-Python fallback, **log the measured slowdown factor when degraded**. Apply to request/header serialization + URL coding (Lancelot's per-call hot ops).
5. **`DictView` case-insensitive header map** (`Itools/__init__.py:260-288`) + **`memarray` explicit zero-copy slicing** (`:104-116`) + **`ddict`** universal record: directly portable, no deps.

(Runner-up steals: self-specializing dispatch chosen at startup based on registered middleware §1.4; single classified exception boundary that treats disconnects as non-errors §1.4; off-loop batched logger §3.4 — but implement via stdlib `QueueHandler`; `wait_for_key` startup-order-independent DI §3.3.)

### AVOID (Blazeio decisions Lancelot must not copy)
- **Wholesale `asyncio.Task` replacement (`reTask`)**: loses `contextvars` (need it for request-scoped tracing/auth in an AI backend), structured cancellation, and debuggability; invisible to `asyncio.all_tasks()` (its own watchdog can't see them). Keep `asyncio.Task`; recycle only the one hottest awaitable.
- **TLS `CERT_NONE` default** (`Dependencies/__init__.py:687-696`): security defect for calling external LLM APIs. Verify by default.
- **HTTP/1.1-only, one-request-per-connection client**: no multiplexing. Use HTTP/2 (h2) for LLM providers.
- **External-watchdog request timeouts** (`ReMonitor` thread inspecting tasks): racy, and blind to `reTask`s. Use per-request `asyncio.wait_for`/deadline.
- **No DNS caching**: add a TTL'd resolver cache (Blazeio resolves every connect).
- **7-deep mixin tower + manual MRO `Super` walk** (`__init__.py:657`, `Itools:170-191`): brittle, no constructor args, anti-`super()`. Prefer composition.
- **Exception comparison by class-name string** (`Ehandler.should_ignore`, `alts.py:609-616`): use `isinstance`.
- **Self-rewriting `setup_build.py` version bump** (`Extensions/setup_build.py:19-26`) and **hand-written CPython C with fixed `char[4096]` stack buffers** (`c_request_util.c:28` — overflowable by a long URL): use Rust/PyO3 or Cython + proper wheels.
- **Routes-only-from-function-names + funcname normalization** as the *only* mechanism: keep it as optional sugar; provide explicit path strings + real `{param}` routing.
- **`Taskscope` (attrs on `current_task()`) as a contextvars substitute**: use real `contextvars.ContextVar`.

---

## 9. Reimplementable walkthrough — `reFuture`/`reTask` line-by-line (the recyclable scheduler)

This is the part of Blazeio a senior engineer must be able to rebuild from notes alone. The full mechanism, annotated, with the exact CPython contract it relies on.

### 9.1 The awaitable contract `reFuture` exploits

CPython's `asyncio.tasks.Task.__step` does, in essence: `result = coro.send(None)`; then it checks `result._asyncio_future_blocking`. If that attribute is `True`, the Task treats `result` as a future: it sets `result._asyncio_future_blocking = False`, registers `Task.__wakeup` as `result.add_done_callback`, and **suspends** until that callback fires. If `_asyncio_future_blocking` is absent/falsey, the Task raises `RuntimeError("Task got bad yield")`. `reFuture.__await__` (`_refuture.py:90-98`) reproduces exactly the producer side of this contract:

```python
def __await__(app):
    if not app.done():
        app._asyncio_future_blocking = True   # tell the *driving* Task "I am a future, suspend on me"
        yield app                             # the bare object reaches Task.__step as `result`
    if not app.done():
        raise RuntimeError("await wasn't used with future")
    return app.result()                       # resumed: deliver value or raise stored exception
```

So `reFuture` is awaitable by **any** asyncio Task *and* by `reTask`. No `__iter__`, no generator wrapping — one `yield self`. That's the entire interop surface. (CPython also requires `add_done_callback(cb)` and `result()` — both implemented, `:38-52`.)

### 9.2 The recycle cycle, step by step

State machine over `_state ∈ {_PENDING,_CANCELLED,_FINISHED}` × `_mortality ∈ {_INFINITE,_FINITE}`:

1. `__init__` (`:12-13`): `_state=_PENDING`, `_sleepers=deque()`, `_mortality=_INFINITE` (default) — an *event-like* recyclable future; `reTask` passes `_FINITE` (a one-shot).
2. A consumer `await fut`: `done()` is False → set `_asyncio_future_blocking=True`, `yield app`. The driving Task registers its wakeup via `fut.add_done_callback(task_wakeup)` (`:38-42`): since `_state==_PENDING`, the callback is appended to `_sleepers`, not called.
3. A producer calls `fut.set_result(v)` (`:54-63`): guards (`_CANCELLED`→raise stored exc; `_FINISHED`→ignore — **idempotent**), then `_state=_FINISHED`, `_result=v`, `flush()`.
4. `flush()` (`:85-88`): `awake()` then, **iff `is_infinite()`**, `self._loop.call_soon(self.restart)`.
5. `awake()` (`:74-77`): drain `_sleepers`, for each `(cb,ctx)` do `loop.call_soon(cb, app, context=ctx)` — schedules every waiter's wakeup on the next loop iteration (never reentrant; matches asyncio's "callbacks run via call_soon" invariant).
6. The waiter's wakeup runs `fut.result()` (`:44-52`): if `_exception` set → raise it; else return `_result`. The Task resumes inside `__await__`'s `return app.result()`.
7. **Meanwhile** the `call_soon(restart)` from step 4 runs (`:79-83`): if still `_FINISHED`, reset `_result=None`, `_state=_PENDING`, `_asyncio_future_blocking=False`. **Same object, same `_sleepers` deque, ready to be awaited again.** No allocation.

The ordering is the subtle part: `awake()` schedules waiter-resumptions *before* `restart` is scheduled (both via `call_soon`, FIFO), so a waiter that already grabbed the result resumes with the correct value, and only afterward does the future flip back to `_PENDING` for the next cycle. A *new* awaiter arriving after `restart` simply suspends again (normal pending path). This is why `_INFINITE` works as a reusable "edge-triggered" signal without losing the in-flight result.

`cancel(msg)` (`:65-72`) only succeeds while `_PENDING` (else `InvalidStateError`) — sets `_CANCELLED`, `_mortality=_FINITE` (a cancelled future does **not** recycle — terminal), stores `CancelledError(msg)`, `flush()` (which now skips the `restart` because `is_infinite()` is False). `set_exception` (`:26-36`) likewise forces `_FINITE` (an errored future is terminal) and wraps `StopIteration` to avoid the generator-corruption bug, mirroring CPython `Future.set_exception`.

### 9.3 `reTask` as a trampoline (`:100-148`)

```python
class reTask(reFuture):                       # a Task IS a (finite) future
    __slots__ = ("_coro","__taskpool_timer_handle__","__taskpool_callbacks__")
    def __init__(app, coro, *a, **k):
        app._coro = coro
        super().__init__(*a, mortality=_FINITE, **k)   # one-shot: resolves once with the coro's return
        app._loop.call_soon(app._step)                 # schedule first drive on next tick (not eager)
```

`_step` (`:110-116`) guards "already done" then calls `_step_run_and_handle_result`, and in `finally` does `app = None` — drops the local strong ref so the task object isn't kept alive by the frame longer than needed (GC-friendly; CPython's Task uses `__del__`+weakrefs instead). `_step_run_and_handle_result` (`:118-138`):

```python
try:
    result = app._coro.send(None) if exc is None else app._coro.throw(exc)
except StopIteration as exc:                                  # coroutine returned
    app.cancel(exc.value) if app.cancelled() else app.set_result(exc.value)
except CancelledError as e: app.set_exception(e)
except BaseException as e:  app.set_exception(e)
else:
    if result and result is not app:                          # coro yielded an awaitable
        result.add_done_callback(app._wakeup)                 # resume when it completes
    else:
        app.set_result(result)                                # bare/None yield → done
```

`_wakeup(fut)` (`:140-148`): `fut.result()` (re-raise into us via `set_exception` on failure) else `_step()` again; then `app=None`. This is a 2-method trampoline: `send` → if it yields a future, hang `_wakeup` on it; when that fires, `result()`-propagate then `send` again; on `StopIteration` capture `.value`. The whole Task is ~40 lines because it omits: name generation, `contextvars.copy_context()`, eager-start, `_must_cancel`/`_fut_waiter`, `_log_destroy_pending`, `__del__` "task was never retrieved" warnings, and the `_asyncio_future_blocking` re-arming dance (it drives the coro directly instead of being driven).

### 9.4 Minimal Lancelot port (the part to actually take)

Do **not** port `reTask`. Port only the recyclable signal, and only for the connection read-ready edge:

```python
class RecyclableEvent:
    """Edge-triggered, single-object, reusable. await N times, allocate once.
       Safe with asyncio.Task (uses the same _asyncio_future_blocking contract)."""
    __slots__ = ("_loop","_sleepers","_state","_result")
    def __init__(self, loop): self._loop, self._sleepers, self._state, self._result = loop, [], 0, None
    def __await__(self):
        if self._state == 0:
            self._afb = True            # _asyncio_future_blocking
            yield self
        s, self._state, self._result = self._result, 0, None   # consume + auto-reset
        return s
    def add_done_callback(self, cb, context=None):
        (self._loop.call_soon(cb, self, context=context) if self._state
         else self._sleepers.append((cb, context)))
    def result(self): return self._result
    def set(self, v=True):
        if self._state: return
        self._state, self._result = 1, v
        ws, self._sleepers = self._sleepers, []
        for cb, ctx in ws: self._loop.call_soon(cb, self, context=ctx)
```

Benchmark against `asyncio.Event` *and* `SharpEvent` on uvloop before adopting; the win is real on CPython-asyncio, smaller on uvloop. Keep it confined to the one hot wait (bytes-available); everything else stays vanilla `asyncio` so `contextvars`/cancellation/debug all work.

---

## 10. Reimplementable walkthrough — one pooled streaming LLM call, end to end

The exact code path for `async with getSession.post("https://api.provider.com/v1/chat/completions", json=payload, headers=h) as r: async for chunk in r: ...`, with the AI-relevant behavior at each step.

1. **`getSession(...)`** (`Client/__init__.py:657-658`) → `pool().Session(*args)` → `createSessionPool` whose `Session` is a `PooledSession` (`:583-604`). `.post` resolves through `PooledSession.__getattr__` (`:594-600`) to `app(url, "POST", *rest)` → `SessionPool(*args, max_conns, max_contexts, pool_memory)` (`:602-604`). The pool is a process singleton stashed in `InternalScope` (`:638-644`) sized `socket_limits/2` per `RLIMIT_NOFILE` (`Dependencies/__init__.py:706-709`).
2. **`SessionPool.__aenter__`** (`:559-563`): `session = await pool.get(url, "POST", ...)`. `__SessionPool__.get` (`:509-532`): `host,port,path = ioConf.url_to_host(url, {})` (C ext); `key=(host,port,*proxy)`; find-or-create the `instances` list; `get_instance` returns a free one (its `available` SharpEvent is set), else create (under `max_contexts`) or pick `min(waiter_count)`. `async with instance.context:` (an `ioCondition`) → `await instance.context.wait()` serializes this physical socket. **Liveness gate** (`:525-531`) nulls a dead/closing/timed-out `protocol` so the next step reconnects. Returns the pooled `Session`.
3. **`session.__aenter__(False)`** then **`session.prepare(*args, **kwargs)`** (`:562-563`). `prepare` (`:246-254`) short-circuits if the protocol is alive and headers already sent; else `_connector` (`:259-271`): retry loop ≤ `max_retries=3`, but `ServerDisconnected` only retried for idempotent methods — **a POST that fails after send is NOT replayed** (no duplicate completions/billing). On success: `current_task().__BlazeioClientProtocol__ = app.protocol` so the body iterator can locate the connection from the running task.
4. **`create_connection`** (`:273-402`): C `url_to_host` for host/port/path; `DictView` normalizes headers case-insensitively without copying; `json=payload` → `dumps(json,indent=0).encode()` + `Content-type: application/json`, `Content-length` set (`:334-339`). `port==443` → cached process-global `SSLContext` (⚠ `CERT_NONE`). If no live protocol: `BlazeioClient(remote_host, remote_port, ssl=ssl)` → `create_connection` → `loop.create_connection(lambda: app, host, port)` → `TCP_NODELAY=1` + keepalive 30/10/3 on the raw socket (`:113-117`). **C `gen_payload`** assembles the entire request-line+headers byte string in one stack/heap buffer (`:370`); `if body: payload += body` (`:372`); **one `await protocol.push(payload)`** = one `transport.write` = one syscall for headers+body (`:375`). `configure_writer` picks raw push vs `write_chunked`.
5. **First `async for chunk in r`** → `Pulltools.pull` (`client_protocol_tools.py:710-723`): `is_prepared()` False → `prepare_http` → `_prepare_http` (`:565-593`) → `MinParsers.client.aparse(app)` (`min_http_parser.py:154-175`): loop `await r.protocol` (the buffered `__await__`) accumulating until `\r\n\r\n`, validate `HTTP/1.1`/`HTTP/1.0` present, parse status + **lowercased** headers, set `received_len/content_length/content_type`, choose `handler`: `transfer-encoding`→`handle_chunked`, `content-length`→`handle_raw`, else `protocol.pull`. Over-read body bytes `prepend`'d back (zero-copy un-read). If `content-encoding: gzip|br` and `decode_resp` (default True) → wrap handler in a streaming decompressor (`:571-578`). `follow_redirects` handled here (re-`_connector` on 3xx with `Location`, drops the connection if host changed, `:587-591`).
6. **Streaming the model output**: `pull` (`:718-723`) iterates `handler()` (or `decoder()`); `handle_chunked` (`:603-639`) decodes hex-size frames incrementally and **`yield`s each chunk the instant it's parsed** — no whole-body buffering, so first token reaches the caller as soon as the provider flushes it. Backpressure: the protocol `pause_reading()`s after every `buffer_updated` and only `resume_reading()`s when the consumer drained `__stream__` (`Protocols/__init__.py:45-54`) — a fast model can't balloon memory. Per chunk the only allocation is `bytes(memoryview[:nbytes])` into the `deque`.
7. **`__aexit__`** (`Client/__init__.py:213-242`): `close_on_exit=False` for pooled sessions, so the socket is **not** closed; instead the Session's `on_exit_callback=(pool.release, instance)` fires → `release` (`:461-466`): `acquires-=1`, `available.set()`, `perf_counter=now`, `context.notify(1)` — the warm connection returns to the pool for the next LLM call. `clean_instance` on the 30s `ReMonitor` timer reaps it only if idle > `timeout=60s`.

**Concurrency model:** N concurrent `getSession.post(...)` to the same provider share the per-host `instances` list. Each physical connection serves **one request at a time** (`async with instance.context`). To get C-way concurrency you need `max_contexts ≥ C` connections (default 2 — raise for LLM fan-out). Selection is least-in-flight (`min(waiter_count)`), so load spreads evenly. There is **no HTTP/2 multiplexing** — this is the main scaling ceiling vs httpx; Lancelot should add h2 so one socket carries many concurrent completions.

**Worked example — 50 concurrent streaming completions, Blazeio vs Lancelot-target:**
- Blazeio: needs ≥50 sockets to the provider (one in-flight req each); 50 preallocated 100 KB recv buffers (~5 MB, bounded — good); 50 `reTask`/Task; pool reaps them 60s after the burst. TLS unverified by default (must override). No per-request deadline unless the watchdog thread catches it. Works, but socket-heavy and one slow stream holds its whole connection.
- Lancelot-target: 2–4 HTTP/2 sockets, 50 concurrent streams multiplexed; same one-buffer-per-connection recv discipline (so ~4 × 100 KB, not 50); `asyncio.wait_for` deadline per stream; verified TLS; `min(in_flight)` stream assignment per connection; orjson body (no extra `.encode()` copy). Same TTFT behavior (yield-on-arrival), far fewer FDs, safe defaults.

---

## 11. The buffered protocol state machine — full detail (server + client share this)

`BlazeioProtocol` (`Protocols/__init__.py:4-67`) is the shared base of both `BlazeioServerProtocol` and `BlazeioClientProtocol`. Complete state/transition table — this is the recv engine to reimplement:

| Callback (asyncio → protocol) | Code | Effect |
|---|---|---|
| `connection_made(transport)` | `client_protocol.py:42-44` / `server_protocol.py:47-51` | `transport.pause_reading()` immediately (no bytes until a reader awaits); store transport; server also spawns the `transporter` task and exposes `.cancel` |
| `get_buffer(sizehint)` | `:11-18` | return `__buff__memory__[:sizehint]`; grow `__buff__` only if `sizehint` exceeds it (rare). **No allocation in steady state.** |
| `buffer_updated(nbytes)` | `:6-9` | `pause_reading()`; `__stream__.append(bytes(__buff__memory__[:nbytes]))` (the one mandatory copy — buffer is about to be reused); `__evt__.set()` (wake reader) |
| `eof_received()` | `:25-27` | `__is_at_eof__=True`; `__evt__.set()` |
| `connection_lost(exc)` | `:20-23` | `__evt__.set()`; `__overflow_evt__.set()`; if `cancel and cancel_on_disconnect`: `cancel()` |
| `pause_writing()` | `:29-30` | `__is_buffer_over_high_watermark__=True` (transport send buffer full) |
| `resume_writing()` | `:32-34` | clear flag; `__overflow_evt__.set()` |

Consumer side (the reader coroutine):

| Method | Code | Effect |
|---|---|---|
| `__await__` | `:59-61` | `yield from ensure_reading()`; return `__stream__.popleft()` or `None` (single-chunk read) |
| `__aiter__` | `:63-67` | loop: `await ensure_reading()`; drain `while __stream__: yield popleft()` (streaming read) |
| `ensure_reading` | `:45-54` | if transport open and `__stream__` empty: if not reading → `resume_reading()`; `await __evt__.wait_clear()`. If transport closing → `abort_connection()` |
| `prepend(data)` | `:36-39` | `pause_reading()` if reading; `__stream__.appendleft(data)`; `__evt__.set()` — **zero-copy un-read** (push parsed-but-unconsumed body back) |
| `buffer_overflow_manager` | `:41-43` | before a write: if over high-watermark, `await __overflow_evt__.wait_clear()` — **outbound backpressure** so a slow consumer of *our* writes (e.g. slow downstream while proxying an LLM stream) can't OOM us |
| `writer(data)` | `client_protocol.py:61-67` | `await buffer_overflow_manager()`; if transport open `transport.write(data)` else `abort_connection()` (raises `ServerDisconnected`) |

The cycle: read exactly one buffer → pause → copy to deque → signal → consumer drains deque → consumer calls `ensure_reading` → resume → repeat. **Memory is bounded to ~one `__chunk_size__` (100 KB) per idle connection** no matter how fast the peer pushes — the property that makes thousands of concurrent LLM streams safe. `__overflow_evt__` provides the symmetric outbound bound. Both events are `SharpEvent` (single shared future, already-set fast path) so the per-read overhead is one already-resolved-future check in the common case.

**Reimplement for Lancelot:** this exact 7-callback/6-method machine on `asyncio.BufferedProtocol`, with `SharpEvent`-style signals, is the recv core. The only changes: (a) for HTTP/2 the "one buffer" is per-connection but frames are demuxed to per-stream deques; (b) enforce a per-read/per-request deadline at `ensure_reading` via `asyncio.wait_for(self.__evt__.wait(), timeout)` instead of relying on an external watchdog.

---

## 12. Concurrency primitives for AI fan-out — `TaskPool`, `Asynchronizer`, `Enqueue`

An AI backend's other hot pattern (besides one outbound call) is **bounded concurrent fan-out**: "embed these 500 chunks", "call the model 20× in parallel for a map-reduce", "stream from N tools at once". Blazeio ships three primitives for this in `alts.py`/`Dependencies`.

### 12.1 `TaskPool` — bounded concurrent task pool (`alts.py:290-357`)

`__slots__ = ("taskpool","task_activity","task_under_flow","loop","maxtasks","listener_task","timeout")`. A semaphore-like bounded spawner built on `ioCondition`:

```python
async def create_task(app, *args, **kwargs):
    async with app.task_under_flow:                     # ioCondition
        app.task_activity.set()
        await app.task_under_flow.wait()                # blocks until a slot frees
    task = app.loop.create_task(*args, **kwargs)
    app.taskpool.append(task)
    if app.timeout:
        task.__taskpool_timer_handle__ = app.loop.call_later(app.timeout, task.cancel)  # per-task deadline
    task.__taskpool_callbacks__ = [app.done_callback]
    task.__taskpool_add_callback__ = lambda f, t=task: app.add_call_back(t, f)
    task.add_done_callback(app.done_callback_orchestrator)
    return task
```

A background `listener()` (`:313-320`) waits on `task_activity`, and whenever `len(taskpool) < maxtasks` calls `task_under_flow.notify(available)` — releasing exactly `available` blocked `create_task` callers. `done_callback` (`:322-324`) removes the finished task and pings `task_activity` (freeing a slot) and cancels the per-task timeout handle. `TaskPoolManager` (`:359-369`) is the `async with` wrapper: `__aexit__` does `await pool.gather()` then `pool.close()` — **structured concurrency**: leaving the block awaits all spawned tasks. `maxtasks` default 100, optional per-task `timeout` via `loop.call_later(timeout, task.cancel)`.

**This is the right shape for LLM fan-out.** Steal: bounded spawn (don't fire 500 model calls at once — cap at `maxtasks`), optional per-task deadline, and the `TaskPoolManager` "exit awaits all" structured-concurrency wrapper. **For Lancelot**, `asyncio.Semaphore` + `asyncio.TaskGroup` (3.11+) gives the same semantics with stdlib structured concurrency and exception aggregation — prefer that over the bespoke `ioCondition` machine, but keep the *API* (`async with pool: pool.create_task(model_call(x))`) and the per-task timeout.

### 12.2 `Asynchronizer` — run work on a second event loop in a thread (`alts.py:193-288`)

A worker **thread with its own event loop** that executes submitted jobs (sync or async) and signals completion back to the caller's loop:

```python
async def job(app, func, *args, **kwargs):
    job = {"func":func,"args":args,"kwargs":kwargs,"exception":None,"result":NotImplemented,
           "event":SharpEvent(True),"loop":get_event_loop(),"current_task":current_task()}
    app.loop.call_soon_threadsafe(app.jobs.put_nowait, job)          # hand to worker loop
    await wrap_future(run_coroutine_threadsafe(job["event"].wait(), app.loop))  # await across loops
    if job["exception"]: raise job["exception"]
    return job["result"]
```

`worker()` (`:236-251`) drains an `asyncio.Queue`; sync funcs run inline on the worker loop, async funcs `create_task(async_tasker(job))`. The cross-loop wakeup uses `run_coroutine_threadsafe` + `wrap_future`. Use case: offload CPU-ish or blocking work (tokenizer, large JSON, sync SDK) off the request loop without `to_thread`'s per-call thread overhead — a persistent worker loop. **For Lancelot:** `asyncio.to_thread` / a `ThreadPoolExecutor` covers most cases more simply; the dedicated-worker-loop pattern is only worth it for a steady stream of offloaded jobs (e.g. a sync tokenizer called on every request). Note the cross-loop `run_coroutine_threadsafe`+`wrap_future` bridge is the reusable technique (also how the threaded logger and `ReMonitor` signal back, `Dependencies/__init__.py:433,622,681`).

### 12.3 `Enqueue` — the backpressured async queue (`Dependencies/__init__.py:289-353`)

`__slots__`-based `deque` + two `SharpEvent`s + an overflow `Condition`, with a background `check_overflow` task (`:327-333`) that `notify`s the underflow condition whenever `len(queue) <= maxsize`. `put` (`:340-347`) blocks on `queueunderflow.wait()` until space, then appends + sets the activity event; `get`/`get_one` (`:335-352`) await the add-event then drain. It's `asyncio.Queue` with the Blazeio event primitives and an explicit overflow controller. Powers the threaded logger's pipe. **For Lancelot:** stdlib `asyncio.Queue(maxsize=)` already gives bounded backpressure; only reach for a custom one if you need the `notify(available)` batch-release semantics (release N producers at once) which `asyncio.Queue` lacks.

### 12.4 The watchdog as a timeout mechanism — and why to avoid it

`__ReMonitor__` (`Dependencies/__init__.py:546-685`) is a singleton **thread + its own loop** that every `__timeout_check_freq__=30s` iterates `all_tasks(loop)` for each registered server, and for any task carrying `__BlazeioProtocol__` checks `transport.is_closing()` (disconnect) and `perf_counter - __perf_counter__ >= __timeout__` (default `60*10 = 600s`, `SrvConfig:28`) → `task.cancel()`. It also runs the pool's `clean_instance` callbacks via `add_callback` (`:611-613`). This is how Blazeio enforces request/idle timeouts and reaps pooled connections **out-of-band**. The flaw for an AI backend: (a) 30s granularity is coarse for sub-second SLAs; (b) `reTask`s aren't in `all_tasks()` so they're invisible to it; (c) it cancels by walking *all* tasks, O(tasks) every tick. **Lancelot DESIGN:** enforce deadlines *inline* with `asyncio.wait_for`/`asyncio.timeout()` per request (precise, in-band, visible), and reap pool connections with a per-connection `loop.call_later` (Blazeio already does this for `clean_instance` via `ReMonitor.add_callback` — just do it directly on the main loop). Keep a watchdog only for "whole-worker stuck" detection, not per-request timeouts.

---

## 13. Net architecture recommendation for Lancelot (synthesis)

The "faster FastAPI for startups + AI" wedge, assembled from what to steal:

- **Routing:** exact-match dict (O(1), Blazeio's core win over Starlette's regex list) **+** a compiled segment trie for `{param}` routes checked only on dict miss (Robyn-class capability). Optional "function-name → route" sugar. No per-request Pydantic unless the handler opts in (validation is opt-in, not mandatory tax) — but offer a typed-handler decorator that does `msgspec`/`pydantic-core` validation when present (msgspec is faster than pydantic-core for pure validation).
- **Server recv:** Blazeio's `BufferedProtocol` one-buffer + `pause_reading` backpressure + `prepend` un-read, verbatim. `SharpEvent`-style signals. uvloop soft-default.
- **Outbound client (the AI hot path):** Blazeio's pool hygiene (per-host key, stale-detect-before-handout, idle-reap-below-provider-timeout, least-in-flight selection, **no POST auto-retry**) **+** HTTP/2 multiplexing **+** verified TLS **+** in-band `asyncio.timeout` deadlines **+** DNS cache **+** orjson (bytes, no extra encode). Same yield-on-arrival streaming.
- **Concurrency:** `asyncio.TaskGroup` + `Semaphore` with Blazeio's `TaskPool` *API* and per-task deadline, for bounded LLM fan-out.
- **Native accel:** one Rust/PyO3 (not hand C) module for request-line/header serialization + URL/percent-coding, behind a single indirection point, with an always-correct pure-Python fallback and a **logged slowdown factor** when degraded (steal Blazeio's operator-honesty pattern; reject its overflowable C and self-rewriting build script).
- **Primitives:** `SharpEvent` (read-ready), `RecyclableEvent` (§9.4, only the byte-ready edge, post-benchmark), `DictView` (case-insensitive headers no-copy), `memarray` (explicit zero-copy slicing), `ddict` (universal record). Keep vanilla `asyncio.Task`/`contextvars`/structured cancellation everywhere else.
- **Reject wholesale:** `reTask`, 7-mixin tower + manual MRO `Super`, `Taskscope`-on-task, `CERT_NONE`, external-watchdog timeouts, exception-by-classname compare, HTTP/1.1-only client.

This gives Blazeio's allocation/throughput behavior with FastAPI's ergonomics-on-demand and safe defaults, and a Robyn-class router — the actual "faster FastAPI" position.

---

## 13a. Serving LLM output downstream — the response writer the client mirrors

The other half of an AI backend: stream the model's tokens *to your own client* (SSE / chunked). Blazeio's server writer (`Protocols/server_protocol_extratools.py:156-352`, `Modules/streaming.py`) is the emitter Lancelot needs, and it's the symmetric twin of the client's `Pushtools` (`client_protocol_tools.py:650-691`).

**Writer selection at `prepare`** (`server_protocol_extratools.py:306-341`): `prepare(headers, status)` writes the status line + headers, then **picks `app.write` by header**:
- `Transfer-encoding: chunked` → `app.write = write_chunked`
- `Content-length` present → `write_raw`
- `Content-type: text/event-stream` (+ `encode_event_stream`) → `write_event_stream`
- else → `write_raw`

So a handler does `await Prepare.text(..., chunked=True)` (or sets `Content-type: text/event-stream`) once, then every `await r.write(token)` is automatically framed correctly. **`write_event_stream`** (`:213-222`):
```python
async def write_event_stream(app, data, start=b"data: ", end=b"\n\n"):
    if app.encoder: data = await app.encoder(data)
    if isinstance(data, (dict, list)): data = dumps(data, indent=0).encode()
    if data[:len(start)] != start: data = b"%b%b%b" % (start, data, end)   # SSE-frame if not already
    return await app.writer(data)
```
That is a complete SSE emitter: pass a dict/str token, it `data: …\n\n`-frames it (idempotent — won't double-frame if you already framed). **`write_chunked`** (`:200-211`) emits `b"%X\r\n%s\r\n" % (len(data), data)` per chunk and `0\r\n\r\n` on `eof` — HTTP/1.1 chunked, the exact inverse of the client's `handle_chunked` decoder (§4.6). `StreamJson` (`Modules/streaming.py:183-199`) brackets a generator with `[\n` / `,\n` / `\n]` to stream a JSON array element-by-element (useful for streaming a list of model results without buffering). `Deliver`/`Abort`/`Prepare` (`Modules/streaming.py:45-131`) are the handler-facing response API; `Abort` *is* a raised response (caught at `handle_client:218`, `.text(r)` writes it).

**For Lancelot:** steal the **header-driven writer selection at prepare-time** (pick the framing function once, not per-write) and the **idempotent SSE framer**. This is exactly how to relay an upstream LLM stream to a downstream browser: `async for tok in upstream: await r.write(tok)` where `r.write` is auto-selected `write_event_stream`. Combined with the client's `pipe_to`/`forward_to_client` (`Modules/streaming.py:170-181`) you get zero-buffer upstream→downstream LLM proxying: `async for chunk in upstream_session: await downstream.write(chunk)` — neither side materializes the full completion. Backpressure on both legs (`buffer_overflow_manager`, §11) keeps memory bounded even if the browser is slower than the model.

## 13b. Decorator & OOP registration mechanics (`__init__.py:52-91, 254-321`)

Two registration front-ends over the same `app.add_route`:
- **`Add_Route`** (`:254-291`, exposed as `app.route` lazily via `Protocol_methods.__getattr__:643-645`): `@app.route.get("/path")` / `@app.route.post(...)`. `__getattr__` (`:269-276`) turns any HTTP-method name into a decorator; `add_route` wraps the handler in `wrapped_func` that enforces the method (`before_func` raises `Abort("Method not allowed",405)` on mismatch, `:279-281`) then calls `app.web.add_route(wrapped_func, func, path)`. Bare `@app.route` → method `"ALL"` (`:266-267`).
- **`OOP_Route_Def.attacher`** (`:56-82`): `app.attach(SomeClass)` introspects a class — every public method not starting `_` (unless dunder) and not ending `_middleware` whose signature has an `r` param becomes a route via `app.add_route(method, route_name=name)`; `__main_handler__` / `__server_instance__` are special-hooked. This is the "a class is a router, its methods are routes" ergonomic.

Both converge on `add_route` → the `OrderedDict`. `Routemanager`/`rawRoutemanager` (`:293-336`) are alternative `ddict`-subclass registries used by sub-apps. **For Lancelot:** the method-name-as-decorator (`@app.route.post`) is clean; keep it. The class-introspection attacher is cute but signature-introspection-per-method at startup is opaque and fragile (depends on the param being literally named `r`) — offer explicit decorators as the primary API and class-based views as an opt-in with explicit route declarations, not name-sniffing.

## 14. Evidence index (file:line)

- App / MRO / dispatch: `Blazeio/__init__.py:657` (App bases), `:721-776` (`add_route`/`route_validator`), `:158-191` (serve_route ±middleware), `:207-232` (`handle_client` exception boundary), `:127-140` (startup dispatch specialization), `:142-156` (default parser).
- Routing helpers: `Modules/server_utilities.py:5-26` (WildcardRouteMatcher), `Itools/__init__.py:83-102` (Multirouter), `Dependencies/__init__.py:78-81` (funcname normalizers).
- Custom futures: `Other/_refuture.py:9-98` (`reFuture` self-recycle via `flush`/`restart`/`_mortality`), `:100-148` (`reTask`), `:150-153` (`tempFut`).
- Events/conditions: `Dependencies/__init__.py:146-200` (`SharpEvent`), `alts.py:50-101` (`SharpEventLab`), `alts.py:103-191` (`ioCondition`, least-loaded support).
- Stdlib swaps: `Dependencies/__init__.py:46-50` (ujson/json), `:480-498` (uvloop/asyncio + aiofile via `optional_module_importer`), `:202-288` (`__Scope__`/`Taskscope`), `:354-478` (threaded logger + `Enqueue`), `alts.py:586-660` (`Ehandler`).
- Client: `Client/__init__.py:80-125` (BlazeioClient + TCP/TLS opts), `:147-454` (Session build/retry/writer), `:456-532` (`__SessionPool__` get/release/clean), `:534-664` (SessionPool/createSessionPool/getSession), `Protocols/client_protocol.py:6-69` (protocol), `Protocols/__init__.py:6-67` (buffered recv/backpressure/prepend), `Protocols/client_protocol_tools.py:46-102` (streaming decompressors), `:565-648` (prepare_http/handle_chunked/handle_raw), `:693-905` (Pulltools consumer API), `Parsers/min_http_parser.py:99-175` (client response parser).
- C exts: `Extensions/client_payload_gen.c:17-162` (`gen_payload`), `Extensions/Blazeio_iourllib.c:4-120` (`url_to_host`), `Extensions/c_request_util.c:20-175` (url decode/encode/params; **overflow risk :28**), `Extensions/setup_build.py:1-46`, `setup.py:41-61`, fallback wiring `Dependencies/__init__.py:134-144` + `client_protocol_tools.py:326-347`.
- Itools: `Itools/__init__.py:6-35` (ddict), `:104-148` (memarray/zcbuff), `:170-191` (Super), `:260-313` (DictView/dotdictView), `Other/algorithms.py:4-80` (Perf bench).

*RE complete. Pure source analysis of the local Blazeio tree; no network probes (Blazeio is a library, not a hosted product). Labels: KNOWN = read in source; INFERRED = mechanism-deduced; DESIGN = Lancelot recommendation.*
