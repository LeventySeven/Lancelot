# M16 — streaming/transport resilience fixes (P1, P2, P8) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (1 impl subagent → 1 recheck subagent → final review). Subagents MUST use superpowers:test-driven-development. P1's native-path work uses the Rust skills (unsafe-checker if any unsafe, coding-guidelines, m07-concurrency) if it touches `src/*.rs`.

**Goal:** Fix two confirmed latent streaming bugs (P1 pre-200 status, P2 zombie-stream keepalive self-reset) and close the P8 streaming-relay disconnect-cancel gap — the highest-leverage, clearly-in-scope subset from the 2026-05-31 fresh corpus mine (`docs/plans/2026-05-31-fastapi-rust-absorb-mine.md`, items P1/P2/P8).

**Architecture:** Surgical changes to the streaming path (`lancelot/asgi.py` is the integration surface), `lancelot/stream.py`, `lancelot/app.py` (`@app.stream` kwargs), `lancelot/llm.py` (`pool.stream`/`pool.relay`), and possibly `src/service.rs`/`src/stream.rs` for the P1 native mirror. No new runtime deps (pydantic-only). Pure Python where possible — only P1 may touch Rust, and only if a clean GLOBAL eager-first-chunk is reachable (per-route config is not a native feature — see P1).

**Tech stack:** Python 3.13 + pyo3/hyper/tokio Rust core. Tests = pytest with **real-server subprocess + HTTP** for every streaming change (cardinal rule #2). Conformance = `make m1-conformance` (floor `passes=974`; currently 978).

---

## Shared context (give to the subagent)

**Tool affirmation (anti-hallucination):** You have FULL tool access (Read, Grep, Glob, Bash, Edit, Write). Verify every line number against live code before editing — anchors below were captured 2026-05-31 post-M15 and may drift. You ARE implementing.

**Positioning lock:** Lancelot owns the HTTP byte path (transport/serialization/streaming/resilience); AI logic stays user-side. All three tasks are transport-lifecycle/resilience — in lane.

**Cardinal rules that bind this work:**
1. No bullshit / no overkill.
2. **Validate under REAL load** — every streaming/transport change gets a real-server (subprocess `serve()`/uvicorn + raw-socket/HTTP) test exercising the exact failure path (the handler that raises *before* its first yield; the *ping-only* stream; the *disconnecting* relay client). Green `TestClient` ≠ works under a real socket.
3. Runtime dep = pydantic only.
4. Vendored FastAPI tests in `conformance/vendor/fastapi/` are SACRED — never edit.
5. Default behavior must stay byte-identical when the new opt-in kwargs are off (conformance floor 974 + the M15 SSE tests must stay green).

**Gate (after each task; all green before "done"):**
```bash
.venv/bin/python -m pytest tests/ -q                                    # full suite; baseline 511 passed / 3 skipped
make m1-conformance                                                      # floor passes=974 (currently 978)
# if src/*.rs touched (only P1 might):
PATH="$HOME/.cargo/bin:$PATH" .venv/bin/maturin develop --release        # rebuild wheel BEFORE pytest
$HOME/.cargo/bin/cargo clippy --release --all-targets -- -D warnings
$HOME/.cargo/bin/cargo test --release --lib
```
Branch `m16-streaming-resilience-fixes` (already created; NOT main). One commit per task; messages end with the `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` trailer.

**Full evidence for each task:** `docs/plans/2026-05-31-fastapi-rust-absorb-mine.md` §3 (P1/P2/P8 rows) + §5 (the load-gate). Citation corrections from the critic (apply): the BentoML eager-first-chunk reference is `bentoml/sdk/io_models.py` (~227) and BentoML only LOGS the post-first-chunk error — **Letta `StreamingResponseWithStatusCode` is the true P1 reference**; treat `N/24` recurrence counts as directional.

---

## Task P1: pre-200 streaming status (`defer_status` / eager-first-chunk)

**Files:** `lancelot/asgi.py` (the stream branch: gen built ~929-935, `status:200` sent before first `__anext__` ~1030), `lancelot/app.py` (`@app.stream` kwargs ~474), `lancelot/stream.py` (if framing helper needed). Possibly `src/service.rs`/`src/stream.rs` (native mirror — see the nuance). Test `tests/test_m16_defer_status.py` (real-server).

**Confirmed bug (verify live):** the ASGI seam builds the async-gen, then sends `http.response.start` with `status:200` + headers BEFORE pulling the first item. A handler that **raises before its first `yield`** therefore commits a `200` and can only emit an error *frame* — a half-open 200. Native (`src/service.rs` ~265-272) sets `status(OK)` + headers with a lazy `StreamBody` polled only after headers ship — same shape.

**Change:** add `@app.stream(defer_status=True)` (default **False** → byte-identical to today). When on: the seam pulls the FIRST item inside a `try` BEFORE `http.response.start`. If the handler raises `HTTPException` (or a `StreamError(status=...)`) before yielding → send a real 4xx/5xx + JSON body (reuse the shipped `ProblemDetail`, M15 `lancelot/exceptions.py`). Otherwise send `200` + the stashed first chunk, then continue the stream normally. Reference impl: Letta `StreamingResponseWithStatusCode` (peeks `__anext__`, unpacks `(content, status)`).

**Native-path nuance (make the call + report it):** per-route config is NOT a native feature (`src/stream.rs` forbids per-route streaming policy; `app.run()` forwards only routes). So `defer_status` is naturally an **ASGI-seam (uvicorn) feature**. For the native path, EITHER (a) implement a clean GLOBAL eager-first-chunk in `build_result` (pre-pull `chunk[0]` before sending headers — applies to all native streams, no per-route config needed) IF it's clean and doesn't regress the R3 pump, OR (b) document that `defer_status` is ASGI-seam-only (consistent with the M15-documented native lifespan/DI limitations) and keep native unchanged. Pick the sound option; do NOT force a risky Rust change. Report which you chose + why.

**Tests (real-server — load gate):**
- `test_defer_status_pre_yield_raise_sends_real_4xx`: a `@app.stream(defer_status=True)` handler that raises `HTTPException(409)` before its first yield → client receives a real `409` + JSON body (NOT a half-open 200 + error frame). Run on the ASGI/uvicorn subprocess; if native eager-pull was implemented, also assert on a native subprocess.
- `test_defer_status_off_is_byte_identical`: with `defer_status=False` (default), a normal stream is byte-identical to pre-M16 (regression; the M15 SSE tests must also stay green).
- `test_defer_status_happy_path`: a `defer_status=True` stream that yields normally still emits 200 + all chunks in order.

**Acceptance:** real-server pre-yield-raise test passes; default path byte-identical; gate green; the native decision documented. ~6-9h.

---

## Task P2: stuck-stream silence-abort + keepalive self-reset fix

**Files:** `lancelot/asgi.py` (the keepalive machinery: `_last_send` ~969, `_send_serialized` ~973-981, `_keepalive_loop` ~1008-1027), `lancelot/app.py` (`@app.stream` kwargs). Test `tests/test_m16_stream_silence.py` (real-server). **Python-only.**

**Confirmed bug (verify live):** `_send_serialized` bumps `_last_send` on EVERY send including its own keepalive ping (asgi.py:978/981), and `_keepalive_loop` (:1015) only checks `idle = monotonic - _last_send`. So a generator that has gone silent but whose keepalive pings keep firing **resets its own idle timer forever** → a zombie/stuck stream is never detected or closed. There is no max-silence cap. (The shipped `idle_guarded_chunks`, `llm.py:397`, is UPSTREAM-only; per-route `timeout=` explicitly does not wrap async-gens.)

**Change:** track **last-DATA send** separately from **last-PING send** (this alone fixes the self-reset — the keepalive ping must NOT reset the data-silence timer). Add `@app.stream(max_stream_silence=300)` (default **None** = off → no behavior change). When only keepalive pings (no real data) have flowed for `max_stream_silence` seconds, cancel the generator (so its `finally`/`aclose` runs — teardown preserved) and close the connection. Symmetric with the upstream `idle_guarded_chunks` concept.

**Tests (real-server — load gate):**
- `test_ping_only_stream_aborts_at_max_silence`: a `@app.stream(sse_keepalive=1, max_stream_silence=3)` generator that emits ONLY keepalive-triggering silence (no data) → assert the connection drops at ~`max_stream_silence` AND the generator's `finally`/`aclose` ran (record via a sidecar). Today it would never drop.
- `test_keepalive_does_not_reset_data_silence`: data flows, then goes silent while pings continue → silence is measured from the last DATA, not the last ping.
- `test_max_silence_off_default`: `max_stream_silence=None` (default) → a long quiet-but-pinging stream stays open (byte-identical to today); the M15 keepalive tests stay green.

**Acceptance:** real-server ping-only-abort test passes (+ gen teardown ran); default unchanged; gate green. ~3-5h.

---

## Task P8: `pool.stream` disconnect-cancel + `pool.relay` upstream→client SSE relay

**Files:** `lancelot/llm.py` (`pool.stream` ~385 + the base at ~663; reuse `_await_cancellable` ~353 and the `request=`/`_cancel_event` race that `request()` ~243-279 already uses; add `pool.relay`). Test `tests/test_m16_relay_cancel.py` (real-server). **Pure Python — do NOT build a Rust pump variant (Tier-D: per-chunk path, upstream-rate-limited).**

**Confirmed gap (verify live):** `pool.stream(self, method, url, **kw)` (llm.py:385) takes only `**kw` — **no `request=` param, no `_await_cancellable` wrap** — unlike `pool.request(..., request=None)` (:243-244) which wraps the call in `_await_cancellable` (:279) so a client disconnect aborts it. So a client disconnecting mid-stream does NOT abort the upstream stream → the user keeps paying for tokens nobody is watching (the exact thing M15's C2 unary `cancel_on_disconnect` stops, but on the relay path). There is no `relay`/`reverse_proxy` helper.

**Change:** (1) add `request=` to `pool.stream(...)` (both the impl ~385 and the base ~663) and wire the same `_cancel_event` race the unary `request()` path uses, so a client disconnect cancels the upstream stream. (2) Add `pool.relay(method, url, *, request=req)` — an async-context-manager that opens the upstream stream, forwards chunks via the shipped `idle_guarded_chunks`, cancels on client disconnect, and `aclose()`s the upstream in `finally`. Usage: `async with pool.relay("POST", url, request=req) as upstream: return StreamingResponse(upstream)`. Reference: BentoML `_parse_stream_response` (~`http_client.py:808-814`, `aiter_bytes` + `finally: aclose()`).

**Tests (real-server — load gate):**
- `test_pool_stream_cancels_on_client_disconnect`: a handler relaying an upstream stream (a fake slow upstream that records if it ran to completion); a raw-socket client disconnects mid-relay → assert the upstream stream was CANCELLED (did not complete) and `aclose()` ran. Mirror the M15 unary-cancel real-server test shape.
- `test_relay_forwards_and_closes`: `pool.relay` forwards all chunks to the client on the happy path and `aclose()`s the upstream at the end.
- `test_pool_stream_request_none_unchanged`: `pool.stream(...)` with no `request=` behaves exactly as today (regression).

**Acceptance:** real-server disconnect-cancel test passes (upstream actually cancelled); no-request path unchanged; gate green. ~3-5h.

---

## Execution

ONE batch (P1, P2, P8 — cohesive streaming path), 1 impl subagent + 1 recheck subagent (Opus 4.8), then a final whole-branch review, then the integration choice. Recheck covers: bugs/correctness, vulnerabilities, re-verify vs live source, patterns/anti-patterns, core goals/principles (positioning + Tier-D + no-bullshit + the "don't regress R3 / don't build a Rust per-chunk pump" constraints), plan alignment (default-off byte-identity; real-server tests that fail-without-fix), and (P1) whether the native-path decision is sound + documented.

## Self-review (vs the mine doc)
- P1 (mine §3 P1) → defer_status, ASGI seam + native decision. ✓
- P2 (mine §3 P2) → last-data vs last-ping split + max_stream_silence. ✓
- P8 (mine §3 P8) → pool.stream request= + pool.relay, pure Python, no Rust pump. ✓
- Out of scope (deferred to a later milestone / your call): P3 rate-limiter, P4 response_model skip-validate, P5 API-key gate, P6 SSE error-frame, P7 streamed cost/token, P9 Page[T]; R1 gzip (needs bench + Tier-D sign-off), R2 base64 (needs bench), R3 broker (positioning call). The native-lifespan follow-up (task #7) also remains open.
