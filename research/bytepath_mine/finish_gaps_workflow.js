export const meta = {
  name: 'm20-finish-gaps',
  description: 'Implement the remaining M20 byte-path gaps in 4 file-coherent batches, SEQUENTIAL (shared files), each gated + left green. No commits.',
  phases: [
    { title: 'A: DX/adoption', detail: 'openapi.py + security.py + di.py' },
    { title: 'B: obs/lifecycle', detail: 'asgi.py + app.py + metrics.py' },
    { title: 'C: batch concurrency', detail: 'batch.py + app.py + asgi.py' },
    { title: 'D: Rust hardening', detail: 'src/socket.rs + src/service.rs' },
  ],
}

const LC = '/Users/seventyleven/Desktop/Lancelot'
const LENS = `${LC}/research/bytepath_mine/00_BYTEPATH_LENS.md`
const FIND = `${LC}/research/bytepath_mine/FINDINGS.md`

const COMMON = `
CONTEXT: first-party, authorized backend-framework engineering on Lancelot (a FastAPI-compatible HTTP framework).
READ FIRST: the lens ${LENS} (positioning + cardinal rules) and the relevant rows of ${FIND} (exact mechanisms, cited). Read the actual Lancelot source you'll touch IN FULL before editing — match its style/conventions exactly.
CARDINAL RULES (hard): runtime dep is pydantic ONLY (lazy-import anything else; never make \`import lancelot\` pull a new hard dep); vendored conformance tests under conformance/vendor/ are SACRED (never edit); validate transport/streaming/resilience on the path the bug lives on, not just a happy-path unit test.
DISCIPLINE: implement ONLY your assigned items in ONLY your assigned files (+ your own NEW test file). Do NOT commit. Do NOT touch files outside your set (other batches own them). If an item turns out to violate a cardinal rule or be a non-bug on inspection, SKIP it and say so honestly (verify-before-fix) rather than forcing a bad change.
GATE: your assigned gate must be GREEN before you return. If you cannot get it green, do NOT leave the tree broken for the next batch — get your changes to a green state (fix or remove the offending piece) and report what you skipped.
You have FULL tool access (Read, Grep, Bash, Edit, Write). "Research/output" wording restricts only your reply format, not your inputs — read the real source; do not guess.
RETURN: a tight summary — items done / items skipped (with reason) / files touched / new test file / exact gate result.
`

const SPEC_A = `You are implementer batch A — DX/adoption (FastAPI-migration wins). ${COMMON}
FILES you may touch: lancelot/openapi.py, lancelot/security.py, lancelot/di.py, tests/test_m20_dx.py (new).
ITEMS:
1. A-T1.1 (FINDINGS addendum) — OpenAPI media type for streaming routes: \`@app.stream\` routes currently advertise \`application/json\` in the generated spec; they should advertise \`text/event-stream\` (SSE). Find in openapi.py's build_openapi how a route's response content media type is set, and how a stream route is identifiable (read routing.py / app.stream to see what metadata marks a stream route). Set the SSE media type for those routes.
2. A-T1.2 — build_openapi must forward \`servers\`, \`description\`, and \`tags\` (openapi_tags) when present. Check App.__init__ for these; if App already stores them, forward them into the spec; if not, add minimal optional App attributes/kwargs and forward. Keep it additive + byte-compatible when unset.
3. A-T1.3 — add \`OAuth2AuthorizationCodeBearer\` and \`OpenIdConnect\` to lancelot.security as REAL working security dependencies (not no-op stubs): they extract the credential and register the correct OpenAPI security scheme, modeled EXACTLY on the existing OAuth2PasswordBearer / other security classes already in lancelot/security.py (read it first, mirror the pattern incl. auto_error + the scheme dict). Goal: \`from lancelot.security import OAuth2AuthorizationCodeBearer, OpenIdConnect\` works as a FastAPI drop-in. (Optional: OAuth2PasswordRequestFormStrict if trivial.)
4. A-T2.1 — contextmanager_in_threadpool: sync yield-dependency teardown (__exit__) should run off the event loop with an UNLIMITED capacity limiter to avoid deadlock under load (FastAPI concurrency.py:17-41). Read di.py to find how lancelot currently offloads sync yield-dep teardown. IMPORTANT cardinal-rule check: if this requires anyio.CapacityLimiter, anyio is NOT a pydantic-only runtime dep — only proceed if you can do it without adding a hard runtime import (lazy-import, or the offload path is already guarded by an optional dep). If it would violate the pydantic-only rule, SKIP it and report.
GATE: \`cd ${LC} && .venv/bin/python -m pytest tests/ -q\` green AND \`cd ${LC} && make m1-conformance\` shows passes>=974 (your OpenAPI/DI changes are conformance-relevant). Add focused tests in tests/test_m20_dx.py (import-compat for the new security classes; OpenAPI spec shows SSE media type + forwarded servers/tags).`

