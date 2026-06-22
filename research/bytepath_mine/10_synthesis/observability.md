# Observability — byte-path domain synthesis

**Scope.** RED metrics, streaming TTFT, W3C traceparent propagation, cost/token
accounting, request-id correlation, the OTel surface, and — the angle STEP 3
calls out — the **instrumentation seams**: what HOOKS Lancelot must expose so an
external SDK (Sentry/OTel/Prometheus) can wrap the request without
monkeypatching framework internals.

**Method.** Diffed every peer mechanism against the *actual* Lancelot source
(`lancelot/metrics.py`, `telemetry.py`, `upstream_metrics.py`, `context.py`,
`fleet.py`, the `asgi.py` record sites) + the `05_lancelot_state/` baseline +
the ledger's REFUSE/DEFERRED list. Sources: `02_fastapi/proj_auth_obs_ai.md`
(sentry-python ASGI seams O1–O6, langserve/r2r SSE taxonomy), `04_serving/*`
(LiteLLM/Portkey TTFT normalization, SGLang/TGI/vLLM metrics), `01_robyn`,
`03_blazeio`, and `05_lancelot_state/research_plans_ledger.md` (the BUILD/DEFER
ledger).

**Headline.** Lancelot's observability is already *deep* — RED + dual TTFT
(inbound stream + outbound upstream) + traceparent propagation + cost/token
ContextVars + 8-provider upstream taxonomy + cardinality-bounded labels. This is
**ahead of every peer** on AI-aware observability. The gaps are not "build a
metrics system" — they are (a) **a few precise correctness bugs the ledger
already flagged but never shipped** (499-on-disconnect never emitted;
RED fires on OPTIONS/HEAD; upstream error body never captured), (b) **one
genuinely-missing seam** — an OTel/Prometheus *export* path / a metric-attr hook
so the operator joins their own pipeline without re-implementing the meter, and
(c) **small operator-experience adds** (`queue_depth` on `/fleet/status`,
slow-consumer drain metric). Most are S-sized. Nothing here is a throughput
pitch; every item is operator-experience or correctness.

---

## What Lancelot already HAS (do NOT rebuild — tag so nothing is redundant)

