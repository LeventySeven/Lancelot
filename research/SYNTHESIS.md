# Lancelot — Synthesis: Which Framework Exceeds At What, and the Resulting Architecture

> Built from 9 source-level teardowns (8,645 lines total) in `research/{fastapi,robyn,blazeio}/`.
> Every claim here traces to one of those files; open them for the `path:line` evidence.
> Labels: **STEAL** = adopt into Lancelot. **AVOID** = anti-pattern found in the wild. **DESIGN** = Lancelot-specific synthesis (not in any of the three).
>
> Scope reminder: the framework *design itself* is deliberately deferred ("we'll think of Rust+Python later"). This document is the **evidence-grounded blueprint** the design brainstorm will start from — not the final design.

---

## 0. The three frameworks in one line each

| | What it actually is | Its one superpower | Its fatal weakness for our goal |
|---|---|---|---|
| **FastAPI** (0.136-class fork, on Kludex Starlette) | A *registration-time compiler* for typed HTTP handlers on top of Pydantic-core (Rust) + Starlette ASGI | **Ergonomics**: DI graph + validation + OpenAPI from one type annotation, compiled once at import | Per-request Python tax: O(N) regex routing, 40-thread sync cliff, ≥10 framework allocs/req before user code |
| **Robyn** (0.85.0, PyO3/actix) | The architecture we want, already shipping: Rust HTTP core + opaque-callable Python ABI + multiprocess GIL escape | **The GIL choreography**: entire non-handler request lifecycle is GIL-free Rust; GIL held µs ×3/req, never across `await` | Eager total request marshalling (~30–66 allocs/req even if unread); per-token GIL churn on streaming |
| **Blazeio** (`anonyxbiz`) | A from-scratch pure-asyncio HTTP/1.1 server obsessed with allocation count | **Micro-optimization**: zero-copy buffered receive, pull-by-default backpressure, pooled-future events, no per-token Future alloc | Pure Python ceiling: O(n·k) header re-scan (slow-loris), exception-as-response, HTTP/1.1-only, weak/dead static |

**The thesis in one sentence:** *Lancelot = Robyn's Rust-core/GIL model + FastAPI's registration-time compiler ergonomics + Blazeio's allocation discipline and streaming backpressure — with the three things none of them got right (lazy request marshalling, native async-generator streaming pump, an AI-native layer that isn't a PoC) built correctly from day one.*

---

## 1. Head-to-head: which framework exceeds at what

Winner = "if you could only copy one framework's implementation of this subsystem, copy this one." `→` = the file with the evidence.

