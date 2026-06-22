# Streaming — byte-path domain synthesis

**Domain:** the R3 pump, SSE/WS, backpressure, client-disconnect→cancel, TTFT, AI token
streaming, mid-stream error chunks, request collectors, the `@with_cancellation` race.

**Method:** diff every peer streaming mechanism (vLLM, SGLang, TGI, Ray Serve, LitServe,
BentoML, Modal, Blazeio-relay) against Lancelot's current R3 pump + C2 cancellation, as
inventoried in `05_lancelot_state/bytepath.md §1` + `resilience.md` + source reads of
`lancelot/stream.py`, `lancelot/asgi.py` (the streaming seam ~lines 980–1400),
`lancelot/routing.py`, and `src/stream.rs`. **Additive:** anything M1–M19 already shipped is
tagged HAVE so it is never re-proposed; the Tier-D refuse list and the DEFERRED ledger
(`05_lancelot_state/research_plans_ledger.md`) are respected — items that violate the cardinal
rules go in `refuse`.

**Bottom line.** Lancelot's streaming core is the most complete in the corpus and is *ahead*
of every peer on the marquee axes: the on-loop `_drain` pump (one bridge crossing/stream),
bounded-mpsc backpressure, client-disconnect → upstream-cancel + `finally`/`aclose`, TTFT
metric, mid-stream `error_frame` + guaranteed `[DONE]`, `max_stream_silence` /
`max_stream_lifetime` watchdogs, `defer_status` eager-first-chunk, `stream_interval`
coalescing, SSE anti-buffering headers, single-task keepalive with separate data/ping silence
tracking. The peer mine surfaces **no new headline streaming primitive** — it surfaces a
handful of **small, real hardening gaps** on the egress/resilience edge and one **borderline
buildable** (the 1-slot coalescing mailbox, already DEFERRED as speculative). The honest read:
**streaming is in "harden, don't add" mode**, exactly the bias M4 earned.

---

## must_build (small, real, in-mission — egress resilience + observability)

These are the only streaming items that pass the bar (change the byte path or operator
experience, respect cardinal rules, not already shipped, not Tier-D). All are S/M. None is a
new marquee feature — each closes a measured DoS/observability/correctness edge on the
streaming path.

### S1 — `@app.stream(max_stream_bytes=N)` — output bytes cap on the egress path
- **Mechanism (peer):** SGLang clips per-request future-token budget (`CLIP_MAX_NEW_TOKENS=4096`,
  `schedule_policy.py:62`) so one runaway request can't starve the pool. The HTTP analog is a
  hard cap on *bytes emitted* by one stream. Source: `04_serving/serving_frontier.md §10`
  (re-verdicted there from REFUSE → NEW: "it's an output body cap, not model-server logic").
- **Lancelot state:** MISSING. `max_stream_silence` bounds *idle time*, `max_stream_lifetime`
  bounds *wall-clock duration* (`asgi.py:1092-1093`, `routing.py:70,77`) — neither bounds
  *size*. A correct-but-malicious gen that streams a fast infinite body within the lifetime
  window has no byte cap.
- **Approach:** add `max_stream_bytes` to `Route` (`routing.py`); in the ASGI pump
  (`asgi.py` ~`_send_serialized`), accumulate a per-stream byte counter on each DATA send;
  on breach, cancel the gen (its `finally`/`aclose` runs — reuse the existing
  `max_stream_silence` abort path) and, under `error_frame=True`, emit the scrubbed
  ProblemDetail + `[DONE]`. Same shape as the body cap (`LANCELOT_MAX_BODY` → 413) but on
  egress. Default `None` = off → byte-identical to today.
- **Buildability:** S. **gate_risk:** none — additive kwarg, off by default; conformance
  untouched (FastAPI has no analog); no Rust change (ASGI-seam only, consistent with
  `defer_status`/`max_stream_lifetime`).

### S2 — Emit `499` to metrics when the client-disconnect race is won (the missing adverse-path label)
- **Mechanism (peer):** vLLM/SGLang treat client-cancel as a first-class terminal state, not
  a 200. Lancelot's own metric layer *anticipates* it — `metrics.py:149` comment literally
  says "status=499 for client-disconnect" — but the code never produces it.
