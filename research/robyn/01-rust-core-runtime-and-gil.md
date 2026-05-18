# Robyn Rust Core, FFI Boundary & GIL Strategy — The Single Most Important Reference for Lancelot's Core

> Source-level RE of Robyn 0.85.0 (`/Users/seventyleven/Desktop/researchfms/robyn/Robyn`). Every claim cites `path:line`. Labels: **KNOWN** = read directly from source; **INFERRED** = deduced from source + runtime semantics; **DESIGN** = recommendation for Lancelot, not present in Robyn.
>
> Robyn IS the architecture Lancelot wants (Rust core + Python API, "faster FastAPI"). This document mines the runtime/GIL/FFI axis exhaustively. Object-conversion details (Request/Response marshalling field-by-field) are deferred to the routing/types agent — here we focus on the *call mechanism* and the *GIL*.
>
> Baseline `teardowns/ROBYN.md` (1268 lines) is the floor. This goes deeper on the one axis that decides Lancelot's core: how Rust calls Python, where the GIL is held, and what to build instead.

---

## Executive Summary (the recommended Lancelot core, in 12 lines)

1. **Stack:** `pyo3 0.27.x` (`extension-module`, `py-clone`) + `pyo3-async-runtimes 0.27` (`tokio-runtime`) + `tokio 1.40+ "full"` + an HTTP engine. Robyn picks `actix-web 4.4` — for Lancelot prefer **`hyper 1.x` + `tower` directly** (lower abstraction, no actor system, no `actix::System` global), but actix-web is a proven shortcut. Build with `lto="fat"`, `codegen-units=1`, `panic="abort"`, `opt-level=3` (Robyn `Cargo.toml:46-51` — KNOWN).
2. **Process model:** one OS process per CPU = one CPython interpreter = one GIL. Bind ONE shared listening socket with `SO_REUSEPORT`+`SO_REUSEADDR`+`TCP_NODELAY`, `listen(backlog=16384)`, pass the cloned fd into each forked process; the kernel load-balances accept across processes (Robyn `shared_socket.rs:23-29`, `processpool.py:107-128` — KNOWN). This is the *only* real CPU-scaling mechanism; threads don't scale Python.
3. **HTTP I/O never touches the GIL.** Accept, TLS, HTTP/1.1+HTTP/2 parsing, header framing, body read, response write — all pure Rust/tokio. The GIL is acquired *only* to (a) build handler args, (b) call the handler / create the coroutine, (c) marshal the return value. Everything else is GIL-free (Robyn `server.rs:168` parse path, `executors/mod.rs:519-545` — KNOWN/INFERRED).
4. **`Python::with_gil` is held for microseconds, three times per dynamic request**, never across an `.await`. Async path: acquire GIL → call handler → get coroutine → `into_future` → **drop GIL** → tokio awaits the Python future (GIL free, worker runs other requests) → reacquire GIL → marshal result (`executors/mod.rs:524-535` — KNOWN). This single discipline is the entire reason a Rust-core Python framework beats uvicorn.
5. **Sync handlers run on a dedicated blocking-thread pool**, not on the async workers. Robyn ships a Granian-derived `BlockingRunner` (Empty / Mono / Pool variants) fed by a `crossbeam-channel` MPMC queue; each worker thread holds the GIL via `Python::attach` and *releases it* (`py.detach`) while blocked on `queue.recv()` (Robyn `blocking.rs:34-177` — KNOWN). Lancelot must replicate this: blocking Python on the async reactor is fatal.
6. **Custom awaitables beat the library.** Robyn hand-rolls 5 `#[pyclass]` awaitables (`PyEmptyAwaitable`, `PyDoneAwaitable`, `PyErrAwaitable`, `PyIterAwaitable`, `PyFutureAwaitable`) that are *"~38–55% faster than `pyo3_asyncio.future_into_py`"* (Robyn `runtime.rs:163,182` verbatim comment — KNOWN). They avoid per-call Python `Future` allocation and a `call_soon_threadsafe` round-trip. Lancelot should adopt the same pattern from day one.
7. **One `contextvars.Context` per request lifecycle**, allocated once via the CPython C-API `PyContext_CopyCurrent` / `Context()` and threaded through before/handler/after phases so `ContextVar` writes are visible across middleware and don't leak between requests on a reused worker thread (Robyn `asyncio.rs:32-49`, `server.rs:515-522`, fix for issue #1380 — KNOWN). Sync handlers run inside `ctx.run(...)`; async handlers via `loop.create_task(coro, context=ctx)`.
8. **Const/cached routes bypass Python entirely.** When no middleware is registered, a matched const route returns a pre-baked `HttpResponse` from a Rust map with **zero GIL acquisitions, zero Request allocation, zero handler call** (Robyn `server.rs:231-251` — KNOWN). Lancelot should expose an explicit "static/const route" tier — it's free throughput.
9. **`TaskLocals` is created once (`OnceCell`), cloned per request under a cheap GIL.** The asyncio event loop + copied context is captured at startup; each request does `Python::with_gil(|py| TASK_LOCALS.get().clone_ref(py))` (one refcount bump under GIL) then `scope_local` to bind it to the tokio task (Robyn `server.rs:92,120-124,255-273` — KNOWN).
10. **Streaming is the weak point.** Robyn pumps a Python generator by calling `gen.__next__()` inside `spawn_blocking` + `Python::with_gil` **per chunk** (Robyn `response.rs:128-146` — KNOWN). For AI/LLM token streaming this is one GIL acquisition + one blocking-pool hop *per token* — see §10. Lancelot must do better (async-generator native pump, batched chunking).
11. **Top GIL/FFI pitfalls:** (a) holding the GIL across `.await` (serializes the whole process); (b) running sync/CPU-bound or blocking Python on the async workers (starves the reactor); (c) per-chunk GIL churn on streaming (kills LLM token throughput under concurrency). All three are avoidable and Robyn avoids 1+2 but only partially 3.
12. **Net for Lancelot:** Rust core + Python API is *strictly* faster than ASGI/uvicorn for the hot path *because the entire non-handler request lifecycle is GIL-free Rust*; the GIL only ever serializes the handler body itself, which is unavoidable in any CPython framework. The win is real but bounded by Amdahl: if handlers are pure-Python CPU, you're back to multiprocess scaling. For AI workloads (handlers are mostly `await openai(...)` I/O) the architecture is close to ideal — the GIL is released for the entire outbound LLM call.

---

## 0. The exact crate stack and build profile (KNOWN — `Cargo.toml`)

Verbatim from `robyn/Robyn/Cargo.toml:16-54`:

```toml
[dependencies]
pyo3 = { version = "0.27.2", features = ["extension-module", "py-clone"]}
pyo3-async-runtimes = { version = "0.27", features = ["tokio-runtime"] }
pyo3-async-runtimes-macros = { version = "0.27" }
pyo3-log = "0.13.2"
tokio = { version = "1.40", features = ["full"] }
dashmap = "5.4.3"
anyhow = "1.0.69"
actix = "0.13.4"
actix-web-actors = "4.3.0"
actix-web = "4.4.2"
actix-http = "3.3.1"
actix-files = "0.6.2"
futures = "0.3.27"
futures-util = "0.3.27"
matchit = "0.7.3"
socket2 = { version = "0.5.1", features = ["all"] }
uuid = { version = "1.3.0", features = ["serde", "v4"] }
log = "0.4.17"
pythonize = "0.27"
serde = "1.0.187"
serde_json = "1.0.109"
once_cell = "1.8.0"
actix-multipart = "0.6.1"
parking_lot = "0.12.3"
crossbeam-channel = "0.5"

[features]
io-uring = ["actix-web/experimental-io-uring"]

[profile.release]
codegen-units = 1
lto = "fat"
panic = "abort"
strip = true
opt-level = 3
```

`crate-type = ["cdylib", "rlib"]` (`Cargo.toml:14`) — builds a C-ABI dynamic library loadable by CPython *and* an rlib (so it can be linked as a normal Rust crate, e.g. for tests/benches). `[lib] name = "robyn"` and `module-name = "robyn"` in `pyproject.toml:152` — the compiled `.so` is imported as `from robyn.robyn import Server, SocketHeld, ...` (see `lib.rs:37-69` `#[pymodule] fn robyn`).

### What each crate does on the runtime/GIL axis