| Subsystem | FastAPI | Robyn | Blazeio | **Winner & why** |
|---|---|---|---|---|
| **HTTP parsing / accept loop** | delegates to uvicorn/httptools | **Rust actix (zero-GIL, HTTP/2)** | pure-Python `bytes.find` (fast but O(n·k), HTTP/1.1 only) | **Robyn** — only one that parses off-GIL; Blazeio's parser is the *spec* for Lancelot's Rust parser `→ blazeio/01`, `robyn/01` |
| **Routing / match** | O(N) regex scan + 2nd scan on 404 (AVOID) | **`matchit` radix trie per method + const-router tier** | O(1) dict exact + O(k) wildcard suffix-trim | **Robyn** (trie) + **Blazeio** (O(1) exact-match dict). FastAPI loses outright `→ fastapi/01 §1.2`, `robyn/02 §0` |
| **Const/static-response tier** | none | **pre-baked `CachedResponse`, zero-GIL/zero-Python, ~10–50×** | in-memory static cache (but dead code) | **Robyn** — the single best idea in the corpus. Make it a first-class Lancelot tier `→ robyn/02 §0` |
| **Request marshalling cost** | ~10–15 allocs + `Request`+exit stacks | eager **~30–66 allocs even if handler reads nothing** (AVOID) | **lazy: nothing parsed until handler touches it** | **Blazeio** (laziness) — Robyn shows the cost of getting this wrong `→ robyn/02 §3.4`, `blazeio/02 §1` |
| **Dependency injection** | **two-phase compiler: reflect→immutable `Dependant` DAG once; per-req recursive solve** | type-identity 3-phase resolver, no `Depends()` | none (manual) | **FastAPI** — the DI compiler is *the* ergonomics moat; Robyn shows the cheaper-but-weaker alternative `→ fastapi/02` |
| **Validation** | **per-field `TypeAdapter` compiled once → pydantic-core Rust `validate_python`** | opt-in lazy Pydantic `model_validate_json` (single-pass) | none | **Robyn's single-pass `validate_json(bytes)`** beats FastAPI's double-parse (`json.loads`→`validate_python`) — a free Lancelot win `→ fastapi/03 §0.1`, `robyn/03 §5` |
| **Response serialization** | fast path `dump_json()` (1 Rust pass) **but** `jsonable_encoder` recursive Python walk still on untyped/SSE/OpenAPI paths (AVOID) | in-Rust cached `orjson.dumps`, ordered fast-path extraction | hand-framed bytes, never materialized | **FastAPI's `dump_json` fast path** — make it the *only* typed path; kill `jsonable_encoder` from the hot path `→ fastapi/03 §0.2-0.3` |
| **Streaming write path (AI hot path)** | native SSE/JSONL, decoupled producer + bounded chan + 15s keepalive timer, PEP-789 correct | **GIL + spawn_blocking per token (AVOID — the one thing Robyn must not be copied on)** | **boolean backpressure gate: no Future per token; prepare-time writer rebind; chunked/SSE hand-framing** | **Blazeio** (per-token cost) + **FastAPI** (the producer/keepalive *architecture*). Robyn loses `→ blazeio/02 §0`, `fastapi/03 §0.7-0.9`, `robyn/01 §10` |
| **Receive backpressure** | Starlette automatic transport pause | actix-managed | **pause-by-default, demand-pumped, `prepend()` O(1) pushback** | **Blazeio** — pull-driven is the correct model for slow LLM clients `→ blazeio/01 §0.2,0.6` |
| **Send backpressure** | `StreamWriter.drain()` (Future/token) | actix | **2-event flip-flop, sync early-return when client keeps up** | **Blazeio** — zero Future on the fast path `→ blazeio/02 §0.3` |
| **Concurrency / scaling** | 40-thread `CapacityLimiter` sync cliff (AVOID) | **N processes (`SO_REUSEPORT` + `try_clone`) × actix workers; GIL escaped by process replication** | single-process asyncio | **Robyn** — multiprocess is the *only* real Python CPU scaling `→ robyn/01 §0.2`, `robyn/03 §3` |
| **GIL management** | n/a (uvicorn) | **`with_gil` µs ×3/req, dropped across `.await`; sync→GIL-detaching blocking pool** | n/a (single GIL) | **Robyn** — this discipline *is* why a Rust core beats uvicorn `→ robyn/01 §0.3-0.5` |
| **Sync handler handling** | anyio 40-thread pool (queues at 41) | Granian-derived `BlockingRunner` (GIL-detaching) — but **not wired into HTTP path (bug)** | n/a | **Robyn's design** (port it, *bound* the queue, actually wire it) `→ robyn/01 §0.5`, `robyn/02 (sync-bug)` |
| **Custom async primitives** | none | 5 `#[pyclass]` awaitables, "38–55% faster than pyo3-asyncio" | **`reFuture` self-recycling Future; `SharpEvent` pooled-future event** | **Both** — Robyn's Rust awaitables + Blazeio's `SharpEvent` (reject `reTask`: loses contextvars) `→ robyn/01 §0.6`, `blazeio/03 §0.1` |
| **Outbound HTTP client (AI calls!)** | none (bring httpx) | none | **per-host pool, stale-detect-before-handout, least-in-flight, POST-never-retried, zero-copy streaming** | **Blazeio** — the AI outbound hot path; needs HTTP/2 + verify=True added `→ blazeio/03 §0.2,4.8` |
| **OpenAPI / schema** | **free byproduct of the same type compile; `$ref` dedup; input/output split** | manual `openapi.py` | none | **FastAPI** — schema from the same Rust registry, once at boot `→ fastapi/03 §5-6` |
| **AI-native layer** | none (userland) | **`mcp.py` = full MCP JSON-RPC 2.0 server, auto schema-from-signature** (`ai.py` is a weak PoC — AVOID) | none | **Robyn's `mcp.py`** — copy wholesale as the shape of Lancelot's AI layer `→ robyn/03 §0.7` |
| **Packaging / shipping** | pure-Python wheel (trivial) | maturin, per-(platform×CPython) wheel matrix (~20-25/release) | pure-Python + transparent C-ext fallback (logs slowdown) | **DESIGN**: maturin **abi3 wheels** (1/platform, not 1/CPython) + Blazeio's transparent-fallback telemetry idea `→ robyn/03 §0.8`, `blazeio/03 §0.3` |
| **Hot reload / DX** | uvicorn `--reload` | watchdog + subprocess respawn (CLI-only — AVOID the CLI lock-in) | none | **Robyn's mechanism**, exposed in-process not CLI-only `→ robyn/03 §0.4` |