| Mechanism | Lancelot location | Peer cross-ref |
|---|---|---|
| **RED metrics** (request counter / duration histogram / in-flight UpDownCounter) | `metrics.py:48–59`; emitted from ASGI seam `asgi.py:431` | sentry O3, vLLM/SGLang |
| **Route-TEMPLATE labels (cardinality-safe)** — `/items/{id}` not `/items/42`; `__not_found__`/`__method_not_allowed__`/`__unmatched__` sentinels for no-match | `metrics.py` docstring + `asgi.py:430` (`scope["_lancelot_route"]`) | sentry O2 (two-phase txn rename) — **Lancelot gets this right at match time** |
| **`http.status_code` attr on every metric** (adverse-path slicing: 499/503/504) | `metrics.py:148–158` | M7 T2.5 |
| **Streaming TTFT (inbound)** — first-streamed-chunk time at the ASGI send boundary; fires on `text/event-stream` OR first `more_body=True`; never unary | `metrics.py:67` + `asgi.py:525–571` (`_record_ttft_now`/`_ttft_send_wrapper`) | SGLang/TGI "TTFT+P99 are THE streaming metrics" (`04_serving/serving_frontier.md` §2) |
| **Upstream TTFT (outbound)** — first-chunk on the TransportPool side | `upstream_metrics.py:215`; `llm.py:521–554` | LiteLLM `lowest_latency.py` |
| **Cost / token accounting** — `cost_usd`/`tokens_in`/`tokens_out` ContextVars; `X-Lancelot-Cost-USD` trailer when >0; `lancelot.http.cost.usd` histogram; Lancelot ships **NO rates** (user `set_rate_table()`/`LANCELOT_LLM_RATES`) | `context.py:65–76`; `metrics.py:72–80`; `upstream_metrics.calc_cost_usd` | Sim.ai usage-billing (M7 T2.1) |
| **Upstream LLM taxonomy** — 8-provider detection, 3-shape token extraction, 5-class error taxonomy (`ok`/`quota`/`timeout`/`transient`/`permanent`/`upstream_5xx`), 6 OTel instruments under `lancelot.upstream.*` | `upstream_metrics.py` whole module | Commure error taxonomy; LiteLLM cooldown trip rules |
| **Queue-wait histogram** — `lancelot.http.queue_wait_ms`, emitted every request (incl. zero baseline) | `metrics.py:81–92`; set by `TenantConcurrencyLimiter` (`tenant.py:101–116`) | Turbopuffer (M7 T4.8) |
| **Request-id correlation** — `X-Request-ID`/`X-Correlation-ID` echo, CR/LF/NUL-scrubbed + 128-char bound, minted if absent; single header scan shared with WS | `context.py:20`; `asgi.py:440–467` (`_scan_request_context`) | sentry, every stack |
| **W3C traceparent propagation (no OTel dep)** — parse incoming `traceparent` → `trace_id`+`trace_parent` ContextVars; `TransportPool` forwards on outbound LLM call → joins distributed trace | `context.py:45–54,108–120`; `llm.py:764–797` (`_inject_traceparent`, `setdefault`) | the "lightweight context propagation, full span export is user's OTel job" boundary |
| **Telemetry sink seam** — `set_sink(callable)`, `Event(kind, attrs)`, default None (zero cost); OTel GenAI semconv shape; `gen_ai.cost.usd` always None (no rates) | `telemetry.py` whole module | OTel GenAI semconv |
| **Tenant-label cardinality cap** — `LANCELOT_TENANT_LABEL_MAX` (default 1000) → over-cap to `_overflow_`; memoized env read | `metrics.py:98–135` | Hightouch (M7 T2.4) |
| **Fleet rollup + region tagging** — `/fleet/status` {region, worker_id, status, in_flight, uptime_s, version}; `install_otel_resource_attrs()` adds region+worker_id as OTel **resource** attrs (per-process, no per-emit cost) | `fleet.py` whole module | Conductor (M7 T2.3) |
| **status-capture seam** — status read from `http.response.start` first send message (the only place it lives in ASGI) | `asgi.py:361,370` (`_final_status_holder`) | sentry O3 (exact technique) |
| **OTel is NEVER a runtime dep** — every public fn early-returns on a single None-check when `opentelemetry-api` isn't importable; user opts in by installing it | `metrics.py:33–95`, `upstream_metrics.py:194–232` | cardinal rule |

**Scope caveat already documented (not a gap):** RED/TTFT fire **only on the
Python ASGI seam**. The native `_native.run` const-tier server emits no OTel
metrics by design (`grep -rI opentelemetry src/` is empty). Operators who run the
prebaked-bytes tier for raw throughput get no RED from it — this is the
throughput/observability tradeoff, intentional.

---

## must_build (new or precise-correctness wins)

Ordered by ratio of (operator value) to (risk × effort). All S except where noted.

### MB1 — Emit **499** through RED metrics on a won client-disconnect race
**Mechanism.** sentry/Ray peek `http.response.start` for status; Lancelot does
too (`_final_status_holder=[500]`, set at `asgi.py:370`). But when the unary
`cancel_on_disconnect` race is WON by the disconnect (`_race_handler_vs_disconnect`
raises `CancelledError`, `asgi.py:599–630`) **the handler is cancelled before any
`http.response.start` is sent** → `_final_status_holder` is still its `500`
default → the metric records a **500**, not a client-cancel. The ledger
explicitly notes "`metrics.py:149` anticipates 499 but never produced it"
(`research_plans_ledger.md` ROUND 2 MED). 499 (nginx's client-closed-request
code) is the canonical label so dashboards don't count client aborts as server
errors.
**Source cite.** `05_lancelot_state/research_plans_ledger.md` ROUND-2 MED
("Emit 499 through metrics on a won disconnect race"); `asgi.py:361,599–630`;
sentry O3 (`02_fastapi/proj_auth_obs_ai.md`).
**Lancelot state.** PARTIAL — the disconnect race is detected and cancels the
handler correctly; the metric label is just wrong (500 instead of 499).
**Buildability.** S — in the `finally` at `asgi.py:431`, if the dispatch exited
via the disconnect `CancelledError` AND no `http.response.start` was observed,
record status `499`. Set a flag in the race-won path; check it before
`record_request`.
**Gate risk.** None. Conformance untouched (these tests don't disconnect
mid-flight). Needs a real-server subprocess test (cardinal rule 2): client
opens, server awaits, client drops → assert the metric carries 499 not 500.