| Crate (version) | Role | Lancelot verdict |
|---|---|---|
| `pyo3 0.27.2` (`extension-module`, `py-clone`) | The entire FFI. `extension-module` = link into a pre-existing CPython, do **not** link libpython statically (lets the wheel be portable across patch versions; the host interpreter provides the symbols). `py-clone` enables `Py<T>::clone()` (used pervasively for `clone_ref`). | **Use pyo3 0.27.x exactly.** `extension-module` is mandatory for a pip-installable wheel. Pin the minor — pyo3 makes breaking API changes per minor (0.27 changed `IntoPyObject`, `FromPyObject<'a,'b>`, `Bound` API vs 0.20). |
| `pyo3-async-runtimes 0.27` (`tokio-runtime`) | The asyncio↔tokio bridge: `into_future` (Python awaitable → Rust `Future`), `scope_local`/`TaskLocals` (bind a Python event loop + `contextvars.Context` to a tokio task), `run_until_complete`, `into_future_with_locals`. This is the *only* sanctioned way to drive a Python `async def` from inside a Rust async task. | **Use it, but only for the slow path.** Robyn's own benchmark comment says its hand-rolled awaitables are 38–55% faster than this crate's `future_into_py` (`runtime.rs:163,182`). Use `pyo3-async-runtimes` for `into_future` (Python coro → Rust future) but build custom awaitables for Rust future → Python (the response side). |
| `pyo3-async-runtimes-macros 0.27` | `#[pyo3_async_runtimes::tokio::main]`-style macros. Not used in the hot path. | Optional. Skip. |
| `pyo3-log 0.13.2` | Bridges Rust `log::` → Python `logging`. `pyo3_log::init()` called once at `Server::start` (`server.rs:90`). | Nice-to-have. Adds a GIL acquisition per log call if logging is hot — keep Rust logging off the request path or use a Rust-native subscriber for high-volume logs. |
| `tokio 1.40` (`full`) | Async runtime + timers + net + sync + `spawn_blocking`. **Critical nuance (§2):** Robyn does NOT spin tokio's multi-thread runtime for HTTP. It runs `actix_web::rt::System` (actix's own single-threaded-per-worker runtime) and only uses a `new_current_thread` tokio runtime in the Granian-derived helper (`runtime.rs:137-143`). | **For Lancelot, drive your own `tokio::runtime::Builder::new_multi_thread()`** with explicit `worker_threads` and `max_blocking_threads` — don't inherit actix's `System` model unless you adopt actix-web wholesale. |
| `actix-web 4.4.2` / `actix-http 3.3.1` / `actix-files 0.6.2` / `actix-multipart 0.6.1` | The HTTP engine: `HttpServer`/`App`, HTTP/1.1+HTTP/2 parsing, static files, multipart. **Not raw hyper, not tower.** | **Lancelot decision point.** actix-web gives you a batteries-included server fast. But it brings the actix actor `System`, a per-worker single-thread arbiter model, and a large dep tree. For a *new* framework, `hyper 1.x` + `tower` (or `axum`'s internals) is leaner and gives you direct control of the runtime — see §11. |
| `actix 0.13.4` + `actix-web-actors 4.3.0` | The actor system, used only for WebSockets (each connection is an `Actor`). | Only needed if you take actix-web. With hyper you'd use `tokio-tungstenite` for WS. |
| `socket2 0.5.1` (`all`) | Raw socket creation for `SO_REUSEPORT` multi-process socket sharing. | **Use socket2 0.5.x verbatim** (`shared_socket.rs`). This is the load-balancing primitive. |
| `crossbeam-channel 0.5` | The MPMC queue feeding the blocking-thread pool (`blocking.rs`). | **Use crossbeam-channel** for the sync-handler offload queue. `unbounded()` is what Robyn uses; consider bounded for backpressure (see §5 pitfalls). |
| `dashmap 5.4.3` / `parking_lot 0.12.3` / `once_cell 1.8.0` | Concurrent map (routers/headers), faster `RwLock`/`Mutex`, lazy statics. | Use all three. `once_cell::sync::OnceCell` is how `TASK_LOCALS` is held (`server.rs:92`). |
| `anyhow 1.0` | Error type for the async pipeline (`Result<(), anyhow::Error>`). | Use `anyhow` at the pipeline boundary, concrete errors internally. |

### Build profile — why it matters for a Rust core (KNOWN, `Cargo.toml:46-54`)

- `lto = "fat"` + `codegen-units = 1` — maximal cross-crate inlining. For an FFI library where the hot path crosses `pyo3` generic monomorphized functions (`get_function_output<T>`, `extract_response_type_fast`), fat LTO is worth real latency. **DESIGN: Lancelot must ship with the same profile** — without it the `into_pyobject`/`extract` generic chains don't inline and per-request overhead measurably rises.
- `panic = "abort"` — **architecturally load-bearing.** A Rust panic does NOT unwind; it `abort()`s the whole OS process. Consequence: every `unwrap()`/`expect()` on the request path is a process-kill bug. Robyn's discipline is to *never panic in request handling* and convert all errors to `Response::internal_server_error` (`server.rs:497-500,517-520,581-586`). The cost saving: no landing-pad code, smaller binary, faster calls. **DESIGN for Lancelot: adopt `panic="abort"` AND a `catch_unwind` boundary** around the Python call only (so a panic *inside pyo3 marshalling* — which can happen — returns 500 instead of killing the worker; Robyn does NOT do this and is fragile here). pyo3 itself converts Python exceptions to `PyErr` (not panics), but a `Py<T>` misuse or a poisoned lock *will* panic — `catch_unwind` at the FFI seam is the safety net Robyn lacks.
- `strip = true` — smaller `.so`. Cosmetic; keep.
- `[profile.release.build-override] opt-level = 3` — build scripts/proc-macros optimized too (compile-time only).

---

## 1. The process & socket model — the only thing that actually scales CPython (KNOWN)

### 1.1 Why this is THE decision for a Rust-core Python framework

The GIL means **one CPython interpreter executes Python bytecode on exactly one core at a time**. No amount of Rust threading changes this: the moment a request's handler runs `await db.query()` or `x + y` in Python, that's serialized against every other Python-executing request *in the same interpreter*. The Rust core makes the *non-Python* part of the request (parse/route/marshal/write) parallel and GIL-free, but the handler body is still single-threaded per interpreter.

Therefore the ONLY way to use N cores for Python work is **N OS processes, each with its own interpreter and its own GIL**, all sharing one listening socket. Robyn does exactly this. Lancelot must too. Threads (`workers` in Robyn terms) only help the *Rust I/O side*; processes (`processes`) are what scale Python.

### 1.2 The shared socket (`shared_socket.rs:1-44` — KNOWN, verbatim)

```rust
#[pyclass]
pub struct SocketHeld { pub socket: Socket }   // socket2::Socket

#[pymethods]
impl SocketHeld {
    #[new]
    pub fn new(ip: String, port: u16) -> PyResult<SocketHeld> {
        let ip: IpAddr = ip.parse()?;
        let socket = if ip.is_ipv4() {
            Socket::new(Domain::IPV4, Type::STREAM, Some(Protocol::TCP))?
        } else {
            Socket::new(Domain::IPV6, Type::STREAM, Some(Protocol::TCP))?
        };
        let address = SocketAddr::new(ip, port);
        #[cfg(not(target_os = "windows"))]
        socket.set_reuse_port(true)?;        // SO_REUSEPORT — kernel-level LB across procs
        socket.set_reuse_address(true)?;     // SO_REUSEADDR  — fast restart
        socket.set_nodelay(true)?;           // TCP_NODELAY   — no Nagle, low latency
        socket.bind(&address.into())?;
        socket.listen(16384)?;               // backlog = 16384 (huge — burst tolerance)
        socket.set_nonblocking(true)?;       // required for async accept loop
        Ok(SocketHeld { socket })
    }
    pub fn try_clone(&self) -> PyResult<SocketHeld> {       // dup() the fd
        let copied = self.socket.try_clone()?;
        Ok(SocketHeld { socket: copied })
    }
}
impl SocketHeld {
    pub fn get_socket(&self) -> Socket { self.socket.try_clone().unwrap() }
}
```

**Mechanism:** `SO_REUSEPORT` (Linux 3.9+, also BSD/macOS) lets multiple processes each `bind()` the *same* `ip:port`. The kernel maintains one accept queue per listening socket and hashes incoming connections (4-tuple) across them. This is **kernel-space connection load-balancing with zero userspace coordination** — strictly better than a single acceptor process handing fds to workers (no fd-passing, no thundering herd on a shared queue, NUMA-friendly because each process accepts its own connections).

**Why it's fast:** the load-balancing is done by the kernel's TCP stack at `SYN` time; there is no lock, no channel, no master process bottleneck. `backlog=16384` means the kernel can hold a large burst of half-open/accepted-but-not-yet-`accept()`ed connections without `RST`ing clients during a latency spike. `TCP_NODELAY` removes the up-to-40ms Nagle delay (critical for small JSON / streaming chunks). `set_nonblocking(true)` is mandatory: the async runtime polls readiness via epoll/kqueue and must never block in `accept()`.

**The fd-clone trick (`shared_socket.rs:34-37`, `processpool.py:107-128` — KNOWN):**

```python
# processpool.py
socket = SocketHeld(url, port)                 # bind ONCE in the parent
for _ in range(processes):
    copied_socket = socket.try_clone()          # dup() -> new fd, same kernel socket object
    process = Process(target=spawn_process, args=(..., copied_socket, workers, ...))
    process.start()                             # multiprocess (dill) pickles the fd-holding pyclass
```

Note Robyn binds *once in the parent* then `try_clone()`s (a `dup()`); with `SO_REUSEPORT` set, even independent `bind()`s would work, but cloning the already-bound fd is simpler and means all children share the exact same kernel socket. `multiprocess` (the `dill`-backed fork of stdlib `multiprocessing`) is required because the `SocketHeld` pyclass and the route closures must be **pickled across the fork** — stdlib `pickle` can't serialize the handler closures, `dill` can (`processpool.py:6`, baseline note `ROBYN.md:99` — KNOWN).

### 1.3 Default scaling constants (KNOWN — `argument_parser.py:106-112`)

```python
# "fast" mode (auto-tuned):
cpu_count = os.cpu_count() or 1
self.processes = self.processes or ((cpu_count * 2) + 1) or 1   # ≈ 2N+1 processes
self.workers   = self.workers or 2                              # 2 actix worker threads / process
# default mode:
self.processes = self.processes or 1
self.workers   = self.workers or 1
# guard:
if self.dev and (processes != 1 or workers != 1):
    raise ValueError("--processes and --workers shouldn't be used with --dev")
```

- **`processes` = number of OS processes = number of CPython interpreters = number of GILs.** Default `1` (single interpreter); "fast" mode uses `2*cpu+1` (the classic gunicorn heuristic — oversubscribe slightly so a process blocked on a syscall doesn't idle a core).
- **`workers` = actix worker threads *inside one process*.** Default `1`, fast-mode `2`. These threads share ONE GIL — they parallelize the *Rust* I/O (accept/parse/write) but **serialize on Python**. More workers than ~2–4 per process is mostly wasted for Python-bound work and only helps if handlers spend most time in GIL-released Rust/I/O.
- **`--dev` forces `processes=1, workers=1`** — single process so the auto-reloader (watchdog) can cleanly restart and so tracebacks/`pdb` work.

**DESIGN for Lancelot:** Expose the same two knobs with the same names but make the auto-tuning explicit and documented:
- `processes` default = `os.cpu_count()` (not `2N+1` — for AI workloads handlers are I/O-bound on outbound LLM calls, so `N` processes each running an async loop saturates cores without the memory cost of `2N+1` interpreters; `2N+1` is for sync/blocking workloads).
- `workers` (tokio worker threads / process) default = small (`2`–`4`); document that this scales Rust I/O, not Python.
- Add a third knob `blocking_threads` (the sync-handler pool size, §5) — Robyn buries this; Lancelot should surface it because sync handlers are common in real apps.
- Ship `SO_REUSEPORT` socket sharing identically; it is the single best scaling primitive and there is no reason to deviate.

### 1.4 The startup sequence inside one process (`processpool.py:149-223`, `server.rs:84-313` — KNOWN)

```
spawn_process(...)                                  # one per OS process
  loop = initialize_event_loop()                    # uvloop.new_event_loop() on Linux/mac CPython,
                                                     #   else asyncio.new_event_loop()
  server = Server()                                  # the Rust #[pyclass], one per process
  server.add_directory / apply_request_headers / add_route / add_global_middleware /
         add_startup_handler / add_shutdown_handler  # register everything (Rust-side maps)
  server.start(socket, workers)                      # -> Rust Server::start (server.rs:84)
  loop = asyncio.get_event_loop(); loop.run_forever()
```

Inside `Server::start` (`server.rs:84-313` — KNOWN):

1. `pyo3_log::init()` (`:90`) — wire Rust logs → Python logging.
2. `static TASK_LOCALS: OnceCell<TaskLocals>` (`:92`) — process-global, set once.
3. `STARTED.compare_exchange(false,true,SeqCst,Relaxed)` (`:94-99`) — idempotent guard; a second `start()` is a no-op (returns `Ok(())`). `AtomicBool` at `:42`.
4. `raw_socket = socket.get_socket()` (`:101`) — `dup()` the shared fd into this process's runtime.
5. `Arc::clone` the routers/headers/dirs (`:103-109`) — cheap refcount bumps; all routing tables are `Arc<...>` shared into the actix app factory.
6. Build the asyncio loop refs: `_py.import("asyncio")`, `new_event_loop()`, `set_event_loop(loop)` (`:111-113`). **This runs under the GIL the caller already holds** (`start` takes `_py: Python`).
7. `TASK_LOCALS.get_or_try_init(|| Python::with_gil(|py| TaskLocals::new(event_loop).copy_context(py)))` (`:120-124`) — capture the event loop + a *copied contextvars context* once. **GIL acquired here, once per process.**
8. Parse `ROBYN_MAX_PAYLOAD_SIZE` env (default `1_000_000` = 1 MB, `server.rs:39-40,126-134`).
9. **`thread::spawn(move || actix_web::rt::System::new().block_on(async move { ... }))`** (`:136-287`) — the entire HTTP server runs on a *separate OS thread* driving **actix's own runtime**, not the tokio multi-thread runtime, not the Python thread.
10. Inside that thread: `execute_startup_handler(...)` (`:139`, awaits the user's `@app.startup_handler`), then `HttpServer::new(app_factory).keep_alive(KeepAlive::Os).workers(workers).client_request_timeout(Duration::from_secs(0)).listen(raw_socket.into()).run().await` (`:147-285`).
11. Back on the main thread: `event_loop.call_method0("run_forever")` (`:289`). The Python asyncio loop runs forever on the main thread; the actix server runs on the spawned thread; they communicate via `pyo3-async-runtimes` `TaskLocals`/`scope_local`. If `run_forever` returns (loop stopped → shutdown), run the shutdown handler and `exit(0)` (`:289-311`).

**Critical architectural observation (INFERRED):** there are effectively **three execution domains per process**: (a) the **main thread** running the Python `asyncio`/`uvloop` event loop forever (this is where `async def` handler coroutines actually get *driven* — their `await`s resume here); (b) the **spawned actix thread(s)** running the HTTP accept/parse/dispatch loop; (c) the **blocking-pool threads** (§5) running sync handlers. The GIL is the one resource shared across all of them. `pyo3-async-runtimes` `TaskLocals`/`into_future` is the bridge that lets an actix-thread task `.await` a coroutine that is actually being stepped by the asyncio loop on the main thread.


---

## 2. The GIL: where it is held, for how long, and why this is the whole game (KNOWN + INFERRED)

### 2.1 The mental model

`Python::with_gil(|py| { ... })` (pyo3 0.27) does: block until the GIL is acquirable → acquire it → run the closure with a `Python<'_>` token (proof you hold the GIL) → release it on closure exit. Within the closure you can touch Python objects; outside it you cannot (the borrow checker enforces this — `Py<T>` is `Send` but `Bound<'py, T>`/`&PyAny` are tied to the `'py` lifetime). pyo3 0.27 also exposes the newer `Python::attach`/`py.detach` (used in `blocking.rs` — see §5) which is the same primitive with clearer naming (`attach` = acquire-and-run, `detach` = temporarily release the GIL while running a non-Python closure, e.g. a blocking `recv()`).

The rule that makes or breaks a Rust-core Python framework:

> **Hold the GIL only to touch Python objects. Never hold it across `.await`. Never hold it while doing Rust I/O, blocking syscalls, or CPU work that doesn't need Python.**

Robyn obeys this rule with surgical precision. Below is every GIL acquisition on the request path, in order.

### 2.2 Every GIL acquisition on the dynamic-request path (KNOWN, traced)

The actix default service handler (`server.rs:220-276`):

```rust
move |router, const_router, middleware_router, payload, ...req: HttpRequest| async move {
    // (A) FAST PATH — const route, no middleware: ZERO GIL.
    if !middleware_router.has_any_middleware() {                       // server.rs:233
        if let Ok(http_method) = HttpMethod::from_actix_method(req.method()) {
            if let Some(cached) = const_router.get_cached_route(&http_method, req.uri().path()) {
                return cached.to_http_response();                      // server.rs:248 — pure Rust
            }
        }
    }
    // (B) NORMAL PATH
    let req_ref = req.clone();
    let task_locals = Python::with_gil(|py| TASK_LOCALS.get().unwrap().clone_ref(py)); // GIL #0
    let response = pyo3_async_runtimes::tokio::scope_local(task_locals, async move {
        index(router, const_router, payload, middleware_router, ...req).await          // §2.3
    }).await;
    response.respond_to(&req_ref)                                                       // pure Rust
}
```

- **GIL #0** (`server.rs:255-256`): `Python::with_gil(|py| TASK_LOCALS.get().unwrap().clone_ref(py))`. This is the *cheapest possible* GIL acquisition — it does exactly one thing: bump the refcount on the cached `TaskLocals` (which holds the asyncio loop + a copied `contextvars.Context`). Nanoseconds of Python work under the GIL. Then the GIL is dropped and `scope_local` binds those locals to the tokio task so any nested `into_future` knows which event loop to schedule on.
- The const fast path (A) takes **zero GIL acquisitions** and never allocates a `Request`. (`server.rs:231-251` — KNOWN.)

### 2.3 `index()` — the request lifecycle (server.rs:475-644 — KNOWN)

```
index(...):
  HttpMethod check (pure Rust)                                                server.rs:485-492
  request = Request::from_actix_request(&req, payload, &global_request_headers).await
        └─ parse body / multipart / headers — Rust; deferred to types agent    server.rs:494-501
  route = "{method}{path}"                                                     server.rs:503
  request_context = Python::with_gil(asyncio::new_context)?                    server.rs:515  ← GIL #1
        └─ allocate ONE contextvars.Context() for the whole request           asyncio.rs:46-49
  ── before-middleware loop:  execute_middleware_function(...).await           server.rs:536-556
  ── route match:
       const cached  → build Response from baked bytes (pure Rust)            server.rs:561-573
       dynamic route → execute_http_function(&request,&function,ctx).await     server.rs:577  ← the FFI call
       no match      → Response::not_found                                     server.rs:589
  set_missing global response headers (pure Rust)                             server.rs:597-599
  ── after-middleware loop:  execute_after_middleware_function(...).await      server.rs:610-640
  return ResponseType
```

- **GIL #1** (`server.rs:515`, body in `asyncio.rs:47-49`): allocate one `contextvars.Context()` per request. `new_context` = `contextvars.Context()()` via cached module ref. Tiny Python work under GIL; the resulting `Py<PyAny>` is threaded as `ctx_ref` through every middleware + the handler so all phases share one context (see §4). On failure → 500, no panic (`server.rs:517-520`).

### 2.4 `execute_http_function` — THE FFI call boundary (executors/mod.rs:519-545 — KNOWN, verbatim)

This is the single most important function for Lancelot. Two paths, branched on `function.is_async` (a `bool` precomputed at route registration by Python `inspect.iscoroutinefunction`, stored in `FunctionInfo`):

```rust
pub async fn execute_http_function(
    request: &Request, function: &FunctionInfo, context: Option<&Py<PyAny>>,
) -> PyResult<ResponseType> {
    if function.is_async {
        // ───────── ASYNC HANDLER PATH ─────────
        let output = Python::with_gil(|py| -> PyResult<_> {               // GIL #2  (executors/mod.rs:525)
            let coroutine = get_function_output(function, py, request)?;  //   call handler(req) -> coro obj
            let awaitable = match context {
                Some(ctx) => wrap_coro_in_context(py, ctx.bind(py), coroutine)?, // loop.create_task(coro, context=ctx)
                None => coroutine,
            };
            pyo3_async_runtimes::tokio::into_future(awaitable)            //   Python awaitable -> Rust Future
        })?                                                               // ← GIL DROPPED HERE
        .await?;                                                          // ← tokio awaits, GIL FREE  (executors/mod.rs:533)
        Python::with_gil(|py| extract_response_type(output, py))          // GIL #3  marshal result (executors/mod.rs:535)
    } else {
        // ───────── SYNC HANDLER PATH ─────────
        Python::with_gil(|py| {                                           // GIL #2' held for the WHOLE call
            let output = match context {
                Some(ctx) => get_function_output_in_context(function, py, ctx.bind(py), request)?, // ctx.run(handler, req)
                None => get_function_output(function, py, request)?,
            };
            extract_response_type_bound(output)                           // marshal while still holding GIL
        })
    }
}
```

This is the crux. Read it three times. The async path:

1. **GIL #2** (`executors/mod.rs:525`): acquire GIL, call `handler(request)`. For an `async def`, calling it does *not run the body* — it returns a coroutine object instantly. Then optionally wrap it as a context-bound task, then `into_future` converts the Python awaitable into a Rust `Future`. **All of this is microseconds of Python work.**
2. **GIL #2 is dropped** the instant the `with_gil` closure returns (the `?` is outside the closure, `executors/mod.rs:532`). The Rust `Future` is now `.await`ed on line 533 **with the GIL released**.
3. While that future is pending, the coroutine's `await`s (e.g. `await openai_client.chat(...)`) are driven by the asyncio/uvloop event loop **on the main thread**, and the actix worker thread that called `execute_http_function` is **free to process other requests**. The GIL is only grabbed briefly each time the asyncio loop steps a coroutine — it is NOT held for the whole I/O.
4. **GIL #3** (`executors/mod.rs:535`): once the future resolves, reacquire the GIL just long enough to marshal the Python return value into a Rust `Response` (`extract_response_type_fast`, `executors/mod.rs:86-153` — fast-path downcasts: `PyResponse` pyclass → dict/list via cached `orjson.dumps` → str/bytes → `StreamingResponse` → generic `FromPyObject`).

**Why this is fast (INFERRED, decisive):** for an I/O-bound async handler (the common AI case — handler is `async def chat(): return await llm.generate(...)`), the GIL is held for maybe a few microseconds (call + into_future) + a few microseconds (marshal) = **single-digit microseconds of GIL hold per request**, regardless of how long the LLM call takes. The 800ms the LLM takes to respond is **entirely GIL-free** from the framework's perspective (the asyncio loop grabs the GIL in tiny slices to step the coroutine when the socket is readable). This is why one process can hold thousands of concurrent in-flight LLM requests: they're all parked in `.await` with the GIL released.

**The sync path (`executors/mod.rs:537-543`) holds the GIL for the ENTIRE handler execution** — call + body + marshal, all inside one `Python::with_gil`. This is correct (a sync `def` runs Python bytecode start-to-finish, you must hold the GIL the whole time) but it means **a sync handler blocks the GIL for its full duration**. That is exactly why sync handlers must NOT run on the actix worker thread — they'd block the reactor. Robyn's answer: route sync handlers onto the blocking-thread pool (§5) where blocking the GIL only blocks that one pool thread, not the I/O reactor. (Note: in `execute_http_function` as shown, the sync branch runs inline; the offload to the blocking pool happens in the router/executor layer that decides which threadpool the future runs on — Robyn's sync handlers are dispatched such that the `with_gil` block runs on a blocking thread, not the actix arbiter. The mechanism is `runtime.rs` `spawn_blocking` → `BlockingRunner` — see §5.)

### 2.5 `get_function_output` — how the Python callable is actually invoked (executors/mod.rs:155-199 — KNOWN)

```rust
fn get_function_output<'a, T>(function: &'a FunctionInfo, py: Python<'a>, function_args: &T)
    -> Result<Bound<'a, PyAny>, PyErr>
where T: Clone + for<'py> IntoPyObject<'py>, ...
{
    let handler = function.handler.bind(py).downcast()?;          // Py<PyAny> -> &Bound, downcast to callable
    if function.number_of_params == 0 {                            // 0-arg handler:
        return handler.call0();                                    //   handler()  — skip Request conversion entirely
    }
    let kwargs = function.kwargs.bind(py);
    let function_args: Py<PyAny> = function_args.clone().into_pyobject(py)?...; // Request -> Python object
    match function.number_of_params {
        1 => if has global/router dependencies in kwargs { handler.call((), Some(kwargs)) }
             else { handler.call1((function_args,)) }              //   handler(request)
        _ => handler.call((function_args,), Some(kwargs)),         //   handler(request, **kwargs)
    }
}
```

Key efficiency decisions Lancelot should copy (KNOWN):

- **`function.handler` is a `Py<PyAny>` stored once at route registration**, not looked up per request. `.bind(py)` is a zero-cost lifetime attach; `.downcast()` confirms callability. No `getattr`, no import, no dict lookup on the hot path.
- **`number_of_params` is precomputed** (Python `inspect.signature` at registration) so the dispatch is a small `match`, and **0-param handlers skip Request→Python conversion entirely** (`call0()`). This is a real optimization: a healthcheck `def ping(): return "ok"` never pays the Request marshalling cost.
- **`kwargs` (the dependency-injection dict) is precomputed and stored as `Py<PyDict>`**, bound per call but not rebuilt. The `global_dependencies`/`router_dependencies` branch decides calling convention without rebuilding args.
- Argument conversion (`into_pyobject`) is the per-request FFI cost — deferred to the types agent, but note it happens **once, under GIL #2, only for ≥1-param handlers**.

### 2.6 The startup/shutdown handler call (executors/mod.rs:557-575, server.rs:289-311 — KNOWN)

```rust
pub async fn execute_startup_handler(event_handler: Option<Arc<FunctionInfo>>, task_locals: &TaskLocals) {
    if let Some(function) = event_handler {
        if function.is_async {
            Python::with_gil(|py| pyo3_async_runtimes::into_future_with_locals(
                task_locals, function.handler.bind(py).call0()?))?.await?;   // GIL drop across await
        } else {
            Python::with_gil(|py| function.handler.call0(py))?;              // sync: GIL whole call
        }
    }
}
```

Same async/sync split, same discipline (drop GIL across the await for async). Shutdown handler at `server.rs:291-308` uses `run_until_complete` if async (because by then the loop is being torn down) or a plain `Python::with_gil(|py| function.handler.call0(py))` if sync.

### 2.7 Error & panic propagation Rust→Python (KNOWN)

- **Python exceptions are `PyErr`, not panics.** pyo3 turns a raised Python exception into `Err(PyErr)`. Robyn's pipeline is `PyResult`/`anyhow::Result` end to end. A handler that raises → `execute_http_function` returns `Err` → `index()` logs `get_traceback(&e)` and returns `Response::internal_server_error(None)` (HTTP 500) (`server.rs:577-586`).
- **`get_traceback` (server.rs:646-658)** reacquires the GIL to format the Python traceback: `error.traceback(py).format()` → logged. This is an extra GIL acquisition *only on the error path* (acceptable).
- **`panic = "abort"`** (Cargo.toml:49) means any Rust `panic!`/`unwrap()`/`expect()`/poisoned-lock on the request path **kills the entire OS process** (all in-flight requests in that interpreter die). Robyn mitigates by *discipline* (convert everything to `Response::*_error`) but there are residual `.unwrap()`s: `server.rs:138` `TASK_LOCALS.get().unwrap()`, `:150` `directories.read().unwrap()`, `:282-285` `.listen(...).unwrap().run().await.unwrap()` (startup only). On the *per-request* path the unwraps are on `Python::with_gil(|py| TASK_LOCALS.get().unwrap().clone_ref(py))` (`server.rs:256`) — `TASK_LOCALS` is always set by then, so safe in practice but fragile by construction.
- **DESIGN for Lancelot — the safety net Robyn lacks:** wrap the Python call in `std::panic::catch_unwind` at the FFI seam (requires `panic="unwind"` for that boundary, OR use `panic="abort"` globally but isolate the Python-call worker so an abort doesn't take down the acceptor). Recommended: `panic="abort"` for max speed + an explicit invariant that the request path has **zero `unwrap()`** (enforce with a clippy lint `clippy::unwrap_used` denied on the request modules) + a process supervisor that restarts a crashed worker process (the multiprocess model already gives you N processes; a SIGCHLD handler that respawns is ~20 lines and turns "abort kills one of N interpreters" into "self-healing"). Robyn's supervisor (`processpool.py`) does NOT respawn — a crashed worker stays dead. Lancelot should respawn.

---

## 3. The runtime topology — actix's `System` vs the Granian-derived tokio runtime (KNOWN — the most subtle part)

Robyn has **two distinct async runtimes** in the same process and it is easy to miss this. Lancelot must decide deliberately which model to use; Robyn's split is partly historical and partly intentional.

### 3.1 The HTTP server runs on actix's runtime, NOT tokio multi-thread (KNOWN — server.rs:136-287)

```rust
thread::spawn(move || {
    actix_web::rt::System::new().block_on(async move {                  // server.rs:137
        ...
        HttpServer::new(move || { App::new()... })
            .keep_alive(KeepAlive::Os)                                   // server.rs:278
            .workers(workers)                                            // server.rs:279
            .client_request_timeout(std::time::Duration::from_secs(0))   // server.rs:280  (0 = no timeout)
            .listen(raw_socket.into()).unwrap()                          // server.rs:281  (the shared fd)
            .run().await.unwrap();                                       // server.rs:283
    });
});
```

- `actix_web::rt::System::new()` builds **actix's own runtime** (which is tokio under the hood, but a *current-thread* tokio runtime per arbiter with actix's `Arbiter`/`System` actor scaffolding). `.workers(N)` spawns **N arbiter threads**, each running its own current-thread tokio + a slice of the connections. This is the actix concurrency model: N single-threaded reactors, connections sharded across them, no work-stealing between arbiters.
- `KeepAlive::Os` — delegate keep-alive timeout to the OS TCP stack (no actix-level idle timer). Good for long-lived connections (streaming/SSE/WS) — actix won't reap an idle-but-streaming connection.
- `client_request_timeout(Duration::from_secs(0))` — **disable the request-read timeout entirely**. `0` means actix will *not* time out a slow client sending headers. INFERRED rationale: AI clients streaming large request bodies (e.g. big prompts, file uploads) or holding long connections shouldn't be killed mid-request. **Pitfall:** this also means a slowloris attacker can hold a connection forever with no framework-level defense — Robyn relies on `backlog=16384` + OS limits + reverse proxy. **DESIGN: Lancelot should make this configurable with a sane default (e.g. 30s header timeout) and a documented `0`-to-disable for streaming.**
- `.listen(raw_socket.into())` — bind the actix server to the **already-created `SO_REUSEPORT` fd** (not `.bind("addr")`), so all processes share the kernel-LB'd socket.

### 3.2 The Granian-derived tokio runtime (runtime.rs:42-143 — KNOWN, mostly unused on the HTTP hot path)

`runtime.rs` ports Granian's `RuntimeWrapper`/`RuntimeRef`:

```rust
pub(crate) struct RuntimeWrapper {
    pub inner: tokio::runtime::Runtime,   // a tokio runtime
    br: Arc<BlockingRunner>,              // the sync/blocking pool (§5)
    pr: Arc<Py<PyAny>>,                   // the Python event loop handle
}
fn default_runtime(blocking_threads: usize) -> tokio::runtime::Runtime {
    RuntimeBuilder::new_current_thread()           // runtime.rs:138  ← CURRENT-THREAD, not multi_thread
        .max_blocking_threads(blocking_threads)    // runtime.rs:139
        .enable_all()                              // runtime.rs:140  timers + I/O drivers
        .build().unwrap()
}
```

**KNOWN, important:** Robyn's *own* tokio runtime helper is `new_current_thread()` — a single-threaded tokio runtime, with `max_blocking_threads` set for `spawn_blocking`. It is NOT a multi-thread work-stealing runtime. On the HTTP path this helper is largely bypassed (the server uses actix's `System`); the `runtime.rs` machinery + custom awaitables are used by `future_into_py` for the non-actix entry points (e.g. driving a Rust future into Python from a context where there's no actix arbiter — `runtime.rs:289-332`). The `RuntimeRef` impls `spawn` (→ `tokio::Handle::spawn`) and `spawn_blocking` (→ `BlockingRunner::run`, §5) and `ContextExt::py_event_loop` (returns the cached Python loop).

### 3.3 What this means and what Lancelot should do (DESIGN)

Robyn's topology — *actix arbiters for HTTP + a side current-thread tokio + a separate blocking pool + the Python asyncio loop on the main thread* — is **four schedulers coordinating through the GIL and `pyo3-async-runtimes::TaskLocals`**. It works but it is baroque, and the actix `System` global + actor model is dead weight for a framework that doesn't otherwise use actors (Robyn only uses actix actors for WebSockets).

**DESIGN — Lancelot's recommended topology (single, explicit, multi-thread tokio):**

```rust
// One process. One tokio multi-thread runtime. One Python interpreter/asyncio loop.
let rt = tokio::runtime::Builder::new_multi_thread()
    .worker_threads(workers)                 // default 2–4: scales Rust I/O, NOT Python
    .max_blocking_threads(blocking_threads)  // default = #cores: for sync-handler offload pool
    .enable_all()                            // I/O + time drivers
    .thread_name("lancelot-worker")
    .build()?;
// HTTP via hyper 1.x + tower on this runtime (no actix System, no actor scaffolding).
// Python asyncio loop driven via pyo3-async-runtimes TaskLocals, same as Robyn.
// Sync handlers -> dedicated BlockingRunner (port Granian's, §5), NOT tokio spawn_blocking
//   (tokio's blocking pool is shared with file I/O / DNS; a flood of slow sync handlers
//    would starve those. A dedicated GIL-aware pool is the Granian/Robyn insight — keep it.)
```

Rationale:
- **Multi-thread tokio with explicit `worker_threads`** gives work-stealing across cores for the Rust I/O (parse/marshal/write) — better tail latency than actix's sharded single-thread arbiters when connection load is uneven. The Python part is still GIL-serialized regardless, so the only thing being parallelized is the Rust I/O, and work-stealing does that strictly better than static sharding.
- **`hyper 1.x` + `tower`** (or `axum` which is hyper+tower) avoids the `actix::System` thread-local global, the `Arbiter` model, and the actix actor dep tree. It also gives first-class HTTP/2 (multiplexed streams — relevant for many concurrent SSE/LLM streams over one connection) without actix's actor-based WS layer.
- **Keep Granian's dedicated `BlockingRunner` for sync handlers** (do NOT just use `tokio::task::spawn_blocking`): tokio's blocking pool is also used by `tokio::fs`, DNS resolution, and any `spawn_blocking` in deps; mixing slow Python sync handlers into it causes head-of-line blocking of unrelated I/O. A separate, GIL-aware, idle-scaling pool (§5) isolates the blast radius.
- **One asyncio/uvloop loop per process on a dedicated thread**, bound via `TaskLocals` — keep Robyn's approach verbatim, it is correct.

### 3.4 `TaskLocals` and `scope_local` — the asyncio↔tokio binding (KNOWN — server.rs:92-124,255-273)

```rust
static TASK_LOCALS: OnceCell<TaskLocals> = OnceCell::new();             // server.rs:92  process-global
let _ = TASK_LOCALS.get_or_try_init(|| Python::with_gil(|py|
    pyo3_async_runtimes::TaskLocals::new(event_loop.clone().into()).copy_context(py))); // server.rs:120-124
...
// per request:
let task_locals = Python::with_gil(|py| TASK_LOCALS.get().unwrap().clone_ref(py));      // server.rs:256
let response = pyo3_async_runtimes::tokio::scope_local(task_locals, async move {
    index(...).await                                                                   // server.rs:257-273
}).await;
```

- `TaskLocals` = (a reference to the Python `asyncio` event loop) + (a copied `contextvars.Context`). Created **once per process** under one GIL acquisition (`OnceCell::get_or_try_init`).
- `copy_context(py)` snapshots the current contextvars context at startup; this is the *base* context. (Per-request context isolation is a *separate* mechanism — §4 — layered on top.)
- Per request, `clone_ref(py)` is a refcount bump (cheap, one GIL slice — GIL #0 §2.2). `scope_local(task_locals, fut)` makes that loop+context the **task-local** for the duration of `fut`, so any nested `pyo3_async_runtimes::tokio::into_future(awaitable)` knows *which* asyncio loop to schedule the coroutine on and which thread to wake. Without this, `into_future` wouldn't know where to drive the Python coroutine.

**DESIGN:** Lancelot should use the identical `OnceCell<TaskLocals>` + per-request `clone_ref` + `scope_local` pattern. It is the correct, minimal binding. Do not create `TaskLocals` per request (that would re-snapshot context and add GIL cost).

---

## 4. Per-request `contextvars.Context` — correctness mechanism with GIL cost (KNOWN — asyncio.rs)

This is a correctness fix (issue #1380) with direct relevance to Lancelot because **AI apps lean heavily on `ContextVar`s** (request-scoped trace IDs, tenant, auth principal, LLM client with per-request config, OpenTelemetry spans).

### 4.1 The problem (KNOWN — asyncio.rs:40-62 docstring, server.rs:505-514 comment)

asyncio **copies the current context for every `Task` it creates**. So if `before_request` writes a `ContextVar`, then the route handler runs as a *different* task, the handler sees its own *copy* — the write is invisible. Worse: for **sync handlers run via `ctx.run(...)`**, a `ContextVar` write would persist in the *worker thread's* current context and **leak into the next request handled by that same pooled thread** (a cross-request data leak — a security/correctness bug, e.g. tenant A's auth bleeding into tenant B).

### 4.2 The fix (KNOWN — asyncio.rs:32-85, server.rs:515-522)

```rust
// asyncio.rs
#[inline(always)]
pub(crate) fn new_context(py: Python) -> PyResult<Py<PyAny>> {           // asyncio.rs:47-49
    Ok(contextvars(py)?.getattr("Context")?.call0()?.unbind())          //   contextvars.Context()
}
#[inline(always)]
pub(crate) fn copy_context(py: Python) -> PyResult<Py<PyAny>> {          // asyncio.rs:33-38
    unsafe { let ptr = pyo3::ffi::PyContext_CopyCurrent();               //   raw C-API, fastest path
             Ok(Bound::from_owned_ptr_or_err(py, ptr)?.unbind()) }
}
// the helper that schedules an async handler INSIDE a given context (asyncio.rs:63-85):
//   Python 3.11+:  async def _run_in_context(coro, ctx):
//                       task = asyncio.get_running_loop().create_task(coro, context=ctx)
//                       return await task
//   Python 3.10:   async def _run_in_context(coro, ctx): return await coro   (no context= kwarg pre-3.11)
```

```rust
// server.rs:515 — ONE context allocated per request, threaded through every phase:
let request_context: Py<PyAny> = match Python::with_gil(crate::asyncio::new_context) { ... };  // GIL #1
let ctx_ref = Some(&request_context);
// ...passed to execute_middleware_function / execute_http_function / execute_after_middleware_function
```

- **Async handlers** (`executors/mod.rs:524-535`): `wrap_coro_in_context(py, ctx, coroutine)` → `_run_in_context(coro, ctx)` → `loop.create_task(coro, context=ctx)`. The context is passed *by reference, not copied*, so `ContextVar` writes inside the coroutine persist in `ctx` and are visible to `after_request`.
- **Sync handlers** (`executors/mod.rs:207-250` `get_function_output_in_context`): the handler is invoked as `ctx.call_method1("run", (handler, request))` i.e. `ctx.run(handler, request)`. The fresh per-request `ctx` means writes land in `ctx` and **do not leak** into the next request on that pooled thread.
- Python **3.10 fallback**: no `context=` kwarg on `create_task` pre-3.11, so the helper degrades to plain `await coro` (pre-fix behavior — cross-phase writes not visible on 3.10). KNOWN limitation documented at `asyncio.rs:58-62`.

### 4.3 GIL cost & Lancelot guidance (INFERRED + DESIGN)

- Cost: **one extra GIL acquisition per request** (GIL #1, `server.rs:515`) to allocate `contextvars.Context()`, plus the `ctx.run(...)` / `create_task(..., context=ctx)` indirection. This is cheap (one small Python object alloc) and **mandatory for correctness** — without it you get cross-request `ContextVar` leakage on the sync pool, which for an AI SaaS is a tenant-isolation bug.
- `PyContext_CopyCurrent` via raw `pyo3::ffi` (`asyncio.rs:34-37`) is used for the startup base-context copy — the raw C-API call is faster than going through `contextvars.copy_context()` Python-level. **DESIGN: Lancelot should use the same raw FFI for context copy on any hot path.**
- **DESIGN:** Lancelot must implement per-request context isolation from day one (Robyn added it late, as a bug fix — #1380). Allocate one `Context` per request, run sync handlers via `ctx.run`, async via `create_task(coro, context=ctx)`. Require Python ≥3.11 to get correct cross-phase semantics (3.10 is EOL-soon anyway; don't carry the degraded fallback as a supported mode — make it an explicit warning).
- Cache the `contextvars` module ref in a `PyOnceLock` exactly as `asyncio.rs:10-18` does (`CONTEXTVARS: PyOnceLock<Py<PyAny>>`) so you don't re-import per request.

---

## 5. The blocking-thread pool — how sync Python is kept off the reactor (KNOWN — blocking.rs, Granian-derived)

This is one of the two highest-value patterns to steal (the other is the custom awaitables, §6). It is the answer to "how do I support `def` handlers in an async Rust core without destroying throughput".

### 5.1 The three-variant runner (blocking.rs:34-60 — KNOWN, verbatim)

```rust
pub(crate) enum BlockingRunner {
    Empty,                       // 0 threads: sync offload disabled (no-op)
    Mono(BlockingRunnerMono),    // exactly 1 dedicated thread
    Pool(BlockingRunnerPool),    // N threads, lazily/idle-scaled
}
impl BlockingRunner {
    pub fn new(max_threads: usize, idle_timeout: u64) -> Self {
        match max_threads { 0 => Empty, 1 => Mono(..), _ => Pool(..) }   // blocking.rs:42-46
    }
    pub fn run<T>(&self, task: T) -> Result<(), channel::SendError<BlockingTask>>
        where T: FnOnce(Python) + Send + 'static { ... }                 // dispatch to variant
}
pub(crate) struct BlockingTask { inner: Box<dyn FnOnce(Python) + Send + 'static> }
```

A `BlockingTask` is a boxed closure that *takes a `Python` GIL token* — i.e. the unit of work is "some code that needs the GIL", and the pool's job is to run it on a thread that isn't the reactor.

### 5.2 The mono runner (1 thread) — blocking.rs:62-82 (KNOWN)

```rust
pub fn new() -> Self {
    let (qtx, qrx) = channel::unbounded();          // crossbeam-channel MPMC, UNBOUNDED
    thread::spawn(move || blocking_worker(qrx));     // one long-lived OS thread
    Self { queue: qtx }
}
pub fn run<T>(&self, task) { self.queue.send(BlockingTask::new(task)) }   // just enqueue
```

### 5.3 The pool runner — adaptive thread spawning (blocking.rs:84-161 — KNOWN, the clever part)

```rust
pub(crate) struct BlockingRunnerPool {
    birth: time::Instant,
    queue: channel::Sender<BlockingTask>,
    tq: channel::Receiver<BlockingTask>,
    threads: Arc<atomic::AtomicUsize>,    // current live thread count
    tmax: usize,                          // max_threads cap
    idle_timeout: time::Duration,
    spawning: atomic::AtomicBool,         // single-flight spawn guard
    spawn_tick: atomic::AtomicU64,        // rate-limit timestamp
}
pub fn new(max_threads, idle_timeout) -> Self {
    let (qtx, qrx) = channel::unbounded();
    thread::spawn(move || blocking_worker(qrx));    // always 1 permanent thread at birth
    Self { threads: Arc::new(1.into()), tmax: max_threads, ... }
}
fn spawn_thread(&self) {
    let tick = self.birth.elapsed().as_micros() as u64;
    if tick - self.spawn_tick.load(Relaxed) < 350 { return; }            // rate-limit: ≤1 spawn / 350µs
    if self.spawning.compare_exchange(false,true,Relaxed,Relaxed).is_err() { return; } // single-flight
    thread::spawn(move || {
        tcount.fetch_add(1, Release);
        blocking_worker_idle(queue, timeout);       // this thread can DIE after idle_timeout
        tcount.fetch_sub(1, Release);
    });
    self.spawn_tick.store(self.birth.elapsed().as_micros() as u64, Relaxed);
    self.spawning.store(false, Relaxed);
}
pub fn run<T>(&self, task) {
    self.queue.send(BlockingTask::new(task))?;
    if self.queue.len() > 1 && self.threads.load(Acquire) < self.tmax {   // backlog forming + headroom:
        self.spawn_thread();                                              //   grow the pool
    }
    Ok(())
}
```

**Mechanics, why each piece matters (KNOWN→INFERRED):**

- **Starts with exactly 1 thread**, grows up to `tmax` only when `queue.len() > 1` (a backlog is forming → the single thread can't keep up). Lazy growth → low idle footprint.
- **Spawn rate-limited to ≤1 new thread per 350µs** (`spawn_tick`) + **single-flight via `spawning` CAS** — prevents a burst from spawning a thundering herd of threads all at once (which would each then contend for the GIL anyway — pointless). Threads are added gradually as sustained load proves it's needed.
- **Grown threads run `blocking_worker_idle` and self-terminate after `idle_timeout`** (`recv_timeout` returns `Err` → loop exits → `threads.fetch_sub(1)`). The pool **shrinks back to 1** when load subsides. This is autoscaling a thread pool with hysteresis, in ~80 lines.
- **`crossbeam-channel::unbounded()`** is the work queue — lock-free MPMC, far better than `std::sync::mpsc` under contention.

### 5.4 The worker loop — the GIL detach trick (blocking.rs:163-177 — KNOWN, THE key insight)

```rust
fn blocking_worker(queue: channel::Receiver<BlockingTask>) {
    Python::attach(|py| {                                  // acquire GIL once, hold the token
        while let Ok(task) = py.detach(|| queue.recv()) {  // RELEASE GIL while blocked on recv()
            task.run(py);                                  // REACQUIRE GIL implicitly, run the Python work
        }
    });
}
fn blocking_worker_idle(queue, timeout) {
    Python::attach(|py| {
        while let Ok(task) = py.detach(|| queue.recv_timeout(timeout)) {  // same, with idle timeout
            task.run(py);
        }
    });
}
```

This is the single most important 14 lines in `blocking.rs`. Read it carefully:

- `Python::attach(|py| ...)` (pyo3 0.27 naming; older = `Python::with_gil`) — the worker thread acquires the GIL **once** and keeps the token `py` for its whole life.
- `py.detach(|| queue.recv())` — **temporarily releases the GIL while the thread is blocked waiting for work**. This is `Python::allow_threads` in older pyo3. Without this, an idle blocking-pool thread parked in `recv()` would be **holding the GIL while doing nothing**, freezing every other thread in the process. The `detach` is what makes the pool *not* a global deadlock.
- When a task arrives, `recv()` returns, the GIL is **reacquired** (exiting `detach`), and `task.run(py)` runs the sync Python handler **with the GIL held** (correct — sync Python needs the GIL the whole time).
- Net behavior: at most one blocking-pool thread executes Python bytecode at a time anyway (GIL), but **while a sync handler is running on a pool thread, the actix reactor threads are NOT blocked** — they can keep accepting/parsing/writing for *async* requests (which release the GIL during their I/O). The sync handler only serializes against *other Python execution*, which is unavoidable in CPython. The reactor stays alive.

### 5.5 The shared default blocking runner & how sync handlers reach it (KNOWN — runtime.rs:281-287, 122-128)

```rust
static SHARED_BLOCKING_RUNNER: OnceLock<Arc<BlockingRunner>> = OnceLock::new();
fn shared_blocking_runner() -> Arc<BlockingRunner> {
    SHARED_BLOCKING_RUNNER.get_or_init(|| Arc::new(BlockingRunner::new(1, 30))).clone()  // default: Mono, 30s idle
}
// RuntimeRef::spawn_blocking just forwards into the BlockingRunner:
fn spawn_blocking<F>(&self, task: F) where F: FnOnce(Python) + Send + 'static {
    _ = self.innerb.run(task);                                                  // runtime.rs:127
}
```

The default shared runner is **`Mono` (1 thread), 30s idle timeout** (`runtime.rs:285`). `RuntimeRef::spawn_blocking` (`runtime.rs:122-128`) routes a GIL-taking closure onto it. The custom awaitables (§6) use `rth.spawn_blocking(move |py| PyFutureAwaitable::set_result(...))` to *resolve a Python future on a GIL-holding thread without blocking the tokio reactor* (`runtime.rs:176,198`).

### 5.6 Lancelot guidance (DESIGN)

- **Steal this pool wholesale.** It is the correct, battle-tested (Granian production) answer to sync handlers. Port `blocking.rs` almost verbatim.
- **Surface `blocking_threads` as a first-class config knob** (Robyn hides it; default `Mono`/1 is too small for an app with many sync handlers — e.g. a sync DB driver). DESIGN default: `Pool` with `max_threads = os.cpu_count()` and `idle_timeout = 30s`. Reasoning: sync handlers often wrap blocking I/O (sync DB drivers, `requests`); with the GIL released during their *I/O syscalls* (if the driver releases it — `psycopg2`, `requests` do via the socket layer), multiple pool threads *can* progress concurrently on I/O. So sizing the pool to #cores (or higher for I/O-bound sync work) is right; `Mono` is only right for pure-CPU sync.
- **Keep the GIL `detach` on the queue wait** — this is non-negotiable; getting it wrong is an instant process-wide freeze.
- **Keep the rate-limited single-flight adaptive spawn** — it prevents thread-explosion under burst. The 350µs constant is a reasonable default; make it tunable but don't remove the mechanism.
- **Do NOT use `tokio::task::spawn_blocking` for sync handlers** (re-stated from §3.3): tokio's blocking pool is shared with `tokio::fs`/DNS/dependency `spawn_blocking`; a flood of slow sync handlers there causes head-of-line blocking of unrelated runtime I/O and DNS, manifesting as mysterious latency spikes. A dedicated, GIL-aware, isolated pool is the entire point.
- **Pitfall — unbounded queue:** Robyn uses `channel::unbounded()`. Under sustained sync-handler overload the queue grows without bound → memory blowup + unbounded latency (requests queued for seconds). **DESIGN: use a *bounded* crossbeam channel for the sync pool and shed load (503) when full**, or at minimum expose a max-queue-depth metric and a configurable bound. This is a real production gap in Robyn.

---

## 6. The custom awaitables — beating `pyo3-async-runtimes` by 38–55% (KNOWN — callbacks.rs + runtime.rs, Granian-derived)

Robyn ships **five hand-rolled `#[pyclass]` awaitables** and uses them instead of `pyo3_async_runtimes::tokio::future_into_py` on the hot paths. The verbatim source comments quantify the win:

- `runtime.rs:163`: `// NOTE: ~55% faster than pyo3_asyncio.future_into_py` (for `future_into_py_iter`)
- `runtime.rs:182`: `// NOTE: ~38% faster than pyo3_asyncio.future_into_py` (for `future_into_py_futlike`)

This is the difference between "Rust core" being a marketing claim and an actual latency win on the response side.

### 6.1 The five awaitables (callbacks.rs — KNOWN)

| Type | Purpose | Key trait |
|---|---|---|
| `PyEmptyAwaitable` (`callbacks.rs:12-28`) | An already-resolved no-value awaitable. `__next__` → `None` immediately. | `#[pyclass(frozen, freelist = 128, ...)]` — **freelist of 128** = pyo3 recycles up to 128 instances without re-allocating; zero-alloc for the common "fire-and-forget" case. |
| `PyDoneAwaitable` (`callbacks.rs:30-57`) | Wraps an already-known `PyResult<Py<PyAny>>`; `__next__` raises `StopIteration(value)` (the coroutine protocol's "return value" mechanism) immediately. | `frozen` — immutable, no per-instance lock. |
| `PyErrAwaitable` (`callbacks.rs:59-83`) | An awaitable that immediately raises a stored `PyErr`. Error fast-path without constructing a Future. | `frozen`. |
| `PyIterAwaitable` (`callbacks.rs:85-127`) | Poll-style: holds `OnceLock<PyResult<Py<PyAny>>>`. `__next__` returns `None` (not ready) until set, then raises `StopIteration(value)`. No event loop callback needed. | Used by `future_into_py_iter` (the 55%-faster path). |
| `PyFutureAwaitable` (`callbacks.rs:129-375`) | Full asyncio-Future-compatible: `state` atomic (Pending/Completed/Cancelled), `add_done_callback`/`remove_done_callback`/`cancel`/`result`/`exception`/`get_loop`/`_asyncio_future_blocking`. Resolves via `loop.call_soon_threadsafe`. | Used by `future_into_py_futlike` (the 38%-faster path). |

### 6.2 Why they're faster than the library (INFERRED from source)

`pyo3_async_runtimes::tokio::future_into_py` constructs a real Python `asyncio.Future`, registers Rust as the resolver, and on completion calls back into the loop via `call_soon_threadsafe` — every await pays: a Python `Future()` allocation, an `add_done_callback` registration, a thread-safe loop wakeup, GIL round-trips on both ends.

Robyn's awaitables collapse this:

- **`PyIterAwaitable` (poll model, `callbacks.rs:107-127`)**: no Python `Future` object at all. It IS the awaitable (a tiny frozen `#[pyclass]` with a `OnceLock`). `__next__` returns `py.None()` while pending (the asyncio loop treats that as "not done, reschedule") and raises `StopIteration(value)` when the `OnceLock` is set. The Rust side just does `OnceLock::set` from `spawn_blocking`. **No `Future` alloc, no `add_done_callback`, no `call_soon_threadsafe`.** Hence ~55% faster.
- **`PyFutureAwaitable` (future model, `callbacks.rs:129-375`)**: still resolves via the loop (`call_soon_threadsafe`) so it's compatible with `await` semantics that need a real future (cancellation, `asyncio.gather`), but it's a *Rust-owned* frozen pyclass with **atomic state** and a **single-callback optimization** (`callbacks.rs:238-266`: "only the most recent callback is stored ... in practice asyncio registers at most one done-callback per future") — no `Vec<callback>`, no `HashMap`, no Python-side `Future` machinery. `_asyncio_future_blocking` getter/setter (`callbacks.rs:224-232`) makes it satisfy asyncio's duck-typed future protocol. Hence ~38% faster (still pays the loop wakeup, but nothing else).
- `freelist = 128` on `PyEmptyAwaitable` (`callbacks.rs:12`) — pyo3 keeps a free-list so creating/destroying these is allocation-free under churn.
- All are `#[pyclass(frozen, module = "robyn._robyn")]` — `frozen` removes the per-instance `RefCell` borrow-checking pyo3 normally adds to `#[pyclass]`, so field access is a direct read with no runtime borrow check.

### 6.3 The dispatch helpers (runtime.rs:145-204 — KNOWN)

```rust
fn empty_future_into_py(py)        -> PyEmptyAwaitable                        // runtime.rs:146-148
fn done_future_into_py(py, result) -> PyDoneAwaitable::new(result)            // runtime.rs:151-156
fn err_future_into_py(py, err)     -> PyErrAwaitable::new(err)                // runtime.rs:159-161
// 55%-faster poll path:
fn future_into_py_iter<R,F>(rt, py, fut) {                                    // runtime.rs:165-180
    let aw = Py::new(py, PyIterAwaitable::new())?;
    rt.spawn(async move { let result = fut.await;
        rth.spawn_blocking(move |py| PyIterAwaitable::set_result(aw, py, result)); }); // resolve on GIL thread
    Ok(py_fut)                                                                // return the awaitable NOW
}
// 38%-faster future path (unix):
fn future_into_py_futlike<R,F>(rt, py, fut) {                                 // runtime.rs:185-204
    let (aw, cancel_tx) = PyFutureAwaitable::new(event_loop).to_spawn(py)?;
    rt.spawn(async move { tokio::select! { biased;
        result = fut => rth.spawn_blocking(move |py| PyFutureAwaitable::set_result(aw, py, result)),
        () = cancel_tx.notified() => rth.spawn_blocking(move |py| aw.drop_ref(py)),  // cancellation path
    }});
    Ok(py_fut)
}
```

Note `tokio::select! { biased; result = fut => ..., () = cancel_tx.notified() => ... }` — **cancellation-aware**: if the Python side `.cancel()`s the future (`callbacks.rs:282-320` notifies `cancel_tx`), the Rust task drops cleanly instead of leaking. `biased` = check the result branch first (the common case) before the cancel branch — micro-opt.

### 6.4 Lancelot guidance (DESIGN)

- **Build the same five awaitables on day one.** This is not premature optimization — it's the response-side analogue of the GIL discipline; without it, every async handler return pays the `pyo3-async-runtimes` `Future`+callback tax. The ~38–55% figure is on the *bridge* cost, which is a measurable slice of per-request overhead for fast handlers.
- **Default to the poll model (`PyIterAwaitable`)** for the response future where you don't need cancellation/`gather` semantics on the *bridge* future (the user's coroutine still has full asyncio semantics; this is only the Rust→Python resolution wrapper). Fall back to the futlike model when the Python side may cancel.
- **Use `#[pyclass(frozen, freelist = N)]`** for all bridge awaitables — `frozen` kills the borrow-check overhead, `freelist` kills the allocation under churn. These attributes are free wins.
- **Keep cancellation wired** (`tokio::select! biased` + a `Notify`): a long-lived AI stream that the client disconnects must propagate cancel into the Rust task so the outbound LLM call is aborted and resources freed. Robyn's `PyFutureAwaitable::cancel` → `cancel_tx.notify_one()` → select drops the future is the right shape.

### 6.5 The fallback path — when there's no current tokio runtime (runtime.rs:289-332 — KNOWN)

`future_into_py` (`runtime.rs:289`) checks `tokio::runtime::Handle::try_current()`:
- **Inside a tokio context** → builds a `RuntimeRef` from the current handle + the shared blocking runner + the running asyncio loop, wraps the future to map errors to `PyRuntimeError`, and uses the fast `future_into_py_futlike`.
- **No tokio context** → falls back to the library `pyo3_async_runtimes::tokio::future_into_py` (correctness over speed when there's no runtime to attach to).

DESIGN: Lancelot can keep this exact fallback structure — fast custom path when a runtime is present, library path as the safe default otherwise.

---

## 7. Streaming responses & the AI angle — does Rust-core help or hurt? (KNOWN + INFERRED — the section that matters most for an AI framework)

Lancelot targets "web + AI". The dominant AI workload is **long-lived streaming responses** (SSE token streams from LLMs) and **high fan-out outbound LLM calls** (one handler issues many concurrent `await openai(...)`). Analyze both against this architecture.

### 7.1 Outbound LLM calls (fan-out) — architecture is NEAR-IDEAL (INFERRED, strong)

Consider `async def chat(req): return await openai_client.chat(...)` under 5000 concurrent clients.

- Each request: GIL #0 (clone TaskLocals, ~ns) + GIL #1 (alloc context, ~µs) + GIL #2 (call handler → coroutine → into_future, ~µs) → **GIL released** → the `await openai(...)` runs on the asyncio/uvloop loop, which holds the GIL only in tiny slices to step coroutines when their sockets are readable → GIL #3 (marshal result, ~µs).
- For the ~800ms the LLM is generating, **the framework holds the GIL for ~single-digit µs total per request**. 5000 in-flight LLM calls are 5000 coroutines parked in `await`, costing ~the memory of 5000 coroutine frames + 5000 sockets. The GIL is *not* the bottleneck — the event loop's ability to poll N sockets is, and uvloop (libuv) does that at C speed.
- **This is strictly better than uvicorn/FastAPI on the same workload only by the framework-overhead delta** (Rust parse/route/marshal vs Python Starlette parse/route/marshal): both release the GIL during the actual LLM await (FastAPI does too — `httpx`/`aiohttp` await releases the GIL via the event loop). The win is the **per-request fixed overhead** (Rust µs vs Python tens-of-µs) which matters at high RPS but is dwarfed by LLM latency per request. **Where the Rust core decisively wins for AI: high-RPS short calls (embeddings, classification, RAG retrieval, cache hits, healthchecks) and the const-route fast path** — there the framework overhead is the whole cost and Rust is 5–10x lighter.

**Verdict: for outbound LLM fan-out the architecture HELPS modestly (lower fixed overhead) and never hurts** — the GIL discipline (drop across await) means thousands of concurrent LLM calls coexist in one interpreter. The dominant cost (LLM latency, socket polling) is GIL-free regardless of framework. Lancelot's edge over FastAPI here is the *fixed per-request µs*, which compounds at scale and on the fast paths.

### 7.2 Streaming responses (SSE token streams) — the architecture HURTS as written (KNOWN — response.rs:107-146)

This is Robyn's real weakness and the thing Lancelot must fix.

```rust
impl Responder for StreamingResponse {                                  // response.rs:97-122
    fn respond_to(self, _req) -> HttpResponse {
        ...
        .append_header(("X-Accel-Buffering", "no"))                     // disable nginx buffering
        .append_header(("Cache-Control", "no-cache, no-store, must-revalidate"))
        let stream = create_python_stream(self.content_generator);
        response_builder.streaming(stream)
    }
}
fn create_python_stream(generator: Py<PyAny>)
    -> Pin<Box<dyn Stream<Item=Result<Bytes, actix_web::Error>> + Send>> {
    Box::pin(futures::stream::unfold(generator, |generator| async move {
        match tokio::task::spawn_blocking(move || {                     // response.rs:128  ← per chunk!
            Python::with_gil(|py| {                                     // response.rs:129  ← GIL per chunk!
                let gen = generator.bind(py);
                match gen.call_method0("__next__") {                    // response.rs:130  sync generator only
                    Ok(value) => value.extract::<String>().ok().map(|s| (s, generator)),
                    Err(e) => { if !e.is_instance_of::<PyStopIteration>(py) { log::error!(...) } None }
                }
            })
        }).await {
            Ok(Some((s, generator))) => Some((Ok(Bytes::from(s)), generator)),
            _ => None,
        }
    }))
}
```

**The problems (KNOWN, severe for AI):**

1. **`tokio::task::spawn_blocking` per chunk** (`response.rs:128`) — every single token/chunk hops onto tokio's *shared* blocking pool (the one also used by fs/DNS — §3.3 pitfall). For an LLM emitting 500 tokens, that's 500 blocking-pool dispatches for *one* response. Under 1000 concurrent streams that's a storm on the shared blocking pool → head-of-line blocking of all other blocking work in the process.
2. **`Python::with_gil` per chunk** (`response.rs:129`) — one GIL acquire/release per token. With C concurrent streams each emitting tokens, the GIL is being grabbed C×(tokens/sec) times/sec just for stream pumping, on top of normal request GIL traffic. The GIL becomes the streaming bottleneck: streams serialize against each other on every token, not just on the LLM.
3. **Sync generator only** (`gen.call_method0("__next__")`, `response.rs:130`) — it pumps a *synchronous* Python generator. An `async def` streaming handler that does `async for chunk in llm.stream()` cannot be pumped this way; the user must wrap it in a sync generator that *blocks* a pool thread for each `await` (or Robyn handles async streams elsewhere — but `create_python_stream` is sync-`__next__` only). For LLM streaming where each token IS an awaited network read, a sync generator means a pool thread is *blocked on the network per token* — exactly the anti-pattern.
4. **`.extract::<String>()` per chunk** — every chunk is forced through a Python-str→Rust-String copy; bytes/SSE-framed output pays UTF-8 validation + allocation per token.

**Verdict: as written, Robyn's streaming HURTS under concurrent AI token streaming.** It works fine for a few streams; it degrades badly at the concurrency an AI product needs (hundreds–thousands of simultaneous SSE streams), because every token of every stream is a GIL acquisition + a shared-blocking-pool hop + a string copy.

### 7.3 What Lancelot must do for streaming (DESIGN — concrete)

1. **Native async-generator pump.** Support `async def` streaming handlers: drive the Python async generator via `__anext__()` → `into_future` so each chunk's underlying `await llm.stream()` is a real GIL-released asyncio await, NOT a blocked pool thread. This is the single biggest fix. Use the custom `PyIterAwaitable` (§6) to wrap each `__anext__` cheaply.
2. **Batch/coalesce chunks.** Don't cross the FFI per token. Accumulate tokens on the Python side (or in a small ring buffer) and yield ~every N tokens or every ~5–15ms (whichever first), so one GIL acquisition flushes many tokens. For SSE this is transparent to the client (still streamed, just coalesced). This turns "1 GIL/token" into "1 GIL/flush" — a 10–50x reduction in GIL traffic for streaming.
3. **Dedicated stream-pump executor, not tokio's shared `spawn_blocking`.** If a sync generator must be supported, pump it on the dedicated `BlockingRunner` (§5), never `tokio::task::spawn_blocking` (don't poison fs/DNS).
4. **Zero-copy bytes path.** Accept `bytes`/`bytearray`/memoryview chunks and pass them as `Bytes` without UTF-8 round-trip; only validate when the user yields `str`. For SSE, frame (`data: ...\n\n`) in Rust to avoid a Python-side string build per event.
5. **Backpressure.** actix `response_builder.streaming(stream)` will buffer if the client is slow; for LLM streams pin a bounded channel between the Python producer and the Rust writer so a slow client applies backpressure to (or cancels) the LLM call instead of unbounded memory growth. Wire client-disconnect → cancel the producer coroutine (use the `PyFutureAwaitable` cancel path, §6.4) → abort the outbound LLM request.
6. **HTTP/2.** Many concurrent SSE streams from one client multiplex far better over HTTP/2 (one connection, N streams) than HTTP/1.1 (one connection per stream, hits browser 6-conn limit). actix-web supports h2; if Lancelot uses hyper, h2 is first-class. Make h2 the default for streaming endpoints.

`X-Accel-Buffering: no` + `Cache-Control: no-cache` + `Connection: keep-alive` (`response.rs:107-117`, `PyStreamingResponse::new` `response.rs:280-289`) are correct SSE headers — keep these verbatim. `media_type` default `text/event-stream` (`response.rs:266`) is the right SSE default.

### 7.4 GIL implications for many concurrent LLM streams — summary table (INFERRED)

| Scenario | GIL behavior | Bottleneck | Lancelot action |
|---|---|---|---|
| 1000 concurrent `await llm.chat()` (non-stream) | GIL held ~µs/req total; released for the 800ms LLM call | Event loop socket polling (uvloop, C-speed) — not GIL | Architecture already near-ideal; keep GIL-drop-across-await discipline |
| 1000 concurrent SSE streams (Robyn as-is) | 1 GIL acquire **per token per stream** + shared blocking-pool hop/token | **GIL** + shared blocking pool — both saturate | Coalesce chunks (§7.3.2) + async-gen pump (§7.3.1) + dedicated pump pool |
| 1000 concurrent SSE streams (Lancelot fixed) | 1 GIL acquire **per flush** (~every 10ms), async-gen awaits GIL-free | Event loop + network — GIL no longer dominant | Implement §7.3 fully before launch |
| High-RPS short calls (embeddings/RAG/cache) | GIL ~µs/req; Rust overhead dominates and is 5–10x < Python | Framework fixed overhead | This is where Rust-core decisively wins vs FastAPI — optimize the fixed path, use const-route tier |

---

## 8. Robyn vs pure-Python asyncio (Blazeio) vs ASGI/uvicorn (FastAPI) — the runtime/GIL axis (INFERRED, cross-referenced)

This is the comparison that justifies Lancelot's architecture. All three host CPython; the difference is *where the request lifecycle executes* relative to the GIL.

| Axis | **Robyn (Rust core + PyO3)** | **Blazeio (pure-Python asyncio)** | **FastAPI on uvicorn (ASGI, pure Python)** |
|---|---|---|---|
| HTTP parse (request line/headers/body framing) | Pure Rust (actix-http), **GIL-free** | Python bytecode, **holds GIL** | uvicorn's `httptools` (C) parses GIL-free-ish, but ASGI scope/headers dict built in Python (GIL) |
| Routing | Rust `matchit` radix tree, **GIL-free**; const routes never enter Python | Python dict/regex match, **GIL** | Starlette regex route table, Python, **GIL** |
| Request object construction | Rust struct; converted to Python only for ≥1-param handlers, once, under one GIL slice | Python object, **GIL** the whole time | Starlette `Request` + Pydantic model validation, Python+C, **GIL** |
| GIL hold per async request | ~single-digit µs (3 short slices), **released across the entire handler await** | GIL held for all framework code; released only inside `await` socket ops | GIL held for all Starlette/ASGI glue; released inside `await` |
| CPU scaling | N processes (SO_REUSEPORT), N GILs; workers=Rust I/O threads | N processes (gunicorn/uvicorn `--workers`), N GILs; one event loop/proc | N uvicorn workers (processes), N GILs |
| Sync `def` handler | Offloaded to dedicated GIL-aware blocking pool (Granian-derived); reactor stays alive | Blocks the single event loop unless user manually `run_in_executor` | Starlette runs sync endpoints in a `ThreadPoolExecutor` (anyio) — similar idea, but Python-level thread pool, GIL-bound, shared |
| Response serialization | Rust fast-path: `PyResponse` downcast / cached `orjson.dumps` / str/bytes — minimal GIL | Python serialization, **GIL** | Pydantic `.model_dump()` + `json`/`orjson` in Python, **GIL** |
| Async bridge cost (response side) | Custom awaitables, ~38–55% faster than the library bridge | N/A (no FFI bridge — it's all Python) | N/A (no FFI — ASGI is Python protocol) |
| Streaming | Per-chunk GIL + shared blocking pool (weak — §7.2) | Per-chunk Python (GIL) but no FFI hop; simple `async for` yield | Starlette `StreamingResponse` async-gen, per-chunk Python (GIL), no FFI hop |
| Floor latency (empty/const route) | **Lowest** — const route = zero GIL, zero Python, pure Rust map lookup | Higher — minimal but still Python per request | Highest of the three — full ASGI scope + Starlette stack in Python |
| Where it wins | High-RPS short calls, const routes, fixed per-req overhead | Simplicity, no build step, no FFI bugs, easy to reason about GIL | Ecosystem, Pydantic, OpenAPI; not raw speed |
| Where it loses / risks | FFI complexity, `panic=abort` fragility, streaming weakness, pyo3 version churn | Everything framework-side is GIL-bound — no Rust offload of parse/route/marshal | Heaviest per-request Python; Pydantic v2 is C-fast but ASGI glue is Python |

**Key cross-reference insight:** the *only* fundamental advantage of Robyn/Lancelot over Blazeio/FastAPI is that **the non-handler request lifecycle (parse/route/build/marshal/write) executes GIL-free in Rust**. For an I/O-bound async handler, *all three* release the GIL during the actual `await` (network), so the LLM-call latency itself is GIL-free everywhere. The Rust-core win is therefore **the fixed per-request overhead delta** — large in relative terms (Rust µs vs Python tens-of-µs) and decisive for high-RPS short endpoints and const routes, but a small absolute fraction of a request dominated by an 800ms LLM call. **Lancelot's value prop is precisely: "the framework overhead is ~zero so your handler latency is all that's left, and your sync handlers don't block the reactor, and 5000 concurrent LLM streams cost ~nothing in GIL."** That is true and defensible — but only if streaming is fixed (§7.3) and the GIL discipline (§2) is exact.

Blazeio's lesson for Lancelot (INFERRED): a pure-Python asyncio core has *zero FFI risk* and is trivially correct on the GIL (you never hold it "wrong" because you never explicitly hold it), but it can never move parse/route/marshal off the GIL — so it is overhead-bound at high RPS. FastAPI's lesson: ecosystem (Pydantic/OpenAPI) is why people pick it despite the overhead — Lancelot needs Rust speed *and* a FastAPI-grade Python DX (Pydantic integration, OpenAPI, DI) or it loses on adoption regardless of latency.

---

## 9. The consolidated Lancelot core blueprint (DESIGN — reimplementable)

Everything above, distilled into the actual core spec.

### 9.1 Cargo dependencies (pin exactly)

```toml
[dependencies]
pyo3 = { version = "0.27", features = ["extension-module", "py-clone"] }
pyo3-async-runtimes = { version = "0.27", features = ["tokio-runtime"] }
tokio = { version = "1.40", features = ["full"] }       # full: net, time, rt-multi-thread, sync, macros
hyper = { version = "1", features = ["http1", "http2", "server"] }   # instead of actix-web
hyper-util = "0.1"                                       # tokio executor glue for hyper 1.x
tower = "0.5"                                            # middleware/service abstraction
http-body-util = "0.1"                                   # streaming body helpers
socket2 = { version = "0.5", features = ["all"] }        # SO_REUSEPORT — verbatim from Robyn
crossbeam-channel = "0.5"                                # blocking-pool queue (bounded! see §5.6)
dashmap = "6"                                            # concurrent route/header maps
parking_lot = "0.12"
once_cell = "1"
matchit = "0.8"                                          # radix-tree router (Robyn uses 0.7)
anyhow = "1"
pyo3-log = "0.13"                                        # optional; keep Rust logs off hot path

[lib]
crate-type = ["cdylib", "rlib"]

[profile.release]
codegen-units = 1
lto = "fat"
panic = "abort"        # + zero-unwrap discipline on request path + supervisor respawn (§2.7)
strip = true
opt-level = 3
```

(Robyn proves `actix-web 4.4` works; the hyper+tower swap is the recommended divergence for a greenfield framework — leaner, h2-first, no actor `System`. If time-to-first-version matters more than long-term leanness, `actix-web 4` is a safe shortcut and Robyn is the reference implementation.)

### 9.2 The runtime construction (one place, explicit)

```rust
// Per OS process (one CPython interpreter, one GIL):
let rt = tokio::runtime::Builder::new_multi_thread()
    .worker_threads(cfg.workers.unwrap_or(num_cpus_min(2, 4)))   // Rust I/O parallelism
    .max_blocking_threads(512)                                    // tokio's own pool (fs/DNS); NOT sync handlers
    .enable_all()
    .thread_name("lancelot-io")
    .build()?;
// Dedicated, GIL-aware, BOUNDED sync-handler pool (port blocking.rs, bounded queue):
let blocking = BlockingRunner::new(cfg.blocking_threads.unwrap_or(num_cpus()), /*idle*/ 30,
                                   /*queue_bound*/ cfg.sync_queue_max.unwrap_or(10_000));
// One asyncio/uvloop loop per process on its own thread; bound via TaskLocals (verbatim Robyn).
static TASK_LOCALS: OnceCell<TaskLocals> = OnceCell::new();
```

### 9.3 The request hot path (the exact GIL choreography to implement)

```
accept (SO_REUSEPORT, kernel-LB)                                  pure Rust, GIL-free
  ── hyper HTTP/1.1|2 parse                                       pure Rust, GIL-free
  ── matchit route lookup                                         pure Rust, GIL-free
  ── const/static-route tier? → serve baked bytes                 ZERO GIL, ZERO Python  [implement this tier]
  ── else build Request (Rust struct)                             pure Rust, GIL-free
  ── ctx = Python::with_gil(new contextvars.Context)              GIL #1  (~µs)  [per-request isolation]
  ── before-middleware*                                           GIL per mw call, drop across async await
  ── dispatch handler:
       async def:  with_gil{ call→coro; create_task(coro,ctx); into_future } ; DROP GIL ; .await ; with_gil{ marshal }
                                                                   GIL #2 (~µs) ; FREE during await ; GIL #3 (~µs)
       def:        BlockingRunner.run(move |py| ctx.run(handler,req); marshal)   GIL held on POOL thread only
  ── after-middleware*                                            GIL per mw call
  ── marshal Response (fast downcast: pyclass / orjson / str|bytes) GIL #3 (~µs)
  ── hyper write body                                             pure Rust, GIL-free
```

Invariant to enforce in code review / clippy: **no `Python::with_gil`/`attach` scope may contain an `.await`**. (Robyn obeys this; it's the whole game.) Add `#![deny(clippy::await_holding_lock)]` and a custom check for `with_gil` spans.

### 9.4 What to port near-verbatim from Robyn vs what to change

| Component | Robyn source | Lancelot action |
|---|---|---|
| `SocketHeld` SO_REUSEPORT socket | `shared_socket.rs` (44 lines) | **Port verbatim.** Perfect as-is. |
| Multi-process supervisor | `processpool.py` | Port the fork+`dup`+pickle model; **ADD: respawn-on-crash** (Robyn doesn't). |
| GIL discipline in `execute_http_function` | `executors/mod.rs:519-545` | **Port the exact async/sync split + drop-GIL-across-await.** Non-negotiable. |
| Custom awaitables | `callbacks.rs` (409 lines) | **Port all 5** (`frozen`, `freelist`). 38–55% bridge speedup. |
| Blocking-thread pool | `blocking.rs` (177 lines) | **Port the 3-variant + GIL-detach-on-recv + adaptive spawn**; change `unbounded()` → **bounded** + load-shed. |
| Per-request contextvars | `asyncio.rs` (85 lines) | **Port verbatim**; require Python ≥3.11 (drop the 3.10 degraded path as a hard warning). |
| `TaskLocals` OnceCell + scope_local | `server.rs:92,120-124,255-273` | **Port verbatim.** |
| Const-route fast path | `server.rs:231-251` | **Port + make it a first-class API** (`@app.const_route` / static route tier). |
| Streaming pump | `response.rs:107-146` | **DO NOT PORT AS-IS.** Reimplement per §7.3 (async-gen, coalesced, dedicated pump, zero-copy, backpressure, cancel). |
| HTTP engine | actix-web `System`/arbiters | **Replace with hyper+tower multi-thread tokio** (or accept actix as a shortcut). |
| Build profile | `Cargo.toml:46-51` | **Port verbatim** + add `catch_unwind` at the FFI seam (Robyn lacks this). |

---

## 10. Top 3 GIL/FFI pitfalls (and Robyn's verdict on each)

### Pitfall #1 — Holding the GIL across an `.await` (fatal: serializes the entire interpreter)
If any `Python::with_gil`/`attach` scope contains an `.await`, the GIL is held for the *entire* duration of that await across the whole process — every other thread/coroutine in that interpreter is frozen until the await completes. For an AI handler awaiting an 800ms LLM call, this would drop throughput from thousands of concurrent requests to ~1. **Robyn's verdict: AVOIDED with precision** — `execute_http_function` (`executors/mod.rs:524-535`) closes the `with_gil` scope *before* the `.await`, reacquires after. **Lancelot: enforce as a hard invariant** (clippy lint + manual review of every `with_gil`); it is the single most important rule in the codebase.

### Pitfall #2 — Running sync/CPU-bound/blocking Python on the async reactor (starves all I/O)
A sync `def` handler (or any handler doing blocking I/O / CPU work) executed directly on the actix/tokio worker holds the GIL for its full duration *and* occupies a reactor thread — every async request on that thread stalls. **Robyn's verdict: AVOIDED via the dedicated Granian-derived blocking pool** (`blocking.rs`) with the GIL `py.detach()` on the queue wait so idle pool threads don't hold the GIL. **Lancelot: port the pool, but make the queue *bounded* and shed load when full** (Robyn's `unbounded()` is a latent memory/latency bomb under sync overload — §5.6). Also: never use tokio's shared `spawn_blocking` for sync handlers (poisons fs/DNS).

### Pitfall #3 — Per-chunk GIL acquisition + per-chunk FFI hop on streaming (kills concurrent LLM token throughput)
Pumping a Python generator with one `Python::with_gil` + one `tokio::task::spawn_blocking` *per token* means at C concurrent streams × T tokens/sec you do C·T GIL acquisitions/sec just for stream pumping, and C·T hops on the shared blocking pool. The GIL becomes the streaming bottleneck and streams serialize against each other on every token. **Robyn's verdict: NOT avoided** — `create_python_stream` (`response.rs:128-146`) does exactly this (per-chunk `spawn_blocking` + `with_gil` + sync-generator-only + `extract::<String>` copy). This is Robyn's worst weakness for AI workloads. **Lancelot: must fix before launch** — native async-generator pump via `into_future` (GIL-free awaits), chunk coalescing (1 GIL per ~10ms flush, not per token), a dedicated pump executor (not tokio's shared pool), zero-copy bytes, bounded backpressure, and client-disconnect→cancel→abort-LLM wiring (§7.3).

**Honorable-mention pitfalls (also relevant):** (4) `panic="abort"` + any request-path `unwrap()` = whole-interpreter kill, with no respawn in Robyn's supervisor — Lancelot needs zero-unwrap discipline + `catch_unwind` at the seam + respawn. (5) pyo3 minor-version churn — 0.27's `IntoPyObject`/`FromPyObject<'a,'b>`/`Bound` API differs from 0.20/0.23; pin the minor and budget a migration each pyo3 minor (Robyn's git history shows repeated pyo3-bump churn). (6) Cross-request `ContextVar` leakage on a reused sync-pool thread if you forget the per-request `Context` (Robyn shipped this bug, fixed in #1380 — Lancelot must do it from day one or it's a tenant-isolation defect in a multi-tenant AI SaaS).

---

## Appendix A — File-by-file evidence index (KNOWN, all read line-by-line)

| File | Lines | What it proves for this analysis |
|---|---|---|
| `Cargo.toml` | 58 | Exact crate versions; aggressive release profile incl. `panic="abort"` |
| `pyproject.toml` | 153 | maturin build, `extension-module`, uvloop conditional dep, multiprocess/orjson |
| `src/lib.rs` | 69 | `#[pymodule]` registers `Server`/`SocketHeld`/awaitables/types — the FFI surface |
| `src/shared_socket.rs` | 44 | SO_REUSEPORT+SO_REUSEADDR+TCP_NODELAY, backlog 16384, `try_clone` fd dup |
| `src/server.rs` | 658 | `Server::start` topology, const fast path (zero GIL), `index()` lifecycle, GIL #0/#1, TaskLocals OnceCell, actix `System` thread, shutdown/panic paths |
| `src/executors/mod.rs` | 576 | **THE FFI boundary**: `execute_http_function` async/sync split, GIL #2/#3, drop-GIL-across-await, `get_function_output` calling convention, fast response extraction |
| `src/runtime.rs` | 332 | Granian-derived RuntimeWrapper, `new_current_thread` tokio, custom-awaitable dispatch (38–55% notes), `spawn_blocking`→BlockingRunner, `future_into_py` fallback |
| `src/callbacks.rs` | 409 | The 5 custom awaitables; `frozen`/`freelist`; poll vs futlike model; cancellation wiring |
| `src/blocking.rs` | 177 | 3-variant pool, GIL `py.detach()` on `recv()`, adaptive rate-limited single-flight spawn, idle-timeout thread death |
| `src/asyncio.rs` | 85 | Per-request `contextvars.Context`, raw `PyContext_CopyCurrent` FFI, 3.11 `create_task(context=)` helper, 3.10 fallback |
| `src/io_helpers/mod.rs` | 33 | Header application, blocking `read_file` (note: sync file read — not async) |
| `src/types/response.rs` | (streaming portions) | `create_python_stream` per-chunk GIL+spawn_blocking pump; SSE headers; sync-`__next__`-only generator |
| `robyn/processpool.py` | 223 | Parent binds socket once → `try_clone` per process → `multiprocess.Process(spawn_process)`; uvloop install; no respawn |
| `robyn/argument_parser.py` | (scaling) | Defaults: processes `1`/fast `2N+1`, workers `1`/fast `2`; `--dev` forces 1/1 |

## Appendix B — The single decisive sentence for Lancelot

> A Rust-core Python web framework wins **only** to the extent that it executes the request lifecycle outside the GIL and **never** holds the GIL across an `.await`; Robyn proves this is achievable in production (the async path holds the GIL for single-digit microseconds per request and drops it for the entire handler I/O), and Lancelot should copy that discipline verbatim, copy the Granian-derived blocking pool and custom awaitables, fix the streaming pump that Robyn got wrong, and add the crash-safety (catch_unwind + respawn) and bounded backpressure that Robyn lacks.
