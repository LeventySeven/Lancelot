# M20 — Byte-path mine build plan (Robyn / FastAPI / Blazeio → Lancelot)

**Source of truth:** `research/bytepath_mine/FINDINGS.md` (ranked, 9 domains) + `research/bytepath_mine/00_BYTEPATH_LENS.md` + the 9 `10_synthesis/*.md` domain briefs.
**Method:** mine → plan → build. Every batch = one gated change. **Gate (all):** `.venv/bin/python -m pytest tests/ -q` + `make m1-conformance` (floor **passes=974**) + (Rust only) `$HOME/.cargo/bin/cargo clippy --release --all-targets -- -D warnings` + fmt. Transport/streaming/resilience features get a **real-server (subprocess+HTTP) adversarial test** — not just TestClient (the M4 lesson: 4 headline features shipped green-on-TestClient but broke under load).

## Verdict (why this plan is small and surgical)
The streaming / resilience / serialization / observability / routing / concurrency cores are **mature and ahead of every peer — there is no new headline primitive in this mine.** The moat (one Rust-owned, backpressured, cancellation-correct in+out byte path) is intact. The real wins, in priority order: (1) **complete `@app.batch`** — the one place with genuine new AI-serving/cost value; (2) **source-verified correctness bugs**; (3) cheap **hardening/observability** edges; (4) **FastAPI-parity/adoption** fixes. Do **not** relitigate the REFUSED list (FINDINGS appendix: faster serializer, circuit breaker, model gateway, durable job, built-in CLI, per-chunk gzip, etc.).

---

## Build order (each batch is an independently gated change)

### Batch 1 — Correctness & adoption fixes (small, high-confidence) ← START HERE
Verify-then-fix; each gets a test. Pure-Python, conformance-safe.
- **Decimal int/float fidelity** (T3.7) — `validation.py` `_json_default`: emit `int(obj)` when a `Decimal` has `exponent >= 0`, else `float`. FastAPI `encoders.py:84` parity; silent corruption for financial/`Numeric(_,0)` APIs today.
- **`inspect.signature(fn, eval_str=True)`** (dx bug) — `di.py`: handlers using `from __future__ import annotations` currently get stringified annotations → params silently skipped. Verify, then add `eval_str=True`.
- **`FileResponse` / `RedirectResponse` re-export** (dx I7) — confirmed `ImportError` from `lancelot.responses`; add to `responses.py` surface + `__init__` lazy compat. Migration blocker.
- **`docs.py` HTML-escape** (dx I2) — escape `<>&` in injected JSON for Swagger/ReDoc HTML. Security (stored-config XSS).
Gate: pytest + conformance.

### Batch 2 — Readyz startup gate + readiness hook (code-verified deploy bug)
- **Three-state `/readyz`** (T1.2) — 503 + Retry-After until the lifespan `startup` completes. `app.py:314` `_readyz()` only checks `_shutting_down`; add `_started` flag set when `lifespan.startup.complete` is sent (`asgi.py`). `docs/deploy.md` currently *wrongly* claims this is fixed — correct it.
- **`App(readyz_check=fn)`** (T3.6) — optional user readiness predicate; ensure a user-registered `/readyz` route can still override the builtin (M19b lesson).
Gate: pytest + conformance + **real-server test** (probe returns 503 during startup, 200 after).

### Batch 3 — `@app.batch` completions (THE headline AI-serving win)
One coherent change in `batch.py`. **EACH feature gets a real-server awaiting-handler test** (M4 lesson: the timer-self-cancel bug shipped green on non-awaiting tests).
- **T1.1 dedup** — `dedup_key=`: identical-key waiters in a window reuse ONE upstream call, fan the result to all. (Nx cost cut for repeated-prompt RAG / retry storms / identical polls. SGLang `schedule_policy.py:160`.)
- **T1.3 `max_concurrent_batches=N`** — pipeline window k+1 while batch k is upstream (a `Semaphore`); default 1 = today's behavior. (Ray Serve `batching.py:439`.)
- **T1.4 `request_timeout_ms=N`** — per-item timestamp; drop stale waiters with a **504 exception** (NOT skip — skipping hangs the waiter, the exact M4 failure) before the handler runs. (BentoML `dispatcher.py:227`.)
- **T1.5 compute-overload shed** — 503 + Retry-After when the pipeline semaphore is saturated (reuse `BatchOverloadError`). (BentoML `dispatcher.py:121`.)
- **T3.4 name-the-knob** — `X-Lancelot-Shed-Reason` header on the 503.
Gate: pytest + conformance + the awaiting-handler real-server tests.

