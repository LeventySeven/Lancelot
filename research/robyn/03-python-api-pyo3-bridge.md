# Robyn — Python API Layer, PyO3 Bridge, Scaling Model, AI/MCP Modules & Packaging

> Source-level RE for **Lancelot** (Rust core + Python API, "faster FastAPI" for startups + AI).
> All claims cite `path:line` against the local clone at `robyn/Robyn/` (Robyn `v0.85.0`, Cargo `0.85.0`, pyproject declares `0.85.0`).
> Labels: **KNOWN** = read directly from source; **INFERRED** = deduced from code behavior; **DESIGN-NOTE** = Lancelot recommendation.
> This file goes *deeper on the Python side* than `teardowns/ROBYN.md` (which is Rust-internals-heavy). Read that for actix/tokio internals; read this for the binding surface, scaling, DX, AI layer, and shipping.

---

## 0. Executive summary (the 10-line version)

1. **The bridge is one compiled `.so` named `robyn`** (`Cargo.toml:13` `[lib] name="robyn"`, `pyproject.toml:152` `[tool.maturin] module-name="robyn"`). Python imports Rust types via `from robyn.robyn import Server, FunctionInfo, Headers, …` — i.e. `robyn/robyn.{abi}.so` lives *inside* the `robyn/` package. `robyn.pyi` (576 lines) is the hand-written binding contract.
2. **The entire Python→Rust handler ABI is a single dataclass: `FunctionInfo(handler, is_async, number_of_params, args, kwargs)`** (`robyn.pyi:54-71`). Python wraps every user handler in a closure, snapshots its signature *once at registration*, and hands Rust an opaque callable + arity. Rust never introspects Python signatures on the hot path.
3. **Scaling = `multiprocess` fork × actix workers, GIL sidestepped by process replication.** `processpool.py:107` forks `processes` OS processes, each calling `socket.try_clone()` (`processpool.py:108`) to share one `SO_REUSEPORT` listener; each process then runs `server.start(socket, workers)` spinning `workers` actix worker threads. Defaults: `processes=1, workers=1`; `--fast` → `processes=(cpu*2)+1, workers=2` (`argument_parser.py:103-109`).
4. **Hot reload is a watchdog `Observer` + subprocess respawn**, debounced 0.5 s, with a 0.2 s settle sleep (`reloader.py:152-164`). It is CLI-only — the pure-Python wrapper hard-`SystemExit`s if `--dev` is used without the CLI (`__init__.py:144-149`).
5. **Typed params without FastAPI-style DI**: a 3-phase resolver in `router.py:177-278` (type-annotation match → reserved-name match → individual path/query coercion via `_param_utils.py`). No `Depends()`, no Annotated metadata — type *identity* drives injection. Pydantic is *opt-in and lazy* (`pydantic_support.py:34-48`), validated via `model_validate_json` (single-pass, `pydantic_support.py:132`).
6. **`robyn/ai.py` (308 lines) is an explicit experimental PoC** — an OpenAI chat wrapper with pluggable `AgentRunner`/`MemoryProvider` ABCs and an in-memory store. Default model `gpt-4o`, temp `0.7`, max_tokens `1000` (`ai.py:180-183`). It is *not* production-grade and is the weakest module — Lancelot should treat it as a *shape* to copy, not code.
7. **`robyn/mcp.py` (461 lines) is the real AI surface**: a complete MCP JSON-RPC 2.0 server mounted at `POST /mcp` with `@app.mcp.resource/.tool/.prompt` decorators, auto JSON-schema generation from function signatures, URI-template resources with regex param extraction. Protocol version `2024-11-05`. This is a strong, copyable template for Lancelot's AI-native layer.
8. **Packaging = maturin + per-(platform×CPython) prebuilt wheels.** Release CI builds macOS (x86_64/aarch64/universal2), Windows (x64/x86), Linux glibc (x86_64/i686), Linux musl-cross (aarch64/armv7) × CPython 3.10–3.14 (`.github/workflows/release-CI.yml`). `panic="abort"`, `lto="fat"`, `codegen-units=1`, `strip=true` (`Cargo.toml:46-51`).
9. **Runtime deps are tiny**: `orjson`, `multiprocess`, `uvloop` (non-win/CPython/non-armv7), `watchdog`, `rustimport`, `inquirerpy` (`pyproject.toml:27-35`). Pydantic and Jinja2 are *optional extras*. The wheel ships zero heavy transitive deps.
10. **Steal list for Lancelot (top 5):** (a) the `FunctionInfo` opaque-callable ABI — register once, never introspect on hot path; (b) `multiprocess`+`SO_REUSEPORT` fork model with `try_clone()`; (c) 3-phase param resolver with registration-time signature snapshot; (d) the MCP module wholesale as the AI-native primitive; (e) lazy-optional Pydantic (zero import cost when unused). Avoid: `ai.py`'s sync-OpenAML-in-async-handler and `eval()`-in-docstring example; CLI-only dev mode; the per-request `inspect.signature` in `ws.py`.

---

## 1. The PyO3 bridge — how Python and Rust meet (KNOWN)

### 1.1 One module, one `.so`, package-internal

- **Crate** (`Cargo.toml:1-14`): `name = "robyn"`, `[lib] name = "robyn"`, `crate-type = ["cdylib", "rlib"]`. The `cdylib` is the Python extension; the `rlib` lets Rust integration tests link the same code.
- **maturin config** (`pyproject.toml:151-152`): `[tool.maturin] module-name = "robyn"`. Also mirrored in `[package.metadata.maturin] name = "robyn"` (`Cargo.toml:56-57`).
- **PyO3 features** (`Cargo.toml:17`): `pyo3 = { version = "0.27.2", features = ["extension-module", "py-clone"] }`.
  - `extension-module` → builds a CPython extension (no libpython link; symbols resolved at load — see `.cargo/config.toml` below).
  - `py-clone` → allows `Py<T>` to `Clone` while holding the GIL. This is load-bearing: `FunctionInfo` must be `Clone` so each forked process / actix worker gets its own handle (see `teardowns/ROBYN.md §32`).
- **The pymodule** (`src/lib.rs:37-68`): `#[pymodule] pub fn robyn(_py, m)` registers exactly: `get_version` (fn) + classes `Server, Headers, Cookie, Cookies, CookiesIter, WebSocketRegistry, WebSocketConnector, WebSocketChannel, SocketHeld, FunctionInfo, Identity, PyRequest, PyResponse, PyStreamingResponse, Url, QueryParams, MiddlewareType, HttpMethod` + 5 awaitable shims (`PyEmptyAwaitable, PyDoneAwaitable, PyErrAwaitable, PyIterAwaitable, PyFutureAwaitable`, `lib.rs:62-66`).
- **The import line that wires it all** (`robyn/__init__.py:24`):
  ```python
  from robyn.robyn import FunctionInfo, Headers, HttpMethod, Request, Response, WebSocketConnector, get_version
  ```
  The compiled artifact lands at `robyn/robyn.cpython-3XX-<plat>.so` (i.e. *inside* the `robyn/` Python package, sibling to `__init__.py`). `__version__ = get_version()` (`__init__.py:30`) reads `env!("CARGO_PKG_VERSION")` from the Rust binary (`lib.rs:32-35`) — single source of truth for the version is the Rust crate, not Python.

**DESIGN-NOTE (Lancelot):** Use the *exact same shape*: crate `lancelot`, `[lib] name="lancelot"`, `crate-type=["cdylib","rlib"]`, maturin `module-name="lancelot._lancelot"` (prefer a `_`-prefixed inner module so the public Python API in `lancelot/__init__.py` re-exports a curated surface and the raw extension stays private — Robyn exposes `robyn.robyn` directly, which leaks the binding to users; a `_lancelot` inner module is cleaner). Version: single source = `env!("CARGO_PKG_VERSION")`, exported as `get_version()`, consumed by `__init__.py`. Keep `extension-module` + `py-clone`.

### 1.2 The `.cargo/config.toml` macOS link trick (KNOWN — `.cargo/config.toml:1-11`)

```toml
[target.x86_64-apple-darwin]
rustflags = ["-C", "link-arg=-undefined", "-C", "link-arg=dynamic_lookup"]
[target.aarch64-apple-darwin]
rustflags = ["-C", "link-arg=-undefined", "-C", "link-arg=dynamic_lookup"]
```
On macOS a CPython extension must **not** hard-link `libpython`; undefined Python symbols are resolved at `dlopen` time from the host interpreter. `-undefined dynamic_lookup` tells the linker to allow undefined symbols. Without this, `cargo build` fails to link the `cdylib` on macOS. Linux/Windows don't need this (PyO3 `extension-module` handles it).

**DESIGN-NOTE:** Ship the identical `.cargo/config.toml`. This is the single most common "my Rust+Python build fails on a Mac dev laptop" footgun; copy it verbatim and you skip a class of issues.

### 1.3 The binding contract is a hand-written `.pyi` (KNOWN — `robyn/robyn.pyi`, 576 lines)

Robyn does **not** auto-generate stubs. `robyn.pyi` is hand-maintained and is the *human-readable ABI*. It declares every Rust class' Python-visible method signature + docstring. Notable: it documents `Server.start(self, socket, workers, client_timeout, keep_alive_timeout)` (`robyn.pyi:517`) — **note the `.pyi` is already drifted from `processpool.py:220` which calls `server.start(socket, workers)` with only 2 args** (the Rust side has defaulted/positional-optional params; the `.pyi` lists 4). This is a real drift bug in Robyn and a lesson:

**DESIGN-NOTE:** Auto-generate Lancelot's `.pyi` from the PyO3 class definitions (e.g. via `pyo3-stub-gen` or a build script) so the stub can never drift from the ABI. Robyn's hand-written stub already lies about `Server.start`'s arity — a generated stub would not.

### 1.4 `FunctionInfo` — the entire handler ABI is one dataclass (KNOWN)

`robyn.pyi:54-71`:
```python
@dataclass
class FunctionInfo:
    handler: Callable          # the Python callable (already a closure, see §3)
    is_async: bool             # asyncio.iscoroutinefunction result, computed once
    number_of_params: int      # len(inspect.signature(...).parameters), computed once
    args: dict                 # the parameters OrderedDict (name -> inspect.Parameter)
    kwargs: dict               # injected dependency map (router/global deps)
```
This is the *only* thing Rust knows about a user handler. Constructed in Python at registration:
- HTTP route: `router.py:341-360` — `FunctionInfo(async_inner_handler, True, len(params), params, new_injected_dependencies)` (async) or `(inner_handler, False, …)` (sync).
- Middleware: `router.py:403-409`.
- Startup/shutdown events: `__init__.py:322` — `FunctionInfo(handler, is_async, 0, {}, {})` (zero params, no deps).
- WebSocket connect/message/close: `ws.py:273-299` — three `FunctionInfo`s, all `is_async=True`.

Rust calls `handler(*args)` (arity from `number_of_params`) and never re-introspects. This is the core perf decision: **all reflection happens once at boot, never per request.** See `teardowns/ROBYN.md §3.2` for the Rust `execute_http_function` side.

**STEAL #1 (Lancelot):** Adopt this exact ABI. One `FnInfo { handler: PyObject, is_async: bool, n_params: usize, params: Py<PyDict>, deps: Py<PyDict> }` pyclass. The Python layer does *all* signature/typing work at decoration time and hands Rust an opaque callable + arity. Rust's hot path is then: look up route → clone `FnInfo` (cheap, `py-clone`) → build arg tuple of length `n_params` → call. Zero `inspect` on the request path. This is *the* reason a Rust-runtime Python framework can beat FastAPI: FastAPI re-resolves dependencies and validates per request through a Python-level resolver; Robyn/Lancelot front-loads it.


