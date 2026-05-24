# Releasing Lancelot

Operational checklist for publishing a new Lancelot release to PyPI.

## Pre-release gates (every release)

1. **All tests green:**
   ```bash
   .venv/bin/python -m pytest tests/ -q
   ```
2. **Conformance ratchet ≥ 974 (M2 floor):**
   ```bash
   make m1-conformance
   ```
3. **Rust gate clean:**
   ```bash
   $HOME/.cargo/bin/cargo clippy --release --all-targets -- -D warnings
   ```
4. **Wheel builds clean:**
   ```bash
   PATH="$HOME/.cargo/bin:$PATH" .venv/bin/maturin build --release
   ```
5. **Smoke install + import in a fresh venv:**
   ```bash
   python -m venv /tmp/fresh-venv
   /tmp/fresh-venv/bin/pip install target/wheels/lancelot-*.whl
   /tmp/fresh-venv/bin/python -c "import lancelot; print(lancelot.__version__)"
   ```

## Version bump

1. Update `pyproject.toml::project.version`.
2. Update `lancelot/__init__.py::__version__` (must match).
3. Update `CHANGELOG.md` (if present) with milestone-anchored highlights.
4. Tag the release after merging to main:
   ```bash
   git tag -a v0.X.Y -m "Lancelot v0.X.Y — <short description>"
   ```
   Tags follow the cardinal rule: never tag with a red gate.

## Build + publish

Lancelot is a maturin-built Rust extension. PyPI needs platform-specific wheels for every supported Python version × OS × arch combination. CI should build wheels via `maturin-action` for:

- Linux x86_64 (manylinux 2_28)
- Linux aarch64 (manylinux 2_28)
- macOS x86_64 (10.12+)
- macOS aarch64 (11.0+)

Each for **Python 3.11 / 3.12 / 3.13** (per `requires-python = ">=3.11"`).

### Manual publish (for local-built single-platform wheels)

```bash
# 1. Build the wheel for the local platform.
PATH="$HOME/.cargo/bin:$PATH" .venv/bin/maturin build --release

# 2. Inspect what was built.
ls -la target/wheels/

# 3. Upload via twine (after `pip install twine`).
twine upload target/wheels/lancelot-*.whl
```

### CI publish (recommended; not yet wired in this repo)

A GitHub Actions workflow at `.github/workflows/release.yml` should:
1. Trigger on tag push (`v*.*.*`).
2. Use `PyO3/maturin-action@v1` for each platform × Python combo.
3. Upload artifacts to PyPI via `PYPI_API_TOKEN` secret.

Template:
```yaml
on:
  push:
    tags: ['v*.*.*']

jobs:
  build-and-publish:
    strategy:
      matrix:
        target: [x86_64-unknown-linux-gnu, aarch64-unknown-linux-gnu,
                 x86_64-apple-darwin, aarch64-apple-darwin]
        python-version: ['3.11', '3.12', '3.13']
    # ... see maturin-action docs for the full recipe
```

(This file documents the policy; the actual workflow file is not yet committed — wire it up when the team is ready to ship the first PyPI release.)

## Name-collision contingency

`pip index versions lancelot` currently returns "No matching distribution found" — which means **either** the name is fully available **or** taken but with no published versions (or yanked). Before the first `twine upload`, verify the name on https://pypi.org/project/lancelot/ in a browser.

If the name is taken:
- Negotiate with the existing owner (if any) for transfer, OR
- Publish under a different name (e.g. `lancelot-rs`, `lancelot-framework`). Update `pyproject.toml::project.name`, `setup` references in deploy scripts, and the README install instructions. PyPI search rules: names must be unique; case-insensitive; hyphens and underscores are equivalent.

## What's in this release (0.1.0 ship list)

- M0-M4: framework groundwork, FastAPI conformance to 99.6%, M4 resilience validated under adversarial load.
- M5 (committed, deploys pending): 4-way peer bench (Lancelot/Robyn/FastAPI/Blazeio), drop-in compat harness + 2 adoption-friction fixes (`FastAPI=App` alias + lazy `_FASTAPI_COMPAT` re-exports; `-> Response` return-annotation loosening), operator paper trail.
- M6 T1: streaming regression fix (8.4×); orjson on both Python + Rust paths (1.74-1.84× large JSON, beats Robyn); Robyn + Blazeio perf deep-dives.
- M6 T2: leak guards G3+G4 encoded; degraded-upstream tests; chaos cohort (jitter + burst).
- M6 T3: agents.md; MCP docs server; code-first README; PyPI 0.1.0 release prep (this file).

Phase B Render + Phase C VPS deploys + 24h soak are user-driven operations (deploy infrastructure required) — the harness, app, deploy scripts, and analysis docs are all ready in `benchmarks/m5/exit_gate/`.

## Cardinal-rule reminder before push

- Conformance ratchet ≥ 974? ✅ (currently 977/987 = 99.0%)
- All `pytest tests/ -q` green? (must check)
- Clippy + maturin clean? (must check)
- `docs/agents.md` + `CLAUDE.md` reflect the shipped surface? (must check on changes)
- `researchfms/.git` untouched? (cardinal rule #5)

If any answer is no, **do not push**.
