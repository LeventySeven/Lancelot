# Lancelot Deep Research 07 — Stability Failure-Mode Catalog & Release-Gate Guards

> **Purpose.** The user's #1 requirement for Lancelot is "stable like FastAPI/Robyn, but faster." A Rust-speed thesis is worthless if the framework drops in-flight requests on deploy, dies on one bad input, OOMs on a slow client, or leaks an LLM connection per stream. This file is the **definitive stability failure-mode catalog**: every way the three source frameworks (FastAPI-0.136-fork, Robyn 0.85.0, Blazeio/`anonyxbiz`) break under concurrency, resource pressure, bad input, disconnect, backpressure failure, crash, shutdown, and cancellation — each with a `path:line` (or teardown-ref) citation, the failure stated, and **the exact guard Lancelot must ship**: the mechanism, the constant, and whether it lives in Rust or Python.
>
> **Method.** Mined defensively across all 3 codebases (`research/{fastapi,robyn,blazeio}/*`, 8,645 lines of source-level RE) + `research/design/01-core-performance-stability.md` (§5 stability checklist) + `SYNTHESIS.md` + `DESIGN-DECISIONS.md`. Labels: **KNOWN** = read directly from a teardown citing source `path:line`. **INFERRED** = deduced from KNOWN facts + first principles. **DESIGN** = Lancelot's prescribed guard (not present in any of the three).
>
> **How to read.** The 12-line release-gate checklist below IS the deliverable — it is the merge gate. Each numbered item maps to one §-section that gives the full evidence trail, the failure, and the reimplementable guard. A PR that regresses any checklist item does not ship.

---

## Executive Summary — The 12-Line Release-Gate Checklist (the deliverable)

A Lancelot release **does not ship** unless every one of these 12 gates is green. Each fixes a *specific KNOWN defect* in Robyn/Blazeio/FastAPI (cited in its section). This is the operational restatement of `design/01 §5.7`, expanded with the per-failure-class guards mined here.

1. **Graceful drain.** SIGTERM → stop accept → finish in-flight (bounded `graceful_timeout`=30 s) → SSE finish-current-event → run shutdown lifespan/yield-dep teardown → SIGKILL. *Rust supervisor + hyper `GracefulShutdown`.* Fixes Robyn `processpool.py:55` `process.kill()`. → §1
2. **Worker respawn + crash isolation.** Parent respawns dead children (rate-capped exp-backoff, ≤5/30 s, then alert); `catch_unwind`/unwind-scope at the FFI seam; zero-`unwrap()` clippy-deny on request-path modules. *Rust supervisor + Rust FFI seam.* Fixes Robyn no-supervisor + `panic="abort"`. → §2
3. **Header-receipt deadline + body + total timeouts.** Single-pass resumable Rust parser; `header_read_timeout`=30 s (0=disable, documented for streaming), body=60 s, total=120 s, per-route override. *Rust (`hyper::http1::Builder` + `tower-http::timeout`).* Fixes Blazeio O(n·k) re-scan / Robyn `client_request_timeout(0)`. → §3
4. **Bounded admission + load-shed.** Bounded sync-handler queue (`sync_queue_max`=10 000) + per-worker connection cap; overflow → Rust emits `503 Retry-After` *before* the Python frame; surfaced in `/metrics`. *Rust admission control + bounded `crossbeam-channel`.* Fixes FastAPI `CapacityLimiter(40)` cliff + Robyn `unbounded()` queue. → §4
5. **End-to-end streaming backpressure.** Pause-by-default receive; native async-gen pump (no per-token `spawn_blocking`/`with_gil`); time+size coalesce (~25 ms / 2 KiB); bounded SPSC ring (size-1); boolean send-gate (no Future/token). Slow client → stalled handler → stalled upstream-LLM read, bounded RAM regardless of completion length. *Rust ring + codec; Python async-gen.* Fixes Robyn per-token churn / FastAPI sync-gen hop / Blazeio Python ceiling. → §5
6. **Disconnect → cancel → abort-upstream.** hyper connection-closed signal → cancel the producer task → propagate `CancelledError` into the LLM generator → abort the outbound request; `ConnectionTaskShield`-style escape hatch for must-complete (billing) sections. *Rust disconnect watch → Python task cancel.* Fixes Robyn (no clean per-token cancel) / FastAPI (relies on `anyio.sleep(0)` checkpoint timing). → §6
7. **Per-request `contextvars.Context`** for tenant isolation + structured cancellation; sync via `ctx.run`, async via `create_task(coro, context=ctx)`; Python ≥3.11. *Rust allocates once/req, Python semantics.* Fixes Robyn pre-#1380 cross-request `ContextVar` leak. → §7
8. **Bounded memory: lazy request proxy + max-body-limit + pooled buffers.** Nothing materialized until touched (~1–3 allocs/req vs Robyn 30–66); `RequestBodyLimit` default 16 MiB (per-route override for uploads); no eager full-body buffer. *Rust pooled `Bytes` + lazy `#[pyclass]`.* Fixes Robyn eager total marshalling / Blazeio fixed 100 KiB/conn. → §8
9. **Resource-leak closure: yield-dep / exit-stack / WS-task / future.** Streaming resource teardown structurally request-scoped (PEP-789), never GC; bounded WS inbound channel + per-app task registry + reaper; cancellation-aware bridge futures. *Rust drop-schedule; bounded channel.* Fixes FastAPI GC-finalization edge / Robyn WS unbounded channel + module-global task dict. → §9
10. **Error recovery: handler/dep/tool/upstream/JSON/truncated-body.** One collapsed request-scope try/except (MRO lookup, exact 422 shape); typed responses not exception-as-control-flow; malformed JSON → 422 `json_invalid`; truncated body → 400; upstream LLM fail → typed 502/circuit-breaker, POST never auto-retried. *Rust catch-all + Python handlers.* Fixes Blazeio exception-as-primary / FastAPI double-validate / Robyn 500-on-any. → §10
11. **No anti-pattern dispatch.** No `BaseHTTPMiddleware`/`call_next` task-group-per-request; no sequential post-response BackgroundTasks blocking connection release; no per-chunk sync-gen thread hop; no GIL held across `.await` (clippy `await_holding_lock`). *Rust/tower middleware only.* Fixes FastAPI `base.py:100-199` / `bonus` / `§5.3`. → §11
12. **Liveness ≠ readiness; cgroup-aware; fail-fast boot.** `/livez` (const-tier, passes even when Python pool saturated) ≠ `/readyz` (accepting + pool not saturated + not draining); cgroup-aware worker count; **never `input()`** on port conflict (fail fast, non-zero exit). *Rust const-tier + bootstrap.* Fixes Robyn `os.cpu_count()` + `input()` container hang; none of the three split liveness/readiness. → §12

**The 5 stability bugs ALL THREE share** (the ones Lancelot must fix to *be* the stable one): (1) **no client-disconnect → upstream-LLM-abort wiring** — none of the three stop paying for tokens nobody reads (FastAPI relies on `anyio.sleep(0)` checkpoint timing, Robyn has no clean per-token cancel, Blazeio cancels the task but its `reTask` path loses structured cancel); (2) **no per-route/total request timeout coverage by default** (FastAPI: only the implicit ASGI-server keep-alive; Robyn: `client_request_timeout(0)` disables it; Blazeio: TCP keepalive only — none enforce header+body+total deadlines); (3) **unbounded queues somewhere on the hot path** (FastAPI: `CapacityLimiter(40)` then unbounded wait; Robyn: `crossbeam-channel::unbounded()` sync queue + unbounded WS mpsc; Blazeio: unbounded log `Enqueue` and per-task scope leak); (4) **streaming/long-stream memory & teardown not structurally bounded under concurrency** (FastAPI: per-stream ≈8 frames + ≥3 exit-stacks resident; Robyn: per-token GIL+spawn_blocking storm; Blazeio: 100 KiB/conn fixed + GIL-serialized future churn); (5) **no crash/abort self-healing** (FastAPI/uvicorn: a worker that wedges is not respawned by the framework; Robyn: `panic="abort"` + no supervisor = permanent worker loss; Blazeio: an unhandled non-`BlazeioException` in `finally` masks the error and the single process has no respawn). Lancelot closes all five by construction — they are gates 1, 2, 3, 4, 5, 6 above.

---

## §0. What "Stable Like FastAPI/Robyn" Concretely Means (the requirement, decomposed)

The user's #1 requirement is a *comparative* one — not "stable in the abstract" but "**at least as stable as FastAPI and Robyn**, while faster." That is a precise, falsifiable bar, and decomposing it is what makes the rest of this catalog a gate rather than a wishlist.

**What FastAPI is actually stable at (and Lancelot must not regress):** (a) **a handler exception never kills the worker** — CPython zero-cost exceptions + `ServerErrorMiddleware`'s `try/except Exception` that maps to 500 and re-raises for logging (`middleware/errors.py:149-185`, `fastapi/01 §9.1`, KNOWN); the happy path has *"~zero"* exception cost (`fastapi/01 §9.3`, KNOWN). (b) **Correct lifespan/yield-dep teardown ordering** — the PEP-789 streaming fix (`fastapi/02 §6.3`, KNOWN), exit-stack scoping that the entire ecosystem's DB-session/transaction patterns depend on. (c) **Exact, stable wire contracts** — `422 {"detail":[...]}`, `Match` tri-state 404-vs-405, strict-content-type CSRF default (`fastapi/01 §12`, KNOWN) — clients and test suites are *built against these*; changing them is an instability *for the user's users*. **Where FastAPI is NOT stable:** the 40-thread sync cliff (§4), `BaseHTTPMiddleware` per-request task-group (§11), sequential post-response BackgroundTasks (§11), no request timeouts (§3/§15). "Stable like FastAPI" = keep (a)(b)(c), fix the rest.

**What Robyn is actually stable at (and Lancelot must not regress):** (a) **The GIL choreography** — single-digit µs GIL hold/request, dropped across every `.await`, so 5000 concurrent LLM calls coexist (`robyn/01 §2.4,§7.1`, KNOWN); this *is* the stability property under concurrency. (b) **Multiprocess GIL escape** — N independent interpreters via `SO_REUSEPORT`, one bad interpreter cannot corrupt another (`robyn/01 §1.2`, KNOWN). (c) **Per-request `contextvars.Context`** after the #1380 fix (`robyn/01 §4`, KNOWN). **Where Robyn is NOT stable:** no graceful drain, no respawn, `panic="abort"` fragility, `client_request_timeout(0)`, unbounded sync queue + sync not wired, per-token streaming, `os.cpu_count()`, `input()` on port conflict (`robyn/03 §3.5`, KNOWN — the entire ops surface). "Stable like Robyn" = keep (a)(b)(c) verbatim, fix the ops surface.

**The three frameworks' stability philosophies, contrasted (the design lens):**

| | Stability philosophy | The cost of that philosophy | Lancelot's synthesis |
|---|---|---|---|
| **FastAPI** | *Ergonomics-first; correctness via Python's exception model + structured exit-stacks* | per-request Python tax; tutorial-recommended paths are the slow/unstable ones (`@app.middleware`, BackgroundTasks) | keep the exit-stack correctness + wire contracts; move them into a Rust drop-schedule; lock out the slow paths at the API |
| **Robyn** | *Speed-first; the Rust core is correct, the Python ops layer is an afterthought* | the core is production-grade, the supervisor is missing entirely | keep the core discipline verbatim; add the missing ops layer (drain, respawn, deadlines, bounds) — the §1/§2/§3/§4 guards |
| **Blazeio** | *Allocation-first; the right backpressure model, pure-Python ceiling, exception-as-control-flow* | correct model but GIL-ceilinged; non-local untestable error flow; security-unsafe client defaults | keep the backpressure *model* + `finally`-cleanup funnel; move it to Rust (no ceiling); typed responses not raise; verify=True |

**Why a Rust-speed framework is *more* exposed to instability, not less (the central tension):** every guard in this catalog exists because the Rust core *adds* failure surfaces a pure-Python framework does not have — a panic at the FFI seam is a process kill (FastAPI has no seam, §2/§14); a GIL held across an await freezes the *whole interpreter* (FastAPI has no explicit GIL handling, §7/§14); a shared `#[pyclass]` is a data race under free-threading (pure-Python has no `#[pyclass]`, §14). The "faster" half of the requirement is what *creates* the stability risk; the catalog is the proof that Lancelot pays that risk down to *below* the pure-Python baseline, not just to parity. That is the only honest reading of "stable like FastAPI/Robyn but faster."

---

## §1. Graceful Shutdown / Connection Draining

### 1.1 The defects (KNOWN)

**Robyn — drops every in-flight request on deploy.**
`run_processes` installs `terminating_signal_handler` which on SIGINT/SIGTERM does `for process in process_pool: process.kill()` (`robyn/03 §3.1`, `processpool.py:55`, KNOWN). `process.kill()` is SIGKILL-class — the child gets no chance to finish the request it is serving. The teardown's verdict: *"In-flight requests are dropped"* (`robyn/03:197`); *"Startups doing payments/AI streaming cannot drop requests on deploy"* (`robyn/03:255`, KNOWN). Robyn additionally has **no SIGTERM→drain→SIGKILL escalation** and **no per-request quiescence** — the parent just `process.join()`s after killing (`processpool.py:65-66`), so there is not even a drain window. For an LLM SSE stream mid-generation or a payment POST mid-commit, deploy = data loss.

**Blazeio — per-connection cancel, but no orchestrated fleet drain.**
Blazeio has `connection_lost`/`abort_connection` per-connection cancellation (`blazeio/01 §1.5`, `Protocols/__init__.py:20-23`, KNOWN) and a `Server`/`Taskmng` layer with "graceful shutdown, serve_forever" + "create_task tracking + cancel-on-exit" (`blazeio/03:33-34`, KNOWN). But it is a single-process pure-asyncio server: there is no multi-worker drain coordination, no SIGTERM→finish-in-flight→timeout→hard-stop state machine surfaced, and `Taskmng`'s "cancel-on-exit" *cancels* in-flight tasks rather than letting them complete — so a graceful redeploy still `CancelledError`s active handlers. The keep-alive loop's `finally` (`server_protocol.py:111-112`, `blazeio/02 §5`) guarantees `eof()`/`reset()` per request but not "let this request finish before the process exits."

**FastAPI/uvicorn — has lifespan + drain, but its drain is the ASGI server's, not the framework's, and the framework's own teardown ordering is fragile.**
The customized FastAPI fork has correct lifespan (merged async-CM chain, `routing.py:209-223`, `fastapi/01 §11.1-11.2`, KNOWN) and `lifespan.shutdown` runs the merged shutdown CM. uvicorn drains in-flight on SIGTERM. The fork's *real* shutdown defects are: (a) **`BackgroundTasks` run sequentially inside the request coroutine after body send** (`responses.py:171,285`, `fastapi/01 §10`, KNOWN) — a 30 s background job keeps the request slot occupied 30 s past the 200, so a "graceful" drain that waits for in-flight requests can hang for the worst background-task duration; (b) **request-scoped `yield`-dep teardown runs after the body is sent** via `request_stack.__aexit__` (`routing.py:121`, `fastapi/02 §6.1`, KNOWN) — correct, but if the process is killed during the drain window the DB/LLM client teardown is skipped (connection leak on the *backend*, not the client).

### 1.2 The guard Lancelot ships (DESIGN)

**Mechanism — the exact drain state machine (Rust supervisor + per-worker hyper drain):**

