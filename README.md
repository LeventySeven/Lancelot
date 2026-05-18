# Lancelot

A fast, modern Python web/AI backend framework — "faster FastAPI", built for startups, web apps, and AI backends (LLM token streaming, agents). Intended architecture: **Rust core + Python API** with FastAPI's ergonomics and Blazeio's allocation discipline.

> Project repo: `github.com/LeventySeven/Lancelot.git`. The 2.4 GB `researchfms/` reference dump (cloned repos, tarballs, teardowns) is gitignored; everything else here is tracked.

## Status

- **Research phase: complete.** Source-level reverse-engineering of FastAPI, Robyn, and Blazeio (9 deep dossiers, 8,645 lines) is in `research/`.
- **Design: complete.** `docs/specs/2026-05-19-lancelot-v0-design.md` (authoritative) + `ULTIMATE-PLAN.md`.
- **M0 vertical slice: built & green** (tag `m0-green`). Real socket (SO_REUSEPORT) → hyper → matchit → lazy `Request` → GIL-choreography (GIL dropped across the Python `await`) → pydantic-core one-pass → 200/404. All 7 M0 DoD gates pass: `make m0`.
- **Start here:** `research/SYNTHESIS.md` (comparison + blueprint); the M0 plan is `docs/plans/2026-05-19-lancelot-m0-vertical-slice.md`.
- **Next:** M1 (table-stakes + FastAPI conformance ≥90%) — derived from the WBS waves; M1 carry-forwards tracked in project memory.

## Layout

```
Lancelot/
├── README.md
├── Cargo.toml · pyproject.toml · rust-toolchain.toml · Makefile
├── src/        Rust core: gil · cpu · socket · router · ffi/{fninfo,request}
│              · serialize · dispatch · service · bin/alloc_probe
├── lancelot/   Python pkg: __init__ · app · _native.pyi
├── tests/      trybuild (compile-fail invariant) + pytest (echo/dispatch/
│              acceptance/lifecycle)
├── docs/       specs/ (v0 design) · plans/ (M0 vertical slice)
└── research/   SYNTHESIS.md + fastapi/ robyn/ blazeio/ deep/ design/ external/
```

Every research file: source read line-by-line, `path:line` cited, KNOWN/INFERRED/DESIGN labelled, ≥800 lines, framed as *steal / avoid for Lancelot*.
