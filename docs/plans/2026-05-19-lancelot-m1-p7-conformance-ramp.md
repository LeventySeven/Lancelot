# Lancelot M1-P7 — Conformance Ramp (real pinned FastAPI corpus + honest ratchet)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. The deliverable is *measurement infrastructure*, not feature code — the "test" is the real FastAPI corpus run through the swap. **Cardinal rule (harness-design §4):** a vendored test is NEVER edited; modifying a test to make it pass "converts a measurement into a lie." The honest outcome is the TRUE pass ratio, whatever it is — the M1 ≥90% is an *exit criterion the ratchet drives toward*, not a number to manufacture this sub-plan.

**Goal:** Replace the 11-file hand-written seed with FastAPI 0.136.1's *exact, byte-unmodified, pinned* test corpus (sdist `tests/` + `docs_src/`, ~592 files); a deterministic classifier with anti-cheat invariants; a runner that drives Class-A through the existing `conftest.py` swap; and a ratchet set to the **real measured Class-A pass ratio**. Report the true number and the v0-subset (table-stakes) pass-rate honestly.

**Architecture:** The swap (`conformance/conftest.py`) and `RealTestClient` already exist and are correct (built M1-P1, used every sub-plan). M1-P7 swaps the *corpus* (seed → real pinned tree) and adds the classifier + manifests + the anti-cheat ratchet. No Lancelot runtime code changes (pure conformance infra; zero Rust delta).

**Tech Stack:** Python stdlib (`tarfile`, `ast`, `tomllib`, `subprocess`, `pathlib`), the existing Starlette-backed swap, the already-downloaded `/tmp/fa_sdist/fastapi-0.136.1.tar.gz`.

---