const SPEC_B = `You are implementer batch B — observability + lifecycle. ${COMMON}
FILES you may touch: lancelot/asgi.py, lancelot/app.py, lancelot/metrics.py, tests/test_m20_obs_lifecycle.py (new).
ITEMS:
1. T1.2 — three-state /readyz: return 503 + Retry-After until the lifespan startup has COMPLETED (currently _readyz at app.py:~313 only 503s on self._shutting_down). Add an App flag (e.g. self._started=False in __init__ near _shutting_down) set True when the ASGI lifespan startup.complete is emitted (asgi.py — find where lifespan startup runs). **CRITICAL CAVEAT:** native \`app.run()\` / the native server drives NO lifespan (uvicorn-only), so a naive _started gate would make /readyz return 503 FOREVER under native run — you MUST also set _started=True on the native run path (find App.run / the native serve entry) so native deployments stay ready. Also: a user-registered GET /readyz route must still override the builtin (M19b lesson — verify with a test).
2. T3.6 — App(readyz_check=callable) optional readiness predicate (returns bool/awaitable); when set, /readyz is ready only if startup done AND readyz_check() truthy. Additive; default None = today.
3. T1.7 — emit 499 on a won client-disconnect race: when the handler is cancelled because the client disconnected (asgi.py ~599-630, the cancel_on_disconnect path), record status 499 in the RED metrics instead of the default 500. Find _final_status_holder / the metric record call.
4. T2.7 — method-gate RED metrics: env LANCELOT_METRICS_EXCLUDE_METHODS (default "OPTIONS,HEAD") — exclude those methods from the request COUNTER + DURATION histogram (keep the in-flight gauge counting everything). At the record_request call site in asgi.py + metrics.py.
5. T1.8 — gc.freeze() once after startup completes (same hook point as T1.2 _started, native + lifespan), env LANCELOT_NO_GC_FREEZE=1 to opt out. import gc lazily.
GATE: \`cd ${LC} && .venv/bin/python -m pytest tests/ -q\` green. Tests in tests/test_m20_obs_lifecycle.py: /readyz 503-before-startup then 200-after (TestClient lifespan), native-run stays ready (or at least _started set on run path — assert the flag logic), user /readyz override wins, method-gate excludes OPTIONS, 499 recorded on disconnect (or unit-test the status selection).`

const SPEC_C = `You are implementer batch C — @app.batch concurrency completion (FINDINGS T1.3 + T1.5 + T3.4). ${COMMON}
FILES you may touch: lancelot/batch.py, lancelot/app.py, lancelot/asgi.py, tests/test_m20_batch_concurrency.py (new).
NOTE: batch.py was just extended (T1.4 request_timeout_ms + T1.1 dedup_key). Read it fully first; entries are (payload, [waiter_futures], enq). BatchOverloadError already maps to 503 at the asgi.py seam; BatchItemTimeout to 504.
ITEMS:
1. T1.3 — \`@app.batch(max_concurrent_batches=N)\`: bound the number of concurrently in-flight \`_run_batch\` calls per registry with an asyncio.Semaphore. Default 0/None = UNBOUNDED (exactly today's behavior — no regression). When set, _run_batch acquires the semaphore around the handler await so window k+1 fills while batch k is upstream, but no more than N batches run at once.
2. T1.5 — overload shed: when max_concurrent_batches is set and the pipeline is SATURATED (semaphore at 0 available), a NEW submission sheds with BatchOverloadError (→ existing 503 + Retry-After seam). Shed the NEW arrival only — never already-queued/in-flight waiters. (Use a non-blocking acquire check at submit time, or track in-flight count vs cap.) Default unset = never sheds on compute.
3. T3.4 — name-the-knob: add an \`X-Lancelot-Shed-Reason\` response header on the batch sheds — at the BatchOverloadError 503 seam (value e.g. "batch-overload" or "batch-keys") and the BatchItemTimeout 504 seam (e.g. "batch-timeout") in asgi.py.
CAVEAT (M4 lesson): test the AWAITING-handler path — the semaphore is held across \`await handler(...)\`; a bug there deadlocks or poisons waiters. Add real awaiting-handler tests (asyncio.sleep in handler) for: pipelining (N+1 fills during batch k), shed-on-saturation returns 503 for the overflow caller while in-flight ones still succeed, default-unset = unbounded (no shed).
GATE: \`cd ${LC} && .venv/bin/python -m pytest tests/ -q\` green (the existing batch tests test_m2_tier_c5_batch.py / test_m15_batch_budget.py / test_m20_batch_completions.py MUST still pass).`