1. **Parent installs `SIGTERM`/`SIGINT` → broadcast "drain"** to all workers (`design/01 §5.1.1`, fixes Robyn `processpool.py:55`). Where: **Rust** parent supervisor (the process that holds the `SO_REUSEPORT` bind and the `process_pool`).
2. **Each worker on "drain":** stop `accept()` on its listening fd / leave the `SO_REUSEPORT` set so the kernel routes *new* connections to still-live workers (`robyn/01 §1.2` socket model is the substrate); keep serving in-flight; advertise `Connection: close` on responses so keep-alive clients don't pipeline a new request onto a draining worker. Where: **Rust** (the acceptor owns the fd).
3. **`hyper-util`'s `GracefulShutdown`** tracks in-flight connections; `await graceful.shutdown()` resolves when all connections finish *or* the bounded **`graceful_timeout` (default 30 s, configurable)** timer fires (`design/01 §5.1.3`, KNOWN hyper-util has `graceful`). Where: **Rust**.
4. **SSE/streaming connections on drain:** signal the producer to finish the *current* event then close (PEP-789-correct teardown, SYNTHESIS steal #9) — **do not hard-cut mid-token**. Where: **Rust** sends a "drain" signal into the SPSC ring; **Python** producer observes ring-closed-after-current and returns. (§5/§6 share this mechanism.)
5. **Run shutdown lifespan + yield-dep request-stack teardown** for every still-open request *after* its last byte, *before* SIGKILL — the merged async-CM chain (FastAPI's contract, SYNTHESIS steal #17) executed in the Rust drop-schedule (`fastapi/02 §6.4(e)`: registration emits `Vec<TeardownOp{slot,scope,is_async}>`, Rust unwinds LIFO after the response future completes). This guarantees DB pools / LLM clients close cleanly. Where: **Rust** drives the schedule, crossing into **Python** only for the user `yield`-dep's post-`yield` segment.
6. **After `graceful_timeout`: `SIGKILL` the worker.** Then parent exits. Where: **Rust**.

**Constants:** `graceful_timeout` = 30 s (override via config / `LANCELOT_GRACEFUL_TIMEOUT`); SSE drain finishes the current event only (no extra grace beyond `graceful_timeout`); `Connection: close` set on every response once draining.

**Why this is the stable one:** Robyn drops 100 % of in-flight on deploy; Lancelot loses 0 % within `graceful_timeout` and degrades to "lose only requests still running after 30 s" (and those get a clean `CancelledError`, not a severed socket). This is non-negotiable for the "payments/AI streaming startup" audience (`robyn/03 §3.5`, KNOWN).

---

## §2. Worker Crash / Panic at the FFI Seam / No Respawn

### 2.1 The defects (KNOWN — this is the single most dangerous Robyn defect)

**Robyn — `panic="abort"` + no supervisor = the fleet bleeds out to zero.**
Build profile is `panic = "abort"` (`Cargo.toml:48`, `robyn/01 §0`, KNOWN): *"A Rust panic does NOT unwind; it `abort()`s the whole OS process"* (`robyn/01 §0:92`, KNOWN). Robyn has **no crashed-worker supervisor** (`robyn/03 §3.5:257`, KNOWN). Combined: *"a panic kills a worker permanently"* (`robyn/03:626,693`, KNOWN) — one panicking request **permanently removes a worker from the fleet, forever, with no replacement**; over time the fleet bleeds to zero. Residual `.unwrap()`s exist on/near the request path: `server.rs:256` `TASK_LOCALS.get().unwrap()`, `server.rs:150` `directories.read().unwrap()`, `server.rs:282-285` `.listen(...).unwrap().run().await.unwrap()` (`robyn/01 §2.7`, KNOWN). pyo3 converts Python exceptions to `PyErr` (not panics), **but a `Py<T>` misuse or a poisoned `parking_lot` lock *will* panic** — and there is no `catch_unwind` at the FFI seam (`robyn/01 §0:92`, KNOWN: Robyn *"is fragile here"*). `http_router.rs:91` `from_py_dict` *"unwrap()s extraction — a non-string value panics"* (`robyn/02:463`, KNOWN) — a malformed auth-middleware dict is a process-kill.

**Blazeio — single process, an unhandled non-`BlazeioException` masks the error and there is no respawn.**
The keep-alive loop catches `(CancelledError, KeyboardInterrupt, CloseConnection, ClientDisconnected, ServerDisconnected)` and re-raises, but bare `except Exception as e: exc = e` then formats a 500 in `finally` (`server_protocol.py`, `blazeio/02 §5`, KNOWN). The teardown verdict: *"a bug in `after_middleware` or `eof()` inside `finally` can mask the real error"* (`blazeio/02:457`, KNOWN). Pure-Python so no FFI panic, but **a single process with no parent supervisor** — a fatal error in the connection driver loop kills the only server.

**FastAPI/uvicorn — `ServerErrorMiddleware` catches `Exception` and re-raises; a wedged worker is not respawned by the framework.**
`ServerErrorMiddleware` wraps everything in `try/except Exception` and **always re-raises** `raise exc` after sending the 500 (`middleware/errors.py:184`, `fastapi/01 §9.1`, KNOWN) so the ASGI server logs it. A Python exception never kills the worker (zero-cost exceptions), so FastAPI is the *least* exposed of the three to crash-loss — but a C-extension segfault, an OOM-kill, or an event-loop deadlock wedges the uvicorn worker and the framework provides no respawn; that is the deployment's process manager's job, and FastAPI does not ship one.

### 2.2 The guard Lancelot ships (DESIGN)

**Mechanism (Rust supervisor + Rust FFI-seam isolation + clippy enforcement):**

1. **Parent supervisor loop.** Poll `process.is_alive()`; a dead child → immediately `spawn_process` a replacement with a fresh `SO_REUSEPORT` fd clone (`design/01 §5.2.1`, `robyn/03 §3.5:257`, KNOWN-as-recommendation; `robyn/01 §9.4` table: *"ADD: respawn-on-crash (Robyn doesn't)"*). Where: **Rust** parent supervisor.
2. **Rate-capped exponential backoff:** ≤5 restarts / 30 s / worker; after the cap, mark the worker **permanently failed and alert** (do not busy-restart a deterministic crash — a crash-loop pinning the box is its own outage). Constants: `max_restarts=5`, `restart_window=30 s`, backoff base 200 ms ×2. Where: **Rust**.
3. **`catch_unwind`/unwind-scope at the FFI seam only.** Keep `panic="abort"` for the non-FFI hot path (no landing pads = faster/smaller, `robyn/01 §0`, KNOWN), but the **Python-call boundary** is the one place a `Py<T>` misuse / poisoned lock can panic — wrap it so a marshalling panic becomes a `500`, not a worker kill (`design/01 §5.2.2`, `robyn/01 §0:92`, KNOWN-as-recommendation). Practical pattern (because `panic="abort"` makes `catch_unwind` a no-op): **zero-`unwrap()`/`expect()`/`panic!`/`unreachable!`/indexing discipline on the request path, enforced by a clippy `deny` on the request-path modules** + the supervisor as the backstop (`design/01 §5.2.2`, `robyn/01 §10` hon-4, KNOWN). Where: **Rust** (the seam crate + clippy config).
4. **Never panic on user input.** Robyn's `from_py_dict` `unwrap()` (`robyn/02:463`, KNOWN) → Lancelot's equivalent returns `Err(PyErr)` → 400/422, never a panic. Where: **Rust**.

**Constants:** `max_restarts=5`, `restart_window=30 s`; clippy: `#![deny(clippy::unwrap_used, clippy::expect_used, clippy::panic, clippy::unreachable, clippy::indexing_slicing)]` scoped to `request_path/*` modules; ship `panic="abort"` **only with** the supervisor wired (`design/01 §5.2`, `robyn/03:639`, KNOWN-as-recommendation).

**Why this is the stable one:** Robyn's fleet monotonically decays under any panic-triggering input class; Lancelot self-heals (respawn) and refuses to panic on the request path (clippy-enforced) so the trigger rarely fires, and the rare poisoned-lock panic is one 500 + one respawn, not a permanent capacity loss.

---

## §3. Slow-Loris / Header-Flood / Huge-Body / Timeout Coverage

### 3.1 The defects (KNOWN — two of three are wide open; the third has the wrong default)

**Blazeio — O(n·k) header re-scan, no header-receipt deadline: a real DoS surface.**
The parser does `while b"\r\n\r\n" in buff` (`blazeio/01 §4.2`, `min_http_parser.py`, KNOWN) which **re-scans the accumulated buffer from byte 0 on every `recv`** until the terminator arrives → O(S·n) under fragmented headers. Header-line stripping is `header = header[idx+2:]` per line → **O(H²) tail recopy** (`blazeio/01 §4.4:436`, KNOWN). The `102400`-byte cap bounds memory but **there is no per-connection request-header receipt deadline** (`blazeio/01 §8:751`, KNOWN, verbatim): *"an attacker dribbling request headers one byte per second forces `while b"\r\n\r\n" in buff` to re-scan on every byte … the connection (and its 100 KiB buffer + a pump coroutine + a `Future`) is held hostage for up to the keepalive timeout … This is a real DoS surface and a strong Rust-core argument."* Only TCP keepalive (30–60 s) and the byte cap defend; with thousands of slow-loris connections the GIL-serialized per-connection overhead (task + futures + 100 KiB) exhausts first (`blazeio/01 §8:758`, KNOWN). The chunked decoder also `int(buff[:idx],16)`s without stripping chunk extensions → `int("1a;foo",16)` raises `ValueError` (`blazeio/01 §5.3:562`, KNOWN gap) — a crafted chunked body is a parse-exception flood.

**Robyn — the request-read timeout is disabled entirely.**
`HttpServer::new(...).client_request_timeout(Duration::from_secs(0))` (`server.rs:280`, `robyn/01 §3.1:397`, KNOWN): *"`0` means actix will not time out a slow client sending headers … a slowloris attacker can hold a connection forever with no framework-level defense — Robyn relies on `backlog=16384` + OS limits + reverse proxy"* (KNOWN). `KeepAlive::Os` (`server.rs:278`) delegates idle timeout to the OS TCP stack — good for legit long streams, but means no application-level slow-loris cutoff at all. The teardown's own prescription: *"Lancelot should make this configurable with a sane default (e.g. 30 s header timeout) and a documented `0`-to-disable for streaming"* (`robyn/01:397`, KNOWN-as-recommendation).

**Robyn — eager full-body buffer: a 10 MB upload is 20 MB resident before Python sees byte 0.**
`Request::from_actix_request` buffers the entire body into a `Vec<u8>` *before* the handler is invoked (`request.rs:158-163`, `robyn/02 §3.1:254`, KNOWN): *"For a 10 MB upload that's a 10 MB `BytesMut` + a 10 MB `Vec<u8>` copy = 20 MB resident before Python sees byte zero."* `ROBYN_MAX_PAYLOAD_SIZE` default is `1_000_000` (1 MB, `server.rs:39-40`, KNOWN) — a hard cap exists but there is no streaming intake and the cap is a single global, not per-route.

**FastAPI — no body/total deadline; relies on the ASGI server.** The fork has no per-route header/body/total request timeout; slow-body and slow-header attacks are uvicorn's problem, and uvicorn's defaults are permissive. `email.message.Message()` is instantiated **per request** just to split a Content-Type (`routing.py:417-419`, `fastapi/01 §4.2`, KNOWN) — a cheap amplification target under a header flood.

### 3.2 The guard Lancelot ships (DESIGN)

**Mechanism (all in the Rust core — this is the core's central justification, `blazeio/01 §8`, `SYNTHESIS:102`):**

1. **Single-pass resumable Rust parser.** hyper's HTTP/1 parser scans each byte once with a resumable state machine — it does **not** re-scan from 0 (the structural win over Blazeio's `bytes.find`, `blazeio/01 §4.2`, KNOWN). This alone kills the O(S·n) and O(H²) quadratics and the chunked-extension `ValueError` flood. Where: **Rust** (`hyper` `http1`).
2. **Header-receipt deadline.** From first byte of a connection's request to full header receipt: default **30 s**, configurable, `0` = disable (documented, for streaming-request-body endpoints) — exactly the teardown's prescription (`robyn/01:397`, KNOWN-as-recommendation). Mechanism: `hyper::server::conn::http1::Builder::header_read_timeout(Duration::from_secs(30))` (KNOWN — hyper 1.x exposes `header_read_timeout`) wrapping the connection-serve future. Where: **Rust**.
3. **Separate body-read and total-request timeouts.** A slow body upload is a *different* attack from slow-loris headers — distinct deadlines. Defaults: header **30 s**, body **60 s**, total **120 s**; all overridable **per-route** (streaming/upload routes set body/total to `0` explicitly). Mechanism: `tower-http::timeout::TimeoutLayer` for total + a body-stream deadline wrapper. Where: **Rust** (per-route config baked at registration, applied by the tower layer).
4. **Connection cap + accept backpressure** (Granian's model, KNOWN — *"a secondary backlog … interrupts the accept loop in case too many requests are waiting"*): a per-worker `max_concurrent_connections`; when exceeded, stop accepting (the kernel `SO_REUSEPORT` queue + `listen(16384)` backlog absorbs the burst, or another worker takes it). This bounds a slow-loris swarm's cost to `cap × (parser_state + buffer)`, tiny in Rust (resumable parser state ≈ tens of bytes) vs Blazeio's `task + 100 KiB + Future` per connection. Where: **Rust** acceptor.
5. **Streaming body intake with backpressure** instead of eager full buffer (fixes Robyn `request.rs:158-163`): the Rust core owns a pooled refcounted `BytesMut`; a streaming-body route gets an async-iter with pull backpressure (`blazeio/01:177` DESIGN-NOTE model in Rust); a non-streaming route's body is bounded by `RequestBodyLimit` (§8). Where: **Rust**.
6. **Content-Type parsed in Rust** — split on `;`, lowercase, compare a tiny set / suffix `+json` (fixes FastAPI's per-request `email.message.Message()`, `fastapi/01 §4.2`, KNOWN), zero Python allocation, no amplification target. Where: **Rust**.

**Constants:** `header_read_timeout` = 30 s (0 = disable, documented); `body_read_timeout` = 60 s; `total_request_timeout` = 120 s; all per-route overridable; `max_concurrent_connections` per worker = configurable (default e.g. 1024, surfaced in `/metrics`); `RequestBodyLimit` default 16 MiB (§8).

**Why this is the stable one:** Blazeio and Robyn have **zero** application-level slow-loris defense; Lancelot drops a dribbling client at near-zero per-connection cost (Rust parser + tokio timeout) — the precise scenario the teardowns name as *the* core justification for a Rust core (`blazeio/01 §4.2`, `SYNTHESIS:102`, KNOWN).

---

## §4. Backpressure Failure / Unbounded Buffers / Load-Shedding / OOM

### 4.1 The defects (KNOWN — every one of the three has an unbounded queue somewhere)

**FastAPI — the silent `CapacityLimiter(40)` cliff: the #1 production AI incident.**
Sync `def` handlers, sync deps, sync exception handlers, and sync stream generators all dispatch via `run_in_threadpool` → `anyio.to_thread.run_sync` with **no `limiter=`**, which uses the per-event-loop default `CapacityLimiter(40)` (anyio `_backends/_asyncio.py:2957`, `fastapi/01 §5.1`, KNOWN, exact constant). *"The 41st concurrent blocking call suspends on the limiter until a token frees … process-wide per event loop, shared across all sync endpoints, sync deps, sync exception handlers, and `iterate_in_threadpool`"* (KNOWN). For an AI backend whose handlers wrap blocking SDKs (`openai` sync client, sync vector DB), *"each in-flight chat request holds one of the 40 threads for the entire LLM latency (seconds) … request 41+ queues before even entering the handler, adding unbounded latency. There is no backpressure signal to the client — it just hangs. This is the #1 production FastAPI scaling incident pattern for AI apps"* (`fastapi/01 §5.2`, KNOWN). Worse, **response validation of a sync endpoint is itself punted to the same 40-pool** (`serialize_response:295`, `await run_in_threadpool(field.validate,...)`, `fastapi/01 §6.1`, KNOWN) — a double hop. The queue after the 40 is **unbounded** — no shed, no 503, no signal.

**Robyn — `crossbeam-channel::unbounded()` sync-pool queue + sync handlers not even wired to it.**
The `BlockingRunner` feeds off `channel::unbounded()` (`blocking.rs:545,566`, `robyn/01 §5.2,5.3`, KNOWN). The teardown's verdict: *"Under sustained sync-handler overload the queue grows without bound → memory blowup + unbounded latency (requests queued for seconds) … This is a real production gap in Robyn"* (`robyn/01 §5.6:646`, KNOWN). And it is moot in the worst way: **the sync HTTP path never calls the BlockingRunner at all** — `execute_http_function`'s sync branch runs the handler inline under `Python::with_gil` on the actix worker (`robyn/02 §5b:441`, KNOWN, the "big finding"): *"a synchronous blocking handler (`requests.get`, sync `openai`, a slow CPU loop) holds the GIL and blocks that worker for its full duration."* So Robyn has *both* an unbounded queue *and* a sync path that doesn't use it — a blocking handler pins a whole reactor worker.

**Robyn — unbounded WebSocket inbound mpsc.**
Each WS connection creates `tokio::sync::mpsc::unbounded_channel::<Option<String>>()` (`websockets/mod.rs`, `robyn/02 §7.2:494`, KNOWN): *"`unbounded` = no backpressure (a fast client can OOM the channel)."*

**Blazeio — backpressure is *correct* but pure-Python-ceilinged; plus an unbounded log queue.**
Blazeio's receive (pause-by-default, `transport.pause_reading()` on every `buffer_updated`, `blazeio/01 §1.4`, KNOWN) and send (boolean `__is_buffer_over_high_watermark__` gate, no Future on the fast path, `blazeio/01 §3`, KNOWN) backpressure is *the correct model* — "no unbounded queue, memory bounded by `INBOUND_CHUNK_SIZE` (102400) + what the handler holds" (`blazeio/02 §2:164`, KNOWN). The ceiling is the GIL: *"Many concurrent streams = many `SharpEvent`/`Future` wakeups … with 5k concurrent SSE/LLM streams … the GIL serializes all of this … the Python-level future churn + GIL is the ceiling"* (`blazeio/01 §8:749`, KNOWN). Separately, the threaded logger's `Enqueue` is a `deque` + overflow `Condition` with no hard bound surfaced (`blazeio/03:287`, KNOWN) — log floods grow it.

### 4.2 The guard Lancelot ships (DESIGN)

**Mechanism (Rust admission control + bounded queues everywhere; Blazeio's backpressure *model* in Rust):**

1. **Bounded sync-handler queue + actually wired into the HTTP path.** Port Robyn's 3-variant `BlockingRunner` + GIL-`detach`-on-`recv` + adaptive rate-limited single-flight spawn (`robyn/01 §5.4`, KNOWN — "THE key insight"), but change `unbounded()` → `crossbeam-channel::bounded(sync_queue_max)` and **classify handler async/sync at registration** (`FunctionInfo.is_async`, SYNTHESIS steal #10) so sync → `BlockingRunner`, async → loop, with no per-request introspection (fixes Robyn's "exists but not wired", `robyn/02 §5b`, KNOWN). Where: **Rust** classifies + routes; the pool is Rust.
2. **Load-shed before the Python frame.** On full queue / connection cap exceeded → the **Rust core returns `503` + `Retry-After` *before* spawning the Python frame** (`design/01 §4.4.2`, `fastapi/01 §5:402,409`, KNOWN-as-recommendation — this is exactly FastAPI's missing backpressure). Never a silent hang; always an observable status. Where: **Rust**.
3. **Surface saturation in `/metrics`.** sync-pool queue depth, connection count, shed count — Prometheus + the readiness probe (§12) reads pool-not-saturated. None of the three expose this. Where: **Rust** const-tier `/metrics`.
4. **Bounded WS inbound channel** with an explicit policy (drop-oldest / close-with-1009 / backpressure-the-reader), configurable; raw-`bytes` path alongside text (fixes Robyn `websockets/mod.rs` unbounded + UTF-8-coerce, `robyn/02 §7.5`, KNOWN). Where: **Rust**.
5. **End-to-end send/receive backpressure in Rust** (Blazeio's model, `blazeio/01 §1.4,§3`, KNOWN, but without the GIL ceiling): pause-by-default receive on a pooled `BytesMut`; boolean send-backpressure gate (no Future/token); a **bounded SPSC ring (size-1 default)** between the Python producer and the Rust writer. The full chain: slow client → transport write buffer fills → ring fills → producer coroutine parks → it stops pulling from the LLM → **bounded server RAM regardless of completion length** (`design/01 §5.4`, SYNTHESIS steal #8:67, KNOWN). This is §5's mechanism; load-shedding (this section) is the *admission* half, backpressure (§5) is the *in-flight* half. Where: **Rust** ring/codec; **Python** async-gen producer.
6. **Bounded log sink.** Off-loop batched drain (Blazeio's pattern, `blazeio/03:287`, KNOWN) but via stdlib `logging.handlers.QueueHandler`/`QueueListener` with a **bounded** queue + drop-on-full counter (never an unbounded `Enqueue`). Where: **Python** (logging is not on the Rust hot path).

**Constants:** `sync_queue_max` = 10 000 (override); `max_concurrent_connections` per worker = configurable; SPSC ring size = 1 (lowest latency + tightest backpressure, tuned by the §8-spike); WS inbound channel bound = configurable (default e.g. 256 frames); 503 `Retry-After` = configurable (default 1 s).

**Why this is the stable one:** all three have an unbounded queue that turns sustained overload into OOM or an infinite silent hang; Lancelot has a *bound everywhere* and converts overload into an **observable `503 Retry-After`** emitted by Rust before any Python runs — the load-shed FastAPI's `CapacityLimiter(40)` conspicuously lacks (`fastapi/01 §5`, KNOWN).

---

## §5. Memory Growth Under Long-Running Streams (the AI Hot Path)

### 5.1 The defects (KNOWN — each of the three gets one half right and one half catastrophically wrong)

**Robyn — per-token `spawn_blocking` + `with_gil`: "Robyn's worst weakness for AI workloads."**
`create_python_stream` (`response.rs:128-146`, `robyn/01 §7.2`, KNOWN, verbatim) does, **per chunk**: `tokio::task::spawn_blocking(move || Python::with_gil(|py| gen.call_method0("__next__")...))`. Four KNOWN problems (`robyn/01 §7.2:770-777`): (1) `spawn_blocking` per chunk onto tokio's **shared** pool (the one fs/DNS uses) — for a 500-token completion that is 500 dispatches; under 1000 concurrent streams a storm that head-of-line-blocks all other blocking work; (2) `Python::with_gil` per chunk — the GIL becomes the streaming bottleneck, streams serialize against each other *on every token*; (3) **sync generator only** (`call_method0("__next__")`) — an `async def` streaming handler cannot be pumped; a sync wrapper blocks a pool thread *per awaited token*; (4) `.extract::<String>()` per chunk — a Python-str→Rust-String copy + UTF-8 validation per token. Verdict (`robyn/01 §10` pitfall 3, KNOWN): *"Robyn's worst weakness for AI workloads … must fix before launch."*

**FastAPI — architecturally correct producer/keepalive, but the sync-generator path is "catastrophic."**
The fork's SSE path is the *right* shape: decoupled producer task + bounded `anyio.create_memory_object_stream[bytes](max_buffer_size=1)` + a **separate keepalive task that wraps the channel `receive()`, not the generator's `__anext__`** (`routing.py:534-595`, `fastapi/01 §7.1`, KNOWN — PEP-789-correct: wrapping `__anext__` in `anyio.fail_after` would `CancelledError` the generator mid-stream). It is entered on the request-scoped exit stack so teardown is structural, not GC (`fastapi/02 §6.3`, KNOWN). **Steal this architecture wholesale.** But the *sync*-generator branch uses `iterate_in_threadpool` which does **one thread hop + one default-limiter token acquire/release per yielded chunk** (`concurrency.py:39-58`, `fastapi/01 §5.3`, KNOWN): *"For an LLM token stream (hundreds–thousands of chunks) via a sync generator, that's hundreds of thread round-trips and token churn — catastrophic."* And per-stream the entire nested middleware stack stays resident: *"hundreds × (≈8 frames + ≥3 AsyncExitStacks + 2 memory streams + 2 producer tasks) of resident memory"* (`fastapi/01 §7.4`, KNOWN).

**Blazeio — the correct backpressure model, the wrong coalescing constant, the Python ceiling.**
`writer()` → `buffer_overflow_manager()` is a **synchronous boolean early-return on the fast path — no Future per token** (`server_protocol.py:64-72`, `blazeio/02 §3.2`, KNOWN), strictly better than Starlette's `drain()` (Future every chunk). End-to-end backpressure is genuinely correct (`blazeio/02 §3.3:319`, KNOWN, verbatim): *"slow client → stalled handler → paused upstream-LLM read … Server-side unbuffered memory per stream is bounded by the transport high-water mark (~64 KiB), not by completion length or client speed. This is the property to copy verbatim."* But `Batchedwriter` coalesces only at `rem_chunk_size = 30720` bytes (`writers.py:9-10`, `blazeio/02 §3.4`, KNOWN): *"a 30 KiB threshold is far too coarse for interactive chat — it would hold ~5000 tokens before the first flush, destroying perceived streaming. The pattern is right, the constant is wrong for AI."* And the GIL ceiling: 5k concurrent streams = GIL-serialized `SharpEvent`/`Future` churn (`blazeio/01 §8:749`, KNOWN).

### 5.2 The guard Lancelot ships (DESIGN — the synthesis, end-to-end)

**Mechanism (FastAPI's architecture + Blazeio's no-Future gate, both pushed into Rust, fixing Robyn's per-token tax):**

1. **Native async-generator pump** via `into_future` — each token's `await llm.stream()` is a real GIL-released asyncio await, NOT a blocked pool thread (`robyn/01 §7.3.1`, KNOWN). Use the custom `PyIterAwaitable` (`robyn/01 §6`, KNOWN — "38–55% faster, no Future alloc") to wrap each `__anext__` cheaply. **Reject sync generators for streaming** with a clear registration-time error ("use `async def` + `yield`"), eliminating FastAPI's per-chunk `iterate_in_threadpool` hop entirely (`fastapi/01 §5.5`, KNOWN-as-recommendation). Where: **Python** async-gen driven by **Rust** pump.
2. **Coalesce tokens time+size** — flush when buffer ≥ ~2 KiB **OR** ≥ ~25 ms since last flush (`design/01 §5.4`, SYNTHESIS §4.5 — *not* Blazeio's 30 KiB size-only which holds thousands of tokens, KNOWN). This turns "1 GIL/token" into "1 GIL/flush" — a 10–50× GIL-traffic cut (`robyn/01 §7.3.2`, KNOWN). Where: **Rust** (the codec accumulates; the producer just yields).
3. **Bounded SPSC ring (size-1 default)** between the Python producer and the Rust writer (`design/01 §5.4`, SYNTHESIS §4.5, KNOWN — size-1 = lowest latency + backpressure straight onto the model). Where: **Rust**.
4. **Boolean send-backpressure gate, no Future/token** (Blazeio's `buffer_overflow_manager` fast-path verbatim, `blazeio/02 §3.2`, KNOWN — synchronous early-return when the client keeps up). Where: **Rust** (the writer's `poll_flush` integrates the gate).
5. **Rust-side SSE/JSONL framing** (`data: %b\n\n`, `\n`) + a **timer-wheel keepalive** that wraps the *ring receive*, not the generator's `__anext__` (FastAPI's PEP-789 correctness, `fastapi/01 §7.1`, KNOWN, moved to Rust — kills the per-event Python list-join + the Python keepalive task). Forced headers `Cache-Control: no-cache` + `X-Accel-Buffering: no` (KNOWN, both FastAPI `routing.py:616-618` and Robyn `response.rs:107-117` set these — match verbatim). Where: **Rust**.
6. **Structurally request-scoped teardown** (PEP-789): the producer task + ring live in a request-scoped Rust drop-schedule, unwound *after* the response future completes — never via async-generator GC (`fastapi/02 §6.3,6.4(e)`, KNOWN). A request-scoped `yield`-dep DB/LLM client injected into the streaming generator stays open through the whole stream and closes after the last byte (the "load-bearing for AI" ordering, `fastapi/02 §6.3:992`, KNOWN: *"Lancelot must preserve this exact ordering or AI streaming apps will leak connections or close sessions mid-stream"*). Where: **Rust** drop-schedule; **Python** only for the user yield-fn's post-`yield` segment.

**The full chain (the property all three only partially achieve, Lancelot achieves whole):** slow client → transport write buffer fills → SPSC ring fills → producer coroutine parks → it stops pulling from the LLM → **bounded server RAM regardless of completion length** (SYNTHESIS steal #8:67, KNOWN).

**Constants:** coalesce flush = 2 KiB OR 25 ms (whichever first; tuned by the §8 spike); SPSC ring size = 1; keepalive interval = 15 s (match FastAPI `_PING_INTERVAL = 15.0`, `sse.py:222`, KNOWN); keepalive payload `b": ping\n\n"` (match `sse.py:218`, KNOWN); SSE terminator `\n\n` (match, KNOWN).

**Why this is the stable one:** Robyn's per-token GIL+spawn_blocking storm degrades catastrophically at the concurrency an AI product needs; FastAPI's sync-gen path is "catastrophic" and its per-stream resident footprint is ≈8 frames; Blazeio's model is correct but GIL-ceilinged and mis-tuned. Lancelot is FastAPI's correct architecture + Blazeio's no-Future gate, both in Rust → 1 GIL/flush, size-1 ring, bounded RAM at 10k streams.

---

## §6. Connection Drop / Client Disconnect Mid-Stream / Cancellation Correctness

### 6.1 The defects (KNOWN — this is the #1 shared bug: nobody aborts the upstream LLM)

**The shared failure: client disconnects, server keeps generating, you keep paying for tokens nobody reads.** This is the single most expensive correctness bug for an AI gateway and **all three get it at least partly wrong**.

**Robyn — no clean per-token cancel.**
The streaming pump is per-token `spawn_blocking`+`with_gil` on a *sync* generator (`response.rs:128-146`, `robyn/01 §7.2`, KNOWN). There is no disconnect→cancel path wired through that pump: *"Robyn: per-token spawn_blocking has no clean cancel"* (`design/01 §5.5` table, `robyn/01 §7.2`, KNOWN). The `PyFutureAwaitable` has a `cancel` path (`callbacks.rs:282-320` → `cancel_tx.notify_one()` → `tokio::select! biased` drops the future, `robyn/01 §6.3,6.4`, KNOWN) — the *mechanism* exists for response futures, but it is **not wired to the streaming pump**, so a disconnected client's stream keeps pumping tokens (and keeps the upstream LLM read alive) until the generator naturally ends. The teardown's prescription (`robyn/01 §7.3.5`, KNOWN-as-recommendation): *"Wire client-disconnect → cancel the producer coroutine (use the `PyFutureAwaitable` cancel path) → abort the outbound LLM request."*

**FastAPI — disconnect detection depends on `anyio.sleep(0)` checkpoint timing.**
The fork inserts `await anyio.sleep(0)` after every streamed chunk (`routing.py:605,634,665`, `fastapi/01 §7.1:529`, KNOWN, ref issue #14680) so cancellation *can* be delivered. But this is **timing-dependent**: *"Without this, a fast LLM stream to a disconnected client can spin without noticing the disconnect"* (KNOWN) — and even with it, the disconnect is only observed at the next checkpoint, and only if Starlette's transport actually surfaces the disconnect to the generator's task. The fork's own teardown verdict (`fastapi/01 §7.4`, KNOWN-as-recommendation): *"Disconnect detection: Rust core watches the socket; on client disconnect it cancels the Python producer task immediately (no reliance on `anyio.sleep(0)` checkpoint timing). This is strictly better than FastAPI's approach for cost control on AI backends."* FastAPI does **not** propagate cancel to the upstream LLM call beyond whatever the user's generator does on `GeneratorExit`/`CancelledError`.

**Blazeio — cancels the handler task on disconnect (good), but `reTask` loses structured cancellation.**
`connection_lost` → `app.cancel()` if `cancel_on_disconnect` (default True), where `app.cancel` is the handler task's `.cancel` (`server_protocol.py:50-56`, `blazeio/02 §3.6`, KNOWN): *"when the client disconnects mid-stream, the handler coroutine is cancelled at its next await — which propagates `CancelledError` into the LLM generator, closing the upstream connection (if the generator cleans up on cancel)."* This is the **closest-to-correct** of the three, and `ConnectionTaskShield` (`server_protocol.py:123-138`, KNOWN) is a genuinely good escape-hatch CM for must-complete (billing) sections — **steal both**. But Blazeio's `reTask` (its hand-rolled Task replacement) **loses structured cancellation, contextvars, and `all_tasks()` visibility** (`blazeio/03 §2.2:180`, `blazeio/03:283`, KNOWN, `SYNTHESIS` steal #13 reject-clause): *"`reTask`s are invisible to `all_tasks()` … the watchdog would not see them … weaker cancellation semantics (`cancel()` only works while `_PENDING`)."* So on the `reTask` path the cancel is not guaranteed to propagate cleanly into nested awaits.

### 6.2 The guard Lancelot ships (DESIGN)

**Mechanism (Rust socket-disconnect watch → Python structured cancel → upstream abort):**

1. **Rust core watches the socket** (hyper surfaces connection-closed); on client disconnect it **immediately cancels the Python producer task** — no reliance on `anyio.sleep(0)` checkpoint timing (`fastapi/01 §7.4`, KNOWN-as-recommendation; strictly better than all three). Mechanism: the producer is driven by a `PyFutureAwaitable`-style bridge whose `tokio::select! { biased; result = producer => ..., () = disconnect.notified() => cancel }` drops on disconnect (Robyn's cancel-aware select shape, `robyn/01 §6.3`, KNOWN, **wired to the stream pump** — the wiring Robyn lacks). Where: **Rust** watch + select.
2. **Structured cancellation into the LLM call.** The cancel propagates `CancelledError` into the producer coroutine at its next await, which propagates into the `async for tok in llm.stream()` generator, which (because Lancelot's built-in LLM client is cancellation-correct, see `DESIGN-DECISIONS §4 AI-native layer`) **aborts the outbound HTTP request to the provider** — "stop paying for tokens nobody reads" for free (`design/01 §5.4`, SYNTHESIS §4.5, KNOWN). Use **real `asyncio.Task` with `create_task(coro, context=ctx)`**, NOT Blazeio's `reTask` (preserves structured cancel + contextvars + `all_tasks()` visibility — `blazeio/03 §0.1` reject-clause, KNOWN). Where: **Python** task graph; **Rust** triggers the cancel.
3. **Must-complete escape hatch.** Port Blazeio's `ConnectionTaskShield` async-CM (`server_protocol.py:123-138`, `blazeio/02 §3.6`, KNOWN) so a billing-commit / audit-write section can opt out of cancel-on-disconnect (`with app.shield(): commit_charge()`). Where: **Python** API over a **Rust** "don't-cancel-this-span" flag.
4. **Cancellation propagates to children.** Client-disconnect cancels the handler task which cancels its children (asyncio structured cancellation via real `Task`s — `design/01 §5.5` table, KNOWN). The per-request `contextvars.Context` (§7) is what keeps this correct and tenant-isolated under cancel.
5. **Idempotency on the upstream abort.** The built-in LLM client **never auto-retries a POST** (Blazeio's correct rule, `Client/__init__.py` `NON_BODIED_HTTP_METHODS`, `blazeio/03 §4.5:390`, KNOWN: *"a POST is not auto-retried (correct: never silently replay a non-idempotent LLM call)"*) — so aborting and not retrying cannot double-bill. Where: **Python/Rust** LLM client.

**Constants:** `cancel_on_disconnect` = True by default (Blazeio's default, KNOWN); shield is explicit opt-in per-section; disconnect→cancel latency = one tokio poll (sub-ms), not one stream-chunk interval.

**Why this is the stable one (and the #1 shared-bug fix):** none of the three reliably stop the upstream LLM when the client leaves — Robyn can't cancel the pump, FastAPI's detection is checkpoint-timing-dependent and doesn't abort upstream, Blazeio cancels the task but its `reTask` path loses structured propagation. Lancelot: Rust watches the socket, cancels in sub-ms via a wired `select!`, propagates structured `CancelledError` into the LLM generator → the provider request is aborted → billing stops. This is a direct cost-control feature, not just a stability fix.

---

## §7. Concurrency Races: Shared State, the GIL Boundary, the Rust↔Python Seam

### 7.1 The defects (KNOWN)

**Robyn — cross-request `ContextVar` leakage on a reused pool thread (shipped as a bug, fixed in #1380).**
asyncio copies the current context for every `Task`, so a `ContextVar` written in `before_request` is invisible to the handler (a different task), and — the dangerous one — *"for sync handlers run via `ctx.run(...)`, a `ContextVar` write would persist in the worker thread's current context and leak into the next request handled by that same pooled thread (a cross-request data leak — a security/correctness bug, e.g. tenant A's auth bleeding into tenant B)"* (`asyncio.rs:40-62`, `robyn/01 §4.1`, KNOWN). Robyn shipped this and fixed it in #1380 by allocating one `contextvars.Context()` per request (`server.rs:515`, GIL #1) threaded through every phase (`robyn/01 §4.2`, KNOWN). The teardown's verdict (`robyn/01 §10` hon-6, KNOWN): *"Lancelot must do it from day one or it's a tenant-isolation defect in a multi-tenant AI SaaS."* On Python 3.10 the `create_task(context=)` kwarg doesn't exist so Robyn degrades to plain `await coro` — the pre-fix (buggy) behavior (`asyncio.rs:58-62`, `robyn/01 §4.2`, KNOWN).

**Robyn — GIL held across nothing-but-`await` is the one fatal invariant; obeyed by convention only.**
The single most important rule: *"No `Python::with_gil`/`attach` scope may contain an `.await`"* (`robyn/01 §10` pitfall 1, KNOWN). If violated, *"the GIL is held for the entire duration of that await across the whole process — every other thread/coroutine in that interpreter is frozen … throughput from thousands of concurrent requests to ~1."* Robyn obeys it (`executors/mod.rs:524-535` closes the `with_gil` before `.await`, KNOWN) but **only by discipline** — there is no lint enforcing it.

**Robyn — GIL acquired on the routing hot path just to clone `FunctionInfo`; `std::sync::RwLock` poisoning risk.**
`http_router.rs:53` does `Python::with_gil(|_| res.value.to_owned())` — *"acquires the GIL just to clone a `FunctionInfo`"* on every matched request *before* deciding to call Python (`robyn/02 §2:70`, KNOWN). `middleware_router` uses `std::sync::RwLock` while the rest uses `parking_lot` — *"poisoning risk + slower; inconsistent"* (`robyn/02 §8:518`, KNOWN): a panic while holding a `std::sync::RwLock` poisons it and every subsequent `.read().unwrap()` panics (with `panic="abort"`, that is process death — compounding §2).

**Free-threaded build hazard (INFERRED, decisive — a *rewrite* if deferred).** Under the GIL build every shared Rust structure reachable from `#[pyclass]`/module state is implicitly serialized by the GIL whenever Python touches it (Robyn's implicit invariant `Python<'py> ⇒ exclusive`, `robyn/01 §2.1`, KNOWN). Under free-threading that serialization is gone (`design/01 §3.3a`, KNOWN, pyo3 0.28: `Python<'py>` = "attached", not "exclusive"). A `#[pyclass]` with mutable fields (Robyn-style lazy request, response, awaitables) is **undefined behavior on the ft build** — discovered only when someone runs `python3.14t`, by which point the API is shipped and the fix is breaking (`design/01 §3.3a`, KNOWN). FastAPI/Blazeio are pure Python so they have the inverse risk: under free-threading, code that *assumed* GIL-atomicity of `dict`/`list` ops races.

### 7.2 The guard Lancelot ships (DESIGN)

1. **Per-request `contextvars.Context`, from day one.** Allocate exactly one `Context` per request (raw `pyo3::ffi::PyContext_CopyCurrent` for the base, `contextvars.Context()` per request — Robyn's `asyncio.rs:33-49` verbatim, `robyn/01 §4.2`, KNOWN); sync handlers via `ctx.run(handler, req)`, async via `loop.create_task(coro, context=ctx)`. **Require Python ≥3.11** — drop Robyn's degraded 3.10 path as a hard error, not a silent fallback (`design/01 §5.5` table, `robyn/01 §4.3`, KNOWN-as-recommendation). Under free-threading this becomes *more* load-bearing: sync handlers on the bounded pool run truly in parallel, so the per-request context is the only thing preventing a *concurrent* cross-request `ContextVar` data race (`design/01 §3.3a`, KNOWN). Where: **Rust** allocates once/req under GIL #1; **Python** semantics.
2. **The detach-across-await invariant, clippy-enforced.** `#![deny(clippy::await_holding_lock)]` workspace-wide + a custom check/review-gate that greps every `with_gil`/`attach` span for `.await` (`design/01 §2.2`, `robyn/01 §9.3`, KNOWN-as-recommendation). The internal `without_gil`/`with_gil` wrappers take a **non-async closure type** so "await inside with_gil" *does not compile* where structurable (`design/01 §2.2`, INFERRED design technique). This is a correctness-*and*-stability invariant: holding the GIL across an await freezes the whole interpreter (`robyn/01 §10` pitfall 1, KNOWN). Where: **Rust** + clippy + CI.
3. **No GIL on the routing path; `parking_lot` everywhere; freeze routing tables.** Routing/marshalling decisions made GIL-free in Rust; do not acquire the GIL to clone a handler handle (fixes `http_router.rs:53`, `robyn/02 §2`, KNOWN). Use `parking_lot::Mutex`/`RwLock` uniformly (no `std::sync::RwLock` poisoning, `robyn/02 §8`, KNOWN). Freeze routing/const-tier tables after boot into an `Arc<ImmutableRouter>`; hot-reload swaps the whole `Arc` (RCU), never mutates in place (`design/01 §3.3a`, KNOWN). Where: **Rust**.
4. **Free-threaded-clean by design (cheap now, a rewrite later).** Every `#[pyclass]` (lazy request proxy, response objects, the 5 custom awaitables) is `#[pyclass(frozen)]` + interior `Mutex`/atomics from day one — the single biggest day-one design constraint ft imposes (`design/01 §3.3a` item 5, KNOWN). No `GILProtected` (removed pyo3 0.28); no `static mut`/lazy globals — `parking_lot`/atomics only. `python3.14t` in CI from day one + a `sys._is_gil_enabled()` assertion to catch accidental GIL re-enable (`design/01 §3.2,§3.4`, KNOWN). Where: **Rust** design constraint.

**Constants:** Python floor = 3.11 (hard error below); clippy: `deny(await_holding_lock)`; CI matrix includes `cp314t`.

**Why this is the stable one:** Robyn shipped the tenant-leak bug; Lancelot makes it impossible by construction (per-request context from day one) and the one fatal GIL-across-await invariant is compiler-enforced, not convention. The ft-clean design means the no-GIL future is a build flag, not a breaking `#[pyclass]` rewrite.

---

## §8. Resource Leaks I — Bounded Memory: Eager Marshalling, Body Buffers, Fixed Per-Conn

### 8.1 The defects (KNOWN)

**Robyn — ~30–66 allocs/req even if the handler reads nothing; body buffered 3×; empty `PyDict`s always allocated.**
`Request::from_actix_request` → `into_pyobject` eagerly marshals headers (DashMap, ~4 allocs × N headers ≈ 48), query (2 `String`/pair), `Url` (3 `String`), and **always allocates empty `PyDict`s for `path_params`, `form_data`, `files` even when unused** + heap-allocs the `PyRequest` pyclass (`request.rs:32-71`, `robyn/02 §3.2-3.4`, KNOWN): *"~60 allocations / string copies for a trivial GET, none of which the handler may use."* The body is copied up to 3× (`BytesMut` → `.to_vec()` → `Request::clone` → `PyString/PyBytes`, `robyn/02 §8:513`, KNOWN) and **fully buffered before the handler is invoked** — no streaming intake (§3). `SYNTHESIS:32,94` summarizes: ~30–66 allocs/req eager total marshalling — *"the dominant per-request cost … exactly where Lancelot wins by going lazy."*

**Blazeio — fixed 100 KiB/connection regardless of need; GIL-serialized future churn ceilings concurrency.**
`INBOUND_CHUNK_SIZE = 102400`; *"every connection preallocates a 100 KiB `bytearray` once, at connection-accept time"* (`Protocols/__init__.py`, `blazeio/01 §1.2:69`, KNOWN, the explicit tradeoff): *"100 KiB × N connections = 100 MiB resident at 1000 conns even when idle. uvicorn/h11 grow buffers lazily. This is a deliberate latency-over-memory trade."* The §8 ceiling (`blazeio/01 §8:749,758`, KNOWN): fixed 100 KiB/conn + GIL-serialized `SharpEvent`/`Future` churn caps it at thousands of streams.

**FastAPI — ≈12–16 framework allocs/req + 2 `AsyncExitStack`s even with zero yield-deps + per-stream ≈8 frames resident.**
*"~12-16 heap allocations + ~6 coroutine frames of pure framework tax per request, before a single line of user code, for the simplest possible endpoint"* (`fastapi/01 §1.1`, KNOWN), including a *second* `Request` object (`fastapi/routing.py:111`, KNOWN) and **two `AsyncExitStack()` constructed every request even with zero yield-deps** (`routing.py:116,118`, `fastapi/02 §6.4(b)`, KNOWN). A long stream holds ≈8 frames + ≥3 exit-stacks + 2 memory streams + 2 producer tasks resident for the whole generation (`fastapi/01 §7.4`, KNOWN). The default response path also double-encodes (`jsonable_encoder` recursive walk → `json.dumps`, 2–3× memory, `fastapi/01 §6.2`, KNOWN).

### 8.2 The guard Lancelot ships (DESIGN)

1. **Lazy request proxy (~1–3 allocs/req).** A `#[pyclass(frozen)]` holding the raw Rust request + unconsumed pooled `Bytes`; headers/query/body/json materialize **on attribute access only** (SYNTHESIS steal #4, `robyn/02 §0,§9`, KNOWN). A handler reading one header pays one lookup, not 30–66 allocs. Combine with Blazeio's lazy-parse-on-first-iteration (`blazeio/01 §0.5`, KNOWN). Per-request framework alloc target **~1–3** (vs Robyn 30–66, FastAPI 10–15 — `SYNTHESIS:136`, KNOWN). Where: **Rust** lazy `#[pyclass]`.
2. **No empty-`PyDict` allocation.** `path_params`/`form_data`/`files` are lazy slices over the live request, materialized only on access (fixes Robyn's "always 3 empty `PyDict`s", `robyn/02 §3.4:514`, KNOWN). Where: **Rust**.
3. **Zero-copy body, copied only on `.json()`/`.text()`.** `memoryview`/`PyBytes` over the pooled Rust `Bytes` (`design/01 §2.3,§2.5`, `blazeio/01 §8`, KNOWN); no eager `String`/UTF-8 round-trip. Where: **Rust**.
4. **Max-body-size limit, per-route override.** `tower-http::limit::RequestBodyLimit`, default **16 MiB**; per-route override for upload endpoints; streaming intake (§3) for large bodies — never Robyn's eager 20-MB-resident-for-10-MB-upload (`robyn/02 §3.1:254`, KNOWN). Where: **Rust** tower layer.
5. **Pooled/refcounted Rust buffers, adaptive size.** Not Blazeio's fixed 100 KiB/conn — pool buffers, shrink after the request body is done (`blazeio/01 §8` "Tunables Lancelot must expose that Blazeio hardcodes", KNOWN). Where: **Rust**.
6. **Skip exit-stack allocation when zero yield-deps.** Registration knows per-route whether any yield-deps exist (`route.has_yield_deps`); skip the `AsyncExitStack` creation entirely if false (fixes FastAPI's "2 stacks every request even with zero yield-deps", `fastapi/02 §6.4(d)`, KNOWN-as-recommendation). The teardown emits a static `Vec<TeardownOp{slot,scope,is_async}>` at registration; Rust holds two drop-stacks as `Vec` (`fastapi/02 §6.4(e)`, KNOWN). Where: **Rust** drop-schedule.
7. **One-pass typed serialization only** — pydantic-core `dump_json()` → bytes in one Rust pass; **delete `jsonable_encoder` from the hot path** (fixes FastAPI's 2–3× double-encode, `fastapi/01 §6.2`, SYNTHESIS steal #6, KNOWN). Where: **Rust**/pydantic-core FFI.

**Constants:** `RequestBodyLimit` = 16 MiB default (per-route override); per-request framework alloc target ~1–3; receive buffer pooled + adaptive (not fixed 100 KiB).

**Why this is the stable one:** Robyn's eager marshalling is a memory-amplification DoS surface on large requests and a constant tax on every request; Blazeio's fixed 100 KiB/conn caps concurrency; FastAPI's per-stream ≈8-frame footprint limits concurrent streams. Lancelot materializes nothing until touched and bounds body size in Rust before Python — ~1–3 allocs/req, 16 MiB cap, pooled buffers.

---

## §9. Resource Leaks II — Yield-Dep / Exit-Stack / WS-Task / Future Closure

### 9.1 The defects (KNOWN — the load-bearing AI subsystem)

**FastAPI — correct *if* the exit-stack ordering holds; the failure is GC-finalization of streaming deps.**
The hardest case: an SSE endpoint that is an async generator streaming for minutes, using a `yield`-dep (DB session / LLM client) that must stay open *for the whole stream* and close *after* — **not when the generator is GC'd** (`fastapi/02 §6.3`, KNOWN). FastAPI solves it correctly: the generator is consumed by a producer task inside an `@asynccontextmanager` entered on the **request-scoped exit stack** (`routing.py:590`), so `__aexit__` runs via structured teardown after the streaming response completes, *not* via `GeneratorExit` (PEP-789, `fastapi/02 §6.3:979`, KNOWN). The teardown's stern warning (`fastapi/02 §6.3:992`, KNOWN): *"Lancelot must preserve this exact ordering or AI streaming apps will leak connections or close sessions mid-stream."* The failure modes if you get the ordering wrong: (a) a request-scoped DB session closed *during* the stream (used-after-close); (b) a connection *leaked* because teardown was left to GC and the generator was never finalized; (c) the `response_awaited` guard (`routing.py:124-131`, KNOWN) exists precisely because *"a `yield` dep swallowed the exception with bare `except`"* is a known bug class. The `scope="function"` vs `scope="request"` split (teardown before vs after body sent, `fastapi/02 §6.1`, KNOWN) is a footgun if mis-declared.

**Robyn — module-global WS task dict, never bounded, leaks a never-returning handler forever.**
`_connection_tasks` is a **module-global dict** (`ws.py:89`, `robyn/03:432`, KNOWN): *"not bounded and a leaked task (handler that never returns and never disconnects cleanly) stays forever."* The `close_handler` join has a 5.0 s timeout (`ws.py:242`, KNOWN, reasonable) but a handler that hangs without disconnecting is never reaped. The WS inbound mpsc is unbounded (§4). Robyn also has no per-app registry — with `processes>1` each process has its own copy, and a leaked task in any of them is permanent (compounding §2's no-respawn).

**Blazeio — `reTask` invisible to the watchdog; `Taskscope` leaks if a task holds references; the connection object is reused across requests (good, but the reset must be exhaustive).**
`reTask`s are invisible to `asyncio.all_tasks()`, so `ReMonitor.analyze_protocols` (which iterates `all_tasks(loop)`) **cannot see them** (`blazeio/03 §2.2:180`, KNOWN, INFERRED limitation) — a wedged `reTask` is unreapable by Blazeio's own watchdog. `Taskscope` mangles attributes onto `current_task()` as a contextvars substitute (`blazeio/03:283`, KNOWN): *"leaks if a task holds references … invisible to debuggers."* The keepalive-connection reset (`server_protocol_extratools.py:33-58`, `blazeio/01 §6:627`, KNOWN) zeroes state + clears `__stream__`/`__evt__`/`__overflow_evt__` and reuses the 100 KiB buffer — *good* for allocation, but any state the `reset` forgets leaks into the next request on that connection (the same bug class as Robyn's `ContextVar` leak, §7).

### 9.2 The guard Lancelot ships (DESIGN)

1. **Streaming resource teardown structurally request-scoped, never GC** (FastAPI's PEP-789 pattern, `fastapi/02 §6.3`, KNOWN — the non-negotiable AI ordering). Registration emits a per-route `Vec<TeardownOp{slot,scope,is_async}>`; Rust holds request-scope and function-scope drop-stacks as `Vec`; on the function/request boundary it runs the schedule LIFO (`fastapi/02 §6.4(e)`, KNOWN). The streaming case: Rust owns the producer task + bounded ring; the drop-stack is unwound by Rust **after the response future completes** — same ordering as FastAPI's `request_stack.__aexit__`, no Python `AsyncExitStack`, no `contextmanager` wrapper alloc. Only the user yield-fn's pre-`yield`/post-`yield` segments run in Python (`next()`/`asend()` twice). Where: **Rust** drop-schedule; **Python** only the user yield segments.
2. **Preserve the exact `scope="function"` (before body) vs `scope="request"` (after body) teardown ordering** (`fastapi/02 §6.1`, KNOWN) — byte-for-byte, because the ecosystem depends on it and getting it wrong leaks connections or closes sessions mid-stream. The scope-mismatch guard (a request-scoped yield-dep may not depend on a function-scoped one, `fastapi/02:331,365`, KNOWN) is enforced at registration. Where: **Rust** schedule built from **Python** signature reflection.
3. **The `response_awaited` guard** (FastAPI `routing.py:124-131`, KNOWN) — detect a yield-dep that swallowed the exception with bare `except`; surface a `LancelotError` not a silent leak. Where: **Rust**/Python boundary check.
4. **Bounded WS inbound channel + per-app task registry + reaper** (fixes Robyn's module-global unbounded `_connection_tasks`, `robyn/03:432`, KNOWN): a per-app registry with a max + a reaper that cancels a WS handler task that has neither returned nor seen a frame within an idle deadline; `close_handler` join timeout 5 s (Robyn's value, KNOWN, configurable). Where: **Rust** registry + reaper.
5. **Real `asyncio.Task`, never `reTask`** — preserves `all_tasks()` visibility (so a watchdog/`/metrics` can see and reap a wedged task), structured cancellation, and contextvars (`blazeio/03 §0.1` reject-clause, SYNTHESIS steal #13, KNOWN). Real `contextvars.ContextVar` for request scope, never Blazeio's `Taskscope` attribute-mangling (`blazeio/03:283`, KNOWN-as-recommendation). Where: **Python**.
6. **Exhaustive connection reset on keepalive reuse.** If Lancelot reuses a connection object across requests (Blazeio's win, `blazeio/01 §6:627`, KNOWN), the reset must zero *all* per-request state; the per-request `contextvars.Context` (§7) is fresh per request precisely so a forgotten reset field cannot leak tenant state. Where: **Rust** connection reset.
7. **Cancellation-aware bridge futures.** The 5 custom awaitables wire `tokio::select! { biased; result = fut, () = cancel_tx.notified() => drop_ref }` (Robyn `runtime.rs:185-204`, `robyn/01 §6.3`, KNOWN) so a cancelled stream drops the Rust task instead of leaking it. Where: **Rust**.

**Constants:** WS handler idle-reap deadline = configurable (default e.g. 300 s); WS `close_handler` join = 5 s (Robyn's, configurable); per-app WS task registry max = configurable.

**Why this is the stable one:** FastAPI's correct-but-fragile exit-stack ordering becomes a compiled Rust drop-schedule (the ordering is structural, not GC-dependent, and not re-derivable to break); Robyn's unbounded module-global WS task dict becomes a bounded, reaped, per-app registry; Blazeio's invisible-to-watchdog `reTask` becomes a real `Task`. No connection, session, or task leaks across a long stream or a wedged handler.

---

## §10. Error Recovery: Handler Raises / Dep Fails / Upstream Fails / Malformed JSON / Truncated Body

### 10.1 The defects (KNOWN)

**Blazeio — exception-as-the-primary-return-mechanism: non-local, untestable, slower when raised.**
Success-by-imperative-write but error/early-exit/HEAD by `raise Abort("msg",404)` / `raise NotFoundErr(...)` / `raise Eof()` caught in the keep-alive loop which calls `e.text(r)` to emit the response (`server_protocol.py:96-121`, `blazeio/02 §5`, KNOWN). The verdict (`blazeio/02 §5:455-458`, KNOWN): *"Exceptions as normal control flow are slower than returns in CPython when actually raised+caught … `raise Eof()` for every HEAD request and `raise Eof("Request has no headers")` for every malformed/probe request — those are exception-per-request on non-negligible traffic classes (health probes, HEAD checks). Measurable under load."* And: *"Control flow is non-local and invisible at the call site … hard to reason about, hard to unit-test handlers in isolation."* And the masking bug: *"`except Exception as e: exc = e` then 500 in `finally` swallows the original traceback location … a bug in `after_middleware` or `eof()` inside `finally` can mask the real error"* (`blazeio/02:457`, KNOWN). The casing fragility (`Deliver`/`Abort` hand-checking `"Content-type"`/`"Content-Type"`/`"content-type"`, `streaming.py:89,93`, KNOWN) is *symptomatic of* "no typed response, just dicts and exceptions."

**FastAPI — correct error mapping, but double-validates on output and rebuilds the dependant under override.**
The 3-layer exception pipeline is cheap on the happy path (zero-cost exceptions) and the MRO-based handler lookup + exact `422 {"detail":[...]}` shape is correct and must be matched (`fastapi/01 §9`, KNOWN). But: **response validation runs every request** and *"for a sync endpoint this is itself punted to the 40-token threadpool — a second threadpool hop just to validate the response"* (`serialize_response:293-295`, `fastapi/01 §6.1`, KNOWN); a malformed body maps to `RequestValidationError` `type:"json_invalid"` `loc:("body",e.pos)` (`routing.py:427-441`, KNOWN, correct) but a non-JSON parse error → generic `HTTPException(400,"There was an error parsing the body")` (`routing.py:445-449`, KNOWN). `deepcopy(default)` per missing optional + full `get_dependant()` rebuild per request when dependency overrides are active (`fastapi/02 §9`, KNOWN) — an error-amplification path under override-heavy test/staging.

**Robyn — any handler error → blanket 500; a malformed auth dict → panic (process death).**
A handler that raises → `execute_http_function` returns `Err` → `index()` logs the traceback and returns `Response::internal_server_error(None)` (HTTP 500) for *everything* (`server.rs:577-586`, `robyn/01 §2.7`, KNOWN) — no typed mapping, no 422/400/502 distinction, no per-exception handler MRO. And `from_py_dict` `unwrap()`s extraction so *"a non-string value panics"* (`robyn/02:463`, KNOWN) — with `panic="abort"` a crafted auth-middleware payload is a worker kill (compounding §2). No circuit breaker, no upstream-fail mapping (Robyn has no built-in LLM client).

**Truncated body / malformed input — all three.** Blazeio: connection closes mid-headers → 400 (`blazeio/03:104`, KNOWN), but `handle_raw` *"does not split the final chunk at `content_length`"* so an over-read mis-attributes bytes to the next pipelined request (`blazeio/01 §5.1:506`, KNOWN gap), and chunk-extensions break the chunked decoder with a `ValueError` (`blazeio/01 §5.3:562`, KNOWN). FastAPI: body parse errors mapped (above). Robyn: body fully buffered, a truncated body just yields a short `Vec` with no explicit framing-error distinction.

### 10.2 The guard Lancelot ships (DESIGN)

1. **One collapsed request-scope error funnel** (steal FastAPI's structure, collapse the 4 wrappers to 1): a single `try/except` with the MRO-based most-specific-handler lookup + status-code-handler-first-for-HTTPException ordering + the **exact `422 {"detail":[{type,loc,msg,input,ctx}]}` wire shape** (clients/tests depend on it, `fastapi/01 §9.2`, SYNTHESIS steal #17, KNOWN) + `EndpointContext` source attribution (cheap, cached by `id(func)`, `fastapi/01 §9.2:649`, KNOWN). Keep Blazeio's genuinely-good `finally`-guaranteed cleanup funnel (`eof()`/`reset()` on every path, `blazeio/02 §5:450`, KNOWN) **but reject success-via-`raise`** — typed returned response objects, not exception-as-control-flow (`SYNTHESIS §3` Blazeio-AVOID, KNOWN). HEAD handled by Rust (no `raise Eof()` per HEAD). Where: **Rust** catch-all + the one Python try/except; **Python** user handlers.
2. **Malformed JSON → 422 `json_invalid`** with `loc:("body",pos)` (match FastAPI byte-for-byte, `routing.py:427-441`, KNOWN); **truncated/short body vs Content-Length → 400** with an explicit framing error; **chunk-extension-bearing chunked body parsed correctly** (Rust `hyper` handles extensions — fixes Blazeio's `ValueError`, `blazeio/01 §5.3`, KNOWN); **over-read split at `content_length`** so pipelined requests aren't mis-attributed (fixes Blazeio `handle_raw`, `blazeio/01 §5.1:506`, KNOWN). Where: **Rust** parser.
3. **Never panic on user input** — Robyn's `from_py_dict` `unwrap()` (`robyn/02:463`, KNOWN) → Lancelot returns `Err(PyErr)` → 422, never a process kill (cross-ref §2). Where: **Rust**.
4. **Upstream LLM failure → typed 502 + circuit breaker; POST never auto-retried.** The built-in LLM client (per `DESIGN-DECISIONS §4`) maps a provider 5xx/timeout/connection-fail to a typed `UpstreamError` → 502 (or a user-handled exception), trips a per-host circuit breaker, and **never auto-retries a POST** (Blazeio's correct idempotency rule, `blazeio/03 §4.5:390`, KNOWN — *"never silently replay a non-idempotent LLM call"*; retry only pre-first-byte connect failures, `blazeio/03:441`, KNOWN-as-recommendation). Where: **Python/Rust** LLM client.
5. **No double-validate on output for the typed path** — pydantic-core `dump_json()` once (fixes FastAPI's per-request response re-validation + the sync-endpoint second threadpool hop, `fastapi/01 §6.1`, KNOWN). Don't rebuild the dependant per request under override (cache it; `fastapi/02 §9`, KNOWN). Where: **Rust**/pydantic-core.
6. **Disconnect/lifecycle exceptions are NOT errors** — classify `ClientDisconnected`/`Eof`/`ServerDisconnected`/`CloseConnection`/`BrokenPipeError`/`CancelledError` as normal lifecycle and **swallow silently** (no stack trace when a client hangs up — Blazeio's genuinely-good classification, `blazeio/03 §1:100`, KNOWN-as-steal). Where: **Rust** + the one Python funnel.

**Constants:** validation error status = 422, body `{"detail":[...]}` (match FastAPI exactly, KNOWN); body-parse error = 400; upstream fail = 502; circuit-breaker thresholds = configurable; POST retries = 0 (connect-only, pre-first-byte).

**Why this is the stable one:** Blazeio's exception-as-control-flow is slow on probe/HEAD traffic and masks real errors; Robyn 500s everything and panics on a bad dict; FastAPI double-validates. Lancelot has one typed funnel with FastAPI's exact wire shapes, Blazeio's `finally`-guaranteed cleanup, no success-via-raise, no panic on input, and a circuit-broken non-retried upstream.

---

## §11. Anti-Pattern Dispatch (Inherited Stability Traps Lancelot Must Lock Out)

### 11.1 The defects (KNOWN — FastAPI's tutorial-recommended slow paths)

**`@app.middleware("http")` / `BaseHTTPMiddleware` — a task-group + 2 memory streams + a cached request *per request*.**
`BaseHTTPMiddleware.__call__` per request builds a `_CachedRequest`, an `anyio.Event`, an `anyio.create_memory_object_stream[Message]()`, enters `anyio.create_task_group()`, and `start_soon`s the downstream app with a `receive_or_disconnect` wrapper that **spawns a nested task group per `receive()`** (`base.py:100-199`, `fastapi/01 §8.4`, KNOWN): *"every single request through a `BaseHTTPMiddleware` allocates a task group + a memory object stream + a cached request + (per receive) another task group … by far the most expensive thing a FastAPI app commonly does, and `@app.middleware("http")` is the most-recommended way to add middleware in tutorials. The framework's own docs nudge users straight into the slow path."* For a streaming response it proxies every chunk through the memory stream. Plus the always-on 4-layer middleware/exception nesting adds *"3 extra Python function calls per chunk"* for streaming (`fastapi/01 §8.3`, KNOWN) — ~3000 wrapper `send` calls for a 1000-token stream.

**`BackgroundTasks` — run sequentially inside the request coroutine after body send: "return fast, work later" is silently broken.**
`for task in self.tasks: await task()` runs **after the body is sent, inside the same request coroutine, strictly sequentially**, sync tasks through the 40-pool (`background.py:31-33`, `responses.py:171,285`, `fastapi/01 §10`, KNOWN): *"A 30s background embedding job keeps a request slot occupied for 30s after the user got their 200 … with keep-alive the connection isn't freed … sequential execution means N background tasks = sum of their latencies. This is a frequent FastAPI production surprise."* Directly compounds §1 (a graceful drain waiting on in-flight requests hangs for the worst background-task duration).

**Robyn — sync handler not offloaded (pins a reactor worker); GIL on the routing path.** Covered in §4/§7 — restated here as a dispatch anti-pattern Lancelot must not reproduce.

### 11.2 The guard Lancelot ships (DESIGN)

1. **No `call_next` dispatch model.** Public middleware API is pure-ASGI-shape or a Rust/tower chain only; **never** a `dispatch(request, call_next)` that needs a task-group + memory-stream + cached-request bridge (`fastapi/01 §8.4`, SYNTHESIS §3 AVOID, KNOWN). CORS/GZip/HTTPS-redirect/trusted-host are **Rust-core middleware** (header checks + gzip have no business in Python, `fastapi/01 §8.5`, KNOWN). If a `call_next` ergonomic is demanded, implement it without a task group (the Rust core already knows disconnect — no anyio race needed). Where: **Rust**/tower.
2. **Collapse the 4 always-on wrappers into 1.** `ServerErrorMiddleware` + `ExceptionMiddleware` + `wrap_app_handling_exceptions` + `AsyncExitStackMiddleware` → one request-scope wrapper (one try/except + one drop-schedule + one `send`-interceptor) — one frame + one `send` wrapper instead of four, eliminating the per-chunk 3× wrapper tax on streams (`fastapi/01 §8.3`, KNOWN-as-recommendation). Where: **Rust** + one Python funnel.
3. **Real detached background tasks.** `background.spawn()` enqueues into a **Rust-owned worker pool** (separate from request workers, bounded queue, configurable concurrency); the request coroutine returns **immediately** after body flush; tasks run truly detached and concurrently. Opt-in `await_in_request=True` for the rare old-semantics case. Surface queue depth in `/metrics`. Fixes FastAPI's connection-blocking sequential model (`fastapi/01 §10`, SYNTHESIS §3 AVOID, KNOWN) — and makes §1's graceful drain bounded (it no longer waits on background work). Where: **Rust** pool; **Python** API.
4. **Sync handlers always offloaded to the bounded `BlockingRunner`, wired at registration** (fixes Robyn's "exists but not wired", `robyn/02 §5b`, KNOWN) — cross-ref §4. Reject sync generators for streaming (cross-ref §5). Where: **Rust**.
5. **No GIL on the routing/dispatch path** (cross-ref §7) — clippy-enforced detach-across-await. Where: **Rust** + clippy.

**Constants:** background pool concurrency = configurable; background queue bound = configurable (default e.g. 10 000); built-in middleware (CORS/GZip/etc.) = Rust, config-toggled.

**Why this is the stable one:** FastAPI's most-recommended middleware path is its most expensive, and its background-task model silently breaks "return fast" *and* hangs graceful drain. Lancelot locks both out at the API level — no `call_next` task-group bridge exists to be used, background tasks are genuinely detached, sync is always offloaded.

---

## §12. Liveness ≠ Readiness, cgroup-Awareness, Fail-Fast Boot

### 12.1 The defects (KNOWN — none of the three split liveness/readiness; Robyn hangs containers)

**None of the three split liveness from readiness** (`design/01 §5.7` item 12, INFERRED, KNOWN-none-do): a saturated worker (sync pool full, or draining) still answers a health probe 200 → the load balancer keeps routing to a worker that cannot serve, *or* a slow health endpoint that shares the saturated Python pool fails liveness and the orchestrator kills a *healthy-but-busy* worker (the classic "health check death spiral").

**Robyn — `os.cpu_count()` ignores cgroup quotas; `input()` on port conflict hangs containers forever.**
`--fast` uses `(os.cpu_count()*2)+1` processes (`argument_parser.py:107`, `robyn/03 §3.5:258`, KNOWN): *"`os.cpu_count()` ignores cgroup CPU quotas (containers) … so it doesn't spawn 65 processes in a 2-vCPU container"* — 65 interpreters in a 2-vCPU cgroup = thrash + OOM-kill. And the port-in-use path: `Robyn.start` has an *"interactive reprompt loop — blocks on `input()`, bad for containers"* (`__init__.py:712`, `robyn/03:589,700`, KNOWN): a port conflict in a container hangs the boot **forever** waiting on stdin that never comes (no fail, no crash, no restart — the orchestrator sees a "running" pod that never serves).

**Robyn — `--dev` CLI-only; `SystemExit` on `python app.py --dev`.** (`__init__.py:144-149`, `robyn/03:696`, KNOWN) — a DX papercut, but also a boot-mode footgun (the wrong invocation hard-exits instead of degrading).

### 12.2 The guard Lancelot ships (DESIGN)

1. **Liveness ≠ readiness, both const-tier (zero Python).** `/livez` = process up (served from the Rust const-tier — **passes even when the Python pool is 100% saturated**, because it never enters Python; `design/01 §5.7` item 12, DESIGN-DECISIONS §1 fork-5: the const-tier *is* the substrate for `/livez`). `/readyz` = accepting **AND** sync-pool not saturated **AND** drain not in progress (so the LB stops routing to a draining/saturated worker but the orchestrator does *not* kill a busy-but-alive one). `/startupz` for slow-boot (model warmup) gating. All three Rust const-tier — K8s probe + Docker HEALTHCHECK + AWS LWA, one mechanism (DESIGN-DECISIONS §1 fork-5, KNOWN). Where: **Rust** const-tier.
2. **cgroup-aware worker count.** Read cgroup v2 `cpu.max` (or `len(os.sched_getaffinity(0))`), never `os.cpu_count()` (fixes Robyn `argument_parser.py:106`, `robyn/03 §3.5:258`, KNOWN). `worker_threads = clamp(cgroup_cpus, 2, 4)` (`design/01 §4.2`, KNOWN). Where: **Rust**/bootstrap.
3. **Fail-fast boot, never `input()`.** Port conflict → clear error to stderr + **non-zero exit** (so the orchestrator restarts/reports), never block on stdin (fixes Robyn `__init__.py:712`, `robyn/03:700`, KNOWN-as-recommendation). Fail-fast config validation (`design/01 §5.7` item 9, DESIGN-DECISIONS §3: "fail-fast config validation"). `lifespan.startup.failed` aborts boot (don't serve a half-initialized app — FastAPI's correct contract, `fastapi/01 §11.2`, KNOWN-as-steal). Where: **Rust**/bootstrap + **Python** lifespan.
4. **`--dev` works as `python app.py --dev`** (self-exec into a reloader supervisor, uvicorn-style) — not CLI-only `SystemExit` (fixes Robyn `__init__.py:144-149`, `robyn/03:400`, KNOWN-as-recommendation). Where: **Python** bootstrap.

**Constants:** worker count = `clamp(cgroup_cpus, 2, 4)`; `/livez`/`/readyz`/`/startupz` const-tier (zero GIL); port conflict → exit code ≠ 0; `lifespan.startup.failed` → boot abort.

**Why this is the stable one:** none of the three let an orchestrator distinguish "process alive but busy" from "process should be replaced," and Robyn's `os.cpu_count()` + `input()` actively break containers. Lancelot's const-tier `/livez` passes under a saturated Python pool (no death spiral), `/readyz` sheds the LB cleanly, the worker count respects the cgroup, and a port conflict fails fast instead of hanging the pod forever.

---

## §13. The Drain + Respawn Supervisor — Reimplementable Reference (DESIGN)

§1 and §2 are the two gates with no analogue in any of the three codebases (Robyn `processpool.py` has neither; Blazeio is single-process; FastAPI delegates to the ASGI server). They are therefore the highest-risk to under-specify. This section is the reimplementable spec — the exact state machine, signals, and constants — so the guard is buildable, not aspirational.

### 13.1 Process topology (the substrate, KNOWN from Robyn)

The substrate is Robyn's verbatim socket model (`shared_socket.rs:1-44`, `robyn/01 §1.2`, KNOWN): parent `SocketHeld::new` → `SO_REUSEPORT`+`SO_REUSEADDR`+`TCP_NODELAY`, `listen(16384)`, `set_nonblocking(true)`; per child `try_clone()` the fd; kernel load-balances accept. **What Lancelot adds** (the two missing pieces, `robyn/01 §9.4` table KNOWN-as-recommendation: *"ADD: respawn-on-crash (Robyn doesn't)"*; `robyn/03 §3.5:255` KNOWN-as-recommendation): a supervisor that owns the child handle table and a drain protocol.

### 13.2 The supervisor state machine (DESIGN — reimplementable)

```
parent:
  bind SO_REUSEPORT socket once               # Robyn shared_socket.rs verbatim (KNOWN)
  children = {}
  for i in range(cgroup_aware_count()):        # §12: clamp(cgroup_cpus,2,4), NOT os.cpu_count() (fixes robyn/03:258)
      children[i] = spawn(fd.try_clone(), gen=0)
  install SIGTERM/SIGINT -> state = DRAINING   # fixes robyn processpool.py:55 process.kill()
  install SIGCHLD -> reap()

  loop every SUPERVISE_TICK (250 ms):
    for i, ch in children:
      if not ch.alive():                       # crashed (panic=abort) OR OOM-killed
        if state == RUNNING:
          rc = ch.restart_count_in(RESTART_WINDOW=30s)
          if rc < MAX_RESTARTS=5:
            backoff = min(RESTART_BASE=200ms * 2**rc, RESTART_CAP=10s)
            sleep(backoff); children[i] = spawn(fd.try_clone(), gen=ch.gen+1)
          else:
            mark_permanently_failed(i); alert()  # do NOT busy-restart a deterministic crash
    if state == DRAINING:
      broadcast DRAIN to all live children      # one signal/pipe write per child
      wait until all children exited OR GRACEFUL_TIMEOUT=30s
      for ch still alive: ch.SIGKILL()
      run_parent_shutdown_hooks(); exit(0)

child (on DRAIN signal):
  stop accept() on listening fd                 # leave SO_REUSEPORT set: kernel routes NEW conns to live workers
  hyper_graceful = GracefulShutdown::new()
  for every new response while draining: set "Connection: close"
  signal SSE/stream producers: "finish current event then close"   # §5/§6 ring carries a drain flag
  hyper_graceful.shutdown().await  with deadline GRACEFUL_TIMEOUT   # resolves when in-flight done OR timer
  run drop-schedule (yield-dep/lifespan teardown, LIFO)             # §9 Rust drop-stack, after last byte
  exit(0)
```

**Every constant is justified:** `SUPERVISE_TICK=250 ms` (fast enough that a crashed worker is replaced sub-second, slow enough to not spin); `MAX_RESTARTS=5 / RESTART_WINDOW=30 s` (a deterministic crash — e.g. a bad migration — must not crash-loop-pin the box, `design/01 §5.2.1`, KNOWN-as-rec); `RESTART_BASE=200 ms ×2 cap 10 s` (exp backoff); `GRACEFUL_TIMEOUT=30 s` (the in-flight quiescence window, `design/01 §5.1`). The `gen` counter distinguishes a respawned child from the original in logs/metrics. **SIGCHLD reap** prevents zombie accumulation (Robyn `process.join()`s in a loop after kill, `processpool.py:65-66`, KNOWN — Lancelot reaps asynchronously so the supervisor loop is never blocked).

### 13.3 Why each branch fixes a cited defect

| Supervisor branch | Fixes (KNOWN cite) |
|---|---|
| `SIGTERM -> DRAINING` (not `process.kill()`) | Robyn `processpool.py:55` drops 100% in-flight (`robyn/03:197`) |
| `not ch.alive() -> respawn` | Robyn no supervisor → panic permanently loses a worker (`robyn/03:257,693`) |
| `rc >= MAX_RESTARTS -> alert, stop` | a crash-loop is its own outage; Robyn has neither respawn nor cap |
| `stop accept(), leave SO_REUSEPORT` | kernel reroutes new conns to live workers — zero-downtime deploy (Robyn has no drain at all) |
| `hyper_graceful.shutdown() w/ deadline` | bounded quiescence; Blazeio's `Taskmng` cancels in-flight instead of finishing (`blazeio/03:34`) |
| `run drop-schedule after last byte` | backend connection (DB/LLM) closed cleanly even on deploy (fixes the §9 GC-leak risk during drain) |
| `cgroup_aware_count()` | Robyn `os.cpu_count()` spawns 65 procs in a 2-vCPU cgroup (`robyn/03:258`) |
| SIGCHLD async reap | Robyn parent blocks on `process.join()` loop (`processpool.py:65-66`) |

This is the single largest "all three fail, Lancelot must not" surface — Robyn has *neither* piece, Blazeio cancels rather than drains, FastAPI's drain is uvicorn's and is hung by its own sequential BackgroundTasks (§11). The state machine above is the spec the implementation is graded against.

---

## §14. The Rust↔Python Seam: Panic-Propagation & GIL-Boundary Race Taxonomy (KNOWN + DESIGN)

§7 covered the contextvars leak and the await-holding-GIL invariant. This section is the exhaustive seam-failure taxonomy at reimplementable depth — every distinct way the FFI boundary itself can fail, the Robyn evidence, and the Lancelot guard. This is the subsystem most likely to be under-covered because it is invisible in pure-Python frameworks (FastAPI/Blazeio have no seam) — but it is *the* load-bearing surface for a Rust-core framework.

### 14.1 Panic-source taxonomy on the request path (KNOWN — Robyn, every source)

`panic="abort"` means each of these is a **whole-process kill** with no respawn in Robyn (`robyn/01 §0:92`, `robyn/03:626`, KNOWN). Enumerated from the Robyn teardowns:

| Panic source | Robyn site (KNOWN) | Trigger | Lancelot guard (where) |
|---|---|---|---|
| `unwrap()` on `TASK_LOCALS` | `server.rs:256` (`robyn/01 §2.7`) | never in practice but fragile-by-construction | clippy `unwrap_used` deny on request path (Rust CI) |
| `.read().unwrap()` poisoned lock | `server.rs:150`; `middleware_router` `std::sync::RwLock` (`robyn/02 §8:518`) | a panic *while holding* the lock poisons it → every subsequent `.read().unwrap()` panics → cascade | `parking_lot` everywhere (no poisoning); clippy deny (Rust) |
| `extract()`/`from_py_dict` `unwrap()` on user data | `http_router.rs:91` (`robyn/02:463`) | a non-string value in an auth-middleware dict | return `Err(PyErr)` → 422, never panic (Rust seam) |
| index/slice out of bounds | implied throughout marshalling | malformed/short body, header edge | clippy `indexing_slicing` deny; checked slicing (Rust) |
| `Py<T>` misuse / refcount error | the FFI seam generally (`robyn/01 §0:92`) | a logic bug in marshalling | `catch_unwind`/unwind-scope at the seam → 500 (Rust) |
| `StatusCode::from_u16(code).unwrap()` | `executors/mod.rs` (`robyn/02:364`) | a handler returns status 999 | clamp/validate at registration + seam (Rust) |
| startup `.listen().unwrap().run().await.unwrap()` | `server.rs:282-285` (`robyn/01 §2.7`) | port bind fail | fail-fast non-zero exit (§12), not panic (bootstrap) |

**The compounding chain (KNOWN):** poisoned `std::sync::RwLock` (Robyn `middleware_router`, `robyn/02 §8`) → next `.read().unwrap()` panics → `panic="abort"` → process dies → no supervisor → worker permanently gone. **Three cited defects chain into one fatal outcome.** Lancelot breaks the chain at every link: `parking_lot` (no poison) + clippy-deny `unwrap` (no panic) + supervisor (respawn if it somehow does) + `catch_unwind` at the seam (a marshalling panic is a 500).

### 14.2 The GIL-boundary race taxonomy (KNOWN Robyn + DESIGN ft)

The seam has three race surfaces, each with a distinct guard:

1. **GIL held across `.await`** — the one fatal invariant (`robyn/01 §10` pitfall 1, KNOWN): freezes the *entire interpreter* for the await duration; throughput → ~1. Robyn obeys by *convention only* (`executors/mod.rs:524-535` closes `with_gil` before `.await`). Guard: clippy `deny(await_holding_lock)` + the `without_gil` wrapper taking a non-async closure so it *cannot compile* (§7 item 2). Where: **Rust** + CI.
2. **Sync Python on the reactor thread** — holds the GIL *and* occupies a reactor thread for the handler's full duration; every async request on that thread stalls (`robyn/01 §10` pitfall 2, KNOWN). Robyn ships the `BlockingRunner` but **doesn't wire it to the HTTP path** (`robyn/02 §5b:441`, KNOWN). Guard: registration-time async/sync classification routes sync → bounded `BlockingRunner` with GIL-`detach`-on-`recv` (§4). Where: **Rust**.
3. **Shared-state race under free-threading** — every `#[pyclass]`/module-state structure implicitly GIL-serialized today becomes a genuine data race under `python3.14t` (`design/01 §3.3a`, KNOWN; pyo3 0.28 `Python<'py>` = "attached" not "exclusive"). The structure-by-structure hazard map (`design/01 §3.3a`, KNOWN): `TASK_LOCALS` OnceCell (init fine; `Py<T>` refcount must go through pyo3 0.28 ft-aware path), route/const maps (freeze into `Arc<Immutable>` after boot, RCU-swap on hot-reload), per-request `contextvars.Context` (safe by construction — and *more* load-bearing under ft because sync-pool handlers run truly parallel), blocking-pool queue (`crossbeam` already MPMC-safe), **every `#[pyclass]` field (the pervasive one — must be `frozen` + interior-sync from day one)**, `static mut`/lazy globals (none allowed; `parking_lot`/atomics only). Guard: design ft-clean now (cheap) — the §7 item-4 constraint. Where: **Rust** design constraint + `cp314t` CI.

### 14.3 The exact GIL choreography to enforce (KNOWN — port verbatim from Robyn)

The reimplementable invariant (`robyn/01 §2.4`, `executors/mod.rs:519-545`, KNOWN, verbatim):

```
async: with_gil{ call handler -> coro; create_task(coro, ctx); into_future }  GIL#2 ~µs
       DROP GIL ───────── .await (asyncio loop steps coro) ───────── GIL-FREE
       with_gil{ pydantic-core validate_json/dump_json (1 Rust pass) }        GIL#3 ~µs
sync:  BlockingRunner.run(|py| { ctx.run(handler, req); marshal })  — GIL on a POOL thread only
```

For `async def chat(): return await llm.generate(...)` the framework holds the GIL **single-digit µs total per request**; the 800 ms LLM call is entirely GIL-free; 5000 concurrent LLM calls = 5000 parked coroutines (`robyn/01 §7.1`, KNOWN). **This is the stability property** — without it, concurrency collapses to 1. The catalog gate (checklist #11, §11 item 5, §7 item 2): every `with_gil` span is `.await`-free, clippy-enforced, not convention.

---

## §15. Timeout Coverage Matrix — Every Deadline, Every Framework (KNOWN)

The brief explicitly asks for timeout coverage: per-request, header, idle, total. None of the three has full coverage; this is the exhaustive matrix and Lancelot's complete deadline set.

| Deadline | FastAPI | Robyn | Blazeio | Lancelot guard (where) |
|---|---|---|---|---|
| **Header-receipt** | ✗ none (uvicorn's, permissive) | ✗ `client_request_timeout(0)` disabled `server.rs:280` (`robyn/01 §3.1`) | ✗ none, only 102400 byte cap + O(n·k) re-scan (`blazeio/01 §8:751`) | `hyper header_read_timeout`=30 s, 0=disable documented (Rust) |
| **Body-read** | ✗ none | ✗ none (eager full buffer, `robyn/02 §3.1`) | ✗ none (TCP keepalive only) | tower body-stream deadline=60 s, per-route 0 (Rust) |
| **Total request** | ✗ none | ✗ none | ✗ none | `tower-http::timeout`=120 s, per-route 0 (Rust) |
| **Idle / keep-alive** | ◐ uvicorn keep-alive (default ~5 s) | ◐ `KeepAlive::Os` — delegated to OS TCP (`server.rs:278`, `robyn/01 §3.1`) | ◐ advertises `Keep-Alive: timeout=18000` (5 h!) `server_protocol.py` (`blazeio/01 §6:628`) — effectively no idle reap | hyper keep-alive idle timeout, configurable default 75 s (Rust) |
| **Sync-handler queue wait** | ✗ unbounded after `CapacityLimiter(40)` (`fastapi/01 §5`) | ✗ `unbounded()` queue (`robyn/01 §5.6`) | — (no thread pool) | bounded queue + 503 before Python (Rust, §4) |
| **Outbound LLM call** | — (userland httpx) | — (no client) | ◐ external watchdog thread, racy; `reTask` invisible (`blazeio/03 §4.5`) | in-band `asyncio.wait_for` per request, NOT a watchdog (Python client) |
| **WS handler idle** | — | ✗ leaked task lives forever, module-global dict (`robyn/03:432`) | — | per-app registry + idle reaper (Rust, §9) |
| **WS close join** | — | ◐ 5.0 s `ws.py:242` (`robyn/03:433`) | — | 5.0 s configurable (Rust, §9) |
| **Graceful drain** | ◐ uvicorn's, hung by seq BG-tasks (`fastapi/01 §10`) | ✗ none, `process.kill()` (`robyn/03:197`) | ◐ cancels not finishes (`blazeio/03:34`) | `graceful_timeout`=30 s (Rust supervisor, §1) |
| **Startup / boot** | ◐ `lifespan.startup.failed` aborts (`fastapi/01 §11.2`) | ✗ `input()` hangs forever on port conflict (`robyn/03:700`) | ◐ degrades silently | fail-fast non-zero exit + `/startupz` (Rust/bootstrap, §12) |

**Blazeio's `Keep-Alive: timeout=18000` (5 hours)** is a quietly severe one (`server_protocol.py:76`, `blazeio/01 §6:628`, KNOWN, `keepalive_headers` precomputed with `timeout = 60*60*5`): combined with no header-receipt deadline (§3) it means a slow-loris connection is held for **up to 5 hours** by the advertised keep-alive alone. Lancelot's idle keep-alive default of 75 s + a 30 s header deadline bounds a hostile connection to ≤105 s of resource hold, vs Blazeio's ≤5 h and Robyn's "until the OS TCP stack decides."

**The KNOWN gap restated:** no framework enforces header **and** body **and** total **and** idle. FastAPI relies entirely on the ASGI server; Robyn disabled the one it had (`client_request_timeout(0)`); Blazeio has none and advertises a 5 h keep-alive. Lancelot is the only one with all four, all in Rust, all per-route-overridable, `0`=disable documented for legitimate long streams.

---

## §16. Per-Failure-Class Reproduction Scenarios + Calibration Plan (DESIGN)

A release-gate checklist is only enforceable if each item has a *reproduction*. This is the test matrix — the exact adversarial input per gate, the expected stable behavior, and how to calibrate Lancelot against the originals' real APIs (per CLAUDE.md: every teardown ends with a calibration plan).

| Gate | Reproduction (the adversarial scenario) | FAIL signature (observed in the corpus) | Lancelot PASS criterion |
|---|---|---|---|
| §1 | `SIGTERM` mid-SSE-stream + mid-payment-POST under load | Robyn: severed sockets, 0 bytes more (`processpool.py:55`) | in-flight finish ≤30 s; SSE ends with clean event; 0 severed |
| §2 | POST an auth header that makes `from_py_dict` see a non-str (`robyn/02:463`); panic a request handler | Robyn: worker process gone, never returns, fleet -1 forever | 422 (bad dict) / 500 (panic) + worker respawned ≤1 s |
| §3 | dribble request headers 1 byte/s × 5000 conns | Blazeio: 5000 × (100 KiB + task + Future) held to keep-alive (`blazeio/01 §8:751`); Robyn: held forever (`client_request_timeout(0)`) | each dropped at 30 s; per-conn cost ≈ parser state |
| §4 | 10 000 concurrent sync-handler requests (blocking SDK) | FastAPI: 41st+ hangs silently no signal (`fastapi/01 §5.2`); Robyn: queue grows unbounded | `503 Retry-After` from Rust at queue cap; `/metrics` shows depth |
| §5 | 5000 concurrent SSE streams, 1000 tokens each, slow clients | Robyn: per-token GIL+spawn_blocking storm (`robyn/01 §7.2`); Blazeio: GIL-ceiling | 1 GIL/flush; RAM bounded by ring not completion length |
| §6 | client disconnects 100 ms into a 60 s LLM stream | none abort upstream (FastAPI checkpoint-timing; Robyn no pump cancel; Blazeio `reTask` loses it) | upstream request aborted ≤1 tokio poll; billing stops |
| §7 | tenant-A request writes `ContextVar`, tenant-B reuses the pool thread | Robyn pre-#1380: B sees A's auth (`robyn/01 §4.1`) | B sees fresh context always; (ft) no data race |
| §8 | 10 MB upload to a handler that reads nothing | Robyn: 20 MB resident before byte 0 (`robyn/02 §3.1:254`) | rejected at 16 MiB cap in Rust, or streamed; ~1–3 allocs |
| §9 | SSE handler with a `yield`-dep DB session; client stays 10 min then leaves | FastAPI (if ordering broken): session closed mid-stream or leaked | session open whole stream, closed after last byte, on cancel too |
| §10 | malformed JSON body; truncated body; provider returns 503 | Blazeio: `raise`-per-probe slow (`blazeio/02:455`); Robyn: blanket 500 / panic | 422 `json_invalid` / 400 framing / 502 + breaker; POST not retried |
| §11 | add `@app.middleware("http")`; `background_tasks.add_task(30s_job)` | FastAPI: task-group+2 streams/req; 200 then connection held 30 s (`fastapi/01 §8.4,§10`) | no `call_next` bridge exists; BG task detached, conn freed immediately |
| §12 | saturate Python pool, hit `/livez`; port conflict in a container | none: LB death spiral; Robyn: `input()` hangs pod forever (`robyn/03:700`) | `/livez` 200 (const-tier, no Python); `/readyz` 503; port conflict exit≠0 |

### 16.1 Calibration against the originals (the parity test)

Per CLAUDE.md "calibration": run the same adversarial scenario against the real FastAPI/Robyn/Blazeio servers (their public APIs / a local `docker-compose`) and against Lancelot, and assert Lancelot is **strictly better or equal** on every gate, never worse:

1. **§3 slow-loris harness:** a Python client opening N connections, sending one header byte per `T` seconds. Measure: connections still held at `t=60 s`, RSS delta. Robyn/Blazeio hold all N; Lancelot must hold 0 past 30 s.
2. **§4 load-shed harness:** ramp concurrent blocking-handler requests; measure the latency-vs-concurrency curve. FastAPI shows the cliff at 40 (`CapacityLimiter`); Lancelot must show a clean `503` plateau, not an unbounded latency ramp.
3. **§5 stream-memory harness:** N concurrent SSE streams to throttled clients; measure server RSS vs N and vs completion length. Robyn RSS grows with token rate × N; Lancelot RSS must be flat in completion length (ring-bounded).
4. **§6 upstream-abort harness:** a mock LLM provider that counts tokens *generated*; disconnect the client mid-stream; assert the provider stops being read within one poll for Lancelot, and (for contrast) keeps being read for FastAPI/Robyn.
5. **§1 drain harness:** `SIGTERM` during a mix of in-flight requests; assert Lancelot's success rate for in-flight = 100% (within `graceful_timeout`) vs Robyn's 0%.

The quality gap to close is *zero* on stability (Lancelot must dominate every gate) — unlike latency where parity-not-identity is acceptable, a stability regression vs Robyn/FastAPI fails the "stable like FastAPI/Robyn but faster" thesis outright.

---

## §17. Cross-Cutting: How the 12 Gates Compose (the failure-interaction map)

Stability bugs compound — the dangerous production incidents are *interactions*, not single failures. The corpus shows three concrete chains; Lancelot's guards must hold *jointly*, not just individually.

**Chain A — the "deploy during incident" cascade (Robyn).** Sync handler pins a worker (§4/§11, `robyn/02 §5b`) → latency climbs → ops deploys a fix → `SIGTERM` → `process.kill()` drops all in-flight (§1, `processpool.py:55`) → a panic in the marshalling of one of the killed-then-retried requests (§2, `robyn/02:463`) → `panic="abort"` → worker gone, no respawn (§2, `robyn/03:257`) → fleet -1 → remaining workers more saturated → cascade. **Lancelot breaks it at 4 points:** sync offloaded+shed (§4), drain not kill (§1), no panic on input (§2/§14), respawn (§2). Any one guard alone is insufficient; the catalog requires all four.

**Chain B — the "slow client OOM" cascade (Blazeio/Robyn).** Slow client (§3) → no header deadline so the connection is held (§3/§15) → if it's a stream, Robyn buffers per-token with no backpressure (§5, `robyn/01 §7.2`) or Blazeio holds 100 KiB + GIL-churns (§8/§5) → memory climbs → more slow clients → OOM-kill → no respawn (§2) → fewer workers → faster OOM. **Lancelot breaks it:** header deadline drops the slow client (§3), end-to-end backpressure bounds stream RAM (§5), lazy proxy + body cap bound request RAM (§8), respawn if OOM-killed anyway (§2).

**Chain C — the "tenant leak under load" cascade (Robyn pre-#1380).** High load → sync handlers reuse pool threads (§4) → a `ContextVar` write leaks across requests on a reused thread (§7, `robyn/01 §4.1`) → tenant A's auth serves tenant B → under free-threading this becomes a *concurrent data race* not just a reuse bug (§7/§14, `design/01 §3.3a`). **Lancelot breaks it:** per-request `contextvars.Context` from day one (§7) — and ft-clean design so the race never materializes (§14).

**The composition rule:** the 12 gates are not independent checkboxes — they are a *joint invariant*. A release that ships 11 of 12 is not "92% stable"; it is exposed to whichever chain the missing gate completes. This is why the checklist is a hard merge gate (all-green), not a scorecard.

---

## §18. The WebSocket Stability Surface (KNOWN — Robyn's WS layer is a distinct failure class)

The brief's failure dimensions (concurrency races, resource leaks, backpressure failure, disconnect, unbounded buffers) all recur on the WebSocket path with *different mechanisms* than the HTTP path, and Robyn's WS implementation (the only one of the three with first-class WS) fails several of them independently. For an AI product this matters: agent/chat backends are WS-heavy (`/ws/conversations/:id`), so a WS stability bug is an AI-product stability bug.

### 18.1 The defects (KNOWN — Robyn `websockets/`)

1. **Unbounded inbound mpsc — a fast client OOMs the channel.** Each connection creates `tokio::sync::mpsc::unbounded_channel::<Option<String>>()` (`websockets/mod.rs:494`, `robyn/02 §7.2`, KNOWN, verbatim): *"`unbounded` = no backpressure (a fast client can OOM the channel)."* A client that floods frames faster than the Python handler `await channel.receive()`s them grows the channel without bound — the §4 unbounded-queue bug, on the WS path, *per connection* (so it scales with connection count, worse than the single sync queue).
2. **Binary frames coerced to UTF-8 — data corruption + an exception path.** `StreamHandler::handle`: `Binary` → UTF-8 try → send as text else *echo back* (`websockets/mod.rs:494`, `robyn/02 §7.2`, KNOWN): *"binary frames are coerced to UTF-8 strings (no raw-bytes path to Python)."* An agent backend sending protobuf/audio frames either gets silent corruption or an unexpected echo — and the failed-UTF-8 branch is an error path on hot binary traffic.
3. **`ws.clone()` per handler invocation — heavy per-message, includes a GIL acquisition.** `execute_ws_function` does `handler.call1((ws.clone(),))` and *"`ws.clone()` on every handler call clones the whole `WebSocketConnector` (incl. `task_locals.clone_ref` under GIL, `:111-124`) — heavy per-invocation"* (`websockets/mod.rs:497`, `robyn/02 §7.3`, KNOWN). Every inbound message pays a full connector deep-clone + a GIL slice — under a chatty WS client this is a per-message GIL-contention multiplier (the §7 GIL-on-hot-path bug, WS variant).
4. **O(n) broadcast with a `String` clone per client.** `WebSocketRegistry::SendMessageToAll` *"iterates all clients, `do_send` to each — O(n) per broadcast, clones the message `String` per client"* (`registry.rs:91-99`, `robyn/02 §7.4`, KNOWN). A chat room broadcast is O(clients) allocations; a large room amplifies one inbound message into N string copies.
5. **Module-global unbounded task dict — a hung handler leaks forever** (cross-ref §9): `_connection_tasks` is module-global, unbounded, never reaped for a handler that neither returns nor disconnects (`ws.py:89`, `robyn/03:432`, KNOWN). The `close_handler` join is 5.0 s (`ws.py:242`, KNOWN) but only fires on a *clean* close.
6. **WS routing is exact-string only — no path params.** `web_socket_router.rs:46-48` is `HashMap<String, HashMap<String, FunctionInfo>>` with stringly-typed `{connect,close,message}` keys, no `matchit` (`robyn/02 §2.5:166`, KNOWN): *"agent chat backends frequently want `/ws/conversations/:id`."* Not a crash, but it forces every agent backend into a query-string or in-band-message workaround — an instability source in *user* code the framework induces.

### 18.2 The guard Lancelot ships (DESIGN)

1. **Bounded WS inbound channel + explicit overflow policy.** Replace `unbounded_channel` with a bounded `tokio::sync::mpsc` (default ~256 frames, configurable); on overflow apply a configurable policy: backpressure-the-reader (stop reading the socket — natural flow control, the §5 model on WS), or close with `1009 Message Too Big`, or drop-oldest. Never unbounded (`robyn/02 §7.5`, KNOWN-as-recommendation). Where: **Rust**.
2. **`enum WsFrame { Text(String), Binary(Bytes) }` across the boundary** — a raw-bytes path to Python alongside text, no UTF-8 coercion, no echo-on-fail (`robyn/02 §7.5`, KNOWN-as-recommendation — audio/agents need bytes). Where: **Rust**.
3. **Pass the connector by `Arc`/handle, never `ws.clone()`** — eliminate the per-message deep-clone + GIL slice (`robyn/02 §7.5`, KNOWN-as-recommendation). Where: **Rust**.
4. **Broadcast with `Arc<str>`/`Arc<[u8]>` shared payload** — one allocation, N refcount bumps, not N `String` clones (`robyn/02 §7.5`, KNOWN-as-recommendation). Where: **Rust**.
5. **Per-app WS task registry + idle reaper** (cross-ref §9): bounded, with an idle deadline that cancels a handler that has neither returned nor seen a frame; `close_handler` join 5.0 s (Robyn's, configurable). Where: **Rust**.
6. **`matchit` WS routing with path params** — `/ws/chat/:room`, an `enum WsPhase { Connect, Close, Message }` instead of `&str` keys (`robyn/02 §2.5`, `robyn/02 §7.5`, KNOWN-as-recommendation). Where: **Rust**.
7. **Async-generator → socket streaming** — support a handler returning an async generator to stream tokens *into* the WS (token streaming over WS for chat, `robyn/02 §7.5`, KNOWN-as-recommendation), reusing the §5 coalesced backpressured pump. Where: **Rust** pump + **Python** async-gen.

**Constants:** WS inbound channel bound ≈256 (configurable); overflow policy = backpressure (default) | close-1009 | drop-oldest; WS handler idle reap ≈300 s; `close_handler` join 5.0 s.

**Why this is the stable one:** Robyn's WS path independently reproduces the unbounded-queue (§4), GIL-on-hot-path (§7), and task-leak (§9) bugs — and an AI chat product lives on WS, so these are not edge cases. Lancelot's WS path inherits every HTTP-path guard (bounded, backpressured, `Arc`-shared, reaped, `matchit`-routed) so the WS surface is as stable as the HTTP surface, not a separate weak flank.

---

## §19. Malformed Input / Body-Framing / Partial-Read Correctness (KNOWN — the parser failure detail)

The brief names "JSON malformed, body truncated" explicitly. §10 covered the error *mapping*; this section is the body-*framing* correctness detail — the parser-level failures that produce wrong bytes, mis-attributed pipelined requests, or exception floods, which §10's mapping cannot fix because the bytes are already wrong.

### 19.1 The defects (KNOWN)

1. **Blazeio — `handle_raw` does not split the final chunk at `content_length`: pipelined-request mis-attribution.** *"Over-read is not handled here — if a chunk overshoots `content_length`, the whole chunk is yielded (the extra bytes belong to the next pipelined request and would be mis-attributed)"* (`Modules/request.py:294-305`, `blazeio/01 §5.1:506`, KNOWN gap, verbatim): *"`handle_raw` does not split the final chunk at `content_length`; contrast `Rutils.read_exactly` at `:101-119` which does clamp via `chunk[:rem]`. So streaming body reads don't pipeline-correct."* On a keep-alive connection with pipelined requests, request N's trailing bytes get fed to request N+1's handler — a silent data-corruption bug, not an exception.
2. **Blazeio — chunk extensions break the chunked decoder with a `ValueError` flood.** `int(buff[:idx], 16)` without stripping `;ext=val` → `int("1a;foo",16)` raises `ValueError` (`Modules/request.py:241-275`, `blazeio/01 §5.3:562`, KNOWN gap): *"chunk extensions break this decoder."* A client sending RFC-legal chunk extensions causes a parse exception *per chunked request* — an exception flood on a legal input class (compounding Blazeio's exception-as-control-flow cost, §10).
3. **Blazeio — the chunked decoder's O(C·body) recopy under large chunked uploads.** *"every state transition does `memarray(buff[...])` — a fresh `bytearray` copy of the remaining buffer … O(total_body_size) extra copying in the worst case"* (`Modules/request.py:241-275`, `blazeio/01 §5.3:565`, KNOWN). A large chunked upload is a CPU+allocation amplification (the third Python quadratic, after the header re-scan §3 and header tail-recopy §3).
4. **Blazeio — no HTTP version validation.** *"no version validation that make it both slower and less safe than a Rust state-machine parser under fragmented/adversarial input"* (`blazeio/01 §4.6:483`, KNOWN) — a malformed status/version line is not rejected early.
5. **Blazeio — `bytes(memoryview[:nbytes])` and the body copied per chunk; the receive buffer reuse means a retained slice is unsafe** (`blazeio/01 §1.3:92`, KNOWN) — correctness constraint that forces the copy, not a bug, but the reason a pure-Python parser cannot be both zero-copy and correct here.
6. **Robyn — body fully buffered with no framing-error distinction.** The whole body is `Vec<u8>` before the handler runs (`request.rs:158-163`, `robyn/02 §3.1`, KNOWN); a truncated body just yields a short `Vec` — no explicit "incomplete body vs complete body" signal to the handler (the handler must re-derive it from `Content-Length` vs `len`).
7. **FastAPI — body parse error mapping is correct but content-type sniffing is per-request `email.message.Message()`.** Malformed JSON → `422 json_invalid` `loc:("body",e.pos)` is correct (`routing.py:427-441`, `fastapi/01 §4.2`, KNOWN); a non-JSON error → generic `400` (`routing.py:445-449`, KNOWN). But the Content-Type split instantiates a full `email.message.Message()` *per request* (`routing.py:417-419`, `fastapi/01 §4.2`, KNOWN) — under a malformed-Content-Type flood that is an allocation amplification.

### 19.2 The guard Lancelot ships (DESIGN)

1. **Single-pass resumable Rust parser that splits exactly at `content_length`** — `hyper`'s body reader yields exactly `Content-Length` bytes and leaves the remainder for the next pipelined request (fixes Blazeio `handle_raw` mis-attribution, `blazeio/01 §5.1:506`, KNOWN). Where: **Rust**.
2. **Chunked decoding handles extensions** — `hyper` parses `;ext=val` correctly (fixes Blazeio's `ValueError` flood, `blazeio/01 §5.3:562`, KNOWN) and decodes framing **in place with index advancement, zero copies** (fixes the O(C·body) recopy, `blazeio/01 §5.3:565`, KNOWN). Where: **Rust**.
3. **HTTP version/status-line validation at parse** — reject a malformed version line early with a 400, not deep in the handler (fixes Blazeio's no-version-validation, `blazeio/01 §4.6:483`, KNOWN). Where: **Rust**.
4. **Explicit truncated-body signal** — a body shorter than `Content-Length` when the connection closes → an explicit framing error → **400** (not a silently-short `Vec` the handler must re-check, fixes Robyn `request.rs:158-163`, `blazeio/03:104`). The lazy request proxy (§8) exposes a "body complete?" property derived in Rust. Where: **Rust**.
5. **Malformed JSON → `422 json_invalid` `loc:("body",pos)`** byte-for-byte matching FastAPI (`routing.py:427-441`, KNOWN — clients/tests depend on the exact shape, §10); single-pass `validate_json(bytes)` (no `json.loads`→`validate_python` double-parse, SYNTHESIS steal #7, KNOWN). Where: **Rust**/pydantic-core.
6. **Content-Type parsed in Rust** — split on `;`, lowercase, suffix `+json` check; zero Python allocation, no `email.message.Message()` per request (fixes FastAPI, `fastapi/01 §4.2`, KNOWN); cache the parsed `(maintype,subtype)` on the connection. Where: **Rust**.
7. **Zero-copy body with refcounted slices** — Rust `Bytes::split_to(n)` hands the consumer an owned refcounted slice with no copy and reuses the tail in place (the thing Blazeio structurally cannot do because retaining a `memoryview` over a reused `bytearray` is unsafe, `blazeio/01 §1.3:92`, KNOWN). Where: **Rust**.

**Constants:** body framing error → 400; malformed JSON → 422 `json_invalid` (exact FastAPI shape); over-read split exactly at `Content-Length`; chunk extensions parsed (not rejected).

**Why this is the stable one:** Blazeio's parser silently mis-attributes pipelined bytes (a data-integrity bug, the worst class — no exception, wrong data), floods exceptions on legal chunk extensions, and quadratically recopies large chunked bodies; Robyn gives no truncation signal. Lancelot's Rust parser is single-pass, splits exactly at `Content-Length`, handles extensions, validates the version line, and signals truncation explicitly — the body bytes the handler sees are *correct*, which §10's error mapping cannot retroactively guarantee.

---

## Appendix A — Cross-Cutting Stability Constants (the release-gate numbers)

Every constant a Lancelot replica must ship with, with source and whether it matches or consciously diverges from the corpus.

| Constant | Value | Lives | Source / fixes | Match or diverge |
|---|---|---|---|---|
| `graceful_timeout` | 30 s | Rust supervisor | fixes Robyn `processpool.py:55` | DESIGN (Robyn has none) |
| `max_restarts` / `restart_window` | 5 / 30 s | Rust supervisor | fixes Robyn no-supervisor | DESIGN |
| restart backoff | 200 ms ×2 exp | Rust supervisor | `design/01 §5.2.1` | DESIGN |
| `header_read_timeout` | 30 s (0=disable) | Rust (`hyper`) | `robyn/01:397` KNOWN-as-rec; fixes Robyn `client_request_timeout(0)` | DESIGN (diverge from Robyn's 0) |
| `body_read_timeout` | 60 s (per-route 0) | Rust (tower) | `design/01 §5.3` | DESIGN |
| `total_request_timeout` | 120 s (per-route 0) | Rust (tower) | `design/01 §5.5` | DESIGN |
| `max_concurrent_connections` | configurable (~1024) | Rust acceptor | Granian model, KNOWN | DESIGN |
| `sync_queue_max` | 10 000 | Rust (bounded `crossbeam`) | fixes Robyn `unbounded()` `blocking.rs:545` | DESIGN (diverge: bounded) |
| `503 Retry-After` | 1 s (configurable) | Rust | `fastapi/01 §5:409` KNOWN-as-rec | DESIGN |
| SPSC ring size | 1 | Rust | `design/01 §5.4`, SYNTHESIS §4.5 | DESIGN |
| stream coalesce flush | 2 KiB OR 25 ms | Rust codec | SYNTHESIS §4.5; fixes Blazeio 30 720 size-only | DESIGN (diverge: time+size) |
| SSE keepalive interval | 15.0 s | Rust timer | FastAPI `sse.py:222` KNOWN | **Match** |
| SSE keepalive payload | `b": ping\n\n"` | Rust | FastAPI `sse.py:218` KNOWN | **Match** |
| SSE terminator | `\n\n` | Rust | FastAPI `sse.py:212-214` KNOWN | **Match** |
| SSE headers | `Cache-Control: no-cache`, `X-Accel-Buffering: no` | Rust | FastAPI `routing.py:616-618` + Robyn `response.rs:107-117` KNOWN | **Match** |
| `RequestBodyLimit` | 16 MiB (per-route override) | Rust (tower) | fixes Robyn eager 20 MB / `ROBYN_MAX_PAYLOAD_SIZE` 1 MB global | DESIGN |
| per-request alloc target | ~1–3 | Rust lazy proxy | `SYNTHESIS:136`; vs Robyn 30–66, FastAPI 10–15 | DESIGN |
| Python floor | 3.11 (hard error) | Python | `robyn/01 §4.3` KNOWN-as-rec (drop 3.10 degraded) | DESIGN (diverge: hard) |
| `cancel_on_disconnect` | True | Rust→Python | Blazeio default `server_protocol.py:50` KNOWN | **Match** |
| POST auto-retries | 0 (connect-only pre-first-byte) | Python/Rust client | Blazeio `NON_BODIED_HTTP_METHODS` `blazeio/03 §4.5` KNOWN | **Match** (Blazeio's rule) |
| TLS verify (outbound LLM) | True | Python/Rust client | fixes Blazeio `CERT_NONE` `blazeio/03 §4.3` KNOWN | DESIGN (diverge: secure) |
| worker count | `clamp(cgroup_cpus, 2, 4)` | Rust bootstrap | fixes Robyn `os.cpu_count()` `argument_parser.py:106` | DESIGN |
| validation error | `422 {"detail":[...]}` | Rust/pydantic | FastAPI `exception_handlers.py:20-26` KNOWN | **Match** (clients depend) |
| WS `close_handler` join | 5.0 s | Rust | Robyn `ws.py:242` KNOWN | **Match** (configurable) |
| WS inbound channel | bounded (~256) | Rust | fixes Robyn `unbounded_channel` `robyn/02 §7.2` | DESIGN (diverge: bounded) |
| clippy gate | `deny(await_holding_lock, unwrap_used, expect_used, panic, indexing_slicing)` on request-path | Rust CI | `design/01 §2.2,§5.2`, `robyn/01 §10` | DESIGN |
| port conflict | exit ≠ 0 (never `input()`) | bootstrap | fixes Robyn `__init__.py:712` `robyn/03:700` | DESIGN |
| `/livez` `/readyz` `/startupz` | const-tier, zero GIL | Rust | `design/01 §5.7` item 12; none of the three split | DESIGN |

## Appendix B — Failure-Class → Framework Verification Matrix

Which framework gets each failure class wrong (✗ = broken, ◐ = partial/right-model-wrong-impl, ✓ = correct, — = N/A). Lancelot's column is "ships the §-guard."

| Failure class | FastAPI | Robyn | Blazeio | Lancelot |
|---|---|---|---|---|
| §1 Graceful drain | ◐ (uvicorn drains; BG-tasks block it `fastapi/01 §10`) | ✗ `process.kill()` `processpool.py:55` | ◐ cancels in-flight (`Taskmng`) | §1 supervisor + hyper drain |
| §2 Crash/panic respawn | ◐ (Py exc safe; no framework respawn) | ✗ `panic=abort` + no supervisor `robyn/03 §3.5` | ✗ single proc, masks error `blazeio/02:457` | §2 respawn + FFI seam + clippy |
| §3 Slow-loris / timeouts | ✗ no header/body/total deadline | ✗ `client_request_timeout(0)` `server.rs:280` | ✗ O(n·k) re-scan, no deadline `blazeio/01 §8` | §3 Rust resumable + 3 deadlines |
| §4 Backpressure / load-shed | ✗ `CapacityLimiter(40)` silent `fastapi/01 §5` | ✗ `unbounded()` + sync not wired `robyn/01 §5.6` | ◐ correct model, GIL-ceilinged | §4 bounded + 503 Retry-After |
| §5 Stream memory | ◐ correct arch, sync-gen catastrophic `fastapi/01 §5.3` | ✗ per-token GIL+spawn_blocking `robyn/01 §7.2` | ◐ correct gate, 30 KiB mis-tuned | §5 async-gen + coalesce + ring |
| §6 Disconnect→abort-upstream | ✗ checkpoint-timing, no upstream abort | ✗ no clean pump cancel `robyn/01 §7.2` | ◐ task cancel; `reTask` loses it | §6 Rust watch → structured cancel |
| §7 ContextVar / GIL race | ✓ (anyio handles) | ✗ shipped #1380 leak `robyn/01 §4` | ◐ `Taskscope` leak `blazeio/03:283` | §7 per-req ctx + clippy invariant |
| §8 Bounded memory | ◐ 12–16 allocs + 2 stacks always | ✗ 30–66 eager + body ×3 `robyn/02 §3` | ✗ fixed 100 KiB/conn `blazeio/01 §1.2` | §8 lazy proxy + 16 MiB cap |
| §9 Yield-dep/task leak | ◐ correct-but-fragile ordering `fastapi/02 §6.3` | ✗ module-global unbounded WS dict `robyn/03:432` | ◐ `reTask` invisible to watchdog | §9 Rust drop-schedule + reaper |
| §10 Error recovery | ◐ double-validate output `fastapi/01 §6.1` | ✗ blanket 500 + panic-on-dict `robyn/02:463` | ✗ exception-as-primary `blazeio/02 §5` | §10 one funnel + typed + 502 |
| §11 Anti-pattern dispatch | ✗ `BaseHTTPMiddleware` + seq BG-tasks | ◐ sync not offloaded `robyn/02 §5b` | ✓ pure-async | §11 no call_next + detached BG |
| §12 Liveness≠readiness/boot | ✗ no split | ✗ `os.cpu_count()` + `input()` `robyn/03 §3.5` | ✗ no split | §12 const-tier split + cgroup |

**Reading:** every column has multiple ✗. No single framework is "the stable one" — FastAPI is safest on crashes (zero-cost exceptions) but worst on dispatch anti-patterns and timeouts; Robyn has the best core model but the worst ops story (no drain, no respawn, panics on input); Blazeio has the most correct backpressure *model* but a pure-Python ceiling and exception-as-control-flow. Lancelot's stability claim is *only* credible if every §-guard ships — the catalog is the gate.

## Appendix C — The Single Decisive Sentence

> Lancelot is "stable like FastAPI/Robyn but faster" **only if** it ships all twelve §-guards: it is the one framework where a deploy drains instead of dropping (§1), a panic respawns instead of bleeding the fleet (§2), a slow-loris is dropped in Rust instead of holding a connection hostage (§3), overload returns `503 Retry-After` instead of hanging silently (§4), 10k concurrent token streams cost 1 GIL/flush and bounded RAM instead of a per-token storm (§5), a client disconnect aborts the upstream LLM instead of paying for unread tokens (§6), tenant context never leaks across requests (§7), the request object materializes nothing until touched (§8), no session/task/connection leaks across a long stream (§9), every error is a typed response not a masked exception or a process kill (§10), no tutorial-recommended path is a per-request task-group bomb (§11), and an orchestrator can tell "busy" from "dead" while a port conflict fails fast instead of hanging the pod forever (§12) — each fixing a *specifically cited* KNOWN defect in FastAPI, Robyn, or Blazeio, none of which gets more than a handful of these right.

*Compiled 2026-05-19 from the 9-file source-level RE corpus (`research/{fastapi,robyn,blazeio}/*`, 8,645 lines) + `research/design/01` (554 lines) + `SYNTHESIS.md` + `DESIGN-DECISIONS.md`. Every claim traces to a `path:line` or teardown `§ref`. `Lancelot/` is gitignored.*
