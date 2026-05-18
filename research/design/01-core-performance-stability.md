# Lancelot Design Decision 01 — Core Performance & Stability

> Decision-grade design research resolving SYNTHESIS §4.8 Q1 (hyper vs actix) and the free-threaded-Python (nogil) architecture question, plus the PyO3 pattern, async-runtime, and stability-engineering decisions that follow from them.
>
> **Method:** landscape + decision research (2026 facts via WebSearch/WebFetch) grounded against the 9 source-level teardowns in `research/{fastapi,robyn,blazeio}/`. Every external claim carries a source URL. Every framework claim cross-refs the teardown by `file §section` or `file:line`. Labels: **KNOWN** (sourced — from a teardown or a cited URL), **INFERRED** (deduced from sourced facts + first principles), **RECOMMENDATION** (Lancelot decision).
>
> Inputs read: `SYNTHESIS.md`, `robyn/01-rust-core-runtime-and-gil.md` (§0,§1,§2,§3,§7,§9,§10), `blazeio/01-protocol-server-core.md` (§4.2,§8), `fastapi/01-routing-and-request-lifecycle.md` (§5).

---

## Executive Summary — the recommendation up front (12 lines)

1. **Rust HTTP stack: `hyper 1.x` + `hyper-util` + `http-body-util` + `tower 0.5`, on a `tokio 1.x` multi-thread runtime.** NOT actix-web. hyper 1.0 carries a 3-year semver-stability promise (KNOWN — seanmonstar); actix's actor `System`/`Arbiter` model is dead weight for a framework that isn't actor-based and is the single biggest source of Robyn's runtime baroqueness (`robyn/01 §3.4`).
2. **Do NOT take a thread-per-core io_uring runtime (glommio/monoio) or may_minihttp.** glommio is effectively unmaintained (DataDog moved on); monoio is feature-thin and behind on ecosystem; both lose the tower middleware ecosystem and the PyO3 integration story. Thread-per-core's win is erased by the GIL anyway (INFERRED).
3. **PyO3 0.27 is the floor; design the FFI seam to be PyO3-0.28-clean now.** 0.28 renames `allow_threads`→`detach`, removes `GILProtected`, and is the version with first-class free-threaded support (KNOWN — pyo3.rs). Pin the minor; budget a migration per pyo3 minor (Robyn's git history proves the churn — `robyn/01 §10` pitfall 5).
4. **The nogil verdict: dual-build, multiprocess-first, free-threaded-ready — NOT nogil-first.** Free-threaded CPython is *supported* (not experimental) as of 3.14 / PEP 779, but ships a 5–10% single-thread penalty + 15–20% memory overhead, ~50% of top packages still lack ft wheels (Feb 2026), and **abi3 is fundamentally incompatible with the free-threaded ABI** (KNOWN — docs.python.org, pyo3.rs, PEP 803). Lancelot's multiprocess model is NOT obsolete — it remains the safe default for years.
5. **Abi3 stance: ship abi3 wheels for the GIL build (1 wheel/platform), AND version-specific free-threaded wheels (`cp313t`/`cp314t`).** This is forced — there is no abi3 for free-threaded until PEP 803's `abi3t` lands in CPython 3.15+ (KNOWN — PEP 803). Plan for `abi3t` as the long-term unifier.
6. **Runtime model: N OS processes (`SO_REUSEPORT`) × one tokio multi-thread runtime per process** (Robyn's process model verbatim — `robyn/01 §1`), uvloop on the Python side, GIL dropped across every `.await` (Robyn's discipline verbatim — `robyn/01 §2.4`). When run under free-threaded CPython, collapse to 1 process × N threads with the same code path (Granian's v2 model — KNOWN).
7. **Stability is where all three teardown targets fail and Lancelot must not:** no graceful drain (Robyn), no crashed-worker respawn (Robyn, fatal with `panic=abort`), no header-receipt deadline / slow-loris defense (Blazeio + Robyn's `client_request_timeout(0)`), no `catch_unwind` at the FFI seam (Robyn), unbounded sync queue (Robyn), the silent 40-thread sync cliff (FastAPI). The §5.7 checklist closes every one.
8. **The nogil verdict in one sentence:** *design free-threaded-clean from day one (declare `gil_used=false`, no `GILProtected`, audit all `static`/`OnceCell` for thread-safety, test under `cp314t`), ship multiprocess as the default, offer free-threaded as an opt-in build — and revisit making it default when Lancelot's own dep closure (uvloop or its replacement, pydantic-core, orjson) all ship ft wheels and the single-thread penalty is under 5%.*

---

## 1. Rust HTTP stack selection (resolves SYNTHESIS §4.8 Q1)

The question SYNTHESIS leaves open (`SYNTHESIS.md:159`, `robyn/01 §0` line 13): *hyper 1.x + tower vs actix-web vs axum vs ntex vs may_minihttp vs glommio/monoio (thread-per-core), for a PyO3-bound Python framework.* This section resolves it with a decisive recommendation and the evidence behind it.

### 1.0 The decision criteria, weighted for *this* product

Lancelot is not a Rust web app — it is a **Rust HTTP core that exists to feed a Python interpreter through a PyO3 FFI seam**. That changes the weighting versus a pure-Rust-service framework comparison:

| Criterion | Weight | Why for Lancelot specifically |
|---|---|---|
| API stability / maintenance horizon | **Critical** | Lancelot is a framework others build on for years; a core HTTP-stack breaking change is a Lancelot breaking change. pyo3 churn (`robyn/01 §10` pitfall 5) is already one forced-migration axis — don't add a second. |
| Direct runtime control | **Critical** | Lancelot must own the tokio runtime to enforce the GIL-drop-across-await invariant (`robyn/01 §2.4`), wire a *bounded* GIL-aware blocking pool (`robyn/01 §5`), and bind the `SO_REUSEPORT` fd (`robyn/01 §1.2`). A framework that owns the runtime fights you. |
| HTTP/2 + HTTP/1.1 + WebSocket + path to HTTP/3 | High | Many concurrent SSE/LLM streams multiplex far better over h2 (`robyn/01 §7.3.6`); h3 is the credible next step. |
| tower middleware ecosystem | High | SYNTHESIS §4.8 Q3 wants ASGI-or-Rust-native middleware tiers; tower is the de-facto Rust middleware contract and axum/hyper-util/tonic all speak it. |
| Raw throughput / tail latency | **Medium (not high)** | INFERRED + KNOWN: the GIL caps Python-bound throughput regardless of Rust stack (`robyn/01 §8` "Amdahl"). The Rust stack delta only shows on const-route / high-RPS-short-call paths. A 10–15% Rust-framework RPS delta is dwarfed by an 800 ms LLM call (`robyn/01 §7.1`). |
| Build time / binary size | Low | One-time cost; `lto=fat`+`codegen-units=1` dominates either way (`robyn/01 §0`). |
| FFI ergonomics | Medium | The stack must not impose its own object model between the socket and the PyO3 boundary. |

The decisive reframing: **for a GIL-bound Python framework, raw Rust throughput is the *least* important axis** (it is capped by Amdahl on Python — `robyn/01 §8`, KNOWN). API stability and direct runtime control are the most important. This inverts the usual "actix is fastest, pick actix" reasoning.

### 1.1 hyper 1.x + tower — the recommendation

**KNOWN (seanmonstar, https://seanmonstar.com/blog/hyper-v1/):** hyper 1.0 makes an explicit stability commitment: *"Major versions, like 1.0, are stable for at least 3 years"*, MSRV is kept *"at least 6 months old"*, and the formal stability promise is documented at hyper.rs/contrib/vision/#stability-promise. hyper 1.x deliberately scoped *out* the opinionated high-level pieces — they live in `hyper-util`, `tower`/`tower-http`, `reqwest`, `axum`. hyper provides only HTTP/1 + HTTP/2 server/client over *your* IO and *your* runtime: *"allowing you to bring your own IO and runtime."*

That "bring your own runtime" property is exactly what Lancelot needs and what actix-web denies. The teardown already reached this conclusion from the source side: *"For a new framework, hyper 1.x + tower (or axum's internals) is leaner and gives you direct control of the runtime"* (`robyn/01 §0` crate table, line 82, KNOWN); *"Robyn's topology — actix arbiters for HTTP + a side current-thread tokio + a separate blocking pool + the Python asyncio loop on the main thread — is four schedulers coordinating through the GIL and pyo3-async-runtimes::TaskLocals. It works but it is baroque, and the actix System global + actor model is dead weight for a framework that doesn't otherwise use actors"* (`robyn/01 §3.4`, line 422, KNOWN).

**The stack (RECOMMENDATION), pinned:**

```toml
hyper          = { version = "1", features = ["http1", "http2", "server"] }
hyper-util     = { version = "0.1", features = ["server", "server-auto", "tokio"] }  # tokio executor glue + auto h1/h2
http-body-util = "0.1"      # streaming/aggregate body helpers (the AI stream path)
tower          = { version = "0.5", features = ["util"] }   # Service/Layer middleware contract
tower-http     = { version = "0.6", features = ["timeout","limit","trace","cors","compression-gzip"] }
tokio          = { version = "1", features = ["full"] }      # rt-multi-thread, net, time, sync, macros
tokio-tungstenite = "0.24"  # WebSocket (replaces actix-web-actors; no actor system needed)
socket2        = { version = "0.5", features = ["all"] }     # SO_REUSEPORT — verbatim from Robyn shared_socket.rs
```

This matches and tightens the teardown's own §9.1 blueprint (`robyn/01:832-859`, RECOMMENDATION there; promoted to decision here). `axum` itself is *hyper + tower + matchit* — Lancelot is effectively building "axum for a Python interpreter," so depending on hyper+tower directly (not axum) keeps the request type, body type, and the FFI seam fully under Lancelot's control while reusing axum's exact foundation. (axum's own router is `matchit`, the crate the teardown already mandates — `robyn/01 §9.1`, KNOWN.)

**Why not axum-the-framework even though it's hyper+tower:** axum imposes its `Handler`/`FromRequest`/`IntoResponse` extractor model and its `Router`. Lancelot's "handler" is a *Python callable behind a `FunctionInfo` ABI* (`robyn/03 §0.2`, KNOWN via SYNTHESIS steal #10), not a Rust closure. Threading a Python coroutine through axum's extractor traits adds a layer between the socket and the PyO3 seam for zero benefit. Use hyper's `service_fn` + `tower::Service` directly; reuse axum *patterns*, not axum *types*. INFERRED, high confidence.

### 1.2 actix-web — the proven shortcut Lancelot deliberately rejects

**KNOWN (Robyn `Cargo.toml:41-56`, `robyn/01 §0`):** Robyn ships on `actix-web 4.4.2` + `actix-http 3.3.1` + `actix 0.13.4` + `actix-web-actors 4.3.0`. It works in production. So actix is a *proven* path — the teardown is explicit: *"actix-web 4 is a safe shortcut and Robyn is the reference implementation"* (`robyn/01:861`, KNOWN).

**Why reject it for a greenfield framework (the case against, evidence-backed):**

1. **The actor `System`/`Arbiter` global.** `actix_web::rt::System::new().block_on(...)` builds actix's *own* runtime — N single-threaded current-thread tokio runtimes behind `Arbiter` actor scaffolding, connections statically sharded, **no work-stealing between arbiters** (`robyn/01 §3.1`, line 395, KNOWN). For uneven connection load (the AI norm — some requests are 50 ms RAG, some are 90 s streams) static sharding gives worse tail latency than tokio work-stealing (`robyn/01 §3.4` line 442, INFERRED). Confirmed externally: actix's edge *comes from* "N single-threaded Tokio runtimes — one per physical core … instead of a single multi-threaded work-stealing runtime" (KNOWN — https://medium.com/@abhinav.dobhal/actix-web-vs-e1e019714542) — i.e. its speed and its tail-latency liability are the same property.
2. **Dead-weight dep tree.** actix actors are used by Robyn *only* for WebSockets (`robyn/01 §0` line 83, KNOWN). Pulling the entire `actix`/`actix-web-actors` actor system to get WebSockets is gratuitous when `tokio-tungstenite` does it with zero actor machinery.
3. **Four-scheduler baroqueness.** Robyn's actix path forces *actix arbiters + a side current-thread tokio + the blocking pool + the asyncio loop* — four schedulers coordinating through the GIL (`robyn/01 §3.4`, line 422, KNOWN). hyper-on-owned-tokio collapses this to *one tokio multi-thread runtime + the blocking pool + the asyncio loop* — three, and the first two are both tokio.
4. **You don't control the runtime.** To enforce *"no `Python::with_gil` scope contains an `.await`"* (the single most important invariant — `robyn/01 §10` pitfall 1, KNOWN) and to size `worker_threads`/`max_blocking_threads` explicitly (`robyn/01 §9.2`, KNOWN), Lancelot must own `tokio::runtime::Builder`. Under actix the runtime is actix's.

**Throughput counter-argument, defused:** actix-web is ~10–15% higher RPS than axum under saturation, ~20k vs ~18k RPS on the cited hardware (KNOWN — https://medium.com/@abhinav.dobhal/actix-web-vs-e1e019714542; TechEmpower Round 23 https://www.techempower.com/blog/2025/03/17/framework-benchmarks-round-23/). **This delta is irrelevant for Lancelot** because (a) Python-bound throughput is GIL-capped regardless of the Rust stack (`robyn/01 §8`, KNOWN — "if handlers are pure-Python CPU, you're back to multiprocess scaling"); (b) the only path where the Rust delta shows is the const-route / high-RPS-short-call tier (`robyn/01 §7.4`, KNOWN), and there the win is the *fixed per-request µs* (Rust µs vs Python tens-of-µs) which hyper and actix are within noise of each other on — the 10–15% actix-vs-axum gap is a *serde-vs-other-JSON* and *static-sharding* artifact, not a parse-speed gap relevant to a 1–3-alloc const path. The cited article's own verdict: *"this throughput difference is irrelevant for 95% of applications"* (KNOWN). Lancelot is in that 95% on the Rust axis because its bottleneck is Python.

**Verdict: actix-web is the *fallback if time-to-first-version dominates* (Robyn is the working reference). For the actual product, hyper+tower.** This upgrades SYNTHESIS §4.8 Q1 from "(open: revisit with a spike)" to **decided: hyper+tower; spike actix only as a schedule hedge.**

### 1.3 axum — why it's the second choice, not the first

axum *is* hyper 1.x + tower + matchit + its extractor layer (KNOWN — seanmonstar hyper-v1; https://medium.com/@abhinav.dobhal/actix-web-vs-e1e019714542 "APIs were designed by the people who maintain the async runtime underneath it"). It is the most ecosystem-aligned, deepest-Tokio-integrated, fastest-onboarding choice for a *Rust* service (KNOWN — same source: "Days for Tokio-familiar engineers" vs actix "Weeks"). For Lancelot it is strictly dominated by raw hyper+tower for one reason: **axum's value is its extractor/handler ergonomics, and Lancelot's handler is Python, not Rust — so axum's entire value-add is unused while its `Router`/`Handler` types sit between the socket and the FFI seam.** Keep axum as the reference for *how* to wire hyper+tower+matchit correctly (its source is the cleanest tutorial), and as the fallback-above-actix if raw hyper proves too low-level for the team. INFERRED, high confidence.

### 1.4 ntex / xitca-web — faster, but wrong tradeoff

**KNOWN (https://medium.com/@abhinav.dobhal/actix-web-vs-e1e019714542):** ntex with neon-uring pushes past ~25k RPS where actix tops ~20k and axum ~18k; ntex/may_minihttp post ~2× axum JSON because they use `yarte` not `serde` (KNOWN — TechEmpower Round 23 discussion https://github.com/TechEmpower/FrameworkBenchmarks/discussions/8634). xitca-web has its *own* HTTP library base (not hyper) (KNOWN — same comparison search).

Reject both: (a) the throughput edge is in the GIL-shadow for Lancelot (§1.0, §1.2); (b) ntex's actor model carries the same "dead weight unless you're actor-based" objection as actix; (c) xitca's non-hyper HTTP base means no hyper stability promise and a thinner ecosystem; (d) the JSON-serializer delta is moot — Lancelot serializes via **pydantic-core `dump_json()` / cached `orjson` in/near Rust** (SYNTHESIS steal #6, `fastapi/03 §0.2`, `robyn/01 §8`), never serde-on-the-Rust-side for the typed path, so the serde-vs-yarte benchmark axis doesn't apply to Lancelot at all. INFERRED, high confidence.

### 1.5 may_minihttp — disqualified

`may_minihttp` is built on `may` (a stackful-coroutine green-thread runtime), not async/await. It posts top TechEmpower numbers via `yarte` + stackful coroutines (KNOWN — https://users.rust-lang.org/t/opinion-of-benchmarks-actix-vs-may-minihttp-vs-xitca-web/118039). Disqualified outright: stackful coroutines do not compose with `pyo3-async-runtimes` / tokio `into_future` (the *only* sanctioned asyncio↔Rust bridge — `robyn/01 §0` line 78, KNOWN). The entire GIL-drop-across-`.await` choreography (`robyn/01 §2.4`) assumes a `Future`-based async runtime. `may` is architecturally incompatible with the PyO3 async bridge. RECOMMENDATION: do not consider further.

### 1.6 glommio / monoio — thread-per-core io_uring: the GIL erases the win

Thread-per-core + io_uring is the genuine performance frontier for *pure-Rust* I/O services (KNOWN — Apache Iggy migration https://iggy.apache.org/blogs/2026/02/27/thread-per-core-io_uring/; monoio benchmarks: ~2× tokio at 4 cores, ~3× at 16 cores under high load — https://github.com/bytedance/monoio/blob/master/docs/en/benchmark.md). For Lancelot it is the wrong tool, on three independent grounds:

1. **Maintenance risk (KNOWN — https://iggy.apache.org/blogs/2026/02/27/thread-per-core-io_uring/):** *"Glauber moved on to work on Turso, and the Datadog team does not seem to be actively maintaining"* glommio; monoio *"is pretty far behind when it comes to feature parity and doesn't appear to be very actively maintained."* A framework with a multi-year stability obligation (§1.0) cannot sit on an unmaintained runtime. tokio is *"the mature, production-proven choice with active maintenance"* (KNOWN — same source).
2. **The GIL erases the win (INFERRED, decisive).** monoio/glommio's advantage is parallel *I/O* throughput. But Lancelot's I/O (parse/route/marshal/write) is already GIL-free in Rust *and* a small fraction of a request dominated by Python + an 800 ms LLM call (`robyn/01 §7.1`, §8, KNOWN). A 2–3× faster I/O layer on the ~µs Rust portion of a request whose total is dominated by GIL-serialized Python + network is an Amdahl rounding error. The teardown's core finding — *"the only fundamental advantage … is that the non-handler request lifecycle executes GIL-free in Rust … the Rust-core win is the fixed per-request overhead delta"* (`robyn/01 §8`, KNOWN) — already captures the whole Rust upside; thread-per-core doesn't add to it for a Python-bound workload.
3. **No tower ecosystem, harder PyO3 story.** glommio/monoio are not tokio; `pyo3-async-runtimes` ships a `tokio-runtime` feature (KNOWN — `robyn/01 §0` line 78); the entire `TaskLocals`/`scope_local`/`into_future` bridge (`robyn/01 §3.5`, KNOWN) is built and tested against tokio. Reimplementing the asyncio↔runtime bridge on a thread-per-core runtime is high-risk net-negative work. tower middleware (tower-http timeout/limit/trace) also assumes a tokio-compatible runtime.

RECOMMENDATION: **tokio multi-thread, not thread-per-core.** Revisit only if Lancelot ever ships a pure-Rust const-tier-only deployment mode with zero Python in the path (a v3+ "edge cache" idea — out of scope here).

### 1.7 How the closest competitors actually do it (grounding)

- **Granian (the closest competitor — Rust+Python, "faster uvicorn"):** built on **hyper + tokio** (KNOWN — https://github.com/emmett-framework/granian; https://www.deployhq.com/blog/python-application-servers-in-2025-from-wsgi-to-modern-asgi-solutions). Not actix. This is direct external validation of the §1.1 recommendation: the most credible Rust+Python server on the market chose hyper+tokio, exactly the stack recommended here. Granian exposes explicit `runtime-mode` *st* (N single-threaded runtimes) vs *mt* (one multi-thread runtime with N threads), `workers` (processes/threads), `blocking-threads` (Python-interacting), `runtime-threads` (network I/O), `runtime-blocking-threads` (fs) — a clean separation Lancelot should mirror (KNOWN — https://github.com/emmett-framework/granian). Granian's RSGI protocol passes *"a lightweight Python object backed directly by Rust memory"* instead of a giant ASGI dict (KNOWN — https://www.deployhq.com/blog/...) — this is exactly SYNTHESIS steal #4 (the lazy request proxy, `robyn/02 §0`) and steal #10 (`FunctionInfo` opaque ABI). Even on standard ASGI, Granian shows *"20% to 40% increase in requests per second"* purely from Rust HTTP parsing (KNOWN — https://www.deployhq.com/blog/...) — the quantified value of the Rust core, consistent with `robyn/01 §8`'s "fixed per-request overhead delta" framing.
- **Robyn:** actix-web 4.4 (KNOWN — `robyn/01 §0`). The teardown's verdict (`robyn/01:861`, KNOWN): proven shortcut, but hyper+tower is the right greenfield divergence. Lancelot diverges.
- **Socketify:** a libuv/µWebSockets-backed Python server (C/C++ core via the µWS stack), not Rust+PyO3. Its lesson is orthogonal (raw µWS speed) and not portable to a PyO3 architecture; it confirms only that *the HTTP core off the Python interpreter* is the universal pattern for fast Python servers — the thesis Lancelot already adopts (`robyn/01 §8`). INFERRED.

### 1.7a The benchmark evidence, assembled (so the "throughput is GIL-capped" claim is not hand-waved)

The whole §1 weighting (raw throughput = medium, not high) rests on one empirical claim: *for a Python framework the Rust-stack throughput delta is dominated by Python/Amdahl*. The evidence, assembled:

| Measurement | Number | Source | What it proves for Lancelot |
|---|---|---|---|
| actix-web vs axum, saturation | ~20k vs ~18k RPS (~10–15%) | KNOWN — abhinav.dobhal; TechEmpower R23 | The *entire* actix-over-axum (hence actix-over-hyper+tower) edge is ~10–15% **on a pure-Rust JSON benchmark with no Python in the loop** |
| Granian (Rust core) vs pure-Python ASGI server, standard ASGI | +20–40% RPS | KNOWN — deployhq | The value of *having a Rust HTTP core at all* is +20–40% — an order of magnitude bigger than the actix-vs-hyper choice within Rust |
| Granian RSGI (lazy proxy) vs ASGI dict | "throughput doubling or tripling" (Litestar) | KNOWN — deployhq | The lazy-request-proxy / opaque-ABI (SYNTHESIS steal #4/#10) is worth 2–3× — again an order of magnitude bigger than the Rust-framework choice |
| FastAPI IO-bound endpoint, GIL vs no-GIL | ~9.3 RPS, **unchanged** | KNOWN — kevaldekivadiya2415 | For the AI/IO workload, neither the Rust stack nor the GIL is the bottleneck — the network await is |
| LLM call latency | ~800 ms typical | KNOWN — `robyn/01 §7.1` | A ~10–15% delta on the ~µs Rust portion of an 800 ms request is ~0.000…% of wall time |

The conclusion is forced, not asserted: **the Rust-stack *choice within Rust* (hyper vs actix) is the smallest lever in the entire stack** — the big levers are (a) having a Rust core at all (+20–40%), (b) the lazy proxy / opaque ABI (2–3×), (c) not blocking the reactor with sync handlers (§4.4), (d) the GIL-drop-across-await discipline (§2.2). hyper vs actix is a ~10% lever *on the ~10% of request time that is Rust* — i.e. ~1% of wall time on the fast path and ~0% on the AI path. So the choice is correctly decided on **stability + control + ecosystem** (§1.0), where hyper wins decisively (3-yr promise, owned runtime, tower, no actor dead-weight), not on a throughput delta that is in the noise for this product. This is the quantified justification for §1.0's reweighting and the answer to "but actix is faster" — *yes, by an amount that does not matter here, at a cost (actor System, no runtime control) that does.*

### 1.8 §1 verdict

**RECOMMENDATION (resolves SYNTHESIS §4.8 Q1, definitively):**
`hyper 1.x` + `hyper-util 0.1` + `http-body-util 0.1` + `tower 0.5`/`tower-http 0.6` + `tokio 1.x` multi-thread + `tokio-tungstenite` (WS) + `socket2 0.5` (SO_REUSEPORT) + `matchit 0.8` (router). actix-web is the schedule hedge only. axum is the reference implementation and the second fallback. ntex/xitca/may_minihttp/glommio/monoio are all rejected with cause. The decision is driven by **stability + runtime control + ecosystem**, not raw throughput, because raw throughput is GIL-capped for a Python framework (`robyn/01 §8`, KNOWN). External validation: Granian (the nearest competitor) independently chose hyper+tokio.

---

## 2. PyO3 patterns & versions (the FFI seam)

The FFI seam is where Lancelot's whole performance thesis lives or dies (`robyn/01 §2.4` "THE FFI call boundary", KNOWN). This section fixes the pyo3 version, the marshalling discipline, the zero-copy buffer story, and the abi3 stance.

### 2.1 Version: 0.27 floor, 0.28-clean design, pin-the-minor

**KNOWN (Robyn `Cargo.toml:34-36`):** Robyn ships `pyo3 0.27.2` + `pyo3-async-runtimes 0.27` + `pyo3-async-runtimes-macros 0.27`. SYNTHESIS §4.1 and `robyn/01 §9.1` mandate `pyo3 0.27` (`extension-module`, `py-clone`).

**KNOWN (https://pyo3.rs/v0.28.3/free-threading; pyo3 migration/changelog https://pyo3.rs/main/migration):** PyO3 0.28 made three changes that are *architecture-relevant* for Lancelot:
1. `Python::allow_threads` is **renamed `Python::detach`**; `Python::with_gil`'s mental model becomes `Python::attach` (the thread attaches to the interpreter; under free-threaded builds holding `Python<'py>` no longer implies the GIL is held — it means *the thread is attached*).
2. `GILProtected<T>` is **removed entirely** — replace with `std::sync::Mutex`/atomics, using `MutexExt::lock_py_attached()` when the lock guards code that calls into Python.
3. PyO3 0.28 is the version with **first-class free-threaded support** + `PyUntypedBuffer` (type-erased buffer protocol) and defaults modules to *assumed thread-safe* (`gil_used = true` to opt back into GIL).

**RECOMMENDATION:** target **pyo3 0.27 as the build floor (matches Robyn, proven), but write the FFI seam in the 0.28 idiom from day one** — i.e. *do not use `GILProtected`* (use `parking_lot::Mutex` / atomics, already in the dep set per `robyn/01 §0`), *name the GIL-release boundary as a future `detach`* (a thin internal wrapper `lancelot::gil::without_gil(|| …)` that maps to `allow_threads` on 0.27 and `detach` on 0.28 — one-line migration), and *never assume `Python<'py>` ⇒ GIL-held* in any invariant comment (it won't under free-threaded; §3). This makes the eventual 0.27→0.28 bump (forced for free-threaded support) a mechanical change, not a rewrite. This directly addresses `robyn/01 §10` pitfall #5 (KNOWN): *"pyo3 minor-version churn — 0.27's IntoPyObject/FromPyObject/Bound API differs from 0.20/0.23; pin the minor and budget a migration each pyo3 minor."*

Pin exactly (`= "0.27.2"`-style or a tight `~0.27`), never a floating `0.27` that silently jumps to 0.28's breaking renames. Auto-generate the `.pyi` from the real ABI (SYNTHESIS steal #17 / `robyn/03 §0` — Robyn's hand-written `.pyi` drifted; KNOWN).

### 2.2 The GIL-release-across-await discipline (the one non-negotiable invariant)

This is the entire reason a Rust-core Python framework beats uvicorn (`robyn/01 §2`, KNOWN). Restated as the Lancelot invariant:

> **No `Python::with_gil`/`attach` scope may contain an `.await`.** (`robyn/01 §10` pitfall 1, `robyn/01 §9.3`, KNOWN — Robyn obeys this; it is "the whole game".)

The exact choreography to port verbatim (`robyn/01 §2.4`, `executors/mod.rs:519-545`, KNOWN):

```
async path:
  with_gil { call handler → coroutine ; create_task(coro, ctx) ; into_future }   GIL #2  (~µs)
  DROP GIL ───────────── .await the Rust future (asyncio loop steps the coro) ─── GIL-FREE
  with_gil { marshal return (pydantic-core validate_json/dump_json, 1 Rust pass) } GIL #3  (~µs)
sync path:
  BlockingRunner.run(move |py| { ctx.run(handler, req); marshal })  — GIL held on a POOL thread only
```

For an `async def chat(): return await llm.generate(...)` the framework holds the GIL for *single-digit µs total per request*; the 800 ms LLM call is entirely GIL-free (`robyn/01 §7.1`, KNOWN). 5000 concurrent LLM calls coexist as 5000 parked coroutines (`robyn/01 §2.4` line 317, KNOWN).

**Enforcement (RECOMMENDATION, hardening Robyn which only does this by convention):**
- `#![deny(clippy::await_holding_lock)]` workspace-wide (`robyn/01 §9.3`, KNOWN-as-recommendation).
- A custom clippy/lint or a code-review checklist item: grep every `with_gil`/`attach` span for `.await`.
- The internal `without_gil`/`with_gil` wrappers (`§2.1`) take a non-async closure type so the type system makes "await inside with_gil" *not compile* where it can be structured that way (INFERRED design technique).
- `pyo3-async-runtimes 0.27` `tokio::into_future` is the **only** sanctioned async bridge (`robyn/01 §0` line 78, KNOWN); for the response side build the 5 custom awaitables (`robyn/01 §6`, "38–55% faster than `future_into_py`", KNOWN verbatim Robyn comment) — but `into_future` for coroutine→Rust-future.

### 2.3 Zero-copy buffer passing — `Bound`, `PyBytes`, the buffer protocol

The marshalling cost is the residual per-request tax once the GIL choreography is right. SYNTHESIS steal #4 (lazy request proxy, `robyn/02 §0`) and steal #8 (Blazeio zero-copy receive) both depend on not copying bodies across the FFI. Current best practice (2026):

- **`Bound<'py, T>`, never the legacy GIL-Ref/`Py<T>`-deref pattern.** KNOWN (https://pyo3.rs/main/performance): *"Access to `&Bound<'py, PyAny>` implies access to `Python<'py>`"* — use `Bound::py()` to get the Python token rather than calling `Python::attach`/`with_gil` again, *"which has measurable overhead even when already attached."* Every hot-path function takes `&Bound<'py, …>` and threads `py` from it.
- **`cast()` not `extract()` for native types on the hot path.** KNOWN (https://pyo3.rs/main/performance): for `PyList`/`PyBytes`/etc., `cast()` avoids the costly `PyDowncastError`→`PyErr` conversion that `FromPyObject`/`extract()` forces. Lancelot's response fast-path (downcast pyclass / `bytes` / `str` — `robyn/01 §8`, KNOWN) must use `cast`/`downcast`, not `extract`.
- **`vectorcall` via Rust tuples.** KNOWN (https://pyo3.rs/main/performance): calling the Python handler with a **Rust tuple** of args uses the faster `vectorcall` protocol; `Bound<PyTuple>`/`Py<PyTuple>` fall back to the slower `tp_call`. Lancelot's `FunctionInfo`-driven arg binding (SYNTHESIS steal #10, `robyn/03 §0.2`) must assemble args as a Rust tuple, not build a `PyTuple` object.
- **Zero-copy bytes both directions.** Request body: the Rust core owns a pooled `BytesMut`/`Bytes` (Blazeio's pooled-buffer model in Rust — `blazeio/01 §8`/SYNTHESIS steal #8, KNOWN); expose it to Python lazily as a `memoryview`/`PyBytes` over the Rust buffer (the lazy proxy — SYNTHESIS steal #4). Response body: accept `bytes`/`bytearray`/`memoryview` and pass as `Bytes` **without a UTF-8 round-trip**; only validate when the user yields `str` (`robyn/01 §7.3.4`, KNOWN — Robyn's per-chunk `extract::<String>()` is the anti-pattern). The `pyo3-bytes` crate (`PyBytes` wrapper over `bytes::Bytes` implementing the buffer protocol, KNOWN — https://docs.rs/pyo3-bytes) or a hand-rolled `#[pyclass]` over `Bytes` is the mechanism. PyO3 0.28's `PyUntypedBuffer` (KNOWN — pyo3.rs migration) is the type-erased path for arbitrary buffer-protocol objects (numpy arrays a user might return).
- **The FFI marshalling cost, quantified.** KNOWN (https://pyo3.rs/main/performance): *"attaching and detaching from the Python interpreter takes less than a millisecond … any work expected to take multiple milliseconds can likely benefit from detaching."* Combined with `robyn/01 §2.4` (KNOWN — GIL held single-digit µs ×3/req): the FFI seam itself is ~µs when done right; the cost that matters is *allocations per request* (SYNTHESIS: Robyn ~30–66, FastAPI ~10–15, Lancelot target ~1–3 — `SYNTHESIS.md:136`). The lazy proxy (steal #4) is what hits the 1–3 target; zero-copy buffers are what keep it there for bodies.

### 2.4 `abi3` — the stable-ABI decision (and its hard collision with free-threading)

**KNOWN (https://pyo3.rs/v0.28.3/free-threading; PEP 803 https://peps.python.org/pep-0803/):** *"The free-threaded build uses a completely new ABI and there is not yet an equivalent to the limited API for the free-threaded ABI."* If a crate sets the `abi3` feature, *"PyO3 will print a warning and ignore that setting when building extensions using the free-threaded interpreter."* **abi3 and free-threaded are mutually exclusive today.** PEP 803 proposes `abi3t` (a stable ABI for free-threaded builds) targeting **CPython 3.15+**; initial testing shows PyO3/CFFI/Cython will work with it under patched packaging tools (KNOWN — PEP 803; https://discuss.python.org/t/pep-803-...).

This is **architecture-defining for packaging** (SYNTHESIS §4.7 wants abi3 — "one wheel per platform, not per CPython"). The decision:

| Build | ABI | Wheels | Rationale |
|---|---|---|---|
| **GIL build (default)** | `abi3-py311` | **1 wheel / platform** for CPython ≥3.11 | SYNTHESIS §4.7 verbatim — ~5× fewer artifacts, zero churn on new CPython. KNOWN: abi3 works fine on GIL builds. |
| **Free-threaded build (opt-in)** | **non-abi3, version-specific** | 1 wheel per (platform × `cp313t`/`cp314t`) | FORCED by the KNOWN abi3↔ft incompatibility. No way around it until `abi3t`. |
| **Future (CPython 3.15+)** | `abi3t` | 1 ft wheel / platform | PEP 803 — adopt the moment it lands; collapses the ft matrix back to 1/platform. |

**RECOMMENDATION:** maturin builds **both**: an `abi3-py311` wheel set (the default `pip install lancelot` for GIL CPython) and a `cp314t`/`cp313t` free-threaded wheel set (`pip install lancelot` resolves it automatically on a `python3.14t`). Tag the build with the `Py_MOD_GIL` slot correctly per build (§3.3). This means Lancelot's wheel matrix is **(1 abi3 / platform) + (1 per ft-minor / platform)** — far below Robyn's ~20–25/release (`robyn/03 §0.8`, KNOWN) for the GIL path, with the ft path being the only per-minor cost, and that cost evaporating at CPython 3.15 + `abi3t`. SYNTHESIS §4.7's "abi3 wheels" stance is correct *for the GIL build* and must be qualified: it is impossible for the free-threaded build until PEP 803.

**One abi3 caveat to measure, not assume (INFERRED):** the limited API (abi3) routes some operations through function calls instead of inlined macros (e.g. `Py_INCREF`/type-slot access become calls), which is a *small* per-call cost. For Lancelot this is mitigated by design: the hot path crosses the FFI seam ~3×/request (`robyn/01 §2.4`, KNOWN) holding the GIL for single-digit µs, and the heavy work (validation/serialization) is *inside* pydantic-core/orjson which are themselves compiled extensions not bottlenecked by Lancelot's abi3 boundary. The abi3 packaging win (1 wheel/platform, §2.4 table) dominates the marginal per-call cost for a framework whose per-request FFI surface is tiny by design. RECOMMENDATION: ship abi3 for the GIL build; add a non-abi3 `cpXY` "performance wheel" build *only if* the spike (§8) measures the abi3 boundary cost as material on the const-tier/high-RPS path — do not pre-optimize this.

### 2.5 The per-request marshalling cost model (the residual tax, quantified)

Once the GIL choreography (§2.2) is correct, the residual per-request cost is **allocations + FFI crossings**, not GIL time. The teardown quantifies the target precisely: Robyn ~30–66 allocs/req (eager total marshalling, even if the handler reads nothing — `robyn/02 §0,§3.4`, SYNTHESIS line 94, KNOWN), FastAPI ~10–15 (`Request` + dual exit stacks — SYNTHESIS line 32, KNOWN), **Lancelot target ~1–3** (`SYNTHESIS.md:136`, KNOWN-as-recommendation). The mechanism to hit ~1–3 (INFERRED from the steal-list, made concrete here):

| Per-request cost | Robyn (KNOWN) | Lancelot mechanism (RECOMMENDATION) | Source |
|---|---|---|---|
| Request object | Full marshal of headers+query+body into Python dicts/str eagerly | **Lazy proxy** `#[pyclass]` holding the raw Rust request + unconsumed `Bytes`; headers/query/body/json materialize **on attribute access only** | SYNTHESIS steal #4, `robyn/02 §0,§9`, KNOWN |
| Body copy | `String` copy + UTF-8 validate eagerly | Zero-copy `memoryview`/`PyBytes` over the pooled Rust `Bytes`; copy only if handler calls `.json()`/`.text()` | §2.3, `blazeio/01 §8`, KNOWN |
| Arg binding | `pythonize`/dict build per request | Rust-tuple `vectorcall` from the compiled `FunctionInfo` plan (no per-request signature introspection) | §2.3, SYNTHESIS steal #10, `robyn/03 §0.2`, KNOWN |
| contextvars.Context | 1 alloc/req (`contextvars.Context()`) | Keep — it is the tenant-isolation correctness mechanism, the *one* alloc that is non-negotiable | `robyn/01 §4`, KNOWN |
| Response | `extract::<String>` / orjson copy | Fast downcast: pyclass `Response` / cached `orjson.dumps` / `str`\|`bytes` passthrough — `cast` not `extract` | §2.3, `robyn/01 §8`, KNOWN |
| Coroutine task | `create_task` + `into_future` (1 Future) | Keep `into_future` for coro→Rust; custom `PyIterAwaitable`/`PyFutureAwaitable` for Rust→Python (no per-call Python `Future` alloc — "38–55% faster") | `robyn/01 §6`, KNOWN |

The decisive insight (INFERRED, ties §2 together): **the ~µs GIL hold (§2.2) is already optimal in Robyn; the 30–66→1–3 alloc reduction is the *actual* remaining Lancelot win on the fixed-overhead path** (`robyn/01 §8` "the Rust-core win is the fixed per-request overhead delta", KNOWN). This is what makes the const-tier and high-RPS-short-call paths 5–10× lighter than FastAPI (`robyn/01 §7.4`, KNOWN) — and it is a *PyO3-pattern* problem (lazy proxy + zero-copy + vectorcall + cast), not an HTTP-stack problem, which is why §1's stack choice is correctly weighted away from raw throughput (§1.0).

---

## 3. The free-threaded Python question (the architecture-defining one)

This is the decision that could obsolete Lancelot's entire multiprocess model — or not. The brief flags it as "potentially architecture-defining; research it hard." Conclusion first, then the full evidence and the reasoning.

> **Verdict (RECOMMENDATION): multiprocess-first, free-threaded-clean, dual-build. NOT nogil-first. The multiprocess model is NOT obsolete and remains the default for the foreseeable horizon (through at least CPython 3.15, ~2026–2027).**

### 3.1 Current state of free-threaded CPython (2026) — KNOWN, sourced

- **Status: supported, not experimental, not default.** PEP 779 ("Criteria for supported status") was accepted; **as of Python 3.14 the free-threaded build is officially supported (Phase II of PEP 703)** but is *not* the default interpreter build (KNOWN — https://peps.python.org/pep-0779/; https://docs.python.org/3/howto/free-threading-python.html; https://docs.python.org/3/whatsnew/3.14.html). 3.13 introduced it experimentally; 3.14 (released Oct 2025) graduated it to supported with official macOS/Windows installers.
- **Single-thread penalty: 5–10% (3.14), down from worse in 3.13.** KNOWN — https://docs.python.org/3/howto/free-threading-python.html: *"the average overhead ranges from about 1% on macOS aarch64 to 8% on x86-64 Linux"* on pyperformance; the broader figure quoted across sources is **5–10%** (KNOWN — astral.sh/blog/python-3.14; https://peps.python.org/pep-0703/). 3.14 enabled the specializing adaptive interpreter (PEP 659) in free-threaded mode, which closed most of 3.13's larger gap (KNOWN — whatsnew 3.14).
- **Memory overhead: ~15–20% higher (geometric mean, pyperformance).** KNOWN — astral.sh/blog/python-3.14 / whatsnew 3.14. Material for a multi-worker AI box (each worker is bigger).
- **Scaling: near-linear on CPU-bound parallel work.** KNOWN — FastAPI under `3.13.0t`: CPU-bound endpoint ~4 RPS (GIL, 1 uvicorn worker) → ~32 RPS (no-GIL), **~8× on a 4-thread M2** (KNOWN — https://medium.com/@kevaldekivadiya2415/...). Meta internal: near-linear to 8 cores, ~7.8× on 8-core (KNOWN — https://medium.com/@backendbyeli/...). **I/O-bound endpoints: ~no change** (~9.3 RPS both builds) — *"The GIL was never the bottleneck here"* (KNOWN — same FastAPI benchmark).
- **Ecosystem readiness: ~50% of the top native-wheel packages, Feb 2026.** KNOWN — https://hugovk.github.io/free-threaded-wheels/ + https://labs.quansight.org/blog/free-threaded-python-halfway: *"As of February 19th, 2026 … 180/360 tracked packages upload free-threaded wheels to PyPI"* (rose to 183/360). numpy 2.3+ improved ft compatibility; pydantic/django doing the safety work; the long tail is unported (KNOWN — same sources).

### 3.2 What this means for a *new* framework designed in 2026 (the reasoning — INFERRED from the KNOWN facts)

**The case that free-threading obsoletes the multiprocess model:** if no-GIL gives near-linear CPU scaling in *one* process, then "N processes to escape the GIL" (`robyn/01 §1`, the entire CPU-scaling rationale, KNOWN) is unnecessary — one process × N threads suffices, with shared memory (no per-worker model duplication, no IPC, no `dill` pickling the socket — `robyn/01 §1.2`, KNOWN), lower total memory despite the 15–20% per-interpreter overhead (one interpreter, not N).

**Why that case does NOT hold *yet*, and why multiprocess stays the default (decisive, INFERRED from KNOWN facts):**

1. **Lancelot's value prop is AI/IO-bound workloads, where free-threading buys ~nothing.** KNOWN: I/O-bound FastAPI endpoints showed **no change** GIL vs no-GIL (`~9.3 RPS` both — https://medium.com/@kevaldekivadiya2415/...) — because for I/O the GIL is *already released across the `await`* (`robyn/01 §7.1`, §8, KNOWN: all of Robyn, Blazeio, FastAPI release the GIL during the network await). The 8× free-threading win is on **CPU-bound** code. Lancelot's headline workload is `await llm.generate(...)` — the GIL is already not the bottleneck there (`robyn/01 §7.1`, KNOWN). So the single biggest reason to go nogil-first does not apply to Lancelot's primary workload. Free-threading helps Lancelot only on the *CPU-bound minority* of handlers (heavy pure-Python transforms, sync ML pre/post-processing) — exactly the handlers that already belong on the bounded blocking pool (`robyn/01 §5`, KNOWN), and where multiprocess already gives linear scaling today with zero ecosystem risk.
2. **The ecosystem is half-ready (KNOWN — 50%, Feb 2026).** Lancelot's own runtime dep closure is the gate, not the package average: it needs uvloop (or a replacement), pydantic-core, orjson all shipping ft wheels and ft-safe. A framework that *requires* free-threading inherits every unported transitive C-extension of its users (numpy-stack, DB drivers, ML libs). A C-extension not marked ft-safe **silently re-enables the GIL at import with a `RuntimeWarning`** (KNOWN — https://docs.python.org/3/howto/free-threading-python.html) — so an nogil-first Lancelot would, in the common real-world dep tree, *silently fall back to single-interpreter-GIL with no multiprocess escape* = strictly worse than today's multiprocess model. This is the killer argument against nogil-first: **the failure mode of "ship nogil-first, user has one unported C-ext" is worse than the multiprocess baseline.**
3. **abi3 incompatibility forces a wheel-matrix penalty for nogil (KNOWN — §2.4, PEP 803).** An nogil-first framework cannot ship abi3 — it must ship per-ft-minor wheels until CPython 3.15 + `abi3t`. SYNTHESIS §4.7's packaging win (1 wheel/platform) is *only* available on the GIL build. Making nogil the default sacrifices the packaging thesis.
4. **5–10% single-thread tax + 15–20% memory (KNOWN).** For the IO-bound AI common case (where free-threading's parallelism upside is ~zero per point 1), nogil-first means paying a 5–10% latency tax and 15–20% memory for a parallelism benefit the workload doesn't use. Net negative for the primary workload.
5. **It's not the default until ~CPython 3.15 (KNOWN — roadmap, https://peps.python.org/pep-0703/, astral.sh).** A framework designed in 2026 must run great on the *default* CPython that 95%+ of users will `pip install` under for the next 2+ years — that is the GIL build. Optimizing for the non-default build as the primary path is a market mistake.

**Why nogil-clean-from-day-one is nonetheless mandatory (INFERRED, decisive):** the *transition* is real and Lancelot will outlive the GIL default. Retrofitting thread-safety into a Rust core that assumed `Python<'py> ⇒ GIL-held` (the implicit Robyn invariant — `robyn/01 §2.1`, KNOWN) is a rewrite. Designing ft-clean now is nearly free. Concretely (RECOMMENDATION):
- Declare the module `#[pymodule(gil_used = false)]` / `PyModule::gil_used(false)` (KNOWN — pyo3.rs free-threading) — but **only after** auditing every `static`/`OnceCell`/`DashMap`/`parking_lot` for true thread-safety under no-GIL (the `TASK_LOCALS` `OnceCell`, the route maps, the const-tier map — `robyn/01 §3.5,§0`, KNOWN). Under no-GIL these are accessed by multiple threads *with no GIL serializing them* — every shared-mutable structure needs real synchronization, not GIL-implied safety.
- Never use `GILProtected` (removed in 0.28 — KNOWN §2.1). Use `parking_lot::Mutex` (already in the dep set — `robyn/01 §0`) / atomics; `MutexExt::lock_py_attached()` where the guarded code calls Python (KNOWN — pyo3.rs).
- Treat `Python<'py>` as "attached", not "GIL held", in all reasoning and comments (KNOWN — pyo3.rs: under ft the lifetime only means attached). The GIL-drop-across-await invariant (§2.2) becomes **detach-across-await**, semantically identical, and *still correct and beneficial under free-threading* (detaching lets other threads run; the invariant is build-agnostic).
- CI matrix must include `python3.14t` (free-threaded) from day one; run the full test suite + a `sys._is_gil_enabled()` assertion (KNOWN — docs.python.org) to catch accidental GIL re-enable.

### 3.3 The dual-build model — how multiprocess and free-threaded coexist in one codebase (RECOMMENDATION)

Granian already proved the shape (KNOWN — https://github.com/emmett-framework/granian): under standard CPython, **workers = processes**; under free-threaded CPython ≥2.0, **workers = threads in one shared interpreter, each with its own asyncio loop running on the worker thread (not the main thread)**. Lancelot adopts the identical model:

| Build | Scaling unit | Mechanism | Memory | Wheel |
|---|---|---|---|---|
| **GIL CPython (default)** | N OS processes | `SO_REUSEPORT` fd-clone per fork (`robyn/01 §1.2`, KNOWN) — verbatim | N × interpreter (model/weights duplicated per worker) | abi3-py311, 1/platform |
| **Free-threaded CPython (opt-in)** | N threads, 1 process | one interpreter; N tokio worker threads each driving an asyncio loop; shared route maps/const-tier (now genuinely concurrent — must be ft-safe per §3.2) | 1 × interpreter + 15–20% (shared model/weights — *big win for large-model AI apps*) | cp313t/cp314t, 1/(platform×minor) |

The Rust hot path is **identical** in both — the GIL-drop/detach-across-await choreography (§2.2) is build-agnostic and correct in both. The only differences are (a) the spawn strategy (fork+SO_REUSEPORT vs thread spawn), (b) the `Py_MOD_GIL` slot value at build time, (c) whether the shared structures rely on GIL-implied or explicit synchronization (Lancelot uses explicit always — §3.2 — so it's correct in both with no branch). This means **one codebase, two builds, no architectural fork** — the multiprocess model is not "replaced," it is one of two deployment topologies selected by which CPython you installed.

The free-threaded build's *killer feature for AI specifically* (INFERRED, high confidence): a 13B-param model loaded once and **shared across all worker threads** instead of duplicated per process. Under multiprocess, 8 workers × a multi-GB in-process model/embedding-table = 8× the RAM; under free-threaded, 1×. For local-model AI deployments this is the single strongest reason Lancelot must be free-threaded-*ready* even while multiprocess-*default*. This is a documented "when to choose free-threaded" lever, not the default.

### 3.3a The Rust-core shared-state hazard map under free-threading (the part that is a *rewrite* if deferred)

This is why "free-threaded-clean from day one" is not optional bookkeeping but the load-bearing reason to decide *now*. Under the GIL build, every shared structure in the Rust core is *implicitly* serialized by the GIL whenever Python touches it — Robyn relies on exactly this implicit safety (`robyn/01 §2.1`, KNOWN: the implicit invariant is `Python<'py> ⇒ exclusive interpreter access`). Under free-threading that implicit serialization **is gone**: multiple OS threads run Python bytecode concurrently, and any Rust structure reachable from `#[pyclass]`/module state is now genuinely concurrently accessed with *no* GIL barrier (KNOWN — https://pyo3.rs/v0.28.3/free-threading: `Python<'py>` means "attached", not "exclusive"). The audit, structure by structure (the concrete §3.2 "audit every static/OnceCell/DashMap" made specific):

| Rust shared structure | GIL-build safety | Free-threaded hazard | Lancelot RECOMMENDATION |
|---|---|---|---|
| `TASK_LOCALS: OnceCell<TaskLocals>` (`robyn/01 §3.5`, KNOWN) | GIL serializes `get_or_try_init`; `clone_ref` is GIL-safe refcount bump | `OnceCell` init race is fine (it's `sync::OnceCell`), but **`clone_ref` / `Py<T>` refcounting is no longer GIL-protected** — pyo3 0.28 handles this internally for the ft build, but only if built against the ft ABI (non-abi3, §2.4) | `OnceCell` is correct; ensure all `Py<T>` clones go through pyo3 0.28's ft-aware refcount path; never hand-roll refcount |
| Route maps / const-tier map (`DashMap`, `robyn/01 §0,§2`, KNOWN) | Read-mostly; GIL serializes the Python-visible reads anyway | Read-mostly after boot → `DashMap`/`arc-swap` of an immutable map is correct and lock-free; **only hazard is if routes mutate at runtime** (hot-reload) | Freeze routing tables after boot into an `Arc<ImmutableRouter>`; hot-reload swaps the whole `Arc` (RCU), never mutates in place |
| Per-request `contextvars.Context` (`robyn/01 §4`, KNOWN) | Fresh per request → no sharing | No sharing → **safe by construction**, and *more* important under ft (sync handlers on the blocking pool run truly in parallel; the per-request context is the only thing preventing cross-request `ContextVar` bleed across now-parallel pool threads) | Keep verbatim; ft makes it *more* load-bearing, not less — a tenant-isolation defect here is now a *concurrent* data race, not just a reuse bug |
| Blocking-pool queue / counters (`robyn/01 §5`, KNOWN) | `crossbeam-channel` is already MPMC-safe | Already thread-safe (crossbeam) — the GIL was never what protected it | Already correct; bound it (§4.4) — orthogonal to ft |
| `Py<T>` stored in `#[pyclass]` fields (lazy request proxy, response, awaitables) | GIL serializes all access | Concurrent access to a `Py<T>` field from two threads is a data race unless `Sync` + interior sync | pyo3 0.28 requires `#[pyclass(frozen)]` or explicit `Sync` types for ft modules; design every `#[pyclass]` as `frozen` + interior `Mutex`/atomics from day one (this is the single biggest day-one design constraint ft imposes) |
| `static mut` / lazy globals (logging, metrics) | GIL-serialized | Data race | None allowed; `parking_lot::Mutex`/atomics only (already the dep — `robyn/01 §0`) |

The decisive point (INFERRED, high confidence): items 1–4 and 6 are *cheap to get right at design time and a rewrite to retrofit*. Item 5 — **every `#[pyclass]` must be `frozen` + interior-synchronized** — is a pervasive structural constraint that touches the lazy request proxy (SYNTHESIS steal #4), the response objects, and the 5 custom awaitables (`robyn/01 §6`). If Lancelot designs these GIL-implicitly-safe (Robyn-style mutable `#[pyclass]` fields) it works on the GIL build and **is undefined behavior on the ft build** — discovered only when someone runs under `python3.14t`, by which point the API is shipped and the fix is breaking. This is the concrete, code-level reason the nogil decision must be made at architecture time, not deferred: *the cost of free-threaded-clean is a design constraint adopted now; the cost of deferring it is a breaking rewrite of the entire `#[pyclass]` surface later.* The verdict (multiprocess-default, ft-clean) is what lets Lancelot pay the cheap cost and avoid the expensive one.

### 3.4 §3 verdict (the nogil stance, decisive)

**RECOMMENDATION:** **Multiprocess-first, free-threaded-clean, dual-build.**
- *Default*: multiprocess + GIL CPython + abi3 wheels. This is the production default through ~CPython 3.15.
- *Not obsolete*: the multiprocess model (`robyn/01 §1`, SYNTHESIS steal #3) stays — it is one of two deployment topologies, the safe one, and the only one whose ecosystem is 100% ready and whose packaging hits the abi3 1-wheel/platform thesis.
- *Free-threaded-clean from day one*: `gil_used=false` (post audit), no `GILProtected`, explicit synchronization on every shared structure, detach-across-await framing, `python3.14t` in CI. Cheap now, a rewrite later.
- *Free-threaded as opt-in build*: ship `cp31Xt` wheels; document it as the choice for large-shared-model AI apps and CPU-bound-handler-heavy apps.
- *Trigger to flip the default to free-threaded*: when (a) Lancelot's full runtime dep closure ships ft wheels and is ft-safe, (b) the single-thread penalty is <5%, (c) PEP 803 `abi3t` ships (restores 1-wheel/platform), and (d) free-threaded is the CPython default. Re-evaluate each CPython release; this is a deferred decision with explicit, measurable criteria — not a guess.

---

## 4. Async runtime tuning

The runtime topology is where Robyn is most baroque (four schedulers — `robyn/01 §3.4`, KNOWN) and where hyper+tower lets Lancelot be clean. This section fixes the exact runtime construction.

### 4.1 tokio multi-thread, NOT current-thread-per-process, NOT thread-per-core

**KNOWN (Robyn `runtime.rs:137-143`, `robyn/01 §0` line 81, §3):** Robyn does *not* run a tokio multi-thread runtime for HTTP — it runs `actix_web::rt::System` (N current-thread tokio runtimes behind actix `Arbiter`s, connections statically sharded, no work-stealing) and only a `new_current_thread()` tokio runtime in the Granian-derived helper. The teardown's verdict (`robyn/01 §0` line 81, §3.4, KNOWN-as-recommendation): *"For Lancelot, drive your own `tokio::runtime::Builder::new_multi_thread()` with explicit `worker_threads` and `max_blocking_threads`."*

**RECOMMENDATION (resolves the work-stealing vs thread-per-core sub-question of §4):** **one tokio multi-thread runtime per process, work-stealing.** The reasoning (INFERRED from `robyn/01 §3.4` line 442, KNOWN):
- The Python part is GIL-serialized regardless (`robyn/01 §8`, KNOWN), so the *only* thing the runtime parallelizes is the Rust I/O (accept/parse/marshal/write). Work-stealing does that *strictly better* than static per-core sharding when connection load is uneven — and AI load is maximally uneven (a 30 ms RAG lookup next to a 120 s SSE stream). Static sharding (actix arbiters, thread-per-core) gives one shard a 120 s stream while its peers idle; work-stealing rebalances. `robyn/01 §3.4` line 442 (KNOWN): *"Multi-thread tokio with explicit worker_threads gives work-stealing across cores for the Rust I/O — better tail latency than actix's sharded single-thread arbiters when connection load is uneven."*
- Thread-per-core io_uring (glommio/monoio) is rejected in §1.6 (maintenance + GIL-erases-win + no tokio bridge). Same conclusion, runtime axis: thread-per-core's static affinity is the *wrong* model for bursty uneven AI load, independent of the maintenance issue.

### 4.2 The exact runtime construction (RECOMMENDATION — promoting `robyn/01 §9.2`)

```rust
// Per OS process (GIL build) OR per process==1 (free-threaded build): one tokio MT runtime.
let rt = tokio::runtime::Builder::new_multi_thread()
    .worker_threads(cfg.runtime_threads.unwrap_or(cgroup_aware_cpus().clamp(2, 4)))  // Rust I/O parallelism
    .max_blocking_threads(512)        // tokio's OWN pool: fs/DNS/tokio::fs ONLY — never Python
    .enable_all()
    .thread_name("lancelot-io")
    .thread_stack_size(2 * 1024 * 1024)
    .build()?;
```

Three numbers, each justified by the teardown:
- **`worker_threads = clamp(cgroup_cpus, 2, 4)`** — Robyn defaults workers to 1, fast-mode 2 (`robyn/01 §1.3`, KNOWN); the teardown notes *"more workers than ~2–4 per process is mostly wasted for Python-bound work"* (`robyn/01:175`, KNOWN). Rust I/O does not need more than ~CPU threads; Python won't keep more busy. **cgroup-aware**, not `os.cpu_count()` — Robyn's `os.cpu_count()` ignores container quotas (`robyn/01 §0`/`robyn/03 §0` AVOID, KNOWN) → over-threading in a 1-vCPU container.
- **`max_blocking_threads = 512` is tokio's pool for `tokio::fs`/DNS ONLY.** It must **never** run Python sync handlers (`robyn/01 §3.4` line 444, KNOWN: *"never use tokio's shared spawn_blocking for sync handlers (poisons fs/DNS)"*). Sync Python goes on the *separate* `BlockingRunner` (§4.4).
- **N processes** over this (GIL build): `SO_REUSEPORT`+`SO_REUSEADDR`+`TCP_NODELAY`, `listen(16384)`, fd-cloned per fork — verbatim Robyn `shared_socket.rs` (`robyn/01 §1.2`, KNOWN, "Port verbatim. Perfect as-is.").

### 4.3 How the Rust runtime and the Python asyncio loop coexist under PyO3

**KNOWN (Robyn `server.rs:92,120-124,255-273,289`, `robyn/01 §3, §3.5`):** the coexistence mechanism, ported verbatim:
- One **asyncio/uvloop event loop per process, on its own thread**, `run_forever()` (`robyn/01 §3.1` line 209, §2 line 211, KNOWN). The Python `async def` coroutines are *driven* by this loop; their `await`s resume here.
- `TaskLocals` (= reference to that asyncio loop + a copied `contextvars.Context`) created **once per process** in a `OnceCell` under one GIL acquisition (`robyn/01 §3.5`, KNOWN — `OnceCell::get_or_try_init`).
- Per request: `Python::with_gil(|py| TASK_LOCALS.get().clone_ref(py))` — a single refcount bump (GIL #0, ~ns — `robyn/01 §2.2`, KNOWN) — then `scope_local(task_locals, fut)` binds that loop+context to the tokio task so a nested `pyo3_async_runtimes::tokio::into_future(coro)` knows which asyncio loop to schedule the coroutine on. *"Without this, into_future wouldn't know where to drive the Python coroutine"* (`robyn/01 §3.5`, KNOWN).
- The tokio worker thread that called the handler is **free to process other requests** while the coroutine's `await`s are stepped by the asyncio loop on its own thread (`robyn/01 §2.4` line 314, KNOWN). The GIL is grabbed only in tiny slices when the loop steps a ready coroutine.

**RECOMMENDATION:** port this *exactly* (`robyn/01 §9.4` table: "Port verbatim", KNOWN). Do **not** create `TaskLocals` per request (re-snapshots context, adds GIL cost — `robyn/01 §3.5` line 465, KNOWN). The only Lancelot change vs Robyn: it runs on Lancelot's owned tokio MT runtime instead of actix's `System`, which *simplifies* the coexistence (one tokio runtime + the asyncio loop thread + the blocking pool = three domains, vs Robyn's four — `robyn/01 §3.4`, KNOWN).

### 4.4 The sync-handler blocking pool — dedicated, bounded, GIL-detaching

**KNOWN (Robyn `blocking.rs:34-177`, Granian-derived, `robyn/01 §5`):** sync `def` handlers must not run on the I/O reactor (they hold the GIL for their full duration *and* occupy a reactor thread — `robyn/01 §2.4` line 319, §10 pitfall 2, KNOWN). Robyn ships a 3-variant `BlockingRunner` (Empty/Mono/Pool) fed by a `crossbeam-channel` MPMC queue; worker threads `Python::attach` then **`py.detach()` while blocked on `queue.recv()`** so idle pool threads don't hold the GIL (`robyn/01 §5.4`, KNOWN — "THE key insight"). Adaptive rate-limited single-flight thread spawn, idle-timeout thread death (`robyn/01 §5.3`, KNOWN).

**RECOMMENDATION (port + fix Robyn's two defects):**
1. Port the 3-variant pool + GIL-detach-on-recv + adaptive spawn verbatim (`robyn/01 §9.4`, KNOWN).
2. **Bound the queue** (Robyn uses `crossbeam-channel::unbounded()` — `robyn/01 §5`, §10 pitfall 2, KNOWN: *"a latent memory/latency bomb under sync overload"*). Use `bounded(cfg.sync_queue_max.unwrap_or(10_000))`; on full → **load-shed: the Rust core returns `503` + `Retry-After` *before* spawning the Python frame** (`fastapi/01 §5` line 402, 409, KNOWN — this is exactly FastAPI's missing backpressure; `robyn/01 §5.6`, KNOWN-as-recommendation).
3. **Actually wire it into the HTTP path.** Robyn's sync-handler offload *exists but is not wired into the HTTP path* — a blocking `def` handler pins the worker and holds the GIL (`robyn/02` sync-bug; `robyn/01 §9.4` table, KNOWN). Lancelot classifies handler async/sync **at registration** (`FunctionInfo.is_async` — SYNTHESIS steal #10, `robyn/03 §0.2`, KNOWN) and routes sync → `BlockingRunner`, async → loop, with no per-request introspection.

This is the direct fix for FastAPI's #1 production AI incident pattern: the silent `CapacityLimiter(40)` cliff where the 41st blocking handler queues with no backpressure and the client just hangs (`fastapi/01 §5` line 366,378,402, KNOWN). Lancelot's pool size is explicit, configurable per-app, surfaced in `/metrics`, and overflow is an observable `503`, never a silent hang (`fastapi/01 §5` line 409, KNOWN-as-recommendation).

### 4.4a How the runtime construction changes under the free-threaded build (the dual-build, made concrete)

§4.2 shows the GIL-build runtime (one tokio MT runtime per process, N processes). The free-threaded build (§3.3) changes the *spawn topology* but **not the per-worker runtime code** — this is the property that makes dual-build "one codebase, two builds, no fork" true. Concretely (RECOMMENDATION, the code-level delta):

| Aspect | GIL build | Free-threaded build | Code delta |
|---|---|---|---|
| Top-level spawn | `for _ in range(N): fork()` + `SO_REUSEPORT` fd clone (`robyn/01 §1.2`, KNOWN) | `for _ in range(N): thread::spawn` in one process | One `if cfg!(free_threaded)` branch in the *bootstrap only*, not the hot path |
| Interpreter | N independent CPython, N GILs | 1 CPython, 0 GIL | None in Lancelot code (CPython build decides) |
| asyncio loop | 1 uvloop per process, on its own thread, `run_forever` | 1 asyncio loop **per worker thread** (Granian v2 model — KNOWN), each `run_forever` on its worker thread | `TaskLocals` becomes thread-local-per-worker instead of process-global `OnceCell`; the `scope_local` binding code is identical |
| tokio runtime | 1 MT runtime, `worker_threads=clamp(cpus,2,4)` | 1 MT runtime shared, or 1 per worker thread — measure in spike (§8) | Same `Builder` code; the `worker_threads` math is the only tuning difference |
| Shared route/const map | `Arc` per process (duplicated) | `Arc` shared across all worker threads (1 copy — memory win) | Identical code; ft just makes the `Arc` genuinely shared (must be the frozen immutable router of §3.3a) |
| `SO_REUSEPORT` | the load-balancer | not needed (in-process thread dispatch) or still used for multi-process-of-multi-thread hybrids | Bootstrap-only branch |

The hot path — accept → parse → route → const-tier → lazy proxy → with_gil/attach → detach-across-await → marshal → write (`robyn/01 §9.3`, KNOWN) — is **byte-for-byte identical** in both builds. The detach-across-await invariant (§2.2) is *build-agnostic and beneficial in both*: on the GIL build it releases the GIL so other coroutines run; on the ft build `Python::detach` releases the thread-state so the thread doesn't block "stop-the-world" GC pauses (KNOWN — https://pyo3.rs/main/performance: detach *"on free-threaded builds prevents blocking stop-the-world events"*). This is why §3.4's "no architectural fork" claim is literally true at the code level, not aspirational: the only `cfg`/runtime branches are in bootstrap (spawn topology) and the `TaskLocals` storage (process-`OnceCell` vs thread-local), ~50 lines total, zero on the request path.

### 4.5 uvloop on the Python side

**KNOWN (Robyn `pyproject.toml`, `processpool.py`, `robyn/01 Appendix A`):** Robyn installs uvloop (conditional dep) as the asyncio loop per process. uvloop = libuv-backed event loop, C-speed socket polling — *"uvloop (libuv) does that at C speed"* for N-socket polling, the actual bottleneck for many concurrent LLM awaits (`robyn/01 §7.1` line 732, KNOWN).

**RECOMMENDATION:** install uvloop as the default loop on the GIL build (verbatim Robyn). **Free-threaded caveat (INFERRED from §3.1):** uvloop's free-threaded-wheel/ft-safety status is part of the §3.4 trigger criteria — it is a C extension; if unported it forces the GIL re-enable warning under `python3.14t` (KNOWN — docs.python.org). Until uvloop ships ft wheels, the free-threaded build falls back to stdlib asyncio (slower socket polling but correct). This is a concrete, named item in the §3.4 "trigger to flip default" list, not a hand-wave. The asyncio loop runs on its own thread per worker (process or thread depending on build — §3.3), driven by `run_forever()` (`robyn/01 §3.1`, KNOWN).

### 4.6 §4 verdict

**RECOMMENDATION:** one **tokio multi-thread work-stealing** runtime per process (`worker_threads = clamp(cgroup_cpus,2,4)`, `max_blocking_threads=512` for fs/DNS only); a **separate bounded GIL-detaching `BlockingRunner`** for sync handlers (load-shed → 503, actually wired in); one **uvloop asyncio loop per worker on its own thread**; `TaskLocals` OnceCell + per-request `clone_ref` + `scope_local` bridge ported verbatim from Robyn. Work-stealing beats thread-per-core/static-sharding because AI connection load is maximally uneven and only Rust I/O is being parallelized anyway. This collapses Robyn's four schedulers to three (all-tokio + asyncio + blocking pool).

---

## 5. Stability engineering — what FastAPI/Robyn/Blazeio each get wrong, and the fix

Stability is the axis where **all three teardown targets fail in different ways**, and where Lancelot's "ops-correct for startups" wedge lives (`robyn/03 §3.5`, line 711, KNOWN). This is the longest section because it is the highest-leverage differentiator: the Rust-speed thesis is undermined by a framework that drops requests on deploy or dies on one bad input.

### 5.1 Graceful shutdown / connection draining

**The defect (KNOWN):** Robyn has **no graceful shutdown** — `process.kill()` drops all in-flight requests (`robyn/03 §3.5`, `processpool.py:55`, line 255, 692, KNOWN). On a deploy, every in-flight payment, every in-progress LLM stream, is severed. *"Startups doing payments/AI streaming cannot drop requests on deploy"* (`robyn/03:255`, KNOWN). FastAPI/uvicorn does have lifespan + graceful drain (`fastapi/01 §lifespan`, KNOWN) — its defect is elsewhere (§5.6). Blazeio has `connection_lost`/`abort_connection` per-connection cancellation (`blazeio/01 §1`, KNOWN) but no orchestrated fleet drain.

**RECOMMENDATION (the exact mechanism):**
1. Parent installs a `SIGTERM`/`SIGINT` handler → broadcasts "drain" to all workers (`robyn/03 §3.5` line 255, 692, KNOWN-as-recommendation: *"SIGTERM children, let [the server] drain with a timeout, then SIGKILL"*).
2. Each worker on "drain": stop `accept()` (close the listening fd / leave the `SO_REUSEPORT` set so the kernel routes new conns to still-live workers), keep serving in-flight requests, advertise `Connection: close` on responses, run a bounded **drain timer** (`cfg.graceful_timeout`, default 30 s).
3. hyper's `GracefulShutdown` (hyper-util `graceful` — KNOWN, https://seanmonstar.com/blog/hyper-v1/ "hyper-util contains utilities") tracks in-flight connections; `await graceful.shutdown()` resolves when all connections finish or the timer fires.
4. SSE/streaming connections: on drain, signal the producer to finish the current event then close (PEP-789-correct stream teardown — SYNTHESIS steal #9, `fastapi/03 §0.7-0.9`, KNOWN); do not hard-cut mid-token.
5. After drain timer: `SIGKILL` the worker. Then parent exits.
6. Run shutdown lifespan handlers (the merged async-CM chain — `fastapi/01 §lifespan`, SYNTHESIS steal #17, KNOWN) so DB pools / LLM clients close cleanly *after* the last request, not before (the yield-deps + exit-stack ordering — SYNTHESIS §4.4, KNOWN).

### 5.2 Worker supervision & respawn (the `panic=abort` corollary)

**The defect (KNOWN, and it is *fatal* combined with the build profile):** Robyn has **no crashed-worker supervisor** (`robyn/03 §3.5`, line 257, 693, KNOWN). The build profile is `panic = "abort"` (Robyn `Cargo.toml:48`, `robyn/01 §0`, KNOWN) — *"a Rust panic does NOT unwind; it abort()s the whole OS process"* (`robyn/01 §0`, KNOWN). So **one panicking request permanently removes a worker from the fleet, forever, with no replacement** (`robyn/03 §3.5` line 626, 693, KNOWN: *"a panic kills a worker permanently"*). Over time the fleet bleeds out to zero. This is the single most dangerous Robyn defect for production.

**RECOMMENDATION:**
1. **Parent supervisor loop:** poll `process.is_alive()`; a dead child → immediately re-`spawn_process` a replacement with a fresh `SO_REUSEPORT` fd clone (`robyn/03 §3.5` line 257, 693, KNOWN-as-recommendation; `robyn/01 §9.4` table: *"ADD: respawn-on-crash (Robyn doesn't)"*). Cap restart rate (exponential backoff, e.g. ≤5 restarts/30 s/worker) to avoid a crash-loop pinning the box; after the cap, mark the worker permanently failed and alert (don't busy-restart a deterministic crash).
2. **`catch_unwind` at the FFI seam** (`robyn/01 §0` line 92, §10 honorable-mention 4, KNOWN-as-recommendation; Robyn does NOT do this and *"is fragile here"*). Keep `panic="abort"` for the *non-FFI* hot path (no landing pads = faster/smaller — `robyn/01 §0`, KNOWN), but wrap the **Python call boundary only** in `std::panic::catch_unwind`. A panic *inside pyo3 marshalling* (a `Py<T>` misuse, a poisoned `parking_lot` lock) becomes a `500`, not a worker kill (`robyn/01 §0` line 92, KNOWN: *"pyo3 itself converts Python exceptions to PyErr (not panics), but a Py<T> misuse or a poisoned lock will panic — catch_unwind at the FFI seam is the safety net Robyn lacks"*). Note: with `panic="abort"`, `catch_unwind` does NOT catch (abort is not unwind) — so the FFI-seam crate/boundary must be compiled allowing unwind for that scope, or use the `-C panic=unwind` for the dylib + abort discipline elsewhere; the practical pattern is **zero-`unwrap()` discipline on the request path** (`robyn/01 §0`, §10 hon-4, KNOWN: *"every unwrap()/expect() on the request path is a process-kill bug"*) **plus** the supervisor as the backstop. RECOMMENDATION: ship `panic="abort"` *only with* the supervisor (`robyn/03 §3.5` line 639, KNOWN-as-recommendation: *"only ship panic=abort with the worker-supervisor so an aborting panic self-heals"*) + a clippy `deny` on `unwrap`/`expect`/`panic!`/`unreachable!`/indexing on the request-path modules.
3. cgroup-aware CPU count for the worker count (Robyn's `os.cpu_count()` ignores cgroup quotas — `robyn/03 §3.5` line 803, `robyn/01 §9.2`, KNOWN AVOID) → never over-spawn in a container.
4. No `input()` on port conflict (Robyn blocks on `input()` when the port is in use — `robyn/03 §3.5` line 589, 695, KNOWN: *"bad for containers"*, hangs a container forever) → fail fast with a clear error + non-zero exit.

### 5.3 Slow-loris / header-receipt timeout defense

**The defect (KNOWN, two of three targets):**
- Blazeio: the `while b"\r\n\r\n" in buff` parser **re-scans from byte 0 on every recv** → O(n·k) under fragmented headers; the 102400-byte cap bounds memory but **there is no per-connection request-header receipt deadline** (`blazeio/01 §4.2` line 370, §8 line 751, 818, KNOWN). *"an attacker dribbling request headers one byte per second … the connection is held hostage … This is a real DoS surface and a strong Rust-core argument"* (`blazeio/01 §8` line 751, KNOWN). This is *the* core justification for Lancelot's Rust core (`blazeio/01 §4.2`, SYNTHESIS line 102, KNOWN).
- Robyn: `client_request_timeout(Duration::from_secs(0))` — **the request-read timeout is disabled entirely** (`robyn/01 §3.1` line 397, KNOWN). *"a slowloris attacker can hold a connection forever with no framework-level defense"* (`robyn/01 §3.1`, KNOWN). The teardown's own recommendation: *"Lancelot should make this configurable with a sane default (e.g. 30 s header timeout) and a documented 0-to-disable for streaming"* (`robyn/01:397`, KNOWN-as-recommendation).

**RECOMMENDATION:**
1. **Single-pass resumable Rust parser with a header-receipt deadline.** hyper's HTTP/1 parser already scans each byte once with a resumable state machine (it does not re-scan from 0 — this is the structural win over Blazeio's pure-Python `bytes.find`, `blazeio/01 §4.2`, KNOWN). On top, set a **header timeout**: from first byte of a connection's request to full header receipt, default **30 s**, configurable, `0` = disable (documented, for streaming-request-body endpoints) — exactly the teardown's prescription (`robyn/01 §3.1`, KNOWN-as-recommendation). Mechanism: `tower-http::timeout` / `hyper` `http1::Builder::header_read_timeout` (KNOWN — hyper 1.x exposes header_read_timeout) wrapping the connection serve future.
2. Separate **body-read timeout** and **total-request timeout** (distinct from header timeout — a slow body upload is different from slow-loris headers). Defaults: header 30 s, body 60 s, total 120 s; all overridable per-route (streaming/upload routes set body/total to 0).
3. **Connection cap + accept backpressure** (Granian's model — KNOWN, https://github.com/emmett-framework/granian: *"a secondary backlog … limit connections … interrupts the accept loop in case too many requests are waiting"*). A per-worker max-concurrent-connections; when exceeded, stop accepting (the kernel `SO_REUSEPORT` queue + `listen(16384)` backlog absorbs the burst — `robyn/01 §1.2`, KNOWN — or other workers take it). This bounds the per-connection memory cost of a slow-loris swarm to `cap × (parser state + buffer)`, which in Rust is tiny vs Blazeio's `task + 100 KiB + Future` per connection (`blazeio/01 §8` line 751, KNOWN).

### 5.4 Backpressure end-to-end (the AI hot path)

**The defect (KNOWN):** Robyn's streaming pumps a sync generator with `spawn_blocking` + `with_gil` **per token** + no producer↔writer bounded channel (`robyn/01 §7.2`, §10 pitfall 3, KNOWN — *"Robyn's worst weakness for AI workloads"*). FastAPI's SSE architecture is correct (decoupled producer + bounded channel + keepalive timer that wraps the *channel receive*, not the generator's `__anext__` — PEP-789 — `fastapi/03 §0.7-0.9`, SYNTHESIS steal #9, KNOWN) but its sync-generator path does one thread hop per chunk via `iterate_in_threadpool` (`fastapi/01 §5.3` line 385,392, KNOWN — *"catastrophic"*). Blazeio's backpressure is the *correct model* (pause-by-default receive; boolean send-backpressure gate, no Future/token; `buffer_overflow_manager` fused into `write`; 10 MiB/0 hysteresis — `blazeio/01 §3`, §8, SYNTHESIS steal #8, KNOWN) but pure-Python-ceilinged (GIL-serialized Future churn at thousands of streams — `blazeio/01 §8` line 749, KNOWN).

**RECOMMENDATION (the synthesis, end-to-end):**
- **Receive side:** Rust core owns a pooled refcounted `BytesMut`, pause-by-default / demand-pumped intake (Blazeio's model in Rust — `blazeio/01 §0.2,§8`, SYNTHESIS steal #8, KNOWN). Python never sees raw segments for normal requests (Robyn's model); streaming request bodies get an async-iter with backpressure (`blazeio/01:177` DESIGN-NOTE, KNOWN).
- **Send side / SSE:** native **async-generator** pump via `into_future` (each token's `await llm.stream()` is a real GIL-released asyncio await, NOT a blocked pool thread — `robyn/01 §7.3.1`, KNOWN); **coalesce** tokens time+size (~25 ms / 2 KiB — SYNTHESIS §4.5, *not* Blazeio's 30 KiB size-only which holds thousands of tokens — SYNTHESIS line 147, KNOWN) → 1 GIL/flush not 1 GIL/token (10–50× GIL-traffic cut — `robyn/01 §7.3.2`, KNOWN); **bounded SPSC ring** (size-1 default = lowest latency + backpressure straight onto the model — SYNTHESIS §4.5, KNOWN) between the Python producer and the Rust writer; boolean send-backpressure gate, no Future/token (Blazeio — `blazeio/01 §3`, SYNTHESIS steal #8, KNOWN); Rust-side SSE framing (`data: %b\n\n`) — no per-token Python string build (`robyn/01 §7.3.4`, KNOWN).
- **The full chain:** slow client → transport write buffer fills → ring fills → producer coroutine parks → it stops pulling from the LLM → bounded server RAM **regardless of completion length** (SYNTHESIS steal #8 line 67, KNOWN). This is the property FastAPI, Robyn, and pure-Python-ceilinged Blazeio each only partially achieve; Lancelot must achieve it whole.
- **Client-disconnect → cancel → abort upstream LLM** (don't keep paying for tokens nobody reads — SYNTHESIS §4.5, `robyn/01 §7.3.5`, KNOWN): wire hyper's connection-closed signal → cancel the producer coroutine → abort the outbound LLM request (the `PyFutureAwaitable` cancel path — `robyn/01 §6.4`, KNOWN).

### 5.5 Request timeouts, load shedding, memory bounds, structured cancellation

| Concern | Defect in the corpus (KNOWN) | Lancelot RECOMMENDATION |
|---|---|---|
| **Request timeout** | Robyn disables it (`client_request_timeout(0)` — `robyn/01 §3.1`, KNOWN); Blazeio relies on TCP keepalive only (`blazeio/01 §8`, KNOWN) | Per-route total/header/body timeouts (§5.3); default total 120 s; streaming routes opt to 0 explicitly. `tower-http::timeout` layer + hyper header_read_timeout. |
| **Load shedding** | FastAPI: 41st sync handler silently queues, client hangs, no signal (`fastapi/01 §5` line 366,378, KNOWN); Robyn unbounded sync queue (`robyn/01 §5`, KNOWN) | Bounded sync queue + connection cap → **`503` + `Retry-After` emitted by Rust before spawning the Python frame** (`fastapi/01 §5` line 402,409, KNOWN-as-recommendation). Observable in `/metrics`, never a silent hang. |
| **Memory bounds** | Blazeio: fixed 100 KiB/conn + GIL-serialized Future churn ceilings it at thousands of streams (`blazeio/01 §8` line 749,758, KNOWN); Robyn: eager full-body buffer even if unread, ~30–66 allocs/req (`robyn/02 §0`, SYNTHESIS line 94, KNOWN) | Rust pooled/refcounted buffers; lazy request proxy (nothing materialized until touched — SYNTHESIS steal #4, KNOWN); bounded streaming ring; max-body-size limit (`tower-http::limit::RequestBodyLimit`, default e.g. 16 MiB, per-route override for uploads). Per-request framework alloc target ~1–3 (SYNTHESIS line 136, KNOWN). |
| **Structured cancellation** | Robyn: per-token spawn_blocking has no clean cancel (`robyn/01 §7.2`, KNOWN); Blazeio: `reTask` loses contextvars/structured-cancel/`all_tasks()` visibility (`blazeio/03 §0.1`, SYNTHESIS steal #13 reject-clause, KNOWN) | Per-request `contextvars.Context` (SYNTHESIS steal #14, `robyn/01 §4`, KNOWN — also the tenant-isolation fix, issue #1380); client-disconnect cancels the handler task which cancels its children (asyncio structured cancellation via real `Task`s, NOT `reTask` — `blazeio/03 §0.1`, KNOWN). Require Python ≥3.11 for `create_task(context=)` (`robyn/01 §4` line 506, SYNTHESIS §4.1, KNOWN). |
| **Cross-request `ContextVar` leak** | Robyn shipped this bug (tenant A's auth bleeding into tenant B on a reused pool thread), fixed in #1380 (`robyn/01 §4`, §10 hon-6, KNOWN) | One fresh `contextvars.Context` per request, threaded through before/handler/after; sync handlers via `ctx.run(...)`, async via `create_task(coro, context=ctx)` — verbatim Robyn `asyncio.rs` (`robyn/01 §4`, §9.4 "Port verbatim", KNOWN). Must be correct from day one — *"a tenant-isolation defect in a multi-tenant AI SaaS"* (`robyn/01 §10` hon-6, KNOWN). |
| **`panic=abort` fragility** | Robyn: any request-path `unwrap()` kills a worker, no respawn (`robyn/01 §10` hon-4, KNOWN) | §5.2: zero-unwrap clippy-deny on request-path modules + `catch_unwind` at FFI seam + supervisor respawn. |

### 5.6 FastAPI's specific stability failures (cross-ref, since Lancelot keeps its ergonomics)

Lancelot adopts FastAPI's ergonomics moat (SYNTHESIS Tier-S #1, the registration-time compiler) so it must NOT inherit FastAPI's stability traps (`fastapi/01 §5`, SYNTHESIS §3, KNOWN):
- **The `CapacityLimiter(40)` silent sync cliff** (`fastapi/01 §5` line 366, KNOWN) → §4.4 + §5.5 (explicit sized pool, 503/Retry-After).
- **`BaseHTTPMiddleware`/`@app.middleware("http")`**: a task group + 2 memory streams + a cached request *per request* (`fastapi/01 §1` line 13, SYNTHESIS §3 AVOID, KNOWN) → pure-Rust/tower or pure-ASGI middleware only; never a `call_next` dispatch model.
- **`BackgroundTasks` run sequentially inside the request coroutine after body send** — blocks connection release; "return fast, work later" is silently broken (`fastapi/01 bonus`, SYNTHESIS §3 AVOID, KNOWN) → Rust-owned detached task pool, connection released immediately.
- **`iterate_in_threadpool` one-thread-hop-per-chunk** for sync stream generators (`fastapi/01 §5.3`, KNOWN) → reject sync generators for streaming with a clear error ("use `async def` + `yield`"), or drain in *one* dedicated thread pushing an async queue (`fastapi/01 §5.4` line 403, KNOWN-as-recommendation).

### 5.7 §5 verdict — the stability checklist (the deliverable)

Every item is a defect *observed* in the corpus (KNOWN) with the fix (RECOMMENDATION). This is the production-stability gate for Lancelot:

1. **Graceful shutdown:** SIGTERM → stop accept → drain in-flight (bounded `graceful_timeout`, default 30 s) → SSE finish-current-event → run shutdown lifespan → SIGKILL. *(fixes Robyn `processpool.py:55`)*
2. **Worker supervisor:** parent respawns dead children, rate-capped backoff, alert on permanent failure. *(fixes Robyn `robyn/03 §3.5`)*
3. **`panic=abort` only with supervisor + zero-unwrap clippy-deny on request path + `catch_unwind`/unwind-scope at the FFI seam.** *(fixes Robyn `robyn/01 §0,§10`)*
4. **Header-receipt deadline** (default 30 s, `0`=disable for streaming) + separate body/total timeouts. Single-pass resumable Rust parser (hyper). *(fixes Blazeio `§4.2` slow-loris, Robyn `client_request_timeout(0)`)*
5. **Bounded sync-handler pool + connection cap → `503 Retry-After` from Rust before the Python frame.** *(fixes FastAPI `CapacityLimiter(40)`, Robyn unbounded queue)*
6. **End-to-end streaming backpressure:** pause-by-default receive, async-gen pump, coalesced flush, bounded SPSC ring, boolean send-gate, client-disconnect→cancel→abort-LLM. *(fixes Robyn per-token, FastAPI sync-gen hop, Blazeio pure-Python ceiling)*
7. **Per-request `contextvars.Context`** for tenant isolation + structured cancellation; Python ≥3.11. *(fixes Robyn pre-#1380 leak)*
8. **Max-body-size limit**, lazy request proxy (~1–3 allocs/req), pooled Rust buffers. *(fixes Robyn eager marshalling, Blazeio fixed-100 KiB)*
9. **cgroup-aware worker count**; **no `input()`** on port conflict (fail fast, non-zero exit). *(fixes Robyn `os.cpu_count()`, `input()` container hang)*
10. **No `BaseHTTPMiddleware`-style `call_next` dispatch; no sequential post-response BackgroundTasks; no per-chunk sync-gen thread hop.** *(fixes FastAPI `fastapi/01 §1,§5,bonus`)*
11. **Detach-across-await invariant enforced** (clippy `await_holding_lock` + review) — a *correctness*-and-stability invariant (holding the GIL across an await freezes the whole interpreter — `robyn/01 §10` pitfall 1, KNOWN).
12. **Health/readiness split:** liveness (process up) ≠ readiness (accepting + sync-pool not saturated + drain not in progress) — so the load balancer stops routing to a draining/saturated worker (INFERRED, standard, none of the three do this).

---

## 6. WebSocket, HTTP/2, HTTP/3 — protocol coverage decisions

**HTTP/1.1 + HTTP/2: mandatory, both via hyper.** hyper 1.x ships both first-class over your IO/runtime (KNOWN — https://seanmonstar.com/blog/hyper-v1/). Use `hyper-util`'s `auto` builder (h1/h2 negotiated by ALPN/upgrade). HTTP/2 is the **default for streaming endpoints** (`robyn/01 §7.3.6`, KNOWN-as-recommendation): N concurrent SSE/LLM streams from one client multiplex over one h2 connection vs hitting the browser's 6-connection HTTP/1.1 limit (`robyn/01 §7.3.6`, KNOWN).

**WebSocket: `tokio-tungstenite`, NOT an actor system.** Robyn implements WS as an actix actor per connection (`WebSocketConnector`) + a per-endpoint `WebSocketRegistry` actor + a `tokio::sync::mpsc::unbounded_channel` carrying inbound frames to Python as `String` (`robyn/02 §7.1,§7.2`, KNOWN). Defects to fix (KNOWN, `robyn/02 §7.2`): the channel is **unbounded** (*"a fast client can OOM the channel"* — KNOWN), binary frames are coerced to UTF-8 (no raw-bytes path — KNOWN), WS routing is exact-string only (no `:id` params — `robyn/02 §2.5`, KNOWN). RECOMMENDATION: `tokio-tungstenite` over the hyper upgrade (zero actor machinery — the §1.2 reason actix is rejected); a **bounded** mpsc with backpressure (drop-or-close policy on overflow, configurable); a raw-`bytes` path to Python alongside text; `matchit` WS routing with path params (`robyn/02 §2.5` line 166, KNOWN-as-recommendation — *"agent chat backends frequently want /ws/conversations/:id"*).

**HTTP/3: defer, but keep the door open (not a v1 blocker).** hyper's `h3` crate is generic over any QUIC impl, `h3-quinn` binds Quinn; reqwest has unstable h3, hyper plans to surface it (KNOWN — https://github.com/hyperium/h3; https://seanmonstar.com/blog/hyper-v1/). Granian's roadmap lists HTTP/3 as "eventually 3", not shipped (KNOWN — https://github.com/emmett-framework/granian). RECOMMENDATION: **HTTP/3 is a post-v1 feature.** The hyper+tower choice (§1.1) is what *keeps the door open* — `h3`/`h3-quinn` are in the hyper ecosystem; an actix or non-hyper base would make this a rewrite. No HTTP/3 work in v1; design the connection-acceptor abstraction so a QUIC listener can be added as a parallel acceptor later (INFERRED, low-cost forward-compat).

---

## 7. The decisive recommendation (answers SYNTHESIS §4.8 Q1 + the nogil question)

### 7.1 The exact Rust stack + versions (RECOMMENDATION — pinned)

```toml
[dependencies]
pyo3                = { version = "0.27", features = ["extension-module", "py-clone"] }   # 0.28-clean idiom (§2.1); pin minor
pyo3-async-runtimes = { version = "0.27", features = ["tokio-runtime"] }                  # only sanctioned asyncio↔tokio bridge
tokio               = { version = "1", features = ["full"] }                              # multi-thread work-stealing
hyper               = { version = "1", features = ["http1", "http2", "server"] }          # 3-yr stability promise; NOT actix
hyper-util          = { version = "0.1", features = ["server", "server-auto", "tokio"] }  # auto h1/h2 + graceful shutdown
http-body-util      = "0.1"                                                               # streaming body (AI hot path)
tower               = { version = "0.5", features = ["util"] }                            # middleware contract
tower-http          = { version = "0.6", features = ["timeout","limit","trace","cors","compression-gzip"] }
tokio-tungstenite   = "0.24"                                                              # WS, no actor system
socket2             = { version = "0.5", features = ["all"] }                             # SO_REUSEPORT — verbatim Robyn
matchit             = "0.8"                                                               # radix-trie router (axum's; Robyn 0.7)
crossbeam-channel   = "0.5"                                                               # BOUNDED sync-pool queue (Robyn unbounded = bug)
dashmap             = "6"
parking_lot         = "0.12"                                                              # replaces removed GILProtected
once_cell           = "1"
anyhow              = "1"

[lib]
crate-type = ["cdylib", "rlib"]

[profile.release]
codegen-units = 1
lto           = "fat"
panic         = "abort"     # ONLY with the worker supervisor + zero-unwrap clippy-deny on request path (§5.2)
strip         = true
opt-level     = 3
```

### 7.2 The stack recommendation in 6 bullets

1. **HTTP core: `hyper 1.x` + `hyper-util` + `http-body-util` + `tower 0.5`/`tower-http 0.6`** — chosen for hyper's 3-year semver-stability promise and full runtime control, NOT for raw throughput (throughput is GIL-capped for a Python framework — `robyn/01 §8`, KNOWN). Externally validated by Granian (the nearest competitor) independently choosing hyper+tokio. **actix-web is the schedule-hedge fallback only; axum is the reference implementation and second fallback; ntex/xitca/may_minihttp/glommio/monoio all rejected with cause (§1.4–1.6).**
2. **Runtime: one `tokio` multi-thread *work-stealing* runtime per process** (`worker_threads = clamp(cgroup_cpus, 2, 4)`, `max_blocking_threads = 512` for fs/DNS only) — work-stealing beats thread-per-core/static-sharding because AI connection load is maximally uneven and only Rust I/O is parallelized anyway (`robyn/01 §3.4`, KNOWN).
3. **Scaling: N OS processes via `SO_REUSEPORT` (verbatim Robyn `shared_socket.rs`) + uvloop asyncio loop per worker + the `TaskLocals` OnceCell/`scope_local` bridge ported verbatim** — plus a **separate bounded GIL-detaching `BlockingRunner`** for sync handlers, actually wired into the HTTP path (Robyn's exists but isn't — `robyn/02` sync-bug, KNOWN), load-shedding to `503 Retry-After`.
4. **FFI: `pyo3 0.27` floor, written in the 0.28-clean idiom** (no `GILProtected`, `detach`-named GIL-release wrapper, `Python<'py>`≠GIL-held) — `Bound` everywhere, `cast` not `extract` on the hot path, Rust-tuple `vectorcall` args, zero-copy `Bytes`/buffer-protocol both directions, the **detach-across-await invariant enforced by clippy** (the one non-negotiable rule — `robyn/01 §10` pitfall 1, KNOWN).
5. **Packaging: dual-build — `abi3-py311` wheels (1/platform) for the GIL build + `cp313t`/`cp314t` wheels for the free-threaded build** (forced: abi3 is incompatible with the free-threaded ABI until PEP 803 `abi3t` at CPython 3.15+ — KNOWN). Build profile verbatim Robyn + `panic=abort` *only with* the supervisor.
6. **Stability: the 12-point §5.7 checklist is a release gate** — graceful drain, worker respawn, header-receipt deadline, bounded pool + 503, end-to-end streaming backpressure, per-request contextvars, max-body-limit, cgroup-aware count, no `input()`, no `BaseHTTPMiddleware`/sequential-BackgroundTasks/per-chunk-sync-hop, the detach-across-await invariant, liveness≠readiness. Each fixes a *specific KNOWN defect* in Robyn/Blazeio/FastAPI.

### 7.3 The nogil verdict (decisive — answers the architecture-defining question)

**Multiprocess-first, free-threaded-clean, dual-build. The multiprocess model is NOT obsolete and is the production default through ~CPython 3.15 (2026–2027).**

- **Why not nogil-first:** free-threading's headline 8× win is on **CPU-bound** code (KNOWN — FastAPI ~4→~32 RPS); Lancelot's headline workload is **IO-bound** `await llm(...)` where free-threading buys **~nothing** (KNOWN — IO endpoints unchanged GIL vs no-GIL, because the GIL is already released across the await — `robyn/01 §7.1`, KNOWN). Add: 5–10% single-thread tax, 15–20% memory, ~50% of top packages still lacking ft wheels (Feb 2026, KNOWN), the **silent GIL-re-enable on any unported C-extension import** (KNOWN — docs.python.org) which makes an nogil-first failure mode *worse than the multiprocess baseline*, and the abi3↔ft incompatibility that sacrifices the 1-wheel/platform packaging thesis. Five independent reasons, all KNOWN-sourced.
- **Why free-threaded-clean from day one:** the transition is real and Lancelot will outlive the GIL default; retrofitting thread-safety into a core that assumed `Python<'py>⇒GIL-held` is a rewrite, while designing clean now (declare `gil_used=false` post-audit, no `GILProtected`, explicit synchronization on every shared structure, detach-across-await framing, `python3.14t` in CI) is nearly free.
- **The dual-build model (Granian-proven, KNOWN):** GIL build → N processes (`SO_REUSEPORT`); free-threaded build → N threads, 1 shared interpreter (each with its own asyncio loop on its worker thread). Same Rust hot path, no architectural fork. The free-threaded build's killer feature for AI: a large model loaded **once and shared across threads** instead of duplicated per process — document it as the choice for large-shared-model and CPU-handler-heavy apps.
- **Trigger to flip the default to free-threaded (explicit, measurable — re-evaluate each CPython release):** (a) Lancelot's full runtime dep closure (uvloop-or-replacement, pydantic-core, orjson) ships ft wheels and is ft-safe; (b) single-thread penalty <5%; (c) PEP 803 `abi3t` ships (restores 1-wheel/platform); (d) free-threaded is the CPython default. Until all four hold, multiprocess is the default. This is a *deferred decision with criteria*, not a guess.

**One sentence:** *Lancelot ships multiprocess + abi3 + GIL-CPython as the safe, ecosystem-ready, best-packaged default, is engineered free-threaded-clean so the no-GIL build is a build flag and not a rewrite, and flips the default to free-threaded only when four explicit measurable criteria are met — so the multiprocess model is a deliberate current default, not technical debt.*

---

## 8. Open items handed forward (not blockers for this decision)

- SYNTHESIS §4.8 Q2 (how far DI/validation moves into Rust vs codegen'd Python vs FFI-to-pydantic-core) — separate design doc; orthogonal to the HTTP-stack/nogil decision resolved here.
- SYNTHESIS §4.8 Q3 (ASGI middleware contract vs Rust-native middleware ABI) — the hyper+tower choice here *enables both* (tower for Rust-native; an ASGI shim atop tower for ecosystem); the tier split is a middleware-design doc.
- SYNTHESIS §4.8 Q4/Q5 (const-tier complexity; streaming ring size / keepalive interval / coalescing thresholds) — set by benchmark in the spike, not by guess (SYNTHESIS line 163, KNOWN-as-recommendation). This doc fixes the *architecture* of the streaming path (§5.4); the *constants* are a calibration task.
- Spike to run before first line of core code: hyper+tower vs actix const-tier + 1k-concurrent-SSE-stream microbench under `cp314` and `cp314t`, to confirm §1.8 and quantify the free-threaded streaming delta for the §7.3 trigger.

---

## Sources

External (2026 facts):
- PyO3 free-threading guide — https://pyo3.rs/v0.28.3/free-threading
- PyO3 performance guide — https://pyo3.rs/main/performance
- PyO3 migration/changelog — https://pyo3.rs/main/migration
- pyo3-bytes (buffer-protocol Bytes) — https://docs.rs/pyo3-bytes/latest/pyo3_bytes/
- CPython free-threading HOWTO — https://docs.python.org/3/howto/free-threading-python.html
- What's new in Python 3.14 — https://docs.python.org/3/whatsnew/3.14.html
- PEP 779 (supported status for free-threaded) — https://peps.python.org/pep-0779/
- PEP 703 (optional GIL) — https://peps.python.org/pep-0703/
- PEP 803 (abi3t — stable ABI for free-threaded) — https://peps.python.org/pep-0803/ ; packaging thread — https://discuss.python.org/t/pep-803-stable-abi-for-free-threaded-builds-packaging-thread/104976/108
- Free-threaded wheels tracker — https://hugovk.github.io/free-threaded-wheels/ ; Quansight "halfway" — https://labs.quansight.org/blog/free-threaded-python-halfway
- Astral Python 3.14 — https://astral.sh/blog/python-3.14
- hyper v1 stability — https://seanmonstar.com/blog/hyper-v1/ ; hyper h3 — https://github.com/hyperium/h3
- TechEmpower Round 23 — https://www.techempower.com/blog/2025/03/17/framework-benchmarks-round-23/ ; JSON serializer discussion — https://github.com/TechEmpower/FrameworkBenchmarks/discussions/8634
- actix-web vs axum 2026 — https://medium.com/@abhinav.dobhal/actix-web-vs-e1e019714542
- glommio/monoio maintenance + thread-per-core (Apache Iggy) — https://iggy.apache.org/blogs/2026/02/27/thread-per-core-io_uring/ ; monoio benchmarks — https://github.com/bytedance/monoio/blob/master/docs/en/benchmark.md ; may_minihttp — https://users.rust-lang.org/t/opinion-of-benchmarks-actix-vs-may-minihttp-vs-xitca-web/118039
- Granian — https://github.com/emmett-framework/granian ; Python app servers 2026 — https://www.deployhq.com/blog/python-application-servers-in-2025-from-wsgi-to-modern-asgi-solutions
- FastAPI no-GIL benchmark — https://medium.com/@kevaldekivadiya2415/python-gil-vs-no-gil-real-fastapi-benchmarks-with-free-threaded-python-3-13-b5751f8d57a2 ; Meta near-linear scaling — https://medium.com/@backendbyeli/the-end-of-pythons-gil-free-threaded-python-arrives-578313f4e5f8

Internal teardowns (grounding):
- `research/robyn/01-rust-core-runtime-and-gil.md` — §0 crate stack/build profile, §1 process/socket model, §2 GIL choreography, §3 runtime topology, §4 contextvars, §5 blocking pool, §6 custom awaitables, §7 streaming, §8 cross-framework axis, §9 blueprint, §10 pitfalls
- `research/robyn/02-rust-routing-types-websockets.md` — §0 const-router, §2.5/§7 WebSockets, lazy-proxy fix
- `research/robyn/03-python-api-pyo3-bridge.md` — §3.5 no-graceful-shutdown/no-supervisor/`input()`/cgroup, `FunctionInfo` ABI, packaging
- `research/blazeio/01-protocol-server-core.md` — §3 write backpressure, §4.2 O(n·k) header re-scan / slow-loris, §8 AI streaming ceiling
- `research/fastapi/01-routing-and-request-lifecycle.md` — §5 `CapacityLimiter(40)` cliff / `iterate_in_threadpool` / 503-recommendation, §1 `BaseHTTPMiddleware`, BackgroundTasks
- `research/SYNTHESIS.md` — §2 steal-list, §3 anti-patterns, §4 blueprint, §4.8 open questions
