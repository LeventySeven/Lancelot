# Lancelot M5 Plan — Prove vs Peers & Adoptable from FastAPI

**STATUS: 📋 PLAN (not started; predecessor M4 ✅ complete @ tag `m4-sweep-validated`).**

**Date:** 2026-05-23
**Anchor rule (user-restated):** *no bullshit, no useless logic.* Every item below either produces evidence a FastAPI user can quote OR fixes a measured gap that evidence exposed. Nothing speculative; no new surface unless a measured gap demands it.

## Why this milestone exists (the M4 outcome)

M4 ("Prove & Harden") validated every shipped claim under adversarial load — and found **four** headline features broken in real use (R3 streaming pump, `@app.batch` self-cancel, graceful drain, C2 cancellation), each fixed and re-tested. Resilience caps, AI primitives, observability all hold. We have:

- 974/978 = 99.6% FastAPI byte-parity (the 4 remaining are documented no-bullshit boundaries).
- Honest measured wins: large-payload serialization (~2.3× @ 69 KB), streaming (parity-to-1.3× vs Starlette), `@app.batch` (~12× / ~16× vs rate-limited LLM upstream), const tier (~2.8× uvicorn+uvloop on prebaked responses).
- A documented honest envelope (uvicorn+uvloop wins tiny one-shot; multiprocess `SO_REUSEPORT` only LB's on Linux; etc.).

**What we still can't claim — and what a FastAPI user needs to see to switch:**

1. **Head-to-head numbers against the actual peers** (Robyn, Blazeio) — only Starlette/uvicorn variants are in `benchmarks/`. The pitch claims "faster than FastAPI and Robyn, comparable to Blazeio" but the bench doesn't yet prove it.
2. **Endurance.** M4 was burst + adversarial. Nobody has run any of these frameworks (Lancelot included) at moderate steady load for 6-24 h with RSS/FD/task-count watchers, on a real deploy. Adoption requires "won't surprise you overnight."
3. **Drop-in compat on REAL apps, not conformance tests.** 99.6% on FastAPI's OWN test suite ≠ "your app from GitHub runs unmodified." The two are different evidence categories; we only have the former.
4. **Operator-readiness paper trail.** Migration cookbook, importable Grafana dashboards, debug runbook, failure-modes table — nothing of these exists in `docs/`. M4 produced the engineering; M5 produces what an operator actually reads.
5. **Source-level peer recheck.** We have local copies of `researchfms/{robyn/Robyn, blazeio/Blazeio, fastapi/fastapi}`. The existing teardowns (`researchfms/teardowns/{ROBYN,BLAZEIO,FASTAPI}.md`) are good but were written before the M4 reckoning. A fresh read against actual source (especially Robyn — same Rust-base, same FastAPI-replacement target market) will surface what we missed, what we should borrow, what we should explicitly NOT do (and why).

**M5's thesis:** *Shipped + green internal validation under our own load ≠ ready for a FastAPI user to bet their service on Lancelot.* M5 closes that gap with the three things missing above: a 4-way head-to-head bench (lab + one real deploy), endurance + drop-in evidence with documented operator artifacts, and a source-level peer recheck whose low-risk findings land in M5 directly.

## Headline claim M5 must enable

> "Lancelot is FastAPI-compatible (99.6% byte-parity, K real FastAPI OSS apps run unmodified) + measurably faster on AI shapes (streaming TTFT, large-payload JSON, batch coalescing — N% / X× vs FastAPI, parity-or-better vs Blazeio, faster than Robyn) + holds 24 h of soak-clean on a deployed AI-shaped workload."

Three sub-claims (compat, perf, endurance) — each backed by reproducible evidence committed to the repo. This is the artifact a FastAPI user opens before deciding.

## Honest state of the framework (updated for M5)

| Claim | Status (post-M4) | Evidence | M5 changes |
|---|---|---|---|
| FastAPI byte-parity | **Proven** 974/978 = 99.6% | `conformance/` ratchet | T2.b adds REAL-APP evidence on top |
| Large-payload serialization faster than `json.dumps` | **Proven** ~2.3× @ 69 KB | `benchmarks/characterize.sh` | T1 adds Robyn/Blazeio variants on same shape |
| Streaming parity-to-1.3× vs Starlette | **Proven** vs Starlette only | M4 `m4-stream-rearch` | T1 adds Robyn/Blazeio variants + TTFT distributions |
| `@app.batch` coalescing | **Proven** vs rate-limited LLM upstream | `run_batch.sh` | T1 adds peer-runs (peers route 1:1 = control group) |
| Const tier prebaked-bytes | **Proven** ~2.8× uvicorn+uvloop | `m4-const-tier` | T1 includes const path in 4-way bench |
| Resilience under adversarial load | **Proven** | M4 V1.x tests | T1 retains + T2.a soak extends to 24 h |
| AI primitives under load | **Proven** | M4 V2.x tests | T1 includes; T2.a soak runs `@app.batch` workload too |
| Observability emit | **Proven** counters/histograms exact | M4 V3.x tests | T2.c packages as importable Grafana dashboard |
| **Head-to-head vs Robyn / Blazeio** | **Missing** | — | **T1 produces it** |
| **Endurance (steady-load, hours)** | **Missing** | — | **T2.a produces it (6h dev + 24h exit gate)** |
| **Drop-in compat on real OSS apps** | **Missing** | — | **T2.b produces it (K=3 apps documented)** |
| **Operator paper trail** | **Missing** | scattered notes only | **T2.c + T2.d produce it (cookbook + dashboards + runbook + failure-modes)** |
| **Peer source-level recheck** | **Stale** | teardowns predate M4 reckoning | **T3 produces fresh comparative + low-risk adoptions** |

## Estimation note (read this before sizing anything)

Agentic coding compresses pure code-authoring effort by ~10× vs solo-dev estimates. The long pole in M5 is **wall-clock-bound validation time** (24 h soak runs, 30-min sustained benches, per-OSS-app debug sessions), NOT harness code. Every item below estimates **coding hours** and **wall-clock-bound time** separately. Don't pad coding estimates; that hides where the actual wall clock goes.

## M5 items — produce evidence, adopt low-risk wins (no new surface)

### Track T1 — Multi-framework production benchmark (4-way: Lancelot / Robyn / FastAPI / Blazeio)

**Goal:** Reproducible evidence that the M5 headline claim's perf sub-claim is true, against the actual peers, on shapes that mirror real production — not just lab `oha` micro-bursts.

| # | Item | Why | Done = |
|---|------|-----|--------|
| T1.1 | Add `robyn_factory` + `blazeio_factory` to `benchmarks/servers.py` with the same minimal `/echo`+`/stream` shape as the existing variants. Pin to versions in `researchfms/{robyn,blazeio}`. | Without peer factories the head-to-head claim is vapor. | All 4 frameworks runnable from one harness; `run_load.sh` 4-way. (Coding ~1-2 h.) |
| T1.2 | Sustained 30-min `oha` runs across the existing 4 shapes (tiny / mid / large / streaming) on all 4 frameworks. Capture p50/p95/p99 + error rate + RSS at start/middle/end of run. | Today's runs are ≤60 s; many degradation modes only appear past 5 min. | Raw `oha` dumps + RSS samples in `benchmarks/results/M5/sustained/`. (Coding ~1 h; **wall clock ~8 h** = 4 frameworks × 4 shapes × 30 min, sequential.) |
| T1.3 | Mixed adversarial+benign cohort: N benign clients + M slow-loris dribbling clients during the SAME run; assert benign p99 doesn't degrade. Run on all 4 frameworks (peers may fall over — that's a fair result). | Slow-loris isolation is a cloud-native pitch; the comparison is what makes it visible. | Mixed-cohort dumps + a written analysis row per framework. (Coding ~1-2 h; wall clock ~30 min per framework.) |
| T1.4 | Streaming TTFT distributions on a 64 / 256 / 1024-chunk stream. For Lancelot: `lancelot.http.stream.ttft` histogram. For peers: wall-clock first-byte (oha can capture). Tabulate p50/p95/p99. | TTFT is THE AI-streaming UX metric per `researchfms/core/`; today's bench only reports total RPS. | TTFT table + per-framework histogram dumps in `benchmarks/results/M5/ttft/`. (Coding ~1-2 h; wall clock ~30 min.) |
| T1.5 | Batch-coalescing peer comparison: extend `run_batch.sh` to point Robyn/FastAPI/Blazeio at the SAME rate-limited mock backend (cap=8, 50ms/call, c=200). Peers route 1:1 (they have no `@app.batch`) — the comparison is "what does life look like without coalescing?" | Marquee differentiator stated as a number, not just an internal microbench. | 4-way table: throughput, p99 latency, upstream-call-count, error rate. (Coding ~1 h; wall clock ~10 min.) |
| T1.6 | **Linux multiprocess scaling test.** Provision a Linux VPS, run all 4 frameworks at workers=1, 2, 4, 8 procs via `SO_REUSEPORT`. M4 documented Linux-only LB; M5 produces the actual Linux number. | The macOS multiprocess number is a no-distribution floor, not the ceiling. Without Linux numbers, the scaling claim is theoretical. | Linux scaling curve published per framework. (Coding ~1-2 h to wire the recipe; wall clock ~1-2 h benches + VPS time.) |
| T1.7 | **Deployed AI-shaped exit-gate workload.** Three routes against a real deployment on two same-region small VPSs (suggest 4 vCPU / 8 GB, Hetzner CPX31 or DO Premium Intel): `POST /v1/chat/completions` (streaming passthrough to mocked LLM upstream emitting 50 tok/s w/ 10ms TTFT), `POST /api/embed` (large-JSON-out ~32 KB), `POST /api/lookup` (rate-limited mock backend; Lancelot uses `@app.batch`, peers route 1:1). nginx in front for TLS termination, matching the documented deploy model. | Lab numbers always have caveats; one real network-level run is what flips "lab" to "validated." | App source + deploy scripts in `benchmarks/exit_gate/`; 1 h smoke run + raw oha dumps + grafana screenshot committed to `benchmarks/results/M5/exit-gate/`. (Coding ~2-3 h for app+mocks+nginx+deploy scripts; wall clock dominated by 1 h smoke + VPS provisioning ~30 min.) |

