# Deployment / packaging — byte-path + operator-experience synthesis

**Domain:** multiprocess model, wheels/abi3, container cold-start, multi-region, cloud
platforms (Railway/Render/Modal/Cloud Run/k8s), secrets, config-from-env. The **operator
experience** of shipping a Lancelot app.

**Method:** read the full mine (`01_robyn`, `02_fastapi`, `03_blazeio`, `04_serving`,
`05_lancelot_state`) + diffed against Lancelot source **on disk** (not the notes):
`docs/deploy.md`, `.github/workflows/wheels.yml`, `lancelot/{app,asgi,fleet}.py`,
`src/service.rs`, `Cargo.toml`, `pyproject.toml`, and the absence of `lancelot/cli.py` /
`lancelot/__main__.py` / `templates/`. Every verdict respects the Tier-D refuse list
(`05_lancelot_state/research_plans_ledger.md §1`) and the cardinal rules.

**One-line state:** the deployment story is **already strong and mostly correct** — the
hard parts (multiprocess + `SO_REUSEPORT`-Linux-only, cgroup-aware workers, graceful
SIGTERM drain, abi3 wheels CI, `$PORT` binding, lean cold-start, an honest platform matrix,
secrets, config-from-env) are **shipped and ahead of Robyn**. The gaps are a **handful of
small correctness/operator-experience items**, the largest of which is a **code-verified
`/readyz` startup-gate bug** that `docs/deploy.md` already (incorrectly) claims is fixed.

---

## VERIFIED on-disk facts (the diff baseline)

| Fact | Evidence | Status |
|---|---|---|
| abi3 wheels CI: manylinux x86_64+aarch64, macOS arm64+x86_64, Windows x64; one wheel/arch covers py3.11+; PyPI Trusted Publishing on `v*` tag | `.github/workflows/wheels.yml:32-36, 62-74` | HAVE |
| `app.run()` binds from env: `LANCELOT_HOST` else 127.0.0.1; `LANCELOT_PORT`→`PORT`→8000; bare `$HOST` ignored | `docs/deploy.md:41-63`; CLAUDE.md M12 | HAVE |
| Multiprocess deploy doc: N procs × 1 worker, `SO_REUSEPORT` Linux-only LB, macOS = distinct ports behind proxy | `docs/deploy.md:131-155` | HAVE |
| cgroup-aware worker count (`cpu.max`), `LANCELOT_WORKERS` override [1,64] | `docs/deploy.md:84-95`; `src/cpu.rs` | HAVE |
| Graceful SIGTERM drain `acquire_many_owned(max_conns)`, `LANCELOT_DRAIN_SECS` (Rust 10 / Py 30) | `src/service.rs:105-117`; `docs/deploy.md:228-233` | HAVE (ahead of Robyn hard-kill) |
| Lean import (`import lancelot` pulls only pydantic) → fast cold start | `docs/deploy.md:92-93`; cardinal rule 3 | HAVE |
| Honest platform matrix: container PaaS first-class; serverless caveated; Supabase = connect-to-not-host | `docs/deploy.md:101-129` | HAVE |
| Slow-loris header-read deadline `LANCELOT_HEADER_TIMEOUT_MS` (15000) | `src/service.rs:124, 523` | HAVE (ahead of Robyn `client_request_timeout(0)`) |
| Release profile `lto="fat"`, `codegen-units=1`, `opt-level=3` | `Cargo.toml:64-67` | HAVE (matches Robyn) |
| `panic="unwind"` (NOT abort) | `Cargo.toml:68` | **Deliberate divergence** — R7 `catch_unwind` FFI panic safety requires unwind; `abort` would break it. NOT a gap. |
| `__version__="0.1.0"`; `/fleet/status` reports it | `lancelot/__init__.py:28`; `fleet.py:75-90` | HAVE |
| **`/readyz` checks ONLY `self._shutting_down`** — returns 200 from route-registration, BEFORE `startup` lifespan completes | `lancelot/app.py:313-321` | **MISSING gate** (see B1) |
| Lifespan emits `lifespan.startup.complete` (the hook point for the gate) | `lancelot/asgi.py:228, 272` | enables B1 |
| **No `lancelot/cli.py`, no `lancelot/__main__.py`, no `templates/`** — `python -m lancelot run` does NOT exist today | dir listing | gap behind a Tier-D wall (see B2) |
| No body-read / first-body-chunk deadline knob (only header deadline) | `src/service.rs` grep | gap (see B4) |
| No free-threaded (`cp313t`/`cp314t`) wheels in the matrix | `wheels.yml:28-36` | gap (see I3) |

