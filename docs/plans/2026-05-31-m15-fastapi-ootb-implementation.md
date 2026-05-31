# M15 — FastAPI-corpus out-of-the-box optimizations — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (impl subagent per batch → one combined recheck subagent). Subagents MUST use superpowers:test-driven-development. Rust tasks MUST use the Rust skills (unsafe-checker, coding-guidelines, m07-concurrency, m10-performance) and our Rust patterns in `docs/peer-source-recheck/M7-RUST-PATTERNS.md` + the cited teardowns in `researchfms/teardowns/`.

**Goal:** Implement the critic-adjusted out-of-the-box optimizations mined from the 24-project FastAPI corpus — fix 3 confirmed bugs and finish the byte-path patterns Lancelot already half-built — so Lancelot is better by default.

**Architecture:** Surgical changes to the existing ASGI seam (`lancelot/asgi.py`), metrics (`metrics.py`), app surface (`app.py`), routing/openapi, batch/stream/background pools, and two Rust touches (`src/stream.rs`, `src/router.rs`). No new runtime deps (cardinal rule #3: `pydantic>=2.7` only). Every change either changes the byte path or the operator experience; nothing AI-logic (positioning lock).

**Tech stack:** Python 3.13 + pyo3/hyper/tokio Rust core. Tests = pytest (in-process `TestClient` AND subprocess+HTTP real-server tests for transport/streaming/lifecycle). Conformance = `make m1-conformance` (floor `passes=974`).

**Source of truth for evidence:** `docs/plans/2026-05-31-fastapi-corpus-mine-ootb-optimizations.md` (the mine report + §6 critic verdicts). This plan is the executable distillation.

---

## Shared context (give to EVERY subagent)

**Tool affirmation (anti-hallucination):** You have FULL tool access (Read, Grep, Glob, Bash, Edit, Write). Read the ACTUAL files and verify every line number against live code before editing — anchors below were captured 2026-05-31 and may have drifted. "Pure analysis / do not implement" never appears in your prompt; you ARE implementing.

**Positioning lock (do not drift):** Lancelot optimizes the HTTP **byte path** (transport/serialization/streaming/resilience/observability) for AI/cloud/web/startups. **AI logic stays user-side** (agent loops, vector stores, prompt orchestration, model routing). Every change must touch the byte path or the operator experience — else it doesn't ship (cardinal rule #1).

**Cardinal rules that bind this work:**
1. No bullshit / no useless logic.
2. **Validate under REAL load, not just unit tests** — any transport/streaming/lifecycle feature gets a real-server (subprocess + HTTP) adversarial test exercising the path the bug lives on (the *awaiting* handler, the *disconnecting* client, the *flooded* cap). Green `TestClient` ≠ works under load (this rule caught 4 broken headline features).
3. Runtime dep = `pydantic>=2.7` only. Everything else lazy-imported/test-only. Never make `import lancelot` pull httpx/otel/etc.
4. Vendored FastAPI tests in `conformance/vendor/fastapi/` are SACRED — never edit them.
5. `researchfms/.git` is OFF-LIMITS — always `git -C` for the Lancelot repo.

**Gate (run after every task; ALL must be green before the task is "done"):**
```bash
.venv/bin/python -m pytest tests/ -q                                   # internal suite
make m1-conformance                                                     # FastAPI byte-parity, floor passes=974
$HOME/.cargo/bin/cargo clippy --release --all-targets -- -D warnings    # Rust lint (if Rust touched)
$HOME/.cargo/bin/cargo test --release --lib                            # Rust unit (if Rust touched)
```
After ANY Rust (`src/*.rs`) change, rebuild the wheel before pytest: `PATH="$HOME/.cargo/bin:$PATH" .venv/bin/maturin develop --release`.

**Commit discipline:** frequent commits, one per task (TDD: test → fail → impl → pass → commit). Commit message ends with the Co-Authored-By trailer. Work on branch `m15-fastapi-ootb-optimizations` (already created; NOT main).

**Rust-pattern sources (Rust tasks MUST consult):** `docs/peer-source-recheck/M7-RUST-PATTERNS.md` — esp. ZED `select_biased!`+Drop-guard graceful shutdown (`researchfms/teardowns/ZED.md:4826-4861`), Drop-as-cancel (universal), `cfg(test)` buffer=1 backpressure (`ZED.md:6157-6165`); LANCEDB router/error patterns (`researchfms/teardowns/LANCEDB.md`); DATAFUSION mpsc streams + JoinSet Drop-cancel (`researchfms/teardowns/DATAFUSION.md:1983-2018`). Honor the AVOID table (no new deps, no custom executor, no compression).

---

## File map

| File | Tasks | Responsibility |
|---|---|---|
| `lancelot/metrics.py` | T1 | RED + TTFT metric emit; route-template label + sentinels |
| `lancelot/asgi.py` | T1,T4,T6,T7,T10 | the ASGI seam: metric caller, mount lifespan, disconnect watcher, stream framing, WS contextvars |
| `lancelot/app.py` | T2,T4,T5,T6,T7,T9,T12 | App surface: openapi factory, mount, lifetime_task, drain, kwargs |
| `lancelot/openapi.py` | T2 | operationId/openapi_extra in build_openapi |
| `lancelot/routing.py` | T2,T6,T7,T12 | Route fields, route_class, tier eviction |
| `lancelot/background.py` | T3 | detached spawn context propagation |
| `lancelot/requests.py` | T6 | `is_disconnected()` alias |
| `lancelot/stream.py` | T7 | SSE model framing + [DONE] |
| `lancelot/batch.py` | T9 | budget knobs + window snapshot |
| `lancelot/exceptions.py` | T11 | ProblemDetail helper |
| `src/stream.rs` | T7 | mirror SSE coercion + EOF in the R3 pump |
| `src/router.rs` | T12 | remove/rebuild (matchit has no delete) |
| `docs/operator/openai-compat.md` (new) | T8 | OpenAI-compat user recipe (NO first-party models) |
| `tests/test_m15_*.py` | all | unit + real-server tests |

---

## BATCH 1 — build-time bug fixes (lowest risk, no load gate)

### Task T1 (#2): Metric label = route TEMPLATE not concrete URL, + TTFT twin + bounded 404/405 sentinels

**Files:** Modify `lancelot/asgi.py` (`_http` finally ~310, `_http_dispatch` route-match ~351-357, stream TTFT ~746-749), `lancelot/metrics.py` (docstrings). Test `tests/test_m15_metric_cardinality.py`.

**Confirmed current state (verify live):** `asgi.py:179 path = scope["path"]` (concrete URL); `asgi.py:310 record_request(path, ...)`; `metrics.py:134` stores it as `"http.route"`. **TWIN:** `asgi.py:746-749 record_stream_ttft(... path ...)` → `metrics.py:171` stores the same concrete path as `"http.route"`. The matched `Route` (with `.path` = template) is resolved in `_http_dispatch` (~asgi.py:351-357) but never threaded back to the `_http` finally.

**Change:**
1. In `_http_dispatch`, right after the route is matched, stash the template: `scope["_lancelot_route"] = route.path`.
2. At the 404 and 405 emit sites (no route matched), stash a **bounded sentinel**: `scope["_lancelot_route"] = "__not_found__"` (404) / `"__method_not_allowed__"` (405). (Critic: the 404 path never reaches a Route stash, so a URL-spraying scanner explodes cardinality unless the sentinel is wired at the 404/405 emit site or defaulted in the finally.)
3. In the `_http` finally, read `route_path = scope.get("_lancelot_route") or "__unmatched__"` and pass THAT to `record_request` instead of the concrete `path`.
4. Apply the identical fix to the `record_stream_ttft` call (the twin) — pass the template, not `path`.
5. Update `metrics.py` docstrings to document `http.route` is the template + the sentinel set.

**Investigate + document (the critic's open question):** Determine whether `record_request`/`record_stream_ttft` are also called on the **Rust `_native.run` server path** (`src/service.rs`) or only on the Python ASGI seam. Grep `src/` for any metrics emission. Write the answer (1 paragraph) into the task's recheck notes AND as a comment near `record_request`: if metrics only fire on the Python ASGI seam (not the Rust server prod deploys multiprocess), say so explicitly — it scopes the win.

**Tests (TDD, write first):**
- `test_metric_label_is_route_template`: hit `/items/{id}` with `/items/42` and `/items/43`; assert the recorded `http.route` attribute == `/items/{id}` for both (capture via a fake OTel meter or `metrics`-module seam — see existing `tests/` patterns for how metrics are asserted; reuse `reset_for_tests`).
- `test_metric_404_uses_sentinel`: GET `/no-such-path-{random}`; assert `http.route == "__not_found__"` (NOT the concrete URL).
- `test_metric_405_uses_sentinel`: wrong method on an existing path; assert `__method_not_allowed__`.
- `test_stream_ttft_label_is_template`: a streaming route at `/s/{id}`; assert the TTFT histogram's `http.route` == `/s/{id}`.

**Acceptance:** all 4 tests pass; full gate green (esp. conformance 974 — the hot `finally` must not regress); the Rust-server-path question is answered in writing.

**Positioning:** transport/observability hygiene — bounding label cardinality. ~3h.

---

### Task T2 (#11): Honor `operation_id` / `generate_unique_id_function` / `openapi_extra` in OpenAPI gen (dead-code bug)

**Files:** Modify `lancelot/openapi.py` (build_openapi, operationId fallback ~280), `lancelot/app.py` (capture the factory from `_extra_kwargs` at `__init__` ~48-53), `lancelot/routing.py` (add `openapi_extra` Route field; `operation_id` already stored ~65). Test `tests/test_m15_openapi_overrides.py`.

**Confirmed current state (verify live):** `openapi.py:280` always calls the hardcoded `operation_id(r)` formula; `route.operation_id` is stored (`routing.py:65`) but **never consumed**; `generate_unique_id_function` exists only in the `app.py:38` docstring and is swallowed into `_extra_kwargs` (`app.py:53`); `openapi_extra` has nowhere to land.

**Change:**
1. `App.__init__`: capture `self._generate_unique_id_function = kwargs.pop("generate_unique_id_function", None)` (before the accept-and-store of `_extra_kwargs`), default `None`.
2. `build_openapi`: the operationId for a route `r` is, in order: `r.operation_id` (verbatim, if set) → `app._generate_unique_id_function(r)` (if set) → the existing formula. (Pass `app` / the factory into `build_openapi`.)
3. Add `openapi_extra: dict | None = None` as a Route field (`routing.py`), settable via the route decorator kwargs; in `build_openapi`, shallow-merge `r.openapi_extra` into that path-operation's dict (after the generated keys, so it can override).
4. The OpenAPI schema is memoized (`app.py:50 _openapi_cache`, built once) → build-time only, zero hot-path cost.

**Tests (TDD):**
- `test_operation_id_verbatim`: `@app.get("/checkouts", operation_id="checkouts:create")` → `/openapi.json` has `operationId == "checkouts:create"` (NOT the formula `create_checkout_checkouts_get`).
- `test_generate_unique_id_function`: `App(generate_unique_id_function=lambda r: f"x_{r.name}")` → all operationIds use it.
- `test_operation_id_precedence`: per-route `operation_id` beats the app factory.
- `test_openapi_extra_merged`: `@app.get("/x", openapi_extra={"x-speakeasy-name-override": "list"})` → that key is present in the op dict.

**Acceptance:** 4 tests pass; gate green. Build-time only, no load gate.

**Positioning:** the operationId is the generated SDK/MCP method name — directly the AI/SDK-codegen audience; pure `/openapi.json` bytes. ~4h.

---

### Task T3 (#12): Detached `background.spawn` propagates request context

**Files:** Modify `lancelot/background.py` (`BackgroundPool.spawn` ~52-70, and the module-level `spawn` ~89-96). Test `tests/test_m15_background_context.py`.

**Confirmed current state (verify live):** `background.py:58-66 _run` submits to the ThreadPoolExecutor with NO `contextvars.copy_context()`. `pool.py:48` (offload) and `pool.py:68` (run_backpressured) BOTH do `ctx = _cv.copy_context()` then `ctx.run(fn, ...)`. So a detached `spawn()` runs in an empty context → reads `'-'`/`0.0` sentinels for request_id/trace_id/tenant_id. The asymmetry (offload propagates, spawn doesn't) is the bug.

**Change:** In `BackgroundPool.spawn`, capture `ctx = contextvars.copy_context()` at spawn-time (synchronously, in the caller's context — BEFORE the executor submit) and run the body via `ctx.run(fn, *args, **kwargs)` inside `_run` (mirror `pool.py:48`). Import `contextvars` at module top or locally as pool.py does. Keep the `_depth` accounting and `QueueFull` behavior unchanged. Keep refusing durability/retry (that's the user's queue).

**Tests (TDD):**
- `test_spawn_propagates_request_id`: set `lancelot.context.request_id` to a sentinel value, `spawn` a fn that records `request_id.get()` into a threading.Event-guarded holder; assert the spawned fn observed the SAME request_id (today it observes `'-'`).
- `test_spawn_propagates_trace_and_tenant`: same for trace_id + tenant_id.
- `test_spawn_still_bounded`: spawning past the ceiling still raises `QueueFull` (no regression).

**Acceptance:** 3 tests pass; gate green. Trivial; no load gate (one real-request assertion suffices).

**Positioning:** pure context propagation — Lancelot owns these ContextVars; propagating them into its own detached pool is byte-path. ~2h.

---

## BATCH 2 — lifecycle / composition

### Task T4 (#3): Run mounted sub-app lifespans + `root_path` propagation (latent correctness bug)

**Files:** Modify `lancelot/asgi.py` (`_lifespan` ~101-159; dispatcher root_path ~84-88), `lancelot/app.py` (`mount` ~102-106 record lifespan-bearing sub-apps). Test `tests/test_m15_mount_lifespan.py` (real-server subprocess).

**Confirmed current state (verify live):** dispatcher delegates http/ws to `_mounts` (asgi.py:79-89) but `_lifespan` (asgi.py:101-159) drives ONLY `app._lifespan` — a mounted sub-app's startup/shutdown NEVER runs under `serve()`. `mount()` just appends `(prefix, app)`. `asgi.py:84-88` strips the prefix but never sets `sub_scope["root_path"]`.

**Change:**
1. In `_lifespan`, after entering the root app's lifespan, iterate `app._mounts`; for each mounted sub-app that exposes a lifespan (ASGI `lifespan` protocol OR a `._lifespan`/Starlette `router.lifespan_context`), enter it on an `AsyncExitStack` (so teardown is LIFO/reverse order). Guarantee partial-startup safety: if a sub-app raises during startup, only the already-entered ones tear down (the AsyncExitStack guarantee).
2. In the dispatcher, set `sub_scope["root_path"] = mount_prefix` (the stripped prefix) so the sub-app emits correct absolute URLs (its `/openapi.json` server URL, `url_path_for`).
3. **Scope trim (critic):** DROP the optional `app.host(domain, subapp)` host-header-routing extra — net-new surface, not a bug fix, no recurrence number. Bug-fix half only.

**Tests (TDD, REAL-SERVER subprocess — load gate):**
- `test_mounted_subapp_lifespan_runs`: subprocess `serve()` a parent app with a mounted sub-app whose lifespan increments a startup counter and decrements on shutdown (write the counter to a file/port the test reads); hit it, SIGTERM, assert BOTH startup and shutdown fired (today: neither).
- `test_mounted_subapp_root_path`: assert the sub-app's `/openapi.json` server URL / a `url_path_for` reflects the mount prefix.
- `test_partial_startup_safety`: a sub-app that raises on startup tears down only what entered (no teardown of un-entered sub-apps; parent still shuts down cleanly).

**Acceptance:** real-server tests pass; gate green.

**Positioning:** ASGI-composition correctness — silent sub-app resource non-init is a real bug. ~5h (bug-fix half).

---

### Task T5 (#6-core): `app.lifetime_task(coro_fn)` — supervised app-lifetime daemon

**Files:** Modify `lancelot/app.py` (register tasks; extend `drain()` ~585 to cancel+await them), `lancelot/asgi.py` (`_lifespan` startup starts the tasks). Test `tests/test_m15_lifetime_task.py` (real-server subprocess).

**Confirmed current state (verify live):** no `app.lifetime_task`/`add_lifecycle`/`app.resource` exists; `background.py spawn` is detached fire-and-forget (not lifecycle-bound); `drain()` (~585) handles graceful drain but doesn't cancel app-owned daemons.

**Change:** Add `app.lifetime_task(coro_fn)` — registers a coroutine factory. At lifespan startup, `asyncio.ensure_future(coro_fn())` for each, retain a strong ref (so it isn't GC'd). In `drain()` (and shutdown), cancel each task and `await` it bounded by `LANCELOT_DRAIN_SECS` (reuse the existing drain-deadline machinery), suppressing `CancelledError`. No "Task was destroyed" warnings; no orphans. (Mirror the graceful-teardown discipline already in `serve()`/`graceful_loop_teardown`.)

**Scope trim (critic):** DEFER the typed `app.resource(name, factory)` injection tier and the `add_lifecycle` ladder — they touch the DI compile path (hot-path-adjacent, conformance-sensitive). `app.state` already covers cross-context singleton reads today. Ship ONLY `lifetime_task` (it directly extends graceful drain and is the dependency the deferred items would hang off).

**Tests (TDD, REAL-SERVER subprocess — load gate):**
- `test_lifetime_task_runs_and_is_cancelled_on_drain`: register a `lifetime_task` that loops incrementing a counter; subprocess serve; confirm it's running (counter advances); SIGTERM; assert the task was cancelled + awaited within `LANCELOT_DRAIN_SECS`, no "Task was destroyed" warning in stderr, in-flight requests still drained (don't regress B1).
- `test_lifetime_task_strong_ref`: the task isn't GC'd mid-run (counter keeps advancing across a GC).

**Acceptance:** real-server tests pass; gate green; B1 graceful drain not regressed.

**Positioning:** operator-experience/resilience — the supervised-daemon plumbing every inference server rebuilds; the coro is the user's, Lancelot owns the bounded-cancel-on-drain. ~6h.

---

## BATCH 3 — the marquee: non-streaming disconnect cancellation (solo, load-gated, regression-sensitive)

### Task T6 (#1): Extend C2 disconnect→cancel to non-streaming handlers + `cancel_on_disconnect` + `request.is_disconnected()`

**Files:** Modify `lancelot/asgi.py` (factor `_watch_disconnect` out of the is_stream branch ~659-685; arm it on the non-stream path ~853-878 and the custom-route emit path), `lancelot/routing.py` (`cancel_on_disconnect` route kwarg), `lancelot/app.py` (per-app default), `lancelot/requests.py` (`is_disconnected()` alias → `self.cancelled`). Reuse `lancelot/llm.py:353-383 _await_cancellable` (the existing dual-task FIRST_COMPLETED race). Test `tests/test_m15_unary_cancel.py` (real-server subprocess).

**Confirmed current state (verify live):** `_cancel_event` is created ONLY inside `if getattr(route, "is_stream", False):` (asgi.py:662). The non-stream path (~asgi.py:853-878) and the custom-route emit path never create it, so `llm.py:358 _await_cancellable` reads `None` → zero-overhead pass-through → a unary handler's in-flight upstream is NEVER cancelled on disconnect. The reusable dual-task race exists at `llm.py:353-383`; the watcher exists at `asgi.py:671-685`.

**Change:**
1. Factor `_watch_disconnect` into a shared helper (module-level or a small closure factory) usable by BOTH the stream branch and the unary path.
2. Add `cancel_on_disconnect: bool = False` as a route kwarg (`routing.py`) + an `App(...)`-level default.
3. On a route with `cancel_on_disconnect=True` (or app default) that is **async or has a `timeout=`**, spawn the watcher, set `lreq._cancel_event`, and race the handler task vs. the disconnect via the existing `_await_cancellable` shape (or an equivalent FIRST_COMPLETED on the handler coroutine).
4. **CRITICAL (critic):** a cancelled unary handler must still run yield-dependency teardown — feed `CancelledError` into the existing `solve_async` AsyncExitStack `__aexit__` (the same B4 semantics the per-route `timeout=` already preserves at ~asgi.py:840-846). A cancelled handler must NOT skip DB-rollback/file-close.
5. **CRITICAL (critic, fast-path gate):** Do NOT arm the `receive()`-polling watcher on routes WITHOUT `cancel_on_disconnect` — the tiny one-shot path (59k req/s, the shape closest to Starlette+uvloop) must not regress. Only async/timeout'd cancel routes pay for the watcher.
6. Add `Request.is_disconnected()` (async) → returns `self.cancelled` (FastAPI-named alias for migrating users); generalize the `requests.py:39-44` docstring (currently says "mid-stream").

**Tests (TDD, REAL-SERVER subprocess — MANDATORY load gate):**
- `test_unary_handler_cancelled_on_disconnect`: subprocess server, a `@app.post("/x", cancel_on_disconnect=True)` async handler that starts a long `await` (sleep / fake upstream). A raw-socket client opens the POST, then HARD-CLOSES the socket mid-flight. Assert the handler task was CANCELLED (not run to completion) — e.g. the handler's `finally`/teardown recorded a cancellation, and the fake upstream task was cancelled.
- `test_unary_cancel_runs_yield_dep_teardown`: a yield-dep (acquire→yield→release) on the cancelled route; assert the release ran (teardown not skipped).
- `test_non_cancel_route_not_armed`: a route WITHOUT the flag completes normally even if the client is slow; (and document/measure that no watcher task is spawned for it).
- **SHIP GATE (run, record numbers):** `benchmarks/run_load.sh` tiny-shape before/after — the tiny one-shot number for NON-cancel routes must be within noise. Record the delta in the recheck notes. If it regresses, the watcher is being armed too broadly — fix before shipping.

**Acceptance:** real-server cancel test passes; teardown test passes; tiny-shape bench within noise (recorded); gate green.

**Positioning:** pure transport lifecycle — stop server work + stop paying for upstream tokens the instant the socket closes. Agent loop untouched. ~8h.

---

## BATCH 4 — SSE wire framing + OpenAI recipe (Rust touch)

### Task T7 (#5): SSE-of-models framing + `done_sentinel='[DONE]'`

**Files:** Modify `lancelot/stream.py` (model→`data: <json>\n\n` framing), `lancelot/asgi.py` (`_stream_chunk_bytes` ~327-343 + EOF sentinel in the is_stream loop), `lancelot/routing.py`/`lancelot/app.py` (`sse=`/`done_sentinel=` kwargs), `src/stream.rs` (mirror the coercion + EOF in the R3 pump). Test `tests/test_m15_sse_framing.py` (real-server EventSource).

**Confirmed current state (verify live):** `stream.py Event.render()` owns `event:`/`data:` framing with CR/LF/NUL scrub; `asgi.py:327-343 _stream_chunk_bytes` coerces bytes/str/Event, but a bare Pydantic model falls to the `json.dumps(...)+"\n"` newline-delimited path (~asgi.py:342-343) — NOT `data: <json>\n\n` (wrong shape for an SSE/EventSource client). No `[DONE]` sentinel anywhere (grep confirms zero in `lancelot/`).

**Change:**
1. `@app.stream(sse=True, done_sentinel="[DONE]")`: when `sse=True`, a yielded model/dict is framed as `data: <pydantic-core json>\n\n` via the Rust `serialize` path (`serialize::to_json_bytes` — NOT Python `model_dump_json`), and on generator exhaustion the framework auto-emits `data: <done_sentinel>\n\n` (default `[DONE]`; `None` disables).
2. Event/bytes/str yield paths stay BYTE-IDENTICAL (backward compatible) — only the bare-model path and the EOF sentinel change, and only under `sse=True`.
3. Mirror the coercion + EOF sentinel in `src/stream.rs` so the native R3 pump produces the same bytes (Rust task — use Rust skills + M7-RUST-PATTERNS.md; preserve the same-thread `try_push` + bounded-backpressure invariant; `intern!` for any hot attr lookups per pattern #13; do NOT regress the R3 pump — see CLAUDE.md Tier-D "don't regress R3").

**Tests (TDD, REAL-SERVER — load gate):**
- `test_sse_model_framing`: `@app.stream(sse=True)` yielding Pydantic models; a real EventSource/raw-socket client reads exactly `data: <json>\n\n` frames (one per model).
- `test_sse_done_sentinel`: the last frame on clean exhaustion is `data: [DONE]\n\n`; `done_sentinel=None` disables it.
- `test_sse_backward_compat`: yielding `Event`/bytes/str under existing usage is byte-identical to before (regression).
- `test_sse_disconnect_midstream`: client disconnects mid-stream → generator left suspended (≤1 extra chunk per the R3 contract); server still serves the next request.

**Acceptance:** real-server SSE tests pass; Rust pump parity confirmed; R3 not regressed; gate green (incl. cargo clippy/test + wheel rebuild).

**Positioning:** SSE wire framing + Rust-core per-chunk serialization (the streaming marquee); the token-producing generator stays user-side. NOT the Tier-D "optimize @app.stream per-chunk overhead" item (no perf claim). ~6h.

---

### Task T8 (#4): OpenAI-compatible serving as a documented USER RECIPE (no first-party models)

**Files:** Create `docs/operator/openai-compat.md`. Possibly a tiny example under `docs/`/examples. NO new `lancelot/openai_compat.py`, NO first-party `ChatCompletion(Chunk)` Pydantic models.

**Critic REVISE (binding):** Do NOT ship a first-party OpenAI-schema shim — the OpenAI request/response schema is a model-API contract, not a transport format; shipping `ChatCompletionChunk` Pydantic shapes pulls Lancelot toward a model gateway, which is Tier-D. Instead, document the recipe: compose `@app.stream(sse=True, done_sentinel="[DONE]")` (from T7) + `pool.offload` for a sync `run` + the user's OWN `openai`-python response models + a `/v1/models` route the user writes. Model resolution + token generation stay 100% user-side — exactly how `AutoCommitAPIRoute` is documented as a recipe, not first-party.

**Change:** Write `docs/operator/openai-compat.md`: a complete, copy-pasteable recipe for an OpenAI-compatible `/v1/chat/completions` + `/v1/models` server on Lancelot, using only shipped primitives (T7 SSE framing, `pool.offload`, `cancel_on_disconnect` from T6, the user's own pydantic models). Explain WHY Lancelot ships the wire primitive but not the OpenAI models (positioning).

**Tests:** none (docs). Optionally a doctest-style example that imports cleanly. Acceptance: recipe is runnable as written (spot-check by following it); positioning rationale stated. ~2h.

---

## BATCH 5 — batch budget knobs + WS observability seam + error helper

### Task T9 (#9): `@app.batch` budget knobs (size_fn + window snapshot + defer-to-tail; max_keys→503)

**Files:** Modify `lancelot/batch.py` (`_BatchGroup.submit` ~57, `_run_batch`/timer ~68, max_keys path ~150), `lancelot/app.py:279` (kwargs). Test `tests/test_m15_batch_budget.py` (+ `benchmarks/run_batch.sh` load gate).

**Confirmed current state (verify live):** `batch.py:57` reads `self.max_batch` LIVE each submit and compares only `len(self.pending)`; `batch.py:68` is a fixed `asyncio.sleep(window_s)`; `app.py:279-281` signature is `batch_window_ms`/`max_batch`/`max_keys`/`key` only — no `size_fn`, no per-batch window snapshot. `batch.py:150` raises `RuntimeError` on max_keys (→500).

**Change:**
1. Snapshot `window_s` + `max_batch` into the `_BatchGroup` at first-submit, so a future live retune can't corrupt a half-built batch.
2. `size_fn: Callable[[list], int] | None` kwarg — when set, the flush trigger is a byte/token BUDGET (`size_fn(pending) >= max_size`) not just a count. (`size_fn` is user-supplied → no tokenizer ships, stays in-lane.)
3. Defer an over-budget item to the NEXT batch instead of overflowing the current one. **Critic (idempotency):** ensure exactly-once fanout under the defer path — a deferred item must not be double-counted if `max_keys` eviction races the defer or the first batch partially fired. Test this explicitly.
4. Make the `max_keys`-hit return **503 + Retry-After** (a sheddable load signal), not the current 500 (`RuntimeError`).

**Scope trim (critic):** DEFER the adaptive learned `max_latency` SLA-hold (bentoml `lstsq`) — larger, edges toward orchestration. Budget knobs only.

**Tests (TDD; load gate via run_batch.sh):**
- `test_size_fn_caps_bytes_not_count`: with `size_fn=len-of-bytes` and a small `max_size`, a batch flushes on byte budget before `max_batch` count.
- `test_window_snapshot_survives_retune`: mutate `max_batch` mid-window; the in-flight batch uses the snapshot, not the new value.
- `test_over_budget_item_deferred_exactly_once`: an over-budget item goes to the next batch and is fanned out exactly once (assert no double-fire even when max_keys eviction races).
- `test_max_keys_returns_503`: exceeding `max_keys` returns 503+Retry-After (not 500).
- **Load gate:** `benchmarks/run_batch.sh` against the rate-limited upstream still shows the ~12× throughput / coalescing win; record numbers.

**Acceptance:** tests pass; run_batch.sh win preserved; gate green.

**Positioning:** request-scheduling/coalescing-budget math on the transport seam — the framework owns WHEN to fire; the upstream call stays user-side. ~5h.

---

### Task T10 (#10-core): WebSocket ContextVar/traceparent observability seam

**Files:** Modify `lancelot/asgi.py` (factor the `_http` header-scan ~216-237 into a shared helper; call from `_ws` ~1248-1320 inside try/finally). Test `tests/test_m15_ws_context.py` (real-server WS).

**Confirmed current state (verify live):** `_ws` (asgi.py:1248-1320) never sets ANY ContextVar — `request_id`/`trace_id`/`agent_id` are set only in `_http` (asgi.py:216-237). So a WS connection gets no request-id echo, no traceparent propagation. (The handshake DOES run `solve_async`, so Depends/Security already resolve — auth is achievable today via deps.)

**Change:** Factor the `_http` header-scan + ContextVar-set (request_id honor-else-mint, traceparent parse, agent_id) into a shared helper. Call it from `_ws` inside a try/finally: set the ContextVars on handshake, RESET on disconnect (no leak across connections). This makes `TransportPool._inject_traceparent` forward the socket's trace to upstream calls made from inside a WS handler — HTTP machinery extended to WS.

**Scope trim (critic):** DEFER the declarative handshake-auth surface (`QueryToken`/`WSCookie`/`WSHeader` + close codes) AND the `@app.middleware('websocket')` kind — net-new auth/middleware surface, not the observability fix; Security DI already resolves at handshake.

**Tests (TDD, REAL-SERVER WS — load gate):**
- `test_ws_request_id_echoed`: a WS client connects with `x-request-id: abc`; assert it's honored (a handler reading `context.request_id` sees `abc`); a connection without it gets a minted id.
- `test_ws_traceparent_forwarded`: a WS handler makes an upstream `TransportPool` call; assert the socket's traceparent is forwarded.
- `test_ws_contextvars_reset_across_connections`: two sequential WS connections don't leak request_id (each sees its own / a fresh mint).

**Acceptance:** real-server WS tests pass; gate green.

**Positioning:** observability/lifecycle symmetry — request-id echo + traceparent propagation are transport plumbing already shipped for HTTP. ~6h.

---

### Task T11 (#7-minimal): Opt-in `ProblemDetail` (RFC 9457) + retry-taxonomy convenience

**Files:** Modify `lancelot/exceptions.py` (ProblemDetail helper). Test `tests/test_m15_problem_detail.py`.

**Confirmed current state (verify live):** exception hooks exist (`app.py:560 add_exception_handler`/`@app.exception_handler`; `asgi.py _find_exc_handler` MRO-walk; `responses.py set_retry_hint → X-Should-Retry`). No `problem+json`/`ProblemDetail`/`error_envelope` (grep zero). Default emits `{"detail": ...}`.

**Critic REVISE (binding):** Do NOT ship a bespoke `{error:{type,message}}` envelope — no consensus across the corpus (11/24 each chose different shapes), and it risks the `{"detail":...}` byte-parity that IS the 99.6% conformance story. Ship instead a thin, OPT-IN `lancelot.exceptions.ProblemDetail` (RFC 9457 — at least a STANDARD shape) that the user routes via `response_class`/raises explicitly, and fold `set_retry_hint` into a small documented retry-taxonomy convenience. Keep the DEFAULT path byte-identical to FastAPI. Skip the stream-error-frame-through-handler sub-item (the constant "internal server error" is the safe default).

**Change:** Add `lancelot.exceptions.ProblemDetail` — a helper that produces an RFC 9457 `application/problem+json` body (`type`/`title`/`status`/`detail`/`instance`) when a user opts in (raises it or returns it via a response class). Add a small retry-taxonomy convenience that wires `set_retry_hint`/`Retry-After` to a status taxonomy. Default behavior (plain `HTTPException` → `{"detail":...}`) UNCHANGED.

**Tests (TDD):**
- `test_problem_detail_shape`: raising/returning `ProblemDetail(...)` emits `application/problem+json` with the RFC 9457 fields.
- `test_default_error_unchanged`: a plain `HTTPException(404, "nope")` still emits `{"detail":"nope"}` (byte-identical — conformance guard).
- `test_retry_taxonomy_sets_headers`: the retry convenience sets `Retry-After`/`X-Should-Retry` as documented.

**Acceptance:** tests pass; conformance 974 unchanged (default error path byte-identical); gate green. ~4h.

---

## BATCH 6 — live route add/remove + Rust router rebuild (solo, riskiest, Rust)

### Task T12 (#8): Live route add/remove with const-tier eviction + OpenAPI cache-bust + Rust matchit rebuild

**Files:** Modify `lancelot/routing.py` (`Router.remove` + tier eviction), `lancelot/app.py` (`add_route_live`/`remove_route` + cache bust ~528-538), `src/router.rs` (remove/rebuild). Test `tests/test_m15_live_routes.py` (real-server) + `src/router.rs` `#[cfg(test)]`.

**Confirmed current state (verify live):** no `remove_route`/`add_route_dynamic`. The Router keeps SEPARATE tiers `_const`/`_const_paths`/`_dyn` (`routing.py:637-668`) — removing from `.routes` alone leaves a stale O(1) const-tier hit serving a dead route. OpenAPI is memoized with no invalidation (`app.py:528-538`). `src/router.rs add()` only inserts (matchit returns Conflict on dup; **no delete**) — and the prebaked const tier ALSO lives in Rust (`src/lib.rs` const_routes → `src/service.rs` Prebaked) on the `_native.run` server.

**Change:**
1. `Router.remove(path, method=None)` + `App.remove_route` / `App.add_route_live` that evict from BOTH Python tiers (`_const`/`_const_paths`/`_dyn`) AND `routes`, and null `app._openapi_cache` (so `/openapi.json` rebuilds).
2. `src/router.rs`: add `remove(method, path)`. Since matchit has no delete, REBUILD the per-method `Matchit` from the retained patterns. **Note:** `patterns` currently stores only path strings — extend it to retain `(path, slot)` per method (or a parallel slot map) so the tree can be rebuilt with correct slots. Use Rust skills (unsafe-checker if any unsafe — there shouldn't be; coding-guidelines; m07-concurrency for the `#[pyclass] Arc<Mutex<>>` interior mutability per M7-RUST-PATTERNS.md row LANCEDB.md:2628). Add `#[cfg(test)]` tests for add→remove→re-add and remove-nonexistent.
3. **Critic (binding):** a live-removed CONST route must be evicted from the Rust prebaked set too (`src/lib.rs`/`src/service.rs`), not just the Python `_const` dict — or document clearly that live route mutation is unsupported on the prebaked `_native` const tier and applies to the Python/dynamic tier only. State which, explicitly.

**Tests (TDD; real-server + Rust unit):**
- `test_add_route_live`: add a route at runtime; it serves + appears in `/openapi.json`.
- `test_remove_route_evicts_all_tiers`: remove a route; it 404s (NOT a stale const-tier hit) and disappears from `/openapi.json`.
- `src/router.rs`: `add_remove_readd` (remove drops the match, re-add works), `remove_nonexistent_is_noop_or_err`.

**Acceptance:** tests pass; cargo clippy/test green + wheel rebuilt; const-tier eviction (or its documented exclusion) verified.

**Note on priority/positioning:** the report ranks this LAST ("ship when an inference-server user asks" — net-new dynamic surface on a hot path). It IS in scope per "fix all of them," but the recheck agent must confirm it doesn't violate cardinal rule #1 (wait-for-demand) — if the recheck judges it speculative, flag for deferral rather than force it. ~8h. Routing-table + cache surgery the framework must own.

---

## Batching & execution (subagent-driven, all Opus 4.8)

| Batch | Tasks | Impl agent | Recheck agent | Notes |
|---|---|---|---|---|
| 1 | T1, T2, T3 | 1 | 1 | build-time bugs; no load gate |
| 2 | T4, T5 | 1 | 1 | lifecycle; real-server tests |
| 3 | T6 | 1 | 1 | marquee disconnect (solo — regression-sensitive, mandatory bench gate) |
| 4 | T7, T8 | 1 | 1 | SSE framing (Rust) + OpenAI recipe |
| 5 | T9, T10, T11 | 1 | 1 | batch/WS/errors |
| 6 | T12 | 1 | 1 | live routes + Rust router (solo — riskiest, wait-for-demand check) |

**Recheck agent (one per batch) checks:** (1) bugs/correctness, (2) vulnerabilities, (3) re-verify the change against live source, (4) Rust patterns/anti-patterns vs `M7-RUST-PATTERNS.md` (Rust batches), (5) Lancelot core goals/principles (positioning lock + Tier-D + cardinal rules — esp. "no bullshit", pydantic-only runtime, no AI-logic), (6) plan alignment (did it build exactly this task, no more/less; critic scope-trims honored). Reviewer finds issue → same impl subagent fixes → re-recheck → only then mark complete. After all 6 batches: one final whole-branch reviewer, then `superpowers:finishing-a-development-branch`.

## Self-review (against the mine report §2 + §6)
- §2 items #1–#12 → T6, T1, T4, T8(recipe), T7, T5, T11, T12, T9, T10, T2, T3. All covered. ✓
- §6 critic REVISEs folded: #4→recipe-only (T8), #6→lifetime_task-only (T5), #7→ProblemDetail opt-in (T11), #10→observability-seam-only (T10). ✓
- §6 critic MISSING folded: TTFT twin + 404 sentinels (T1), unary-cancel teardown + tiny-handler bench gate (T6), Rust-server-path question (T1 investigate), batch defer idempotency (T9), const-tier eviction on Rust prebaked (T12). ✓
- Deferred (explicitly, with rationale): `app.resource` DI tier + add_lifecycle (T5), adaptive batch max_latency (T9), WS auth surface + ws-middleware-kind (T10), `app.host()` (T4), the lower-priority §2 tail (on_commit, is_leader, dynamic_route, size histograms, etc.) — out of this milestone's scope.
