# M1-EXIT-X1 — Pydantic-model-as-request-bucket + required-noneable

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. TDD = RED→verify-fail→GREEN→verify-pass→exit-gated commit. The byte-oracle is the vendored fastapi 0.136.1 corpus already pinned. NO vendored test is edited (cardinal rule).

**Goal:** ratchet the M1 conformance floor from 36.5% toward ~45% by closing the model-bucket family. Concretely: `Annotated[FormModel, Form()]` parses the urlencoded body into a Pydantic model; `data: Model = Query()` / `Header()` / `Cookie()` does the same for the named bucket with `model_config={"extra":"allow"}` honored; `q: str | None` with NO default is REQUIRED (422 absent, accepts `null` present) — FastAPI's actual semantics.

**Architecture:** All three behaviors converge on one mechanism — Lancelot's existing scalar field bucket machinery (`lancelot/di.py:_raw_for` + `lancelot/validation.py:ParamValidator`) generalized: when the annotation IS a `BaseModel`, lift the parsed bucket into a model validate. No new pipeline; one extra branch in the existing compiler. Zero Rust delta.

**Tech Stack:** Python stdlib + pydantic (existing). Real fastapi 0.136.1 vendored corpus = oracle.

---

### T1 — Form-model body (`Annotated[FormModel, Form()]`)

**Files:** modify `lancelot/routing.py` (body detection: Form+BaseModel → body bucket), `lancelot/di.py` (compile_dependant + `_raw_for` form-as-model path), `lancelot/validation.py` (BodyValidator for form-model).

- [ ] **Step 1: RED** — run the vendored test:
  ```bash
  cd /Users/seventyleven/Desktop/Lancelot
  .venv/bin/python -m pytest conformance/vendor/fastapi/tests/test_forms_single_model.py -q --no-header -p no:cacheprovider --import-mode=importlib -c /dev/null --rootdir conformance \
    -o python_paths=conformance/vendor/fastapi
  ```
  Expect: most tests fail (Form() with BaseModel annotation goes through scalar Form path → wrong loc/422).

- [ ] **Step 2: Implement** — in `compile_dependant`, detect: `default is Form() AND ann is BaseModel-subclass OR Annotated[BaseModel, Form()]`. Lift to body-form path: register as `route.body = (name, FormModelValidator(model))`. In asgi seam, when a form-body route, parse `urlencoded` once, validate against model (pydantic `model_validate`). Honor `model_config={"extra":"allow"}` (pydantic does it automatically). The alias case (`Field(alias="with")`) just works (pydantic handles aliases).

- [ ] **Step 3: GREEN** — re-run the same vendored test. Confirm pass count rises. Run the corpus runner and confirm the overall pct rises.

- [ ] **Step 4: Commit** —
  ```bash
  git add lancelot/routing.py lancelot/di.py lancelot/validation.py
  pytest tests/ -q >/dev/null 2>&1
  rc=$?; test $rc -eq 0 && git commit -m "feat(m1-exit-x1): Form() with Pydantic-model annotation -> model body"
  ```

### T2 — Model as Query/Header/Cookie bucket

**Files:** `lancelot/di.py` (compile_dependant + `_raw_for` model-as-bucket paths).

- [ ] **Step 1: RED** — vendored `test_query_cookie_header_model_extra_params.py` failing.
- [ ] **Step 2: Implement** — `data: Model = Query()` etc.: in compile_dependant, when marker is Query/Header/Cookie AND ann is a BaseModel subclass, register a "bucket-model" field. In solve_async, build the full bucket dict from request.query_params / headers / cookies (NOT just one key) and `model.model_validate(dict)`. The `extra="allow"` passes through automatically. Header model: lower-case + `-`-to-`_` conversion follows the existing header rule.
- [ ] **Step 3: GREEN** — confirm pass count rises on `test_query_cookie_header_model_extra_params`.
- [ ] **Step 4: Commit** — `feat(m1-exit-x1): Pydantic model as Query/Header/Cookie bucket`.

### T3 — `q: str | None` required-noneable

**Files:** `lancelot/params.py:extract_field` (or where `required` is computed).

- [ ] **Step 1: RED** — vendored `test_required_noneable.py` failing (Lancelot likely treats `str | None` as optional).
- [ ] **Step 2: Implement** — FastAPI rule: `required` ⟺ NO default value (irrespective of annotation noneability). The Optional/Union[X, None] annotation just means "accepts None at runtime"; required-ness is purely about the default. Fix `extract_field`: `required = (default is Required)` regardless of annotation noneability. Validate: if absent → 422 missing; if present and None → pass; if present and X → pass.
- [ ] **Step 3: GREEN** — `test_required_noneable.py` passes; confirm M1-P2 422 byte-shape regression suite still passes (no over-/under-emission).
- [ ] **Step 4: Commit** — `fix(m1-exit-x1): q: T|None with no default is REQUIRED (FastAPI rule)`.

### T4 — Ratchet up + review + tag

- [ ] **Step 1: Re-emit classifier manifests** — `python conformance/classify.py --emit && python conformance/classify.py --check`. New A files (if any) properly classified.
- [ ] **Step 2: Run the full corpus** — `make m1-conformance` reports the NEW measured Class-A pct. Set `parity_floor.txt = floor(measured)`. Anti-cheat: ratchet first runs `--check`, then the new floor.
- [ ] **Step 3: Full regression** — `make m1-spine`, `make m0` (must stay green; M0 7/7); `pytest tests/ -q`; zero Rust delta from m1-p7-green; `cargo clippy -- -D warnings` rc=0.
- [ ] **Step 4: Review agent** — anti-cheat re-audit: (a) zero vendored-test edits since m1-p7-green; (b) exclude.toml unchanged (no new excludes); (c) new floor = real measured number (not inflated); (d) the new code is HONEST (no behavior fakery — e.g. Form-model doesn't return a stubbed payload to pass tests).
- [ ] **Step 5: Tag + push + memory** — `git tag m1-exit-x1-green`, `git push --follow-tags`, append to `lancelot-m1-progress.md` (new floor, what shipped, next family).

---

## Self-Review

**Spec coverage:** T1 = forms-single-model + forms-from-non-typing-sequences (~9 tests). T2 = query/cookie/header_model_extra_params (~5 tests). T3 = required_noneable (~2 tests). Combined ~16 test cases close; the actual pct delta depends on collection-time blockers each file has — measured at T4.

**Honest invariants:** (1) NO vendored test edited (validated by `git diff m1-p7-green -- conformance/vendor/fastapi/`); (2) exclude.toml unchanged (anti-cheat self-checks); (3) floor = real measured (the ratchet only-rises makes inflation self-correcting); (4) new code is real implementation, not behavior-fakery (review agent verifies); (5) M1-P2 byte-shape parity (422 wire shape) preserved.

**Risk notes:** T2 (model as bucket) intersects header-name case-conversion logic — must keep the existing M1-P2 header-bucket conversion intact when wrapping a model around it. T3 (required-noneable) intersects M1-P2's optional/default semantics — must not regress the existing `q: int = 0` (optional with default) tests.