---

## MUST-BUILD (real, in-mission, respects cardinal rules)

### B1 — Three-state health: `/readyz` 503 until lifespan startup completes  *(S, code-verified bug)*

**Mechanism (peer):** SGLang/TGI separate "alive" from "ready-to-serve" — TGI's `Warmup`
gates traffic until the engine can produce tokens; SGLang `/health_generate`. The HTTP-layer
translation is the **startup window**: between port-bind and `startup`-lifespan-complete a
worker must answer `/readyz` with **503 + Retry-After**, not 200.
**Source:** `04_serving/serving_frontier.md §6` (SGLang `http_server.py:656`); the FastAPI
corpus 5-project convergence on a readiness gate (`05_..._ledger §4 MED "Readiness-callback"`).

**Lancelot state — MISSING, and worse than the docs claim.** `lancelot/app.py:313-321`
`_readyz()` returns 200 the instant the route is registered and only flips to 503 on
`self._shutting_down`. There is **no `_starting_up`/`_lifespan_started` flag**. Yet
`docs/deploy.md:248-249` asserts `/readyz` returns "lifespan started + in-flight under
threshold" — the doc is **ahead of the code**. Every LB/orchestrator that probes `/readyz`
before the first request gets a **false-positive 200 during the startup window**, so a new
replica receives traffic before its DB pool / model client / `app.state.engine` is ready —
the exact deploy-and-roll failure the gate exists to prevent.

