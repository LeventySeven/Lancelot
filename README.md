# Lancelot

A fast, modern Python web/AI backend framework — "faster FastAPI", built for startups, web apps, and AI backends (LLM token streaming, agents). Intended architecture: **Rust core + Python API** with FastAPI's ergonomics and Blazeio's allocation discipline.

> Private project. This entire folder is gitignored.

## Status

- **Research phase: complete.** Source-level reverse-engineering of FastAPI, Robyn, and Blazeio (9 deep dossiers, 8,645 lines) is in `research/`.
- **Start here:** `research/SYNTHESIS.md` — head-to-head "which framework exceeds at what", the consolidated steal-list, the anti-patterns, and the evidence-grounded Lancelot architecture blueprint.
- **Next:** brainstorm the framework design from the SYNTHESIS blueprint (the Rust+Python specifics, the open questions in §4.8). Implementation deferred until the design is settled.

## Layout

```
Lancelot/
├── README.md              ← you are here
└── research/
    ├── SYNTHESIS.md        ← the payoff: comparison + Lancelot blueprint
    ├── fastapi/   01 lifecycle · 02 dependency-injection · 03 validation/serialization/openapi
    ├── robyn/     01 rust-core/GIL · 02 routing/types/ws · 03 python-api/pyo3/AI-MCP
    └── blazeio/   01 protocol/parser · 02 streaming/static · 03 app/client/utils
```

Every research file: source read line-by-line, `path:line` cited, KNOWN/INFERRED/DESIGN labelled, ≥800 lines, framed as *steal / avoid for Lancelot*.
