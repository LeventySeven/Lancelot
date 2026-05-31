# M17 — panel-approved deferred wins (P7, P6, P3, P5, P4, P9-cap) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (impl subagent per batch → recheck subagent → final review). Subagents MUST use superpowers:test-driven-development. All-Python — no Rust this milestone.

**Goal:** Build the 6 candidates the 5-judge panel approved (`docs/plans/2026-05-31-deferred-candidates-panel.md`), with the panel's fences baked in. Held: R1 gzip (bench+sign-off). Skipped: R2 base64 (use pip pybase64), R3 broker (positioning), P9-envelope (use fastapi-pagination).

**Architecture:** Pure-Python, on the ASGI seam + `TransportPool` + two new opt-in middleware modules. No new runtime deps (pydantic-only; Redis/etc. behind lazy hooks). Every default-off path stays byte-identical (conformance floor 974, currently 978).

**Tech stack:** Python 3.13. Tests = pytest with **real-server subprocess + HTTP** for P7/P6/P3/P5 (the bug lives under a real socket); unit + conformance for P4/P9.

---

## Shared context (give to EVERY subagent)

**Tool affirmation:** FULL tool access (Read/Grep/Glob/Bash/Edit/Write). Verify every line number against live code first — anchors captured 2026-05-31 post-M16 may drift. You ARE implementing.

**Positioning + rules:** byte-path/operator-experience only; AI logic user-side. **No bullshit, no overkill.** Runtime dep = pydantic only (Redis/JWT/etc. NEVER imported eagerly; lazy hooks only). Default behavior byte-identical when new opt-ins are off (conformance 974 + the M15/M16 streaming tests stay green). Don't edit `conformance/vendor/fastapi/`. Don't regress R3 streaming (on-loop drain / same-thread push / bounded backpressure / ≤1-extra-chunk disconnect).

**Gate (no Rust this milestone):**
```bash
.venv/bin/python -m pytest tests/ -q          # full suite; baseline 521 passed / 3 skipped
make m1-conformance                            # floor passes=974 (currently 978)
```
Branch `m17-deferred-wins` (created; NOT main). One commit per task; trailer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

**Panel verdict + evidence:** `docs/plans/2026-05-31-deferred-candidates-panel.md` (the fences below come from it); `docs/plans/2026-05-31-fastapi-rust-absorb-mine.md` §3 (P-row file:line evidence). Real-server harness pattern: copy `tests/test_m16_*.py` / `tests/test_m15_unary_cancel.py` (subprocess uvicorn + raw socket) — do NOT invent a new one.

---

## BATCH A — streaming observability (P7 + P6) — real-server tests

### Task P7: streamed-response cost/token/TTFT accounting (the parity bug)
**Files:** `lancelot/llm.py` (`pool.stream` ~482, `relay`/`_relay_forward` ~539/579, the base `stream` ~832; reuse `_emit_upstream` ~424 + `extract_tokens`), `lancelot/upstream_metrics.py` (`extract_tokens` ~108 already handles OpenAI/Anthropic/Google). Test `tests/test_m17_stream_usage.py` (real-server).

**Confirmed gap (verify live):** `pool.request` calls `_emit_upstream` (`llm.py:394`) which records `upstream.*` metrics + accumulates `cost_usd` into the ContextVar (`:441-444`). **`pool.stream` (`:482`) and `_relay_forward` (`:579`) record NOTHING** — so cost/tokens/upstream-TTFT for a STREAMED LLM call are silently 0 (the `X-Lancelot-Cost-USD` trailer + `upstream.*` metrics read 0). r2r re-runs tiktoken to recover this lost number.