**Recommended approach:** add `self._lifespan_started: bool = False` in `App.__init__`; set
it `True` at the `lifespan.startup.complete` send site (`asgi.py:228` and `:272` — both the
native-driven and mounted paths). In `_readyz()`: if `not self._lifespan_started` → 503 +
`Retry-After: 1` `{"status":"starting"}`; elif `self._shutting_down` → existing 503
`draining`; else 200. **Gate behind `LANCELOT_LIFESPAN` being on** (if lifespan is disabled,
keep the current always-200 so the escape hatch isn't broken). Fix the deploy.md sentence to
match. **Validate under load** (cardinal rule 2): subprocess test — probe `/readyz` during a
slow `startup` and assert 503-then-200.

**Gate risk:** none. `/readyz` is `include_in_schema=False`, not a conformance route; no
clippy/perf surface (pure-Python flag read on a non-hot endpoint).

---

### B2 — `python -m lancelot run` entrypoint + minimal PaaS zero-config start  *(S–M, behind a Tier-D wall — scope tightly)*

**Mechanism (peer):** Railway's **Railpack** detects FastAPI by import and emits
`uvicorn app:app --host 0.0.0.0 --port ${PORT}`; unknown frameworks fall to `python main.py`.
A Lancelot app with no `Procfile`/`railpack.json` hits the `python main.py` fallback, which
only works if `__main__` calls `app.run()` — a real friction point on Railway/Render.
**Source:** `04_serving/gateways_deploy.md §M14` (RAILWAY.md:272-323), `§M13` (Railway
`drainingSeconds`+`healthcheckPath`), `§M15` (`$PORT`).

**Lancelot state — MISSING.** No `lancelot/__main__.py` / `lancelot/cli.py` (verified). The
recommended start commands in `docs/deploy.md` are `uvicorn …` or `python yourmod.py` — both
require the operator to write the entrypoint.

**Tier-D tension (must respect):** the ledger REFUSES a "Built-in CLI (`lancelot run …`)"
(`§1`, rationale: FastAPI delegates to `fastapi-cli`; uvicorn already works) AND the WBS
`lancelot init --docker --k8s --lambda` scaffolder (U37) is a large `templates/` system —
**do not build that.** The buildable sliver that does NOT violate the rule: a **3-line
`lancelot/__main__.py`** that resolves an app from `$LANCELOT_APP` (e.g. `mymod:app`) and
calls `app.run()` (which already reads `LANCELOT_HOST`/`PORT` from env). This is *not* an
arg-parsing CLI framework — it is the missing module entrypoint so `python -m lancelot run`
works as a Railpack/Procfile target. Optionally ship a **copy-pasteable** `Procfile` +
`render.yaml` + `railpack.json` snippet **in docs** (not a generator).

**Recommended approach:** `lancelot/__main__.py` → `App` discovery from `LANCELOT_APP` env
(fail-fast with a clear message if unset/unimportable) → `app.run()`. Document the Procfile
(`web: python -m lancelot run`) and `healthcheckPath: /readyz` in `docs/deploy.md`. Keep it
to one file; if it grows arg-parsing it has crossed the Tier-D line — stop.

**Gate risk:** none on conformance/clippy. The only risk is **scope creep into the refused
CLI** — bound it to the entrypoint + docs.

---

### B3 — Drain-timing doc rule: `platform_shutdown_grace ≥ LANCELOT_DRAIN_SECS + 10`  *(S, docs only — real correctness)*

**Mechanism (peer):** Render's deploy ladder sends SIGTERM **+60s after** the new instance
is live, then SIGKILLs after `maxShutdownDelaySeconds` (default 30s, max 300s). Railway has
`drainingSeconds`/`overlapSeconds`. If an operator raises `LANCELOT_DRAIN_SECS` above the
platform's grace window, the drain is **SIGKILLed mid-flight** — silently re-creating the
hard-kill anti-goal Lancelot explicitly avoids.
**Source:** `04_serving/gateways_deploy.md §M12` (RENDER.md:249-257), `§M13` (RAILWAY.md:649-650).

**Lancelot state — HAVE the mechanism (`LANCELOT_DRAIN_SECS` + `initiate_shutdown()` +
`/readyz` 503), MISSING the operator guidance.** `docs/deploy.md` documents the drain but
never states the platform-grace relationship.

**Recommended approach:** one rule in `docs/deploy.md` + `docs/operator/runbook.md`:
*"Set the platform's shutdown grace (`render.maxShutdownDelaySeconds`,
`railway.drainingSeconds`, k8s `terminationGracePeriodSeconds`) ≥ `LANCELOT_DRAIN_SECS` + 10."*
Pair with the B2 docs snippet so the scaffolded `render.yaml`/manifest defaults
`grace = max(40, LANCELOT_DRAIN_SECS + 10)`.

**Gate risk:** none (docs).

---

### B4 — First-body-chunk deadline → 408 (slow-loris variant past the header timeout)  *(M, Rust)*

**Mechanism (peer):** Modal imposes `FIRST_MESSAGE_TIMEOUT_SECONDS = 5.0` on the first ASGI
`http.request` message; on miss it synthesizes a 408 and disconnects. This closes the
"headers sent, body stalled forever" shape that **bypasses a header-only deadline**.
**Source:** `04_serving/serving_frontier.md`-adjacent / `02_fastapi/proj_serving.md §19`
(MODAL `_runtime/asgi.py:21,121-160`).

**Lancelot state — MISSING.** `src/service.rs` has `header_read_timeout` (slow-loris on the
headers) but **no deadline on body reads** once hyper has parsed headers and the ASGI bridge
fires. A client that sends a valid header block then dribbles/never-sends the body holds a
connection slot until the OS or `LANCELOT_MAX_CONNECTIONS` backpressure kicks in. This is a
genuine adversarial-resilience gap, complementary to the shipped header guard.