- **Lancelot state:** PARTIAL. The disconnect race fires correctly (`_watch_disconnect` +
  `cancel_on_disconnect`, `asgi.py:574-630,1292`); the upstream is cancelled; but the request
  is recorded with whatever status was committed (often 200 for an already-started stream) or
  nothing. Operators slicing RED metrics by `http.status_code` cannot see the
  disconnect-cancel rate — the single most important streaming-health signal for an LLM
  gateway. Confirmed BUILD candidate in the ledger (`research_plans_ledger.md §4 MED`:
  "Emit 499 through metrics on a won disconnect race — anticipated but never produced").
- **Approach:** when the disconnect arm wins the `FIRST_COMPLETED` race, set the recorded
  status to `499` (nginx convention) before `record_request` runs in the `finally`
  (`asgi.py:399-405`). One conditional in the streaming-cancel path. Do NOT change the wire
  (the client is already gone) — this is metric-only.
- **Buildability:** S. **gate_risk:** none — metric label only, no wire change, no
  conformance surface (vendored tests don't assert metrics).

### S3 — Per-stream slow-consumer signal (TTFT has a twin: time-to-first-*backpressure*)
- **Mechanism (peer):** vLLM's `RequestOutputCollector` (`v1_output_processor.py:45-107`)
  exists *because* a fast producer + slow HTTP consumer is the canonical streaming failure;
  Blazeio's `pause_writing`/`resume_writing` → `__overflow_evt__`
  (`03_blazeio/core.md M8`) is the same concern at the socket. The operator question is
  "are my streams backpressuring because clients are slow?" — distinct from TTFT.
- **Lancelot state:** MISSING as a *metric*. The mechanism is HAVE (the bounded-mpsc +
  `await_capacity` fires only on a full channel, `src/stream.rs:202` `ChunkSink`), but the
  *event* (a stream blocked on `await_capacity`) is never counted. The ledger lists
  "Per-event client-drain (slow-consumer) metric" as a BUILD-lower item
  (`research_plans_ledger.md §4`).
- **Approach:** in `ChunkSink::await_capacity` (the only place the pump blocks because the
  client isn't draining), increment a counter the Python seam can read, or emit a
  `lancelot.http.stream.backpressure` counter (lazy-bound like the other OTel instruments,
  `metrics.py`). Cheap: it fires only when the channel is full (rare unless a client is
  genuinely slow), so zero hot-path cost on the healthy path.
- **Buildability:** S (Python-seam counter) / M (if surfaced from Rust). **gate_risk:**
  none — additive OTel instrument, lazy-bound, no conformance surface. Honest caveat:
  for LLM streams (upstream-rate-limited) this rarely fires; the win is for large-payload /
  high-throughput streaming where the client *is* the bottleneck.

### S4 — `gc.freeze()` after startup (cut old-gen GC pause jitter on the stream path)
- **Mechanism (peer):** vLLM calls `freeze_gc_heap()` (= `gc.freeze()`) right before the
  lifespan `yield` (`openai/server_utils.py:467`, `02_fastapi/proj_inference.md M6`): every
  object alive at startup (route table, middleware chain, Pydantic validators, OTel
  instruments) moves to a permanent generation the GC never re-scans. Streaming is where this
  matters most — a gen-2 GC pause mid-stream is a visible TTFT/latency hiccup.
- **Lancelot state:** MISSING (grep: no `gc.freeze` anywhere in `lancelot/`). Lancelot's
  pydantic-only runtime means a smaller static heap than vLLM's model weights, but the route
  table + validators + middleware + OTel instruments are all long-lived and currently scanned
  on every gen-2 cycle.
- **Approach:** one line in the lifespan-startup-complete path
  (`asgi.py` lifespan, or `App.run()` after startup hooks): `if hasattr(gc, "freeze"):
  gc.freeze()`. Gate with an opt-out env `LANCELOT_NO_GC_FREEZE=1` for the rare env relying
  on cyclic-ref cleanup of static objects.
- **Buildability:** S (literally one guarded line). **gate_risk:** none — `gc.freeze()` is
  correctness-neutral (GC still runs, just skips the frozen gen); zero conformance impact.
  Validate-under-load: a sustained streaming soak with GC-pause histograms before/after
  (per cardinal rule 2 — don't ship "green on TestClient").

### S5 — Body-receipt deadline after headers (`LANCELOT_BODY_TIMEOUT_MS`) — slow-loris variant that bypasses the header timeout
- **Mechanism (peer):** Modal imposes `FIRST_MESSAGE_TIMEOUT_SECONDS = 5.0` on the first ASGI
  body message → synthesizes a 408 (`_runtime/asgi.py:121-160`,
  `02_fastapi/proj_serving.md §19`). This closes the "headers sent, body stalled forever"
  attack that slips past a *header*-read timeout.
- **Lancelot state:** MISSING. `LANCELOT_HEADER_TIMEOUT_MS` (default 15000, `service.rs:124`)
  guards header receipt; once headers parse and the Rust→Python bridge fires, there is **no
  deadline on the request body arriving**. A client that sends headers and holds the body
  open burns a `LANCELOT_MAX_CONNECTIONS` slot indefinitely. This is squarely a streaming /
  resilience gap (it's the inbound mirror of the egress slow-consumer problem).
- **Approach:** a Rust-side body-read deadline in `src/service.rs` (a `tokio::time::timeout`
  around the `Limited` body collect, or a per-poll idle deadline) → 408. New knob
  `LANCELOT_BODY_TIMEOUT_MS`, default off or generous (streaming uploads are legitimate, so
  this must default permissive and be opt-in-tight, like `LANCELOT_MAX_INFLIGHT_BYTES`).
  Validate under the M4 adversarial harness (the "headers-then-stall" client).
- **Buildability:** M (Rust, hyper body path). **gate_risk:** clippy (new Rust on the request
  path — must satisfy the `await_holding_lock` / no-unwrap denies); perf (must be OnceLock-read
  + zero-cost when off); no conformance surface. **Honest caveat:** must not break legitimate
  slow streaming-multipart uploads — default permissive, document as opt-in for tight
  deployments.

---

## have (already shipped in Lancelot — explicitly NOT to build)

The peer mine's streaming mechanisms map overwhelmingly to features M1–M19 already ship. Listed
so no synthesizer re-proposes them.

- **On-loop `_drain` R3 pump (one bridge crossing/stream, same-thread `try_push`,
  GIL-dropped per chunk).** `src/stream.rs` `drainer()`/`stream_body()`/`ChunkSink`. Beats
  every peer pump: Robyn's per-`__anext__` `spawn_blocking`+`with_gil`
  (`01_robyn/teardown.md M11`, `responses.py AsyncGeneratorWrapper`'s per-chunk
  `run_until_complete`), FastAPI's sync-gen thread-hop per chunk. The 16–26× regression this
  *was* is fixed and is on the Tier-D "never revert" list.
- **Bounded-mpsc backpressure, event-driven (`await_capacity` fires only on full).**
  `src/stream.rs:202`, `LANCELOT_STREAM_BUFFER` default 256. Structurally equivalent to
  Blazeio's `pause_reading` deque discipline (`03_blazeio/core.md M2`) but in Rust, no GIL.
- **Client-disconnect → cancel → upstream abort + `finally`/`aclose`.** The `@with_cancellation`
  dual-task `FIRST_COMPLETED` race (vLLM `utils.py:57-99`, `02_fastapi/proj_inference.md M1`)
  is Lancelot's `_watch_disconnect` + `_race_handler_vs_disconnect` (`asgi.py:574-630`,
  `cancel_on_disconnect`, `asgi.py:1292`); the upstream-cancel half is `TransportPool`'s
  `_race_chunk_vs_cancel` + `idle_guarded_chunks` + `relay` (`llm.py:36-72,634-749`).
  Validated under real adversarial load (M4 C2). This IS the moat
  (`research_plans_ledger.md §5`): inbound stream and outbound LLM are one cancellation-correct
  Rust-owned system. Blazeio's relay *omits* the upstream abort — Lancelot's moat is real
  (`03_blazeio/modules.md §4,§15`).
- **Mid-stream error-as-chunk (`error_frame`) + guaranteed terminal `[DONE]`.** vLLM emits the
  error as an SSE frame then unconditional `data: [DONE]\n\n`
  (`chat_completion/serving.py:989-996`, `02_fastapi/proj_inference.md M2`). Lancelot:
  `error_frame=True` → scrubbed ProblemDetail frame + guaranteed `done_sentinel`
  (`asgi.py:1107,1155-1397`, `routing.py:87`). Distinguishes genuine internal cancel from
  client disconnect (`routing.py:84`).
- **TTFT (time-to-first-chunk) metric, inbound + outbound.** Every serving stack agrees TTFT +
  P99 are THE streaming metrics (`04_serving/serving_frontier.md §2`). Lancelot:
  `lancelot.http.stream.ttft` (`metrics.py:67`, `_ttft_send_wrapper`, `asgi.py`) fires on first
  streamed chunk, excludes keepalive pings; outbound `lancelot.upstream.ttft`
  (`upstream_metrics.py`). Extended to any-body in M19.
- **`stream_interval` (N events/SSE frame, first token always immediate).** SGLang
  `stream_interval=1` (`server_args.py:439`), vLLM `RequestState.make_request_output`
  (`v1_output_processor.py:287`). Lancelot: `@app.stream(stream_interval=N)` (`routing.py:53`,
  `asgi.py:1054`).
- **`max_stream_lifetime` reconnect sentinel.** Absolute wall-clock cap → `event: reconnect\n
  data: {"reason":"max_stream_lifetime"}\n\n` + close (`asgi.py:1360`, `routing.py:77`).
- **`max_stream_silence` idle-abort with separate DATA/PING tracking.** Caps how long a stream
  may emit *only* keepalive pings before the gen is cancelled (`asgi.py:1092,1322-1340`,
  `routing.py:70`). The "ping resets its own silence timer" zombie-stream bug is already fixed
  (last-DATA tracked separately from last-PING, `asgi.py:1080-1091`). This is the
  `@app.stream`-level idle timeout the serving frontier wanted — distinct from
  `LANCELOT_UPSTREAM_STREAM_IDLE_SECS` on `TransportPool`.
- **`defer_status` eager-first-chunk → pre-yield errors become real 4xx/5xx, not 200+broken
  body.** BentoML consumes the first yield eagerly (`io_models.py:225-257`,
  `02_fastapi/proj_serving.md §10`). Lancelot: `@app.stream(defer_status=True)` pulls the first
  item before committing `http.response.start` (`asgi.py:981-1018`), handling
  StopAsyncIteration / CancelledError / HTTPException distinctly. Default off for byte-parity.
  (Native R3 server commits 200 before the first chunk by design — a documented limitation,
  `src/stream.rs:65-79`, NOT a build item.)
- **Single-task SSE keepalive (15 s comment ping), serialized through a lock.** Replaced the
  per-chunk `wait_for(shield(ensure_future(...)))` that caused the M5 10× regression
  (`asgi.py:1066-1097`). Matches FastAPI's `_PING_INTERVAL = 15.0`.
- **SSE anti-buffering headers.** `cache-control: no-cache` + `x-accel-buffering: no` on every
  SSE response (`asgi.py:1041-1042`). Blazeio sets the same plus HTTP/1.0 remnants
  (`Pragma`/`Expires`, `03_blazeio/modules.md §12`, `01_robyn/teardown.md M10`) — those are
  proxy-era cruft; the Lancelot subset is correct. Robyn's double-`Cache-Control` bug is
  avoided (single set site).
- **In-flight gauge held for the full stream duration.** vLLM needs `@load_aware_call`'s
  `BackgroundTask`-deferred decrement (`utils.py:106-151`, `02_fastapi/proj_inference.md M8`)
  because FastAPI's `StreamingResponse` returns immediately. Lancelot's pump runs *inside*
  `_http_dispatch`, which returns only after the last chunk — so the `in_flight_delta(-1)` in
  the `finally` (`asgi.py:399-405`) already fires post-stream. Verified: not a bug.
- **SSE `[DONE]` sentinel on clean exhaustion.** `sse_adapter(done_sentinel="[DONE]")`
  (`stream.py:76`). OpenAI-compatible terminator (`04_serving/serving_frontier.md §24`,
  LitServe `02_fastapi/proj_serving.md §23`).
- **Stream-resource teardown is request-scoped (yield-deps stay open through the stream,
  PEP-789 ordering).** Differentiator from FastAPI's GC-finalization edge
  (`research_plans_ledger.md §7.1 §9`).
- **`bytes`/`str`/`Event` chunk coercion + `render()` hook + media-type detection (CRLF/ctrl
  stripped).** `src/stream.rs:80,142`, `stream.py Event.render`. (M4 fixed the silent-empty-body
  bug on bytes/str yields.)
- **SSE id/name CR/LF/NUL sanitization.** M13 (`research_plans_ledger.md §3`).
- **TLS-record / write coalescing for SSE.** Blazeio's `Batchedwriter` (30 KB threshold,
  `03_blazeio/core.md M7`) and SSE-exempt TLS coalescer (`03_blazeio/modules.md §5,§8`) are
  NOT NEEDED in Lancelot — the Rust hyper/tokio layer buffers at the frame level; a Python
  `bytearray` coalescer above it would double-buffer and add GIL hold time. (One narrow
  *non*-SSE relay coalescer is noted in `03_blazeio/modules.md Action B` as low-priority and
  out of *this* domain's scope — it's a relay-throughput item, not a streaming-pump item.)

---

## refuse (violates a cardinal rule or the Tier-D / DEFERRED ledger)

- **Regress the R3 pump to per-`__anext__` bridging** (any cross-thread
  `into_future`/`call_soon_threadsafe` per chunk). Tier-D explicit: re-creates the 16–26×
  cliff. The on-loop `_drain` + same-thread `try_push` must stay.
- **Optimize `@app.stream` per-chunk overhead** (e.g. a keepalive-loop refactor for raw
  throughput). Tier-D: LLM token streaming is upstream-rate-limited (~50–200 tok/s/stream);
  the framework is never the bottleneck. ~6.7× slower than raw Starlette at MAX chunk rate is
  irrelevant — the LLM is the rate limiter.
- **Per-chunk Pydantic validation on streams.** Tier-D: perf cliff. Streaming is raw-bytes +
  lightweight typing.
- **Per-chunk / streaming gzip (Brotli or gzip).** Tier-D: per-chunk compression is banned;
  `GZipMiddleware` (one-shot, off-loop) is the right shape and excludes `text/event-stream`
  (`compress.py:74`). Brotli is a second dep + negotiation branch (proxy's job).
- **`Last-Event-ID` SSE replay / resumable streams.** Tier-D / DEFERRED: stateful resume is
  user-side; `stream.py` already declined it; the WBS U29 MCP resumability is a separate
  unconfirmed track, not a general `@app.stream` feature.
- **Streaming batch handlers (per-caller generators from one `@app.batch`).** SKIP per Tier-D
  (`02_fastapi/proj_serving.md §5`): crosses into AI-logic; the throughput win of batching
  (fewer upstream calls) is already delivered by non-streaming `@app.batch`; an
  `async-gen → N-caller demux` is orchestration.
- **Mid-stream abort as a new primitive.** Already-have: a user aborts by returning from their
  async-gen; client-side abort is the disconnect→cancel path.
- **`POST /abort_request` by request-id control-plane endpoint** (SGLang
  `04_serving/serving_frontier.md §8`). Out of this domain and low-value: requires a
  thread-safe in-flight `{request_id: Task}` registry; the high-frequency case
  (client-disconnect) is already covered. Ship only on explicit operator demand — not a
  streaming-pump win.
- **Engine-death watchdog flipping `should_exit`** (vLLM M3,
  `02_fastapi/proj_inference.md M3`). N/A: Lancelot has no separate engine process — Lancelot
  IS the byte path; the upstream LLM lives behind `TransportPool`. The global "no requests in
  N s → restart" watchdog is a k8s liveness-probe / orchestrator concern, not framework code.
- **`RequestOutputCollector` 1-slot coalescing mailbox** (vLLM M4,
  `02_fastapi/proj_inference.md M4`). **Borderline — kept in refuse for this pass.** It's
  technically buildable (an `asyncio.Event` + in-place merge replacing one bounded-mpsc slot,
  S–M), and it IS the DEFERRED "coalescing 1-slot mailbox opt-in `@app.stream` mode"
  (`research_plans_ledger.md §2`, "Speculative; build only on demonstrated demand"). But its
  whole purpose — coarsen chunks for a slow consumer instead of backpressuring — only matters
  when the *producer* is faster than the consumer, which for LLM streaming it never is (the LLM
  is the slowest link). Lancelot's bounded-mpsc already gives correct backpressure. Building a
  merge mailbox now is overkill (no demonstrated demand, against the no-bullshit rule). Revisit
  only if a real workload shows fast-producer/slow-consumer streaming where backpressure on the
  producer is unacceptable. S2 (the slow-consumer *metric*) is the cheap first step that would
  *prove* whether this is ever needed.

---

## improve (sharpen existing streaming surface — lower priority than must_build)

- **`@app.batch` adaptive window + anti-starvation step cap** (TGI `waiting_served_ratio=0.3` +
  `max_waiting_tokens=20`, `04_serving/serving_frontier.md §3`). The batch window auto-extends
  while the rate-limited upstream is busy, shrinks when free → more requests/upstream-call at
  the same worst-case latency. *Adjacent to streaming* (batched streams), genuinely in-mission
  for the LLM-gateway pitch, S effort. **Note:** the Tier-D list bans "adaptive SLA-aware
  batch window / dynamic model batching" as worker-side — so this must be framed strictly as a
  *transport admission* knob (flush-when-full-OR-starvation-timeout), NOT model-aware SLA
  logic, to stay legal. Buildability S; gate_risk none.
- **`@app.batch` in-batch dedup — fan one upstream result to all identical-key waiters**
  (SGLang in-batch prefix caching, `04_serving/serving_frontier.md §4`). `@app.batch` already
  has `key=`; today it sends all members together. Sending ONE representative per key and
  fanning the result to identical-key waiters is strictly better when N≫1 for the same prompt
  (10× → 100× for repeated-prompt bursts). Adjacent to streaming (the marquee `@app.batch`
  primitive). Buildability M; gate_risk none. (High value, but it's a batch-coalescing item;
  belongs to the batch synthesizer's primary domain — flag here for cross-domain visibility.)
- **`@app.batch` per-item request timeout + overload-shed 503** (BentoML CorkDispatcher
  `dispatcher.py:227-230`, LitServe `loops/base.py:111-130`, `02_fastapi/proj_serving.md
  §8,§9`). An item that waited longer than its deadline is cancelled, not run (the exact class
  of bug that broke `@app.batch` under M4 load). Store a monotonic timestamp in the
  `(payload, future)` tuple; `_flush` drops stale futures. Buildability S; gate_risk none.
- **`max_concurrent_batches` pipelining on `@app.batch`** (Ray Serve `batching.py:439`). Batch
  k+1 fills while batch k is in flight upstream → ~10% throughput on rate-limited backends.
  Buildability S; gate_risk none.
- **SSE `seq_id` cursor on keepalive ping** (`research_plans_ledger.md §4 lower`). A monotonic
  cursor on keepalive pings lets a reconnecting client signal where it was — a *lightweight*
  step short of full `Last-Event-ID` replay (which stays refused). Buildability S; gate_risk
  none. Only build on demonstrated client-reconnect demand.
- **`Server-Timing` header surfacing `queue_wait_ms` + duration** (Turbopuffer pattern,
  `research_plans_ledger.md §4`). Flag-gated (not every response). Gives streaming-client devs
  the queue-wait + handler-time breakdown in-band. Buildability S; gate_risk none.

---

## Buildability + gate-risk summary (streaming domain)

| Item | Class | Build | gate_risk | Lancelot state |
|---|---|---|---|---|
| S1 `max_stream_bytes` egress cap | must_build | S | none (ASGI-seam, off-by-default) | missing |
| S2 emit 499 on won disconnect race | must_build | S | none (metric-only) | partial (label anticipated, never produced) |
| S3 slow-consumer / backpressure counter | must_build | S–M | none (lazy OTel instrument) | missing (mechanism HAVE, metric not) |
| S4 `gc.freeze()` post-startup | must_build | S | none (correctness-neutral) | missing |
| S5 `LANCELOT_BODY_TIMEOUT_MS` body deadline | must_build | M | clippy + perf (Rust request path) | missing |
| adaptive batch window (transport-only) | improve | S | none | partial |
| in-batch dedup fan-out | improve | M | none | missing (batch domain) |
| per-item batch timeout + shed | improve | S | none | missing |
| `max_concurrent_batches` | improve | S | none | missing |
| SSE seq_id cursor on ping | improve | S | none | missing |
| Server-Timing header | improve | S | none | missing |

**Gate discipline (all items):** every must_build that touches the wire/Rust gets a
real-server subprocess + HTTP adversarial test (cardinal rule 2 — S5's "headers-then-stall"
client, S1's runaway-gen, S4's GC-pause-under-streaming-soak); conformance floor stays
`passes=974`; `cargo clippy --release --all-targets -- -D warnings` clean (S5);
`pydantic>=2.7`-only runtime preserved (all items are stdlib/Rust/OTel-lazy). No item adds a
runtime import to `import lancelot`.