---

## 2. The developer-facing API — decorators & route registration (KNOWN)

### 2.1 The class hierarchy

`__init__.py`:
- `BaseRobyn(ABC)` (`__init__.py:77-680`) — holds all routing/middleware/DI/openapi/mcp state and every decorator. Constructed with `file_object: str` (the caller passes `__file__`), `config`, `openapi`, `dependencies`.
- `Robyn(BaseRobyn)` (`__init__.py:683-745`) — adds `.start(host, port, _check_port, client_timeout=30, keep_alive_timeout=20)`.
- `SubRouter(BaseRobyn)` (`__init__.py:748-890`) — adds `prefix` and overrides every verb decorator to prepend the prefix via `__add_prefix` (`__init__.py:753-770`).
- `ALLOW_CORS(app, origins, headers=None)` (`__init__.py:893-944`) — a free function that installs a `@app.before_request()` CORS middleware + sets default response headers.

Constructor key state (`__init__.py:111-123`): `router=Router()`, `middleware_router=MiddlewareRouter()`, `web_socket_router=WebSocketRouter()`, `request_headers`/`response_headers` as Rust `Headers({})` singletons, `directories=[]`, `event_handlers={}`, `exception_handler=None`, `authentication_handler=None`, `included_routers=[]`, `_mcp_app=None`, `_added_routes:set[str]=set()` (dup-route guard).

### 2.2 The decorator surface

Every HTTP verb is a method returning an `inner(handler)` decorator that calls `self.add_route(...)`. Full list with exact signatures:

| Decorator | Signature (`__init__.py`) | Notes |
|---|---|---|
| `@app.get` | `get(endpoint, const=False, auth_required=False, openapi_name="", openapi_tags=["get"])` `:366` | only verb with `const` |
| `@app.post` | `post(endpoint, auth_required=False, openapi_name="", openapi_tags=["post"])` `:389` | |
| `@app.put` | `:417` | |
| `@app.delete` | `:445` | |
| `@app.patch` | `:473` | |
| `@app.head` | `:501` | |
| `@app.options` | `:529` | |
| `@app.connect` | `:557` | maps to `HttpMethod.CONNECT` |
| `@app.trace` | `:585` | |
| `@app.before_request(endpoint=None)` | `:240` | `None` ⇒ global middleware |
| `@app.after_request(endpoint=None)` | `:248` | |
| `@app.websocket(endpoint)` | `:295` → `create_websocket_decorator` | |
| `@app.exception` | `:363` sets `self.exception_handler` | one global handler |
| `app.startup_handler(fn)` / `app.shutdown_handler(fn)` | `:324/:327` | not decorators-with-args, take fn directly |