### Batch 4 — Observability & retry correctness (Python)
- **Emit `499`** on a won client-disconnect race (T1.7) — `_final_status_holder` stays 500 today; record 499. `metrics.py:149`/`asgi.py:599`. Real-server disconnect test.
- **Method-gate RED metrics** (T2.7) — `LANCELOT_METRICS_EXCLUDE_METHODS` default `OPTIONS,HEAD`; keep the in-flight gauge counting everything. Document the metric-stream change.
- **Retry sharpening (one PR, `llm.py`)** — decorrelated/full-jitter backoff (T1.9, kills fanout thundering-herd) + Azure `x-ms-retry-after-ms` (T3.3) + honor inbound `x-should-retry` (T3.5).
- **`gc.freeze()` after lifespan startup** (T1.8) — `LANCELOT_NO_GC_FREEZE=1` opt-out; soak-validate the TTFT-histogram delta (vLLM `server_utils.py:467`).
Gate: pytest + conformance.

### Batch 5 — OpenAPI / DX parity + security-scheme import-compat
- **SSE/JSONL media type in OpenAPI** (dx #1) — `@app.stream` routes advertise `text/event-stream`, not `application/json`. `openapi.py`.
- **`servers` / `description` / `tags` forwarding** in `build_openapi` (dx #2).
- **`OAuth2AuthorizationCodeBearer` + `OpenIdConnect` + `OAuth2PasswordRequestFormStrict` stubs** (dx #3/I3/I4) — import-compat for Auth0/Okta/Cognito migration. `security.py`.
- **`contextmanager_in_threadpool` unlimited `CapacityLimiter`** (dx #6) — sync yield-dep `__exit__` deadlock risk under load. `di.py`. FastAPI `concurrency.py:17`.
Gate: pytest + conformance.

### Batch 6 — Rust hardening (gate adds clippy + adversarial/bench)
- **TCP keepalive triple** on the listen socket (T1.6) — `SO_KEEPALIVE` + idle/intvl/cnt, `LANCELOT_TCP_KEEPALIVE_SECS`. `src/socket.rs` (~6 lines). Reclaims `MAX_CONNECTIONS` slots from half-open conns (k8s evict / spot term / mobile drop). Blazeio `socket_ops.py:19`.
- **Body-read deadline → 408** (T2.1) — `LANCELOT_BODY_TIMEOUT_MS`, **default 0=off**. `src/service.rs:330` (wrap `Limited…collect()` in `tokio::time::timeout`). Adversarial test: headers-then-body-stall → 408 + slot release, AND a slow-but-progressing upload is NOT killed.
- **(bench-gated)** `freelist` on `Request`/awaitable + `strip=true` release profile + 0-param `call0` fast path (T2.8 / routing improve) — ship **only if** the oha tiny-shape delta is real under the GIL; never pitch as a throughput headline (Tier-D).
Gate: pytest + conformance + clippy -D warnings + fmt + bench/adversarial.

### Batch 7 — Validation-gated T2 (each needs its named load test)
`max_stream_bytes` egress cap (T2.2) · metric-attr hook + cardinality guard (T2.3) · Python-seam streaming bytes-in-flight cap (T2.4) · slow-consumer/backpressure metric (T2.6). Each merges only behind its named load/bench test.

### Deferred (T3) / REFUSED
See `FINDINGS.md` T3 table + REFUSED appendix. The Granian custom-awaitable (routing_request must_build #1) is **bench-gated/large** → T3/Batch-6-optional; it's the only lever on the one shape Lancelot trails raw Starlette (~88%), but may wash out under the GIL — measure before building, never pitch as a headline.

---

## Notes
- **Naming:** this is **M20**. Tag per-batch only when its gate is green.
- **Cardinal rules in force:** pydantic-only runtime (lazy any new dep); vendored conformance tests sacred; `git -C` for `researchfms`; no red-gate tag; validate-under-load.
- **Confidence:** every "MISSING/bug" claim in FINDINGS was source-verified by the ranker; Batch 1 re-verifies each against `lancelot/` before fixing (don't fix a non-bug).
