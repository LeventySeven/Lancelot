# Lancelot M6 Plan — Speed, Stability, Adoption

**STATUS: 📋 PLAN (not started).** Predecessor M5 is code complete (commits `777fa99..9ae8f3f`, 14 commits); Phase B Render + Phase C VPS deployed-validation runs deferred from M5 land here as M6 T2.3/T2.4.

**Date:** 2026-05-24
**Anchor rule (user-restated):** *no bullshit, no useless logic.* Every M6 item either fixes a measured M5 gap, validates a peer claim under our load shape, or applies a YC-transcript pattern that materially changes adoption math. Nothing speculative.

## Why this milestone exists (the M5 outcome)

M5 produced the harness + the lab-body baseline + the documented FastAPI divergences. Three concrete findings now point at M6:

1. **Two measured perf bugs.** Lab body (60 s × c=50 loopback): Robyn beats Lancelot 1.6× on 70 KB JSON (8.1k vs 5.0k rps — Robyn ships with orjson, we use pydantic-core); `uvicorn-lancelot` streaming hits 529 rps vs native rust's 6,198 rps — a ~10× regression we own.
2. **Leak guards planned, not encoded.** M5 T2.a soak driver landed but its G3 (RSS slope > 5%/h over 4 h window) and G4 (FD monotonic over 1 h) guards were documented as "planned" — only G1 (success rate) and G2 (p99 doubling) actually trip. Endurance claims need the rest.
3. **Adoption flywheel from the YC transcripts.** Trigger.dev's "two users now" pattern (human + LLM) — MCP docs server, agents.md for LLM convention, code-first landing page, open-source-LLM-recommendation flywheel — is the playbook for Python AI backend adoption. Assembly AI's reliability-creates-habit thesis is the migration-story angle. Both apply directly to Lancelot's positioning.

**M6's thesis:** *M5 produced evidence and the documented gaps it exposed; M6 closes those gaps in code (T1), encodes the planned-but-not-coded guards + runs the deferred soaks (T2), and applies the LLM-legible-product patterns the YC transcripts validate (T3).*

## Headline claim M6 must enable

> "Lancelot is FastAPI-compatible (99.0% byte-parity + 977 conformance + zero-patch drop-in on K ≥ 3 real OSS apps), measurably parity-or-better than every named peer (FastAPI, Robyn, Blazeio) on every M5 shape AND holds 24 h soak-clean on the deployed exit-gate, AND is LLM-recommendable (MCP docs server + agents.md + code-first README — the Trigger.dev flywheel applied to Python AI backends)."

Three sub-claims (drop-in zero-patch, perf parity-or-better-with-soak, LLM-recommendable) — each backed by reproducible evidence in the repo.

## Honest state of the framework (updated for M6)

