# R1 + Task#7 — Build & Benchmark Plan

*Synthesis of 5 engineering panels (rust-impl / framework-arch / bench-method / skeptic-benefit / positioning-maint), reconciled against the live source on `m17-deferred-wins`. Every architectural claim below was re-verified by a fresh read; where a panel cited a fact wrong, it is flagged inline.*

---

## Item R1 — GIL-releasing gzip (Rust under `allow_threads`, Python ASGI seam)

### 1. Panel tally + consensus

| Panel | Verdict |
|---|---|
| Rust/pyo3 systems | **BUILD-IF-BENCH** (high) |
| Framework API architect | **BUILD-IF-BENCH** (medium) |
| Benchmark methodologist | **BUILD-IF-BENCH** (medium) |
| Skeptic | **SKIP** (high) |
| Positioning + maintenance | **BUILD-IF-BENCH** (medium) |

**Consensus: 4 BUILD-IF-BENCH / 1 SKIP → BUILD-IF-BENCH with a hard, high bar + mandatory maintainer sign-off.** The skeptic's SKIP is *not* an outlier to dismiss — it is the correct prior, and it converges with the four others on the operational truth: **the bench is the entire decision; the expected terminal state is REVERT unless a concurrency cliff shows up.** All five agree on every load-bearing fact (the GIL argument is genuinely new; the audience is narrow; it reopens a documented Tier-D decision). They differ only on whether to *run the experiment* (4 yes) or *predict its failure and skip* (1). Running it is cheap (~8-12h) and produces a durable, citable answer to a question the project will otherwise keep relitigating — so build-then-bench, but pre-commit to deleting it.

The four pre-existing panel docs corroborate this exactly: the absorb-mine marks R1 **NEEDS_BENCH + sign-off** (`docs/plans/2026-05-31-fastapi-rust-absorb-mine.md:21-27`), and the deferred-candidates panel returns **MAYBE — bench-gate + maintainer sign-off, 4 MAYBE / 1 SKIP-leaning** (`docs/plans/2026-05-31-deferred-candidates-panel.md:72-76`).

### 2. Recommended implementation approach

**Where it lives:** Python ASGI seam *only*. This is not a preference — it is forced by architecture and is the single most important fact about R1:

- `lancelot/app.py:915` — `run()` forwards only `[(method, path, handler)]` to `_native.run`. **It doesn't even forward `const_routes`** (verified: the const tier is unreachable via the public `App.run()`).
- `src/service.rs:178-286` — `handle()` collects the body to `Bytes` (`:242`) and emits a *finished* `Bytes` via `json_ok` (`:168-176`) with **zero `accept-encoding` read anywhere in `src/`**. Grep for `negotiat` in `service.rs` returns exactly one hit: the h2c comment at `:377`. There is **no post-serialization `send`/negotiation seam** on the native server.

So the native server (the marquee 183k path) **cannot host gzip at all.** This is the skeptic's sharpest point (notes: "the new GIL insight lands on the wrong surface") and it is verified-true. R1 helps only the uvicorn-hosted `lancelot.App` path — which is precisely where Starlette's `add_middleware(GZipMiddleware)` already works.

**Python side** — new opt-in `lancelot/compress.py` (confirmed absent today). A pure-ASGI responder (NEVER `BaseHTTPMiddleware` — cardinal rule), wired via the **existing `app.add_middleware(Cls, **opts)` pattern** (`app.py:215-220`), matching the just-merged M17 house style (`ratelimit.py`, `auth.py`). **Reject the `App(compress=)` constructor kwarg** the absorb-mine floated (`fastapi-rust-absorb-mine.md:24`) and the framework panel correctly killed: a constructor flag would imply the native `app.run()` server compresses, which it provably cannot — it would lie about where the feature works. Mirror Starlette's `GZipResponder` state machine exactly (verified `.venv/.../starlette/middleware/gzip.py`) to inherit its correct exclusions for free:
- `:24` require `gzip` in `Accept-Encoding`;
- `:55` skip if `content-encoding` already set;
- `:56` skip `text/event-stream` (`DEFAULT_EXCLUDED_CONTENT_TYPES`, `:8`);
- `:66` skip if body `< minimum_size`;
- on compress, set `content-encoding: gzip` + `vary: accept-encoding` + recomputed `content-length` (`:74-79`).