**Done = (T1):** 4-way bench across 5 shapes published with raw dumps + analysis in `benchmarks/README.md` (M5 section); one real VPS deployment of the exit-gate workload running a 1 h smoke green; raw evidence committed.

### Track T2 — Migration stability (4 sub-axes for FastAPI users to switch)

**Goal:** Produce the stability evidence + the operator paper trail that lets a FastAPI user open `docs/migration/` and decide.

#### T2.a — Endurance / soak

| # | Item | Why | Done = |
|---|------|-----|--------|
| T2.a.1 | `benchmarks/soak.py` — drives the T1.7 deployed exit-gate workload at moderate steady load (~60% of bench peak), samples RSS / FD count / asyncio task count / OTel gauges every 30 s, plots over time. Same harness runs against all 4 frameworks. | We've never load-tested any of these (Lancelot included) for hours of steady load. Burst+adversarial ≠ endurance. | `soak.py` exists + emits a CSV + a plot script. (Coding ~2-3 h.) |
| T2.a.2 | Endurance guards: fail if RSS slope > 5%/h over the last 4 h window, or if FD count / asyncio task count has ANY monotonic increase over a 1 h window. (Allow temporary spikes; require return-to-baseline.) | A leak guard with no false positives is the whole point; if we can't write the guard we don't know what "stable" means. | Guards encoded in `soak.py` + a synthetic-leak self-test asserts they fire. (Coding ~1-2 h.) |
| T2.a.3 | **24 h soak as M5 exit gate.** All 4 frameworks run the same workload for 24 h. Lancelot must pass the guards; peers may not (that's a fair result that strengthens the pitch). Smaller smokes: 1 h (CI lane) + 6 h (dev iteration). | The headline endurance claim needs the longest run we can stomach; 24 h is the standard. | 24 h dumps + plots in `benchmarks/results/M5/soak/`; per-framework pass/fail row. (Coding ~0 h beyond above; **wall clock 24 h × frameworks-in-parallel-or-serial** — run in parallel on 4 VPSs to keep total at 24 h, or serial for 96 h.) |

#### T2.b — Drop-in compat on real OSS apps

| # | Item | Why | Done = |
|---|------|-----|--------|
| T2.b.1 | Pick K=3 real FastAPI OSS apps. Suggested set (swap based on availability): (1) tiangolo's `full-stack-fastapi-template` (by-the-book FastAPI + SQLModel + Pydantic v2 — the "must work" baseline); (2) a streaming-agent app (LangGraph examples or similar — exercises streaming/SSE); (3) a LiteLLM-shape LLM proxy (exercises dependency-heavy patterns + the LLM-proxy use case). | 99.6% on FastAPI's own tests ≠ "your repo runs unmodified." | App set chosen and pinned. (Coding ~10 min.) |
| T2.b.2 | For each app: `pip install lancelot` + import-swap + run their test suite + smoke-deploy + minimal load. Document every patch needed and root-cause it (either "Lancelot bug, file issue" or "FastAPI-specific behavior we deliberately don't replicate, here's the rationale"). | This is the actual "drop-in" evidence — and the patches we DO need are the M6 input. | `docs/migration/drop-in-results.md` — per-app row: ran-unmodified / patches-needed (with diffs) / root-cause / referenced-issue. (Coding ~30 min/app harness; wall clock variable — hours per app of debug if anything breaks.) |
| T2.b.3 | Target: zero patches across K=3. Tolerated: documented patches with root cause. NOT tolerated: undocumented patches or skipped tests. | Adoption fails on the surprise patch, not the documented one. | The doc above + green-or-explained for every test in every app. |

#### T2.c — Operator confidence (the paper trail)

| # | Item | Why | Done = |
|---|------|-----|--------|
| T2.c.1 | `docs/migration/from-fastapi.md` — step-by-step migration with a real diff (import swap + the one or two behaviors that differ — `BaseHTTPMiddleware` streaming caveat, exception-handler subtleties, the 4 conformance-ceiling cases). Includes a "what NOT to expect" section (no small-response throughput win on tiny one-shot; no Windows support yet; etc.). | A FastAPI user opens this first. If it doesn't read like a friend showed them, they bounce. | Doc written, with a real before/after diff that compiles + runs. (Writing ~2 h.) |
| T2.c.2 | `docs/operator/dashboards/lancelot-red.json` — importable Grafana dashboard for: RED panel (rate/errors/duration histogram from OTel), streaming TTFT histogram, batch coalescing ratio gauge, in-flight gauge, drain state. Tested against the actual metrics emitted in T1.7's deploy. | "Dashboards out of the box" is a stated pitch element; today it's vapor. | Dashboard JSON committed + a screenshot in the deploy doc showing it populated. (Writing+wiring ~2 h.) |
| T2.c.3 | `docs/operator/runbook.md` — first-action playbook for the operational events that DO happen: "slow handler" / "stuck drain" / "leak suspect" / "batch flood" / "TTFT regression" / "upstream dead" / "413 flood". Each row: metric signal → log signature → first action → escalation. | When pages fire, ops people need one page, not a tour of the codebase. | Runbook committed, each row referencing a real metric and a real log line. (Writing ~1-2 h.) |

#### T2.d — Failure-mode clarity (synthesis)

| # | Item | Why | Done = |
|---|------|-----|--------|
| T2.d.1 | `docs/operator/failure-modes.md` — stitches the M4 V1-V3 evidence + T2.a soak findings into a single "here's what happens at the edges" table: input (e.g. 2× max_connections, dead upstream, 10× spike, SIGTERM mid-stream) → outcome → metric signature → recovery action. Pure synthesis; no new code. | The operator story is currently scattered across CLAUDE.md + M4 plan + memory; one page makes it real. | One doc, ~1-2 pages, every row backed by a tagged M4/M5 test or bench result. (Writing ~1 h.) |

**Done = (T2):** 24 h soak passes for Lancelot with leak guards green; K=3 OSS apps documented with target = zero patches; migration cookbook + Grafana dashboard + runbook + failure-modes doc all committed under `docs/migration/` and `docs/operator/`.

### Track T3 — Source-level peer recheck (with adopted low-risk wins)

**Goal:** Read the local copies of `researchfms/{robyn/Robyn, blazeio/Blazeio, fastapi/fastapi}` as primary source — not the existing teardown docs — and find what we missed since M4. Adopt low-risk wins in M5 directly; defer higher-risk findings to M6 with documented rationale.

**Methodology — same 8 axes for each peer:**
1. Request parsing path (header read, framing, decode)
2. Body reading (streaming vs eager, max-body strategy)
3. Response writing (serialization choice, chunking, content-length)
4. Streaming pump (their equivalent of our R3)
5. Connection lifecycle (accept loop, max-conns, drain semantics)
6. Middleware / extension model (where they put hooks, what the chain costs)
7. Routing (matching strategy, path-param handling)
8. Concurrency model (GIL handling, worker model, async runtime)

| # | Item | Why | Done = |
|---|------|-----|--------|
| T3.1 | **Robyn source recheck** (`researchfms/robyn/Robyn`). Same Rust-base, same FastAPI-replacement target market — our most direct competitor on byte path. Special attention to: their tokio worker model, their pyo3 hand-off pattern, their streaming impl, their middleware insertion point. | If anyone has byte-path tricks we haven't tried, it's Robyn. T1 will surface where they're faster; T3.1 explains why. | `researchfms/teardowns/ROBYN.md` updated with an "M5 source recheck" section: 8-axis writeup + classification table (borrow / avoid / already-have / defer). (Reading + writing ~3-4 h.) |
| T3.2 | **Blazeio source recheck** (`researchfms/blazeio/Blazeio`). Pure-Python ASGI server with a distinct architecture. We don't aim to be a Python-only stack but their concurrency choices may inform what we expose. | Blazeio is the perf-leader benchmark we cite; understanding why is necessary for an honest "comparable to Blazeio" claim. | `researchfms/teardowns/BLAZEIO.md` updated with M5 source-recheck section. (Reading + writing ~2-3 h.) |
| T3.3 | **FastAPI source recheck** (`researchfms/fastapi/fastapi`). Not for byte-path wins — for SEMANTIC DIVERGENCE. Where do we differ on purpose? Are all divergences in CLAUDE.md / `from-fastapi.md`? | The migration story breaks on undocumented semantic differences. T3.3 surfaces every one. | `researchfms/teardowns/FASTAPI.md` updated with a divergence-audit section; cross-linked from `docs/migration/from-fastapi.md`. (Reading + writing ~2-3 h.) |
| T3.4 | **Low-risk adoption gate.** For each "borrow" finding from T3.1/T3.2/T3.3, classify by risk. **Low-risk** = (small Rust micro-opt OR well-scoped Python pattern) AND (does not violate any Tier-D rule) AND (closes a measured T1 gap OR fixes a clear correctness issue). Land low-risk in M5. Defer the rest. | Without an adoption gate, the recheck collapses into endless scope. | Per finding: implemented + tested OR deferred-to-M6 with documented rationale. (Coding variable; each low-risk item is ~30 min - few hours.) |
| T3.5 | **Comparative deep-dive doc.** Synthesize T3.1-T3.3 into one cross-framework axis-by-axis writeup. Includes the 4-way bench numbers from T1 for context. | The peer-comparison story for the README + the M5 announcement comes from this. | `docs/peer-comparison.md` — for each of the 8 axes: row per framework with link to source, with one paragraph of analysis. (Writing ~2-3 h.) |

**Done = (T3):** Three updated teardowns + one comparative doc + any low-risk findings landed + any deferred findings logged in an M6 candidates section.

## Explicitly NOT in M5 (still Tier-D / declined; restated for clarity)

- ❌ **Reposition / pivot the pitch.** The CLAUDE.md positioning (AI/cloud/web/startups byte path) is locked; M5 produces evidence FOR it, not for an alternate angle.
- ❌ **Small-response Rust re-architecture** (still declined; if T1 shows it costs us adopters, revisit as M6 spike, NOT in M5).
- ❌ **New AI-primitive surface.** M5 hardens + measures + documents what exists; growing the API would dilute the headline.
- ❌ **Custom JSON serializer (orjson/ujson).** Pydantic v2 + the const tier already cover it.
- ❌ **App-layer gzip.** `App.add_middleware(GZipMiddleware)` already works; the TLS proxy already gzips. Building our own is redundant Tier-D.
- ❌ **Built-in CLI (`lancelot run`).** FastAPI's own delegates to `fastapi-cli`; uvicorn/hypercorn already work; not core.
- ❌ **Windows support.** Not core to the AI/cloud target; document the absence in `from-fastapi.md`.
- ❌ **OpenAPI callbacks / webhooks.** Rare, complex, no payoff for the target market.
- ❌ **Per-chunk Pydantic validation on streams.** Already-stated perf cliff.
- ❌ **Robyn-style hard-kill shutdown.** Explicit anti-goal; T2.a soak validates the opposite.
- ❌ **High-risk T3 adoptions in M5.** Any T3 finding tagged "high-risk" or "large surface" is deferred to M6 with documented rationale, never silently landed.

## How each M5 item is validated (the M4-derived discipline)

Every track-item ships with: (1) a **real-load harness** (oha or a concurrent Python client driving the REAL server in a subprocess or a real VPS — not an in-process unit shim), (2) the metric it must hit, (3) a regression test that exercises the path the bug would live on (M4 found 4 broken-under-load features all green on unit tests — assume the same discipline applies), (4) raw evidence committed to `benchmarks/results/` or `docs/`, and (5) all existing gates green (`pytest tests/ -q` + `make m1-conformance` ratchet ≥974 + `cargo clippy --release --all-targets -- -D warnings`). If validation finds a bug (precedent: 4/4 so far), fix-first then re-measure.

## Sequencing (3 tracks share evidence, run in parallel where possible)

1. **T3.1-T3.3 start immediately** — pure source reading, no infra dependency. Can run while T1.1 (peer factories) is being wired.
2. **T1.1 → T1.2-T1.5 lab body** — peer factories first, then sustained / mixed / TTFT / batch shapes in parallel.
3. **T1.6 + T1.7 + T2.a soak** — share VPS infrastructure. Provision the Linux VPS for T1.6 multiprocess scaling, then re-use the two-VPS exit-gate pair for T1.7 deploy → T2.a soak. Run T2.a soak in parallel across 4 VPSs (one per framework) so total wall clock stays ~24 h, not 96 h.
4. **T2.b drop-in** — gated on T1 lab body proving Lancelot baseline correctness on the head-to-head shapes; otherwise we'd debug bugs that aren't drop-in problems.
5. **T2.c + T2.d + T3.5 writing-track** — can run anytime; happens in parallel with bench/soak wall-clock waits.
6. **T3.4 low-risk adoptions** — land throughout, gated by T3.1-T3.3 findings + T1 measured gaps. Each adoption ships with its own regression test + bench rerun.
7. **M5 exit tag** — only after T1 evidence published + T2.a 24 h soak green + T2.b K=3 documented + all T2.c/T2.d/T3.5 docs committed.

## Definition of "no bullshit" (unchanged, restated for M5)

Every item: produces evidence a FastAPI user can quote OR fixes a measured gap that evidence exposed; changes the byte path or operator experience (never the AI logic); is exercised under REAL load (subprocess + HTTP, or a real deploy), not an in-process shim that can hide the bug. If an item can't name the metric / evidence artifact / doc it must produce, it doesn't belong here.

## Estimation summary (coding hours + wall-clock hours)

| Track | Coding (agentic) | Wall-clock-bound | Notes |
|---|---|---|---|
| T1 | ~8-12 h | ~10-12 h (sustained benches + smoke + provision) | Bench runs the long pole; parallel across VPSs where possible |
| T2.a | ~3-5 h harness | **24 h** soak (parallel across frameworks) | The single biggest wall-clock item |
| T2.b | ~1 h harness + per-app debug variable | per-app, possibly hours each | Bounded by K=3 apps |
| T2.c+d | ~6-8 h writing | — | Runs in parallel with benches |
| T3 | ~10-12 h reading+writing + variable adoption | — | Reading is the long pole; can be parallelized via Explore agents |
| **Total** | **~30-40 h coding** | **~36 h wall-clock (with parallel)** | Sequential serial ≈ 4-6 days; parallel ≈ 2 days |

The single biggest mistake when planning M5 is treating it like M4's burst-validation. M4 fit in a session because the wall clock was minutes. **M5's wall clock is the soak.** Plan around that.

---

**Cross-references:**
- M4 plan (complete): `docs/plans/M4-PLAN-PROVE-AND-HARDEN.md`
- M2-M3 plan (complete): `docs/plans/M2-M3-PLAN-FROM-RESEARCH.md`
- Honest perf envelope: `benchmarks/README.md`, [[lancelot-rust-bench-honest]]
- Deployment recipe (M4 V3.3): `docs/deploy.md`
- Existing peer teardowns: `researchfms/teardowns/{ROBYN,BLAZEIO,FASTAPI}.md` (to be updated in T3.1-T3.3)
- Cardinal rules + Tier-D: `CLAUDE.md`
- Estimation discipline: [[agentic-coding-timing]]