**Mutable-default-arg smell (KNOWN):** every verb uses `openapi_tags: list[str] = ["get"]` as a *mutable default argument* (e.g. `__init__.py:372`). Robyn gets away with it only because it never mutates the list in place (it's passed through to `add_route` → `Route` NamedTuple). Still a latent bug.

**DESIGN-NOTE:** Lancelot should use `openapi_tags: tuple[str, ...] | None = None` and default inside the body. Don't copy the mutable default. Also: collapse the 9 near-identical verb methods into one `_verb(method)` factory + `get = _make_verb(HttpMethod.GET)` — Robyn's `__init__.py` is 969 lines mostly because of this copy-paste; ~400 lines are 9× the same body.

### 2.3 `add_route` — the registration pipeline (`__init__.py:151-221`, KNOWN)

```
add_route(route_type, endpoint, handler, is_const=False, auth_required=False, openapi_name="", openapi_tags=None)
 1. injected_dependencies = self.dependencies.get_dependency_map(self)     # :173
 2. str→HttpMethod via a dict literal (GET/POST/PUT/DELETE/PATCH/HEAD/OPTIONS)  # :177-187
 3. normalized_endpoint = _normalize_endpoint(endpoint)                    # :190
      - strip, rstrip('/'), ensure leading '/', "" → "/" (root), None invalid  (:33-67)
 4. if auth_required: middleware_router.add_auth_middleware(ep, type)(handler)  # :195-196
 5. route_key = f"{route_type}:{ep}"; if in self._added_routes: raise ValueError("Route ... already exists")  # :199-202
 6. self._added_routes.add(route_key)                                      # :205
 7. self.router.add_route(route_type, ep, handler, is_const, auth_required, openapi_name, openapi_tags, self.exception_handler, injected_dependencies)  # :207-217
```
Returns the wrapped handler (so `@app.get(...)` rebinds the name to the wrapper — important: the *registered* object is the closure, not the user fn).

**STEAL:** the exact-duplicate-route guard (`_added_routes` set, key = `"{method}:{path}"`, raise on collision) is a good DX default — FastAPI silently shadows; Robyn fails loud at import time. Copy this.

### 2.4 `Router.add_route` — where the closure is built (`router.py:136-360`, KNOWN — the heart of the Python layer)

This is the most important Python function in Robyn. At **registration time** it:

1. `route_param_names = parse_route_param_names(endpoint)` (`router.py:150`) — walk string for `:name` (`_param_utils.py:170-192`).
2. `handler_params = inspect.signature(handler).parameters` — **called exactly once, here, never per request** (`router.py:151`).
3. Warns if route declares `:params` the handler doesn't accept (`router.py:154-161`).
4. `pydantic_params = detect_pydantic_params(handler_params)` + `check_pydantic_installed_for_handler` (`router.py:164-165`) — zero cost if Pydantic unused (lazy import, §5).
5. Warns if a GET/HEAD handler has a Pydantic body param (`router.py:167-175`).
6. Builds the closure `wrapped_handler(*args, **kwargs)` (`router.py:177-278`, the 3-phase resolver — §4).
7. Wraps in `async_inner_handler` or `inner_handler` (`router.py:280-330`) which: call `wrapped_handler`, run `self._format_response(...)`, and catch `QueryParamValidationError`→400, `PydanticBodyValidationError`→422, generic `Exception`→re-raise or `exception_handler`.
8. Filters injected deps to only those the handler names (`router.py:334-339`).
9. `iscoroutinefunction(handler)` chooses async vs sync `FunctionInfo`; appends `Route(route_type, endpoint, function, is_const, auth_required, openapi_name, openapi_tags)` to `self.routes` (`router.py:341-360`).

`Route` is a `NamedTuple` (`router.py:43-51`): `(route_type, route, function:FunctionInfo, is_const, auth_required, openapi_name, openapi_tags)`. The whole route table is just `list[Route]` (`router.py:73`) — no trie/regex on the Python side; the Rust `matchit` router (see `teardowns/ROBYN.md §5`) does the actual matching after `processpool.spawn_process` feeds it `server.add_route(...)`.

**STEAL #3 (Lancelot):** front-load *all* reflection into `Router.add_route`. Compute and freeze: param names, signature, pydantic params, route param names, the resolver closure. The request path then has zero `inspect.*` calls. This is the single biggest ergonomics-vs-speed lever and it is *invisible to the user* — they still write plain typed handlers.


---

## 3. The scaling model — `processpool.py` (KNOWN — *the* file for Lancelot)

This is how a Python web framework beats the GIL. Two orthogonal axes:

- **`processes`** = number of OS processes (forked via `multiprocess`). Each is a fully independent Python interpreter → independent GIL.
- **`workers`** = number of actix worker threads *inside one process's Rust runtime* (Rust threads, no GIL contention for the I/O/parsing work; Python is only re-entered for the handler call).

### 3.1 The shared socket — `SocketHeld` + `try_clone()` (KNOWN)

`run_processes` (`processpool.py:15-68`):
```python
socket = SocketHeld(url, port)                       # :33  — Rust binds ONE listening socket
process_pool = init_processpool(... socket, workers, processes, ...)   # :35
def terminating_signal_handler(_sig,_frame):         # :52
    for process in process_pool: process.kill()      # :55  — SIGINT/SIGTERM kills all children
signal.signal(SIGINT, ...); signal.signal(SIGTERM, ...)   # :57-58
for process in process_pool: process.join()          # :65-66  — parent blocks on children
```
`SocketHeld(url, port)` is a Rust pyclass (`robyn.pyi:10-14`) that creates the listening socket with `SO_REUSEADDR`/`SO_REUSEPORT` (see `Cargo.toml:32` `socket2` with `features=["all"]`; mechanism detailed in `teardowns/ROBYN.md §10`). `socket.try_clone()` (`robyn.pyi:13`, called `processpool.py:108`) returns a *new fd referencing the same kernel socket* so every child process can `accept()` on it — the kernel load-balances connections across processes (SO_REUSEPORT).

### 3.2 `init_processpool` — fork-vs-single decision (`processpool.py:71-130`, KNOWN)

```python
if sys.platform.startswith("win32") or processes == 1:        # :88
    spawn_process(... socket, workers, ...)                   # :89  — NO fork; run in-process
    return process_pool                                       # :105  (empty list)

for _ in range(processes):                                    # :107
    copied_socket = socket.try_clone()                        # :108  — one cloned fd per child
    process = Process(target=spawn_process, args=(..., copied_socket, workers, ...))  # :109-126
    process.start(); process_pool.append(process)             # :127-128
```
Key facts:
- **Windows or `processes==1` ⇒ no child process at all** — `spawn_process` runs in the main process. So single-process mode has zero IPC/fork overhead.
- **`Process` is `multiprocess.Process`** (`processpool.py:6`), not stdlib `multiprocessing`. `multiprocess` uses `dill` instead of `pickle`, so it can pickle closures, lambdas, and the `FunctionInfo`-wrapped handlers (stdlib `multiprocessing` cannot pickle the route closures). This is *why* Robyn depends on `multiprocess` (`pyproject.toml:29`). `__init__.py:724-726` also calls `mp.allow_connection_pickling()` if present, so the cloned socket survives the fork pickle.
- **Fork vs spawn:** `multiprocess.Process` defaults to the platform default start method. On Linux that is **fork** (child inherits the already-imported app, routes, the Rust extension state); on macOS modern Python defaults to **spawn** but Robyn doesn't force a method — it relies on `dill` to re-serialize the whole arg tuple (directories, headers, routes, middlewares, websockets, event handlers, the cloned socket) into the child. The cloned socket fd is passed *as a pickled argument*, not inherited — that's why `allow_connection_pickling()` is needed.
- No graceful drain: `terminating_signal_handler` calls `process.kill()` (SIGKILL-ish) on every child (`processpool.py:55`). In-flight requests are dropped.

### 3.3 `spawn_process` — one Rust runtime per process (`processpool.py:149-225`, KNOWN)

```python
loop = initialize_event_loop()                       # :179
server = Server()                                     # :181  — fresh Rust Server pyclass per process
for directory in directories: server.add_directory(*directory.as_list())   # :185-186
server.apply_request_headers(request_headers)         # :188
server.apply_response_headers(response_headers)       # :190
server.set_response_headers_exclude_paths(excluded_response_headers_paths)  # :192
for route in routes:                                  # :194
    route_type, endpoint, function, is_const, auth_required, openapi_name, openapi_tags = route
    server.add_route(route_type, endpoint, function, is_const)   # :196
for mt, fn in global_middlewares: server.add_global_middleware(mt, fn)      # :198-199
for mt, ep, fn, rt in route_middlewares: server.add_middleware_route(mt, ep, fn, rt)  # :201-202
if STARTUP in event_handlers: server.add_startup_handler(event_handlers[STARTUP])      # :204-205
if SHUTDOWN in event_handlers: server.add_shutdown_handler(event_handlers[SHUTDOWN])   # :207-208
for endpoint in web_sockets:                          # :210
    server.add_web_socket_route(endpoint, ws["connect"], ws["close"], ws["message"])   # :212-217
try:
    server.start(socket, workers)                     # :220  — hands the cloned socket + worker count to Rust
    loop = asyncio.get_event_loop(); loop.run_forever()   # :221-222
except KeyboardInterrupt: loop.close()                # :223-224
```
So the **route table is rebuilt in every child** from the pickled `routes` list — there's no shared route table; each process has its own Rust `Server`. `server.start(socket, workers)` is where Rust spins the actix `System` with `workers` worker threads bound to the cloned socket (full Rust side: `teardowns/ROBYN.md §4, §10`).

### 3.4 The event loop — uvloop with fallbacks (`processpool.py:133-147`, KNOWN)

```python
def initialize_event_loop():
    if sys.platform.startswith("win32") or sys.platform.startswith("linux-cross"):
        loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop); return loop  # :135-137
    else:
        import uvloop                                  # :141
        uvloop.install(); loop = uvloop.new_event_loop(); asyncio.set_event_loop(loop); return loop  # :143-146
```
uvloop everywhere except Windows and cross-compiled Linux (arm). `pyproject.toml:33` gates the uvloop dep with the marker `sys_platform != 'win32' and platform_python_implementation == 'CPython' and platform_machine != 'armv7l'` so uvloop isn't even installed where it can't run. The async handlers run on this loop; Rust drives it via `pyo3-async-runtimes` (`Cargo.toml:18`).

### 3.5 The defaults & `--fast` math (`argument_parser.py`, KNOWN)

| Flag | Default | `--fast` value | Source |
|---|---|---|---|
| `--processes` | `1` | `(os.cpu_count() * 2) + 1` | `argument_parser.py:107, 111` |
| `--workers` | `1` | `2` | `argument_parser.py:108, 112` |
| `--log-level` | `INFO` (`DEBUG` if `--dev`) | `WARNING` | `argument_parser.py:109, 126-129` |
| `--dev` | off | — (mutually exclusive w/ `--fast`, `:121`) | `argument_parser.py:120-124` |

`--fast` total concurrency ≈ `((cpu*2)+1) processes × 2 workers`. On an 8-core box: 17 processes × 2 = 34 actix worker threads across 17 independent GILs. `--dev` forbids `processes!=1 or workers!=1` (`argument_parser.py:123-124`) — dev mode is strictly single-process for reload sanity.

**STEAL #2 (Lancelot — the scaling model verbatim):**
- One Rust-bound `SocketHeld` with `SO_REUSEPORT`; `.try_clone()` per child.
- `multiprocess` (dill) `Process`, **not** stdlib `multiprocessing`, so handler closures pickle.
- `processes==1 or win32` ⇒ run in-process, zero fork cost (the common dev/startup case is fast).
- Two axes: processes (GIL escape) × Rust worker threads (I/O parallelism, no GIL).
- uvloop with the exact platform-marker gating.

**AVOID / IMPROVE for Lancelot:**
- Robyn has **no graceful shutdown** (`process.kill()` drops in-flight requests, `processpool.py:55`). Lancelot should SIGTERM children, let actix drain with a timeout, then SIGKILL. Startups doing payments/AI streaming cannot drop requests on deploy.
- Robyn rebuilds the entire route table per child via dill (`spawn_process`), so boot time scales with `processes × route_count`. For Lancelot, prefer `fork` on Linux (children inherit the already-built Rust `Server`) and only fall back to dill-spawn on macOS/Windows. Robyn leaves the start method unset; choose it explicitly.
- No auto-restart of a crashed worker. Add a supervisor loop (parent re-spawns a child whose `process.is_alive()` goes False) — trivial and a big reliability win for the startup audience.
- `os.cpu_count()` ignores cgroup CPU quotas (containers). Lancelot's `--fast` should read the cgroup v2 `cpu.max` (or `len(os.sched_getaffinity(0))`) so it doesn't spawn 65 processes in a 2-vCPU container.


---

## 4. Typed params WITHOUT FastAPI DI — the 3-phase resolver (KNOWN)

Robyn rejects FastAPI's `Depends()`/`Annotated` machinery. Instead, the closure `wrapped_handler` (`router.py:177-278`) resolves args by **type identity** and **reserved names**, in 3 phases, all driven off the signature snapshot taken at registration.

### 4.1 Fast-path bailout (`router.py:178-181`)
```python
request = next(filter(lambda it: isinstance(it, Request), args), None)
if not request or (len(handler_params) == 1 and next(iter(handler_params)) is Request):
    return handler(*args, **kwargs)
```
If the handler is the classic `def h(request): ...` (single param), skip the entire resolver and call directly. Zero overhead for the common case.

### 4.2 Phase 1 — type-annotation match (`router.py:183-231`)
A fixed `type_mapping` (`router.py:183-195`):
```
"request"→Request, "query_params"→QueryParams, "headers"→Headers, "path_params"→PathParams,
"body"→Body, "method"→Method, "url"→Url, "form_data"→FormData, "files"→Files,
"ip_addr"→IPAddress, "identity"→Identity
```
For each handler param, if its annotation *is* one of these types (identity check `is`, not `isinstance`), bind `getattr(request, type_name)`. Then `issubclass` checks for user subclasses: `JsonBody`→`request.json()` (400 on bad JSON), `Body`→`request.body`, `QueryParams`→`request.query_params`, `TypedDict`→`request.json()` (`router.py:208-230`). The `Request` annotation always binds the request object itself.

### 4.3 Phase 1.5 — Pydantic body (`router.py:232-240`)
If `pydantic_params` (computed at registration), for each: `validate_pydantic_body(model_class, request.body)`; on error raise `PydanticBodyValidationError` (→422 in the wrapper). Only one Pydantic body param allowed (enforced at registration, `pydantic_support.py:200-209`).

### 4.4 Phase 2 — reserved-name match (`router.py:242-264`)
A dict of magic names regardless of annotation: `r/req/request`→request, `query_params, headers, path_params, body, method, url, ip_addr, identity, form_data, files`, plus `router_dependencies`/`global_dependencies` (the DI maps), plus `**kwargs`. Only names present in `handler_params` and not already resolved are taken.

### 4.5 Phase 3 — individual path/query params with coercion (`router.py:266-278` → `_param_utils.resolve_individual_params`)
Anything still unresolved is treated as an individual path or query param. `_param_utils.py:93-167`:
- annotation `empty` ⇒ treated as `str` (`:122-123`).
- `unwrap_optional` handles `T | None` / `Optional[T]` / PEP-604 unions (`_param_utils.py:39-51`).
- `is_list_type` + `get_list_element_type` handle `list[T]` from repeated query keys via `query_params.get_all` (`_param_utils.py:54-64, 139-143`).
- path params win over query params *only if the name is in `route_param_names`* (`_param_utils.py:131-136`).
- `coerce_value` (`_param_utils.py:67-90`): `str`/empty→passthrough, `int(value)`, `float(value)`, bool via frozensets `{"true","1","yes","on"}`/`{"false","0","no","off",""}` (`_param_utils.py:18-19`), else `target_type(value)` (covers Enum, UUID, …). Failure → `QueryParamValidationError` (→400).
- Resolution order if no value: param default → `None` if Optional → else raise `QueryParamValidationError(missing)`.

### 4.6 Ergonomics comparison — Robyn vs FastAPI vs Blazeio

| Aspect | FastAPI | Robyn | Blazeio | Lancelot target |
|---|---|---|---|---|
| Param injection | `Depends()`, `Annotated[T, ...]`, per-request resolver tree | type-identity + reserved-name, snapshot at registration | none — raw `request`, you parse manually | snapshot-at-registration like Robyn, **plus** opt-in `Annotated`-based providers for advanced DI |
| Body validation | Pydantic mandatory in signature | Pydantic **optional**, lazy, one body model max | manual | optional + lazy like Robyn; allow >1 model via field-level |
| Per-request reflection | yes (dependency graph walk) | **none** | none | none |
| Type coercion of query/path | Pydantic | hand-rolled `coerce_value` (`_param_utils.py:67`) | none | hand-rolled + Pydantic-typed fallback |
| Sub-dependencies / `yield` deps | yes (powerful, slower) | **no** — `DependencyMap` is a flat dict | no | provide a lightweight scoped-resource API, not FastAPI's full graph |
| Validation error shape | 422 + Pydantic detail | 400 (`QueryParamValidationError`) / 422 (`PydanticBodyValidationError`) | manual | 422 w/ structured detail for both, consistently |

**The core tradeoff:** FastAPI's DI is *more powerful* (sub-deps, `yield` cleanup, security scopes) but pays a per-request resolver cost and forces Pydantic. Robyn's is *faster and lighter* but cannot express "dependency that depends on another dependency" or request-scoped resource cleanup. Blazeio gives you nothing — max speed, you write the plumbing.

**DESIGN-NOTE (Lancelot's sweet spot):** Default to Robyn's snapshot resolver (fast, no Pydantic tax). Add an *opt-in* `Provide[T]` / `Annotated[T, Depends(...)]`-style API for the minority of routes that need real DI (DB sessions with cleanup, auth scopes). Resolve the dependency *plan* once at registration (topo-sort the graph, store the call order) so even DI routes have near-zero per-request reflection — this beats FastAPI on its own turf. Keep `coerce_value`'s exact bool frozensets and Optional/list handling — it's correct and battle-tested.

**STEAL:** the `_param_utils.py` module is shared verbatim between HTTP (`router.py`) and WebSocket (`ws.py`) param resolution. One coercion engine, two callers. Lancelot: one `params` crate/module, reused by HTTP/WS/MCP-tool arg binding.


---

## 5. Pydantic & DI — opt-in, lazy, flat (KNOWN)

### 5.1 Lazy Pydantic (`pydantic_support.py`, 225 lines)

The whole point: **zero import-time cost when Pydantic is unused.** `pydantic_support.py:34-48`:
```python
_BaseModel = None; _ValidationError = None; _pydantic_checked = False
def _ensure_pydantic():
    global _BaseModel, _ValidationError, _pydantic_checked
    if _pydantic_checked: return
    _pydantic_checked = True
    try:
        from pydantic import BaseModel, ValidationError
        _BaseModel, _ValidationError = BaseModel, ValidationError
    except ImportError:
        _BaseModel = _ValidationError = None
```
Module imports no Pydantic at top level (`router.py:15` and `openapi.py:14` import this module unconditionally — safe because no Pydantic at module scope, per the module docstring `pydantic_support.py:1-9`). `_ensure_pydantic` runs at most once, lazily.

Key functions:
- `detect_pydantic_params(handler_params)` (`:64-83`) — scan the *pre-computed* params OrderedDict for `BaseModel` subclasses. Returns `{}` if Pydantic absent. Called once at registration (`router.py:164`).
- `validate_pydantic_body(model_class, body)` (`:116-137`) — `model_class.model_validate_json(body)` (single-pass parse+validate, accepts str/bytes/bytearray, no intermediate dict). Returns `(instance, None)` or `(None, error_dict)`. `_sanitize_errors` (`:86-113`) makes Pydantic v2 error dicts JSON-safe (decodes bytes `input`, lists `loc` tuples, str-ifies `ctx`).
- `serialize_pydantic_response(res)` (`:160-176`) — `res.model_dump_json()` for a model, or `orjson.dumps([m.model_dump(mode="python") …])` for a list of models, else `None` (caller falls through). Called in `router._format_response` (`router.py:104-110`).
- `get_pydantic_openapi_schema(model_class)` (`:140-157`) — `model_json_schema(ref_template="#/components/schemas/{model}")`, pops `$defs` into component schemas (OpenAPI integration, `openapi.py:344-347`).
- Registration-time guards: `check_pydantic_installed_for_handler` (`:212-225`) raises `PydanticNotInstalledError` if a handler annotates a model but Pydantic isn't installed (error message points at `pip install "robyn[pydantic]"`), and `MultiplePydanticBodyError` if >1 model body param.

**STEAL #5 (Lancelot):** copy the lazy-singleton pattern exactly. Heavy validators (Pydantic, msgspec, attrs) must be *optional extras*, imported on first use, with a registration-time check that fails loud with an actionable `pip install lancelot[pydantic]` message. Use `model_validate_json` (not `model_validate(json.loads(...))`) — single pass, ~2× faster. Robyn caps at one body model per handler; Lancelot should allow multiple (bind each to a named param from sub-keys, or per-field), since AI apps often want `(req: ChatRequest, opts: GenOptions)`.

### 5.2 Dependency injection — a flat dict, no graph (`dependency_injection.py`, 71 lines, KNOWN)

`DependencyMap` (`dependency_injection.py:6-71`):
- `global_dependency_map: dict[str,Any]` and `router_dependency_map: dict[router, dict[str,Any]]`.
- `app.inject_global(**kw)` → `add_global_dependency` (`__init__.py:231-238`, `dependency_injection.py:24-31`).
- `app.inject(**kw)` → `add_router_dependency(self, **kw)` keyed by the router object itself (`dependency_injection.py:12-22`).
- `get_dependency_map(router)` → `{"global_dependencies": {...}, "router_dependencies": {...}}` (`:67-71`).
- Handlers receive these via the magic names `global_dependencies` / `router_dependencies` in Phase 2 of the resolver (`router.py:257-258`). There is **no resolution, no lifecycle, no sub-dependencies** — you get the raw dict and pull `deps["db"]` yourself.
- `merge_dependencies` (`:52-65`) copies only *global* deps that don't already exist when `include_router` merges a SubRouter (`__init__.py:641`).

This is deliberately primitive vs FastAPI. It's a service-locator, not DI. Pro: zero per-request cost, trivial to reason about. Con: no `yield` cleanup (DB session close), no scoped lifetimes, no override-for-tests ergonomics.

**DESIGN-NOTE:** For Lancelot's startup audience, a pure service-locator is too thin (they *will* want request-scoped DB sessions that auto-close). Provide both: (1) Robyn-style global/router dict for the 80% case; (2) an opt-in `@app.provide` that registers a (possibly `async` generator) provider resolved per-request with guaranteed teardown — but resolve the *plan* at registration so the per-request cost is just calling the cached provider list, not walking a graph. This is the one place to be *more* like FastAPI than Robyn.


---

## 6. Hot reload — `reloader.py` (164 lines, KNOWN) — DX for startups

### 6.1 Mechanism

`setup_reloader(directory_path, file_path)` (`reloader.py:81-112`):
```python
event_handler = EventHandler(file_path, directory_path)
event_handler.reload()                                   # :85  — start the server subprocess immediately
signal.signal(SIGINT, terminating_signal_handler)        # :99  — stop subprocess + observer
signal.signal(SIGTERM, terminating_signal_handler)       # :100
observer = Observer()                                    # :102  — watchdog Observer
observer.schedule(event_handler, path=directory_path, recursive=True)   # :103
observer.start()                                         # :104
while observer.is_alive(): observer.join(1)              # :107-108
```
`EventHandler(FileSystemEventHandler)` (`reloader.py:115-164`):
- `reload()` (`:128-150`): `stop_server()` (SIGTERM the old subprocess via `os.kill(self.process.pid, SIGTERM)`, `:124-126`), copy env + set `IS_RELOADER_RUNNING=True` (IPC flag so the child doesn't recursively start a reloader, `:133-134`), strip `--dev` from argv (`:136`), recompile any `*.rs` files (`compile_rust_files`, `:139`), `prev_process.kill()` (`:142-143`), then `subprocess.Popen([sys.executable, *arguments], env=new_env)` (`:145-148`).
- `on_modified(event)` (`:152-164`): debounce — if `time.time() - self.last_reload < 0.5: return` (`:160-161`); else `time.sleep(0.2)` to let the file finish writing (`:163`) then `self.reload()`.

So it's: **full process restart** (not in-process module reload). One watchdog observer in the parent; the actual server runs as a `Popen` child; on any file change the child is killed and re-spawned. The `IS_RELOADER_RUNNING` env var is the IPC channel preventing infinite reloader recursion (`cli.py:88` checks it before calling `setup_reloader`).

### 6.2 The Rust hot-compile path (`reloader.py:15-49`)

`compile_rust_files(directory_path)` globs `**/*.rs`, runs `python -m rustimport build <file>` per file (`:22-27`), and tracks the produced binaries (`.dll`/`.so` per platform, `:34-47`) so `clean_rust_binaries` (`:75-78`) can delete them on next reload. This is the `rustimport==1.3.4` dependency (`pyproject.toml:31`) — it lets a dev drop a `.rs` file next to their `app.py` and have it compiled+imported live. Niche but a real differentiator: write a hot path in Rust without leaving the project.

### 6.3 The CLI-only restriction (KNOWN — `__init__.py:139-149`)

`_handle_dev_mode` hard-fails if `--dev` is set but `ROBYN_CLI` env isn't:
```python
if cli_dev_mode and not is_robyn:
    raise SystemExit("Dev mode is not supported in the python wrapper. Please use the Robyn CLI...")
```
Reason: the reloader must `Popen` the same `sys.argv`; running `python app.py --dev` directly would have the app process *be* the thing that needs restarting. So `python -m robyn app.py --dev` is mandatory. `cli.py:103-135` `run()` sets `ROBYN_CLI=True` and routes `--dev` to `start_dev_server` → `setup_reloader`.

**STEAL / IMPROVE for Lancelot:**
- Copy the watchdog `Observer` + `Popen`-respawn + `IS_RELOADER_RUNNING` IPC flag + 0.5 s debounce + 0.2 s settle. These constants are well-tuned; don't reinvent.
- **AVOID the CLI-only restriction.** It's a real DX papercut — `python app.py --dev` is what every Flask/FastAPI+uvicorn user types reflexively. Lancelot can support it: if `--dev` and not already the reloaded child, `os.execv` into a reloader supervisor *in the same invocation* (the supervisor then `Popen`s the worker). uvicorn's `--reload` does exactly this; Robyn's limitation is self-imposed.
- Add reload-include/exclude globs (Robyn watches the whole dir recursively → `node_modules`, `.venv`, `__pycache__` churn triggers reloads). uvicorn's `--reload-dir`/`--reload-exclude` is table stakes.
- The live-Rust-compile (`rustimport`) is a genuinely novel DX hook for a "Rust core + Python" framework — keep it, market it: "drop a `.rs` file, it's hot in your handler."


---

## 7. WebSocket layer — `ws.py` (322 lines, KNOWN) — the Rust-channel bridge

Robyn's modern WS API is interesting for Lancelot because it shows the *async Rust↔Python channel* pattern.

### 7.1 The adapter (`ws.py:24-86`)

`WebSocketAdapter(connector, channel)` wraps a Rust `WebSocketConnector` + Rust `WebSocketChannel`:
- `receive_text()` → `await self._channel.receive()`; `None` ⇒ raise `WebSocketDisconnect` (`ws.py:36-44`). The channel is a Rust crossbeam/tokio channel exposed as a Python awaitable.
- `receive_bytes()` = `receive_text().encode()` (`:46-49`); `receive_json()` = `orjson.loads(receive_text())` (`:51-55`).
- `send_text(d)` → `await self._connector.async_send_to(self._connector.id, d)` (`:57-59`); `send_json` → `orjson.dumps(...).decode()` (`:65-67`); `broadcast(d)` → `self._connector.async_broadcast(d)` (`:69-71`).
- `close()` → `self._connector.close()` (`:73-75`). `.id` / `.query_params` proxy the connector (`:77-85`).

### 7.2 The decorator factory (`ws.py:92-322`)

`create_websocket_decorator(app_instance)` returns `websocket(endpoint)` → `decorator(handler)`. It builds **three** internal async functions and registers them as one `handlers` dict (`ws.py:269-299`):
- `connect_handler(ws)` (`:171-223`): creates the adapter, resolves the handler's extra params via `_resolve_ws_params` (DI + `query_params` + individual coerced query params, reusing `resolve_individual_params` with `path_params=None`, `ws.py:123-168`), then starts the user handler as a long-running `asyncio.create_task` stored in the module-global `_connection_tasks: dict[str, asyncio.Task]` keyed by `ws.id` (`ws.py:89, 202-203`). Calls user `on_connect` if defined.
- `message_handler(ws, msg)` (`:226-229`): a **no-op** in channel mode — Rust pushes messages straight into the channel; the dummy exists only to satisfy the 3-handler `add_web_socket_route` ABI (`robyn.pyi:508-516`).
- `close_handler(ws)` (`:232-266`): pops the task, `await asyncio.wait_for(task, timeout=5.0)` (`:242`), cancels on timeout, calls user `on_close`.

`handler.on_connect` / `handler.on_close` are attached as attributes (`ws.py:312-313`) so users write `@handler.on_connect`. Registered via `app_instance.add_web_socket(endpoint, handlers)` → `WebSocketRouter.routes[endpoint] = handlers` (`router.py:525-534`).

### 7.3 Tradeoffs & Lancelot notes

- **Pattern worth stealing:** the user writes a *linear* `async def handler(ws): while True: msg = await ws.receive_text(); ...` loop. The framework runs it as a detached task and uses a Rust→Python channel for backpressure. This is far nicer than ASGI's `receive()`/`send()` event dict. Lancelot should expose this exact ergonomic.
- **AVOID:** `inspect.signature(handler)` is called **per connection** inside `connect_handler` (`ws.py:183`) and again for `on_connect`/`on_close` (`:209, 255`). Unlike the HTTP path (snapshot once), WS re-introspects on every connect. For a chat/AI-streaming app with many short-lived WS connections this is wasteful. Lancelot: snapshot WS handler signatures at registration like the HTTP path.
- **AVOID:** `_connection_tasks` is a *module-global* dict (`ws.py:89`). With `processes>1` each process has its own copy (fine), but it's not bounded and a leaked task (handler that never returns and never disconnects cleanly) stays forever. Lancelot: per-app registry with a max + reaper.
- The 5.0 s `close_handler` join timeout (`ws.py:242`) is a reasonable default — copy it but make it configurable.


---

## 8. `robyn/ai.py` — the experimental AI module, fully dissected (308 lines, KNOWN)

> Module docstring (`ai.py:1-7`): *"This is an experimental AI integration module... A poc for the blog at https://sanskar.wtf/posts/the-future-of-robyn. Provides agent and memory functionality for building AI-powered applications for the demonstration of the vision."* — i.e. Robyn itself labels this a vision PoC, not production. Treat as a *shape*, not a dependency.

### 8.1 `AIConfig` (`ai.py:17-60`)

`AIConfig(**kwargs)` stores a dict, then `_load_from_env()` (`:24-44`) maps env → config keys **only if not already set**:
```
OPENAI_API_KEY→openai_api_key, ANTHROPIC_API_KEY→anthropic_api_key, GOOGLE_API_KEY→google_api_key,
AI_MODEL→model, AI_TEMPERATURE→temperature(float), AI_MAX_TOKENS→max_tokens(int)
```
`get/set/update/to_dict` are trivial dict ops (`:46-60`). Note: it *declares* Anthropic/Google keys but only OpenAI is ever used (see `SimpleRunner`). The multi-provider config is aspirational.

### 8.2 Memory: ABC + in-memory impl + facade (`ai.py:63-127`)

- `MemoryProvider(ABC)` (`:63-79`): `async store(user_id, data)`, `async retrieve(user_id, query=None) -> list[dict]`, `async clear(user_id)`. **`query` is accepted but never used for filtering** — `InMemoryProvider.retrieve` returns the whole list (`:93-94`). No vector search, no embedding, no relevance — purely append-and-return-all.
- `InMemoryProvider(MemoryProvider)` (`:82-98`): `dict[str, list[dict]]`, append on store, return-all on retrieve, `del` on clear. Process-local, lost on restart, not shared across `processes>1`.
- `Memory(provider, user_id, **kw)` (`:101-126`): facade. `provider="inmemory"` → `InMemoryProvider()`, anything else str → `ValueError`. `add(message, metadata)` stores `{"message":..,"metadata":..}`; `get(query)` → `provider.retrieve`; `clear()` → `provider.clear`.

### 8.3 Agent runner: ABC + OpenAI impl (`ai.py:129-204`)

- `AgentRunner(ABC)` (`:129-135`): `async run(query, **kwargs) -> dict`.
- `SimpleRunner(AgentRunner)` (`:138-204`):
  - `_get_client()` (`:145-154`): lazy `import openai`; `openai.OpenAI(api_key=...)`. Raises `ImportError` with `pip install openai` hint if missing. Cached in `self._client`.
  - `run(query, **kwargs)` (`:156-204`):
    - `messages=[{"role":"user","content":query}]`.
    - History injection (`:168-177`): `kwargs["context"]["history"]`, last 10 items, parses `"Query: "`/`"Response: "` prefixes back into `user`/`assistant` messages, `messages.insert(-1, ...)`.
    - `client.chat.completions.create(model=cfg.get("model","gpt-4o"), messages, temperature=cfg.get("temperature",0.7), max_tokens=cfg.get("max_tokens",1000))` (`:179-184`). **The synchronous OpenAI client is called inside an `async def`** — this blocks the event loop. Real bug for a server.
    - Returns `{"response": content, "query": query, "metadata": {"runner_type":"simple_openai","model":..,"usage":{prompt/completion/total tokens}}}` (`:186-200`); `debug` adds `config` dump.

### 8.4 `Agent` + factory functions (`ai.py:207-308`)

- `Agent(runner, memory=None, **kw)` (`:207-241`): `runner="simple"`→`SimpleRunner(**kw)`. `run(query, history=False, **kw)` (`:221-241`): if `memory and history` → `context["history"]=await memory.get()`; run runner; if memory → `memory.add(f"Query: {query}")` and `memory.add(f"Response: {result['response']}")`. **Memory is stringly-typed round-tripped** via `"Query: "`/`"Response: "` prefixes — lossy, fragile (a user message literally starting with "Response: " breaks role parsing).
- Factories: `memory(provider="inmemory", user_id="default", **kw)` (`:244-256`), `configure(**kw)->AIConfig` (`:259-276`), `agent(runner="simple", memory=None, config=None, **kw)->Agent` (`:279-308`, merges `config.to_dict()` into kwargs).

### 8.5 Verdict & Lancelot extraction

**What's good (the shape):** the ABC layering — `MemoryProvider`/`AgentRunner` as swappable strategies, thin facades (`Memory`/`Agent`), `configure()/agent()/memory()` factory functions, env-var config loading. This is the right *interface skeleton* for an AI-native framework's "batteries" layer.

**What's broken (don't copy the code):**
1. Sync OpenAI client inside `async def run` (`ai.py:179`) — blocks the loop. Must use `openai.AsyncOpenAI` and `await`.
2. Stringly-typed memory round-trip via `"Query: "/"Response: "` prefixes (`ai.py:174-177, 237-239`) — should store structured `{role, content}` messages.
3. `retrieve(query)` ignores `query` (`ai.py:93`) — no actual retrieval/RAG; "memory" is just an unbounded list.
4. OpenAI-only despite multi-provider config keys (`ai.py:26-33`) — Anthropic/Google never wired.
5. `InMemoryProvider` is process-local — useless with `processes>1` (Robyn's own default scaling). Memory must be out-of-process (Redis/pg) to work with the fork model.

**DESIGN-NOTE (Lancelot's AI-native layer — do it properly):**
- Keep the ABC skeleton: `LLMProvider` (async, streaming-first), `MemoryStore` (structured messages + optional vector retrieval), `Agent` (tool-calling loop), `configure()/agent()/memory()` factories + env config.
- **Async + streaming by default.** Every provider method `async`, returns an async iterator of tokens. AI startups live or die on TTFT/streaming; Robyn's blocking sync call is disqualifying. This is where Lancelot wins vs Robyn outright.
- **Structured memory**, not prefix-parsed strings. Default store = out-of-process (matches the fork scaling model — an in-memory store silently breaks at `processes>1`, which is `--fast`'s default).
- **Multi-provider for real** via the AI Gateway pattern (one interface, OpenAI/Anthropic/Gemini behind it) — Robyn declared the keys but never delivered; deliver it.
- Provider clients should be created once per process (in `spawn_process`, like `Server`), not per request.
- The default model/temp/max_tokens constants (`gpt-4o`/`0.7`/`1000`) are reasonable seeds but make them config-first; pin a *current* default model at ship time.


---

## 9. `robyn/mcp.py` — the real AI surface: MCP JSON-RPC 2.0 server (461 lines, KNOWN)

This is the strong module. It turns any Robyn app into an MCP server (resources/tools/prompts consumable by Claude Desktop etc.) at `POST /mcp`. This is the **template for Lancelot's AI-native primitive.**

### 9.1 Schema auto-generation from signatures (`mcp.py:19-72`)

- `_extract_uri_params(uri)` (`:19-21`): `re.findall(r"\{(\w+)\}", uri)` → `echo://{message}` ⇒ `["message"]`.
- `_generate_schema_from_function(func)` (`:24-57`): build JSON Schema from `inspect.signature`. Type map: `str→string, int→integer, float→number, bool→boolean`, anything with `__origin__` (generics) → `object`, default `string`. Param required iff no default. Returns `{"type":"object","properties":{...},"required":[...]}`.
- `_generate_prompt_args_from_function(func)` (`:60-72`): `[{"name","description":f"Parameter {n}","required": no-default}]`.

### 9.2 Dataclasses + error type (`mcp.py:75-122`)

`MCPResource(uri,name,description?,mimeType?)`, `MCPTool(name,description,inputSchema)`, `MCPPrompt(name,description,arguments?)`, `MCPMessage(jsonrpc="2.0",id,method,params,result,error)`, `MCPError(code,message,data)` (`:75-122`).

### 9.3 `MCPHandler` — the JSON-RPC dispatcher (`mcp.py:125-339`)

Registries: `resources/tools/prompts: dict[str,Callable]` + parallel `*_metadata` dicts. `server_info={"name":"robyn-mcp-server","version":"1.0.0"}` (`:135`).

`handle_request(request_data)` (`:152-185`) dispatches on `method`:
| method | handler | returns |
|---|---|---|
| `initialize` | `_handle_initialize` `:187-193` | `{"protocolVersion":"2024-11-05","capabilities":{"resources":{"subscribe":False,"listChanged":False},"tools":{},"prompts":{}},"serverInfo":{...}}` |
| `resources/list` | `:195-200` | `{"resources":[asdict(meta)...]}` |
| `resources/read` | `:234-281` | `{"contents":[{"uri","mimeType","text":str(content)}]}` |
| `tools/list` | `:283-288` | `{"tools":[asdict(meta)...]}` |
| `tools/call` | `:290-306` | `{"content":[{"type":"text","text":str(result)}]}` |
| `prompts/list` | `:308-313` | `{"prompts":[asdict(meta)...]}` |
| `prompts/get` | `:315-339` | `{"description":..,"messages":[{role,content}]}` |
| unknown | — | raise `MCPError(-32601, "Method not found")` |

Error envelope (`:181-185`): `MCPError` → `{"jsonrpc":"2.0","id":..,"error":{"code","message","data"}}`; generic `Exception` → `-32603 Internal error` + `str(e)`. JSON-RPC error codes used: `-32601` (method not found), `-32602` (invalid params: missing URI / resource/tool/prompt not found), `-32603` (internal / handler param error), `-32700` (parse error, `mcp.py:378`).

**URI-template matching** `_match_uri_template(requested_uri)` (`:202-232`): for each registered template, extract `{param}` names; no params ⇒ exact-match; else build a regex replacing `{param}` with `(.+)` if param name ∈ `{"path","file_path","directory"}` else `([^/]+)`, anchor `^...$`, return `(template, {param: group})`. This is the *resource routing* — clever: path-like params greedily match slashes, segment params don't.

**Handler invocation** is signature-aware (`_handle_read_resource :249-272`): `iscoroutinefunction` → await; if `uri_params` → `handler(**uri_params)`; elif handler takes params → `handler(params)` (whole dict); else `handler()`. `TypeError` → `MCPError(-32603, "Handler parameter error")`. `tools/call` (`:290-306`): `handler(**arguments)` (await if async). `prompts/get` (`:315-339`): normalizes result — `str`→`[{"role":"user","content":{"type":"text","text":result}}]`, `list`→as-is, else stringify.

### 9.4 `MCPApp` — the decorator surface mounted on Robyn (`mcp.py:342-461`)

`MCPApp(robyn_app)` (`:342-348`): holds `MCPHandler`, calls `_setup_routes()`.

`_setup_routes` (`:350-381`): registers **one route** `@self.app.post("/mcp")` → `handle_mcp_request(request)`. It robustly parses the body: try `request.json()`, fallback to `json.loads(body)` (str or bytes), handle double-encoded JSON-string, then `await self.handler.handle_request(data)`; `JSONDecodeError` → `-32700`, other → `-32603`. Returns the dict (Robyn's `_format_response` serializes via orjson).

Decorators (auto-generate metadata from the function if not supplied):
- `@app.mcp.resource(uri=None, name=None, description=None, mime_type=None)` (`:383-409`): defaults `uri=f"function://{func.__name__}"`, `name=func.__name__.replace("_"," ").title()`, `description=func.__doc__ or f"Resource: {name}"`, `mime_type="text/plain"`. → `handler.register_resource`.
- `@app.mcp.tool(name=None, description=None, input_schema=None)` (`:411-435`): defaults `name=func.__name__`, `description=func.__doc__ or f"Tool: {name}"`, `input_schema=_generate_schema_from_function(func)`. → `register_tool`.
- `@app.mcp.prompt(name=None, description=None, arguments=None)` (`:437-461`): analogous, `arguments=_generate_prompt_args_from_function(func)`. → `register_prompt`.

Exposed lazily on the app via `@property def mcp` (`__init__.py:652-680`): `if self._mcp_app is None: self._mcp_app = MCPApp(self); return self._mcp_app` — so the `/mcp` route is only added if the user touches `app.mcp` (zero cost otherwise). `MCPApp` is in `__all__` (`__init__.py:967`).

### 9.5 Verdict & Lancelot extraction — this is the keeper

**STEAL #4 (Lancelot's AI-native primitive — copy this module's design wholesale):**
- The decorator-driven registry (`@app.mcp.tool/.resource/.prompt`) with **auto JSON-schema from type hints** is exactly the ergonomic AI builders want. Lancelot should ship this on day one as a first-class primitive (not "experimental").
- Lazy mount via a `@property` so MCP costs nothing unless used — copy verbatim.
- URI-template resource routing with the `(.+)` vs `([^/]+)` distinction for path-like params — copy verbatim, it's correct.
- Signature-aware handler dispatch (sync/async, uri-params vs whole-params vs no-params) — copy.
- JSON-RPC envelope + the exact error code set (`-32700/-32601/-32602/-32603`) — copy; it's spec-compliant.

**Gaps to close in Lancelot's version:**
1. **Streaming / SSE transport.** Robyn's MCP is request/response `POST /mcp` only. The MCP spec also defines an SSE/stream transport; AI clients increasingly want streamed tool output. Lancelot already has SSE (`responses.SSEResponse`); wire MCP over SSE too.
2. **Schema inference is shallow** — generics all collapse to `"object"` (`mcp.py:47-49`); no Pydantic model → JSON-schema bridge. Lancelot already has `get_pydantic_openapi_schema` (`pydantic_support.py:140`); reuse it so MCP tool args can be Pydantic models with rich schemas.
3. **No auth on `/mcp`.** It's an open POST endpoint. Lancelot should let `@app.mcp` accept `auth_required=True` (it already has the auth middleware, §10) — MCP servers exposing tools must be authenticatable.
4. `protocolVersion` hardcoded `"2024-11-05"` (`mcp.py:190`) — make it a constant that's bumped per MCP spec release; negotiate against the client's requested version.
5. Tool/resource results stringified via `str(content)` (`mcp.py:281, 306`) — support structured/JSON and binary content types per spec.

**This module is the single most directly reusable thing in Robyn for Lancelot's "AI backend" goal.** It is ~460 lines, dependency-free (stdlib `inspect`/`json`/`re`/`dataclasses` only), and demonstrates the entire "framework as MCP server" pattern. Lancelot should reimplement it as a core, non-experimental primitive with the 5 gaps above closed.


---

## 10. Remaining API surface — auth, OpenAPI, sub-routers, lifecycle, CORS (KNOWN)

### 10.1 Authentication (`authentication.py`, 95 lines)
- `TokenGetter(ABC)` (`:16-45`): `scheme` (defaults to class name), `get_token(request)->str|None`, `set_token(request, token)`. Concrete: `BearerGetter` (`:75-95`) — reads `Authorization: Bearer <t>`, strips `"Bearer "` (7 chars, `:91`).
- `AuthenticationHandler(ABC)` (`:48-72`): wraps a `TokenGetter`; `unauthorized_response` → 401 + `WWW-Authenticate: <scheme>` (`:57-63`); abstract `authenticate(request)->Identity|None`.
- Wiring: `app.configure_authentication(handler)` (`__init__.py:643-650`) sets it on the middleware router. `auth_required=True` on any route causes `add_route` to call `middleware_router.add_auth_middleware(ep, type)(handler)` (`__init__.py:195-196`) which installs a BEFORE_REQUEST middleware that calls `authentication_handler.authenticate(request)`; `None` → returns `unauthorized_response`; else sets `request.identity` and returns the request (`router.py:413-441`). `AuthenticationNotConfiguredError` if `auth_required` but no handler.

**Lancelot:** clean, minimal, no session/cookie auth, no OAuth flows — just a token-getter + identity ABC. Good enough as a primitive; ship it, and make MCP/`/docs` honor `auth_required` (Robyn already supports `auth_required` on the openapi routes, `__init__.py:347-360`).

### 10.2 OpenAPI (`openapi.py`, 498 lines)
- Generated lazily: `_add_openapi_routes` (`__init__.py:337-361`) adds `GET /openapi.json` (`openapi.get_openapi_config`) and `GET /docs` (Swagger UI from packaged `swagger.html` via `importlib.resources`, `openapi.py:484-491`), both `is_const=True`, and excludes them from response-header injection (`__init__.py:361`). Disabled by `--disable-openapi` (`argument_parser.py:73-79`) or an `openapi.json` override file (`openapi.py:474-482`).
- `add_openapi_path_obj` (`openapi.py:173-234`): `inspect.signature` + docstring → path object. Type→schema via `get_schema_object` (`:401-472`) handles `list[T]`, unions/`Optional` (`anyOf` + `{"type":"null"}`), Pydantic models (→ `$ref` components via `get_pydantic_openapi_schema`), TypedDicts, nested classes. Robyn path `:param` → OpenAPI `{param}` via regex (`openapi.py:310-316`).
- Spec root: `openapi 3.1.0` (`openapi.py:165`).

**Lancelot:** auto-OpenAPI from type hints + bundled Swagger UI is table stakes for the FastAPI audience. Robyn's `get_schema_object` recursion is a decent starting point; prefer routing *everything* through the Pydantic JSON-schema path when models are used (richer than the hand-rolled type map).

### 10.3 SubRouter & include_router (`__init__.py:613-641, 748-890`)
- `SubRouter(file_object, prefix="", config, openapi)` — every verb overridden to `self.__add_prefix(endpoint)` (`:753-770`, handles root/empty/double-slash normalization).
- `app.include_router(subrouter)` (`:613-641`): **mutates** the parent — `router.routes.extend(...)`, middleware lists extend, websocket routes re-prefixed, `dependencies.merge_dependencies`. Explicitly documented as non-deep-copy, caller must avoid conflicts. OpenAPI sub-paths merged via `add_subrouter_paths`.

**Lancelot:** the prefix-override pattern is fine; the mutate-parent `include_router` is acceptable but the `_added_routes` dup guard (§2.3) is *not* re-checked on include — a SubRouter can silently introduce a duplicate route. Lancelot should run the dup guard during include too.

### 10.4 Lifecycle & app boot
- `app.startup_handler(fn)` / `shutdown_handler(fn)` (`__init__.py:324-328`) → `FunctionInfo(fn, is_async, 0, {}, {})`; only `STARTUP`/`SHUTDOWN` events accepted (`:318-319`). Fed to Rust via `server.add_startup_handler/add_shutdown_handler` in `spawn_process` (`processpool.py:204-208`).
- `Robyn.start(host="127.0.0.1", port=8080, _check_port=True, client_timeout=30, keep_alive_timeout=20)` (`__init__.py:684-745`): env overrides `ROBYN_HOST/PORT/CLIENT_TIMEOUT/KEEP_ALIVE_TIMEOUT/BROWSER_OPEN` (`:702-706`); port-in-use interactive reprompt loop (`:708-715`) — **blocks on `input()`**, bad for containers; adds openapi routes; `mp.allow_connection_pickling()` (`:724-726`); `run_processes(...)`.

**Lancelot AVOID:** the interactive `input("Enter a different port:")` on port conflict (`__init__.py:712`) hangs headless/CI/container deploys. Fail fast with a clear error or honor a `--port` retry policy; never block on stdin in a server boot path.

### 10.5 CORS (`__init__.py:893-944`)
`ALLOW_CORS(app, origins, headers=None)` installs a `@app.before_request()` that 403s disallowed origins, 204s preflight `OPTIONS` with full `Access-Control-*` headers (Max-Age 3600), and sets default response CORS headers. Single-origin → echoes it; multi → `*` (then dynamic). Adequate; Lancelot should make it a first-class `app.add_cors(...)` not a free function.

---

## 11. Packaging & distribution — maturin + prebuilt wheels (KNOWN)

### 11.1 Build backend
`pyproject.toml:1-3`: `requires=["maturin>=1.0,<2.0"]`, `build-backend="maturin"`. `[tool.maturin] module-name="robyn"` (`:151-152`). Dev pins `maturin==1.7.4` (`:57`); linux-cross job pins maturin `v1.12.0` (`release-CI.yml:134`).

### 11.2 Release matrix (`.github/workflows/release-CI.yml`, KNOWN)
Jobs build wheels via `PyO3/maturin-action@v1`, args `--release --out dist`:
| Job | OS | Targets | CPython |
|---|---|---|---|
| `macos` (`:18-50`) | macos-latest | `aarch64-apple-darwin`, `x86_64`, `universal2-apple-darwin` | 3.10–3.14 |
| `windows` (`:52-80`) | windows-latest | `x64`, `x86` | 3.10–3.14 |
| `linux` (`:82-110`) | ubuntu-latest | `x86_64`, `i686`, `manylinux: auto` | 3.10–3.14 |
| `linux-cross` (`:112-161`) | ubuntu-latest | `aarch64`, `armv7` (musl, `PYO3_CROSS_LIB_DIR`) | 3.10–3.13 (abi cp310–cp313) |
| `merge` (`:163-181`) | bundles all wheel artifacts | | |
| `release` (`:184+`) | publishes | | |

So Robyn ships ~5 platforms × ~5 CPython = **~20–25 prebuilt binary wheels per release**. CPython-only (`Programming Language :: Python :: Implementation :: CPython`, `pyproject.toml:25`); no PyPy, no abi3/stable-ABI — a separate wheel per minor (cp310-cp310 … cp314-cp314). No universal/`sdist`-only fallback observed in the build args (`-i python --release --out dist`); users on an unbuilt platform must have a Rust toolchain to `pip install` from sdist.

### 11.3 Release profile (`Cargo.toml:46-55`)
```toml
[profile.release]
codegen-units = 1     # max cross-fn optimization (slow build, fast binary)
lto = "fat"           # whole-program LTO
panic = "abort"       # no unwind tables; smaller, faster; a Rust panic = process abort
strip = true          # strip symbols; small wheel
opt-level = 3
[profile.release.build-override] opt-level = 3
```
`panic="abort"` is significant: a Rust panic does not unwind into Python — it aborts the *worker process*. With `processes>1` and a supervisor, one bad request kills one worker, not the fleet. (Robyn has no supervisor — §3.5 AVOID — so a panic kills a worker permanently.)

### 11.4 Runtime dependency footprint (`pyproject.toml:27-40`)
Required: `inquirerpy==0.3.4`, `multiprocess>=0.70.18,<0.71`, `orjson>=3.11.5,<4`, `rustimport==1.3.4`, `uvloop~=0.22.1` (marker-gated: non-win, CPython, non-armv7), `watchdog>=6,<7`. Optional extras: `templating`→jinja2, `pydantic`→pydantic v2, `all`→both. **No Pydantic, no Starlette, no anyio, no httptools in the base install.** The wheel is the `.so` + ~6 small pure-Python deps.

### 11.5 Packaging recommendation for Lancelot

**Recommendation: maturin + prebuilt wheels (abi3) + sdist fallback. Specifically:**
1. **maturin** as the build backend — it is the de-facto standard for PyO3 (`PyO3/maturin-action@v1` makes the CI a copy-paste). Pin a recent maturin (`>=1.7`).
2. **Use the `abi3` (stable ABI) feature** (`pyo3/abi3-py310`) so you build **one wheel per platform** that works on CPython 3.10+, instead of Robyn's per-minor matrix (~5×5=25 wheels → ~5 wheels). Robyn does *not* do this (it ships cp310…cp314 separately) — Lancelot can cut its wheel matrix 5× and never need a new release just because CPython 3.15 ships. Tiny perf cost (abi3 forbids a few fast PyO3 paths); for a web framework the network dominates, so abi3 is the right call. *This is a concrete improvement over Robyn's packaging.*
3. **Ship the platform matrix Robyn ships**: macOS (universal2 covers both arches in one wheel — Robyn already does this), Linux glibc manylinux x86_64 + aarch64 (manylinux_2_28), Linux musl (Alpine — many startup Dockerfiles use `python:alpine`), Windows x64. armv7/i686 are low-priority for the startup/AI audience (servers are x86_64/aarch64) — defer them.
4. **Always publish an sdist** so `pip install lancelot` on an unsupported platform falls back to a source build (requires Rust). Robyn's CI doesn't appear to emit an explicit sdist — add `maturin sdist` to CI; it's free insurance.
5. **No pure-Python fallback.** A "Rust core" framework whose value *is* the Rust runtime has no meaningful pure-Python mode — don't build one; it would be a slow imposter that fragments the API. The sdist (source build) is the only fallback, gated on a Rust toolchain, which is acceptable because prebuilt wheels cover ~99% of the audience (manylinux/musllinux/macOS-universal2/win-x64).
6. **Copy `Cargo.toml`'s release profile verbatim** (`codegen-units=1, lto="fat", panic="abort", strip=true, opt-level=3`) — but only ship `panic="abort"` *with* the worker-supervisor (§3.5) so an aborting panic self-heals instead of permanently losing a worker.
7. **Copy `.cargo/config.toml`** (macOS dynamic_lookup) verbatim (§1.2).
8. Keep the runtime dep list as thin as Robyn's; make Pydantic/templating optional extras (`lancelot[pydantic]`, `lancelot[all]`) exactly like `pyproject.toml:37-40`.
9. Single version source = `env!("CARGO_PKG_VERSION")` surfaced as `get_version()`; auto-generate the `.pyi` from the PyO3 classes (Robyn's hand-written stub already drifted — §1.3).

**Pros of this strategy for a startup shipping Lancelot:** `pip install lancelot` "just works" with no Rust toolchain for ~all users (prebuilt wheels); abi3 means one wheel per platform and no churn on new CPython releases (lower release ops than Robyn); thin deps = fast installs, small images, few CVEs; sdist guarantees source-build escape hatch. **Cons / watch-outs:** the wheel build matrix + cross-compile (musl/aarch64) is non-trivial CI to stand up (mitigated: `maturin-action` is mostly declarative — clone Robyn's `release-CI.yml`); abi3 closes a few micro-opt PyO3 paths (immaterial for an I/O-bound web framework); musllinux/Alpine wheels need explicit cross jobs (Robyn's `linux-cross` job is the template).


---

## 12. End-to-end Python-side request trace (KNOWN — reimplementable)

A `GET /users/:id?verbose=true` with handler `async def h(id: int, verbose: bool = False, db = Depends-style via deps): ...`:

**Registration (once, at import):**
1. `@app.get("/users/:id")` → `BaseRobyn.get` → `inner(h)` → `add_route(GET, "/users/:id", h, ...)` (`__init__.py:384-385`).
2. `add_route`: deps map fetched, str→`HttpMethod.GET`, `_normalize_endpoint("/users/:id")`→`/users/:id`, dup-guard key `HttpMethod.GET:/users/:id` added, → `Router.add_route` (`__init__.py:173-217`).
3. `Router.add_route`: `route_param_names={"id"}` (`_param_utils.parse_route_param_names`), `handler_params=signature(h).parameters` snapshot, `detect_pydantic_params`→`{}` (no model), build `wrapped_handler` closure, wrap in `async_inner_handler`, `iscoroutinefunction`→True, append `Route(GET,"/users/:id",FunctionInfo(async_inner_handler,True,3,params,deps),False,False,"",["get"])` (`router.py:150-360`).

**Boot:** `app.start()` → `run_processes` → `SocketHeld(host,port)` → fork N `spawn_process` → each: `Server()`, `server.add_route(GET,"/users/:id",FunctionInfo,False)`, `server.start(socket,workers)` → actix `System` accepts on the SO_REUSEPORT socket (`processpool.py:33-220`).

**Per request (Rust → Python):**
1. Rust actix worker accepts conn, `matchit` routes `/users/:id` → the `FunctionInfo`, extracts `path_params={"id":"42"}`, `query_params={"verbose":["true"]}`, builds `PyRequest` (`teardowns/ROBYN.md §3`).
2. Rust acquires GIL, calls `FunctionInfo.handler(request)` (arity from `number_of_params`; the handler is `async_inner_handler`).
3. `async_inner_handler(request)` → `await wrapped_handler(request)` (`router.py:281-285`).
4. `wrapped_handler`: not the single-`Request` fast path. Phase 1: no annotation matches `Request`/component types/`JsonBody`/`Body`/`QueryParams`/TypedDict for `id`/`verbose`. Phase 1.5: no pydantic. Phase 2: `id`/`verbose` not reserved names; `db` *is* resolvable if named `router_dependencies`/`global_dependencies`. Phase 3: `unresolved={id,verbose}` → `resolve_individual_params({id:Param(int),verbose:Param(bool,default=False)}, query_params, path_params={"id":"42"}, {"id"})` → `id`: in `route_param_names` ∧ in path → `coerce_value("42",int,"id")`=42; `verbose`: not path param → query `get("verbose")`="true" → `coerce_value("true",bool,...)`=True. → `h(id=42, verbose=True)` (`router.py:177-278`, `_param_utils.py:93-167`).
5. handler returns e.g. a dict. `_format_response`: not Response/Streaming, `serialize_pydantic_response`→None, is `dict` → returned **bare** (Rust serializes via orjson, skipping Python `Response`/`Headers` construction — `router.py:112-117`).
6. Rust extracts the bare dict, orjson-serializes, builds the wire response, releases GIL (`teardowns/ROBYN.md §3.4`).

Failure paths: bad `int` for `id` → `QueryParamValidationError` → caught in `async_inner_handler` → 400 text (`router.py:286-291`); missing required → same; bad Pydantic body (if used) → `PydanticBodyValidationError` → 422 JSON (`router.py:292-297`); other exception → `exception_handler(err)` or re-raise (`router.py:298-304`).

**The reimplementable invariant:** every `inspect.*` / typing introspection happens in step 3 (registration), never in steps 4–6 (per request). Lancelot must preserve this.

---

## 13. Consolidated scorecard for Lancelot

### STEAL (copy the mechanism)
| # | What | Source | Why |
|---|---|---|---|
| 1 | `FunctionInfo` opaque-callable ABI (handler+is_async+arity+params+deps), all reflection at registration | `robyn.pyi:54-71`, `router.py:341-360` | the core perf primitive; invisible to users; this is *why* it beats FastAPI |
| 2 | `multiprocess`(dill)+`SocketHeld.try_clone()` SO_REUSEPORT fork × actix workers; `processes==1`⇒in-process | `processpool.py:33-220` | GIL escape + I/O parallelism, two clean axes |
| 3 | 3-phase param resolver (type-identity → reserved-name → coerced individual) sharing `_param_utils` w/ WS | `router.py:177-278`, `_param_utils.py` | typed handlers without FastAPI DI tax |
| 4 | MCP module wholesale (decorators, auto-schema, URI templates, JSON-RPC envelope, lazy mount) | `mcp.py` (all), `__init__.py:652-680` | the AI-native primitive; ~460 dep-free lines |
| 5 | Lazy-optional Pydantic (singleton import, `model_validate_json`, registration-time guard) | `pydantic_support.py:34-137` | zero cost when unused; opt-in heavy deps pattern |
| 6 | Dup-route guard (`_added_routes`, raise on collision at import) | `__init__.py:199-205` | fail-loud DX vs FastAPI silent shadowing |
| 7 | Watchdog `Observer`+`Popen`-respawn reloader (0.5s debounce, 0.2s settle, `IS_RELOADER_RUNNING` IPC) | `reloader.py:115-164` | well-tuned dev reload |
| 8 | Release profile `lto=fat/codegen-units=1/panic=abort/strip` + `.cargo/config.toml` macOS dynamic_lookup | `Cargo.toml:46-55`, `.cargo/config.toml` | fast small binary; the macOS link footgun fix |
| 9 | Live `.rs` hot-compile via rustimport in dev | `reloader.py:15-49` | unique "Rust core + Python" DX hook to market |

### AVOID / IMPROVE (Robyn's mistakes Lancelot should not repeat)
| What | Source | Fix for Lancelot |
|---|---|---|
| No graceful shutdown — `process.kill()` drops in-flight | `processpool.py:55` | SIGTERM→actix drain w/ timeout→SIGKILL |
| No crashed-worker supervisor (panic=abort permanently loses a worker) | `processpool.py` | parent re-spawns dead children |
| `os.cpu_count()` ignores cgroup quota | `argument_parser.py:106` | read cgroup `cpu.max` / `sched_getaffinity` |
| CLI-only `--dev` (SystemExit on `python app.py --dev`) | `__init__.py:144-149` | self-exec into reloader supervisor (uvicorn-style) |
| Reloader watches whole dir (.venv/__pycache__ churn) | `reloader.py:103` | include/exclude globs |
| Per-connection `inspect.signature` in WS | `ws.py:183,209,255` | snapshot at registration like HTTP |
| `ai.py` sync OpenAI in `async def` (blocks loop) | `ai.py:179` | AsyncOpenAI + await; streaming-first |
| `ai.py` stringly-typed memory `"Query: "/"Response: "` round-trip; in-mem store useless at processes>1 | `ai.py:174-177,237-239,82-98` | structured messages; out-of-process store default |
| `input()` on port conflict (hangs containers/CI) | `__init__.py:712` | fail fast / retry policy, never block stdin |
| Hand-written `.pyi` drifted (`Server.start` arity) | `robyn.pyi:517` vs `processpool.py:220` | auto-generate stubs from PyO3 |
| 9× copy-pasted verb methods (~400 LoC) | `__init__.py:366-611` | one `_verb(method)` factory |
| Mutable default arg `openapi_tags=["get"]` | `__init__.py:372` etc. | `tuple|None`, default in body |
| `/mcp` unauthenticated; MCP no streaming; shallow schema inference | `mcp.py:353,190,47-49` | honor `auth_required`; MCP-over-SSE; Pydantic→JSON-schema |
| `include_router` skips dup-route guard | `__init__.py:613-641` | re-run guard on include |

### Ergonomics positioning (Robyn vs FastAPI vs Blazeio vs Lancelot)
- **FastAPI**: richest DI (sub-deps, `yield`, scopes), Pydantic-mandatory, per-request resolver cost. Best types/DX, slowest.
- **Blazeio**: raw speed, sparse API, you hand-write parsing/validation/routing plumbing. Fastest, least batteries.
- **Robyn**: Rust runtime, registration-time reflection, type-identity injection, optional Pydantic, flat service-locator DI, strong MCP, weak `ai.py`, thin deps. Fast + decent DX, shallow DI/AI.
- **Lancelot target**: Robyn's snapshot-at-registration speed model + Robyn's MCP primitive (productionized + streaming + auth) + an *opt-in* DI graph resolved-once (beat FastAPI on its turf) + streaming-first async AI layer (fix `ai.py`'s fatal flaw) + abi3 wheels (5× fewer artifacts than Robyn) + graceful shutdown + worker supervisor + non-CLI `--dev`. Net: "FastAPI ergonomics, Rust speed, AI-native, ops-correct."

---

## 14. Source evidence index (for reimplementation cross-check)

| Subsystem | Primary files:lines |
|---|---|
| PyO3 module / binding | `src/lib.rs:37-68`, `Cargo.toml:12-17,56-57`, `pyproject.toml:151-152`, `.cargo/config.toml:1-11`, `robyn.pyi` (576) |
| App class / decorators | `robyn/__init__.py:77-969` (BaseRobyn 77-680, Robyn 683-745, SubRouter 748-890, ALLOW_CORS 893-944) |
| Route registration / resolver | `robyn/router.py:136-360` (closure 177-278), `robyn/_param_utils.py:1-193` |
| Scaling | `robyn/processpool.py:1-225` (run 15-68, init 71-130, spawn 149-225, loop 133-147), `robyn/argument_parser.py:103-129` |
| Hot reload | `robyn/reloader.py:1-164`, `robyn/cli.py:81-135`, `__init__.py:139-149` |
| Pydantic | `robyn/pydantic_support.py:1-226` |
| DI | `robyn/dependency_injection.py:1-72` |
| WebSocket | `robyn/ws.py:1-323` |
| AI (PoC) | `robyn/ai.py:1-309` |
| MCP (real AI surface) | `robyn/mcp.py:1-462`, `__init__.py:652-680` |
| Auth | `robyn/authentication.py:1-96` |
| OpenAPI | `robyn/openapi.py:1-499` |
| Packaging | `.github/workflows/release-CI.yml`, `Cargo.toml:43-57`, `pyproject.toml:1-40` |

> Cross-ref: `teardowns/ROBYN.md` for the Rust internals (actix System, tokio bridge, `executors/mod.rs` FFI, `matchit` router, `shared_socket.rs`, awaitables, KV/streaming). This file is the Python-side + bridge + scaling + AI + packaging companion.

---

## 15. Reimplementable skeletons for Lancelot (KNOWN-derived)

These distill the above into copy-start scaffolding. Not Robyn's verbatim code — the *extracted pattern* with Robyn's decisions applied and its mistakes fixed.

### 15.1 The bridge crate (mirrors `Cargo.toml`/`lib.rs`/`.pyi` decisions)

`Cargo.toml`:
```toml
[lib]
name = "lancelot"
crate-type = ["cdylib", "rlib"]
[dependencies]
pyo3 = { version = "0.27", features = ["extension-module", "py-clone", "abi3-py310"] }  # abi3 = 1 wheel/plat
pyo3-async-runtimes = { version = "0.27", features = ["tokio-runtime"] }
tokio = { version = "1", features = ["full"] }
# ... actix-web/matchit/socket2(all)/dashmap/orjson-equiv as Robyn Cargo.toml:16-41
[profile.release]
codegen-units = 1
lto = "fat"
panic = "abort"     # only ship with the worker supervisor (§3.5)
strip = true
opt-level = 3
```
`.cargo/config.toml`: copy Robyn's `.cargo/config.toml:1-11` verbatim (macOS `link-arg=-undefined`/`dynamic_lookup`).

`src/lib.rs` (`#[pymodule]`): name it `_lancelot` (private inner module — fixes Robyn's `robyn.robyn` leak):
```rust
#[pymodule]
fn _lancelot(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(get_version, m)?)?;
    m.add_class::<Server>()?; m.add_class::<FnInfo>()?; m.add_class::<SocketHeld>()?;
    m.add_class::<Headers>()?; m.add_class::<PyRequest>()?; m.add_class::<PyResponse>()?;
    // ... awaitables, WS channel, QueryParams, HttpMethod, MiddlewareType, Identity
    Ok(())
}
```
`pyproject.toml`: `[tool.maturin] module-name = "lancelot._lancelot"`; `lancelot/__init__.py` re-exports the curated public API (so users never see `_lancelot`). Stub: generate `_lancelot.pyi` in `build.rs`/CI via `pyo3-stub-gen` (never hand-write — Robyn's drifted, §1.3).

### 15.2 The `FnInfo` ABI (the one struct Rust knows)
```rust
#[pyclass] #[derive(Clone)]   // Clone needs pyo3 "py-clone"
pub struct FnInfo {
    #[pyo3(get)] pub handler: PyObject,   // the Python closure (already wrapped, §2.4)
    #[pyo3(get)] pub is_async: bool,      // computed once at registration
    #[pyo3(get)] pub n_params: usize,     // arity, computed once
    #[pyo3(get)] pub params: Py<PyDict>,  // name -> Parameter (frozen)
    #[pyo3(get)] pub deps: Py<PyDict>,    // filtered injected deps (frozen)
}
```
Python builds it once (Robyn `router.py:341-360`); Rust hot path: route→`fninfo.clone()`→build arg tuple len `n_params`→`handler.call1(args)`→extract. **No `inspect` after boot.**

### 15.3 The scaling driver (mirrors `processpool.py`, fixes added)
```python
def run_processes(host, port, routes, ..., processes, workers):
    sock = SocketHeld(host, port)                       # Rust SO_REUSEPORT listener
    pool = []
    if sys.platform == "win32" or processes == 1:
        spawn_process(routes, ..., sock, workers); return []   # in-process, zero fork
    for _ in range(processes):
        p = mp.Process(target=spawn_process, args=(routes, ..., sock.try_clone(), workers))
        p.start(); pool.append(p)
    install_graceful_term(pool)                         # FIX: SIGTERM→drain→SIGKILL (Robyn lacks)
    supervise(pool, respawn=spawn_process)              # FIX: re-spawn dead children (Robyn lacks)
    for p in pool: p.join()
    return pool

def cpu_budget():                                       # FIX: cgroup-aware (Robyn uses os.cpu_count)
    try:    return max(1, len(os.sched_getaffinity(0)))
    except: return os.cpu_count() or 1
# --fast: processes = cpu_budget()*2+1 ; workers = 2 ; log_level = WARNING  (Robyn argument_parser.py:103-109)
```
- `mp` MUST be `multiprocess` (dill), not stdlib `multiprocessing` — the route handlers are *closures* (`router.py:177` `wrapped_handler`) and stdlib `pickle` cannot serialize closures/lambdas; dill can. This is non-negotiable and is exactly why Robyn depends on `multiprocess` (`pyproject.toml:29`) and calls `mp.allow_connection_pickling()` (`__init__.py:724-726`) so the cloned socket fd survives the spawn-pickle.
- Linux: prefer `mp.set_start_method("fork")` so children inherit the already-built app/`Server` (Robyn leaves it default — slower on macOS spawn since it re-dills the whole route table per child).

### 15.4 Registration closure (mirrors `Router.add_route`)
```python
def add_route(method, endpoint, handler, ...):
    route_params  = parse_route_param_names(endpoint)         # _param_utils
    sig_params    = inspect.signature(handler).parameters     # ONCE, here
    pyd_params    = detect_pydantic_params(sig_params)         # lazy; {} if unused
    check_pydantic_installed_for_handler(handler, pyd_params)  # fail-loud at import
    def wrapped(*a, **kw):                                     # the 3-phase resolver (§4)
        req = first_request(a)
        if single_request_param(sig_params): return handler(*a, **kw)
        bound = phase1_type_identity(req, sig_params)
        bound |= phase15_pydantic(req, pyd_params)
        bound |= phase2_reserved_names(req, sig_params, deps)
        bound |= phase3_individual(req, sig_params - bound, route_params)  # coerce_value
        return handler(**bound)
    wrapped = async_wrap(wrapped) if iscoroutine(handler) else sync_wrap(wrapped)
    fn = FnInfo(wrapped, iscoroutine(handler), len(sig_params), sig_params, filter_deps(deps, sig_params))
    if (method, endpoint) in self._added: raise ValueError("duplicate route")  # fail loud
    self._added.add((method, endpoint)); self.routes.append(Route(method, endpoint, fn, ...))
```

### 15.5 AI-native layer reference (fixes every `ai.py` flaw, keeps the shape)
```python
class LLMProvider(ABC):
    @abstractmethod
    async def stream(self, messages: list[Message], **opts) -> AsyncIterator[str]: ...   # streaming-first
    @abstractmethod
    async def complete(self, messages: list[Message], **opts) -> Completion: ...

class MemoryStore(ABC):                                  # structured, not "Query: "/"Response: " strings
    @abstractmethod async def append(self, sid: str, msg: Message) -> None: ...
    @abstractmethod async def history(self, sid: str, k: int|None=None) -> list[Message]: ...
    @abstractmethod async def search(self, sid: str, query: str, k: int) -> list[Message]: ...  # real retrieval

# default store = Redis/pg (out-of-process) because --fast forks N procs; in-mem silently breaks (ai.py flaw #5)
# providers constructed once per process (in spawn_process, like Server), never per request
# multi-provider for real via a gateway: AnthropicProvider | OpenAIProvider | GeminiProvider behind LLMProvider
```
MCP layer = `mcp.py` reimplemented as a **core** primitive (not `experimental`): keep decorators + auto-schema + URI templates + JSON-RPC envelope + lazy `@property` mount (`__init__.py:652-680`); add (1) MCP-over-SSE transport (reuse `SSEResponse`), (2) Pydantic-model tool args → rich JSON-schema (reuse `get_pydantic_openapi_schema`), (3) `@app.mcp(auth_required=True)` honoring the auth middleware, (4) negotiated `protocolVersion`, (5) structured/binary content results.

---

## 16. Why this set of decisions makes Lancelot "faster FastAPI for startups + AI" (synthesis)

1. **Speed comes from *when*, not *what*.** Robyn isn't fast because Rust parses HTTP (that helps); it's fast because *all Python reflection is amortized to registration* (`router.py:151` one `inspect.signature`, never per request) and the Rust↔Python ABI is one opaque `FnInfo`. FastAPI re-walks a dependency graph per request. Lancelot inherits Robyn's timing model and additionally resolves even opt-in DI graphs once.
2. **The fork model is the GIL answer.** `processes` × independent interpreters via `multiprocess`(dill)+`SO_REUSEPORT` (`processpool.py:107-128`) is simpler and more robust than free-threading/no-GIL builds today, and `processes==1` keeps the dev path zero-overhead. Lancelot copies it and adds the two things Robyn forgot: graceful drain + supervisor (the difference between "toy" and "ops-correct" for a startup running it in prod).
3. **AI-native = MCP as a first-class primitive, not a wrapper.** Robyn's `mcp.py` proves a web framework can *be* an MCP server with ~460 dependency-free lines and three decorators. That is the differentiating primitive for the "+ AI" half of Lancelot's pitch — productionize it (streaming, auth, rich schemas) and lead with it. `ai.py` shows the *interface shape* (provider/memory/agent ABCs + factories) but its implementation is a disqualifying anti-pattern (sync LLM in async, in-proc memory under a fork model) — Lancelot takes the shape, rewrites the substance streaming-first and out-of-process.
4. **Shipping is a feature for startups.** Thin deps (`pyproject.toml:27-35`), optional heavy extras, prebuilt wheels = `pip install lancelot` works with no toolchain, small images, fast CI. Lancelot improves Robyn's packaging concretely with abi3 (one wheel per platform, no per-CPython churn — ~5× fewer artifacts and zero release needed when CPython 3.15 ships) + an explicit sdist escape hatch + auto-generated stubs that can't drift.

The net thesis: **Lancelot = Robyn's registration-time-reflection + fork-scaling speed model, plus Robyn's MCP primitive made production-grade and streaming-first, plus an opt-in resolve-once DI that beats FastAPI's per-request graph walk, shipped as abi3 wheels with graceful ops.** Every one of those is traceable to a specific Robyn file:line above — the gaps Robyn left (graceful shutdown, supervisor, async AI, non-CLI dev, abi3, stub-gen) are precisely Lancelot's wedge.
