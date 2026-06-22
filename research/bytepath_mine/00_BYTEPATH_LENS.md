# Lancelot byte-path mine — lens (read this first)

**Mission.** A comprehensive, **SOURCE-LEVEL** mine of Lancelot's peer HTTP frameworks — **Robyn** (Rust+Python, the closest architectural peer), **FastAPI** (+ its real production ecosystem), **Blazeio** (async-Python, perf-focused) — plus AI-serving infra, to surface concrete **byte-path wins Lancelot should build next**. Additive: every finding is cross-checked against what Lancelot already has. The pass ends in a prioritized build plan and shipped code.

**Method = "just build, brutal."** No overkill, no bullshit, no chasing 1% wins. Every candidate must **change the byte path or the operator experience** — else it doesn't ship. Mechanism-level, cited, honest about what's a real win vs a micro-optimization vs a non-bottleneck.

## What Lancelot IS (positioning — do not drift)
A backend framework like FastAPI/Robyn that optimizes **HTTP transport / serialization / streaming / resilience** for **AI / cloud / web / startups**. **AI logic stays user-side**; Lancelot owns the byte path. Rust core (hyper/tokio/pyo3 + matchit), pydantic-core serialization, uvloop embedded loop, HTTP/1.1-only (cleartext behind a TLS proxy).

**Proven wins (the lead — extend these, don't re-pitch them):**
- **Const tier** — prebaked fixed responses from Rust, zero-GIL: ~183k req/s (~2.8× uvicorn+uvloop).
- **Large-payload serialization** — pydantic-core `to_json` beats `json.dumps` (~2.3× at 69 KB; crossover ~1–4 KB).
- **R3 streaming pump** — on-loop `_drain`, bounded-mpsc → hyper, GIL dropped per chunk, backpressure, graceful drain, client-disconnect→upstream-cancel; TTFT metric. Parity-to-1.3× vs Starlette.
- **`@app.batch`** — prefix-aware coalescing: ~12× throughput / ~16× fewer upstream calls vs a rate-limited LLM backend.
- **Cloud resilience + observability** — body/conn caps, slow-loris guard, per-route timeout, graceful drain, RED metrics, streaming TTFT, request-id, W3C traceparent propagation, /livez+/readyz.
- **FastAPI byte-parity** 99.6% (974/978).

**Honest perf envelope (do not oversell).** uvloop embedded loop → tiny one-shot ~59k req/s: **beats FastAPI (~41k), Robyn (~36k), Blazeio (~59k)**; ~88% of raw Starlette+uvloop (~67k) on that one shape (residual FFI/asyncio-bridge cost). Const tier flips it. **Anti-scales with tokio threads (GIL) → deploy MULTIPROCESS.** SO_REUSEPORT LB is Linux-only. Never claim a blanket "faster server"; lead with the wins above + cost-effectiveness for AI.

## Cardinal rules (constrain every candidate win)
1. **No bullshit / no useless logic** — must change the byte path or the operator experience.
2. **Validate under REAL load** (subprocess + HTTP), not just unit tests — multiple headline features shipped green-on-TestClient but BROKEN under load. Any transport/streaming/resilience win needs a real-server adversarial test.
3. **Runtime dep = `pydantic>=2.7` ONLY.** Everything else lazy-imported or test-only. Never make `import lancelot` pull httpx/otel/multipart. (Rust-side crates are fine.)
4. **Vendored FastAPI conformance tests are SACRED** (sha256-verified). Never edit `conformance/vendor/fastapi/`.
5. **`researchfms/.git` is OFF-LIMITS** (nested repo). Use `git -C` if ever needed.
6. **Don't ship a red gate.** Build phase gates: `pytest tests/ -q` + `make m1-conformance` (floor **passes=974**) + `cargo clippy --release --all-targets -- -D warnings` + fmt, all green.

## Quality bar
Mechanism-level: the exact knob / constant / file / function — never vibes. "Robyn releases the GIL in `callbacks.rs` via `Python::allow_threads` around the await, and runs handlers on a `tokio::runtime` with N workers from `processpool.py`" beats "Robyn is fast." Cite peer `file:line` + section. No basics. Be honest: real byte-path win vs micro-opt vs non-bottleneck (e.g. per-chunk stream perf is upstream-rate-limited → not the bottleneck).

## Additive mandate
Lancelot already mined these peers (`research/{robyn,fastapi,blazeio,deep,external,design}` — 26 notes) and shipped M1–M19. **Miners:** extract comprehensively + mechanism-level (don't pre-filter). **Synthesizers:** compare each peer mechanism against Lancelot's current state (the `05_lancelot_state/` baseline notes + `lancelot/` source + `CLAUDE.md`) and surface only **NEW or improvable** wins — explicitly tag already-built ones as **HAVE** so nothing is redundant. The whole point of "additive" is: don't propose rebuilding what M1–M19 shipped.

## Output contract
- **Miners:** write a mechanism-level, cited note to your assigned path under `research/bytepath_mine/`; return the structured digest.
- **Synthesizers:** write the domain brief to `10_synthesis/`; return must_build / improve / have / refuse + buildability + gate-risk.

Full tool access (Read, Grep, Bash, WebFetch). "Research output" restricts your reply FORMAT, not your inputs — **READ THE FULL SOURCE** (ls dirs, read key files in full, grep large ones). Do not guess.