**Change:** add **opt-in** `track_usage=True` to `pool.stream`/`pool.relay`. When set: as chunks flow, retain the LAST SSE data frame; on stream end, parse it and run the existing `extract_tokens` (3 provider shapes — NO tiktoken, NO new dep), compute cost via the same rate path `_emit_upstream` uses, record the `upstream.*` token/cost metrics, and accumulate into the `cost_usd` ContextVar (so the shipped `X-Lancelot-Cost-USD` trailer covers streamed calls too). Also record upstream-TTFT (first chunk) for the streamed path. Default `track_usage=False` → no frame retention/scan (zero overhead, byte-identical). Provider-reported usage only — if the terminal frame has no usage, record nothing (don't guess).

**Tests (real-server):** `test_stream_track_usage_accumulates_cost` (a `pool.relay`/`pool.stream` over a fake upstream whose terminal SSE frame carries `usage`; assert `cost_usd` ContextVar accumulated + the `upstream.tokens.*` metric recorded), `test_stream_track_usage_off_zero_overhead` (default → no frame scan, behavior byte-identical), `test_stream_no_usage_frame_records_nothing` (terminal frame lacks usage → no spurious record). **Acceptance:** real-server cost-accumulation test passes; default unchanged; gate green. ~3-4h.

### Task P6: guaranteed SSE terminal sentinel + crash/cancel→ProblemDetail error-frame
**Files:** `lancelot/asgi.py` (the stream branch: hardcoded error `~1198`, the `finally` EOF, the CancelledError arm), `lancelot/stream.py` (`sse_adapter`), `lancelot/app.py` (`@app.stream` kwarg). Test `tests/test_m17_sse_finalize.py` (real-server). **Bundle with P7 (shared streaming path).**

**Confirmed state (verify live, post-M16):** the mid-stream error frame is a HARDCODED `{"error": "internal server error"}` (`asgi.py:1198-1199`), NOT the shipped `ProblemDetail`/`retry_taxonomy`. The `finally` sends a clean EOF with NO terminal `[DONE]`. The CancelledError arm emits NO error frame.

**Change:** add **opt-in** `@app.stream(sse=True, error_frame=True)` (default off → current behavior byte-identical). When on: (a) route the mid-stream error frame through `ProblemDetail` (shape it like the shipped RFC-9457 error, still scrubbed, still never leaking `str(exc)` by default); (b) emit the terminal `done_sentinel` (`[DONE]`) in the `finally` so a client can always tell a finished stream from a dropped one; (c) **the cancel-arm error frame fires ONLY for a genuine INTERNAL cancel (e.g. the M16 `max_stream_silence` silence-abort), NEVER for a client disconnect** (a disconnected client can't read it, and emitting on disconnect would be wrong). Lives in `sse_adapter` (single source of truth per `src/stream.rs`).

**Tests (real-server):** `test_error_frame_midstream_raise_is_problemdetail` (mid-stream raise → a ProblemDetail-shaped error frame + terminal `[DONE]`), `test_error_frame_client_disconnect_emits_nothing` (client disconnects mid-stream → NO error frame, gen left suspended — reuse the M16 disconnect harness), `test_error_frame_off_default_byte_identical` (default → exactly today). **Acceptance:** real-server raise + disconnect tests pass; default byte-identical; M15/M16 SSE tests green; gate green. ~4-6h.

---

## BATCH B — opt-in security/resilience middleware (P3 + P5) — real-server tests

### Task P3: inbound rate-limiter → 429 + Retry-After
**Files:** new `lancelot/ratelimit.py` (opt-in ASGI middleware; reuse `_TokenBucket` from `llm.py:128`). Test `tests/test_m17_ratelimit.py` (real-server flood).

**Confirmed gap (verify live):** every `429`/`Retry-After` in the tree is OUTBOUND (`llm.py`), the `@app.batch` shed, or the conn cap; `tenant.py` is per-tenant **concurrency** (`asyncio.Semaphore`), NOT rate-over-window. No inbound limiter.

**Change:** `lancelot.ratelimit.RateLimit(rate='100/minute', key='ip'|'api_key'|callable, burst=N)` — an opt-in pure-Python ASGI middleware that, per key, token-buckets requests and returns **429 + `Retry-After` + `RateLimit-Limit`/`RateLimit-Remaining`/`RateLimit-Reset` headers** on exceed. Reuse the existing `_TokenBucket` math. **Per-process by default** with a **LOUD docstring** that it under-counts on the multiprocess deploy Lancelot mandates, PLUS a pluggable `backend=` hook (a small interface) so users can wire Redis on the global case. **PANEL FENCES (binding): do NOT ship a Redis dependency; do NOT grow a rule-DSL.** Keep it one thin middleware.

**Tests (real-server flood):** `test_ratelimit_429_and_retry_after` (flood past the rate → 429 + valid `Retry-After` + `RateLimit-*`), `test_ratelimit_key_by_ip_vs_api_key` (independent buckets per key), `test_ratelimit_under_limit_passes` (within rate → 200). **Acceptance:** flood test 429s correctly; per-process caveat documented; no Redis/DSL; gate green. ~4-6h.

### Task P5: constant-time static-token / API-key gate
**Files:** new `lancelot/auth.py` (opt-in ASGI middleware; stdlib `hmac`/`secrets`/`hashlib` only). Test `tests/test_m17_token_gate.py` (real-server).

**Confirmed gap (verify live):** `security.py` only EXTRACTS credentials (`HTTPBearer`/`OAuth2PasswordBearer`/`APIKeyHeader` return the value) — nothing compares/verifies inbound. vLLM + Prefect hand-roll a gate and DIVERGE on the Host-spoof/allowlist subtleties.

**Change:** `lancelot.auth.StaticTokenGate(tokens=[...], scheme='Bearer', guard_prefixes=('/v1',), open_paths=('/livez','/readyz'), skip_options=True)` — opt-in ASGI middleware. stdlib only: hash the configured tokens once, `secrets.compare_digest` the presented token (constant-time, OR-loop over allowed tokens), emit a byte-exact **401 + `WWW-Authenticate`** WITHOUT entering DI. **Always read `scope['path']`** (never `request.url` — anti Host-spoof), skip `OPTIONS`, allowlist `open_paths`, guard only `guard_prefixes`. **PANEL FENCE (binding): this is a static-token gate, NOT a JWT/auth framework — do NOT add JWT/OAuth/scope logic** (that's user-side / `security.py`).

**Tests (real-server):** `test_token_gate_valid_invalid_missing` (valid→200, wrong→401+WWW-Authenticate, missing→401), `test_token_gate_skips_options_and_open_paths` (OPTIONS + `/livez` pass ungated), `test_token_gate_uses_scope_path_not_host` (a spoofed Host header can't bypass — the guard keys off `scope['path']`). **Acceptance:** auth tests pass incl. the Host-spoof case; constant-time compare; no JWT creep; gate green. ~3-5h.

---

## BATCH C — response/request shaping (P4 + P9-cap) — unit + conformance

### Task P4: `response_model` skip-validate-then-serialize (fenced)
**Files:** `lancelot/asgi.py` (the response path: `rm.model_validate(...)` then `_ta.dump_json(...)` — grep for the response_model coercion), `lancelot/routing.py` (route kwarg + the registration-time fence). Test `tests/test_m17_skip_validation.py`.

**Confirmed (verify live):** the response path does `model_validate(result, from_attributes=True)` THEN `dump_json(...)` — a double pass. Adapter cached at registration; no skip path. ~4× on a 1000-row read.

**Change:** `@app.get(..., response_model_skip_validation=True)` (opt-in, default False = parity): `dump_json` the handler result directly with `from_attributes=True`, skipping the validate pass (the user vouches for the shape). **PANEL FENCE (binding, verify the precise constraint): the response-model-shaping flags that need a VALIDATED instance — `response_model_exclude_unset` / `response_model_exclude_defaults` / `response_model_exclude_none` / `by_alias` where it relies on validation — are INCOMPATIBLE with skip-validation.** Verify which flags actually require a validated instance (test against pydantic-core), and **raise `FastAPIError` at REGISTRATION** if `response_model_skip_validation=True` is combined with any incompatible flag (fail loud at startup, never silently wrong at runtime). Document "you vouch for the shape; non-conforming returns serialize as-is."

**Tests (unit):** `test_skip_validation_serializes_directly` (a route returning ORM-ish objects with `skip_validation=True` serializes via `dump_json`+`from_attributes`, no validate pass — assert correctness on a conforming shape), `test_skip_validation_incompatible_flag_raises_at_registration` (combining with `response_model_exclude_unset=True` raises `FastAPIError` at decoration time), `test_skip_validation_off_byte_identical` (default → exactly today; conformance). **Acceptance:** tests pass; conformance 978 unchanged; the fence raises at registration. ~3-5h.

### Task P9-cap: safe pagination/sort params (`PageParams` + `SortParam`) — DoS-cap only, NO envelope
**Files:** new pagination dependency (e.g. `lancelot/pagination.py`) + export. Test `tests/test_m17_pagination_cap.py`.

**Confirmed footgun (verify live):** dispatch `database_service.py:738` `if items_per_page == -1: items_per_page = None` → unbounded full-table read by query param; bitcart `Query(default=5, ge=-1, le=1000)` + `if limit == -1` → unbounded. Lancelot's cap family (body/conn/timeout) has NO page-size cap.

**Change:** `lancelot.PageParams` — a dependency parsing `page`/`size` (or `limit`/`offset`) query params, **capped by `LANCELOT_PAGE_MAX_SIZE` (safe default, e.g. 100), with NO `-1`/unbounded escape hatch** (over-cap → 422, or clamp — pick one, document it; 422 is safer/clearer). `lancelot.SortParam(allowed=[...])` — parses a sort param against an **allowlist → 422 on a disallowed field** (anti sort-injection). The user still runs their own SQL with the validated/capped params. **PANEL FENCE (binding): do NOT ship the `Page[T]` response envelope** — 5-7 corpus projects each ship a different shape (nobody adopts a generic one), `fastapi-pagination` already exists, and it has zero byte-path angle. Cap + sort-allowlist ONLY; never sold as perf.

**Tests (unit):** `test_pageparams_caps_size` (size over `LANCELOT_PAGE_MAX_SIZE` → 422), `test_pageparams_rejects_negative_one` (the `-1` unbounded footgun is rejected, not honored), `test_sortparam_allowlist_422` (a field not in `allowed` → 422; an allowed one passes). **Acceptance:** tests pass; the `-1` DoS is closed; NO `Page[T]` envelope shipped; gate green. ~3-5h.

---

## Execution

| Batch | Tasks | Notes |
|---|---|---|
| A | P7, P6 | streaming observability; real-server; shared SSE/relay path |
| B | P3, P5 | new opt-in ASGI middleware (`ratelimit.py`, `auth.py`); real-server; reuse `_TokenBucket` + stdlib; NO Redis/JWT dep |
| C | P4, P9-cap | response/request shaping; unit + conformance; registration-time fence (P4); DoS-cap only (P9) |

Per batch: 1 impl subagent + 1 recheck subagent (Opus 4.8). Recheck covers: bugs/correctness, vulnerabilities (esp. P5 Host-spoof/constant-time, P3 bucket correctness), re-verify vs live source, patterns/anti-patterns, core goals/principles (the panel FENCES are binding: no Redis/JWT/DSL dep, no Page[T] envelope, P4 registration-fence, P6 cancel-frame-not-on-disconnect, pydantic-only runtime, default-off byte-identity), plan alignment (built exactly the task; real-server tests fail-without-fix). After all 3 batches: final whole-branch review, then the integration choice.

## Self-review (vs the panel verdict)
- BUILD: P7 ✓ (Batch A), P3 ✓ (B), P5 ✓ (B). FENCED: P4 ✓ (C, registration-fence), P6 ✓ (A, bundled w/ P7). RE-SCOPE: P9 ✓ (C, cap-only, envelope dropped).
- HELD/SKIPPED (NOT built, by panel verdict): R1 gzip (bench+sign-off), R2 base64 (pip pybase64), R3 broker (positioning), P9 `Page[T]` envelope (fastapi-pagination). Plus the M15 native-lifespan follow-up (task #7) remains open.