**Recommended approach:** a `LANCELOT_BODY_TIMEOUT_MS` env (default **0 = off**, so streaming
uploads and long-poll bodies are unaffected unless opted in; clamp like the others) wrapping
the body-collect future in `tokio::time::timeout` → 408 on expiry. **Must default OFF** (an
always-on body deadline would break legitimate slow large uploads — the same reason the
header timeout is generous at 15s). **Validate under load** (cardinal rule 2): a
real-server test that sends headers + a stalled body and asserts 408 + slot release, plus a
test that a slow-but-progressing upload is NOT killed.

**Gate risk:** clippy/perf low (one `timeout` wrapper on a non-hot path, off by default).
No conformance surface (native server only; ASGI/uvicorn unaffected).

---

## IMPROVE (smaller, in-mission)

### I1 — `bake_global_headers` into the const-tier `Prebaked` response  *(S, Rust)*

**Mechanism (peer):** Robyn bakes global response headers into each `CachedResponse`'s
pre-flattened `Arc<Vec<(name,value)>>` **at startup** (gated on no-middleware), so a const
hit is one Arc-refcount-bump + a loop over pre-built pairs — zero per-request header work.
**Source:** `01_robyn/rust_core.md §M6/M9`, `01_robyn/teardown.md §M9` (`const_router.rs:172-199`).