**Scope discipline:** one-shot buffered bodies only. **Do NOT stream-compress** (no per-chunk `GzipFile` — that is the documented Tier-D per-chunk anti-pattern *and* would kill TTFT, the AI-streaming UX metric). Streaming/`more_body=True` responses pass through identity. This also keeps the Rust function stateless (~40 lines, no streaming-state machine).

**The Rust piece** — new `src/compress.rs`, one `#[pyfunction] gzip_encode(py, data: &[u8], level: u8) -> Py<PyBytes>`. Codec: **prefer `libdeflater`** (single-shot, `gzip_compress_bound` for one pre-sized allocation, ~2-3× zlib per-byte) with `flate2`/miniz_oxide (pure-Rust, no C dep) as the fallback. Both are **Rust-side deps → cardinal-rule-3 clean** (they never enter `import lancelot`'s Python runtime). Cargo has neither today (verified: `grep flate2|libdeflater|gzip Cargo.toml` is empty; miniz is only a transitive backtrace dep).

**The two load-bearing soundness hazards (one panel got the API wrong — correct it):**

- **HAZARD A — the GIL release helper does not exist yet, and "the codebase wraps `py.detach`" is FALSE.** The rust-impl panel claims (notes) "the codebase wraps `attach()` and the GIL-release equivalent is `py.detach` — use detach." **Verified wrong.** `src/gil.rs:27-30` explicitly states: *"(No `detach` wrapper: the GIL is released structurally — every `.await` sits OUTSIDE a `with_gil` scope... An explicit `py.detach` marker would imply a release pattern this crate doesn't use, so it's intentionally absent.)"* Lancelot has **zero explicit GIL-release call sites** today; the `serialize.rs:79` comment ("pyo3 already detached the GIL") refers to pydantic-core releasing it *internally*, not a Lancelot call. R1 would introduce the framework's **first** explicit `py.detach(|| ...)` (pyo3 0.27.2 deprecates `allow_threads`; `detach` is the live name). This is sound and fine — but it is *new surface*, not "reusing an existing wrapper." Note it in the PR.

- **HAZARD B — the off-loop hop is MANDATORY or the entire thesis is void (all panels agree, it is the crux).** `py.detach` releases the GIL *for other OS threads*. A single uvloop event loop is single-threaded — calling `gzip_encode` directly in a coroutine releases the GIL **to nobody** (every other coro on that loop is blocked at the same `await`). The win materializes **only** when the encode runs off-loop: `await loop.run_in_executor(None, _native.gzip_encode, body, level)` so a real thread-pool thread takes the freed GIL. The middleware MUST use `run_in_executor` (or a free-threaded 3.13t build). If a fresh engineer wires the encode inline, the bench will show ~zero gain and they'll wrongly conclude the mechanism is dead. **This is the one place R1 is easy to get silently wrong.**

Minor hazards: `&[u8]` borrows the `PyBytes` (kept alive by the caller's ref through the call; copy out before re-attach — no dangling). `gzip_compress` returns `Result` → map `Err` to `PyErr`, no panic across FFI (Cargo already pins `panic = "unwind"` + the `await_holding_lock`/`await_holding_refcell_ref` deny-set at `Cargo.toml:64,70-72`, so the FFI-panic and no-await-holding-GIL gates are enforced for any new code).

### 3. The benchmark plan (decisive, runnable)

The bench must **isolate the GIL-serialization concurrency cliff**, not per-byte speed — loopback cannot show the bandwidth win (this is why R1 was NEEDS_BENCH, not just BUILD: `fastapi-rust-absorb-mine.md:27`, `:93`).

**Harness — reuse what exists.** `benchmarks/servers.py` already has (a) `lancelot_factory()` (the uvicorn ASGI path R1 targets, `:102-120`) and (b) `LC_BENCH_ITEMS=N` payload sizing (`:45-50`) producing a list of varied `{id, name, val}` objects — a **low-redundancy** payload, which is exactly right (a repeated-token string compresses to nothing in <0.2ms and would hide the CPU cost — the bench-method panel caught this explicitly). `run_load.sh` is the run1/run2 + warmup + `wait_listen` template.

**Setup:** single-process uvicorn (`uvloop + httptools`, `--workers 1` — **one GIL, the worst case for contention, which is the whole point**). One route returning ~30-64 KB compressible JSON (`LC_BENCH_ITEMS≈1000`). New `benchmarks/compress_bench.sh` beside `run_load.sh`; raw dumps under `benchmarks/results/`.

**Three arms, identical payload/route, `oha -z 30s -c 200 -H 'Accept-Encoding: gzip'`, 2 runs each:**
- **A** = no compression (throughput ceiling).
- **B** = Starlette `GZipMiddleware(compresslevel=9)` (GIL held across zlib on the loop thread — `gzip.py:126/133`).
- **C** = `lancelot.compress.GZipResponder` (libdeflate level 9 via `run_in_executor`, GIL released off-loop).

**Metric:** per-worker **Requests/sec AND p99 latency** under c=200 (the throughput of *other* concurrent requests is what the GIL-release frees). `oha` auto-decompresses, so a passing run also proves wire correctness.

**Three mandatory controls** (separate the structural claim from noise):
1. **c=1 control** (A vs B vs C at single concurrency): C and B should be **near-parity** — this proves any c=200 win is *structural concurrency*, not per-call Rust speed.
2. **Co-tenant starvation row** (the decisive test, from skeptic + framework panels): 50% large-compressible route + 50% a tiny `/echo`, hammered together. Measure the **tiny route's p99**. If B's held-GIL starves the co-tenant and C's released-GIL doesn't, *that* is the mechanism made visible. If there's no starvation difference, the GIL was never the bottleneck.
3. **Multi-process control** (`--workers N`): separate GILs → the gap should **shrink or vanish**, telling the operator exactly when R1 matters (it doesn't, behind multi-worker uvicorn).

Plus: byte-equality of decompressed output A-vs-C; confirm an SSE route is passed through uncompressed under both.

### 4. SUCCESS THRESHOLD (= KEEP)

KEEP **only if ALL hold**:
- At **c=200, single worker, level 9, ~30-64 KB**: **RPS(C) ≥ 1.5× RPS(B)** OR **p99(C) ≤ 0.6× p99(B)** (the GIL cliff Starlette pays is visibly removed);
- The **co-tenant tiny-route p99 is materially better under C than B** (proves the held-GIL was actually starving peers — the structural claim, not an artifact);
- The **c=1 control stays within ±10%** (confirms the win is concurrency, not per-call speed);
- Output is byte-valid gzip; SSE/streaming correctly skipped;
- **A maintainer explicitly signs off on reopening the documented Tier-D gzip rejection** + revising the freshly-written `docs/deploy.md` / CLAUDE.md compression stance. This is a **HARD gate independent of the number** (every panel + both prior docs require it).

### 5. REVERT-IF (= delete it, restore Tier-D)

REVERT — delete `lancelot/compress.py` + `src/compress.rs` + the Cargo dep, restore the Tier-D entry, document `add_middleware(GZipMiddleware)` / "let the proxy compress" — if **ANY**:
- RPS(C) < 1.5× RPS(B) **and** p99 not materially better at c=200 single-process;
- the co-tenant row shows **no starvation difference** (GIL wasn't the bottleneck at realistic payloads — the most likely outcome if `run_in_executor`'s thread-hop overhead eats the gain, or CPython's zlib already drops the GIL on large blocks, a risk the framework panel flagged to test);
- the multi-process control shows the gap vanishes **and** the maintainer judges single-process-PaaS too narrow to own a forever Rust codec + hot-path negotiation branch;
- **no maintainer sign-off.**

**The default on a non-decisive bench is REVERT** — R1 reopens a documented decision, so the burden of proof is on the feature, and whoever runs the bench should (positioning panel's words) "be emotionally prepared to delete it."

### 6. Honest benefit read + go/no-go

The GIL argument is **legitimately new and reopens Tier-D honestly** — verified: Starlette runs `gzip.GzipFile(compresslevel=9)` holding the GIL across the whole zlib pass (`gzip.py:126/133`), and the prior rejection (CLAUDE.md Tier-D) only ever reasoned "the proxy does it / reimplementing Starlette = useless logic" — it never reasoned about the GIL. And "the proxy compresses" is genuinely false on single-container PaaS, which M12 made first-class.

**But the benefit is structurally narrow, and three verified facts bound it:**
1. **ASGI-seam-only** — the marquee 183k native path gets *nothing* (no send seam). The feature cannot improve the server Lancelot actually leads with. (This is the skeptic's core reason for SKIP, and it is correct.)
2. **"No compressing proxy" is the binding precondition** — anyone behind nginx/Cloudflare/a TLS proxy already gets compression there and would not switch for this.
3. **Recurrence is thin** — 2 distinct corpus projects (dispatch, prefect), not "3/24" (critic-corrected in both prior docs: `fastapi-rust-absorb-mine.md:128,147`; `deferred-candidates-panel.md:74`). And uvicorn's own production scaling is multi-**process** (separate GILs), where the contention R1 fixes doesn't exist.

**Who actually benefits:** a single-container PaaS app, under uvicorn (not the native server), with no compressing proxy, returning large compressible non-SSE JSON, under real concurrency. A real but thin slice.

**Go/no-go: BUILD-then-BENCH, pre-committed to REVERT.** The mechanism is sound and the question is worth settling once with data. But do **not** oversell it: even on a KEEP it is *a structural-concurrency fix for a niche uvicorn deploy*, never a blanket "faster compression" claim. **Flag for the maintainer:** the four BUILD-IF-BENCH panels are *not* rationalizing past Tier-D — they each independently restate the narrow-audience caveat and gate on sign-off. The risk of rationalization is the opposite: treating a marginal c=200 number as a win. Hold the 1.5× line.

---

## Item Task#7 — Native server drives the ASGI lifespan protocol

### 1. Panel tally + consensus

| Panel | Verdict |
|---|---|
| Rust/pyo3 systems | **BUILD** (high) |
| Framework API architect | **BUILD** (high) |
| Benchmark methodologist | **BUILD** (high) |
| Skeptic | **BUILD-IF-BENCH** (high) |
| Positioning + maintenance | **BUILD** (high) |

**Consensus: 4 BUILD / 1 BUILD-IF-BENCH → BUILD (option A).** Unanimous in direction. The lone "BUILD-IF-BENCH" is the *skeptic* — and even the skeptic, who came in expecting to recommend REVERT, lands on BUILD: *"This is the rare deferred item where the skeptic lens still says BUILD: the bug is real, the fix is structurally clean, and the revert trigger (any hot-path regression) is sharp and cheap to check."* The "IF-BENCH" qualifier is not about whether the feature is worth it — every panel agrees it is **principled regardless of any perf win**, because it fixes a silent production footgun. The bench is a **guardrail (prove no hot-path regression)**, not a go/no-go on worth. The four BUILD panels say the same thing in their benefit reads ("the bench only PROTECTS the envelope, it doesn't justify the feature").

### 2. Recommended implementation approach

**The bug (confirmed):** `app.run()` runs route handlers + connection-drain only; it does **not** drive the ASGI lifespan protocol. Verified at the docstring (`app.py:207-212`) and `docs/deploy.md:16-23`: under `app.run()`, `lifespan=` / `@app.on_event` / `@app.lifetime_task` / mounted sub-app lifespans **silently no-op** — an `@app.on_event("startup")` that sets `app.state.client` never fires, so the first request AttributeErrors. The majority of real apps (DB pool, model load, lifetime daemons, mounted sub-apps) are silently broken on the native deploy path.

**Build option A: make the native server DRIVE the existing `_lifespan` machinery — reuse, do not reimplement.** The entire machinery already exists and is proven on the uvicorn path:
- `lancelot/asgi.py:195-294` — `_lifespan` already handles `_run_on_event('startup'/'shutdown')`, root `lifespan=` CM `__aenter__`/`__aexit__`, `_enter_mounts` (mounted sub-apps, LIFO), `_start_lifetime_tasks`, partial-startup unwind, and failure reporting with full traceback.
- `asgi.py:108-182` — `_sub_lifespan_cm` already drives the ASGI lifespan protocol against an app callable via in-memory asyncio queues. This is the exact reusable shape.

**Two proven Rust↔Python patterns the fix reuses (verified — this is why it's low-risk):**
1. `src/service.rs:445` — `graceful_loop_teardown` **already** does `event_loop.call_method1("run_until_complete", ...)` *inside* the driver. So driving a coro on the server's own loop pre-bind / post-drain is an existing, tested pattern — **not** new GIL-across-await risk.
2. `run_until_complete` is used at `dispatch.rs:183`, `stream.rs:410`, `service.rs:405` — the bridge is well-worn.

**Wiring:**
- **Python (`app.py:908` `run()`):** build a thin `_run_startup(app)` / `_run_shutdown(app)` that drive the *existing* `_lifespan` protocol (via the `_sub_lifespan_cm`-style in-memory channel, or by directly invoking `cm.__aenter__()` + `_enter_mounts` + `_start_lifetime_tasks` in startup order and the mirror in shutdown order). **Build zero new lifespan logic.** Persist the entered `AsyncExitStack` + root cm on the app so shutdown can unwind. Pass these as `on_startup`/`on_shutdown` coroutine factories into `_native.run` (new optional trailing kwargs, back-compat).
- **Rust (`src/service.rs::serve`):**
  - **BEFORE binding the listener** (before the `loop {}` accept block, `:343`), run startup on the server's own loop: `event_loop.run_until_complete(startup_coro)`. **If startup raises → REFUSE TO SERVE:** skip bind, `event_loop.close()`, propagate the `PyErr` so `app.run()` exits non-zero (fail-fast parity with uvicorn's `startup.failed`; never bind a socket on a half-initialized app).
  - **AFTER the existing `graceful_break!` drain completes** (`:326-333`, after `acquire_many_owned(max_conns)` and BEFORE `graceful_loop_teardown`): run shutdown bounded by `drain_timeout()` / `LANCELOT_DRAIN_SECS` — `run_until_complete(asyncio.wait_for(shutdown_coro, drain_secs))` so a stuck DB-pool-close can't wedge process exit.

**THE one design detail that decides correctness — loop identity (every panel flagged it; the skeptic + bench-method + framework panels made it the explicit revert trigger):** `_start_lifetime_tasks` does `asyncio.ensure_future(fn())` (`app.py:165`), binding daemons to the **running loop**. Startup-created async resources (DB pools, daemons) MUST be created on the **server's own loop** — `app.locals.event_loop(py)` (the loop `serve()` already owns and will pump, obtained at `service.rs:295`) — NOT a fresh short-lived Python loop. A pure-Python "drive startup then call `_native.run`" is **WRONG**: the pool/daemon would bind to a dead loop and be unusable from handlers. Startup must run *inside* `serve()`, on `event_loop`, before the accept loop. (The bench's GATE 1 daemon-heartbeat assertion is precisely what catches a loop-identity mistake.)

**HOT PATH IS UNTOUCHED BY CONSTRUCTION (the must-not-break invariant):** lifespan is bind-time + drain-time only. `handle()` (`service.rs:178`) and the const-tier fast path (`:209-211`, which returns *before* any GIL touch) get **zero new instructions**. The 183k const-tier and ~59k tiny-handler numbers cannot move because not one cycle is added between accept and response. Gate all new work strictly before the accept `loop {}` and strictly after `graceful_break!`.

**Default-ON with a `LANCELOT_LIFESPAN=0` escape hatch** (framework panel): a flag re-creates the silent-no-op trap (just-works is the point). Lifespan-less apps are a cheap no-op (`_lifespan` already handles `app._lifespan is None` + empty on_event/mounts at `asgi.py:209-243`).

**Docs:** flip `docs/deploy.md:16-23` and the `App.run()` docstring (`app.py:207-212`) from "lifecycle runs on uvicorn only" → "the native server drives startup/shutdown." **Scope-discipline note the skeptic made explicit and is verified-correct:** this item is **LIFESPAN ONLY.** The native server still runs **no DI/body-parsing** — handlers under `app.run()` still receive a raw `Request`, not FastAPI-style injected deps/parsed bodies (verified: `app.py:915` forwards raw routes). The docs must say so or users hit the *second* gap and assume full ASGI parity. Recommended doc flip: *"lifespan now runs on the native server; DI/body-parsing still require uvicorn."*

### 3. The benchmark plan (two gates, both required)

**GATE 1 — IT WORKS (functional; the primary proof).** A real-server subprocess test launched via `python fixture.py` ending in `app.run()` — **NOT** `TestClient` and **NOT** uvicorn (cardinal rule 2: `TestClient`/uvicorn drive lifespan today and would *hide* this bug). Mirror the existing `tests/test_m15_mount_lifespan.py` / `tests/test_m4_const_tier.py` subprocess + SIGTERM shape. Fixture registers:
- `@app.on_event("startup")` setting `app.state.client = "READY"`;
- a `lifespan=` async-CM appending an enter/exit line to a side-channel file;
- an `@app.lifetime_task` daemon writing a heartbeat every ~50-100ms;
- a mounted sub-app whose lifespan touches a marker file;
- a `GET /probe` returning `app.state.client`.

Assertions: (a) first `GET /probe` returns `"READY"` (today: AttributeError → 500); (b) lifespan-enter + ≥2 heartbeat lines appear before the first request (daemon runs on the server's loop — the loop-identity proof); (c) sub-app marker exists; (d) `kill -TERM` → lifespan-EXIT line + sub-app teardown written and **heartbeat STOPS** within `LANCELOT_DRAIN_SECS`, with **no "Task was destroyed but pending" on stderr**; (e) negative: a startup that **raises** → process exits **non-zero WITHOUT binding** (assert connection-refused + nonzero rc); (f) lifespan drives exactly once.

**GATE 2 — NO PERF REGRESSION (the guardrail / tripwire).** Re-run `benchmarks/run_load.sh` percore (`LANCELOT_WORKERS=1`) on native `/echo` tiny-handler **AND** a const-tier `const_routes` `/livez` (the ~183k shape), on the **pre-change** vs **post-change** binary, 2 equal-duration runs each, same machine/loopback/oha. Run it twice over: once on an app with **no lifespan** (fast path) and once **with a trivial lifespan** (proves the per-request path is untouched regardless of lifespan presence). Because the change adds zero per-request instructions, the expected delta is **dead-flat noise**.

> *Note for GATE 2 wiring:* the const-tier 183k path is benched via `_native.run(const_routes=...)` directly today, because `App.run()` drops `const_routes` (`app.py:915`). Bench it via `_native.run` directly, or fold the trivial `const_routes` pass-through fix into this PR (see bottom-line note).

### 4. SUCCESS THRESHOLD (= KEEP)

KEEP if **BOTH**:
- **GATE 1** fully green on the native subprocess: startup state visible to the first request (no AttributeError), lifetime daemon runs **and is cleanly cancelled on SIGTERM with its `finally` executing**, mounted sub-app lifespan enters+exits LIFO, shutdown completes within `LANCELOT_DRAIN_SECS`, a raising startup fail-fasts non-zero **without binding**, lifespan drives exactly once;
- **GATE 2** native tiny-handler `/echo` and const-tier `/livez` per-core RPS **within ±3% (noise band)** of the pre-change baseline across both runs, for both the no-lifespan and trivial-lifespan apps.

Plus pre-merge gates (not bench, but blocking): conformance ratchet **≥974** (the `_lifespan` reuse must stay behavior-preserving on the uvicorn path), `cargo clippy --release --all-targets -D warnings`, `cargo test --release --lib`, internal pytest green.

### 5. REVERT-IF (= back to "deploy under uvicorn for lifecycle", build nothing — option B)

REVERT if **EITHER**:
- **GATE 2** shows any **reproducible >3% (beyond run-to-run noise)** regression on the const-tier OR tiny-handler path — that means a hot-path instruction leaked in (it should not, by construction; if it does, the wiring touched `handle()` and is wrong). *The native server's whole identity is the perf envelope; trading it for lifespan would be a bad deal when uvicorn already runs lifespan correctly.*
- **GATE 1** reveals a structural defect: startup resources don't bind to the server's loop (loop-identity design wrong — a pool created at startup is unusable from handlers); OR a daemon can't be cleanly cancelled within `LANCELOT_DRAIN_SECS` (orphaned tasks / "Task was destroyed"); OR shutdown can't be bounded (a hung user shutdown wedges process exit — *worse* than the current no-op); OR a Rust→Python re-entrancy issue destabilizes `run_until_complete`.

**Partial-revert escape valve (bench-method panel):** if only the cross-loop `@app.lifetime_task` daemon case is problematic but `lifespan=`/`@app.on_event` (resource setup/teardown — the majority benefit) works flat, **scope DOWN** to lifespan + on_event rather than reverting wholesale. Full revert only if GATE 2 fails.

### 6. Honest benefit read + go/no-go

**Go: BUILD.** This is the strongest item on the board and is **principled regardless of any headline bench win** — the bench is a safety gate, not a justification. Today the native `app.run()` server (the high-throughput product Lancelot leads with: const-tier 183k, large-payload + streaming wins) silently no-ops `@app.on_event` / `lifespan=` / `@app.lifetime_task`, so the majority of real apps either AttributeError on first request or leak/never-init resources — the **worst kind of failure** (green under `TestClient`/uvicorn, broken on the very server the docs recommend for perf). "Just use uvicorn" is the weak answer *precisely because* the const-tier/large-payload apps that pick the native server are the ones most penalized by switching off it (~12% slower on tiny, no const tier).

**Who benefits:** every native-server user with any startup/shutdown need — i.e. nearly everyone deploying a real AI/cloud app beyond a stateless echo service. **Nobody on the tiny/const fast path pays** (zero per-request cost).

**Cost is low and bounded:** ~6-10h — a Python lifespan-runner wrapper + ~30 lines of Rust calling `run_until_complete` twice around the accept loop. It **reuses the entire audited `_lifespan` machinery** (no second lifespan implementation to keep in sync) and the existing `graceful_loop_teardown` Rust→Python pattern.

**The only real risk is the Rust↔Python loop-identity / bridge-robustness detail** — pinned by the design (run on `app.locals.event_loop`) and directly verified by GATE 1's daemon-heartbeat assertion + the sharp GATE 2 revert trigger. **Document the honest caveats:** SIGKILL (not SIGTERM) still skips shutdown (unavoidable), a free-standing `app.run()` is still single-process (multiprocess scaling unchanged), and **DI/body-parsing still require uvicorn** (this item is lifespan-only).

**Flag for the maintainer:** no panel here is rationalizing past the perf envelope — every one makes "hot-path within noise" the hard, cheap-to-check revert gate, and the fix is hot-path-untouched by construction (the const tier returns at `service.rs:211` before any handler/GIL touch). The risk to watch is the *inverse* of Task7's design: don't let a marginal GATE 2 reading slide. ±3% is the line.

---

## Bottom-line build order

**1. Build Task#7 FIRST (BUILD, unanimous-in-direction).** It fixes a real, silent, in-production footgun on the server Lancelot markets; it is principled regardless of any perf number; the machinery is already built (reuse `asgi.py:195-294` + the `service.rs:445` Rust→Python pattern); and the per-request hot path is untouched by construction.
- **Its bench must show:** GATE 1 — startup hooks fire before the first request (no AttributeError), a lifetime daemon runs on the server's loop *and* is cleanly cancelled on SIGTERM with its `finally` executing, mounted sub-app lifespan runs LIFO, shutdown completes within `LANCELOT_DRAIN_SECS`, and a raising startup fail-fasts non-zero **without binding**. GATE 2 — const-tier (~183k) and tiny-handler (~59k) per-core RPS **within ±3%** of pre-change. **Survives only if both pass.**
- Fold in the trivial `App.run()` → `const_routes` pass-through fix (`app.py:915` drops it today) so the const tier is reachable via the public API *and* benchable via GATE 2 — but keep it a clearly separate commit.

**2. Build R1 SECOND, gated, and only after a maintainer signs off on reopening Tier-D (BUILD-IF-BENCH, 4-1, REVERT-biased).** The GIL mechanism is genuinely new and sound, but it lands on the uvicorn seam only (the native 183k path provably cannot host it — `service.rs` has no send seam), the audience is the thin "single-container PaaS, no proxy, large non-SSE JSON, under concurrency" slice, and it reopens a documented decision.
- **Its bench must show:** at c=200 / single-worker / level-9 / 30-64 KB low-redundancy JSON, **RPS(C) ≥ 1.5× RPS(B)** OR **p99(C) ≤ 0.6× p99(B)** vs Starlette `GZipMiddleware`, **AND** the co-tenant tiny-route p99 materially better under C than B (the GIL-starvation made visible), **AND** the c=1 control near-parity (proves structural concurrency, not per-call speed). **Below the bar, or no sign-off, or the multi-process control erases the gap → delete `compress.py` + `compress.rs` + the Cargo dep and restore the Tier-D rejection.** Default-on-non-decisive = REVERT.

**Where the evidence overrules a panel:**
- **R1 — the SKIP (skeptic) is the correct prior, not an outlier.** Side with running the cheap experiment (4 panels) *because* a durable data point settles a recurring question — but adopt the skeptic's framing wholesale: expect REVERT, hold the 1.5× + co-tenant-starvation line, and don't let a marginal number rationalize past Tier-D. The one factual error to carry forward: the rust-impl panel's "the codebase wraps `py.detach`" is **wrong** (`gil.rs:27-30` — no detach wrapper exists; R1 introduces the first explicit GIL-release in the crate). Get that right in the PR.
- **Task#7 — the "BUILD-IF-BENCH" (skeptic) collapses into BUILD;** even that panel concludes BUILD, and the "IF-BENCH" is a guardrail (no hot-path regression), not a worth-gate. No panel is rationalizing past the perf envelope here — the hot-path-untouched-by-construction fact (`service.rs:209-211`) plus the ±3% tripwire keeps it honest. Watch only that GATE 2 is read strictly.

**Files that will change** (absolute paths): Task#7 — `/Users/seventyleven/Desktop/Lancelot/lancelot/app.py`, `/Users/seventyleven/Desktop/Lancelot/src/service.rs`, `/Users/seventyleven/Desktop/Lancelot/src/lib.rs`, `/Users/seventyleven/Desktop/Lancelot/docs/deploy.md`, reuse `/Users/seventyleven/Desktop/Lancelot/lancelot/asgi.py`. R1 — new `/Users/seventyleven/Desktop/Lancelot/lancelot/compress.py` + `/Users/seventyleven/Desktop/Lancelot/src/compress.rs` + `/Users/seventyleven/Desktop/Lancelot/Cargo.toml` (add `libdeflater`) + new `/Users/seventyleven/Desktop/Lancelot/benchmarks/compress_bench.sh`, reuse `/Users/seventyleven/Desktop/Lancelot/benchmarks/servers.py`.