| Claim | Status (post-M5) | M6 changes |
|---|---|---|
| FastAPI byte-parity | **Proven** 977/987 = 99.0% (up from 974 post-M5 routing.py loosening) | T3.4 adds real-app evidence on top |
| Large-payload JSON serialization | **Real per-request loss to Robyn** (5.0k vs 8.1k @ 70 KB) | T1.2 closes the gap via orjson |
| Streaming rps | **Native rust wins** (6.2k @ 256 chunks vs Starlette 5.1k) | T1.1 fixes the ASGI-Lancelot 10× regression |
| Streaming TTFT | **Top-3 across shapes** (p50 0.88 ms @ 64 chunks tied with Blazeio) | T1 holds; T1.4 documents WHY Blazeio ties |
| `@app.batch` coalescing | **~12.7× vs no-coalescing** (2,189 vs 172 rps c=200) | Held |
| Soak / endurance | **G1/G2 encoded; G3/G4 documented but not coded** | T2.1 encodes G3/G4 |
| Adversarial under load | **Validated** (slow-loris, body-cap flood, conn cap, drain, timeout) | T2.2 adds slow-upstream + jitter + partial-outage |
| Drop-in compat | **Fixture-tested only** (1 fixture, 2 friction-fixes found) | T2.b → T3.4: K ≥ 3 real OSS apps |
| Operator paper trail | **Migration cookbook + Grafana JSON + runbook + failure-modes** | T3.3 rewrites README code-first |
| Peer source recheck | **3-axis comparative doc** | T1.3/T1.4 add per-framework perf deep-dives |
| **LLM-friendly surface** | **None** | **T3.1 (agents.md) + T3.2 (MCP docs server)** |
| **On PyPI** | **No** (`pip index versions lancelot` → no matching) | T3.5 release prep (push is user's call) |

## Estimation note (read this before sizing)

Per [[agentic-coding-timing]]: coding hours and wall-clock-bound hours are estimated separately. M6 coding ~16-26 h agentic; M6 wall clock dominated by Phase B 6 h soak + Phase C 24 h soak in T2 (~30 h sequential, ~24 h if soaks run in parallel across instances). T1.6 sustained re-bench adds ~25 min wall clock. T3.4 drop-in apps add 30-min-to-hours per app variable.

## Deployment progression (carry-forward from M5)

Same two-phase shape: Phase B Render (adoption-signal deploy) → Phase C VPS pair (production-scale, M6 EXIT GATE). The `benchmarks/m5/exit_gate/` scaffolding (`render.yaml`, `Dockerfile`, `deploy_vps.sh`, `nginx.conf`, 4 framework apps + mock upstream) is ready as-is; M6 T2.3/T2.4 run it.

## M6 items

### Track T1 — Speed (close measured gaps + peer deep-dives)

**Goal:** Both M5-measured perf bugs closed (or root-caused as physically-bounded); deep-dive published for Robyn and Blazeio; any low-risk borrow landed; M6 lab body shows ≥ M5 on targeted shapes + no regression elsewhere.

| # | Item | Why | Done = |
|---|------|-----|--------|
| T1.1 | **Fix `uvicorn-lancelot` streaming 10× regression.** Native rust hit 6,198 rps on `/stream` (256 chunks); ASGI-Lancelot on uvicorn hit 529 rps. Investigate the `@app.stream` adapter for the ASGI seam — likely per-chunk Event coercion or an extra middleware layer the native path bypasses. | Lab body finding we own; ~10× gap on a shape we ship the marquee win on. | Bench post-fix: `uvicorn-lancelot /stream` rps ≥ 80% of native rust path; root-cause documented in commit message; regression test in `tests/test_m6_*` exercises the path the bug lives on. (Coding ~2-3 h.) |
| T1.2 | **orjson swap experiment.** On 70 KB JSON: Robyn 8.1k (orjson) vs Lancelot 5.0k (pydantic-core to_json). Add `LANCELOT_JSON_SERIALIZER=orjson` opt-in OR detect+prefer orjson if installed, OR document why pydantic-core is structurally slower and can't catch up. | Marquee shape; bounded experiment. | Bench rerun on `LC_BENCH_ITEMS=1500` shows ≥ Robyn's 8.1k OR a written + measured reason. (Coding ~1-2 h; bench ~10 min.) |
| T1.3 | **Robyn perf deep-dive.** Read source: `robyn/jsonify`, the orjson + Response writing path, actix-web vs hyper per-request overhead. Output: `docs/peer-source-recheck/M6-ROBYN-PERF.md`. | "Analyze how Robyn is fast" per user direction; informs T1.5. | Doc cites file:line per finding; classifies BORROW/AVOID/ALREADY-HAVE; per-finding row in summary table. (Coding ~1-2 h.) |
| T1.4 | **Blazeio perf deep-dive.** Read source: `BufferedProtocol` zero-copy receive, `SharpEvent` single-waiter primitive, the manual `r.writer()` chunked fast path, the `with web: web.runner()` event-loop strategy. Output: `docs/peer-source-recheck/M6-BLAZEIO-PERF.md`. | "Analyze how Blazeio is fast" per user direction; confirms/denies the two M5 T3 BORROW candidates. | Same shape as T1.3. (Coding ~1-2 h.) |
| T1.5 | **Adopt low-risk findings.** From T1.3/T1.4 + T1.2: any borrow that passes the gate (small + no Tier-D violation + measured win OR clear correctness). Each adoption: regression test + bench rerun + commit. | Closes the loop between "found X" and "shipped X." | Per adoption commit, gating on `pytest tests/ -q` + conformance ≥ 974 + clippy clean. (Coding variable, ~30 min - 2 h each.) |
| T1.6 | **M6 lab body re-bench + comparative analysis.** Run `m5/run_load_4way.sh`, `m5/run_ttft.sh`, `m5/run_batch_4way.sh` post-T1. Diff vs M5 ANALYSIS.md. Commit raw dumps + `benchmarks/results/M6/ANALYSIS.md`. | M6 T1 done means measurable improvement; the diff is the proof. | Each shape: M6 rps ≥ M5 on T1.x-targeted shapes; M6 rps within ±10% of M5 elsewhere (no regression on shapes we didn't optimize). (Coding ~30 min; **wall clock ~25 min**.) |

**Done = (T1):** Both M5-measured perf bugs closed (or physically-bounded with a documented reason); Robyn + Blazeio perf docs published; M6 lab body ANALYSIS.md committed showing the diff vs M5.

---

### Track T2 — Stability (encode planned guards + run deferred soaks + chaos)

**Goal:** Endurance claims hold — leak guards trip on real leaks, the deferred Phase B + Phase C soaks run green, degraded-upstream paths recover correctly. This is the part FastAPI users need to bet their service on Lancelot.

| # | Item | Why | Done = |
|---|------|-----|--------|
| T2.1 | **Encode M5 leak guards G3 + G4 in `soak.py`.** G3: RSS slope > 5%/h over the trailing 4 h window → fail. G4: FD count OR asyncio task count monotonically non-decreasing over the trailing 1 h window → fail. Self-test: a synthetic-leak fixture that increments a global list per request fires both guards within the configured windows. | M5 T2.a documented them as planned; M6 ships them. Without these, the 24 h soak is just a long G1/G2 run. | `soak.py` exits non-zero on the synthetic-leak fixture; CI lane (1 h soak) green on the deployed exit-gate. (Coding ~1-2 h.) |
| T2.2 | **Degraded-upstream tests.** Three named scenarios against `mock_upstream.py`: (a) slow upstream — bump `MOCK_LOOKUP_MS=2000`, assert Lancelot's per-route timeout + circuit-breaker semantics hold; (b) network jitter — add packet-loss via `tc qdisc` on the upstream interface (Linux only — skip on macOS), assert TTFT degrades gracefully; (c) partial outage — mock upstream returns 503 50% of the time, assert retries + caller-visible error rate stay bounded. | "Make every degraded-upstream path testable" is the operator-confidence story; M5 V1 covered adversarial *clients*, not adversarial *upstreams*. | `tests/test_m6_degraded.py` covers (a)+(c) on the existing harness; (b) is documented as Linux-only and runs in Phase C. (Coding ~1-2 h.) |
| T2.3 | **Phase B — Render deploy + 6 h Phase-B soak.** Connect repo in Render dashboard → New Blueprint → `benchmarks/m5/exit_gate/render.yaml`. 1 h smoke on all 5 services (4 frameworks + mock upstream). 6 h soak with T2.1 leak guards on. | Carry-forward from M5; the adoption-signal deploy. | 6 h soak green for Lancelot (peers may fail — fair result, strengthens the pitch); dumps in `benchmarks/results/M6/soak/render/`. (Coding ~0 h beyond M5 scaffolding; **wall clock 6 h** + ~30 min Render provisioning.) |
| T2.4 | **Phase C — VPS pair deploy + 24 h soak. M6 EXIT GATE.** Provision Linux VPS pair (4 vCPU / 8 GB; Hetzner CPX31 or DO Premium Intel — final pick at run time). `deploy_vps.sh` on the app host; bench from second VPS in same region; 24 h soak with T2.1 guards on, against all 4 framework variants (parallel across 4 instances, 1 framework each, ~24 h total wall clock not 96 h). | The endurance claim's load-bearing artifact. | 24 h soak green for Lancelot; dumps in `benchmarks/results/M6/soak/vps/`. T1.6 multiprocess scaling Linux curve published (Lancelot, Robyn, Blazeio, uvicorn) on the same VPS pair while Phase C is up. (Coding ~0 h; **wall clock 24 h**.) |
| T2.5 | **Chaos cohort on the mixed harness.** Extend `m5/run_mixed.sh` with optional `JITTER_MS` (random latency injection per slow-loris connection) + `BURST_C` (periodic spike — 10× benign connections every 60 s for 5 s). Assert benign p99 stays bounded across the burst. | Real adversarial load isn't steady — bursts + jitter expose latency hotspots a flat slow-loris doesn't. | `JITTER_MS=200 BURST_C=500 m5/run_mixed.sh` runs cleanly against Lancelot for ≥ 30 s; benign p99 ≤ 2× the M5 baseline p99 under burst. (Coding ~1 h.) |

**Done = (T2):** G3+G4 leak guards encoded + self-tested; T2.2 degraded-upstream tests green; Phase B 6 h soak green; Phase C 24 h soak green = M6 exit gate; chaos cohort holds benign p99.

---

### Track T3 — Adoption (the YC-transcript layer)

**Goal:** Apply the patterns the YC Root Access transcripts validate for AI-era developer tools: LLM-legible product surface (Trigger.dev's "two users now"), code-first onboarding, real OSS-app drop-in proof, PyPI release-ready.

| # | Item | Why | Done = |
|---|------|-----|--------|
| T3.1 | **`docs/agents.md` — Lancelot conventions for LLM consumption.** Distinct from CLAUDE.md (project notes for human ops). agents.md content: handler shapes to prefer (async over sync; explicit return types; `response_model=None` for streaming), idioms to avoid (no per-chunk Pydantic validation; no `BaseHTTPMiddleware` for streaming-sensitive paths), where to put what (routers, deps, middleware), how to run tests, the explicit drop-in surface from `from-fastapi.md`. Cross-link from CLAUDE.md + README. | Trigger.dev pattern: "two users now: human user + LLM user." When Claude/Cursor see `agents.md` in the repo they treat it as conventions to honor. | Doc committed; Cursor / Claude Code given a Lancelot repo task pick up the conventions without further prompting (manual spot-test). (Writing ~1-2 h.) |
| T3.2 | **MCP docs server for Lancelot.** Standalone Python package (or script in `tools/mcp_docs_server.py`) that exposes `docs/`, `CLAUDE.md`, `from-fastapi.md`, `agents.md`, the runbook, the failure-modes doc, and the conformance ratchet results over MCP. Tooling instructions on the MCP read end: query "how do I do X in Lancelot" → answer from source-of-truth docs, not LLM training data. | Trigger.dev pattern — closes the LLM-recommendation flywheel. Once users add the MCP server, Lancelot answers in their IDE come from the actual docs. | Standalone script + README section documenting installation + a smoke test that confirms the server responds to a sample query. (Coding ~2-3 h.) |
| T3.3 | **Code-first README rewrite.** Open with a 15-line FastAPI → Lancelot diff (import swap + 3 routes including a streaming one) BEFORE any marketing prose. Then a 1-line headline claim ("FastAPI-compatible + measurably faster on AI shapes"), then the 3 measured wins table from M6 ANALYSIS.md, then the migration cookbook link. Existing prose moves below or into `docs/why-lancelot.md`. | Trigger.dev pattern: "I always hate going to someone's developer website and you have to scroll and there's no code." Developers decide from code shape. | README first 50 lines are 70%+ code + 30% tight prose; the M6 numbers appear in the first screen; the diff compiles and runs. (Writing ~1-2 h.) |
| T3.4 | **Drop-in compat on K ≥ 3 real OSS FastAPI apps.** Carry M5 T2.b. Pick: tiangolo's `full-stack-fastapi-template` (the "must work" baseline), one streaming-agent app (LangGraph examples or similar), one LLM-proxy-shape (LiteLLM proxy or a slim equivalent). For each: `python benchmarks/m5/drop_in_test.py /path/to/app --test-cmd "..." --report ...`. Document every patch needed; root-cause each + close the friction in Lancelot if possible (mirrors the 2 fixes M5 found from the fixture). | "Drop-in compat" claim is only as strong as the apps it's verified against. The fixture proved the pattern; M6 proves it on real code. | `docs/migration/drop-in-results.md` — one row per app: ran-unmodified / patches-needed-with-diffs / root-cause / referenced-issue. Target: ≥ K=3 apps; ≥ 2 land at "ran-unmodified" or "1 line of patches with documented rationale." (Coding ~1 h harness; variable per-app debug.) |
| T3.5 | **PyPI 0.1.0 release prep.** Version bump `0.0.0 → 0.1.0` in `pyproject.toml` + `__init__.py`. PyPI name-availability check (`pip index versions lancelot`). MANIFEST cleanup if needed. Build + test the wheel locally (`maturin build --release`; `pip install` it into a fresh venv; smoke). Release docs in `docs/releasing.md`. **The actual `twine upload` is the user's call** — M6 ships the readiness; user pushes when ready. | `pip install lancelot` is the migration story's first line. Today it 404s. | Wheel builds clean; smoke install + import + a 3-route app run; `docs/releasing.md` documents the publish command. No actual upload unless the user explicitly asks. (Coding ~30-60 min; +1 h for name-collision contingency.) |

**Done = (T3):** agents.md committed; MCP docs server runnable; code-first README live; ≥ K=3 OSS apps drop-in documented; PyPI 0.1.0 wheel built + release docs committed (push is user's call).

---

## Explicitly NOT in M6 (still Tier-D)

- ❌ **Snapshot/restore / durable execution.** Trigger.dev's deep bet; orchestration = user-side per the cardinal rule. Document the boundary.
- ❌ **TypeScript SDK.** Even if "TypeScript is the de facto AI app language" (Trigger.dev claim), Lancelot is Python-only. We own the Python AI backend niche.
- ❌ **`lancelot run …` CLI.** Same Tier-D as M5 — `uvicorn` + `_native.run` already work.
- ❌ **Custom JSON re-implementation.** orjson is the choice if T1.2 lands it; not a Lancelot-built serializer.
- ❌ **HTTP/2 cleartext (h2c) on the Rust core.** TLS+HTTP/2 terminate at the proxy; documented in `from-fastapi.md`.
- ❌ **Windows support.** Linux + macOS only; documented.
- ❌ **Per-chunk Pydantic validation on streams.** Already-stated perf cliff.
- ❌ **High-risk peer adoptions (Blazeio's full `BufferedProtocol` re-wiring).** T1.4 reads + classifies; landing is M7 or later if T1.6 bench shows a real gap. M6 stays in low-risk-adoption territory.

## How each M6 item is validated

Every item ships with: (1) a **real harness** (oha / a real Python load client / a real deploy) — never an in-process shim that can hide the bug; (2) the metric it must hit, stated upfront; (3) a regression test exercising the bug's actual path (M4/M5 found 4+2 broken-under-load features all green on unit tests — assume the discipline); (4) raw evidence committed to `benchmarks/results/M6/`; (5) all existing gates green at each chunk commit (`pytest tests/ -q` + `make m1-conformance` ≥ 974 + `cargo clippy --release --all-targets -- -D warnings`).

## Sequencing (3 tracks share infrastructure where possible)

1. **T1.3 + T1.4 start immediately** — pure source reading, no infra dependency. Parallel-safe.
2. **T1.1 (streaming regression) + T1.2 (orjson)** — independent code paths, parallel-safe.
3. **T1.5 adoptions** — gated by T1.3/T1.4 findings + T1.2 result.
4. **T1.6 lab body re-bench** — gates on T1.1, T1.2, T1.5 landed. Output feeds T2.3/T2.4 deploys + T3.3 README numbers.
5. **T2.1 (leak guards) + T2.2 (degraded upstream) + T2.5 (chaos)** — independent of T1; can land in parallel with T1.x. T2.1 self-test is required before T2.3/T2.4.
6. **T2.3 (Render) → T2.4 (VPS)** — Render first per the M5 progression (adoption-signal then production-scale). Phase C VPS is the M6 exit gate.
7. **T3.1 + T3.3 (agents.md + README) writing-track** — runs in parallel with bench/soak wall-clock waits; gated on T1.6 ANALYSIS for the numbers in README.
8. **T3.2 MCP docs server** — independent; ships when written.
9. **T3.4 OSS app drop-in** — gated on T1.5 (any drop-in friction the bench surfaced fixed first) + Lancelot install path (T3.5 builds it).
10. **T3.5 PyPI prep** — independent until the publish step; user gates push.
11. **M6 exit tag** — only after: T1 lab body shows ≥ M5 on targeted shapes; T2.4 24 h VPS soak green; T3.4 K ≥ 3 OSS apps documented; T3.1/T3.2/T3.3/T3.5 artifacts committed.

## Definition of "no bullshit" (unchanged, restated)

Every item: closes a measured M5 gap OR validates a peer claim under our load OR applies a YC-transcript pattern that materially changes adoption math; changes the byte path or operator experience (never the AI logic); exercised under REAL load (subprocess + HTTP, or a real deploy); cites the metric or evidence artifact it produces. If an item can't name that artifact, it doesn't belong here.

## Estimation summary

| Track | Coding (agentic) | Wall-clock-bound | Notes |
|---|---|---|---|
| T1 Speed | ~6-10 h | ~30 min (sustained re-bench) | T1.1 streaming regression is the biggest unknown |
| T2 Stability | ~4-6 h harness | **6 h Render + 24 h VPS, both in parallel across 4 instances** | The single biggest wall-clock item — ~30 h total wall |
| T3 Adoption | ~6-10 h | variable per-app debug for T3.4 | Writing + MCP server + per-app debug; bounded by K=3 |
| **Total** | **~16-26 h coding** | **~30-32 h wall-clock (with parallel soaks)** | Sequential serial ≈ 4-6 days; parallel ≈ 2-3 days |

Same shape as M5: **wall clock is the soak**. The coding fits in a single session; the validation runs are the long pole.

## Adoptions documented from YC Root Access transcripts (referenced in T3)

Per direct read of `researchfms/core/TRANSCRIPTS_YC_ROOT_ACCESS.md`, focused on backend/HTTP/AI-infra sections (Assembly AI / Trigger.dev primarily; spot-checks of Artie, Recall.ai, Cartesia, Mem0, Context Engineering). Patterns LANDED in M6:

- **"Two users now: human + LLM"** (Trigger.dev) — drives T3.1 (agents.md) + T3.2 (MCP docs server).
- **Code-first landing page** ("I just want to see the code" — Matt, Trigger) — drives T3.3 (README rewrite).
- **Open-source = LLM training-footprint advantage** (Trigger.dev) — drives T3.5 (PyPI release prep) + T3.2 (MCP server) so LLMs learn the real Lancelot, not a memorized snapshot.
- **Reliability → habit-formation** (Dylan Fox, Assembly AI on Amazon Echo) — drives T2 stability framing; documented in T3.3 README's "why Lancelot" section.
- **Backend tests as agent-verifiable signal** (Eric, Trigger) — already our M5 discipline; validates the existing 229-test suite + the M6 regression-test rule.

Patterns NOT landed in M6 (Tier-D from transcripts):

- ❌ Trigger.dev's full snapshot/restore primitive — orchestration is user-side.
- ❌ TypeScript SDK — Python-only.
- ❌ Forward-deployed-engineer go-to-market — not a framework concern.

---

**Cross-references:**
- M5 plan (predecessor): `docs/plans/M5-PLAN-PROVE-VS-PEERS-AND-ADOPTABLE.md`
- M5 lab body results: `benchmarks/results/M5/ANALYSIS.md`
- M5 peer source recheck: `docs/peer-source-recheck/M5-peers.md`
- M5 exit-gate scaffolding (carried forward as M6 T2.3/T2.4): `benchmarks/m5/exit_gate/`
- Migration cookbook (extended in T3.3): `docs/migration/from-fastapi.md`
- Operator runbook + failure-modes (extended in T2.2): `docs/operator/{runbook,failure-modes}.md`
- YC Root Access transcripts (informed T3): `researchfms/core/TRANSCRIPTS_YC_ROOT_ACCESS.md`
- Honest perf envelope + Tier-D do-not-do list: `CLAUDE.md`
- Estimation discipline: [[agentic-coding-timing]]
