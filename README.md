# Lancelot

```diff
- from fastapi import FastAPI, Depends, HTTPException
- from fastapi.responses import StreamingResponse
+ from lancelot import FastAPI, Depends, HTTPException
+ from lancelot.responses import StreamingResponse

  app = FastAPI()

  @app.get("/healthz")
  async def healthz() -> str:
      return "ok"

  @app.post("/v1/chat/completions", response_model=None)   # streaming → response_model=None
  async def chat(request) -> StreamingResponse:
      async def gen():
          async for chunk in upstream.stream(...):
              yield chunk
      return StreamingResponse(gen(), media_type="text/event-stream")
```

**Lancelot is FastAPI-compatible (99.6% byte-parity) and measurably faster on AI shapes.**

## Measured wins vs the field (M6 lab body, loopback, 30 s × c=50)

| shape | Lancelot rust | Lancelot ASGI | Robyn 0.84 | Starlette+uvloop | FastAPI+uvloop |
|---|---|---|---|---|---|
| **large JSON (~70 KB)** | **9,211** | **8,380** | 7,741 | 2,204 | 278 |
| **streaming (256 chunks)** | **6,137** | 4,020 | 103 | 5,061 | 4,434 |
| **streaming TTFT p50 @ 64 chunks** | **0.88 ms** | 2.47 ms | 5.30 ms | 2.36 ms | 2.67 ms |
| **`@app.batch` coalescing** (rate-limited upstream, c=200) | **2,189** rps | — | — *(no equivalent)* | — | — |
| tiny one-shot JSON | 42,103 | 42,593 | 33,076 | **64,395** | 39,337 |