**Reading the table:** Robyn wins the *core* (parsing, routing, GIL, scaling, const-tier, AI layer). FastAPI wins the *ergonomics* (DI compiler, validation-from-types, OpenAPI). Blazeio wins the *micro-layer* (allocation discipline, backpressure, streaming per-token cost, the async client). Lancelot is the union of the three winners, minus each one's fatal weakness.

---

## 2. The consolidated steal-list (ranked, with provenance)

**Tier S — non-negotiable, these define Lancelot:**

1. **Registration-time compiler, request-time execute-only.** FastAPI freezes the per-route request→response path as `route.app` in `APIRoute.__init__` (`fastapi/01 §0.1`). Lancelot: reflect signature → DI DAG → validators → serializer → OpenAPI **once at import, in/towards Rust**; the request path never introspects. The "done once vs per-request" split is the entire optimization thesis (`fastapi/02 §9.3`).
2. **Robyn's GIL choreography verbatim** (`robyn/01 §0.3-0.5`): non-handler lifecycle 100% GIL-free Rust; `with_gil` only to (a) build args (b) call handler/make coroutine (c) marshal return; **drop GIL across the entire `.await`**; **hard invariant: no `with_gil` scope contains `.await`**. Sync handlers → dedicated GIL-detaching blocking pool (bounded + load-shed; Robyn's is unbounded = latent OOM).
3. **Multiprocess scaling**: N processes (one GIL each) sharing one `SO_REUSEPORT`+`SO_REUSEADDR`+`TCP_NODELAY` socket, `listen(16384)`, fd cloned per fork (`robyn/01 §0.2`, `robyn/03 §3`). Threads scale only Rust I/O; processes are the only thing that scales Python.
4. **Lazy request proxy** (fixes Robyn's worst flaw): a pyclass holding the raw Rust request + un-consumed payload; headers/query/body/json materialize **on attribute access**. Handler reading one header pays one lookup, not 30–66 allocs (`robyn/02 §0,§9`). Combine with Blazeio's lazy-parse-on-first-iteration (`blazeio/01 §0.5`).
5. **Const/static-response tier** (Robyn's best single idea, `robyn/02 §0`): routes whose response is computed once at boot are served from a Rust map — zero GIL, zero Python, zero request alloc, ~10–50×. Improve it: allow with pure-Rust middleware chains; fix Robyn's trailing-slash normalization bug.

**Tier A — major wins:**

6. **One-pass typed serialization only.** pydantic-core `dump_json()` → bytes in one Rust pass; **delete `jsonable_encoder` from the hot path** (FastAPI keeps it for untyped/OpenAPI/SSE — Lancelot needs a Rust path for those too) (`fastapi/03 §0.2-0.3`).
7. **Single-pass input validation**: `validate_json(bytes)` directly, *not* FastAPI's `request.json()`→`json.loads`→`validate_python` double-parse (`fastapi/03 §0` residual tax; `robyn/03 §5`).
8. **Blazeio's streaming substrate for the AI hot path** (`blazeio/02 §0`, `blazeio/01 §0`): `BufferedProtocol` + one preallocated `bytearray(102400)` + cached `memoryview` (one copy/recv, zero steady-state alloc); pause-reading-by-default pull model; **boolean send-backpressure gate (no Future per token)**; prepare-time writer rebind (no per-chunk branch); chunked/SSE hand-framing (`b"data: %b\n\n"`, OpenAI-compatible). End-to-end backpressure: slow client → stalled handler → stalled upstream-LLM read → bounded server RAM regardless of completion length.
9. **FastAPI's SSE *architecture*** (`fastapi/03 §0.7-0.9`, `fastapi/01 §0.7`): decoupled producer task + bounded channel + **separate keepalive timer that wraps the channel receive, never the generator's `__anext__`** (PEP-789: wrapping the generator kills the LLM stream via `GeneratorExit`). Forced headers `Cache-Control: no-cache` + `X-Accel-Buffering: no`. `anyio.sleep(0)` cancellation checkpoints. Implement the bytes+timer in Rust (timer-wheel keepalive, bounded SPSC ring).
10. **Robyn's `FunctionInfo` opaque-callable ABI** (`robyn/03 §0.2`): the entire Python→Rust contract is one dataclass (handler + is_async + arity + params + deps); all reflection amortized to registration; Rust never introspects signatures on the hot path. This is the concrete mechanism for steal #1.
11. **`mcp.py` wholesale as the AI-native layer shape** (`robyn/03 §0.7`): MCP JSON-RPC 2.0 server, decorator registry, auto-JSON-schema-from-signature, URI-template resources. Reject `ai.py` (sync OpenAI client in `async def` = blocks the loop).
12. **Blazeio's async client design for outbound LLM calls** (`blazeio/03 §0.2,4.8`): per-(scheme,host,port) pool, stale-detect-before-handout, idle-reap below provider timeout, least-in-flight selection, **POST never auto-retried** (no duplicate completions/billing), zero-copy streaming recv. Add: **HTTP/2 default**, **verify=True default** (Blazeio ships `CERT_NONE` — a security defect), DNS caching, in-band `asyncio.timeout` (not Blazeio's racy watchdog thread).

**Tier B — cheap, high-leverage:**

13. `SharpEvent` pooled-future event (drop-in over `asyncio.Event`, no per-wake alloc) — `blazeio/01 §0.3`. Reject `reTask` (loses contextvars/structured cancellation/`all_tasks()` visibility) — `blazeio/03 §0.1`.
14. Per-request `contextvars.Context` threaded through middleware (tenant isolation; Robyn fixed issue #1380 this way) — `robyn/01 §0.7`. Require Python ≥3.11.
15. Robyn's 5 custom Rust awaitables (poll model, no Python `Future` alloc) — `robyn/01 §0.6`.
16. Transparent native-accel with measured-slowdown telemetry: pure-Python fallback always correct, log the exact factor when degraded — `blazeio/03 §0.3`.
17. FastAPI ergonomic/correctness details that the ecosystem depends on: 422 `{"detail":[{type,loc,msg,input,ctx}]}` wire shape, `include_router` flatten-at-registration, strict-content-type CSRF default, `Default()` sentinel — `fastapi/01 §0`, `fastapi/03 §0`.

---

## 3. Anti-patterns — what NOT to copy (per framework)

**From FastAPI — AVOID:**
- O(N) per-route regex `.match()` + a *second* full scan on 404 when `redirect_slashes=True` (`fastapi/01 §0.2`). → radix trie.
- `CapacityLimiter(40)` silent sync cliff; the 41st blocking handler queues with no backpressure; `iterate_in_threadpool` does one thread hop *per stream chunk* (`fastapi/01 §0.3`). → explicit sized executor + Rust admission control + 503 `Retry-After`.
- `BaseHTTPMiddleware` / `@app.middleware("http")`: task group + 2 memory streams + cached request **per request** (`fastapi/01 §0.5`). → pure-Rust/ASGI middleware only, never a `call_next` dispatch model.
- `jsonable_encoder` on the hot path (recursive isinstance ladder, 2–3× memory) (`fastapi/03 §0.3`).
- `BackgroundTasks` run **sequentially inside the request coroutine after body send** — blocks connection release; "return fast, work later" is silently broken (`fastapi/01 bonus`). → Rust-owned detached pool.
- Double-validate on output + `deepcopy(default)` per missing optional + full `get_dependant()` rebuild per request when dependency overrides active (`fastapi/02 §9`).

**From Robyn — AVOID:**
- Eager total request marshalling (~30–66 allocs/req even if unread; body fully buffered, no streaming intake) (`robyn/02 §0,§3.4`). → lazy proxy (steal #4).
- Per-token streaming: `gen.__next__()` in `spawn_blocking` + `with_gil` **per chunk**, sync-generator-only, `String` copy (`robyn/01 §10`, `robyn/02`). → native async-gen pump, chunk coalescing ~1 GIL/10ms, zero-copy bytes.
- Sync-handler thread-pool offload **exists but is not wired into the HTTP path** — a blocking `def` handler pins the worker and holds the GIL (`robyn/02 sync-bug`). Wire it; bound it.
- No graceful shutdown (`process.kill()` drops in-flight), no crashed-worker supervisor (fatal with `panic=abort`), `os.cpu_count()` ignores cgroup quotas, `input()` on port conflict hangs containers, CLI-only `--dev` (`robyn/03 §0`).
- `ai.py`: sync OpenAI client inside `async def`, stringly-typed memory, process-local store useless under its own `--fast` fork default (`robyn/03 §0.6`).
- Hand-written `.pyi` drifted from the real ABI (`robyn/03 §0`). → auto-generate.

**From Blazeio — AVOID:**
- O(n·k) header scan: `while b"\r\n\r\n" in buff` re-scans from byte 0 every recv; O(H²) header-line stripping; per-chunk chunked-decode recopy — a real slow-loris DoS surface, no header-receipt deadline (`blazeio/01 §0 ceiling`). → single-pass resumable Rust parser with a deadline. **This is the core justification for Lancelot's Rust core.**
- Exception-as-response as the *primary* return mechanism (`raise Abort(...)`/`raise Eof()` for HEAD): non-local, untestable, slower when raised, source of the 3-casing header hand-checks (`blazeio/02 §7`). → steal the central `try/except/finally` funnel + `finally`-guaranteed cleanup; reject success-via-`raise`.
- Static subsystem: weak name+size ETag, 1 KiB chunks, off-by-one range length, no `sendfile(2)`, two non-importable dead modules (`blazeio/02 §0.8,8`). → Starlette-class static + real `sendfile`; only borrow Blazeio's conditional-request logic + path-traversal guard.
- The multiplexer (over-engineered; window=1 stop-and-wait throughput trap) and `SharpEvent` for *multi-waiter fan-out* (`_waiters.clear()` drops broadcast wakeups) (`blazeio/01 §2.2,§7`).
- HTTP/1.1-only client, `ssl.CERT_NONE` default, DNS-resolve-every-connect, racy external-thread request timeout, C `char output[MAX_URL_LEN]` URL-overflow bug (`blazeio/03 §4.8`). → don't port the C verbatim; HTTP/2 + verify=True + DNS cache + in-band timeout.

---

## 4. The resulting Lancelot architecture (evidence-grounded blueprint — DESIGN)

> This is the synthesis, not the final design. Decisions marked **(open)** go to the brainstorm.

### 4.1 Core stack
- **`pyo3 0.27`** (`extension-module`, `py-clone`, `abi3-py311`) + **`pyo3-async-runtimes 0.27`** (`tokio-runtime`) + **`tokio 1.40+ "full"`**.
- **HTTP engine: `hyper 1.x` + `tower`** (no actor system, no `actix::System` global) rather than Robyn's actix-web — actix is a proven shortcut, hyper is the right greenfield choice **(open: hyper vs actix is a real fork-in-the-road; revisit with a spike)**.
- Build profile verbatim from Robyn: `lto="fat"`, `codegen-units=1`, `panic="abort"`, `opt-level=3`, `strip=true` — **plus** a `catch_unwind` at the FFI seam + a respawn-on-crash supervisor (Robyn lacks both; fatal with `panic=abort`).
- Require **CPython ≥ 3.11** (drop Robyn's degraded 3.10 contextvars fallback).

### 4.2 Process / concurrency model
- N OS processes via `SO_REUSEPORT`+`SO_REUSEADDR`+`TCP_NODELAY`, `listen(16384)`, fd cloned per fork (kernel load-balances accept). cgroup-aware CPU count (not `os.cpu_count()`).
- Per process: tokio multi-thread runtime for Rust I/O + a **bounded, load-shedding** GIL-detaching blocking pool for sync `def` handlers (Granian/Robyn `BlockingRunner` shape, actually wired into the HTTP path).
- Graceful shutdown (drain in-flight) + crashed-worker respawn supervisor.

### 4.3 Request lifecycle (the hot path)
```
accept/TLS/HTTP parse (Rust, zero GIL, single-pass resumable parser w/ header-receipt deadline)
  → route match (Rust radix trie per method; const-tier short-circuit BEFORE any Python)
  → [const route?] → pre-baked CachedResponse, return. zero GIL, zero Python, zero alloc.
  → build LazyRequest proxy (Rust request + unconsumed payload; nothing materialized yet)
  → with_gil: bind args from compiled plan, call handler / make coroutine   ── µs
  → DROP GIL ──────────────── await handler (GIL-free; LLM I/O here) ────────
  → with_gil: pydantic-core validate_json/dump_json (1 Rust pass), marshal    ── µs
  → stream/serialize response (Rust writer, boolean backpressure, no Future/token)
```
Per-request framework allocation target: **~1–3** (vs Robyn ~30–66, FastAPI ~10–15).

### 4.4 Ergonomics layer (the FastAPI moat, made cheap)
- **Two-phase DI compiler**: registration reflects signature → immutable DAG → flat topological execution plan + dense cache slots (interned-int keys, not tuple-hash); per-request executes the plan in Rust, crossing into Python only for the N user callables. Keep FastAPI's exact "code-shape→once / request-data→per-request" split (`fastapi/02 §9.3`). Target 3–10× DI-overhead reduction.
- Per-field validator/serializer/OpenAPI-schema all compiled from **one** type representation at import (FastAPI's model, moved Rust-ward).
- Preserve byte-for-byte: 422 wire shape, `Annotated`/`Depends`/`Security` surface, yield-deps + exit-stack teardown ordering (the PEP-789 streaming fix — DB/LLM client stays open through the SSE stream, closes after).

### 4.5 Streaming (the AI headline workload)
- Transport substrate from Blazeio verbatim (BufferedProtocol receive, pull-by-default, pooled event, prepare-time writer rebind, boolean backpressure gate).
- Response model: **typed returned `SSEResponse`/`ChunkedResponse` objects** (not exception-as-response).
- Rust SSE/JSONL codec + timer-wheel keepalive + bounded SPSC ring (size-1 default = lowest latency + backpressure onto the model). Native **async-generator** pump (not Robyn's sync-gen + per-token spawn_blocking).
- Coalescing: time+size trigger (~25 ms / 2 KiB), **not** Blazeio's 30 KiB size-only (holds thousands of tokens).
- Client-disconnect → cancel → abort upstream LLM call (don't keep paying for tokens nobody reads).

### 4.6 AI-native layer
- `mcp.py` shape wholesale: MCP JSON-RPC 2.0 server, decorator registry, auto schema-from-signature, URI-template resources, lazy `@property` mount.
- A *proper* async LLM client (not Robyn's `ai.py`): the Blazeio pool design + HTTP/2 + verify=True + DNS cache + in-band timeout + per-host circuit breaker + concurrency semaphore + orjson bytes-in/bytes-out.

### 4.7 Packaging
- maturin + **abi3 wheels** (one per platform for CPython 3.11+, not one per minor — ~5× fewer artifacts, zero churn on new CPython): macOS universal2 + manylinux_2_28 (x86_64/aarch64) + musllinux (Alpine/startup Dockerfiles) + win-x64. `maturin sdist` as the only fallback. Auto-generated `.pyi`. Single version source = `env!("CARGO_PKG_VERSION")`.
- **No pure-Python fallback** (a slow imposter fragments the API) — but adopt Blazeio's transparent measured-slowdown telemetry idea for any *optional* native accel.

### 4.8 Open questions for the brainstorm
1. hyper+tower vs actix-web for the core (spike both; measure const-tier + streaming).
2. How far does the DI/validation graph actually move into Rust vs codegen'd Python vs FFI-to-pydantic-core? (the per-request Python frame is the residual tax).
3. ASGI compatibility surface — adopt ASGI as the middleware contract (ecosystem) or a Rust-native middleware ABI (speed)? Probably both tiers.
4. Is the const-tier worth the trailing-slash/middleware-interaction complexity, or a v2 feature?
5. Streaming: SPSC ring size, keepalive interval, coalescing thresholds — set by benchmark, not guess.

---

## 5. Index of the research corpus

| File | Lines | Owns |
|---|---|---|
| `fastapi/01-routing-and-request-lifecycle.md` | 828 | request lifecycle, route compile, middleware, BackgroundTasks, scaling cliffs |
| `fastapi/02-dependency-injection.md` | 1325 | the DI two-phase compiler, `solve_dependencies`, exit stacks, the once/per-request split |
| `fastapi/03-validation-serialization-openapi.md` | 1212 | pydantic-core seam, `jsonable_encoder`, `dump_json` fast path, OpenAPI, SSE framing |
| `robyn/01-rust-core-runtime-and-gil.md` | 955 | crate stack, process model, GIL choreography, blocking pool, custom awaitables, streaming weakness |
| `robyn/02-rust-routing-types-websockets.md` | 867 | const-router, `matchit` routing, FFI marshalling cost, websockets, the lazy-proxy fix |
| `robyn/03-python-api-pyo3-bridge.md` | 860 | `FunctionInfo` ABI, multiprocess scaling, param resolver, `mcp.py`/`ai.py`, packaging |
| `blazeio/01-protocol-server-core.md` | 866 | BufferedProtocol, the `bytes.find` parser, pull model, backpressure, the pure-Python ceiling |
| `blazeio/02-request-response-streaming-static.md` | 906 | lazy request, streaming write/drain (AI hot path), exception-control-flow verdict, static |
| `blazeio/03-app-routing-client-utils.md` | 826 | App/dispatch, `reFuture`/`SharpEvent`, the async client, C-ext fallback strategy |
| **total** | **8,645** | |

*Generated 2026-05-18 from a 9-agent parallel source-level RE pass. `Lancelot/` is gitignored.*