**Lancelot state — GAP (minor).** `service::Prebaked` (`src/service.rs:142`) bakes status +
content-type + body but **not** framework/global response headers — a const `/livez` returns
a bare hyper response with no `Server:`/request-id/operator headers. For most const probes
this is fine (that's the point — minimal), but operators who want a consistent header set on
every response (incl. const) can't get it on the zero-GIL tier.

**Recommended approach:** accept an optional `Vec<(HeaderName, HeaderValue)>` at
`Prebaked::new()` (validated at registration like content-type already is) and include it in
the pre-built response builder. Keep it **opt-in** — the default const tier stays header-bare
to preserve the 183k-req/s zero-work property. Wire through `app.const_route(...)` /
`_native.run(const_routes=...)`.

**Gate risk:** none on conformance; clippy/perf negligible (startup-time bake, refcount-bump
serve). **Honest note:** this is an *operator-convenience* win, not a throughput win — scope
accordingly.

---

### I2 — `/fleet/status` `queue_depth` field for LB shedding  *(S, one-liner)*

**Mechanism (peer):** SGLang `GET /get_load` → `{num_requests_waiting, num_requests_running}`
so a frontend LB sheds to the less-loaded replica.
**Source:** `04_serving/serving_frontier.md §17` (`http_server.py:656`).

**Lancelot state — PARTIAL HAVE.** `/fleet/status` (`fleet.py:75-90`) already reports
`in_flight` (running) but not **queued-behind-the-semaphore** depth. The signal exists in
`TenantConcurrencyLimiter._semaphores` and/or `App._max_concurrent_requests` waiters.

**Recommended approach:** add `queue_depth` to `fleet_info()` derived from the concurrency
semaphore's outstanding-waiters count (0 when no limiter is installed). Ships with the next
fleet-observability touch; no new dep.

**Gate risk:** none.

---

### I3 — Free-threaded wheels (`cp313t`/`cp314t`) in the build matrix  *(M, forward-looking, bench-gate the claim)*

**Mechanism (WBS):** U45 specifies `cp313t`/`cp314t` ×5 platforms + a `sys._is_gil_enabled()`
assertion in CI. The entire Lancelot perf story is GIL-bound ("anti-scales with threads →
deploy multiprocess"); a free-threaded build is the one path that could change that calculus.
**Source:** `05_lancelot_state/research_plans_ledger.md §8 U45`.

**Lancelot state — MISSING.** `wheels.yml` builds abi3 (`abi3-py311`) only — abi3 and
free-threaded are **mutually exclusive** (the `cp3Xt` ABI is non-stable), so ft wheels are a
separate matrix dimension with their own build, not a flag flip.

**Recommended approach:** add a `cp313t` build arm (maturin `--interpreter python3.13t`,
`abi3` OFF for that arm) as **build-only first** (no publish), with a CI assertion that the
extension imports under a free-threaded interpreter. Treat the *perf* claim as a **bench-gate**
(cardinal rule: don't oversell) — only pitch "scales on threads" after the oha harness shows
the GIL-serialization actually lifts under `python3.13t`. Until then it's packaging
readiness, not a perf claim.

**Gate risk:** CI matrix grows (build time); no conformance/clippy surface. **Risk of
overselling** — keep the claim gated on measured numbers.

---

### I4 — `LANCELOT_BODY_TIMEOUT_MS` is the same lever as B4 (cross-ref)

(Listed once under B4; noted here so the env-knob inventory in `docs/deploy.md` gains the row.)

---

## HAVE — already shipped, do NOT rebuild (tag so nothing is redundant)

- **Multiprocess model** — N procs × 1 worker; `SO_REUSEPORT` Linux-only LB documented as a
  floor on macOS; `LANCELOT_WORKERS` clamp [1,64]. (`docs/deploy.md:131-155`.) **Ahead of
  Robyn** which forks via `dill`/`multiprocess` + pickles a `SocketHeld` fd — Lancelot's
  "N independent processes, no fork, no pickle, no dill dep" is the cleaner model
  (`01_robyn/teardown.md §M19`).
- **abi3 wheels CI** — 5 platforms, one wheel/arch py3.11+, PyPI Trusted Publishing on `v*`.
  (`.github/workflows/wheels.yml`.) `pip install lancelot` in a slim Rust-free image.
- **cgroup-aware worker count** — reads `cpu.max`, not host cores; no thread explosion in a
  capped container (the `os.cpu_count()` footgun that bites Robyn, `07 §12`).
- **Graceful SIGTERM drain** — `acquire_many_owned(max_conns)` bounded by
  `LANCELOT_DRAIN_SECS`; validated under load (`tests/test_m4_resilience_load.py`). **Ahead
  of Robyn** (`process.kill()` hard-kill, `01_robyn/python_bridge.md §1`).
- **`$PORT` / `LANCELOT_HOST` binding** — every-PaaS contract; bare `$HOST` ignored on
  purpose; default loopback so a dev server isn't accidentally exposed. (`docs/deploy.md:41-63`.)
- **Lean cold-start import** — pydantic-only at import; small layer, fast boot.
- **Honest platform matrix** — container PaaS first-class (Render/Railway/Fly/Cloud
  Run/ECS/GKE); serverless (Lambda via Mangum) caveated; Supabase = connect-to. (`deploy.md:101-129`.)
- **Slow-loris header deadline** `LANCELOT_HEADER_TIMEOUT_MS` — **ahead of Robyn**
  (`client_request_timeout(0)` = disabled, `01_robyn/teardown.md §M7`).
- **Secrets management** — 5 lazy backends (Env/File/Vault/Infisical/AwsSecretsManager),
  SIGHUP rotation, fail-safe audit hook, FileBackend traversal guard. (`secrets.py`,
  `05_..._state/resilience.md §5`.) Like-for-like, not a switch reason (ledger §1).
- **Config-from-env** — full knob inventory (Rust: MAX_BODY/MAX_CONNECTIONS/DRAIN_SECS/
  HEADER_TIMEOUT_MS/MAX_INFLIGHT_BYTES/STREAM_BUFFER/WORKERS/LOOP/CONST_STRICT; Python seam +
  upstream pool). (`05_..._state/resilience.md §"env-knob inventory"`.)
- **Release profile** — `lto="fat"`, `codegen-units=1`, `opt-level=3` (matches Robyn
  `01_robyn/rust_core.md §M11`). `panic="unwind"` is a **deliberate** divergence for R7
  `catch_unwind` FFI panic safety — not a gap.
- **`/livez` + `/readyz` + `/fleet/status`** auto-registered; const-tier `/livez` for the
  zero-GIL probe. (`app.py:305-338`; `deploy.md:247-270`.) (Readiness *startup gate* is the
  B1 gap; the routes themselves exist.)
- **`/fleet/status` region/worker/uptime/in_flight/version** (`fleet.py`); `LANCELOT_REGION`
  + region-pinned routes (`@app.get('/x', region=...)`). The multi-region *plumbing* HAVE.

---

## REFUSE (Tier-D / cardinal-rule violations / out of scope)

| Item | Why |
|---|---|
| Full `lancelot init --docker --k8s --lambda` scaffolder + `templates/` system (WBS U37) | The ledger REFUSES a built-in CLI (FastAPI delegates to `fastapi-cli`; uvicorn works). B2 keeps ONLY the 3-line `__main__` entrypoint + copy-paste docs — anything that grows into an arg-parsing CLI/template generator has crossed the line. |
| `io-uring` opt-in feature flag (Robyn `Cargo.toml:53`) | Lancelot is hyper/tokio, not actix; `tokio-uring` needs its own incompatible runtime. 183k const tier + uvloop already beat Robyn without it. Tier-D unless a measured epoll-overhead win appears. (`01_robyn/teardown.md §M8`.) |
| `dill`/`multiprocess` fork + pickled socket fd (Robyn `processpool.py`) | Lancelot's no-fork "N independent processes + `SO_REUSEPORT`" model is cleaner and dep-free. Adopting fork would ADD a dep and complexity. Already ahead. (`01_robyn/teardown.md §M19`.) |
| `ROBYN_BROWSER_OPEN` dev-ergonomics knob | Pure dev ergonomics, no byte path. Tier-D by lens. (`01_robyn/python_bridge.md §12`.) |
| `rustimport` user-Rust integration | Operator-DX niche, runtime dep, no byte path. (`01_robyn/teardown.md §M17`.) |
| Cross-process shared-memory in-flight counter (`mp.Value`, LitServe) | Linux-only + fragile; `/fleet/status` aggregation is the deliberate Lancelot answer (I2 adds queue_depth to it). (`02_fastapi/proj_serving.md §21`.) |
| Cross-worker pub/sub + leader election; cross-process model-worker broker | Ledger §1 REFUSE (external bus / hosting the model = user-side). |
| `strip=true` in release profile | Marginal binary-size only; maturin/abi3 packaging handles distribution size. Not a byte-path or operator win worth a profile change. (Robyn `§M11`.) |

---

## DEFERRED (proposed elsewhere, gated on wall-clock infra — not refused, not buildable now)

- **T3.1 multi-region deploy recipes** (Render multi-region + AWS multi-AZ + Fly.io) — needs
  ~9-12h wall-clock provisioning + 1h soak each. (`05_..._ledger §2`, CLAUDE.md M7.)
- **T2.6 multi-region session-affinity verification** — gates on T3.1. The *primitives*
  (`Response.set_session_affinity`, region-pinned routes, `spawn_child(session_affinity=)`)
  are HAVE; only the live multi-region *verification* is deferred.

---

## Build order (deployment domain, brutal-prioritized)

1. **B1 (three-state `/readyz`)** — code-verified bug, docs already claim it's done, S effort,
   high deploy-and-roll correctness. **Do first.**
2. **B3 (drain-timing doc rule)** — docs-only, prevents silent SIGKILL-mid-drain. Pair with B1.
3. **B2 (`python -m lancelot run` entrypoint + PaaS docs)** — closes the Railway/Render
   zero-config friction; keep to one file + docs (Tier-D wall).
4. **B4 (`LANCELOT_BODY_TIMEOUT_MS` → 408, default off)** — closes the post-header slow-loris
   variant; validate under load.
5. **I2 (`queue_depth` on `/fleet/status`)** — one-liner LB signal, ride the next fleet touch.
6. **I1 (`bake_global_headers` in Prebaked)** — opt-in operator convenience, minor.
7. **I3 (free-threaded wheels, build-only)** — packaging readiness; bench-gate any perf claim.

**Positioning guard:** none of these are throughput pitches. B1/B3/B4 are *resilience/
correctness*; B2/I1/I2 are *operator experience*; I3 is *packaging*. Lead the deployment
narrative with what already ships (graceful drain ahead of Robyn, cgroup workers, abi3
wheels, honest multiprocess matrix) — these items just remove the last operator surprises.