(Tiny one-shot: uvicorn+uvloop wins — Lancelot's `@app.const` tier is the answer for fixed responses, ~183k rps. See `docs/deploy.md`.)

## Install

```bash
pip install lancelot-framework            # prebuilt wheels — no Rust toolchain needed
pip install 'lancelot-framework[orjson]'  # optional: faster JSON serialization
```

Distribution name is **`lancelot-framework`**; the import stays **`lancelot`**
(the PyPI name `lancelot` is held by an unrelated 2009 package). Requires Python ≥ 3.11.

## Quickstart

```bash
pip install lancelot-framework 'uvicorn[standard]'
python -m uvicorn myapp:app --loop uvloop --http httptools   # FastAPI-parity path
```

```python
# myapp.py — import name stays `lancelot`
from lancelot import FastAPI
app = FastAPI()

@app.get("/healthz")
async def healthz() -> str:
    return "ok"
```

<details><summary>Build from source (contributors / unsupported platform)</summary>

```bash
git clone https://github.com/LeventySeven/Lancelot.git && cd Lancelot
python -m venv .venv && source .venv/bin/activate
pip install maturin && maturin develop --release   # Rust toolchain required
```
</details>

Migration: 1 line in most apps. See **[`docs/migration/from-fastapi.md`](docs/migration/from-fastapi.md)** for the import swap + the four documented behavior divergences + Render/VPS deploy recipes.

## What Lancelot is for

A backend HTTP framework like FastAPI / Robyn that optimizes **transport / serialization / streaming / resilience** for **AI, cloud, web, and startups**. AI logic stays in your handlers — LangGraph, agent loops, vector stores, prompt orchestration are all user-side. We own the byte path.

The four marquee wins:

- **Large-payload JSON** — pydantic-core + orjson auto-detect (`lancelot/validation.py::_wire_json`), beats every peer (1.19× vs Robyn, 33× vs FastAPI on real-per-request 70 KB serialization).
- **Streaming for AI** — R3 pump (on-loop `_drain` coroutine; ~21% over Starlette), bounded backpressure, client-disconnect → upstream cancellation (M4 V2.2).
- **`@app.batch` coalescing** — prefix-aware request grouping against rate-limited LLM upstreams (~12.7× throughput / ~16× fewer upstream calls). No analogue in FastAPI/Robyn/Blazeio.
- **Cloud-native resilience** — graceful drain (M4 V1.4), body cap, connection cap, slow-loris (15 s header timeout), per-route timeout, all validated under adversarial load.

## Deploy

- **Render:** [`benchmarks/m5/exit_gate/render.yaml`](benchmarks/m5/exit_gate/render.yaml) — Docker, multi-service blueprint.
- **VPS (Ubuntu 22.04/24.04):** [`benchmarks/m5/exit_gate/deploy_vps.sh`](benchmarks/m5/exit_gate/deploy_vps.sh) — idempotent provisioning with systemd + nginx + optional certbot.
- **Multiprocess scaling:** `LANCELOT_WORKERS=1` per process, N processes via `SO_REUSEPORT` ([`docs/deploy.md`](docs/deploy.md)).

## Observability

Lancelot emits four OpenTelemetry metrics out of the box (`lancelot.http.{requests, request.duration, requests.in_flight, stream.ttft}`); the importable Grafana dashboard is at [`docs/operator/dashboards/lancelot-red.json`](docs/operator/dashboards/lancelot-red.json). Operator runbook + failure-modes table: [`docs/operator/`](docs/operator/).

## For LLM-driven code agents

If you're an agent (Claude Code, Cursor, …) working in a Lancelot codebase, **read [`docs/agents.md`](docs/agents.md) first.** It covers handler shapes to prefer, the documented anti-patterns, module layout, test commands, and what NOT to modify. For LLM clients with MCP support, register `tools/mcp_docs_server.py` to query the docs as the source of truth.

## Project layout

```
Lancelot/
├── README.md                   ← you are here
├── CLAUDE.md                   ← engineering notes (positioning, cardinal rules)
├── pyproject.toml · Cargo.toml · Makefile
├── src/                        Rust core: hyper · tokio · pyo3 · matchit
├── lancelot/                   Python pkg: app · routing · responses · llm.TransportPool
├── tests/                      pytest (240+ tests, 977/987 = 99.0% FastAPI conformance)
├── conformance/                vendored FastAPI 0.136.1 tests + ratchet
├── benchmarks/                 oha-driven 4-way bench + M5/M6 exit-gate scaffolding
└── docs/
    ├── agents.md               ← read first if you are an LLM
    ├── deploy.md
    ├── migration/from-fastapi.md
    ├── operator/{runbook,failure-modes}.md
    ├── operator/dashboards/lancelot-red.json
    ├── plans/M[0-7]-PLAN-*.md
    └── peer-source-recheck/M{5,6}-*.md
```

## Honest envelope (what NOT to expect)

- **No tiny-one-shot win.** uvicorn+uvloop beats every framework on small `/echo` responses — Lancelot pays per-request FFI overhead. The const tier hits ~183k rps on prebaked responses (e.g. `/healthz`), but ad-hoc handlers don't.
- **HTTP/1.1 only on the native core.** Terminate TLS+HTTP/2 at the proxy (nginx, Caddy, Cloudflare).
- **Linux + macOS only.** No Windows.
- **`SO_REUSEPORT` LB is Linux-only.** macOS binds N sockets but delivers to one — see `docs/deploy.md` for the documented LB-scaling story.
- **No built-in CLI.** Use `uvicorn`, `hypercorn`, or `lancelot._native.run` directly. FastAPI itself delegates to a separate `fastapi-cli` package.

## License + cardinal rules

Apache 2 (TODO: confirm). Engineering rules in [`CLAUDE.md`](CLAUDE.md):

> *No bullshit, no useless logic.* Every change must change the byte path or the operator experience. Validate under real load, not just unit tests. Runtime dep = `pydantic>=2.7` only. AI logic stays user-side. The `researchfms/.git` is OFF-LIMITS — always use `git -C "$LC"`.

Plans: [`docs/plans/`](docs/plans/) — M5 (validated vs peers), M6 (speed + stability + adoption), M7 (distributed plumbing + MCP).