### MB2 — Method-gate RED metrics (exclude OPTIONS/HEAD by default)
**Mechanism.** `record_request` fires for **every** method including CORS
preflight `OPTIONS` and `HEAD` probes (verified: `asgi.py:431` has no method
filter). On a CORS-heavy frontend, OPTIONS preflights can double the request
count and pollute the duration histogram with sub-ms no-ops, skewing P99 and
inflating the rate panel. R2R's "log only POST/DELETE" pattern is the same
instinct (`02_fastapi/proj_auth_obs_ai.md` S8). Want a knob, not a hard rule
(some operators DO want OPTIONS counts).
**Source cite.** `research_plans_ledger.md` ROUND-2 lower ("Method-gated RED
metrics (exclude OPTIONS/HEAD)"); `02_fastapi/proj_auth_obs_ai.md` S8; verified
no gate at `asgi.py:431`.
**Lancelot state.** MISSING — no method exclusion exists.
**Buildability.** S — `LANCELOT_METRICS_EXCLUDE_METHODS` env (default
`OPTIONS,HEAD`; empty string = count everything) checked once-memoized in the
seam before `record_request`. Keep the in-flight gauge counting everything
(operators still want to see preflight concurrency); only gate the
counter+duration emit.
**Gate risk.** None. Pure addition; default-on changes the metric stream so
document it as a default behavior change.

### MB3 — User-extensible metric-attr hook (the missing instrumentation seam)
**Mechanism.** This is the STEP-3 "what HOOKS must Lancelot expose" item. Today
the RED attr set is **hardcoded** (`http.route`/`http.method`/`http.status_*`/
`lancelot.tenant`, `metrics.py:152–158`) and there is **no way for an operator to
add a label** (e.g. `feature_flag`, `model`, `customer_tier`) without forking the
module — `grep` for any `metric_hook`/`extra_attr`/`set_metric` returns empty.
sentry survives precisely because Starlette exposes the request through the ASGI
scope so it can rename the transaction post-routing (O2) and read status from
`http.response.start` (O3); Lancelot should expose the symmetric seam for its
OWN meter: a single `lancelot.metrics.set_attr_hook(fn)` where
`fn(scope, status) -> dict[str,str]` returns extra bounded attributes merged into
every emit. Without this, the only escape is "install your own ASGI middleware
and re-implement RED," which defeats the built-in.
**Source cite.** sentry O1/O2/O3 (`02_fastapi/proj_auth_obs_ai.md` — the seams
exist *because* the framework exposes scope+send hooks); verified absence in
`lancelot/` (no metric-attr hook); `telemetry.set_sink` is the precedent shape.
**Lancelot state.** MISSING — `set_sink` exists for the GenAI cost *event* but
there is no hook for the RED *metric* attributes.
**Buildability.** S–M — `set_attr_hook(fn)` (default None, zero cost); call in
`record_request`, wrap in try/except (telemetry must never kill a request), and
**run the returned dict through the same `_bounded_tenant`-style cardinality
discipline** (cap the number of distinct values per key, bucket to `_overflow_`)
so a careless hook can't blow up the time-series count.
**Gate risk.** Perf: the hook is one dict-merge per request on the
metrics-enabled path only (OTel installed) — negligible, but the cardinality
guard is load-bearing or this becomes a footgun. Conformance untouched.

### MB4 — Truncate (~2 KB) + NUL-strip any captured upstream error body
**Mechanism.** `upstream_metrics`/`llm.py` parse the upstream response body for
tokens/model (`llm.py:498–507`, `upstream_metrics.extract_tokens`). When a call
fails, operators want the error body in their logs/trace — but an unbounded,
un-sanitized upstream body is both a memory-amplification vector and a
log-injection vector (CR/LF/NUL into the log line). Lancelot already applies
exactly this discipline to the request-id echo (CR/LF/NUL strip + 128-char bound,
`asgi.py:457–458`) — extend the same to any captured upstream body before it
reaches a sink/log. Verified: `upstream_metrics` does NOT currently retain/cap
the body (it only reads `usage`).
**Source cite.** `research_plans_ledger.md` BUILD list ("Truncate (~2KB)+NUL-strip
any captured upstream body in `upstream_metrics` — byte-path-build-order TIER 3;
Polar pattern"); `llm.py:498–507`; CR/LF/NUL precedent `asgi.py:457`.
**Lancelot state.** MISSING for the body path (the CR/LF/NUL discipline exists
elsewhere; not applied to captured upstream bodies).
**Buildability.** S — a `_safe_snippet(body, cap=2048)` helper (strip NUL +
collapse CR/LF, slice to cap) applied wherever an upstream body would be put in
the `info` dict or relayed to a sink. Off by default unless the caller asks for
the snippet (don't retain bodies on the happy path — zero-cost when not needed).
**Gate risk.** None. Extends existing sanitization discipline; no new dep.

### MB5 — `queue_depth` on `/fleet/status` (waiting behind the concurrency semaphore)
**Mechanism.** `/fleet/status` returns `{in_flight, uptime_s, status}` but not
**how many requests are queued** behind `TenantConcurrencyLimiter`/the
`max_concurrent_requests` semaphore. An LB doing least-loaded routing wants the
queue depth, not just in-flight, to avoid piling onto a saturated worker.
`TenantConcurrencyLimiter` already tracks the wait (it sets
`queue_wait_ms`); exposing a depth counter is the same signal at fleet level.
SGLang/TGI surface exactly this (waiting-queue length) for admission decisions.
**Source cite.** `04_serving/serving_frontier.md` §17 ("Adding `queue_depth` to
`/fleet/status` gives LBs the same signal. Zero new code beyond exposing an
existing counter… one-liner"); `fleet.py:57–89`; `tenant.py`.
**Lancelot state.** PARTIAL — the wait is measured per-request (queue_wait_ms);
a fleet-level depth counter is not exposed.
**Buildability.** S — increment/decrement a process-global `AtomicInt`-style
counter (a plain int under the GIL) around the semaphore acquire in the seam +
`TenantConcurrencyLimiter`; add `"queue_depth"` to `fleet_info(app)`.
**Gate risk.** None.

### MB6 — Per-event slow-consumer (client-drain) stream metric
**Mechanism.** For a streaming response, the dangerous pathology is a **slow
consumer**: the server keeps producing SSE frames while the client's TCP window
is full → the R3 pump's bounded channel backpressures (good — `LANCELOT_STREAM_BUFFER`)
but the operator has **no visibility** into how often / how long a stream stalls
on the client. TGI/SGLang track exactly this on the egress side
(`04_serving/serving_frontier.md` §9 — "if the server has emitted N bytes but the
client hasn't consumed them… keep emitting and you OOM"). A histogram of
per-stream time-blocked-on-send (or a counter of backpressure events) surfaces
slow consumers before they become an OOM incident.
**Source cite.** `research_plans_ledger.md` ROUND-2 lower ("Per-event
client-drain (slow-consumer) metric"); `04_serving/serving_frontier.md` §9;
R3 backpressure path (`src/stream.rs` `await_capacity`, CLAUDE.md).
**Lancelot state.** MISSING — backpressure works but is unobserved.
**Buildability.** M — the count lives where the R3 pump fires `await_capacity`
(only on a full channel). Cleanest as a Python-seam metric where the
`_ttft_send_wrapper`/stream send loop already sits (`asgi.py:1300+`): time each
`send` await; if it exceeds a threshold, record into a new
`lancelot.http.stream.drain_block_ms` histogram. Keep it OTel-gated +
zero-cost-when-disabled like the rest.
**Gate risk.** Perf: must not add a `perf_counter` call per chunk on the hot
streaming path when OTel is absent — gate the timing behind the same
`_maybe_bind() is not None` early check the other metrics use. Needs a
real-server slow-consumer test (cardinal rule 2): a deliberately-slow reader →
assert the drain metric fires.

### MB7 — Parse Azure `x-ms-retry-after-ms` in upstream retry (observability-adjacent correctness)
**Mechanism.** `_retry_after_seconds` parses `Retry-After` + `retry-after-ms`
(`llm.py:376–390`) but not Azure OpenAI's `x-ms-retry-after-ms`. Portkey parses
it (`retryHandler.ts:49`). This affects the `quota`/`timeout` taxonomy accuracy
and the retry-budget accounting — a missed header means Lancelot retries on its
own backoff schedule instead of the provider's hint, mis-attributing the wait.
**Source cite.** `04_serving/gateways_deploy.md` M1 ("Portkey also parses
`x-ms-retry-after-ms` (Azure-specific)… IMPROVE: add it — one-liner");
`llm.py:376–390`.
**Lancelot state.** PARTIAL — two of three retry-after header shapes parsed.
**Buildability.** S — one extra header check in `_retry_after_seconds`.
**Gate risk.** None.

---

## improve (documented recipes / hardening of existing seams — no new surface)

### IMP1 — ASGI recursion guard on Lancelot's own outermost dispatch (sub-app mount)
**Mechanism.** sentry uses an `_asgi_middleware_applied` ContextVar to avoid
double-wrapping when ASGI apps nest (O1, `02_fastapi/proj_auth_obs_ai.md`). If
Lancelot's `App.mount` lets a sub-app re-traverse the parent's outer
middleware/metric path, a single request would be **double-counted** in RED. Audit
the mount path; if double-traversal is possible, add a ContextVar guard so
`record_request` (and any outer middleware) fires exactly once per external
request.
**Source cite.** sentry O1; `lancelot/asgi.py` mount path.
**State.** PARTIAL (needs verification). **Buildability.** S. **Gate risk.** None
— if no double-traversal exists today, this is a one-line defensive guard +
a mount test.

### IMP2 — Standardized SSE envelope helper (`metadata`/`data`/`end`/`error` + error scrub)
**Mechanism.** LangServe/R2R converge on a named-event SSE taxonomy with **error
scrubbing** — the real exception text is never sent to the client; only a generic
`{"status_code":500,"message":"Internal Server Error"}` (`02_fastapi/proj_auth_obs_ai.md`
S3). Lancelot already routes SSE errors through `ProblemDetail` + a `done_sentinel`
(CLAUDE.md M15), so the scrubbing instinct is present; the addition is an optional
`lancelot.sse.SSEEvent` helper that wraps a user async-gen with the standard
envelope so AI-app callers get a predictable contract. This is the observability
contract for *clients*, complementing the server-side TTFT metric.
**Source cite.** `02_fastapi/proj_auth_obs_ai.md` S3; CLAUDE.md M15 SSE error
frame.
**State.** PARTIAL (error scrubbing exists; envelope not formalized as a helper).
**Buildability.** S. **Gate risk.** None — opt-in helper, no hot-path change.

### IMP3 — Document the receive/send re-wrap idempotency guard for middleware chaining
**Mechanism.** sentry guards `receive`/`send` re-wrapping by `__name__`
comparison (`_sentry_receive`, O4) so the same callables flowing through a stack
aren't wrapped twice. If Lancelot grows middleware that wraps `send` (the TTFT
wrapper already does, `asgi.py:542`), document the idempotency pattern so a future
middleware doesn't double-wrap `send` and double-fire the TTFT record.
**Source cite.** sentry O4; `asgi.py:542` (`_ttft_send_wrapper`).
**State.** HAVE (single TTFT wrapper today, no double-wrap) — this is a
**documentation** improvement to keep it that way. **Buildability.** S (doc).
**Gate risk.** None.

### IMP4 — Document the OTel resource-attr wiring step + readiness-callback hook
**Mechanism.** `install_otel_resource_attrs()` is intentionally NOT auto-called
(`fleet.py:100–125`) — it needs the user's OTel pipeline up first. This is
correct but under-documented; the operator runbook should show the exact wiring
order (install SDK → set meter provider → call `install_otel_resource_attrs()`).
Bundle with the ledger's "readiness-callback hook → /readyz 503" (5-project
convergence, `research_plans_ledger.md`) so `/readyz` can consult a
user-supplied "am I ready" probe — the operator-facing health seam.
**Source cite.** `fleet.py:100–125`; `research_plans_ledger.md` ROUND-2 ADAPT
("Readiness-callback hook → /readyz 503").
**State.** PARTIAL (resource attrs ship; readiness callback not wired).
**Buildability.** S. **Gate risk.** None.

---

## refuse (violates a cardinal rule / Tier-D / explicit ledger REFUSE)

| Item | Why refused |
|---|---|
| **Bundle an OTel/Prometheus EXPORTER as a runtime dep** (auto-export metrics) | Cardinal rule 3 (pydantic-only runtime). The ledger lists "OTel/Prometheus export transport" as a DEFERRED *mine target*, NOT a build — and any built-in exporter pulls `opentelemetry-sdk`/`prometheus_client` into `import lancelot`. The correct shape is **MB3's hook + the existing `set_sink` + lazy-bound API meter**; the operator installs their own exporter. Exposing the seam = build; shipping the exporter = refuse. |
| **Per-chunk Pydantic validation / re-serialization on streams for "structured metrics"** | Tier-D explicit (`research_plans_ledger.md` REFUSE) — perf cliff; streaming is raw-bytes + lightweight typing. |
| **Local tiktoken / heuristic token estimation in `upstream_metrics`** | By-design: provider-reported tokens only (`05_lancelot_state` table). A second tokenizer dep + drift from real billing; AI-logic-adjacent. |
| **Cost RATE table shipped in Lancelot** | Hard cardinal stance (`telemetry.py`/`upstream_metrics.py` docstrings): rates change weekly; a stale table silently diverges from billing. Ship the accumulator + emission; the operator supplies rates. |
| **Built-in structured access log** (per-request log line emit) | Operator-side / framework-bloat: uvicorn/hypercorn already emit access logs; Lancelot's RED metrics + request-id are the structured signal. A second log stream is duplicate machinery, and a built-in logger competes with the operator's logging config. (Note: Lancelot already logs stream-lifecycle events under `lancelot.stream` — that's lifecycle, not an access log, and stays.) |
| **Cross-worker metric aggregation / leader election for fleet rollup** | Ledger REFUSE ("Cross-worker pub/sub + leader election — external bus = user-side"). `/fleet/status` + the aggregator-is-operator's-choice model is the right boundary (`fleet.py` docstring). |
| **Full distributed-tracing span export (re-implement OTel spans)** | `context.py:43–54` boundary: Lancelot does lightweight context *propagation* (traceparent in/out, no dep); full span export is the user's OTel job. Re-implementing it is a cardinal-rule violation + scope creep. |
| **Latency-aware provider routing keyed on TTFT** (LiteLLM `lowest_latency`) | AI-logic / `app.llm` router layer, not the byte path. Record the TTFT (HAVE); routing on it is multi-deployment selection = user/`app.llm` side (`04_serving/gateways_deploy.md` M3 — "SKIP until latency-aware selector is built"). |
| **Failure-RATE circuit breaker in `TransportPool`** | Explicit Tier-D ("No circuit breaker"); `TransportPool` is single-provider per instance. The cooldown/trip constants belong to the `app.llm` router phase if ever built (`04_serving/gateways_deploy.md` M2). |

---

## Build order (just-build, brutal — by value/risk)

1. **MB1** (499-on-disconnect) — correctness bug the ledger flagged; dashboards
   currently miscount client aborts as 500s. S, no gate risk, needs a
   subprocess disconnect test.
2. **MB2** (method-gate OPTIONS/HEAD) — stops CORS preflight from skewing P99 +
   rate. S.
3. **MB4** (truncate+NUL-strip upstream body) — security + log-injection
   hardening, extends existing discipline. S.
4. **MB5** (`queue_depth` on `/fleet/status`) — one-liner, real LB signal. S.
5. **MB7** (Azure `x-ms-retry-after-ms`) — one-liner retry-accuracy fix. S.
6. **MB3** (metric-attr hook) — the real missing seam; unblocks operators who
   need custom labels without forking. S–M, cardinality guard is load-bearing.
7. **MB6** (slow-consumer stream metric) — M, needs a slow-reader load test +
   careful hot-path gating.
8. **IMP1–IMP4** — guards + recipes; fold into the same pass / docs.

Every item is operator-experience or correctness — **none** is a throughput
claim. The "build a metrics system" work is done; this is sharpening the edges
and exposing the one seam (MB3) that lets the operator's own pipeline plug in
without re-implementing what Lancelot already emits.