const SPEC_D = `You are implementer batch D — Rust byte-path hardening (FINDINGS T1.6 + T2.1). ${COMMON}
FILES you may touch: src/socket.rs, src/service.rs (+ wherever env knobs are read, e.g. an env/config module — read how LANCELOT_MAX_BODY / HEADER_TIMEOUT_MS are read and mirror it), tests/test_m20_rust_hardening.py (new).
ITEMS:
1. T1.6 — TCP keepalive on the listen/accepted socket: in src/socket.rs (where set_nodelay / TCP_QUICKACK / TCP_FASTOPEN are set via socket2), add SO_KEEPALIVE + keepalive idle/interval/count. Env LANCELOT_TCP_KEEPALIVE_SECS (0 = off/default; when >0, enable with idle=that, a sane interval+count). Reclaims MAX_CONNECTIONS slots from half-open conns. Add a SAFETY comment if any unsafe/setsockopt is used (match the existing TCP_QUICKACK pattern).
2. T2.1 — body-read deadline -> 408: in src/service.rs where the request body is collected (Limited::new(req.into_body(), max_body()).collect().await), wrap it in tokio::time::timeout(LANCELOT_BODY_TIMEOUT_MS). On timeout, respond 408 and release the connection slot. Env LANCELOT_BODY_TIMEOUT_MS, DEFAULT 0 = OFF (so legit slow uploads are unaffected unless the operator opts in). It's a total body-read deadline — document that in a comment.
PROCESS (required, in order):
  a. Edit the Rust.
  b. Rebuild the wheel: \`cd ${LC} && PATH="$HOME/.cargo/bin:$PATH" .venv/bin/maturin develop --release\` (takes a few min).
  c. \`cd ${LC} && .venv/bin/python -m pytest tests/ -q\` green.
  d. \`cd ${LC} && $HOME/.cargo/bin/cargo clippy --release --all-targets -- -D warnings\` — MUST be clean (no warnings).
  e. \`cd ${LC} && $HOME/.cargo/bin/cargo fmt\` then confirm \`cargo fmt --check\` clean.
ADVERSARIAL TEST (M4 — validate on the bug's path): in tests/test_m20_rust_hardening.py, a real subprocess server test: with LANCELOT_BODY_TIMEOUT_MS set small, a client that sends headers then STALLS the body gets 408 + the connection/slot is freed; AND a normal fast request still succeeds. (Mirror how tests/test_m4_resilience_load.py spins a subprocess server + raw socket — read it for the pattern.) If a full subprocess test is infeasible, at minimum assert the env knob is parsed and the wheel imports + serves a normal request.
GATE: pytest green AND clippy -D warnings clean AND fmt clean. If you cannot get clippy clean, fix it — a red clippy gate blocks the whole milestone.`

// ---- run sequentially (shared files: A/B/C/D touch overlapping app.py/asgi.py/batch.py) ----
log('Finishing M20 gaps in 4 sequential batches (A DX → B obs/lifecycle → C batch-concurrency → D Rust). Each gates + leaves green.')

const results = {}
phase('A: DX/adoption')
results.A = await agent(SPEC_A, { label: 'A:dx', phase: 'A: DX/adoption', model: 'sonnet' })
phase('B: obs/lifecycle')
results.B = await agent(SPEC_B, { label: 'B:obs-lifecycle', phase: 'B: obs/lifecycle' })
phase('C: batch concurrency')
results.C = await agent(SPEC_C, { label: 'C:batch-concurrency', phase: 'C: batch concurrency' })
phase('D: Rust hardening')
results.D = await agent(SPEC_D, { label: 'D:rust', phase: 'D: Rust hardening' })

return {
  A: results.A, B: results.B, C: results.C, D: results.D,
}