## File Structure
- Create: `conformance/vendor/fastapi/` — extracted `tests/` + `docs_src/` (read-only, byte-unmodified) + `FASTAPI_VERSION`.
- Create: `conformance/classify.py` — deterministic per-file classifier (Class A / A2-snapshot / B / C-EXCLUDE) + anti-cheat validator.
- Create: `conformance/exclude.toml`, `conformance/xfail_snapshot.toml`, `conformance/v0_subset.toml` — manifests, every line carrying its justification.
- Modify: `conformance/runner.py` — point at the real corpus; run only Class A (+ collect A2 separately); keep subprocess isolation.
- Modify: `conformance/parity_floor.txt` — set to the TRUE measured Class-A ratio (honest baseline; ratchet only-rises).
- Modify: `conformance/ratchet.py` — enforce the anti-cheat (classifier must agree with `exclude.toml`) in addition to the only-rises gate.
- Create: `conformance/vendor/quarantine/test_p7_status.py` — honest RED ledger: the M1-exit gap (true% → ≥90%/100%-v0), the §3-69 real-network Robyn lane, A2 OpenAPI-snapshot lane.
- Modify: `.gitignore` — ensure the FastAPI sdist tarball/tmp is not committed; the vendored tree IS committed (it's the pinned corpus).

---

### Task 1: Vendor the real pinned corpus (read-only, byte-unmodified)

- [ ] **Step 1:** Extract `tests/` + `docs_src/` from `/tmp/fa_sdist/fastapi-0.136.1.tar.gz` into `conformance/vendor/fastapi/` verbatim. Write `conformance/vendor/fastapi/FASTAPI_VERSION` = `0.136.1`. Add a `conformance/vendor/fastapi/README.md` stating: pinned, read-only, NEVER edited; bumping the version is a reviewed event.
- [ ] **Step 2:** Verify byte-fidelity: `tar` extraction count == sdist `tests/**.py` + `docs_src/**.py` count; `git status` shows only additions under `conformance/vendor/fastapi/`.
- [ ] **Step 3:** Commit: `chore(m1-p7): vendor pinned fastapi 0.136.1 test corpus (read-only)`.

### Task 2: Deterministic classifier + anti-cheat manifests

- [ ] **Step 1:** `conformance/classify.py` — for each `vendor/fastapi/tests/**/*.py`, parse imports via `ast` (no execution) and tag, in order: (1) under `tests/benchmarks/` → EXCLUDE(perf); (2) imports a `FASTAPI_PRIVATE` symbol (`fastapi.dependencies.utils|models`, `fastapi._compat*`, `fastapi.utils`, `fastapi.concurrency`, `fastapi.openapi.utils`, `fastapi.params`, `fastapi.routing` *private symbols only*) → EXCLUDE Class-C (record the symbol); (3) asserts a byte-exact `/openapi.json`|`/docs`|`/redoc` snapshot (`inline_snapshot`/`client.get("/openapi.json")` + `== {`) → LANE A2; (4) constructs `Request`/`WebSocket` in-test or introspects `app.router.routes` / custom `APIRoute` → CLASS B; (5) else CLASS A.
- [ ] **Step 2:** Emit `exclude.toml` / `xfail_snapshot.toml` from the classifier with the detected justification per line. **Anti-cheat:** `classify.py --check` FAILS if `exclude.toml` lists a file the classifier did NOT independently tag EXCLUDE (you cannot park a behavioral test in the exclude bin); manifests are append-only.
- [ ] **Step 3:** Commit: `feat(m1-p7): deterministic corpus classifier + anti-cheat manifests`.

### Task 3: Corpus runner through the swap + honest measurement

- [ ] **Step 1:** Rewrite `conformance/runner.py` `run()` to: collect Class-A files from the classifier, run them in a subprocess pytest with the swap conftest (existing mechanism) and `vendor/fastapi` on `sys.path` (so `from docs_src... import app` resolves), `--ignore` benchmarks/Class-C/quarantine, `-p no:cacheprovider`. Parse `passed/failed/error/total`. Return `(passed, total)` over **Class A only** (A2 measured separately, reported, not in the gate).
- [ ] **Step 2:** Run it. Record the TRUE numbers (Class-A passed/total/%, A2 count, Class-B count, Class-C excluded count + reasons). **Do not modify any vendored test or selectively exclude to move the number.**
- [ ] **Step 3:** Commit: `feat(m1-p7): real-corpus runner through the swap (honest measurement)`.

### Task 4: Ratchet to the TRUE floor + v0_subset report

- [ ] **Step 1:** Set `conformance/parity_floor.txt` to the real measured Class-A % (floored to a defensible integer; the ratchet only ever rises from here — this is the honest baseline, not a target).
- [ ] **Step 2:** `conformance/v0_subset.toml` — the table-stakes file globs from harness-design §6 that map to *implemented* features (routing/path/query, body+validation, the 422 wire-shape, Depends+overrides+yield, lifespan, CORS, sync/async, WS, static, background, security, urlencoded forms). Report its pass-rate separately and honestly.
- [ ] **Step 3:** `ratchet.py` — in addition to "pct may only rise ≥ floor", invoke `classify.py --check` and fail CI if the anti-cheat is violated (exclude.toml drifted from the classifier).
- [ ] **Step 4:** `make m1-conformance` / `m1-spine` / `m0` still green (the ratchet now gates the real corpus at the true floor). Commit: `feat(m1-p7): ratchet at the TRUE measured floor + v0-subset + anti-cheat gate`.

### Task 5: Honest ledger + clean-sweep + review + tag/push

- [ ] **Step 1:** `conformance/vendor/quarantine/test_p7_status.py` — RED ledger stating, with the real numbers: (a) the M1-exit gap = `true Class-A %` → ≥90% / 100%-of-v0-subset (the remaining FastAPI-feature surface, by failing-test theme); (b) the §3-69 real-network Robyn-integration lane (still in-process ASGI only); (c) Lane A2 OpenAPI/HTML byte-snapshots are semantic-equiv-only at v0. No overstatement: M1-P7 delivers the *honest gated measurement*, not a ≥90% claim.
- [ ] **Step 2:** Full regression: `pytest tests/ -q`, `make m1-conformance`/`m1-spine`/`m0`, import-clean; zero Rust delta since `m1-p6-green` (`git diff --stat m1-p6-green -- 'crates/**/*.rs'` empty); `cargo clippy -- -D warnings` rc=0.
- [ ] **Step 3:** Review agent (anti-cheat focus): independently re-classify ≥10 sampled files; confirm no vendored test was edited (`git diff` vs sdist); confirm `exclude.toml` contains no behavioral test; confirm the floor == the real measured number (not inflated); confirm the ledger is honest about the M1-exit gap. Verdict TAG-READY/NOT-READY.
- [ ] **Step 4:** Tag `m1-p7-green`, `git push --follow-tags`, update memory `lancelot-m1-progress.md`: M1-P7 done; **M1 status = the honest true conformance number + the explicit exit gap to ≥90%**; next = M2 (AI tier) same approach.

---

## Self-Review
**Spec coverage:** harness-design §3 (vendored/read-only/pinned) → T1; §5 classifier + anti-cheat → T2; §4.1 swap reuse (already built) → T3; §6 v0-subset + ratchet only-rises → T4; §6.1/§7 divergences + Robyn lane ledgered → T5.
**Placeholder scan:** none — the classifier rule is fully specified (the §5 ordered list); the manifests are generated, not hand-waved; the measured number is whatever the run yields.
**Honesty invariants (the whole point):** (1) no vendored test edited — verified by diff vs sdist in T5; (2) `exclude.toml` cannot hide a behavioral test — enforced by `classify.py --check` in `ratchet.py`; (3) `parity_floor.txt` == the real number — the ratchet only rises, so a too-low floor self-corrects upward and a manufactured-high floor would fail the very next run; (4) the M1 ≥90% is documented as the *exit criterion the ratchet enforces*, and the true current number + the gap is stated plainly in the ledger and memory. This sub-plan delivers an honest, gamed-proof measurement — consistent with the byte-differential discipline held across P4–P6.
